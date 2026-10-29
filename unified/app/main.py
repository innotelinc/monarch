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

from . import db, indexers as indexer_mod, library, scoring
from .acquisition import (
    CATEGORIES, Scheduler, indexers_from_db, latest_quality, live_lineup,
)
from .downloader import Transmission
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
    kind: str = Field(default="torznab", pattern="^(torznab|newznab)$")
    cats: str = ""
    enabled: bool = True


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
            item_id = db.upsert_item(conn, row)
            if before is None:
                added += 1
            else:
                item_id = item_id
    return {"scanned": len(rows), "added": added}


@app.get("/api/dedupe")
def dedupe_report(_: str = Depends(current_user)) -> dict[str, Any]:
    """Files in the library whose bytes are identical, and what they cost."""
    paths = list(library.iter_files(settings.library_root))
    report = library.plan_dedupe(paths)
    return report


@app.get("/api/live/channels")
def live_channels(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"channels": live_lineup(settings)}


@app.get("/api/live/guide")
def live_guide(hours: int = Query(default=6, ge=1, le=72),
               _: str = Depends(current_user)) -> dict[str, Any]:
    from . import livetv
    from .acquisition import _fetch_text

    if not settings.livetv_m3u:
        return {"channels": [], "programmes": []}
    channels = livetv.parse_m3u(_fetch_text(settings.livetv_m3u))
    names, programmes = (livetv.parse_xmltv(_fetch_text(settings.livetv_xmltv))
                         if settings.livetv_xmltv else ({}, []))
    channels = livetv.number_channels(livetv.merge_logos(channels, names),
                                      settings.livetv_channel_base)
    return {
        "channels": [c.to_dict() for c in channels],
        "programmes": [p.to_dict() for p in programmes if p.stop > db_now()][:2000],
        "hours": hours,
    }


def db_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


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
