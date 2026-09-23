# 性能

[English](performance.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [部署](deployment.zh-CN.md) · [设计审查](design-review.md)

Decis 跑的是决策模型——每个请求只有一次前向，不生成文本——所以真正重要的数字是单题延迟、
冷启动时间和内存。本页收集项目引用的全部实测数字。这里没有手写的东西：每张表都由
[`benchmarks/report.py`](../benchmarks/report.py) 从 [`benchmarks/results/`](../benchmarks/results/)
里的原始 JSON 生成，仓库里的表与数据不一致时 CI 会失败。

测量是在**一台 24 vCPU、没有 GPU 的 aarch64 机器**上做的。决策模型在 Apple 芯片上用 MLX 也很快
——上游为那种配置发布的数字要低得多——但只有 CPU 的容器做不到。

## 延迟

<!-- LATENCY:START -->

| 引擎 | 设备 | dtype | 线程 | 1 个问题 | 10 个问题 | 冷启动 | 峰值 RSS |
|---|---|---|---:|---:|---:|---:|---:|
| `laya` | cpu | float32 | 24 | 446 ms | 3,222 ms (322.1 ms/题) | 76.3 s | 2.8 GB |
| `laya-multilingual` | cpu | float32 | 24 | 231 ms | 987 ms (98.7 ms/题) | 72.9 s | 4.84 GB |

由 [`benchmarks/report.py`](../benchmarks/report.py) 从 [`benchmarks/results/`](../benchmarks/results/) 的原始 JSON 生成；**整行取自同一个配置**（torch 在本机的默认线程数，每个 vCPU 一个），样本 p50，单进程，仅请求内批处理。

**这是延迟，不是吞吐。** 每个请求的问题共享同一个 `state`，这是容易的情况。跨请求批处理此后也测过，结论是在 CPU 上**不提升吞吐**（见下面的批处理一节与 [`docs/design-review.md`](design-review.md) §4-M5）。

<!-- LATENCY:END -->

延迟表刻意选的是容易的情况：一个请求里的所有问题共享同一个 `state`。值得从中读出两件事：

- 一次调用问十个问题，每题的代价比单独问一个低得多。
- 冷启动约 75 秒（上表那一列是 73–76 秒），峰值内存是权重文件的数倍。就绪探针和容器内存上限要按
  这张表来定，而不是按下载体积。

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

完整表格——每个批大小、padding 比例、盈亏平衡等待时间，以及多进程对比——见
[`design-review.md §4-M5`](design-review.md)。跨请求批处理只有在序列短到"每次调用的固定开销
占主导"时才划算；真实 state 长度下已经没有开销可摊，而长度一倾斜，padding 会让它慢好几倍。
每个响应里仍然会报 `decis.batch_size`，因为无法观测的机制就无法被检验。

## 复现这些数字

```bash
uv run decis bench --engine laya-multilingual --batch 1,3,10,30   # 采集
uv run python benchmarks/report.py --write                        # 生成
uv run python benchmarks/report.py --check                        # CI 跑这个
```

采集方法、主机规格与每份数据的已知局限见 [`benchmarks/README.md`](../benchmarks/README.md)。
不要引用任何不在 `benchmarks/results/` 里的数字；见 [`AGENTS.md §8`](../AGENTS.md)。
