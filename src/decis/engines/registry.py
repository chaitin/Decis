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


SPECS: dict[str, EngineSpec] = {
    "mock": EngineSpec(
        id="mock",
        target="decis.engines.mock:MockEngine",
        extra="",  # no dependencies at all: usable in a bare install
        aliases=("decis-mock", "mock-engine"),
    ),
    # Real engines are registered in the same change that implements them
    # (AGENTS.md §5). Registering a target whose module does not exist yet would
    # advertise a model that cannot be served, and `decis models` would report a
    # missing dependency -- for our own missing code.
    #
    #   "laya-multilingual": EngineSpec(
    #       id="laya-multilingual",
    #       target="decis.engines.laya:LayaEngine",
    #       extra="laya",
    #       aliases=("decis-laya-multilingual", "laya"),
    #   ),
}


def canonical(name: str) -> str | None:
    """Resolve a client-supplied model name to a registered engine id.

    Accepts the engine id, any alias, and the versioned form that responses
    return (`decis/mock@0.1.0`), so that feeding a response's `model` field back
    in as a request works.
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
