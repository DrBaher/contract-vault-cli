"""Shared test fixtures + a tiny stdlib JSON-Schema (2020-12 subset) validator.

The validator deliberately depends on nothing beyond the standard library, so the
whole suite (including schema-conformance tests) runs offline and without extract-cli
or any third-party package installed. It supports exactly the keywords used by the
schemas in docs/spec/ and the vendored extract-output schema: type (incl. arrays and
null), enum, const, required, properties, additionalProperties (bool|schema), items,
$ref (#/$defs/...), minimum, maximum, minItems, pattern, allOf/anyOf/oneOf.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, List

import pytest

# Make the single-module CLI importable without an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import contract_vault_cli as cv  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTRACT_FIXTURES = FIXTURES / "extract"
RECORD_FIXTURES = FIXTURES / "records"
VENDOR = Path(__file__).resolve().parent / "vendor"
SPEC_DIR = REPO_ROOT / "docs" / "spec"


# --------------------------------------------------------------------------- #
# Minimal JSON Schema validator
# --------------------------------------------------------------------------- #


def _is_type(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    raise AssertionError(f"unsupported type keyword: {t}")


def _resolve(ref: str, root: dict) -> dict:
    assert ref.startswith("#"), f"only local refs supported: {ref}"
    if ref == "#":
        return root
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def schema_errors(instance: Any, schema: dict, root: dict | None = None, path: str = "$") -> List[str]:
    root = root if root is not None else schema
    errors: List[str] = []

    if "$ref" in schema:
        return schema_errors(instance, _resolve(schema["$ref"], root), root, path)

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
    if "type" in schema:
        types = schema["type"]
        types = [types] if isinstance(types, str) else types
        if not any(_is_type(instance, t) for t in types):
            errors.append(f"{path}: expected type {types}, got {type(instance).__name__} ({instance!r})")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
    if isinstance(instance, str) and "pattern" in schema:
        if re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']!r}")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required property {req!r}")
        ap = schema.get("additionalProperties", True)
        for key, val in instance.items():
            if key in props:
                errors += schema_errors(val, props[key], root, f"{path}.{key}")
            elif ap is False:
                errors.append(f"{path}: additional property {key!r} not allowed")
            elif isinstance(ap, dict):
                errors += schema_errors(val, ap, root, f"{path}.{key}")

    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, el in enumerate(instance):
                errors += schema_errors(el, items, root, f"{path}[{i}]")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")

    for sub in schema.get("allOf", []):
        errors += schema_errors(instance, sub, root, path)
    if "anyOf" in schema and not any(not schema_errors(instance, s, root, path) for s in schema["anyOf"]):
        errors.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema:
        matched = sum(1 for s in schema["oneOf"] if not schema_errors(instance, s, root, path))
        if matched != 1:
            errors.append(f"{path}: matched {matched} of oneOf (need exactly 1)")

    return errors


def assert_valid(instance: Any, schema: dict) -> None:
    errs = schema_errors(instance, schema)
    assert not errs, "schema validation failed:\n  " + "\n  ".join(errs)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def record_schema() -> dict:
    return load_json(SPEC_DIR / "contract-record.schema.json")


@pytest.fixture()
def obligations_schema() -> dict:
    return load_json(SPEC_DIR / "obligations-output.schema.json")


@pytest.fixture()
def extract_schema() -> dict:
    return load_json(VENDOR / "extract-output.schema.json")


def extract_fixture(name: str) -> dict:
    return load_json(EXTRACT_FIXTURES / f"{name}.json")


def all_extract_fixtures() -> List[Path]:
    return sorted(EXTRACT_FIXTURES.glob("*.json"))


def all_record_fixtures() -> List[Path]:
    return sorted(RECORD_FIXTURES.glob("*.json"))


# --------------------------------------------------------------------------- #
# Environment / state hygiene
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, color-free, identity-stable environment for every test."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CONTRACT_VAULT_DIR", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    cv._NO_COLOR = False
    cv._QUIET = False


@pytest.fixture()
def empty_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    assert cv.main(["init", str(vault)]) == 0
    return vault


@pytest.fixture()
def loaded_vault(empty_vault: Path) -> Path:
    """A vault with all four extract fixtures ingested (no extract-cli involved)."""
    for fx in all_extract_fixtures():
        assert cv.main(["ingest", str(fx), "--vault", str(empty_vault)]) == 0
    return empty_vault


@pytest.fixture()
def corpus_vault(empty_vault: Path) -> Path:
    """A vault populated from the pre-built record fixtures (bypassing ingest)."""
    for fx in all_record_fixtures():
        rec = load_json(fx)
        dest = empty_vault / rec["id"]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / cv.RECORD_FILENAME).write_text(cv._dump_json(rec), encoding="utf-8")
    cv.git_commit(empty_vault, "test: load corpus")
    return empty_vault
