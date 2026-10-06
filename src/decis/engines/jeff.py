"""Jeff: full-weight fine-tunes of Qwen3.5 and Gemma 4 with a single-token readout.

A Jeff checkpoint is not a classifier head bolted onto a chat model, and it is not an
adapter either: `model.safetensors` *is* the fine-tuned backbone, and
`readout.safetensors` holds a 255-row linear head whose weights were initialised from
the backbone's own output-embedding rows and then trained. Serving is one prefill pass:
the hidden state at the last prompt token is multiplied by the readout, the logits past
the option count are masked out, and a temperature from `decision_config.json` is
applied before the softmax. Nothing is generated, so the whole model is one forward pass
-- which is why it belongs behind the same contract as Laya and kev.

Two architectures are served here, and the difference is not cosmetic:

* **`jeff-qwen3.5-0.8b`** loads through `_jeff_vendor.DecisionModel`: a `Qwen3_5Model`
  backbone, the prompt applied through the *processor's* chat template with
  `enable_thinking=False`, hidden size read from `config.text_config`.
* **`jeff-gemma4-e2b`** loads through `_jeff_vendor.GenericDecoderDecisionModel`: a
  plain `AutoModel` (`Gemma4TextModel`) with the prompt applied through the
  *tokenizer's* chat template and the user turn collapsed to a bare string, hidden size
  from `config.hidden_size`.

Both classes come from the pinned vendored copy rather than being re-derived here,
because the chat template call and the answer-code vocabulary are exactly the parts that
degrade an answer silently when they are wrong (`_jeff_vendor/VENDOR.md`).

## The compatibility layer: why this engine sees raw JSON

Decis's wire contract is Jev's, and `render.py` flattens any JSON `state`,
`instructions` or criterion into readable text. Jeff was trained on its own serving
format, which is *not* that text: `<owner>/model.py: decision_messages` builds
`"State:\\n" + describe(value)`, and `describe` is `json.dumps(value, ensure_ascii=False)`
for anything that is not a string. A dict state therefore reaches the model as
`{"voice_transcript": "...", "current_screen": "..."}`, not as the flattened
`current_screen: ...` lines Decis sends to Laya and kev.

Feeding it the flattened form would be a silent quality regression -- nothing errors,
the answer is just worse than the checkpoint's benchmark -- so the raw values are
carried through `domain.PreparedQuestion.raw` / `WorkItem.raw_state` and handed to
upstream's own `decision_messages`/`options`. The public API is unchanged: this is an
internal compatibility layer, and `tests/test_engines_jeff.py` pins the resulting prompt
against upstream's function byte for byte.

## What upstream cannot tell us

`model.prepare` **raises** past `max_length` rather than truncating, which is the right
behaviour and the one thing that makes a capacity check meaningful here: `measure()`
counts the real chat-templated sequence with the real processor, so a request that would
not fit is a 422 before the model runs (`AGENTS.md §5-4`).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain import MeasuredTokens, PreparedRequest, ProbDist
from ..errors import EngineUnavailableError
from ..paths import WeightSpec, fetch_checkpoint, resolve
from .base import DecisionEngine, EngineInfo, Prediction, WorkItem, validate_distribution
from .devices import best_device, requested_device, warn_if_accelerator_is_idle

if TYPE_CHECKING:  # pragma: no cover - types only, never imported at runtime
    from ..config import Settings

_logger = logging.getLogger("decis.engines.jeff")

#: The vendored serving code's own sequence limit: `prepare(max_length=8192)`, for both
#: architectures. It is upstream's *serving* window, not a claim about the backbone's
#: positional capacity -- and it is a hard failure past it, which is what makes the
#: number worth advertising.
MAX_SEQUENCE = 8192

#: Upstream's own serving batch size (`predict(batch_size=8)`). Kept rather than raised:
#: a bigger batch trades memory for throughput on hardware nobody has measured here.
_BATCH_SIZE = 8

#: The readout always has this many rows (`_jeff_vendor/model.py: MAX_OPTIONS`), and the
#: answer-code vocabulary is the first 255 single-token codes. A checkpoint's own
#: `max_options` may be smaller, and then it is the binding one.
_HARD_MAX_OPTIONS = 255

#: The tag the vendored copy is pinned to, reported as the engine version. A caller who
#: needs to reproduce an answer needs the code that ran the forward pass; the checkpoint
#: revision is pinned in each `WeightSpec` below.
REVISION = "vendored-f0397f3"

#: The pinned tag's date (`git log -1 v1.1`), which is also the day both checkpoints were
#: published. `GET /v1/models` needs *a* date and this is the verifiable one.
CHECKPOINT_DATE = "2026-09-29"

#: Importable modules the checkpoint cannot load without. `PIL` is `pillow`: upstream's
#: `model.py`/`types.py` import it at module scope for the image path, even though the
#: Jeff checkpoints are text-only and Decis never sends images.
#:
#: `torchvision` is here for a subtler reason, and it is listed because a real load
#: failed without it (2026-09-30, `tests/test_jeff_inference.py`): the Qwen checkpoint's
#: processor is `Qwen3VLProcessor`, and because it descends from Qwen3-VL its
#: `processor_config.json` names a `Qwen3VLVideoProcessor`. `AutoProcessor.from_pretrained`
#: builds *every* sub-processor named there, and that class raises `ImportError: ...
#: requires the Torchvision library` while being imported -- so the load fails before a
#: single tensor is read, from a code path that never touches a video.
_REQUIRES = ("torch", "transformers", "torchvision", "safetensors", "PIL")

#: The interpreter this engine needs, because the vendored serving code uses PEP 695 `type`
#: aliases (`_jeff_vendor/types.py:8-12`, and `Question`/`Answer` below them). On 3.11 that
#: file is a `SyntaxError`, so the *package* cannot be imported at all -- not "a feature is
#: missing", the whole engine. The project's own floor stays 3.11 (`requires-python`) and the
#: published images run 3.13, so this is a per-engine floor: `registry.EngineSpec.python_min`
#: carries the same number for `decis models`, and `tests/test_engines_jeff.py` asserts the
#: two agree so neither can drift.
#:
#: Why not rewrite the aliases the way the imports were rewritten (that is the one edit this
#: vendor copy already carries): they are **recursive** (`JSONValue` contains
#: `list[JSONValue]`), which PEP 695 evaluates lazily and a plain `TypeAlias` assignment
#: cannot express without turning them into strings. A mechanical rewrite would therefore
#: stop being mechanical, and `tests/test_jeff_vendor.py` would have to give up pinning the
#: vendored text against upstream (`design-review.md §2-D31`).
MIN_PYTHON = (3, 12)

#: What one checkpoint is made of. Both repositories also carry videos and README assets
#: that an inference server has no use for; listing the files is what keeps
#: `decis download` from pulling them.
_CHECKPOINT_FILES = (
    "*.json",
    "*.safetensors",
    "tokenizer*",
    "*.jinja",
    "LICENSE",
    "NOTICE",
)

#: The Qwen checkpoint declares `license: apache-2.0` and links no separate text, so its model
#: page is the reference. The Gemma one declares `apache-2.0` too but carries a `license_link`
#: to Google's Gemma 4 terms, because its base is `google/gemma-4-E2B-it` -- that link is the
#: more useful of the two to hand a reader, so it is the one recorded.
_QWEN_LICENSE_URL = "https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B"
_GEMMA_LICENSE_URL = "https://ai.google.dev/gemma/docs/gemma_4_license"

#: Sizes measured from the Hub's own file listing (`?blobs=true`, 2026-09-30), restricted
#: to `_CHECKPOINT_FILES`. Reported by `decis doctor` and `decis download`.
_QWEN_BYTES = 1_726_570_651
_GEMMA_BYTES = 9_290_236_873

WEIGHTS: dict[str, WeightSpec] = {
    "jeff-qwen3.5-0.8b": WeightSpec(
        engine_id="jeff-qwen3.5-0.8b",
        repo_id="mstrasser/Jeff-Qwen3.5-0.8B",
        revision="0f212b3e72acb4dde3f7da61e925d6ab7f819990",
        marker="decision_config.json",
        expected_bytes=_QWEN_BYTES,
        license_name="Apache-2.0",
        license_url=_QWEN_LICENSE_URL,
        requires=_REQUIRES,
        checkpoint_files=_CHECKPOINT_FILES,
    ),
    "jeff-gemma4-e2b": WeightSpec(
        engine_id="jeff-gemma4-e2b",
        repo_id="mstrasser/Jeff-Gemma4-E2B",
        revision="e3de3e99a979f92afd995d4e526c7e3170ae49cf",
        marker="decision_config.json",
        expected_bytes=_GEMMA_BYTES,
        license_name="Apache-2.0",
        license_url=_GEMMA_LICENSE_URL,
        requires=_REQUIRES,
        checkpoint_files=_CHECKPOINT_FILES,
    ),
}


class JeffEngine(DecisionEngine):
    """Serves one Jeff checkpoint.

    One subclass per checkpoint rather than one class taking an id, because
    `registry.create` builds engines with no arguments -- that is what keeps importing
    the registry free of torch.
    """

    #: Set by each subclass.
    engine_id: str = ""
    #: Which vendored loader to use, and the option ceiling from the pinned checkpoint's
    #: own `decision_config.json`. Both are replaced by the checkpoint's real values at
    #: load time; the constants exist so that `GET /v1/models` and `decis models` can be
    #: answered before (or without) loading anything, and they describe the checkpoint
    #: this build pins rather than an invented ceiling.
    loader: str = ""
    default_max_options: int = _HARD_MAX_OPTIONS

    def __init__(self) -> None:
        super().__init__()
        self._id = self.engine_id
        if self._id not in WEIGHTS:
            raise ValueError(f"JeffEngine has no weights declared for {self._id!r}")
        if not self.loader:
            raise ValueError(f"JeffEngine {self._id!r} declares no loader")
        self._model: Any = None
        self._max_options = self.default_max_options
        self._prompt_layout = "state-first"
        # Not "cpu": which device this engine gets depends on the machine, and answering
        # `/v1/models` must not import torch to find out (AGENTS.md §6).
        self._device = "unloaded"

    # --- declarative ---------------------------------------------------------

    def weights(self) -> WeightSpec:
        return WEIGHTS[self._id]

    def info(self) -> EngineInfo:
        return EngineInfo(
            id=self._id,
            version=REVISION,
            primitives=frozenset({"noul", "choice", "score"}),
            # The checkpoint's own trained maximum (254 for the Qwen model, 26 for
            # Gemma), re-read from `decision_config.json` on load. Not 255: the readout
            # has 255 rows, but "the head can express it" and "the model was trained on
            # it" are different claims and only the second one is worth advertising.
            max_options=self._max_options,
            max_sequence_tokens=MAX_SEQUENCE,
            # The question and the state share one sequence and upstream caps only their
            # sum, so this is the loosest *sound* bound and the sequence check is the one
            # that binds. Same reasoning as kev's `max_question_tokens`.
            max_question_tokens=MAX_SEQUENCE,
            # No separate state cap: upstream's only limit is the 8192-token sequence.
            max_state_tokens=0,
            languages="English",
            device=self._device,
            dtype=self._dtype(),
            description=self._description(),
            release_date=CHECKPOINT_DATE,
            aliases=self._aliases(),
        )

    def _description(self) -> str:
        raise NotImplementedError

    def _aliases(self) -> tuple[str, ...]:
        return ()

    def _dtype(self) -> str:
        if self._model is None:
            return "unloaded"
        return str(next(self._model.readout.parameters()).dtype).removeprefix("torch.")

    # --- lifecycle -----------------------------------------------------------

    def load(self, settings: Settings | None = None) -> None:
        """Load the fine-tuned backbone, the readout and the tokenizer, then warm up.

        `dtype` is **not** taken from `registry.DTYPE_DEFAULTS`: neither vendored class
        accepts one, and both choose `bfloat16` on an accelerator and `float32` on CPU.
        That is the same position Laya is in -- its own `Agent` decides precision -- so
        this engine consults neither `DECIS_DTYPE` nor the dtype table. `DECIS_DEVICE`
        *is* honoured: `device` is passed explicitly so upstream's `cuda if available
        else cpu` fallback is never reached (`AGENTS.md §9`).
        """
        if self._loaded:
            return
        if sys.version_info[:2] < MIN_PYTHON:
            # Before the dependency check and before the import below: on an older
            # interpreter the vendored package is a `SyntaxError`, which would surface as
            # "invalid syntax (types.py, line 8)" from three frames down.
            floor = ".".join(str(part) for part in MIN_PYTHON)
            running = ".".join(str(part) for part in sys.version_info[:3])
            raise EngineUnavailableError(
                f"The {self._id} engine needs Python {floor} or newer; this interpreter is "
                f"{running}. Its vendored serving code uses PEP 695 type aliases "
                f"(_jeff_vendor/types.py), which are a syntax error before 3.12. The project itself "
                f"still supports 3.11 for the other engines."
            )
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - exercised by the fast suite
            raise EngineUnavailableError(
                f"The {self._id} engine needs PyTorch, transformers, torchvision, safetensors "
                f"and pillow. Install them with: uv sync --extra jeff  ({exc})"
            ) from exc

        from ..config import load_settings
        from . import _jeff_vendor

        settings = settings if settings is not None else load_settings()
        spec = self.weights()
        source = resolve(spec, settings)
        if source.kind == "none":
            raise EngineUnavailableError(
                f"No weights found for {self._id}, and this engine has no repository to fetch them "
                f"from. Point DECIS_MODEL_DIR at a tree containing {self._id}/, or pass "
                f"--model-path {self._id}=<dir> for a directory containing {spec.marker}."
            )

        if source.is_local:
            directory = source.path
        else:
            _logger.info("fetching %s (%s) -- pinned at %s", spec.repo_id, spec.expected_bytes, spec.revision)
            directory = fetch_checkpoint(spec, settings)
        assert directory is not None  # `resolve` only returns "hub" for a downloadable spec

        # `requested_device` is `DECIS_DEVICE` when set and legal; unset asks the machine.
        device = requested_device(settings.device) or best_device()
        self._device = device
        # Before the load: the answer to "why is my GPU idle?" is worth more at the start
        # of a multi-second checkpoint load than after it.
        warn_if_accelerator_is_idle(device, pinned=settings.device is not None, logger=_logger)

        loader = getattr(_jeff_vendor, self.loader)
        # Upstream calls `torch.set_num_threads(cpu_threads)` in its constructor, so pass
        # the thread count Decis resolved rather than letting a default of 8 override
        # `DECIS_TORCH_THREADS` -- and pass the current value when Decis has no opinion,
        # which leaves it alone.
        threads = settings.torch_threads or torch.get_num_threads()
        self._model = loader(checkpoint=str(directory), device=device, cpu_threads=threads)

        # The checkpoint's own numbers replace the per-subclass fallbacks. `codes` is the
        # vocabulary the readout was trained against; a checkpoint whose tokenizer cannot
        # reproduce it has already failed inside the vendored loader.
        self._max_options = self._read_max_options(directory, len(self._model.codes))
        self._prompt_layout = getattr(self._model, "prompt_layout", "state-first")
        self._loaded = True
        _logger.info(
            "%s loaded on %s (%s), %d options, prompt layout %s",
            self._id,
            device,
            self._dtype(),
            self._max_options,
            self._prompt_layout,
        )
        self._warmup()

    @staticmethod
    def _read_max_options(directory: Path, vocabulary: int) -> int:
        """The option ceiling the checkpoint was trained on, from its own config.

        Falls back to the vocabulary size when a fine-tune omits the field, and is
        clamped to it so that an edited config cannot promise more options than there are
        answer codes.
        """
        config = json.loads((directory / "decision_config.json").read_text(encoding="utf-8"))
        declared = config.get("max_options")
        if declared is None:
            return vocabulary
        return max(1, min(int(declared), vocabulary))

    def _warmup(self) -> None:
        """One real `predict` before `/readyz` goes green.

        Built through `render.prepare_question` rather than by hand so that the warmup
        exercises the same compatibility layer a request does -- a hand-built
        `PreparedQuestion` would skip `raw` and only discover a broken row builder on the
        first caller.
        """
        from ..render import prepare_question
        from ..schema import NoulCriteria, NoulQuestion

        question = prepare_question(
            "warmup",
            NoulQuestion(
                type="noul",
                instructions="Does the text contain the word warmup?",
                criteria=NoulCriteria(false="It does not.", true="It does."),
            ),
        )
        self.predict(
            [
                WorkItem(
                    request_id="warmup",
                    state_text="warmup",
                    question=question,
                    raw_state="warmup",
                )
            ]
        )

    # --- the compatibility layer ---------------------------------------------

    def _state_of(self, item: WorkItem) -> Any:
        """The value upstream's `describe()` should serialise.

        `raw_state` is the caller's own JSON; `state_text` is the fallback for a
        synthetic item (a warmup, or a test that builds a `WorkItem` by hand), where the
        flattened text is the only state there is.
        """
        return item.state_text if item.raw_state is None else item.raw_state

    def _row(self, item: WorkItem) -> dict[str, Any]:
        """One upstream `DecisionInput`. Its `question` is the caller's values verbatim."""
        raw = item.question.raw
        if raw is None:
            # `render.prepare_question` fills `raw` for every primitive, so this means the
            # question was built directly -- an engine or test double, as kev's warmup does.
            # Loud, because the alternative is a prompt built from flattened text and a
            # quietly worse answer.
            raise ValueError(
                f"question {item.question.qid!r} carries no raw values; Jeff needs the caller's "
                "JSON, so it must be prepared by `render.prepare_request`"
            )
        return {"state": self._state_of(item), "question": raw}

    def _rows(self, items: list[WorkItem]) -> list[dict[str, Any]]:
        return [self._row(item) for item in items]

    # --- capacity ------------------------------------------------------------

    def measure(self, request: PreparedRequest) -> MeasuredTokens:
        """The real chat-templated token count, from the real processor.

        Upstream's `prepare` is the measurement: it applies the chat template, tokenises,
        and refuses rather than truncates past its limit. Re-deriving the count here
        would be a second implementation of a sequence format this module deliberately
        does not own.

        Two counts per question, because the two numbers `schema.validate_capacity`
        needs are genuinely different: the whole `state + question` sequence (what the
        model consumes) and the question's own head (the same prompt with the state
        blanked, which is what the state is added to). The difference is how much the
        state actually contributes -- tokenisation is not additive across the boundary,
        so measuring either alone and subtracting a guess would be wrong in the
        direction that lets an over-long request through.

        A question with more options than the checkpoint was trained on is skipped: the
        capacity check rejects it on `max_options` *after* this runs, and building its
        prompt would raise upstream's generic "1 to 255 options" error instead of a 422
        naming the question.
        """
        if self._model is None:
            return super().measure(request)

        heads: dict[str, int] = {}
        sequence = 0
        state_tokens = 0
        for question in request.questions:
            if question.raw is None or len(question.options) > self._max_options:
                continue
            row = {
                "state": request.state_text if request.raw_state is None else request.raw_state,
                "question": question.raw,
            }
            full = self._count([row])
            head = self._count([{**row, "state": ""}])
            heads[question.qid] = head
            sequence = max(sequence, full)
            state_tokens = max(state_tokens, full - head)
        return MeasuredTokens(state_tokens=state_tokens, head_tokens=heads, sequence_tokens=sequence)

    def _count(self, rows: list[dict[str, Any]]) -> int:
        # A generous `max_length` on purpose: this *is* the measurement, so it must be
        # allowed to report a number past the limit and let the capacity check turn it
        # into a 422. Letting `prepare` raise here would surface upstream's message
        # instead of one naming the state and the limit.
        return int(self._model.prepare(rows, max_length=1 << 30).input_tokens)

    # --- inference -----------------------------------------------------------

    def predict(self, items: list[WorkItem]) -> Prediction:
        if self._model is None:
            raise EngineUnavailableError(f"The {self._id} engine is not loaded.")

        rows = self._rows(items)
        # Upstream's own serving entry point produces the distributions, so the numerics
        # are the model's and not a re-derivation of them.
        distributions = self._model.predict(rows, batch_size=_BATCH_SIZE)
        probabilities: list[ProbDist] = [
            validate_distribution(
                values, len(item.question.options), context=f"{self._id} question {item.question.qid!r}"
            )
            for item, values in zip(items, distributions, strict=True)
        ]
        # `input_tokens` is a contract field, and upstream's `predict` does not return it.
        # Preparing the same chunks again costs one extra tokenisation pass and keeps the
        # probabilities coming from upstream rather than from a hand-written copy of its
        # softmax.
        input_tokens = sum(
            self._model.prepare(rows[start : start + _BATCH_SIZE]).input_tokens
            for start in range(0, len(rows), _BATCH_SIZE)
        )
        return Prediction(probabilities=probabilities, input_tokens=int(input_tokens))

    def close(self) -> None:
        self._model = None
        self._loaded = False
        self._device = "unloaded"


class JeffQwenEngine(JeffEngine):
    """`mstrasser/Jeff-Qwen3.5-0.8B`: the Qwen3.5 architecture, 254 trained options."""

    engine_id = "jeff-qwen3.5-0.8b"
    loader = "DecisionModel"
    default_max_options = 254

    def _description(self) -> str:
        return (
            "Jeff: a Qwen3.5-0.8B fine-tune with a 255-row single-token readout, one prefill pass, no text generation."
        )

    def _aliases(self) -> tuple[str, ...]:
        # `jeff` names the 0.8B: it is the smallest and fastest checkpoint, and a caller
        # who only wants "a Jeff" should not have to know the sizes.
        return ("jeff", "jeff-qwen", "jeff-qwen3.5")


class JeffGemmaEngine(JeffEngine):
    """`mstrasser/Jeff-Gemma4-E2B`: the generic decoder path, 26 trained options."""

    engine_id = "jeff-gemma4-e2b"
    loader = "GenericDecoderDecisionModel"
    default_max_options = 26

    def _description(self) -> str:
        return (
            "Jeff: a Gemma 4 E2B fine-tune with a 255-row single-token readout, one prefill pass, "
            "no text generation. Trained on up to 26 options."
        )

    def _aliases(self) -> tuple[str, ...]:
        return ("jeff-gemma", "jeff-gemma4")


__all__ = [
    "CHECKPOINT_DATE",
    "MAX_SEQUENCE",
    "REVISION",
    "WEIGHTS",
    "JeffEngine",
    "JeffGemmaEngine",
    "JeffQwenEngine",
]
