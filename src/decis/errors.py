"""Exceptions and the two error body shapes the wire contract allows.

Only two shapes ever go on the wire (see docs/api-compatibility.md §5):

1. ``{"detail": {"error_type": ..., "message": ...}}`` for everything that is not
   a request-validation failure -- auth, rate limiting, overload, server faults.
   This is the shape the live jev API returns for 401/403.
2. ``{"detail": [{"loc": [...], "msg": ..., "type": ...}]}`` for validation
   failures, which is FastAPI/Pydantic's default and the only error shape the
   official OpenAPI actually documents.

Messages are written for the person reading the error: one sentence saying what
is wrong, and where it helps, what to do about it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .observability import REQUEST_ID_HEADER, get_request_id


class DecisError(Exception):
    """An error with a defined HTTP status and wire body."""

    status: int = 500
    error_type: str = "server_error"

    def __init__(self, message: str, *, status: int | None = None, error_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if error_type is not None:
            self.error_type = error_type

    def body(self) -> dict[str, Any]:
        return {"detail": {"error_type": self.error_type, "message": self.message}}


class AuthRequiredError(DecisError):
    """No usable credentials were supplied.

    403, not 401: the live jev API distinguishes "you sent nothing" from "what you
    sent is wrong", and the official SDK maps the two to different exception
    classes. See docs/contract/observations-2026-09-22.md §2.
    """

    status = 403
    error_type = "authentication_error"


class InvalidCredentialError(DecisError):
    """A bearer token was supplied but it is not valid. 401."""

    status = 401
    error_type = "authentication_error"


class PayloadTooLargeError(DecisError):
    """The request body exceeds DECIS_MAX_REQUEST_BYTES. 413."""

    status = 413
    error_type = "request_too_large"


class EngineOverloadedError(DecisError):
    """The engine queue is full. 429 -- retryable, so callers should back off."""

    status = 429
    error_type = "rate_limit_error"

    def __init__(self, message: str, *, retry_after_ms: int = 1000) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class EngineUnavailableError(DecisError):
    """The engine is not loaded. 503."""

    status = 503
    error_type = "engine_unavailable"


class EngineTimeoutError(DecisError):
    """Inference did not finish inside the request budget. 504."""

    status = 504
    error_type = "timeout_error"


class EngineFailedError(DecisError):
    """The engine raised while running. 500, with the engine's own words."""

    status = 500
    error_type = "engine_error"


class InvalidRequestError(DecisError):
    """A request that Pydantic cannot catch, reported in the validation shape.

    Used for things only the engine layer knows: an unknown model name, a choice
    question with no options, a state longer than the engine supports. The status
    and body match a Pydantic validation failure so that clients (including the
    official SDK) treat it identically, while the message says plainly what is
    wrong.
    """

    status = 422

    def __init__(self, message: str, *, loc: list[str | int] | None = None, type: str = "value_error") -> None:
        super().__init__(message)
        self.loc = loc or []
        self.error_type = type

    def body(self) -> dict[str, Any]:
        return {"detail": [{"loc": self.loc, "msg": self.message, "type": self.error_type}]}


def error_response(error: DecisError) -> JSONResponse:
    """Render an error with the request id header attached."""
    headers = {REQUEST_ID_HEADER: get_request_id()}
    if isinstance(error, InvalidCredentialError):
        # RFC 9110 §15.5.2: a 401 MUST carry WWW-Authenticate. The live jev API
        # omits it; adding a response header cannot break a client, so we follow
        # the standard here. See docs/api-compatibility.md §5.
        headers["www-authenticate"] = 'Bearer error="invalid_token", error_description="The API key is not valid."'
    if isinstance(error, EngineOverloadedError):
        # Without this the official SDK falls back to exponential backoff and
        # hammers a server that is already overloaded (AGENTS.md §3-16).
        headers["retry-after-ms"] = str(error.retry_after_ms)
        headers["retry-after"] = str(max(1, error.retry_after_ms // 1000))
    return JSONResponse(status_code=error.status, content=error.body(), headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Map exceptions onto the two contract shapes."""

    @app.exception_handler(DecisError)
    async def _decis_error(_: Request, exc: DecisError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI already produces this shape; we only make sure the request id
        # header survives, since the SDK reads it on failures too.
        return JSONResponse(
            status_code=422,
            content={"detail": _clean(exc.errors())},
            headers={REQUEST_ID_HEADER: get_request_id()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak a traceback to the client. The log has the detail.
        return error_response(
            DecisError(f"Internal error: {type(exc).__name__}. Quote the request id when reporting this.")
        )


def _clean(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only JSON-serialisable validation fields, in the documented order.

    Pydantic can attach a non-serialisable `ctx` (for example a raised exception),
    which would make the error response itself fail to encode.
    """
    cleaned: list[dict[str, Any]] = []
    for entry in errors:
        item: dict[str, Any] = {
            "loc": list(entry.get("loc", ())),
            "msg": entry.get("msg", "Invalid value."),
            "type": entry.get("type", "value_error"),
        }
        if "input" in entry:
            try:
                import json

                json.dumps(entry["input"])
            except (TypeError, ValueError):
                pass
            else:
                item["input"] = entry["input"]
        cleaned.append(item)
    return cleaned
