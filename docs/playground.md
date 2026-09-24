# Playground

**English** · [简体中文](playground.zh-CN.md)

[Documentation index](../README.md#documentation) · [Deployment](deployment.md) · [API](api.md)

The playground is three small browser games you can play yourself or hand to a live engine.
In AI mode every move is a real `POST /v1/systemone` request, and the probability bars are the
model's actual answers, so you can watch a decision model play a game instead of reading JSON.

`docker compose up` starts it next to the engine at <http://localhost:8080>.

| Page | What it asks per decision |
|---|---|
| `/snake` | One `choice` over four moves, from a flood-fill safety analysis |
| `/dino` | One `choice` over jump/duck/run, from a physics planner's safe/best marks |
| `/tetris` | A `choice` over five placements, a `choice` strategy, a `score` stack health, and a `noul` fit |

On a CPU-only host the games are slower than the original GPU deployment and each paces
itself around the latency it measures. A failed call is never passed off as a model decision:
snake plays its own simulation and says `Running (sim)`, dino shows an `API Error` badge and
keeps its planner's safety net, and tetris falls back to its local heuristic or retries. The
console holds the request and the error either way.

## How it is wired

The playground is a proxy, not a client:

```
browser ──POST /v1/systemone──▶ playground ──POST /v1/systemone──▶ engine
        (same origin, no key)              (attaches DECIS_API_KEY)
```

The browser never holds the API key. It needs none: it sends to the playground's own origin
and the playground attaches the token server-side. Two consequences follow, and both are
deliberate:

- **Anyone who can reach the playground's port can use the model.** Publishing it is the
  same decision as publishing the engine's port, without even a token prompt. The default
  binds every interface; `DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080` keeps it local.
- **The playground's port is the model's port.** The playground rewrites the request's
  `model` to the id the engine reported at `/readyz`, so the pages can ask for `jev-latest`
  and work under either engine profile.

### Finding the engine

The playground needs no configuration to find an engine. It asks `/readyz` on a list of
candidates and connects to the first that answers:

1. `http://laya-multilingual:8000`
2. `http://kev-0.8b:8000`
3. `http://host.docker.internal:8000`

So the same service works under `--profile kev-0.8b` and next to a `decis serve` running on
the host. While the engine is still loading, the playground reports `searching` and the
games say so instead of failing. Set `DECIS_PLAYGROUND_UPSTREAM` to name the engine and skip
the search, or `DECIS_PLAYGROUND_CANDIDATES` to replace the list.

It does not `depends_on` an engine: a Compose profile that is not active does not resolve
the service name at all, so a dependency on a stopped engine would fail to start.

## Interface

The four pages share one stylesheet ([`playground/web/theme.css`](../playground/web/theme.css)),
one i18n mechanism ([`playground/web/i18n.js`](../playground/web/i18n.js)) and one shell
([`playground/web/game.js`](../playground/web/game.js)). A page carries its own strings, its own
board and its own options; the manual/AI switch, the inference panel, the console for the last
call, the engine chip and the keyboard shortcuts come from the shell, so no page restates the
palette or re-implements a mode control.

The reading order is the same on all three games. The game bar carries the title, the live status,
the score and the controls. Under it sit the board and the model's readout — except on dino, whose
canvas is a wide side-scroller: there the run takes the full width and the model's next action goes
underneath it. Below them both is one row of two panels: the inference panel, and the console for
the last call, which starts **open**, request on the left and response on the right. The console
collapses to a single line if you would rather watch the board.

One switch (`M`) decides who plays: you, or the model. `enter` starts and pauses on every page
and `R` resets. In manual mode snake and tetris take the arrow keys — `space` is the hard drop in
tetris — and dino takes `space` or the up arrow to jump and the down arrow to duck.

The inference panel is fed only by the API's own `usage` and the browser's clock. Latency and
throughput are end-to-end, because the contract has no server-side timing; p50 and p95 describe
the last 200 calls, and a page that has not called anything yet shows dashes rather than a
plausible-looking number. The console prints the body of the last `/v1/systemone` call as it went
on the wire, next to the response or the error.

The interface is bilingual, English and Simplified Chinese. The language comes from
`navigator.languages` unless `?lang=zh` says otherwise; the switch in the app bar overrides
it, and the choice is remembered in `localStorage`.

**Only the interface is translated.** The `state`, `instructions` and `criteria` sent to the
model stay English, because that is the language the prompts are written in and because the
token budget these pages were sized against is the budget of those exact strings. Where a
page shows a value it also sends — an option name, a placement id — the label around it may
be localised, but the value on the wire is not.

## The placement shortlist

Which placements Tetris asks about is a capacity question, not a taste one, and both halves of it
are measurements.

**How many.** Laya charges a whole question against its `head_max_len` and rejects an over-budget
one instead of truncating it, so the page asks with the number that fits the smallest budget a
checkpoint can fall back to. Measured through the engine's own `measure()`: the placement question
is **178 tokens at five options** on an empty board and **185 at worst** once the board fills up
(45 real questions from two AI sessions — a filled board makes the option lines longer), and the
whole request goes from **415 to 475** of the 512-token `state + question` budget a checkpoint that
declares no limits falls back to. Six options would be about 207 and would get a 422. If an engine
still says no the page drops to its own heuristic, rather than showing a guess as an answer.

**Which ones.** Sorting the legal placements by the page's own heuristic and taking the top five
does not give five decisions: on a flat board the heuristic prefers one orientation at five
columns, and the first request this page sent had five options that were all `rot2`. The model was
choosing a column, not a placement — which is what a person watching the page sees as "it never
rotates". The page now puts the best placement of **each rotation** on the list first and fills the
remaining slots with the best of the rest. A piece with one distinct rotation (`O`) simply gets
five placements of that orientation. Measured over the first 26 real questions after the change: 23
offered two or more distinct rotations, 11 offered all four, and every question that offered one
was an `O`. Each option's key says which rotation it is (`rot<R>_col<C>`, `R` = the piece turned 90
degrees clockwise `R` times) and the panel shows the same rotation beside the option's bar.

