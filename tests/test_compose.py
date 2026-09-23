"""`docker-compose.yml` must describe the images CI actually publishes.

Compose is the documented way to run the published images locally, so it is exactly the
place where a tag no event publishes (`docs/design-review.md` §2-D15), a host port that
desyncs from the port inside the container, or a pretch that writes outside the volume
would be found by a user instead of by us.

No docker daemon is involved. The set of tags is read out of the workflow's real `plan`
script (`test_docker_workflow`), not copied here: a second copy of the tag scheme is the
defect these tests exist to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_docker_workflow import load_workflow, plan_script_of, run_plan

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
BUILD_OVERRIDE = ROOT / "docker-compose.build.yml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the compose file; it is not a runtime dependency")

# `"${DECIS_HOST_PORT:-8000}:8000"` -- host side templated, container side fixed.
PORT_MAPPING = re.compile(r"^\$\{([A-Z_]+)(?::-(\d+))?\}:(\d+)$")


# --- fixtures and helpers -------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def planned(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Everything the workflow publishes on a release, plus the master-push tag names."""
    script = plan_script_of(load_workflow())
    tags: set[str] = set()
    extras: dict[str, str] = {}
    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", "refs/tags/v1.2.0", "v1.2.0"),
    ):
        planned = run_plan(script, tmp_path_factory.mktemp(name), EVENT=event, REF=ref, REF_NAME=name)
        tags |= {build["image_tag"] for build in planned["builds"]}
        if planned["tag"] == "latest":
            tags.add("latest")
        extras.update({build["engine"]: build["extra"] for build in planned["builds"]})
    return {"tags": tags, "extras": extras}


def engines(compose: dict) -> dict[str, tuple[dict, dict]]:
    """Engine id -> (server, weights) service, derived from the file rather than listed."""
    services = compose["services"]
    return {
        name: (service, services[f"weights-{name}"])
        for name, service in services.items()
        if not name.startswith("weights-")
    }


def tag_of(image: str) -> str:
    repository, _, tag = image.partition(":")
    assert repository == "kingfs/decis", f"an unpublishable repository: {image}"
    return tag


def mount_target(service: dict, source: str) -> str | None:
    for entry in service.get("volumes", []):
        if isinstance(entry, str) and entry.startswith(f"{source}:"):
            return entry.split(":", 1)[1]
        if isinstance(entry, dict) and entry.get("source") == source:
            return str(entry["target"])
    return None


def healthcheck_block() -> str:
    """The Dockerfile's HEALTHCHECK instruction, which every service inherits."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    return dockerfile.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]


# --- the images are the published ones -----------------------------------------


def test_every_compose_image_is_a_tag_the_workflow_publishes(compose: dict, planned: dict) -> None:
    for name, service in compose["services"].items():
        assert tag_of(service["image"]) in planned["tags"], (
            f"{name} runs {service['image']}, which no event in the workflow creates "
            f"(it creates {sorted(planned['tags'])})"
        )


def test_a_profile_is_an_engine_and_an_engine_is_a_profile(compose: dict, planned: dict) -> None:
    """One name for the image, the profile, `--engine` and the default engine.

    If these drift, `COMPOSE_PROFILES=kev-0.8b` starts an image that serves something
    else, and no test that checks them one at a time would notice.
    """
    for engine, (server, _) in engines(compose).items():
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

    for name, service in compose["services"].items():
        command = service["command"]
        assert command[0] == "decis", f"{name} would exec {command[0]!r}, which is not on PATH: {command}"


def test_every_service_is_behind_a_profile(compose: dict) -> None:
    """A service with no profile starts on every `docker compose up`, both engines at once.

    That is the resource problem the profiles exist to solve, so it must be impossible to
    reintroduce by adding a service without one.
    """
    for name, service in compose["services"].items():
        assert service.get("profiles"), f"{name} would start unconditionally"


# --- weights land in the volume the server reads -------------------------------


def test_weights_are_fetched_once_into_the_volume_the_server_reads(compose: dict) -> None:
    """The one-shot prefetch (`design-review.md` §2-D17 is why it works at all).

    `decis download` writes `<DECIS_MODEL_DIR>/<engine id>/`, which is where
    `paths.resolve` looks; both services must agree on that directory and on the volume,
    or the server re-downloads what the prefetch just fetched.
    """
    for engine, (server, weights) in engines(compose).items():
        assert weights["command"] == ["decis", "download", "--engine", engine], weights["command"]
        assert weights["image"] == server["image"], weights
        assert set(weights["profiles"]) == {engine}, weights["profiles"]
        assert weights["restart"] == "no", "a one-shot that restarts would loop forever"
        assert server["depends_on"][f"weights-{engine}"]["condition"] == "service_completed_successfully"

        for name, service in ((engine, server), (f"weights-{engine}", weights)):
            target = mount_target(service, "models")
            assert target == "/models", f"{name} does not mount the weights volume: {service.get('volumes')}"
            for variable in ("DECIS_MODEL_DIR", "HF_HOME"):
                value = service["environment"][variable]
                assert value == target or value.startswith(f"{target}/"), (
                    f"{name}: {variable}={value} is outside {target}"
                )


# --- the port contract ---------------------------------------------------------


def test_the_published_port_cannot_desync_from_the_port_decris_listens_on(compose: dict) -> None:
    """`DECIS_PORT` comes from `.env` too, so a user who sets it would otherwise land on nothing.

    The container side is pinned to the service's `DECIS_PORT` and only the host side is
    templated; the defaults must also differ, because two engines on one host port means
    whichever starts second fails.
    """
    defaults: dict[str, str] = {}
    for engine, (server, _) in engines(compose).items():
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
    """§3-19: an unauthenticated server on 0.0.0.0 must not be reachable by default."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${DECIS_API_KEY:?" in text, "the missing-token failure must happen before an image is pulled"
    for name, service in engines(compose).items():
        assert service[0]["environment"]["DECIS_API_KEY"].startswith("${DECIS_API_KEY:?"), name


