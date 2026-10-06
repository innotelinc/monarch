#!/usr/bin/env python3
"""The alert paths of `scripts/drift-check.sh`, run offline.

An alert is only as good as the sentence it carries, and both of the ones this
check can send used to be the same nine words (`⚠️ Monarch drift check failed on
<host>`). They are told apart now — a re-check after a heal that did not take, and
a run that only looked — but that wording is inside a shell script's `if`, so
nothing in the fast suite would notice it going back or the two rules crossing.

The send is replaceable for exactly this reason (`DRIFT_TELEGRAM_CMD`, the same
shape as `MONARCH_LDAP_PROBE`), and the check's state directory is a variable
(`MONARCH_STATE_DIR`), so a test can be a whole host's state — a manifest at the
state directory's own path, a heal clock, and a verdict file — without touching
one. No stack and no bot are involved: every probed port points at a closed one,
which is a finding on any machine, and the seam writes the subject and body to a
temp file instead of pasting an operator.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "drift-check.sh"

INIT = REPO / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)

# The send, replaced: what the alert would have carried, on disk. Both halves are
# kept because the tests assert on both.
SEAM = 'printf "%s\\n" "$DRIFT_TELEGRAM_SUBJECT" "$DRIFT_TELEGRAM_BODY" >> "$DRIFT_ALERT_FILE"'

# Placeholders, not credentials: the alert branch only has to be entered, and the
# seam means no token is ever used. Shaped as placeholders so a secret scan does
# not have to guess.
FAKE_TOKEN = "<fake>"


def dead_port() -> int:
    """A port nothing listens on, bound once so the OS picks a free one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DriftCheckAlerts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-check-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # The state directory: manifest, heal clock and verdict all live here, and
        # pointing the check at this one is what keeps a test off the host's.
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.alerted = self.tmp / "alerted"
        self.env_file = self.tmp / "check.env"
        self.env_file.write_text("MONARCH_USERNAME=admin\nMONARCH_PASSWORD=monarch8\n")

        # Every probed service on one closed port, so the run finds drift on a
        # machine with no stack, on one with a perfect stack, and in CI. Built from
        # the real manifest so the shape cannot drift from init's.
        closed = dead_port()
        self.manifest = init_mod.build_invariants()
        for app in self.manifest["arr_apps"]:
            app["port"] = closed
        for key in ("prowlarr", "transmission", "jellyfin", "jellyseerr", "bazarr"):
            self.manifest[key]["port"] = closed

    # ── helpers ─────────────────────────────────────────────────────────
    def write_manifest(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.manifest, indent=2))
        return path

    def run_check(self, *args: str, own_manifest: bool = False, env: dict | None = None):
        """Run the check against this test's state directory.

        `own_manifest` puts the manifest where the check's own host would keep it
        (so the run is not staged); otherwise it goes somewhere else, which is what
        a rehearsal and CI do.
        """
        run_env = dict(os.environ)
        run_env.update(
            {
                "MONARCH_ENV": str(self.env_file),
                "MONARCH_STATE_DIR": str(self.state),
                "DRIFT_JELLYFIN_GRACE_SEC": "0",
                "DRIFT_TELEGRAM_CMD": SEAM,
                "DRIFT_ALERT_FILE": str(self.alerted),
                "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
                "TELEGRAM_CHAT_ID": FAKE_TOKEN,
            }
        )
        if own_manifest:
            run_env.pop("MONARCH_INVARIANTS", None)
            self.write_manifest(self.state / "invariants.json")
        else:
            run_env["MONARCH_INVARIANTS"] = str(
                self.write_manifest(self.tmp / "other-host" / "invariants.json")
            )
        run_env.update(env or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=REPO,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=240,
        )

    def alert_text(self) -> str:
        return self.alerted.read_text() if self.alerted.exists() else ""

    def seed_heal_state(self, epoch: int, count: int) -> None:
        (self.state / "drift-heal-last").write_text(f"{epoch} {count}\n")

    def status(self) -> dict:
        text = (self.state / "drift-last").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    # ── the rules ───────────────────────────────────────────────────────
    def test_a_staged_run_reports_and_stays_silent(self):
        res = self.run_check(own_manifest=False)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("DRIFT-FAIL", res.stderr)
        self.assertIn("no alert", res.stdout)
        self.assertEqual(self.alert_text(), "", "a staged run notified the operator")
        # And it records nothing: the verdict is this host's, and a run judging
        # somebody else's manifest would make the file say something untrue.
        self.assertFalse((self.state / "drift-last").exists())

    def test_an_unstaged_failure_says_it_only_checked(self):
        res = self.run_check(own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        text = self.alert_text()
        self.assertIn("Monarch drift check failed on ", text)
        self.assertIn("only checked - nothing was repaired", text)
        self.assertNotIn("AFTER A HEAL", text)

    def test_a_post_heal_failure_says_a_heal_did_not_take(self):
        # The file says 2 because the heal this re-check belongs to counted itself
        # on the way out - the count is attempts, and this run is reading back the
        # attempt that just happened.
        self.seed_heal_state(0, 2)
        res = self.run_check(own_manifest=True, env={"DRIFT_REVERIFY_FROM_HEAL": "1"})
        self.assertEqual(res.returncode, 1, res.stderr)
        text = self.alert_text()
        self.assertIn("Monarch drift check failed on ", text)
        self.assertIn("AFTER A HEAL", text)
        self.assertIn("needs a person", text)
        self.assertIn("heal attempt 2 in a row", text)
        # A post-heal alert must not also tell the reader nothing was repaired.
        self.assertNotIn("nothing was repaired", text)

    def test_the_alert_counts_the_heals_that_have_not_cleared_it(self):
        self.seed_heal_state(0, 3)
        res = self.run_check(own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("survived 3 heal attempt(s) in a row", self.alert_text())

    # ── the verdict, for a reader who is not in the journal ─────────────
    def test_a_run_records_its_verdict_where_status_can_read_it(self):
        res = self.run_check(own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        recorded = self.status()
        self.assertEqual(recorded["verdict"], "drift")
        self.assertEqual(recorded["manifest"], str(self.state / "invariants.json"))
        self.assertGreater(int(recorded["issues"]), 0)
        self.assertIn("finding=", (self.state / "drift-last").read_text())

        shown = self.run_check("--status")
        self.assertEqual(shown.returncode, 1, shown.stderr)
        self.assertIn("verdict=drift", shown.stdout)

    def test_status_follows_the_read_only_rule(self):
        # 0 clean, 1 drift, 2 nothing recorded - the same rule the check itself
        # follows, so `--status` can be used like one.
        (self.state / "drift-last").write_text("verdict=ok\nat=now\nissues=0\n")
        clean = self.run_check("--status")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("verdict=ok", clean.stdout)

        (self.state / "drift-last").unlink()
        missing = self.run_check("--status")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no verdict recorded", missing.stderr)


if __name__ == "__main__":
    unittest.main()
