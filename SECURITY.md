# Security

contract-vault stores **executed contracts** — sensitive legal and financial data — so
this document states the security model honestly: what the tool guarantees, what it does
**not**, and how to operate it safely.

## Reporting a vulnerability

Please report privately — do **not** open a public issue. Use GitHub's "Report a
vulnerability" (Security advisories) on the repo, or email **Drbaher@gmail.com**. We aim to
acknowledge within a few days. The latest released minor version receives fixes.

## What the tool guarantees

- **No network, no telemetry, no background process.** contract-vault never transmits your
  data anywhere; there is no code path that phones home. It does not auto-push to git.
- **No in-tool LLM.** Extraction (and any LLM use) is delegated to `extract-cli`, opt-in via
  `ingest --llm`. The register / calendar / reminder logic is fully deterministic.
- **Zero runtime dependencies.** The installed package pulls in nothing — a minimal
  supply-chain surface. Releases are published via PyPI **Trusted Publishing** (OIDC, no
  long-lived token).
- **No dangerous parsing/execution.** JSON only — no `eval`, `exec`, `pickle`, or
  `yaml.load`. All subprocess calls (`git`, `extract`) are list-form (no shell), and git
  paths are passed after `--`.
- **Path containment.** Counterparty/title are slugified to `[a-z0-9-]`, so values like
  `../../etc/passwd` cannot escape the vault.
- **Untrusted input is not trusted with your filesystem.** When you ingest piped or `.json`
  extract output, `document.source_path` is treated as metadata only — contract-vault will
  **not** copy an arbitrary local file into the vault. Only `ingest <doc>` (a file *you*
  chose on the command line) vaults the source document.
- **Restrictive permissions (POSIX).** The vault directory is created `0700` and
  `record.json` / vaulted source files `0600` (owner-only). On Windows, rely on ACLs.
- **Integrity + audit.** `verify` recomputes each vaulted source's SHA-256 and checks the
  git tree is clean; every change (ingest, accept, obligation, config) is an auditable git
  commit (`history <deal>`).

## What the tool does NOT do (your responsibility)

- **The working copy is plaintext.** record.json and source documents on disk are not
  encrypted in the working tree. Protect them with:
  1. **Full-disk encryption** (FileVault / LUKS / BitLocker) — the primary control.
  2. The default **owner-only permissions** (above) — keep the vault off shared accounts.
  3. A **private git remote** — never push a contract vault to a public repo.
- **Commits are not cryptographically signed.** Integrity is covered by SHA-256 + git;
  for authorship authenticity, enable `git config commit.gpgsign true` in the vault.

## Optional: encryption at rest (git-crypt)

For an encrypted-at-rest vault (so what's committed/pushed is ciphertext), initialize with:

```bash
contract-vault init ~/contracts --encrypt     # requires `git-crypt` on PATH
git-crypt export-key ~/contract-vault.key      # BACK THIS UP — without it, history is unrecoverable
```

This marks `record.json` and `source.*` as git-crypt-encrypted via `.gitattributes`. The
**working tree stays decrypted** (so the CLI works normally) — git-crypt protects the repo
and any remote, *not* the local working copy, so keep full-disk encryption on too. Existing
plaintext history is not retroactively encrypted; enable `--encrypt` on a fresh vault.

## Hardening checklist

- [ ] Vault on a full-disk-encrypted volume
- [ ] Private git remote only
- [ ] `contract-vault init --encrypt` (git-crypt) if the repo/remote may be exposed; key backed up
- [ ] `git config commit.gpgsign true` if you need signed history
- [ ] `contract-vault verify` in CI/periodically to detect tampering
