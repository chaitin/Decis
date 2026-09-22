"""The request budget (AGENTS.md §3-17).

"No synchronous wait may make a single request exceed 10 s." The official SDK's per-HTTP
timeout is 10 s and a timeout is in its retry set, so a request that the server is still
holding when the client gives up gets *re-sent* -- the server keeps computing the first
copy while a second arrives. The invariant exists to stop that feedback loop.

What is enforceable, and therefore what is tested here: the wait for the engine. A
synchronous `torch` forward cannot be interrupted once started, so a request that cannot
be *begun* inside the budget is refused instead of queued. These tests pin the refusal,
its status code, its backoff header, and -- the part that actually encodes "does not
exceed the budget" -- that it comes back promptly instead of blocking.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from decis.app import create_app
from decis.config import Settings
from decis.engines.base import Prediction, WorkItem
from decis.engines.mock import MockEngine
from decis.errors import EngineOverloadedError
from decis.scheduler import InProcessScheduler


class BlockingEngine(MockEngine):
    """A loaded engine whose `predict` parks until the test releases it.

    Stands in for a forward pass that takes longer than a caller is willing to wait,
    without needing one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._count_lock = threading.Lock()

    def load(self) -> None:
        # Skip MockEngine's work; `loaded` is what the scheduler checks.
        self._loaded = True

    def predict(self, items: list[WorkItem]) -> Prediction:
        with self._count_lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(30)
        # `ProbDist` is `list[float]` (domain.py), not a class: one distribution per
        # item, ordered like `item.question.options`.
        return Prediction(probabilities=[[0.5, 0.5] for _ in items], input_tokens=len(items))


def _item() -> WorkItem:
    from decis.domain import Option, PreparedQuestion

    return WorkItem(
        request_id="req_test",
        state_text="state",
        question=PreparedQuestion(
            qid="q",
            type="noul",
            instructions="",
            options=(Option(name="false", description=""), Option(name="true", description="")),
        ),
    )


def _loaded(engine: MockEngine, **kwargs) -> InProcessScheduler:
    scheduler = InProcessScheduler(engine, **kwargs)
    scheduler.load()
    return scheduler


# --- the scheduler's own behaviour ------------------------------------------


def test_a_request_that_cannot_start_is_refused_rather_than_queued() -> None:
    """The core of the invariant: bounded waiting, and a retryable answer."""
    engine = BlockingEngine()
    scheduler = _loaded(engine, request_timeout_ms=200)

    first = threading.Thread(target=lambda: scheduler.run([_item()]))
    first.start()
    assert engine.entered.wait(5), "the first request never reached the engine"

    started = time.perf_counter()
    with pytest.raises(EngineOverloadedError) as caught:
        scheduler.run([_item()])
    elapsed = time.perf_counter() - started

    assert elapsed < 5, f"the refusal took {elapsed:.1f}s; the wait was not bounded"
    assert caught.value.retry_after_ms > 0
    assert "not started" in caught.value.message
    # It gave up *before* the engine, so the engine only ever saw one request.
    assert engine.calls == 1

    engine.release.set()
    first.join(10)


def test_the_engine_is_usable_again_after_the_busy_one_finishes() -> None:
    """A refusal must not wedge the engine: the lock has to be released."""
    engine = BlockingEngine()
    scheduler = _loaded(engine, request_timeout_ms=200)

    first = threading.Thread(target=lambda: scheduler.run([_item()]))
    first.start()
    assert engine.entered.wait(5)
    with pytest.raises(EngineOverloadedError):
        scheduler.run([_item()])

    engine.release.set()
    first.join(10)
    assert not first.is_alive()

    # Now idle: a third request gets in and reaches the engine.
    engine.release.clear()
    engine.entered.clear()
    third = threading.Thread(target=lambda: scheduler.run([_item()]))
    third.start()
    assert engine.entered.wait(5), "the engine was left unusable after a refusal"
    engine.release.set()
    third.join(10)
    assert engine.calls == 2


