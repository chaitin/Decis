"""The Docker workflow decides what to publish. Test that decision, not the YAML.

`docker-build.yml` generates its own build matrix in bash, from the event name, the ref
and the dispatch inputs. That logic is where a release goes wrong: it can silently build
nothing, publish a moving tag from a feature branch, or ask the Dockerfile to bake weights
for an engine that has none. It cannot be exercised by running the workflow, so it is
exercised here -- the `plan` step's script is extracted from the YAML and executed with the
environment GitHub would provide.

Everything asserted here is a property that would otherwise only be discovered during a
release:

* a release tag builds both variants; a branch push (default or feature) and a pull request
  build only the weightless `-runtime` one;
* weights are downloaded (`DECIS_PREDOWNLOAD` non-empty) only on a release tag or a dispatch
  that asks for `baked`/`both` — never on a branch push, never on a pull request;
* a release moves the unsuffixed `<engine>` and `<engine>-runtime` names, a branch push moves
  only `-runtime` names, and `latest` is only ever the release's baked `laya-multilingual`
  image;
* a pull request builds the Dockerfile with no engine extra at all, which is the cheap
  check now that no weight-free engine ships;
* every engine in the matrix is a registered engine, so a rename cannot leave a stale
  matrix behind;
* the Dockerfile's `DECIS_EXTRAS` mapping matches what the registry declares;
* a `v*` tag also creates a GitHub Release, and no other event does.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docker-build.yml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"

#: Every engine the workflow images on a branch push. Pinned as a literal rather than
#: derived, because this is the set of *published tags*: `chaitin/decis:jeff-gemma4-e2b`
#: appearing in Docker Hub is a promise, and it should take a deliberate edit here to
#: start making it. `test_the_published_engines_are_the_workflows_own_list` then checks
#: this literal against the workflow's own array, so the two cannot drift apart.
PUBLISHED_ENGINES = {"laya-multilingual", "kev-0.8b", "jeff-qwen3.5-0.8b", "jeff-gemma4-e2b"}

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the workflow; it is not a runtime dependency")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return load_workflow()


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def plan_script_of(workflow: dict) -> str:
    """The `plan` step's shell: the single source of truth for the published tags.

    Plain functions as well as fixtures, because `test_compose.py` needs the same tags.
    A second copy of the tag scheme in that file is what §2 forbids.
    """
    for step in workflow["jobs"]["plan"]["steps"]:
        if step.get("id") == "plan":
            return step["run"]
    raise AssertionError("the plan job has no step with id 'plan'")


@pytest.fixture(scope="module")
def plan_script(workflow: dict) -> str:
    return plan_script_of(workflow)


def run_shell(script: str, cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a workflow step's shell the way a runner would."""
    return subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
        check=False,
    )


