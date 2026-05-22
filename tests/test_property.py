"""Property-based invariants using stdlib random.Random(seed) (no hypothesis)."""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest

import contract_vault_cli as cv
from conftest import load_json, all_extract_fixtures

SEED = 0xC0FFEE
CASES = 250


def _rand_date(rng: random.Random) -> dt.date:
    base = dt.date(2020, 1, 1)
    return base + dt.timedelta(days=rng.randint(0, 365 * 12))


def _field(value: Any, rng: random.Random, source: str = "deterministic") -> Dict[str, Any]:
    return {"value": value, "confidence": round(rng.uniform(0.0, 1.0), 4), "source": source}


def _random_payload(rng: random.Random) -> Dict[str, Any]:
    eff = _rand_date(rng)
    exp = eff + dt.timedelta(days=rng.randint(30, 3000))
    notice = rng.choice([None, rng.randint(1, 365)])
    amount = rng.choice([None, rng.randint(1000, 9_000_000)])
    return {
        "document": {
            "title": rng.choice([None, "Agreement", "Lease & Co., Ltd."]),
            "format": rng.choice(["pdf", "docx", "markdown", "text", "html"]),
            "sha256": "%064x" % rng.getrandbits(256),
            "source_path": f"/c/{rng.randint(0, 9999)}.pdf",
        },
        "parties": [
            {"name": rng.choice(["Acme", "Globex", "Initech", "Soylent"]), "role": None,
             "confidence": round(rng.uniform(0, 1), 4), "source": "deterministic"},
        ],
        "dates": {
            "effective": _field(eff.isoformat(), rng),
            "expiration": _field(exp.isoformat(), rng),
        },
        "term": {
            "length": _field(f"{rng.randint(1, 5)} years", rng),
            "auto_renew": _field(rng.choice([True, False]), rng),
            "notice_period_days": _field(notice, rng, "deterministic" if notice else "none"),
        },
        "governing_law": _field(rng.choice(["Delaware", "California", "New York"]), rng),
        "clauses": [],
        "defined_terms": [],
        "value": _field(f"${amount:,}" if amount is not None else None, rng,
                        "deterministic" if amount is not None else "none"),
        "obligations": [
            {"text": f"Deliver by {(_rand_date(rng)).isoformat()}.",
             "confidence": round(rng.uniform(0, 1), 4), "source": "deterministic"},
        ],
        "_meta": {"extractor_version": "extract-cli 1.4.0", "tiers_used": ["deterministic"], "llm_used": False},
        "_amount": amount,
        "_notice": notice,
        "_exp": exp,
    }


def test_build_record_invariants(record_schema: dict) -> None:
    rng = random.Random(SEED)
    for _ in range(CASES):
        payload = _random_payload(rng)
        amount, notice, exp = payload.pop("_amount"), payload.pop("_notice"), payload.pop("_exp")
        rec = cv.build_record(payload, deal_identifier="cp/deal", title=payload["document"]["title"],
                              source_rel_path="x", source_vaulted=False)

        # 1. always schema-conformant
        from conftest import schema_errors
        assert not schema_errors(rec, record_schema)

        # 2. expiration obligation due == expiration_date
        exp_obs = [o for o in rec["obligations"] if o["type"] == "expiration"]
        assert exp_obs and exp_obs[0]["due"] == exp.isoformat()

        # 3. renewal window deadline == expiration - notice (when notice present)
        if notice is not None:
            assert rec["term"]["renewal_window"]["deadline"] == (exp - dt.timedelta(days=notice)).isoformat()
            rn = [o for o in rec["obligations"] if o["type"] == "renewal_notice"]
            assert rn and rn[0]["due"] == (exp - dt.timedelta(days=notice)).isoformat()
        else:
            assert rec["term"]["renewal_window"] is None

        # 4. value amount recovered from the formatted string
        if amount is not None:
            assert rec["value"]["amount"] == float(amount)

        # 5. every dated obligation parses
        for o in rec["obligations"]:
            if o["due"] is not None:
                assert cv.parse_date(o["due"]) is not None


def test_parse_date_round_trips() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(CASES):
        d = _rand_date(rng)
        assert cv.parse_date(d.isoformat()) == d
        assert cv.parse_date(d.strftime("%B %d, %Y")) == d
        assert cv.parse_date(d.strftime("%d %B %Y")) == d


def test_parse_money_round_trips() -> None:
    rng = random.Random(SEED + 2)
    for _ in range(CASES):
        amount = rng.randint(1, 5_000_000)
        assert cv.parse_money(f"${amount:,}") == (float(amount), "USD")
        assert cv.parse_money(f"USD {amount}") == (float(amount), "USD")
        assert cv.parse_money(amount) == (float(amount), None)


def test_money_suffixes() -> None:
    assert cv.parse_money("$1.5M") == (1_500_000.0, "USD")
    assert cv.parse_money("2k") == (2_000.0, None)
    assert cv.parse_money("3B") == (3_000_000_000.0, None)


def test_slugify_idempotent() -> None:
    rng = random.Random(SEED + 3)
    alphabet = "ABCdef 123 -_/&,.()"
    for _ in range(CASES):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        once = cv.slugify(s)
        assert cv.slugify(once) == once
        assert " " not in once and "/" not in once
        assert len(once) <= 80  # always a path-component-safe length


def test_slugify_caps_long_input() -> None:
    long = "word-" * 500
    assert len(cv.slugify(long)) <= 80
    assert not cv.slugify(long).endswith("-")


def _write_record(vault: Path, rec: dict) -> None:
    d = vault / rec["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / cv.RECORD_FILENAME).write_text(cv._dump_json(rec), encoding="utf-8")


def test_due_window_monotonic_and_ics_count(tmp_path: Path) -> None:
    rng = random.Random(SEED + 4)
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(15):
        payload = _random_payload(rng)
        for k in ("_amount", "_notice", "_exp"):
            payload.pop(k)
        rec = cv.build_record(payload, deal_identifier=f"cp{i}/deal{i}",
                              title=payload["document"]["title"], source_rel_path="x", source_vaulted=False)
        _write_record(vault, rec)

    as_of = dt.date(2020, 1, 1)
    prev: set = set()
    for window in (30, 180, 365, 1500, 6000):
        rows = cv.upcoming_obligations(vault, within_days=window, as_of=as_of)
        keys = {(r["deal"], r["due"], r["type"]) for r in rows}
        assert prev <= keys  # wider window only adds obligations
        prev = keys
        # ICS event count equals the number of dated obligations returned
        ics = cv.build_ics(rows)
        assert ics.count("BEGIN:VEVENT") == len(rows)
        # all returned dues lie within [as_of, as_of+window]
        for r in rows:
            due = cv.parse_date(r["due"])
            assert due is not None and as_of <= due <= as_of + dt.timedelta(days=window)


def test_ingest_idempotent_under_random_repeats(tmp_path: Path) -> None:
    rng = random.Random(SEED + 5)
    vault = tmp_path / "vault"
    assert cv.main(["init", str(vault)]) == 0
    fixtures = [load_json(p) for p in all_extract_fixtures()]
    # ingest each fixture a random number of times in random order; count stays stable
    plan: List[dict] = []
    for fx in fixtures:
        plan += [fx] * rng.randint(1, 3)
    rng.shuffle(plan)
    for fx in plan:
        cv.store_record(vault, dict(fx), counterparty_override=None, name_override=None, local_source=None)
    assert len(list(vault.rglob(cv.RECORD_FILENAME))) == len(fixtures)
