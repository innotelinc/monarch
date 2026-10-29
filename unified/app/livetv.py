"""Live TV without a TV backend.

TVHeadend, NextPVR, Dispatcharr and autobrr each existed to turn a playlist
and a guide into something a player could use. This module does that directly:
an M3U playlist is the tuner, an XMLTV document is the guide, and channels are
numbered deterministically so a number always means the same channel.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


def parse_xmltv(text: str) -> tuple[dict[str, str], list[Programme]]:
    """Return (channel id -> display name, programmes). Broken guides are empty."""
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
        label = (node.findtext("display-name") or "").strip()
        if channel_id:
            names[channel_id] = label or channel_id

    for node in root.findall("programme"):
        start = _when(node.get("start", ""))
        stop = _when(node.get("stop", ""))
        if not start or not stop:
            continue
        programmes.append(Programme(
            channel_id=node.get("channel", ""),
            title=(node.findtext("title") or "").strip() or "Untitled",
            start=start,
            stop=stop,
            description=(node.findtext("desc") or "").strip(),
            category=(node.findtext("category") or "").strip(),
        ))
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
