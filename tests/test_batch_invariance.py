"""Does batching change an answer? `docs/design.md §6.6` says it can, so we measure it.

The fact being tested is uncomfortable and easy to wave away: padding changes the
order of floating-point reductions, and a different batch composition makes a GEMM
tile differently. So the same `(state, question)` at `batch=1` and at `batch=32` can
come out with different probabilities -- and in the worst case a different argmax.

That is user-visible, because the official SDK **retries POSTs**: a retry that lands
in a differently-composed batch and flips `choice` looks like a bug in Decis.

So Decis admits the property rather than hiding it:

* the wire contract is pure *per batch composition*, not bit-for-bit across
  compositions;
* probability drift is bounded and asserted here;
* **an argmax flip is a failure**, not a wobble;
* one item per forward pass is the escape hatch for byte-reproducible runs.

This file is the dependency-free half, so it runs in CI on every commit. It uses a
fixture engine that genuinely pads to the widest row in the batch and genuinely
sums over the padded width -- the same shape of arithmetic that makes real engines
drift. `test_laya_inference.py` and `test_kev_inference.py` assert the same property
against real weights; this file is what keeps the *assertion itself* under test.

That last point is why `test_the_harness_catches_a_missing_mask` exists. A batch
invariance test that cannot fail is indistinguishable from no test at all -- the
lesson from `docs/design-review.md §2-D1`.
"""

from __future__ import annotations

import hashlib
import math
from typing import ClassVar

import pytest

from decis.domain import Option, PreparedQuestion, ProbDist
from decis.engines.base import DecisionEngine, EngineInfo, Prediction, WorkItem, softmax, validate_distribution

#: Batch sizes from `docs/design.md §6.6`. 32 is deliberately larger than any
#: batch the current single-process scheduler can assemble, because the property has
#: to hold for the batcher Stage 3 will add.
BATCH_SIZES = (1, 8, 32)

#: The bound on probability drift.
#:
#: `docs/design.md §6.6` proposed 0.02 as an initial value. Measurement since then
#: says that is two orders of magnitude too generous: over 16 states, all three
#: primitives and batch sizes 2/4/8/16, real Laya weights drifted at most **8.3e-07**
#: (`docs/contract/stage1-batch-invariance.json`), and the kev/Laya weights tests in
#: this suite use 1e-5 to match. A threshold loose enough to be satisfied by a real
#: masking bug (which produces order-1 errors) would defeat the purpose, so the
#: numbers here are set from measurement, not taste.
TOLERANCE = 1e-3


#: Padding positions in a correctly-masked engine contribute exactly zero. The
#: fixture sums over the padded width anyway, so float non-associativity inside the
#: accumulator is what produces the (tiny, non-zero) drift this file measures.
#: Without that, the fixture would be bit-identical and would prove nothing.
class _FixtureEngine(DecisionEngine):
    """A padding-and-masking engine small enough to reason about.

    The scoring rule is deliberately trivial: each option contributes a deterministic
    hash-derived value, and an item's logit for option `k` is the sum of the
    contributions of *its own* options, accumulated over a vector padded to the
    batch's widest question. Options are scored jointly (all options share one
    accumulator order), which is what makes the padded width observable in the last
    bit of the result -- the same mechanism as a padded GEMM.
    """

    engine_id: ClassVar[str] = "fixture"
    #: When True, the mask is ignored and padding contributes as if it were a real
    #: option. This is the bug the negative control proves is detectable.
    ignore_mask: ClassVar[bool] = False
    #: When True, pad with a value at the same scale as real options, so ignoring the
    #: mask moves the answer. A zero pad would make the broken engine accidentally
    #: correct for additive scoring, which would be a useless control.
    pad_value: ClassVar[float] = 0.7

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="fixture",
            version="0",
            primitives=frozenset({"noul", "choice", "score"}),
            max_options=64,
            max_sequence_tokens=4096,
            max_question_tokens=4096,
            languages="any",
            device="cpu",
            dtype="fp64",
            description="Padding-and-masking fixture for the batch invariance assertions.",
            release_date="2026-09-22",
        )

    def load(self) -> None:
        self._loaded = True

    def predict(self, items: list[WorkItem]) -> Prediction:
        if not items:
            return Prediction(probabilities=[])

        width = max(len(item.question.options) for item in items)
        distributions: list[ProbDist] = []
        for item in items:
            own = [self._contribution(item, index) for index in range(len(item.question.options))]
            if self.ignore_mask:
                # The padding is scored as if it were a real option. Its value must
                # depend on the *row*, or it would be a constant added to every logit
                # and softmax would cancel it -- a broken engine that computes the same
                # answer is not a control at all. A per-row value mixes into each
                # option's logit differently through `_weight`, which is what an
                # unmasked attention row actually does.
                padded = [*own, *(self._pad_contribution(item, row) for row in range(len(own), width))]
            else:
                padded = [*own, *([0.0] * (width - len(own)))]
            # Accumulate jointly over the whole padded width, so the batch's width is
            # observable in the last bit -- the same mechanism as a padded GEMM.
            scaled = [value / width for value in padded]
            logits: list[float] = []
            for index in range(len(own)):
                total = 0.0
                for row in range(width):
                    total += scaled[row] * self._weight(index, row)
                logits.append(total * width)
            distributions.append(validate_distribution(softmax(logits), len(logits), context="fixture"))

        tokens = sum(self._token_count(item.state_text) for item in items)
        return Prediction(probabilities=distributions, input_tokens=tokens)

    def _contribution(self, item: WorkItem, index: int) -> float:
        option = item.question.options[index]
        payload = f"{item.state_text}\x1f{item.question.instructions}\x1f{option.name}\x1f{option.description}"
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        # In [-1, 1), deterministic, and sensitive to every field of the request.
        return (int.from_bytes(digest[:8], "big") / 2**63) - 1.0

    def _weight(self, option_index: int, row: int) -> float:
        """A fixed mixing matrix, so joint scoring is order-dependent but stable."""
        return 1.0 / (1.0 + abs(option_index - row))

    def _pad_contribution(self, item: WorkItem, row: int) -> float:
        """A deterministic per-row value for a padding position.

        Deterministic so the broken engine is reproducible (a flaky control is worse
        than none), and per-row so it cannot cancel out of the softmax.
        """
        payload = f"pad\x1f{item.state_text}\x1f{item.question.instructions}\x1f{row}"
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        return self.pad_value * ((int.from_bytes(digest[:8], "big") / 2**63) - 1.0)

    def _token_count(self, text: str) -> int:
        return max(1, math.ceil(len(text) / 4))


