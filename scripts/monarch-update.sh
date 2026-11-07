#!/usr/bin/env bash
# Keep Monarch's Jellyfin and *arr services on the newest published images.
#
# Monarch runs one compose project with 35 containers. Most images are tagged
# `:latest`, and a watchtower container already pulls those every six hours —
# with no health gate and no rollback. Jellyfin is the exception: it is pinned by
# digest and carries `watchtower.enable=false`, so it is the one media service on
# that host that never moves.
#
# This is the deliberate path for both. For each managed service it resolves what
# the tag (or the digest) points at today, and only when that is a *different*
# image does it recreate that one container, wait for it to settle, and put the
# previous image back if it does not. Jellyfin's new digest is written into the
# compose file the project was started from, so the next `compose up` does not
# silently undo it.
#
# It does not fight watchtower: a container started within MONARCH_SKIP_RECENT
# seconds is left alone, because that is watchtower having just done it.
#
# Usage:
#   monarch-update.sh [--dry-run] [--no-pull] [--notify-only]
#                     [--retry-quarantined] [--services a,b,c]
#
# Environment (/etc/innotel/monarch-update.env):
#   MONARCH_SERVICES         comma list to manage (default jellyfin, sonarr,
#                            radarr, lidarr, whisparr, prowlarr, bazarr)
#   JELLYFIN_IMAGE_TAG       tag whose digest jellyfin is pinned to (default latest)
#   MONARCH_HEALTH_TIMEOUT   seconds to wait for one service (default 900;
#                            jellyfin needs ~5 minutes after an image hop)
#   MONARCH_SKIP_RECENT      leave a container started this recently alone,
#                            which is watchtower (default 900)
#   MONARCH_QUARANTINE_DAYS  stop retrying a service that failed to settle on an
#                            image for this long (default 7)
#   ALERT_EMAIL_TO           mailbox a failed update is mailed to (sendmail)
#   ALERT_WEBHOOK_URL        JSON webhook a failed update is POSTed to
#   MONARCH_STATE            alert/state directory
#                            (default /var/lib/innotel/monarch-update)
#
# Install (timer: once a day at 05:00):
#   install -Dm755 monarch-update.sh /opt/innotel/monarch-update/monarch-update.sh
#   install -Dm644 monarch-update.service /etc/systemd/system/monarch-update.service
#   install -Dm644 monarch-update.timer   /etc/systemd/system/monarch-update.timer
#   systemctl daemon-reload && systemctl enable --now monarch-update.timer
#
# Exit: 0 current or updated · 1 a service failed and was rolled back · 2 precondition.
set -euo pipefail

SERVICES="${MONARCH_SERVICES:-jellyfin,sonarr,radarr,lidarr,whisparr,prowlarr,bazarr}"
JELLYFIN_TAG="${JELLYFIN_IMAGE_TAG:-latest}"
HEALTH_TIMEOUT="${MONARCH_HEALTH_TIMEOUT:-900}"
SKIP_RECENT="${MONARCH_SKIP_RECENT:-900}"
QUARANTINE_DAYS="${MONARCH_QUARANTINE_DAYS:-7}"
STATE_DIR="${MONARCH_STATE:-/var/lib/innotel/monarch-update}"
ALERT_TO="${ALERT_EMAIL_TO:-}"
ALERT_WEBHOOK="${ALERT_WEBHOOK_URL:-}"
LOCK="${MONARCH_LOCK:-/run/lock/monarch-update.lock}"

