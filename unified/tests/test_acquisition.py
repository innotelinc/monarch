import dataclasses
from pathlib import Path

import pytest

from app import acquisition, db, indexers
from app.parser import parse
from app.settings import Settings


def result(title: str, *, size: int = 8 * 1024 ** 3, seeders: int = 10,
           indexer: str = "tracker-a", infohash: str = "HASH1") -> indexers.IndexerResult:
    return indexers.IndexerResult(
        indexer=indexer, title=title, guid=f"guid-{title}", size=size,
        seeders=seeders, infohash=infohash,
        download_url=f"http://tracker/{infohash}.torrent",
    )


class FakeTransmission:
    def __init__(self, completed=None):
        self.added = []
        self._completed = completed or []

    def add(self, url, **_kwargs):
        self.added.append(url)
        return {"id": len(self.added), "name": url, "hash": "h", "duplicate": False}

    def completed(self):
        return self._completed

    def status(self):
        return {"reachable": True, "count": len(self.added), "downloading": 0,
                "complete": len(self._completed), "rate": 0}

    def list(self):
        return self._completed


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "core.sqlite3")
    yield connection
    connection.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(data_dir=tmp_path, library_root=tmp_path / "library",
                    static_dir=Path("static"))


def test_plan_chooses_best_candidate(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    results = [
        result("Dune.Part.Two.2024.720p.WEB-DL.x264-GRP", infohash="A"),
        result("Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP", infohash="B"),
        result("Silo.S02E01.2160p.WEB-DL.x265-GRP", infohash="C"),
    ]
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                            dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                            search=lambda *a, **k: results)
    assert plan is not None
    assert plan.release.resolution == "2160p"
    assert plan.result.infohash == "B"


def test_plan_skips_already_grabbed_release(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    title = "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP"
    conn.execute("INSERT INTO grabs(wanted_id, release, state, grabbed_at) VALUES(?,?,'failed',?)",
                 (wanted_id, title, db.now()))
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                            dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                            search=lambda *a, **k: [result(title, infohash="A")])
    assert plan is None


def test_plan_does_not_regrab_the_same_quality_twice(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    db.upsert_item(conn, {
        "kind": "movie", "title": "Dune Part Two", "year": 2024,
        "quality": "2160p web-dl x265", "path": "/x.mkv", "size": 1,
        "fingerprint": "movie|dune part two|2024|||2160p/web-dl",
    })
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                            dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                            search=lambda *a, **k: [result(
                                "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP", infohash="A")])
    assert plan is None


def test_grab_records_and_marks_wanted(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                            dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                            search=lambda *a, **k: [result(
                                "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP", infohash="A")])
    client = FakeTransmission()
    grab_id = acquisition.grab(conn, plan, client, settings)
    assert grab_id is not None
    assert client.added == ["http://tracker/A.torrent"]
    grab_row = db.one(conn, "SELECT * FROM grabs WHERE id = ?", (grab_id,))
    assert grab_row["state"] == "grabbed"
    assert grab_row["download_id"] == "1"
    assert db.one(conn, "SELECT status FROM wanted WHERE id = ?", (wanted_id,))[0] == "grabbed"


def test_grab_without_a_link_fails_before_the_client_is_asked(conn, settings):
    """A result with no download and no hash is not handed to Transmission."""
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    bare = dataclasses.replace(
        result("Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP", infohash=""),
        download_url="",
    )
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                           dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                           search=lambda *a, **k: [bare])
    client = FakeTransmission()

    assert acquisition.grab(conn, plan, client, settings) is None
    assert client.added == [], "an empty filename is not a download request"
    grab_row = db.one(conn, "SELECT * FROM grabs")
    assert grab_row["state"] == "failed"
    assert db.one(conn, "SELECT status FROM wanted")[0] == "failed"
    error = db.one(conn, "SELECT last_error FROM wanted")[0]
    assert "tracker-a" in error and "no download link" in error


