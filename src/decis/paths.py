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

import json
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


def missing_shards(root: Path) -> list[str]:
    """Files a checkpoint's own manifest names but that are not in `root`.

    A sharded checkpoint ships `*.safetensors.index.json`, whose `weight_map` maps every
    tensor to the file holding it. That manifest is the exact list `transformers` will
    open, which is why this is the check rather than a byte count: `expected_bytes` is
    declared per checkpoint and could drift, while the index cannot disagree with the
    loader.

    It matters because a checkpoint arrives at a mounted directory *in pieces* -- the last
    shard lands last -- so an interrupted `decis download --dest`, a `cp` that ran out of
    disk, or a partially synced volume all look complete from the outside: the config, the
    tokenizer and the index are there. Without this, that directory is resolved as a local
    checkpoint, `decis models` calls it **ready**, and the loader raises
    `FileNotFoundError` from inside `transformers` naming a shard nobody was told to expect
    (`docs/design-review.md` §2-D29).

    Empty list means "nothing to complain about": either there is no index (single-file
    checkpoints, and every base model of the Laya family) or every file it names is present.
    """
    for index_file in sorted(root.glob("*.safetensors.index.json")):
        try:
            manifest = json.loads(index_file.read_text(encoding="utf-8"))
            weight_map = manifest["weight_map"]
        except (OSError, ValueError, KeyError, TypeError):
            # An unreadable or malformed manifest cannot be trusted to describe what is
            # here, so it is reported as the missing piece rather than skipped.
            return [index_file.name]
        if not isinstance(weight_map, dict):
            return [index_file.name]
        names = {name for name in weight_map.values() if isinstance(name, str)}
        if not names:
            return [index_file.name]
        missing = sorted(name for name in names if not (root / name).is_file())
        if missing:
            return missing
    return []


def checkpoint_root(directory: Path, spec: WeightSpec) -> Path | None:
    """The directory to hand the loader, or None if this is not a usable checkpoint.

    Two layouts are valid and both occur in practice:

    * `directory` *is* the checkpoint (what `decis download` writes);
    * `directory` contains the subfolder (a raw `snapshot_download`, or a mount
      that mirrors the repository).

    Returning the resolved root rather than a boolean keeps the caller from
    appending the subfolder twice, which is the failure this exists to prevent.

    "Usable" is two conditions, not one: the marker must be there, *and* every shard the
    checkpoint's own index names must be there (`missing_shards`). The marker alone is not
    enough -- see `design-review.md` §2-D29 -- and a directory that fails the second
    condition is reported as "not a checkpoint here", so resolution falls through to the
    Hub and the user gets a complete copy instead of a crash from inside the loader.
    """
    if not directory.is_dir():
        return None
    if spec.marker is None:
        root = directory if any(directory.iterdir()) else None
    elif (directory / spec.marker).is_file():
        root = directory
    elif spec.subfolder is not None and (directory / spec.subfolder / spec.marker).is_file():
        root = directory / spec.subfolder
    else:
        root = None
    if root is None or missing_shards(root):
        return None
    return root


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


def fetch_checkpoint(spec: WeightSpec, settings: Settings) -> Path:
    """Fetch `spec` from a Hub and return the directory a loader can read.

    The one place an engine gets weights over the network, and the reason it is here
    rather than in an engine (AGENTS.md §2): the pin and the file list are properties
    of the `WeightSpec`, and a caller that re-derived either would load a different
    checkpoint than the one this build declares. `laya.Agent(repo_id)` did exactly
    that -- upstream has no `revision` parameter, so the loader read `main` while the
    log line promised the pinned commit (`docs/design-review.md` §2-D26).

    *Which* Hub is `decis/hub.py`'s decision, not this function's: Hugging Face when it
    answers, ModelScope when the network cannot reach it and the operator has not pinned a
    source. Answers from that client's cache when it is there, so calling it on every start
    costs a couple of HEAD requests, not a download. Validated with `checkpoint_root` -- the
    same predicate a mounted directory goes through -- because "the download finished"
    and "the loader will find a checkpoint" are different claims (D17).

    A Hub that cannot be used here -- no client installed for the selected source -- is raised
    as `FileNotFoundError`, the type the engines already translate into
    `EngineUnavailableError` with this message ("the weights are not where I can reach them",
    which is exactly what it is).
    """
    from .hub import HubUnavailableError, choose
    from .hub import download as hub_download

    selection = choose(settings)
    try:
        downloaded = hub_download(spec, selection=selection)
    except HubUnavailableError as exc:
        raise FileNotFoundError(str(exc)) from exc
    root = checkpoint_root(downloaded, spec)
    if root is None:
        raise FileNotFoundError(
            f"{spec.repo_id} was fetched to {downloaded} but holds no {spec.marker}; "
            f"expected one of the files in {list(spec.checkpoint_files)}."
        )
    return root


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


def module_available(name: str) -> bool:
    """Whether the module `name` can be imported here, without importing it.

    The one answer to "is it installed on this machine", shared by the engine-dependency
    check below, the Hub clients (`hub.client_installed`) and the CLI, because the three
    used to each do their own `find_spec` -- and the two that skipped the `except` reported a
    broken namespace or missing parent package as an installed module.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # The parent package is itself missing, or the module is a broken namespace:
        # either way it cannot be imported.
        return False


def missing_requirements(spec: WeightSpec) -> list[str]:
    """Which of `spec.requires` cannot be imported here.

    Importing an engine *class* succeeds without its heavy dependencies by design
    (AGENTS.md §6), so `registry.describe()` succeeding says nothing about whether
    `load()` would work. This is the missing half of that answer.
    """
    return [module for module in spec.requires if not module_available(module)]


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
    "fetch_checkpoint",
    "filesystem_has_room",
    "human_bytes",
    "missing_requirements",
    "missing_shards",
    "resolve",
]
