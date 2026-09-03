"""
monarch-health - media health analytics for the Monarch platform.

Periodically scans the Jellyfin library + media volume and produces a health
report consumed by dashboards/APIs:

  * per-library stats (item counts by type, paths)
  * missing files  - items whose media file no longer exists on disk
  * duplicates     - files sharing the same name inside a library
  * orphans        - media files on disk not registered in Jellyfin
  * disk usage     - capacity / used / free for the media volume
  * most played    - top titles by play count (UserData)
  * recently added - newest items by DateCreated
  * service status - which Monarch/Jellyfin endpoints answer

The report is written to REPORT_FILE (JSON) after every scan and served back
through the API, so other components can read a consistent snapshot.

Jellyfin access uses the admin token exported by monarch-init on first boot
(/docker/appdata/init/jellyfin-api-key.txt), falling back to JELLYFIN_API_KEY.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
KEY_FILE = os.environ.get("JELLYFIN_API_KEY_FILE", "").strip()
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/data/media").rstrip("/")
REPORT_FILE = os.environ.get("REPORT_FILE", "/app/data/analytics.json")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "3600"))

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv",
                    ".ts", ".mpg", ".mpeg", ".flv", ".iso", ".m2ts"}
SERVICE_ENDPOINTS = {
    "jellyfin": (f"{JELLYFIN_URL}/System/Info/Public", "GET"),
    "monarch-recs": ("http://monarch-recs:8002/health", "GET"),
    "monarch-health": ("http://monarch-health:8003/health", "GET"),
    "authentik": ("http://authentik-server:9000/-/health/ready/", "GET"),
}


def _api_key() -> str:
    if API_KEY:
        return API_KEY
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    return ""


def _headers() -> dict:
    return {"X-Emby-Token": _api_key(), "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Jellyfin client
# ---------------------------------------------------------------------------


class Jellyfin:
    def __init__(self):
        # API key is read per request so it picks up the token monarch-init
        # exports on first boot even if this service started earlier.
        self._client = httpx.Client(base_url=JELLYFIN_URL, timeout=30)

    def get(self, path: str, **params):
        params["api_key"] = _api_key()
        try:
            r = self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"Jellyfin request failed ({path}): {exc}")

    def library_folders(self) -> list[dict]:
        return self.get("/Library/VirtualFolders")

    def items(self, item_types: tuple[str, ...], limit: int = 500) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            data = self.get(
                "/Items",
                Recursive="true",
                IncludeItemTypes=",".join(item_types),
                Fields="Path,DateCreated,ProductionYear",
                StartIndex=start, Limit=limit,
            )
            page = (data or {}).get("Items") or []
            items.extend(page)
            total = (data or {}).get("TotalRecordCount") or 0
            start += len(page)
            if not page or start >= total:
                break
        return items


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

STATE = {
    "report": None,
    "last_scan": 0.0,
    "scanning": False,
    "last_error": None,
}
LOCK = threading.Lock()

app = FastAPI(title="Monarch Media Health", version="1.0.0")
jf = Jellyfin()


def _library_of(path: str, folders: list[dict]) -> str:
    for folder in folders:
        for root in (folder.get("Locations") or []):
            if root and path.startswith(root.rstrip("/") + "/"):
                return folder.get("Name") or os.path.basename(root)
    return "Unknown"


def _walk_media_files(root: str) -> list[str]:
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS:
                found.append(os.path.join(dirpath, name))
    return found


def scan() -> dict:
    """Run one full health scan and return the report."""
    if not _api_key():
        raise HTTPException(status_code=503,
                            detail="Jellyfin API key not available yet "
                                   "(monarch-init exports it on first boot)")

    start = time.time()
    folders = jf.library_folders()

    item_types = ("Movie", "Series", "Episode", "Video", "MusicVideo", "Audio")
    items = jf.items(item_types)

    # ---- per-library + per-type stats, missing, duplicates, recency ----
    libs: dict[str, dict] = {}
    missing: list[dict] = []
    by_basename: dict[str, list[dict]] = {}
    recently_added: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    played: list[dict] = []

    for it in items:
        path = it.get("Path") or ""
        lib = _library_of(path, folders)
        lib_stats = libs.setdefault(lib, {"name": lib, "count": 0, "types": {}})
        lib_stats["count"] += 1
        itype = it.get("Type") or "Other"
        lib_stats["types"][itype] = lib_stats["types"].get(itype, 0) + 1

        entry = {
            "id": it.get("Id"),
            "name": it.get("Name"),
            "type": itype,
            "year": it.get("ProductionYear"),
            "path": path,
        }
        if path:
            if not os.path.exists(path):
                missing.append(entry)
            base = os.path.basename(path).lower()
            by_basename.setdefault(base, []).append(entry)

        created = it.get("DateCreated")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt >= cutoff:
                    recently_added.append(entry)
            except ValueError:
                pass

        ud = it.get("UserData") or {}
        if ud.get("PlayCount"):
            entry["play_count"] = ud["PlayCount"]
            played.append(entry)

    duplicates = [{"name": k, "count": len(v), "items": v}
                  for k, v in by_basename.items() if len(v) > 1]
    recently_added.sort(key=lambda e: e.get("name", ""))
    played.sort(key=lambda e: e.get("play_count", 0), reverse=True)

    # ---- orphans + disk usage on the media volume ----
    media_files = _walk_media_files(MEDIA_ROOT)
    known_paths = {it.get("Path") for it in items if it.get("Path")}
    orphans = [p for p in media_files if p not in known_paths]

    try:
        usage = shutil.disk_usage(MEDIA_ROOT)
        disk = {"path": MEDIA_ROOT,
                "total_gb": round(usage.total / 1e9, 1),
                "used_gb": round(usage.used / 1e9, 1),
                "free_gb": round(usage.free / 1e9, 1),
                "percent_used": round(usage.used / usage.total * 100, 1)}
    except OSError as exc:
        disk = {"path": MEDIA_ROOT, "error": str(exc)}

    # ---- service status ----
    services = {}
    for name, (url, method) in SERVICE_ENDPOINTS.items():
        try:
            r = httpx.request(method, url, timeout=8,
                              headers=_headers() if "jellyfin" in name else {})
            services[name] = {"up": r.status_code < 500, "status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            services[name] = {"up": False, "error": str(exc)}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_duration_seconds": round(time.time() - start, 2),
        "libraries": sorted(libs.values(), key=lambda l: -l["count"]),
        "totals": {
            "items": len(items),
            "missing_files": len(missing),
            "duplicates": len(duplicates),
            "orphan_files": len(orphans),
        },
        "missing": missing[:200],
        "duplicates": duplicates[:100],
        "orphans_sample": orphans[:100],
        "orphan_paths": orphans,  # full list (filesystem scan)
        "recently_added_30d": recently_added[:100],
        "most_played": played[:50],
        "disk": disk,
        "media_root": MEDIA_ROOT,
        "services": services,
    }
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    tmp = REPORT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    os.replace(tmp, REPORT_FILE)
    return report


def _run_scan_loop() -> None:
    while True:
        try:
            report = scan()
            with LOCK:
                STATE["report"] = report
                STATE["last_scan"] = time.time()
                STATE["last_error"] = None
            print(f"[monarch-health] scan complete: {report['totals']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            with LOCK:
                STATE["last_error"] = str(exc)
            print(f"[monarch-health] WARNING: scan failed: {exc}", flush=True)
        time.sleep(SCAN_INTERVAL)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    with LOCK:
        return {
            "status": "ok",
            "jellyfin_key": bool(_api_key()),
            "last_scan": STATE["last_scan"],
            "last_error": STATE["last_error"],
        }


@app.get("/api/analytics")
def analytics() -> dict:
    """Latest report (from the periodic scan)."""
    with LOCK:
        report = STATE["report"]
        last = STATE["last_scan"]
    if report is None:
        raise HTTPException(status_code=503,
                            detail="No report yet - the first scan is running "
                                   "(see /api/analytics/scan to force one)")
    return {"last_scan": last, "report": report}


@app.post("/api/analytics/scan")
def force_scan() -> dict:
    """Run a scan on demand and return the fresh report."""
    with LOCK:
        if STATE["scanning"]:
            raise HTTPException(status_code=409, detail="A scan is already running")
        STATE["scanning"] = True
    try:
        report = scan()
    finally:
        with LOCK:
            STATE["scanning"] = False
    with LOCK:
        STATE["report"] = report
        STATE["last_scan"] = time.time()
        STATE["last_error"] = None
    return {"last_scan": STATE["last_scan"], "report": report}


@app.get("/api/services")
def services() -> dict:
    """Reachability of the platform's core services."""
    status = {}
    for name, (url, method) in SERVICE_ENDPOINTS.items():
        try:
            r = httpx.request(method, url, timeout=8,
                              headers=_headers() if "jellyfin" in name else {})
            status[name] = {"up": r.status_code < 500, "status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            status[name] = {"up": False, "error": str(exc)}
    return {"services": status}


threading.Thread(target=_run_scan_loop, daemon=True).start()