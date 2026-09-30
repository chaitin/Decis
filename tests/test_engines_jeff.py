"""The Jeff engines' compatibility layer and capacity arithmetic, without a checkpoint.

What this file is for, and why it does not load a model:

* **the raw-value layer** -- Jeff's prompt is `json.dumps` of the caller's own JSON, while
  `render.py` flattens that JSON into readable lines for Laya and kev. Feeding the flattened
  form to a Jeff checkpoint is a *silent* quality regression: nothing raises, the answer is
  simply worse than the checkpoint's benchmark. So the rows this engine builds are asserted
  here, including the explicit `None` that the flattened path would have dropped.
* **the arithmetic** -- `measure()`'s two counts per question and `predict()`'s token
  accounting, against a stub that records what it was asked. Both are places where a wrong
  number is invisible until a request is quietly truncated, and `AGENTS.md §5-4` forbids
  deriving either from upstream's truncated output.

The checkpoint itself is exercised by `tests/test_jeff_inference.py` (`-m weights`). This
suite never imports `_jeff_vendor`: that package imports torch *and* PIL at module scope, and
the fast suite has to run on a machine with no engine extras at all (`AGENTS.md §7`). The one
prompt-byte-parity assertion that genuinely needs upstream's `decision_messages` lives in the
weights suite for the same reason.

`load()` is still covered here, through a stub vendored module and a temporary checkpoint
directory, because the parts of it that can be wrong without weights are the parts that wire
`Settings` into the loader: the device, the thread count and the option ceiling re-read from
`decision_config.json`.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from decis.config import Settings
from decis.engines import jeff as jeff_module
from decis.engines.base import WorkItem
from decis.engines.jeff import (
    CHECKPOINT_DATE,
    MAX_SEQUENCE,
    REVISION,
    WEIGHTS,
    JeffEngine,
    JeffGemmaEngine,
    JeffQwenEngine,
)
from decis.engines.registry import SPECS, canonical, create
from decis.render import prepare_request
from decis.schema import SystemOneRequest

QWEN = "jeff-qwen3.5-0.8b"
GEMMA = "jeff-gemma4-e2b"


# --- a stub that stands in for the vendored loader ----------------------------


class FakeReadout:
    """Enough of `torch.nn.Linear` for `info().dtype`, without importing torch.

    `dtype` is spelled the way torch spells it, so the engine's `removeprefix("torch.")`
    is genuinely exercised instead of being bypassed by a pre-stripped string.
    """

    def __init__(self, dtype: str = "torch.float32") -> None:
        self.dtype = dtype

    def parameters(self) -> Any:
        return iter([self])


class FakeModel:
    """A vendored loader that records its arguments and answers with fixed distributions.

    The two methods mirror the vendored API exactly as `jeff.py` calls it:
    `prepare(rows, max_length=...) -> .input_tokens` and `predict(rows, batch_size=...) ->
    list of distributions. `token_cost` is a function of the row so that a test can make the
    state's contribution differ from the question's.
    """

    def __init__(self, *, vocab: int = 255, dtype: str = "torch.float32") -> None:
        self.codes = tuple(f"c{index}" for index in range(vocab))
        self.readout = FakeReadout(dtype)
        self.prepared: list[dict[str, Any]] = []
        self.predicted: list[list[dict[str, Any]]] = []
        self.max_lengths: list[int] = []
        self.batch_sizes: list[int] = []

    def prepare(self, rows: list[dict[str, Any]], max_length: int = 8192) -> Any:
        self.prepared.extend(rows)
        self.max_lengths.append(max_length)
        return SimpleNamespace(input_tokens=sum(self._cost(row) for row in rows))

    def predict(self, rows: list[dict[str, Any]], batch_size: int = 8) -> list[list[float]]:
        self.predicted.append(list(rows))
        self.batch_sizes.append(batch_size)
        return [[0.75, 0.25][: len(row["question"]["criteria"])] for row in rows]

    @staticmethod
    def _cost(row: dict[str, Any]) -> int:
        """The row's token cost, with the state's contribution clearly separable.

        `json.dumps` here is *not* a re-implementation of the prompt -- the stub has no
        tokenizer. It only has to be a deterministic function of the row so that
        `measure()`'s arithmetic can be checked.
        """
        return 100 + len(json.dumps(row["state"])) + len(json.dumps(row["question"]))


def _loaded(engine: JeffEngine, model: FakeModel, **settings: Any) -> JeffEngine:
    """Put `model` in place of a loaded vendored model, as `load()` would have."""
    engine._model = model
    engine._loaded = True
    engine._device = settings.pop("device", "cpu")
    engine._max_options = settings.pop("max_options", engine.default_max_options)
    return engine


# --- registry and declaration -------------------------------------------------


def test_both_checkpoints_are_registered_under_their_own_ids() -> None:
    assert QWEN in SPECS and GEMMA in SPECS
    for engine_id in (QWEN, GEMMA):
        assert SPECS[engine_id].extra == "jeff", "both checkpoints share one dependency set"
        assert SPECS[engine_id].target == "decis.engines.jeff:JeffQwenEngine" or SPECS[engine_id].target == (
            "decis.engines.jeff:JeffGemmaEngine"
        ), "the registry stores a string path, so nothing heavy is imported to list it"


def test_the_aliases_resolve_and_do_not_collide() -> None:
    assert canonical("jeff") == QWEN
    assert canonical("jeff-qwen") == QWEN
    assert canonical("jeff-qwen3.5") == QWEN
    assert canonical("jeff-gemma") == GEMMA
    assert canonical("jeff-gemma4") == GEMMA
    # `jeff` deliberately means the smaller checkpoint, so no alias may name both.
    for name in ("jeff-qwen", "jeff-gemma", "jeff-qwen3.5", "jeff-gemma4"):
        assert canonical(name) in {QWEN, GEMMA}


def test_creating_each_engine_needs_nothing_but_this_module() -> None:
    """`AGENTS.md §6`: the registry stores string paths, so `decis models` works without extras.

    The order-independent version of "this import is lazy" is in
    `tests/test_engines.py::test_core_modules_do_not_pull_in_torch`, which runs in a fresh
    interpreter. What is checked here is the other half: constructing an engine -- all the
    registry does -- touches no vendored code, so a `/v1/models` call cannot drag in torch.
    """
    engine = create(QWEN)
    assert isinstance(engine, JeffQwenEngine)
    assert isinstance(create(GEMMA), JeffGemmaEngine)
    assert "decis.engines._jeff_vendor" not in sys.modules


def test_the_declared_capacities_describe_the_pinned_checkpoints() -> None:
    for engine_id, expected_options in ((QWEN, 254), (GEMMA, 26)):
        info = create(engine_id).info()
        assert info.id == engine_id
        assert info.version == REVISION
        assert info.primitives == frozenset({"noul", "choice", "score"})
        # The *trained* ceiling, not the 255 rows the readout always has.
        assert info.max_options == expected_options
        assert info.max_options < 255, "255 would mean 'the head can express it', not 'it was trained on it'"
        assert info.max_sequence_tokens == MAX_SEQUENCE == 8192
        # The question and the state share one sequence and upstream caps only their sum,
        # so the loosest sound bound for a question alone is the whole window.
        assert info.max_question_tokens == MAX_SEQUENCE
        # No separate state cap: the sequence limit is the only one upstream has.
        assert info.max_state_tokens == 0
        assert info.release_date == CHECKPOINT_DATE
        # Not "cpu": reporting a device would mean importing torch to find one.
        assert info.device == "unloaded"
        assert info.dtype == "unloaded"


def test_the_aliases_are_advertised_on_the_engine_that_owns_them() -> None:
    assert create(QWEN).info().aliases == ("jeff", "jeff-qwen", "jeff-qwen3.5")
    assert create(GEMMA).info().aliases == ("jeff-gemma", "jeff-gemma4")


def test_an_unknown_checkpoint_id_is_refused_rather_than_guessed() -> None:
    class Nameless(JeffEngine):
        engine_id = "jeff-something-else"
        loader = "DecisionModel"

    with pytest.raises(ValueError, match="no weights declared"):
        Nameless()


def test_a_subclass_must_name_its_loader() -> None:
    class Loaderless(JeffEngine):
        engine_id = QWEN

    with pytest.raises(ValueError, match="declares no loader"):
        Loaderless()


# --- weights ------------------------------------------------------------------


def test_each_checkpoint_declares_a_pinned_revision_and_a_size() -> None:
    """A revision has to be immutable and `expected_bytes` has to be measured, not guessed."""
    for engine_id, spec in WEIGHTS.items():
        assert spec.engine_id == engine_id, "the key and the spec must not disagree"
        assert len(spec.revision) == 40 and all(character in "0123456789abcdef" for character in spec.revision)
        assert spec.repo_id and spec.repo_id.startswith("mstrasser/Jeff-")
        assert spec.marker == "decision_config.json", "the marker is what `resolve` looks for"
        assert spec.expected_bytes and spec.expected_bytes > 0
        assert spec.license_name == "Apache-2.0"
        assert spec.license_url and spec.license_url.startswith("https://")
        # Jeff checkpoints are full fine-tunes: there is no adapter and no second repo, so
        # `bases` must stay empty or `decis download` would fetch a base nobody reads.
        assert not spec.bases


def test_the_two_checkpoints_are_an_order_of_magnitude_apart() -> None:
    """Guards the numbers `docs/engines.md` publishes, in the direction that matters.

    A copy/paste of the Qwen size into the Gemma entry would make `decis doctor` claim the
    8.65 GiB checkpoint fits in 1.61 GiB and `decis download` would start it happily.
    """
    assert WEIGHTS[QWEN].expected_bytes == 1_726_570_651
    assert WEIGHTS[GEMMA].expected_bytes == 9_290_236_873
    assert WEIGHTS[GEMMA].expected_bytes > 4 * WEIGHTS[QWEN].expected_bytes


def test_the_checkpoint_file_list_excludes_the_readme_and_the_videos() -> None:
    """Both repositories ship demo videos and assets an inference server must not pull."""
    patterns = WEIGHTS[QWEN].checkpoint_files
    assert patterns == WEIGHTS[GEMMA].checkpoint_files, "both checkpoints need the same kinds of file"
    # `*.json` is what carries `preprocessor`-style configs; `*.safetensors` is both the
    # backbone and `readout.safetensors`; `*.jinja` is the chat template.
    for needed in ("*.json", "*.safetensors", "tokenizer*", "*.jinja", "LICENSE", "NOTICE"):
        assert needed in patterns
    for excluded in ("*.mp4", "assets/*", "videos/*", "README.md"):
        assert excluded not in patterns


def test_the_required_modules_include_pillow_and_torchvision() -> None:
    """Both are hard requirements, and neither is obvious from the checkpoint.

    `_jeff_vendor/types.py` imports PIL at module scope. `torchvision` is stranger: the
    Qwen checkpoint's `processor_config.json` names a `Qwen3VLVideoProcessor` (inherited
    from the Qwen3-VL base), `AutoProcessor.from_pretrained` builds it eagerly, and that
    class raises ImportError while being imported -- so a text-only load fails without it.
    Measured, not guessed: the extra shipped without torchvision until the weights suite
    ran (2026-09-30), and this assertion is what keeps the two lists in step.
    """
    for module in ("PIL", "torch", "transformers", "torchvision", "safetensors"):
        assert module in WEIGHTS[QWEN].requires, f"{module} must be a declared requirement"
    assert WEIGHTS[GEMMA].requires == WEIGHTS[QWEN].requires, "one extra serves both checkpoints"


#: The PyPI distribution that provides each importable module in `requires`. A fact about
#: PyPI (`PIL` is `pillow`), not a copy of this repository's configuration -- the
#: configuration itself is read out of `pyproject.toml` below.
_DISTRIBUTION = {
    "PIL": "pillow",
    "torch": "torch",
    "transformers": "transformers",
    "torchvision": "torchvision",
    "safetensors": "safetensors",
}


def test_the_extra_declares_every_required_module() -> None:
    """`requires` (what the engine needs) and the extra (what we install) must agree.

    Two lists of the same thing kept in two files is exactly the drift `AGENTS.md §2`
    is about, and here the drift is invisible until a real load: `requires` only feeds
    `decis models` / `decis doctor` / the `EngineUnavailableError` message, so a module
    that is missing from the *extra* is never noticed by the fast suite. Measured: the
    `jeff` extra shipped without `torchvision` and only the weights suite caught it
    (2026-09-30). This assertion is the cheap guard that runs in CI.
    """
    pyproject = tomllib.loads((Path(jeff_module.__file__).parents[3] / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.split(r"[<>=!\[;]", dependency, maxsplit=1)[0].strip().lower()
        for dependency in pyproject["project"]["optional-dependencies"]["jeff"]
    }
    for module in WEIGHTS[QWEN].requires:
        assert _DISTRIBUTION[module] in declared, (
            f"{module} is in WeightSpec.requires but the `jeff` extra never installs "
            f"{_DISTRIBUTION[module]}; `uv sync --extra jeff` would produce an engine "
            f"that cannot load"
        )


# --- the compatibility layer --------------------------------------------------


def noul_item(engine: JeffEngine, state: Any, *, instructions: str = "Is it urgent?") -> WorkItem:
    """A `WorkItem` built the way `service.py` builds one, through the real renderer."""
    wire = {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"false": "It is not.", "true": "It is."},
    }
    request = SystemOneRequest(model=engine.info().id, state=state, questions={"q1": wire})
    prepared = prepare_request(request)
    return WorkItem(
        request_id="r1",
        state_text=prepared.state_text,
        question=prepared.questions[0],
        raw_state=prepared.raw_state,
    )


def test_a_dict_state_reaches_the_prompt_as_json_not_as_flattened_text() -> None:
    """The whole point of the layer, and the failure it prevents is silent.

    `render.py` turns `{"screen": "cart"}` into a `screen: cart` line for Laya and kev.
    Jeff was trained on `describe()`, which is `json.dumps` for a non-string, so the engine
    has to hand upstream the object itself.
    """
    engine = _loaded(create(QWEN), FakeModel())
    state = {"screen": "cart", "items": 3, "nested": {"a": [1, 2]}}
    item = noul_item(engine, state)

    # The flattened text really is different, so this test would fail an engine that used it.
    assert "screen: cart" in item.state_text
    assert json.dumps(state, ensure_ascii=False) not in item.state_text

    row = engine._row(item)
    # Pydantic owns the parsed request, so what arrives here is its validated copy rather
    # than the caller's original object -- equal, and still a container rather than a string.
    assert row["state"] == state, "the caller's own values, not the flattened text"
    assert isinstance(row["state"], dict), "a dict must not have been flattened into a line"


def test_the_question_keeps_its_nested_values_and_an_explicit_none() -> None:
    """`describe()` distinguishes `None` from "key absent"; flattening does not.

    `render.render_value` drops a `None` because a wire answer has nowhere to put it. A Jeff
    prompt has: `json.dumps({"a": None})` is `{"a": null}`, and the checkpoint was trained on
    prompts that contain it.
    """
    wire = {
        "type": "choice",
        "instructions": "Which one?",
        "criteria": {"a": None, "b": {"deeply": {"nested": [1, None, "x"]}}},
    }
    request = SystemOneRequest(model=QWEN, state="s", questions={"q1": wire})
    prepared = prepare_request(request)
    engine = _loaded(create(QWEN), FakeModel())

    raw = engine._row(WorkItem("r1", prepared.state_text, prepared.questions[0], prepared.raw_state))["question"]
    assert raw["criteria"]["a"] is None, "an explicit null must survive"
    assert raw["criteria"]["b"] == {"deeply": {"nested": [1, None, "x"]}}
    # And the flattened form genuinely lost it, which is why `raw` exists at all.
    assert "a:" not in prepared.questions[0].text()


def test_a_string_state_is_still_a_string() -> None:
    """`describe` returns a string unchanged, so no quoting is added anywhere."""
    engine = _loaded(create(QWEN), FakeModel())
    item = noul_item(engine, "the user is angry")
    assert engine._row(item)["state"] == "the user is angry"


def test_a_synthetic_item_falls_back_to_the_flattened_text() -> None:
    """A warmup and a hand-built test item have no raw state; the text is all there is."""
    engine = _loaded(create(QWEN), FakeModel())
    item = WorkItem("r1", "flat state", noul_item(engine, "ignored").question)
    assert item.raw_state is None
    assert engine._row(item)["state"] == "flat state"


def test_a_question_without_raw_values_is_a_loud_failure() -> None:
    """Reachable only from a `PreparedQuestion` built by hand, and it must not be tolerated.

    `render.prepare_question` fills `raw` for all three primitives, so going through the
    renderer is always safe. What is not safe is constructing a `PreparedQuestion` directly --
    which is exactly what kev's warmup does, and what a test double does -- because then there
    is no caller JSON and the only thing left to send is the flattened text.
    """
    from decis.domain import PreparedQuestion
    from decis.render import noul_options

    engine = _loaded(create(QWEN), FakeModel())
    question = PreparedQuestion(qid="q1", type="noul", instructions="?", options=noul_options(), raw=None)
    assert question.raw is None
    with pytest.raises(ValueError, match="carries no raw values"):
        engine._row(WorkItem("r1", "s", question))


def test_both_engines_share_one_row_builder() -> None:
    """The two checkpoints differ in loader and option ceiling, not in how a row is built."""
    for engine_id in (QWEN, GEMMA):
        engine = _loaded(create(engine_id), FakeModel())
        row = engine._row(noul_item(engine, {"k": "v"}))
        assert set(row) == {"state", "question"}
        assert row["question"]["type"] == "noul"


# --- capacity -----------------------------------------------------------------


def test_measure_charges_the_state_for_the_room_it_actually_takes() -> None:
    """Two counts per question, because tokenisation is not additive at the boundary.

    `state_tokens` is `full - head` -- the state's real contribution -- and not the length of
    the state text. A guess here is what lets an over-long request through.
    """
    model = FakeModel()
    engine = _loaded(create(QWEN), model, max_options=254)
    wire = {"type": "noul", "instructions": "Is it urgent?", "criteria": {"false": "no", "true": "yes"}}
    request = prepare_request(SystemOneRequest(model=QWEN, state={"big": "x" * 40}, questions={"q1": wire}))

    measured = engine.measure(request)
    full = 100 + len(json.dumps(request.raw_state)) + len(json.dumps(request.questions[0].raw))
    head = 100 + len(json.dumps("")) + len(json.dumps(request.questions[0].raw))
    assert measured.sequence_tokens == full
    assert measured.head_tokens == {"q1": head}
    assert measured.state_tokens == full - head > 0


def test_measure_reports_a_sequence_past_the_limit_rather_than_raising() -> None:
    """The capacity check turns an oversized number into a 422; `measure` must not raise.

    Upstream's `prepare` refuses past `max_length`, and if `measure` let it use its own
    default limit the engine would surface upstream's generic message instead of one naming
    the state and the limit.
    """
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    request = prepare_request(
        SystemOneRequest(
            model=QWEN,
            state="s",
            questions={"q1": {"type": "noul", "instructions": "?", "criteria": {"false": "n", "true": "y"}}},
        )
    )
    engine.measure(request)
    assert model.max_lengths, "measure must go through the real prepare"
    assert all(limit > MAX_SEQUENCE for limit in model.max_lengths), (
        "measure passes a generous limit on purpose: it is the measurement, not the check"
    )


def test_measure_takes_the_worst_question_in_the_request() -> None:
    """`validate_capacity` compares per question, so both maxima must be over all of them."""
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    request = prepare_request(
        SystemOneRequest(
            model=QWEN,
            state={"pad": "y" * 20},
            questions={
                "short": {"type": "noul", "instructions": "a", "criteria": {"false": "n", "true": "y"}},
                "long": {"type": "noul", "instructions": "a" * 50, "criteria": {"false": "n", "true": "y"}},
            },
        )
    )
    measured = engine.measure(request)
    assert measured.sequence_tokens == max(measured.head_tokens.values()) + measured.state_tokens
    assert measured.head_tokens["long"] > measured.head_tokens["short"]


def test_measure_skips_a_question_with_too_many_options() -> None:
    """Building its prompt would raise upstream's generic error instead of a 422.

    `validate_capacity` rejects an over-wide question *after* measuring, and it needs a
    measurement that does not blow up first.
    """
    model = FakeModel()
    engine = _loaded(create(QWEN), model, max_options=2)
    request = prepare_request(
        SystemOneRequest(
            model=QWEN,
            state="s",
            questions={
                "wide": {
                    "type": "choice",
                    "instructions": "?",
                    "criteria": {f"option-{index}": f"value-{index}" for index in range(5)},
                }
            },
        )
    )
    measured = engine.measure(request)
    assert measured.head_tokens == {}, "an over-wide question contributes nothing"
    assert measured.sequence_tokens == 0
    assert model.prepared == [], "its prompt must not be built at all"


def test_measure_without_a_model_falls_back_to_the_engine_default() -> None:
    """Not a usable answer, but it must not crash: `registry.status` measures nothing."""
    engine = create(QWEN)
    request = prepare_request(
        SystemOneRequest(
            model=QWEN,
            state="s",
            questions={"q1": {"type": "noul", "instructions": "?", "criteria": {"false": "n", "true": "y"}}},
        )
    )
    measured = engine.measure(request)
    assert measured.sequence_tokens > 0
    assert set(measured.head_tokens) == {"q1"}


# --- inference ----------------------------------------------------------------


def test_predict_returns_one_distribution_per_item_in_order() -> None:
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    items = [noul_item(engine, {"index": index}, instructions=f"q{index}") for index in range(3)]

    prediction = engine.predict(items)
    assert len(prediction.probabilities) == 3
    assert all(len(distribution) == 2 for distribution in prediction.probabilities)
    assert prediction.input_tokens > 0
    # Order is preserved: the contract's answers are keyed by question id, so a reordering
    # here would silently answer one question with another's distribution.
    assert [row["state"] for row in model.predicted[0]] == [{"index": 0}, {"index": 1}, {"index": 2}]


def test_predict_hands_every_item_to_upstream_in_one_call() -> None:
    """Upstream does the chunking, so `predict` must not pre-slice the list itself.

    It takes `batch_size` and creates one batch. Slicing here as well would halve the
    effective batch without changing a single answer -- invisible except in throughput.
    """
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    items = [noul_item(engine, {"index": index}) for index in range(20)]

    engine.predict(items)
    assert len(model.predicted) == 1, "one call to upstream, whatever the batch size"
    assert len(model.predicted[0]) == 20


def test_input_tokens_count_every_chunk_upstream_would_have_run() -> None:
    """`input_tokens` is a contract field upstream's `predict` does not return.

    The second pass has to chunk at exactly upstream's batch size, or the reported number
    describes a different set of forward passes than the one that produced the answers.
    """
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    items = [noul_item(engine, {"index": index}) for index in range(20)]

    prediction = engine.predict(items)
    expected = sum(model._cost(row) for row in model.predicted[0])
    assert prediction.input_tokens == expected
    # 20 items at batch_size 8 is 8 + 8 + 4: three chunks, so three `prepare` calls, and the
    # first pass (also chunked by upstream) is not double counted.
    assert model.batch_sizes == [8]


def test_predict_refuses_to_run_unloaded() -> None:
    from decis.errors import EngineUnavailableError

    engine = create(QWEN)
    with pytest.raises(EngineUnavailableError, match="not loaded"):
        engine.predict([])


def test_a_distribution_that_does_not_match_the_options_is_rejected() -> None:
    """An engine bug, not a client error: it must not reach the wire as a strange answer."""
    model = FakeModel()
    engine = _loaded(create(QWEN), model)
    items = [noul_item(engine, "s")]
    model.predict = lambda rows, batch_size=8: [[0.5, 0.25, 0.25]]  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="probabilities for 2 options"):
        engine.predict(items)


# --- load(): wiring Settings into the vendored loader -------------------------


@pytest.fixture
def checkpoint(tmp_path: Path) -> Path:
    """A directory that looks like a downloaded Jeff checkpoint.

    Only `decision_config.json` is read by this engine, but the rest of the files are
    written too: `paths.checkpoint_root` looks for the marker, and a test that got the
    layout wrong would then be testing the wrong branch.
    """
    directory = tmp_path / QWEN
    directory.mkdir()
    (directory / "decision_config.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "base_model": "Qwen/Qwen3.5-0.8B",
                "revision": "2fc06364715b967f1860aea9cf38778875588b17",
                "max_options": 254,
                "temperature": 1.1289476733993191,
                "prompt_layout": "state-first",
            }
        ),
        encoding="utf-8",
    )
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "readout.safetensors").write_bytes(b"")
    return directory


@pytest.fixture
def stub_vendor(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `_jeff_vendor` and `torch`, so `load()` needs no engine extra at all.

    `load()` reaches for exactly two things outside this module: the vendored loader and
    `torch.get_num_threads()`. Stubbing both is what lets the whole `load()` path run on a
    machine with no extras -- and it makes the thread count a test's own value rather than
    whatever the host reports, which is otherwise an untestable branch.
    """
    recorded: dict[str, Any] = {"calls": [], "threads": []}
    model = FakeModel()
    recorded["model"] = model

    def loader(**kwargs: Any) -> FakeModel:
        recorded["calls"].append(kwargs)
        recorded["model"] = model
        return model

    class _Torch:
        #: Not the host's count: the point of passing it through is that Deci's value wins.
        default_threads = 4

        @staticmethod
        def get_num_threads() -> int:
            return _Torch.default_threads

        @staticmethod
        def set_num_threads(count: int) -> None:
            recorded["threads"].append(count)

    stub = SimpleNamespace(DecisionModel=loader, GenericDecoderDecisionModel=loader)
    monkeypatch.setattr(jeff_module, "_jeff_vendor", stub, raising=False)
    monkeypatch.setitem(sys.modules, "decis.engines._jeff_vendor", stub)
    monkeypatch.setitem(sys.modules, "torch", _Torch())
    recorded["stub"] = stub
    return recorded


def test_load_reads_the_checkpoint_ceiling_instead_of_the_subclass_default(
    checkpoint: Path, stub_vendor: dict[str, Any]
) -> None:
    """`decision_config.json` is the checkpoint's own claim; the class constant is a guess.

    A fine-tune that trained on fewer options must narrow the reported ceiling, or a request
    with more of them passes capacity and upstream raises a generic error.
    """
    (checkpoint / "decision_config.json").write_text(json.dumps({"max_options": 12}), encoding="utf-8")
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))

    assert engine.info().max_options == 12
    assert engine.loaded


def test_load_clamps_an_edited_config_to_the_answer_vocabulary(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    """A config promising 900 options would advertise a ceiling the readout cannot express.

    `FakeModel.codes` has 255 entries, which is what the readout always has.
    """
    (checkpoint / "decision_config.json").write_text(json.dumps({"max_options": 900}), encoding="utf-8")
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    assert engine.info().max_options == 255


def test_load_falls_back_to_the_vocabulary_when_the_field_is_absent(
    checkpoint: Path, stub_vendor: dict[str, Any]
) -> None:
    (checkpoint / "decision_config.json").write_text(json.dumps({"format_version": 1}), encoding="utf-8")
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    assert engine.info().max_options == 255


def test_read_max_options_never_returns_zero(tmp_path: Path) -> None:
    """`max_options: 0` would reject every request; the field is clamped to at least one."""
    (tmp_path / "decision_config.json").write_text(json.dumps({"max_options": 0}), encoding="utf-8")
    assert JeffEngine._read_max_options(tmp_path, 255) == 1


def test_load_passes_the_resolved_device_explicitly(
    checkpoint: Path, stub_vendor: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AGENTS.md §9`: the engine must not let upstream pick a device.

    Upstream's own fallback is `cuda if available else cpu`, which ignores `DECIS_DEVICE` and
    would hand an Apple-silicon host the CPU path (measured 15x slower for kev).
    """
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent, device="cpu"))

    assert stub_vendor["calls"][0]["device"] == "cpu"
    assert engine.info().device == "cpu"
    assert stub_vendor["calls"][0]["checkpoint"] == str(checkpoint)


def test_load_honours_the_resolved_thread_count(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    """Upstream calls `torch.set_num_threads(cpu_threads)`, so its own default of 8 would
    override `DECIS_TORCH_THREADS` -- the variable that decides how many cores a CPU
    deployment uses. With no opinion from Decis, torch's current value is passed through, so
    the engine leaves the process exactly as it found it."""
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent, torch_threads=3))
    assert stub_vendor["calls"][0]["cpu_threads"] == 3

    passthrough = create(QWEN)
    passthrough.load(Settings(model_dir=checkpoint.parent))
    assert stub_vendor["calls"][1]["cpu_threads"] == 4, "torch's own value, untouched"


def test_load_ignores_decis_dtype_because_the_loader_decides_its_own(
    checkpoint: Path, stub_vendor: dict[str, Any]
) -> None:
    """Neither vendored class takes a dtype; both choose bf16 on an accelerator, fp32 on CPU.

    Documented rather than merely true, and worth a test: `DECIS_DTYPE` forcing fp16 here
    would be a promise nothing keeps. The reported dtype comes from the loaded head, with
    torch's own `torch.` prefix stripped for the wire.
    """
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent, dtype="fp16"))
    assert "dtype" not in stub_vendor["calls"][0]
    assert engine.info().dtype == "float32"


def test_load_reports_the_head_dtype_it_actually_loaded(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    stub_vendor["model"].readout = FakeReadout("torch.bfloat16")
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    assert engine.info().dtype == "bfloat16"


def test_load_warms_up_through_the_same_row_builder_a_request_uses(
    checkpoint: Path, stub_vendor: dict[str, Any]
) -> None:
    """A hand-built warmup row would skip `raw` and only fail on the first caller."""
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))

    rows = stub_vendor["model"].predicted[0]
    assert len(rows) == 1
    # The warmup goes through `render.prepare_question`, so its question really is a caller's
    # payload rather than a hand-built `PreparedQuestion` with no `raw`.
    assert rows[0]["state"] == "warmup"
    assert rows[0]["question"] == {
        "type": "noul",
        "instructions": "Does the text contain the word warmup?",
        "criteria": {"false": "It does not.", "true": "It does."},
    }


def test_load_takes_the_prompt_layout_from_the_checkpoint(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    """Only some checkpoints record one, and a layout the code does not know would raise."""
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    assert engine._prompt_layout == getattr(stub_vendor["model"], "prompt_layout", "state-first")


def test_load_is_idempotent(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    """`--preload` and a lazy first request can race; the second call must not reload."""
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    engine.load(Settings(model_dir=checkpoint.parent))
    assert len(stub_vendor["calls"]) == 1


def test_close_releases_the_model_and_the_device(checkpoint: Path, stub_vendor: dict[str, Any]) -> None:
    engine = create(QWEN)
    engine.load(Settings(model_dir=checkpoint.parent))
    engine.close()
    assert not engine.loaded
    assert engine.info().device == "unloaded"
    assert engine.info().dtype == "unloaded"


def test_a_model_path_override_reaches_the_loader(tmp_path: Path, stub_vendor: dict[str, Any]) -> None:
    """The one thing `config.py` cannot do: a command-line override is not in the environment.

    The directory is deliberately *not* under `model_dir`, and is named something unrelated
    to the engine id, so only the override can have found it.
    """
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / "decision_config.json").write_text(json.dumps({"max_options": 7}), encoding="utf-8")
    (elsewhere / "readout.safetensors").write_bytes(b"")

    engine = create(QWEN)
    engine.load(Settings(model_dir=tmp_path / "empty", model_paths={QWEN: elsewhere}))
    assert stub_vendor["calls"][0]["checkpoint"] == str(elsewhere)
    assert engine.info().max_options == 7


def test_an_alias_in_a_model_path_override_reaches_the_loader(tmp_path: Path, stub_vendor: dict[str, Any]) -> None:
    """`--model-path jeff=PATH` is documented, and the alias has to survive the trip.

    `paths.candidate_directories` keys overrides on the engine id, so the alias cannot be
    resolved there: `paths.py` sits below `engines/registry.py` and must not import it (that
    would be a cycle). The canonicalisation therefore happens where the option is parsed, and
    this goes through that real function rather than writing the canonical dict by hand --
    otherwise the test would be checking a path no user takes.
    """
    from decis.cli import _parse_model_paths

    elsewhere = tmp_path / "aliased"
    elsewhere.mkdir()
    (elsewhere / "decision_config.json").write_text(json.dumps({"max_options": 5}), encoding="utf-8")
    (elsewhere / "readout.safetensors").write_bytes(b"")

    overrides = _parse_model_paths([f"jeff={elsewhere}"])
    assert overrides == {QWEN: elsewhere}, "the alias must be canonicalised, not kept verbatim"

    engine = create(QWEN)
    engine.load(Settings(model_paths=overrides))
    assert stub_vendor["calls"][0]["checkpoint"] == str(elsewhere)
    assert engine.info().max_options == 5
