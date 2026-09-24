"""Generate the tables in the docs from the raw JSON in `benchmarks/results/`.

`AGENTS.md §8` makes two demands that this program exists to satisfy:

1. **No hand-written numbers.** Every figure in `docs/` that describes performance is
   rendered here, from a checked-in raw file. The READMEs print none of them: latency is
   dominated by the machine, so a headline table there says more about the host than about
   this project. The measured record lives in `docs/performance.md`.
2. **Refuse to compare incomparable things.** Before rendering, every configuration
   inside a comparison group must be shown to have processed the *same input* --
   identical `input_sha256` and identical token count. If they differ, the honest
   conclusion is that the comparison is invalid, so this exits non-zero and says which
   configuration differs.

Point 2 is not hypothetical. Writing this program immediately caught a real defect in
the tables it replaces: the README's English row reported 432 ms for one question and
3222 ms for ten, but those came from **different thread counts** (16 and 24). Both
numbers were individually honest and the pair was meaningless. A generated table
derives a row from one declared configuration, so that class of error cannot recur.

Usage:

    python benchmarks/report.py            # print the generated blocks
    python benchmarks/report.py --write    # update docs/performance.md and docs/feasibility.md
    python benchmarks/report.py --check    # exit 1 if the docs are out of date (CI)

`--check` is what makes §8 enforceable: a PR that edits a number by hand fails CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results"
RUNTIME_RECORD = ROOT / "docs" / "contract" / "stage1-batch-invariance.json"

MARKER_BEGIN = "<!-- MEASUREMENTS:START -->"
MARKER_END = "<!-- MEASUREMENTS:END -->"
README_LATENCY_BEGIN = "<!-- LATENCY:START -->"
README_LATENCY_END = "<!-- LATENCY:END -->"
README_DTYPE_BEGIN = "<!-- DTYPE:START -->"
README_DTYPE_END = "<!-- DTYPE:END -->"
BATCHING_BEGIN = "<!-- BATCHING:START -->"
BATCHING_END = "<!-- BATCHING:END -->"

#: The throughput experiment's schema. A different shape from `decis-benchmark/1`
#: because it answers a different question: not "how long does one call take" but "does
#: joining calls together make each call cheaper".
BATCH_GAIN_SCHEMA = "decis-batch-gain/1"


class InconsistentInputsError(RuntimeError):
    """Two configurations in one comparison group processed different input.

    Raised rather than warned: any latency conclusion drawn across them would be
    meaningless, and a warning in a build log is a warning nobody reads.
    """


@dataclass(frozen=True)
class Config:
    """One measured configuration, normalised from whichever schema recorded it."""

    questions: int
    threads: int | None
    input_tokens: int | None
    input_sha256: str | None
    p50_ms: float
    n: int | None = None
    p95_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    ms_per_question: float | None = None
    dtype: str | None = None
    state_chars: int | None = None
    samples_ms: tuple[float, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def rate(self) -> float | None:
        """Questions per second for a *single serial caller*. Not throughput.

        Named for what it is, because the old field name `questions_per_second`
        invited readers to treat it as a service capacity (benchmarks/README.md M4).
        """
        if not self.p50_ms:
            return None
        return self.questions / (self.p50_ms / 1000.0)


@dataclass(frozen=True)
class Group:
    """All configurations measured together on one host, in one file."""

    name: str
    engine: str
    device: str
    dtype: str
    configs: tuple[Config, ...]
    source: Path
    host: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    load_s: float | None = None
    peak_rss_gb: float | None = None
    engine_version: str | None = None
    state_chars: int | None = None
    batching_scope: str = "within-request"
    #: The checkpoint/subfolder inside the engine, when the raw file records one. Two
    #: Laya checkpoints share the `laya` engine id, so without this both tables would be
    #: titled "laya" and a reader could not tell them apart.
    variant: str | None = None
    caveats: tuple[str, ...] = ()
    #: True when the file predates input-hash recording, so comparability can only be
    #: checked on token counts. Reported, never hidden.
    hashless: bool = False

    def by_questions(self) -> dict[int, list[Config]]:
        buckets: dict[int, list[Config]] = {}
        for config in self.configs:
            buckets.setdefault(config.questions, []).append(config)
        return buckets


# --- loading, with an adapter per schema ----------------------------------------


def load_group(path: Path) -> Group:
    """Read one raw file into a `Group`, dispatching on the schema it declares.

    Three shapes exist in the repository: the current `decis-benchmark/1`, and two
    pre-implementation probe schemas kept because they are the evidence behind numbers
    already published. The legacy files are parsed rather than re-typed: a manual
    transcription step is exactly where a wrong number gets introduced.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    schema = document.get("schema")
    if schema == "decis-benchmark/1":
        return _load_native(path, document)
    if "peak_rss_gb_after_load" in document:
        return _load_legacy_sweep(path, document)
    if any("dtype" in result for result in document.get("results", [])):
        return _load_legacy_dtype(path, document)
    raise ValueError(f"{path.name}: unrecognised schema; add an adapter or re-measure with benchmarks/run.py")


def _load_native(path: Path, document: dict[str, Any]) -> Group:
    configs = []
    for section in document.get("runs", []):
        for raw in section.get("configs", []):
            configs.append(
                Config(
                    questions=raw["questions_per_request"],
                    threads=raw.get("threads"),
                    input_tokens=raw.get("input_tokens"),
                    input_sha256=raw.get("input_sha256"),
                    p50_ms=raw["p50_ms"],
                    n=raw.get("n"),
                    p95_ms=raw.get("p95_ms"),
                    min_ms=raw.get("min_ms"),
                    max_ms=raw.get("max_ms"),
                    ms_per_question=raw.get("ms_per_question"),
                    dtype=document.get("dtype"),
                    state_chars=raw.get("state_chars"),
                    samples_ms=tuple(raw.get("samples_ms", ())),
                    extra={"answer_sha256": raw.get("answer_sha256")},
                )
            )
    observed = (document.get("runs") or [{}])[0].get("observed", {})
    return Group(
        name=document.get("group", path.stem),
        engine=document.get("engine", "unknown"),
        device=document.get("device", "cpu"),
        dtype=document.get("dtype", "unknown"),
        configs=tuple(configs),
        source=path,
        host=document.get("host", {}),
        runtime=document.get("runtime", {}),
        load_s=document.get("load_s"),
        peak_rss_gb=document.get("peak_rss_gb"),
        engine_version=document.get("engine_version") or observed.get("version"),
        state_chars=document.get("state_chars"),
        batching_scope=document.get("batching", {}).get("scope", "within-request"),
        variant=document.get("checkpoint"),
        caveats=tuple(document.get("methodology_caveats", ())),
    )


