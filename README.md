# Decis

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/chaitin/Decis/actions/workflows/ci.yml/badge.svg)](https://github.com/chaitin/Decis/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Docker Pulls](https://img.shields.io/docker/pulls/chaitin/decis.svg)](https://hub.docker.com/r/chaitin/decis)

**One API to run all light-weight decision models.**

Decis is a self-hostable inference server for open decision models. It implements the
[TypeSafe System One API](https://docs.typesafe.ai/api) — the `/v1/systemone` contract the
official `typesafe-sdk` already speaks — so pointing the SDK at your own host is the whole
migration. One wire contract, several interchangeable engines, one container per engine.

A *decision model* returns calibrated probabilities for typed questions instead of
generating text: one forward pass, small enough to run next to your app.

- **Runs out of the box.** Three commands and you have an engine answering `/v1/systemone`
  and a playground to try it in. The images carry their weights, so nothing is downloaded at
  startup and there is no volume to mount.
- **A Jev-like API.** Decis speaks the contract the closed model does, so the official
  `typesafe-sdk` needs `base_url` changed and nothing else.
- **Laya and kev, one image each.** A published multi-arch image per engine, weights
  included.
- **A playground with three games.** Snake, dino and tetris, each one deciding through a
  real `/v1/systemone` call.

> **Status:** pre-1.0, currently `v0.3.0`. The wire contract (`v1`) is stable and only gains
> fields; the running server reports its own version at `/healthz`.

## Quickstart

Three commands, no build and no model download:

```bash
git clone https://github.com/chaitin/Decis && cd Decis
cp .env.example .env                                # set DECIS_API_KEY
docker compose -f docker-compose.yml up -d --wait    # engine on :8000, games on :8080
```

`laya-multilingual` is the default engine. `/healthz` answers immediately; `/readyz` reports
`loading` until the model can answer:

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

The client side is one changed line. The official SDK works unchanged once it points here:

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

Compose pulls two published images, `chaitin/decis:laya-multilingual` and
`chaitin/decis:playground`. To run the API without Compose:

```bash
docker run --rm -p 8000:8000 -e DECIS_API_KEY=change-me chaitin/decis:laya-multilingual
```

From a source checkout instead:

```bash
uv sync --extra dev --extra laya
uv run decis download --engine laya-multilingual   # 647 MiB, once
uv run decis serve --host 127.0.0.1 --port 8000
```

[Getting started](docs/getting-started.md) covers readiness, the raw `curl` form, and
`decis models` / `decis doctor`.

## Engines

Four engines ship. `laya-multilingual` (the default, about 100 languages) and `kev-0.8b`
have published multi-arch images; `laya` and `laya-typed-decisions` run from a source
checkout. One image per engine, the engine is the tag, and the weight files are inside it —
no network, no volume and no download step at startup. The server requires a bearer token,
compares it in constant time, and refuses to start on a public address with no token
configured.

Backbones, weight sizes and per-engine limits are in [Engines](docs/engines.md). Pull sizes,
the weightless runtime variant, Kubernetes probes and building behind a proxy are in
[Deployment](docs/deployment.md).

## Playground

The Quickstart also starts three browser games at <http://localhost:8080> — snake, dino and
tetris — plus a reference page for the API itself at `/api`. Each one can be played by hand
from the keyboard or handed to the model: the manual/AI switch, the inference panel and the
console for the last call are the same on all three, and in AI mode every decision is one
real `/v1/systemone` call. The playground holds the API key server-side, so the pages never
see it, and it finds the engine by itself. See [Playground](docs/playground.md), including
the projects the games are adapted from.

Each recording is that page in AI mode, against a real engine on CPU. Unchanged frames are
dropped.

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
| [Performance](docs/performance.md) | Measured latency and memory, on the machine they were measured on |
| [Playground](docs/playground.md) | The three games, the proxy, and credits |
| [Wire contract](docs/api-compatibility.md) | The exact jev contract, with an evidence level per claim |
| [Design](docs/design.md) | Architecture, the engine abstraction, packaging |
| [Design review](docs/design-review.md) | A self-audit: defects found, methodology limits, what is unverified |
| [Feasibility](docs/feasibility.md) | Investigation results and the risk register |
| [`examples/`](examples/README.md) | Runnable `curl` and official-SDK examples, executed by CI |
| [AGENTS.md](AGENTS.md) | Engineering contract for contributors and agents |

The wire contract, the design, its self-audit and the feasibility study are written in
Chinese; the contract is also pinned as generated JSON Schema under
[`docs/schema/`](docs/schema/), generated from `src/decis/schema.py` and checked in CI.

## Relationship to other projects

- **[Jev](https://docs.typesafe.ai/introduction)** (TypeSafe AI) — the closed model whose
  API this project targets. Decis reimplements the *interface*, not the model.
- **[kev](https://github.com/jaredpalmer/kev)** (Jared Palmer) — a Jev-style decision model
  built on Qwen3.5. Decis reuses kev's inference kernel (vendor-pinned, Apache-2.0,
  attributed in [`NOTICE`](NOTICE)) and generalises the serving layer to many engines.
- **[Laya](https://huggingface.co/convaiinnovations/laya)** (Convai Innovations) —
  Apache-2.0, multilingual, one forward pass. Decis uses the official `laya` package.
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
