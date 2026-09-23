"""`docker-compose.yml` must describe the images CI actually publishes.

Compose is the documented way to run the published images locally, so it is exactly the
place where a tag no event publishes (`docs/design-review.md` §2-D15), a host port that
desyncs from the port inside the container, or a mount that hides the weights baked into
the image (§2-D21) would be found by a user instead of by us.

No docker daemon is involved. The set of tags, and which of them carry weights, are read out
of the workflow's real `plan` script (`test_docker_workflow`), not copied here: a second
copy of the tag scheme is the defect these tests exist to catch.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from test_docker_workflow import load_workflow, plan_script_of, run_plan

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
# The name is the mechanism: Compose loads `docker-compose.override.yml` on top of
# `docker-compose.yml` whenever it is present, which is what makes a checkout build from
# source while a copy of the base file alone pulls the published image.
OVERRIDE = ROOT / "docker-compose.override.yml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
PLAYGROUND_DOCKERFILE = ROOT / "playground" / "Dockerfile"
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the compose file; it is not a runtime dependency")

# `"${DECIS_HOST_PORT:-8000}:8000"` -- host side templated, container side fixed.
PORT_MAPPING = re.compile(r"^\$\{([A-Z_]+)(?::-(\d+))?\}:(\d+)$")


# --- fixtures and helpers -------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def override() -> dict:
    return yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def planned(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Everything the workflow builds on a master push and on a release tag."""
    script = plan_script_of(load_workflow())
    tags: set[str] = set()
    extras: dict[str, str] = {}
    bakes: dict[tuple[str, str], str] = {}
    #: The playground's moving tag, i.e. what a master push publishes. Read from the plan
    #: script rather than written here, for the same reason the engine tags are.
    playground_tag = ""
    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", "refs/tags/v1.2.0", "v1.2.0"),
    ):
        plan = run_plan(script, tmp_path_factory.mktemp(name), EVENT=event, REF=ref, REF_NAME=name)
        tags |= {build["image_tag"] for build in plan["builds"]}
        tags.add(plan["playground_tag"])
        if plan["tag"] == "latest":
            tags.add("latest")
            playground_tag = plan["playground_tag"]
        extras.update({build["engine"]: build["extra"] for build in plan["builds"]})
        bakes.update({(build["engine"], build["variant"]): build["bake"] for build in plan["builds"]})
    return {"tags": tags, "extras": extras, "bakes": bakes, "playground_tag": playground_tag}


def engines(compose: dict) -> dict[str, dict]:
    """Engine id -> its service, derived rather than listed here.

    A service is an engine service when the tag of its `kingfs/decis` image is a registered
    engine id. The playground shares that repository but its tag is not an engine, so it
    falls out of this mapping -- which is what keeps the engine invariants below (a profile
    is an engine, a command names `decis`, a port mirrors `DECIS_PORT`) about engines
    instead of being loosened for the one service that is not one. A *new* engine is picked
    up automatically; a new non-engine service would too, which is why
    `test_every_service_is_either_an_engine_or_the_playground` exists.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from decis.engines.registry import SPECS  # after the sys.path insert

    return {name: service for name, service in compose["services"].items() if tag_of(service["image"]) in SPECS}


def tag_of(image: str) -> str:
    repository, _, tag = image.partition(":")
    assert repository == "kingfs/decis", f"an unpublishable repository: {image}"
    return tag


def mount_target(service: dict, target: str) -> str | None:
    """The source of a mount whose target is `target`, if the service mounts one."""
    for entry in service.get("volumes", []):
        if isinstance(entry, str):
            source, _, mounted = entry.partition(":")
            if mounted.split(":", 1)[0] == target:
                return source
        elif isinstance(entry, dict) and str(entry.get("target")) == target:
            return str(entry.get("source"))
    return None


def dockerfile_env(variable: str) -> str:
    """Read `ENV VAR=value` out of the Dockerfile, ignoring commented-out lines."""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = re.match(rf"\s*{re.escape(variable)}=(\S+)", line)
        if match:
            return match.group(1).rstrip("\\")
    raise AssertionError(f"the Dockerfile sets no ENV {variable}")


def healthcheck_block(dockerfile: Path = DOCKERFILE) -> str:
    """A Dockerfile's HEALTHCHECK instruction."""
    return dockerfile.read_text(encoding="utf-8").split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]


