"""L3b: the error contract.

The shape of an error is as much a part of a wire contract as the shape of a
success, and it is the part an SDK is most likely to branch on. Everything here
was checked against the live API on 2026-09-22; the raw record is in
docs/contract/observations-2026-09-22.md.

The two findings that changed the design:

* A missing credential is **403**, not 401. Only an invalid one is 401.
* Authentication is checked **before** body validation, so an unauthenticated
  request with a malformed body is 403, not 422.
"""

from __future__ import annotations

import re

import pytest

REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")

VALID_BODY = {"state": "text", "model": "mock", "questions": {"q": {"type": "noul"}}}


# --- AGENTS.md §3-14 / §3-15: the 401-403 split ------------------------------


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic abc", "abc", "Token xyz"])
def test_missing_or_wrong_scheme_is_403(client, header) -> None:
    headers = {} if header is None else {"Authorization": header}
    response = client.post("/v1/systemone", json=VALID_BODY, headers=headers)
    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "error_type": "authentication_error",
            "message": response.json()["detail"]["message"],
        }
    }


def test_invalid_token_is_401(client) -> None:
    response = client.post("/v1/systemone", json=VALID_BODY, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.json()["detail"]["error_type"] == "authentication_error"


def test_401_advertises_how_to_authenticate(client) -> None:
    """RFC 9110 §15.5.2 says a 401 MUST carry WWW-Authenticate.

    The live jev API omits it, so this is a deliberate deviation *towards* the
    standard. It cannot break a client: an added response header is not part of any
    request/response body.
    """
    response = client.post("/v1/systemone", json=VALID_BODY, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert "www-authenticate" in {name.lower() for name in response.headers}


def test_403_does_not_advertise_a_challenge(client) -> None:
    """No credential was offered, so there is nothing to challenge."""
    response = client.post("/v1/systemone", json=VALID_BODY)
    assert response.status_code == 403
    assert "www-authenticate" not in {name.lower() for name in response.headers}


def test_the_error_message_says_what_to_do(client) -> None:
    """Plain, actionable text -- not a stack trace and not a status code."""
    message = client.post("/v1/systemone", json=VALID_BODY).json()["detail"]["message"]
    assert "Authorization: Bearer" in message
    assert "Traceback" not in message


def test_authentication_covers_both_endpoints(client) -> None:
    assert client.post("/v1/systemone", json=VALID_BODY).status_code == 403
    assert client.get("/v1/models").status_code == 403


def test_health_endpoints_are_not_authenticated(client) -> None:
    """A container runtime must be able to probe liveness without a token."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_second_configured_key_also_works(settings) -> None:
    """Comma-separated keys exist so a token can be rotated without downtime."""
    import dataclasses

    from fastapi.testclient import TestClient

    from decis.app import create_app

    rotated = dataclasses.replace(settings, api_keys=(*settings.api_keys, "second-key"))
    with TestClient(create_app(rotated)) as test_client:
        for key in ("test-token-do-not-use-in-production", "second-key"):
            assert (
                test_client.post(
                    "/v1/systemone", json=VALID_BODY, headers={"Authorization": f"Bearer {key}"}
                ).status_code
                == 200
            )
        assert (
            test_client.post(
                "/v1/systemone", json=VALID_BODY, headers={"Authorization": "Bearer third-key"}
            ).status_code
            == 401
        )


# --- AGENTS.md §3-13: auth precedes validation -------------------------------


def test_auth_is_checked_before_the_body(client) -> None:
    """The security-relevant ordering: no credential must not leak validation detail."""
    response = client.post("/v1/systemone", json={"this": "is not a valid request"})
    assert response.status_code == 403, "an unauthenticated malformed body must be 403, not 422"
    assert isinstance(response.json()["detail"], dict)


def test_malformed_body_with_a_valid_token_is_422(client, auth) -> None:
    response = client.post("/v1/systemone", json={"this": "is not a valid request"}, headers=auth)
    assert response.status_code == 422


def test_auth_is_checked_before_the_size_limit_is_enforced(client) -> None:
    """Both are pre-dispatch; the credential check comes first."""
    huge = "x" * (3 * 1024 * 1024)
    response = client.post("/v1/systemone", content=huge, headers={"Content-Type": "application/json"})
    assert response.status_code == 403


# --- the two body shapes -----------------------------------------------------


def test_validation_errors_use_the_list_shape(client, auth) -> None:
    response = client.post("/v1/systemone", json={"state": "x"}, headers=auth)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    for entry in detail:
        assert set(entry) <= {"loc", "msg", "type", "input"}
        assert isinstance(entry["loc"], list)
        assert isinstance(entry["msg"], str)


def test_an_unknown_model_uses_the_validation_shape(client, auth, noul_request) -> None:
    """A registry miss is a request problem, so it looks like a validation failure."""
    response = client.post("/v1/systemone", json=dict(noul_request, model="nope"), headers=auth)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_oversized_body_is_rejected_before_it_is_read(client, auth) -> None:
    response = client.post(
        "/v1/systemone",
        content="x" * (3 * 1024 * 1024),
        headers={**auth, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["error_type"] == "request_too_large"
    assert "DECIS_MAX_REQUEST_BYTES" in response.json()["detail"]["message"]


def test_unknown_path_is_404_and_unknown_method_is_405(client, auth) -> None:
    """FastAPI defaults, matching the live API's `{"detail": "..."}` string shape."""
    not_found = client.get("/v1/nonexistent", headers=auth)
    assert not_found.status_code == 404
    assert isinstance(not_found.json()["detail"], str)

    wrong_method = client.get("/v1/systemone", headers=auth)
    assert wrong_method.status_code == 405
    assert isinstance(wrong_method.json()["detail"], str)


# --- AGENTS.md §3-15: the request id ----------------------------------------


def test_every_response_carries_a_well_formed_request_id(client, auth, mixed_request) -> None:
    responses = [
        client.get("/healthz"),
        client.get("/readyz"),
        client.get("/v1/models", headers=auth),
        client.post("/v1/systemone", json=mixed_request, headers=auth),
        client.post("/v1/systemone", json=mixed_request),  # 403
        client.post("/v1/systemone", json=mixed_request, headers={"Authorization": "Bearer no"}),  # 401
        client.post("/v1/systemone", json={"bad": "body"}, headers=auth),  # 422
        client.get("/v1/nope", headers=auth),  # 404
    ]
    for response in responses:
        request_id = response.headers.get("x-typesafe-request-id")
        assert request_id is not None, f"{response.request.method} {response.request.url} is missing the header"
        assert REQUEST_ID.match(request_id), request_id


def test_request_ids_are_unique_per_request(client, auth, noul_request) -> None:
    seen = {
        client.post("/v1/systemone", json=noul_request, headers=auth).headers["x-typesafe-request-id"]
        for _ in range(10)
    }
    assert len(seen) == 10


def test_the_request_id_appears_in_the_log(client, auth, noul_request, caplog) -> None:
    """AGENTS.md §3-10: the id a client quotes must be findable in our logs."""
    import logging

    with caplog.at_level(logging.INFO, logger="decis"):
        response = client.post("/v1/systemone", json=noul_request, headers=auth)
    request_id = response.headers["x-typesafe-request-id"]
    assert any(request_id in record.getMessage() for record in caplog.records)
