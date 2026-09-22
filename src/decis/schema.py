"""The wire format, as Pydantic models.

This file is the only place the HTTP shape is defined (AGENTS.md §2). It mirrors
`docs/contract/typesafe-openapi-0.2.0.json` field for field, including which
fields may be null -- the request types are not uniform about that:

===================  ==========================================
`state`              string | object | array        (no null)
`instructions`       string | object | array | null
choice criterion     string | object | array | null
score level          string | object | array        (no null)
`noul.criteria`      object | null
===================  ==========================================

`tests/test_contract_openapi.py` asserts this file still agrees with that
snapshot, so a contract change cannot pass unnoticed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .domain import MeasuredTokens, PreparedRequest
from .errors import InvalidRequestError

# --- request -----------------------------------------------------------------

JsonObject = dict[str, Any]
JsonArray = list[Any]

#: Arbitrary JSON a caller may attach to an instruction or criterion.
Instruction = str | JsonObject | JsonArray | None
#: A choice criterion may additionally be null ("decide by name alone").
ChoiceCriterion = str | JsonObject | JsonArray | None
#: A score level may not be null -- it is the rubric text for that level.
ScoreLevel = str | JsonObject | JsonArray
#: `state` has no null variant in the OpenAPI schema.
State = str | JsonObject | JsonArray


class NoulCriteria(BaseModel):
    """What counts as yes and what counts as no. Both halves are optional."""

    true: ChoiceCriterion = None
    false: ChoiceCriterion = None


class NoulQuestion(BaseModel):
    type: Literal["noul"]
    instructions: Instruction = None
    criteria: NoulCriteria | None = None


class ChoiceQuestion(BaseModel):
    type: Literal["choice"]
    instructions: Instruction = None
    #: Required, and deliberately without `min_length`: the official OpenAPI does
    #: not declare one, so `{}` is a valid request that no engine can answer.
    #: That mismatch is reported as a 422 by the capacity check, not by Pydantic.
    criteria: dict[str, ChoiceCriterion]


class ScoreQuestion(BaseModel):
    type: Literal["score"]
    instructions: Instruction = None
    criteria: Annotated[list[ScoreLevel], Field(min_length=1)]


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class SystemOneRequest(BaseModel):
    """`POST /v1/systemone` request body."""

    state: State
    model: str
    questions: Annotated[dict[str, Question], Field(min_length=1)]


# --- response ----------------------------------------------------------------


class Usage(BaseModel):
    """Both fields are required integers. They are a billing convention, not a
    generation length: these models do not generate text (see docs/design.md §4.3)."""

    input_tokens: int
    output_tokens: int


class NoulAnswer(BaseModel):
    """A scalar. No `confidence`, no `probabilities` -- matching the wire contract."""

    type: Literal["noul"]
    noul: float


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: str
    confidence: float
    probabilities: dict[str, float]


class ScoreAnswer(BaseModel):
    type: Literal["score"]
    score: float
    confidence: float
    #: Keys are the strings "0", "1", ... -- see AGENTS.md §3-1.
    legend: dict[str, ScoreLevel]
    probabilities: dict[str, float]


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class DecisExtensions(BaseModel):
    """Everything Decis adds beyond the contract.

    Kept in one namespace so that `extra="ignore"` on the official SDK's models
    swallows all of it, and so that nothing here can ever be mistaken for a
    contract field.
    """

    engine: str
    engine_version: str
    device: str
    dtype: str
    latency_ms: float
    batch_size: int
    #: Set when the caller named a model this server does not have and we
    #: substituted the one it runs -- typically `jev-latest`. Never silent.
    requested_model: str | None = None
    #: The engine's own confidence, one entry per question id, when the engine has a
    #: calibrated one. The contract-level `confidence` is Decis's own formula and is
    #: comparable across engines; this preserves the engine's, which is not
    #: (see docs/api-compatibility.md §7). A map rather than a scalar because one
    #: request can mix question types, and averaging them would be meaningless.
    native_confidence: dict[str, float] | None = None


class SystemOneResponse(BaseModel):
    model: str
    answers: dict[str, Answer]
    usage: Usage
    decis: DecisExtensions | None = None


class ModelMetadata(BaseModel):
    name: str
    description: str
    release_date: str
    #: Decis-specific capability reporting. Allowed because the official SDK's
    #: response models ignore unknown fields.
    decis: dict[str, Any] | None = None


class ModelMetadataList(BaseModel):
    models: list[ModelMetadata]


class ErrorDetail(BaseModel):
    """The body shape for every non-validation error (auth, overload, faults)."""

    error_type: str
    message: str


class ErrorResponse(BaseModel):
    """Documented for our own OpenAPI output. Not part of the jev contract."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"detail": {"error_type": "authentication_error", "message": "..."}}}
    )

    detail: ErrorDetail


