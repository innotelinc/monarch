#!/usr/bin/env python3
"""Tests for the Jellyfin admin rights `init/init.py` applies to the local admin.

The OIDC role mapping is applied by the plugin when a user logs in *through the
provider*, so it never reaches `MONARCH_USERNAME` — a local account, and the one
an operator uses to run the DVR. Jellyfin offers no way to delete a recording
unless that account carries `EnableLiveTvManagement`, so the gap stays invisible
until the Recordings library is full and nothing in the UI will clear it.

`POST /Users/{id}/Policy` replaces the whole policy rather than merging into it,
which is what makes the read-modify-write worth testing: a version that skipped
the read would quietly strip the account's other permissions.

No network: `_http` is stubbed.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent.parent / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)


class FakeJellyfin:
    """A `_http` that records its calls, so the writes can be asserted."""

    def __init__(self, policy: dict | None, name: str | None = None):
        self.name = init_mod.USER if name is None else name
        self.policy = policy
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, base, path, method="GET", body=None, headers=None, **_):
        self.calls.append((path, method, body))
        if path == "/Users":
            if self.policy is None:
                return 500, "", None
            return 200, "", [{"Id": "abc123", "Name": self.name, "Policy": self.policy}]
        return 204, "", None

    def posted_policy(self) -> dict | None:
        for path, method, body in self.calls:
            if method == "POST" and path.endswith("/Policy"):
                return body
        return None


class TheAdminGroupMapping(unittest.TestCase):
    """One source of truth for what an admin holds."""

    def test_the_local_admin_gets_what_the_group_grants(self) -> None:
        self.assertEqual(init_mod.jellyfin_admin_permissions(),
                         {"EnableContentDeletion": True, "EnableLiveTvManagement": True})


class ApplyingIt(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (init_mod._http, init_mod._issues)
        init_mod._issues = []
        self.addCleanup(self.restore)

    def restore(self) -> None:
        init_mod._http, init_mod._issues = self.saved

    def test_it_folds_the_flags_into_the_existing_policy(self) -> None:
        # Both flags off, something else on: the write has to carry all three.
        fake = FakeJellyfin({"EnableContentDeletion": False,
                             "EnableLiveTvManagement": False,
                             "EnableAllLibraries": True})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_admin_permissions("token"))
        self.assertEqual(fake.posted_policy(), {"EnableContentDeletion": True,
                                                "EnableLiveTvManagement": True,
                                                "EnableAllLibraries": True})

    def test_a_policy_that_already_agrees_is_left_alone(self) -> None:
        fake = FakeJellyfin({"EnableContentDeletion": True, "EnableLiveTvManagement": True})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_admin_permissions("token"))
        self.assertIsNone(fake.posted_policy())

    def test_a_missing_account_is_an_issue_not_a_crash(self) -> None:
        fake = FakeJellyfin({"EnableContentDeletion": False}, name="somebody-else")
        init_mod._http = fake
        self.assertFalse(init_mod.jellyfin_ensure_admin_permissions("token"))
        self.assertTrue(any("no account named" in issue for issue in init_mod._issues))

    def test_an_unreadable_user_list_is_an_issue(self) -> None:
        init_mod._http = FakeJellyfin(None)
        self.assertFalse(init_mod.jellyfin_ensure_admin_permissions("token"))
        self.assertTrue(init_mod._issues)


if __name__ == "__main__":
    unittest.main()
