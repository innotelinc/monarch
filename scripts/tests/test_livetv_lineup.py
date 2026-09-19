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

LINEUP = [
    {"name": "WSHM (CBS)", "number": 3, "category": "Local", "source": "springfield",
     "kind": "local", "rules": [__import__("re").compile(r"\bwshm\b", 2)]},
    {"name": "CBS News 24/7", "number": None, "category": "News", "source": "fast-only",
     "kind": "network", "rules": [__import__("re").compile(r"\bcbs news\b", 2)]},
    {"name": "A&E", "number": 37, "category": "Entertainment", "source": "ma-convention",
     "kind": "network", "rules": [__import__("re").compile(r"\ba&e\b", 2)]},
]


def entry(name: str, tvg_id: str = "", url: str = "http://stream.test/x") -> dict:
    return {"name": name, "attrs": {"tvg-id": tvg_id}, "url": url, "group": ""}


class TheRealLineupFile(unittest.TestCase):
    """The file that ships, not a fixture: a typo'd alias is a silent misnumber."""

    def test_it_loads_with_aliases_compiled(self) -> None:
        lineup = lineup_mod.load_lineup()
        self.assertGreater(len(lineup), 30)
        for item in lineup:
            self.assertTrue(item["rules"], f"{item['name']} has no aliases")
            self.assertIn(item["category"], lineup_mod.CATEGORY_ORDER)

    def test_every_numbered_entry_says_where_the_number_came_from(self) -> None:
        for item in lineup_mod.load_lineup():
            if item.get("number") is not None:
                self.assertIn(item.get("source"), {"springfield", "ma-convention"},
                              f"{item['name']} has a number and no traceable source")

    def test_the_specific_service_beats_the_affiliate(self) -> None:
        # "CBS News 24/7" is a streaming news service; the CBS affiliate's number
        # must not land on it.
        lineup = lineup_mod.load_lineup()
        self.assertEqual(lineup_mod.lineup_of("CBS News 24/7", lineup)["source"], "fast-only")
        self.assertEqual(lineup_mod.lineup_of("CBS News New York", lineup)["source"], "fast-only")


class Matching(unittest.TestCase):
    def test_a_name_binds_to_its_network(self) -> None:
        self.assertEqual(lineup_mod.lineup_of("A&E Crime 360", LINEUP)["number"], 37)

    def test_file_order_decides_ties(self) -> None:
        self.assertEqual(lineup_mod.lineup_of("CBS News 24/7", LINEUP)["number"], None)

    def test_an_unknown_name_matches_nothing(self) -> None:
        self.assertIsNone(lineup_mod.lineup_of("Some Obscure FAST Channel", LINEUP))


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
