# Decis API server.
#
# One image per engine, because engine dependencies conflict and are large:
#
#   docker build -f docker/Dockerfile -t decis:mock .
#   docker build -f docker/Dockerfile --build-arg DECIS_EXTRAS=laya -t decis:laya .
#
# The default image carries NO model weights and no engine dependencies. It serves
# the mock engine, which is what CI, the contract tests and `docker run` demos use.
# Per-engine images that bake weights are produced on top of this one (Stage 4).

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

# Which engine's dependencies to install. Empty means none: an API-only image that
# starts in under a second.
ARG DECIS_EXTRAS=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/models/.hf \
    # Overridable at run time; the container must be able to listen on 0.0.0.0,
    # and a token is required before it will.
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

# Fail the build, not the deployment, if the wiring is broken.
RUN decis doctor || true
RUN python -c "from decis.app import create_app; create_app(load_engine=False); print('app builds')"

# Weights are mounted, never baked into the base image (AGENTS.md §9).
VOLUME ["/models"]
ENV DECIS_MODEL_DIR=/models

# Non-root, with a fixed uid so a mounted volume can be chowned predictably.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin decis \
    && mkdir -p /models \
    && chown -R 10001:10001 /models /app
USER 10001:10001

EXPOSE 8000

# Liveness only: /readyz would report unhealthy during a 73 s cold start and get
# the container killed mid-load.
HEALTHCHECK --interval=10s --timeout=3s --start-period=120s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# Exec form so the server is PID 1 and receives SIGTERM directly.
CMD ["decis", "serve"]
