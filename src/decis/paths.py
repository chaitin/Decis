"""Where model weights come from.

One question, one answer, in one place: given an engine's declared `WeightSpec`
and the server's configuration, is this a directory on disk or a Hugging Face
repository, and exactly what will be loaded?

Why this is not inside the engines: images ship with weights baked in, but
`DECIS_MODEL_DIR` is meant to let an operator mount their own -- including a
fine-tune the registry has never heard of. If every engine decided that for
itself, "mount your own model" would work for one engine and silently not for
another. `AGENTS.md §2` names this module as the canonical home.

The resolution order is a deliberate security property as much as a convenience:
an explicitly mounted directory always wins over a network fetch, so a container
that is supposed to run offline cannot be tricked into reaching out to the Hub by
a missing volume.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Settings

#: Files that make up one checkpoint, relative to its subfolder. Listing them is
#: what keeps a download from pulling every sibling checkpoint in the repository.
_CHECKPOINT_FILES = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")


@dataclass(frozen=True)
class BaseModel:
    """A published model a checkpoint *adapts*, without which it cannot run.

    kev's checkpoints are LoRA adapters over a Qwen base: the adapter repository
    holds `adapter_model.safetensors` and a pointer head, and not a single one of
    the tensors that actually compute anything. A `WeightSpec` that named only the
    adapter would let `decis download` report success and leave a checkpoint that
    cannot produce a number, which is the failure this exists to prevent.

    `checkpoint_files` is per-base rather than shared because a base is a normal
    transformers model -- config, weights, tokenizer -- not a Decis checkpoint.
    """

    repo_id: str
    revision: str | None = None
    marker: str = "config.json"
    expected_bytes: int | None = None
    checkpoint_files: tuple[str, ...] = ("*.json", "*.safetensors", "tokenizer*", "*.txt", "*.jinja")

    @property
    def directory_name(self) -> str:
        """The directory name a local mount would use, matching the Hub's own."""
        return self.repo_id.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class WeightSpec:
    """What an engine needs to load, declared before anything is downloaded.

    This is what `decis download` reads and what `decis doctor` reports, so it has
    to be answerable without the engine's dependencies installed -- hence plain
    data with no framework imports.
    """

    engine_id: str
    #: Hugging Face repository, or None for an engine that has no published weights.
    repo_id: str | None = None
    #: Checkpoint inside a repository that bundles several.
    subfolder: str | None = None
    #: Pinned commit. A tag or branch would let an upstream force-push change what a
    #: given Decis build loads, which makes a released image unreproducible.
    revision: str | None = None
    #: A file whose presence means "this directory is a complete checkpoint".
    marker: str | None = None
    #: Directory name to look for under `DECIS_MODEL_DIR`. Defaults to `engine_id`,
    #: but engines sharing one repository need distinct names for their subfolders.
    local_dir_name: str | None = None
    #: Published size, for `decis doctor` and progress reporting. Not enforced.
    expected_bytes: int | None = None
    license_name: str = "Apache-2.0"
    license_url: str = ""
    #: Top-level modules that must be importable for this checkpoint to load -- the
    #: packages its engine extra provides. Importing the engine *class* succeeds
    #: without them (that is the point of the lazy imports in AGENTS.md §6), so
    #: without this the registry cannot distinguish "registered" from "runnable".
    requires: tuple[str, ...] = ()
    #: Files that make up this checkpoint, relative to `subfolder`. Per-spec because
    #: a Laya checkpoint and a kev adapter are made of different files.
    checkpoint_files: tuple[str, ...] = _CHECKPOINT_FILES
    #: Models this checkpoint adapts. Declared so `decis download` fetches them and
    #: `decis doctor` reports on them; see `BaseModel`.
    bases: tuple[BaseModel, ...] = ()

    def directory_name(self) -> str:
        return self.local_dir_name or self.engine_id

    def base_specs(self) -> tuple[WeightSpec, ...]:
        """This spec's bases, as specs, so one set of resolution rules covers them.

        Derived rather than declared separately so a base cannot drift out of sync
        with the checkpoint that names it. `engine_id` is namespaced because a base
        is not independently servable and must not appear in `GET /v1/models` as one.
        """
        return tuple(
            WeightSpec(
                engine_id=f"{self.engine_id}->{base.repo_id}",
                repo_id=base.repo_id,
                revision=base.revision,
                marker=base.marker,
                local_dir_name=base.directory_name,
                expected_bytes=base.expected_bytes,
                license_name=self.license_name,
                license_url=self.license_url,
                checkpoint_files=base.checkpoint_files,
            )
            for base in self.bases
        )

    def is_downloadable(self) -> bool:
        return self.repo_id is not None


