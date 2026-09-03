#!/usr/bin/env python3
"""
monarch-seed: pre-seed qBittorrent WebUI credentials before first boot.

qBittorrent >= 4.6.1 refuses to log in with the historical admin/adminadmin
default and instead prints a random temporary password to the container logs
on first start. There is no API to change the WebUI password, so the only
reliable way to give it a known login from the start is to write its
qBittorrent.conf *before* the container first starts.

This container is gated by `qbittorrent -> depends_on monarch-seed
(service_completed_successfully)` so the config always lands first.

The stack uses the official `ghcr.io/qbittorrent/docker-qbittorrent-nox`
image, which launches qBittorrent with `--profile=/config`. qBittorrent 5.x
resolves that profile to the nested config path:

    /config/qBittorrent/config/qBittorrent.conf

(not the flat `/config/qBittorrent/qBittorrent.conf` that the linuxserver
image used). The entrypoint creates that file on first boot, so seeding it
beforehand is what makes the known login stick.

The WebUI password is stored as a PBKDF2-HMAC-SHA512 hash (16-byte salt,
64-byte digest, 100 000 iterations), formatted as:

    WebUI\\Password_PBKDF2="@ByteArray(<base64 salt>:<base64 digest>)"

Idempotent: if the config already contains a non-empty password hash
(first boot already happened, or the admin changed it), we leave it alone.

If login still fails later (e.g. qBittorrent changes its hash parameters),
fall back to: `docker logs qbittorrent` -> grab the temporary password ->
change it in the WebUI. See the README "Troubleshooting" section.
"""

import base64
import hashlib
import os
import re
import sys

USER = os.environ.get("MONARCH_USERNAME", "admin")
PASS = os.environ.get("MONARCH_PASSWORD", "monarch8")

# qBittorrent launched with --profile=/config resolves its 5.x config here.
# (The linuxserver image used the flat CONFIG_DIR/qBittorrent.conf instead.)
CONFIG_DIR = "/config/qBittorrent/config"
CONFIG_FILE = os.path.join(CONFIG_DIR, "qBittorrent.conf")

PBKDF2_ITERATIONS = 100_000
PBKDF2_DK_LEN = 64
SALT_LEN = 16


def pbkdf2_hash(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=PBKDF2_DK_LEN
    )
    return "@ByteArray({}:{})".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def main() -> int:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as exc:
        print(f"[monarch-seed] ERROR: cannot create {CONFIG_DIR}: {exc}")
        return 1

    existing = ""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            existing = normalize_line_endings(fh.read())

    # Never clobber an existing password (first boot happened already).
    if re.search(r"WebUI\\Password_PBKDF2\s*=\s*\"?[^\s\"]", existing):
        print("[monarch-seed] qBittorrent.conf already has a WebUI password - nothing to do.")
        return 0

    salt = os.urandom(SALT_LEN)
    password_line = f'WebUI\\Password_PBKDF2="{pbkdf2_hash(PASS, salt)}"'
    username_line = f"WebUI\\Username={USER}"

    if existing:
        # Merge into the file: keep the rest of the user's preferences and make
        # sure the WebUI keys land inside the [Preferences] section, not at the
        # end of the file (QSettings would ignore keys in the wrong section).
        lines = existing.splitlines()

        # Replace any existing WebUI keys wherever they sit.
        replaced_user = replaced_pass = False
        for i, line in enumerate(lines):
            if re.match(r"WebUI\\Password_PBKDF2\s*=", line):
                lines[i] = password_line
                replaced_pass = True
            elif re.match(r"WebUI\\Username\s*=", line):
                lines[i] = username_line
                replaced_user = True

        prefs_at = next(
            (i for i, line in enumerate(lines) if line.startswith("[Preferences]")), None
        )
        if prefs_at is None:
            # No [Preferences] section yet: open one at the very top.
            lines = ["[Preferences]"] + lines
            prefs_at = 0

        if not (replaced_user and replaced_pass):
            # Slot the missing keys in right after the [Preferences] header.
            additions = []
            if not replaced_user:
                additions.append(username_line)
            if not replaced_pass:
                additions.append(password_line)
            lines = lines[: prefs_at + 1] + additions + lines[prefs_at + 1:]
        body = "\n".join(lines) + "\n"
    else:
        body = (
            "[Preferences]\n"
            "WebUI\\Address=*\n"
            f"{username_line}\n"
            f"{password_line}\n"
        )

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print(f"[monarch-seed] ERROR: cannot write {CONFIG_FILE}: {exc}")
        return 1

    print(f"[monarch-seed] Seeded qBittorrent WebUI credentials for user '{USER}' into {CONFIG_FILE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())