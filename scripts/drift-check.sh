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
#     - authMethod = external: the Cerulean Authentik auth_request gate is the
#       ONLY login, so the app must not also show its own Forms prompt
#     - expected media root folder present
#     - qBittorrent download client present
#   Prowlarr:
#     - qBittorrent download client present
#     - Sonarr/Radarr/Lidarr/Whisparr apps registered
#   qBittorrent:
#     - WebUI login works with the shared credentials
#     - movies/tv/music/xxx categories exist
#   Jellyfin:
#     - admin API access works: the shared credentials when they still match,
#       otherwise the exported admin token (init writes it; a diverged local
#       admin password is reported as a note, not a failure)
#     - media libraries exist
#   Jellyseerr:
#     - initialized, Jellyfin sign-in enabled
#   Cerulean Vault (SecretOps):
#     - .env holds materialized values, with no unresolved vault:// reference
#   Magnate (RevenueOps, when MAGNATE_URL is set):
#     - every managed user's Jellyfin policy matches its Magnate tier
#       (scripts/magnate-entitlements.py --check, read-only)
#   Bazarr:
#     - API key readable, basic auth configured
#   Authentik:
#     - LDAP outpost provisioned (when AUTHENTIK_BASE_URL is set)
#   Nginx Proxy Manager:
#     - scripts/check-proxy-ports.py: npm-hosts.conf forwards to ports
#       docker-compose.yml publishes (static, no credentials needed)
#     - when the local NPM container runs (or NPM_MODE=remote with
#       credentials): live proxy hosts match scripts/npm-hosts.conf
#       (npm-proxy-hosts.py --check: subdomain, forward host, port, websockets)
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
# Set when the live NPM proxy hosts drift from npm-hosts.conf. The healer is
# `npm-proxy-hosts.py` (the reconciler), NOT monarch-init — tracked here because
# the two are fixed by different programs.
NPM_DRIFT=0
say()  { [ "$QUIET" -eq 0 ] && echo "$@"; }
indent() { sed 's/^/  /'; }   # prefix each line of stdin with two spaces
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
  # `external` is the correct value, not `forms`: init/init.py
  # set_monarch_app_auth() switches these apps to *arr's "a reverse proxy
  # already authenticated this user" mode so the Cerulean Authentik gate on
  # <svc>.MONARCH_DOMAIN is the only login. Forms auth here would be a
  # SECOND prompt after SSO.
  if [ "$method" != "external" ]; then
    fail "$svc: the Cerulean SSO gate is not the only login (authenticationMethod='$method', expected 'external')"
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
# Jellyfin (admin API access + libraries)
# ───────────────────────────────────────────────────────────────────────────
JF_PORT=$(manifest_val "['jellyfin']['port']")
JELLYFIN_KEY_FILE=/docker/appdata/init/jellyfin-api-key.txt
# This build (the pinned v12 image) reads the MediaBrowser header from
# `Authorization`, NOT `X-Emby-Authorization`: the X-Emby-* spellings are
# rejected with HTTP 400 ("Value cannot be null. (Parameter 'request.App')")
# even with valid credentials, which is what made this check report a false
# failure. The header value is the same either way.
jf_auth='MediaBrowser Client="Drift Check", Device="Linux", DeviceId="drift-check-001", Version="1.0.0"'
jf_code=$(curl -s -o /tmp/drift-jf.$$ -w "%{http_code}" \
  -X POST "http://localhost:$JF_PORT/Users/AuthenticateByName" \
  -H "Content-Type: application/json" \
  -H "Authorization: $jf_auth" \
  -d "{\"Username\":\"$USER\",\"Pw\":\"$PASS\"}")
jf_token=""
jf_via=""
if [ "$jf_code" = "200" ]; then
  jf_token=$(python3 -c "import sys,json; print(json.load(open('/tmp/drift-jf.$$')).get('AccessToken',''))" 2>/dev/null)
  jf_via="login"
  [ -n "$jf_token" ] || fail "jellyfin: login returned no AccessToken"
