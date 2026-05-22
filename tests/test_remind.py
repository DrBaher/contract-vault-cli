"""Tests for the `remind` digest: obligations whose reminder window is open now (0.3.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import assert_valid


def _payload(obtext: str, exp: str = "2027-12-31") -> dict:
    f = lambda x: {"value": x, "confidence": 0.9, "source": "deterministic"}  # noqa: E731
    return {
        "document": {"title": "Svc", "format": "pdf", "sha256": "a" * 64, "source_path": None},
        "parties": [{"name": "Globex", "role": "Customer", "confidence": 0.9, "source": "deterministic"}],
        "dates": {"effective": f("2026-01-01"), "expiration": f(exp)},
        "term": {"length": f("1y"), "auto_renew": f(False), "notice_period_days": f(None)},
        "governing_law": f("NY"), "clauses": [], "defined_terms": [], "value": f("$1"),
        "obligations": [{"text": obtext, "confidence": 0.8, "source": "deterministic"}],
        "_meta": {"extractor_version": "x", "tiers_used": ["deterministic"], "llm_used": False},
    }


def _ingest(vault: Path, payload: dict) -> str:
    deal, _ = cv.store_record(vault, payload, counterparty_override=None, name_override=None, local_source=None)
    return deal


def _obl_id(vault: Path, deal: str) -> str:
    rec = json.loads((vault / deal / cv.RECORD_FILENAME).read_text())
    return next(o["id"] for o in rec["obligations"] if o["type"] == "obligation")


def _remind(vault: Path, as_of: str, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    cv.main(["remind", "--json", "--as-of", as_of, "--vault", str(vault), *extra])
    return json.loads(capsys.readouterr().out)


def test_remind_uses_default_lead(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))  # type 'obligation' -> default lead 7
    assert _remind(empty_vault, "2026-02-15", capsys)["count"] == 1   # 5 days out, within 7
    assert _remind(empty_vault, "2026-02-01", capsys)["count"] == 0   # 19 days out, beyond 7


def test_remind_honors_custom_reminders(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))
    cv.main(["obligation", deal, _obl_id(empty_vault, deal), "--reminders", "30", "--vault", str(empty_vault)])
    capsys.readouterr()
    assert _remind(empty_vault, "2026-02-01", capsys)["count"] == 1   # 19 days out, now within 30


def test_remind_strict_exit_code(empty_vault: Path) -> None:
    _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))
    assert cv.main(["remind", "--as-of", "2026-02-15", "--strict", "--vault", str(empty_vault)]) == cv.EXIT_FAIL
    assert cv.main(["remind", "--as-of", "2025-01-01", "--strict", "--vault", str(empty_vault)]) == cv.EXIT_OK


def test_remind_excludes_done(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))
    cv.main(["obligation", deal, _obl_id(empty_vault, deal), "--status", "done", "--vault", str(empty_vault)])
    capsys.readouterr()
    assert _remind(empty_vault, "2026-02-15", capsys)["count"] == 0


def test_remind_recurring_next_occurrence(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest(empty_vault, _payload("Quarterly review by 2026-03-01.", exp="2027-12-31"))
    cv.main(["obligation", deal, _obl_id(empty_vault, deal), "--reminders", "30", "--vault", str(empty_vault)])
    capsys.readouterr()
    data = _remind(empty_vault, "2026-02-10", capsys)  # next occurrence 2026-03-01 is 19 days out (<=30)
    assert any(o["type"] == "obligation" and o["due"] == "2026-03-01" for o in data["obligations"])


def test_remind_table_empty(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))
    assert cv.main(["remind", "--as-of", "2025-01-01", "--vault", str(empty_vault)]) == cv.EXIT_OK
    assert "no reminders due" in capsys.readouterr().out.lower()


def test_remind_json_conforms_to_obligations_schema(empty_vault: Path, obligations_schema: dict, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault, _payload("Pay fee by 2026-02-20."))
    data = _remind(empty_vault, "2026-02-15", capsys)
    assert_valid(data, obligations_schema)
    assert data["count"] >= 1
