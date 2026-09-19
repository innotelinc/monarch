#!/usr/bin/env python3
"""Tests for the Live TV EPG channel-list assembly in `init/init.py`.

The guide covers exactly the channels this list names, so a wrong dedupe key is
not a cosmetic bug: keying on `site_id` alone silently drops a whole site's copy
of a channel, because two guide sites both call their CNN entry `cnn`. The tests
below assert the pair, not the id, is what makes a duplicate — and that the
measured site order (which decides which source the grabber is offered first) is
what actually gets assembled.

No network: the site files are served from a temp directory through `file://`,
which `urllib.request.urlopen` reads exactly like the raw.githubusercontent URL
the real function uses.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent.parent / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)


def site_file(root: Path, site: str, body: str) -> None:
    directory = root / site
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{site}.channels.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + body + "\n</channels>\n",
        encoding="utf-8",
    )


def channel(site: str, site_id: str, xmltv_id: str, name: str) -> str:
    return (f'  <channel site="{site}" site_id="{site_id}" '
            f'xmltv_id="{xmltv_id}">{name}</channel>')


class TheAssembledChannelList(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved_sites = init_mod.LIVETV_EPG_SITES
        self._saved_raw = init_mod.LIVETV_EPG_RAW
        init_mod.LIVETV_EPG_RAW = "file://" + str(self.root / "{site}" / "{site}.channels.xml")

    def tearDown(self) -> None:
        init_mod.LIVETV_EPG_SITES = self._saved_sites
        init_mod.LIVETV_EPG_RAW = self._saved_raw
        self._tmp.cleanup()

    def assemble(self, sites: list[str]) -> str | None:
        init_mod.LIVETV_EPG_SITES = sites
        return init_mod._fetch_livetv_channels()

    def test_the_same_site_id_from_two_sites_is_not_a_duplicate(self) -> None:
        site_file(self.root, "xumo.tv", channel("xumo.tv", "cnn", "CNN.us@SD", "CNN"))
        site_file(self.root, "tvtv.us", channel("tvtv.us", "cnn", "CNN.us@SD", "CNN"))
        text = self.assemble(["xumo.tv", "tvtv.us"])
        assert text is not None
        # Both sources are kept so the grabber can merge their listings; dropping
        # the second is what a site_id-only key would do.
        self.assertEqual(text.count("<channel "), 2)
        self.assertIn('site="tvtv.us"', text)

    def test_the_same_channel_twice_from_one_site_is_a_duplicate(self) -> None:
        site_file(self.root, "xumo.tv",
                  channel("xumo.tv", "cnn", "CNN.us@SD", "CNN") + "\n" +
                  channel("xumo.tv", "cnn", "CNN.us@SD", "CNN"))
        text = self.assemble(["xumo.tv"])
        assert text is not None
        self.assertEqual(text.count("<channel "), 1)

    def test_a_channel_with_no_guide_id_is_dropped(self) -> None:
        site_file(self.root, "xumo.tv",
                  channel("xumo.tv", "x1", "CNN.us@SD", "CNN") + "\n" +
                  channel("xumo.tv", "x2", "", "Mystery TV"))
        text = self.assemble(["xumo.tv"])
        assert text is not None
        self.assertIn("CNN", text)
        self.assertNotIn("Mystery TV", text)

    def test_sites_are_assembled_in_the_order_they_are_listed(self) -> None:
        site_file(self.root, "xumo.tv", channel("xumo.tv", "a", "A.us@SD", "A"))
        site_file(self.root, "tvtv.us", channel("tvtv.us", "b", "B.us@SD", "B"))
        text = self.assemble(["tvtv.us", "xumo.tv"])
        assert text is not None
        self.assertLess(text.index('site="tvtv.us"'), text.index('site="xumo.tv"'))

    def test_a_site_that_fails_does_not_lose_the_others(self) -> None:
        site_file(self.root, "xumo.tv", channel("xumo.tv", "a", "A.us@SD", "A"))
        text = self.assemble(["xumo.tv", "not-a-real-site.example"])
        assert text is not None
        self.assertIn('site="xumo.tv"', text)
        self.assertEqual(text.count("<channel "), 1)

    def test_every_site_failing_returns_nothing(self) -> None:
        self.assertIsNone(self.assemble(["not-a-real-site.example"]))

    def test_the_shipped_site_list_covers_the_sources_it_claims(self) -> None:
        # The default list is a measurement (see init.py): the sites with real
        # coverage of the US playlist's stream ids. Order matters — it is the
        # order the grabber is offered the sources in.
        self.assertEqual(init_mod.LIVETV_EPG_SITES[:4],
                         ["xumo.tv", "tvtv.us", "tvpassport.com", "tvguide.com"])
        self.assertEqual(len(set(init_mod.LIVETV_EPG_SITES)),
                         len(init_mod.LIVETV_EPG_SITES))


if __name__ == "__main__":
    unittest.main()
