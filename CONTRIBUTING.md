# Contributing to contract-vault

Thanks for helping! contract-vault is part of the [contract-ops CLI
suite](https://github.com/DrBaher) and follows its shared conventions.

## Development setup

```bash
git clone https://github.com/DrBaher/contract-vault-cli
cd contract-vault
make install        # editable install with dev extras (pytest, coverage, mypy, build)
make test           # full suite
make typecheck      # mypy --strict
make coverage       # coverage report
```

No network or extract-cli is needed to develop or test: fixtures vendor sample extract
JSON, and schema-conformance uses a stdlib validator.

## Ground rules

- **Stdlib only at runtime.** `contract_vault_cli.py` must keep `dependencies = []`. Heavy
  work belongs in `extract-cli` (delegated), not here. Dev-only tools go in the `[dev]`
  extra.
- **Single file.** All CLI code lives in `contract_vault_cli.py`.
- **`mypy --strict` must pass.** Annotate everything.
- **ASCII-safe, UTF-8.** Output must be locale-safe; CI runs on macOS too.
- **Deterministic core.** Anything in the register / `due` / `.ics` path must work with the
  LLM off. LLM use stays opt-in and delegated to `extract`.
- **Honor the I/O contract.** `--json` → stdout, `--why` → stderr, respect
  `-q/--quiet/--silent`, `--no-color`, `NO_COLOR`/`FORCE_COLOR`. Exit `0`/`1`/`2`.

## Adding a subcommand

1. Write a `cmd_<name>(args) -> int` handler.
2. Register it in `build_parser()` with `_add_common(parser)` for the shared flags.
3. Add unit tests in `tests/test_<name>.py`.
4. If it emits a new machine format, add a JSON Schema 2020-12 file under `docs/spec/`,
   register it in `docs/INTEROP.md`, and add a conformance test.
5. Update `README.md` and `CHANGELOG.md`.

## Schemas

Record/output schema changes are **semver-meaningful**: backward-incompatible changes
need a major bump; new optional fields are minor. Keep the embedded validator in
`tests/conftest.py` in sync if you use a new JSON Schema keyword.

## Commit identity

Commits to this repository are authored as `DrBaher <Drbaher@gmail.com>` (the suite
identity). Set it locally before committing:

```bash
git config user.name "DrBaher"
git config user.email "Drbaher@gmail.com"
```

(This is unrelated to the runtime identity contract-vault uses for *vault* commits, which
follows the end user's own git config.)

## Releasing

```bash
make release VERSION=X.Y.Z   # verifies version, runs tests + mypy + build, tags vX.Y.Z
```

Pushing the tag triggers PyPI Trusted Publishing via `.github/workflows/publish.yml`.
