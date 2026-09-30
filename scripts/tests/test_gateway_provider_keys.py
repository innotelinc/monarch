#!/usr/bin/env python3
"""Unit tests for gateway-provider-keys.py — the check on the model gateway's keys.

The estate runs one OmniRoute for every product and nothing in the media stack
*needs* it, which is how a refused key went unnoticed for hours while the
symptom appeared somewhere else as "every model in the chain failed". This is
the check that watches the gateway's own idea of its credentials, so what is
pinned here is what it is willing to call a rejected key:

  * **401 and 403 are the finding** — in either shape the gateway reports a
    status (`401`, and `"401.0"`, because the column is a float behind the API),
    and by `lastErrorType` when it fills only that in;
  * **429, 402, 404 and 503 are not** — a free-tier gateway is rate-limited and
    out of credit most of the time, and a check that fires on those is one
    nobody reads by the second day. This is the distinction the whole file
    exists for, so it is asserted rather than assumed;
  * **a connection somebody switched off is not a fault** — turning a provider
    off is the documented answer for one that is out of credit, so judging it
    would make the fix itself raise the alarm;
  * **the token is sent when it is set, and omitted when it is not** — it is
    optional on this estate (OmniRoute's `REQUIRE_API_KEY` gates `/v1/*`, and
    the connection list answers identically without one), so both halves are
    asserted against a real socket rather than left to a comment.

A fake gateway captures the header, so the exit-code contract (0 clean, 1
refused, 2 unreadable) is exercised the way `verify-ldap.py`'s is.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gateway-provider-keys.py"


def _load():
    spec = importlib.util.spec_from_file_location("gateway_provider_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gw = _load()


def connection(**overrides):
    """One row shaped like the endpoint's, with only the fields a test cares about."""
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "provider": "openrouter",
        "name": "main",
        "isActive": True,
        "testStatus": "active",
        "errorCode": None,
        "lastError": None,
        "lastErrorType": None,
    }
    row.update(overrides)
    return row


def write_fixture(rows) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"connections": list(rows)}, handle)
    handle.close()
    return handle.name


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = gw.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ClassifyTest(unittest.TestCase):
    """Which rows count as a rejected key, and which are merely a busy gateway."""

    def test_401_is_a_rejection(self):
        code, out, err = run("--check", "--from-file", write_fixture([connection(errorCode="401")]))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED openrouter/main", out)
        self.assertIn("HTTP 401", out)

    def test_403_is_a_rejection(self):
        code, out, _ = run("--check", "--from-file", write_fixture([connection(errorCode=403)]))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", out)

    def test_a_float_encoded_status_is_still_read(self):
        """`errorCode` arrives as `"401.0"`: the column is a float behind the API."""
        code, out, _ = run("--check", "--from-file", write_fixture([connection(errorCode="401.0")]))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", out)

    def test_an_auth_error_type_alone_is_enough(self):
        """A gateway that fills in only `lastErrorType` is still reporting the refusal."""
        row = connection(errorCode=None, lastErrorType="invalid_key")
        code, out, _ = run("--check", "--from-file", write_fixture([row]))
        self.assertEqual(code, 1)
        self.assertIn("invalid_key", out)

    def test_quota_and_upstream_errors_are_not_rejections(self):
        """The noise a free-tier gateway makes all day, and the reason this check is narrow."""
        rows = [
            connection(provider="gemini", errorCode="429.0", lastErrorType="rate_limited"),
            connection(provider="meta-muse", errorCode=402, lastErrorType="quota_exhausted"),
            connection(provider="upstream", errorCode=503, lastErrorType="server_error"),
            connection(provider="gone", errorCode=404, lastErrorType="model_not_found"),
        ]
        code, out, err = run("--check", "--from-file", write_fixture(rows))
        self.assertEqual(code, 0)
        self.assertIn("4 active connection(s), every key accepted", out)
        self.assertNotIn("REJECTED", out)

    def test_a_switched_off_connection_is_not_judged(self):
        """Switching a provider off is the documented fix, so it must not raise the alarm."""
        rows = [
            connection(provider="meta-muse", isActive=False, errorCode=401),
            connection(provider="openrouter"),
        ]
        code, out, _ = run("--check", "--from-file", write_fixture(rows))
        self.assertEqual(code, 0)
        self.assertIn("1 active connection(s)", out)
        self.assertNotIn("REJECTED", out)

    def test_a_missing_flag_means_active(self):
        """An older gateway that does not publish `isActive` must not make the check a no-op."""
        row = connection(errorCode=401)
        del row["isActive"]
        code, out, _ = run("--check", "--from-file", write_fixture([row]))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)

    def test_many_rejections_are_all_reported_and_counted(self):
        rows = [
            connection(provider="a", errorCode=401),
            connection(provider="b", errorCode=403),
            connection(provider="c"),
        ]
        code, out, err = run("--check", "--from-file", write_fixture(rows))
        self.assertEqual(code, 1)
        self.assertEqual(out.count("REJECTED"), 2)
        self.assertIn("2 of 3 active connection(s)", err)


