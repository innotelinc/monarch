#!/usr/bin/env python3
"""Tests for scripts/vault-env.py — the estate's Python `vault://` resolver.

The contract under test (mirrored from Zeus's vault-env.mjs):
  * `KEY=vault://mount/path#key` resolves against KV v2 and rewrites in place
  * plain values, comments and blank lines pass through untouched
  * a leftover `infisical://` value is refused, never passed through
  * an unresolvable reference aborts (fail fast), and says which key failed
    without echoing other secrets
  * `--out` writes a 0600 file; stdout mode never writes

Vault is faked with a local HTTP server so the URL/token plumbing is real.
"""

import http.server
import os
import stat
import sys
import tempfile
import threading
import unittest

import importlib.util  # noqa: E402

_VAULT_ENV_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vault-env.py")
)
_spec = importlib.util.spec_from_file_location("vault_env", _VAULT_ENV_PATH)
vault_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vault_env)

SECRETS = {
    "AUTHENTIK_CLIENT_SECRET": "s3cr3t-value",
    "NPM_PASSWORD": "pass'with'quotes",
}


class FakeVault(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/secret/data/monarch"):
            body = {"data": {"data": SECRETS}}
            self.send_response(200)
        elif self.path.startswith("/v1/secret/data/missing"):
            body = {"data": {"data": {}}}
            self.send_response(200)
        else:
            body = {}
            self.send_response(404)
        payload = __import__("json").dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


class VaultEnvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), FakeVault)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        os.environ["VAULT_ADDR"] = f"http://127.0.0.1:{self.port}"
        os.environ["VAULT_TOKEN"] = "test-token"
        os.environ.pop("VAULT_TOKEN_FILE", None)
        os.environ.pop("VAULT_SKIP_VERIFY", None)

    def env(self, content):
        path = os.path.join(self.tmp.name, f"env-{id(content)}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def run_resolver(self, path, *extra):
        return vault_env.resolve_file(path, os.environ["VAULT_ADDR"], "test-token", None, True)

    def test_reference_resolves_in_place(self):
        lines, count = self.run_resolver(self.env(
            "AUTHENTIK_CLIENT_SECRET=vault://secret/monarch#AUTHENTIK_CLIENT_SECRET\n"
        ))
        self.assertEqual(count, 1)
        self.assertEqual(lines, ["AUTHENTIK_CLIENT_SECRET='s3cr3t-value'"])

    def test_plain_values_pass_through(self):
        source = "FOO=bar\n# comment\n\nNPM_PASSWORD=plain-pass\n"
        lines, count = self.run_resolver(self.env(source))
        self.assertEqual(count, 0)
        self.assertEqual(lines, source.splitlines())

    def test_single_quotes_in_secret_are_escaped(self):
        lines, _ = self.run_resolver(self.env("NPM_PASSWORD=vault://secret/monarch#NPM_PASSWORD\n"))
        self.assertEqual(lines[0], "NPM_PASSWORD='pass''with''quotes'")

    def test_infisical_reference_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            vault_env.parse_reference("infisical://project/123#KEY")
        self.assertIn("retired", str(ctx.exception))

    def test_malformed_reference_names_the_problem(self):
        for bad in ("vault://nokey#", "vault://#key", "vault://onlymount#key", "vault://mount/path"):
            with self.assertRaises(SystemExit):
                vault_env.parse_reference(bad)

    def test_unresolvable_key_fails_and_names_it(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_resolver(self.env("MISSING_KEY=vault://secret/missing#NOPE\n"))
        self.assertIn("'NOPE'", str(ctx.exception))

    def test_unreachable_vault_fails_fast(self):
        os.environ["VAULT_ADDR"] = "http://127.0.0.1:1"
        with self.assertRaises(SystemExit) as ctx:
            self.run_resolver(self.env("K=vault://secret/monarch#AUTHENTIK_CLIENT_SECRET\n"))
        self.assertIn("cannot reach Vault", str(ctx.exception))

    def test_out_writes_0600(self):
        out = os.path.join(self.tmp.name, "resolved.env")
        result = vault_env.main.__wrapped__ if hasattr(vault_env.main, "__wrapped__") else None
        # Exercise through the real entry point.
        sys.argv = ["vault-env.py", self.env(
            "AUTHENTIK_CLIENT_SECRET=vault://secret/monarch#AUTHENTIK_CLIENT_SECRET\n"
        ), "--out", out]
        self.assertEqual(vault_env.main(), 0)
        mode = stat.S_IMODE(os.stat(out).st_mode)
        self.assertEqual(mode, 0o600)
        with open(out, encoding="utf-8") as fh:
            self.assertIn("s3cr3t-value", fh.read())

    def test_missing_token_is_a_config_error(self):
        os.environ.pop("VAULT_TOKEN", None)
        with self.assertRaises(SystemExit) as ctx:
            vault_env.vault_env()
        self.assertIn("VAULT_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
