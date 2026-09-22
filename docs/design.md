# Decis 设计方案

> Decis —— One API to run all light-weight decision models.

本文是 Decis 的架构与实现方案。可行性证据、实测数据与风险见 [feasibility.md](feasibility.md)；对外契约见 [api-compatibility.md](api-compatibility.md)。

---

## 1. 目标与非目标

### 目标

| # | 目标 | 可验收的标准 |
|---|---|---|
| G1 | **一套稳定的 API**，与 jev / TypeSafe System One 一致 | 官方 `typesafe-sdk` 不改一行代码即可指向 Decis；官方文档示例原样跑通 |
| G2 | **多引擎**，同一 API 背后可换模型 | 新增一个引擎 = 新增一个实现 `DecisionEngine` 的模块 + 一行注册，不改动 HTTP 层 |
| G3 | **高性能**，面向高频小请求 | 支持跨请求动态批处理；给出可复现的 latency/throughput 基准，数字由脚本生成而非手写 |
| G4 | **开箱即用**，权重默认打进镜像，也支持挂载 | `docker run` 一条命令起服务；`DECIS_MODEL_DIR` 指向挂载卷即可用外部权重 |
| G5 | **工程化质量** | 契约测试进 CI；无权重也能跑完整测试（MockEngine + tiny-fixture）；README 面向人类、AGENTS.md 面向 AI |

### 非目标（明确不做）

- 不做训练/微调（可以留 hook，但 Decis 是*推理服务*）
- 不做文本生成、不做 chat completion
- 不做流式（官方无流式接口）
- 不做多模态 state（decision 模型只吃文本）
- 不追求与 jev 的**数值**一致（权重不同，只保证**接口与语义**一致）

---

## 2. 核心抽象：一切决策模型都是"给选项打分"

这是整个设计的支点。观察两个完全独立的开源实现：

| | kev（自回归 LM 变种） | Laya（双向编码器变种） |
|---|---|---|
| 骨架 | Qwen3.5 + LoRA r=16 | ModernBERT-large / mmBERT-base |
| 读出头 | pointer head 对每个 `</opt>` 的 hidden state 与 `<decide>` 打分 | 每个选项一个 `[MASK]` marker token，对 marker 位置打分 |
| 三种 primitive | 全部降维成 2/ K / L 个"选项" | 同左 |
| 一次前向 | 打包所有问题的分支，共享 state 前缀 | `collate_items` 展平所有问题成 batch |

**两者殊途同归：`(state, instructions, [option…]) → 每个选项一个概率`。**

所以 Decis 的中间表示（IR）就是这个：

```python
# 一个问题的一次推理结果：与 options 逐位对齐的概率分布，和约为 1
ProbDist = list[float]
```

**关键决策：引擎只负责产出 `ProbDist`，不负责产出线格式。** 把 `ProbDist → Noul/Choice/Score answer` 的映射收敛到**唯一一处**（§4 的 `answers.py`）。收益：

1. 三种 primitive 的语义（`noul = p[1]`、`score = Σ k·p_k`、`legend` 键为字符串）只实现一次，不会在每个引擎里各错一遍。
2. `confidence` 跨引擎定义一致——同一个阈值在不同引擎下含义相同。这是"统一 API"真正的价值所在。
3. 新增引擎的接入成本降到"把权重跑出概率向量"。

---

## 3. 分层架构

```
                   ┌──────────────────────────────────────────────┐
 HTTP              │ FastAPI  app.py                              │
 (契约层)           │  POST /v1/systemone   GET /v1/models         │
                   │  鉴权 · request-id · 422 校验 · CORS · 健康检查 │
                   └───────────────────────┬──────────────────────┘
                                           │  DomainRequest (已归一化)
                   ┌───────────────────────▼──────────────────────┐
 归一化层           │ schema.py   线格式 ⇄ 域模型（唯一转换点）        │
 (单一事实来源)      │ render.py   JSON/state → 模型可见文本（唯一）   │
                   │ answers.py  ProbDist → Answer（唯一）         │
                   └───────────────────────┬──────────────────────┘
                                           │  list[PreparedRequest]
                   ┌───────────────────────▼──────────────────────┐
 调度层             │ batch.py    跨请求微批处理（按 question 聚合）  │
 (吞吐核心)         │ pool.py     进程/线程池 · 队列 · 背压 · 超时     │
                   └───────────────────────┬──────────────────────┘
                                           │  batched items
                   ┌───────────────────────▼──────────────────────┐
 引擎层             │ base.py     DecisionEngine / EngineInfo       │
 (可插拔)           │ registry.py id → 引擎类 · 别名表 · 能力声明     │
                   │ engines/kev.py    engines/laya.py             │
                   │ engines/remote.py（转发真 jev） engines/mock.py│
                   └───────────────────────┬──────────────────────┘
                                           │
                   ┌───────────────────────▼──────────────────────┐
 模型资产层         │ paths.py    权重解析：本地目录优先 > HF 缓存     │
 (离线可用)         │ download.py CLI：构建期把权重烘进镜像           │
                   └──────────────────────────────────────────────┘
```

依赖方向严格单向向下。**引擎层不得 import HTTP 层；归一化层不得 import 任何引擎。**

### 3.1 HTTP 层：认证、限流、错误映射

初版设计**完全漏掉了鉴权**——这是一个真实的安全缺口：Decis 的 Docker 镜像监听 `0.0.0.0:8000`，而 kev 上游之所以把 `host` 硬编码成 `127.0.0.1`，正是为了不让一个无鉴权的推理服务暴露在网络上。Decis 把服务开放出去，就必须自己把这件事做对。

**认证**：

- `DECIS_API_KEY`（单个）与 `DECIS_API_KEYS`（逗号分隔多个，用于轮换）二选一。用 `hmac.compare_digest` 做常数时间比较，不走 `==`。
- **顺序是契约的一部分**：**先认证、再解析/校验请求体**。线上实测确认真 jev 就是这个顺序（无 key + 非法 body → 403 而非 422）。反过来的实现会把"你的请求体哪里不合法"泄露给未认证调用方。
- 状态码分工（与 jev 一致，见 [api-compatibility.md §5](api-compatibility.md)）：

  | 情况 | 状态码 | body |
  |---|---|---|
  | 无 `Authorization` 头 / scheme 不是 `Bearer` | **403** | `{"detail":{"error_type":"authentication_error","message":"…"}}` |
  | 提供了 `Bearer` 但 key 不对 | **401** | 同上形状，message 不同 |

- **默认行为必须安全**：未设置 `DECIS_API_KEY` 时，若 `--host` 不是回环地址，**拒绝启动**并给出明确错误，同时提示可以用 `DECIS_ALLOW_NO_AUTH=1` 显式承担风险。宁可启动失败，也不要静默地跑一个公开的无鉴权推理服务。这是从 kev 的 `127.0.0.1` 默认值学到的：**不安全的默认值会被用户原样部署到生产**。
- 401 响应带 `WWW-Authenticate: Bearer`（RFC 9110 要求；上游漏了，Decis 补上，见 [api-compatibility.md §5.2](api-compatibility.md)）。

**限流与背压**（与 §6.5 是同一套机制，这里定义对外表现）：

- `DECIS_RATE_LIMIT_RPM`、`DECIS_RATE_LIMIT_TOKENS_PER_S`，默认关闭。两者任一超限 → **429 且必须带 `retry-after-ms`**。不带该头会让官方 SDK 退化成指数退避，把已过载的服务打得更狠。
- 队列满同样走 429（而不是 503/529）：429 在 SDK 的重试集合里且语义准确。

**请求体上限**（OpenAPI 未声明，必须自己定，否则一个巨大 `state` 就能打爆内存）：

- `DECIS_MAX_REQUEST_BYTES`（默认 2 MiB）——在**读取 body 之前**就用 `Content-Length` 拒绝，不给攻击者把整个 body 读进内存的机会。
- `DECIS_MAX_STATE_CHARS`、`DECIS_MAX_QUESTIONS`——在 422 阶段拒绝，`loc` 指向具体字段。

**CORS**：默认**关闭**。它是一个 API，不是给浏览器直接调用的；开 `*` 会让任意网页拿着用户浏览器里的 key 打你的服务。需要时用 `DECIS_CORS_ORIGINS` 显式列出。

---

## 4. 归一化层：三个"唯一事实来源"

### 4.1 `render.py` —— 文本渲染

`state`、`instructions`、`criteria` 都是 `str | dict | list | null`，必须扁平成模型能读的文本。这一步**直接决定模型质量**，而且必须跨引擎一致，否则同一个请求在 kev 上和 Laya 上看到的文本不同，比较就失去意义。

规则（以 kev `api.py:49-59` 的 `render()` 为参考实现）：

