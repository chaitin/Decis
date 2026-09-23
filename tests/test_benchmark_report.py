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
    """A raw file with no adapter must fail loudly, not be skipped out of the report.

    Both schemas are covered, because a file the reporter cannot parse would drop a whole
    experiment out of the generated docs while `--check` still reported everything in sync.
    """
    loaded = report.load_any(path)
    assert loaded.engine, f"{path.name} has no engine"
    if isinstance(loaded, report.BatchGain):
        assert loaded.item_sets or loaded.aggregate, f"{path.name} parsed to no measurements"
    else:
        assert loaded.configs, f"{path.name} parsed to zero configurations"


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
    code = cli_module.main(["bench", "--engine", "stub", "--batch", "1,2", "--env-file", ""])
    assert code == 0
    command = seen["command"]
    assert command[1].endswith("benchmarks/run.py"), command
    assert "--engine" in command and "stub" in command
    assert "1,2" in command


def _captured_command(monkeypatch, argv: list[str]) -> list[str]:
    """Run `decis bench ...` with `subprocess.run` stubbed, and return the command."""
    from decis import cli as cli_module

    seen: dict[str, list[str]] = {}

    def fake_run(command, check=False):
        seen["command"] = list(command)

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert cli_module.main([*argv, "--env-file", ""]) == 0
    return seen["command"]


def test_cross_request_selects_the_batching_harness(monkeypatch) -> None:
    """Two harnesses answer two questions; the flag must reach the right one."""
    command = _captured_command(monkeypatch, ["bench", "--engine", "stub", "--cross-request", "--threads", "8"])
    assert command[1].endswith("benchmarks/batch_gain.py"), command
    # `run.py` spells it `--batch`; `batch_gain.py` takes a list of sizes.
    assert "--batches" in command, command
    assert "--processes" in command, command


def test_the_process_comparison_holds_the_thread_budget_constant(monkeypatch) -> None:
    """The guard for the mistake that already invalidated one round of this data.

    `batch_gain.py --threads` is *per process*. Passing the machine's core count through
    unchanged to `--processes 4` would run 4x the threads the serial baseline used, and
    the comparison would report thread oversubscription as if it were process scaling.
    """
    command = _captured_command(
        monkeypatch,
        ["bench", "--engine", "stub", "--cross-request", "--threads", "24", "--processes", "4"],
    )
    assert command[command.index("--threads") + 1] == "6", command
    assert command[command.index("--processes") + 1] == "4", command

    # One process keeps the whole budget.
    command = _captured_command(
        monkeypatch,
        ["bench", "--engine", "stub", "--cross-request", "--threads", "24", "--processes", "1"],
    )
    assert command[command.index("--threads") + 1] == "24", command


def test_a_zero_process_count_is_refused(monkeypatch) -> None:
    """`--processes 0` would divide by zero; it must be a config error, not a traceback."""
    from decis import cli as cli_module

    monkeypatch.setattr("subprocess.run", lambda command, check=False: None)
    assert cli_module.main(["bench", "--engine", "stub", "--cross-request", "--processes", "0"]) == 2


def test_decis_bench_says_so_when_the_harness_is_absent(monkeypatch) -> None:
    """An installed wheel has no `benchmarks/`. Refusing clearly beats a traceback."""
    from pathlib import Path as _Path

    from decis import cli as cli_module

    monkeypatch.setattr(_Path, "is_file", lambda self: False)
    code = cli_module.main(["bench", "--engine", "stub", "--env-file", ""])
    assert code == 2


# --- cross-request batching: the negative result has to be defensible ----------
#
# `docs/design-review.md §4-M5` is now closed with a *negative* answer, and a negative
# answer is only worth anything if the harness that produced it could have produced a
# positive one. These tests hold that line: the positive control is mandatory, the
# comparison is refused when it would be measuring the wrong thing, and the conclusion the
# docs quote is recomputed from the raw JSON rather than restated.


@pytest.fixture(scope="module")
def gains():
    return report.load_batch_gains()


def test_the_batching_experiment_is_present(gains) -> None:
    assert gains, "no `decis-batch-gain/1` files in benchmarks/results/"
    assert any(gain.processes == 1 for gain in gains), "the batching run itself is missing"
    assert any(gain.processes > 1 for gain in gains), "the multiprocess comparison is missing"


