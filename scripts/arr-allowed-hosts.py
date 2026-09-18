#!/usr/bin/env python3
"""Make every *arr answer the name its peers call it by — and check that it does.

An *arr refuses a Host name it was not told about with HTTP 400 (its
DNS-rebinding guard; the default names are localhost only). Prowlarr's app test
and its indexer sync run over the compose network as `http://sonarr:8989` and
back as `http://prowlarr:9696`, so both ends were refused *before authentication*
— while the Apps page showed all four registered and every *arr showed an empty
indexer list. From the UI that reads as "Prowlarr is not registering".

WHY THIS ASKS THE APPS AND NOT THEIR CONFIG. `allowedHosts` is the setting that
produces the behaviour, but it is not a reliable proxy for it in either
direction: Lidarr (2.x) answers 200 to a service-name Host and does not persist
`allowedHosts` at all, so a config comparison reports permanent drift for an app
that is fine. `--check` therefore sends one request carrying the name the peers
use and treats a 400 as the finding — the thing that actually breaks the sync.

WHY THE RESTART IS PART OF APPLYING IT. The setting is read at startup, so an
app that has already read the old list keeps refusing the new name until it is
restarted; writing it and calling the API "applied" is what left this stack
broken for four apps. `--no-restart` exists for a maintenance window, not as the
normal path.

Usage:
    scripts/arr-allowed-hosts.py --check      # does each app answer its peers? (exit 2 if not)
    scripts/arr-allowed-hosts.py              # fix, restarting what changed
    scripts/arr-allowed-hosts.py --dry-run    # report, change nothing
    scripts/arr-allowed-hosts.py --no-restart # write the list, restart later

Exit codes: 0 every app answers (or, applying, fixed); 1 an app is unreachable or
the list could not be read; 2 --check found an app that refuses its peers.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = REPO / "init" / "arr-allowlist.txt"

# svc, port, api version — the five apps that talk to each other in this stack.
APPS = (
    ("prowlarr", 9696, "v1"),
    ("sonarr", 8989, "v3"),
    ("radarr", 7878, "v3"),
    ("lidarr", 8686, "v1"),
    ("whisparr", 6969, "v3"),
)
DEFAULT_APPDATA = Path("/docker/appdata")
REFUSED = 400


def parse_allowlist(text: str) -> list[str]:
    """The names in the file, in file order, without comments or blanks."""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        for name in line.replace(",", "\n").split():
            if name and name not in names:
                names.append(name)
    return names


def missing_hosts(have: list[str], want: list[str]) -> list[str]:
    """What the app must be told, keeping every name already in its list."""
    return [name for name in want if name not in have]


def merged(have: list[str], want: list[str]) -> str:
    return ",".join(have + missing_hosts(have, want))


def split_hosts(value: str | None) -> list[str]:
    return [name.strip() for name in (value or "").split(",") if name.strip()]


def evaluate(status: int | None) -> str:
    """What a status means: accepted | refused | unreachable.

    400 is the host guard specifically. Anything else — 200, or a 401/404 from
    an app that answers and does not like the request — means the name got past
    the guard, which is all this asks.
    """
    if status is None:
        return "unreachable"
    return "refused" if status == REFUSED else "accepted"


def probe(port: int, api: str, key: str, host: str, timeout: float = 10.0) -> int | None:
    """Ask the app at 127.0.0.1:port for its status, claiming to be `host`.

    Uses a raw connection rather than urllib: the Host header is the entire
    point, and a library that rewrites it would make this check say 200 forever.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", f"/api/{api}/system/status",
                           headers={"Host": host, "X-Api-Key": key})
        return connection.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def api_key(appdata: Path, svc: str) -> str:
    config = appdata / svc / "config.xml"
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                      config.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise RuntimeError(f"no <ApiKey> in {config}")
    return match.group(1)


