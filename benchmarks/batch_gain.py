"""Would cross-request batching actually be worth building? Measure before writing it.

`docs/design-review.md §4-M5` has said since the first review that Decis's central
performance claim -- joining questions from *different* requests into one forward pass --
has never been measured, while `README.md` is forbidden from quoting a number without it.
This script exists to answer that question with data instead of with an implementation.

The shape of the experiment matters, so it is stated here and repeated in the output:

* **Real traffic is one question per request, each with its own state.** The win that
  request-internal batching already gives ("ask ten questions about one document") does not
  apply to it. So the items here deliberately have **different states and different
  question lengths** -- the worst case for a batcher, because every row in a batch is
  padded to the longest one.
* **Batches are synthesised by calling the engine directly**, not by building a queue. That
  is an **upper bound**: a real batcher must also wait for items to arrive, handle a client
  that gives up mid-batch, and decide what to do when one item in a batch fails. None of
  that is in these numbers, and every one of them makes the real result *worse*.
* **Two length distributions are measured** (`uniform` and `skewed`). If padding is what
  eats the win, the two curves will diverge, and that is the finding.

Output goes to `benchmarks/results/` and is read by `benchmarks/report.py`; no number from
here may appear in the docs except through that generator (`AGENTS.md §8`).

    uv run python benchmarks/batch_gain.py --engine laya-multilingual --threads 24
    uv run python benchmarks/batch_gain.py --engine laya-multilingual --processes 2

Needs real weights, so it is not part of the default test run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Reuse the provenance helpers rather than growing a second copy of them: `AGENTS.md §2`
# makes "how a benchmark describes its host" a single home, and `report.py` demangles the
# keys these produce.
_RUN = importlib.util.spec_from_file_location("_bench_run", Path(__file__).resolve().parent / "run.py")
assert _RUN and _RUN.loader
run = importlib.util.module_from_spec(_RUN)
sys.modules["_bench_run"] = run
_RUN.loader.exec_module(run)

SCHEMA = "decis-batch-gain/1"
RESULTS = Path(__file__).resolve().parent / "results"

#: Batch sizes to synthesise. 1 is the serial baseline and is measured the same way,
#: one item per call, so the comparison is not "HTTP vs direct call".
BATCH_SIZES = (1, 2, 4, 8, 16)

#: The item sets measured, in output order. `shared` is a positive control: it must show a
#: gain, or the harness cannot detect one and its negative results mean nothing.
ITEM_SETS = ("shared_short", "shared", "uniform", "skewed")

#: Sentence pool for generating states. Kept deliberately dull: the point is length, not
#: semantics, and a decision model's answer must not depend on what the text says for a
#: timing experiment to mean anything.
_SENTENCES = (
    "The customer reported the problem on the fourteenth of March.",
    "Our records show two charges for the same invoice number.",
    "The account has been active since two thousand and nineteen.",
    "Support replied within one business day and asked for a screenshot.",
    "The mobile application closes immediately after the splash screen.",
    "A refund was promised but has not appeared on the statement.",
    "The subscription renews annually on the first of the month.",
    "Access was restored after the password was reset by the user.",
)


def _state_text(index: int, sentences: int) -> str:
    """A state of a chosen length, distinct per index.

    The index is woven in so that no two items share a state. That matters for the same
    reason `AGENTS.md §3-18` forbids grouping a batch by state: items that happened to be
    identical could share a prefix cache and make batching look better than it is.
    """
    parts = [f"Case {index}: {_SENTENCES[(index + offset) % len(_SENTENCES)]}" for offset in range(sentences)]
    return " ".join(parts)


def _question(index: int) -> Any:
    """One wire question of a rotating type, with a length that varies per index.

    Question text is part of the sequence, so a batch of identical questions would not be
    the traffic shape and would understate the padding a real batch pays.
    """
    from pydantic import TypeAdapter

    from decis.schema import Question

    kind = ("choice", "noul", "score")[index % 3]
    if kind == "choice":
        wire: dict[str, Any] = {
            "type": "choice",
            "instructions": "Which category does this case belong to? " + "Consider the record. " * (1 + index % 4),
            "criteria": {
                f"option_{n}": f"criterion number {n} " + "detail " * (1 + n % 5) for n in range(2 + index % 7)
            },
        }
    elif kind == "score":
        wire = {
            "type": "score",
            "instructions": "How severe is this? " + "Judge carefully. " * (1 + index % 3),
            "criteria": [f"level {n} " + "description " * (1 + n % 4) for n in range(2 + index % 5)],
        }
    else:
        wire = {
            "type": "noul",
            "instructions": "Is this case urgent? " + "Answer yes or no. " * (1 + index % 3),
            "criteria": {"true": "requires attention today", "false": "can wait " * (1 + index % 6)},
        }
    # Validate through the real wire model so the generator cannot produce a shape the
    # server would reject: a timing experiment must not measure something unreachable
    # over HTTP.
    adapter: TypeAdapter[Any] = TypeAdapter(Question)
    return adapter.validate_python(wire)


def _item(index: int, sentences: int, wire: Any) -> Any:
    """A work item. `wire` is a validated wire question; it is prepared here.

    `WorkItem.question` is a `PreparedQuestion`, not a wire type -- that is the whole
    point of the layering (`domain.py`). Preparing it from the validated wire model is
    what keeps this generator measuring the same text the server would render.
    """
    from decis.engines.base import WorkItem
    from decis.render import prepare_question

    return WorkItem(
        request_id=f"req_{index:04d}",
        state_text=_state_text(index, sentences),
        question=prepare_question(f"q{index}", wire),
    )


def _measure_one(engine: Any, item: Any) -> Any:
    from decis.domain import PreparedRequest

    return engine.measure(PreparedRequest(model="", state_text=item.state_text, questions=(item.question,)))


def _tokens_per_sentence(engine: Any, wire: Any) -> float:
    """Calibrate the tokenizer, so state lengths can be set as a fraction of the budget.

    Without this the generated states either overflow the engine's sequence limit (and
    `check_capacity` refuses to measure them) or come nowhere near it, and a padding
    experiment at 20% of the budget says nothing about behaviour at the limit.
    """
    low = _measure_one(engine, _item(0, 2, wire)).sequence_tokens
    high = _measure_one(engine, _item(0, 14, wire)).sequence_tokens
    slope = (high - low) / 12.0
    if slope <= 0:
        raise SystemExit("the engine reports no token growth with state length; cannot calibrate")
    return slope


def build_items(engine: Any, count: int, *, skew: str, budget: int) -> list[Any]:
    """`count` work items with different states and different question shapes.

    `skew` controls how much the sequence lengths differ *within* a batch, which is the
    variable that decides whether padding eats the win:

    * `uniform` -- every item lands near the same length, so padding costs almost nothing.
      This is the optimistic case.
    * `skewed` -- one item in four is long and the rest are short, so a batch is padded to
      its single longest row. This is what a mixed workload looks like.

    Lengths are set as a fraction of `budget` (the engine's real sequence limit) using the
    engine's own tokenizer, so both curves are measured at comparable pressure and neither
    relies on a character-per-token guess.
    """
    slope = _tokens_per_sentence(engine, _question(0))
    items = []
    for index in range(count):
        # The control reuses item 0 verbatim, so all items share one state and one
        # question and the batch pads to exactly its own length.
        control = skew.startswith("shared")
        wire = _question(0 if control else index)
        # A one-sentence probe gives this question's fixed overhead exactly: options,
        # separators and mask positions all land in it (design-review.md §2-D10).
        probe_index = 0 if control else index
        base = _measure_one(engine, _item(probe_index, 1, wire)).sequence_tokens
        if skew == "uniform":
            fraction = 0.55 + 0.05 * (index % 3)
        elif skew == "skewed":
            fraction = 0.80 if index % 4 == 0 else 0.15 + 0.05 * (index % 2)
        elif skew == "shared":
            # Every item identical, on purpose. This is the *request-internal* shape --
            # ten questions about one document -- which is already known to batch well.
            # It is the positive control: a harness that cannot show a win where a win
            # exists cannot be trusted when it shows none.
            fraction = 0.40
        elif skew == "shared_short":
            # The same control at a short sequence length. The existing request-internal
            # measurement (one question 201 ms -> ten questions 98.7 ms) was taken at a
            # short state, where per-call overhead is a large share of the cost. If the
            # gain is really about amortising overhead, it must reappear here and be
            # absent at the longer length -- which is what makes "no gain" a finding
            # about sequence length rather than about the harness.
            fraction = 0.12
        else:
            raise ValueError(f"unknown skew {skew!r}")
        target = budget * fraction
        sentences = max(1, min(400, round(1 + (target - base) / slope)))
        items.append(_item(probe_index, sentences, wire))
    return items


def sequence_lengths(engine: Any, items: list[Any]) -> list[int]:
    """The sequence length each item will occupy, as the engine itself measures it.

    Uses `engine.measure()` rather than counting characters: the head budget includes
    per-option decorations that do not appear in the rendered text, and getting this wrong
    is how `design-review.md §2-D10` happened.
    """
    from decis.domain import PreparedRequest

    lengths = []
    for item in items:
        measured = engine.measure(PreparedRequest(model="", state_text=item.state_text, questions=(item.question,)))
        lengths.append(measured.sequence_tokens)
    return lengths


def check_capacity(engine: Any, items: list[Any], lengths: list[int]) -> None:
    """Refuse to measure an item the engine would truncate.

    A truncated input still returns an answer, so measuring one would silently benchmark
    something other than what this script claims -- and the engine is faster on a shorter
    sequence, which would flatter batching (`AGENTS.md §5-4`).
    """
    info = engine.info()
    for item, length in zip(items, lengths, strict=True):
        if info.max_sequence_tokens and length > info.max_sequence_tokens:
            raise SystemExit(
                f"item {item.request_id} needs {length} tokens but {info.id} allows "
                f"{info.max_sequence_tokens}. Shorten the generated states; measuring a "
                f"truncated request would produce numbers for a request nobody sent."
            )


def padding(waste_lengths: list[int], batch: int) -> float:
    """How many times more sequence positions a batch occupies than it needs to.

    Computed from the measured lengths, **not** read from the engine's counter: engines
    report the useful token count, so the padded count is not observable from outside.
    That makes this a derived quantity and it is labelled as one in the output.
    """
    if batch <= 1:
        return 1.0
    useful = sum(waste_lengths)
    return (len(waste_lengths) * max(waste_lengths)) / useful if useful else 1.0


def _timed(engine: Any, items: list[Any]) -> tuple[float, int]:
    started = time.perf_counter()
    prediction = engine.predict(items)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return elapsed_ms, int(prediction.input_tokens or 0)


def measure_batches(
    engine: Any, items: list[Any], lengths: list[int], sizes: tuple[int, ...], rounds: int, warmup: int
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """One config per batch size. Batch 1 is the serial baseline, measured identically.

    **Rounds, not blocks.** Every round visits every batch size once, in an order shuffled
    per round, so a slow patch of the run (thermal drift, a co-tenant, a frequency change)
    lands on the batch sizes roughly evenly instead of landing entirely on whichever sizes
    happened to run during it. Measuring size-by-size in ascending order is what produced
    the alternation recorded as `design-review.md §4-M2`, and it is not repeated here: the
    sizes are visited in a different order each round and the per-size medians are taken
    across all rounds.

    The remainder that does not fill a whole batch is dropped, and how many items were
    dropped is recorded -- silently measuring a different item count for one config would
    make the per-item column incomparable.
    """
    random_source = random.Random(20260922)

    plans: dict[int, tuple[list[list[Any]], list[list[int]]]] = {}
    for batch in sizes:
        groups = [items[start : start + batch] for start in range(0, len(items) - batch + 1, batch)]
        group_lengths = [lengths[start : start + batch] for start in range(0, len(lengths) - batch + 1, batch)]
        if groups:
            plans[batch] = (groups, group_lengths)

    # Warm up every size before any measurement, so no size pays for a cold allocator or a
    # lazily-compiled kernel that the size before it did not.
    for groups, _ in plans.values():
        for _ in range(warmup):
            for group in groups:
                engine.predict(group)

    samples: dict[int, list[float]] = {batch: [] for batch in plans}
    tokens: dict[int, int] = dict.fromkeys(plans, 0)
    orders: list[list[int]] = []
    for _ in range(rounds):
        order = sorted(plans)
        random_source.shuffle(order)
        orders.append(order)
        for batch in order:
            for group in plans[batch][0]:
                elapsed_ms, group_tokens = _timed(engine, group)
                samples[batch].append(elapsed_ms)
                tokens[batch] += group_tokens

    configs = []
    for batch in sorted(plans):
        groups, group_lengths = plans[batch]
        calls = len(samples[batch])
        answered = calls * batch
        per_item = [value / batch for value in samples[batch]]
        configs.append(
            {
                "kind": "serial" if batch == 1 else "synthetic-batch",
                "batch": batch,
                "items_per_call": batch,
                "groups": len(groups),
                "n": calls,
                "items_answered": answered,
                "dropped_items": len(items) - len(groups) * len(groups[0]),
                "samples_ms": [round(value, 3) for value in samples[batch]],
                "call_ms": _stats(samples[batch]),
                "per_item_ms": _stats(per_item),
                "tokens_per_item": round(tokens[batch] / answered, 1) if answered else 0.0,
                "padding_ratio": round(statistics.fmean(padding(gl, batch) for gl in group_lengths), 4),
                "max_sequence_tokens": max(lengths),
                "note": (
                    "the serial baseline: one item per call, through the same code path as the batches"
                    if batch == 1
                    else "items grouped in arrival order, no waiting; an upper bound on what a real batcher can get"
                ),
            }
        )
    return configs, orders


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(ordered[len(ordered) // 2], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def derive(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-config speedup and break-even wait, computed from the measured medians.

    `break_even_wait_ms` is the number that decides `DECIS_BATCH_MAX_WAIT_MS`: it is how
    much *extra* latency an item can absorb before batching has cost it more than the
    batching saved. A batcher that waits longer than this makes every item slower than not
    batching at all, so it is the ceiling on any sane wait setting.
    """
    serial = next((config for config in configs if config["batch"] == 1), None)
    if serial is None:
        return configs
    baseline = serial["per_item_ms"]["p50"]
    for config in configs:
        per_item = config["per_item_ms"]["p50"]
        config["speedup_vs_serial"] = round(baseline / per_item, 3) if per_item else 0.0
        config["break_even_wait_ms"] = round(baseline - per_item, 3)
    return configs


def load_engine(engine_id: str, threads: int) -> tuple[Any, float]:
    """Load weights and report how long it took."""
    os.environ["DECIS_TORCH_THREADS"] = str(threads)
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = "127.0.0.1,localhost,::1"

    from decis.engines.registry import create

    engine = create(engine_id)
    started = time.perf_counter()
    engine.load()
    return engine, time.perf_counter() - started


def measure(
    engine_id: str, threads: int, items: int, sizes: tuple[int, ...], iterations: int, warmup: int
) -> dict[str, Any]:
    """The whole single-process experiment: both length distributions, all batch sizes."""
    engine, load_s = load_engine(engine_id, threads)
    info = engine.info()
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "engine": engine_id,
        "engine_version": info.version,
        "device": info.device,
        "dtype": info.dtype,
        "processes": 1,
        "threads": threads,
        "items": items,
        "batch_sizes": list(sizes),
        "iterations": iterations,
        "warmup": warmup,
        "host": run.host_facts(),
        "runtime": run.runtime_facts(),
        "load_s": round(load_s, 1),
        "peak_rss_gb": run.peak_rss_gb(),
        "capacities": {
            "max_sequence_tokens": info.max_sequence_tokens,
            "max_state_tokens": info.max_state_tokens,
            "max_question_tokens": info.max_question_tokens,
        },
        "methodology": {
            "traffic_shape": (
                "one question per item and a different state per item, which is what real "
                "traffic looks like; request-internal batching is not measured here because "
                "it is already implemented and already measured (benchmarks/results/laya-*-sweep.json)"
            ),
            "batch_construction": (
                "the engine is called directly with a synthesised list, so there is no queue, "
                "no waiting, no client cancellation and no partial failure. These figures are "
                "an UPPER BOUND on what a real batcher can achieve; every one of those missing "
                "mechanisms makes the real number worse."
            ),
            "padding_ratio": (
                "derived from engine.measure() sequence lengths as batch*max/sum, not read from "
                "the engine: engines report useful tokens, so the padded count is not externally "
                "observable. A ratio of 2.0 means half the computed positions were padding."
            ),
            "item_sets": {
                "shared_short": (
                    "POSITIVE CONTROL at a short sequence length (where request-internal batching was "
                    "already measured to help). The gain has to reappear here, or this harness cannot "
                    "detect one at all."
                ),
                "shared": (
                    "POSITIVE CONTROL: every item is byte-identical, which is the request-internal "
                    "shape (many questions about one document) that is already known to batch well. "
                    "If neither this nor `shared_short` shows a gain, the harness is broken and none "
                    "of the other numbers mean anything."
                ),
                "uniform": "different states, near-identical lengths: the best case for a batcher",
                "skewed": "different states, lengths spanning 6x: what a mixed workload looks like",
            },
            "rounds": (
                f"{iterations} interleaved rounds; each round visits every batch size once in a "
                "per-round shuffled order, and each config's median is taken across all rounds. "
                "Measuring one size to completion before the next would confound drift with size."
            ),
            "not_measured": [
                "queue wait, cancellation and partial-failure behaviour -- they need the batcher",
                "cross-state prefix sharing, which a real engine could exploit and this cannot",
                "p99 under sustained load",
            ],
        },
        "skews": {},
    }

    # Leave headroom: the engine's limit is a hard truncation point, and a padding
    # experiment should run near it, not past it.
    budget = int(info.max_sequence_tokens * 0.95)
    for skew in ITEM_SETS:
        work = build_items(engine, items, skew=skew, budget=budget)
        lengths = sequence_lengths(engine, work)
        check_capacity(engine, work, lengths)
        configs, orders = measure_batches(engine, work, lengths, sizes, iterations, warmup)
        configs = derive(configs)
        document["skews"][skew] = {
            # The shuffled visit order per round, kept so an order effect can be checked
            # for instead of assumed away (`design-review.md §4-M2`).
            "round_orders": orders,
            "sequence_tokens": {
                "min": min(lengths),
                "max": max(lengths),
                "mean": round(statistics.fmean(lengths), 1),
                # The number that predicts how much padding a fixed batch size must pay.
                "max_over_mean": round(max(lengths) / statistics.fmean(lengths), 2),
            },
            "state_chars": {
                "min": min(len(item.state_text) for item in work),
                "max": max(len(item.state_text) for item in work),
            },
            "configs": configs,
        }

    engine.close()
    return document


