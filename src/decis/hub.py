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

**A host that answers is not a host that is usable.** A probe answers "does this endpoint
speak HTTP from here"; a throttled or cross-border route answers that *and* then crawls
through a checkpoint that a faster route would deliver in minutes. So when a specific
checkpoint is about to be fetched, `auto` also measures both hubs -- one listing request plus
at most `SPEED_SAMPLE_BYTES` read from the largest file in the repository, capped at
`SPEED_SAMPLE_SECONDS` -- and takes the faster one when it wins by `SPEED_MARGIN`
(`docs/design-review.md` §2-D37). Two properties keep this honest:

* the measurement is **bounded and stated**: at most a listing and a megabyte per hub, each
  capped in bytes *and* in wall-clock time, with the numbers printed as the reason the source
  was picked;
* speed never wins silently over reproducibility. The pinned commit cannot be addressed on
  ModelScope at all (§2-D32), so a faster mirror is *different bytes* -- every caller
  prints `Selection.pinned`/`reason` before the transfer, and a deployment that needs the
  pin writes `DECIS_HUB=huggingface`.

A checkpoint that is already whole in *either* client's cache is used from there
(`cached`), before any measurement: re-downloading 8 GiB because the other hub won a race
would be a worse outcome than the slowness this exists to avoid.

**The pin cannot be honored on ModelScope, and that is not a detail**
(`docs/design-review.md` §2-D32). ModelScope revisions are branch and tag *names*; the
mirrors of these repositories have `master` and nothing else. Handing ModelScope the
Hugging Face commit sha does not raise: it logs "No files to download for
<repo>@<sha>" and returns **success**, so `decis download` only finds out that nothing
arrived when it validates the directory. So the sha is never passed. The mirror's own
current revision is used, `Selection.pinned` says so, and every caller tells the user.
An operator who needs the pin to mean something pins the source instead:
`DECIS_HUB=huggingface` fails loudly rather than quietly serving different bytes.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .paths import WeightSpec, checkpoint_root, download_arguments, module_available

_logger = logging.getLogger("decis.hub")

#: The endpoint `huggingface_hub` uses when `HF_ENDPOINT` is unset. Spelled out rather than
#: imported from that library because this module has to be able to probe before importing
#: a client -- an API-only image has neither of them installed.
DEFAULT_ENDPOINT = "https://huggingface.co"

#: The endpoint `modelscope` uses when neither `MODELSCOPE_ENDPOINT` nor the older
#: `MODELSCOPE_DOMAIN` is set. Same reason as `DEFAULT_ENDPOINT`: the listing and the sample
#: are fetched without importing that client.
DEFAULT_MODELSCOPE_ENDPOINT = "https://www.modelscope.cn"

#: The only revision ModelScope mirrors of these repositories carry (§2-D32). Spelled out
#: here because the sample URL names it; the client is handed no revision at all.
MODELSCOPE_REVISION = "master"

#: How long the reachability probe may take. It sits on the path of an interactive command,
#: so it is a couple of seconds, not a download timeout: a Hub that cannot answer a listing
#: in this long is a Hub this command cannot use.
PROBE_TIMEOUT_S = 3.0

#: The speed measurement: at most this many bytes, and at most this long, per hub. Both reads
#: are bounded in *both* dimensions (`_pull`): a size cap alone lets a trickling peer hold an
#: interactive command open, and a deadline alone lets a fast peer stream a gigabyte into the
#: decision. With `PROBE_TIMEOUT_S` per listing, choosing costs at most ~20 s (2 x 3 s + 2 x 6 s)
#: and 2 MiB of samples, plus whatever the listings carry -- each capped at
#: `LISTING_LIMIT_BYTES` -- against a transfer of hundreds of megabytes. It costs nothing at all
#: when a cache already holds the checkpoint or the source is pinned. A fire-and-forget ping
#: would answer a different question (`docs/design-review.md` §2-D37).
SPEED_SAMPLE_BYTES = 1 << 20
SPEED_SAMPLE_SECONDS = 6.0

