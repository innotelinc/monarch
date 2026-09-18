#!/usr/bin/env python3
"""Seed every Homarr v1 board with the designed Innotel dashboard (sqlite).

Homarr v1 (homarr-labs) stores boards, sections, apps, tiles and layout in a
SQLite database under /appdata/db/db.sqlite (this repo mounts
/docker/appdata/homarr/appdata). The v0 `configs/default.json` seeding no longer
applies, so this script is the operative seeder, and it is the DESIGN: the
`DESIGN` list below is what the dashboard is, and re-running it is how a change
reaches it.

WHAT IT DOES, per board in the database (every board, not just the first - a
second board is what an operator gets after a teammate saves their own, and a
half-updated dashboard is worse than an unwritten one):

* upserts an app row per tile and repairs a stale href,
* drops the tiles - and the app rows - of services that cannot answer (retired
  ones, and names with no DNS record at all: a row pointing at a name that does
  not resolve is a dead entry in every picker, so it goes rather than lingers),
* creates each designed SECTION by name, orders them top to bottom, and places
  every tile inside the section it belongs to at a deterministic x/y,
* removes tiles, sections and placements the design does not name, so the board
  converges on the design instead of growing a new bucket every run,
* sets the home board for every user.

Usage (run on the Monarch host; Homarr may be running - the writes are
transactional and sqlite is WAL):

    python3 scripts/seed-homarr-board.py [path/to/db.sqlite]

WHY THE TILE SET IS VERIFIED, NOT ASSUMED. `DEAD` names the hrefs that resolve
to nothing: the retired services (their compose entries are
`profiles: ["legacy"]`) and the platforms whose public names were never created
in NPM/DNS. Both were tiles on the live board that could only ever fail, which
is what "the dashboard has broken links" means. Add a name here only after
`getent hosts <name>` answers on the deployment host.
"""
import json
import os
import secrets
import sqlite3
import string
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "/docker/appdata/homarr/appdata/db/db.sqlite"
ICON = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/{name}.png"

