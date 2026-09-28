#!/usr/bin/env python3
"""Unit tests for scripts/clipbucket-sync.py.

The script exists so a Jellyfin add or delete reaches tube.innotel.us without a
person running a command, and its two load-bearing judgements are pure functions
over a directory tree, so they are tested without a container, a database or a
library:

  * **the fingerprint** decides whether the (expensive, ffprobe-per-item) import
    runs at all. It must change when the library changes and NOT change when it
    does not — a fingerprint that moved on its own would re-import every two
    minutes forever;
  * **the settle window** is what keeps a half-written film out of the import, so
    a file touched inside it must be invisible to the fingerprint *and* become
    visible once it is old enough.

The database half (`catalogue_rows`, `set_active`) is deliberately not faked: it
is `clipbucket-library.py`'s own plumbing, tested there, and a second mock of it
would only prove the mock.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(os.path.join(HERE, "..", "clipbucket-sync.py"))
_spec = importlib.util.spec_from_file_location("clipbucket_sync", SCRIPT)
cs = importlib.util.module_from_spec(_spec)
sys.modules["clipbucket_sync"] = cs
_spec.loader.exec_module(cs)

MOVIES = "movies"
TV = "tv"


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cbsync-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, MOVIES))
        os.makedirs(os.path.join(self.root, TV))

    def write(self, rel: str, body: str, age: float = 0.0) -> str:
        """A file under the root, backdated by `age` seconds (0 = written now)."""
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def fp(self, settle: int = 0) -> str:
        return cs.fingerprint(self.root, MOVIES, TV, settle, time.time())

    def test_a_settled_file_changes_the_fingerprint(self):
        before = self.fp()
        self.write("movies/a-2026-1.mp4", "a", age=600)
        self.assertNotEqual(before, self.fp(), "an added film must move the fingerprint")

    def test_deleting_a_file_changes_the_fingerprint(self):
        path = self.write("movies/a-2026-1.mp4", "a", age=600)
        before = self.fp()
        os.remove(path)
        self.assertNotEqual(before, self.fp(), "a deleted film must move the fingerprint")

    def test_size_and_mtime_both_count(self):
        path = self.write("tv/show-s01e01-1.mp4", "aa", age=600)
        by_size = self.fp()
        self.write("tv/show-s01e01-1.mp4", "aaaa", age=600)
        self.assertNotEqual(by_size, self.fp(), "a replaced source must move the fingerprint")
        by_mtime = self.fp()
        stamp = time.time() - 900
        os.utime(path, (stamp, stamp))
        self.assertNotEqual(by_mtime, self.fp(), "a re-imported source must move the fingerprint")

    def test_the_fingerprint_is_stable_when_nothing_moves(self):
        self.write("movies/a-2026-1.mp4", "a", age=600)
        self.assertEqual(self.fp(), self.fp(), "an unchanged library must not look changed")

    def test_a_file_inside_the_settle_window_is_invisible(self):
        # The `*arr` case: the file is being written now. Importing it would
        # hardlink a half-copied film, so it must not count yet.
        unsettled = self.fp(settle=600)
        self.write("movies/half-copied-2026-9.mp4", "partial", age=5)
        self.assertEqual(unsettled, self.fp(settle=600), "a file being written must not count")

    def test_it_becomes_visible_once_it_settles(self):
        self.write("movies/half-copied-2026-9.mp4", "whole", age=5)
        self.assertEqual(self.fp(settle=600), self.fp(settle=600))
        # Make it older than the window: now it is a film, not a transfer.
        path = os.path.join(self.root, "movies", "half-copied-2026-9.mp4")
        stamp = time.time() - 900
        os.utime(path, (stamp, stamp))
        self.assertNotEqual(
            cs.fingerprint(self.root, MOVIES, TV, 600, time.time() - 880),
            cs.fingerprint(self.root, MOVIES, TV, 600, time.time()),
            "once old enough the file must be counted",
        )

    def test_both_halves_of_the_library_are_covered(self):
        before = self.fp()
        self.write("tv/show-s01e02-2.mp4", "ep", age=600)
        self.assertNotEqual(before, self.fp(), "an episode must count, not just a film")

    def test_an_absent_library_is_an_empty_fingerprint_not_an_error(self):
        # Missing root, missing halves: the caller refuses to write when the
        # library is absent, so this must not raise on the way to that decision.
        absent = os.path.join(self.root, "nope")
        self.assertEqual(
            cs.fingerprint(absent, MOVIES, TV, 0, time.time()),
            cs.fingerprint(absent, MOVIES, TV, 0, time.time()),
        )


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cbsync-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "nested", "clipbucket-sync.fingerprint")

    def test_round_trip(self):
        cs.write_state(self.path, "abc123")
        self.assertEqual(cs.read_state(self.path), "abc123")

    def test_a_missing_state_file_reads_empty(self):
        # The first run on a host has no state: it must look like "changed" so the
        # import runs, not like a crash.
        self.assertEqual(cs.read_state(self.path), "")


if __name__ == "__main__":
    unittest.main()
