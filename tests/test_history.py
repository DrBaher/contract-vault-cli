"""Tests for `history` — per-deal git history (ingest + each accept)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES

DEAL = "acme-corporation/master-services-agreement"


def test_history_after_ingest(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    capsys.readouterr()  # drop ingest output so we parse only the history JSON
    rc = cv.main(["history", "--json", "--vault", str(empty_vault), DEAL])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["deal"] == DEAL
    assert data["count"] == 1
    assert "ingest" in data["history"][0]["message"]
    assert data["history"][0]["author"] == "Test User"   # set by conftest env


def test_history_records_accepts_newest_first(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    cv.main(["accept", DEAL, "governing_law", "--value", "Delaware", "--vault", str(empty_vault)])
    capsys.readouterr()  # drop ingest/accept output
    rc = cv.main(["history", "--json", "--vault", str(empty_vault), DEAL])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 2
    assert "accept" in data["history"][0]["message"]   # newest first
    assert "ingest" in data["history"][1]["message"]


def test_history_table(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    rc = cv.main(["history", "--vault", str(empty_vault), DEAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert DEAL in out and "ingest" in out


def test_history_missing_deal(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    assert cv.main(["history", "--vault", str(empty_vault), "no-such-deal"]) == cv.EXIT_FAIL
