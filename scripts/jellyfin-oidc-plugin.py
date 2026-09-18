#!/usr/bin/env python3
"""jellyfin-oidc-plugin.py — install the Jellyfin SSO button from a pinned release.

WHY THIS EXISTS
---------------
Jellyfin's login page offers a **Cerulean Authentik** button because of a plugin
that Jellyfin's own catalog does not carry: `Jellyfin.Plugin.OIDC.dll`, installed
by hand into `/docker/appdata/jellyfin/data/plugins/`. That was the one hole in an
otherwise self-wiring stack — a rebuilt host comes back with a login form and no
SSO, which is the posture `jellyfin-sso` no longer gates and therefore no longer
covers.

The plugin's own `meta.json` is no help either: it ships `"sourceUrl": ""`, so
nothing recorded *which* build was installed. Measured on 2026-09-18, it is
`Ezeqielle/jellyfin-plugin-oidc` **v1.0.10** —

    installed Jellyfin.Plugin.OIDC.dll  sha256 7f023992c632de03e20bb445e262ce7916f955e4d627c70416cc6acefe0dedd9
    inside v1.0.10 oidc-rbac.zip        sha256 f9945726a1482c576151382cae05fcbd185c0c157bd783b584151a2348eab32d

— and both hashes are asserted here, so a different build is a *finding* rather
than a silent upgrade. `init/jellyfin-oidc-plugin.json` is the single place the
pin lives; `monarch-init` reads the same file, so a fresh install and this host
end up with the same plugin.

WHAT IT DOES NOT DO
-------------------
It does not write the plugin's *configuration* (`Jellyfin.Plugin.OIDC.xml` — the
provider, client id/secret and role mappings). That is `monarch-init`'s
(`write_oidc_plugin_config`), and `scripts/jellyfin-oidc-sso.py` checks the result
end to end. This script owns the binary and only the binary.

USAGE
-----
    python3 scripts/jellyfin-oidc-plugin.py --check      # is the pin installed? (default)
    python3 scripts/jellyfin-oidc-plugin.py --status     # the pin, and what is on disk
    python3 scripts/jellyfin-oidc-plugin.py --install    # fetch, verify, extract

`--install` verifies the release zip's sha256 **and** the assembly inside it
before anything is written, then extracts into
`<plugins>/<plugin_dir>/` and hands the files to uid 1000 (the uid Jellyfin runs
as). Jellyfin only picks a plugin up on restart, which the script says rather than
does: restarting someone's media server is an operator's call, and `drift-check`
runs this read-only.

Exit codes: 0 the pinned plugin is installed (or was just installed), 1 it is not
(or the download did not match the pin), 2 cannot run (no manifest, unreadable
plugins dir).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "init" / "jellyfin-oidc-plugin.json"
DEFAULT_PLUGINS_DIR = Path("/docker/appdata/jellyfin/data/plugins")
# Jellyfin runs as PUID/PGID 1000 in this stack, and a plugin it cannot read is a
# plugin it does not load.
PLUGIN_UID = PLUGIN_GID = 1000


class CannotRun(RuntimeError):
    """Nothing on disk to judge, or the run cannot proceed."""


class Refused(RuntimeError):
    """The download did not match the pin — nothing was written."""


def load_pin(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CannotRun(f"cannot read the plugin pin at {path} ({error})") from None
    except json.JSONDecodeError as error:
        raise CannotRun(f"{path} is not valid JSON ({error})") from None
    for key in ("repo", "tag", "asset", "asset_sha256", "assembly", "assembly_sha256",
                "plugin_dir"):
        if not payload.get(key):
            raise CannotRun(f"{path} does not pin {key!r}")
    return payload


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_assembly(plugins_dir: Path, assembly: str) -> Path | None:
    """The installed copy of the assembly, wherever Jellyfin's scan put it."""
    if not plugins_dir.is_dir():
        raise CannotRun(f"{plugins_dir} is not a directory — has Jellyfin run?")
    for folder in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        candidate = folder / assembly
        if candidate.is_file():
            return candidate
    return None


