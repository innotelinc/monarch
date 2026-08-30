#!/usr/bin/env bash
set -euo pipefail

# Build the ARR Media Stack offline release bundle:
#
#   arr-deployment.tar.gz        - full source + compose + systemd payload
#   docker-images-partN.tar.gz   - Docker image archives (split < 2 GB each
#                                  so they can be uploaded to GitHub)
#   SHA256SUMS                   - checksums for the above
#
# Output goes to dist/offline-bundle/ by default.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO/dist/offline-bundle}"
IMAGES_FILE="${IMAGES_FILE:-$REPO/scripts/offline-images.txt}"
MAX_PART_BYTES="${MAX_PART_BYTES:-1900000000}"   # stay under GitHub's 2 GB limit
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$OUT_DIR"
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }

echo "== Building deployment bundle (source + compose + systemd) =="
mkdir -p "$STAGE/src"
git -C "$REPO" archive --format=tar HEAD | tar -xf - -C "$STAGE/src" 2>/dev/null || \
  cp -a "$REPO/." "$STAGE/src/"
for file in scripts/install-arr.sh scripts/fetch-offline-bundle.sh \
            scripts/split-image-bundle.sh \
            scripts/build-source-bundle.sh scripts/build-offline-bundle.sh \
            scripts/build-live-usb.sh scripts/offline-images.txt; do
  mkdir -p "$STAGE/src/$(dirname "$file")"
  cp "$REPO/$file" "$STAGE/src/$file" 2>/dev/null || true
done
rm -rf "$STAGE/src/.env" "$STAGE/src/.git" "$STAGE/src/dist" "$STAGE/src/.live-build"
tar -czf "$OUT_DIR/arr-deployment.tar.gz" -C "$STAGE/src" .

DEPLOYMENT_ONLY=0
[ "${1:-}" = "--deployment-only" ] && DEPLOYMENT_ONLY=1

if [ "$DEPLOYMENT_ONLY" -eq 1 ]; then
  echo "== Checksums (deployment + any existing image parts) =="
  ( cd "$OUT_DIR" && shopt -s nullglob && sha256sum arr-deployment.tar.gz docker-images-part*.tar.gz > SHA256SUMS )
  echo "Created in $OUT_DIR:"
  ls -lh "$OUT_DIR"
  exit 0
fi

echo "== Building Docker image archives (streamed) =="
IMAGES=()
while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | xargs)"
  [ -n "$line" ] || continue
  IMAGES+=("$line")
done < "$IMAGES_FILE"

# Stream each `docker save` into the split parts directly, so we never hold
# both the unpacked image archives AND the packed parts on disk at once (the
# previous two-stage build kept ~2x the bundle size around and could exhaust
# a constrained CI runner). Each per-image gzip is a separate member of one
# multi-member gzip stream; the parts are just that stream split < 2 GB each
# (cat parts* reconstructs it). Consumers split members back out with
# scripts/split-image-bundle.sh.
rm -f "$OUT_DIR"/docker-images-part*.tar.gz
{
  for img in "${IMAGES[@]}"; do
    echo "-- Pulling $img"
    docker pull "$img"
    echo "-- Saving $img (streamed)"
    docker save "$img" | gzip -1
  done
} | split -b "$MAX_PART_BYTES" -d -a 2 - "$OUT_DIR/docker-images-part"
# split names parts part00, part01, ...; add the .tar.gz suffix and drop any
# trailing empty part produced by an exact-size split.
for f in "$OUT_DIR"/docker-images-part??; do
  if [ ! -s "$f" ]; then
    rm -f "$f"
  else
    mv "$f" "$f.tar.gz"
  fi
done
[ -n "$(echo "$OUT_DIR"/docker-images-part*.tar.gz)" ] || { \
  echo "No image parts produced - build or pull failed." >&2; exit 1; }

echo "== Verifying streamed bundle (cat parts* must be a valid multi-member gzip) =="
cat "$OUT_DIR"/docker-images-part*.tar.gz | gzip -t && echo "gzip OK"

echo "== Checksums =="
( cd "$OUT_DIR" && sha256sum arr-deployment.tar.gz docker-images-part*.tar.gz > SHA256SUMS )

echo "Created in $OUT_DIR:"
ls -lh "$OUT_DIR"