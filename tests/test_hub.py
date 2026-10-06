"""Which Hub the weights come from, and what each one can be asked for.

The fallback exists for one network shape: Hugging Face unreachable, ModelScope reachable.
Everything about it that can be wrong is asserted here, without a network, because the
deployment that needs it is exactly the deployment where a test *could* not reach Hugging
Face either:

* a pinned `DECIS_HUB` never probes and never substitutes;
* auto probes Hugging Face and falls back only when the host does not answer at all;
* `HF_ENDPOINT` is the endpoint the probe asks, so a mirror is not misread as "blocked";
* the pinned commit is **never** handed to ModelScope -- it does not fail there, it silently
  downloads nothing (`docs/design-review.md` §2-D32), so the only safe thing is to leave it
  out and say so;
* what the client is asked for is the same manifest the Hugging Face path is asked for, and
  the layout it lands is one `paths.resolve` accepts -- the D17 rule, applied to a second
  client.
"""

from __future__ import annotations

import sys
import types

import pytest

from decis.config import Settings
from decis.hub import (
    DEFAULT_ENDPOINT,
    HubUnavailableError,
    choose,
    client_installed,
    download,
    hugging_face_endpoint,
    probe_endpoint,
    probe_url,
)
from decis.paths import WeightSpec, checkpoint_root

SPEC = WeightSpec(
    engine_id="laya-multilingual",
    repo_id="convaiinnovations/laya",
    subfolder="multilingual",
    revision="a" * 40,
    marker="rl_agent_config.json",
)


def settings_with(**changes: object) -> Settings:
    return Settings(**changes)  # type: ignore[arg-type]


def unreachable(url: str) -> bool:
    """A probe that fails, and says which URL it was asked about."""
    unreachable.seen.append(url)  # type: ignore[attr-defined]
    return False


unreachable.seen = []  # type: ignore[attr-defined]


def never_called(url: str) -> bool:
    pytest.fail(f"a pinned source probed {url} instead of doing what it was told")


class _Response:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# --- the probe ------------------------------------------------------------------


def test_any_http_status_counts_as_reachable() -> None:
    """A 401 or 404 proves the host is up; only a transport failure means "blocked".

    This is what keeps an authenticated or private mirror from being misread as unreachable
    and quietly swapped for a *different* source.
    """
    import urllib.error

    def forbidden(url: str, timeout: float) -> _Response:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)  # type: ignore[arg-type]

    assert probe_endpoint("https://huggingface.co/api/models?limit=1", open_url=forbidden) is True


def test_a_transport_failure_is_unreachable() -> None:
    import urllib.error

    def no_dns(url: str, timeout: float) -> _Response:
        raise urllib.error.URLError("nodename nor servname provided")

    assert probe_endpoint("https://huggingface.co/api/models?limit=1", open_url=no_dns) is False


def test_an_answer_reads_as_reachable() -> None:
    assert (
        probe_endpoint("https://huggingface.co/api/models?limit=1", open_url=lambda url, timeout: _Response()) is True
    )


def test_the_probe_asks_a_listing_and_not_a_repository() -> None:
    """A probe of one repository would read "renamed" as "blocked"."""
    url = probe_url(settings_with())
    assert url == f"{DEFAULT_ENDPOINT}/api/models?limit=1"


def test_hf_endpoint_is_the_host_the_probe_asks() -> None:
    """A mirror is a supported deployment, not an unreachable Hugging Face."""
    settings = settings_with(hf_endpoint="https://hf-mirror.example/")
    assert hugging_face_endpoint(settings) == "https://hf-mirror.example"
    assert probe_url(settings).startswith("https://hf-mirror.example/")


# --- the decision ---------------------------------------------------------------


def test_a_pinned_source_does_not_probe() -> None:
    """`DECIS_HUB=huggingface` is how a deployment says "fail rather than substitute"."""
    chosen = choose(settings_with(hub="huggingface"), probe=never_called)
    assert chosen.name == "huggingface"
    assert chosen.pinned is True


def test_auto_uses_hugging_face_when_it_answers() -> None:
    chosen = choose(settings_with(), probe=lambda url: True)
    assert chosen.name == "huggingface"
    assert chosen.pinned, "Hugging Face can address the pinned commit, so nothing is given up"
    assert DEFAULT_ENDPOINT in chosen.reason


def test_auto_falls_back_when_hugging_face_does_not_answer() -> None:
    unreachable.seen = []
    chosen = choose(settings_with(), probe=unreachable)
    assert chosen.name == "modelscope"
    assert unreachable.seen == [probe_url(settings_with())], "the probe must ask the configured endpoint"
    assert not chosen.pinned, "ModelScope has no commit revisions; the deviation must be visible"


def test_the_fallback_names_what_it_gives_up() -> None:
    """`Selection.describe()` is what a user reads before 647 MiB arrive."""
    text = choose(settings_with(), probe=unreachable).describe()
    assert "modelscope" in text
    assert "not the pinned commit" in text


def test_the_fallback_warns_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="decis.hub"):
        choose(settings_with(), probe=unreachable)
    assert any("pinned revision cannot be honored" in record.getMessage() for record in caplog.records)


def test_a_pinned_modelscope_skips_the_probe_too() -> None:
    chosen = choose(settings_with(hub="modelscope"), probe=never_called)
    assert chosen.name == "modelscope"
    assert not chosen.pinned


