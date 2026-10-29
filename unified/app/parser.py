"""Release-name parsing.

The *arr family hides a lot of value in this file, so it is written as a
deliberate parser rather than a pile of substring checks: a release name is a
sequence of tokens, and each attribute is matched at the point where scene
naming puts it. Everything it does not recognise is left unknown instead of
guessed, because a wrong guess here decides what gets downloaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}

_RESOLUTION = (
    ("2160p", ("2160p", "4k", "uhd")),
    ("1080p", ("1080p",)),
    ("1080i", ("1080i",)),
    ("720p", ("720p",)),
    ("576p", ("576p",)),
    ("480p", ("480p", "sd")),
)

_SOURCE = (
    ("remux", ("remux", "bdremux")),
    ("bluray", ("bluray", "blu-ray", "bdrip", "brrip", "bd25", "bd50")),
    ("web-dl", ("web-dl", "webdl", "web")),
    ("webrip", ("webrip", "web-rip")),
    ("hdtv", ("hdtv", "pdtv", "sdtv")),
    ("dvdrip", ("dvdrip", "dvd", "dvd5", "dvd9")),
    ("hdrip", ("hdrip",)),
    ("cam", ("cam", "camrip", "hdcam")),
    ("telesync", ("ts", "telesync", "hdts")),
    ("screener", ("screener", "dvdscr", "scr", "r5")),
)

_CODEC = (
    ("x265", ("x265", "h265", "h.265", "hevc")),
    ("x264", ("x264", "h264", "h.264", "avc")),
    ("av1", ("av1",)),
    ("vp9", ("vp9",)),
    ("xvid", ("xvid", "divx")),
    ("mpeg2", ("mpeg2", "mpeg-2")),
)

_AUDIO = (
    ("dts-hd ma", ("dts-hd.ma", "dts-hdma", "dts.ma")),
    ("dts-hd", ("dts-hd", "dtshd")),
    ("truehd", ("truehd",)),
    ("atmos", ("atmos",)),
    ("ddp", ("ddp", "dd+", "eac3", "e-ac3")),
    ("dts", ("dts",)),
    ("ac3", ("ac3", "dd5.1", "dd5 1", "dd2.0", "dolby.digital")),
    ("aac", ("aac", "aac2.0", "aac5.1")),
    ("flac", ("flac",)),
    ("mp3", ("mp3", "320kbps", "v0")),
    ("opus", ("opus",)),
)

_HDR = (
    ("dolby vision", ("dolby.vision", "dovi", "dv")),
    ("hdr10+", ("hdr10+", "hdr10plus")),
    ("hdr10", ("hdr10",)),
    ("hdr", ("hdr",)),
)

_EDITIONS = {
    "directors cut": ("directors.cut", "director's.cut", "dc"),
    "extended": ("extended", "extended.cut"),
    "uncut": ("uncut",),
    "remastered": ("remastered",),
    "imax": ("imax",),
    "theatrical": ("theatrical",),
}

_LANGUAGES = {
    "multi": ("multi", "multi.subs"),
    "dual": ("dual.audio", "dual"),
    "english": ("english", "eng"),
    "italian": ("italian", "ita"),
    "german": ("german", "ger", "deutsch"),
    "french": ("french", "fre", "vff", "vfq"),
    "spanish": ("spanish", "spa", "castellano"),
    "russian": ("russian", "rus"),
}

_SXXEXX = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})(?:[\s._-]?(?:E|-E)?(\d{1,3}))?\b", re.I)
_NXNN = re.compile(r"\b(\d{1,2})x(\d{2,3})\b")
_SEASON_ONLY = re.compile(r"\bS(\d{1,2})\b(?![\s._-]?E)", re.I)
_SEASON_WORD = re.compile(r"\bseason[\s._-]?(\d{1,2})\b", re.I)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|ts|mov|wmv|mpg|mpeg|webm|flac|mp3|m4a|ogg|opus|wav|aac)$", re.I)
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_GROUP = re.compile(r"-([A-Za-z0-9][A-Za-z0-9._-]{1,30})$")
_TOKEN_SPLIT = re.compile(r"[\s._]+")


def normalise(name: str) -> str:
    """Turn a release or file name into space-separated tokens.

    A *leading* bracketed group is dropped (the fansub convention:
    `[Group] Show - 12 [1080p]`), but later brackets keep their contents —
    that is where the resolution often lives, so flattening them would throw
    away the one tag the decision engine needs.
    """
    name = _EXT.sub("", name.strip())
    if name.startswith(("[", "(", "{")):
        name = re.sub(r"^[\[\(\{][^\]\)\}]*[\]\)\}]\s*", "", name)
    name = re.sub(r"[\[\(\{\]+|[\]\)\}]+", " ", name)
    name = name.replace("&", " and ")
    return _TOKEN_SPLIT.sub(" ", name).strip()


def title_key(title: str) -> str:
    """The comparison key used for title matching and dedupe."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", normalise(title).lower())
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class Release:
    raw: str
    title: str
    kind: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    audio: str | None = None
    hdr: str | None = None
    edition: str | None = None
    group: str | None = None
    languages: tuple[str, ...] = field(default_factory=tuple)
    size: int | None = None
    proper: bool = False
    repack: bool = False
    pack: bool = False

    @property
    def quality(self) -> str:
        """A short, stable label — this is what the catalog and UI store."""
        parts = [p for p in (self.resolution, self.source, self.codec, self.hdr) if p]
        return " ".join(parts) if parts else "unknown"

    @property
    def is_video(self) -> bool:
        return self.kind in {"movie", "episode"}

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "title": self.title,
            "kind": self.kind,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "episode_end": self.episode_end,
            "resolution": self.resolution,
            "source": self.source,
            "codec": self.codec,
            "audio": self.audio,
            "hdr": self.hdr,
            "edition": self.edition,
            "group": self.group,
            "languages": list(self.languages),
            "size": self.size,
            "proper": self.proper,
            "repack": self.repack,
            "pack": self.pack,
            "quality": self.quality,
        }


