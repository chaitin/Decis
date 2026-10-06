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
  client;
* a checkpoint that is already whole in either cache is used from there, before any
  measurement (`cached`);
* and the measurement itself (`docs/design-review.md` §2-D37): the largest file the fetch
  would pull is the one timed, the mirror has to win by `SPEED_MARGIN` to be worth different
  bytes, a hub that does not have the repository is not a candidate at all, and the
  pinned-source path never asks either question.
"""

from __future__ import annotations

import json
import sys
import time
import types

import pytest

from decis.config import Settings
from decis.hub import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODELSCOPE_ENDPOINT,
    LISTING_LIMIT_BYTES,
    MODELSCOPE_REVISION,
    PROBE_TIMEOUT_S,
    SPEED_MARGIN,
    SPEED_SAMPLE_BYTES,
    HubUnavailableError,
    Survey,
    cached,
    choose,
    client_installed,
    download,
    file_url,
    hugging_face_endpoint,
    listing_url,
    modelscope_endpoint,
    probe_endpoint,
    probe_url,
    rate,
    read_bounded,
    sample_path,
    survey_hub,
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
    """The two things `urllib`'s response is used for: read some bytes, be a context manager."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.at = 0

    def read(self, size: int = -1) -> bytes:
        """Consumes its body: a real response does not replay what it already handed over."""
        if size is None or size < 0:
            size = len(self.body) - self.at
        chunk = self.body[self.at : self.at + size]
        self.at += len(chunk)
        return chunk

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


# --- measuring both hubs (§2-D37) -----------------------------------------------
#
# The measurement exists because a host that answers is not a host that is usable, so the
# tests below are all about the two ways that sentence can be wrong: the number has to come
# from a file this fetch would really pull, and a mirror that "wins" has to win by enough to
# be worth different bytes. Nothing here opens a socket: the listing body and the sample are
# handed to `survey_hub`, and `choose` gets a `survey` that returns what those answers imply.


def listing_body(name: str, files: list[tuple[str, int]]) -> bytes:
    """A listing in the shape `name` really returns (recorded from a live request)."""
    if name == "modelscope":
        return json.dumps(
            {"Code": 200, "Data": {"Files": [{"Type": "blob", "Path": path, "Size": size} for path, size in files]}}
        ).encode()
    return json.dumps([{"type": "file", "path": path, "size": size} for path, size in files]).encode()


FILES = [
    ("multilingual/rl_agent_config.json", 1_200),
    ("multilingual/model.safetensors", 700_000_000),
    ("typed-decisions/model.safetensors", 900_000_000),
    (".gitattributes", 400),
]


def test_the_measurement_reads_a_listing_and_times_the_biggest_file_this_fetch_would_pull(monkeypatch) -> None:
    """The sibling checkpoint in the same repository is 900 MB and must not be the sample.

    `convaiinnovations/laya` holds three checkpoints under one repo id, so "largest file in
    the repository" would time `typed-decisions/` while downloading `multilingual/`. The
    patterns the client is handed are the only thing that keeps them apart, so they are what
    selects the sample.
    """
    listed: list[str] = []
    timed: list[str] = []

    def open_url(url: str, timeout: float) -> _Response:
        listed.append(url)
        return _Response(listing_body("huggingface", FILES))

    surveyed = survey_hub(
        "huggingface", SPEC, settings_with(), open_url=open_url, measure=lambda url: timed.append(url) or 2.5
    )

    assert surveyed.available
    assert surveyed.speed == 2.5
    assert listed == [listing_url("huggingface", SPEC, settings_with())]
    assert timed[-1] == file_url("huggingface", SPEC, "multilingual/model.safetensors", settings_with())
    assert "typed-decisions" not in timed[-1], "the sample must be a file the fetch would pull"


