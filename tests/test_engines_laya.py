"""The Laya engine's normalisation and capacity arithmetic, without torch.

Splits deliberately from `tests/test_upstream_contract.py`:

* **here** -- that `_internal` produces the dict shape Laya's primitives read, and
  that `measure` charges the state for the room the head consumes. This is the
  arithmetic where a silent truncation hid, and it must be covered in the fast suite
  that CI always runs.
* **there** -- that those shapes and constants match the *real* `build_sequence`. That
  needs the `laya` extra, so it is skipped without it.

`_options` is overridden with a local stand-in so no `laya` import is needed. That is
sound precisely because this module is not testing Laya's rendering: the upstream test
asserts `_internal`'s dict is what `render_options` consumes, and what the real
renderer returns. What is tested here is what the engine does with those numbers.
"""

from __future__ import annotations

import pytest

from decis.domain import MeasuredTokens, Option, PreparedQuestion, PreparedRequest
from decis.engines.laya import (
    CHECKPOINT_DATE,
    REVISION,
    WEIGHTS,
    LayaEngine,
    LayaMultilingualEngine,
    LayaTypedDecisionsEngine,
    budgeted_head,
)
from decis.errors import InvalidRequestError
from decis.render import prepare_request
from decis.schema import SystemOneRequest, validate_capacity


class CountingTokenizer:
    """One token per character, so every expected count is obvious in the assertion."""

    def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
        del add_special_tokens
        return {"input_ids": [0] * len(text)}


class _FakeDevice:
    type = "cpu"


class _FakeAgent:
    """Enough of a loaded `laya.Agent` for `info()` to describe it.

    `info()` reads `device` and `dtype` off the loaded agent, so a stub without them
    would make every capacity assertion fail for a reason unrelated to capacity.
    """

    tok = CountingTokenizer()
    device = _FakeDevice()
    dtype = "float32"


