# Decis

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/kingfs/Decis/actions/workflows/ci.yml/badge.svg)](https://github.com/kingfs/Decis/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Docker Pulls](https://img.shields.io/docker/pulls/kingfs/decis.svg)](https://hub.docker.com/r/kingfs/decis)

**One API to run all light-weight decision models.**

Decis is a small, self-hostable server that speaks [TypeSafe's System One
API](https://docs.typesafe.ai/api) — the same `/v1/systemone` contract as Jev — and answers
those requests with an open decision model of your choosing. Point the official
`typesafe-sdk` at Decis instead of `api.typesafe.ai` and nothing else changes.

A *decision model* returns calibrated probabilities for typed questions instead of
generating text. It is a single forward pass, small enough to run next to your app, and fast
enough to sit in a request path. Decis is the serving layer for those models: one wire
contract, many interchangeable engines, and one container per engine.

> **Status: v0.2.0.** Two model families run behind one contract —
> `laya-multilingual` (default) and `kev-0.8b` — plus the English and typed-decision Laya
> checkpoints. The wire contract, authentication, error shapes, the engine abstraction,
> weight resolution, the CLI, the Docker images and CI are implemented and tested. The
> contract rests on the [official OpenAPI
> snapshot](docs/contract/typesafe-openapi-0.2.0.json) and a [record of what the live API
> actually returns](docs/contract/observations-2026-09-22.md), not on inference. What is
> **not** done is written down in [`docs/design-review.md`](docs/design-review.md) rather
> than left out.

## Quickstart

```bash
git clone https://github.com/kingfs/Decis && cd Decis
uv sync --extra dev --extra laya
cp .env.example .env                               # set DECIS_API_KEY=local, as the sample does
uv run decis download --engine laya-multilingual   # 647 MiB, once
uv run decis serve --host 127.0.0.1 --port 8000
```

`laya-multilingual` is the default engine and takes about **75 seconds** to load on CPU
([measured](docs/performance.md#latency)). `/healthz` answers immediately; `/readyz` reports
`loading` until the model can answer:

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

The official SDK needs one changed line:

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# No `model=`: the SDK sends its default, "jev-latest", and Decis answers with the
# engine it is actually running. Swapping base_url is the whole migration.
client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")

response = client.system_one(
    state={
        "subject": "Duplicate charge on invoice #4411",
        "body": "We were billed twice for March. Refund it today or we cancel our plan.",
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

print(response.choices["department"].choice)  # one of the criteria keys
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true), 0..1
```

[Getting started](docs/getting-started.md) covers readiness, the raw `curl` form, and
`decis models` / `decis doctor`.

## What you get

- **The jev contract, implemented once.** `POST /v1/systemone`, `GET /v1/models`, the
  `choice` / `score` / `noul` primitives, the official error shapes and the request-id
  header. The wire format is pinned in [`docs/api-compatibility.md`](docs/api-compatibility.md)
  with an evidence level per claim.
- **Pluggable engines.** An engine produces a probability per option; the server turns that
  into identical `Noul` / `Choice` / `Score` answers, including one documented `confidence`
  formula. Adding an engine changes no normalisation code.
- **Baked-in weights.** The image tag that names an engine carries that engine's checkpoint,
  so `docker run` needs no network, no volume and no download step. (The `-runtime` variant
  and the `playground` image are the exceptions, and are named as such.)
- **Safe by default.** Bearer auth, a constant-time comparison, a request-size cap, and a
  refusal to start on a public address with no token configured.

## Engines

| Engine | Backbone | Params | Weights | Notes |
|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | 100+ languages; the default engine |
| `laya` | ModernBERT-large | 421M | 807 MiB | English |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | The typed-decisions checkpoint |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.69 GiB | Prefill-only; wants a GPU |

Every registered engine is a real checkpoint. The weight-free test double used by the
contract suite is [`tests/fixture_engine.py`](tests/fixture_engine.py) and is deliberately
not registered by the server. See [Engines](docs/engines.md).

## Performance

Decision models do one forward pass, so the figures usually quoted for them come from small
inputs on Apple silicon with MLX. That is real, but it is not what a CPU-only container does.
Measured on a **24 vCPU aarch64 host with no GPU**:

<!-- LATENCY:START -->

| Engine | Device | dtype | Threads | 1 question | 10 questions | Cold start | Peak RSS |
|---|---|---|---:|---:|---:|---:|---:|
| `laya` | cpu | float32 | 24 | 446 ms | 3,222 ms (322.1 ms/question) | 76.3 s | 2.8 GB |
| `laya-multilingual` | cpu | float32 | 24 | 231 ms | 987 ms (98.7 ms/question) | 72.9 s | 4.84 GB |

Generated by [`benchmarks/report.py`](benchmarks/report.py) from the raw JSON in [`benchmarks/results/`](benchmarks/results/); a whole row comes from one configuration (the thread count torch picks by default, one per vCPU). p50 over the recorded samples, single process, within-request batching only.

**These are latencies, not throughput.** Every question in a row shares one `state`, which is the easy case. Cross-request batching has since been measured and does **not** raise throughput on CPU -- see [`docs/performance.md`](docs/performance.md) and [`docs/design-review.md §4-M5`](docs/design-review.md).

<!-- LATENCY:END -->

Two things worth taking from it: asking ten questions in one call is much cheaper per
question than asking one, and peak memory is several times the weight files. Cross-request
batching, the original throughput claim, was measured and **does not help** on CPU; the
evidence and the upper bound are in [Performance](docs/performance.md).

## Deploy

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me kingfs/decis:laya-multilingual
```

One image per engine, one Docker Hub repository, the engine in the tag. Every engine-tagged
image is multi-arch (`amd64` + `arm64`) and carries its weights. From a checkout, Compose and
`make` wrap the same thing:

```bash
docker compose up -d --wait                         # published images, default engine
docker compose --profile kev-0.8b up -d             # the other engine, host port 8001

make help                    # every target, and the engine this checkout resolves to
make up / down               # build from this tree and run, or stop
make build-playground        # rebuild just the games image: seconds
make up-playground           # the games, beside an engine already running anywhere
make pull                    # the deployment path: the published images
```

Sizes, volume caveats, the weightless `-runtime` variant, Kubernetes probes and proxy builds
are in [Deployment](docs/deployment.md).

## Playground

`docker compose up` also starts three browser games at <http://localhost:8080> — snake, dino
and tetris — plus a reference page for the API itself at `/api`. Each one can be played by hand
from the keyboard or handed to the model: the manual/AI switch, the inference panel and the
console for the last call are the same on all three, and in AI mode every decision is one real
`/v1/systemone` call. The playground holds the API key server-side, so the pages never see it,
and it finds the engine by itself. See [Playground](docs/playground.md), including the projects
the games are adapted from.

Each recording below is that page in AI mode, taken from this repository (real engine, CPU);
frames in which nothing changed are dropped, so they run faster than the session they came from:

| Snake | Dino | Tetris |
|---|---|---|
| ![The model choosing each move in snake](playground/web/media/snake.gif) | ![The model choosing jump, duck or run in dino](playground/web/media/dino.gif) | ![The model choosing a placement in tetris](playground/web/media/tetris.gif) |


## Documentation

| Document | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | Clone to first answer, readiness, CLI |
| [API reference](docs/api.md) | Endpoints, schemas, primitives, error codes, limits |
| [API schemas](docs/schema/) | Generated JSON Schema and the server's OpenAPI document |
| [Configuration](docs/configuration.md) | Every `DECIS_*` variable, auth, model paths, dtype |
| [Engines](docs/engines.md) | What each engine is, its limits, how to add one |
| [Deployment](docs/deployment.md) | Docker, Compose, `make`, Kubernetes |
| [Performance](docs/performance.md) | Measured latency, memory, dtype and batching results |
| [Playground](docs/playground.md) | The three games, the proxy, and credits |
| [Wire contract](docs/api-compatibility.md) | The exact jev contract, with an evidence level per claim |
| [Design](docs/design.md) | Architecture, the engine abstraction, packaging |
| [Design review](docs/design-review.md) | A self-audit: defects found, methodology limits, what is unverified |
| [Feasibility](docs/feasibility.md) | Investigation results and the risk register |
| [`examples/`](examples/README.md) | Runnable `curl` and official-SDK examples, executed by CI |
| [AGENTS.md](AGENTS.md) | Engineering contract for contributors and agents |

If you read one document before contributing, read the design review: it is the list of
defects found and questions still open.

The wire contract, the design, its self-audit and the feasibility study are written in
Chinese — that is the language they were researched and reviewed in. The contract itself is
not locked in prose: [`docs/schema/`](docs/schema/) is generated from `src/decis/schema.py`
and checked in CI, and [API reference](docs/api.md) is the English view of it.

## Relationship to other projects

Decis does not train models. It serves them, and it gives credit rather than duplicating
work.

- **[Jev](https://docs.typesafe.ai/introduction)** (TypeSafe AI) — the closed model whose
  API this project targets. Decis reimplements the *interface*, not the model.
- **[kev](https://github.com/jaredpalmer/kev)** (Jared Palmer) — a Jev-style decision model
  built on Qwen3.5. Decis reuses kev's inference kernel (vendor-pinned, Apache-2.0,
  attributed in [`NOTICE`](NOTICE)) and generalises the serving layer to many engines.
- **[Laya](https://huggingface.co/convaiinnovations/laya)** (Convai Innovations) —
  Apache-2.0, multilingual, one forward pass. Decis uses the official `laya` package.
- **[UniTS-Hub](https://github.com/kingfs/UniTS-Hub)** — the multi-model container build
  pattern Decis follows.
- **[djev-run](https://github.com/taeold/djev-run)** (Daniel Lee) — the playground's games
  are adapted from it; see [Playground](docs/playground.md#credits).

## Contributing

Contributions are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the development
setup, the two test environments, and the pull-request rules; [`AGENTS.md`](AGENTS.md) is
the normative engineering contract — canonical homes, invariants and banned patterns — and
applies to human and automated contributors alike.

```bash
uv sync --extra dev
uv run pytest -q            # the weight-free suite: what CI runs
uv run ruff check && uv run ruff format --check
```

Security issues should go through [`SECURITY.md`](SECURITY.md), not a public issue.

## License

Apache-2.0. See [`NOTICE`](NOTICE) for third-party attribution.
