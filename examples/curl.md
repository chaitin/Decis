# Raw HTTP examples

One `curl` command per example, run against a server you started (see
[`README.md`](README.md)). Each command ends with `-w '\n%{http_code}\n'` so the status
code is visible; drop that flag for clean JSON. Every status shown below is asserted by
`tests/test_examples.py`, which starts a real server and runs these commands verbatim.

The requests send `"model": "jev-latest"` — the official SDK's own default name, which
Decis maps to whichever engine the server was started with (`DECIS_DEFAULT_ENGINE`,
`laya-multilingual` by default). The response bodies below show the **shape** the contract
guarantees, with `<…>` for values that depend on the engine and its weights — they are not
captured output, and no number here is meant to be reproduced verbatim. The field-by-field
contract, with evidence levels, is
[`docs/api-compatibility.md`](../docs/api-compatibility.md).

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

```jsonc
{"status": "ok", "version": "<server version>"}
```

```bash
curl -s -w '\n%{http_code}\n' "$BASE/readyz"
# -> 200
```

```jsonc
{"status": "ready", "engine": "<engine id>"}
```

While loading, the same call returns 503 with `retry-after: 1` — a "not yet" that is worth
retrying. A **failed** load returns 503 *without* `retry-after`, because that state is
terminal until the process is replaced; a caller told to back off from a permanently broken
engine would retry forever (`src/decis/routes.py:76-88`):

```jsonc
// still loading -- retry
{"status": "loading", "engine": "<engine id>"}
// HTTP/1.1 503 Service Unavailable
// retry-after: 1

// terminal failure -- do not retry, alert instead
{"status": "failed", "engine": "<engine id>", "error": "<why>"}
// HTTP/1.1 503 Service Unavailable
```

## 2. Which models can this server run?

Requires authentication. Every registered engine is listed, with its own capacity limits under
the `decis` key (extra fields are namespaced so the official SDK can ignore them). An engine
whose upstream package is not installed is still listed, and reports `version: "not-installed"`
— so you can tell "dependencies missing" from "not registered".

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/models" -H "authorization: Bearer $TOKEN"
# -> 200
```

```jsonc
{"models": [
  {"name": "decis/<engine>@<version>", "description": "…", "release_date": "…",
   "decis": {"engine": "<engine>", "version": "<version>",
             "aliases": [],
             "primitives": ["choice", "noul", "score"],
             "max_sequence_tokens": "<n>", "languages": "<languages the checkpoint declares>",
             "device": "<cpu|cuda|mps|unloaded>",
             "dtype": "<float32|float16|bfloat16|unloaded>"}}
]}
```

> `dtype` is `"unloaded"` until the engine has loaded. Do not use `device` as a load test:
> Laya reports `unloaded` while it is idle, kev reports the device it will use before it
> loads. `version: "not-installed"` means the engine's upstream package is missing, not that
> its weights are. The numeric capacities are engine-specific, and `uv run decis models` does
> not print them — it reports whether each engine can run on this machine. `aliases` is part
> of the payload but empty for the engines that ship today; `uv run decis models` lists the
> names a server actually answers to.

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

```jsonc
{"model": "decis/<engine>@<version>",
 "answers": {"is_bug": {"type": "noul", "noul": "<P(true), a number in [0, 1]>"}},
 "usage": {"input_tokens": "<n>", "output_tokens": "<n>"},
 "decis": {"engine": "<engine>", "engine_version": "<version>", "device": "cpu",
           "dtype": "<dtype>", "latency_ms": "<n>", "batch_size": 1,
           "requested_model": "jev-latest"}}
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
  "model": "jev-latest",
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
{"model": "decis/<engine>@<version>",
 "answers": {
   "department": {"type": "choice", "choice": "<one of your criteria keys>",
                  "confidence": "<0..1>",
                  "probabilities": {"billing": "<p>", "technical": "<p>", "sales": "<p>"}},
   "churn_risk": {"type": "noul", "noul": "<P(true)>"},
   "urgency": {"type": "score", "score": "<expected level, 0..L-1>",
               "confidence": "<0..1>",
               "legend": {"0": "Can wait", "1": "Soon", "2": "Today", "3": "Immediately"},
               "probabilities": {"0": "<p>", "1": "<p>", "2": "<p>", "3": "<p>"}}},
 "usage": {"input_tokens": "<n>", "output_tokens": "<n>"},
 "decis": {"engine": "<engine>", "engine_version": "<version>", "device": "cpu",
           "dtype": "<dtype>", "latency_ms": "<n>", "batch_size": 3}}