def measure_processes(
    engine_id: str, threads: int, items: int, iterations: int, warmup: int, processes: int
) -> dict[str, Any]:
    """Serial throughput with `processes` independent engine instances.

    The alternative to batching, and the thing a batcher must beat: if two processes
    already double the throughput at this memory cost, then the added concurrency
    semantics of a batcher may not be worth their risk. Each child reports its own wall
    time; because they run at the same time the slowest one is the effective duration.

    Memory is the binding constraint and is reported, not estimated.

    Every child measures the **same full item set**, not a different slice of it. Measuring
    different subsets would compare different sequence lengths across process counts --
    the sets are generated, and the first eight items are not length-matched to the first
    four. Identical work per child makes the aggregate a clean `processes x items / wall`.
    """
    children = []
    for _ in range(processes):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--engine",
            engine_id,
            "--threads",
            str(threads),
            "--items",
            str(items),
            "--iterations",
            str(iterations),
            "--warmup",
            str(warmup),
            "--skew",
            "uniform",
        ]
        children.append(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))

    started = time.perf_counter()
    results = []
    for child in children:
        stdout, _ = child.communicate()
        if child.returncode != 0:
            raise SystemExit(f"a child process failed with exit code {child.returncode}")
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    launch_to_done_s = time.perf_counter() - started

    # The measurement window is the children's own, not this process's wall clock. Each
    # child loads weights before it starts timing, and loading takes tens of seconds; using
    # the parent's elapsed time would add every child's cold start to the per-item cost and
    # make concurrency look catastrophic. The children run at the same time, so the
    # effective duration is the slowest child's measurement window.
    wall_s = max(result["wall_s"] for result in results)
    answered = sum(result["items_answered"] for result in results)
    per_item = wall_s * 1000.0 / answered if answered else 0.0
    return {
        "schema": SCHEMA,
        "engine": results[0]["engine"],
        "engine_version": results[0]["engine_version"],
        "device": results[0]["device"],
        "dtype": results[0]["dtype"],
        "processes": processes,
        "threads": threads,
        "items": answered,
        "iterations": iterations,
        "warmup": warmup,
        "host": results[0]["host"],
        "runtime": results[0]["runtime"],
        "load_s": max(result["load_s"] for result in results),
        "peak_rss_gb_per_process": max(result["peak_rss_gb"] for result in results),
        "aggregate": {
            "wall_s": round(wall_s, 2),
            # Reported so the load cost is visible rather than hidden inside the throughput
            # figure above. It is not part of `per_item_ms`.
            "load_overlapped_s": round(max(result["load_s"] for result in results), 1),
            "launch_to_done_s": round(launch_to_done_s, 2),
            "items_answered": answered,
            "per_item_ms": round(per_item, 2),
            "items_per_second": round(answered / wall_s, 2) if wall_s else 0.0,
            "child_wall_s": [round(result["wall_s"], 2) for result in results],
        },
        "methodology": {
            "traffic_shape": "one question per item, a different state per item, the same generator as the single-process run",
            "kind": (
                f"{processes} independent engine instances, one per process, each answering the same "
                "full item set serially and concurrently. No batching anywhere. This is the baseline "
                "a batcher would have to beat."
            ),
            "aggregate": (
                "per_item_ms is wall / (processes x items x rounds): the cost of one item when the "
                "machine is shared by that many processes. The children overlap, so the effective "
                "duration is the slowest child's measurement window, which excludes cold-start load."
            ),
            "memory": (
                "peak RSS per process is reported; the total is roughly processes x that, which is "
                "the constraint that caps how far this scales. Laya needs several GB per process, so "
                "this host's 33.5 GB caps the useful process count well before the core count does."
            ),
            "not_measured": ["whether the OS scheduler actually keeps them on separate cores", "p99 under this load"],
        },
        "skews": {},
    }


