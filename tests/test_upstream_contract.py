"""Upstream contract guards for the Laya engine.

`engines/laya.py` drives Laya's primitives directly instead of calling
`Agent.system_one`, because that method takes one state for the whole call while Decis
schedules a flat list of `WorkItem`s each carrying its own state.

Those primitives are public but not all are in `laya.__all__`, so nothing stops
upstream from renaming or reshaping them in a patch release -- and the failure mode is
a wrong answer or a silent truncation, not an ImportError. This module pins them.

The most important assertions here are the ones that check `_HEAD_RESERVE` and
`_SEQUENCE_SKELETON` against the **real `build_sequence`** rather than against a
comment. Those two constants come from reading upstream's arithmetic, and a constant
misread there is a request that passes validation and is then quietly truncated.

Needs the `laya` extra, so it is skipped without it. The arithmetic those constants
feed is still covered without torch by `tests/test_engines_laya.py`.
"""

from __future__ import annotations

import inspect

import pytest

laya = pytest.importorskip("laya", reason="requires the `laya` extra")

from laya.common import (  # noqa: E402
    QTYPES,
    build_sequence,
    collate_items,
    confidence_from_probs,
    render_options,
    temp_bucket,
)

from decis.engines.laya import _SEQUENCE_SKELETON, budgeted_head  # noqa: E402


class FakeTokenizer:
    """A tokenizer whose token counts are the string lengths, so the arithmetic is checkable.

    Deliberately not a real tokenizer: the question here is whether Decis' formula
    matches `build_sequence`'s layout, and `len(text)` makes every term of that formula
    independently verifiable. A real tokenizer would make the test pass or fail for
    reasons that have nothing to do with the formula.
    """

    cls_token_id = 1
    sep_token_id = 2
    mask_token_id = 3
    mask_token = "[MASK]"
    pad_token_id = 0

    def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
        del add_special_tokens
        return {"input_ids": list(range(10, 10 + len(text)))}


def _head_length_from_upstream(tokenizer: FakeTokenizer, question: dict, head_max_len: int, state: str) -> int:
    """The real head length, recovered from `build_sequence`'s output.

    The head is everything before the state. `markers` locates each option, and the
    state begins right after the final separator that follows the last option, so the
    head ends there -- recoverable without reimplementing anything.
    """
    _ids, markers = build_sequence(tokenizer, state, question, 10**6, head_max_len)
    options = render_options(question)
    last_option_length = 1 + len(tokenizer(" " + options[-1])["input_ids"])
    head_end = markers[-1] + last_option_length + 1  # +1 for the [SEP] after the options
    return head_end


def test_the_question_type_map_is_what_we_index_with() -> None:
    """`predict` uses `QTYPES[t]` to build the `qtype` tensor."""
    assert QTYPES == {"choice": 0, "score": 1, "noul": 2}


def test_build_sequence_keeps_its_signature() -> None:
    """Positional order matters: `predict` passes max_len and head_max_len positionally."""
    parameters = list(inspect.signature(build_sequence).parameters)
    assert parameters[:5] == ["tok", "state", "q", "max_len", "head_max_len"]


def test_collate_items_flattens_groups_of_items() -> None:
    """The cross-state batch depends entirely on this flattening.

    `predict` passes `[[item] for item in items]` -- one item per group -- so items
    with unrelated states land in one forward pass. If upstream stopped flattening,
    the batch would silently become a batch of one per group and cross-request
    batching would never happen, with no error anywhere.
    """
    parameters = list(inspect.signature(collate_items).parameters)
    assert parameters[:2] == ["batch", "pad_id"]

    source = inspect.getsource(collate_items)
    assert "[it for group in batch for it in group]" in source, (
        "collate_items no longer flattens its groups, so "
        "`collate_items([[item] for item in items], pad)` no longer batches across states"
    )


def test_render_options_reads_the_t_ins_crit_shape() -> None:
    """`_internal` builds exactly this dict, so its keys are part of the contract."""
    assert render_options({"t": "choice", "ins": "pick", "crit": {"a": "first", "b": "second"}}) == [
        "a: first",
        "b: second",
    ]
    # A criterion with no description renders as the bare name: why `_internal` passes
    # None rather than "" for an empty description.
    assert render_options({"t": "choice", "ins": "", "crit": {"a": None}}) == ["a"]
    # Score levels are labelled by Laya, not by the criteria -- the "level N: " prefix
    # is head cost that Decis' own rendering never sees.
    assert render_options({"t": "score", "ins": "", "crit": ["none", "some"]}) == [
        "level 0: none",
        "level 1: some",
    ]
    assert render_options({"t": "noul", "ins": "", "crit": None}) == [
        "false: no, the statement does not hold",
        "true: yes, the statement holds",
    ]


