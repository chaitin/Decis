"""L2: the invariants of a successful response.

Table-driven over AGENTS.md §3. These are the promises the project exists to keep:
if one of them breaks, an existing jev client breaks with it.
"""

from __future__ import annotations

import pytest

# --- AGENTS.md §3-7: the top level is always the same three keys --------------


def test_response_has_exactly_model_answers_and_usage(client, auth, mixed_request) -> None:
    body = client.post("/v1/systemone", json=mixed_request, headers=auth).json()
    assert {"model", "answers", "usage"} <= set(body)
    # `decis` is the only permitted addition, and it is namespaced.
    assert set(body) - {"model", "answers", "usage"} == {"decis"}


def test_usage_is_two_plain_integers(client, auth, mixed_request) -> None:
    usage = client.post("/v1/systemone", json=mixed_request, headers=auth).json()["usage"]
    assert set(usage) == {"input_tokens", "output_tokens"}
    for name, value in usage.items():
        assert isinstance(value, int) and not isinstance(value, bool), f"{name} must be a plain int"
        assert value >= 0


def test_model_is_the_versioned_id_not_the_request_alias(client, auth, mixed_request) -> None:
    """AGENTS.md §3-8: feeding the response's `model` back in must work."""
    body = client.post("/v1/systemone", json=mixed_request, headers=auth).json()
    assert body["model"] == "decis/stub@0.1.0"

    echoed = dict(mixed_request, model=body["model"])
    again = client.post("/v1/systemone", json=echoed, headers=auth)
    assert again.status_code == 200
    assert again.json()["model"] == body["model"]


@pytest.mark.parametrize("alias", ["stub", "decis-stub", "stub-engine", "decis/stub@0.1.0"])
def test_every_alias_is_accepted(client, auth, noul_request, alias) -> None:
    response = client.post("/v1/systemone", json=dict(noul_request, model=alias), headers=auth)
    assert response.status_code == 200


def test_foreign_default_model_is_substituted_and_reported(client, auth, noul_request) -> None:
    """The SDK defaults to `jev-latest`; swapping only TYPESAFE_BASE_URL must work."""
    body = client.post("/v1/systemone", json=dict(noul_request, model="jev-latest"), headers=auth).json()
    assert body["model"] == "decis/stub@0.1.0"
    assert body["decis"]["requested_model"] == "jev-latest"


def test_unknown_model_is_rejected_with_a_useful_message(client, auth, noul_request) -> None:
    response = client.post("/v1/systemone", json=dict(noul_request, model="gpt-9"), headers=auth)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list), "validation failures use the list shape"
    assert detail[0]["loc"] == ["body", "model"]
    assert "gpt-9" in detail[0]["msg"]
    assert "stub" in detail[0]["msg"], "the message must say what is available"


# --- AGENTS.md §3-6: answer keys mirror question keys -------------------------


def test_answer_keys_equal_question_keys(client, auth, mixed_request) -> None:
    body = client.post("/v1/systemone", json=mixed_request, headers=auth).json()
    assert set(body["answers"]) == set(mixed_request["questions"])


def test_question_ids_are_never_sent_to_the_model(client, auth) -> None:
    """A question id is a JSON key, not prompt text. It must not reach the engine."""
    from decis.domain import PreparedRequest
    from decis.render import prepare_request
    from decis.schema import SystemOneRequest

    secret = "qid-that-must-not-be-rendered"
    request = SystemOneRequest(
        state="some content",
        model="stub",
        questions={secret: {"type": "choice", "criteria": {"a": "yes", "b": "no"}}},
    )
    prepared: PreparedRequest = prepare_request(request)
    assert secret not in prepared.state_text
    for question in prepared.questions:
        assert secret not in question.text()
        assert secret not in question.instructions


# --- AGENTS.md §3-1..3-5: per-primitive invariants ---------------------------


def test_noul_is_a_scalar_with_no_confidence_or_probabilities(client, auth, noul_request) -> None:
    answer = client.post("/v1/systemone", json=noul_request, headers=auth).json()["answers"]["billing"]
    assert set(answer) == {"type", "noul"}
    assert answer["type"] == "noul"
    assert 0.0 <= answer["noul"] <= 1.0


def test_choice_invariants(client, auth, mixed_request) -> None:
    answer = client.post("/v1/systemone", json=mixed_request, headers=auth).json()["answers"]["tone"]
    criteria = mixed_request["questions"]["tone"]["criteria"]

    assert set(answer) == {"type", "choice", "confidence", "probabilities"}
    # AGENTS.md §3-4: keys identical to the request's, order preserved.
    assert list(answer["probabilities"]) == list(criteria)
    assert answer["choice"] in criteria
    # The winner is the argmax of the published values.
    assert answer["choice"] == max(answer["probabilities"], key=answer["probabilities"].__getitem__)
    assert 0.0 <= answer["confidence"] <= 1.0
    assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=0.03)


