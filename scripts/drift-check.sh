#!/usr/bin/env bash
set -uo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# drift-check.sh - live-stack health check
#
# Probes the running Monarch stack and verifies the invariants monarch-init
# is supposed to maintain. It NEVER writes anything - it only reads API keys
# from /docker/appdata and issues GET/POST checks against the services.
#
# Single source of truth: what to check comes from
# /docker/appdata/init/invariants.json, which monarch-init emits from the
# same constants it configures with (init/init.py build_invariants()). The
# check can therefore never diverge from what init actually sets up - if an
# app, root folder, category or library is added there, it is checked here
# automatically.
#
# Exits non-zero when drift is found, so it can be run from a systemd timer
# (systemd/monarch-drift-check.{service,timer}) or cron to alert on drift.
#
# Checks (all driven by the invariants manifest):
#   *arr (sonarr/radarr/lidarr/whisparr):
#     - API reachable
#     - forms authentication configured (authMethod = forms)
#     - expected media root folder present
#     - qBittorrent download client present
#   Prowlarr:
#     - qBittorrent download client present
#     - Sonarr/Radarr/Lidarr/Whisparr apps registered
#   qBittorrent:
#     - WebUI login works with the shared credentials
#     - movies/tv/music/xxx categories exist
#   Jellyfin:
#     - admin login works with the shared credentials
#     - media libraries exist
#   Jellyseerr:
#     - initialized, Jellyfin sign-in enabled
#   Bazarr:
#     - API key readable, basic auth configured
#   Authentik:
#     - LDAP outpost provisioned (when AUTHENTIK_BASE_URL is set)
#   Nginx Proxy Manager (only when the local NPM container runs):
#     - live proxy hosts match scripts/npm-hosts.conf (npm-proxy-hosts.py
#       --check: subdomain, forward host, port and websocket support)
#   Infra (host, only when the docker CLI works):
#     - /data and /docker/appdata disk usage below DRIFT_DISK_MAX_PCT (90)
#     - each probed container not crash-looping (RestartCount below
#       DRIFT_MAX_RESTARTS, default 10)
#     - each probed container runs the current image (watchtower pulled a
#       newer one but the container was never recreated -> stale image)
#
# Modes:
#   (default)            check the live stack, read-only
#   --quiet              only print DRIFT-FAIL lines (for cron/timers)
#   --heal               when drift is found, re-run monarch-init, then
#                        re-verify and report whether the stack healed.
#                        Rate-limited: DRIFT_HEAL_MIN_INTERVAL (default 3600s)
#                        must have passed since the last heal attempt, else
#                        it escalates straight to an alert instead of looping.
#   --check-manifest     validate a manifest file's schema only (no network) -
#                        used by fresh-install-check.sh in CI; pass the file
#                        with MONARCH_INVARIANTS=<path>
#   --test-telegram      send a test Telegram message (needs .env vars)
#
# Usage:
#   scripts/drift-check.sh
#   scripts/drift-check.sh --quiet --heal
#   MONARCH_INVARIANTS=/tmp/inv.json scripts/drift-check.sh --check-manifest
# ═══════════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")/.." || exit 1

QUIET=0
HEAL=0
CHECK_MANIFEST=0
TEST_TG=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --heal) HEAL=1 ;;
    --check-manifest) CHECK_MANIFEST=1 ;;
    --test-telegram) TEST_TG=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ENV_FILE="${MONARCH_ENV:-.env}"
MANIFEST="${MONARCH_INVARIANTS:-/docker/appdata/init/invariants.json}"

# ── --check-manifest: validate schema only (no .env, no network) ──────────
if [ "$CHECK_MANIFEST" -eq 1 ]; then
  if [ ! -f "$MANIFEST" ]; then
    echo "DRIFT-FAIL: manifest not found at $MANIFEST" >&2
    exit 1
  fi
  python3 - "$MANIFEST" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as fh:
    m = json.load(fh)

errors = []
if m.get("version") != 1:
    errors.append("version != 1")

arr = m.get("arr_apps")
if not isinstance(arr, list) or not arr:
    errors.append("arr_apps missing/empty")
else:
    for a in arr:
        for key in ("svc", "port", "api", "category", "media", "root_folder"):
            if key not in a:
                errors.append(f"arr_apps entry missing '{key}': {a}")

pw = m.get("prowlarr", {})
if not isinstance(pw.get("apps"), list) or not pw["apps"]:
    errors.append("prowlarr.apps missing/empty")