def _load_legacy_sweep(path: Path, document: dict[str, Any]) -> Group:
    """The pre-implementation Laya thread x questions sweep."""
    configs = tuple(
        Config(
            questions=raw["questions"],
            threads=raw["threads"],
            input_tokens=raw.get("input_tokens"),
            input_sha256=raw.get("input_sha256"),
            p50_ms=raw["p50_ms"],
            n=raw.get("n"),
            p95_ms=raw.get("p95_ms"),
            min_ms=raw.get("min_ms"),
            max_ms=raw.get("max_ms"),
            ms_per_question=raw.get("ms_per_question"),
            dtype="float32",
            extra={"single_caller_questions_per_second": raw.get("single_caller_questions_per_second")},
        )
        for raw in document["results"]
    )
    return Group(
        name=path.stem,
        engine=document.get("engine", path.stem),
        device="cpu",
        dtype="float32",
        configs=configs,
        source=path,
        host=document.get("host", {}),
        runtime={
            "python": document.get("python_version"),
            "torch": document.get("torch_version"),
            "laya": document.get("laya_version"),
        },
        load_s=document.get("load_s"),
        peak_rss_gb=document.get("peak_rss_gb_after_load"),
        variant=document.get("checkpoint"),
        caveats=tuple(document.get("methodology_caveats", ())),
    )


def _load_legacy_dtype(path: Path, document: dict[str, Any]) -> Group:
    """The kev fp32-vs-bf16 record.

    This file has no input hash -- its `method` field says the figures were
    "transcribed by hand from the response body" -- so `hashless` is set and both the
    console and the generated tables carry that fact.
    """
    request = document.get("request", {})
    tokens = (request.get("usage") or {}).get("input_tokens")
    threads = _threads_from_method(document.get("method", ""))
    configs = tuple(
        Config(
            questions=int(request.get("questions", 0)) or 1,
            threads=threads,
            input_tokens=tokens,
            input_sha256=None,
            p50_ms=float(raw["server_latency_ms"]),
            n=1,
            ms_per_question=float(raw["server_latency_ms"]) / max(1, int(request.get("questions", 1))),
            dtype=raw["dtype"],
            extra={"answers": raw.get("answers")},
        )
        for raw in document["results"]
    )
    return Group(
        name=path.stem,
        engine=document.get("engine", path.stem),
        device="cpu",
        dtype=" / ".join(raw["dtype"] for raw in document["results"]),
        configs=configs,
        source=path,
        host=document.get("host", {}),
        runtime={
            "python": (document.get("host") or {}).get("python_version"),
            "torch": (document.get("host") or {}).get("torch_version"),
            "transformers": (document.get("host") or {}).get("transformers_version"),
        },
        variant=document.get("checkpoint"),
        caveats=(document.get("method", ""), document.get("conclusion", "")),
        hashless=True,
    )


def _threads_from_method(method: str) -> int | None:
    for token in method.replace(",", " ").split():
        if token.upper().startswith("OMP_NUM_THREADS="):
            _, _, value = token.partition("=")
            if value.isdigit():
                return int(value)
    return None


# --- the discipline: refuse to compare incomparable inputs ----------------------


def verify(groups: list[Group]) -> list[str]:
    """Check every group's configurations processed the same input.

    Returns a list of human-readable provenance gaps (things that could not be
    verified). Raises `InconsistentInputs` when something *was* verified and failed.

    The check is per `questions_per_request`: configurations with different question
    counts legitimately send different requests, so they are not compared to each
    other -- only to configurations that sent the same request.
    """
    gaps: list[str] = []

    for group in groups:
        if not group.configs:
            gaps.append(f"{group.source.name}: no configurations")
            continue

        for questions, bucket in sorted(group.by_questions().items()):
            if len(bucket) < 2:
                continue  # nothing to compare against

            hashes = {config.input_sha256 for config in bucket}
            if group.hashless or hashes == {None}:
                gaps.append(
                    f"{group.source.name} ({questions} question(s)): no input hash recorded, "
                    f"so identical input could only be checked via token counts"
                )
            elif len(hashes) > 1:
                detail = ", ".join(f"threads={config.threads} sha={str(config.input_sha256)[:12]}" for config in bucket)
                raise InconsistentInputsError(
                    f"{group.source.name}: configurations with {questions} question(s) sent "
                    f"different input, so any latency comparison between them is invalid ({detail})"
                )

            tokens = {config.input_tokens for config in bucket if config.input_tokens is not None}
            if len(tokens) > 1:
                detail = ", ".join(f"threads={config.threads} tokens={config.input_tokens}" for config in bucket)
                raise InconsistentInputsError(
                    f"{group.source.name}: configurations with {questions} question(s) "
                    f"tokenised to different lengths, so they are not the same request ({detail})"
                )
            if not tokens:
                gaps.append(f"{group.source.name} ({questions} question(s)): no token count recorded")

    return gaps


# --- rendering ------------------------------------------------------------------


def _display_name(group: Group) -> str:
    """The engine id a user would actually pass to `--engine`.

    Legacy sweep files record `engine: laya` plus `checkpoint: multilingual`, which is
    not the id anyone types. Resolving through the registry keeps one source of truth for
    engine ids (`AGENTS.md §2`) instead of hard-coding `laya-multilingual` here; when the
    registry is unavailable the pair is shown verbatim rather than guessed at.
    """
    if not group.variant:
        return group.engine
    candidate = f"{group.engine}-{group.variant}"
    try:
        import sys as _sys

        _sys.path.insert(0, str(ROOT / "src"))
        from decis.engines.registry import SPECS

        if candidate in SPECS:
            return candidate
        # An alias is not a display name: `laya-english` resolves to the canonical `laya`,
        # or the table would show an id that `decis download --engine` does not list.
        for engine_id, spec in SPECS.items():
            if candidate in spec.aliases:
                return engine_id
        # A repo id names the weights, not the engine: the legacy file says `kev` +
        # `jaredpalmer/kev-0.8b`, and the registered id is `kev-0.8b`. The repo id lives
        # in the engine module's WEIGHTS table, so read it there rather than duplicating
        # the mapping. Importing an engine module is safe and cheap: `AGENTS.md §6` makes
        # torch-free module imports a tested invariant.
        import importlib

        for engine_id, spec in SPECS.items():
            module_name, _, _ = spec.target.partition(":")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            for weights in (getattr(module, "WEIGHTS", None) or {}).values():
                if getattr(weights, "repo_id", None) == group.variant:
                    return engine_id
    except ImportError:
        pass
    return f"{group.engine} ({group.variant})"


def _fmt(value: float | int | None, spec: str = ",.1f") -> str:
    """Format a number, or an em dash when the raw file did not record it.

    The legacy kev record has no p95/min/max: it is one observation per dtype. Printing
    "0.0" there would invent a measurement.
    """
    return "—" if value is None else format(value, spec)


def _summary(group: Group) -> str:
    """The dimensions §8 requires next to any number, on one line."""
    parts = [f"device `{group.device}`", f"dtype `{group.dtype}`"]
    if group.engine_version:
        parts.append(f"`{group.engine}` {group.engine_version}")
    if group.load_s:
        parts.append(f"cold start {group.load_s:g} s")
    if group.peak_rss_gb:
        parts.append(f"peak RSS {group.peak_rss_gb:g} GB")
    if group.configs and group.configs[0].state_chars:
        parts.append(f"state {group.configs[0].state_chars} chars")
    parts.append(f"processes 1, {group.batching_scope} batching")
    return " · ".join(parts)