def run_plan(plan_script: str, tmp_path: Path, **env: str) -> dict:
    """Execute the plan step exactly as GitHub would, and parse its outputs."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    environment = {
        # A realistic runner environment: no `git` repository, so the script must not
        # depend on one. `actions/checkout` does provide one, but a shallow or detached
        # checkout can still leave `git rev-parse` unable to answer.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
        "GITHUB_OUTPUT": str(output),
        # Configured, as they are in the real repository. The guard's refusal path is
        # tested separately by overriding this.
        "HAVE_CREDENTIALS": "true",
        **env,
    }
    completed = run_shell(plan_script, tmp_path, environment)
    assert completed.returncode == 0, f"the plan step failed:\n{completed.stdout}\n{completed.stderr}"
    parsed: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    parsed["builds"] = json.loads(parsed["matrix"])["include"]
    parsed["merges"] = json.loads(parsed["merges"])["include"]
    return parsed


@pytest.fixture
def push_to_master(tmp_path: Path, plan_script: str) -> dict:
    return run_plan(plan_script, tmp_path, EVENT="push", REF="refs/heads/master", REF_NAME="master")


# --- what each event builds ----------------------------------------------------


def test_a_branch_push_builds_every_engine_for_both_architectures(push_to_master: dict) -> None:
    engines = {build["engine"] for build in push_to_master["builds"]}
    assert engines == PUBLISHED_ENGINES, engines
    arches = {build["arch"] for build in push_to_master["builds"]}
    assert arches == {"amd64", "arm64"}, arches
    assert push_to_master["push"] == "true"


def test_the_published_engines_are_the_workflows_own_list(plan_script: str) -> None:
    """`PUBLISHED_ENGINES` is checked against the array that produces the matrix.

    Otherwise the expectation is a memory of the workflow, and the way this fails is the
    quiet one: an engine is added to `all_engines`, the literal is updated by reflex, and
    nobody notices that `extra_for` has no case for it until a release build stops with
    "no pip extra is mapped".
    """
    match = re.search(r"^\s*all_engines=\(([^)]*)\)", plan_script, flags=re.MULTILINE)
    assert match, "the plan step no longer declares an `all_engines` array"
    assert set(match.group(1).split()) == PUBLISHED_ENGINES, (
        f"the workflow would image {sorted(match.group(1).split())} but this file expects {sorted(PUBLISHED_ENGINES)}"
    )


def test_a_branch_push_publishes_only_the_weightless_variant(push_to_master: dict) -> None:
    """A branch push must not pay for a checkpoint download per architecture.

    The baked image is the one whose tag carries the engine's name, and it is built only
    where a *published* baked image is the point: a release tag, or a dispatch that asks.
    On a branch push the engine name is exactly what would promise offline weights, so it is
    not built at all.
    """
    assert {build["variant"] for build in push_to_master["builds"]} == {"runtime"}, push_to_master["builds"]
    assert all(build["bake"] == "" for build in push_to_master["builds"])


def test_a_release_moves_the_unsuffixed_names_and_a_branch_push_does_not(tmp_path: Path, plan_script: str) -> None:
    """The bare `<engine>` tag means "the newest *released* image", so only a release moves it.

    `docker-compose.yml` and the README pull the bare names, so they have to keep existing;
    a branch push must not repoint one, or a `docker compose up` would silently start
    whatever commit happened to be pushed last.
    """
    released = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v1.2.0", REF_NAME="v1.2.0")
    aliases = {(merge["engine"], merge["variant"]): merge["alias_tag"] for merge in released["merges"]}
    assert aliases[("laya-multilingual", "baked")] == "laya-multilingual", aliases
    assert aliases[("laya-multilingual", "runtime")] == "laya-multilingual-runtime", aliases
    assert aliases[("kev-0.8b", "baked")] == "kev-0.8b", aliases
    assert aliases[("kev-0.8b", "runtime")] == "kev-0.8b-runtime", aliases
    # ...and `latest` is claimed by the baked default engine and by nothing else.
    promoted = [(merge["engine"], merge["variant"]) for merge in released["merges"] if merge["promote_latest"]]
    assert promoted == [("laya-multilingual", "baked")], promoted

    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", "refs/heads/topic", "topic"),
        ("pull_request", "refs/pull/7/merge", "7/merge"),
    ):
        plan = run_plan(plan_script, tmp_path, EVENT=event, REF=ref, REF_NAME=name)
        assert all(merge["alias_tag"] == "" for merge in plan["merges"]), plan["merges"]
        assert not any(merge["promote_latest"] for merge in plan["merges"]), plan["merges"]


def test_a_branch_push_moves_only_weightless_names(push_to_master: dict) -> None:
    """No branch push may move `latest` or a bare engine name someone could have pinned.

    The bare names mean "the newest released image", so a branch push moves the `-runtime`
    moving names and nothing else. A version tag is the pin; a branch push is not.
    """
    moved = {build["image_tag"] for build in push_to_master["builds"]}
    moved |= {merge["alias_tag"] for merge in push_to_master["merges"] if merge["alias_tag"]}
    assert moved, "the branch push publishes nothing"
    assert all(tag.endswith("-runtime") for tag in moved), sorted(moved)
    assert "latest" not in moved
    assert not (moved & PUBLISHED_ENGINES), sorted(moved & PUBLISHED_ENGINES)
    assert not any(merge["promote_latest"] for merge in push_to_master["merges"])


def test_a_feature_branch_pins_the_commit_instead_of_moving_a_name(tmp_path: Path, plan_script: str) -> None:
    """A topic branch must not touch a name someone could have pinned to."""
    plan = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/heads/topic", REF_NAME="topic")
    moved = {build["image_tag"] for build in plan["builds"]}
    moved |= {merge["alias_tag"] for merge in plan["merges"] if merge["alias_tag"]}
    assert moved == {
        "laya-multilingual-runtime-sha-a1b2c3d",
        "kev-0.8b-runtime-sha-a1b2c3d",
        "jeff-qwen3.5-0.8b-runtime-sha-a1b2c3d",
        "jeff-gemma4-e2b-runtime-sha-a1b2c3d",
    }, sorted(moved)
    assert not any(merge["promote_latest"] for merge in plan["merges"]), plan["merges"]


def test_only_a_release_or_an_explicit_dispatch_downloads_weights(tmp_path: Path, plan_script: str) -> None:
    """`DECIS_PREDOWNLOAD` is the whole cost of a baked image, so the event has to decide it.

    `decis download` writes the checkpoint into the image at build time — 647 MiB for Laya,
    8.65 GiB for Gemma — times two architectures. A branch push, a feature branch and a pull
    request must never ask for that; the only ways in are a `v*` tag and a dispatch that
    names the weights (`variants: baked` or `both`).
    """

    def baked(plan: dict) -> set[str]:
        return {build["engine"] for build in plan["builds"] if build["bake"]}

    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", "refs/heads/main", "main"),
        ("push", "refs/heads/feature/x", "feature/x"),
        ("pull_request", "refs/pull/7/merge", "7/merge"),
    ):
        plan = run_plan(plan_script, tmp_path, EVENT=event, REF=ref, REF_NAME=name)
        assert not baked(plan), f"{event} on {ref} would download weights: {sorted(baked(plan))}"

    released = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v9.9.9", REF_NAME="v9.9.9")
    assert baked(released) == PUBLISHED_ENGINES, sorted(baked(released))

    for variants, expected in (
        # The default: no click on the weights, no download.
        ("runtime", set()),
        ("baked", {"laya-multilingual"}),
        ("both", {"laya-multilingual"}),
    ):
        plan = run_plan(
            plan_script,
            tmp_path,
            EVENT="workflow_dispatch",
            REF="refs/heads/master",
            REF_NAME="master",
            INPUT_ENGINES="laya-multilingual",
            INPUT_VARIANTS=variants,
            INPUT_PUSH="true",
        )
        assert baked(plan) == expected, f"variants={variants}: {sorted(baked(plan))}"


def test_a_version_tag_also_builds_the_weightless_variants(tmp_path: Path, plan_script: str) -> None:
    """A deployment that mounts one copy of the weights still needs an image without them."""
    plan = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v1.2.0", REF_NAME="v1.2.0")
    variants = {build["variant"] for build in plan["builds"]}
    assert variants == {"baked", "runtime"}, variants
    assert plan["tag"] == "v1.2.0"
    for build in plan["builds"]:
        assert build["bake"] == (build["engine"] if build["variant"] == "baked" else ""), build


def test_weights_are_never_baked_for_an_engine_that_has_none(tmp_path: Path, plan_script: str) -> None:
    """`DECIS_PREDOWNLOAD` must name an engine that actually has weights.

    `decis download --engine <weightless id>` exits 2 (a download request for a
    weight-free engine probably means a mistyped engine id), so baking one would fail
    the build rather than produce a smaller image.
    """
    plan = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v9.9.9", REF_NAME="v9.9.9")
    for build in plan["builds"]:
        assert build["bake"] in ("", build["engine"]), build
    baked = {build["engine"] for build in plan["builds"] if build["bake"]}
    assert baked == PUBLISHED_ENGINES, baked

    # ...and baking needs the engine's extra: `decis download` verifies its work through
    # `paths.resolve`, which needs the engine installed. An engine-free leg that asked for
    # weights would download them and then fail the build (`design-review.md` §2-D14 is the
    # same shape of mistake, caught here instead of after a 1.4 GB download).
    for build in plan["builds"]:
        assert not (build["bake"] and not build["extra"]), build


def test_a_pull_request_builds_an_engine_free_image(tmp_path: Path, plan_script: str) -> None:
    """The cheap check: build the Dockerfile without installing an engine runtime.

    There is no weight-free engine to publish any more, so a PR spends its time on the
    install path rather than on torch. `DECIS_ENGINE` still names a registered engine, so
    the app builds and `/v1/models` has something to list.
    """
    plan = run_plan(plan_script, tmp_path, EVENT="pull_request", REF="refs/pull/7/merge", REF_NAME="7/merge")
    assert plan["push"] == "false", "a pull request must not push to the registry"
    assert plan["builds"], "a PR must still build, or the Dockerfile is never checked"
    # The point is the Dockerfile, so one engine on one platform is enough.
    assert len(plan["builds"]) == 1, plan["builds"]
    assert plan["tag"].startswith("sha-"), plan["tag"]
    build = plan["builds"][0]
    assert build["extra"] == "", f"a PR would install engine dependencies: {build['extra']!r}"
    assert build["bake"] == "", f"a PR would download weights it cannot verify: {build['bake']!r}"
    assert build["engine"] == "laya-multilingual", build


def test_the_moving_tag_is_never_set_from_a_pull_request(tmp_path: Path, plan_script: str) -> None:
    """`latest` from a PR would publish unreviewed code to everyone's `docker pull`."""
    for ref, name in (("refs/pull/7/merge", "7/merge"), ("refs/heads/feature/x", "feature/x")):
        plan = run_plan(plan_script, tmp_path, EVENT="pull_request", REF=ref, REF_NAME=name)
        assert plan["tag"] != "latest", f"{ref} produced a moving tag"


