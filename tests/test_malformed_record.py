"""Malformed / hand-edited record.json must yield a clean error or empty result --
never a Python traceback. record.json is user- and git-editable, so every nested
field may be null, a string, a list, etc. (audit P1: malformed-record crashes)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES


def _ingest_one(vault: Path) -> str:
    """Ingest a single real fixture and return its deal id (relative path under the vault)."""
    fx = EXTRACT_FIXTURES / "acme-msa.json"
    assert cv.main(["ingest", str(fx), "--vault", str(vault)]) == 0
    rec_file = next(vault.rglob(cv.RECORD_FILENAME))
    return rec_file.parent.relative_to(vault).as_posix()


def _record_path(vault: Path, deal: str) -> Path:
    return vault / deal / cv.RECORD_FILENAME


def _corrupt(vault: Path, deal: str, mutate: Callable[[dict], None]) -> None:
    path = _record_path(vault, deal)
    rec = json.loads(path.read_text())
    mutate(rec)
    path.write_text(cv._dump_json(rec), encoding="utf-8")


# Commands that read records and must survive a malformed shape without a traceback.
_READ_COMMANDS = [
    ["get", "{deal}"],
    ["get", "{deal}", "--json"],
    ["list"],
    ["list", "--json"],
    ["find", "--json"],
    ["find", "--text", "acme"],
    ["stats"],
    ["stats", "--json"],
    ["verify"],
    ["verify", "--json"],
    ["due", "--json", "--within", "365d"],
    ["risk", "--json"],
    ["review"],
    ["review", "--json"],
    ["export", "--format", "csv"],
    ["export", "--format", "json"],
]


def _run_all_read_commands(vault: Path, deal: str) -> None:
    for tmpl in _READ_COMMANDS:
        argv = [a.format(deal=deal) for a in tmpl] + ["--vault", str(vault)]
        # The only contract here: no uncaught exception / traceback. Any documented
        # exit code (0 ok, 1 fail, 2 usage) is acceptable for a corrupt record.
        rc = cv.main(argv)
        assert rc in (0, 1, 2), f"{argv} returned {rc}"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.__setitem__("value", None), id="value-null"),
        pytest.param(lambda r: r.__setitem__("value", "not-a-dict"), id="value-string"),
        pytest.param(lambda r: r.__setitem__("value", [1, 2]), id="value-list"),
        pytest.param(lambda r: r.__setitem__("term", None), id="term-null"),
        pytest.param(lambda r: r.__setitem__("term", []), id="term-list"),
        pytest.param(lambda r: r.__setitem__("source", None), id="source-null"),
        pytest.param(lambda r: r.__setitem__("source", "x"), id="source-string"),
        pytest.param(lambda r: r.__setitem__("provenance", None), id="provenance-null"),
        pytest.param(lambda r: r.__setitem__("provenance", []), id="provenance-list"),
        pytest.param(lambda r: r.__setitem__("parties", None), id="parties-null"),
        pytest.param(lambda r: r.__setitem__("parties", "Globex"), id="parties-string"),
        pytest.param(lambda r: r.__setitem__("parties", [None, "x", 3]), id="parties-nondict-elems"),
        pytest.param(lambda r: r.__setitem__("field_meta", None), id="field_meta-null"),
        pytest.param(lambda r: r.__setitem__("field_meta", ["a", "b"]), id="field_meta-list"),
        pytest.param(lambda r: r.__setitem__("obligations", None), id="obligations-null"),
        pytest.param(lambda r: r.__setitem__("obligations", {"k": "v"}), id="obligations-dict"),
        pytest.param(lambda r: r.__setitem__("obligations", [None, "x"]), id="obligations-nondict-elems"),
        pytest.param(
            lambda r: r["term"].__setitem__("renewal_window", []) if isinstance(r.get("term"), dict) else None,
            id="renewal_window-list",
        ),
    ],
)
def test_read_commands_survive_malformed_field(empty_vault: Path, mutate: Callable[[dict], None]) -> None:
    deal = _ingest_one(empty_vault)
    _corrupt(empty_vault, deal, mutate)
    _run_all_read_commands(empty_vault, deal)


def test_obligation_command_idless_obligation_no_keyerror(empty_vault: Path) -> None:
    # An index-resolved obligation lacking "id" must not raise KeyError in `obligation`.
    deal = _ingest_one(empty_vault)

    def drop_ids(r: dict) -> None:
        for o in r.get("obligations", []):
            o.pop("id", None)

    _corrupt(empty_vault, deal, drop_ids)
    # Resolve by index [0]; the command stamps a stable id and succeeds cleanly.
    rc = cv.main(["obligation", deal, "[0]", "--status", "done", "--vault", str(empty_vault)])
    assert rc == 0
    rec = json.loads(_record_path(empty_vault, deal).read_text())
    assert rec["obligations"][0].get("id")  # id was stamped back


def test_obligation_missing_obligations_list(empty_vault: Path) -> None:
    deal = _ingest_one(empty_vault)
    _corrupt(empty_vault, deal, lambda r: r.__setitem__("obligations", None))
    rc = cv.main(["obligation", deal, "0", "--status", "done", "--vault", str(empty_vault)])
    # No obligations -> clean NotFound/usage error, never a traceback.
    assert rc in (1, 2)


def test_main_backstop_catches_generic_exception(
    monkeypatch: pytest.MonkeyPatch, empty_vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A command that raises an unexpected (non-VaultError/OSError) exception should be
    # caught by main()'s backstop: clean `error: ...` to stderr + exit 2, no traceback.
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected internal state")

    # cmd_stats calls load_all_records; the parser already bound func at parse time, so
    # we inject the failure into a helper the command invokes rather than the command itself.
    monkeypatch.setattr(cv, "load_all_records", boom)
    rc = cv.main(["stats", "--vault", str(empty_vault)])
    assert rc == cv.EXIT_USAGE
    err = capsys.readouterr().err
    assert "unexpected internal state" in err
    assert "Traceback" not in err
