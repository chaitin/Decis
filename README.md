# Decis

**English** · [简体中文](README.zh-CN.md)

**One API to run all light-weight decision models.**

Decis is a small, self-hostable server that speaks [TypeSafe's System One API](https://docs.typesafe.ai/api) — the same `/v1/systemone` contract as Jev — and answers those requests with an open decision model of your choosing. Point the official `typesafe-sdk` at Decis instead of `api.typesafe.ai` and nothing else changes.

> **Status: design phase.** No code has been written yet. [`docs/design.md`](docs/design.md) is the target implementation; [`AGENTS.md`](AGENTS.md) is the engineering contract for building it; [`docs/design-review.md`](docs/design-review.md) is an adversarial audit of the design, including what is still unproven. The contract in [`docs/api-compatibility.md`](docs/api-compatibility.md) rests on the [official OpenAPI snapshot](docs/contract/typesafe-openapi-0.2.0.json) and a [record of what the live API actually returns](docs/contract/observations-2026-09-22.md) — not on inference.

---

## The problem

Jev proved that a *decision model* — a model that answers typed questions about a state and returns calibrated probabilities instead of generated text — belongs in your request path, not in a chat window. But it is a hosted, closed API:

- **Latency.** Independently measured at 236–276 ms p50 per question. If your decision is a routing check, that is a network round trip you pay on every call.
- **Data.** Your ticket, your invoice, your user's message leaves your infrastructure.
- **Cost.** $42 per billion input tokens, forever.

There are now open alternatives. They are small enough to run next to your app. What is missing is not the model — it is a **uniform way to serve it**.

Decis is that uniform way: one stable API, many engines, packaged as one container per model.

## What you get

- **The jev contract, implemented once.** `POST /v1/systemone`, `GET /v1/models`, `choice` / `score` / `noul` primitives, the official SDK's error shapes and request-id header. The wire format is pinned down in [`docs/api-compatibility.md`](docs/api-compatibility.md) with an evidence level on every claim.
- **Pluggable engines.** An engine only has to produce a probability per option; the server turns that into `Noul` / `Choice` / `Score` answers, so every engine returns identical, comparable shapes — including a single documented `confidence` definition.
- **Built for small, frequent calls.** Both open decision models are a single forward pass, so Decis batches questions *across requests* before they reach the model. This is the difference between "a FastAPI wrapper" and a server.
- **Baked-in weights or a mounted volume.** Images ship with the model so `docker run` works offline; `DECIS_MODEL_DIR` overrides it with your own directory.

## Engines

| Engine | Backbone | Params | Weights | Notes |
|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 614 MiB | 100+ languages, ~2.2× faster — the best default |
| `laya` | ModernBERT-large | 421M | 804 MiB | English, strongest on English benchmarks |
| `laya-typed-decisions` | ModernBERT-large | 421M | 804 MiB | Tuned for the typed-decisions workflows |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA | 0.8B | ~1.7 GB | Different architecture, different error profile |
| `kev-4b` / `kev-9b` | Qwen3.5 + LoRA | 4B / 9B | ~8 GB / ~18 GB | Higher accuracy, no longer "light-weight" |
| `remote` | — | — | — | Forwards to the real `api.typesafe.ai`; useful for A/B and as a test oracle |

Adding an engine is one module plus one registry line. See [`AGENTS.md §5`](AGENTS.md).

## Performance

Decision models do one forward pass, so the headline numbers people quote come from **small, short
inputs on Apple silicon with MLX**: ~11–18 ms per question on an M3 Max. That is real, but it is not
what a CPU-only container does. Measured on a **24 vCPU aarch64 host with no GPU** — the worst case,
and the one most people will try first:

| Engine | Cold start | Peak RSS | 1 question | 10 questions |
|---|---:|---:|---:|---:|
| `laya-multilingual` (322M) | 73 s | 4.84 GB | 201 ms | 987 ms — **98.7 ms/question** |
| `laya` (English, 421M) | 76 s | 2.80 GB | 432 ms | 3222 ms — 322 ms/question |
| `kev-0.8b` (fp32, 3 questions) | 15 s | — | — | 1656 ms — 552 ms/question |

Three things worth taking from this table:

- **Batching is the lever.** Ten questions in one call cost about half as much per question as one
  question on its own. Decis batches across requests, not just within one.
- **Cold start is ~75 seconds**, and peak memory is 3–7× the weight files. Plan readiness probes and
  container memory accordingly.
- **`kev-0.8b` wants a GPU.** Its Qwen3.5 backbone needs `flash-linear-attention` and `causal_conv1d`
  to run at speed, and both require Triton/CUDA. On CPU it is an order of magnitude slower than Laya.
  Its `bf16` path on CPU is a further **83×** slower than `fp32` — which is why Decis picks dtypes per
  engine *and* device, and warns rather than silently crawling.

Raw per-sample output, the exact commands, and the host spec are in
[`benchmarks/results/`](benchmarks/results/). Every number quoted in the docs comes from there.

## Usage

```bash
docker run -p 8000:8000 ghcr.io/kingfs/decis:laya-multilingual-latest
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000", model="laya-multilingual")

response = client.system_one(
    state={"subject": "Duplicate charge on invoice #4411",
           "body": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan."},
    questions={
        "department":  Choice(instructions="Which team should handle this?",
                              criteria={"billing": "invoices, payments, refunds",
                                        "technical": "bugs, outages, system errors",
                                        "sales": "pricing, new contracts"}),
        "urgency":     Score(instructions="How urgent is this?",
                             criteria=["not urgent", "soon", "critical deadline or blocking issue"]),
        "churn_risk":  Noul(instructions="Does the user threaten to cancel or leave?"),
    },
)

print(response.choices["department"].choice)      # billing
print(response.scores["urgency"].score)           # 1.84
print(response.nouls["churn_risk"].noul)          # 0.89
```

Or by hand:

```bash
curl -s localhost:8000/v1/systemone -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "laya-multilingual",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/laya-multilingual@0.3.5",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.71,
                    "probabilities": { "billing": 0.85, "technical": 0.15 } },
    "churn_risk": { "type": "noul", "noul": 0.89 }
  },
  "usage": { "input_tokens": 96, "output_tokens": 21 }
}
```

## Primitives

Three question types, mixable in one request, evaluated in parallel against the same state:

| Type | Ask | Returns |
|---|---|---|
| `choice` | pick one of a named set | `choice`, `probabilities`, `confidence` |
| `score` | rate on an ordered rubric | `score` (expected level), `legend`, `probabilities`, `confidence` |
| `noul` | is this true? | `noul` — calibrated P(true), 0–1 |

## Documentation

| Document | What it covers |
|---|---|
| [`docs/api-compatibility.md`](docs/api-compatibility.md) | The exact wire contract, with an evidence level per claim and every known deviation |
| [`docs/design.md`](docs/design.md) | Architecture, the engine abstraction, batching, packaging, roadmap |
| [`docs/design-review.md`](docs/design-review.md) | A self-audit of this design: defects found and fixed, methodology limits, what is still unverified |
| [`docs/feasibility.md`](docs/feasibility.md) | Investigation results, measured numbers, risk register |
| [`docs/contract/`](docs/contract/) | The official OpenAPI snapshot, plus the raw record of what the live API actually returns |
| [`AGENTS.md`](AGENTS.md) | Engineering contract: canonical homes, invariants, banned patterns |

If you read only one document before contributing, read the design review. It is where the gaps are
written down instead of hidden.

## Relationship to other projects

Decis does not train models. It serves them, and it tries to give credit rather than duplicate work.

- **[Jev](https://docs.typesafe.ai/introduction)** (TypeSafe AI) — the closed model whose API this project targets. Decis reimplements the *interface*, not the model.
- **[kev](https://github.com/jaredpalmer/kev)** (Jared Palmer) — a Jev-style decision model built on Qwen3.5, and already a TypeSafe-compatible server for its own weights. Decis reuses kev's inference kernel (vendor-pinned, Apache-2.0, attributed in `NOTICE`) and generalises the serving layer to many engines.
- **[Laya](https://huggingface.co/convaiinnovations/laya)** (Convai Innovations) — Apache-2.0, multilingual, non-autoregressive, one forward pass. Decis uses the official `laya` package as an engine.
- **[UniTS-Hub](https://github.com/kingfs/UniTS-Hub)** — the multi-model container build pattern Decis follows (one Dockerfile with a build arg, GitHub Actions matrix pushing by digest, then `imagetools create`).

## Development

```bash
uv sync --extra server --extra laya
uv run pytest -q           # no model weights needed
uv run decis serve
```

## License

Apache-2.0. See [`NOTICE`](NOTICE) for third-party attribution.
