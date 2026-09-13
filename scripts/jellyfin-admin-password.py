#!/usr/bin/env python3
"""
jellyfin-admin-password.py - keep the last local Monarch credential in step with
.env, and keep the admin API key that Monarch's services use alive.

Why this exists
---------------
Monarch delegates identity to Authentik, so the only local credential left is the
Jellyfin `admin` account that `monarch-init` creates from MONARCH_PASSWORD. Once
Jellyfin's first-run wizard has set a password, Jellyfin will not change it
without the CURRENT password - and the recovery PIN is not returned by the API -
so an operator changing it by hand silently desynchronises the shared credential.
`drift-check` could only ever report that drift until this script existed.

The supported repair path is Jellyfin's own forgot-password flow. It has two
traps, both of which made an earlier attempt look like it "succeeded but did not
take":

  1. POST /Users/ForgotPassword  {"EnteredUsername": "<user>"}
       -> {"Action": "PinCode", "PinFile": "/config/data/passwordreset<id>.json"}
     The PIN is WRITTEN TO THAT FILE INSIDE THE CONTAINER. It is not in the
     response, so the call looks like it did nothing.
  2. POST /Users/ForgotPassword/Pin  {"Pin": "<pin>"}
       -> {"Success": true, "UsersReset": ["<user>"]}
     Redeeming the PIN makes THE PIN ITSELF the account's password. Log in with
     the PIN afterwards - not with an empty password, and not with the old one.

The new password is then set the normal way, authenticated with that PIN:

  POST /Users/{id}/Password   {"CurrentPw": "<pin>", "NewPw": MONARCH_PASSWORD}

Header gotcha: this build (the pinned v12 image) reads the MediaBrowser header
from `Authorization` only. `X-Emby-Authorization` is rejected with HTTP 400 and
`X-Emby-Token` / `?api_key=` are rejected with 401 even when the credentials are
right, so an authenticated call must send:

  Authorization: MediaBrowser Token="<token>", Client=..., Device=..., DeviceId=...

Token caveat: changing the password revokes every session token the admin holds
(this is what breaks the exported admin token in /docker/appdata/init), so
--set also refreshes the credential Monarch's services use. It mints a DURABLE
API key (Jellyfin's ApiKeys table, like the Seerr/Homarr entries) instead of a
session token, because an API key survives the next password change. The key is
written to both places it is read from: the Jellyfin key file and
JELLYFIN_API_KEY in .env (ai-recs, health-analytics, magnate-entitlements).

Modes:

  --check       (default) verify the whole credential chain, change nothing.
  --set         align the password when it has drifted, then refresh the API key.
  --force       with --set, use the forgot-password flow even when the login works.
  --check-apps  verify the keys the APPS hold (Jellyseerr's plaintext copy and
                Homarr's encrypted one) still authenticate. A half-finished
                rotation leaves an app configured with a token Jellyfin has
                forgotten, which nothing else notices: the container is up and
                its own UI answers. Read-only; drift-check runs this.

Usage:

  python3 scripts/jellyfin-admin-password.py --check
  python3 scripts/jellyfin-admin-password.py --check-apps
  python3 scripts/jellyfin-admin-password.py --set

Exit: 0 = aligned/verified · 1 = drift or failure · 2 = not configured
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

DEFAULT_JELLYFIN_URL = "http://localhost:8097"
DEFAULT_KEY_NAME = "monarch-admin"
# Where the apps keep their own copy of a Jellyfin key (--check-apps).
DEFAULT_SEERR_SETTINGS = "/docker/appdata/jellyseerr/settings.json"
DEFAULT_HOMARR_DB = "/docker/appdata/homarr/appdata/db/db.sqlite"
DEFAULT_HOMARR_CONTAINER = "homarr"
# Where monarch-init exports the admin token Monarch's services read.
KEY_FILE = Path("/docker/appdata/init/jellyfin-api-key.txt")
# How the Jellyfin config volume is reached on the host, for reading the PIN file
# when `docker exec` is not the way in (JELLYFIN_CONFIG_HOST overrides).
DEFAULT_CONFIG_HOST = "/docker/appdata/jellyfin"

CLIENT = ('Client="jellyfin-admin-password", Device="Linux", '
          'DeviceId="monarch-admin-password", Version="1.0"')


# ── env ─────────────────────────────────────────────────────────────────────
def load_env(path):
    """Parse KEY=VALUE lines into os.environ (never overwrite)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def env(name, default=""):
    return os.environ.get(name, default).strip()


