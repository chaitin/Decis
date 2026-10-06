"""Which hub serves the weights, and how one fetch from it is expressed.

`paths.py` answers "a directory on disk, or over the network?". This module answers the
question after that -- *where* over the network -- and it is the only place that names a
hub client. There are two: `huggingface_hub`, which can address a commit, and `modelscope`,
whose mirrors answer on networks that cannot reach Hugging Face at all.

Why a fallback rather than a mirror setting: `HF_ENDPOINT` already covers "point the same
client at another host" and it keeps the pin. It does not help when the host itself is
blocked and no mirror has been configured -- which is the case this exists for. A probe
decides reachability, once per command, and `DECIS_HUB` overrides the decision in either
direction (`docs/configuration.md`).

**The pin cannot be honored on ModelScope, and that is not a detail**
(`docs/design-review.md` §2-D32`). ModelScope revisions are branch and tag *names*; the
mirrors of these repositories have `master` and nothing else. Handing ModelScope the
Hugging Face commit sha does not raise: it logs "No files to download for
<repo>@<sha>" and returns **success**, so `decis download` only finds out that nothing
arrived when it validates the directory. So the sha is never passed. The mirror's own
current revision is used, `Selection.pinned` says so, and every caller tells the user.
An operator who needs the pin to mean something pins the source instead:
`DECIS_HUB=huggingface` fails loudly rather than quietly serving different bytes.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .paths import WeightSpec, download_arguments, module_available

_logger = logging.getLogger("decis.hub")

#: The endpoint `huggingface_hub` uses when `HF_ENDPOINT` is unset. Spelled out rather than
#: imported from that library because this module has to be able to probe before importing
#: a client -- an API-only image has neither of them installed.
DEFAULT_ENDPOINT = "https://huggingface.co"

#: How long the reachability probe may take. It sits on the path of an interactive command,
#: so it is a couple of seconds, not a download timeout: a Hub that cannot answer a listing
#: in this long is a Hub this command cannot use.
PROBE_TIMEOUT_S = 3.0

#: The importable module each source needs. Kept next to the fetchers so "the client is not
#: installed" is reportable *before* a download starts, rather than as an ImportError from
#: the middle of one.
CLIENTS = {"huggingface": "huggingface_hub", "modelscope": "modelscope"}

#: What `decis download --engine` and `decis doctor` call each source. The reason a user
#: reads is decided in `choose`, not here.
HUGGINGFACE = "huggingface"
MODELSCOPE = "modelscope"


class HubUnavailableError(RuntimeError):
    """The selected hub cannot be used here: its client is missing, or it was configured wrong."""


@dataclass(frozen=True)
class Selection:
    """Where one fetch will come from, and what choosing it costs.

    `pinned` is the honest half: it is False when the checkpoint's declared revision cannot
    be addressed at this source, which is the case for every ModelScope mirror. Callers print
    `reason` before the download so the deviation is visible before 8 GiB of weights arrive,
    not after.
    """

    name: str
    reason: str
    #: The base URL the fetch goes to, when the source has one worth naming. ModelScope's is
    #: decided inside its own SDK (it reads `MODELSCOPE_DOMAIN`), so this stays empty rather
    #: than naming a host that the SDK may not use.
    endpoint: str = ""
    pinned: bool = True

    @property
    def module(self) -> str:
        """The package this source needs installed."""
        return CLIENTS[self.name]

    def describe(self) -> str:
        where = f"{self.name} at {self.endpoint}" if self.endpoint else self.name
        pin = "the pinned revision" if self.pinned else "the mirror's current revision, not the pinned commit"
        return f"{where}: {self.reason}; fetches {pin}"


def hugging_face_endpoint(settings: Settings) -> str:
    """The endpoint the probe asks -- the same one `huggingface_hub` will use.

    `HF_ENDPOINT` is honored even though it belongs to that library: probing anything else
    would answer a question nobody asked, and an internal mirror is exactly the deployment
    that must keep working when the public host is blocked.
    """
    return (settings.hf_endpoint or DEFAULT_ENDPOINT).rstrip("/")


def probe_url(settings: Settings) -> str:
    """A listing URL that is reachable wherever the Hub is reachable.

    Not a specific repository: the probe answers "can this host speak to that Hub at all",
    and a 404 for a repository that was renamed would otherwise read as "blocked".
    """
    return f"{hugging_face_endpoint(settings)}/api/models?limit=1"


def probe_endpoint(url: str, *, timeout: float = PROBE_TIMEOUT_S, open_url: Callable[..., Any] | None = None) -> bool:
    """Whether `url`'s host answers at all.

    Any HTTP status counts as an answer: a 404 or a 401 proves the host is up and speaking
    HTTP, while only a transport failure (DNS, TCP, TLS, timeout) means "this network cannot
    use that Hub". That is the entire distinction the fallback needs, and it is what keeps a
    private/authenticated mirror from being misread as unreachable.

    `open_url` is a parameter so the rule can be tested without a network, in the same spirit
    as `config.normalize_proxy_environment(environ=...)`.
    """
    opener = open_url or urllib.request.urlopen
    try:
        with opener(url, timeout=timeout) as response:
            response.read(64)
        return True
    except urllib.error.HTTPError:
        # `HTTPError` is an `OSError`, so it has to be caught first: the host answered.
        return True
    except Exception:
        # Anything else -- URLError, a TLS failure, a malformed response, a timeout -- means
        # this endpoint is not usable from here. Broad on purpose: the caller only needs a
        # boolean, and an unanticipated transport exception must not turn into a traceback.
        return False


def choose(settings: Settings, *, probe: Callable[[str], bool] | None = None) -> Selection:
    """Decide which hub to fetch from.

    A pinned `DECIS_HUB` never probes and never falls back: an operator who named a source
    gets that source and its failures, which is the whole point of naming one.
    """
    endpoint = hugging_face_endpoint(settings)
    if settings.hub == HUGGINGFACE:
        return Selection(HUGGINGFACE, "DECIS_HUB=huggingface", endpoint=endpoint)
    if settings.hub == MODELSCOPE:
        return Selection(MODELSCOPE, "DECIS_HUB=modelscope", pinned=False)

    checker = probe or probe_endpoint
    url = probe_url(settings)
    if checker(url):
        return Selection(HUGGINGFACE, f"{endpoint} answered", endpoint=endpoint)
    _logger.warning(
        "%s did not answer; falling back to ModelScope. ModelScope mirrors have no commit "
        "revisions, so the checkpoint's pinned revision cannot be honored there "
        "(docs/design-review.md §2-D32). Set DECIS_HUB=huggingface to fail instead.",
        endpoint,
    )
    return Selection(MODELSCOPE, f"{endpoint} did not answer", pinned=False)


def client_installed(name: str) -> bool:
    """Whether `name`'s client package is importable here, without importing it.

    Delegates to `paths.module_available`: "is it installed" has one home, and this used to
    be a second `find_spec` without the `except` that a missing parent package needs.
    """
    return module_available(CLIENTS[name])


def download(spec: WeightSpec, *, selection: Selection, destination: Path | None = None) -> Path:
    """Fetch `spec` from `selection` and return the directory it landed in.

    `destination=None` means "the client's own cache", which is what the loaders use and what
    `decis download` writes to when no `DECIS_MODEL_DIR` is configured.

    Validating the result is the caller's job (`paths.checkpoint_root`): a client returning
    successfully is not the same claim as "a loader can read a checkpoint here", and §2-D17
    is what that distinction cost the first time it was skipped.
    """
    arguments = download_arguments(spec)
    if selection.name == MODELSCOPE:
        return _from_modelscope(spec, arguments, destination)
    return _from_hugging_face(spec, arguments, destination)


def _from_hugging_face(spec: WeightSpec, arguments: dict[str, object], destination: Path | None) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise HubUnavailableError(
            f"`huggingface_hub` is not installed, so {spec.repo_id} cannot be fetched. It arrives with "
            f"any engine extra (`uv sync --extra laya`), or on its own with `uv sync --extra download`."
        ) from exc
    if destination is None:
        return Path(snapshot_download(**arguments))  # type: ignore[arg-type]
    return Path(snapshot_download(local_dir=str(destination), **arguments))  # type: ignore[arg-type]


def _from_modelscope(spec: WeightSpec, arguments: dict[str, object], destination: Path | None) -> Path:
    """ModelScope's `snapshot_download`, with the pin deliberately left out.

    The key is `allow_file_pattern` rather than the newer `allow_patterns` alias: it is the
    spelling every modelScope version in use accepts, and the two are documented as
    equivalent where both exist. Verified against 1.40.1: a subfolder-prefixed pattern list
    lands exactly the files `download_arguments` names.
    """
    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise HubUnavailableError(
            f"{spec.repo_id} would be fetched from ModelScope, but the `modelscope` package is not "
            f"installed here. Install it with `uv sync --extra download` -- every engine extra already "
            f"pulls that in -- or set DECIS_HUB=huggingface to fail instead of falling back."
        ) from exc

    # No `revision=`: see the module docstring. Passing the sha does not fail there, it
    # silently downloads nothing (§2-D32).
    kwargs: dict[str, object] = {
        "model_id": arguments["repo_id"],
        "allow_file_pattern": list(arguments["allow_patterns"]),  # type: ignore[arg-type]
    }
    if destination is not None:
        kwargs["local_dir"] = str(destination)
    return Path(str(snapshot_download(**kwargs)))  # type: ignore[arg-type]


__all__ = [
    "CLIENTS",
    "DEFAULT_ENDPOINT",
    "HUGGINGFACE",
    "MODELSCOPE",
    "PROBE_TIMEOUT_S",
    "HubUnavailableError",
    "Selection",
    "choose",
    "client_installed",
    "download",
    "hugging_face_endpoint",
    "probe_endpoint",
    "probe_url",
]
