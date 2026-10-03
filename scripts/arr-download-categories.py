#!/usr/bin/env python3
"""Make every download land in the downloads tree, not in a library.

WHY THIS EXISTS. Two halves have to agree for a download to be filed correctly,
and each one fails invisibly on its own:

  * each *arr tells its Transmission client a CATEGORY NAME. Servarr sends that
    name to Transmission, which files the torrent in a subfolder of the download
    dir - so the name is what decides the folder the download lands in;
  * Transmission's download dir has to sit OUTSIDE every library root. A
    download dir that points at /data/media/<type> is exactly what makes each
    app warn "Download client Transmission places downloads in the root folder
    /data/media/<type>", and it drops an unfinished album into the music
    library for Jellyfin to scan.

The repo's `init/init.py` reconciles both, but only while it runs: a deployment
configured by hand before that fix keeps the hand-set category forever, because
the arr only reports the mismatch as a *warning* on its own Health page. This is
the side that can be run on a live host, that restarts nothing, and that can be
checked on a timer.

WHAT IT ASKS AND WHAT IT DOES. Both halves come from the invariants manifest
(`/docker/appdata/init/invariants.json`, written by monarch-init) so there is one
source of truth and no second copy of the map here: `transmission.category_paths`
is the name -> folder map, `arr_apps[].category` is the name each app must send,
and `transmission.download_dir` is where those folders live. Applying corrects
an arr's download client in place; Transmission itself has no category objects
to create (the folders appear on demand under the download dir), so there is
nothing to prune.

Usage:
    scripts/arr-download-categories.py --check     # exit 2 if anything drifted
    scripts/arr-download-categories.py             # correct it (restarts nothing)
    scripts/arr-download-categories.py --dry-run   # report, change nothing

Credentials for the arr APIs come from their config.xml API keys; Transmission
is reached unauthenticated on the compose network (the daemon keeps no local
login).

Exit codes: 0 everything agrees (or, applying, corrected); 1 Transmission, an
*arr or the manifest could not be read; 2 --check found drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path(os.environ.get("MONARCH_INVARIANTS",
                                       "/docker/appdata/init/invariants.json"))
DEFAULT_APPDATA = Path("/docker/appdata")
DEFAULT_TRANSMISSION = "http://localhost:9091"
CATEGORY_FIELDS = ("tvCategory", "movieCategory", "musicCategory", "category")


class Drift(RuntimeError):
    pass


def call(url: str, method: str = "GET", body: dict | None = None, raw_form: bool = False,
         headers: dict | None = None, timeout: float = 20.0):
    """One request; returns (status, parsed-or-text)."""

    def send():
        payload = None
        if body is not None:
            payload = urllib.parse.urlencode(body).encode() if raw_form else json.dumps(body).encode()
        request = urllib.request.Request(url, data=payload, method=method)
        if payload and not raw_form:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with urllib.request.build_opener().open(request, timeout=timeout) as response:
            raw = response.read()
        try:
            return response.status, json.loads(raw or b"null")
        except ValueError:
            return response.status, raw[:400].decode("utf-8", "replace")

    try:
        return send()
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"null")
        except ValueError:
            return error.code, raw[:400].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        raise Drift(str(error)) from error


def category_field(resource: dict) -> dict | None:
    """The field holding this app's download-client category, or None."""
    fields = {f.get("name"): f for f in resource.get("fields") or []}
    for name in CATEGORY_FIELDS:
        if name in fields:
            return fields[name]
    return None


def api_key(appdata: Path, svc: str) -> str:
    config = appdata / svc / "config.xml"
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                      config.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise Drift(f"no <ApiKey> in {config}")
    return match.group(1)


def arr_targets(manifest: dict) -> list[tuple[str, int, str, str]]:
    """(svc, port, api, category) for every *arr the manifest names."""
    out = []
    for app in manifest.get("arr_apps") or []:
        out.append((app["svc"], int(app["port"]), app["api"], app["category"]))
    return out


