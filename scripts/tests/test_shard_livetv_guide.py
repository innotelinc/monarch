#!/usr/bin/env python3
"""Tests for scripts/shard-livetv-guide.py.

The decisions worth asserting are the ones that decide what Jellyfin ends up
reading: that the channel list is grouped by the site that serves it and split so
no grab is handed more than it was sized for, that the part names are stable (a
`guide-<site>.xml` left from yesterday must not be merged into today's output), and
that the merge keeps one entry per channel and one programme per slot with the
first part winning. Everything else in the script is either a subprocess or a
print.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "shard-livetv-guide.py"
spec = importlib.util.spec_from_file_location("shard_livetv_guide", SCRIPT)
assert spec and spec.loader
shard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shard)


def channel(site: str, xmltv_id: str, name: str = "Some Channel") -> str:
    return (f'<channel site="{site}" site_id="{xmltv_id}@{site}" lang="en" '
            f'xmltv_id="{xmltv_id}">{name}</channel>')


def channels_file(*entries: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + "\n".join(entries) + "\n</channels>\n"


def guide(language: str = "en") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="iptv-org/epg">\n'
            f'  <channel id="A.us@SD"><display-name lang="{language}">A</display-name></channel>\n'
            '  <programme start="20260920060000 +0000" stop="20260920070000 +0000" channel="A.us@SD">\n'
            '    <title lang="en">Morning</title>\n  </programme>\n</tv>\n')


class ParseChannels(unittest.TestCase):
    def test_groups_by_site_in_file_order(self) -> None:
        grouped = shard.parse_channels(channels_file(
            channel("xumo.tv", "A.us@SD"),
            channel("tvtv.us", "B.us@SD"),
            channel("xumo.tv", "C.us@SD"),
        ))
        self.assertEqual(list(grouped), ["xumo.tv", "tvtv.us"])
        self.assertEqual(len(grouped["xumo.tv"]), 2)
        self.assertEqual(len(grouped["tvtv.us"]), 1)

    def test_ignores_entries_with_no_site(self) -> None:
        # A hand-edited list may hold a bare entry; it belongs to no grab, and
        # guessing one would send it to a site that does not serve it.
        grouped = shard.parse_channels(
            '<?xml version="1.0"?><channels>\n<channel xmltv_id="A.us@SD">A</channel>\n</channels>')
        self.assertEqual(grouped, {})


class Chunking(unittest.TestCase):
    def test_splits_into_pieces_of_at_most_the_size(self) -> None:
        entries = [channel("xumo.tv", f"E{i}.us@SD") for i in range(5)]
        self.assertEqual([len(piece) for piece in shard.chunk(entries, 2)], [2, 2, 1])

    def test_size_zero_means_one_grab(self) -> None:
        entries = [channel("xumo.tv", f"E{i}.us@SD") for i in range(5)]
        self.assertEqual([len(piece) for piece in shard.chunk(entries, 0)], [5])

    def test_an_empty_site_still_yields_one_piece(self) -> None:
        # Otherwise the loop body never runs and the site silently vanishes.
        self.assertEqual([len(piece) for piece in shard.chunk([], 4)], [0])


class ContainerPaths(unittest.TestCase):
    def test_a_host_path_becomes_the_mounted_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            epg = Path(tmp)
            target = epg / "parts" / "channels-xumo.tv.xml"
            target.parent.mkdir(parents=True)
            target.write_text("x", encoding="utf-8")
            self.assertEqual(shard.container_path(target, epg), "public/parts/channels-xumo.tv.xml")


class GrabberCommand(unittest.TestCase):
    def test_days_is_a_flag_because_the_image_never_passes_it(self) -> None:
        command = shard.grabber_command("iptv", "public/parts/a.xml", "public/parts/b.xml", 3)
        self.assertIn("--days=3", command)
        self.assertIn("--channels=public/parts/a.xml", command)
        self.assertIn("--output=public/parts/b.xml", command)
        self.assertEqual(command[:5], ["docker", "exec", "iptv", "npm", "run"])


class MergeGuides(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_two_parts_become_one_guide(self) -> None:
        first = self.write("guide-a.xml", guide())
        second = self.write("guide-b.xml", guide().replace("A.us@SD", "B.us@SD"))
        out = self.dir / "guide.xml"

        counts = shard.merge_guides([first, second], out)

        root = ET.parse(out).getroot()
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["A.us@SD", "B.us@SD"])
        self.assertEqual(len(root.findall("programme")), 2)
        self.assertEqual(counts, {"channels": 2, "programmes": 2})

    def test_one_channel_from_two_sites_is_one_channel(self) -> None:
        first = self.write("guide-a.xml", guide())
        second = self.write("guide-b.xml", guide(language="fr"))
        out = self.dir / "guide.xml"

        shard.merge_guides([first, second], out)

        root = ET.parse(out).getroot()
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["A.us@SD"])

    def test_the_first_part_wins_a_slot_two_sites_publish(self) -> None:
        first = self.write("guide-a.xml", guide())
        second = self.write("guide-b.xml", guide().replace("Morning", "Matinee"))
        out = self.dir / "guide.xml"

        shard.merge_guides([first, second], out)

        titles = [t.text for t in ET.parse(out).getroot().findall("programme/title")]
        self.assertEqual(titles, ["Morning"], "parts are merged in channel-list order, most useful first")

    def test_the_same_guide_is_produced_twice(self) -> None:
        first = self.write("guide-a.xml", guide())
        second = self.write("guide-b.xml", guide().replace("A.us@SD", "B.us@SD"))
        one, two = self.dir / "one.xml", self.dir / "two.xml"

        shard.merge_guides([first, second], one)
        shard.merge_guides([first, second], two)

        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_channels_come_before_programmes(self) -> None:
        # Jellyfin indexes programmes against channels as it reads; a guide that
        # names a channel after using it is the shape this is written to avoid.
        first = self.write("guide-a.xml", guide())
        out = self.dir / "guide.xml"

        shard.merge_guides([first], out)

        tags = [element.tag for _, element in ET.iterparse(out, events=("start",))]
        self.assertLess(tags.index("channel"), tags.index("programme"))


if __name__ == "__main__":
    unittest.main()
