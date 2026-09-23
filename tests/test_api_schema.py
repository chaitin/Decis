"""The checked-in JSON Schema is generated from `src/decis/schema.py`, not written beside it.

`AGENTS.md` §2 gives the wire format exactly one home: the Pydantic models. The files in
`docs/schema/` exist so a client author can read the contract without cloning the repo or
starting a server, and the only thing that keeps them honest is that regenerating them is
a no-op unless the models actually changed. This file is that check:

* the checked-in files match what `docs/schema/export.py` renders right now, and a
  hand-edited digit is detected (the negative control -- without it, "in sync" could
  just mean "not comparing anything");
* the contract's awkward corners survive the round trip through JSON Schema: `noul` is
  a scalar, `score` has a floor but no ceiling, `choice` declares no minimum, and the
  three question types stay discriminated by `type`;
* `openapi.json` is the same document the server serves, with both contract endpoints.

The generator is loaded by path, like `benchmarks/report.py`, because it lives outside
the package on purpose: it must keep working when `decis` cannot be imported.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPORT_PATH = ROOT / "docs" / "schema" / "export.py"
SCHEMA_DIR = ROOT / "docs" / "schema"


def _load_export():
    spec = importlib.util.spec_from_file_location("_schema_export", EXPORT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_schema_export"] = module
    spec.loader.exec_module(module)
    return module


export = _load_export()


def load(filename: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


# --- the files are generated, and a hand edit fails ----------------------------


def test_the_checked_in_schemas_are_in_sync() -> None:
    """This is what CI runs: a schema edited without editing `schema.py` fails here."""
    assert export.stale() == [], "run: python docs/schema/export.py --write"


def test_a_hand_edited_schema_is_detected(tmp_path: Path) -> None:
    """The negative control.

    Without it, `stale() == []` could mean "in sync" or "the comparison is broken".
    """
    for path, content in export.rendered(tmp_path).items():
        path.write_text(content, encoding="utf-8")
    assert export.stale(tmp_path) == []

    target = tmp_path / "systemone-request.schema.json"
    target.write_text(target.read_text(encoding="utf-8").replace('"minProperties": 1', '"minProperties": 2'), "utf-8")
    assert export.stale(tmp_path) == [target]


def test_the_export_is_deterministic(tmp_path: Path) -> None:
    """Two runs must produce identical bytes, or `--check` would fail on its own output."""
    assert export.rendered(tmp_path) == export.rendered(tmp_path)


def test_every_schema_declares_its_dialect_and_identity() -> None:
    for filename in export.EXPORTS:
        document = load(filename)
        assert document["$schema"] == export.JSON_SCHEMA_DIALECT, filename
        assert document["$id"].endswith(filename), filename
        assert document["title"], filename


# --- the contract's awkward corners survive the export -------------------------


def test_the_request_schema_discriminates_the_three_question_types() -> None:
    document = load("systemone-request.schema.json")
    assert set(document["required"]) == {"state", "model", "questions"}
    assert document["properties"]["questions"]["minProperties"] == 1

    question = document["properties"]["questions"]["additionalProperties"]
    assert question["discriminator"]["propertyName"] == "type"
    mapping = question["discriminator"]["mapping"]
    assert set(mapping) == {"noul", "choice", "score"}
    for name, ref in mapping.items():
        target = document["$defs"][ref.split("/")[-1]]
        assert target["properties"]["type"]["const"] == name


def test_score_needs_one_level_and_has_no_ceiling() -> None:
    """The OpenAPI snapshot declares `minItems: 1` and no `maxItems`; so must we."""
    criteria = load("systemone-request.schema.json")["$defs"]["ScoreQuestion"]["properties"]["criteria"]
    assert criteria["type"] == "array"
    assert criteria["minItems"] == 1
    assert "maxItems" not in criteria


def test_choice_declares_no_minimum_number_of_options() -> None:
    """`{}` is a legal request that no engine can answer -- a 422 from the capacity check.

    If this ever grows a `minProperties`, the server has moved a documented decision from
    the engine layer into the schema and `docs/api-compatibility.md` §3.2 is out of date.
    """
    criteria = load("systemone-request.schema.json")["$defs"]["ChoiceQuestion"]["properties"]["criteria"]
    assert criteria["type"] == "object"
    assert "minProperties" not in criteria


def test_noul_is_a_scalar_answer_with_no_confidence_and_no_probabilities() -> None:
    answer = load("systemone-response.schema.json")["$defs"]["NoulAnswer"]
    assert set(answer["properties"]) == {"type", "noul"}
    assert set(answer["required"]) == {"type", "noul"}


def test_score_and_choice_answers_carry_confidence_and_probabilities() -> None:
    defs = load("systemone-response.schema.json")["$defs"]
    for name in ("ScoreAnswer", "ChoiceAnswer"):
        properties = set(defs[name]["properties"])
        assert {"type", "confidence", "probabilities"} <= properties, name
    # `score` is an expected level, so it can land between levels: a number, not an int.
    assert defs["ScoreAnswer"]["properties"]["score"]["type"] == "number"
    # The legend's values are the caller's rubric text, which may be structured JSON.
    assert "legend" in defs["ScoreAnswer"]["properties"]


def test_usage_is_required_and_integral() -> None:
    usage = load("systemone-response.schema.json")["$defs"]["Usage"]
    assert set(usage["required"]) == {"input_tokens", "output_tokens"}
    assert all(usage["properties"][field]["type"] == "integer" for field in usage["required"])


def test_decis_extensions_stay_in_their_own_namespace() -> None:
    document = load("systemone-response.schema.json")
    assert set(document["properties"]) == {"model", "answers", "usage", "decis"}
    assert set(document["required"]) == {"model", "answers", "usage"}
    extension = document["$defs"]["DecisExtensions"]["properties"]
    # The observability fields a caller needs to tell what happened, plus the honesty
    # fields: the substituted model name and the engine's own confidence.
    for field in ("engine", "engine_version", "device", "dtype", "latency_ms", "batch_size"):
        assert field in extension, field
    assert "requested_model" in extension and "native_confidence" in extension


def test_the_error_schema_is_the_object_shape_not_the_validation_array() -> None:
    """401/403/429 use `{detail: {error_type, message}}`; 422 is an array (FastAPI's).

    The two shapes are not interchangeable and clients branch on them, so the standalone
    error schema documents the object one and `docs/api.md` carries the array.
    """
    detail = load("errors.schema.json")["$defs"]["ErrorDetail"]
    assert set(detail["required"]) == {"error_type", "message"}


# --- the OpenAPI document the server itself serves -----------------------------


def test_the_openapi_document_covers_the_contract_endpoints() -> None:
    document = load("openapi.json")
    assert document["openapi"].startswith("3.")
    assert set(document["paths"]) == {"/v1/systemone", "/v1/models"}
    assert "post" in document["paths"]["/v1/systemone"]
    assert "get" in document["paths"]["/v1/models"]


def test_the_openapi_document_embeds_the_same_models() -> None:
    components = load("openapi.json")["components"]["schemas"]
    for name in ("SystemOneRequest", "SystemOneResponse", "NoulAnswer", "ChoiceAnswer", "ScoreAnswer"):
        assert name in components, name


def test_the_exported_openapi_matches_the_running_app() -> None:
    """`GET /openapi.json` and the checked-in file must be one document, not two.

    The file is the offline copy of that route; if they diverge, one of them is lying
    about the API and the reader has no way to tell which.
    """
    assert load("openapi.json") == export.openapi_document()


@pytest.mark.parametrize("filename", sorted(export.EXPORTS))
def test_every_generated_file_is_valid_json(filename: str) -> None:
    assert isinstance(load(filename), dict)