class UnreadableTest(unittest.TestCase):
    """The `2` in the exit-code contract: nothing was read, so nothing is claimed."""

    def test_no_url_is_not_a_pass(self):
        code, _, err = run("--check")
        self.assertEqual(code, 2)
        self.assertIn("DRIFT_GATEWAY_URL", err)

    def test_malformed_json_is_a_two(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write("not json at all")
        handle.close()
        code, _, err = run("--check", "--from-file", handle.name)
        self.assertEqual(code, 2)
        self.assertIn("could not read the connection list", err)

    def test_a_payload_without_connections_is_a_two(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"unexpected": True}, handle)
        handle.close()
        code, _, err = run("--check", "--from-file", handle.name)
        self.assertEqual(code, 2)
        self.assertIn("connections", err)

    def test_an_unreachable_gateway_is_a_two(self):
        """A closed port, so the failure is the network rather than the gateway's answer."""
        code, _, err = run("--check", "--url", "http://127.0.0.1:1", "--timeout", "2")
        self.assertEqual(code, 2)
        self.assertIn("could not read the connection list", err)


class BearerTest(unittest.TestCase):
    """The token is optional here, so both halves are asserted."""

    def setUp(self):
        self.seen: list[str | None] = []

        def handler_factory(payload):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
                    self.server.seen.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            return Handler

        self.payload = {"connections": [connection(errorCode=401)]}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.payload))
        self.server.seen = self.seen  # type: ignore[attr-defined]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def test_the_token_is_sent_as_a_bearer(self):
        code, out, _ = run("--check", "--url", self.url, "--token", "sk-test-key")
        self.assertEqual(code, 1)
        self.assertIn("REJECTED openrouter/main", out)
        self.assertEqual(self.seen, ["Bearer sk-test-key"])

    def test_without_a_token_the_request_goes_plain(self):
        """Still asked, because the endpoint answers the same either way here."""
        code, _, _ = run("--check", "--url", self.url)
        self.assertEqual(code, 1)
        self.assertEqual(self.seen, [None])

    def test_the_env_var_is_the_default_source(self):
        """`drift-check` is driven from `.env`, so the variables have to work."""
        import os

        os.environ["DRIFT_GATEWAY_URL"] = self.url
        os.environ["DRIFT_GATEWAY_TOKEN"] = "sk-from-env"
        self.addCleanup(os.environ.pop, "DRIFT_GATEWAY_URL", None)
        self.addCleanup(os.environ.pop, "DRIFT_GATEWAY_TOKEN", None)

        code, _, _ = run("--check")
        self.assertEqual(code, 1)
        self.assertEqual(self.seen, ["Bearer sk-from-env"])


if __name__ == "__main__":
    unittest.main()
