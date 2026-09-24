#!/usr/bin/env python3
"""Export the wire format as JSON Schema and OpenAPI, from the code that defines it.

`src/decis/schema.py` is the single home of the HTTP shape (`AGENTS.md` §2). The
files next to this script are **derived** from it: they exist so that a client
author can read the contract without cloning the repository or running the
server, and so that an editor can validate a payload before it is sent.

Two outputs per run:

* `*.schema.json` -- a self-contained JSON Schema (draft 2020-12) for the
  request, the response, `GET /v1/models`, and the non-validation error body.
  Each one names its root type and carries its nested models under `$defs`.
* `openapi.json` -- the server's own OpenAPI 3.1 document, exactly what
  `GET /openapi.json` returns. It is checked in because a schema you can only
  read by starting a server is a schema most people will never read.

`tests/test_api_schema.py` re-runs the export and fails when a checked-in file
differs, so the files cannot drift from the models. Editing one by hand is a
change that the next run deletes -- edit `schema.py` and run:

    python docs/schema/export.py --write

`python docs/schema/export.py` prints what would change without touching
anything; `--check` exits non-zero when something is stale (what CI runs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = Path(__file__).resolve().parent
SRC = ROOT / "src"

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: Which Pydantic model becomes which file. The root class name is exported in the
#: file's `title`, so a reader does not have to know our module layout.
EXPORTS: dict[str, str] = {
    "systemone-request.schema.json": "SystemOneRequest",
    "systemone-response.schema.json": "SystemOneResponse",
    "models.schema.json": "ModelMetadataList",
    "errors.schema.json": "ErrorResponse",
}

#: Request models are read as a client sends them (`validation`); response models as
#: the server writes them (`serialization`). The distinction only matters for fields
#: whose two representations differ, which is why it is written down rather than
#: assumed.
VALIDATION_ONLY = {"systemone-request.schema.json"}


def _import_package() -> None:
    """Make `src/` importable when this is run as a plain script."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _models() -> dict[str, Any]:
    _import_package()
    from decis import schema

    return {
        "SystemOneRequest": schema.SystemOneRequest,
        "SystemOneResponse": schema.SystemOneResponse,
        "ModelMetadataList": schema.ModelMetadataList,
        "ErrorResponse": schema.ErrorResponse,
    }


def model_schema(name: str, *, mode: str, filename: str | None = None) -> dict[str, Any]:
    """The JSON Schema for one wire model, with this file's `$id` and dialect set."""
    document = _models()[name].model_json_schema(mode=mode)
    document["$schema"] = JSON_SCHEMA_DIALECT
    document["$id"] = f"https://github.com/chaitin/Decis/docs/schema/{filename or name + '.schema.json'}"
    return document


def openapi_document() -> dict[str, Any]:
    """The OpenAPI document the server serves at `GET /openapi.json`.

    Built with `load_engine=False`: exporting a schema must not need weights, a
    tokenizer, or the engine extra to be installed -- the same rule that keeps
    `/v1/models` answerable in an engine-free image (`AGENTS.md` §6).
    """
    _import_package()
    from decis.app import create_app
    from decis.config import Settings

    settings = Settings(
        api_keys=("schema-export",),
        host="127.0.0.1",
        default_engine="laya-multilingual",
        env_file="",
    )
    return create_app(settings, load_engine=False).openapi()


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def rendered(directory: Path = SCHEMA_DIR) -> dict[Path, str]:
    """Every generated file and its exact contents. No I/O beyond imports."""
    out: dict[Path, str] = {}
    for filename, model in EXPORTS.items():
        mode = "validation" if filename in VALIDATION_ONLY else "serialization"
        out[directory / filename] = _dump(model_schema(model, mode=mode, filename=filename))
    out[directory / "openapi.json"] = _dump(openapi_document())
    return out


def stale(directory: Path = SCHEMA_DIR) -> list[Path]:
    """Which checked-in files differ from what the models would generate now."""
    return [
        path
        for path, content in rendered(directory).items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the Decis wire format as JSON Schema / OpenAPI.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the checked-in schema files")
    mode.add_argument("--check", action="store_true", help="exit 1 if the checked-in files are out of date")
    parser.add_argument("--directory", type=Path, default=SCHEMA_DIR, help="where the files live")
    args = parser.parse_args(argv)

    changed = stale(args.directory)
    if args.write:
        for path, content in rendered(args.directory).items():
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")
                print(f"updated {path.relative_to(ROOT)}")
        if not changed:
            print("nothing to update")
        return 0

    if args.check:
        if changed:
            print("these generated schemas are out of date:", file=sys.stderr)
            for path in changed:
                print(f"  - {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}", file=sys.stderr)
            print("run: python docs/schema/export.py --write", file=sys.stderr)
            return 1
        print(f"schemas are in sync ({len(rendered(args.directory))} file(s))")
        return 0

    for path in rendered(args.directory):
        status = "stale" if path in changed else "in sync"
        print(f"{status}: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
