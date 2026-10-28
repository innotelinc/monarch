#!/usr/bin/env python3
"""Refresh Jellyfin's live TV guide, then say whether it took.

The dial is regenerated on this host (`livetv_lineup.py` writes `/opt/epg`); the
part that is easy to forget is that Jellyfin keeps its *own* copy of the playlist
and the guide, and a fresh file on disk changes nothing until it re-reads it. The
operations doc points at Dashboard → Scheduled Tasks → Refresh Guide, which is
this, by hand.

It waits up to `TIMEOUT_SECONDS` for the task to finish rather than firing it and
returning: a caller that verifies immediately afterwards would otherwise read the
old guide and conclude the new one was wrong. A task that outlives the wait is
left running and the script says so; the guide task itself has no deadline.

Jellyfin also keeps the downloaded XMLTV in `cache/xmltv/<provider-id>.xml` and
reuses it across Refresh Guide runs, so a freshly written dial guide is otherwise
ignored until that cache expires. The cache is removed before a new task starts;
the refresh then downloads the file that is actually on disk. If a task is
already running the cache is left alone and this waits for that task instead, so
two refreshes never race over the same file. This is what makes the documented
"regenerate the dial, then refresh" step mean what it says.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
JELLYFIN = "http://localhost:8097"
TIMEOUT_SECONDS = 600
# Host path of Jellyfin's downloaded-guide cache. Override for a non-default
# appdata layout; the container sees the same files at /config/cache/xmltv.
XMLTV_CACHE = Path(os.environ.get("JELLYFIN_XMLTV_CACHE",
                                  "/docker/appdata/jellyfin/cache/xmltv"))


def clear_xmltv_cache(directory: Path = XMLTV_CACHE) -> int:
    """Remove Jellyfin's downloaded XMLTV copies, returning how many were removed.

    A missing directory is not an error: Jellyfin creates it after the first
    listing-provider download, and a host that has never refreshed a guide is
    exactly the case where there is nothing stale to remove.
    """
    if not directory.is_dir():
        print(f"no XMLTV cache at {directory} - nothing to clear")
        return 0
    removed = 0
    for path in sorted(directory.glob("*.xml")):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"could not remove {path}: {exc}", file=sys.stderr)
    print(f"cleared {removed} cached XMLTV file(s) from {directory}")
    return removed


def api_key() -> str:
    key = ""
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("JELLYFIN_API_KEY="):
            key = line.partition("=")[2].strip().strip('"')
    if not key:
        raise SystemExit(f"JELLYFIN_API_KEY is not set in {ENV}")
    return key


def headers(key: str) -> dict[str, str]:
    return {
        "Authorization": (
            f'MediaBrowser Token="{key}", Client="livetv-refresh", '
            'Device="host", DeviceId="livetv-refresh", Version="1"'
        )
    }


def call(path: str, key: str, method: str = "GET"):
    request = urllib.request.Request(f"{JELLYFIN}{path}", headers=headers(key), method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
    return json.loads(body) if body else None


def guide_tasks(key: str) -> list[dict]:
    return [
        task
        for task in (call("/ScheduledTasks", key) or [])
        if "guide" in f"{task.get('Key', '')}{task.get('Name', '')}".lower()
        or "livetv" in f"{task.get('Key', '')}{task.get('Name', '')}".lower()
    ]


def main() -> int:
    key = api_key()
    tasks = guide_tasks(key)
    if not tasks:
        print("no scheduled task mentions the guide — is Live TV configured?", file=sys.stderr)
        return 1

    # Never clear the cache out from under an active grab: the task is writing
    # that file. Wait for it instead — its result is the fresh guide too, and
    # starting a second one would only make the two race.
    running = [task for task in tasks if task.get("State") == "Running"]
    started = []
    if running:
        print(f"{len(running)} guide task(s) already running; waiting for them")
        started = running
    else:
        # Before the task starts: a cache file written by the previous refresh
        # is what Jellyfin would otherwise parse while the new one sits beside it.
        clear_xmltv_cache()
        for task in tasks:
            task_id = task.get("Id")
            try:
                call(f"/ScheduledTasks/Running/{task_id}", key, "POST")
            except urllib.error.HTTPError as error:
                print(f"could not start {task.get('Name')!r}: HTTP {error.code}", file=sys.stderr)
                continue
            started.append(task)
            print(f"started: {task.get('Name')}")

    by_id: dict = {}
    deadline = time.time() + TIMEOUT_SECONDS
    while started and time.time() < deadline:
        time.sleep(5)
        by_id = {task.get("Id"): task for task in guide_tasks(key)}
        running = [t for t in started if (by_id.get(t.get("Id")) or {}).get("State") == "Running"]
        if not running:
            break
        print(f"  … {len(running)} still running")

    for task in started:
        state = (by_id.get(task.get("Id")) or {}).get("State", "unknown")
        final = (by_id.get(task.get("Id")) or {}).get("LastExecutionResult") or {}
        print(f"  {task.get('Name')}: {state}  status={final.get('Status', 'n/a')}")
    still_running = [task for task in started
                     if (by_id.get(task.get("Id")) or {}).get("State") == "Running"]
    if still_running:
        print(f"  {len(still_running)} task(s) still running after {TIMEOUT_SECONDS}s; "
              "leaving them to finish in the background")
    return 0


if __name__ == "__main__":
    sys.exit(main())
