"""What `decis serve` says, and in which order, before it hands the port to uvicorn.

Two defects are pinned here, both reported from a real ARM host:

* `Uvicorn running on http://127.0.0.1:8000` appeared while the engine was still being
  fetched, and a user reasonably read that as "it works now". The state has to be on the
  screen before uvicorn gets to say anything (`startup_banner`), and the promise has to
  be one a probe can test -- `/readyz` -- rather than a line of prose.
* `--preload` is the opt-in for the other ordering: load first, then open the port.

The banner is a pure function of settings, so it is asserted as text. `--preload` is
asserted through the one thing it has to change: whether uvicorn is handed an app that
can already answer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io

import pytest

from decis import cli as cli_module
from decis.config import Settings
from decis.service import DecisionService

#: The weight-free engine `tests/conftest.py` registers, so nothing here needs weights.
STUB = "stub"


@pytest.fixture(autouse=True)
def the_hub_probe_is_a_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor` reports which Hub answers here; the answer comes from a stub, not from the wire.

    `tests/conftest.py` closes the transport, so leaving this out fails the test rather than
    measuring this machine -- which is the point: on the network the fallback exists for,
    Hugging Face does not answer, and a report asserted against a live probe would only hold
    on the machine that wrote it. The two answers, and what `doctor` prints for each, are
    asserted in `test_doctor_reports_the_source_it_would_fetch_from`.
    """
    monkeypatch.setattr("decis.hub.probe_endpoint", lambda url, **kwargs: True)


def banner(*, preload: bool = False, **changes: object) -> str:
    settings = dataclasses.replace(Settings(), **{"default_engine": STUB, **changes})  # type: ignore[arg-type]
    return cli_module.startup_banner(settings, "127.0.0.1", 8000, preload=preload)


def run_cli(*argv: str) -> tuple[int, str]:
    """Run the CLI with both streams captured, as a user would see it."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli_module.main(list(argv))
    return code, buffer.getvalue()


def test_the_banner_warns_that_the_port_opens_before_the_engine_answers() -> None:
    """The rule a bound socket cannot express on its own: open is not the same as ready."""
    text = banner()
    assert "engine    stub" in text
    assert "bind      127.0.0.1:8000" in text
    assert "/readyz returns 503" in text
    assert "engine stub ready" in text
    assert "--preload" in text


def test_the_banner_says_where_the_weights_come_from() -> None:
    """A cold start that downloads 647 MiB should be announced, not discovered."""
    assert "self-contained" in banner()  # the stub has no weights at all

    text = banner(default_engine="laya-multilingual")
    assert "convaiinnovations/laya" in text
    assert "cold cache" in text
    assert "646.8 MiB" in text, "the declared size is what tells a user whether to wait"


def test_the_banner_says_preload_delays_the_healthz_answer() -> None:
    """`--preload` moves the load in front of the socket, and probes see that."""
    text = banner(preload=True)
    assert "/healthz" in text
    assert "503" not in text, "under --preload there is no window in which a 503 is served"
    assert "liveness probe" in text


def test_serve_binds_only_after_a_preload_that_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the flag: uvicorn must not be reached for an engine that failed."""
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app, kwargs=kwargs))

    code, _ = run_cli("serve", "--engine", STUB, "--host", "127.0.0.1", "--preload", "--env-file", "")
    assert code == 0
    assert seen["app"].state.service.ready, "--preload must load before uvicorn.run is reached"


def test_serve_does_not_bind_when_the_preload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: a failed load has to look like a failure, not like a server."""
    import uvicorn

    bound: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: bound.append("bound"))

    def explode(self: DecisionService) -> None:
        raise RuntimeError("no room on this disk")

    monkeypatch.setattr(DecisionService, "load", explode)
    code, output = run_cli("serve", "--engine", STUB, "--host", "127.0.0.1", "--preload", "--env-file", "")

    assert code == 1
    assert bound == []
    assert "no room on this disk" in output
    assert "nothing was bound" in output


def test_without_preload_the_engine_is_left_to_the_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default has to stay non-blocking: a probe must get an answer during a cold start.

    `tests/test_readiness.py` owns the app-level half of that; the CLI-level half is
    that nothing here waits for the load -- it happens inside the lifespan -- and that
    the banner said so before uvicorn was called.
    """
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app))

    code, output = run_cli("serve", "--engine", STUB, "--host", "127.0.0.1", "--env-file", "")
    assert code == 0
    assert seen["app"].state.service.ready is False
    assert "/readyz returns 503" in output


def test_the_banner_in_the_docs_is_the_banner_this_prints() -> None:
    """A verbatim console sample has to be the console output.

    Both language versions of `getting-started` show it so a reader can compare what they
    see against what the docs promise; a sample that drifts is worse than no sample, and
    the release line is the only part that moves on its own. Guarded here rather than in
    `tests/test_docs.py`, which compares the two languages with each other and cannot tell
    either of them from the program.
    """
    from pathlib import Path

    expected = cli_module.startup_banner(Settings(), "127.0.0.1", 8000).splitlines()[1:]
    for name in ("getting-started.md", "getting-started.zh-CN.md"):
        text = (Path(__file__).resolve().parent.parent / "docs" / name).read_text(encoding="utf-8")
        matches = []
        for block in text.split("```")[1::2]:
            lines = block.splitlines()
            for index, line in enumerate(lines[:-1]):
                if line.startswith("decis ") and lines[index + 1].startswith("  engine    laya"):
                    matches.append(lines[index + 1 :])
        assert len(matches) == 1, f"{name}: expected exactly one `decis serve` transcript, found {len(matches)}"
        assert matches[0] == expected, f"{name} shows a banner this program does not print"


