"""Every environment variable Decis reads, in one place.

`os.environ` must not appear anywhere else in the package (AGENTS.md §2). Import
`Settings` and pass it down instead.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_ENV_FILE = ".env"


class ConfigError(Exception):
    """The configuration is unusable. Raised at startup, never during a request."""


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean (1/0/true/false), got {raw!r}")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _str(name).split(",") if part.strip())


def is_loopback(host: str) -> bool:
    """Whether binding to `host` keeps the server off the network."""
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we cannot resolve here. Treat it as public: the safety check
        # must fail closed, not open.
        return False


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Immutable, and safe to pass anywhere."""

    api_keys: tuple[str, ...] = ()
    allow_no_auth: bool = False
    accept_foreign_defaults: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    default_engine: str = "mock"
    model_dir: Path | None = None
    max_request_bytes: int = 2 * 1024 * 1024
    request_timeout_ms: int = 8000
    torch_threads: int | None = None
    log_level: str = "info"
    log_payloads: bool = False
    env_file: str = DEFAULT_ENV_FILE
    # Names of the DECIS_* variables that were actually set, for `decis doctor`.
    sources: tuple[str, ...] = field(default=())

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    def check_safe_to_serve(self, host: str | None = None) -> None:
        """Refuse configurations that would expose an unauthenticated engine.

        An unsafe default gets deployed to production, so this is a hard failure
        rather than a warning (AGENTS.md §3-19).
        """
        bind = self.host if host is None else host
        if self.auth_enabled or self.allow_no_auth or is_loopback(bind):
            return
        raise ConfigError(
            f"Refusing to serve on {bind} with no API key configured: anyone who can reach "
            "this port could use your models.\n"
            "Fix one of these:\n"
            "  - set DECIS_API_KEY in .env (see .env.example)\n"
            f"  - bind to loopback instead: --host 127.0.0.1\n"
            "  - set DECIS_ALLOW_NO_AUTH=1 if you really mean to expose it"
        )


def load_settings(env_file: str | None = None) -> Settings:
    """Load `.env` (if present) and then read the environment.

    Real environment variables win over `.env`, so `DECIS_PORT=9000 decis serve`
    works the way you would expect.
    """
    path = env_file if env_file is not None else _str("DECIS_ENV_FILE", DEFAULT_ENV_FILE)
    if path and Path(path).is_file():
        load_dotenv(path, override=False)

    model_dir = _str("DECIS_MODEL_DIR")
    torch_threads = _str("DECIS_TORCH_THREADS")

    return Settings(
        api_keys=_csv("DECIS_API_KEY") + _csv("DECIS_API_KEYS"),
        allow_no_auth=_bool("DECIS_ALLOW_NO_AUTH"),
        accept_foreign_defaults=_bool("DECIS_ACCEPT_FOREIGN_DEFAULTS", True),
        host=_str("DECIS_HOST", "0.0.0.0"),
        port=_int("DECIS_PORT", 8000),
        default_engine=_str("DECIS_DEFAULT_ENGINE", "mock"),
        model_dir=Path(model_dir) if model_dir else None,
        max_request_bytes=_int("DECIS_MAX_REQUEST_BYTES", 2 * 1024 * 1024),
        request_timeout_ms=_int("DECIS_REQUEST_TIMEOUT_MS", 8000),
        torch_threads=int(torch_threads) if torch_threads else None,
        log_level=_str("DECIS_LOG_LEVEL", "info"),
        log_payloads=_bool("DECIS_LOG_PAYLOADS"),
        env_file=path,
        sources=tuple(sorted(name for name in os.environ if name.startswith("DECIS_"))),
    )
