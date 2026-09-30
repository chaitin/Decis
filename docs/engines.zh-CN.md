# 引擎

[English](engines.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [配置](configuration.zh-CN.md) · [API](api.zh-CN.md)

引擎是线格式契约之下的决策模型。它接收一个 state 和一个已渲染完成的问题（`PreparedQuestion`），
为每个选项返回一个概率；
服务端把它变成 `Noul`、`Choice` 和 `Score` 答案。引擎永远不写响应，也永远不知道问题的 id，所以每个
引擎返回的形状都可以互相比较，新增一个引擎也不会碰到归一化层。

有一件事引擎**可能**看到：调用方自己的值。[`render.py`](../src/decis/render.py) 为了线格式把 JSON
扁平化成可读文本，而一个按别的渲染方式训练出来的模型可以通过 `WorkItem.raw_state` 和
`PreparedQuestion.raw` 拿到原始值。两个 Jeff 引擎就是这么做的：它们的提示词由调用方对象的
`json.dumps` 决定，而不是由 Laya 和 kev 训练时用的扁平文本决定。这一层留在内部——对外 API 不变，
"一个问题有哪些字段"仍然由 `render.py` 决定。

## 已注册引擎

| 引擎 | 骨干 | 参数量 | 权重 | Extra | 说明 |
|---|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | `laya` | 100+ 种语言；默认引擎 |
| `laya` | ModernBERT-large | 421M | 807 MiB | `laya` | 英文 checkpoint；没有签入的准确率基准 |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | `laya` | 同仓库的 typed-decisions checkpoint |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.69 GiB | `kev` | 只有 prefill，不生成文本；建议用 GPU |
| `jeff-qwen3.5-0.8b` | Qwen3.5-0.8B 全权重微调 + readout head | 0.85B | 1.61 GiB | `jeff` | 只有 prefill；`jeff`、`jeff-qwen`、`jeff-qwen3.5` 是别名 |
| `jeff-gemma4-e2b` | Gemma 4 E2B 全权重微调 + readout head | 4.63B | 8.65 GiB | `jeff` | 只有 prefill；按 26 个选项训练；本仓库最大的镜像 |

两个 Jeff 的参数量都不是 checkpoint 名字里那个数：前者是 852,985,920，后者是
4,628,569,379，都取自 Hub 自己的 `safetensors.parameters`，而不是 `0.8B` / `E2B`。体积取自 Hub
的文件清单，只算引擎真正需要的那些文件。

```bash
uv run decis models     # 本机注册了哪些引擎、每个是否真的能跑
```

## 安装引擎

引擎的依赖在它自己的 extra 里，不放进 `[project.dependencies]`，所以一次 sync 只装你点名的
引擎。裸 `uv sync` 一个都不点名——而且它会**删掉**上一次 sync 装上的引擎依赖，这就是"昨天还
能用的引擎今天没了"的原因（此时 `decis models` 报 `deps missing`，并打出修复它的命令）。

```bash
uv sync --all-extras                # 全部引擎 + 开发工具：本地 checkout 一条命令装齐
uv sync --extra dev --extra laya    # 只装 Laya 家族
uv sync --extra dev --extra kev     # 只装 kev-0.8b
uv sync --extra dev --extra jeff    # 两个 Jeff checkpoint
```

两个 Jeff 引擎需要 **Python 3.12 或更新**；项目本身仍支持 3.11，已发布的镜像跑的是 3.13。
原因是 vendored 的推理代码：它用了 PEP 695 的 `type` 别名（`_jeff_vendor/types.py`），在 3.12
之前**连解析都过不去**，而且这些别名是递归的（`JSONValue` 里含 `list[JSONValue]`），所以没法像
那份拷贝的 import 一样改写成 3.11 能读的形式。因此 `jeff` extra 带 `python_version >= '3.12'`
marker，在 3.11 上什么都不装，`decis models` 报的是 `needs Python 3.12+` 而不是"缺某个模块"
——那个模块在这个 extra 里本来就不会被装上（`docs/design-review.md §2-D31`）。

## 容量

每个引擎上报自己的上限，而它们唯一发布的地方是 `GET /v1/models`（在 `decis` 命名空间下）。
`decis models` 报告的是某个引擎在本机能不能跑，而不是它能吃下多少。

| 引擎 | `max_options` | `max_question_tokens` | `max_sequence_tokens` | `max_state_tokens` |
|---|---|---|---|---|
| `laya*` | 255 | 取自 checkpoint 的 `head_max_len`（兜底 192） | 取自 checkpoint 的 `max_len`（兜底 512） | 无 |
| `kev-0.8b` | 255 | 1024 | 1024 | 384 |
| `jeff-qwen3.5-0.8b` | 254 | 8192 | 8192 | 无 |
| `jeff-gemma4-e2b` | 26 | 8192 | 8192 | 无 |

Laya 的上限来自 checkpoint 自己的 `config`，所以它们对你实际加载的模型是对的，而不是一个常量。
兜底值在权重还没到位时生效。两个 Jeff 的 `max_options` 是各自 `decision_config.json` 里记的训练上限，
加载时会再读一次；两个 readout head 都是 255 行，所以选项**词表**从来不是那个卡住你的上限。
8192 是 vendored 服务代码自己的序列窗口，它对 `state + 一个问题` 整体生效。

超出上限的请求会被拒绝，返回 **422**，消息里给出实测数字和那个上限。Decis 绝不会为了装下过长的
输入而截断它：上游会那么做，于是给出一个自信的错答案，而不是报错。

## 新增一个引擎

工作量是一个模块加一行注册。如果一个引擎需要改 [`render.py`](../src/decis/render.py) 或
[`answers.py`](../src/decis/answers.py)，那就是抽象错了——第二个引擎接进来就是为了通过这个检验。

1. 在 `src/decis/engines/<name>.py` 里实现 `DecisionEngine`：`info()`、`load()`、
   `predict()`、`close()`，外加 `weights()` 和 `measure()`。
2. 在 [`engines/registry.py`](../src/decis/engines/registry.py) 里把它注册为
   `"module:ClassName"`——一条**字符串路径**，这样 import 注册表不会连带 import 引擎的重依赖。
3. 把它的依赖加成 `pyproject.toml` 里的一个 extra。引擎依赖永远不进
   `[project.dependencies]`。
4. 诚实声明它的各项容量，并用真实 tokenizer 实现 `measure()`。
   `max_sequence_tokens` 是 `state + 一个问题` 的预算，不是 `state` 单独的预算。
   如果这个模型的提示词是由调用方的 JSON 而不是扁平文本决定的，就读
   `PreparedQuestion.raw` 和 `WorkItem.raw_state`；不要自己再实现一遍扁平化。
5. 两套测试都要加：一套无权重，覆盖 `measure()` 的算术和引擎的内部形状；一套 `-m weights`，
   覆盖批不变性与超长拒绝。

## dtype 与设备

`DECIS_DEVICE` 选择 `cpu`、`cuda`、`mps`、`xpu` 或 `npu`；不设置时取 `torch` 报告可用的第一个
加速器，顺序 `cuda`、`xpu`、`npu`、`mps`，都没有则 `cpu`（`src/decis/engines/devices.py`）。
要这个选择的是 `kev-0.8b` 和两个 Jeff 引擎；不设置时 Laya 交给自己的 `Agent`，它的顺序是
CUDA、Metal、CPU。
`DECIS_DTYPE` 只为 **`kev-0.8b`** 强制指定精度——读取它的是那些查 `registry.DTYPE_DEFAULTS`
的引擎，而 Laya 由它自己的 `Agent` 决定精度，两个 Jeff 加载器也在内部自己决定。在 Laya 或 Jeff
引擎上设置它没有效果。它的用途是在你自己的硬件上重新测量。不设置时，dtype 由引擎和设备决定：

| 引擎 | cpu | cuda | mps |
|---|---|---|---|
| `laya*` | 由 Laya 自己的 `Agent` 决定 | 同上 | 同上 |
| `kev-0.8b` | `fp32` | `bf16` | `fp32` |
| `jeff-qwen3.5-0.8b` | `fp32` | `bf16` | `bf16` |
| `jeff-gemma4-e2b` | `fp32` | `bf16` | `bf16` |

表里用的是 `DECIS_DTYPE` 的写法。响应里报的是线格式的值：`float32`、`float16` 或
`bfloat16`，引擎加载前则是 `unloaded`——`/v1/models` 与每个答案的 `decis` 命名空间都是如此。

Jeff 那两行是 vendored 加载器自己的规则（`cuda` 和 `mps` 上用 `bfloat16`，其余一律
`float32`），所以 `xpu` 和 `npu` 落到 `fp32` 而不是某个 Decis 默认值。Qwen 那个加载器还会
按上游的做法在整个进程里关掉 cuDNN 的 SDPA 后端；本服务的目标部署就是一个进程一个引擎。

`kev-0.8b` 在 CPU 上用 `bf16` 比 `fp32` **慢 83 倍**。强制指定它只会记一条警告，而不是拒绝
启动。测量见[性能](performance.zh-CN.md#每种引擎每种设备的-dtype)。

## 权重

解析顺序见[配置](configuration.zh-CN.md#引擎选择与权重)。三条与引擎有关的说明：

- **Laya** 的 checkpoint 自带容量，所以 `max_len` 不同的微调模型会自动按正确的预算提供服务。
- **kev** 在适配器的 checkpoint 元数据里用 Hub repo id 引用它的 Qwen3.5 基座，所以另外挂载的
  基座副本不会被采用。`decis download` 把基座放进 Hugging Face 缓存，引擎从那里离线运行。
- **Jeff** 的 checkpoint 是全权重微调，所以 `decis download` 只取那一个目录，旁边没有基座要放。
  每个 checkpoint 都在 `decision_config.json` 里带着自己的答案词表和采样温度，而加载器在
  tokenizer 复现不出那份词表时拒绝启动——如果你自己重新导出微调模型，必须带上自己的
  `readout.safetensors`，并让这两个文件保持一致。`jeff` extra 还会装上 `torchvision`，尽管
  请求路径上一张图都不碰：Qwen 那个 checkpoint 的 processor 是 `Qwen3VLProcessor`，它的配置里
  写着一个 video processor，而 `AutoProcessor` 会急切地构造其中列出的每一个子 processor——
  那个类没有 torchvision 就在 import 时失败，于是加载根本走不到读张量那一步。
