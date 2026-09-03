#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# setup.sh - one-command Monarch Media Platform setup
#
#   1. Creates .env from .env.sample when missing (never overwrites).
#   2. Installs the stack: Docker (if needed), /data layout, systemd service
#      and `docker compose up -d` (via scripts/install-monarch.sh).
#   3. Seeds the Homarr landing board (homarr/board.default.json) so the
#      apex MONARCH_DOMAIN shows the Monarch main interface linking to every
#      subdomain (skips boards that were already customized).
#   4. Auto-configures Nginx Proxy Manager through its API
#      (scripts/npm-proxy-hosts.py): subdomain proxy hosts + wildcard
#      Let's Encrypt certificate via DNS challenge. When the NPM admin has
#      not been created yet, the script bootstraps it from NPM_ADMIN_EMAIL /
#      NPM_ADMIN_PASSWORD automatically.
#   5. Optionally provisions the two Stripe webhook endpoints from
#      STRIPE_SECRET_KEY (scripts/stripe-webhooks.sh) - runs only when a real
#      key is set and the webhook secrets are still placeholders.
#
# Requires: bash, python3 (for the NPM script), and the variables documented
# in .env.sample - at minimum MONARCH_DOMAIN, and for HTTPS the SSL_EMAIL +
# DNS provider credentials (see NPM_DNS_CREDENTIALS / CLOUDFLARE_API_TOKEN)
# plus NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD.
#
# Idempotent: safe to re-run after editing .env or npm-hosts.conf - the proxy
# hosts are reconciled and the certificate reused if it already exists.
# ═══════════════════════════════════════════════════════════════════════════

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== Monarch Media Platform setup ==="

# Enable the version-controlled commit-guard hooks (.githooks) if this is a
# git checkout (blocks attribution to anyone but Darnel Hunter).
if [ -d "$ROOT/.githooks" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git config core.hooksPath "$ROOT/.githooks"
  echo "commit guard hook enabled (core.hooksPath -> .githooks)"
fi

# 1. .env from sample
if [ ! -f .env ] && [ -f .env.sample ]; then
  cp .env.sample .env
  echo "Created .env from .env.sample -"
  echo "  edit MONARCH_USERNAME / MONARCH_PASSWORD, MONARCH_DOMAIN, SSL_EMAIL,"
  echo "  NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD and the DNS credentials,"
  echo "  then re-run:  ./setup.sh"
fi

# 2. Install the stack (in-place mode; idempotent)
# Deploy in place from this repo checkout by default, so the stack runs from
# the source you cloned instead of a copy at /opt/monarch. Override with
# MONARCH_TARGET (e.g. a deployed /opt/monarch layout or a disk install).
export MONARCH_TARGET="${MONARCH_TARGET:-$ROOT}"

# Nginx Proxy Manager: "local" (default) runs the NPM container via the
# "npm" compose profile; "remote" reuses an existing NPM server instead
# (see NPM_MODE / NPM_BASE_URL in .env). Tell the installer which mode so it
# can strip the profile from the systemd unit for remote installs.
npm_mode="$(sed -n 's/^NPM_MODE=//p' .env 2>/dev/null | tail -1 | xargs | tr '[:upper:]' '[:lower:]')"
export MONARCH_NPM_LOCAL=1
[ "$npm_mode" = "remote" ] && export MONARCH_NPM_LOCAL=0
bash scripts/install-monarch.sh

# 3. Seed the Homarr landing board (Monarch main interface)
# Renders homarr/board.default.json (a __DOMAIN__ template) into the Homarr
# configs dir that docker-compose mounts. Never overwrites a board that was
# already customized away from the stock Homarr welcome screen.
homarr_conf="/docker/appdata/homarr/configs/default.json"
if [ -f "$ROOT/homarr/board.default.json" ]; then
  if [ -f "$homarr_conf" ] && ! grep -q 'Welcome to Homarr' "$homarr_conf" 2>/dev/null; then
    echo ""
    echo "Homarr board already customized - leaving it as-is."
    echo "  (template: homarr/board.default.json - copy it over $homarr_conf to re-seed)"
  else
    mkdir -p "$(dirname "$homarr_conf")"
    sed "s/__DOMAIN__/${MONARCH_DOMAIN:-monarch.innotel.us}/g" \
      "$ROOT/homarr/board.default.json" > "$homarr_conf"
    echo ""
    echo "Seeded Homarr landing board -> $homarr_conf"
    docker restart homarr >/dev/null 2>&1 || true
  fi
fi

# 4. Configure Nginx Proxy Manager (proxy hosts + wildcard SSL)
if command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "=== Configuring Nginx Proxy Manager ==="
  python3 scripts/npm-proxy-hosts.py "$@"
else
  echo ""
  echo "WARNING: python3 not found - skipping Nginx Proxy Manager configuration."
  echo "Install python3 and re-run:  python3 scripts/npm-proxy-hosts.py"
fi

# 5. Stripe webhook (optional) - only when a real STRIPE_SECRET_KEY is in
# .env and the signing secret is still a placeholder (first configure).
# Magnate is the source billing platform, so this just ensures the single
# subscribe.innotel.us webhook endpoint exists.
if [ -f .env ] && grep -Eq '^STRIPE_SECRET_KEY=(sk_(test|live)_.+)$' .env; then
  if grep -Eq '^STRIPE_WEBHOOK_SECRET=(|whsec_replace_me)$' .env; then
    echo ""
    echo "=== Provisioning Stripe webhook ==="
    bash scripts/stripe-webhooks.sh || echo "  (stripe-webhooks.sh failed - see its output)"
  else
    echo ""
    echo "Stripe webhook secret already configured - skipping webhook setup."
  fi
fi

echo ""
echo "Setup finished. The stack is running; open the dashboard:"
echo "  https://${MONARCH_DOMAIN:-monarch.innotel.us}   (main interface)"
echo "  https://app.${MONARCH_DOMAIN:-monarch.innotel.us}"
