# 线上观测记录：api.typesafe.ai

**日期**：2026-09-22
**目的**：把 `api-compatibility.md` 里原先"读 schema 声明"得出的结论，换成"真的发请求、真的看响应"得出的结论。
**被测**：`https://api.typesafe.ai`（经 HTTP 代理，实际协议 HTTP/2）

这几条观测推翻或补充了初版契约，是 [`api-compatibility.md §5`](../api-compatibility.md) 的直接依据。**保留原始命令与原始输出**，不要只留结论——结论会过时，原始记录不会。

---

## 1. 权威 schema 可公开获取

```
$ curl -s -o typesafe-openapi.json -w '%{http_code} %{size_download}\n' https://api.typesafe.ai/openapi.json
200 14158
```

- OpenAPI **3.1.0**，`info.title = "TypeSafe"`，`info.version = **0.2.0**`
- sha256 `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5`
- 已收进仓库：`docs/contract/typesafe-openapi-0.2.0.json`

`paths` 只有两个：`/v1/systemone`（post）、`/v1/models`（get）。两者都声明 `security: [{HTTPBearer: []}]`。

**关键发现**：OpenAPI 只文档化了 **200 与 422** 两种响应。**401、403、404、405、429 全部未文档化**——它们只存在于运行时。这正是必须做线上观测的原因。

组件 schema 的 `required` 集合：

```
Answer                   (oneOf, 无 required)
ChoiceAnswer             ['choice', 'confidence', 'probabilities', 'type']
ChoiceQuestion           ['criteria', 'type']
HTTPValidationError      (无 required)
ModelMetadata            ['name', 'description', 'release_date']
ModelMetadataList        ['models']
NoulAnswer               ['noul', 'type']          <- 无 confidence
NoulCriteria             (无 required)              <- true/false 都可选
NoulQuestion             ['type']
Question                 (oneOf, 无 required)
ScoreAnswer              ['score', 'confidence', 'legend', 'probabilities', 'type']
ScoreQuestion            ['criteria', 'type']
SystemOneRequest         ['model', 'questions', 'state']
SystemOneResponse        ['model', 'answers', 'usage']
Usage                    ['input_tokens', 'output_tokens']   <- 非空整数
ValidationError          ['loc', 'msg', 'type']
```

值得单独记的两点：

- `SystemOneRequest.questions` 有 `minProperties: 1`；`ScoreQuestion.criteria` 有 `minItems: 1`；**`ChoiceQuestion.criteria` 没有任何 `minProperties`** —— 空对象 `{}` 是合法请求。这是官方 schema 自身的不一致，Decis 必须显式补上（见契约 §3.2）。
- `ScoreAnswer.legend` / `probabilities` 在 OpenAPI 里只是 `additionalProperties: {string|number}`，**没有把键约束成整数或数字字符串**。"键必须是 `"0"`、`"1"`…" 这条来自官方 SDK 的 Pydantic 模型（`ScoreAnswer.probabilities: dict[int, float]`），是**客户端**约束，不是线格式约束。

---

## 2. 错误契约（初版写错的地方）

用的探测函数（每条打印状态行、关键响应头、body）：

```bash
B=https://api.typesafe.ai
probe () { desc="$1"; shift; echo "### $desc"; \
  curl -s -D /tmp/h.txt -o /tmp/b.txt -m 25 "$@"; \
  grep -iE '^(HTTP/|x-typesafe|www-authenticate|retry-after|content-type)' /tmp/h.txt; \
  echo "body: $(head -c 400 /tmp/b.txt)"; }
```

### 2.1 无 `Authorization` 头

```
### GET /v1/models  (no auth)
HTTP/2 403
content-type: application/json
x-typesafe-request-id: req_01a0c86078e37a7f88f1bee4694b4f1e
body: {"detail":{"error_type":"authentication_error","message":"Must supply an API key! Check your request and try again."}}
```

**403，不是 401。** 初版写的是 401，错了。

### 2.2 `Authorization: Bearer <无效 key>`

