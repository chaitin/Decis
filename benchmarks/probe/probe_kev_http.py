"""CPU latency probe for a kev server (Decis feasibility study). Measures the HTTP path."""

import json
import statistics
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10

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


def post(body):
    req = urllib.request.Request(
        f"{BASE}/v1/systemone", data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


resp = post(BODY)  # warm-up + first (prefix-cache-miss) pass
lat = []
for _ in range(N):
    t = time.time()
    resp = post(BODY)
    lat.append((time.time() - t) * 1000)

lat2 = []
for _ in range(N):
    t = time.time()
    post(BODY)
    lat2.append((time.time() - t) * 1000)

print(
    json.dumps(
        {
            "engine": "kev",
            "models_endpoint": get("/v1/models"),
            "latency_ms": {
                "min": round(min(lat2), 1),
                "p50": round(statistics.median(lat2), 1),
                "max": round(max(lat2), 1),
            },
            "first_pass_ms": round(lat[0], 1),
            "response": resp,
        },
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)
