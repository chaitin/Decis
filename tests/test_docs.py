"""Documentation is part of the product, and both of its languages are part of the contract.

The guides are bilingual, which means every one of them exists twice. Two hand-maintained
copies drift; the point of these tests is to make the drift loud instead of gradual:

* every user-facing guide has a Chinese twin, and the two link to each other;
* they keep the same **structure** -- the same relative links, the same `DECIS_*` variables,
  the same fenced-block languages, the same generated markers -- because those are the parts
  that are not translation and must not quietly disappear in one language;
* every relative link in every markdown file resolves to a file that exists, so a moved
  document is caught here rather than by a reader;
* the README links every guide, so a new page cannot be orphaned.

What is *not* checked is prose. A translation is a translation; a test cannot tell a good
sentence from a bad one, and pretending otherwise would only make the check easy to game.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

#: Internal design documents. They are written in Chinese for the maintainers and are not
#: part of the reader-facing guide set, so they have no English twin to keep in step.
INTERNAL = {"api-compatibility.md", "design.md", "design-review.md", "feasibility.md"}

#: The guides a reader is expected to follow, for which both languages must exist.
GUIDES = [
    "getting-started.md",
    "configuration.md",
    "deployment.md",
    "api.md",
    "engines.md",
    "playground.md",
    "performance.md",
]

#: Files whose relative links are checked. The contract directory holds JSON evidence, not
#: prose, so it is out of scope.
MARKDOWN = [
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CODE_OF_CONDUCT.md",
    ROOT / "AGENTS.md",
    *sorted(DOCS.glob("*.md")),
    *sorted((ROOT / "examples").glob("*.md")),
]

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_REFERENCE = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
_FENCE = re.compile(r"^```([^\n`]*)$", re.MULTILINE)
_MARKER = re.compile(r"<!--\s*([A-Z_]+):(START|END)\s*-->")
_ENV = re.compile(r"\bDECIS_[A-Z0-9_]+\b")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_EXTERNAL = ("http://", "https://", "mailto:", "tel:")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_code(text: str) -> str:
    """Drop fenced code blocks.

    A link inside an example is not a link the reader can follow -- the curl cookbook and
    the Dockerfile snippets are full of them -- and a fenced block's first tokens look like
    a reference definition to the regex below.
    """
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def _targets(text: str) -> set[str]:
    """Every relative link target, with any anchor stripped."""
    found: set[str] = set()
    for raw in _LINK.findall(text) + _REFERENCE.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(_EXTERNAL) or "://" in target:
            continue
        found.add(target)
    return found


def _shared_targets(text: str) -> set[str]:
    """Link targets with the language suffix folded away.

    The switcher line is the one place where the two files must differ: the English file
    links `api.zh-CN.md` and the Chinese one links `api.md`. Normalising `.zh-CN` out
    removes exactly that pair and leaves every content link -- the ones a translation has
    no business touching -- to be compared for real.
    """
    return {target.replace(".zh-CN.md", ".md") for target in _targets(text)}


def _env_names(text: str) -> set[str]:
    return set(_ENV.findall(text))


def _fence_languages(text: str) -> set[str]:
    return {language.strip() for language in _FENCE.findall(text) if language.strip()}


def _markers(text: str) -> set[str]:
    return {f"{name}:{kind}" for name, kind in _MARKER.findall(text)}


def _slug(heading: str) -> str:
    """GitHub's heading anchor, near enough: lower-case, punctuation dropped, spaces hyphenated."""
    heading = re.sub(r"`([^`]*)`", r"\1", heading)
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"[*~]", "", heading)
    kept = "".join(ch for ch in heading.lower() if ch.isalnum() or ch in " -_")
    return kept.strip().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(heading) for heading in _HEADING.findall(_text(path))}


