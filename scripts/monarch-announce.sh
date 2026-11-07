#!/usr/bin/env bash
# Ask the Monarch host what its updater would do, and mail anything pending.
#
# The Monarch box has no MTA of its own, so the announcement runs here, on the
# host that can send mail, and asks the updater on the media host to resolve what
# is pending. `--notify-only` pulls nothing and recreates nothing: it compares
# what each container runs against what the registry publishes today.
#
# Nothing is mailed when nothing is pending — the journal records that the check
# happened. Exit: 0 the question was answered (pending or not) · 1 no answer.
#
# Environment (/etc/innotel/monarch-announce.env):
#   MONARCH_HOST        ssh target of the media host (default root@192.168.1.56)
#   MONARCH_UPDATER     path to the updater there
#   ALERT_EMAIL_TO      mailbox the list is mailed to (sendmail)
#   ALERT_WEBHOOK_URL   JSON webhook the list is POSTed to instead
#   SSHPASS             password for MONARCH_HOST (sshpass -e)
set -uo pipefail

HOST=${MONARCH_HOST:-root@192.168.1.56}
UPDATER=${MONARCH_UPDATER:-/opt/innotel/monarch-update/monarch-update.sh}
ALERT_TO=${ALERT_EMAIL_TO:-}
ALERT_WEBHOOK=${ALERT_WEBHOOK_URL:-}
HOSTNAME_LOCAL=$(hostname -f 2>/dev/null || hostname)

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { printf '%s WARNING: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

if [ -z "${SSHPASS:-}" ]; then
  warn "SSHPASS is unset; cannot reach $HOST"
  exit 1
fi

out=$(sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=10 \
  "$HOST" "$UPDATER --notify-only" 2>&1)
rc=$?
if [ $rc -ne 0 ] && ! printf '%s' "$out" | grep -q '^pending: '; then
  warn "the updater on $HOST did not answer (exit $rc)"
  printf '%s\n' "$out" | tail -5 | sed 's/^/  /' >&2
  exit 1
fi

pending=$(printf '%s\n' "$out" | grep '^pending: ' || true)
if [ -z "$pending" ]; then
  log "nothing pending on $HOST"
  exit 0
fi

count=$(printf '%s\n' "$pending" | wc -l)
subject="Monarch: $count media update(s) pending"
body="The 05:00 run on $HOST would move:

$(printf '%s\n' "$pending" | sed 's/^pending: /  - /')

Nothing has been pulled or restarted. To hold one back, pin it by digest in the compose file; to apply them now: systemctl start monarch-update.service on $HOST."
log "$count pending"

if [ -n "$ALERT_TO" ] && [ -x /usr/sbin/sendmail ]; then
  printf 'To: %s\nSubject: %s\nFrom: monarch-announce@%s\nAuto-Submitted: auto-generated\n\n%s\n' \
    "$ALERT_TO" "$subject" "$HOSTNAME_LOCAL" "$body" |
    /usr/sbin/sendmail -t || warn "sendmail refused the message — it is only in the journal"
fi
if [ -n "$ALERT_WEBHOOK" ]; then
  payload=$(printf '%s' "$body" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')
  curl -fsS --max-time 15 -X POST -H 'Content-Type: application/json' \
    -d "{\"text\":\"$subject — $payload\"}" "$ALERT_WEBHOOK" >/dev/null ||
    warn "the webhook refused the POST — it is only in the journal"
fi
if [ -z "$ALERT_TO" ] && [ -z "$ALERT_WEBHOOK" ]; then
  warn "nothing receives this (ALERT_EMAIL_TO / ALERT_WEBHOOK_URL unset); it is only in the journal"
fi

exit 0
