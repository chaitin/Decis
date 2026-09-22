"""`decis` command line.

Kept to the three commands Stage 0 can honour. `download` and `bench` arrive with
the first real engine, when there are weights to fetch and a scheduler worth
measuring -- shipping a command that only prints "not implemented" would be worse
than not shipping it.
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
    from .engines.registry import describe

    print(f"decis {__version__}  default engine: {settings.default_engine}\n")
    width = max(len(spec.id) for spec in SPECS.values())
    for engine_id in sorted(SPECS):
        try:
            info = describe(engine_id)
        except Exception as exc:
            print(f"  {engine_id:<{width}}  unavailable  {_reason(exc)}")
            continue
        marks = ",".join(sorted(info.primitives))
        print(f"  {engine_id:<{width}}  ready        {info.model_id}  [{marks}]")
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
    # normal case, not a fault.
    print("\nengines")
    for engine_id in sorted(SPECS):
        spec = SPECS[engine_id]
        try:
            from .engines.registry import describe

            info = describe(engine_id)
            print(f"  {engine_id:<24} ok        {info.device}/{info.dtype}")
        except Exception as exc:
            print(f"  {engine_id:<24} missing   {_reason(exc)}")
            if spec.extra:
                print(f"  {'':<24}           install with: pip install 'decis[{spec.extra}]'")

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


# --- helpers -----------------------------------------------------------------


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(args.env_file)


def _with(settings: Settings, **changes: object) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, **changes)  # type: ignore[arg-type]


def _reason(exc: Exception) -> str:
    if isinstance(exc, ModuleNotFoundError):
        return f"missing module: {exc.name}"
    return str(exc).splitlines()[0]


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
