#!/usr/bin/env bash
# install-media-retention.sh — schedule the library sweep on the media host.
#
# The sweep itself (scripts/media-retention.py) is safe to run anywhere, but the
# library only exists on the host that mounts it, and that host has no runner
# this repo can reach. This installs the tool there and writes the cron entry
# that keeps the library inside its limits.
#
# Dry run by default: it prints the cron entry it would write and the plan the
# sweep would act on. Pass --apply to install the entry that deletes.
#
#   sudo ./scripts/install-media-retention.sh --apply
#
set -euo pipefail

APPLY=0
MEDIA_ROOT="${MEDIA_ROOT:-/data/media}"
MAX_AGE_DAYS=7
KEEP_LATEST=3
SCHEDULE="30 4 * * *"
TARGET=/usr/local/sbin/media-retention.py
CRON_FILE=/etc/cron.d/media-retention

usage() {
  cat <<'USAGE'
usage: install-media-retention.sh [--apply] [--root DIR] [--max-age-days N]
                                  [--keep-latest N] [--schedule "M H * * *"]

  --apply            write the cron entry that deletes; without it the script
                     only prints what it would install
  --root DIR         library root (default $MEDIA_ROOT or /data/media)
  --max-age-days N   remove titles older than N days (default 7)
  --keep-latest N    keep only the newest N episodes per series (default 3)
  --schedule SPEC    cron schedule (default "30 4 * * *")
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --root) MEDIA_ROOT="$2"; shift 2 ;;
    --max-age-days) MAX_AGE_DAYS="$2"; shift 2 ;;
    --keep-latest) KEEP_LATEST="$2"; shift 2 ;;
    --schedule) SCHEDULE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install-media-retention: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$HERE/media-retention.py"
[ -f "$TOOL" ] || { echo "install-media-retention: missing $TOOL" >&2; exit 1; }

CRON_LINE="$SCHEDULE root MEDIA_ROOT=$MEDIA_ROOT $TARGET --root $MEDIA_ROOT --max-age-days $MAX_AGE_DAYS --keep-latest $KEEP_LATEST"
if [ "$APPLY" = 1 ]; then
  CRON_LINE="$CRON_LINE --apply"
fi

if [ ! -d "$MEDIA_ROOT" ]; then
  echo "install-media-retention: $MEDIA_ROOT is not a directory on this host." >&2
  echo "  Install this on the host that mounts the library, not on a build box." >&2
  exit 1
fi

echo "library:  $MEDIA_ROOT"
echo "would install: $TARGET"
echo "cron entry:    $CRON_LINE"

if [ "$APPLY" != 1 ]; then
  echo
  echo "Dry run: nothing installed. The plan the sweep would act on:"
  python3 "$TOOL" --root "$MEDIA_ROOT" --max-age-days "$MAX_AGE_DAYS" --keep-latest "$KEEP_LATEST"
  echo
  echo "Re-run with --apply to install the cron entry that deletes."
  exit 0
fi

if [ "$(id -u)" != 0 ]; then
  echo "install-media-retention: --apply needs root (writes $TARGET and $CRON_FILE)" >&2
  exit 1
fi

install -m 0755 "$TOOL" "$TARGET"
printf '# Sweeps the Monarch media library: %s day age limit, newest %s episodes kept.\n%s\n' \
  "$MAX_AGE_DAYS" "$KEEP_LATEST" "$CRON_LINE" > "$CRON_FILE"
chmod 0644 "$CRON_FILE"
echo
echo "installed. First run at the next $SCHEDULE; check with: tail -f /var/log/syslog | grep media-retention"
