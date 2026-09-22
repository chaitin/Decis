# Raw HTTP examples

One `curl` command per example, run against a server you started (see
[`README.md`](README.md)). Each command ends with `-w '\n%{http_code}\n'` so the status
code is visible; drop that flag for clean JSON. Every status shown below is asserted by
`tests/test_examples.py`.

The examples use `mock` because it needs no weights and answers deterministically. Point
them at a real engine by changing `"model"` — the request shape is identical for all
engines, which is the point of Decis.

```bash
BASE=http://localhost:8000
TOKEN=local            # whatever you put in DECIS_API_KEY
```

## 1. Health and readiness

`/healthz` answers as soon as the process is up. `/readyz` is 503 until the model has
finished loading, which for Laya on CPU takes ~75 seconds. Use `/healthz` for liveness and
`/readyz` for readiness — a Kubernetes `readinessProbe` pointing at `/healthz` would send
traffic to a server that cannot answer yet.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/healthz"
# -> 200
```

```json
{"status":"ok","version":"0.1.0"}
```

```bash
curl -s -w '\n%{http_code}\n' "$BASE/readyz"
# -> 200
```

```json
{"status":"ready","engine":"mock"}
```

While loading, the same call returns 503 with `retry-after: 1` — a "not yet" that is worth
retrying. A **failed** load returns 503 *without* `retry-after`, because that state is
terminal until the process is replaced; a caller told to back off from a permanently broken
engine would retry forever (`src/decis/routes.py:76-88`):

```jsonc
// still loading -- retry
{"status":"loading","engine":"laya-multilingual"}
// HTTP/1.1 503 Service Unavailable
// retry-after: 1

// terminal failure -- do not retry, alert instead
{"status":"failed","engine":"laya-multilingual","error":"..."}
// HTTP/1.1 503 Service Unavailable
```

## 2. Which models can this server run?

Requires authentication. Each engine carries its own capacity limits under the `decis` key
(extra fields are namespaced so the official SDK can ignore them); engines whose weights are
not on this machine report `not-installed` rather than disappearing, so you can tell
"not downloaded" from "not registered".

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/models" -H "authorization: Bearer $TOKEN"
# -> 200
```

```jsonc
{"models":[
  {"name":"decis/mock@0.1.0","description":"Deterministic mock engine. ...","release_date":"2026-09-22",
   "decis":{"engine":"mock","version":"0.1.0","primitives":["choice","noul","score"],
            "max_sequence_tokens":32000,"device":"cpu","dtype":"none"}},
  {"name":"decis/laya-multilingual@not-installed","description":"Laya (multilingual) ...",
   "decis":{"engine":"laya-multilingual","version":"not-installed",
            "max_sequence_tokens":512,"max_question_tokens":192,"device":"unloaded"}},
  {"name":"decis/kev-0.8b@vendored-90990a5","description":"kev: Qwen3.5 + LoRA ...",
   "decis":{"engine":"kev-0.8b","max_state_tokens":384,"languages":"English ..."}}
]}
```

> Note the difference between `device`/`dtype` for a loaded engine (`cpu`/`none`) and an
> unloaded one (`unloaded`). A `/v1/models` entry saying `unloaded` is installed but idle;
> `not-installed` in the version means the weights are missing.

## 3. The smallest possible call

One `noul` question, the answer is a single number: `P(true)`.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "The login page returns a 500 error.",
  "model": "jev-latest",
  "questions": {
    "is_bug": {"type": "noul", "instructions": "Is this a bug report?"}
  }}'
# -> 200
```

```json
{"model":"decis/mock@0.1.0",
 "answers":{"is_bug":{"type":"noul","noul":0.4725}},
 "usage":{"input_tokens":13,"output_tokens":10},
 "decis":{"engine":"mock","engine_version":"0.1.0","device":"cpu","dtype":"none",
          "latency_ms":0.08,"batch_size":1,"requested_model":"jev-latest"}}
