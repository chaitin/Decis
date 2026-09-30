"""Jeff against real weights. Run with `pytest -m weights`.

These are the assertions that can only exist with a checkpoint in memory:

* **the prompt is Jeff's, not Decis's** -- the row this engine builds renders through
  upstream's own `decision_messages` to the text the checkpoint was trained on, and the
  flattened text `render.py` produces for Laya and kev is *different*. That difference is the
  whole reason the compatibility layer exists, and it fails silently: nothing raises, the
  answer is just worse than the checkpoint's benchmark.
* **batch invariance** -- `docs/design.md §6.6` and `tests/test_batch_invariance.py` explain
  why the same question must not change its answer because of who else was in the batch. The
  official SDK retries POSTs, so a flip is user-visible. "One `predict` is one upstream call"
  is asserted here too, because the throughput story depends on it and nothing else checks it.
* **over-long is refused, not truncated** -- `AGENTS.md §9`. Upstream's `prepare` raises past
  its window; the engine's `measure()` has to turn that into a 422 that names the limit.
* **the readout is wired to the backbone** -- two clearly different states must not produce
  the same distribution. A readout loaded with the wrong weights, or a hidden state taken
  from the wrong position, produces a plausible-looking distribution that never changes.

The Gemma checkpoint is 8.65 GiB of weights, 18.5 GiB of them resident in `float32` on CPU and
23.8 GiB of peak RSS in a measured load, so loading it is opt-in: set `DECIS_TEST_JEFF_GEMMA=1`,
and run the two engines as separate passes -- both resident at once needs ~26 GiB, which the
31 GiB host this was measured on cannot spare. The suite parametrises over both engines anyway,
so the skip is visible in the report rather than hidden behind a shorter list.

The two checkpoints' prompts are *not* interchangeable, which is why the helpers below read the
prompt back out of the engine's own token ids instead of re-rendering it: `docs/design-review.md
§2-D30` is the bug that assumption caused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decis.config import Settings, load_settings
from decis.domain import PreparedRequest
from decis.engines.base import WorkItem
from decis.engines.jeff import MAX_SEQUENCE, WEIGHTS, JeffEngine
from decis.engines.registry import create
from decis.errors import EngineUnavailableError, InvalidRequestError
from decis.paths import resolve
from decis.render import prepare_request
from decis.scheduler import InProcessScheduler
from decis.schema import SystemOneRequest, validate_capacity

pytestmark = pytest.mark.weights

QWEN = "jeff-qwen3.5-0.8b"
GEMMA = "jeff-gemma4-e2b"
ENGINE_IDS = (QWEN, GEMMA)

#: How far the batched path may sit from the single-item path. Set from measurement rather
#: than taste, like `test_kev_inference.py: PARITY_TOLERANCE`: the readout is one linear layer
#: over a hidden state, so the only source of drift is the padded batch's reductions. A
#: masking bug moves a distribution by order 1 (`docs/design-review.md §2-D9`), so this still
#: fails loudly on one.
BATCH_TOLERANCE = 1e-5

#: The two states the readout-is-wired test compares. One is an unambiguous escalation, the
#: other an unambiguous non-event, and the assertion is only that the distributions *differ*
#: -- not which way, because that is the checkpoint's business and not the contract's.
OBVIOUS = "I have been charged twice and nobody has replied for six days. I am cancelling today."
CALM = "Thanks, the replacement cable arrived this morning and works fine. No further action needed."

#: Non-Latin and not escaped on purpose: `describe()` must pass it through as written, and the
#: fullwidth punctuation is the part a default `json.dumps(ensure_ascii=True)` would rewrite as
#: `\uXXXX`. The lint warning about ambiguous characters is about prose, not about this string.
NON_ASCII_STATE = "重复扣款，请立即处理"  # noqa: RUF001


@pytest.fixture(scope="module", params=ENGINE_IDS)
def engine(request: pytest.FixtureRequest) -> JeffEngine:
    """One loaded engine per checkpoint, for the whole module: loading is slow on CPU."""
    engine_id = request.param
    if engine_id == GEMMA and os.environ.get("DECIS_TEST_JEFF_GEMMA") != "1":
        pytest.skip(
            f"{GEMMA} is 8.65 GiB of weights (~23.8 GiB peak RSS in float32 on CPU, measured); "
            "set DECIS_TEST_JEFF_GEMMA=1 to include it"
        )
    instance = create(engine_id)
    assert isinstance(instance, JeffEngine)
    try:
        instance.load(load_settings())
    except EngineUnavailableError as exc:  # a missing extra, not a missing checkpoint
        pytest.skip(f"{engine_id} cannot load here: {exc}")
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def checkpoint_dir(engine: JeffEngine) -> Path:
    """The directory the engine loaded from, so its own config can be the oracle."""
    source = resolve(WEIGHTS[engine.info().id], load_settings())
    if not source.is_local:
        pytest.skip("the checkpoint is not on disk; run `decis download` first")
    assert source.path is not None
    return source.path


@pytest.mark.parametrize("engine_id", ENGINE_IDS)
def test_the_checkpoint_can_be_read_and_templated_without_loading_it(engine_id: str) -> None:
    """The cheap half of "can this checkpoint run here?", and it runs for *both* engines.

    The rest of this file needs the weights in memory, which for the Gemma checkpoint means
    8.65 GiB of download and ~23.8 GiB of peak RSS. Everything the loader does *before* the
    weights is far cheaper and can still be the reason an engine never loads:

    * the architecture its `config.json` names must exist in this `transformers` -- otherwise
      `AutoModel.from_pretrained` raises, and with a 4.63B-parameter checkpoint you would only
      find out on a machine with the RAM to try;
    * the chat template must render with the arguments the vendored decoder passes it, since
      `enable_thinking=False` is one of them and a template that rejects an argument raises
      where the model is asked for its prompt.

    Measured here: `transformers 5.17.0` resolves `Qwen3_5Model` and `Gemma4TextModel`, and
    both templates render. Skips when that engine's directory is not on disk, so it costs
    nothing on a machine that has not downloaded it.
    """
    source = resolve(WEIGHTS[engine_id], load_settings())
    if not source.is_local:
        pytest.skip(f"{engine_id} is not on disk; run `decis download --engine {engine_id}` first")
    assert source.path is not None

    transformers = pytest.importorskip("transformers", reason="the `jeff` extra is not installed")
    config = transformers.AutoConfig.from_pretrained(source.path, local_files_only=True)
    architectures = list(config.architectures or ())
    assert architectures, "the checkpoint must name the class that loads it"
    for architecture in architectures:
        assert hasattr(transformers, architecture), (
            f"{engine_id} names {architecture}, which this transformers "
            f"({transformers.__version__}) does not implement; `AutoModel.from_pretrained` "
            f"cannot load this checkpoint at all"
        )

    tokenizer = transformers.AutoTokenizer.from_pretrained(source.path, local_files_only=True)
    rendered = tokenizer.apply_chat_template(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tokenize=False,
        add_generation_prompt=True,
        # The same three arguments `_jeff_vendor/decoder.py` passes.
        enable_thinking=False,
    )
    assert isinstance(rendered, str) and rendered.strip(), "the template must render to text"


def prepared_for(
    engine: JeffEngine, *, state: object, questions: dict[str, object] | None = None
) -> tuple[PreparedRequest, list[WorkItem]]:
    """A prepared request and its work items, built through the real renderer.

    Both come out of one `prepare_request` on purpose: the point of most tests here is that
    `raw_state` reaches the engine, and re-deriving the items from a second request would let
    the two halves disagree without anything noticing.
    """
    request = SystemOneRequest(
        model=engine.info().id,
        state=state,
        questions=questions or ALL_THREE,
    )
    prepared = prepare_request(request)
    items = [
        WorkItem(
            request_id="r1",
            state_text=prepared.state_text,
            question=question,
            raw_state=prepared.raw_state,
        )
        for question in prepared.questions
    ]
    return prepared, items


#: All three primitives in one request, so a single call exercises every path the service
#: takes from JSON to a row.
ALL_THREE: dict[str, object] = {
    "escalate": {
        "type": "noul",
        "instructions": "Does this need urgent human attention?",
        "criteria": {"true": "Needs a person now", "false": "Can wait"},
    },
    "team": {
        "type": "choice",
        "instructions": "Which team should handle this?",
        "criteria": {
            "returns": "Exchanges, refunds, wrong or damaged items",
            "shipping": "Delivery status, delays, lost packages",
            "billing": "Charges, invoices, payment problems",
        },
    },
    "frustration": {
        "type": "score",
        "instructions": "How frustrated is the customer?",
        "criteria": ["Calm", "Frustrated", "Very angry"],
    },
}

#: One question, for the tests that want a single row.
ONLY_ESCALATE = {"escalate": ALL_THREE["escalate"]}


def items_for(engine: JeffEngine, *, state: object) -> list[WorkItem]:
    return prepared_for(engine, state=state)[1]


@pytest.fixture(scope="module")
def config(checkpoint_dir: Path) -> dict[str, object]:
    """The checkpoint's own `decision_config.json`, which is the oracle for its constants."""
    return json.loads((checkpoint_dir / "decision_config.json").read_text(encoding="utf-8"))