# name -> (href, icon, tooltip). href doubles as the ping URL (status check).
# Every href is a hostname that exists in Nginx Proxy Manager AND resolves on
# the deployment host - the old `*.monarch.local` set resolved to nothing, so a
# tile for one was dead no matter how healthy the container was.
APPS = {
    # -- Playback -----------------------------------------------------------
    "Jellyfin": ("https://media.innotel.us", "jellyfin",
                 "Jellyfin - stream movies & TV"),
    "Jellyseerr": ("https://req.innotel.us", "jellyseerr",
                   "Jellyseerr - request movies & TV"),
    "Requestrr": ("https://requestrr.monarch.innotel.us", "discord",
                  "Requestrr - Discord request bot (admin console)"),
    "IPTV (EPG)": ("https://tv.monarch.innotel.us", "tvheadend",
                   "IPTV - live TV guide (XMLTV EPG)"),
    # -- Libraries ----------------------------------------------------------
    "Sonarr": ("https://sonarr.monarch.innotel.us", "sonarr",
               "Sonarr - TV collection manager"),
    "Radarr": ("https://radarr.monarch.innotel.us", "radarr",
               "Radarr - movie collection manager"),
    "Lidarr": ("https://lidarr.monarch.innotel.us", "lidarr",
               "Lidarr - music collection manager"),
    "Whisparr": ("https://whisparr.monarch.innotel.us", "whisparr",
                 "Whisparr - adult collection manager"),
    "Bazarr": ("https://bazarr.monarch.innotel.us", "bazarr",
               "Bazarr - subtitle manager"),
    # -- Indexers & downloads ----------------------------------------------
    "Prowlarr": ("https://prowlarr.monarch.innotel.us", "prowlarr",
                 "Prowlarr - indexer manager"),
    "qBittorrent": ("https://qbittorrent.monarch.innotel.us", "qbittorrent",
                    "qBittorrent - torrent client"),
    "SABnzbd": ("https://sabnzbd.monarch.innotel.us", "sabnzbd",
                "SABnzbd - usenet client"),
    # -- Business -----------------------------------------------------------
    "Magnate": ("https://app.magnate.innotel.us", "stripe",
                "Magnate - billing platform & admin portal"),
    "Subscribe": ("https://subscribe.innotel.us", "stripe",
                  "Subscribe - the portal landing page: pick a plan, pay via Stripe, manage your account (Magnate)"),
    "Subscribe (Zeus)": ("https://subscribe.zeus.innotel.us", "asterisk",
                         "Zeus subscription page - phone plan + Capstone voice agents add-on"),
    "Capstone": ("https://dashboard.capstone.innotel.us", "openai",
                 "Capstone - voice AI agent platform dashboard"),
    "Zeus PBX": ("https://pbx.zeus.innotel.us", "asterisk",
                 "Zeus - PBX / VoIP (Asterisk + coturn)"),
    "AvantFAX": ("https://fax.zeus.innotel.us", "files",
                 "AvantFAX - fax service"),
    "AthenIQ Learn": ("https://learn.innotel.us", "moodle",
                      "AthenIQ - LMS / learning platform"),
    "AthenIQ Studio": ("https://studio.innotel.us", "code",
                       "AthenIQ Studio - course authoring"),
    "Signara": ("https://app.signara.innotel.us", "vault",
                "Signara - trust / certificate signing portal"),
    # -- Platform -----------------------------------------------------------
    "Olympus": ("https://olympus.innotel.us", "openai", "Olympus - AI platform"),
    "Olympus Studio": ("https://studio.olympus.innotel.us", "code",
                       "Olympus Studio - app builder"),
    "Atlas": ("https://atlas.innotel.us", "gitea", "Atlas - DevOps / coding platform"),
    "ZapIt": ("https://zapit.innotel.us", "linkwarden", "ZapIt - short links"),
    "Monarch": ("https://monarch.innotel.us", "jellyfin",
                "Monarch - this dashboard (apex origin)"),
    # -- Infrastructure -----------------------------------------------------
    "Cerulean SSO": ("https://auth.cerulean.innotel.us", "authentik",
                     "Cerulean - identity provider (Authentik SSO)"),
    "Nginx Proxy": ("https://admin.monarch.innotel.us", "nginx-proxy-manager",
                    "Nginx Proxy Manager - edge admin UI"),
}

# Rows and tiles to REMOVE. Two kinds of nothing:
#   * retired services - their compose entries are `profiles: ["legacy"]`, so no
#     host runs them and no name fronts them;
#   * names that never existed in NPM/DNS (`.monarch.local` for all of them,
#     plus platforms that publish under a different domain), which is why the
#     `Transmission`/`Onyx`/`Oasis`/`Rizzaura` tiles 000'd on the live board.
# The app row is deleted with the tile: an entry that can only fail is exactly
# what an operator should not have to tell apart from a working one, and
# re-adding one is a single line in APPS above.
DEAD = (
    "Autobrr",
    "ClipBucket",
    "Deluge",
    "Dispatcharr",
    "Monarch Recs",
    "NextPVR",
    "Onyx",
    "Oasis",
    "Rizzaura",
    "TVHeadend",
    "Transmission",
)

# Any app still pointing at one of these names is stale by construction.
STALE_SUFFIXES = (".monarch.local",)
STALE_HOSTS = {
    "media.monarch.innotel.us": "media.innotel.us",
    "req.monarch.innotel.us": "req.innotel.us",
    "recs.monarch.innotel.us": "monarch.innotel.us",
    # The old LAN tile: Requestrr's console has a gate and a name now, so a
    # private address is both wrong and unusable from off the LAN.
    "http://192.168.1.46:4545": "https://requestrr.monarch.innotel.us",
}


def app_tile(name: str, width: int = 1):
    return ("app", name, width)


def widget(kind: str, width: int = 1):
    """A Homarr widget tile, by its `item.kind` (clock, mediaServer, ...)."""
    return ("widget", kind, width)


