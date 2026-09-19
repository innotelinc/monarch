#!/usr/bin/env python3
"""Refresh Jellyfin's live TV guide, then say whether it took.

The dial is regenerated on this host (`livetv_lineup.py` writes `/opt/epg`); the
part that is easy to forget is that Jellyfin keeps its *own* copy of the playlist
and the guide, and a fresh file on disk changes nothing until it re-reads it. The
operations doc points at Dashboard → Scheduled Tasks → Refresh Guide, which is
this, by hand.

It waits for the task to finish rather than firing it and returning: a caller that
verifies immediately afterwards would otherwise read the old guide and conclude
the new one was wrong.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
JELLYFIN = "http://localhost:8097"
TIMEOUT_SECONDS = 600


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

    started = []
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
