# Engines

**English** · [简体中文](engines.zh-CN.md)

[Documentation index](../README.md#documentation) · [Configuration](configuration.md) · [API](api.md)

An engine is a decision model behind the wire contract. It takes a state and one prepared
question, and returns a probability for each option; the server turns that into `Noul`,
`Choice` and `Score` answers. Engines never see JSON and never write a response, which is
why every engine returns comparable shapes and adding one does not touch the normalisation
layer.

## Registered engines

| Engine | Backbone | Params | Weights | Extra | Notes |
|---|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | `laya` | 100+ languages, the default engine |
| `laya` | ModernBERT-large | 421M | 807 MiB | `laya` | The English checkpoint; no accuracy benchmark is checked in |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | `laya` | The typed-decisions checkpoint from the same repository |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.69 GiB | `kev` | Prefill-only, no text generation; wants a GPU |

```bash
uv run decis models     # what is registered, and what this machine can actually run
```

## Capacity

Each engine reports its own limits, and the only place they are published is `GET /v1/models`
(under the `decis` namespace). `decis models` reports whether an engine can run here, not how
much it can take.

| Limit | `laya*` | `kev-0.8b` |
|---|---|---|
| `max_options` | 255 | 255 |
| `max_question_tokens` | from the checkpoint's `head_max_len` (fallback 192) | 1024 |
| `max_sequence_tokens` | from the checkpoint's `max_len` (fallback 512) | 1024 |
| `max_state_tokens` | none | 384 |

The Laya limits come from the checkpoint's own `config`, so they are correct for the model
you loaded rather than a constant. The fallbacks apply before weights are present.

A request that exceeds a limit is rejected with **422** and a message naming the measured
number and the limit. Decis never truncates an over-long input to make it fit: upstream does
that, and it produces a confident wrong answer instead of an error.

## Adding an engine

The work is one module plus one registry line. If an engine needs
[`render.py`](../src/decis/render.py) or [`answers.py`](../src/decis/answers.py) changed,
the abstraction is wrong — that is the test the second engine was added to pass.

1. Implement `DecisionEngine` in `src/decis/engines/<name>.py`: `info()`, `load()`,
   `predict()`, `close()`, plus `weights()` and `measure()`.
2. Register it in [`engines/registry.py`](../src/decis/engines/registry.py) as
   `"module:ClassName"` — a **string path**, so importing the registry does not import the
   engine's heavy dependencies.
3. Add its dependencies as a `pyproject.toml` extra. Engine dependencies never go in
   `[project.dependencies]`.
4. Declare its capacities honestly, and implement `measure()` with the real tokenizer.
   `max_sequence_tokens` is the budget for `state + one question`, not for `state` alone.
5. Add both test suites: a weight-free one for `measure()`'s arithmetic and the engine's
   internal shapes, and a `-m weights` one for batch invariance and over-long rejection.

## dtype and device

`DECIS_DEVICE` selects `cpu`, `cuda` or `mps`; unset picks the best available.
`DECIS_DTYPE` forces a precision for **`kev-0.8b` only** — it is read by the engines that
consult `registry.DTYPE_DEFAULTS`, and Laya decides its own precision through its `Agent`.
Setting it on a Laya engine has no effect. Its use is re-measuring on your own hardware.
Without it, the dtype comes from the engine and device:

| Engine | cpu | cuda | mps |
|---|---|---|---|
| `laya*` | decided by Laya's own `Agent` | same | same |
| `kev-0.8b` | `fp32` | `bf16` | `fp32` |

The table is written in `DECIS_DTYPE` spelling. A response reports the wire value instead:
`float32`, `float16` or `bfloat16`, and `unloaded` before the engine has loaded — in
`/v1/models` and in the `decis` namespace of every answer.

`kev-0.8b` on CPU in `bf16` is **83× slower** than `fp32`. Forcing it logs a warning rather
than refusing to start. The measurement is in
[Performance](performance.md#dtype-per-engine-and-device).

## Weights

Resolution order is described in
[Configuration](configuration.md#engine-selection-and-weights). Two engine-specific notes:

- **Laya** checkpoints carry their own capacities, so a fine-tune with a different
  `max_len` is served with the right budget automatically.
- **kev** references its Qwen3.5 base by Hub repo id inside the adapter's checkpoint
  metadata, so a separately mounted copy of the base is not picked up. `decis download`
  places the base in the Hugging Face cache, and the engine runs offline from there.
