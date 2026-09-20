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
        text = lineup_mod.render(entries, numbers, [0], "Cable channels")
        self.assertIn('tvg-chno="37"', text)
        self.assertIn('group-title="Cable channels"', text)
        self.assertIn('tvg-id="AETV.us@SD"', text)
        self.assertTrue(text.endswith("http://stream.test/x\n"))

    def test_a_callable_group_labels_each_entry(self) -> None:
        entries = [entry("A&E Crime 360")]
        lineup_mod.plan(entries, LINEUP)
        numbers = lineup_mod.dial_numbers(entries)
        text = lineup_mod.render(entries, numbers, [0], lambda e: e["category"])
        self.assertIn('group-title="Entertainment"', text)


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
        self.root = ET.fromstring(lineup_mod.renumber_guide(GUIDE, {"AETV.us@SD": "37", "QVC.us@SD": "58"}))

    def test_programmes_are_left_exactly_as_they_were(self) -> None:
        before = ET.fromstring(GUIDE)
        self.assertEqual(len(self.root.findall("programme")), len(before.findall("programme")))
        titles = [p.findtext("title") for p in self.root.findall("programme")]
        self.assertIn("Forensic Files", titles)
        self.assertIn("Football Team Shop", titles)
        channels = {p.get("channel") for p in self.root.findall("programme")}
        self.assertEqual(channels, {"AETV.us@SD", "QVC.us@SD"})

    def test_a_numbered_channel_shows_its_number(self) -> None:
        channel = next(c for c in self.root.findall("channel") if c.get("id") == "QVC.us@SD")
        self.assertEqual(channel.findtext("display-name"), "58 QVC")
        self.assertEqual(channel.findtext("lcn"), "58")

    def test_a_channel_with_no_number_keeps_its_name(self) -> None:
        channel = next(c for c in self.root.findall("channel") if c.get("id") == "Unmatched.us@SD")
        self.assertEqual(channel.findtext("display-name"), "Unmatched Channel")
        self.assertIsNone(channel.find("lcn"))

    def test_channels_are_ordered_by_number(self) -> None:
        ids = [c.get("id") for c in self.root.findall("channel")]
        self.assertEqual(ids, ["AETV.us@SD", "QVC.us@SD", "Unmatched.us@SD"])


if __name__ == "__main__":
    unittest.main()
