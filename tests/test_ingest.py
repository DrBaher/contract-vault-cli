"""Tests for `ingest`: mapping, idempotency, stdin, source vaulting, error paths."""
from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES, extract_fixture


def _read_record(vault: Path, deal: str) -> dict:
    return json.loads((vault / deal / cv.RECORD_FILENAME).read_text())


def test_ingest_json_file_maps_fields(empty_vault: Path) -> None:
    rc = cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    assert rc == 0
    rec = _read_record(empty_vault, "acme-corporation/master-services-agreement")
    assert rec["expiration_date"] == "2026-02-28"
    assert rec["effective_date"] == "2025-03-01"
    assert rec["value"]["amount"] == 120000.0
    assert rec["value"]["currency"] == "USD"
    assert rec["term"]["auto_renew"] is True
    assert rec["term"]["notice_period_days"] == 60
    assert rec["governing_law"] == "Delaware"
    assert rec["provenance"]["extractor_version"] == "extract-cli 1.4.0"
    assert rec["provenance"]["from_extract"] is True


def test_ingest_computes_renewal_window(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    rec = _read_record(empty_vault, "acme-corporation/master-services-agreement")
    rw = rec["term"]["renewal_window"]
    expiration = dt.date(2026, 2, 28)
    assert rw["deadline"] == (expiration - dt.timedelta(days=60)).isoformat()
    assert rw["expiration"] == "2026-02-28"


def test_ingest_derives_obligations(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    rec = _read_record(empty_vault, "acme-corporation/master-services-agreement")
    types = {o["type"] for o in rec["obligations"]}
    assert {"expiration", "renewal_notice", "obligation"} <= types
    payment = [o for o in rec["obligations"] if o["type"] == "obligation"][0]
    assert payment["due"] == "2025-04-15"  # date scanned from the obligation text


def test_ingest_field_meta_carries_source(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    rec = _read_record(empty_vault, "initech-inc/mutual-non-disclosure-agreement")
    assert rec["field_meta"]["governing_law"]["source"] == "deterministic"
    assert rec["value"]["amount"] is None  # no value in this NDA
    assert rec["term"]["auto_renew"] is False


def test_ingest_idempotent_on_sha256(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = str(EXTRACT_FIXTURES / "acme-msa.json")
    assert cv.main(["ingest", p, "--vault", str(empty_vault)]) == 0
    capsys.readouterr()
    assert cv.main(["ingest", p, "--vault", str(empty_vault)]) == 0
    assert "already ingested" in capsys.readouterr().out.lower()
    deals = list(empty_vault.rglob(cv.RECORD_FILENAME))
    assert len(deals) == 1


def test_ingest_overrides(empty_vault: Path) -> None:
    cv.main([
        "ingest", str(EXTRACT_FIXTURES / "acme-msa.json"),
        "--counterparty", "Custom Co", "--name", "Renamed Deal",
        "--vault", str(empty_vault),
    ])
    assert (empty_vault / "custom-co" / "renamed-deal" / cv.RECORD_FILENAME).is_file()


def test_ingest_from_stdin(empty_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(extract_fixture("soylent-saas"))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = cv.main(["ingest", "-", "--vault", str(empty_vault)])
    assert rc == 0
    # title was null -> name derived from source_path stem
    assert (empty_vault / "soylent-systems" / "soylent-saas" / cv.RECORD_FILENAME).is_file()


def test_ingest_empty_stdin_errors(empty_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    rc = cv.main(["ingest", "-", "--vault", str(empty_vault)])
    assert rc == cv.EXIT_USAGE


def test_ingest_rejects_non_extract_json(empty_vault: Path, tmp_path: Path) -> None:
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}')
    rc = cv.main(["ingest", str(junk), "--vault", str(empty_vault)])
    assert rc == cv.EXIT_FAIL


def test_validate_extract_payload_rejects_bad_input() -> None:
    with pytest.raises(cv.VaultError):
        cv.validate_extract_payload([1, 2, 3])
    with pytest.raises(cv.VaultError):
        cv.validate_extract_payload({"document": {}})  # missing top-level keys


def test_ingest_document_without_extract_on_path(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    doc = tmp_path / "contract.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(cv.shutil, "which", lambda name: None)
    rc = cv.main(["ingest", str(doc), "--vault", str(empty_vault)])
    assert rc == cv.EXIT_FAIL
    assert "pip install extract-cli" in capsys.readouterr().err


def test_ingest_vaults_source_and_records_sha(empty_vault: Path, tmp_path: Path) -> None:
    doc = tmp_path / "lease.pdf"
    doc.write_bytes(b"the actual signed bytes")
    payload = extract_fixture("umbrella-lease")
    payload["document"]["source_path"] = str(doc)
    deal, created = cv.store_record(
        empty_vault, payload, counterparty_override=None, name_override=None, local_source=doc,
    )
    assert created
    rec = _read_record(empty_vault, deal)
    assert rec["source"]["vaulted"] is True
    # sha256 is recomputed from the bytes actually stored, not trusted from extract
    assert rec["source"]["sha256"] == cv.sha256_file(doc)
    assert (empty_vault / deal / rec["source"]["path"]).read_bytes() == b"the actual signed bytes"


def test_ingest_long_title_does_not_crash(empty_vault: Path) -> None:
    # Regression: a ~1000-char title previously raised OSError(ENAMETOOLONG) on mkdir.
    payload = extract_fixture("acme-msa")
    payload["document"]["title"] = "Very " * 200 + "Long Agreement"
    payload["document"]["sha256"] = "e" * 64
    deal, created = cv.store_record(
        empty_vault, payload, counterparty_override=None, name_override=None, local_source=None,
    )
    assert created
    assert all(len(part.encode("utf-8")) <= 80 for part in deal.split("/"))  # path-safe components
    assert (empty_vault / deal / cv.RECORD_FILENAME).is_file()


def test_ingest_commits_to_git(empty_vault: Path) -> None:
    before = cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip()
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    after = cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) == int(before) + 1
