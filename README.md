# Decis

**English** · [简体中文](README.zh-CN.md)

**One API to run all light-weight decision models.**

Decis is a small, self-hostable server that speaks [TypeSafe's System One API](https://docs.typesafe.ai/api) — the same `/v1/systemone` contract as Jev — and answers those requests with an open decision model of your choosing. Point the official `typesafe-sdk` at Decis instead of `api.typesafe.ai` and nothing else changes.

> **Status: the API server works and Laya runs behind it.** `Stage 0` and `Stage 1` are done. The wire contract, authentication, error shapes, the engine abstraction, weight resolution, the CLI, the Dockerfile and CI are implemented, with **274 tests passing** without weights — including the official `typesafe-sdk` 0.7.1 driven over a real socket. Three real Laya checkpoints are registered alongside `mock`, and **`decis serve --engine laya-multilingual` answers real requests today**; 14 further tests load the real weights and check batch invariance. kev lands in Stage 2 (`docs/design.md §12`).
>
> The contract in [`docs/api-compatibility.md`](docs/api-compatibility.md) rests on the [official OpenAPI snapshot](docs/contract/typesafe-openapi-0.2.0.json) and a [record of what the live API actually returns](docs/contract/observations-2026-09-22.md) — not on inference. [`docs/design-review.md`](docs/design-review.md) is an adversarial audit of the design, including what is still unproven.

---

## Quickstart

No weights, no GPU, no model download. The `mock` engine answers deterministically.

```bash
git clone https://github.com/kingfs/Decis && cd Decis
uv sync --extra dev
cp .env.example .env          # then set DECIS_API_KEY to anything
uv run decis serve --host 127.0.0.1 --port 8000
```

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# No `model=`: the SDK sends its default, "jev-latest", and Decis answers with the
# engine it is actually running. Swapping TYPESAFE_BASE_URL is the whole migration.
client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")

response = client.system_one(
    state={
        "subject": "Duplicate charge on invoice #4411",
        "body": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
    },
    questions={
        "department": Choice(
            instructions="Which team should handle this?",
            criteria={
                "billing": "invoices, payments, refunds",
                "technical": "bugs, outages, system errors",
                "sales": "pricing, new contracts",
            },
        ),
        "churn_risk": Noul(instructions="Does the user threaten to cancel or leave?"),
    },
)

print(response.model)  # decis/mock@0.1.0
print(response.choices["department"].choice)  # whichever the engine picked
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true), 0..1
```

The same request by hand:

```bash
curl -s localhost:8000/v1/systemone \
  -H 'authorization: Bearer local' -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "mock",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/mock@0.1.0",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.763,
                    "probabilities": { "billing": 0.8815, "technical": 0.1185 } },
    "churn_risk": { "type": "noul", "noul": 0.89 }
  },
  "usage": { "input_tokens": 67, "output_tokens": 40 },
  // Everything Decis adds beyond the contract lives under one key, and the
  // official SDK ignores it. `batch_size` is how you can see batching happen.
  "decis": { "engine": "mock", "engine_version": "0.1.0", "device": "cpu", "dtype": "none",
             "latency_ms": 0.2, "batch_size": 2 }
}
```

Other things that work today:

```bash
uv run decis models     # which engines are registered and usable here
uv run decis doctor     # environment and configuration self-check
```

### A real model

Swap `mock` for Laya. The request above does not change — only which engine answers it.

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual --dest ./models   # 647 MiB, one time
uv run decis serve --engine laya-multilingual --host 127.0.0.1
```

