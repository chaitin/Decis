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


@pytest.fixture(autouse=True)
def the_machines_caches_do_not_decide_a_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """`hub.cached` reads this host's real client caches; a test may not depend on them.

    The lookup is deliberately cheap and local, which is exactly why it would otherwise be
    invisible here: a checkpoint that happens to be in the developer's Hugging Face cache
    would make `download` a no-op and the assertions below would test nothing.
    """
    monkeypatch.setattr(hub, "cached", lambda spec, settings: None)


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hugging Face does not answer; the client for the fallback is installed."""
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: False)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)
    # The fallback asks ModelScope whether it has the repository before committing to it
    # (`hub.survey_hub`: mstrasser/Jeff-* have no mirror at all). Canned here so the suite
    # stays offline; the answers themselves are asserted in `tests/test_hub.py`.
    monkeypatch.setattr(hub, "survey_hub", lambda name, spec, settings, **kwargs: hub.Survey(name, available=True))


@pytest.fixture
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hugging Face answers, and a named fetch measures both hubs without a network."""
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: True)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)
    monkeypatch.setattr(
        hub,
        "survey_hub",
        lambda name, spec, settings, **kwargs: hub.Survey(
            name, available=True, speed=(1 << 20) if name == "huggingface" else (9 << 20)
        ),
    )


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
    # The fallback also asks the mirror whether it *has* the repository before committing to
    # it; without this the test measures this machine's route to modelscope.cn.
    monkeypatch.setattr(hub, "survey_hub", lambda name, spec, settings, **kwargs: hub.Survey(name, available=True))
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


# --- the source can also be chosen for being faster (§2-D37) --------------------


def test_a_slower_hugging_face_sends_the_fetch_to_the_faster_mirror(tmp_path, monkeypatch, reachable: None) -> None:
    """The user-visible half of the measurement: the mirror is used, and both numbers are printed.

    The fixtures answer "huggingface at 1 MiB/s, modelscope at 9 MiB/s", which is the shape
    this exists for: an endpoint that answers *and* moves a 647 MiB checkpoint too slowly to
    use. Nothing here is a network measurement -- the rule itself is asserted in
    `tests/test_hub.py`; what is asserted here is that `download` acts on it and says why.
    """
    models_dir = tmp_path / "models"
    spec = spec_of(LAYA)
    calls = install_client(monkeypatch, "modelscope", lambda model_id: spec)

    code, text = run_cli("download", "--engine", LAYA, "--dest", str(models_dir), "--env-file", "")

    assert code == 0, text
    assert calls and calls[0]["model_id"] == "convaiinnovations/laya"
    assert "9.0 MiB/s" in text and "1.0 MiB/s" in text, "the reason has to carry both measurements"
    assert "pins the revision" in text, "and the pin it gives up has to stay visible"
    assert resolve(spec, dataclasses.replace(Settings(), model_dir=models_dir)).is_local


def test_a_checkpoint_already_in_a_cache_is_never_fetched_from_the_other_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reachable: None
) -> None:
    """The protection against the speed rule: cached bytes beat a faster route, always.

    Without this, an 8 GiB checkpoint that is already on disk in one client's cache would be
    fetched again from whichever hub won a race -- which is the *slow* outcome dressed up as
    the fast one.
    """
    spec = spec_of(LAYA)
    cached_root = layout(tmp_path / "cache", spec)

    def never(**kwargs: object) -> str:
        pytest.fail("a checkpoint that is already on disk was fetched again")

    install_client(monkeypatch, "modelscope", never)
    install_client(monkeypatch, "huggingface_hub", never)
    monkeypatch.setattr(hub, "cached", lambda spec, settings: ("huggingface", cached_root))

    code, text = run_cli("download", "--engine", LAYA, "--env-file", "")

    assert code == 0, text
    assert f"already in the huggingface cache at {cached_root}" in text
    assert "source huggingface" in text


def test_a_destination_is_filled_from_the_cache_without_asking_the_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reachable: None
) -> None:
    """`--dest` on a warm cache is a copy, and a copy is the only thing that works offline.

    Asking the client to lay out a checkpoint it already has (`local_dir=...`) resolves the
    revision over the network first: with Hugging Face unreachable that was an unhandled
    `httpx.ConnectError` *after* the command had printed "already in the cache", and for
    ModelScope `local_dir` bypasses its cache entirely. So the bytes are copied instead, and
    the resolver the server uses is what says the copy landed.
    """
    spec = spec_of(LAYA)
    cached_root = layout(tmp_path / "cache", spec)
    monkeypatch.setattr(hub, "cached", lambda spec, settings: ("huggingface", cached_root))
    monkeypatch.setattr(hub, "choose", lambda *args, **kwargs: pytest.fail("a warm cache must not be decided"))
    install_client(monkeypatch, "huggingface_hub", lambda repo_id: pytest.fail("fetched again"))
    install_client(monkeypatch, "modelscope", lambda model_id: pytest.fail("fetched again"))
    models_dir = tmp_path / "models"

    code, text = run_cli("download", "--engine", LAYA, "--dest", str(models_dir), "--env-file", "")

    assert code == 0, text
    assert "already in the huggingface cache" in text
    source = resolve(spec, dataclasses.replace(Settings(), model_dir=models_dir))
    assert source.is_local, text
    assert (source.path / str(spec.marker)).is_file()


def test_a_clients_own_network_error_is_reported_as_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reachable: None
) -> None:
    """The clients raise transport exceptions; a traceback is not a report to a user."""

    def refused(repo_id: str) -> WeightSpec:
        raise ConnectionError("connection refused by the proxy")

    install_client(monkeypatch, "huggingface_hub", refused)

    code, text = run_cli(
        "download", "--engine", LAYA, "--dest", str(tmp_path / "models"), "--hub", "huggingface", "--env-file", ""
    )

    assert code == 1, text
    assert "could not fetch convaiinnovations/laya from huggingface" in text
    assert "connection refused by the proxy" in text
    assert "Traceback" not in text


