"""The vendored kev copy is pinned by content, and nothing may quietly edit it.

`src/decis/engines/_kev_vendor/` holds byte-identical copies of two files from
https://github.com/jaredpalmer/kev at commit `90990a5`. The whole point of copying
rather than depending on a package is that the model Decis runs is fixed and
inspectable -- which is only true if the bytes stay what they were.

So the sha256 of each file is recorded in `VENDOR.md` and recomputed here. If these
fail, someone edited third-party code in place. The fix is to re-vendor at a new
commit (VENDOR.md documents the procedure) and update VENDOR.md and NOTICE together,
**not** to update the expected hashes below to match a local edit.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

VENDOR_DIR = Path(__file__).resolve().parent.parent / "src" / "decis" / "engines" / "_kev_vendor"

# (file, sha256, upstream path) -- pinned to kev commit 90990a5.
PINNED = (
    ("model.py", "c743c26e20fe8550e28b0697cb123173ee24c83e1b387830bff9f0b178900d55", "kev/model.py"),
    ("checkpoint.py", "9cd2ef04632ee84340c07cf95e8c80f999f153f4d896c46a4b74dbd1fb0cb193", "kev/checkpoint.py"),
)


def test_the_vendored_directory_exists() -> None:
    assert VENDOR_DIR.is_dir(), f"missing {VENDOR_DIR}"


@pytest.mark.parametrize("name,expected,upstream", PINNED)
def test_each_vendored_file_matches_its_pinned_hash(name: str, expected: str, upstream: str) -> None:
    """Content, not existence: a one-word edit to third-party weights code must not pass."""
    path = VENDOR_DIR / name
    assert path.is_file(), f"{upstream} should be vendored as {path}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected, (
        f"{name} differs from the pinned copy of {upstream}.\n"
        f"  expected {expected}\n  actual   {actual}\n"
        "Do not edit vendored third-party code. Re-vendor at a new commit and update "
        "VENDOR.md + NOTICE instead (see VENDOR.md 'Re-vendoring')."
    )


def test_vendor_md_records_the_same_hashes() -> None:
    """VENDOR.md is the human-readable half of the pin; the two must not drift apart."""
    text = (VENDOR_DIR / "VENDOR.md").read_text(encoding="utf-8")
    for name, expected, _ in PINNED:
        assert expected in text, f"VENDOR.md does not record the sha256 of {name}"
    assert "90990a5" in text, "VENDOR.md does not record the pinned commit"


def test_the_apache_licence_travels_with_the_code() -> None:
    """Apache-2.0 section 4 requires the licence text to accompany a redistribution."""
    licence = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence
    assert "Version 2.0" in licence


def test_vendor_md_names_what_was_left_out() -> None:
    """The exclusions are a design decision (api.py owns nothing here), so they are pinned too."""
    text = (VENDOR_DIR / "VENDOR.md").read_text(encoding="utf-8")
    for excluded in ("kev/api.py", "kev/serve.py"):
        assert excluded in text, f"VENDOR.md should say why {excluded} is not vendored"


def test_nothing_imports_upstream_kev() -> None:
    """The engine must run from the vendored copy, not a `kev` install that may not exist.

    A stray `import kev` would work on a developer machine that happens to have the
    research repo on its path and fail in a container -- the exact split AGENTS.md §7
    warns about. `_kev_vendor` is not a match: the pattern requires the name to be
    exactly `kev`.
    """
    pattern = re.compile(r"^\s*(?:from|import)\s+kev\b", re.MULTILINE)
    offenders = []
    for path in sorted((VENDOR_DIR.parent.parent.parent).rglob("*.py")):
        # The vendored files themselves are checked too: checkpoint.py's
        # `from .model import ...` is relative and stays inside the copy.
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, f"modules import the upstream `kev` package instead of the vendored copy: {offenders}"
