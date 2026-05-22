"""Schema-conformance: inputs match extract's contract; outputs match ours."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import (
    SPEC_DIR,
    VENDOR,
    all_extract_fixtures,
    all_record_fixtures,
    assert_valid,
    load_json,
    schema_errors,
)


@pytest.mark.parametrize("fixture", all_extract_fixtures(), ids=lambda p: p.stem)
def test_extract_fixtures_match_input_contract(fixture: Path, extract_schema: dict) -> None:
    """Every vendored ingest input conforms to extract-cli's published schema."""
    assert_valid(load_json(fixture), extract_schema)


@pytest.mark.parametrize("fixture", all_record_fixtures(), ids=lambda p: p.stem)
def test_record_fixtures_match_record_schema(fixture: Path, record_schema: dict) -> None:
    assert_valid(load_json(fixture), record_schema)


@pytest.mark.parametrize("fixture", all_extract_fixtures(), ids=lambda p: p.stem)
def test_ingest_output_conforms_to_record_schema(fixture: Path, record_schema: dict) -> None:
    """Records produced by build_record conform to docs/spec/contract-record.schema.json."""
    payload = load_json(fixture)
    rec = cv.build_record(
        payload,
        deal_identifier="counterparty/deal",
        title=payload["document"]["title"],
        source_rel_path="(not vaulted)",
        source_vaulted=False,
    )
    assert_valid(rec, record_schema)


def test_due_output_conforms_to_obligations_schema(loaded_vault: Path, obligations_schema: dict, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["due", "--format", "json", "--vault", str(loaded_vault), "--as-of", "2025-01-01", "--within", "365d"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert_valid(data, obligations_schema)
    assert data["count"] > 0  # ensure we actually validated populated output


def test_empty_due_output_conforms(loaded_vault: Path, obligations_schema: dict, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["due", "--format", "json", "--vault", str(loaded_vault), "--as-of", "1990-01-01", "--within", "1d"])
    assert rc == 0
    assert_valid(json.loads(capsys.readouterr().out), obligations_schema)


@pytest.mark.parametrize("name", ["contract-record.schema.json", "obligations-output.schema.json"])
def test_spec_schemas_are_2020_12(name: str) -> None:
    schema = load_json(SPEC_DIR / name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "$id" in schema and name in schema["$id"]


def test_vendored_extract_schema_present_and_2020_12() -> None:
    schema = load_json(VENDOR / "extract-output.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "extract-cli" in schema["$id"]


def test_validator_rejects_bad_record(record_schema: dict) -> None:
    """Sanity check the embedded validator actually catches violations."""
    bad = {"schema_version": "1.0"}  # missing nearly everything
    assert schema_errors(bad, record_schema), "validator should report missing required props"

    good = cv.build_record(
        load_json(all_extract_fixtures()[0]),
        deal_identifier="a/b", title="t", source_rel_path="x", source_vaulted=False,
    )
    mutated = json.loads(json.dumps(good))
    mutated["parties"][0]["source"] = "telepathic"  # not in the source enum
    assert schema_errors(mutated, record_schema)
