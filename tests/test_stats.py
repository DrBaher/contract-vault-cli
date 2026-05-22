"""Tests for `stats` portfolio aggregation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv


def _stats(vault: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> dict:
    rc = cv.main(["stats", "--json", "--vault", str(vault), *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_stats_counts_and_totals(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _stats(loaded_vault, capsys=capsys)
    assert data["count"] == 4
    # acme has an explicit "$" -> USD; umbrella "£" -> GBP; soylent is a bare number
    # (no currency symbol) -> "(unknown)"; initech has no value at all.
    assert data["total_value"]["USD"] == 120000.0
    assert data["total_value"]["GBP"] == 2400000.0
    assert data["total_value"]["(unknown)"] == 50000.0


def test_stats_auto_renew_and_breakdowns(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _stats(loaded_vault, capsys=capsys)
    assert data["auto_renew_count"] == 3
    assert set(data["by_governing_law"]) == {"Delaware", "California", "New York", "England and Wales"}
    assert data["by_counterparty"]["Acme Corporation"] == 1


def test_stats_expiring_soon_deterministic(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # as-of 2026-01-01 -> 90-day horizon ends 2026-04-01 -> only acme (2026-02-28)
    data = _stats(loaded_vault, "--as-of", "2026-01-01", capsys=capsys)
    assert data["expiring_within_90d"] == 1


def test_stats_empty_vault(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _stats(empty_vault, capsys=capsys)
    assert data["count"] == 0
    assert data["total_value"] == {}


def test_stats_table(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["stats", "--vault", str(loaded_vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Portfolio: 4 deal(s)" in out
    assert "by counterparty" in out


def test_stats_corpus(corpus_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _stats(corpus_vault, capsys=capsys)
    assert data["count"] == 2
    assert data["total_value"]["USD"] == 3000000.0
