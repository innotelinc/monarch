#!/usr/bin/env python3
"""Tests for scripts/media-retention.py.

What matters is the decision, not the deleting: a film past the age limit goes
with its poster and subtitles, a series keeps the newest episodes and nothing
else, an old episode is not saved by being recent in the listing, and a dry run
touches nothing at all. The tree is built in a temp directory, so no library is
needed.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "media-retention.py"


def load_script():
    spec = importlib.util.spec_from_file_location("media_retention", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["media_retention"] = module
    spec.loader.exec_module(module)
    return module


retention = load_script()


class LibraryTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movies = self.root / "movies"
        self.tv = self.root / "tv"
        self.movies.mkdir()
        self.tv.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, path: Path, *, age_days: float, body: str = "x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def run_script(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = retention.main(["--root", str(self.root), *extra])
        return code, out.getvalue(), err.getvalue()


class MovieTests(LibraryTree):
    def test_film_past_the_limit_is_removed_with_its_sidecars(self) -> None:
        folder = self.movies / "Old Film (1999)"
        video = self.write(folder / "Old Film (1999).mkv", age_days=9)
        poster = self.write(folder / "Old Film (1999).jpg", age_days=9)
        subs = self.write(folder / "Old Film (1999).en.srt", age_days=9)

        code, out, _ = self.run_script("--apply")

        self.assertEqual(code, 0)
        self.assertFalse(video.exists())
        self.assertFalse(poster.exists())
        self.assertFalse(subs.exists())
        self.assertFalse(folder.exists())
        self.assertIn("removed 3 file(s)", out)

    def test_recent_film_stays(self) -> None:
        video = self.write(self.movies / "New Film (2026)" / "New Film (2026).mkv", age_days=2)

        code, _, _ = self.run_script("--apply")

        self.assertEqual(code, 0)
        self.assertTrue(video.exists())

    def test_a_dry_run_removes_nothing_but_names_the_files(self) -> None:
        video = self.write(self.movies / "Old Film (1999)" / "Old Film (1999).mkv", age_days=30)

        code, out, _ = self.run_script()

        self.assertEqual(code, 0)
        self.assertTrue(video.exists())
        self.assertIn("would remove", out)
        self.assertIn("pass --apply", out)

    def test_a_title_folder_survives_when_one_file_is_current(self) -> None:
        old = self.write(self.movies / "Remux (2001)" / "extra.mkv", age_days=40)
        new = self.write(self.movies / "Remux (2001)" / "Remux (2001).mkv", age_days=1)

        self.run_script("--apply")

        self.assertTrue(old.exists())
        self.assertTrue(new.exists())


class ShowTests(LibraryTree):
    def series(self, name: str, count: int, *, age_days: float = 1.0, start: int = 1) -> list[Path]:
        made = []
        for number in range(start, start + count):
            made.append(self.write(
                self.tv / name / "Season 01" / f"{name} - S01E{number:02d} - Episode {number}.mkv",
                age_days=age_days,
            ))
        return made

    def test_a_series_keeps_only_its_newest_three_episodes(self) -> None:
        episodes = self.series("Monarch", 6)

        code, out, _ = self.run_script("--apply")

        self.assertEqual(code, 0)
        self.assertEqual([p.exists() for p in episodes], [False, False, False, True, True, True])
        self.assertIn("kept 3", out)

    def test_episode_order_comes_from_the_name_not_the_listing(self) -> None:
        episodes = self.series("Wings", 5)
        # Make the *first* episode the newest file on disk: the order has to come
        # from the episode number in the name, not from the listing.
        now = time.time()
        for index, path in enumerate(episodes):
            stamp = now - (index + 1) * 3600
            os.utime(path, (stamp, stamp))

        self.run_script("--apply")

        self.assertEqual([p.exists() for p in episodes], [False, False, True, True, True])

    def test_an_old_episode_goes_even_when_it_is_among_the_newest(self) -> None:
        episodes = self.series("Archive", 2, age_days=30)

        self.run_script("--apply")

        self.assertFalse(any(p.exists() for p in episodes))
        self.assertFalse((self.tv / "Archive").exists())

    def test_a_short_series_keeps_every_recent_episode(self) -> None:
        episodes = self.series("Fresh", 2)

        self.run_script("--apply")

        self.assertTrue(all(p.exists() for p in episodes))

    def test_keep_latest_zero_keeps_none(self) -> None:
        episodes = self.series("Cleared", 2)

        self.run_script("--keep-latest", "0", "--apply")

        self.assertFalse(any(p.exists() for p in episodes))

    def test_files_that_are_not_media_are_left_alone(self) -> None:
        keep = self.series("Docs", 1)[0]
        note = self.write(self.tv / "Docs" / "Season 01" / "notes.txt", age_days=400, body="hand-written")

        self.run_script("--apply")

        self.assertTrue(note.exists())
        self.assertTrue(keep.exists())


class LibraryGuardTests(LibraryTree):
    def test_a_missing_library_is_reported_rather_than_treated_as_empty(self) -> None:
        empty = Path(self.tmp.name) / "nowhere"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = retention.main(["--root", str(empty), "--apply"])

        self.assertEqual(code, 2)
        self.assertIn("no library", err.getvalue())

    def test_negative_limits_are_refused(self) -> None:
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                retention.main(["--root", str(self.root), "--max-age-days", "-1"])


if __name__ == "__main__":
    unittest.main()
