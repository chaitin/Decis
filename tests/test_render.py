"""render.py: deterministic JSON -> text.

The official SDK retries POSTs, and benchmark runs compare input hashes, so the
same request must always render to byte-identical text.
"""

from __future__ import annotations

from decis.render import prepare_question, prepare_request, render_value
from decis.schema import SystemOneRequest


def test_scalars_render_without_change() -> None:
    assert render_value("  hello  ") == "hello"
    assert render_value(None) == ""
    assert render_value(42) == "42"
    assert render_value(1.5) == "1.5"
    assert render_value(True) == "true"
    assert render_value(False) == "false"


def test_dicts_render_as_sorted_key_value_lines() -> None:
    """Sorted, so the rendering does not depend on the caller's key order."""
    assert render_value({"b": 2, "a": 1}) == "a: 1\nb: 2"
    assert render_value({"b": 2, "a": 1}) == render_value({"a": 1, "b": 2})


def test_lists_render_as_bullets() -> None:
    assert render_value(["one", "two"]) == "- one\n- two"


def test_nested_structures_render_in_full() -> None:
    rendered = render_value({"order": {"id": "A-1", "items": ["box", "tape"]}})
    assert "order:" in rendered
    assert "A-1" in rendered
    assert "- box" in rendered


def test_empty_values_are_dropped_rather_than_left_as_blank_lines() -> None:
    assert render_value({"a": None, "b": "keep"}) == "b: keep"
    assert render_value(["", "keep"]) == "- keep"
    assert render_value({}) == ""
    assert render_value([]) == ""


def test_unicode_is_preserved() -> None:
    assert render_value({"备注": "已退款"}) == "备注: 已退款"


def test_rendering_is_stable_across_calls() -> None:
    payload = {"z": [3, 2, 1], "a": {"deep": {"deeper": "value"}}, "m": "text"}
    assert len({render_value(payload) for _ in range(50)}) == 1


# --- question preparation ----------------------------------------------------


def test_noul_options_are_false_then_true_regardless_of_key_order() -> None:
    """A caller writing `true` before `false` must not flip the meaning of p[1]."""
    from decis.schema import NoulQuestion

    question = NoulQuestion(
        type="noul",
        criteria={"true": "it is a complaint", "false": "it is not"},  # type: ignore[arg-type]
    )
    prepared = prepare_question("q", question)
    assert [option.name for option in prepared.options] == ["false", "true"]
    assert prepared.options[0].description == "it is not"
    assert prepared.options[1].description == "it is a complaint"


def test_noul_without_criteria_still_has_two_options() -> None:
    from decis.schema import NoulQuestion

    prepared = prepare_question("q", NoulQuestion(type="noul"))
    assert [option.name for option in prepared.options] == ["false", "true"]
    assert all(option.description == "" for option in prepared.options)


def test_score_levels_are_indexed_by_position() -> None:
    from decis.schema import ScoreQuestion

    prepared = prepare_question("q", ScoreQuestion(type="score", criteria=["low", "mid", "high"]))
    assert [(option.name, option.description) for option in prepared.options] == [
        ("0", "low"),
        ("1", "mid"),
        ("2", "high"),
    ]


def test_choice_option_names_are_the_criteria_keys_verbatim() -> None:
    """Including awkward ones: the name is a JSON key, never shown to the model."""
    from decis.schema import ChoiceQuestion

    criteria = {"very likely": "text", " unlikely ": "other", "3": "digit"}
    prepared = prepare_question("q", ChoiceQuestion(type="choice", criteria=criteria))
    assert [option.name for option in prepared.options] == list(criteria)


def test_question_text_excludes_option_names_but_includes_descriptions() -> None:
    """Only the description is model-visible; the key is a wire detail."""
    from decis.schema import ChoiceQuestion

    prepared = prepare_question(
        "q",
        ChoiceQuestion(type="choice", instructions="Pick one.", criteria={"secret_key": "say this instead"}),
    )
    text = prepared.text()
    assert "say this instead" in text
    assert "secret_key" not in text
    assert "Pick one." in text


def test_state_is_rendered_once_for_the_whole_request() -> None:
    """Every WorkItem carries the same rendered state, so batching across requests
    is possible without re-rendering per question."""
    request = SystemOneRequest(
        state={"body": "text"},
        model="stub",
        questions={f"q{i}": {"type": "noul"} for i in range(3)},
    )
    prepared = prepare_request(request)
    assert prepared.state_text == "body: text"
    assert len(prepared.questions) == 3
