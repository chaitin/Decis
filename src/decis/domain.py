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
    """A cheap character-based estimate, for engines with no tokenizer.

    Only ever feeds a capacity check that produces a clear 422, never a silent
    truncation -- provided the caller overrides the measurement. See
    `MeasuredTokens`.
    """
    return max(0, len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class MeasuredTokens:
    """How many tokens an engine will *actually* consume, per budget.

    Exists because a character estimate was measurably wrong in the direction that
    hurts: an engine's head budget also pays for per-option decorations (option
    names, `[MASK]` positions, separators) that do not appear in the rendered
    question text, so `len(text) // 4` under-counts. A request could then pass
    validation and still be **silently truncated by the engine**, which is exactly
    the degraded answer `AGENTS.md §5-4` forbids.

    So the engine measures (it is the only layer that knows its own sequence
    layout) and `schema.validate_capacity` applies the policy (it is the only
    layer that owns the error shape).
    """

    #: Raw length of the rendered state, for reporting.
    state_tokens: int
    #: Per question: what that question's own head costs (instructions + options).
    head_tokens: dict[str, int]
    #: The longest single sequence any one question needs -- state plus that
    #: question's head. This, not `state_tokens`, is the real context constraint:
    #: the state and the head share one sequence, so checking them independently
    #: lets a request satisfy both budgets and still be truncated.
    sequence_tokens: int

    def describe(self) -> str:
        heads = ", ".join(f"{qid}={n}" for qid, n in self.head_tokens.items())
        return f"sequence={self.sequence_tokens} state={self.state_tokens} heads: {heads}"


def as_text(value: Any) -> str:
    """Flatten a rendered value to a single line, for log lines and titles."""
    return " ".join(str(value).split())


__all__ = [
    "CHARS_PER_TOKEN",
    "MeasuredTokens",
    "Option",
    "PreparedQuestion",
    "PreparedRequest",
    "ProbDist",
    "QuestionType",
    "as_text",
    "estimate_tokens",
]
