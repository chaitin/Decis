"""Small-sample kev latency probe: prints each request's wall time so partial results are usable."""

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3

BODY = {
    "state": "Shoes arrived two weeks late and in the wrong size. Also I see two charges on my card.",
    "model": "kev-latest",
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "Which team should handle this?",
            "criteria": {
                "returns": "Exchanges, refunds, wrong or damaged items",
                "shipping": "Delivery status, delays, lost packages",
                "billing": "Charges, invoices, payment problems",
            },
        },
        "escalate": {"type": "noul", "instructions": "Does this need urgent human attention?"},
        "frustration": {
            "type": "score",
            "instructions": "How frustrated is the customer?",
            "criteria": ["Calm", "Frustrated", "Very angry"],
        },
    },
}


def post(body, timeout=3600):
    req = urllib.request.Request(
        f"{BASE}/v1/systemone", data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


lat = []
for i in range(N + 1):  # first is warm-up
    t = time.perf_counter()
    resp = post(BODY)
    dt = (time.perf_counter() - t) * 1000
    print(f"  request {i}{' (warmup)' if i == 0 else ''}: {dt:.0f} ms", flush=True)
    if i:
        lat.append(dt)

print(
    json.dumps(
        {
            "n": len(lat),
            "min_ms": round(min(lat)),
            "p50_ms": round(sorted(lat)[len(lat) // 2]),
            "max_ms": round(max(lat)),
            "response": resp,
        },
        indent=2,
        ensure_ascii=False,
    )
)
