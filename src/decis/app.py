"""Application wiring: middleware, startup and shutdown.

Two ordering rules are enforced here rather than left to framework defaults, and
both are asserted by tests:

* **Authentication runs before the body is parsed or validated.** The live jev API
  does this, and `tests/test_contract_errors.py` checks that we still do: an
  unauthenticated request with a malformed body must be 403, not 422. Keeping it
  in middleware makes the order structural instead of a property of how FastAPI
  happens to order dependency resolution today.
* **The engine is loaded before the server accepts traffic.** Cold start is 73 s
  for Laya on CPU. `/healthz` stays up throughout so an orchestrator does not kill
  the container, while `/readyz` reports 503 until the engine is usable.
"""

from __future__ import annotations

import logging
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
from .scheduler import InProcessScheduler, Scheduler
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
        scheduler = InProcessScheduler(create(engine_id))

    service = DecisionService(resolved, scheduler)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if load_engine and not service.ready:
            _logger.info("loading engine %s; /readyz reports 503 until this finishes", service.engine_id)
            service.load()
            _logger.info("engine %s ready", service.engine_id)
        try:
            yield
        finally:
            _logger.info("shutting down engine %s", service.engine_id)
            service.close()

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
