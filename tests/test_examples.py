"""`examples/` is documentation, and documentation rots.

The README of a project like this claims "point the official SDK at Decis and nothing
else changes". A claim like that is only worth something if it is executed, so this file
executes the examples:

* **every** `curl` command in `examples/curl.md` runs against a real server, and the
  status code printed next to it is asserted against the code actually returned;
* a command added without a documented status is a failure, so the two cannot drift;
* the mock's answer *values* printed in the doc are asserted too -- they are stable, so a
  doc that shows numbers the server no longer produces is caught rather than trusted;
* `examples/python_sdk.py` runs end to end with the unmodified official SDK.

`curl` is not a Python dependency, so the HTTP examples skip (with a reason) where it is
missing. The SDK script is Python and is always run when `typesafe-sdk` is importable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CURL_MD = ROOT / "examples" / "curl.md"
SDK_SCRIPT = ROOT / "examples" / "python_sdk.py"
TOKEN = "local"

#: `# -> 200` on the line after a command. The parser below keys off this exact shape.
_STATUS_MARKER = re.compile(r"^#\s*->\s*(\d{3})\s*$")


@pytest.fixture(scope="module")
def server_url():
    """A real socket, because `curl` cannot talk to an ASGI stub."""
    import socket

    import uvicorn

    from decis.app import create_app
    from decis.config import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(
        Settings(api_keys=(TOKEN,), host="127.0.0.1", default_engine="mock", env_file="", request_timeout_ms=8000)
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the example server never started")
    return f"http://127.0.0.1:{port}"


def _is_complete(command: str) -> bool:
    """Is this a complete shell command?

    Not just "does it end with a backslash": the JSON payloads in the cookbook are
    single-quoted across several lines, so the command is only finished when the quotes
    balance too. A backslash-only check silently truncates those commands, and the test
    then reports a missing status marker instead of the real problem.
    """
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is None:
            if char in "\"'":
                quote = char
            elif char == "\\":
                index += 1  # skip the escaped character
        elif char == quote:
            quote = None
        index += 1
    return quote is None and not command.rstrip().endswith("\\")


def parse_examples(path: Path = CURL_MD) -> list[tuple[str, int]]:
    """Every `curl` command in curl.md, paired with its documented status code.

    Commands may span lines (trailing backslashes, or a single-quoted JSON payload that
    wraps); the marker is the first non-blank line after the command ends.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    examples: list[tuple[str, int]] = []
    index = 0
    in_bash = False
    while index < len(lines):
        line = lines[index]
        if line.strip() == "```bash":
            in_bash = True
            index += 1
            continue
        if line.strip().startswith("```") and in_bash:
            in_bash = False
            index += 1
            continue
        if in_bash and line.startswith("curl"):
            command = line
            while not _is_complete(command):
                index += 1
                assert index < len(lines), f"unterminated command in curl.md: {command[:120]}"
                command = command.rstrip().rstrip("\\") + " " + lines[index].strip()
            # Find the marker, skipping blank lines.
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            match = _STATUS_MARKER.match(lines[lookahead].strip()) if lookahead < len(lines) else None
            if match is None:
                found = lines[lookahead].strip() if lookahead < len(lines) else "<end of file>"
                raise ValueError(
                    f"a curl command in {path.name} has no `# -> NNN` marker after it "
                    f"(found {found!r}). Every example must declare the status it returns, or "
                    f"this test cannot tell a documented behaviour from an undocumented one.\n"
                    f"  command: {command[:120]}"
                )
            examples.append((command, int(match.group(1))))
            index = lookahead
        index += 1
    return examples


EXAMPLES = parse_examples()


def test_the_cookbook_has_examples_at_all() -> None:
    """Guards the parser: if the format changes, this fails instead of silently finding none."""
    assert len(EXAMPLES) >= 8, f"only found {len(EXAMPLES)} examples; did the fence format change?"


def test_every_status_code_in_the_cookbook_is_reachable() -> None:
    """The doc claims to cover the error contract, so all four codes must appear."""
    documented = {status for _, status in EXAMPLES}
    assert {200, 401, 403, 422} <= documented, f"documented statuses are only {sorted(documented)}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed; the HTTP examples need it")
