"""Indexer search.

Prowlarr used to sit between us and every tracker purely to normalise results
and to fan a query out. This module does both: it speaks Torznab/Newznab
directly, parses the RSS into one result shape, and merges answers from every
enabled indexer, collapsing duplicates by infohash before the decision engine
ever sees them.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

TORZNAB = "{http://torznab.com/schemas/2015/feed}"
NEWZNAB = "{http://newznab.com/v0.0}"

CATEGORY_VIDEO = "2000"
CATEGORY_MOVIE = "2000,2010,2030,2040,2045,2050,2060,2070,2080"
CATEGORY_TV = "5000,5030,5040,5045,5050,5060,5070,5080"
CATEGORY_MUSIC = "3000,3010,3020,3030,3040"


@dataclass(frozen=True)
class IndexerResult:
    indexer: str
    title: str
    guid: str
    download_url: str = ""
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    infohash: str = ""
    published: str = ""
    category: str = ""

    @property
    def key(self) -> str:
        """Identity used to collapse the same release from two indexers."""
        if self.infohash:
            return self.infohash.lower()
        slug = re.sub(r"[^a-z0-9]+", "", self.title.lower())
        return f"{slug}:{self.size}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "indexer": self.indexer,
            "title": self.title,
            "guid": self.guid,
            "download_url": self.download_url,
            "size": self.size,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "infohash": self.infohash,
            "published": self.published,
            "category": self.category,
        }


# Prowlarr is the one *arr service worth keeping while its indexers are worth
# having: it is the only thing that speaks to trackers with no Torznab of their
# own. Core can read its search API directly, so one row replaces the per-site
# proxying and the results still get core's ranking and dedupe.
PROTOCOL_TTL = 300
_protocol_cache: dict[str, tuple[float, dict[str, str]]] = {}


@dataclass(frozen=True)
class Indexer:
    name: str
    url: str
    api_key: str = ""
    kind: str = "torznab"
    cats: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "url": self.url, "kind": self.kind,
            "cats": self.cats, "enabled": self.enabled,
            # The key is never returned to the browser.
            "has_key": bool(self.api_key),
        }


def _attr(item: ET.Element, name: str) -> str:
    for ns in (TORZNAB, NEWZNAB):
        for node in item.findall(f"{ns}attr"):
            if node.get("name") == name:
                return (node.get("value") or "").strip()
    return ""


def _int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_feed(payload: bytes, indexer: str) -> list[IndexerResult]:
    """Parse a Torznab/Newznab RSS answer into results."""
    results: list[IndexerResult] = []
    if not payload:
        return results
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return results

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        guid = (item.findtext("guid") or item.findtext("link") or title).strip()
        enclosure = item.find("enclosure")
        download_url = ""
        size = 0
        if enclosure is not None:
            download_url = (enclosure.get("url") or "").strip()
            size = _int(enclosure.get("length") or "0")
        link = (item.findtext("link") or "").strip()
        if not download_url:
            download_url = link
        if not size:
            size = _int(item.findtext("size") or "")

        results.append(IndexerResult(
            indexer=indexer,
            title=title,
            guid=guid,
            download_url=download_url or _attr(item, "magneturl"),
            size=size,
            seeders=_int(_attr(item, "seeders")),
            leechers=_int(_attr(item, "peers") or _attr(item, "leechers")),
            infohash=_attr(item, "infohash"),
            published=(item.findtext("pubDate") or "").strip(),
            category=_attr(item, "category"),
        ))
    return results


def search_url(indexer: Indexer, query: str, *, limit: int = 100,
               categories: str = "") -> str:
    base = indexer.url.rstrip("/")
    params = {
        "t": "search",
        "q": query,
        "limit": str(limit),
        "extended": "1",
    }
    if indexer.api_key:
        params["apikey"] = indexer.api_key
    cats = categories or indexer.cats
    if cats:
        params["cat"] = cats
    return f"{base}/api?{urllib.parse.urlencode(params)}"


def prowlarr_protocols(indexer: Indexer, client: httpx.Client,
                       timeout: float) -> dict[str, str]:
    """indexerId -> 'torrent' | 'usenet', so usenet results are never offered."""
    import time

    key = f"{indexer.url}|{indexer.api_key}"
    cached = _protocol_cache.get(key)
    if cached and time.monotonic() - cached[0] < PROTOCOL_TTL:
        return cached[1]
    mapping: dict[str, str] = {}
    try:
        response = client.get(f"{indexer.url.rstrip('/')}/api/v1/indexer",
                              params={"apikey": indexer.api_key}, timeout=timeout)
        if response.status_code == 200:
            for row in response.json() or []:
                mapping[str(row.get("id"))] = str(row.get("protocol") or "").lower()
    except (httpx.HTTPError, OSError, ValueError):
        mapping = {}
    _protocol_cache[key] = (time.monotonic(), mapping)
    return mapping


def search_prowlarr(indexer: Indexer, query: str, *, limit: int = 100,
                    categories: str = "", timeout: float = 30.0,
                    client: httpx.Client) -> list[IndexerResult]:
    """Prowlarr's aggregated search: every indexer it knows, in one call."""
    # Prowlarr wants categories as repeated parameters: a comma-separated value
    # is a 400 (measured against a live Prowlarr, not assumed).
    # A live Prowlarr fans out to every tracker it knows: measured at ~40 s for
    # fifty results and ~60 s for a hundred, against ~40 indexers. Asking for
    # more than fifty buys nothing (core dedupes and shows the best sixty) and
    # costs twenty seconds, so the aggregate query is capped. Its timeout is its
    # own, well above the per-indexer default.
    limit = min(limit, 50)
    params: list[tuple[str, str]] = [
        ("query", query), ("type", "search"), ("limit", str(limit)),
        ("apikey", indexer.api_key),
    ]
    for category in (c.strip() for c in categories.split(",")):
        if category:
            params.append(("categories", category))
    response = client.get(f"{indexer.url.rstrip('/')}/api/v1/search",
                          params=params, timeout=max(timeout, 150.0))
    if response.status_code != 200:
        return []
    protocols = prowlarr_protocols(indexer, client, timeout)
    base = indexer.url.rstrip("/")
    results: list[IndexerResult] = []
    for row in response.json() or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        protocol = protocols.get(str(row.get("indexerId"))) or str(row.get("protocol") or "").lower()
        if protocol == "usenet":
            continue  # Transmission is the only client; an NZB cannot be pinned
        download = str(row.get("downloadUrl") or "").strip()
        if download.startswith("/"):
            download = base + download
        results.append(IndexerResult(
            indexer=str(row.get("indexer") or indexer.name),
            title=title,
            guid=str(row.get("guid") or download or title),
            download_url=download or str(row.get("magnetUrl") or ""),
            size=_int(str(row.get("size") or 0)),
            seeders=_int(str(row.get("seeders") or 0)),
            leechers=_int(str(row.get("leechers") or 0)),
            infohash=str(row.get("infoHash") or "")
            or str(row.get("magnetUrl") or "").split("btih:")[-1][:40],
            published=str(row.get("publishDate") or ""),
        ))
    return results


