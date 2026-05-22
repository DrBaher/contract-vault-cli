"""Tests for the corpus-wide reminder policy: `config reminders` (0.4.0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv


def _payload(exp: str = "2026-06-01") -> dict:
    f = lambda x: {"value": x, "confidence": 0.9, "source": "deterministic"}  # noqa: E731
    return {
        "document": {"title": "Svc", "format": "pdf", "sha256": "a" * 64, "source_path": None},
        "parties": [{"name": "Globex", "role": "Customer", "confidence": 0.9, "source": "deterministic"}],
        "dates": {"effective": f("2026-01-01"), "expiration": f(exp)},
        "term": {"length": f("1y"), "auto_renew": f(False), "notice_period_days": f(None)},
        "governing_law": f("NY"), "clauses": [], "defined_terms": [], "value": f("$1"),
        "obligations": [], "_meta": {"extractor_version": "x", "tiers_used": ["deterministic"], "llm_used": False},
    }


def _ingest(vault: Path, payload: dict) -> str:
    deal, _ = cv.store_record(vault, payload, counterparty_override=None, name_override=None, local_source=None)
    return deal


def test_obligation_reminders_resolution_order() -> None:
    assert cv.obligation_reminders({"type": "expiration", "reminders": [5]}, {"expiration": [60]}) == [5]   # per-ob wins
    assert cv.obligation_reminders({"type": "expiration"}, {"expiration": [60, 30]}) == [60, 30]            # vault type default
    assert cv.obligation_reminders({"type": "obligation"}, {"default": [14, 7]}) == [14, 7]                 # vault catch-all
    assert cv.obligation_reminders({"type": "expiration"}, {}) == [30]                                      # built-in
    assert cv.obligation_reminders({"type": "renewal_notice"}, None) == [14]                                # built-in


def test_config_set_persists_and_commits(empty_vault: Path) -> None:
    before = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    assert cv.main(["config", "reminders", "--type", "expiration", "--set", "60,30,7", "--vault", str(empty_vault)]) == 0
    cfg = json.loads((empty_vault / cv.VAULT_CONFIG_NAME).read_text())
    assert cfg["reminder_defaults"]["expiration"] == [60, 30, 7]
    after = int(cv._git(empty_vault, "rev-list", "--count", "HEAD").stdout.strip())
    assert after == before + 1


def test_config_applies_corpus_wide(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ingest(empty_vault, _payload(exp="2026-06-01"))  # expiration obligation, no per-ob override
    cv.main(["config", "reminders", "--type", "expiration", "--set", "60,30,7", "--vault", str(empty_vault)])
    capsys.readouterr()
    cv.main(["due", "--json", "--as-of", "2026-01-01", "--within", "365d", "--vault", str(empty_vault)])
    rows = json.loads(capsys.readouterr().out)["obligations"]
    exp = next(o for o in rows if o["type"] == "expiration")
    assert exp["reminders"] == [60, 30, 7]            # corpus-wide policy applied, no per-obligation edit
    # ...and the .ics gets one VALARM per lead
    ics = cv.build_ics(cv.upcoming_obligations(empty_vault, within_days=365, as_of=cv.dt.date(2026, 1, 1)))
    assert ics.count("BEGIN:VALARM") == 3


def test_per_obligation_override_beats_policy(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deal = _ingest(empty_vault, _payload(exp="2026-06-01"))
    cv.main(["config", "reminders", "--type", "expiration", "--set", "60,30,7", "--vault", str(empty_vault)])
    rec = json.loads((empty_vault / deal / cv.RECORD_FILENAME).read_text())
    exp_id = next(o["id"] for o in rec["obligations"] if o["type"] == "expiration")
    cv.main(["obligation", deal, exp_id, "--reminders", "1", "--vault", str(empty_vault)])
    capsys.readouterr()
    cv.main(["due", "--json", "--as-of", "2026-01-01", "--within", "365d", "--vault", str(empty_vault)])
    exp = next(o for o in json.loads(capsys.readouterr().out)["obligations"] if o["type"] == "expiration")
    assert exp["reminders"] == [1]   # explicit override wins over the corpus policy


def test_config_default_catch_all(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["config", "reminders", "--type", "default", "--set", "45,15", "--vault", str(empty_vault)])
    capsys.readouterr()
    # a type with no specific entry falls back to 'default'
    assert cv.obligation_reminders({"type": "obligation"}, {"default": [45, 15]}) == [45, 15]


def test_config_show_and_clear(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["config", "reminders", "--type", "expiration", "--set", "60,30,7", "--vault", str(empty_vault)])
    capsys.readouterr()
    assert cv.main(["config", "reminders", "--show", "--vault", str(empty_vault)]) == 0
    assert "expiration" in capsys.readouterr().out
    cv.main(["config", "reminders", "--type", "expiration", "--clear", "--vault", str(empty_vault)])
    cfg = json.loads((empty_vault / cv.VAULT_CONFIG_NAME).read_text())
    assert "expiration" not in cfg.get("reminder_defaults", {})


def test_config_json(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["config", "reminders", "--type", "obligation", "--set", "14,7", "--vault", str(empty_vault)])
    capsys.readouterr()
    cv.main(["config", "reminders", "--json", "--vault", str(empty_vault)])
    data = json.loads(capsys.readouterr().out)
    assert data["reminder_defaults"]["obligation"] == [14, 7]
    assert "builtin_fallback" in data


def test_config_errors(empty_vault: Path) -> None:
    assert cv.main(["config", "reminders", "--set", "30,7", "--vault", str(empty_vault)]) == cv.EXIT_USAGE  # no --type
    assert cv.main(["config", "reminders", "--type", "expiration", "--set", "-5", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
    assert cv.main(["config", "reminders", "--type", "expiration", "--set", "abc", "--vault", str(empty_vault)]) == cv.EXIT_USAGE
