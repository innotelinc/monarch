#!/usr/bin/env python3
"""Unit tests for verify-sso.py's SSO start-URL judgement.

One outage, one test. Jellyfin builds the URL its login page advertises (and
that `/sso/OIDC/Providers` hands to the mobile/TV apps) from the *request*
scheme, and it believes `X-Forwarded-Proto` only from a caller in its
`KnownProxies` list. With that list empty Jellyfin forwards nothing at all, so
the endpoint answered `200` while advertising
`http://media.magnate.innotel.us/sso/OIDC/Start/authentik` on a name that serves
https only. A desktop browser rode the 80 -> 443 redirect, which is why a
hand-run login test passed; the phone app could not, and that was the report.

So what these tests pin is the judgement itself: the scheme a client would have
to follow, not the config that produced it. `start_url_defect` is the whole
decision, and `SSO_PROVIDERS` has to keep naming *both* public Jellyfin hosts —
a check that silently judged one of them would have passed on the day this was
found, because only the queried name is wrong.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "verify-sso.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_sso", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StartUrlDefect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_https_start_url_is_accepted(self):
        provider = {"ProviderId": "authentik",
                    "StartUrl": "https://media.magnate.innotel.us/sso/OIDC/Start/authentik"}
        self.assertEqual(self.mod.start_url_defect(provider), "")

    def test_cleartext_start_url_is_reported(self):
        provider = {"ProviderId": "authentik",
                    "StartUrl": "http://media.magnate.innotel.us/sso/OIDC/Start/authentik"}
        defect = self.mod.start_url_defect(provider)
        self.assertIn("http://media.magnate.innotel.us", defect)
        self.assertIn("webview", defect)
        # The operator has to be sent to the cause, not just the symptom.
        self.assertIn("jellyfin_ensure_known_proxies", defect)

    def test_missing_start_url_is_reported(self):
        self.assertIn("no StartUrl", self.mod.start_url_defect({"ProviderId": "authentik"}))
        self.assertIn("no StartUrl", self.mod.start_url_defect({"StartUrl": "   "}))

    def test_scheme_case_is_not_a_defect(self):
        # A client parses the scheme case-insensitively, so neither may this.
        provider = {"StartUrl": "HTTPS://media.innotel.us/sso/OIDC/Start/authentik"}
        self.assertEqual(self.mod.start_url_defect(provider), "")

    def test_schemeless_url_is_reported(self):
        provider = {"StartUrl": "media.innotel.us/sso/OIDC/Start/authentik"}
        self.assertIn("no scheme", self.mod.start_url_defect(provider))


class ProviderNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_both_jellyfin_names_are_judged(self):
        hosts = {host for _, host in self.mod.SSO_PROVIDERS}
        self.assertIn("media.innotel.us", hosts)
        self.assertIn("media.magnate.innotel.us", hosts)


if __name__ == "__main__":
    unittest.main()
