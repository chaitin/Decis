# Decis 可行性分析与调查报告

## 1. 这份调查的定位

这是**实现 Decis 之前**的调查报告，调查时间 2026-09-22，回答三个问题：这条路走得通吗、当时的假设对不对、
最大的坑在哪。**它现在只保留证据本身**：§2 的上游调查、§3 的逐条对照、§4 的实测数据与复现路径、
§5 的风险登记册。

从这些证据推出的结论已经分别写进 [`design.md`](design.md)（§12.1 按"能对外承诺什么"排的三档账）、
[`design-review.md`](design-review.md)（缺陷与方法论问题）与 [`performance.md`](performance.md)
（由 checked-in JSON 生成的表）。在这里再复述一遍就是第二处记录（`AGENTS.md §2`）。

---

## 2. 调查结果

### 2.1 契约（详见 [api-compatibility.md](api-compatibility.md)）

已在 `typesafe_sdk` 0.7.1 的生成代码与传输层源码中确认：

- `POST /v1/systemone`，`Authorization: Bearer <key>`，`state` + `model` + `questions`
- 响应 `{model, answers, usage}`；`noul` 是标量无 confidence；`choice` 概率键=选项名；**`score` 的 `legend`/`probabilities` 线格式键是字符串 `"0"/"1"/…`**
- 官方 SDK 对 POST 自动重试 `{408,429,500..599}`，读 `x-typesafe-request-id`

**最关键的一条**：契约是由 OpenAPI 生成物定义的，所以 Decis 可以逐字段对齐并有据可依。

### 2.2 kev（jaredpalmer/kev）

| 项 | 结论 | 来源 |
|---|---|---|
| 技术栈 | Python / PyTorch + transformers 5.17 + peft + FastAPI | `pyproject.toml` |
| 架构 | Qwen3.5 基座（0.8B / 4B / 9B）+ LoRA r=16 + pointer head | `README.md`、`kev/model.py` |
| 推理 | **prefill-only，不生成文本**。每问一条 branch，共享 state | `kev/model.py:32-69` |
| 服务 | 自带 TypeSafe 兼容 `POST /v1/systemone` | `kev/serve.py` |
| 契约偏差 | `/v1/models` 形状不符；`model` 有默认值；score 上限 255（文档称 10）；响应多 `latency_ms`；`HTTPException(422, str)` 错误体与 SDK 生成模型不符 | 见 [api-compatibility.md §7](api-compatibility.md) |
| 并发 | **单请求串行**（一个 `threading.Lock`），无跨请求批处理 | `kev/serve.py:33,45` |
| Docker | **无 Dockerfile，无镜像 CI**，无 ONNX/量化导出 | 全仓 grep |
| 权重 | adapter 小（0.8b 仓库 ≈113MB），基座大（0.8B bf16 ≈1.6GB） | HF API |

**注意一个认知校正**：kev 不是"很小的 transformers 变种"。它是 **0.8B–9B** 的因果 LM 加 adapter。"轻量"只对 0.8B 成立；4B/9B 已经超出这个定位（4B bf16 服务需约 9GB 显存）。

**它对本项目最重要的价值**：`kev/AGENTS.md` 自己就把 `model.py` / `api.py` / `checkpoint.py` 三件套 vendor 进 HF Space，说明这套文件本就是为嵌入而组织的 —— Decis vendor 它的推理内核是顺应其设计意图，不是硬拆。

### 2.3 Laya（convaiinnovations/laya）

