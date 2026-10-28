#!/usr/bin/env python3
"""Tests for the Known proxies entry `init/init.py` records for the SSO gateway.

WHY THIS EXISTS
---------------
Jellyfin honours `X-Forwarded-Proto` only from a caller in its own
NetworkConfiguration `KnownProxies`, and with that list empty it does not merely
ignore the header — it turns forwarded headers off entirely:

    if (config.KnownProxies.Length == 0)
    {
        options.ForwardedHeaders = ForwardedHeaders.None;
        ...
    }

(`Jellyfin.Server/Extensions/ApiServiceCollectionExtensions.cs`,
`ConfigureForwardHeaders`). What that breaks is the OIDC plugin's **start URL**:
the plugin builds it from the *request* scheme, not from the forwarded one, so
the login page — and `/sso/OIDC/Providers`, which is how a native client
discovers the provider — advertised

    http://media.magnate.innotel.us/sso/OIDC/Start/authentik

on a name that serves https only. A desktop browser follows the `80 -> 443`
redirect without noticing, which is why a hand-run login test passed; an
iOS/Android webview refuses the cleartext request, and "I cannot sign in from the
app" was the whole report. (`getRequestBase` ignoring `X-Forwarded-Proto` is
upstream's, 9p4/jellyfin-plugin-sso#345.)

Three things are pinned here, because each of them is wrong quietly:

  * **Both spellings go in.** Jellyfin parses each entry as an IP, a subnet *or*
    a hostname (`AddProxyAddresses` → `NetworkUtils.TryParseHost`), so the docker
    name survives a recreated gateway — and its resolved address is what keeps
    the gateway trusted when the restart happens while the name does not resolve.
  * **The write is a read-modify-write.** `POST /System/Configuration/network`
    takes the whole configuration, so a body carrying only `KnownProxies` would
    drop the rest of it — the same shape as the admin policy in
    `test_init_jellyfin_admin.py`.
  * **A change reports the restart it needs.** Jellyfin reads `KnownProxies` at
    *startup*, so init records the list and says a restart is required rather
    than taking the media server down; `scripts/verify-sso.py` step [2c] is what
    proves the result from outside (it asserts an https start URL).

No network: `_http` is stubbed, and so is `socket.getaddrinfo` — a unit test that
needs docker DNS to find the gateway would fail on the machine it runs on rather
than on the code it judges.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import socket
import unittest
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent.parent / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init_known_proxies", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)

CONFIG_PATH = "/System/Configuration/network"


class FakeJellyfin:
    """A `_http` that records its calls, so the write can be asserted."""

    def __init__(self, config: dict | None, write_status: int = 204):
        self.config = config
        self.write_status = write_status
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, base, path, method="GET", body=None, headers=None, **_):
        self.calls.append((path, method, body))
        if method == "GET":
            if self.config is None:
                return 500, "", None
            return 200, "", self.config
        return self.write_status, "", None

    def methods(self) -> list[str]:
        return [method for _, method, _ in self.calls]

    def posted_config(self) -> dict | None:
        for path, method, body in self.calls:
            if method == "POST":
                return body
        return None

    def posted_known_proxies(self) -> list | None:
        posted = self.posted_config()
        return None if posted is None else posted.get("KnownProxies")


def resolver(*addresses: str):
    """A `socket.getaddrinfo` that answers with these addresses, in order."""

    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]

    return resolve


def unresolvable():
    """A `socket.getaddrinfo` for a name this host cannot resolve."""

    def resolve(host, *args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    return resolve


class TheGatewayEntries(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = init_mod.socket.getaddrinfo
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        init_mod.socket.getaddrinfo = self.saved

    def test_the_name_comes_first_then_each_address_once(self) -> None:
        # The gateway is `jellyfin-sso` on the compose network. Two A records
        # (or the same one twice, as getaddrinfo can answer) must not become two
        # entries: the list is read by a human in Dashboard -> Networking.
        init_mod.socket.getaddrinfo = resolver("172.20.0.7", "172.20.0.7")
        self.assertEqual(init_mod.jellyfin_gateway_entries(),
                         [init_mod.JELLYFIN_SSO_GATEWAY, "172.20.0.7"])

    def test_an_unresolvable_name_is_still_recorded(self) -> None:
        # Migrations and restarts happen: the name is what resolves again on the
        # next Jellyfin start, so a gateway that does not resolve *now* is not a
        # reason to record nothing.
        init_mod.socket.getaddrinfo = unresolvable()
        self.assertEqual(init_mod.jellyfin_gateway_entries(),
                         [init_mod.JELLYFIN_SSO_GATEWAY])


class EnsuringTheGatewayIsTrusted(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (init_mod._http, init_mod._issues, init_mod.socket.getaddrinfo)
        init_mod._issues = []
        init_mod.socket.getaddrinfo = resolver("172.20.0.7")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        init_mod._http, init_mod._issues, init_mod.socket.getaddrinfo = self.saved

    def test_the_gateway_is_added_when_nothing_is_trusted(self) -> None:
        # The state the media host was in: no network.xml at all, so Jellyfin
        # forwarded nothing and advertised an http:// start URL.
        fake = FakeJellyfin({})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertEqual(fake.posted_known_proxies(),
                         [init_mod.JELLYFIN_SSO_GATEWAY, "172.20.0.7"])
        self.assertEqual([path for path, _, _ in fake.calls], [CONFIG_PATH, CONFIG_PATH])

    def test_the_write_carries_the_rest_of_the_configuration(self) -> None:
        # POST /System/Configuration/network replaces the object, so anything the
        # read produced has to travel back with the new list.
        fake = FakeJellyfin({"KnownProxies": ["127.0.0.1"],
                             "BaseUrl": "https://media.innotel.us",
                             "EnableHttps": False})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_known_proxies("token"))
        posted = fake.posted_config()
        self.assertEqual(posted["KnownProxies"],
                         ["127.0.0.1", init_mod.JELLYFIN_SSO_GATEWAY, "172.20.0.7"])
        self.assertEqual(posted["BaseUrl"], "https://media.innotel.us")
        self.assertIs(posted["EnableHttps"], False)

    def test_a_gateway_already_trusted_is_left_alone(self) -> None:
        fake = FakeJellyfin({"KnownProxies": [init_mod.JELLYFIN_SSO_GATEWAY, "172.20.0.7"]})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertEqual(fake.methods(), ["GET"])

    def test_a_name_that_does_not_resolve_records_the_name_alone(self) -> None:
        init_mod.socket.getaddrinfo = unresolvable()
        fake = FakeJellyfin({})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertEqual(fake.posted_known_proxies(), [init_mod.JELLYFIN_SSO_GATEWAY])

    def test_the_write_reports_the_restart_it_needs(self) -> None:
        # The list is read once, at startup: without this the operator sees
        # "configured" and the login page keeps the http start URL.
        fake = FakeJellyfin({})
        init_mod._http = fake
        self.assertTrue(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertTrue(any("restart the jellyfin" in issue for issue in init_mod._issues
                            if issue.startswith("Jellyfin: the SSO gateway was added")))

    def test_an_unreadable_configuration_is_an_issue_not_a_crash(self) -> None:
        init_mod._http = FakeJellyfin(None)
        self.assertFalse(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertTrue(any("http:// sign-in URL" in issue for issue in init_mod._issues))

    def test_a_failed_write_is_an_issue_and_not_reported_as_done(self) -> None:
        # 401/500 here means the gateway is NOT trusted, so returning True (or
        # saying nothing) would leave the symptom in place with a green run.
        init_mod._http = FakeJellyfin({}, write_status=500)
        self.assertFalse(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertTrue(any("could not record the SSO gateway" in issue
                            for issue in init_mod._issues))
        self.assertFalse(any("was added to Known proxies" in issue
                             for issue in init_mod._issues))

    def test_a_configuration_that_is_not_an_object_is_not_trusted(self) -> None:
        # A 200 that is not a config (an error page, an empty body) must not be
        # read as "the list is empty, nothing to do".
        class NotAnObject(FakeJellyfin):
            def __call__(self, base, path, method="GET", body=None, headers=None, **_):
                self.calls.append((path, method, body))
                return 200, "", []

        init_mod._http = NotAnObject(None)
        self.assertFalse(init_mod.jellyfin_ensure_known_proxies("token"))
        self.assertTrue(init_mod._issues)


if __name__ == "__main__":
    unittest.main()
