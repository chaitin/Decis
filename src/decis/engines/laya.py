"""Laya: a non-autoregressive, multilingual System 1 decision model.

Laya is the primary engine because it is fast on CPU, genuinely multilingual, and
answers `choice` / `score` / `noul` in a single forward pass.

## Why this file drives Laya's primitives instead of calling `Agent.system_one`

`Agent.system_one(state, questions)` takes **one state for the whole call**. Decis
schedules a flat list of `WorkItem`s, each carrying its own `state_text`, because
grouping a batch by state would pin the batch size at 1 under real traffic and
cross-request batching would never trigger at all (AGENTS.md §3-18).

Laya's own primitives compose across states without copying or trickery:
`collate_items` flattens a list of *groups* (`[it for group in batch for it in group]`,
`laya/common.py:248`), so `collate_items([[item] for item in items], pad)` batches
items that share nothing. `build_sequence` writes the state into every row, so a row
needs no knowledge of its neighbours. That is the whole mechanism.

Everything else is Laya's own code path, deliberately: `build_sequence` and
`render_options` decide the sequence layout, including how options are decorated and
budgeted, and reimplementing that here would be both a copy of upstream internals and
a guaranteed drift. `tests/test_upstream_contract.py` pins the symbols relied on.

## The part upstream cannot tell us

`build_sequence` truncates the state, the instructions and the options to fit its
budgets, and **signals none of it**. A silently truncated decision prompt produces a
confident wrong answer, which `AGENTS.md §5-4` forbids. So `measure()` computes what
the head and the state will really cost, and `schema.validate_capacity` turns an
over-budget request into a 422 before the model runs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..domain import MeasuredTokens, PreparedQuestion, PreparedRequest, ProbDist
from ..errors import EngineUnavailableError, InvalidRequestError
from ..paths import WeightSpec, resolve
from ..render import noul_options
from .base import DecisionEngine, EngineInfo, Prediction, WorkItem, validate_distribution
from .registry import DEVICES

if TYPE_CHECKING:  # pragma: no cover - types only, never imported at runtime
    from ..config import Settings

_logger = logging.getLogger("decis.engines.laya")

#: Fallbacks for a checkpoint whose config omits its budgets. These are Laya's own
#: defaults (`laya/agent.py:262-263`), used only before `load()`, and always replaced
#: by the checkpoint's real values afterwards -- a capacity check against invented
#: numbers is worse than no check.
DEFAULT_MAX_LEN = 512
DEFAULT_HEAD_MAX_LEN = 192

#: Laya truncates each option to 48 tokens (`laya/common.py:64`). Mirrored so that
#: `measure` does not over-report a long criterion and reject a request Laya would
#: have answered correctly.
_OPTION_TOKEN_CAP = 48

#: What the head can physically hold at the 48-token option cap. An honest ceiling
#: rather than an aspirational one: a `choice` with more options than this cannot fit
#: inside `head_max_len` and is rejected by the question budget first.
_MAX_OPTIONS = 255

#: Separators and `[CLS]` framing a whole sequence: `[CLS] <head> [SEP] <options>
#: [SEP] <state> [SEP]` (`laya/common.py:71-77`). The per-option `[MASK]` is counted
#: with its option, because that is where `build_sequence` puts it.
_SEQUENCE_SKELETON = 4

#: `build_sequence` truncates the options once their budget drops below this
#: (`laya/common.py:66-69`), and truncates the instructions to whatever the option
#: budget leaves (`laya/common.py:70`). Both conditions collapse into
#: `budgeted_head()` below -- verified against the real `build_sequence` rather than
#: trusted, in `tests/test_upstream_contract.py`.
_MIN_OPTION_BUDGET = 16

#: The commit this repository pins. A tag or branch would let an upstream force-push
#: change what a given Decis image loads, which makes a release unreproducible. A
#: local checkpoint mounted with `DECIS_MODEL_PATH_*` is the escape hatch.
REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"

#: The date of the revision above, from the Hub's own metadata. It is the checkpoint's
#: `lastModified`, not a marketing release date -- `GET /v1/models` needs *a* date and
#: this is the only one that is verifiable.
CHECKPOINT_DATE = "2026-09-20"

_LICENSE_URL = "https://huggingface.co/convaiinnovations/laya"

#: Sizes are the total of the files `decis download` fetches, measured from the Hub's
#: file listing (`?recursive=true`), so `decis doctor` can warn about disk space
#: before a 800 MiB download rather than after.
#: The importable package every Laya checkpoint needs. Declared once so a checkpoint
#: added later cannot silently omit it and make `decis models` overstate readiness.
_REQUIRES = ("laya",)

_ROOT = WeightSpec(
    engine_id="laya",
    repo_id="convaiinnovations/laya",
    subfolder=None,
    revision=REVISION,
    marker="rl_agent_config.json",
    expected_bytes=846_195_574,
    license_name="Apache-2.0",
    license_url=_LICENSE_URL,
    requires=_REQUIRES,
)

WEIGHTS: dict[str, WeightSpec] = {
    "laya": _ROOT,
    "laya-multilingual": WeightSpec(
        engine_id="laya-multilingual",
        repo_id="convaiinnovations/laya",
        subfolder="multilingual",
        revision=REVISION,
        marker="rl_agent_config.json",
        expected_bytes=678_201_636,
        license_name="Apache-2.0",
        license_url=_LICENSE_URL,
        requires=_REQUIRES,
    ),
    "laya-typed-decisions": WeightSpec(
        engine_id="laya-typed-decisions",
        repo_id="convaiinnovations/laya",
        subfolder="typed-decisions",
        revision=REVISION,
        marker="rl_agent_config.json",
        expected_bytes=846_195_716,
        license_name="Apache-2.0",
        license_url=_LICENSE_URL,
        requires=_REQUIRES,
    ),
}


def budgeted_head(instruction_tokens: int, option_tokens: int) -> int:
    """The head cost as `build_sequence` budgets it, for one comparison against `head_max_len`.

    Upstream applies two separate limits and signals neither
    (`laya/common.py:66-70`):

    1. options are truncated when their budget `head_max_len - option_tokens` falls
       below 16;
    2. instructions are truncated to `max(8, head_max_len - option_tokens)`.

    Both collapse to this expression. Given `H = head_max_len`, `A = instruction_tokens`
    and `O = option_tokens`:

    * when ``A >= 16`` the second limit is `A + O <= H`, which already forces
      `O <= H - 16`, so the first is implied;
    * when ``A < 16`` the first limit `O <= H - 16` is the binding one, and it implies
      `A <= 16 <= H - O`, satisfying the second.

    So `O + max(A, 16) <= H` is *exactly* "nothing in this head will be truncated",
    which is what makes a single comparison in `schema.validate_capacity` sufficient.
    Verified against the real `build_sequence` over a grid of shapes in
    `tests/test_upstream_contract.py`.
    """
    return option_tokens + max(instruction_tokens, _MIN_OPTION_BUDGET)


def requested_device(name: str | None) -> str | None:
    """`DECIS_DEVICE`, checked, or `None` to let Laya choose.

    Upstream's own order is CUDA, then Metal, then CPU (`laya/agent.py:177-182`), and on
    an Apple-silicon Mac that means the GPU -- while the same image in a Linux container
    has no Metal and serves from the CPU. The gap between those two is large enough to
    look like a container problem, so pinning `cpu` has to actually pin it
    (`docs/performance.md`).

    A typo is refused here rather than handed to `torch.device`, which would fail inside
    `Agent.__init__` with a message that never names the variable.
    """
    if name is None or name in DEVICES:
        return name
    raise EngineUnavailableError(f"DECIS_DEVICE={name!r} is not a device. Use one of: {', '.join(sorted(DEVICES))}.")


class LayaEngine(DecisionEngine):
    """Serves one Laya checkpoint.

    One subclass per checkpoint rather than one class taking an id, because
    `registry.create` builds engines with no arguments -- that is what keeps
    importing the registry free of torch.
    """

    #: Set by each subclass. The registry path is `decis.engines.laya:LayaEngine`
    #: for `laya`, and each subclass overrides `_id`.
    engine_id: str = "laya"

    def __init__(self) -> None:
        super().__init__()
        self._id = self.engine_id
        if self._id not in WEIGHTS:
            raise ValueError(f"LayaEngine has no weights declared for {self._id!r}")
        self._agent: Any = None
        self._max_len = DEFAULT_MAX_LEN
        self._head_max_len = DEFAULT_HEAD_MAX_LEN

    # --- declarative ---------------------------------------------------------

    def weights(self) -> WeightSpec:
        return WEIGHTS[self._id]

    def info(self) -> EngineInfo:
        return EngineInfo(
            id=self._id,
            version=self._version(),
            primitives=frozenset({"noul", "choice", "score"}),
            max_options=_MAX_OPTIONS,
            # The head (instructions + options) and the state share one sequence, so
            # the context limit is on their sum. Reporting the two separately is what
            # lets the 422 name whichever one is actually at fault.
            max_sequence_tokens=self._max_len,
            max_question_tokens=self._head_max_len,
            languages="100+ languages" if "multilingual" in self._id else "English",
            device=self._device(),
            dtype=self._dtype(),
            description=(f"Laya {self._subfolder_description()}; one forward pass per call, no text generation."),
            release_date=CHECKPOINT_DATE,
            aliases=(),
        )

    def _subfolder_description(self) -> str:
        subfolder = self.weights().subfolder
        return f"({subfolder}) decision model" if subfolder else "(English) decision model"

    def _version(self) -> str:
        # The `laya` package version, not the checkpoint's -- the contract's `model`
        # field is `decis/laya-multilingual@<version>`, and what a caller needs to be
        # able to reproduce is the library that ran the forward pass. The checkpoint
        # revision is reported separately by `decis doctor`.
        try:
            import importlib.metadata

            return importlib.metadata.version("laya")
        except Exception:
            # Not installed. The id is still listable -- that is the entire point of
            # lazy engine registration -- so say so instead of inventing a version.
            return "not-installed"

    def _device(self) -> str:
        return "unloaded" if self._agent is None else str(self._agent.device.type)

    def _dtype(self) -> str:
        return "unloaded" if self._agent is None else str(self._agent.dtype).removeprefix("torch.")

    # --- lifecycle -----------------------------------------------------------

    def load(self) -> None:
        """Load the checkpoint and run one warmup pass.

        Warmup is not optional. The first forward pass pays for lazy kernel selection
        and allocator growth, which on CPU is several times the steady state; doing it
        here means the first *real* request gets the latency the benchmarks report, and
        `/readyz` turns green only once that is true.
        """
        if self._loaded:
            return
        try:
            import laya
            import torch
        except ImportError as exc:
            raise EngineUnavailableError(
                f"The {self._id} engine needs PyTorch and the `laya` package. "
                f"Install them with: uv sync --extra laya  ({exc})"
            ) from exc

        from ..config import load_settings

        settings: Settings = load_settings()
        source = resolve(self.weights(), settings)
        if source.kind == "none":
            raise EngineUnavailableError(
                f"No weights found for {self._id}, and this engine has no repository to fetch them "
                f"from. Set DECIS_MODEL_DIR or DECIS_MODEL_PATH_{self._id.upper().replace('-', '_')} to a "
                f"directory containing {self.weights().marker}."
            )

        if settings.torch_threads:
            # Left at torch's default when unset: the sweep in benchmarks/results/
            # shows the best thread count depends on the batch size, so pinning a
            # number here would be a claim this code cannot back.
            torch.set_num_threads(settings.torch_threads)

        # `None` means "let Laya pick", which is upstream's own default. On an
        # Apple-silicon Mac that pick is `mps`, and a Linux container has no Metal at
        # all -- the same release on the same machine therefore serves from a different
        # device depending on how it was started. `DECIS_DEVICE=cpu` is what makes the
        # two comparable, so the knob has to reach the `Agent` (docs/performance.md).
        device = requested_device(settings.device)
        _logger.info("loading %s from %s (device=%s)", self._id, source.describe(), device or "auto")
        self._agent = self._instantiate(laya, source, device)
        if device and self._agent.device.type != device:
            # Upstream falls back on its own, with a `print` that a server's log may not
            # keep. Say it where an operator will actually see it.
            _logger.warning(
                "DECIS_DEVICE=%s was requested but %s loaded on %s: that device is not available "
                "here (a Linux container has no MPS, and no CUDA without a GPU runtime).",
                device,
                self._id,
                self._agent.device.type,
            )
        config = self._agent.cfg
        self._max_len = int(config.get("max_len", DEFAULT_MAX_LEN))
        self._head_max_len = int(config.get("head_max_len", DEFAULT_HEAD_MAX_LEN))
        self._warmup()
        self._loaded = True
        _logger.info(
            "loaded %s (device=%s dtype=%s threads=%d max_len=%d head_max_len=%d)",
            self._id,
            self._device(),
            self._dtype(),
            torch.get_num_threads(),
            self._max_len,
            self._head_max_len,
        )

    def _instantiate(self, laya: Any, source: Any, device: str | None) -> Any:
        """Build the Agent against whatever `paths.resolve` decided.

        `laya.load` would re-derive all of this from a repository id and reach for the
        network; going through `Agent` directly is what makes a mounted directory work
        offline. It is the same class `laya.load` returns, and `laya.load` is a
        one-line wrapper around it (`laya/agent.py:378-385`).

        `device=None` is upstream's own signature default, so "no `DECIS_DEVICE`" and
        "asked for the best available" stay one code path.
        """
        try:
            if source.kind == "local":
                # `checkpoint_root` already resolved the subfolder, so passing it
                # again here would look for `<path>/<subfolder>/<subfolder>`.
                return laya.Agent(str(source.path), device=device)
            return laya.Agent(str(source.repo_id), device=device, subfolder=source.subfolder)
        except FileNotFoundError as exc:
            raise EngineUnavailableError(f"Could not load {self._id} from {source.describe()}: {exc}") from exc

    def _warmup(self) -> None:
        """One tiny forward pass, so the first real request is not the slow one.

        Routed through the real renderer rather than hand-building a
        `PreparedQuestion`: the option names are `render.py`'s to define
        (`AGENTS.md §2`), and a warmup that assembled its own would be a second
        construction site for them -- and would stop exercising the real path.
        """
        warmup = PreparedQuestion(
            qid="warmup",
            type="noul",
            instructions="Does this text contain the word warmup?",
            options=noul_options("no", "yes"),
        )
        try:
            self.predict([WorkItem(request_id="warmup", state_text="warmup", question=warmup)])
        except Exception:
            # A failed warmup must not be swallowed: the engine is unusable, and
            # failing startup beats failing every request afterwards.
            _logger.exception("%s failed its warmup pass", self._id)
            raise

    def close(self) -> None:
        self._agent = None
        self._loaded = False

    # --- capacity ------------------------------------------------------------

    def measure(self, request: PreparedRequest) -> MeasuredTokens:
        """What this checkpoint will really consume, in its own tokenizer's tokens.

        Reproduces the two numbers `build_sequence` uses to decide truncation
        (`laya/common.py:49-77`):

        * the **head** -- `[CLS]`, the type-worded instructions, `[SEP]`, then one
          `[MASK]`-led run per option, each capped at 48 tokens;
        * the **sequence** -- the head plus whatever state fits in `max_len`.

        Option text goes through upstream's own public `render_options`, so the
        `"name: description"` decoration and the `"level N: "` prefix are counted as
        Laya will actually emit them. Counting `PreparedQuestion.text()` instead is
        precisely the bug this method exists to prevent: it omits those prefixes, the
        `[MASK]` tokens and the separators, so it under-reports and lets through a
        request the engine then truncates in silence.
        """
        if self._agent is None:
            return super().measure(request)

        tokenizer = self._agent.tok
        costs = {question.qid: self._head_cost(question) for question in request.questions}
        state = len(tokenizer(request.state_text, add_special_tokens=False)["input_ids"])
        # The state only gets the room the largest head leaves behind
        # (`room = max_len - len(ids) - 1`, `laya/common.py:75`). Reporting the raw
        # state length would let a request pass when the head is what pushed it over --
        # and `build_sequence` then cuts the state without saying so.
        return MeasuredTokens(
            state_tokens=state,
            head_tokens={qid: budgeted_head(*cost) for qid, cost in costs.items()},
            sequence_tokens=max((a + o for a, o in costs.values()), default=0) + state + _SEQUENCE_SKELETON,
        )

    def _head_cost(self, question: PreparedQuestion) -> tuple[int, int]:
        """`(instruction_tokens, option_tokens)` for one question.

        The two terms upstream budgets separately: `len(head_ids)` and
        `sum(len(o) for o in opt_ids)`, with each `[MASK]` counted alongside its option
        because that is how upstream builds `opt_ids` (`laya/common.py:63-65`).

        Kept as a pair rather than a sum so the comparisons in `measure` can be about
        what upstream actually compares, and so the arithmetic is testable without
        torch (`tests/test_engines_laya.py`).
        """
        internal = self._internal(question)
        # `build_sequence` renders the head as "<type> question: <instructions>".
        instructions = self._agent.tok(f"{internal['t']} question: {internal['ins']}", add_special_tokens=False)[
            "input_ids"
        ]
        options = sum(1 + self._option_tokens(text) for text in self._options(internal))
        return len(instructions), options

    def _option_tokens(self, text: str) -> int:
        """Tokens one option costs, capped the way `build_sequence` caps it."""
        ids = self._agent.tok(" " + text, add_special_tokens=False)["input_ids"]
        return min(len(ids), _OPTION_TOKEN_CAP)

    def _options(self, internal: dict[str, Any]) -> list[str]:
        """Option texts exactly as Laya will render them, via upstream's renderer.

        Upstream's renderer rather than ours, because this is a *measurement of what
        Laya will emit*: `render_options` adds `"name: "` to each choice option (from
        the criterion key, which our own rendering never sees) and `"level N: "` to
        each score level. Measuring our own rendering instead would under-count by
        exactly those tokens, which is the bug this whole method exists to prevent.
        """
        from laya.common import render_options

        return render_options(internal)

    # --- inference -----------------------------------------------------------

    def predict(self, items: list[WorkItem]) -> Prediction:
        if self._agent is None:
            raise EngineUnavailableError(f"The {self._id} engine is not loaded.")

        agent = self._agent
        # Imported here rather than at module scope: this module must stay importable
        # in order to *list* the engine inside a container that has no torch.
        import numpy as np
        import torch
        from laya.common import (
            QTYPES,
            build_sequence,
            collate_items,
            confidence_from_probs,
            render_options,
            temp_bucket,
        )

        prepared: list[dict[str, Any]] = []
        internals: list[dict[str, Any]] = []
        for item in items:
            internal = self._internal(item.question)
            sequence, markers = build_sequence(agent.tok, item.state_text, internal, self._max_len, self._head_max_len)
            option_count = len(render_options(internal))
            if len(markers) != option_count:
                # Upstream raises a similar error inside `system_one`; raising here
                # attaches the question id and the budget, which its message does not.
                # `measure()` should have caught this first, so reaching it means a
                # measurement bug rather than a bad request.
                raise InvalidRequestError(
                    f"Question {item.question.qid!r} did not fit in this model's "
                    f"{self._head_max_len}-token question budget. Shorten the instructions or the "
                    f"criteria descriptions.",
                    loc=["body", "questions", item.question.qid],
                    type="too_long",
                )
            internals.append(internal)
            prepared.append({"ids": sequence, "markers": markers, "qtype": QTYPES[internal["t"]]})

        # One item per group: `collate_items` flattens groups, so this *is* the
        # cross-state batch. See the module docstring.
        batch = collate_items([[entry] for entry in prepared], agent.tok.pad_token_id)
        if batch is None:  # pragma: no cover - an empty request is rejected earlier
            raise InvalidRequestError("No questions to answer.", loc=["body", "questions"])

        # Matches `Agent.system_one` exactly (`laya/agent.py:295-300`): autocast only
        # on CUDA, and the checkpoint's own dtype. Deviating here would make these
        # probabilities differ from the model's own published quality numbers.
        use_amp = agent.device.type == "cuda"
        try:
            with torch.no_grad(), torch.autocast(device_type=agent.device.type, dtype=agent.dtype, enabled=use_amp):
                logits, _action = agent.model(
                    batch["input_ids"].to(agent.device),
                    batch["attention_mask"].to(agent.device),
                    batch["marker_pos"].to(agent.device),
                    batch["marker_mask"].to(agent.device),
                    batch["qtype"].to(agent.device),
                )
        except (RuntimeError, torch.cuda.OutOfMemoryError) as exc:
            # Upstream silently moves the model to CPU here. On a server that would
            # mutate global state on the request path (making a "pure function" POST
            # change every later request's latency by an order of magnitude), so this
            # fails loudly instead and lets the caller retry smaller.
            raise EngineUnavailableError(
                f"The {self._id} engine ran out of memory on a batch of {len(prepared)} questions. "
                f"Retry with fewer questions per call. ({exc})"
            ) from exc

        logits = logits.float().cpu().numpy()

        probabilities: list[ProbDist] = []
        native: list[float] = []
        for row, item in enumerate(items):
            internal = internals[row]
            count = len(prepared[row]["markers"])
            # Laya's checkpoint ships per-(question type, option count) fitted
            # temperatures and clamps out-of-range ones (`laya/common.py:219-241`).
            # Applying them is part of the model, not a decoration: they are what
            # makes the published probabilities calibrated.
            question_type = QTYPES[internal["t"]]
            temperature = agent.temperature_by_options.get(
                temp_bucket(question_type, count), agent.temperature[question_type]
            )
            scaled = logits[row, :count] / float(temperature)
            shifted = np.exp(scaled - scaled.max())
            distribution = shifted / shifted.sum()
            probabilities.append(
                validate_distribution(
                    [float(value) for value in distribution],
                    len(item.question.options),
                    context=f"{self._id} question {item.question.qid!r}",
                )
            )
            native.append(float(confidence_from_probs(distribution, count)))

        # The true count, not an estimate: upstream reports the same quantity as
        # `int(b["attention_mask"].sum())`, which is exactly the work the encoder did.
        return Prediction(
            probabilities=probabilities,
            input_tokens=int(batch["attention_mask"].sum()),
            native_confidences=native,
        )

    # --- normalisation -------------------------------------------------------

    def _internal(self, question: PreparedQuestion) -> dict[str, Any]:
        """A Decis-rendered question, in the shape Laya's primitives expect.

        Built from `PreparedQuestion` rather than upstream's `Agent._to_internal`,
        which reads the raw wire dict and therefore requires an `instructions` key
        that the OpenAPI makes optional (`laya/agent.py:255`).

        For `choice` the option *name* is passed as the key, because Laya renders it
        into the prompt (`"name: description"`) -- the one place a wire key is
        legitimately model-visible. For `score` Laya labels the levels itself
        (`"level 0: ..."`), so only the descriptions are passed.
        """
        if question.type == "score":
            criteria: Any = [option.description for option in question.options]
        else:
            # `None` (not `""`) means "no description": upstream then renders the bare
            # name, which is the same meaning Decis' own renderer gives an empty one.
            criteria = {option.name: (option.description or None) for option in question.options}
        return {"t": question.type, "ins": question.instructions, "crit": criteria}


class LayaMultilingualEngine(LayaEngine):
    engine_id = "laya-multilingual"


class LayaTypedDecisionsEngine(LayaEngine):
    engine_id = "laya-typed-decisions"


__all__ = [
    "CHECKPOINT_DATE",
    "REVISION",
    "WEIGHTS",
    "LayaEngine",
    "LayaMultilingualEngine",
    "LayaTypedDecisionsEngine",
]
