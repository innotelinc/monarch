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
#     - Transmission download client present
#   Prowlarr:
#     - Transmission download client present
#     - Sonarr/Radarr/Lidarr/Whisparr apps registered
#   Transmission:
#     - RPC reachable and download dir is /data/torrents
#     - no local WebUI login (Cerulean is the only door)
#   Jellyfin:
#     - admin API access works: the shared credentials when they still match,
#       otherwise the exported admin token (init writes it; a diverged local
#       admin password is reported as a note, not a failure)
#     - media libraries exist (read from the API: a server that does not answer is
#       reported as not serving rather than as a stack whose libraries are gone,
#       after waiting DRIFT_JELLYFIN_GRACE_SEC for a restart to settle)
#     - the login screen shows Monarch's own splash: the rendered asset is
#       installed under the data dir and branding.xml names it, rather than the
#       poster collage Jellyfin's own post-scan task regenerates
#       (scripts/jellyfin-splash.py --check, read-only)
#     - the app's own HTTPS listener is up on the Cerulean certificate for
#       media.innotel.us: EnableHttps is on, the file behind CertificatePath is
#       PKCS#12 and readable by the container's user, the served chain verifies
#       and still matches the material installed beside it
#       (scripts/jellyfin-tls.py --check, read-only)
#   Jellyseerr:
#     - initialized, Jellyfin sign-in enabled
#   Cerulean Vault (SecretOps):
#     - .env holds materialized values, with no unresolved vault:// reference
#   Magnate (RevenueOps, when MAGNATE_URL is set):
#     - every managed user's Jellyfin policy matches its Magnate tier
#       (scripts/magnate-entitlements.py --check, read-only)
#   Bazarr:
#     - API key readable, basic auth configured
#   ClipBucket:
#     - the install is finished, not merely running: schema, version row, admin
#       password, base_url, and the browser installer locked
#       (scripts/clipbucket-install.py --check)
#     - the catalogue holds the media library: one cb_video row, one playable
#       file under the name the app builds, and the card's thumbnails, per
#       movie and TV episode (scripts/clipbucket-library.py --check)
#     - the site serves it: every item's watch page emits a source with a file
#       behind it the web server will read (scripts/clipbucket-library.py
#       --serve-check)
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
#   --heal               when drift is found, clear any container stuck in
#                        Docker's Dead state (which needs a daemon restart; opt
#                        out with DRIFT_HEAL_DOCKER_RESTART=0), reconcile the
#                        stack with `docker compose up`, re-run monarch-init, then
#                        re-verify and report whether the stack healed. The
#                        re-verify waits for Jellyfin to answer an authenticated
#                        call first, because init restarts it and the boot is not
#                        over when the commands return (DRIFT_READY_TIMEOUT_SEC,
#                        default 300s; DRIFT_READY_WAIT=0 skips the wait).
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

tr = m.get("transmission", {})
if not isinstance(tr.get("categories"), list) or not tr["categories"]:
    errors.append("transmission.categories missing/empty")
if not isinstance(tr.get("port"), int):
    errors.append("transmission.port missing")
