#!/usr/bin/env bash
set -euo pipefail

# Download the Monarch Media Platform offline bundle (deployment payload + Docker image
# archives) from the GitHub release, verify checksums, and unpack it so it can
# be handed to install-monarch.sh (or copied to a USB stick for offline use).
#
# Usage: fetch-offline-bundle.sh [OUT_DIR]   (default: ~/monarch-offline-bundle)

REPO_SLUG="${MONARCH_REPO:-innotelinc/monarch}"
RELEASE_TAG="${MONARCH_RELEASE_TAG:-v1.0.0}"
OUT_DIR="${1:-${MONARCH_OUT_DIR:-$HOME/monarch-offline-bundle}}"
BASE_URL="https://github.com/${REPO_SLUG}/releases/download/${RELEASE_TAG}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v curl >/dev/null 2>&1 || { echo "curl is required (install it or run from the Monarch live image)" >&2; exit 1; }

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

echo "Downloading from $BASE_URL"

for file in monarch-deployment.tar.gz SHA256SUMS; do
  echo "  $file"
  curl -fL --retry 3 --retry-delay 2 -o "$file" "$BASE_URL/$file"
done

# Docker image archives are split into <2 GB parts.
i=0
while :; do
  part="$(printf 'docker-images-part%02d.tar.gz' "$i")"
  if curl -fsSL --retry 3 --retry-delay 2 -o "$part" "$BASE_URL/$part" 2>/dev/null; then
    echo "  $part"
    i=$(( i + 1 ))
  else
    rm -f "$part"
    break
  fi
done
[ "$i" -gt 0 ] || { echo "No docker image archives found for $RELEASE_TAG" >&2; }

echo "Verifying checksums..."
sha256sum -c SHA256SUMS

echo "Unpacking the deployment payload..."
tar -xzf monarch-deployment.tar.gz

echo "Reassembling the Docker image archives..."
: > docker-images.tar.gz
for part in docker-images-part*.tar.gz; do
  [ -e "$part" ] || continue
  cat "$part" >> docker-images.tar.gz
done
# The bundle is a multi-member gzip (one `docker save | gzip` per image) split
# into <2 GB parts. Split it back into per-image .tar.gz archives under
# dist/docker-images/, ready for `gzip -dc | docker load`.
mkdir -p dist/docker-images
bash "$SCRIPT_DIR/split-image-bundle.sh" docker-images.tar.gz dist/docker-images
rm -f docker-images.tar.gz

echo "======================================================"
echo " Offline bundle ready at: $OUT_DIR"
echo " Install it with:  $OUT_DIR/scripts/install-monarch.sh"echo " Or copy the whole directory onto a USB stick for a"
 echo " fully offline install from the Monarch live image."
echo "======================================================"