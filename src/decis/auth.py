"""Bearer token authentication.

Deliberately small: a static token (or a comma-separated set of them, so keys can
be rotated without downtime) read from `DECIS_API_KEY` / `DECIS_API_KEYS`, or from
`.env`. Comparison is constant-time.

The 401/403 split is not cosmetic -- see `errors.AuthRequiredError`.
"""

from __future__ import annotations

import hmac

from .config import Settings
from .errors import AuthRequiredError, InvalidCredentialError

BEARER = "bearer"


def _token_from_header(authorization: str | None) -> str | None:
    """Extract the token, or None if the header carries no usable credential."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER:
        return None
    return parts[1].strip() or None


def authenticate(authorization: str | None, settings: Settings) -> None:
    """Raise unless the request carries an accepted token.

    No configured keys means authentication is disabled, which `Settings.
    check_safe_to_serve` has already refused to allow on a public address.
    """
    if not settings.auth_enabled:
        return

    supplied = _token_from_header(authorization)
    if supplied is None:
        raise AuthRequiredError("No API key supplied. Send it as: Authorization: Bearer <your token>")

    # compare_digest over every configured key, without an early exit, so that
    # timing does not reveal which key matched or how much of one was correct.
    matched = False
    for expected in settings.api_keys:
        if hmac.compare_digest(supplied, expected):
            matched = True
    if not matched:
        raise InvalidCredentialError("Invalid API key. Check the token you are sending.")
