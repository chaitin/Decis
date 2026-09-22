"""HTTP routes. Thin on purpose: no decision logic, no error shaping."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import __version__
from .observability import get_request_id
from .schema import ErrorResponse, ModelMetadataList, SystemOneRequest, SystemOneResponse
from .service import DecisionService

AUTH_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "The supplied API key is not valid."},
    403: {"model": ErrorResponse, "description": "No API key was supplied."},
}


def create_router(service: DecisionService) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/systemone",
        response_model=SystemOneResponse,
        response_model_exclude_none=True,
        summary="Ask questions about one piece of content",
        responses=AUTH_ERROR_RESPONSES,
    )
    def systemone(payload: SystemOneRequest, request: Request) -> SystemOneResponse:
        """Answer every question about `state` with the selected model.

        Pure: the same request always produces the same answer for a given model,
        because the official SDK retries POSTs automatically.

        **Not `async` by accident.** Inference is synchronous and CPU-bound, and
        `service.answer` is a plain function. Declaring this route `async` would run
        it *on the event loop*, so a single inference -- hundreds of milliseconds,
        and seconds for a long state -- would stop the server from answering
        anything else, `/healthz` included. A probe arriving during inference would
        hang, and an orchestrator could kill a container that is working correctly.
        Starlette runs a plain `def` route in its worker threadpool instead, which
        leaves the loop free and lets `InProcessScheduler`'s lock -- rather than the
        event loop -- be the thing that serialises the engine.
        """
        return service.answer(payload, request_id=get_request_id())

    @router.get(
        "/v1/models",
        response_model=ModelMetadataList,
        response_model_exclude_none=True,
        summary="List the models this server can run",
        responses=AUTH_ERROR_RESPONSES,
    )
    def models() -> ModelMetadataList:
        return service.models()

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness. Stays 200 while the process is up, even mid-load.

        Deliberately not gated on the engine: this is what a container runtime
        restarts on, and restarting during a 75 s cold start would never converge.
        """
        return {"status": "ok", "version": __version__}

    @router.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness. Three states, because "not ready" has two very different causes.

        `loading` is worth retrying, so it carries `retry-after`. `failed` is
        terminal until the process is replaced, so it deliberately does *not*: the
        official SDK retries 5xx, and a caller told to back off from a permanently
        broken engine would hammer it forever. The error text is truncated by
        `LoadStatus` and the traceback stays in the log.
        """
        status = service.load_status
        if status.ready:
            return JSONResponse({"status": "ready", "engine": status.engine_id})
        if status.failed:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "engine": status.engine_id, "error": status.error},
            )
        return JSONResponse(
            status_code=503,
            content={"status": "loading", "engine": status.engine_id},
            headers={"retry-after": "1"},
        )

    return router
