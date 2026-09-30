# Getting started

**English** · [简体中文](getting-started.zh-CN.md)

[Documentation index](../README.md#documentation) · [Configuration](configuration.md) · [API](api.md)

Decis is a self-hosted HTTP server that answers TypeSafe System One requests with an open
decision model. This page takes you from a clone to a first answer. For deployment, see
[Deployment](deployment.md); for every field on the wire, see the [API reference](api.md).

## Requirements

| | |
|---|---|
| Python | 3.11 or newer, with [uv](https://docs.astral.sh/uv/). The two Jeff engines need 3.12+; `decis models` says so instead of blaming a missing dependency. |
| Disk | ~650 MiB of weights for the default engine, plus the Python environment |
| Memory | A few GiB resident once loaded, and peak RSS is several times the weight files. Size a container from the measured figures in [Performance](performance.md), not from the download size. |
| GPU | optional; everything here runs on CPU |

Docker is an alternative to a source checkout — the published images already carry the
weights. See [Deployment](deployment.md) if you would rather not install Python.

## Run from source

```bash
git clone https://github.com/chaitin/Decis && cd Decis
uv sync --all-extras
cp .env.example .env                               # set DECIS_API_KEY=local, as the samples do
uv run decis download --engine laya-multilingual   # 647 MiB, once
uv run decis serve --host 127.0.0.1 --port 8000
```

Engine dependencies live in extras, so `--all-extras` installs every engine plus the dev
tools. Serving one engine needs only its extra (`uv sync --extra dev --extra laya`); a plain
`uv sync` names none of them and removes the engine dependencies an earlier sync added. See
[Engines](engines.md#installing-an-engine).

Any token value works, but the examples below send `local`, so use that for a first run.

`laya-multilingual` is the default engine. `--host 127.0.0.1` keeps the server on this
machine, where a missing token is allowed by default; it is what you want for a first run.
To expose it on a network, set `DECIS_API_KEY` and read
[Authentication](configuration.md#authentication) first.

`decis download` is optional when `DECIS_MODEL_DIR` is unset and the weights are not cached
yet — but doing it up front separates a slow download from a slow model load, which makes
the first start much easier to read.

### Wait for readiness

On CPU the default engine takes about **75 seconds** to load, and on a slower or ARM host
it can take minutes. The process answers probes the whole time: `/healthz` is up
immediately, and `/readyz` says what is happening.

`decis serve` prints what it is about to do before it binds anything — the engine, where
its weights come from, and the bind address — and then says, in the log, that the engine
is **not ready** yet:

```
decis 0.4.0
  engine    laya-multilingual
  weights   convaiinnovations/laya/multilingual@1c5edc17a7acd8701df6fc341c0d179f1c62c982
            read from the Hugging Face cache; 646.8 MiB on a cold cache
  bind      127.0.0.1:8000
  startup   the socket opens first, so a probe can tell "starting" from "crashed":
            /readyz returns 503 and every /v1/* request is refused until the log says
            "engine laya-multilingual ready". `decis serve --preload` loads first instead.
```

"Uvicorn running on http://127.0.0.1:8000" therefore means *the port is open*, not *the
model answers*. Requests to `/v1/*` are refused with 503 until the engine is ready.

```bash
curl -s localhost:8000/healthz   # {"status":"ok","version":"..."}
curl -s localhost:8000/readyz    # 503 {"status":"loading","engine":"laya-multilingual"}
```

Poll `/readyz` until it returns 200:

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

If you would rather not watch for that — you are on a console, with no probe waiting —
`decis serve --preload` fetches and loads the engine *before* opening the port:

```bash
uv run decis serve --host 127.0.0.1 --preload
```

The port then means "ready to answer", and the log says so in order. The trade-off is
explicit: nothing answers during the load, `/healthz` included, so do **not** use
`--preload` where a liveness probe has to reach the process during a cold start. In a
container or under an orchestrator, keep the default.

Do not point a *liveness* probe at `/readyz`: a probe that fails for the first 75 seconds
would make an orchestrator restart a server that is working. Use `/healthz` for liveness
and `/readyz` for readiness. A load that failed is terminal — `/readyz` returns 503 with
`"status":"failed"` and **no** `retry-after`, because retrying a permanently broken engine
only wastes time.

## Send a request

The point of Decis is that the official SDK needs no change beyond `base_url`:

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# No `model=`: the SDK sends its default, "jev-latest", and Decis answers with the
# engine it is actually running.
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

print(response.model)  # decis/laya-multilingual@<version>
print(response.choices["department"].choice)  # one of the criteria keys
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true), 0..1
```

The same request with `curl`:

```bash
curl -s localhost:8000/v1/systemone \
  -H 'authorization: Bearer local' -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "jev-latest",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/laya-multilingual@<version>",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.87,
                    "probabilities": { "billing": 0.87, "technical": 0.13 } },
    "churn_risk": { "type": "noul", "noul": 0.62 }
  },
  "usage": { "input_tokens": 128, "output_tokens": 3 },
  // Everything Decis adds beyond the contract is namespaced, so the official SDK
  // ignores it.
  "decis": { "engine": "laya-multilingual", "device": "cpu", "dtype": "float32",
             "latency_ms": 231.4, "batch_size": 1 }
}
```

Values depend on the engine and its weights, so run the commands to see your own.
[`examples/curl.md`](../examples/curl.md) walks through every endpoint and all three
question primitives, and CI executes every command in it.

## Check what this machine can run

```bash
uv run decis models     # registered engines, and whether each is usable here
uv run decis doctor     # dependencies, configuration safety, bind address, thread count
```

`decis models` answers one question per engine — can this machine run it — with `ready`,
`deps missing`, `needs weights`, `no weights`, `needs Python 3.12+` or `unavailable`, plus a
remedy such as `uv sync --extra laya` or `decis download --engine laya-multilingual`. So a
missing dependency, weights that were never downloaded, a directory that is not a valid
checkpoint and an interpreter below an engine's floor are all different answers, and none of
them is reported as if the engine were broken. The capacities an engine can accept are not
printed here; they are in
[`GET /v1/models`](api.md#get-v1models).

## Next

| | |
|---|---|
| [Configuration](configuration.md) | Every `DECIS_*` variable, authentication, model paths |
| [API reference](api.md) | Endpoints, request and response schemas, error codes |
| [Engines](engines.md) | What each engine is, its limits, and how to add one |
| [Deployment](deployment.md) | Docker, Compose, `make`, Kubernetes probes |
| [Playground](playground.md) | Three browser games that call a live engine |
