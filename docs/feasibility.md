# Decis 可行性分析与调查报告

本文回答三个问题：**这条路走得通吗？**、**我原来的假设对吗？**、**最大的坑在哪？**

调查时间：2026-09-22。所有结论都标注了证据来源；未实测的地方明确写"未实测"，不推断。

---

## 1. 结论摘要

**可行，且抽象已经被两个独立实现验证过。** 三条主要判断：

1. **统一 API 的抽象成立。** kev（自回归 LM + LoRA + pointer head）和 Laya（双向编码器 + option marker head）是两种完全不同的架构，但它们**各自独立地收敛到同一个中间表示**：`(state, instructions, [option…]) → 每个 option 一个概率`。kev 的 `api.py` 头注释和 Laya 的 `common.py:QTYPES` 就是同一条设计。这不是巧合，是这类"非生成式决策模型"的必然形状。Decis 的核心抽象因此是被验证过的，而不是猜的。

2. **契约是确定的，不需要猜。** `typesafe-sdk` 0.7.1 的 wheel 里带 `_schemas/models.py`，文件头写明它由 `https://api.typesafe.ai/openapi.json` 生成。这是线格式的权威来源，比文档散文更硬。**"与 jev 一致"因此是一个有明确验收标准的工程问题，不是模糊目标。**

3. **性能预期需要按硬件分层，不能笼统说"20ms"。** 20ms 量级在"M3 Max + MLX + 短输入"下成立（实测 P50 17.75ms）。在无 GPU 的容器里，Laya 官方给的 CPU 数字是 **193–464 ms**。Decis 的 README 必须给分层的、有出处的数字，否则会误导用户。

**主要风险不在抽象，而在部署**：kev 的 Qwen3.5 基座是混合架构（Gated DeltaNet），在 CUDA 上需要 `flash-linear-attention` + `triton`，这是真正的脆弱点。Laya 侧风险小得多。

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

### 2.5 UniTS-Hub 的 Docker 模式

直接可复用（已写进 [`design.md §8`](design.md)）：单 Dockerfile + `ARG MODEL_TYPE`、CI 三段式 `test → build(matrix, push-by-digest) → merge(imagetools create)`、tag 规范 `<image>:<model>-latest/-<ver>/-<sha7>`、权重构建期 `snapshot_download` 烘进 `/app/models` + `TRANSFORMERS_OFFLINE=1`、`MODELS_DIR` 可挂载覆盖。

---

## 3. 逐条对照项目期望

| 你的期望 | 判定 | 依据 |
|---|---|---|
| 1. Python 栈即可 | ✅ | 两个引擎都是纯 Python；无编译型扩展需要自己写 |
| 2. 现代化构建/发布（uv） | ✅ | uv 已是 kev / laya-mlx 的实际选择；PyPI 发布路径成熟 |
| 3. 高性能 API server | ⚠️ **可行但有条件** | 单次推理确实快，但"高 QPS"靠的是**跨请求批处理 + 多进程**，不是换 server 框架。实测：CPU-only 单问 201 ms（多语）/ 446 ms（英文）；同请求内批处理把每问成本降到 99–234 ms。要低延迟必须上 GPU。见 §4 |
| 4. 高质量抽象、易扩展 | ✅ | 抽象已被两个独立实现验证（§1）；新增引擎 = 1 个模块 + 1 行注册 |
| 5. 按模型分别打 Docker + GitHub Actions | ✅ | UniTS-Hub 模式已验证可直接套用 |

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

请求体：1 个 `choice`（4 选项）+ 1 个 `score`（3 档）+ 1 个 `noul`，state 约 40 词的英文工单。Laya 用 `laya.load(...)` 直接调用；kev 用其自带 `kev.serve` 走完整 HTTP 路径。脚本见 `.scratch/probe/`。

> **口径声明**：这些数字来自一次单机测量，**不是** `AGENTS.md §8` 要求的"由 `benchmarks/report.py` 从 checked-in 原始 JSON 生成的正式基准"。它们是可行性判断的依据，不应直接写进 README 当作产品性能。

### 4.2 结果

<!-- MEASUREMENTS:START -->
### 主机

