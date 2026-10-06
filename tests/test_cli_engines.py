"""`decis engines`: the catalogue of what this build can be asked for.

The command exists because "which engines are there, and which repository does each one
fetch?" was only answerable by reading `registry.py` and six engine modules -- and the ids
are the argument of `--engine`/`model` on every other command. So the assertions here are
about *reading*, not restating: every value in the table is compared with the object the
fetch itself uses (`registry.SPECS`, `create(id).weights()`), never with a copy of it
(`AGENTS.md` §2/§9; the D14 lesson about hand-copied tables living in tests).

`decis models` is a different question -- "can this machine run it" -- and stays as it was:
a host with no weights and no extra still gets a full catalogue here.
"""

from __future__ import annotations

from decis.engines.registry import SPECS, create, known_names
from test_cli_serve import run_cli


def test_the_catalogue_names_every_registered_engine() -> None:
    code, text = run_cli("engines", "--env-file", "")
    assert code == 0, text

    for engine_id in SPECS:
        assert engine_id in text, f"{engine_id} is registered but not listed"
    assert text.count("\n") >= len(SPECS), "one row per engine, plus the header and the notes"


def test_each_row_names_the_repository_that_engine_would_fetch() -> None:
    """The weights column is the argument of `decis download`, so it is read from the spec."""
    _, text = run_cli("engines", "--env-file", "")
    _, servable = run_cli("models", "--env-file", "")

    for engine_id, entry in SPECS.items():
        spec = create(engine_id).weights()
        if spec is None or not spec.is_downloadable():
            assert "none (self-contained)" in text, f"{engine_id} claims to ship no weights"
            continue
        assert spec.repo_id in text, f"{engine_id} fetches {spec.repo_id}, which is not in the catalogue"
        # A subfolder is part of the identity: three engines share one repository.
        if spec.subfolder:
            assert f"{spec.repo_id}/{spec.subfolder}" in text, f"{engine_id} hides its subfolder"
        assert entry.extra in text, f"{engine_id}'s extra is the install line a reader needs"
        assert engine_id in servable, "the catalogue and `models` must describe the same registry"


def test_the_base_model_a_checkpoint_adapts_is_visible() -> None:
    """kev is useless without its base, and the base is a *second* repository (§2-D33)."""
    _, text = run_cli("engines", "--env-file", "")

    spec = create("kev-0.8b").weights()
    assert spec is not None
    bases = spec.base_specs()
    assert bases, "kev's base is what keeps it from being a one-repository download"
    for base in bases:
        assert base.repo_id in text, f"the base {base.repo_id} is fetched too but not listed"


def test_the_aliases_are_listed_because_they_are_valid_model_names() -> None:
    """`model: "kev"` works, and only the catalogue says so."""
    _, text = run_cli("engines", "--env-file", "")

    for engine_id, entry in SPECS.items():
        for alias in entry.aliases:
            assert alias in text, f"{engine_id}'s alias {alias} is accepted but not listed"
    assert set(known_names()) >= {alias for entry in SPECS.values() for alias in entry.aliases}


def test_the_default_engine_is_named_once() -> None:
    """`serve` with no `--engine` runs it, so a reader has to be able to find it."""
    _, text = run_cli("engines", "--env-file", "")
    assert "default engine:" in text


def test_an_unknown_engine_points_at_the_catalogue() -> None:
    """The other half of "list what is supported": the error has to be actionable."""
    from decis.cli import _EXIT_CONFIG_ERROR

    code, text = run_cli("download", "--engine", "typo", "--env-file", "")
    assert code == _EXIT_CONFIG_ERROR, text
    for engine_id in SPECS:
        assert engine_id in text, "the error lists the ids, so a typo is one read away from a fix"


def test_the_catalogue_is_not_a_verdict_about_this_machine() -> None:
    """Two questions, two commands: `models` says "runnable here", `engines` says "what exists".

    A user on a host with nothing installed still gets the full list -- that is what makes it
    the first command to run -- and the local verdict's words must not leak into it, or the
    two outputs would disagree about a machine neither of them describes.
    """
    _, catalogue = run_cli("engines", "--env-file", "")
    _, verdict = run_cli("models", "--env-file", "")

    for engine_id in SPECS:
        assert engine_id in catalogue and engine_id in verdict
    assert "usable:" not in catalogue, "the catalogue must not carry this host's readiness"
    assert "deps missing" not in catalogue and "needs weights" not in catalogue
    assert "weights:" in catalogue, "the weights column has to say what it is"
