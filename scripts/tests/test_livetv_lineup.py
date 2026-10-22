#!/usr/bin/env python3
"""Tests for scripts/livetv_lineup.py.

The decisions worth asserting are the ones that put a channel number on a
stream: which lineup entry a name binds to (order matters, and the specific
service must beat the affiliate), how sub-feeds share a cable number, and that a
stream with no cable counterpart gets a category band instead of borrowing a
number that belongs to someone else. The guide half is asserted the same way —
the programmes are not ours to invent, so they must survive renumbering
byte-for-byte in count and content.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "livetv_lineup.py"
spec = importlib.util.spec_from_file_location("livetv_lineup", SCRIPT)
assert spec and spec.loader
lineup_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lineup_mod)

def entry(name: str, tvg_id: str = "", url: str = "http://stream.test/x") -> dict:
    return {"name": name, "attrs": {"tvg-id": tvg_id}, "url": url, "group": ""}


# The real dial. "CBS News 24/7" and the assistant's own hypotheticals have no
# entry here, which is the point: a FAST service with no cable counterpart must
# not be given somebody else's number.
LINEUP = None  # set in setUpModule, so importing the module cannot half-load the file


def setUpModule() -> None:  # noqa: N802 - unittest's name
    global LINEUP
    LINEUP = lineup_mod.load_lineup()


def dial(name: str):
    """The number a real stream name gets, or None."""
    hit = lineup_mod.lineup_of(name, LINEUP)
    return (hit or {}).get("number")


class TheRealLineupFile(unittest.TestCase):
    """The file that ships, not a fixture: a bad row is a silently missing channel."""

    def test_every_row_is_a_number_and_a_name(self) -> None:
        self.assertGreater(len(LINEUP), 1000)
        for item in LINEUP:
            self.assertIsInstance(item["number"], (int, float), item)
            self.assertTrue(item["name"].strip(), item)
            self.assertTrue(item["tokens"], item)
            self.assertIn(item["category"], lineup_mod.CATEGORY_ORDER)

    def test_the_numbers_are_the_operators_and_are_unique(self) -> None:
        """Every number traces to the supplied dial, and no two rows claim one."""
        numbers = [item["number"] for item in LINEUP]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual({item["source"] for item in LINEUP}, {"comcast-springfield"})

    def test_the_springfield_locals_are_where_comcast_files_them(self) -> None:
        # 2 WGBY, 3 WSHM, 4 WGGB, 5 WWLP — the locals the operator's list opens with.
        for name, number in (("WGBY", 2), ("WSHM", 3), ("WGGB", 4), ("WWLP", 5)):
            self.assertEqual(dial(name), number, name)


class Matching(unittest.TestCase):
    """Which dial slot a stream takes, and — as importantly — when it takes none."""

    def test_a_network_takes_its_number(self) -> None:
        for name, number in (
            ("ESPN", 49),
            ("ESPN2", 50),
            ("TNT", 33),
            ("Discovery Channel", 39),
            ("Food Network", 67),
            ("A&E", 37),
            ("USA Network", 35),
            ("Disney Channel", 24),
            ("HBO", 301),
        ):
            self.assertEqual(dial(name), number, name)

    def test_an_hd_feed_takes_the_hd_dial_row(self) -> None:
        # The dial lists both positions as rows, so the stream says which one it
        # belongs on: "CNN HD" is the 842 simulcast, while a name that says
        # nothing stays on the classic 42. Handing the HD feed the SD number is
        # how the HD block becomes the block nothing reaches.
        self.assertEqual(dial("CNN"), 42)
        self.assertEqual(dial("CNN HD"), 842)
        self.assertEqual(dial("CNN HD East"), 842)

    def test_a_resolution_counts_as_the_feed_too(self) -> None:
        # The playlist spells quality in brackets more often than as a word, so
        # "Fox News Channel (720p)" is the HD feed and belongs on 841.
        self.assertEqual(dial("Fox News Channel"), 41)
        self.assertEqual(dial("Fox News Channel (720p)"), 841)

    def test_a_specific_feed_still_beats_the_hd_row(self) -> None:
        # Specificity is scored before the feed: CNN en Español HD is the Spanish
        # service on 709, not CNN's 842.
        self.assertEqual(dial("CNN en Español HD"), 709)

    def test_the_dials_legacy_name_binds_to_the_name_people_use(self) -> None:
        self.assertEqual(dial("CNN"), dial("Cable News Network"))
        self.assertEqual(dial("HGTV"), 32)
        self.assertEqual(dial("MSNBC"), 65)

    def test_a_specific_feed_beats_the_parent_channel(self) -> None:
        # CNN en Español is on 709, and must not be handed CNN's 42.
        self.assertEqual(dial("CNN en Español"), 709)
        self.assertEqual(dial("Nat Geo Wild"), 232)

    def test_a_generic_word_alone_cannot_carry_a_match(self) -> None:
        # "CBS News 24/7" contains "news"; every news channel does. The dial's
        # "Cable News Network" reduces to that one word, and must not claim it.
        self.assertIsNone(dial("CBS News 24/7"))
        self.assertIsNone(dial("Some Obscure FAST Channel"))

    def test_the_weather_channel_still_claims_its_own_name(self) -> None:
        # Its identifying word is generic, so it matches only its own name — which
        # is exactly what a stream called "The Weather Channel" says.
        self.assertEqual(dial("The Weather Channel"), 47)
        self.assertEqual(dial("FOX Weather"), 1108)

    def test_the_fast_services_on_the_dial_get_their_own_numbers(self) -> None:
        for name, number in (
            ("ABC News Live", 14017),
            ("NBC News NOW", 14005),
            ("Sky News", 14006),
            ("Xumo Free Movies", 14055),
        ):
            self.assertEqual(dial(name), number, name)


class Parsing(unittest.TestCase):
    """The playlist is somebody else's file, and it is not always tidy."""

    def test_a_comma_inside_an_attribute_is_not_the_title(self) -> None:
        # iptv-org ships `http-user-agent="…(KHTML, like Gecko)…"`, so a split on
        # the *first* comma makes half a user agent the channel's name — which is
        # how channel 33 came to be listed as "like Gecko) Chrome/144.0.0.0". The
        # title follows the comma that ends the attributes, and that comma is the
        # first one *outside* a quoted value.
        line = (
            '#EXTINF:-1 tvg-id="TNTInternational.ru@SD" tvg-logo="http://x/l.png" '
            'http-user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36" '
            'group-title="Entertainment",TNT International (480p)'
        )
        parsed = lineup_mod.parse_m3u(line + "\nhttp://stream.test/tnt\n")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "TNT International (480p)")
        self.assertEqual(parsed[0]["attrs"]["tvg-id"], "TNTInternational.ru@SD")
        self.assertEqual(parsed[0]["attrs"]["group-title"], "Entertainment")
        self.assertEqual(parsed[0]["url"], "http://stream.test/tnt")

    def test_a_title_may_contain_a_comma_itself(self) -> None:
        parsed = lineup_mod.parse_m3u(
            '#EXTINF:-1 tvg-id="X.us@SD",X, The Channel\nhttp://stream.test/x\n')
        self.assertEqual(parsed[0]["name"], "X, The Channel")


