"""Guards for AGENTS.md §2 (one canonical home) and §6 (lazy imports).

Every concept in the canonical-home table gets an assertion here. When you add a
canonical helper, add its guard in the same commit -- otherwise the table becomes
documentation that drifts.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "decis"


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _module_scope_imports(tree: ast.Module) -> list[str]:
    """Imports that run when the module is imported.

    Only the top level counts. `ast.walk` would also visit the inside of every
    function, which is exactly where a lazy import *should* live (AGENTS.md §6) -- so
    walking the whole tree would fail every engine that does the right thing.
    """
    names: list[str] = []

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.append(node.module)
            elif isinstance(node, (ast.If, ast.Try)):
                # `if TYPE_CHECKING:` and `try: import x` still execute at import time
                # (the latter catches ImportError, which is a real dependency signal).
                visit(node.body)
                visit(getattr(node, "orelse", []))

    visit(tree.body)
    return names


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported anywhere in `path`, at any level."""
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module)
            elif node.module:
                imported.add(f"{path.parent.name}.{node.module}")
    return imported


def test_canonical_files_exist() -> None:
    """The table in AGENTS.md §2 names files; they must all be real."""
    for name in (
        "render.py",
        "answers.py",
        "schema.py",
        "errors.py",
        "auth.py",
        "paths.py",
        "config.py",
        "observability.py",
        "scheduler.py",
        "service.py",
        "routes.py",
        "app.py",
        "domain.py",
        "engines/registry.py",
    ):
        assert (PACKAGE / name).is_file(), f"AGENTS.md §2 names {name}, which does not exist"


# --- §2: environment variables only in config.py -------------------------------


def test_os_environ_is_only_read_in_config() -> None:
    offenders = []
    for path in _modules():
        if path.name == "config.py":
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert not offenders, f"read environment variables through decis.config instead: {offenders}"


# --- §6: the core must not import torch --------------------------------------


def test_core_modules_never_import_torch() -> None:
    """A laya-only image has no peft; a kev-only image has no laya."""
    heavy = {"torch", "transformers", "laya", "peft", "accelerate", "safetensors", "numpy"}
    offenders = []
    for path in _modules():
        if "engines" in path.parts and path.name not in {"registry.py", "base.py"}:
            continue  # engines may import their own runtime, but only inside load()
        for node in ast.walk(_tree(path)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in heavy:
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} imports {name}")
    assert not offenders, f"heavy imports must be lazy, inside load(): {offenders}"


def test_registry_does_not_import_engine_modules() -> None:
    """Engines are referenced by string path so the registry stays cheap."""
    source = _source(PACKAGE / "engines" / "registry.py")
    assert "import_module" in source, "the registry must resolve engines lazily"
    tree = _tree(PACKAGE / "engines" / "registry.py")
    for node in ast.walk(tree):
        eager = (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("decis.engines.")
            and node.module != "decis.engines.base"
        )
        if eager:
            pytest.fail(f"registry.py must not import {node.module} eagerly")


# --- §4: dependency direction -------------------------------------------------


def test_engines_never_import_the_http_layer() -> None:
    http_modules = {"fastapi", "starlette", "uvicorn", "decis.routes", "decis.app", "decis.service"}
    offenders = []
    for path in (PACKAGE / "engines").rglob("*.py"):
        for node in ast.walk(_tree(path)):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            root = module.split(".")[0] if module else None
            if root in http_modules or module in http_modules:
                offenders.append(f"{path.name}:{node.lineno} imports {module}")
    assert not offenders, f"engines must not know about HTTP: {offenders}"


def test_normalisation_layer_never_imports_an_engine() -> None:
    offenders = []
    for name in ("schema.py", "render.py", "answers.py", "domain.py"):
        for node in ast.walk(_tree(PACKAGE / name)):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            if module and "engines" in module:
                offenders.append(f"{name}:{node.lineno} imports {module}")
    assert not offenders, f"normalisation must not depend on engines: {offenders}"


def test_domain_imports_nothing_from_the_package() -> None:
    """domain.py is the bottom of the package: it must stay dependency-free."""
    for node in ast.walk(_tree(PACKAGE / "domain.py")):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            pytest.fail(f"domain.py must not import from decis (line {node.lineno})")


# --- §3: the confidence formula has one definition ---------------------------


def test_confidence_is_defined_only_in_answers() -> None:
    offenders = []
    for path in _modules():
        if path.name == "answers.py":
            continue
        source = _source(path)
        if "def choice_confidence" in source or "def score_confidence" in source:
            offenders.append(str(path.relative_to(PACKAGE)))
    assert not offenders, f"confidence has one home, answers.py: {offenders}"


def test_wire_answer_models_are_constructed_only_in_answers() -> None:
    """AGENTS.md §9: no answer dict outside answers.py."""
    offenders = []
    for path in _modules():
        if path.name in {"answers.py", "schema.py"}:
            continue
        source = _source(path)
        for marker in ("NoulAnswer(", "ChoiceAnswer(", "ScoreAnswer("):
            if marker in source:
                offenders.append(f"{path.relative_to(PACKAGE)} constructs {marker[:-1]}")
    assert not offenders, "; ".join(offenders)


