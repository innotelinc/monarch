#!/usr/bin/env python3
"""Unit tests for jellyfin-oidc-sso.py — the SSO button on Jellyfin's login page.

The cases these encode are the two halves that must agree, and the ways they look
healthy while being broken: a plugin config that names an issuer nobody signs in
against (the button renders and dies inside Authentik), and a provider that
never got the callback registered (the button dies at the redirect, before any
login form appears). A plugin directory and an Authentik API are stood up here,
so the suite runs anywhere, including CI.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "jellyfin-oidc-sso.py"

ASSEMBLY = "Jellyfin.Plugin.OIDC.dll"
CONFIG = "Jellyfin.Plugin.OIDC.xml"
CLIENT_ID = "monarch-media"

PLUGIN_META = {
    "guid": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f90",
    "name": "OIDC RBAC",
    "owner": "Ezeqielle",
    "category": "Authentication",
    "versions": [{"version": "1.0.10.0", "targetAbi": "10.11.0.0"}],
}


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_oidc_sso", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oidc = _load()


def config_xml(providers) -> str:
    """A plugin configuration holding the given providers."""
    body = []
    for provider in providers:
        fields = "".join(f"<{k}>{v}</{k}>" for k, v in provider.items())
        body.append(f"<OidcProviderConfig>{fields}</OidcProviderConfig>")
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<PluginConfiguration>"
            f"<Providers>{''.join(body)}</Providers>"
            "<DefaultProvider>authentik</DefaultProvider></PluginConfiguration>")


def good_provider(**overrides):
    provider = {
        "ProviderId": "authentik",
        "DisplayName": "Cerulean Authentik",
        "Authority": "https://auth.cerulean.innotel.us/application/o/monarch-media/",
        "ClientId": CLIENT_ID,
        "ClientSecret": "s3cret",
        "Enabled": "true",
        "ServerBaseUrl": "https://media.magnate.innotel.us",
    }
    provider.update(overrides)
    return provider


def parsed(**overrides):
    """The provider as `parse_providers` hands it to the judge: keys lower-cased.

    The XML spells the element names PascalCase (`<ProviderId>`) and the module
    normalises them on the way in, so a test that judged the XML spelling
    directly would be testing a shape nothing else produces.
    """
    return {key.lower(): value for key, value in good_provider(**overrides).items()}


class PluginDirectory:
    """A Jellyfin plugin directory: one plugin folder, one configurations folder."""

    def __init__(self, providers=None, *, assembly=True, meta=PLUGIN_META, config=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        folder = self.root / "OIDC-RBAC"
        folder.mkdir(parents=True)
        if meta is not None:
            (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if assembly:
            (folder / ASSEMBLY).write_bytes(b"MZ")
        if config:
            configs = self.root / "configurations"
            configs.mkdir()
            (configs / CONFIG).write_text(
                config_xml(providers if providers is not None else [good_provider()]),
                encoding="utf-8")

    def cleanup(self):
        self.tmp.cleanup()


class AuthentikStub:
    """Answers `/api/v3/providers/oauth2/` the way Authentik does."""

    def __init__(self, uris, client_id=CLIENT_ID, status=200):
        self.uris = [{"matching_mode": "strict", "url": u} for u in uris]
        self.client_id = client_id
        self.status = status
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if stub.status != 200:
                    self.send_response(stub.status)
                    self.end_headers()
                    return
                payload = {"results": [
                    {"client_id": "someone-else", "redirect_uris": []},
                    {"client_id": stub.client_id, "redirect_uris": stub.uris},
                ]}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def run(args, env=None):
    """Run main() with stdout/stderr captured; returns (code, stdout, stderr).

    Every call is pinned to an empty `--env-file`, and the ambient Authentik
    settings are cleared first: the module reads the stack's `.env` by default,
    and on the machine that runs this suite that file is the live deployment, so
    a test that forgot to pin it would query the real Authentik instead of its
    stub. What the checks need is set in the environment instead.
    """
    out, err = io.StringIO(), io.StringIO()
    keys = ("AUTHENTIK_BASE_URL", "AUTHENTIK_BOOTSTRAP_TOKEN")
    saved = {key: os.environ.pop(key, None) for key in keys}
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / ".env"
        empty.write_text("", encoding="utf-8")
        pinned = list(args) + ["--env-file", str(empty)]
        for key, value in (env or {}).items():
            os.environ[key] = value
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = oidc.main(pinned)
        finally:
            for key in (env or {}):
                os.environ.pop(key, None)
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
    return code, out.getvalue(), err.getvalue()


class ParsingTests(unittest.TestCase):
    def test_reads_every_configured_provider(self):
        directory = PluginDirectory([good_provider(), good_provider(ProviderId="other")])
        self.addCleanup(directory.cleanup)
        providers = oidc.parse_providers(directory.root / "configurations" / CONFIG)
        self.assertEqual([p["providerid"] for p in providers], ["authentik", "other"])

    def test_absent_enabled_field_means_enabled(self):
        # An older config layout has no `Enabled` key at all, and the plugin
        # renders a button for it — so it must not be reported as disabled.
        providers = [{"providerid": "authentik"}]
        self.assertEqual(len(oidc.enabled_providers(providers)), 1)

    def test_disabled_provider_is_not_a_button(self):
        providers = [{"providerid": "authentik", "enabled": "False"}]
        self.assertEqual(oidc.enabled_providers(providers), [])

    def test_callback_is_built_from_the_provider_id(self):
        provider = parsed()
        self.assertEqual(
            oidc.callbacks_for(provider, ("media.innotel.us", "media.magnate.innotel.us")),
            ["https://media.innotel.us/sso/OIDC/Callback/authentik",
             "https://media.magnate.innotel.us/sso/OIDC/Callback/authentik"])

    def test_provider_without_an_id_has_no_callback(self):
        self.assertEqual(oidc.callbacks_for({"authority": "x"}, ("media.innotel.us",)), [])

    def test_env_file_ignores_comments_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MONARCH_SSO_CLIENT_ID=monarch-media\n"
                            "AUTHENTIK_BASE_URL=https://auth.example  # comment\n"
                            "QUOTED='a b'\n# skip=me\n", encoding="utf-8")
            values = oidc.env_from_file(path)
        self.assertEqual(values["MONARCH_SSO_CLIENT_ID"], "monarch-media")
        self.assertEqual(values["AUTHENTIK_BASE_URL"], "https://auth.example")
        self.assertEqual(values["QUOTED"], "a b")
        self.assertNotIn("# skip", values)


class JudgeProviderTests(unittest.TestCase):
    def test_a_cerulean_provider_passes(self):
        self.assertEqual(
            oidc.judge_provider(parsed(), CLIENT_ID, "auth.cerulean.innotel.us"), [])

    def test_a_foreign_issuer_is_a_problem(self):
        problems = oidc.judge_provider(
            parsed(Authority="https://idp.example.com/realms/myrealm"),
            CLIENT_ID, "auth.cerulean.innotel.us")
        self.assertTrue(any("idp.example.com" in p for p in problems), problems)

    def test_a_borrowed_client_is_a_problem(self):
        problems = oidc.judge_provider(parsed(ClientId="monarch"),
                                       CLIENT_ID, "auth.cerulean.innotel.us")
        self.assertTrue(any("'monarch'" in p for p in problems), problems)

    def test_a_missing_secret_is_a_problem(self):
        problems = oidc.judge_provider(parsed(ClientSecret=""),
                                       CLIENT_ID, "auth.cerulean.innotel.us")
        self.assertTrue(any("ClientSecret" in p for p in problems), problems)

    def test_a_missing_authority_is_a_problem(self):
        problems = oidc.judge_provider(parsed(Authority=""),
                                       CLIENT_ID, "auth.cerulean.innotel.us")
        self.assertTrue(any("no Authority" in p for p in problems), problems)


class CheckTests(unittest.TestCase):
    def test_wired_end_to_end_passes(self):
        directory = PluginDirectory(meta=PLUGIN_META)
        self.addCleanup(directory.cleanup)
        stub = AuthentikStub([
            "https://media.innotel.us/sso/OIDC/Callback/authentik",
            "https://media.magnate.innotel.us/sso/OIDC/Callback/authentik",
        ])
        self.addCleanup(stub.stop)
        code, out, _ = run(["--check", "--plugins-dir", str(directory.root),
                            "--authentik-base", stub.url, "--issuer-host",
                            "auth.cerulean.innotel.us"],
                           env={"AUTHENTIK_BOOTSTRAP_TOKEN": "tok"})
        self.assertEqual(code, 0, out)
        self.assertIn("offers Cerulean Authentik", out)

    def test_missing_plugin_is_a_failure_not_a_skip(self):
        directory = PluginDirectory(assembly=False, meta=PLUGIN_META)
        self.addCleanup(directory.cleanup)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root)])
        self.assertEqual(code, 1)
        self.assertIn("no SSO button", err)

    def test_unreadable_config_cannot_be_judged(self):
        directory = PluginDirectory(config=False)
        self.addCleanup(directory.cleanup)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root)])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_no_provider_is_a_failure(self):
        directory = PluginDirectory(providers=[])
        self.addCleanup(directory.cleanup)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root)])
        self.assertEqual(code, 1)
        self.assertIn("no SSO button", err)

    def test_missing_callback_is_a_failure(self):
        directory = PluginDirectory()
        self.addCleanup(directory.cleanup)
        stub = AuthentikStub([
            "https://media.innotel.us/sso/OIDC/Callback/authentik",
            # media.magnate.innotel.us was never registered: the failure that
            # reads as "the button did nothing" from the login page.
        ])
        self.addCleanup(stub.stop)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root),
                            "--authentik-base", stub.url],
                           env={"AUTHENTIK_BOOTSTRAP_TOKEN": "tok"})
        self.assertEqual(code, 1)
        self.assertIn("media.magnate.innotel.us/sso/OIDC/Callback/authentik", err)
        self.assertIn("redirect_uri does not match", err)

    def test_a_rejected_token_is_not_a_pass(self):
        directory = PluginDirectory()
        self.addCleanup(directory.cleanup)
        stub = AuthentikStub([], status=403)
        self.addCleanup(stub.stop)
        code, out, _ = run(["--check", "--plugins-dir", str(directory.root),
                            "--authentik-base", stub.url],
                           env={"AUTHENTIK_BOOTSTRAP_TOKEN": "bad"})
        # The plugin half holds, the provider half could not be read: reported as
        # not-checked rather than as a green light.
        self.assertEqual(code, 0, out)
        self.assertIn("not checked", out)

    def test_without_authentik_the_plugin_half_is_still_judged(self):
        directory = PluginDirectory()
        self.addCleanup(directory.cleanup)
        code, out, _ = run(["--check", "--plugins-dir", str(directory.root)])
        self.assertEqual(code, 0, out)
        self.assertIn("not checked", out)

    def test_a_broken_plugin_config_fails_even_without_authentik(self):
        directory = PluginDirectory([good_provider(Authority="https://idp.example.com/x")])
        self.addCleanup(directory.cleanup)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root)])
        self.assertEqual(code, 1)
        self.assertIn("idp.example.com", err)

    def test_the_client_id_can_come_from_the_environment(self):
        directory = PluginDirectory([good_provider(ClientId="monarch")])
        self.addCleanup(directory.cleanup)
        code, _, err = run(["--check", "--plugins-dir", str(directory.root),
                            "--client-id", "monarch"])
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