# --- capacity ------------------------------------------------------------------


class EngineCapacity(Protocol):
    """The subset of `EngineInfo` this module needs, so that `schema` does not
    have to import the engine layer."""

    primitives: frozenset[str]
    max_options: int
    max_sequence_tokens: int
    max_question_tokens: int


def validate_capacity(
    prepared: PreparedRequest,
    capacity: EngineCapacity,
    measure: Callable[[PreparedRequest], MeasuredTokens],
) -> MeasuredTokens:
    """Reject requests the target engine cannot honour.

    Checked *before* the engine runs, so an over-long input is a clear 422 naming
    the field rather than a truncated sequence producing a confidently wrong
    answer.

    `measure` comes from the engine and reports what it will *actually* consume,
    not a character estimate. That distinction is load-bearing: an engine's
    question budget also pays for per-option decoration (option names, mask
    positions, separators) which never appears in the rendered question text, so
    a character count under-reports and lets through a request the engine would
    then silently truncate. The engine measures; this function decides what to do
    about it (`AGENTS.md §2`, `§5-4`).

    The two limits are separate because real engines have separate budgets: Laya
    spends a long-context encoder on the state but only a small fixed allowance on
    the options (`head_max_len`). The official docs express this as "state + the
    longest question <= 32k"; two named limits say the same thing more precisely.

    Returns the measurement, so the caller can log it without measuring twice.
    """
    measured = measure(prepared)
    if measured.sequence_tokens > capacity.max_sequence_tokens:
        raise InvalidRequestError(
            f"`state` plus the longest question is about {measured.sequence_tokens} tokens (state "
            f"alone: {measured.state_tokens}), over this model's limit of "
            f"{capacity.max_sequence_tokens}. Shorten the content, or ask fewer or shorter "
            f"questions about it.",
            loc=["body", "state"],
            type="too_long",
        )

    for question in prepared.questions:
        if question.type not in capacity.primitives:
            raise InvalidRequestError(
                f"Question {question.qid!r} asks for a {question.type!r} answer, which this model "
                f"does not support. It supports: {', '.join(sorted(capacity.primitives))}.",
                loc=["body", "questions", question.qid, "type"],
            )

        if question.type == "choice" and not question.options:
            # The official OpenAPI does not put minProperties on choice criteria,
            # so `{}` is a well-formed request that no engine can answer. Reject it
            # here, where we can say something useful.
            raise InvalidRequestError(
                f"Question {question.qid!r} is a choice with no criteria, so there is nothing to "
                "choose between. Add at least one entry to `criteria`.",
                loc=["body", "questions", question.qid, "criteria"],
                type="too_short",
            )

        if len(question.options) > capacity.max_options:
            raise InvalidRequestError(
                f"Question {question.qid!r} has {len(question.options)} options; this model supports "
                f"at most {capacity.max_options}.",
                loc=["body", "questions", question.qid, "criteria"],
                type="too_long",
            )

        head_tokens = measured.head_tokens.get(question.qid, 0)
        if head_tokens > capacity.max_question_tokens:
            raise InvalidRequestError(
                f"Question {question.qid!r} is about {head_tokens} tokens, over this model's "
                f"limit of {capacity.max_question_tokens} per question. Shorten the instructions "
                "or the criteria descriptions.",
                loc=["body", "questions", question.qid],
                type="too_long",
            )

    return measured
