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
    async def systemone(payload: SystemOneRequest, request: Request) -> SystemOneResponse:
        """Answer every question about `state` with the selected model.

        Pure: the same request always produces the same answer for a given model,
        because the official SDK retries POSTs automatically.
        """
        return service.answer(payload, request_id=get_request_id())

    @router.get(
        "/v1/models",
        response_model=ModelMetadataList,
        response_model_exclude_none=True,
        summary="List the models this server can run",
        responses=AUTH_ERROR_RESPONSES,
    )
    async def models() -> ModelMetadataList:
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
        """Readiness. 503 until the engine has finished loading."""
        if service.ready:
            return JSONResponse({"status": "ready", "engine": service.engine_id})
        return JSONResponse(
            status_code=503,
            content={"status": "loading", "engine": service.engine_id},
            headers={"retry-after": "1"},
        )

    return router
