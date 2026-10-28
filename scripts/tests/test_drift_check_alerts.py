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
import time
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
        # A Jellyfin token the check will accept as already exported. Without one the
        # library probe is never reached (the login fails first and the check says
        # so), and the tests below are about what happens when the probe IS reached
        # and nothing answers. Placeholder-shaped: it is never sent anywhere.
        self.jf_key = self.tmp / "jellyfin-api-key.txt"
        self.jf_key.write_text("<fake>\n")
        self.env_file = self.tmp / "check.env"
        self.env_file.write_text("MONARCH_USERNAME=admin\nMONARCH_PASSWORD=monarch8\n")

        # A docker that logs and fails, first on PATH. Not an accident: a test must
        # never be able to touch the machine's stack, and the standing-down tests
        # assert that the heal did not even ask docker for anything.
        self.shim = self.tmp / "bin"
        self.shim.mkdir()
        self.docker_log = self.tmp / "docker-called"
        fake_docker = self.shim / "docker"
        fake_docker.write_text(
            '#!/bin/sh\n'
            f'echo "docker $*" >> "{self.docker_log}"\n'
            # `SHIM_JELLYFIN_STARTED` is how a test says docker CAN answer when the
            # Jellyfin container started: the check asks for exactly this on the
            # nothing-listening path, to tell a boot in progress from a server that
            # is not coming back. Unset - the default, and what CI does - means
            # docker answers nothing at all, and the check falls back to judging the
            # read on its own.
            'if [ -n "${SHIM_JELLYFIN_STARTED:-}" ]; then\n'
            '  case "$*" in\n'
            '    *"{{.State.StartedAt}}"*jellyfin*) printf \'%s\\n\' "$SHIM_JELLYFIN_STARTED"; exit 0 ;;\n'
            '  esac\n'
            'fi\n'
            'exit 1\n'
        )
        fake_docker.chmod(0o755)

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
                "PATH": f"{self.shim}:{os.environ.get('PATH', '')}",
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

    def seed_heal_state(self, epoch: int, count: int, standdown_alerted: int = 0) -> None:
        (self.state / "drift-heal-last").write_text(f"{epoch} {count} {standdown_alerted}\n")

    def heal_state_fields(self) -> list[str]:
        return (self.state / "drift-heal-last").read_text().split()

    def stand_down_once(self) -> Path:
        """Put the heal in the standing-down state and let its first alert out.

        Returns the record of what that alert said.
        """
        self.seed_heal_state(0, 5)
        first = self.run_check("--heal", own_manifest=True)
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertIn("has stood down", self.alert_text())
        record = self.state / "drift-standdown-alert"
        self.assertTrue(record.exists(), "the standing-down alert left no record of what it said")
        self.alerted.unlink()
        return record

    def docker_calls(self) -> str:
        return self.docker_log.read_text() if self.docker_log.exists() else ""

    def status(self) -> dict:
        text = (self.state / "drift-last").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    # ── a Jellyfin that is not there at all ─────────────────────────────
    def test_nothing_listening_is_a_finding_without_shell_noise(self):
        # curl writes the body file only when it reads something, so a refused
        # connection leaves the redirect into python3 pointing at a file that was
        # never created - and bash prints that on stderr, under a finding that
        # already says what is wrong (measured on monarch 2026-10-07).
        # DRIFT_JELLYFIN_BOOT_SEC=0 is the old behaviour: judge the read, ask docker
        # nothing.
        res = self.run_check(
            own_manifest=True,
            env={"DRIFT_JELLYFIN_BOOT_SEC": "0", "JELLYFIN_KEY_FILE": str(self.jf_key)},
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        # The bug's own shape: the redirect into python3 named a file that was not
        # there, and bash announced that under a finding that already said what was
        # wrong. (Not "No such file or directory" in the whole stream: the *arr
        # scripts say that about their own absent config files on a machine with no
        # stack, which is a finding, not noise.)
        self.assertNotIn("drift-libs", res.stderr)
        self.assertNotRegex(res.stderr, r"drift-check\.sh: line \d+: ")
        self.assertIn("answered HTTP 000", (self.state / "drift-last").read_text())
        self.assertNotIn("waiting for it to come up", res.stdout)

    def test_a_container_that_just_started_is_waited_for(self):
        # A recreate - watchtower pulling a new image - takes the port away for
        # minutes while the container starts, and a run that lands in that window
        # used to report the libraries unreadable, which is a finding about an image
        # update. The container's own start time is what tells the two apart.
        started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        res = self.run_check(
            own_manifest=True,
            env={
                "SHIM_JELLYFIN_STARTED": started,
                "DRIFT_JELLYFIN_BOOT_SEC": "9",
                "JELLYFIN_KEY_FILE": str(self.jf_key),
            },
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        # `say`, not `fail`: the note goes to stdout, the finding to stderr.
        self.assertIn("nothing is listening on jellyfin's port and the container started", res.stdout)
        # It waited that budget out instead of calling it drift, and says how long
        # the container has been starting when the budget was not enough.
        self.assertIn("it is still starting", self.alert_text())
        self.assertIn("longer than a boot", self.alert_text())

    def test_a_container_that_has_been_up_a_while_is_not_a_boot(self):
        started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600))
        began = time.time()
        res = self.run_check(
            own_manifest=True,
            env={"SHIM_JELLYFIN_STARTED": started, "JELLYFIN_KEY_FILE": str(self.jf_key)},
        )
        elapsed = time.time() - began
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertNotIn("waiting for it to come up", res.stdout)
        self.assertIn("the container has been up", self.alert_text())
        self.assertLess(elapsed, 60, "a container that has been up an hour is not waited for")

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

    # ── the heal that stops trying ──────────────────────────────────────
    def test_the_heal_stands_down_after_its_streak(self):
        # Five attempts in a row did not clear it, so the sixth run must not try
        # again: repeating a repair that has failed five times is not a plan. The
        # docker shim is the proof - the heal never asked it for anything.
        self.seed_heal_state(0, 5)
        res = self.run_check("--heal", own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("heal standing down", res.stderr)
        self.assertNotIn("reconciling the stack", res.stderr)
        self.assertNotIn("compose", self.docker_calls())
        self.assertIn("has stood down", self.alert_text())
        self.assertNotIn("only checked", self.alert_text())

    def test_a_standing_down_heal_does_not_alert_again_the_next_tick(self):
        # A stack already known to be in this state does not need to be told again
        # every six hours - but the run has to say why it stayed silent, or a quiet
        # timer looks like a timer that found nothing.
        self.seed_heal_state(0, 5, standdown_alerted=int(time.time()))
        res = self.run_check("--heal", "--quiet", own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("no alert - the heal stood down", res.stderr)
        self.assertEqual(self.alert_text(), "")

    def test_a_standing_down_repeat_says_what_changed(self):
        # The interval is how often the operator may be told again; it is not a
        # promise to send the same page again. A repeat the stack did not move under
        # leads with what moved, so yesterday's reader does not diff two messages.
        record = self.stand_down_once()
        said = record.read_text().splitlines()
        record.write_text("\n".join(said[:-1]) + "\n")  # as if the last one had been fixed
        res = self.run_check(
            "--heal", "--quiet", own_manifest=True,
            env={"DRIFT_STANDDOWN_ALERT_REPEAT_SEC": "0"},
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        text = self.alert_text()
        self.assertIn("what changed since", text)
        self.assertIn(f"new since then: {said[-1]}", text)
        # Sending it re-records what it said, so the next repeat compares against
        # the alert that actually went out, and the interval starts again.
        self.assertEqual(record.read_text().splitlines(), said)
        self.assertGreater(int(self.heal_state_fields()[2]), int(time.time()) - 60)

    def test_a_standing_down_repeat_names_drift_that_went_away(self):
        # The other direction counts too: drift that shrank means a repair worked on
        # part of the stack, which is a different situation from nothing happening.
        record = self.stand_down_once()
        record.write_text("DRIFT-FAIL: a service that is no longer checked\n" + record.read_text())
        res = self.run_check(
            "--heal", "--quiet", own_manifest=True,
            env={"DRIFT_STANDDOWN_ALERT_REPEAT_SEC": "0"},
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(
            "no longer reported since then: DRIFT-FAIL: a service that is no longer checked",
            self.alert_text(),
        )

    def test_a_standing_down_repeat_with_nothing_new_stays_silent(self):
        record = self.stand_down_once()
        before = record.read_text()
        res = self.run_check(
            "--heal", "--quiet", own_manifest=True,
            env={"DRIFT_STANDDOWN_ALERT_REPEAT_SEC": "0"},
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        # Silent, but not unexplained: a quiet timer must not look like a timer that
        # found nothing.
        self.assertIn("no alert - the standing-down alert went out", res.stderr)
        self.assertEqual(self.alert_text(), "")
        self.assertEqual(record.read_text(), before)

    def test_a_standing_down_repeat_without_the_record_alerts(self):
        # Nothing to compare against is not the same as nothing having changed, so
        # the unknown case is sent rather than swallowed.
        self.stand_down_once()
        (self.state / "drift-standdown-alert").unlink()
        res = self.run_check(
            "--heal", "--quiet", own_manifest=True,
            env={"DRIFT_STANDDOWN_ALERT_REPEAT_SEC": "0"},
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("has stood down", self.alert_text())

    def test_a_heal_attempt_keeps_the_standdown_clock(self):
        # One line holds three facts, and a heal attempt is not standing down - so
        # writing that line must not drop the clock of the alert that was sent (a
        # raised DRIFT_HEAL_MAX_STREAK puts a standing-down heal back to work).
        self.seed_heal_state(0, 1, standdown_alerted=999)
        res = self.run_check("--heal", own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(self.heal_state_fields()[1:], ["2", "999"])

    def test_reset_streak_lets_the_heal_try_again(self):
        self.seed_heal_state(0, 5, standdown_alerted=int(time.time()))
        res = self.run_check("--reset-streak")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.state / "drift-heal-last").read_text().split()[1:], ["0", "0"])
        self.assertIn("heal streak reset", res.stdout)
        # Nothing else on stderr, because this run does nothing else: a shell that
        # executes a line meant to be prose reports it there and nowhere else
        # (measured 2026-10-07: a doc-header edit left a bare sentence outside the
        # comment block, and the only symptom was `Pointing: command not found`).
        self.assertEqual(res.stderr, "", "the reset run said something besides its own message")
        (self.state / "drift-heal-last").unlink()
        self.assertEqual(self.run_check("--reset-streak").returncode, 2)

    def test_the_verdict_records_what_the_heal_did(self):
        # One attempt, below the streak, so the heal runs (through the shim) and the
        # re-check it execs records both that it was a re-check and the new streak.
        self.seed_heal_state(0, 1)
        res = self.run_check("--heal", own_manifest=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("compose", self.docker_calls())
        recorded = self.status()
        self.assertEqual(recorded["heal"], "recheck")
        self.assertEqual(recorded["heal_streak"], "2")

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