# --- what it loaded -----------------------------------------------------------


def test_the_engine_loads_and_reports_what_it_loaded(engine: JeffEngine) -> None:
    info = engine.info()
    assert info.device in {"cpu", "cuda", "mps"}
    assert info.dtype in {"float32", "float16", "bfloat16"}
    assert info.max_options > 0
    assert info.version != "not-installed"
    assert info.max_sequence_tokens == MAX_SEQUENCE


def test_the_reported_ceiling_is_the_checkpoints_own_number(engine: JeffEngine, config: dict[str, object]) -> None:
    """`max_options` in `decision_config.json` is the trained ceiling; nothing else is.

    A load that never re-read the file would report the subclass constant instead, which is
    the same number only because it was written down from the same file.
    """
    declared = config["max_options"]
    assert engine.info().max_options == declared
    assert declared < 255, "both checkpoints trained on fewer options than the readout can express"


def test_the_reported_temperature_is_the_checkpoints_own(engine: JeffEngine, config: dict[str, object]) -> None:
    """Not 1.0: the checkpoint's fitted temperature is part of the model.

    Upstream applies it before the softmax, so a loader that dropped it would produce
    over-confident distributions -- plausible, well-formed and wrong.
    """
    declared = config["temperature"]
    assert engine._model.temperature == pytest.approx(declared)
    assert declared != 1.0