def search(indexer: Indexer, query: str, *, limit: int = 100,
           categories: str = "", timeout: float = 30.0,
           client: httpx.Client | None = None) -> list[IndexerResult]:
    """Ask one indexer. A broken indexer yields nothing, never an exception."""
    if not indexer.enabled or not indexer.url:
        return []
    own_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        if indexer.kind == "prowlarr":
            return search_prowlarr(indexer, query, limit=limit, categories=categories,
                                   timeout=timeout, client=client)
        response = client.get(search_url(indexer, query, limit=limit, categories=categories))
        if response.status_code != 200:
            return []
        return parse_feed(response.content, indexer.name)
    except (httpx.HTTPError, OSError, ValueError):
        return []
    finally:
        if own_client:
            client.close()


def search_all(indexers: Iterable[Indexer], query: str, *, limit: int = 100,
               categories: str = "", timeout: float = 30.0,
               workers: int = 8) -> list[IndexerResult]:
    """Query every enabled indexer concurrently and collapse duplicates.

    The collapse is the dedupe: the same torrent from three trackers becomes
    one candidate, and the copy with the most seeders is the one kept.
    """
    enabled = [i for i in indexers if i.enabled and i.url]
    if not enabled:
        return []

    collected: list[IndexerResult] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=min(workers, len(enabled))) as pool:
            futures = [
                pool.submit(search, indexer, query, limit=limit,
                            categories=categories, timeout=timeout, client=client)
                for indexer in enabled
            ]
            for future in futures:
                collected.extend(future.result() or [])

    best: dict[str, IndexerResult] = {}
    for result in collected:
        current = best.get(result.key)
        if current is None or result.seeders > current.seeders:
            best[result.key] = result
    return sorted(best.values(), key=lambda r: (r.seeders, r.size), reverse=True)


def looks_fresh(published: str, days: int = 4000) -> bool:
    """Cheap sanity check on a pubDate; unparseable dates are not rejected."""
    if not published:
        return True
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            when = datetime.strptime(published.strip(), fmt)
            return (datetime.now(timezone.utc) - when).days <= days
        except ValueError:
            continue
    return True