def test_every_shipped_engine_reaches_the_hub_through_one_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One download abstraction for all six engines -- asserted, not assumed.

    The rule is `AGENTS.md` §2 ("权重路径解析...引擎自己调 `snapshot_download`" is forbidden),
    and §2-D33 is what happens without it: kev's adapter went to a vendored loader that called
    the Hugging Face client itself, so `--hub modelscope` was silently ignored. This walks the
    registry and checks that every engine's declared repository arrives at `hub.download` --
    including a second repository when the engine has a base.
    """
    from decis.engines.registry import SPECS

    fetched: dict[str, WeightSpec] = {}
    real_download = hub.download

    def spy(spec: WeightSpec, *, selection, destination=None, local_only: bool = False) -> Path:
        fetched[spec.repo_id or "?"] = spec
        landing = Path(destination) if destination is not None else tmp_path / "cache" / (spec.repo_id or "x")
        return real_download(spec, selection=selection, destination=landing, local_only=local_only)

    install_client(monkeypatch, "huggingface_hub", lambda model_id: fetched[str(model_id)])
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: True)
    monkeypatch.setattr(hub, "download", spy)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)

    for engine_id in sorted(SPECS):
        spec = create(engine_id).weights()
        if spec is None or not spec.is_downloadable():
            continue
        fetched.clear()
        code, text = run_cli(
            "download",
            "--engine",
            engine_id,
            "--dest",
            str(tmp_path / engine_id),
            "--hub",
            "huggingface",
            "--env-file",
            "",
        )
        assert code == 0, f"{engine_id}: {text}"
        assert spec.repo_id in fetched, f"{engine_id} never fetched {spec.repo_id} through hub.download"
        for base in spec.base_specs():
            assert base.repo_id in fetched, f"{engine_id} never fetched its base {base.repo_id}"


def test_the_load_path_reaches_the_hub_through_the_same_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` and `download` must not have separate routes to the Hub.

    The test above drives `decis download`; this one drives `paths.fetch_checkpoint`, which is
    what every engine's `load()` calls. An adversarial reviewer replaced that function's body
    with `raise AssertionError` and the whole suite stayed green, so the download half alone
    did not guard the half that `serve` uses.
    """
    from decis import paths
    from decis.engines.registry import SPECS

    fetched: list[WeightSpec] = []

    def spy(spec: WeightSpec, *, selection, destination=None, local_only: bool = False) -> Path:
        fetched.append(spec)
        return real_download(spec, selection=selection, destination=destination, local_only=local_only)

    landing = tmp_path / "hub"
    real_download = hub.download

    def snapshot_download(**kwargs: object) -> str:
        # A client cache is a *directory*, and this one writes the layout `paths.resolve` reads.
        # Every checkpoint of that repository: three Laya checkpoints share one repo id, and
        # `checkpoint_root` expects each one's own subfolder.
        for candidate in specs_with_repo(str(kwargs.get("repo_id"))):
            layout(landing, candidate)
        return str(landing)

    stub = types.ModuleType("huggingface_hub")
    stub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", stub)
    monkeypatch.setattr(hub, "cached", lambda spec, settings: None)
    monkeypatch.setattr(hub, "download", spy)
    monkeypatch.setattr(hub, "probe_endpoint", lambda url, **kwargs: True)
    monkeypatch.setattr(hub, "client_installed", lambda name: True)
    monkeypatch.setattr(hub, "survey_hub", lambda name, spec, settings, **kwargs: hub.Survey(name, available=True))

    settings = dataclasses.replace(Settings(), hub="huggingface")
    for engine_id in sorted(SPECS):
        spec = spec_of(engine_id)
        if spec is None or not spec.is_downloadable():
            continue
        fetched.clear()
        paths.fetch_checkpoint(spec, settings)
        assert [entry.repo_id for entry in fetched] == [spec.repo_id], f"{engine_id} bypassed paths.fetch_checkpoint"


def specs_with_repo(repo_id: str) -> list[WeightSpec]:
    """Every shipped checkpoint that declares `repo_id`, for the fake client's benefit."""
    from decis.engines.registry import SPECS

    declared = [spec_of(engine_id) for engine_id in SPECS]
    found = [spec for spec in declared if spec is not None and spec.repo_id == repo_id]
    if not found:
        pytest.fail(f"a client was asked for {repo_id}, which no shipped engine declares")
    return found


def test_a_cache_hit_on_the_mirror_still_warns_about_the_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes in a ModelScope cache *are* the mirror's revision, so the caveat still applies.

    Read from the cache or fetched from the network, the checkpoint a deployment loads is the
    one the mirror tracks -- and the warning is the only thing that says so before it is used.
    """
    spec = spec_of(LAYA)
    cached_root = layout(tmp_path / "cache", spec)

    def never(**kwargs: object) -> str:
        pytest.fail("a cached checkpoint was fetched from the network")

    install_client(monkeypatch, "modelscope", never)
    install_client(monkeypatch, "huggingface_hub", never)
    monkeypatch.setattr(hub, "cached", lambda spec, settings: ("modelscope", cached_root))

    code, text = run_cli("download", "--engine", LAYA, "--env-file", "")

    assert code == 0, text
    assert "already in the modelscope cache" in text
    assert f"pins the revision {spec.revision[:12]}" in text
    assert "DECIS_HUB=huggingface" in text, "the escape hatch has to be in the message here too"
