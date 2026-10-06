"""`decis download` end to end, with fake clients and no network.

`tests/test_hub.py` covers the decision and the arguments each client is asked for. This
file covers what a user sees and what lands on disk: the message naming the source, the
warning about a pin that cannot be honored, the exit code when the fallback's client is
missing, and the one property that matters after all of it -- `paths.resolve` finds the
checkpoint afterwards, for the checkpoint *and* for the base model kev adapts.

Nothing here reaches the network in either direction: the probe is replaced and the clients
are fake modules. That is not a shortcut -- the fallback exists precisely for hosts where
Hugging Face is unreachable, and a test that needed it reachable would be a test that cannot
run where the feature is used.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from decis import hub
from decis.config import Settings
from decis.engines.registry import create
from decis.paths import WeightSpec, resolve
from test_cli_serve import run_cli

LAYA = "laya-multilingual"
KEV = "kev-0.8b"


def spec_of(engine_id: str) -> WeightSpec:
    return create(engine_id).weights()


def layout(directory: Path, spec: WeightSpec) -> Path:
    """Write what `paths.resolve` requires of a checkpoint directory: its marker."""
    root = directory / spec.subfolder if spec.subfolder else directory
    root.mkdir(parents=True, exist_ok=True)
    (root / str(spec.marker)).write_bytes(b"marker")
    return directory


def install_client(monkeypatch: pytest.MonkeyPatch, module: str, layout_for: Callable[[str], WeightSpec]) -> list[dict]:
    """A fake hub client that records its calls and writes a *valid* layout where told.

    The destination is taken from `local_dir`, exactly as the real clients do, so the test
    exercises the same write-then-resolve loop the command does -- and the assertion at the
    end is `paths.resolve`, the server's own reader (§2-D17).
    """
    calls: list[dict] = []

    def snapshot_download(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        destination = Path(str(kwargs["local_dir"]))
        spec = layout_for(str(kwargs.get("model_id") or kwargs.get("repo_id")))
        return str(layout(destination, spec))

    stub = types.ModuleType(module)
    stub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module, stub)
    return calls


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hugging Face does not answer; the client for the fallback is installed."""
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: False)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)


def test_the_cli_falls_back_to_modelscope_and_says_which_source_it_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable: None
) -> None:
    models_dir = tmp_path / "models"
    spec = spec_of(LAYA)
    calls = install_client(monkeypatch, "modelscope", lambda model_id: spec)

    code, text = run_cli("download", "--engine", LAYA, "--dest", str(models_dir), "--hub", "auto", "--env-file", "")

    assert code == 0, text
    assert "modelscope" in text, text
    assert "huggingface.co did not answer" in text, "the reason must be visible before the transfer"
    assert calls[0]["model_id"] == "convaiinnovations/laya"
    assert "revision" not in calls[0], "the pinned commit must never reach ModelScope (§2-D32)"

    # The oracle: the resolver the *server* uses, not the downloader's own bookkeeping.
    source = resolve(spec, dataclasses.replace(Settings(), model_dir=models_dir))
    assert source.is_local
    assert (source.path / "rl_agent_config.json").is_file()


def test_the_cli_warns_that_the_pin_cannot_be_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable: None
) -> None:
    """Downloading different bytes than the pinned revision is a warning, not a footnote."""
    spec = spec_of(LAYA)
    install_client(monkeypatch, "modelscope", lambda model_id: spec)

    code, text = run_cli("download", "--engine", LAYA, "--dest", str(tmp_path / "models"), "--env-file", "")

    assert code == 0, text
    assert f"pins the revision {spec.revision[:12]}" in text
    assert "DECIS_HUB=huggingface" in text, "the escape hatch has to be in the message"


def test_a_pinned_hugging_face_never_asks_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DECIS_HUB=huggingface` means fail loudly, not fall back quietly."""

    def explode(url: str) -> bool:
        pytest.fail("a pinned source probed instead of doing what it was told")

    monkeypatch.setattr(hub, "probe_endpoint", explode)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)
    spec = spec_of(LAYA)
    calls = install_client(monkeypatch, "huggingface_hub", lambda model_id: spec)

    code, text = run_cli(
        "download", "--engine", LAYA, "--dest", str(tmp_path / "models"), "--hub", "huggingface", "--env-file", ""
    )

    assert code == 0, text
    assert calls[0]["revision"] == spec.revision, "the pin is the reason this source is the pinned one"


def test_a_missing_fallback_client_is_a_configuration_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: False)
    monkeypatch.setattr(hub, "client_installed", lambda name: False)

    code, text = run_cli("download", "--engine", LAYA, "--dest", str(tmp_path / "models"), "--env-file", "")

    assert code == 2, text
    assert "modelscope" in text
    assert "uv sync --extra" in text, "the remedy has to be in the message"


def test_the_bases_land_next_to_the_checkpoint_they_are_adapted_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable: None
) -> None:
    """kev's adapter is useless without its base, so `--dest` has to hold both.

    Asserted through `paths.resolve` for the base as well: the point of writing it here
    rather than into a Hub cache is that the *loader* resolves it locally
    (`engines/kev.py: _base_directory`), which is what makes a mounted model directory
    self-contained.
    """
    models_dir = tmp_path / "models"
    adapter = spec_of(KEV)
    base_spec = adapter.base_specs()[0]
    calls = install_client(
        monkeypatch,
        "modelscope",
        lambda model_id: base_spec if model_id == base_spec.repo_id else adapter,
    )

    code, text = run_cli("download", "--engine", KEV, "--dest", str(models_dir), "--env-file", "")
    assert code == 0, text
    assert {call["model_id"] for call in calls} == {adapter.repo_id, base_spec.repo_id}

    settings = dataclasses.replace(Settings(), model_dir=models_dir)
    assert resolve(adapter, settings).is_local, text
    base_source = resolve(base_spec, settings)
    assert base_source.is_local, f"the base must resolve locally too; calls were {[c['model_id'] for c in calls]}"
    assert (base_source.path / "config.json").is_file()