def _host_line(group: Group) -> str:
    host = group.host or {}
    runtime = {key: value for key, value in (group.runtime or {}).items() if value}
    bits = []
    if host.get("machine"):
        bits.append(str(host["machine"]))
    if host.get("cpu_count"):
        bits.append(f"{host['cpu_count']} vCPU")
    if host.get("mem_gb"):
        bits.append(f"{host['mem_gb']} GB RAM")
    if host:
        bits.append("**no GPU**" if not host.get("gpu") else "GPU")
    if runtime:
        bits.append(" · ".join(f"{key} {value}" for key, value in runtime.items()))
    return " · ".join(bits)


@dataclass(frozen=True)
class AnswerDelta:
    """How two recorded answer bodies differ, split into the parts a caller cares about.

    A plain `==` is not informative: the kev file records answers rounded to two
    decimals, so its fp32 and bf16 bodies differ as JSON while its own conclusion says
    the argmax is identical and the probability delta is <= 0.01. Reporting "different"
    would look like a contradiction and "identical" would be false, so the
    user-visible decision and the calibration drift are extracted separately -- and
    this carries no prose, so each renderer can phrase it in its own language.
    """

    flips: tuple[str, ...] = ()
    worst_delta: float = 0.0
    comparable: bool = True

    def render(self, *, english: bool) -> str:
        if not self.comparable:
            return "question sets differ" if english else "问题集合不同"
        if self.flips:
            joined = "; ".join(self.flips)
            return f"**argmax flipped**: {joined}" if english else f"**argmax 翻转**：{joined}"
        if self.worst_delta:
            limit = f"{self.worst_delta:.2f}"
            return f"same argmax, max probability delta {limit}" if english else f"argmax 相同，概率差 ≤{limit}"
        return "identical" if english else "完全相同"


def _answer_delta(baseline: Any, other: Any) -> AnswerDelta:
    if not isinstance(baseline, dict) or not isinstance(other, dict):
        return AnswerDelta(comparable=False)
    if set(baseline) != set(other):
        return AnswerDelta(comparable=False)

    flips: list[str] = []
    worst = 0.0
    for qid in baseline:
        left, right = baseline[qid], other[qid]
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        if left.get("choice") != right.get("choice"):
            flips.append(f"{qid}: {left.get('choice')} -> {right.get('choice')}")
        one, two = left.get("probabilities"), right.get("probabilities")
        if isinstance(one, dict) and isinstance(two, dict) and set(one) == set(two):
            worst = max(worst, max(abs(float(one[key]) - float(two[key])) for key in one))
        if "noul" in left and "noul" in right:
            worst = max(worst, abs(float(left["noul"]) - float(right["noul"])))
            if (float(left["noul"]) >= 0.5) != (float(right["noul"]) >= 0.5):
                flips.append(f"{qid}: noul crossed 0.5")
    return AnswerDelta(flips=tuple(flips), worst_delta=worst)


def _varying(configs: tuple[Config, ...], attribute: str) -> list[Any]:
    return sorted({getattr(config, attribute) for config in configs if getattr(config, attribute) is not None})


