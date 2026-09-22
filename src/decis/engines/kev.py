"""kev: a decoder LM run prefill-only, with a pointer readout over option tokens.

kev is not a classifier head bolted onto a chat model. The backbone is a causal LM
whose hidden state at the `<decide>` token is compared against the hidden states of
the `<box_end>` token of every option, and the similarities become the distribution
(`_kev_vendor/model.py: PointerHead`). Nothing is generated, so the whole model is one
prefill pass -- which is why it belongs behind the same contract as Laya.

Two things about this engine are worth knowing before reading it.

**It runs vendored code, not the `kev` package.** kev is not on PyPI and its runtime
dependencies are a research project's. `_kev_vendor/` holds a pinned byte-identical
copy; its `VENDOR.md` explains what was taken and what was left out.

**The text kev sees is kev's, not Decis's.** `render.py` owns "any JSON to readable
text", but the *option text* is part of this engine's sequence format, and kev's own
convention differs from Decis's rendered option names:

    choice   "name: description"     -- the same as Decis
    noul     "no" / "yes"            -- *not* Decis's "false"/"true" wire keys
    score    the bare level text     -- *not* "0: <level>"

This is not cosmetic. kev's `data.py:395 materialize()` builds every *training* record
"via the serving path (`api.to_record`)", so the serving text is the text the model
learned on. Sending Decis's own names would hand the model strings it has never seen,
and the answer would degrade with no error anywhere -- exactly the failure mode
`AGENTS.md` exists to prevent. Measured: the record built here is field-for-field
equal to `api.to_record`'s, and the probabilities match upstream's own `model.probs()`
to 0.00e+00 (`tests/test_kev_inference.py`).
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain import MeasuredTokens, PreparedQuestion, PreparedRequest, ProbDist
from ..errors import EngineUnavailableError, InvalidRequestError
from ..paths import BaseModel, WeightSpec, resolve
from .base import DecisionEngine, EngineInfo, Prediction, WorkItem, validate_distribution

_logger = logging.getLogger(__name__)

#: kev's serving window, from `_kev_vendor/model.py:13`. Both limits are enforced, and
#: they are genuinely different: see `EngineInfo.max_state_tokens`.
MAX_STATE = 384
MAX_BRANCH = 1024

#: Every kev checkpoint answers all three primitives; the pointer readout is over
#: options, and `noul`/`score` are just option shapes (kev/api.py:94).
_PRIMITIVES = frozenset({"noul", "choice", "score"})

#: The vendored copy needs transformers 5 for `DynamicCache(config=...)` and for the
#: `dtype=` argument to `from_pretrained`; peft for the adapter. The combination that
#: is actually tested is recorded in `docs/design.md §7.2`.
_REQUIRES = ("torch", "transformers", "peft")


def _dtype_map() -> dict[str, Any]:
    """torch's dtypes, resolved lazily so importing this module needs no torch."""
    import torch

    return {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}


_QWEN_BASE = "Qwen/Qwen3.5-0.8B-Base"

#: A checkpoint's own repository holds only the adapter and the pointer head; the base
#: is a separate published model. See `paths.BaseModel`.
_BASE_08B = BaseModel(repo_id=_QWEN_BASE, revision="dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68")

#: Fetched by `decis download`. `tokenizer.json` is deliberately absent: `DecisionModel`
#: loads the tokenizer from the *base* (`checkpoint.py:132`), so pulling the adapter's
#: copy would be dead weight.
_ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors", "head.pt")

REVISION = "54f4f8777356cd5bbbb6c6919c657f26e6f2f6d8"
CHECKPOINT_DATE = "2026-09-21"

WEIGHTS: dict[str, WeightSpec] = {
    "kev-0.8b": WeightSpec(
        engine_id="kev-0.8b",
        repo_id="jaredpalmer/kev-0.8b",
        revision=REVISION,
        marker="head.pt",
        expected_bytes=13_000_000,
        license_name="Apache-2.0",
        license_url="https://github.com/jaredpalmer/kev",
        requires=_REQUIRES,
        checkpoint_files=_ADAPTER_FILES,
        bases=(_BASE_08B,),
    ),
}