```

How to read the three answers:

| Primitive | The decision | The uncertainty |
|---|---|---|
| `choice` | `choice` — always the highest-probability criterion | `probabilities` over your `criteria` keys, plus `confidence` |
| `score` | `score` — the expected level, `Σ k·p(k)`, so it can land between levels (e.g. `1.96`) | `probabilities` over `"0"`, `"1"`, … and `confidence` |
| `noul` | `noul` — `P(true)` as a plain number | that number *is* the uncertainty; there is no `confidence` |

`confidence` is normalised so that a coin flip is 0 and certainty is 1: for `choice` it is
`(p_max − 1/K)/(1 − 1/K)`, for `score` it is `1 − H(p)/ln(L)`. Use `probabilities` when you
need calibration; use `confidence` to threshold "should a human look at this".

`decis.batch_size` is `3` — all three questions went through one forward pass. That number
is how you can verify batching is happening rather than taking it on faith.

> Keys in `score.probabilities` and `legend` are **strings** (`"0"`, `"1"`) on the wire,
> because JSON object keys are always strings. The official SDK converts them back to
> `int` for you — see [`python_sdk.py`](python_sdk.py).

## 5. Asking several questions about several documents

`state` can be a string, a JSON object, or an array — Decis flattens it into text before
the model sees it. A `noul` question can also define what counts as yes and no:

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "model": "jev-latest",
  "state": [{"ticket": "App crashes on launch", "notes": "happens every time, Android 14"},
            {"ticket": "Feature request: dark mode", "notes": "would be nice"}],
  "questions": {
    "needs_engineer": {"type": "noul",
                       "instructions": "Does this ticket need an engineer rather than a product manager?",
                       "criteria": {"true": "a defect, crash, error or data loss",
                                    "false": "a preference, question or planned work"}}
  }}'
# -> 200
```

```jsonc
{"model": "decis/<engine>@<version>",
 "answers": {"needs_engineer": {"type": "noul", "noul": "<P(true)>"}},
 "usage": {"input_tokens": "<n>", "output_tokens": "<n>"},
 "decis": {"engine": "<engine>", "engine_version": "<version>", "device": "cpu",
           "dtype": "<dtype>", "latency_ms": "<n>", "batch_size": 1}}
```

The array became one block of text; the question is asked once about the whole thing, not
once per ticket. To ask per ticket, send one request per ticket — or use the SDK and let a
larger batch share the same forward pass (see [`python_sdk.py`](python_sdk.py)).

> **A `noul` question's `criteria` keys are `"true"` and `"false"`** — not `"yes"` and
> `"no"`, which is the natural thing to guess. The contract permits unknown properties, so
> mistyping them currently changes the answer **without an error**: the rubric text is
> dropped, the model answers the bare question, and the response is HTTP 200 either way.
> **The wire contract allows the extras, so tightening it is a product decision, not a bug
> fix.**

## 6. The error contract

Four cases, and the distinction between the first two matters.

**No credential at all → 403.** Not 401.

```bash
curl -s -w '\n%{http_code}\n' "$BASE/v1/systemone" \
  -H 'content-type: application/json' -d '{
  "state": "x", "model": "jev-latest",
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
  "state": "x", "model": "jev-latest",
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
  "state": "x", "model": "jev-latest",
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
  "state": "x", "model": "jev-latest",
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
{"detail": [{"loc": ["body", "model"], "type": "value_error",
  "msg": "Unknown model 'gpt-9'. Available on this server: <every registered name>"}]}
```

## 7. Every response carries a request id

Keep it: it appears in the server's structured logs, so it is the thing to quote in a bug
report, and the official SDK raises if the header is missing.

```bash
curl -s -D - -o /dev/null "$BASE/v1/systemone" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "state": "x", "model": "jev-latest",
  "questions": {"q": {"type": "noul", "instructions": "?"}}}'
# -> 200
```

```text
HTTP/1.1 200 OK
x-typesafe-request-id: req_<32 lowercase hex digits>
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
{"detail": {"error_type": "rate_limit_error",
            "message": "The <engine> engine is still busy with earlier requests after 8.0s, so this request was not started. It is refused rather than left to outlive the client's timeout, which would make the client retry and load the engine further. Retry shortly."}}
```

The message is generated by `InProcessScheduler` (`src/decis/scheduler.py:236`) and names
the engine and the budget it exceeded. Both `retry-after-ms` and `retry-after` are sent.

This case needs concurrent traffic to trigger, so it is the one block here without a
`curl` command that provokes it deterministically. The budget is
`DECIS_REQUEST_TIMEOUT_MS` (default 8000 ms, deliberately under the official SDK's 10 s
timeout).
