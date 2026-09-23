"""The Makefile is a convenience layer, not a second definition of anything.

`make` must not become the place where an image tag, an engine id or a build argument is
written down again: `docker-compose.yml`, `docker-compose.override.yml` and `.env` already
define those, and a copy in a fourth file is a copy that drifts (`AGENTS.md` §2, §9). So the
targets all delegate to Compose, and these tests check that they still do -- and then run
`make` itself, through a fake `docker`, so they need neither a daemon nor a network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
COMPOSE = ROOT / "docker-compose.yml"
OVERRIDE = ROOT / "docker-compose.override.yml"
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"
AGENTS = ROOT / "AGENTS.md"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="no make on this machine")

#: `target: ## what it does` -- the shape the `help` target itself greps for, so a target not
#: in this shape is a target nobody can find.
DOCUMENTED = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):.*?## (.+)$", re.MULTILINE)

#: Terminology that belongs to the Compose files and the workflow; none of it may appear in
#: the Makefile. The engine ids are added from the compose file rather than written here (§9:
#: a hand-copied list can be right while the thing it copies is wrong).
FOREIGN = ("kingfs/", "decis-local", "DECIS_EXTRAS", "DECIS_PREDOWNLOAD", "DECIS_ENGINE")

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def engines() -> set[str]:
    """The engine ids, read out of the compose file's profiles."""
    return set(re.findall(r"^\s*profiles: \[([a-z0-9.-]+)\]$", COMPOSE.read_text(encoding="utf-8"), re.MULTILINE))


def documented() -> dict[str, str]:
    return dict(DOCUMENTED.findall(makefile_text()))


def phony() -> set[str]:
    """The names in the `.PHONY` list, which may be continued across lines with `\\`."""
    lines = makefile_text().splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(".PHONY:")), None)
    assert start is not None, "the Makefile has no .PHONY line, so a file named `build` would shadow a target"
    words: list[str] = []
    for line in lines[start:]:
        words += line.rstrip("\\").split()
        if not line.rstrip().endswith("\\"):
            break
    return {word for word in words if word != ".PHONY:"}