class Simulcast(unittest.TestCase):
    """An HD feed leaves its SD row empty, and both numbers are one channel."""

    def test_an_hd_feed_also_keeps_its_sd_row(self) -> None:
        # 8 Jewelry Television / 1032 Jewelry Television HD are two rows for one
        # channel, and the HD stream takes 1032 — so 8 is filled with it too.
        stream = entry("Jewelry TV 2 (720p)", "JewelryTV2.us@SD")
        lineup_mod.plan([stream], LINEUP)
        self.assertEqual((stream["lineup"] or {}).get("number"), 1032)
        self.assertEqual([row["lineup"]["number"] for row in lineup_mod.simulcast_rows([stream], LINEUP)],
                         [8])

    def test_an_sd_feed_is_not_duplicated_onto_its_own_row(self) -> None:
        # A name that claims no quality is already on the classic position, and
        # the HD row above it is not ours to fill.
        stream = entry("CNN", "CNN.us@SD")
        lineup_mod.plan([stream], LINEUP)
        self.assertEqual(lineup_mod.simulcast_rows([stream], LINEUP), [])

    def test_a_row_another_stream_already_holds_is_left_alone(self) -> None:
        # CNN and CNN HD are both in the playlist: 42 is taken by the SD feed and
        # must not be handed the HD one as well.
        hd, sd = entry("CNN HD", "CNN.us@HD"), entry("CNN", "CNN.us@SD")
        lineup_mod.plan([hd, sd], LINEUP)
        self.assertEqual(lineup_mod.simulcast_rows([hd, sd], LINEUP), [])

    def test_a_stream_with_no_cable_row_is_left_alone(self) -> None:
        stream = entry("Some Obscure FAST Channel (720p)", "Obscure.us@FAST")
        lineup_mod.plan([stream], LINEUP)
        self.assertEqual(lineup_mod.simulcast_rows([stream], LINEUP), [])


