"""L0: our models must still accept and produce what the official OpenAPI says.

The snapshot in `docs/contract/` is the authoritative document. These tests do not
compare prose -- they read the JSON Schema in it and check our behaviour against
it, so that a contract change cannot land without this file failing.

See docs/api-compatibility.md §1 for the evidence levels, and
docs/design-review.md §4-M1 for why a document-level check is not sufficient on
its own: it can only prove we agree with the snapshot, never that the live server
agrees with its own OpenAPI. L5 covers that.
"""

from __future__ import annotations

import pytest

from decis.schema import (
    ChoiceQuestion,
    ModelMetadata,
    ModelMetadataList,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    SystemOneRequest,
    SystemOneResponse,
    Usage,
)

REQUEST_SCHEMA = "SystemOneRequest"
RESPONSE_SCHEMA = "SystemOneResponse"


def _resolve(snapshot: dict, schema: dict) -> dict:
    """Follow a single `$ref` into components."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return snapshot["components"]["schemas"][name]
    return schema


def _component(snapshot: dict, name: str) -> dict:
    return snapshot["components"]["schemas"][name]


# --- the snapshot itself -----------------------------------------------------


def test_snapshot_is_the_expected_version(openapi_snapshot: dict) -> None:
    """Pinned deliberately: an upstream bump must be a conscious change here."""
    assert openapi_snapshot["openapi"].startswith("3.1")
    assert openapi_snapshot["info"]["version"] == "0.2.0"


def test_snapshot_exposes_only_the_two_documented_paths(openapi_snapshot: dict) -> None:
    assert sorted(openapi_snapshot["paths"]) == ["/v1/models", "/v1/systemone"]


def test_snapshot_requires_bearer_auth_on_both_paths(openapi_snapshot: dict) -> None:
    for path in openapi_snapshot["paths"].values():
        for operation in path.values():
            assert operation["security"] == [{"HTTPBearer": []}]


# --- request -----------------------------------------------------------------


def test_request_required_fields_match(openapi_snapshot: dict) -> None:
    documented = set(_component(openapi_snapshot, REQUEST_SCHEMA)["required"])
    assert {"model", "questions", "state"} == documented


def test_we_accept_every_documented_state_form() -> None:
    """`state` is string | object | array, with no null variant."""
    for state in ("text", {"a": 1}, [1, "two", {"three": 3}]):
        SystemOneRequest(state=state, model="mock", questions={"q": {"type": "noul"}})
    for rejected in (None, 42, True):
        with pytest.raises(Exception):  # noqa: B017 - pydantic's error type is incidental
            SystemOneRequest(state=rejected, model="mock", questions={"q": {"type": "noul"}})


def test_questions_must_be_non_empty(openapi_snapshot: dict) -> None:
    """The snapshot says minProperties: 1."""
    schema = _component(openapi_snapshot, REQUEST_SCHEMA)["properties"]["questions"]
    assert schema["minProperties"] == 1
    with pytest.raises(Exception):  # noqa: B017
        SystemOneRequest(state="x", model="mock", questions={})


def test_choice_criteria_is_required_but_may_be_empty(openapi_snapshot: dict) -> None:
    """A deliberate asymmetry with score, and a real trap.

    `ChoiceQuestion.criteria` has no `minProperties` in the snapshot, so `{}` is a
    schema-valid request that no engine can answer. `ScoreQuestion.criteria` does
    have `minItems: 1`. We reproduce the asymmetry in the schema and reject the
    empty choice later, in `schema.validate_capacity`, where we can explain it.
    """
    documented = _component(openapi_snapshot, "ChoiceQuestion")
    assert "criteria" in documented["required"]
    assert "minProperties" not in documented["properties"]["criteria"]

    # Accepted by the model...
    ChoiceQuestion(type="choice", criteria={})

    score_documented = _component(openapi_snapshot, "ScoreQuestion")
    assert score_documented["properties"]["criteria"]["minItems"] == 1
    with pytest.raises(Exception):  # noqa: B017
        ScoreQuestion(type="score", criteria=[])


def test_instructions_and_criteria_accept_arbitrary_json() -> None:
    """The documented examples include nested objects and lists."""
    NoulQuestion(type="noul", instructions={"task": "classify", "labels": ["a", "b"]})
    NoulQuestion(type="noul", instructions=["one", "two"])
    NoulQuestion(type="noul", instructions=None)
    ChoiceQuestion(type="choice", criteria={"a": {"why": "because"}, "b": None, "c": ["x"]})


def test_score_levels_may_not_be_null() -> None:
    """`ScoreQuestion.criteria` items are string | object | array, without null."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ScoreQuestion(type="score", criteria=["fine", None])


def test_unknown_fields_are_ignored_not_rejected() -> None:
    """Forward compatibility: a client sending a newer field must still be served."""
    request = SystemOneRequest(
        state="x",
        model="mock",
        questions={"q": {"type": "noul"}},
        something_from_the_future=True,
    )
    assert request.model == "mock"


# --- response ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (Usage, {"input_tokens", "output_tokens"}),
        (ModelMetadata, {"name", "description", "release_date"}),
        (ModelMetadataList, {"models"}),
        (SystemOneResponse, {"model", "answers", "usage"}),
        (NoulAnswer, {"type", "noul"}),
        (ScoreAnswer, {"type", "score", "confidence", "legend", "probabilities"}),
    ],
)
def test_response_required_fields_match(model: type, expected: set[str]) -> None:
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert required == expected


def test_usage_fields_are_non_optional_integers(openapi_snapshot: dict) -> None:
    """The snapshot types them as plain integers, so null is not allowed."""
    documented = _component(openapi_snapshot, "Usage")
    assert set(documented["required"]) == {"input_tokens", "output_tokens"}
    for name in ("input_tokens", "output_tokens"):
        assert "null" not in str(documented["properties"][name])

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Usage(input_tokens=None, output_tokens=1)


def test_noul_answer_has_no_confidence_or_probabilities(openapi_snapshot: dict) -> None:
    """A scalar. Adding either field would be a contract change."""
    documented = set(_component(openapi_snapshot, "NoulAnswer")["properties"])
    assert documented == {"type", "noul"}
    assert set(NoulAnswer.model_fields) == {"type", "noul"}


def test_choice_answer_field_set_matches(openapi_snapshot: dict) -> None:
    documented = set(_component(openapi_snapshot, "ChoiceAnswer")["properties"])
    assert documented == {"type", "choice", "confidence", "probabilities"}


def test_answers_are_a_discriminated_union_on_type(openapi_snapshot: dict) -> None:
    """`answers` maps a question id to an `Answer`, which is a oneOf on `type`."""
    answers = _component(openapi_snapshot, RESPONSE_SCHEMA)["properties"]["answers"]
    assert answers["type"] == "object"
    assert answers["additionalProperties"]["$ref"].endswith("/Answer")
    assert answers["minProperties"] == 1

    answer = _component(openapi_snapshot, "Answer")
    variants = answer.get("oneOf") or answer.get("anyOf")
    documented = {_resolve(openapi_snapshot, variant)["properties"]["type"]["const"] for variant in variants}
    assert documented == {"noul", "choice", "score"}