def test_both_hubs_list_the_same_repository_and_the_mirror_never_sees_the_commit() -> None:
    """The mirror is addressed by branch, so a URL carrying the sha could never be right.

    Same rule as `_from_modelscope` leaving `revision` out (§2-D32): ModelScope's `Revision`
    is a branch or tag, and the sha in this spec does not exist there.
    """
    settings = settings_with()
    ours = listing_url("huggingface", SPEC, settings)
    theirs = listing_url("modelscope", SPEC, settings)

    assert ours == f"{DEFAULT_ENDPOINT}/api/models/convaiinnovations/laya/tree/{SPEC.revision}?recursive=true"
    assert theirs.startswith(f"{DEFAULT_MODELSCOPE_ENDPOINT}/api/v1/models/convaiinnovations/laya/repo/files?")
    assert f"Revision={MODELSCOPE_REVISION}" in theirs
    assert SPEC.revision not in theirs
    assert SPEC.revision not in file_url("modelscope", SPEC, "multilingual/model.safetensors", settings)
    assert SPEC.revision in file_url("huggingface", SPEC, "multilingual/model.safetensors", settings)


def test_the_modelscope_endpoint_is_read_and_normalized() -> None:
    """The measurement has to describe the network the client will use, not a hardcoded host."""
    assert modelscope_endpoint(settings_with()) == DEFAULT_MODELSCOPE_ENDPOINT
    assert modelscope_endpoint(settings_with(modelscope_endpoint="mirror.example/")) == "https://mirror.example"
    assert modelscope_endpoint(settings_with(modelscope_endpoint="http://inner:8080")) == "http://inner:8080"
    assert file_url("modelscope", SPEC, "a/b.bin", settings_with(modelscope_endpoint="inner")).startswith(
        "https://inner/api/v1/"
    )


def open_with(body: bytes | Exception, seen: list[str] | None = None):
    def open_url(url: str, timeout: float) -> _Response:
        if seen is not None:
            seen.append(url)
        if isinstance(body, Exception):
            raise body
        return _Response(body)

    return open_url


def test_a_hub_without_the_repository_is_not_available_and_says_why() -> None:
    import urllib.error

    gone = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    surveyed = survey_hub("modelscope", SPEC, settings_with(), open_url=open_with(gone))

    assert not surveyed.available
    assert surveyed.detail == "HTTP 404"
    assert surveyed.missing, "a 404 is the one answer that is about the repository"
    assert "does not have this repository" in surveyed.describe()


def test_a_body_that_is_not_a_listing_is_not_a_fact_about_the_repository() -> None:
    """A captive portal or a proxy's HTML error page is a 200 with nothing to learn from.

    Reading it as "Hugging Face does not have this repository" would move the fetch to a
    source that cannot honor the pin, on evidence about the *network*, not the repository.
    """
    surveyed = survey_hub("huggingface", SPEC, settings_with(), open_url=open_with(b"<html>captcha</html>"))

    assert not surveyed.available and not surveyed.missing and not surveyed.answered
    assert "could not be listed" in surveyed.describe()
    assert "does not have this repository" not in surveyed.describe()
    assert "JSONDecodeError" in surveyed.detail, "the reason has to name what happened"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_a_listing_that_could_not_be_made_says_so_instead_of_naming_the_repository(status: int) -> None:
    """A gated repository, a rate limit or a broken hub is not "Hugging Face has no such repo".

    The listing carries no credential while the *client* may have one, so reading its 401 as
    "not there" would hand the fetch to ModelScope and drop the pin on a hub that has the
    checkpoint (adversarial review, `docs/design-review.md` §2-D37).
    """
    import urllib.error

    refused = urllib.error.HTTPError("http://x", status, "no", {}, None)  # type: ignore[arg-type]
    surveyed = survey_hub("huggingface", SPEC, settings_with(), open_url=open_with(refused))

    assert not surveyed.available and not surveyed.missing
    assert surveyed.detail == f"HTTP {status}"
    assert "could not be listed" in surveyed.describe()
    assert "does not have this repository" not in surveyed.describe()