class Numbers(unittest.TestCase):
    def plan(self, names: list[str]) -> tuple[list[dict], dict[int, str]]:
        entries = [entry(name) for name in names]
        lineup_mod.plan(entries, LINEUP)
        return entries, lineup_mod.dial_numbers(entries)

    def test_a_cable_network_takes_its_number(self) -> None:
        _, numbers = self.plan(["A&E Crime 360"])
        self.assertEqual(numbers[0], "37")

    def test_sub_feeds_share_the_number_with_decimal_suffixes(self) -> None:
        _, numbers = self.plan(["A&E Crime 360", "A&E Lives", "A&E Movies"])
        self.assertEqual([numbers[i] for i in range(3)], ["37", "37.1", "37.2"])

    def test_a_stream_with_no_number_gets_a_category_band(self) -> None:
        entries, numbers = self.plan(["Some Obscure News Channel", "A&E Crime 360"])
        self.assertEqual(numbers[1], "37")
        self.assertEqual(numbers[0], str(lineup_mod.BANDS["News"]))
        self.assertNotEqual(numbers[0], "37")

    def test_a_category_band_never_lands_on_a_cable_number(self) -> None:
        """The bands and the dial share one number space, so they must not collide."""
        entries, numbers = self.plan(["Some Obscure News Channel", "ESPN"])
        self.assertEqual(numbers[1], "49")
        self.assertNotEqual(numbers[0], "49")

    def test_numbers_are_unique(self) -> None:
        _, numbers = self.plan(["A&E Crime 360", "A&E Lives", "Obscure One", "Obscure Two"])
        self.assertEqual(len(set(numbers.values())), len(numbers))

    def test_the_dial_orders_cable_numbers_first(self) -> None:
        entries, numbers = self.plan(["Obscure One", "A&E Crime 360", "WSHM News"])
        order = lineup_mod.order_for_dial(entries, numbers)
        self.assertEqual([entries[i]["name"] for i in order[:2]], ["WSHM News", "A&E Crime 360"])


