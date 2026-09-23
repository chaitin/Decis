# jev / TypeSafe System One 兼容性契约

Decis 对外只暴露**一套**接口，其语义目标是"把 `base_url` 从 `https://api.typesafe.ai` 换成 Decis 后，官方 SDK 与官方文档示例都能原样工作"。

本文是这套契约的**唯一事实来源（single source of truth）**。任何代码改动若与本文冲突，以本文为准；确需变更契约时，先改本文并附证据。

---

## 1. 证据等级

写契约最怕"猜字段名"。本文对每条结论标注证据等级：

| 等级 | 来源 | 说明 |
|---|---|---|
| **L** | **对线上 `https://api.typesafe.ai` 的实际 HTTP 观测** | 最强证据：不是读代码或文档，而是真的发了请求、真的拿到了响应。记录见 §5 与 [`contract/observations-2026-09-22.md`](contract/observations-2026-09-22.md) |
| **S** | **官方 OpenAPI 文档本身**：`https://api.typesafe.ai/openapi.json`（OpenAPI 3.1.0，`info.version` **0.2.0**） | 已收进仓库：[`contract/typesafe-openapi-0.2.0.json`](contract/typesafe-openapi-0.2.0.json)，sha256 `a191f8a7…0360d5`。这是**机器可读、可 diff、可测**的权威来源，取代了原先只能间接依赖 SDK 生成物的做法 |
| **A** | `typesafe_sdk` 0.7.1 wheel 内的 `_schemas/models.py` | 文件头写明 `generated from https://api.typesafe.ai/openapi.json`。与 S 一致，但**是客户端视角**，且可能滞后于服务端 |
| **A** | `typesafe_sdk/_core/{transport,constants,errors,retry,response_types}.py` | 官方 SDK 实际发出的请求构造与错误解析逻辑 |
| **B** | docs.typesafe.ai 正文（`/api`、`/primitives/*`、`/confidence`、`/quickstart`、`/sdk/python`） | 官方文档散文，含示例 |
| **C** | kev（jaredpalmer/kev）的 `kev/api.py` + `tests/test_api.py` | 第三方实现旁证，已用官方 SDK 实测通过 |
| **D** | Laya（ConvAI）的 `laya` PyPI 包与 HF model card | 第三方实现，schema 与 jev 高度一致但**非权威** |

**冲突时的优先级：L > S > A > B > C > D。**

> **方法说明（为什么这次要补 L 级）**：初版契约完全建立在 S/A 上——即"读服务端的 schema 声明"。这有一个结构性盲点：**它无法验证服务端是否真的按声明行事**。本次补做的线上观测立刻推翻了初版的一处结论（认证错误码，见 §5），并暴露出三类只能靠观测发现的契约（错误码与 `/v1/models` 的 `detail` 复用、认证先于校验、错误响应也带 request id）。这是"尊重标准"的必要成本：**标准写了什么 ≠ 实现做了什么**。

---

## 2. 传输层

| 项 | 值 | 证据 |
|---|---|---|
| Base URL | `https://api.typesafe.ai`（Decis 中为 `http://host:port`） | A |
| 环境变量覆盖 | `TYPESAFE_BASE_URL`、`TYPESAFE_API_KEY`、`TYPESAFE_DEFAULT_MODEL` | A |
| 端点 | `POST /v1/systemone`、`GET /v1/models` | A |
| 认证 | `Authorization: Bearer <key>` | A |
| 请求头 | `Accept: application/json`、`Content-Type: application/json`、`User-Agent`、`X-TypeSafe-SDK`、`X-TypeSafe-Runtime`、重试时加 `X-TypeSafe-Retry-Count` | A（`_core/constants.py`、`transport.py:63-66`） |
| 响应头 | `x-typesafe-request-id`，**在所有响应上都有**（含 403/404/405），格式 `req_` + 32 位小写十六进制 | **L** |
| 默认超时 | 10.0 s / 次 HTTP 操作（`DEFAULT_TIMEOUT`）；另有 `RetryPolicy.timeout=30.0` 作为**整次调用的重试总预算** | A |
| 默认模型 | `jev-latest`（`DEFAULT_MODEL`） | A |
| 重试 | `max_retries=2`，退避 0.5s 起、上限 5s、抖动 0.25，重试 `{408,429,500..599}` **+ 连接错误 + 超时**，遵守 `Retry-After` / `retry-after-ms` | A |
| 不重试的状态码 | **401 / 403 / 404 / 422** —— 认证与校验失败不会重试 | A（`retry.py:110-118`） |
| 流式 | **官方文档未说明，SDK 无流式接口** → Decis 不实现 | B |

