"""Security hardening tests (0.5.0): restrictive perms, untrusted source_path, encryption."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import contract_vault_cli as cv
from conftest import EXTRACT_FIXTURES, extract_fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission/shim semantics")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@posix_only
def test_vault_dir_is_owner_only(empty_vault: Path) -> None:
    assert _mode(empty_vault) == 0o700


@posix_only
def test_record_and_config_are_owner_only(empty_vault: Path) -> None:
    cv.main(["ingest", str(EXTRACT_FIXTURES / "acme-msa.json"), "--vault", str(empty_vault)])
    rec = next(empty_vault.rglob(cv.RECORD_FILENAME))
    assert _mode(rec) == 0o600
    assert _mode(empty_vault / cv.VAULT_CONFIG_NAME) == 0o600
    assert _mode(rec.parent) == 0o700   # deal dir


def test_untrusted_json_does_not_copy_arbitrary_source_file(empty_vault: Path, tmp_path: Path) -> None:
    # A crafted .json payload points source_path at a sensitive local file. It must NOT be
    # copied into the vault (only kept as metadata; vaulted=False).
    secret = tmp_path / "id_rsa"
    secret.write_text("SUPER-SECRET-KEY-MATERIAL")
    payload = extract_fixture("acme-msa")
    payload["document"]["source_path"] = str(secret)
    payload["document"]["sha256"] = "e" * 64
    crafted = tmp_path / "payload.json"
    crafted.write_text(json.dumps(payload))

    assert cv.main(["ingest", str(crafted), "--vault", str(empty_vault)]) == 0
    deal_dir = next(empty_vault.rglob(cv.RECORD_FILENAME)).parent
    assert not list(deal_dir.glob("source.*"))                       # no source file copied in
    rec = json.loads((deal_dir / cv.RECORD_FILENAME).read_text())
    assert rec["source"]["vaulted"] is False
    # the secret's *contents* are nowhere in the vault
    blob = "".join(p.read_text(errors="ignore") for p in empty_vault.rglob("*") if p.is_file() and ".git" not in p.parts)
    assert "SUPER-SECRET-KEY-MATERIAL" not in blob


def test_user_chosen_document_is_still_vaulted(empty_vault: Path, tmp_path: Path) -> None:
    # Regression: `ingest <doc>` (a file the user chose) DOES still vault the source.
    doc = tmp_path / "signed.pdf"
    doc.write_bytes(b"real signed bytes")
    payload = extract_fixture("acme-msa")
    deal, _ = cv.store_record(empty_vault, payload, counterparty_override=None, name_override=None, local_source=doc)
    rec = json.loads((empty_vault / deal / cv.RECORD_FILENAME).read_text())
    assert rec["source"]["vaulted"] is True
    if sys.platform != "win32":
        assert _mode(empty_vault / deal / rec["source"]["path"]) == 0o600


def test_encrypt_requires_git_crypt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cv.shutil, "which", lambda name: None if name == "git-crypt" else "/usr/bin/" + name)
    rc = cv.main(["init", str(tmp_path / "v"), "--encrypt"])
    assert rc == cv.EXIT_USAGE
    assert "git-crypt" in capsys.readouterr().err
    assert not (tmp_path / "v" / cv.VAULT_CONFIG_NAME).exists()      # vault NOT created (no false sense of security)


@posix_only
def test_encrypt_scaffolds_gitcrypt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fake `git-crypt` on PATH (exits 0) so we can test the scaffolding without real git-crypt.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git-crypt"
    shim.write_text("#!/usr/bin/env sh\nexit 0\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    vault = tmp_path / "vault"
    assert cv.main(["init", str(vault), "--encrypt"]) == 0
    attrs = (vault / ".gitattributes").read_text()
    assert "record.json filter=git-crypt" in attrs and "source.* filter=git-crypt" in attrs
    cfg = json.loads((vault / cv.VAULT_CONFIG_NAME).read_text())
    assert cfg["encrypted"] is True