```

Two things worth noticing:

- `"model": "jev-latest"` was accepted even though no engine is called that. Decis serves
  the jev wire format, so a client already sending jev's model name keeps working; the
  engine that actually answered is in `decis.engine`, and `decis.requested_model` records
  what you asked for.
- `noul` has **no `confidence` and no `probabilities`**. It is a scalar. That is the
  contract, not an omission — see `docs/api-compatibility.md §4.2`.

## 4. All three primitives in one call

One request can mix question types, and every question is answered by the **same** forward
pass. `criteria` is what the model chooses between: a map of `name -> description` for
`choice`, an ordered list of levels for `score`.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
  "model": "mock",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors",
                                "sales": "pricing, new contracts"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel or leave?"},
    "urgency": {"type": "score", "instructions": "How urgent is this?",
                "criteria": ["Can wait", "Soon", "Today", "Immediately"]}
  }}'
# -> 200
```

```jsonc
{"model":"decis/mock@0.1.0",
 "answers":{
   "department":{"type":"choice","choice":"technical","confidence":0.6083,
                 "probabilities":{"billing":0.1879,"technical":0.7389,"sales":0.0732}},
   "churn_risk":{"type":"noul","noul":0.9711},
   "urgency":{"type":"score","score":1.9608,"confidence":0.6254,
              "legend":{"0":"Can wait","1":"Soon","2":"Today","3":"Immediately"},
              "probabilities":{"0":0.0109,"1":0.0709,"2":0.8647,"3":0.0535}}},
 "usage":{"input_tokens":118,"output_tokens":94},
 "decis":{"engine":"mock","engine_version":"0.1.0","device":"cpu","dtype":"none",
          "latency_ms":0.25,"batch_size":3}}
```

How to read the three answers:

| Primitive | The decision | The uncertainty |
|---|---|---|
| `choice` | `choice` — always the highest-probability criterion | `probabilities` over your `criteria` keys, plus `confidence` |
| `score` | `score` — the expected level, `Σ k·p(k)`, so it can land between levels (`1.96` here) | `probabilities` over `"0"`, `"1"`, … and `confidence` |
| `noul` | `noul` — `P(true)` as a plain number | that number *is* the uncertainty; there is no `confidence` |

`confidence` is normalised so that a coin flip is 0 and certainty is 1: for `choice` it is
`(p_max − 1/K)/(1 − 1/K)`, for `score` it is `1 − H(p)/ln(L)`. Use `probabilities` when you
need calibration; use `confidence` to threshold "should a human look at this".

`decis.batch_size` is `3` — all three questions went through one forward pass. That number
is how you can verify batching is happening rather than taking it on faith:
`docs/design-review.md §4-M5` explains why it is exposed.

> Keys in `score.probabilities` and `legend` are **strings** (`"0"`, `"1"`) on the wire,
> because JSON object keys are always strings. The official SDK converts them back to
> `int` for you — see [`python_sdk.py`](python_sdk.py).

## 5. Asking several questions about several documents

`state` can be a string, a JSON object, or an array — Decis flattens it into text before
the model sees it. A `noul` question can also define what counts as yes and no:

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": [{"ticket": "App crashes on launch", "notes": "happens every time, Android 14"},
            {"ticket": "Feature request: dark mode", "notes": "would be nice"}],
  "model": "mock",
  "questions": {
    "needs_engineer": {"type": "noul",
                       "instructions": "Does this ticket need an engineer rather than a product manager?",
                       "criteria": {"true": "a defect, crash, error or data loss",
                                    "false": "a preference, question or planned work"}}
  }}'
# -> 200
```

```json
{"model":"decis/mock@0.1.0",
 "answers":{"needs_engineer":{"type":"noul","noul":0.9435}},
 "usage":{"input_tokens":65,"output_tokens":12},
 "decis":{"engine":"mock","engine_version":"0.1.0","device":"cpu","dtype":"none",
          "latency_ms":0.1,"batch_size":1}}
