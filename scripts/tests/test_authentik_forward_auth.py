#!/usr/bin/env python3
"""Tests for scripts/authentik-forward-auth.py.

Three behaviours here are easy to get silently wrong, and all three have bitten
this deployment in production:

  1. `AUTHENTIK_FORWARD_GROUP` must actually create a policy binding ON THE
     APPLICATION. It used to only look the group up and print PASS, which left
     every gate open to any authenticated user while looking configured.
  2. `--check` must notice a missing binding. A gate whose binding was dropped
     is indistinguishable from a working one until someone outside the group
     gets in, so drift has to be reported, not assumed.
  3. The application lookup must survive Authentik's list endpoint dropping an
     application. `olympus-npm-forward-auth` is unlistable on this instance
     (`count` > len(results), and `?search=` returns count=1 with zero rows)
     while a direct slug GET resolves it — so a list-only lookup reported a
     healthy application as missing.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "authentik-forward-auth.py"

spec = importlib.util.spec_from_file_location("authentik_forward_auth", SCRIPT_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

DOMAIN = "olympus.innotel.us"
PROVIDER = "Olympus NPM Forward Auth"
APP_SLUG = "olympus-npm-forward-auth"
GROUP = "Olympus"
AUTH_FLOW = "default-provider-authorization-implicit-consent"
INVAL_FLOW = "default-invalidation-flow"
APP_PK = "app-1"
GROUP_PK = "grp-1"


def provider_row():
    return {
        "pk": 33,
        "name": PROVIDER,
        "authorization_flow": "flow-auth",
        "invalidation_flow": "flow-inval",
        "external_host": f"https://auth.{DOMAIN}",
        "mode": "forward_domain",
        "cookie_domain": DOMAIN,
    }


def application_row():
    return {"pk": APP_PK, "slug": APP_SLUG, "provider": 33, "policy_engine_mode": "any"}


def binding_row():
    return {"pk": "bind-1", "target": APP_PK, "group": GROUP_PK,
            "enabled": True, "negate": False, "order": 0}


class FakeAk(mod.Ak):
    """An in-memory Authentik, wired through the real `find`/`get_application`.

    Only `_call` is faked, so the lookup and filtering logic under test is the
    production code rather than a reimplementation of it.
    """

    def __init__(self, *, slug_get_ok=True, app_in_list=True,
                 group_exists=True, binding=None):
        super().__init__("https://auth.example.test", "test-token")
        self.slug_get_ok = slug_get_ok
        self.app_in_list = app_in_list
        self.group_exists = group_exists
        self.binding = binding
        self.calls: list[tuple[str, str]] = []
        self.created_bindings: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self._application = application_row()

    # ── transport ────────────────────────────────────────────────────────
    def _call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET":
            return self._get(path)
        if method == "POST":
            return self._post(path, body)
        if method == "PATCH":
            self.patched.append((path, body or {}))
            return {**provider_row(), **(body or {})}
        raise AssertionError(f"unexpected {method} {path}")

    def _get(self, path):
        if path.startswith("/flows/instances/"):
            return {"results": [{"pk": "flow-auth", "slug": AUTH_FLOW},
                                {"pk": "flow-inval", "slug": INVAL_FLOW}]}
        if path.startswith("/core/groups/"):
            return {"results": [{"pk": GROUP_PK, "name": GROUP}] if self.group_exists else []}
        if path.startswith("/core/applications/"):
            if path == "/core/applications/?page_size=200":
                # The list endpoint really does drop an application while still
                # counting it — that is the whole reason for this fallback.
                return {"results": [self._application] if self.app_in_list else []}
            slug = path[len("/core/applications/"):].rstrip("/")
            if self.slug_get_ok and slug == APP_SLUG:
                return self._application
            raise mod.ApiError(f"GET {path} -> HTTP 404: not found")
        if path.startswith("/providers/proxy/"):
            return {"results": [provider_row()]}
        if path.startswith("/policies/bindings/"):
            return {"results": [self.binding] if self.binding else []}
        if path.startswith("/outposts/instances/"):
            return {"results": [{"pk": "op-1", "name": "authentik Embedded Outpost",
                                 "providers": [33]}]}
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, path, body):
        if path == "/policies/bindings/":
            self.created_bindings.append(body or {})
            return {"pk": "bind-new", **(body or {})}
        if path == "/core/applications/":
            self._application = {**application_row(), **(body or {})}
            return self._application
        raise AssertionError(f"unexpected POST {path}")


def run_main(ak, *, group=None, check=False, dry_run=False):
    """Invoke main() with the network replaced, returning (exit code, stdout)."""
    environ = {
        "AUTHENTIK_BASE_URL": "https://auth.example.test",
        "AUTHENTIK_BOOTSTRAP_TOKEN": "test-token",
        "MONARCH_DOMAIN": DOMAIN,
        "AUTHENTIK_FORWARD_PROVIDER": PROVIDER,
        "AUTHENTIK_FORWARD_APP_SLUG": APP_SLUG,
    }
    if group:
        environ["AUTHENTIK_FORWARD_GROUP"] = group

    argv = ["authentik-forward-auth.py", "--env-file", "/nonexistent/.env"]
    if check:
        argv.append("--check")
    if dry_run:
        argv.append("--dry-run")

    saved = {key: os.environ.get(key) for key in environ}
    original_ak, original_argv = mod.Ak, sys.argv
    mod.Ak = lambda base, token: ak
    mod.sys.argv = argv
    os.environ.update(environ)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = mod.main()
    finally:
        mod.Ak = original_ak
        mod.sys.argv = original_argv
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, buffer.getvalue()


class ApplicationLookup(unittest.TestCase):
    """`get_application` must not depend on the list endpoint behaving."""

    def test_direct_slug_get_finds_an_application_the_list_omits(self):
        ak = FakeAk(app_in_list=False)  # the real quirk, reproduced
        app = ak.get_application(APP_SLUG)
        self.assertIsNotNone(app)
        self.assertEqual(app["slug"], APP_SLUG)
        self.assertIn(("GET", f"/core/applications/{APP_SLUG}/"), ak.calls)

    def test_falls_back_to_the_list_when_the_slug_get_fails(self):
        ak = FakeAk(slug_get_ok=False)  # e.g. an endpoint without slug routing
        app = ak.get_application(APP_SLUG)
        self.assertIsNotNone(app)
        self.assertEqual(app["slug"], APP_SLUG)

    def test_returns_none_when_neither_source_has_it(self):
        ak = FakeAk(slug_get_ok=False, app_in_list=False)
        self.assertIsNone(ak.get_application(APP_SLUG))

    def test_check_does_not_call_a_missing_application_drift_when_only_the_list_omits_it(self):
        # The regression: this used to exit 1 with "application ... missing".
        ak = FakeAk(app_in_list=False, binding=binding_row())
        code, out = run_main(ak, group=GROUP, check=True)
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK OK", out)


class GroupBinding(unittest.TestCase):

    def test_creates_the_binding_when_a_group_is_configured(self):
        ak = FakeAk()
        code, out = run_main(ak, group=GROUP)
        self.assertEqual(code, 0, out)
        self.assertIn("only its members pass the gate", out)
        self.assertEqual(len(ak.created_bindings), 1)
        binding = ak.created_bindings[0]
        self.assertEqual(binding["target"], APP_PK)
        self.assertEqual(binding["group"], GROUP_PK)
        self.assertTrue(binding["enabled"])
        self.assertFalse(binding["negate"])

    def test_no_binding_is_created_when_no_group_is_configured(self):
        ak = FakeAk()
        code, out = run_main(ak, group=None)
        self.assertEqual(code, 0, out)
        self.assertEqual(ak.created_bindings, [])
        self.assertIn("any authenticated Authentik user", out)

    def test_dry_run_reports_the_binding_without_writing_it(self):
        ak = FakeAk()
        code, out = run_main(ak, group=GROUP, dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(ak.created_bindings, [])
        self.assertIn("would bind application", out)

    def test_check_flags_a_missing_binding_as_drift(self):
        ak = FakeAk(binding=None)
        code, out = run_main(ak, group=GROUP, check=True)
        self.assertEqual(code, 1, out)
        self.assertIn("not restricted to group", out)

    def test_check_passes_when_the_binding_is_present(self):
        ak = FakeAk(binding=binding_row())
        code, out = run_main(ak, group=GROUP, check=True)
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK OK", out)

    def test_check_flags_a_group_that_does_not_exist(self):
        ak = FakeAk(group_exists=False)
        code, _ = run_main(ak, group=GROUP, check=True)
        self.assertEqual(code, 1)


class ProviderAndOutpost(unittest.TestCase):

    def test_rerun_does_not_mutate_an_already_correct_provider(self):
        ak = FakeAk(binding=binding_row())
        code, _ = run_main(ak, group=GROUP)
        self.assertEqual(code, 0)
        self.assertEqual(ak.patched, [])


if __name__ == "__main__":
    unittest.main()
