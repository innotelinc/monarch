#!/usr/bin/env python3
"""Push Prowlarr's indexers into the *arrs — and prove each app received them.

WHY THIS IS SEPARATE FROM prowlarr-indexers.py. That script fills *Prowlarr's*
list. Nothing there reaches Sonarr, Radarr, Lidarr or Whisparr: Prowlarr only
offers an indexer to each app during an application sync, and an app that was
registered after the last sync holds nothing until one runs. The state an
operator sees is Prowlarr full, every *arr empty, and the app tests green —
"Prowlarr is not registering its indexers" — while the actual cause is that the
sync never happened, or happened before the indexers existed.

WHAT COUNTS AS RECEIVED. An app answers "Prowlarr is registered" by holding an
indexer whose base URL points at Prowlarr's own Torznab proxy,
`<prowlarrUrl>/<id>/`. That is the only thing a synced indexer looks like, so it
is what --check counts, against the `prowlarrUrl` Prowlarr itself has filed for
that app rather than a name guessed here.

WHY "ZERO FROM PROWLARR" IS THE FINDING. Prowlarr will not sync every indexer to
every app — it skips one that returns no results in that app's categories (its
own FAQ: "Prowlarr will not sync X Indexer to App"), so Sonarr holding 22 of
Prowlarr's 73 is normal and must not be a failure. Holding *none* is not normal:
it means the sync never delivered, which is a different state from "delivered
what applied". So the check fails on zero, and only on zero, when Prowlarr has
an enabled indexer to offer.

Usage:
    scripts/arr-sync.py --check     # did each app receive? (exit 2 if not)
    scripts/arr-sync.py             # sync, wait, then report what arrived
    scripts/arr-sync.py --dry-run   # report, change nothing

Run it where Prowlarr's API and the apps' config.xml are reachable - the host
running the stack.

Exit codes: 0 every app received (or, applying, delivered); 1 Prowlarr, an app,
or an API key could not be reached; 2 --check found an app that received nothing
(or one of Prowlarr's own app tests is failing).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:9696"
DEFAULT_APPDATA = Path("/docker/appdata")
API = "/api/v1"

# svc, port, api version — the four apps Prowlarr syncs to, in this stack.
APPS = (
    ("sonarr", 8989, "v3"),
    ("radarr", 7878, "v3"),
    ("lidarr", 8686, "v1"),
    ("whisparr", 6969, "v3"),
)
# A sync pushes every indexer the app does not already have, so it is measured
# in tens of seconds even when nothing changed.
SYNC_TIMEOUT = 300.0
TERMINAL = ("completed", "failed", "aborted", "cancelled")


class ArrSyncError(RuntimeError):
    pass


class Prowlarr:
    def __init__(self, base: str, key: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def call(self, method: str, path: str, body: object | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{API}{path}", data=data, method=method)
        request.add_header("X-Api-Key", self.key)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"null")
            except ValueError:
                return error.code, raw[:400].decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            raise ArrSyncError(f"{method} {path}: {error}") from error
        try:
            return response.status, json.loads(raw or b"null")
        except ValueError:
            return response.status, raw[:400].decode("utf-8", "replace")

    def applications(self) -> list[dict]:
        # Prowlarr's route is plural here and singular for /indexer - both were
        # checked against a live 2.6.5, and a 404 is silent about which is which.
        status, body = self.call("GET", "/applications")
        if status != 200 or not isinstance(body, list):
            raise ArrSyncError(f"application list unreachable (HTTP {status})")
        return body

    def test_all(self) -> dict[str, tuple[bool, str]]:
        """Prowlarr's own test of every registered app, keyed by name.

        This is the side of the app link that is *not* the Host guard: it proves
        Prowlarr can log in to the app and read its indexers, which every sync
        below depends on.
        """
        status, body = self.call("POST", "/applications/testall")
        if status not in (200, 201, 202) or not isinstance(body, list):
            raise ArrSyncError(f"application test unavailable (HTTP {status})")
        by_id = {int(app["id"]): app.get("name") or str(app["id"]) for app in self.applications()}
        results: dict[str, tuple[bool, str]] = {}
        for entry in body:
            name = by_id.get(int(entry.get("id", 0)), str(entry.get("id")))
            failures = [str(f.get("errorMessage") or f) for f in entry.get("validationFailures") or []]
            results[name] = (bool(entry.get("isValid")), "; ".join(failures)[:200])
        return results

    def indexer_count(self) -> int:
        status, body = self.call("GET", "/indexer")
        if status != 200 or not isinstance(body, list):
            raise ArrSyncError(f"indexer list unreachable (HTTP {status})")
        return sum(1 for indexer in body if indexer.get("enable"))

    def start_sync(self, application_id: int) -> int:
        status, body = self.call("POST", "/command",
                                 {"name": "ApplicationIndexerSync", "applicationId": application_id})
        if status not in (200, 201, 202) or not isinstance(body, dict):
            raise ArrSyncError(f"could not start the sync for application {application_id} (HTTP {status})")
        return int(body.get("id") or 0)

    def wait(self, command_id: int, deadline: float = SYNC_TIMEOUT) -> str:
        """The command's final status, polling because the sync is asynchronous."""
        end = time.monotonic() + deadline
        status_text = "queued"
        while time.monotonic() < end:
            status, body = self.call("GET", f"/command/{command_id}")
            if status != 200 or not isinstance(body, dict):
                return "unknown"
            status_text = str(body.get("status") or "unknown").lower()
            if status_text in TERMINAL:
                return status_text
            time.sleep(1.0)
        return status_text


