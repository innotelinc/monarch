#!/usr/bin/env python3
"""seerr-login-methods.py — Cerulean is the identity; Seerr's own password store is not.

WHY THIS EXISTS
---------------
Seerr (the request manager behind `req.innotel.us`) has two sign-in methods of its
own, and Seerr's settings page calls them exactly that:

  * `main.localLogin` — **"Enable Local Sign-In": allow users to sign in using
    their email address and password.** A credential store Seerr keeps to itself.
  * `main.mediaServerLogin` — **"Enable <media server> Sign-In": allow users to
    sign in using their Jellyfin account.** That account *is* the Cerulean
    identity: the LDAP outpost resolves it against Authentik, and disabling a user
    in Authentik blocks their media login.

Measured against the image this stack runs (`ghcr.io/seerr-team/seerr:latest`)
rather than assumed: Seerr has **no OpenID Connect support** — no `openid` string
in `server/`, `src/`, or its own OpenAPI document, and no OIDC environment
variables in the compiled server. So it cannot join Cerulean Authentik directly
the way Homarr does, and `server/routes/auth.ts` enforces the two methods above
with plain checks (`if (!settings.main.localLogin)`). The public name is what
Authentik fronts (`jellyseerr-sso`, port 14009), and this script is the other
half: with local sign-in off, the only credential that opens Seerr is the one
Authentik already manages.

`mediaServerLogin` is deliberately left alone. Turning it off too would leave a
gateway that authenticates nobody into Seerr: the proxy proves an Authentik
session for the *name*, but Seerr still needs its own session, and the Jellyfin
sign-in is how a user obtains one without a second password. What is removed is
the second password, not the sign-in.

USAGE
-----
    python3 scripts/seerr-login-methods.py --check     # report only (default)
    python3 scripts/seerr-login-methods.py --apply     # turn local sign-in off
    python3 scripts/seerr-login-methods.py --url http://192.168.1.56:5055

Runs on the media host, where Seerr's port is published on loopback
(`127.0.0.1:5055`) and its `settings.json` is bind-mounted at
`/docker/appdata/jellyseerr/`. The API key is read from that file (`main.apiKey`)
so no secret has to be copied onto the command line; `SEERR_API_KEY` overrides it.

`POST /api/v1/settings/main` merges what it is given (`merge(settings.main,
req.body)` in `server/routes/settings/index.ts`), so the request carries one field
and cannot clobber the rest of the settings object.

Exit codes: 0 = the posture is as intended, 1 = local sign-in is still enabled (or
the update failed), 2 = cannot run (no api key, no settings file).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SETTINGS = Path("/docker/appdata/jellyseerr/settings.json")
DEFAULT_URL = "http://127.0.0.1:5055"


class SeerrError(RuntimeError):
    pass


def api_key(settings_path: Path, override: str) -> str:
    """The API key Seerr wrote for itself, so nothing has to be pasted in."""
    if override:
        return override
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SeerrError(f"cannot read {settings_path} ({error})") from None
    except json.JSONDecodeError:
        raise SeerrError(f"{settings_path} is not JSON") from None
    key = str((payload.get("main") or {}).get("apiKey") or "").strip()
    if not key:
        raise SeerrError(f"{settings_path} has no main.apiKey — is Seerr initialized?")
    return key


def call(base_url: str, key: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("X-Api-Key", key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        hint = (
            " — the API key was rejected"
            if error.code in (401, 403)
            else ""
        )
        raise SeerrError(f"{method} {path} answered {error.code}{hint} {detail}".strip()) from None
    except OSError as error:
        raise SeerrError(f"cannot reach Seerr at {base_url} ({error})") from None


def report(settings: dict) -> None:
    local = settings.get("localLogin")
    media = settings.get("mediaServerLogin")
    print(f"  applicationUrl      {settings.get('applicationUrl') or '(unset)'}")
    print(f"  localLogin          {local}   (email + password, Seerr's own store)")
    print(f"  mediaServerLogin    {media}   (the Jellyfin account: the Cerulean identity)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="turn local sign-in off (default: report only)")
    parser.add_argument("--check", action="store_true",
                        help="report only, the default")
    parser.add_argument("--url", default=os.environ.get("SEERR_URL", DEFAULT_URL),
                        help=f"Seerr base URL (default {DEFAULT_URL})")
    parser.add_argument("--settings", type=Path,
                        default=Path(os.environ.get("SEERR_SETTINGS", str(DEFAULT_SETTINGS))),
                        help=f"Seerr settings.json, for the API key (default {DEFAULT_SETTINGS})")
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    try:
        key = api_key(args.settings, os.environ.get("SEERR_API_KEY", ""))
    except SeerrError as error:
        print(f"seerr-login-methods: {error}", file=sys.stderr)
        return 2

    print(f"seerr-login-methods [{'apply' if args.apply else 'check'}] {base_url}")
    try:
        settings = call(base_url, key, "GET", "/api/v1/settings/main")
    except SeerrError as error:
        print(f"seerr-login-methods: {error}", file=sys.stderr)
        return 2
    report(settings)

    if settings.get("localLogin") is False:
        print("\nok: only the Jellyfin (Cerulean) sign-in can open this app.")
        return 0

    if not args.apply:
        print("\nlocal sign-in is still enabled — re-run with --apply to turn it off.")
        return 1

    try:
        updated = call(base_url, key, "POST", "/api/v1/settings/main", {"localLogin": False})
    except SeerrError as error:
        print(f"seerr-login-methods: {error}", file=sys.stderr)
        return 1

    if updated.get("localLogin") is not False:
        print("seerr-login-methods: Seerr accepted the request and still reports "
              "localLogin enabled", file=sys.stderr)
        return 1

    print("  localLogin          False  (updated)")
    print("\nok: only the Jellyfin (Cerulean) sign-in can open this app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
