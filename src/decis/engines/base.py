"""The engine interface.

An engine's only job is: given rendered questions, produce one probability
distribution per option. It returns plain floats and never touches the wire
format -- `answers.py` owns that. Adding an engine must not require changing
`render.py` or `answers.py` (AGENTS.md §5).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from ..domain import PreparedQuestion, ProbDist, estimate_tokens

Primitive = Literal["noul", "choice", "score"]

#: Model names clients may send, before alias resolution.
DEFAULT_ALIAS_STRIP = "decis/"


@dataclass(frozen=True)
class EngineInfo:
    """What an engine can do. Reported by `GET /v1/models` and used to reject
    requests the engine cannot answer *before* they reach it, so a capacity
    problem is a clear 422 rather than a quietly truncated answer."""

    id: str
    version: str
    primitives: frozenset[Primitive]
    max_options: int
    max_state_tokens: int
    max_question_tokens: int
    languages: str
    device: str
    dtype: str
    description: str
    release_date: str
    aliases: tuple[str, ...] = field(default=())

    @property
    def model_id(self) -> str:
        """The versioned id returned in a response's `model` field.

        Aliases move between releases; this does not, so a log line or an answer
        can always be traced back to what actually produced it.
        """
        return f"{DEFAULT_ALIAS_STRIP}{self.id}@{self.version}"

    def capacities(self) -> dict[str, object]:
        return {
            "primitives": sorted(self.primitives),
            "max_options": self.max_options,
            "max_state_tokens": self.max_state_tokens,
            "max_question_tokens": self.max_question_tokens,
            "languages": self.languages,
            "device": self.device,
            "dtype": self.dtype,
        }


@dataclass(frozen=True)
class WorkItem:
    """One question about one piece of content. The unit of scheduling.

    Each item carries its own `state_text`. Batching is never grouped by state:
    real traffic has a different state per request, so grouping by state would
    pin the batch size at 1 and batching would never happen at all
    (AGENTS.md §3-18).
    """

    request_id: str
    state_text: str
    question: PreparedQuestion


@dataclass(frozen=True)
class Prediction:
    """One distribution per work item, in the same order as the input."""

    probabilities: list[ProbDist]
    input_tokens: int = 0
    #: The engine's own confidence, when it has a calibrated one. Unlike the
    #: contract's `confidence`, this is not comparable across engines; it is
    #: surfaced under `decis` for callers who know what they are asking for.
    native_confidence: float | None = None


class DecisionEngine(ABC):
    """Base class for engines.

    Structurally a protocol -- duck-typed stand-ins work fine in tests -- but a
    base class so the optional parts have defaults.
    """

    def __init__(self) -> None:
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @abstractmethod
    def info(self) -> EngineInfo:
        """Static description. Must not load weights."""

    @abstractmethod
    def load(self) -> None:
        """Load weights. Idempotent. Called before the server accepts traffic."""

    @abstractmethod
    def predict(self, items: list[WorkItem]) -> Prediction:
        """Answer every item. Must return one distribution per item, in order."""

    def count_tokens(self, texts: list[str]) -> int:
        """Tokens the model will see. Override with the real tokenizer when there is one."""
        return sum(estimate_tokens(text) for text in texts)

    def close(self) -> None:
        """Release resources. The default is enough for engines with no handles.

        Deliberately not abstract: most engines hold nothing worth releasing, and
        forcing an empty override on each of them would be noise.
        """
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self.info().id!r}, loaded={self.loaded})"


# --- distribution helpers ----------------------------------------------------


def softmax(scores: list[float]) -> ProbDist:
    """Turn arbitrary logits into a probability distribution."""
    if not scores:
        raise ValueError("cannot build a distribution from zero options")
    largest = max(scores)
    exponentiated = [math.exp(score - largest) for score in scores]
    total = sum(exponentiated)
    if not math.isfinite(total) or total <= 0:
        return uniform(len(scores))
    return [value / total for value in exponentiated]


def normalize(scores: list[float]) -> ProbDist:
    """Turn non-negative scores into a distribution, falling back to uniform."""
    if not scores:
        raise ValueError("cannot build a distribution from zero options")
    total = sum(max(0.0, score) for score in scores)
    if not math.isfinite(total) or total <= 0:
        return uniform(len(scores))
    return [max(0.0, score) / total for score in scores]


def uniform(count: int) -> ProbDist:
    return [1.0 / count] * count


def validate_distribution(probabilities: ProbDist, options: int, *, context: str) -> ProbDist:
    """Reject anything an engine should never have produced.

    A wrong-length or non-finite distribution is an engine bug, not a client
    error, so it must surface as a loud failure rather than a strange answer.
    """
    if len(probabilities) != options:
        raise ValueError(f"{context}: got {len(probabilities)} probabilities for {options} options")
    for value in probabilities:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{context}: probability {value!r} is not a finite number")
    return [float(value) for value in probabilities]
