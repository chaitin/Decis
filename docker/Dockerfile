# Decis API server.
#
# One image per engine, because engine dependencies conflict and are large:
#
#   docker build -f docker/Dockerfile --build-arg DECIS_EXTRAS=laya \
#     --build-arg DECIS_ENGINE=laya-multilingual -t decis:laya-multilingual .
#
# The image installs no engine extra unless you ask for one, so the bare build is an
# API-only image: it starts and answers `/healthz` and `/v1/models`, but it cannot serve
# a request until an engine's dependencies are present. An engine image that also works
# with no network at run time bakes the weights in:
#
#   --build-arg DECIS_EXTRAS=laya \
#   --build-arg DECIS_ENGINE=laya-multilingual \
#   --build-arg DECIS_PREDOWNLOAD=laya-multilingual
#
# The multi-architecture matrix that publishes these to a registry is Stage 4
# (docs/design.md §12); this file only has to be able to produce one.

# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS base

# OCI image annotations (https://github.com/opencontainers/image-spec/blob/main/annotations.md)
LABEL org.opencontainers.image.title="Decis" \
      org.opencontainers.image.description="One API to run all light-weight decision models." \
      org.opencontainers.image.url="https://github.com/kingfs/Decis" \
      org.opencontainers.image.source="https://github.com/kingfs/Decis" \
      org.opencontainers.image.documentation="https://github.com/kingfs/Decis/blob/master/README.md" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="docker.io/library/python:3.13-slim"

# Which engine's dependencies to install. Empty means none: an API-only image.
ARG DECIS_EXTRAS=""
# The engine this image serves by default. It must be an id the registry knows, even
# when no extra is installed: `/v1/models` lists it without importing its framework.
ARG DECIS_ENGINE="laya-multilingual"
# An engine id whose weights to bake in. Empty means "mount them instead".
ARG DECIS_PREDOWNLOAD=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Weight downloads land here, under <engine id>/, which is exactly where
    # `paths.resolve` looks first.
    DECIS_MODEL_DIR=/models \
    HF_HOME=/models/.hf \
    DECIS_DEFAULT_ENGINE=${DECIS_ENGINE} \
    # Overridable at run time; the container must be able to listen on 0.0.0.0, and a
    # token is required before it will.
    DECIS_HOST=0.0.0.0 \
    DECIS_PORT=8000

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

WORKDIR /app

# Dependency layer first so that editing source does not reinstall the world.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN if [ -n "$DECIS_EXTRAS" ]; then \
        uv pip install --system --no-cache ".[$DECIS_EXTRAS]"; \
    else \
        uv pip install --system --no-cache .; \
    fi \
    && rm -rf /app/src/*.egg-info

# Fail the build, not the deployment, if the wiring is broken. `doctor` exits non-zero
# when no API key is configured, which is expected at build time, so its status is
# ignored while its output is kept.
RUN decis doctor || true
RUN python -c "from decis.app import create_app; create_app(load_engine=False); print('app builds')"

# Bake weights when asked. Done as its own layer so that changing source does not
# refetch hundreds of megabytes, and *before* the non-root user exists so the files are
# owned correctly afterwards. No weights are ever committed to git (AGENTS.md §9).
RUN if [ -n "$DECIS_PREDOWNLOAD" ]; then \
        mkdir -p /models && decis download --engine "$DECIS_PREDOWNLOAD"; \
    else \
        mkdir -p /models; \
    fi

# No VOLUME declaration on purpose. `/models` is declared as a volume only for images
# that lack weights, and a volume hides whatever was baked into the image behind it --
# which would silently turn an offline image into one that reaches for the Hub. Use
# `-v /srv/models:/models` to mount weights; that works without the declaration.

# Non-root, with a fixed uid so a mounted volume can be chowned predictably.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin decis \
    && chown -R 10001:10001 /models /app
USER 10001:10001

EXPOSE 8000

# Liveness only -- do not point this at /readyz. The engine loads on a worker thread, so
# /healthz answers within a second of start and a Kubernetes liveness probe with default
# settings is safe. The start period is kept generous anyway because the *first* response
# still waits for the process to finish importing torch, which is slow on a cold page
# cache. Measured cold start for laya-multilingual: 75-90 s on CPU
# (docs/design-review.md §2-D7).
HEALTHCHECK --interval=10s --timeout=3s --start-period=180s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# Exec form so the server is PID 1 and receives SIGTERM directly.
CMD ["decis", "serve"]
