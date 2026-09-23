"""Weight location.

Worth its own suite because this is where "mount your own model" either works or
quietly does not. `DECIS_MODEL_DIR` is normally a volume mount, and a mount point
exists even when the volume behind it is empty -- so the common failure is a directory
that is present, readable and wrong. The resolver has to reject it here rather than let
the model loader fail with something unhelpful much later.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from decis.config import Settings, load_settings
from decis.paths import (
    WeightSpec,
    candidate_directories,
    checkpoint_root,
    download_arguments,
    filesystem_has_room,
    human_bytes,
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


def test_per_engine_overrides_are_read_from_the_environment(monkeypatch) -> None:
    """`DECIS_MODEL_PATH_LAYA_MULTILINGUAL` -> the `laya-multilingual` engine."""
    monkeypatch.setenv("DECIS_MODEL_PATH_LAYA_MULTILINGUAL", "/srv/acme")
    monkeypatch.setenv("DECIS_MODEL_PATH_KEV_0_8B", "/srv/kev")
    settings = load_settings(env_file="")
    assert settings.model_paths["laya-multilingual"] == Path("/srv/acme")
    assert settings.model_paths["kev-0-8b"] == Path("/srv/kev")


def test_a_blank_override_is_ignored(monkeypatch, tmp_path: Path) -> None:
    """An unset variable in a compose file arrives as an empty string, not as absent."""
    monkeypatch.setenv("DECIS_MODEL_PATH_LAYA", "   ")
    assert "laya" not in load_settings(env_file="").model_paths


def test_an_override_wins_even_when_the_shared_directory_also_has_one(tmp_path: Path) -> None:
    """Documented precedence, asserted end to end through the environment."""
    import os

    checkpoint(tmp_path / "models" / "laya")
    fine_tune = tmp_path / "mine"
    checkpoint(fine_tune)

    os.environ["DECIS_MODEL_PATH_LAYA"] = str(fine_tune)
    try:
        settings = load_settings(env_file="")
    finally:
        del os.environ["DECIS_MODEL_PATH_LAYA"]

    root_spec = dataclasses.replace(SPEC, engine_id="laya", subfolder=None)
    source = resolve(root_spec, dataclasses.replace(settings, model_dir=tmp_path / "models"))
    assert source.path == fine_tune