def prowlarr_url(fields: list[dict]) -> str:
    """The `prowlarrUrl` Prowlarr filed for an app, as the prefix an indexer carries."""
    for field in fields or []:
        if field.get("name") == "prowlarrUrl":
            return str(field.get("value") or "").rstrip("/")
    return ""


def is_from_prowlarr(indexer: dict, prowlarr_base: str) -> bool:
    """True when this app-side indexer is one Prowlarr delivered.

    Compares the base URL by path boundary: `http://prowlarr:9696/37/` is a
    delivered indexer, `http://prowlarr:9696.example/` is not, and neither is a
    hand-added indexer pointing somewhere else.
    """
    if not prowlarr_base:
        return False
    for field in indexer.get("fields") or []:
        if field.get("name") == "baseUrl":
            base = str(field.get("value") or "").rstrip("/")
            return base == prowlarr_base or base.startswith(f"{prowlarr_base}/")
    return False


def evaluate(received: int, offered: int) -> str:
    """What a count means: delivered | nothing_to_deliver | not_registered."""
    if received > 0:
        return "delivered"
    return "nothing_to_deliver" if offered == 0 else "not_registered"


def api_key(appdata: Path, svc: str) -> str:
    config = appdata / svc / "config.xml"
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                      config.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise ArrSyncError(f"no <ApiKey> in {config}")
    return match.group(1)


def received(appdata: Path, svc: str, port: int, api: str, prowlarr_base: str,
             timeout: float = 20.0) -> int:
    """How many of this app's indexers came from Prowlarr.

    Asks the app itself rather than Prowlarr: an indexer Prowlarr believes it
    delivered, and which the app has since dropped, is exactly the state this is
    for.
    """
    key = api_key(appdata, svc)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/{api}/indexer")
    request.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            indexers = json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raise ArrSyncError(f"indexer list unreachable (HTTP {error.code})") from error
    except (urllib.error.URLError, OSError) as error:
        raise ArrSyncError(str(error)) from error
    if not isinstance(indexers, list):
        raise ArrSyncError("indexer list was not a list")
    return sum(1 for indexer in indexers if is_from_prowlarr(indexer, prowlarr_base))


def report(prowlarr: Prowlarr, appdata: Path, offered: int, tests: dict, applying: bool) -> int:
    """One line per app, and 2 when an app that should have received did not."""
    findings: list[str] = []
    unreachable = 0
    for app in prowlarr.applications():
        name = app.get("name") or str(app.get("id"))
        svc = name.lower()
        entry = next((a for a in APPS if a[0] == svc), None)
        prowlarr_base = prowlarr_url(app.get("fields") or [])
        valid, detail = tests.get(name, (False, "not tested"))
        if not valid:
            print(f"{svc:<9} Prowlarr's own test FAILED: {detail}")
            findings.append(f"{svc}: Prowlarr cannot talk to it ({detail})")
            continue
        if entry is None:
            print(f"{svc:<9} registered with Prowlarr, not a local app - not counted")
            continue
        try:
            count = received(appdata, entry[0], entry[1], entry[2], prowlarr_base)
        except (ArrSyncError, OSError) as error:
            print(f"{svc:<9} unreachable on 127.0.0.1:{entry[1]} ({error})", file=sys.stderr)
            unreachable += 1
            continue
        verdict = evaluate(count, offered)
        if verdict == "not_registered":
            print(f"{svc:<9} holds NO indexer from Prowlarr while Prowlarr offers {offered}")
            findings.append(f"{svc}: 0 of Prowlarr's {offered} indexer(s) delivered")
        elif verdict == "nothing_to_deliver":
            print(f"{svc:<9} holds {count} from Prowlarr - nothing to deliver yet")
        else:
            print(f"{svc:<9} holds {count} indexer(s) from Prowlarr")
    if findings:
        if not applying:
            print("", file=sys.stderr)
        for finding in findings:
            print(f"FAIL {finding}", file=sys.stderr)
        return 2
    return 1 if unreachable else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"Prowlarr base URL (default {DEFAULT_BASE})")
    parser.add_argument("--api-key", default="",
                        help="Prowlarr API key (default: read config.xml from --appdata)")
    parser.add_argument("--appdata", default=str(DEFAULT_APPDATA),
                        help="where the apps' config.xml files are")
    parser.add_argument("--check", action="store_true",
                        help="report whether each app received, and change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be synced, change nothing")
    parser.add_argument("--timeout", type=float, default=SYNC_TIMEOUT,
                        help=f"seconds to wait for one sync (default {SYNC_TIMEOUT:.0f})")
    args = parser.parse_args(argv)

    key = args.api_key.strip()
    if not key:
        config = Path(args.appdata) / "prowlarr" / "config.xml"
        try:
            match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                              config.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            print(f"cannot read {config}: {error}", file=sys.stderr)
            return 1
        if not match:
            print(f"no <ApiKey> in {config}", file=sys.stderr)
            return 1
        key = match.group(1)

    prowlarr = Prowlarr(args.base, key)
    try:
        applications = prowlarr.applications()
        offered = prowlarr.indexer_count()
        applying = not args.check and not args.dry_run
        for app in applications:
            name = app.get("name") or str(app.get("id"))
            if args.check:
                continue
            if args.dry_run:
                print(f"would sync {name}")
                continue
            command = prowlarr.start_sync(int(app["id"]))
            print(f"synced {name}: {prowlarr.wait(command, deadline=args.timeout)}")
        tests = prowlarr.test_all()
        return report(prowlarr, Path(args.appdata), offered, tests, applying=applying)
    except ArrSyncError as error:
        print(f"arr-sync: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