def test_dispatch_inputs_are_honoured(tmp_path: Path, plan_script: str) -> None:
    plan = run_plan(
        plan_script,
        tmp_path,
        EVENT="workflow_dispatch",
        REF="refs/heads/master",
        REF_NAME="master",
        # Includes a dotted engine id: `--model-path` and the compose profile both carry
        # ids verbatim now, so nothing may quietly normalise the dot away on this path.
        INPUT_ENGINES="laya-multilingual kev-0.8b jeff-qwen3.5-0.8b",
        INPUT_VARIANTS="both",
        INPUT_PUSH="true",
    )
    engines = {build["engine"] for build in plan["builds"]}
    assert engines == {"laya-multilingual", "kev-0.8b", "jeff-qwen3.5-0.8b"}, engines
    assert {build["variant"] for build in plan["builds"]} == {"baked", "runtime"}
    assert plan["push"] == "true"


def test_a_dispatch_defaults_to_the_weightless_variant(tmp_path: Path, plan_script: str) -> None:
    """The dispatch default is the cheap artifact a branch push publishes, not the baked one.

    A manual run has to ask for the weights; forgetting to click `baked` must not quietly
    start an 8.65 GiB download per architecture.
    """
    plan = run_plan(
        plan_script,
        tmp_path,
        EVENT="workflow_dispatch",
        REF="refs/heads/master",
        REF_NAME="master",
        INPUT_ENGINES="laya-multilingual",
        # No INPUT_VARIANTS: the `variants` input's own default has to apply.
        INPUT_PUSH="false",
    )
    assert {build["variant"] for build in plan["builds"]} == {"runtime"}, plan["builds"]
    assert all(build["bake"] == "" for build in plan["builds"])


def test_a_dispatch_can_ask_for_the_baked_variant(tmp_path: Path, plan_script: str) -> None:
    """A dispatch with `push=true` is the only manual way to publish a baked image."""
    plan = run_plan(
        plan_script,
        tmp_path,
        EVENT="workflow_dispatch",
        REF="refs/heads/master",
        REF_NAME="master",
        INPUT_ENGINES="laya-multilingual",
        INPUT_VARIANTS="baked",
        INPUT_PUSH="true",
    )
    assert {build["variant"] for build in plan["builds"]} == {"baked"}, plan["builds"]
    assert all(build["bake"] == "laya-multilingual" for build in plan["builds"])