| 项 | 结论 | 来源 |
|---|---|---|
| 架构 | ModernBERT-large (421M) / mmBERT-base (322M) + 决策头（2 层 transformer + option-marker scorer + act/escalate head） | HF model card |
| 形态 | **非自回归、单次前向**，`[CLS] <type> instructions [SEP] [MASK] opt0 [MASK] opt1 … [SEP] state [SEP]` | `laya/common.py:build_sequence` |
| 官方 SDK | **PyPI `laya` 0.3.5，Apache-2.0，wheel 仅 42KB**；依赖 `torch/transformers/safetensors/huggingface_hub/numpy` | PyPI |
| 入口 | `laya.load(...)` → `Agent.system_one(state, questions)`，**返回值已接近 jev 形状** | `laya/agent.py:266-368` |
| 三个 checkpoint | `laya`(en,512) / `laya-multilingual`(100+ 语言,1024) / `laya-typed-decisions` | HF |
| 速度 | T4: 1 问 39.5ms，10 问 158.6ms(15.9ms/问)，50 问 771ms → 103–332 q/s | HF model card |
| CPU | 官方称 **193–464 ms/请求**（preload 模式）；**本次实测 201–446 ms/单问请求**，量级一致 | PyPI 页面 + §4 |
| 限制 | `>20` 选项时准确率显著下降（选项共享固定 token 预算，77 选项每个只有 3–4 token） | HF model card |
| 校准 | 出厂即过度自信（ECE 0.466），需按 (question type, option count) 重新拟合温度；包内已对温度做 `[0.5,5.0]` clamp 防"假置信度" | `laya/common.py:219-240` |

**对 Decis 极其重要的发现**：`laya/common.py:collate_items` 的实现是

```python
items = [it for group in batch for it in group]  # 展平多组问题
```

即 **Laya 的官方原语天然支持"把多个请求的问题组展平成一个 batch 做一次前向"**。上游 `system_one` 只传了 `[items]`（一组），但底下的 `collate_items` + `model(...)` 可以直接喂多组。这意味着 Decis 的**跨请求批处理在 Laya 上是复用官方原语，不是 hack**，实现量约 50 行。

### 2.4 laya-mlx（mizorewww/laya-mlx）

一个 macOS/MLX 的第三方移植。调查结论：

- **只在 macOS arm64 上能跑**（顶层 `import mlx.core`，`Agent` 只接受 `gpu/metal/cpu`，CI 只在 `macos-26`）。它**没有** torch/CUDA 后端；仓库里那个 `laya-multilingual-torch-mps-float32.json` 是"从 `.upstream` 克隆的上游 PyTorch 实现"的对照数据，不是本包能力。
- **Linux/Docker 路线必须走官方 `laya` PyPI 包**，不是这个。
- 但它的**工程做法值得抄**，而且已经写进 Decis 的设计：
  - `tests/conftest.py` 的 **tiny random checkpoint fixture**（现场造 3 层/64 维小模型 + 临时 tokenizer，测试不下载权重）—— 这是 Decis CI 能做"引擎也有测试"的关键
  - `benchmarks/report.py` 的**报告从原始 JSON 生成 + 断言输入哈希一致**—— 已写进 `AGENTS.md §8`
  - `strict=True` 加载（缺键/错形状立即报错）、温度 clamp、归一化熵置信度
- 它的 README 头条延迟（13.42/7.39ms）与仓库内 checked-in 结果（17.75/10.91ms）**不一致**。规划按保守值。

---

## 3. 逐条对照项目期望

| 你的期望 | 判定 | 依据 |
|---|---|---|
| 1. Python 栈即可 | ✅ | 两个引擎都是纯 Python；无编译型扩展需要自己写 |
| 2. 现代化构建/发布（uv） | ✅ | uv 已是 kev / laya-mlx 的实际选择；PyPI 发布路径成熟 |
| 3. 高性能 API server | ⚠️ **可行，但 CPU 上没有高吞吐手段** | 单次推理确实快，但"高 QPS"**不成立**：跨请求批处理实测为负收益（`design-review.md` §4-M5），加进程也不增加吞吐。实测：CPU-only 单问 201 ms（多语）/ 446 ms（英文）；同请求内批处理把每问成本降到 99–234 ms。要低延迟必须上 GPU，而 GPU 侧没有数据。见 §4 |
| 4. 高质量抽象、易扩展 | ✅ | 抽象已被两个独立实现验证（§1）；新增引擎 = 1 个模块 + 1 行注册 |
| 5. 按模型分别打 Docker + GitHub Actions | ✅ | 已验证可直接套用 |

---

## 4. 实测

### 4.1 方法与环境

在一台**无 GPU** 的 Linux 主机上实测，模拟"最差情况下的 Docker CPU 容器"：

| 项 | 值 |
|---|---|
| CPU | 24 vCPU |
| 内存 | 31 GiB |
| GPU | 无 |
| Python | 3.12.3 |
| torch | 2.14.0+cpu |
| transformers | 5.17.0 |
| laya | 0.3.5 |

