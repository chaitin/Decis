"""Laya inference, with real weights.

The acceptance test for Stage 1, and the only place the engine's claims are checked
against a real model. Everything here is marked `weights` and skipped by default:
CI runs the no-weights suite, so these are run deliberately with

    uv run pytest -m weights

Batch invariance is the assertion that matters most. `docs/design.md §6.6` flags it as
a design risk: the whole throughput argument rests on joining questions from different
requests into one forward pass, and if a padded batch produced a different answer than
the same question alone, that argument would collapse. Padding is masked, so it *should*
not -- but "should" is what this file replaces with a measurement.
"""

from __future__ import annotations

import pytest

from decis.domain import PreparedQuestion, PreparedRequest
from decis.engines.base import WorkItem
from decis.engines.laya import LayaMultilingualEngine
from decis.errors import InvalidRequestError
from decis.render import prepare_request
from decis.schema import SystemOneRequest, validate_capacity

pytestmark = pytest.mark.weights


@pytest.fixture(scope="module")
def engine() -> LayaMultilingualEngine:
    """One loaded engine for the whole module: loading is ~75 s on CPU."""
    instance = LayaMultilingualEngine()
    instance.load()
    yield instance
    instance.close()


def question_of(kind: str, index: int = 0) -> PreparedQuestion:
    """A question in each primitive, built through the real renderer."""
    if kind == "noul":
        wire = {"type": "noul", "instructions": "Does the user threaten to cancel?"}
    elif kind == "choice":
        wire = {
            "type": "choice",
            "instructions": "Which department should handle this?",
            "criteria": {
                "billing": "invoices, payments, refunds",
                "technical": "bugs, outages, system errors",
                "sales": "pricing, new contracts",
            },
        }
    else:
        wire = {
            "type": "score",
            "instructions": "How urgent is this?",
            "criteria": ["Can wait", "Soon", "Today"],
        }
    request = SystemOneRequest(model="laya-multilingual", state="s", questions={f"q{index}": wire})
    return prepare_request(request).questions[0]


def test_the_engine_loads_and_reports_what_it_loaded(engine: LayaMultilingualEngine) -> None:
    info = engine.info()
    assert info.id == "laya-multilingual"
    assert info.loaded if hasattr(info, "loaded") else True
    # Real values, not the pre-load placeholders.
    assert info.device in {"cpu", "cuda", "mps"}
    assert info.dtype in {"float32", "float16", "bfloat16"}
    # The checkpoint's own budgets, read out of rl_agent_config.json.
    assert info.max_sequence_tokens > 0
    assert info.max_question_tokens > 0
    assert info.version != "not-installed"


@pytest.mark.parametrize("kind", ["noul", "choice", "score"])
def test_every_primitive_produces_a_usable_distribution(engine: LayaMultilingualEngine, kind: str) -> None:
    question = question_of(kind)
    items = [WorkItem(request_id="t", state_text="We were billed twice in March.", question=question)]
    prediction = engine.predict(items)

    assert len(prediction.probabilities) == 1
    distribution = prediction.probabilities[0]
    assert len(distribution) == len(question.options)
    assert all(0.0 <= value <= 1.0 for value in distribution)
    assert sum(distribution) == pytest.approx(1.0, abs=1e-5)
    assert prediction.input_tokens > 0
    assert prediction.native_confidences is not None
    assert 0.0 <= prediction.native_confidences[0] <= 1.0


def test_a_different_state_changes_the_answer(engine: LayaMultilingualEngine) -> None:
    """A sanity check that the state is really reaching the model.

    If `render.py` or the sequence builder dropped the state, every answer would be
    identical and every other test here would still pass.
    """
    question = question_of("noul")
    billing = engine.predict([WorkItem("a", "We were billed twice. Refund us.", question)]).probabilities[0]
    outage = engine.predict([WorkItem("b", "The dashboard returns 500 for every user.", question)]).probabilities[0]
    assert billing != outage, "the state had no effect on the answer"


def test_the_same_request_twice_gives_the_same_answer(engine: LayaMultilingualEngine) -> None:
    """`/v1/systemone` is a pure function (AGENTS.md §3-11).

    The official SDK retries POSTs, including on connection errors, so a retry that
    returned a different answer would be a correctness problem and not just an
    annoyance.
    """
    item = WorkItem("t", "We were billed twice in March.", question_of("choice"))
    first = engine.predict([item]).probabilities[0]
    second = engine.predict([item]).probabilities[0]
    assert first == pytest.approx(second, abs=1e-6)