def save_env_key(path, key, value):
    """Set KEY=value in an env file in place, preserving everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ── Jellyfin ────────────────────────────────────────────────────────────────
class Jellyfin:
    """The Jellyfin admin API, using only the header spelling this build takes."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    def call(self, path, method="GET", body=None, token="", timeout=25):
        """Returns (status, parsed-body-or-None); status 0 means transport error."""
        auth = f'MediaBrowser Token="{token}", {CLIENT}' if token \
            else f"MediaBrowser {CLIENT}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, (json.loads(raw) if raw.strip() else None)
            except ValueError:
                return exc.code, None
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": str(exc)}

    def login(self, user, password):
        """(ok, token, user-id) for the given credentials."""
        status, body = self.call("/Users/AuthenticateByName", method="POST",
                                 body={"Username": user, "Pw": password})
        if status == 200 and isinstance(body, dict) and body.get("AccessToken"):
            return True, body["AccessToken"], (body.get("User") or {}).get("Id", "")
        return False, "", ""

    def verify(self, token):
        """Status of an authenticated admin call, i.e. is this token usable."""
        status, _ = self.call("/Users", token=token)
        return status

    def api_keys(self, token):
        status, body = self.call("/Auth/Keys", token=token)
        if status == 200 and isinstance(body, dict):
            return body.get("Items") or []
        return []

    def ensure_api_key(self, token, name, log=print):
        """Return a durable API key called <name>, creating it when absent.

        API keys live in Jellyfin's own table and are NOT revoked by a password
        change, unlike the session token AuthenticateByName hands out.
        """
        for key in self.api_keys(token):
            if (key.get("AppName") or "") == name and key.get("AccessToken"):
                log(f"  API key {name!r} already exists - reusing it")
                return key["AccessToken"]
        status, _ = self.call(f"/Auth/Keys?app={urllib.parse.quote(name)}",
                              method="POST", token=token)
        if status not in (200, 204):
            log(f"  ERROR: could not create the API key (HTTP {status})")
            return ""
        # The list endpoint lags a moment behind the create on this build.
        for _ in range(5):
            for key in self.api_keys(token):
                if (key.get("AppName") or "") == name and key.get("AccessToken"):
                    log(f"  API key {name!r} created")
                    return key["AccessToken"]
            time.sleep(1)
        log(f"  ERROR: created the API key but the list endpoint never showed it")
        return ""


