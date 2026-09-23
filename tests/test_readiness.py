"""Startup, readiness and shutdown.

An 80 s cold start is long enough that the difference between "alive", "ready" and
"broken" decides whether a rolling deploy works or wedges. These tests pin all
three, and they pin the *timing*, not just the status codes: until Stage 2 the
engine was loaded synchronously inside the lifespan, so no HTTP response happened
at all until the weights were in memory and `/healthz` hung rather than answering.
A test that only checks status codes after startup cannot tell those apart -- it
would have passed. So the checks below hold the load open on purpose and probe
*while it is running*.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import VERSIONED_STUB
from decis.app import create_app
from decis.config import Settings
from decis.domain import MeasuredTokens, PreparedRequest
from decis.engines.base import EngineInfo, Prediction, WorkItem
from decis.errors import EngineUnavailableError
from decis.scheduler import LoadPhase, LoadStatus, short_error
from fixture_engine import StubEngine


class FakeScheduler:
    """A scheduler whose load duration, outcome and close count the test controls.

    Deliberately implements the whole `Scheduler` protocol rather than duck-typing a
    subset: if the protocol grows a member that startup depends on, this fails to
    type-check instead of quietly exercising a path the real scheduler does not have.
    """

    def __init__(self, *, gate: threading.Event | None = None, fails: str | None = None) -> None:
        self._info = StubEngine().info()
        self._gate = gate
        self._fails = fails
        self._lock = threading.Lock()
        self._phase = LoadPhase.IDLE
        self._error: str | None = None
        self.load_calls = 0
        self.close_calls = 0

    # --- the protocol ------------------------------------------------------

    @property
    def info(self) -> EngineInfo:
        return self._info

    @property
    def ready(self) -> bool:
        return self._phase is LoadPhase.READY

    @property
    def load_status(self) -> LoadStatus:
        with self._lock:
            return LoadStatus(phase=self._phase, engine_id=self._info.id, error=self._error)

    def load(self) -> None:
        self.load_calls += 1
        self._set(LoadPhase.LOADING)
        if self._gate is not None:
            self._gate.wait(20)
        if self._fails is not None:
            failure = RuntimeError(self._fails)
            self._set(LoadPhase.FAILED, short_error(failure))
            raise failure
        self._set(LoadPhase.READY)

    def close(self) -> None:
        self.close_calls += 1

    def count_tokens(self, texts: list[str]) -> int:
        return 1

    def measure(self, request: PreparedRequest) -> MeasuredTokens:
        # A real engine has no tokenizer until `load()` has run. Mirroring that here
        # is what makes "did the capacity check run too early?" observable: reaching
        # this method on a cold engine would be a 500, not a 503.
        if not self.ready:
            raise AssertionError("measure() was called before the engine was loaded")
        return MeasuredTokens(state_tokens=1, head_tokens=1, sequence_tokens=2)

    def run(self, items: list[WorkItem]) -> Prediction:
        if not self.ready:
            raise EngineUnavailableError(self.load_status.unavailable_message())
        raise AssertionError("FakeScheduler cannot answer; these tests only measure startup")

    # --- test control ------------------------------------------------------

    def _set(self, phase: LoadPhase, error: str | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._error = error


def _slow_scheduler() -> tuple[FakeScheduler, threading.Event]:
    gate = threading.Event()
    return FakeScheduler(gate=gate), gate


def _fast_shutdown(settings: Settings) -> Settings:
    """Keep the shutdown-wait short so a deliberately stuck load does not stall CI."""
    return dataclasses.replace(settings, shutdown_grace_ms=100)


# --- liveness during a cold start -------------------------------------------


def test_healthz_answers_while_the_engine_is_still_loading(settings: Settings) -> None:
    """The D7 regression test, and the reason it is phrased in terms of timing.

    The load is held open, so this asserts that HTTP works *during* the load. Before
    Stage 2 the lifespan blocked the protocol loop for the whole 80 s cold start, and
    this call would have hung until the gate was released -- or forever, with a
    generous timeout. `assert elapsed < 5` is therefore the actual assertion; the 200
    is what a status-code-only test would have checked (after startup, when it was
    always true).
    """
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.get("/healthz")
        elapsed = time.perf_counter() - started

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert elapsed < 5, f"/healthz took {elapsed:.1f}s while the engine was loading; the protocol loop is blocked"
        gate.set()

    assert scheduler.load_calls == 1


def test_readyz_says_loading_while_the_engine_loads(settings: Settings) -> None:
    """`loading` is a distinct, retryable state, not just "not ready"."""
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "loading", "engine": "stub"}
        assert response.headers.get("retry-after") == "1"
        gate.set()


def test_readyz_becomes_ready_once_the_load_finishes(settings: Settings, engine_ready) -> None:
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/readyz").json()["status"] == "loading"
        gate.set()
        assert engine_ready(client) == {"status": "ready", "engine": "stub"}


def test_healthz_does_not_depend_on_the_engine_at_all(settings: Settings) -> None:
    """With no load started, liveness must still be 200 -- that is what makes it liveness."""
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"


def test_concurrent_readiness_probes_are_safe(settings: Settings) -> None:
    """An orchestrator may poll several times a second from more than one place."""
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

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
        gate.set()


# --- a load that fails ------------------------------------------------------


def test_a_failed_load_is_reported_as_failed_and_does_not_stop_the_server(settings: Settings) -> None:
    """The Stage 2 reversal of "a load failure must take the process down".

    Stage 0 chose to fail fast. That is defensible, but it costs observability: the
    only report is a crash-looping container plus a log line, and `/readyz` -- the
    endpoint the orchestrator is already polling -- can say nothing. Now the failure
    is a first-class state that the probe reports, and traffic is still refused.
    """
    scheduler = FakeScheduler(fails="weights are corrupt")
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:  # entering at all is the assertion: no exception
        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["engine"] == "stub"
        assert "weights are corrupt" in body["error"]
        assert "RuntimeError" in body["error"]


def test_a_failed_engine_does_not_ask_the_caller_to_retry(settings: Settings) -> None:
    """A terminal failure must not carry `retry-after`.

    The official SDK retries 5xx. Telling it to back off from an engine that will
    never recover produces an infinite retry loop against a broken server.
    """
    scheduler = FakeScheduler(fails="no such checkpoint")
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert "retry-after" not in response.headers
        # The retryable state does carry it -- see
        # test_readyz_says_loading_while_the_engine_loads. Asserted here too, so the
        # contrast is stated in one place: same status code, opposite instruction.
        assert response.json()["status"] == "failed"


def test_inference_on_a_failed_engine_says_it_failed_not_that_it_is_loading(
    settings: Settings, auth: dict[str, str]
) -> None:
    """The message is the whole point: "still loading" forever is a lie.

    A caller told the model is still loading will retry until its own timeout with no
    idea it is waiting on something that will never finish.
    """
    scheduler = FakeScheduler(fails="weights are corrupt")
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/v1/systemone",
            json={"state": "x", "model": "stub", "questions": {"q": {"type": "noul"}}},
            headers=auth,
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["error_type"] == "engine_unavailable"
        assert "failed to load" in detail["message"]
        assert "will not recover" in detail["message"]
        assert "still loading" not in detail["message"]


def test_inference_during_a_cold_start_is_503_and_never_reaches_the_measurement(
    settings: Settings, auth: dict[str, str]
) -> None:
    """The capacity check needs a loaded tokenizer, so it must not run first.

    `FakeScheduler.measure` raises an AssertionError when called unloaded, and an
    AssertionError escaping would be a 500. Getting a clean 503 proves the readiness
    gate is placed before the measurement -- a bug that only becomes reachable once
    the engine loads in the background.
    """
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/v1/systemone",
            json={"state": "x", "model": "stub", "questions": {"q": {"type": "noul"}}},
            headers=auth,
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["error_type"] == "engine_unavailable"
        assert "still loading" in detail["message"]
        gate.set()


def test_a_request_during_a_cold_start_still_carries_the_request_id(settings: Settings, auth: dict[str, str]) -> None:
    """AGENTS.md §3-15: every response has one, errors included. A 503 is no exception."""
    scheduler, gate = _slow_scheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/v1/systemone",
            json={"state": "x", "model": "stub", "questions": {"q": {"type": "noul"}}},
            headers=auth,
        )
        assert response.status_code == 503
        assert response.headers["x-typesafe-request-id"].startswith("req_")
        gate.set()


def test_models_is_available_without_weights(settings: Settings, auth: dict[str, str]) -> None:
    """AGENTS.md §6: listing models must not require a loaded engine."""
    app = create_app(settings, load_engine=False)
    with TestClient(app) as client:
        response = client.get("/v1/models", headers=auth)
        assert response.status_code == 200
        assert any(model["name"] == VERSIONED_STUB for model in response.json()["models"])


# --- shutdown ---------------------------------------------------------------


def test_shutdown_closes_a_loaded_engine(settings: Settings, engine_ready) -> None:
    scheduler = FakeScheduler()
    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        engine_ready(client)
        assert scheduler.close_calls == 0
    assert scheduler.close_calls == 1


def test_shutdown_does_not_close_an_engine_that_is_still_loading(settings: Settings) -> None:
    """Closing a half-initialised model races the loader.

    The gate is never released, so shutdown has to give up and skip `close()`. The
    alternative -- closing anyway -- is a use-after-free on a partly built model,
    which is strictly worse than not closing during process exit.
    """
    scheduler, _gate = _slow_scheduler()
    app = create_app(_fast_shutdown(settings), scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/readyz").json()["status"] == "loading"
    assert scheduler.close_calls == 0


def test_shutdown_waits_for_a_load_that_finishes_within_the_grace_period(settings: Settings, engine_ready) -> None:
    """The other half: a load that completes in time is still closed properly."""
    gate = threading.Event()

    class Racing(FakeScheduler):
        def load(self) -> None:
            threading.Timer(0.05, gate.set).start()
            super().load()

    scheduler = Racing(gate=gate)
    app = create_app(dataclasses.replace(settings, shutdown_grace_ms=5000), scheduler=scheduler)  # type: ignore[arg-type]

    with TestClient(app) as client:
        engine_ready(client)
    assert scheduler.close_calls == 1


def test_the_engine_is_not_loaded_twice(settings: Settings) -> None:
    """`load_engine=True` plus an already-loaded engine must not reload 1.5 GB."""
    scheduler = FakeScheduler()
    scheduler._set(LoadPhase.READY)  # already loaded before the app was built

    app = create_app(settings, scheduler=scheduler)  # type: ignore[arg-type]
    with TestClient(app):
        pass
    assert scheduler.load_calls == 0


@pytest.mark.parametrize("engine_id", ["stub", "laya-multilingual"])
def test_every_engine_reports_a_phase_rather_than_a_bare_boolean(engine_id: str) -> None:
    """`ready` alone cannot distinguish "starting" from "broken", so both must exist."""
    from decis.engines.registry import create
    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(create(engine_id))
    status = scheduler.load_status
    assert status.phase is LoadPhase.IDLE
    assert not status.ready
    assert not status.failed
    assert not scheduler.ready
    # The refusal text exists before any load is attempted, so a request arriving
    # first can never produce an empty message.
    assert status.unavailable_message().strip()
    assert "retry" in status.unavailable_message().lower()


def test_an_idle_engine_is_not_reported_as_failed() -> None:
    """`failed` must mean "we tried and it broke", never "we have not tried yet"."""
    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(StubEngine())
    assert scheduler.load_status.phase is LoadPhase.IDLE
    scheduler.load()
    assert scheduler.load_status.phase is LoadPhase.READY
    assert scheduler.load_status.error is None


def test_a_real_failed_load_records_the_error_and_reraises() -> None:
    """The synchronous path is still used by tests and `decis` helpers, so it must
    keep raising while also recording the phase for the refusal path."""

    class Broken(StubEngine):
        def load(self) -> None:
            raise RuntimeError("weights are corrupt")

    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(Broken())
    with pytest.raises(RuntimeError, match="corrupt"):
        scheduler.load()
    status = scheduler.load_status
    assert status.phase is LoadPhase.FAILED
    assert status.error == "RuntimeError: weights are corrupt"
    assert "will not recover" in status.unavailable_message()


def test_loading_is_idempotent() -> None:
    """A second `load()` on a ready engine must be a no-op, not a second 80 s."""
    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(StubEngine())
    scheduler.load()
    scheduler.load()
    assert scheduler.load_status.phase is LoadPhase.READY


def test_the_error_text_is_bounded() -> None:
    """It goes in an unauthenticated response body, so an engine that raises a novel
    long enough to hold a filesystem path must not have it relayed verbatim."""

    class Verbose(StubEngine):
        def load(self) -> None:
            raise RuntimeError("x" * 5000)

    from decis.scheduler import InProcessScheduler

    scheduler = InProcessScheduler(Verbose())
    with pytest.raises(RuntimeError):
        scheduler.load()
    error = scheduler.load_status.error
    assert error is not None
    assert len(error) <= 300
    assert error.endswith("\u2026")
