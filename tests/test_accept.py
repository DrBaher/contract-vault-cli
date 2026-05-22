"""Tests for the human-review workflow: accept (mark manual / override) + review --strict."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES, schema_errors


def _read(vault: Path, deal: str) -> dict:
    return json.loads((vault / deal / cv.RECORD_FILENAME).read_text())


def test_recompute_schedule_is_noop_on_fresh_record(empty_vault: Path) -> None:
    """Guards against divergence: recompute_schedule must reproduce build_record's output."""
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    deal = "acme-corporation/master-services-agreement"
    rec = _read(empty_vault, deal)
    before = copy.deepcopy(rec)
    cv.recompute_schedule(rec)
    assert rec["term"]["renewal_window"] == before["term"]["renewal_window"]
    assert rec["obligations"] == before["obligations"]


INITECH = "initech-inc/mutual-non-disclosure-agreement"


def test_accept_as_is_marks_manual_and_clears_flag(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    before = cv.review_flags(_read(empty_vault, INITECH))
    assert any(f["field"] == "value" for f in before)  # value was unidentified
    capsys.readouterr()
    assert cv.main(["accept", INITECH, "value", "--vault", str(empty_vault)]) == 0
    rec = _read(empty_vault, INITECH)
    assert rec["field_meta"]["value"] == {"source": "manual", "confidence": 1.0}
    assert not any(f["field"] == "value" for f in cv.review_flags(rec))  # no longer flagged


def test_accept_override_value(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    cv.main(["accept", INITECH, "value", "--value", "$2,000,000", "--vault", str(empty_vault)])
    rec = _read(empty_vault, INITECH)
    assert rec["value"] == {"raw": "$2,000,000", "amount": 2000000.0, "currency": "USD"}
    assert rec["field_meta"]["value"]["source"] == "manual"


def test_accept_expiration_recomputes_calendar(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    deal = "acme-corporation/master-services-agreement"
    cv.main(["accept", deal, "expiration_date", "--value", "2027-01-31", "--vault", str(empty_vault)])
    rec = _read(empty_vault, deal)
    assert rec["expiration_date"] == "2027-01-31"
    exp_ob = [o for o in rec["obligations"] if o["type"] == "expiration"][0]
    assert exp_ob["due"] == "2027-01-31"
    # renewal window = expiration - 60 days notice, recomputed
    rn = [o for o in rec["obligations"] if o["type"] == "renewal_notice"][0]
    assert rec["term"]["renewal_window"]["expiration"] == "2027-01-31"
    assert rn["due"] == rec["term"]["renewal_window"]["deadline"]


def test_accept_auto_renew_and_notice(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    cv.main(["accept", INITECH, "term.auto_renew", "--value", "true", "--vault", str(empty_vault)])
    cv.main(["accept", INITECH, "term.notice_period_days", "--value", "45 days", "--vault", str(empty_vault)])
    rec = _read(empty_vault, INITECH)
    assert rec["term"]["auto_renew"] is True
    assert rec["term"]["notice_period_days"] == 45
    # now that notice + expiration exist, a renewal window/obligation appears
    assert rec["term"]["renewal_window"] is not None
    assert any(o["type"] == "renewal_notice" for o in rec["obligations"])


def test_accept_party(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    cv.main(["accept", INITECH, "parties[1]", "--vault", str(empty_vault)])
    rec = _read(empty_vault, INITECH)
    assert rec["parties"][1]["source"] == "manual" and rec["parties"][1]["confidence"] == 1.0


def test_accept_clears_all_flags_for_deal(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    for field in ("value", "term.notice_period_days", "parties[1]"):
        cv.main(["accept", INITECH, field, "--vault", str(empty_vault)])
    assert cv.review_flags(_read(empty_vault, INITECH)) == []


def test_accept_conforms_to_schema(empty_vault: Path, record_schema: dict) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    deal = "acme-corporation/master-services-agreement"
    cv.main(["accept", deal, "expiration_date", "--value", "2027-01-31", "--vault", str(empty_vault)])
    rec = _read(empty_vault, deal)
    assert "last_reviewed_at" in rec["provenance"]
    assert not schema_errors(rec, record_schema)


def test_accept_commits(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    deal = "acme-corporation/master-services-agreement"
    before = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    cv.main(["accept", deal, "governing_law", "--value", "Delaware", "--vault", str(empty_vault)])
    after = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    assert after == before + 1


def test_accept_errors(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    deal = "acme-corporation/master-services-agreement"
    assert cv.main(["accept", deal, "not_a_field", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    assert cv.main(["accept", deal, "expiration_date", "--value", "garbage", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    assert cv.main(["accept", "no-such-deal", "value", "--vault", str(empty_vault)]) == cv.EXIT_FAIL


def test_review_strict_exit_code(loaded_vault: Path) -> None:
    assert cv.main(["review", "--vault", str(loaded_vault)]) == cv.EXIT_OK            # informational
    assert cv.main(["review", "--strict", "--vault", str(loaded_vault)]) == cv.EXIT_FAIL  # CI gate, has findings


def test_review_strict_clean_vault(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])  # all deterministic
    assert cv.main(["review", "--strict", "--vault", str(empty_vault)]) == cv.EXIT_OK