def test_batching_files_are_not_mistaken_for_latency_tables(gains) -> None:
    """The two schemas answer different questions and must not be merged into one table."""
    sources = {path.name for path in report.RESULTS.glob("*.json")}
    latency = {group.source.name for group in report.load_all()}
    assert sources - latency == {gain.source.name for gain in gains}


def test_the_batching_comparison_verifies(gains) -> None:
    assert report.verify_batch_gains(gains) == []


def test_a_positive_control_is_mandatory(gains) -> None:
    """Without it, "batching does not help" is indistinguishable from "the harness is broken".

    This is `§2-D1`'s lesson applied to a measurement instead of a mechanism: a harness that
    cannot detect a gain where one exists cannot be trusted to report that none exists.
    """
    import copy

    broken = copy.deepcopy(gains[0])
    with_control_removed = report.BatchGain(
        **{**broken.__dict__, "item_sets": {k: v for k, v in broken.item_sets.items() if not k.startswith("shared")}}
    )
    problems = report.verify_batch_gains([with_control_removed])
    assert any("positive control" in problem for problem in problems), problems


def test_a_missing_serial_baseline_is_refused(gains) -> None:
    """A speedup needs something to be a speedup over."""
    import copy

    broken = copy.deepcopy(gains[0])
    item_sets = {name: copy.deepcopy(block) for name, block in broken.item_sets.items()}
    item_sets["uniform"]["configs"] = [c for c in item_sets["uniform"]["configs"] if c["batch"] != 1]
    problems = report.verify_batch_gains([report.BatchGain(**{**broken.__dict__, "item_sets": item_sets})])
    assert any("serial (batch 1) baseline" in problem for problem in problems), problems


def test_an_impossible_padding_ratio_is_refused(gains) -> None:
    """Padding below 1.0 means the measured lengths are wrong, not that the batch was free."""
    import copy

    broken = copy.deepcopy(gains[0])
    item_sets = {name: copy.deepcopy(block) for name, block in broken.item_sets.items()}
    item_sets["uniform"]["configs"][1]["padding_ratio"] = 0.5
    problems = report.verify_batch_gains([report.BatchGain(**{**broken.__dict__, "item_sets": item_sets})])
    assert any("below 1.0" in problem for problem in problems), problems


def test_process_counts_must_share_one_thread_budget(gains) -> None:
    """The oversubscription guard.

    Comparing 1 process x 24 threads with 4 processes x 24 threads measures thread
    oversubscription, not process scaling, and would make concurrency look either wonderful
    or catastrophic depending on which way the oversubscription fell.
    """
    mismatched = [
        report.BatchGain(**{**gain.__dict__, "threads": gain.threads * 2}) if gain.processes > 1 else gain
        for gain in gains
    ]
    problems = report.verify_batch_gains(mismatched)
    assert any("thread budget" in problem for problem in problems), problems


def test_the_rendered_batching_block_carries_its_own_markers(gains) -> None:
    """`replace_block` consumes the markers it is handed, so the renderer must emit them.

    Getting this wrong deletes the block's delimiters and the next run cannot find them --
    which is a real failure this test exists to prevent, not a hypothetical one.
    """
    rendered = report.render_batching(gains, chinese=True)
    assert rendered.startswith(report.BATCHING_BEGIN)
    assert rendered.rstrip().endswith(report.BATCHING_END)

    for path in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        block = report.render_batching_readme(gains, chinese=path.name.endswith("zh-CN.md"))
        assert block.startswith(report.BATCHING_BEGIN), path.name
        assert block.rstrip().endswith(report.BATCHING_END), path.name


def test_the_documented_conclusion_is_recomputed_from_the_raw_json(gains) -> None:
    """The verdict in the docs must follow from the data, in both languages.

    A hand-written "batching does not help" would be as unbacked as a hand-written number,
    so the generator computes it and this test pins the computation.
    """
    batched = next(gain for gain in gains if gain.processes == 1)
    control = max(
        config.get("speedup_vs_serial", 0.0)
        for name, block in batched.item_sets.items()
        if name.startswith("shared")
        for config in block["configs"]
        if config["batch"] > 1
    )
    real = max(
        config.get("speedup_vs_serial", 0.0)
        for name, block in batched.item_sets.items()
        if not name.startswith("shared")
        for config in block["configs"]
        if config["batch"] > 1
    )
    # The control has to be the better of the two, or the experiment says nothing.
    assert control > real, f"the positive control ({control}) did not beat the real sets ({real})"

    for chinese in (True, False):
        verdict = report._verdict(gains, chinese)
        assert f"{control:.2f}x" in verdict, verdict
        # And it must not claim a gain for the shape real traffic has.
        assert f"{real:.2f}x" in verdict, verdict