> **工程含义 1（幂等）**：SDK 会自动重试 POST（`retry.py` 默认 `max_retries=2`），**并且连接错误与超时也会重试**——这意味着"请求已到达但响应丢失"的重发是常态，不只是失败重试。`/v1/systemone` 必须是纯函数：同一请求重复执行结果一致。Decis 不得在请求路径上做任何可变业务状态写入（计数器除外）。
>
> **工程含义 2（request id 是必需的，错误响应也不例外）**：SDK 的 `SystemOneResponse.request_id` 在响应头缺失时**抛异常**（`response_types.py:75-80`），不是返回 `None`。而 `TypeSafeAPIError.request_id` 只是可能为 `None`（`errors.py:118-120`）。线上观测确认真 jev 在 403/404/405 上也带该头。**Decis 必须在所有响应上回填它**，包括错误响应——否则用户报错时拿不到可关联的 id。
>
> **工程含义 3（重试可观测）**：SDK 重试时会带上 `X-TypeSafe-Retry-Count`。Decis 应把它记进日志——客户端在重试意味着**服务端在失败**（或响应太慢），这是最直接的告警信号。
>
> **工程含义 4（超时预算）**：官方 SDK 默认总重试预算 30 s、单次 HTTP 超时 10 s。**Decis 的任何同步等待都不得让单次请求超过 10 s**，否则 SDK 会在服务端还在算的时候放弃并重发，把负载放大。这直接约束了 §6.5 的队列等待上限。

---

## 3. 请求 schema

```jsonc
{
  "state":     "…",            // 必填：string | object | array（文本内容，不支持图片/音频/视频）
  "model":     "jev-latest",   // 必填：string
  "questions": {               // 必填：map，至少 1 个；键由调用方自选
    "<id>": { "type": "noul" | "choice" | "score", "instructions": …, "criteria": … }
  }
}
```

### 3.1 顶层字段

| 字段 | 类型 | 必填 | 备注 |
|---|---|---|---|
| `state` | `str \| dict \| list` | ✅ | OpenAPI 中**不含** `null`/`int`/`float`/`bool` 顶层标量 |
| `model` | `str` | ✅ | kev 实现给了默认值 `kev-latest`，**这是偏差**：官方 schema 必填。Decis 应必填，但为兼容 kev 客户端可接受缺省并回落到配置的默认引擎 |
| `questions` | `dict[str, Question]` | ✅ | `min_length=1`；**question id 不发送给模型**，仅用于回填 answers |

### 3.2 三种 Question

`Entry = str | dict | list`（choice 的 criteria 值还允许 `null`）。三种 question 的 `type` 是 `const`（判别式），所以它必填。

| type | `instructions` | `criteria` | 约束（来自 OpenAPI） |
|---|---|---|---|
| `noul` | 可选，可 `null` | 可选，可 `null`；`{"true": Entry, "false": Entry}` | 仅 `type` 必填；`NoulCriteria` 的 `true`/`false` **都非必填** |
| `choice` | 可选，可 `null` | **必填** `{option_name: Entry}` | **`minProperties` 未声明** —— 见下 |
| `score` | 可选，可 `null` | **必填** `[Entry, …]` 有序，位置即分值，从 0 开始 | `minItems: 1`；**无 `maxItems`** |

> **`choice.criteria` 可以是空对象 `{}`。** OpenAPI 只声明了它是 `object` 且 `additionalProperties` 有类型，**没有 `minProperties`**。也就是说 `{"type":"choice","criteria":{}}` 是**符合契约的请求**，而它要求"从零个选项里选一个"——没有良定义答案。
>
> 这正是 OpenAPI 无法表达、必须由实现层决定的地方。Decis 的处理：**在契约层接受、在引擎层以 422 拒绝**，`loc` 指向 `["body","questions","<qid>","criteria"]`，理由写"choice requires at least one criterion"。这既没有拒收合法请求（400/422 的语义是"请求本身不合法"，而它合法），也没有让引擎面对无解输入。（对比 OpenAPI 对 `questions` 顶层声明了 `minProperties: 1`、对 `score.criteria` 声明了 `minItems: 1`——**恰恰漏了这一个**，是官方 schema 的不一致，Decis 需要显式补上。）

