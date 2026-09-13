#!/usr/bin/env python3
"""Seed the Homarr v1 board (sqlite) with the full Innotel stack tile set.

Homarr v1 (homarr-labs) stores boards, apps and layout in a SQLite database
under /appdata/db/db.sqlite (this repo mounts /docker/appdata/homarr/appdata).
The old v0 `configs/default.json` seeding no longer applies, so this script is
the operative seeder: it idempotently

  * fixes stale app URLs (e.g. Jellyfin -> media.monarch.innotel.us),
  * upserts app rows for every platform service (including Capstone),
  * adds app tiles (items + grid layout rows) for anything missing on the
    existing home board, appended below the current content,
  * sets the home board so the apex (monarch.innotel.us) shows the dashboard.

Usage (run on the Monarch host; Homarr may be running — the writes are
transactional and sqlite is WAL):

    python3 scripts/seed-homarr-board.py [path/to/db.sqlite]

Canonical tile spec follows — edit the list here to change what the board
shows, then re-run (items that already exist are left in place).
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
#
# Every href here is a hostname that exists in Nginx Proxy Manager. A tile that
# points at a name with no DNS record (the old `*.monarch.local` set) is a dead
# tile no matter how healthy the container is, so real hosts only.
APPS = [
    # -- Subscription entry points. Magnate owns plans, checkout and the
    #    subscriber's account; each service has its own subscribe page and the
    #    ecosystem-wide one is the landing page (see docs/operations.md ->
    #    Subscription platform).
    ("Subscribe", "https://subscribe.innotel.us", "stripe", "Subscribe - the portal landing page: pick a plan, pay via Stripe, manage your account (Magnate)"),
    ("Subscribe (Zeus)", "https://subscribe.zeus.innotel.us", "asterisk", "Zeus subscription page - phone plan + Capstone voice agents add-on"),
    # -- Media stack. Jellyfin/Jellyseerr are published as media.innotel.us and
    #    req.innotel.us; the `*.monarch.innotel.us` names below are the arr
    #    apps' real hosts (the DB used to carry `*.monarch.local`, which has no
    #    DNS record at all).
    ("Jellyfin", "https://media.innotel.us", "jellyfin", "Jellyfin - stream movies & TV"),
    ("Jellyseerr", "https://req.innotel.us", "jellyseerr", "Jellyseerr - request movies & TV"),
    ("IPTV (EPG)", "https://tv.monarch.innotel.us", "tvheadend", "IPTV - live TV guide (XMLTV EPG)"),
    ("Radarr", "https://radarr.monarch.innotel.us", "radarr", "Radarr - movie collection manager"),
    ("Sonarr", "https://sonarr.monarch.innotel.us", "sonarr", "Sonarr - TV collection manager"),
    ("Bazarr", "https://bazarr.monarch.innotel.us", "bazarr", "Bazarr - subtitle manager"),
    ("Lidarr", "https://lidarr.monarch.innotel.us", "lidarr", "Lidarr - music collection manager"),
    ("Prowlarr", "https://prowlarr.monarch.innotel.us", "prowlarr", "Prowlarr - indexer manager"),
    ("qBittorrent", "https://qbittorrent.monarch.innotel.us", "qbittorrent", "qBittorrent - torrent client"),
    ("SABnzbd", "https://sabnzbd.monarch.innotel.us", "sabnzbd", "SABnzbd - usenet client"),
    ("Whisparr", "https://whisparr.monarch.innotel.us", "whisparr", "Whisparr - adult collection manager"),
    # -- Platform / stack services (added below the existing tiles) --
    ("Monarch", "https://monarch.innotel.us", "jellyfin", "Monarch - this dashboard (apex origin)"),
    ("Capstone", "https://dashboard.capstone.innotel.us", "openai", "Capstone - voice AI agent platform dashboard"),
    ("Zeus PBX", "https://pbx.zeus.innotel.us", "asterisk", "Zeus - PBX / VoIP (Asterisk + coturn)"),
    ("AvantFAX", "https://fax.zeus.innotel.us", "files", "AvantFAX - fax service"),
    ("Magnate", "https://app.magnate.innotel.us", "stripe", "Magnate - billing platform & admin portal"),
    ("Cerulean SSO", "https://auth.cerulean.innotel.us", "authentik", "Cerulean - identity provider (Authentik SSO)"),
    ("Nginx Proxy", "https://admin.monarch.innotel.us", "nginx-proxy-manager", "Nginx Proxy Manager - edge admin UI"),
    ("Olympus", "https://olympus.innotel.us", "openai", "Olympus - AI platform"),
    ("Olympus Studio", "https://studio.olympus.innotel.us", "code", "Olympus Studio - app builder"),
    ("AthenIQ Learn", "https://learn.innotel.us", "moodle", "AthenIQ - LMS / learning platform"),
    ("AthenIQ Studio", "https://studio.innotel.us", "code", "AthenIQ Studio - course authoring"),
    ("Signara", "https://app.signara.innotel.us", "vault", "Signara - trust / certificate signing portal"),
    ("Onyx", "https://onyx.innotel.us", "minio", "Onyx - object storage"),
    ("Rizzaura", "https://rizzaura.innotel.us", "mastodon", "Rizzaura - social platform"),
    ("Atlas", "https://atlas.innotel.us", "gitea", "Atlas - DevOps / coding platform"),
    ("Oasis", "https://oasis.innotel.us", "mailcow", "Oasis - mail platform"),
    ("ZapIt", "https://zapit.innotel.us", "linkwarden", "ZapIt - short links"),
    # -- LAN-only services: no NPM host exists, so the tile points at the
    #    container's published port on this host instead of a name that does
    #    not resolve.
    ("Requestrr", "http://192.168.1.46:4545", "discord", "Requestrr - Discord request bot (LAN)"),
]

# Tiles to REMOVE from the board (the app row is kept, so re-adding a tile is a
# one-line change here). These are the services that are gone or unreachable:
# their compose entries are `profiles: ["legacy"]` (retired on purpose) or they
# publish no address at all, so a tile can only ever 404.
RETIRED = [
    "Autobrr",
    "ClipBucket",
    "Deluge",
    "Dispatcharr",
    "NextPVR",
    "TVHeadend",
    "Monarch Recs",
]

# Any app still pointing at one of these names is stale by construction.
STALE_SUFFIXES = (".monarch.local",)
STALE_HOSTS = {
    "media.monarch.innotel.us": "media.innotel.us",
    "req.monarch.innotel.us": "req.innotel.us",
    "recs.monarch.innotel.us": "monarch.innotel.us",
}


def repair_stale_url(href: str) -> tuple[str, bool]:
    """Rewrite a board href that points at a name with no DNS record.

    The board predates the platform-wide `<service>.<domain>` hostnames and
    carries `*.monarch.local` links (no DNS at all) and a couple of one-off
    names that were never created in NPM. Returns (href, changed).
    """
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


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"DB not found: {DB_PATH}")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        boards = db.execute("SELECT id, name FROM board ORDER BY name").fetchall()
        if not boards:
            sys.exit("No board found - finish Homarr onboarding first (a board must exist).")
        board = boards[0]
        print(f"Using board: {board['name']} ({board['id']})")

        sections = db.execute(
            "SELECT id, board_id, kind, x_offset, y_offset FROM section WHERE board_id=?", (board["id"],)
        ).fetchall()
        layouts = db.execute(
            "SELECT id, name, board_id, column_count FROM layout WHERE board_id=?", (board["id"],)
        ).fetchall()
        if not sections or not layouts:
            sys.exit("Board has no section/layout - open it once in the UI first.")
        section = sections[0]
        layout = layouts[0]
        cols = layout["column_count"] or 10
        print(f"  section={section['id']} layout={layout['id']} columns={cols}")

        # Highest occupied row on the main layout -> append below it
        occupied = db.execute(
            "SELECT COALESCE(MAX(y_offset + height), -1) AS m FROM item_layout "
            "WHERE section_id=? AND layout_id=?",
            (section["id"], layout["id"]),
        ).fetchone()
        cursor_y = int(occupied["m"]) + 1
        print(f"  existing content ends at row {int(occupied['m'])}; new tiles start at y={cursor_y}")

        existing_apps = {r["name"]: r for r in db.execute("SELECT * FROM app").fetchall()}
        existing_items = {
            r["id"]: r for r in db.execute("SELECT * FROM item WHERE board_id=? AND kind='app'", (board["id"],)).fetchall()
        }
        # map itemId -> appId for app items on this board
        item_app = {}
        for it in existing_items.values():
            try:
                item_app[it["id"]] = json.loads(it["options"])["json"].get("appId")
            except Exception:
                pass
        app_of_item = {aid: iid for iid, aid in item_app.items()}

        # 0. Repair stale hrefs on apps we do not otherwise manage. The board
        #    used to carry `*.monarch.local` names, which have no DNS record, so
        #    every one of those tiles was dead. Rewrite them to the real
        #    hostname instead of leaving a link that cannot resolve.
        specs = {name: (href, tooltip) for name, href, _icon, tooltip in APPS}
        for name, row in existing_apps.items():
            if name in specs or name in RETIRED:
                continue
            new_href, changed = repair_stale_url(row["href"] or "")
            if changed:
                db.execute("UPDATE app SET href=?, ping_url=? WHERE id=?", (new_href, new_href, row["id"]))
                print(f"  repaired stale URL: {name} -> {new_href}")

        placed = 0
        for name, href, icon_name, tooltip in APPS:
            icon = ICON.format(name=icon_name)
            # 1. upsert the app row
            if name in existing_apps:
                row = existing_apps[name]
                if row["href"] != href or row["ping_url"] != href or row["icon_url"] != icon:
                    db.execute(
                        "UPDATE app SET href=?, ping_url=?, icon_url=?, description=? WHERE id=?",
                        (href, href, icon, tooltip, row["id"]),
                    )
                    print(f"  updated app: {name} -> {href}")
                app_id = row["id"]
            else:
                app_id = nanoid()
                db.execute(
                    "INSERT INTO app (id, name, description, icon_url, href, ping_url) VALUES (?,?,?,?,?,?)",
                    (app_id, name, tooltip, icon, href, href),
                )
                print(f"  added app: {name} -> {href}")
                existing_apps[name] = {"id": app_id}

            # 2. ensure an app tile (item) exists for it on this board
            item_id = app_of_item.get(app_id)
            if item_id is None:
                item_id = nanoid()
                options = json.dumps({"json": {"appId": app_id, "openInNewTab": True, "showTitle": True}})
                db.execute(
                    "INSERT INTO item (id, board_id, kind, options, advanced_options) VALUES (?,?,?,?,?)",
                    (item_id, board["id"], "app", options, '{"json": {}}'),
                )
                print(f"  added tile: {name} (item {item_id[:8]}...)")

            # 3. ensure grid placement exists
            has_layout = db.execute(
                "SELECT 1 FROM item_layout WHERE item_id=? AND layout_id=?",
                (item_id, layout["id"]),
            ).fetchone()
            if not has_layout:
                x = placed % cols
                y = cursor_y + placed // cols
                db.execute(
                    "INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, width, height) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (item_id, section["id"], layout["id"], x, y, 1, 1),
                )
                print(f"  placed {name} at x={x} y={y}")
                placed += 1
            else:
                print(f"  already placed: {name}")

        # 3.5 Retire tiles for services that no longer publish an address. The
        #     app row stays (so nothing is lost and re-adding is one line), but
        #     the board stops offering a link that can only 404.
        retired = 0
        for name in RETIRED:
            row = existing_apps.get(name)
            if not row:
                continue
            app_id = row["id"]
            item_id = app_of_item.get(app_id)
            if item_id is None:
                continue
            db.execute("DELETE FROM item_layout WHERE item_id=?", (item_id,))
            db.execute("DELETE FROM item WHERE id=?", (item_id,))
            retired += 1
            print(f"  removed retired tile: {name}")

        # 4. Make this board the home board (apex shows the dashboard)
        db.execute(
            "UPDATE serverSetting SET value=? WHERE setting_key='board'",
            (json.dumps({"json": {"homeBoardId": board["id"], "mobileHomeBoardId": board["id"],
                                  "enableStatusByDefault": True, "forceDisableStatus": False}}),),
        )
        db.execute(
            "UPDATE user SET home_board_id=?, mobile_home_board_id=? WHERE home_board_id IS NULL OR home_board_id=''",
            (board["id"], board["id"]),
        )
        db.commit()
        print(f"\nDone: board '{board['name']}' is the home board with the full stack tile set "
              f"({len(APPS)} apps ensured, {placed} newly placed, {retired} retired).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