DRY_RUN=false
PULL=true
NOTIFY=false
IGNORE_QUARANTINE=false
FAILED=()
PENDING=()
QUARANTINED=()

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { printf '%s WARNING: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

# Best effort on purpose: a broken alert channel must not hide the failure it is
# reporting. Without a channel the message is still in the journal.
alert() {  # subject, body
  local subject=$1 body=$2 payload host
  host=$(hostname)
  printf '%s\n' "$body" | sed 's/^/  /' >&2
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$subject" >>"$STATE_DIR/last-alert" 2>/dev/null || true
  if [ -n "$ALERT_TO" ] && [ -x /usr/sbin/sendmail ]; then
    printf 'To: %s\nSubject: %s\nFrom: monarch-update@%s\nAuto-Submitted: auto-generated\n\n%s\n' \
      "$ALERT_TO" "$subject" "$host" "$body" |
      /usr/sbin/sendmail -t || warn "could not hand the alert to sendmail — it is only in the journal"
  fi
  if [ -n "$ALERT_WEBHOOK" ]; then
    payload=$(printf '%s' "$subject — $body" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')
    curl -fsS --max-time 15 -X POST -H 'Content-Type: application/json' \
      -d "{\"text\":\"$payload\"}" "$ALERT_WEBHOOK" >/dev/null ||
      warn "the alert webhook refused the POST — it is only in the journal"
  fi
  if [ -z "$ALERT_TO" ] && [ -z "$ALERT_WEBHOOK" ]; then
    warn "nothing receives this alert (ALERT_EMAIL_TO / ALERT_WEBHOOK_URL unset); it is only in the journal"
  fi
}

die() {  # code, message
  local code=$1
  shift
  printf '%s ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
  exit "$code"
}

usage() { sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --no-pull) PULL=false ;;
    --notify-only) NOTIFY=true; DRY_RUN=true ;;
    --retry-quarantined) IGNORE_QUARANTINE=true ;;
    --services)
      [ $# -ge 2 ] || die 2 "--services needs a comma list"
      SERVICES=$2
      shift
      ;;
    -h|--help) usage ;;
    *) die 2 "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

command -v docker >/dev/null 2>&1 || die 2 "no docker on PATH"
mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR="${TMPDIR:-/tmp}"

# One run at a time: a slow pull must not overlap the next tick.
if command -v flock >/dev/null 2>&1; then
  mkdir -p "$(dirname "$LOCK")" 2>/dev/null || true
  exec 9>"$LOCK" || die 2 "cannot open the lock file $LOCK"
  if ! flock -n 9; then
    log "another run holds $LOCK; leaving it alone"
    exit 0
  fi
fi

# ── Talking to the project ───────────────────────────────────────────────────
label_of() { docker inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true; }

container_for() {  # service → container name
  local svc=$1 c
  if docker inspect "$svc" >/dev/null 2>&1; then printf '%s' "$svc"; return 0; fi
  c=$(docker ps -a --format '{{.Names}}|{{.Label "com.docker.compose.service"}}' 2>/dev/null |
    awk -F'|' -v s="$svc" '$2 == s {print $1; exit}')
  [ -n "$c" ] || return 1
  printf '%s' "$c"
}

COMPOSE_CMD=()
CONFIG_FILES=()
PINNED_FILES=()
build_compose() {  # container
  local c=$1 project workdir files f
  COMPOSE_CMD=(docker compose)
  CONFIG_FILES=()
  project=$(label_of "$c" com.docker.compose.project)
  workdir=$(label_of "$c" com.docker.compose.project.working_dir)
  files=$(label_of "$c" com.docker.compose.project.config_files)
  if [ -n "$project" ] && [ "$project" != "<no value>" ]; then
    COMPOSE_CMD+=(--project-name "$project")
  fi
  if [ -n "$workdir" ] && [ "$workdir" != "<no value>" ]; then
    COMPOSE_CMD+=(--project-directory "$workdir")
  fi
  if [ -n "$files" ] && [ "$files" != "<no value>" ]; then
    for f in $(printf '%s' "$files" | tr ',' ' '); do
      COMPOSE_CMD+=(-f "$f")
      CONFIG_FILES+=("$f")
    done
  fi
}

current_digest() {  # container → the digest it is running
  local img
  img=$(docker inspect -f '{{.Image}}' "$1" 2>/dev/null || true)
  [ -n "$img" ] || return 0
  docker image inspect "$img" --format '{{index .RepoDigests 0}}' 2>/dev/null | sed 's/.*@//'
}

