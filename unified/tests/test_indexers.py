from app import indexers

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed" version="2.0">
  <channel>
    <item>
      <title>Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.HDR.HEVC-CMRG</title>
      <guid>abc123</guid>
      <link>http://indexer/download/abc123</link>
      <pubDate>Wed, 29 Oct 2026 10:00:00 +0000</pubDate>
      <enclosure url="http://indexer/download/abc123.torrent" length="16106127360" type="application/x-bittorrent"/>
      <torznab:attr name="seeders" value="42"/>
      <torznab:attr name="peers" value="3"/>
      <torznab:attr name="infohash" value="ABCDEF0123456789"/>
      <torznab:attr name="category" value="2040"/>
    </item>
  </channel>
</rss>
"""


def test_parse_feed_extracts_every_field():
    results = indexers.parse_feed(FEED, "tracker-a")
    assert len(results) == 1
    result = results[0]
    assert result.indexer == "tracker-a"
    assert result.size == 16106127360
    assert result.seeders == 42
    assert result.leechers == 3
    assert result.infohash == "ABCDEF0123456789"
    assert result.download_url.endswith("abc123.torrent")
    assert result.category == "2040"


def test_parse_feed_handles_garbage():
    assert indexers.parse_feed(b"not xml", "x") == []
    assert indexers.parse_feed(b"", "x") == []


def test_search_url_builds_query_and_key():
    indexer = indexers.Indexer(name="a", url="http://tracker/", api_key="secret")
    url = indexers.search_url(indexer, "dune part two", limit=50)
    assert url.startswith("http://tracker/api?")
    assert "q=dune+part+two" in url
    assert "apikey=secret" in url
    assert "limit=50" in url


def test_key_prefers_infohash_then_name_and_size():
    a = indexers.IndexerResult(indexer="a", title="X", guid="1", infohash="ABC")
    b = indexers.IndexerResult(indexer="b", title="Y", guid="2", infohash="abc")
    assert a.key == b.key

    c = indexers.IndexerResult(indexer="a", title="Movie.2024-GRP", guid="1", size=10)
    d = indexers.IndexerResult(indexer="b", title="Movie.2024-GRP", guid="2", size=10)
    e = indexers.IndexerResult(indexer="b", title="Movie.2024-GRP", guid="3", size=99)
    assert c.key == d.key
    assert c.key != e.key


def test_search_all_dedupes_and_keeps_best_seeded(monkeypatch):
    first = indexers.IndexerResult(indexer="a", title="One", guid="1",
                                   infohash="HASH", seeders=5, size=100)
    second = indexers.IndexerResult(indexer="b", title="One", guid="2",
                                    infohash="HASH", seeders=50, size=100)
    other = indexers.IndexerResult(indexer="b", title="Two", guid="3",
                                   infohash="OTHER", seeders=1, size=50)

    def fake_search(indexer, query, **_kwargs):
        return [first] if indexer.name == "a" else [second, other]

    monkeypatch.setattr(indexers, "search", fake_search)

    results = indexers.search_all(
        [indexers.Indexer(name="a", url="http://a"),
         indexers.Indexer(name="b", url="http://b")],
        "one",
    )
    assert len(results) == 2
    assert results[0].seeders == 50  # the better-seeded copy of HASH wins
    assert results[0].indexer == "b"


def test_disabled_indexer_is_never_queried():
    results = indexers.search_all(
        [indexers.Indexer(name="a", url="http://a", enabled=False)], "x")
    assert results == []


def test_prowlarr_search_maps_json_and_drops_usenet(monkeypatch):
    """One aggregated indexer: relative download links fixed, NZBs skipped."""
    from app import indexers

    class FakeResponse:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if url.endswith("/api/v1/indexer"):
                return FakeResponse([
                    {"id": 1, "protocol": "torrent"},
                    {"id": 2, "protocol": "usenet"},
                ])
            return FakeResponse([
                {"guid": "g1", "title": "Film.2020.1080p.WEB-DL.x264-GRP", "indexerId": 1,
                 "indexer": "Tracker One", "size": 2147483648, "seeders": 12,
                 "leechers": 1, "downloadUrl": "/1/download?guid=g1",
                 "infoHash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
                 "publishDate": "2026-10-01T00:00:00Z"},
                {"guid": "g2", "title": "Film.2020.1080p.NZB", "indexerId": 2,
                 "indexer": "Usenet One", "size": 10, "seeders": 0,
                 "downloadUrl": "/2/download?guid=g2"},
            ])

    client = FakeClient()
    indexer = indexers.Indexer(name="Prowlarr", url="http://prowlarr:9696/",
                               api_key="k", kind="prowlarr")
    results = indexers.search_prowlarr(indexer, "Film 2020", limit=50,
                                       categories=indexers.CATEGORY_MOVIE, client=client)

    assert [row.title for row in results] == ["Film.2020.1080p.WEB-DL.x264-GRP"]
    row = results[0]
    assert row.download_url == "http://prowlarr:9696/1/download?guid=g1"
    assert row.indexer == "Tracker One"
    assert row.infohash.lower() == "abcdef0123456789abcdef0123456789abcdef01"
    assert row.seeders == 12 and row.size == 2147483648

    search_calls = [call for call in client.calls if call[0].endswith("/api/v1/search")]
    assert search_calls, client.calls
    _, params = search_calls[0]
    assert params["query"] == "Film 2020"
    assert params["categories"] == indexers.CATEGORY_MOVIE
    assert params["apikey"] == "k"

    # The protocol map is cached, so a second search only asks for results.
    before = len(client.calls)
    indexers.search_prowlarr(indexer, "Film 2020", client=client)
    assert len(client.calls) == before + 1


def test_prowlarr_kind_is_dispatched_from_search(monkeypatch):
    from app import indexers

    calls = []

    def fake(indexer, query, **kwargs):
        calls.append(indexer.kind)
        return []

    monkeypatch.setattr(indexers, "search_prowlarr", fake)
    indexers.search(indexers.Indexer(name="Prowlarr", url="http://p", api_key="k",
                                     kind="prowlarr"), "dune")
    assert calls == ["prowlarr"]


def test_an_old_database_gains_the_prowlarr_kind(tmp_path):
    """An existing core.sqlite3 predates the aggregated indexer; it must widen."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE indexers (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL UNIQUE,
            url     TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            kind    TEXT NOT NULL DEFAULT 'torznab' CHECK (kind IN ('torznab','newznab')),
            cats    TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO indexers(name, url, kind) VALUES('old', 'http://old', 'torznab');
    """)
    conn.commit()
    conn.close()

    from app import db
    upgraded = db.connect(path)
    try:
        assert db.one(upgraded, "SELECT name FROM indexers")["name"] == "old"
        upgraded.execute("INSERT INTO indexers(name, url, kind) VALUES('p', 'http://p', 'prowlarr')")
        assert db.one(upgraded, "SELECT kind FROM indexers WHERE name = 'p'")["kind"] == "prowlarr"
    finally:
        upgraded.close()
