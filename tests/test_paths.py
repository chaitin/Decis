"""Weight location.

Worth its own suite because this is where "mount your own model" either works or
quietly does not. `DECIS_MODEL_DIR` is normally a volume mount, and a mount point
exists even when the volume behind it is empty -- so the common failure is a directory
that is present, readable and wrong. The resolver has to reject it here rather than let
the model loader fail with something unhelpful much later.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from decis.config import Settings, load_settings
from decis.paths import (
    WeightSpec,
    candidate_directories,
    checkpoint_root,
    download_arguments,
    filesystem_has_room,
    human_bytes,
    missing_shards,
    resolve,
)

SPEC = WeightSpec(
    engine_id="laya-multilingual",
    repo_id="convaiinnovations/laya",
    subfolder="multilingual",
    revision="a" * 40,
    marker="rl_agent_config.json",
    expected_bytes=678_201_636,
)


def checkpoint(root: Path, *, subfolder: str | None = None) -> Path:
    """Write a minimal but *valid* checkpoint into `root`."""
    directory = root / subfolder if subfolder else root
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rl_agent_config.json").write_text("{}")
    (directory / "model.safetensors").write_bytes(b"weights")
    return directory


def settings_with(**changes: object) -> Settings:
    return dataclasses.replace(Settings(), **changes)  # type: ignore[arg-type]


# --- the decision itself -------------------------------------------------------


def test_no_configuration_means_the_hub() -> None:
    """Nothing mounted and nothing set: fetch it. That is the docker-run case."""
    source = resolve(SPEC, settings_with())
    assert source.kind == "hub"
    assert source.repo_id == "convaiinnovations/laya"
    assert source.subfolder == "multilingual"
    assert source.revision == "a" * 40
    assert not source.is_local


def test_an_engine_with_no_repository_and_no_directory_resolves_to_nothing() -> None:
    source = resolve(WeightSpec(engine_id="custom", marker="rl_agent_config.json"), settings_with())
    assert source.kind == "none"


def test_a_mounted_directory_wins_over_the_network(tmp_path: Path) -> None:
    """An offline container must not reach out to the Hub because a volume is present.

    This is a security property, not just a preference: the whole point of baking or
    mounting weights is that the image can run with no egress.
    """
    checkpoint(tmp_path / "laya-multilingual")
    source = resolve(SPEC, settings_with(model_dir=tmp_path))
    assert source.is_local
    assert source.path == tmp_path / "laya-multilingual"
    assert source.repo_id is None


def test_an_explicit_per_engine_override_beats_the_shared_directory(tmp_path: Path) -> None:
    """How you serve a fine-tune the registry has never heard of."""
    checkpoint(tmp_path / "shared" / "laya-multilingual")
    fine_tune = tmp_path / "finetunes" / "acme-triage"
    checkpoint(fine_tune)

    source = resolve(SPEC, settings_with(model_dir=tmp_path / "shared", model_paths={"laya-multilingual": fine_tune}))
    assert source.path == fine_tune


def test_a_directory_that_exists_but_is_empty_is_not_a_checkpoint(tmp_path: Path) -> None:
    """The failure mode a mount point creates: present, readable, wrong."""
    (tmp_path / "laya-multilingual").mkdir(parents=True)
    source = resolve(SPEC, settings_with(model_dir=tmp_path))
    assert source.kind == "hub", "an empty mount must not be mistaken for weights"


def test_a_directory_with_the_wrong_contents_is_not_a_checkpoint(tmp_path: Path) -> None:
    """A partially copied or partially downloaded directory must not be used."""
    partial = tmp_path / "laya-multilingual"
    partial.mkdir(parents=True)
    (partial / "model.safetensors").write_bytes(b"incomplete")
    source = resolve(SPEC, settings_with(model_dir=tmp_path))
    assert source.kind == "hub", "a checkpoint missing its config must not be used"


def test_a_repository_layout_mount_resolves_into_the_subfolder(tmp_path: Path) -> None:
    """A raw `snapshot_download`, or a mount mirroring the repo, has the subfolder inside.

    Returning the resolved root rather than the directory keeps the loader from
    appending the subfolder a second time.
    """
    outer = tmp_path / "laya-multilingual"
    checkpoint(outer, subfolder="multilingual")
    source = resolve(SPEC, settings_with(model_dir=tmp_path))
    assert source.is_local
    assert source.path == outer / "multilingual"


def test_the_flat_layout_also_resolves(tmp_path: Path) -> None:
    """What `decis download` writes: the checkpoint files directly in the directory."""
    checkpoint(tmp_path / "laya-multilingual")
    assert resolve(SPEC, settings_with(model_dir=tmp_path)).path == tmp_path / "laya-multilingual"


def test_a_root_checkpoint_is_found_by_its_own_name(tmp_path: Path) -> None:
    root_spec = dataclasses.replace(SPEC, engine_id="laya", subfolder=None)
    checkpoint(tmp_path / "laya")
    source = resolve(root_spec, settings_with(model_dir=tmp_path))
    assert source.path == tmp_path / "laya"


# --- candidate ordering --------------------------------------------------------


def test_candidates_are_the_override_then_the_configured_directory(tmp_path: Path) -> None:
    override = tmp_path / "override"
    candidates = candidate_directories(
        SPEC, settings_with(model_dir=tmp_path / "models", model_paths={"laya-multilingual": override})
    )
    assert candidates[0] == override
    assert tmp_path / "models" / "laya-multilingual" in candidates


def test_candidates_do_not_repeat_a_directory(tmp_path: Path) -> None:
    """`directory_name()` falls back to the engine id, which would otherwise list twice."""
    candidates = candidate_directories(SPEC, settings_with(model_dir=tmp_path))
    assert len(candidates) == len(set(candidates))


def test_no_model_dir_means_no_candidates() -> None:
    assert candidate_directories(SPEC, settings_with()) == []


def test_checkpoint_root_rejects_a_file(tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("x")
    assert checkpoint_root(a_file, SPEC) is None


def test_a_spec_with_no_marker_accepts_any_non_empty_directory(tmp_path: Path) -> None:
    spec = WeightSpec(engine_id="opaque")
    (tmp_path / "opaque").mkdir()
    assert checkpoint_root(tmp_path / "opaque", spec) is None
    (tmp_path / "opaque" / "anything").write_text("x")
    assert checkpoint_root(tmp_path / "opaque", spec) == tmp_path / "opaque"


# --- a checkpoint that is only half here ---------------------------------------
#
# Found by downloading the 8.65 GiB Gemma Jeff checkpoint and pointing a loader at the
# directory while the shards were still landing: `decision_config.json` and
# `model.safetensors.index.json` arrive first, so the marker meant "complete checkpoint"
# while `model-00001-of-00002.safetensors` did not exist yet. `decis models` called that
# **ready** and the loader raised `FileNotFoundError` from inside `transformers`
# (`docs/design-review.md` §2-D29). These are the assertions that keep the marker honest.


def sharded_checkpoint(root: Path, *, present: tuple[str, ...] = (), subfolder: str | None = None) -> Path:
    """A checkpoint with an index naming two shards, `present` of which are written."""
    directory = checkpoint(root, subfolder=subfolder)
    names = ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")
    (directory / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": 8},
                "weight_map": {f"layer.{index}": name for index, name in enumerate(names)},
            }
        ),
        encoding="utf-8",
    )
    for name in present:
        (directory / name).write_bytes(b"weights")
    return directory


def test_a_manifest_with_missing_shards_is_not_a_checkpoint(tmp_path: Path) -> None:
    """The marker is there and the weights are not: that is not a checkpoint."""
    directory = sharded_checkpoint(tmp_path / "half", present=("model-00001-of-00002.safetensors",))
    assert (directory / SPEC.marker).is_file(), "the precondition: the marker alone says yes"
    assert checkpoint_root(tmp_path / "half", SPEC) is None
    assert missing_shards(directory) == ["model-00002-of-00002.safetensors"]


def test_a_manifest_whose_shards_are_all_there_is_a_checkpoint(tmp_path: Path) -> None:
    directory = sharded_checkpoint(
        tmp_path / "whole",
        present=("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"),
    )
    assert missing_shards(directory) == []
    assert checkpoint_root(tmp_path / "whole", SPEC) == tmp_path / "whole"


def test_an_unreadable_manifest_is_not_silently_ignored(tmp_path: Path) -> None:
    """A manifest nobody can parse cannot vouch for the directory it sits in."""
    directory = sharded_checkpoint(tmp_path / "broken", present=("model-00001-of-00002.safetensors",))
    (directory / "model.safetensors.index.json").write_text("{not json", encoding="utf-8")
    assert missing_shards(directory) == ["model.safetensors.index.json"]
    assert checkpoint_root(tmp_path / "broken", SPEC) is None


def test_a_single_file_checkpoint_has_no_manifest_to_answer_to(tmp_path: Path) -> None:
    """Every Laya checkpoint and every kev adapter is one `model.safetensors`."""
    directory = checkpoint(tmp_path / "single")
    assert missing_shards(directory) == []
    assert checkpoint_root(tmp_path / "single", SPEC) == tmp_path / "single"


def test_a_half_downloaded_mount_falls_through_to_the_hub(tmp_path: Path) -> None:
    """The point of the predicate: a half-copied volume must not shadow a real fetch.

    `resolve` prefers local over network on purpose (offline containers depend on it), so a
    directory that *looks* local has to be a directory the loader can actually read --
    otherwise the security property "a mounted copy wins" turns into "an interrupted copy
    wins, and the service dies on startup".
    """
    sharded_checkpoint(tmp_path / "models" / "laya-multilingual", present=("model-00001-of-00002.safetensors",))
    source = resolve(SPEC, settings_with(model_dir=tmp_path / "models"))
    assert source.kind == "hub", "an incomplete local copy must not be reported as ready"


# --- downloads -----------------------------------------------------------------


def test_a_subfolder_download_is_restricted_to_that_checkpoint() -> None:
    """The repo bundles three checkpoints; an unrestricted snapshot would fetch all."""
    arguments = download_arguments(SPEC)
    patterns = arguments["allow_patterns"]
    assert isinstance(patterns, list)
    assert all(pattern.startswith("multilingual/") for pattern in patterns)
    assert "multilingual/model.safetensors" in patterns
    assert "multilingual/rl_agent_config.json" in patterns


def test_a_root_download_does_not_match_the_sibling_checkpoints() -> None:
    """No prefix must not degrade into "download everything"."""
    root_spec = dataclasses.replace(SPEC, engine_id="laya", subfolder=None)
    patterns = download_arguments(root_spec)["allow_patterns"]
    assert "model.safetensors" in patterns
    assert not any(str(pattern).startswith("multilingual/") for pattern in patterns)
    assert not any(str(pattern).startswith("typed-decisions/") for pattern in patterns)


def test_download_arguments_carry_the_pinned_revision() -> None:
    assert download_arguments(SPEC)["revision"] == SPEC.revision


def test_an_engine_without_a_repository_refuses_to_download() -> None:
    import pytest

    with pytest.raises(ValueError, match="no published weights"):
        download_arguments(WeightSpec(engine_id="stub"))


# --- fetching, for the loader rather than for `decis download` -------------------
#
# The stub writes the layout the real `snapshot_download` writes -- the named files,
# under the subfolder, inside a directory it returns -- because asserting which
# arguments were passed while the writer and the reader disagree about the layout is
# exactly the D17 mistake (`docs/design-review.md` §2). What is asserted is that
# `fetch_checkpoint` answers with a directory `paths` itself accepts.


def _fake_snapshot_download(monkeypatch, *, landing: Path, ignore_patterns: bool = False) -> dict:
    """Install a `huggingface_hub.snapshot_download` that really writes the files."""
    import sys
    import types

    seen: dict = {}

    def snapshot_download(**kwargs: object) -> str:
        seen.update(kwargs)
        patterns = [] if ignore_patterns else list(kwargs.get("allow_patterns") or [])
        for pattern in patterns:
            destination = landing / str(pattern)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"weights")
        landing.mkdir(parents=True, exist_ok=True)
        return str(landing)

    module = types.ModuleType("huggingface_hub")
    module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return seen


def test_fetching_a_checkpoint_returns_a_directory_the_resolver_accepts(tmp_path: Path, monkeypatch) -> None:
    """The oracle is `checkpoint_root` -- the same predicate a mounted directory passes."""
    from decis.paths import fetch_checkpoint

    landing = tmp_path / "hub" / "models--convaiinnovations--laya" / "snapshots" / ("a" * 40)
    seen = _fake_snapshot_download(monkeypatch, landing=landing)
    root = fetch_checkpoint(SPEC)
    assert seen["revision"] == "a" * 40, "the pin has to reach the Hub, not just the log line"
    assert root == landing / "multilingual"
    assert checkpoint_root(root, SPEC) == root


def test_a_download_that_lands_nothing_a_loader_can_read_is_an_error(tmp_path: Path, monkeypatch) -> None:
    """`snapshot_download` returning is not the same claim as "a checkpoint is here"."""
    import pytest

    from decis.paths import fetch_checkpoint

    _fake_snapshot_download(monkeypatch, landing=tmp_path / "empty", ignore_patterns=True)
    with pytest.raises(FileNotFoundError, match=r"holds no rl_agent_config\.json"):
        fetch_checkpoint(SPEC)


def test_human_bytes_reads_sensibly() -> None:
    assert human_bytes(512) == "512 B"
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(678_201_636) == "646.8 MiB"


def test_room_check_is_unknown_rather_than_wrong_when_nothing_is_declared(tmp_path: Path) -> None:
    """`None` means "cannot tell", and must not be reported as "no room"."""
    assert filesystem_has_room(tmp_path, None) is None


def test_a_small_request_obviously_fits(tmp_path: Path) -> None:
    assert filesystem_has_room(tmp_path, 1024) is True


# --- configuration plumbing ----------------------------------------------------


def test_the_environment_cannot_carry_a_per_engine_override(monkeypatch) -> None:
    """There is no `DECIS_MODEL_PATH_*`, and this is the assertion that keeps it gone.

    The variable had to encode the engine id in its own *name*, upper-cased with
    underscores, so `kev-0.8b` normalised to `kev-0-8b` and the override addressed no
    engine at all -- silently, because an override for an unknown id looks exactly like
    no override. Every Jeff id contains a dot, so the form was removed rather than
    documented around: `--model-path ENGINE=PATH` puts the id in the *value*, where it can
    be spelled exactly (AGENTS.md §9).
    """
    monkeypatch.setenv("DECIS_MODEL_PATH_LAYA_MULTILINGUAL", "/srv/acme")
    monkeypatch.setenv("DECIS_MODEL_PATH_KEV_0_8B", "/srv/kev")
    monkeypatch.setenv("DECIS_MODEL_PATH_JEFF_QWEN3_5_0_8B", "/srv/jeff")
    assert load_settings(env_file="").model_paths == {}


def test_an_override_wins_even_when_the_shared_directory_also_has_one(tmp_path: Path) -> None:
    """Documented precedence, asserted end to end from the command line to the loader.

    Written through `cli._parse_model_paths` rather than by hand so that the flag's own
    parsing is part of what this test connects: a typo there would leave the override
    pointing at a directory nobody reads, which is precisely the failure the removed
    environment variable had.
    """
    from decis.cli import _parse_model_paths

    checkpoint(tmp_path / "models" / "laya")
    fine_tune = tmp_path / "mine"
    checkpoint(fine_tune)

    overrides = _parse_model_paths([f"laya={fine_tune}"])
    settings = settings_with(model_dir=tmp_path / "models", model_paths=overrides)

    root_spec = dataclasses.replace(SPEC, engine_id="laya", subfolder=None)
    source = resolve(root_spec, settings)
    assert source.path == fine_tune