# The design, top to bottom. Each section is (title, tiles), and each tile is
# (kind, name, width) - width in grid columns of the board's layout (10 here),
# so a row of five 1-wide app tiles fills half the board and a 2-wide widget
# takes two. Tiles are placed left to right in the order written, and every
# section starts on its own row.
DESIGN = (
    ("Now", (
        widget("clock"),
        widget("weather"),
        widget("calendar", 2),
        widget("bookmarks"),
    )),
    ("Playback", (
        app_tile("Jellyfin"),
        app_tile("Jellyseerr"),
        app_tile("Requestrr"),
        app_tile("IPTV (EPG)"),
    )),
    ("Media activity", (
        widget("mediaServer", 2),
        widget("downloads", 2),
        widget("mediaReleases", 3),
        widget("mediaMissing", 2),
        widget("mediaRequests-requestStats"),
    )),
    ("Libraries", (
        app_tile("Sonarr"),
        app_tile("Radarr"),
        app_tile("Lidarr"),
        app_tile("Whisparr"),
        app_tile("Bazarr"),
        widget("bazarr", 2),
    )),
    ("Indexers & downloads", (
        app_tile("Prowlarr"),
        app_tile("qBittorrent"),
        app_tile("SABnzbd"),
        widget("indexerManager", 2),
        widget("mediaRequests-requestList", 3),
    )),
    ("Business", (
        app_tile("Magnate"),
        app_tile("Subscribe"),
        app_tile("Subscribe (Zeus)"),
        app_tile("Capstone"),
        app_tile("Zeus PBX"),
        app_tile("AvantFAX"),
        app_tile("AthenIQ Learn"),
        app_tile("AthenIQ Studio"),
        app_tile("Signara"),
    )),
    ("Platform", (
        app_tile("Olympus"),
        app_tile("Olympus Studio"),
        app_tile("Atlas"),
        app_tile("ZapIt"),
        app_tile("Monarch"),
    )),
    ("Infrastructure", (
        app_tile("Cerulean SSO"),
        app_tile("Nginx Proxy"),
    )),
)

BOARD_TITLE = "Innotel dashboard"


def repair_stale_url(href: str) -> tuple[str, bool]:
    """Rewrite a board href that points at a name with no DNS record."""
    if not href:
        return href, False
    for old, new in STALE_HOSTS.items():
        if old in href:
            return href.replace(old, new), True
    for suffix in STALE_SUFFIXES:
        if suffix in href:
            return href.replace(suffix, ".monarch.innotel.us"), True
    return href, False


def nanoid(n=25):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def item_options(app_id: str) -> str:
    return json.dumps({"json": {"appId": app_id, "openInNewTab": True, "showTitle": True}})


def drop_dead(db, board_id: str) -> int:
    """Remove every tile and app row the design no longer carries.

    A removed app leaves its tiles behind unless they go together, and a tile
    whose app row is gone renders as an empty box - so both sides are deleted,
    for the dead list and for anything else not in APPS or DESIGN.
    """
    wanted_apps = {name for _title, tiles in DESIGN for kind, name, _w in tiles if kind == "app"}
    wanted_widgets = {name for _title, tiles in DESIGN for kind, name, _w in tiles if kind == "widget"}
    removed = 0
    apps = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM app")}
    for item in db.execute("SELECT id, kind, options FROM item WHERE board_id=?",
                           (board_id,)).fetchall():
        label = item["kind"]
        if item["kind"] == "app":
            try:
                label = apps.get(json.loads(item["options"])["json"].get("appId"), item["kind"])
            except Exception:
                label = item["kind"]
            if label in wanted_apps:
                continue
        elif item["kind"] in wanted_widgets:
            continue
        db.execute("DELETE FROM item_layout WHERE item_id=?", (item["id"],))
        db.execute("DELETE FROM item WHERE id=?", (item["id"],))
        removed += 1
        print(f"  removed tile: {label}")
    # The app rows go with their tiles: a row whose name resolves to nothing is
    # a dead entry in the app picker, and the design is where it comes back from.
    for app_id, name in list(apps.items()):
        if name in wanted_apps:
            continue
        db.execute("DELETE FROM app WHERE id=?", (app_id,))
        print(f"  removed app row: {name}")
    return removed