def read_pin(pin_file, container, log=print):
    """Read the forgot-password PIN from <pin_file> (a path inside the container).

    The API only returns the path, so the file has to be read where Jellyfin
    wrote it: via `docker exec`, or through the host-side config mount when the
    container is not reachable from here.
    """
    if container:
        proc = subprocess.run(["docker", "exec", container, "cat", pin_file],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return (json.loads(proc.stdout).get("Pin") or "").strip()
            except ValueError:
                return proc.stdout.strip()
    config_host = env("JELLYFIN_CONFIG_HOST", DEFAULT_CONFIG_HOST)
    if config_host and pin_file.startswith("/config/"):
        host_path = Path(config_host) / pin_file[len("/config/"):]
        if host_path.is_file():
            log(f"  (read the PIN from {host_path} - the container was not reachable)")
            return (json.loads(host_path.read_text(encoding="utf-8")).get("Pin") or "").strip()
    return ""


# Homarr encrypts integration secrets at rest; this is its scheme, and it is
# reproduced rather than reimplemented from the docs: AES-256-CBC, key = the 64
# hex chars of SECRET_ENCRYPTION_KEY, a 16-byte IV, stored hex(ct).hex(iv).
# Verified against a stored value whose plaintext was known. Running it in the
# container keeps the key in the environment that owns it.
HOMARR_DECRYPT = (
    'const c=require("crypto"),k=Buffer.from(process.env.SECRET_ENCRYPTION_KEY,"hex"),'
    '[h,i]=process.env.CT.split("."),d=c.createDecipheriv("aes-256-cbc",k,Buffer.from(i,"hex"));'
    'process.stdout.write(Buffer.concat([d.update(Buffer.from(h,"hex")),d.final()]).toString("utf8"))')


def decrypt_homarr_secret(ciphertext, container):
    """Homarr's plaintext for a stored secret, or "" when it cannot be read."""
    if not container or not ciphertext:
        return ""
    proc = subprocess.run(
        ["docker", "exec", "-e", f"CT={ciphertext}", container, "node", "-e",
         HOMARR_DECRYPT], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def check_app_keys(jf, log=print):
    """Verify the Jellyfin keys the apps hold. Returns the number of failures.

    A key that cannot be READ - the file is absent, or Homarr's secret cannot be
    decrypted because its container is not reachable - is reported as unverified
    rather than failed: what this exists to catch is a key Jellyfin REJECTS,
    which is what a half-finished rotation leaves behind.
    """
    failures = 0

    def validate(token, who):
        nonlocal failures
        status = jf.verify(token)
        if status == 200:
            log(f"  ok: {who} holds a Jellyfin API key that still authenticates")
            return
        failures += 1
        log(f"  DRIFT: {who} holds a Jellyfin API key Jellyfin rejects "
            f"(HTTP {status}), so it cannot read Jellyfin. Re-run the rotation "
            "in docs/operations.md.")

    # Jellyseerr keeps its copy in plaintext.
    settings = Path(env("JELLYSEERR_SETTINGS_FILE", DEFAULT_SEERR_SETTINGS))
    if not settings.is_file():
        log(f"  note: {settings} not found - jellyseerr's key not checked")
    else:
        token = ""
        try:
            token = (json.loads(settings.read_text(encoding="utf-8"))
                     .get("jellyfin", {}).get("apiKey") or "").strip()
        except (OSError, ValueError) as exc:
            log(f"  note: {settings} could not be parsed ({exc}) - jellyseerr's "
                "key not checked")
        if token:
            validate(token, "jellyseerr")
        elif settings.is_file():
            failures += 1
            log(f"  DRIFT: jellyseerr stores no Jellyfin API key "
                f"(jellyfin.apiKey is empty in {settings})")

    # Homarr keeps its copy encrypted, so it has to be decrypted first.
    db = Path(env("HOMARR_DB_FILE", DEFAULT_HOMARR_DB))
    if not db.is_file():
        log(f"  note: {db} not found - homarr's key not checked")
        return failures
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            'SELECT s.value FROM "integrationSecret" s '
            'JOIN "integration" i ON i.id = s.integration_id '
            "WHERE i.kind = 'jellyfin' AND s.kind = 'apiKey'").fetchone()
        con.close()
    except sqlite3.Error as exc:
        log(f"  note: homarr's database could not be read ({exc}) - its key not checked")
        return failures
    if not row or not row[0]:
        log("  note: homarr has no Jellyfin integration secret - not checked")
        return failures
    token = decrypt_homarr_secret(row[0], env("HOMARR_CONTAINER", DEFAULT_HOMARR_CONTAINER))
    if not token:
        log("  note: homarr's stored key could not be decrypted (its container or "
            "crypto scheme is unavailable) - not checked")
        return failures
    validate(token, "homarr")
    return failures


def align_password(jf, user, password, container, log=print):
    """Run the forgot-password flow and set <password>. Returns True on success."""
    log("  Logging in with the shared credentials failed - using Jellyfin's "
        "forgot-password flow (it needs no current password).")
    status, body = jf.call("/Users/ForgotPassword", method="POST",
                           body={"EnteredUsername": user})
    if status != 200 or not isinstance(body, dict):
        log(f"  ERROR: POST /Users/ForgotPassword -> HTTP {status} {body}")
        return False
    pin_file = body.get("PinFile") or ""
    if not pin_file:
        log(f"  ERROR: no PinFile in the response ({body.get('Action')!r}) - "
            "password reset needs a configured reset provider on this server.")
        return False
    pin = read_pin(pin_file, container, log=log)
    if not pin:
        log(f"  ERROR: the PIN was not readable from {pin_file}. Jellyfin writes "
            "it inside the container; pass --container, or set JELLYFIN_CONFIG_HOST "
            "to the host directory mounted at /config.")
        return False
    status, body = jf.call("/Users/ForgotPassword/Pin", method="POST",
                           body={"Pin": pin})
    if status != 200 or not (isinstance(body, dict) and body.get("Success")):
        log(f"  ERROR: redeeming the PIN -> HTTP {status} {body}")
        return False
    log(f"  PIN redeemed ({', '.join(body.get('UsersReset') or [])}); the account's "
        "password is now the PIN, so the new one can be set.")
    # The PIN is the current password at this point - that is the second trap of
    # this flow: it is neither empty nor the old password.
    ok, token, uid = jf.login(user, pin)
    if not ok:
        log("  ERROR: could not log in with the redeemed PIN (the reset did not bind)")
        return False
    status, body = jf.call(f"/Users/{uid}/Password", method="POST",
                           body={"CurrentPw": pin, "NewPw": password}, token=token)
    if status not in (200, 204):
        log(f"  ERROR: setting the new password -> HTTP {status} {body}")
        return False
    log("  Password set from MONARCH_PASSWORD.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Align Jellyfin's local admin password with MONARCH_PASSWORD "
                    "and refresh the durable admin API key")
    parser.add_argument("--check", action="store_true",
                        help="verify the credential chain only (default)")
    parser.add_argument("--check-apps", action="store_true",
                        help="verify the Jellyfin API keys held by Jellyseerr and "
                             "Homarr (read-only)")
    parser.add_argument("--set", action="store_true",
                        help="align the password when it has drifted, and refresh "
                             "the admin API key")
    parser.add_argument("--force", action="store_true",
                        help="with --set: run the forgot-password flow even when "
                             "the shared credentials already work")
    parser.add_argument("--container", default=env("JELLYFIN_CONTAINER", "jellyfin"),
                        help="Jellyfin container name, for reading the PIN file "
                             "(default: jellyfin)")
    args = parser.parse_args()

    load_env(ENV_FILE)
    base = env("JELLYFIN_URL", DEFAULT_JELLYFIN_URL)
    user = env("JELLYFIN_ADMIN_USER", "admin")
    password = env("MONARCH_PASSWORD")
    key_name = env("JELLYFIN_API_KEY_NAME", DEFAULT_KEY_NAME)

    jf = Jellyfin(base)

    # The app-held keys need no credential of their own: they are validated by
    # calling Jellyfin with them.
    if args.check_apps:
        print(f"Jellyfin API keys held by the apps ({base}):")
        return 1 if check_app_keys(jf) else 0

    if not password:
        print("NOT CONFIGURED: MONARCH_PASSWORD is not set (see .env.sample).")
        return 2

    print(f"Jellyfin admin credential: {user}@{base}")

    ok, token, _ = jf.login(user, password)
    aligned = ok

    if args.set:
        if ok and not args.force:
            print(f"  Password already matches MONARCH_PASSWORD - nothing to change.")
        elif not align_password(jf, user, password, args.container):
            return 1
        else:
            ok, token, _ = jf.login(user, password)
            if not ok:
                print("  ERROR: the login still fails after setting the password.")
                return 1
            aligned = True
    else:
        if not ok:
            print(f"  DRIFT: the admin password does not match MONARCH_PASSWORD.")
            print("  Fix: python3 scripts/jellyfin-admin-password.py --set")
        else:
            print("  ok: the admin password matches MONARCH_PASSWORD")

    # The admin API key Monarch's services use. A password change revokes session
    # tokens, so this is a durable API key, and it is kept in both places it is
    # read from (the init key file and JELLYFIN_API_KEY in .env).
    file_token = KEY_FILE.read_text(encoding="utf-8").strip() if KEY_FILE.is_file() else ""
    env_token = env("JELLYFIN_API_KEY")
    key_ok = bool(token) and jf.verify(token) == 200

    if args.set and aligned:
        key = jf.ensure_api_key(token, key_name) if key_ok else ""
        if not key:
            print("  ERROR: no usable admin API key - cannot refresh the exported "
                  "token.")
            return 1
        if file_token != key:
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            if KEY_FILE.is_file():
                shutil.copy2(KEY_FILE, str(KEY_FILE) + ".bak")
            KEY_FILE.write_text(key + "\n", encoding="utf-8")
            KEY_FILE.chmod(0o600)
            print(f"  wrote the {key_name!r} API key -> {KEY_FILE}")
        if env_token != key:
            if ENV_FILE.is_file():
                shutil.copy2(ENV_FILE, str(ENV_FILE) + ".bak")
            save_env_key(ENV_FILE, "JELLYFIN_API_KEY", key)
            print("  updated JELLYFIN_API_KEY in .env (backup: .env.bak)")
        file_token = env_token = key

    # Report the key chain without ever printing a token.
    if not file_token and not env_token:
        print(f"  WARNING: no admin API key: neither {KEY_FILE} nor "
              "JELLYFIN_API_KEY in .env is set (run with --set to mint one)")
    for label, value in (("key file", file_token), (".env JELLYFIN_API_KEY", env_token)):
        if not value:
            continue
        status = jf.verify(value)
        if status == 200:
            print(f"  ok: {label} authorises admin API calls")
        else:
            print(f"  DRIFT: {label} answers HTTP {status} (stale or wrong)")

    if not aligned:
        return 1
    if (file_token and jf.verify(file_token) != 200) or \
            (env_token and jf.verify(env_token) != 200):
        return 1
    print("OK: the Jellyfin admin credential chain matches .env")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
