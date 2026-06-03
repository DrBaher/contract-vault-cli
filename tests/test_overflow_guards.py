"""Regression tests for crafted/hand-edited numeric inputs that overflow date math.

These cover audit findings:
  P1 - crafted term.notice_period_days overflows ingest / recompute_schedule
  P1 - hand-edited obligation reminders overflow the projection horizon
  P2 - main() backstop must exit 1 (failure), not 2 (bad usage), on malformed state
  P3 - absurd --within / far-future --as-of windows overflow timedelta / dt.date

The contract throughout: a clean documented exit code (0/1/2), never a traceback,
and date arithmetic that saturates at dt.date bounds instead of raising.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES


def _ingest(vault: Path, payload: dict, name: str = "crafted.json") -> int:
    fx = vault.parent / name
    fx.write_text(json.dumps(payload), encoding="utf-8")
    return cv.main(["ingest", str(fx), "--vault", str(vault)])


def _base_payload() -> dict:
    return json.loads((EXTRACT_FIXTURES / "acme-msa.json").read_text())


# --------------------------------------------------------------------------- #
# P1: notice_period_days overflow (build_record + recompute_schedule)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("notice", [3_000_000, 10**18, -10**18, "9999999999 days"])
def test_ingest_absurd_notice_period_no_crash(empty_vault: Path, notice: object) -> None:
    payload = _base_payload()
    payload["term"]["notice_period_days"] = {"value": notice, "confidence": 0.9, "source": "deterministic"}
    # No OverflowError; ingest succeeds and simply omits the renewal window.
    assert _ingest(empty_vault, payload) == 0
    rec_file = next(empty_vault.rglob(cv.RECORD_FILENAME))
    rec = json.loads(rec_file.read_text())
    assert rec["term"].get("renewal_window") is None
    # No renewal_notice obligation was derived from the out-of-range notice.
    assert all(o.get("type") != "renewal_notice" for o in rec.get("obligations", []))


def test_ingest_sane_notice_period_still_builds_window(empty_vault: Path) -> None:
    # Guard against over-clamping: an in-range notice must still yield a window.
    payload = _base_payload()
    payload["term"]["notice_period_days"] = {"value": 60, "confidence": 0.9, "source": "deterministic"}
    assert _ingest(empty_vault, payload) == 0
    rec = json.loads(next(empty_vault.rglob(cv.RECORD_FILENAME)).read_text())
    assert rec["term"]["renewal_window"] is not None


def test_recompute_schedule_absurd_notice_no_crash(empty_vault: Path) -> None:
    # A hand-edited record.json with an absurd notice period, replayed through
    # recompute_schedule (e.g. via accept of a date/term field), must not overflow.
    rec: dict = {
        "id": "x/y",
        "counterparty": "Z",
        "expiration_date": "2030-01-01",
        "term": {"notice_period_days": 5_000_000},
        "field_meta": {},
        "obligations": [],
    }
    cv.recompute_schedule(rec)
    assert rec["term"]["renewal_window"] is None


# --------------------------------------------------------------------------- #
# P1: hand-edited reminders overflow remind/due horizon
# --------------------------------------------------------------------------- #


def test_coerce_leads_drops_out_of_range() -> None:
    assert cv._coerce_leads([30, 10**12, -5, cv.MAX_HORIZON_DAYS + 1, 90]) == [90, 30]


def test_remind_with_absurd_reminder_no_crash(empty_vault: Path) -> None:
    payload = _base_payload()
    payload["obligations"] = [
        {"text": "Pay by 2026-01-01.", "confidence": 0.9, "source": "deterministic"}
    ]
    assert _ingest(empty_vault, payload) == 0
    rec_file = next(empty_vault.rglob(cv.RECORD_FILENAME))
    rec = json.loads(rec_file.read_text())
    for ob in rec["obligations"]:
        ob["reminders"] = [10**15]
    rec_file.write_text(cv._dump_json(rec), encoding="utf-8")
    # Out-of-range reminders are dropped; remind falls back to the type default, no crash.
    assert cv.main(["remind", "--json", "--vault", str(empty_vault)]) in (0, 1)


# --------------------------------------------------------------------------- #
# P3: absurd --within and far-future --as-of
# --------------------------------------------------------------------------- #


def test_parse_within_caps_at_max_horizon() -> None:
    assert cv.parse_within("90d") == 90
    assert cv.parse_within(f"{cv.MAX_HORIZON_DAYS}d") == cv.MAX_HORIZON_DAYS
    with pytest.raises(cv.UsageError):
        cv.parse_within("99999999y")


def test_due_absurd_within_is_usage_error(empty_vault: Path) -> None:
    assert cv.main(["due", "--within", "99999999y", "--vault", str(empty_vault)]) == cv.EXIT_USAGE


def test_due_far_future_as_of_no_overflow(loaded_vault: Path) -> None:
    # as_of near dt.date.max plus a (capped) horizon would overflow without clamping.
    rc = cv.main(["due", "--as-of", "9999-12-31", "--within", "36500d", "--json", "--vault", str(loaded_vault)])
    assert rc in (0, 1)


def test_add_days_clamped_saturates() -> None:
    assert cv._add_days_clamped(dt.date.max, 10**9) == dt.date.max
    assert cv._add_days_clamped(dt.date.min, -(10**9)) == dt.date.min


def test_add_months_saturates_beyond_year_max() -> None:
    assert cv._add_months(dt.date(9999, 6, 1), 24) == dt.date.max


# --------------------------------------------------------------------------- #
# P2: backstop exit code (covered for malformed accept --from too)
# --------------------------------------------------------------------------- #


def test_accept_from_deals_with_null_entry_is_usage_error(empty_vault: Path, tmp_path: Path) -> None:
    # {"deals":[null]} / [{}] in a review-output file -> structured UsageError, not a
    # raw AttributeError/KeyError surfacing through the backstop.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"deals": [None]}))
    assert cv.main(["accept", "--from", str(bad), "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    bad.write_text(json.dumps({"deals": [{}]}))
    assert cv.main(["accept", "--from", str(bad), "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    bad.write_text(json.dumps({"deals": [{"id": "x/y", "flags": [{}]}]}))
    assert cv.main(["accept", "--from", str(bad), "--vault", str(empty_vault)]) == cv.EXIT_USAGE