def healthcheck_test(service: dict) -> str:
    return " ".join(str(part) for part in service["healthcheck"]["test"])


# --- the images are the published ones -----------------------------------------


def test_every_compose_image_is_a_tag_the_workflow_publishes(compose: dict, planned: dict) -> None:
    for name, service in compose["services"].items():
        assert tag_of(service["image"]) in planned["tags"], (
            f"{name} runs {service['image']}, which no event in the workflow creates "
            f"(it creates {sorted(planned['tags'])})"
        )


def test_every_service_is_either_an_engine_or_the_playground(compose: dict, planned: dict) -> None:
    """Every service is classified, so a new one cannot hide from the invariants below.

    `engines()` derives its answer from the registry and the playground is identified by
    the tag the plan script publishes, so this is a partition of the file: add a service
    that is neither and one of these two tests goes red before it can ship.
    """
    classified = set(engines(compose))
    classified |= {
        name for name, service in compose["services"].items() if tag_of(service["image"]) == planned["playground_tag"]
    }
    assert classified == set(compose["services"]), (
        f"unclassified services: {sorted(set(compose['services']) - classified)}"
    )
    assert len(engines(compose)) == len(planned["extras"]), "a workflow engine has no service, or the reverse"


def test_the_engine_image_is_the_one_that_carries_the_weights(compose: dict, planned: dict) -> None:
    """`docker run kingfs/decis:<engine>` must not need the network.

    The published tag that is just the engine's name has to be the variant the workflow
    bakes weights into. If the default is ever flipped back to the weightless one, every
    documented `docker run` starts downloading -- which is what the image exists to avoid.
    """
    for engine, service in engines(compose).items():
        tag = tag_of(service["image"])
        assert planned["bakes"][(engine, "baked")] == engine, (
            f"the workflow builds {engine}'s default image without weights: {planned['bakes'][(engine, 'baked')]!r}"
        )
        assert tag == engine, f"{engine} runs {service['image']}; the default tag must be the baked one"


def test_a_profile_is_an_engine_and_an_engine_is_a_profile(compose: dict) -> None:
    """One name for the image, the profile, `--engine` and the default engine.

    If these drift, `COMPOSE_PROFILES=kev-0.8b` starts an image that serves something
    else, and no test that checks them one at a time would notice.
    """
    for engine, server in engines(compose).items():
        assert set(server["profiles"]) == {engine}, server["profiles"]
        assert tag_of(server["image"]) == engine, server["image"]
        assert server["command"] == ["decis", "serve", "--engine", engine], server["command"]
        assert server["environment"]["DECIS_DEFAULT_ENGINE"] == engine, server["environment"]