@dataclass(frozen=True)
class WeightSource:
    """A resolved decision about where weights will be read from."""

    engine_id: str
    kind: Literal["local", "hub", "none"]
    #: Set when `kind == "local"`.
    path: Path | None = None
    #: Set when `kind == "hub"`.
    repo_id: str | None = None
    subfolder: str | None = None
    revision: str | None = None

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def describe(self) -> str:
        if self.kind == "local":
            return str(self.path)
        if self.kind == "hub":
            location = f"{self.repo_id}/{self.subfolder}" if self.subfolder else str(self.repo_id)
            return f"{location}@{self.revision}" if self.revision else location
        return "no weights"


def _explicit_override(engine_id: str, settings: Settings) -> Path | None:
    return settings.model_paths.get(engine_id)


def candidate_directories(spec: WeightSpec, settings: Settings) -> list[Path]:
    """Every local directory that could hold this engine's weights, best first."""
    directories: list[Path] = []
    override = _explicit_override(spec.engine_id, settings)
    if override is not None:
        directories.append(override)
    if settings.model_dir is not None:
        base = Path(settings.model_dir)
        # The engine's own directory, then the name it shares with sibling
        # checkpoints from the same repository.
        for name in (spec.directory_name(), spec.engine_id):
            candidate = base / name
            if candidate not in directories:
                directories.append(candidate)
    return directories


def checkpoint_root(directory: Path, spec: WeightSpec) -> Path | None:
    """The directory to hand the loader, or None if this is not a checkpoint.

    Two layouts are valid and both occur in practice:

    * `directory` *is* the checkpoint (what `decis download` writes);
    * `directory` contains the subfolder (a raw `snapshot_download`, or a mount
      that mirrors the repository).

    Returning the resolved root rather than a boolean keeps the caller from
    appending the subfolder twice, which is the failure this exists to prevent.
    """
    if not directory.is_dir():
        return None
    if spec.marker is None:
        return directory if any(directory.iterdir()) else None
    if (directory / spec.marker).is_file():
        return directory
    if spec.subfolder is not None and (directory / spec.subfolder / spec.marker).is_file():
        return directory / spec.subfolder
    return None


def resolve(spec: WeightSpec, settings: Settings) -> WeightSource:
    """Decide where to read weights from, preferring local over network."""
    for directory in candidate_directories(spec, settings):
        root = checkpoint_root(directory, spec)
        if root is not None:
            return WeightSource(engine_id=spec.engine_id, kind="local", path=root)
    if spec.is_downloadable():
        return WeightSource(
            engine_id=spec.engine_id,
            kind="hub",
            repo_id=spec.repo_id,
            subfolder=spec.subfolder,
            revision=spec.revision,
        )
    return WeightSource(engine_id=spec.engine_id, kind="none")


def download_arguments(spec: WeightSpec) -> dict[str, object]:
    """Arguments for `huggingface_hub.snapshot_download`, restricted to one checkpoint.

    The upstream repository bundles several checkpoints under distinct subfolders;
    an unrestricted snapshot would pull all of them. Keeping the file list here
    rather than at the call site means `decis download` and the loader agree on
    what a checkpoint contains.
    """
    if spec.repo_id is None:
        raise ValueError(f"{spec.engine_id} has no published weights to download")
    prefix = f"{spec.subfolder}/" if spec.subfolder else ""
    return {
        "repo_id": spec.repo_id,
        "revision": spec.revision,
        "allow_patterns": [prefix + name for name in spec.checkpoint_files],
    }


def describe_local(directory: Path) -> str:
    """A human-readable size, for `decis doctor`. Never raises."""
    try:
        total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
    except OSError:
        return "size unknown"
    return human_bytes(total)


def human_bytes(count: int | float) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def missing_requirements(spec: WeightSpec) -> list[str]:
    """Which of `spec.requires` cannot be imported here.

    Importing an engine *class* succeeds without its heavy dependencies by design
    (AGENTS.md §6), so `registry.describe()` succeeding says nothing about whether
    `load()` would work. This is the missing half of that answer.
    """
    import importlib.util

    missing = []
    for module in spec.requires:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            # The parent package is itself missing, or the module is a broken
            # namespace: either way it cannot be imported.
            found = False
        if not found:
            missing.append(module)
    return missing


def filesystem_has_room(directory: Path, needed_bytes: int | None) -> bool | None:
    """Whether the target filesystem can hold the download. None if unknown."""
    if needed_bytes is None:
        return None
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = os.statvfs(probe).f_bavail * os.statvfs(probe).f_frsize
    except (OSError, AttributeError):
        return None
    return free >= needed_bytes


__all__ = [
    "WeightSource",
    "WeightSpec",
    "candidate_directories",
    "checkpoint_root",
    "describe_local",
    "download_arguments",
    "filesystem_has_room",
    "human_bytes",
    "missing_requirements",
    "resolve",
]
