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


def prepare_question(qid: str, question: Question) -> PreparedQuestion:
    """Turn one wire question into rendered options."""
    instructions = render_value(question.instructions)

    if isinstance(question, NoulQuestion):
        yes = render_value(question.criteria.true) if question.criteria else ""
        no = render_value(question.criteria.false) if question.criteria else ""
        return PreparedQuestion(
            qid=qid,
            type="noul",
            instructions=instructions,
            options=(Option("false", no), Option("true", yes)),
        )

    if isinstance(question, ChoiceQuestion):
        options = tuple(Option(name, render_value(description)) for name, description in question.criteria.items())
        return PreparedQuestion(qid=qid, type="choice", instructions=instructions, options=options)

    if isinstance(question, ScoreQuestion):
        options = tuple(
            Option(str(level), render_value(description)) for level, description in enumerate(question.criteria)
        )
        return PreparedQuestion(qid=qid, type="score", instructions=instructions, options=options)

    raise AssertionError(f"unhandled question type: {type(question).__name__}")


def prepare_request(request: SystemOneRequest) -> PreparedRequest:
    return PreparedRequest(
        model=request.model,
        state_text=render_state(request.state),
        questions=tuple(prepare_question(qid, question) for qid, question in request.questions.items()),
    )


__all__ = ["prepare_question", "prepare_request", "render_state", "render_value"]
