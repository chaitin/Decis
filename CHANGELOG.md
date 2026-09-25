# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
project is pre-1.0, the wire contract (`v1`) is stable and only adds fields; the server's own
version is reported by `/healthz`, while `/v1/models` reports the upstream package version
each engine will run.

## [Unreleased]

## [0.3.1] - 2026-09-25

### Added

- **A `v*` tag now publishes a GitHub Release.** The `release` job in
  `.github/workflows/docker-build.yml` waits for the images, then creates the Release from the
  tag with generated notes; a re-run finds it already there and does nothing. Until now a tag
  was published with no Release behind it.

### Changed

- **The READMEs and the guides were rewritten against the code.** The temporary status prose,
  the hand-written performance numbers (`AGENTS.md §8` requires those to come from
  `benchmarks/results/`) and the field values, CLI output and error codes the code does not
  have are gone; what replaced them is shorter and traceable to a file and line.
- **The READMEs lead with what the project is for, and print no measurements.** Three commands
  bring up an engine and the playground; the four highlights are out of the box, a Jev-like
  API, one published image per engine, and the three games. The latency table is gone,
  because the figure is a property of the host — the same release answers in hundreds of
  milliseconds on the machine it was measured on and several times faster on a laptop — so it
  lives on `docs/performance.md`, beside the host description and the method.

### Removed

- **`DECIS_LOG_PAYLOADS`.** It was documented but never read: no code path logs request or
  response bodies, so the variable did nothing. A knob that silently does nothing is worse
  than no knob.

### Fixed

- **The three games no longer act on an answer the board has moved past.** Reset, Pause and a
  mode switch cancel the call that is in flight, and each page re-checks the run it asked for
  after the await. A late snake answer used to kill a brand-new game on its first tick without
  moving it; a late dino answer ducked the dinosaur a person had taken over; and a late tetris
  answer locked a tetromino the manual player had already locked — one piece, two pieces on the
  board, and another consumed unplayed.
- **A call the engine did not answer is no longer counted as a model decision.** The tetris
  page's local fallback fed the agreement badge and painted its own simulated reads as the
  model's; only a live answer is counted and drawn now, and the fallback is labelled as the
  page's own read. Its fallback latch also no longer survives a Reset, so one failed fetch does
  not turn the page into a local game for the rest of its life.
- **The dino page cannot stall itself any more.** `resetGame` zeroed the in-flight counter that
  the outstanding calls decrement, which left it negative and stopped the AI from ever asking
  again; each game gets its own generation now, and a failed call waits the 250 ms backoff its
  reference uses instead of re-issuing immediately — a 503-answering engine was being asked
  roughly 200 times a second, and the page's own 503 body says a cold start takes minutes.
- **The pages' readouts say what was measured.** The dino page repainted its Deaths KPI as 0 on
  a language switch, and its action tags described the previous AI answer for a whole manual
  game (the planner runs in both modes now); the tetris health bar put its Clean end at 128% of
  its own track and its English health labels disagreed with the criteria the model is given;
  the snake page called an equally short route "longer", and refused a legal manual turn
  because it compared against the previous heading instead of the queued one.
- **A response body the page cannot use is a bad response, not a crash.** A 200 with no JSON
  body (a captive portal, a 204) threw in the snake page after `inflight` was set, and the flag
  then stayed set: Pause, Resume and Reset did nothing until the page was reloaded. The same
  body used to fabricate a `right` turn out of an empty `probabilities` object, before the
  safety net's own documented fallback could run.

## [0.3.0] - 2026-09-24

### Added

- **`DOCKERHUB_NAMESPACE`**, a repository variable that points the published images at
  another Docker Hub namespace. Without it the workflow publishes to `chaitin`, which is
  now a fixed part of the workflow rather than something derived from the login
  credential: an organization is not a user account, so `DOCKERHUB_USERNAME` (the login)
  is not necessarily the namespace the documentation names.

### Changed

- **The project moved to the `chaitin` organization.** Every clone URL, badge, issue link,
  image reference and workflow comment now names `chaitin/Decis` and `chaitin/decis`.
- The deployment guide describes versioned image tags as a scheme
  (`<engine>-<version>`) instead of listing what past releases produced. Those images live
  in the namespace the project used before the move, so the list named tags that are not in
  `chaitin/decis`.
- The package version is `0.3.0`.

### Fixed

- `tests/test_upstream_contract.py` no longer fails against a newer `laya`: its tokenizer
  double now accepts the `truncation` and `max_length` arguments that `laya` began passing
  to the tokenizer, and honours them the way the upstream truncation does. The declared
  range (`>=0.3.5,<0.4`) is now covered by the guard, not only its lowest bound.

## [0.2.0] - 2026-09-24

### Added

- **Playground API reference** at `/api`: the endpoint, the three question primitives, one
  captured request/response pair, a form that really posts, the parameters and limits, and
  both error tables — the engine's compared row by row against [`docs/api.md`](docs/api.md).