Re-measuring after changing a page's questions does not need a tokenizer: an over-budget question
comes back as ``Question 'placement' is about N tokens, over this model's limit of M per
question``, and a successful response reports the same figures under `usage.input_tokens` and the
`decis` namespace. Send the page's own payload and read them off the console.

## Credits

The games are adapted from open-source projects, and the API and inference are Decis's:

| | |
|---|---|
| [taeold/djev-run](https://github.com/taeold/djev-run) | The Snake, Dino and Tetris pages. Decis changed their endpoint to the playground's own origin, removed the borrowed latency baselines, cut Tetris from sixteen verbose options to five terse ones to fit the default engine's token budget, and restyled and translated the interface. The games' physics and planner code comes from the projects that repository credits. |
| [trungdq88/jev-tetris](https://github.com/trungdq88/jev-tetris) | The Tetris page also credits it. |
| [kingfs/Decis](https://github.com/kingfs/Decis) | The server: the System One wire format, the engine abstraction, model inference and token accounting. The playground renders the games and proxies their requests; it owns none of the decision logic. |

Attribution is recorded in [`NOTICE`](../NOTICE).

## Build and run it yourself

The playground's image has no Python dependencies and its Dockerfile has no `RUN`, so it
builds in seconds:

```bash
docker build -f playground/Dockerfile -t decis-playground .
docker run --rm -p 8080:8080 -e DECIS_API_KEY=change-me \
  --add-host=host.docker.internal:host-gateway \
  -e DECIS_PLAYGROUND_CANDIDATES=http://host.docker.internal:8000 decis-playground
```

`--add-host` is what makes `host.docker.internal` resolve on Linux; Docker Desktop resolves
it on its own. `DECIS_PLAYGROUND_UPSTREAM` names one engine to try first (it is still probed,
not trusted). Without it, the proxy tries both engine container names, `host.docker.internal`
and `127.0.0.1`; the first that answers `/readyz` with 200 wins.

Or through Compose, beside an engine that is already running anywhere:

```bash
make build-playground
make up-playground
```

## Tests

[`tests/test_playground.py`](../tests/test_playground.py) stands up the playground and a
fake engine on loopback and checks the proxy without importing `decis`, because the
playground's image does not contain it. It asserts the pages are self-contained, that every
string a page asks for is declared, that the three games mount the same shell instead of their
own mode switch or engine poll, that the proxied request carries the playground's token and the
rewritten model id, and that the repository URL has one home.
