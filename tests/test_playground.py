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
import re
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


# --- the way back, and the way out ------------------------------------------------
#
# Every page carries the same two shell links, mounted by i18n.js: back to the playground
# from a game, and out to the repository from anywhere. They are mounted rather than
# written into each page because the repository URL has one home (AGENTS.md §2) -- a
# fourth copy of it in a fourth page is a copy that drifts.


@pytest.mark.parametrize("name", PAGES)
def test_every_page_links_to_the_repository(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert "data-repo-link" in text, f"{name} has no link to the repository"
    # The URL itself lives in the shell, so a page that repeats it is the second home.
    assert "github.com/kingfs/Decis" not in text, f"{name} hardcodes the repository URL"


def test_the_repository_url_has_one_home() -> None:
    """`i18n.js` is where the project's URL is written down, and it is written once."""
    text = (WEB / "i18n.js").read_text(encoding="utf-8")
    assert text.count('REPO_URL = "https://github.com/kingfs/Decis"') == 1
    assert "data-repo-link" in text, "the mount point is no longer recognised"
    assert "data-back-link" in text, "the way back is no longer recognised"


@pytest.mark.parametrize("name", ("snake.html", "dino.html", "tetris.html"))
def test_every_game_has_a_way_back_to_the_playground(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert "data-back-link" in text, f"{name} has no way back to the playground"


def test_the_index_does_not_link_back_to_itself() -> None:
    """The index is where the back links point, so it has none of its own."""
    assert "data-back-link" not in (WEB / "index.html").read_text(encoding="utf-8")


def test_the_index_credits_the_projects_it_borrowed_from() -> None:
    """The page says who made the games and who answers their questions.

    Both halves matter: the games are adapted from `djev-run` (and tetris also credits
    `jev-tetris`), and the API and inference are Decis's, not the playground's. Saying so
    on the page is cheaper than a reader guessing.
    """
    text = (WEB / "index.html").read_text(encoding="utf-8")
    assert "github.com/taeold/djev-run" in text
    assert "credits.core" in text and "credits.title" in text
    # And the credit line points at the repository through the shared mount, not a copy.
    assert "github.com/kingfs/Decis" not in text


def test_tetris_sizes_its_question_from_one_named_budget() -> None:
    """The option count is a capacity decision, so it lives in exactly one place.

    Laya refuses a question over `head_max_len` rather than truncating it (AGENTS.md §3-9),
    and a checkpoint that declares no limits falls back to 192 tokens -- where five terse
    placements fit and six do not (measured with the engine's own `measure()`: worst head
    185 over 26 real questions, ~207 at six). The count is load-bearing in the API request,
    in the local simulation the page compares itself against, and in the row count of its own
    panel, and they have to agree or they are answering different questions.

    This is a text-level guard on purpose: the only part of the budget the weightless suite
    can check is that the number is not written into the page. The budget itself needs
    weights, so it is measured rather than tested.
    """
    text = (WEB / "tetris.html").read_text(encoding="utf-8")
    assert text.count("const PLACEMENT_SHORTLIST = ") == 1, "the shortlist constant was duplicated or renamed"
    for hardcoded in ("slice(0, 6)", "slice(0, 16)", "slice(0, 5)"):
        assert hardcoded not in text, f"tetris.html hardcodes the shortlist as {hardcoded}"
    assert text.count("PLACEMENT_SHORTLIST") >= 4, "the constant is declared but no longer used"
    # The page used to carry its own copy of the count in a "last call" panel, and when the
    # shortlist was cut from six to five that label kept a literal `6`: the page went on
    # claiming one option more than it sent (AGENTS.md §9). There is no such label now --
    # the shared I/O console prints the body that actually went on the wire -- so what is
    # left to guard is that the request still sizes itself from the constant, and that the
    # console is fed the request itself rather than a summary of it.
    assert "GameShell.observe({ request," in text, "the console is no longer fed the request as sent"
    # One home for "which options are asked": the request, the page's own simulation, its
    # local heuristic and the panel's fallback ranking all go through `shortlistPlacements`.
    assert text.count("function shortlistPlacements(") == 1, "the shortlist is built in more than one place"
    calls = [
        line
        for line in text.splitlines()
        if "shortlistPlacements(placements)" in line and not line.lstrip().startswith("function ")
    ]
    assert len(calls) == 4, f"there are {len(calls)} call sites instead of four: {calls}"
    body = text.split("function shortlistPlacements(", 1)[1].split("\n}", 1)[0]
    assert "PLACEMENT_SHORTLIST" in body, "the request no longer sizes its options from the constant"
    assert "slice(0, PLACEMENT_SHORTLIST)" in body, "the budget is no longer applied where the shortlist is built"
    # **Every rotation gets a slot.** Sorting by the local heuristic alone collapses to one
    # rotation on a flat board -- five options that were all `rot2`, so the model was only
    # ever choosing a column and the game looked like it could not rotate (AGENTS.md).
    assert "p.rotation" in body, "the shortlist no longer looks at rotations"
    # The panel draws one row per option from the same constant: a six-row panel next to a
    # five-option question is the literal-6 bug this page has already had once.
    assert "i < 6" not in text, "the placement panel is back to a hardcoded row count"
    # Only the shortlist's own values, not every clamp in the file: `Math.min(100, pct)` is
    # arithmetic, `Math.min(6, …)` is the shortlist written out a second time.
    leftover = re.search(r"Math\.min\(\s*(?:5|6|16)(?!\d)", text)
    assert leftover is None, f"a numeric option count survived as a literal: {leftover and leftover.group(0)}"


# --- one design system, one i18n, four pages ---------------------------------------
#
# The pages are separately authored but must not be separately designed or separately
# translated. Both mechanisms have one home (`theme.css`, `i18n.js`) and every page uses
# it; the guards below are what keep a fourth page from growing its own palette or its own
# half-finished dictionary. A browser can check the result (`.scratch/webcheck.py` reports
# missing strings, unapplied ones, overflow and JS errors) but it cannot run in CI, so the
# shape is asserted here and the rendering is checked by hand.

#: Tokens a page would be redefining the shared palette with. A page may add its own
#: layout variables; it may not restate these.
THEME_TOKENS = ("--bg:", "--ink:", "--muted:", "--line:", "--accent:", "--surface:", "--radius:")


@pytest.mark.parametrize("name", PAGES)
def test_every_page_uses_the_shared_design_system(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert 'href="/theme.css"' in text, f"{name} does not link the shared stylesheet"
    assert 'src="/i18n.js"' in text, f"{name} does not load the shared i18n"
    assert "data-lang-switch" in text, f"{name} has no language switch"
    # A page that restates the palette is the second implementation this project forbids.
    style = text.split("<style>", 1)[-1].split("</style>", 1)[0]
    for token in THEME_TOKENS:
        assert token not in style, f"{name} redefines {token} instead of using theme.css"


@pytest.mark.parametrize("name", PAGES)
def test_every_page_marks_up_its_translatable_text(name: str) -> None:
    """Static text is tagged; a page with no tags is a page nobody translated."""
    text = (WEB / name).read_text(encoding="utf-8")
    assert text.count("data-i18n=") >= 8, f"{name} has almost no tagged strings"
    assert "I18N.add(" in text, f"{name} declares no strings of its own"


def test_the_shared_assets_are_served(playground_url: str) -> None:
    """`theme.css`, `i18n.js` and `game.js` are served by the same file rule as the pages."""
    for path, content_type in (
        ("/theme.css", "text/css"),
        ("/i18n.js", "text/javascript"),
        ("/game.js", "text/javascript"),
    ):
        status, headers, body = get(playground_url + path)
        assert status == 200, path
        assert headers["content-type"].startswith(content_type), path
        assert len(body) > 500, path
    # The rule that serves them still refuses to walk out of the web directory.
    assert get(playground_url + "/../server.py")[0] == 404


# --- one shell for the three games --------------------------------------------------
#
# The three games are three boards around one API call, so the things that are not the
# board -- the manual/AI switch, the inference panel, the I/O console, the engine chip and
# the keyboard shortcuts -- have exactly one implementation (`game.js`) and every game page
# uses it (AGENTS.md §2). These guards are textual because CI has no browser; the rendering
# and the interaction are audited for real with `.scratch/final_sweep.py` (every page, both
# languages, three viewport widths) and `.scratch/uisweep.py` (switch, keys, telemetry).

GAMES = ("snake.html", "dino.html", "tetris.html")

#: What `game.js` paints, so a page must provide every one of these or the shell draws into
#: a hole. The switch, the telemetry panel and the console are mount points, not ids.
SHELL_MOUNTS = ("data-ai-switch", "data-telemetry", "data-io-console")
SHELL_IDS = ("game-dot", "game-status-text", "btn-start", "btn-reset", "dot", "status-text")


@pytest.mark.parametrize("name", GAMES)
def test_every_game_mounts_the_shared_shell(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert 'src="/game.js"' in text, f"{name} does not load the shared game shell"
    for mount in SHELL_MOUNTS:
        assert mount in text, f"{name} has no {mount}"
    for element_id in SHELL_IDS:
        assert f'id="{element_id}"' in text, f"{name} has no #{element_id} for the shell to paint"
    assert "GameShell.init(" in text, f"{name} never initialises the shell"
    # Every page talks to the playground's own proxy; a page that renders its own switch,
    # its own engine poll or its own call console is the second implementation §2 forbids.
    assert 'id="mode-select"' not in text, f"{name} grew its own mode control"
    assert "refreshEngine(" not in text, f"{name} polls the engine itself instead of using game.js"
    assert "GameShell.mode()" in text, f"{name} does not ask the shell which mode is selected"
    # The two panels about the *call* are one row under the game area (`.game-under` in
    # theme.css): telemetry, then the console, each mounted once. The console prints the last
    # request and the last response side by side, which is what this playground is for, so it
    # does not live in the narrow readout column next to the board.
    assert text.count('<div class="game-under">') == 1, f"{name} has no single call-panel row"
    assert text.count("data-telemetry") == 1 and text.count("data-io-console") == 1, name
    under = text.index('<div class="game-under">')
    assert text.index('class="game-grid"') < under < text.index("data-telemetry") < text.index("data-io-console"), (
        f"{name} puts the call panels somewhere other than under the board"
    )


def test_the_shell_opens_the_console_and_each_page_leaves_it_alone() -> None:
    """The console starts open, and that is the shell's decision.

    The call is what the playground is showing, so the panels under the board start
    expanded; a reader who wants to watch the board is one click from a summary line. The
    initial state lives in `game.js`, which builds the panel -- three pages each carrying an
    `open` attribute would be three opinions about one thing (AGENTS.md §2).
    """
    shell = (WEB / "game.js").read_text(encoding="utf-8")
    assert "host.open = true;" in shell, "the shell no longer opens the console it builds"
    for name in GAMES:
        text = (WEB / name).read_text(encoding="utf-8")
        tag = re.search(r"<details[^>]*data-io-console[^>]*>", text)
        assert tag, f"{name} has no console element"
        assert " open" not in tag.group(0), f"{name} decides the console's initial state itself"


@pytest.mark.parametrize("name", GAMES)
def test_no_game_reports_a_performance_number_it_did_not_measure(name: str) -> None:
    """Latency, tokens and throughput are measured by the shell or not shown at all.

    AGENTS.md §8: a number in the interface has to come from a measurement. dino used to
    draw two charts with placeholder latencies (116 ms / 139 ms) and a "benchmark
    comparison" it had never run; those are gone, and this is what keeps them gone.
    """
    text = (WEB / name).read_text(encoding="utf-8")
    for placeholder in ("116 ms", "139 ms", "Benchmark comparison", "m-ms", "m-pipeline"):
        assert placeholder not in text, f"{name} still carries {placeholder!r}"
    # The shell is the only thing that counts a call or a token.
    assert "GameShell.observe(" in text, f"{name} never reports its call to the shell"


def test_the_game_bar_is_the_same_on_every_game() -> None:
    """One layout: title, status, score strip, switch, start/reset -- in that order.

    The games differ in their numbers and their extra options; the primary bar is what a
    person reads first, and it has to look the same on all three or the playground reads
    as three different toys (the state this replaces).
    """
    bars = {}
    for name in GAMES:
        text = (WEB / name).read_text(encoding="utf-8")
        bar = text.split('<div class="game-bar">', 1)[1].split("</div>\n\n", 1)[0]
        bars[name] = [part.strip() for part in re.findall(r'class="(game-id|score-strip|top-actions)"', bar)]
        assert text.count('class="game-bar"') == 1, name
        assert 'class="game-title"' in bar, name
        assert 'id="game-status-text"' in bar, name
        assert 'class="metric-val"' in bar, name
    assert len(set(map(tuple, bars.values()))) == 1, bars
    # The three pages share one title style, so the game's name is the whole <h1> and the
    # engine is announced once, by the chip in the app bar.
    for name in GAMES:
        text = (WEB / name).read_text(encoding="utf-8")
        title = text.split('class="game-title"', 1)[1].split("</h1>", 1)[0]
        assert "System One" not in title, f"{name} repeats the engine in the game title"


def test_the_app_bar_is_the_same_on_every_page() -> None:
    """The chip, the nav and the language switch are the shell's, not a page's."""
    heads = {}
    for name in PAGES:
        text = (WEB / name).read_text(encoding="utf-8")
        head = text.split('<header class="app-bar">', 1)[1].split("</header>", 1)[0]
        # Three things may differ, and only three: which nav link is the current page, the
        # index page's own id on the chip (the games use the shared one), and the way back,
        # which the index deliberately does not carry -- it is what the back links point at
        # (`test_the_index_does_not_link_back_to_itself`).
        head = (
            head.replace(' aria-current="page"', "")
            .replace(' id="status-chip"', "")
            .replace("      <a data-back-link></a>\n", "")
        )
        heads[name] = head
    assert len(set(heads.values())) == 1, "the app bar differs between pages"
    # ...and it names the page you are on. The index is the home page, so none of its links
    # is "current", which is why it is excluded from this half.
    for name in GAMES:
        text = (WEB / name).read_text(encoding="utf-8")
        bar = text.split('<header class="app-bar">', 1)[1].split("</header>", 1)[0]
        assert "aria-current=" in bar, name


def test_the_pages_are_served_without_a_stale_cache(playground_url: str) -> None:
    """Editing a page during development must not need a hard refresh."""
    status, headers, _ = get(playground_url + "/index.html")
    assert status == 200
    assert headers["cache-control"] == "no-store"


# --- the dictionaries ---------------------------------------------------------------
#
# Parsed out of the JavaScript rather than duplicated here: a test that carried its own
# copy of the strings would stay green while the page said something else (AGENTS.md §9).
#
# The scanner is deliberately small -- the dictionaries are flat maps of string to string,
# which is all it has to read. A nested value is skipped rather than interpreted, so the
# day one of these files grows structure the test fails loudly instead of misreading it.


def _js_blank(text: str, index: int, *, commas: bool = False) -> int:
    """Advance past whitespace and JS comments.

    The pages are written by people: a `//` note inside the dictionary is normal, and the
    scanner has to read the strings rather than choke on the prose around them.
    """
    separators = " \t\r\n," if commas else " \t\r\n"
    while index < len(text):
        if text[index] in separators:
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index)
            index = len(text) if end < 0 else end + 2
        else:
            return index
    return index


def _js_string(text: str, index: int) -> tuple[str, int]:
    """The JS string literal at `index`, and the index just after it."""
    quote = text[index]
    assert quote in "\"'", f"expected a string at {index}: {text[index : index + 24]!r}"
    out: list[str] = []
    i = index + 1
    while i < len(text):
        char = text[i]
        if char == "\\":
            escape = text[i + 1]
            if escape == "u":
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
            else:
                out.append({"n": "\n", "t": "\t"}.get(escape, escape))
                i += 2
            continue
        if char == quote:
            return "".join(out), i + 1
        out.append(char)
        i += 1
    raise AssertionError("unterminated string")


def _js_braces(text: str, open_index: int) -> str:
    """The `{...}` that starts at `open_index`, nested braces included."""
    assert text[open_index] == "{", text[open_index : open_index + 24]
    depth = 0
    i = open_index
    while i < len(text):
        char = text[i]
        if char in "\"'":
            _, i = _js_string(text, i)
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            i = _js_blank(text, i)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : i + 1]
        i += 1
    raise AssertionError("unbalanced braces")


def _js_pairs(literal: str) -> dict[str, str]:
    """The top-level `"key": "value"` entries of a flat object literal."""
    pairs: dict[str, str] = {}
    i = 1  # past the opening brace
    end = len(literal) - 1
    while i < end:
        i = _js_blank(literal, i, commas=True)
        if i >= end:
            break
        key, i = _js_string(literal, i)
        i = _js_blank(literal, i)
        assert literal[i] == ":", literal[i - 20 : i + 20]
        i = _js_blank(literal, i + 1)
        if literal[i] not in "\"'":
            # A nested array or object: take its text as-is rather than pretending to
            # understand it, so a page that grows structure fails the comparison loudly.
            if literal[i] in "[{":
                nested = _js_braces(literal, i) if literal[i] == "{" else None
                pairs[key] = nested if nested is not None else literal[i : literal.index("]", i) + 1]
                i += len(pairs[key])
                continue
            raise AssertionError(f"unsupported value for {key!r}: {literal[i : i + 24]!r}")
        value, i = _js_string(literal, i)
        pairs[key] = value
    return pairs


def _dictionary(text: str, marker: str) -> dict[str, dict[str, str]]:
    """The `{ en: {...}, zh: {...} }` written after `marker`."""
    start = text.index(marker) + len(marker)
    while text[start] in " \t\r\n(":
        start += 1
    outer = _js_braces(text, start)
    found: dict[str, dict[str, str]] = {}
    for language in ("en", "zh"):
        match = re.search(rf"(?<![\w$]){language}\s*:\s*\{{", outer)
        assert match, f"no {language!r} dictionary after {marker}"
        found[language] = _js_pairs(_js_braces(outer, match.end() - 1))
    return found


def _page_strings(name: str) -> dict[str, dict[str, str]]:
    return _dictionary((WEB / name).read_text(encoding="utf-8"), "I18N.add(")


def _shell_strings() -> dict[str, dict[str, str]]:
    return _dictionary((WEB / "i18n.js").read_text(encoding="utf-8"), "var SHELL = ")


#: Strings deliberately identical in both languages because they are identifiers rather
#: than prose: a product name, or a value the model itself sees. Being on this list is a
#: decision, which is the point -- the test makes someone write it down.
KEPT_IN_ENGLISH = {"nav.repo"}


def _reads_as_prose(value: str) -> bool:
    """A sentence or a phrase, as opposed to a label or an identifier."""
    words = [word for word in value.split() if any(char.isalpha() for char in word)]
    return len(words) >= 2 or len(value) >= 16


@pytest.mark.parametrize("name", PAGES)
def test_every_page_declares_both_languages_completely(name: str) -> None:
    """Every key in both, and nothing left in English that reads as a sentence."""
    english, chinese = (_page_strings(name)[language] for language in ("en", "zh"))
    assert len(english) >= 10, f"{name} declares only {len(english)} strings"
    assert not set(english) - set(chinese), f"{name} has no Chinese for: {sorted(set(english) - set(chinese))}"
    assert not set(chinese) - set(english), f"{name} translates unknown keys: {sorted(set(chinese) - set(english))}"
    untranslated = sorted(k for k, v in english.items() if chinese[k] == v and _reads_as_prose(v))
    assert set(untranslated) <= KEPT_IN_ENGLISH, f"{name} left prose in English: {untranslated}"


def test_the_shell_strings_are_complete() -> None:
    english, chinese = (_shell_strings()[language] for language in ("en", "zh"))
    assert set(english) == set(chinese)
    for key, value in english.items():
        if chinese[key] == value and _reads_as_prose(value):
            assert key in KEPT_IN_ENGLISH, f"i18n.js left {key!r} in English"


def _i18n_argument(text: str, open_index: int) -> str:
    """The source between the parentheses of `I18N.t(...)`, comments and strings skipped."""
    depth = 0
    i = open_index  # at '('
    while i < len(text):
        char = text[i]
        if char in "\"'":
            _, i = _js_string(text, i)
            continue
        if char == "`":
            i = text.index("`", i + 1) + 1
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            i = _js_blank(text, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
        i += 1
    raise AssertionError("unbalanced call")


def _i18n_head(argument: str) -> str:
    """The key expression only: the first argument, before any `{...}` of interpolation vars."""
    depth = 0
    for i, char in enumerate(argument):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            return argument[:i]
    return argument


def _i18n_usages(text: str) -> tuple[set[str], set[str]]:
    """The string keys a page asks for, and the families it builds at runtime.

    Families are things like `I18N.t("dir." + move)` or ``I18N.t(`health.${level}`)``: the
    page names them in pieces, so the test can only ask that the family exists at all.
    """
    literals: set[str] = set()
    families: set[str] = set()
    for match in re.finditer(r'data-i18n(?:-title|-aria-label)?="([^"]+)"', text):
        literals.add(match.group(1))
    for match in re.finditer(r"I18N\.t\(", text):
        head = _i18n_head(_i18n_argument(text, match.end() - 1))
        for template in re.findall(r"`([^`]*)`", head):
            if "${" in template:
                families.add(template.split("${", 1)[0])
            else:
                literals.add(template)
        for literal in re.findall(r"[\"']([^\"']+)[\"']", head):
            if literal.endswith("."):
                families.add(literal)
            else:
                literals.add(literal)
    literals = {key for key in literals if re.fullmatch(r"[a-z][A-Za-z0-9_.]*", key)}
    return literals, families


@pytest.mark.parametrize("name", PAGES)
def test_every_string_a_page_asks_for_is_declared(name: str) -> None:
    """`I18N.t` falls back to the key, so a dropped string shows up as `desc.holes.none`.

    That is how the unified layout lost the placement-row descriptions: the code kept
    asking for `desc.holes.none` while the rewritten dictionary no longer had it. The
    interface still worked, so only a guard like this one finds it.
    """
    declared = set(_page_strings(name)["en"]) | set(_shell_strings()["en"])
    literals, families = _i18n_usages((WEB / name).read_text(encoding="utf-8"))
    assert not literals - declared, f"{name} asks for undeclared strings: {sorted(literals - declared)}"
    for family in families:
        assert any(key.startswith(family) for key in declared), (
            f"{name} builds {family!r} at runtime but declares no {family}* string"
        )


def test_the_language_comes_from_the_browser_and_can_be_switched() -> None:
    """Detection order, persistence and the switch -- the mechanism, not the rendering.

    The rendering is checked in a real browser by `.scratch/webcheck.py`; what has to hold
    in the weightless suite is that a Chinese browser gets Chinese without touching
    anything (the whole point of the feature), that the choice sticks, that `?lang=` works
    for a shared link, and that text is assigned as text.
    """
    text = (WEB / "i18n.js").read_text(encoding="utf-8")
    assert "navigator.languages" in text and "navigator.language" in text
    assert 'indexOf("zh")' in text, "the Chinese tags are not recognised"
    assert "localStorage" in text, "the choice is not remembered"
    assert 'get("lang")' in text, "a link cannot name a language"
    assert "document.documentElement.lang" in text, "the page language is never set"
    # Assigned as text, never as markup: the comment in the file may name the alternative.
    assert not re.search(r"\.innerHTML\s*=", text), "translated text must be text, not markup"


def test_the_dictionary_scanner_reads_javascript_not_just_json() -> None:
    """The scanner is the thing that reads the pages, so it gets its own test.

    It was written for JSON-shaped literals and the first page to carry a `//` note inside
    its dictionary broke it. A `//` inside a *string* is the case that makes this
    interesting: it has to be kept, while the same two characters outside a string are a
    comment to skip.
    """
    sample = """I18N.add({
      // a note above the block
      en: {
        "a": "one", // trailing note
        /* block */ "b": "two",
        "c": "three, with a comma and a // not-a-comment",
      },
      zh: { "a": "\\u4e00", "b": "二", "c": "三" },
    });"""
    strings = _dictionary(sample, "I18N.add(")
    assert strings["en"] == {
        "a": "one",
        "b": "two",
        "c": "three, with a comma and a // not-a-comment",
    }
    assert strings["zh"] == {"a": "一", "b": "二", "c": "三"}


def test_the_scanner_refuses_a_dictionary_with_no_chinese() -> None:
    """A page that declares one language is a page that was not translated."""
    with pytest.raises(AssertionError, match="zh"):
        _dictionary('I18N.add({ en: { "a": "one" } });', "I18N.add(")