```
### GET /v1/models  (Bearer garbage)
HTTP/2 401
content-type: application/json
x-typesafe-request-id: req_01a0c86090ab7dc48498e0e6b474bba1
body: {"detail":{"error_type":"authentication_error","message":"Cannot authenticate with the server. Please check your API key and try again."}}
```

**401。** 与 2.1 合起来：**缺凭证 → 403，凭证无效 → 401**。与 RFC 9110 的分工一致。

### 2.3 scheme 不是 Bearer

```
### GET /v1/models  (malformed scheme: "Authorization: garbage")
HTTP/2 403
body: {"detail":{"error_type":"authentication_error","message":"Must supply an API key! Check your request and try again."}}
```

与"无头"同码同体——说明它按"未提供可用凭证"处理。

### 2.4 认证先于校验

```
### POST /v1/systemone  (no auth, INVALID body {"bogus":1})
HTTP/2 403
body: {"detail":{"error_type":"authentication_error","message":"Must supply an API key! ..."}}
```

请求体是**非法**的（缺 `model`/`state`/`questions`），但因为没有 key，返回的是 403 而不是 422。**认证在请求体校验之前**。这是安全上正确的顺序，Decis 必须一致，否则会向未认证调用方泄露校验细节。

### 2.5 方法/路径错误用 FastAPI 默认形状

```
### GET /v1/systemone      -> HTTP/2 405  body: {"detail":"Method Not Allowed"}
### POST /v1/models        -> HTTP/2 405  body: {"detail":"Method Not Allowed"}
### GET /v1/nope           -> HTTP/2 404  body: {"detail":"Not Found"}
```

三条**都带** `x-typesafe-request-id`。

### 2.6 结论：`detail` 是多态的

| body | 出现于 | 形状 |
|---|---|---|
| `{"detail": {"error_type", "message"}}` | 401 / 403 | **对象** |
| `{"detail": [ValidationError, …]}` | 422 | **数组** |
| `{"detail": "Method Not Allowed"}` / `{"detail": "Not Found"}` | 404 / 405 | **字符串** |

官方 SDK 的 `errors.py:extract_message()` 显式枚举了全部这些分支（包括嵌套的 `detail.message`），证明多态是**有意的设计**，不是偶然。

### 2.7 request id 的格式

四条 2.5/2.1 的观测都是 `req_` + 32 位小写十六进制，例如 `req_01a0c860fb35716a8c61382985cd2fa9`。前 8 位很像秒级时间戳——与 ULID 的布局一致。**Decis 按 `req_[0-9a-f]{32}` 生成即可**，不必实现完整 ULID，但保持长度与字符集一致，以免依赖该头做解析的客户端出问题。

### 2.8 RFC 层面的偏差

401 响应**没有** `WWW-Authenticate` 头。RFC 9110 §15.5.2 规定 401 **必须**带。这是上游的一处标准违背。

Decis 的决定：**补上 `WWW-Authenticate: Bearer`**。它是纯增量的（官方 SDK 不读该头），且兼容性目标是"客户端可移植"而不是"复刻服务端缺陷"。这处有意偏离记在契约 §5.2-④。

---

## 3. 未能观测的部分（诚实记录）

| 项 | 原因 |
|---|---|
| 429 与 `Retry-After` | 没有有效 API key，无法触发限流。SDK 会读 `retry-after-ms`/`Retry-After` 是**代码证据（A 级）**，不是观测证据 |
| 529 | 未触发，且 OpenAPI 未声明 |
| 200 响应体的真实取值 | 无 key。响应**形状**由 OpenAPI（S 级）+ SDK 模型（A 级）+ kev 的实测输出（C 级）三方交叉确认 |
| 真实容量限制（64k tokens、250k tok/s、1200 req/min） | 文档散文（B 级），未实测 |

**这几项要等有了 `TYPESAFE_API_KEY` 才能补。** 在补齐之前，契约里相应条目保持 B/A 级标注，不得升格。
