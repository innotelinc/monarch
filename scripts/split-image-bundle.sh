#!/usr/bin/env bash
set -euo pipefail

# Split the streamed offline Docker image bundle into per-image .tar.gz
# archives, one per gzip member.
#
# The bundle produced by build-offline-bundle.sh is a single multi-member gzip
# stream (one `docker save | gzip -1` blob per image) split into
# <2 GB "part" files; `cat part*` reconstructs it. This script walks the gzip
# member boundaries and writes each member to `<outdir>/imageNN.tar.gz`, ready
# for `gzip -dc <member> | docker load`.
#
# Usage: split-image-bundle.sh <bundle.gz> <outdir>
#   bundle.gz  the concatenated (or single-part) image bundle
#   outdir     directory that receives image00.tar.gz, image01.tar.gz, ...

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <bundle.gz> <outdir>" >&2
  exit 2
fi
BUNDLE="$1"
OUTDIR="$2"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
mkdir -p "$OUTDIR"

python3 - "$BUNDLE" "$OUTDIR" <<'PY'
import os, struct, sys, zlib

src, outdir = sys.argv[1], sys.argv[2]
with open(src, 'rb') as f:
    data = f.read()

pos, idx, written = 0, 0, 0
while pos < len(data):
    if data[pos:pos+2] != b'\x1f\x8b':
        raise SystemExit(f"error: not a gzip member at byte {pos} (corrupt bundle?)")
    flg = data[pos+3]
    hlen = 10
    if flg & 4:    # FEXTRA
        xlen = struct.unpack('<H', data[pos+10:pos+12])[0]
        hlen += 2 + xlen
    if flg & 8:    # FNAME
        j = data.index(b'\x00', pos + hlen)
        hlen += (j - (pos + hlen)) + 1
    if flg & 16:   # FCOMMENT
        j = data.index(b'\x00', pos + hlen)
        hlen += (j - (pos + hlen)) + 1
    if flg & 2:    # FHCRC
        hlen += 2
    # decompress remaining stream to find where the deflate data (and its
    # 8-byte trailer) ends; zlib tells us how much input it consumed.
    d = zlib.decompressobj(-zlib.MAX_WBITS)
    rest = data[pos + hlen:]
    try:
        d.decompress(rest)
    except zlib.error:
        pass
    defl = len(rest) - len(d.unused_data)
    member_end = pos + hlen + defl + 8          # +8 = CRC32 + ISIZE trailer
    with open(os.path.join(outdir, "image%02d.tar.gz" % idx), 'wb') as out:
        out.write(data[pos:member_end])
    written += 1
    pos = member_end
    idx += 1

print(f"split {written} image archive(s) into {outdir}")
PY