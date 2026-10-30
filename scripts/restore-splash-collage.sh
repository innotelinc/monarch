#!/bin/sh
# Keep Jellyfin's generated splash collage replaced by Monarch's artwork.
#
# WHY THIS EXISTS
# ---------------
# `SplashscreenPostScanTask` rebuilds `{DataPath}/splashscreen.png` from up to 30
# posters and 30 thumbnails after **every** library scan, so the file reverts to a
# poster wall on its own. Measured on the media host 2026-10-30: a "Scan Media
# Library" run changed that file's SHA-256 while `GET /Branding/Splashscreen`
# kept answering with the `SplashscreenLocation` image — the custom splash is what
# the login screen and the TV clients ask for, and it wins. The collage still
# matters twice: it is the fallback a client shows when the location is not
# honoured, and it is the file an operator finds when they look in the data dir.
#
# `monarch-splash-guard.path` runs this whenever that file is written. The copy
# only happens when the bytes actually differ, so the path unit does not loop on
# this script's own write: one restore, then one no-op run, then quiet.
set -eu

collage=${SPLASH_COLLAGE:-/docker/appdata/jellyfin/data/data/splashscreen.png}
asset=${SPLASH_ASSET:-}

# The *installed* draw is the truth, not the repo's 4K asset: a host carrying the
# 1080p render (`--apply --scale 1`) must have the collage restored from that
# render, or the fallback and the SplashscreenLocation disagree by 4× the bytes.
if [ -z "$asset" ]; then
  for candidate in "${collage%/*}/monarch-splash.png" "${collage%/*}/monarch-splash-1080.png" \
                   /opt/monarch/assets/monarch-splash.png; do
    if [ -f "$candidate" ]; then asset=$candidate; break; fi
  done
fi

[ -f "$asset" ] || { echo "missing splash asset: ${asset:-none}" >&2; exit 0; }
[ -f "$collage" ] || exit 0
cmp -s -- "$asset" "$collage" && exit 0
cp -- "$asset" "$collage"
echo "restored $collage from $asset"
