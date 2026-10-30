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

So this reads the board's own database (sqlite, read-only) and asks each href — and
each icon — for an answer. It never edits a board.

WHAT COUNTS AS ALIVE
--------------------
Any 2xx or 3xx after redirects — and 401/403, because a gated sign-in page is a
working link that happens to want a session, which is most of them (`media`,
`req`, `tv`, … are behind one oauth2-proxy). Dead is: DNS that does not resolve,
a refused connection, a timeout, or a 4xx/5xx the app itself produced.

Icons are checked too, because a 404 icon draws a blank square that reads as a
loading state nobody waits out. They are third-party URLs (jsdelivr), so they are
reported per tile as `(icon)` and `--no-icons` skips them on a host with no egress.

The **status probe** (`ping_url`) is checked as well, because the board's dot is a
second question: a tile can load and its endpoint be down, or the endpoint can be
down while the link is fine - and a red dot with nothing behind it is the worst of
both. Those endpoints are often docker-internal (`http://monarch-health:8003/...`),
which cannot resolve from wherever this runs, so a *name-resolution* failure is a
note ("Homarr probes it from its own network") and only an answered-but-failing
probe is a finding.

WHAT IS NOT A FINDING YET (`KNOWN_DOWN`)
---------------------------------------
Three tiles name products that another estate's edge does not serve yet. They are
named and explained on every run, but they do not fail the check: a check that is
red forever is one nobody reads, which is how this class of failure started. Run
`--strict` after that edge is fixed, and delete the entry with it.

USAGE
-----
    python3 scripts/check-board-tiles.py                    # default board db
    python3 scripts/check-board-tiles.py /path/to/db.sqlite
    HOMARR_DB=/path/to/db.sqlite python3 scripts/check-board-tiles.py --timeout 15
    python3 scripts/check-board-tiles.py --strict           # KNOWN_DOWN counts too
    python3 scripts/check-board-tiles.py --no-icons         # links only

Exit codes, as everywhere in this repo: 0 = every tile answers (or is known down),
1 = at least one does not (each named), 2 = cannot run (no database, no sqlite3, no
board rows).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_DB = os.environ.get("HOMARR_DB", "/docker/appdata/homarr/appdata/db/db.sqlite")
USER_AGENT = "monarch-board-check/1"
# Answering, but not for the public: a gated page is a live tile.
ALIVE_STATUSES = (401, 403)
# A status probe that cannot even resolve is not a finding from a shell: the board's
# own probes run inside the compose network, where `monarch-health` is a name.
import re  # noqa: E402 - kept beside its only use, below the constants
DNS_FAILURES = re.compile(r"name or service not known|temporary failure in name "
                          r"resolution|no address associated|nodename nor servname|\[Errno -[235]\]",
                          re.IGNORECASE)


# Names known not to answer, and why. A name here is still reported - named, with
# its reason, on every run - but it does not fail the check: these are another
# estate's missing vhosts, not this host's drift, and a check that is red forever
# is one nobody reads. `--strict` turns them into findings.
#
# The three below are a safety net, not the live state: their tiles were removed
# from the board design on 2026-10-30 (`DEAD` in scripts/seed-homarr-board.py), so
# the dashboard no longer carries a link that can only fail. The entries stay for
# the cheap failure - somebody re-adds one of these hrefs by hand before its edge
# catches up - which should read "known down, here is why" rather than page.
KNOWN_DOWN = {
    # The three names below resolve to 73.68.203.71, and the edge answering there
    # has no vhost or certificate for them: TLS reports UNRECOGNIZED_NAME and the
    # connection is refused (measured 2026-10-30, from the media host and from a
    # workstation on another network). The products themselves are provisioned on
    # the voice estate at 192.168.1.30 behind the i4 proxy (scripts/npm-pbx-repoint.sh
    # repoints those rows), so the product is real and the name is what the edge
    # does not serve yet.
    "dashboard.capstone.innotel.us":
        "the Capstone UI is proxied by the i4 edge (192.168.1.30:3010); the edge this "
        "name resolves to has no vhost for it",
    "subscribe.zeus.innotel.us":
        "the Zeus subscribe page is served from the voice estate; the edge this name "
        "resolves to has no vhost for it",
    "pbx.zeus.innotel.us":
        "the Zeus PBX admin is served from the voice estate; the edge this name "
        "resolves to has no vhost for it",
}


class CannotRun(RuntimeError):
    """Not enough on disk to judge the board (no database, no rows)."""


def tiles(db_path: Path) -> list[tuple[str, str, str, str]]:
    """(name, href, icon_url, ping_url) for every app on the board, live or stale.

    `app` rows are the tiles; the board also has widgets, but a widget has neither
    an href nor an icon to check. A name can carry the same href twice (two tiles
    to one service), so the caller probes each URL once while still reporting per
    tile.
    """
    if not db_path.is_file():
        raise CannotRun(f"no Homarr database at {db_path} - is the dashboard on this host? "
                        "(set HOMARR_DB, or pass the path)")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT name, href, icon_url, ping_url FROM app ORDER BY name").fetchall()
    except sqlite3.Error as error:
        raise CannotRun(f"cannot read {db_path} ({error})") from None
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a connection that failed to open
            pass
    if not rows:
        raise CannotRun(f"{db_path} has no app rows - the board has not been seeded")
    return [(row["name"], (row["href"] or "").strip(), (row["icon_url"] or "").strip(),
             (row["ping_url"] or "").strip()) for row in rows]


