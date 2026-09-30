# Engines

**English** · [简体中文](engines.zh-CN.md)

[Documentation index](../README.md#documentation) · [Configuration](configuration.md) · [API](api.md)

An engine is a decision model behind the wire contract. It takes a state and one prepared
question, and returns a probability for each option; the server turns that into `Noul`,
`Choice` and `Score` answers. Engines never write a response and never learn a question id,
which is why every engine returns comparable shapes and adding one does not touch the
normalisation layer.

One thing an engine *may* see is the caller's own values. [`render.py`](../src/decis/render.py)
flattens JSON into readable text for the wire, and a model trained on a different rendering
can ask for the original through `WorkItem.raw_state` and `PreparedQuestion.raw`. The two
Jeff engines do exactly that: their prompts are defined by `json.dumps` of the caller's
objects rather than by the flattened lines Laya and kev are trained on. It stays internal —
the public API is unchanged, and `render.py` still owns which fields a question has.

## Registered engines

| Engine | Backbone | Params | Weights | Extra | Notes |
|---|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | `laya` | 100+ languages, the default engine |
| `laya` | ModernBERT-large | 421M | 807 MiB | `laya` | The English checkpoint; no accuracy benchmark is checked in |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | `laya` | The typed-decisions checkpoint from the same repository |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.66 GiB | `kev` | Prefill-only, no text generation; wants a GPU |
| `jeff-qwen3.5-0.8b` | Qwen3.5-0.8B, full fine-tune + readout head | 0.85B | 1.61 GiB | `jeff` | Prefill-only; `jeff`, `jeff-qwen` and `jeff-qwen3.5` are aliases |
| `jeff-gemma4-e2b` | Gemma 4 E2B, full fine-tune + readout head | 4.63B | 8.65 GiB | `jeff` | Prefill-only; trained on 26 options; the largest image here |

Neither Jeff parameter count is the checkpoint's name: the first is 852,985,920 and the
second 4,628,569,379, both from the Hub's own `safetensors.parameters` block rather than from
`0.8B` / `E2B`. The weight figures are the Hub's file listing restricted to the files an
engine needs: for `kev-0.8b` that is the three adapter files (~13 MB) plus the Qwen3.5 base
it resolves through (1.65 GiB), not the adapter repository's full 43 MiB listing.

```bash
uv run decis models     # what is registered, and what this machine can actually run
```

## Installing an engine

An engine's dependencies live in its own extra, never in `[project.dependencies]`, so a sync
installs only the engines you name. A plain `uv sync` names none of them — and it **removes**
the engine dependencies that an earlier sync added, which is how an engine that worked
yesterday is missing today (`decis models` then reports `deps missing`, with the command that
fixes it).

```bash
uv sync --all-extras                # every engine, plus the dev tools: one command for a local checkout
uv sync --extra dev --extra laya    # only the Laya family
uv sync --extra dev --extra kev     # only kev-0.8b
uv sync --extra dev --extra jeff    # both Jeff checkpoints
```

The two Jeff engines need **Python 3.12 or newer**; the project itself still supports 3.11,
and the published images run 3.13. The reason is the vendored serving code: it uses PEP 695
`type` aliases (`_jeff_vendor/types.py`), which do not even parse before 3.12, and they are
recursive — `JSONValue` contains `list[JSONValue]` — so they cannot be rewritten for 3.11 the
way that copy's imports were. The `jeff` extra therefore carries a `python_version >= '3.12'`
marker and installs nothing on 3.11, and `decis models` reports `needs Python 3.12+` instead
of naming a module that the extra would never install (`docs/design-review.md §2-D31`).

## Capacity

Each engine reports its own limits, and the only place they are published is `GET /v1/models`
(under the `decis` namespace). `decis models` reports whether an engine can run here, not what
its token and option limits are.

| Engine | `max_options` | `max_question_tokens` | `max_sequence_tokens` | `max_state_tokens` |
|---|---|---|---|---|
| `laya*` | 255 | from the checkpoint's `head_max_len` (fallback 192) | from the checkpoint's `max_len` (fallback 512) | none |
| `kev-0.8b` | 255 | 1024 | 1024 | 384 |
| `jeff-qwen3.5-0.8b` | 254 | 8192 | 8192 | none |
| `jeff-gemma4-e2b` | 26 | 8192 | 8192 | none |

The Laya limits come from the checkpoint's own `config`, so they are correct for the model
you loaded rather than a constant. The fallbacks apply before weights are present. The Jeff
`max_options` values are the training ceilings recorded in each checkpoint's
`decision_config.json`, read again at load time; both readout heads have 255 rows, so how many
options an engine accepts is the checkpoint's declared `max_options`, not a limit of the head.
8192 is the vendored serving code's own sequence window, and it applies to `state + one
question` together.

A request that exceeds a limit is rejected with **422** and a message naming the measured
number and the limit. Decis never truncates an over-long input to make it fit: upstream does
that, and it produces a confident wrong answer instead of an error.

### Memory

The weight files are not the footprint. Every engine holds the model in memory once it is
loaded, in the dtype its device implies (`fp32` on CPU), and the load itself peaks higher than
the finished model because the checkpoint arrives in `bfloat16` and is converted. Two measured
examples, from a load on a 24-core CPU host with no GPU — a single observation each, not a
benchmark:

| Engine | Weights on disk | Peak RSS while loading and answering |
|---|---|---|
| `jeff-qwen3.5-0.8b` | 1.61 GiB | ~5.3 GiB |
| `jeff-gemma4-e2b` | 8.65 GiB | **23.8 GiB** |

Both were read with `/usr/bin/time -v` (`Maximum resident set size`), which is why they are
`GiB` rather than the download sizes' decimal units. The Qwen figure is the higher of the two
probes taken on that host, so treat it as "a few GiB, not a 1.6 GiB process".

So the Gemma checkpoint needs a machine with ~24 GiB free on CPU, and the two Jeff engines are
not comfortable in one process: both resident is roughly 26 GiB. `docs/deployment.md` shows the
placeholder Kubernetes request/limit that these numbers exist to replace.

## Adding an engine

The work is one module plus one registry line. If an engine needs
[`render.py`](../src/decis/render.py) or [`answers.py`](../src/decis/answers.py) changed to fit
in, the engine interface does not accommodate it: adding an engine should not mean editing the
normalisation layer.

1. Implement `DecisionEngine` in `src/decis/engines/<name>.py`: `info()`, `load()`,
   `predict()`, `close()`, plus `weights()` and `measure()`.
2. Register it in [`engines/registry.py`](../src/decis/engines/registry.py) as
   `"module:ClassName"` — a **string path**, so importing the registry does not import the
   engine's heavy dependencies.
3. Add its dependencies as a `pyproject.toml` extra. Engine dependencies never go in
   `[project.dependencies]`.
4. Declare its capacities honestly, and implement `measure()` with the real tokenizer.
   `max_sequence_tokens` is the budget for `state + one question`, not for `state` alone.
   If the model's prompt is defined in terms of the caller's JSON rather than of the
   flattened text, read `PreparedQuestion.raw` and `WorkItem.raw_state`; do not re-implement
   the flattening.
5. Add both test suites: a weight-free one for `measure()`'s arithmetic and the engine's
   internal shapes, and a `-m weights` one for batch invariance and over-long rejection.

## dtype and device

How `DECIS_DEVICE` resolves and which engines `DECIS_DTYPE` reaches are in
[Configuration](configuration.md#compute); the measured cost of the wrong dtype is in
[Performance](performance.md#dtype-per-engine-and-device). What follows is per engine.

`kev-0.8b` and both Jeff engines ask for the device that resolution picks; Laya leaves the
unset case to its own `Agent`, whose order is CUDA, then Metal, then CPU. Without an override,
the dtype comes from the engine and device:

| Engine | cpu | cuda | mps |
|---|---|---|---|
| `laya*` | decided by Laya's own `Agent` | same | same |
| `kev-0.8b` | `fp32` | `bf16` | `fp32` |
| `jeff-qwen3.5-0.8b` | `fp32` | `bf16` | `bf16` |
| `jeff-gemma4-e2b` | `fp32` | `bf16` | `bf16` |

The table is written in `DECIS_DTYPE` spelling. A response reports the wire value instead:
`float32`, `float16` or `bfloat16`, and `unloaded` before the engine has loaded — in
`/v1/models` and in the `decis` namespace of every answer.

The Jeff rows are the vendored loaders' own rule (`bfloat16` on `cuda` and `mps`,
`float32` everywhere else), which is why `xpu` and `npu` land on `fp32` rather than on a
Decis default. The Qwen loader also turns cuDNN's SDPA backend off process-wide, as
upstream does; one engine per process is the deployment this service targets.

## Weights

Resolution order is described in
[Configuration](configuration.md#engine-selection-and-weights). Three engine-specific notes:

- **Laya** checkpoints carry their own capacities, so a fine-tune with a different
  `max_len` is served with the right budget automatically.
- **kev** references its Qwen3.5 base by Hub repo id inside the adapter's checkpoint
  metadata, so a separately mounted copy of the base is not picked up. `decis download`
  places the base in the Hugging Face cache, and the engine runs offline from there.
- **Jeff** checkpoints are full-weight fine-tunes, so `decis download` fetches only the one
  directory and there is no base to place beside it. Each carries its answer vocabulary and
  its sampling temperature in `decision_config.json`, and the loader refuses to start if the
  tokenizer cannot reproduce that vocabulary — a fine-tune you re-export yourself must bring
  its own `readout.safetensors` and keep those two files in step. The `jeff` extra also
  installs `torchvision`, although nothing on the request path touches an image: the Qwen
  checkpoint's processor is `Qwen3VLProcessor`, its config names a video processor, and
  `AutoProcessor` builds every named sub-processor eagerly — that class fails at import
  without torchvision, so the load never reaches a tensor.
