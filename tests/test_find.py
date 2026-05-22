"""Tests for `find`/`search` queries across fields."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

import contract_vault_cli as cv


def _find(vault: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> List[dict]:
    rc = cv.main(["find", "--json", "--vault", str(vault), *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)["deals"]


def test_find_by_counterparty(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--counterparty", "acme", capsys=capsys)
    assert len(deals) == 1
    assert deals[0]["counterparty"] == "Acme Corporation"


def test_find_by_governing_law(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--governing-law", "California", capsys=capsys)
    assert {d["id"] for d in deals} == {"initech-inc/mutual-non-disclosure-agreement"}


def test_find_expiring_before(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--expiring-before", "2026-06-01", capsys=capsys)
    ids = {d["id"] for d in deals}
    assert ids == {"acme-corporation/master-services-agreement"}


def test_find_value_gt(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--value-gt", "100000", capsys=capsys)
    ids = {d["id"] for d in deals}
    assert ids == {
        "acme-corporation/master-services-agreement",
        "umbrella-corp/commercial-lease-agreement",
    }


def test_find_auto_renew(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--auto-renew", capsys=capsys)
    assert len(deals) == 3
    assert all(d["auto_renew"] is True for d in deals)


def test_find_full_text(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "uptime", capsys=capsys)
    assert {d["id"] for d in deals} == {"soylent-systems/soylent-saas"}


def test_find_combined_filters(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--auto-renew", "--value-gt", "100000", capsys=capsys)
    assert {d["id"] for d in deals} == {
        "acme-corporation/master-services-agreement",
        "umbrella-corp/commercial-lease-agreement",
    }


def test_find_no_matches(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deals = _find(loaded_vault, "--counterparty", "nonexistent-party", capsys=capsys)
    assert deals == []


def test_find_invalid_expiring_before(loaded_vault: Path) -> None:
    rc = cv.main(["find", "--expiring-before", "not-a-date", "--vault", str(loaded_vault)])
    assert rc == cv.EXIT_USAGE


def test_search_alias(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["search", "--json", "--vault", str(loaded_vault), "--counterparty", "globex"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_get_and_show(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["get", "--json", "--vault", str(loaded_vault), "acme-corporation/master-services-agreement"])
    assert rc == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["title"] == "Master Services Agreement"
    # leaf-name resolution + human output via the `show` alias
    rc = cv.main(["show", "--vault", str(loaded_vault), "commercial-lease-agreement"])
    assert rc == 0
    assert "Umbrella Corp" in capsys.readouterr().out


def test_get_ambiguous_or_missing(loaded_vault: Path) -> None:
    assert cv.main(["get", "--vault", str(loaded_vault), "no-such-deal"]) == cv.EXIT_FAIL