def ensure_sections(db, board_id: str) -> dict[str, sqlite3.Row]:
    """The design's sections, created where missing and removed where unplanned.

    A board that has only ever had Homarr's own default carries one section with
    no name at all. Deleting it and inserting the design's sections would throw
    away the section every existing tile is placed in, so the unnamed one is
    ADOPTED for the first title that needs a section instead - the tiles are
    re-placed either way, and the board keeps the id it was rendering.
    """
    wanted = [title for title, _tiles in DESIGN]
    rows = db.execute("SELECT id, name FROM section WHERE board_id=?", (board_id,)).fetchall()
    by_name = {row["name"]: row["id"] for row in rows if row["name"]}
    unnamed = [row["id"] for row in rows if not row["name"]]
    sections: dict[str, str] = {}
    for title in wanted:
        if title in by_name:
            sections[title] = by_name[title]
            continue
        if unnamed:
            section_id = unnamed.pop()
            db.execute("UPDATE section SET name=? WHERE id=?", (title, section_id))
            print(f"  named the board's unnamed section {title!r}")
        else:
            section_id = nanoid()
            db.execute(
                "INSERT INTO section (id, board_id, kind, x_offset, y_offset, name, options) "
                "VALUES (?,?,?,?,?,?,?)",
                (section_id, board_id, "empty", 0, wanted.index(title), title, '{"json": {}}'))
            print(f"  added section: {title!r}")
        sections[title] = section_id
    keep = set(sections.values())
    for row in rows:
        if row["id"] in keep:
            continue
        db.execute("DELETE FROM item_layout WHERE section_id=?", (row["id"],))
        db.execute("DELETE FROM section WHERE id=?", (row["id"],))
        print(f"  removed section {row['name']!r}" if row["name"] else "  removed an extra section")
    return sections


def place_tiles(db, board_id: str, layout, sections: dict[str, str],
                by_name: dict[str, str]) -> int:
    """Put every designed tile in its section, at a deterministic x/y.

    The row cursor runs across the whole board rather than restarting per
    section, and each section records the row it starts on: sections stack top
    to bottom, and the two readings of `y_offset` (absolute on the item,
    relative to the section) then agree instead of overlapping.
    """
    cols = layout["column_count"] or 10
    cursor_y = 0
    widgets: dict[str, str] = {}
    for row in db.execute("SELECT id, kind FROM item WHERE board_id=? AND kind != 'app'",
                          (board_id,)):
        widgets.setdefault(row["kind"], row["id"])
    placed = 0
    for title, tiles in DESIGN:
        section_id = sections[title]
        db.execute("UPDATE section SET y_offset=? WHERE id=?", (cursor_y, section_id))
        x = 0
        for kind, name, width in tiles:
            if kind == "app":
                item_id = by_name.get(name)
            else:
                item_id = widgets.get(name)
                if item_id is None:
                    print(f"  skipped widget {name!r} (this board has no such tile)")
                    continue
            if item_id is None:
                print(f"  skipped tile {name!r} (no item)")
                continue
            if x + width > cols:
                x = 0
                cursor_y += 1
            db.execute("DELETE FROM item_layout WHERE item_id=? AND layout_id=?",
                       (item_id, layout["id"]))
            db.execute(
                "INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, "
                "width, height) VALUES (?,?,?,?,?,?,?)",
                (item_id, section_id, layout["id"], x, cursor_y, width, 1),
            )
            placed += 1
            x += width
        cursor_y += 1
    return placed


