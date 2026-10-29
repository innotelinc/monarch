import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "stats" in body


def test_library_listing(client):
    response = client.get("/api/library?kind=movie")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 0
    assert isinstance(body["items"], list)


def test_add_and_delete_wanted(client):
    created = client.post("/api/wanted", json={
        "kind": "movie", "title": "Dune Part Two", "year": 2024,
    })
    assert created.status_code == 200, created.text
    wanted_id = created.json()["item"]["id"]
    assert created.json()["item"]["status"] == "wanted"

    listed = client.get("/api/wanted").json()["items"]
    assert any(row["id"] == wanted_id for row in listed)

    removed = client.delete(f"/api/wanted/{wanted_id}")
    assert removed.status_code == 200
    assert all(row["id"] != wanted_id for row in client.get("/api/wanted").json()["items"])


def test_episode_request_needs_a_season(client):
    response = client.post("/api/wanted", json={"kind": "series", "title": "Severance"})
    assert response.status_code == 422


def test_invalid_kind_is_rejected(client):
    response = client.post("/api/wanted", json={"kind": "book", "title": "x"})
    assert response.status_code == 422


def test_indexer_crud_and_key_is_not_echoed(client):
    saved = client.post("/api/indexers", json={
        "name": "tracker-test", "url": "http://tracker.test", "api_key": "sekrit",
    })
    assert saved.status_code == 200
    listed = client.get("/api/indexers").json()["indexers"]
    row = next(i for i in listed if i["name"] == "tracker-test")
    assert row["has_key"] is True
    assert "sekrit" not in str(listed)

    assert client.delete("/api/indexers/tracker-test").status_code == 200
    assert all(i["name"] != "tracker-test"
               for i in client.get("/api/indexers").json()["indexers"])


def test_profile_round_trip(client):
    original = client.get("/api/profile").json()
    updated = client.put("/api/profile", json={"cutoff": "2160p web-dl"}).json()
    assert updated["cutoff"] == "2160p web-dl"
    assert client.get("/api/profile").json()["cutoff"] == "2160p web-dl"
    client.put("/api/profile", json={"cutoff": original["cutoff"]})


def test_search_without_indexers_explains_itself(client):
    response = client.get("/api/search?q=dune")
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert "no indexers" in body["reason"]


def test_scan_runs_on_empty_library(client):
    response = client.post("/api/scan")
    assert response.status_code == 200
    assert response.json()["scanned"] >= 0


def test_unknown_api_path_is_404(client):
    assert client.get("/api/does-not-exist").status_code == 404


def test_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Monarch" in response.text


# ------------------------------------------------------------- manual grab

class FakeTransmission:
    """Stands in for the RPC client so a grab is tested without a downloader."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.added: list[tuple[str, list[str] | None]] = []

    def add(self, url, *, labels=None, **_):
        if self.fail:
            # Exactly what the real client raises when the RPC is unreachable.
            from app.downloader import TransmissionError
            raise TransmissionError("unreachable: connection refused")
        self.added.append((url, labels))
        return {"id": 99, "name": "added", "hash": "abc", "duplicate": False}

    def status(self):
        return {"reachable": not self.fail, "count": 1, "downloading": 0,
                "complete": 0, "rate": 0}

    def list(self, ids=None):
        return []


@pytest.fixture()
def downloader(monkeypatch):
    fake = FakeTransmission()
    monkeypatch.setattr("app.main.transmission", lambda: fake)
    return fake


def test_manual_grab_from_a_search_result(client, downloader):
    response = client.post("/api/grab", json={
        "title": "Manual.Film.2019.1080p.WEB-DL.x264-GRP",
        "kind": "movie",
        "infohash": "abcdef0123456789abcdef0123456789abcdef01",
        "indexer": "tracker-test",
        "size": 1234,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["duplicate"] is False
    assert body["grab"]["state"] == "grabbed"
    assert body["grab"]["quality"] == "1080p web-dl x264"
    assert body["grab"]["download_id"] == "99"
    assert downloader.added[0][0].startswith("magnet:?xt=urn:btih:abcdef")

    # Dedupe: the same release is not sent to the downloader twice.
    again = client.post("/api/grab", json={
        "title": "Manual.Film.2019.1080p.WEB-DL.x264-GRP", "kind": "movie",
        "infohash": "abcdef0123456789abcdef0123456789abcdef01",
    }).json()
    assert again["ok"] is True and again["duplicate"] is True
    assert len(downloader.added) == 1


def test_manual_grab_needs_a_link(client):
    response = client.post("/api/grab", json={"title": "Nothing", "kind": "movie"})
    assert response.status_code == 422


def test_manual_grab_survives_an_unreachable_downloader(client, monkeypatch):
    monkeypatch.setattr("app.main.transmission", lambda: FakeTransmission(fail=True))
    body = client.post("/api/grab", json={
        "title": "Broken.Film.2018.720p.WEBRip.x264-OTHER", "kind": "movie",
        "download_url": "http://tracker.test/file.torrent",
    }).json()
    assert body["ok"] is False
    assert "downloader" in body["error"]
    assert body["grab"]["state"] == "failed"


def test_manual_grab_links_to_a_wanted_row(client, downloader):
    wanted = client.post("/api/wanted", json={
        "kind": "movie", "title": "Linked Film", "year": 2017,
    }).json()["item"]
    body = client.post("/api/grab", json={
        "title": "Linked.Film.2017.2160p.WEB-DL.x265-GRP", "kind": "movie",
        "download_url": "http://tracker.test/linked.torrent",
        "wanted_id": wanted["id"],
    }).json()
    assert body["ok"] is True
    assert body["grab"]["wanted_id"] == wanted["id"]
    rows = client.get("/api/wanted").json()["items"]
    row = next(row for row in rows if row["id"] == wanted["id"])
    assert row["status"] == "grabbed"


# ------------------------------------------------- subtitles and Jellyfin

def test_subtitles_report_is_honest_about_being_unconfigured(client):
    body = client.get("/api/subtitles").json()
    assert body["enabled"] is False
    assert body["provider"] == ""
    assert body["languages"] == ["en"]
    assert body["ok"] >= 0


def test_subtitle_fetch_refuses_without_a_provider(client):
    response = client.post("/api/subtitles/fetch")
    assert response.status_code == 409
    assert "OPENSUBTITLES_API_KEY" in response.json()["detail"]


def test_jellyfin_reports_unconfigured_and_refuses_to_refresh(client):
    assert client.get("/api/jellyfin").json()["configured"] is False
    response = client.post("/api/jellyfin/refresh")
    assert response.status_code == 409


def test_scan_reports_its_jellyfin_step(client):
    body = client.post("/api/scan").json()
    assert "jellyfin" in body
    assert body["jellyfin"] is None