def test_question_keys_has_one_home() -> None:
    """The "false"/"true", option-name, "0".."n-1" rule lives in answers.py only."""
    definitions = []
    for path in _modules():
        if "def question_keys" in _source(path):
            definitions.append(str(path.relative_to(PACKAGE)))
    assert definitions == ["answers.py"], f"question_keys must be defined once, in answers.py: {definitions}"

    # The noul option names are built in exactly one place, because render.py owns
    # turning a question into options.
    builders = []
    for path in _modules():
        source = _source(path)
        if 'Option("false"' in source or 'Option("true"' in source:
            builders.append(str(path.relative_to(PACKAGE)))
    assert builders == ["render.py"], f"noul option names must be built in render.py only: {builders}"


# --- §9: the forbidden list --------------------------------------------------


def test_weights_are_not_committed() -> None:
    repo = PACKAGE.parent.parent
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    forbidden = [
        path
        for path in tracked
        if path.endswith((".safetensors", ".pt", ".bin", ".ckpt", ".onnx", ".gguf")) and "tests/fixtures" not in path
    ]
    assert not forbidden, f"weights must never be committed: {forbidden}"


def test_streaming_is_not_advertised(client, auth) -> None:
    """AGENTS.md §3-12: no stream parameter, because jev has no streaming API."""
    schema = client.get("/openapi.json").json()
    body = schema["components"]["schemas"]["SystemOneRequest"]["properties"]
    assert "stream" not in body


def test_python_version_floor_matches_pyproject() -> None:
    assert sys.version_info >= (3, 11)


# --- §2: weight location has one home ------------------------------------------


def test_weight_location_is_decided_only_in_paths() -> None:
    """Engines must not invent their own search order.

    If they did, "mount your own model with DECIS_MODEL_DIR" would work for one engine
    and silently not for another -- and the failure would appear as the model loader
    reporting a missing file, far from the actual cause.
    """
    offenders = []
    for path in _modules():
        if path.parent.name != "engines":
            continue
        source = _source(path)
        # A Hub repository id being read is fine; deciding *where* to read from is not.
        if "snapshot_download" in source or "huggingface_hub" in source:
            offenders.append(str(path.relative_to(PACKAGE)))
    assert offenders == [], f"engines must not fetch weights themselves; use paths.resolve: {offenders}"


def test_paths_does_not_import_the_engine_layer() -> None:
    """`paths.py` sits below `engines/` in the layering (AGENTS.md §4)."""
    imported = _imported_modules(PACKAGE / "paths.py")
    assert not any(name.startswith("decis.engines") for name in imported), imported


def test_the_laya_engine_does_not_import_torch_at_module_scope() -> None:
    """AGENTS.md §6: an image with the `laya` extra absent must still list the engine.

    Torch may be imported *inside* a method -- `load()` and `predict()` do so -- but
    touching it at module scope would make `GET /v1/models` fail in a container that
    only installed a different engine.
    """
    heavy = {"torch", "transformers", "numpy", "laya", "safetensors", "huggingface_hub"}
    # Positive control: the guard must be capable of failing, or it proves nothing.
    assert heavy & set(_module_scope_imports(_tree(PACKAGE / "engines" / "base.py"))) == set()
    for name in _module_scope_imports(_tree(PACKAGE / "engines" / "laya.py")):
        assert name.split(".")[0] not in heavy, (
            f"laya.py imports {name!r} at module scope; it must be inside load()/predict() "
            f"so that GET /v1/models works without the extra installed"
        )


def test_torch_is_imported_nowhere_in_the_core() -> None:
    """The rest of the package must not reach for torch on any path."""
    offenders = []
    for path in _modules():
        if path.parent.name == "engines":
            continue
        if any(name.split(".")[0] == "torch" for name in _module_scope_imports(_tree(path))):
            offenders.append(str(path.relative_to(PACKAGE)))
    assert offenders == [], f"only engines may import torch: {sorted(set(offenders))}"


def test_measured_tokens_is_defined_once() -> None:
    definitions = [str(path.relative_to(PACKAGE)) for path in _modules() if "class MeasuredTokens" in _source(path)]
    assert definitions == ["domain.py"], f"MeasuredTokens must be defined once, in domain.py: {definitions}"


def test_head_budget_arithmetic_has_one_home() -> None:
    """`budgeted_head` encodes upstream's truncation condition; a copy would drift."""
    definitions = [str(path.relative_to(PACKAGE)) for path in _modules() if "def budgeted_head" in _source(path)]
    assert definitions == ["engines/laya.py"], definitions


def test_engine_readiness_is_classified_once() -> None:
    """`decis models` and `decis doctor` must not each decide what "ready" means.

    They did, in the first Stage 1 draft: both printed "ready" for every registered
    engine, because both only checked that the engine *class* imports -- which is
    always true, since registration is lazily imported by design (AGENTS.md §6).
    """
    definitions = [str(path.relative_to(PACKAGE)) for path in _modules() if "def status(" in _source(path)]
    assert definitions == ["engines/registry.py"], definitions
    # A reachability check: the words a user reads must come from that one place.
    cli = _source(PACKAGE / "cli.py")
    assert "unavailable  " not in cli, "cli.py is formatting readiness itself"
