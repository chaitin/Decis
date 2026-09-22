"""kev against real weights. Run with `pytest -m weights`.

These are the tests that can only exist with the checkpoint in memory, and they are
where kev's specific hazards are pinned:

* the record this engine builds is identical to kev's own `api.to_record`, so the model
  sees the text it was trained on (the D9 finding -- a silent-degradation bug otherwise);
* the probabilities match upstream's own `model.probs()` to floating-point noise, so the
  batched path Decis uses is the single-record path upstream publishes numbers with;
* a state over kev's 384-token window is **refused**, not truncated.

The oracle is upstream's own code, imported from the research checkout. If that
checkout is absent the comparison is skipped rather than faked -- an unverified
"matches upstream" claim is worse than no claim (`AGENTS.md §8`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from decis.domain import PreparedQuestion
from decis.engines.base import WorkItem
from decis.engines.kev import MAX_BRANCH
from decis.errors import InvalidRequestError
from decis.render import noul_options, prepare_request
from decis.schema import SystemOneRequest

pytestmark = pytest.mark.weights

UPSTREAM = Path("/data/src/github.com/jaredpalmer/kev")

REQUEST = {
    "model": "kev-0.8b",
    "state": "Shoes arrived two weeks late and in the wrong size. Also I see two charges on my card.",
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "Which team should handle this?",
            "criteria": {
                "returns": "Exchanges, refunds, wrong or damaged items",
                "shipping": "Delivery status, delays, lost packages",
                "billing": "Charges, invoices, payment problems",
            },
        },
        "escalate": {
            "type": "noul",
            "instructions": "Does this need urgent human attention?",
            "criteria": {"true": "Needs a person now", "false": "Can wait"},
        },
        "frustration": {
            "type": "score",
            "instructions": "How frustrated is the customer?",
            "criteria": ["Calm", "Frustrated", "Very angry"],
        },
    },
}


def _weights_are_cached(spec) -> bool:
    """Is this checkpoint readable right now, locally or from the Hub cache?

    `paths.resolve` only knows about `DECIS_MODEL_DIR` and explicit overrides; a
    checkpoint fetched by `decis download` into the **Hub cache** is neither, so
    checking `source.is_local` here would skip these tests on the very machine that has
    the weights. The base model is checked too: an adapter without its base cannot run.
    """
    from decis.paths import resolve

    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:  # pragma: no cover - the kev extra provides it
        return resolve(spec, _settings()).is_local

    for one in (spec, *spec.base_specs()):
        if resolve(one, _settings()).is_local:
            continue
        hit = try_to_load_from_cache(one.repo_id, one.marker, revision=one.revision)
        if not isinstance(hit, str):
            return False
    return True


def _settings():
    from decis.config import load_settings

    return load_settings()


@pytest.fixture(scope="module")
def engine():
    """One loaded engine for the whole module: the load costs ~40 s and ~3 GB."""
    from decis.engines.registry import create

    instance = create("kev-0.8b")
    if not _weights_are_cached(instance.weights()):
        pytest.skip("kev-0.8b weights are not present; run: decis download --engine kev-0.8b")
    instance.load()
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def upstream():
    """Upstream's `api` module, or skip if the research checkout is not on this machine."""
    if not (UPSTREAM / "kev/api.py").is_file():
        pytest.skip(f"upstream kev checkout not found at {UPSTREAM}")
    # `kev/__init__.py` imports nothing heavy, but `kev.api` needs pydantic only.
    sys.path.insert(0, str(UPSTREAM))
    try:
        import kev.api as api
    finally:
        sys.path.remove(str(UPSTREAM))
    return api


def _items(engine) -> list[WorkItem]:
    prepared = prepare_request(SystemOneRequest.model_validate(REQUEST))
    return [
        WorkItem(request_id="r1", state_text=prepared.state_text, question=question) for question in prepared.questions
    ]


# --- the D9 pin: the model must see its own option text ------------------------


def test_the_record_matches_upstreams_serving_path(engine, upstream) -> None:
    """Decis's record must equal `kev/api.py:to_record`'s, field for field.

    This is the guard for D9. kev renders noul options as `"no"`/`"yes"` and score
    levels as bare text, where Decis's renderer produces `"false"`/`"true"` and
    `"0: <level>"`. Since `kev/data.py:395 materialize()` builds *training* records
    "via the serving path (api.to_record)", that text is what the model learned on.
    Sending Decis's names instead would degrade answers with no error anywhere.
    """
    prepared = prepare_request(SystemOneRequest.model_validate(REQUEST))
    ours = [engine._question(question) for question in prepared.questions]
    theirs, _meta = upstream.to_record(upstream.SystemOneRequest.model_validate(REQUEST))

    assert [q["instr"] for q in ours] == [q["instr"] for q in theirs["questions"]]
    assert [q["options"] for q in ours] == [q["options"] for q in theirs["questions"]], (
        "the option text this engine sends differs from kev's own serving path; the model "
        "would be reading strings it was not trained on"
    )


def test_option_text_matches_upstreams_helper(engine, upstream) -> None:
    """`_option_text` is a reimplementation, so it is pinned against the original.

    `api.py` is deliberately not vendored (it also carries answer construction and the
    confidence formulas, which are `answers.py`'s job), which makes this the test that
    keeps the small copy honest.
    """
    from decis.engines.kev import _option_text

    for name, value in (("billing", "Charges, invoices"), ("billing", ""), ("no", None), ("yes", "Needs a person now")):
        assert _option_text(name, value or "") == upstream.option_text(name, value)


