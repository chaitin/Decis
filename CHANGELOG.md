# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
project is pre-1.0, the wire contract (`v1`) is stable and only adds fields; the server's own
version is reported by `/healthz`, while `/v1/models` reports the upstream package version
each engine will run.

## [Unreleased]

### Added

- **`decis engines` lists the catalogue.** One row per shipped engine: the id `--engine` and a
  request's `model` field accept, its aliases, its extra, its Python floor, and the Hub
  repository `decis download` would fetch — with the declared size, the subfolder (three
  engines share one repository) and the base model an adapter resolves
  (`kev-0.8b -> Qwen/Qwen3.5-0.8B-Base`). Every value is read from `registry.SPECS` and the
  engine's own `weights()`. `decis models` keeps answering the other question — whether each
  id can run on this machine.
- **A slow Hugging Face now yields to ModelScope, not just an unreachable one.**
  `DECIS_HUB=auto` used to ask one question ("does the endpoint answer?") and a throttled or
  cross-border route answers *and* moves a 647 MiB checkpoint at a crawl — for hours. When a
  *specific* checkpoint is about to be fetched, both hubs are now asked to list the repository
  (which also settles whether they have it: neither `mstrasser/Jeff-*` has a ModelScope
  mirror) and are timed on the largest file that fetch would really pull, bounded to 1 MiB and
  6 s each — and each listing itself is bounded in bytes and in time, so a hub that trickles
  cannot hold the command open. The faster one wins if it is faster by at least 1.5x — below
  that the difference is noise, and the mirror cannot honor the pinned commit, so Hugging Face
  keeps the tie. Two facts move the fetch without a number: a 404 (the repository is not there)
  and a request that never completed (DNS, a refused connection, a timeout, a body that is not
  a listing) — the blocked route this fallback was always for. An *answer* with a status keeps
  Hugging Face, since a 401 on a gated repository (this listing carries no token), a 403, a
  429 or a 5xx is about the request rather than the route. The source line prints both rates,
  and every warning about the pin is unchanged.
- **A checkpoint already in either client's cache is never re-fetched.** Both clients are
  asked to look only at their own cache (`local_files_only`) before any choice is made, so a
  warm cache costs no probe, no measurement and no transfer — and "the other hub measured
  faster" cannot re-download gigabytes that are already on disk. With `--dest` (or
  `DECIS_MODEL_DIR`) the cached checkpoint is *copied* to `<dir>/<engine id>/` instead of being
  handed back to a client to lay out, which needs no network at all — and a client's own
  transport error (`httpx.ConnectError` and friends) is now one `error:` line rather than a
  traceback. `MODELSCOPE_ENDPOINT` (and the older `MODELSCOPE_DOMAIN`) is now read, so the
  measurement describes the network the client will use rather than the public host.
- **`decis download` falls back to ModelScope when Hugging Face cannot be reached.** A fetch
  probes `<HF_ENDPOINT>/api/models?limit=1` once (3 s timeout; any HTTP status counts as
  reachable, so a private mirror is not mistaken for a blocked host) and only then picks a
  client. `DECIS_HUB=auto|huggingface|modelscope` — or `--hub` on `serve`, `models`, `doctor`
  and `download` — pins the source instead, and a pinned source never probes and never
  substitutes. ModelScope's revisions are branch and tag names and its mirrors of these
  repositories carry `master` only, so the commit this repository pins cannot be honored
  there: Decis never passes it, `hub.Selection.pinned` records the deviation, `decis download`
  says so before transferring anything and `decis doctor` reports which source this host
  would use. `uv sync --extra download` installs both clients; every engine extra pulls it in.
- **The base model of an adapter engine is fetched and resolved like a checkpoint.**
  `kev-0.8b`'s Qwen3.5 base lands next to the adapter when a model directory is configured
  (`<dir>/Qwen3.5-0.8B-Base/`), and `engines/kev.py` hands `transformers` that directory. A
  mounted model directory is therefore self-contained, and a base fetched from ModelScope is
  usable at all — `transformers` only ever looks in the Hugging Face cache.
