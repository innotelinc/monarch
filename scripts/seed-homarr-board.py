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
* keeps the board's one main canvas section (Homarr v1 allows exactly one
  `empty` section per lane, and a second one is the "has multiple main canvas
  roots" error that stops the board rendering) and creates each designed group
  under it as a `container`, ordered top to bottom,
* places every tile inside the container it belongs to at a deterministic x/y,
  on every layout the board has, and records each container's own geometry in
  `section_layout`,
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

# Homarr v1 keeps exactly one `empty` (canvas) section per lane and renders every
# named group as a `container` nested inside it. The board's old shape - one
# `empty` section per group - is what `getRootSectionForLane` throws
# "has multiple main canvas roots" on, so the design's titles are containers and
# the lane root is a section of its own.
MAIN_LANE_OFFSET = 0
EMPTY_OPTIONS = '{"json": {}}'


def container_options(title: str) -> str:
    """The options a container section carries (Homarr's schema defaults)."""
    return json.dumps({"json": {
        "title": title[:64],
        "customCssClasses": [],
        "borderColor": "",
        "showLabel": True,
        "collapsible": True,
        "showOpenAll": True,
    }})

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
    "Sonarr": ("https://tv.innotel.us", "sonarr",
               "Sonarr - TV collection manager"),
    "Radarr": ("https://movies.innotel.us", "radarr",
               "Radarr - movie collection manager"),
    "Lidarr": ("https://mp3.innotel.us", "lidarr",
               "Lidarr - music collection manager"),
    "Whisparr": ("https://xxx.innotel.us", "whisparr",
                 "Whisparr - adult collection manager"),
    "Bazarr": ("https://bazarr.monarch.innotel.us", "bazarr",
               "Bazarr - subtitle manager"),
    # -- Indexers & downloads ----------------------------------------------
    "Prowlarr": ("https://prowlarr.monarch.innotel.us", "prowlarr",
                 "Prowlarr - indexer manager"),
    "Transmission": ("https://transmission.monarch.innotel.us", "transmission",
                     "Transmission - torrent client"),
    "SABnzbd": ("https://sabnzbd.monarch.innotel.us", "sabnzbd",
                "SABnzbd - usenet client"),
    # -- Business -----------------------------------------------------------
    "Magnate": ("https://app.magnate.innotel.us", "stripe",
                "Magnate - billing platform & admin portal"),
    "Subscribe": ("https://subscribe.innotel.us", "stripe",
                  "Subscribe - the portal landing page: pick a plan, pay via Stripe, manage your account (Magnate)"),
    # Capstone, Subscribe (Zeus) and Zeus PBX are in DEAD below, not missing by
    # accident: on 2026-10-30 none of their names answered at the public edge
    # (`TLSV1_UNRECOGNIZED_NAME` on dashboard.capstone.innotel.us,
    # subscribe.zeus.innotel.us and pbx.zeus.innotel.us, checked from the media host
    # and from a workstation, and `capstone.innotel.us` now 301s to `pbx.it.com`).
    # A tile that can only fail is worse than no tile, so bring them back by adding
    # the row here again only once the edge serves the name.
    "AvantFAX": ("https://fax.zeus.innotel.us", "files",
                 "AvantFAX - fax service"),
    "Signara": ("https://app.signara.innotel.us", "vault",
                "Signara - trust / certificate signing portal"),
    # -- Platform -----------------------------------------------------------
    # Olympus is decommissioned (2026-09-30) and its names no longer answer, so
    # its tiles are gone rather than left as dead links. AthenIQ is in the same
    # state now: its `.59` instance is gone and `learn`/`studio.innotel.us`
    # answer 502, so those tiles are gone too (see DEAD).
    # Genie is its replacement and is deployed, so it carries the tile Olympus
    # and AthenIQ Studio used to.
    "Genie": ("https://genie.innotel.us", "code",
              "Genie - browser coding console (watch and gate an agent's work)"),
    "Atlas": ("https://atlas.innotel.us", "gitea", "Atlas - DevOps / coding platform"),
    "ZapIt": ("https://zapit.innotel.us", "linkwarden", "ZapIt - short links"),
    "Monarch": ("https://monarch.innotel.us", "jellyfin",
                "Monarch - this dashboard (apex origin)"),
    # Straight at the endpoint a client is handed before it has a session, so the
    # state of the splash is readable without a shell on the media host: the crest
    # means the guard is holding, a poster wall means a scan rewrote the collage
    # and nothing has put the design back. This is the visible half of both the
    # splash and the guard, and the status dot on the tile is whether Jellyfin
    # answers at all.
    "Splash (live)": ("https://media.innotel.us/Branding/Splashscreen", "jellyfin",
                      "Monarch splash - the image every client gets before sign-in"),
    # -- Infrastructure -----------------------------------------------------
    "Cerulean SSO": ("https://auth.cerulean.innotel.us", "authentik",
                     "Cerulean - identity provider (Authentik SSO)"),
    "Nginx Proxy": ("https://admin.monarch.innotel.us", "nginx-proxy-manager",
                    "Nginx Proxy Manager - edge admin UI"),
    # The one question no other tile can ask, and the one that was invisible for 35
    # commits: is the code running here the code in the branch? It answers 200 only
    # while the checkout is origin/main with a clean tree and 503 when it is not, so
    # a stale deployment is a red dot instead of a green board over old code.
    # `health.monarch.innotel.us`, not `health.innotel.us`: the conf row is `health`
    # under MONARCH_DOMAIN, so that is the name with a record - the short one was
    # the first guess here and it resolves for nobody, which the board check said
    # out loud (`[Errno -3] Temporary failure in name resolution`) within a minute.
    "Deployment": ("https://health.monarch.innotel.us/deployment/health", "docker",
                   "Deployment - is the running checkout origin/main, tree clean?"),
}

