"""Systematic CPU sweep for Laya: thread count x questions-per-request.

Writes raw per-config JSON (the Decis benchmarks/ convention: raw samples + input hash,
report generated from the JSON, never hand-typed numbers).
"""
import hashlib
import json
import os
import platform
import resource
import statistics
import sys
import time

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch  # noqa: E402
import laya  # noqa: E402

SUBFOLDER = sys.argv[1] if len(sys.argv) > 1 else "multilingual"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/laya_sweep.json"
ITER = int(os.environ.get("ITER", "8"))

STATE = {
    "from": "user@acme.com",
    "subject": "Duplicate charge on invoice #4411",
    "body": "Hi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
}


def make_questions(n):
    """n questions: a rotating mix of choice(4) / score(3) / noul, like a real triage call."""
    out = {}
    for i in range(n):
        kind = i % 3
        if kind == 0:
            out[f"choice_{i}"] = {
                "type": "choice",
                "instructions": "Which department should handle this request?",
                "criteria": {"billing": "invoices, payments, refunds",
                             "technical": "bugs, outages, system errors",
                             "sales": "pricing, new contracts",
                             "other": "everything else"},
            }
        elif kind == 1:
            out[f"score_{i}"] = {
                "type": "score",
                "instructions": "How urgent is this request?",
                "criteria": ["not urgent", "soon", "critical deadline or blocking issue"],
            }
        else:
            out[f"noul_{i}"] = {
                "type": "noul",
                "instructions": "Does the user threaten to cancel or leave?",
            }
    return out


def token_estimate(agent, questions):
    """Same token accounting the model sees, for the report's input-hash assertion."""
    from laya.common import build_sequence
    max_len = agent.cfg.get("max_len", 512)
    head_max_len = agent.cfg.get("head_max_len", 192)
    total = 0
    for qid, q in questions.items():
        internal = agent._to_internal(q)
        seq, _ = build_sequence(agent.tok, STATE, internal, max_len, head_max_len)
        total += len(seq)
    return total


def measure(agent, questions, n_iter):
    agent.predict(STATE, questions)  # warm
    samples = []
    for _ in range(n_iter):
        t = time.perf_counter()
        r = agent.predict(STATE, questions)
        samples.append((time.perf_counter() - t) * 1000)
    return samples, r


results = []

t_load = time.perf_counter()
agent = laya.load("convaiinnovations/laya", subfolder=SUBFOLDER or None)
load_s = time.perf_counter() - t_load

peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024

for threads in [1, 2, 4, 8, 16, 24]:
    torch.set_num_threads(threads)
    for nq in [1, 3, 10, 30]:
        qs = make_questions(nq)
        try:
            samples, resp = measure(agent, qs, ITER)
        except Exception as e:  # noqa: BLE001
            results.append({"threads": threads, "questions": nq, "error": repr(e)[:300]})
            continue
        results.append({
            "threads": threads,
            "questions": nq,
            "input_tokens": token_estimate(agent, qs),
            "input_sha256": hashlib.sha256(
                json.dumps(qs, sort_keys=True).encode() + json.dumps(STATE, sort_keys=True).encode()
            ).hexdigest()[:16],
            "n": len(samples),
            "p50_ms": round(statistics.median(samples), 1),
            "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 1),
            "min_ms": round(min(samples), 1),
            "max_ms": round(max(samples), 1),
            "ms_per_question": round(statistics.median(samples) / nq, 2),
            # NOT throughput: this is what one serial caller achieves. It ignores concurrency
            # and cross-request batching. See benchmarks/README.md and docs/design-review.md M4.
            "single_caller_questions_per_second": round(1000 / (statistics.median(samples) / nq), 1),
            "sample_choice_confidence": resp["answers"][next(k for k in resp["answers"] if k.startswith("choice"))]["confidence"],
        })

payload = {
    "engine": "laya",
    "checkpoint": SUBFOLDER or "english",
    "laya_version": getattr(laya, "__version__", "?"),
    "torch_version": torch.__version__,
    "host": {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
        "gpu": False,
        "mem_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1),
    },
    "load_s": round(load_s, 1),
    "peak_rss_gb_after_load": round(peak_before, 2),
    "peak_rss_gb_total": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 2),
    "results": results,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)

print(json.dumps({k: v for k, v in payload.items() if k != "results"}, indent=2, ensure_ascii=False))
print("\nthreads  questions   p50_ms   p95_ms  ms/question  q/s(serial)")
for r in results:
    if "error" in r:
        print(f"{r['threads']:>7}  {r['questions']:>9}   ERROR {r['error'][:60]}")
    else:
        print(f"{r['threads']:>7}  {r['questions']:>9}  {r['p50_ms']:>7}  {r['p95_ms']:>7}  "
              f"{r['ms_per_question']:>11}  {r['single_caller_questions_per_second']:>11}")
