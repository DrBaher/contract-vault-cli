"""RFC 5545 (.ics) validity tests for `due --format ics`."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Dict, List

import pytest

import contract_vault_cli as cv


def _unfold(ics: str) -> List[str]:
    """RFC 5545 line unfolding: a CRLF followed by space/tab continues the prior line."""
    assert "\r\n" in ics, "ICS must use CRLF line endings"
    raw = ics.split("\r\n")
    out: List[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [ln for ln in out if ln]


def _events(ics: str) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []
    cur: Dict[str, str] | None = None
    in_alarm = False
    for line in _unfold(ics):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            assert cur is not None
            events.append(cur)
            cur = None
            continue
        if line == "BEGIN:VALARM":
            in_alarm = True
            cur = cur or {}
            cur["_has_alarm"] = "1"
            continue
        if line == "END:VALARM":
            in_alarm = False
            continue
        if cur is not None and ":" in line and not in_alarm:
            key, val = line.split(":", 1)
            cur[key.split(";")[0]] = val
        if cur is not None and in_alarm and line.startswith("TRIGGER"):
            cur["_alarm_trigger"] = line.split(":", 1)[1]
    return events


def _calendar(vault: Path) -> str:
    return cv.build_ics(cv.upcoming_obligations(vault, within_days=365, as_of=dt.date(2025, 1, 1)))


def test_calendar_envelope(loaded_vault: Path) -> None:
    ics = _calendar(loaded_vault)
    lines = _unfold(ics)
    assert lines[0] == "BEGIN:VCALENDAR"
    assert lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert any(ln.startswith("PRODID:") for ln in lines)
    assert "CALSCALE:GREGORIAN" in lines


def test_event_count_matches_dated_obligations(loaded_vault: Path) -> None:
    rows = cv.upcoming_obligations(loaded_vault, within_days=365, as_of=dt.date(2025, 1, 1))
    events = _events(_calendar(loaded_vault))
    assert len(events) == len(rows) == 3


def test_event_required_properties(loaded_vault: Path) -> None:
    for ev in _events(_calendar(loaded_vault)):
        assert ev["UID"].endswith("@contract-vault")
        assert re.fullmatch(r"\d{8}T\d{6}Z", ev["DTSTAMP"])
        assert "SUMMARY" in ev and ev["SUMMARY"]
        assert ev.get("_has_alarm") == "1"
        assert ev.get("_alarm_trigger", "").startswith("-P")


def test_all_day_dtstart_dtend(loaded_vault: Path) -> None:
    # Re-parse keeping params so we can confirm VALUE=DATE all-day events.
    ics = _calendar(loaded_vault)
    starts = re.findall(r"DTSTART;VALUE=DATE:(\d{8})", ics)
    ends = re.findall(r"DTEND;VALUE=DATE:(\d{8})", ics)
    assert starts and len(starts) == len(ends)
    for s, e in zip(starts, ends):
        sd = dt.datetime.strptime(s, "%Y%m%d").date()
        ed = dt.datetime.strptime(e, "%Y%m%d").date()
        assert ed == sd + dt.timedelta(days=1)  # all-day: DTEND is exclusive next day


def test_escaping_special_characters() -> None:
    rows = [
        {
            "deal": "x/y", "counterparty": "A, Inc; Ltd", "type": "obligation",
            "due": "2025-05-01", "days_until": 1,
            "description": "Pay; deliver, and notify\nnext line", "source": "manual",
            "confidence": 0.5, "lead_days": 7,
        }
    ]
    ics = cv.build_ics(rows)
    assert "\\;" in ics and "\\," in ics and "\\n" in ics
    # raw (unescaped) separators must not leak into the SUMMARY/DESCRIPTION text payload
    summary_line = [ln for ln in _unfold(ics) if ln.startswith("SUMMARY:")][0]
    assert "A\\, Inc\\; Ltd" in summary_line


def test_long_line_folding() -> None:
    rows = [
        {
            "deal": "x/y", "counterparty": "Z", "type": "obligation",
            "due": "2025-05-01", "days_until": 1,
            "description": "D" * 400, "source": "manual", "confidence": 0.5, "lead_days": 7,
        }
    ]
    ics = cv.build_ics(rows)
    # every physical line must be <= 75 octets
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, repr(line)
    # but after unfolding the long description is recovered intact
    desc = [ln for ln in _unfold(ics) if ln.startswith("DESCRIPTION:")][0]
    assert "D" * 400 in desc


def test_multibyte_folding_not_split() -> None:
    rows = [
        {
            "deal": "x/y", "counterparty": "Ünïcödé Café " + "é" * 60, "type": "obligation",
            "due": "2025-05-01", "days_until": 1, "description": "ok",
            "source": "manual", "confidence": 0.5, "lead_days": 7,
        }
    ]
    ics = cv.build_ics(rows)
    # must remain valid UTF-8 and unfold cleanly (no broken multibyte sequences)
    summary = [ln for ln in _unfold(ics) if ln.startswith("SUMMARY:")][0]
    assert "é" * 60 in summary


def test_due_ics_via_cli(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["due", "--format", "ics", "--vault", str(loaded_vault), "--as-of", "2025-01-01", "--within", "365d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in out
