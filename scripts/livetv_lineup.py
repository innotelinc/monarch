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
    sits where channel 5 sits. A stream that advertises HD takes the network's
    HD row, and the SD row beside it is then empty — so the same feed is listed
    there as well, because the lineup says both numbers are that one channel and
    a viewer typing the low number should still land on it. Streams with no
    cable counterpart (FAST services) keep the category bands below, so nothing
    is hidden and nothing pretends to be a cable channel it is not.
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

DEFAULT_M3U = os.environ.get(
    "LIVETV_M3U_URL", "https://iptv-org.github.io/iptv/countries/us.m3u")
# The dial itself, exactly as the operator supplied it: one `number<TAB>name` per
# line. This is the authoritative source for a channel number, so it is stored
# verbatim rather than transcribed into a second format that could disagree with
# it — every number here is Comcast's for this franchise, and nothing in this
# file is inferred.
LINEUP_PATH = Path(__file__).resolve().parent.parent / "data" / "comcast-springfield-channels.tsv"

# Ordered: the first rule that matches wins, so the specific beats the general
# ("CBS Sports HQ" is a sports channel, not a CBS affiliate, and belongs in
# Sports — but it is *also* part of the CBS network grouping, which the network
# pass handles separately).
#
# The brand lists are long because they are the *cable* brands now, not just the
# FAST ones: the dial these playlists mirror has ESPN on 49 and TNT on 33, and a
# category playlist that filed either under "General" would be no use to anybody
# comparing it against their TV.
CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("Weather", re.compile(
        r"\b(weather|accuweather|storm|climate|fox weather)\b", re.I)),
    ("News", re.compile(
        r"\b(news|i24news|reuters|headlines|nowhere|scripps|oann|tyt|cheddar|"
        r"live ?now|court ?tv|cnn|bbc|sky news|cbc|newsmax|nexstar|cnbc|"
        r"bloomberg|cspan|ms now|msnbc|fox business|newsnation|new england "
        r"cable|herald|noticias|estrella|comercio|ntd|oann|newsy|scripps)\b", re.I)),
    ("Sports", re.compile(
        r"\b(sports?|nfl|nba|mlb|nhl|mls|pga|mma|pfl|wrestling|kickbox|billiard|"
        r"poker|racers?|tennis|real madrid|draftkings|acc digital|willow|"
        r"strongman|pursuit|speedvision|glory|pbr|ridepass|golf|espn|nesn|"
        r"sec network|big ten|fanduel|zona futbol|tudn|deportes|universo|"
        r"extra innings|league pass|center ice|redzone|outdoor channel|"
        r"sportsman|pickleball|unbeaten|billiards|bowling|ryz|phly|dnvr|chgo|joyn|"
        r"speed|olympic|ufc|sportsnet|beIN|premier league|f1 )\b", re.I)),
    ("Movies", re.compile(
        r"\b(movies?|cinema|cine|filmex|filme|xumo free westerns|westerns|"
        r"action|thriller|horror|black cinema|film|hbo|cinemax|showtime|sho |"
        r"starz|mgm\+|screenpix|flix|the movie channel|movie channel|tcm|"
        r"turner classic|ifc|sundance|encore|outer ?sphere|moviesphere|"
        r"universal (action|monsters|movies|westerns)|outflix|pelimex|todo cine|"
        r"canela|dnu cine|viendo ?movies|cinema dinamita|sony cine)\b", re.I)),
    ("Kids", re.compile(
        r"\b(kids|toon|baby shark|ninja kidz|pbs kids|disney|nick|cartoon|"
        r"babyfirst|boomerang|meTV toons|primo tv|kids street)\b", re.I)),
    ("Music", re.compile(
        r"\b(stingray|iheart|music choice|music|hits|country|soul|rock|mtv|"
        r"vh1|cmt|bet|axs|revolt|afro|loop|bounce|the grio|shades of black|"
        r"mtv live|nick music)\b", re.I)),
    ("Documentary", re.compile(
        r"\b(documentary|histor(?:y|ies)|true history|curiosity|nature|earth|"
        r"wildlife|wild ?earth|science|space|antiques|discovery|national "
        r"geographic|smithsonian|animal planet|military history|american "
        r"heroes|investigation discovery|crime \+ investigation|"
        r"science channel|discovery turbo|love nature|mysterious worlds)\b", re.I)),
    ("Lifestyle", re.compile(
        r"\b(food|kitchen|home|garden|design|travel|gotravel|house|tiny house|"
        r"weddings|tastemade|gusto|shop|qvc|hsn|hobby|craft|hgtv|home & "
        r"garden|tlc|cooking|magnolia|jewelry|recipe|how-to|handyman|"
        r"jamie oliver|test kitchen|dog whisperer|family handyman|"
        r"million dollar|say yes|shopp|ing|powernation|estate|balance|fat |"
        r"hobby|diy|shop lc|binge|gems)\b", re.I)),
    ("Local", re.compile(
        r"\b(boston|springfield|chicopee|worcester|hartford|new england|"
        r"wgby|wscc|wggb|wwlp|wshm|wedh|whtx|wdmr|local access|leased|"
        r"westfield|local \d|comcast employee|greater boston employee)\b", re.I)),
    ("Entertainment", re.compile(
        r"\b(comedy|drama|game show|laugh|reality|ghost|haunt|mysteries|"
        r"midsomer|detective|crime|forensic|unsolved|dateline|reel|tbs|tnt|"
        r"usa|fx|fxx|e!|syfy|paramount|pop |tv land|we tv|oxygen|hallmark|"
        r"a&e|tru ?tv|bravo|freeform|amc|logo|in ?sp|justice|as ?pire|"
        r"own|oprah|trutv|vice|fy i|fyi|comedy\.tv|cnbc|bet her|cleo|ebony|"
        r"lifetime|lmn|uptv|gaf|great american|ovation|a&e network|the "
        r"conners|family feud|deal or no deal|america's got talent|"
        r"price is right|game show central|buzzr|ninja warrior|baywatch|"
        r"family entertainment|sonlife|god tv|intouch|impact|daystar|ewtn|"
        r"tbn|trinity broadcasting|insp|circle country|pbs )\b", re.I)),
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


