# API 参考

[English](api.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [快速开始](getting-started.zh-CN.md) · [线格式契约](api-compatibility.md)

Decis 实现了 TypeSafe System One API。本页是实用参考：发什么、返回什么、错误分别是什么意思。
机器可读的定义在 [`docs/schema/`](schema/)，每条契约决策背后的证据在
[`api-compatibility.md`](api-compatibility.md)。

## Base URL 与版本

| | |
|---|---|
| Base URL | 自托管时为 `http://<host>:<port>` |
| 端点 | `POST /v1/systemone`、`GET /v1/models`、`GET /healthz`、`GET /readyz` |
| 内容类型 | `application/json` |
| 认证 | `Authorization: Bearer <token>` |

只有一个 API 版本 `v1`，它是 Decis 与托管 API 共享的契约。服务端自身的版本只在 `/healthz`
里。响应里的 `decis` 命名空间带的是 `engine_version`，那是上游模型包的版本，与 Decis 自己的
版本各自独立。两者都不改变线格式。

## 认证

每个 `/v1/*` 端点都需要 Bearer token。这个区分很重要，客户端应当据它分支：

| 请求 | 响应 |
|---|---|
| 没有 `Authorization` 头，或 scheme 不是 `Bearer` | **403** |
| `Bearer` token 与 `DECIS_API_KEY` 不匹配 | **401** |

```bash
curl -s localhost:8000/v1/models -H 'authorization: Bearer local'
```

认证先于请求体校验，所以未认证且请求体非法的请求返回 403，而不是 422。`/healthz` 与
`/readyz` 不需要认证：探针没有 token。

## 请求头

| 请求头 | 是否必填 | 说明 |
|---|---|---|
| `Authorization` | 是 | `Bearer <token>` |
| `Content-Type` | POST 时必填 | `application/json` |
| `Accept` | 否 | JSON 是唯一的表示形式 |
| `X-TypeSafe-Retry-Count` | 否 | 官方 SDK 重试时会带上它。Decis 会记录它——客户端在重试意味着服务端在失败或太慢。 |

## 响应头

| 响应头 | 出现于 | 说明 |
|---|---|---|
| `x-typesafe-request-id` | **每个响应**，错误响应也不例外 | `req_` + 32 位小写十六进制。报问题时请附上它；同一个 id 也在服务端日志里。 |
| `retry-after-ms` | 429 | 需要等待多久，单位毫秒。同时以秒为单位发送 `retry-after`。 |
| `retry-after` | 429，以及加载期间的 503 | 加载中的 503 值得重试；**加载失败**的 503 不值得，且不带 `retry-after`。 |
| `www-authenticate` | 401 | `Bearer error="invalid_token"`。托管 API 不带它；Decis 遵循 RFC 9110。 |

官方 SDK 的成功响应模型在缺少 `x-typesafe-request-id` 时会**抛异常**，所以每个响应都带它，
包括 4xx 与 5xx。

## 端点

### `POST /v1/systemone`

针对一段内容提出任意数量的带类型问题。

```bash
curl -s localhost:8000/v1/systemone \
  -H 'authorization: Bearer local' -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "jev-latest",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "urgency": {"type": "score", "instructions": "How urgent is this?",
                "criteria": ["no deadline", "this week", "today", "already late"]},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

请求体是一个 JSON 对象，包含三个必填字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `state` | string \| object \| array | 需要推理的内容。仅支持文本——不支持图片、音频或视频。 |
| `model` | string | 用哪个模型。`jev-latest`（官方 SDK 的默认值）会解析到服务端实际运行的引擎；见 [模型名称](#模型名称)。 |
| `questions` | object | 至少一个条目。键由你自选；会原样返回。 |

JSON Schema 是 [`docs/schema/systemone-request.schema.json`](schema/systemone-request.schema.json)。

#### 问题原语

三种原语可以在一个请求里混用，并针对同一个 `state` 求值。

**`choice`** —— 从一组命名选项中选一个。

```jsonc
{
  "type": "choice",
  "instructions": "Which team should handle this?",
  "criteria": {
    "billing": "invoices, payments, refunds",
    "technical": "bugs, outages, system errors",
    "sales": "pricing, new contracts"
  }
}
```

- `criteria` 必填。它的键就是作为 `choice` 返回的选项名。
- criterion 的值可以是字符串、对象、数组或 `null`（仅凭名字决定）。这个值只是给模型的描述，
  不会返回。
- 选项数量受引擎的 `max_options` 限制（见 [`/v1/models`](#get-v1models)）。空 `criteria` 对
  Schema 来说是合法请求，但引擎会返回 **422**，因为没有引擎能从零个选项里选。

**`score`** —— 按有序评分标准打分。

```jsonc
{
  "type": "score",
  "instructions": "How urgent is this?",
  "criteria": ["no deadline", "this week", "today", "already late"]
}
```

- `criteria` 是必填的非空**数组**，从最低到最高排列。
- 答案的 `legend` 把字符串键 `"0"`、`"1"`、… 映射到你的评分标准条目，`probabilities` 使用
  同样的字符串键。
- `score` 是期望档位，即在 0 基档位上求 `Σ k · pₖ`，所以可以是小数。用上面的评分标准，
  `2.4` 表示“介于 today 与 already late 之间”。

**`noul`** —— 一个是/否问题。

```jsonc
{
  "type": "noul",
  "instructions": "Does the user threaten to cancel?",
  "criteria": { "true": "explicitly threatens to cancel", "false": "does not" }
}
```

- `criteria` 可选；两半都可选。如果发送它，键必须恰好是 `"true"` 与 `"false"`——
  **其他任何键都会被静默忽略**，答案在不含它的情况下算出。这是底层 Schema 的行为，
  不是 Decis 的选择。
- 答案是单个标量 `noul` = P(true)，没有 `confidence`，也没有 `probabilities`。

#### 响应

```jsonc
{
  "model": "decis/laya-multilingual@0.3.6",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.87,
                    "probabilities": { "billing": 0.87, "technical": 0.13 } },
    "urgency":    { "type": "score", "score": 2.4, "confidence": 0.71,
                    "legend": { "0": "no deadline", "1": "this week", "2": "today", "3": "already late" },
                    "probabilities": { "0": 0.02, "1": 0.11, "2": 0.45, "3": 0.42 } },
    "churn_risk": { "type": "noul", "noul": 0.62 }
  },
  "usage": { "input_tokens": 128, "output_tokens": 3 },
  "decis": { "engine": "laya-multilingual", "engine_version": "0.3.6", "device": "cpu",
             "dtype": "float32", "latency_ms": 231.4, "batch_size": 1 }
}
```

上面样例里的 `@0.3.6` 是抓它们时装的 `laya` 构建，不是 Decis 的版本：`engine_version` 取的是
`importlib.metadata.version("laya")`（`src/decis/engines/laya.py`），跟着镜像或本机 extra 装到的
那个上游版本走（`laya<0.4`）。

JSON Schema 是
[`docs/schema/systemone-response.schema.json`](schema/systemone-response.schema.json)。
每个响应都成立的保证：

- `answers` 的键与 `questions` 完全相同。question 的键由你自选，永远不会发给模型，
  所以两个问题的措辞可以完全相同而不会被混淆。
- `model` 是实际作答的**版本化** id（`decis/<engine>@<version>`），不是你请求里的别名。
- `choice` 等于 `argmax(probabilities)`，且 `choice.probabilities` 的键与顺序和你的
  `criteria` 完全一致。
- `noul` 是标量，既没有 `confidence` 也没有 `probabilities`。
- `score` 等于在 `legend` 的 0 基档位上求 `Σ k · pₖ`。
- `usage.input_tokens` 与 `usage.output_tokens` 是必填整数。它们是计费口径而不是生成长度：
  这些模型不生成文本，`output_tokens` 是序列化后的 `answers` 对象的 token 数，不是答案的数量。

#### `decis` 命名空间

Decis 在契约之外增加的一切都放在同一个键下，所以官方 SDK（它的模型会忽略未知字段）会把它们
全部接受，这里的东西也不会被误认为契约字段。

| 字段 | 含义 |
|---|---|
| `engine` | 引擎 id，例如 `laya-multilingual`。 |
| `engine_version` | 上游模型包的版本。 |
| `device` | `cpu`、`cuda` 或 `mps`。 |
| `dtype` | `float32`、`float16` 或 `bfloat16`。 |
| `latency_ms` | 本次请求在服务端的推理耗时。 |
| `batch_size` | 一次前向里算了多少个问题。`1` 表示没有发生批处理。 |
| `requested_model` | 仅当你点了一个服务端没有的模型名（通常是 `jev-latest`），而它用自己的引擎作答时才出现。这个替换从不静默发生。 |
| `native_confidence` | 引擎自己的逐问题置信度，前提是它有标定过的值。契约层的 `confidence` 是 Decis 自己的公式，可跨引擎比较；这个字段保留引擎的原生值，而后者不可跨引擎比较。 |

### `GET /v1/models`

列出本服务端能运行的模型。需要认证。

```bash
curl -s localhost:8000/v1/models -H 'authorization: Bearer local'
```

```jsonc
{
  "models": [
    {
      "name": "decis/laya-multilingual@0.3.6",
      "description": "…",
      "release_date": "…",
      "decis": {
        "engine": "laya-multilingual",
        "version": "0.3.6",
        "primitives": ["choice", "noul", "score"],
        "max_options": 255,
        "max_question_tokens": 192,
        "max_sequence_tokens": 512,
        "max_state_tokens": 0,
        "device": "cpu",
        "dtype": "float32"
      }
    }
  ]
}
```

- 每一个注册过的引擎都会被列出，不管这个镜像能不能跑它。`version` 是将要执行前向的上游包的
  版本，该包缺失时为 `not-installed`——缺*依赖包*不会让引擎消失，缺*权重*同样不会。
- `dtype` 在引擎加载前是 `unloaded`；`device` 不能当作"是否已加载"的判断依据：有的引擎在加载
  前就报出设备（`kev-0.8b` 报 `cpu`），有的空闲时报 `unloaded`（Laya）。
- 这里的 `max_question_tokens` 与 `max_sequence_tokens` 是 Laya 为"没有声明上限的 checkpoint"
  准备的兜底值（`src/decis/engines/laya.py` 的 `DEFAULT_HEAD_MAX_LEN` / `DEFAULT_MAX_LEN`）——
  尚未加载的引擎报的就是它们。加载完成后这一项会被换成 checkpoint 自己声明的上限，所以请等
  `/readyz` 变绿之后再读这一行。
- `languages` 是 checkpoint 声明的语言覆盖范围。`aliases` 是响应里就有的字段，但今天出厂的
  引擎都报空列表；服务端真正会应答的名字由 `uv run decis models` 打印。
- 额外字段只允许出现在 `decis` 命名空间里，容量信息放在那里而不是顶层，就是这个原因。
- `max_state_tokens: 0` 表示只有序列上限这一条。`kev-0.8b` 会设置它（384），因为它把 `state`
  单独设了上限，与 `state + question` 分开。

### `GET /healthz` 与 `GET /readyz`

两者都不需要认证。

```jsonc
// GET /healthz -> 进程一起来就 200，加载期间也一样
{"status": "ok", "version": "0.3.2"}