请求体：1 个 `choice`（4 选项）+ 1 个 `score`（3 档）+ 1 个 `noul`，state 约 40 词的英文工单。Laya 用 `laya.load(...)` 直接调用；kev 用其自带 `kev.serve` 走完整 HTTP 路径。脚本见 [`benchmarks/probe/`](../benchmarks/probe/)。

> **口径声明**：这些数字来自一次单机测量，**不是** `AGENTS.md §8` 要求的"由 `benchmarks/report.py` 从 checked-in 原始 JSON 生成的正式基准"。它们是可行性判断的依据，不应直接写进 README 当作产品性能。

### 4.2 结果

<!-- MEASUREMENTS:START -->
### 主机

- aarch64 · 24 vCPU · 33.5 GB RAM · **no GPU** · torch 2.14.0+cpu · laya 0.3.5
- aarch64 · 24 vCPU · 33.5 GB RAM · **no GPU** · torch 2.14.0+cpu · transformers 5.17.0

这台机器代表“最低配的 Docker CPU 容器”，也就是用户在没有 GPU 的服务器上第一次跑 Decis 会遇到的情形。

> 本节的每个数字都由 [`benchmarks/report.py`](../benchmarks/report.py) 从
> [`benchmarks/results/`](../benchmarks/results/) 的原始 JSON 生成，**没有手写数字**。
> 生成前脚本会断言同一组内各配置处理的是**同一个输入**（`input_sha256` 与 token 数一致），
> 不一致就拒绝生成。改动本节请跑 `python benchmarks/report.py --write`。

### 结果 1：laya 延迟（p50）

device `cpu` · dtype `float32` · cold start 76.3 s · peak RSS 2.8 GB · processes 1, within-request batching

| 线程 \ 问题数 | 1 | 3 | 10 | 30 |
|---|---:|---:|---:|---:|
| 1 | 872 ms | 1,776 ms | 5,871 ms | 13,636 ms |
| 2 | 744 ms | 1,487 ms | 4,649 ms | 11,340 ms |
| 4 | 615 ms | 1,219 ms | 3,999 ms | 9,425 ms |
| 8 | 527 ms | 1,041 ms | 3,507 ms | 8,370 ms |
| 16 | 432 ms | 918 ms | 3,082 ms | 7,042 ms |
| 24 | 446 ms | 822 ms | 3,222 ms | 7,032 ms |

换算成**每个问题**的成本（同一批数据，同一配置）：

| 线程 | 1 问 | 3 问 | 10 问 | 30 问 |
|---|---:|---:|---:|---:|
| 1 | 871.6 ms | 592.2 ms | 587.1 ms | 454.5 ms |
| 2 | 744.2 ms | 495.6 ms | 464.9 ms | 378.0 ms |
| 4 | 615.0 ms | 406.4 ms | 399.9 ms | 314.2 ms |
| 8 | 527.1 ms | 347.1 ms | 350.7 ms | 279.0 ms |
| 16 | 432.3 ms | 306.1 ms | 308.2 ms | 234.8 ms |
| 24 | 445.8 ms | 274.0 ms | 322.1 ms | 234.4 ms |

### 结果 2：laya-multilingual 延迟（p50）

device `cpu` · dtype `float32` · cold start 72.9 s · peak RSS 4.84 GB · processes 1, within-request batching

| 线程 \ 问题数 | 1 | 3 | 10 | 30 |
|---|---:|---:|---:|---:|
| 1 | 316 ms | 628 ms | 2,254 ms | 7,391 ms |
| 2 | 354 ms | 671 ms | 2,139 ms | 5,661 ms |
| 4 | 299 ms | 544 ms | 1,790 ms | 4,449 ms |
| 8 | 256 ms | 466 ms | 1,327 ms | 3,983 ms |
| 16 | 201 ms | 392 ms | 1,115 ms | 3,660 ms |
| 24 | 231 ms | 387 ms | 987 ms | 3,426 ms |

换算成**每个问题**的成本（同一批数据，同一配置）：

