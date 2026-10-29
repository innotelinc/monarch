"""Configuration for the Monarch core service.

Everything is environment-driven so the compose file stays the only place a
deployment is described. The defaults are the ones the live stack uses, so a
container started with no environment still comes up and serves the UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.environ.get("CORE_DATA_DIR", "/data"))
    static_dir: Path = Path(os.environ.get("CORE_STATIC_DIR", "static"))

    # Where the library actually lives, one root per kind. A single root is
    # fine: the scanner classifies by the top-level directory name.
    library_root: Path = Path(os.environ.get("CORE_LIBRARY_ROOT", "/media"))
    completed_root: Path = Path(os.environ.get("CORE_COMPLETED_ROOT", "/downloads/complete"))

    # Transmission speaks JSON-RPC over HTTP. We keep it as the only downloader.
    transmission_url: str = os.environ.get("TRANSMISSION_URL", "http://transmission:9091")
    transmission_user: str = os.environ.get("TRANSMISSION_USER", "")
    transmission_password: str = os.environ.get("TRANSMISSION_PASSWORD", "")
    transmission_category: str = os.environ.get("TRANSMISSION_CATEGORY", "monarch")

    # Live TV: an M3U playlist plus an XMLTV guide, both read natively. This is
    # what replaces TVHeadend, NextPVR, Dispatcharr and autobrr.
    livetv_m3u: str = os.environ.get("LIVETV_M3U_URL", "")
    livetv_xmltv: str = os.environ.get("LIVETV_XMLTV_URL", "")
    livetv_channel_base: int = _int("LIVETV_CHANNEL_BASE", 2)

    # One sign-in for the whole platform: the single oauth2-proxy gateway in
    # front of this service sets this header. Trusting it is only safe because
    # the app is not published directly; see docs/unified.md.
    auth_header: str = os.environ.get("CORE_AUTH_HEADER", "X-Auth-Request-Email")
    auth_disabled: bool = _bool("CORE_AUTH_DISABLED", False)

    # Scheduler cadence, in seconds.
    search_interval: int = _int("CORE_SEARCH_INTERVAL", 900)
    import_interval: int = _int("CORE_IMPORT_INTERVAL", 60)
    scan_interval: int = _int("CORE_SCAN_INTERVAL", 3600)
    livetv_refresh: int = _int("CORE_LIVETV_REFRESH", 3600)
    search_batch: int = _int("CORE_SEARCH_BATCH", 4)
    results_per_indexer: int = _int("CORE_RESULTS_PER_INDEXER", 100)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "core.sqlite3"

    @property
    def media_kinds(self) -> dict[str, str]:
        """Top-level library directory -> item kind."""
        return {
            "movies": "movie",
            "tv": "episode",
            "music": "track",
            "adult": "movie",
        }


settings = Settings()