文档中出现的 `what` / `not_for` / `examples` / `question` / `focus` / `summary` / `signals` 等键**都不是 API 的一部分**，纯粹是用户自选的描述结构，不得作为保留字处理。

### 3.3 容量限制（官方）

- 单请求 64k tokens（state + 全部问题）
- state + 最长单问 ≤ 32k tokens
- 限流：250,000 tokens/s **且** 1,200 req/min，任一超限返回 429

> **工程含义 3**：这些是 *jev* 的容量。Decis 上不同引擎的容量差异极大（例如 Laya 英文 512 上下文、单问选项预算 `head_max_len=192`；kev 单问 8192）。因此 `EngineInfo` 必须声明容量，服务端在**校验阶段**就拒绝超限请求，而不是让引擎截断后静默给出劣化答案。

---

## 4. 响应 schema

```jsonc
{
  "model": "jev-1.13.0",
  "answers": { "<id>": Answer },
  "usage": { "input_tokens": 392, "output_tokens": 65 }
}
```

### 4.1 Answer 三型（按 `type` 判别）

```jsonc
// noul —— 标量，没有 confidence，没有 probabilities
{ "type": "noul", "noul": 0.98 }

// choice —— 概率键 = 选项名
{ "type": "choice", "choice": "technical", "confidence": 0.78,
  "probabilities": { "billing": 0.15, "technical": 0.85, "sales": 0.0 } }

// score —— legend/probabilities 的键是层级序号的【字符串】
{ "type": "score", "score": 1.09, "confidence": 0.87,
  "legend": { "0": "…", "1": "…", "2": "…" },
  "probabilities": { "0": 0.0, "1": 0.91, "2": 0.09 } }
```

### 4.2 三个最容易做错的点

1. **`score` 的 `legend` / `probabilities` 键必须是字符串 `"0"`、`"1"`、`"2"`**，不是数组、不是整数。SDK 的 `ScoreAnswer.probabilities: dict[int, float]` 依赖 Pydantic 从 JSON 字符串 key 强转（`typesafe_sdk/_core/response_types.py:53-58`）。发成 JSON 数组会直接解码失败。
2. **`noul` 是标量**，不是 `{"false": …, "true": …}` 概率对。`kev/api.py:140` 内部确实按两选项算，但线格式只发 `p(true)`。
3. **`legend` 的 value 可以是 object/array**（结构化层级），不能假设为 string。同理 `choice.probabilities` 的键必须与请求 `criteria` 的键**逐字相同**（含大小写与空白）。

### 4.3 `usage`

| 字段 | 类型 | 说明 |
|---|---|---|
| `input_tokens` | `int` | 计费口径的输入 token |
| `output_tokens` | `int` | 计费口径的输出 token |

**两者在 OpenAPI 里都是必填的非空整数**（`Usage.required = ["input_tokens","output_tokens"]`，类型 `integer`）。官方 SDK 把字段放宽为 `int | None` 只是客户端容错，**不构成"可以不发"的许可**。Decis 必须始终发整数。

> **工程含义 4**：决策模型**不生成文本**，"输出 token"在 Decis 中无自然定义。kev 用"序列化 answers 的 token 数"（`kev/api.py:150-152`）作为计费式口径。Decis 采用同一口径并**在文档中明确说明它是计费口径、不是生成长度**——这是必须诚实交代的地方。实现见 `src/decis/answers.py: estimate_output_tokens`；`input_tokens` 由引擎自己的 tokenizer 报（引擎没有 tokenizer 时才退化为字符估算）。

### 4.4 `model` 回填

官方语义：*"Name of the model that answered the questions. May differ from the alias supplied in the request."* 别名会随发版移动，响应回报真正的版本化 ID（如 `jev-1.13.0`）。

Decis 据此回报自己的版本化 ID，例如 `decis/laya-multilingual@0.3.5`。这样：
- 契约合法（允许与请求别名不同）
- 排障时能立刻看出"这次请求实际由哪个引擎、哪个版本回答"
- 不会被误认为在冒充 TypeSafe

映射关系通过 `GET /v1/models` 的扩展字段暴露。

### 4.5 解码器的容错边界（SDK 实测行为）

从 `typesafe_sdk/_core/response_types.py` 与 `_core/schemas/base.py` 确认的三条，决定了 Decis 可以/不可以"加东西"：

