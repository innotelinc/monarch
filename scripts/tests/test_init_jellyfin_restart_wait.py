#!/usr/bin/env python3
"""Tests for the wait that follows a Jellyfin restart in `init/init.py`.

`monarch-init` restarts Jellyfin through the server's own `POST /System/Restart`,
and that call is asynchronous: it answers while the process it is replacing is
still serving. The wait init used before this one accepted any non-zero status as
"up", so it was satisfied by the outgoing process - init declared the stack wired
while Jellyfin was about to go down and spend ~40s booting.

That is not hypothetical. On monarch at 2026-10-05 00:04 the heal re-checked the
instant init returned, read the booting server, and reported the libraries
missing and the apps' API keys dead about a stack nothing was wrong with.
`wait_for_jellyfin_restart` is the fix: a restart is two observations - the old
process stops answering, and a server that can answer an *authenticated* call
starts - not one.

The second half of it is `jellyfin_serving`: this build runs a setup host that
binds the port before the real server is up, and it answers `/Users` with 503.
`/System/Info/Public` is public, so the setup host can satisfy it; that is why
the test is two calls, and why a permanently-503 `/Users` is a finding even while
the public endpoint says 200.

No network: `_http` is stubbed. The clock is faked too, so the waits are instant
and the deadlines are still exercised.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent.parent / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)


class FakeClock:
    """A `time` module whose every reading is `step` later than the last."""

    def __init__(self, step: float = 5.0):
        self.now = 0.0
        self.step = step

    def time(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, _seconds: float) -> None:  # never really waits
        return None


class ScriptedJellyfin:
    """A `_http` that answers `/Users` from a script and public info with `public`.

    The script is consumed one entry per `/Users` call and its last entry repeats
    once exhausted, so a case can say "down forever" without counting calls.
    """

    def __init__(self, users: list[int], public: int = 200):
        self.users = list(users)
        self.public = public
        self.users_calls = 0
        self.paths: list[str] = []

    def __call__(self, base, path, method="GET", body=None, headers=None,
                 opener=None, timeout=30, raw_form=False):
        self.paths.append(path)
        if path == "/System/Info/Public":
            return self.public, "", None
        if path == "/Users":
            status = self.users[min(self.users_calls, len(self.users) - 1)]
            self.users_calls += 1
            return status, "", None
        return 204, "", None


class TheRestartWait(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (init_mod._http, init_mod._issues, init_mod.time)
        init_mod._issues = []
        init_mod.time = FakeClock()
        self.addCleanup(self.restore)

    def restore(self) -> None:
        init_mod._http, init_mod._issues, init_mod.time = self.saved

    def wait(self, http: ScriptedJellyfin, **kw) -> tuple[bool, str]:
        init_mod._http = http
        out = StringIO()
        with redirect_stdout(out):
            ok = init_mod.wait_for_jellyfin_restart("Jellyfin (after restart)",
                                                    "test-token", **kw)
        return ok, out.getvalue()

    def test_it_waits_for_the_old_process_to_go_and_the_new_one_to_serve(self) -> None:
        # The old process answers twice, the restart takes it away (503 from the
        # setup host), and the wired server answers 200 only at the end.
        http = ScriptedJellyfin([200, 200, 503, 503, 503, 200])
        ok, out = self.wait(http)
        self.assertTrue(ok)
        self.assertEqual(init_mod._issues, [])
        self.assertEqual(http.users_calls, 6,
                         "the wait returned while the outgoing process still answered")
        self.assertIn("is serving.", out)

    def test_a_stale_token_is_still_a_wired_server(self) -> None:
        # A 401 is the real server answering, so a restart is not held up by a
        # credential this wait has no business judging.
        http = ScriptedJellyfin([200, 401, 401, 401])
        ok, out = self.wait(http)
        self.assertTrue(ok)
        self.assertEqual(init_mod._issues, [])

    def test_the_public_endpoint_alone_is_not_serving(self) -> None:
        # The setup host answers /System/Info/Public while the media service is
        # still initializing; /Users stays 503 and that is a finding.
        http = ScriptedJellyfin([503], public=200)
        ok, out = self.wait(http, timeout=30, down_grace=25)
        self.assertFalse(ok)
        self.assertTrue(init_mod._issues and "never came back serving" in init_mod._issues[0])
        self.assertIn("WARNING", out)

    def test_a_restart_that_never_shows_does_not_hang(self) -> None:
        # /System/Restart can be a no-op (nothing to reload). Then the old
        # process keeps serving, and the wait says so rather than spinning.
        http = ScriptedJellyfin([200], public=200)
        ok, out = self.wait(http, timeout=1000)
        self.assertTrue(ok)
        self.assertIn("still serving after", out)

    def test_a_server_that_never_answers_is_bounded_by_the_timeout(self) -> None:
        # No reply at all - a stopped container. The wait ends, and it ends with
        # an issue rather than an exception.
        http = ScriptedJellyfin([0], public=0)
        ok, out = self.wait(http, timeout=30, down_grace=25)
        self.assertFalse(ok)
        self.assertEqual(len(init_mod._issues), 1)


if __name__ == "__main__":
    unittest.main()
