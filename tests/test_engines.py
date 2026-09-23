"""Engine layer: the registry, lazy imports, and the stub engine's contract.

No weights are downloaded here. `tests/test_engines_weights.py` (marked `weights`)
covers the real engines.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import VERSIONED_STUB
from decis.domain import Option, PreparedQuestion
from decis.engines import registry
from decis.engines.base import (
    DecisionEngine,
    Prediction,
    WorkItem,
    normalize,
    softmax,
    uniform,
    validate_distribution,
)
from decis.errors import InvalidRequestError
from fixture_engine import StubEngine

REPO_ROOT = Path(__file__).resolve().parent.parent


def _item(state: str = "some content", qid: str = "q", names: tuple[str, ...] = ("a", "b")) -> WorkItem:
    return WorkItem(
        request_id="req_test",
        state_text=state,
        question=PreparedQuestion(
            qid=qid,
            type="choice",
            instructions="pick",
            options=tuple(Option(name, f"option {name}") for name in names),
        ),
    )


# --- registry ----------------------------------------------------------------


def test_every_registered_engine_target_is_importable() -> None:
    """A typo in a string path would otherwise only surface when a request arrives."""
    for engine_id in registry.SPECS:
        registry.load_class(engine_id)


def test_engine_classes_subclass_the_protocol() -> None:
    for engine_id in registry.SPECS:
        assert issubclass(registry.load_class(engine_id), DecisionEngine)


def test_the_shipped_registry_registers_no_test_double() -> None:
    """Every engine the package ships is a real checkpoint; the stub is tests-only.

    Asked in a fresh interpreter on purpose: in-process, `conftest.py` has already
    registered the weight-free test double, so the answer here would be "it does"
    whichever way the shipped registry was written.
    """
    code = "from decis.engines.registry import SPECS; print(','.join(sorted(SPECS)))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kev-0.8b,laya,laya-multilingual,laya-typed-decisions"


@pytest.mark.parametrize(
    "module",
    ["decis.engines.registry", "decis.app", "decis.service", "decis.schema", "decis.render"],
)
def test_core_modules_do_not_pull_in_torch(module: str) -> None:
    """AGENTS.md §6: an image with one engine's extra must still serve `/v1/models`.

    Run in a fresh interpreter on purpose. The earlier version of this test asserted
    `"torch" not in sys.modules` in-process, which was both vacuous and order-dependent:
    vacuous in the no-weights environment, where torch is not installed at all, and
    dependent on whichever test happened to import torch first once the `laya` extra was
    present. A subprocess is the only way to ask "does *importing this* pull in torch".
    """
    code = (
        "import sys, importlib;"
        f"importlib.import_module({module!r});"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'torch', 'transformers', 'laya', 'numpy', 'safetensors'});"
        "print(','.join(heavy));"
        "sys.exit(1 if heavy else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert result.returncode == 0, (
        f"importing {module} pulled in heavyweight dependencies: {result.stdout.strip()}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("stub", "stub"),
        ("decis-stub", "stub"),
        ("stub-engine", "stub"),
        (VERSIONED_STUB, "stub"),
        ("decis/stub", "stub"),
        ("  stub  ", "stub"),
        ("STUB", None),  # case-sensitive on purpose: model names are identifiers
        ("gpt-4", None),
        ("", None),
    ],
)
def test_canonical_resolution(name: str, expected: str | None) -> None:
    assert registry.canonical(name) == expected


def test_unknown_model_error_lists_the_alternatives() -> None:
    with pytest.raises(InvalidRequestError) as caught:
        registry.resolve_or_raise("nope")
    message = caught.value.message
    assert "nope" in message
    assert "stub" in message


@pytest.mark.parametrize("name", ["jev-latest", "jev", "system-one", "default", "jev-latest-8b"])
def test_foreign_defaults_are_recognised(name: str) -> None:
    assert registry.is_foreign_default(name)


@pytest.mark.parametrize("name", ["stub", "laya", "gpt-4", "jev-not-a-thing"])
def test_non_defaults_are_not_treated_as_foreign_defaults(name: str) -> None:
    assert not registry.is_foreign_default(name)


def test_describe_does_not_load_weights() -> None:
    info = registry.describe("stub")
    assert info.id == "stub"


def test_missing_dependency_is_reported_as_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-engine image must explain itself, not crash."""
    monkeypatch.setitem(
        registry.SPECS,
        "broken",
        registry.EngineSpec(id="broken", target="decis.engines.does_not_exist:Thing", extra="broken"),
    )
    registry.load_class.cache_clear()
    try:
        with pytest.raises(InvalidRequestError) as caught:
            registry.load_class("broken")
        assert "decis[broken]" in caught.value.message
        assert caught.value.status == 422
    finally:
        registry.load_class.cache_clear()


# --- EngineInfo --------------------------------------------------------------


def test_model_id_is_versioned() -> None:
    info = StubEngine().info()
    assert info.model_id == f"decis/stub@{info.version}"
    assert "@" in info.model_id


def test_capacities_are_json_friendly() -> None:
    capacities = StubEngine().info().capacities()
    assert capacities["primitives"] == ["choice", "noul", "score"]
    assert all(isinstance(value, (int, str, list)) for value in capacities.values())


# --- the stub engine ---------------------------------------------------------


def test_stub_declares_all_three_primitives() -> None:
    assert StubEngine().info().primitives == frozenset({"noul", "choice", "score"})


def test_engine_must_be_loaded_before_use() -> None:
    engine = StubEngine()
    assert not engine.loaded
    with pytest.raises(RuntimeError, match="before load"):
        engine.predict([_item()])
    engine.load()
    assert engine.loaded
    assert engine.predict([_item()]).probabilities


