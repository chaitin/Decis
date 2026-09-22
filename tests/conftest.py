"""Shared fixtures.

Every test drives the API through a real ASGI client, because the things most
likely to break are the ones the framework decides: header propagation, the order
of auth and validation, and status codes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The SDK's HTTP library parses the proxy environment on client construction.
# Some hosts export a `no_proxy` containing the bracketed IPv6 literal `[::1]`,
# which httpx2 rejects with `InvalidURL: Invalid port: ':1]'` -- before any request
# is made. It is an environment problem, not a Decis one, but it makes every test
# that builds a client fail, so normalise it here. See benchmarks/README.md.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = "127.0.0.1,localhost,::1"

from decis.app import create_app  # noqa: E402 - must follow the proxy normalisation
from decis.config import Settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = REPO_ROOT / "docs" / "contract"
OPENAPI_SNAPSHOT = CONTRACT_DIR / "typesafe-openapi-0.2.0.json"

TEST_KEY = "test-token-do-not-use-in-production"


@pytest.fixture
def settings() -> Settings:
    """Authenticated, loopback, mock engine."""
    return Settings(
        api_keys=(TEST_KEY,),
        host="127.0.0.1",
        default_engine="mock",
        env_file="",
    )


@pytest.fixture
def open_settings() -> Settings:
    """No auth, but explicitly allowed so the safety check is satisfied."""
    return Settings(api_keys=(), allow_no_auth=True, host="127.0.0.1", default_engine="mock", env_file="")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def open_client(open_settings: Settings) -> TestClient:
    with TestClient(create_app(open_settings)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture(scope="session")
def openapi_snapshot() -> dict:
    return json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))


# --- reference payloads ------------------------------------------------------


@pytest.fixture
def noul_request() -> dict:
    return {
        "state": "I was charged twice for the same order and nobody has replied.",
        "model": "mock",
        "questions": {"billing": {"type": "noul", "instructions": "Is this about billing?"}},
    }


@pytest.fixture
def mixed_request() -> dict:
    """One of each primitive, which is what exercises the whole normalisation path."""
    return {
        "state": {"subject": "Refund request", "body": "The item arrived damaged.", "order_id": "A-1"},
        "model": "mock",
        "questions": {
            "complaint": {
                "type": "noul",
                "instructions": "Is the customer complaining?",
                "criteria": {"true": "Describes a problem", "false": "No problem described"},
            },
            "tone": {
                "type": "choice",
                "instructions": "What is the tone?",
                "criteria": {"calm": "Neutral and factual", "angry": "Frustrated or hostile"},
            },
            "urgency": {
                "type": "score",
                "instructions": "How urgent is this?",
                "criteria": ["Can wait", "Soon", "Today", "Immediately"],
            },
        },
    }


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect weight-dependent tests unless `-m weights` was asked for."""
    if config.getoption("-m") and "weights" in str(config.getoption("-m")):
        return
    skip = pytest.mark.skip(reason="needs model weights; run with: pytest -m weights")
    for item in items:
        if "weights" in item.keywords:
            item.add_marker(skip)
