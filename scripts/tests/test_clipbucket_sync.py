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
    visible once it is old enough;
  * **which rows the delete covers** — the shape of the SQL, not a database. That
    is the half that broke in production, so it is pinned here.

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
from unittest import mock

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


class VanishedTest(unittest.TestCase):
    """Which catalogue rows count as "the source is gone".

    Rows are `file_name -> (videoid, active)`. The judgement decides what gets
    DELETED, so the two ways to get it wrong both matter: keeping a row whose film
    is gone (the bug this script exists for) and deleting one that is merely
    hidden already (`--hide` was used, and a converged host must not re-report it
    as drift forever).
    """

    def test_a_listed_row_with_no_source_is_vanished(self):
        rows = {"gone": (7, True), "here": (8, True)}
        self.assertEqual(cs.vanished_names(rows, {"here"}), ["gone"])

    def test_an_already_hidden_row_is_not_vanished(self):
        rows = {"gone": (7, False)}
        self.assertEqual(cs.vanished_names(rows, set()), [])

    def test_a_row_whose_source_is_back_is_not_vanished(self):
        rows = {"back": (7, True)}
        self.assertEqual(cs.vanished_names(rows, {"back"}), [])

    def test_the_answer_is_sorted_so_a_run_is_reproducible(self):
        rows = {"b": (2, True), "a": (1, True)}
        self.assertEqual(cs.vanished_names(rows, set()), ["a", "b"])


class RemoveItemsTest(unittest.TestCase):
    """The delete a vanished item gets: completeness, order, and one transaction.

    None of this is visible by running the tool against the real schema, which is
    why it is pinned here: the first version deleted `cb_video`, its images and
    its thumbs, and looked right. It was not — `cb_videos_categories` also has a
    RESTRICT foreign key onto `cb_video.videoid`, so MySQL aborted the statement
    after `cb_video_image` had already gone and left the item half-deleted (row
    present, images gone, media file still on disk). Completeness and order are
    exactly the two properties a reader cannot check by eye against a schema they
    cannot see from the call site.

    `mysql_exec` is stubbed so the SQL is inspected rather than executed; nothing
    else about the delete is faked.
    """

    def setUp(self):
        self.calls: list[str] = []
        patcher = mock.patch.object(cs.cl, "mysql_exec", lambda container, sql: self.calls.append(sql))
        patcher.start()
        self.addCleanup(patcher.stop)
        files = mock.patch.object(cs, "remove_files", lambda facts, name: 0)
        files.start()
        self.addCleanup(files.stop)

    def sql(self, named: dict[str, int] | None = None) -> str:
        cs.remove_items("clipbucket", {}, named or {"gone": 7})
        self.assertEqual(len(self.calls), 1, "one call, so one connection and one transaction")
        return self.calls[0]

    def test_every_child_table_of_cb_video_is_covered(self):
        # Missing one is not a tidiness problem: the constraint aborts the delete.
        sql = self.sql()
        for table, column in cs.VIDEO_CHILD_TABLES:
            self.assertIn(f"DELETE FROM {table} WHERE {column} IN (7)", sql)
        self.assertIn(
            "DELETE FROM cb_collection_items WHERE type='videos' AND object_id IN (7)", sql
        )
        self.assertIn("DELETE FROM cb_video WHERE videoid IN (7)", sql)

    def test_thumbs_go_before_the_image_rows_they_hang_off(self):
        sql = self.sql()
        self.assertLess(
            sql.index("DELETE t FROM cb_video_thumb t"),
            sql.index("DELETE FROM cb_video_image "),
            "thumbs key off cb_video_image.id_video_image, so deleting images first orphans them",
        )

    def test_it_is_one_transaction_so_a_surprise_rolls_back(self):
        # A schema change that adds a constraint this list does not know about must
        # leave the item whole (a state the next run can judge), not half-deleted.
        sql = self.sql()
        self.assertTrue(sql.startswith("START TRANSACTION;"), sql)
        self.assertTrue(sql.endswith("COMMIT;"), sql)

    def test_every_id_goes_in_the_same_pass(self):
        self.assertIn("IN (3, 9)", self.sql({"a": 3, "b": 9}))

    def test_the_files_are_removed_for_each_item_and_counted(self):
        with mock.patch.object(cs, "remove_files", lambda facts, name: 2):
            self.assertEqual(cs.remove_items("clipbucket", {}, {"a": 3, "b": 9}), 4)


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