def test_grab_failure_is_recorded(conn, settings):
    class Broken(FakeTransmission):
        def add(self, url, **_kwargs):
            from app.downloader import TransmissionError
            raise TransmissionError("offline")

    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    row = db.one(conn, "SELECT * FROM wanted WHERE id = ?", (wanted_id,))
    plan = acquisition.plan(conn, row, [indexers.Indexer(name="a", url="http://a")],
                            dict(acquisition.scoring.DEFAULT_PROFILE), settings,
                            search=lambda *a, **k: [result(
                                "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP", infohash="A")])
    assert acquisition.grab(conn, plan, Broken(), settings) is None
    assert db.one(conn, "SELECT state FROM grabs")[0] == "failed"
    assert db.one(conn, "SELECT status FROM wanted")[0] == "failed"


def test_import_completed_links_file_and_marks_imported(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    conn.execute(
        "INSERT INTO grabs(wanted_id, release, state, download_id, grabbed_at)"
        " VALUES(?,?,'completed','7',?)",
        (wanted_id, "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP.mkv", db.now()),
    )
    download = settings.library_root.parent / "downloads" / "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP"
    download.mkdir(parents=True)
    (download / "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP.mkv").write_bytes(b"x" * 2048)

    from app.downloader import Torrent

    class Ready(FakeTransmission):
        def completed(self):
            return [Torrent(id=7, name=download.name, hash="h", status="seeding",
                            percent=1.0, size=2048, rate=0, eta=0,
                            directory=str(download.parent), error="", finished=True)]

    imported = acquisition.import_completed(Ready(), settings)
    assert len(imported) == 1
    assert imported[0]["action"] in {"linked", "copied"}
    assert Path(imported[0]["path"]).exists()
    assert db.one(conn, "SELECT state FROM grabs")[0] == "imported"
    assert db.one(conn, "SELECT status FROM wanted")[0] == "imported"
    assert db.one(conn, "SELECT count(*) FROM items")[0] == 1


def test_import_waits_when_file_missing(conn, settings):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    conn.execute(
        "INSERT INTO grabs(wanted_id, release, state, download_id, grabbed_at)"
        " VALUES(?,?,'grabbed','9',?)", (wanted_id, "Movie.2024.1080p-GRP", db.now()))

    from app.downloader import Torrent

    class Empty(FakeTransmission):
        def completed(self):
            return [Torrent(id=9, name="Movie.2024.1080p-GRP", hash="h", status="seeding",
                            percent=1.0, size=10, rate=0, eta=0,
                            directory=str(settings.library_root), error="", finished=True)]

    assert acquisition.import_completed(Empty(), settings) == []
    assert db.one(conn, "SELECT state FROM grabs")[0] == "completed"


def test_scheduler_tick_survives_missing_services(conn, settings):
    scheduler = acquisition.Scheduler(settings, client_factory=FakeTransmission)
    db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    outcome = scheduler.tick()
    assert "stats" in outcome
    conn.close()


def test_after_import_notifies_jellyfin_and_fetches_a_subtitle(conn, settings, tmp_path):
    from app import subtitles

    class Jelly:
        configured = True

        def __init__(self):
            self.paths = []

        def refresh(self, paths=(), full=True):
            self.paths = list(paths)
            return {"configured": True, "updated": len(self.paths), "refreshed": True}

    class Provider:
        name = "fake"
        configured = True

        def search(self, request, languages):
            return [subtitles.Candidate("fake", "1", languages[0])]

        def download(self, candidate):
            return b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    video = tmp_path / "library" / "movies" / "Film (2020)" / "Film.2020.1080p.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 4096)

    jelly = Jelly()
    report = acquisition.after_import(settings, [video], provider=Provider(), jelly=jelly)
    assert jelly.paths == [video]
    assert report["jellyfin"]["refreshed"] is True
    assert [row["status"] for row in report["subtitles"]] == ["ok"]
    assert subtitles.subtitle_path(video, "en").exists()


def test_after_import_never_lets_jellyfin_break_the_import(conn, settings, tmp_path):
    import dataclasses

    class Broken:
        configured = True

        def refresh(self, paths=(), full=True):
            raise RuntimeError("boom")

    # Subtitles off, so this test exercises the Jellyfin failure only and never
    # reaches the network through the keyless provider.
    no_subs = dataclasses.replace(settings, subtitles_enabled=False)
    report = acquisition.after_import(no_subs, [tmp_path / "x.mkv"], jelly=Broken())
    assert "boom" in report["jellyfin"]["error"]
    assert "subtitles" not in report