```python
def render(value, indent=0) -> str:
    """str|int|float|bool → 原样；list → 每项 '- '；dict → 'key:' 后跟缩进内容。字段名保留为 label。"""
```

- 对象/数组的**字段名必须保留**——它们是模型判断的线索（docs 中的结构化 `instructions` 示例正是靠字段名 `field`/`extracted_value` 表达语义）。
- `option_text(name, desc)`：`desc` 为 `None`/`""` 时只用 `name`，否则 `"name: desc"`。
- **不可伪造分隔符**：用户文本里若出现引擎的特殊 token（如 kev 的 `<|fim_prefix|>`），必须转义（kev 用 `<|name|>` → `<¦name¦>`）。Decis 在 `render.py` 统一做一次，引擎不得各自处理。

> `render.py` 是**契约的一部分**，改动会改变所有引擎的行为。CI 必须有一组冻结的 `(input, rendered_text)` 快照。

**Stage 1 修正：`render.py` 的边界在哪。** 上面写的"唯一"需要更精确。接 Laya 时发现的实际情况是：

- **Laya 自己会渲染选项文本**。`build_sequence` 内部调用它自己的 `render_options`
  （`laya/common.py:33-46`），给每个 choice 选项加 `"name: "` 前缀、给每个 score level 加
  `"level N: "` 前缀、给 noul 补上默认的 false/true 描述。这些是 **Laya 序列格式的一部分**，
  重写一份放在 `render.py` 里既是对上游内部的复制，也保证会漂移。
- 所以正确的分工是：**`render.py` 拥有"任意 JSON → 可读文本"这一步**（`state` 整体、
  `instructions`、每个 criterion 的值），这是所有引擎共用的、也是"同一个请求在不同引擎上看到的
  文本一致"这条要求的落点；**每个引擎拥有"把这些片段排成它自己的序列"这一步**，因为那是模型
  特有的，而且由模型自己的库提供。
- 这个修正**没有动 `answers.py`，也没有动路由层**——`AGENTS.md §5` 说"如果接一个引擎要改这两处，
  说明抽象错了"，这条通过了；要修正的是 `render` 契约的表述，不是它的实现。
- 一个直接的后果：`noul` 的两个选项名 `"false"/"true"` 必须有一个渲染侧的家，供"只想构造一个
  合法 noul"的调用方（例如引擎的 warmup）使用。它是 `render.noul_options`
  （`AGENTS.md §2`），而不是各处的字面量；`tests/test_conventions.py` 断言只有 `render.py`
  里出现 `Option("false"`。

**还有一条 Stage 1 才变得具体的要求：引擎必须能报告"我到底会吃掉多少 token"。**
初版把容量校验设计成"引擎提供一个 `count_tokens(texts)`，`schema.py` 拿预算去比"。用 Laya 一测
就发现这个接口不够：`build_sequence` **会静默截断** state、instructions 和选项，而它的 head 预算
里还包含 `render.py` 根本看不到的东西（`"name: "`、`"level N: "`、每个选项一个 `[MASK]`、分隔符）。
用 `len(text)//4` 或只数渲染后的文本都会**低报**，于是请求通过校验、然后被引擎悄悄截断——
一个自信的、基于部分输入的答案，正是 `AGENTS.md §5-4` 禁止的。

所以接口改成 `DecisionEngine.measure(request) -> MeasuredTokens`：**引擎测量，`schema.py` 决定
怎么办**。`MeasuredTokens` 同时给出 `sequence_tokens`（最长的那条完整序列，state 与 head 共享同一个
预算，所以必须一起算）和 `head_tokens`（每题自己的 head 开销），`EngineInfo.max_state_tokens`
相应改名为 `max_sequence_tokens`。Laya 侧的换算被压缩成一个可直接验证的表达式
（`engines/laya.py: budgeted_head`），"它是否恰好等价于 build_sequence 的不截断条件"由
`tests/test_upstream_contract.py` 在 11 种形状 × 6 个预算上**双向**断言。

### 4.2 `answers.py` —— 概率 → 答案

```python
def to_answer(kind: str, keys: Sequence[str], probs: Sequence[float], legend=None) -> dict:
    if kind == "noul":
        return {"type": "noul", "noul": r2(probs[1])}  # 标量，无 confidence
    if kind == "choice":
        return {
            "type": "choice",
            "choice": keys[argmax(probs)],
            "confidence": r2(choice_confidence(probs)),
            "probabilities": {k: r2(p) for k, p in zip(keys, probs)},
        }
    return {
        "type": "score",
        "score": r2(sum(i * p for i, p in enumerate(probs))),
        "confidence": r2(score_confidence(probs)),
        "legend": {str(i): t for i, t in enumerate(legend)},
        "probabilities": {str(i): r2(p) for i, p in enumerate(probs)},
    }
```

`keys` 的决定规则（`question_keys`）：

| kind | keys |
|---|---|
| `noul` | `["false", "true"]` |
| `choice` | `list(criteria.keys())`，**顺序即请求中的顺序** |
| `score` | `["0", "1", …]` |

`confidence` 的定义（Decis 自有、公开、跨引擎统一）。两者口径一致：**0 = 模型没有倾向（均匀分布），1 = 模型确定**。

- Choice：`(p_max − 1/K) / (1 − 1/K)`，`K = 1` 时为 `1.0`
- Score：`1 − H(p) / ln(L)`（归一化熵），`L = 1` 时为 `1.0`

Score 用归一化熵而不是"到众数的平均距离"，因为后者的可达区间不是 `[0,1]`：加上众数约束后，`L=3` 的均匀分布算出来是 `0.5` 而非 `0`——"没有倾向"却报出中等置信度，无法向用户解释。实现见 `src/decis/answers.py` 的 `score_confidence`。

理由见 [api-compatibility.md §7](api-compatibility.md)。引擎原生置信度不丢弃，放进响应的 `decis.native_confidence` 扩展字段。

### 4.3 `schema.py` —— 线格式 ⇄ 域模型

Pydantic 模型**手写**，字段名与 `typesafe_sdk/_schemas/models.py`（由官方 OpenAPI 生成）逐字对齐，并额外做引擎容量校验。

域模型（内部）：

```python
@dataclass(frozen=True)
class Option:
    key: str  # 线格式键
    text: str  # 模型可见文本


@dataclass(frozen=True)
class PreparedQuestion:
    qid: str
    kind: Literal["noul", "choice", "score"]
    instructions: str
    options: tuple[Option, ...]


@dataclass(frozen=True)
class PreparedRequest:
    state_text: str
    questions: tuple[PreparedQuestion, ...]
```

一个设计选择：**`noul` 也展开成 2 个 option**（`false` / `true`）。这样引擎侧只有一条代码路径（永远在给选项打分），三种 primitive 的差异全部消失在上层。kev 和 Laya 都独立收敛到这个做法，验证了它。

---

## 5. 引擎层

### 5.1 协议

```python
@dataclass(frozen=True)
class EngineInfo:
    id: str  # "laya-multilingual" / "kev-0.8b"
    version: str  # 上游包/权重版本，用于拼响应 model 字段
    primitives: frozenset[str]  # {"noul","choice","score"}
    max_options: int  # 单问选项上限
    max_state_tokens: int
    max_question_tokens: int
    languages: str  # "en" / "100+" / "multilingual"
    device: str  # "cpu" / "cuda" / "mps"
    dtype: str
    description: str  # 用于 GET /v1/models
    release_date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class WorkItem:
    """一个 state 上的一个问题。调度器的最小工作单位。"""

    request_id: str  # 回填用；不发给模型
    state_text: str  # 已完成渲染（render.py）
    question: PreparedQuestion  # 已完成渲染


class DecisionEngine(Protocol):
    def info(self) -> EngineInfo: ...
    def load(self) -> None: ...  # 幂等；冷启动在服务就绪前完成
    def predict(self, items: Sequence[WorkItem]) -> list[ProbDist]: ...
    def close(self) -> None: ...
```

**`predict` 的签名是关键，初版把它设计错了。** 初版是：

```python
# 初版（错误）：整批共享一个 state_text
def predict(self, batch: Sequence[Sequence[PreparedQuestion]], state_text: str) -> list[list[ProbDist]]: ...
```

它假设"一批问题共享同一个 state"。但真实流量里**每个请求的 state 都不同**（不同的工单、不同的邮件），按 state 分组会让组大小退化成 1，**跨请求批处理——整个吞吐论点的基石——就永远不会发生**。这是初版最严重的缺陷。

现在改成**扁平的工作项列表，每项自带 `state_text`**，返回与之逐位对应的 `list[ProbDist]`。这个签名严格更一般：