// GET /readyz -> 引擎能作答后 200
{"status": "ready", "engine": "laya-multilingual"}

// GET /readyz -> 加载期间 503，带 retry-after: 1
{"status": "loading", "engine": "laya-multilingual"}

// GET /readyz -> 加载失败时 503，不带 retry-after
{"status": "failed", "engine": "laya-multilingual", "error": "…"}
```

存活探针用 `/healthz`，就绪探针用 `/readyz`。`failed` 的加载在进程被替换前是终态，
所以不要对它重试。

## 错误

响应体有三种形状，客户端必须都能处理。

**除校验之外的一切** —— 认证、过载、故障：

```jsonc
{"detail": {"error_type": "authentication_error", "message": "…"}}
```

**请求校验**，包括引擎容量失败：

```jsonc
{"detail": [{"loc": ["body", "questions", "department"], "msg": "…", "type": "too_long"}]}
```

**路径不存在或方法不对**：这两类由路由在任何 Decis 代码之前直接答复，所以 `detail` 是字符串。
request id 头仍然会带上。

```jsonc
// GET /nope -> 404
{"detail": "Not Found"}
// GET /v1/systemone、POST /v1/models -> 405
{"detail": "Method Not Allowed"}
```

| 状态码 | `error_type` | 何时 |
|---|---|---|
| **401** | `authentication_error` | Bearer token 无效。 |
| **403** | `authentication_error` | 没有凭证，或 scheme 不是 `Bearer`。 |
| **404** | —（`detail` 是字符串） | 路径不存在，由路由答复。 |
| **405** | —（`detail` 是字符串） | 路径存在但方法不对，由路由答复。 |
| **413** | `request_too_large` | 请求体超过 `DECIS_MAX_REQUEST_BYTES`（默认 2 MiB）。 |
| **422** | —（`detail` 是列表） | JSON 形状不合法，或 Decis 无法满足的请求：选项过多、问题过长、`state` 超出引擎预算、`criteria` 为空、`model` 未知。 |
| **429** | `rate_limit_error` | 等待引擎超过了 `DECIS_REQUEST_TIMEOUT_MS`。会带 `retry-after-ms`。已经开始的前向无法中断，所以这只覆盖排队——没有单独的 504。 |
| **500** | `engine_error` | 引擎抛异常。消息里给出引擎名和异常；request id 在响应头和日志里。 |
| **503** | `engine_unavailable` | 引擎未加载，或加载失败。 |

容量相关的 422 会指出字段和上限，例如
`Question 'placement' is about 207 tokens, over this model's limit of 192 per question. Shorten the instructions or the criteria descriptions.`
Decis 会拒绝超预算的请求而不是截断它，因为被截断的输入产生的是一个自信的错误答案，
而不是一个错误。