def test_the_answer_vocabulary_is_the_full_readout(engine: JeffEngine) -> None:
    """255 distinct single-token codes, re-derived by the vendored loader from the tokenizer.

    A tokenizer that cannot reproduce them makes the loader raise, so this is a statement
    about the checkpoint on disk rather than a check of the loader.
    """
    assert len(engine._model.codes) == 255
    assert len(set(engine._model.codes)) == 255


# --- the compatibility layer, on the real tokenizer ---------------------------


def prompt_of(engine: JeffEngine, item: WorkItem) -> str:
    """The prompt this engine really feeds its model, read back from its own token ids.

    Not rebuilt here out of `decision_messages` plus a chat template, which is what this
    helper used to do and what `docs/design-review.md §2-D30` is about: the two upstream
    loaders render the prompt through *different* paths, so a mirror of one of them asserts a
    rendering the other engine never sends.

    * Qwen: `processor.apply_chat_template(messages, ..., enable_thinking=False)` with the user
      turn's multimodal content intact (`_jeff_vendor/model.py:213`), tokenised by the
      processor;
    * Gemma: `chat_text` → `tokenizer.apply_chat_template(...)`, with the user turn rewritten
      to its last text part (`_jeff_vendor/decoder.py:105-120`), tokenised with
      `add_special_tokens=False` — and `GenericDecoderDecisionModel` has no `.processor` at
      all, so the mirror did not even raise a legible failure: it was an `AttributeError`.

    Decoding `prepare()`'s own `input_ids` cannot drift from what was tokenised, and the two
    checkpoints' prompts differ structurally anyway (`<|im_start|>`/`<think>` for Qwen,
    `<|turn>`/`model` for Gemma) while both carry the caller's JSON.
    """
    batch = engine._model.prepare([engine._row(item)])
    tokenizer = getattr(engine._model, "tokenizer", None) or engine._model.processor.tokenizer
    return str(tokenizer.decode(batch.inputs["input_ids"][0], skip_special_tokens=False))


