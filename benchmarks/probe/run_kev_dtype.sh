#!/usr/bin/env bash
# kev CPU dtype comparison: fp32 vs bf16, small sample, per-request timing.
#
# Prerequisites: a Python environment with torch, transformers, peft, accelerate and
# the kev source tree (https://github.com/jaredpalmer/kev) on PYTHONPATH.
# See benchmarks/README.md for the measured results and their caveats.
#
#   KEV_SRC=/path/to/kev PY=/path/to/python bash benchmarks/probe/run_kev_dtype.sh
set -eu

KEV_SRC="${KEV_SRC:-/data/src/github.com/jaredpalmer/kev}"
PY="${PY:-python}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-$HERE/../results}"
RUN="${KEV_RUN:-jaredpalmer/kev-0.8b}"

export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export USE_TF=0
# The host's no_proxy may contain an IPv6 literal that httpx cannot parse as a URL.
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="$NO_PROXY"

run_variant () {
  local dtype="$1" port="$2" threads="$3"
  echo "===VARIANT $dtype threads=$threads port=$port==="
  ( cd "$KEV_SRC" && \
    OMP_NUM_THREADS="$threads" PYTHONPATH="$KEV_SRC" KEV_DTYPE="$dtype" \
      "$PY" -m kev.serve --run "$RUN" --port "$port" > "$OUT_DIR/kev-$dtype.log" 2>&1 ) &
  local srv=$!
  local up=0
  for _ in $(seq 1 300); do
    if ! kill -0 "$srv" 2>/dev/null; then echo "SERVER DIED ($dtype)"; break; fi
    if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/v1/models"; then echo "UP"; up=1; break; fi
    sleep 1
  done
  if [ "$up" = "1" ]; then
    "$PY" "$HERE/probe_kev2.py" "http://127.0.0.1:$port" "${KEV_ITERS:-2}"
  fi
  kill "$srv" 2>/dev/null || true
  wait "$srv" 2>/dev/null || true
  echo "===VARIANT $dtype done==="
}

mkdir -p "$OUT_DIR"
run_variant fp32 8011 "${KEV_THREADS:-16}"
run_variant bf16 8012 "${KEV_THREADS:-16}"
echo "===ALL DONE==="