| 引擎批处理形态 | 如何实现 `predict(items)` |
|---|---|
| **Laya**（每 item 独立构序列，state 嵌在序列里） | 直接全量喂给 `collate_items`，一次前向 |
| **kev `rows` 模式**（每行是独立序列） | 同上，全量一次前向 |
| **kev `prefix` 模式**（同一 state 共享 KV 前缀） | 引擎内部按 `state_text` 自行分组，每组一次前向，最后按原顺序拼回 |

关键点：**"能不能跨 state 批"是引擎的实现细节，不是协议的约束**。协议只要求"给我一批工作项，还我一串等长的概率分布"。能跨 state 批的引擎自然拿到大 batch；不能的引擎自己退化，且退化代价被限制在引擎内部。

`scheduler/pool.py` 因此可以把**任何**请求的问题合并成一批（只受 token 预算与引擎容量约束），不需要按 state 分组。这同时消掉了"谁来负责 state 一致性"这个原本被推给调度器的问题。


### 5.2 三个引擎的正确性边界

**`engines/laya.py`** —— 依赖 PyPI `laya>=0.3.5,<0.4`（Apache-2.0，wheels 仅 42KB）。

- 上游 `Agent.system_one()` 已经返回近乎正确的 jev 形状，但 Decis **不复用它的答案构造**，只复用它的一次前向：
  - 用 `laya.common.build_sequence` 构造每条问题的序列与 option marker 位置；
  - 用 `laya.common.collate_items` **把多个请求的问题组展平成一个 batch**（`common.py` 中 `items = [it for group in batch for it in group]`，这是官方原语，不是 hack）；
  - 手动调 `Agent.model(...)` 拿到 `logits`，切片回每个请求；
  - 自己套温度（`Agent.temperature_by_options`，官方已做 `[0.5, 5.0]` clamp）并 softmax → `ProbDist`；
  - 答案交给 `answers.py`。
- 这样做的收益就是 §2 说的统一 `confidence`，以及**跨请求批处理**——上游 `system_one` 一次只能处理一个请求的问题组。
- 必须处理的两个上游特性：
  1. `noul` 的 `confidence` 被上游覆写成 `max(p, 1−p)`——Decis 不发这个字段（`noul` answer 按契约没有 confidence）。
  2. 上游返回的 `action.act_probability` 是 escalate head 的输出——放进 `decis.engine` 扩展字段，不进核心 schema。
- 风险：Decis 使用了 `laya.common` 的模块级函数。这些是包内公开名字（`__init__.py` 导出了 `render_options`、`confidence_from_probs`、`QTYPES` 等），但 `build_sequence`/`collate_items` 未在 `__all__` 中。**缓解**：pin `<0.4`；加一个"上游契约测试"，断言这两个函数存在且签名未变；上游若改，测试先红。

**`engines/kev.py`** —— **不 pip 依赖 kev**。

- 理由：kev 未发布到 PyPI；其 `pyproject.toml` 把 `datasets`、`scikit-learn` 列在**运行时**依赖里，只为了一个推理服务而引入它们不合理。而 kev 的 `AGENTS.md` 自己就把 `model.py`/`api.py`/`checkpoint.py` 三件套 vendor 进 HF Space，说明这套文件本就是为嵌入而组织的。
- 方案：vendor 一份**最小子集**到 `src/decis/engines/_kev_vendor/`，pin 到具体 commit，附 `NOTICE` 与 Apache-2.0 许可：
  - `model.py` 的 `encode` / `branch_mask*` / `PointerHead` / `DecisionModel`（含 prefix cache 三条路径）
  - `checkpoint.py` 的 `Checkpoint` / `Meta` / `LoadOptions`
  - **不要** `api.py`（答案构造由 Decis 自己做）、**不要** `train/evaluate/suite/data` 等研究代码
- 上游 `serve.py:147` 硬编码 `host="127.0.0.1"`——Decis 自己的 HTTP 层完全绕开它，不存在这个坑。
- 必须遵守的上游不变量（`kev/AGENTS.md` 的 parity 要求）：merged/unmerged LoRA、prefix cache 命中/未命中、shape bucket padding 三条路径结果一致。Decis 的引擎测试要覆盖这三条。
- **dtype 默认值必须按「引擎 × 设备」决定，不能全局统一**。实测（见 [feasibility.md §4](feasibility.md)）：kev-0.8b 在 CPU 上 `bf16` 比 `fp32` **慢 83 倍**（137s vs 1.66s），而两者概率几乎相同（差 ≤0.01）。原因是 Qwen3.5 的 Gated DeltaNet 在缺少 `flash-linear-attention` 时会回落到参考实现，而该路径在 CPU 上的 bf16 表现病态。
  因此 `registry.py` 要为每个引擎声明 `default_dtype` 与「已知劣化组合」表：

  ```python
  # (engine, device) -> dtype；未列出的组合用引擎默认值
  DTYPE_DEFAULTS = {
      ("kev", "cpu"): "fp32",  # bf16 on CPU is ~83x slower (measured)
      ("kev", "cuda"): "bf16",
      ("laya", "cpu"): "fp32",  # 上游 Agent 自身也会在 cpu/mps 上强制 fp32
      ("laya", "cuda"): "fp16",
  }
  DEGRADED = {("kev", "cpu", "bf16"): "kev bf16 on CPU is ~83x slower than fp32 (measured); use fp32"}
  ```

  当用户显式覆盖成一个已知劣化组合时，**启动日志必须大声告警**，而不是安静地慢 83 倍。

**`engines/remote.py`** —— 转发到真 `api.typesafe.ai`。

- 价值有三：用户可以同一套 API 在本地/托管之间切换；**它是差分测试的 oracle**（见 §9）；成本极低（一个 httpx 客户端）。
- 需要 `TYPESAFE_API_KEY`；`EngineInfo` 的能力来自配置或 `GET /v1/models`。

**`engines/mock.py`** —— 确定性假引擎。

- 输入哈希 → 固定概率分布。用于 CI 中跑完整契约测试而**不下载任何权重**，也用于压测 HTTP 层而不受模型速度干扰。

### 5.3 引擎的加载必须是惰性的

```python
# registry.py
ENGINES: dict[str, tuple[str, str]] = {
    "laya": ("decis.engines.laya:LayaEngine", "laya"),
    "laya-multilingual": ("decis.engines.laya:LayaEngine", "laya"),
    "kev-0.8b": ("decis.engines.kev:KevEngine", "kev"),
    "remote": ("decis.engines.remote:RemoteEngine", None),
    "mock": ("decis.engines.mock:MockEngine", None),
}
```

字符串路径 + 用时 import。理由：一个只装 `decis[laya]` 的镜像里**没有 torch 版的 peft**，反之亦然；`GET /v1/models` 和 `/healthz` 必须在只加载了所选引擎的情况下可用。可选依赖 extras：

```toml
[project.optional-dependencies]
server = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "pydantic>=2.9"]
laya   = ["laya>=0.3.5,<0.4"]
kev    = ["torch>=2.6,<3", "transformers>=5.17,<6", "peft>=0.21", "accelerate>=1.15"]
all    = ["decis[laya,kev]"]
```

---

## 6. 并发与批处理：QPS 的核心

### 6.1 事实基础

- kev 与 Laya **都是同步、阻塞、单模型实例**的推理路径，没有 server、没有异步、没有跨请求 batching。
- 两者的算力瓶颈都在**一次前向的 GEMM**，单条问题的 batch=1 前向严重浪费算力。Laya 的官方基准已经证明这点：T4 上 1 问 39.5ms，10 问 158.6ms → **15.9ms/问**；50 问 771ms。即批处理把单问成本降了一半以上，且问题越多越接近稳态吞吐（103–332 q/s）。
- Python GIL：`torch` 的算子会释放 GIL，tokenization 不会。所以纯线程池收益有限，**真正的并行单位是进程**。

### 6.2 调度单位是"问题"，不是"请求"

一个请求带 Q 个问题。若按请求调度，batch=2 个请求但每个 1 问，等于 batch=2；而按问题调度，可以攒到 batch=32。这是 Decis 相对"直接包一层 FastAPI"的核心差异。

```
请求 A (2 问, state=X) ─┐
请求 B (3 问, state=X) ─┼─► 队列 ─► 攒批器(只按 engine/model/token 预算, 不按 state) ─► engine.predict(items)
请求 C (1 问, state=Y) ─┘                                                          │
                                                                         引擎内部决定如何前向
```

**攒批器不按 state 分组**（初版是分组的，说明见 §5.1——那会让真实流量下的 batch 恒为 1）。它只按 `(engine, model)` 分区、按 token 预算与引擎容量上限凑批。state 复用是**引擎内部**的优化：kev 的 `prefix` 模式会自己在批内找相同 state 的项并共享 KV 前缀，Laya 不需要。协议因此对两者一视同仁，能跨 state 批的引擎自然拿到更大的 batch。

### 6.3 两个引擎的批处理形态差异