@pytest.mark.parametrize(("command", "expected"), EXAMPLES, ids=[str(status) for _, status in EXAMPLES])
def test_documented_curl_command_returns_the_documented_status(command: str, expected: int, server_url: str) -> None:
    """Run the command exactly as documented, against a real server."""
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={"BASE": server_url, "TOKEN": TOKEN, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"curl failed:\n{completed.stderr}"
    output = completed.stdout

    # Most examples end with `-w '\n%{http_code}\n'`; the header example prints the
    # status line instead. Accept either rather than requiring one style.
    tail = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if re.fullmatch(r"\d{3}", tail):
        actual = int(tail)
    else:
        header = re.search(r"^HTTP/\d(?:\.\d)?\s+(\d{3})", output, re.MULTILINE)
        assert header, f"could not find a status code in the output:\n{output[:400]}"
        actual = int(header.group(1))

    assert actual == expected, f"documented {expected}, got {actual}\ncommand: {command}\noutput: {output[:400]}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed; the HTTP examples need it")
def test_the_documented_values_are_the_ones_the_server_produces(server_url: str) -> None:
    """The doc prints concrete answers. They must still be the answers.

    The mock derives its output from a hash of the request, so these numbers are
    stable. If the mock changes, the doc is wrong and this says so, instead of leaving
    a plausible-looking number that no longer corresponds to anything.
    """
    payload = json.dumps(
        {
            "state": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
            "model": "mock",
            "questions": {
                "department": {
                    "type": "choice",
                    "instructions": "Which team should handle this?",
                    "criteria": {
                        "billing": "invoices, payments, refunds",
                        "technical": "bugs, outages, system errors",
                        "sales": "pricing, new contracts",
                    },
                },
                "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel or leave?"},
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent is this?",
                    "criteria": ["Can wait", "Soon", "Today", "Immediately"],
                },
            },
        }
    )
    completed = subprocess.run(
        [
            "curl",
            "-s",
            f"{server_url}/v1/systemone",
            "-H",
            f"authorization: Bearer {TOKEN}",
            "-H",
            "content-type: application/json",
            "-d",
            payload,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    body = json.loads(completed.stdout)
    answers = body["answers"]

    doc = CURL_MD.read_text(encoding="utf-8")
    assert answers["department"]["choice"] == "technical"
    assert f'"choice":"{answers["department"]["choice"]}"' in doc
    # The exact floats shown in the "all three primitives" example.
    for rendered in (
        f'"confidence":{answers["department"]["confidence"]}',
        f'"noul":{answers["churn_risk"]["noul"]}',
        f'"score":{answers["urgency"]["score"]}',
    ):
        assert rendered in doc, f"{rendered} is no longer what the doc shows"
    # Batching is visible, which is why the doc tells readers to look at it.
    assert body["decis"]["batch_size"] == 3
    assert '"batch_size":3' in doc


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed; the HTTP examples need it")
def test_the_server_url_placeholder_is_used_everywhere(server_url: str) -> None:
    """A hard-coded localhost:8000 would make the examples unrunnable as written."""
    for command, _ in EXAMPLES:
        assert "$BASE" in command, f"example does not use $BASE: {command[:100]}"


def test_the_cookbook_explains_why_the_status_codes_matter() -> None:
    """403 vs 401 is the least obvious part of the contract; the doc must state it."""
    doc = CURL_MD.read_text(encoding="utf-8")
    assert "No credential at all → 403" in doc
    assert "A credential was sent but is wrong → 401" in doc


# --- the SDK example -----------------------------------------------------------


SDK_AVAILABLE = shutil.which("python") is not None


def test_the_sdk_example_runs_against_a_real_server(server_url: str) -> None:
    """The whole compatibility claim, executed.

    Uses the interpreter running the tests, so it exercises the SDK that CI installed
    (`dev` extra) rather than whatever happens to be on PATH.
    """
    pytest.importorskip("typesafe_sdk", reason="typesafe-sdk is in the `dev` extra")
    completed = subprocess.run(
        [sys.executable, str(SDK_SCRIPT), "--base-url", server_url, "--api-key", TOKEN],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0, f"the example failed:\n{completed.stdout}\n{completed.stderr}"
    out = completed.stdout

    # It parsed typed objects, not raw dicts: these lines only exist if `response.choices`,
    # `response.nouls` and `response.scores` did their job.
    assert "model: decis/mock@0.1.0" in out
    assert "request id: req_" in out
    assert "choice  department = 'technical'" in out
    assert "noul    churn_risk = " in out
    assert "score   urgency    = " in out
    assert "0 Can wait" in out, "the score legend's int keys were not usable -- the SDK converts them"
    assert "usage: " in out


def test_the_sdk_example_reports_the_401_distinction(server_url: str) -> None:
    """A wrong key must surface as the SDK's authentication error, not a generic one."""
    pytest.importorskip("typesafe_sdk", reason="typesafe-sdk is in the `dev` extra")
    completed = subprocess.run(
        [sys.executable, str(SDK_SCRIPT), "--base-url", server_url, "--api-key", "definitely-wrong"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=ROOT,
    )
    # Every documented path fails, so the script reports it rather than pretending.
    assert completed.returncode == 1
    assert "TypeSafeAuthenticationError" in completed.stdout + completed.stderr


def test_the_sdk_example_works_with_a_proxied_no_proxy(server_url: str) -> None:
    """The trap this example had to work around, pinned so it cannot come back.

    `httpx` builds a URLPattern for each `NO_PROXY` entry and cannot parse a bracketed
    IPv6 literal, which fails inside the client constructor with a message that mentions
    neither proxies nor the example.
    """
    pytest.importorskip("typesafe_sdk", reason="typesafe-sdk is in the `dev` extra")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "NO_PROXY": "127.0.0.1,localhost,10.2.0.0/16,::1,[::1]",
        "no_proxy": "127.0.0.1,localhost,10.2.0.0/16,::1,[::1]",
        "PYTHONPATH": str(ROOT / "src"),
    }
    completed = subprocess.run(
        [sys.executable, str(SDK_SCRIPT), "--base-url", server_url, "--api-key", TOKEN],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=ROOT,
        env=env,
    )
    assert completed.returncode == 0, f"a bracketed IPv6 NO_PROXY broke the example:\n{completed.stderr}"


# --- the parser's own guards ---------------------------------------------------


def test_a_command_without_a_status_marker_is_rejected(tmp_path: Path) -> None:
    """The negative control for the format rule.

    Without this, `parse_examples` returning entries proves nothing about whether it
    would notice a command that forgot to say what it returns.
    """
    doc = tmp_path / "curl.md"
    doc.write_text(
        '```bash\ncurl -s "$BASE/healthz"\n# -> 200\n```\n\n```bash\ncurl -s "$BASE/readyz"\n```\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no `# -> NNN` marker"):
        parse_examples(doc)


def test_the_parser_reads_a_payload_that_wraps_across_lines(tmp_path: Path) -> None:
    """The bug this parser had, pinned.

    A backslash-only continuation check stops at `-d '{` and then reports a missing
    marker, pointing at the wrong problem entirely. The command is only complete when
    its quotes balance.
    """
    doc = tmp_path / "curl.md"
    doc.write_text(
        "```bash\n"
        "curl -s -w '\\n%{http_code}\\n' \"$BASE/v1/systemone\" \\\n"
        "  -H 'content-type: application/json' -d '{\n"
        '  "state": "x",\n'
        '  "model": "mock"\n'
        "  }}'\n"
        "# -> 422\n"
        "```\n",
        encoding="utf-8",
    )
    examples = parse_examples(doc)
    assert len(examples) == 1
    command, status = examples[0]
    assert status == 422
    # The whole payload was captured, not just the first line of it.
    assert '"model": "mock"' in command
    assert "-H 'content-type: application/json'" in command


def test_the_parser_ignores_non_curl_blocks(tmp_path: Path) -> None:
    """Only `curl` lines are executed; a `BASE=` setup block is not a command."""
    doc = tmp_path / "curl.md"
    doc.write_text(
        "```bash\nBASE=http://localhost:8000\nTOKEN=local\n```\n\n"
        "```bash\ncurl -s -w '\\n%{http_code}\\n' \"$BASE/healthz\"\n# -> 200\n```\n",
        encoding="utf-8",
    )
    assert len(parse_examples(doc)) == 1


def test_the_parser_ignores_json_output_blocks(tmp_path: Path) -> None:
    """`json` fences contain no commands, and must not be mistaken for any."""
    doc = tmp_path / "curl.md"
    doc.write_text(
        '```bash\ncurl -s -w \'\\n%{http_code}\\n\' "$BASE/healthz"\n# -> 200\n```\n\n```json\n{"status":"ok"}\n```\n',
        encoding="utf-8",
    )
    assert len(parse_examples(doc)) == 1
