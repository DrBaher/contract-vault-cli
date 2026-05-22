"""Tests for obligation lifecycle: id + status (open/done/waived) + owner (0.2.0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES, assert_valid, schema_errors

ACME = "acme-corporation/master-services-agreement"


def _read(vault: Path, deal: str = ACME) -> dict:
    return json.loads((vault / deal / cv.RECORD_FILENAME).read_text())


def _ingest(vault: Path, name: str = "acme-msa") -> None:
    assert cv.main(["ingest", str(EXTRACT_FIXTURES / f"{name}.json"), "--vault", str(vault)]) == 0


def _payment_id(vault: Path) -> str:
    return next(o["id"] for o in _read(vault)["obligations"] if o["type"] == "obligation")


def test_ingest_stamps_lifecycle_fields(empty_vault: Path) -> None:
    _ingest(empty_vault)
    for o in _read(empty_vault)["obligations"]:
        assert o["id"].startswith("o") and len(o["id"]) == 9
        assert o["status"] == "open" and o["owner"] is None


def test_obligation_id_is_stable_and_distinct() -> None:
    a = {"type": "obligation", "description": "Pay by 2026-01-01."}
    assert cv.obligation_id(a) == cv.obligation_id(dict(a))
    assert cv.obligation_id({"type": "expiration", "description": "x"}) != cv.obligation_id(a)


def test_obligation_set_status_and_owner(empty_vault: Path) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    assert cv.main(["obligation", ACME, pid, "--status", "done", "--owner", "finance", "--vault", str(empty_vault)]) == 0
    o = next(x for x in _read(empty_vault)["obligations"] if x["id"] == pid)
    assert o["status"] == "done" and o["owner"] == "finance"


def test_find_obligation_by_prefix_and_index(empty_vault: Path) -> None:
    _ingest(empty_vault)
    rec = _read(empty_vault)
    pid = _payment_id(empty_vault)
    assert cv.find_obligation(rec, pid[:5])["id"] == pid          # unique prefix
    assert cv.find_obligation(rec, "[0]")["id"] == rec["obligations"][0]["id"]  # index


def _due(vault: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    cv.main(["due", "--json", "--as-of", "2025-01-01", "--within", "999d", "--vault", str(vault), *extra])
    return json.loads(capsys.readouterr().out)


def test_due_default_hides_done(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    cv.main(["obligation", ACME, pid, "--status", "done", "--vault", str(empty_vault)])
    capsys.readouterr()
    assert pid not in {o["id"] for o in _due(empty_vault, capsys)["obligations"]}        # open view hides it
    allv = _due(empty_vault, capsys, "--status", "all")
    assert any(o["id"] == pid and o["status"] == "done" for o in allv["obligations"])    # all view shows it


def test_due_filter_by_type_and_owner(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    cv.main(["obligation", ACME, pid, "--owner", "legal", "--vault", str(empty_vault)])
    capsys.readouterr()
    only_exp = _due(empty_vault, capsys, "--type", "expiration")
    assert only_exp["count"] >= 1 and all(o["type"] == "expiration" for o in only_exp["obligations"])
    by_owner = _due(empty_vault, capsys, "--owner", "legal")
    assert {o["id"] for o in by_owner["obligations"]} == {pid}


def test_recompute_preserves_status(empty_vault: Path) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    cv.main(["obligation", ACME, pid, "--status", "done", "--vault", str(empty_vault)])
    cv.main(["accept", ACME, "expiration_date", "--value", "2027-01-31", "--vault", str(empty_vault)])  # triggers recompute
    o = next(x for x in _read(empty_vault)["obligations"] if x["id"] == pid)
    assert o["status"] == "done"


def test_clear_owner_with_empty_string(empty_vault: Path) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    cv.main(["obligation", ACME, pid, "--owner", "finance", "--vault", str(empty_vault)])
    cv.main(["obligation", ACME, pid, "--owner", "", "--vault", str(empty_vault)])
    assert next(x for x in _read(empty_vault)["obligations"] if x["id"] == pid)["owner"] is None


def test_obligation_errors(empty_vault: Path) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    assert cv.main(["obligation", ACME, pid, "--vault", str(empty_vault)]) == cv.EXIT_USAGE       # no --status/--owner
    assert cv.main(["obligation", ACME, "zzzznope", "--status", "done", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    assert cv.main(["obligation", "no-deal", pid, "--status", "done", "--vault", str(empty_vault)]) == cv.EXIT_FAIL


def test_schemas_conform_after_lifecycle_change(
    empty_vault: Path, record_schema: dict, obligations_schema: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    cv.main(["obligation", ACME, pid, "--status", "waived", "--owner", "ops", "--vault", str(empty_vault)])
    assert not schema_errors(_read(empty_vault), record_schema)
    capsys.readouterr()
    assert_valid(_due(empty_vault, capsys, "--status", "all"), obligations_schema)


def test_commit_made(empty_vault: Path) -> None:
    _ingest(empty_vault)
    pid = _payment_id(empty_vault)
    before = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    cv.main(["obligation", ACME, pid, "--status", "done", "--vault", str(empty_vault)])
    after = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    assert after == before + 1
