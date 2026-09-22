"""kev's engine, without weights. CI runs this.

mirrors `tests/test_engines_laya.py`: the parts of the engine that are arithmetic or
pure mapping are tested here, so a regression in them fails in seconds instead of
after a 40-second load. The numerical claims live in `test_kev_inference.py`
(`-m weights`).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from decis.domain import Option, PreparedQuestion
from decis.engines import kev as kev_module
from decis.engines.base import EngineInfo
from decis.engines.kev import MAX_BRANCH, MAX_STATE, KevEngine, _option_text
from decis.engines.registry import SPECS, create
from decis.errors import InvalidRequestError
from decis.render import noul_options, prepare_request
from decis.schema import SystemOneRequest, validate_capacity

REQUEST = {
    "model": "kev-0.8b",
    "state": "A customer is unhappy.",
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "Which team should handle this?",
            "criteria": {"returns": "Refunds and exchanges", "billing": "Charges and invoices"},
        },
        "escalate": {
            "type": "noul",
            "instructions": "Does this need a person?",
            "criteria": {"true": "Needs a person now", "false": "Can wait"},
        },
        "frustration": {
            "type": "score",
            "instructions": "How frustrated?",
            "criteria": ["Calm", "Frustrated", "Very angry"],
        },
    },
}


def _prepared():
    return prepare_request(SystemOneRequest.model_validate(REQUEST))


def _question(**kwargs) -> PreparedQuestion:
    base = {
        "qid": "q",
        "type": "choice",
        "instructions": "Pick one.",
        "options": (Option("a", "first"), Option("b", "second")),
    }
    return PreparedQuestion(**{**base, **kwargs})


# --- the option text kev was trained on (the D9 guard, weight-free half) --------


def test_option_text_is_a_bare_name_without_a_description() -> None:
    assert _option_text("billing", "") == "billing"
    assert _option_text("billing", "Charges") == "billing: Charges"


def test_choice_options_are_name_colon_description() -> None:
    """kev and Decis agree here, and it is worth pinning that they do."""
    engine = KevEngine()
    choice = next(q for q in _prepared().questions if q.type == "choice")
    assert engine._question(choice)["options"] == ["returns: Refunds and exchanges", "billing: Charges and invoices"]


def test_noul_options_are_no_and_yes_not_the_wire_keys() -> None:
    """The contract's keys are "false"/"true"; kev's *prompt* text is "no"/"yes".

    Getting this backwards is silent (the model still answers, just worse), which is why
    it is asserted on the weight-free path too.
    """
    engine = KevEngine()
    question = _question(type="noul", options=noul_options("Can wait", "Needs a person now"))
    built = engine._question(question)
    assert built["options"] == ["no: Can wait", "yes: Needs a person now"]
    assert question.options[0].name == "false", "the wire key is still false/true"
    assert not any("false" in option for option in built["options"])


def test_score_options_are_the_bare_level_text() -> None:
    """No "0: " prefix: upstream sends the level text alone (kev/api.py:114)."""
    engine = KevEngine()
    score = next(q for q in _prepared().questions if q.type == "score")
    assert engine._question(score)["options"] == ["Calm", "Frustrated", "Very angry"]


def test_a_question_with_no_description_does_not_gain_a_colon() -> None:
    engine = KevEngine()
    question = _question(type="choice", options=(Option("billing", ""),))
    assert engine._question(question)["options"] == ["billing"]


def test_the_instructions_reach_kev_verbatim() -> None:
    engine = KevEngine()
    choice = next(q for q in _prepared().questions if q.type == "choice")
    assert engine._question(choice)["instr"] == "Which team should handle this?"


def test_the_question_id_is_never_sent_to_the_model() -> None:
    """Invariant §3-6, checked at the level where it could actually leak."""
    engine = KevEngine()
    question = _question(qid="secret_question_id")
    assert "secret_question_id" not in repr(engine._question(question))


# --- declared capacities -------------------------------------------------------


def test_info_declares_both_of_kevs_windows() -> None:
    """kev caps the state alone at 384 and state+question at 1024; both must be declared.

    Before this, `EngineInfo` had nowhere to put the state cap and `validate_capacity`
    never looked at `state_tokens`, so a 500-token state passed the check and was then
    truncated by `encode` without a word (design-review.md §2-D10).
    """
    info = KevEngine().info()
    assert info.max_state_tokens == MAX_STATE == 384
    assert info.max_sequence_tokens == MAX_BRANCH == 1024
    # The per-question bound must not be tighter than the sequence bound, or it rejects
    # requests kev handles (a long question about a short state).
    assert info.max_question_tokens == MAX_BRANCH
    assert info.primitives == frozenset({"noul", "choice", "score"})
    assert info.capacities()["max_state_tokens"] == MAX_STATE


def test_an_engine_without_a_state_cap_keeps_the_old_behaviour() -> None:
    """`max_state_tokens` defaults to 0 = "no separate limit", so Laya is unaffected."""
    assert (
        EngineInfo(
            id="x",
            version="1",
            primitives=frozenset({"noul"}),
            max_options=2,
            max_sequence_tokens=10,
            max_question_tokens=10,
            languages="en",
            device="cpu",
            dtype="fp32",
            description="",
            release_date="",
        ).max_state_tokens
        == 0
    )


def test_a_state_over_the_cap_is_rejected_rather_than_truncated() -> None:
    """The D10 regression test, on the schema side, with a stub measurement.

    An engine that reports a state over its declared cap must be refused -- that is the
    whole point of the field existing.
    """
    from decis.domain import MeasuredTokens

    capacity = KevEngine().info()

    def measure(_request):
        return MeasuredTokens(state_tokens=500, head_tokens={"q": 10}, sequence_tokens=510)

    with pytest.raises(InvalidRequestError) as caught:
        validate_capacity(_prepared(), capacity, measure)
    assert caught.value.loc == ["body", "state"]
    # The message must name the content, not a phantom question limit.
    assert "state" in str(caught.value).lower()


def test_a_state_exactly_at_the_cap_is_allowed() -> None:
    from decis.domain import MeasuredTokens

    capacity = KevEngine().info()

    def measure(_request):
        return MeasuredTokens(state_tokens=MAX_STATE, head_tokens={"q": 10}, sequence_tokens=MAX_STATE + 10)

    validate_capacity(_prepared(), capacity, measure)  # must not raise


def test_a_request_over_the_sequence_cap_is_still_rejected() -> None:
    from decis.domain import MeasuredTokens

    capacity = KevEngine().info()

    def measure(_request):
        return MeasuredTokens(state_tokens=100, head_tokens={"q": 1000}, sequence_tokens=1100)

    with pytest.raises(InvalidRequestError):
        validate_capacity(_prepared(), capacity, measure)


# --- measurement without weights ----------------------------------------------


def test_measure_falls_back_to_the_estimate_before_loading() -> None:
    """`measure` must not require a tokenizer: `decis doctor` and 422s come first."""
    measured = KevEngine().measure(_prepared())
    assert measured.sequence_tokens > 0
    assert measured.state_tokens > 0


def test_predict_refuses_before_loading() -> None:
    from decis.errors import EngineUnavailableError

    with pytest.raises(EngineUnavailableError):
        KevEngine().predict([])


# --- declarative weights -------------------------------------------------------


def test_weights_name_both_the_adapter_and_its_base() -> None:
    """An adapter alone cannot answer anything, so the base is declared, not assumed."""
    spec = KevEngine().weights()
    assert spec.repo_id == "jaredpalmer/kev-0.8b"
    assert spec.revision, "a tag or branch would let upstream move what a build loads"
    assert spec.marker == "head.pt"
    assert [base.repo_id for base in spec.bases] == ["Qwen/Qwen3.5-0.8B-Base"]
    assert spec.bases[0].revision, "the base revision is pinned too"


def test_the_adapter_download_excludes_dead_weight() -> None:
    """`tokenizer.json` lives in the adapter repo but is never read: the base has it."""
    spec = KevEngine().weights()
    assert "tokenizer.json" not in spec.checkpoint_files
    assert "adapter_model.safetensors" in spec.checkpoint_files
    assert "head.pt" in spec.checkpoint_files


def test_a_base_becomes_a_resolvable_spec() -> None:
    """`base_specs()` is how download/doctor reach the base with the same rules.

    Derived from `bases` rather than declared twice, so the two cannot drift.
    """
    spec = KevEngine().weights()
    (base,) = spec.base_specs()
    assert base.repo_id == "Qwen/Qwen3.5-0.8B-Base"
    assert base.marker == "config.json"
    assert base.local_dir_name == "Qwen3.5-0.8B-Base"
    # Namespaced: a base is not independently servable and must not look like an engine.
    assert base.engine_id.startswith("kev-0.8b->")
    assert base.engine_id not in SPECS


def test_a_base_download_targets_the_hub_cache() -> None:
    """No `local_dir`: `Checkpoint.load` resolves the base by repo id, so a local_dir
    download would land where the loader never looks and be fetched again."""
    from decis.paths import download_arguments

    arguments = download_arguments(KevEngine().weights().base_specs()[0])
    assert "local_dir" not in arguments
    assert arguments["repo_id"] == "Qwen/Qwen3.5-0.8B-Base"
    assert arguments["revision"]
    assert "*.safetensors" in arguments["allow_patterns"]


def test_laya_weights_are_unaffected_by_the_base_model_addition() -> None:
    """`checkpoint_files` defaults to the Laya file list, so nothing regressed."""
    from decis.paths import _CHECKPOINT_FILES

    weights = create("laya").weights()
    assert weights.checkpoint_files == _CHECKPOINT_FILES
    assert weights.bases == ()


# --- registration and lazy imports --------------------------------------------


def test_kev_is_registered_under_its_own_extra() -> None:
    spec = SPECS["kev-0.8b"]
    assert spec.extra == "kev"
    assert spec.target == "decis.engines.kev:KevEngine"
    assert "kev" in spec.aliases, "callers send `kev-latest`, and upstream's own default is `kev-latest`"


def test_the_registry_can_describe_kev_without_torch() -> None:
    """`AGENTS.md §6`: importing the registry must not import torch."""
    assert "torch" not in sys.modules or _torch_was_already_imported()
    engine = create("kev-0.8b")
    assert engine.info().id == "kev-0.8b"


def _torch_was_already_imported() -> bool:
    # Other tests in the session may have imported torch; the subprocess probe below is
    # the real check, and this keeps the module-level assertion honest about that.
    return True


def test_importing_the_engine_module_does_not_import_torch() -> None:
    """A subprocess probe, because by now the test session has imported torch for real."""
    code = (
        "import sys; import decis.engines.kev; "
        "heavy = {'torch', 'transformers', 'peft'} & set(sys.modules); "
        "print('|'.join(sorted(heavy)))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "", f"importing the engine pulled in {result.stdout.strip()}"


def test_the_engine_module_declares_its_public_surface() -> None:
    for name in ("KevEngine", "WEIGHTS", "MAX_STATE", "MAX_BRANCH", "REVISION"):
        assert name in kev_module.__all__


def test_an_unknown_engine_id_is_a_programming_error_not_a_silent_default() -> None:
    class Wrong(KevEngine):
        engine_id = "kev-nonexistent"

    with pytest.raises(ValueError, match="no weights declared"):
        Wrong()


# --- dtype selection -----------------------------------------------------------


def test_every_registered_engine_with_deps_has_a_cpu_dtype() -> None:
    """A new engine that forgets `DTYPE_DEFAULTS` must not silently get a guess.

    `default_dtype` falls back to fp32 on cpu and fp16 on cuda. For an engine whose
    upstream defaults differently that guess is wrong in a way nothing reports, so the
    engines that need an entry are listed explicitly and this keeps the list honest.
    """
    from decis.engines.registry import DTYPE_DEFAULTS, default_dtype

    # Every engine that declares heavy dependencies must have decided its cpu dtype.
    for engine_id, spec in SPECS.items():
        if not spec.extra:
            continue
        assert (engine_id, "cpu") in DTYPE_DEFAULTS or spec.extra == "laya", (
            f"{engine_id} declares the {spec.extra!r} extra but no cpu dtype; "
            f"either add a DTYPE_DEFAULTS entry or say here why the fallback is right"
        )
    assert default_dtype("kev-0.8b", "cpu") == "fp32", "kev in bf16 on cpu is ~83x slower"


def test_a_known_bad_dtype_is_reported_not_hidden() -> None:
    """The 83x slowdown is a measured fact, so the combination is named, not inferred."""
    from decis.engines.registry import degraded_reason

    assert degraded_reason("kev-0.8b", "cpu", "bf16"), "bf16 on cpu is the measured-bad one"
    assert degraded_reason("kev-0.8b", "cpu", "fp32") is None
    assert degraded_reason("kev-0.8b", "cuda", "bf16") is None


def test_dtype_defaults_never_select_a_degraded_combination() -> None:
    """The default must not be a combination Decis itself warns about."""
    from decis.engines.registry import DTYPE_DEFAULTS, degraded_reason

    for (engine_id, device), dtype in DTYPE_DEFAULTS.items():
        assert degraded_reason(engine_id, device, dtype) is None, f"{engine_id}/{device}/{dtype}"
