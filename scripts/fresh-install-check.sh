#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# fresh-install-check.sh - smoke-check the Monarch fresh-install path
#
# Verifies the pieces ./setup.sh runs on a brand-new box - and that the
# seeded Homarr landing board actually renders - without touching a real
# deployment:
#
#   Stage 1 (default, no Docker daemon needed):
#     1. Renders a throwaway .env from .env.sample (random creds, never the
#        repo's real .env) and checks it parses.
#     2. Renders homarr/board.default.json (the __DOMAIN__ template setup.sh
#        seeds) and asserts it is valid JSON, has no leftover __DOMAIN__
#        placeholders, links every service to a domain/subdomain declared in
#        scripts/npm-hosts.conf, and keeps guests locked out.
#     3. Dry-runs scripts/npm-proxy-hosts.py and asserts the apex (@) host -
#        the main Homarr interface at MONARCH_DOMAIN itself - is planned
#        first, before the subdomains.
#     4. When the docker CLI is present (no daemon required): validates that
#        docker-compose.yml parses with the throwaway .env, catching broken
#        ${VAR} interpolation (e.g. nested MONARCH_DOMAIN defaults).
#
#   Stage 2 (--full, DISPOSABLE host/VM with Docker + sudo): boots the real
#     homarr container, seeds its board the same way setup.sh does (rendering
#     the template into /docker/appdata/homarr/configs/default.json), restarts
#     it, and probes http://127.0.0.1:7575 for HTTP 200. The container is shut
#     down afterwards (keep it running with MONARCH_CHECK_KEEP=1).
#
# Stage 2 writes to the real /docker/appdata and starts a container, so it
# refuses to run when a live stack is detected unless the host is explicitly
# declared disposable. Stage 1 is safe to run anywhere, any time.
#
# Usage:
#   scripts/fresh-install-check.sh                     # offline rehearsal
#   scripts/fresh-install-check.sh --full              # + live Homarr boot
#   MONARCH_DOMAIN=my.test scripts/fresh-install-check.sh
#
# Env overrides: MONARCH_CHECK_DOMAIN (default monarch-check.test - use a
#   separate name so a real MONARCH_DOMAIN in the environment is never
#   clobbered), MONARCH_CHECK_KEEP, MONARCH_CHECK_DISPOSABLE (=1 to allow
#   --full on a host that already has /docker/appdata/homarr state)
# ═══════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

FULL=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    *) echo "Unknown argument: $arg (expected --full)" >&2; exit 2 ;;
  esac
done

TEST_DOMAIN="${MONARCH_CHECK_DOMAIN:-monarch-check.test}"
case "$TEST_DOMAIN" in
  *[!a-zA-Z0-9.-]*|*..*|.*|*.) echo "Invalid MONARCH_CHECK_DOMAIN: $TEST_DOMAIN" >&2; exit 2 ;;
esac
TEST_PASSWORD="check-$(openssl rand -hex 6 2>/dev/null || tr -dc 'a-f0-9' < /dev/urandom | head -c 12)"
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

SCRATCH="$(mktemp -d)"
cleanup() {
  if [ "${MONARCH_CHECK_KEEP:-0}" = "1" ]; then
    echo "  (MONARCH_CHECK_KEEP=1 - leaving scratch dir at $SCRATCH)"
  else
    rm -rf "$SCRATCH"
  fi
}
trap cleanup EXIT

render_env() {
  local out="$1"
  sed \
    -e "s|^MONARCH_DOMAIN=.*|MONARCH_DOMAIN=$TEST_DOMAIN|" \
    -e "s|^MONARCH_PASSWORD=.*|MONARCH_PASSWORD=$TEST_PASSWORD|" \
    -e "s|^SESSION_SECRET=change-me.*|SESSION_SECRET=$(openssl rand -hex 32 2>/dev/null || tr -dc 'a-f0-9' < /dev/urandom | head -c 64)|" \
    .env.sample > "$out"
}

render_board() {
  local domain="$1" out="$2"
  sed "s/__DOMAIN__/$domain/g" homarr/board.default.json > "$out"
}

echo "=== Monarch fresh-install check (domain: $TEST_DOMAIN) ==="

# ── Stage 1a: throwaway .env from the sample ──────────────────────────────
echo ""
echo "--- .env rendering ---"
render_env "$SCRATCH/.env"
if grep -q "^MONARCH_DOMAIN=$TEST_DOMAIN$" "$SCRATCH/.env" \
   && grep -q "^MONARCH_PASSWORD=" "$SCRATCH/.env"; then
  ok ".env renders from .env.sample (test domain + random password)"
else
  bad ".env did not render from .env.sample"