registry_digest() {  # repo (ghcr.io/owner/name), tag → digest, "" when unreadable
  local repo=$1 tag=$2 path tok
  path=${repo#ghcr.io/}
  tok=$(curl -fsS --max-time 30 "https://ghcr.io/token?service=ghcr.io&scope=repository:${path}:pull" 2>/dev/null |
    sed -nE 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
  [ -n "$tok" ] || return 0
  curl -sS -D - -o /dev/null --max-time 30 \
    -H "Authorization: Bearer $tok" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/${path}/manifests/${tag}" 2>/dev/null |
    sed -nE 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*(sha256:[0-9a-f]+).*/\1/p' | head -1
}

pin_digest() {  # repo, old digest, new digest → appends the files it re-pinned
  local repo=$1 old=$2 new=$3 f
  for f in "${CONFIG_FILES[@]}" "${EXTRA_FILES[@]}"; do
    [ -f "$f" ] || continue
    grep -qF "${repo}@${old}" "$f" || continue
    cp -a "$f" "$f.jellyfin-bak"
    sed -i "s|${repo}@${old}|${repo}@${new}|g" "$f"
    PINNED_FILES+=("$f")
    printf '%s pinned %s@%s in %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$repo" "$new" "$f" >&2
  done
}

started_recently() {  # container → 0 when it started within SKIP_RECENT
  local started epoch now
  started=$(docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null || true)
  [ -n "$started" ] || return 1
  epoch=$(date -d "$started" +%s 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $((now - epoch)) -lt "$SKIP_RECENT" ]
}

wait_settled() {  # container → 0 when it is up and healthy (or up with no healthcheck)
  local c=$1 deadline=$((SECONDS + HEALTH_TIMEOUT)) health state
  while [ "$SECONDS" -lt "$deadline" ]; do
    state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
    if [ "$state" = exited ] || [ "$state" = dead ] || [ "$state" = missing ]; then
      log "$c is $state"
      return 1
    fi
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo missing)
    case "$health" in
      healthy) return 0 ;;
      none) if [ "$state" = running ]; then return 0; fi ;;
    esac
    sleep 3
  done
  return 1
}

rollback() {  # container, service, ref, old image id
  local c=$1 svc=$2 ref=$3 old_id=$4 f
  warn "$svc did not come up; restoring the previous image"
  # Every file that was re-pinned goes back, not only the last one: the digest
  # lives in the project compose and, when present, the group compose too.
  for f in "${CONFIG_FILES[@]}" "${EXTRA_FILES[@]}"; do
    if [ -f "$f.jellyfin-bak" ]; then cp -a "$f.jellyfin-bak" "$f"; fi
  done
  if [ -n "$old_id" ]; then
    # Works for a tag; a digest ref cannot be re-tagged, and its image is still
    # local, so restoring the compose line is enough for that case.
    docker tag "$old_id" "$ref" >/dev/null 2>&1 || true
  fi
  "${COMPOSE_CMD[@]}" up -d --no-deps "$svc" >/dev/null 2>&1 ||
    warn "restarting the previous $svc failed too"
  # The point of a rollback is that the old image is what runs afterwards; say so
  # loudly when it is not, instead of reporting a clean rollback.
  local now_id
  now_id=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null || true)
  if [ -n "$old_id" ] && [ "$now_id" != "$old_id" ]; then
    warn "$svc is still not on the image it ran before; check it by hand"
  fi
  for f in "${CONFIG_FILES[@]}" "${EXTRA_FILES[@]}"; do
    if [ -f "$f.jellyfin-bak" ]; then rm -f "$f.jellyfin-bak"; fi
  done
}

# pull_needed: after "up -d", is the container even on the new image?
container_image() { docker inspect -f '{{.Image}}' "$1" 2>/dev/null || true; }

# ── Quarantine ───────────────────────────────────────────────────────────────
# An image that starts and then never reports healthy will not start on the next
# run either. Remember it, so the nightly run does not take the service down for
# the whole health timeout every night; forget it after MONARCH_QUARANTINE_DAYS,
# or immediately with --retry-quarantined.
quarantine_file() { printf '%s/quarantine' "$STATE_DIR"; }

quarantine_get() {  # service → "<service> <image> <epoch>"
  local f
  f=$(quarantine_file)
  [ -f "$f" ] || return 1
  awk -v s="$1" '$1 == s' "$f" | tail -1
}