def test_every_command_names_the_console_script(compose: dict) -> None:
    """`command:` replaces the image's `CMD`, it does not extend it (`design-review.md` §2-D18).

    The Dockerfile ends in `CMD ["decis", "serve"]` with no `ENTRYPOINT`, so
    `command: ["download", ...]` asked the kernel to exec a program called `download`, and
    the container died with `exec: "download": executable file not found in $PATH`. Every
    command must therefore name `decis` itself -- and the reason is read out of the
    Dockerfile rather than assumed, so adding an `ENTRYPOINT` later does not leave a test
    asserting something that is no longer true.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    if any(line.startswith("ENTRYPOINT") for line in dockerfile.splitlines()):
        pytest.skip("the image has an ENTRYPOINT, so `command:` is appended to it")

    # Engine services only: `command:` overrides the engine image's CMD, and this is the
    # file/CMD pair it has to agree with. The playground's command is its own image's CMD,
    # which `test_the_playground_runs_its_own_entry_point` reads from its own Dockerfile.
    for name, service in engines(compose).items():
        command = service["command"]
        assert command[0] == "decis", f"{name} would exec {command[0]!r}, which is not on PATH: {command}"


def test_every_service_is_behind_a_profile(compose: dict) -> None:
    """A service with no profile starts on every `docker compose up`, both engines at once.

    That is the resource problem the profiles exist to solve, so it must be impossible to
    reintroduce by adding a service without one.
    """
    for name, service in compose["services"].items():
        assert service.get("profiles"), f"{name} would start unconditionally"


def test_starting_one_engine_is_one_engine_container(compose: dict) -> None:
    """No prefetch container and no dependency chain: the weights are already in the image.

    A second service per engine was the first design (a one-shot `decis download` into a
    named volume). It is gone because the published image carries the weights, and with them
    gone there is nothing left to order: a `depends_on` or a `weights-*` service reappearing
    means something is downloading at start-up again. The playground is a second container,
    but not a second *engine* container: it mounts nothing, waits for nothing, and finds the
    engine by asking, so it cannot reintroduce an ordering or a volume.
    """
    for name, service in compose["services"].items():
        assert not name.startswith("weights-"), f"{name} is a prefetch service again"
        assert "depends_on" not in service, f"{name} waits for another service: {service['depends_on']}"
        assert "volumes" not in service, f"{name} mounts something: {service['volumes']}"


# --- the baked weights must stay visible ---------------------------------------


def test_nothing_is_mounted_over_the_weights_baked_into_the_image(compose: dict) -> None:
    """`design-review.md` §2-D21: a mount at `DECIS_MODEL_DIR` hides the baked weights.

    A named volume is seeded from the image once and then keeps its own copy; a bind mount
    replaces the directory outright. Either way the image stops answering offline and starts
    downloading, with no error to explain why. The path comes from the Dockerfile, so the
    test follows it if it ever moves.
    """
    baked = dockerfile_env("DECIS_MODEL_DIR")
    for name, service in compose["services"].items():
        mounted = mount_target(service, baked)
        assert mounted is None, f"{name} mounts {mounted} at {baked}, hiding the weights baked into the image"
    for name, service in engines(compose).items():
        assert service["environment"]["DECIS_MODEL_DIR"] == baked, (
            f"{name} points the loader at {service['environment']['DECIS_MODEL_DIR']}, not at the baked {baked}"
        )


# --- the port contract ---------------------------------------------------------


def test_the_published_port_cannot_desync_from_the_port_decris_listens_on(compose: dict) -> None:
    """`DECIS_PORT` comes from `.env` too, so a user who sets it would otherwise land on nothing.

    The container side is pinned to the service's `DECIS_PORT` and only the host side is
    templated; the defaults must also differ, because two engines on one host port means
    whichever starts second fails.
    """
    defaults: dict[str, str] = {}
    for engine, server in engines(compose).items():
        entries = server["ports"]
        assert len(entries) == 1, entries
        match = PORT_MAPPING.match(entries[0])
        assert match, entries[0]
        variable, default, container_port = match.groups()
        assert server["environment"]["DECIS_PORT"] == container_port, (
            f"{engine} listens on {server['environment']['DECIS_PORT']} but publishes {container_port}"
        )
        assert default, f"{engine} publishes {variable} with no default"
        defaults[engine] = default
    assert len(set(defaults.values())) == len(defaults), f"two engines share a host port: {defaults}"


def test_no_service_authenticates_with_a_default_token(compose: dict) -> None:
    """§3-19: an unauthenticated server on 0.0.0.0 must not be reachable by default.

    The playground counts twice over: it attaches the engine's token to every request it
    forwards, so its own port is model access without even a token prompt. It must refuse
    to start without `DECIS_API_KEY` for the same reason the engine must.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${DECIS_API_KEY:?" in text, "the missing-token failure must happen before an image is pulled"
    for name, service in compose["services"].items():
        assert service["environment"]["DECIS_API_KEY"].startswith("${DECIS_API_KEY:?"), name


# --- readiness in compose, liveness in the image -------------------------------


def test_compose_probes_readiness_while_the_image_probes_liveness(compose: dict) -> None:
    """`design-review.md` §2-D22: `--wait` used to return before the model could answer.

    The image's HEALTHCHECK has to stay on `/healthz`: an orchestrator that restarts a
    container for failing its liveness probe must not do so while the engine is loading.
    Compose is not that orchestrator -- nothing restarts a container because a probe failed
    -- so the compose-level probe can be the readiness one, which is what makes
    `docker compose up -d --wait` mean "the model is loaded".

    The playground is the opposite case: `/readyz` on it means "an engine was found", which
    is legitimately false for the minutes an engine spends loading, so *its* probe is its
    own `/healthz` in both places. Probing the playground's `/readyz` would report a
    healthy process unhealthy, and probing its `/healthz` for readiness would defeat
    `--wait`.
    """
    healthcheck = healthcheck_block()
    assert "/healthz" in healthcheck, healthcheck
    assert "/readyz" not in healthcheck, healthcheck

    for name, service in engines(compose).items():
        probe = healthcheck_test(service)
        assert "/readyz" in probe, f"{name}: `--wait` would return before the engine is loaded: {probe}"
        assert "/healthz" not in probe, f"{name} probes liveness and calls it readiness: {probe}"
        # A cold CPU start was measured at 78-122 s across runs; the start period has to outlast it,
        # and failures inside it do not count towards `retries`.
        start_period = service["healthcheck"]["start_period"]
        assert int(str(start_period).rstrip("s")) >= 180, start_period

    probe = healthcheck_test(compose["services"]["playground"])
    assert "/healthz" in probe, probe
    assert "/readyz" not in probe, f"the playground's liveness would fail while the engine loads: {probe}"


def test_the_probe_cannot_be_hijacked_by_a_proxy_in_the_environment(compose: dict) -> None:
    """`design-review.md` §2-D19: `urllib` reads `HTTP_PROXY`, so a probe must unset it.

    An image that fetched its weights through a proxy withholds nothing: a probe that
    inherits it asks the proxy for `http://127.0.0.1:8000/healthz`, gets a 502, and a server
    that logged `engine ready after 86.1s` is reported unhealthy -- in Kubernetes that means
    restarting a healthy pod forever. Every probe is built from that environment -- the two
    images' and the compose-level ones -- so all of them are checked here.
    """
    variables = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
    probes = {
        "the engine Dockerfile HEALTHCHECK": healthcheck_block(DOCKERFILE),
        "the playground Dockerfile HEALTHCHECK": healthcheck_block(PLAYGROUND_DOCKERFILE),
    }
    probes.update(
        {f"the {name} healthcheck": healthcheck_test(service) for name, service in compose["services"].items()}
    )

    for where, probe in probes.items():
        for variable in variables:
            assert f"-u {variable}" in probe, f"{where} would inherit {variable}: {probe}"


# --- the file `.env.example` ships ---------------------------------------------


def test_the_env_example_selects_one_engine_that_exists(compose: dict) -> None:
    """The user's stated goal: `docker compose up` starts one engine, not both."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^COMPOSE_PROFILES=(.+)$", text, re.MULTILINE)
    assert match, ".env.example no longer sets COMPOSE_PROFILES, so `docker compose up` starts nothing"

    profiles = [name.strip() for name in match.group(1).split(",")]
    known = {profile for name, service in engines(compose).items() for profile in service["profiles"]}
    assert set(profiles) <= known, f".env.example enables {profiles}, which is not a profile in {sorted(known)}"
    assert len(profiles) == 1, f"the default must start one engine, not {profiles}"
    # The bare `latest` image and the default profile must be the same engine, or the
    # documented `docker pull kingfs/decis` and `docker compose up` disagree.
    assert profiles == ["laya-multilingual"], profiles


def test_the_documented_profile_commands_name_real_profiles(compose: dict) -> None:
    known = {profile for service in compose["services"].values() for profile in service["profiles"]}
    for readme in READMES:
        for named in re.findall(r"--profile ([A-Za-z0-9._-]+)", readme.read_text(encoding="utf-8")):
            assert named in known, f"{readme.name} suggests --profile {named}, which is not one of {sorted(known)}"


# --- the playground ------------------------------------------------------------


def test_the_playground_is_in_every_engine_profile(compose: dict) -> None:
    """It must start with whichever engine the profile selects, not only the default one.

    Derived from the engine services rather than written as `[laya-multilingual, kev-0.8b]`:
    adding an engine whose profile omits the playground would otherwise ship a compose file
    where `--profile <new-engine>` silently has no games.
    """
    playground = compose["services"]["playground"]
    assert set(playground["profiles"]) == set(engines(compose)), playground["profiles"]


def test_the_playground_does_not_inherit_the_engine_env_file(compose: dict) -> None:
    """`env_file: [.env]` carries `DECIS_PORT`, `DECIS_MODEL_DIR` and the engine's proxy
    settings; the playground runs none of that, and inheriting it would make the two
    containers' configurations look interchangeable when they are not. It gets exactly the
    variables its own interface names, plus the token it must attach.
    """
    playground = compose["services"]["playground"]
    assert "env_file" not in playground, playground["env_file"]
    assert set(playground["environment"]) == {
        "DECIS_PLAYGROUND_HOST",
        "DECIS_PLAYGROUND_PORT",
        "DECIS_PLAYGROUND_UPSTREAM",
        "DECIS_PLAYGROUND_CANDIDATES",
        "DECIS_API_KEY",
    }, playground["environment"]


def test_the_playground_runs_its_own_entry_point(compose: dict) -> None:
    """The image's CMD is the program; the compose file must not replace it.

    Same trap as `design-review.md` §2-D18 from the other side: an engine service has to
    name `decis` because `command:` replaces the CMD, and the playground has to name
    *nothing* because its CMD already starts the server. Both are read from the Dockerfile
    that actually decides it.
    """
    playground = compose["services"]["playground"]
    assert "command" not in playground, f"the playground replaces its image CMD: {playground['command']}"

    dockerfile = PLAYGROUND_DOCKERFILE.read_text(encoding="utf-8")
    assert not any(line.startswith("ENTRYPOINT") for line in dockerfile.splitlines()), (
        "update this test: the entry point moved"
    )
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    # The last argument is the script the image runs; it has to exist in the build context,
    # and it is the same file `playground/server.py` that the server tests exercise.
    script = cmd.rstrip("]").rsplit(",", 1)[1].strip().strip('"')
    in_checkout = script.replace("/app/", "playground/", 1).lstrip("/")
    assert (ROOT / in_checkout).is_file(), f"{cmd} runs {script}, which is not {in_checkout} in this checkout"
    assert script.endswith("server.py"), cmd


def test_the_playground_publishes_the_tag_the_workflow_builds(compose: dict, planned: dict) -> None:
    """A documented port and a documented image, both from the file that produces them."""
    playground = compose["services"]["playground"]
    assert tag_of(playground["image"]) == planned["playground_tag"], playground["image"]
    assert playground["ports"] == ["${DECIS_PLAYGROUND_HOST_PORT:-8080}:8080"], playground["ports"]
    assert playground["environment"]["DECIS_PLAYGROUND_PORT"] == "8080", playground["environment"]


def test_the_playground_refuses_to_start_without_a_token(compose: dict) -> None:
    """It is a proxy that attaches the engine's key, so its port *is* model access."""
    playground = compose["services"]["playground"]
    assert playground["environment"]["DECIS_API_KEY"].startswith("${DECIS_API_KEY:?"), playground["environment"]


# --- the local-build override --------------------------------------------------


def test_only_the_override_builds_and_only_for_this_checkout(compose: dict, override: dict, planned: dict) -> None:
    """Deployment pulls, the checkout builds, and the two never share a tag.

    The base file is what a deployment copies, so it must not contain a `build:` -- a
    deployment that builds is a deployment that needs the source. The override has to cover
    every service: leaving one out would silently mix a locally built image with a pulled one,
    which stays invisible until the two disagree. Its tags are `decis-local:*` so a build
    cannot repoint `kingfs/decis:<engine>` at whatever is on this machine.

    Each service builds the Dockerfile that actually produces it: the engines share
    `docker/Dockerfile` with different build args, and the playground has its own because it
    is not an engine.
    """
    for name, service in compose["services"].items():
        assert "build" not in service, f"{name} builds in the deployment file"

    assert set(override["services"]) == set(compose["services"]), "the override misses a service"
    for name, service in override["services"].items():
        repository, _, tag = service["image"].partition(":")
        assert repository == "decis-local", service["image"]
        assert tag == name, service["image"]
        build = service["build"]
        assert build["context"] == ".", build
        if name in engines(compose):
            assert build["dockerfile"] == "docker/Dockerfile", build
            assert build["args"]["DECIS_ENGINE"] == name, build
            assert build["args"]["DECIS_EXTRAS"] == planned["extras"][name], build
            # Same weights as the published image, so "it works locally" means the same thing.
            assert build["args"]["DECIS_PREDOWNLOAD"] == name, build
        else:
            assert build["dockerfile"] == "playground/Dockerfile", build
            assert "args" not in build, f"the playground Dockerfile declares no ARG: {build['args']}"
