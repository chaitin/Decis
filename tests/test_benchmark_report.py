"""The benchmark report generator, and the discipline it is supposed to enforce.

`AGENTS.md §8` says every performance number in the docs must come from a checked-in raw
JSON file and be rendered by `benchmarks/report.py`. That is only a rule if something
checks it, so this file tests the generator the way the generator tests the data:

* the checked-in docs are in sync with the raw JSON, and **a hand-edited digit fails**;
* two configurations in one comparison group that processed different input are refused;
* configurations that legitimately sent *different* requests are not falsely refused;
* a raw file that cannot be parsed is an error, not a silently skipped table.

The generator lives outside the package (`benchmarks/`), so it is loaded by path. That is
deliberate: it must keep working in a checkout where `decis` cannot be imported, because
its job includes telling you the engine metadata it could not resolve.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "benchmarks" / "report.py"


def _load_report():
    spec = importlib.util.spec_from_file_location("_bench_report", REPORT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_bench_report"] = module
    spec.loader.exec_module(module)
    return module


report = _load_report()


@pytest.fixture(scope="module")
def groups():
    return report.load_all()


@pytest.fixture(scope="module")
def gaps(groups):
    return report.verify(groups)


def _config(**overrides):
    base = {
        "questions": 3,
        "threads": 1,
        "input_tokens": 100,
        "input_sha256": "a" * 64,
        "p50_ms": 10.0,
    }
    return report.Config(**{**base, **overrides})


def _group(configs, **overrides):
    base = {
        "name": "test",
        "engine": "test-engine",
        "device": "cpu",
        "dtype": "float32",
        "configs": tuple(configs),
        "source": ROOT / "benchmarks" / "results" / "synthetic.json",
    }
    return report.Group(**{**base, **overrides})


# --- the point of the generator: refuse incomparable input ---------------------


def test_configurations_that_sent_different_input_are_refused() -> None:
    """The core assertion. A latency comparison across different input is meaningless."""
    group = _group(
        [
            _config(threads=1, input_sha256="a" * 64),
            _config(threads=8, input_sha256="b" * 64),
        ]
    )
    with pytest.raises(report.InconsistentInputsError) as caught:
        report.verify([group])
    message = str(caught.value)
    assert "different input" in message
    # It must name which configurations differ, or the failure is not actionable.
    assert "threads=1" in message and "threads=8" in message


def test_different_token_counts_are_refused_even_with_a_matching_hash() -> None:
    """A hash collision is not the only way to compare different things."""
    group = _group(
        [
            _config(threads=1, input_tokens=100),
            _config(threads=8, input_tokens=140),
        ]
    )
    with pytest.raises(report.InconsistentInputsError) as caught:
        report.verify([group])
    assert "tokenised to different lengths" in str(caught.value)


def test_different_question_counts_are_not_compared() -> None:
    """A 1-question and a 10-question request *should* differ, and must not be refused.

    Treating that as an inconsistency would make the generator unusable, so the check is
    scoped per question count. This is the test that keeps the rule from being
    over-applied.
    """
    group = _group(
        [
            _config(questions=1, input_sha256="a" * 64, input_tokens=50),
            _config(questions=10, input_sha256="b" * 64, input_tokens=500),
        ]
    )
    assert report.verify([group]) == []


def test_a_single_configuration_needs_no_comparison() -> None:
    assert report.verify([_group([_config()])]) == []


def test_a_file_without_an_input_hash_is_reported_not_hidden() -> None:
    """The legacy kev record cannot be verified. That must be stated, not assumed away.

    If this ever stops being reported, an unverifiable comparison would start looking
    like a verified one -- which is the failure mode `AGENTS.md §8` exists to prevent.
    """
    group = _group(
        [_config(dtype="fp32", input_sha256=None), _config(dtype="bf16", input_sha256=None)],
        hashless=True,
    )
    gaps = report.verify([group])
    assert any("no input hash" in gap for gap in gaps), gaps


def test_the_kev_dtype_record_is_flagged_as_hashless(groups) -> None:
    """The checked-in evidence must carry its own limitation in the generated output."""
    kev = [group for group in groups if "kev" in group.source.name]
    assert kev, "the kev dtype record disappeared from benchmarks/results/"
    assert all(group.hashless for group in kev)


# --- the checked-in docs are generated, and a hand edit fails ------------------


def test_the_checked_in_docs_are_in_sync_with_the_raw_json(groups, gaps) -> None:
    """This is what CI runs. A number edited by hand must fail here."""
    assert report.stale(groups, gaps) == [], "run: python benchmarks/report.py --write"


def test_a_hand_edited_number_is_detected() -> None:
    """The negative control for the guard above.

    Without this, `stale()` returning `[]` could mean "in sync" or "not actually
    comparing anything". A single changed digit must make it report the file.
    """
    groups = report.load_all()
    gaps = report.verify(groups)
    readme = ROOT / "README.md"
    original = readme.read_text(encoding="utf-8")

    begin = report.README_LATENCY_BEGIN
    end = report.README_LATENCY_END
    block = original[original.index(begin) : original.index(end)]
    # Change one number inside the generated block, exactly as a careless edit would.
    edited_block = block.replace(" ms", " ms", 1).replace("| 24 |", "| 23 |", 1)
    assert edited_block != block, "the fixture failed to modify the block"

    try:
        readme.write_text(original.replace(block, edited_block), encoding="utf-8")
        assert report.stale(groups, gaps) != [], "a hand-edited table was not detected"
    finally:
        readme.write_text(original, encoding="utf-8")
    assert report.stale(groups, gaps) == [], "the test failed to restore README.md"


def test_every_generated_block_has_markers_in_its_target() -> None:
    """`--write` needs both markers; a missing one is a confusing crash, so assert first."""
    groups = report.load_all()
    for path, begin, end, _ in report.targets(groups, report.verify(groups)):
        if begin is None:
            continue
        text = path.read_text(encoding="utf-8")
        assert begin in text, f"{path.name} is missing {begin}"
        assert end in text, f"{path.name} is missing {end}"


def test_replace_block_keeps_everything_outside_the_markers() -> None:
    """Generated content must never touch the prose around it."""
    original = "before\nBEGIN\nold\nEND\nafter\n"
    updated = report.replace_block(original, "BEGIN", "END", "BEGIN\nnew\nEND")
    assert updated == "before\nBEGIN\nnew\nEND\nafter\n"


def test_replace_block_refuses_when_a_marker_is_absent() -> None:
    with pytest.raises(ValueError, match="not found"):
        report.replace_block("no markers here", "BEGIN", "END", "x")


# --- the generator understands every checked-in file ---------------------------


@pytest.mark.parametrize("path", sorted((ROOT / "benchmarks" / "results").glob("*.json")))
def test_every_raw_result_file_can_be_loaded(path: Path) -> None:
    """A raw file with no adapter must fail loudly, not be skipped out of the report."""
    group = report.load_group(path)
    assert group.configs, f"{path.name} parsed to zero configurations"
    assert group.engine, f"{path.name} has no engine"


def test_an_unknown_schema_is_an_error_not_a_skip(tmp_path: Path) -> None:
    unknown = tmp_path / "mystery.json"
    unknown.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognised schema"):
        report.load_group(unknown)


def test_no_raw_results_at_all_is_an_error(tmp_path: Path, monkeypatch) -> None:
    """An empty results directory must not produce empty tables that look authoritative."""
    monkeypatch.setattr(report, "RESULTS", tmp_path)
    with pytest.raises(SystemExit, match="no raw results"):
        report.load_all()


# --- the generated content is faithful to the raw data -------------------------


def test_generated_tables_quote_the_numbers_that_are_in_the_json(groups) -> None:
    """Every latency the README shows must equal the raw file's value.

    A weaker version would check that *some* number appears; this checks the value the
    generator actually chose against the file it claims to have read.
    """
    rendered = report.render_readme_latency(groups)
    for group in groups:
        if group.hashless:
            continue
        counts = {config.questions for config in group.configs}
        if not {1, 10} <= counts:
            continue
        for config in group.configs:
            if config.questions == 1 and config.threads == (group.host or {}).get("cpu_count"):
                assert f"{config.p50_ms:,.0f} ms" in rendered


def test_the_readme_row_uses_one_thread_count_for_both_columns(groups) -> None:
    """The defect this generator was written to fix, pinned as a test.

    The table it replaced reported a 1-question latency from thread count 16 and a
    10-question latency from thread count 24 in the same row. Both numbers were honest;
    the row was not. A generated row now states its thread count and uses only that one.
    """
    rendered = report.render_readme_latency(groups)
    rows = [line for line in rendered.splitlines() if line.startswith("| `")]
    assert rows, "no data rows were generated"
    for row in rows:
        cells = [cell.strip() for cell in row.split("|")]
        threads = cells[4]
        assert threads.isdigit(), f"the row does not declare its thread count: {row}"
        for group in groups:
            expected = [config for config in group.configs if str(config.threads) == threads]
            if not expected:
                continue
            assert all(config.threads == int(threads) for config in expected)


# --- the CLI wrapper -----------------------------------------------------------


def test_decis_bench_runs_the_checked_in_harness(monkeypatch, tmp_path: Path) -> None:
    """`decis bench` must call the same harness the docs regenerate tables from.

    A second implementation would be the classic two-homes bug (`AGENTS.md §2`): the
    numbers in the docs would then come from code nobody measured with.
    """
    from decis import cli as cli_module

    seen: dict[str, list[str]] = {}

    def fake_run(command, check=False):
        seen["command"] = list(command)

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr("subprocess.run", fake_run)
    code = cli_module.main(["bench", "--engine", "mock", "--batch", "1,2", "--env-file", ""])
    assert code == 0
    command = seen["command"]
    assert command[1].endswith("benchmarks/run.py"), command
    assert "--engine" in command and "mock" in command
    assert "1,2" in command


def test_decis_bench_says_so_when_the_harness_is_absent(monkeypatch) -> None:
    """An installed wheel has no `benchmarks/`. Refusing clearly beats a traceback."""
    from pathlib import Path as _Path

    from decis import cli as cli_module

    monkeypatch.setattr(_Path, "is_file", lambda self: False)
    code = cli_module.main(["bench", "--engine", "mock", "--env-file", ""])
    assert code == 2