| | Laya | kev |
|---|---|---|
| 跨 state 组合 | **原生支持**：`build_sequence` 把 state 嵌进每条序列，`collate_items` 展平多组 | `rows` 模式原生支持；`prefix` 模式只在同 state 内受益 |
| 模式 A（推荐默认） | — | **rows**：每条 (state+question) 作为独立 causal row，右 padding 成一个大 batch。上游 `forward_rows_batch` 已是这个形状（训练即用此路径） |
| 模式 B | — | **packed + prefix cache**：同 state 共享 KV 前缀，只跑分支。同 state 重复请求延迟最低 |
| 权衡 | 无 | rows 吞吐高但 state 被重复计算；packed 适合同 state 热点（如固定文档 + 多变问题） |

配置：`DECIS_BATCH_MODE=rows|prefix|auto`（auto：**批内 state 重复度**高时用 prefix，否则 rows）。**这是需要实测才能定默认值的开关**，见 [feasibility.md](feasibility.md)。

> `auto` 的判据必须是"批内 state 重复度"，不是"队列里出现过相同 state"。在真实流量（state 各不相同）下 `auto` 应当稳定地选 `rows`；只有"固定文档 + 多变问题"这类场景才切 `prefix`。**`prefix` 模式的适用面比初版设想的窄**——初版把"同 state 分组"当成主路径，那是错的。这一点需要 Stage 3 实测确认，**在实测前不得写进 README 的性能承诺**。

### 6.4 进程模型

```
uvicorn  (N_http workers, 无模型)
   │  HTTP → 内部队列 (multiprocessing / 共享内存 or 本地 socket)
   ▼
engine workers × N_engine   (每个进程一份权重，跑攒批循环)
```

- 默认 `DECIS_HTTP_WORKERS=1`、`DECIS_ENGINE_WORKERS=1`，用小机器也能跑。
- 机器上量后推荐 `N_engine = 加速器数` 或 `= min(cpu/4, 4)`（CPU）。
- **内存要按实测 RSS 算，不是按权重体积算**。CPU 实测（见 [feasibility.md §4](feasibility.md)）：`laya-multilingual` 峰值 RSS **4.84 GB**、`laya` **2.80 GB**，而权重只有 644 MB / 804 MB —— 差额是 PyTorch 运行时加长序列 × 大批次的激活。所以 `N_engine × 5 GB` 才是容量规划公式。
- **冷启动实测 73–76 s**（同一环境）。这直接决定 §10 的 `/healthz` 与 `/readyz` 必须分离，以及 §8 里 `HEALTHCHECK --start-period` 取 120 s。
- 单机不引入 Redis/Celery 等外部依赖；跨机扩展留给 Stage 4。

**并发不变量（必须写进代码注释与测试）**：

> **一个引擎实例在任一时刻只能被一个线程执行 `predict`。**

理由：`torch.nn.Module` 的 `forward` 通常不保证可重入（Laya 的 `Agent.model(...)` 会读写模块状态，kev 的 prefix cache 是有状态字典），而 MPS/CUDA 的某些后端在并发提交时行为未定义。所以：

- 引擎实例由**单线程**驱动（进程模型天然满足；线程池模型必须用**每实例一把锁**，而不是细粒度锁）。
- `DECIS_ENGINE_WORKERS > 1` 是**多进程**，不是多线程共享权重。`fork` 后各进程各自持有权重副本——这正是 §6.4 内存要按 `N_engine × RSS` 算的原因。
- 禁止跨线程共享 tokenizer 之外的任何引擎内部对象。tokenizer 的 `encode` 在多线程下是安全的，但**不要在并发路径上共享 PyTorch tensor**。

**如果进程模型在 Stage 1 过于复杂，可先做进程内线程池 + 攒批**（一个模型实例、一把锁、一个攒批窗口），把接口留成 `Scheduler` 协议，后续替换实现不动引擎。这是推荐的分阶段落地方式。

### 6.5 背压与超时

- 队列上限 `DECIS_MAX_QUEUE`，满则 **429 且带 `retry-after-ms`**（不是 503/529——429 在官方 SDK 的重试集合里且语义准确；不带 `Retry-After` 头会让 SDK 用指数退避砸已过载的服务）。
- **单请求必须在 10 s 内返回**。这不是我们自己定的数字：官方 SDK 的 `DEFAULT_TIMEOUT` 是 **10.0 s / 次 HTTP 操作**，超过它 SDK 会放弃并**重发**（连接错误与超时也在默认重试集合里）。服务端还在算的时候客户端已经重发，负载会被放大成 2–3 倍。所以：
  - `DECIS_REQUEST_TIMEOUT_MS` 默认 **8000**（给网络留余量）；
  - 队列等待时间计入这个预算，不允许"排队 30 s 然后正常处理"；

**Stage 2 修正：这个预算只能加在"进入引擎之前"。** 初版写"预算耗尽返回 504"、"单次 `predict`
也要有上限"，实现时发现后者做不到：**同步的 `torch` 前向一旦开始就无法中断**，Python 里没有安全的
办法把线程从一次 forward 里拉出来。能强制执行的只有**取锁**这一步，那也恰好是唯一属于 Decis 责任
而非模型责任的部分。于是：

  - `InProcessScheduler.run` 用 `acquire(timeout=DECIS_REQUEST_TIMEOUT_MS)` 取锁；
  - 取不到就返回 **429 + `retry-after-ms`**，而**不是 504**。两者都在官方 SDK 的重试集合里，
    但 504 不带退避指令，SDK 会退回指数退避继续砸一个已经饱和的服务（§3-16）；429 才能告诉它等多久。
    这个请求**根本没有被启动**，所以它不消耗算力，也不会有"算完了但客户端已走"的浪费；
  - 已经在算的工作不受预算约束——这是必须如实说明的局限，不是可以悄悄略过的细节。
  - **前提条件**：阻塞路由**不得**写成 `async def`，否则序列化发生在事件循环上，锁和预算都形同虚设
    （`design-review.md §2-D8` 记录了踩到的这个坑）。

守卫：`tests/test_request_budget.py`。
- 攒批窗口 `DECIS_BATCH_MAX_WAIT_MS`（默认 2–5ms，需实测）：太小失去批处理收益，太大增加尾延迟。**窗口必须有上限**，否则低流量时每个请求都会等到窗口结束才开始算——这会把 p50 延迟凭空抬高一个窗口长度。
- **必须暴露的指标**：`queue_depth`、`batch_size_histogram`、`engine_infer_ms`、`prefix_cache_hit_ratio`、`rejected_total{reason}`。没有这些就无法调参，也无法区分"慢"是因为排队、算力还是 tokenization。

### 6.6 批处理会改变数值结果（必须显式面对）

这是一个容易被忽略、但会影响正确性主张的事实：**批处理会改变浮点结果**。

- padding 改变归约顺序；不同 batch 组成导致 GEMM 的 tiling 不同；Laya 的 option 预算还会随问题数变化（`head_max_len=192` 是按批内最长项截断的）。
- 因此同一个 `(state, question)` 在 `batch=1` 与 `batch=32` 下**可能得到不同概率**，极端情况下 `noul` 会跨过 0.5 或 `choice` 的 argmax 翻转。

这与契约直接相关：官方 SDK **会对 POST 重试**，所以"同一个请求重发两次拿到不同答案"是可观测的行为，会被用户当成 bug 报告。

处理方式（不是忽略它）：

1. **承认并在文档里写明**：`/v1/systemone` 是**纯函数级**的（相同输入 + 相同批组成 → 相同输出），但**不承诺跨批组成的逐位一致**。这是所有批处理推理服务的共同性质，不是 Decis 的缺陷。
2. **加测试把偏差钉住**：`tests/test_batch_invariance.py` 用固定权重、固定输入，比较 `batch=1/8/32` 的 `noul` 与概率向量，断言最大绝对偏差小于一个阈值（初值 0.02），并断言 **argmax 不变**。argmax 翻转必须视为测试失败——那是用户能感知的错误，而概率的微小抖动不是。
3. **给用户一个逃生门**：`DECIS_BATCH_MAX_SIZE=1` 关闭批处理，换取逐位可复现（代价是吞吐）。需要审计/回归对比的场景可以用它。
4. 这条也是 Stage 1 就要做的，不是优化项的附属品——因为如果偏差大到会翻转 argmax，整个批处理设计的价值就要重新评估。

---

## 7. 模型资产与离线部署

### 7.1 解析顺序（一个函数，两处调用）— ✅ Stage 1 已实现