class StubOptionsEngine(LayaEngine):
    """Real `measure`/`_head_cost`/`budgeted_head`, with Laya's renderer stubbed out.

    The stub reproduces only the two decorations that matter to the arithmetic --
    `"name: "` for choice options and `"level N: "` for score levels -- so the test can
    assert they are counted. The real renderer is what `test_upstream_contract.py`
    checks.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self._agent = _FakeAgent()
        for key, value in kwargs.items():
            setattr(self, key, value)

    def _options(self, internal: dict) -> list[str]:
        if internal["t"] == "score":
            return [f"level {i}: {text}" for i, text in enumerate(internal["crit"])]
        return [key if not value else f"{key}: {value}" for key, value in internal["crit"].items()]


def request_of(questions: dict, *, state: str = "a state", model: str = "laya") -> PreparedRequest:
    return prepare_request(SystemOneRequest(model=model, state=state, questions=questions))


def noul(**overrides: object) -> dict:
    return {"type": "noul", "instructions": "Is it so?", **overrides}


def choice(criteria: dict, **overrides: object) -> dict:
    return {"type": "choice", "instructions": "Which one?", "criteria": criteria, **overrides}


def score(criteria: list, **overrides: object) -> dict:
    return {"type": "score", "instructions": "How much?", "criteria": criteria, **overrides}


# --- _internal: the dict shape Laya reads -------------------------------------


def test_choice_passes_the_criterion_names_as_keys() -> None:
    """Laya renders `"name: description"`, so for choice the wire key *is* prompt text."""
    engine = LayaEngine()
    prepared = PreparedQuestion(
        qid="q",
        type="choice",
        instructions="Which team?",
        options=(Option("billing", "invoices"), Option("tech", "bugs")),
    )
    assert engine._internal(prepared) == {
        "t": "choice",
        "ins": "Which team?",
        "crit": {"billing": "invoices", "tech": "bugs"},
    }


def test_an_undescribed_criterion_becomes_none_not_an_empty_string() -> None:
    """`None` and `""` mean the same thing to Laya, but `render_options` treats an
    empty description as absent. Passing `None` says that once, at the boundary."""
    engine = LayaEngine()
    prepared = PreparedQuestion(
        qid="q",
        type="choice",
        instructions="",
        options=(Option("bare", ""), Option("full", "text")),
    )
    assert engine._internal(prepared)["crit"] == {"bare": None, "full": "text"}


def test_score_passes_only_descriptions_because_laya_labels_the_levels() -> None:
    engine = LayaEngine()
    prepared = PreparedQuestion(
        qid="q",
        type="score",
        instructions="How urgent?",
        options=(Option("0", "low"), Option("1", "high")),
    )
    assert engine._internal(prepared)["crit"] == ["low", "high"]


def test_noul_passes_false_and_true_in_contract_order() -> None:
    engine = LayaEngine()
    prepared = PreparedQuestion(
        qid="q",
        type="noul",
        instructions="Is it so?",
        options=(Option("false", "no"), Option("true", "yes")),
    )
    assert engine._internal(prepared)["crit"] == {"false": "no", "true": "yes"}


def test_an_empty_description_survives_the_full_render_path() -> None:
    """End to end through the real renderer, so the `None` bridge is exercised."""
    engine = LayaEngine()
    prepared = request_of(
        {"q": {"type": "choice", "instructions": "", "criteria": {"a": None, "b": "text"}}},
        state="s",
    )
    assert engine._internal(prepared.questions[0])["crit"] == {"a": None, "b": "text"}


# --- measure: the arithmetic that prevents silent truncation ------------------


def test_measure_before_load_falls_back_to_the_character_estimate() -> None:
    """An unloaded engine cannot tokenize, so it must not pretend to.

    Falls back to the shared estimate rather than reporting zeros, which would make
    every capacity check pass.
    """
    measured = LayaEngine().measure(request_of({"q": noul()}))
    assert isinstance(measured, MeasuredTokens)
    assert set(measured.head_tokens) == {"q"}
    assert measured.sequence_tokens > 0
    assert measured.state_tokens > 0


def test_the_state_is_charged_for_the_room_the_head_consumes() -> None:
    """The bug this whole mechanism exists for.

    A state can fit `max_sequence_tokens` on its own and still not fit once the head is
    included -- and `build_sequence` then cuts the state's tail without reporting it.
    `sequence_tokens` must reflect the sum, not the state alone.
    """
    engine = StubOptionsEngine()
    engine._max_len = 200
    engine._head_max_len = 192

    # Pick the longest state that still fits the window on its own. The head alone is
    # enough to push it over, which is exactly the case a state-only check misses.
    requests = request_of({"q": noul()}, state="s" * (engine._max_len - 1))
    measured = engine.measure(requests)

    # The state alone is inside the context window...
    assert measured.state_tokens == engine._max_len - 1 < engine._max_len
    # ...but the head is charged on top of it, and the two together exceed it.
    assert measured.sequence_tokens > engine._max_len
    assert measured.sequence_tokens > measured.state_tokens

    # And validate_capacity turns exactly that into a 422 that names `state`.
    with pytest.raises(InvalidRequestError) as caught:
        validate_capacity(requests, engine.info(), engine.measure)
    assert caught.value.status == 422
    detail = caught.value.body()["detail"][0]
    assert detail["loc"] == ["body", "state"]
    assert "longest question" in detail["msg"]


def test_a_short_state_with_the_same_head_still_passes() -> None:
    """The check must not reject everything -- it has to be a real boundary."""
    engine = StubOptionsEngine()
    engine._max_len = 200
    engine._head_max_len = 192
    requests = request_of({"q": noul()}, state="s" * 10)
    measured = validate_capacity(requests, engine.info(), engine.measure)
    assert measured.sequence_tokens <= engine._max_len


def test_measure_counts_the_option_decorations_laya_adds() -> None:
    """The criterion *name* is prompt text for `choice`, and must be charged for.

    Asserted by difference rather than by reconstructing the layout: two more
    characters in the criterion name must cost exactly two more tokens. Measuring
    `PreparedQuestion.text()` would not notice, because it only contains descriptions
    -- and that under-count is how a request passes validation and is then truncated.
    """
    engine = StubOptionsEngine()
    short = engine.measure(request_of({"q": choice({"a": "desc"})}))
    longer = engine.measure(request_of({"q": choice({"aaaa": "desc"})}))
    assert longer.head_tokens["q"] - short.head_tokens["q"] == 3  # 3 extra name chars

    # Same for the instructions, which Laya prefixes with "<type> question: ".
    brief = engine.measure(request_of({"q": choice({"a": "d"}, instructions="x")}))
    wordy = engine.measure(request_of({"q": choice({"a": "d"}, instructions="xxx")}))
    assert wordy.head_tokens["q"] - brief.head_tokens["q"] == 2


def test_measure_reports_one_head_per_question() -> None:
    engine = StubOptionsEngine()
    prepared = PreparedRequest(
        model="laya",
        state_text="s",
        questions=(
            PreparedQuestion("short", "noul", "x", (Option("false", "no"), Option("true", "yes"))),
            PreparedQuestion("long", "noul", "x" * 50, (Option("false", "no"), Option("true", "yes"))),
        ),
    )
    measured = engine.measure(prepared)
    assert set(measured.head_tokens) == {"short", "long"}
    assert measured.head_tokens["long"] > measured.head_tokens["short"]
    # The sequence budget is set by the *largest* head, since each question is its own
    # sequence.
    assert measured.sequence_tokens == max(measured.head_tokens.values()) + measured.state_tokens + 4


def test_a_question_over_the_head_budget_is_rejected_by_name() -> None:
    engine = StubOptionsEngine()
    engine._max_len = 4000
    engine._head_max_len = 40
    requests = request_of({"huge": choice({"a": "x" * 200})}, state="s")
    with pytest.raises(InvalidRequestError) as caught:
        validate_capacity(requests, engine.info(), engine.measure)
    assert caught.value.body()["detail"][0]["loc"] == ["body", "questions", "huge"]
    assert caught.value.status == 422


def test_budgeted_head_is_monotonic_and_reserves_the_option_budget() -> None:
    """A necessary property of the comparison, independent of upstream's internals."""
    assert budgeted_head(0, 0) == 16
    assert budgeted_head(100, 10) == 110
    assert budgeted_head(10, 100) == 116
    assert budgeted_head(20, 10) < budgeted_head(21, 10)
    assert budgeted_head(10, 20) < budgeted_head(10, 21)


