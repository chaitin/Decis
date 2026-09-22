"""Probability distribution -> wire answer, and the one definition of `confidence`.

Everything that decides what a client sees about a decision lives here, so that
two engines cannot disagree about what `confidence` or a wire key means
(AGENTS.md §2). Engines return plain `list[float]` and know nothing about the
wire format.

Two consistency rules are enforced by construction rather than by convention:

* `choice == argmax(probabilities)` is computed from the **published, rounded**
  values. Computing it from the unrounded ones can disagree with what the client
  sees when two options round to the same number.
* `score == sum(k * probabilities[k])` likewise. Clients check this; see
  docs/api-compatibility.md §8 L2.
"""

from __future__ import annotations

import math

from .domain import PreparedQuestion, ProbDist, estimate_tokens
from .errors import InvalidRequestError
from .schema import ChoiceAnswer, NoulAnswer, ScoreAnswer

#: Enough precision that the values still sum to ~1, few enough that the payload
#: stays readable. The contract only promises "approximately 1".
PROBABILITY_DECIMALS = 4


def question_keys(question: PreparedQuestion) -> list[str]:
    """The wire keys for a question, in order.

    `noul` -> ``["false", "true"]``; `choice` -> the caller's criterion names;
    `score` -> ``["0", "1", ...]``. This is the single definition of that rule.
    """
    return [option.name for option in question.options]


def round_probs(probabilities: ProbDist) -> list[float]:
    return [round(float(p), PROBABILITY_DECIMALS) for p in probabilities]


def choice_confidence(probabilities: ProbDist) -> float:
    """Normalised peak: how far the winner is above the uniform baseline.

    0 when all options are equally likely, 1 when one option is certain. Defined
    for a single option (always 1.0).
    """
    k = len(probabilities)
    if k <= 1:
        return 1.0
    peak = max(probabilities)
    return _clamp((peak - 1.0 / k) / (1.0 - 1.0 / k))


def score_confidence(probabilities: ProbDist) -> float:
    """1 minus the normalised entropy of the distribution.

    0 when every level is equally likely, 1 when one level is certain. Entropy
    rather than distance-from-the-mode, because that keeps the same meaning as
    `choice_confidence` -- 0 means "no opinion", 1 means "certain" -- and because
    the ordering information a score carries is already in `score` itself.
    """
    levels = len(probabilities)
    if levels <= 1:
        return 1.0
    entropy = -sum(p * math.log(p) for p in probabilities if p > 0)
    return _clamp(1.0 - entropy / math.log(levels))


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), PROBABILITY_DECIMALS)


def build_answer(question: PreparedQuestion, probabilities: ProbDist) -> NoulAnswer | ChoiceAnswer | ScoreAnswer:
    """Convert one distribution into the answer the contract specifies for it."""
    keys = question_keys(question)
    if not keys:
        # No options means nothing to decide. A choice question reaches this state
        # through schema-legal but empty `criteria`; `schema.validate_capacity`
        # rejects it earlier with the same wording.
        raise InvalidRequestError(
            f"Question {question.qid!r} has nothing to choose between. Give it at least one "
            "option, or use a different question type.",
            loc=["body", "questions", question.qid, "criteria"],
        )
    if len(probabilities) != len(keys):
        raise InvalidRequestError(
            f"Engine returned {len(probabilities)} probabilities for question "
            f"{question.qid!r}, which has {len(keys)} options.",
            loc=["body", "questions", question.qid],
        )

    published = round_probs(probabilities)

    if question.type == "noul":
        # The contract makes noul a scalar with no confidence field. The second
        # option is "true"; see render.prepare_question.
        return NoulAnswer(type="noul", noul=published[1])

    if question.type == "choice":
        winner = _argmax(published)
        return ChoiceAnswer(
            type="choice",
            choice=keys[winner],
            confidence=choice_confidence(published),
            probabilities=dict(zip(keys, published, strict=True)),
        )

    if question.type == "score":
        # Expected level, computed from the published values so that a client
        # recomputing it from `probabilities` gets the same number.
        expected = round(sum(level * p for level, p in enumerate(published)), PROBABILITY_DECIMALS)
        return ScoreAnswer(
            type="score",
            score=expected,
            confidence=score_confidence(published),
            legend={str(level): option.description for level, option in enumerate(question.options)},
            probabilities={str(level): p for level, p in enumerate(published)},
        )

    raise AssertionError(f"unhandled question type: {question.type!r}")


def _argmax(values: list[float]) -> int:
    """Index of the largest value; ties go to the earliest option."""
    best = 0
    for index in range(1, len(values)):
        if values[index] > values[best]:
            best = index
    return best


def estimate_output_tokens(answers: dict[str, object]) -> int:
    """The billing convention for `usage.output_tokens`.

    These models generate no text, so "output tokens" has no natural meaning. We
    report the token count of the serialised answers, which is what a caller is
    charged for, and we say so in the docs rather than implying it measures
    generation length (docs/api-compatibility.md §4.3).
    """
    import json

    return estimate_tokens(json.dumps(answers, ensure_ascii=False, separators=(",", ":"), default=str))