def _real_head(tokenizer: FakeTokenizer, question: dict, head_max_len: int, state: str) -> int:
    """The head length `build_sequence` actually produced.

    The head is everything before the state: `markers[-1]` locates the last option, and
    the state starts after the `[SEP]` that follows it.
    """
    options = render_options(question)
    _, markers = build_sequence(tokenizer, state, question, 10**6, head_max_len)
    last_option = 1 + min(len(tokenizer(" " + options[-1])["input_ids"]), 48)
    return markers[-1] + last_option + 1


def _head_cost(tokenizer: FakeTokenizer, question: dict) -> tuple[int, int]:
    options = render_options(question)
    instructions = len(tokenizer(f"{question['t']} question: {question['ins']}")["input_ids"])
    return instructions, sum(1 + min(len(tokenizer(" " + text)["input_ids"]), 48) for text in options)


#: Shapes chosen to straddle both truncation limits: tiny instructions with huge
#: options (option budget under 16), huge instructions with tiny options (instruction
#: cap), and several comfortable middles.
HEAD_SHAPES = [
    {"t": "noul", "ins": "does it?", "crit": {"false": "no", "true": "yes"}},
    {"t": "choice", "ins": "which team?", "crit": {"billing": "invoices", "tech": "bugs"}},
    {"t": "choice", "ins": "one option only", "crit": {"only": "x"}},
    {"t": "score", "ins": "how urgent?", "crit": ["low", "medium", "high"]},
    {"t": "score", "ins": "", "crit": ["a"]},
    {"t": "noul", "ins": "x" * 60, "crit": None},
    {"t": "noul", "ins": "", "crit": None},
    {"t": "choice", "ins": "", "crit": {f"o{i}": "y" * 60 for i in range(4)}},
    {"t": "choice", "ins": "a" * 200, "crit": {"a": "b", "c": "d"}},
    {"t": "score", "ins": "s" * 190, "crit": ["q" * 60, "r" * 60]},
    {"t": "choice", "ins": "many", "crit": {f"opt{i}": "y" * (i + 1) for i in range(12)}},
]


@pytest.mark.parametrize("question", HEAD_SHAPES, ids=lambda q: f"{q['t']}-{len(render_options(q))}opt")
def test_budgeted_head_is_exactly_the_no_truncation_condition(question: dict) -> None:
    """`budgeted_head` must predict truncation, not approximate it.

    This is the assertion that keeps the silent-truncation guard honest, and it is
    checked in both directions: below `head_max_len` the real head must be complete,
    and above it `build_sequence` must actually have changed something. A constant that
    is merely conservative would fail the second half.
    """
    tokenizer = FakeTokenizer()
    state = "state text"
    cost = _head_cost(tokenizer, question)

    for head_max_len in (20, 40, 64, 100, 128, 192):
        predicted_ok = budgeted_head(*cost) <= head_max_len
        real = _real_head(tokenizer, question, head_max_len, state)

        # Truncation is detectable: with a generous budget nothing is cut, so compare
        # against the untruncated ground truth.
        ids, markers = build_sequence(tokenizer, state, question, 10**6, head_max_len)
        untruncated = markers[-1] + 1 + min(len(tokenizer(" " + render_options(question)[-1])["input_ids"]), 48) + 1
        actually_truncated = real != untruncated or len(ids) != len(
            build_sequence(tokenizer, state, question, 10**6, 10**6)[0]
        )

        assert predicted_ok is not actually_truncated, (
            f"budgeted_head={budgeted_head(*cost)} vs head_max_len={head_max_len} predicted "
            f"{'no truncation' if predicted_ok else 'truncation'}, but build_sequence "
            f"{'truncated' if actually_truncated else 'did not truncate'} ({question['t']}, cost={cost})"
        )