def test_score_invariants(client, auth, mixed_request) -> None:
    answer = client.post("/v1/systemone", json=mixed_request, headers=auth).json()["answers"]["urgency"]
    assert set(answer) == {"type", "score", "confidence", "legend", "probabilities"}

    # AGENTS.md §3-1: the keys are the STRINGS "0", "1", ... Never integers.
    assert list(answer["legend"]) == ["0", "1", "2", "3"]
    assert list(answer["probabilities"]) == ["0", "1", "2", "3"]
    assert all(isinstance(key, str) for key in answer["legend"])

    # AGENTS.md §3-5: score is the expected level under the published values.
    expected = sum(int(key) * value for key, value in answer["probabilities"].items())
    assert answer["score"] == pytest.approx(expected, abs=0.01)
    assert 0.0 <= answer["score"] <= 3.0
    assert 0.0 <= answer["confidence"] <= 1.0
    assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=0.03)


def test_choice_with_one_option_is_legal_and_certain(client, auth) -> None:
    response = client.post(
        "/v1/systemone",
        json={
            "state": "x",
            "model": "stub",
            "questions": {"only": {"type": "choice", "criteria": {"yes": "the only choice"}}},
        },
        headers=auth,
    )
    answer = response.json()["answers"]["only"]
    assert answer["choice"] == "yes"
    assert answer["confidence"] == 1.0


def test_single_level_score_is_legal(client, auth) -> None:
    response = client.post(
        "/v1/systemone",
        json={"state": "x", "model": "stub", "questions": {"only": {"type": "score", "criteria": ["the only level"]}}},
        headers=auth,
    )
    answer = response.json()["answers"]["only"]
    assert answer["score"] == 0.0
    assert answer["confidence"] == 1.0
    assert answer["legend"] == {"0": "the only level"}


def test_empty_choice_criteria_is_rejected_with_an_explanation(client, auth) -> None:
    """Schema-legal (no minProperties upstream) but unanswerable -- so we explain it."""
    response = client.post(
        "/v1/systemone",
        json={"state": "x", "model": "stub", "questions": {"q": {"type": "choice", "criteria": {}}}},
        headers=auth,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "questions", "q", "criteria"]
    assert "nothing to choose" in detail[0]["msg"]


# --- AGENTS.md §3-11: POST is a pure function --------------------------------


def test_same_request_gives_the_same_answer(client, auth, mixed_request) -> None:
    """The official SDK retries POSTs, so a retry must not change the answer."""
    first = client.post("/v1/systemone", json=mixed_request, headers=auth).json()
    second = client.post("/v1/systemone", json=mixed_request, headers=auth).json()
    assert first["answers"] == second["answers"]
    assert first["usage"] == second["usage"]


def test_multiple_questions_keep_their_own_state(client, auth) -> None:
    """One request, several questions, still one answer each -- and in order."""
    request = {
        "state": "The delivery was late and the box was crushed.",
        "model": "stub",
        "questions": {
            f"q{index}": {"type": "choice", "criteria": {"a": f"option a {index}", "b": f"option b {index}"}}
            for index in range(5)
        },
    }
    body = client.post("/v1/systemone", json=request, headers=auth).json()
    assert list(body["answers"]) == list(request["questions"])


def test_state_forms_all_work(client, auth) -> None:
    for state in ("plain text", {"key": "value"}, ["one", {"two": 2}, ["three"]]):
        response = client.post(
            "/v1/systemone",
            json={"state": state, "model": "stub", "questions": {"q": {"type": "noul"}}},
            headers=auth,
        )
        assert response.status_code == 200, state


def test_large_state_is_rejected_rather_than_truncated(client, auth) -> None:
    """AGENTS.md §5-4: refuse over-capacity input instead of degrading silently."""
    huge = "word " * 60_000  # ~300k characters, far past the stub's 32k-token budget
    response = client.post(
        "/v1/systemone",
        json={"state": huge, "model": "stub", "questions": {"q": {"type": "noul"}}},
        headers=auth,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "state"]
    assert "limit" in detail[0]["msg"]


def test_too_many_options_is_rejected(client, auth) -> None:
    response = client.post(
        "/v1/systemone",
        json={
            "state": "x",
            "model": "stub",
            "questions": {"q": {"type": "choice", "criteria": {f"opt{i}": None for i in range(300)}}},
        },
        headers=auth,
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"][0]["msg"]