aarch64 · 24 vCPU · 33.5 GB RAM · **无 GPU** · Python 3.12.3 · torch 2.14.0+cpu · transformers 5.17.0 · laya 0.3.5

这台机器代表"最低配的 Docker CPU 容器"，也就是用户在没有 GPU 的服务器上第一次跑 Decis 会遇到的情形。

### 结果 1：Laya 单次请求延迟（p50，线程数 × 单请求问题数）

`laya-multilingual`（322M，`max_len=1024`）· 加载耗时 **72.9 s** · 峰值 RSS **4.84 GB**

| 线程 \ 问题数 | 1 | 3 | 10 | 30 |
|---|---:|---:|---:|---:|
| 1 | 316 ms | 628 ms | 2254 ms | 7391 ms |
| 4 | 299 ms | 544 ms | 1790 ms | 4449 ms |
| 8 | 256 ms | 466 ms | 1327 ms | 3983 ms |
| **16** | **201 ms** | 392 ms | 1115 ms | 3660 ms |
| 24 | 231 ms | 387 ms | **987 ms** | **3426 ms** |

换算成**每个问题**的成本，批处理的收益非常清楚：

| 线程 | 1 问 | 3 问 | 10 问 | 30 问 |
|---|---:|---:|---:|---:|
| 16 | 200.8 ms | 130.7 ms | 111.5 ms | 122.0 ms |
| 24 | 231.4 ms | 129.1 ms | **98.7 ms** | 114.2 ms |

`laya`（英文，421M，`max_len=512`）· 加载耗时 **76.3 s** · 峰值 RSS **2.80 GB**

| 线程 \ 问题数 | 1 | 3 | 10 | 30 |
|---|---:|---:|---:|---:|
| 8 | 527 ms | 1041 ms | 3507 ms | 8370 ms |
| 16 | 432 ms | 919 ms | 3082 ms | 7042 ms |
| 24 | 446 ms | 822 ms | 3222 ms | 7032 ms |

### 结果 2：kev-0.8b dtype 对照（同一台机器，同一请求）

请求含 3 个问题（choice 3 选项 + noul + score 3 档），state 约 20 词。

| dtype | 服务端 `latency_ms` | 相对倍率 | 答案差异 |
|---|---:|---:|---|
| **fp32** | **1656 ms** | 1× | 基准 |
| bf16 | **137174 ms** | **83× 慢** | 概率差 ≤0.01，argmax 相同 |

日志同时给出原因：

```
[transformers] `chunk_gated_delta_rule` is falling back to its reference PyTorch
implementation because `flash-linear-attention` is not installed. This is correct
but much slower; install `flash-linear-attention` for the optimized kernel.
[transformers] `causal_conv1d_fn` is falling back to its reference PyTorch
implementation because `causal_conv1d` is not installed. ...
```

`flash-linear-attention` / `causal_conv1d` 都是依赖 Triton/CUDA 内核的包，**在 CPU 上装不了**。所以这不是"忘了装依赖"，而是 kev 的 Qwen3.5 基座在 CPU 上的结构性劣势。

### 结果 3：原始数据

逐样本原始 JSON（含每次调用的完整 `samples_ms`、输入 sha256、token 数、p50/p95）已整理进仓库：

- [`benchmarks/results/laya-multilingual-sweep.json`](../benchmarks/results/laya-multilingual-sweep.json)
- [`benchmarks/results/laya-english-sweep.json`](../benchmarks/results/laya-english-sweep.json)
- [`benchmarks/results/kev-0.8b-cpu-dtype.json`](../benchmarks/results/kev-0.8b-cpu-dtype.json)

采集方法、主机规格、复现命令与已知局限见 [`benchmarks/README.md`](../benchmarks/README.md)；采集脚本在 [`benchmarks/probe/`](../benchmarks/probe/)。

> **口径声明（更新）**：这些是**实现 Decis 之前**对**引擎本身**的单机测量，不是 Decis 服务的性能。它们满足"数字必须有 checked-in 原始数据支撑"这条纪律，但按 `benchmarks/README.md` 的说明，正式基准要在 `src/decis/` 存在后由 `decis bench` 重新采集。

<!-- MEASUREMENTS:END -->

### 4.4 解读

实测把几个原本模糊的问题变成了确定的设计输入。

