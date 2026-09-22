# Vendored from kev

Everything in this directory except this file and `__init__.py` is a **byte-identical
copy** of files from [jaredpalmer/kev](https://github.com/jaredpalmer/kev). They are
not modified in any way, and nothing here may be edited: `tests/test_kev_vendor.py`
recomputes the sha256 of each file and fails if it changes. The point of the hashes is
that "did we alter the model we claim to run?" is a question with a one-command answer.

| File | Upstream path | Bytes | sha256 |
|---|---|---|---|
| `model.py` | `kev/model.py` | 19947 | `c743c26e20fe8550e28b0697cb123173ee24c83e1b387830bff9f0b178900d55` |
| `checkpoint.py` | `kev/checkpoint.py` | 9177 | `9cd2ef04632ee84340c07cf95e8c80f999f153f4d896c46a4b74dbd1fb0cb193` |
| `LICENSE` | `LICENSE` | 11343 | Apache-2.0, unmodified |

- **Source**: https://github.com/jaredpalmer/kev
- **Commit**: `90990a5fac2995b9faa3190f7d437e84f2067768` (2026-09-21)
- **Licence**: Apache-2.0 (see `LICENSE` here and the `NOTICE` entry at the repo root)

## Why vendor instead of depending on kev

kev is not published to PyPI. Its `pyproject.toml` lists `datasets` and
`scikit-learn` as *runtime* dependencies for its research tooling, and pulling those
into an inference server is not a trade Decis wants to make. kev's own
`scripts/publish_space.sh` vendors `model.py`/`api.py`/`checkpoint.py` into its
Hugging Face Space, so this file set is already organised by upstream to be embedded.

`checkpoint.py`'s only kev-internal import is `from .model import ...`, so the two
files stand alone as a package.

## What was left out, and why

| Upstream file | Why not vendored |
|---|---|
| `kev/api.py` | The wire format is Decis's, and `answers.py` is its canonical home. `api.py` also defines `to_answers`, `choice_confidence` and `score_confidence`; importing them would put a second implementation of the confidence formulas in the tree. The engine returns `ProbDist`, never a contract answer (`AGENTS.md §3.11`, §2). |
| `kev/serve.py` | Decis has its own HTTP layer. Upstream's hardcodes `host="127.0.0.1"`, which would break container use (`AGENTS.md §9`). |
| `kev/{data,train,evaluate,suite,benchmark,...}.py` | Research tooling; drags in `datasets`/`scikit-learn`. |

## Two `os.environ` reads in the vendored code, and why they are inert here

`model.py:186` reads `KEV_SHAPE_BUCKET` at class-definition time and
`checkpoint.py:96` offers `LoadOptions.from_env()`. Decis's convention is that
`config.py` is the only module that reads `os.environ` (`AGENTS.md §2`), so both are
worth naming explicitly:

- `SHAPE_BUCKET` only pads sequences on MPS (per-shape kernel warm-up). On CPU and
  CUDA it is never consulted.
- Decis never calls `LoadOptions.from_env()`. The engine builds a `LoadOptions`
  explicitly so dtype and merge are decided by Decis's `DTYPE_DEFAULTS` table rather
  than by whatever happens to be in the environment.

The vendored tree is excluded from both the ruff run and the conventions scan;
`AGENTS.md §2`'s rule is about Decis's own modules, and editing a third-party copy to
satisfy a style rule would defeat the point of pinning it.

## Re-vendoring

Upstream reports serving-relevant fixes as a reason to republish its Space (see
kev's `AGENTS.md`), so treat this copy as stale-able rather than frozen forever:

```bash
cd /path/to/kev && git checkout <new-commit>
cp kev/model.py kev/checkpoint.py LICENSE /path/to/Decis/src/decis/engines/_kev_vendor/
sha256sum src/decis/engines/_kev_vendor/{model.py,checkpoint.py}   # update the table above
```

Then run the `weights` suite, because a `model.py` bump can change numerics.