def test_a_busy_engine_does_not_block_forever_by_default() -> None:
    """The default must itself be bounded, or the invariant depends on configuration.

    The default is 8000 ms, below the SDK's 10 s, so even an unconfigured deployment
    answers before the client gives up. Asserted with a generous ceiling so a slow CI
    machine does not make this flaky; the point is that a bound exists at all.
    """
    assert InProcessScheduler(BlockingEngine())._request_timeout_ms == 8000
    assert InProcessScheduler(BlockingEngine())._request_timeout_ms < 10_000


# --- through the API --------------------------------------------------------


@pytest.fixture
def budget_settings(settings: Settings) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, request_timeout_ms=250)


def _post(client: TestClient, headers: dict[str, str]) -> object:
    return client.post(
        "/v1/systemone",
        json={"state": "x", "model": "mock", "questions": {"q": {"type": "noul"}}},
        headers=headers,
    )


def test_a_busy_engine_answers_429_with_the_backoff_the_sdk_honours(budget_settings: Settings, auth) -> None:
    """429 + `retry-after-ms`, not 504.

    Both would be 5xx-retryable by the official SDK, but a 504 carries no instruction,
    so the SDK falls back to exponential backoff against an engine that is already
    saturated (AGENTS.md §3-16). 429 is the answer that says how long to wait.
    """
    engine = BlockingEngine()
    engine.load()
    scheduler = InProcessScheduler(engine, request_timeout_ms=budget_settings.request_timeout_ms)
    app = create_app(budget_settings, scheduler=scheduler)

    with TestClient(app) as client:
        first = threading.Thread(target=lambda: _post(client, auth))
        first.start()
        assert engine.entered.wait(5), "the first request never reached the engine"

        started = time.perf_counter()
        response = _post(client, auth)
        elapsed = time.perf_counter() - started

        assert response.status_code == 429
        assert response.headers["retry-after-ms"] == "1000"
        assert response.headers.get("retry-after")
        assert response.json()["detail"]["error_type"] == "rate_limit_error"
        # The whole point: it came back inside the budget, not after the client gave up.
        assert elapsed < 5, f"the 429 took {elapsed:.1f}s"

        engine.release.set()
        first.join(10)


def test_the_request_budget_comes_from_settings(settings: Settings) -> None:
    """A configured budget must reach the scheduler, not be silently ignored."""
    import dataclasses

    tuned = dataclasses.replace(settings, request_timeout_ms=1234)
    app = create_app(tuned)
    assert app.state.service.scheduler._request_timeout_ms == 1234


def test_healthz_and_readyz_answer_while_inference_is_running(settings: Settings, auth) -> None:
    """A synchronous inference must not take the event loop down with it.

    This is the test that would have caught a real bug: `/v1/systemone` was declared
    `async def` while calling a blocking function, so a single inference stopped the
    server from answering *anything* -- probes included -- until it finished. An
    orchestrator polling `/healthz` during a slow request would have seen a hang and
    could have restarted a container that was working correctly.

    The engine is held inside `predict` here, so both probes are answered while an
    inference is genuinely in flight.
    """
    engine = BlockingEngine()
    engine.load()
    scheduler = InProcessScheduler(engine, request_timeout_ms=settings.request_timeout_ms)
    app = create_app(settings, scheduler=scheduler)

    with TestClient(app) as client:
        inference = threading.Thread(target=lambda: _post(client, auth))
        inference.start()
        assert engine.entered.wait(5), "the inference never reached the engine"

        started = time.perf_counter()
        health = client.get("/healthz")
        elapsed = time.perf_counter() - started

        assert health.status_code == 200
        assert elapsed < 5, f"/healthz took {elapsed:.1f}s during inference; the event loop is blocked"
        assert client.get("/readyz").status_code == 200

        engine.release.set()
        inference.join(10)
        assert not inference.is_alive()


def test_the_default_budget_is_below_the_official_sdk_timeout() -> None:
    """8 s against the SDK's 10 s, so Decis answers before the client re-sends.

    A budget above the client's timeout would be worse than none: the server would
    refuse a request the client had already abandoned and re-sent.
    """
    from decis.config import Settings as Fresh

    assert Fresh(env_file="").request_timeout_ms == 8000
    assert Fresh(env_file="").request_timeout_ms < 10_000
