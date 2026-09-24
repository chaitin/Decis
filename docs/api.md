# API reference

**English** · [简体中文](api.zh-CN.md)

[Documentation index](../README.md#documentation) · [Getting started](getting-started.md) · [Wire contract](api-compatibility.md)

Decis implements the TypeSafe System One API. This page is the practical reference: what to
send, what comes back, and what the errors mean. The machine-readable definitions are in
[`docs/schema/`](schema/), and the evidence behind each contract decision is in
[`api-compatibility.md`](api-compatibility.md).

## Base URL and versioning

| | |
|---|---|
| Base URL | `http://<host>:<port>` when self-hosted |
| Endpoints | `POST /v1/systemone`, `GET /v1/models`, `GET /healthz`, `GET /readyz` |
| Content type | `application/json` |
| Auth | `Authorization: Bearer <token>` |

There is one API version, `v1`, and it is the contract Decis shares with the hosted API. The
server's own version is in `/healthz` only. The `decis` namespace of a response carries
`engine_version`, which is the upstream model package's version and moves independently of
Decis's own. Neither ever changes the wire shape.

## Authentication

Every `/v1/*` endpoint requires a bearer token. The distinction matters and clients should
branch on it:

| Request | Response |
|---|---|
| No `Authorization` header, or a scheme other than `Bearer` | **403** |
| A `Bearer` token that does not match `DECIS_API_KEY` | **401** |

```bash
curl -s localhost:8000/v1/models -H 'authorization: Bearer local'
```

Authentication runs before body validation, so an unauthenticated request with a malformed
body is 403, not 422. `/healthz` and `/readyz` are not authenticated: a probe has no token.

## Request headers

| Header | Required | Notes |
|---|---|---|
| `Authorization` | yes | `Bearer <token>` |
| `Content-Type` | yes for POST | `application/json` |
| `Accept` | no | JSON is the only representation |
| `X-TypeSafe-Retry-Count` | no | The official SDK sets it when retrying. Decis logs it — a client retrying means the server is failing or too slow. |

## Response headers

| Header | On | Notes |
|---|---|---|
| `x-typesafe-request-id` | **every response**, errors included | `req_` + 32 lowercase hex digits. Quote it when reporting a problem; the same id is in the server log. |
| `retry-after-ms` | 429 | How long to wait, in milliseconds. Also sent as `retry-after` in seconds. |
| `retry-after` | 429, and 503 while loading | The loading 503 is worth retrying; a 503 for a **failed** load is not, and carries no `retry-after`. |
| `www-authenticate` | 401 | `Bearer error="invalid_token"`. The hosted API omits this; Decis follows RFC 9110. |

The official SDK's success model **raises** if `x-typesafe-request-id` is missing, so it is
on every response including 4xx and 5xx.

## Endpoints

### `POST /v1/systemone`

Ask any number of typed questions about one piece of content.

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

The request body is a JSON object with three required fields:

| Field | Type | Notes |
|---|---|---|
| `state` | string \| object \| array | The content to reason about. Text only — no images, audio or video. |
| `model` | string | Which model to use. `jev-latest` (the official SDK's default) resolves to the engine the server runs; see [Model names](#model-names). |
| `questions` | object | At least one entry. Keys are yours; they come back verbatim. |

The JSON Schema is [`docs/schema/systemone-request.schema.json`](schema/systemone-request.schema.json).

#### Question primitives

All three can be mixed in one request and are evaluated against the same `state`.

**`choice`** — pick one of a named set.

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

- `criteria` is required. Keys are the option names returned as `choice`.
- A criterion value may be a string, an object, an array, or `null` (decide by the name
  alone). The value is a description for the model, not a value that comes back.
- The number of options is limited by the engine's `max_options` (see
  [`/v1/models`](#get-v1models)). An empty `criteria` is a valid request to the schema and a
  **422** from the engine, because no engine can choose from nothing.

**`score`** — rate on an ordered rubric.

```jsonc
{
  "type": "score",
  "instructions": "How urgent is this?",
  "criteria": ["no deadline", "this week", "today", "already late"]
}
```

- `criteria` is a required, non-empty **array**, ordered from lowest to highest.
- The answer's `legend` maps the string keys `"0"`, `"1"`, … to your rubric entries, and
  `probabilities` uses the same string keys.
- `score` is the expected level, `Σ k · pₖ` over 0-based levels, so it can be fractional.
  With the rubric above, `2.4` means "between today and already late".

**`noul`** — a yes/no question.

```jsonc
{
  "type": "noul",
  "instructions": "Does the user threaten to cancel?",
  "criteria": { "true": "explicitly threatens to cancel", "false": "does not" }
}
```

- `criteria` is optional; both halves are optional. If you send it, the keys must be exactly
  `"true"` and `"false"` — **any other key is ignored silently** and the answer is computed
  without it. That is the underlying schema's behaviour, not a Decis choice.
- The answer is a single scalar `noul` = P(true), with no `confidence` and no
  `probabilities`.

#### Response

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

The `@0.3.6` in the samples above is the upstream `laya` build they were captured with, not a
Decis version: `engine_version` is `importlib.metadata.version("laya")`
(`src/decis/engines/laya.py`), so it follows whichever release the image or the local extra
installed (`laya<0.4`).

The JSON Schema is
[`docs/schema/systemone-response.schema.json`](schema/systemone-response.schema.json).
Guarantees that hold on every response:

- `answers` has exactly the same keys as `questions`. Question keys are yours and are never
  sent to the model, so two questions may be worded identically without being confused.
- `model` is the **versioned** id that actually answered (`decis/<engine>@<version>`), not
  the alias you asked for.
- `choice` equals `argmax(probabilities)`, and `choice.probabilities` has the same keys, in
  the same order, as your `criteria`.
- `noul` is a scalar and has neither `confidence` nor `probabilities`.
- `score` equals `Σ k · pₖ` over the 0-based levels in `legend`.
- `usage.input_tokens` and `usage.output_tokens` are required integers. They are a billing
  convention rather than a generation length: these models do not generate text, and
  `output_tokens` is the token count of the serialised `answers` object, not a number of
  answers.

#### The `decis` namespace

Everything Decis adds beyond the contract lives under one key, so the official SDK — whose
models ignore unknown fields — accepts all of it and nothing here can be mistaken for a
contract field.

| Field | Meaning |
|---|---|
| `engine` | Engine id, e.g. `laya-multilingual`. |
| `engine_version` | The upstream model package's version. |
| `device` | `cpu`, `cuda` or `mps`. |
| `dtype` | `float32`, `float16` or `bfloat16`. |
| `latency_ms` | Server-side inference time for this request. |
| `batch_size` | How many questions were run in one forward pass. `1` means no batching happened. |
| `requested_model` | Set only when you named a model the server does not have — typically `jev-latest` — and it answered with the engine it runs. The substitution is never silent. |
| `native_confidence` | The engine's own confidence per question, when it has a calibrated one. The contract-level `confidence` is Decis's own formula and is comparable across engines; this preserves the engine's, which is not. |

### `GET /v1/models`

List the models this server can run. Requires authentication.

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

- Every registered engine is listed, whether or not this image can run it. `version` is the
  version of the upstream package that will run the forward pass, or `not-installed` when
  that package is missing — a missing *package* does not hide an engine, and neither does a
  missing *checkpoint*.
- `dtype` is `unloaded` until the engine has loaded, and `device` is not a load test: some
  engines report their device before loading (`kev-0.8b` reports `cpu`), others report
  `unloaded` while they are idle (Laya).
- `max_question_tokens` and `max_sequence_tokens` here are the fallbacks Laya declares for a
  checkpoint that names no limits (`DEFAULT_HEAD_MAX_LEN`, `DEFAULT_MAX_LEN` in
  `src/decis/engines/laya.py`) — which is what an engine that has not loaded reports. Once it
  has loaded, the entry is replaced with the limits its checkpoint declares, so read this row
  after `/readyz` turns green.
- `languages` is the language coverage the checkpoint declares. `aliases` is part of the
  payload, but the engines that ship today report an empty list; the names a server actually
  answers to are the ones `uv run decis models` prints.
- Extra fields are allowed in the `decis` namespace only, which is why the capacities are
  there and not at the top level.
- `max_state_tokens: 0` means the sequence limit is the only one. `kev-0.8b` sets it (384),
  because it caps `state` separately from `state + question`.

### `GET /healthz` and `GET /readyz`

Neither requires authentication.

```jsonc
// GET /healthz -> 200 as soon as the process is up, even mid-load
{"status": "ok", "version": "0.3.0"}

// GET /readyz -> 200 once the engine can answer
{"status": "ready", "engine": "laya-multilingual"}

// GET /readyz -> 503 while loading, with `retry-after: 1`
{"status": "loading", "engine": "laya-multilingual"}

// GET /readyz -> 503 when the load failed, with NO retry-after
{"status": "failed", "engine": "laya-multilingual", "error": "…"}
```

Use `/healthz` for liveness and `/readyz` for readiness. A `failed` load is terminal until
the process is replaced, which is why it does not invite a retry.

## Errors

Three body shapes exist, and clients must handle all three.

**Everything except validation** — auth, overload, faults:

```jsonc
{"detail": {"error_type": "authentication_error", "message": "…"}}
```

**Request validation**, including engine-capacity failures:

```jsonc
{"detail": [{"loc": ["body", "questions", "department"], "msg": "…", "type": "too_long"}]}
```

**Unknown path or wrong method**, answered by the router before any Decis code runs, so
`detail` is a plain string. The request id header is still set.

```jsonc
// GET /nope -> 404
{"detail": "Not Found"}
// GET /v1/systemone, POST /v1/models -> 405
{"detail": "Method Not Allowed"}
```

| Status | `error_type` | When |
|---|---|---|
| **401** | `authentication_error` | The bearer token is not valid. |
| **403** | `authentication_error` | No credential, or a scheme other than `Bearer`. |
| **404** | — (`detail` is a string) | Unknown path, answered by the router. |
| **405** | — (`detail` is a string) | Known path, wrong method, answered by the router. |
| **413** | `request_too_large` | Body over `DECIS_MAX_REQUEST_BYTES` (2 MiB by default). |
| **422** | — (`detail` is a list) | Bad JSON shape, or a request Decis cannot honour: too many options, an over-long question, `state` over the engine's budget, an empty `criteria`, an unknown `model`. |
| **429** | `rate_limit_error` | Waiting for the engine exceeded `DECIS_REQUEST_TIMEOUT_MS`. Carries `retry-after-ms`. A forward pass that has already started cannot be interrupted, so this covers queueing only — there is no separate 504. |
| **500** | `engine_error` | The engine raised. The message names the engine and the exception; the request id is in the header and the log. |
| **503** | `engine_unavailable` | The engine is not loaded, or the load failed. |

A 422 for capacity names the field and the limit, for example
`Question 'placement' is about 207 tokens, over this model's limit of 192 per question. Shorten the instructions or the criteria descriptions.`
Decis rejects an over-budget request rather than truncating it, because a truncated input
produces a confident wrong answer instead of an error.

## Model names

The `model` field is required. Two kinds of value are accepted:

- **A "your default" name** such as `jev-latest` — answered by the engine the server runs.
  This is what makes changing `base_url` sufficient for the official SDK. The substitution
  is reported in `decis.requested_model`. Set `DECIS_ACCEPT_FOREIGN_DEFAULTS=0` to turn the
  substitution off and get a 422 instead.
- **A versioned id** such as `decis/laya-multilingual@0.3.6`, as returned by `/v1/models`.
  A server running that engine answers it; a server running a different engine returns 422.

A server also answers each engine's aliases: `laya-multi` for `laya-multilingual`, `kev` or
`kev-latest` for `kev-0.8b`, `laya-english` for `laya`. `uv run decis models` prints the full
list of names a server accepts.

## Retries and idempotency

The official SDK retries on `{408, 429, 500..599}` **and on connection errors and
timeouts**, so "the request arrived but the response was lost" is a normal event. `POST
/v1/systemone` is a pure function: the same request against the same model returns the same
answer. Nothing on the request path writes mutable business state.

When you do retry, honour `retry-after-ms`. The SDK does; a client that ignores it and
retries with exponential backoff makes an overloaded server worse.

## Official SDK

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")
response = client.system_one(state=..., questions={...})
```

Nothing else changes. [`examples/python_sdk.py`](../examples/python_sdk.py) is the whole
compatibility argument, and CI runs it against a real socket.

## Limits

Limits are per engine and are reported by `/v1/models`:

| Limit | Meaning |
|---|---|
| `max_options` | Most criteria a single `choice` may have. |
| `max_question_tokens` | Token budget for one question, instructions and options included. |
| `max_sequence_tokens` | Token budget for `state` + one question. |
| `max_state_tokens` | A separate budget for `state` alone, when the engine has one. `0` means none. |

Token counts are measured with the engine's own tokenizer, not estimated from characters. If
you need to know whether a request fits, send it and read the error — the message contains
the measured number and the limit.

## Generated schemas

| File | Contents |
|---|---|
| [`systemone-request.schema.json`](schema/systemone-request.schema.json) | The `POST /v1/systemone` request body |
| [`systemone-response.schema.json`](schema/systemone-response.schema.json) | The response envelope and all three answer types |
| [`models.schema.json`](schema/models.schema.json) | `GET /v1/models` |
| [`errors.schema.json`](schema/errors.schema.json) | The `{detail: {error_type, message}}` body |
| [`openapi.json`](schema/openapi.json) | The server's full OpenAPI 3.1 document |

They are generated from `src/decis/schema.py` — the wire format's single home — and CI fails
if a checked-in file no longer matches the models:

```bash
uv run python docs/schema/export.py --check   # what CI runs
uv run python docs/schema/export.py --write   # after changing schema.py
```

## Error contract, in full

The evidence for each decision above — which behaviours were observed against the hosted
API, which come from its OpenAPI document, and which are Decis's own — is recorded with a
level per claim in [`api-compatibility.md`](api-compatibility.md). If you are changing any
field on this page, read that first.
