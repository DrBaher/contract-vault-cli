# Changelog

All notable changes to **contract-vault** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Schema changes are
semver-meaningful: backward-incompatible record/output changes require a major bump;
new optional fields are minor additions.

## [0.1.0] - 2026-05-22

Initial release: the post-signature management layer of the contract-ops CLI suite.

### Added
- Git-backed, single-file (`contract_vault_cli.py`), stdlib-only CLI with zero runtime
  dependencies.
- Subcommands: `init`, `ingest` (+ stdin `-`), `list`, `get`/`show`, `find`/`search`,
  `due`/`obligations`, `stats`, `verify`, `demo`, and a hidden `__complete`.
- `ingest` consumes [extract-cli](https://github.com/DrBaher/extract-cli) output: it
  shells out to `extract <file> --json` when on PATH, accepts piped JSON via `-`, and
  ingests `.json` extract payloads directly (offline path). Extraction is never
  reimplemented; `--llm` is forwarded to `extract` (delegated, opt-in).
- Deterministic obligation engine: `expiration`, `renewal_notice` (notice deadline =
  expiration − `notice_period_days`), and date-scanned `obligation` items are computed at
  ingest and stored. `due` is a date-filtered view emitting a valid RFC 5545 `.ics`
  (stdlib-built, folded, escaped, with a `VALARM` per event), JSON (the reminder
  manifest), or a table.
- JSON Schema 2020-12 contracts in `docs/spec/`:
  [`contract-record.schema.json`](docs/spec/contract-record.schema.json) and
  [`obligations-output.schema.json`](docs/spec/obligations-output.schema.json), registered
  in [`docs/INTEROP.md`](docs/INTEROP.md).
- Suite I/O conventions: `--json`, `--why`, `-q/--quiet/--silent`, `--no-color`
  (honoring `NO_COLOR`/`FORCE_COLOR`), `-V/--version`; exit codes `0`/`1`/`2`.
- CI matrix (Ubuntu × macOS × Py 3.9–3.12) + `mypy --strict` typecheck + build-smoke;
  PyPI Trusted Publishing on `v*` tags.

### Design decisions (documented for the suite)
- **Provenance / "verify, not trust".** Top-level fields stay flat and queryable; a
  `field_meta` map records the `{confidence, source}` of every field, and obligations /
  parties carry per-item `source` + `confidence`. `source` extends extract's
  `{deterministic, llm, none}` with `manual` for human edits.
- **`value` is normalized** to `{raw, amount, currency}`. `amount`/`currency` are parsed
  deterministically (`$`, `£`, `€`, `¥`, ISO codes, `k`/`m`/`b` suffixes); a bare number
  has no currency and aggregates under `(unknown)` in `stats` (currency is never guessed).
- **`renewal_window`** is derived as `{deadline, expiration}` from `expiration_date` and
  `notice_period_days`.
- **Idempotency.** Re-ingesting a document with a known source `sha256` is a no-op (no new
  commit). When a local source file is vaulted, its `sha256` is recomputed from the bytes
  actually stored rather than trusted from extract.
- **Completion.** contract-vault uses the hidden-`__complete` style of its template-vault
  sibling (extract-cli uses an `extract completion <shell>` subcommand instead — both are
  valid across the suite).
- **Commit identity.** Vault commits use the user's configured git identity (or
  `GIT_AUTHOR_*`), falling back to a neutral `contract-vault` bot so `ingest`/`demo`
  succeed in any environment — independent of this project's own source-commit identity.
- **Testing.** A small embedded JSON-Schema-2020-12 validator keeps schema-conformance
  tests dependency-free; fixtures vendor sample extract JSON so the suite runs offline and
  without extract-cli installed. Property tests use stdlib `random.Random(seed)`.

[0.1.0]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.0