class KevEngine(DecisionEngine):
    """Serves one kev checkpoint.

    One subclass per checkpoint, because `registry.create` builds engines with no
    arguments -- that is what keeps importing the registry free of torch.
    """

    engine_id: str = "kev-0.8b"

    def __init__(self) -> None:
        super().__init__()
        self._id = self.engine_id
        if self._id not in WEIGHTS:
            raise ValueError(f"KevEngine has no weights declared for {self._id!r}")
        self._tok: Any = None
        self._model: Any = None
        self._device = "cpu"

    # --- declarative ---------------------------------------------------------

    def weights(self) -> WeightSpec:
        return WEIGHTS[self._id]

    def info(self) -> EngineInfo:
        return EngineInfo(
            id=self._id,
            version=self._version(),
            primitives=_PRIMITIVES,
            # `encode` refuses a branch longer than the branch budget outright rather
            # than degrading, and MAX_OPTIONS upstream is 255 (kev/api.py:14).
            max_options=255,
            max_sequence_tokens=MAX_BRANCH,
            # The branch shares the sequence with the state, so the real per-question
            # allowance is `MAX_BRANCH - len(state)` -- a number that changes per
            # request and therefore cannot be advertised here. Reporting the tightest
            # possible value (`MAX_BRANCH - MAX_STATE`, i.e. assuming a full state)
            # would reject a 700-token question about a 100-token state, which kev
            # handles fine. So this is the loosest *sound* bound and the sequence check
            # is the one that actually binds; `measure` still reports each question's
            # own cost, so a 422 can name the question that caused it.
            max_question_tokens=MAX_BRANCH,
            max_state_tokens=MAX_STATE,
            languages="English (checkpoints are trained on English decision datasets)",
            device=self._device,
            dtype=self._dtype(),
            description="kev: Qwen3.5 + LoRA + pointer readout, one prefill pass, no text generation.",
            release_date=CHECKPOINT_DATE,
            aliases=(),
        )

    def _version(self) -> str:
        # The upstream package version, as Laya reports its own: what a caller needs to
        # reproduce an answer is the code that ran the forward pass plus the pinned
        # checkpoint revision, which `decis doctor` reports.
        return "vendored-90990a5"

    def _dtype(self) -> str:
        if self._model is None:
            return "unloaded"
        return str(next(self._model.lm.parameters()).dtype).removeprefix("torch.")

    # --- lifecycle -----------------------------------------------------------

    def load(self) -> None:
        """Load the adapter, its base and the pointer head, then warm up.

        Warmup matters more here than for Laya: the first pass through a hybrid
        (Gated DeltaNet) backbone compiles and caches per-shape kernels, and the
        vendored `DecisionModel.SHAPE_BUCKET` exists precisely because that cost is
        large enough to be worth padding for on MPS. Doing it in `load()` means the
        first real request gets the steady-state latency and `/readyz` turns green
        only once that is true.
        """
        if self._loaded:
            return
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - exercised by the fast suite
            raise EngineUnavailableError(
                f"The {self._id} engine needs PyTorch, transformers and peft. "
                f"Install them with: uv sync --extra kev  ({exc})"
            ) from exc

        from ..config import load_settings
        from ._kev_vendor import Checkpoint

        settings = load_settings()
        source = resolve(self.weights(), settings)
        if source.kind == "none":
            raise EngineUnavailableError(
                f"No weights found for {self._id}, and this engine has no repository to fetch "
                f"them from. Set DECIS_MODEL_DIR or DECIS_MODEL_PATH_"
                f"{self._id.upper().replace('-', '_')} to a directory containing "
                f"{self.weights().marker}."
            )

        if settings.torch_threads:
            torch.set_num_threads(settings.torch_threads)

        device = settings.device or "cpu"
        self._device = device
        # The adapter is resolved by `paths.resolve`, so a mounted directory works with
        # no network. Upstream's own `resolve_run` would reach for the Hub here.
        target = str(source.path) if source.is_local else f"{source.repo_id}@{source.revision}"
        _logger.info("loading %s from %s (device=%s)", self._id, source.describe(), device)

        checkpoint = Checkpoint(target)
        meta = checkpoint.meta
        if meta.option_isolation:
            # `DecisionModel` raises on this combination itself, but naming the
            # checkpoint and the reason is worth more than upstream's message.
            raise EngineUnavailableError(
                f"{self._id} was trained with option_isolation, which the packed block-causal "
                f"mask provides and a hybrid backbone cannot. This checkpoint cannot be served "
                f"by this engine."
            )
        _logger.info(
            "%s: base=%s@%s head_dim=%d temperature=%.4f lora=%d",
            self._id,
            meta.base,
            meta.base_revision,
            meta.head_dim,
            meta.temperature,
            meta.lora,
        )

        # dtype comes from Decis's own table rather than the environment: bf16 on CPU
        # measured ~83x slower for kev (docs/feasibility.md §4). An explicit
        # DECIS_DTYPE still wins, but a known-bad combination says so out loud instead
        # of leaving someone to wonder why their service got 80x slower.
        from ..engines.registry import default_dtype, degraded_reason

        chosen = settings.dtype or default_dtype(self._id, device)
        dtypes = _dtype_map()
        if chosen not in dtypes:
            raise EngineUnavailableError(
                f"DECIS_DTYPE={chosen!r} is not a dtype. Use one of: {', '.join(sorted(dtypes))}."
            )
        warning = degraded_reason(self._id, device, chosen)
        if warning:
            _logger.warning("%s on %s in %s: %s", self._id, device, chosen, warning)
        dtype = dtypes[chosen]
        # `merge=True` in fp32 is exact and is what upstream's reported numbers use
        # (`checkpoint.py:79-83`). `Checkpoint.load` overrides both when the checkpoint
        # says its own weights are bf16, which is the case for the large MoE bases.
        options = self._vendor_load_options(dtype)
        self._tok, self._model = checkpoint.load(device, options)
        self._warmup()
        self._loaded = True
        _logger.info(
            "loaded %s (device=%s dtype=%s hybrid=%s max_state=%d max_branch=%d)",
            self._id,
            device,
            self._dtype(),
            self._model.hybrid,
            MAX_STATE,
            MAX_BRANCH,
        )

    def _vendor_load_options(self, dtype: Any) -> Any:
        """A `LoadOptions` built explicitly rather than from `KEV_*` environment variables.

        `LoadOptions.from_env()` exists upstream for its command-line tools. Decis
        reads configuration through `config.py` only (`AGENTS.md §2`), so the values
        are passed in. `temperature=None` keeps the checkpoint's own fitted
        calibration, which is part of the model rather than a decoration.
        """
        from ._kev_vendor import LoadOptions

        return LoadOptions(dtype=dtype, merge=True)

    def _warmup(self) -> None:
        import torch

        from ..render import noul_options
        from .base import WorkItem as _WorkItem

        item = _WorkItem(
            request_id="warmup",
            state_text="warmup",
            question=PreparedQuestion(
                qid="warmup",
                type="noul",
                instructions="warmup",
                options=noul_options(),
            ),
        )
        with torch.no_grad():
            self.predict([item])

    def close(self) -> None:
        self._tok = None
        self._model = None
        self._loaded = False

    # --- measurement ---------------------------------------------------------

    def measure(self, request: PreparedRequest) -> MeasuredTokens:
        """What this checkpoint will really consume, in its own tokenizer's tokens.

        Uses upstream's own `encode(strict=True)`, which is the function that decides
        truncation, rather than re-deriving its arithmetic. `strict=True` raises
        instead of cutting, so the measurement cannot drift from the engine's real
        behaviour: if this method's numbers were wrong, `encode` would still refuse and
        the request would fail loudly at prediction time.

        The two limits are reported separately because they are separate: `encode`
        caps the state at `MAX_STATE` and the state-plus-branch at `MAX_BRANCH`
        (`_kev_vendor/model.py:44,54`). `state_tokens` rides the first,
        `sequence_tokens` the second.
        """
        if self._model is None:
            return super().measure(request)

        # The *true* state length, deliberately not read back out of `encode`: `encode`
        # clamps the state to its budget and reports what is left in `state_truncated`,
        # so a measurement derived from its output can never exceed the cap and the cap
        # check would never fire. (That was a real bug here: `min(state, MAX_STATE)`
        # pinned the report at exactly 384 for every over-long state.)
        state_tokens = 1 + len(self._tok(request.state_text, add_special_tokens=False)["input_ids"])
        head_tokens = {question.qid: self._branch_tokens(question) for question in request.questions}
        longest = max(head_tokens.values(), default=0)
        return MeasuredTokens(
            state_tokens=state_tokens,
            head_tokens=head_tokens,
            sequence_tokens=state_tokens + longest,
        )

    def _branch_tokens(self, question: PreparedQuestion) -> int:
        """One question's own tokens: instructions, every option span, `<decide>`.

        Measured by encoding it against an *empty* state, because the branch tokens do
        not depend on the state (`encode` builds the instruction and option spans from
        the question alone), and an empty state keeps `encode`'s own state clamp out of
        the arithmetic. The one token subtracted is the `<|fim_prefix|>` delimiter that
        an empty state still contributes.
        """
        empty = self._model.encode(self._tok, {"state": "", "questions": [self._question(question)]})
        return len(empty["ids"]) - 1

    # --- inference -----------------------------------------------------------

    def predict(self, items: list[WorkItem]) -> Prediction:
        if self._model is None:
            raise EngineUnavailableError(f"The {self._id} engine is not loaded.")

        import torch

        if not items:
            return Prediction(probabilities=[])

        records = [self._record(item.state_text, item.question) for item in items]
        try:
            encodings = [self._model.encode(self._tok, record) for record in records]
        except ValueError as exc:
            # `encode` refuses a state over 384 tokens or a branch that will not fit
            # beside it. `measure()` should have rejected this before it got here, so
            # reaching it means a measurement bug rather than a bad request -- but the
            # message still has to be actionable rather than an upstream traceback.
            raise InvalidRequestError(
                f"A question could not be encoded for {self._id} within its "
                f"{MAX_STATE}-token state / {MAX_BRANCH}-token sequence budget. "
                f"Shorten the `state`, the instructions or the criteria. ({exc})",
                loc=["body"],
                type="too_long",
            ) from exc

        with torch.no_grad():
            try:
                # One padded forward pass for *every* item, regardless of state: the
                # batch is across requests, which is the whole point of the scheduler
                # handing the engine a list (`AGENTS.md §3-18`).
                logits = self._model.forward_batch(encodings)
            except (RuntimeError, torch.cuda.OutOfMemoryError) as exc:
                raise EngineUnavailableError(
                    f"The {self._id} engine could not run a batch of {len(items)} questions. "
                    f"Retry with fewer questions per call. ({exc})"
                ) from exc

        probabilities: list[ProbDist] = []
        for item, per_question in zip(items, logits, strict=True):
            # `forward_batch` returns raw logits (unlike the prefix-cache path, which
            # softmaxes internally). `PointerHead` has already divided by the
            # checkpoint's fitted temperature in eval mode, so this is calibrated.
            distribution = torch.softmax(per_question[0], -1)
            probabilities.append(
                validate_distribution(
                    [float(value) for value in distribution.tolist()],
                    len(item.question.options),
                    context=f"{self._id} question {item.question.qid!r}",
                )
            )

        # The real count: what `_pad_rows` fed the backbone, matching what upstream's
        # own `serve` bills for the same work.
        input_tokens = sum(len(enc["ids"]) for enc in encodings)
        return Prediction(probabilities=probabilities, input_tokens=input_tokens)

    # --- normalisation -------------------------------------------------------

    def _record(self, state_text: str, question: PreparedQuestion) -> dict[str, Any]:
        """A Decis-rendered question in kev's record shape.

        Option-for-option this mirrors `kev/api.py:to_record` (line 102), including the
        parts that differ from Decis's own option naming -- see the module docstring.
        `label` is required by `encode` and unused at inference, so it is 0, exactly as
        upstream's serving path sets it.
        """
        return {"state": state_text, "questions": [self._question(question)]}

    def _question(self, question: PreparedQuestion) -> dict[str, Any]:
        if question.type == "choice":
            # Decis names a choice option with its criterion key and renders the value
            # into the description, which is exactly kev's "name: description".
            options = [_option_text(option.name, option.description) for option in question.options]
        elif question.type == "noul":
            # kev's option texts, not Decis's "false"/"true" wire keys. The keys are
            # unaffected: they are `answers.py`'s business and stay "false"/"true".
            options = [
                _option_text(name, option.description)
                for name, option in zip(("no", "yes"), question.options, strict=True)
            ]
        else:
            # A score's levels are the bare rendered text; upstream adds no index prefix.
            options = [option.description for option in question.options]
        return {"instr": question.instructions, "options": options, "label": 0}


def _option_text(name: str, description: str) -> str:
    """`kev/api.py:58` in behaviour: a bare name when the description is empty.

    Reimplemented rather than imported because `api.py` is deliberately not vendored
    (it also carries answer construction and confidence, which are `answers.py`'s job
    here). `tests/test_kev_inference.py` pins it against upstream's own function.
    """
    return name if not description else f"{name}: {description}"


__all__ = ["CHECKPOINT_DATE", "MAX_BRANCH", "MAX_STATE", "REVISION", "WEIGHTS", "KevEngine"]
