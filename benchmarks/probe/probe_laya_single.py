"""CPU latency probe for the Laya decision model (Decis feasibility study)."""

import json
import os
import resource
import statistics
import sys
import time

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

import laya

STATE = {
    "from": "user@acme.com",
    "subject": "Duplicate charge on invoice #4411",
    "body": "Hi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
}
QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which department should handle this request?",
        "criteria": {
            "billing": "invoices, payments, refunds",
            "technical": "bugs, outages, system errors",
            "sales": "pricing, new contracts",
            "other": "everything else",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this request?",
        "criteria": ["not urgent", "soon", "critical deadline or blocking issue"],
    },
    "churn_risk": {
        "type": "noul",
        "instructions": "Does the user threaten to cancel or leave?",
    },
}

sub = sys.argv[1] if len(sys.argv) > 1 else "multilingual"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
threads = os.environ.get("OMP_NUM_THREADS", "unset")

t0 = time.time()
agent = laya.load("convaiinnovations/laya", subfolder=sub or None)
load_s = time.time() - t0

r = agent.predict(STATE, QUESTIONS)  # warm-up
lat = []
for _ in range(n):
    t = time.time()
    r = agent.predict(STATE, QUESTIONS)
    lat.append((time.time() - t) * 1000)

rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024

print(
    json.dumps(
        {
            "engine": "laya",
            "checkpoint": sub or "english",
            "threads": threads,
            "load_s": round(load_s, 1),
            "latency_ms": {
                "min": round(min(lat), 1),
                "p50": round(statistics.median(lat), 1),
                "max": round(max(lat), 1),
            },
            "peak_rss_gb": round(rss_gb, 2),
            "answers": r["answers"] if isinstance(r, dict) and "answers" in r else r,
            "top_level_keys": sorted(r.keys()) if isinstance(r, dict) else None,
        },
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)