elif [ -f "$JELLYFIN_KEY_FILE" ]; then
  # Fallback for when the shared credentials do not log in: Jellyfin's local
  # admin password can diverge from MONARCH_PASSWORD (the password endpoint
  # needs the CURRENT password), and that is reported rather than failed on.
  # The credential that does not depend on the password is the durable admin
  # API key at $JELLYFIN_KEY_FILE - it survives a password change, unlike the
  # session tokens, which is why init mints a key instead of a token.
  jf_token=$(cat "$JELLYFIN_KEY_FILE" 2>/dev/null)
  jf_via="exported token"
  if [ -n "$jf_token" ]; then
    say "note: jellyfin admin login with the shared credentials failed (HTTP $jf_code) - using the exported admin API key; re-align the password with scripts/jellyfin-admin-password.py --set"
  fi
fi
if [ -z "$jf_token" ]; then
  fail "jellyfin: admin login failed (HTTP $jf_code) and no exported token at $JELLYFIN_KEY_FILE"
else
  libs=$(curl -s "http://localhost:$JF_PORT/Library/VirtualFolders" \
    -H "Authorization: MediaBrowser Token=$jf_token" | \
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
  say "ok: jellyfin ($jf_via, libraries='$libs')"
fi
rm -f /tmp/drift-jf.$$

# ───────────────────────────────────────────────────────────────────────────
# Jellyfin API keys held by the apps
# ───────────────────────────────────────────────────────────────────────────
# Rotating a key means updating whoever holds it, and a half-finished rotation
# (new key minted, the old one deleted, the app still configured with it) leaves
# the app authenticating with a token Jellyfin has forgotten. Nothing else
# notices: the container is up, its own UI answers, and only its requests to
# Jellyfin fail. Read each app's copy and prove Jellyfin still accepts it.
if app_keys_out=$(python3 scripts/jellyfin-admin-password.py --check-apps 2>&1); then
  say "ok: the Jellyfin API keys held by the apps still authenticate"
  [ "$QUIET" -eq 0 ] && printf '%s\n' "$app_keys_out" | indent
else
  fail "jellyfin: an app's stored API key no longer authenticates"
  printf '%s\n' "$app_keys_out" | indent >&2
fi

# ───────────────────────────────────────────────────────────────────────────
# The apps' own sign-in methods
# ───────────────────────────────────────────────────────────────────────────
# The gateways prove a Cerulean session for the *name*; they say nothing about
# the credential form behind it. Two of these apps keep a second, local store of
# credentials, and each is a way in that no gateway covers: Seerr's
# email-and-password sign-in, and any Jellyfin account its own database owns
# instead of the LDAP outpost. That is why a user disabled in Authentik could
# still sign in to them.
#
# Neither is configuration in this repo, so nothing else can see it drift back:
# Seerr re-enables local sign-in on a settings import, and Jellyfin's first-run
# wizard or an administrator in its UI creates a local account. Both scripts are
# read-only here — `--apply` is the operator's move (docs/operations.md).
#
# Exit 2 is "cannot judge" in both, and it is reported as a skip rather than a
# pass: a key that could not be read, or a build that does not say which provider
# owns an account, is not evidence that the posture holds.
seerr_login_out=$(python3 scripts/seerr-login-methods.py --check 2>&1)
seerr_login_code=$?
if [ "$seerr_login_code" -eq 0 ]; then
  say "ok: Seerr's only sign-in is the Cerulean (Jellyfin) account"
elif [ "$seerr_login_code" -eq 2 ]; then
  say "note: Seerr's sign-in methods could not be read (skipped) - $(printf '%s' "$seerr_login_out" | tail -1)"
else
  fail "jellyseerr: local (email + password) sign-in is enabled - run scripts/seerr-login-methods.py --apply"
  printf '%s\n' "$seerr_login_out" | indent >&2
fi

jellyfin_login_out=$(python3 scripts/jellyfin-login-methods.py --check 2>&1)
jellyfin_login_code=$?
# The local accounts the deployment *keeps* are declared, not exempted in
# code: `JELLYFIN_LOCAL_ACCOUNTS` in .env lists the logins that cannot use the
# OIDC button (the TV and mobile clients), beside the break-glass
# `JELLYFIN_ADMIN_USER`. Declaring them there rather than in the checker is what
# keeps this honest — the account name is deployment state that changes, and the
# count is printed so the set cannot grow quietly.
if [ "$jellyfin_login_code" -eq 0 ]; then
  say "ok: Jellyfin's only local accounts are the ones this deployment declares"
elif [ "$jellyfin_login_code" -eq 2 ]; then
  say "note: Jellyfin's local accounts could not be judged (skipped) - $(printf '%s' "$jellyfin_login_out" | tail -1)"
else
  fail "jellyfin: a local account can sign in outside Authentik - declare it in JELLYFIN_LOCAL_ACCOUNTS (.env) if that is deliberate, otherwise run scripts/jellyfin-login-methods.py --apply"
  printf '%s\n' "$jellyfin_login_out" | indent >&2
fi

# Jellyfin's login page is published rather than gated (docker-compose.yml,
# 2026-09-18), so the page's own SSO button is now load-bearing: browsers sign
# in with it, and native clients with the LDAP account above. It is two halves
# that fail silently - a plugin config pointing at an issuer nobody signs in
# against, and a provider that never registered the callback - so it is judged
# here rather than left to whoever tries the button next. Exit 2 is "cannot
# judge" (no plugin installed on a fresh host), reported as a skip.
jellyfin_oidc_out=$(python3 scripts/jellyfin-oidc-sso.py --check 2>&1)
jellyfin_oidc_code=$?
if [ "$jellyfin_oidc_code" -eq 0 ]; then
  say "ok: Jellyfin's login page offers Cerulean Authentik and the provider takes its callback"
elif [ "$jellyfin_oidc_code" -eq 2 ]; then
  say "note: Jellyfin's OIDC SSO could not be judged (skipped) - $(printf '%s' "$jellyfin_oidc_out" | tail -1)"
else
  fail "jellyfin: the login page's SSO button is not wired - see scripts/jellyfin-oidc-sso.py --check, then docs/operations.md"
  printf '%s\n' "$jellyfin_oidc_out" | indent >&2
fi

# ...and the plugin *builds* are pinned rather than remembered. Neither plugin
# says which one it is: the OIDC plugin's meta.json ships no `sourceUrl` and the
# LDAP plugin reports an empty version list, so before init/jellyfin-plugins.json
# the only record of what was installed was a zip in /tmp - a rebuilt host came
# back with a login form and no SSO, a swapped build looked like nothing at all,
# and LDAP-Auth v23 installed beside v24 (which Jellyfin loads together, casting
# the plugin's config type across two load contexts) answered HTTP 500 for a
# correct password exactly as it did for a wrong one.
#
# So the check is a hash comparison against the pin, plus a count of the folders
# carrying each assembly: "a .dll is present" is not the question, and "there is
# one of them" is part of the answer.
plugins_out=$(python3 scripts/jellyfin-plugin-pin.py --check 2>&1)
plugins_code=$?
if [ "$plugins_code" -eq 0 ]; then
  say "ok: Jellyfin's LDAP and OIDC plugins are the pinned builds, one copy each"
elif [ "$plugins_code" -eq 2 ]; then
  say "note: the Jellyfin plugin pins could not be judged (skipped) - $(printf '%s' "$plugins_out" | tail -1)"
else
  fail "jellyfin: a plugin is not the pinned build, or is installed twice - run scripts/jellyfin-plugin-pin.py --install, then restart Jellyfin; see docs/operations.md"
  printf '%s\n' "$plugins_out" | indent >&2
fi

# The LDAP outpost is Jellyfin's credential store now that the page is published,
# and it fails in two ways that both surface as an HTTP 500 on the login form:
# the outpost's own API token drifting from the key Authentik holds (the
# container logs `403 Forbidden (Token invalid/expired)` and never starts its
# LDAP listener), and the bind password drifting from the bind user's password
# (the listener is up and every bind returns LDAP result code 49).
#
# Both happened on this deployment on 2026-09-18 and cost every Cerulean identity
# its login - Jellyfin answered 500, and Seerr's Jellyfin sign-in passed that 500
# straight through. `verify-ldap.py` was written for exactly this and was never
# run by anything, so it is run here: it binds as the bind user and performs the
# very search the plugin performs.
ldap_path_out=$(python3 scripts/verify-ldap.py 2>&1)
ldap_path_code=$?
if [ "$ldap_path_code" -eq 0 ]; then
  say "ok: Jellyfin's LDAP login path works end to end (outpost token + bind credential)"
else
  fail "jellyfin: the LDAP login path is broken (verify-ldap.py exit $ldap_path_code) - every Cerulean identity gets HTTP 500 from the login form; see docs/operations.md 'A Cerulean identity cannot sign in'"
  printf '%s\n' "$ldap_path_out" | indent >&2
fi

# ───────────────────────────────────────────────────────────────────────────
# Homarr's integration-secret encryption key
# ───────────────────────────────────────────────────────────────────────────
# Homarr encrypts every stored integration secret - including the Jellyfin API
# key it uses - with SECRET_ENCRYPTION_KEY. That key and the Homarr database are
# a single artifact: if .env and the running container disagree, Homarr cannot
# decrypt its own integrations. The dashboard stays up and the tiles simply
# stop working, which looks like a broken Jellyfin integration and not at all
# like a key problem. Assert the two sides agree rather than assume it.
#
# Never "fix" a mismatch by rotating: the running container's value is the one
# the stored ciphertext was made with. Put .env back to it (and restore them
# together - docs/operations.md -> Homarr).
homarr_key=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' homarr 2>/dev/null \
  | sed -n 's/^SECRET_ENCRYPTION_KEY=//p' | head -1)
if [ -z "$homarr_key" ]; then
  say "ok: Homarr encryption key (skipped - homarr not running)"
elif [ -z "${SECRET_ENCRYPTION_KEY:-}" ]; then
  fail "homarr: .env carries no SECRET_ENCRYPTION_KEY while the running container has one - its stored integration secrets cannot be read back"
elif [ "$SECRET_ENCRYPTION_KEY" = "$homarr_key" ]; then
  say "ok: .env and the running Homarr share one SECRET_ENCRYPTION_KEY"
else
  fail "homarr: SECRET_ENCRYPTION_KEY in .env does not match the running container - Homarr cannot decrypt its stored integration secrets (the Jellyfin API key among them). Restore the container's value; do NOT rotate"
fi

# ───────────────────────────────────────────────────────────────────────────
# Is the database Homarr has open the one on disk?
# ───────────────────────────────────────────────────────────────────────────
# SQLite is opened by inode. Replace db.sqlite under a *running* container - a
# migration, a restore - and the copy unlinks the file the server is using, so
# the server keeps reading a deleted inode while the real data sits on disk
# untouched. The symptom is not an error: Homarr looks freshly installed and
# redirects every route to the /init wizard, which on this SSO-only deployment
# cannot be completed at all (there is no username/password form) - the
# instance is simply unenterable. Measured on `.56` after the move: the DB on
# disk held the user, the board and its 44 items while `ls -l /proc/*/fd` in the
# container showed `/appdata/db/db.sqlite (deleted)`. `docker restart homarr` is
# the fix; this check is what turns it into a reported drift instead of an
# unexplained sign-in failure.
if ! docker inspect homarr >/dev/null 2>&1; then
  say "ok: Homarr database file (skipped - no homarr container)"
elif [ "$(docker inspect -f '{{.State.Running}}' homarr 2>/dev/null)" != "true" ]; then
  say "ok: Homarr database file (skipped - homarr not running)"
else
  homarr_deleted=$(docker exec homarr sh -c 'ls -l /proc/[0-9]*/fd 2>/dev/null | grep "db.sqlite (deleted)" | wc -l' 2>/dev/null || printf '0')
  case "$homarr_deleted" in
    ''|*[!0-9]*) say "ok: Homarr database file (not readable from this host)" ;;
    0) say "ok: the database Homarr has open is the file on disk" ;;
    *) fail "homarr: the running container still holds a DELETED db.sqlite open ($homarr_deleted handle(s)) - it is reading the file that was replaced under it, so it behaves like a fresh install: every route redirects to the /init wizard, which an SSO-only deployment cannot complete. Restart it: docker restart homarr" ;;
  esac