## 模型名称

`model` 字段必填。接受两类值：

- **“你的默认值”这类名字**，例如 `jev-latest`——由服务端运行的引擎作答。这正是官方 SDK
  只改 `base_url` 就够用的原因。替换会在 `decis.requested_model` 里回报。设置
  `DECIS_ACCEPT_FOREIGN_DEFAULTS=0` 可以关闭替换，改为返回 422。
- **版本化 id**，例如 `decis/laya-multilingual@0.3.6`，由 `/v1/models` 返回。运行该引擎的
  服务端会作答；运行其他引擎的服务端返回 422。

服务端也会应答每个引擎的别名：`laya-multilingual` 的 `laya-multi`，`kev-0.8b` 的 `kev` 或
`kev-latest`，`laya` 的 `laya-english`。`uv run decis models` 会打印一个服务端接受的全部名字。

## 重试与幂等

官方 SDK 会对 `{408, 429, 500..599}` **以及连接错误与超时**重试，所以“请求已到达但响应
丢失”是常态。`POST /v1/systemone` 是纯函数：同一请求打到同一个模型返回同样的答案。
请求路径上不会写入任何可变的业务状态。

真要重试时，请遵守 `retry-after-ms`。SDK 会遵守；忽略它并用指数退避重试的客户端会让已经
过载的服务更糟。