| 行为 | 出处 | 对 Decis 的含义 |
|---|---|---|
| 顶层额外字段被**忽略**（`Schema.model_config = extra="ignore"`，`Response(Schema, _ResponseMixin)`） | `schemas/base.py:18-21,100` | 可以加 `latency_ms`、`decis` 等扩展字段；SDK 读不到但不会报错。**但扩展字段不能替代契约字段** |
| `answers` 里出现**未知的 `type`** 时，该条被**丢弃并打 warning**，不会让整个响应失败 | `response_types.py:89-93` | Decis 将来可以新增 answer 类型而不破坏旧 SDK；但也不能指望旧客户端能看见它 |
| `model` 与 `usage` 是**必填**，`answers` 可缺省为空 dict；SDK 把 `usage` 的字段放宽为 `int \| None`，但 OpenAPI 要求非空整数 | `response_types.py:70-73,99-109` + `openapi.json` | 即使引擎报不出 token 数，也必须带上 `usage` 且给出整数，不能省略或发 `null` |

---

## 5. 错误契约

**这一节在初版里是错的。** 初版按文档散文推断"认证失败 = 401"，并用 FastAPI 默认形状推断所有错误。2026-09-22 对线上 `api.typesafe.ai` 的实际观测推翻了这个推断。

### 5.1 线上实测（L 级）

| 场景 | 状态码 | Body | 备注 |
|---|---:|---|---|
| **无** `Authorization` 头，或 scheme 不是 `Bearer` | **403** | `{"detail":{"error_type":"authentication_error","message":"Must supply an API key! Check your request and try again."}}` | **不是 401** |
| `Authorization: Bearer <无效 key>` | **401** | `{"detail":{"error_type":"authentication_error","message":"Cannot authenticate with the server. Please check your API key and try again."}}` | |
| 方法不对（`GET /v1/systemone`、`POST /v1/models`） | 405 | `{"detail":"Method Not Allowed"}` | FastAPI 默认形状 |
| 路径不存在 | 404 | `{"detail":"Not Found"}` | FastAPI 默认形状 |
| 请求体校验失败 | 422 | `HTTPValidationError`（见下） | OpenAPI 中唯一被文档化的错误 |

全部响应都带 `x-typesafe-request-id`。

### 5.2 由此确定的四条契约

**① 401 与 403 有严格分工，不能混用。** 这与 RFC 9110 一致（401 = 未提供有效凭证；403 = 服务器理解但拒绝）：

- **缺少凭证或 scheme 不对 → 403**
- **提供了凭证但验证失败 → 401**

Decis 必须照此实现。只返回 401（很多 API 的做法）会让依赖状态码分支的客户端行为与 jev 不一致。官方 SDK 也确实把两者映射成**不同的异常类**（`TypeSafeAuthenticationError` / `TypeSafePermissionDeniedError`，`errors.py:150-157`），所以这不是可有可无的细节。

**② 认证先于请求体校验。** 实测：`POST /v1/systemone` 带**非法请求体**且**无 key**，返回的是 403 而不是 422。这是正确的安全顺序——Decis 必须同样先认证、再校验，否则会把"你的请求体哪里不合法"泄露给未认证调用方。

**③ `detail` 是**多态**字段，客户端必须能处理三种形状。** 这不是 Decis 的设计选择，是既有事实：

| body | 出现在 | 形状 |
|---|---|---|
| `{"detail": {"error_type": …, "message": …}}` | 401 / 403 | **对象** |
| `{"detail": [ {loc, msg, type, input?, ctx?}, … ]}` | 422 | **数组** |
| `{"detail": "Not Found"}` | 404 / 405 | **字符串** |

官方 SDK 的 `extract_message()`（`errors.py:35-63`）显式枚举了全部这些分支——包括 `{"detail":{"message":…}}`。**Decis 不需要发明错误形状**：401/403 用上面的对象形状，422 用 FastAPI 默认的数组形状，404/405 交给 FastAPI 默认处理即可。

**④ 上游有 RFC 违背之处，Decis 要决定是否跟随。** 线上 401 响应**没有 `WWW-Authenticate` 头**，而 RFC 9110 §15.5.2 要求 401 **必须**包含它。Decis 的选择：

