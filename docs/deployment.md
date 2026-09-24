# Deployment

**English** · [简体中文](deployment.zh-CN.md)

[Documentation index](../README.md#documentation) · [Configuration](configuration.md) · [Getting started](getting-started.md)

Decis ships **one image per engine**, because engine dependencies conflict and are large.
Every engine-tagged image carries its checkpoint, so a container needs no network, no volume
and no second service on first start. The `-runtime` variant and the `playground` image are
the deliberate exceptions, and are named as such below.

## Published images

All engines share one Docker Hub repository; the engine is the tag:

| Tag | What it carries | Compressed size |
|---|---|---|
| `kingfs/decis:laya-multilingual` (= `:latest`) | The default engine and its 647 MiB checkpoint | 4.4 GB amd64 / 4.5 GB arm64 |
| `kingfs/decis:kev-0.8b` | The kev adapter and its Qwen3.5 base, baked in | 6.0 GB / 6.2 GB |

`kingfs/decis:playground` is the third image in the same repository, built and pushed by the
same workflow: three browser games and the proxy that fronts them, with no model weights and
no `RUN` in its Dockerfile. `make build-playground` builds the same tag from this checkout in
seconds, which is what the source tree's Compose override does.

Sizes are the compressed download size, summed from the registry manifests of the tags
above. They are not part of `benchmarks/results/` and `report.py` does not regenerate them,
so treat them as an indication of pull size rather than as a benchmark.

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me kingfs/decis:laya-multilingual
```

`laya-multilingual` is the only tag that also gets a bare `latest`, so `docker pull
kingfs/decis` gives you the default engine. Every tag is a multi-arch manifest covering
`amd64` and `arm64`. The registered `laya` (English) and `laya-typed-decisions` checkpoints
have no image; run those from a source checkout.

Releases also publish versioned tags, which do not move: `laya-multilingual-v0.2.0` and
`kev-0.8b-v0.2.0` are what `v0.2.0` published, `laya-multilingual-v0.1.0` and
`kev-0.8b-v0.1.0` what `v0.1.0` did, and `kev-0.8b-v0.0.1` / `laya-multilingual-v0.0.1` the
release before that. A versioned tag is created when the matching `v*` git tag is pushed, and
the engine-named tags keep moving with every push to `master`.

### Bringing your own weights

To run a different checkpoint without building an image, mount a directory that already
holds `<engine-id>/`:

```bash
# /srv/models/laya-multilingual/multilingual/... must already exist
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models kingfs/decis:laya-multilingual
```

> **Do not mount an empty directory at `/models`.** A mount there hides the baked weights —
> a named volume is seeded from the image once and then keeps its own copy, and a bind mount
> replaces the directory outright. The container then quietly downloads what you thought it
> already had. `docker-compose.yml` mounts nothing there for this reason.

### Weightless images

For a deployment that keeps one copy of the weights on a shared volume, or must keep every
node's image small, releases also publish a weightless variant named
`<engine>-runtime-<version>` (3.2 GB amd64 / 3.3 GB arm64). It has no weights baked in, so
fill the volume once and mount it from then on:

```bash
docker pull kingfs/decis:laya-multilingual-runtime-v0.2.0
docker run --rm -v decis-models:/models kingfs/decis:laya-multilingual-runtime-v0.2.0 \
  decis download --engine laya-multilingual
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v decis-models:/models \
  kingfs/decis:laya-multilingual-runtime-v0.2.0
```

The volume must be filled before the service starts: this image has nothing to fall back on.
`v0.2.0` is the current version, and pinning it is the point of this variant — the
engine-named tags move with every push to `master`.

## Docker Compose

`docker-compose.yml` runs the published image and nothing else: no prefetch container, no
volume, no download. The profile selects the engine, and the profile name is the engine id.

```bash
cp .env.example .env                                # set DECIS_API_KEY
docker compose -f docker-compose.yml up -d --wait   # laya-multilingual on :8000
docker compose --profile kev-0.8b up -d             # kev on :8001
docker compose up -d laya-multilingual              # the engine alone, no playground
```

`--wait` returns when the engine can actually answer: the Compose-level probe asks
`/readyz`, so it waits out the CPU cold start (see the latency table in
[Performance](performance.md)). The image's own `HEALTHCHECK` stays on `/healthz`, because a
liveness probe must not fail while the engine is loading. If you would rather not use
`--wait`:

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

Naming a service starts that service alone. The engine profile also includes the playground,
so `docker compose up` brings up both; the last command above selects the engine by name when
you do not want the games.

The `-f docker-compose.yml` is not decoration. Inside a checkout, Compose also loads
`docker-compose.override.yml` by filename and builds the same services from your working
tree into `decis-local:*`. Leave `-f` off to develop against your edit; a deployment copies
`docker-compose.yml` alone.

One engine per container is deliberate: both engines resident together hold several GB in
memory, which is why the default profile starts only one.

## `make` shortcuts

`make` wraps the Compose files, so there is nothing to retype:

```bash
make help                    # every target, and the engine this checkout resolves to

make up                      # build decis-local:* from this tree, run one engine + the games
make down                    # stop; the images stay

make build-playground        # seconds: that Dockerfile has no RUN
make up-playground           # the games only, beside an engine already running anywhere
make build-engine ENGINE=kev-0.8b
make up-engine    ENGINE=kev-0.8b

make ps / logs / images / config   # what is running, and the images it came from
make pull                    # the deployment path: the published kingfs/decis:* images
make test / lint
```

The `Makefile` writes down no image tag, engine id or build argument: it asks Compose. That
is why `make up` and `docker compose up` cannot drift apart, and why
`COMPOSE_PROFILES=kev-0.8b make build-engine` builds kev — the shell overrides `.env` for
Compose, so it does for `make` too.

## Building an image

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

| Build argument | Effect |
|---|---|
| `DECIS_EXTRAS` | Which engine extra to install. Unset produces an API-only image: it starts and answers `/healthz` and `/v1/models`, but `/readyz` stays 503. |
| `DECIS_ENGINE` | Which engine the image is configured to serve. |
| `DECIS_PREDOWNLOAD` | Which engine's weights to bake in. Unset produces the weightless variant. |

On a network that needs a proxy, pass it to the **build** as well. `env_file` only reaches
containers, and Docker forwards neither `.env` nor your shell's `HTTP_PROXY` into a `RUN` —
so the weight download fails with `Network is unreachable` while the dependency install may
still succeed:

```bash
docker compose build --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY"
```

The container runs as a non-root user, needs no external services — no Redis, no Postgres,
no Celery — and refuses to start on a public address with no token configured.

## Kubernetes

Two probes, and they are not interchangeable:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 5
  failureThreshold: 60        # the model may take ~2 minutes to load
resources:
  requests: { memory: 3Gi, cpu: "2" }   # the model is resident once loaded, not a thin API
  limits:   { memory: 6Gi }
terminationGracePeriodSeconds: 30   # > DECIS_SHUTDOWN_GRACE_MS
```

- `/healthz` means "the process is up". Use it for liveness only.
- `/readyz` means "this instance can answer". Pointing liveness at it turns every cold start
  into a restart loop.
- A load that failed returns 503 `{"status":"failed"}` with no `retry-after` — that state is
  terminal until the process is replaced, so alert on it rather than retrying.
- The memory values above are placeholders. The engine is resident after load and its peak is
  several times the weight files, so size the request and limit from the measured table in
  [Performance](performance.md), per engine.

Peak memory is several times the weight files (see [Performance](performance.md)); size the
limit from measurement, not from the download size.

## Security notes

- The server refuses to start on a non-loopback address with no token configured, unless
  `DECIS_ALLOW_NO_AUTH=1` is set explicitly. See
  [Authentication](configuration.md#authentication).
- The playground attaches your API key server-side and proxies `/v1/systemone`, so **anyone
  who can reach its port can use the model**. That is the same decision as publishing the
  engine's port, without even a token prompt. Bind it to loopback on a host you do not
  trust: `DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080`.
- Every image tag is built by the workflow in
  [`.github/workflows/docker-build.yml`](../.github/workflows/docker-build.yml) and carries
  an SBOM and provenance attestation.
