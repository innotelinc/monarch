"""What the library view needed: the right rows, grouped, with a poster.

Three complaints, three causes, held here:

  * **TV was empty.** `/api/library?kind=episode` mapped `episode` to `series`,
    a kind nothing has ever been stored as, so the tab asked for its own items
    and was told there are none.
  * **Music was a flat list.** 140 tracks sorted by when they were added say
    nothing about which album they belong to. The layout the importer writes
    already answers it, so `library.place` reads the path back.
  * **Films had no pictures.** The library directories hold no artwork; Jellyfin
    is the only thing that has any, so the poster is proxied from its index.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, jellyfin, library, livetv


# ── the guide: parse what is on, not the whole document ──────────────────────

def test_only_the_programmes_in_the_window_are_kept(tmp_path):
    guide = tmp_path / "guide.xml"
    guide.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="bbc1.uk"><display-name>BBC One</display-name></channel>
  <programme channel="bbc1.uk" start="20200101000000 +0000" stop="20200101010000 +0000">
    <title>Years ago</title>
  </programme>
  <programme channel="bbc1.uk" start="20300101180000 +0000" stop="20300101190000 +0000">
    <title>Tonight</title>
  </programme>
</tv>
""")
    now = datetime(2030, 1, 1, 17, 30, tzinfo=timezone.utc)
    names, programmes = livetv.parse_xmltv_file(guide, window=(now, now + timedelta(hours=6)))
    assert names["bbc1.uk"] == "BBC One"
    assert [p.title for p in programmes] == ["Tonight"]


def test_a_truncated_guide_is_a_shorter_guide_not_an_exception(tmp_path):
    guide = tmp_path / "guide.xml"
    guide.write_text("""<?xml version="1.0"?>
<tv>
  <programme channel="a" start="20300101180000 +0000" stop="20300101190000 +0000">
    <title>Kept</title>
  </programme>
  <programme channel="a" start="20300101190000""")
    names, programmes = livetv.parse_xmltv_file(guide)
    assert names == {}
    assert [p.title for p in programmes] == ["Kept"]


def test_the_text_form_honours_the_same_window():
    text = """<tv>
  <programme channel="a" start="20200101000000 +0000" stop="20200101010000 +0000"><title>Old</title></programme>
  <programme channel="a" start="20300101180000 +0000" stop="20300101190000 +0000"><title>New</title></programme>
</tv>"""
    now = datetime(2030, 1, 1, 17, 30, tzinfo=timezone.utc)
    _, programmes = livetv.parse_xmltv(text, window=(now, now + timedelta(hours=6)))
    assert [p.title for p in programmes] == ["New"]


# ── where a file sits, in a person's words ──────────────────────────────────

def test_a_track_knows_its_artist_and_album(tmp_path):
    root = Path("/lib")
    album = library.place(Path("/lib/music/Drake/HABIBTI (2026)/01 - Rusty.flac"), root)
    assert album == {"artist": "Drake", "album": "HABIBTI"}


def test_a_track_with_no_album_folder_still_knows_its_artist(tmp_path):
    assert library.place(Path("/lib/music/Black Pumas/08 - Confines.flac"), Path("/lib")) == {
        "artist": "Black Pumas", "album": ""}


def test_an_episode_knows_its_show_and_season():
    assert library.place(
        Path("/lib/tv/South Park/Season 29/South.Park.S29E01.mkv"), Path("/lib")) == {
        "show": "South Park", "season": "Season 29"}


def test_a_path_outside_the_library_says_nothing():
    assert library.place(Path("/somewhere/else.mkv"), Path("/lib")) == {}


def test_a_track_title_drops_the_number_the_file_name_carries():
    assert library.track_title("08 - Confines") == "Confines"
    assert library.track_title("01. Leave Me Alone") == "Leave Me Alone"
    assert library.track_title("Song 2") == "Song 2"