def test_a_dispatch_with_an_unknown_variant_stops_the_plan(tmp_path: Path, plan_script: str) -> None:
    """A typo must fail loudly rather than fall back to some variant nobody asked for."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    completed = run_shell(
        plan_script,
        tmp_path,
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
            "GITHUB_OUTPUT": str(output),
            "HAVE_CREDENTIALS": "true",
            "EVENT": "workflow_dispatch",
            "REF": "refs/heads/master",
            "REF_NAME": "master",
            "INPUT_ENGINES": "laya-multilingual",
            "INPUT_VARIANTS": "everything",
            "INPUT_PUSH": "false",
        },
    )
    assert completed.returncode != 0, "an unknown `variants` value was planned anyway"
    assert "variants must be one of" in completed.stderr, completed.stderr


def test_a_dispatch_dry_run_does_not_push(tmp_path: Path, plan_script: str) -> None:
    """The default has to be safe: a dispatch with no inputs must not publish."""
    plan = run_plan(
        plan_script,
        tmp_path,
        EVENT="workflow_dispatch",
        REF="refs/heads/master",
        REF_NAME="master",
        INPUT_ENGINES="laya-multilingual",
        INPUT_VARIANTS="runtime",
        INPUT_PUSH="false",
    )
    assert plan["push"] == "false"


# --- the plan agrees with the code it describes --------------------------------


def test_every_engine_in_the_workflow_is_a_registered_engine(push_to_master: dict) -> None:
    """A renamed engine must not leave a stale entry that fails at release time."""
    sys.path.insert(0, str(ROOT / "src"))
    from decis.engines.registry import SPECS

    planned = {build["engine"] for build in push_to_master["builds"]}
    unknown = planned - set(SPECS)
    assert not unknown, f"the workflow builds engines that are not registered: {sorted(unknown)}"


def test_the_planned_extra_matches_the_registry(push_to_master: dict) -> None:
    """Each image must install exactly the dependencies its engine declares.

    Asserted against the *planned matrix*, not against the YAML text: the value used to
    come from `engine == 'x' && '' || 'laya'`, and an empty string is falsy in a GitHub
    expression, so the engine-free image silently installed Laya and shipped 3.1 GB of
    torch. Only executing the step catches that.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from decis.engines.registry import SPECS

    # `tests/conftest.py` registers a weight-free test double in this process. It lives in
    # `tests/` and is not a shipped engine, so it is not something to image.
    shipped = {name for name, spec in SPECS.items() if not spec.target.startswith("fixture_engine")}

    planned = {build["engine"]: build["extra"] for build in push_to_master["builds"]}
    assert set(planned) == PUBLISHED_ENGINES, sorted(planned)
    for engine, extra in planned.items():
        assert engine in shipped, f"{engine} is in the workflow but not the registry"
        assert SPECS[engine].extra == extra, (
            f"{engine}: the registry declares extra {SPECS[engine].extra!r}, the workflow would install {extra!r}"
        )

    # And every engine the registry can serve is either imaged or deliberately not.
    unimaged = shipped - set(planned)
    assert unimaged == {"laya", "laya-typed-decisions"}, (
        f"{sorted(unimaged)} are registered but have no image. Either add them to the "
        f"workflow's engine list or say here why they share an image."
    )


def test_an_engine_with_no_mapped_extra_stops_the_plan(tmp_path: Path, plan_script: str) -> None:
    """An image that installs nothing is better than one that installs the wrong thing."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    completed = run_shell(
        plan_script,
        tmp_path,
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
            "GITHUB_OUTPUT": str(output),
            "HAVE_CREDENTIALS": "true",
            "EVENT": "workflow_dispatch",
            "REF": "refs/heads/master",
            "REF_NAME": "master",
            "INPUT_ENGINES": "laya-typed-decisions",
            "INPUT_VARIANTS": "runtime",
            "INPUT_PUSH": "false",
        },
    )
    assert completed.returncode != 0, "an engine with no extras mapping was planned anyway"
    assert "no pip extra is mapped" in completed.stderr, completed.stderr


def test_the_workflow_passes_only_dockerfile_declared_build_args(push_to_master: dict) -> None:
    """An undeclared `--build-arg` is silently ignored by Docker, so it would do nothing."""
    declared = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARG "):
            declared.add(line.split()[1].split("=")[0])
    for name in ("DECIS_EXTRAS", "DECIS_ENGINE", "DECIS_PREDOWNLOAD"):
        assert name in declared, f"{name} is passed by the workflow but never declared in the Dockerfile"


# --- where the images go, and under what names ---------------------------------
#
# The registry and the tag scheme are the published interface: a user types these, and a
# rename is not something you can quietly undo. So they are tested by *running* the steps
# that build them rather than by matching strings in the YAML. That needs `docker` to be
# a stub -- these steps only ever ask it to copy manifests, which is what we want to
# observe.


@pytest.fixture(scope="module")
def build_meta_script(workflow: dict) -> str:
    for step in workflow["jobs"]["build"]["steps"]:
        if step.get("id") == "meta":
            return step["run"]
    pytest.fail("the build job has no step with id 'meta'")


@pytest.fixture(scope="module")
def merge_script(workflow: dict) -> str:
    for step in workflow["jobs"]["merge"]["steps"]:
        if step.get("name") == "Create the multi-arch manifest":
            return step["run"]
    pytest.fail("the merge job has no 'Create the multi-arch manifest' step")


@pytest.fixture(scope="module")
def playground_meta_script(workflow: dict) -> str:
    for step in workflow["jobs"]["playground"]["steps"]:
        if step.get("id") == "meta":
            return step["run"]
    pytest.fail("the playground job has no step with id 'meta'")


def parse_outputs(path: Path) -> dict[str, str]:
    """Parse a `$GITHUB_OUTPUT` file, heredocs included.

    A multi-line output is written as `name<<EOF` ... `EOF`, so splitting every line on
    `=` silently drops all but the first line of exactly the values worth testing.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            name, _, delimiter = line.partition("<<")
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            outputs[name] = "\n".join(body)
        elif "=" in line:
            name, _, value = line.partition("=")
            outputs[name] = value
        index += 1
    return outputs


