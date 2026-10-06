#!/usr/bin/env python3
"""Tests that `monarch-init` carries the drift check's verdict into status.json.

The check runs on a timer four times a day and keeps its own state in
`drift-last`; init runs once and writes `status.json`, which is the file an
operator reads after an install (`sudo cat /docker/appdata/init/status.json`).
Folding one into the other means that one file answers "what did init do?" and
"is the stack drifted right now?" - and it has to be done by *reading* the
check's file, not by judging the stack here: "init says everything is configured"
is not the same claim as "the live stack is healthy", and only the check probes
the services.

The parse is deliberately forgiving: a verdict file is written by a shell script,
so a line it does not recognise is skipped rather than fatal, and a file that is
not there yet is reported as not recorded - which is the truth on a host whose
timer has not run since it was installed.

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

VERDICT = """\
verdict=drift
at=2026-10-06T18:02:31-04:00
epoch=1791308073
host=monarch
manifest=/docker/appdata/init/invariants.json
issues=2
heal_streak=3
heal=recheck
finding=a heal reconciled the stack and re-ran monarch-init, and this re-check still finds drift
finding=jellyfin: libraries missing: 'Movies'
"""


class DriftVerdict(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-verdict-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "drift-last"

    def test_a_recorded_verdict_is_parsed(self):
        self.path.write_text(VERDICT)
        verdict = init_mod.read_drift_verdict(str(self.path))
        self.assertTrue(verdict["recorded"])
        self.assertEqual(verdict["verdict"], "drift")
        self.assertEqual(verdict["at"], "2026-10-06T18:02:31-04:00")
        self.assertEqual(verdict["host"], "monarch")
        self.assertEqual(verdict["manifest"], "/docker/appdata/init/invariants.json")
        self.assertEqual(verdict["issues"], 2)
        self.assertEqual(verdict["heal_streak"], 3)
        self.assertEqual(verdict["heal"], "recheck")
        self.assertEqual(
            verdict["findings"],
            [
                "a heal reconciled the stack and re-ran monarch-init, and this re-check still finds drift",
                "jellyfin: libraries missing: 'Movies'",
            ],
        )

    def test_a_host_that_has_not_run_the_check_is_not_recorded(self):
        verdict = init_mod.read_drift_verdict(str(self.path))
        self.assertFalse(verdict["recorded"])
        self.assertIn(str(self.path), verdict["note"])

    def test_a_clean_verdict_has_no_findings(self):
        self.path.write_text("verdict=ok\nat=now\nissues=0\nheal_streak=0\nheal=none\n")
        verdict = init_mod.read_drift_verdict(str(self.path))
        self.assertEqual(verdict["verdict"], "ok")
        self.assertEqual(verdict["findings"], [])

    def test_lines_it_does_not_know_are_skipped(self):
        # The file is written by a shell script that may grow a field before this
        # reader does; an unparsed line must not take the verdict down with it, and
        # a count that is not a number is not a count.
        self.path.write_text("verdict=drift\nnotes from a newer check\n=bad\nextra=1\nissues=oops\n")
        verdict = init_mod.read_drift_verdict(str(self.path))
        self.assertEqual(verdict["verdict"], "drift")
        self.assertNotIn("extra", verdict)
        self.assertNotIn("issues", verdict)


if __name__ == "__main__":
    unittest.main()
