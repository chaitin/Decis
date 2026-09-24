# Contributing to Decis

Thanks for taking the time. This document is the practical guide: how to set up, how to run
the tests, and what a pull request needs to contain. [`AGENTS.md`](AGENTS.md) is the
normative engineering contract — canonical homes, invariants, banned patterns — and applies
to human and automated contributors alike. Where the two disagree, `AGENTS.md` wins.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## What contributions fit

- **New engines** are the most common extension. One module plus one registry line, with two
  test suites. [`AGENTS.md §5`](AGENTS.md) is the step-by-step contract; read it first.
- **Contract work** — anything touching request or response fields — starts at
  [`docs/api-compatibility.md`](docs/api-compatibility.md), the single source of truth, and
  must update it in the same pull request.
- **Bug reports** are welcome as issues. A reproduction with the request id from the
  response header makes it much faster to act on.
- **Security reports** must not go in a public issue. See [`SECURITY.md`](SECURITY.md).

Before starting something large, open an issue so we can agree on the shape. A
well-argued "no" early is cheaper than a rewrite.

## Development setup

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/chaitin/Decis && cd Decis
uv sync --extra dev
cp .env.example .env          # set DECIS_API_KEY to anything for local work
uv run pytest -q              # the weight-free suite: no weights, no network
uv run ruff check && uv run ruff format --check
```

The weight-free suite is what CI runs. It needs no model and no network.

### The two environments

Two environments catch different bugs, and both are required before you call a change done:

| Environment | How | What it catches |
|---|---|---|
| Weight-free | `uv sync --extra dev` | The API, the contract, and the engine-free paths |
| Engine installed | `uv sync --extra dev --extra laya` | Dependency-gated branches of the engine code |

Keep the two side by side under different prefixes, so running the second never replaces the
first. `.scratch/` is git-ignored and holds the engine-installed one:

```bash
uv sync --extra dev                                   # .venv -- CI's environment
uv run pytest -q

uv venv .scratch/venv
uv pip install --python .scratch/venv/bin/python -e '.[dev,laya]'
.scratch/venv/bin/python -m pytest -q
```

The weight-free suite skips every branch that needs an engine's dependencies, so a real bug
can hide in a class of code it never executes. The engine-installed environment has the
opposite blind spot: it cannot tell you whether the "dependency missing" message is clear,
because the dependency is present. Do not assume which environment you are in — use
`pytest.skip` with a reason rather than asserting that a package is or is not installed.

Real weights are a third, opt-in suite:

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual
uv run pytest -m weights      # loads the checkpoint; minutes on CPU
```

## Checks CI runs

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
uv run python benchmarks/report.py --check     # docs' performance tables match the raw JSON
uv run python docs/schema/export.py --check    # checked-in JSON Schema matches the models
```

All four must pass. The last two exist because a number or a field that was edited by hand
is a lie that no other test catches.

The playground pages are guarded by `tests/test_playground.py`, which compares them with the
sources they restate: the palette and typography in `playground/web/theme.css`, the shared
shell in `playground/web/game.js`, the interface language in `playground/web/i18n.js`, and the
error table on `/api` against `docs/api.md`. Run it alone with
`uv run pytest -q tests/test_playground.py`; it needs no model and no browser.

## Pull-request rules

- **One pull request, one thing.** A new engine is one pull request. A contract change is
  its own pull request, and it must update `docs/api-compatibility.md`.
- **Contract and error-code changes must include a live differential run.** Offline tests
  cannot detect the hosted API disagreeing with its own OpenAPI document. Run:

  ```bash
  TYPESAFE_LIVE_API_KEY=<key> uv run pytest tests/test_contract_sdk.py -m network
  ```

  and paste the result into the description.
- **Performance pull requests must add raw JSON** under `benchmarks/results/` and quote the
  path. Any measured mechanism needs a **positive control** — a harness that cannot detect a
  gain cannot tell "the mechanism does not help" from "the measurement is broken".
- **Never hand-write a number in the docs.** Tables are rendered by
  [`benchmarks/report.py`](benchmarks/report.py) from checked-in JSON; edit the JSON or
  re-measure.
- **Do not add a second source of truth.** If a concept needs a constant in two places, one
  of them is a bug. [`AGENTS.md §2`](AGENTS.md) lists every canonical home, and
  [`tests/test_conventions.py`](tests/test_conventions.py) enforces the layering.
- **New third-party code needs a `NOTICE` entry**, and vendored code is copied byte-for-byte
  with its sha256 pinned.

### Documentation changes

User-facing documentation is bilingual: every guide exists as `docs/<name>.md` and
`docs/<name>.zh-CN.md`. When you change one, change both, and keep the structure aligned —
`tests/test_docs.py` checks that the pair exists, links to each other, and that every
relative link in them resolves. Code blocks, command names, links and field names stay
identical; only prose is translated.

Write for the reader: concrete, first-person-plural, code before adjectives. State what is
measured and what is not; "not verified" is a useful sentence, a confident guess is not.

### Adding an engine, in one paragraph

Implement `DecisionEngine` in `src/decis/engines/<name>.py`, register it in
`engines/registry.py` by string path so importing the registry stays cheap, declare its
dependencies as a `pyproject.toml` extra, and declare its capacities honestly — especially
`measure()`, which must use the real tokenizer and the real sequence layout. Add a
weight-free test suite and a `-m weights` suite. If you find yourself needing to change
`render.py` or `answers.py`, stop and open an issue: the abstraction is wrong, not your
engine.

## Releasing

A release is one tag. The workflow does the rest, and nothing else should be done by hand:

```bash
# `__version__` in src/decis/__init__.py and the matching `## [x.y.z]` section in
# CHANGELOG.md are one change -- tests/test_docs.py compares them.
git tag v0.4.0 && git push origin v0.4.0
```

Pushing a `v*` tag makes [`docker-build.yml`](.github/workflows/docker-build.yml) publish the
versioned image tags ([Deployment](docs/deployment.md) describes the scheme) and then create
the GitHub Release for that tag, with notes generated from the commits since the previous one.
The release step waits for the images, so a Release never announces an image that failed to
publish, and it is idempotent: re-running the workflow for a tag that already has a release
does nothing rather than failing.

`v<version>` appears in [`SECURITY.md`](SECURITY.md), both READMEs, and
`docs/deployment.md`; `tests/test_docs.py` fails if any of those literals disagrees with
`decis.__version__`, so the bump is the one edit that touches several files on purpose.

## Commit and review style

- Small, self-describing commits. The subject says what changed and why in one line; the body
  carries the reasoning.
- Explain *why*, especially for a rule: a reader six months from now needs the reason, not
  just the change.
- Expect review to ask for the measurement, the guard test, or the negative control. That is
  the project's normal standard, not a comment on your patch.

## License

Decis is Apache-2.0. By contributing you agree that your contribution is licensed under the
same terms. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