def test_the_prompt_carries_the_callers_json_and_not_the_flattened_text(engine: JeffEngine) -> None:
    """The load-bearing assertion of the whole feature, in bytes.

    Read back from the ids the engine tokenised, so this is the string the model receives
    rather than a description of it. `describe()` is `json.dumps` for a non-string, which is
    why the dict survives as JSON.
    """
    from decis.engines._jeff_vendor import describe

    state = {"voice_transcript": "I was charged twice", "current_screen": "billing"}
    item = items_for(engine, state=state)[0]
    prompt = prompt_of(engine, item)

    assert describe(state) in prompt, "the object must reach the prompt as JSON"
    # And the flattened form is genuinely a different string, so an engine that sent
    # `state_text` would produce a different prompt -- which is what makes this a test.
    assert item.state_text not in prompt
    assert "current_screen: billing" in item.state_text


def test_the_engine_tokenises_the_prompt_this_suite_asserts_on(engine: JeffEngine) -> None:
    """The prompt assertions above are about *this* engine's tokenised sequence.

    `prompt_of` now reads the prompt out of `prepare()`'s ids, so "the suite's prompt" and
    "what the engine sends" cannot diverge by construction. What is still worth pinning is
    the substance, on both checkpoints: the ids carry the caller's JSON and not the
    flattened text. That is the assertion an engine that quietly reverted to `state_text`
    would fail, and it is the reason the compatibility layer exists.
    """
    from decis.engines._jeff_vendor import describe

    state = {"voice_transcript": "I was charged twice", "current_screen": "billing"}
    item = items_for(engine, state=state)[0]
    batch = engine._model.prepare([engine._row(item)])
    tokenizer = getattr(engine._model, "tokenizer", None) or engine._model.processor.tokenizer
    decoded = str(tokenizer.decode(batch.inputs["input_ids"][0], skip_special_tokens=False))

    assert describe(state) in decoded, "the JSON the caller sent is what gets tokenised"
    assert item.state_text not in decoded, "the flattened text must not be what gets tokenised"
    assert batch.inputs["input_ids"].shape[0] == 1, "one row in, one sequence out"


def test_a_null_in_the_state_survives_to_the_prompt(engine: JeffEngine) -> None:
    """`describe({"a": None})` is `{"a": null}`; the flattened text drops the key entirely.

    This is the case where the two prompts differ by *content* rather than by formatting:
    `render.render_value` has no text for a null, so `state_text` loses the field and
    flattens a nested object into an ambiguous `nested: y: 1`. A checkpoint trained on the
    JSON now sees a state with a field missing -- not an error, just a worse answer.
    """
    item = items_for(engine, state={"a": None, "cancelled_at": "2026-09-30", "nested": {"y": 1}})[0]
    prompt = prompt_of(engine, item)

    assert '"a": null' in prompt, "the null must survive as JSON"
    assert '"nested": {"y": 1}' in prompt, "and so must the nesting"
    assert "null" not in item.state_text, "the flattened text is the lossy one"
    assert "nested: y: 1" in item.state_text


def test_the_state_is_described_not_reprd(engine: JeffEngine) -> None:
    """A string state must not gain quotes, and non-ASCII must not gain escapes.

    `describe` passes a string through unchanged and calls `json.dumps(..., ensure_ascii=False)`
    for everything else; a `!r` or a default `json.dumps` would change every prompt for a
    caller whose state contains non-Latin text.
    """
    item = items_for(engine, state=NON_ASCII_STATE)[0]
    prompt = prompt_of(engine, item)
    assert NON_ASCII_STATE in prompt
    assert "\\u" not in prompt


# --- capacity, measured with the real processor -------------------------------


def test_measure_counts_the_real_chat_templated_sequence(engine: JeffEngine) -> None:
    """Not `len(text)//4`: the chat template and the option block are most of the prompt.

    A short state with three options already costs more than a hundred tokens, which is the
    margin a character estimate would under-report.
    """
    request = prepare_request(
        SystemOneRequest(
            model=engine.info().id,
            state=OBVIOUS,
            questions={
                "escalate": {
                    "type": "noul",
                    "instructions": "Does this need urgent human attention?",
                    "criteria": {"true": "Needs a person now", "false": "Can wait"},
                }
            },
        )
    )
    measured = engine.measure(request)
    assert 0 < measured.sequence_tokens < MAX_SEQUENCE
    assert measured.sequence_tokens > len(request.state_text) // 4, "a character estimate would be too small"
    assert measured.state_tokens > 0, "the state has to be charged for its room"
    assert measured.head_tokens["escalate"] > 0


