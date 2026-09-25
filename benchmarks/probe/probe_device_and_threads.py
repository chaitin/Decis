"""Which device, which PyTorch build, how many threads -- one configuration per run.

This probe exists to answer "why is the container slower than `uv run` on the same Mac".
Three separate things turn out to be in that gap, and they are only separable if each run
holds the rest still:

* **which device** Laya picks when nobody pins one (`laya/agent.py:177-182`: CUDA, then
  Metal, then CPU -- so macOS gets the GPU and a Linux container gets the CPU);
* **which PyTorch build** is running, since the macOS wheel reaches Apple's matrix kernels
  and the `linux-aarch64` wheel does not. `gemm1024` is the control for this: it is raw
  fp32 matmul at the same thread count, so a gap there is the platform rather than the
  model;
* **how many threads** torch gives itself, which is one per visible CPU -- on Apple silicon
  that is every core, including the efficiency ones, whereas PyTorch's own macOS default is
  `hw.perflevel0.physicalcpu`, the performance cores only.

Run it on the host and inside the container against the same weights and the same request:

    PY=path/to/venv/bin/python
    $PY probe/probe_device_and_threads.py cpu ~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/<rev>/multilingual 5 big
    docker cp probe/probe_device_and_threads.py <container>:/tmp/ && \\
      docker exec <container> python /tmp/probe_device_and_threads.py cpu /models/laya-multilingual/multilingual 5 big

It prints one JSON object. This is a probe, not a benchmark result file: nothing reads it,
and the figures in `docs/performance.md` come from `benchmarks/run.py` instead.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

#: Where `decis download` puts it by default, for the "run it on the host" side.
DEFAULT_SNAPSHOT = (
    Path.home()
    / ".cache/huggingface/hub/models--convaiinnovations--laya/snapshots"
    / "1c5edc17a7acd8701df6fc341c0d179f1c62c982/multilingual"
)

#: A small request (one score question, ~200 tokens in) and one that uses most of the
#: 512-token sequence budget, because latency scales with the sequence, not with the
#: number of questions alone.
SMALL_STATE = (
    "Score 42. Level 3. Player x=10 y=20, next block=7, lines=12, speed=1.4. "
    "Board has 3 holes and a flat surface. Instruction text follows: keep the stack low. "
) * 3
BOARD = "\n".join("#" * 10 if row in (18, 19) else ("#" + "." * 9 if row == 17 else "." * 10) for row in range(20))
BIG_STATE = json.dumps(
    {
        "game": {
            "rules": (
                "Standard Tetris. Board is 10 columns wide and 20 rows tall. Rows fill left to "
                "right; a full row disappears. The game is lost when the stack reaches the top."
            ),
            "board_rows_top_to_bottom": BOARD,
            "legend": "# is a filled cell, . is an empty cell. The first row is the top of the board.",
            "column_heights_left_to_right": [0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
            "stack_height": "low",
            "holes_in_stack": "none",
            "surface": "flat",
            "current_piece": "T",
            "next_piece": "L",
            "lines_cleared_so_far": 12,
        }
    }
)

SMALL_QUESTIONS = {
    "q1": {
        "type": "score",
        "instructions": "How good is the current position?",
        "criteria": ["terrible", "bad", "ok", "good", "excellent"],
    }
}
BIG_QUESTIONS = {
    "q1": {
        "type": "choice",
        "instructions": "Which placement should the piece be dropped into?",
        "criteria": {f"p{i}": f"Column {i} keeping the surface even and the stack low, no new holes" for i in range(6)},
    }
}


def main() -> None:
    device = sys.argv[1] if len(sys.argv) > 1 else "cpu"
    model_path = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "-" else DEFAULT_SNAPSHOT
    threads = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    shape = sys.argv[4] if len(sys.argv) > 4 else "small"

    import laya
    import torch

    if threads:
        torch.set_num_threads(threads)

    before = {
        "intraop": torch.get_num_threads(),
        "interop": torch.get_num_interop_threads(),
        "cpu_count": os.cpu_count(),
        # What the container is actually allowed to run on, which `os.cpu_count()` does
        # not answer under a cgroup quota.
        "affinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "cuda": torch.cuda.is_available(),
        "mps": getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available(),
    }

    started = time.perf_counter()
    agent = laya.Agent(str(model_path), device=device)
    load_s = time.perf_counter() - started

    state = SMALL_STATE if shape == "small" else BIG_STATE
    questions = SMALL_QUESTIONS if shape == "small" else BIG_QUESTIONS

    usage = agent.system_one(state, questions)["usage"]
    agent.system_one(state, questions)  # warm, so the samples are the steady state
    samples = []
    for _ in range(7):
        t = time.perf_counter()
        agent.system_one(state, questions)
        samples.append((time.perf_counter() - t) * 1000)

    # The control: platform throughput with the model out of the picture.
    left, right = torch.randn(1024, 1024), torch.randn(1024, 1024)
    for _ in range(3):
        left @ right
    t = time.perf_counter()
    for _ in range(10):
        left @ right
    gemm_ms = (time.perf_counter() - t) * 1000 / 10

    print(
        json.dumps(
            {
                "platform": f"{platform.system()}/{platform.machine()}",
                "requested_device": device,
                "resolved_device": str(agent.device),
                "dtype": str(agent.dtype),
                "threads": before,
                "shape": shape,
                "input_tokens": usage["input_tokens"],
                "load_s": round(load_s, 1),
                "samples_ms": [round(value, 1) for value in samples],
                "median_ms": round(statistics.median(samples), 1),
                "gemm1024_ms": round(gemm_ms, 2),
                "gemm1024_gflops": round(2 * 1024**3 / (gemm_ms / 1000) / 1e9, 1),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
