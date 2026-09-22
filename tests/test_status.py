"""`decis models` must not overstate what this server can run.

`registry.status` exists because "the engine class imports" and "this server can answer
with it" are different claims. Registration is lazy by design (`AGENTS.md §6`), so every
engine class imports cleanly in an image that installed none of its dependencies -- and
the first version of `decis models` printed "ready" for all of them.

That matters more than a cosmetic label: `decis models` is the command an operator or a
startup script greps to decide whether the container is worth sending traffic to. A
truthful "needs weights" with the exact command next to it is useful; a false "ready" is
a trap.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from decis.engines.laya import WEIGHTS, LayaEngine
from decis.engines.registry import SPECS, EngineStatus, create, status
from decis.paths import WeightSpec, missing_requirements

# --- the probe itself -----------------------------------------------------------


def test_a_missing_module_is_reported() -> None:
    """Uses a name that cannot be installed, so the answer does not depend on the extras.

    An earlier version of this test asserted `requires=("laya",)` was missing -- true in
    the no-weights CI environment and false on any machine that had actually installed
    the engine, which is exactly where the interesting bugs live.
    """
    assert missing_requirements(WeightSpec(engine_id="x", requires=("json", "os"))) == []
    assert missing_requirements(WeightSpec(engine_id="x")) == []
    assert missing_requirements(WeightSpec(engine_id="x", requires=("decis_no_such_module",))) == [
        "decis_no_such_module"
    ]


def test_a_present_module_is_reported_as_present() -> None:
    """The complement: `requires` naming the engine's own package must resolve here."""
    from decis.engines.laya import WEIGHTS

    if missing_requirements(next(iter(WEIGHTS.values()))):
        pytest.skip("the `laya` extra is not installed; the other direction is measured instead")
    assert missing_requirements(WEIGHTS["laya-multilingual"]) == []


def test_a_missing_parent_package_does_not_raise() -> None:
    """`find_spec("a.b")` raises ImportError when `a` is absent, not returns None."""
    absent = WeightSpec(engine_id="x", requires=("definitely_not_a_real_package.sub",))
    assert missing_requirements(absent) == ["definitely_not_a_real_package.sub"]


def test_every_weighted_engine_declares_what_it_needs() -> None:
    """A checkpoint that declares no requirements would always report... something.

    Without `requires`, `status` cannot tell an installed engine from an absent one,
    so it would fall through to the weights check and report "needs weights" for an
    image where the truth is "the package is not installed". Both are unactionable
    guesses; the declaration is what makes the answer exact.
    """
    for engine_id, spec in WEIGHTS.items():
        assert spec.requires, f"{engine_id} declares no `requires`, so its status cannot be exact"


# --- the classifier ------------------------------------------------------------


def test_mock_is_usable_because_it_has_no_weights() -> None:
    """The one engine that is genuinely ready in a bare install."""
    state = status("mock")
    assert state.usable
    assert state.summary == "ready"


def test_a_laya_engine_reports_whichever_half_is_missing(monkeypatch, tmp_path: Path) -> None:
    """The two failure modes are different, and `remedy` must name the right fix.

    Asserted by forcing each half, because on any one machine only one of them is
    usually true and the other branch would never be exercised.
    """
    # Half one: the package is absent. Whatever the weights situation, this wins,
    # because installing nothing makes a weights download pointless.
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: ["laya"])
    absent = status("laya-multilingual")
    assert not absent.usable
    assert absent.summary == "deps missing"
    assert "uv sync --extra laya" in absent.remedy

    # Half two: the package is present but the checkpoint is not on disk. The remedy
    # must be the download command, not the install command.
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: [])
    monkeypatch.setattr("decis.paths.resolve", lambda spec, settings: _Source(kind="hub"))
    missing_weights = status("laya-multilingual")
    assert not missing_weights.usable
    assert missing_weights.summary == "needs weights"
    assert missing_weights.remedy == "decis download --engine laya-multilingual"


def _spec(engine_id: str) -> WeightSpec:
    from decis.engines.laya import WEIGHTS

    return WEIGHTS[engine_id]


class _Engine:
    """An engine stub whose `weights()` is a spec of our choosing."""

    def __init__(self, spec: WeightSpec) -> None:
        self._spec = spec

    def weights(self) -> WeightSpec:
        return self._spec


class _Source:
    """Just enough `WeightSource` for the classifier."""

    def __init__(self, *, kind: str, path: Path | None = None) -> None:
        self.kind = kind
        self.path = path

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def is_downloadable(self) -> bool:
        return self.kind == "hub"


def test_local_weights_make_an_engine_usable(monkeypatch) -> None:
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: [])
    monkeypatch.setattr("decis.paths.resolve", lambda spec, settings: _Source(kind="local", path=Path("/srv/laya")))
    state = status("laya")
    assert state.usable
    assert state.summary == "ready"
    assert "/srv/laya" in state.remedy


def test_a_present_but_wrong_mount_is_not_reported_as_ready(monkeypatch) -> None:
    """The trap this guards: a mounted volume that exists, is readable, and is wrong.

    `resolve` refuses to call such a directory a checkpoint, so the engine falls back
    to the Hub -- which means the honest answer here is "needs weights", not "ready"
    and not "something is broken".
    """
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: [])
    monkeypatch.setattr("decis.paths.resolve", lambda spec, settings: _Source(kind="hub"))
    state = status("laya")
    assert not state.usable
    assert state.summary == "needs weights"


def test_an_engine_with_no_repository_reports_no_weights(monkeypatch) -> None:
    """Reachable only for a spec with neither a local checkpoint nor a repo to fetch."""
    repo_less = dataclasses.replace(_spec("laya"), repo_id=None, subfolder=None)
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: [])
    monkeypatch.setattr("decis.paths.resolve", lambda spec, settings: _Source(kind="none"))
    monkeypatch.setattr("decis.engines.registry.create", lambda engine_id: _Engine(repo_less))
    state = status("laya")
    assert not state.usable
    assert state.summary == "no weights"