def test_batching_across_different_states_is_invariant(engine: LayaMultilingualEngine) -> None:
    """One question alone must equal the same question inside a mixed batch.

    States differ on purpose: that is the case real traffic creates and the case
    `docs/design-review.md §2-D1` said the first design got wrong. Sequences of
    different lengths get padded to a common width, and if the attention mask were not
    honoured the shorter rows would read padding and shift.

    The tolerance is set from measurement, not taste: over 16 states, all three
    primitives and batch sizes 2/4/8/16, the observed maximum absolute deviation was
    8.3e-07 with no argmax flip (`docs/contract/stage1-batch-invariance.json`). 1e-5 is
    one order of magnitude of headroom -- loose enough not to flake on another CPU, far
    tighter than 1e-4, which a genuine masking error would sail past.
    """
    questions = [question_of(kind, index) for index, kind in enumerate(["noul", "choice", "score", "noul"])]
    states = [
        "We were billed twice in March. Please refund the duplicate today.",
        "The dashboard returns 500 for every user since the deploy.",
        "Can you add a second seat to our plan before Friday?",
        "This is the third time I have asked about this. Cancel my account.",
    ]

    alone = [
        engine.predict([WorkItem(f"solo{i}", state, question)]).probabilities[0]
        for i, (state, question) in enumerate(zip(states, questions, strict=True))
    ]

    batched = engine.predict(
        [
            WorkItem(f"batch{i}", state, question)
            for i, (state, question) in enumerate(zip(states, questions, strict=True))
        ]
    )

    assert len(batched.probabilities) == len(alone)
    for index, (single, pooled) in enumerate(zip(alone, batched.probabilities, strict=True)):
        assert single == pytest.approx(pooled, abs=1e-5), (
            f"row {index} changed when batched with other states: {single} vs {pooled}"
        )


def test_a_long_and_a_short_state_padded_together_stay_correct(engine: LayaMultilingualEngine) -> None:
    """The padding case in its sharpest form: a 5-token row next to a 400-token row."""
    question = question_of("noul")
    short = "Refund please."
    long = "We were billed twice in March. " * 20

    alone_short = engine.predict([WorkItem("s", short, question)]).probabilities[0]
    batched = engine.predict([WorkItem("s", short, question), WorkItem("l", long, question)])

    assert alone_short == pytest.approx(batched.probabilities[0], abs=1e-5)