**① 批处理确实有效，但收益在高并发场景才能兑现。** 单请求内多问题已经把每问成本从 201 ms 压到 99–112 ms（约 2×）。注意这是**同一个请求内**的批处理 —— 上游 `system_one` 已经能做的部分。真正未兑现的是**跨请求**批处理（`design.md §6`），那才是高 QPS 的来源。这也说明"每个请求 1 个问题"的调用模式下，单进程上限只有约 4–5 req/s。

**② 加载时间 73–76 秒，这是容器编排的硬约束。** 远超默认的 readiness 探测窗口。设计含义：

- `HEALTHCHECK --start-period` 必须 ≥120 s
- `/healthz`（进程活着）与 `/readyz`（权重就绪）必须分开，否则编排器会在加载期间反复杀 Pod
- 权重必须烘进镜像或预挂载到本地盘；冷启动现场下载会在此之上再叠加下载时间
- 上游 `laya` 的 `Agent.__init__` 是同步阻塞的，加载期间不能接受流量 → 必须在 `serve` 启动序列里先加载、后监听

**③ 内存必须按 3–5 GB/进程规划，而不是按权重体积。** 多语峰值 RSS 4.84 GB、英文 2.80 GB，而权重本身只有 644 MB / 804 MB。差额来自 PyTorch 运行时 + 长序列（`max_len=1024`）× 大批次的激活。`design.md §6.4` 的 `N_engine` 相乘时要乘这个数，不是乘权重大小。

**④ "bf16 更省内存所以更快"是错的，而且错得很贵。** kev-0.8b 在 CPU 上 bf16 比 fp32 **慢 83 倍**（137 s vs 1.66 s），概率几乎相同。这条已经写进设计：`registry.py` 必须按「引擎 × 设备」给 dtype 默认值，并对已知劣化组合告警（`design.md §5.2`）。

**⑤ kev 在 CPU 上不适合"轻量高频"这个定位。** 1.66 s/请求（fp32，3 问题）比 Laya 慢 4–8 倍。原因是架构性的（混合 DeltaNet 基座 + 缺少 CPU 内核），不是配置问题。设计含义：

- kev 应以 **CUDA 为主场**，CPU 镜像标为"功能可用、性能不达标"
- 若必须 CPU 部署，考虑上代 Qwen3 基座的 kev（`jaredpalmer/kev-0.6b` / `kev-4b@qwen3`）—— 纯 attention，没有 DeltaNet 参考实现的问题，但需要实测确认
- 这正是 `design.md §13-Q5` 需要 GPU 机器验证的事项

**⑥ 线程数不是越多越好。** 1 问场景 16 线程（201 ms）优于 24 线程（231 ms）；大 batch 下 24 线程才占优。默认值应取 8–16 而不是 `os.cpu_count()`，并且必须可配（`DECIS_TORCH_THREADS`）。

**⑦ 默认引擎建议 multilingual。** 在同一台机器上比英文版**快 2–4 倍**（987 ms vs 3222 ms @ 10 问），且覆盖 100+ 语言。用户原始设想里"laya 和 kev"的默认选择应该是 `laya-multilingual`，英文版只在明确需要英文最优精度时使用。

**⑧ 与用户基准的关系。** 用户提到的 20 ms 级延迟对应的是 **M3 Max + MLX + 短输入**（laya-mlx checked-in 结果：EN P50 17.75 ms / 多语 10.91 ms）。本次 CPU 实测是 200–450 ms，与 Laya 官方给出的 CPU 数字（193–464 ms）一致，说明测量可信。**两者不矛盾，是不同硬件与不同运行时**。README 必须分层给数字，否则用户会以为 Decis 慢了两个数量级。

---

