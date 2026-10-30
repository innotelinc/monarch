#!/usr/bin/env python3
"""The Cerulean service-bridge DNS path in npm-proxy-hosts.py.

Pins what replaced the Technitium HTTP API on Monarch: which variables
configure the bridge, how the zone defaults from MONARCH_DOMAIN, and the rule
that a name which already resolves is left alone (Monarch's hosts are CNAMEs to
the apex, which holds the A record).
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = next(p for p in (os.path.join(os.path.dirname(HERE), "npm-proxy-hosts.py"),
                          os.path.join(HERE, "npm-proxy-hosts.py")) if os.path.exists(p))
SPEC = importlib.util.spec_from_file_location("npm_proxy_hosts", SCRIPT)
nph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nph)

ENV_KEYS = ["CERULEAN_API_URL", "CERULEAN_SERVICE_KEY", "CERULEAN_TOKEN",
            "CERULEAN_ZONE", "MONARCH_DOMAIN", "TECHNITIUM_URL"]


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self._api = nph.cerulean_api

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        nph.cerulean_api = self._api


class ZoneTest(_Base):
    def test_registrable_zone(self):
        self.assertEqual(nph.zone_of("monarch.innotel.us"), "innotel.us")
        self.assertEqual(nph.zone_of("a.b.innotel.us"), "innotel.us")
        self.assertEqual(nph.zone_of("innotel.us"), "innotel.us")


class ConfigTest(_Base):
    def test_unset_or_keyless_is_none(self):
        self.assertIsNone(nph.cerulean_config())
        os.environ["CERULEAN_API_URL"] = "http://192.168.1.71:3003"
        self.assertIsNone(nph.cerulean_config())

    def test_configured_strips_url_and_defaults_zone(self):
        os.environ["CERULEAN_API_URL"] = "http://192.168.1.71:3003/"
        os.environ["CERULEAN_SERVICE_KEY"] = "ceru_abc_def"
        os.environ["MONARCH_DOMAIN"] = "monarch.innotel.us"
        self.assertEqual(nph.cerulean_config(),
                         ("http://192.168.1.71:3003", "ceru_abc_def", "innotel.us"))

    def test_zone_override_and_token_alias(self):
        os.environ["CERULEAN_API_URL"] = "http://c:3003"
        os.environ["CERULEAN_TOKEN"] = "ceru_x_y"
        os.environ["CERULEAN_ZONE"] = "example.com"
        self.assertEqual(nph.cerulean_config(), ("http://c:3003", "ceru_x_y", "example.com"))


class UpsertTest(_Base):
    CFG = ("http://c:3003", "ceru_x_y", "innotel.us")

    def test_existing_name_is_left_alone(self):
        calls = []

        def fake(url, token, path, params=None, method="GET", body=None):
            calls.append((method, path, params, body))
            if method == "GET":
                return [{"name": "tv.monarch.innotel.us", "type": "CNAME",
                         "value": "innotel.us"}]
            raise AssertionError("must not write when the name resolves")

        nph.cerulean_api = fake
        self.assertTrue(nph.dns_upsert_cerulean(
            self.CFG, "tv.monarch.innotel.us", "192.168.1.56"))
        self.assertEqual([m for m, *_ in calls], ["GET"])

    def test_missing_name_to_a_private_target_becomes_a_cname_to_the_apex(self):
        """A LAN address must never reach the public zone.

        Every row here forwards to this host's LAN address, so writing the A
        record the script used to write published a name that resolves for
        nobody outside the LAN, plus the internal topology. The zone's own
        convention is a CNAME to the apex, which holds the A record.
        """
        calls = []

        def fake(url, token, path, params=None, method="GET", body=None):
            calls.append((method, path, params, body))
            return [] if method == "GET" else {"ok": True}

        nph.cerulean_api = fake
        self.assertTrue(nph.dns_upsert_cerulean(
            self.CFG, "new.monarch.innotel.us", "192.168.1.56"))
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = posts[0][3]
        self.assertEqual(body["type"], "CNAME")
        self.assertEqual(body["value"], "innotel.us.")
        self.assertEqual(body["name"], "new.monarch.innotel.us")
        self.assertEqual(body["zone"], "innotel.us")

    def test_missing_name_to_a_public_target_gets_an_a_record(self):
        """A target outside the LAN is still written as the A record it is."""
        calls = []

        def fake(url, token, path, params=None, method="GET", body=None):
            calls.append((method, path, params, body))
            return [] if method == "GET" else {"ok": True}

        nph.cerulean_api = fake
        self.assertTrue(nph.dns_upsert_cerulean(
            self.CFG, "new.monarch.innotel.us", "1.2.3.4"))
        body = [c for c in calls if c[0] == "POST"][0][3]
        self.assertEqual(body["type"], "A")
        self.assertEqual(body["value"], "1.2.3.4")
        self.assertEqual(body["name"], "new.monarch.innotel.us")
        self.assertEqual(body["zone"], "innotel.us")

    def test_dry_run_writes_nothing(self):
        def boom(*a, **k):
            raise AssertionError("dry-run must not call the API")

        nph.cerulean_api = boom
        self.assertTrue(nph.dns_upsert_cerulean(
            self.CFG, "x.innotel.us", "1.2.3.4", dry_run=True))


if __name__ == "__main__":
    unittest.main()