def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    """A `docker` that records what it was asked to do instead of contacting a registry."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "docker.log"
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        # `imagetools inspect` is used as an existence probe; every tag exists in a test.
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir, log


def run_step(script: str, tmp_path: Path, docker_bin: Path, **env: str) -> subprocess.CompletedProcess:
    environment = {
        "PATH": f"{docker_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
        "REGISTRY": "docker.io",
        "IMAGE_REPO": "decis",
        # The published namespace, not a credential: the workflow fixes it, so a token
        # held by some other account cannot move the images.
        "IMAGE_NAMESPACE": "chaitin",
        **env,
    }
    return run_shell(script, tmp_path, environment)


def test_the_published_registry_is_docker_hub(workflow: dict) -> None:
    """The project publishes to exactly one registry, and it must be the documented one."""
    assert workflow["env"]["REGISTRY"] == "docker.io"
    assert workflow["env"]["IMAGE_REPO"] == "decis"
    # The namespace is decided by the workflow (a repository variable may override it),
    # not derived at run time from whichever credential happens to be configured.
    assert "chaitin" in workflow["env"]["IMAGE_NAMESPACE"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io" not in text, "a GHCR reference survived the move to Docker Hub"


def test_a_version_tag_also_creates_the_github_release(workflow: dict) -> None:
    """A tag push has to produce a Release, or the Releases page lags the tags.

    `v0.1.0` was published by hand and `v0.2.0` and `v0.3.0` were never released at all, so
    the repository's "latest release" sat several versions behind its own tags. Only the
    version-tag leg may create one -- a branch push must not announce a release -- and it
    waits for the manifests, because a release is the announcement that the artifacts
    exist. `gh release view` comes first because re-running the workflow for a tag that
    already has a release is normal (`gh run rerun`) and must not fail.
    """
    creators = [
        name
        for name, job in workflow["jobs"].items()
        if any("gh release create" in (step.get("run") or "") for step in job.get("steps") or [])
    ]
    assert creators == ["release"], f"expected exactly one job to create releases, got {creators}"

    job = workflow["jobs"]["release"]
    assert "startsWith(github.ref, 'refs/tags/v')" in job["if"], job["if"]
    assert "needs.plan.outputs.push == 'true'" in job["if"], job["if"]
    assert job["permissions"]["contents"] == "write", job["permissions"]
    assert "merge" in job["needs"], "a release must not be announced before the manifests exist"

    script = "\n".join(step.get("run") or "" for step in job["steps"])
    assert "gh release view" in script, "a tag that already has a release has to be a no-op, not a failure"
    assert "--generate-notes" in script, "the notes have to come from the repository, not a hand-written file"


@pytest.mark.parametrize(
    ("event", "ref", "ref_name", "expected"),
    [
        # A branch push publishes the weightless variant under its moving name. The baked
        # image is not built there at all, so the bare engine name is absent from this
        # table: it only moves on a release.
        (
            "push",
            "refs/heads/master",
            "master",
            {
                ("laya-multilingual", "runtime"): "laya-multilingual-runtime",
                ("kev-0.8b", "runtime"): "kev-0.8b-runtime",
            },
        ),
        # A release tag pins the version for both variants...
        (
            "push",
            "refs/tags/v1.2.0",
            "v1.2.0",
            {
                ("laya-multilingual", "baked"): "laya-multilingual-v1.2.0",
                ("kev-0.8b", "baked"): "kev-0.8b-v1.2.0",
                ("laya-multilingual", "runtime"): "laya-multilingual-runtime-v1.2.0",
                ("kev-0.8b", "runtime"): "kev-0.8b-runtime-v1.2.0",
            },
        ),
        # ...and a feature branch never moves a name someone could have pinned to.
        (
            "push",
            "refs/heads/topic",
            "topic",
            {
                ("laya-multilingual", "runtime"): "laya-multilingual-runtime-sha-a1b2c3d",
                ("kev-0.8b", "runtime"): "kev-0.8b-runtime-sha-a1b2c3d",
            },
        ),
    ],
)
def test_the_image_tag_is_decided_by_the_event(
    tmp_path: Path, plan_script: str, event: str, ref: str, ref_name: str, expected: dict[tuple[str, str], str]
) -> None:
    """The tag a user types is decided by the plan script, so it is tested there."""
    planned = run_plan(plan_script, tmp_path, EVENT=event, REF=ref, REF_NAME=ref_name)
    tags = {(b["engine"], b["variant"]): b["image_tag"] for b in planned["builds"]}
    for key, tag in expected.items():
        assert tags.get(key) == tag, f"{key} on {ref}: expected {tag!r}, got {tags.get(key)!r}"


def test_the_weightless_variant_is_a_distinct_tag(tmp_path: Path, plan_script: str) -> None:
    """An image without weights is a different artifact, so it must not share a tag."""
    planned = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v1.2.0", REF_NAME="v1.2.0")
    tags = {(b["engine"], b["variant"]): b["image_tag"] for b in planned["builds"]}
    assert tags[("laya-multilingual", "baked")] == "laya-multilingual-v1.2.0", tags
    assert tags[("laya-multilingual", "runtime")] == "laya-multilingual-runtime-v1.2.0", tags


def test_no_plan_publishes_the_same_name_twice(tmp_path: Path, plan_script: str) -> None:
    """A moving alias must not overwrite another artifact's tag.

    The merge job writes the alias on top of the manifest it just built, so a release whose
    alias collided with another engine's tag would publish one engine's image under the
    other's name — and the per-arch images would still all be there, so nothing would look
    wrong until someone pulled it.
    """
    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", "refs/tags/v1.2.0", "v1.2.0"),
        ("push", "refs/heads/topic", "topic"),
    ):
        planned = run_plan(plan_script, tmp_path, EVENT=event, REF=ref, REF_NAME=name)
        names = [merge["image_tag"] for merge in planned["merges"]]
        names += [merge["alias_tag"] for merge in planned["merges"] if merge["alias_tag"]]
        if any(merge["promote_latest"] for merge in planned["merges"]):
            names.append("latest")
        assert len(names) == len(set(names)), f"{ref} publishes {names} twice over"


# --- the one non-engine image ---------------------------------------------------


def test_the_playground_is_tagged_but_never_as_an_engine(tmp_path: Path, plan_script: str) -> None:
    """`playground` on the default branch, `playground-v1.2.0` on a release.

    It shares the engines' repository -- a second Docker Hub repository would be a manual
    step nothing else needs -- so its tag has to be as predictable as theirs. It must also
    never *collide* with one: `chaitin/decis:playground` is a web page, not a checkpoint, and
    a tag that matched an engine's would publish one over the other.
    """
    for event, ref, name, expected in (
        ("push", "refs/heads/master", "master", "playground"),
        ("push", "refs/tags/v1.2.0", "v1.2.0", "playground-v1.2.0"),
        ("push", "refs/heads/topic", "topic", "playground-sha-a1b2c3d"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        plan = run_plan(plan_script, directory, EVENT=event, REF=ref, REF_NAME=name)
        assert plan["playground_tag"] == expected, plan["playground_tag"]
        engine_tags = {build["image_tag"] for build in plan["builds"]}
        assert plan["playground_tag"] not in engine_tags, "the playground tag collides with an engine's"


def test_a_pull_request_builds_the_playground_for_one_platform(tmp_path: Path, plan_script: str) -> None:
    """A build that does not push cannot export a multi-platform manifest list.

    `test_a_pull_request_builds_an_engine_free_image` covers the engines; this is the same
    trap for the playground's single job, which pushes both architectures in one step
    (safe without emulation because that Dockerfile has no `RUN`).
    """
    pull_request = tmp_path / "pr"
    pull_request.mkdir()
    plan = run_plan(plan_script, pull_request, EVENT="pull_request", REF="refs/pull/7/merge", REF_NAME="7/merge")
    assert plan["push"] == "false"
    assert plan["playground_platforms"] == "linux/amd64", plan["playground_platforms"]

    master = tmp_path / "master"
    master.mkdir()
    released = run_plan(plan_script, master, EVENT="push", REF="refs/heads/master", REF_NAME="master")
    assert set(released["playground_platforms"].split(",")) == {"linux/amd64", "linux/arm64"}, released[
        "playground_platforms"
    ]


def test_the_playground_image_shares_the_engines_repository(playground_meta_script: str, tmp_path: Path) -> None:
    """`chaitin/decis:playground`, not a second repository to create by hand."""
    docker_bin, _ = fake_docker(tmp_path)
    completed = run_step(
        playground_meta_script,
        tmp_path,
        docker_bin,
        PLAYGROUND_TAG="playground",
        GITHUB_OUTPUT=str(tmp_path / "out"),
    )
    assert completed.returncode == 0, completed.stderr
    outputs = parse_outputs(tmp_path / "out")
    assert outputs["image"] == "docker.io/chaitin/decis", outputs
    assert outputs["tag"] == "playground", outputs


def test_the_playground_job_builds_the_playground_dockerfile(workflow: dict) -> None:
    """The path in the workflow is the Dockerfile `docker-compose.override.yml` builds.

    The compose override and the publish job are two places that name this file; a rename
    in one of them has to fail here rather than at release time.
    """
    steps = workflow["jobs"]["playground"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build and push")
    assert build["with"]["file"] == "playground/Dockerfile", build["with"]
    assert build["with"]["platforms"] == "${{ needs.plan.outputs.playground_platforms }}", build["with"]


def test_no_two_engines_publish_the_same_provenance_tag(build_meta_script: str, tmp_path: Path) -> None:
    """Every tag a build leg writes must be written by that leg alone.

    All engines share one repository, so a provenance tag of `sha-<commit>-<arch>` is
    written by all six legs at once. They overwrite each other's images, and by the time
    the merge jobs run they all read back whichever engine finished last -- so three
    different tags got published as the same manifest.
    """
    docker_bin, _ = fake_docker(tmp_path)
    published: dict[str, set[str]] = {}
    for engine, variant in (
        ("laya-multilingual", "baked"),
        ("laya-multilingual", "runtime"),
        ("kev-0.8b", "baked"),
        ("kev-0.8b", "runtime"),
        ("jeff-qwen3.5-0.8b", "baked"),
        ("jeff-qwen3.5-0.8b", "runtime"),
        ("jeff-gemma4-e2b", "baked"),
        ("jeff-gemma4-e2b", "runtime"),
    ):
        for arch in ("amd64", "arm64"):
            out = tmp_path / f"{engine}-{variant}-{arch}"
            completed = run_step(
                build_meta_script,
                tmp_path,
                docker_bin,
                ENGINE=engine,
                VARIANT=variant,
                ARCH=arch,
                IMAGE_TAG=engine if variant == "baked" else f"{engine}-runtime",
                GITHUB_OUTPUT=str(out),
            )
            assert completed.returncode == 0, completed.stderr
            published[f"{engine}/{variant}/{arch}"] = set(parse_outputs(out)["tags"].splitlines())

    collisions = {
        (a, b): tags_a & tags_b
        for a, tags_a in published.items()
        for b, tags_b in published.items()
        if a < b and tags_a & tags_b
    }
    assert not collisions, f"build legs would overwrite each other: {collisions}"


def test_every_engine_shares_one_repository(build_meta_script: str, tmp_path: Path) -> None:
    """`chaitin/decis:laya-multilingual`, not `chaitin/decis-laya-multilingual`."""
    docker_bin, _ = fake_docker(tmp_path)
    completed = run_step(
        build_meta_script,
        tmp_path,
        docker_bin,
        ENGINE="laya-multilingual",
        VARIANT="runtime",
        ARCH="amd64",
        IMAGE_TAG="laya-multilingual",
        GITHUB_OUTPUT=str(tmp_path / "out"),
    )
    assert completed.returncode == 0, completed.stderr
    outputs = parse_outputs(tmp_path / "out")
    assert outputs["image"] == "docker.io/chaitin/decis", outputs
    tags = outputs["tags"].splitlines()
    assert "docker.io/chaitin/decis:laya-multilingual-amd64" in tags, tags
    # The per-arch provenance tag the merge job consumes must name the artifact as well as
    # the platform.
    assert "docker.io/chaitin/decis:laya-multilingual-sha-a1b2c3d4e5f6-amd64" in tags, tags


def test_the_namespace_is_neither_the_credential_nor_the_repository_owner(
    build_meta_script: str, tmp_path: Path
) -> None:
    """A published name must not follow whichever account holds the token.

    `DOCKERHUB_USERNAME` is a Docker Hub *login*; an organization is not a user account,
    so the account that authenticates is not necessarily the namespace the docs name.
    Deriving the image from it pushes somewhere no document mentions, which is invisible
    until someone cannot pull what the README told them to pull.
    """
    docker_bin, _ = fake_docker(tmp_path)
    completed = run_step(
        build_meta_script,
        tmp_path,
        docker_bin,
        ENGINE="laya-multilingual",
        VARIANT="runtime",
        ARCH="amd64",
        IMAGE_TAG="laya-multilingual",
        # A fork would see its own owner here, and the credential can be any account.
        GITHUB_REPOSITORY_OWNER="someone-else",
        DOCKERHUB_USERNAME="someone-elses-account",
        IMAGE_NAMESPACE="Chaitin",  # registries require lowercase
        GITHUB_OUTPUT=str(tmp_path / "out"),
    )
    assert completed.returncode == 0, completed.stderr
    assert parse_outputs(tmp_path / "out")["image"] == "docker.io/chaitin/decis"


def merged_tags(log: Path) -> list[str]:
    """Every tag the merge step asked `docker` to create from the per-arch images."""
    tags = []
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[:3] == ["buildx", "imagetools", "create"]:
            tags.append(parts[parts.index("--tag") + 1])
    return tags


@pytest.mark.parametrize(
    ("engine", "variant", "image_tag", "alias_tag", "promote_latest", "expected_latest"),
    [
        # `laya-multilingual` is the server's default engine and the one the README leads
        # with, so it is the right answer to a bare `docker pull chaitin/decis`. It is the
        # *baked* variant of a release, so it is also what `latest` follows.
        ("laya-multilingual", "baked", "laya-multilingual-v1.2.0", "laya-multilingual", "true", True),
        # The weightless variant moves its own unsuffixed name but never `latest`...
        (
            "laya-multilingual",
            "runtime",
            "laya-multilingual-runtime-v1.2.0",
            "laya-multilingual-runtime",
            "false",
            False,
        ),
        # ...and neither does another engine, even on a release.
        ("kev-0.8b", "baked", "kev-0.8b-v1.2.0", "kev-0.8b", "false", False),
        # A branch push's moving name has nothing to alias.
        ("laya-multilingual", "runtime", "laya-multilingual-runtime", "", "false", False),
    ],
)
def test_only_the_default_engine_claims_the_bare_latest_tag(
    merge_script: str,
    tmp_path: Path,
    engine: str,
    variant: str,
    image_tag: str,
    alias_tag: str,
    promote_latest: str,
    expected_latest: bool,
) -> None:
    docker_bin, log = fake_docker(tmp_path)
    completed = run_step(
        merge_script,
        tmp_path,
        docker_bin,
        ENGINE=engine,
        VARIANT=variant,
        IMAGE_TAG=image_tag,
        ALIAS_TAG=alias_tag,
        PROMOTE_LATEST=promote_latest,
    )
    assert completed.returncode == 0, completed.stderr
    tags = merged_tags(log)
    assert f"docker.io/chaitin/decis:{image_tag}" in tags, tags
    if alias_tag:
        assert f"docker.io/chaitin/decis:{alias_tag}" in tags, tags
    assert ("docker.io/chaitin/decis:latest" in tags) is expected_latest, tags


def test_the_merge_uses_the_per_arch_images_of_this_commit(merge_script: str, tmp_path: Path) -> None:
    """Every source must be this commit's per-arch manifest, or a release mixes commits."""
    docker_bin, log = fake_docker(tmp_path)
    # Without an alias or `latest` there is exactly one `imagetools create`, so the sources
    # of the published manifest are the only ones in the log.
    completed = run_step(
        merge_script,
        tmp_path,
        docker_bin,
        ENGINE="kev-0.8b",
        VARIANT="runtime",
        IMAGE_TAG="kev-0.8b-runtime",
        ALIAS_TAG="",
        PROMOTE_LATEST="false",
    )
    assert completed.returncode == 0, completed.stderr
    create = [line for line in log.read_text(encoding="utf-8").splitlines() if "imagetools create" in line]
    assert len(create) == 1, create
    words = create[0].split()
    # Drop the `--tag <ref>` pair: its value is a destination, not a source.
    skip = {words.index("--tag") + 1} if "--tag" in words else set()
    sources = [word for i, word in enumerate(words) if word.startswith("docker.io/") and i not in skip]
    assert sources == [
        "docker.io/chaitin/decis:kev-0.8b-runtime-sha-a1b2c3d4e5f6-amd64",
        "docker.io/chaitin/decis:kev-0.8b-runtime-sha-a1b2c3d4e5f6-arm64",
    ], sources