def transmission_rpc(base: str, method: str, arguments: dict | None = None) -> dict:
    """One Transmission RPC call, doing the session-id handshake when asked.

    Transmission answers the first request with HTTP 409 carrying the
    `X-Transmission-Session-Id` to repeat it with. The daemon keeps no local
    login, so no credential travels.
    """
    body = json.dumps({"method": method, "arguments": arguments or {}}).encode("utf-8")
    opener = urllib.request.build_opener()
    session_id = ""
    for attempt in (1, 2):
        request = urllib.request.Request(
            f"{base.rstrip('/')}/transmission/rpc", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        if session_id:
            request.add_header("X-Transmission-Session-Id", session_id)
        try:
            with opener.open(request, timeout=20) as response:
                parsed = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 409 and attempt == 1:
                session_id = error.headers.get("X-Transmission-Session-Id", "")
                continue
            raise Drift(f"Transmission RPC {method} failed (HTTP {error.code})") from error
        except (urllib.error.URLError, OSError) as error:
            raise Drift(f"Transmission RPC {method} failed: {error}") from error
        if parsed.get("result") not in (None, "success"):
            raise Drift(f"Transmission RPC {method}: {parsed.get('result')}")
        return parsed.get("arguments") or {}
    raise Drift(f"Transmission RPC {method}: session handshake failed")


def check_arrs(manifest: dict, appdata: Path, apply_fix: bool,
               dry_run: bool) -> tuple[list[str], bool]:
    """Does each app's Transmission client carry the manifest's category?

    Returns the findings and whether every app could be read at all.
    """
    findings: list[str] = []
    reachable = True
    for svc, port, api, want in arr_targets(manifest):
        base = f"http://localhost:{port}/api/{api}"
        try:
            key = api_key(appdata, svc)
        except (Drift, OSError) as error:
            findings.append(f"{svc}: {error}")
            reachable = False
            continue
        try:
            status, clients = call(f"{base}/downloadclient", headers={"X-Api-Key": key})
        except Drift as error:
            findings.append(f"{svc}: {error}")
            reachable = False
            continue
        if status != 200 or not isinstance(clients, list):
            findings.append(f"{svc}: downloadclient unreachable (HTTP {status})")
            reachable = False
            continue
        client = next((c for c in clients
                       if isinstance(c, dict) and c.get("implementation") == "Transmission"), None)
        if client is None:
            findings.append(f"{svc}: no Transmission download client")
            continue
        field = category_field(client)
        if field is None:
            findings.append(f"{svc}: this build's Transmission client has no category field")
            continue
        if field.get("value") == want:
            print(f"{svc:<9} files under {want!r}")
            continue
        if not apply_fix or dry_run:
            findings.append(f"{svc}: sends category {field.get('value')!r}, expected {want!r}")
            continue
        previous = field.get("value")
        field["value"] = want
        # Servarr has no per-field endpoint: the whole resource travels back.
        status, _ = call(f"{base}/downloadclient/{client['id']}", method="PUT", body=client,
                         headers={"X-Api-Key": key})
        if status in (200, 202):
            print(f"{svc:<9} category {previous!r} -> {want!r}")
        else:
            findings.append(f"{svc}: could not set the category (HTTP {status})")
    return findings, reachable


def check_transmission(manifest: dict, base: str) -> tuple[list[str], bool]:
    """Is Transmission's download dir the manifest's, outside every library?"""
    section = manifest.get("transmission") or {}
    want = section.get("download_dir")
    if not want:
        return ["manifest carries no transmission.download_dir"], False
    try:
        session = transmission_rpc(base, "session-get")
    except Drift as error:
        return [f"transmission: {error}"], False

    findings: list[str] = []
    current = session.get("download-dir")
    if current == want:
        print(f"transmission files downloads under {want}")
    else:
        findings.append(f"transmission: download dir is {current!r}, expected {want!r} "
                        f"- downloads land outside the tree the *arrs import from")
    if section.get("auth_required") is False and session.get("rpc-authentication-required"):
        findings.append("transmission: still requires its own WebUI login - every user "
                        "meets a SECOND login after Cerulean (unset USER/PASS)")
    return findings, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                        help=f"the invariants manifest (default {DEFAULT_MANIFEST})")
    parser.add_argument("--appdata", default=str(DEFAULT_APPDATA),
                        help="where the apps' config.xml files are")
    parser.add_argument("--transmission",
                        default=os.environ.get("MONARCH_TRANSMISSION_URL", DEFAULT_TRANSMISSION),
                        help=f"Transmission RPC base (default {DEFAULT_TRANSMISSION})")
    parser.add_argument("--check", action="store_true",
                        help="report drift and change nothing (exit 2 on drift)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, change nothing")
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"cannot read the manifest {args.manifest}: {error}", file=sys.stderr)
        return 1

    applying = not (args.check or args.dry_run)
    arr_findings, arr_ok = check_arrs(manifest, Path(args.appdata), applying, args.dry_run)
    trans_findings, trans_ok = check_transmission(manifest, args.transmission)

    findings = trans_findings + arr_findings
    for finding in findings:
        print(f"FAIL {finding}", file=sys.stderr)
    if findings:
        return 2 if (trans_ok or arr_ok) else 1
    return 0 if (trans_ok and arr_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
