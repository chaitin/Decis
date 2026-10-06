"""`decis` command line.

`serve`, `models`, `doctor`, `download` and `bench`.

`bench` is a thin wrapper around `benchmarks/run.py` rather than a second
implementation: the measurement code has to be the same code whose output
`benchmarks/report.py` renders, or the numbers in the docs would come from somewhere
other than the runner (`AGENTS.md §8`). It measures **within-request** batching only --
cross-request batching needs the Stage 3 scheduler, and the raw JSON says so.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .app import create_app
from .config import HUBS, ConfigError, Settings, load_settings
from .engines.registry import SPECS, canonical, known_names
from .hub import Selection
from .observability import configure_logging
from .paths import WeightSpec

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
    serve.add_argument(
        "--preload",
        action="store_true",
        help=(
            "fetch and load the engine before binding the port, so the server only starts once it can "
            "answer. Use it when you are watching the console; do NOT use it where a liveness probe has "
            "to reach /healthz during a cold start."
        ),
    )
    serve.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env)")
    _add_model_path(serve)
    _add_hub(serve)
    serve.set_defaults(handler=_serve)

    models = sub.add_parser("models", help="list registered engines and whether they are usable here")
    models.add_argument("--env-file", default=None)
    _add_model_path(models)
    _add_hub(models)
    models.set_defaults(handler=_models)

    doctor = sub.add_parser("doctor", help="check the environment before you deploy")
    doctor.add_argument("--env-file", default=None)
    _add_model_path(doctor)
    _add_hub(doctor)
    doctor.set_defaults(handler=_doctor)

    download = sub.add_parser("download", help="fetch model weights ahead of time")
    download.add_argument("--engine", required=True, help="engine id whose weights to fetch")
    download.add_argument(
        "--dest",
        default=None,
        help="model directory; weights land in <dest>/<engine id>/ (default: DECIS_MODEL_DIR, else the hub's cache)",
    )
    download.add_argument("--env-file", default=None)
    _add_hub(download)
    download.set_defaults(handler=_download)

    bench = sub.add_parser("bench", help="measure latency and write raw JSON (see AGENTS.md §8)")
    bench.add_argument("--engine", default=None, help="engine id to measure (default: the configured one)")
    bench.add_argument("--threads", default="", help="comma-separated torch thread counts; default: one per vCPU")
    bench.add_argument("--batch", default="1,3,10,30", help="comma-separated questions per request")
    bench.add_argument("--iterations", type=int, default=8, help="measured samples per configuration")
    bench.add_argument("--warmup", type=int, default=2, help="discarded calls before measuring")
    bench.add_argument("--out", default="", help="output path (default: benchmarks/results/<engine>-thread-sweep.json)")
    bench.add_argument("--env-file", default=None)
    bench.add_argument(
        "--cross-request",
        action="store_true",
        help="measure whether joining questions from different requests pays (needs the batcher to not exist yet)",
    )
    bench.add_argument(
        "--processes",
        type=int,
        default=1,
        help="with --cross-request: run this many engine processes sharing the thread budget",
    )
    bench.set_defaults(handler=_bench)

    return parser


def _add_hub(parser: argparse.ArgumentParser) -> None:
    """`--hub auto|huggingface|modelscope`, overriding `DECIS_HUB`.

    The same option on every command that can fetch, rather than only on `download`: the
    server fetches too (a cold cache, or a mounted directory that turned out to be
    incomplete), and debugging that with an environment variable but no flag is how the two
    paths come to disagree about which Hub they use.
    """
    parser.add_argument(
        "--hub",
        choices=HUBS,
        default=None,
        help=(
            "where to fetch weights from: auto probes Hugging Face and falls back to ModelScope when it "
            "cannot be reached (default: DECIS_HUB, auto)"
        ),
    )


def _add_model_path(parser: argparse.ArgumentParser) -> None:
    """`--model-path ENGINE=PATH`, repeatable.

    Deliberately a command-line option rather than `DECIS_MODEL_PATH_<ENGINE_ID>`: an
    engine id may contain a dot (`kev-0.8b`, `jeff-qwen3.5-0.8b`), no environment
    variable *name* can, and the old form therefore had to mangle the id -- which meant
    it silently addressed nothing. `DECIS_MODEL_DIR` still covers "a tree of
    `<engine id>/` directories" for deployment, where a flag is awkward.
    """
    parser.add_argument(
        "--model-path",
        action="append",
        default=[],
        metavar="ENGINE=PATH",
        help=(
            "serve ENGINE from the checkpoint in PATH (repeatable). Beats DECIS_MODEL_DIR for that "
            "engine. Example: --model-path kev-0.8b=/srv/kev"
        ),
    )


# --- commands ----------------------------------------------------------------


def _bench(args: argparse.Namespace) -> int:
    """Run a checked-in benchmark harness in this process's interpreter.

    The harnesses live outside the package because they are not part of the served
    artifact. Running one as a subprocess of the same interpreter keeps its two facts
    true: it measures the installed `decis`, and the JSON it writes is exactly what
    `benchmarks/report.py` reads.

    Two harnesses, one entry point, because they answer different questions.
    `run.py` asks "how long does one request take"; `batch_gain.py` asks "is joining
    requests worth it at all" and is the one that must be run *before* building a
    scheduler (`design-review.md §4-M5`). The flag picks the harness rather than a mode
    inside it, and neither is reimplemented here (`AGENTS.md §2`).
    """
    import subprocess

    name = "batch_gain.py" if args.cross_request else "run.py"
    harness = Path(__file__).resolve().parent.parent.parent / "benchmarks" / name
    if not harness.is_file():
        print(
            f"error: {harness} is missing. `decis bench` runs the checked-in harness; it is not "
            f"available in an installed wheel. Use a source checkout.",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR

    settings = _load(args)
    engine = args.engine or settings.default_engine

    if args.cross_request:
        per_process = args.processes
        if per_process < 1:
            print("error: --processes must be at least 1", file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        # The harness takes the thread count *per process*. Dividing the machine's cores
        # across the processes is what keeps the total budget constant: comparing
        # `1 x 24 threads` with `4 x 24 threads` measures thread oversubscription, not
        # process scaling (`AGENTS.md §9`).
        total_threads = int(args.threads.split(",")[0]) if args.threads else (os.cpu_count() or 4)
        command = [
            sys.executable,
            str(harness),
            "--engine",
            engine,
            "--batches",
            args.batch,
            "--iterations",
            str(args.iterations),
            "--warmup",
            str(args.warmup),
            "--threads",
            str(max(1, total_threads // per_process)),
            "--processes",
            str(per_process),
        ]
    else:
        command = [
            sys.executable,
            str(harness),
            "--engine",
            engine,
            "--batch",
            args.batch,
            "--iterations",
            str(args.iterations),
            "--warmup",
            str(args.warmup),
        ]
        if args.threads:
            command += ["--threads", args.threads]
    if args.out:
        command += ["--out", args.out]

    print(f"measuring {engine} (this loads the model and may take minutes)...", file=sys.stderr)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        return completed.returncode
    return 0


def startup_banner(settings: Settings, host: str, port: int, *, preload: bool = False) -> str:
    """What `decis serve` prints before it binds anything.

    Two lines an operator used to see were uvicorn's `Uvicorn running on
    http://127.0.0.1:8000` and, seconds or minutes later, a `/readyz` that still said
    `503`. Both were true and the pair was misleading: the socket really is open, and
    the engine really is not answering yet. So the state is stated before the
    framework gets a chance to imply otherwise, together with where the weights come
    from -- on a cold cache the next thing that happens is a 647 MiB download, and
    that is worth announcing rather than discovering.
    """
    from .engines.registry import canonical
    from .paths import human_bytes, resolve

    engine_id = canonical(settings.default_engine) or settings.default_engine
    spec = _weight_spec(engine_id)
    lines = [f"decis {__version__}", f"  engine    {engine_id}"]
    if spec is None:
        lines.append("  weights   none published; this engine is self-contained")
    else:
        source = resolve(spec, settings)
        lines.append(f"  weights   {source.describe()}")
        if source.kind == "hub":
            size = human_bytes(spec.expected_bytes) if spec.expected_bytes else "size unknown"
            lines.append("            fetched on first use -- from Hugging Face, or from ModelScope when that")
            lines.append(f"            cannot be reached; {size} on a cold cache")
        elif source.kind == "none":
            lines.append("            not a checkpoint, and this engine has nothing to fetch: it will not load")
    lines.append(f"  bind      {host}:{port}")
    if preload:
        lines.append(
            "  startup   --preload: fetching and loading the engine before the port opens, so nothing\n"
            "            answers -- not even /healthz -- until it is ready. A liveness probe that\n"
            "            expects an answer during a cold start will fail; drop --preload for that."
        )
    else:
        lines.append(
            '  startup   the socket opens first, so a probe can tell "starting" from "crashed":\n'
            "            /readyz returns 503 and every /v1/* request is refused until the log says\n"
            f'            "engine {engine_id} ready". `decis serve --preload` loads first instead.'
        )
    return "\n".join(lines)


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

    # `load_engine=False` under `--preload`: the load is driven here, before uvicorn
    # exists, so there is no window in which a port is open and nothing can answer it.
    app = create_app(settings, load_engine=not args.preload)
    # stderr on purpose: uvicorn logs there too, and a banner on a block-buffered stdout
    # would arrive *after* the very line it exists to put in context.
    print(startup_banner(settings, host, port, preload=args.preload), file=sys.stderr)

    if args.preload:
        try:
            app.state.service.load()
        except Exception as exc:
            print(
                f"error: the engine failed to load, so nothing was bound: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=settings.log_level,
        # A cold start is ~80 s for Laya on CPU and minutes on a slow or ARM host, and
        # the engine loads on a worker thread while uvicorn serves probes. Give the
        # shutdown drain room rather than leaving a SIGKILL mid-load.
        timeout_graceful_shutdown=30,
        reload=args.reload,
    )
    return 0


def _models(args: argparse.Namespace) -> int:
    settings = _load(args)
    from .engines.registry import SPECS, status

    print(f"decis {__version__}  default engine: {settings.default_engine}\n")
    width = max(len(spec.id) for spec in SPECS.values())
    states = {engine_id: status(engine_id, settings) for engine_id in SPECS}
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
    paths = ", ".join(f"{engine}={path}" for engine, path in sorted(settings.model_paths.items()))
    print(f"  model paths      {paths if paths else '(none; DECIS_MODEL_DIR and the Hub cache only)'}")
    _report_hub(settings)
    print(f"  DECIS_* set      {', '.join(settings.sources) if settings.sources else '(none)'}")
    # Only when something was actually changed: a silent rewrite of the environment is
    # the kind of thing an operator should be able to see, and a "no changes" line on
    # every run is noise (`docs/design-review.md` §2-D25). The variable is named because
    # the same broken entry usually appears in both spellings, and `[::1] -> ::1` twice
    # with no attribution is not a report.
    for name, before, after in settings.proxy_rewrites:
        replacement = f" -> {after}" if after else " dropped (not expressible as a proxy bypass)"
        print(f"  proxy bypass     {name}: {before}{replacement}")

    # Engine availability, reported one by one: a single-engine image is the
    # normal case, not a fault. Uses the same classifier as `decis models`, and with the
    # settings this run resolved -- a `--model-path`/`DECIS_MODEL_DIR` override has to move
    # this line too, or `doctor` contradicts both its own `model paths` line above and
    # `models` (`docs/design-review.md` §2-D34).
    print("\nengines")
    from .engines.registry import status

    for engine_id in sorted(SPECS):
        state = status(engine_id, settings)
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
    container starts. Both destinations are ones `paths.resolve` reads: an explicit
    model directory (`<dir>/<engine id>/`) or, with none configured, the Hub cache.
    So this cannot fetch somewhere the server will not later look.

    Which Hub is decided by `decis/hub.py` and printed before anything is transferred: on a
    network that cannot reach Hugging Face the fallback fetches from ModelScope, and a
    user has to be told that before 647 MiB arrive -- especially since the pinned commit
    cannot be honored there (`docs/design-review.md` §2-D32).
    """
    from .engines.registry import canonical
    from .hub import HubUnavailableError, choose, client_installed
    from .hub import download as hub_download
    from .paths import (
        checkpoint_root,
        describe_local,
        filesystem_has_room,
        human_bytes,
        resolve,
    )

    settings = _load(args)
    engine_id = canonical(args.engine) or args.engine
    if engine_id not in SPECS:
        print(f"error: unknown engine {args.engine!r}. Available: {', '.join(sorted(SPECS))}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    spec = _weight_spec(engine_id)
    if spec is None:
        # Not a success: the requested action did not happen. A script doing
        # `decis download --engine <typo> && serve` here would be acting on a false
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

    # Two destinations, and the loader reads back both:
    #
    #   * a model directory (`--dest`, else `DECIS_MODEL_DIR`) -> `<dir>/<engine id>/`,
    #     which is what a mounted volume serves and what an offline image bakes;
    #   * neither configured -> the Hub client's cache, which is where `serve` looks
    #     when no model directory is set. That is the default because it is the only
    #     destination the server finds again without being told where to look.
    base = Path(args.dest) if args.dest else settings.model_dir
    configured = _with(settings, model_dir=base) if base is not None else settings

    existing = resolve(spec, configured)
    if existing.is_local:
        print(f"{engine_id}: already present at {existing.path} ({describe_local(existing.path)})")
        # The adapter can be here while the base it adapts is not, so this is not a no-op
        # yet. The Hub is only probed if something actually has to be fetched.
        return _download_bases(spec, configured)

    selection = choose(settings)
    print(f"{engine_id}: source {selection.describe()}")
    if not client_installed(selection.name):
        print(
            f"error: fetching from {selection.name} needs the `{selection.module}` package, which is not "
            f"installed here.\n  uv sync --extra {SPECS[engine_id].extra or 'download'}",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR
    if spec.revision and not selection.pinned:
        print(
            f"warning: {engine_id} pins the revision {spec.revision[:12]}, which {selection.name} cannot "
            f"address; the mirror's current revision is fetched instead. DECIS_HUB=huggingface fails "
            f"loudly rather than substituting.",
            file=sys.stderr,
        )

    expected = spec.expected_bytes or 0

    if base is None:
        print(f"{engine_id}: downloading to the {selection.name} cache ({human_bytes(expected)}) ...")
        try:
            cached = hub_download(spec, selection=selection)
        except HubUnavailableError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        if checkpoint_root(cached, spec) is None:
            print(
                f"error: download finished but {cached} still has no usable checkpoint. "
                f"{_why_unusable(cached, spec.marker)}",
                file=sys.stderr,
            )
            return 1
        print(f"{engine_id}: ready in the {selection.name} cache at {cached}")
        return _download_bases(spec, configured, selection)

    # `snapshot_download(local_dir=...)` replicates the *repository's* own layout, so the
    # checkpoint keeps the subfolder it lives in upstream. Landing it under
    # `<dir>/<engine id>/` is what makes `paths.resolve` -- which looks under
    # `<DECIS_MODEL_DIR>/<engine id>/` -- find it again. Writing it directly into `<dir>/`
    # meant `decis download` failed on every destination it was ever given:
    # docs/design-review.md §2-D17.
    destination = base / spec.directory_name()
    if filesystem_has_room(destination, spec.expected_bytes) is False:
        print(
            f"warning: {destination} may not have room for {human_bytes(expected)}.",
            file=sys.stderr,
        )

    destination.mkdir(parents=True, exist_ok=True)
    print(f"{engine_id}: downloading to {destination} ({human_bytes(expected)}) ...")
    try:
        hub_download(spec, selection=selection, destination=destination)
    except HubUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    resolved = resolve(spec, configured)
    if not resolved.is_local:
        # Do not report success just because the HTTP calls succeeded: the only
        # thing that matters is whether the *loader* will find a checkpoint, and
        # that is exactly the question `resolve` answers.
        print(
            f"error: download finished but {destination} still has no usable checkpoint. "
            f"{_why_unusable(destination, spec.marker)}",
            file=sys.stderr,
        )
        return 1
    print(f"{engine_id}: ready at {resolved.path} ({describe_local(resolved.path)})")
    return _download_bases(spec, configured, selection)


def _report_hub(settings: Settings) -> None:
    """Which Hub this host would fetch from, and why.

    Probed rather than restated: "which Hub answers from here" is the fact that decides
    whether a `download` works, it is invisible from the configuration alone, and the
    fallback's cost (no commit pin) is exactly what a deployment needs to know *before* it
    relies on it. The probe is bounded by `hub.PROBE_TIMEOUT_S`; pinning `DECIS_HUB` skips
    it entirely.
    """
    from .hub import choose, client_installed

    selection = choose(settings)
    print(f"  weight hub       {settings.hub} -> {selection.name}: {selection.reason}")
    if not selection.pinned:
        print(f"  hub pin          {selection.name} has no commit revisions; DECIS_HUB=huggingface fails instead")
    if not client_installed(selection.name):
        print(f"  hub client       {selection.module} is NOT installed (uv sync --extra download)")


def _why_unusable(directory: Path, marker: str | None) -> str:
    """Why `checkpoint_root` rejected a directory, in the words of the actual reason.

    Two ways a downloaded directory is still unusable, and they need different actions: a
    missing marker means the fetch did not land a checkpoint at all, while a manifest whose
    shards are absent means it landed *part* of one. Telling a user with a half-copied
    8.65 GiB checkpoint to look for `config.json` -- which is sitting right there -- sends
    them to re-check the one thing that is fine (`docs/design-review.md` §2-D29).
    """
    from .paths import missing_shards

    missing = missing_shards(directory)
    if missing:
        shown = ", ".join(missing[:3])
        more = f" (and {len(missing) - 3} more)" if len(missing) > 3 else ""
        return (
            f"Its weight manifest names {len(missing)} file(s) that are not there: {shown}{more}. Re-run the download."
        )
    return f"Expected to find {marker} there."


def _download_bases(spec: WeightSpec, configured: Settings, selection: Selection | None = None) -> int:
    """Fetch the base models a checkpoint adapts.

    kev's adapter is useless on its own (see `paths.BaseModel`), so a `download` that
    fetched only the adapter would report success and leave `serve` unable to load.

    A base is a normal transformers model, but it is resolved exactly like a checkpoint --
    `paths.resolve`, then the same Hub choice -- because that is what the loader now does
    with it: `engines/kev.py` hands `transformers` a *directory*, so a base that lives in
    `DECIS_MODEL_DIR/<base name>/` is found offline, and one that does not is fetched from
    whichever Hub answers and lands in that Hub's cache. Writing it next to the checkpoint
    under `--dest` is what makes a mounted model directory self-contained: leaving it in the
    Hugging Face cache meant an air-gapped volume held the adapter and not its base.
    """
    from .hub import HubUnavailableError, choose
    from .hub import download as hub_download
    from .paths import checkpoint_root, filesystem_has_room, human_bytes, resolve

    bases = spec.base_specs()
    if not bases:
        return 0

    for base in bases:
        # Local first, and the Hub probe only when something has to be fetched: re-running
        # `download` on a complete directory must not need the network at all.
        if resolve(base, configured).is_local:
            print(f"{spec.engine_id}: base model {base.repo_id} is already present")
            continue
        if selection is None:
            selection = choose(configured)
        destination = configured.model_dir / base.directory_name() if configured.model_dir is not None else None
        size = f" ({human_bytes(base.expected_bytes)})" if base.expected_bytes else ""
        where = str(destination) if destination is not None else f"the {selection.name} cache"
        print(f"{spec.engine_id}: base model {base.repo_id}{size} -> {where} ...")
        if destination is not None:
            if filesystem_has_room(destination, base.expected_bytes) is False:
                print(f"warning: {destination} may not have room for {size.strip(' ()')}.", file=sys.stderr)
            destination.mkdir(parents=True, exist_ok=True)
        try:
            landed = hub_download(base, selection=selection, destination=destination)
        except HubUnavailableError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        # The same predicate the loader will apply, so "downloaded" and "loadable" cannot
        # drift apart (docs/design-review.md §2-D17).
        if checkpoint_root(landed, base) is None:
            print(
                f"error: the base model finished downloading but {landed} has no usable checkpoint. "
                f"{_why_unusable(landed, base.marker)}",
                file=sys.stderr,
            )
            return 1
    return 0


def _weight_spec(engine_id: str) -> WeightSpec | None:
    """An engine's declared weights, without importing its heavy dependencies.

    `decis.engines.laya` imports nothing but stdlib and Decis modules at module
    scope, which is what makes this possible (AGENTS.md §6).
    """
    from .engines.registry import create

    return create(engine_id).weights()


# --- helpers -----------------------------------------------------------------


def _load(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.env_file)
    overrides = _parse_model_paths(getattr(args, "model_path", None) or [])
    if overrides:
        settings = _with(settings, model_paths={**settings.model_paths, **overrides})
    # `--hub` is the flag for `DECIS_HUB`; argparse has already restricted it to
    # `config.HUBS`, so this cannot put an unusable value into `Settings`.
    hub = getattr(args, "hub", None)
    if hub:
        settings = _with(settings, hub=hub)
    return settings


def _parse_model_paths(entries: list[str]) -> dict[str, Path]:
    """`["kev-0.8b=/srv/kev"]` -> `{"kev-0.8b": Path("/srv/kev")}`.

    An unknown or misspelled engine id is a configuration error rather than a silently
    ignored flag: the reason the id moved out of the variable name and into the value
    is precisely so that it can be written exactly, dots included.
    """
    found: dict[str, Path] = {}
    for entry in entries:
        engine, separator, path = entry.partition("=")
        engine, path = engine.strip(), path.strip()
        if not separator or not engine or not path:
            raise ConfigError(f"--model-path takes ENGINE=PATH, got {entry!r}")
        resolved = canonical(engine)
        if resolved is None:
            raise ConfigError(
                f"--model-path names {engine!r}, which is not a registered engine. "
                f"Registered: {', '.join(sorted(SPECS))}."
            )
        found[resolved] = Path(path)
    return found


def _with(settings: Settings, **changes: object) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, **changes)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
