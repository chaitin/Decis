"""The Docker workflow decides what to publish. Test that decision, not the YAML.

`docker-build.yml` generates its own build matrix in bash, from the event name, the ref
and the dispatch inputs. That logic is where a release goes wrong: it can silently build
nothing, publish a moving tag from a feature branch, or ask the Dockerfile to bake weights
for an engine that has none. It cannot be exercised by running the workflow, so it is
exercised here -- the `plan` step's script is extracted from the YAML and executed with the
environment GitHub would provide.

Everything asserted here is a property that would otherwise only be discovered during a
release:

* a version tag builds both variants, a branch push does not, and a PR builds neither;
* the moving tag is `latest` only from the default branch, never from a pull request;
* `DECIS_PREDOWNLOAD` is never set to an engine that has no weights -- `decis download
  --engine mock` exits 2 *by design*, so that would fail the build;
* every engine in the matrix is a registered engine, so a rename cannot leave a stale
  matrix behind;
* the Dockerfile's `DECIS_EXTRAS` mapping matches what the registry declares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docker-build.yml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the workflow; it is not a runtime dependency")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plan_script(workflow: dict) -> str:
    for step in workflow["jobs"]["plan"]["steps"]:
        if step.get("id") == "plan":
            return step["run"]
    pytest.fail("the plan job has no step with id 'plan'")


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
        **env,
    }
    completed = subprocess.run(
        ["bash", "-c", plan_script],
        env=environment,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
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
    assert engines == {"mock", "laya-multilingual", "kev-0.8b"}, engines
    arches = {build["arch"] for build in push_to_master["builds"]}
    assert arches == {"amd64", "arm64"}, arches
    assert push_to_master["push"] == "true"
    assert push_to_master["tag"] == "latest"


def test_a_branch_push_does_not_bake_weights(push_to_master: dict) -> None:
    """An offline image is large and needs no rebuilding on every commit."""
    assert all(build["variant"] == "runtime" for build in push_to_master["builds"])
    assert all(build["bake"] == "" for build in push_to_master["builds"])


def test_a_version_tag_also_builds_the_offline_variants(tmp_path: Path, plan_script: str) -> None:
    plan = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v1.2.0", REF_NAME="v1.2.0")
    variants = {build["variant"] for build in plan["builds"]}
    assert variants == {"runtime", "offline"}, variants
    assert plan["tag"] == "v1.2.0"


def test_weights_are_never_baked_for_an_engine_that_has_none(tmp_path: Path, plan_script: str) -> None:
    """`DECIS_PREDOWNLOAD=mock` would fail the build: `decis download --engine mock` exits 2.

    That exit code is deliberate (a download request for a weightless engine probably
    means a mistyped engine id), which turns an "offline mock" image from pointless into
    broken.
    """
    plan = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v9.9.9", REF_NAME="v9.9.9")
    for build in plan["builds"]:
        if build["engine"] == "mock":
            assert build["bake"] == "", "the workflow would ask the Dockerfile to download mock's weights"
    baked = {build["engine"] for build in plan["builds"] if build["bake"]}
    assert baked == {"laya-multilingual", "kev-0.8b"}, baked


def test_a_pull_request_builds_but_never_publishes(tmp_path: Path, plan_script: str) -> None:
    plan = run_plan(plan_script, tmp_path, EVENT="pull_request", REF="refs/pull/7/merge", REF_NAME="7/merge")
    assert plan["push"] == "false", "a pull request must not push to the registry"
    assert plan["builds"], "a PR must still build, or the Dockerfile is never checked"
    # The point is the Dockerfile, so one engine on one platform is enough.
    assert len(plan["builds"]) == 1, plan["builds"]
    assert plan["tag"].startswith("sha-"), plan["tag"]


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
        INPUT_ENGINES="mock kev-0.8b",
        INPUT_BAKE="true",
        INPUT_PUSH="true",
    )
    engines = {build["engine"] for build in plan["builds"]}
    assert engines == {"mock", "kev-0.8b"}, engines
    # mock has no weights, so it stays runtime-only even with baking on.
    assert {build["variant"] for build in plan["builds"]} == {"runtime", "offline"}
    assert all(build["bake"] == "" for build in plan["builds"] if build["engine"] == "mock")
    assert plan["push"] == "true"


def test_a_dispatch_dry_run_does_not_push(tmp_path: Path, plan_script: str) -> None:
    """The default has to be safe: a dispatch with no inputs must not publish."""
    plan = run_plan(
        plan_script,
        tmp_path,
        EVENT="workflow_dispatch",
        REF="refs/heads/master",
        REF_NAME="master",
        INPUT_ENGINES="mock",
        INPUT_BAKE="false",
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


def test_the_extra_mapping_matches_the_registry(push_to_master: dict) -> None:
    """`DECIS_EXTRAS` is chosen by a ternary in YAML; a new engine would silently get `laya`."""
    sys.path.insert(0, str(ROOT / "src"))
    from decis.engines.registry import SPECS

    # The workflow encodes this mapping in one expression; keep it in step with what the
    # registry declares, or an engine image installs the wrong dependencies.
    expected = {"mock": "", "laya-multilingual": "laya", "kev-0.8b": "kev"}
    for engine, extra in expected.items():
        assert engine in SPECS, f"{engine} is in the workflow but not the registry"
        assert SPECS[engine].extra == extra, f"{engine}: registry says {SPECS[engine].extra!r}, workflow says {extra!r}"

    # And every engine the registry can serve is either imaged or deliberately not.
    unimaged = set(SPECS) - set(expected)
    assert unimaged == {"laya", "laya-typed-decisions"}, (
        f"{sorted(unimaged)} are registered but have no image. Either add them to the "
        f"workflow's engine list or say here why they share an image."
    )


def test_the_workflow_passes_only_dockerfile_declared_build_args(push_to_master: dict) -> None:
    """An undeclared `--build-arg` is silently ignored by Docker, so it would do nothing."""
    declared = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARG "):
            declared.add(line.split()[1].split("=")[0])
    for name in ("DECIS_EXTRAS", "DECIS_ENGINE", "DECIS_PREDOWNLOAD"):
        assert name in declared, f"{name} is passed by the workflow but never declared in the Dockerfile"
