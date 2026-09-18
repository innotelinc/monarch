#!/usr/bin/env python3
"""jellyfin-plugin-pin.py — judge (and repair) the Jellyfin plugin builds this stack runs.

WHY THIS EXISTS
---------------
Two plugins are load-bearing here and neither records which build it is:

  * **OIDC RBAC** (`Jellyfin.Plugin.OIDC.dll`) draws the **Cerulean Authentik**
    button on Jellyfin's login page. Jellyfin's own catalog does not carry it and
    its `meta.json` ships `"sourceUrl": ""`, so it used to be installed by hand.
  * **LDAP Authentication** (`LDAP-Auth.dll`) is the credential store behind that
    page — the Authentik LDAP outpost. It reports an empty `versions` list, so
    nothing on disk says which release it came from either.

Without a pin, "which plugin is installed" has no answer: a rebuilt host came
back with a login form and no SSO button, and a *second* copy of the LDAP plugin
(v23 beside v24) made every authentication throw `InvalidCastException` — the
login form answering HTTP 500 for a correct password exactly as it did for a
wrong one, which reads as "the password is wrong" to whoever is typing it.

So `init/jellyfin-plugins.json` records, per plugin, the release **and the sha256
of both the zip and the assembly inside it**, and this script is the one place
that decides whether what is on disk is that build. `monarch-init` installs from
the same file, `scripts/drift-check.sh` runs `--check` against the deployment, and
`init/init.py` writes the plugin's *config* (this script owns the binary and only
the binary).

WHAT IT ADDS BESIDES THE HASH
-----------------------------
**Duplicate copies are a failure.** Jellyfin loads every plugin folder that
carries the assembly, and two copies of one auth plugin is not cosmetic: the
configuration type is then cast across two load contexts and every authentication
fails. Folders retired by renaming (`LDAP-Auth.superseded-20260918-140321`) are
not counted — Jellyfin itself reports those as `Superseded` rather than loading
them — but any other second copy is reported, named, and fails the check.

USAGE
-----
    python3 scripts/jellyfin-plugin-pin.py --check              # every pin (default)
    python3 scripts/jellyfin-plugin-pin.py --status             # the pins, and what is on disk
    python3 scripts/jellyfin-plugin-pin.py --install            # fetch, verify, extract what is missing
    python3 scripts/jellyfin-plugin-pin.py --plugin ldap        # just one pin

`--install` verifies the zip's sha256 **and** the assembly inside it before
anything is written, extracts into `<plugins>/<plugin_dir>/`, and hands the files
to uid 1000 (the uid Jellyfin runs as). Jellyfin only picks a plugin up on
restart, which the script says rather than does: restarting someone's media server
is an operator's call, and drift-check runs this read-only.

Exit codes: 0 the pinned builds are installed (or were just installed), 1 one of
them is not, 2 cannot run (no pin file, unreadable plugins directory).
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
DEFAULT_MANIFEST = REPO_ROOT / "init" / "jellyfin-plugins.json"
DEFAULT_PLUGINS_DIR = Path("/docker/appdata/jellyfin/data/plugins")
# Jellyfin runs as PUID/PGID 1000 in this stack, and a plugin it cannot read is a
# plugin it does not load.
PLUGIN_UID = PLUGIN_GID = 1000
# A folder renamed to retire it. Jellyfin reports these as `Superseded` and does
# not load them, so they are not a second copy — everything else is.
RETIRED_MARKER = "superseded"
REQUIRED_PIN_FIELDS = ("name", "repo", "tag", "asset", "asset_sha256", "assembly",
                       "assembly_sha256", "plugin_dir")


class CannotRun(RuntimeError):
    """Nothing on disk to judge, or the run cannot proceed."""


class Refused(RuntimeError):
    """The download did not match the pin — nothing was written."""


# ── the pins ────────────────────────────────────────────────────────────────

def load_pins(path: Path) -> list[dict]:
    """Every pinned plugin, in the order the file lists them."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CannotRun(f"cannot read the plugin pins at {path} ({error})") from None
    except json.JSONDecodeError as error:
        raise CannotRun(f"{path} is not valid JSON ({error})") from None

    plugins = payload.get("plugins") if isinstance(payload, dict) else payload
    if not isinstance(plugins, list) or not plugins:
        raise CannotRun(f"{path} pins no plugins")
    for pin in plugins:
        if not isinstance(pin, dict):
            raise CannotRun(f"{path} has a plugin entry that is not an object")
        for key in REQUIRED_PIN_FIELDS:
            if not pin.get(key):
                raise CannotRun(f"{path} does not pin {key!r} for "
                                f"{pin.get('name') or '(unnamed plugin)'}")
    return plugins