- **`decis doctor` reports which device this host will serve from, and an idle GPU is no
  longer silent.** A new `compute` section prints the device the engines would pick (`auto`
  or a `DECIS_DEVICE` pin), the installed `torch` build (`2.14.0, CUDA 13.0, 1 CUDA
  device(s), 0: NVIDIA GeForce RTX 4070`), every GPU `nvidia-smi` reports, and an `advice`
  line when those disagree. The same check runs at load time in all three engines
  (`engines/devices.py: warn_if_accelerator_is_idle`): an engine that lands on `cpu` with no
  pinned device now says whether the machine has a GPU it could have used, and which command
  fixes it (§2-D36). A machine with no GPU gets no line at all — that is a normal CPU host,
  and a warning printed on every start is a warning nobody reads.

### Fixed

- **`kev-0.8b` fetched its adapter from Hugging Face even when another source was
  selected.** The engine handed the vendored checkpoint code a `repo@revision` string, and
  that code calls `huggingface_hub.snapshot_download` itself — so `DECIS_HUB=modelscope` and
  `--hub modelscope` did not apply to the adapter: with no `--dest`/`DECIS_MODEL_DIR` the
  engine went back to Hugging Face every time, so on a network where only ModelScope answers
  `kev-0.8b` could not load at all -- not even after `decis download` had put the adapter in
  ModelScope's cache, because `paths.resolve` has no candidate directory there. A local hit
  (the `--dest` case) passed the directory through, which is why this was invisible here. The adapter now goes through
  `paths.fetch_checkpoint` like the base model already did, and the loaders are handed
  directories (`docs/design-review.md` §2-D33).
- **`kev-0.8b`'s declared download size was 13 MB; the three files are 43.3 MiB.** The old
  number was a written-down guess that `human_bytes` rendered as `12.4 MiB`, which is how it
  passed for a measurement in five documents and in `decis download`'s own output. It is now
  derived from a per-file measurement
  (`_ADAPTER_MANIFEST`) with a guard that also rejects a total smaller than a single weight
  file. The base model — 1.65 GiB, 98% of a kev fetch — had no declared size at all, so
  `decis download` neither printed it nor checked room for it; `docs/design.md §7.2` carried a
  figure nothing could verify. Both are measured now (§2-D35).
- **`decis doctor` printed its engine list from a second, default `load_settings()`.** With
  `--model-path` or `DECIS_MODEL_DIR` set, the command reported `model paths
  kev-0.8b=/srv/…` and `kev-0.8b needs weights` on adjacent lines, while `decis models`
  disagreed. The classifier is now asked with the settings this run resolved (§2-D34).
- **On Windows, `uv sync --all-extras` installed a CPU-only `torch`, so the GPU was
  unreachable before any Decis code ran.** PyPI publishes Windows `torch` wheels without CUDA
  — the dependency list in the previous `uv.lock` carried `cuda-toolkit`, `nvidia-cudnn-cu13`,
  `nvidia-nccl-cu13`, `triton` and `cuda-bindings` under `sys_platform == 'linux'` only — so
  `torch.cuda.is_available()` was `False` on a machine with an RTX card in it, and no device
  detection could have found the GPU. `pyproject.toml` now resolves `torch` and `torchvision`
  from the PyTorch CUDA index on Windows (`[[tool.uv.index]]` + `[tool.uv.sources]` with a
  `sys_platform == 'win32'` marker, `explicit = true`), and `uv.lock` carries two forks:
  `2.14.0+cu130` for Windows and the unchanged PyPI `2.14.0` for Linux and macOS. The CUDA
  wheel is about 1.9 GiB against 124 MiB, so a Windows host with no NVIDIA GPU — or a driver
  older than the CUDA 13.0 generation — should take the small one back with
  `uv sync --all-extras --no-sources` (§2-D36). **Not verified on real Windows hardware**:
  what is asserted is that Windows resolves to the `+cu130` wheel (from `uv.lock`, with
  guards) and that the diagnosis above is what a CPU-only build produces.