if not isinstance(pw.get("port"), int):
    errors.append("prowlarr.port missing")
if not pw.get("download_client"):
    errors.append("prowlarr.download_client missing")

qbt = m.get("qbt", {})
if not isinstance(qbt.get("categories"), list) or not qbt["categories"]:
    errors.append("qbt.categories missing/empty")
if not isinstance(qbt.get("port"), int):
    errors.append("qbt.port missing")

jf = m.get("jellyfin", {})
if not isinstance(jf.get("libraries"), list) or not jf["libraries"]:
    errors.append("jellyfin.libraries missing/empty")
if not isinstance(jf.get("port"), int):
    errors.append("jellyfin.port missing")

for key in ("jellyseerr", "bazarr"):
    if not isinstance(m.get(key), dict) or not isinstance(m[key].get("port"), int):
        errors.append(f"{key}.port missing")
if not isinstance(m.get("bazarr", {}).get("auth_type"), str):
    errors.append("bazarr.auth_type missing")
if not m.get("authentik", {}).get("ldap_outpost"):
    errors.append("authentik.ldap_outpost missing")

if errors:
    for e in errors:
        print(f"DRIFT-FAIL: manifest schema: {e}", file=sys.stderr)
    sys.exit(1)
print(f"ok: manifest {sys.argv[1]} schema valid")
sys.exit(0)
PYEOF
  exit $?
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "DRIFT-FAIL: $ENV_FILE not found - cannot read credentials" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
USER="${MONARCH_USERNAME:-admin}"
PASS="${MONARCH_PASSWORD:-monarch8}"

if [ ! -f "$MANIFEST" ]; then
  echo "DRIFT-FAIL: invariants manifest $MANIFEST not found - run monarch-init first" >&2
  exit 1
fi

FAILS=0
FAIL_LINES=()
say()  { [ "$QUIET" -eq 0 ] && echo "$@"; }
fail() { echo "DRIFT-FAIL: $*" >&2; FAIL_LINES+=("$*"); FAILS=$((FAILS + 1)); }