def probe(href: str, timeout: float) -> tuple[bool, str]:
    """(answers?, what happened). Redirects are followed by urllib."""
    request = urllib.request.Request(href, method="GET",
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        # An HTTPError *is* the response, not just an exception: it holds the socket
        # open until close(), and urllib does not close it for you. Left to the
        # garbage collector this leaked one socket per gated or 4xx probe - 81
        # ResourceWarnings in a single suite run, which is how it was found, and
        # noise like that is what hides the next real warning.
        code = error.code
        try:
            error.close()
        except Exception:  # noqa: BLE001 - a leaked socket must not fail the probe
            pass
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
    parser.add_argument("db", nargs="?", default=DEFAULT_DB,
                        help=f"Homarr db (default {DEFAULT_DB})")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="seconds to wait for one tile (default 8)")
    parser.add_argument("--strict", action="store_true",
                        help="treat a KNOWN_DOWN tile as a finding (after its edge is fixed)")
    parser.add_argument("--no-icons", action="store_true",
                        help="check links only (a host with no egress to the icon CDN)")
    parser.add_argument("--no-ping", action="store_true",
                        help="do not check the status probes (ping_url)")
    args = parser.parse_args(argv)

    try:
        rows = tiles(Path(args.db))
    except CannotRun as error:
        print(f"check-board-tiles: {error}", file=sys.stderr)
        return 2

    answered: dict[str, tuple[bool, str]] = {}
    icons: dict[str, tuple[bool, str]] = {}
    dead: list[str] = []
    dead_tiles: set[str] = set()
    known: list[str] = []
    for name, href, _icon, _ping in rows:
        if not href:
            dead.append(f"{name}: no href - the tile cannot be clicked")
            dead_tiles.add(name)
            continue
        if href not in answered:
            answered[href] = probe(href, args.timeout)
        ok, detail = answered[href]
        if ok:
            continue
        host = urllib.parse.urlsplit(href).hostname or ""
        reason = KNOWN_DOWN.get(host)
        if reason and not args.strict:
            known.append(f"{name} → {href}: known down - {reason}")
        else:
            dead.append(f"{name} → {href}: {detail}")
            dead_tiles.add(name)

    if not args.no_icons:
        for name, _href, icon, _ping in rows:
            if not icon:
                continue
            if icon not in icons:
                icons[icon] = probe(icon, args.timeout)
            ok, detail = icons[icon]
            if not ok:
                dead.append(f"{name} (icon) → {icon}: {detail}")
                dead_tiles.add(name)

    # The status probe: a second question from the same tile. Only distinct URLs
    # that are not already the href are worth asking again. A probe note is kept
    # apart from KNOWN_DOWN: one is a tile that does not answer, the other is a
    # probe this machine cannot reach, and reporting them as one number is how a
    # reader learns to skip the summary.
    probe_notes: list[str] = []
    status: dict[str, tuple[bool, str]] = {}
    if not args.no_ping:
        for name, href, _icon, ping in rows:
            if not ping or ping == href:
                continue
            if ping not in status:
                status[ping] = probe(ping, args.timeout)
            ok, detail = status[ping]
            if ok:
                continue
            if DNS_FAILURES.search(detail):
                probe_notes.append(f"{name} (status) → {ping}: not resolvable from here "
                                   f"({detail}) - Homarr probes it from its own network")
            else:
                dead.append(f"{name} (status) → {ping}: {detail}")
                dead_tiles.add(name)

    for line in known:
        print(f"  note  {line}")
    for line in probe_notes:
        print(f"  note  {line}")
    for line in dead:
        print(f"  FAIL  {line}")
    if dead:
        print(f"\nFAIL — {len(dead_tiles)} of {len(rows)} tile(s) have a problem: the board "
              "links somewhere that does not")
        return 1
    probe_tail = (f"; {len(probe_notes)} status probe(s) not resolvable from here"
                  if probe_notes else "")
    if known:
        print(f"\n  ok    {len(rows) - len(known)} of {len(rows)} tile(s) answer; "
              f"{len(known)} known down (named above, not hidden — --strict makes "
              f"them findings)"
              + (f"; {len(icons)} icon(s) answer" if icons else "") + probe_tail)
        return 0
    if icons:
        print(f"  ok    {len(answered)} link(s), {len(icons)} icon(s) answer{probe_tail}")
        return 0
    print(f"  ok    all {len(rows)} tile(s) answer "
          f"({len(answered)} distinct URL(s) probed){probe_tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
