# contract-vault

**Never miss a contract renewal, notice deadline, or obligation.** A git-backed
register for your **signed** contracts: register an executed deal, then search the
portfolio and surface **renewal / notice / payment deadlines** as a calendar.
Local-first, single-file, stdlib-only — no DB, no daemon, no SaaS.

`obligations` is a *view* over the register, not a separate tool.

- **Stdlib only.** Zero runtime dependencies; fully functional with no extras.
- **Git-backed, single file.** No DB, no daemon. The vault is a plain git repo.
- **Deterministic.** The register, `.ics` calendar, and reminders work fully with the
  LLM **off** and no network.
- **Verify, not trust.** Every field carries its `source` (`deterministic` | `llm` |
  `manual` | `none`) and a confidence, surfaced everywhere.

> **Part of the contract-ops suite — optional.** contract-vault stands on its own,
> but it also composes with the [contract-ops CLI suite](https://github.com/DrBaher):
> it can ingest [`extract-cli`](https://github.com/DrBaher/extract-cli) output (so any
> LLM/parsing need is delegated to that extraction step, never reimplemented here) and
> shares the suite's agent conventions — sitting at the manage-out end of
> `extract → … → sign → contract-vault`. It's the post-signature sibling of
> [`template-vault`](https://github.com/DrBaher/template-vault-CLI): template-vault
> stores blank templates, contract-vault stores the signed instances.

---

## Run this

```bash
pipx run contract-vault demo     # zero-config: register two sample contracts → renewals calendar
# or, installed:  pip install contract-vault && contract-vault demo
```

That runs the full `ingest → find → due` flow on bundled fixtures (no extract-cli,
no LLM, no network) and prints a renewals/notice calendar. Point it at your own
signed contract with `extract contract.pdf | contract-vault ingest -`.

## Where to go next

- **New here?** Keep reading — [Quick start](#quick-start) and [The vault model](#the-vault-model).
- **Driving it from an agent?** See [`AGENTS.md`](AGENTS.md) and call
  `contract-vault --catalog json` at startup to discover commands/flags. Stored
  records follow [`docs/spec/contract-record.schema.json`](docs/spec/contract-record.schema.json).
- **Wiring it into the pipeline?** [`docs/INTEROP.md`](docs/INTEROP.md) — it ingests
  extract-cli output and keeps the suite's `confidence`/`source` envelope.
- **Contributing?** [`CONTRIBUTING.md`](CONTRIBUTING.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

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
| `due` / `obligations` | Project upcoming actions. `--within 30d\|60d\|90d`, `--format ics\|json\|table`, `--status open\|done\|waived\|all` (default open), `--type`, `--owner`. Emits valid RFC 5545 `.ics`. |
| `obligation <deal> <id>` | Track an obligation: `--status open\|done\|waived`, `--owner NAME`, `--recurrence weekly\|monthly\|quarterly\|semiannual\|annual`, `--reminders 30,7`. Completed obligations drop off `due`; recurring ones expand into occurrences. |
| `stats` | Portfolio stats: count, total value, expiring soon, by counterparty / governing law. |
| `export` | Export the register as `csv` / `md` / `json` (`--expiring-before`, `--needs-review`). For spreadsheets & reports. |
| `verify` | Integrity check: source `sha256` matches + git tree clean. |
| `review` | Deterministic worklist of fields that are unidentified / LLM-derived / low-confidence (`--threshold`; `--strict` exits 1 for CI). Never calls an LLM. |
| `accept <deal> <field>` | Mark a reviewed field as human-verified (`source=manual`), optionally `--value` to correct it; recomputes the calendar for date/term changes. Bulk via `accept --from FILE`. |
| `risk` / `at-risk` | Renewal exposure: **missed** notice deadlines (CRITICAL if auto-renewing), imminent notices, and expirations (`--within`, `--strict`). |
| `remind` | The reminder digest: obligations whose reminder window is open right now (honors per-obligation `--reminders`). For cron/agents (`--strict`, `--format`). |
| `config reminders` | Set **corpus-wide** default reminder lead-times per type (`--type … --set 60,30,7`), `--show`/`--clear`. Applies to every contract; overridable per obligation. |
| `history <deal>` | The deal's git history (ingest + each accept). |
| `demo` | Run the full flow on bundled fixtures (no extract-cli, no LLM). |

### Global I/O conventions (shared across the suite)

- `--catalog json` — print the machine-readable command/flag catalog and exit (the suite discovery contract; agents call this at startup, before any subcommand).
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

# Hand the portfolio to finance/legal as a spreadsheet or a report.
contract-vault export > portfolio.csv
contract-vault export --format md --expiring-before 2027-01-01 > renewals-report.md

# Pipe the machine-readable register into jq.
contract-vault find --expiring-before 2026-01-01 --json | jq '.deals[].id'

# A reminder manifest is just the JSON projection (days_until + suggested lead_days).
contract-vault due --within 365d --format json > reminders.json
```

`--format json` (or `--json`) for `due`/`obligations` doubles as a **reminder manifest**:
each row carries `days_until` and a suggested `lead_days`. The `.ics` output is the same
data rendered as RFC 5545, with a `VALARM` per event.

---

## Notifications & reminders

contract-vault has **no daemon and sends no notifications itself** — it computes deadlines
and lead-times deterministically; *delivery is your environment's job*. There are three
ways to actually get notified:

1. **Calendar (zero code).** `contract-vault due --within 365d --format ics > contracts.ics`
   and subscribe to it. Each event carries `VALARM`s (`TRIGGER:-P30D`, …) from the
   per-obligation `--reminders`, so your calendar app fires the alerts natively.
2. **Cron / CI gate.** Run on a schedule and act on the **exit code** or piped JSON —
   e.g. `contract-vault remind --strict || mail-me` (exit 1 when something is due), or
   `contract-vault risk --within 30d --strict`.
3. **An agent (Claude, etc.).** The agent polls the CLI and notifies through its own
   channel. contract-vault is built for this (`--catalog json`, `--json`, exit codes,
   [`AGENTS.md`](AGENTS.md)).

The **`remind`** command is the turnkey digest — "what should I be notified about *now*":

```bash
contract-vault remind                 # obligations whose reminder window is open today
contract-vault remind --strict --json # exit 1 + JSON if anything is due (cron/agent loop)
```

It returns each obligation only while `0 ≤ days_until ≤ its longest reminder lead`, honoring
`obligation <deal> <id> --reminders 30,7`. A daily `remind --json` is exactly the set a
notifier should send.

Set lead-times **once for the whole corpus** (instead of per obligation) with a vault policy:
```bash
contract-vault config reminders --type expiration --set 60,30,7   # applies to every contract
contract-vault config reminders --type obligation  --set 14,7
```
Resolution per obligation: per-obligation `--reminders` → vault default for its type →
vault `default` catch-all → built-in default.

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

## LLM (opt-in, delegated) and "verify, not trust"

contract-vault never calls an LLM itself. Identification of clauses and fields — including
the LLM fallback for anything the deterministic tiers miss — is the job of
[`extract-cli`](https://github.com/DrBaher/extract-cli). `ingest --llm` simply forwards
`--llm` to the `extract` step, which runs that LLM tier; shared config is looked up at
`~/.config/contract-ops/llm.json` first, then `./config/llm.json`
(see [`config/llm.json.example`](config/llm.json.example)). The register / `due` / `.ics`
paths stay fully deterministic and work with the LLM off.

Every field contract-vault stores carries its `source`
(`deterministic` | `llm` | `manual` | `none`) and a `confidence`. `review` surfaces the
ones worth a human look — without calling a model:

```bash
contract-vault review                       # unidentified / llm-derived / low-confidence fields
contract-vault review --strict               # exit 1 if anything needs review (CI gate)
contract-vault find --needs-review --json    # which deals need attention
extract deal.pdf --json --llm | contract-vault ingest -   # improve them at extraction time

# After a human checks a contract, record the verdict (-> source=manual, drops out of review):
contract-vault accept acme-corp/msa governing_law --value Delaware
contract-vault accept acme-corp/msa expiration_date --value 2027-01-31   # recomputes the calendar
contract-vault accept acme-corp/msa value            # accept the current value as verified
```

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