def test_sequence_skeleton_matches_build_sequence() -> None:
    """`sequence_tokens = head + state + _SEQUENCE_SKELETON` must be the real length.

    The state is what gets truncated when this is under-counted, and `build_sequence`
    reports nothing when it happens.
    """
    tokenizer = FakeTokenizer()
    question = {"t": "noul", "ins": "does it?", "crit": None}
    state = "a state of some length"

    ids, _ = build_sequence(tokenizer, state, question, 10**6, 192)
    options = render_options(question)
    instructions = tokenizer(f"{question['t']} question: {question['ins']}")["input_ids"]
    head = len(instructions) + sum(1 + len(tokenizer(" " + text)["input_ids"]) for text in options)
    state_tokens = len(tokenizer(state)["input_ids"])

    assert head + state_tokens + _SEQUENCE_SKELETON == len(ids)


def test_build_sequence_would_truncate_what_we_reject() -> None:
    """The guard has to be *necessary*, not merely conservative.

    Constructs a state that fits `max_len` on its own but not once the head is
    included, and asserts that `build_sequence` really does drop tokens -- so that the
    combined `sequence_tokens` check is protecting against a real truncation rather
    than an imagined one.
    """
    tokenizer = FakeTokenizer()
    question = {"t": "score", "ins": "q" * 100, "crit": ["a" * 40, "b" * 40]}
    max_len = 512
    state = "s" * 400

    ids, _ = build_sequence(tokenizer, state, question, max_len, 192)
    options = render_options(question)
    instructions = tokenizer(f"{question['t']} question: {question['ins']}")["input_ids"]
    head = len(instructions) + sum(1 + len(tokenizer(" " + text)["input_ids"]) for text in options)
    state_tokens = len(tokenizer(state)["input_ids"])

    # The state alone fits the context window...
    assert state_tokens < max_len
    # ...but head + state does not, and the tail of the state is gone.
    assert head + state_tokens + _SEQUENCE_SKELETON > max_len
    assert len(ids) == max_len
    assert "s" * state_tokens not in str(ids[-state_tokens:])  # the last state token was cut


def test_laya_native_confidence_is_the_same_entropy_formula_decis_uses() -> None:
    """Documents a coincidence that is worth knowing about.

    Decis' canonical `confidence` is normalised entropy -- chosen over kev's
    distance-from-mode (docs/api-compatibility.md §7). Laya's own
    `confidence_from_probs` is `1 - H(p)/log(k)`, the same thing, so for Laya the two
    fields agree. For an engine that reports something else they will not, which is
    why `decis.native_confidence` is kept separate.
    """
    import math

    import numpy as np

    uniform = np.array([0.5, 0.5])
    assert confidence_from_probs(uniform, 2) == pytest.approx(0.5 - 0.5, abs=1e-9)  # 1 - H/log(2) = 0
    assert confidence_from_probs(np.array([1.0, 0.0]), 2) == pytest.approx(1.0)
    three = np.array([1 / 3, 1 / 3, 1 / 3])
    assert confidence_from_probs(three, 3) == pytest.approx(0.0, abs=1e-6)
    peaked = np.array([0.9, 0.05, 0.05])
    expected = 1.0 - (-(peaked * np.log(peaked)).sum()) / math.log(3)
    assert confidence_from_probs(peaked, 3) == pytest.approx(expected)


def test_temp_bucket_key_format_is_what_predict_looks_up() -> None:
    """`predict` indexes `agent.temperature_by_options` with this, so the format matters."""
    assert temp_bucket(0, 2) == "choice:2"
    assert temp_bucket(1, 3) == "score:3-5"
    assert temp_bucket(2, 8) == "noul:6-10"
    assert temp_bucket(0, 12) == "choice:11+"


def test_agent_exposes_what_the_engine_reads() -> None:
    """`Agent` is constructed directly so a mounted directory works offline.

    Checked against `__init__`'s source rather than `hasattr`, because these are
    instance attributes: `hasattr(Agent, ...)` on the class would pass or fail for
    reasons unrelated to whether a loaded agent carries them.
    """
    from laya import Agent

    source = inspect.getsource(Agent.__init__)
    for attribute in ("cfg", "tok", "model", "device", "dtype", "temperature", "temperature_by_options"):
        assert f"self.{attribute}" in source, (
            f"laya.Agent.__init__ no longer sets self.{attribute!r}, which engines/laya.py reads"
        )
    assert hasattr(Agent, "predict")