def test_a_listing_that_answers_but_transfers_nothing_has_no_speed() -> None:
    """`None` is not zero: the fallback still wins on a hub whose sample did not arrive."""
    surveyed = survey_hub(
        "modelscope",
        SPEC,
        settings_with(),
        open_url=open_with(listing_body("modelscope", FILES)),
        measure=lambda url: None,
    )

    assert surveyed.available
    assert surveyed.speed is None
    assert "moved no sample" in surveyed.describe()


def test_availability_alone_costs_one_request_and_no_bytes() -> None:
    """The fallback path has no alternative, so timing the mirror there is pure cost."""
    seen: list[str] = []
    surveyed = survey_hub(
        "modelscope", SPEC, settings_with(), sample=False, open_url=open_with(listing_body("modelscope", FILES), seen)
    )

    assert seen == [listing_url("modelscope", SPEC, settings_with())], "the sample must not be fetched"
    assert surveyed.available and surveyed.speed is None
    assert surveyed.describe().endswith("has it")


def test_a_repository_that_is_not_listed_at_all_is_unavailable() -> None:
    surveyed = survey_hub("modelscope", SPEC, settings_with(), open_url=open_with(listing_body("modelscope", [])))
    assert surveyed.available, "an empty repository is still a repository"
    assert surveyed.speed is None
    assert "no listed file matches" in surveyed.detail


def test_nothing_matching_this_checkpoint_means_no_sample_rather_than_a_sibling() -> None:
    """The marker exists in all three Laya checkpoints, so it cannot stand in for a match."""
    siblings = [("typed-decisions/rl_agent_config.json", 1_200), ("typed-decisions/model.safetensors", 900_000_000)]
    assert sample_path(SPEC, siblings) is None
    assert survey_hub(
        "huggingface", SPEC, settings_with(), open_url=open_with(listing_body("huggingface", siblings))
    ).detail.startswith("no listed file matches")


def test_read_bounded_stops_at_the_byte_cap() -> None:
    """A speed test that downloaded the whole checkpoint would be the bug it is measuring."""
    asked: list[int] = []

    class Unbounded(_Response):
        def read(self, size: int = -1) -> bytes:
            asked.append(size)
            return b"y" * size

    speed = read_bounded("http://x/big", open_url=lambda url, timeout: Unbounded())
    assert speed is not None and speed > 0
    assert sum(asked) == SPEED_SAMPLE_BYTES, "the reader must stop at the cap, not at the file's end"


