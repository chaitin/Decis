# 性能

[English](performance.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [部署](deployment.zh-CN.md)

Decis 跑的是决策模型——每个请求只有一次前向，不生成文本——所以真正重要的数字是单题延迟、
冷启动时间和内存。本页收集项目引用的全部实测数字。这里没有手写的东西：每张表都由
[`benchmarks/report.py`](../benchmarks/report.py) 从 [`benchmarks/results/`](../benchmarks/results/)
里的原始 JSON 生成，仓库里的表与数据不一致时 CI 会失败。

测量是在**一台 24 vCPU、没有 GPU 的 aarch64 机器**上做的。延迟既是 Decis 的性质，也同样是机器的
性质：这类模型通常拿 Apple 芯片上的 MLX 做基准，同一个版本在那里比下面这些 CPU 数字快得多。请把
这些表当作**这台机器**的数字，定容量之前先在自己机器上量一遍。仓库里**没有自己的 GPU 或 Apple
芯片数字**，所以本页的任何结论都不适用于那两种硬件。

## 延迟

<!-- LATENCY:START -->

| 引擎 | 设备 | dtype | 线程 | 1 个问题 | 10 个问题 | 冷启动 | 峰值 RSS |
|---|---|---|---:|---:|---:|---:|---:|
| `laya` | cpu | float32 | 24 | 446 ms | 3,222 ms (322.1 ms/题) | 76.3 s | 2.8 GB |
| `laya-multilingual` | cpu | float32 | 24 | 231 ms | 987 ms (98.7 ms/题) | 72.9 s | 4.84 GB |

由 [`benchmarks/report.py`](../benchmarks/report.py) 从 [`benchmarks/results/`](../benchmarks/results/) 的原始 JSON 生成；**整行取自同一个配置**（torch 在本机的默认线程数，每个 vCPU 一个），样本 p50，单进程，仅请求内批处理。

**这是延迟，不是吞吐。** 每个请求的问题共享同一个 `state`，这是容易的情况。跨请求批处理已实测，结论是在 CPU 上**不提升吞吐**（见下面的批处理一节与 [`design-review.md`](design-review.md) §4-M5）。

<!-- LATENCY:END -->

延迟表刻意选的是容易的情况：一个请求里的所有问题共享同一个 `state`。值得从中读出两件事：

- 一次调用问十个问题，每题的代价比单独问一个低得多。
- 冷启动约 75 秒（上表那一列是 73–76 秒），峰值内存是权重文件的数倍。就绪探针和容器内存上限要按
  这张表来定，而不是按下载体积。

## Apple 芯片、CPU 与容器

在 Apple 芯片的 Mac 上，`uv run decis serve` 与同一个 release 的 `docker run` 并不是同一台硬件：
比较两者的延迟，比较的是两个设备，而不是两种打包方式。

不设 `DECIS_DEVICE` 时，Laya 按 CUDA、Metal（MPS）、CPU 的顺序挑设备（`laya/agent.py:177-182`）。
在 macOS 上那就是 **GPU**。Linux 容器既没有 Metal 也没有 CUDA 运行时，于是同一个镜像在同一台机器上
是以 `fp32` 跑在 **CPU** 上的。镜像里的任何设置都改不了这一点：Docker Desktop 不会把 Apple GPU
暴露给 Linux 容器。

两条 CPU 路径也不等价。PyTorch 的 macOS wheel 能用到 Apple 自己的矩阵内核，`linux-aarch64` 的
wheel 用不到，所以同一段前向在容器里比在宿主机上可测量地更慢——即使两侧把 `DECIS_TORCH_THREADS`
设成同一个值，这个差距依然在。这一部分是平台的性质，不是容器额外加上的开销。

想公平比较就固定设备，并从任意答案的 `decis` 命名空间、或引擎启动时那行
`loaded ... device=... threads=...` 日志里读回真正的设备：

```bash
DECIS_DEVICE=cpu uv run decis serve --host 127.0.0.1   # 与容器同一个设备
```

另外两件事决定你从 playground 上读到的数字：

- **游戏页面打印的是调用前后的墙钟时间**，所以它包含排在别的在途请求后面的等待。引擎一次只做一次
  前向（[AGENTS.md](../AGENTS.md) §3-20），于是一个保持三个决策在途的页面，三次调用报出来的大约是
  1、2、3 次前向。这个排队在 GPU 上小到看不出来，在 CPU 上不是。
- **请求越长越慢**：head 与 state 共用同一个序列预算。要比就比同一个 payload 的两次运行，而不是
  同一个游戏的两次运行。

## 每种引擎、每种设备的 dtype

`kev-0.8b` 的 Qwen3.5 骨干要用 `flash-linear-attention` 和 `causal_conv1d`，而这两个都依赖
Triton/CUDA。在 CPU 上它比 Laya 慢一个数量级，它的 `bf16` 路径还要更差：

<!-- DTYPE:START -->

| 引擎 | dtype | 3 个问题 | 相对 | 对比基线 |
|---|---|---:|---:|---|
| `kev-0.8b` | `fp32` | 1,656 ms | 1x | 基线 |
| `kev-0.8b` | `bf16` | 137,174 ms | 83x | argmax 相同，概率差 ≤0.01 |

由 [`benchmarks/report.py`](../benchmarks/report.py) 生成。每个 dtype 一次观测，所以倍数是数量级结论而不是统计量；对比列是从记录的答案正文算出来的，不是照着表旁的文字写的。

<!-- DTYPE:END -->

所以 Decis 按引擎*和*设备分别选 dtype（[`engines/registry.py`](../src/decis/engines/registry.py)），
对已知很糟的组合给出警告，而不是悄悄选它。

## 跨请求批处理：测过了，结论是否定的

把*不同*请求的问题合并进同一次前向，是设计里原本的吞吐主张。这件事已经测过，结论是否定的：

<!-- BATCHING:START -->

| 序列集 | 序列长度 | padding | 最好的批大小 | 相对串行 |
|---|---|---:|---:|---:|
| 同一 state（正对照，短） | 115–115 | 1.00 | 8 | 1.92x |
| 同一 state（正对照） | 393–393 | 1.00 | 16 | 1.24x |
| 不同 state，长度接近 | 530–674 | 1.10 | 8 | 1.09x |
| 不同 state，长度倾斜 | 140–821 | 2.41 | 8 | 0.34x |

正对照（所有项完全相同、padding 为 1.00）达到 **1.92x**（`shared_short`，batch 8），证明瓶颈确实是每次调用的固定开销，也证明这套测量能测出收益。 真实流量形状（不同 state）最高 1.09x（`uniform`，batch 8），而最差 0.34x（`skewed`，batch 8）。 长度倾斜时 padding 最高 2.47x（`skewed`，batch 16）：每一行都要补齐到批内最长，浪费的算力直接变成更慢的每项成本。

原始 JSON：[`benchmarks/results/`](../benchmarks/results/)；完整表格（每个批大小、padding、盈亏平衡等待、进程数对比）见 [`docs/design-review.md`](design-review.md) §4-M5。批是**直接调用引擎**合成的，没有队列与取消，所以这些数字是真实攒批器的**上界**。

<!-- BATCHING:END -->

## 复现这些数字

```bash
# 延迟表（`decis bench` 转调 benchmarks/run.py）
uv run decis bench --engine laya-multilingual --batch 1,3,10,30   # 写入 benchmarks/results/laya-multilingual-thread-sweep.json
uv run decis bench --engine laya --batch 1,3,10,30

# 跨请求批处理表（`--cross-request` 转调 benchmarks/batch_gain.py）
uv run decis bench --engine laya-multilingual --cross-request --threads 24 --processes 4

uv run python benchmarks/report.py --write                        # 生成全部表格
uv run python benchmarks/report.py --check                        # CI 跑这个
```

仓库里这些文件早于 `decis bench`，所以名字与现在跑出来的不同：两份 Laya 延迟扫描是
`laya-multilingual-sweep.json` 与 `laya-english-sweep.json`，dtype 表来自
`kev-0.8b-cpu-dtype.json`（每个 dtype 一次观测），批处理结果是
`laya-multilingual-batch-gain.json` 与两份 `laya-multilingual-multiprocess-p*.json`。

采集方法、主机规格与每份数据的已知局限见 [`benchmarks/README.md`](../benchmarks/README.md)。
不要引用任何不在 `benchmarks/results/` 里的数字；见 [`AGENTS.md §8`](../AGENTS.md)。
本页每条结论的理由、以及还没有测的东西，见 [`design-review.md`](design-review.md)。
