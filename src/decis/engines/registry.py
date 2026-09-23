"""Engine id -> implementation, resolved lazily.

Engine classes are referenced by the *string* path of their module and class, so
importing the registry never imports torch. An image that installs one engine's
extra must still be able to serve `/healthz`, `/readyz` and `GET /v1/models`
without the other engine's dependencies present (AGENTS.md §6).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import cache

from ..errors import InvalidRequestError
from .base import DecisionEngine, EngineInfo


@dataclass(frozen=True)
class EngineSpec:
    id: str
    target: str  # "module:ClassName" -- never imported at module import time
    extra: str  # the pyproject extra that provides its dependencies
    aliases: tuple[str, ...] = ()


#: Model names that mean "whatever default this server runs", rather than naming a
#: specific model. `jev-latest` is the official SDK's default, so without this a
#: caller who changes only `TYPESAFE_BASE_URL` gets a 422 -- and the whole point of
#: Decis is that changing one environment variable is enough. Decis reports the
#: substitution in the response's `decis.requested_model`, so nothing is hidden.
FOREIGN_DEFAULT_MODELS = frozenset(
    {
        "jev-latest",
        "jev",
        "jev-latest-8b",
        "systemone-latest",
        "system-one",
        "system-one-latest",
        "default",
    }
)


def is_foreign_default(name: str) -> bool:
    candidate = name.strip().lower()
    if "@" in candidate:
        candidate = candidate.split("@", 1)[0]
    return candidate in FOREIGN_DEFAULT_MODELS


#: `(engine_id, device) -> dtype`. A dtype default cannot be global, because the same
#: weights behave completely differently per engine and device: kev-0.8b on CPU in bf16
#: measured **~83x slower** than fp32 (137 s vs 1.66 s per request) because Qwen3.5's
#: Gated DeltaNet falls back to a reference implementation that is pathological in bf16
#: on CPU (docs/feasibility.md §4). Engines absent from this table decide for themselves
#: -- Laya's own `Agent` forces fp32 on cpu/mps, and duplicating that here would give the
#: same fact two homes.
DTYPE_DEFAULTS: dict[tuple[str, str], str] = {
    ("kev-0.8b", "cpu"): "fp32",
    ("kev-0.8b", "cuda"): "bf16",
    ("kev-0.8b", "mps"): "fp32",
}

#: Combinations that work but are known to be bad, with the reason. A user who forces
#: one gets a loud warning at load time instead of a service that is silently 83x slow.
DEGRADED: dict[tuple[str, str, str], str] = {
    ("kev-0.8b", "cpu", "bf16"): "kev bf16 on CPU is ~83x slower than fp32 (measured); use fp32",
}


def default_dtype(engine_id: str, device: str) -> str:
    """The dtype to load `engine_id` with on `device`, absent an explicit override."""
    return DTYPE_DEFAULTS.get((engine_id, device), "fp16" if device == "cuda" else "fp32")


def degraded_reason(engine_id: str, device: str, dtype: str) -> str | None:
    """Why this combination is a bad idea, or None. Purely declarative, so the engine
    does not have to know which of its knobs are known-bad."""
    return DEGRADED.get((engine_id, device, dtype))


SPECS: dict[str, EngineSpec] = {
    # One entry per Laya checkpoint, because they are separate sets of weights with
    # separate capacities and must be describable independently by `GET /v1/models`.
    # `laya` is the English root checkpoint; the other two are subfolders of the same
    # Hub repository (see `engines/laya.py`).
    "laya-multilingual": EngineSpec(
        id="laya-multilingual",
        target="decis.engines.laya:LayaMultilingualEngine",
        extra="laya",
        aliases=("decis-laya-multilingual", "laya-multi"),
    ),
    "laya": EngineSpec(
        id="laya",
        target="decis.engines.laya:LayaEngine",
        extra="laya",
        aliases=("decis-laya", "laya-english"),
    ),
    # kev is an adapter over a published base model: `decis download` fetches both, and
    # the base is declared on the WeightSpec so it cannot go missing (paths.BaseModel).
    "kev-0.8b": EngineSpec(
        id="kev-0.8b",
        target="decis.engines.kev:KevEngine",
        extra="kev",
        aliases=("kev", "decis-kev", "kev-latest"),
    ),
    "laya-typed-decisions": EngineSpec(
        id="laya-typed-decisions",
        target="decis.engines.laya:LayaTypedDecisionsEngine",
        extra="laya",
        aliases=("decis-laya-typed-decisions",),
    ),
}


def canonical(name: str) -> str | None:
    """Resolve a client-supplied model name to a registered engine id.

    Accepts the engine id, any alias, and the versioned form that responses
    return (`decis/laya-multilingual@0.3.5`), so that feeding a response's
    `model` field back in as a request works.
    """
    candidate = name.strip()
    if not candidate:
        return None
    if "@" in candidate:
        candidate = candidate.split("@", 1)[0]
    if candidate.startswith("decis/"):
        candidate = candidate[len("decis/") :]
    if candidate in SPECS:
        return candidate
    for spec in SPECS.values():
        if candidate == spec.id or candidate in spec.aliases:
            return spec.id
    return None


def known_names() -> list[str]:
    """Everything a client may pass as `model`, for error messages and `/v1/models`."""
    names: list[str] = []
    for spec in SPECS.values():
        names.append(spec.id)
        names.extend(spec.aliases)
    return sorted(names)


def resolve_or_raise(name: str) -> str:
    resolved = canonical(name)
    if resolved is None:
        raise InvalidRequestError(
            f"Unknown model {name!r}. Available: {', '.join(sorted(SPECS))}.",
            loc=["body", "model"],
        )
    return resolved


def unknown_model_error(name: str) -> InvalidRequestError:
    return InvalidRequestError(
        f"Unknown model {name!r}. Available on this server: {', '.join(known_names())}.",
        loc=["body", "model"],
    )


@cache
def load_class(engine_id: str) -> type[DecisionEngine]:
    """Import and cache the engine class. Importing is the expensive part."""
    spec = SPECS[engine_id]
    module_name, _, class_name = spec.target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise InvalidRequestError(
            f"Engine {engine_id!r} is registered but its dependencies are not installed "
            f"({exc}). Install them with: pip install 'decis[{spec.extra}]'",
            loc=["body", "model"],
            type="engine_unavailable",
        ) from exc
    return getattr(module, class_name)  # type: ignore[no-any-return]


def create(engine_id: str) -> DecisionEngine:
    if engine_id not in SPECS:
        raise InvalidRequestError(
            f"Unknown engine {engine_id!r}. Available: {', '.join(sorted(SPECS))}.",
            loc=["body", "model"],
        )
    return load_class(engine_id)()


def describe(engine_id: str) -> EngineInfo:
    """Engine metadata without loading weights."""
    return create(engine_id).info()


@dataclass(frozen=True)
class EngineStatus:
    """Whether an engine can answer a request *right now*, and if not, what to do.

    This exists because "the class imports" is not the same claim as "this server
    can run it". Registration is lazy by design (AGENTS.md §6), so every engine
    class imports cleanly even in an image that installed none of its dependencies.
    Reporting that as "ready" would be a false claim in a command operators script
    against, so the distinction is made explicit and derived from two facts the
    engine already declares: the modules it needs, and where its weights are.
    """

    engine_id: str
    usable: bool
    #: One short phrase naming the state, for a column in `decis models`.
    summary: str
    #: The exact command or path that changes the answer, when there is one.
    remedy: str = ""


def status(engine_id: str) -> EngineStatus:
    """Classify one engine. Never downloads and never loads weights."""
    from ..config import load_settings
    from ..paths import missing_requirements, resolve

    try:
        engine = create(engine_id)
        spec = engine.weights()
    except InvalidRequestError as exc:
        # Collapsed to one line: `EngineStatus.remedy` goes in a table, and these
        # messages are written to be complete, not short.
        return EngineStatus(engine_id, False, "unavailable", str(exc).splitlines()[0])

    if spec is None:
        return EngineStatus(engine_id, True, "ready")

    absent = missing_requirements(spec)
    if absent:
        extra = SPECS[engine_id].extra or "all"
        return EngineStatus(
            engine_id,
            False,
            "deps missing",
            f"uv sync --extra {extra}   (no {', '.join(absent)} module)",
        )

    source = resolve(spec, load_settings())
    if source.is_local:
        return EngineStatus(engine_id, True, "ready", f"weights at {source.path}")
    # Whether a *download* is possible is a property of the checkpoint's declaration,
    # not of where the resolver happened to look.
    if spec.is_downloadable():
        return EngineStatus(engine_id, False, "needs weights", f"decis download --engine {engine_id}")
    return EngineStatus(engine_id, False, "no weights", "the path is not a valid checkpoint")
