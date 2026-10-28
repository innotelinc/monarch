#!/usr/bin/env python3
"""Unit tests for ldap-outpost-host.py.

The outage this pins is not a credential: Authentik keeps the LDAP bind flow in
an `authentik_session` cookie scoped to `.innotel.us`, and a cookie for that
domain is never sent to a bare IP. The outpost then loses the flow between the
challenge and the credentials, loops until `exceeded stage recursion depth`, and
every bind returns LDAP 49. These tests hold the three pieces that decide what
happens next: whether a host can hold the cookie, which name the repair picks,
and what it writes to .env.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ldap-outpost-host.py"


def _load():
    spec = importlib.util.spec_from_file_location("ldap_outpost_host", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hostcheck = _load()


class CookieDomainTests(unittest.TestCase):
    def test_a_name_under_the_cookie_domain_matches(self):
        self.assertTrue(hostcheck.host_matches_cookie_domain(
            "auth.cerulean.innotel.us", "innotel.us"))
        self.assertTrue(hostcheck.host_matches_cookie_domain(
            "innotel.us", ".innotel.us"))

    def test_an_ip_never_matches_a_domain(self):
        # The failure that started this: the cookie is scoped to .innotel.us and
        # the outpost was dialing the LAN address.
        self.assertFalse(hostcheck.host_matches_cookie_domain(
            "192.168.1.71", "innotel.us"))

    def test_a_host_outside_the_domain_does_not_match(self):
        self.assertFalse(hostcheck.host_matches_cookie_domain(
            "auth.example.com", "innotel.us"))

    def test_a_host_only_cookie_matches_every_host(self):
        self.assertTrue(hostcheck.host_matches_cookie_domain(
            "192.168.1.71", ""))

    def test_a_suffix_without_a_label_boundary_does_not_match(self):
        self.assertFalse(hostcheck.host_matches_cookie_domain(
            "notinnotel.us", "innotel.us"))


class ParsingTests(unittest.TestCase):
    def test_url_host_and_port(self):
        self.assertEqual(hostcheck.host_from_url("http://192.168.1.71:9000"),
                         "192.168.1.71")
        self.assertEqual(hostcheck.host_from_url("https://auth.cerulean.innotel.us"),
                         "auth.cerulean.innotel.us")
        self.assertEqual(hostcheck.port_from_url("http://192.168.1.71:9000"), "9000")
        self.assertEqual(hostcheck.port_from_url("https://auth.cerulean.innotel.us"),
                         "9000")

    def test_env_comments_are_stripped_like_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "AUTHENTIK_BASE_URL=http://192.168.1.71:9000  # LAN\n"
                "AUTHENTIK_LDAP_AUTHORITY=auth.cerulean.innotel.us\n",
                encoding="utf-8")
            env = hostcheck.load_env(str(path))
        self.assertEqual(env["AUTHENTIK_BASE_URL"], "http://192.168.1.71:9000")
        self.assertEqual(env["AUTHENTIK_LDAP_AUTHORITY"],
                         "auth.cerulean.innotel.us")


class RepairChoiceTests(unittest.TestCase):
    def test_the_public_name_wins_over_the_ip(self):
        env = {
            "AUTHENTIK_BASE_URL": "http://192.168.1.71:9000",
            "AUTHENTIK_UPSTREAM_HOST": "192.168.1.71",
            "MONARCH_SSO_AUTHENTIK_BASE": "https://auth.cerulean.innotel.us",
        }
        authority, address, port = hostcheck.desired(env, "innotel.us", None)
        self.assertEqual(authority, "auth.cerulean.innotel.us")
        self.assertEqual(address, "192.168.1.71")
        self.assertEqual(port, "9000")

    def test_the_default_name_is_under_the_deployment_domain(self):
        env = {}
        authority, address, port = hostcheck.desired(env, "innotel.us", None)
        self.assertEqual(authority, hostcheck.DEFAULT_AUTHORITY)
        self.assertEqual(address, hostcheck.DEFAULT_IP)
        self.assertEqual(port, "9000")

    def test_no_compatible_name_is_an_error_not_a_bind_to_the_ip(self):
        env = {"AUTHENTIK_BASE_URL": "http://192.168.1.71:9000"}
        with self.assertRaises(ValueError):
            hostcheck.desired(env, "example.com", None)


class WriteEnvTests(unittest.TestCase):
    def test_the_three_keys_are_replaced_and_a_backup_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "MONARCH_PASSWORD=keep-me\n"
                "AUTHENTIK_LDAP_AUTHORITY=192.168.1.71\n"
                "AUTHENTIK_LDAP_BIND_TOKEN=keep-this-too\n",
                encoding="utf-8")
            changed = hostcheck.write_env(
                str(path), hostcheck.load_env(str(path)),
                "auth.cerulean.innotel.us", "192.168.1.71", "9000")
            self.assertTrue(changed)
            text = path.read_text(encoding="utf-8")
            self.assertIn("MONARCH_PASSWORD=keep-me", text)
            self.assertIn("AUTHENTIK_LDAP_BIND_TOKEN=keep-this-too", text)
            self.assertIn("AUTHENTIK_LDAP_AUTHORITY=auth.cerulean.innotel.us", text)
            self.assertIn("AUTHENTIK_LDAP_AUTHORITY_IP=192.168.1.71", text)
            self.assertIn("AUTHENTIK_LDAP_AUTHORITY_PORT=9000", text)
            backups = list(Path(tmp).glob(".env.bak-ldap-host-*"))
            self.assertEqual(len(backups), 1)

    def test_a_compatible_env_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "AUTHENTIK_LDAP_AUTHORITY=auth.cerulean.innotel.us\n"
                "AUTHENTIK_LDAP_AUTHORITY_IP=192.168.1.71\n"
                "AUTHENTIK_LDAP_AUTHORITY_PORT=9000\n",
                encoding="utf-8")
            changed = hostcheck.write_env(
                str(path), hostcheck.load_env(str(path)),
                "auth.cerulean.innotel.us", "192.168.1.71", "9000")
            self.assertFalse(changed)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self.old_env_file = hostcheck.ENV_FILE
        hostcheck.ENV_FILE = str(self.env_path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(hostcheck, "ENV_FILE", self.old_env_file))

    def test_check_fails_on_an_ip_and_passes_on_a_name(self):
        code = hostcheck.main([
            "--check", "--cookie-domain", "innotel.us",
            "--outpost-host", "http://192.168.1.71:9000"])
        self.assertEqual(code, 1)

        code = hostcheck.main([
            "--check", "--cookie-domain", "innotel.us",
            "--outpost-host", "http://auth.cerulean.innotel.us:9000"])
        self.assertEqual(code, 0)

    def test_an_unreachable_authentik_is_not_a_finding(self):
        # No --cookie-domain and no AUTHENTIK_BASE_URL: nothing to judge, and a
        # check that cannot look must not invent a failure.
        code = hostcheck.main(["--check"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
