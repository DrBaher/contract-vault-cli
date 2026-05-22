"""Tests for `risk` — renewal exposure (missed / imminent notice deadlines)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

import contract_vault_cli as cv

AS_OF = dt.date(2026, 6, 1)


def _field(v: Any) -> dict:
    return {"value": None, "confidence": 0.0, "source": "none"} if v is None else {"value": v, "confidence": 0.9, "source": "deterministic"}


def _rec(rid: str, exp: Optional[str], notice: Optional[int], auto: Optional[bool]) -> Tuple[str, Path, dict]:
    payload = {
        "document": {"title": rid, "format": "pdf", "sha256": "a" * 64, "source_path": None},
        "parties": [{"name": "Co", "role": "x", "confidence": 0.9, "source": "deterministic"}],
        "dates": {"effective": _field("2024-01-01"), "expiration": _field(exp)},
        "term": {"length": _field("1y"), "auto_renew": _field(auto), "notice_period_days": _field(notice)},
        "governing_law": _field("NY"), "clauses": [], "defined_terms": [], "value": _field("$1"),
        "obligations": [], "_meta": {"extractor_version": "x", "tiers_used": ["deterministic"], "llm_used": False},
    }
    rec = cv.build_record(payload, deal_identifier=rid, title=rid, source_rel_path="x", source_vaulted=False)
    return (rid, Path("."), rec)


def _records() -> List[Tuple[str, Path, dict]]:
    return [
        _rec("a/missed-auto", "2026-06-15", 30, True),    # deadline 2026-05-16 past, active, auto -> CRITICAL
        _rec("b/missed-noauto", "2026-06-20", 30, False),  # deadline past, not auto -> warning
        _rec("c/soon-notice", "2026-07-10", 30, True),     # deadline 2026-06-10 within 30d -> soon
        _rec("d/expiring", "2026-06-20", None, True),      # no notice; expiration within 30d -> soon
        _rec("e/expired", "2026-01-01", 30, True),         # already expired -> excluded
    ]


def test_risk_items_classification() -> None:
    items = cv.risk_items(_records(), AS_OF, 30)
    by_deal = {it["deal"]: it for it in items}
    assert "e/expired" not in by_deal                              # expired excluded
    assert by_deal["a/missed-auto"]["severity"] == "critical"
    assert by_deal["a/missed-auto"]["days_until"] < 0              # deadline in the past
    assert by_deal["b/missed-noauto"]["severity"] == "warning"
    assert by_deal["c/soon-notice"]["severity"] == "soon" and by_deal["c/soon-notice"]["kind"] == "renewal_notice"
    assert by_deal["d/expiring"]["kind"] == "expiration"
    assert [it["severity"] for it in items][0] == "critical"        # sorted by severity


def _vault_with_records(vault: Path) -> None:
    for rid, _p, rec in _records():
        d = vault / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / cv.RECORD_FILENAME).write_text(cv._dump_json(rec), encoding="utf-8")


def test_risk_json(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _vault_with_records(empty_vault)
    rc = cv.main(["risk", "--as-of", "2026-06-01", "--within", "30d", "--json", "--vault", str(empty_vault)])
    assert rc == cv.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["critical"] == 1
    assert data["count"] == 4
    assert data["items"][0]["severity"] == "critical"


def test_risk_strict_exit(empty_vault: Path) -> None:
    _vault_with_records(empty_vault)
    assert cv.main(["risk", "--as-of", "2026-06-01", "--vault", str(empty_vault)]) == cv.EXIT_OK
    assert cv.main(["risk", "--as-of", "2026-06-01", "--strict", "--vault", str(empty_vault)]) == cv.EXIT_FAIL


def test_risk_alias_and_empty(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # at-risk alias works; with no at-risk items it reports clean
    rc = cv.main(["at-risk", "--as-of", "2026-06-01", "--vault", str(empty_vault)])
    assert rc == cv.EXIT_OK
    assert "no at-risk" in capsys.readouterr().out.lower()
