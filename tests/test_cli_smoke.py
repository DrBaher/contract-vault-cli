"""End-to-end smoke tests for the command surface (no extract-cli, zero extras)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_vault_cli as cv


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["--version"])
    assert rc == 0
    assert f"contract-vault {cv.__version__}" in capsys.readouterr().out


def test_no_subcommand_prints_help_and_usage_exit(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main([])
    assert rc == cv.EXIT_USAGE
    assert "usage:" in capsys.readouterr().out.lower()


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["definitely-not-a-command"])
    assert rc == cv.EXIT_USAGE


def test_init_creates_config_and_git(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    rc = cv.main(["init", str(vault)])
    assert rc == 0
    assert (vault / cv.VAULT_CONFIG_NAME).is_file()
    assert (vault / ".git").is_dir()
    cfg = json.loads((vault / cv.VAULT_CONFIG_NAME).read_text())
    assert cfg["kind"] == cv.VAULT_KIND


def test_init_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "v"
    assert cv.main(["init", str(vault)]) == 0
    capsys.readouterr()
    assert cv.main(["init", str(vault)]) == 0
    assert "already" in capsys.readouterr().out.lower()


def test_demo_runs_offline(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Demo complete" in out
    assert "BEGIN:VCALENDAR" in out


def test_completion_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["__complete"]) == 0
    lines = capsys.readouterr().out.split()
    assert "ingest" in lines and "due" in lines and "verify" in lines


def test_completion_prefix_filter(capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["__complete", "d"]) == 0
    out = set(capsys.readouterr().out.split())
    assert out == {"due", "demo"}


def test_completion_deal_ids(loaded_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(loaded_vault)
    assert cv.main(["__complete", "get", ""]) == 0
    out = capsys.readouterr().out
    assert "acme-corporation/master-services-agreement" in out


def test_no_color_env_strips_ansi(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cv.main(["list", "--vault", str(loaded_vault)])
    assert "\033[" not in capsys.readouterr().out


def test_version_matches_pyproject() -> None:
    import re
    from conftest import REPO_ROOT
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert m is not None and m.group(1) == cv.__version__, "pyproject version must match __version__"


def test_global_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["--help"])
    assert rc == 0
    assert "contract-vault" in capsys.readouterr().out


def test_catalog_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["--catalog", "json"]) == 0
    cat = json.loads(capsys.readouterr().out)
    assert {"name", "bin", "version", "description", "commands", "exitCodes"} <= set(cat)
    assert cat["name"] == "contract-vault" and cat["bin"] == "contract-vault"
    assert cat["version"] == cv.__version__
    assert set(cat["exitCodes"]) == {"0", "1", "2"}
    for c in cat["commands"]:
        assert set(c) == {"name", "help", "flags"}
    # Drift guard: the catalog lists exactly the commands the parser accepts.
    parser = cv.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, cv.argparse._SubParsersAction))
    assert {c["name"] for c in cat["commands"]} == set(sub.choices)
    # A representative command's flags are present (incl. aliases).
    find = next(c for c in cat["commands"] if c["name"] == "find")
    flag_names = {f["name"] for f in find["flags"]} | {a for f in find["flags"] for a in f["aliases"]}
    assert {"--auto-renew", "--value-gt", "--currency"} <= flag_names


def test_catalog_defaults_to_json_and_rejects_other(capsys: pytest.CaptureFixture[str]) -> None:
    assert cv.main(["--catalog"]) == 0          # bare --catalog defaults to json
    json.loads(capsys.readouterr().out)
    assert cv.main(["--catalog", "yaml"]) == cv.EXIT_USAGE
