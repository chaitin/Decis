"""The Decis playground: three browser games, and the one proxy that feeds them.

The games are standalone HTML files (`web/snake.html`, `web/dino.html`,
`web/tetris.html`) that need to do exactly one thing with the API: `POST
/v1/systemone`. This module serves them, and it *is* their `/v1/systemone`:
the request arrives from the browser on the playground's own origin, gets the
bearer token attached here, and is forwarded to whichever engine is actually
running.

Two consequences shape the whole file:

* **The token never reaches the browser.** A page cannot hold an API key
  without publishing it to anyone who opens developer tools, so the pages call
  this server and this server calls the engine. That also means the playground's
  port is as sensitive as the engine's -- whoever can reach it can decide.
  `docker-compose.yml` publishes it on every interface because that is what a
  deployment expects; `.env.example` says to change it when that is wrong.
* **The model name is decided here, not in the page.** A page asks with whatever
  name it likes (`jev-latest`, in the pages as shipped); this server rewrites
  `model` to the id of the engine `/readyz` reported. That is what makes the
  playground "just connect to whatever is up" instead of asking the user to keep
  an engine id in sync in four places. `DECIS_ACCEPT_FOREIGN_DEFAULTS` is
  therefore *not* needed to use the playground.

Which engine that is comes from `GET /readyz`, tried against a list of
candidates, first one that answers wins:

1. `DECIS_PLAYGROUND_UPSTREAM`, if it is set (an explicit answer, still probed);
2. otherwise `DECIS_PLAYGROUND_CANDIDATES`, whose default names both published
   engine services by their Compose service name plus `host.docker.internal` so
   a `decis serve` running on the host is found too.

A container that is not running does not resolve its Compose service name, so
"which engine is up" is answered by trying, not by reading `.env`. Nothing here
imports `decis`: this program is deliberately a few hundred lines of standard
library so that its image is a `python:3.13-slim` plus four HTML files, and so
that it can be served by an engine image's interpreter or by nothing at all.
Reading `os.environ` directly is fine for the same reason -- `decis.config` is
the canonical home for the *server's* environment (AGENTS.md §2), and this is
not the server.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import signal
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_logger = logging.getLogger("decis.playground")

#: Where the pages live. `Dockerfile` copies this directory next to `server.py`.
WEB_DIR = Path(__file__).resolve().parent / "web"

#: Tried in order when `DECIS_PLAYGROUND_UPSTREAM` is empty. The Compose service
#: names come first so that the documented `docker compose up` path never waits on
#: a DNS timeout for `host.docker.internal`; the loopback and host entries make the
#: same image useful when the engine runs outside Compose.
DEFAULT_CANDIDATES = (
    "http://laya-multilingual:8000",
    "http://kev-0.8b:8000",
    "http://host.docker.internal:8000",
    "http://127.0.0.1:8000",
)

#: `/snake` and `/snake.html` both work: the first is what the reference project
#: documents, the second is what a relative link from another page produces.
PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/playground": "index.html",
    "/snake": "snake.html",
    "/snake.html": "snake.html",
    "/dino": "dino.html",
    "/dino.html": "dino.html",
    "/tetris": "tetris.html",
    "/tetris.html": "tetris.html",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

#: A request larger than this is refused before it is read. The engine has its own
#: `DECIS_MAX_REQUEST_BYTES`; this only stops the proxy buffering an unbounded body.
MAX_REQUEST_BYTES = 4 * 1024 * 1024

#: Probes deliberately bypass the proxy environment. The engine is reached by its
#: Compose service name on the container network, and `urllib` would otherwise ask
#: `HTTP_PROXY` for it -- the same mistake that made a Kubernetes liveness probe
#: report a healthy server unhealthy (docs/design-review.md §2-D19). One opener,
#: built once, used for both the probe and the forwarded request.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class Config:
    """Everything this program reads from the environment, in one place."""

    host: str = "0.0.0.0"
    port: int = 8080
    upstream: str = ""
    candidates: tuple[str, ...] = DEFAULT_CANDIDATES
    api_key: str = ""
    #: How long one forwarded inference may take. Laya on CPU answers in seconds and
    #: the games only wait behind one request at a time, so this is generous on
    #: purpose: a proxy timeout that fires while the engine is still thinking turns a
    #: slow answer into a failed one.
    timeout_s: float = 120.0
    #: `/readyz` must answer fast or it is not the engine we are looking for.
    probe_timeout_s: float = 2.0
    web_dir: Path = WEB_DIR

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Config:
        env = os.environ if environ is None else environ
        raw = env.get("DECIS_PLAYGROUND_CANDIDATES", "").strip()
        candidates = tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())
        return cls(
            host=env.get("DECIS_PLAYGROUND_HOST", "0.0.0.0"),
            port=int(env.get("DECIS_PLAYGROUND_PORT", "8080")),
            upstream=env.get("DECIS_PLAYGROUND_UPSTREAM", "").strip().rstrip("/"),
            candidates=candidates or DEFAULT_CANDIDATES,
            api_key=env.get("DECIS_API_KEY", ""),
            timeout_s=float(env.get("DECIS_PLAYGROUND_TIMEOUT_S", "120")),
            probe_timeout_s=float(env.get("DECIS_PLAYGROUND_PROBE_TIMEOUT_S", "2")),
            web_dir=Path(env.get("DECIS_PLAYGROUND_WEB_DIR", "") or WEB_DIR),
        )


class Engine:
    """The engine this playground talks to, found by asking, not by configuration.

    The resolution is cached: `/readyz` is cheap, but the games send a request per
    decision and re-probing before each one would multiply that. A forwarded request
    that fails to connect invalidates the cache once, so an engine that restarts is
    found again without also restarting the playground.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._base_url: str | None = None
        self._engine_id: str | None = None

    @property
    def candidates(self) -> tuple[str, ...]:
        if self._config.upstream:
            return (self._config.upstream, *self._config.candidates)
        return self._config.candidates

    def _probe(self, base_url: str) -> str | None:
        """The engine id if `base_url` answers `/readyz` with 200, else None.

        A 503 is a real answer -- the engine is loading, or it failed to load -- and it
        carries the id, but it does not mean "ready", so it is not a match. The games
        are expected to be opened a few seconds after `docker compose up`; until then
        they say so instead of sending requests that would all be rejected.
        """
        try:
            with _DIRECT.open(f"{base_url}/readyz", timeout=self._config.probe_timeout_s) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        if not isinstance(body, dict) or body.get("status") != "ready":
            return None
        engine_id = body.get("engine")
        return engine_id if isinstance(engine_id, str) and engine_id else ""

    def resolve(self, *, refresh: bool = False) -> tuple[str, str] | None:
        """`(base_url, engine_id)`, or None while no candidate is ready."""
        with self._lock:
            if self._base_url is not None and not refresh:
                return self._base_url, self._engine_id or ""
            for base_url in self.candidates:
                engine_id = self._probe(base_url)
                if engine_id is not None:
                    if base_url != self._base_url or engine_id != self._engine_id:
                        _logger.info("engine %s is ready at %s", engine_id or "?", base_url)
                    self._base_url, self._engine_id = base_url, engine_id
                    return base_url, engine_id
            if self._base_url is not None:
                _logger.warning("no candidate answered /readyz; the engine may have stopped")
            self._base_url, self._engine_id = None, None
            return None

    def invalidate(self) -> None:
        with self._lock:
            self._base_url, self._engine_id = None, None