- **Recordings of the three games** ([`playground/web/media/`](playground/web/media/)): each
  page played in AI mode against a real engine, shown in both READMEs and on the index.
- New guards in `tests/test_playground.py` and `tests/test_docs.py`: the shared game shell and
  where its panels live, the request bodies the pages send, the recordings the index shows,
  the errors the `/api` page lists, and the version `/healthz` reports.

### Changed

- **The three games share one shell.** The manual/AI switch, the reasoning panel and the
  last-call console are now the same on all three pages; on snake and tetris the reasoning
  panel sits in the reading column beside the board, and the console below the game spans its
  full width.
- The playground index says how to use the page instead of printing the upstream URL.

### Fixed

- **Tetris now chooses a rotation.** Its shortlist was ordered by the page's own heuristic
  alone, which on a flat board put the same orientation in every slot, so the model was only
  ever choosing a column.
- The playground pages no longer send `samples`, `steps` or `seed`. The contract does not
  define those fields, and `extra="ignore"` had been hiding them.
- `/api` no longer overflows a 380px viewport: unbreakable error names pushed a table past the
  edge.

## [0.1.0] - 2026-09-23

### Added

- **API reference** ([`docs/api.md`](docs/api.md)): every endpoint, all three question
  primitives, the response envelope, the `decis` namespace, error codes and limits.
- **Generated API schemas** ([`docs/schema/`](docs/schema/)): JSON Schema for the request,
  response, model list and error body, plus the server's OpenAPI 3.1 document. They are
  generated from `src/decis/schema.py` by `docs/schema/export.py`, and CI fails if a
  checked-in file drifts.
- **User guides**, each in English and Simplified Chinese: getting started, configuration,
  API, engines, deployment, playground and performance.
- **Community files**: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` and this
  changelog.
- **Playground navigation and credits**: every page links back to the playground and out to
  this repository, and the index credits the open-source projects the games are adapted from
  and states that the API and inference are Decis's.
- `tests/test_api_schema.py` and `tests/test_docs.py`, guarding the generated schemas and the
  bilingual docs.

### Changed

- **The README is a landing page.** Installation, deployment, configuration, performance and
  development details moved into `docs/`, where each guide can be read on its own.
- **Performance tables moved** from the READMEs into
  [`docs/performance.md`](docs/performance.md) and its Chinese twin, still generated by
  `benchmarks/report.py` from the checked-in raw JSON.
- The package version is `0.1.0`.

### Fixed

- The user-facing documentation was checked against the code and corrected where it disagreed
  with it: a wrong `max_options` value in the API reference, `output_tokens` described as a
  count of answers, a timeout response the server never sends, engine capacities attributed to
  `decis models` (which reports whether an engine can run, not what it accepts), and a
  per-engine weight override that `config.py` silently drops for `kev-0.8b`.
  `tests/test_docs.py` now fails if a documented `DECIS_*` variable is read by nothing, or if a
  documented `DECIS_MODEL_PATH_*` variable does not name a registered engine.
- The Tetris page displayed one more placement option than it actually sent; the number shown
  now comes from the same constant the request is built from. On narrow viewports the page
  also overflowed horizontally between 380px and 430px, which is fixed by a breakpoint.
- The exported `openapi.json` reported the old package version.

## [0.0.1] - 2026-09-23

The first tagged release. Everything below is the initial implementation.

### Added

- The TypeSafe System One wire contract on `POST /v1/systemone` and `GET /v1/models`, with
  bearer authentication, the official error shapes, `x-typesafe-request-id` on every response
  and the documented `choice` / `score` / `noul` primitives.
- A pluggable engine layer with two model families: Laya (multilingual, English and
  typed-decisions checkpoints) and kev-0.8b.
- Weight resolution, `decis download`, `decis serve`, `decis models`, `decis doctor` and
  `decis bench`.
- Multi-arch Docker images with the weights baked in, one tag per engine, plus a weightless
  `-runtime` variant, and a `playground` image with three browser games.
- Compose and `make` entry points, a `Makefile` that delegates to Compose, and GitHub Actions
  for tests and image builds.
- The evidence-level wire contract ([`docs/api-compatibility.md`](docs/api-compatibility.md)),
  the design ([`docs/design.md`](docs/design.md)), its self-audit
  ([`docs/design-review.md`](docs/design-review.md)) and the feasibility study
  ([`docs/feasibility.md`](docs/feasibility.md)).

[Unreleased]: https://github.com/chaitin/Decis/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/chaitin/Decis/releases/tag/v0.3.0
[0.2.0]: https://github.com/chaitin/Decis/releases/tag/v0.2.0
[0.1.0]: https://github.com/chaitin/Decis/releases/tag/v0.1.0
[0.0.1]: https://github.com/chaitin/Decis/releases/tag/v0.0.1
