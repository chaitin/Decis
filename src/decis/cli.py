"""`decis` command line.

`serve`, `models`, `doctor` and `download`. `bench` arrives with the Stage 3
scheduler, because a benchmark harness that cannot exercise cross-request batching
would measure something other than what it claims to.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys

from . import __version__
from .app import create_app
from .config import ConfigError, Settings, load_settings
from .engines.registry import SPECS, known_names
from .observability import configure_logging

_EXIT_CONFIG_ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decis", description="One API to run all light-weight decision models.")
    parser.add_argument("--version", action="version", version=f"decis {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default=None, help="bind address (default: DECIS_HOST, 0.0.0.0)")
    serve.add_argument("--port", type=int, default=None, help="bind port (default: DECIS_PORT, 8000)")
    serve.add_argument("--engine", default=None, help="engine to load (default: DECIS_DEFAULT_ENGINE)")
    serve.add_argument("--reload", action="store_true", help="reload on source changes (development only)")
    serve.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env)")
    serve.set_defaults(handler=_serve)

    models = sub.add_parser("models", help="list registered engines and whether they are usable here")
    models.add_argument("--env-file", default=None)
    models.set_defaults(handler=_models)

    doctor = sub.add_parser("doctor", help="check the environment before you deploy")
    doctor.add_argument("--env-file", default=None)
    doctor.set_defaults(handler=_doctor)

    download = sub.add_parser("download", help="fetch model weights ahead of time")
    download.add_argument("--engine", required=True, help="engine id whose weights to fetch")
    download.add_argument("--dest", default=None, help="directory to write to (default: DECIS_MODEL_DIR)")
    download.add_argument("--env-file", default=None)
    download.set_defaults(handler=_download)

    return parser


# --- commands ----------------------------------------------------------------


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _load(args)
    host = args.host or settings.host
    port = args.port or settings.port
    # Refuse to expose an unauthenticated engine. Checked before binding, so an
    # unsafe configuration cannot be reached even briefly.
    settings.check_safe_to_serve(host)

    if args.engine:
        settings = _with(settings, default_engine=args.engine)
    configure_logging(settings.log_level)

    if not settings.auth_enabled:
        print(
            "warning: no DECIS_API_KEY set. Anyone who can reach this port can use your models.\n"
            "         Set one in .env; see .env.example.",
            file=sys.stderr,
        )

    app = create_app(settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=settings.log_level,
        # The engine loads inside the lifespan startup, before the socket accepts
        # traffic. Give a 73 s CPU cold start room rather than letting the
        # orchestrator conclude the container is dead.
        timeout_graceful_shutdown=30,
        reload=args.reload,
    )
    return 0


def _models(args: argparse.Namespace) -> int:
    settings = _load(args)
    from .engines.registry import SPECS, status

    print(f"decis {__version__}  default engine: {settings.default_engine}\n")
    width = max(len(spec.id) for spec in SPECS.values())
    states = {engine_id: status(engine_id) for engine_id in SPECS}
    statwidth = max(len(state.summary) for state in states.values())
    for engine_id in sorted(SPECS):
        state = states[engine_id]
        mark = " " if state.usable else "*"
        note = f"  {state.remedy}" if state.remedy and not state.usable else ""
        print(f" {mark}{engine_id:<{width}}  {state.summary:<{statwidth}}{note}")

    ready = sorted(name for name, state in states.items() if state.usable)
    if ready:
        print(f"\nusable: {', '.join(ready)}")
    if len(ready) < len(states):
        print("* = registered but not runnable here; the line shows what would fix it")
    print("\nModel names you can send as `model`: " + ", ".join(known_names()))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    settings = _load(args)
    problems: list[str] = []

    print(f"decis {__version__}")
    print(f"  python           {sys.version.split()[0]}")
    print(f"  env file         {settings.env_file if settings.env_file else '(none)'}")
    print(f"  bind             {settings.host}:{settings.port}")
    print(f"  default engine   {settings.default_engine}")
    print(f"  request limit    {settings.max_request_bytes} bytes, timeout budget {settings.request_timeout_ms} ms")
    print(f"  torch threads    {settings.torch_threads if settings.torch_threads else 'auto'}")
    print(f"  DECIS_* set      {', '.join(settings.sources) if settings.sources else '(none)'}")

    # Engine availability, reported one by one: a single-engine image is the
    # normal case, not a fault. Uses the same classifier as `decis models`, so the
    # two commands can never disagree about what this server can actually run.
    print("\nengines")
    from .engines.registry import status

    for engine_id in sorted(SPECS):
        state = status(engine_id)
        print(f"  {engine_id:<24} {state.summary:<14} {state.remedy}")

    print("\nserver")
    try:
        settings.check_safe_to_serve()
        if settings.auth_enabled:
            print(f"  auth             enabled ({len(settings.api_keys)} key(s))")
        else:
            print("  auth             DISABLED (loopback or explicitly allowed)")
    except ConfigError as exc:
        problems.append(str(exc))
        print("  auth             UNSAFE")

    if problems:
        print("\n" + "\n\n".join(problems))
        return 1
    print("\nno problems found.")
    return 0


def _download(args: argparse.Namespace) -> int:
    """Fetch one engine's checkpoint, without importing the engine's framework.

    Deliberately independent of torch: the whole point of pre-downloading is to do it
    where the model cannot or should not be loaded -- on a build host, or before a
    container starts. The destination is resolved by the same `paths.resolve` the
    loader uses, so this cannot fetch somewhere the server will not later read.
    """
    from pathlib import Path

    from .engines.registry import canonical
    from .paths import describe_local, download_arguments, filesystem_has_room, human_bytes, resolve

    settings = _load(args)
    engine_id = canonical(args.engine) or args.engine
    if engine_id not in SPECS:
        print(f"error: unknown engine {args.engine!r}. Available: {', '.join(sorted(SPECS))}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    spec = _weight_spec(engine_id)
    if spec is None:
        # Not a success: the requested action did not happen. A script doing
        # `decis download --engine mock && serve` here would be acting on a false
        # premise (it probably meant a different engine id), so exit non-zero with
        # the same code as any other configuration mistake.
        print(
            f"error: {engine_id} has no weights; it is self-contained. Nothing to download.",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR
    if not spec.is_downloadable():
        print(f"error: {engine_id} has no published weights.", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    destination = Path(args.dest) if args.dest else (settings.model_dir or Path("models"))
    configured = _with(settings, model_dir=destination)

    existing = resolve(spec, configured)
    if existing.is_local:
        print(f"{engine_id}: already present at {existing.path} ({describe_local(existing.path)})")
        return 0

    if not has_module("huggingface_hub"):
        print(
            "error: `huggingface_hub` is not installed. It arrives with any engine extra:\n"
            f"  uv sync --extra {SPECS[engine_id].extra or 'laya'}",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR

    destination.mkdir(parents=True, exist_ok=True)
    expected = spec.expected_bytes or 0
    if filesystem_has_room(destination, spec.expected_bytes) is False:
        print(
            f"warning: {destination} may not have room for {human_bytes(expected)}.",
            file=sys.stderr,
        )

    from huggingface_hub import snapshot_download

    print(f"{engine_id}: downloading to {destination} ({human_bytes(expected)}) ...")
    snapshot_download(local_dir=str(destination), **download_arguments(spec))  # type: ignore[arg-type]

    resolved = resolve(spec, configured)
    if not resolved.is_local:
        # Do not report success just because the HTTP calls succeeded: the only
        # thing that matters is whether the *loader* will find a checkpoint, and
        # that is exactly the question `resolve` answers.
        print(
            f"error: download finished but {destination} still has no usable checkpoint. "
            f"Expected to find {spec.marker} there.",
            file=sys.stderr,
        )
        return 1
    print(f"{engine_id}: ready at {resolved.path} ({describe_local(resolved.path)})")
    return 0


def _weight_spec(engine_id: str) -> object:
    """An engine's declared weights, without importing its heavy dependencies.

    `decis.engines.laya` imports nothing but stdlib and Decis modules at module
    scope, which is what makes this possible (AGENTS.md §6).
    """
    from .engines.registry import create

    return create(engine_id).weights()


# --- helpers -----------------------------------------------------------------


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(args.env_file)


def _with(settings: Settings, **changes: object) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, **changes)  # type: ignore[arg-type]


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
