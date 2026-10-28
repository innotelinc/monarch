#!/usr/bin/env python3
"""Print how the generated dial actually came out — used while bringing up the
real Comcast numbering, and harmless to keep: it only reads the written playlist.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "livetv_lineup.py"
spec = importlib.util.spec_from_file_location("livetv_lineup", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

m3u = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/epg/comcast-springfield.m3u")
rows = []
for line in m3u.read_text(encoding="utf-8").splitlines():
    if not line.startswith("#EXTINF"):
        continue
    number = re.search(r'tvg-chno="([^"]*)"', line)
    name = line.rsplit(",", 1)[-1]
    if number:
        rows.append((number.group(1), name))

print(f"{len(rows)} channels in {m3u}")

groups: dict[str, list[str]] = defaultdict(list)
for number, name in rows:
    groups[number.split(".")[0]].append(name)

print("\nbase numbers carrying more than one stream (sub-feeds, or a bad bind):")
for base, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    if len(names) < 2 or int(base) > 400:
        continue
    print(f"  {base:>5}  {len(names):>2}  {names[0][:52]}")
    for name in names[1:]:
        print(f"  {'':>5}      {name[:52]}")

dial = mod.load_lineup()
print("\nspot checks against the matcher:")
for name in (
    "BET Cinema", "BET Her", "Absolute Reality by WE TV", "AMC en Espanol",
    "Autentic History", "USA Network (1080p)", "MBC 1 USA (1080p)", "USA TODAY (1080p)",
    "CNN (1080p)", "ESPN (720p)", "TNT (1080p)", "The Weather Channel (1080p)",
):
    hit = mod.lineup_of(name, dial)
    number = (hit or {}).get("number")
    label = (hit or {}).get("name") or "-"
    print(f"  {name:<34} -> {str(number):>6}  {label}   tokens={sorted(mod._tokens(name))}")
