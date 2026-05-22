"""Tests for the deterministic 'needs-review' surface and delegated-LLM reporting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv


def test_review_flags_unit() -> None:
    rec = {
        "field_meta": {
            "expiration_date": {"source": "none", "confidence": 0.0},
            "governing_law": {"source": "llm", "confidence": 0.8},
            "value": {"source": "deterministic", "confidence": 0.4},
            "effective_date": {"source": "deterministic", "confidence": 0.95},
        },
        "parties": [{"name": "X", "source": "deterministic", "confidence": 0.99}],
        "obligations": [{"type": "obligation", "description": "do thing", "source": "llm", "confidence": 0.5}],
    }
    flags = {f["field"]: f["reasons"] for f in cv.review_flags(rec, 0.6)}
    assert "unidentified" in flags["expiration_date"]
    assert "llm-derived" in flags["governing_law"]
    assert any("low-confidence" in r for r in flags["value"])
    assert "effective_date" not in flags                      # high-confidence deterministic: clean
    assert not any(k.startswith("parties") for k in flags)    # high-confidence party: clean
    ob = next(v for k, v in flags.items() if k.startswith("obligation"))
    assert "llm-derived" in ob and any("low-confidence" in r for r in ob)


def test_review_never_calls_llm_pure_function() -> None:
    # review_flags must be a pure read of stored provenance — no extract/network/LLM.
    rec = {"field_meta": {"value": {"source": "none", "confidence": 0.0}}, "parties": [], "obligations": []}
    assert cv.review_flags(rec) == [
        {"field": "value", "label": "value", "source": "none", "confidence": 0.0, "reasons": ["unidentified"]}
    ]


def test_cmd_review_json(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["review", "--json", "--vault", str(loaded_vault)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    ids = {d["id"] for d in data["deals"]}
    # acme is all-deterministic/high-confidence -> clean; the other three have none/llm fields
    assert "acme-corporation/master-services-agreement" not in ids
    assert "initech-inc/mutual-non-disclosure-agreement" in ids
    assert data["count"] == 3


def test_cmd_review_threshold_raises_bar(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # at 0.99, even acme's high-but-<0.99 deterministic fields become low-confidence
    rc = cv.main(["review", "--json", "--threshold", "0.99", "--vault", str(loaded_vault)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 4
    assert any(d["id"].startswith("acme") for d in data["deals"])


def test_review_clean_vault(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from conftest import EXTRACT_FIXTURES
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    capsys.readouterr()
    rc = cv.main(["review", "--vault", str(empty_vault)])
    assert rc == 0
    assert "nothing needs review" in capsys.readouterr().out.lower()


def test_find_needs_review(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["find", "--needs-review", "--json", "--vault", str(loaded_vault)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 3
    assert not any(d["id"].startswith("acme") for d in data["deals"])


def test_stats_llm_and_review_counts(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["stats", "--json", "--vault", str(loaded_vault)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["llm_used_count"] == 3        # initech/umbrella/soylent have _meta.llm_used
    assert data["needs_review_count"] == 3


def test_ingest_why_reports_provenance(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from conftest import EXTRACT_FIXTURES
    rc = cv.main(["ingest", "--why", str(EXTRACT_FIXTURES / "initech-nda.json"), "--vault", str(empty_vault)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ingest.provenance" in err
    assert "unidentified=" in err and "needs_review=" in err


def test_get_shows_llm_used_and_review(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["get", "--vault", str(loaded_vault), "initech-inc/mutual-non-disclosure-agreement"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "llm_used=True" in out
    assert "needs review" in out
