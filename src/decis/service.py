"""Request -> response, with no HTTP involved.

All the decision logic lives here so it can be tested without a client, a socket
or a running server. Routes stay thin: parse, delegate, serialise.
"""

from __future__ import annotations

import logging
import time

from .answers import build_answer, estimate_output_tokens
from .config import Settings
from .engines.base import EngineInfo, WorkItem
from .engines.registry import SPECS, canonical, describe, is_foreign_default, unknown_model_error
from .errors import InvalidRequestError
from .render import prepare_request
from .scheduler import Scheduler
from .schema import (
    DecisExtensions,
    ModelMetadata,
    ModelMetadataList,
    SystemOneRequest,
    SystemOneResponse,
    Usage,
    validate_capacity,
)

_logger = logging.getLogger("decis.service")


class DecisionService:
    """Answers System One requests using one loaded engine."""

    def __init__(self, settings: Settings, scheduler: Scheduler) -> None:
        self._settings = settings
        self._scheduler = scheduler

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def ready(self) -> bool:
        return self._scheduler.ready

    @property
    def engine_id(self) -> str:
        return self._scheduler.info.id

    def load(self) -> None:
        self._scheduler.load()

    def close(self) -> None:
        self._scheduler.close()

    # --- endpoints ---------------------------------------------------------

    def models(self) -> ModelMetadataList:
        """Every engine whose dependencies are importable in this image.

        Must not need weights, and must not fail because one engine's extra is
        missing (AGENTS.md §6): a laya-only image still lists what it can serve.
        """
        listed: dict[str, ModelMetadata] = {}
        for engine_id in SPECS:
            info = self._describe(engine_id)
            if info is not None:
                listed[info.id] = _metadata(info)

        loaded = self._loaded_info()
        if loaded is not None:
            # Assign rather than setdefault: `describe()` reports pre-load values
            # ("unloaded" device), and the loaded engine's are the true ones.
            listed[loaded.id] = _metadata(loaded)

        return ModelMetadataList(models=list(listed.values()))

    def answer(self, request: SystemOneRequest, *, request_id: str) -> SystemOneResponse:
        prepared = prepare_request(request)
        info = self._scheduler.info
        loaded = canonical(info.id)

        requested = canonical(request.model)
        substituted = requested is None
        if substituted:
            # "jev-latest" and friends mean "whatever this server runs". Accepting
            # them is what makes swapping TYPESAFE_BASE_URL sufficient; the
            # substitution is reported back in `decis.requested_model`.
            if not (self._settings.accept_foreign_defaults and is_foreign_default(request.model)):
                raise unknown_model_error(request.model)
            requested = loaded

        if requested != loaded:
            # A model this server knows but is not running. Say which one it does
            # run, rather than quietly answering with a different model.
            raise InvalidRequestError(
                f"This server is running {info.id!r}, so it cannot answer as {request.model!r}. "
                f"Send model={info.model_id!r}, or start a server with that engine loaded.",
                loc=["body", "model"],
            )

        validate_capacity(prepared, info, self._scheduler.measure)

        items = [
            WorkItem(request_id=request_id, state_text=prepared.state_text, question=question)
            for question in prepared.questions
        ]

        started = time.perf_counter()
        prediction = self._scheduler.run(items)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        answers = {
            question.qid: build_answer(question, probabilities)
            for question, probabilities in zip(prepared.questions, prediction.probabilities, strict=True)
        }

        # Zip by question id rather than trusting order, so a native confidence can
        # never end up attributed to a different question than the one it scored.
        native = None
        if prediction.native_confidences is not None:
            native = {
                question.qid: value
                for question, value in zip(prepared.questions, prediction.native_confidences, strict=True)
            }

        return SystemOneResponse(
            model=info.model_id,
            answers=answers,
            usage=Usage(
                input_tokens=prediction.input_tokens,
                output_tokens=estimate_output_tokens({qid: answer.model_dump() for qid, answer in answers.items()}),
            ),
            decis=DecisExtensions(
                engine=info.id,
                engine_version=info.version,
                device=info.device,
                dtype=info.dtype,
                latency_ms=latency_ms,
                batch_size=len(items),
                native_confidence=native,
                requested_model=request.model if substituted else None,
            ),
        )

    # --- helpers -----------------------------------------------------------

    def _describe(self, engine_id: str) -> EngineInfo | None:
        try:
            return describe(engine_id)
        except Exception as exc:
            # An engine whose dependencies are not installed is normal in a
            # single-engine image. It is not an error, so it is not logged as one.
            _logger.debug("engine %s is registered but unavailable here: %s", engine_id, exc)
            return None

    def _loaded_info(self) -> EngineInfo | None:
        return self._scheduler.info if self._scheduler.ready else None


def _metadata(info: EngineInfo) -> ModelMetadata:
    return ModelMetadata(
        name=info.model_id,
        description=info.description,
        release_date=info.release_date,
        decis={
            "engine": info.id,
            "version": info.version,
            "aliases": list(info.aliases),
            **info.capacities(),
        },
    )
