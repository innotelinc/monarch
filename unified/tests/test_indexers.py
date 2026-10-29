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
