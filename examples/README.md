# Examples

Runnable examples, in the order a newcomer should read them.

| File | What it shows |
|---|---|
| [`curl.md`](curl.md) | Raw HTTP. Every endpoint, all three question primitives, and the error contract — one `curl` command per example, each with the status code it returns. |
| [`python_sdk.py`](python_sdk.py) | The **official** `typesafe-sdk`, unmodified, pointed at Decis instead of `api.typesafe.ai`. This is the whole compatibility argument in 40 lines. |

## Before you start

```bash
uv sync --extra dev            # `dev` includes typesafe-sdk, needed by python_sdk.py
cp .env.example .env           # set DECIS_API_KEY in it
uv run decis serve --host 127.0.0.1 --port 8000 --engine mock
```

`mock` needs no weights, so it starts instantly and answers deterministically — the same
request always returns the same numbers. That makes it the right engine for learning the
shape of the API. Swap `--engine laya-multilingual` (after `uv sync --extra laya` and
`uv run decis download --engine laya-multilingual`) when you want real inference; the
requests do not change.

Wait for readiness before sending traffic:

```bash
curl -s localhost:8000/readyz          # {"status":"ready","engine":"mock"}
```

Every example below sends `Authorization: Bearer local` and uses `model: "mock"`. If you
did not set `DECIS_API_KEY`, a server on a loopback address accepts no token at all — see
[`docs/design.md §12`](../docs/design.md) for why that is the safe default and why it is
refused on a public interface.

## These examples are tested

`tests/test_examples.py` starts a real server, executes **every** command in `curl.md`,
and asserts the status code printed next to it. It also runs `python_sdk.py`. If a command
here stops working, or the API changes shape, CI fails.

That is why the numbers in `curl.md` are real: they were captured from a running server,
and the test re-derives the important ones. The mock's answers are stable, so the choice,
score and `noul` values shown there are asserted, not illustrative.

## Where to go next

- [`../README.md`](../README.md) — install, deploy, and the measured performance table.
- [`../docs/api-compatibility.md`](../docs/api-compatibility.md) — the wire contract, with
  an evidence level on every claim.
- [`../docs/design.md`](../docs/design.md) — why the server is built the way it is.
