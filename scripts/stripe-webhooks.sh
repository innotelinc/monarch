#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# stripe-webhooks.sh - provision the Monarch Stripe webhook endpoints.
#
# Replaces the manual "two Stripe webhooks, six events each" step. Reads .env
# and uses the Stripe API to create (never duplicate) two endpoints:
#
#   1. Subscription platform : ${APP_URL}/api/webhook   -> STRIPE_WEBHOOK_SECRET
#   2. billing-api (paid gating): ${BILLING_BASE_URL}/api/webhook -> BILLING_WEBHOOK_SECRET
#
# Each endpoint subscribes to the same six events:
#   checkout.session.completed, customer.subscription.created,
#   customer.subscription.updated, customer.subscription.deleted,
#   invoice.paid, invoice.payment_failed
# (override with STRIPE_EVENTS="a,b,c"). billing-api can also reactivate on
# invoice.payment_succeeded - append it to STRIPE_EVENTS if you rely on that.
#
# The returned webhook signing secrets are written into .env - and only when
# the variable is empty or still a placeholder - so re-running is safe and
# existing secrets are never overwritten or printed. Run `docker compose up -d`
# afterwards so the containers reload .env.
#
# Usage:
#   ./scripts/stripe-webhooks.sh [--dry-run]
#   STRIPE_EVENTS="checkout.session.completed,invoice.paid" ./scripts/stripe-webhooks.sh
#
# Requires STRIPE_SECRET_KEY in .env (sk_test_... / sk_live_...).
# ═══════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# Load .env (never override already-exported values).
env_file="${MONARCH_ENV:-$ROOT/.env}"
if [ -f "$env_file" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      ''|\#*) continue ;;
    esac
    value="${value%\"}"; value="${value#\"}"
    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done < "$env_file"
fi

STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-}"
if [ -z "$STRIPE_SECRET_KEY" ] || [ "$STRIPE_SECRET_KEY" = "sk_live_replace_me" ]; then
  echo "ERROR: STRIPE_SECRET_KEY is not set in .env - add your Stripe secret key"
  echo "       (Developers > API keys), then re-run this script."
  exit 2
fi

domain="${MONARCH_DOMAIN:-monarch.innotel.us}"
app_url="${APP_URL:-https://subscribe.$domain}"
billing_base="${BILLING_BASE_URL:-https://api.$domain}"
url_platform="${app_url%/}/api/webhook"
url_billing="${billing_base%/}/api/webhook"

# Six default events; override wholesale with STRIPE_EVENTS.
IFS=',' read -r -a EVENTS <<< "${STRIPE_EVENTS:-checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed}"

api="https://api.stripe.com/v1"

echo "=== Monarch Stripe webhook provisioning ==="
echo "  platform endpoint : $url_platform"
echo "  billing endpoint  : $url_billing"
echo "  events (${#EVENTS[@]}): ${EVENTS[*]}"

# Idempotence: skip endpoints that already exist with the same URL.
existing() {
  # echos the webhook endpoint id for the given url, or nothing.
  curl -s -u "$STRIPE_SECRET_KEY:" "$api/webhook_endpoints?limit=100" \
    | python3 -c "
import json, sys
url = sys.argv[1]
for e in json.load(sys.stdin).get('data', []):
    if e.get('url') == url:
        print(e['id']); break
" "$1" 2>/dev/null || true
}

# Create an endpoint; echoes "id secret".
create_endpoint() {
  local url="$1"
  local args=(-s -u "$STRIPE_SECRET_KEY:" -X POST "$api/webhook_endpoints" -d "url=$url")
  for ev in "${EVENTS[@]}"; do
    args+=(-d "enabled_events[]=$ev")
  done
  curl "${args[@]}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('id'):
    print(d['id'], d.get('secret', ''))
else:
    sys.stderr.write('ERROR: ' + json.dumps(d) + '\n')
    sys.exit(1)
"
}

set_secret() {
  # Set VAR in .env to SECRET, only when empty or still a placeholder.
  local var="$1" secret="$2" file="$3"
  local cur
  cur="$(sed -n "s/^$var=//p" "$file" | tail -1 | xargs)"
  case "$cur" in
    ""|"whsec_replace_me"|"replace_me") ;;
    *)
      echo "  $var already set in .env - leaving it untouched."
      return ;;
  esac
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] would set $var in .env"
    return
  fi
  if [ -w "$file" ]; then
    if grep -q "^$var=" "$file"; then
      sed -i "s|^$var=.*|$var=$secret|" "$file"
    else
      printf '\n%s=%s\n' "$var" "$secret" >> "$file"
    fi
    echo "  $var set in $file"
  else
    echo "  WARNING: $file is not writable - set $var=$secret manually."
  fi
}

for pair in "$url_platform:STRIPE_WEBHOOK_SECRET" "$url_billing:BILLING_WEBHOOK_SECRET"; do
  # Split on the LAST colon so URLs containing "://" aren't truncated.
  url="${pair%:*}"; var="${pair##*:}"
  echo ""
  echo "--- $url ---"
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] would create endpoint for $url (events: ${EVENTS[*]})"
    continue
  fi
  existing_id="$(existing "$url")"
  if [ -n "$existing_id" ]; then
    echo "  endpoint $existing_id already exists - skipping."
    continue
  fi
  out="$(create_endpoint "$url")"
  id="${out%% *}"; secret="${out#* }"
  echo "  created endpoint $id"
  [ -n "$secret" ] && set_secret "$var" "$secret" "$env_file"
done

echo ""
echo "Done. Reload the services so they pick up the new secrets:"
echo "  sudo docker compose up -d"
echo "Verify delivery under https://dashboard.stripe.com/webhooks"
