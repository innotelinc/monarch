#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO/dist/source-bundle}"
NAME="${NAME:-arr-source-bundle.tar.gz}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT_DIR"

# Export tracked files plus intentionally included deployment assets, while
# excluding secrets, Git metadata, runtime state, and generated archives.
git -C "$REPO" archive --format=tar HEAD | tar -xf - -C "$STAGE" 2>/dev/null || \
  cp -a "$REPO/." "$STAGE/"
# Include current tracked deployment scripts even when this builder is run from
# a worktree whose HEAD predates the latest installer additions.
for file in scripts/build-live-usb.sh scripts/build-source-bundle.sh \
            scripts/fetch-offline-bundle.sh scripts/install-arr.sh \
            scripts/split-image-bundle.sh scripts/build-offline-bundle.sh \
            scripts/offline-images.txt; do
  mkdir -p "$STAGE/$(dirname "$file")"
  cp "$REPO/$file" "$STAGE/$file" 2>/dev/null || true
done
rm -rf "$STAGE/.env" "$STAGE/.git" "$STAGE/dist" "$STAGE/.live-build"
tar -czf "$OUT_DIR/$NAME" -C "$STAGE" .
sha256sum "$OUT_DIR/$NAME" > "$OUT_DIR/$NAME.sha256"
echo "Created $OUT_DIR/$NAME"