if not tr.get("download_dir"):
    errors.append("transmission.download_dir missing")

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
# There is deliberately no flag for the model-gateway check below: nothing in the
# heal can fix a refused key (only a person with a new one can), so it is not a
# thing for the heal to react to. It is reported, it counts as a failure, and it
# keeps failing until somebody re-keys the connection or switches it off.
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
  has_tr=0
  if [ -n "$body" ]; then
    has_tr=$(echo "$body" | python3 -c "
import sys, json
try:
    cs = json.load(sys.stdin)
    print(1 if any(c.get('implementation') == 'Transmission' for c in cs) else 0)
except Exception:
    print(0)
" 2>/dev/null)
  fi
  [ "$has_tr" = "1" ] || fail "$svc: Transmission download client missing"

  say "ok: $svc (auth=$method, root=$([ "$found" = 1 ] && echo yes || echo no), transmission=$([ "$has_tr" = 1 ] && echo yes || echo no))"
done < <(arr_rows)

# ───────────────────────────────────────────────────────────────────────────
# Prowlarr
# ───────────────────────────────────────────────────────────────────────────
PROW_PORT=$(manifest_val "['prowlarr']['port']")
pkey=$(api_key_for "prowlarr")
if [ -n "$pkey" ]; then
  phdr=(-H "X-Api-Key: $pkey")

  body=$(json_get "http://localhost:$PROW_PORT/api/v1/downloadclient" "${phdr[@]}")
  has_tr=0
  if [ -n "$body" ]; then
    has_tr=$(echo "$body" | python3 -c "
import sys, json
try:
    cs = json.load(sys.stdin)
    print(1 if any(c.get('implementation') == 'Transmission' for c in cs) else 0)
except Exception:
    print(0)
" 2>/dev/null)
  fi
  [ "$has_tr" = "1" ] || fail "prowlarr: Transmission download client missing"

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
  say "ok: prowlarr (transmission=$([ "$has_tr" = 1 ] && echo yes || echo no), apps='$apps')"
else
  fail "prowlarr: API key not found"
fi

# ───────────────────────────────────────────────────────────────────────────
# Transmission (RPC reachable + download dir + no local login)
# ───────────────────────────────────────────────────────────────────────────
# Transmission has no categories to enumerate: Servarr files each download in a
# subfolder of Transmission's download dir, so the invariants are the download
# dir itself (must be /data/torrents, outside every library) and the local login
# being off (the WebUI is loopback-only and transmission-sso is the only door).
TR_PORT=$(manifest_val "['transmission']['port']")
TR_WANT_DIR=$(manifest_val "['transmission']['download_dir']")
tr_state=$(python3 - "$TR_PORT" <<'PY'
import json, sys, urllib.error, urllib.request

base = f"http://localhost:{sys.argv[1]}"
payload = json.dumps({"method": "session-get", "arguments": {}}).encode()


def call(session_id):
    request = urllib.request.Request(f"{base}/transmission/rpc", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    if session_id:
        request.add_header("X-Transmission-Session-Id", session_id)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}").get("arguments") or {}


try:
    try:
        session = call("")
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        session = call(error.headers.get("X-Transmission-Session-Id", ""))
except Exception as error:  # noqa: BLE001
    print(f"ERR|{error}")
    raise SystemExit(0)
print(f"OK|{session.get('download-dir')}|{session.get('rpc-authentication-required')}")
PY
)
if [[ "$tr_state" == ERR\|* ]]; then
  fail "transmission: RPC unreachable (${tr_state#ERR|})"
else
  IFS='|' read -r _ tr_dir tr_auth <<< "$tr_state"
  if [ "$tr_dir" = "$TR_WANT_DIR" ]; then
    say "ok: transmission files downloads under $tr_dir"
  else
    fail "transmission: download dir is '$tr_dir', expected '$TR_WANT_DIR' - downloads land outside the tree the *arrs import from; re-run monarch-init (configure_transmission pins it)"
  fi
  if [ "$tr_auth" = "True" ]; then
    fail "transmission: the WebUI still requires its own login - every user meets a SECOND login after Cerulean; unset USER/PASS on the transmission service and recreate it, then re-run monarch-init"
  else
    say "ok: transmission keeps no local login - Cerulean is the only door"
  fi
fi

# The same question asked the other way round: does each *arr tell Transmission
# the category the manifest names? Servarr sends `category` as a subfolder of the
# download dir, so a client with no category (or the wrong one) drops the download
# somewhere the imports do not expect. The apps report this only as a warning on
# their own Health page, which is why it is checked here.
dload_cats_out=$(python3 scripts/arr-download-categories.py --check 2>&1)
dload_cats_code=$?
if [ "$dload_cats_code" -eq 0 ]; then
  say "ok: every *arr files its downloads under the manifest's Transmission categories"
elif [ "$dload_cats_code" -eq 1 ]; then
  say "note: Transmission or an *arr is not reachable from here (download categories skipped) - $(printf '%s' "$dload_cats_out" | grep -m1 FAIL)"
else
  fail "downloads: an *arr sends a category the manifest does not name, or Transmission's download dir drifted (arr-download-categories.py exit $dload_cats_code) - downloads land in a library root; run scripts/arr-download-categories.py (it restarts nothing)"
  printf '%s\n' "$dload_cats_out" | indent >&2
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
  # The status code is read, not just the body - and a non-200 is not judged on
  # the first read. This endpoint needs Jellyfin's media library service, which
  # is absent for the ~40s the server spends booting after a restart (its own
  # dashboard, a plugin install, or monarch-init through the heal all restart
  # it) and for as long as it is stopped. Its setup server listens first and
  # answers 503 for that whole window, which is what a scheduled run can land on;
  # both cases used to arrive here as an unparseable body and be reported as
  # `libraries missing: ... (have: '')` - a finding about a server that had
  # simply not finished starting. So a run that finds it not serving waits a
  # bounded moment to see whether it is coming back before calling it drift;
  # DRIFT_JELLYFIN_GRACE_SEC is how long (default 60s, one boot), 0 judges the
  # first read, and the heal's own wait is separate and longer.
  jf_grace="${DRIFT_JELLYFIN_GRACE_SEC:-60}"
  libs_code="000"
  jf_waited=0
  for _ in $(seq 1 $(( jf_grace / 3 + 1 ))); do
    libs_code=$(curl -s -o /tmp/drift-libs.$$ -w "%{http_code}" \
      "http://localhost:$JF_PORT/Library/VirtualFolders" \
      -H "Authorization: MediaBrowser Token=$jf_token")
    [ "$libs_code" = "200" ] && break
    sleep 3
    jf_waited=1
  done
  libs=$(python3 -c "
import sys, json
try:
    print(','.join(sorted(v.get('Name','') for v in json.load(sys.stdin))))
except Exception:
    print('')
" < /tmp/drift-libs.$$ 2>/dev/null)
  rm -f /tmp/drift-libs.$$
  if [ "$libs_code" != "200" ]; then
    fail "jellyfin: GET /Library/VirtualFolders answered HTTP $libs_code, not 200 even after waiting ${jf_grace}s - the server is not serving, so its libraries could not be read at all (a restart settles within one boot; a stopped server does not - see 'systemctl status jellyfin')"
  else
    [ "$jf_waited" = 0 ] || say "note: jellyfin was not serving on the first read - it answered after waiting (grace ${jf_grace}s)"
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

# Seerr's Owner is a row id, not a permission (`server/routes/user/index.ts`), so
# an install whose first account was the break-glass admin is owned by an account
# nobody signs in as - and the badge, plus the right to grant admin, never moves.
# seerr-owner.py reads the account from the invariants manifest and swaps the two
# rows; here we only judge it.
seerr_owner_out=$(python3 scripts/seerr-owner.py --check 2>&1)
seerr_owner_code=$?
if [ "$seerr_owner_code" -eq 0 ]; then
  say "ok: Seerr is owned by the account this estate names - $(printf '%s' "$seerr_owner_out" | grep -m1 'Owner ' | sed 's/^ *//')"
elif [ "$seerr_owner_code" -eq 2 ]; then
  say "note: Seerr's owner could not be judged (skipped) - $(printf '%s' "$seerr_owner_out" | tail -1)"
else
  fail "jellyseerr: Seerr is not owned by the account the manifest names - run scripts/seerr-owner.py --apply (nothing is deleted: the two accounts are swapped)"
  printf '%s\n' "$seerr_owner_out" | indent >&2
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

# The login screen is the one page every Cerulean identity sees before they have
# a session, and the image on it is not configuration: Jellyfin *generates* a
# collage from up to 30 posters and 30 thumbnails into {DataPath}/splashscreen.png
# after every library scan, so a branded splash exists only if the file is
# installed AND branding.xml names it - and `SplashscreenLocation` is not
# settable through the API at all (BrandingOptionsDto omits it,
# jellyfin/jellyfin#13744), so the configuration file is the only way to point
# at one. Both halves fail quietly: a location naming a path that is not there
# is not an error the server reports, it serves the collage instead, and the
# first version of the splash script aimed at /config/data while installing to
# /config/data/data (this image's data dir) - measured by putting a different
# image where the collage goes and watching which one came back from
# /Branding/Splashscreen. Exit 2 is "cannot look" (no appdata here, so this is
# not the media host) and stays a note, like the ClipBucket checks.
splash_out=$(python3 scripts/jellyfin-splash.py --check 2>&1)
splash_code=$?
if [ "$splash_code" -eq 0 ]; then
  say "ok: Jellyfin's login screen shows Monarch's splash, not the generated collage"
elif [ "$splash_code" -eq 2 ]; then
  say "note: the Jellyfin splash could not be judged (skipped) - $(printf '%s' "$splash_out" | tail -1)"
else
  fail "jellyfin: the login screen is Jellyfin's poster collage, or branding points at a splash that is not there - run scripts/jellyfin-splash.py --apply, then restart Jellyfin; see docs/operations.md"
  printf '%s\n' "$splash_out" | indent >&2
fi

# Jellyfin's own HTTPS listener is the other half of "this app is served over
# TLS": the edge terminates TLS for the published name, and the app is asked to
# serve the same material for anything that reaches it directly. It fails in
# three ways, none of which Jellyfin reports: `CertificatePath` naming a PEM
# (the app loads PKCS#12 only), naming a file the container's user cannot read,
# and naming the material from the day it was installed - the estate renews on a
# timer and pushes the result to the edge, never into this filesystem, so the
# day the certificate expires the app starts serving an expired one. The check
# reads the configuration *and* the certificate the listener actually returns,
# which is the only half that proves the file was loaded. Exit 2 is "cannot
# look" (no appdata here, so this is not the media host, or no docker) and stays
# a note, like the splash and ClipBucket checks.
tls_out=$(python3 scripts/jellyfin-tls.py --check 2>&1)
tls_code=$?
if [ "$tls_code" -eq 0 ]; then
  say "ok: Jellyfin serves media.innotel.us over TLS with the certificate this repo installed"
elif [ "$tls_code" -eq 2 ]; then
  say "note: Jellyfin's HTTPS listener could not be judged (skipped) - $(printf '%s' "$tls_out" | tail -1)"
else
  fail "jellyfin: its HTTPS listener is not serving our certificate - renew media.innotel.us in Cerulean, then run scripts/jellyfin-tls.py --renew and restart Jellyfin; material the edge does not hold takes scripts/jellyfin-tls.py --apply --pem <file>; see docs/operations.md"
  printf '%s\n' "$tls_out" | indent >&2
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
# Exit 1 is "nothing answered", which is a different finding from "the outpost
# answered and refused": the outpost's own bind replies take seconds against the
# Cerulean Authentik, and this check used to call a late reply a drifted
# credential. A *result code* is the drift this exists for.
#
# An unanswered probe is a note only while the outpost is plausibly still coming
# up. It stayed a note unconditionally until 2026-09-18, and the cost was
# measured: the outpost ran for 45 minutes with its API token rejected (the
# container log said `403 Forbidden (Token invalid/expired)`, its own
# `/ldap healthcheck` failed 541 times, and it never opened 3389), every Cerulean
# identity got HTTP 500 from Jellyfin's login form, and this run reported
# "all live-stack invariants OK". The container's state is what separates the two
# cases: running, past its start period, and still not serving is a failure; a
# container that is starting, or was restarted seconds ago, is a note. The fix is
# a recreate (init pins the token, but a process already running against the old
# one keeps failing on its own), which is what verify-ldap.py prints.
# How long the outpost may be up without answering before that is a finding
# instead of a note: its own healthcheck gives up in ~5s per try and the start
# period is 3s, so a minute and a half is well past "still booting".
ldap_grace=${DRIFT_LDAP_GRACE_SEC:-90}
ldap_probe=${MONARCH_LDAP_PROBE:-python3 scripts/verify-ldap.py}
ldap_path_out=$($ldap_probe 2>&1)
ldap_path_code=$?
if [ "$ldap_path_code" -eq 0 ]; then
  say "ok: Jellyfin's LDAP login path works end to end (outpost token + bind credential)"
elif [ "$ldap_path_code" -eq 1 ]; then
  ldap_state=$(docker inspect authentik-ldap --format '{{.State.Status}}' 2>/dev/null || echo "")
  ldap_health=$(docker inspect authentik-ldap --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo "")
  ldap_started=$(docker inspect authentik-ldap --format '{{.State.StartedAt}}' 2>/dev/null || echo "")
  ldap_age=""
  if [ -n "$ldap_started" ]; then
    ldap_age=$(( $(date +%s) - $(date -d "$ldap_started" +%s 2>/dev/null || echo 0) ))
  fi
  if [ -z "$ldap_state" ]; then
    say "note: no authentik-ldap container on this host (skipped) - Jellyfin logins through Cerulean need it; $(printf '%s' "$ldap_path_out" | grep -m1 FAIL)"
  elif [ "$ldap_state" = "running" ] && { [ "$ldap_health" = "starting" ] || { [ -n "$ldap_age" ] && [ "$ldap_age" -lt "$ldap_grace" ]; }; }; then
    say "note: the Authentik LDAP outpost is still coming up (state=$ldap_state health=$ldap_health age=${ldap_age}s) - Jellyfin logins fail until it listens"
  elif [ "$ldap_state" = "running" ]; then
    fail "jellyfin: the Authentik LDAP outpost is running but not serving (health=$ldap_health age=${ldap_age}s), so every Cerulean identity gets HTTP 500 from the login form - run 'docker compose up -d --force-recreate authentik-ldap' (init pins the token; a process already running against the old one keeps failing)"
    printf '%s\n' "$ldap_path_out" | indent >&2
  else
    # Not running is a different fault from running-and-not-serving, and the
    # message used to call both of them "up but not serving" - so an operator
    # read "recreate the outpost" while the container was `exited`, recreated
    # it by hand, and the run stayed red because the stack it belongs to had
    # never been brought back up. `exited` is the daemon-restarted or stopped
    # case and is fixed by bringing the project up; `dead` is the daemon's own
    # broken bookkeeping, which compose cannot operate on at all (see the
    # Dead-state check below) and which no recreate will help.
    fail "jellyfin: the Authentik LDAP outpost is not running (state=$ldap_state age=${ldap_age}s), so every Cerulean identity gets HTTP 500 from the login form - bring the stack up with 'docker compose up -d' (state=created/exited means the project is down, not the outpost's token)"
    printf '%s\n' "$ldap_path_out" | indent >&2
  fi
else
  fail "jellyfin: the LDAP login path is broken (verify-ldap.py exit $ldap_path_code) - every Cerulean identity gets HTTP 500 from the login form; see docs/operations.md 'A Cerulean identity cannot sign in'"
  printf '%s\n' "$ldap_path_out" | indent >&2
fi

# The *arr wiring: Prowlarr <-> Sonarr/Radarr/Lidarr/Whisparr.
#
# An *arr answers 400 to any Host name it was not told about, and its default
# names only localhost - so Prowlarr's app test and its indexer sync, which run
# over the compose network, were refused before authentication every time. The
# apps still showed all four registered and an empty indexer list in each of
# them, which reads like "Prowlarr is not registering" and is not what was wrong.
# `arr-allowed-hosts.py --check` compares the live apps against the shared list
# (init/arr-allowlist.txt) and exits 2 on drift, 1 when the apps are not on this
# host at all - which is a note here, not a failure.
#
# The second half is the indexers themselves: one that is present but blocked
# makes a search look empty rather than unconfigured. The timer asks the cheap
# question (--offline: Prowlarr holds an enabled indexer) because the deep one -
# testing every definition, some through FlareSolverr - is thirty requests to
# public trackers, which is not something to run every six hours. `--check`
# without --offline is the operator's, and is documented for that. Exit 2 is the
# finding, 1 is "no Prowlarr here".
arr_hosts_out=$(python3 scripts/arr-allowed-hosts.py --check 2>&1)
arr_hosts_code=$?
if [ "$arr_hosts_code" -eq 0 ]; then
  say "ok: every *arr answers the names the stack calls it by (Prowlarr's app tests can pass)"
elif [ "$arr_hosts_code" -eq 1 ]; then
  say "note: the *arr apps are not reachable from here (skipped) - $(printf '%s' "$arr_hosts_out" | grep -m1 FAIL)"
else
  fail "*arr: an app does not answer a name its peers call it by (arr-allowed-hosts.py exit $arr_hosts_code) - Prowlarr's app test and indexer sync fail with HTTP 400, so no indexer reaches that app; run scripts/arr-allowed-hosts.py (it restarts what it changes)"
  printf '%s\n' "$arr_hosts_out" | indent >&2
fi

prowlarr_idx_out=$(python3 scripts/prowlarr-indexers.py --check --offline 2>&1)
prowlarr_idx_code=$?
if [ "$prowlarr_idx_code" -eq 0 ]; then
  say "ok: $(printf '%s' "$prowlarr_idx_out" | head -1)"
elif [ "$prowlarr_idx_code" -eq 1 ]; then
  say "note: Prowlarr is not reachable from here (skipped)"
else
  fail "prowlarr: no enabled indexer (prowlarr-indexers.py exit $prowlarr_idx_code) - searches look empty rather than unconfigured; run scripts/prowlarr-indexers.py to add the ones that work, and --check to test the ones already there"
  printf '%s\n' "$prowlarr_idx_out" | indent >&2
fi

# And the last link: Prowlarr's list only reaches an app during an application
# sync, so Prowlarr can be full - seventy indexers, every app test green - while
# every *arr holds none of them. That is the state an operator reads as
# "Prowlarr is not registering its indexers", and the only thing that separates
# it from the supported state is a number, so it is counted rather than assumed:
# each app must hold at least one indexer whose base URL is Prowlarr's own proxy
# path. Fewer than Prowlarr has is normal (Prowlarr will not sync an indexer that
# returns no results in that app's categories); none at all is not.
# Exit 2 is the finding, 1 is "no Prowlarr here".
arr_sync_out=$(python3 scripts/arr-sync.py --check 2>&1)
arr_sync_code=$?
if [ "$arr_sync_code" -eq 0 ]; then
  say "ok: every *arr holds the indexers Prowlarr syncs to it"
elif [ "$arr_sync_code" -eq 1 ]; then
  say "note: Prowlarr or an *arr app is not reachable from here (arr-sync skipped) - $(printf '%s' "$arr_sync_out" | grep -m1 FAIL)"
else
  fail "*arr: an app holds none of Prowlarr's indexers, or Prowlarr's own app test fails (arr-sync.py exit $arr_sync_code) - Prowlarr is full and the app searches nothing; run scripts/arr-sync.py to sync the four apps"
  printf '%s\n' "$arr_sync_out" | indent >&2
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

  # ── the outpost image must track the Authentik *server* version ──────────
  # The `authentik-ldap` container is a *client* of the Cerulean Authentik, and
  # goauthentik keeps the outpost and the server on one version line for a
  # reason: the outpost speaks the server's API and schema. On 2026-09-19 this
  # deployment sat at ldap:2026.8.2 against a 2026.8.3 server and nothing here
  # noticed - the compose comment says "bump both together", and a comment is
  # not a check.
  #
  # The server answers `/admin/version/` with `version_current`, which is the
  # authoritative version to track. Its `outpost_outdated` flag is deliberately
  # NOT the trigger: this is the estate's *shared* Authentik, so that flag is
  # true while any outpost anywhere lags, and one belonging to another team
  # would fail this host's run over something it cannot fix. The outposts API
  # exposes no per-outpost version to narrow it with (the `version` field is
  # absent entirely, not merely null). So the scoped question - "does the
  # outpost on this host run the server's version?" - is answered from the
  # container's own image tag, and `outpost_outdated` is read only to enrich
  # the message.
  ak_ver_code=$(curl -s -o /tmp/drift-akv.$$ -w "%{http_code}" \
    -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
    "$ak_base/api/v3/admin/version/")
  if [ "$ak_ver_code" = "200" ]; then
    ak_versions=$(python3 -c "
import sys, json
try:
    d = json.load(open('/tmp/drift-akv.$$'))
    print('%s|%s' % (d.get('version_current',''), d.get('outpost_outdated','')))
except Exception:
    print('|')
" 2>/dev/null)
    ak_server_version="${ak_versions%%|*}"
    ak_outpost_outdated="${ak_versions##*|}"
    ldap_ref=$(docker inspect authentik-ldap --format '{{.Config.Image}}' 2>/dev/null || echo "")
    ldap_ref="${ldap_ref%%@*}"
    ldap_tag="${ldap_ref##*:}"
    [ "$ldap_tag" = "$ldap_ref" ] && ldap_tag=""   # no tag in the ref
    if [ -z "$ldap_tag" ]; then
      if [ "$ak_outpost_outdated" = "True" ]; then
        fail "authentik: the server reports an outpost behind it (outpost_outdated=true, server $ak_server_version) and no authentik-ldap container is on this host - set ghcr.io/goauthentik/ldap:$ak_server_version wherever the outpost runs, then recreate it; see docs/operations.md 'The Authentik LDAP outpost version'"
      else
        say "note: no authentik-ldap container on this host (skipped) - outpost version not checked"
      fi
    elif [ -n "$ak_server_version" ] && [ "$ldap_tag" != "$ak_server_version" ]; then
      ak_corroborates=""
      if [ "$ak_outpost_outdated" = "True" ]; then
        ak_corroborates=" (the server corroborates: outpost_outdated=true)"
      fi
      fail "authentik: the LDAP outpost runs ldap:$ldap_tag while the Authentik server is $ak_server_version$ak_corroborates - an outpost is a client of the server and must sit on its version line. Set ghcr.io/goauthentik/ldap:$ak_server_version in docker-compose.yml (and ips/groups/3-media.yml), then 'docker compose up -d --force-recreate authentik-ldap'; see docs/operations.md 'The Authentik LDAP outpost version'"
    else
      say "ok: authentik LDAP outpost image tracks the server ($ak_server_version)"
    fi
  else
    say "note: authentik admin/version API unreachable (HTTP $ak_ver_code) - outpost version not checked"
  fi
  rm -f /tmp/drift-akv.$$
fi

# ───────────────────────────────────────────────────────────────────────────
# ClipBucket — is it *installed*, not merely running?
# ───────────────────────────────────────────────────────────────────────────
# The app ships a nine-step browser wizard and a deployment has no browser in
# it, so a host whose clipbucket_db volume never got the wizard sits serving its
# installer — which looks exactly like an installed site with no content, and is
# how this stack came to be described as migrated while the volume held a single
# entry (`db.opt`) against a document claiming "80 database tables, 2 users".
#
# scripts/clipbucket-install.py --check is the judgement (schema, version row,
# admin password, base_url, leftover install.me), and two of its findings are
# invisible from outside the app: without the version row the app gates its
# queries on a table that is empty and answers HTTP 500 on every *logged-in*
# page while anonymous pages are fine, and a leftover `files/temp/install.me`
# leaves the browser installer reachable over a finished site.
# Exit 2 is "cannot tell" (no container here, or no docker), which is a note: a
# host running part of the group is not a drifted host.
clipbucket_out=$(python3 scripts/clipbucket-install.py --check 2>&1)
clipbucket_code=$?
if [ "$clipbucket_code" -eq 0 ]; then
  say "ok: ClipBucket is installed (schema, version, admin, base_url) with its installer locked"
elif [ "$clipbucket_code" -eq 2 ]; then
  say "note: ClipBucket's install state could not be judged (skipped) - $(printf '%s' "$clipbucket_out" | tail -1)"
else
  fail "clipbucket: the install is not finished - run scripts/clipbucket-install.py --apply; see docs/operations.md 'Completed media migration'"
  printf '%s\n' "$clipbucket_out" | indent >&2
fi

# ───────────────────────────────────────────────────────────────────────────
# ClipBucket — does the catalogue hold the library?
# ───────────────────────────────────────────────────────────────────────────
# A *finished* install is an empty site, and that is the state this stack sat in
# while its own docs called it migrated: cb_video held nothing, so
# tube.innotel.us answered a working site with no content — the same
# caller-visible result as a broken import, and the reason "installed" and
# "has the library" are two checks rather than one.
#
# The library is /data/media, the same files Jellyfin serves, and
# scripts/clipbucket-library.py is the judgement: every movie and TV episode
# there has a catalogue row, a playable file under the name the app's own
# get_video_files() builds, and the thumbnails its card renders. Each of the
# three fails invisibly on its own (an invisible row, a dead link, a broken
# image), which is why they are one check.
#
# The library is the list: there is no exclusion mechanism on purpose, so "what
# should not be on the site" is answered by the media library rather than by a
# second list here that would drift from it. Exit 2 is "cannot tell" (no
# container, no docker, no /data/media) and stays a note — a host running part
# of the group is not a drifted host. Only judged once the install above is
# finished, because on an unfinished install every finding below is a
# consequence of that one.
if [ "$clipbucket_code" -eq 0 ]; then
  clip_lib_out=$(python3 scripts/clipbucket-library.py --check 2>&1)
  clip_lib_code=$?
  if [ "$clip_lib_code" -eq 0 ]; then
    say "ok: ClipBucket's catalogue holds the media library"
  elif [ "$clip_lib_code" -eq 2 ]; then
    say "note: ClipBucket's library could not be judged (skipped) - $(printf '%s' "$clip_lib_out" | tail -1)"
  else
    fail "clipbucket: the catalogue is behind the media library - run scripts/clipbucket-library.py --apply; see docs/operations.md 'ClipBucket'"
    printf '%s\n' "$clip_lib_out" | indent >&2
  fi
fi

# ClipBucket — is the *site* serving that library?
# ───────────────────────────────────────────────────────────────────────────
# The check above judges this repo's model of the app — the name the app's
# get_video_files() builds for a row. Everything in that model can be right and
# the site still play nothing, which is not hypothetical: the first import wrote
# `<name>-<hash>.mp4` and produced rows every filesystem check called complete
# while their watch pages had no `<source>` at all. This asks the app: fetch each
# item's watch page, take the sources it emits, and require a file behind each
# that the web server will actually serve bytes from — the half `ls` cannot see,
# because a file the container user cannot read is listed fine and answered 403.
# Only judged once the install is finished, for the same reason as above.
if [ "$clipbucket_code" -eq 0 ]; then
  clip_serve_out=$(python3 scripts/clipbucket-library.py --serve-check 2>&1)
  clip_serve_code=$?
  if [ "$clip_serve_code" -eq 0 ]; then
    say "ok: ClipBucket serves a playable file for every catalogue item"
  elif [ "$clip_serve_code" -eq 2 ]; then
    say "note: ClipBucket's playback could not be judged (skipped) - $(printf '%s' "$clip_serve_out" | tail -1)"
  else
    fail "clipbucket: a catalogue item's watch page serves no playable file - re-run scripts/clipbucket-library.py --apply, then check the file volume's ownership; see docs/operations.md 'ClipBucket'"
    printf '%s\n' "$clip_serve_out" | indent >&2
  fi
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
# Model gateway (OmniRoute) — a provider key that stopped being accepted
# ───────────────────────────────────────────────────────────────────────────
# The estate runs one OmniRoute for every product and it lives on the proxy
# host, not here. Nothing in this media stack *needs* it, which is exactly how
# a rejected key went unnoticed: the gateway went on answering on :20128,
# refused the work with a 401, and the failure surfaced as "every model in the
# chain failed" in somebody else's console. Set `DRIFT_GATEWAY_URL` (e.g.
# http://192.168.1.71:20128) in `.env` to watch it; unset means skipped, like
# every other check that needs somewhere else to be reachable.
#
# What counts is narrow on purpose, and lives in `gateway-provider-keys.py`: the
# authentication classes only (401/403, in either shape the gateway reports a
# status). A 429, 402, 404 or 503 is a quota, a missing model or an upstream
# having a bad day — routine on a free-tier gateway, and reporting them would
# make this line an alert nobody reads. Only connections the deployment has
# switched *on* are judged: turning one off is the documented answer for a
# provider that is out of credit, so judging it would make the fix the alarm.
#
# `DRIFT_GATEWAY_TOKEN` is optional and is not needed on this estate: the
# management API answers the same projection without it (checked both ways
# against the live gateway), so no credential is put on this host for the check.
# Set it only for a gateway that gates `/api/*` as well. Both variables live in
# `.env`.
GATEWAY_URL="${DRIFT_GATEWAY_URL:-}"
if [ -z "$GATEWAY_URL" ]; then
  say "ok: model gateway (skipped - DRIFT_GATEWAY_URL not set)"
else
  gw_env=(--url "$GATEWAY_URL")
  [ -n "${DRIFT_GATEWAY_TOKEN:-}" ] && gw_env+=(--token "$DRIFT_GATEWAY_TOKEN")
  if gw_out=$(python3 scripts/gateway-provider-keys.py --check "${gw_env[@]}" 2>&1); then
    say "ok: $(printf '%s\n' "$gw_out" | head -1)"
  else
    gw_rc=$?
    if [ "$gw_rc" -eq 2 ]; then
      # Unreadable is a note, not a finding - the same posture every other check
      # that needs somewhere else to be reachable takes. The gateway is not this
      # stack's dependency and an outage here is not this stack's drift.
      say "note: the model gateway could not be read (skipped) - $(printf '%s\n' "$gw_out" | tail -1)"
    else
      while IFS= read -r gw_line; do
        case "$gw_line" in
          REJECTED\ *) fail "gateway: ${gw_line#REJECTED }" ;;
          *) say "$gw_line" ;;
        esac
      done <<< "$gw_out"
    fi
  fi
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
  done < <({ arr_rows | cut -d'|' -f1; echo prowlarr; echo transmission; echo jellyfin; echo jellyseerr; echo bazarr; echo homarr; echo nginx-proxy-manager; echo authentik-ldap; } | sort -u)
fi

rm -f /tmp/drift-body.$$

# ── docker's own bookkeeping: a container stuck in the Dead state ─────────
# This is not an app's drift, it is the daemon's. A container whose removal did
# not finish keeps a `Dead` entry that compose still reads as part of its
# project, and then *every* compose call for that project - `up`, `ps`, `down` -
# fails with `Error response from daemon: No such container: <id>`. The whole
# stack compose manages stays down with it, the LDAP outpost included, so
# Jellyfin's login form answers 500 and the alert names the outpost while the
# outpost was simply never started. `--heal` is a no-op here for the same
# reason: it is a compose call, and it fails the same way (`docker start` in the
# init re-run is swallowed by `|| true`). `docker rm` does not help either - it
# reports the same "No such container", because the daemon's in-memory entry has
# lost the container it names. Only a daemon restart rebuilds the state; the
# stale directory under /var/lib/docker/containers/<id> is removed first because
# it is what the daemon re-reads on start.
#
# This happened on 2026-09-30: monarch-init's one-shot container was left Dead
# by a daemon restart, monarch.service looped 2253 times creating the *arr
# containers without ever starting them, and the media stack was down for ten
# hours while the alert blamed the outpost.
if command -v docker >/dev/null 2>&1; then
  dead_containers=$(docker ps -a --filter status=dead --format '{{.ID}} {{.Image}}' 2>/dev/null | tr '\n' ' ')
  if [ -n "${dead_containers// /}" ]; then
    fail "infra: a container is stuck in docker's Dead state (${dead_containers% }) - while one exists every 'docker compose' call for its project fails with 'No such container', so the services it manages (and the LDAP outpost every Cerulean login goes through) never start; 'drift-check --heal' clears it by clearing its stale directory and restarting the Docker daemon, or clear it by hand with 'rm -rf /var/lib/docker/containers/<id>' then 'systemctl restart docker'; see docs/operations.md 'When docker compose will not bring the stack up'"
  fi
fi

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
    echo "drift-check: $FAILS issue(s) found - reconciling the stack to heal..." >&2
    echo "$now" > "$HEAL_STATE" 2>/dev/null || true
    # A container stuck in Docker's Dead state poisons *every* compose call for its
    # project (see the check above), so nothing else in this heal can work until it
    # is gone - and no CLI removes one: `docker rm` answers "No such container" for
    # the same reason the container is Dead, and compose fails the same way. Only a
    # restart of the daemon rebuilds the state, and the stale directory goes first
    # because that is what the daemon re-reads on start. This is the one heal step
    # that touches something other than this stack, so it is scoped to the Dead case
    # (which is otherwise unrecoverable) and can be turned off on a host that would
    # rather page a person: DRIFT_HEAL_DOCKER_RESTART=0.
    if [ "${DRIFT_HEAL_DOCKER_RESTART:-1}" = "1" ] && command -v docker >/dev/null 2>&1; then
      dead_ids=$(docker ps -a --no-trunc --filter status=dead --format '{{.ID}}' 2>/dev/null)
      if [ -n "${dead_ids// /}" ]; then
        echo "drift-check: clearing $(printf '%s' "$dead_ids" | wc -w | tr -d ' ') Dead container(s) - restarting the Docker daemon..." >&2
        for dead_id in $dead_ids; do
          rm -rf "/var/lib/docker/containers/$dead_id" || true
        done
        if command -v systemctl >/dev/null 2>&1; then
          systemctl restart docker >/dev/null 2>&1 || true
          # Wait for the daemon to answer again before the compose reconcile below,
          # so that reconcile is not the thing that discovers the restart is slow.
          for _ in $(seq 1 30); do
            docker info >/dev/null 2>&1 && break
            sleep 2
          done
        else
          echo "drift-check: no systemctl here - a Dead container may remain and compose will keep failing; clear it by hand (see docs/operations.md)" >&2
        fi
      fi
    fi
    # monarch-init only pins credentials; it does not bring a stopped or exited
    # service back, and the heal used to be nothing but that re-run. So a
    # service that was merely down - the LDAP outpost after a daemon restart,
    # say - stayed down through every heal while the alert repeated. `docker
    # compose up` is what the systemd unit actually runs and what reconciles
    # the stack, so run it first. Its failure is printed rather than swallowed:
    # when compose cannot operate on the project (a Dead container, a bad
    # compose file) that is the answer, and an `|| true` here is what kept it
    # invisible for ten hours.
    if command -v docker >/dev/null 2>&1; then
      if compose_out=$(docker compose -f docker-compose.yml up -d --remove-orphans 2>&1); then
        echo "drift-check: docker compose up reconciled the stack" >&2
      else
        echo "drift-check: docker compose up FAILED during heal - the stack was not reconciled:" >&2
        printf '%s\n' "$compose_out" | indent >&2
      fi
    fi
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
    # The stack this heal just reconciled is not ready when the commands return.
    # `docker compose up` only starts containers, and monarch-init POSTs
    # Jellyfin's own /System/Restart - which is asynchronous, so the old process
    # answers for a few seconds and the new one then spends ~40s booting, its
    # setup server listening first and replying 503 throughout. The re-check
    # below is the next reader after all of that, so it used to read a
    # half-started Jellyfin and report its libraries missing and its API keys
    # dead - two findings about a credential that was never wrong, both gone by
    # the next tick. Wait for the server to answer one authenticated call before
    # asking it the questions it can only answer once it is up.
    #
    # /Users and not /System/Info/Public: only the wired server can answer an
    # authenticated call, so this cannot be satisfied by the boot-time setup
    # server the way a public endpoint can. Override the bound with
    # DRIFT_READY_TIMEOUT_SEC, or skip the wait with DRIFT_READY_WAIT=0.
    if [ "${DRIFT_READY_WAIT:-1}" = "1" ] && command -v curl >/dev/null 2>&1 \
        && [ -n "${JF_PORT:-}" ]; then
      ready_timeout="${DRIFT_READY_TIMEOUT_SEC:-300}"
      ready_key=""
      [ -f "$JELLYFIN_KEY_FILE" ] && ready_key=$(cat "$JELLYFIN_KEY_FILE" 2>/dev/null)
      if [ -n "$ready_key" ]; then
        echo "drift-check: waiting up to ${ready_timeout}s for Jellyfin to serve again before re-checking..." >&2
        jf_ready=0
        for _ in $(seq 1 $(( ready_timeout / 3 ))); do
          ready_code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 \
            "http://localhost:$JF_PORT/Users" \
            -H "Authorization: MediaBrowser Token=\"$ready_key\", Client=\"Drift Check\", Device=\"Linux\", DeviceId=\"drift-check-001\", Version=\"1.0.0\"")
          if [ "$ready_code" = "200" ]; then jf_ready=1; break; fi
          sleep 3
        done
        if [ "$jf_ready" = "1" ]; then
          echo "drift-check: Jellyfin is serving again" >&2
        else
          echo "drift-check: Jellyfin did not start serving within ${ready_timeout}s - the re-check reports what it finds" >&2
        fi
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