def test_a_listing_is_read_within_its_own_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Hub that trickles a huge listing must not stretch the cost the docs state.

    Unbounded `response.read()` waits for the whole body, so a slow-but-alive Hub could hold
    `decis download` for minutes before the speed rule even starts. Two caps instead: bytes,
    and wall-clock time against `PROBE_TIMEOUT_S`.
    """
    from decis import hub as hub_module

    class Endless(_Response):
        def read(self, size: int = -1) -> bytes:
            return b"z" * size

    assert len(hub_module._read_listing(Endless())) == LISTING_LIMIT_BYTES, "the byte cap must bind"

    class Clock:
        """Answers `monotonic()` with the ticks it was given, then stays put."""

        def __init__(self, *ticks: float) -> None:
            self.ticks = iter(ticks)

        def monotonic(self) -> float:
            return next(self.ticks, PROBE_TIMEOUT_S)

    # `_pull` reads the clock once at the start and once per iteration, so the second tick
    # has to be the one that lands on the deadline.
    monkeypatch.setattr(hub_module, "time", Clock(0.0, 0.0, PROBE_TIMEOUT_S))
    assert len(hub_module._read_listing(Endless())) == 65536, "the clock must bind before the byte cap"


def test_a_trickling_peer_cannot_outlive_the_time_budget() -> None:
    """`timeout=` is per-recv, so a peer that dribbles resets it on every chunk.

    A byte cap alone would let a throttled mirror hold `decis download` open for as long as its
    body takes to arrive -- the network this measurement exists to route around. The budget has
    to be a deadline, and only a real socket can tell the two apart (found by adversarial
    review, `docs/design-review.md` §2-D37).
    """
    import socket
    import threading
    import urllib.request

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(5.0)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = server.accept()
        except OSError:  # pragma: no cover - the test finished first
            return
        with connection:
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            for _ in range(40):  # ~2 s of dribble; the body would need ~500 s
                try:
                    connection.sendall(b"x" * 64)
                except OSError:
                    return
                time.sleep(0.05)

    threading.Thread(target=serve, daemon=True).start()
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    speed = read_bounded(
        f"http://127.0.0.1:{port}/f",
        open_url=lambda url, timeout: direct.open(url, timeout=timeout),
        budget=0.3,
    )
    elapsed = time.monotonic() - started
    server.close()

    assert speed is not None and speed > 0
    assert elapsed < 1.5, f"the budget is a deadline, not a per-recv timeout (took {elapsed:.1f}s)"


def test_read_bounded_answers_none_instead_of_raising() -> None:
    def explode(url: str, timeout: float) -> _Response:
        raise TimeoutError("no answer")

    assert read_bounded("http://x/big", open_url=explode) is None


def test_rate_is_mib_per_second_so_a_user_can_compare_it_with_a_download_size() -> None:
    assert rate(1 << 20) == "1.0 MiB/s"
    assert rate(1.5 * (1 << 20)) == "1.5 MiB/s"


MIB = 1 << 20


def surveyed(
    name: str,
    *,
    speed: float | None = None,
    available: bool = True,
    detail: str = "",
    answered: bool = True,
) -> Survey:
    """A survey as `survey_hub` would build it, `missing`/`answered` included.

    Only a 404 is "not there", and only a status that never came back is "not answered":
    the two facts that let a substitution happen without any measurement.
    """
    return Survey(
        name,
        available=available,
        speed=speed,
        detail=detail,
        missing=detail.startswith("HTTP 404"),
        answered=answered,
    )


def two_hubs(ours: float, theirs: float):
    """A `survey` answering for both hubs at the given byte rates."""
    speeds = {"huggingface": ours, "modelscope": theirs}
    return lambda name, spec, settings, **kwargs: surveyed(name, speed=speeds[name])


def test_the_faster_mirror_has_to_win_by_the_margin_to_be_worth_different_bytes() -> None:
    """1.4x is noise from one megabyte; the pin is worth more than that."""
    just_under = choose(settings_with(), spec=SPEC, probe=lambda url: True, survey=two_hubs(MIB, 1.4 * MIB))
    assert just_under.name == "huggingface"
    assert just_under.pinned, "a near-tie keeps the commit"

    clearly = choose(settings_with(), spec=SPEC, probe=lambda url: True, survey=two_hubs(MIB, 1.5 * MIB))
    assert clearly.name == "modelscope"
    assert not clearly.pinned, "the mirror cannot address the commit; that must stay visible"
    assert "1.0 MiB/s" in clearly.reason and "1.5 MiB/s" in clearly.reason, "both numbers are the reason"
    assert f"{SPEED_MARGIN}x" in clearly.reason


def test_the_speed_switch_warns_about_the_pin_it_gives_up(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="decis.hub"):
        choose(settings_with(), spec=SPEC, probe=lambda url: True, survey=two_hubs(MIB, 4 * MIB))
    assert any("pinned revision cannot be honored" in record.getMessage() for record in caplog.records)


def test_a_mirror_without_the_repository_is_not_a_candidate() -> None:
    """`mstrasser/Jeff-Qwen3.5-0.8B` has no ModelScope mirror, so it stays on Hugging Face."""
    chosen = choose(
        settings_with(),
        spec=SPEC,
        probe=lambda url: True,
        survey=lambda name, spec, settings, **kwargs: surveyed(
            name, speed=MIB if name == "huggingface" else 99 * MIB, available=name == "huggingface", detail="HTTP 404"
        ),
    )
    assert chosen.name == "huggingface"
    assert "does not have this repository" in chosen.reason


def test_a_hub_that_could_not_be_listed_does_not_lose_to_the_mirror() -> None:
    """No answer is not a "no": the pin-capable source keeps the fetch when the listing failed.

    A 401 on a gated repository (this listing has no token), a 429, a 5xx or a truncated body
    must not read as "Hugging Face does not have it" -- that substitution would fetch
    different bytes without anyone measuring anything.
    """
    chosen = choose(
        settings_with(),
        spec=SPEC,
        probe=lambda url: True,
        survey=lambda name, spec, settings, **kwargs: surveyed(
            name, speed=MIB if name == "huggingface" else 99 * MIB, available=name == "huggingface", detail="HTTP 401"
        ),
    )
    assert chosen.name == "huggingface"
    assert chosen.pinned
    assert "could not be listed" in chosen.reason


def test_a_hub_that_did_not_answer_at_all_hands_the_fetch_to_the_mirror(caplog: pytest.LogCaptureFixture) -> None:
    """No status is the *other* fact that justifies switching without a measurement.

    The probe answered (so the endpoint speaks HTTP from here) and then the listing never
    completed: DNS, a refused connection, a reset, a timeout. That is the blocked or throttled
    route this fallback exists for, so the mirror takes it -- and the log must say *why*, not
    invent a speed comparison that never ran.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="decis.hub"):
        chosen = choose(
            settings_with(),
            spec=SPEC,
            probe=lambda url: True,
            survey=lambda name, spec, settings, **kwargs: surveyed(
                name,
                available=name == "modelscope",
                answered=name == "modelscope",
                detail="" if name == "modelscope" else "URLError",
            ),
        )

    assert chosen.name == "modelscope"
    assert not chosen.pinned, "the mirror cannot address the commit; that must stay visible"
    assert "could not be listed" in chosen.reason
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "could not be listed" in logged and "measured faster" not in logged


