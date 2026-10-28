#!/usr/bin/env python3
"""Verify the Comcast dial + guide as Jellyfin actually holds them.

Prints the lowest-numbered channels (the cable block), how many channels carry a
number, and a programme sample from the guide for one of them.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ENV = Path("/usr/src/projects/complete/3-media/monarch/.env")
key = ""
for line in ENV.read_text().splitlines():
    if line.startswith("JELLYFIN_API_KEY="):
        key = line.partition("=")[2].strip().strip('"')
AUTH = {
    "Authorization": f'MediaBrowser Token="{key}", Client="verify", Device="host", '
                     'DeviceId="verify", Version="1"',
}


def get(path: str) -> dict:
    request = urllib.request.Request(f"http://localhost:8097{path}", headers=AUTH)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def number(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 99999.0


channels = get("/LiveTv/Channels?limit=5000")
items = channels.get("Items") or []
print(f"channels: {channels.get('TotalRecordCount')}")
print("the cable block:")
for channel in sorted(items, key=lambda c: number(c.get("Number")))[:16]:
    print(f"  {str(channel.get('Number')):>6}  {channel.get('Name')}")
print("carrying a number:", sum(1 for c in items if c.get("Number")), "of", len(items))

titled = [c for c in items if c.get("Number") and re.match(r"^\d", str(c.get("Name", "")))]
print("names that show the number:", len(titled), "e.g.", [c.get("Name") for c in titled[:4]])

# The guide covers only the channels the iptv container grabs for, so the guide
# check has to land on one of those. Walk the dial in order and report the first
# few channels that actually have programmes — which is also the answer to "what's
# on now", the point of asking the guide at all.
print("\nthe guide, on the dial:")
with_programmes = 0
for channel in sorted(items, key=lambda c: number(c.get("Number"))):
    if with_programmes >= 4:
        break
    if not re.match(r"^\d", str(channel.get("Number"))):
        continue
    programs = get(f"/LiveTv/Programs?ChannelIds={channel.get('Id')}&limit=2")
    rows = programs.get("Items") or []
    if not rows:
        continue
    with_programmes += 1
    print(f"  {channel.get('Number'):>6}  {channel.get('Name')}")
    for row in rows[:2]:
        start = (row.get("StartDate") or "")[11:16]
        print(f"          {start}  {row.get('Name')}")
print("\nchannels with guide data found in the first",
      min(len(items), 200), "of the dial:", with_programmes)
