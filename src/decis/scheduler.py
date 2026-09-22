"""Running work on an engine.

Stage 0 keeps this deliberately thin: one engine instance, one lock, one call.
Cross-request batching -- joining several requests' questions into a single
forward pass -- is the thing this layer exists to grow into, and it arrives in
Stage 3 with the measurements that justify its complexity (docs/design.md §6).

The lock is not optional. A `torch.nn.Module` is not re-entrant, and some
backends behave unpredictably under concurrent submission, so exactly one thread
may be inside `predict` at a time (AGENTS.md §3-20).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from .engines.base import DecisionEngine, EngineInfo, Prediction, WorkItem
from .errors import EngineFailedError, EngineUnavailableError

_logger = logging.getLogger("decis.scheduler")


class Scheduler(Protocol):
    """How the rest of the package runs inference. Swappable for a batching or
    multi-process implementation without touching a route."""

    @property
    def info(self) -> EngineInfo: ...

    @property
    def ready(self) -> bool: ...

    def count_tokens(self, texts: list[str]) -> int: ...

    def run(self, items: list[WorkItem]) -> Prediction: ...


class InProcessScheduler:
    """Runs items on one engine instance, serialised by a lock."""

    def __init__(self, engine: DecisionEngine) -> None:
        self._engine = engine
        self._lock = threading.Lock()

    @property
    def engine(self) -> DecisionEngine:
        return self._engine

    @property
    def info(self) -> EngineInfo:
        return self._engine.info()

    @property
    def ready(self) -> bool:
        return self._engine.loaded

    def load(self) -> None:
        self._engine.load()

    def close(self) -> None:
        self._engine.close()

    def count_tokens(self, texts: list[str]) -> int:
        return self._engine.count_tokens(texts)

    def run(self, items: list[WorkItem]) -> Prediction:
        if not self._engine.loaded:
            raise EngineUnavailableError("The model is still loading. Retry shortly.")
        with self._lock:
            try:
                prediction = self._engine.predict(items)
            except (EngineUnavailableError, EngineFailedError):
                raise
            except Exception as exc:
                _logger.exception("engine %s failed", self.info.id)
                raise EngineFailedError(f"The {self.info.id} engine failed: {type(exc).__name__}: {exc}") from exc
        if len(prediction.probabilities) != len(items):
            raise EngineFailedError(
                f"The {self.info.id} engine returned {len(prediction.probabilities)} answers "
                f"for {len(items)} questions."
            )
        return prediction
