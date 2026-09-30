# Vendored from jeff

`model.py`, `decoder.py` and `types.py` are copies of files from
[firelex/jeff](https://github.com/firelex/jeff), the training and serving code for the
Jeff decision models. `LICENSE` is upstream's, unmodified. **Nothing here may be edited
by hand**: `tests/test_jeff_vendor.py` recomputes the sha256 of every file and fails if
one changes, so "did we alter the model we claim to run?" stays a one-command question.

| File | Upstream path | Upstream bytes | Upstream sha256 | Vendored sha256 |
|---|---|---|---|---|
| `model.py` | `src/jeff/model.py` | 15283 | `74729564f6f2cfe129f6ccbb16a9fd37466b38863e1f920e3cab16c7f535fe73` | `c34cb7439321fce84e964b8357bee627ae2e32f80d8969b443a82c0d28d080bf` |
| `decoder.py` | `src/jeff/decoder.py` | 10694 | `a81775fc69592c2701ab300458eb61e3477ccdf5d3102f17f47f99d0dc62955d` | `8e156b92b81811f9a52c17ad7634a24f370f587ff5b16d5aa0ab47faf2b6d98d` |
| `types.py` | `src/jeff/types.py` | 1852 | `0f8e3bfa4e92693ec50b9f258fb9180438a62d30caf9b3e6c00860a71e46dc5a` | `0f8e3bfa4e92693ec50b9f258fb9180438a62d30caf9b3e6c00860a71e46dc5a` |
| `LICENSE` | `LICENSE` | 1162 | `ab061adada2c603bdd6d7773bd89a0c48af32f9ddb3c5a12086b6f5c7c364bdb` | same |

- **Source**: https://github.com/firelex/jeff
- **Pin**: tag `v1.1` = `f0397f3785d93f73a01411d785d2ed026f53181d` (2026-09-29)
- **Licence**: MIT (see `LICENSE` here and the `NOTICE` entry at the repo root). The
  *checkpoints* are a separate question: both Hub repositories declare `apache-2.0` for the
  weights, and the Gemma one links Google's Gemma 4 terms because its base is
  `google/gemma-4-E2B-it`. Code licence and weight licence are recorded separately on
  purpose.

## The one edit: relative imports

Unlike kev -- whose vendored files already used `from .model import ...` and so stood
alone byte-for-byte -- jeff uses absolute imports against its own distribution name.
Two lines were rewritten so the files work as a subpackage of `decis`:

| File | Upstream | Vendored |
|---|---|---|
| `model.py:22` | `from jeff.types import Answer, Content, ...` | `from .types import Answer, Content, ...` |
| `decoder.py:18` | `from jeff.model import MAX_OPTIONS, PreparedBatch, ...` | `from .model import MAX_OPTIONS, PreparedBatch, ...` |
| `decoder.py:19` | `from jeff.types import DecisionInput, JSONValue` | `from .types import DecisionInput, JSONValue` |

Nothing else differs, which is why the table above records both hashes: the vendored
hash is what the guard checks, and the upstream hash is what makes the rewrite
falsifiable (`tests/test_jeff_vendor.py -m network` re-fetches the pinned files,
applies exactly this substitution, and compares the bytes).

## Why vendor instead of depending on jeff

- **jeff is not on PyPI.** The distribution name in its `pyproject.toml` is `jeff`, so a
  dependency would resolve to whatever unrelated project holds that name.
- **Its runtime pins are a whole application's.** `[project.dependencies]` pins
  `torch==2.14.0`, `torchvision==0.29.0`, `transformers==5.17.0`, `pillow==12.3.0`,
  `fastapi==0.141.1`, `uvicorn==0.52.4`. A git dependency would drag torchvision into
  every image that can serve a Jeff checkpoint and would let an unrelated project's
  exact pins decide Decis's dependency graph. Decis declares its own floors instead
  (`pyproject.toml`), and the combination that is tested is recorded in
  `docs/design.md §7.2`.

## What the checkpoints say about their own provenance

Both released checkpoints carry a `decision_config.json` whose `provenance` block names
`git_commit a095c98f3fd2782bba033b378f7375ecc33c35fa` and sha256s for the training-time
`model.py` (`cbb5c04f...`) and `decoder.py` (`2f311639...`). **That commit is not in the
public history** -- `GET /repos/firelex/jeff/commits/a095c98...` answers 422, and no
public ref contains those blobs; `types.py` is the only recorded file that matches any
public commit. The repository was rewritten between the training runs and the `v1.0` /
`v1.1` tags, so the exact source of the checkpoints cannot be reconstructed from GitHub.

`v1.1` is therefore the closest reproducible pin, chosen over `main` because its
`decoder.py` predates the additive Phi-4 base entry (the only difference between `v1.1`
and `main` in these files). Two things bound the remaining risk:

- the loader compares the checkpoint's own `codes` / `token_ids` against what the
  vendored code derives from the checkpoint's tokenizer and refuses to load on a
  mismatch (upstream's check, kept verbatim);
- `tests/test_jeff_inference.py` (marker `weights`) compares this engine's distributions
  with upstream's own `DecisionModel.predict` / `GenericDecoderDecisionModel.predict`
  run on the same rows, so a serving-path difference is a test failure rather than a
  quietly worse answer.

## What was left out, and why

| Upstream file | Why not vendored |
|---|---|
| `src/jeff/server.py` | The wire format is Decis's. Vendoring it would also add a second `confidence` implementation and hardcode `host="127.0.0.1"` (`AGENTS.md §2`, `§9`). |
| `src/jeff/data.py`, `train.py`, `evaluate.py`, `panel.py`, `synthetic.py`, ... | Training and evaluation tooling; upstream itself keeps `datasets`, `matplotlib` and `httpx` in a separate `train` group. |
| `src/jeff/mlx_backend.py` | Apple-silicon backend for the Qwen checkpoints only. Decis serves every engine through `engines/devices.py`, so a second device story would contradict `DECIS_DEVICE`. |
| `src/jeff/games/`, `src/jeff/latency.py`, `src/jeff/voice.py` | Demos and harnesses. |

## Re-vendoring

```bash
cd /path/to/jeff && git checkout <new-tag>
sed -e 's/^from jeff\.types import/from .types import/' \
    -e 's/^from jeff\.model import/from .model import/' \
    src/jeff/model.py src/jeff/decoder.py src/jeff/types.py
# copy the three files and LICENSE into src/decis/engines/_jeff_vendor/
sha256sum src/decis/engines/_jeff_vendor/{model.py,decoder.py,types.py,LICENSE}
```

Then update the table above, the `REVISION` constant in `engines/jeff.py`, and the
`NOTICE` entry -- and run the `weights` suite, because a `model.py` bump can change
numerics.
