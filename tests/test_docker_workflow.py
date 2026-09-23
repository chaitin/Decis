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


def test_the_planned_extra_matches_the_registry(push_to_master: dict) -> None:
    """Each image must install exactly the dependencies its engine declares.

    Asserted against the *planned matrix*, not against the YAML text: the value used to
    come from `engine == 'mock' && '' || 'laya'`, and an empty string is falsy in a GitHub
    expression, so `mock` silently installed Laya and shipped 3.1 GB of torch. Only
    executing the step catches that.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from decis.engines.registry import SPECS

    planned = {build["engine"]: build["extra"] for build in push_to_master["builds"]}
    assert set(planned) == {"mock", "laya-multilingual", "kev-0.8b"}, sorted(planned)
    for engine, extra in planned.items():
        assert engine in SPECS, f"{engine} is in the workflow but not the registry"
        assert SPECS[engine].extra == extra, (
            f"{engine}: the registry declares extra {SPECS[engine].extra!r}, the workflow would install {extra!r}"
        )

    # `mock` is the one every user pulls first and the one the contract tests share; it
    # must not drag in a model runtime. This is the regression that shipped.
    assert planned["mock"] == "", f"the mock image would install {planned['mock']!r}"

    # And every engine the registry can serve is either imaged or deliberately not.
    unimaged = set(SPECS) - set(planned)
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
            "INPUT_BAKE": "false",
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
        "DOCKERHUB_USERNAME": "kingfs",
        **env,
    }
    return run_shell(script, tmp_path, environment)


def test_the_published_registry_is_docker_hub(workflow: dict) -> None:
    """The project publishes to exactly one registry, and it must be the documented one."""
    assert workflow["env"]["REGISTRY"] == "docker.io"
    assert workflow["env"]["IMAGE_REPO"] == "decis"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io" not in text, "a GHCR reference survived the move to Docker Hub"


@pytest.mark.parametrize(
    ("event", "ref", "ref_name", "expected"),
    [
        # On a branch push the engine name IS the tag, as if the repository held one model:
        # `docker pull kingfs/decis:laya-multilingual`. Nothing is appended, so the
        # documented tag is a tag that exists -- `:laya-multilingual` did not, the
        # workflow produced `:laya-multilingual-latest` while the README said otherwise.
        (
            "push",
            "refs/heads/master",
            "master",
            {"mock": "mock", "laya-multilingual": "laya-multilingual"},
        ),
        # A release tag pins, so it must say which release.
        (
            "push",
            "refs/tags/v1.2.0",
            "v1.2.0",
            {"mock": "mock-v1.2.0", "laya-multilingual": "laya-multilingual-v1.2.0"},
        ),
        # A feature branch never moves a name someone could have pinned to.
        (
            "push",
            "refs/heads/topic",
            "topic",
            {"mock": "mock-sha-a1b2c3d", "laya-multilingual": "laya-multilingual-sha-a1b2c3d"},
        ),
    ],
)
def test_the_image_tag_is_the_engine_name_on_a_branch_push(
    tmp_path: Path, plan_script: str, event: str, ref: str, ref_name: str, expected: dict[str, str]
) -> None:
    """The tag a user types is decided by the plan script, so it is tested there."""
    planned = run_plan(plan_script, tmp_path, EVENT=event, REF=ref, REF_NAME=ref_name)
    tags = {b["engine"]: b["image_tag"] for b in planned["builds"] if b["variant"] == "runtime"}
    for engine, tag in expected.items():
        assert tags.get(engine) == tag, f"{engine} on {ref}: expected {tag!r}, got {tags.get(engine)!r}"


def test_the_offline_variant_is_a_distinct_tag(tmp_path: Path, plan_script: str) -> None:
    """Baked weights are a different artifact, so they must not share a tag."""
    planned = run_plan(plan_script, tmp_path, EVENT="push", REF="refs/tags/v1.2.0", REF_NAME="v1.2.0")
    tags = {(b["engine"], b["variant"]): b["image_tag"] for b in planned["builds"]}
    assert tags[("laya-multilingual", "runtime")] == "laya-multilingual-v1.2.0", tags
    assert tags[("laya-multilingual", "offline")] == "laya-multilingual-offline-v1.2.0", tags


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
        ("mock", "runtime"),
        ("laya-multilingual", "runtime"),
        ("laya-multilingual", "offline"),
        ("kev-0.8b", "runtime"),
        ("kev-0.8b", "offline"),
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
                IMAGE_TAG=f"{engine}-{variant}" if variant != "runtime" else engine,
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
    """`kingfs/decis:laya-multilingual`, not `kingfs/decis-laya-multilingual`."""
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
    assert outputs["image"] == "docker.io/kingfs/decis", outputs
    tags = outputs["tags"].splitlines()
    assert "docker.io/kingfs/decis:laya-multilingual-amd64" in tags, tags
    # The per-arch provenance tag the merge job consumes must name the artifact as well as
    # the platform.
    assert "docker.io/kingfs/decis:laya-multilingual-sha-a1b2c3d4e5f6-amd64" in tags, tags


def test_the_namespace_comes_from_the_secret_not_the_repository_owner(build_meta_script: str, tmp_path: Path) -> None:
    """A fork's owner is not a Docker Hub namespace, so it must not decide the image name."""
    docker_bin, _ = fake_docker(tmp_path)
    completed = run_step(
        build_meta_script,
        tmp_path,
        docker_bin,
        ENGINE="mock",
        VARIANT="runtime",
        ARCH="amd64",
        IMAGE_TAG="mock",
        # A fork would see its own owner here; the image name must ignore it.
        GITHUB_REPOSITORY_OWNER="someone-else",
        DOCKERHUB_USERNAME="KingFS",  # registries require lowercase
        GITHUB_OUTPUT=str(tmp_path / "out"),
    )
    assert completed.returncode == 0, completed.stderr
    assert parse_outputs(tmp_path / "out")["image"] == "docker.io/kingfs/decis"