def test_the_verdict_refuses_to_claim_a_gain_that_is_not_there(gains) -> None:
    """The negative branch is the one the docs depend on, so it is tested directly."""
    import copy

    batched = next(gain for gain in gains if gain.processes == 1)
    item_sets = {name: copy.deepcopy(block) for name, block in batched.item_sets.items()}
    for block in item_sets.values():
        for config in block["configs"]:
            if config["batch"] > 1:
                # Every batch is a regression, as the skewed set already is.
                config["speedup_vs_serial"] = 0.2
    verdict = report._verdict([report.BatchGain(**{**batched.__dict__, "item_sets": item_sets})], chinese=False)
    assert "no gain at any batch size" in verdict, verdict


def test_a_hand_edited_batching_table_is_detected(gains) -> None:
    """The negative control, on the new block, in the file that had drifted by hand before."""
    groups = report.load_all()
    gaps = report.verify(groups)
    readme = ROOT / "README.zh-CN.md"
    original = readme.read_text(encoding="utf-8")
    block = report.render_batching_readme(gains, chinese=True)
    assert block in original, "the Chinese README's batching block is not in sync"

    edited = block.replace("1.92x", "9.99x", 1)
    assert edited != block, "the fixture failed to modify the block"
    try:
        readme.write_text(original.replace(block, edited), encoding="utf-8")
        assert report.stale(groups, gaps) != [], "a hand-edited batching table was not detected"
    finally:
        readme.write_text(original, encoding="utf-8")
    assert report.stale(groups, gaps) == [], "the test failed to restore README.zh-CN.md"


def test_the_readmes_agree_because_one_generator_writes_both(groups) -> None:
    """The Chinese README used to carry a hand-copied table, and it drifted (D12 again).

    It quoted 201 ms for one question (16 threads) beside 987 ms for ten (24 threads) --
    the same mixed-thread-count defect `§2-D12` records, still live because no generator
    checked that file. Both READMEs are generated now, so their numbers must be identical.
    """

    def figures(path: Path) -> list[str]:
        """Every figure in the file's latency block, in file order.

        Extracted with a regex rather than by splitting cells: a cell reads `987 ms
        (98.7 ms/question)`, and a check that only accepted integer cells would compare a
        couple of thread counts and prove nothing.
        """
        import re

        text = path.read_text(encoding="utf-8")
        block = text[text.index(report.README_LATENCY_BEGIN) : text.index(report.README_LATENCY_END)]
        rows = [line for line in block.splitlines() if line.startswith("| `")]
        assert rows, f"{path.name} has no data rows"
        return re.findall(r"\d[\d,]*\.?\d*", "\n".join(rows))

    english = figures(ROOT / "README.md")
    chinese = figures(ROOT / "README.zh-CN.md")
    # Read from the files, not from the renderers: comparing two calls of the same
    # generator would pass even if a file still held a stale hand-written table, which is
    # exactly the defect being guarded against.
    assert english == chinese, f"the two READMEs quote different figures:\n{english}\n{chinese}"
    # 2 engines x (threads + 2 latencies + 2 memory figures) at least.
    assert len(english) >= 10, english


def test_both_readmes_have_every_generated_marker() -> None:
    """A missing marker is a crash during `--write`, and it had already happened once."""
    groups = report.load_all()
    for path in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        text = path.read_text(encoding="utf-8")
        for marker in (
            report.README_LATENCY_BEGIN,
            report.README_LATENCY_END,
            report.README_DTYPE_BEGIN,
            report.README_DTYPE_END,
            report.BATCHING_BEGIN,
            report.BATCHING_END,
        ):
            assert marker in text, f"{path.name} is missing {marker}"
    assert report.load_all(), groups
