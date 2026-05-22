# Scheduling reminders & at-risk alerts

contract-vault has **no daemon and sends no notifications itself** — it computes deadlines
deterministically; *you schedule a job that delivers them.* This guide shows how to wire
that up. **You choose the cadence and the channel** — both are left open on purpose.

## The two commands to schedule

Both scan the **whole vault** and use exit codes so any scheduler can gate on them:

| Command | Emits / exit 1 when… | Use for |
|---|---|---|
| `contract-vault remind --strict --json` | an obligation's reminder window is open *today* (`0 ≤ days_until ≤ its longest lead`) | the daily "what to act on now" digest |
| `contract-vault risk --within 30d --strict --json` | a **CRITICAL** item — a missed/imminent auto-renewal **notice** | the renewal safety net |

Exit `0` = nothing to report (stay quiet); exit `1` = there's something — deliver it.
Set lead-times once for the whole corpus with `contract-vault config reminders --type … --set …`.

Pick a cadence (e.g. daily 08:00, weekdays, or a weekly Monday digest) and a channel
(email, Slack, macOS notification, an agent message, a calendar feed — whatever you have).

---

## Option A — Local scheduler (cron / launchd)

Best when the vault is a directory **on the machine running the scheduler**. Use the
example wrapper [`scripts/contract-vault-remind.sh`](../scripts/contract-vault-remind.sh):
edit its `notify()` function for your channel, then schedule it.

**cron** (run `crontab -e`):
```cron
# 07:00 UTC daily (≈ 09:00 Europe/Vienna). cron times are the box's local time unless noted.
0 7 * * *  CONTRACT_VAULT_DIR=$HOME/contracts /path/to/contract-vault-remind.sh
```

**macOS launchd** (`~/Library/LaunchAgents/com.you.contract-vault-remind.plist`): a
`StartCalendarInterval` entry running the same script. `launchctl load` it once.

The wrapper exits 0 for cron-friendliness and only invokes `notify()` when something is due.

---

## Option B — Remote scheduled agent (Claude Code routine via `/schedule`)

Best when the vault is a **GitHub repo** (a remote agent can't see your local machine).
The agent clones the repo, `pip install`s the CLI from PyPI, runs the checks, and reports.

**Prereqs**
- Vault pushed to a (private) GitHub repo of `record.json` files.
- *(Optional, for push delivery)* connect a Slack/email connector at
  <https://claude.ai/customize/connectors>. Without one, each run's summary appears in
  <https://claude.ai/code/routines> (and the Claude app).

**Set it up** with the `/schedule` skill (or the routines UI). Schedule in **UTC**
(convert from your local time); model `claude-sonnet-4-6`; allowed tools `Bash, Read`.
Self-contained agent prompt:

```text
You monitor a contract-vault vault for upcoming and at-risk contract obligations. Read-only.

1. Install the CLI:  pip install --quiet --upgrade contract-vault
2. Point it at the checkout:  export CONTRACT_VAULT_DIR="$PWD"
3. Run both checks (each scans the whole vault; each exits non-zero when it has findings):
     contract-vault remind --strict --json            # reminders whose window is open today
     contract-vault risk --within 30d --strict --json # missed / imminent renewal notices
4. If BOTH are empty (exit 0): reply exactly "All clear — no reminders or at-risk items."
   Otherwise: write a concise summary grouped by deal — for each item show the deal,
   type, due date, days_until, and (for risk) the severity.
5. Deliver it: if a Slack/email connector is attached, post the summary there; otherwise
   make the summary your final message. Do NOT modify the vault.
```

---

## Option C — CI (GitHub Actions)

Best when the vault is a GitHub repo and you want zero extra infrastructure. A scheduled
workflow in the *vault* repo:

```yaml
name: contract-reminders
on:
  schedule: [{ cron: "0 7 * * *" }]   # 07:00 UTC daily
  workflow_dispatch:
jobs:
  remind:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install contract-vault
      - name: Reminders + at-risk (non-zero exit => GitHub notifies watchers)
        env: { CONTRACT_VAULT_DIR: ${{ github.workspace }} }
        run: |
          contract-vault remind --strict --json
          contract-vault risk --within 30d --strict --json
```

A failing run is the signal (GitHub emails repo watchers). To push elsewhere instead, drop
`--strict` and add an `if: ${{ always() }}` step that posts the JSON to Slack/email and
never fails the job.

---

## What to schedule (cheat-sheet)

- **Daily ops:** `remind --strict --json` — today's reminders, channel of your choice.
- **Renewal safety net:** `risk --within 30d --strict --json` — catch missed notices early.
- **Weekly calendar refresh:** `due --within 365d --format ics > portfolio.ics` and
  re-publish the feed your team subscribes to.

All of these read the entire vault, so one scheduled job covers your whole contract corpus.