```

The array became one block of text; the question is asked once about the whole thing, not
once per ticket. To ask per ticket, send one request per ticket — or use the SDK and let a
larger batch share the same forward pass (see [`python_sdk.py`](python_sdk.py)).

> **A `noul` question's `criteria` keys are `"true"` and `"false"`** — not `"yes"` and
> `"no"`, which is the natural thing to guess. The contract permits unknown properties, so
> mistyping them currently changes the answer **without an error**: the rubric text is
> dropped and the model answers the bare question. Measured — the request above with
> `"yes"`/`"no"` returns `noul: 0.3694` and 47 input tokens instead of `0.9435` and 65,
> with HTTP 200 either way. Recorded as D13 in
> [`docs/design-review.md`](../docs/design-review.md); **the wire contract allows the
> extras, so tightening it is a product decision, not a bug fix.**

## 6. The error contract

Four cases, and the distinction between the first two matters.

**No credential at all → 403.** Not 401.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H 'content-type: application/json' -d '{
  "state": "x", "model": "mock",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 403
```

```json
{"detail":{"error_type":"authentication_error","message":"No API key supplied. Send it as: Authorization: Bearer <your token>"}}
```

**Wrong scheme → also 403.** `Authorization: local` is not `Bearer local`, which is an easy
mistake to make by hand.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: $TOKEN" -H 'content-type: application/json' -d '{
  "state": "x", "model": "mock",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 403
```

```json
{"detail":{"error_type":"authentication_error","message":"No API key supplied. Send it as: Authorization: Bearer <your token>"}}
```

**A credential was sent but is wrong → 401.**

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H 'authorization: Bearer not-the-token' -H 'content-type: application/json' -d '{
  "state": "x", "model": "mock",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 401
```

```json
{"detail":{"error_type":"authentication_error","message":"Invalid API key. Check the token you are sending."}}
```

**Authentication is checked before the body is validated.** The requests above have a
valid body, but the ordering is what you should design around: send no credential *and* a
malformed body and you get 403, not 422 — a 422 would tell an unauthenticated caller which
fields to fix.

**A request that is understood but impossible → 422**, with a message that names the
problem:

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "x", "model": "mock",
  "questions": {"q": {"type": "choice", "instructions": "?", "criteria": {}}}}'
# -> 422
```

```json
{"detail":[{"loc":["body","questions","q","criteria"],"msg":"Question 'q' is a choice with no criteria, so there is nothing to choose between. Add at least one entry to `criteria`.","type":"too_short"}]}
```

An unknown model is a 422 whose message lists what this server can serve, so a
configuration typo is self-diagnosing:

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "x", "model": "gpt-9",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 422
```

```jsonc
{"detail":[{"loc":["body","model"],"type":"value_error",
  "msg":"Unknown model 'gpt-9'. Available on this server: decis-kev, decis-laya, ..."}]}
```

## 7. Every response carries a request id

Keep it: it appears in the server's structured logs, so it is the thing to quote in a bug
report, and the official SDK raises if the header is missing.

```bash
curl -s -D - -o /dev/null "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "x", "model": "mock",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 200
```

```text
HTTP/1.1 200 OK
x-typesafe-request-id: req_83ef524b2c881d9f200760b3da33c501
```

## 8. Overload: 429 with a retry instruction

When a request would have to queue past the server's budget, Decis answers **429 with
`retry-after-ms`** rather than holding the connection. This is not a failure to paper over:
the official SDK retries 429, and a retry with a wait is a retry that does not stampede.
See `docs/design.md §5.3`.

```jsonc
// HTTP/1.1 429 Too Many Requests
// retry-after-ms: 1000
// retry-after: 1
{"detail":{"error_type":"rate_limit_error",
           "message":"The mock engine is still busy with earlier requests after 8.0s, so this request was not started. It is refused rather than left to outlive the client's timeout, which would make the client retry and load the engine further. Retry shortly."}}
```

The message is generated by `InProcessScheduler` (`src/decis/scheduler.py:236`) and names
the engine and the budget it exceeded. Both `retry-after-ms` and `retry-after` are sent.

This case needs concurrent traffic to trigger, so it is the one block here without a
`curl` command that provokes it deterministically. The budget is
`DECIS_REQUEST_TIMEOUT_MS` (default 8000 ms, deliberately under the official SDK's 10 s
timeout).
