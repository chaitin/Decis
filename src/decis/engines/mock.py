"""A deterministic engine with no weights.

Its job is to make the API testable and demonstrable without downloading 1.6 GB
of anything: CI asserts the wire contract against it, and `decis serve` works on
a laptop in a second.

It is **not** a model. It scores each option by hashing the request -- plus a
small bonus for options that share words with the content -- so that answers are
stable across runs and roughly plausible in a demo. Do not read anything into
them.
"""

from __future__ import annotations

import hashlib

from .. import __version__
from ..domain import ProbDist
from .base import DecisionEngine, EngineInfo, Prediction, WorkItem, softmax

#: Higher means more confident. Chosen so the mock produces decisive-but-not-
#: degenerate distributions, which is what makes contract tests meaningful.
SCORE_SCALE = 5.0
OVERLAP_WEIGHT = 0.35


class MockEngine(DecisionEngine):
    """Deterministic, dependency-free, weight-free."""

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="mock",
            version=__version__,
            primitives=frozenset({"noul", "choice", "score"}),
            max_options=255,
            max_sequence_tokens=32_000,
            max_question_tokens=32_000,
            languages="any",
            device="cpu",
            dtype="none",
            description=(
                "Deterministic mock engine. No weights and no model: answers are "
                "derived from a hash of the request. For contract tests and demos."
            ),
            release_date="2026-09-22",
            aliases=("decis-mock", "mock-engine"),
        )

    def load(self) -> None:
        self._loaded = True

    def predict(self, items: list[WorkItem]) -> Prediction:
        if not self.loaded:
            raise RuntimeError("engine used before load(); the server must load engines at startup")

        distributions: list[ProbDist] = []
        for item in items:
            distributions.append(self._score(item))

        texts = [item.state_text for item in items]
        texts.extend(item.question.text() for item in items)
        return Prediction(probabilities=distributions, input_tokens=self.count_tokens(texts))

    def _score(self, item: WorkItem) -> ProbDist:
        state_words = _words(item.state_text)
        context = f"{item.state_text}\x00{item.question.text()}"
        scores = []
        for option in item.question.options:
            base = _hash_unit(f"{context}\x00{option.name}")
            overlap = _overlap(state_words, option.description)
            scores.append(base + OVERLAP_WEIGHT * overlap)
        return softmax([score * SCORE_SCALE for score in scores])


def _hash_unit(text: str) -> float:
    """A stable number in [0, 1) derived from the text."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _words(text: str) -> set[str]:
    return {word for word in "".join(char.lower() if char.isalnum() else " " for char in text).split() if len(word) > 2}


def _overlap(state_words: set[str], description: str) -> float:
    option_words = _words(description)
    if not option_words:
        return 0.0
    return len(state_words & option_words) / len(option_words)