def measure_shard(engine_id: str, threads: int, items: int, iterations: int, warmup: int, skew: str) -> dict[str, Any]:
    """One child process: load, then answer `items` items one at a time. No HTTP."""
    engine, load_s = load_engine(engine_id, threads)
    info = engine.info()
    work = build_items(engine, items, skew=skew, budget=int(info.max_sequence_tokens * 0.95))
    lengths = sequence_lengths(engine, work)
    check_capacity(engine, work, lengths)

    for item in work[: max(1, warmup)]:
        engine.predict([item])

    started = time.perf_counter()
    answered = 0
    for _ in range(iterations):
        for item in work:
            engine.predict([item])
            answered += 1
    wall_s = time.perf_counter() - started
    engine.close()
    return {
        "engine": engine_id,
        "engine_version": info.version,
        "device": info.device,
        "dtype": info.dtype,
        "load_s": round(load_s, 1),
        "peak_rss_gb": run.peak_rss_gb(),
        "host": run.host_facts(),
        "runtime": run.runtime_facts(),
        "wall_s": wall_s,
        "items_answered": answered,
        "sequence_tokens": {"min": min(lengths), "max": max(lengths), "mean": round(statistics.fmean(lengths), 1)},
    }


def default_out(engine_id: str, processes: int) -> Path:
    suffix = "batch-gain" if processes == 1 else "multiprocess"
    return RESULTS / f"{engine_id}-{suffix}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", default="laya-multilingual")
    parser.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--items", type=int, default=16, help="distinct work items per measurement")
    parser.add_argument("--batches", default=",".join(str(size) for size in BATCH_SIZES))
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--processes", type=int, default=1, help="serial processes to run instead of batching")
    parser.add_argument("--out", default="", help="output path; default benchmarks/results/<engine>-<kind>.json")
    parser.add_argument("--stdout", action="store_true", help="write the JSON to stdout instead of a file")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skew", default="uniform", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    sizes = tuple(int(part) for part in args.batches.split(",") if part)

    if args.child:
        document = measure_shard(args.engine, args.threads, args.items, args.iterations, args.warmup, args.skew)
        print(json.dumps(document))
        return 0

    if args.processes > 1:
        document = measure_processes(
            args.engine, args.threads, args.items, args.iterations, args.warmup, args.processes
        )
    else:
        print(
            f"loading {args.engine} with {args.threads} threads; this takes a while on CPU",
            file=sys.stderr,
        )
        document = measure(args.engine, args.threads, args.items, sizes, args.iterations, args.warmup)

    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    if args.stdout:
        print(text, end="")
        return 0

    out = Path(args.out) if args.out else default_out(args.engine, args.processes)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _summarise(document, out)
    return 0


def _summarise(document: dict[str, Any], out: Path) -> None:
    """Print the finding to stderr, because the file is the artifact and stdout is data."""
    try:
        shown = out.resolve().relative_to(Path.cwd())
    except ValueError:
        shown = out
    print(f"wrote {shown}", file=sys.stderr)
    if document.get("aggregate"):
        aggregate = document["aggregate"]
        print(
            f"  {document['processes']} processes: {aggregate['per_item_ms']:.1f} ms/item, "
            f"{aggregate['items_per_second']:.2f} items/s",
            file=sys.stderr,
        )
        return
    for skew, block in document["skews"].items():
        print(f"  {skew}: sequences {block['sequence_tokens']}", file=sys.stderr)
        for config in block["configs"]:
            print(
                f"    batch {config['batch']:>2}: {config['per_item_ms']['p50']:8.2f} ms/item "
                f"({config['speedup_vs_serial']:.2f}x, break-even wait "
                f"{config['break_even_wait_ms']:.1f} ms, padding {config['padding_ratio']:.2f})",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