def _match(table, haystack: str) -> str | None:
    """The label whose alias appears *first* in the name.

    Position, not table order, decides: `DDP5.1.Atmos` is a DDP track that
    carries Atmos metadata, and `DTS-HD.MA` is MA before it is plain DTS. Ties
    fall back to table order so the more specific label wins.
    """
    best: tuple[int, int, str] | None = None
    for rank, (label, aliases) in enumerate(table):
        for alias in aliases:
            pattern = alias if any(ch in alias for ch in ".*+?[]") else re.escape(alias)
            match = re.search(rf"(?<![a-z0-9]){pattern}(?![a-z])", haystack)
            if not match:
                continue
            candidate = (match.start(), rank, label)
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else None


def _strip_tags(text: str) -> str:
    """Cut the name at the first token that is a technical tag."""
    tokens = text.split()
    for index, token in enumerate(tokens):
        low = token.lower()
        if (
            _YEAR.fullmatch(low)
            or any(low == label or low in aliases for label, aliases in _RESOLUTION)
            or any(low == label or low in aliases for label, aliases in _SOURCE)
            or any(low == label or low in aliases for label, aliases in _CODEC)
            or _SXXEXX.fullmatch(token)
            or _NXNN.fullmatch(token)
            or _SEASON_WORD.fullmatch(low)
            or low in {"proper", "repack", "internal", "limited"}
        ):
            return " ".join(tokens[:index]).strip()
    return text.strip()


def parse(name: str, kind: str | None = None) -> Release:
    """Parse a release or library file name into a Release."""
    raw = name
    text = normalise(name)
    low = text.lower()

    season = episode = episode_end = None
    pack = False

    m = _SXXEXX.search(text)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        if m.group(3):
            episode_end = int(m.group(3))
    else:
        m = _NXNN.search(text)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
        else:
            m = _SEASON_WORD.search(text) or _SEASON_ONLY.search(text)
            if m:
                season = int(m.group(1))

    # A season with no episode is a pack, whether or not the name says so.
    if season is not None and episode is None:
        pack = True

    year_match = _YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None

    # The title is everything before the first technical tag. For an episode
    # that means before SxxExx, so the episode marker must be cut as well.
    head = text
    if m:
        head = text[: m.start()]
    head = _strip_tags(head)
    if not head:
        head = _strip_tags(text)
    title = " ".join(head.split()).strip(" -")

    if kind is None:
        if any(ext in raw.lower() for ext in AUDIO_EXT):
            kind = "track"
        elif season is not None:
            kind = "episode"
        else:
            kind = "movie"
    if kind == "series" and season is not None:
        kind = "episode"

    group = None
    gm = _GROUP.search(raw.strip())
    if gm:
        candidate = gm.group(1)
        if not _YEAR.fullmatch(candidate) and candidate.lower() not in {
            "mkv", "mp4", "avi", "ts", "srt", "nfo",
        }:
            group = candidate

    languages = tuple(
        label for label, aliases in _LANGUAGES.items()
        if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low) for a in aliases)
    )

    edition = None
    for label, aliases in _EDITIONS.items():
        if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low) for a in aliases):
            edition = label
            break

    return Release(
        raw=raw,
        title=title or normalise(name),
        kind=kind,
        year=year,
        season=season,
        episode=episode,
        episode_end=episode_end,
        resolution=_match(_RESOLUTION, low),
        source=_match(_SOURCE, low),
        codec=_match(_CODEC, low),
        audio=_match(_AUDIO, low),
        hdr=_match(_HDR, low),
        edition=edition,
        group=group,
        languages=languages,
        proper=bool(re.search(r"\bproper\b", low)),
        repack=bool(re.search(r"\brepack\b", low)),
        pack=pack,
    )


def matches(release: Release, title: str, year: int | None = None,
            season: int | None = None, episode: int | None = None) -> bool:
    """Whether a candidate release is the thing we asked for.

    Titles must agree on their normalised key, the year must not contradict,
    and an episode request must match the season and episode. A pack that
    covers the episode counts; a different episode does not.
    """
    if title_key(release.title) != title_key(title):
        return False
    if year and release.year and release.year != year:
        return False
    if season is not None and release.season != season:
        return False
    if episode is not None:
        if release.episode is None:
            return release.pack
        end = release.episode_end or release.episode
        return release.episode <= episode <= end
    if episode is None and season is None and release.season is not None:
        # A movie request must not be satisfied by an episode.
        return False
    return True
