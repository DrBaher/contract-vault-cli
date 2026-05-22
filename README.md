# contract-vault

**The post-signature management layer of the [contract-ops CLI suite](https://github.com/DrBaher).**

```
extract → draft → review → compare → convert → sign → **contract-vault**
   (entry)            (core authoring / negotiation)        (manage-out)
```

[`template-vault`](https://github.com/DrBaher/template-vault-CLI) stores **blanks**
(clauses, variables, composition). **contract-vault** is its post-signature sibling: it
stores **signed instances** — the parties, dates, money, and obligations of executed
deals — in the same git-backed, single-file, subcommand-rich shape. It is where executed
contracts are **registered, searched**, and where **renewal / notice / payment deadlines**
are surfaced as a calendar.

`obligations` is a *view* over the register, not a separate tool.

- **Stdlib only.** Zero runtime dependencies; fully functional with no extras.
- **Git-backed, single file.** No DB, no daemon. The vault is a plain git repo.
- **Deterministic.** The register, `.ics` calendar, and reminders work fully with the
  LLM **off**. Any LLM need is *delegated* to the `extract` step you call — never
  reimplemented here, never on a hot path.
- **Verify, not trust.** Every field carries its `source` (`deterministic` | `llm` |
  `manual` | `none`) and a confidence, propagated from extract and surfaced everywhere.

---

## Install

```bash
pip install contract-vault            # zero dependencies, fully functional
```

Extraction itself is delegated to [`extract-cli`](https://github.com/DrBaher/extract-cli).
contract-vault works **without** it (piped JSON / `.json` input), but for one-shot
ingestion of real documents install it with the backends you need:

```bash
pip install extract-cli               # text / markdown
pip install "extract-cli[docx]"       # + Word
pip install "extract-cli[pdf]"        # + PDF
# convenience extras that pull extract-cli in via contract-vault:
pip install "contract-vault[pdf]"
```

Requires Python 3.9+.

---

## Quick start

```bash
contract-vault init ~/contracts            # create a git-backed vault
contract-vault demo                        # full ingest→find→due flow on bundled fixtures

# Ingest an executed contract (shells out to `extract` if it is on PATH):
contract-vault ingest ~/Downloads/acme-msa.pdf --vault ~/contracts

# ...or compose with extract explicitly (works even if extract is elsewhere):
extract ~/Downloads/acme-msa.pdf --json | contract-vault ingest - --vault ~/contracts

contract-vault list --vault ~/contracts
contract-vault find --auto-renew --value-gt 100000 --vault ~/contracts
contract-vault due --within 90d --format ics --vault ~/contracts > renewals.ics
```

> The `--vault` flag is optional: contract-vault discovers the vault by walking up
> from the current directory (like git), or reads `$CONTRACT_VAULT_DIR`.

---

## The vault model

A vault is a git repository laid out exactly like template-vault, one directory per deal:

```
~/contracts/
├── .contract-vault.json                       # vault config (kind, schema_version)
├── acme-corporation/
│   └── master-services-agreement/
│       ├── record.json                         # the structured deal record
│       └── source.pdf                          # the executed document (when vaulted)
└── initech-inc/
    └── mutual-non-disclosure-agreement/
        └── record.json
```

Each `record.json` conforms to
[`docs/spec/contract-record.schema.json`](docs/spec/contract-record.schema.json) and
carries: `parties[]`, `effective_date`, `expiration_date`,
`term{length, auto_renew, notice_period_days, renewal_window}`, `governing_law`, `value`,
`status`, `signed_on`, `source{path, sha256, format, vaulted}`,
`obligations[]{type, due, description, source, confidence}`,
`provenance{from_extract, extractor_version, ...}`, and a `field_meta` map recording the
**source + confidence of every field**. Re-ingesting the same document (same `sha256`)
is idempotent.

---

## Commands

| Command | What it does |
|---|---|
| `init [path]` | Create / initialize an executed-contract vault (a git repo). |
| `ingest <file>` | Run `extract <file> --json` (if on PATH) and store + commit the record. |
| `ingest -` | Read piped extract JSON from stdin (`extract f --json \| contract-vault ingest -`). |
| `list` | List stored deals. |
| `get <id>` / `show <id>` | Print one record (by path, leaf name, or unique prefix). |
| `find` / `search` | Query by `--counterparty`, `--governing-law`, `--currency CCC`, `--expiring-before DATE`, `--value-gt N`, `--auto-renew`, or full-text. Pair `--currency` with `--value-gt` for currency-aware thresholds. |
| `due` / `obligations` | Project upcoming actions. `--within 30d\|60d\|90d`, `--format ics\|json\|table`. Emits valid RFC 5545 `.ics`. |
| `stats` | Portfolio stats: count, total value, expiring soon, by counterparty / governing law. |
| `verify` | Integrity check: source `sha256` matches + git tree clean. |
| `demo` | Run the full flow on bundled fixtures (no extract-cli, no LLM). |

### Global I/O conventions (shared across the suite)

- `--json` — machine-readable JSON on **stdout** (opt-in; default output is human).
- `--why` — structured explanation on **stderr** (`[why] <header>` + indented lines).
- `-q` / `--quiet` / `--silent` — suppress non-error output.
- `--no-color` — disable ANSI color (also honors `NO_COLOR` and `FORCE_COLOR`).
- `-V` / `--version`, `-h` / `--help`.
- **Exit codes:** `0` ok, `1` failure / findings (e.g. `verify` mismatch), `2` bad usage.

---

## Composability

```bash
# One-shot: extract a PDF and register it.
extract deal.pdf --json | contract-vault ingest -

# Export the next quarter of renewals/notices to any calendar app.
contract-vault due --within 90d --format ics > renewals.ics

# Pipe the machine-readable register into jq.
contract-vault find --expiring-before 2026-01-01 --json | jq '.deals[].id'

# A reminder manifest is just the JSON projection (days_until + suggested lead_days).
contract-vault due --within 365d --format json > reminders.json
```

`--format json` (or `--json`) for `due`/`obligations` doubles as a **reminder manifest**:
each row carries `days_until` and a suggested `lead_days`. The `.ics` output is the same
data rendered as RFC 5545, with a `VALARM` per event.

---

## Shell completion

contract-vault ships a hidden `__complete` subcommand (matching the template-vault
sibling). Register it for bash with:

```bash
_contract_vault() {
  COMPREPLY=( $(contract-vault __complete "${COMP_WORDS[@]:1}") )
}
complete -F _contract_vault contract-vault
```

It completes subcommands, deal ids (for `get`/`show`/`verify`), and query
flags/counterparties/governing-laws (for `find`/`search`). (extract-cli ships an
`extract completion <shell>` subcommand instead; both styles are valid across the suite —
contract-vault uses the hidden-subcommand style.)

---

## LLM (opt-in, delegated)

contract-vault never calls an LLM itself. `ingest --llm` simply forwards `--llm` to the
`extract` step. Shared config is looked up at `~/.config/contract-ops/llm.json` first,
then `./config/llm.json`. See [`config/llm.json.example`](config/llm.json.example).

---

## Interop

The cross-CLI contracts live under [`docs/spec/`](docs/spec/) as JSON Schema 2020-12 and
are registered in [`docs/INTEROP.md`](docs/INTEROP.md):

- **Input:** [`extract-cli` output](https://github.com/DrBaher/extract-cli) → consumed by `ingest`.
- **Output:** [`contract-record.schema.json`](docs/spec/contract-record.schema.json),
  [`obligations-output.schema.json`](docs/spec/obligations-output.schema.json).

---

## Development

```bash
make install      # editable install with dev extras
make test         # full test suite
make typecheck    # mypy --strict
make coverage     # tests under coverage + report
make build        # wheel + sdist
make smoke        # build, install the wheel in a clean venv, run it
make spec-check   # validate fixtures + outputs against docs/spec schemas (offline)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © DrBaher