## 5. 风险登记册

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | **kev 的 Qwen3.5 混合基座缺少 CPU/CUDA 优化内核**（`flash-linear-attention` / `causal_conv1d`），回落到参考实现 | **已发生（CPU）** | 高 | CPU 实测 1.66 s/请求（fp32）；**bf16 在 CPU 上慢 83 倍**。缓解：按「引擎×设备」设 dtype 默认值 + 劣化组合告警（`design.md §5.2`）；镜像构建期做真跑 smoke test；pin 全部版本；kev 定位为 GPU 引擎；提供 Qwen3 世代 kev 作回退（待实测） |
| R2 | Laya 在 **>20 选项**的 choice 上准确率断崖式下降 | 高 | 中 | `EngineInfo.max_options` 诚实声明并在文档中明说；为高基数场景保留 `predict_shortlist` 扩展路径；不与 jev 的 255 选项能力做等价宣称 |
| R3 | **CPU 上的延迟达不到用户预期**（用户基准是 M3 Pro 20 ms） | **已发生** | 中（预期管理） | 实测 200–450 ms/请求（单问），与 Laya 官方 CPU 数字一致。缓解：文档分层给数字；`decis bench` 让用户自测；提供 CUDA 镜像；README 首屏就说明"20 ms 属于 M3 Max+MLX，CPU 是百毫秒级" |
| R4 | `laya` 包内部 API（`build_sequence`/`collate_items` 未被 `__all__` 导出）在 0.4 变更 | 中 | 中 | pin `<0.4`；加"上游契约测试"断言存在与签名；最坏情况退回 `system_one`（失去跨请求批处理但仍可服务） |
| R5 | **vendor kev 的推理内核**造成长期维护负担 | 中 | 中 | 只 vendor 最小子集（不含训练/评测）；pin commit；在 `NOTICE` 记录；上游若发布 serve-only extra 或 PyPI 包则切回依赖 |
| R6 | 各引擎的 `confidence` 语义不同导致阈值不可移植 | 高（若不处理） | 高（统一 API 的价值受损） | 已在设计中收敛为**服务端统一定义**（`design.md §4.2`），引擎原生置信度放入扩展字段 |
| R7 | 契约细节做错（尤其 `score` 字符串键、`noul` 无 confidence） | 中 | 高（SDK 直接解码失败） | 契约来自 OpenAPI 生成物而非推测；L1–L3 契约测试进 CI；官方 SDK 作为验收 |
| R8 | **冷启动 73–76 s**（实测）导致编排器在加载期反复重启 Pod | **已发生** | 高 | `HEALTHCHECK --start-period` ≥120 s；`/healthz` 与 `/readyz` 分离；权重烘进镜像避免叠加下载时间；文档给出 readiness 配置示例 |
| R9 | **内存占用被低估**：峰值 RSS 4.84 GB（多语）/ 2.80 GB（英文），远高于权重 644/804 MB | **已发生** | 中 | 文档按实测 RSS 给容器内存建议；`N_engine × RSS`；提供 `decis doctor` 报告真实占用 |
| R10 | 免费 HF 下载在 CI 中不稳定/限流 | 中 | 中（构建失败） | 构建期设 `HF_TOKEN`；缓存 HF 目录；digest artifact 保留更久以便重试 merge |
| R11 | 模型许可证 | 低 | 高 | Laya 与 kev 均为 Apache-2.0，已确认；`NOTICE` 保留署名；不复制 laya-mlx 代码 |

---

## 6. 对你原始假设的校正

| 你的假设 | 实际情况 | 影响 |
|---|---|---|
| "laya 和 kev 模型很小，可以内置到 docker 中" | Laya 确实小（614–804 MiB）。**kev 只有 0.8B 算小**（adapter 113MB + 基座约 1.6GB）；4B/9B 分别是 ~8GB / ~18GB | 默认镜像只做 `kev-0.8b`；大模型做成可选镜像 |
| "laya 是 bert 变种" | ✅ 正确。ModernBERT-large / mmBERT-base + 自定义决策头 | 无 |
| "kev 是 transformers 的变种模型" | 更准确说：**Qwen3.5 因果 LM + LoRA + pointer head**，prefill-only 不做生成 | 决定了它不是"换个分类头"，而是需要 transformer 全量前向 |
| "我本地测每次推理 20ms" | 与 laya-mlx 在 **M3 Max + MLX** 上的 checked-in 结果（EN P50 17.75ms / 多语 10.91ms）一致。但**不能外推到 CPU 或小机器**：本次 CPU 实测 201–446 ms/单问请求 | 预期管理 + 必须提供 GPU 镜像路径 |
| "kev 可以内置进 docker 快速体验" | 技术可行（adapter 113MB + 基座 1.6GB），但 **CPU 上 fp32 就要 1.66 s/请求**，bf16 更是 137 s | kev 应以 CUDA 为主场；CPU 镜像明确标注性能不达标 |
| "仅一套 api server，稳定接口与 jev 一致" | ✅ 完全可行，且契约有 OpenAPI 生成物可依 | 无 |
| "方便后续增加更多推理方式" | ✅ 抽象已被两种异构架构验证 | 无 |
| "用 GitHub Actions 按模型分别打镜像" | ✅ UniTS-Hub 模式直接可用 | 无 |

