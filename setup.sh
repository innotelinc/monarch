#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# setup.sh - one-command Monarch Media Platform setup
#
#   1. Creates .env from .env.sample when missing (never overwrites).
#   2. Installs the stack: Docker (if needed), /data layout, systemd service
#      and `docker compose up -d` (via scripts/install-monarch.sh).
#   3. Auto-configures Nginx Proxy Manager through its API
#      (scripts/npm-proxy-hosts.py): subdomain proxy hosts + wildcard
#      Let's Encrypt certificate via DNS challenge.
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

# 1. .env from sample
if [ ! -f .env ] && [ -f .env.sample ]; then
  cp .env.sample .env
  echo "Created .env from .env.sample -"
  echo "  edit MONARCH_USERNAME / MONARCH_PASSWORD, MONARCH_DOMAIN, SSL_EMAIL,"
  echo "  NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD and the DNS credentials,"
  echo "  then re-run:  ./setup.sh"
fi

# 2. Install the stack (in-place mode; idempotent)
bash scripts/install-monarch.sh

# 3. Configure Nginx Proxy Manager (proxy hosts + wildcard SSL)
if command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "=== Configuring Nginx Proxy Manager ==="
  python3 scripts/npm-proxy-hosts.py "${@:-}"
else
  echo ""
  echo "WARNING: python3 not found - skipping Nginx Proxy Manager configuration."
  echo "Install python3 and re-run:  python3 scripts/npm-proxy-hosts.py"
fi

echo ""
echo "Setup finished. The stack is running; open the dashboard:"
echo "  https://app.${MONARCH_DOMAIN:-monarch.innotel.us}"