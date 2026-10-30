#!/usr/bin/env python3
"""`scripts/check-deployment-current.sh` against throwaway repositories.

The finding this exists for is a checkout that is not the branch: measured on the
media host 2026-10-30, 35 commits behind `origin/main` with those same changes
sitting in the tree as local edits, so drift-check reported OK about code nobody
had deployed. Nothing could see it, which is why the rules below are pinned rather
than trusted:

  * current and clean is the only 0;
  * behind is a finding that names the distance when the object is present, and a
    finding that says "run git pull to see how far" when it is not (guessing a
    count from an absent object is how a check starts inventing facts);
  * local edits are their own finding, because that is the state the host was in;
  * ahead and diverged are reported, not assumed;
  * "not a checkout" and "no such remote" are 2, the cannot-look code, so a
    machine without a checkout stays a note instead of a permanent failure.

Each test builds its own origin + two clones under a temporary directory and
points the check at them with MONARCH_REPO, so nothing here can read or touch the
real repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "check-deployment-current.sh"

GIT = shutil.which("git")


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          check=True, **kw)


@unittest.skipUnless(GIT, "git is required to build the fixtures")
class DeploymentCurrentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="deploy-current-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.origin = self.tmp / "origin.git"
        # `main` explicitly: the check follows refs/heads/main, and a fixture whose
        # default branch depends on the machine's git config tests the config.
        run([GIT, "-c", "init.defaultBranch=main", "init", "--bare", "-q",
             str(self.origin)], self.tmp)

        # The author's clone: commits land here and are pushed to origin.
        self.work = self.tmp / "work"
        run([GIT, "clone", "-q", str(self.origin), str(self.work)], self.tmp)
        self._identity(self.work)
        self._commit(self.work, "first")

        # The deployment: a second clone of the same origin, which is what a host
        # checks out. Its HEAD is what the check compares.
        self.deploy = self.tmp / "deploy"
        run([GIT, "clone", "-q", str(self.origin), str(self.deploy)], self.tmp)
        self._identity(self.deploy)

    # ── fixtures ────────────────────────────────────────────────────────────
    def _identity(self, repo):
        run([GIT, "config", "user.email", "t@example.invalid"], repo)
        run([GIT, "config", "user.name", "Test"], repo)
        run([GIT, "config", "commit.gpgsign", "false"], repo)

    def _commit(self, repo, message, push=True):
        (Path(repo) / "file.txt").write_text(message + "\n")
        run([GIT, "add", "-A"], repo)
        run([GIT, "commit", "-q", "-m", message], repo)
        if push:
            branch = run([GIT, "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
            run([GIT, "push", "-q", "origin", branch], repo)

    def check(self, repo=None, **env):
        full = dict(os.environ)
        full.update({"MONARCH_REPO": str(repo or self.deploy),
                     "MONARCH_GIT_TIMEOUT": "20"})
        full.update(env)
        proc = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                              env=full)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    # ── tests ───────────────────────────────────────────────────────────────
    def test_current_and_clean_passes(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("tree clean", out)

    def test_behind_names_the_distance(self):
        self._commit(self.work, "second")
        self._commit(self.work, "third")
        # The deployment has not pulled; fetch only so the tip object is present,
        # which is the state after any normal `git fetch` on the host.
        run([GIT, "fetch", "-q", "origin"], self.deploy)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("behind", out)
        self.assertIn("by 2 commit(s)", out)

    def test_unfetched_tip_is_still_a_finding(self):
        # No fetch at all: the object is absent, so the count cannot be known.
        # The check must still fail, and must not invent a number.
        self._commit(self.work, "second")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("git pull", out)

    def test_local_edits_are_their_own_finding(self):
        (self.deploy / "file.txt").write_text("edited in place\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("local edit", out)

    def test_ahead_is_reported_not_hidden(self):
        self._commit(self.deploy, "never pushed", push=False)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("ahead", out)

    def test_diverged_is_reported(self):
        self._commit(self.work, "theirs")
        self._commit(self.deploy, "ours", push=False)
        run([GIT, "fetch", "-q", "origin"], self.deploy)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("diverged", out)

    def test_not_a_checkout_cannot_be_judged(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        code, out = self.check(repo=plain)
        self.assertEqual(code, 2, out)
        self.assertIn("not a git checkout", out)

    def test_missing_remote_cannot_be_judged(self):
        run([GIT, "remote", "set-url", "origin", str(self.tmp / "nowhere.git")],
            self.deploy)
        code, out = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("cannot ask", out)

    def test_the_missing_ref_cannot_be_judged(self):
        code, out = self.check(MONARCH_DEPLOY_REF="refs/heads/does-not-exist")
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
