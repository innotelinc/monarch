#!/usr/bin/env python3
"""check-board-tiles.py — does every tile on the Homarr board still answer?

WHY THIS EXISTS
---------------
The dashboard is the front door, and its tiles are the only links an operator uses
without thinking. Every one of them fails silently: the board carried
`*.monarch.local` hrefs for months because nothing ever clicked them, and a tile
whose service was retired, renamed or moved keeps rendering exactly like a healthy
one. `scripts/seed-homarr-board.py` fixes the designed tiles, but a tile a person
added by hand, or a hostname somebody removed from Nginx Proxy Manager afterwards,
is invisible to it.

So this reads the board's own database (sqlite, read-only) and asks each href for an
answer. It never edits a board.

WHAT COUNTS AS ALIVE
--------------------
Any 2xx or 3xx after redirects — and 401/403, because a gated sign-in page is a
working link that happens to want a session, which is most of them (`media`,
`req`, `tv`, … are behind one oauth2-proxy). Dead is: DNS that does not resolve,
a refused connection, a timeout, or a 4xx/5xx the app itself produced.

USAGE
-----
    python3 scripts/check-board-tiles.py                    # default board db
    python3 scripts/check-board-tiles.py /path/to/db.sqlite
    HOMARR_DB=/path/to/db.sqlite python3 scripts/check-board-tiles.py --timeout 15

Exit codes, as everywhere in this repo: 0 = every tile answers, 1 = at least one
does not (each named), 2 = cannot run (no database, no sqlite3, no board rows).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DB = os.environ.get("HOMARR_DB", "/docker/appdata/homarr/appdata/db/db.sqlite")
USER_AGENT = "monarch-board-check/1"
# Answering, but not for the public: a gated page is a live tile.
ALIVE_STATUSES = (401, 403)


class CannotRun(RuntimeError):
    """Not enough on disk to judge the board (no database, no rows)."""


def tiles(db_path: Path) -> list[tuple[str, str]]:
    """(name, href) for every app on the board, live or stale.

    `app` rows are the tiles; the board also has widgets, but a widget has no href
    to check. A name can carry the same href twice (two tiles to one service), so
    the caller probes each URL once while still reporting per tile.
    """
    if not db_path.is_file():
        raise CannotRun(f"no Homarr database at {db_path} - is the dashboard on this host? "
                        "(set HOMARR_DB, or pass the path)")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT name, href FROM app ORDER BY name").fetchall()
    except sqlite3.Error as error:
        raise CannotRun(f"cannot read {db_path} ({error})") from None
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a connection that failed to open
            pass
    if not rows:
        raise CannotRun(f"{db_path} has no app rows - the board has not been seeded")
    return [(row["name"], (row["href"] or "").strip()) for row in rows]


def probe(href: str, timeout: float) -> tuple[bool, str]:
    """(answers?, what happened). Redirects are followed by urllib."""
    request = urllib.request.Request(href, method="GET",
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        return False, f"cannot connect ({reason})"
    if code in ALIVE_STATUSES:
        return True, f"HTTP {code} (gated, but answering)"
    if 200 <= code < 400:
        return True, f"HTTP {code}"
    return False, f"HTTP {code}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", nargs="?", default=DEFAULT_DB, help=f"Homarr db (default {DEFAULT_DB})")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="seconds to wait for one tile (default 8)")
    args = parser.parse_args(argv)

    try:
        rows = tiles(Path(args.db))
    except CannotRun as error:
        print(f"check-board-tiles: {error}", file=sys.stderr)
        return 2

    answered: dict[str, tuple[bool, str]] = {}
    dead: list[str] = []
    for name, href in rows:
        if not href:
            dead.append(f"{name}: no href - the tile cannot be clicked")
            continue
        if href not in answered:
            answered[href] = probe(href, args.timeout)
        ok, detail = answered[href]
        if not ok:
            dead.append(f"{name} → {href}: {detail}")

    for line in dead:
        print(f"  FAIL  {line}")
    if dead:
        print(f"\nFAIL — {len(dead)} of {len(rows)} tile(s) do not answer: the board links "
              "somewhere that does not")
        return 1
    print(f"  ok    all {len(rows)} tile(s) answer "
          f"({len(answered)} distinct URL(s) probed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