# --- declarations -------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine_class", "engine_id"),
    [
        (LayaEngine, "laya"),
        (LayaMultilingualEngine, "laya-multilingual"),
        (LayaTypedDecisionsEngine, "laya-typed-decisions"),
    ],
)
def test_each_checkpoint_is_its_own_engine(engine_class: type[LayaEngine], engine_id: str) -> None:
    engine = engine_class()
    assert engine.info().id == engine_id
    assert engine.weights() is WEIGHTS[engine_id]


def test_weights_are_pinned_to_a_commit_not_a_branch() -> None:
    """A branch would let upstream change what a released Decis image loads."""
    for engine_id, spec in WEIGHTS.items():
        assert spec.revision == REVISION, f"{engine_id} is not pinned"
        assert len(REVISION) == 40 and all(c in "0123456789abcdef" for c in REVISION)
        assert spec.marker == "rl_agent_config.json"
        assert spec.repo_id == "convaiinnovations/laya"
        assert spec.license_name == "Apache-2.0"
        assert spec.expected_bytes and spec.expected_bytes > 0


def test_two_checkpoints_share_one_repository_but_not_one_subfolder() -> None:
    """The subfolder is what keeps them from loading each other's weights."""
    subfolders = {spec.subfolder for spec in WEIGHTS.values()}
    assert subfolders == {None, "multilingual", "typed-decisions"}


def test_capabilities_are_declared_without_loading() -> None:
    info = LayaEngine().info()
    assert info.primitives == frozenset({"noul", "choice", "score"})
    assert info.max_sequence_tokens > 0
    assert info.max_question_tokens > 0
    # Honest until something is loaded.
    assert info.device == "unloaded"
    assert info.dtype == "unloaded"
    # The version cannot be known without the package, and must not be invented.
    assert info.version == "not-installed" or info.version[0].isdigit()
    assert info.release_date == CHECKPOINT_DATE
