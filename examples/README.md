# Examples

Runnable examples, in the order a newcomer should read them.

| File | What it shows |
|---|---|
| [`curl.md`](curl.md) | Raw HTTP. Every endpoint, all three question primitives, and the error contract — one `curl` command per example, each with the status code it returns. |
| [`python_sdk.py`](python_sdk.py) | The **official** `typesafe-sdk`, unmodified, pointed at Decis instead of `api.typesafe.ai`. This is the compatibility argument, executable. |

## Before you start

```bash
uv sync --extra dev --extra laya   # `dev` includes typesafe-sdk, needed by python_sdk.py
cp .env.example .env               # set DECIS_API_KEY in it
uv run decis download --engine laya-multilingual   # ~647 MiB
uv run decis serve --host 127.0.0.1 --port 8000 --engine laya-multilingual
```

`laya-multilingual` is the server's default engine. On CPU it takes 75–90 seconds to load
before `/readyz` turns green; poll it before sending traffic:

```bash
curl -s localhost:8000/readyz          # {"status":"ready","engine":"laya-multilingual"}
```

Every example below sends `Authorization: Bearer local` and does not name a model, so it is
answered by whichever engine the server runs. If you did not set `DECIS_API_KEY`, a server on
a loopback address accepts no token at all — see [`docs/design.md §12`](../docs/design.md)
for why that is the safe default and why it is refused on a public interface.

## These examples are tested

`tests/test_examples.py` starts a real server, executes **every** command in `curl.md`, and
asserts the status code printed next to it. It also runs `python_sdk.py`. If a command here
stops working, or the API changes shape, CI fails.

The response bodies in `curl.md` are **shapes, not captured output**: values depend on the
engine and its weights, so the doc writes `<…>` where a number goes and a test asserts that
the fields it names are the ones the wire contract defines. For real numbers, run the
commands — or read the evidence-level table in
[`../docs/api-compatibility.md`](../docs/api-compatibility.md).

## Where to go next

- [`../README.md`](../README.md) — what Decis is, the engines, and the measured latency table.
- [`../docs/getting-started.md`](../docs/getting-started.md) — install, first request, readiness.
- [`../docs/deployment.md`](../docs/deployment.md) — Docker, Compose, Kubernetes.
- [`../docs/api-compatibility.md`](../docs/api-compatibility.md) — the wire contract, with
  an evidence level on every claim.
- [`../docs/design.md`](../docs/design.md) — why the server is built the way it is.
