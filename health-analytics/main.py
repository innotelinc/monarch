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

import hashlib
import json
import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
KEY_FILE = os.environ.get("JELLYFIN_API_KEY_FILE", "").strip()
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/data/media").rstrip("/")
REPORT_FILE = os.environ.get("REPORT_FILE", "/app/data/analytics.json")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "3600"))

# The splash the media host serves, and what the drift check last judged of it.
# All three paths are mounted read-only by docker-compose.yml: Jellyfin's config
# (the branding store), its data dir (the collage a scan rewrites), and the state
# directory `drift-check.sh` writes its splash/guard verdict into.
#
# This is a container, so it cannot ask systemd anything. The *unit's* state comes
# from that verdict, and the *effect* the unit is supposed to have comes from the
# files - which is the half that matters, because a unit can be active while the
# data directory it watches has moved.
SPLASH_STATUS_FILE = os.environ.get("SPLASH_STATUS_FILE", "/state/splash-status")
JELLYFIN_CONFIG_DIR = os.environ.get("JELLYFIN_CONFIG_DIR", "/jellyfin")
JELLYFIN_DATA_DIR = os.environ.get("JELLYFIN_DATA_DIR", "/jellyfin/data/data")
# `SplashscreenLocation` is recorded as the path *Jellyfin* sees; this is the
# appdata mount it resolves through, so the same string can be read here.
JELLYFIN_CONTAINER_CONFIG = os.environ.get("JELLYFIN_CONTAINER_CONFIG", "/config")

