"""Tests for the `export` reporting command (csv / md / json)."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES, extract_fixture


def _csv(out: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(out)))


def test_export_csv_structure(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--vault", str(loaded_vault)]) == 0
    rows = _csv(capsys.readouterr().out)
    assert rows[0] == cv.EXPORT_COLUMNS
    assert len(rows) == 1 + 4  # header + 4 deals
    acme = next(r for r in rows[1:] if r[0] == "acme-corporation/master-services-agreement")
    cols = dict(zip(cv.EXPORT_COLUMNS, acme))
    assert cols["value_amount"] == "120000.0" and cols["value_currency"] == "USD"
    assert cols["auto_renew"] == "true" and cols["governing_law"] == "Delaware"


def test_export_csv_quotes_commas(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = extract_fixture("acme-msa")
    payload["parties"][0]["name"] = "Acme, Inc."   # comma must be quoted, not split
    payload["document"]["sha256"] = "e" * 64
    cv.store_record(empty_vault, payload, counterparty_override=None, name_override=None, local_source=None)
    assert cv.main(["export", "--vault", str(empty_vault)]) == 0
    rows = _csv(capsys.readouterr().out)
    assert any(dict(zip(cv.EXPORT_COLUMNS, r)).get("counterparty") == "Acme, Inc." for r in rows[1:])


def test_export_json(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--json", "--vault", str(loaded_vault)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 4
    assert data["columns"] == cv.EXPORT_COLUMNS
    assert all(set(cv.EXPORT_COLUMNS) <= set(d) for d in data["deals"])


def test_export_markdown(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--format", "md", "--vault", str(loaded_vault)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Contract portfolio (4 deal(s))")
    assert "| id | counterparty |" in out
    assert "Total value:" in out
    assert out.count("\n|") >= 5  # header sep + 4 rows


def test_export_expiring_before_filter(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--json", "--expiring-before", "2026-06-01", "--vault", str(loaded_vault)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data["deals"]} == {"acme-corporation/master-services-agreement"}


def test_export_needs_review_filter(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--json", "--needs-review", "--vault", str(loaded_vault)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 3
    assert not any(d["id"].startswith("acme") for d in data["deals"])


def test_export_empty_vault(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["export", "--vault", str(empty_vault)]) == 0
    rows = _csv(capsys.readouterr().out)
    assert rows == [cv.EXPORT_COLUMNS]  # header only


def test_export_next_due_never_in_past(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import datetime as dt
    cv.main(["export", "--json", "--vault", str(loaded_vault)])
    data = json.loads(capsys.readouterr().out)
    today = dt.date.today()
    for d in data["deals"]:
        if d["next_due"]:
            parsed = cv.parse_date(d["next_due"])
            assert parsed is not None and parsed >= today   # only soonest *upcoming* obligation
    assert any(d["next_due"] for d in data["deals"])         # at least one deal has one
