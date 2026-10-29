"""Monarch core — one service for the whole media platform.

The eight public hostnames (media/req/movies/tv/index/xxx/mp3/dl) collapse to
this: one process serves the UI and the API, owns the catalog, searches the
indexers, decides what to grab, watches the downloader and reads live TV. The
only things still outside are Jellyfin (playback and transcoding) and
Transmission (the one download client), both of which this service drives
rather than replaces.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, indexers as indexer_mod, library, scoring, subtitles
from .acquisition import (
    CATEGORIES, Scheduler, guide, indexers_from_db, jellyfin_client, latest_quality,
    subtitle_provider,
)
from .downloader import Transmission, TransmissionError
from .parser import parse, title_key
from .settings import settings

scheduler = Scheduler(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect(settings.db_path).close()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Monarch core", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- dependencies

def current_user(request: Request) -> str:
    """The signed-in user, from the single SSO gateway in front of the app.

    The app is never published directly: one oauth2-proxy instance terminates
    the browser session and sets the header. CORE_AUTH_DISABLED exists for
    local development and is called out in the docs as unsafe in production.
    """
    if settings.auth_disabled:
        return request.headers.get(settings.auth_header) or "dev@localhost"
    email = request.headers.get(settings.auth_header, "").strip()
    if not email:
        raise HTTPException(status_code=401, detail="no authenticated session")
    return email


def conn_scope():
    return db.session(settings.db_path)


def transmission() -> Transmission:
    return Transmission(settings.transmission_url, settings.transmission_user,
                        settings.transmission_password)


def profile_of(conn) -> dict:
    raw = db.get_setting(conn, "profile")
    return {**scoring.DEFAULT_PROFILE, **(json.loads(raw) if raw else {})}


# ---------------------------------------------------------------------- models

class WantedIn(BaseModel):
    kind: str = Field(pattern="^(movie|episode|series|track)$")
    title: str = Field(min_length=1, max_length=250)
    year: int | None = Field(default=None, ge=1880, le=2100)
    season: int | None = Field(default=None, ge=0, le=100)
    episode: int | None = Field(default=None, ge=0, le=2000)
    profile: str = "default"


class IndexerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=4, max_length=500)
    api_key: str = ""
    kind: str = Field(default="torznab", pattern="^(torznab|newznab|prowlarr)$")
    cats: str = ""
    enabled: bool = True


class GrabIn(BaseModel):
    """One release, grabbed by hand from a search result."""
    title: str = Field(min_length=1, max_length=400)
    kind: str = Field(default="movie", pattern="^(movie|episode|track)$")
    download_url: str = Field(default="", max_length=2000)
    infohash: str = Field(default="", max_length=64)
    indexer: str = Field(default="", max_length=80)
    guid: str = Field(default="", max_length=1000)
    size: int = Field(default=0, ge=0)
    wanted_id: int | None = None


class ProfileIn(BaseModel):
    resolutions: list[str] | None = None
    sources: list[str] | None = None
    codecs: list[str] | None = None
    require_hdr: bool | None = None
    min_size_gb: float | None = None
    max_size_gb: float | None = None
    upgrade_allowed: bool | None = None
    cutoff: str | None = None


# ------------------------------------------------------------------------- API

@app.get("/api/health")
def health() -> dict[str, Any]:
    with conn_scope() as conn:
        stats = db.stats(conn)
    return {"ok": True, "version": app.version, "stats": stats,
            "scheduler": {"at": scheduler.last.get("at")}}


@app.get("/api/stats")
def stats(_: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        return {
            **db.stats(conn),
            "transmission": transmission().status(),
            "livetv": bool(settings.livetv_m3u),
            "last": scheduler.last,
        }


@app.get("/api/library")
def library_items(
    kind: str | None = Query(default=None, pattern="^(movie|episode|series|track)$"),
    q: str = "",
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(current_user),
) -> dict[str, Any]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append("series" if kind == "episode" else kind)
    if q.strip():
        clauses.append("lower(title) LIKE ?")
        params.append(f"%{q.strip().lower()}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn_scope() as conn:
        rows = db.query(
            conn,
            f"SELECT * FROM items {where} ORDER BY added_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = db.one(conn, f"SELECT count(*) FROM items {where}", tuple(params))
    return {"total": int(total[0]) if total else 0,
            "items": [dict(row) for row in rows]}


@app.get("/api/items/{item_id}")
def item(item_id: int, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        row = db.one(conn, "SELECT * FROM items WHERE id = ?", (item_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="no such item")
        grabs = db.query(
            conn, "SELECT * FROM grabs WHERE wanted_id IN "
                  "(SELECT id FROM wanted WHERE lower(title) = lower(?) AND ifnull(year,0) = ifnull(?,0))",
            (row["title"], row["year"]),
        )
    return {"item": dict(row), "grabs": [dict(g) for g in grabs]}


@app.get("/api/search")
def search(
    q: str = Query(min_length=2, max_length=200),
    kind: str = Query(default="movie", pattern="^(movie|episode|track)$"),
    _: str = Depends(current_user),
) -> dict[str, Any]:
    """Live search across every indexer, scored but not grabbed."""
    with conn_scope() as conn:
        indexer_list = indexers_from_db(conn)
        profile = profile_of(conn)
    if not indexer_list:
        return {"query": q, "results": [], "reason": "no indexers configured"}

    results = indexer_mod.search_all(
        indexer_list, q, limit=settings.results_per_indexer,
        categories=CATEGORIES.get(kind, ""),
    )
    current = None
    with conn_scope() as conn:
        current = latest_quality(conn, kind, q, None, None, None)

    payload = []
    for result in results[:60]:
        release = parse(result.title, kind)
        from .parser import Release
        release = Release(**{**release.__dict__, "size": result.size or None})
        verdict = scoring.evaluate(release, profile, kind)
        payload.append({**result.to_dict(), "parsed": release.to_dict(),
                        "verdict": verdict.to_dict()})
    return {"query": q, "results": payload, "current_quality": current}


@app.post("/api/grab")
def grab(payload: GrabIn, _: str = Depends(current_user)) -> dict[str, Any]:
    """Grab one release the user picked by hand.

    The automatic path chooses for you; this is the override. It goes through
    the same publishing steps — a row first, then the downloader — so a hand
    grab is watched, imported and deduplicated exactly like an automatic one.
    """
    link = payload.download_url.strip()
    if not link and payload.infohash:
        link = f"magnet:?xt=urn:btih:{payload.infohash}"
    if not link:
        raise HTTPException(status_code=422, detail="a download URL or an infohash is required")

    release = parse(payload.title, payload.kind)
    with conn_scope() as conn:
        existing = db.one(
            conn,
            "SELECT * FROM grabs WHERE release = ? AND state IN ('grabbed','completed','imported')"
            " ORDER BY id DESC LIMIT 1",
            (release.raw,),
        )
        if existing is not None:
            return {"ok": True, "duplicate": True, "grab": dict(existing),
                    "message": "that release was already grabbed"}

        cur = conn.execute(
            "INSERT INTO grabs(wanted_id, indexer, guid, release, size, quality, score,"
            " infohash, state, grabbed_at) VALUES(?,?,?,?,?,?,0,?,'grabbed',?)",
            (payload.wanted_id, payload.indexer or "manual", payload.guid,
             release.raw, payload.size or None, release.quality,
             payload.infohash or None, db.now()),
        )
        grab_id = int(cur.lastrowid)
        if payload.wanted_id:
            conn.execute("UPDATE wanted SET status = 'grabbed', updated_at = ? WHERE id = ?",
                         (db.now(), payload.wanted_id))
        row = db.one(conn, "SELECT * FROM grabs WHERE id = ?", (grab_id,))

        try:
            added = transmission().add(link, labels=[settings.transmission_category])
        except (TransmissionError, OSError) as exc:
            conn.execute("UPDATE grabs SET state = 'failed' WHERE id = ?", (grab_id,))
            if payload.wanted_id:
                conn.execute(
                    "UPDATE wanted SET status = 'failed', last_error = ?, updated_at = ?"
                    " WHERE id = ?", (f"transmission: {exc}", db.now(), payload.wanted_id),
                )
            failed = db.one(conn, "SELECT * FROM grabs WHERE id = ?", (grab_id,))
            return {"ok": False, "grab": dict(failed), "error": f"downloader: {exc}"}
        conn.execute("UPDATE grabs SET download_id = ? WHERE id = ?",
                     (str(added.get("id") or ""), grab_id))
        row = db.one(conn, "SELECT * FROM grabs WHERE id = ?", (grab_id,))

    return {"ok": True, "duplicate": bool(added.get("duplicate")),
            "grab": dict(row), "added": added}


@app.get("/api/wanted")
def wanted_list(status: str | None = None, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        if status:
            rows = db.query(conn, "SELECT * FROM wanted WHERE status = ? ORDER BY id DESC", (status,))
        else:
            rows = db.query(conn, "SELECT * FROM wanted ORDER BY id DESC LIMIT 300")
    return {"items": [dict(row) for row in rows]}


@app.post("/api/wanted")
def wanted_add(payload: WantedIn, _: str = Depends(current_user)) -> dict[str, Any]:
    kind = "episode" if payload.kind == "series" else payload.kind
    if kind == "episode" and payload.season is None:
        raise HTTPException(status_code=422, detail="an episode needs a season")
    with conn_scope() as conn:
        wanted_id = db.add_wanted(conn, kind, payload.title, payload.year,
                                  payload.season, payload.episode, payload.profile)
        row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    return {"item": dict(row)}


@app.delete("/api/wanted/{wanted_id}")
def wanted_delete(wanted_id: int, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        conn.execute("DELETE FROM wanted WHERE id = ?", (wanted_id,))
    return {"deleted": wanted_id}


@app.get("/api/downloads")
def downloads(_: str = Depends(current_user)) -> dict[str, Any]:
    client = transmission()
    with conn_scope() as conn:
        grabs = db.query(conn, "SELECT * FROM grabs ORDER BY id DESC LIMIT 200")
    status = client.status()
    torrents = client.list() if status.get("reachable") else []
    return {"status": status, "torrents": [t.to_dict() for t in torrents],
            "grabs": [dict(row) for row in grabs]}


@app.post("/api/scan")
def scan(_: str = Depends(current_user)) -> dict[str, Any]:
    rows = library.scan(settings.library_root, settings.media_kinds)
    added = 0
    with conn_scope() as conn:
        for row in rows:
            before = db.one(conn, "SELECT id FROM items WHERE fingerprint = ?",
                            (row["fingerprint"],))
            db.upsert_item(conn, row)
            if before is None:
                added += 1
    jellyfin_report = None
    if added and settings.jellyfin_enabled:
        jellyfin_report = jellyfin_client(settings).refresh()
    return {"scanned": len(rows), "added": added, "jellyfin": jellyfin_report}


@app.get("/api/subtitles")
def subtitles_status(preview: int = Query(default=0, ge=0, le=100),
                     _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        report = subtitles.stats(conn)
    provider = subtitle_provider(settings)
    payload: dict[str, Any] = {
        **report,
        "languages": list(settings.subtitle_languages),
        "provider": provider.name if provider else "",
        "enabled": bool(settings.subtitles_enabled and provider is not None),
    }
    if preview:
        payload["library"] = subtitles.missing_report(
            settings.library_root, list(settings.subtitle_languages), limit=preview,
        )
    return payload


@app.post("/api/subtitles/fetch")
def subtitles_fetch(limit: int = Query(default=0, ge=0, le=200),
                    _: str = Depends(current_user)) -> dict[str, Any]:
    """Run a subtitle pass now. Missing files are recorded, not retried blindly."""
    provider = subtitle_provider(settings)
    if provider is None:
        raise HTTPException(
            status_code=409,
            detail="no subtitle provider configured — set CORE_SUBTITLE_PROVIDER "
                   "(auto/opensubtitles/legacy) or OPENSUBTITLES_API_KEY",
        )
    with conn_scope() as conn:
        result = subtitles.run_pass(conn, settings, provider,
                                   limit=limit or settings.subtitle_batch, delay=0)
    return result


@app.get("/api/jellyfin")
def jellyfin_status(_: str = Depends(current_user)) -> dict[str, Any]:
    return jellyfin_client(settings).info()


@app.post("/api/jellyfin/refresh")
def jellyfin_refresh(_: str = Depends(current_user)) -> dict[str, Any]:
    client = jellyfin_client(settings)
    if not client.configured:
        raise HTTPException(status_code=409, detail="JELLYFIN_URL / JELLYFIN_API_KEY are not set")
    return client.refresh()


@app.get("/api/dedupe")
def dedupe_report(_: str = Depends(current_user)) -> dict[str, Any]:
    """Files in the library whose bytes are identical, and what they cost."""
    paths = list(library.iter_files(settings.library_root))
    report = library.plan_dedupe(paths)
    return report


@app.get("/api/live/channels")
def live_channels(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"channels": guide(settings)["channels"]}


@app.get("/api/live/guide")
def live_guide(hours: int = Query(default=6, ge=1, le=72),
               _: str = Depends(current_user)) -> dict[str, Any]:
    """The guide window. Parsed once and cached: a real XMLTV file is tens of MB."""
    return guide(settings, hours=hours)


@app.get("/api/indexers")
def indexers_list(_: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        return {"indexers": [i.to_dict() for i in indexers_from_db(conn)]}


@app.post("/api/indexers")
def indexers_add(payload: IndexerIn, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        conn.execute(
            "INSERT INTO indexers(name, url, api_key, kind, cats, enabled)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
            " url = excluded.url, api_key = excluded.api_key, kind = excluded.kind,"
            " cats = excluded.cats, enabled = excluded.enabled",
            (payload.name, payload.url.rstrip("/"), payload.api_key, payload.kind,
             payload.cats, int(payload.enabled)),
        )
    return {"saved": payload.name}


@app.delete("/api/indexers/{name}")
def indexers_delete(name: str, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        conn.execute("DELETE FROM indexers WHERE name = ?", (name,))
    return {"deleted": name}


@app.get("/api/profile")
def profile_get(_: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        return profile_of(conn)


@app.put("/api/profile")
def profile_put(payload: ProfileIn, _: str = Depends(current_user)) -> dict[str, Any]:
    with conn_scope() as conn:
        profile = profile_of(conn)
        for key, value in payload.model_dump(exclude_none=True).items():
            profile[key] = value
        db.set_setting(conn, "profile", json.dumps(profile))
    return profile


@app.get("/api/scheduler")
def scheduler_status(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"last": scheduler.last,
            "interval": settings.search_interval,
            "batch": settings.search_batch}


@app.post("/api/scheduler/tick")
def scheduler_tick(_: str = Depends(current_user)) -> dict[str, Any]:
    return scheduler.tick()


# -------------------------------------------------------------------------- UI

STATIC = settings.static_dir
if STATIC.exists():
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index_page() -> FileResponse:
    page = STATIC / "index.html"
    if not page.exists():
        return JSONResponse({"detail": "UI not installed"}, status_code=500)
    return FileResponse(page)


@app.get("/{path:path}")
def spa(path: str):
    """Everything that is not an API route is the single-page app."""
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="no such endpoint")
    candidate = STATIC / path
    if candidate.is_file() and STATIC in candidate.resolve().parents:
        return FileResponse(candidate)
    return index_page()
