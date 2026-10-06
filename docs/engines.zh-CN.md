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
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.66 GiB | `kev` | 只有 prefill，不生成文本；建议用 GPU |
| `jeff-qwen3.5-0.8b` | Qwen3.5-0.8B 全权重微调 + readout head | 0.85B | 1.61 GiB | `jeff` | 只有 prefill；`jeff`、`jeff-qwen`、`jeff-qwen3.5` 是别名 |
| `jeff-gemma4-e2b` | Gemma 4 E2B 全权重微调 + readout head | 4.63B | 8.65 GiB | `jeff` | 只有 prefill；按 26 个选项训练；本仓库最大的镜像 |

两个 Jeff 的参数量都不是 checkpoint 名字里那个数：前者是 852,985,920，后者是
4,628,569,379，都取自 Hub 自己的 `safetensors.parameters`，而不是 `0.8B` / `E2B`。体积取自 Hub
的文件清单，只算引擎真正需要的那些文件：`kev-0.8b` 是三个适配器文件（45,443,000 B，43.3 MiB）
加上它引用的 Qwen3.5 基座（1.65 GiB），不是那个仓库完整的 62.5 MiB 清单（光 `tokenizer.json`
就 19 MiB，而真正被加载的是基座那份）。`engines/kev.py` 里的 `expected_bytes` 就是这些数字，
`tests/test_engines_kev.py::test_the_declared_size_is_the_sum_of_the_files_a_fetch_writes`
让它永远等于那份文件清单。

```bash
uv run decis engines    # 出厂目录：id、别名、extra、Python floor，以及每个引擎要取什么
uv run decis models     # 本机注册了哪些引擎、每个是否真的能跑
```

两条命令回答不同的问题，而且只有一条跟这台机器有关。`decis engines` 是出厂目录——每台机器上
都一样，装任何 extra 之前就能读——里面有每个 id 要取的仓库、这份构建声明的体积，以及 checkpoint
适配的基座（`kev-0.8b -> Qwen/Qwen3.5-0.8B-Base`）。`decis models` 是本机判词：依赖、磁盘上的
权重、解释器 floor。两者的内容都从 `registry.SPECS` 与各引擎自己的 `weights()` 读出来，所以一个
id 不可能只出现在其中一条里，而它打印的 id 正是 `--engine` 与请求里 `model` 字段接受的那些。

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
`decis models` 报告的是某个引擎在本机能不能跑，而不是它的 token 上限和选项上限。

| 引擎 | `max_options` | `max_question_tokens` | `max_sequence_tokens` | `max_state_tokens` |
|---|---|---|---|---|
| `laya*` | 255 | 取自 checkpoint 的 `head_max_len`（默认 192） | 取自 checkpoint 的 `max_len`（默认 512） | 无 |
| `kev-0.8b` | 255 | 1024 | 1024 | 384 |
| `jeff-qwen3.5-0.8b` | 254 | 8192 | 8192 | 无 |
| `jeff-gemma4-e2b` | 26 | 8192 | 8192 | 无 |

Laya 的上限来自 checkpoint 自己的 `config`，所以它们对你实际加载的模型是对的，而不是一个常量。
这两个后备上限在权重还没到位时生效。两个 Jeff 的 `max_options` 是各自 `decision_config.json` 里记的训练
上限，加载时会再读一次；两个 readout head 都是 255 行，所以一个引擎能收多少选项由它声明的
`max_options` 决定，而不是 head 本身的限制。
8192 是 vendored 服务代码自己的序列窗口，它对 `state + 一个问题` 整体生效。

超出上限的请求会被拒绝，返回 **422**，消息里给出实测数字和那个上限。Decis 绝不会为了装下过长的
输入而截断它：上游会那么做，于是给出一个自信的错答案，而不是报错。

## 新增一个引擎

工作量是一个模块加一行注册。如果一个引擎要改 [`render.py`](../src/decis/render.py) 或
[`answers.py`](../src/decis/answers.py) 才接得进来，说明引擎接口没有容纳它：接一个引擎不该意味着
改归一化层。

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

`DECIS_DEVICE` 怎么解析、哪些引擎会读 `DECIS_DTYPE`，见[配置](configuration.zh-CN.md#计算)；
选错 dtype 的实测代价见[性能](performance.zh-CN.md#每种引擎每种设备的-dtype)。下面只说每个引擎
自己的情况。

要设备解析结果的是 `kev-0.8b` 和两个 Jeff 引擎；不设置时 Laya 交给自己的 `Agent`，它的顺序是
CUDA、Metal、CPU。不设置覆盖时，dtype 由引擎和设备决定：

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

## 权重

解析顺序见[配置](configuration.zh-CN.md#引擎选择与权重)。三条与引擎有关的说明：

- **Laya** 的 checkpoint 自带容量，所以 `max_len` 不同的微调模型会自动按正确的预算提供服务。
- **kev** 需要第二个仓库：适配器训练时用的 Qwen3.5 基座，声明在 `WeightSpec.bases` 里。
  `decis download` 两个都会取；给了 `--dest`（或 `DECIS_MODEL_DIR`）时基座落在适配器旁边的
  `<dir>/Qwen3.5-0.8B-Base/`，一个目录就装下引擎要读的全部内容；两个都没给时它落在应答的那个
  Hub 的缓存里（`decis/hub.py`）。**两个仓库都走这个选择**，而且都以目录的形式交给加载器：
  vendored 的 checkpoint 代码拿到 repo id 时会自己用 `huggingface_hub` 去下，所以把适配器当
  `repo@revision` 传进去就等于绕过 `DECIS_HUB`/`--hub`——反过来，`transformers` 也根本看不到
  ModelScope 下载下来的东西。
- **Jeff** 的 checkpoint 是全权重微调，所以 `decis download` 只取那一个目录，旁边没有基座要放。
  每个 checkpoint 都在 `decision_config.json` 里带着自己的答案词表和采样温度，而加载器在
  tokenizer 复现不出那份词表时拒绝启动——如果你自己重新导出微调模型，必须带上自己的
  `readout.safetensors`，并让这两个文件保持一致。`jeff` extra 还会装上 `torchvision`，尽管
  请求路径上一张图都不碰：Qwen 那个 checkpoint 的 processor 是 `Qwen3VLProcessor`，它的配置里
  写着一个 video processor，而 `AutoProcessor` 会急切地构造其中列出的每一个子 processor——
  那个类没有 torchvision 就在 import 时失败，于是加载根本走不到读张量那一步。