def pin_named(pins: list[dict], name: str) -> dict:
    """One pin by name, for the callers that only want one."""
    for pin in pins:
        if pin["name"] == name:
            return pin
    raise CannotRun(f"no pin named {name!r} "
                    f"(have: {', '.join(p['name'] for p in pins)})")


# ── what is on disk ─────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def carrying_assemblies(plugins_dir: Path, assembly: str) -> list[Path]:
    """Every folder Jellyfin would load this assembly from, retired ones excluded."""
    if not plugins_dir.is_dir():
        raise CannotRun(f"{plugins_dir} is not a directory — has Jellyfin run?")
    found = []
    for folder in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        if not (folder / assembly).is_file():
            continue
        if RETIRED_MARKER in folder.name:
            continue
        found.append(folder)
    return found


def version_of(assembly_path: Path) -> str:
    """The version the plugin's own meta.json records, if it records one.

    Both pinned plugins ship an empty (or absent) version list, so this is often
    blank — which is why the pin, not the plugin, is what says which build this is.
    """
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
    """(status, assembly path, detail) for one pin against what is on disk."""
    try:
        folders = carrying_assemblies(plugins_dir, pin["assembly"])
    except CannotRun as error:
        return "cannot-run", None, str(error)

    if not folders:
        return "absent", None, (f"no installed plugin carries {pin['assembly']} — "
                                f"{pin['title']}")
    if len(folders) > 1:
        names = ", ".join(folder.name for folder in folders)
        return "duplicate", folders[0], (
            f"{len(folders)} installed folders carry {pin['assembly']} ({names}) — Jellyfin "
            f"loads all of them and one auth plugin loaded twice fails every "
            f"authentication")

    assembly_path = folders[0] / pin["assembly"]
    digest = sha256_file(assembly_path)
    if digest != pin["assembly_sha256"]:
        version = version_of(assembly_path) or "version not recorded"
        return "drift", assembly_path, (
            f"{assembly_path} is {version} and hashes to {digest[:16]}…, but the pin is "
            f"{pin['repo']} {pin['tag']} (…{pin['assembly_sha256'][:16]}…)")
    return "ok", assembly_path, (
        f"{pin['assembly']} {version_of(assembly_path) or pin['version']} from "
        f"{pin['repo']} {pin['tag']}")


def state_of(pin: dict, plugins_dir: Path) -> str:
    """Just the status: 'ok' | 'absent' | 'drift' | 'duplicate' | 'cannot-run'."""
    return describe(pin, plugins_dir)[0]


# ── installing one ──────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "monarch-jellyfin-plugins"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise Refused(f"{url} answered HTTP {error.code}") from None
    except OSError as error:
        raise Refused(f"cannot reach {url} ({error})") from None


def install(pin: dict, plugins_dir: Path, source_url: str = "", log=print) -> None:
    """Fetch one pinned zip, verify it, and extract it."""
    url = source_url or pin.get("release_url") or (
        f"https://github.com/{pin['repo']}/releases/download/{pin['tag']}/{pin['asset']}")
    log(f"  fetching    {url}")
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
    log(f"  installed   {pin['assembly']} {pin['version']} into {target}")