# ── Telegram alerting (optional) ──────────────────────────────────────────
# Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to get a push message
# when drift is found. The bot must be created via @BotFather; the chat id
# can be found with @userinfobot (or use a group the bot is added to).
# A test message can be sent with:
#   scripts/drift-check.sh --test-telegram
notify_telegram() {  # notify_telegram <subject> <message...> -> 0 on success
  local subject="$1"; shift
  local msg="$subject" line
  for line in "$@"; do msg+=$'\n'"$line"; done
  local reply
  reply=$(curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$msg" \
    --data-urlencode "disable_web_page_preview=true")
  echo "$reply" | grep -q '"ok":true' || { echo "Telegram send failed: $(echo "$reply" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("description",d))' 2>/dev/null || echo "$reply")" >&2; return 1; }
}

json_get() {  # json_get <url> <header...> -> echoes body, 0 on HTTP 200
  local url="$1"; shift
  local code
  code=$(curl -s -o /tmp/drift-body.$$ -w "%{http_code}" "$url" "$@")
  if [ "$code" = "200" ]; then
    cat /tmp/drift-body.$$
    return 0
  fi
  echo ""
  return 1
}

api_key_for() {  # api_key_for <svc> -> echoes api key
  local svc="$1" path
  for path in "/docker/appdata/$svc/config.xml" "/docker/appdata/$svc/config/config.xml"; do
    [ -f "$path" ] || continue
    grep -oP '(?<=<ApiKey>)[^<\s]+' "$path" 2>/dev/null | head -1
    return 0
  done
  return 1
}

# ── Load the invariants manifest (single source of truth) ─────────────────
# Each extractor prints rows consumed by the loops below.
arr_rows()    { python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
for a in m['arr_apps']:
    print(f\"{a['svc']}|{a['port']}|{a['api']}|{a['root_folder']}|{a['media']}\")
"; }
manifest_val()  { python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
print(m$1)
"; }
manifest_list() { python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
for x in m$1:
    print(x)
"; }

# ───────────────────────────────────────────────────────────────────────────
# *arr apps
# ───────────────────────────────────────────────────────────────────────────
while IFS='|' read -r svc port api root media; do
  [ -n "$svc" ] || continue
  key=$(api_key_for "$svc")
  if [ -z "$key" ]; then
    fail "$svc: API key not found in /docker/appdata/$svc/config.xml"
    continue
  fi
  hdr=(-H "X-Api-Key: $key")

  body=$(json_get "http://localhost:$port/api/$api/config/host" "${hdr[@]}")
  if [ -z "$body" ]; then
    fail "$svc: /api/$api/config/host unreachable (HTTP != 200)"
    continue
  fi
  method=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('authenticationMethod',''))" 2>/dev/null)
  if [ "$method" != "forms" ]; then
    fail "$svc: forms auth not configured (authenticationMethod='$method')"
  fi

  body=$(json_get "http://localhost:$port/api/$api/rootfolder" "${hdr[@]}")
  found=0
  if [ -n "$body" ]; then
    found=$(echo "$body" | python3 -c "
import sys, json
try:
    rfs = json.load(sys.stdin)
    print(1 if any(str(r.get('path','')).rstrip('/') == '$root'.rstrip('/') for r in rfs) else 0)
except Exception:
    print(0)
" 2>/dev/null)
  fi
  [ "$found" = "1" ] || fail "$svc: root folder $root missing"

  body=$(json_get "http://localhost:$port/api/$api/downloadclient" "${hdr[@]}")
  has_qbt=0
  if [ -n "$body" ]; then
    has_qbt=$(echo "$body" | python3 -c "
import sys, json
try:
    cs = json.load(sys.stdin)
    print(1 if any(c.get('implementation') == 'QBittorrent' for c in cs) else 0)
except Exception:
    print(0)
" 2>/dev/null)
  fi
  [ "$has_qbt" = "1" ] || fail "$svc: qBittorrent download client missing"

  say "ok: $svc (auth=$method, root=$([ "$found" = 1 ] && echo yes || echo no), qbt=$([ "$has_qbt" = 1 ] && echo yes || echo no))"
done < <(arr_rows)

# ───────────────────────────────────────────────────────────────────────────
# Prowlarr
# ───────────────────────────────────────────────────────────────────────────
PROW_PORT=$(manifest_val "['prowlarr']['port']")
pkey=$(api_key_for "prowlarr")
if [ -n "$pkey" ]; then
  phdr=(-H "X-Api-Key: $pkey")

  body=$(json_get "http://localhost:$PROW_PORT/api/v1/downloadclient" "${phdr[@]}")
  has_qbt=0
  if [ -n "$body" ]; then
    has_qbt=$(echo "$body" | python3 -c "
import sys, json
try:
    cs = json.load(sys.stdin)
    print(1 if any(c.get('implementation') == 'QBittorrent' for c in cs) else 0)
except Exception:
    print(0)
" 2>/dev/null)
  fi
  [ "$has_qbt" = "1" ] || fail "prowlarr: qBittorrent download client missing"

  body=$(json_get "http://localhost:$PROW_PORT/api/v1/applications" "${phdr[@]}")
  apps=""
  if [ -n "$body" ]; then
    apps=$(echo "$body" | python3 -c "
import sys, json
try:
    print(','.join(sorted(a.get('implementation','') for a in json.load(sys.stdin))))
except Exception:
    print('')
" 2>/dev/null)
  fi
  while IFS= read -r want; do
    [ -n "$want" ] || continue
    case ",$apps," in
      *",$want,"*) : ;;
      *) fail "prowlarr: app $want not registered (have: '$apps')" ;;
    esac
  done < <(manifest_list "['prowlarr']['apps']")
  say "ok: prowlarr (qbt=$([ "$has_qbt" = 1 ] && echo yes || echo no), apps='$apps')"
else
  fail "prowlarr: API key not found"
fi

# ───────────────────────────────────────────────────────────────────────────
# qBittorrent (login + categories)
# ───────────────────────────────────────────────────────────────────────────
QBT_PORT=$(manifest_val "['qbt']['port']")
qbt_cj=/tmp/drift-qbt.$$.cookies
rm -f "$qbt_cj"
qbt_code=$(curl -s -o /dev/null -w "%{http_code}" -c "$qbt_cj" \
  -d "username=$USER&password=$PASS" \
  "http://localhost:$QBT_PORT/api/v2/auth/login")
# qBittorrent >= 5.2 returns 204 on success; older returns 200.
if [ "$qbt_code" != "204" ] && [ "$qbt_code" != "200" ]; then
  fail "qbittorrent: WebUI login failed (HTTP $qbt_code)"
  rm -f "$qbt_cj"
else
  cats=$(curl -s -b "$qbt_cj" "http://localhost:$QBT_PORT/api/v2/torrents/categories" | \
    python3 -c "
import sys, json
try:
    print(','.join(sorted(json.load(sys.stdin).keys())))
except Exception:
    print('')
" 2>/dev/null)
  missing=""
  while IFS= read -r want; do
    [ -n "$want" ] || continue
    case ",$cats," in
      *",$want,"*) : ;;
      *) missing="$missing $want" ;;
    esac
  done < <(manifest_list "['qbt']['categories']")
  [ -z "$missing" ] || fail "qbittorrent: categories missing:$missing (have: '$cats')"
  say "ok: qbittorrent (login ok, categories='$cats')"
  rm -f "$qbt_cj"
fi

# ───────────────────────────────────────────────────────────────────────────
# Jellyfin (admin login + libraries)
# ───────────────────────────────────────────────────────────────────────────
JF_PORT=$(manifest_val "['jellyfin']['port']")
jf_auth='MediaBrowser Client="Drift Check", Device="Linux", DeviceId="drift-check-001", Version="1.0.0"'
jf_code=$(curl -s -o /tmp/drift-jf.$$ -w "%{http_code}" \
  -X POST "http://localhost:$JF_PORT/Users/AuthenticateByName" \
  -H "Content-Type: application/json" \
  -H "X-Emby-Authorization: $jf_auth" \
  -d "{\"Username\":\"$USER\",\"Pw\":\"$PASS\"}")
if [ "$jf_code" != "200" ]; then
  fail "jellyfin: admin login failed (HTTP $jf_code)"
else
  jf_token=$(python3 -c "import sys,json; print(json.load(open('/tmp/drift-jf.$$')).get('AccessToken',''))" 2>/dev/null)
  if [ -z "$jf_token" ]; then
    fail "jellyfin: login returned no AccessToken"
  else
    libs=$(curl -s "http://localhost:$JF_PORT/Library/VirtualFolders" -H "X-Emby-Token: $jf_token" | \
      python3 -c "
import sys, json
try:
    print(','.join(sorted(v.get('Name','') for v in json.load(sys.stdin))))
except Exception:
    print('')
" 2>/dev/null)
    missing=""
    while IFS= read -r want; do
      [ -n "$want" ] || continue
      case ",$libs," in
        *",$want,"*) : ;;
        *) missing="$missing '$want'" ;;
      esac
    done < <(manifest_list "['jellyfin']['libraries']")
    [ -z "$missing" ] || fail "jellyfin: libraries missing:$missing (have: '$libs')"
    say "ok: jellyfin (login ok, libraries='$libs')"
  fi
fi
rm -f /tmp/drift-jf.$$

# ───────────────────────────────────────────────────────────────────────────
# Jellyseerr (initialized + Jellyfin sign-in)
# ───────────────────────────────────────────────────────────────────────────
JSERR_PORT=$(manifest_val "['jellyseerr']['port']")
js_body=$(json_get "http://localhost:$JSERR_PORT/api/v1/settings/public")
if [ -z "$js_body" ]; then
  fail "jellyseerr: /api/v1/settings/public unreachable"
else
  js_init=$(echo "$js_body" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('initialized') else 'no')" 2>/dev/null)
  js_login=$(echo "$js_body" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('mediaServerLogin') else 'no')" 2>/dev/null)
  [ "$js_init" = "yes" ] || fail "jellyseerr: not initialized (run monarch-init)"
  [ "$js_login" = "yes" ] || fail "jellyseerr: Jellyfin sign-in not enabled"
  say "ok: jellyseerr (initialized=$js_init, jellyfinLogin=$js_login)"
fi

# ───────────────────────────────────────────────────────────────────────────
# Bazarr (auth configured)
# ───────────────────────────────────────────────────────────────────────────
BZ_PORT=$(manifest_val "['bazarr']['port']")
BZ_AUTH_TYPE=$(manifest_val "['bazarr']['auth_type']")
bz_key=""
if [ -f /docker/appdata/bazarr/config/config.yaml ]; then
  bz_key=$(grep -oP '^\s*apikey:\s*\K[^\s]+' /docker/appdata/bazarr/config/config.yaml 2>/dev/null | head -1)
fi
if [ -z "$bz_key" ]; then
  fail "bazarr: API key not found in config.yaml"
else
  bz_body=$(json_get "http://localhost:$BZ_PORT/api/system/settings" -H "X-API-KEY: $bz_key")
  if [ -z "$bz_body" ]; then
    fail "bazarr: /api/system/settings unreachable with API key"
  else
    bz_type=$(echo "$bz_body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('auth',{}).get('type') or '')" 2>/dev/null)
    [ "$bz_type" = "$BZ_AUTH_TYPE" ] || fail "bazarr: basic auth not configured (type='$bz_type')"
    say "ok: bazarr (auth type='$bz_type')"
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Authentik LDAP outpost (only when the API is reachable/configured)
# ───────────────────────────────────────────────────────────────────────────
AK_OUTPOST=$(manifest_val "['authentik']['ldap_outpost']")
if [ -n "${AUTHENTIK_BASE_URL:-}" ] && [ -n "${AUTHENTIK_BOOTSTRAP_TOKEN:-}" ]; then
  ak_base="${AUTHENTIK_BASE_URL%/}"
  ak_code=$(curl -s -o /tmp/drift-ak.$$ -w "%{http_code}" \
    -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
    "$ak_base/api/v3/outposts/instances/")
  if [ "$ak_code" = "200" ]; then
    ak_outpost=$(python3 -c "
import sys, json
try:
    d = json.load(open('/tmp/drift-ak.$$'))
    hits = [o.get('name','') for o in d.get('results',[]) if o.get('name') == '$AK_OUTPOST']
    print('yes' if hits else 'no')
except Exception:
    print('no')
" 2>/dev/null)
    [ "$ak_outpost" = "yes" ] || fail "authentik: LDAP outpost $AK_OUTPOST not found"
    say "ok: authentik (LDAP outpost present)"
  else
    fail "authentik: outposts API unreachable (HTTP $ak_code)"
  fi
  rm -f /tmp/drift-ak.$$
fi

# ───────────────────────────────────────────────────────────────────────────
# Nginx Proxy Manager (local container or NPM_MODE=remote + credentials)
# ───────────────────────────────────────────────────────────────────────────
# Verifies the live NPM proxy hosts match scripts/npm-hosts.conf via
# npm-proxy-hosts.py --check (read-only, exit 1 on drift). Runs when the
# local NPM container is up, or when NPM_MODE=remote points at an external
# server with NPM_ADMIN_* credentials set; skipped otherwise.
npm_container=0
if command -v docker >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx nginx-proxy-manager; then
  npm_container=1
fi
if [ "$npm_container" -eq 1 ] \
   || { [ "${NPM_MODE:-local}" = "remote" ] \
        && [ -n "${NPM_ADMIN_EMAIL:-}" ] && [ -n "${NPM_ADMIN_PASSWORD:-}" ]; }; then
  if [ -z "${NPM_ADMIN_EMAIL:-}" ] || [ -z "${NPM_ADMIN_PASSWORD:-}" ]; then
    say "ok: npm (skipped - NPM_ADMIN_EMAIL/PASSWORD not set)"
  else
    if npm_out=$(python3 scripts/npm-proxy-hosts.py --check 2>&1); then
      say "ok: npm proxy hosts match npm-hosts.conf"
      [ "$QUIET" -eq 0 ] && echo "$npm_out" | sed 's/^/  /'
    else
      fail "npm: proxy hosts drifted from npm-hosts.conf"
      echo "$npm_out" | sed 's/^/  /' >&2
    fi
  fi
else
  say "ok: npm (skipped - no local NPM container and NPM_MODE!=remote)"
fi

# ───────────────────────────────────────────────────────────────────────────
# Infra (host-level, only when the docker CLI works)
# ───────────────────────────────────────────────────────────────────────────
DRIFT_DISK_MAX_PCT="${DRIFT_DISK_MAX_PCT:-90}"
DRIFT_MAX_RESTARTS="${DRIFT_MAX_RESTARTS:-10}"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # 1. Disk usage on the two host mounts everything reads/writes.
  for mp in /data /docker/appdata; do
    [ -d "$mp" ] || continue
    pct=$(df -P "$mp" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    if [ -n "$pct" ] && [ "$pct" -ge "$DRIFT_DISK_MAX_PCT" ]; then
      fail "infra: $mp at ${pct}% disk usage (>= ${DRIFT_DISK_MAX_PCT}%)"
    else
      say "ok: infra disk $mp at ${pct}%"
    fi
  done

  # 2. Container health: crash-looping (high RestartCount) or running a stale
  #    image (watchtower pulled a newer one but the container was never
  #    recreated). Probed containers are the *arr apps + the fixed set.
  while IFS= read -r cname; do
    [ -n "$cname" ] || continue
    rc=$(docker inspect -f '{{.RestartCount}}' "$cname" 2>/dev/null || echo 0)
    if [ "$rc" -ge "$DRIFT_MAX_RESTARTS" ]; then
      fail "infra: $cname restarted $rc times (>= ${DRIFT_MAX_RESTARTS}) - possible crash loop"
    fi
    # Image the container was created from vs the current image for its tag.
    cimg=$(docker inspect -f '{{.Image}}' "$cname" 2>/dev/null || echo "")
    tag=$(docker inspect -f '{{.Config.Image}}' "$cname" 2>/dev/null || echo "")
    if [ -n "$cimg" ] && [ -n "$tag" ]; then
      cur=$(docker image inspect -f '{{.Id}}' "$tag" 2>/dev/null || echo "")
      if [ -n "$cur" ] && [ "$cimg" != "$cur" ]; then
        fail "infra: $cname runs a stale image (container ${cimg:0:12}, current ${cur:0:12}) - recreate needed"
      fi
    fi
    say "ok: infra container $cname (restarts=$rc)"
  done < <({ arr_rows | cut -d'|' -f1; echo prowlarr; echo qbittorrent; echo jellyfin; echo jellyseerr; echo bazarr; echo homarr; echo nginx-proxy-manager; } | sort -u)
fi

rm -f /tmp/drift-body.$$

# ── --test-telegram: verify the bot without waiting for drift ─────────────
if [ "$TEST_TG" -eq 1 ]; then
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "DRIFT-FAIL: --test-telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env" >&2
    exit 1
  fi
  if notify_telegram "drift-check test from $(hostname)" "Telegram alerting configured and reachable."; then
    echo "drift-check: test message sent to Telegram chat $TELEGRAM_CHAT_ID"
    exit 0
  fi
  exit 1
fi

# ── --heal: re-run monarch-init on drift, then re-verify ──────────────────
# Rate limit: remember the last heal attempt so a persistently drifted stack
# escalates to an alert instead of looping init every timer tick.
DRIFT_HEAL_MIN_INTERVAL="${DRIFT_HEAL_MIN_INTERVAL:-3600}"
HEAL_STATE="/docker/appdata/init/drift-heal-last"
HEAL_SUPPRESSED=0
if [ "$FAILS" -gt 0 ] && [ "$HEAL" -eq 1 ]; then
  now=$(date +%s)
  last=0
  [ -f "$HEAL_STATE" ] && last=$(cat "$HEAL_STATE" 2>/dev/null || echo 0)
  if [ $((now - last)) -lt "$DRIFT_HEAL_MIN_INTERVAL" ]; then
    HEAL_SUPPRESSED=1
    echo "drift-check: heal suppressed - last attempt $((now - last))s ago (< ${DRIFT_HEAL_MIN_INTERVAL}s) - escalating to alert" >&2
  else
    echo "drift-check: $FAILS issue(s) found - re-running monarch-init to heal..." >&2
    echo "$now" > "$HEAL_STATE" 2>/dev/null || true
    if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx monarch-init; then
      # Re-run the existing one-shot container (created by `docker compose up`)
      # and wait for it to exit. init waits up to 15 min per service on first
      # boot; on a warm stack it exits in a minute or two.
      docker start monarch-init >/dev/null 2>&1 || true
      for _ in $(seq 1 450); do
        st=$(docker inspect -f '{{.State.Status}}' monarch-init 2>/dev/null || echo gone)
        [ "$st" = "exited" ] && break
        sleep 2
      done
    else
      # No container yet (fresh stack): docker compose run is synchronous, so
      # this blocks until init finishes on its own.
      docker compose -f docker-compose.yml run --rm monarch-init >/dev/null 2>&1 || true
    fi
    echo "drift-check: monarch-init finished - re-verifying..." >&2
    # Re-run the check suite WITHOUT --heal (avoids a heal loop). The exit code
    # of that run reports whether the stack healed.
    exec bash "$0" --quiet
  fi
fi

if [ "$FAILS" -gt 0 ]; then
  echo "drift-check: $FAILS issue(s) found" >&2
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    if [ "$HEAL_SUPPRESSED" -eq 1 ]; then
      FAIL_LINES+=("heal suppressed by rate limit (DRIFT_HEAL_MIN_INTERVAL=${DRIFT_HEAL_MIN_INTERVAL}s) - persistent drift")
    fi
    notify_telegram "⚠️ Monarch drift check failed on $(hostname)" "${FAIL_LINES[@]}" || true
  fi
  exit 1
fi
echo "drift-check: all live-stack invariants OK"
exit 0