# Changelog

All notable changes to **contract-vault** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Schema changes are
semver-meaningful: backward-incompatible record/output changes require a major bump;
new optional fields are minor additions.

## [0.1.6] - 2026-05-22

### Added
- **`export` command** — emit the register as **csv** (default; opens in any spreadsheet),
  **md** (a shareable report with a value summary), or **json**. One flat row per deal:
  counterparty, dates, auto-renew, notice, renewal deadline, governing law, value
  (amount + currency), next upcoming obligation, needs-review count, vaulted. Filter with
  `--expiring-before DATE` and `--needs-review`. Composable:
  `contract-vault export > portfolio.csv`. Deterministic, stdlib `csv` only.

## [0.1.5] - 2026-05-22

### Added — the human-review workflow (close the "verify, not trust" loop)
- **`accept <deal> <field> [--value V]`** — mark a reviewed field as human-verified
  (`source=manual`, `confidence=1.0`), optionally overriding its value. Supports the scalar
  fields (`value`, `governing_law`, `effective_date`, `expiration_date`, `term.length`,
  `term.auto_renew`, `term.notice_period_days`) and `parties[i]`. Values are typed/parsed
  per field; changing a date/term field **recomputes the deadline calendar**
  (renewal_window + expiration/renewal_notice). Commits to the vault. Never calls an LLM.
  Accepted fields drop out of `review`.
- **`review --strict`** — exit 1 when any field needs review, for CI gating (like `verify`).
- record schema: optional `provenance.last_reviewed_at` timestamp, set by `accept`.

## [0.1.4] - 2026-05-22

### Added — surfacing provenance ("verify, not trust")
- **`review` command** — a deterministic worklist of fields that are unidentified
  (`source=none`), LLM-derived (`source=llm`), or low-confidence (`< --threshold`,
  default 0.6), across the whole vault. Pure inspection of stored provenance; **it never
  calls an LLM**. To *improve* flagged fields, re-ingest with `ingest --llm`, which
  delegates the LLM work to extract-cli (where clause/field identification lives).
- **`find --needs-review`** — filter the register to deals that have review flags.
- **`ingest --why`** now reports a per-record provenance breakdown
  (deterministic / llm / unidentified field counts, `llm_used`, needs-review count).
- **`stats`** gains `llm_used_count` and `needs_review_count`; **`get`** shows `llm_used`
  and a needs-review line.

Note: clause identification (incl. the LLM tier) remains an extract-cli responsibility;
contract-vault stores signed instances (parties/dates/obligations), not clauses, and keeps
the register/ics path fully deterministic with the LLM off.

## [0.1.3] - 2026-05-22

### Changed (parsing robustness — hardening the limitations surfaced by the stress test)
- **Locale-aware number parsing.** `value` amounts now understand European grouping:
  the decimal separator is inferred from context, so `1.000.000,50` → `1000000.50` and
  `1,234.56` → `1234.56` (US grouping like `$120,000` is unchanged).
- **Negative / accounting values.** A leading `-` or surrounding parentheses now yield a
  negative amount (`-$50,000` and `($50,000)` → `-50000`).
- **Multiple obligation dates.** An obligation clause naming several dates now produces
  one deadline per date (e.g. "pay 50% by A and the remainder by B" → two `due` items).
- **Stricter obligation date scanning.** Dates embedded in larger tokens are no longer
  misread as deadlines (e.g. the `2026-07-15` in `invoice-2026-07-15-A` is ignored).

## [0.1.2] - 2026-05-22

### Fixed
- **Long titles/counterparties no longer crash `ingest`.** Slugs are now capped to a
  path-component-safe length (80 chars), so a very long contract title can't exceed the
  filesystem's 255-byte directory-name limit (previously raised an uncaught
  `OSError: File name too long`). Surfaced by a 50-agreement stress test.
- Any `OSError` during a command (filesystem/git I/O) is now reported as a clean
  `error:` message with exit code 1 instead of a raw traceback.

## [0.1.1] - 2026-05-22

### Added
- `find --currency CCC` — filter deals by their value currency (case-insensitive;
  `none`/`unknown` matches unpriced/bare-number deals). Pairing it with `--value-gt`
  makes value thresholds **currency-aware** (e.g. `find --currency GBP --value-gt 1000000`),
  addressing the fact that `--value-gt` alone compares raw amounts across currencies.
  Deterministic and offline — no FX rates or conversion (currency is never guessed).

### Changed
- `find` human output now shows each deal's value + currency alongside expiration and
  governing law.

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

[0.1.6]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.6
[0.1.5]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.5
[0.1.4]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.4
[0.1.3]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.3
[0.1.2]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.2
[0.1.1]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.1
[0.1.0]: https://github.com/DrBaher/contract-vault-cli/releases/tag/v0.1.0
