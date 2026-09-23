"""The acceptance test that matters most: the official SDK, unmodified.

Nothing else proves compatibility. Our own tests can only confirm that we agree
with ourselves and with the OpenAPI snapshot; this runs the real client library
against a real socket and asserts on the typed objects it parses out.

Run it against the live API to check the contract itself rather than our
implementation::

    TYPESAFE_API_KEY=<key> TYPESAFE_BASE_URL=https://api.typesafe.ai \\
        pytest tests/test_contract_sdk.py -q
"""

from __future__ import annotations

import threading

import pytest
import uvicorn
from typesafe_sdk import (
    Choice,
    Noul,
    Score,
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafePermissionDeniedError,
)

from conftest import VERSIONED_STUB
from decis.app import create_app
from decis.config import Settings

TEST_KEY = "sdk-acceptance-key"


@pytest.fixture
def live_server(settings):
    """A real HTTP server, because the SDK needs a socket, not an ASGI stub.

    A stubbed transport would bypass exactly the layer most likely to differ:
    header handling, status codes and JSON encoding.
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(Settings(api_keys=(TEST_KEY,), host="127.0.0.1", default_engine="stub", env_file=""))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time

    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("the test server did not start")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def sdk(live_server):
    with TypeSafeClient(api_key=TEST_KEY, base_url=live_server) as client:
        yield client


def _client_without_credentials(live_server, api_key: str = "placeholder"):
    """An SDK client whose Authorization header never reaches the server.

    The SDK refuses to construct with an empty `api_key`, so a client can never
    actually send *no* credential -- which means the 403 path is only reachable by
    a non-SDK caller. To still check how the SDK maps our 403, strip the header in
    the transport rather than skipping the test.
    """
    import httpx2

    class StripAuth(httpx2.BaseTransport):
        def __init__(self) -> None:
            self._inner = httpx2.HTTPTransport()

        def handle_request(self, request):
            request.headers.pop("authorization", None)
            return self._inner.handle_request(request)

    return TypeSafeClient(api_key=api_key, base_url=live_server, transport=StripAuth())


def test_sdk_answers_all_three_primitives(sdk) -> None:
    result = sdk.system_one(
        state="The parcel arrived crushed and support has not replied for a week.",
        questions={
            "complaint": Noul(instructions="Is the customer complaining?"),
            "tone": Choice(instructions="What is the tone?", criteria={"calm": None, "angry": None}),
            "urgency": Score(instructions="How urgent?", criteria=["later", "soon", "today", "now"]),
        },
        model="stub",
    )

    assert result.model == VERSIONED_STUB
    assert result.usage.input_tokens >= 0
    assert result.usage.output_tokens >= 0
    assert set(result.answers) == {"complaint", "tone", "urgency"}

    # The SDK lifts answers into typed buckets; these are its own accessors.
    assert 0.0 <= result.nouls["complaint"].noul <= 1.0
    assert result.choices["tone"].choice in {"calm", "angry"}
    assert 0.0 <= result.scores["urgency"].score <= 3.0


def test_sdk_parses_score_probability_keys_as_integers(sdk) -> None:
    """The SDK types `legend`/`probabilities` as dict[int, ...], so our string keys
    must convert. This is the clearest single check that the key encoding is right."""
    result = sdk.system_one(
        state="text",
        questions={"u": Score(instructions="How urgent?", criteria=["a", "b", "c"])},
        model="stub",
    )
    answer = result.scores["u"]
    assert set(answer.probabilities) == {0, 1, 2}
    assert all(isinstance(key, int) for key in answer.probabilities)
    assert set(answer.legend) == {0, 1, 2}


def test_sdk_list_models(sdk) -> None:
    models = sdk.models.list()
    names = {model.name for model in models.models}
    assert VERSIONED_STUB in names
    for model in models.models:
        assert model.description
        assert model.release_date


def test_sdk_accepts_extra_top_level_fields(sdk) -> None:
    """We add a `decis` namespace; the SDK's models must ignore it.

    `extra="ignore"` is documented behaviour of the SDK's response models, and this
    is what lets us report engine, device and latency without breaking anyone.
    """
    result = sdk.system_one(state="text", questions={"q": Noul(instructions="?")}, model="stub")
    assert not hasattr(result, "decis") or getattr(result, "decis", None) is None


def test_sdk_default_model_jev_latest_works(sdk) -> None:
    """The single most important compatibility behaviour.

    A client that only changes TYPESAFE_BASE_URL still sends `jev-latest`, because
    that is the SDK's default. It must be served.
    """
    result = sdk.system_one(state="text", questions={"q": Noul(instructions="Is this a test?")})
    assert result.model == VERSIONED_STUB


def test_sdk_raises_permission_denied_without_a_key(live_server) -> None:
    """403 -- not 401. The SDK maps the two to different exception classes, so a
    wrong status code here would send callers down the wrong recovery path."""
    with _client_without_credentials(live_server) as client, pytest.raises(TypeSafePermissionDeniedError):
        client.system_one(state="text", questions={"q": Noul(instructions="?")}, model="stub")


def test_sdk_raises_authentication_error_with_a_bad_key(live_server) -> None:
    with (
        TypeSafeClient(api_key="not-the-key", base_url=live_server) as client,
        pytest.raises(TypeSafeAuthenticationError),
    ):
        client.system_one(state="text", questions={"q": Noul(instructions="?")}, model="stub")


def test_sdk_does_not_retry_auth_failures(live_server) -> None:
    """Retrying a 401 would triple the cost of a misconfigured client at our end."""
    attempts = 0

    def counting_transport(request):  # pragma: no cover - replaced below
        raise AssertionError

    import httpx2

    class Counter(httpx2.BaseTransport):
        def handle_request(self, request):
            nonlocal attempts
            attempts += 1
            return httpx2.Response(401, json={"detail": {"error_type": "authentication_error", "message": "no"}})

    with (
        TypeSafeClient(api_key="bad", base_url=live_server, transport=Counter()) as client,
        pytest.raises(TypeSafeAuthenticationError),
    ):
        client.system_one(state="text", questions={"q": Noul(instructions="?")}, model="stub")
    assert attempts == 1, f"the SDK retried an auth failure {attempts} times"


def test_sdk_error_reports_a_readable_message(live_server) -> None:
    """The point of the error work: a caller can act on what we send back."""
    with _client_without_credentials(live_server) as client, pytest.raises(TypeSafeAPIError) as caught:
        client.system_one(state="text", questions={"q": Noul(instructions="?")}, model="stub")
    assert "Authorization: Bearer" in str(caught.value)


@pytest.mark.network
def test_live_api_still_matches_our_assumptions() -> None:
    """L5: the differential run AGENTS.md §12 requires before a contract change.

    Skipped unless credentials are supplied. Run it against api.typesafe.ai before
    changing anything in docs/api-compatibility.md.
    """
    import os

    key = os.environ.get("TYPESAFE_LIVE_API_KEY")
    if not key:
        pytest.skip("set TYPESAFE_LIVE_API_KEY to run the differential contract check")

    with (
        TypeSafeClient(api_key=key, base_url="https://api.typesafe.ai") as client,
        # A missing credential must be 403, and an unusable one 401.
        _client_without_credentials("https://api.typesafe.ai") as anonymous,
        pytest.raises(TypeSafePermissionDeniedError),
    ):
        anonymous.system_one(state="text", questions={"q": Noul(instructions="?")})

        result = client.system_one(state="text", questions={"q": Noul(instructions="Is this a test?")})
        assert result.usage.input_tokens > 0