| 线程 | 1 问 | 3 问 | 10 问 | 30 问 |
|---|---:|---:|---:|---:|
| 1 | 315.9 ms | 209.2 ms | 225.3 ms | 246.3 ms |
| 2 | 354.5 ms | 223.8 ms | 213.9 ms | 188.7 ms |
| 4 | 299.0 ms | 181.2 ms | 179.0 ms | 148.3 ms |
| 8 | 255.7 ms | 155.2 ms | 132.7 ms | 132.8 ms |
| 16 | 200.8 ms | 130.7 ms | 111.5 ms | 122.0 ms |
| 24 | 231.4 ms | 129.1 ms | 98.7 ms | 114.2 ms |

### 结果 3：kev-0.8b 延迟（p50）

device `cpu` · dtype `fp32 / bf16` · processes 1, within-request batching

| dtype | p50 | 相对倍率 | 每问成本 | 答案是否相同 |
|---|---:|---:|---:|---|
| `fp32` | 1,656 ms | 1× | 552 ms | 基准 |
| `bf16` | 137,174 ms | 83× | 45,725 ms | argmax 相同，概率差 ≤0.01 |

线程数 16（记录于原始文件），单进程，within-request batching。**每个 dtype 只有一次观测**，所以这是比值而非统计量（`benchmarks/README.md` M3）。

> ⚠️ 该文件**没有记录输入哈希**，因此无法验证两次配置处理的是同一个输入，只能比对 token 数。

### 原始数据

逐样本原始 JSON（含每次调用的完整 `samples_ms`、`input_sha256`、token 数、p50/p95）都已 checked in：

- [`benchmarks/results/kev-0.8b-cpu-dtype.json`](../benchmarks/results/kev-0.8b-cpu-dtype.json)
- [`benchmarks/results/laya-english-sweep.json`](../benchmarks/results/laya-english-sweep.json)
- [`benchmarks/results/laya-multilingual-sweep.json`](../benchmarks/results/laya-multilingual-sweep.json)

采集方法、主机规格、复现命令与已知局限见 [`benchmarks/README.md`](../benchmarks/README.md)；
采集脚本在 [`benchmarks/probe/`](../benchmarks/probe/) 与 [`benchmarks/run.py`](../benchmarks/run.py)。

**无法验证的部分**（脚本报告的出处缺口，不是隐瞒）：

- kev-0.8b-cpu-dtype.json (3 question(s)): no input hash recorded, so identical input could only be checked via token counts

> **口径声明**：`laya-*-sweep.json` 是**实现 Decis 之前**对**引擎本身**的单机测量，
> 不是 Decis 服务的性能；`kev-0.8b-cpu-dtype.json` 每个 dtype 只有**一次观测**，
> 记录的是比值的量级而非统计量。正式基准要由 `benchmarks/run.py` 重新采集。

<!-- MEASUREMENTS:END -->

### 4.4 线程数

实测（§4.2 的两张表）：1 问场景 **16 线程（201 ms）快于 24 线程（231 ms）**；大 batch 下 24 线程才占优。
所以默认线程数取 8–16 而不是 `os.cpu_count()`，并且必须可配（`DECIS_TORCH_THREADS`）。

其余的解读——跨请求批处理的收益、kev 在 CPU 上的定位、内存规划、20 ms 基准属于哪种硬件、
默认引擎选谁——已经分别在 `design-review.md`（§4-M5、§4-M6）、`design.md`（§12.1）与
`performance.md` 里，这里不再复述。

---

