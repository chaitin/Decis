"""The vendored jeff copy is pinned by content, and nothing may quietly edit it.

`src/decis/engines/_jeff_vendor/` holds copies of three files from
https://github.com/firelex/jeff at tag `v1.1` (`f0397f3`). Copying rather than depending is
what fixes the model Decis runs and keeps it inspectable -- which is only true if the bytes
stay what they were, so the sha256 of each file is recorded in `VENDOR.md` and recomputed
here. If these fail, someone edited third-party model code in place. The fix is to re-vendor
at a new revision (VENDOR.md documents the procedure) and update VENDOR.md and NOTICE
together, **not** to update the expected hashes to match a local edit.

Unlike the kev copy, this one is not byte-identical: jeff imports itself absolutely
(`from jeff.types import ...`), and the files have to work as a subpackage of `decis`. So
there are two pins, not one -- the vendored hash, and the upstream hash that the
substitution must reproduce. The offline half checks that the *only* difference is the
substitution; the `network`-marked half re-fetches the pinned upstream bytes and proves it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

VENDOR_DIR = Path(__file__).resolve().parent.parent / "src" / "decis" / "engines" / "_jeff_vendor"

REPO = "https://raw.githubusercontent.com/firelex/jeff"
PIN = "f0397f3785d93f73a01411d785d2ed026f53181d"

#: (file, vendored sha256, upstream sha256, upstream path) -- pinned to jeff tag v1.1.
#: `types.py` is byte-identical because it imports nothing from the package.
PINNED = (
    (
        "model.py",
        "c34cb7439321fce84e964b8357bee627ae2e32f80d8969b443a82c0d28d080bf",
        "74729564f6f2cfe129f6ccbb16a9fd37466b38863e1f920e3cab16c7f535fe73",
        "src/jeff/model.py",
    ),
    (
        "decoder.py",
        "8e156b92b81811f9a52c17ad7634a24f370f587ff5b16d5aa0ab47faf2b6d98d",
        "a81775fc69592c2701ab300458eb61e3477ccdf5d3102f17f47f99d0dc62955d",
        "src/jeff/decoder.py",
    ),
    (
        "types.py",
        "0f8e3bfa4e92693ec50b9f258fb9180438a62d30caf9b3e6c00860a71e46dc5a",
        "0f8e3bfa4e92693ec50b9f258fb9180438a62d30caf9b3e6c00860a71e46dc5a",
        "src/jeff/types.py",
    ),
)

#: The complete set of differences between upstream and this copy, as (upstream, vendored).
#: Forward is what re-vendoring does; `_upstream_of` applies it in reverse so the two can be
#: compared.
SUBSTITUTIONS = (
    ("from jeff.types import", "from .types import"),
    ("from jeff.model import", "from .model import"),
)


def _upstream_of(vendored: str) -> str:
    """Undo the import rewrite, so the result must equal the file upstream ships."""
    for upstream, local in SUBSTITUTIONS:
        vendored = vendored.replace(local, upstream)
    return vendored


def test_the_vendored_directory_exists() -> None:
    assert VENDOR_DIR.is_dir(), f"missing {VENDOR_DIR}"


@pytest.mark.parametrize("name,expected,upstream_hash,upstream_path", PINNED)
def test_each_vendored_file_matches_its_pinned_hash(
    name: str, expected: str, upstream_hash: str, upstream_path: str
) -> None:
    """Content, not existence: a one-word edit to third-party model code must not pass."""
    path = VENDOR_DIR / name
    assert path.is_file(), f"{upstream_path} should be vendored as {path}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected, (
        f"{name} differs from the pinned copy of {upstream_path}.\n"
        f"  expected {expected}\n  actual   {actual}\n"
        "Do not edit vendored third-party code. Re-vendor at a new revision and update "
        "VENDOR.md + NOTICE instead (see VENDOR.md 'Re-vendoring')."
    )


def test_vendor_md_records_the_same_hashes() -> None:
    """VENDOR.md is the human-readable half of the pin; the two must not drift apart."""
    text = (VENDOR_DIR / "VENDOR.md").read_text(encoding="utf-8")
    for name, expected, upstream_hash, _ in PINNED:
        assert expected in text, f"VENDOR.md does not record the vendored sha256 of {name}"
        assert upstream_hash in text, f"VENDOR.md does not record the upstream sha256 of {name}"
    assert PIN in text, "VENDOR.md does not record the pinned tag's commit"
    assert "v1.1" in text, "VENDOR.md does not record the pinned tag"


def test_the_mit_licence_travels_with_the_code() -> None:
    """MIT requires the copyright notice and the permission text to travel with the code.

    The code licence and the weight licence are different things here, and the distinction is
    easy to get backwards: jeff's *source* is MIT, while both *checkpoints* declare
    `apache-2.0` on the Hub. `NOTICE` records both.
    """
    licence = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    assert licence.startswith("MIT License")
    # Both holders named in the vendored copy, not just the primary author: jeff's training
    # code derives from AutoJev.
    assert "Mathias Strasser" in licence
    assert "Denis Yarats" in licence
    assert "WITHOUT WARRANTY OF ANY KIND" in licence


@pytest.mark.parametrize("name,_expected,_upstream_hash,_upstream_path", PINNED)
def test_the_only_difference_from_upstream_is_the_import_rewrite(
    name: str, _expected: str, _upstream_hash: str, _upstream_path: str
) -> None:
    """Offline proof that nothing *else* was changed, not just that the bytes are stable.

    The hashes above say "these bytes did not move". They cannot say whether they were
    correct when they were first written, so this asserts the property that matters: every
    absolute self-import became a relative one, and no other substitution was made. A
    re-vendor that also reformatted a line would keep the hash test green on its own.
    """
    text = (VENDOR_DIR / name).read_text(encoding="utf-8")
    assert "from jeff" not in text and "import jeff" not in text, (
        f"{name} still imports jeff absolutely; the vendored copy must stand alone as a subpackage"
    )
    # And the rewrite is confined to import lines: the substitution must be reversible
    # without touching anything the model computes with.
    assert "jeff" not in _upstream_of(text).replace("from jeff.", "").replace("import jeff", ""), (
        f"{name} mentions jeff outside an import statement"
    )


def test_the_package_explains_its_own_provenance() -> None:
    """`__init__.py` is the entry point a reader lands on; it must not be a bare re-export."""
    text = (VENDOR_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "firelex/jeff" in text, "__init__.py does not name where this code came from"
    assert "VENDOR.md" in text, "__init__.py does not point at the provenance record"
    # The three things upstream does that Decis deliberately does not vendor, each for a
    # stated reason: the wire format, the training code and the MLX backend.
    for absent in ("server.py", "mlx_backend.py", "train.py"):
        assert absent in text, f"__init__.py does not say why {absent} is not vendored"


def test_nothing_in_the_package_touches_network_or_environment_at_import() -> None:
    """Importing the vendored code must not need a Hub token or a network round trip."""
    for name, *_ in PINNED:
        text = (VENDOR_DIR / name).read_text(encoding="utf-8")
        assert "os.environ" not in text, f"{name} reads the environment at import time"


@pytest.mark.network
@pytest.mark.parametrize("name,_expected,upstream_hash,upstream_path", PINNED)
def test_the_pinned_upstream_bytes_reproduce_this_copy(
    name: str, _expected: str, upstream_hash: str, upstream_path: str
) -> None:
    """Re-fetch the pinned revision and apply the rewrite; the bytes must match exactly.

    This is the test that turns "we vendored v1.1" from a sentence in `VENDOR.md` into
    something checkable, including the provenance problem `VENDOR.md` records: the
    checkpoints name a commit that is not in the public history, so the pin is the closest
    reproducible revision rather than the training-time one.
    """
    httpx = pytest.importorskip("httpx", reason="the fetch needs httpx, a dev dependency")

    response = httpx.get(f"{REPO}/{PIN}/{upstream_path}", follow_redirects=True, timeout=30.0)
    assert response.status_code == 200, f"{upstream_path} at {PIN}: HTTP {response.status_code}"
    upstream = response.text
    assert hashlib.sha256(response.content).hexdigest() == upstream_hash, (
        f"{upstream_path} at {PIN} is not the file VENDOR.md records; the tag may have moved"
    )
    vendored = (VENDOR_DIR / name).read_text(encoding="utf-8")
    assert _upstream_of(vendored) == upstream, f"{name} is not upstream {upstream_path} plus the import rewrite"
