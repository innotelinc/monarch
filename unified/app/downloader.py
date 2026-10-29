"""Transmission JSON-RPC client.

Transmission is the one download client the platform keeps. Its RPC needs a
session id handed back on the first 409, which is the only fiddly part; the
rest is four methods (add, get, start, remove) and one status mapping so the
UI can say "downloads" instead of "state 4".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

STATUS = {
    0: "stopped",
    1: "check-wait",
    2: "checking",
    3: "download-wait",
    4: "downloading",
    5: "seed-wait",
    6: "seeding",
}

FIELDS = (
    "id", "name", "hashString", "status", "percentDone", "totalSize",
    "rateDownload", "rateUpload", "eta", "downloadDir", "error",
    "errorString", "labels", "isFinished", "doneDate", "addedDate",
)


class TransmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Torrent:
    id: int
    name: str
    hash: str
    status: str
    percent: float
    size: int
    rate: int
    eta: int
    directory: str
    error: str
    finished: bool

    @property
    def complete(self) -> bool:
        return self.finished or self.percent >= 0.9999

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hash": self.hash,
            "status": self.status,
            "percent": round(self.percent * 100, 1),
            "size": self.size,
            "rate": self.rate,
            "eta": self.eta,
            "directory": self.directory,
            "error": self.error,
            "complete": self.complete,
        }


def _torrent(row: dict[str, Any]) -> Torrent:
    return Torrent(
        id=int(row.get("id", 0)),
        name=str(row.get("name", "")),
        hash=str(row.get("hashString", "")),
        status=STATUS.get(int(row.get("status", 0)), "unknown"),
        percent=float(row.get("percentDone", 0) or 0),
        size=int(row.get("totalSize", 0) or 0),
        rate=int(row.get("rateDownload", 0) or 0),
        eta=int(row.get("eta", 0) or 0),
        directory=str(row.get("downloadDir", "")),
        error=str(row.get("errorString", "") or ""),
        finished=bool(row.get("isFinished", False)),
    )


class Transmission:
    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout: float = 20.0) -> None:
        self.url = url.rstrip("/") + "/transmission/rpc"
        self.auth = (username, password) if username else None
        self.timeout = timeout
        self.session_id = ""

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-Transmission-Session-Id": self.session_id}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.url, json=payload, headers=headers, auth=self.auth)
                if response.status_code == 409:
                    self.session_id = response.headers.get("X-Transmission-Session-Id", "")
                    headers["X-Transmission-Session-Id"] = self.session_id
                    response = client.post(self.url, json=payload, headers=headers, auth=self.auth)
                if response.status_code != 200:
                    raise TransmissionError(f"rpc returned {response.status_code}")
                body = response.json()
        except httpx.HTTPError as exc:
            # Every caller handles TransmissionError; an unreachable downloader
            # is a normal condition (restart, network) and must not surface as a
            # 500 from an API route.
            raise TransmissionError(f"unreachable: {exc}") from exc
        if body.get("result") != "success":
            raise TransmissionError(str(body.get("result") or "rpc failed"))
        return body.get("arguments", {}) or {}

    def add(self, url: str, *, download_dir: str | None = None,
            paused: bool = False, labels: list[str] | None = None) -> dict[str, Any]:
        """Add a magnet or .torrent URL. Idempotent: a known hash is a no-op."""
        payload: dict[str, Any] = {"filename": url, "paused": paused}
        if download_dir:
            payload["download-dir"] = download_dir
        if labels:
            payload["labels"] = labels
        args = self._post({"method": "torrent-add", "arguments": payload})
        added = args.get("torrent-added") or args.get("torrent-duplicate") or {}
        return {"id": added.get("id"), "name": added.get("name", ""),
                "hash": added.get("hashString", ""),
                "duplicate": "torrent-duplicate" in args}

    def list(self, ids: list[int] | None = None) -> list[Torrent]:
        payload: dict[str, Any] = {"fields": list(FIELDS)}
        if ids:
            payload["ids"] = ids
        args = self._post({"method": "torrent-get", "arguments": payload})
        return [_torrent(row) for row in args.get("torrents", [])]

    def completed(self) -> list[Torrent]:
        return [t for t in self.list() if t.complete]

    def remove(self, torrent_id: int, delete_data: bool = False) -> None:
        self._post({
            "method": "torrent-remove",
            "arguments": {"ids": [torrent_id], "delete-local-data": delete_data},
        })

    def status(self) -> dict[str, Any]:
        """A summary the UI can render without listing every torrent."""
        try:
            torrents = self.list()
        except (TransmissionError, httpx.HTTPError, OSError) as exc:
            return {"reachable": False, "error": str(exc), "count": 0, "rate": 0}
        return {
            "reachable": True,
            "count": len(torrents),
            "downloading": sum(1 for t in torrents if t.status == "downloading"),
            "complete": sum(1 for t in torrents if t.complete),
            "rate": sum(t.rate for t in torrents),
        }
