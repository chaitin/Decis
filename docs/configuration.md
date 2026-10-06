# Configuration

**English** · [简体中文](configuration.zh-CN.md)

[Documentation index](../README.md#documentation) · [Getting started](getting-started.md) · [Deployment](deployment.md)

Decis reads configuration from `.env` and from the environment. **A variable already set
in the shell wins over `.env`**, so `DECIS_PORT=9000 decis serve` does what it looks like.
`.env.example` is the commented template; `decis doctor` prints what was actually resolved
and which variables were set.

Every server variable on this page is read in one module,
[`src/decis/config.py`](../src/decis/config.py). No other module in the package reads the
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

### Startup order

The engine loads in a background thread, so probes answer while it does and the port is
open before the model can answer. `/readyz` reports `loading`, `/v1/*` is refused, and the
log says both. `--preload` reverses that order — fetch and load first, then bind:

```bash
decis serve --preload
```

Nothing answers during the load then, `/healthz` included, so it is the right trade on a
console and the wrong one wherever a liveness probe must reach the process during a cold
start. [Getting started](getting-started.md#wait-for-readiness) shows what each order
looks like in the log.

## Engine selection and weights

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_DEFAULT_ENGINE` | `laya-multilingual` | The engine `decis serve` loads, and the one that answers requests whose `model` is a "your default" name such as `jev-latest`. `--engine` overrides it. |
| `DECIS_ACCEPT_FOREIGN_DEFAULTS` | `1` | Answer `jev-latest` (the official SDK's default) with the loaded engine instead of 422. This is what makes swapping `base_url` sufficient. The substitution is reported as `decis.requested_model`. |
| `DECIS_MODEL_DIR` | unset | A directory of pre-downloaded weights. Expected layout: `<DECIS_MODEL_DIR>/<engine-id>/`. |
| `DECIS_HUB` | `auto` | Which Hub to fetch from: `auto`, `huggingface` or `modelscope`. See [Which Hub the weights come from](#which-hub-the-weights-come-from). `--hub` overrides it on `serve`, `models`, `doctor` and `download`. |

Weight resolution is a fixed order; the first hit wins, and a local directory beats the
network:

1. The directory given to `--model-path` for this engine (see below).
2. `DECIS_MODEL_DIR/<engine-id>/`.
3. A Hub cache — Hugging Face, or ModelScope when Hugging Face does not answer — downloading
   if needed.

A candidate only counts if it is a *complete* checkpoint: the file the engine's
`WeightSpec.marker` names has to be there, and for a sharded checkpoint every file its
`*.safetensors.index.json` lists has to be there too. A directory that fails either check is
treated as absent, so a download you interrupted half way falls through to step 3 instead of
being served and then failing to load.

```bash
uv run decis download --engine laya-multilingual                  # the Hub cache of the source it picks
uv run decis download --engine laya-multilingual --dest ./models  # into ./models/laya-multilingual/
DECIS_MODEL_DIR=./models uv run decis serve
```

### Which Hub the weights come from

A fetch tries Hugging Face and falls back to [ModelScope] when the Hugging Face endpoint
cannot be reached *at all*: a DNS failure, a refused connection or a timeout. An HTTP error
status is not "unreachable" — a 401 or a 404 still proves the host answered — so a private or
authenticated mirror is never misread as a blocked one. The fallback is for networks where
Hugging Face is blocked; where it is reachable, nothing changes.

The decision covers **every** download a command makes, not just the first one: a kev fetch
resolves its adapter *and* the base model behind it through the same choice, and both are
handed to the loaders as directories. So if `decis download --engine kev-0.8b --hub modelscope`
prints `source modelscope`, neither the adapter nor the base comes from Hugging Face.

```bash
uv run decis doctor                      # which source a fetch here would use, and why
uv run decis download --engine kev-0.8b  # prints the source before transferring anything
```

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_HUB` | `auto` | `auto`, `huggingface` or `modelscope`. `auto` probes and falls back; a pinned value never probes and never substitutes, so `huggingface` fails loudly where `auto` would switch sources. |
| `HF_ENDPOINT` | `https://huggingface.co` | The endpoint `huggingface_hub` uses **and** the one the probe asks, so an internal mirror (or `https://hf-mirror.com`) is treated as reachable rather than as a blocked Hugging Face. |

[ModelScope]: https://modelscope.cn

**`auto` answers one question: does the endpoint answer?** It does not measure speed, and it
does not try a transfer first. A Hugging Face that answers slowly, throttles large files, or
has only its model *pages* reachable will therefore keep being chosen, and the download either
takes a long time or fails in the client. `decis download` prints which source it picked and
why (`https://huggingface.co answered`), so the line to read is that one — reachable means
no fallback. Force the other source with `--hub modelscope` or `DECIS_HUB=modelscope`.

**A pinned revision cannot be honored on ModelScope.** Its revisions are branch and tag
names, and the mirrors of these repositories carry `master` and nothing else. Handing it the
Hugging Face commit sha does not fail: it logs `No files to download` and returns success
with an empty directory, so the pinned revision becomes a silent no-op. Decis therefore never
sends the sha there. That is not a cosmetic difference: measured on 2026-10-06, the mirror's
`kev-0.8b` adapter is the same size as the pinned commit and **different bytes**
(`adapter_model.safetensors` hashes to `9b908623…` there and `c81d5716…` at the pin), because
a mirror tracks the repository's branch. A fallback is a different download, not the same
download from a closer host. Instead `decis download` prints which source it is about to use, why,
and a warning naming the revision that source cannot honor; `decis doctor` reports what a
fetch would do on this host. `DECIS_HUB=huggingface` turns the whole situation into a
failure, which is what a deployment that needs the pin to mean something should set.

`uv sync --extra download` installs both clients; every engine extra already pulls that in,
and `decis doctor` says which of the two is missing when one is.

```bash
uv run decis doctor                                        # includes a bounded probe of the endpoint
uv run decis download --engine laya-multilingual           # prints the source before transferring
uv run decis download --engine kev-0.8b --hub modelscope   # skip the probe and use ModelScope
```

### Serving one engine from a directory of your own

`--model-path` names a checkpoint directory for one engine, and beats `DECIS_MODEL_DIR` for
it. It is repeatable, and it accepts an engine id or any of its aliases:

```bash
uv run decis serve --engine kev-0.8b --model-path kev-0.8b=/srv/finetunes/acme-triage
uv run decis serve --model-path laya-multilingual=/srv/mine --model-path jeff=/srv/jeff-qwen
```

There is deliberately **no `DECIS_MODEL_PATH_<ENGINE_ID>` variable**. That form had to encode
the engine id in a variable *name*, upper-cased with `_` for `-`, and no variable name can
contain the dot in `kev-0.8b`, `jeff-qwen3.5-0.8b` or `jeff-gemma4-e2b`: the name for
`kev-0.8b` normalises to the id `kev-0-8b`, which is registered as nothing, so the override
was silently dropped. Putting the id in the *value* removes the restriction. `decis doctor`
prints every override that was resolved, and an unknown engine id on the command line is a
configuration error rather than a no-op.

> **A caveat for `kev-0.8b`.** The directory you point at is the **adapter** (the LoRA and
> pointer head), which is the part you fine-tune; the Qwen3.5 **base** model is a second
> repository, declared in `WeightSpec.bases`. `decis download` fetches both, and the engine
> resolves both. With `--dest` (or `DECIS_MODEL_DIR`) the base lands beside the adapter as
> `<dir>/Qwen3.5-0.8B-Base/`; with neither, it goes to the cache of whichever Hub answered
> and is read back from there. The adapter's own metadata names the base repository, and a
> base this build does not declare is left to the loader rather than substituted. The two
> Jeff engines are different: each is a full-weight fine-tune, so one directory holds
> everything and nothing is fetched alongside it.

## Compute

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_DEVICE` | auto | `cpu`, `cuda`, `mps`, `xpu` or `npu`. Unset picks the best available. |
| `DECIS_DTYPE` | per engine+device | Force `fp32`, `fp16` or `bf16`. Read by engines that consult `registry.DTYPE_DEFAULTS` — today that is `kev-0.8b` only. Laya decides its own precision through its `Agent`, and both Jeff checkpoints decide theirs inside the loader (bf16 on an accelerator, fp32 on CPU), so all three ignore this variable. |
| `DECIS_TORCH_THREADS` | one per vCPU | Thread count for the process. |

Unset `DECIS_DEVICE` is resolved by `src/decis/engines/devices.py`: the first accelerator
`torch` reports as available, in the order `cuda`, `xpu`, `npu`, `mps`, otherwise `cpu`.
`npu` needs the `torch_npu` plugin installed; the other four are `torch`'s own. `cpu` is
always available, so a machine with no accelerator serves from it. `kev-0.8b` asks for this
choice; Laya leaves it to its own `Agent`.

Only `cpu`, `cuda` and `mps` have been measured here. `xpu` and `npu` are detected (and
accepted) so that they can be pinned explicitly, but no run in `benchmarks/results/` covers
them, and they fall back to `fp32` through the dtype table's default.

dtype is chosen per engine **and** device, because the same choice can be an order of
magnitude apart on different hardware. `kev-0.8b` on CPU in `bf16` is orders of magnitude
slower than `fp32`; the measured table is in
[Performance](performance.md#dtype-per-engine-and-device) — one observation per dtype, so
read the ratio as an order of magnitude, not as a statistic.
`DECIS_DTYPE` exists mainly to re-measure on your own hardware, and only `kev-0.8b` reads it.
Setting a combination known to perform badly logs a warning rather than refusing to start.

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
the engine, and this container runs no engine — so only the variables below are passed in.

| Variable | Default | Meaning |
|---|---|---|
| `DECIS_PLAYGROUND_HOST` | `0.0.0.0` | Bind address of the playground. |
| `DECIS_PLAYGROUND_PORT` | `8080` | Bind port inside the container; Compose pins it. This is not the host port — that is `DECIS_PLAYGROUND_HOST_PORT` below. |
| `DECIS_PLAYGROUND_WEB_DIR` | `playground/web` | Directory of the static files it serves. |
| `DECIS_PLAYGROUND_TIMEOUT_S` | `120` | How long one proxied `/v1/systemone` call may take. Generous on purpose: a proxy timeout that fires while the engine is still computing reports an error for an answer that was about to arrive. |
| `DECIS_PLAYGROUND_PROBE_TIMEOUT_S` | `2` | How long a `/readyz` probe of one candidate engine may take. |
| `DECIS_PLAYGROUND_UPSTREAM` | unset | Name one engine to try first instead of searching. It is still probed, not trusted. |
| `DECIS_PLAYGROUND_CANDIDATES` | built-in list | Comma-separated `/readyz` candidates, tried in order. Compose sets it to every engine service name (four today) plus `host.docker.internal`. |

The probes bypass any proxy in the environment: a proxied probe would ask the proxy about
`127.0.0.1` and learn nothing about the engine.

## Compose-only variables

These are read by `docker-compose.yml`, not by the server. See [Deployment](deployment.md).

| Variable | Default | Meaning |
|---|---|---|
| `COMPOSE_PROFILES` | `laya-multilingual` | Which engine container `docker compose up` starts. |
| `DECIS_HOST_PORT` | `8000` | Host port for the default engine. |
| `DECIS_KEV_HOST_PORT` | `8001` | Host port for the kev engine. |
| `DECIS_JEFF_QWEN_HOST_PORT` | `8002` | Host port for the jeff-qwen3.5-0.8b engine. |
| `DECIS_JEFF_GEMMA_HOST_PORT` | `8003` | Host port for the jeff-gemma4-e2b engine. |
| `DECIS_PLAYGROUND_HOST_PORT` | `8080` | Host port for the playground, published to `DECIS_PLAYGROUND_PORT` in the container. |