```
resolve(spec, settings) →                                    src/decis/paths.py
  1. $DECIS_MODEL_PATH_<ENGINE_ID> 指向的目录是完整 checkpoint  → 用它（你自己的权重）
  2. $DECIS_MODEL_DIR/<engine-id>/ 是完整 checkpoint           → 用它（挂载模式）
  3. $DECIS_MODEL_DIR/<engine-id>/<subfolder>/ 是完整 checkpoint → 用它（仓库布局的挂载）
  4. 有 repo_id → 交给调用方去 snapshot_download（decis download）→ 再回到上面的判断
  5. 否则 → 报错，错误信息里给出确切的下载命令
```

- **"完整"是有定义的**：目录里必须有 `rl_agent_config.json`（引擎在 `WeightSpec.marker` 里声明）。
  这条不是形式主义：`DECIS_MODEL_DIR` 通常是个挂载点，而**挂载点在卷为空时依然存在**，
  这是最常见的故障，且不检查的话会以模型加载器深处的一个费解错误浮现。`tests/test_paths.py`
  钉住了"存在但为空"和"存在但缺文件"两种情况都必须回退到网络而不是被当成可用权重。
- **本地永远优先于网络**。这是安全性质而非偏好：把权重烘进镜像或挂载的全部意义就是
  镜像可以无出口网络运行，所以一个缺失的卷绝不能导致容器去访问 Hub。
- 第 3 步返回的是**解析后的根目录**（`checkpoint_root`），所以调用方不会把 subfolder 加两次——
  那正是这个函数要防的错误。
- **revision 钉在 commit sha 上**，不是 tag 或 branch：否则上游一次 force-push 就会改变某个已发布的
  Decis 镜像加载的是什么权重，可复现性就没有了（`engines/laya.py: REVISION`）。
  要跑自己的微调就用第 1 条，那是这条规则预留的出口。
