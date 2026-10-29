#!/usr/bin/env python3
"""migrate-from-arr.py — carry the old stack's intent into the new one.

What is worth migrating is not the *arr databases; it is the decisions a
person made in them:

  * Prowlarr itself, as one aggregated indexer row — its search API already
    fans out to every tracker it knows;
  * what Sonarr and Radarr were still *missing* — the monitored episodes and
    films with no file — which becomes the new `wanted` queue;
  * the library itself, which is on disk and is picked up by a scan.

Everything else (history rows, quality definitions per series, delays) belongs
to services that are being retired. This script is idempotent: run it twice and
the second run adds nothing.

Usage
    python3 scripts/migrate-from-arr.py --prowlarr http://192.168.1.56:9696 \\
        --prowlarr-key KEY --sonarr http://192.168.1.56:8989 --sonarr-key KEY \\
        --radarr http://192.168.1.56:7878 --radarr-key KEY

    # write to a specific database instead of the default
    ... --db /docker/appdata/core/core.sqlite3

    # see the plan without touching anything
    ... --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unified"))

import httpx  # noqa: E402

from app import db  # noqa: E402

DEFAULT_DB = Path("/docker/appdata/core/core.sqlite3")


def api(client: httpx.Client, base: str, key: str, path: str, params: dict | None = None):
    response = client.get(f"{base.rstrip('/')}{path}",
                          params={**(params or {}), "apikey": key}, timeout=60)
    response.raise_for_status()
    return response.json()




def sonarr_missing(client, base: str, key: str) -> list[dict]:
    missing: list[dict] = []
    for series in api(client, base, key, "/api/v3/series"):
        if not series.get("monitored"):
            continue
        try:
            episodes = api(client, base, key, "/api/v3/episode",
                           {"seriesId": series["id"]})
        except httpx.HTTPError as exc:
            print(f"  ! {series.get('title')}: {exc}", file=sys.stderr)
            continue
        for episode in episodes:
            if episode.get("hasFile") or not episode.get("monitored"):
                continue
            if episode.get("seasonNumber", 0) == 0:  # specials stay out of the queue
                continue
            missing.append({
                "kind": "episode",
                "title": series.get("title", "").strip(),
                "year": (series.get("year") or None),
                "season": episode.get("seasonNumber"),
                "episode": episode.get("episodeNumber"),
            })
    return [row for row in missing if row["title"]]


def radarr_missing(client, base: str, key: str) -> list[dict]:
    missing: list[dict] = []
    for movie in api(client, base, key, "/api/v3/movie"):
        if not movie.get("monitored") or movie.get("hasFile"):
            continue
        if (movie.get("status") or "").lower() in {"announced", "in cinemas", "tba"}:
            continue
        missing.append({
            "kind": "movie",
            "title": (movie.get("title") or "").strip(),
            "year": movie.get("year") or None,
            "season": None,
            "episode": None,
        })
    return [row for row in missing if row["title"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--prowlarr")
    parser.add_argument("--prowlarr-key", default="")
    parser.add_argument("--sonarr")
    parser.add_argument("--sonarr-key", default="")
    parser.add_argument("--radarr")
    parser.add_argument("--radarr-key", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, change nothing")
    # A monitored Sonarr list is mostly back-catalogue: the first real library
    # here had 1800+ missing episodes of shows nobody is collecting. The queue
    # is only useful if it holds what someone actually wants now, so both
    # filters exist to keep a migration from starting a download stampede.
    parser.add_argument("--skip-episodes", action="store_true",
                        help="do not queue missing TV episodes")
    parser.add_argument("--skip-movies", action="store_true",
                        help="do not queue missing films")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="queue at most N wanted items (films first); 0 = all")
    args = parser.parse_args()

    if not any((args.prowlarr, args.sonarr, args.radarr)):
        parser.error("give at least one of --prowlarr, --sonarr, --radarr")

    indexers: list[dict] = []
    wanted: list[dict] = []

    with httpx.Client(follow_redirects=True) as client:
        if args.prowlarr:
            print(f"prowlarr: {args.prowlarr}")
            # One row, not one per tracker: Prowlarr's own search API already
            # fans out, and core applies its own ranking and cross-indexer
            # dedupe to the answer. Usenet indexers are skipped at query time
            # because Transmission is the only download client.
            indexers.append({
                "name": "Prowlarr",
                "url": args.prowlarr.rstrip("/"),
                "api_key": args.prowlarr_key,
                "cats": "",
                "kind": "prowlarr",
            })
            print("  1 aggregated indexer (every tracker Prowlarr knows, torrents only)")

        if args.sonarr:
            print(f"sonarr: {args.sonarr}")
            episodes = sonarr_missing(client, args.sonarr, args.sonarr_key)
            wanted.extend(episodes)
            print(f"  {len(episodes)} missing episodes")

        if args.radarr:
            print(f"radarr: {args.radarr}")
            movies = radarr_missing(client, args.radarr, args.radarr_key)
            wanted.extend(movies)
            print(f"  {len(movies)} missing films")

    if args.skip_episodes:
        wanted = [row for row in wanted if row["kind"] != "episode"]
    if args.skip_movies:
        wanted = [row for row in wanted if row["kind"] != "movie"]
    wanted.sort(key=lambda row: (row["kind"] != "movie", row["title"].lower(),
                                 row["season"] or 0, row["episode"] or 0))
    if args.limit and len(wanted) > args.limit:
        print(f"limit: keeping {args.limit} of {len(wanted)} wanted items", file=sys.stderr)
        wanted = wanted[:args.limit]

    print(f"\ntotal: {len(indexers)} indexers, {len(wanted)} wanted items")
    if args.dry_run:
        for row in indexers:
            print(f"  indexer  {row['name']:<24} {row['url']}")
        for row in wanted[:40]:
            suffix = (f" S{row['season']:02d}E{row['episode']:02d}"
                      if row["kind"] == "episode" else "")
            print(f"  wanted   {row['title']}{suffix} ({row['year'] or '—'})")
        if len(wanted) > 40:
            print(f"  … and {len(wanted) - 40} more")
        print("\ndry run: nothing written")
        return 0

    if not args.db.exists():
        print(f"FAIL no database at {args.db} — start the core service once first",
              file=sys.stderr)
        return 1

    connection = db.connect(args.db)
    try:
        added_indexers = 0
        for row in indexers:
            before = db.one(connection, "SELECT name FROM indexers WHERE name = ?",
                            (row["name"],))
            connection.execute(
                "INSERT INTO indexers(name, url, api_key, kind, cats, enabled)"
                " VALUES(?,?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET"
                " url = excluded.url, api_key = excluded.api_key,"
                " kind = excluded.kind, cats = excluded.cats",
                (row["name"], row["url"], row["api_key"], row["kind"], row["cats"]),
            )
            added_indexers += 0 if before else 1

        added_wanted = 0
        for row in wanted:
            before = db.one(
                connection,
                "SELECT id FROM wanted WHERE kind = ? AND lower(title) = lower(?)"
                " AND ifnull(year,0) = ifnull(?,0) AND ifnull(season,-1) = ifnull(?,-1)"
                " AND ifnull(episode,-1) = ifnull(?,-1)",
                (row["kind"], row["title"], row["year"], row["season"], row["episode"]),
            )
            db.add_wanted(connection, row["kind"], row["title"], row["year"],
                          row["season"], row["episode"])
            added_wanted += 0 if before else 1

        print(f"wrote {added_indexers} new indexers, {added_wanted} new wanted items")
        print(f"database: {args.db}")
        print("next: rescan the library in the UI, then start the scheduler")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
