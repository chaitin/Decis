"""Startup, readiness and shutdown.

A 73 s cold start is long enough that the difference between "alive" and "ready"
decides whether a rolling deploy works or wedges. These tests pin that behaviour.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from decis.app import create_app
from decis.config import Settings


def test_healthz_is_up_before_the_engine_finishes_loading(settings: Settings) -> None:
    """Liveness must not depend on the engine, or a slow start becomes a restart loop.

    The app is built with `load_engine=False`, which is the state the process is in
    for the first 73 s of a Laya cold start.
    """
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"


def test_readyz_is_503_until_the_engine_is_loaded(settings: Settings) -> None:
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "loading"
        assert response.headers.get("retry-after") == "1"


def test_readyz_becomes_200_after_loading(settings: Settings) -> None:
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        app.state.service.load()
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "engine": "mock"}


def test_the_engine_is_loaded_during_startup_by_default(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):
        assert app.state.service.ready


def test_inference_before_loading_is_a_clear_503(settings: Settings, auth) -> None:
    """Not a 500, and not a wrong answer: the caller should retry."""
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/systemone",
            json={"state": "x", "model": "mock", "questions": {"q": {"type": "noul"}}},
            headers=auth,
        )
        assert response.status_code == 503
        assert response.json()["detail"]["error_type"] == "engine_unavailable"
        assert "retry" in response.json()["detail"]["message"].lower()


def test_models_is_available_without_weights(settings: Settings, auth) -> None:
    """AGENTS.md §6: listing models must not require a loaded engine."""
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        response = client.get("/v1/models", headers=auth)
        assert response.status_code == 200
        assert any(model["name"] == "decis/mock@0.1.0" for model in response.json()["models"])


def test_shutdown_closes_the_engine(settings: Settings) -> None:
    closed: list[bool] = []

    class Tracked:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def close(self) -> None:
            closed.append(True)

    from decis.engines.mock import MockEngine
    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(Tracked(MockEngine()))  # type: ignore[arg-type]
    app = create_app(settings, scheduler=scheduler)

    with TestClient(app):
        assert not closed
    assert closed == [True]


def test_an_engine_that_fails_to_load_stops_startup(settings: Settings) -> None:
    """Serving traffic we cannot answer is worse than not starting."""

    class Broken:
        @property
        def info(self):
            from decis.engines.mock import MockEngine

            return MockEngine().info()

        @property
        def ready(self) -> bool:
            return False

        def load(self) -> None:
            raise RuntimeError("weights are corrupt")

        def close(self) -> None:
            pass

        def count_tokens(self, texts):
            return 0

        def run(self, items):
            raise AssertionError("unreachable")

    app = create_app(settings, scheduler=Broken())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="corrupt"), TestClient(app):
        pass


def test_concurrent_readiness_probes_are_safe(settings: Settings) -> None:
    """An orchestrator may poll several times a second from more than one place."""
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        results: list[int] = []

        def probe() -> None:
            results.append(client.get("/readyz").status_code)

        threads = [threading.Thread(target=probe) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results == [503] * 16


def test_startup_does_not_block_healthz_for_long(settings: Settings) -> None:
    """The engine load is synchronous; healthz must still answer promptly after."""

    class Slow:
        @property
        def info(self):
            from decis.engines.mock import MockEngine

            return MockEngine().info()

        @property
        def ready(self) -> bool:
            return False

        def load(self) -> None:
            time.sleep(0.2)

        def close(self) -> None:
            pass

        def count_tokens(self, texts):
            return 0

        def run(self, items):
            raise AssertionError("unreachable")

    app = create_app(settings, scheduler=Slow())  # type: ignore[arg-type]
    started = time.perf_counter()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert time.perf_counter() - started >= 0.2