class Rendering(unittest.TestCase):
    def test_an_entry_carries_its_number_and_group(self) -> None:
        entries = [entry("A&E Crime 360", "AETV.us@SD")]
        lineup_mod.plan(entries, LINEUP)
        numbers = lineup_mod.dial_numbers(entries)
        lineup_mod.assign_guide_ids(entries, numbers)
        text = lineup_mod.render(entries, numbers, [0], "Cable channels")
        self.assertIn('tvg-chno="37"', text)
        self.assertIn('group-title="Cable channels"', text)
        self.assertIn('tvg-id="AETV.us@SD.37"', text)
        self.assertTrue(text.endswith("http://stream.test/x\n"))

    def test_a_callable_group_labels_each_entry(self) -> None:
        entries = [entry("A&E Crime 360")]
        lineup_mod.plan(entries, LINEUP)
        numbers = lineup_mod.dial_numbers(entries)
        lineup_mod.assign_guide_ids(entries, numbers)
        text = lineup_mod.render(entries, numbers, [0], lambda e: e["category"])
        self.assertIn('group-title="Entertainment"', text)

    def test_streams_with_the_same_upstream_id_get_distinct_ids(self) -> None:
        # The bug this prevents: Jellyfin imported only the last of these two,
        # so channel 42 was absent even though the dial listed it.
        entries = [entry("CNN", "CNN.us@SD"), entry("CNN HD", "CNN.us@SD")]
        lineup_mod.plan(entries, LINEUP)
        numbers = lineup_mod.dial_numbers(entries)
        lineup_mod.assign_guide_ids(entries, numbers)
        self.assertEqual([e["guide_id"] for e in entries],
                         ["CNN.us@SD.42", "CNN.us@SD.842"])

    def test_the_same_feed_on_two_numbers_gets_distinct_playback_urls(self) -> None:
        # Jellyfin also collapses rows that share a URL. The second occurrence
        # gets a query parameter the origin ignores; the first is untouched.
        entries = [entry("CNN", "CNN.us@SD"), entry("CNN HD", "CNN.us@SD")]
        lineup_mod.plan(entries, LINEUP)
        numbers = lineup_mod.dial_numbers(entries)
        lineup_mod.assign_guide_ids(entries, numbers)
        lineup_mod.assign_stream_urls(entries, numbers)
        self.assertEqual(entries[0]["stream_url"], "http://stream.test/x")
        self.assertEqual(entries[1]["stream_url"], "http://stream.test/x?monarch=842")

    def test_playback_attributes_survive_a_rewrite(self) -> None:
        # A source may only answer with a referrer/user agent; the rendered dial
        # has to keep carrying it.
        stream = entry("CNN", "CNN.us@SD")
        stream["attrs"]["http-referrer"] = "https://usnewson.com"
        stream["attrs"]["http-user-agent"] = "Monarch/1"
        lineup_mod.plan([stream], LINEUP)
        numbers = lineup_mod.dial_numbers([stream])
        lineup_mod.assign_guide_ids([stream], numbers)
        text = lineup_mod.render([stream], numbers, [0], "News")
        self.assertIn('http-referrer="https://usnewson.com"', text)
        self.assertIn('http-user-agent="Monarch/1"', text)


GUIDE = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Unmatched.us@SD"><display-name>Unmatched Channel</display-name></channel>
  <channel id="AETV.us@SD"><display-name>Crime 360</display-name></channel>
  <channel id="QVC.us@SD"><display-name>QVC</display-name></channel>
  <programme start="20260919200000 +0000" stop="20260919210000 +0000" channel="AETV.us@SD">
    <title>Forensic Files</title>
  </programme>
  <programme start="20260919200000 +0000" stop="20260919210000 +0000" channel="QVC.us@SD">
    <title>Football Team Shop</title>
  </programme>
</tv>
"""


class Guide(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = {
            "AETV.us@SD": [("AETV.us@SD.37", "37")],
            "QVC.us@SD": [("QVC.us@SD.58", "58")],
        }
        self.root = ET.fromstring(lineup_mod.renumber_guide(GUIDE, self.mapping))

    def test_programmes_are_left_exactly_as_they_were(self) -> None:
        before = ET.fromstring(GUIDE)
        self.assertEqual(len(self.root.findall("programme")), len(before.findall("programme")))
        titles = [p.findtext("title") for p in self.root.findall("programme")]
        self.assertIn("Forensic Files", titles)
        self.assertIn("Football Team Shop", titles)
        channels = {p.get("channel") for p in self.root.findall("programme")}
        self.assertEqual(channels, {"AETV.us@SD.37", "QVC.us@SD.58"})

    def test_a_numbered_channel_shows_its_number(self) -> None:
        channel = next(c for c in self.root.findall("channel") if c.get("id") == "QVC.us@SD.58")
        self.assertEqual(channel.findtext("display-name"), "58 QVC")
        self.assertEqual(channel.findtext("lcn"), "58")

    def test_a_channel_with_no_number_keeps_its_name(self) -> None:
        channel = next(c for c in self.root.findall("channel") if c.get("id") == "Unmatched.us@SD")
        self.assertEqual(channel.findtext("display-name"), "Unmatched Channel")
        self.assertIsNone(channel.find("lcn"))

    def test_channels_are_ordered_by_number(self) -> None:
        ids = [c.get("id") for c in self.root.findall("channel")]
        self.assertEqual(ids, ["AETV.us@SD.37", "QVC.us@SD.58", "Unmatched.us@SD"])

    def test_two_dial_rows_for_one_network_are_two_guide_channels(self) -> None:
        """Jellyfin keeps one channel per tvg-id, so 42 and 842 need distinct ids."""
        guide = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="CNN.us@SD"><display-name>CNN</display-name></channel>
  <programme start="20260919200000 +0000" stop="20260919210000 +0000" channel="CNN.us@SD">
    <title>CNN Newsroom</title>
  </programme>
</tv>
"""
        root = ET.fromstring(lineup_mod.renumber_guide(
            guide, {"CNN.us@SD": [("CNN.us@SD.42", "42"), ("CNN.us@SD.842", "842")]}))
        channels = root.findall("channel")
        self.assertEqual([c.get("id") for c in channels], ["CNN.us@SD.42", "CNN.us@SD.842"])
        self.assertEqual([c.findtext("lcn") for c in channels], ["42", "842"])
        self.assertEqual([c.findtext("display-name") for c in channels],
                         ["42 CNN", "842 CNN"])
        # The same programme, on both numbers: the guide data is not invented,
        # it is the one programme the upstream guide already had.
        programmes = root.findall("programme")
        self.assertEqual([p.get("channel") for p in programmes],
                         ["CNN.us@SD.42", "CNN.us@SD.842"])
        self.assertEqual([p.findtext("title") for p in programmes],
                         ["CNN Newsroom", "CNN Newsroom"])

    def test_the_same_upstream_programme_is_not_duplicated_twice(self) -> None:
        # One number, one programme: the mapping is a list, not a licence to
        # repeat a stream that the playlist listed once.
        self.assertEqual(len(self.root.findall("programme")), 2)