fi

# ───────────────────────────────────────────────────────────────────────────
# Cerulean Vault (SecretOps): does .env hold materialized values?
# ───────────────────────────────────────────────────────────────────────────
# Read-only. Cerulean Vault is the source of truth for secrets, and this stack
# has no runtime resolver — so .env must carry the resolved value, never a
# reference. A leftover `vault://` (or retired `infisical://`) reference would
# reach the container as a literal string: configured-looking, and not a
# credential. Which plaintext values are not in the store yet is what
# `python3 scripts/vault-migrate.py --from-env-file .env --dry-run` reports.
if [ ! -f "$ENV_FILE" ]; then
  say "ok: Cerulean Vault (skipped - no $ENV_FILE)"
elif vault_refs=$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=[[:space:]]*(vault|infisical)://' "$ENV_FILE"); then
  fail "cerulean-vault: unresolved secret references in $ENV_FILE"
  printf '%s\n' "$vault_refs" | indent >&2
else
  say "ok: Cerulean Vault (no unresolved references in $ENV_FILE)"
fi

# ───────────────────────────────────────────────────────────────────────────
# Magnate entitlements -> Jellyfin policies (RevenueOps)
# ───────────────────────────────────────────────────────────────────────────
# Read-only: asks Magnate what each managed user is entitled to and compares it
# with the Jellyfin policy (scripts/magnate-tiers.json is the tier map). Skipped
# when Magnate is not configured; a Jellyfin that cannot answer counts as a
# skip, not drift.
if [ -n "${MAGNATE_URL:-}" ]; then
  ent_out=$(python3 scripts/magnate-entitlements.py --check 2>&1)
  ent_rc=$?
  if [ "$ent_rc" -eq 0 ]; then
    say "ok: Magnate entitlements match Jellyfin policies"
    [ "$QUIET" -eq 0 ] && printf '%s\n' "$ent_out" | indent
  elif [ "$ent_rc" -eq 2 ]; then
    say "ok: Magnate entitlements (skipped - not configured)"
  else
    fail "magnate: Jellyfin policies drifted from the Magnate tier map"
    printf '%s\n' "$ent_out" | indent >&2
  fi