def _chown(path: Path, log) -> None:
    """Hand a file to the uid Jellyfin runs as; best effort (and skippable in tests)."""
    if os.geteuid() != 0:
        return
    try:
        os.chown(path, PLUGIN_UID, PLUGIN_GID)
    except OSError as error:
        log(f"  note: could not chown {path.name} to {PLUGIN_UID}:{PLUGIN_GID} ({error})")


# ── the command line ────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="is every pinned plugin the installed build? (default)")
    parser.add_argument("--status", action="store_true",
                        help="print every pin and what is installed")
    parser.add_argument("--install", action="store_true",
                        help="fetch, verify and extract the pins that are missing or stale")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help=f"the plugin pins (default {DEFAULT_MANIFEST})")
    parser.add_argument("--plugin", default="",
                        help="limit to one pin by name (e.g. oidc, ldap)")
    parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR,
                        help=f"Jellyfin's plugin directory (default {DEFAULT_PLUGINS_DIR})")
    parser.add_argument("--source-url", default="",
                        help="fetch the zip from here instead of the release URL "
                             "(installs from a local mirror, and used by the tests)")
    args = parser.parse_args(argv)

    try:
        pins = load_pins(args.manifest)
        if args.plugin:
            pins = [pin_named(pins, args.plugin)]
    except CannotRun as error:
        print(f"jellyfin-plugin-pin: {error}", file=sys.stderr)
        return 2

    if args.source_url and len(pins) > 1:
        print("jellyfin-plugin-pin: --source-url installs one plugin; add --plugin", file=sys.stderr)
        return 2

    print(f"jellyfin-plugin-pin [{'install' if args.install else 'check'}] {args.plugins_dir}")

    failures = 0
    to_install: list[tuple[dict, str]] = []
    for pin in pins:
        status, assembly_path, detail = describe(pin, args.plugins_dir)
        if status == "cannot-run":
            print(f"jellyfin-plugin-pin: {detail}", file=sys.stderr)
            return 2
        if args.status:
            print(f"  {pin['name']:<10} {pin['repo']} {pin['tag']} "
                  f"({pin['asset']}, {pin.get('asset_bytes', '?')} bytes)")
            print(f"  {'':<10} installed   "
                  f"{assembly_path.parent if assembly_path else '(nothing)'}")
            print(f"  {'':<10} status      {status}: {detail}")
        if status == "ok":
            if not args.status:
                print(f"  ok          {detail}")
            continue
        if status == "duplicate" or not args.install:
            print(f"  FAIL {pin['name']:<6} {detail}", file=sys.stderr)
            failures += 1
            continue
        to_install.append((pin, detail))

    if args.status:
        return 1 if failures else 0

    for pin, detail in to_install:
        print(f"  {pin['name']:<10} {detail}")
        try:
            install(pin, args.plugins_dir, args.source_url)
        except Refused as error:
            print(f"jellyfin-plugin-pin: {error}", file=sys.stderr)
            print(f"nothing was written for {pin['name']}; the pin in "
                  f"init/jellyfin-plugins.json is the expected build", file=sys.stderr)
            failures += 1

    if failures:
        print(f"\nFAIL — {failures} pinned plugin(s) are not the installed build. Fix with "
              f"`python3 scripts/jellyfin-plugin-pin.py --install`, then restart Jellyfin; "
              f"see docs/operations.md.", file=sys.stderr)
        return 1

    for pin, _detail in to_install:
        status, _path, detail = describe(pin, args.plugins_dir)
        if status != "ok":
            print(f"jellyfin-plugin-pin: installed {pin['name']}, but it still reads as "
                  f"{status}: {detail}", file=sys.stderr)
            failures += 1
    if failures:
        return 1

    if to_install:
        print(f"\ninstalled {len(to_install)} plugin(s). Restart Jellyfin so they load:  "
              f"docker restart jellyfin")
        return 0
    print(f"\nok: every pinned plugin is the installed build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
