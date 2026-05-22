"""Coverage for shell-out integration, color, --why, LLM config, and edge output."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List

import pytest

import contract_vault_cli as cv
from conftest import extract_fixture


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_extract_shells_out(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "deal.pdf"
    doc.write_bytes(b"%PDF fake")
    payload = json.dumps(extract_fixture("acme-msa"))
    captured: List[List[str]] = []
    real_run = cv.subprocess.run

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd and cmd[0] == "extract":
            captured.append(cmd)
            return _FakeProc(0, stdout=payload)
        return real_run(cmd, **kw)  # let real git commits happen

    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/extract")
    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    rc = cv.main(["ingest", str(doc), "--vault", str(empty_vault)])
    assert rc == 0
    assert captured[0][:1] == ["extract"] and "--json" in captured[0]
    assert (empty_vault / "acme-corporation/master-services-agreement" / cv.RECORD_FILENAME).is_file()


def test_ingest_llm_is_forwarded(empty_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "deal.pdf"
    doc.write_bytes(b"%PDF fake")
    captured: List[List[str]] = []
    real_run = cv.subprocess.run

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd and cmd[0] == "extract":
            captured.append(cmd)
            return _FakeProc(0, stdout=json.dumps(extract_fixture("acme-msa")))
        return real_run(cmd, **kw)

    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/extract")
    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    assert cv.main(["ingest", str(doc), "--llm", "--vault", str(empty_vault)]) == 0
    assert "--llm" in captured[0]


def test_run_extract_nonzero_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/extract")
    monkeypatch.setattr(cv.subprocess, "run", lambda *a, **k: _FakeProc(3, stderr="boom"))
    with pytest.raises(cv.VaultError):
        cv.run_extract(tmp_path / "x.pdf", use_llm=False)


def test_run_extract_bad_json_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/extract")
    monkeypatch.setattr(cv.subprocess, "run", lambda *a, **k: _FakeProc(0, stdout="not json"))
    with pytest.raises(cv.VaultError):
        cv.run_extract(tmp_path / "x.pdf", use_llm=False)


def test_color_helpers_with_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    cv._NO_COLOR = False
    assert "\033[" in cv._green("ok")
    assert "\033[" in cv._bold("hi")


def test_no_color_flag_beats_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    cv._NO_COLOR = True
    assert "\033[" not in cv._green("ok")


def test_find_llm_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert cv.find_llm_config() is None
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "llm.json").write_text("{}")
    found = cv.find_llm_config()
    assert found is not None and found.exists()


def test_why_goes_to_stderr(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["find", "--why", "--counterparty", "acme", "--vault", str(loaded_vault)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[why] find" in err


def test_get_human_output_full_record(loaded_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["get", "--vault", str(loaded_vault), "acme-corporation/master-services-agreement"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "renewal window" in out
    assert "obligations:" in out
    assert "provenance" in out


def test_list_empty(empty_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["list", "--vault", str(empty_vault)])
    assert rc == 0
    assert "no deals yet" in capsys.readouterr().out.lower()


def test_quiet_suppresses_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["init", str(tmp_path / "v"), "-q"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert (tmp_path / "v" / cv.VAULT_CONFIG_NAME).is_file()


def test_resolve_vault_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cv.main(["list", "--vault", str(tmp_path / "not-a-vault")])
    assert rc == cv.EXIT_FAIL
    assert "error:" in capsys.readouterr().err


def test_vault_discovery_walks_up(loaded_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    deep = loaded_vault / "acme-corporation" / "master-services-agreement"
    monkeypatch.chdir(deep)
    rc = cv.main(["list", "--json"])  # no --vault: must discover by walking up
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["count"] == 4


def test_env_var_vault(loaded_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CONTRACT_VAULT_DIR", str(loaded_vault))
    monkeypatch.chdir("/")
    rc = cv.main(["stats", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["count"] == 4