else
  say "ok: Magnate entitlements (skipped - MAGNATE_URL not set)"
fi

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
    # Bazarr must keep NO local login - bazarr.$MONARCH_DOMAIN carries the
    # Cerulean Authentik gate, and init deliberately leaves settings-auth-type
    # alone (Bazarr's API cannot express "no auth": it accepts only
    # None/basic/form and rejects an empty value with HTTP 406). The live API
    # reports type null for "no login", so normalise both sides: the manifest
    # may carry either the machine value ("none") or the older description.
    bz_type=$(echo "$bz_body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('auth',{}).get('type') or 'none')" 2>/dev/null)
    bz_want=$(printf '%s' "$BZ_AUTH_TYPE" | tr '[:upper:]' '[:lower:]')
    case "$bz_want" in
      *sso*|*none*) bz_want="none" ;;
    esac
    case "$bz_type" in
      ""|none|None|null) bz_type="none" ;;
    esac
    [ "$bz_type" = "$bz_want" ] || fail "bazarr: expected no local login (got type='$bz_type')"
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
# The static half: npm-hosts.conf must forward to ports docker-compose.yml
# actually publishes (scripts/check-proxy-ports.py). Mode-independent and needs
# no credentials - the live NPM can match a wrong row perfectly, which is how
# `tv` forwarded to the Zeus portal's :3001 while the guide published 3011.
if ports_out=$(python3 scripts/check-proxy-ports.py 2>&1); then
  say "ok: npm-hosts.conf ports match docker-compose publishes"
  [ "$QUIET" -eq 0 ] && printf '%s\n' "$ports_out" | indent
else
  fail "npm: npm-hosts.conf forwards to a port docker-compose does not publish"
  printf '%s\n' "$ports_out" | indent >&2
fi

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
      [ "$QUIET" -eq 0 ] && printf '%s\n' "$npm_out" | indent
    else
      NPM_DRIFT=1
      fail "npm: proxy hosts drifted from npm-hosts.conf"
      printf '%s\n' "$npm_out" | indent >&2
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
    # Proxy-host drift is healed by the reconciler, not by monarch-init: init
    # seeds the stack, it does not manage NPM. Without this the NPM half of the
    # heal is a no-op, the rate limiter suppresses the next attempt, and the same
    # alert returns every interval while nothing ever applies npm-hosts.conf.
    # Runs only when the check actually reported that drift.
    if [ "$NPM_DRIFT" -eq 1 ]; then
      echo "drift-check: reconciling NPM proxy hosts from npm-hosts.conf..." >&2
      if python3 scripts/npm-proxy-hosts.py >/dev/null 2>&1; then
        echo "drift-check: NPM proxy hosts reconciled" >&2
      else
        echo "drift-check: NPM reconciler failed (see: scripts/npm-proxy-hosts.py)" >&2
      fi
    fi
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