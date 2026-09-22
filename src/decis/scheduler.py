"""Running work on an engine.

Stage 0 keeps this deliberately thin: one engine instance, one lock, one call.
Cross-request batching -- joining several requests' questions into a single
forward pass -- is the thing this layer exists to grow into, and it arrives in
Stage 3 with the measurements that justify its complexity (docs/design.md §6).

The lock is not optional. A `torch.nn.Module` is not re-entrant, and some
backends behave unpredictably under concurrent submission, so exactly one thread
may be inside `predict` at a time (AGENTS.md §3-20).

This layer also owns the engine's load phase, because it is the layer that has to
explain a refusal: `run()` and the API's readiness endpoint must give the same
reason for the same state, and a refusal that says "still loading" when the load
has already failed permanently would send callers into an endless retry.

It owns the request budget for the same reason. A single forward pass cannot be
interrupted in-process, so the only place the AGENTS.md §3-17 budget can be
enforced is the wait *before* the engine is entered -- which is also the only part
that is Decis's fault rather than the model's.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .domain import MeasuredTokens, PreparedRequest
from .engines.base import DecisionEngine, EngineInfo, Prediction, WeightSpec, WorkItem
from .errors import EngineFailedError, EngineOverloadedError, EngineUnavailableError

_logger = logging.getLogger("decis.scheduler")

#: Enough of an engine's load error to act on, short enough to put in a response
#: body and a log line. The full traceback goes to the log.
_MAX_ERROR_CHARS = 300


def short_error(exc: BaseException) -> str:
    """Format a load failure for `LoadStatus.error`.

    Public because the shape of this string is observable in a response body, so a
    second implementation -- a test double, or a future out-of-process scheduler --
    must produce the same thing rather than re-derive it. The exception type is kept
    because "weights are corrupt" alone does not say whether it was a corrupt
    checkpoint, an OOM, or a missing module.
    """
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_ERROR_CHARS else text[: _MAX_ERROR_CHARS - 1] + "\u2026"


class LoadPhase(StrEnum):
    """Where the engine is in its lifecycle.

    Deliberately separate from `ready`, and both are needed: `ready` answers "can
    it serve?", which only the engine knows, while the phase answers "if not, why?",
    which is what an operator and an error message need. Only `FAILED` is terminal.
    """

    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class LoadStatus:
    """The answer to "why is the next request going to be refused?"."""

    phase: LoadPhase
    engine_id: str
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.phase is LoadPhase.READY

    @property
    def failed(self) -> bool:
        return self.phase is LoadPhase.FAILED

    @property
    def loading(self) -> bool:
        return self.phase is LoadPhase.LOADING

    def unavailable_message(self) -> str:
        """The single home of the "here is why you cannot be served" text.

        A caller that is told "still loading" forever will retry forever, so a
        terminal failure has to say that it is terminal.
        """
        if self.phase is LoadPhase.FAILED:
            return (
                f"The {self.engine_id} engine failed to load and cannot serve requests "
                f"({self.error}). It will not recover without a restart."
            )
        if self.phase is LoadPhase.LOADING:
            return "The model is still loading. Please retry shortly."
        return "The server is starting up. Please retry shortly."


class Scheduler(Protocol):
    """How the rest of the package runs inference. Swappable for a batching or
    multi-process implementation without touching a route."""

    @property
    def info(self) -> EngineInfo: ...

    @property
    def ready(self) -> bool: ...

    @property
    def load_status(self) -> LoadStatus: ...

    def count_tokens(self, texts: list[str]) -> int: ...

    def measure(self, request: PreparedRequest) -> MeasuredTokens: ...

    def run(self, items: list[WorkItem]) -> Prediction: ...


class InProcessScheduler:
    """Runs items on one engine instance, serialised by a lock.

    The lock is acquired with a deadline, so a request that cannot start within the
    caller's budget is refused instead of piling up: see `run`.
    """

    def __init__(self, engine: DecisionEngine, *, request_timeout_ms: int = 8000) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        # Separate from `_lock`, which is held for the whole of `predict`. Sharing
        # one lock would make `/readyz` block behind an in-flight inference, which
        # is exactly when a probe is most likely to arrive.
        self._status_lock = threading.Lock()
        self._phase = LoadPhase.IDLE
        self._error: str | None = None
        self._request_timeout_ms = request_timeout_ms

    @property
    def engine(self) -> DecisionEngine:
        return self._engine

    @property
    def info(self) -> EngineInfo:
        return self._engine.info()

    @property
    def ready(self) -> bool:
        return self._engine.loaded

    @property
    def load_status(self) -> LoadStatus:
        engine_id = self._engine.info().id
        with self._status_lock:
            phase, error = self._phase, self._error
        if phase is not LoadPhase.READY and self._engine.loaded:
            # The engine is the authority on whether it can serve. A scheduler that
            # did not drive the load -- an app handed an already-loaded engine, which
            # `create_app` explicitly supports -- must not report it as "loading" and
            # so contradict `ready`, which reads the engine. Two answers to one
            # question is precisely the bug this reconciliation removes.
            phase, error = LoadPhase.READY, None
        return LoadStatus(phase=phase, engine_id=engine_id, error=error)

    def load(self) -> None:
        """Load the engine. Synchronous, and raises on failure.

        Recording the phase here rather than in the caller is what lets the refusal
        path say *why* it is refusing: whoever asks next -- `/readyz`, or a request
        arriving during a cold start -- reads the same recorded outcome, whether the
        load was driven by the lifespan thread or by a test.
        """
        with self._status_lock:
            if self._phase is LoadPhase.READY:
                return
            self._phase = LoadPhase.LOADING
            self._error = None
        try:
            self._engine.load()
        except Exception as exc:
            with self._status_lock:
                self._phase = LoadPhase.FAILED
                self._error = short_error(exc)
            raise
        with self._status_lock:
            self._phase = LoadPhase.READY

    def close(self) -> None:
        self._engine.close()

    def count_tokens(self, texts: list[str]) -> int:
        return self._engine.count_tokens(texts)

    def measure(self, request: PreparedRequest) -> MeasuredTokens:
        # Delegated rather than snapshotted: the measurement depends on the
        # loaded tokenizer and the checkpoint's own budgets, both of which only
        # exist after `load()`.
        return self._engine.measure(request)

    def weights(self) -> WeightSpec | None:
        return self._engine.weights()

    def run(self, items: list[WorkItem]) -> Prediction:
        """Answer `items`, or refuse within the caller's budget.

        The wait for the engine is bounded by `request_timeout_ms` (AGENTS.md §3-17).
        A synchronous `torch` forward cannot be interrupted once it has started, so
        the budget is enforced where it can be: before the work begins. A request
        that cannot get the engine in time was never going to be answered before the
        client's own 10 s timeout -- and the client would then re-send, so letting it
        queue would amplify the load that caused the queue. It gets a 429 with
        `retry-after-ms` instead, which is the one answer that tells the SDK how to
        back off rather than hammer.

        The cost is honest and worth stating: the engine itself is still busy, so
        this bounds *waiting*, not work already in flight.
        """
        if not self._engine.loaded:
            raise EngineUnavailableError(self.load_status.unavailable_message())

        budget_s = self._request_timeout_ms / 1000
        started = time.perf_counter()
        if not self._lock.acquire(timeout=budget_s):
            waited_ms = round((time.perf_counter() - started) * 1000)
            _logger.warning(
                "engine %s was still busy after %dms (budget %dms); refusing rather than queueing",
                self.info.id,
                waited_ms,
                self._request_timeout_ms,
            )
            raise EngineOverloadedError(
                f"The {self.info.id} engine is still busy with earlier requests after {budget_s:.1f}s, so "
                f"this request was not started. It is refused rather than left to outlive the client's "
                f"timeout, which would make the client retry and load the engine further. Retry shortly.",
                retry_after_ms=1000,
            )
        try:
            try:
                prediction = self._engine.predict(items)
            except (EngineUnavailableError, EngineFailedError):
                raise
            except Exception as exc:
                _logger.exception("engine %s failed", self.info.id)
                raise EngineFailedError(f"The {self.info.id} engine failed: {type(exc).__name__}: {exc}") from exc
        finally:
            self._lock.release()
        if len(prediction.probabilities) != len(items):
            raise EngineFailedError(
                f"The {self.info.id} engine returned {len(prediction.probabilities)} answers "
                f"for {len(items)} questions."
            )
        return prediction
