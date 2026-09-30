"""A pinned copy of the parts of jeff that Decis needs to serve its checkpoints.

`model.py`, `decoder.py` and `types.py` are vendored from
https://github.com/firelex/jeff at tag `v1.1`
(`f0397f3785d93f73a01411d785d2ed026f53181d`, MIT -- the *weights* are Apache-2.0, which is a
separate licence recorded on each `WeightSpec`). See `VENDOR.md` for the recorded sha256 of
every file, the single mechanical edit their relative imports required, and what was
deliberately left out. `tests/test_jeff_vendor.py` fails if any vendored byte changes.

Do not edit anything in this directory by hand. If upstream needs to change,
re-vendor at a new revision and update `VENDOR.md`, `NOTICE` and the engine's
`REVISION` string in the same commit.

What is *not* imported from upstream, on purpose:

- `jeff/server.py` -- the wire format and answer construction are Decis's job
  (`schema.py`, `answers.py`). Vendoring it would put a second implementation of
  `question_keys` and of the confidence formulas in the tree, and the engine is
  required to return `ProbDist` rather than a contract answer (`AGENTS.md §2`). It
  also hardcodes `host="127.0.0.1"`, which would break container use (`§9`).
- `jeff/data.py`, `jeff/train.py`, `jeff/evaluate.py`, `jeff/panel.py`, ... --
  research and training code. `jeff`'s own `pyproject.toml` keeps `datasets`,
  `matplotlib` and friends in a separately-installed `train` group for exactly this
  reason, and an inference server has no use for them.
- `jeff/mlx_backend.py` -- an Apple-silicon backend for the Qwen models only. Decis
  serves through torch on every device so that one `DECIS_DEVICE` story covers all
  engines (`engines/devices.py`).
"""

from .decoder import GenericDecoderDecisionModel
from .model import MAX_OPTIONS, DecisionModel, PreparedBatch, decision_messages, describe, options
from .types import Answer, Content, DecisionInput, JSONValue, Question

__all__ = [
    "MAX_OPTIONS",
    "Answer",
    "Content",
    "DecisionInput",
    "DecisionModel",
    "GenericDecoderDecisionModel",
    "JSONValue",
    "PreparedBatch",
    "Question",
    "decision_messages",
    "describe",
    "options",
]