def test_doctor_names_the_proxy_entry_it_rewrote(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_settings` edits the environment; the edit has to be attributable.

    The entry is reached through `PROXY_VARIABLES` so that nothing outside
    `decis.config` spells the variable names (`tests/test_conventions.py`). The key is
    set so `doctor`'s exit code reflects what is under test here and not the auth check
    the bare default bind address would raise.
    """
    from decis.config import PROXY_VARIABLES

    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "127.0.0.1,[::1]")

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    for name in PROXY_VARIABLES:
        assert f"{name}: [::1] -> ::1" in output, output
    assert output.count("[::1] -> ::1") == len(PROXY_VARIABLES)


def test_doctor_is_quiet_about_a_bypass_list_it_did_not_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report that always prints something is a report nobody reads.

    The list here is the one the test suite itself installs, so this is also the check
    that a normal run of `doctor` keeps the first section to the settings a user set.
    """
    from decis.config import LOOPBACK_BYPASS, PROXY_VARIABLES

    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, ",".join(LOOPBACK_BYPASS))

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    assert "proxy bypass" not in output


def test_doctor_names_the_gpu_the_installed_torch_cannot_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """D36 on screen: an idle RTX card, a CPU-only wheel, and the command that fixes it.

    Both probes are faked, so this asserts the *report* rather than the host the suite
    happens to run on. The Windows case is the one that matters: `torch.version.cuda` is
    the only field that separates "no NVIDIA GPU" from "a wheel with no CUDA support", and
    before this block `decis doctor` printed neither.
    """
    from decis.engines import devices

    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")
    monkeypatch.setattr(devices, "best_device", lambda *args, **kwargs: "cpu")
    monkeypatch.setattr(
        devices,
        "torch_build",
        lambda: devices.TorchBuild(version="2.14.0", cuda=None, hip=None, cuda_devices=0, cuda_device_name=None),
    )
    monkeypatch.setattr(
        devices, "nvidia_gpus", lambda *args, **kwargs: (devices.Gpu("NVIDIA GeForce RTX 4070", "552.22"),)
    )

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    assert "  device           cpu (auto)" in output
    assert "  torch            2.14.0, built without CUDA support" in output
    assert "  gpu              NVIDIA GeForce RTX 4070 (driver 552.22)" in output
    assert "  advice           " in output
    assert "uv sync --all-extras" in output


def test_doctor_says_nothing_about_a_gpu_that_is_working(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the report: a machine with no GPU is not a machine with a fault."""
    from decis.engines import devices

    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")
    monkeypatch.setattr(devices, "best_device", lambda *args, **kwargs: "mps")
    monkeypatch.setattr(
        devices,
        "torch_build",
        lambda: devices.TorchBuild(version="2.14.0", cuda=None, hip=None, cuda_devices=0, cuda_device_name=None),
    )
    monkeypatch.setattr(devices, "nvidia_gpus", lambda *args, **kwargs: ())

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    assert "  device           mps (auto)" in output
    assert "  gpu              none reported by nvidia-smi" in output
    assert "advice" not in output


def test_doctor_refuses_a_misspelled_device_instead_of_tracebacking(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DECIS_DEVICE=gpu` fails every load; `doctor` is where it should be legible."""
    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")
    monkeypatch.setenv("DECIS_DEVICE", "gpu")

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 1
    assert "device           INVALID -- DECIS_DEVICE='gpu' is not a device" in output


def test_doctor_reports_the_source_it_would_fetch_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Hub decision is reported as a decision, with its reason and its cost.

    Two things are pinned: a reachable Hugging Face is named as the source, and the speed line
    says `not measured` -- `doctor` names no checkpoint, so there is no fetch to measure, and a
    number here would be one no download is obliged to get (`docs/design-review.md` §2-D37).

    The fallback case then shows the other half: the report names ModelScope, gives the reason,
    and repeats the cost a deployment is about to pay -- no commit pin.
    """
    monkeypatch.setenv("DECIS_API_KEY", "doctor-test-key")

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    assert "  weight hub       auto -> huggingface:" in output, output
    assert "  hub speed        not measured (no engine named)" in output, output
    assert "hub pin" not in output, "the pin is honorable here; nothing to warn about"

    monkeypatch.setattr("decis.hub.probe_endpoint", lambda url, **kwargs: False)

    code, output = run_cli("doctor", "--env-file", "")
    assert code == 0
    assert "  weight hub       auto -> modelscope:" in output, output
    assert "did not answer" in output, "the reason has to be on the line, not just the verdict"
    assert "  hub pin          modelscope has no commit revisions" in output, output
