"""Live TV without a TV backend.

TVHeadend, NextPVR, Dispatcharr and autobrr each existed to turn a playlist
and a guide into something a player could use. This module does that directly:
an M3U playlist is the tuner, an XMLTV document is the guide, and channels are
numbered deterministically so a number always means the same channel.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ATTR = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


@dataclass(frozen=True)
class Channel:
    tvg_id: str
    name: str
    group: str = ""
    logo: str = ""
    url: str = ""
    number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "id": self.tvg_id,
            "name": self.name,
            "group": self.group,
            "logo": self.logo,
            "url": self.url,
        }


@dataclass(frozen=True)
class Programme:
    channel_id: str
    title: str
    start: datetime
    stop: datetime
    description: str = ""
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "stop": self.stop.isoformat(),
            "description": self.description,
            "category": self.category,
        }


def parse_m3u(text: str) -> list[Channel]:
    """Parse an M3U/M3U8 playlist into channels, in playlist order."""
    channels: list[Channel] = []
    pending: dict[str, str] | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            info = line.split(",", 1)
            attrs = dict(_ATTR.findall(info[0]))
            label = info[1].strip() if len(info) > 1 else ""
            pending = {
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", "") or label,
                "group": attrs.get("group-title", ""),
                "logo": attrs.get("tvg-logo", ""),
            }
        elif not line.startswith("#") and pending is not None:
            channels.append(Channel(
                tvg_id=pending["tvg_id"],
                name=pending["tvg_name"] or pending["tvg_id"],
                group=pending["group"],
                logo=pending["logo"],
                url=line,
            ))
            pending = None
    return channels


def _when(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _programme(node: ET.Element) -> Programme | None:
    """One `<programme>` element, or None when its times do not parse."""
    start = _when(node.get("start", ""))
    stop = _when(node.get("stop", ""))
    if not start or not stop:
        return None
    return Programme(
        channel_id=node.get("channel", ""),
        title=(node.findtext("title") or "").strip() or "Untitled",
        start=start,
        stop=stop,
        description=(node.findtext("desc") or "").strip(),
        category=(node.findtext("category") or "").strip(),
    )


def _keep(programme: Programme, window: tuple[datetime, datetime] | None) -> bool:
    if window is None:
        return True
    start, stop = window
    return programme.stop > start and programme.start < stop


def _label(node: ET.Element) -> str:
    return (node.findtext("display-name") or "").strip()


def parse_xmltv(text: str,
                window: tuple[datetime, datetime] | None = None
                ) -> tuple[dict[str, str], list[Programme]]:
    """Return (channel id -> display name, programmes). Broken guides are empty.

    `window` keeps only the programmes that overlap it. The whole document is
    still built here, so this is the form for text already in memory (tests, a
    caller holding the bytes); `parse_xmltv_file` is the one that streams.
    """
    names: dict[str, str] = {}
    programmes: list[Programme] = []
    if not (text or "").strip():
        return names, programmes
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return names, programmes

    for node in root.findall("channel"):
        channel_id = node.get("id", "")
        label = _label(node)
        if channel_id:
            names[channel_id] = label or channel_id

    for node in root.findall("programme"):
        programme = _programme(node)
        if programme and _keep(programme, window):
            programmes.append(programme)
    programmes.sort(key=lambda p: p.start)
    return names, programmes


def parse_xmltv_file(path: str | Path,
                     window: tuple[datetime, datetime] | None = None
                     ) -> tuple[dict[str, str], list[Programme]]:
    """Parse a guide from disk without holding the document in memory.

    A real XMLTV file is tens of megabytes and its parsed tree several times
    that, which is enough to take the whole service past its memory limit: on
    2026-10-30 opening Live TV did exactly that, the kernel killed uvicorn, and
    the browser got a 502 instead of a guide. `iterparse` with an element clear
    after each programme keeps the footprint to the programmes that are kept,
    and `window` decides which those are.
    """
    names: dict[str, str] = {}
    programmes: list[Programme] = []
    try:
        for _, node in ET.iterparse(str(path), events=("end",)):
            if node.tag == "channel":
                channel_id = node.get("id", "")
                if channel_id:
                    names[channel_id] = _label(node) or channel_id
                node.clear()
            elif node.tag == "programme":
                programme = _programme(node)
                if programme and _keep(programme, window):
                    programmes.append(programme)
                node.clear()
    except (ET.ParseError, OSError):
        # A truncated download is a guide with fewer programmes, not an outage.
        return names, programmes
    programmes.sort(key=lambda p: p.start)
    return names, programmes


def number_channels(channels: list[Channel], base: int = 2) -> list[Channel]:
    """Stable, deterministic numbering: by group, then name.

    A channel keeps its number as long as its name and group do, regardless of
    playlist order — which is what makes a number in a recording mean something.
    """
    ordered = sorted(channels, key=lambda c: (c.group.lower(), c.name.lower(), c.tvg_id))
    return [
        Channel(
            tvg_id=c.tvg_id, name=c.name, group=c.group, logo=c.logo, url=c.url,
            number=base + index,
        )
        for index, c in enumerate(ordered)
    ]


def guide_for(channel_id: str, programmes: list[Programme],
              at: datetime | None = None, hours: int = 6) -> list[Programme]:
    at = at or datetime.now(timezone.utc)
    horizon = at + timedelta(hours=hours)
    return [
        p for p in programmes
        if p.channel_id == channel_id and p.stop > at and p.start < horizon
    ]


def now_and_next(channel_id: str, programmes: list[Programme],
                 at: datetime | None = None) -> dict[str, Any]:
    at = at or datetime.now(timezone.utc)
    upcoming = [p for p in programmes if p.channel_id == channel_id and p.stop > at]
    now = next((p for p in upcoming if p.start <= at < p.stop), None)
    nxt = next((p for p in upcoming if p.start >= at), None)
    return {
        "now": now.to_dict() if now else None,
        "next": nxt.to_dict() if nxt else None,
        "progress": (
            round((at - now.start).total_seconds() / max((now.stop - now.start).total_seconds(), 1) * 100, 1)
            if now else None
        ),
    }


def lineup(channels: list[Channel], programmes: list[Programme],
           at: datetime | None = None) -> list[dict[str, Any]]:
    """The whole channel list with what is on now — one call for the UI."""
    at = at or datetime.now(timezone.utc)
    return [{**channel.to_dict(), **now_and_next(channel.tvg_id, programmes, at)}
            for channel in channels]


def merge_logos(channels: list[Channel], names: dict[str, str]) -> list[Channel]:
    """Prefer the guide's display name when the playlist had none."""
    return [
        Channel(
            tvg_id=c.tvg_id,
            name=(c.name or names.get(c.tvg_id, "") or c.tvg_id),
            group=c.group, logo=c.logo, url=c.url, number=c.number,
        )
        for c in channels
    ]