# Words that name the *feed*, not the network. Dropping them is what makes "CNN",
# "CNN HD" and "CNN HD East" one channel, while "CNN en Español" stays a
# different one — the words that survive are the ones that identify a service.
# A call sign's own suffixes go too: "WSHM-LP", "WGBY-DT" and "WGGB-DT2" are the
# local station, and a stream named after the station has to reach it.
FEED_TOKENS = {
    "lp", "ld", "dt", "dt2", "dt3", "dt4", "rf",
    "hd", "sd", "uhd", "4k", "east", "west", "pacific", "feed", "stream",
    "streaming", "excludes", "adult", "swim", "the", "a", "of", "and", "with",
    "channel", "network", "television", "cable", "tv", "north", "america",
    "canada", "plus", "e", "teve",
}
# Deliberately *not* dropped: "us" and "usa", because USA Network is a channel and
# dropping the word would leave it with no name at all.
# Kept deliberately: "national" is dropped above but "geographic" is not, so
# "National Geographic" and "Nat Geo Wild" stay apart; "en" is not dropped, so
# the Spanish feeds do too.
KEEP_TOKENS = {"en", "espanol", "espa", "black", "wild", "family", "classic"}

# The dial lists a network's SD and HD positions as separate rows (41 Fox News
# Channel / 841 Fox News Channel HD, 42 Cable News Network / 842 CNN HD), and a
# stream says which one it belongs on. Quality is read from the *raw* name rather
# than from the identifying words, because those words are exactly what is dropped
# above (`HD`, and a `(720p)` parenthetical). A name that says nothing is "" — and
# "" matches a dial row that also says nothing, which is the classic SD slot.
#
# Without this, every HD feed binds to the lowest-numbered simulcast and the HD
# block is the block nothing reaches.
HD_WORDS = {"hd", "fhd", "uhd", "4k", "8k"}
SD_WORDS = {"sd", "ld"}
RESOLUTION = re.compile(r"\b(\d{3,4})[pi]\b", re.I)


def quality_of(name: str) -> str:
    """``"hd"``, ``"sd"`` or ``""`` for the feed a name advertises (or none).

    A quality word anywhere in the name wins, then a resolution in brackets
    (720p and up is HD, 480p and below is SD). ``""`` means the name claims
    neither, and that is matched against the dial rows that claim neither.
    """
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    if words & HD_WORDS:
        return "hd"
    if words & SD_WORDS:
        return "sd"
    resolutions = [int(value) for value in RESOLUTION.findall(name)]
    if resolutions:
        return "hd" if max(resolutions) >= 720 else "sd"
    return ""