- 离线：`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，与 UniTS-Hub 一致。

### 7.2 各引擎的权重清单 — ✅ 体积取自 Hub 文件列表实测

下表"体积"是 `decis download` 实际会拉取的文件总和（用 Hub 的
[tree API](https://huggingface.co/api/models/convaiinnovations/laya/tree/main?recursive=true) 实测，
不是估算）；"模型文件"是其中 `model.safetensors` 的大小。

| 引擎 | 来源 | 下载总量 | 模型文件 | 备注 |
|---|---|---|---|---|
| `laya`（英文） | `convaiinnovations/laya`（根目录） | 807.0 MiB | 803.6 MiB | 421M 参数，ModernBERT-large |
| `laya-multilingual` | 同上，`subfolder="multilingual"` | 646.8 MiB | 614.0 MiB | 322M 参数，mmBERT-base，~2.2× 快 |
| `laya-typed-decisions` | 同上，`subfolder="typed-decisions"` | 807.0 MiB | 803.6 MiB | typed-decisions workflow 专用 |
| `kev-0.8b` | adapter `jaredpalmer/kev-0.8b`（pin `54f4f87`）+ 基座 `Qwen/Qwen3.5-0.8B-Base`（pin `dc7cdfe`） | **45,443,000 B (43.3 MiB) + 1,769,896,333 B (1.65 GiB)** | 43.3 MiB + 1.63 GiB | 实测自本机 Hub 缓存；**基座是大头**，adapter 只占 2.5% |
| `kev-4b` / `kev-9b` | 同上 | 未实测 | ≈582MB + 8GB / ≈392MB + 18GB | 超出"轻量"定位，只做可选镜像 |

三个 Laya checkpoint 共用同一个仓库，但 `allow_patterns` 只列出一个 checkpoint 的文件
（`rl_agent_config.json` / `model.safetensors` / `tokenizer/*` / `encoder/*`，带 subfolder 前缀），
所以装一个不会顺带拉另外两个。`tests/test_paths.py` 断言了这一点，包括"根 checkpoint 的前缀为空
时不能退化成下载全部"。

**实测过的组合**（`benchmarks/results/`）：`laya 0.3.5` + `torch 2.14.0+cpu` +
`transformers 5.17.0` + `safetensors 0.8.0` + `numpy 2.5.3`。`pyproject.toml` 里只钉
`laya>=0.3.5,<0.4`，不重复声明它自己已经声明的 torch 等依赖——加一个我们没测过的下界，
是一个无法支撑的兼容性承诺。

`EngineInfo` 里的 `release_date` 用 Hub 元数据里的 `lastModified`（`2026-09-20`），
那是唯一可核实的日期；它不是营销意义上的发布日。

### 7.3 kev 的特殊处理

kev 是"小 adapter + 大基座"。因此：

- adapter + `head.pt` 很小（113MB），适合随镜像分发或挂载；
- 基座大，**建议烘进镜像或预热到共享卷**；
- 提供 `decis download kev-0.8b --base-only` 之类的能力，让 CI 可以分离"下载基座"和"下载 adapter"。

---

## 8. 打包与镜像

沿用 [UniTS-Hub](https://github.com/kingfs/UniTS-Hub) 已验证的模式（单 Dockerfile + `ARG` 分支 + CI 三段式）。

### 8.1 单 Dockerfile + 构建参数

```dockerfile
# ---------- builder ----------
FROM python:3.12-slim-bookworm AS builder
ARG DECIS_ENGINE=laya-multilingual
ARG DECIS_EXTRAS=laya
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HF_HOME=/tmp/hf
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --extra ${DECIS_EXTRAS}
COPY . /app
RUN uv sync --frozen --extra ${DECIS_EXTRAS}
# 权重在构建期落地：镜像自包含、冷启动可预测、离线可跑
RUN uv run decis download --engine ${DECIS_ENGINE} --dest /app/models
# 构建期 smoke test：缺内核/权重坏/精度不兼容都在这里失败，而不是发布后
RUN uv run decis doctor --engine ${DECIS_ENGINE} --smoke-test

# ---------- final ----------
FROM python:3.12-slim-bookworm AS final
ARG DECIS_ENGINE=laya-multilingual
ARG DECIS_VERSION=0.0.0
ARG DECIS_REVISION=unknown
ARG DECIS_CREATED=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.title="Decis" \
      org.opencontainers.image.description="One API to run all light-weight decision models." \
      org.opencontainers.image.source="https://github.com/kingfs/Decis" \
      org.opencontainers.image.url="https://github.com/kingfs/Decis" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${DECIS_VERSION}" \
      org.opencontainers.image.revision="${DECIS_REVISION}" \
      org.opencontainers.image.created="${DECIS_CREATED}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm" \
      ai.decis.engine="${DECIS_ENGINE}"
ENV DECIS_MODEL_DIR=/app/models HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    DECIS_DEFAULT_ENGINE=${DECIS_ENGINE} PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/models /app/models
COPY --from=builder /app/src /app/src
COPY --from=builder /app/README.md /app/README.md
ENV PATH="/app/.venv/bin:$PATH"

# 非 root 运行。权重目录与缓存目录都要可写或显式只读
RUN useradd --create-home --uid 10001 decis && chown -R decis:decis /app
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# 不用 exec 形式之外的花样：SIGTERM 必须直达 decis，优雅退出才生效
CMD ["decis", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

要点：

- final 阶段只有 venv + 权重 + 代码，**不带编译器/git**；`HF_HOME` 指向 `/tmp` 所以在 builder 里不污染最终镜像；`DECIS_MODEL_DIR` 可被挂载卷覆盖。
- **构建期 smoke test 是硬要求**（`decis doctor --smoke-test`），不是可选优化。kev 在 CPU 上缺内核、Laya 在 numpy 2.x 上的兼容问题，都必须在**构建期**暴露。见 [feasibility.md §5](feasibility.md) 的 R1。
- **OCI 标签**（`org.opencontainers.image.*`）是供应链的基本要求：能追溯版本、revision、构建时间与基镜像。`ai.decis.engine` 用自有命名空间，不与 OCI 保留键冲突。
- **非 root 运行**（`USER 10001:10001`）。只监听 8000 端口，不需要特权。这同时是很多集群的准入要求（pod security standards 的 `restricted` 档禁止 root）。
- **SIGTERM 必须能到达进程**，所以 `CMD` 用 exec 形式且不套 shell 包装，否则容器编排的优雅下线失效（见 §10.3）。
- `--start-period=120s` 是实测冷启动 73–76 s 的直接结论，不是拍脑袋的余量。

### 8.2 CI 三段式（GitHub Actions）

```
test  ──►  build (matrix: engine × arch, push-by-digest, 不打 tag)  ──►  merge (imagetools create)
```

- **matrix**：`engine ∈ {laya, laya-multilingual, kev-0.8b}` × `arch ∈ {amd64(ubuntu-latest), arm64(ubuntu-24.04-arm)}`，`fail-fast: false`。
- build 阶段`outputs: type=image,name=$IMAGE,push-by-digest=true,name-canonical=true,push=true`，digest 存 artifact——**多架构并行时不抢 tag**。
- merge 阶段 `docker buildx imagetools create` 合成 multi-arch manifest。
- tag 规范：`<image>:<engine>-latest`（默认分支）、`<image>:<engine>-<semver>`（`v*` tag）、`<image>:<engine>-<sha7>`（永远打，回滚锚点）。
- 相对 UniTS-Hub 的改进：打开 `provenance: true` + SBOM（供应链），digest artifact 保留 7 天而非 1 天，同时推 GHCR 与 Docker Hub。

### 8.3 镜像清单

| 镜像 | 引擎 | 基座 | 估算体积 |
|---|---|---|---|
| `decis:laya-latest` | Laya 英文 | CPU | ~1.8 GB |
| `decis:laya-multilingual-latest` | Laya 多语 | CPU | ~1.6 GB |
| `decis:kev-0.8b-latest` | kev-0.8b | CPU | ~3.0 GB |
| `decis:kev-0.8b-cuda` | kev-0.8b | CUDA runtime | ~6–8 GB |
| `decis:all-latest` | 全部（demo） | CPU | ~4 GB |

`docker-compose.yml` 用 profiles 按引擎起服务，与 UniTS-Hub 一致。

---

## 9. 测试策略

**核心原则：CI 不下载权重。** 完整契约测试跑在 `MockEngine` 上；真实权重只在夜间/发布前任务里跑。

| 层 | 内容 | 权重 | CI |
|---|---|---|---|
| 单元 | `render` 快照、`answers` 公式、`question_keys` | 否 | ✅ |
| 契约 | [api-compatibility §8](api-compatibility.md) 的 **L0–L3b**（schema 同源、形状、语义不变量、官方 SDK、**错误契约**） | 否 | ✅ |
| 单元（引擎） | 每个引擎用 **tiny random fixture**（随机初始化的小模型 + WordLevel tokenizer，写进 tmp 目录）跑通 `predict` 到 `ProbDist` 的形状与归一化 | 否 | ✅ |
| **批不变性** | `batch=1/8/32` 下 `noul` 与概率向量的最大偏差 < 阈值，且 **argmax 不变**（§6.6）；`tests/test_batch_invariance.py` 还带一个必然失败的负向对照 | 否（tiny fixture） | ✅ |
| 数值 | weight-backed：与存档的参考概率比对（容差），覆盖 kev 的 merged/unmerged、prefix/full、bucket 三条 parity 路径 | 是 | 夜间 |
| 差分 | 同一请求打真 jev 与 Decis，比对 JSON schema **与错误码/错误体形状** | 否（需 key） | 手动（改契约前必跑） |
| 上游契约 | 断言 `laya.common.build_sequence` / `collate_items` 存在且签名未变 | 否 | ✅ |
| 安全 | 无 key → 403、错 key → 401、认证先于校验、未配 key + 非回环地址时**拒绝启动**、超大 body → 413、CORS 默认关闭 | 否 | ✅ |
| 性能 | `benchmarks/run.py` 输出原始逐样本 JSON（含输入 sha256 与全部维度）；`benchmarks/report.py --check` 在 CI 里断言文档表格与原始 JSON 一致 | 运行 `run.py` 时需要 | `--check` ✅ / 采集 手动 |

`tiny random fixture` 的做法直接从 [laya-mlx 的 `tests/conftest.py`](https://github.com/mizoreww/laya-mlx) 学：现场造 3 层 / 64 维的小模型，测试全在 CPU 上跑，不需要下载任何权重。这是让"引擎也要有测试"在 CI 里可行的关键。

性能报告方法论也照抄 laya-mlx：**报告从原始 JSON 生成，不手填数字**；生成前断言各后端处理的输入哈希与 token 数一致。

---

## 10. 可观测性

- 每个响应带 `x-typesafe-request-id`（契约要求），同一条结构化日志里输出：`request_id`、`engine`、`batch_size`、`queue_ms`、`infer_ms`、`total_ms`、`input_tokens`、`prefix_cache_hit`。
- 必须额外记录的两个字段：
  - **`X-TypeSafe-Retry-Count`**（SDK 重试时带上）——非零意味着客户端认为我们在失败，需要单独计数并告警；
  - **`client_disconnect`**——SDK 10 s 超时后客户端会走掉。**必须能在日志里区分"我们慢"和"客户端先走了"**，否则会误判成服务端超时。
- `/metrics` 暴露 Prometheus 指标（可选依赖 `decis[metrics]`）：队列深度、批大小直方图、推理耗时、引擎缓存命中率、各引擎请求计数、`rejected_total{reason}`。
- 日志里的 `state`/`instructions` 内容**默认不打**（可能含用户隐私数据），只打 token 数与 hash；需要排障时用 `DECIS_LOG_PAYLOADS=1` 显式打开。

### 10.1 健康检查三分离

模型加载实测要 **73–76 s**（[feasibility.md §4](feasibility.md)），所以"进程活着"和"能服务"必须分开，否则编排器会在加载期间反复杀 Pod：

| 端点 | 语义 | 何时可用 |
|---|---|---|
| `/healthz` | 进程活着、事件循环没卡死 | 立即 |
| `/readyz` | 权重已加载 **且已跑过一次 warmup 前向** | 加载 + warmup 之后 |
| `/v1/models` | 引擎依赖已 import、权重文件在位 | 立即（**不触发加载**，见 `AGENTS.md §6`） |

`/readyz` 必须包含 warmup 的原因：Laya 的第一次前向会做惰性初始化（kernel 选择、内存池），把它算进"就绪"会导致**第一个真实用户请求特别慢**，而编排器此时已经把流量放进来了。

### 10.2 启动期契约

**Stage 2 修正：改成"先监听、后台加载"，并保留"绝不服务答不了的流量"。** 初版要求
"先加载、后监听"＋"加载失败即退出"。实测后发现这样做的代价是**冷启动的 80 秒里服务完全不响应**，
`/healthz` 也挂起——探针拿不到任何信号，编排器无法区分"还在加载"和"崩了"（`design-review.md §2-D7`
有原始时间线）。

现行契约：

- **端口立刻可服务**，引擎在 lifespan 起的后台线程里加载。`/healthz` 立即 200。
- `/readyz` 三态：`loading`（503 + `retry-after`）→ `ready`（200）→ `failed`（503，**不带**
  `retry-after`，因为重试不会让它恢复）。
- **加载失败不再让进程退出**，而是记录错误文本（截断）+ 完整 traceback 到日志，`/readyz` 持续报
  `failed`。代价是"容器没起来"这个信号消失了，改为靠 `/readyz` 或日志告警——这一点必须让部署者知道。
- 加载期间到达的 `/v1/systemone` 返回 503 `engine_unavailable`，消息明确是"还在加载"还是"加载失败"。
  就绪检查排在 `measure()` **之前**，因为容量校验需要已加载的 tokenizer。
- 启动日志必须包含：引擎 id、device、dtype、线程数、权重路径与来源（本地目录 / HF 缓存）、加载耗时、**以及命中的劣化配置告警**（如 kev+CPU+bf16）。

### 10.3 优雅下线

`SIGTERM` 后：**停止接受新连接 → 排空在途请求 → 释放引擎 → 退出**。宽限期要大于单请求超时预算（8 s），建议 15–30 s。

这一条对 Decis 尤其重要，因为**重启成本是 75 s**：如果编排器滚动更新时直接杀进程，会有一批请求失败并且要重新付一次 75 s 的冷启动。`terminationGracePeriodSeconds` 必须大于冷启动时间，否则滚动更新会永远健康不了（新 Pod 还没 ready，旧 Pod 已经被杀）。

---

## 11. 仓库结构

```
Decis/
├── README.md                     # 面向人类：项目价值、快速开始、文档索引（英文，默认）
├── README.zh-CN.md               # 中文 README（与英文互链切换）
├── AGENTS.md                     # 面向 AI agent：约束、唯一事实来源、命令、禁区
├── LICENSE                       # Apache-2.0
├── NOTICE                        # 第三方署名（kev vendored 代码、Laya 等）
├── pyproject.toml                # uv / hatchling，extras: server,laya,kev,all
├── uv.lock
├── Dockerfile                    # 单文件 + ARG DECIS_ENGINE
├── docker-compose.yml            # profiles: laya / laya-multilingual / kev-0.8b
├── .github/workflows/
│   ├── ci.yml                    # lint + 无权重测试
│   └── docker-build.yml          # test → build(matrix) → merge
├── docs/
│   ├── api-compatibility.md      # jev 契约（唯一事实来源）
│   ├── design.md                 # 本文
│   ├── design-review.md          # 对本设计的自我审查：已知不足与改进项
│   ├── feasibility.md            # 调查 + 可行性 + 实测 + 风险
│   └── contract/
│       ├── typesafe-openapi-0.2.0.json      # 官方 OpenAPI 快照（L0 测试的基准）
│       └── observations-2026-09-22.md       # 线上实测记录（L 级证据的原始材料）
├── src/decis/
│   ├── __init__.py
│   ├── app.py                    # FastAPI 装配
│   ├── routes.py                 # /v1/systemone, /v1/models, /healthz, /readyz
│   ├── auth.py                   # Bearer 校验、常数时间比较、401/403 分工（唯一）
│   ├── errors.py                 # 异常 → 契约错误形状的映射（唯一）
│   ├── schema.py                 # 线格式 Pydantic + 域模型 + 引擎容量校验
│   ├── render.py                 # JSON/state → 文本（唯一）
│   ├── answers.py                # ProbDist → Answer + confidence（唯一）
│   ├── batch.py                  # 攒批器
│   ├── scheduler.py              # Scheduler 协议 + 进程内实现
│   ├── config.py                 # 环境变量（DECIS_*）集中处
│   ├── paths.py                  # 权重解析（唯一）
│   ├── cli.py                    # decis serve|download|models|doctor|bench
│   ├── engines/
│   │   ├── base.py               # DecisionEngine / EngineInfo / WorkItem
│   │   ├── registry.py           # id → 引擎类、别名表、惰性 import、dtype 策略
│   │   ├── laya.py
│   │   ├── kev.py
│   │   ├── remote.py
│   │   ├── mock.py
│   │   └── _kev_vendor/          # pinned subset + NOTICE
│   └── observability.py
├── tests/
│   ├── conftest.py               # MockEngine + tiny fixtures
│   ├── test_contract_openapi.py  # L0：schema 与官方 OpenAPI 快照同源
│   ├── test_contract_shape.py    # L1/L2
│   ├── test_contract_errors.py   # L3b：403/401/422/404/405 + request-id
│   ├── test_contract_sdk.py      # L3：官方 typesafe-sdk
│   ├── test_auth.py              # 认证顺序、常数时间、拒绝不安全的默认启动
│   ├── test_batch_invariance.py  # batch=1/8/32 的偏差与 argmax 稳定性
│   ├── test_conventions.py       # "唯一事实来源"的守卫（照抄 kev 的思路）
│   ├── test_engines_shape.py
│   └── test_upstream_contract.py # laya 内部 API 未变
├── benchmarks/
│   ├── run.py                    # 逐样本 JSON + 输入 sha256
│   ├── report.py                 # 由 JSON 生成 docs 中的表格
│   ├── probe/                    # 实现前的引擎探测脚本（已 checked in）
│   └── results/                  # checked-in 原始结果
└── examples/
    ├── curl.md
    └── python_sdk.py
```

---

## 12. 里程碑

**Stage 0 — 契约冻结（2–3 天）— ✅ 已完成**

交付 `docs/api-compatibility.md`（含 L 级证据）与 `schema.py`、`auth.py`、`errors.py`；不接任何模型，用 `MockEngine` 跑通官方 SDK。结束标志与实际结果：

1. ✅ `typesafe_sdk` 的 `TypeSafeClient` 对着 Decis 跑通三种原语（`tests/test_contract_sdk.py`，走真实 uvicorn socket）。
2. ✅ **L3b 错误契约测试全绿**（403/401 分工、认证先于校验、request-id 格式、422 形状；`tests/test_contract_errors.py`）。
3. ✅ **L0 同源测试**：`test_contract_openapi.py` 断言 `schema.py` 的键集合/required/`const`/`minItems`/`minProperties` 与 `docs/contract/typesafe-openapi-0.2.0.json` 一致；CI 另有一个 job 校验快照版本未被无意 bump。
4. ✅ 认证与不安全默认值的测试全绿（未配 key + 非回环地址 → 拒绝启动）。

实现时新增/偏离设计的两点，均已回写文档：

- 为打破 `schema ↔ render` 的循环依赖，抽出 `domain.py` 作为各层共享的领域类型（`Option`/`PreparedQuestion`/`PreparedRequest`/`ProbDist`）。分层见 `AGENTS.md §4`。
- 加入 `service.py` 作为 HTTP 与引擎之间的编排层，使全部业务判断可以脱离 HTTP 测试；`routes.py` 因此没有分支逻辑。
- 请求容量校验落在 `schema.validate_capacity`，接受一个 `count_tokens` 回调，从而不必 import 引擎层（`AGENTS.md §2` 已同步）。
- 认证放在 ASGI 中间件而非 FastAPI 依赖，使"认证先于请求体校验"成为结构性质而非框架内部顺序的副产品。
- `score` 的 `confidence` 由 kev 的"到众数平均距离"改为归一化熵，理由见 `api-compatibility.md §7`。

**Stage 1 — Laya 引擎闭环** — ✅ 已完成

四个验收标准全部达成：

1. ✅ **`decis serve --engine laya-multilingual` 用真实权重回答真实请求**。三个原语
   （`noul`/`choice`/`score`）都产出合法分布；`tests/test_laya_inference.py` 用真实权重验证，
   共 14 个用例，CPU 上约 90 秒。
2. ⚠️ **`/readyz` 在 warmup 完成后才转绿，但冷启动期间服务完全不响应**——这条与初版设计不符，
   实测记录在 `design-review.md §2-D7`。冷启动实测 **79.7 秒**（CPU）：引擎在 lifespan 里同步加载，
   而 uvicorn 是在 lifespan 跑完之后才进入协议循环的，所以这期间 `/healthz` 也是挂起的，
   不是返回 503。镜像的 HEALTHCHECK 因此必须给足 `start-period`（现为 180 秒）。
3. ✅ **`decis download` 可用**，且**只**拉目标 checkpoint 的文件（三个 checkpoint 共用一个
   仓库，`allow_patterns` 保证不互相牵连）。
4. ✅ **带权重的镜像可用**：`DECIS_PREDOWNLOAD=<engine>` 在构建期落地权重，
   镜像因此可以无出口网络运行。

实现时暴露的三个问题（前两个改的是设计，不只是代码）：

- **`render.py` 的"唯一"边界写得过宽**。Laya 自己渲染选项文本（`"name: "`、
  `"level N: "`、noul 的默认描述），那些是它序列格式的一部分。正确的分工是
  "`render.py` 拥有任意 JSON → 可读文本，引擎拥有把片段排成自己的序列"，
  见 §4.1 的 Stage 1 修正。**这处修正没有触碰 `answers.py` 或路由层**，
  所以 §2 的抽象通过了它的第一次检验。
- **容量校验的接口错了**。初版设计成"引擎给一个 `count_tokens`，`schema.py` 拿预算去比"。
  `build_sequence` 会静默截断，而它的 head 预算包含 `render.py` 看不到的东西
  （选项名前缀、每个选项一个 `[MASK]`、分隔符），所以任何基于"渲染后文本"的估算都会**低报**，
  结果是请求通过校验然后被悄悄截断。改成 `DecisionEngine.measure() -> MeasuredTokens`
  ——**引擎测量，`schema.py` 决定怎么办**；`EngineInfo.max_state_tokens` 随之改名为
  `max_sequence_tokens`，因为 state 与 head 共享同一条序列。upstream 的不截断条件被压成
  一个表达式（`engines/laya.py: budgeted_head`），并由 11 种形状 × 6 个预算的**双向**断言钉住。
- **Laya 的 `Agent` 会和官方 manifest 打架**。上游 `Agent.__init__` 在加载失败时会**静默回落到
  CPU**。Decis 不跟：那会在一次请求中间改变设备，破坏 §3-11 的"POST 是纯函数"，
  并让此后每个请求的延迟变 10 倍。改为抛 `EngineUnavailableError`（503）。

顺带记录一条对 §7 的独立佐证：**Laya 的 `confidence_from_probs` 就是 `1 − H(p)/log k`**，
正是 Decis 为 `choice` 选的归一化熵口径。`source: laya/common.py`。对 Laya 而言
`decis.native_confidence == confidence`；两个字段仍然分开，因为 kev 的 `noul` 口径不同。

**Stage 2 — kev 引擎 + 抽象验证（3–5 天）** — ✅ 完成

**已完成的先行项**（都是接 kev 之前必须先修的地基，详见 `design-review.md §2-D7/D8`）：

1. ✅ **引擎改为后台加载**（D7 方案 B）。冷启动期间 `/healthz` 立即可用、`/readyz` 报
   `loading`/`ready`/`failed`。实测：修正前第一条 HTTP 响应在 **79.7 秒**，修正后 **0.5 秒**。
2. ✅ **修掉阻塞路由**（D8）。`/v1/systemone` 原本是 `async def` 却调用同步推理，一次推理会堵死
   事件循环——连 `/healthz` 一起堵。改成普通 `def` 后由 Starlette 的线程池执行，
   序列化交回给调度器的锁。
3. ✅ **§3-17 的请求预算真正实现**。取锁设 `DECIS_REQUEST_TIMEOUT_MS`（默认 8000）上限，
   超时返回 429 + `retry-after-ms`；见 `docs/design.md §6.5` 与 `tests/test_request_budget.py`。

4. ✅ **kev 引擎落地**。vendor 了 `model.py`/`checkpoint.py`（pin `90990a5`，逐字节 + sha256 守卫），
   实现了 `engines/kev.py`，注册为 `kev-0.8b`（别名 `kev`/`kev-latest`）。**`answers.py`、`render.py`
   与路由层一行未改**——抽象成立。
5. ✅ **抽象验证的实测结论**：Decis 构造的 record 与 kev 自己的 `api.to_record` 逐字段相等；`encode`
   的 `ids/seg/pos/opt/decide_idx/opt_idx` 全等；概率与上游 `model.probs()` 差 `0.00e+00`；
   官方 `typesafe-sdk` 通过 HTTP 拿到 `decis/kev-0.8b@vendored-90990a5` 的完整答案（20/20）。
6. ✅ **新发现两处接口缺口并修掉**：选项文本必须由引擎自己决定（`design-review.md §2-D9`），
   以及容量接口原本没有"state 单独上限"的位置（`§2-D10`，`EngineInfo.max_state_tokens`）。

**未做**：`prefix` 缓存路径（`probs_and_prefix`/`probs_with_prefix`）尚未接进引擎——当前只走
`forward_batch` 的批处理路径。多问题请求里 state 会被每行重复计算，属 Stage 3 的优化，
已记录在 §5.1。kev-4b/9b 未注册（超出"轻量"定位）。
vendor kev 最小子集，接入第二个引擎。**这一步的真正目的是证伪/证实 §2 的抽象**：如果接 kev 需要改动 `answers.py` 或路由层，说明抽象错了，必须回去改。同时验证 §5.1 的 `WorkItem` 签名对 `rows` 与 `prefix` 两种模式都成立。

**Stage 3 — 性能（5–7 天）** — 🟡 进行中

已完成的部分（都不依赖攒批器）：

- ✅ `tests/test_batch_invariance.py`：CI 可跑的批不变性。无权重、无依赖，靠一个真会 padding
  的 fixture 引擎，带负向对照证明断言有效。
- ✅ `benchmarks/run.py`：逐样本延迟 + `input_sha256` + 完整维度（引擎/设备/dtype/线程/进程/
  批大小/state 长度/问题数）的原始 JSON。**线程数在子进程里测**，因为 torch 的线程数初始化后
  改不了，同进程测两个会静默报错一个。
- ✅ `benchmarks/report.py`：由原始 JSON 生成 README 与 `docs/feasibility.md` 的表格，
  **生成前断言同组各配置处理同一个输入**（`input_sha256` + token 数），不一致拒绝生成；
  `--check` 已进 CI。它第一次运行就抓到了 `design-review.md §2-D12`。
- ✅ `decis bench`：`benchmarks/run.py` 的薄包装（不是第二份实现）。

未做：攒批器、进程池、`/metrics`、各引擎 × 设备 × 批大小的完整表。**本阶段有三个必须先做的验证**：

1. **跨请求批处理的真实收益**（§6.2）——这是整个吞吐论点的基石，仍然**完全未实测**。
   Stage 1 只证明了"引擎层能正确地把不同 state 的问题合成一次前向"（
   `tests/test_laya_inference.py: test_one_predict_call_handles_every_state_in_one_forward_pass`），
   那是必要条件，不是收益证据；攒批窗口能不能攒到东西，取决于真实流量的到达分布。
2. ✅ **批不变性**（§6.6）——已在 Stage 1 用真实权重测过（它是阶段 1 的前置，因为若 argmax
   会翻转，整个批处理设计要重估）：16 条不同 state、三种原语、batch=2/4/8/16 共 12 组配置下，
   **最大绝对偏差 8.345e-07，argmax 翻转 0 次**（原始记录
   `docs/contract/stage1-batch-invariance.json`，设备/精度/runtime 均记在文件内）。
   `tests/test_laya_inference.py` 以 `1e-5` 为界——比实测宽一个量级，但比"什么都不测"紧得多，
   足以在 mask 出问题时变红。注意这只覆盖了"同一进程内、
   不同 batch 组成"，**不覆盖**多进程/多 worker 之间的一致性。
   **已补 CI 版本**：`tests/test_batch_invariance.py` 不再依赖权重——它用一个真的会 padding
   并按 padding 宽度累加的 fixture 引擎，在 `batch=1/8/32` 上断言偏差上界与 argmax 稳定，
   并且带一个**负向对照**（故意忽略 mask 的引擎必须让同一个断言失败），保证这个断言不是空转。
   实测：正确引擎在 batch=32 上偏差 1.1e-16、0 次翻转；坏引擎偏差 2.3e-02 且翻转 1 次。
3. `DECIS_BATCH_MODE=rows|prefix|auto` 的默认值与判据。

**Stage 4 — 发布工程（3–5 天）** — ⬜ 未开始
CI 三段式多架构多引擎镜像；GHCR + Docker Hub；`docker-compose.yml`；SBOM 与 provenance（`--provenance`）；README 定稿；`AGENTS.md` 定稿；首个 release。

**Stage 5（可选）— 扩展** — ⬜ 未开始
ONNX Runtime 引擎（无 torch 的极小镜像）；MLX 引擎（macOS，复用 laya-mlx）；`Router` 式按语言自动选 checkpoint；shortlist 支持高基数 choice。

---

## 13. 待定问题（需要实测或决策）

| # | 问题 | 影响 | 计划 |
|---|---|---|---|
| Q1 | CPU-only 容器上两个引擎的真实延迟与内存 | 决定 README 的期望管理与默认并发 | **已完成**，见 [feasibility.md §4](feasibility.md) |
| Q2 | 跨请求批处理的实际收益曲线（batch 1→8→32） | 决定 `batch.py` 的复杂度是否值得。**这是全设计最大的未验证假设** | Stage 3 实测，且是 Stage 3 的第一件事 |
| Q3 | kev 的 `rows` vs `prefix` 模式在不同 state 分布下的权衡 | 决定默认 `DECIS_BATCH_MODE`。注意 §6.3 的修正：`prefix` 的适用面比初版设想的窄 | Stage 3 实测 |
| Q4 | CPU 上 `torch.set_num_threads` 与进程数的组合 | 影响吞吐 2–3× | 部分完成（单进程线程扫描已完成）；多进程组合 Stage 3 实测 |
| Q5 | 是否需要支持 Qwen3 世代的 kev 模型作为回退 | Qwen3.5 混合架构在 CUDA 上需要 `flash-linear-attention`+triton，是部署脆弱点；**CPU 上实测 1.66 s/请求（fp32）**，已不适合"轻量高频"定位 | 先支持 0.8b；CPU 镜像把它标为"功能可用、性能不达标"；CUDA 实测后再定 |
| Q6 | README 用英文还是中文 | 影响受众 | **已决定**：英文为默认 + `README.zh-CN.md`，互链切换 |
| Q7 | 认证方案（单 key / 多 key / 是否需要 JWT 或按 key 的配额） | 影响多租户可用性 | ✅ Stage 0 已做 `DECIS_API_KEY`/`DECIS_API_KEYS`（常数时间比较、401/403 分工）；多租户配额留到有真实需求时 |
| Q8 | `confidence` 是否要提供"引擎原生"与"统一"两个可选口径 | 影响可移植性与标定准确性（api-compatibility §7） | ✅ Stage 0 固定统一口径 + 始终暴露 `decis.native_confidence`；若用户反馈强烈再加开关 |
| Q7 | 是否接受 `laya` 作为硬依赖（而非 vendor） | 上游 API 稳定性 | 先用 PyPI + 契约测试守卫 |

---

## 14. 与 kev 的关系（诚实说明）

kev 已经实现了 jev 兼容的 `/v1/systemone`，Decis 与它的关系必须说清楚，否则会被认为是重复造轮子：

| | kev | Decis |
|---|---|---|
| 定位 | 一个模型家族的**研究项目** | 多模型家族的**服务框架** |
| 引擎数 | 1（自己的 checkpoint） | N（可插拔） |
| 契约 | 兼容，但有 §7 表中的若干偏差 | 严格按 OpenAPI 生成物对齐 |
| 批处理 | 单请求串行（一个 `threading.Lock`） | 跨请求攒批 |
| 打包 | 无 Dockerfile、无镜像 CI | 多引擎多架构镜像矩阵 |
| 引擎来源 | 自训练 | 复用社区权重（kev、Laya），并可转发真 jev |

Decis 会**复用 kev 的推理内核**（在许可与署名前提下），并把它从"我的模型"推广到"任何决策模型"。这不是替代，是承接。