# Rows and tiles to REMOVE. Two kinds of nothing:
#   * retired services - their compose entries are `profiles: ["legacy"]`, so no
#     host runs them and no name fronts them;
#   * names that never existed in NPM/DNS (`.monarch.local` for all of them,
#     plus platforms that publish under a different domain), which is why the
#     `Onyx`/`Oasis`/`Rizzaura` tiles 000'd on the live board.
# The app row is deleted with the tile: an entry that can only fail is exactly
# what an operator should not have to tell apart from a working one, and
# re-adding one is a single line in APPS above.
DEAD = (
    "AthenIQ Learn",
    "AthenIQ Studio",
    "Autobrr",
    # Measured 2026-10-30 by scripts/check-board-tiles.py, which is what clicks
    # these: all three names fail at the edge with SNI unrecognised, and the
    # Capstone domain itself redirects to an unrelated host. Removed rather than
    # left to look healthy; re-add the row in APPS when the name answers again.
    "Capstone",
    "ClipBucket",
    "Deluge",
    "Dispatcharr",
    "Monarch Recs",
    "NextPVR",
    "Onyx",
    "Oasis",
    "Rizzaura",
    "Subscribe (Zeus)",
    "TVHeadend",
    "Zeus PBX",
)

# Tiles whose status dot asks a different question from "does the page load".
# The splash tile shows the image; the guard behind that image is what decides
# whether it survives a library scan, and only the health service can answer
# that - so this dot reads the guard, not the PNG.
PING_OVERRIDES = {
    "Splash (live)": "http://monarch-health:8003/splash/health",
    # The guide is only as good as its feeds, and a dead feed leaves the EPG page
    # looking exactly like a live one. This dot reads the monitor's report (a feed
    # has to fail twice in a row to count) instead of the page's 200.
    "IPTV (EPG)": "http://monarch-health:8003/livetv/health",
    # Same fact asked from inside: the public name leaves the host, crosses the
    # edge and comes back, which is a needless dependency for a dot on the board
    # it draws (and the one dot that would go red for the wrong reason).
    "Deployment": "http://monarch-health:8003/deployment/health",
}

# Any app still pointing at one of these names is stale by construction.
STALE_SUFFIXES = (".monarch.local",)
STALE_HOSTS = {
    "media.monarch.innotel.us": "media.innotel.us",
    # The 2026-10 renames: these names still answer, but only as 301s to the
    # short one, so a board pointing at them walks a redirect on every click.
    "media.magnate.innotel.us": "media.innotel.us",
    "sonarr.monarch.innotel.us": "tv.innotel.us",
    "radarr.monarch.innotel.us": "movies.innotel.us",
    "lidarr.monarch.innotel.us": "mp3.innotel.us",
    "whisparr.monarch.innotel.us": "xxx.innotel.us",
    "req.monarch.innotel.us": "req.innotel.us",
    "recs.monarch.innotel.us": "monarch.innotel.us",
    # The old LAN tile: Requestrr's console has a gate and a name now, so a
    # private address is both wrong and unusable from off the LAN.
    "http://192.168.1.74:4545": "https://requestrr.monarch.innotel.us",
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
        app_tile("Transmission"),
        app_tile("SABnzbd"),
        widget("indexerManager", 2),
        widget("mediaRequests-requestList", 3),
    )),
    ("Business", (
        app_tile("Magnate"),
        app_tile("Subscribe"),
        app_tile("AvantFAX"),
        app_tile("Signara"),
    )),
    ("Platform", (
        app_tile("Genie"),
        app_tile("Atlas"),
        app_tile("ZapIt"),
        app_tile("Monarch"),
        app_tile("Splash (live)"),
    )),
    ("Infrastructure", (
        app_tile("Cerulean SSO"),
        app_tile("Nginx Proxy"),
        app_tile("Deployment"),
    )),
)

