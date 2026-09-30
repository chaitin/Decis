"""Wire JSON -> the text a model sees.

The one place that decides how a `state`, an `instructions` blob or a criterion
becomes a string (AGENTS.md §2). Engines must not build prompts of their own --
they receive already-rendered pieces and arrange them into whatever sequence
their architecture needs.

Rendering is deterministic: dict keys are sorted, so the same request always
produces byte-identical text. That matters because the official SDK retries
POSTs, and because benchmark runs compare input hashes.
"""

from __future__ import annotations

import json
from typing import Any

from .domain import Option, PreparedQuestion, PreparedRequest
from .schema import (
    ChoiceQuestion,
    NoulQuestion,
    Question,
    ScoreQuestion,
    SystemOneRequest,
)


def render_value(value: Any) -> str:
    """Render any JSON value as readable plain text.

    Callers may attach arbitrary JSON to `instructions` and to criteria -- the
    documented examples include `{"task": ...}`, lists and nested objects. Nothing
    here is a reserved word: the structure is the caller's, we only make it
    readable.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, dict):
        lines = []
        for key in sorted(value):
            rendered = render_value(value[key])
            if rendered:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            rendered = render_value(item)
            if rendered:
                lines.append(f"- {rendered}")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_state(state: Any) -> str:
    return render_value(state)


def noul_options(false_description: str = "", true_description: str = "") -> tuple[Option, ...]:
    """The two options a `noul` always has, in the order the contract fixes.

    A helper rather than two literals at each call site because it is the *type*
    that determines these names, not the question: anything needing to build a
    `noul` -- including a warmup pass that just needs a valid question -- goes
    through here, so `render.py` stays the only place the wire keys `"false"` and
    `"true"` are written (`AGENTS.md §2`).
    """
    return (Option("false", false_description), Option("true", true_description))


def raw_question(question: Question) -> dict[str, Any]:
    """The caller's own values for one question, unrendered.

    Shaped like the object upstream's HTTP layer would build (a `model_dump` with
    `None` fields dropped), because that is what an engine whose prompt is defined in
    terms of the caller's JSON has to be handed. Only the *values* travel: `type` is
    the discriminator an engine already knows, and no engine may see a `qid`
    (`AGENTS.md §3-6`). Kept here rather than in the engine so that "which fields a
    question has" stays a fact about the wire format, which this module owns.

    A choice criterion's explicit `null` is preserved inside `criteria` on purpose: it
    means "decide by name alone", and dropping it would change how many options the
    question has.
    """
    raw: dict[str, Any] = {"type": question.type}
    if question.instructions is not None:
        raw["instructions"] = question.instructions
    if isinstance(question, NoulQuestion):
        # `NoulCriteria` is a model, so its unset halves have to be dropped to match what
        # upstream's HTTP layer would have received.
        if question.criteria is not None:
            raw["criteria"] = question.criteria.model_dump(exclude_none=True)
    else:
        # A choice's criteria are a plain `dict` and a score's are a plain `list`, so the
        # caller's values are already here verbatim -- `None` included, because a choice
        # criterion of explicit `null` means "decide by name alone" and dropping it would
        # change how many options the question has.
        raw["criteria"] = question.criteria
    return raw


def prepare_question(qid: str, question: Question) -> PreparedQuestion:
    """Turn one wire question into rendered options."""
    instructions = render_value(question.instructions)
    raw = raw_question(question)

    if isinstance(question, NoulQuestion):
        yes = render_value(question.criteria.true) if question.criteria else ""
        no = render_value(question.criteria.false) if question.criteria else ""
        return PreparedQuestion(
            qid=qid,
            type="noul",
            instructions=instructions,
            options=noul_options(no, yes),
            raw=raw,
        )

    if isinstance(question, ChoiceQuestion):
        options = tuple(Option(name, render_value(description)) for name, description in question.criteria.items())
        return PreparedQuestion(qid=qid, type="choice", instructions=instructions, options=options, raw=raw)

    if isinstance(question, ScoreQuestion):
        options = tuple(
            Option(str(level), render_value(description)) for level, description in enumerate(question.criteria)
        )
        return PreparedQuestion(qid=qid, type="score", instructions=instructions, options=options, raw=raw)

    raise AssertionError(f"unhandled question type: {type(question).__name__}")


def prepare_request(request: SystemOneRequest) -> PreparedRequest:
    return PreparedRequest(
        model=request.model,
        state_text=render_state(request.state),
        questions=tuple(prepare_question(qid, question) for qid, question in request.questions.items()),
        raw_state=request.state,
    )


__all__ = ["noul_options", "prepare_question", "prepare_request", "raw_question", "render_state", "render_value"]
