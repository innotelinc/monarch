#!/usr/bin/env bash
set -uo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# drift-check.sh - live-stack health check (read-only)
#
# Probes the running Monarch stack and verifies the invariants monarch-init
# is supposed to maintain. It NEVER writes anything - it only reads API keys
# from /docker/appdata and issues GET/POST checks against the services.
#
# Exits non-zero when drift is found, so it can be run from a systemd timer
# (systemd/monarch-drift-check.{service,timer}) or cron to alert on drift.
#
# Checks:
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
#
# Usage:
#   scripts/drift-check.sh            # check the live stack
#   scripts/drift-check.sh --quiet    # only print drift lines (for cron)
# ═══════════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")/.." || exit 1

QUIET=0
TEST_TG=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --test-telegram) TEST_TG=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ENV_FILE="${MONARCH_ENV:-.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "DRIFT-FAIL: $ENV_FILE not found - cannot read credentials" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
USER="${MONARCH_USERNAME:-admin}"
PASS="${MONARCH_PASSWORD:-monarch8}"

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

# ───────────────────────────────────────────────────────────────────────────
# *arr apps
# ───────────────────────────────────────────────────────────────────────────
arr_apps=(
  "sonarr|8989|v3|/data/media/tv|tv"
  "radarr|7878|v3|/data/media/movies|movies"
  "lidarr|8686|v1|/data/media/music|music"
  "whisparr|6969|v3|/data/media/xxx|xxx"
)

for entry in "${arr_apps[@]}"; do
  IFS='|' read -r svc port api root media <<< "$entry"
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
done

# ───────────────────────────────────────────────────────────────────────────
# Prowlarr
# ───────────────────────────────────────────────────────────────────────────
PROW_PORT=9696
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
  for want in Sonarr Radarr Lidarr Whisparr; do
    case ",$apps," in
      *",$want,"*) : ;;
      *) fail "prowlarr: app $want not registered (have: '$apps')" ;;
    esac
  done
  say "ok: prowlarr (qbt=$([ "$has_qbt" = 1 ] && echo yes || echo no), apps='$apps')"
else
  fail "prowlarr: API key not found"
fi

# ───────────────────────────────────────────────────────────────────────────
# qBittorrent (login + categories)
# ───────────────────────────────────────────────────────────────────────────
QBT_PORT=8080
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
  for want in movies tv music xxx; do
    case ",$cats," in
      *",$want,"*) : ;;
      *) missing="$missing $want" ;;
    esac
  done
  [ -z "$missing" ] || fail "qbittorrent: categories missing:$missing (have: '$cats')"
  say "ok: qbittorrent (login ok, categories='$cats')"
  rm -f "$qbt_cj"
fi

# ───────────────────────────────────────────────────────────────────────────
# Jellyfin (admin login + libraries)
# ───────────────────────────────────────────────────────────────────────────
JF_PORT=8096
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
    for want in Movies "TV Shows" Music Other; do
      case ",$libs," in
        *",$want,"*) : ;;
        *) missing="$missing '$want'" ;;
      esac
    done
    [ -z "$missing" ] || fail "jellyfin: libraries missing:$missing (have: '$libs')"
    say "ok: jellyfin (login ok, libraries='$libs')"
  fi
fi
rm -f /tmp/drift-jf.$$

# ───────────────────────────────────────────────────────────────────────────
# Jellyseerr (initialized + Jellyfin sign-in)
# ───────────────────────────────────────────────────────────────────────────
JSERR_PORT=5055
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
BZ_PORT=6767
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
    [ "$bz_type" = "basic" ] || fail "bazarr: basic auth not configured (type='$bz_type')"
    say "ok: bazarr (auth type='$bz_type')"
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Authentik LDAP outpost (only when the API is reachable/configured)
# ───────────────────────────────────────────────────────────────────────────
if [ -n "${AUTHENTIK_BASE_URL:-}" ] && [ -n "${AUTHENTIK_BOOTSTRAP_TOKEN:-}" ]; then
  ak_base="${AUTHENTIK_BASE_URL%/}"
  ak_code=$(curl -s -o /tmp/drift-ak.$$ -w "%{http_code}" \
    -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
    "$ak_base/api/v3/core/outposts/")
  if [ "$ak_code" = "200" ]; then
    ak_outpost=$(python3 -c "
import sys, json
try:
    d = json.load(open('/tmp/drift-ak.$$'))
    hits = [o.get('name','') for o in d.get('results',[]) if o.get('name') == '${AUTHENTIK_LDAP_OUTPOST:-jellyfin-ldap}']
    print('yes' if hits else 'no')
except Exception:
    print('no')
" 2>/dev/null)
    [ "$ak_outpost" = "yes" ] || fail "authentik: LDAP outpost ${AUTHENTIK_LDAP_OUTPOST:-jellyfin-ldap} not found"
    say "ok: authentik (LDAP outpost present)"
  else
    fail "authentik: outposts API unreachable (HTTP $ak_code)"
  fi
  rm -f /tmp/drift-ak.$$
fi

rm -f /tmp/drift-body.$$

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

if [ "$FAILS" -gt 0 ]; then
  echo "drift-check: $FAILS issue(s) found" >&2
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    notify_telegram "⚠️ Monarch drift check failed on $(hostname)" "${FAIL_LINES[@]}" || true
  fi
  exit 1
fi
echo "drift-check: all live-stack invariants OK"
exit 0