# The board's own branding: the tab/meta title, the crown as its mark, and the
# Monarch palette. Homarr keeps these on the `board` row, so the same pass that
# places the tiles also makes the board *look* like the platform it is the front
# door of. The crown is served from the public apex; see scripts/npm-theme.py for
# the same asset reaching the media apps.
BOARD_TITLE = "Monarch"
BOARD_LOGO = "https://monarch.innotel.us/monarch/monarch-crown.svg"
BOARD_COLORS = ("#f5cc57", "#b5101f")   # primary (crown gold), secondary (velvet red)
BOARD_CUSTOM_CSS = (
    "/* Monarch — the dashboard wears the platform mark, not Homarr's. */\n"
    "a { color: #e7b63f; }\n"
    "a:hover { color: #fff0b8; }\n"
)


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


def section_title(row) -> str | None:
    """A section's design title, whether it lives in `name` or in the options.

    A group is named twice on the way through the model change: the old canvas
    sections carried it in `name`, and a container carries it in
    `options.title` (which is what the UI renders). Reading both is what keeps a
    re-run from creating a second copy of every group.
    """
    if row["name"]:
        return row["name"]
    try:
        return json.loads(row["options"] or "{}").get("json", {}).get("title") or None
    except (TypeError, ValueError):
        return None


def ensure_sections(db, board_id: str) -> tuple[str, dict[str, str]]:
    """The board's canvas root plus the design's groups as container sections.

    The root has to be a section of its own with nothing in it: adopting one of
    the design's groups as the canvas would leave its tiles homeless, and a
    second `empty` section in the main lane is the error the board refuses to
    render with. A group the board already carries - in either model - keeps its
    id, so the tiles that point at it stay where they are.
    """
    wanted = [title for title, _tiles in DESIGN]
    rows = db.execute("SELECT id, name, kind, x_offset, options FROM section WHERE board_id=?",
                      (board_id,)).fetchall()
    by_id = {row["id"]: row for row in rows}
    placed_in = {row[0] for row in db.execute("SELECT DISTINCT section_id FROM item_layout")}

    root = next((row for row in rows
                 if row["kind"] == "empty"
                 and (row["x_offset"] or 0) == MAIN_LANE_OFFSET
                 and row["id"] not in placed_in), None)
    if root is None:
        root_id = nanoid()
        db.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset, name, options) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (root_id, board_id, "empty", MAIN_LANE_OFFSET, 0, None, EMPTY_OPTIONS))
        print("  added the board's main canvas section")
    else:
        root_id = root["id"]
        db.execute("UPDATE section SET kind='empty', x_offset=?, y_offset=0, name=NULL, options=? "
                   "WHERE id=?", (MAIN_LANE_OFFSET, EMPTY_OPTIONS, root_id))

    by_title: dict[str, str] = {}
    for row in rows:
        title = section_title(row)
        if title and row["id"] != root_id:
            by_title.setdefault(title, row["id"])

    sections: dict[str, str] = {}
    for title in wanted:
        section_id = by_title.get(title)
        if section_id is None:
            section_id = nanoid()
            db.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset, name, options) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (section_id, board_id, "container", None, None, None,
                        container_options(title)))
            print(f"  added section: {title!r}")
        else:
            was_canvas = by_id[section_id]["kind"] == "empty"
            db.execute("UPDATE section SET kind='container', x_offset=NULL, y_offset=NULL, "
                       "name=NULL, options=? WHERE id=?", (container_options(title), section_id))
            if was_canvas:
                print(f"  made {title!r} a container under the board's canvas")
        sections[title] = section_id

    keep = set(sections.values()) | {root_id}
    for row in rows:
        if row["id"] in keep:
            continue
        db.execute("DELETE FROM item_layout WHERE section_id=?", (row["id"],))
        db.execute("DELETE FROM section_layout WHERE section_id=?", (row["id"],))
        db.execute("DELETE FROM section WHERE id=?", (row["id"],))
        print(f"  removed section {row['name']!r}" if row["name"] else "  removed an extra section")

    # A container reads its collapsed state per user; with no row the section is
    # open, which is what the design wants (and a re-run must not flip it shut).
    for user in db.execute("SELECT id FROM user").fetchall():
        for section_id in sections.values():
            db.execute("INSERT OR IGNORE INTO section_collapse_state (user_id, section_id, collapsed) "
                       "VALUES (?,?,0)", (user["id"], section_id))
    return root_id, sections


