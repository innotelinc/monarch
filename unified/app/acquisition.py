"""The acquisition engine.

One loop replaces Sonarr, Radarr, Lidarr, Whisparr and Prowlarr:

    wanted row -> search every indexer -> parse names -> score against the
    profile -> drop what the catalog already has -> grab the best -> poll the
    downloader -> link the finished file into the library -> mark imported

Every step is idempotent. A grab is unique per (wanted, release) in the
database, so a restart cannot double-download; an import is keyed on the
library fingerprint, so a re-run cannot double-copy.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db, indexers as indexer_mod, library, livetv, scoring
from . import jellyfin as jellyfin_mod, subtitles
from .downloader import Transmission, TransmissionError
from .parser import Release, matches, parse
from .settings import Settings

log = logging.getLogger("core.acquisition")

CATEGORIES = {"movie": indexer_mod.CATEGORY_MOVIE,
              "episode": indexer_mod.CATEGORY_TV,
              "track": indexer_mod.CATEGORY_MUSIC}


@dataclass
class GrabPlan:
    wanted_id: int
    release: Release
    result: indexer_mod.IndexerResult
    score: int
    reasons: list[str]


def indexers_from_db(conn) -> list[indexer_mod.Indexer]:
    rows = db.query(conn, "SELECT * FROM indexers WHERE enabled = 1 ORDER BY name")
    return [
        indexer_mod.Indexer(
            name=row["name"], url=row["url"], api_key=row["api_key"],
            kind=row["kind"], cats=row["cats"], enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


def latest_quality(conn, kind: str, title: str, year: int | None,
                   season: int | None, episode: int | None) -> str | None:
    """The quality the catalog already holds for this exact thing, if any."""
    row = db.one(
        conn,
        "SELECT quality FROM items WHERE kind = ? AND lower(title) = lower(?) "
        "AND ifnull(year,0) = ifnull(?,0) AND ifnull(season,-1) = ifnull(?,-1) "
        "AND ifnull(episode,-1) = ifnull(?,-1) ORDER BY id DESC LIMIT 1",
        (kind, title, year, season, episode),
    )
    return row["quality"] if row else None


def release_query(kind: str, title: str, year: int | None, season: int | None,
                  episode: int | None) -> str:
    """The search string: exactly what Sonarr/Radarr would send."""
    parts = [title]
    if year:
        parts.append(str(year))
    if kind == "episode" and season is not None:
        parts.append(f"S{season:02d}" + (f"E{episode:02d}" if episode is not None else ""))
    return " ".join(parts)


def plan(conn, wanted_row, indexer_list, profile: dict, settings: Settings,
         search: Callable[..., list] | None = None) -> GrabPlan | None:
    """Search and decide for one wanted row. Returns the grab to make, or None.

    The dedupe here is the load-bearing part: a release already grabbed for
    this wanted row (even failed) is not grabbed again, and a release that is
    not better than what the catalog holds is skipped.
    """
    search = search or indexer_mod.search_all
    kind = wanted_row["kind"]
    title = wanted_row["title"]
    year = wanted_row["year"]
    season = wanted_row["season"]
    episode = wanted_row["episode"]

    results = search(
        indexer_list,
        release_query(kind, title, year, season, episode),
        limit=settings.results_per_indexer,
        categories=CATEGORIES.get(kind, ""),
    )
    if not results:
        return None

    candidates: list[Release] = []
    by_name: dict[str, indexer_mod.IndexerResult] = {}
    for result in results:
        release = parse(result.title, kind)
        release = Release(**{**release.__dict__, "size": result.size or None})
        if not matches(release, title, year, season, episode):
            continue
        candidates.append(release)
        by_name.setdefault(release.raw, result)

    if not candidates:
        return None

    already = {
        row["release"]
        for row in db.query(conn, "SELECT release FROM grabs WHERE wanted_id = ?",
                            (wanted_row["id"],))
    }
    candidates = [c for c in candidates if c.raw not in already]
    if not candidates:
        return None

    current = latest_quality(conn, kind, title, year, season, episode)
    chosen, verdict = scoring.best(
        candidates, title, year, season, episode, profile=profile,
        wanted_kind=kind, current_quality=current,
    )
    if chosen is None:
        return None

    return GrabPlan(
        wanted_id=int(wanted_row["id"]),
        release=chosen,
        result=by_name[chosen.raw],
        score=verdict.score,
        reasons=verdict.reasons,
    )


def grab(conn, grab_plan: GrabPlan, client: Transmission,
         settings: Settings) -> int | None:
    """Record and start a download. The row is written before the RPC so a
    crash between the two cannot lose track of what was asked for."""
    release = grab_plan.release
    result = grab_plan.result
    link = result.download_url
    if not link and result.infohash:
        link = f"magnet:?xt=urn:btih:{result.infohash}"

    cur = conn.execute(
        "INSERT INTO grabs(wanted_id, indexer, guid, release, size, quality, score,"
        " infohash, state, grabbed_at) VALUES(?,?,?,?,?,?,?,?,'grabbed',?)",
        (grab_plan.wanted_id, result.indexer, result.guid, release.raw,
         result.size, release.quality, grab_plan.score, result.infohash, db.now()),
    )
    grab_id = int(cur.lastrowid)
    conn.execute("UPDATE wanted SET status = 'grabbed', updated_at = ? WHERE id = ?",
                 (db.now(), grab_plan.wanted_id))

    if not link:
        # Nothing to hand the client. Saying so is the difference between a wanted
        # item that is retried against another indexer and one that reads as a
        # download that failed for no stated reason.
        conn.execute("UPDATE grabs SET state = 'failed' WHERE id = ?", (grab_id,))
        conn.execute(
            "UPDATE wanted SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            (f"{result.indexer}: result carried no download link", db.now(),
             grab_plan.wanted_id),
        )
        return None

    try:
        added = client.add(link, labels=[settings.transmission_category])
    except (TransmissionError, OSError) as exc:
        conn.execute("UPDATE grabs SET state = 'failed' WHERE id = ?", (grab_id,))
        conn.execute(
            "UPDATE wanted SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            (f"transmission: {exc}", db.now(), grab_plan.wanted_id),
        )
        return None

    conn.execute("UPDATE grabs SET download_id = ? WHERE id = ?",
                 (str(added.get("id") or ""), grab_id))
    return grab_id


def import_completed(client: Transmission, settings: Settings) -> list[dict]:
    """Link finished downloads into the library and mark their grabs imported.

    A torrent counts as imported only once a file is actually in place; a
    finished torrent with nothing importable stays 'completed' so the next run
    can retry instead of silently losing it.

    The downloader is asked *before* a transaction is opened: holding a write
    lock across an RPC is how a scheduler pass used to stall every request.
    """
    imported: list[dict] = []
    try:
        torrents = client.completed()
    except (TransmissionError, OSError) as exc:
        log.warning("transmission unreachable: %s", exc)
        return imported
    if not torrents:
        return imported

    with db.session(settings.db_path) as conn:
        imported = _import_into(conn, torrents, settings)

    if imported:
        # The two things the old stack needed other services for: tell Jellyfin
        # what just arrived, and fetch a subtitle for the new file only.
        after_import(settings, [Path(row["path"]) for row in imported])
    return imported


def _import_into(conn, torrents, settings: Settings) -> list[dict]:
    imported: list[dict] = []
    by_download = {
        str(row["download_id"]): row
        for row in db.query(
            conn, "SELECT * FROM grabs WHERE state IN ('grabbed','completed')"
        )
    }

    for torrent in torrents:
        row = by_download.get(str(torrent.id))
        if row is None:
            continue
        release = parse(torrent.name)
        directory = Path(torrent.directory) / torrent.name
        video = library.find_video(directory) if directory.exists() else None
        if video is None:
            conn.execute("UPDATE grabs SET state = 'completed' WHERE id = ?", (row["id"],))
            continue

        destination = library.target_path(video, release, settings.library_root)
        action = library.link_or_copy(video, destination)
        size = video.stat().st_size if video.exists() else 0
        db.upsert_item(conn, {
            "kind": release.kind,
            "title": release.title,
            "year": release.year,
            "season": release.season,
            "episode": release.episode,
            "quality": release.quality,
            "path": str(destination),
            "size": size,
            "fingerprint": library.fingerprint(release),
        })
        conn.execute("UPDATE grabs SET state = 'imported' WHERE id = ?", (row["id"],))
        conn.execute("UPDATE wanted SET status = 'imported', updated_at = ? WHERE id = ?",
                     (db.now(), row["wanted_id"]))
        imported.append({"release": release.raw, "path": str(destination), "action": action})
    return imported


def subtitle_provider(settings: Settings):
    """The best available subtitle provider for this configuration."""
    return subtitles.make_provider(settings)


def jellyfin_client(settings: Settings) -> jellyfin_mod.Jellyfin:
    return jellyfin_mod.Jellyfin(settings.jellyfin_url, settings.jellyfin_api_key)


def after_import(settings: Settings, paths: list[Path],
                 provider=None, jelly: jellyfin_mod.Jellyfin | None = None) -> dict:
    """Post-import work: Jellyfin notification, then subtitles for those files.

    Both steps are optional and none of them may fail an import that already
    put the file on disk, so each is wrapped and reported separately. Jellyfin
    is told before any transaction is opened, for the same reason the import is.
    """
    report: dict[str, Any] = {}
    if settings.jellyfin_enabled:
        jelly = jelly or jellyfin_client(settings)
        try:
            report["jellyfin"] = jelly.refresh(paths)
        except Exception as exc:  # a broken client must not lose the import
            log.warning("jellyfin refresh failed: %s", exc)
            report["jellyfin"] = {"configured": jelly.configured, "error": str(exc)}

    if settings.subtitles_enabled:
        provider = provider or subtitle_provider(settings)
        if provider is not None and getattr(provider, "configured", True):
            fetched: list[dict] = []
            with db.session(settings.db_path) as conn:
                for path in paths:
                    try:
                        fetched.extend(subtitles.fetch_for(conn, path,
                                                           list(settings.subtitle_languages),
                                                           provider))
                    except Exception as exc:
                        log.warning("subtitle fetch failed for %s: %s", path, exc)
            report["subtitles"] = fetched
    return report


def _fetch_text(url: str) -> str:
    import httpx
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


# A real guide is tens of megabytes. Parsing it once per page load was the one
# place the merged service could burn CPU and bandwidth for nothing, so the
# parsed result is cached for a few minutes per (playlist, guide, horizon).
GUIDE_TTL = 600
_guide_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def guide(settings: Settings, hours: int = 6,
          fetch: Callable[[str], str] | None = None,
          ttl: float | None = None) -> dict[str, Any]:
    """Channels, what is on now, and the guide window — fetched at most once."""
    from datetime import datetime, timedelta, timezone

    key = f"{settings.livetv_m3u}|{settings.livetv_xmltv}|{hours}"
    ttl = GUIDE_TTL if ttl is None else ttl
    cached = _guide_cache.get(key)
    now_monotonic = time.monotonic()
    if cached and now_monotonic - cached[0] < ttl:
        return cached[1]

    payload: dict[str, Any] = {"channels": [], "programmes": [], "hours": hours}
    if settings.livetv_m3u:
        fetch = fetch or _fetch_text
        channels = livetv.parse_m3u(fetch(settings.livetv_m3u))
        names: dict[str, str] = {}
        programmes: list[livetv.Programme] = []
        if settings.livetv_xmltv:
            names, programmes = livetv.parse_xmltv(fetch(settings.livetv_xmltv))
        channels = livetv.number_channels(livetv.merge_logos(channels, names),
                                          settings.livetv_channel_base)
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        window = [p for p in programmes if p.stop > now and p.start < horizon]
        payload = {
            "channels": livetv.lineup(channels, window),
            "programmes": [p.to_dict() for p in window][:4000],
            "hours": hours,
            "fetched": db.now(),
        }
    _guide_cache[key] = (now_monotonic, payload)
    return payload


def live_lineup(settings: Settings, fetch: Callable[[str], str] | None = None) -> list[dict]:
    """The numbered channel list, from the cached guide."""
    return guide(settings, fetch=fetch)["channels"]


class Scheduler:
    """The single background loop. One thread, one lock, no cron files.

    Replacing five separate services with one scheduler is a real saving: the
    idle cost is one thread instead of five containers, and the work is
    serialised so search cannot race import.
    """

    def __init__(self, settings: Settings, client_factory: Callable[[], Transmission] | None = None):
        self.settings = settings
        self.client_factory = client_factory or (lambda: Transmission(
            settings.transmission_url, settings.transmission_user,
            settings.transmission_password,
        ))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {}
        self._next_subtitles = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="core-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> dict[str, Any]:
        """One pass of every job. Safe to call directly in tests.

        Each step takes its own short transaction. Search fans out over the
        network for up to a minute and must not hold a write lock while it does:
        that is what made a search request wait out its whole busy timeout
        behind a scheduler pass.
        """
        result: dict[str, Any] = {}
        client = self.client_factory()
        path = self.settings.db_path

        result["imported"] = import_completed(client, self.settings)
        result["downloads"] = client.status()

        with db.session(path) as conn:
            profile_row = db.get_setting(conn, "profile")
            profile = scoring.DEFAULT_PROFILE
            if profile_row:
                import json
                profile = {**scoring.DEFAULT_PROFILE, **json.loads(profile_row)}
            indexer_list = indexers_from_db(conn)
            wanted = db.query(
                conn, "SELECT * FROM wanted WHERE status IN ('wanted','failed') "
                      "ORDER BY id LIMIT ?", (self.settings.search_batch,),
            )

        grabbed: list[str] = []
        for row in wanted:
            with db.session(path) as conn:
                conn.execute(
                    "UPDATE wanted SET status = 'searching', updated_at = ? WHERE id = ?",
                    (db.now(), row["id"]),
                )
                plan_row = plan(conn, row, indexer_list, profile, self.settings)
            if plan_row is None:
                with db.session(path) as conn:
                    conn.execute(
                        "UPDATE wanted SET status = 'wanted', attempts = attempts + 1,"
                        " last_error = NULL, updated_at = ? WHERE id = ?",
                        (db.now(), row["id"]),
                    )
                continue
            with db.session(path) as conn:
                if grab(conn, plan_row, client, self.settings):
                    grabbed.append(plan_row.release.raw)
        result["grabbed"] = grabbed

        with db.session(path) as conn:
            result["stats"] = db.stats(conn)

        # Subtitles are the slowest job here (a network call per file) and the
        # least urgent, so they run on their own cadence with a small batch.
        due = time.monotonic() >= self._next_subtitles
        if due and self.settings.subtitles_enabled:
            try:
                with db.session(path) as conn:
                    result["subtitles"] = subtitles.run_pass(
                        conn, self.settings, subtitle_provider(self.settings),
                    )
            except Exception as exc:
                log.warning("subtitle pass failed: %s", exc)
                result["subtitles"] = {"error": str(exc)}
            self._next_subtitles = time.monotonic() + self.settings.subtitle_interval

        self.last = {**result, "at": db.now()}
        return self.last

    def _run(self) -> None:
        next_search = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now >= next_search:
                    self.tick()
                    next_search = now + self.settings.search_interval
                else:
                    with db.session(self.settings.db_path) as conn:
                        import_completed(conn, self.client_factory(), self.settings)
            except Exception:  # a scheduler thread must never die
                log.exception("scheduler pass failed")
            self._stop.wait(self.settings.import_interval)
