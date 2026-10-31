#!/usr/bin/env python3
"""check-jellyfin-names.py — does every published Jellyfin name serve Jellyfin as itself?

WHY THIS EXISTS
---------------
A Jellyfin client is not handed a page and left alone: it asks the server who it
is, and then dials what the server says. `EnablePublishedServerUriByRequest`
makes Jellyfin hand back the name the client arrived on, which is what lets one
app answer as several names at once. On 2026-10-30 the Android TV app was told
`https://media.innotel.us` while it had dialled another name, followed the
advertisement and timed out — the app was healthy, the certificate was healthy,
and the *answer* was wrong. Nothing in the estate could see that: the page still
loaded, and the name still answered 200.

So each name is asked two questions here:

  * is the certificate this name's own? (urllib verifies the chain and the
    hostname, so an expired or wrong-name certificate fails here, which is the
    other half of the same outage), and
  * does `/System/Info/Public` report `LocalAddress` as that same name?

A name whose `LocalAddress` is a different host is a finding. So is a name that
does not answer at all.

USAGE
-----
    python3 scripts/check-jellyfin-names.py
    JELLYFIN_NAMES=media.innotel.us python3 scripts/check-jellyfin-names.py
    python3 scripts/check-jellyfin-names.py --timeout 20

Exit codes, as everywhere in this repo: 0 = every name serves the app as itself,
1 = at least one does not (each named), 2 = cannot look (nothing could be
reached at all, so this is not a verdict about the names).
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# Both names publish the same app: `media` is the address the web client and the
# board use, `iptv` is the one the TV app is configured with (it has no
# oauth2-proxy gate, so a device can authenticate against it directly).
DEFAULT_NAMES = ("media.innotel.us", "iptv.innotel.us")
INFO_PATH = "/System/Info/Public"
USER_AGENT = "monarch-jellyfin-names/1"


def judge(name: str, status: int, payload: bytes | str) -> tuple[bool, str]:
    """(serves as itself?, what to say). Separated from the request so a test can
    hand it a status and a body without a server."""
    if status != 200:
        return False, f"HTTP {status} from {INFO_PATH}"
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        return False, f"{INFO_PATH} did not answer JSON"
    if not body.get("ServerName"):
        return False, f"{INFO_PATH} is not Jellyfin's public info (no ServerName)"
    published = str(body.get("LocalAddress") or "").strip()
    version = f" {body.get('Version')}" if body.get("Version") else ""
    if not published:
        # Nothing advertised: the client keeps the address it dialled, which is
        # exactly the behaviour that works. Not a finding.
        return True, f"serves as {name} ({body['ServerName']}{version}, nothing advertised)"
    host = urllib.parse.urlsplit(published).hostname or published
    if host != name:
        return False, (f"it advertises {published} — a client that dialled {name} "
                       f"is handed {host} and follows it")
    return True, f"serves as {name} ({body['ServerName']}{version})"


def check(name: str, base: str, timeout: float) -> tuple[bool, str]:
    """Ask one name. `base` is the origin to dial (`https://<name>` in the wild,
    a loopback origin in a test), and `name` is the name it must answer as."""
    url = base.rstrip("/") + INFO_PATH
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    context = ssl.create_default_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return judge(name, response.status, response.read())
    except urllib.error.HTTPError as error:
        # An HTTPError is a response, and it holds the socket until closed.
        try:
            payload = error.read()
        finally:
            try:
                error.close()
            except Exception:  # noqa: BLE001 - a leaked socket must not fail the check
                pass
        return judge(name, error.code, payload)
    except (urllib.error.URLError, ssl.SSLError, OSError) as error:
        reason = getattr(error, "reason", error)
        return False, f"cannot connect ({reason})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=12.0,
                        help="seconds per name (default 12)")
    parser.add_argument("--names", nargs="*", default=None,
                        help="names to check (default: the published pair)")
    args = parser.parse_args(argv)

    names = args.names or [n for n in os.environ.get("JELLYFIN_NAMES", "").split(",") if n] \
        or list(DEFAULT_NAMES)
    findings: list[str] = []
    unreachable = 0
    for name in names:
        ok, detail = check(name, f"https://{name}", args.timeout)
        if ok:
            print(f"  ok    {name}: {detail}")
            continue
        findings.append(f"{name}: {detail}")
        if detail.startswith("cannot connect"):
            unreachable += 1

    if findings:
        for line in findings:
            print(f"  FAIL  {line}")
        if unreachable == len(findings):
            # No route, no DNS, no egress: this machine cannot judge the names,
            # and saying "drift" here would be a check that lies whenever the
            # network is down.
            print("check-jellyfin-names: no published name could be reached from here, "
                  "so this is not a verdict", file=sys.stderr)
            return 2
        print(f"\nFAIL — {len(findings)} of {len(names)} published name(s) do not serve "
              "Jellyfin as themselves")
        return 1
    print(f"  ok    all {len(names)} published name(s) serve Jellyfin as themselves, "
          "over their own certificates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