def request(port: int, api: str, path: str, key: str, body: dict | None = None):
    url = f"http://127.0.0.1:{port}/api/{api}{path}"
    data = json.dumps(body).encode() if body is not None else None
    call = urllib.request.Request(url, data=data, method="PUT" if data else "GET")
    call.add_header("X-Api-Key", key)
    if data:
        call.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(call, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw or b"null") if raw else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read()[:300].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        raise RuntimeError(f"{url}: {error}") from error


def write_allowlist(port: int, api: str, key: str, want: list[str],
                    dry_run: bool) -> int:
    """Write the list into the app; returns how many names were missing.

    Reports 0 when the app already has every name, and does not write in that
    case — but a `0` here is not the same as "the app accepts the name": some
    builds ignore the field, which is why `probe` decides.
    """
    status, config = request(port, api, "/config/host", key)
    if status != 200 or not isinstance(config, dict):
        raise RuntimeError(f"config/host unreachable (HTTP {status})")
    have = split_hosts(config.get("allowedHosts"))
    missing = missing_hosts(have, want)
    if not missing or dry_run:
        return len(missing)
    config["allowedHosts"] = merged(have, want)
    # Servarr rejects the PUT unless these agree; there is no per-field endpoint.
    if config.get("password"):
        config["passwordConfirmation"] = config["password"]
    status, _ = request(port, api, "/config/host", key, config)
    if status not in (200, 202):
        raise RuntimeError(f"config/host PUT failed (HTTP {status})")
    return len(missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", default=str(ALLOWLIST_FILE),
                        help=f"the shared list (default {ALLOWLIST_FILE})")
    parser.add_argument("--appdata", default=str(DEFAULT_APPDATA),
                        help="where the apps' config.xml files are")
    parser.add_argument("--check", action="store_true",
                        help="ask each app whether it answers, and change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, change nothing")
    parser.add_argument("--no-restart", action="store_true",
                        help="do not restart the apps whose config changed")
    args = parser.parse_args(argv)

    try:
        want = parse_allowlist(Path(args.allowlist).read_text(encoding="utf-8"))
    except OSError as error:
        print(f"cannot read the allowlist: {error}", file=sys.stderr)
        return 1
    if not want:
        print(f"{args.allowlist} is empty — refusing to write an empty allowlist",
              file=sys.stderr)
        return 1

    reporting = args.check or args.dry_run
    applied: list[str] = []
    refused: list[str] = []
    failed = False

    for svc, port, api in APPS:
        try:
            key = api_key(Path(args.appdata), svc)
            verdict = evaluate(probe(port, api, key, f"{svc}:{port}"))
            if verdict == "accepted":
                print(f"{svc:<9} answers {svc}:{port}")
                continue
            if verdict == "unreachable":
                print(f"{svc:<9} unreachable on 127.0.0.1:{port}", file=sys.stderr)
                failed = True
                continue
            if reporting:
                print(f"{svc:<9} refuses Host: {svc}:{port} (peers are refused)")
                refused.append(svc)
                continue
            count = write_allowlist(port, api, key, want, args.dry_run)
            applied.append(svc)
            print(f"{svc:<9} refused — wrote the list (+{count}) and needs a restart")
        except (RuntimeError, OSError) as error:
            print(f"{svc:<9} FAIL {error}", file=sys.stderr)
            failed = True

    if applied and not args.no_restart:
        result = subprocess.run(("docker", "restart", *applied),
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"restart failed: {result.stderr.strip()[:200]}", file=sys.stderr)
            return 1
        print(f"restarted: {' '.join(applied)}")
        for svc, port, api in [a for a in APPS if a[0] in applied]:
            key = api_key(Path(args.appdata), svc)
            verdict = evaluate(probe(port, api, key, f"{svc}:{port}"))
            print(f"{svc:<9} {verdict} after the restart")
            if verdict != "accepted":
                failed = True

    if args.check:
        return 2 if (refused or failed) else 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