def test_client_installed_answers_for_the_module_each_source_needs() -> None:
    """A bool, and the *right* module -- checked without importing either client."""
    import importlib.util

    for name, module in (("huggingface", "huggingface_hub"), ("modelscope", "modelscope")):
        assert client_installed(name) is (importlib.util.find_spec(module) is not None), name


# --- what each client is asked for ----------------------------------------------


def fake_module(monkeypatch: pytest.MonkeyPatch, name: str, function: object) -> None:
    module = types.ModuleType(name)
    setattr(module, function.__name__ if callable(function) else "snapshot_download", function)  # type: ignore[union-attr]
    monkeypatch.setitem(sys.modules, name, module)


def hugging_face_spy(monkeypatch: pytest.MonkeyPatch, *, landing) -> dict:
    """A `huggingface_hub.snapshot_download` that records its call and writes `landing`."""
    seen: dict = {}

    def snapshot_download(**kwargs: object) -> str:
        seen.update(kwargs)
        landing.mkdir(parents=True, exist_ok=True)
        return str(landing)

    fake_module(monkeypatch, "huggingface_hub", snapshot_download)
    return seen


def modelscope_spy(monkeypatch: pytest.MonkeyPatch, *, landing) -> dict:
    """A ModelScope client that writes the layout the real one writes, under `local_dir`."""
    seen: dict = {}

    def snapshot_download(**kwargs: object) -> str:
        seen.update(kwargs)
        root = landing
        for pattern in kwargs.get("allow_file_pattern") or []:
            target = root / str(pattern)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"weights")
        root.mkdir(parents=True, exist_ok=True)
        return str(root)

    fake_module(monkeypatch, "modelscope", snapshot_download)
    return seen


def test_the_hugging_face_path_still_carries_the_pin_and_the_file_list(tmp_path, monkeypatch) -> None:
    seen = hugging_face_spy(monkeypatch, landing=tmp_path / "hf")
    landed = download(SPEC, selection=choose(settings_with(hub="huggingface"), probe=never_called))
    assert landed == tmp_path / "hf"
    assert seen["revision"] == "a" * 40
    assert seen["repo_id"] == "convaiinnovations/laya"
    assert "multilingual/model.safetensors" in seen["allow_patterns"]


def test_the_modelscope_path_never_receives_the_pinned_commit(tmp_path, monkeypatch) -> None:
    """`docs/design-review.md` §2-D32: the sha is not rejected there, it downloads nothing.

    Verified against modelscope 1.40.1 with `convaiinnovations/laya`: passing the Hugging
    Face commit sha logs "No files to download for <repo>@<sha>", returns **success**, and
    leaves an empty directory. Handing it over would turn the pinned revision into a silent
    no-op -- the failure mode this repository has a whole file of.
    """
    seen = modelscope_spy(monkeypatch, landing=tmp_path / "ms")
    landed = download(
        SPEC,
        selection=choose(settings_with(hub="modelscope"), probe=never_called),
        destination=tmp_path / "ms",
    )
    assert landed == tmp_path / "ms"
    assert "revision" not in seen, f"the pinned commit reached ModelScope: {seen}"
    assert seen["model_id"] == "convaiinnovations/laya"
    assert seen["local_dir"] == str(tmp_path / "ms")
    # The same manifest, under the spelling that client uses.
    assert "multilingual/rl_agent_config.json" in seen["allow_file_pattern"]
    assert not any(pattern.startswith("typed-decisions/") for pattern in seen["allow_file_pattern"])


def test_a_modelscope_download_lands_a_layout_the_resolver_accepts(tmp_path, monkeypatch) -> None:
    """The oracle is `checkpoint_root`, exactly as it is for the Hugging Face path.

    Asserting the arguments while the writer and the reader disagree about the layout is the
    §2-D17 mistake; so the fake writes files and the resolver is asked whether it believes
    them.
    """
    modelscope_spy(monkeypatch, landing=tmp_path / "ms")
    landed = download(SPEC, selection=choose(settings_with(hub="modelscope"), probe=never_called))
    root = checkpoint_root(landed, SPEC)
    assert root == landed / "multilingual"
    assert (root / "rl_agent_config.json").is_file()


def test_without_a_cachedirectory_the_client_owns_the_destination(tmp_path, monkeypatch) -> None:
    """No `local_dir` when no model directory is configured: the client's own cache."""
    seen = modelscope_spy(monkeypatch, landing=tmp_path / "cache")
    download(SPEC, selection=choose(settings_with(hub="modelscope"), probe=never_called))
    assert "local_dir" not in seen, "a cache destination must not be turned into a local_dir"


def test_a_missing_modelscope_client_says_how_to_install_it(monkeypatch) -> None:
    """`uv sync --extra download` is the extra that exists; naming a wrong one is worse than silence."""
    monkeypatch.setitem(sys.modules, "modelscope", None)
    with pytest.raises(HubUnavailableError, match=r"uv sync --extra download"):
        download(
            SPEC,
            selection=choose(settings_with(hub="modelscope"), probe=never_called),
            destination=None,
        )


def test_a_missing_hugging_face_client_says_how_to_install_it(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(HubUnavailableError, match=r"uv sync --extra download"):
        download(SPEC, selection=choose(settings_with(hub="huggingface"), probe=never_called))
