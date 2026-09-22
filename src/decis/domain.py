"""Shared vocabulary: the types every layer passes around.

This module sits at the bottom of the package on purpose. `PreparedQuestion` and
`ProbDist` are used by the normalisation layer *and* by engines, so putting them
in `schema.py` or `render.py` would force an upward import and a cycle. Nothing
here imports anything else in the package.

`PreparedQuestion` is deliberately *not* a wire type and *not* a token sequence:
it is the neutral middle that both sides agree on. That is what lets an engine
know nothing about JSON and `answers.py` know nothing about models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

#: A probability per option, in the order the options were rendered.
ProbDist: TypeAlias = list[float]

#: Rough characters per token, for engines that cannot count tokens themselves.
CHARS_PER_TOKEN = 4

QuestionType: TypeAlias = Literal["noul", "choice", "score"]


@dataclass(frozen=True)
class Option:
    """One thing the model can choose, and the text describing it.

    `name` is the wire key -- it appears in `probabilities`, so it is never shown
    to the model. Only `description` is.
    """

    name: str
    description: str


@dataclass(frozen=True)
class PreparedQuestion:
    """A question, rendered and ready to be scored.

    The `name` of each option is fixed by the contract: `noul` uses "false"/"true",
    `choice` uses the caller's criterion names, `score` uses "0"/"1"/...
    """

    qid: str
    type: QuestionType
    instructions: str
    options: tuple[Option, ...]

    def text(self) -> str:
        """Everything the model reads for this question, options included.

        Used for token accounting and by the mock engine. One definition, so a
        capacity check and a token count cannot drift apart.
        """
        parts = [self.instructions] if self.instructions else []
        parts.extend(option.description for option in self.options if option.description)
        return "\n".join(parts)


@dataclass(frozen=True)
class PreparedRequest:
    """A whole request, normalised. The last point at which `state` is a string."""

    model: str
    state_text: str
    questions: tuple[PreparedQuestion, ...]


def estimate_tokens(text: str) -> int:
    """A cheap character-based estimate for engines with no tokenizer.

    Deliberately conservative: it only ever feeds a capacity check that produces a
    clear 422, never a silent truncation.
    """
    return max(0, len(text) // CHARS_PER_TOKEN)


def as_text(value: Any) -> str:
    """Flatten a rendered value to a single line, for log lines and titles."""
    return " ".join(str(value).split())


__all__ = [
    "CHARS_PER_TOKEN",
    "Option",
    "PreparedQuestion",
    "PreparedRequest",
    "ProbDist",
    "QuestionType",
    "as_text",
    "estimate_tokens",
]