def ensure_apps(db) -> dict[str, str]:
    """Upsert the designed apps, repair stale hrefs, and return name -> item id."""
    existing = {row["name"]: row for row in db.execute("SELECT * FROM app").fetchall()}
    app_ids: dict[str, str] = {}
    for name, (href, icon_name, tooltip) in APPS.items():
        icon = ICON.format(name=icon_name)
        row = existing.get(name)
        if row is None:
            app_id = nanoid()
            db.execute("INSERT INTO app (id, name, description, icon_url, href, ping_url) "
                       "VALUES (?,?,?,?,?,?)", (app_id, name, tooltip, icon, href, href))
            print(f"  added app: {name} -> {href}")
        else:
            app_id = row["id"]
            if (row["href"], row["ping_url"], row["icon_url"]) != (href, href, icon):
                db.execute("UPDATE app SET href=?, ping_url=?, icon_url=?, description=? WHERE id=?",
                           (href, href, icon, tooltip, app_id))
                print(f"  updated app: {name} -> {href}")
        app_ids[name] = app_id

    # Repair a stale href on any app the design does not own before deciding it
    # is dead - the board used to carry `*.monarch.local` links, which resolve to
    # nothing, and a tile for one was dead however healthy its container was.
    for name, row in existing.items():
        if name in APPS or name in DEAD:
            continue
        fixed, changed = repair_stale_url(row["href"] or "")
        if changed:
            db.execute("UPDATE app SET href=?, ping_url=? WHERE id=?", (fixed, fixed, row["id"]))
            print(f"  repaired stale URL: {name} -> {fixed}")
    return app_ids


def ensure_items(db, board_id: str, app_ids: dict[str, str]) -> dict[str, str]:
    """An app tile per designed app, reusing an existing one where there is one.

    Returns NAME -> item id, because that is what the design names its tiles by;
    the app id is the database's business in between.
    """
    by_app_id: dict[str, str] = {}
    for row in db.execute("SELECT id, options FROM item WHERE board_id=? AND kind='app'",
                          (board_id,)).fetchall():
        try:
            app_id = json.loads(row["options"])["json"].get("appId")
        except Exception:
            continue
        by_app_id.setdefault(app_id, row["id"])
    by_name: dict[str, str] = {}
    for name, app_id in app_ids.items():
        item_id = by_app_id.get(app_id)
        if item_id is None:
            item_id = nanoid()
            db.execute("INSERT INTO item (id, board_id, kind, options, advanced_options) "
                       "VALUES (?,?,?,?,?)",
                       (item_id, board_id, "app", item_options(app_id), '{"json": {}}'))
            print(f"  added tile: {name}")
        by_name[name] = item_id
    return by_name


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"DB not found: {DB_PATH}")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        boards = db.execute("SELECT id, name FROM board ORDER BY name").fetchall()
        if not boards:
            sys.exit("No board found - finish Homarr onboarding first (a board must exist).")
        total_placed = 0
        for board in boards:
            print(f"board: {board['name']} ({board['id']})")
            layouts = db.execute("SELECT * FROM layout WHERE board_id=? ORDER BY name",
                                 (board["id"],)).fetchall()
            if not layouts:
                print("  no layout - open it once in the UI first, skipping")
                continue
            app_ids = ensure_apps(db)
            items = ensure_items(db, board["id"], app_ids)
            sections = ensure_sections(db, board["id"])
            placed = place_tiles(db, board["id"], layouts[0], sections, items)
            removed = drop_dead(db, board["id"])
            total_placed += placed
            print(f"  {placed} tile(s) placed in {len(DESIGN)} sections, {removed} removed")

        # The board is the dashboard: a title on the tab, and the apex/home board
        # for every user (an existing user keeps their own choice only if they
        # made one; this sets the ones that never did).
        first = boards[0]["id"]
        db.execute("UPDATE board SET page_title=?, meta_title=? WHERE id=?",
                   (BOARD_TITLE, BOARD_TITLE, first))
        db.execute("UPDATE serverSetting SET value=? WHERE setting_key='board'",
                   (json.dumps({"json": {"homeBoardId": first, "mobileHomeBoardId": first,
                                         "enableStatusByDefault": True,
                                         "forceDisableStatus": False}}),))
        for board in boards:
            db.execute("UPDATE user SET home_board_id=?, mobile_home_board_id=? "
                       "WHERE home_board_id IS NULL OR home_board_id=''",
                       (board["id"], board["id"]))
        db.commit()
        print(f"\nDone: {len(boards)} board(s), {total_placed} tiles placed across "
              f"{len(DESIGN)} sections ({', '.join(title for title, _ in DESIGN)}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
