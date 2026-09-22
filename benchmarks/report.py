"""Generate the tables in the docs from the raw JSON in `benchmarks/results/`.

`AGENTS.md §8` makes two demands that this program exists to satisfy:

1. **No hand-written numbers.** Every figure in `README.md` and `docs/` that describes
   performance is rendered here, from a checked-in raw file.
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
    python benchmarks/report.py --write    # update README.md and docs/feasibility.md
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


def render_readme_latency(groups: list[Group]) -> str:
    """The latency block for `README.md` (English, like the file).

    A row is derived from **one** configuration: the thread count torch picks by
    default on this host (one per vCPU), because that is what a reader gets without
    tuning anything. The alternative -- picking the best cell per column -- is exactly
    how the previous hand-written table ended up quoting two different thread counts in
    one row.
    """
    lines = [README_LATENCY_BEGIN]
    lines.append("")
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
        lines.append(
            f"| `{_display_name(group)}` | {group.device} | {group.dtype} | {chosen} "
            f"| {_fmt(one.p50_ms, ',.0f')} ms | {_fmt(ten.p50_ms, ',.0f')} ms ({_fmt(ten.ms_per_question)} ms/question) "
            f"| {cold} | {rss} |"
        )

    lines.append("")
    lines.append(
        "Generated by [`benchmarks/report.py`](benchmarks/report.py) from the raw JSON in "
        "[`benchmarks/results/`](benchmarks/results/); a whole row comes from one configuration "
        "(the thread count torch picks by default, one per vCPU). p50 over the recorded samples, "
        "single process, within-request batching only."
    )
    lines.append("")
    lines.append(
        "**These are not throughput figures.** `laya-*-sweep.json` was measured against the engine "
        "before the Decis server existed, and cross-request batching -- the mechanism "
        "[`docs/design.md §6`](docs/design.md) relies on for throughput -- is not implemented yet, "
        "so nothing here can be quoted as QPS. See [`benchmarks/README.md`](benchmarks/README.md) M4/M5."
    )
    lines.append("")
    lines.append(README_LATENCY_END)
    return "\n".join(lines)


def render_readme_dtype(groups: list[Group]) -> str:
    """The dtype-comparison block for `README.md`.

    Kept separate from the latency table because it answers a different question -- "is
    this engine usable on this device at all" rather than "how fast is it" -- and it is
    the backing for the `bf16` figure quoted in the prose below it (`AGENTS.md §8`).
    """
    lines = [README_DTYPE_BEGIN, ""]
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
                "baseline"
                if config is baseline
                else _answer_delta(baseline.extra.get("answers"), config.extra.get("answers")).render(english=True)
            )
            lines.append(
                f"| `{_display_name(group)}` | `{config.dtype}` | {_fmt(config.p50_ms, ',.0f')} ms "
                f"| {ratio:.0f}x | {comparison} |"
            )
    lines.append("")
    lines.append(
        "Generated by [`benchmarks/report.py`](benchmarks/report.py). One observation per dtype, so "
        "the ratio is an order-of-magnitude finding, not a statistic, and the comparison column is "
        "computed from the recorded answer bodies rather than from the prose next to them."
    )
    lines.append("")
    lines.append(README_DTYPE_END)
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
        (ROOT / "README.md", README_LATENCY_BEGIN, README_LATENCY_END, render_readme_latency(groups)),
        (ROOT / "README.md", README_DTYPE_BEGIN, README_DTYPE_END, render_readme_dtype(groups)),
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
    paths = sorted(RESULTS.glob("*.json"))
    if not paths:
        raise SystemExit(f"no raw results in {RESULTS}; run benchmarks/run.py first")
    return [load_group(path) for path in paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs tables from raw benchmark JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="update README.md, docs/feasibility.md and benchmarks/RESULTS.md"
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
        for content in (render_measurements(groups, gaps), render_readme_latency(groups), render_readme_dtype(groups)):
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