class HealthDatabase(unittest.TestCase):
    def test_reads_url_status_and_tolerates_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "monitor.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE channels (number TEXT, url TEXT, status TEXT)")
            conn.executemany("INSERT INTO channels VALUES (?, ?, ?)",
                             [("35", "http://a", "ok"), ("835", "http://b", "fail")])
            conn.commit()
            conn.close()
            self.assertEqual(lineup_mod.read_health_db(str(db)),
                             {"http://a": "ok", "http://b": "fail"})
            self.assertEqual(lineup_mod.read_health_db(str(Path(tmp) / "absent.sqlite")), {})
            self.assertEqual(lineup_mod.read_health_db(None), {})


class HealthFailover(unittest.TestCase):
    """A dead feed is dropped only when the same guide channel has a live one."""

    def test_a_dead_primary_is_dropped_when_the_backup_is_live(self) -> None:
        primary = entry("USA Network", "USANetwork.us@East", "http://primary.test/usa")
        backup = entry("USA Network HD (backup)", "USANetwork.us@East",
                       "http://backup.test/usa")
        health = {"http://primary.test/usa": "fail", "http://backup.test/usa": "ok"}
        kept = lineup_mod.drop_dead_duplicates([primary, backup], health)
        self.assertEqual(kept, [backup])

    def test_a_dead_channel_with_no_live_sibling_is_left_alone(self) -> None:
        # A missing channel is worse than one that may recover; the monitor will
        # try again on its next run.
        primary = entry("USA Network", "USANetwork.us@East", "http://primary.test/usa")
        backup = entry("USA Network HD (backup)", "USANetwork.us@East",
                       "http://backup.test/usa")
        health = {"http://primary.test/usa": "fail", "http://backup.test/usa": "fail"}
        self.assertEqual(len(lineup_mod.drop_dead_duplicates([primary, backup], health)), 2)

    def test_an_unconfirmed_feed_is_not_dropped(self) -> None:
        # The monitor's first run records `unknown`, not `fail`, so a baseline
        # run cannot renumber the dial.
        feed = entry("USA Network", "USANetwork.us@East", "http://primary.test/usa")
        self.assertEqual(lineup_mod.drop_dead_duplicates([feed], {"http://primary.test/usa": "unknown"}), [feed])

    def test_a_feed_without_a_guide_id_is_never_dropped(self) -> None:
        feed = entry("Some FAST channel", "", "http://fast.test/x")
        self.assertEqual(lineup_mod.drop_dead_duplicates([feed], {"http://fast.test/x": "fail"}), [feed])

    def test_no_health_state_is_a_no_op(self) -> None:
        feed = entry("USA Network", "USANetwork.us@East")
        self.assertEqual(lineup_mod.drop_dead_duplicates([feed], {}), [feed])


if __name__ == "__main__":
    unittest.main()
