#!/usr/bin/env python3
"""What `scripts/drift-status.py` says, and what it exits with.

The verdict, the heal clock and streak, and the findings the last standing-down
alert carried are three files written by a shell script. `--status` prints the
first verbatim, which is right for a script and wrong for a person; this tool is
the one place all three are read together, so its reading has to be pinned - in
particular the two rules that decide between exit 1 and exit 2: a verdict that was
never recorded, and a verdict too old to judge by (the point of the second is that
a stopped timer is a finding, and a check that reports somebody else's fortnight-old
verdict as current would hide exactly that).

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "drift-status.py"

spec = importlib.util.spec_from_file_location("drift_status", SCRIPT)
assert spec and spec.loader
drift_status = importlib.util.module_from_spec(spec)
sys.modules["drift_status"] = drift_status
spec.loader.exec_module(drift_status)


def verdict_text(verdict: str, *, at: str = "2026-10-07T06:00:03+00:00", epoch: int | None = None,
                 issues: int = 0, heal: str = "none", findings: tuple[str, ...] = ()) -> str:
    epoch = int(time.time()) - 600 if epoch is None else epoch
    lines = [
        f"verdict={verdict}",
        f"at={at}",
        f"epoch={epoch}",
        "host=monarch",
        "manifest=/docker/appdata/init/invariants.json",
        f"issues={issues}",
        "heal_streak=0",
        f"heal={heal}",
    ]
    lines += [f"finding={finding}" for finding in findings]
    return "\n".join(lines) + "\n"


class DriftStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-status-")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    # ── helpers ─────────────────────────────────────────────────────────
    def write_verdict(self, text: str) -> None:
        (self.state / "drift-last").write_text(text)

    def write_heal_state(self, text: str) -> None:
        (self.state / "drift-heal-last").write_text(text)

    def _write_state(self, name: str, text: str) -> Path:
        path = self.state / name
        path.write_text(text)
        return path

    def run_status(self, *args: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--state-dir", str(self.state), *args],
            capture_output=True, text=True, timeout=60,
        )

    # ── the rules ───────────────────────────────────────────────────────
    def test_nothing_recorded_is_cannot_judge(self):
        res = self.run_status()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("cannot judge", res.stdout)
        self.assertIn("no verdict recorded", res.stderr)
        # The next step, because "cannot judge" is where the operator has to be
        # pointed at the timer rather than at the stack.
        self.assertIn("monarch-drift-check.timer", res.stdout)

    def test_a_clean_verdict_exits_zero(self):
        self.write_verdict(verdict_text("ok"))
        res = self.run_status()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ok - the stack matches the invariants", res.stdout)

    def test_drift_lists_the_findings_and_exits_one(self):
        self.write_verdict(verdict_text(
            "drift", issues=2, heal="recheck",
            findings=("jellyfin library Movies is missing", "sonarr root folder is missing"),
        ))
        res = self.run_status()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("DRIFT - 2 finding(s)", res.stdout)
        self.assertIn("- jellyfin library Movies is missing", res.stdout)
        self.assertIn("- sonarr root folder is missing", res.stdout)
        # What the last run DID about them is the part that decides whether waiting
        # is a plan, so it is spelled out rather than left as `heal=recheck`.
        self.assertIn("the repair did not take", res.stdout)

    def test_a_verdict_older_than_the_cadence_is_stale(self):
        # Six hours between runs, so a verdict from yesterday is not a reading of
        # the stack - and a stopped timer must not read as a clean log.
        self.write_verdict(verdict_text("ok", epoch=int(time.time()) - 40 * 3600))
        res = self.run_status()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("STALE", res.stdout)
        self.assertIn("the timer is not keeping up", res.stderr)

        # ... and the threshold is the operator's to set, for a host whose timer is
        # deliberately slower than this one's.
        wide = self.run_status("--stale-after", str(48 * 3600))
        self.assertEqual(wide.returncode, 0, wide.stdout + wide.stderr)

    def test_a_verdict_file_that_cannot_be_read_is_cannot_judge(self):
        self.write_verdict("this is not a verdict\n")
        res = self.run_status()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("does not carry a verdict", res.stderr)

    def test_standing_down_is_named_with_the_way_out(self):
        self.write_verdict(verdict_text("drift", issues=1, heal="stood_down",
                                        findings=("prowlarr is not answering",)))
        self.write_heal_state(f"{int(time.time()) - 7200} 5 {int(time.time()) - 3600}\n")
        (self.state / "drift-standdown-alert").write_text("prowlarr is not answering\n")
        res = self.run_status()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("STOOD DOWN", res.stdout)
        self.assertIn("--reset-streak", res.stdout)
        self.assertIn("5 attempt(s) in a row", res.stdout)
        self.assertIn("last standdown alert  1h ago, carrying 1 finding(s)", res.stdout)

    def test_the_heal_state_is_read_forgivingly(self):
        # The file predates two of its three fields; a one-field file is a hint
        # (the clock) and not a streak, and reading it must not fail the report.
        self.assertEqual(
            drift_status.read_heal_state(self._write_state("drift-heal-last", "1791302829\n")),
            {"attempt_epoch": 1791302829, "count": 0, "standdown_alert_epoch": None},
        )
        self.assertEqual(
            drift_status.read_heal_state(self._write_state("drift-heal-last", "1791302829 4 1791300000\n")),
            {"attempt_epoch": 1791302829, "count": 4, "standdown_alert_epoch": 1791300000},
        )
        # A field that is not a number reads as 0 rather than as an error: this is
        # bookkeeping, and the verdict is the finding.
        self.assertEqual(
            drift_status.read_heal_state(self._write_state("drift-heal-last", "1791302829 oops 17\n")),
            {"attempt_epoch": 1791302829, "count": 0, "standdown_alert_epoch": 17},
        )
        self.assertEqual(
            drift_status.read_heal_state(self.state / "not-there"),
            {"attempt_epoch": None, "count": 0, "standdown_alert_epoch": None},
        )

    def test_json_carries_the_same_facts(self):
        self.write_verdict(verdict_text("drift", issues=1, heal="suppressed",
                                        findings=("bazarr is not answering",)))
        self.write_heal_state(f"{int(time.time()) - 600} 3 0\n")
        (self.state / "drift-standdown-alert").write_text("bazarr is not answering\n")
        res = self.run_status("--json")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["status"], "drift")
        self.assertEqual(payload["verdict"], "drift")
        self.assertEqual(payload["issues"], 1)
        self.assertEqual(payload["findings"], ["bazarr is not answering"])
        self.assertEqual(payload["heal"], "suppressed")
        self.assertEqual(payload["heal_streak"], 3)
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["standdown_alert_findings"], ["bazarr is not answering"])
        self.assertEqual(payload["state_dir"], str(self.state))

        # Nothing recorded is still a readable JSON document - a dashboard asking
        # about a host that has never run the check gets an answer, not a traceback.
        self.write_verdict("")
        (self.state / "drift-last").unlink()
        empty = self.run_status("--json")
        self.assertEqual(empty.returncode, 2, empty.stdout + empty.stderr)
        absent = json.loads(empty.stdout)
        self.assertFalse(absent["recorded"])
        self.assertEqual(absent["status"], "cannot-judge")
        self.assertEqual(absent["findings"], [])

    def test_the_json_uses_the_state_directory_from_the_environment(self):
        # MONARCH_STATE_DIR is what the check itself reads, so a caller that already
        # sets it (the tests, a rehearsal, CI) does not have to pass it twice.
        self.write_verdict(verdict_text("ok"))
        res = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "MONARCH_STATE_DIR": str(self.state)},
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ok - the stack matches the invariants", res.stdout)


if __name__ == "__main__":
    unittest.main()
