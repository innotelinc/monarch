#!/usr/bin/env bash
# jellyfin-plugin-oidc-rebuild.sh — rebuild the OIDC plugin for the Jellyfin we run.
#
# WHY THIS EXISTS
# ---------------
# Jellyfin 12.0 moved the server to .NET 10, and a plugin built for 10.11 does
# not load on it: the assembly declares `targetAbi 10.11.0.0`, so Jellyfin never
# registers the provider it brings, and every account it owns is reported as
# "invalid/missing Authentication Provider". That is what made the Cerulean
# Authentik button disappear from Jellyfin's login page while the login form kept
# working through LDAP (see docs/operations.md).
#
# Upstream (`Ezeqielle/jellyfin-plugin-oidc`) has no 12.0 release, so this rebuilds
# its pinned tag against the 12.0 plugin API. The changes are deliberately the
# smallest ones that make it a 12.0 build:
#
#   * `TargetFramework` net9.0 -> net10.0
#   * `Jellyfin.Controller`/`Jellyfin.Model` 10.11.10 -> the 12.0 packages
#   * drop `IAuthenticationProvider.HasPassword`, which 12.0 removed from the
#     interface (a plugin that still implements it does not compile)
#
# The result is committed under `init/artifacts/` and pinned in
# `init/jellyfin-plugins.json` by its zip and assembly sha256, because upstream's
# `meta.json` carries no `sourceUrl` and the version had to be bumped to 1.0.10.1
# to say so. `init/init.py` and `scripts/jellyfin-plugin-pin.py` both install
# straight from that pin.
#
# USAGE
# -----
# Run on a host with docker and network access (the build box is enough):
#
#     scripts/jellyfin-plugin-oidc-rebuild.sh
#
# Environment overrides: TAG (v1.0.10), VERSION (1.0.10.1), TF_MONIKER (net10.0),
# JELLYFIN_PACKAGES (12.0.0), WORK (/tmp/oidc-rebuild), SDK_IMAGE
# (mcr.microsoft.com/dotnet/sdk:10.0).
#
# It prints the zip's sha256 and the assembly's sha256 at the end. If either
# differs from `init/jellyfin-plugins.json`, update the pin (asset_bytes,
# asset_sha256, assembly_sha256) and copy the zip over
# `init/artifacts/jellyfin-plugin-oidc-<VERSION>-<TF_MONIKER>.zip` — nothing else
# in the deployment changes.

set -euo pipefail

TAG="${TAG:-v1.0.10}"
VERSION="${VERSION:-1.0.10.1}"
TF_MONIKER="${TF_MONIKER:-net10.0}"
JELLYFIN_PACKAGES="${JELLYFIN_PACKAGES:-12.0.0}"
SDK_IMAGE="${SDK_IMAGE:-mcr.microsoft.com/dotnet/sdk:10.0}"
WORK="${WORK:-/tmp/oidc-rebuild}"
REPO="${REPO:-https://github.com/Ezeqielle/jellyfin-plugin-oidc}"

command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }

rm -rf "$WORK"
mkdir -p "$WORK"
git clone --quiet --depth 1 --branch "$TAG" "$REPO" "$WORK/src"
printf 'cloned %s at %s\n' "$REPO" "$(git -C "$WORK/src" rev-parse --short HEAD)"

CSPROJ="$WORK/src/Jellyfin.Plugin.OIDC/Jellyfin.Plugin.OIDC.csproj"
PROVIDER="$WORK/src/Jellyfin.Plugin.OIDC/Auth/OidcAuthProvider.cs"

sed -i "s|<TargetFramework>net9.0</TargetFramework>|<TargetFramework>${TF_MONIKER}</TargetFramework>|" "$CSPROJ"
sed -i "s|Version=\"10\.11\.[0-9]*\"|Version=\"${JELLYFIN_PACKAGES}\"|g" "$CSPROJ"

# 12.0 dropped HasPassword from IAuthenticationProvider; keeping it is a compile
# error, not a warning.
python3 - "$PROVIDER" <<'PY'
import re
import sys
path = sys.argv[1]
source = open(path, encoding="utf-8").read()
patched = re.sub(r"\s*public bool HasPassword\(User user\)\s*\{\s*return false;\s*\}\s*",
                 "\n", source)
if "HasPassword" in patched:
    raise SystemExit("could not remove the HasPassword override — 12.0 dropped it")
open(path, "w", encoding="utf-8").write(patched)
PY

docker run --rm -v "$WORK:/work" -w /work/src/Jellyfin.Plugin.OIDC "$SDK_IMAGE" \
    dotnet publish -c Release -o /work/out \
    -p:Version="$VERSION" -p:AssemblyVersion="$VERSION" -p:FileVersion="$VERSION"

python3 - "$WORK" "$VERSION" "$TF_MONIKER" "$JELLYFIN_PACKAGES" <<'PY'
import hashlib
import json
import os
import sys
import zipfile

work, version, tfm, jf = sys.argv[1:5]
out = os.path.join(work, "out")
stage = os.path.join(work, "stage")
os.makedirs(stage, exist_ok=True)
for name in os.listdir(out):
    if name.endswith(".dll"):
        with open(os.path.join(out, name), "rb") as fh:
            data = fh.read()
        with open(os.path.join(stage, name), "wb") as fh:
            fh.write(data)

meta = json.load(open(os.path.join(out, "meta.json"), encoding="utf-8"))
meta["versions"] = [{
    "version": version,
    "changelog": (f"Rebuild of the upstream tag against the Jellyfin {jf} API ({tfm}): retargeted "
                  "the framework, bumped Jellyfin.Controller/Model, and dropped the "
                  "IAuthenticationProvider.HasPassword override 12.0 removed. No functional "
                  "change to the OIDC login, RBAC or Quick Connect flows."),
    "targetAbi": "12.0.0.0",
    "sourceUrl": "",
    "timestamp": "2026-01-01T00:00:00Z",
}] + [v for v in meta.get("versions", []) if v.get("version") != version]
with open(os.path.join(stage, "meta.json"), "w", encoding="utf-8") as fh:
    fh.write(json.dumps(meta, indent=2) + "\n")

archive = os.path.join(work, "oidc-rbac.zip")
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
    for name in sorted(os.listdir(stage)):
        bundle.write(os.path.join(stage, name), name)

blob = open(archive, "rb").read()
dll = open(os.path.join(stage, "Jellyfin.Plugin.OIDC.dll"), "rb").read()
print()
print(f"artifact    {archive}")
print(f"copy it to  init/artifacts/jellyfin-plugin-oidc-{version}-{tfm}.zip")
print(f"asset_bytes       {len(blob)}")
print(f"asset_sha256      {hashlib.sha256(blob).hexdigest()}")
print(f"assembly_sha256   {hashlib.sha256(dll).hexdigest()}")
print()
print("A .NET build is not byte-reproducible, so these hashes will differ from the")
print("ones already pinned even for the same source. That is expected: the pin names")
print("the committed artifact, so replace that file and update the pin together.")
PY