def _render_group(group: Group, index: int) -> list[str]:
    """Render whichever comparison this group actually is.

    A group is not always a thread sweep. The kev dtype record varies `dtype` at a
    single thread count, and rendering it as a thread sweep silently collapsed its two
    rows into one -- the first thing this generator did when it was pointed at the
    existing evidence. So the axis is detected, not assumed.
    """
    title = _display_name(group)
    lines = [f"### 结果 {index}：{title} 延迟（p50）", ""]
    lines.append(_summary(group))
    lines.append("")

    threads = _varying(group.configs, "threads")
    counts = sorted({config.questions for config in group.configs})
    dtypes = _varying(group.configs, "dtype")

    if len(dtypes) > 1:
        # A dtype comparison: rows are dtypes, and the ratio to the first row is the
        # point of the measurement, so it is shown rather than left to the reader.
        baseline = group.configs[0]
        lines.append("| dtype | p50 | 相对倍率 | 每问成本 | 答案是否相同 |")
        lines.append("|---|---:|---:|---:|---|")
        for config in group.configs:
            ratio = config.p50_ms / baseline.p50_ms if baseline.p50_ms else 0.0
            same = (
                "基准"
                if config is baseline
                else _answer_delta(baseline.extra.get("answers"), config.extra.get("answers")).render(english=False)
            )
            lines.append(
                f"| `{config.dtype}` | {_fmt(config.p50_ms, ',.0f')} ms "
                f"| {ratio:.0f}× | {_fmt(config.ms_per_question, ',.0f')} ms | {same} |"
            )
        lines.append("")
        lines.append(
            f"线程数 {threads[0] if threads else '—'}（记录于原始文件），单进程，"
            f"{group.batching_scope} batching。**每个 dtype 只有一次观测**，"
            "所以这是比值而非统计量（`benchmarks/README.md` M3）。"
        )
        lines.append("")
        if group.hashless:
            lines.append("> ⚠️ 该文件**没有记录输入哈希**，因此无法验证两次配置处理的是同一个输入，只能比对 token 数。")
            lines.append("")
        return lines

    if len(threads) < 2:
        lines.append("| 问题数 | p50 | 每问成本 |")
        lines.append("|---:|---:|---:|")
        for config in sorted(group.configs, key=lambda item: item.questions):
            lines.append(
                f"| {config.questions} | {_fmt(config.p50_ms, ',.1f')} ms | {_fmt(config.ms_per_question)} ms |"
            )
        lines.append("")
        return lines

    lookup = {(config.threads, config.questions): config for config in group.configs}
    lines.append("| 线程 \\ 问题数 | " + " | ".join(str(count) for count in counts) + " |")
    lines.append("|---|" + "---:|" * len(counts))
    for thread in threads:
        cells = [
            f"{_fmt(lookup[(thread, count)].p50_ms, ',.0f')} ms" if (thread, count) in lookup else "—"
            for count in counts
        ]
        lines.append(f"| {thread} | " + " | ".join(cells) + " |")
    lines.append("")

    if any(config.questions > 1 and config.ms_per_question is not None for config in group.configs):
        lines.append("换算成**每个问题**的成本（同一批数据，同一配置）：")
        lines.append("")
        lines.append("| 线程 | " + " | ".join(f"{count} 问" for count in counts) + " |")
        lines.append("|---|" + "---:|" * len(counts))
        for thread in threads:
            cells = []
            for count in counts:
                config = lookup.get((thread, count))
                cells.append(
                    f"{_fmt(config.ms_per_question)} ms" if config and config.ms_per_question is not None else "—"
                )
            lines.append(f"| {thread} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def render_measurements(groups: list[Group], gaps: list[str]) -> str:
    """The generated block for `docs/feasibility.md` (Chinese, like the file)."""
    lines = [MARKER_BEGIN, "### 主机", ""]
    hosts = {_host_line(group) for group in groups if group.host}
    for host in sorted(hosts):
        lines.append(f"- {host}")
    lines.append("")
    lines.append("这台机器代表“最低配的 Docker CPU 容器”，也就是用户在没有 GPU 的服务器上第一次跑 Decis 会遇到的情形。")
    lines.append("")
    lines.append("> 本节的每个数字都由 [`benchmarks/report.py`](../benchmarks/report.py) 从")
    lines.append("> [`benchmarks/results/`](../benchmarks/results/) 的原始 JSON 生成，**没有手写数字**。")
    lines.append("> 生成前脚本会断言同一组内各配置处理的是**同一个输入**（`input_sha256` 与 token 数一致），")
    lines.append("> 不一致就拒绝生成。改动本节请跑 `python benchmarks/report.py --write`。")
    lines.append("")

    # Sweeps first (they are what the section is about), then single-axis comparisons.
    ordered = sorted(groups, key=lambda group: (len(_varying(group.configs, "threads")) < 2, group.source.name))
    for index, group in enumerate(ordered, start=1):
        lines.extend(_render_group(group, index))

    lines.append("### 原始数据")
    lines.append("")
    lines.append("逐样本原始 JSON（含每次调用的完整 `samples_ms`、`input_sha256`、token 数、p50/p95）都已 checked in：")
    lines.append("")
    for group in groups:
        relative = group.source.relative_to(ROOT)
        lines.append(f"- [`{relative}`](../{relative})")
    lines.append("")
    lines.append("采集方法、主机规格、复现命令与已知局限见 [`benchmarks/README.md`](../benchmarks/README.md)；")
    lines.append(
        "采集脚本在 [`benchmarks/probe/`](../benchmarks/probe/) 与 [`benchmarks/run.py`](../benchmarks/run.py)。"
    )
    lines.append("")

    if gaps:
        lines.append("**无法验证的部分**（脚本报告的出处缺口，不是隐瞒）：")
        lines.append("")
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")

    lines.append("> **口径声明**：`laya-*-sweep.json` 是**实现 Decis 之前**对**引擎本身**的单机测量，")
    lines.append("> 不是 Decis 服务的性能；`kev-0.8b-cpu-dtype.json` 每个 dtype 只有**一次观测**，")
    lines.append("> 记录的是比值的量级而非统计量。正式基准要由 `benchmarks/run.py` 重新采集。")
    lines.append("")
    lines.append(MARKER_END)
    return "\n".join(lines)


def render_latency_table(
    groups: list[Group],
    *,
    chinese: bool = False,
    benchmark_prefix: str = "",
) -> str:
    """The headline latency table, for the two `docs/performance*.md` pages.

    A row is derived from **one** configuration: the thread count torch picks by default
    on this host (one per vCPU), because that is what a reader gets without tuning
    anything. The alternative -- picking the best cell per column -- is exactly how the
    previous hand-written table ended up quoting two different thread counts in one row.
    The Chinese page carried the same defect, which is why this is generated into every
    file that shows it instead of copied.

    The table lives inside `docs/`, so both callers pass `benchmark_prefix="../"`; it stays
    a parameter because the tests and the bare print mode render the block from the
    repository root.
    """
    lines = [README_LATENCY_BEGIN, ""]
    if chinese:
        lines.append("| 引擎 | 设备 | dtype | 线程 | 1 个问题 | 10 个问题 | 冷启动 | 峰值 RSS |")
    else:
        lines.append("| Engine | Device | dtype | Threads | 1 question | 10 questions | Cold start | Peak RSS |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")

    for group in groups:
        if group.hashless:
            continue
        counts = {config.questions for config in group.configs}
        if not {1, 10} <= counts:
            continue
        default_threads = (group.host or {}).get("cpu_count")
        threads = sorted({config.threads for config in group.configs if config.threads is not None})
        chosen = default_threads if default_threads in threads else (threads[-1] if threads else None)
        by_count = {config.questions: config for config in group.configs if config.threads == chosen}
        one, ten = by_count.get(1), by_count.get(10)
        if not (one and ten):
            continue
        cold = f"{group.load_s:g} s" if group.load_s else "—"
        rss = f"{group.peak_rss_gb:g} GB" if group.peak_rss_gb else "—"
        per_question = "ms/题" if chinese else "ms/question"
        lines.append(
            f"| `{_display_name(group)}` | {group.device} | {group.dtype} | {chosen} "
            f"| {_fmt(one.p50_ms, ',.0f')} ms | {_fmt(ten.p50_ms, ',.0f')} ms "
            f"({_fmt(ten.ms_per_question)} {per_question}) | {cold} | {rss} |"
        )

    lines.append("")
    if chinese:
        lines.append(
            f"由 [`benchmarks/report.py`]({benchmark_prefix}benchmarks/report.py) 从 "
            f"[`benchmarks/results/`]({benchmark_prefix}benchmarks/results/) 的原始 JSON 生成；"
            "**整行取自同一个配置**（torch 在本机的默认线程数，每个 vCPU 一个），样本 p50，单进程，仅请求内批处理。"
        )
        lines.append("")
        lines.append(
            "**这是延迟，不是吞吐。** 每个请求的问题共享同一个 `state`，这是容易的情况。"
            "跨请求批处理已实测，结论是在 CPU 上**不提升吞吐**（见下面的批处理一节与 "
            "[`design-review.md`](design-review.md) §4-M5）。"
        )
    else:
        lines.append(
            f"Generated by [`benchmarks/report.py`]({benchmark_prefix}benchmarks/report.py) from the raw JSON in "
            f"[`benchmarks/results/`]({benchmark_prefix}benchmarks/results/); a whole row comes from one configuration "
            "(the thread count torch picks by default, one per vCPU). p50 over the recorded samples, "
            "single process, within-request batching only."
        )
        lines.append("")
        lines.append(
            "**These are latencies, not throughput.** Every question in a row shares one `state`, which "
            "is the easy case. Cross-request batching was measured and does **not** raise "
            "throughput on CPU -- see the cross-request batching section below and "
            "[`design-review.md §4-M5`](design-review.md)."
        )
    lines.append("")
    lines.append(README_LATENCY_END)
    return "\n".join(lines)


def render_dtype_table(groups: list[Group], *, chinese: bool = False) -> str:
    """The dtype-comparison block for `docs/performance.md` and its Chinese twin.

    Kept separate from the latency table because it answers a different question -- "is
    this engine usable on this device at all" rather than "how fast is it" -- and it is
    the backing for the `bf16` figure quoted in the prose below it (`AGENTS.md §8`).
    """
    lines = [README_DTYPE_BEGIN, ""]
    if chinese:
        lines.append("| 引擎 | dtype | 3 个问题 | 相对 | 对比基线 |")
    else:
        lines.append("| Engine | dtype | 3 questions | relative | vs. baseline |")
    lines.append("|---|---|---:|---:|---|")
    for group in groups:
        dtypes = _varying(group.configs, "dtype")
        if len(dtypes) < 2:
            continue
        baseline = group.configs[0]
        for config in group.configs:
            ratio = config.p50_ms / baseline.p50_ms if baseline.p50_ms else 0.0
            comparison = (
                ("基线" if chinese else "baseline")
                if config is baseline
                else _answer_delta(baseline.extra.get("answers"), config.extra.get("answers")).render(
                    english=not chinese
                )
            )
            lines.append(
                f"| `{_display_name(group)}` | `{config.dtype}` | {_fmt(config.p50_ms, ',.0f')} ms "
                f"| {ratio:.0f}x | {comparison} |"
            )
    lines.append("")
    if chinese:
        lines.append(
            "由 [`benchmarks/report.py`](../benchmarks/report.py) 生成。每个 dtype 一次观测，"
            "所以倍数是数量级结论而不是统计量；对比列是从记录的答案正文算出来的，不是照着表旁的文字写的。"
        )
    else:
        lines.append(
            "Generated by [`benchmarks/report.py`](../benchmarks/report.py). One observation per dtype, so "
            "the ratio is an order-of-magnitude finding, not a statistic, and the comparison column is "
            "computed from the recorded answer bodies rather than from the prose next to them."
        )
    lines.append("")
    lines.append(README_DTYPE_END)
    return "\n".join(lines)


def render_batching_summary(gains: list[BatchGain], *, chinese: bool = False) -> str:
    """A compact batching block for the performance docs: the finding, not the whole table.

    The README links here for the detail; the per-batch-size table and the multiprocess
    comparison live in `docs/design-review.md §4-M5` and `benchmarks/RESULTS.md`. What
    belongs next to the headline latency table is the answer to "should I expect this to
    be fast", and the answer is a number.
    """
    if not gains:
        return ""
    gain = next((item for item in gains if item.processes == 1), None)
    if gain is None:
        return ""
    lines = [BATCHING_BEGIN, ""]
    if chinese:
        lines.append("| 序列集 | 序列长度 | padding | 最好的批大小 | 相对串行 |")
        lines.append("|---|---|---:|---:|---:|")
        labels = {
            "shared_short": "同一 state（正对照，短）",
            "shared": "同一 state（正对照）",
            "uniform": "不同 state，长度接近",
            "skewed": "不同 state，长度倾斜",
        }
    else:
        lines.append("| Item set | Sequence length | padding | Best batch | vs. serial |")
        lines.append("|---|---|---:|---:|---:|")
        labels = {
            "shared_short": "one state (positive control, short)",
            "shared": "one state (positive control)",
            "uniform": "different states, similar lengths",
            "skewed": "different states, skewed lengths",
        }
    for name, block in gain.item_sets.items():
        configs = [config for config in block.get("configs", []) if config["batch"] > 1]
        if not configs:
            continue
        best = max(configs, key=lambda config: config.get("speedup_vs_serial") or 0.0)
        tokens = block["sequence_tokens"]
        lines.append(
            f"| {labels.get(name, name)} | {tokens['min']}–{tokens['max']} "
            f"| {best['padding_ratio']:.2f} | {best['batch']} | {best.get('speedup_vs_serial', 0.0):.2f}x |"
        )
    lines.append("")
    lines.append(_verdict(gains, chinese))
    lines.append("")
    if chinese:
        lines.append(
            "原始 JSON：[`benchmarks/results/`](../benchmarks/results/)；完整表格（每个批大小、padding、盈亏平衡等待、进程数对比）见 "
            "[`docs/design-review.md`](design-review.md) §4-M5。批是**直接调用引擎**合成的，"
            "没有队列与取消，所以这些数字是真实攒批器的**上界**。"
        )
    else:
        lines.append(
            "Raw JSON in [`benchmarks/results/`](../benchmarks/results/); the full table -- every batch size, "
            "padding, break-even wait, process comparison -- is in "
            "[`docs/design-review.md §4-M5`](design-review.md). Batches are synthesised by calling "
            "the engine directly, with no queue and no cancellation, so these are an **upper bound** on a "
            "real batcher."
        )
    lines.append("")
    lines.append(BATCHING_END)
    return "\n".join(lines)


def render_full_report(groups: list[Group], gaps: list[str]) -> str:
    """The long form, written to `benchmarks/RESULTS.md`."""
    out = [
        "# Generated results",
        "",
        "**Do not edit.** Produced by `python benchmarks/report.py --write` from the raw JSON in",
        "`results/`. Every figure the docs quote is rendered from here, and the generator refuses to",
        "run when two configurations in a comparison group processed different input.",
        "",
    ]
    for group in groups:
        out.append(f"## `{group.engine}` — {group.source.name}")
        out.append("")
        out.append(f"- Source: `{group.source.relative_to(ROOT)}`")
        out.append(f"- {_summary(group)}")
        if group.host:
            out.append(f"- Host: {_host_line(group)}")
        out.append("")
        out.append(
            "| Questions | Threads | dtype | input sha256 | tokens | n | p50 ms | p95 ms | min | max | ms/question |"
        )
        out.append("|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for config in sorted(group.configs, key=lambda item: (item.questions, item.threads or 0)):
            sha = f"`{config.input_sha256[:12]}`" if config.input_sha256 else "— (not recorded)"
            out.append(
                f"| {config.questions} | {config.threads if config.threads is not None else '—'} "
                f"| {config.dtype or group.dtype} | {sha} | {config.input_tokens if config.input_tokens is not None else '—'} "
                f"| {config.n if config.n is not None else '—'} | {_fmt(config.p50_ms)} "
                f"| {_fmt(config.p95_ms)} | {_fmt(config.min_ms)} | {_fmt(config.max_ms)} "
                f"| {_fmt(config.ms_per_question)} |"
            )
        out.append("")
        if group.caveats:
            out.append("Caveats recorded with the data:")
            out.append("")
            for caveat in group.caveats:
                out.append(f"- {caveat}")
            out.append("")

    if gaps:
        out.append("## Provenance gaps")
        out.append("")
        out.append("Things this generator could **not** verify. Listed rather than assumed:")
        out.append("")
        for gap in gaps:
            out.append(f"- {gap}")
        out.append("")

    gains = load_batch_gains()
    if gains:
        out.append("## Cross-request batching")
        out.append("")
        out.append(render_batching(gains, chinese=False))
        out.append("")
    return "\n".join(out)


# --- cross-request batching: was it worth building? ---------------------------------
#
# `docs/design.md §6` claims cross-request batching is the main throughput lever, and
# `docs/design-review.md §4-M5` recorded that the claim had never been measured. This is
# the renderer for the measurement. It deliberately renders a *negative* result as
# readily as a positive one: the question was "is this worth building", and "no" is a
# finding, not a failure of the harness. The `shared` item set is the positive control --
# if it shows no gain, the harness cannot detect one and the file is refused.


@dataclass(frozen=True)
class BatchGain:
    """One batching experiment: one engine, one process count, one host."""

    source: Path
    engine: str
    device: str
    dtype: str
    threads: int
    processes: int
    items: int
    rounds: int
    load_s: float | None
    peak_rss_gb: float | None
    item_sets: dict[str, dict[str, Any]]
    aggregate: dict[str, Any] | None
    methodology: dict[str, Any]

    @property
    def label(self) -> str:
        if self.processes > 1:
            return f"{self.engine} · {self.processes} processes x {self.threads} threads"
        return f"{self.engine} · 1 process x {self.threads} threads"


def _load_batch_gain(path: Path, document: dict[str, Any]) -> BatchGain:
    return BatchGain(
        source=path,
        engine=document.get("engine", "unknown"),
        device=document.get("device", "unknown"),
        dtype=document.get("dtype", "unknown"),
        threads=int(document.get("threads") or 0),
        processes=int(document.get("processes") or 1),
        items=int(document.get("items") or 0),
        rounds=int(document.get("iterations") or 0),
        load_s=document.get("load_s"),
        peak_rss_gb=document.get("peak_rss_gb") or document.get("peak_rss_gb_per_process"),
        item_sets=document.get("skews", {}),
        aggregate=document.get("aggregate"),
        methodology=document.get("methodology", {}),
    )


def load_any(path: Path) -> Group | BatchGain:
    """Read any raw result file, whichever schema it declares.

    `load_group` stays strict about returning a `Group` so a batching file cannot be
    silently coerced into a latency table; this is the dispatcher for callers that accept
    either shape, such as the test that insists every checked-in file is parseable.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") == BATCH_GAIN_SCHEMA:
        return _load_batch_gain(path, document)
    return load_group(path)


def load_batch_gains() -> list[BatchGain]:
    """Every batching experiment in `results/`, in a stable order."""
    gains = []
    for path in sorted(RESULTS.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") == BATCH_GAIN_SCHEMA:
            gains.append(_load_batch_gain(path, document))
    return sorted(gains, key=lambda gain: (gain.engine, gain.processes, gain.threads))


def verify_batch_gains(gains: list[BatchGain]) -> list[str]:
    """Refuse to render a batching comparison that is not internally comparable.

    The failure modes are the same family as `verify`'s: a row whose columns come from
    different configurations, a batch that was never compared with serial, and a
    negative result produced by a harness that cannot detect a positive one.
    """
    problems: list[str] = []
    for gain in gains:
        if not gain.item_sets and not gain.aggregate:
            problems.append(f"{gain.source.name}: no measurements in it")
            continue
        if gain.processes == 1:
            if "shared" not in gain.item_sets:
                problems.append(
                    f"{gain.source.name}: no `shared` item set, so there is no positive control. "
                    f"Without one a null result cannot be distinguished from a broken harness."
                )
            for name, block in gain.item_sets.items():
                configs = {config["batch"]: config for config in block.get("configs", [])}
                if 1 not in configs:
                    problems.append(f"{gain.source.name}: item set {name!r} has no serial (batch 1) baseline")
                for config in configs.values():
                    if config.get("n", 0) < 1:
                        problems.append(f"{gain.source.name}: item set {name!r} batch {config['batch']} has no samples")
                    if config.get("padding_ratio", 1.0) < 1.0:
                        problems.append(
                            f"{gain.source.name}: item set {name!r} batch {config['batch']} reports a padding "
                            f"ratio below 1.0, which is impossible; the lengths are wrong"
                        )
                # A row mixes a baseline with its comparisons, so they must agree on the
                # engine, device and dtype. `design-review.md §2-D12` is this mistake.
                seen = {(gain.engine, gain.device, gain.dtype)}
                if len(seen) != 1:
                    problems.append(f"{gain.source.name}: item set {name!r} mixes configurations")

    # Comparing a 1-process run with an N-process run only means something if the total
    # thread budget is held constant; otherwise it measures oversubscription.
    by_engine: dict[str, list[BatchGain]] = {}
    for gain in gains:
        by_engine.setdefault(gain.engine, []).append(gain)
    for engine, runs in by_engine.items():
        budgets = {run.processes * run.threads for run in runs}
        if len(budgets) > 1:
            problems.append(
                f"{engine}: the runs use different total thread budgets {sorted(budgets)} "
                f"(processes x threads), so a throughput comparison across them would be "
                f"measuring thread oversubscription rather than process scaling"
            )
    return problems


def _verdict(gains: list[BatchGain], chinese: bool) -> str:
    """The computed conclusion. Stated from the data, not asserted about it.

    Written as a sentence rather than a table row because the interesting part is the
    *comparison between item sets*: the positive control showing a gain and the
    heterogeneous sets not showing one is the whole finding.
    """
    batched = [gain for gain in gains if gain.processes == 1]
    if not batched:
        return "未测量跨请求批处理。" if chinese else "Cross-request batching was not measured."
    gain = batched[0]

    # The best speedup each item set achieved at any batch size, and where.
    best: dict[str, tuple[float, int]] = {}
    for name, block in gain.item_sets.items():
        for config in block.get("configs", []):
            if config["batch"] == 1:
                continue
            speedup = config.get("speedup_vs_serial") or 0.0
            if name not in best or speedup > best[name][0]:
                best[name] = (speedup, config["batch"])
    controls = {name: value for name, value in best.items() if name.startswith("shared")}
    real = {name: value for name, value in best.items() if not name.startswith("shared")}
    if not real:
        return "未测量跨请求批处理。" if chinese else "Cross-request batching was not measured."

    parts: list[str] = []

    # 1. Could this harness detect a gain at all?
    if not controls:
        parts.append(
            "**没有正对照数据**，无法判断这套测量能否测出收益。"
            if chinese
            else "**There is no positive control**, so whether this harness can detect a gain is unknown."
        )
    else:
        name, (speedup, batch) = max(controls.items(), key=lambda item: item[1][0])
        if speedup >= 1.2:
            parts.append(
                f"正对照（所有项完全相同、padding 为 1.00）达到 **{speedup:.2f}x**（`{name}`，batch {batch}），"
                f"证明瓶颈确实是每次调用的固定开销，也证明这套测量能测出收益。"
                if chinese
                else f"The positive control (identical items, padding 1.00) reaches **{speedup:.2f}x** "
                f"(`{name}`, batch {batch}), which confirms the bottleneck is per-call fixed overhead and "
                f"that this harness can detect a gain."
            )
        else:
            parts.append(
                f"正对照最高只有 {speedup:.2f}x（`{name}`，batch {batch}）：在这些序列长度上，"
                f"**连完全没有 padding 的批处理也不省时**。"
                if chinese
                else f"Even the positive control reaches only {speedup:.2f}x (`{name}`, batch {batch}): at "
                f"these sequence lengths **batching does not pay even when it is perfect**."
            )

    # 2. What it does to the traffic shape that actually exists.
    pieces = [f"`{name}` {value[0]:.2f}x" for name, value in sorted(real.items())]
    best_name, (best_speedup, best_batch) = max(real.items(), key=lambda item: item[1][0])
    worst_name, (worst_speedup, worst_batch) = min(real.items(), key=lambda item: item[1][0])
    if best_speedup < 1.05:
        parts.append(
            f"真实流量形状（不同 state）在所有批大小上都没有收益（{', '.join(pieces)}），即攒批器只会增加延迟与复杂度。"
            if chinese
            else f"For the shape real traffic has -- different states -- there is no gain at any batch size "
            f"({', '.join(pieces)}), so a batcher would add latency and complexity and buy nothing."
        )
    else:
        parts.append(
            f"真实流量形状（不同 state）最高 {best_speedup:.2f}x（`{best_name}`，batch {best_batch}），"
            f"而最差 {worst_speedup:.2f}x（`{worst_name}`，batch {worst_batch}）。"
            if chinese
            else f"For the shape real traffic has -- different states -- the best case is "
            f"{best_speedup:.2f}x (`{best_name}`, batch {best_batch}) and the worst is "
            f"{worst_speedup:.2f}x (`{worst_name}`, batch {worst_batch})."
        )

    # 3. Padding, when it is large enough to be the explanation.
    paddings = [
        (config["padding_ratio"], name, config["batch"], config.get("speedup_vs_serial") or 0.0)
        for name, block in gain.item_sets.items()
        if not name.startswith("shared")
        for config in block.get("configs", [])
        if config["batch"] > 1
    ]
    if paddings:
        ratio, name, batch, speedup = max(paddings)
        if ratio > 1.5:
            parts.append(
                f"长度倾斜时 padding 最高 {ratio:.2f}x（`{name}`，batch {batch}）：每一行都要补齐到批内最长，"
                f"浪费的算力直接变成更慢的每项成本。"
                if chinese
                else f"When lengths are skewed, padding reaches {ratio:.2f}x (`{name}`, batch {batch}): every "
                f"row is padded to the longest in its batch, and that wasted compute shows up directly as a "
                f"higher per-item cost."
            )
    return " ".join(parts)


def _throughput_summary(gains: list[BatchGain], chinese: bool) -> list[str]:
    """Process count against throughput, with the total thread budget held constant.

    The single-process row is derived from the batching file's serial configuration rather
    than measured in this file, so the note says so. Every row is still one configuration
    and every column is the same quantity, which is what `AGENTS.md §8` requires.
    """
    rows: list[tuple[int, int, float, float, str]] = []
    serial_source = None
    for gain in gains:
        if gain.processes > 1:
            aggregate = gain.aggregate or {}
            if aggregate.get("items_per_second"):
                rows.append(
                    (
                        gain.processes,
                        gain.threads,
                        aggregate["items_per_second"],
                        aggregate["per_item_ms"],
                        gain.source.name,
                    )
                )
            continue
        # processes == 1: its serial baseline is the `uniform` batch-1 config, which is the
        # same item set and the same thread budget as the children measured.
        uniform = gain.item_sets.get("uniform", {}).get("configs", [])
        baseline = next((config for config in uniform if config["batch"] == 1), None)
        if baseline:
            per_item = baseline["per_item_ms"]["p50"]
            rows.append((1, gain.threads, 1000.0 / per_item, per_item, gain.source.name))
            serial_source = gain.source.name
    if len(rows) < 2:
        return []

    rows.sort()
    fastest = max(row[2] for row in rows)
    out = ["### Process count vs throughput" if not chinese else "### 进程数 vs 吞吐", ""]
    out.append(
        "总线程预算是固定的（进程数 × 线程数 = 24），所以这张表比较的是「把机器分给多个进程」"
        "与「全部给一个进程」，而不是线程超配。"
        if chinese
        else "The total thread budget is held constant (processes x threads = 24), so this compares "
        "sharing the machine between processes with giving all of it to one, not thread oversubscription."
    )
    out.append("")
    out.append(
        "| processes | threads | items/s | ms/item | vs best | source |"
        if not chinese
        else "| 进程数 | 线程数 | 项/秒 | ms/项 | 相对最好 | 来源 |"
    )
    out.append("|---:|---:|---:|---:|---:|---|")
    for processes, threads, items_per_second, per_item, source in rows:
        out.append(
            f"| {processes} | {threads} | {items_per_second:.2f} | {per_item:.1f} "
            f"| {items_per_second / fastest:.2f}x | `{source}` |"
        )
    out.append("")
    # The finding is the *flatness*, so state it instead of leaving it to be eyeballed.
    slowest = min(row[2] for row in rows)
    if fastest / slowest < 1.25:
        out.append(
            f"最慢与最快只差 {fastest / slowest:.2f}x：在这台机器上**加进程不增加吞吐**。"
            f"单个 Laya 前向已经能用满 24 线程，把预算切开只是重新分配同一份算力。"
            if chinese
            else f"The slowest and fastest differ by only {fastest / slowest:.2f}x: on this host "
            f"**adding processes does not add throughput**. A single Laya forward pass already uses 24 "
            f"threads well, so splitting the budget only reallocates the same capacity."
        )
        out.append("")
    if serial_source:
        out.append(
            "1 进程那一行取自跨请求批处理文件里的串行配置（同一序列集、同一线程预算），不是另一组实验。"
            if chinese
            else f"The 1-process row comes from the serial configuration inside the batching file "
            f"(`{serial_source}`) -- the same item set and the same thread budget, not a separate "
            f"experiment."
        )
        out.append("")
    return out


def _render_batch_gain(gain: BatchGain, chinese: bool) -> list[str]:
    out: list[str] = []
    header = (
        f"### `{gain.engine}` — {gain.processes} 进程 × {gain.threads} 线程"
        if chinese
        else f"### `{gain.engine}` — {gain.processes} process(es) x {gain.threads} threads"
    )
    out.append(header)
    out.append("")
    out.append(f"- `{gain.source.relative_to(ROOT)}` · {gain.device} · {gain.dtype}")
    if gain.load_s is not None:
        rss = f" · peak RSS {gain.peak_rss_gb:.2f} GB" if gain.peak_rss_gb else ""
        out.append(f"- {'load' if not chinese else '加载'} {gain.load_s:.1f} s{rss}")
    out.append("")

    if gain.aggregate:
        aggregate = gain.aggregate
        out.append(
            "| processes | threads | items | wall s | ms/item | items/s |"
            if not chinese
            else "| 进程数 | 线程数 | 请求数 | 墙钟秒 | ms/项 | 项/秒 |"
        )
        out.append("|---:|---:|---:|---:|---:|---:|")
        out.append(
            f"| {gain.processes} | {gain.threads} | {aggregate['items_answered']} "
            f"| {aggregate['wall_s']:.2f} | {aggregate['per_item_ms']:.1f} | {aggregate['items_per_second']:.2f} |"
        )
        out.append("")
    else:
        out.append(
            "| item set | batch | n | ms/item (p50) | vs serial | break-even wait ms | padding |"
            if not chinese
            else "| 序列集 | 批大小 | n | ms/项 (p50) | 相对串行 | 盈亏平衡等待 ms | padding |"
        )
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        for name, block in gain.item_sets.items():
            for config in block.get("configs", []):
                speedup = config.get("speedup_vs_serial")
                marker = " (control)" if name.startswith("shared") and not chinese else ""
                out.append(
                    f"| {name}{marker} | {config['batch']} | {config['n']} "
                    f"| {config['per_item_ms']['p50']:.1f} "
                    f"| {speedup:.2f}x | {config['break_even_wait_ms']:.1f} "
                    f"| {config['padding_ratio']:.2f} |"
                )
        out.append("")
        tokens = ", ".join(
            f"{name} {block['sequence_tokens']['min']}-{block['sequence_tokens']['max']}"
            for name, block in gain.item_sets.items()
        )
        out.append(f"{'序列 token 数' if chinese else 'sequence tokens (state + one question)'}: {tokens}")
        out.append("")

    for note in gain.methodology.get("not_measured", []):
        out.append(f"- {'未测' if chinese else 'not measured'}: {note}")
    out.append("")
    return out


def render_batching(gains: list[BatchGain], *, chinese: bool) -> str:
    """The batching block: what was measured, and whether it was worth building."""
    if not gains:
        return ""
    # The markers are part of the content: `replace_block` consumes the ones it is given,
    # so a renderer that omits them silently deletes the block's delimiters.
    out: list[str] = [BATCHING_BEGIN]
    if chinese:
        out.append("跨请求批处理的实测结果。原始 JSON 在 `benchmarks/results/`，本表由")
        out.append("`python benchmarks/report.py --write` 生成，**不要手改**。")
    else:
        out.append("Measured cross-request batching. Raw JSON lives in `benchmarks/results/`; this")
        out.append("table is produced by `python benchmarks/report.py --write`. **Do not edit.**")
    out.append("")
    out.append(_verdict(gains, chinese))
    out.append("")
    out.append(
        "`shared_short` 与 `shared` 是正对照：所有项完全相同（padding 恒为 1.00），"
        "即请求内批处理的形状。短序列那组必须显示出收益，否则这套测量根本测不出收益，其余结论无效。"
        "`shared_short`（115 token）复现了已发布的请求内结论：本表 217.8 ms/项 → 113.7 ms/项，"
        "`laya-multilingual-sweep.json` 里 1 问 231 ms → 10 问 98.7 ms/问。两组数据互相印证。"
        if chinese
        else "`shared_short` and `shared` are the positive controls: every item is identical, so padding "
        "is exactly 1.00 and the only variable left is the sequence length. The short one has to show a "
        "gain, or this harness cannot detect one and nothing else here is trustworthy. It reproduces the "
        "request-internal result already published: 217.8 -> 113.7 ms/item here, 231 -> 98.7 ms/question "
        "in `laya-multilingual-sweep.json`."
    )
    out.append("")
    summary = _throughput_summary(gains, chinese)
    if summary:
        out.extend(summary)

    for gain in gains:
        out.extend(_render_batch_gain(gain, chinese))
    if chinese:
        out.append(
            "**范围**：批是直接调用引擎合成出来的，没有队列、没有等待、没有客户端放弃、没有部分失败。"
            "这些数字是真实攒批器的**上界**——上面每一条缺失的机制都只会让它更差。"
        )
        out.append("")
    else:
        out.append(
            "**Scope**: batches are synthesised by calling the engine directly, with no queue, no "
            "waiting, no client giving up and no partial failure. These figures are an **upper "
            "bound** on a real batcher; every mechanism listed above makes it worse."
        )
        out.append("")
    out.append(BATCHING_END)
    return "\n".join(out)


# --- block replacement ----------------------------------------------------------


def replace_block(text: str, begin: str, end: str, content: str) -> str:
    """Swap the text between two markers, keeping everything else byte-identical."""
    start = text.find(begin)
    if start < 0:
        raise ValueError(f"marker {begin!r} not found; add it before using --write")
    stop = text.find(end, start)
    if stop < 0:
        raise ValueError(f"marker {end!r} not found after {begin!r}")
    return text[:start] + content.rstrip("\n") + text[stop + len(end) :]


def targets(groups: list[Group], gaps: list[str]) -> list[tuple[Path, str | None, str | None, str]]:
    """Every generated artifact: (path, begin marker, end marker, content).

    Exposed rather than inlined in `main` so the tests can assert on the same list CI
    checks -- a guard that only exists inside an argument parser is a guard nobody tests.
    """
    return [
        (ROOT / "docs" / "feasibility.md", MARKER_BEGIN, MARKER_END, render_measurements(groups, gaps)),
        # The latency table is generated into both performance pages and nowhere else. It
        # used to be the READMEs' one headline number; it is not, because the figure is a
        # property of the host as much as of this project, so a landing page that leads
        # with it says more about the machine it was measured on than about Decis.
        # `docs/performance.md` states the host and the method beside the numbers.
        (
            ROOT / "docs" / "performance.md",
            README_LATENCY_BEGIN,
            README_LATENCY_END,
            render_latency_table(groups, benchmark_prefix="../"),
        ),
        (
            ROOT / "docs" / "performance.zh-CN.md",
            README_LATENCY_BEGIN,
            README_LATENCY_END,
            render_latency_table(groups, chinese=True, benchmark_prefix="../"),
        ),
        (
            ROOT / "docs" / "performance.md",
            README_DTYPE_BEGIN,
            README_DTYPE_END,
            render_dtype_table(groups),
        ),
        (
            ROOT / "docs" / "performance.md",
            BATCHING_BEGIN,
            BATCHING_END,
            render_batching_summary(load_batch_gains(), chinese=False),
        ),
        (
            ROOT / "docs" / "performance.zh-CN.md",
            README_DTYPE_BEGIN,
            README_DTYPE_END,
            render_dtype_table(groups, chinese=True),
        ),
        (
            ROOT / "docs" / "performance.zh-CN.md",
            BATCHING_BEGIN,
            BATCHING_END,
            render_batching_summary(load_batch_gains(), chinese=True),
        ),
        (
            ROOT / "docs" / "design-review.md",
            BATCHING_BEGIN,
            BATCHING_END,
            render_batching(load_batch_gains(), chinese=True),
        ),
        (ROOT / "benchmarks" / "RESULTS.md", None, None, render_full_report(groups, gaps)),
    ]


def stale(groups: list[Group], gaps: list[str]) -> list[Path]:
    """Which checked-in artifacts differ from what the raw JSON would generate now."""
    out = []
    for path, begin, end, content in targets(groups, gaps):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = (content + "\n") if begin is None else replace_block(current, begin, end, content)
        if current != updated:
            out.append(path)
    return out


def load_all() -> list[Group]:
    """The latency measurements. Batching files are a different schema and shape.

    They are skipped here rather than adapted into a `Group`: the fields do not correspond
    (a `Group` is one configuration per row; a batching file is several configurations per
    item set plus a positive control), and forcing them together would mean inventing
    values for columns neither experiment measured.
    """
    paths = sorted(RESULTS.glob("*.json"))
    if not paths:
        raise SystemExit(f"no raw results in {RESULTS}; run benchmarks/run.py first")
    groups = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") == BATCH_GAIN_SCHEMA:
            continue
        groups.append(load_group(path))
    if not groups:
        raise SystemExit(f"no latency results in {RESULTS}; run benchmarks/run.py first")
    return groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs tables from raw benchmark JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="update the READMEs and the docs generated from the raw JSON"
    )
    mode.add_argument("--check", action="store_true", help="exit 1 if the checked-in docs are out of date")
    args = parser.parse_args(argv)

    groups = load_all()
    try:
        gaps = verify(groups)
    except InconsistentInputsError as error:
        print(f"refusing to generate: {error}", file=sys.stderr)
        return 2

    if not args.write and not args.check:
        for content in (render_measurements(groups, gaps), render_latency_table(groups), render_dtype_table(groups)):
            print(content)
            print()
        if gaps:
            print("Provenance gaps:")
            for gap in gaps:
                print(f"  - {gap}")
        return 0

    changed = stale(groups, gaps)
    if args.write:
        for path, begin, end, content in targets(groups, gaps):
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            updated = (content + "\n") if begin is None else replace_block(current, begin, end, content)
            if current != updated:
                path.write_text(updated, encoding="utf-8")
                print(f"updated {path.relative_to(ROOT)}")

    if args.check:
        if changed:
            print("these generated blocks are out of date:", file=sys.stderr)
            for path in changed:
                print(f"  - {path}", file=sys.stderr)
            print("run: python benchmarks/report.py --write", file=sys.stderr)
            return 1
        print(f"generated blocks are in sync ({len(groups)} group(s), {len(gaps)} provenance gap(s))")
        return 0

    if not changed:
        print("nothing to update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