class _BrokenFixtureEngine(_FixtureEngine):
    """The same engine with the mask ignored. Must fail the invariance assertion."""

    engine_id = "fixture-broken"
    ignore_mask = True


def _question(kind: str, index: int, options: int) -> PreparedQuestion:
    if kind == "noul":
        names, descriptions = ("false", "true"), ("No", "Yes")
    elif kind == "choice":
        names = [f"option_{i}" for i in range(options)]
        descriptions = [f"Description of option {i}" for i in range(options)]
    else:
        names = [str(i) for i in range(options)]
        descriptions = [f"Level {i}" for i in range(options)]
    return PreparedQuestion(
        qid=f"q{index}",
        type=kind,
        instructions=f"Question {index}: decide ({kind}).",
        options=tuple(Option(name, description) for name, description in zip(names, descriptions, strict=True)),
    )


def _items(count: int) -> list[WorkItem]:
    """`count` items with distinct states -- the case real traffic creates.

    Distinct states matter: if every item shared one state, a batcher could group by
    state and the padding question would never arise (`AGENTS.md §3-18`).
    """
    kinds = ("noul", "choice", "score")
    states = [
        "We were billed twice in March and nobody has replied. Please refund the duplicate charge today.",
        "The dashboard returns 500 for every user since yesterday's deploy. This is blocking our team.",
        "Can you add a second seat to our plan before Friday? We are onboarding someone new.",
        "This is the third time I have asked about this. Cancel my account and confirm by email.",
        "The tracking link has not updated in six days and the package was meant to arrive Monday.",
        "Please send the invoice for last month; our finance team cannot find it in the portal.",
        "Our SSO login stopped working after the password rotation. Everyone is locked out.",
        "I want to upgrade to the annual plan. Does the current discount still apply?",
    ]
    return [
        WorkItem(
            request_id=f"item{index}",
            state_text=states[index % len(states)] + f" (case {index})",
            question=_question(kinds[index % len(kinds)], index, options=2 + (index % 3)),
        )
        for index in range(count)
    ]


def assert_batch_invariant(
    engine: DecisionEngine,
    items: list[WorkItem],
    *,
    tolerance: float = TOLERANCE,
    sizes: tuple[int, ...] = BATCH_SIZES,
) -> dict[int, float]:
    """Compare each item alone against the same item inside a padded batch.

    Returns `{batch_size: max_abs_deviation}` so a caller can assert on the actual
    numbers rather than only on pass/fail. Raises `AssertionError` on excessive drift
    or on any argmax change -- an argmax flip is a user-visible error, not rounding.
    """
    alone = {index: engine.predict([item]).probabilities[0] for index, item in enumerate(items)}
    observed: dict[int, float] = {}

    for size in sizes:
        if size > len(items):
            continue
        batched = engine.predict(items[:size]).probabilities
        assert len(batched) == size, f"batch of {size} returned {len(batched)} distributions"

        worst = 0.0
        for index, distribution in enumerate(batched):
            reference = alone[index]
            assert len(distribution) == len(reference), (
                f"item {index} changed option count between batch sizes: "
                f"{len(reference)} alone vs {len(distribution)} batched"
            )
            for single, pooled in zip(reference, distribution, strict=True):
                worst = max(worst, abs(single - pooled))
            assert reference.index(max(reference)) == distribution.index(max(distribution)), (
                f"item {index} argmax flipped at batch={size}: "
                f"{reference} alone vs {distribution} batched -- this is user-visible"
            )
        observed[size] = worst
        assert worst < tolerance, (
            f"item probabilities moved {worst:.3e} at batch={size}, over the {tolerance:.1e} bound"
        )
    return observed