# The dial keeps some channels under names nobody says out loud — "Cable News
# Network" is CNN, "Home & Garden Television" is HGTV, "MS NOW" is what MSNBC
# became. A stream is named the way people say it, so each of these adds the
# shorthand to the entry's identifying words. Keyed by the dial name, lowercased.
#
# Only shorthand that cannot collide is listed: "weather" is already a word of
# "The Weather Channel", and "cnn" cannot match anything else on this dial.
DIAL_ALIASES: dict[str, tuple[str, ...]] = {
    "cable news network": ("cnn",),
    "home & garden television": ("hgtv",),
    "ms now": ("msnbc",),
    "ms now hd": ("msnbc",),
    "national geographic": ("nat", "geo"),
    "national geographic usa": ("nat", "geo"),
    "national geographic hd": ("nat", "geo"),
    "national geographic wild": ("nat", "geo", "wild"),
    "e! entertainment television": ("e",),
    "tru tv": ("trutv",),
    "truTV": ("trutv",),
    "the weather channel": ("twc",),
    "turner classic movies": ("tcm",),
    "the movie channel": ("tmc",),
    "fox news channel": ("fnc",),
    "discovery channel": ("discovery",),
    "home shopping network": ("hsn",),
    "black entertainment television": ("bet",),
}


# A lone dial word that is a *genre* rather than a channel. "Cable News Network"
# reduces to just `news` once its feed words go, and a stream called "CBS News
# 24/7" would otherwise bind to CNN's number — which is how a name-matcher hands
# out confidently wrong dial positions. A single generic word may never carry a
# match; the entry still matches by its full name or by a shorthand alias.
GENERIC_TOKENS = {
    "news", "sports", "sport", "weather", "movies", "movie", "kids", "music",
    "documentary", "entertainment", "lifestyle", "local", "general", "live",
    "now", "free", "tv", "classic", "radio", "hot", "hispanic", "action",
    # A country is not a channel, and USA Network's own name is nothing else: with
    # this, "MBC 1 USA", "USA TODAY" and "Potta-Divine TV USA" stop being filed
    # under channel 35, while a stream that *is* "USA Network" still is.
    "us", "usa",
    # Genre words that happen to be the whole of a dial entry's name. "Autentic
    # History" is not the History channel and "Autentic Travel" is not the Travel
    # Channel; both bind on the one word they share. The channels keep their own
    # names because a stream named after them *is* that word.
    "history", "travel", "science", "outdoor", "we", "pop",
}


def _tokens(name: str) -> frozenset[str]:
    """The words that identify a channel, quality and feed suffixes removed."""
    return frozenset(_words(name))


def _words(name: str) -> list[str]:
    cleaned = name.lower()
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9+&']+", " ", cleaned)
    out = []
    for token in cleaned.split():
        if token in KEEP_TOKENS:
            out.append(token)
        elif token not in FEED_TOKENS and len(token) > 1:
            out.append(token)
    return out


def _normalised(name: str) -> str:
    """The name as identifying words in order — what "the same channel" means."""
    return " ".join(_words(name))


def load_lineup(path: Path | str = LINEUP_PATH) -> list[dict]:
    """The dial, one entry per `number<TAB>name` line, in file order.

    Every entry is a real Comcast number for this franchise, so `source` is the
    same for all of them; it exists because the report and the tests read it, and
    because "where did this number come from" should have an answer on the entry
    rather than in someone's memory of a conversation.
    """
    entries: list[dict] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        number, tab, name = line.partition("\t")
        name = name.strip()
        if not tab or not name:
            continue
        try:
            value: int | float = int(number.strip())
        except ValueError:
            try:
                value = float(number.strip())
            except ValueError:
                continue
        # Read as one name *or* another, not one name *and* another: a stream called
        # "CNN" has to satisfy the shorthand alone, and there is no stream that says
        # both "Cable News Network" and "CNN".
        token_sets = [_tokens(name)]
        shorthand = tuple(
            token for token in DIAL_ALIASES.get(name.strip().lower(), ())
            if token not in FEED_TOKENS
        )
        if shorthand:
            # One set, not one set per word: "nat geo wild" is a name for a
            # channel, and treating its words separately would let any one of them
            # carry a match on its own.
            token_sets.append(frozenset(shorthand))
        entries.append({
            "name": name,
            "number": value,
            "source": "comcast-springfield",
            "aliases": [name],
            "normalised": _normalised(name),
            "tokens": [t for t in token_sets if t],
            "category": categorise(name),
            "quality": quality_of(name),
            "kind": "network",
        })
    return entries


