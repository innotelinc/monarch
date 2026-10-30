"""Jellyfin — the one service the merge keeps.

Playback and transcoding are not duplication, so Jellyfin stays. What it never
had was a reason to notice a new file the moment it arrived: the old stack
refreshed it by hand, or not at all until the next scheduled scan. Two calls fix
that, and both are cheap:

  * `POST /Library/Media/Updated` tells Jellyfin exactly which paths appeared;
  * `POST /Library/Refresh` covers the album folder or nfo that came with them.

Failures are reported, never raised: an unreachable Jellyfin must not turn a
good import into a failed one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger("core.jellyfin")


class Jellyfin:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def headers(self) -> dict[str, str]:
        # `X-Emby-Token` is answered with 401 by Jellyfin 12 on every one of these
        # routes (measured 2026-10-30): the service was reporting itself
        # unreachable and, worse, every "tell Jellyfin a file arrived" was a
        # silent 401. The MediaBrowser form is the header this server accepts.
        return {"Authorization": f"MediaBrowser Token={self.api_key}",
                "Accept": "application/json"}

    def info(self) -> dict[str, Any]:
        """Name and version, for the Settings card. Never raises."""
        if not self.configured:
            return {"configured": False, "reachable": False}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.base_url}/System/Info", headers=self.headers)
            if response.status_code != 200:
                return {"configured": True, "reachable": False,
                        "error": f"http {response.status_code}"}
            body = response.json() or {}
            return {"configured": True, "reachable": True,
                    "server": body.get("ServerName"), "version": body.get("Version")}
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return {"configured": True, "reachable": False, "error": str(exc)}

    def refresh(self, paths: Iterable[Path | str] = (), full: bool = True) -> dict[str, Any]:
        """Tell Jellyfin what changed. Returns a report the scheduler records."""
        if not self.configured:
            return {"configured": False, "updated": 0, "refreshed": False}
        updates = [{"Path": str(Path(path).parent), "UpdateType": "Created"}
                   for path in paths]
        updated = 0
        with httpx.Client(timeout=self.timeout) as client:
            if updates:
                try:
                    response = client.post(f"{self.base_url}/Library/Media/Updated",
                                           headers=self.headers, json={"Updates": updates})
                    if response.status_code in (200, 204):
                        updated = len(updates)
                    else:
                        log.warning("jellyfin media-updated returned %s", response.status_code)
                except (httpx.HTTPError, OSError) as exc:
                    log.warning("jellyfin media-updated failed: %s", exc)
            refreshed = False
            if full or updated:
                try:
                    response = client.post(f"{self.base_url}/Library/Refresh",
                                           headers=self.headers)
                    refreshed = response.status_code in (200, 204)
                    if not refreshed:
                        log.warning("jellyfin library refresh returned %s", response.status_code)
                except (httpx.HTTPError, OSError) as exc:
                    log.warning("jellyfin library refresh failed: %s", exc)
        return {"configured": True, "updated": updated, "refreshed": refreshed}

    def index(self) -> dict[str, dict[str, Any]]:
        """What Jellyfin knows, keyed by file path.

        The library UI needs a poster per catalogued file, and Jellyfin is the
        only thing that has them (it fetches artwork; the library directories
        hold none). One request answers for every item, and the answer is cached
        by the caller, so a grid of 120 cards is still one call.
        """
        if not self.configured:
            return {}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    f"{self.base_url}/Items", headers=self.headers,
                    params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode,Audio",
                            "Fields": "Path,ImageTags", "Limit": 20000},
                )
            response.raise_for_status()
            rows = (response.json() or {}).get("Items") or []
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("jellyfin index failed: %s", exc)
            return {}
        index: dict[str, dict[str, Any]] = {}
        for row in rows:
            path = row.get("Path") or ""
            if not path:
                continue
            index[path] = {
                "id": row.get("Id") or "",
                "type": row.get("Type") or "",
                "series": row.get("SeriesId") or "",
                "album": row.get("AlbumId") or "",
                "art": bool(row.get("ImageTags")),
            }
        return index

    def image(self, item_id: str, max_height: int = 420) -> tuple[bytes, str] | None:
        """The primary image for one Jellyfin item, or None when it has none."""
        if not self.configured or not item_id:
            return None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    f"{self.base_url}/Items/{item_id}/Images/Primary",
                    headers=self.headers, params={"maxHeight": max_height, "quality": 82},
                )
        except (httpx.HTTPError, OSError) as exc:
            log.warning("jellyfin image failed: %s", exc)
            return None
        if response.status_code != 200 or not response.content:
            return None
        return response.content, response.headers.get("content-type", "image/jpeg")
