"""Tests for obligation recurrence + per-obligation reminders (0.3.0)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import assert_valid, schema_errors


def _payload(obtext: str, exp: str = "2026-12-31", eff: str = "2026-01-01") -> dict:
    f = lambda x: {"value": x, "confidence": 0.9, "source": "deterministic"}  # noqa: E731
    return {
        "document": {"title": "Svc", "format": "pdf", "sha256": "a" * 64, "source_path": None},
        "parties": [{"name": "Globex", "role": "Customer", "confidence": 0.9, "source": "deterministic"}],
        "dates": {"effective": f(eff), "expiration": f(exp)},
        "term": {"length": f("1y"), "auto_renew": f(False), "notice_period_days": f(None)},
        "governing_law": f("NY"), "clauses": [], "defined_terms": [], "value": f("$1"),
        "obligations": [{"text": obtext, "confidence": 0.8, "source": "deterministic"}],
        "_meta": {"extractor_version": "x", "tiers_used": ["deterministic"], "llm_used": False},
    }


def _ingest(vault: Path, payload: dict) -> str:
    deal, _ = cv.store_record(vault, payload, counterparty_override=None, name_override=None, local_source=None)
    return deal


def _read(vault: Path, deal: str) -> dict:
    return json.loads((vault / deal / cv.RECORD_FILENAME).read_text())


def _obl(rec: dict) -> dict:
    return next(o for o in rec["obligations"] if o["type"] == "obligation")


def test_detect_recurrence() -> None:
    assert cv.detect_recurrence("Quarterly business review.") == "quarterly"
    assert cv.detect_recurrence("Pay the monthly fee.") == "monthly"
    assert cv.detect_recurrence("Submit an annual report.") == "annual"
    assert cv.detect_recurrence("File a semi-annual statement.") == "semiannual"
    assert cv.detect_recurrence("Weekly status update.") == "weekly"
    assert cv.detect_recurrence("Deliver the goods by Friday.") is None


def test_add_months_clamps_to_month_end() -> None:
    assert cv._add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert cv._add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)  # leap year
    assert cv._add_months(dt.date(2026, 12, 15), 1) == dt.date(2027, 1, 15)
    assert cv._add_months(dt.date(2026, 3, 31), 3) == dt.date(2026, 6, 30)


def test_recurrence_occurrences_window_and_rollforward() -> None:
    occ = cv.recurrence_occurrences(dt.date(2026, 1, 15), "monthly", dt.date(2026, 1, 1), dt.date(2026, 4, 30))
    assert occ == [dt.date(2026, 1, 15), dt.date(2026, 2, 15), dt.date(2026, 3, 15), dt.date(2026, 4, 15)]
    occ2 = cv.recurrence_occurrences(dt.date(2025, 1, 10), "quarterly", dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert occ2 and occ2[0] >= dt.date(2026, 1, 1) and occ2[-1] <= dt.date(2026, 12, 31)


def test_build_record_auto_detects_recurrence(empty_vault: Path) -> None:
    deal = _ingest(empty_vault, _payload("Quarterly business review to be completed by 2026-06-05."))
    assert _obl(_read(empty_vault, deal)).get("recurrence") == "quarterly"


def test_due_expands_recurring_capped_at_expiration(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault, _payload("Quarterly business review to be completed by 2026-06-05.", exp="2026-12-31"))
    cv.main(["due", "--json", "--as-of", "2026-01-01", "--within", "365d", "--vault", str(empty_vault)])
    obs = [o for o in json.loads(capsys.readouterr().out)["obligations"] if o["type"] == "obligation"]
    assert [o["due"] for o in obs] == ["2026-06-05", "2026-09-05", "2026-12-05"]  # 2027-03 > expiration
    assert all(o["recurrence"] == "quarterly" for o in obs)


def test_obligation_set_and_clear_recurrence(empty_vault: Path) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-06-01."))  # no recurrence word
    oid = _obl(_read(empty_vault, deal))["id"]
    cv.main(["obligation", deal, oid, "--recurrence", "monthly", "--vault", str(empty_vault)])
    assert _obl(_read(empty_vault, deal))["recurrence"] == "monthly"
    cv.main(["obligation", deal, oid, "--recurrence", "none", "--vault", str(empty_vault)])
    assert "recurrence" not in _obl(_read(empty_vault, deal))


def test_obligation_set_and_clear_reminders(empty_vault: Path) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-06-01."))
    oid = _obl(_read(empty_vault, deal))["id"]
    cv.main(["obligation", deal, oid, "--reminders", "30,7,7", "--vault", str(empty_vault)])
    assert _obl(_read(empty_vault, deal))["reminders"] == [30, 7]   # deduped + sorted desc
    cv.main(["obligation", deal, oid, "--reminders", "", "--vault", str(empty_vault)])
    assert "reminders" not in _obl(_read(empty_vault, deal))


def test_obligation_reminders_default_and_override() -> None:
    assert cv.obligation_reminders({"type": "renewal_notice"}) == [14]
    assert cv.obligation_reminders({"type": "obligation"}) == [7]
    assert cv.obligation_reminders({"type": "obligation", "reminders": [7, 30]}) == [30, 7]


def test_ics_one_valarm_per_reminder(empty_vault: Path) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-06-01.", exp="2027-12-31"))
    oid = _obl(_read(empty_vault, deal))["id"]
    cv.main(["obligation", deal, oid, "--reminders", "30,7", "--vault", str(empty_vault)])
    rows = cv.upcoming_obligations(empty_vault, within_days=365, as_of=dt.date(2026, 1, 1))
    ics = cv.build_ics(rows)
    assert ics.count("BEGIN:VALARM") >= 2
    assert "TRIGGER:-P30D" in ics and "TRIGGER:-P7D" in ics


def test_reminders_validation(empty_vault: Path) -> None:
    deal = _ingest(empty_vault, _payload("Pay fee by 2026-06-01."))
    oid = _obl(_read(empty_vault, deal))["id"]
    assert cv.main(["obligation", deal, oid, "--reminders", "30,-7", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    assert cv.main(["obligation", deal, oid, "--reminders", "abc", "--vault", str(empty_vault)]) == cv.EXIT_USAGE


def test_recurring_done_drops_from_open_view(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest(empty_vault, _payload("Quarterly review by 2026-06-05.", exp="2027-12-31"))
    oid = _obl(_read(empty_vault, deal))["id"]
    cv.main(["obligation", deal, oid, "--status", "done", "--vault", str(empty_vault)])
    capsys.readouterr()
    cv.main(["due", "--json", "--as-of", "2026-01-01", "--within", "700d", "--vault", str(empty_vault)])
    obs = [o for o in json.loads(capsys.readouterr().out)["obligations"] if o["type"] == "obligation"]
    assert obs == []   # a done recurring obligation produces no occurrences in the open view


def test_schemas_conform_with_recurrence_and_reminders(
    empty_vault: Path, record_schema: dict, obligations_schema: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    deal = _ingest(empty_vault, _payload("Quarterly review by 2026-06-05.", exp="2027-12-31"))
    oid = _obl(_read(empty_vault, deal))["id"]
    cv.main(["obligation", deal, oid, "--reminders", "30,7", "--vault", str(empty_vault)])
    assert not schema_errors(_read(empty_vault, deal), record_schema)
    capsys.readouterr()
    cv.main(["due", "--json", "--as-of", "2026-01-01", "--within", "365d", "--vault", str(empty_vault)])
    assert_valid(json.loads(capsys.readouterr().out), obligations_schema)