# `scripts/livetv-monitor.py` writes this next to its database after every run
# (monarch-livetv-monitor.timer, every 30 minutes). The board reads it through
# /livetv/health so a dead feed is a red tile rather than a stale guide.
LIVETV_STATUS_FILE = os.environ.get("LIVETV_STATUS_FILE", "/var/lib/monarch/livetv-monitor.json")
# Eight monitor ticks. The monitor marks a feed down only after two consecutive
# failures, so its own thresholds are the interesting ones; this only catches a
# timer that has stopped, which is otherwise indistinguishable from "all fine".
LIVETV_MAX_AGE = int(os.environ.get("LIVETV_MAX_AGE", "14400"))

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
    # `Authorization: MediaBrowser Token=` is the only spelling this Jellyfin
    # accepts; X-Emby-Token and ?api_key= both answer 401.
    return {"Authorization": f"MediaBrowser Token={_api_key()}",
            "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Jellyfin client
# ---------------------------------------------------------------------------


class Jellyfin:
    def __init__(self):
        # API key is read per request so it picks up the token monarch-init
        # exports on first boot even if this service started earlier.
        self._client = httpx.Client(base_url=JELLYFIN_URL, timeout=30)

    def get(self, path: str, **params):
        # The token travels in a header. Jellyfin rejects `?api_key=` outright
        # now, which turned every library scan into a 401.
        try:
            r = self._client.get(path, params=params, headers=_headers())
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


# ---------------------------------------------------------------------------
# The splash guard
# ---------------------------------------------------------------------------


def _kv_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _age_seconds(stamp) -> float | None:
    """Seconds since an ISO-8601 `finished_at`, or None if it cannot be read."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _same_bytes(left: str, right: str) -> bool:
    def digest(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    try:
        return digest(left) == digest(right)
    except OSError:
        return False


def splash_health() -> dict:
    """Whether a client falling back to the generated collage gets the design.

    Two halves, because they fail independently:

      * the **file**: `{data}/splashscreen.png` - the one
        `SplashscreenPostScanTask` rewrites after every scan - still carries the
        installed splash. That is the guard's effect, and it is checkable here.
      * the **guard**: whether the path unit that restores it is installed,
        enabled and active. Only systemd knows; the answer is whatever
        `drift-check.sh` last wrote, and a missing verdict is reported as
        unknown rather than healthy - a guard nobody has checked is not proven.
    """
    reasons: list[str] = []
    result: dict = {"ok": False, "guard": "unknown", "splash": "unknown",
                    "checked": None, "installed": None, "fallback": None}

    verdict = _kv_file(SPLASH_STATUS_FILE)
    if verdict:
        result["guard"] = verdict.get("guard", "unknown")
        result["splash"] = verdict.get("splash", "unknown")
        result["checked"] = verdict.get("checked")
        for key in ("splash", "guard"):
            if result[key] == "fail":
                note = verdict.get(f"{key}_note") or f"drift-check reports the {key} failing"
                reasons.append(note)
    else:
        reasons.append(f"no drift-check verdict at {SPLASH_STATUS_FILE} - the splash "
                       "guard has not been judged on this host")

    branding_path = os.path.join(JELLYFIN_CONFIG_DIR, "branding.xml")
    location, enabled = "", False
    try:
        root = ET.parse(branding_path).getroot()
        enabled = (root.findtext("SplashscreenEnabled") or "").strip().lower() == "true"
        location = (root.findtext("SplashscreenLocation") or "").strip()
    except (OSError, ET.ParseError):
        reasons.append(f"cannot read {branding_path}")

    installed = ""
    mount = JELLYFIN_CONTAINER_CONFIG.rstrip("/") + "/"
    if location.startswith(mount):
        installed = os.path.join(JELLYFIN_CONFIG_DIR, location[len(mount):])
    elif location:
        installed = location
    result["installed"] = os.path.basename(installed) or None

    if not enabled:
        reasons.append(f"{branding_path} has SplashscreenEnabled=false - the splash is "
                       "switched off")
    if not installed:
        reasons.append(f"{branding_path} names no SplashscreenLocation - Jellyfin serves "
                       "the image its own post-scan task generates")
    elif not os.path.isfile(installed):
        reasons.append(f"the recorded splash {location} is not at {installed} - Jellyfin does "
                       "not report that, it serves the collage instead")

    fallback = os.path.join(JELLYFIN_DATA_DIR, "splashscreen.png")
    result["fallback"] = fallback
    if not os.path.isfile(fallback):
        reasons.append(f"{fallback} is missing - there is no fallback image at all")
    elif installed and os.path.isfile(installed) and not _same_bytes(fallback, installed):
        reasons.append(f"{fallback} is not the installed splash - a library scan rewrote it "
                       "and nothing has put the design back")

    result["reasons"] = reasons
    result["ok"] = not reasons
    return result


@app.get("/splash/health")
def splash_health_route():
    """200 while the fallback image is the design and the guard is judged healthy.

    Homarr's board reads exactly this for the status dot on the Splash (live)
    tile, so both halves of the failure are visible on the dashboard rather than
    only in `drift-check.sh`: red means a client falling back to the collage is
    being shown a poster wall, or the unit that restores it was found broken.
    """
    result = splash_health()
    return JSONResponse(status_code=200 if result["ok"] else 503, content=result)


@app.get("/livetv/health")
def livetv_health_route():
    """200 while no guide feed is marked down and the monitor is recent.

    The monitor's own rule is the one that matters: a feed has to fail twice in a
    row before it is called dead, so one timeout does not paint the board red. What
    this adds is the failure the monitor cannot report about itself - a timer that
    stopped - which otherwise looks exactly like a healthy guide forever.
    """
    reasons: list[str] = []
    result: dict = {"ok": False, "down": [], "down_count": None, "checked": None,
                    "rows": None, "ok_probes": None, "failed_probes": None}
    try:
        with open(LIVETV_STATUS_FILE, "r", encoding="utf-8") as fh:
            summary = json.load(fh)
    except OSError:
        reasons.append(f"no monitor report at {LIVETV_STATUS_FILE} - the live TV monitor "
                       "has not run on this host")
        summary = None
    except ValueError as error:
        reasons.append(f"{LIVETV_STATUS_FILE} is not JSON ({error})")
        summary = None

    if summary is not None:
        down = summary.get("down") or []
        result.update({
            "down": [{"number": item.get("number"), "name": item.get("name")}
                     for item in down[:20]],
            "down_count": summary.get("down_count"),
            "checked": summary.get("finished_at"),
            "rows": summary.get("rows"),
            "ok_probes": summary.get("ok"),
            "failed_probes": summary.get("failed"),
        })
        if down or summary.get("down_count"):
            names = ", ".join(f"{item.get('number')} {item.get('name')}" for item in down[:5])
            reasons.append(f"{summary.get('down_count') or len(down)} feed(s) marked down"
                           + (f" ({names})" if names else ""))
        age = _age_seconds(summary.get("finished_at"))
        if age is None:
            reasons.append("the monitor report has no usable finished_at - cannot tell how "
                           "old it is")
        elif age > LIVETV_MAX_AGE:
            reasons.append(f"the last monitor run finished {int(age)}s ago "
                           f"(> {LIVETV_MAX_AGE}s) - the timer is not running")

    result["reasons"] = reasons
    result["ok"] = not reasons
    return JSONResponse(status_code=200 if result["ok"] else 503, content=result)


threading.Thread(target=_run_scan_loop, daemon=True).start()