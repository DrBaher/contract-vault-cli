#!/usr/bin/env python3
"""Release helper for contract-vault (mirrors the contract-ops suite).

    python scripts/release.py X.Y.Z

Verifies the version is consistent across the module and pyproject, runs the gate
(mypy --strict, the test suite, a build), and creates an annotated local tag vX.Y.Z.
It does NOT push and does NOT publish -- those stay human-gated; the exact commands are
printed at the end.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+([abrc].*)?$")


def fail(msg: str) -> "None":
    print(f"release: error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(*cmd: str) -> None:
    print(f"$ {' '.join(cmd)}")
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        fail(f"command failed: {' '.join(cmd)}")


def read_module_version() -> str:
    text = (ROOT / "contract_vault_cli.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        fail("could not find __version__ in contract_vault_cli.py")
    assert m is not None
    return m.group(1)


def read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        fail("could not find version in pyproject.toml")
    assert m is not None
    return m.group(1)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        fail("usage: release.py X.Y.Z")
    version = argv[0]
    if not SEMVER.match(version):
        fail(f"not a semantic version: {version!r}")

    mod_v, proj_v = read_module_version(), read_pyproject_version()
    if not (version == mod_v == proj_v):
        fail(
            f"version mismatch: arg={version} __version__={mod_v} pyproject={proj_v}; "
            "bump all three first"
        )

    identity = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--format=%an <%ae>"],
        text=True, capture_output=True,
    ).stdout.strip()
    print(f"release: HEAD authored by {identity}")

    py = sys.executable
    run(py, "-m", "mypy", "--strict", "contract_vault_cli.py")
    run(py, "-m", "pytest", "-q")
    run(py, "-m", "build")

    tag = f"v{version}"
    existing = subprocess.run(
        ["git", "-C", str(ROOT), "tag", "--list", tag], text=True, capture_output=True
    ).stdout.strip()
    if existing:
        fail(f"tag {tag} already exists")
    run("git", "-C", str(ROOT), "tag", "-a", tag, "-m", f"contract-vault {version}")

    print("\nrelease: prepared", tag, "locally. Remaining (human-gated) steps:")
    print(f"  git push origin HEAD")
    print(f"  git push origin {tag}      # triggers PyPI Trusted Publishing (publish.yml)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
