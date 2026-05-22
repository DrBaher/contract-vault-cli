"""Integration tests for the extract-cli seam — the entry point `ingest <doc>`.

Two layers:
- a real `extract` shim placed on PATH (POSIX) exercises the actual subprocess shell-out,
  argument passing, --llm forwarding, and the "extract missing" error;
- ingesting the *captured real* extract-cli 0.1.14 output (parties empty, expiration null,
  plus fields contract-vault doesn't consume) proves we tolerate the real, evolving tool.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import FIXTURES, extract_fixture

# Captured real output from extract-cli 0.1.14 (kept out of the standard 4-fixture corpus).
REAL = FIXTURES / "real-extract-nda.json"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="PATH shebang shim is POSIX-only")


def _install_fake_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload_text: str) -> Path:
    """Put a real, executable `extract` on PATH that logs its args and prints payload_text."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(payload_text, encoding="utf-8")
    log = tmp_path / "args.txt"
    shim = bindir / "extract"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(log)!r}).write_text(' '.join(sys.argv[1:]))\n"
        f"sys.stdout.write(pathlib.Path({str(payload_file)!r}).read_text())\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return log


@posix_only
def test_ingest_shells_out_to_extract_on_path(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "deal.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    log = _install_fake_extract(tmp_path, monkeypatch, REAL.read_text())
    assert cv.main(["ingest", str(doc), "--vault", str(empty_vault)]) == 0
    invoked = log.read_text()
    assert str(doc) in invoked and "--json" in invoked and "--llm" not in invoked
    assert list(empty_vault.rglob(cv.RECORD_FILENAME))   # a record was stored


@posix_only
def test_ingest_forwards_llm_to_extract(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "deal.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    log = _install_fake_extract(tmp_path, monkeypatch, REAL.read_text())
    assert cv.main(["ingest", str(doc), "--llm", "--vault", str(empty_vault)]) == 0
    assert "--llm" in log.read_text()


@posix_only
def test_ingest_errors_when_extract_missing(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    doc = tmp_path / "deal.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))   # no `extract` anywhere on PATH
    assert cv.main(["ingest", str(doc), "--vault", str(empty_vault)]) == cv.EXIT_FAIL
    assert "pip install extract-cli" in capsys.readouterr().err


def test_ingests_real_extract_output_and_flags_gaps(empty_vault: Path) -> None:
    # The real deterministic output: empty parties + null expiration. We still ingest it,
    # store what's there faithfully, and surface the gap via review (verify, not trust).
    assert cv.main(["ingest", str(REAL), "--vault", str(empty_vault)]) == 0
    rec = json.loads(next(empty_vault.rglob(cv.RECORD_FILENAME)).read_text())
    assert rec["parties"] == [] and rec["expiration_date"] is None
    assert rec["value"]["amount"] == 250000.0 and rec["governing_law"] == "State of Delaware"
    assert any(f["field"] == "expiration_date" for f in cv.review_flags(rec))
    # extract's extra top-level fields (jurisdiction/amounts/signatories) are tolerated, not stored
    assert "jurisdiction" not in rec and "amounts" not in rec


def test_unknown_extract_fields_are_tolerated(empty_vault: Path) -> None:
    payload = extract_fixture("acme-msa")
    payload["brand_new_field"] = {"value": "x", "confidence": 1.0, "source": "deterministic"}
    payload["document"]["sha256"] = "f" * 64
    deal, created = cv.store_record(empty_vault, payload, counterparty_override=None, name_override=None, local_source=None)
    assert created   # ingests fine despite a field contract-vault doesn't know


def test_real_extract_payload_passes_validation() -> None:
    # The lenient runtime check accepts the real, superset payload.
    payload = json.loads(REAL.read_text())
    assert cv.validate_extract_payload(payload) is payload


def test_real_fixture_conforms_to_input_and_output_schemas(extract_schema: dict, record_schema: dict) -> None:
    from conftest import assert_valid, schema_errors
    payload = json.loads(REAL.read_text())
    assert_valid(payload, extract_schema)   # real superset output satisfies the subset we require
    rec = cv.build_record(
        payload, deal_identifier="a/b", title=payload["document"]["title"],
        source_rel_path="x", source_vaulted=False,
    )
    assert not schema_errors(rec, record_schema)