def test_the_alias_and_latest_are_the_same_image_as_the_versioned_tag(merge_script: str, tmp_path: Path) -> None:
    """A moving name must point at exactly the manifest it claims to stand for.

    Rebuilding the manifest from something other than the sources just merged is how
    `:laya-multilingual` and `:laya-multilingual-v1.2.0` end up different images.
    """
    docker_bin, log = fake_docker(tmp_path)
    completed = run_step(
        merge_script,
        tmp_path,
        docker_bin,
        ENGINE="laya-multilingual",
        VARIANT="baked",
        IMAGE_TAG="laya-multilingual-v1.2.0",
        ALIAS_TAG="laya-multilingual",
        PROMOTE_LATEST="true",
    )
    assert completed.returncode == 0, completed.stderr
    creates = [line for line in log.read_text(encoding="utf-8").splitlines() if "imagetools create" in line]
    assert len(creates) == 3, creates

    def sources_of(line: str) -> list[str]:
        words = line.split()
        skip = {words.index("--tag") + 1} if "--tag" in words else set()
        return [word for i, word in enumerate(words) if word.startswith("docker.io/") and i not in skip]

    first = sources_of(creates[0])
    assert first, creates[0]
    for line in creates[1:]:
        assert sources_of(line) == first, (line, first)