def test_measure_and_predict_agree_about_how_many_tokens_a_request_costs(engine: JeffEngine) -> None:
    """Two code paths count tokens; a mismatch means the reported `usage` describes a
    different set of forward passes than the one that produced the answers."""
    items = items_for(engine, state=OBVIOUS)
    request = prepare_request(
        SystemOneRequest(
            model=engine.info().id,
            state=OBVIOUS,
            questions={
                "escalate": {
                    "type": "noul",
                    "instructions": "Does this need urgent human attention?",
                    "criteria": {"true": "Needs a person now", "false": "Can wait"},
                }
            },
        )
    )
    predicted = engine.predict([items[0]])
    assert predicted.input_tokens == engine.measure(request).sequence_tokens


def test_an_over_long_request_is_refused_rather_than_truncated(engine: JeffEngine) -> None:
    """`AGENTS.md §9`. Upstream would raise; the capacity check has to turn that into a 422.

    The state is built to blow the 8192-token window on its own, so the only way this request
    could be served is by truncating it -- which upstream refuses to do and Decis must not
    start doing.
    """
    request = prepare_request(
        SystemOneRequest(
            model=engine.info().id,
            state=" ".join(["padding"] * (MAX_SEQUENCE + 1)),
            questions={
                "escalate": {
                    "type": "noul",
                    "instructions": "Does this need urgent human attention?",
                    "criteria": {"true": "Needs a person now", "false": "Can wait"},
                }
            },
        )
    )
    measured = engine.measure(request)
    assert measured.sequence_tokens > MAX_SEQUENCE, "the measurement must be allowed past the limit"
    with pytest.raises(InvalidRequestError) as caught:
        validate_capacity(request, engine.info(), engine.measure)
    assert str(MAX_SEQUENCE) in str(caught.value) or "token" in str(caught.value)
    # And upstream's own refusal, which is what makes the number above meaningful rather
    # than decorative.
    with pytest.raises(ValueError, match="no input was truncated"):
        engine._model.prepare(engine._rows(items_for(engine, state="s")[:1]), max_length=1)


def test_a_question_wider_than_the_trained_ceiling_is_refused(engine: JeffEngine) -> None:
    width = engine.info().max_options + 1
    request = prepare_request(
        SystemOneRequest(
            model=engine.info().id,
            state="s",
            questions={
                "wide": {
                    "type": "choice",
                    "instructions": "Which one?",
                    "criteria": {f"option-{index}": f"value-{index}" for index in range(width)},
                }
            },
        )
    )
    with pytest.raises(InvalidRequestError, match=f"at most {engine.info().max_options}"):
        validate_capacity(request, engine.info(), engine.measure)


# --- inference ----------------------------------------------------------------


def test_predict_answers_every_primitive_with_a_proper_distribution(engine: JeffEngine) -> None:
    items = items_for(engine, state=OBVIOUS)
    prediction = engine.predict(items)

    assert len(prediction.probabilities) == len(items)
    for item, distribution in zip(items, prediction.probabilities, strict=True):
        assert len(distribution) == len(item.question.options)
        assert all(0.0 <= value <= 1.0 for value in distribution)
        assert sum(distribution) == pytest.approx(1.0, abs=1e-5)
    assert prediction.input_tokens > 0


def test_the_same_question_does_not_change_because_of_its_neighbours(engine: JeffEngine) -> None:
    """`docs/design.md §6.6`. The official SDK retries POSTs, so a composition-dependent
    answer is user-visible; an argmax flip is a failure rather than a wobble."""
    alone = items_for(engine, state=OBVIOUS)
    padded = items_for(engine, state=OBVIOUS) + items_for(engine, state="short") + items_for(engine, state=CALM)

    single = engine.predict(alone)
    together = engine.predict(padded)

    for index, (expected, actual) in enumerate(zip(single.probabilities, together.probabilities, strict=False)):
        for left, right in zip(expected, actual, strict=True):
            assert abs(left - right) <= BATCH_TOLERANCE, f"item {index} drifted by {abs(left - right)}"
        assert expected.index(max(expected)) == actual.index(max(actual)), "the argmax flipped"


