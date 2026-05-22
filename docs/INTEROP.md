# contract-vault interop

This document registers the cross-CLI data contracts contract-vault participates in, as
part of the [contract-ops CLI suite](https://github.com/DrBaher). All contracts are
**JSON Schema 2020-12** and live in [`docs/spec/`](spec/).

```
extract → draft → review → compare → convert → sign → contract-vault
```

## The extract → ingest contract (input)

`contract-vault ingest` consumes the structured JSON emitted by
[`extract-cli`](https://github.com/DrBaher/extract-cli) (`extract <file> --json`). That
payload is the **input contract**; its authoritative schema is:

- <https://raw.githubusercontent.com/DrBaher/extract-cli/HEAD/docs/spec/extract-output.schema.json>

A copy is vendored at [`tests/vendor/extract-output.schema.json`](../tests/vendor/extract-output.schema.json)
so tests validate ingest inputs offline. Top-level fields consumed: `document`, `parties`,
`dates`, `term`, `governing_law`, `clauses`, `defined_terms`, `value`, optional
`obligations`, and `_meta`. Every field carries a `confidence` and a `source` ∈
`{deterministic, llm, none}`; contract-vault **stores and propagates** these (adding
`manual` for human edits) and treats data as *verify, not trust*.
`_meta.extractor_version` is carried into the record's `provenance`.

How ingest acquires the payload:

| Invocation | Behavior |
|---|---|
| `contract-vault ingest <doc>` | If `extract` is on PATH, shell out to `extract <doc> --json`. |
| `contract-vault ingest <doc>` (no extract) | Clear error → `pip install extract-cli`. Extraction is never reimplemented. |
| `extract <doc> --json \| contract-vault ingest -` | Read piped JSON from stdin (always supported). |
| `contract-vault ingest payload.json` | A `.json` file that *is* extract output is ingested directly (offline). |

## Output contracts (produced)

| Schema | Produced by | Description |
|---|---|---|
| [`contract-record.schema.json`](spec/contract-record.schema.json) | `ingest` (stored `record.json`), `get --json` | A single executed-deal record. |
| [`obligations-output.schema.json`](spec/obligations-output.schema.json) | `due --format json`, `obligations --json` | Upcoming dated obligations across the vault (also the reminder manifest). |

`due --format ics` renders the **same data** as the obligations output, as an RFC 5545
`VCALENDAR`.

## Shared conventions (suite-wide)

- **Streams:** `--json` → stdout (opt-in; default output is human, never mixed with
  prose). `--why` → stderr as `[why] <header>` plus indented lines. Errors → stderr.
- **Flags:** `-V/--version` (`contract-vault X.Y.Z`), `-h/--help`, `-q/--quiet/--silent`,
  `--no-color` (honors `NO_COLOR`, then `FORCE_COLOR`, then TTY autodetect).
- **Exit codes:** `0` success · `1` failure / findings (e.g. `verify` mismatch) · `2` bad
  usage.
- **LLM config lookup:** `~/.config/contract-ops/llm.json` first, then `./config/llm.json`
  (`provider` ∈ `{anthropic, openai}`, `model`, `api_key`, `base_url`). contract-vault
  only delegates LLM use to `extract`; it never calls a model itself.
- **Completion:** contract-vault exposes a hidden `__complete` subcommand (the
  template-vault style). extract-cli uses `extract completion <shell>` instead; both styles
  are acceptable in the suite.

## Versioning

Schema changes are semver-meaningful: backward-incompatible record/output changes require a
major version bump; new optional fields are minor additions. The current record/output
schema version is `1.0` (`schema_version` field on records).
