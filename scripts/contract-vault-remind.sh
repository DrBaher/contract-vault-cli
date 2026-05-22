#!/usr/bin/env sh
# Example reminder runner for cron/launchd — see docs/SCHEDULING.md.
#
# Runs contract-vault's reminder + at-risk checks against a vault and notifies ONLY when
# there is something to report. Exits 0 so the scheduler stays happy.
#
# Usage:   CONTRACT_VAULT_DIR=$HOME/contracts ./contract-vault-remind.sh
#    or:   ./contract-vault-remind.sh /path/to/vault
#
# EDIT the notify() function below to pick your delivery channel.
set -eu

VAULT="${CONTRACT_VAULT_DIR:-${1:-$HOME/contracts}}"
export CONTRACT_VAULT_DIR="$VAULT"
CV="${CONTRACT_VAULT_BIN:-contract-vault}"

notify() {
    # $1 = subject; the message body arrives on stdin. Pick ONE (and delete the rest):
    #
    #   macOS popup:  osascript -e "display notification \"$1\" with title \"contract-vault\""
    #   email:        mail -s "$1" you@example.com
    #   Slack:        curl -fsS -X POST -H 'Content-type: application/json' \
    #                      --data "{\"text\": \"$1\"}" "$SLACK_WEBHOOK_URL"
    #
    # Default: print to stdout. Under cron with MAILTO set, that becomes an email.
    printf '== %s ==\n' "$1"
    cat
}

# `--strict` exits non-zero when there IS something to report, so the `else` branch fires
# exactly when the user should be notified.
if out="$("$CV" remind --strict --json 2>/dev/null)"; then
    :  # nothing due today
else
    printf '%s\n' "$out" | notify "contract-vault: reminders due"
fi

if out="$("$CV" risk --within 30d --strict --json 2>/dev/null)"; then
    :  # no critical/at-risk items
else
    printf '%s\n' "$out" | notify "contract-vault: AT-RISK renewal notices"
fi

exit 0