def test_noul_options_use_kevs_names_not_the_wire_keys(engine) -> None:
    """The `false`/`true` wire keys must not leak into the prompt."""
    question = PreparedQuestion(qid="q", type="noul", instructions="?", options=noul_options("Can wait", "Now"))
    built = engine._question(question)
    assert built["options"] == ["no: Can wait", "yes: Now"]


def test_score_options_are_the_bare_levels(engine) -> None:
    prepared = prepare_request(SystemOneRequest.model_validate(REQUEST))
    score = next(q for q in prepared.questions if q.type == "score")
    assert engine._question(score)["options"] == ["Calm", "Frustrated", "Very angry"]


# --- numerical parity ----------------------------------------------------------


def test_probabilities_match_upstreams_own_path(engine) -> None:
    """The batched path must equal the single-record path upstream publishes numbers with.

    `forward_batch` returns raw logits and `probs()` applies the softmax; this asserts
    the two agree, which is the claim that makes Decis's batching legitimate rather
    than merely convenient.
    """
    import torch

    items = _items(engine)
    ours = engine.predict(items).probabilities

    for item, ours_dist in zip(items, ours, strict=True):
        record = engine._record(item.state_text, item.question)
        enc = engine._model.encode(engine._tok, record)
        with torch.no_grad():
            expected = engine._model.probs(enc)[0]
        assert max(abs(a - b) for a, b in zip(ours_dist, expected.tolist(), strict=True)) < 1e-6


def test_one_predict_is_one_forward_pass_per_batch(engine) -> None:
    """A batch of N items must not cost N separate backbone passes.

    Counted rather than asserted-by-construction: `docs/design-review.md §2-D1`'s
    lesson is that a batching implementation nobody can prove fires is
    indistinguishable from no batching at all.
    """
    calls = {"n": 0}
    original = engine._model.forward_rows_batch

    def counting(encs):
        calls["n"] += 1
        return original(encs)

    engine._model.forward_rows_batch = counting
    try:
        engine.predict(_items(engine))
    finally:
        engine._model.forward_rows_batch = original
    assert calls["n"] == 1, "one predict() must issue one forward pass for the whole batch"


def test_answers_are_well_formed(engine) -> None:
    """One distribution per item, normalised, right length, in order."""
    items = _items(engine)
    prediction = engine.predict(items)
    assert len(prediction.probabilities) == len(items)
    for item, distribution in zip(items, prediction.probabilities, strict=True):
        assert len(distribution) == len(item.question.options)
        assert all(0.0 <= p <= 1.0 for p in distribution)
        assert abs(sum(distribution) - 1.0) < 1e-5
    assert prediction.input_tokens > 0


# --- capacity: refuse, do not truncate ----------------------------------------


def test_a_state_over_the_window_is_measured_as_too_long(engine) -> None:
    """`encode` truncates silently; `measure` must report numbers that refuse it first.

    kev caps the state at 384 tokens *and* state+question at 1024. The state cap is the
    one `validate_capacity` had no slot for (D10), which is why `max_state_tokens`
    exists.
    """
    request = SystemOneRequest.model_validate({**REQUEST, "state": "shoes " * 400})
    prepared = prepare_request(request)
    measured = engine.measure(prepared)
    assert measured.state_tokens > 384, measured.state_tokens


def test_measurement_matches_what_encode_actually_produces(engine) -> None:
    """The measurement must be the engine's real cost, not an estimate.

    If `measure` under-reported, `validate_capacity` would pass a request that `encode`
    then truncates -- the failure the whole measurement mechanism exists to prevent.
    """
    items = _items(engine)
    measured = engine.measure(prepare_request(SystemOneRequest.model_validate(REQUEST)))
    real = [len(engine._model.encode(engine._tok, engine._record(i.state_text, i.question))["ids"]) for i in items]
    assert measured.sequence_tokens == max(real)
    for item in items:
        branch = engine._model.encode(engine._tok, engine._record(item.state_text, item.question))
        assert measured.head_tokens[item.question.qid] > 0
        assert branch["state_truncated"] is False, "the measurement let a request through that encode truncated"


def test_capacity_and_encode_agree_about_a_long_question(engine) -> None:
    """The capacity check must be exactly as strict as `encode`.

    Two outcomes are correct: the request is refused, or it encodes with nothing cut.
    The one that is not correct is passing the check and then being truncated -- that is
    the silent-degradation bug the measurement mechanism exists to prevent, and `encode`
    reports it in `state_truncated`.
    """
    request = SystemOneRequest.model_validate(
        {
            **REQUEST,
            "questions": {
                "q": {
                    "type": "choice",
                    "instructions": "Pick one.",
                    "criteria": {f"option_{i}": "x" * 120 for i in range(20)},
                }
            },
        }
    )
    prepared = prepare_request(request)
    from decis.schema import validate_capacity

    try:
        validate_capacity(prepared, engine.info(), engine.measure)
    except InvalidRequestError:
        return  # refused, which is always a correct answer

    for question in prepared.questions:
        enc = engine._model.encode(engine._tok, engine._record(prepared.state_text, question))
        assert enc["state_truncated"] is False, "the capacity check passed a request that encode then truncated"
        assert len(enc["ids"]) <= MAX_BRANCH