## 官方 SDK

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")
response = client.system_one(state=..., questions={...})
```

其他什么都不用改。[`examples/python_sdk.py`](../examples/python_sdk.py) 给出了完整的
兼容性论证，CI 会让它走真实 socket 跑一遍。

## 限制

限制是按引擎划分的，由 `/v1/models` 报告：

| 限制 | 含义 |
|---|---|
| `max_options` | 单个 `choice` 最多可以有多少个 criterion。 |
| `max_question_tokens` | 一个问题的 token 预算，含 instructions 与选项。 |
| `max_sequence_tokens` | `state` + 一个问题的 token 预算。 |
| `max_state_tokens` | `state` 单独的预算（当引擎有这一项时）。`0` 表示没有。 |

token 数是引擎用自己的 tokenizer 量出来的，不是按字符估算的。想知道一个请求是否放得下，
就把它发出去读错误——消息里有量出的数字和上限。

## 生成的 Schema

| 文件 | 内容 |
|---|---|
| [`systemone-request.schema.json`](schema/systemone-request.schema.json) | `POST /v1/systemone` 请求体 |
| [`systemone-response.schema.json`](schema/systemone-response.schema.json) | 响应外层结构与全部三种 answer 类型 |
| [`models.schema.json`](schema/models.schema.json) | `GET /v1/models` |
| [`errors.schema.json`](schema/errors.schema.json) | `{detail: {error_type, message}}` 响应体 |
| [`openapi.json`](schema/openapi.json) | 服务端完整的 OpenAPI 3.1 文档 |

它们由 `src/decis/schema.py`（线格式的唯一所在）生成，如果签入的文件不再与模型一致，
CI 就会失败：

```bash
uv run python docs/schema/export.py --check   # CI 跑这个
uv run python docs/schema/export.py --write   # 改完 schema.py 之后跑
```

## 完整的错误契约

上面每条决策的证据——哪些行为是对托管 API 实测到的、哪些来自它的 OpenAPI 文档、哪些是
Decis 自己的——都按每条结论标注了等级，记录在
[`api-compatibility.md`](api-compatibility.md)。如果你要改动本页的任何字段，先读那份文档。
