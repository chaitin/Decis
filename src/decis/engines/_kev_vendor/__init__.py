"""A pinned, byte-identical copy of the parts of kev that Decis needs to embed.

`model.py` and `checkpoint.py` are vendored verbatim from
https://github.com/jaredpalmer/kev at commit `90990a5` (Apache-2.0). See `VENDOR.md`
for the file list, the recorded sha256 of each file, and what was deliberately left
out. `tests/test_kev_vendor.py` fails if any vendored byte changes.

Do not edit anything in this directory. If upstream needs to change, re-vendor at a
new commit and update `VENDOR.md` and `NOTICE` in the same commit -- an edit here
would silently make Decis's copy diverge from the model it claims to run.

What is *not* imported from upstream, on purpose:

- `kev/api.py` -- the wire format and answer construction are Decis's job
  (`schema.py`, `answers.py`). Vendoring it would be a second implementation of
  `question_keys` and the confidence formulas, and the engine is required to return
  `ProbDist` rather than a contract answer.
- `kev/serve.py` -- Decis has its own HTTP layer; upstream's hardcodes
  `host="127.0.0.1"` and Decis must be able to listen on `0.0.0.0`.
- `kev/{data,train,evaluate,suite,...}.py` -- research code. `kev`'s own
  `pyproject.toml` lists `datasets` and `scikit-learn` as *runtime* dependencies for
  these, which an inference server has no use for.
"""

from .checkpoint import Checkpoint, LoadOptions, Meta, read_meta
from .model import DecisionModel, encode, load_tokenizer, user_tokens

__all__ = [
    "Checkpoint",
    "DecisionModel",
    "LoadOptions",
    "Meta",
    "encode",
    "load_tokenizer",
    "read_meta",
    "user_tokens",
]