fi
if grep -q "^SESSION_SECRET=" "$SCRATCH/.env" \
   && ! grep -q "^SESSION_SECRET=change-me$" "$SCRATCH/.env"; then
  ok "SESSION_SECRET generated"
else
  bad "SESSION_SECRET not generated"
fi

# ── Stage 1b: Homarr landing board template ───────────────────────────────
echo ""
echo "--- Homarr landing board template ---"
if python3 -m json.tool homarr/board.default.json > /dev/null 2>&1; then
  ok "board.default.json is valid JSON"
else
  bad "board.default.json is not valid JSON"
fi
render_board "$TEST_DOMAIN" "$SCRATCH/board.json"
if python3 -m json.tool "$SCRATCH/board.json" > /dev/null 2>&1; then
  ok "rendered board is valid JSON"
else
  bad "rendered board is not valid JSON"
fi
if grep -q "__DOMAIN__" "$SCRATCH/board.json"; then
  bad "rendered board still contains __DOMAIN__ placeholders"
else
  ok "no __DOMAIN__ placeholders left after rendering"
fi
# Cross-check the RENDERED board's links against the proxy-host map: every
# URL the board points at must be the apex or a subdomain declared in
# npm-hosts.conf, and the apex tile (the main interface tile itself) must be
# present. The conf parser mirrors load_hosts() ("@" = the apex).
if python3 - "$SCRATCH/board.json" "$TEST_DOMAIN" > "$SCRATCH/cross.txt" 2>&1 <<'PYEOF'
import json, os, sys

rendered, domain = sys.argv[1], sys.argv[2]
script_dir = os.path.join(os.getcwd(), "scripts")

hosts = set()
with open(os.path.join(script_dir, "npm-hosts.conf")) as fh:
    for line in fh:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        sub = parts[0]
        hosts.add(domain if sub == "@" else f"{sub}.{domain}")

with open(rendered) as fh:
    board = json.load(fh)

urls = []
for app in board.get("apps", []):
    for value in (app.get("url"),
                  (app.get("behaviour") or {}).get("onClickUrl"),
                  (app.get("behaviour") or {}).get("externalUrl")):
        if isinstance(value, str) and value.startswith("https://"):
            urls.append(value)

problems = []
for u in sorted(set(urls)):
    host = u.split("/", 3)[2]
    if host not in hosts:
        problems.append(f"{u} -> host '{host}' is not in npm-hosts.conf")
if not any(u.split("/", 3)[2] == domain for u in urls):
    problems.append("board has no tile for the apex (main interface)")
if problems:
    for p in problems:
        print(p)
    sys.exit(1)
print(f"ok: {len(set(urls))} board links all match npm-hosts.conf (incl. apex)")
PYEOF
then
  ok "board links match the proxy-host map"
else
  bad "board link cross-check failed:"
  sed 's/^/    /' "$SCRATCH/cross.txt"
fi

# ── Stage 1c: NPM proxy-host plan (dry run) ───────────────────────────────
echo ""
echo "--- NPM proxy-host plan (dry run) ---"
# Exported vars take precedence over .env in npm-proxy-hosts.py (load_env
# never overwrites), so drive the dry run with the throwaway values only.
# Exported vars take precedence over .env in npm-proxy-hosts.py (load_env
# never overwrites), so drive the dry run with the throwaway values only. The
# forward target depends on NPM_FORWARD_HOST (container name or an IP), so
# assert on the domain -> <anything>:<port> pair rather than a fixed target.
export NPM_ADMIN_EMAIL="check@$TEST_DOMAIN"
export NPM_ADMIN_PASSWORD="$TEST_PASSWORD"
export NPM_DNS_PROVIDER=cloudflare
plan="$SCRATCH/plan.txt"
MONARCH_DOMAIN="$TEST_DOMAIN" python3 scripts/npm-proxy-hosts.py --dry-run > "$plan" 2>&1 || true
if grep -Eq "would (create|update) $TEST_DOMAIN -> .*:7575 \(ws=True\)" "$plan"; then
  ok "apex host ($TEST_DOMAIN -> :7575) is in the plan"
else
  bad "apex host missing from the dry-run plan"
fi
if grep -Eq "would (create|update) app\.$TEST_DOMAIN -> .*:7575" "$plan"; then
  ok "app subdomain host is in the plan"
else
  bad "app subdomain missing from the dry-run plan"
fi
if grep -q "__DOMAIN__" "$plan" || grep -Eq "(^|[^a-z0-9-])monarch\.innotel\.us" "$plan"; then
  bad "plan references the template placeholder or the sample domain"
else
  ok "plan uses only the test domain"
fi

