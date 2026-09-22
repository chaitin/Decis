"""Request ids and logging.

The request id is not decoration: the official SDK reads `x-typesafe-request-id`
off every response and raises if a *successful* response is missing it, and it
attaches the header to its error objects for support tickets. So it is generated
for every request and attached to every response, including errors and 404s.
"""

from __future__ import annotations

import contextvars
import json
import logging
import secrets
import time
from typing import Any

REQUEST_ID_HEADER = "x-typesafe-request-id"

# The live API returns ids like `req_01a0c86078e37a7f88f1bee4694b4f1e`: a prefix
# plus 32 lowercase hex characters. We match the format without implementing ULID,
# so clients that parse or length-check the id keep working.
_REQUEST_ID_BYTES = 16

_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("decis_request_id", default=None)
_logger = logging.getLogger("decis")


def new_request_id() -> str:
    return f"req_{secrets.token_hex(_REQUEST_ID_BYTES)}"


def set_request_id(request_id: str) -> None:
    _current_request_id.set(request_id)


def get_request_id() -> str:
    """The current request id, or a fresh one outside a request context."""
    return _current_request_id.get() or new_request_id()


def configure_logging(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def log_request(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    engine: str | None = None,
    batch_size: int | None = None,
    input_tokens: int | None = None,
    retry_count: str | None = None,
    error: str | None = None,
) -> None:
    """One structured line per request.

    `retry_count` comes from the SDK's `X-TypeSafe-Retry-Count` header. A non-zero
    value means the *client* thinks we are failing, which is the most direct
    signal that something is wrong on our side.
    """
    fields: dict[str, Any] = {
        "request_id": get_request_id(),
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    for key, value in (
        ("engine", engine),
        ("batch_size", batch_size),
        ("input_tokens", input_tokens),
        ("retry_count", retry_count),
        ("error", error),
    ):
        if value is not None:
            fields[key] = value

    message = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    if status >= 500:
        _logger.error(message)
    elif status >= 400:
        _logger.warning(message)
    else:
        _logger.info(message)


class Timer:
    """Wall-clock timer for one request."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000