Cold start is about **80 s on CPU**, and **the server answers nothing until it finishes** — the
engine is loaded during startup, before the HTTP protocol loop begins, so a probe during that window
hangs rather than returning 503. If you put this behind a probe, give it a generous start period
(the image's `HEALTHCHECK` uses 180 s) or the orchestrator will restart a container that is loading
correctly. Weights in the image means it then runs with no network:

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual -t decis:laya-multilingual .
docker run --rm -p 8000:8000 -e DECIS_API_KEY=local decis:laya-multilingual
```

To serve your own fine-tune, point that engine at a directory instead — it takes precedence over
everything else, including the network:

```bash
DECIS_MODEL_PATH_LAYA_MULTILINGUAL=/srv/finetunes/acme-triage uv run decis serve
```

## The problem

Jev proved that a *decision model* — a model that answers typed questions about a state and returns calibrated probabilities instead of generated text — belongs in your request path, not in a chat window. But it is a hosted, closed API:

- **Latency.** Independently measured at 236–276 ms p50 per question. If your decision is a routing check, that is a network round trip you pay on every call.
- **Data.** Your ticket, your invoice, your user's message leaves your infrastructure.
- **Cost.** $42 per billion input tokens, forever.

There are now open alternatives. They are small enough to run next to your app. What is missing is not the model — it is a **uniform way to serve it**.

Decis is that uniform way: one stable API, many engines, packaged as one container per model.

## What you get

- **The jev contract, implemented once.** `POST /v1/systemone`, `GET /v1/models`, `choice` / `score` / `noul` primitives, the official SDK's error shapes and request-id header. The wire format is pinned down in [`docs/api-compatibility.md`](docs/api-compatibility.md) with an evidence level on every claim — and the live API was probed rather than assumed, which is how we found that a missing credential is 403 while an invalid one is 401.
- **Pluggable engines.** An engine only has to produce a probability per option; the server turns that into `Noul` / `Choice` / `Score` answers, so every engine returns identical, comparable shapes — including a single documented `confidence` definition. Engines are referenced by string path, so an image with one engine's dependencies installed can still list the others.
- **Aimed at small, frequent calls.** Both open decision models are a single forward pass, so the request path is designed to batch questions *across requests* before they reach the model — see the honesty note under Performance.
- **Baked-in weights or a mounted volume.** Images ship with the model so `docker run` works offline; `DECIS_MODEL_DIR` overrides it with your own directory.
- **Safe by default.** Bearer-token auth (from `.env`), a constant-time comparison, a request-size cap, and a refusal to start on a public address with no token configured.

## Engines

| Engine | Backbone | Params | Weights | Status | Notes |
|---|---|---|---|---|---|
| `mock` | — | — | none | **shipped** | Deterministic, weight-free. Contract tests and demos |
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | **shipped** | 100+ languages, ~2.2× faster — the intended default |
| `laya` | ModernBERT-large | 421M | 807 MiB | **shipped** | English, strongest on English benchmarks |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | **shipped** | The typed-decisions checkpoint from the same repo |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA | 0.8B | ~1.7 GB | Stage 2 | Different architecture, different error profile |
| `kev-4b` / `kev-9b` | Qwen3.5 + LoRA | 4B / 9B | ~8 GB / ~18 GB | Stage 2 | Higher accuracy, no longer "light-weight" |
| `remote` | — | — | — | planned | Forwards to the real `api.typesafe.ai`; A/B and test oracle |

Adding an engine is one module plus one registry line, and **no change to the normalisation layer** —
if an engine needs `render.py` or `answers.py` changed, the abstraction is wrong. See [`AGENTS.md §5`](AGENTS.md).

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

- **Within-request batching is a real lever.** Ten questions in one call cost about half as much per
  question as one question on its own.
- **Cold start is ~75 seconds**, and peak memory is 3–7× the weight files. Plan readiness probes and
  container memory accordingly.
- **`kev-0.8b` wants a GPU.** Its Qwen3.5 backbone needs `flash-linear-attention` and `causal_conv1d`
  to run at speed, and both require Triton/CUDA. On CPU it is an order of magnitude slower than Laya.
  Its `bf16` path on CPU is a further **83×** slower than `fp32` (a single observation, not a
  benchmark) — which is why Decis picks dtypes per engine *and* device, and warns rather than silently
  crawling.

### What this table does *not* tell you

Decis's central throughput claim is **cross-request batching** — joining questions from *different*
requests into one forward pass. That has **never been measured**, because it needs the batching
scheduler that arrives in Stage 3. The table above only measures questions that share one `state`,
which is the easy case. Real traffic has a different state per request, and the win there may be
smaller.

So: **no QPS figure is quoted here, and none should be until it has been measured.** This is gap M5 in
[`docs/design-review.md`](docs/design-review.md), and it is the reason `decis.batch_size` is in every
response — the mechanism has to be observable, or "it batches" is unfalsifiable. The design does
include a test that the batcher *did* fire, so a batcher that never triggers cannot pass CI.

Raw per-sample output, the exact commands, and the host spec are in
[`benchmarks/results/`](benchmarks/results/). Every number quoted in the docs comes from there.

## Deployment

One image per engine, because engine dependencies conflict and are large. Images with weights baked in
arrive with each engine (Stage 4); today's image is the API plus the mock engine.

```bash
docker build -f docker/Dockerfile -t decis:mock .
docker run -p 8000:8000 -e DECIS_API_KEY=change-me decis:mock
```

Mounted weights beat baked weights when you want to update a model without rebuilding:

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models decis:laya
```

The container runs as a non-root user, needs no external services — no Redis, no Postgres, no Celery —
and refuses to start on a public address with no token configured.

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
uv sync --extra dev
uv run pytest -q                        # 274 tests, ~7 s, no weights, no network
uv run pytest -m weights                # 14 tests that load the real Laya weights
uv run ruff check && uv run ruff format --check
uv run decis serve --host 127.0.0.1     # loopback may run without a token
```

The test suite is organised by the evidence it provides, following
[`docs/api-compatibility.md §8`](docs/api-compatibility.md):

| Layer | What it proves | Where |
|---|---|---|
| L0 | Our models still match the vendored official OpenAPI snapshot | [`tests/test_contract_openapi.py`](tests/test_contract_openapi.py) |
| L1/L2 | Response shape and every invariant in `AGENTS.md §3` | [`tests/test_contract_shape.py`](tests/test_contract_shape.py) |
| L3b | The error contract: 401 vs 403, auth before validation, request ids | [`tests/test_contract_errors.py`](tests/test_contract_errors.py) |
| L4 | **The real `typesafe-sdk` over a real socket** — the only test that proves compatibility | [`tests/test_contract_sdk.py`](tests/test_contract_sdk.py) |
| — | Readiness, cold start, shutdown | [`tests/test_readiness.py`](tests/test_readiness.py) |
| — | The canonical-home and layering rules, enforced by parsing the AST | [`tests/test_conventions.py`](tests/test_conventions.py) |

Before changing anything in `docs/api-compatibility.md`, run the differential check against the live API
— no offline test can detect the live server disagreeing with its own OpenAPI:

```bash
TYPESAFE_LIVE_API_KEY=<key> uv run pytest tests/test_contract_sdk.py -m network
```

## License

Apache-2.0. See [`NOTICE`](NOTICE) for third-party attribution.