@pytest.fixture(scope="module")
def docker(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A `docker` that answers the questions the Makefile asks Compose.

    `config --services` is how the Makefile learns which engine this checkout resolves to.
    The answer comes from `FAKE_ENGINE`, so a test can make it say anything -- including
    nothing, which is what a checkout with no `.env` looks like.
    """
    bin_dir = tmp_path_factory.mktemp("fake-docker")
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"config --services"*) printf \'%s\\n\' "${FAKE_ENGINE-engine-default}" playground ;;\n'
        "  *\"config --profiles\"*) printf '%s\\n' engine-default engine-other ;;\n"
        "  *\"config --images\"*) printf '%s\\n' image-default image-playground ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def run_make(
    *args: str, docker: Path | None = None, path: str | None = None, **env: str
) -> subprocess.CompletedProcess:
    """Run `make` in this checkout. Without `-n` a recipe still only reaches the fake docker."""
    environment = dict(os.environ, PATH=path if path is not None else f"{docker}:{os.environ['PATH']}", **env)
    return subprocess.run(
        ["make", "--no-print-directory", *args], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
    )


# --- the targets exist, and the help lists them -----------------------------------------


def test_every_documented_target_is_a_target_and_dry_runs(docker: Path) -> None:
    """`make -n` on each one: the recipe parses and needs nothing but Compose."""
    assert documented(), "the Makefile documents no targets"
    for name in documented():
        result = run_make("-n", name, docker=docker, ENGINE="engine-default")
        assert result.returncode == 0, f"make -n {name} failed:\n{result.stdout}{result.stderr}"


def test_the_phony_list_and_the_documented_list_are_the_same(docker: Path) -> None:
    """Every target is phony -- none of these names a file -- and every one is documented."""
    assert phony() == set(documented()), (
        f"undocumented: {sorted(phony() - set(documented()))}, "
        f"documented but not phony: {sorted(set(documented()) - phony())}"
    )


def test_the_help_lists_every_target(docker: Path) -> None:
    result = run_make("help", docker=docker)
    assert result.returncode == 0, result.stderr
    listed = {ANSI.sub("", line).split()[0] for line in result.stdout.splitlines() if line.startswith("  ")}
    assert listed == set(documented()), f"help lists {sorted(listed)}, targets are {sorted(documented())}"


def test_help_works_with_no_docker_at_all(tmp_path: Path) -> None:
    """Asking what the targets are must not depend on the daemon being up."""
    bare = tmp_path / "bare-bin"
    bare.mkdir()
    for tool in ("sh", "bash", "grep", "awk", "printf", "env", "make"):
        found = shutil.which(tool)
        if found:
            (bare / tool).symlink_to(found)
    result = run_make("help", path=str(bare))
    assert result.returncode == 0, f"make help without docker:\n{result.stdout}{result.stderr}"
    assert "build-playground" in result.stdout


def test_only_pull_uses_the_deployment_file(docker: Path) -> None:
    """`pull` has to name the base file, and everything else must not.

    The override retags the images `decis-local:*`, which are built here and exist in no
    registry: `docker compose pull` in a checkout asks a registry for those and fails. The
    converse matters too -- a build or an `up` that skipped the override would quietly run a
    published image instead of this checkout's source.
    """
    recipes = [line.strip() for line in makefile_text().splitlines() if line.startswith("\t") and "$(COMPOSE)" in line]
    assert recipes, "no recipe invokes Compose any more"
    for recipe in recipes:
        if "-f docker-compose.yml" in recipe:
            assert "pull" in recipe, f"only pull may bypass the override: {recipe}"
        else:
            assert "pull" not in recipe, f"pull must name the deployment file: {recipe}"


def test_up_waits_for_the_model(docker: Path) -> None:
    """`--wait` is the point of the target: it returns when the engine can answer, not when
    the container started (`design-review.md` §2-D22)."""
    up = run_make("-n", "up", docker=docker)
    assert "docker compose up -d --wait" in up.stdout, up.stdout
    local = run_make("-n", "up-local", docker=docker)
    assert "docker compose up -d --wait --build" in local.stdout, local.stdout


def test_images_reports_what_compose_reports(docker: Path) -> None:
    """So `make images` answers "local or published?" without the Makefile naming either."""
    result = run_make("images", docker=docker)
    assert result.returncode == 0, result.stderr
    assert "image-playground" in result.stdout, result.stdout


# --- nothing here is a second copy of the compose files ---------------------------------


def test_the_makefile_names_no_image_tag_no_engine_and_no_build_argument() -> None:
    """The guard for the whole design: those names live in the Compose files, not here.

    A tag written here would be one the workflow publishes while the Makefile builds
    something else, or the other way round -- `design-review.md` §2-D15 is the time that
    shipped.
    """
    text = makefile_text()
    found = sorted(name for name in engines() if name in text)
    assert not found, f"the Makefile names the engine(s) {found}; it must ask Compose instead"
    leaked = [word for word in FOREIGN if word in text]
    assert not leaked, f"the Makefile repeats what the Compose files own: {leaked}"


def test_the_makefile_asks_compose_which_engine_rather_than_reading_dot_env() -> None:
    """`.env` is Compose's file, and Compose lets the shell win over it. Make does not.

    A Makefile that parsed `.env` itself would disagree with the Compose command it wraps the
    moment someone exports `COMPOSE_PROFILES`.
    """
    text = makefile_text()
    assert "config --services" in text, "the engine is no longer asked of Compose"
    assert not re.search(r"^\s*-?include\s+\.env", text, re.MULTILINE), "the Makefile parses .env itself"


def test_the_engine_the_targets_act_on_is_whatever_compose_reports(docker: Path) -> None:
    for engine in ("engine-one", "engine-two"):
        result = run_make("-n", "build-engine", "up-engine", docker=docker, FAKE_ENGINE=engine)
        assert result.returncode == 0, result.stderr
        assert f"docker compose build {engine}" in result.stdout, result.stdout
        assert f"docker compose up -d --wait {engine}" in result.stdout, result.stdout


def test_the_engine_can_be_named_on_the_command_line(docker: Path) -> None:
    result = run_make("-n", "build-engine", docker=docker, FAKE_ENGINE="engine-one", ENGINE="engine-other")
    assert "docker compose build engine-other" in result.stdout, result.stdout
    assert "engine-one" not in result.stdout, "the command line did not win"


def test_no_engine_selected_is_an_error_that_says_what_to_do(docker: Path) -> None:
    """A checkout with no `.env` has no engine, and building an arbitrary one would be a guess."""
    result = run_make("build-engine", docker=docker, FAKE_ENGINE="")
    assert result.returncode != 0
    assert "no engine selected" in result.stdout + result.stderr


# --- what the docs tell people to run ---------------------------------------------------


#: A target named in prose (`make up`) or in a shell block (a line that is the command, or a
#: comment above it). Prose like "make sure" never matches: both forms require the word to be
#: the command of a line or a backticked span.
INLINE_COMMAND = re.compile(r"`make ([a-z][a-z0-9-]*)")
BLOCK_COMMAND = re.compile(r"^\s*(?:\$ |# )?make ([a-z][a-z0-9-]*)", re.MULTILINE)


@pytest.mark.parametrize(
    "readme",
    [
        README,
        README_ZH,
        AGENTS,
        *sorted((ROOT / "docs").glob("*.md")),
        *sorted((ROOT / "docs" / "schema").glob("*.md")),
    ],
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_the_documented_make_commands_are_real_targets(readme: Path) -> None:
    """A `make something` in the docs that is not a target is a command that cannot run.

    Every markdown file a reader might follow is scanned, not just the READMEs: the guides
    are where the deployment commands now live, so a target renamed in the Makefile and
    missed in `docs/deployment.md` would otherwise ship.
    """
    known = set(documented())
    text = readme.read_text(encoding="utf-8")
    for command in INLINE_COMMAND.findall(text) + BLOCK_COMMAND.findall(text):
        assert command in known, f"{readme.name} tells the reader to run `make {command}`"


def test_the_readme_documents_the_workflow_the_targets_implement() -> None:
    """The lines a reader needs: the target list, rebuild the pages, start what is here."""
    text = README.read_text(encoding="utf-8")
    documented_commands = set(INLINE_COMMAND.findall(text)) | set(BLOCK_COMMAND.findall(text))
    assert {"help", "build-playground", "up-playground", "up"} <= documented_commands, sorted(documented_commands)
