"""Every environment variable Decis reads, in one place.

`os.environ` must not appear anywhere else in the package (AGENTS.md §2). Import
`Settings` and pass it down instead.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import MutableMapping
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


#: `DECIS_MODEL_DIR` holds a tree of `<engine id>/` directories. There is deliberately
#: no per-engine environment variable: see `Settings.model_paths` for why the id lives
#: in a value (`--model-path`) rather than in a variable name.
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


# --- the proxy bypass list, which is not a list of URLs --------------------------
#
# `httpx` turns every `NO_PROXY` entry into a `URLPattern` when it builds a client
# (`httpx/_utils.py: get_environment_proxies`). An entry such as `.example.com` is
# *not* a URL, so that translation is guesswork on httpx's side, and one guess is
# wrong: the bracketed IPv6 literal `[::1]` -- the RFC 3986 form of the address, and
# what at least one internal proxy profile exports -- is not recognised as an address
# (`ipaddress.IPv6Address("[::1]")` raises), so it takes the *domain* branch, becomes
# `all://*[::1]`, and kills the client constructor with
# `httpx.InvalidURL: Invalid port: ':1]'` before a single byte is sent.
#
# That lands on Decis because `huggingface_hub` is an httpx client and a cold
# `decis serve` pulls ~647 MiB through it: the engine then fails to load with a
# message that names neither proxies nor the variable, and `/readyz` reports `failed`
# (docs/design-review.md §2-D25). Nothing is wrong with the host's environment -- the
# bypass list is a fact about the network it sits on -- so the fix belongs here, in
# the one module allowed to touch the environment (AGENTS.md §2), and it must run
# before anything can construct an HTTP client.
#
# Five copies of this loop used to live in `tests/conftest.py`, `examples/`,
# `benchmarks/` and a shell probe. Copies are not a fix: they covered the paths that
# had already been debugged and left `decis serve` broken on exactly the machines the
# bypass list exists for.

#: The variables httpx reads. Both spellings: it consults both, and a host may set
#: either.
PROXY_VARIABLES = ("NO_PROXY", "no_proxy")

#: The loopback names any client of *this* project needs to reach directly. `::1` is
#: absent from most proxy profiles; a proxied probe cannot answer for `127.0.0.1`,
#: which is the failure `docs/design-review.md` §2-D19 records.
LOOPBACK_BYPASS = ("127.0.0.1", "localhost", "::1")


def _url_pattern_safe(entry: str) -> str | None:
    """`entry` in the form httpx can turn into a pattern, or None if it cannot.

    Only literals are rewritten. A domain or a wildcard is left exactly as given:
    httpx has a branch for those and we do not know better than the operator.
    """
    host = entry[1:-1] if entry.startswith("[") and entry.endswith("]") else entry
    try:
        address = ipaddress.ip_address(host.split("/")[0])
    except ValueError:
        # Not an address literal (a domain, a wildcard, a CIDR block). httpx handles
        # all three, as long as we do not get in its way.
        return entry
    if address.version == 4:
        return host
    # httpx wraps an IPv6 entry as `all://[<entry>]`, so only the bare address is
    # expressible: `[::1/64]` is a port number as far as urllib is concerned.
    return None if "/" in host else host


def normalize_proxy_environment(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Rewrite the proxy-bypass entries that break every HTTP client here.

    Returns `(variable, before, after)` for each entry that changed, `after == ""`
    meaning the entry was dropped, so a caller can report the edit instead of making it
    behind the operator's back. Idempotent: a second call returns nothing.

    `environ` is a parameter so the rule can be tested without editing the process
    environment; in every real call site it is `os.environ`, because that is what
    `httpx` reads.
    """
    env = os.environ if environ is None else environ
    changed: list[tuple[str, str, str]] = []
    for name in PROXY_VARIABLES:
        value = env.get(name)
        if not value:
            continue
        kept: list[str] = []
        for entry in (part.strip() for part in value.split(",")):
            if not entry:
                continue
            safe = _url_pattern_safe(entry)
            if safe is None:
                changed.append((name, entry, ""))
            else:
                if safe != entry:
                    changed.append((name, entry, safe))
                kept.append(safe)
        env[name] = ",".join(kept)
    return tuple(changed)


def ensure_loopback_bypass(environ: MutableMapping[str, str] | None = None) -> None:
    """Add the loopback names to the proxy-bypass list if they are missing.

    Called by anything that talks to a server on this machine -- the test suite, the
    examples, the benchmark harnesses -- never by the server itself, which only ever
    reaches outwards for weights.
    """
    env = os.environ if environ is None else environ
    for name in PROXY_VARIABLES:
        # `*` already bypasses everything; adding names to it would be noise.
        if env.get(name, "").strip() == "*":
            continue
        entries = [part.strip() for part in env.get(name, "").split(",") if part.strip()]
        for loopback in LOOPBACK_BYPASS:
            if loopback not in entries:
                entries.append(loopback)
        env[name] = ",".join(entries)


#: Where weights can be fetched from. `auto` probes Hugging Face and falls back to
#: ModelScope when it cannot be reached at all; the other two values pin one source, which
#: is what a reproducible build or an air-gapped host wants (`decis/hub.py`).
HUBS = ("auto", "huggingface", "modelscope")


