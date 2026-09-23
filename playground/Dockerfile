# The playground web service: three browser games, plus the proxy that feeds them.
#
#   docker build -f playground/Dockerfile -t decis-playground .
#   docker run --rm -p 8080:8080 -e DECIS_API_KEY=change-me decis-playground
#
# Deliberately *not* built from `docker/Dockerfile`. The playground does not import
# `decis`: it serves four HTML files and forwards `POST /v1/systemone` to whichever
# engine answers `/readyz`, attaching the bearer token so the browser never holds it.
# Its whole dependency tree is the standard library, so its image is a
# `python:3.13-slim` plus about 150 KB of pages -- a few hundred MB smaller than any
# engine image, and the reason it can be the one service that is always running.
#
# There is no `RUN` in this file on purpose: a cross-platform build then executes
# nothing under emulation, so `linux/amd64,linux/arm64` build in seconds.

FROM python:3.13-slim

LABEL org.opencontainers.image.title="Decis playground" \
      org.opencontainers.image.description="Browser games that call the Decis System One API." \
      org.opencontainers.image.url="https://github.com/kingfs/Decis" \
      org.opencontainers.image.source="https://github.com/kingfs/Decis" \
      org.opencontainers.image.documentation="https://github.com/kingfs/Decis/blob/master/README.md" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="docker.io/library/python:3.13-slim"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DECIS_PLAYGROUND_HOST=0.0.0.0 \
    DECIS_PLAYGROUND_PORT=8080

WORKDIR /app

# 65534 is `nobody` in the Debian base. A numeric uid avoids a `useradd` RUN, which is
# what keeps this file free of `RUN` (see above).
COPY --chown=65534:65534 playground/server.py /app/server.py
COPY --chown=65534:65534 playground/web /app/web

USER 65534:65534

EXPOSE 8080

# Liveness only. The process is healthy as soon as it can serve pages, whether or not an
# engine has been found yet -- "no engine" is what `/readyz` reports, and it is the normal
# state for the two minutes a CPU engine is loading. The proxy variables are unset for the
# same reason as the engine image's probe: `urllib` honours `HTTP_PROXY`, and a probe that
# asks a proxy for a loopback URL gets a 502 and reports a healthy process unhealthy
# (docs/design-review.md §2-D19).
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
    python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

# Exec form so the server is PID 1 and receives SIGTERM directly.
CMD ["python", "/app/server.py"]