def version_of(assembly_path: Path) -> str:
    """The version the plugin's own meta.json records, if it records one."""
    try:
        meta = json.loads((assembly_path.parent / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    versions = meta.get("versions") if isinstance(meta, dict) else None
    for entry in versions or []:
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    return ""


def describe(pin: dict, plugins_dir: Path) -> tuple[str, Path | None, str]:
    """(status, assembly path, detail) for what is on disk versus the pin."""
    try:
        assembly_path = installed_assembly(plugins_dir, pin["assembly"])
    except CannotRun as error:
        return "cannot-run", None, str(error)
    if assembly_path is None:
        return "absent", None, (f"no installed plugin carries {pin['assembly']} — Jellyfin's "
                                f"login page offers no SSO button")
    digest = sha256_file(assembly_path)
    if digest != pin["assembly_sha256"]:
        version = version_of(assembly_path) or "version not recorded"
        return "drift", assembly_path, (
            f"{assembly_path} is {version} and hashes to {digest[:16]}…, but the pin is "
            f"{pin['tag']} (…{pin['assembly_sha256'][:16]}…)")
    return "ok", assembly_path, (
        f"{pin['assembly']} {version_of(assembly_path) or pin['version']} from "
        f"{pin['repo']} {pin['tag']}")


def fetch(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "monarch-jellyfin-oidc"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise Refused(f"{url} answered HTTP {error.code}") from None
    except OSError as error:
        raise Refused(f"cannot reach {url} ({error})") from None


def install(pin: dict, plugins_dir: Path, source_url: str = "", log=print) -> bool:
    """Fetch the pinned zip, verify it, and extract it. True when it changed."""
    url = source_url or pin.get("release_url") or (
        f"https://github.com/{pin['repo']}/releases/download/{pin['tag']}/{pin['asset']}")
    log(f"  fetching {url}")
    blob = fetch(url)

    if pin.get("asset_bytes") and len(blob) != int(pin["asset_bytes"]):
        raise Refused(f"{pin['asset']} is {len(blob)} bytes, the pin says {pin['asset_bytes']}")
    digest = sha256_bytes(blob)
    if digest != pin["asset_sha256"]:
        raise Refused(f"{pin['asset']} hashes to {digest}, the pin says {pin['asset_sha256']}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / pin["asset"]
        archive.write_bytes(blob)
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = [name for name in bundle.namelist() if name.endswith(pin["assembly"])]
                if not members:
                    raise Refused(f"{pin['asset']} does not contain {pin['assembly']}")
                # The assembly is what Jellyfin loads: check it before it is
                # anywhere near the plugins directory.
                if sha256_bytes(bundle.read(members[0])) != pin["assembly_sha256"]:
                    raise Refused(
                        f"the {pin['assembly']} inside {pin['asset']} does not match the pin")
                target = plugins_dir / pin["plugin_dir"]
                target.mkdir(parents=True, exist_ok=True)
                bundle.extractall(target)
        except zipfile.BadZipFile as error:
            raise Refused(f"{pin['asset']} is not a zip file ({error})") from None

    target = plugins_dir / pin["plugin_dir"]
    for root, _dirs, files in os.walk(target):
        for name in files:
            _chown(Path(root) / name, log)
        _chown(Path(root), log)
    _chown(target, log)
    log(f"  installed {pin['assembly']} {pin['version']} into {target}")
    return True


def _chown(path: Path, log) -> None:
    """Hand a file to the uid Jellyfin runs as; best effort (and skippable in tests)."""
    if os.geteuid() != 0:
        return
    try:
        os.chown(path, PLUGIN_UID, PLUGIN_GID)
    except OSError as error:
        log(f"  note: could not chown {path.name} to {PLUGIN_UID}:{PLUGIN_GID} ({error})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="is the pinned plugin installed? (default)")
    parser.add_argument("--install", action="store_true",
                        help="fetch the pinned release, verify it, and extract it")
    parser.add_argument("--status", action="store_true",
                        help="print the pin and what is installed")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help=f"the plugin pin (default {DEFAULT_MANIFEST})")
    parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR,
                        help=f"Jellyfin's plugin directory (default {DEFAULT_PLUGINS_DIR})")
    parser.add_argument("--source-url", default="",
                        help="fetch the zip from here instead of the release URL "
                             "(used to install from a local mirror, and by the tests)")
    args = parser.parse_args(argv)

    try:
        pin = load_pin(args.manifest)
    except CannotRun as error:
        print(f"jellyfin-oidc-plugin: {error}", file=sys.stderr)
        return 2

    print(f"jellyfin-oidc-plugin [{'install' if args.install else 'check'}] {args.plugins_dir}")
    print(f"  pin         {pin['repo']} {pin['tag']} ({pin['asset']}, "
          f"{pin.get('asset_bytes', '?')} bytes)")
    status, assembly_path, detail = describe(pin, args.plugins_dir)

    if status == "cannot-run":
        print(f"jellyfin-oidc-plugin: {detail}", file=sys.stderr)
        return 2
    if args.status:
        print(f"  installed   {assembly_path if assembly_path else '(nothing)'}")
        print(f"  status      {status}: {detail}")
        return 0 if status == "ok" else 1

    if status == "ok":
        print(f"  ok          {detail}")
        print("\nok: the pinned OIDC plugin is installed")
        return 0

    if not args.install:
        print(f"  FAIL        {detail}", file=sys.stderr)
        print("\nFAIL: Jellyfin's login page has no SSO button from the pinned release — "
              "run `python3 scripts/jellyfin-oidc-plugin.py --install`, then restart "
              "Jellyfin so it loads the plugin", file=sys.stderr)
        return 1

    try:
        install(pin, args.plugins_dir, args.source_url)
    except Refused as error:
        print(f"jellyfin-oidc-plugin: {error}", file=sys.stderr)
        print("nothing was written; the pin in init/jellyfin-oidc-plugin.json is the "
              "expected build", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"jellyfin-oidc-plugin: cannot write to {args.plugins_dir} ({error})",
              file=sys.stderr)
        return 1

    status, assembly_path, detail = describe(pin, args.plugins_dir)
    if status != "ok":
        print(f"jellyfin-oidc-plugin: installed, but it still reads as {status}: {detail}",
              file=sys.stderr)
        return 1
    print(f"  ok          {detail}")
    print("\ninstalled. Restart Jellyfin so it loads the plugin:  docker restart jellyfin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
