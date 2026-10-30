#!/bin/sh
# check-splash-guard.sh — is the collage guard actually running on this host?
#
# WHY THIS EXISTS
# ---------------
# `monarch-splash-guard.path` is what keeps Jellyfin's generated collage
# (`{DataPath}/splashscreen.png`, rebuilt from up to 30 posters and 30 thumbnails
# after *every* library scan) from reverting to a poster wall. Installing the
# splash is not enough on its own: measured on the media host 2026-10-30, a scan
# rewrote that file while `GET /Branding/Splashscreen` kept serving the custom
# `SplashscreenLocation` — so the file an operator finds, and the fallback a
# client shows when the location is not honoured, quietly became a mosaic again.
#
# A unit that is installed but not `enabled` stops watching at the next boot; one
# that is not `active` misses the scan happening now; and a guard that is running
# can still be watching the wrong file (a data path that moved), in which case the
# collage is a poster wall whether or not systemd says the unit is fine. This
# judges all three, read-only, for `drift-check.sh`.
#
# Exit codes, this repo's convention: 0 = the guard is enabled, active, and the
# collage carries the installed splash; 1 = a finding; 2 = cannot look (no
# generated collage here, so this is not the media host, or no systemctl).
set -u

collage=${SPLASH_COLLAGE:-/docker/appdata/jellyfin/data/data/splashscreen.png}
unit=${SPLASH_GUARD_UNIT:-monarch-splash-guard.path}
unit_dir=${SPLASH_GUARD_DIR:-/etc/systemd/system}
systemctl=${SPLASH_SYSTEMCTL:-systemctl}

# The installed draw is the truth here, not the repo's 4K asset: a host carrying
# the 1080p render must have the collage restored from *that* render, or the two
# files disagree by a factor of four. Same order as restore-splash-collage.sh.
installed=${SPLASH_INSTALLED:-}
if [ -z "$installed" ]; then
  for candidate in "${collage%/*}/monarch-splash.png" "${collage%/*}/monarch-splash-1080.png"; do
    if [ -f "$candidate" ]; then installed=$candidate; break; fi
  done
fi

# No collage at all is not "the guard is broken": it means Jellyfin's data dir is
# not on this host, which is a host to skip rather than one to page about — the
# same rule the splash check itself follows.
[ -f "$collage" ] || {
  echo "no generated collage at $collage — Jellyfin's data dir is not on this host"
  exit 2
}

if ! command -v "$systemctl" >/dev/null 2>&1; then
  echo "$systemctl is not available, so the guard's state cannot be read"
  exit 2
fi

# Installed at all: the module package ships the unit, an operator installs it by
# hand (docs/operations.md), so "genuinely absent" and "present but stopped" are
# different sentences.
if ! "$systemctl" list-unit-files --no-legend "$unit" 2>/dev/null | grep -q . \
   && [ ! -f "$unit_dir/$unit" ]; then
  echo "$unit is not installed here — the next library scan puts a poster wall back at $collage (install systemd/$unit; see docs/operations.md)"
  exit 1
fi

enabled=$("$systemctl" is-enabled "$unit" 2>/dev/null || true)
if [ "$enabled" != enabled ]; then
  echo "$unit is ${enabled:-not enabled}, not enabled — it stops watching at the next boot (systemctl enable --now $unit)"
  exit 1
fi

active=$("$systemctl" is-active "$unit" 2>/dev/null || true)
if [ "$active" != active ]; then
  echo "$unit is ${active:-not active}, not active — a scan that runs now leaves the poster wall in place (systemctl start $unit)"
  exit 1
fi

if [ -n "$installed" ] && [ -f "$installed" ] && ! cmp -s -- "$installed" "$collage"; then
  echo "$collage is not $installed — the guard is running but the collage was not restored"
  exit 1
fi

echo "$unit is enabled and active, and $collage carries the installed splash"
exit 0