- **加上 `WWW-Authenticate: Bearer`**。这是纯增量的（官方 SDK 不读该头，行为不变），且让 Decis 通过标准 HTTP 客户端与网关的正确性检查。
- 在本文记录这一处**有意偏离**：Decis 比 jev 更严格地遵守 RFC。这属于"兼容目标是客户端可移植，而非逐字节复刻服务端缺陷"。

### 5.3 429 与背压

- SDK 读取 `retry-after-ms`（毫秒，优先）与 `Retry-After`（秒，或 HTTP-date），并在 `respect_retry_after=True`（默认）时按其等待（`errors.py:15-32`）。
- **Decis 在返回 429 时必须带 `Retry-After`**，否则 SDK 退化为 0.5s→5s 的指数退避，会把已经过载的服务打得更狠。推荐带 `retry-after-ms` 给出毫秒级精度。
- 529 未在 OpenAPI 中声明，也未被实测触发；SDK 把任何 ≥500 归为 `TypeSafeInternalServerError` 且会重试。Decis 的过载场景**应优先用 429 + `Retry-After`**（可重试且语义准确），而不是 529 或 503。

### 5.4 422 的形状（S + A 级）

`HTTPValidationError` 在官方 OpenAPI 里是文档化的（这是唯一被文档化的错误），形状为 `{detail: [ValidationError]}`，其中 `ValidationError` 的 required 是 `["loc","msg","type"]`，`input` 与 `ctx` 可选。`loc` 示例里直接给了 `["body","questions","urgency","score","criteria"]`。

> **工程含义 5**：Decis 的引擎容量校验（超选项数、超 token 数）**必须复用这个形状**，产出 `loc: ["body","questions","<qid>","criteria"]` 这类可定位路径，而不是抛裸字符串。kev 的 `HTTPException(422, str)` 就与 SDK 期望的形状不一致——这是必须修掉的偏差。

---

## 6. `GET /v1/models`

**A 级契约**（`_schemas/models.py:53-70`）：

```jsonc
{ "models": [ { "name": "jev-latest", "description": "General-purpose system one model.", "release_date": "2026-09-15" } ] }
```

`name` 是请求 `model` 字段可接受的名称或别名；`release_date` 格式 `YYYY-MM-DD`。

> **偏差警示**：kev 的 `/v1/models` 返回 `{"models":[{"id","aliases","run","base","lora","device",…}]}`，**不含 `name`/`description`/`release_date`**，与契约不符（`kev/serve.py:122-128`）。Decis 必须按上表返回；额外字段（如 `capabilities`、`engine`、`device`）可以加，因为官方 SDK 的响应模型是 `extra="ignore"`。

---

## 7. 已知分歧与 Decis 的取舍

| # | 分歧点 | 证据冲突 | Decis 决策 | 理由 |
|---|---|---|---|---|
| 1 | `score` 层级数下限 | OpenAPI `min_length=1`（A） vs 文档散文"至少 2"（B） vs kev 用 2（C） | **接受 ≥1，不拒绝** | 拒绝一个符合 OpenAPI 的请求风险更大；文档化"≥2 才有语义" |
| 2 | `score` 层级数上限 | 文档散文"最多 10"（B） vs OpenAPI 无上限、kev 允许 255（A/C） | **不设 10 的硬上限**，由各引擎的 `EngineInfo.max_options` 决定（Laya ≤255，kev ≤255） | 10 是 jev 产品限制而非线格式限制；Decis 引擎能力不同 |
| 3 | `model` 是否必填 | OpenAPI 必填（A） vs kev 有默认值（C） | **必填** | 契约优先。官方 SDK 的默认值是 `jev-latest`，见第 6 条 |
| 4 | 响应中额外字段 | 无规定 | 允许，且集中在 `decis` 命名空间下，见下表 | SDK `extra="ignore"`，向前兼容 |
| 5 | `confidence` 公式 | **官方明确不公开**；文档称"是概率分布的统计量"，并明说用户可自行定义（B） | **由 Decis 统一定义并公开**，各引擎不各算各的 | 见下 |
| 6 | 客户端发来的 `jev-latest` | SDK 默认模型名（A，`TYPESAFE_DEFAULT_MODEL`） | **接受**，替换为服务器实际加载的引擎，并在 `decis.requested_model` 回报 | "只改 `TYPESAFE_BASE_URL` 就能跑"是项目的核心承诺；静默替换才是问题，所以必须回报 |

### 7.1 `decis` 扩展字段（Decis 实际发送的内容）

