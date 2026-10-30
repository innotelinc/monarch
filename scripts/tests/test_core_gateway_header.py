#!/usr/bin/env python3
"""The identity header core reads must be the one the gateway actually sends.

On 2026-10-30 every `/api/*` call from a signed-in browser answered 401 while the
page itself loaded, so the UI said *not signed in — reload and sign in again* to a
user who was signed in. The gateway was carrying the session correctly; the two
sides of the contract simply named different headers:

  * `OAUTH2_PROXY_PASS_USER_HEADERS` sets `X-Forwarded-User` / `X-Forwarded-Email`
    on the request handed upstream;
  * `OAUTH2_PROXY_SET_XAUTHREQUEST` (`--set-xauthrequest`) sets
    `X-Auth-Request-User` / `X-Auth-Request-Email` on the *response* to the
    browser, for nginx `auth_request` mode — it never reaches an upstream proxy
    target.

`core` was asking for `X-Auth-Request-Email`, which no reverse-proxied request
carries. Nothing in the suite compared the two, so nothing noticed.

These tests hold the join: for each compose file, the header core reads has to be
one this gateway puts on the upstream request, and the code's default has to be
that same name. They are structural on purpose — no container is started, and the
compose files stay the only description of a deployment.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

# What each option puts where. Only the first set can satisfy core, because only
# it is a *request* header: the runner below is what tells the browser about the
# identity, in nginx auth_request deployments, and is not seen by the upstream.
FORWARDED = {"X-Forwarded-Email", "X-Forwarded-User"}
AUTHORIZED_RESPONSE = {"X-Auth-Request-Email", "X-Auth-Request-User"}


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def upstream_identity_headers(env: dict) -> set[str]:
    """The identity headers oauth2-proxy puts on the request it hands upstream."""
    headers: set[str] = set()
    if _truthy(env.get("OAUTH2_PROXY_PASS_USER_HEADERS", "")):
        headers |= FORWARDED
    return headers


def upstream_identity_headers_from_flags(command: list[str]) -> set[str]:
    """The same, for a gateway configured by flags instead of environment."""
    headers: set[str] = set()
    for flag in command:
        name, _, value = str(flag).lstrip("-").partition("=")
        if name == "pass-user-headers" and (not value or _truthy(value)):
            headers |= FORWARDED
    return headers


def load(compose: Path) -> dict:
    return yaml.safe_load(compose.read_text())


class LiveStackContract(unittest.TestCase):
    """docker-compose.yml: the gateway core actually runs behind."""

    def setUp(self):
        self.doc = load(REPO / "docker-compose.yml")

    def test_the_gateway_sends_a_header_through_the_proxy(self):
        env = self.doc["services"]["core-sso"]["environment"]
        self.assertTrue(
            upstream_identity_headers(env),
            "the gateway passes no identity header upstream, so core can only 401",
        )

    def test_core_reads_a_header_the_gateway_sends(self):
        env = self.doc["services"]["core-sso"]["environment"]
        core = self.doc["services"]["core"]["environment"]
        wanted = core["CORE_AUTH_HEADER"]
        self.assertIn(
            wanted,
            upstream_identity_headers(env),
            f"core reads {wanted}, which this gateway never puts on the upstream "
            f"request (it sends {sorted(upstream_identity_headers(env))}); every "
            "signed-in /api/* call would answer 401",
        )

    def test_the_auth_request_header_is_not_mistaken_for_an_upstream_one(self):
        # The exact wrong turn: turning on set-xauthrequest and pointing core at
        # X-Auth-Request-Email reads like a fix and changes nothing, because that
        # header goes to the browser rather than to core.
        env = self.doc["services"]["core-sso"]["environment"]
        core = self.doc["services"]["core"]["environment"]
        if _truthy(env.get("OAUTH2_PROXY_SET_XAUTHREQUEST", "")):
            self.assertFalse(
                core["CORE_AUTH_HEADER"] in AUTHORIZED_RESPONSE,
                "set-xauthrequest sets response headers for nginx auth_request; "
                "it does not hand X-Auth-Request-Email to core",
            )


class SingleGatewayContract(unittest.TestCase):
    """deploy/monarch-core.compose.yml: the one-file deployment of the same pair."""

    def setUp(self):
        self.doc = load(REPO / "deploy" / "monarch-core.compose.yml")

    def test_core_reads_a_header_the_gateway_sends(self):
        command = self.doc["services"]["sso"]["command"]
        wanted = self.doc["services"]["core"]["environment"]["CORE_AUTH_HEADER"]
        self.assertIn(
            wanted,
            upstream_identity_headers_from_flags(command),
            f"core reads {wanted}, which this gateway's flags never put on the "
            "upstream request",
        )


class DefaultContract(unittest.TestCase):
    def test_the_code_default_is_the_header_the_stack_uses(self):
        spec = importlib.util.spec_from_file_location(
            "core_settings", REPO / "unified" / "app" / "settings.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # dataclasses resolves the defining module through sys.modules, so the
        # name has to be there before the decorator runs.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        env = load(REPO / "docker-compose.yml")["services"]["core-sso"]["environment"]
        self.assertIn(module.Settings().auth_header, upstream_identity_headers(env))


if __name__ == "__main__":
    unittest.main()
