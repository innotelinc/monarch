#!/usr/bin/env python3
"""Probe the Comcast dial, remember what works, and fail over when a feed dies.

Jellyfin has no idea whether the stream behind a channel still answers: the M3U
is a list of URLs, and a dead one stays on the dial forever. Some upstream
providers also rotate their playlist, and the dial is only rebuilt twice a day by
the two timers, so a replacement feed added to `data/livetv-extra.m3u` can sit
unused until the next scheduled rebuild.

This is the sidecar for both problems. Every run it:

  * probes every stream in `/opt/epg/comcast-springfield.m3u` (HLS manifest by
    default, or `--deep` to follow the first variant and segment),
  * records the result, the latency, and a failure streak in a SQLite database,
  * notices when a source playlist or the lineup file changes, and
  * rebuilds the dial with `livetv_lineup.py --health-db`, which drops a dead
    feed only when the same guide channel has a live sibling, so the live one
    takes the primary number. A recovered feed is re-added.
  * then runs `refresh-livetv-guide.py`, which clears Jellyfin's downloaded
    XMLTV cache and makes Jellyfin re-read the dial and the guide.

A feed has to fail **twice in a row** before it is called dead, so one timeout
does not renumber the dial. The first run only establishes a baseline; it never
fails anything over.

Run it by hand while bringing a source up:

    python3 scripts/livetv-monitor.py --once
    python3 scripts/livetv-monitor.py --limit 40 --deep      # a quick look
    sqlite3 /var/lib/monarch/livetv-monitor.sqlite \\
        'select number, name, status, detail from channels where status="fail"'

The timer (`monarch-livetv-monitor.timer`) runs it every 30 minutes. The
database and the JSON status file live in `/var/lib/monarch`; set
`LIVETV_MONITOR_DB` to put them somewhere else. Nothing here needs Jellyfin to
be running: a failed refresh is logged and retried on the next run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SPEC = importlib.util.spec_from_file_location("livetv_lineup", SCRIPT_DIR / "livetv_lineup.py")
assert SPEC and SPEC.loader
lineup_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lineup_mod)

STATE_DIR = Path(os.environ.get("MONARCH_STATE_DIR") or "/var/lib/monarch")
DEFAULT_DB = Path(os.environ.get("LIVETV_MONITOR_DB") or STATE_DIR / "livetv-monitor.sqlite")
DEFAULT_PLAYLIST = Path(os.environ.get("LIVETV_PLAYLIST") or "/opt/epg/comcast-springfield.m3u")
DEFAULT_GUIDE = Path(os.environ.get("LIVETV_GUIDE") or "/opt/epg/guide.xml")
DEFAULT_OUT = Path(os.environ.get("LIVETV_EPG_DIR") or "/opt/epg")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FAILURE_THRESHOLD = 2
RUN_TIMEOUT = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    tvg_id TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    referrer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    probe TEXT NOT NULL DEFAULT 'unknown',
    http_status INTEGER,
    detail TEXT,
    latency_ms INTEGER,
    checked_at TEXT NOT NULL,
    last_ok TEXT,
    last_fail TEXT,
    ok_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS channels_url ON channels (url);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    fingerprint TEXT,
    checked_at TEXT NOT NULL,
    changed_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rows_checked INTEGER NOT NULL DEFAULT 0,
    probes INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    transitions INTEGER NOT NULL DEFAULT 0,
    source_changes INTEGER NOT NULL DEFAULT 0,
    rebuilt INTEGER NOT NULL DEFAULT 0,
    refreshed INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    number TEXT,
    name TEXT,
    detail TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ProbeResult:
    ok: bool
    http_status: int | None
    detail: str
    latency_ms: int


@dataclass
class ChannelResult:
    number: str
    name: str
    url: str
    tvg_id: str
    probe: ProbeResult
    user_agent: str = ""
    referrer: str = ""
    dial: bool = True


def headers_for(user_agent: str = "", referrer: str = "") -> dict[str, str]:
    headers = {"User-Agent": user_agent or DEFAULT_UA, "Accept": "*/*"}
    if referrer:
        headers["Referer"] = referrer
    return headers


def _first_uri(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _fetch(url: str, headers: dict[str, str], timeout: float, limit: int = 4096,
           extra: dict[str, str] | None = None) -> tuple[int, bytes, str, str]:
    request_headers = dict(headers)
    if extra:
        request_headers.update(extra)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(limit)
        return (response.status, body, response.geturl(),
                response.headers.get("Content-Type", ""))


def probe(url: str, user_agent: str = "", referrer: str = "", timeout: float = 10.0,
          deep: bool = False) -> ProbeResult:
    """Is this stream answering?

    A 200 alone is not enough: several dead origins answer with an HTML error
    page, so the body has to look like HLS. `deep` then follows the first
    variant (and, for a media playlist, the first segment) because a master can
    be fine while every child is gone — the exact failure that made a channel
    look alive in Jellyfin and show nothing.
    """
    started = time.monotonic()
    headers = headers_for(user_agent, referrer)
    try:
        # 512 KB, not 4 KB: a Pluto/FAST master can carry several #EXT-X-MEDIA
        # lines with session JWTs before its first variant URI, and truncating
        # there made a live channel look like it had no segment at all.
        status, body, final_url, content_type = _fetch(url, headers, timeout,
                                                      limit=512 * 1024)
    except urllib.error.HTTPError as exc:
        return ProbeResult(False, exc.code, f"HTTP {exc.code}", _ms(started))
    except urllib.error.URLError as exc:
        return ProbeResult(False, None, f"unreachable: {exc.reason}", _ms(started))
    except Exception as exc:  # timeout, TLS, malformed URL, ...
        return ProbeResult(False, None, f"{type(exc).__name__}: {exc}", _ms(started))

    if status >= 400:
        return ProbeResult(False, status, f"HTTP {status}", _ms(started))
    is_hls = b"#EXTM3U" in body or b"#EXT-X-" in body
    if not is_hls:
        if content_type.lower().startswith(("video/", "audio/")):
            return ProbeResult(True, status, "media stream", _ms(started))
        return ProbeResult(False, status, "HTTP 200 but not an HLS playlist", _ms(started))
    if not deep:
        return ProbeResult(True, status, "HLS playlist", _ms(started))

    # Follow up to three playlist levels, then fetch the first segment. This is
    # deliberately bounded: the monitor must finish on a 30-minute timer even
    # when the upstream is slow.
    current_url, current_body = final_url, body
    for _ in range(3):
        first = _first_uri(current_body.decode("utf-8", "replace"))
        if not first:
            return ProbeResult(False, status, "playlist has no segment", _ms(started))
        child = urllib.parse.urljoin(current_url, first)
        try:
            child_status, child_body, child_url, child_type = _fetch(
                child, headers, timeout, limit=8192,
                extra={"Range": "bytes=0-8191"})
        except urllib.error.HTTPError as exc:
            return ProbeResult(False, exc.code, f"variant/segment HTTP {exc.code}", _ms(started))
        except Exception as exc:
            return ProbeResult(False, None, f"variant/segment {type(exc).__name__}: {exc}",
                               _ms(started))
        if child_status >= 400:
            return ProbeResult(False, child_status, f"variant/segment HTTP {child_status}",
                               _ms(started))
        if b"#EXTM3U" in child_body or b"#EXT-X-" in child_body:
            current_url, current_body = child_url, child_body
            status = child_status
            continue
        if child_type.lower().startswith("text/html"):
            return ProbeResult(False, child_status, "segment is an HTML page", _ms(started))
        if not child_body:
            return ProbeResult(False, child_status, "segment is empty", _ms(started))
        return ProbeResult(True, child_status, "HLS segment", _ms(started))
    return ProbeResult(True, status, "HLS playlist (no segment checked)", _ms(started))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def read_dial(path: Path) -> list[dict]:
    """The dial's rows, with the headers a stream may need to answer."""
    entries = lineup_mod.parse_m3u(path.read_text(encoding="utf-8", errors="replace"))
    rows: list[dict] = []
    for index, entry in enumerate(entries):
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        attrs = entry.get("attrs") or {}
        rows.append({
            "number": (attrs.get("tvg-chno") or f"idx-{index}").strip(),
            "name": entry.get("name") or url,
            "url": url,
            "tvg_id": (attrs.get("tvg-id") or "").strip(),
            "user_agent": attrs.get("http-user-agent", ""),
            "referrer": attrs.get("http-referrer", ""),
            "dial": True,
        })
    return rows


