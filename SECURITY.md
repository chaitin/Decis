# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private vulnerability
reporting on the [Security tab](https://github.com/chaitin/Decis/security/advisories/new). If
you cannot use GitHub, open an issue that says only that you have a security report and how
to reach you — no details in the issue itself.

Please include:

- what the problem is and what an attacker can do with it;
- the exact version, image tag or commit you tested;
- a reproduction, if you have one;
- whether you intend to publish it, and on what timeline.

We will acknowledge a report within a few days and tell you what we found. We prefer to fix
first and publish afterwards, and we will credit you unless you ask us not to.

## Supported versions

Decis is pre-1.0. Security fixes land on `master` and in the next release; only the most
recent release image is supported. Pin a versioned tag (`...-v0.3.2` is the current one)
rather than a moving tag in production.

## Security model

Decis is an inference server. What it protects is **access to the model and to the content
you send it**; it is not a sandbox for untrusted model weights.

- **Bearer authentication on every `/v1/*` endpoint.** `/healthz` and `/readyz` are
  intentionally open so orchestrators can probe without a secret.
- **Unsafe defaults fail closed.** With no `DECIS_API_KEY` configured, the server refuses to
  start on a non-loopback address unless `DECIS_ALLOW_NO_AUTH=1` is set explicitly. Binding
  loopback is always allowed.
- **Authentication runs before body validation.** An unauthenticated request with a
  malformed body is 403, not 422, so validation details do not leak to an anonymous caller.
- **Constant-time token comparison**, and 401 versus 403 distinguishes an invalid credential
  from a missing one.
- **Bounded request size** (`DECIS_MAX_REQUEST_BYTES`, 2 MiB by default) and a **bounded
  wait** for the engine (`DECIS_REQUEST_TIMEOUT_MS`, 8 s by default). A request that arrives
  mid-load is refused rather than queued forever.
- **No mutable business state on the request path.** `POST /v1/systemone` is a pure
  function, because the official SDK retries it automatically.
- **The container runs as a non-root user** and needs no external services.
- **Model weights are loaded from a fixed resolution order**; a local directory wins over the
  network, so a deployment can run without outbound connectivity.

## Deliberate exposure: the playground

The playground attaches the API key server-side and proxies `/v1/systemone`, so **anyone who
can reach its port can use the configured model**. Publishing it is the same decision as
publishing the engine's port, without even a token prompt. On a host you do not fully trust,
bind it to loopback:

```bash
DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080
```

## Hardening checklist

- Set `DECIS_API_KEY` and never commit `.env` (it is in `.gitignore`).
- Prefer a reverse proxy that terminates TLS in front of Decis; the server speaks plain HTTP.
- Keep the playground off a public interface unless you mean to expose the model.
- Give the engine container a memory limit sized from measurement — peak memory is several
  times the weight files (see [`docs/performance.md`](docs/performance.md)).
- Use versioned image tags in production, not `latest`.
- Watch for `X-TypeSafe-Retry-Count` in the logs: a client retrying means the server is
  failing or too slow.

## Not in scope

- Vulnerabilities in upstream models or packages (Laya, kev, torch, transformers) — report
  those to their projects; we will still bump our pins when a fix lands.
- Denial of service from a caller who already holds a valid API key. Decis is not a
  multi-tenant rate limiter.
- A model returning a wrong or biased answer. That is model behaviour, not a server
  vulnerability.