def test_a_mirror_that_has_it_while_hugging_face_does_not_list_it_is_chosen() -> None:
    """The reverse case: a 404 listing is evidence about the *repository*, not about speed."""
    chosen = choose(
        settings_with(),
        spec=SPEC,
        probe=lambda url: True,
        survey=lambda name, spec, settings, **kwargs: surveyed(name, available=name == "modelscope", detail="HTTP 404"),
    )
    assert chosen.name == "modelscope"
    assert not chosen.pinned


def test_two_unsampled_hubs_keep_hugging_face() -> None:
    """No number is not a number: without a measurement the pin decides."""
    chosen = choose(
        settings_with(),
        spec=SPEC,
        probe=lambda url: True,
        survey=lambda name, spec, settings, **kwargs: surveyed(name),
    )
    assert chosen.name == "huggingface"
    assert chosen.pinned


def test_choosing_without_a_checkpoint_does_not_measure_anything() -> None:
    """`decis doctor` names no repository, so it must not imply a measurement it did not make."""
    chosen = choose(settings_with(), probe=lambda url: True, survey=never_surveyed)
    assert chosen.name == "huggingface"
    assert "answered" in chosen.reason
    assert "MiB/s" not in chosen.reason


def never_surveyed(name: str, spec: WeightSpec, settings: Settings, **kwargs: object) -> Survey:
    pytest.fail(f"a spec-less choice surveyed {name} instead of answering from the probe")


def test_the_fallback_asks_the_mirror_whether_it_has_the_repository() -> None:
    """Before failing on a missing mirror, say so -- and do not spend a megabyte timing it."""
    seen: list[tuple[str, bool]] = []

    def survey(name: str, spec: WeightSpec, settings: Settings, **kwargs: object) -> Survey:
        seen.append((name, bool(kwargs.get("sample", True))))
        return surveyed(name, available=False, detail="HTTP 404")

    chosen = choose(settings_with(), spec=SPEC, probe=unreachable, survey=survey)

    assert seen == [("modelscope", False)], "availability only: this branch has no alternative to compare"
    assert chosen.name == "modelscope"
    assert SPEC.repo_id in chosen.warning and "HTTP 404" in chosen.warning
    assert chosen.reason.count("did not answer") == 1