## 5. 风险登记册

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | **kev 的 Qwen3.5 混合基座缺少 CPU/CUDA 优化内核**（`flash-linear-attention` / `causal_conv1d`），回落到参考实现 | **已发生（CPU）** | 高 | CPU 实测 1.66 s/请求（fp32）；**bf16 在 CPU 上慢 83 倍**。缓解：按「引擎×设备」设 dtype 默认值 + 劣化组合告警（`design.md §5.2`）；镜像构建期做真跑 smoke test；pin 全部版本；kev 定位为 GPU 引擎；提供 Qwen3 世代 kev 作回退（待实测） |
| R2 | Laya 在 **>20 选项**的 choice 上准确率断崖式下降 | 高 | 中 | `EngineInfo.max_options` 诚实声明并在文档中明说；为高基数场景保留 `predict_shortlist` 扩展路径；不与 jev 的 255 选项能力做等价宣称 |
| R3 | **CPU 上的延迟达不到用户预期**（用户基准是 M3 Pro 20 ms） | **已发生** | 中（预期管理） | 实测 200–450 ms/请求（单问），与 Laya 官方 CPU 数字一致。缓解：文档分层给数字；`decis bench` 让用户自测；提供 CUDA 镜像；README 首屏就说明"20 ms 属于 M3 Max+MLX，CPU 是百毫秒级" |
| R4 | `laya` 包内部 API（`build_sequence`/`collate_items` 未被 `__all__` 导出）在 0.4 变更 | 中 | 中 | pin `<0.4`；加"上游契约测试"断言存在与签名；最坏情况退回 `system_one`（失去把多个问题组展平进一次前向的能力但仍可服务；跨请求攒批本来就没有实现，见 `design-review.md` §4-M5） |
| R5 | **vendor kev 的推理内核**造成长期维护负担 | 中 | 中 | 只 vendor 最小子集（不含训练/评测）；pin commit；在 `NOTICE` 记录；上游若发布 serve-only extra 或 PyPI 包则切回依赖 |
| R6 | 各引擎的 `confidence` 语义不同导致阈值不可移植 | 高（若不处理） | 高（统一 API 的价值受损） | 已在设计中收敛为**服务端统一定义**（`design.md §4.2`），引擎原生置信度放入扩展字段 |
| R7 | 契约细节做错（尤其 `score` 字符串键、`noul` 无 confidence） | 中 | 高（SDK 直接解码失败） | 契约来自 OpenAPI 生成物而非推测；L1–L3 契约测试进 CI；官方 SDK 作为验收 |
| R8 | **冷启动 73–76 s**（实测）导致编排器在加载期反复重启 Pod | **已发生** | 高 | `HEALTHCHECK --start-period` 取 **180 s**（`docker/Dockerfile`）；`/healthz` 与 `/readyz` 分离（加载期间 `/healthz` 仍然 200，`/readyz` 报 `loading`）；权重烘进镜像避免叠加下载时间；文档给出 readiness 配置示例 |
| R9 | **内存占用被低估**：峰值 RSS 4.84 GB（多语）/ 2.80 GB（英文），远高于权重 644/804 MB | **已发生** | 中 | 文档按实测 RSS 给容器内存建议；`N_engine × RSS`；提供 `decis doctor` 报告真实占用 |
| R10 | 免费 HF 下载在 CI 中不稳定/限流 | 中 | 中（构建失败） | 构建期设 `HF_TOKEN`；缓存 HF 目录；digest artifact 保留更久以便重试 merge |
| R11 | 模型许可证 | 低 | 高 | Laya 与 kev 均为 Apache-2.0，已确认；`NOTICE` 保留署名；不复制 laya-mlx 代码 |

---

## 6. 参考来源

**第一方（TypeSafe）**
- [Jev 介绍](https://docs.typesafe.ai/introduction)、[API 参考](https://docs.typesafe.ai/api)、[Choice 原语](https://docs.typesafe.ai/primitives/choice)、[Confidence](https://docs.typesafe.ai/confidence)
- `typesafe-sdk` 0.7.1 wheel：`typesafe_sdk/_schemas/models.py`（由 `api.typesafe.ai/openapi.json` 生成）、`_core/transport.py`、`_core/constants.py`、`_core/response_types.py`

**开源模型**
- [kev](https://github.com/jaredpalmer/kev)（本地 `/data/src/github.com/jaredpalmer/kev`）：`README.md`、`AGENTS.md`、`kev/api.py`、`kev/serve.py`、`kev/model.py`、`kev/checkpoint.py`、`tests/test_api.py`
- [Laya 模型卡](https://huggingface.co/convaiinnovations/laya)、[PyPI laya](https://pypi.org/project/laya/)、[laya GitHub](https://github.com/NandhaKishorM/laya)
- 本地安装的 `laya` 0.3.5：`laya/agent.py`、`laya/common.py`
- [laya-mlx](https://github.com/mizorewww/laya-mlx)（本地 `/data/src/github.com/mizorewww/laya-mlx`）：`pyproject.toml`、`laya_mlx/agent.py`、`tests/conftest.py`、`benchmarks/report.py`、`BENCHMARKS.md`、`docs/*_RESEARCH.md`
