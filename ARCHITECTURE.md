# Architecture

contract-vault is the **manage-out** end of the contract-ops suite. It mirrors
[template-vault](https://github.com/DrBaher/template-vault-CLI)'s shape — a single
stdlib-only Python file managing a git-backed vault — but stores **signed instances**
instead of blanks.

```
extract → draft → review → compare → convert → sign → contract-vault
```

## Principles

1. **Stdlib only, single file.** `contract_vault_cli.py` has zero runtime dependencies.
   Heavy lifting (document parsing) is delegated to `extract-cli`, never reimplemented.
2. **Deterministic core.** The register, `.ics` calendar, and reminders are pure functions
   of stored data and work with the LLM **off**. LLM use is opt-in and delegated to the
   `extract` step (`ingest --llm` simply forwards `--llm`).
3. **Git is the database.** No DB, no daemon. A vault is a git repo; every `ingest` is a
   commit. State is plain JSON on disk, diffable and greppable.
4. **Verify, not trust.** Every value records where it came from (`source`) and a
   `confidence`, propagated from extract and surfaced in `field_meta`, obligations, and
   output.

## File layout (the module)

`contract_vault_cli.py` is organized into labelled sections:

| Section | Responsibility |
|---|---|
| Constants / Errors | exit codes, source markers, `VaultError`/`NotFoundError`/`UsageError`. |
| Output helpers | color (`NO_COLOR`/`FORCE_COLOR`), `--json`/`_emit_json`, `--why`, UTF-8 stream config. |
| Small utilities | `slugify`, sha256, `parse_date`/`scan_date`, `parse_money`. |
| Git operations | `_git` subprocess wrapper, identity-safe `git_commit`. |
| Vault discovery & records | `resolve_vault` (walk-up / `$CONTRACT_VAULT_DIR`), `load_all_records`, `find_deal`. |
| extract → record mapping | `validate_extract_payload`, `build_record`, obligation derivation. |
| due projection | `parse_within`, `upcoming_obligations` (the engine behind `due`). |
| RFC 5545 | `build_ics` + folding/escaping helpers. |
| Commands | one `cmd_*` per subcommand. |
| Completion | hidden `__complete`. |
| Argument parsing / entry | `build_parser`, `main`. |

## The ingest pipeline

```
ingest <file>            ingest -                 ingest payload.json
   │  shell out             │ read stdin             │ read file
   ▼                        ▼                        ▼
extract <file> --json   piped JSON               extract JSON
   └──────────────┬─────────────────────┬──────────┘
                  ▼                      ▼
        validate_extract_payload   (input contract: extract-output.schema.json)
                  ▼
            build_record            (deterministic mapping → record.json)
                  ▼
   store under <counterparty>/<name>/, vault source doc (recompute sha256),
   idempotency check on sha256, git commit.
```

`build_record` maps extract's fields onto the record, computes `renewal_window`, derives
obligations (`expiration`, `renewal_notice`, date-scanned `obligation`s), and records each
field's `{confidence, source}` in `field_meta`. It is a pure function — every test that
needs a record calls it directly, no extract-cli required.

## Obligations & `due`

Obligations are computed **once at ingest** and stored on the record. `due`/`obligations`
is a *view*: `upcoming_obligations` filters stored dated obligations into the window
`[as_of, as_of + within]`, sorted by date. The same rows render three ways:

- `--format ics` → RFC 5545 `VCALENDAR` (CRLF, ≤75-octet folding that never splits a
  multibyte sequence, TEXT escaping, all-day `VALUE=DATE` events, a `VALARM` per event).
- `--format json` (or `--json`) → the machine-readable **reminder manifest**
  (`days_until` + suggested `lead_days`); conforms to `obligations-output.schema.json`.
- `--format table` → human output.

## Schemas & conformance

`docs/spec/*.schema.json` are JSON Schema 2020-12. Tests validate against them with a tiny
embedded validator (`tests/conftest.py`) so the suite has **no third-party test
dependency** and runs offline. The extract input contract is vendored at
`tests/vendor/extract-output.schema.json` and refreshed from upstream `HEAD`.

## Git & identity

`git_commit` stages, checks for a real change, and commits with an explicit
`-c user.name -c user.email`. The identity comes from `GIT_AUTHOR_*` / the repo's git
config when present, otherwise a neutral `contract-vault` bot — so ingest works in fresh
CI environments. This runtime behavior is unrelated to the identity used for commits to
*this project's* source repository.

## Testing strategy

- Per-subcommand unit tests (`tests/test_*.py`).
- Schema-conformance tests for inputs (extract fixtures) and outputs (records, `due` JSON).
- An `.ics`-validity test (unfold/parse + folding/escaping/all-day checks).
- Property-based invariants with stdlib `random.Random(seed)` (no hypothesis): record
  invariants, `parse_date`/`parse_money` round-trips, `due` window monotonicity, ingest
  idempotency.
- Real-fixture corpus: pre-built records under `tests/fixtures/records/`.
