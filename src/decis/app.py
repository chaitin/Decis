"""Application wiring: middleware, startup and shutdown.

Three ordering rules are enforced here rather than left to framework defaults, and
all are asserted by tests:

* **Authentication runs before the body is parsed or validated.** The live jev API
  does this, and `tests/test_contract_errors.py` checks that we still do: an
  unauthenticated request with a malformed body must be 403, not 422. Keeping it
  in middleware makes the order structural instead of a property of how FastAPI
  happens to order dependency resolution today.
* **The engine loads in a background thread, and the server starts serving at once.**
  Cold start is ~80 s for Laya on CPU. Loading synchronously inside the lifespan --
  which is what this did until Stage 2 -- kept uvicorn out of its protocol loop
  until the weights were in memory, so *nothing* answered during that window,
  `/healthz` included: a probe hung instead of being told "starting". Now
  `/healthz` is up immediately and `/readyz` reports `loading`, so an orchestrator
  can tell "still starting" from "crashed"; and a load that fails is reported as
  `failed` rather than taking the process down, while still never serving traffic
  it cannot answer. Measured and recorded as D7 in `docs/design-review.md`.
* **Shutdown does not close an engine that is still loading.** Closing a
  half-initialised model races the loader. If the grace period expires we log and
  let process exit reclaim the memory, which is the only option that cannot corrupt
  anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import __version__
from .auth import authenticate
from .config import Settings, load_settings
from .engines.registry import create, resolve_or_raise
from .errors import DecisError, PayloadTooLargeError, error_response, install_error_handlers
from .observability import REQUEST_ID_HEADER, Timer, configure_logging, log_request, new_request_id, set_request_id
from .routes import create_router
from .scheduler import InProcessScheduler, LoadPhase, Scheduler
from .service import DecisionService

_logger = logging.getLogger("decis.app")

#: Paths that require a bearer token. Listed explicitly rather than as "everything
#: under /v1", so that a future unauthenticated route cannot be opened by accident
#: and so an unknown /v1 path still returns 404 instead of 403.
PROTECTED_PATHS = frozenset({"/v1/systemone", "/v1/models"})

CallNext = Callable[[Request], Awaitable[Response]]


def create_app(
    settings: Settings | None = None,
    *,
    scheduler: Scheduler | None = None,
    load_engine: bool = True,
) -> FastAPI:
    """Build the ASGI app.

    `scheduler` and `load_engine` exist for tests: they let a test drive the API
    with a stub engine, and let a test observe the "not ready yet" state.
    """
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    if scheduler is None:
        engine_id = resolve_or_raise(resolved.default_engine)
        # The engine's request budget comes from settings, so the invariant is a
        # property of configuration rather than a constant buried in the scheduler.
        scheduler = InProcessScheduler(create(engine_id), request_timeout_ms=resolved.request_timeout_ms)

    service = DecisionService(resolved, scheduler)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loader: threading.Thread | None = None
        if load_engine and not service.ready and service.load_status.phase is LoadPhase.IDLE:
            loader = threading.Thread(
                target=_load_engine,
                args=(service,),
                name=f"decis-load-{service.load_status.engine_id}",
                daemon=True,
            )
            loader.start()
        try:
            yield
        finally:
            _shutdown(service, loader)

    app = FastAPI(
        title="Decis",
        version=__version__,
        summary="One API to run all light-weight decision models.",
        description=(
            "A drop-in implementation of the TypeSafe / jev System One API, backed by open "
            "decision models. Point the official `typesafe-sdk` here by setting "
            "`TYPESAFE_BASE_URL`."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.service = service

    install_error_handlers(app)
    app.include_router(create_router(service))
    app.middleware("http")(_context_middleware(service))
    return app


def _load_engine(service: DecisionService) -> None:
    """Load the engine on a worker thread. Never raises.

    A failure is recorded on the service and logged; the process stays up so
    `/readyz` can report *why* it is unusable. That is the whole point of not
    loading synchronously: a container that exits during startup leaves an
    operator with a crash loop and a log line, while `failed` is visible to the
    same probe the orchestrator is already polling.
    """
    engine_id = service.load_status.engine_id
    _logger.info("loading engine %s in the background; /readyz reports 503 until it finishes", engine_id)
    started = time.perf_counter()
    try:
        service.load()
    except Exception:
        _logger.exception(
            "engine %s failed to load; /readyz will report 'failed' and every request will be refused",
            engine_id,
        )
        return
    _logger.info("engine %s ready after %.1fs", engine_id, time.perf_counter() - started)


def _shutdown(service: DecisionService, loader: threading.Thread | None) -> None:
    """Drain an in-flight load, then release the engine."""
    if loader is not None and loader.is_alive():
        grace_s = service.settings.shutdown_grace_ms / 1000
        _logger.info("waiting up to %.1fs for the in-flight engine load to finish", grace_s)
        loader.join(grace_s)
        if loader.is_alive():
            # Closing now would race the loader on a half-built model. Exiting
            # without closing leaks nothing that matters: the process is ending.
            _logger.warning(
                "engine load still running after %.1fs; shutting down without closing it "
                "(process exit will reclaim the memory)",
                grace_s,
            )
            return
    _logger.info("shutting down engine %s", service.load_status.engine_id)
    service.close()


def _context_middleware(service: DecisionService) -> Callable[[Request, CallNext], Awaitable[Response]]:
    """Request id, size limit, auth, access log -- in that order."""

    async def middleware(request: Request, call_next: CallNext) -> Response:
        request_id = new_request_id()
        set_request_id(request_id)
        timer = Timer()

        rejection = _reject_early(request, service)
        if rejection is not None:
            response, reason = rejection
            response.headers[REQUEST_ID_HEADER] = request_id
            _log(request, response, timer, error=reason)
            return response

        status = 500
        reason: str | None = None
        try:
            response = await call_next(request)
            status = response.status_code
            if status >= 400:
                reason = f"HTTP {status}"
        except DecisError as exc:
            response = error_response(exc)
            status = exc.status
            reason = exc.message
        except Exception as exc:
            _logger.exception("unhandled error serving %s %s", request.method, request.url.path)
            response = error_response(
                DecisError(f"Internal error: {type(exc).__name__}. Quote request id {request_id} when reporting this.")
            )
            reason = type(exc).__name__

        response.headers[REQUEST_ID_HEADER] = request_id
        _log(request, response, timer, service=service, error=reason)
        return response

    return middleware


def _reject_early(request: Request, service: DecisionService) -> tuple[Response, str] | None:
    """Checks that must happen before the body is read or validated.

    Returns the response to send instead of dispatching, plus a reason for the log.
    Authentication comes first: an unauthenticated caller must not be able to learn
    anything about our limits, our validation, or our model list.
    """
    if request.url.path in PROTECTED_PATHS:
        try:
            authenticate(request.headers.get("authorization"), service.settings)
        except DecisError as exc:
            return error_response(exc), exc.message

    declared = request.headers.get("content-length")
    limit = service.settings.max_request_bytes
    if declared and declared.isdigit() and int(declared) > limit:
        return (
            error_response(
                PayloadTooLargeError(
                    f"Request body is {int(declared)} bytes; the limit is {limit}. "
                    "Shorten `state` or raise DECIS_MAX_REQUEST_BYTES."
                )
            ),
            "request_too_large",
        )

    return None


def _log(
    request: Request,
    response: Response,
    timer: Timer,
    *,
    service: DecisionService | None = None,
    error: str | None = None,
) -> None:
    log_request(
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=timer.elapsed_ms,
        engine=service.engine_id if service is not None else None,
        retry_count=request.headers.get("x-typesafe-retry-count"),
        error=error,
    )


def build_default_app() -> FastAPI:
    """What `decis serve` runs: load settings, refuse unsafe ones, build the app."""
    settings = load_settings()
    settings.check_safe_to_serve()
    return create_app(settings)


__all__ = ["PROTECTED_PATHS", "build_default_app", "create_app"]