def test_a_missing_platform_leg_is_left_out_rather_than_faked(merge_script: str, tmp_path: Path) -> None:
    """An amd64-only release must not look multi-arch.

    The existence probe is what decides this, so the test makes it fail for arm64.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        # The arm64 probe fails, as it would if that leg had not run.
        'if [ "$3" = "inspect" ] && [[ "$4" == *arm64* ]]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)

    completed = run_step(
        merge_script,
        tmp_path,
        bin_dir,
        ENGINE="laya-multilingual",
        VARIANT="runtime",
        IMAGE_TAG="laya-multilingual-runtime",
        ALIAS_TAG="",
        PROMOTE_LATEST="false",
    )
    assert completed.returncode == 0, completed.stderr
    create = [line for line in log.read_text(encoding="utf-8").splitlines() if "imagetools create" in line]
    assert create, "the merge gave up instead of merging what was there"
    assert "arm64" not in create[0], create
    assert "not built" in completed.stderr, completed.stderr


def test_merging_nothing_at_all_fails(merge_script: str, tmp_path: Path) -> None:
    """Silently publishing no image is worse than failing the release."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "docker").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)

    completed = run_step(
        merge_script,
        tmp_path,
        bin_dir,
        ENGINE="laya-multilingual",
        VARIANT="runtime",
        IMAGE_TAG="laya-multilingual-runtime",
        ALIAS_TAG="",
        PROMOTE_LATEST="false",
    )
    assert completed.returncode != 0
    assert "nothing to merge" in completed.stderr, completed.stderr