quarantine_set() {  # service, image
  local f tmp
  f=$(quarantine_file); tmp="$f.$$"
  printf '%s %s %s\n' "$1" "$2" "$(date +%s)" >"$tmp" 2>/dev/null || return 0
  if [ -f "$f" ]; then awk -v s="$1" '$1 != s' "$f" >>"$tmp" 2>/dev/null; fi
  mv "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}

quarantine_clear() {  # service
  local f tmp
  f=$(quarantine_file)
  [ -f "$f" ] || return 0
  tmp="$f.$$"
  if awk -v s="$1" '$1 != s' "$f" >"$tmp" 2>/dev/null; then mv "$tmp" "$f"; else rm -f "$tmp"; fi
}

quarantined() {  # service, image → 0 when this exact image is held back
  local svc=$1 image=$2 line held set_at
  [ "$IGNORE_QUARANTINE" = true ] && return 1
  line=$(quarantine_get "$svc") || return 1
  held=$(printf '%s' "$line" | awk '{print $2}')
  set_at=$(printf '%s' "$line" | awk '{print $3}')
  [ "$held" = "$image" ] || return 1
  case "$set_at" in ''|*[!0-9]*) return 1 ;; esac
  if [ $(( $(date +%s) - set_at )) -lt $((QUARANTINE_DAYS * 86400)) ]; then
    log "$svc: $image failed on $(date -u -d "@$set_at" +%Y-%m-%d); holding it back for ${QUARANTINE_DAYS} days (--retry-quarantined retries now)"
    QUARANTINED+=("$svc")
    return 0
  fi
  log "$svc: the quarantine on $image has expired; trying it again"
  quarantine_clear "$svc"
  return 1
}

