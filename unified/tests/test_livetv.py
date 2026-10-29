from datetime import datetime, timezone

from app import livetv

M3U = """#EXTM3U
#EXTINF:-1 tvg-id="bbc1.uk" tvg-name="BBC One" tvg-logo="http://logo/bbc.png" group-title="News",BBC One HD
http://stream/bbc1
#EXTINF:-1 tvg-id="itv.uk" group-title="News",ITV
http://stream/itv
#EXTINF:-1 tvg-id="sky.uk" group-title="Sport",Sky Sports
http://stream/sky
"""

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="bbc1.uk"><display-name>BBC One</display-name></channel>
  <programme channel="bbc1.uk" start="20261029180000 +0000" stop="20261029190000 +0000">
    <title>News at Six</title><desc>The day's events.</desc><category>News</category>
  </programme>
  <programme channel="bbc1.uk" start="20261029190000 +0000" stop="20261029200000 +0000">
    <title>Question Time</title>
  </programme>
</tv>
"""


def test_parse_m3u_channels():
    channels = livetv.parse_m3u(M3U)
    assert [c.name for c in channels] == ["BBC One", "ITV", "Sky Sports"]
    assert channels[0].group == "News"
    assert channels[0].url == "http://stream/bbc1"


def test_parse_xmltv_programmes():
    names, programmes = livetv.parse_xmltv(XMLTV)
    assert names["bbc1.uk"] == "BBC One"
    assert [p.title for p in programmes] == ["News at Six", "Question Time"]
    assert programmes[0].start == datetime(2026, 10, 29, 18, tzinfo=timezone.utc)


def test_broken_guide_is_empty_not_fatal():
    assert livetv.parse_xmltv("<tv><programme") == ({}, [])


def test_numbering_is_deterministic_and_grouped():
    numbered = livetv.number_channels(livetv.parse_m3u(M3U), base=2)
    assert [(c.group, c.name, c.number) for c in numbered] == [
        ("News", "BBC One", 2),
        ("News", "ITV", 3),
        ("Sport", "Sky Sports", 4),
    ]
    # Re-parsing the same playlist must give the same numbers.
    again = livetv.number_channels(livetv.parse_m3u(M3U), base=2)
    assert [(c.name, c.number) for c in again] == [(c.name, c.number) for c in numbered]


def test_now_and_next_at_midpoint():
    _, programmes = livetv.parse_xmltv(XMLTV)
    at = datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc)
    state = livetv.now_and_next("bbc1.uk", programmes, at)
    assert state["now"]["title"] == "News at Six"
    assert state["next"]["title"] == "Question Time"
    assert state["progress"] == 50.0


def test_now_and_next_between_programmes():
    _, programmes = livetv.parse_xmltv(XMLTV)
    at = datetime(2026, 10, 29, 19, 30, tzinfo=timezone.utc)
    state = livetv.now_and_next("bbc1.uk", programmes, at)
    assert state["now"]["title"] == "Question Time"
    assert state["next"] is None


def test_lineup_has_numbers_and_titles():
    channels = livetv.number_channels(livetv.parse_m3u(M3U), base=1)
    _, programmes = livetv.parse_xmltv(XMLTV)
    at = datetime(2026, 10, 29, 18, 10, tzinfo=timezone.utc)
    rows = livetv.lineup(channels, programmes, at)
    assert rows[0]["number"] == 1
    assert rows[0]["now"]["title"] == "News at Six"
    assert rows[1]["now"] is None