顶层只有 `decis` 一个额外键。所有这些字段都可以被官方 SDK 安全忽略（`extra="ignore"`）。

| 字段 | 含义 | 为什么在这里而不是契约里 |
|---|---|---|
| `engine` | 引擎 id，如 `laya-multilingual` | 契约只有 `model`（版本化 id）。定位"是谁答的"时 id 比版本化字符串好读 |
| `engine_version` | 引擎版本 | 便于把一条答案追溯回具体的构建 |
| `device` / `dtype` | 如 `cpu` / `float32` | 复现性能与数值差异的必要信息 |
| `latency_ms` | 本次推理耗时（毫秒） | 服务端自己测的，比客户端往返更干净 |
| `batch_size` | 本次实际一起算的问题数 | **这是批处理真的发生了的证据**。`design-review.md §2-D1` 的教训是：无法观测的批处理等于没有批处理 |
| `requested_model` | 仅当客户端点了非本服务器的模型名（如 `jev-latest`）时出现，原样回报客户端请求的字符串 | 替换必须可见 |
| `native_confidence` | 引擎自己的标定置信度，**按 question id 键控的字典**（`{"q1": 0.63, …}`）；引擎不提供时为 `null` | `noul` answer 按契约没有 confidence，这是唯一能拿到 `noul` 不确定性的地方 |

> **`native_confidence` 在 Stage 1 从标量改成了字典。** 一个请求可以同时包含 `noul`、`choice` 和 `score`，它们的原生置信度口径不同（Laya 对 `noul` 覆写成 `max(p, 1−p)`，对其余用归一化熵），一个标量无法表达。
> 另有一处值得记录的巧合：**Laya 的 `confidence_from_probs` 就是 `1 − H(p)/log k`，与 Decis 给 `choice` 选的"归一化熵"口径同源。** 于是对 Laya 而言 `decis.native_confidence == confidence`。这不改变"两个字段都存在"的决定——kev 的 `noul` 口径不同，一个会把两者混为一谈的设计在接 kev 时就会出问题——但它是对 Decis 那个选择的一次独立佐证，记录在此。

### 关于 `confidence`（重要设计决策，且是一处**权衡**）

- 官方立场：`confidence` 只是把分布压成一个数的便利量，**用户不被锁定在该定义上**（docs.typesafe.ai/confidence）。
- kev 用 `(p_max − 1/K) / (1 − 1/K)`（choice）与 `1 − E|level − mode|/(L−1)`（score），自称是近似。
- Laya 用归一化熵 `1 − H(p)/log k`，且对 `noul` 覆写成 `max(p, 1−p)`；**并且它是经过温度标定的**——Laya 出厂过度自信（ECE 0.466），包内按 `(question type, option count)` 重新拟合温度并 clamp 到 `[0.5, 5.0]`。

**必须诚实指出的事实**：这两个公式**都不是"正确"的**，它们是同一个分布的不同统计量：

- kev 的公式是 `p_max` 的单调重标定，**没有做过标定**；
- Laya 的公式背后有 ECE 意义上的标定，但**只对 Laya 自己的模型有效**。

所以不存在"统一公式对所有引擎都更准"这回事。任何跨引擎的阈值都必须在**你自己的数据上重新标定**——Laya 的 model card 自己就是这么说的。

**决策**：

1. **线格式的 `confidence` 用 Decis 统一公式**；理由不是"更准"，而是**契约需要可预测**：官方文档示例、SDK 用户代码、以及"换 base_url 就能跑"这个核心承诺，都要求同一个字段在不同引擎下是同一种东西。两个原语的公式口径统一为 **0 = 均匀/无倾向，1 = 确定**：
   - `choice`：`(p_max − 1/K) / (1 − 1/K)`（kev 那套），`K = 1` 时为 `1.0`；
   - `score`：`1 − H(p)/ln(L)`（归一化熵），`L = 1` 时为 `1.0`。
   `score` 这里**偏离了 kev**：kev 用 `1 − E|level − mode|/(L−1)`，但加上众数约束后该式在 `L=3` 的均匀分布上得 `0.5`，可达区间并非 `[0,1]`，"无倾向"却报中等置信度，无法解释。这是有意的偏离，记录在此。