def _hub(name: str) -> str:
    raw = _str(name, "auto").lower()
    if raw not in HUBS:
        raise ConfigError(f"{name} must be one of {', '.join(HUBS)}, got {raw!r}")
    return raw


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Immutable, and safe to pass anywhere."""

    api_keys: tuple[str, ...] = ()
    allow_no_auth: bool = False
    accept_foreign_defaults: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    default_engine: str = "laya-multilingual"
    model_dir: Path | None = None
    #: Per-engine directory overrides: engine id -> directory. Beats `model_dir`.
    #: Filled by the `--model-path ENGINE=PATH` command-line option, **not** by the
    #: environment: an engine id may contain a dot, and no environment variable *name*
    #: can, so encoding the id in the variable name (the old
    #: `DECIS_MODEL_PATH_<ENGINE_ID>`) silently failed for `kev-0.8b` and would have
    #: failed for every Jeff checkpoint. Putting the id in the *value* removes the
    #: restriction entirely. `decis doctor` prints what was resolved.
    model_paths: dict[str, Path] = field(default_factory=dict)
    max_request_bytes: int = 2 * 1024 * 1024
    request_timeout_ms: int = 8000
    #: How long shutdown waits for an in-flight engine load before giving up and
    #: exiting without closing it. Keep it below the orchestrator's
    #: `terminationGracePeriodSeconds`, or SIGKILL arrives mid-wait.
    shutdown_grace_ms: int = 20000
    torch_threads: int | None = None
    #: `cuda`, `xpu`, `npu`, `mps` or `cpu` (`engines.devices.DEVICES`). Unset means "pick
    #: the best available", which is what an operator almost always wants; setting it lets
    #: a GPU host run a CPU-only comparison without a code change. The unset case is
    #: resolved by `engines/devices.py`, by whichever engine needs it: `kev-0.8b` takes the
    #: first accelerator the machine reports, and Laya leaves the choice to its `Agent`.
    device: str | None = None
    #: Force fp32/fp16/bf16 for the served engine. Unset picks per (engine, device) from
    #: `registry.DTYPE_DEFAULTS`. Mainly for re-measuring a dtype on your own hardware --
    #: the defaults exist because some combinations are far worse than others.
    dtype: str | None = None
    #: Which weight hub `decis download` and the loaders fetch from: `auto` (probe Hugging
    #: Face, fall back to ModelScope when it does not answer), or one of the two pinned.
    #: Kept here rather than in the downloader because it is configuration, and because
    #: "which hub" must be answerable before anything is imported (AGENTS.md §2).
    hub: str = "auto"
    #: `HF_ENDPOINT`, if the host set it. Read here so the reachability probe asks the same
    #: endpoint `huggingface_hub` will use (an internal mirror, `hf-mirror.com`, ...); the
    #: variable itself belongs to `huggingface_hub`, so an empty value means "its default".
    hf_endpoint: str = ""
    log_level: str = "info"
    env_file: str = DEFAULT_ENV_FILE
    # Names of the DECIS_* variables that were actually set, for `decis doctor`.
    sources: tuple[str, ...] = field(default=())
    # Proxy-bypass entries this process rewrote or dropped, as
    # `(variable, before, after)` with `after == ""` meaning dropped. Empty on a host
    # whose list is already parseable. Recorded rather than logged because the edit
    # happens before logging exists, and an operator who reads the variable back later
    # deserves to know it changed.
    proxy_rewrites: tuple[tuple[str, str, str], ...] = field(default=())

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

    # After `.env`, so a list configured there is fixed too, and before anything can
    # build an HTTP client: a cold start's first act is fetching weights through one.
    proxy_rewrites = normalize_proxy_environment()

    model_dir = _str("DECIS_MODEL_DIR")
    torch_threads = _str("DECIS_TORCH_THREADS")

    return Settings(
        api_keys=_csv("DECIS_API_KEY") + _csv("DECIS_API_KEYS"),
        allow_no_auth=_bool("DECIS_ALLOW_NO_AUTH"),
        accept_foreign_defaults=_bool("DECIS_ACCEPT_FOREIGN_DEFAULTS", True),
        host=_str("DECIS_HOST", "0.0.0.0"),
        port=_int("DECIS_PORT", 8000),
        default_engine=_str("DECIS_DEFAULT_ENGINE", "laya-multilingual"),
        model_dir=Path(model_dir) if model_dir else None,
        max_request_bytes=_int("DECIS_MAX_REQUEST_BYTES", 2 * 1024 * 1024),
        request_timeout_ms=_int("DECIS_REQUEST_TIMEOUT_MS", 8000),
        shutdown_grace_ms=_int("DECIS_SHUTDOWN_GRACE_MS", 20000),
        torch_threads=int(torch_threads) if torch_threads else None,
        device=_str("DECIS_DEVICE") or None,
        dtype=_str("DECIS_DTYPE") or None,
        hub=_hub("DECIS_HUB"),
        hf_endpoint=_str("HF_ENDPOINT"),
        log_level=_str("DECIS_LOG_LEVEL", "info"),
        env_file=path,
        sources=tuple(sorted(name for name in os.environ if name.startswith("DECIS_"))),
        proxy_rewrites=proxy_rewrites,
    )