def test_the_fallback_says_nothing_extra_when_the_mirror_has_the_repository() -> None:
    chosen = choose(
        settings_with(), spec=SPEC, probe=unreachable, survey=lambda name, spec, settings, **kwargs: surveyed(name)
    )
    assert chosen.warning == ""
    assert "has it" in chosen.reason


# --- a checkpoint that is already here ------------------------------------------


@pytest.fixture
def clients_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake module has no `__spec__`, so "is the client installed" has to be answered directly.

    The alternative -- teaching `fake_module` to fabricate an import spec -- would test
    `importlib` instead of the lookup.
    """
    monkeypatch.setattr("decis.hub.client_installed", lambda name: True)


def cache_client(monkeypatch: pytest.MonkeyPatch, landing, *, marker: str | None = "rl_agent_config.json") -> dict:
    """A client whose cache answers `local_files_only=True` and nothing else."""
    seen: dict = {}

    def snapshot_download(**kwargs: object) -> str:
        seen.update(kwargs)
        if not kwargs.get("local_files_only"):
            pytest.fail("the cache lookup must never be allowed to reach the network")
        if marker is not None:
            root = landing / SPEC.subfolder if SPEC.subfolder else landing
            root.mkdir(parents=True, exist_ok=True)
            (root / marker).write_bytes(b"marker")
        return str(landing)

    fake_module(monkeypatch, "huggingface_hub", snapshot_download)
    return seen


def test_a_checkpoint_already_in_a_cache_is_used_without_measuring(
    tmp_path, monkeypatch, clients_installed: None
) -> None:
    """The whole point: never re-fetch gigabytes that are already on disk somewhere."""
    seen = cache_client(monkeypatch, tmp_path / "hf")
    hit = cached(SPEC, settings_with())

    assert hit is not None
    name, root = hit
    assert name == "huggingface", "the pin-capable source is asked first, so its bytes win a tie"
    assert root == tmp_path / "hf" / "multilingual"
    assert checkpoint_root(tmp_path / "hf", SPEC) == root
    assert seen["repo_id"] == "convaiinnovations/laya"
    assert seen["local_files_only"] is True


def test_a_cache_that_holds_only_part_of_the_checkpoint_is_not_a_hit(
    tmp_path, monkeypatch, clients_installed: None
) -> None:
    """§2-D29: a half-arrived checkpoint must not be reported as ready, or as a source."""
    cache_client(monkeypatch, tmp_path / "hf", marker=None)
    # Both caches have to be fakes: `cached` asks the next client when the first one misses,
    # and the *developer's* ModelScope cache holding this repository would answer the question
    # this test is asking (the conftest network guard cannot see a client's cache lookup).
    fake_module(monkeypatch, "modelscope", lambda **kwargs: pytest.fail("a miss asked the other cache"))
    assert cached(SPEC, settings_with()) is None


def test_a_cache_lookup_follows_a_pinned_source(tmp_path, monkeypatch, clients_installed: None) -> None:
    """`DECIS_HUB=modelscope` must not be answered out of the Hugging Face cache."""
    fake_module(monkeypatch, "huggingface_hub", lambda **kwargs: pytest.fail("a pinned source asked the other cache"))
    landing = tmp_path / "ms"

    def snapshot_download(**kwargs: object) -> str:
        root = landing / SPEC.subfolder
        root.mkdir(parents=True, exist_ok=True)
        (root / str(SPEC.marker)).write_bytes(b"marker")
        return str(landing)

    fake_module(monkeypatch, "modelscope", snapshot_download)
    assert cached(SPEC, settings_with(hub="modelscope")) == ("modelscope", landing / SPEC.subfolder)


def test_a_cache_lookup_that_fails_is_a_miss_not_an_error(tmp_path, monkeypatch, clients_installed: None) -> None:
    """Every client spells "not here" differently; all of them mean "not here"."""

    def snapshot_download(**kwargs: object) -> str:
        raise RuntimeError("[E1022] Cannot find cached snapshot")

    fake_module(monkeypatch, "modelscope", snapshot_download)
    fake_module(monkeypatch, "huggingface_hub", snapshot_download)
    assert cached(SPEC, settings_with()) is None