def place_board(db, board_id: str, layouts, root_id: str, sections: dict[str, str],
                by_name: dict[str, str]) -> int:
    """Place every designed tile and group on every layout the board has.

    Homarr renders one layout per breakpoint, so a tile placed on only the first
    one vanishes on the others. Each group is a container with its geometry in
    `section_layout` (under the board's canvas root, stacked top to bottom by the
    height it actually uses); the tiles inside it go in `item_layout`, relative
    to the container rather than to the board.
    """
    widgets: dict[str, str] = {}
    for row in db.execute("SELECT id, kind FROM item WHERE board_id=? AND kind != 'app'",
                          (board_id,)):
        widgets.setdefault(row["kind"], row["id"])
    placed = 0
    for layout in layouts:
        cols = layout["column_count"] or 10
        cursor_y = 0
        for title, tiles in DESIGN:
            section_id = sections[title]
            x, row_y, height = 0, 0, 1
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
                width = min(cols, max(1, width))
                if x + width > cols:
                    x = 0
                    row_y += 1
                db.execute("DELETE FROM item_layout WHERE item_id=? AND layout_id=?",
                           (item_id, layout["id"]))
                db.execute(
                    "INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, "
                    "width, height) VALUES (?,?,?,?,?,?,?)",
                    (item_id, section_id, layout["id"], x, row_y, width, 1),
                )
                height = max(height, row_y + 1)
                placed += 1
                x += width
            db.execute("DELETE FROM section_layout WHERE section_id=? AND layout_id=?",
                       (section_id, layout["id"]))
            db.execute(
                "INSERT INTO section_layout (section_id, layout_id, parent_section_id, x_offset, "
                "y_offset, width, height) VALUES (?,?,?,?,?,?,?)",
                (section_id, layout["id"], root_id, 0, cursor_y, cols, height),
            )
            cursor_y += height
    return placed


def ensure_apps(db) -> dict[str, str]:
    """Upsert the designed apps, repair stale hrefs, and return name -> item id."""
    existing = {row["name"]: row for row in db.execute("SELECT * FROM app").fetchall()}
    app_ids: dict[str, str] = {}
    for name, (href, icon_name, tooltip) in APPS.items():
        icon = ICON.format(name=icon_name)
        # Usually the tile's own href doubles as its status probe; the splash tile
        # is the exception, and PING_OVERRIDES says so.
        ping = PING_OVERRIDES.get(name, href)
        row = existing.get(name)
        if row is None:
            app_id = nanoid()
            db.execute("INSERT INTO app (id, name, description, icon_url, href, ping_url) "
                       "VALUES (?,?,?,?,?,?)", (app_id, name, tooltip, icon, href, ping))
            print(f"  added app: {name} -> {href}")
        else:
            app_id = row["id"]
            if (row["href"], row["ping_url"], row["icon_url"]) != (href, ping, icon):
                db.execute("UPDATE app SET href=?, ping_url=?, icon_url=?, description=? WHERE id=?",
                           (href, ping, icon, tooltip, app_id))
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
    db.execute("PRAGMA foreign_keys=ON")
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
            root_id, sections = ensure_sections(db, board["id"])
            placed = place_board(db, board["id"], layouts, root_id, sections, items)
            removed = drop_dead(db, board["id"])
            total_placed += placed
            print(f"  {placed} tile(s) placed in {len(DESIGN)} sections, {removed} removed")

        # The board is the dashboard: its title, its mark, its palette, and the
        # apex/home board for every user (an existing user keeps their own choice
        # only if they made one; this sets the ones that never did).
        first = boards[0]["id"]
        db.execute(
            "UPDATE board SET page_title=?, meta_title=?, logo_image_url=?, "
            "favicon_image_url=?, primary_color=?, secondary_color=?, custom_css=? "
            "WHERE id=?",
            (BOARD_TITLE, BOARD_TITLE, BOARD_LOGO, BOARD_LOGO,
             BOARD_COLORS[0], BOARD_COLORS[1], BOARD_CUSTOM_CSS, first))
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