### Changed

- **The source decision is now a measurement, and says so.** `decis download` prints what each
  hub measured, or which one did not have the repository; `decis doctor` names no checkpoint,
  so it reports reachability and prints `hub speed  not measured (no engine named)` instead of
  implying a number no fetch would necessarily get. A pinned `--hub`/`DECIS_HUB` still probes
  nothing, measures nothing and never substitutes. The cost is bounded and documented: two
  listings and two 1 MiB samples at most, and nothing at all from a warm cache
  (`docs/design-review.md` §2-D37).

- **Weight-bearing images are built only at release.** A push to the default branch publishes
  the weightless `<engine>-runtime` images, a feature branch keeps
  `<engine>-runtime-sha-<7>`, and a pull request still builds one engine-free image, so no
  branch push downloads weights. A release tag builds both variants, publishes
  `<engine>-<version>` and `<engine>-<version>-runtime`, moves the unsuffixed `<engine>` and
  `<engine>-runtime` aliases onto them, and points `latest` at that release's baked
  `laya-multilingual` image. The manual dispatch input is now `variants: runtime|baked|both`
  (default `runtime`) instead of a `runtime_variants` boolean.

## [0.4.0] - 2026-09-30

### Added

- **Two more engines: `jeff-qwen3.5-0.8b` and `jeff-gemma4-e2b`.** Both are fine-tunes of
  [jeff](https://github.com/firelex/jeff) (MIT, vendored as a minimal subset — see `NOTICE`),
  a prefill-only decision-model family: one readout over the last hidden state, a per-checkpoint
  answer vocabulary and a fitted softmax temperature from `decision_config.json`. The Qwen one
  is 1.61 GiB of weights, the Gemma one 8.65 GiB, each baked into its own image tag. `jeff`,
  `jeff-qwen` and `jeff-qwen3.5` are aliases for the Qwen checkpoint; `jeff-gemma` and
  `jeff-gemma4` for the Gemma one.
- **The caller's own JSON now reaches engines that were trained on it.**
  `PreparedQuestion` gained `raw`, `PreparedRequest` gained `raw_state`, and `WorkItem` carries
  `raw_state`. Jeff prompts with `json.dumps(state)` and the criteria object as written, not with
  the flattened `key: value` text `render.py` produces for the classification-family engines —
  and a fine-tune fed the flattened form does not error, it just answers worse. The flattened
  path is unchanged for every existing engine. This is the one recorded exception to "adding an
  engine never changes `render.py`" (`AGENTS.md §5`).
- **`--model-path ENGINE=PATH`** on `serve`, `models` and `doctor` (repeatable, aliases
  accepted), replacing the per-engine `DECIS_MODEL_PATH_<ENGINE_ID>` variables.
- The `jeff` extra, plus the two new tags in the workflow matrix, the `docker-compose.yml`
  profiles, `.env.example` and the playground's engine picker.

### Changed

- **The documentation was reorganised, and the stale parts were removed.**
  [`docs/design-review.md`](docs/design-review.md) now separates the two defects that are still
  open (§2.1: `D11`, `D13`) from the 29 that are fixed and guarded (§2.2, each condensed to
  symptom → fix → guard, with the full incident narrative left in git history). Every `§2-Dxx`
  and `§4-Mx` reference used by `AGENTS.md`, the tests and the code still resolves. Statements
  that no longer matched the code were corrected across `docs/design.md`,
  `docs/feasibility.md`, `docs/api.md`, `docs/configuration.md`, `docs/deployment.md`,
  `docs/engines.md`, `docs/performance.md`, `docs/playground.md`, `docs/getting-started.md` and
  `benchmarks/README.md` — the engine matrix, the directory tree, kev's weight sizes, the
  `/v1/models` `aliases`, the `batch_size` field, the `decis models` statuses, the playground
  candidate list and the `report.py` targets were all wrong or out of date in at least one place.
  Colloquial jargon (CI "构建腿", "烤权重", a failing test "红了", "假引擎") was replaced with
  plain wording in both languages of every page.

### Removed

- **`DECIS_MODEL_PATH_<ENGINE_ID>` is gone, and reading it never worked for most ids.** The
  variable *name* had to encode the engine id, and no variable name can hold the dot in
  `kev-0.8b`: `config.py` normalised the suffix to `kev-0-8b`, found no such registered engine,
  and **dropped the override without a word**. Every Jeff id contains a dot, so the form was
  removed rather than documented around — `--model-path ENGINE=PATH` puts the id in the value,
  where it can be spelled exactly, canonicalises aliases in `cli._parse_model_paths`, and reports
  an unknown id as a `ConfigError` instead of doing nothing (`docs/design-review.md §2-D27`).

### Fixed

- **Both Jeff engines now say they need Python 3.12 instead of failing on 3.11 with a syntax
  error, and say why.** The vendored serving code uses PEP 695 `type` aliases (including recursive
  ones, which cannot be rewritten without breaking the two-way hash check in
  `tests/test_jeff_vendor.py`), so `import decis.engines._jeff_vendor` is a `SyntaxError` on 3.11 —
  an interpreter this project declares support for (`requires-python = ">=3.11"`) and tests in CI.
  The first push of these engines was therefore red on the 3.11 leg while both local environments
  (3.14) were green, and `decis models` on 3.11 blamed a missing dependency and recommended
  `uv sync --extra jeff`, which installs nothing there. The floor now has one home per layer —
  `EngineSpec.python_min` for `registry.status`, `jeff.MIN_PYTHON` for the `load()` guard, a
  `python_version >= '3.12'` marker on the extra's dependencies — and `decis models` reports
  `needs Python 3.12+   this interpreter is 3.11.16   (the vendored serving code uses PEP 695
  type aliases)`. Guards: `tests/test_engines_jeff.py` pins the three numbers together and
  derives the floor from the vendored sources with `ast.parse(..., feature_version=(3, 11))`
  instead of trusting a hand-written constant (`docs/design-review.md §2-D31`).
- **A sharded checkpoint that is only half on disk is no longer called ready.**
  `paths.checkpoint_root` accepted any directory holding the engine's marker file, which is
  enough for a single-file checkpoint and wrong for a sharded one: `model.safetensors.index.json`
  arrives before the shards do, so an interrupted `decis download --dest`, a `cp` that ran out of
  disk, or a partly synced volume produced a directory that `decis models` called **ready** while
  the loader raised `FileNotFoundError` for a shard nobody was told to expect. Found by pointing a
  load probe at the Gemma Jeff checkpoint while its 8.65 GiB were still arriving
  (`docs/design-review.md §2-D29`). The new `paths.missing_shards` reads the checkpoint's own
  `weight_map` and requires every file it names, so an incomplete local copy now falls through to
  the Hub fetch it was shadowing, and `decis download` says which files are missing instead of
  suggesting you look for `config.json`, which is there.
- **The `jeff` extra did not install `torchvision`, and the load failed without it.**
  `AutoProcessor.from_pretrained` builds every sub-processor the checkpoint's
  `processor_config.json` names; the Qwen checkpoint descends from Qwen3-VL, so that file names a
  `Qwen3VLVideoProcessor`, and that class raises `ImportError: ... requires the Torchvision
  library` while being imported — before any weight is read, from a path that never touches a
  video. "It is a text-only model, so the image dependency is not needed" was a conclusion drawn
  from reading code, and the weights suite disproved it on the first real load
  (`docs/design-review.md §2-D28`). The new fast guard,
  `tests/test_engines_jeff.py::test_the_extra_declares_every_required_module`, reads the extra
  out of `pyproject.toml` and compares it with `WeightSpec.requires`, so the two lists cannot
  drift again in the environment where the dependency is not installed.
- **A `NO_PROXY` entry written as a bracketed IPv6 literal stopped every weight download.**
  `httpx` builds a `URLPattern` for each bypass entry and does not recognise `[::1]` as an
  address, so it takes the domain branch, builds `all://*[::1]`, and raises
  `InvalidURL: Invalid port: ':1]'` from inside the client constructor — before a byte is sent.
  `huggingface_hub` is one of those clients, so `decis serve` and `decis download` both failed on
  hosts whose list is written that way, with an error naming neither the variable nor the proxy.
  The rule now lives once, in `decis.config.normalize_proxy_environment` (and
  `ensure_loopback_bypass` for the loopback names), it runs inside `load_settings` so it is in
  place before anything can build a client, `decis doctor` reports what it changed, and the five
  hand-rolled copies that used to sit in the test suite, the SDK example and the benchmark
  harnesses now call it.
- **The pinned revision is the one that gets loaded.** The Hub branch passed `laya.Agent` a
  repository id, and upstream's `Agent` has no `revision` parameter: the log promised this
  build's pinned commit while the loader read `main`. It now goes through
  `paths.fetch_checkpoint`, which fetches with `paths.download_arguments` (revision included) and
  validates the answer with the same `checkpoint_root` predicate a mounted directory passes.
- **`Uvicorn running on ...` no longer reads as "the model is ready".** `decis serve` prints a
  banner before it binds — engine, weight source and cold-cache size, bind address — and the log
  says that `/readyz` returns 503 and `/v1/*` is refused until the engine is loaded. The new
  opt-in `--preload` reverses the order for console use (load first, then bind), documented
  together with what it costs: nothing answers during the load, `/healthz` included. The default
  stays non-blocking, because a probe has to be able to tell "starting" from "crashed".

## [0.3.2] - 2026-09-26

### Fixed

- **`DECIS_DEVICE` now reaches the Laya engines.** They never passed it to `laya.Agent`, so
  the engine kept Laya's own device order — CUDA, then Metal, then CPU — whatever the variable
  said. On an Apple-silicon Mac that order ends at the GPU, which made `DECIS_DEVICE=cpu` a
  documented knob that silently did nothing: the mirror image of the `DECIS_LOG_PAYLOADS`
  removal below, and the same reasoning applies. A value that is not `cpu`, `cuda` or `mps` is
  now refused by name instead of reaching `torch.device`, and a device Laya cannot use is a
  warning at load time rather than only a `print`.
- **A device request that cannot be honoured no longer looks like one that was.** With
  `DECIS_DEVICE` set, the fallback logs the device that actually loaded, and the `loaded ...
  device=... threads=...` line names the thread count torch resolved to — the two facts that
  tell a container apart from the host it is running on.
- **The snake page's food is no longer placed by a formula.** `spawnFood` picked
  `(steps * 17 + score * 31 + 7) % free.length`, which reads like a shuffle and is not one: it
  is a pure function of the game state, and everything downstream of it is pure too — the
  planner is, and the engine answers an identical request identically (`AGENTS.md` §3-11). So
  every run was the same run. Playing the shipped page's own script eight times gave the same
  first apple, the same 263 steps and the same final score eight times; with the reachable
  cells now chosen uniformly at random, eight runs gave eight different first apples and eight
  different scores. Which reachable cell the food lands on is a coin toss; *that* it is
  reachable is unchanged, and still the invariant that keeps the planner's `allowed` set from
  going empty.

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

[Unreleased]: https://github.com/chaitin/Decis/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/chaitin/Decis/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/chaitin/Decis/releases/tag/v0.3.2
[0.3.1]: https://github.com/chaitin/Decis/releases/tag/v0.3.1
[0.3.0]: https://github.com/chaitin/Decis/releases/tag/v0.3.0
[0.2.0]: https://github.com/chaitin/Decis/releases/tag/v0.2.0
[0.1.0]: https://github.com/chaitin/Decis/releases/tag/v0.1.0
[0.0.1]: https://github.com/chaitin/Decis/releases/tag/v0.0.1
