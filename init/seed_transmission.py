#!/usr/bin/env python3
"""
monarch-seed: pin Transmission's session settings before first boot.

Transmission (unlike qBittorrent) keeps everything in one plain JSON file,
`/config/settings.json`. The linuxserver image rewrites only a handful of keys
on every start — authentication from USER/PASS, the two RPC whitelists from
WHITELIST/HOST_WHITELIST, and peer-port from PEERPORT — leaving every other key
alone. This container writes the rest of the posture before the daemon first
reads the file, so the very first boot is already correct:

  * `download-dir` = /data/torrents. The per-app folders qBittorrent kept as
    categories are Servarr's category subfolders under this directory, so the
    layout `movies`/`tv`/`music`/`xxx` is unchanged (/data/torrents/<type>) and
    sits outside every Jellyfin library root.
  * the local login is OFF (`rpc-authentication-required` false). The WebUI is
    published on loopback only and `transmission-sso` is the sole route to it,
    so Cerulean (Authentik) is the only door — the same posture as Bazarr and
    Seerr. (With USER/PASS unset the linuxserver init forces this too; writing
    it here keeps the intent explicit and the first boot deterministic.)
  * both RPC whitelists are OFF, so the *arr apps may reach the daemon by
    container name (`transmission:9091`) without an IP/host allowlist entry.

Idempotent: the keys are enforced on every run, so a settings.json that was
hand-edited away from this posture is put back. Unknown keys are preserved.

`monarch-init` (`configure_transmission`) re-asserts `download-dir` live through
the RPC API, and `scripts/drift-check.sh` asserts it again on the host.
"""

import json
import os
import sys

CONFIG_DIR = "/config"
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

# Enforced on every run. Keys not listed here are left as the operator had them.
SETTINGS = {
    "download-dir": "/data/torrents",
    "incomplete-dir-enabled": False,
    "watch-dir-enabled": False,
    "rpc-authentication-required": False,
    "rpc-username": "",
    "rpc-password": "",
    "rpc-whitelist-enabled": False,
    "rpc-host-whitelist-enabled": False,
    "rpc-bind-address": "0.0.0.0",
    "peer-port": 51413,
    "peer-port-random-on-start": False,
}


def main() -> int:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as exc:
        print(f"[monarch-seed] ERROR: cannot create {CONFIG_DIR}: {exc}")
        return 1

    existing: dict = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as fh:
                existing = json.load(fh)
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError) as exc:
            print(f"[monarch-seed] WARNING: {CONFIG_FILE} is unreadable ({exc}); "
                  f"rewriting it from scratch.")
            existing = {}

    changed = {key: value for key, value in SETTINGS.items()
               if existing.get(key) != value}
    merged = {**existing, **SETTINGS}

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=4, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        print(f"[monarch-seed] ERROR: cannot write {CONFIG_FILE}: {exc}")
        return 1

    if changed:
        print(f"[monarch-seed] Pinned Transmission settings in {CONFIG_FILE}: "
              f"{', '.join(sorted(changed))}.")
    else:
        print(f"[monarch-seed] Transmission settings already pin "
              f"{', '.join(sorted(SETTINGS))} - nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
