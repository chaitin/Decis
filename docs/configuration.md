# Configuration

**English** · [简体中文](configuration.zh-CN.md)

[Documentation index](../README.md#documentation) · [Getting started](getting-started.md) · [Deployment](deployment.md)

Decis reads configuration from `.env` and from the environment. **A variable already set
in the shell wins over `.env`**, so `DECIS_PORT=9000 decis serve` does what it looks like.
`.env.example` is the commented template; `decis doctor` prints what was actually resolved
and which variables were set.

Every server variable on this page is read in one module,
[`src/decis/config.py`](../src/decis/config.py). Nothing else in the package touches the
environment. The playground is a separate process with variables of its own, and the Compose
file has several more; both are listed at the end of this page.

## Authentication

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_API_KEY` | unset | Bearer token(s) clients must send. Comma-separate several to rotate keys without downtime. |
| `DECIS_API_KEYS` | unset | Same, as a second name; the two are concatenated. |
| `DECIS_ALLOW_NO_AUTH` | `0` | Allow serving with no token configured on a non-loopback address. |

Authentication is deliberately two-sided. The live TypeSafe API was probed rather than
assumed, and it distinguishes a **missing** credential from an **invalid** one:

- No `Authorization` header, or a scheme other than `Bearer` → **403**.
- A `Bearer` token that does not match a configured key → **401**.

Both bodies have the same shape, and authentication runs **before** the request body is
validated: an unauthenticated request with a malformed body is 403, not 422. The reason is
security — the other order leaks validation details to an unauthenticated caller. The
comparison is constant-time.

The server refuses to start when it would be unsafe: binding a non-loopback address with no
key configured is a hard error, not a warning, because an unsafe default gets deployed.

```bash
decis serve --host 0.0.0.0                 # .env has DECIS_API_KEY -> fine
decis serve --host 0.0.0.0                 # no key -> refuses to start
decis serve --host 127.0.0.1               # no key, loopback -> allowed
```

## Network

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_HOST` | `0.0.0.0` | Bind address. `--host` overrides it. |
| `DECIS_PORT` | `8000` | Bind port. `--port` overrides it. |
| `DECIS_MAX_REQUEST_BYTES` | `2097152` (2 MiB) | Requests larger than this are rejected with **413**. |

`0.0.0.0` is the right default for a container. `--host 127.0.0.1` is the right default for
a first local run.

## Engine selection and weights

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_DEFAULT_ENGINE` | `laya-multilingual` | The engine `decis serve` loads, and the one that answers requests whose `model` is a "your default" name such as `jev-latest`. `--engine` overrides it. |
| `DECIS_ACCEPT_FOREIGN_DEFAULTS` | `1` | Answer `jev-latest` (the official SDK's default) with the loaded engine instead of 422. This is what makes swapping `base_url` sufficient. The substitution is reported as `decis.requested_model`. |
| `DECIS_MODEL_DIR` | unset | A directory of pre-downloaded weights. Expected layout: `<DECIS_MODEL_DIR>/<engine-id>/`. |
| `DECIS_MODEL_PATH_<ENGINE_ID>` | unset | Point one engine at a directory of its own. Beats `DECIS_MODEL_DIR`. The name is the engine id upper-cased, so it can only address ids without a dot — see the `kev-0.8b` caveat below. |

Weight resolution is a fixed order; the first hit wins, and a local directory beats the
network:

1. `DECIS_MODEL_PATH_<ENGINE_ID>` — e.g. `DECIS_MODEL_PATH_LAYA_MULTILINGUAL=/srv/finetunes/acme-triage`.
2. `DECIS_MODEL_DIR/<engine-id>/`.
3. The Hugging Face cache, downloading if needed.

```bash
uv run decis download --engine laya-multilingual                  # into the HF cache
uv run decis download --engine laya-multilingual --dest ./models  # into ./models/laya-multilingual/
DECIS_MODEL_DIR=./models uv run decis serve
```

> **A caveat for `kev-0.8b`.** The per-engine override cannot address it: the variable name is
> the engine id upper-cased with `_` in place of `-`, and a dot has no representation there, so
> the variable for this engine would normalise to the id `kev-0-8b` — which is registered as
> nothing, and is silently ignored. Use `DECIS_MODEL_DIR` with a `kev-0.8b/` subdirectory
> instead. That directory is the **adapter** (the LoRA and pointer head), which is the part you
> fine-tune; the Qwen3.5 **base** model is referenced by the adapter's checkpoint metadata as a
> Hub repo id, so a separately mounted copy of the base is not picked up. `decis download`
> places the base in the Hugging Face cache, and the engine works offline from there.

## Compute

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_DEVICE` | auto | `cpu`, `cuda` or `mps`. Unset picks the best available. |
| `DECIS_DTYPE` | per engine+device | Force `fp32`, `fp16` or `bf16`. Read by engines that consult `registry.DTYPE_DEFAULTS` — today that is `kev-0.8b` only. Laya decides its own precision through its `Agent` and ignores it. |
| `DECIS_TORCH_THREADS` | one per vCPU | Thread count for the process. |

dtype is chosen per engine **and** device, because the same choice can be an order of
magnitude apart on different hardware. `kev-0.8b` on CPU in `bf16` is 83× slower than
`fp32`; the measured table is in [Performance](performance.md#dtype-per-engine-and-device).
`DECIS_DTYPE` exists mainly to re-measure on your own hardware, and only `kev-0.8b` reads it.
Setting a combination known to be bad logs a warning rather than refusing to start.

`DECIS_TORCH_THREADS` defaults to one thread per vCPU. That is the default, not the
recommendation: in the checked-in sweep the best thread count depends on the request size, so
one setting that is fastest for a single question is not fastest for ten. Measure before
sizing a deployment; the runs are in
[`benchmarks/results/laya-multilingual-sweep.json`](../benchmarks/results/laya-multilingual-sweep.json)
and the table is in [Performance](performance.md#latency).

## Timeouts and shutdown

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_REQUEST_TIMEOUT_MS` | `8000` | How long a request may wait for the engine before it is refused with **429**. Deliberately below the official SDK's 10 s HTTP timeout. |
| `DECIS_SHUTDOWN_GRACE_MS` | `20000` | How long shutdown waits for an in-flight engine load. Keep it below your orchestrator's `terminationGracePeriodSeconds`. |

The official SDK retries on timeout, so a request that outlives 10 s is not just slow — the
client sends it again and doubles the load. Decis therefore bounds the *wait for the engine*
at 8 seconds and answers 429 with `retry-after-ms`. What it cannot do is interrupt a forward
pass that has already started; the budget covers queueing, not computation.

## Logging

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_LOG_LEVEL` | `info` | Standard Python log levels. |

Every request logs one line with method, path, status, duration, engine and the
`x-typesafe-request-id` it returned, so a client-reported id can be found in the server log.
Request and response bodies are not logged: they contain your data.

## Files

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_ENV_FILE` | `.env` | Which env file to load. `--env-file` overrides it per command. |

## Playground variables

The playground is its own process, and it reads `os.environ` directly
([`playground/server.py`](../playground/server.py)) rather than going through `config.py`.
Under Compose it deliberately does **not** get the engine's `env_file` — those values describe
the engine, and this container runs none of it — so only the variables below are passed in.

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_PLAYGROUND_HOST` | `0.0.0.0` | Bind address of the playground. |
| `DECIS_PLAYGROUND_PORT` | `8080` | Bind port inside the container; Compose pins it. This is not the host port — that is `DECIS_PLAYGROUND_HOST_PORT` below. |
| `DECIS_PLAYGROUND_TIMEOUT_S` | `120` | How long one proxied `/v1/systemone` call may take. Generous on purpose: a proxy timeout that fires while the engine is still thinking reports an error for an answer that was about to arrive. |
| `DECIS_PLAYGROUND_PROBE_TIMEOUT_S` | `2` | How long a `/readyz` probe of one candidate engine may take. |
| `DECIS_PLAYGROUND_UPSTREAM` | unset | Name one engine to try first instead of searching. It is still probed, not trusted. |
| `DECIS_PLAYGROUND_CANDIDATES` | built-in list | Comma-separated `/readyz` candidates, tried in order. Compose sets it to the two engine service names plus `host.docker.internal`. |

The probes bypass any proxy in the environment: a proxied probe would ask the proxy about
`127.0.0.1` and learn nothing about the engine.

## Compose-only variables

These are read by `docker-compose.yml`, not by the server. See [Deployment](deployment.md).

| Variable | Default | Meaning |
|---|---|---|
| `COMPOSE_PROFILES` | `laya-multilingual` | Which engine container `docker compose up` starts. |
| `DECIS_HOST_PORT` | `8000` | Host port for the default engine. |
| `DECIS_KEV_HOST_PORT` | `8001` | Host port for the kev engine. |
| `DECIS_PLAYGROUND_HOST_PORT` | `8080` | Host port for the playground, published to `DECIS_PLAYGROUND_PORT` in the container. |
