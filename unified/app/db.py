"""SQLite storage for the core service.

One file, WAL mode, no ORM. The six tables are the whole model: what is in the
library, what we still want, what we grabbed for it, which indexers we may ask,
and the settings that survive a container restart.

The dedupe rule is enforced by the database, not by application code: two rows
with the same fingerprint cannot exist, so a second scan of the same file, a
duplicate release, or two indexers returning the same torrent cannot inflate
the catalog.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('movie','series','episode','album','track')),
    title       TEXT NOT NULL,
    year        INTEGER,
    season      INTEGER,
    episode     INTEGER,
    quality     TEXT,
    path        TEXT,
    size        INTEGER,
    fingerprint TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS items_fingerprint ON items(fingerprint);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS items_title ON items(lower(title));

CREATE TABLE IF NOT EXISTS wanted (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('movie','series','episode','album','track')),
    title      TEXT NOT NULL,
    year       INTEGER,
    season     INTEGER,
    episode    INTEGER,
    status     TEXT NOT NULL DEFAULT 'wanted'
               CHECK (status IN ('wanted','searching','grabbed','imported','failed')),
    profile    TEXT NOT NULL DEFAULT 'default',
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS wanted_key ON wanted(
    kind, lower(title), ifnull(year, 0), ifnull(season, -1), ifnull(episode, -1));
CREATE INDEX IF NOT EXISTS wanted_status ON wanted(status);

CREATE TABLE IF NOT EXISTS grabs (
    id          INTEGER PRIMARY KEY,
    wanted_id   INTEGER REFERENCES wanted(id) ON DELETE CASCADE,
    indexer     TEXT,
    guid        TEXT,
    release     TEXT NOT NULL,
    size        INTEGER,
    quality     TEXT,
    score       INTEGER,
    infohash    TEXT,
    download_id TEXT,
    state       TEXT NOT NULL DEFAULT 'grabbed'
                CHECK (state IN ('grabbed','completed','imported','failed')),
    grabbed_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS grabs_unique ON grabs(wanted_id, release);
CREATE INDEX IF NOT EXISTS grabs_state ON grabs(state);

CREATE TABLE IF NOT EXISTS indexers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    url     TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    kind    TEXT NOT NULL DEFAULT 'torznab'
            CHECK (kind IN ('torznab','newznab','prowlarr')),
    cats    TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (video, language): the record of what was looked for and found,
-- so a file with genuinely no subtitle is not re-queried on every pass.
CREATE TABLE IF NOT EXISTS subtitles (
    id            INTEGER PRIMARY KEY,
    video_path    TEXT NOT NULL,
    language      TEXT NOT NULL,
    subtitle_path TEXT,
    source        TEXT,
    status        TEXT NOT NULL DEFAULT 'ok'
                  CHECK (status IN ('ok','missing','failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS subtitles_key ON subtitles(video_path, language);
CREATE INDEX IF NOT EXISTS subtitles_status ON subtitles(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating the parent directory when needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database written by an older build up to the current schema.

    SQLite cannot widen a CHECK constraint in place, so a table that predates
    the `prowlarr` indexer kind is rebuilt once. Everything else is additive and
    covered by CREATE TABLE IF NOT EXISTS above.
    """
    row = one(conn, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'indexers'")
    if row is None or "'prowlarr'" in (row["sql"] or ""):
        return
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN IMMEDIATE;
        CREATE TABLE indexers_new (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL UNIQUE,
            url     TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            kind    TEXT NOT NULL DEFAULT 'torznab'
                    CHECK (kind IN ('torznab','newznab','prowlarr')),
            cats    TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO indexers_new(id, name, url, api_key, kind, cats, enabled)
            SELECT id, name, url, api_key, kind, cats, enabled FROM indexers;
        DROP TABLE indexers;
        ALTER TABLE indexers_new RENAME TO indexers;
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection plus a transaction that rolls back on failure.

    The transaction is *deferred* on purpose. A caller that only reads (the
    search endpoint, the scheduler's decision pass) must not take a write lock:
    those blocks surround network calls that can take a minute, and an
    immediate lock held for that long made every other request wait out its
    busy timeout. SQLite upgrades to a write lock at the first write, which is
    where the transaction actually needs one.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        # A failed COMMIT (or a failed BEGIN) leaves nothing to roll back; the
        # rollback must not replace the error that got us here.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_item(conn: sqlite3.Connection, item: dict[str, Any]) -> int:
    """Insert an item, or return the id of the row with the same fingerprint.

    The fingerprint is what makes the catalog idempotent: rescanning a library
    of ten thousand files produces zero new rows.
    """
    stamp = now()
    row = one(conn, "SELECT id FROM items WHERE fingerprint = ?", (item["fingerprint"],))
    if row:
        conn.execute(
            "UPDATE items SET path = ?, size = ?, quality = ?, updated_at = ? WHERE id = ?",
            (item.get("path"), item.get("size"), item.get("quality"), stamp, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO items(kind, title, year, season, episode, quality, path, size,"
        " fingerprint, added_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["kind"], item["title"], item.get("year"), item.get("season"),
            item.get("episode"), item.get("quality"), item.get("path"),
            item.get("size"), item["fingerprint"], stamp, stamp,
        ),
    )
    return int(cur.lastrowid)


def add_wanted(conn: sqlite3.Connection, kind: str, title: str, year: int | None = None,
               season: int | None = None, episode: int | None = None,
               profile: str = "default") -> int:
    """Add a wanted item, returning the existing id when it is already wanted."""
    stamp = now()
    row = one(
        conn,
        "SELECT id FROM wanted WHERE kind = ? AND lower(title) = lower(?) "
        "AND ifnull(year,0) = ifnull(?,0) AND ifnull(season,-1) = ifnull(?,-1) "
        "AND ifnull(episode,-1) = ifnull(?,-1)",
        (kind, title, year, season, episode),
    )
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO wanted(kind, title, year, season, episode, profile, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (kind, title, year, season, episode, profile, stamp, stamp),
    )
    return int(cur.lastrowid)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = one(conn, sql, params)
        return int(row[0]) if row else 0

    return {
        "items": count("SELECT count(*) FROM items"),
        "movies": count("SELECT count(*) FROM items WHERE kind = 'movie'"),
        "episodes": count("SELECT count(*) FROM items WHERE kind = 'episode'"),
        "tracks": count("SELECT count(*) FROM items WHERE kind = 'track'"),
        "bytes": count("SELECT coalesce(sum(size), 0) FROM items"),
        "wanted": count("SELECT count(*) FROM wanted WHERE status = 'wanted'"),
        "grabbed": count("SELECT count(*) FROM grabs WHERE state = 'grabbed'"),
        "imported": count("SELECT count(*) FROM grabs WHERE state = 'imported'"),
        "failed": count("SELECT count(*) FROM grabs WHERE state = 'failed'"),
        "indexers": count("SELECT count(*) FROM indexers WHERE enabled = 1"),
    }