def test_one_predict_call_handles_every_state_in_one_forward_pass(engine: LayaMultilingualEngine) -> None:
    """Evidence that the batch really formed, not just that the answers were right.

    `docs/design-review.md §2-D1` is the reason this exists: a batching implementation
    that never actually batches is indistinguishable from no batching at all unless
    something observes the batch. Here that is the model's own forward call.
    """
    calls = 0
    original = engine._agent.model

    def counting_forward(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    question = question_of("noul")
    engine._agent.model = counting_forward
    try:
        prediction = engine.predict([WorkItem(f"r{i}", f"state number {i}", question) for i in range(8)])
    finally:
        engine._agent.model = original

    assert len(prediction.probabilities) == 8
    assert calls == 1, f"8 items from 8 different states took {calls} forward passes, expected 1"

    # And the batch really was 8 rows wide, not 8 batches of one.
    _, markers = __import__("laya.common", fromlist=["build_sequence"]).build_sequence(
        engine._agent.tok, "state number 0", engine._internal(question), 512, 192
    )
    assert len(markers) == len(question.options)


def test_input_tokens_reflect_the_real_batch(engine: LayaMultilingualEngine) -> None:
    """`input_tokens` is the encoder's own token count, not an estimate."""
    question = question_of("noul")
    one = engine.predict([WorkItem("a", "short state", question)]).input_tokens
    three = engine.predict([WorkItem(f"r{i}", "short state", question) for i in range(3)]).input_tokens
    assert 0 < one < three
    # Three identical sequences should cost about three times one, less shared padding.
    assert three == pytest.approx(3 * one, rel=0.35)


def test_an_over_long_state_is_refused_instead_of_truncated(engine: LayaMultilingualEngine) -> None:
    """The payoff of `measure`. `build_sequence` would cut the tail in silence.

    Upstream reports nothing when it truncates, so without the pre-check the caller
    would receive a confident answer computed from a fraction of their input.
    """
    info = engine.info()
    # Fits `max_sequence_tokens` alone; does not fit once the head is added.
    huge = "word " * (info.max_sequence_tokens - 5)
    prepared = prepare_request(
        SystemOneRequest(model="laya-multilingual", state=huge, questions={"q": {"type": "noul", "instructions": "x"}})
    )
    with pytest.raises(InvalidRequestError) as caught:
        validate_capacity(prepared, info, engine.measure)
    assert caught.value.status == 422
    assert "longest question" in caught.value.body()["detail"][0]["msg"]


def test_a_request_at_the_documented_limit_is_not_truncated(engine: LayaMultilingualEngine) -> None:
    """The boundary has to accept *something*, or the check would be vacuous.

    Also asserts the sequence `build_sequence` produces for an accepted request is
    shorter than the budget, which is what "not truncated" means concretely.
    """
    from laya.common import build_sequence

    info = engine.info()
    prepared = prepare_request(
        SystemOneRequest(
            model="laya-multilingual",
            state="t" * (info.max_sequence_tokens // 4),
            questions={"q": {"type": "noul", "instructions": "Is it so?"}},
        )
    )
    measured = validate_capacity(prepared, info, engine.measure)
    assert measured.sequence_tokens <= info.max_sequence_tokens

    question = prepared.questions[0]
    ids, markers = build_sequence(
        engine._agent.tok,
        prepared.state_text,
        engine._internal(question),
        info.max_sequence_tokens,
        info.max_question_tokens,
    )
    assert len(ids) < info.max_sequence_tokens, "the sequence filled the budget exactly; truncation is likely"
    assert len(markers) == len(question.options)


def test_measure_agrees_with_the_sequence_laya_actually_builds(engine: LayaMultilingualEngine) -> None:
    """`measure` is a prediction about `build_sequence`; check it against the real thing.

    If `measure` under-reported, an over-long request would pass validation and be
    truncated. If it over-reported, valid requests would be rejected. Both are bugs, so
    this asserts the two agree closely on a realistic request.
    """
    from laya.common import build_sequence

    prepared = PreparedRequest(
        model="laya-multilingual",
        state_text="We were billed twice in March. Please refund the duplicate today.",
        questions=(
            question_of("noul", 0),
            question_of("choice", 1),
            question_of("score", 2),
        ),
    )
    measured = engine.measure(prepared)
    lengths = {}
    for question in prepared.questions:
        ids, _ = build_sequence(
            engine._agent.tok,
            prepared.state_text,
            engine._internal(question),
            engine.info().max_sequence_tokens,
            engine.info().max_question_tokens,
        )
        lengths[question.qid] = len(ids)

    # `sequence_tokens` is a single figure covering the whole request -- each question is
    # its own sequence, so the binding one is the longest. Exact, in both directions: a
    # prediction that is too low lets a truncation through, and one that is too high
    # rejects a valid request.
    assert measured.sequence_tokens == max(lengths.values()), (
        f"measure predicted {measured.sequence_tokens}; build_sequence produced {lengths}"
    )
    # And the state really is the bulk of it, not a placeholder.
    assert measured.sequence_tokens > measured.state_tokens


def test_the_state_actually_reaches_the_model_through_measure(engine: LayaMultilingualEngine) -> None:
    """`state_tokens` must be the real tokenizer's count, not a character estimate."""
    state = "We were billed twice in March. " * 10
    prepared = PreparedRequest(model="laya-multilingual", state_text=state, questions=(question_of("noul"),))
    measured = engine.measure(prepared)
    from laya.common import serialize_state

    # `add_special_tokens=False` because that is what `build_sequence` passes; the
    # default would add CLS and SEP and make the count disagree by two for the wrong
    # reason.
    expected = len(engine._agent.tok(serialize_state(prepared.state_text), add_special_tokens=False)["input_ids"])
    assert measured.state_tokens == expected
    # And it is a real tokenizer count, not len(text) // 4.
    assert measured.state_tokens != len(state) // 4
