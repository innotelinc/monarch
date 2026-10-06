#!/usr/bin/env python3
"""Tests for `jellyfin-admin-password.py --check-apps` — the keys the apps hold.

`--check-apps` exists for one failure: a half-finished rotation (a new key
minted, the old one deleted, an app still configured with it) leaves the app
authenticating with a token Jellyfin has forgotten. Nothing else notices - the
container is up, its own UI answers, and only its requests to Jellyfin fail.

The distinction these tests pin is the one that made it lie. A key is judged by
a *verdict*: 200 authenticates, 401/403 is a rejection. Anything else is the
server declining to answer - and that is not the same finding. Jellyfin after
its own `/System/Restart` runs a setup server that listens first and answers 503
for the whole ~40s boot, so a check that calls any non-200 a rejected key
reports a restart as *an app's stored API key no longer authenticates*. That is
what `drift-check --heal` printed seconds after restarting the stack it had just
reconciled, naming a credential nothing was wrong with.

No network and no real appdata: the Jellyseerr settings file is a temp file and
the Homarr path points at nothing, so only the Jellyseerr half is exercised.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "jellyfin-admin-password.py"


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_admin_password", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


jap = _load()


class FakeJellyfin:
    """A `verify` that answers with one fixed status, recording the keys."""

    def __init__(self, status: int):
        self.status = status
        self.checked: list[str] = []

    def verify(self, token: str) -> int:
        self.checked.append(token)
        return self.status


class TheAppKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {k: os.environ.get(k)
                      for k in ("JELLYSEERR_SETTINGS_FILE", "HOMARR_DB_FILE")}
        self.addCleanup(self.restore_env)
        # Homarr is absent on purpose: its half is a note, not a verdict.
        os.environ["HOMARR_DB_FILE"] = str(Path(self.tmp.name) / "no-such-homarr.sqlite")
        self.settings = Path(self.tmp.name) / "settings.json"
        os.environ["JELLYSEERR_SETTINGS_FILE"] = str(self.settings)

    def restore_env(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_key(self, key: str) -> None:
        self.settings.write_text(json.dumps({"jellyfin": {"apiKey": key}}),
                                 encoding="utf-8")

    def run_check(self, status: int) -> tuple[int, str]:
        self.write_key("a-stored-key")
        jf = FakeJellyfin(status)
        out = StringIO()
        with redirect_stdout(out):
            failures = jap.check_app_keys(jf, log=print)
        self.assertEqual(jf.checked, ["a-stored-key"],
                         "the stored key was never presented to Jellyfin")
        return failures, out.getvalue()

    def test_a_working_key_authenticates(self) -> None:
        failures, out = self.run_check(200)
        self.assertEqual(failures, 0)
        self.assertIn("holds a Jellyfin API key that still authenticates", out)

    def test_a_rejected_key_is_drift(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                failures, out = self.run_check(status)
                self.assertEqual(failures, 1, "a rejection must be a finding")
                self.assertIn("DRIFT", out)
                self.assertIn(f"HTTP {status}", out)
                self.assertIn("Re-run the rotation", out)

    def test_a_server_that_did_not_answer_is_not_a_rejected_key(self) -> None:
        # 503 is exactly what Jellyfin's setup server answers while it boots
        # after /System/Restart; 0 is no reply at all (a stopped container).
        for status in (0, 503):
            with self.subTest(status=status):
                failures, out = self.run_check(status)
                self.assertEqual(
                    failures, 0,
                    "a server that never read the key cannot have rejected it")
                self.assertNotIn("DRIFT", out)
                self.assertIn("could not be judged", out)
                self.assertIn(f"HTTP {status}", out)

    def test_an_empty_stored_key_is_still_drift(self) -> None:
        # The other half of a half-finished rotation: the app holds no key.
        self.write_key("")
        out = StringIO()
        with redirect_stdout(out):
            failures = jap.check_app_keys(FakeJellyfin(200), log=print)
        self.assertEqual(failures, 1)
        self.assertIn("stores no Jellyfin API key", out.getvalue())

    def test_a_missing_settings_file_is_not_a_failure(self) -> None:
        # Nothing to read is a note everywhere else in this script; the finding
        # it exists for is a key Jellyfin REJECTS.
        os.environ["JELLYSEERR_SETTINGS_FILE"] = str(Path(self.tmp.name) / "absent.json")
        out = StringIO()
        with redirect_stdout(out):
            failures = jap.check_app_keys(FakeJellyfin(200), log=print)
        self.assertEqual(failures, 0)
        self.assertIn("not found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