def upstream_id(tvg_id: str, number: str) -> str:
    """The source id behind a dial id, e.g. `CNN.us@SD.42` -> `CNN.us@SD`.

    The dial's ids are unique per row, so 42 and 842 are two ids. To decide
    whether a dead feed has a live sibling we have to put the pair back together,
    and the row's own number is the suffix that was added.
    """
    suffix = f".{number}".strip()
    if suffix and tvg_id.endswith(suffix):
        return tvg_id[:-len(suffix)]
    return tvg_id


def default_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = [("upstream", lineup_mod.DEFAULT_M3U)]
    extra = Path(os.environ.get("LIVETV_EXTRA_M3U", str(lineup_mod.DEFAULT_EXTRA_M3U)))
    if extra.exists():
        sources.append(("extra", str(extra)))
    if lineup_mod.LINEUP_PATH.exists():
        sources.append(("lineup", str(lineup_mod.LINEUP_PATH)))
    return sources


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def load_source(location: str, timeout: float = 60.0) -> tuple[str | None, str | None]:
    """Source text, or `(None, error)`. A failed fetch must not look changed."""
    try:
        if re.match(r"^https?://", location):
            status, body, _final, _ctype = _fetch(
                location, headers_for(), timeout, limit=8 * 1024 * 1024)
            if status >= 400:
                return None, f"HTTP {status}"
            return body.decode("utf-8", "replace"), None
        return Path(location).read_text(encoding="utf-8", errors="replace"), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    # A database created before the watchlist existed has no header columns;
    # `CREATE TABLE IF NOT EXISTS` will not add them. Migrate in place so an
    # upgrade does not lose the health history.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
    for column in ("user_agent", "referrer"):
        if column not in columns:
            conn.execute(f"ALTER TABLE channels ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    return conn


def record_probes(conn: sqlite3.Connection, results: list[ChannelResult],
                  at: str) -> tuple[list[dict], int, int]:
    """Fold this run's probes into the state, returning events and counts.

    The first failure of a working feed is not a transition: it is held as
    `unknown` until the second consecutive failure. That is what keeps one
    timeout from renumbering the dial, and it also makes the first run a
    baseline rather than a mass failover.
    """
    previous = {row["number"]: row for row in conn.execute("SELECT * FROM channels")}
    transitions: list[dict] = []
    ok = failed = 0
    for result in results:
        probe_ok = result.probe.ok
        if probe_ok:
            ok += 1
        else:
            failed += 1
        prev = previous.get(result.number)
        if prev is not None and prev["url"] != result.url:
            # A new feed took this dial number. Its history is not the old
            # stream's; start it at a baseline instead of inheriting `fail`.
            prev = None
        if prev is None:
            status = "ok" if probe_ok else "unknown"
            streak = 0 if probe_ok else 1
            event = None
        elif probe_ok:
            status = "ok"
            streak = 0
            event = "recovered" if prev["status"] == "fail" else None
        else:
            streak = int(prev["fail_streak"]) + 1
            if streak >= FAILURE_THRESHOLD:
                status = "fail"
                event = "lost" if prev["status"] != "fail" else None
            else:
                status = prev["status"]
                event = None

        ok_count = int(prev["ok_count"]) + (1 if probe_ok else 0) if prev else int(probe_ok)
        fail_count = int(prev["fail_count"]) + (0 if probe_ok else 1) if prev else int(not probe_ok)
        last_ok = at if probe_ok else (prev["last_ok"] if prev else None)
        last_fail = at if not probe_ok else (prev["last_fail"] if prev else None)
        conn.execute(
            """
            INSERT INTO channels (number, name, url, tvg_id, user_agent, referrer,
                                  status, probe, http_status, detail, latency_ms,
                                  checked_at, last_ok, last_fail, ok_count,
                                  fail_count, fail_streak)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(number) DO UPDATE SET
                name=excluded.name, url=excluded.url, tvg_id=excluded.tvg_id,
                user_agent=excluded.user_agent, referrer=excluded.referrer,
                status=excluded.status, probe=excluded.probe,
                http_status=excluded.http_status, detail=excluded.detail,
                latency_ms=excluded.latency_ms, checked_at=excluded.checked_at,
                last_ok=excluded.last_ok, last_fail=excluded.last_fail,
                ok_count=excluded.ok_count, fail_count=excluded.fail_count,
                fail_streak=excluded.fail_streak
            """,
            (result.number, result.name, result.url, result.tvg_id,
             result.user_agent, result.referrer, status,
             "ok" if probe_ok else "fail", result.probe.http_status,
             result.probe.detail, result.probe.latency_ms, at, last_ok, last_fail,
             ok_count, fail_count, streak),
        )
        if event:
            transitions.append({"kind": event, "number": result.number,
                                "name": result.name, "detail": result.probe.detail,
                                "dial": result.dial})
            conn.execute(
                "INSERT INTO events (at, kind, number, name, detail) VALUES (?, ?, ?, ?, ?)",
                (at, event, result.number, result.name, result.probe.detail),
            )
    return transitions, ok, failed


def record_sources(conn: sqlite3.Connection, sources: list[tuple[str, str]],
                   at: str) -> list[dict]:
    changes: list[dict] = []
    for name, location in sources:
        text, error = load_source(location)
        row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
        if text is None:
            if row is None:
                conn.execute(
                    "INSERT INTO sources (name, location, fingerprint, checked_at, error) "
                    "VALUES (?, ?, NULL, ?, ?)", (name, location, at, error))
            else:
                conn.execute("UPDATE sources SET checked_at = ?, error = ? WHERE name = ?",
                             (at, error, name))
            continue
        digest = fingerprint(text)
        if row is None:
            conn.execute(
                "INSERT INTO sources (name, location, fingerprint, checked_at, error) "
                "VALUES (?, ?, ?, ?, NULL)", (name, location, digest, at))
        elif row["fingerprint"] != digest:
            conn.execute(
                "UPDATE sources SET location = ?, fingerprint = ?, checked_at = ?, "
                "changed_at = ?, error = NULL WHERE name = ?",
                (location, digest, at, at, name))
            changes.append({"name": name, "location": location})
            conn.execute(
                "INSERT INTO events (at, kind, number, name, detail) "
                "VALUES (?, 'source', NULL, ?, ?)",
                (at, name, location),
            )
        else:
            conn.execute(
                "UPDATE sources SET location = ?, checked_at = ?, error = NULL WHERE name = ?",
                (location, at, name))
    return changes


def failover_needed(rows: list[dict], health: dict[str, str]) -> bool:
    """Is there a dead row beside a live one on the same guide channel?"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row["tvg_id"]:
            groups.setdefault(upstream_id(row["tvg_id"], row["number"]), []).append(row)
    for group in groups.values():
        dial_rows = [row for row in group if row.get("dial", True)]
        if not dial_rows:
            continue
        # Only a dead row that is actually on the dial needs a rebuild. A dead
        # watchlist row beside a live dial row is the normal post-failover
        # state; a live watchlist row beside a dead dial row is exactly the
        # backup that should be promoted.
        if any(health.get(row["url"]) == "fail" for row in dial_rows) \
                and any(health.get(row["url"]) == "ok" for row in group):
            return True
    return False


def rebuild_dial(db: Path, guide: Path, out: Path, refresh: bool) -> tuple[bool, bool]:
    command = [sys.executable, str(SCRIPT_DIR / "livetv_lineup.py"),
               "--out", str(out), "--health-db", str(db)]
    if guide.exists():
        command += ["--guide", str(guide)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        print("livetv-monitor: dial rebuild failed: " + " / ".join(tail), file=sys.stderr)
        return False, False
    # The generator says how many dead feeds it dropped; that is the line that
    # tells an operator the health database was actually read and applied.
    for line in (result.stdout or "").splitlines():
        if "dropped" in line or "no channels found" in line:
            print(f"livetv-monitor: {line.strip()}")
    print("livetv-monitor: rebuilt the dial with the current health state")
    if not refresh:
        return True, False
    result = subprocess.run([sys.executable, str(SCRIPT_DIR / "refresh-livetv-guide.py")],
                            capture_output=True, text=True, timeout=RUN_TIMEOUT)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        print("livetv-monitor: Jellyfin refresh failed: " + " / ".join(tail), file=sys.stderr)
        return True, False
    print("livetv-monitor: Jellyfin refresh started (XMLTV cache cleared)")
    return True, True


def write_status(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="probe once and exit (default)")
    parser.add_argument("--playlist", default=str(DEFAULT_PLAYLIST),
                        help="the generated dial (default /opt/epg/comcast-springfield.m3u)")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="SQLite state (default /var/lib/monarch/livetv-monitor.sqlite)")
    parser.add_argument("--status-json", default=None,
                        help="status file for monitoring (default: beside the database)")
    parser.add_argument("--guide", default=str(DEFAULT_GUIDE), help="guide.xml to renumber")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="directory holding the dial")
    parser.add_argument("--workers", type=int, default=24, help="parallel probes (default 24)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request seconds")
    parser.add_argument("--limit", type=int, default=0,
                        help="probe only the first N channels (0 = the whole dial)")
    parser.add_argument("--deep", action="store_true",
                        help="follow the first variant and segment, not just the manifest")
    parser.add_argument("--no-rebuild", action="store_true",
                        help="record health/source state but do not rebuild the dial")
    parser.add_argument("--no-refresh", action="store_true",
                        help="rebuild the dial but do not refresh Jellyfin")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args()

    playlist = Path(args.playlist)
    if not playlist.is_file():
        print(f"livetv-monitor: no dial at {playlist} - nothing to probe yet")
        return 0
    db = Path(args.db)
    status_path = Path(args.status_json) if args.status_json else db.with_suffix(".json")

    lock_path = db.with_suffix(db.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("livetv-monitor: another run holds the lock; skipping this tick")
        return 0

    started = now_iso()
    conn = open_db(db)
    run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (started,)).lastrowid
    conn.commit()

    rows = read_dial(playlist)
    if args.limit > 0:
        rows = sorted(rows, key=lambda row: lineup_mod._number_key(row["number"]))[:args.limit]
    else:
        # A feed that was dropped in favour of a live sibling is no longer on
        # the dial, so probing only the dial would never see it recover. Keep
        # probing the failed feeds that still have a network on the dial; the
        # header columns are what let a referrer-only stream answer.
        dial_numbers = {row["number"] for row in rows}
        dial_upstreams = {upstream_id(row["tvg_id"], row["number"])
                          for row in rows if row["tvg_id"]}
        for row in conn.execute(
                "SELECT number, name, url, tvg_id, user_agent, referrer "
                "FROM channels WHERE status = 'fail'"):
            if row["number"] in dial_numbers or not row["url"] or not row["tvg_id"]:
                continue
            if upstream_id(row["tvg_id"], row["number"]) not in dial_upstreams:
                continue
            rows.append({"number": row["number"], "name": row["name"],
                         "url": row["url"], "tvg_id": row["tvg_id"],
                         "user_agent": row["user_agent"] or "",
                         "referrer": row["referrer"] or "", "dial": False})

    # One probe per distinct (URL, headers): the dial lists the same feed on an
    # SD and an HD row, and there is no reason to ask the origin twice.
    probes: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (row["url"], row["user_agent"], row["referrer"])
        probes.setdefault(key, []).append(row)

    results: list[ChannelResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(probe, url, agent, referrer, args.timeout, args.deep): key
            for key, (url, agent, referrer) in ((key, key) for key in probes)
        }
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            result = future.result()
            for row in probes[key]:
                results.append(ChannelResult(row["number"], row["name"], row["url"],
                                             row["tvg_id"], result,
                                             row.get("user_agent", ""),
                                             row.get("referrer", ""),
                                             row.get("dial", True)))

    at = now_iso()
    transitions, ok, failed = record_probes(conn, results, at)
    changes = record_sources(conn, default_sources(), at)
    # The generator reads this database with a second, read-only connection. It
    # must see this run's state, so commit before deciding or rebuilding.
    conn.commit()
    health = {row["url"]: row["status"] for row in conn.execute("SELECT url, status FROM channels")}
    failover = failover_needed(rows, health)
    # A dial row recovering does not change the dial — it was already there.
    # Only a watchlist feed coming back can add a row, or a live watchlist feed
    # beside a dead dial row can replace one.
    recovered_watch = any(t["kind"] == "recovered" and not t.get("dial", True)
                          for t in transitions)
    needs_rebuild = bool(changes) or recovered_watch or failover
    rebuilt = refreshed = False
    detail = ""
    if needs_rebuild:
        print(f"livetv-monitor: rebuild needed (source changes={len(changes)}, "
              f"transitions={len(transitions)}, failover={failover})")
    if needs_rebuild and not args.no_rebuild:
        try:
            rebuilt, refreshed = rebuild_dial(db, Path(args.guide), Path(args.out),
                                              not args.no_refresh)
            if not rebuilt:
                detail = "dial rebuild failed"
            elif not args.no_refresh and not refreshed:
                detail = "Jellyfin refresh failed"
        except subprocess.TimeoutExpired:
            detail = "dial rebuild/refresh timed out"
            print(f"livetv-monitor: {detail}", file=sys.stderr)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            print(f"livetv-monitor: dial rebuild failed: {detail}", file=sys.stderr)

    finished = now_iso()
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, rows_checked = ?, probes = ?, ok = ?,
               failed = ?, transitions = ?, source_changes = ?, rebuilt = ?,
               refreshed = ?, detail = ? WHERE id = ?
        """,
        (finished, len(results), len(probes), ok, failed, len(transitions),
         len(changes), int(rebuilt), int(refreshed), detail or None, run_id),
    )
    conn.commit()

    down = sorted(
        (row for row in conn.execute(
            "SELECT number, name, detail FROM channels WHERE status = 'fail' "
            "ORDER BY fail_streak DESC, number")),
        key=lambda row: lineup_mod._number_key(row["number"]),
    )
    summary = {
        "started_at": started,
        "finished_at": finished,
        "rows": len(results),
        "probes": len(probes),
        "ok": ok,
        "failed": failed,
        "transitions": transitions,
        "source_changes": changes,
        "rebuilt": rebuilt,
        "refreshed": refreshed,
        "down": [{"number": row["number"], "name": row["name"], "detail": row["detail"]}
                 for row in down[:100]],
        "down_count": len(down),
    }
    write_status(status_path, summary)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"livetv-monitor: checked {len(results)} rows ({len(probes)} probes) · "
              f"{ok} ok · {failed} failed · {len(transitions)} change(s) · "
              f"{len(changes)} source change(s)")
        for transition in transitions[:20]:
            print(f"  {transition['kind']:<9} {transition['number']:>7}  {transition['name']}")
        for change in changes:
            print(f"  source    {change['name']}  {change['location']}")
        if down:
            print(f"  {len(down)} feed(s) currently marked down; see {status_path}")
        if needs_rebuild and args.no_rebuild:
            print("  (rebuild was needed but --no-rebuild is set)")
        if not needs_rebuild:
            print("  nothing to change; the dial and Jellyfin are current")
    conn.close()
    lock.close()
    return 0 if not detail else 1


if __name__ == "__main__":
    sys.exit(main())
