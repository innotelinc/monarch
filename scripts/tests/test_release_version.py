#!/usr/bin/env python3
"""Unit tests for release-version.py — how the next release version is derived.

The case this exists for is not hypothetical: from 2026-09-14 to 2026-09-21
every scheduled release run failed with

    release with the same tag name already exists: v1.23

because the version was read off `gh release list --limit 1` (which sorts by
createdAt, so the draft v1.22 outranked the published v1.23) and then bumped by
one. Each run recomputed the same v1.23, so the failure could never clear. The
tests below are written in that order of importance:

  * the derivation ignores the order it is handed the versions in, and takes the
    highest — the regression itself, asserted with the live input;
  * the inputs that are not versions are ignored rather than becoming a base;
  * a draft's tag still counts as taken;
  * "no releases yet" (v1.0.0) is not the same as "sources unreadable" (refuse).

The module under test has a hyphen in its filename, so it is loaded by path.

Run:  python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""
from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "release-version.py"


def load_module():
    spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rv = load_module()

# The live repository state on 2026-09-21, in the order `gh release list`
# returns it: the draft v1.22 first, because its record was created a day later
# than v1.23's.
LIVE_RELEASES = ["v1.22", "v1.23", "v1.21", "v1.20", "v1.19"]
LIVE_TAGS = ["v1.19", "v1.20", "v1.21", "v1.22", "v1.23"]


class ParseVersionTest(unittest.TestCase):
    def test_accepts_the_forms_a_tag_may_take(self):
        self.assertEqual(rv.parse_version("v1.23"), (1, 23, 0))
        self.assertEqual(rv.parse_version("v1.23.4"), (1, 23, 4))
        self.assertEqual(rv.parse_version("v2"), (2, 0, 0))
        self.assertEqual(rv.parse_version("v1.0.0"), (1, 0, 0))

    def test_rejects_what_is_not_a_release_version(self):
        for stray in ("", "nightly", "1.23", "backup-2026-09", "v1.2.3-rc1", "v1.2.3+build"):
            with self.subTest(stray=stray):
                self.assertIsNone(rv.parse_version(stray))

    def test_whitespace_does_not_defeat_it(self):
        self.assertEqual(rv.parse_version("  v1.5\n"), (1, 5, 0))


class HighestVersionTest(unittest.TestCase):
    def test_the_live_regression_order_does_not_decide_the_answer(self):
        """v1.22 arrives first; v1.23 is still the highest.

        This is the bug: reading the first element (or the newest record)
        returned v1.22 and the next release was computed as the one that already
        existed.
        """
        self.assertEqual(rv.highest_version(LIVE_RELEASES), "v1.23")
        self.assertEqual(rv.highest_version(list(reversed(LIVE_RELEASES))), "v1.23")

    def test_ten_outranks_nine(self):
        # A string sort would answer v1.9 here, and cut over v1.2.
        self.assertEqual(rv.highest_version(["v1.9", "v1.10", "v1.2"]), "v1.10")

    def test_a_patch_outranks_its_minor(self):
        self.assertEqual(rv.highest_version(["v1.20", "v1.20.1"]), "v1.20.1")

    def test_only_strays_is_nothing(self):
        self.assertIsNone(rv.highest_version(["nightly", "backup-2026-09"]))

    def test_empty_is_nothing(self):
        self.assertIsNone(rv.highest_version([]))


class NextVersionTest(unittest.TestCase):
    def test_the_live_case_cuts_v1_24_not_the_existing_v1_23(self):
        versions = rv.known_versions(LIVE_TAGS, LIVE_RELEASES)
        self.assertEqual(rv.next_version(versions, "minor"), "v1.24")

    def test_a_fresh_repository_starts_at_v1_0_0(self):
        self.assertEqual(rv.next_version([], "minor"), "v1.0.0")

    def test_major_bump_resets_both_lower_components(self):
        self.assertEqual(rv.next_version(["v1.23.4"], "major"), "v2.0.0")

    def test_the_house_style_is_kept(self):
        """This repository tags v1.23, so the next release is v1.24.

        Emitting v1.24.0 would be a version unlike every predecessor, in every
        script that matches on one.
        """
        self.assertEqual(rv.next_version(LIVE_TAGS, "minor"), "v1.24")
        self.assertEqual(rv.next_version(LIVE_TAGS, "major"), "v2.0")
        # …and the three-component style is kept where it is already in use.
        self.assertEqual(rv.next_version(["v1.20.1"], "minor"), "v1.21.0")

    def test_minor_bump_resets_the_patch(self):
        self.assertEqual(rv.next_version(["v1.20.7"], "minor"), "v1.21.0")

    def test_a_draft_of_the_next_version_is_skipped(self):
        """A draft holds its tag, so the next cut has to move past it."""
        versions = rv.known_versions(["v1.23"], ["v1.24"])
        self.assertEqual(rv.next_version(versions, "minor"), "v1.25")

    def test_strays_do_not_become_the_base(self):
        versions = rv.known_versions(["nightly", "v1.23", "backup-2026-09"], [])
        self.assertEqual(rv.next_version(versions, "minor"), "v1.24")

    def test_an_unknown_bump_is_refused(self):
        with self.assertRaises(ValueError):
            rv.next_version(["v1.23"], "patch")


class KnownVersionsTest(unittest.TestCase):
    def test_the_sources_are_merged_without_duplicating(self):
        self.assertEqual(
            sorted(rv.known_versions(["v1.23", "v1.22"], ["v1.22", "v1.23"])),
            ["v1.22", "v1.23"],
        )

    def test_blanks_are_not_versions(self):
        self.assertEqual(rv.known_versions([""], ["  ", "v1.1"]), ["v1.1"])

    def test_the_ignored_set_is_reported_not_dropped(self):
        self.assertEqual(
            rv.ignored(["nightly", "v1.2", "backup-2026-09", "v1.2"]),
            ["backup-2026-09", "nightly"],
        )


class ReleaseTagTest(unittest.TestCase):
    def test_a_valid_tag_is_taken_as_the_version(self):
        # Verbatim, not re-formatted: the run is for the tag that was pushed.
        self.assertEqual(rv.release_tag("v1.23"), "v1.23")
        self.assertEqual(rv.release_tag("v1.23.4"), "v1.23.4")

    def test_an_unreleasable_tag_is_refused(self):
        # A tag that is not a version would otherwise become one on the release
        # it cuts, which is how a `nightly` tag turns into a version.
        with self.assertRaises(ValueError):
            rv.release_tag("nightly")


class MainTest(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = rv.main(["release-version.py"] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_live_repository_state_prints_the_next_version(self):
        rc, out, err = self._run(
            ["--release-tags", ",".join(LIVE_RELEASES), "--git-tags", ",".join(LIVE_TAGS)]
        )
        self.assertEqual(rc, 0)
        # stdout is the version and nothing else, so a caller can consume it.
        self.assertEqual(out, "v1.24\n")
        self.assertIn("highest of", err)

    def test_a_tagged_run_uses_the_tag_verbatim(self):
        rc, out, _err = self._run(["--tag", "v1.23"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "v1.23\n")

    def test_a_bad_tag_is_a_failure_not_a_version(self):
        rc, out, err = self._run(["--tag", "nightly"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("not a release version", err)

    def test_an_empty_repository_starts_at_v1_0_0(self):
        rc, out, err = self._run(["--git-tags", "", "--release-tags", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "v1.0.0\n")
        self.assertIn("no prior release", err)

    def test_strays_are_named_on_stderr_and_ignored(self):
        rc, out, err = self._run(["--git-tags", "nightly,v1.23", "--release-tags", "v1.23"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "v1.24\n")
        self.assertIn("'nightly'", err)

    def test_major_bump_from_the_cli(self):
        rc, out, _err = self._run(["--bump", "major", "--git-tags", "v1.23", "--release-tags", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "v2.0\n")


if __name__ == "__main__":
    unittest.main()