def _section(text: str, title: str) -> str:
    """The body of the `## <title>` section, up to the next level-2 heading."""
    match = re.search(rf"^## {re.escape(title)}$\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no `## {title}` section"
    return match.group(1)


# --- every guide exists twice, and the copies stay aligned ---------------------


def test_every_user_facing_doc_is_bilingual() -> None:
    """A guide without a Chinese twin is a guide half the readers cannot follow."""
    missing = [
        name
        for name in sorted(path.name for path in DOCS.glob("*.md"))
        if name not in INTERNAL
        and not name.endswith(".zh-CN.md")
        and not (DOCS / name.replace(".md", ".zh-CN.md")).is_file()
    ]
    assert not missing, f"these docs have no .zh-CN.md twin: {missing}"


@pytest.mark.parametrize("name", GUIDES)
def test_the_language_switcher_works_both_ways(name: str) -> None:
    """Each copy links to the other, or the pair is one-way and effectively hidden."""
    english = DOCS / name
    chinese = DOCS / name.replace(".md", ".zh-CN.md")
    assert english.is_file() and chinese.is_file(), name
    assert chinese.name in _text(english), f"{name} does not link to its Chinese twin"
    assert english.name in _text(chinese), f"{chinese.name} does not link back to {name}"


@pytest.mark.parametrize("name", GUIDES)
def test_the_two_languages_keep_the_same_structure(name: str) -> None:
    """The parts that are not prose must survive translation.

    A relative link, a `DECIS_*` variable, a fenced-block language and a generated marker
    are all things a translation has no business changing. If one of them is missing on the
    Chinese side, either the translation dropped a real instruction or the English side grew
    one -- both worth failing over.
    """
    english = _text(DOCS / name)
    chinese = _text(DOCS / name.replace(".md", ".zh-CN.md"))

    assert _shared_targets(english) == _shared_targets(chinese), f"{name}: relative links differ"
    assert _env_names(english) == _env_names(chinese), f"{name}: DECIS_* variables differ"
    assert _fence_languages(english) == _fence_languages(chinese), f"{name}: code-block languages differ"
    assert _markers(english) == _markers(chinese), f"{name}: generated markers differ"


@pytest.mark.parametrize("name", GUIDES)
def test_the_two_languages_are_both_substantial(name: str) -> None:
    """A stub translation is worse than none: it looks finished."""
    english = _text(DOCS / name)
    chinese = _text(DOCS / name.replace(".md", ".zh-CN.md"))
    assert len(english.splitlines()) > 20, name
    # Chinese is denser than English, so this is a floor rather than a ratio.
    assert len(chinese.splitlines()) > len(english.splitlines()) * 0.5, f"{name} reads like a stub"


# --- links resolve -------------------------------------------------------------


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_relative_link_resolves(path: Path) -> None:
    """A moved document leaves a link that 404s, which no other test would notice."""
    broken: list[str] = []
    for target in sorted(_targets(_without_code(_text(path)))):
        if not (path.parent / target).exists():
            broken.append(target)
    assert not broken, f"{path.relative_to(ROOT)} links to files that do not exist: {broken}"


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_anchor_points_at_a_heading(path: Path) -> None:
    """The file check above stops at the `#`: a renamed heading breaks the fragment silently.

    Several links aim at the middle of a document (`playground.md#credits`,
    `design-review.md#4-m5`), and a reader who lands at the top of the file cannot tell that
    anything is wrong. `docs/configuration.md` pointed at `performance.md#latency-by-engine-
    and-device` for a while; the heading is `## Latency`.
    """
    broken: list[str] = []
    for raw in _LINK.findall(_without_code(_text(path))):
        if "#" not in raw or "://" in raw:
            continue
        target, fragment = raw.split("#", 1)
        resolved = path if not target else (path.parent / target).resolve()
        if not fragment or resolved.suffix != ".md" or not resolved.is_file():
            continue
        if fragment not in _anchors(resolved):
            broken.append(raw)
    assert not broken, f"{path.relative_to(ROOT)} links to headings that do not exist: {broken}"


# --- the README is an index, and the API page points at the schemas ------------


def test_the_readme_links_every_guide() -> None:
    """A guide nothing links to is a guide nobody reads."""
    readme = _text(ROOT / "README.md")
    missing = [name for name in GUIDES if f"docs/{name}" not in readme]
    assert not missing, f"README.md does not link: {missing}"


def test_the_readme_links_the_community_files() -> None:
    readme = _text(ROOT / "README.md")
    for name in ("CONTRIBUTING.md", "SECURITY.md", "NOTICE", "LICENSE", "AGENTS.md"):
        assert f"]({name})" in readme, f"README.md does not link {name}"


def test_the_api_page_covers_the_generated_schemas() -> None:
    """The reference and the schemas are two views of one contract; they must agree."""
    text = _text(DOCS / "api.md")
    for filename in ("systemone-request.schema.json", "systemone-response.schema.json", "openapi.json"):
        assert filename in text, f"docs/api.md does not mention {filename}"
    for endpoint in ("/v1/systemone", "/v1/models", "/healthz", "/readyz"):
        assert endpoint in text, f"docs/api.md does not document {endpoint}"
    for primitive in ("choice", "score", "noul"):
        assert primitive in text, f"docs/api.md does not document the {primitive} primitive"
    assert "export.py" in text, "docs/api.md does not say how the schemas are regenerated"


def test_the_documented_healthz_version_is_the_reported_one() -> None:
    """`docs/api.md` prints a `/healthz` body, and `/healthz` prints the package version.

    Two copies of one number, so a release that bumps the version has to move both or the
    page shows a reader a version the server does not report. `decis.__version__` itself is
    tied to `pyproject.toml` by `tests/test_conventions.py`.
    """
    import re

    from decis import __version__

    for name in ("api.md", "api.zh-CN.md"):
        text = _text(DOCS / name)
        documented = re.findall(r'\{"status": "ok", "version": "([^"]+)"\}', text)
        assert documented == [__version__], (
            f"docs/{name} documents /healthz returning {documented}, but the server reports {__version__}"
        )


def test_the_release_version_the_docs_quote_is_the_packaged_one() -> None:
    """A versioned tag the guides print has to be the version this release produces.

    `docs/deployment.md` and `SECURITY.md` name a `-v<version>` image tag, and both READMEs
    announce the same version as their status, so a bump leaves four hand-written copies
    behind -- and a stale one names a tag no release ever made (the `§9`/D15 shape, with a
    date attached). The `v` prefix is what separates our release from the upstream OpenAPI
    snapshot's `0.2.0`, which is a document version and carries no `v`.
    """
    from decis import __version__

    version = f"v{__version__}"
    quoted = re.compile(r"\bv\d+\.\d+\.\d+\b")
    for path in (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "SECURITY.md",
        DOCS / "deployment.md",
        DOCS / "deployment.zh-CN.md",
    ):
        found = sorted(set(quoted.findall(_text(path))))
        assert found == [version], f"{path.name} quotes {found or 'no release version'}, the package reports {version}"


def test_the_readmes_name_exactly_the_registered_engines() -> None:
    """The README names the engines, so those names have to be the registry's.

    That section was a hand-copied copy of the `docs/engines.md` table until `§9` caught it
    drifting -- the two disagreed about `laya-typed-decisions` -- and it is now a sentence.
    Prose drifts more quietly than a table, so the comparison is exact: an engine id is the
    only thing either section puts in backticks.
    """
    from decis.engines.registry import SPECS

    # `tests/conftest.py` registers its own deterministic engine as `stub` while the suite
    # runs. It is deliberately not a shipped engine, so it is not one a README should name.
    registered = {name for name, spec in SPECS.items() if not spec.target.startswith("fixture_engine")}
    for name, title in (("README.md", "Engines"), ("README.zh-CN.md", "引擎")):
        named = set(re.findall(r"`([^`\n]+)`", _section(_text(ROOT / name), title)))
        assert named == registered, f"{name} names {sorted(named)} as engines, the registry has {sorted(registered)}"


def test_the_documented_environment_variables_are_read_by_something() -> None:
    """A documented knob that nothing reads is worse than an undocumented one.

    `configuration.md` is the page a reader trusts to tell them what to set, so every name it
    prints has to exist in something that consumes it. `DECIS_HOST_PORT` and the
    `DECIS_PLAYGROUND_*` variables are Compose-only and never reach `config.py`, which is why
    this looks at the Compose files and the playground as well as the package. `.env.example`
    is deliberately not consulted: it is a second copy of this page, so a name that appears
    only there is read by nothing at all.
    """
    documented = _env_names(_text(DOCS / "configuration.md"))
    sources = [
        *(ROOT / "src" / "decis").rglob("*.py"),
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.override.yml",
        ROOT / "playground" / "server.py",
    ]
    known = "\n".join(_text(path) for path in sources if path.is_file())
    invented = sorted(name for name in documented if name not in known)
    assert not invented, f"docs/configuration.md documents variables nothing reads: {invented}"


def test_every_documented_per_engine_override_names_a_registered_engine() -> None:
    """`DECIS_MODEL_PATH_<ID>` is the engine id upper-cased, so a dot cannot be encoded.

    The variable for `kev-0.8b` would normalise to the id `kev-0-8b`, a name no engine
    registers, and `config.py` would drop it without a word. A documented override that
    silently does nothing is therefore a real defect, and this is what catches it:
    `docs/configuration.md` explains the `kev-0.8b` case in prose instead of printing a name
    for it.
    """
    from decis.engines.registry import SPECS

    # Reader-facing pages only: `design-review.md` names the broken variable on purpose, as
    # the record of the defect.
    reader_facing = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        *[DOCS / name for name in GUIDES],
        *[DOCS / name.replace(".md", ".zh-CN.md") for name in GUIDES],
    ]
    pattern = re.compile(r"\bDECIS_MODEL_PATH_([A-Z0-9_]+)")
    documented = {
        match.group(1)
        for path in reader_facing
        for match in pattern.finditer(_without_code(_text(path)))
        if match.group(1)
    }
    assert documented, "no concrete DECIS_MODEL_PATH_* variable is documented anywhere"
    unusable = sorted(name for name in documented if name.lower().replace("_", "-") not in SPECS)
    assert not unusable, (
        "these DECIS_MODEL_PATH_* names normalise to an engine id that is not registered, so "
        f"setting them would be silently ignored: {unusable}"
    )


def test_the_changelog_is_keep_a_changelog_shaped() -> None:
    """Enough structure that a release notes tool, and a reader, can follow it."""
    text = _text(ROOT / "CHANGELOG.md")
    assert text.startswith("# Changelog")
    assert "Keep a Changelog" in text
    assert "## [Unreleased]" in text
    assert re.search(r"^## \[0\.0\.1\] - \d{4}-\d{2}-\d{2}$", text, re.MULTILINE), "no dated 0.0.1 section"


def test_the_package_version_matches_the_changelog() -> None:
    """The version bump and the changelog entry are one change, not two.

    `tests/test_conventions.py` already ties `decis.__version__` to `pyproject.toml`; this
    adds the human-facing record, which is the one people forget.
    """
    from decis import __version__

    changelog = _text(ROOT / "CHANGELOG.md")
    if f"## [{__version__}]" not in changelog:
        assert "## [Unreleased]" in changelog, f"neither [{__version__}] nor [Unreleased] is in CHANGELOG.md"