def test_a_track_title_drops_the_artist_and_album_the_path_repeats():
    # Under an artist and album heading the row said all three, in the file's
    # words, before the song.
    assert library.track_title("XXXTENTACION - ! - 14 - I don't even speak spanish lol",
                               "XXXTENTACION", "!") == "I don't even speak spanish lol"
    assert library.track_title("03 Bruno Mars - I Just Might",
                               "Bruno Mars", "") == "I Just Might"
    # Unknown artist: the number still goes, the words stay.
    assert library.track_title("07 - Something Else") == "Something Else"


# ── the listing the view is built from ──────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _seed(rows):
    from app import main
    with db.session(main.settings.db_path) as conn:
        return [db.upsert_item(conn, row) for row in rows]


def test_the_tv_tab_gets_the_episodes(client):
    from app import main
    root = main.settings.library_root
    _seed([
        {"kind": "episode", "title": "Pilot", "year": None, "season": 1,
         "episode": 1, "quality": "1080p",
         "path": str(root / "tv/Show/Season 1/e1.mkv"), "size": 10,
         "fingerprint": "tv-tab|s01|e01"},
        {"kind": "movie", "title": "A Film", "year": 2024, "season": None,
         "episode": None, "quality": "1080p",
         "path": str(root / "movies/A Film (2024)/a.mkv"), "size": 10,
         "fingerprint": "tv-tab|a film|2024"},
    ])
    body = client.get("/api/library?kind=episode").json()
    titles = [item["title"] for item in body["items"]]
    assert "Pilot" in titles
    assert "A Film" not in titles
    assert all(item["kind"] == "episode" for item in body["items"])


def test_a_track_carries_its_place_and_a_name_without_the_number(client):
    from app import main
    root = main.settings.library_root
    _seed([{
        "kind": "track", "title": "01 - Rusty", "year": None, "season": None,
        "episode": None, "quality": "unknown",
        "path": str(root / "music/Drake/HABIBTI (2026)/01 - Rusty.flac"),
        "size": 10, "fingerprint": "track-place|rusty",
    }])
    items = client.get("/api/library?kind=track").json()["items"]
    track = next(item for item in items if item["title"] == "01 - Rusty")
    assert track["place"] == {"artist": "Drake", "album": "HABIBTI"}
    assert track["name"] == "Rusty"


# ── the poster ──────────────────────────────────────────────────────────────

def test_an_episode_borrows_its_series_poster(monkeypatch, client):
    asked = []

    class FakeJellyfin:
        def index(self):
            return {"/data/media/tv/Show/Season 1/e1.mkv": {
                "id": "ep1", "type": "Episode", "series": "series1",
                "album": "", "art": False}}

        def image(self, item_id, max_height=420):
            asked.append(item_id)
            return (b"\xff\xd8jpeg", "image/jpeg") if item_id == "series1" else None

    from app import main
    monkeypatch.setattr(main, "jellyfin_client", lambda settings: FakeJellyfin())
    main._art_index.clear()

    with db.session(main.settings.db_path) as conn:
        item_id = db.upsert_item(conn, {
            "kind": "episode", "title": "Pilot", "year": None, "season": 1,
            "episode": 1, "quality": "1080p",
            "path": "/data/media/tv/Show/Season 1/e1.mkv",
            "size": 10, "fingerprint": "pilot|s01|e01",
        })

    response = client.get(f"/api/art/{item_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8jpeg"
    assert asked == ["series1"]


def test_an_item_jellyfin_does_not_know_has_no_poster(monkeypatch, client):
    class Empty:
        def index(self):
            return {}

        def image(self, item_id, max_height=420):  # pragma: no cover - not reached
            raise AssertionError("asked Jellyfin for an image it cannot have")

    from app import main
    monkeypatch.setattr(main, "jellyfin_client", lambda settings: Empty())
    main._art_index.clear()

    with db.session(main.settings.db_path) as conn:
        item_id = db.upsert_item(conn, {
            "kind": "movie", "title": "Unknown Film", "year": 2024, "season": None,
            "episode": None, "quality": "1080p", "path": "/data/media/movies/X/x.mkv",
            "size": 10, "fingerprint": "unknown film|2024",
        })
    assert client.get(f"/api/art/{item_id}").status_code == 404
    assert client.get("/api/art/99999999").status_code == 404