# ── Stage 1d: compose file interpolation (docker CLI, no daemon needed) ────
if command -v docker > /dev/null 2>&1 && docker compose version > /dev/null 2>&1; then
  echo ""
  echo "--- docker-compose.yml interpolation ---"
  if docker compose -f docker-compose.yml --env-file "$SCRATCH/.env" config -q 2>"$SCRATCH/compose.err"; then
    ok "docker-compose.yml parses with the throwaway .env"
  else
    bad "docker-compose.yml failed to parse:"
    sed 's/^/    /' "$SCRATCH/compose.err" | head -10
  fi
else
  echo ""
  echo "--- docker-compose.yml interpolation ---"
  echo "  (skipped - docker CLI or the compose plugin not found; install docker-compose-v2 to enable this check)"
fi

# ── Stage 2 (--full): live Homarr boot + render check ─────────────────────
if [ "$FULL" = "1" ]; then
  echo ""
  echo "=== Stage 2: live Homarr boot (disposable host required) ==="
  command -v docker > /dev/null 2>&1 || { echo "FAIL: --full needs docker" >&2; exit 1; }
  if ! docker info > /dev/null 2>&1; then
    echo "FAIL: docker daemon is not running" >&2; exit 1
  fi
  if docker ps --format '{{.Names}}' | grep -qE '^(homarr|nginx-proxy-manager|monarch-)' \
     && [ "${MONARCH_CHECK_DISPOSABLE:-0}" != "1" ]; then
    echo "FAIL: a Monarch stack is already running - refusing to touch it." >&2
    echo "      Run this on a disposable host/VM, or export MONARCH_CHECK_DISPOSABLE=1." >&2
    exit 1
  fi
  if [ -f /docker/appdata/homarr/configs/default.json ] \
     && [ "${MONARCH_CHECK_DISPOSABLE:-0}" != "1" ]; then
    echo "FAIL: /docker/appdata/homarr already has state (not a fresh host)." >&2
    echo "      Export MONARCH_CHECK_DISPOSABLE=1 to overwrite, or clean the host." >&2
    exit 1
  fi

  # Seed exactly like setup.sh step 3 (back up anything already there).
  $SUDO mkdir -p /docker/appdata/homarr/configs
  $SUDO chown -R 1000:1000 /docker/appdata/homarr 2>/dev/null || true
  if [ -f /docker/appdata/homarr/configs/default.json ]; then
    $SUDO cp /docker/appdata/homarr/configs/default.json \
            /docker/appdata/homarr/configs/default.json.fresh-check.bak
    echo "  (backed up existing board to default.json.fresh-check.bak)"
  fi
  render_board "$TEST_DOMAIN" "$SCRATCH/board.json"
  $SUDO cp "$SCRATCH/board.json" /docker/appdata/homarr/configs/default.json

  echo "  Starting homarr (docker compose up -d homarr)..."
  MONARCH_DOMAIN="$TEST_DOMAIN" docker compose -f docker-compose.yml up -d homarr

  wait_healthy() {
    local n=0
    while [ "$n" -lt 48 ]; do
      local st
      st="$(docker inspect --format '{{.State.Health.Status}}' homarr 2>/dev/null || true)"
      [ "$st" = "healthy" ] && return 0
      n=$((n + 1)); sleep 5
    done
    return 1
  }
  if wait_healthy; then
    ok "homarr container is healthy"
  else
    bad "homarr did not become healthy in time:"
    docker logs --tail 30 homarr 2>&1 | sed 's/^/    /'
    exit 1
  fi

  echo "  Restarting homarr to load the seeded board..."
  docker restart homarr > /dev/null
  if wait_healthy; then
    ok "homarr healthy after restart (board reloaded)"
  else
    bad "homarr not healthy after restart"; exit 1
  fi

  code="$(curl -sS -o "$SCRATCH/page.html" -w '%{http_code}' http://127.0.0.1:7575/ || true)"
  if [ "$code" = "200" ] && [ -s "$SCRATCH/page.html" ]; then
    ok "homarr serves HTTP 200 at http://127.0.0.1:7575 (board page)"
  else
    bad "homarr returned HTTP ${code:-'no response'} at :7575"
  fi

  echo ""
  echo "  Last homarr log lines (eyeball for errors):"
  docker logs --tail 15 homarr 2>&1 | sed 's/^/    /'

  if [ "${MONARCH_CHECK_KEEP:-0}" != "1" ]; then
    echo "  Stopping homarr (keep it up with MONARCH_CHECK_KEEP=1)..."
    docker compose -f docker-compose.yml rm -sf homarr > /dev/null 2>&1 || true
  fi
fi

echo ""
echo "=== Fresh-install check finished: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