update_one() {  # service
  local svc=$1 c ref repo old_id cur_digest want_digest new_id old_tag="" candidate=""
  if ! c=$(container_for "$svc"); then
    log "$svc: no container on this host; skipped"
    return 0
  fi
  build_compose "$c"
  # The group compose in the ips checkout pins jellyfin too; keep it in step on
  # the rare host that carries one (a Monarch box usually does not).
  EXTRA_FILES=()
  for candidate in "$(dirname "${CONFIG_FILES[0]:-/nonexistent}")/../../ips/groups/3-media.yml" \
                   /usr/src/projects/complete/ips/groups/3-media.yml; do
    if [ -f "$candidate" ]; then EXTRA_FILES+=("$candidate"); fi
  done
  ref=$(docker inspect -f '{{.Config.Image}}' "$c")
  old_id=$(docker inspect -f '{{.Image}}' "$c")

  # The announcement reports what a run would find, so it must not be silenced by
  # the watchtower guard.
  if [ "$NOTIFY" = false ] && started_recently "$c"; then
    log "$svc: started less than ${SKIP_RECENT}s ago (watchtower?); leaving it alone"
    return 0
  fi

  case "$ref" in
    *@sha256:*)
      # Digest-pinned (jellyfin, homarr): the tag is the moving part, so resolve
      # what that tag points at now and pin that digest.
      repo=${ref%@*}
      case "$repo" in
        */jellyfin) old_tag=$JELLYFIN_TAG ;;
        *) old_tag=latest ;;
      esac
      cur_digest=$(current_digest "$c")
      if [ -z "$cur_digest" ]; then
        warn "$svc: cannot read the digest it is running; leaving it alone"
        return 0
      fi
      want_digest=$(registry_digest "$repo" "$old_tag")
      if [ -z "$want_digest" ]; then
        warn "$svc: could not read ${repo}:${old_tag} from ghcr; leaving it alone"
        return 0
      fi
      if [ "$want_digest" = "$cur_digest" ]; then
        log "$svc: ${old_tag} digest ${want_digest:0:19} is current"
        quarantine_clear "$svc"
        return 0
      fi
      log "$svc: ${old_tag} digest ${cur_digest:0:19} -> ${want_digest:0:19}"
      candidate="${repo}@${want_digest}"
      if [ "$NOTIFY" = true ]; then
        PENDING+=("$svc: ${old_tag} ${cur_digest:0:19} -> ${want_digest:0:19}")
      fi
      if quarantined "$svc" "$candidate"; then return 0; fi
      if [ "$DRY_RUN" = true ]; then
        log "$svc: dry-run, would re-pin and recreate it"
        return 0
      fi
      PINNED_FILES=()
      pin_digest "$repo" "$cur_digest" "$want_digest"
      if [ "${#PINNED_FILES[@]}" -eq 0 ]; then
        warn "$svc: no compose file carries ${repo}@${cur_digest}; not changing it"
        return 0
      fi
      if ! docker pull "${repo}@${want_digest}" >/dev/null 2>&1; then
        warn "$svc: could not pull ${repo}@${want_digest}"
        rollback "$c" "$svc" "$ref" "$old_id"
        FAILED+=("$svc")
        return 1
      fi
      ;;
    *)
      # Tag-pinned `:latest` (the *arr apps): the tag itself moves.
      if [ "$NOTIFY" = true ]; then
        # Ask the registry instead of pulling: the answer is the same and the
        # announcement must not touch the host.
        repo=${ref%:*}; old_tag=${ref##*:}
        cur_digest=$(current_digest "$c")
        want_digest=$(registry_digest "$repo" "$old_tag")
        if [ -n "$cur_digest" ] && [ -n "$want_digest" ] && [ "$want_digest" != "$cur_digest" ]; then
          PENDING+=("$svc: ${ref#*/} ${cur_digest:0:19} -> ${want_digest:0:19}")
          log "$svc: update pending"
        else
          log "$svc: no update pending"
        fi
        return 0
      fi
      if [ "$DRY_RUN" = true ]; then
        log "$svc: dry-run, would pull $ref and recreate it if the digest moved"
        return 0
      fi
      if [ "$PULL" = true ]; then
        if ! "${COMPOSE_CMD[@]}" pull --quiet "$svc" >/dev/null 2>&1; then
          warn "$svc: pull failed; leaving the running container alone"
          FAILED+=("$svc")
          return 1
        fi
      fi
      new_id=$(docker image inspect "$ref" --format '{{.Id}}' 2>/dev/null || true)
      if [ -z "$new_id" ]; then
        warn "$svc: no local image for $ref after pull; leaving it alone"
        FAILED+=("$svc")
        return 1
      fi
      if [ "$new_id" = "$old_id" ]; then
        log "$svc: running image is current"
        quarantine_clear "$svc"
        return 0
      fi
      log "$svc: pulling moved the image ${old_id:0:19} -> ${new_id:0:19}"
      candidate="$new_id"
      if quarantined "$svc" "$candidate"; then
        # Leave the checkout on the image that is actually running, so a plain
        # `compose up` does not quietly start the one that already failed.
        docker tag "$old_id" "$ref" >/dev/null 2>&1 || true
        return 0
      fi
      ;;
  esac

  log "$svc: recreating"
  if ! "${COMPOSE_CMD[@]}" up -d --no-deps "$svc" >/dev/null 2>&1; then
    rollback "$c" "$svc" "$ref" "$old_id"
    FAILED+=("$svc")
    if [ -n "$candidate" ]; then quarantine_set "$svc" "$candidate"; fi
    alert "Monarch: $svc could not be recreated on $(hostname)" "The compose recreate failed and the previous image was restored."
    return 1
  fi
  if ! wait_settled "$c"; then
    rollback "$c" "$svc" "$ref" "$old_id"
    FAILED+=("$svc")
    if [ -n "$candidate" ]; then quarantine_set "$svc" "$candidate"; fi
    alert "Monarch: $svc did not come up on $(hostname)" "It was recreated, did not settle within ${HEALTH_TIMEOUT}s, and the previous image was restored."
    return 1
  fi
  for f in "${CONFIG_FILES[@]}" "${EXTRA_FILES[@]}"; do
    if [ -f "$f.jellyfin-bak" ]; then rm -f "$f.jellyfin-bak"; fi
  done
  log "$svc: updated and running"
  quarantine_clear "$svc"
  return 0
}


IFS=',' read -r -a wanted <<<"$SERVICES"
log "monarch: checking ${wanted[*]} (dry-run=$DRY_RUN, pull=$PULL)"
for svc in "${wanted[@]}"; do
  [ -n "$svc" ] || continue
  update_one "$svc" || true
done

if [ "$NOTIFY" = true ]; then
  if [ "${#PENDING[@]}" -gt 0 ]; then
    printf '%s\n' "monarch: ${#PENDING[@]} update(s) pending"
    for p in "${PENDING[@]}"; do printf 'pending: %s\n' "$p"; done
  else
    printf 'monarch: nothing pending\n'
  fi
  exit 0
fi

if [ "${#QUARANTINED[@]}" -gt 0 ]; then
  log "monarch: holding back ${QUARANTINED[*]} (state in $STATE_DIR/quarantine)"
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  die 1 "failed and rolled back: ${FAILED[*]}"
fi
log "monarch: all managed services are current"