def test_one_predict_is_one_call_into_upstream(engine: JeffEngine) -> None:
    """The throughput story depends on it, and upstream does the chunking itself.

    Wrapped rather than asserted through a counter, because the thing being ruled out is a
    second *forward pass* -- an extra `prepare` for the token count is expected and fine.
    """
    calls: list[int] = []
    original = engine._model.predict

    def counting(rows: list[dict[str, object]], batch_size: int = 8) -> list[list[float]]:
        calls.append(len(rows))
        return original(rows, batch_size=batch_size)

    engine._model.predict = counting  # type: ignore[method-assign]
    try:
        items = items_for(engine, state=OBVIOUS) * 4  # 12 items, past the batch size of 8
        engine.predict(items)
    finally:
        engine._model.predict = original  # type: ignore[method-assign]

    assert calls == [12], f"expected one upstream call over all 12 items, got {calls}"


def test_the_readout_is_wired_to_the_backbone(engine: JeffEngine) -> None:
    """Two opposite states must not give the same answer.

    Guards the failure that a plausible distribution cannot rule out on its own: a readout
    whose weights were not loaded, a hidden state read from the wrong position, or a prompt
    that ignores the state entirely all produce a well-formed distribution that is simply
    always the same. This is a wiring check, not an accuracy claim.
    """
    escalate = engine.predict(items_for(engine, state=OBVIOUS)[:1]).probabilities[0]
    fine = engine.predict(items_for(engine, state=CALM)[:1]).probabilities[0]
    drift = max(abs(left - right) for left, right in zip(escalate, fine, strict=True))
    assert drift > 1e-3, f"the same answer for opposite states ({escalate} vs {fine})"


# --- end to end, through the real service ------------------------------------


def test_the_whole_stack_answers_a_request_from_the_compatibility_layer(engine: JeffEngine) -> None:
    """The engine tests above call `predict` directly; this one goes through the service.

    What is being checked is the *plumbing* between them: `service.py` has to carry
    `raw_state` into each `WorkItem`, and a regression there would leave every unit test
    green while every real answer silently used the flattened text.
    """
    from decis.app import create_app

    settings = Settings(default_engine=engine.info().id, api_keys=("test-token",))
    scheduler = InProcessScheduler(engine, settings=settings, request_timeout_ms=settings.request_timeout_ms)
    scheduler.load()  # idempotent: the engine is already loaded
    app = create_app(settings, scheduler=scheduler, load_engine=False)

    payload = {
        "model": "jev-latest",
        "state": {"voice_transcript": OBVIOUS, "current_screen": "billing"},
        "questions": {
            "escalate": {
                "type": "noul",
                "instructions": "Does this need urgent human attention?",
                "criteria": {"true": "Needs a person now", "false": "Can wait"},
            },
            "team": {
                "type": "choice",
                "instructions": "Which team should handle this?",
                "criteria": {
                    "returns": "Exchanges, refunds, wrong or damaged items",
                    "billing": "Charges, invoices, payment problems",
                },
            },
            "frustration": {
                "type": "score",
                "instructions": "How frustrated is the customer?",
                "criteria": ["Calm", "Frustrated", "Very angry"],
            },
        },
    }

    with TestClient(app) as client:
        response = client.post("/v1/systemone", json=payload, headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["answers"]) == {"escalate", "team", "frustration"}
    assert body["usage"]["input_tokens"] > 0
    # The alias was answered by this engine, and the substitution is reported rather than
    # hidden (`decis.requested_model`).
    assert body["decis"]["requested_model"] == "jev-latest"
    assert engine.info().id in body["model"]
    # A well-formed answer for each primitive, which is what "the plumbing held" means.
    noul = body["answers"]["escalate"]["noul"]
    assert 0.0 <= noul <= 1.0
    assert set(body["answers"]["team"]["probabilities"]) == {"returns", "billing"}
    score = body["answers"]["frustration"]
    # `score` is the expected value `Σ k·p_k` (`AGENTS.md §3-5`), not a chosen level, so it
    # is a float in [0, 2] rather than one of {0, 1, 2}.
    assert 0.0 <= score["score"] <= 2.0
    assert set(score["legend"]) == {"0", "1", "2"}
    assert sum(score["probabilities"].values()) == pytest.approx(1.0, abs=1e-5)