def lineup_of(name: str, lineup: list[dict]) -> dict | None:
    """The dial entry a stream belongs to, or None.

    Matching is on the identifying words, not on a regex per channel: every token
    of the dial entry has to appear in the stream's name, and the *most specific*
    entry wins. Specificity is what keeps "CNN en Español" off CNN's 42 — it
    matches both, and the one whose words are all used is the right one.

    Among equally specific entries the *feed* decides, because the dial lists a
    network's SD and HD positions as separate rows: an HD stream takes the HD row
    (CNN HD is 842, Fox News Channel (720p) is 841) and a name that says nothing
    stays on the classic position (CNN is 42). Only then does the lowest number
    decide, which is what makes one channel's full name and its shorthand land on
    the same row rather than on a 1100-block simulcast.

    A tie in *all* of those is genuinely ambiguous (two dial entries with the same
    name and feed), and the lowest number is the honest answer: it is the position
    a viewer would try first.
    """
    stream_tokens = _tokens(name)
    if not stream_tokens:
        return None
    stream_quality = quality_of(name)
    best: tuple | None = None
    best_entry: dict | None = None
    for entry in lineup:
        number = float(entry["number"])
        for tokens in entry.get("tokens") or ():
            if not tokens or not tokens <= stream_tokens:
                continue
            # A lone generic word only counts when the stream *is* that word — so
            # "The Weather Channel" still claims its own name, while "FOX Weather"
            # does not get handed it for containing the word "weather".
            if (
                len(tokens) == 1
                and next(iter(tokens)) in GENERIC_TOKENS
                and stream_tokens != tokens
            ):
                continue
            extra = len(stream_tokens - tokens)
            # Fewest unused stream words first, so a more specific channel still
            # wins: "CNN en Español HD" is 709 even though CNN's 842 row is the HD
            # one. Then the feed has to agree — an HD stream takes the HD row and
            # a name that says nothing stays on the classic position — and only
            # then does the lower number decide, which is what makes one channel's
            # full name and its shorthand land on the same row.
            quality = 0 if entry.get("quality", "") == stream_quality else 1
            key = (extra, quality, -len(tokens), number)
            if best is None or key < best:
                best, best_entry = key, entry
    return best_entry


def simulcast_rows(entries: list[dict], lineup: list[dict]) -> list[dict]:
    """Extra dial entries for the SD rows the HD rule leaves empty.

    The dial lists a network's SD and HD positions as two rows, and `lineup_of`
    sends an HD stream to the HD row — which is right, but it leaves the classic
    position showing nothing, so the low end of the dial empties out and a viewer
    typing "8" finds no Jewelry Television at all. The lineup says both rows are
    the same channel, so where the SD row is empty the stream goes on it as well
    and both numbers resolve.

    The SD row is found by running the same matcher over the non-HD rows, which
    is the row the stream would have taken before quality was scored at all: a
    name like "Jewelry TV 2" shares no identifying word with row 8's "Jewelry
    Television", yet the token match still finds it, and only the real matcher
    knows that.
    """
    sd_rows = [row for row in lineup if row.get("quality") != "hd"]
    taken = {
        float((entry.get("lineup") or {}).get("number"))
        for entry in entries
        if isinstance((entry.get("lineup") or {}).get("number"), (int, float))
    }
    out: list[dict] = []
    for entry in entries:
        cable = entry.get("lineup") or {}
        if cable.get("quality") != "hd":
            continue
        row = lineup_of(entry["name"], sd_rows)
        if row is None or float(row["number"]) in taken:
            continue
        taken.add(float(row["number"]))
        out.append({**entry, "lineup": row, "simulcast": True})
    return out


def _split_extinf(line: str) -> tuple[str, str]:
    """``(head, title)`` for an `#EXTINF` line.

    The title is what follows the comma that *ends the attribute list*, and that
    is not the first comma: attribute values are quoted and may contain one.
    iptv-org's `http-user-agent` does — "…AppleWebKit/537.36 (KHTML, like Gecko)
    Chrome/144…" — so splitting on the first comma puts half a user agent into
    the title and, for a stream with no `tvg-name`, hands it the name: channel 33
    was listed as `like Gecko) Chrome/144.0.0.0 Safari/537.36" group-title=…`.
    """
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            return line[:index], line[index + 1:]
    return line, ""


def parse_m3u(text: str) -> list[dict]:
    """``[{name, url, attrs, group}]`` for the `#EXTINF` entries in a playlist."""
    entries: list[dict] = []
    pending: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            head, display = _split_extinf(line)
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
    # Added after `plan` on purpose: the simulcasts belong on the dial, which is
    # what Jellyfin tunes to, but a category playlist listing one channel twice
    # would just be a longer list of the same thing.
    entries = entries + simulcast_rows(entries, lineup)
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
