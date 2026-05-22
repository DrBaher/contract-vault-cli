"""Tests for the `due`/`obligations` projection (deterministic, LLM-off)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv


def _due_json(vault: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> dict:
    rc = cv.main(["due", "--format", "json", "--vault", str(vault), *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_due_window_365(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _due_json(loaded_vault, "--as-of", "2025-01-01", "--within", "365d", capsys=capsys)
    assert data["within_days"] == 365
    assert data["as_of"] == "2025-01-01"
    dues = [(o["deal"], o["due"], o["type"]) for o in data["obligations"]]
    assert dues == [
        ("umbrella-corp/commercial-lease-agreement", "2025-03-01", "obligation"),
        ("acme-corporation/master-services-agreement", "2025-04-15", "obligation"),
        ("acme-corporation/master-services-agreement", "2025-12-30", "renewal_notice"),
    ]


def test_due_sorted_and_days_until(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _due_json(loaded_vault, "--as-of", "2025-01-01", "--within", "365d", capsys=capsys)
    days = [o["days_until"] for o in data["obligations"]]
    assert days == sorted(days)
    first = data["obligations"][0]
    assert first["days_until"] == (dt.date(2025, 3, 1) - dt.date(2025, 1, 1)).days


def test_due_narrow_window(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _due_json(loaded_vault, "--as-of", "2025-04-01", "--within", "30d", capsys=capsys)
    assert data["count"] == 1
    assert data["obligations"][0]["due"] == "2025-04-15"


def test_due_lead_days_present(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _due_json(loaded_vault, "--as-of", "2025-01-01", "--within", "365d", capsys=capsys)
    by_type = {o["type"]: o["lead_days"] for o in data["obligations"]}
    assert by_type["renewal_notice"] == 14
    assert by_type["obligation"] == 7


def test_due_within_units(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 52 weeks ~= 364 days; should behave like a year-ish window
    data = _due_json(loaded_vault, "--as-of", "2025-01-01", "--within", "52w", capsys=capsys)
    assert data["within_days"] == 364


def test_due_table_default(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["due", "--vault", str(loaded_vault), "--as-of", "2025-01-01", "--within", "365d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Upcoming obligations" in out
    assert "renewal_notice" in out


def test_due_table_empty(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["due", "--vault", str(loaded_vault), "--as-of", "2000-01-01", "--within", "1d"])
    assert rc == 0
    assert "no obligations due" in capsys.readouterr().out.lower()


def test_obligations_alias(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["obligations", "--format", "json", "--vault", str(loaded_vault), "--as-of", "2025-01-01", "--within", "365d"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["count"] == 3


def test_due_invalid_within(loaded_vault: Path) -> None:
    assert cv.main(["due", "--within", "soon", "--vault", str(loaded_vault)]) == cv.EXIT_USAGE


def test_due_json_global_flag(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # the global --json flag forces JSON regardless of --format
    rc = cv.main(["due", "--json", "--vault", str(loaded_vault), "--as-of", "2025-01-01", "--within", "365d"])
    assert rc == 0
    assert "obligations" in json.loads(capsys.readouterr().out)


def test_parse_within_units() -> None:
    assert cv.parse_within("90d") == 90
    assert cv.parse_within("90") == 90
    assert cv.parse_within("2w") == 14
    assert cv.parse_within("3m") == 90
    assert cv.parse_within("1y") == 365
    with pytest.raises(cv.UsageError):
        cv.parse_within("nope")
