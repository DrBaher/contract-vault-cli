"""Tests for `verify` integrity checking (source sha256 + git state)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import extract_fixture


def _verify_json(vault: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    rc = cv.main(["verify", "--json", "--vault", str(vault)])
    return {"rc": rc, **json.loads(capsys.readouterr().out)}


def test_verify_clean_vault(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["verify", "--vault", str(loaded_vault)])
    assert rc == cv.EXIT_OK
    assert "OK" in capsys.readouterr().out


def test_verify_corpus_clean(corpus_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _verify_json(corpus_vault, capsys)
    assert result["rc"] == cv.EXIT_OK
    assert result["ok"] is True
    assert result["findings"] == []


def _ingest_vaulted(vault: Path, tmp_path: Path) -> str:
    doc = tmp_path / "signed.pdf"
    doc.write_bytes(b"signed contract bytes v1")
    payload = extract_fixture("acme-msa")
    payload["document"]["source_path"] = str(doc)
    deal, _ = cv.store_record(vault, payload, counterparty_override=None, name_override=None, local_source=doc)
    return deal


def test_verify_detects_sha_mismatch(empty_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest_vaulted(empty_vault, tmp_path)
    rec = json.loads((empty_vault / deal / cv.RECORD_FILENAME).read_text())
    vaulted = empty_vault / deal / rec["source"]["path"]
    vaulted.write_bytes(b"tampered bytes!")
    result = _verify_json(empty_vault, capsys)
    assert result["rc"] == cv.EXIT_FAIL
    assert result["ok"] is False
    assert any("sha256 mismatch" in f for f in result["findings"])


def test_verify_detects_missing_source(empty_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest_vaulted(empty_vault, tmp_path)
    rec = json.loads((empty_vault / deal / cv.RECORD_FILENAME).read_text())
    (empty_vault / deal / rec["source"]["path"]).unlink()
    result = _verify_json(empty_vault, capsys)
    assert result["rc"] == cv.EXIT_FAIL
    assert any("missing" in f for f in result["findings"])


def test_verify_flags_dirty_tree(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (loaded_vault / "stray-uncommitted.txt").write_text("oops")
    result = _verify_json(loaded_vault, capsys)
    assert result["rc"] == cv.EXIT_FAIL
    assert any("not clean" in f for f in result["findings"])
