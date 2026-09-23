"""The playground is a second server, and its two jobs are testable without any weights.

It has to serve the pages, and it has to be the only place the API key exists: the browser
posts to `/v1/systemone` on the playground's origin, and the playground attaches the bearer
token and forwards. What makes that "just connect to the engine" is that it *asks* `/readyz`
on a list of candidates instead of being told which engine `.env` selected -- a container
that is not running does not resolve its Compose name at all.

So the tests here stand up two real HTTP servers on loopback: a fake engine that records
what it was asked, and the playground under test. Nothing imports `decis`, which is the
point -- the playground image does not contain it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from playground import server as playground

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "playground" / "web"
PAGES = ("index.html", "snake.html", "dino.html", "tetris.html")

#: Bypasses the proxy environment for the same reason `playground/server.py` does: a
#: loopback test that asks `HTTP_PROXY` for `127.0.0.1` gets a 502 on a host that has one.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --- a fake engine ---------------------------------------------------------------


class FakeEngine(BaseHTTPRequestHandler):
    """A stand-in for `decis serve`: `/readyz`, and a `/v1/systemone` that answers."""

    protocol_version = "HTTP/1.1"
    ready = True
    engine_id = "laya-multilingual"
    status = 200
    seen: list[dict] = []

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, status: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # the base class decides the name
        if self.path != "/readyz":
            self._json(404, {"detail": {"error_type": "not_found", "message": self.path}})
            return
        if self.ready:
            self._json(200, {"status": "ready", "engine": self.engine_id})
        else:
            self._json(503, {"status": "loading", "engine": self.engine_id})

    def do_POST(self) -> None:  # the base class decides the name
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        type(self).seen.append(
            {
                "path": self.path,
                "body": json.loads(raw),
                "authorization": self.headers.get("authorization"),
            }
        )
        self._json(
            self.status,
            {
                "model": f"decis/{self.engine_id}@0.0.1",
                "answers": {
                    "move": {"type": "choice", "choice": "up", "confidence": 0.6, "probabilities": {"up": 0.6}}
                },
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
            extra={"x-typesafe-request-id": "req_" + "ab" * 16},
        )


class FakeEngineServer(ThreadingHTTPServer):
    daemon_threads = True

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def engine() -> Iterator[FakeEngineServer]:
    FakeEngine.seen = []
    FakeEngine.ready = True
    FakeEngine.engine_id = "laya-multilingual"
    FakeEngine.status = 200
    server = FakeEngineServer(("127.0.0.1", 0), FakeEngine)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


# --- the playground under test ---------------------------------------------------


@pytest.fixture
def playground_url(engine: FakeEngineServer) -> Iterator[str]:
    config = playground.Config(
        host="127.0.0.1",
        port=0,
        candidates=(engine.base_url,),
        api_key="secret-token",
        web_dir=WEB,
        probe_timeout_s=1.0,
    )
    server = playground.build_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with DIRECT.open(url, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def post(url: str, payload: object) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with DIRECT.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


# --- serving the pages ------------------------------------------------------------


@pytest.mark.parametrize("name", PAGES)
def test_every_page_is_served_at_its_route_and_at_its_filename(playground_url: str, name: str) -> None:
    """The pages link to each other by route, and the reference documents `/snake` too."""
    route = "/" if name == "index.html" else f"/{name.removesuffix('.html')}"
    for path in (route, f"/{name}"):
        status, headers, body = get(f"{playground_url}{path}")
        assert status == 200, path
        assert headers["content-type"].startswith("text/html"), (path, headers)
        assert b"<html" in body.lower(), path


def test_an_unknown_page_is_a_404_with_the_engine_error_shape(playground_url: str) -> None:
    status, _, body = get(f"{playground_url}/nope")
    assert status == 404
    assert json.loads(body)["detail"]["error_type"] == "not_found"


def test_a_path_traversal_never_reaches_a_file_outside_the_web_directory(playground_url: str) -> None:
    """`/../server.py` is the file next to the pages, so it must not be readable."""
    for path in ("/../server.py", "/..%2fserver.py", "/web/../../server.py"):
        status, _, _ = get(f"{playground_url}{path}")
        assert status == 404, path


# --- finding the engine ------------------------------------------------------------


def test_healthz_is_liveness_and_readyz_is_the_engine(playground_url: str, engine: FakeEngineServer) -> None:
    status, _, body = get(f"{playground_url}/healthz")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}

    status, _, body = get(f"{playground_url}/readyz")
    assert status == 200
    reported = json.loads(body)
    assert reported["engine"] == "laya-multilingual"
    assert reported["upstream"] == engine.base_url


def test_a_loading_engine_is_reported_as_still_searching(playground_url: str, engine: FakeEngineServer) -> None:
    """`--wait`-style behavior: the playground is up, the engine is not ready yet."""
    FakeEngine.ready = False
    status, _, body = get(f"{playground_url}/readyz")
    assert status == 503
    assert json.loads(body)["status"] == "searching"


def test_the_engine_is_found_by_probing_a_list_not_by_being_told(engine: FakeEngineServer) -> None:
    """The first candidate is dead; the second is the live engine.

    This is the whole "connect to whichever model started" behaviour: the caller does not
    have to keep an engine id in sync, and `laya-multilingual` not being up is not an error.
    """
    config = playground.Config(
        host="127.0.0.1",
        port=0,
        # `127.0.0.1:1` refuses immediately, which is what a stopped Compose service looks
        # like (its name does not resolve at all).
        candidates=("http://127.0.0.1:1", engine.base_url),
        api_key="secret-token",
        web_dir=WEB,
        probe_timeout_s=1.0,
    )
    server = playground.build_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        status, _, body = get(f"http://{host}:{port}/api/config")
        assert status == 200
        config_body = json.loads(body)
        assert config_body["ready"] is True
        assert config_body["engine"] == "laya-multilingual"
        assert config_body["upstream"] == engine.base_url
    finally:
        server.shutdown()
        server.server_close()


def test_an_explicit_upstream_is_tried_first(engine: FakeEngineServer) -> None:
    config = playground.Config(
        host="127.0.0.1",
        port=0,
        upstream=engine.base_url,
        # A candidate that would answer if it were consulted at all.
        candidates=(engine.base_url + "/not-this-one",),
        api_key="k",
        web_dir=WEB,
    )
    assert playground.Engine(config).candidates[0] == engine.base_url


# --- the proxy ---------------------------------------------------------------------


def test_the_proxy_attaches_the_token_and_rewrites_the_model(playground_url: str) -> None:
    """The browser never holds a key, and the page never has to know the engine's name.

    The pages as shipped ask for `jev-latest`; the engine answers to the id it reported at
    `/readyz`. Rewriting here is what makes one set of pages work under either profile --
    and it is why `DECIS_ACCEPT_FOREIGN_DEFAULTS` is not needed to use the playground.
    """
    payload = {"model": "jev-latest", "state": {"x": 1}, "questions": {"q": {"type": "noul"}}}
    status, headers, body = post(f"{playground_url}/v1/systemone", payload)
    assert status == 200, body
    assert headers["x-typesafe-request-id"] == "req_" + "ab" * 16

    assert len(FakeEngine.seen) == 1
    forwarded = FakeEngine.seen[0]
    assert forwarded["path"] == "/v1/systemone"
    assert forwarded["authorization"] == "Bearer secret-token"
    assert forwarded["body"]["model"] == "laya-multilingual"
    # Everything else is the page's request, byte for byte: the proxy is not a translator.
    assert forwarded["body"]["state"] == {"x": 1}
    assert forwarded["body"]["questions"] == {"q": {"type": "noul"}}
    assert json.loads(body)["answers"]["move"]["choice"] == "up"


def test_an_engine_error_is_forwarded_rather_than_masked(playground_url: str) -> None:
    """A 422 from the capacity check has to reach the page: that is how it explains itself.

    Turning it into a generic proxy error would hide the difference between "the model
    refused this question" and "the playground is broken".
    """
    FakeEngine.status = 422
    status, _, body = post(f"{playground_url}/v1/systemone", {"model": "jev-latest", "state": "s", "questions": {}})
    assert status == 422
    assert json.loads(body)["model"] == "decis/laya-multilingual@0.0.1"


def test_no_engine_yet_is_a_503_with_a_readable_message(playground_url: str) -> None:
    FakeEngine.ready = False
    status, _, body = post(f"{playground_url}/v1/systemone", {"model": "jev-latest", "state": "s", "questions": {}})
    assert status == 503
    detail = json.loads(body)["detail"]
    assert detail["error_type"] == "upstream_unavailable"
    assert "cold start" in detail["message"]


def test_the_proxy_refuses_a_body_that_is_not_a_json_object(playground_url: str) -> None:
    request = urllib.request.Request(
        f"{playground_url}/v1/systemone", data=b"not json", headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with DIRECT.open(request, timeout=5):
            raise AssertionError("a non-JSON body was accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 400
        assert json.loads(error.read())["detail"]["error_type"] == "invalid_request_error"


def test_only_systemone_is_proxied(playground_url: str) -> None:
    status, _, _ = post(f"{playground_url}/v1/models", {})
    assert status == 404


# --- configuration ----------------------------------------------------------------


def test_the_candidate_list_comes_from_the_environment() -> None:
    config = playground.Config.from_env(
        {
            "DECIS_PLAYGROUND_CANDIDATES": " http://a:8000 , http://b:8000/ ",
            "DECIS_PLAYGROUND_PORT": "9999",
            "DECIS_API_KEY": "k",
        }
    )
    assert config.candidates == ("http://a:8000", "http://b:8000")
    assert config.port == 9999
    assert config.api_key == "k"


def test_an_empty_candidate_list_falls_back_to_the_built_in_one() -> None:
    """`.env` sets the variable to an empty string by default; that must not mean "nowhere"."""
    config = playground.Config.from_env({"DECIS_PLAYGROUND_CANDIDATES": "", "DECIS_PLAYGROUND_UPSTREAM": ""})
    assert config.candidates == playground.DEFAULT_CANDIDATES
    assert config.upstream == ""


# --- the pages themselves ----------------------------------------------------------


@pytest.mark.parametrize("name", PAGES)
def test_the_pages_are_self_contained(name: str) -> None:
    """No external asset, so the playground works on a network that reaches nothing else.

    tetris loaded Google Fonts; a deployment behind a proxy or an air gap would render it
    slowly or not at all, for a font stack the CSS already falls back through.
    """
    text = (WEB / name).read_text(encoding="utf-8")
    for external in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.", "unpkg.com"):
        assert external not in text, f"{name} loads {external}"


@pytest.mark.parametrize("name", ("snake.html", "dino.html", "tetris.html"))
def test_every_game_asks_the_playground_for_its_decisions(name: str) -> None:
    """Same-origin, always: the endpoint the page calls is the one that attaches the key."""
    text = (WEB / name).read_text(encoding="utf-8")
    assert "/v1/systemone" in text, name
    # The reference project resolved a Cloud Run URL when opened from a file. That path
    # cannot carry the token, so it is gone.
    assert "CLOUD_RUN_URL" not in text, name
    assert "artifax" not in text, name


def test_tetris_sizes_its_question_from_one_named_budget() -> None:
    """The option count is a capacity decision, so it lives in exactly one place.

    Laya refuses a question over `head_max_len` rather than truncating it (AGENTS.md §3-9),
    and a checkpoint that declares no limits falls back to 192 tokens -- where five terse
    placements fit and six do not (measured with the engine's own `measure()`). The count is
    load-bearing in two places, the API request and the local simulation the page compares
    itself against, and they have to agree or the two are answering different questions.

    This is a text-level guard on purpose: the only part of the budget the weightless suite
    can check is that the number is not written into the page. The budget itself needs
    weights, so it is measured rather than tested.
    """
    text = (WEB / "tetris.html").read_text(encoding="utf-8")
    assert text.count("const PLACEMENT_SHORTLIST = ") == 1, "the shortlist constant was duplicated or renamed"
    for hardcoded in ("slice(0, 6)", "slice(0, 16)", "slice(0, 5)"):
        assert hardcoded not in text, f"tetris.html hardcodes the shortlist as {hardcoded}"
    assert text.count("PLACEMENT_SHORTLIST") >= 3, "the constant is declared but no longer used"