def merged_tags(log: Path) -> list[str]:
    """Every tag the merge step asked `docker` to create from the per-arch images."""
    tags = []
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[:3] == ["buildx", "imagetools", "create"]:
            tags.append(parts[parts.index("--tag") + 1])
    return tags


@pytest.mark.parametrize(
    ("engine", "variant", "image_tag", "moving_tag", "expected_latest"),
    [
        # `mock` needs no weights and no GPU, so it is the right answer to a bare
        # `docker pull kingfs/decis`.
        ("mock", "runtime", "mock", "latest", True),
        # ...but only the image that actually moved `latest`. A version tag must not
        # silently repoint the bare tag, and an offline image is not the small default.
        ("mock", "runtime", "mock-v1.2.0", "v1.2.0", False),
        ("mock", "offline", "mock-offline", "latest", False),
        ("laya-multilingual", "runtime", "laya-multilingual", "latest", False),
        ("kev-0.8b", "runtime", "kev-0.8b", "latest", False),
    ],
)
def test_only_mock_claims_the_bare_latest_tag(
    merge_script: str,
    tmp_path: Path,
    engine: str,
    variant: str,
    image_tag: str,
    moving_tag: str,
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
        MOVING_TAG=moving_tag,
    )
    assert completed.returncode == 0, completed.stderr
    tags = merged_tags(log)
    assert f"docker.io/kingfs/decis:{image_tag}" in tags, tags
    assert ("docker.io/kingfs/decis:latest" in tags) is expected_latest, tags


def test_the_merge_uses_the_per_arch_images_of_this_commit(merge_script: str, tmp_path: Path) -> None:
    """Every source must be this commit's per-arch manifest, or a release mixes commits."""
    docker_bin, log = fake_docker(tmp_path)
    completed = run_step(
        merge_script,
        tmp_path,
        docker_bin,
        ENGINE="laya-multilingual",
        VARIANT="runtime",
        IMAGE_TAG="laya-multilingual",
        MOVING_TAG="latest",
    )
    assert completed.returncode == 0, completed.stderr
    create = [line for line in log.read_text(encoding="utf-8").splitlines() if "imagetools create" in line]
    assert len(create) == 1, create
    words = create[0].split()
    # Drop the `--tag <ref>` pair: its value is a destination, not a source.
    skip = {words.index("--tag") + 1} if "--tag" in words else set()
    sources = [word for i, word in enumerate(words) if word.startswith("docker.io/") and i not in skip]
    assert sources == [
        "docker.io/kingfs/decis:laya-multilingual-sha-a1b2c3d4e5f6-amd64",
        "docker.io/kingfs/decis:laya-multilingual-sha-a1b2c3d4e5f6-arm64",
    ], sources


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
        ENGINE="mock",
        VARIANT="runtime",
        IMAGE_TAG="mock",
        MOVING_TAG="latest",
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
        ENGINE="mock",
        VARIANT="runtime",
        IMAGE_TAG="mock",
        MOVING_TAG="latest",
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
            "INPUT_ENGINES": "mock",
            "INPUT_BAKE": "false",
            "INPUT_PUSH": "false",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "push=false" in output.read_text(encoding="utf-8")
