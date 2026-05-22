"""Tests for hardened value/date parsing (0.1.3): locale, sign, multi/embedded dates."""
from __future__ import annotations

import datetime as dt

import contract_vault_cli as cv


def test_money_us_grouping_unchanged() -> None:
    assert cv.parse_money("$120,000") == (120000.0, "USD")
    assert cv.parse_money("$1,234.56") == (1234.56, "USD")
    assert cv.parse_money("USD 5000000") == (5000000.0, "USD")


def test_money_european_grouping() -> None:
    assert cv.parse_money("EUR 1.000.000,50") == (1000000.50, "EUR")
    assert cv.parse_money("€1.234,56") == (1234.56, "EUR")
    assert cv.parse_money("1.000.000") == (1000000.0, None)   # EU thousands, no decimal
    assert cv.parse_money("2,5") == (2.5, None)               # EU decimal, lone comma


def test_money_negative_and_accounting() -> None:
    assert cv.parse_money("-$50,000") == (-50000.0, "USD")
    assert cv.parse_money("($50,000)") == (-50000.0, "USD")
    assert cv.parse_money("$50,000") == (50000.0, "USD")      # positive unaffected
    assert cv.parse_money("$0") == (0.0, "USD")


def test_money_suffixes_unchanged() -> None:
    assert cv.parse_money("$1.5M") == (1_500_000.0, "USD")
    assert cv.parse_money("2k") == (2_000.0, None)
    assert cv.parse_money("3B") == (3_000_000_000.0, None)


def test_scan_dates_multiple_in_order() -> None:
    ds = cv.scan_dates("Pay 50% by 2026-06-01 and the remainder by 2026-09-01.")
    assert ds == [dt.date(2026, 6, 1), dt.date(2026, 9, 1)]


def test_scan_dates_ignores_embedded_numbers() -> None:
    assert cv.scan_dates("Reference invoice-2026-07-15-A for processing.") == []
    assert cv.scan_dates("Order 12345-2026-01-02 shipped") == []
    assert cv.scan_dates("Due by 2026-07-15.") == [dt.date(2026, 7, 15)]  # clean date still found


def test_scan_date_first_only_backcompat() -> None:
    assert cv.scan_date("by 2026-06-01 then 2026-09-01") == dt.date(2026, 6, 1)
    assert cv.scan_date("no dates here") is None


def _payload(obligation_text: str) -> dict:
    f = lambda v, s="deterministic", c=0.9: {"value": v, "confidence": c, "source": s}  # noqa: E731
    return {
        "document": {"title": "X", "format": "pdf", "sha256": "f" * 64, "source_path": None},
        "parties": [{"name": "Co", "role": "Customer", "confidence": 0.9, "source": "deterministic"}],
        "dates": {"effective": f("2025-01-01"), "expiration": f("2027-01-01")},
        "term": {"length": f("2y"), "auto_renew": f(False), "notice_period_days": f(None, "none", 0.0)},
        "governing_law": f("New York"), "clauses": [], "defined_terms": [],
        "value": f("$1,000"),
        "obligations": [{"text": obligation_text, "confidence": 0.8, "source": "deterministic"}],
        "_meta": {"extractor_version": "x", "tiers_used": ["deterministic"], "llm_used": False},
    }


def test_build_record_emits_one_obligation_per_date(record_schema: dict) -> None:
    from conftest import schema_errors
    rec = cv.build_record(
        _payload("Pay 50% by 2026-06-01 and the remainder by 2026-09-01."),
        deal_identifier="co/x", title="X", source_rel_path="x", source_vaulted=False,
    )
    dues = sorted(o["due"] for o in rec["obligations"] if o["type"] == "obligation")
    assert dues == ["2026-06-01", "2026-09-01"]
    assert not schema_errors(rec, record_schema)  # still schema-conformant


def test_build_record_dateless_obligation_kept() -> None:
    rec = cv.build_record(
        _payload("Maintain 99.9% uptime throughout the term."),
        deal_identifier="co/x", title="X", source_rel_path="x", source_vaulted=False,
    )
    obs = [o for o in rec["obligations"] if o["type"] == "obligation"]
    assert len(obs) == 1 and obs[0]["due"] is None