**需要认真调整的是两条**：（1）高性能来自**跨请求批处理 + 硬件**，不是换框架；（2）kev 的"轻量"只在 GPU 上成立，CPU 上它比 Laya 慢一个数量级。把这两点写清楚，比承诺一个达不到的数字重要。

---

## 7. 下一步需要验证的事

按优先级。标注 ✅ 的已在本次调查中完成。

1. ✅ **单请求内的批处理收益曲线**：已测（§4.1）。每问成本 1 问 → 10 问降约 2×。
2. ✅ **CPU 线程数**：已测（§4.1）。1 问场景 16 线程优于 24 线程；默认取 8–16。
3. ⬜ **跨请求批处理的额外收益**（Stage 3）：本次只测了单请求内的批处理；`design.md §6` 的断言（跨请求攒批是 QPS 的主要来源）**尚未验证**，这是 Stage 3 的第一件事。
4. ⬜ **多进程 × 线程数的最优点**（Stage 3）：本次是单进程。`N_engine × DECIS_TORCH_THREADS` 的组合需要实测，且要按 §4.4-③ 的 3–5 GB/进程核算内存。
5. ⬜ **CUDA 可行性**（需 GPU 机器）：`flash-linear-attention` + `causal_conv1d` + triton 在目标镜像里能否装上并跑通 kev-0.8b，以及 kev 在 GPU 上的真实延迟。这是 R1 的另一半（本次只验证了 CPU 侧）。
6. ⬜ **Qwen3 世代 kev 在 CPU 上的表现**：`jaredpalmer/kev-0.6b`（纯 attention）是否能在 CPU 上达到可用延迟。如果是，它可能是 CPU 镜像里比 kev-0.8b 更合适的选择。
7. ⬜ **跨请求批处理在 kev 上的两条路径**（`rows` vs `prefix`）的权衡。
8. ⬜ **差分测试**：有 `TYPESAFE_API_KEY` 时，同一请求打真 jev 与 Decis，比对 JSON schema。

---

## 8. 参考来源

**第一方（TypeSafe）**
- [Jev 介绍](https://docs.typesafe.ai/introduction)、[API 参考](https://docs.typesafe.ai/api)、[Choice 原语](https://docs.typesafe.ai/primitives/choice)、[Confidence](https://docs.typesafe.ai/confidence)
- `typesafe-sdk` 0.7.1 wheel：`typesafe_sdk/_schemas/models.py`（由 `api.typesafe.ai/openapi.json` 生成）、`_core/transport.py`、`_core/constants.py`、`_core/response_types.py`

**开源模型**
- [kev](https://github.com/jaredpalmer/kev)（本地 `/data/src/github.com/jaredpalmer/kev`）：`README.md`、`AGENTS.md`、`kev/api.py`、`kev/serve.py`、`kev/model.py`、`kev/checkpoint.py`、`tests/test_api.py`
- [Laya 模型卡](https://huggingface.co/convaiinnovations/laya)、[PyPI laya](https://pypi.org/project/laya/)、[laya GitHub](https://github.com/NandhaKishorM/laya)
- 本地安装的 `laya` 0.3.5：`laya/agent.py`、`laya/common.py`
- [laya-mlx](https://github.com/mizorewww/laya-mlx)（本地 `/data/src/github.com/mizorewww/laya-mlx`）：`pyproject.toml`、`laya_mlx/agent.py`、`tests/conftest.py`、`benchmarks/report.py`、`BENCHMARKS.md`、`docs/*_RESEARCH.md`

**工程模式**
- [UniTS-Hub](https://github.com/kingfs/UniTS-Hub)：`Dockerfile`、`.github/workflows/docker-build.yml`、`docker-compose.yml`、`scripts/download_models.py`