# --- liveness, not readiness ---------------------------------------------------


def test_no_service_replaces_the_images_healthcheck(compose: dict) -> None:
    """The image probes `/healthz`; a compose-level `/readyz` would restart-loop a loading engine.

    `docs/design-review.md` §2-D7 is the reason the split exists, and `Dockerfile` says
    so in a comment. Inheriting means not mentioning it, so a service that does mention a
    healthcheck is the failure this looks for.
    """
    for name, service in compose["services"].items():
        assert "healthcheck" not in service, f"{name} overrides the image's healthcheck"

    healthcheck = healthcheck_block()
    assert "/healthz" in healthcheck, healthcheck
    assert "/readyz" not in healthcheck, healthcheck


def test_the_healthcheck_cannot_be_hijacked_by_a_proxy_in_the_environment() -> None:
    """`design-review.md` §2-D19: `urllib` reads `HTTP_PROXY`, so the probe must unset it.

    An image that needs a proxy to fetch its weights exports one, and a probe that
    inherits it asks the proxy for `http://127.0.0.1:8000/healthz`. The proxy answers 502,
    and a server that logged `engine ready after 86.1s` is reported unhealthy -- in
    Kubernetes that means restarting a healthy pod forever.
    """
    healthcheck = healthcheck_block()
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        assert f"-u {variable}" in healthcheck, f"the probe would inherit {variable}: {healthcheck}"


# --- the file `.env.example` ships ---------------------------------------------


def test_the_env_example_selects_one_engine_that_exists(compose: dict) -> None:
    """The user's stated goal: `docker compose up` starts one engine, not both."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^COMPOSE_PROFILES=(.+)$", text, re.MULTILINE)
    assert match, ".env.example no longer sets COMPOSE_PROFILES, so `docker compose up` starts nothing"

    profiles = [name.strip() for name in match.group(1).split(",")]
    known = {profile for name, service in engines(compose).items() for profile in service[0]["profiles"]}
    assert set(profiles) <= known, f".env.example enables {profiles}, which is not a profile in {sorted(known)}"
    assert len(profiles) == 1, f"the default must start one engine, not {profiles}"
    # The bare `latest` image and the default profile must be the same engine, or the
    # documented `docker pull kingfs/decis` and `docker compose up` disagree.
    assert profiles == ["laya-multilingual"], profiles


def test_the_documented_profile_commands_name_real_profiles(compose: dict) -> None:
    known = {profile for _, (server, _) in engines(compose).items() for profile in server["profiles"]}
    for readme in READMES:
        for named in re.findall(r"--profile ([A-Za-z0-9._-]+)", readme.read_text(encoding="utf-8")):
            assert named in known, f"{readme.name} suggests --profile {named}, which is not one of {sorted(known)}"


# --- the local-build override --------------------------------------------------


def test_the_build_override_covers_every_service_without_taking_over_a_published_tag(
    compose: dict, planned: dict
) -> None:
    """`-f docker-compose.build.yml` must build all four services from this checkout.

    Leaving one out would silently mix a local image with a pulled one, which is the kind
    of difference that is invisible until the two disagree. The extras come from the
    workflow's own engine -> extra mapping for the same reason.
    """
    override = yaml.safe_load(BUILD_OVERRIDE.read_text(encoding="utf-8"))
    assert set(override["services"]) == set(compose["services"]), "the override misses a service"

    for name, service in override["services"].items():
        engine = name.removeprefix("weights-")
        # A local build must not reuse the published repository: `kingfs/decis:laya-multilingual`
        # would then point at whatever was built here, and a later `docker compose up`
        # without the override would quietly run the local image.
        assert service["image"] == f"decis-local:{engine}", service["image"]
        assert not service["image"].startswith("kingfs/decis:"), service["image"]
        build = service["build"]
        assert build["context"] == ".", build
        assert build["dockerfile"] == "docker/Dockerfile", build
        assert build["args"]["DECIS_ENGINE"] == engine, build
        assert build["args"]["DECIS_EXTRAS"] == planned["extras"][engine], build
