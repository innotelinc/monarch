#!/usr/bin/env python3
"""Number the Live TV channels like a cable box, and group them like one too.

Jellyfin ingests one M3U tuner and shows every channel in a flat list: the tuner
playlist is iptv-org's `us.m3u`, so "the guide" is thousands of unlabelled rows in
playlist order — no channel numbers, no News/Sports/Movies split, and the ABC
stations, the CBS stations and the NBC stations are not gathered together, which
is the shape a viewer expects from a cable box.

This reads the same playlist Jellyfin is tuned to and the Comcast Springfield
lineup in `data/comcast-springfield-lineup.yml` (channel numbers with their
provenance) and writes:

  * `comcast-springfield.m3u` — the dial. A stream whose network has a cable
    number takes that number, and the playlist is ordered by it, so channel 5
    sits where channel 5 sits. Streams with no cable counterpart (FAST services)
    keep the category bands below, so nothing is hidden and nothing pretends to
    be a cable channel it is not.
  * one playlist per category (News, Weather, Sports, Movies, Kids, Music,
    Documentary, Lifestyle, Local, Entertainment, General), and one per network
    that appears more than once — the "more than one ABC station, one
    subcategory" rule. Every entry carries a `group-title`, which is what Jellyfin
    shows as the channel group, and a `tvg-chno`.
  * `comcast-springfield.xml` — the guide, renumbered: the programmes are the
    guide's own (nothing is invented), with each channel's display name and
    `<lcn>` set to its number, so "what's on now" appears against the dial rather
    than against the playlist's alphabetical order.

Usage:

    python3 scripts/livetv_lineup.py --plan                  # the taxonomy + lineup coverage
    python3 scripts/livetv_lineup.py --out /opt/epg          # write the playlists and guide
    python3 scripts/livetv_lineup.py --m3u FILE_OR_URL --guide /opt/epg/guide.xml --out /opt/epg

Writing into `/opt/epg` is the deployment: the `iptv` container serves that
directory at `http://iptv:3000/<name>`, which is how Jellyfin reaches each file
without anything else running. Point the tuner at `comcast-springfield.m3u` and
the XMLTV provider at `comcast-springfield.xml` and both the dial and the guide
are the Comcast-ordered ones.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

DEFAULT_M3U = os.environ.get(
    "LIVETV_M3U_URL", "https://iptv-org.github.io/iptv/countries/us.m3u")
LINEUP_PATH = Path(__file__).resolve().parent.parent / "data" / "comcast-springfield-lineup.yml"

# Ordered: the first rule that matches wins, so the specific beats the general
# ("CBS Sports HQ" is a sports channel, not a CBS affiliate, and belongs in
# Sports — but it is *also* part of the CBS network grouping, which the network
# pass handles separately).
CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("Weather", re.compile(r"\b(weather|accuweather|storm|climate)\b", re.I)),
    ("News", re.compile(
        r"\b(news|i24news|reuters|headlines|nowhere|scripps|oann|tyt|cheddar|"
        r"live ?now|court ?tv|cnn|bbc|sky news|cbc|newsmax|nexstar)\b", re.I)),
    ("Sports", re.compile(
        r"\b(sports?|nfl|nba|mlb|nhl|pga|mma|pfl|wrestling|kickbox|billiard|"
        r"poker|racers?|tennis|real madrid|draftkings|acc digital|willow|"
        r"strongman|pursuit|speedvision|glory|pbr|ridepass|golf)\b", re.I)),
    ("Movies", re.compile(
        r"\b(movies?|cinema|cine|filmex|filme|xumo free westerns|westerns|"
        r"action|thriller|horror|black cinema|film)\b", re.I)),
    ("Kids", re.compile(r"\b(kids|toon|baby shark|ninja kidz|pbs kids)\b", re.I)),
    ("Music", re.compile(r"\b(stingray|iheart|music|hits|country|soul|rock)\b", re.I)),
    ("Documentary", re.compile(
        r"\b(documentary|history|histories|true history|curiosity|nature|earth|"
        r"wildlife|wildearth|science|space|antiques)\b", re.I)),
    ("Lifestyle", re.compile(
        r"\b(food|kitchen|home|garden|design|travel|gotravel|house|tiny house|"
        r"weddings|tastemade|gusto|shop|qvc|hsn|hobby|craft)\b", re.I)),
    ("Local", re.compile(
        r"\b(boston|springfield|chicopee|worcester|hartford|new england)\b", re.I)),
    ("Entertainment", re.compile(
        r"\b(comedy|drama|game show|laugh|reality|ghost|haunt|mysteries|"
        r"midsomer|detective|crime|forensic|unsolved|dateline|reel)\b", re.I)),
]
FALLBACK = "General"

# A network worth its own subcategory: named station families that appear more
# than once in the playlist. Keys are the label shown in Jellyfin; values match
# the channel name. Checked most-specific-first.
NETWORKS: list[tuple[str, re.Pattern[str]]] = [
    ("Stingray", re.compile(r"\bstingray\b", re.I)),
    ("i24NEWS", re.compile(r"\bi24 ?news\b", re.I)),
    ("We TV", re.compile(r"\bwe tv\b", re.I)),
    ("PBS", re.compile(r"\bpbs\b", re.I)),
    ("BBC", re.compile(r"\bbbc\b", re.I)),
    ("CBS", re.compile(r"\bcbs\b", re.I)),
    ("NBC", re.compile(r"\bnbc\b", re.I)),
    ("ABC", re.compile(r"\babc\b", re.I)),
    ("FOX", re.compile(r"\b(fox|boston 25)\b", re.I)),
    ("AMC", re.compile(r"\bamc\b", re.I)),
    ("Lionsgate", re.compile(r"\b(lionsgate|movie ?sphere|ebony tv)\b", re.I)),
    ("FILMEX", re.compile(r"\bfilmex\b", re.I)),
    ("Just for Laughs", re.compile(r"\bjust for laughs\b", re.I)),
]

# Playlists emitted per category (file stem → label). Order sets the numbering
# band, so the guide reads news → weather → sports → movies → the rest.
CATEGORY_ORDER = [
    "News", "Weather", "Sports", "Movies", "Kids", "Music",
    "Documentary", "Lifestyle", "Local", "Entertainment", "General",
]
# Number bands: cable-like round numbers per category, so a category's channels
# cluster instead of interleaving. These are ours, not Comcast's, and they are
# what a stream with no cable number gets.
BANDS = {name: 100 + i * 100 for i, name in enumerate(CATEGORY_ORDER)}


def categorise(name: str) -> str:
    for label, pattern in CATEGORIES:
        if pattern.search(name):
            return label
    return FALLBACK


def network_of(name: str) -> str:
    for label, pattern in NETWORKS:
        if pattern.search(name):
            return label
    return ""


def load_lineup(path: Path | str = LINEUP_PATH) -> list[dict]:
    """The lineup file, with each entry's aliases compiled.

    File order is match order: a `locals` entry ("WSHM (CBS)") is consulted
    before the `networks` block, and within a block the file is written
    most-specific-first, which is why "CBS News 24/7" binds to the FAST service
    rather than to the CBS affiliate's number.
    """
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries: list[dict] = []
    for kind in ("locals", "networks"):
        for item in doc.get(kind) or []:
            if not item or not item.get("name"):
                continue
            entry = dict(item)
            entry["rules"] = [re.compile(alias, re.I) for alias in item.get("aliases") or []]
            entry["kind"] = "local" if kind == "locals" else "network"
            entries.append(entry)
    return entries


def lineup_of(name: str, lineup: list[dict]) -> dict | None:
    for item in lineup:
        if any(rule.search(name) for rule in item["rules"]):
            return item
    return None


def parse_m3u(text: str) -> list[dict]:
    """``[{name, url, attrs, group}]`` for the `#EXTINF` entries in a playlist."""
    entries: list[dict] = []
    pending: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            head, _, display = line.partition(",")
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', head))
            pending = {
                "name": (attrs.get("tvg-name") or display).strip(),
                "attrs": attrs,
                "url": "",
                "group": attrs.get("group-title", "").strip(),
            }
        elif line and not line.startswith("#") and pending is not None:
            pending["url"] = line
            entries.append(pending)
            pending = None
    return entries


def read_source(source: str) -> str:
    if re.match(r"^https?://", source):
        with urllib.request.urlopen(source, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    return Path(source).read_text(encoding="utf-8", errors="replace")


def plan(entries: list[dict], lineup: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """``(by_category, by_network)`` — networks only when they repeat."""
    by_category: dict[str, list[dict]] = {}
    by_network: dict[str, list[dict]] = {}
    for entry in entries:
        cable = lineup_of(entry["name"], lineup)
        entry["lineup"] = cable
        # A cable network's own category wins over the name rules: the guide
        # should group "A&E Crime 360" under Entertainment because that is where
        # the channel lives on the dial, not under crime drama.
        category = (cable or {}).get("category") or categorise(entry["name"])
        entry["category"] = category
        by_category.setdefault(category, []).append(entry)
        network = network_of(entry["name"])
        if network:
            entry["network"] = network
            by_network.setdefault(network, []).append(entry)
    # Only a repeated network is worth a subcategory — one station is not a
    # group, and saying so keeps the guide from filling up with one-channel
    # headings.
    by_network = {k: v for k, v in by_network.items() if len(v) > 1}
    return by_category, by_network


def dial_numbers(entries: list[dict], used: set[str] | None = None) -> dict[int, str]:
    """A channel number for every entry, cable numbers where the lineup has one.

    A cable number is shared by every stream of that network, which is what a
    real dial does with sub-feeds (`37`, `37.1`, `37.2`). Streams with no cable
    counterpart take a number in their category's band, continuing past anything
    the bands already use.
    """
    used = set(used or ())
    numbers: dict[int, str] = {}
    by_cable: dict[float, list[int]] = {}
    extras: list[int] = []
    for index, entry in enumerate(entries):
        number = (entry.get("lineup") or {}).get("number")
        if isinstance(number, (int, float)):
            by_cable.setdefault(float(number), []).append(index)
        else:
            extras.append(index)

    for number in sorted(by_cable):
        feeds = by_cable[number]
        base = f"{number:g}"
        for offset, index in enumerate(feeds):
            label = base if offset == 0 else f"{base}.{offset}"
            numbers[index] = label
            used.add(label)

    next_by_category: dict[str, int] = {}
    for index in extras:
        entry = entries[index]
        category = entry["category"]
        candidate = next_by_category.get(category, BANDS.get(category, 900))
        while str(candidate) in used:
            candidate += 1
        next_by_category[category] = candidate + 1
        numbers[index] = str(candidate)
        used.add(str(candidate))
    return numbers


def order_for_dial(entries: list[dict], numbers: dict[int, str]) -> list[int]:
    """Indexes in dial order: cable numbers numerically first, then the bands."""

    def sort_key(index: int) -> tuple[float, str]:
        cable = (entries[index].get("lineup") or {}).get("number")
        if isinstance(cable, (int, float)):
            return (float(cable), numbers[index])
        name = entries[index]["category"]
        return (BANDS.get(name, 900) + float(numbers[index].split(".")[0]) / 1000, numbers[index])

    return sorted(range(len(entries)), key=sort_key)


def render(entries: list[dict], numbers: dict[int, str], order: list[int], group) -> str:
    """An M3U over ``order``; ``group`` is a label or a callable per entry."""
    lines = ["#EXTM3U"]
    for index in order:
        entry = entries[index]
        attrs = entry["attrs"]
        name = entry["name"]
        label = group(entry) if callable(group) else group
        bits = [
            f'tvg-id="{attrs.get("tvg-id", "")}"',
            f'tvg-name="{name}"',
            f'tvg-chno="{numbers[index]}"',
            f'group-title="{label}"',
        ]
        if attrs.get("tvg-logo"):
            bits.append(f'tvg-logo="{attrs["tvg-logo"]}"')
        lines.append(f'#EXTINF:-1 {" ".join(bits)},{name}')
        lines.append(entry["url"])
    return "\n".join(lines) + "\n"


def renumber_guide(xml_text: str, numbers: dict[str, str]) -> str:
    """The guide with each channel named and numbered, programmes untouched.

    ``numbers`` maps a guide channel id (the playlist's `tvg-id`) to the dial
    number. A channel with no number keeps its name: it is in the playlist, so
    it is in the guide, and dropping it would make the guide disagree with the
    dial. `<lcn>` is the XMLTV element a client reads for a logical channel
    number; the display name carries it too, because Jellyfin shows names.
    """
    root = ET.fromstring(xml_text)
    for channel in root.findall("channel"):
        channel_id = channel.get("id") or ""
        number = numbers.get(channel_id)
        if not number:
            continue
        names = [el for el in channel.findall("display-name")]
        base = (names[0].text or channel_id) if names else channel_id
        for element in names[len(names) - 1:]:
            channel.remove(element)
        first = ET.SubElement(channel, "display-name")
        first.text = f"{number} {base}"
        lcn = ET.SubElement(channel, "lcn")
        lcn.text = number
    ordered = sorted(root.findall("channel"), key=lambda c: _number_key(numbers.get(c.get("id") or "")))
    for channel in ordered:
        root.remove(channel)
    for offset, channel in enumerate(ordered):
        root.insert(offset, channel)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _number_key(number: str | None) -> tuple[float, str]:
    if not number:
        return (float("inf"), "")
    try:
        return (float(number), number)
    except ValueError:
        return (float("inf"), number)


def report(entries: list[dict], by_category: dict[str, list[dict]],
           by_network: dict[str, list[dict]], lineup: list[dict],
           numbers: dict[int, str]) -> None:
    matched = [e for e in entries if e.get("lineup") and isinstance(e["lineup"].get("number"), (int, float))]
    print(f"livetv-lineup: {len(entries)} channels · {len(matched)} on a cable number · "
          f"{len(entries) - len(matched)} in category bands")
    for category in CATEGORY_ORDER:
        rows = by_category.get(category, [])
        if not rows:
            continue
        cable = [e for e in rows if isinstance((e.get("lineup") or {}).get("number"), (int, float))]
        networks = sorted({e.get("network", "") for e in rows if e.get("network")})
        print(f"  {category:<14} {len(rows):>4}  cable-numbered {len(cable):>3}  "
              f"subcategories: {(', '.join(networks)) or '-'}")
    print("  " + "-" * 60)
    for network, rows in sorted(by_network.items(), key=lambda kv: -len(kv[1])):
        print(f"  network {network:<12} {len(rows):>4}  "
              f"({', '.join(e['name'] for e in rows[:4])}{'…' if len(rows) > 4 else ''})")
    unused = [item for item in lineup
              if isinstance(item.get("number"), (int, float))
              and not any(e.get("lineup") is item for e in entries)]
    print(f"  lineup entries with a number and nothing to put on them: {len(unused)}")
    for item in unused[:12]:
        print(f"    {item['number']:>5}  {item['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--m3u", default=DEFAULT_M3U,
                        help="tuner playlist (URL or path; default LIVETV_M3U_URL)")
    parser.add_argument("--xml", help="an iptv-org channels.xml to read names from instead")
    parser.add_argument("--guide", help="an XMLTV file to renumber (the iptv container's guide.xml)")
    parser.add_argument("--lineup", default=str(LINEUP_PATH), help="the lineup dataset")
    parser.add_argument("--out", help="directory to write the playlists into")
    parser.add_argument("--plan", action="store_true", help="print the taxonomy, write nothing")
    args = parser.parse_args()

    lineup = load_lineup(args.lineup)
    if not lineup:
        print(f"livetv-lineup: no lineup entries in {args.lineup}", file=sys.stderr)
        return 1

    if args.xml:
        # Names only: enough to review the taxonomy against the real channel
        # list, and it is the list the guide already agrees with.
        root = ET.parse(args.xml).getroot()
        entries = []
        for channel in root:
            attrs = {}
            if channel.get("xmltv_id"):
                attrs["tvg-id"] = channel.get("xmltv_id")
            entries.append({"name": (channel.text or "").strip(), "attrs": attrs,
                            "url": "", "group": ""})
    else:
        entries = parse_m3u(read_source(args.m3u))
    if not entries:
        print("livetv-lineup: no channels found", file=sys.stderr)
        return 1

    by_category, by_network = plan(entries, lineup)
    numbers = dial_numbers(entries)
    order = order_for_dial(entries, numbers)

    report(entries, by_category, by_network, lineup, numbers)

    if args.plan or not args.out:
        print("(no --out given: nothing written)")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int]] = []

    dial = render(entries, numbers, order,
                  lambda entry: entry["category"] if not entry.get("lineup") or
                  not isinstance((entry["lineup"] or {}).get("number"), (int, float))
                  else "Cable channels")
    (out / "comcast-springfield.m3u").write_text(dial, encoding="utf-8")
    written.append(("comcast-springfield.m3u", len(entries)))

    for category in CATEGORY_ORDER:
        rows = by_category.get(category, [])
        if not rows:
            continue
        indexes = [i for i in order if entries[i]["category"] == category]
        # Category playlist: each entry's group is its network when the network
        # is one of the repeated ones (the subcategory), else the category.
        text = render(entries, numbers, indexes,
                      lambda entry: entry.get("network") if entry.get("network") in by_network
                      else category)
        name = f"{category.lower()}.m3u"
        (out / name).write_text(text, encoding="utf-8")
        written.append((name, len(indexes)))

    for network, rows in sorted(by_network.items()):
        slug = re.sub(r"[^a-z0-9]+", "-", network.lower()).strip("-")
        indexes = [i for i in order if entries[i].get("network") == network]
        name = f"network-{slug}.m3u"
        (out / name).write_text(render(entries, numbers, indexes, network), encoding="utf-8")
        written.append((name, len(indexes)))

    if args.guide:
        guide_path = Path(args.guide)
        by_id: dict[str, str] = {}
        for index, entry in enumerate(entries):
            tvg_id = entry["attrs"].get("tvg-id")
            if tvg_id and tvg_id not in by_id and entry["url"]:
                by_id[tvg_id] = numbers[index]
        xml = renumber_guide(guide_path.read_text(encoding="utf-8", errors="replace"), by_id)
        (out / "comcast-springfield.xml").write_text(xml, encoding="utf-8")
        written.append(("comcast-springfield.xml", len(by_id)))

    print(f"\nlivetv-lineup: wrote {len(written)} files into {out}")
    for name, count in written:
        print(f"  {name:<30} {count:>5}")
    print("\n  Served by the iptv container as http://iptv:3000/<name>; point the "
          "Jellyfin\ntuner at comcast-springfield.m3u and the XMLTV provider at "
          "comcast-springfield.xml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