def test_load_is_idempotent() -> None:
    engine = StubEngine()
    engine.load()
    engine.load()
    assert engine.loaded


def test_predictions_are_valid_distributions() -> None:
    engine = StubEngine()
    engine.load()
    items = [_item(qid=f"q{i}", names=("a", "b", "c")) for i in range(5)]
    prediction = engine.predict(items)
    assert len(prediction.probabilities) == len(items)
    for probabilities in prediction.probabilities:
        assert len(probabilities) == 3
        assert all(0.0 <= value <= 1.0 for value in probabilities)
        assert math.isclose(sum(probabilities), 1.0, abs_tol=1e-9)


def test_predictions_are_deterministic() -> None:
    """The official SDK retries POSTs; a retry must not change the answer."""
    engine = StubEngine()
    engine.load()
    items = [_item(qid="q", names=("a", "b")) for _ in range(3)]
    first = engine.predict(items).probabilities
    second = engine.predict(items).probabilities
    assert first == second


def test_the_stub_is_not_a_constant() -> None:
    """A stub that always answers the same thing would not test anything."""
    engine = StubEngine()
    engine.load()
    distributions = {tuple(engine.predict([_item(state=f"state {index}")]).probabilities[0]) for index in range(20)}
    assert len(distributions) > 15, "the stub should vary with the input"


def test_the_stub_reacts_to_the_option_text() -> None:
    """Lexical overlap makes demo answers look plausible; it is still not a model."""
    engine = StubEngine()
    engine.load()
    item = WorkItem(
        request_id="r",
        state_text="I want to cancel my subscription immediately",
        question=PreparedQuestion(
            qid="q",
            type="choice",
            instructions="",
            options=(Option("cancel", "cancel subscription"), Option("other", "zzzz")),
        ),
    )
    probabilities = engine.predict([item]).probabilities[0]
    assert probabilities[0] > probabilities[1]


def test_token_counting_is_positive_and_grows_with_input() -> None:
    engine = StubEngine()
    engine.load()
    short = engine.predict([_item(state="short")]).input_tokens
    long = engine.predict([_item(state="word " * 500)]).input_tokens
    assert long > short > 0


def test_predict_handles_an_empty_batch() -> None:
    engine = StubEngine()
    engine.load()
    prediction = engine.predict([])
    assert prediction.probabilities == []
    assert prediction.input_tokens == 0


# --- distribution helpers ----------------------------------------------------


def test_softmax_normalises_arbitrary_scores() -> None:
    assert sum(softmax([1.0, 2.0, 3.0])) == pytest.approx(1.0)
    assert softmax([1.0, 2.0, 3.0])[2] > softmax([1.0, 2.0, 3.0])[1]


def test_softmax_is_numerically_stable_for_large_scores() -> None:
    probabilities = softmax([1000.0, 1001.0])
    assert all(math.isfinite(value) for value in probabilities)
    assert sum(probabilities) == pytest.approx(1.0)


def test_uniform_and_normalize() -> None:
    assert uniform(4) == [0.25] * 4
    assert normalize([2.0, 2.0]) == [0.5, 0.5]
    # A zero total falls back to uniform rather than dividing by zero.
    assert normalize([0.0, 0.0]) == [0.5, 0.5]


def test_empty_scores_are_refused() -> None:
    with pytest.raises(ValueError, match="zero options"):
        softmax([])
    with pytest.raises(ValueError, match="zero options"):
        normalize([])


def test_validate_distribution_catches_engine_bugs() -> None:
    assert validate_distribution([0.5, 0.5], 2, context="t") == [0.5, 0.5]
    with pytest.raises(ValueError, match="2 options"):
        validate_distribution([1.0], 2, context="t")
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            validate_distribution([bad, 0.5], 2, context="t")


# --- the scheduler -----------------------------------------------------------


def test_scheduler_serialises_access_to_the_engine() -> None:
    """AGENTS.md §3-20: one engine instance, one thread inside predict at a time."""
    import threading
    import time

    from decis.scheduler import InProcessScheduler

    class Counting(StubEngine):
        def __init__(self) -> None:
            super().__init__()
            self.inside = 0
            self.peak = 0
            self.guard = threading.Lock()

        def predict(self, items):
            with self.guard:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
            time.sleep(0.01)
            try:
                return super().predict(items)
            finally:
                with self.guard:
                    self.inside -= 1

    engine = Counting()
    engine.load()
    scheduler = InProcessScheduler(engine)

    threads = [threading.Thread(target=scheduler.run, args=([_item()],)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert engine.peak == 1, f"{engine.peak} threads were inside predict at once"


def test_scheduler_reports_not_ready_before_load() -> None:
    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(StubEngine())
    assert not scheduler.ready


def test_scheduler_wraps_engine_failures() -> None:
    from decis.errors import EngineFailedError
    from decis.scheduler import InProcessScheduler

    class Exploding(StubEngine):
        def predict(self, items):
            raise ValueError("hardware on fire")

    engine = Exploding()
    engine.load()
    with pytest.raises(EngineFailedError) as caught:
        InProcessScheduler(engine).run([_item()])
    assert "hardware on fire" in caught.value.message


def test_scheduler_rejects_a_wrong_length_result() -> None:
    from decis.errors import EngineFailedError
    from decis.scheduler import InProcessScheduler

    class Truncating(StubEngine):
        def predict(self, items):
            return Prediction(probabilities=[])

    engine = Truncating()
    engine.load()
    with pytest.raises(EngineFailedError, match="answers"):
        InProcessScheduler(engine).run([_item()])