2. **引擎原生的、经过标定的置信度一律保留**，放进响应的 `decis.native_confidence`。对 `noul` 尤其重要：契约规定 `noul` answer **没有** confidence 字段，所以**扩展字段是用户唯一能拿到 noul 不确定性（如 `max(p,1−p)`）的地方**。
3. 文档明确写出：**若把 `confidence` 用于风险决策，请在自己的标注集上重新标定**，不要跨引擎复用阈值。
4. **不再计划**在 `GET /v1/models` 里声明各引擎的 `confidence_formula`：公式由 Decis 统一定义（见第 1 条），本来就不因引擎而异，声明它只会让用户以为它可能会变。

> 这一条从"Decis 统一定义 confidence"（初版写法）改成了上面的形式。初版把它讲成了一个没有代价的改进，实际上它**用可比性换掉了 Laya 已经做过的标定**。诚实的表述是：这是一个取舍，且我们通过保留原生值来让用户自己选。

---

## 8. 兼容性验收

契约不是靠文档保证的，是靠测试保证的。Decis 的验收基线：

| 层级 | 测试 | 是否需要权重 |
|---|---|---|
| L0 schema 同源 | 断言 `src/decis/schema.py` 的键集合、`required`、`const`、`minItems`/`minProperties` 能从 `docs/contract/typesafe-openapi-0.2.0.json` 推导出来；OpenAPI 文件一改，测试即失败 | 否 |
| L1 形状 | §3–§6 的所有 JSON 示例往返（含 `score` 字符串键、`noul` 标量无 confidence） | 否（无权重的测试替身，`tests/fixture_engine.py`） |
| L2 语义 | 概率和为 1（容差 0.03）、`choice == argmax(probabilities)`、`score == Σ k·p_k`、`legend` 键恰为 `"0".."n-1"` | 否 |
| L3 官方客户端 | 官方 `typesafe_sdk` 的 `TypeSafeClient` / `AsyncTypeSafeClient` 跑通文档示例（含 `NoulCriteria`、结构化 `instructions`/`criteria`） | 否 |
| **L3b 错误契约** | 无 `Authorization` → **403**；`Bearer garbage` → **401**；两者 body 均为 `{"detail":{"error_type":"authentication_error","message":…}}`；**认证先于校验**（非法 body + 无 key 仍返回 403）；422 为 `{detail:[…]}`；404/405 为 `{detail:"…"}`；**所有**响应含 `x-typesafe-request-id` 且匹配 `req_[0-9a-f]{32}`；429 带 `retry-after-ms` | 否 |
| L4 官方样例逐字 | 移植 kev `tests/test_api.py` 的全部用例（它已经跑的是官方文档示例） | 是（引擎） |
| L5 差分 | 同一请求分别打到真 jev（`TYPESAFE_API_KEY`）与 Decis，断言 JSON schema **与错误码/错误体形状**一致 | 否（需网络+key，默认不进 CI） |
| L6 扩展 | `decis` 扩展字段不影响 L1–L4 | 否 |

L0–L3b、L6 进 CI；L4 进 weight-backed 夜间任务；L5 手动，但**每次改契约前必须跑一次**（`AGENTS.md §12`）。

> **L5 为什么不可省**：L0 保证"我们和自己的 OpenAPI 快照一致"，但**没有任何离线测试能发现"线上服务端偏离了它自己的 OpenAPI"**。本次 §5 的修正就是 L5 手工跑了一次的直接产物。把 L5 从"可选"提升为"改契约前必跑"，是这次审查最实质的方法论改进。

---

## 9. 明确未说明 / 不做的事

以下均**无官方说明**，Decis 选择不实现或在文档中显式标注为未定义，不臆造：

- 流式接口
- 单请求 question 数量上限（Decis 用引擎容量与请求体大小自行约束）
- 并发连接数上限、幂等键
- **请求体大小上限与 `state` 的字节上限**——OpenAPI 未声明，Decis 必须自己定（否则一个巨大 `state` 就能打爆内存），见 `design.md §6.5`
- `WWW-Authenticate` 头：线上 401 **不带**它（RFC 9110 §15.5.2 要求带）。Decis **带**它，见 §5.2-④
- `confidence` 的精确公式（官方不公开；Decis 自定义并公开，见 §7）
- 视频/图片/音频 state（决策模型只吃文本）

**已从初版"未说明"移入"已由观测确定"的**：错误 body 的确切字段名与状态码分工（§5.1）、认证与校验的顺序（§5.2-②）、request id 在错误响应上的存在与格式（§2）。
