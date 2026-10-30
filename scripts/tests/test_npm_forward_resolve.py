#!/usr/bin/env python3
"""Which upstream `npm-proxy-hosts.py` forwards a row to.

Pins the rule that decides where a proxy host points, because getting it wrong
is invisible from here: the row still saves, nginx still starts, and the name
answers 502 (or 000) for whoever clicks it.

The case that motivated this file: `admin.<domain>` was written as
`127.0.0.1:4180` on the belief that the oauth2-proxy gateway shares the NPM
container's network namespace. Measured on the NPM host 2026-10-30, it does not
— `127.0.0.1:4180` inside `cerulean-npm` answers 000, while the container name
answers 302 — so the row names the container and `docker:` is how it says so
without being mistaken for one of this stack's compose services.
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

DOMAIN = "monarch.innotel.us"


def row(forward, port=4180):
    return {"domain": "admin." + DOMAIN, "forward": forward, "port": port,
            "websockets": False}


class DockerUpstreamTest(unittest.TestCase):
    """A `docker:` forward names a container on the NPM host and always wins."""

    def test_prefix_returns_the_container_name(self):
        self.assertEqual(nph.resolve_forward(row("docker:cerulean-npm-sso"), "192.168.1.56"),
                         "cerulean-npm-sso")

    def test_prefix_wins_over_a_container_forward_mode(self):
        self.assertEqual(nph.resolve_forward(row("docker:cerulean-npm-sso"), "container"),
                         "cerulean-npm-sso")

    def test_prefix_is_not_case_sensitive_about_its_own_name(self):
        # The name after the prefix is Docker's, so it is passed through as
        # written; only the surrounding whitespace is normalised.
        self.assertEqual(nph.resolve_forward(row("docker: cerulean-npm-sso "), "container"),
                         "cerulean-npm-sso")

    def test_empty_container_name_is_refused(self):
        # A `docker:` with nothing after it would otherwise forward to "" and
        # save a host that nginx cannot resolve at all.
        with self.assertRaises(SystemExit):
            nph.resolve_forward(row("docker:"), "container")


class OtherUpstreamTest(unittest.TestCase):
    """The two pre-existing spellings still mean what they meant."""

    def test_ip_is_explicit(self):
        self.assertEqual(nph.resolve_forward(row("127.0.0.1"), "192.168.1.56"), "127.0.0.1")

    def test_dotted_hostname_is_explicit(self):
        self.assertEqual(nph.resolve_forward(row("192.168.1.71"), "container"), "192.168.1.71")

    def test_bare_service_follows_a_container_mode(self):
        self.assertEqual(nph.resolve_forward(row("homarr", 7575), "container"), "homarr")

    def test_bare_service_follows_a_remote_mode(self):
        # This is why the prefix exists: a bare `cerulean-npm-sso` would land
        # here, i.e. be sent to the media host, which runs no such container.
        self.assertEqual(nph.resolve_forward(row("cerulean-npm-sso"), "192.168.1.56"),
                         "192.168.1.56")


class ShippedConfTest(unittest.TestCase):
    """The row this repo actually ships, not a fixture."""

    def setUp(self):
        self._saved = os.environ.get("MONARCH_DOMAIN")

    def tearDown(self):
        os.environ.pop("MONARCH_DOMAIN", None)
        if self._saved is not None:
            os.environ["MONARCH_DOMAIN"] = self._saved

    def test_admin_names_the_gateway_container(self):
        hosts, source = nph.load_hosts(DOMAIN)
        admin = next(h for h in hosts if h["domain"] == "admin." + DOMAIN)
        self.assertEqual(nph.resolve_forward(admin, "192.168.1.56"), "cerulean-npm-sso",
                         f"admin must name the gateway container ({source})")
        self.assertEqual(admin["port"], 4180)

    def test_admin_is_not_loopback(self):
        # The regression this guards: 127.0.0.1 is unreachable from nginx,
        # because the gateway does not share the NPM container's netns.
        hosts, _ = nph.load_hosts(DOMAIN)
        admin = next(h for h in hosts if h["domain"] == "admin." + DOMAIN)
        self.assertNotEqual(nph.resolve_forward(admin, "container"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