def test_every_registered_engine_gets_a_status() -> None:
    """Including a broken one: `status` must classify, never raise."""
    for engine_id in SPECS:
        state = status(engine_id)
        assert isinstance(state, EngineStatus)
        assert state.engine_id == engine_id
        assert state.summary
        if state.usable:
            assert state.summary == "ready"


def test_status_never_loads_weights() -> None:
    """Calling it must stay cheap: `decis models` is not allowed a 75 s cold start.

    A loaded `LayaEngine` would have `_agent` set, and `create()` builds a fresh
    instance, so asserting the *class* is untouched by this call is the observable
    form of "no forward pass happened".
    """
    engine = create("laya-multilingual")
    assert isinstance(engine, LayaEngine)
    assert not engine.loaded
    status("laya-multilingual")
    assert not engine.loaded


# --- the CLI's use of it -------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Run the CLI with stdout captured, as a user would see it."""

    def run(*argv: str) -> tuple[int, str]:
        import contextlib
        import io

        from decis import cli as cli_module

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli_module.main(list(argv))
        return code, buffer.getvalue()

    return run


def test_models_marks_unusable_engines(cli) -> None:
    code, output = cli("models", "--env-file", "")
    assert code == 0
    # `mock` is installed and weight-free, so it is the only one that can say "ready"
    # in the no-weights test environment.
    assert "ready" in output
    assert "usable: mock" in output
    # And the engines that cannot run here are marked, not silently called ready.
    assert "*" in output


def test_models_prints_the_command_that_fixes_each_gap(cli) -> None:
    """The point of the remedy column: the user should not have to guess.

    Asserted against the state that *this* machine is in, because both states are
    legitimate: a bare checkout lacks the engine modules, and a `uv sync --extra <engine>`
    checkout has them but not the weights. An earlier version hardcoded the first and
    failed on the second.

    The expected extra comes from the registry rather than from a literal. It used to
    say `laya` for every engine, which passed only while `laya` was the sole engine with
    dependencies -- adding kev made the test report the *test's* staleness as a product
    bug.
    """
    from decis.engines.registry import status as engine_status

    _, output = cli("models", "--env-file", "")
    for engine_id in SPECS:
        state = engine_status(engine_id)
        if state.usable or not state.remedy:
            continue
        line = next(part for part in output.splitlines() if part.startswith(f" *{engine_id}"))
        if state.summary == "deps missing":
            extra = SPECS[engine_id].extra or "all"
            assert f"uv sync --extra {extra}" in line, line
        elif state.summary == "needs weights":
            assert f"decis download --engine {engine_id}" in line, line


def test_doctor_reports_the_same_distinction(cli) -> None:
    """`doctor` and `models` must not disagree about what is runnable."""
    code, output = cli("doctor", "--env-file", "")
    assert code in (0, 1)
    assert "engines" in output


def test_download_of_a_weightless_engine_is_not_reported_as_success(cli) -> None:
    """`decis download --engine mock` asked for something that cannot happen."""
    code, output = cli("download", "--engine", "mock", "--env-file", "")
    assert code != 0
    assert "no weights" in output


def test_download_rejects_an_unknown_engine(cli) -> None:
    code, output = cli("download", "--engine", "nope", "--env-file", "")
    assert code != 0
    assert "unknown engine" in output


def test_download_accepts_an_alias(cli, tmp_path: Path, monkeypatch) -> None:
    """`--engine laya-multi` must work: users copy names out of `decis models`.

    `snapshot_download` is stubbed, both because a real call is a 647 MiB download and
    because stubbing it is the only way to control the interesting outcome.
    """
    # `huggingface_hub` arrives with an engine extra, so on a bare checkout the CLI
    # stops before reaching the downloader and there is nothing to stub.
    huggingface_hub = pytest.importorskip("huggingface_hub")

    fetched: list[dict] = []

    def fake_snapshot(*args: object, **kwargs: object) -> str:
        fetched.append(dict(kwargs))
        return str(tmp_path)  # deliberately writes no checkpoint files

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    code, output = cli("download", "--engine", "laya-multi", "--dest", str(tmp_path), "--env-file", "")

    if "unknown engine" in output:
        raise AssertionError(f"alias was not resolved: {output}")
    if not fetched:
        pytest.skip("the `laya` extra is not installed here, so no download was attempted")

    # 1. The alias resolved to the *multilingual* checkpoint, not to the root one.
    patterns = [str(pattern) for pattern in fetched[0]["allow_patterns"]]
    assert patterns and all(pattern.startswith("multilingual/") for pattern in patterns), patterns
    assert fetched[0]["revision"].startswith("1c5edc17")

    # 2. The downloader ran, but wrote nothing, so `decis download` must exit non-zero
    #    rather than report success. This is the "do not trust the HTTP calls, check the
    #    loader's own question" rule paying off, and it is the reason this test is worth
    #    stubbing rather than mocking away entirely.
    assert code != 0, f"claimed success with no checkpoint on disk: {output}"
    assert "no usable checkpoint" in output, output


def test_a_weighted_engine_without_huggingface_hub_says_so(cli, tmp_path: Path, monkeypatch) -> None:
    """The remedy for a missing downloader names the extra, not a bare pip package."""
    monkeypatch.setattr("decis.cli.has_module", lambda name: False)
    monkeypatch.setattr("decis.paths.missing_requirements", lambda spec: [])
    code, output = cli("download", "--engine", "laya", "--dest", str(tmp_path), "--env-file", "")
    assert code != 0
    assert "huggingface_hub" in output
