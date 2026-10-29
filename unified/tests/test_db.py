import pytest

from app import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "core.sqlite3")
    yield connection
    connection.close()


def item(**overrides):
    row = {
        "kind": "movie", "title": "Dune Part Two", "year": 2024,
        "quality": "2160p web-dl x265", "path": "/library/movies/dune.mkv",
        "size": 100, "fingerprint": "movie|dune part two|2024|||2160p/web-dl",
    }
    row.update(overrides)
    return row


def test_upsert_item_is_idempotent(conn):
    first = db.upsert_item(conn, item())
    second = db.upsert_item(conn, item(path="/library/movies/dune-renamed.mkv", size=101))
    assert first == second
    rows = db.query(conn, "SELECT * FROM items")
    assert len(rows) == 1
    assert rows[0]["path"].endswith("dune-renamed.mkv")
    assert rows[0]["size"] == 101


def test_fingerprint_is_unique(conn):
    db.upsert_item(conn, item())
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO items(kind,title,year,quality,path,size,fingerprint,added_at,updated_at)"
            " VALUES('movie','Dune Part Two',2024,'x','/p',1,?,?,?)",
            (item()["fingerprint"], db.now(), db.now()),
        )


def test_wanted_is_deduped_case_insensitively(conn):
    first = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    second = db.add_wanted(conn, "movie", "dune part two", 2024)
    assert first == second
    assert len(db.query(conn, "SELECT * FROM wanted")) == 1


def test_episode_wanted_rows_are_distinct(conn):
    a = db.add_wanted(conn, "episode", "Severance", None, 2, 1)
    b = db.add_wanted(conn, "episode", "Severance", None, 2, 2)
    assert a != b


def test_stats_counts(conn):
    db.upsert_item(conn, item())
    db.upsert_item(conn, item(kind="episode", title="Severance", season=2, episode=1,
                              fingerprint="episode|severance||s02|e01|1080p/web-dl"))
    db.add_wanted(conn, "movie", "Silo")
    stats = db.stats(conn)
    assert stats["items"] == 2
    assert stats["movies"] == 1
    assert stats["episodes"] == 1
    assert stats["wanted"] == 1


def test_settings_round_trip(conn):
    assert db.get_setting(conn, "profile") is None
    db.set_setting(conn, "profile", '{"cutoff": "1080p"}')
    assert db.get_setting(conn, "profile") == '{"cutoff": "1080p"}'
    db.set_setting(conn, "profile", '{"cutoff": "2160p"}')
    assert db.get_setting(conn, "profile") == '{"cutoff": "2160p"}'


def test_grab_unique_per_wanted_and_release(conn):
    wanted_id = db.add_wanted(conn, "movie", "Dune Part Two", 2024)
    sql = ("INSERT INTO grabs(wanted_id, release, state, grabbed_at)"
           " VALUES(?,?,'grabbed',?)")
    conn.execute(sql, (wanted_id, "Dune.2024.2160p-GRP", db.now()))
    with pytest.raises(Exception):
        conn.execute(sql, (wanted_id, "Dune.2024.2160p-GRP", db.now()))