# --- the credential guard -------------------------------------------------------


def test_publishing_without_credentials_stops_before_anything_is_built(plan_script: str, tmp_path: Path) -> None:
    """A publish that cannot authenticate must fail here, not after six builds.

    Failing loudly is the point: a green run that quietly pushed nothing is the failure
    nobody notices until a user cannot pull the image.
    """
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    completed = run_shell(
        plan_script,
        tmp_path,
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
            "GITHUB_OUTPUT": str(output),
            "HAVE_CREDENTIALS": "false",
            "EVENT": "push",
            "REF": "refs/heads/master",
            "REF_NAME": "master",
        },
    )
    assert completed.returncode != 0, "a publish without credentials was allowed"
    assert "DOCKERHUB_USERNAME" in completed.stderr, completed.stderr
    assert "nothing" not in output.read_text(encoding="utf-8"), "a matrix was emitted anyway"


def test_a_dry_run_needs_no_credentials(plan_script: str, tmp_path: Path) -> None:
    """The dispatch dry-run exists so a fork or a new repo can validate the workflow."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    completed = run_shell(
        plan_script,
        tmp_path,
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_SHA": "a1b2c3d4e5f6a7b8c9d0",
            "GITHUB_OUTPUT": str(output),
            "HAVE_CREDENTIALS": "false",
            "EVENT": "workflow_dispatch",
            "REF": "refs/heads/master",
            "REF_NAME": "master",
            "INPUT_ENGINES": "laya-multilingual",
            "INPUT_VARIANTS": "runtime",
            "INPUT_PUSH": "false",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "push=false" in output.read_text(encoding="utf-8")


# --- what the docs tell a user to pull -----------------------------------------


IMAGE_REFERENCE = re.compile(r"chaitin/decis:([A-Za-z0-9][A-Za-z0-9._-]*)")


def documented_tags(path: Path) -> set[str]:
    """Every image tag this file tells a user to pull.

    Two places say so: a `chaitin/decis:<tag>` in a command, and the first column of the
    deployment table. Both are read, because the D15 defect was a *table row* for a tag
    the workflow never built -- a guard that only scanned commands would have missed it.
    A cell may spell the reference out and name the alias beside it
    (`| `chaitin/decis:laya-multilingual` (= `:latest`) |`), so a cell is reduced to the tag
    it stands for rather than compared as written.
    """
    text = path.read_text(encoding="utf-8")
    tags = set(IMAGE_REFERENCE.findall(text))
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| Tag |"):
            for row in lines[index + 2 :]:
                if not row.startswith("|"):
                    break
                for cell in re.findall(r"`([^`]+)`", row.split("|")[1]):
                    spelled_out = IMAGE_REFERENCE.findall(cell)
                    if spelled_out:
                        tags.update(spelled_out)
                    elif cell.startswith(":"):
                        tags.add(cell[1:])
    return tags


def test_the_documented_image_tags_are_tags_the_workflow_creates(tmp_path: Path, plan_script: str) -> None:
    """`design-review.md §2-D15`/D17: a documented name that nothing produces.

    The expected set is read out of the plan script rather than written here: a second
    copy of the tag scheme is the defect this guards against (§2). The release leg is run
    with the version the package reports, so a guide that pins `-v<old version>` fails here
    instead of sending a reader to a tag no release made -- the versioned halves of
    `docs/deployment.md` and `SECURITY.md` are the ones a bump forgets. The unsuffixed names
    a release moves (`<engine>`, `<engine>-runtime`) and `latest` are created by the merge
    job from the plan's `alias_tag`/`promote_latest`, not by the matrix, so both are read
    from the plan as well.
    """
    from decis import __version__

    version = f"v{__version__}"
    produced: set[str] = set()
    for event, ref, name in (
        ("push", "refs/heads/master", "master"),
        ("push", f"refs/tags/{version}", version),
    ):
        directory = tmp_path / name
        directory.mkdir()
        planned = run_plan(plan_script, directory, EVENT=event, REF=ref, REF_NAME=name)
        produced |= {build["image_tag"] for build in planned["builds"]}
        produced |= {merge["alias_tag"] for merge in planned["merges"] if merge["alias_tag"]}
        if any(merge["promote_latest"] for merge in planned["merges"]):
            produced.add("latest")
        # The playground is published by its own job, but under a tag computed here, so it
        # is part of "what the workflow creates" just as the engine tags are.
        produced.add(planned["playground_tag"])

    for doc in (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "deployment.md",
        ROOT / "docs" / "deployment.zh-CN.md",
    ):
        tags = documented_tags(doc)
        assert tags, f"{doc.name} documents no image at all -- did the table change shape?"
        for tag in sorted(tags):
            assert tag in produced, (
                f"{doc.name} tells the user to pull {tag!r}, which no event in the workflow creates "
                f"(it creates {sorted(produced)})"
            )
