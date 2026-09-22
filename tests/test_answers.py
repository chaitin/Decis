"""answers.py: the single definition of every wire-level decision."""

from __future__ import annotations

import pytest

from decis.answers import (
    build_answer,
    choice_confidence,
    estimate_output_tokens,
    question_keys,
    round_probs,
    score_confidence,
)
from decis.domain import Option, PreparedQuestion
from decis.errors import InvalidRequestError


def _question(kind: str, names: list[str]) -> PreparedQuestion:
    return PreparedQuestion(
        qid="q",
        type=kind,  # type: ignore[arg-type]
        instructions="",
        options=tuple(Option(name, f"description of {name}") for name in names),
    )


# --- wire keys (AGENTS.md §3-1, §3-3, §3-4) ----------------------------------


def test_noul_keys_are_false_then_true() -> None:
    """The order carries the meaning: noul = p[1]."""
    assert question_keys(_question("noul", ["false", "true"])) == ["false", "true"]


def test_choice_keys_keep_request_order() -> None:
    assert question_keys(_question("choice", ["b", "a", "c"])) == ["b", "a", "c"]


def test_score_keys_are_decimal_strings() -> None:
    keys = question_keys(_question("score", ["0", "1", "10"]))
    assert keys == ["0", "1", "10"]
    assert all(isinstance(key, str) for key in keys)


# --- noul --------------------------------------------------------------------


def test_noul_reports_the_true_probability() -> None:
    answer = build_answer(_question("noul", ["false", "true"]), [0.3, 0.7])
    assert answer.type == "noul"
    assert answer.noul == 0.7
    assert not hasattr(answer, "confidence")


# --- choice ------------------------------------------------------------------


def test_choice_picks_the_largest_and_reports_every_option() -> None:
    answer = build_answer(_question("choice", ["a", "b", "c"]), [0.1, 0.7, 0.2])
    assert answer.choice == "b"
    assert answer.probabilities == {"a": 0.1, "b": 0.7, "c": 0.2}


def test_choice_is_consistent_with_the_published_probabilities() -> None:
    """The winner is computed from the rounded values a client actually sees.

    Computing it from the unrounded ones would let `choice` name an option that is
    not the argmax of `probabilities` -- a self-contradictory response.
    """
    # Rounds to a tie at 4 decimals, so the earliest option wins by the documented
    # tie rule. The unrounded values would have picked the second.
    answer = build_answer(_question("choice", ["a", "b"]), [0.49999, 0.50001])
    assert answer.probabilities == {"a": 0.5, "b": 0.5}
    assert answer.choice == "a"
    assert answer.choice == max(answer.probabilities, key=answer.probabilities.__getitem__)


def test_choice_ties_go_to_the_first_option() -> None:
    answer = build_answer(_question("choice", ["first", "second", "third"]), [0.5, 0.5, 0.0])
    assert answer.choice == "first"


def test_choice_with_no_options_is_refused() -> None:
    with pytest.raises(InvalidRequestError) as caught:
        build_answer(_question("choice", []), [])
    assert "nothing to choose" in caught.value.message
    assert caught.value.status == 422


def test_a_wrong_length_distribution_is_an_engine_bug() -> None:
    with pytest.raises(InvalidRequestError) as caught:
        build_answer(_question("choice", ["a", "b"]), [1.0])
    assert "probabilities" in caught.value.message


# --- score -------------------------------------------------------------------


def test_score_is_the_expected_level_with_string_keys() -> None:
    answer = build_answer(_question("score", ["0", "1", "2"]), [0.1, 0.2, 0.7])
    assert answer.score == pytest.approx(1.6)
    assert answer.probabilities == {"0": 0.1, "1": 0.2, "2": 0.7}
    assert list(answer.legend) == ["0", "1", "2"]
    assert answer.legend["2"] == "description of 2"


def test_score_matches_a_recomputation_from_the_published_values() -> None:
    answer = build_answer(_question("score", ["0", "1", "2", "3"]), [0.2, 0.3, 0.4, 0.1])
    recomputed = sum(int(level) * probability for level, probability in answer.probabilities.items())
    assert answer.score == pytest.approx(recomputed)


def test_score_of_a_single_level_is_zero() -> None:
    answer = build_answer(_question("score", ["0"]), [1.0])
    assert answer.score == 0.0
    assert answer.confidence == 1.0
    assert answer.legend == {"0": "description of 0"}


# --- confidence --------------------------------------------------------------


def test_choice_confidence_spans_zero_to_one() -> None:
    assert choice_confidence([0.5, 0.5]) == 0.0
    assert choice_confidence([1.0, 0.0]) == 1.0
    assert 0.0 < choice_confidence([0.7, 0.3]) < 1.0
    assert choice_confidence([1.0]) == 1.0


def test_choice_confidence_is_normalised_for_option_count() -> None:
    """Two options at 0.5/0.5 and four at 0.25 each are equally uninformative."""
    assert choice_confidence([0.5, 0.5]) == 0.0
    assert choice_confidence([0.25, 0.25, 0.25, 0.25]) == 0.0
    assert choice_confidence([1.0, 0.0, 0.0, 0.0]) == 1.0


def test_score_confidence_is_zero_for_indifference_and_one_for_certainty() -> None:
    assert score_confidence([1.0, 0.0, 0.0]) == 1.0
    assert score_confidence([0.0, 1.0, 0.0]) == 1.0
    # Uniform means no opinion, at any number of levels -- the same convention as
    # choice_confidence.
    assert score_confidence([1 / 3, 1 / 3, 1 / 3]) == 0.0
    assert score_confidence([0.25] * 4) == 0.0
    assert 0.0 < score_confidence([0.1, 0.8, 0.1]) < 1.0
    assert score_confidence([1.0]) == 1.0


def test_both_confidences_share_a_scale() -> None:
    """0 means "no opinion" and 1 means "certain" for both primitives."""
    assert choice_confidence([0.5, 0.5]) == score_confidence([0.5, 0.5]) == 0.0
    assert choice_confidence([1.0, 0.0]) == score_confidence([1.0, 0.0]) == 1.0


def test_confidence_stays_in_range_for_extreme_inputs() -> None:
    for probabilities in ([1.0, 0.0], [0.0, 1.0], [1 / 3] * 3, [1.0] + [0.0] * 9):
        assert 0.0 <= choice_confidence(probabilities) <= 1.0
        assert 0.0 <= score_confidence(probabilities) <= 1.0


# --- rounding and usage ------------------------------------------------------


def test_rounding_preserves_the_distribution_shape() -> None:
    rounded = round_probs([1 / 3, 1 / 3, 1 / 3])
    assert sum(rounded) == pytest.approx(1.0, abs=0.01)
    assert all(len(str(value).split(".")[-1]) <= 4 for value in rounded)


def test_output_tokens_is_the_size_of_the_serialised_answers() -> None:
    """A billing convention, and always a plain int."""
    small = estimate_output_tokens({"a": {"type": "noul", "noul": 0.5}})
    large = estimate_output_tokens({"a": {"type": "score", "legend": {"0": "x" * 400}, "probabilities": {}}})
    assert isinstance(small, int)
    assert large > small


# --- degenerate input --------------------------------------------------------


def test_empty_distribution_is_refused() -> None:
    with pytest.raises(InvalidRequestError):
        build_answer(_question("noul", []), [])