#: A listing is a few kilobytes of JSON for every checkpoint this ships, but nothing in the
#: protocol promises that: a Hub that trickles a huge one must not be able to stretch the
#: documented cost. A body that gets cut off fails to parse, which reads as "this listing
#: could not be used" -- `available=True` with no speed, so the pinned source is kept.
LISTING_LIMIT_BYTES = 4 << 20

#: How much faster the non-pinned source has to be before it is worth giving up the commit.
#: Below this factor the difference is inside the noise of one megabyte, and the pin is
#: worth more than the margin (see the module docstring).
SPEED_MARGIN = 1.5

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

    `warning` is set when the chosen source cannot serve this *repository* at all -- measured,
    not feared: both hubs are asked to list it, and a 404 there is reported before the client
    turns it into an error halfway through a command.
    """

    name: str
    reason: str
    #: The base URL the fetch goes to, when the source has one worth naming. ModelScope's is
    #: decided inside its own SDK (it reads `MODELSCOPE_ENDPOINT`), so this stays empty rather
    #: than naming a host that the SDK may not use.
    endpoint: str = ""
    pinned: bool = True
    warning: str = ""

    @property
    def module(self) -> str:
        """The package this source needs installed."""
        return CLIENTS[self.name]

    def describe(self) -> str:
        where = f"{self.name} at {self.endpoint}" if self.endpoint else self.name
        pin = "the pinned revision" if self.pinned else "the mirror's current revision, not the pinned commit"
        return f"{where}: {self.reason}; fetches {pin}"


@dataclass(frozen=True)
class Survey:
    """What one hub says about one repository: whether it has it, and how fast it moves.

    The listing is not a formality: `mstrasser/Jeff-*` have no ModelScope mirror at all
    (`docs/design-review.md` §2-D32), so "the host answers" and "this repository can be
    fetched from it" are different facts, and the second one is the one that decides.
    """

    name: str
    available: bool
    #: Bytes per second read from the largest file in the repository. None means either "the
    #: listing answered but the sample did not transfer" (`detail` says why) or "speed was not
    #: asked for" (`detail` is empty).
    speed: float | None = None
    #: Why it is unavailable or unmeasurable, in the words of the answer that said so
    #: ("HTTP 404"). Empty means the question was not asked.
    detail: str = ""
    #: True only when the hub *said* the repository is not there (HTTP 404). A 401 on a gated
    #: repository, a 429 or a 5xx all leave this False: those are answers about the *request*,
    #: and substituting a mirror for them would drop the pin on a hub that has the checkpoint
    #: (this listing carries no credential, while the client may have one).
    missing: bool = False
    #: Whether an HTTP status came back at all. False means the request never completed
    #: (DNS, refused connection, timeout) or the body was not a listing (a captive portal,
    #: a proxy's HTML error page, a body cut off mid-flight): the endpoint cannot be *used*
    #: from here, which is the condition the ModelScope fallback exists for.
    answered: bool = True

    def describe(self) -> str:
        if not self.available:
            if self.missing:
                return f"{self.name} does not have this repository ({self.detail or 'no listing'})"
            return f"{self.name} could not be listed ({self.detail or 'no listing'})"
        if self.speed is None:
            return (
                f"{self.name} has it" if not self.detail else f"{self.name} has it but moved no sample ({self.detail})"
            )
        return f"{self.name} at {rate(self.speed)}"


#: One hub's survey, as `choose` receives it: `(name, spec, settings, sample=...) -> Survey`.
#: A parameter rather than three calls to `survey_hub` so tests -- and the fallback path, which
#: wants availability without a measurement -- can answer for both hubs without a network.
Surveyor = Callable[..., Survey]


def rate(bytes_per_second: float) -> str:
    """`MiB/s`, one decimal. The unit a user compares against a download size."""
    return f"{bytes_per_second / (1 << 20):.1f} MiB/s"


def speed_policy() -> str:
    """The measurement's cost and its tie-break, in one sentence, for `decis doctor`.

    Here rather than in the CLI because the numbers are the rule: a doctor line that spelled
    "1 MiB / 6 s / 1.5x" out again would be a second home for all three, and the first
    threshold change would leave the diagnostic lying (`AGENTS.md` §2).
    """
    return (
        f"{SPEED_SAMPLE_BYTES // (1 << 20)} MiB / {SPEED_SAMPLE_SECONDS:g} s of one file per hub, "
        f"{SPEED_MARGIN}x faster to leave {HUGGINGFACE}"
    )


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


def modelscope_endpoint(settings: Settings) -> str:
    """The host ModelScope's client would use, for the same reason `HF_ENDPOINT` is read.

    The SDK honors `MODELSCOPE_ENDPOINT` (and the older `MODELSCOPE_DOMAIN`); measuring a
    hardcoded host while the client talks to an internal mirror would decide the source from
    a number that describes somebody else's network.
    """
    configured = (settings.modelscope_endpoint or DEFAULT_MODELSCOPE_ENDPOINT).rstrip("/")
    if "://" not in configured:
        # The SDK accepts a bare host (`MODELSCOPE_DOMAIN=www.modelscope.cn`) and speaks https.
        configured = f"https://{configured}"
    return configured


def listing_url(name: str, spec: WeightSpec, settings: Settings) -> str:
    """Where one hub lists a repository's files, with sizes.

    Two shapes, one purpose: pick a file that the fetch would really pull, and find out
    whether the repository is there at all. The Hugging Face tree is asked at the *pinned*
    revision, so a listing that answers is also evidence the pin still resolves.
    """
    if spec.repo_id is None:
        raise ValueError(f"{spec.engine_id} has no repository to list")
    if name == MODELSCOPE:
        return (
            f"{modelscope_endpoint(settings)}/api/v1/models/{spec.repo_id}/repo/files"
            f"?Revision={MODELSCOPE_REVISION}&Recursive=True"
        )
    return f"{hugging_face_endpoint(settings)}/api/models/{spec.repo_id}/tree/{spec.revision or 'main'}?recursive=true"


def file_url(name: str, spec: WeightSpec, path: str, settings: Settings) -> str:
    """Where one file of `spec` is read from, on each hub.

    Handing a URL to `urllib` rather than a client call is deliberate: the measurement has to
    work in an image where neither client is installed (`decis doctor` runs in the API-only
    one), and `urllib` is already the transport of the reachability probe, so it honors the
    same proxy environment (`config.ensure_loopback_bypass`).
    """
    quoted = urllib.parse.quote(path)
    if name == MODELSCOPE:
        return (
            f"{modelscope_endpoint(settings)}/api/v1/models/{spec.repo_id}/repo"
            f"?Revision={MODELSCOPE_REVISION}&FilePath={quoted}"
        )
    return f"{hugging_face_endpoint(settings)}/{spec.repo_id}/resolve/{spec.revision or 'main'}/{quoted}"


def _listed_files(name: str, body: bytes) -> list[tuple[str, int]]:
    """`[(path, size)]` out of either hub's listing JSON.

    Kept apart from the request so the two shapes can be asserted against recorded bodies
    instead of against a live Hub (`tests/test_hub.py`).
    """
    document = json.loads(body)
    if name == MODELSCOPE:
        files = document.get("Data", {}).get("Files", [])
        return [(str(item["Path"]), int(item.get("Size") or 0)) for item in files if item.get("Type") == "blob"]
    entries = document if isinstance(document, list) else []
    return [(str(item["path"]), int(item.get("size") or 0)) for item in entries if item.get("type") == "file"]


def sample_path(spec: WeightSpec, files: list[tuple[str, int]]) -> str | None:
    """The file to time, out of a listing.

    The largest file that this fetch would actually pull: that is where a download's time
    goes, and asking for a `config.json` would time a round trip instead. Matching is against
    the same `allow_patterns` the client is handed, so the sample cannot come from a sibling
    checkpoint the fetch would skip -- and the patterns carry the subfolder, which is what
    keeps `laya-multilingual`'s sample out of `typed-decisions/`.
    """
    patterns = [str(pattern) for pattern in download_arguments(spec)["allow_patterns"]]  # type: ignore[union-attr]
    candidates = [entry for entry in files if any(fnmatch.fnmatch(entry[0], pattern) for pattern in patterns)]
    if not candidates:
        # Deliberately no fallback to `spec.marker`: the marker also exists in a sibling
        # checkpoint of the same repository (three Laya checkpoints share `rl_agent_config.json`),
        # so timing it would report the speed of a file this fetch would not pull.
        return None
    return max(candidates, key=lambda entry: entry[1])[0]


#: How much of a body one bounded read asks for. `read1` returns as soon as a chunk is
#: available, which is what lets the deadline below be checked between chunks instead of
#: after the whole thing has arrived.
_PULL_CHUNK_BYTES = 65536


def _wait_at_most(response: Any, seconds: float) -> None:
    """Let the next recv give up after `seconds`, when the transport allows it.

    The `timeout=` handed to the opener is a *per-recv* socket timeout, not a budget: a peer
    that dribbles a byte just under it keeps resetting the clock, so a wall-clock deadline
    alone would still be outlived by one blocking `read()`. Reaching for the socket is what
    makes the documented cap a cap (found by an adversarial review, `docs/design-review.md`
    §2-D37); a transport without one (a test double, a `file:` URL) is left alone.
    """
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        return
    with contextlib.suppress(OSError):  # a socket that cannot be retimed just fails the sample
        sock.settimeout(seconds)


def _pull(response: Any, *, limit: int, seconds: float) -> bytes:
    """Read at most `limit` bytes from `response`, for at most `seconds` of wall clock.

    Bounded in both dimensions on purpose: a size cap alone lets a trickling peer hold an
    interactive command open for as long as its body takes to arrive, and a deadline alone
    lets a fast peer stream a gigabyte into the decision.
    """
    started = time.monotonic()
    chunks: list[bytes] = []
    total = 0
    reader = getattr(response, "read1", None) or response.read
    while total < limit:
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            break
        _wait_at_most(response, left)
        try:
            chunk = reader(min(_PULL_CHUNK_BYTES, limit - total))
        except Exception:
            # A peer that ran out of budget mid-body is a *measurement* of a slow peer, not a
            # failed one: hand back what arrived. Failing before the first byte is different --
            # that is a dead or unreachable route, and the caller reports it as such.
            if not chunks:
                raise
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _read_listing(response: Any) -> bytes:
    """The listing body, capped in bytes and in wall-clock time.

    Whatever arrived inside the caps is returned as-is; an incomplete body fails to parse,
    which the caller reports as an unusable listing rather than as an unreachable Hub.
    """
    return _pull(response, limit=LISTING_LIMIT_BYTES, seconds=PROBE_TIMEOUT_S)


def read_bounded(
    url: str, *, open_url: Callable[..., Any] | None = None, budget: float = SPEED_SAMPLE_SECONDS
) -> float | None:
    """Bytes per second read from `url`, bounded by `SPEED_SAMPLE_BYTES`/`budget` seconds.

    `budget` is the wall clock this may spend, and it is the opener's socket timeout too, so
    one read cannot outlive it. `None` means "no usable measurement" -- a 404, a timeout, a
    redirect loop -- and every caller treats that as "this hub did not win", never as "this
    hub is slow". A Hub that refuses one file may still serve the checkpoint.
    """
    opener = open_url or urllib.request.urlopen
    started = time.monotonic()
    total = 0
    try:
        with opener(url, timeout=budget) as response:
            total = len(_pull(response, limit=SPEED_SAMPLE_BYTES, seconds=budget))
    except Exception as exc:
        _logger.debug("no speed sample from %s: %s", url, exc)
        return None
    elapsed = time.monotonic() - started
    if total == 0 or elapsed <= 0:
        return None
    return total / elapsed


def survey_hub(
    name: str,
    spec: WeightSpec,
    settings: Settings,
    *,
    sample: bool = True,
    open_url: Callable[..., Any] | None = None,
    measure: Callable[[str], float | None] | None = None,
) -> Survey:
    """Whether `name` can serve `spec`, and how fast it moves the biggest file it would pull.

    One request per question. The listing doubles as the "is this repository here at all"
    answer, which is the only thing a caller with no alternative needs (`sample=False`: the
    fallback path, where ModelScope is the answer whether it is fast or not, so reading a
    megabyte to print a number would be pure cost). With `sample=True` a second bounded read
    measures the file a download's time would actually go into. Every failure degrades to
    `available=False` or `speed=None` with the reason attached; no caller guesses a number.
    """
    opener = open_url or urllib.request.urlopen
    # Outside the `try`: a spec with no repository is a programming error, and a broad
    # `except` around it would report that as a fact about the Hub ("ValueError").
    address = listing_url(name, spec, settings)
    try:
        with opener(address, timeout=PROBE_TIMEOUT_S) as response:
            body = _read_listing(response)
    except urllib.error.HTTPError as exc:
        # 404 is the one status that answers the question ("this hub has no such
        # repository", §2-D32 for `mstrasser/Jeff-*`). Anything else -- 401/403 on a gated
        # repository, 429, 5xx -- means the question went unanswered, and `missing` says so.
        return Survey(name, available=False, missing=exc.code == 404, detail=f"HTTP {exc.code}")
    except Exception as exc:
        # No status, so no answer *about the repository*: the route itself did not work.
        return Survey(name, available=False, answered=False, detail=type(exc).__name__)

    try:
        files = _listed_files(name, body)
    except Exception as exc:
        # A 200 that is not a listing (a captive portal, a proxy's error page) is the same
        # kind of fact: nothing was learned about the repository, and nothing can be used.
        return Survey(name, available=False, answered=False, detail=f"{type(exc).__name__} while reading the listing")

    if not sample:
        return Survey(name, available=True)
    path = sample_path(spec, files)
    if path is None:
        return Survey(name, available=True, detail="no listed file matches this checkpoint")
    taken = measure or (lambda url: read_bounded(url, open_url=open_url))
    speed = taken(file_url(name, spec, path, settings))
    return Survey(name, available=True, speed=speed, detail="" if speed is not None else f"no bytes from {path}")


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


def choose(
    settings: Settings,
    *,
    spec: WeightSpec | None = None,
    probe: Callable[[str], bool] | None = None,
    survey: Surveyor | None = None,
) -> Selection:
    """Decide which hub to fetch from.

    A pinned `DECIS_HUB` never probes and never falls back: an operator who named a source
    gets that source and its failures, which is the whole point of naming one.

    `spec` is passed by callers that are about to move *this* checkpoint's bytes (`decis
    download`, `paths.fetch_checkpoint`). With a repository named, a Hub that merely answers
    is not automatically good enough: both are asked to list it and to move a bounded sample,
    and the faster one wins when it wins by `SPEED_MARGIN`. Without a spec -- `decis doctor`
    reporting which Hub *would* be used -- the reachability probe answers on its own, and the
    reason says so rather than implying a measurement that did not happen.
    """
    endpoint = hugging_face_endpoint(settings)
    if settings.hub == HUGGINGFACE:
        return Selection(HUGGINGFACE, "DECIS_HUB=huggingface", endpoint=endpoint)
    if settings.hub == MODELSCOPE:
        return Selection(MODELSCOPE, "DECIS_HUB=modelscope", pinned=False)

    checker = probe or probe_endpoint
    if not checker(probe_url(settings)):
        return _without_hugging_face(settings, spec, endpoint, survey)
    if spec is None:
        return Selection(HUGGINGFACE, f"{endpoint} answered", endpoint=endpoint)

    look = survey or survey_hub
    here = look(HUGGINGFACE, spec, settings)
    mirror = look(MODELSCOPE, spec, settings)
    return _faster(here, mirror, endpoint)


def _without_hugging_face(
    settings: Settings,
    spec: WeightSpec | None,
    endpoint: str,
    survey: Surveyor | None,
) -> Selection:
    """The original fallback: Hugging Face does not answer, so ModelScope it is.

    When a repository is named, the mirror is asked whether it has one -- because for
    `mstrasser/Jeff-*` it does not (§2-D32), and a user on a blocked network deserves to read
    "ModelScope has no such repository" *before* the client fails on it. The choice is not
    changed by that answer: there is nowhere else to go.
    """
    warning = ""
    reason = f"{endpoint} did not answer"
    if spec is not None:
        # Availability only: this branch has already decided on ModelScope, so timing it
        # would spend a megabyte to print a number nobody is choosing between.
        look = survey or survey_hub
        mirror = look(MODELSCOPE, spec, settings, sample=False)
        reason = f"{reason}; {mirror.describe()}; speed not measured (there is no alternative to compare)"
        if not mirror.available:
            because = (
                f"{MODELSCOPE} has no repository {spec.repo_id} ({mirror.detail})"
                if mirror.missing
                else f"{MODELSCOPE} could not be listed ({mirror.detail}), so whether it has {spec.repo_id} is unknown"
            )
            warning = (
                f"{because}. This fetch will fail unless Hugging Face becomes reachable, or a mirror is "
                f"configured (MODELSCOPE_ENDPOINT)."
            )
    _logger.warning(
        "%s did not answer; falling back to ModelScope. ModelScope mirrors have no commit "
        "revisions, so the checkpoint's pinned revision cannot be honored there "
        "(docs/design-review.md §2-D32). Set DECIS_HUB=huggingface to fail instead.",
        endpoint,
    )
    return Selection(MODELSCOPE, reason, pinned=False, warning=warning)


def _faster(here: Survey, mirror: Survey, endpoint: str) -> Selection:
    """Pick between two reachable hubs on what they measured, not on which one answered.

    Hugging Face keeps the tie and the near-tie: it is the only source that can serve the
    pinned commit, so a mirror has to be meaningfully faster to be worth different bytes.
    """
    if mirror.available and (here.missing or not here.answered):
        # One of the two facts that justify different bytes without a measurement: the pinned
        # hub says it has no such repository (404), or it could not be used at all (no status
        # came back, or what came back was not a listing). Neither is a speed comparison.
        return _mirror_wins(f"{here.describe()}; {mirror.describe()}", because=here.describe())
    if not here.available or not mirror.available:
        # Nothing to compare and nowhere to go: the pinned hub answered with a status other
        # than 404 (a 401 on a gated repository -- this listing carries no credential while
        # the client may have one -- a 429, a 5xx), or the mirror could not be used either.
        return Selection(HUGGINGFACE, f"{here.describe()}; {mirror.describe()}", endpoint=endpoint)
    if here.speed is not None and mirror.speed is not None and mirror.speed >= here.speed * SPEED_MARGIN:
        return _mirror_wins(
            f"{mirror.describe()} against {here.describe()} (>= {SPEED_MARGIN}x faster)",
            because=f"it was measured faster than {HUGGINGFACE}",
        )
    return Selection(HUGGINGFACE, f"{here.describe()} against {mirror.describe()}", endpoint=endpoint)


def _mirror_wins(reason: str, *, because: str) -> Selection:
    """Choose ModelScope, and log what that costs a deployment that needs the pin.

    `because` is the sentence the log completes ("Falling back to ModelScope because ...").
    It is passed in rather than derived from `reason`, because only the caller knows whether a
    number was measured: a substitution on a 404 or on an endpoint that could not be reached
    would otherwise be logged as "measured faster", inventing a measurement that never
    happened (`docs/design-review.md` §2-D37, review finding 3).

    Named apart from `_from_modelscope`, which is the *client* call: two functions with one
    name is a collision the second definition wins silently.
    """
    _logger.warning(
        "Falling back to ModelScope because %s. Its mirrors have no commit revisions, so the "
        "checkpoint's pinned revision cannot be honored there and the bytes may differ from the "
        "pinned ones (docs/design-review.md §2-D32). Set DECIS_HUB=huggingface to keep the pin.",
        because,
    )
    return Selection(MODELSCOPE, reason, pinned=False)


def client_installed(name: str) -> bool:
    """Whether `name`'s client package is importable here, without importing it.

    Delegates to `paths.module_available`: "is it installed" has one home, and this used to
    be a second `find_spec` without the `except` that a missing parent package needs.
    """
    return module_available(CLIENTS[name])


def cached(spec: WeightSpec, settings: Settings) -> tuple[str, Path] | None:
    """A client cache that already holds this checkpoint whole, and which client it is.

    Asked before any measurement or transfer, in both `decis download` and the load path,
    because otherwise a speed win on a cold route could re-fetch gigabytes that are already
    on disk in the *other* client's cache -- a worse outcome than the slowness this module
    exists to route around. A checkpoint in a cache is also strictly better than a fresh
    fetch: it is already validated by the same predicate the loader applies.

    Both clients can be told to look only at their cache (`local_files_only=True`), which is a
    local directory lookup, not a request. The pin-capable source is asked first, so when both
    caches hold the repository the pinned bytes are the ones used.
    """
    order = [HUGGINGFACE, MODELSCOPE]
    if settings.hub in (HUGGINGFACE, MODELSCOPE):
        order = [settings.hub]
    for name in order:
        if not client_installed(name):
            continue
        try:
            landed = download(spec, selection=Selection(name, "cache lookup"), local_only=True)
        except Exception:
            # A miss is the normal case, and each client spells it differently
            # (`LocalEntryNotFoundError`, `CacheNotFound`); both mean "not here".
            continue
        root = checkpoint_root(landed, spec)
        if root is not None:
            return name, root
    return None


def download(
    spec: WeightSpec,
    *,
    selection: Selection,
    destination: Path | None = None,
    local_only: bool = False,
) -> Path:
    """Fetch `spec` from `selection` and return the directory it landed in.

    `destination=None` means "the client's own cache", which is what the loaders use and what
    `decis download` writes to when no `DECIS_MODEL_DIR` is configured. `local_only=True`
    forbids the network: that is how `cached` asks a client what it already has.

    Validating the result is the caller's job (`paths.checkpoint_root`): a client returning
    successfully is not the same claim as "a loader can read a checkpoint here", and §2-D17
    is what that distinction cost the first time it was skipped.
    """
    arguments = download_arguments(spec)
    if selection.name == MODELSCOPE:
        return _from_modelscope(spec, arguments, destination, local_only)
    return _from_hugging_face(spec, arguments, destination, local_only)


def _from_hugging_face(
    spec: WeightSpec, arguments: dict[str, object], destination: Path | None, local_only: bool = False
) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise HubUnavailableError(
            f"`huggingface_hub` is not installed, so {spec.repo_id} cannot be fetched. It arrives with "
            f"any engine extra (`uv sync --extra laya`), or on its own with `uv sync --extra download`."
        ) from exc
    kwargs: dict[str, object] = dict(arguments)
    if destination is not None:
        kwargs["local_dir"] = str(destination)
    if local_only:
        kwargs["local_files_only"] = True
    return Path(snapshot_download(**kwargs))  # type: ignore[arg-type]


def _from_modelscope(
    spec: WeightSpec, arguments: dict[str, object], destination: Path | None, local_only: bool = False
) -> Path:
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
    if local_only:
        kwargs["local_files_only"] = True
    return Path(str(snapshot_download(**kwargs)))  # type: ignore[arg-type]


__all__ = [
    "CLIENTS",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODELSCOPE_ENDPOINT",
    "HUGGINGFACE",
    "LISTING_LIMIT_BYTES",
    "MODELSCOPE",
    "MODELSCOPE_REVISION",
    "PROBE_TIMEOUT_S",
    "SPEED_MARGIN",
    "SPEED_SAMPLE_BYTES",
    "SPEED_SAMPLE_SECONDS",
    "HubUnavailableError",
    "Selection",
    "Survey",
    "cached",
    "choose",
    "client_installed",
    "download",
    "file_url",
    "hugging_face_endpoint",
    "listing_url",
    "modelscope_endpoint",
    "probe_endpoint",
    "probe_url",
    "rate",
    "read_bounded",
    "sample_path",
    "speed_policy",
    "survey_hub",
]
