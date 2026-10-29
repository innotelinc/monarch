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