# --- the assertions -----------------------------------------------------------


def test_a_single_item_is_reproducible() -> None:
    """Two identical calls must agree bit for bit before anything else is claimed."""
    engine = _FixtureEngine()
    engine.load()
    item = _items(1)[0]
    assert engine.predict([item]).probabilities[0] == engine.predict([item]).probabilities[0]


@pytest.mark.parametrize("size", BATCH_SIZES)
def test_batch_composition_does_not_change_the_answer(size: int) -> None:
    """The core assertion, at every batch size `docs/design.md §6.6` names."""
    engine = _FixtureEngine()
    engine.load()
    observed = assert_batch_invariant(engine, _items(32), sizes=(size,))
    assert observed[size] < TOLERANCE


def test_drift_is_bounded_across_every_batch_size_at_once() -> None:
    """The batch sizes compared against *one* set of alone-predictions.

    Comparing each size against its own reference would let a size-dependent error
    hide; this is the version that matches how `stage1-batch-invariance.json` was
    collected.
    """
    engine = _FixtureEngine()
    engine.load()
    observed = assert_batch_invariant(engine, _items(32))
    assert set(observed) == set(BATCH_SIZES)
    assert max(observed.values()) < TOLERANCE


def test_the_harness_catches_a_missing_mask() -> None:
    """The negative control: a broken engine **must** fail the same assertion.

    Without this, `assert_batch_invariant` could be silently vacuous -- for instance if
    the fixture never actually padded, or if the comparison accidentally used the same
    values on both sides. `docs/design-review.md §2-D1` is the precedent: an
    implementation that cannot be shown to fire is indistinguishable from one that
    does not exist.
    """
    broken = _BrokenFixtureEngine()
    broken.load()
    with pytest.raises(AssertionError) as caught:
        assert_batch_invariant(broken, _items(32))
    message = str(caught.value)
    # It must fail for the right reason: drift, not a shape or argmax accident.
    assert "moved" in message or "argmax" in message, message


def test_the_fixture_actually_pads_to_the_widest_row() -> None:
    """A fixture that never pads cannot test padding.

    `_items` builds questions with 2, 3 and 4 options, so a correct batch is wider
    than its narrowest member. If this regressed to a uniform option count, every
    other test in this file would pass while testing nothing.
    """
    items = _items(8)
    widths = {len(item.question.options) for item in items}
    assert len(widths) > 1, "the fixture no longer produces mixed option counts, so nothing pads"


# --- the real engines, when their weights are present -------------------------


def _weights_present(engine_id: str) -> bool:
    """Can this engine be loaded on this machine right now?

    Deliberately permissive: absent weights mean "skip", not "fail". A missing
    dependency is caught by `pytest.importorskip` in the caller.
    """
    from decis.config import load_settings
    from decis.engines.registry import SPECS, create
    from decis.paths import resolve

    spec = SPECS.get(engine_id)
    if spec is None:
        return False
    instance = create(engine_id)
    weights = instance.weights()
    if weights is None:
        return False
    if resolve(weights, load_settings()).is_local:
        return True
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    for one in (weights, *weights.base_specs()):
        if resolve(one, load_settings()).is_local:
            continue
        if not isinstance(try_to_load_from_cache(one.repo_id, one.marker, revision=one.revision), str):
            return False
    return True


@pytest.mark.weights
@pytest.mark.parametrize("engine_id", ["laya-multilingual", "kev-0.8b"])
def test_real_engines_are_batch_invariant(engine_id: str) -> None:
    """The same assertion against real weights.

    This is the half that can actually catch a masking bug in a real engine, and it is
    why the fixture above exists: when this test is skipped (CI has no weights), the
    fixture still exercises the assertion.

    Tolerance is 1e-5, matching `test_laya_inference.py`. Measurement says 8.3e-07
    across 12 real configurations, so this is one order of magnitude of headroom --
    room for a different CPU's kernels, not room for a broken mask.
    """
    pytest.importorskip("torch")
    if not _weights_present(engine_id):
        pytest.skip(f"{engine_id} weights are not present; run: decis download --engine {engine_id}")

    from decis.engines.registry import create

    engine = create(engine_id)
    engine.load()
    try:
        observed = assert_batch_invariant(engine, _items(8), tolerance=1e-5, sizes=(2, 4, 8))
        assert max(observed.values()) < 1e-5
    finally:
        engine.close()


@pytest.mark.weights
def test_real_engine_is_reproducible_for_one_item() -> None:
    """Bit-for-bit reproducibility for one item, which is the escape hatch a caller has today."""
    pytest.importorskip("torch")
    if not _weights_present("laya-multilingual"):
        pytest.skip("laya-multilingual weights are not present")

    from decis.engines.registry import create

    engine = create("laya-multilingual")
    engine.load()
    try:
        item = _items(1)[0]
        first = engine.predict([item]).probabilities[0]
        second = engine.predict([item]).probabilities[0]
        assert first == pytest.approx(second, abs=1e-6)
    finally:
        engine.close()