def _request(
    url: str, *, data: bytes | None, headers: dict[str, str], timeout: float
) -> tuple[int, dict[str, str], bytes]:
    """One direct HTTP call. Returns `(status, headers, body)` for any HTTP status.

    An error status is a normal answer here: the engine reports validation failures as
    4xx with a JSON body the games display, and rewriting that into a proxy error would
    hide the reason.
    """
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with _DIRECT.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


class Handler(BaseHTTPRequestHandler):
    """Static pages, the config endpoint, and the `/v1/systemone` proxy."""

    server_version = "decis-playground"
    protocol_version = "HTTP/1.1"

    config: Config
    engine: Engine

    # --- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # the base class names it `format`
        _logger.debug("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        # The pages are edited while the container runs in development, and the games
        # fetch their config per load; caching buys nothing and costs a stale page.
        self.send_header("cache-control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), CONTENT_TYPES[".json"])

    def _send_error_json(self, status: int, error_type: str, message: str) -> None:
        """The engine's own error shape, so a page can read one format for both."""
        self._send_json(status, {"detail": {"error_type": error_type, "message": message}})

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._send_error_json(400, "invalid_request_error", "content-length is not a number")
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json(
                413, "invalid_request_error", f"request body is larger than {MAX_REQUEST_BYTES} bytes"
            )
            return None
        return self.rfile.read(length) if length else b""

    # --- routing -----------------------------------------------------------

    def do_HEAD(self) -> None:  # the base class decides the name
        self.do_GET()

    def do_GET(self) -> None:  # the base class decides the name
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/readyz":
            found = self.engine.resolve(refresh=True)
            if found is None:
                self._send_json(503, {"status": "searching", "candidates": list(self.engine.candidates)})
                return
            base_url, engine_id = found
            self._send_json(200, {"status": "ready", "engine": engine_id, "upstream": base_url})
            return
        if path == "/api/config":
            self._send_json(200, self._config_payload())
            return
        page = self._page_for(path)
        if page is None:
            self._send_error_json(404, "not_found", f"no page at {path!r}")
            return
        self._send(200, page.read_bytes(), CONTENT_TYPES.get(page.suffix, "application/octet-stream"))

    def do_POST(self) -> None:  # the base class decides the name
        path = urllib.parse.urlparse(self.path).path
        if path != "/v1/systemone":
            self._send_error_json(404, "not_found", f"the playground does not proxy {path!r}")
            return
        body = self._read_body()
        if body is None:
            return
        self._proxy_systemone(body)

    def _config_payload(self) -> dict[str, object]:
        """What the pages need to describe the live setup without hardcoding it."""
        found = self.engine.resolve()
        engine_id = found[1] if found else ""
        # `model` is the id to put in a request: this server rewrites it anyway, but a
        # page that shows it should show what will really be asked.
        return {
            "ready": found is not None,
            "engine": engine_id or None,
            "model": engine_id or "jev-latest",
            "upstream": found[0] if found else None,
            "candidates": list(self.engine.candidates),
            "games": ["snake", "dino", "tetris"],
        }

    # --- the proxy ---------------------------------------------------------

    def _proxy_systemone(self, body: bytes) -> None:
        found = self.engine.resolve()
        if found is None:
            self._send_error_json(
                503,
                "upstream_unavailable",
                "No Decis engine answered /readyz yet. Wait for the model to finish loading "
                "(a CPU cold start takes a couple of minutes) and try again.",
            )
            return

        base_url, engine_id = found
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, "invalid_request_error", "the request body is not valid JSON")
            return
        if not isinstance(payload, dict):
            self._send_error_json(400, "invalid_request_error", "the request body is not a JSON object")
            return
        if engine_id:
            # The page may have asked for `jev-latest`; the engine answers to the id it
            # reported. Rewriting here is what makes the pages engine-agnostic.
            payload["model"] = engine_id
        forwarded = json.dumps(payload).encode("utf-8")

        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        try:
            status, response_headers, response_body = _request(
                f"{base_url}/v1/systemone", data=forwarded, headers=headers, timeout=self.config.timeout_s
            )
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            # The engine may have restarted onto a different address, so forget what we
            # knew and let the next request find it again.
            self.engine.invalidate()
            self._send_error_json(502, "upstream_error", f"{base_url} could not be reached: {error}")
            return

        extra = {}
        request_id = response_headers.get("x-typesafe-request-id") or response_headers.get("X-Typesafe-Request-Id")
        if request_id:
            extra["x-typesafe-request-id"] = request_id
        self._send(status, response_body, CONTENT_TYPES[".json"], extra)

    # --- static files -------------------------------------------------------

    def _page_for(self, path: str) -> Path | None:
        web_dir = self.config.web_dir.resolve()
        if path in PAGES:
            page = web_dir / PAGES[path]
            return page if page.is_file() else None
        # A file that is not a page (a future stylesheet, a favicon). `normpath` plus an
        # explicit containment check is what keeps `../server.py` from being served.
        relative = posixpath.normpath(path).lstrip("/")
        if relative.startswith(".."):
            return None
        candidate = (web_dir / relative).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(web_dir):
            return None
        return candidate


class PlaygroundServer(ThreadingHTTPServer):
    """The HTTP server, holding the engine it talks to.

    Subclassing is how the standard library's handler gets its state; the alternative is
    a module-level global, which makes two servers in one process (a test) share it.
    """

    daemon_threads = True
    engine: Engine
    config: Config


def build_server(config: Config) -> PlaygroundServer:
    engine = Engine(config)

    class BoundHandler(Handler):
        pass

    BoundHandler.config = config
    BoundHandler.engine = engine
    server = PlaygroundServer((config.host, config.port), BoundHandler)
    server.engine = engine
    server.config = config
    return server


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("DECIS_LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    server = build_server(config)
    found = server.engine.resolve()
    _logger.info(
        "playground on http://%s:%d (engine: %s)",
        config.host,
        config.port,
        f"{found[1]} at {found[0]}" if found else "not ready yet; will keep looking at " + ", ".join(config.candidates),
    )

    def stop(_signum: int, _frame: object) -> None:
        _logger.info("shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
