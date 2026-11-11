#!/usr/bin/env bash
# Usage: alert.sh LEVEL MESSAGE...
# Logs to syslog/journal and posts to ALERT_WEBHOOK_URL (Discord, Slack or ntfy).
# Always exits 0: alerting must never break the caller.
set -uo pipefail
ENV_FILE=${ENV_FILE:-/etc/livestack/env}
if [ -r "$ENV_FILE" ]; then set -a; . <(tr -d '\r' < "$ENV_FILE"); set +a; fi

level=${1:-warning}; shift || true
msg="[livestack][$(hostname)][$level] $*"
logger -t livestack-alert -- "$msg" 2>/dev/null || true
echo "$msg" >&2

[ -n "${ALERT_WEBHOOK_URL:-}" ] || exit 0
if [[ "$ALERT_WEBHOOK_URL" == *ntfy* ]]; then
  curl -fsS -m 10 --retry 2 -d "$msg" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || true
else
  payload=$(jq -nc --arg t "$msg" '{content:$t, text:$t}')
  curl -fsS -m 10 --retry 2 -H 'Content-Type: application/json' -d "$payload" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || true
fi
exit 0
