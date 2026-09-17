#!/usr/bin/env python3
"""Unit tests for seerr-login-methods.py — the Seerr sign-in posture.

The case these encode: Seerr ships two sign-in methods, and only one of them is an
identity Authentik manages. A fake Seerr answers the two requests the script makes
(`GET`/`POST /api/v1/settings/main`), so the suite runs anywhere, including CI, and
asserts what matters — that `--check` fails while local sign-in is on, that
`--apply` turns it off, and that the update does **not** carry the rest of the
settings object with it (the server merges a partial body: `merge(settings.main,
req.body)`; a full-body POST is how a second field gets silently reset).

The module under test has a hyphen in its filename, so it is loaded by path.
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

SCRIPT = Path(__file__).resolve().parents[1] / "seerr-login-methods.py"


def _load():
    spec = importlib.util.spec_from_file_location("seerr_login_methods", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seerr = _load()

API_KEY = "test-api-key"


class FakeSeerr:
    """A Seerr that only knows the two calls the script makes.

    `settings` is the whole file (`{"main": ..., "jellyfin": ...}`) so the tests
    can assert that the update touched nothing outside `main` — the property the
    partial-body POST exists for.
    """

    def __init__(self, settings: dict):
        self.settings = settings
        self.requests: list[tuple[str, dict | None]] = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                return self.headers.get("X-Api-Key") == fake.settings["main"].get("apiKey")

            def do_GET(self):
                if not self._authorized():
                    return self._respond({"message": "unauthorized"}, 401)
                fake.requests.append(("GET", None))
                if self.path != "/api/v1/settings/main":
                    return self._respond({"message": "not found"}, 404)
                return self._respond(fake.settings["main"])

            def do_POST(self):
                if not self._authorized():
                    return self._respond({"message": "unauthorized"}, 401)
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", body))
                if self.path != "/api/v1/settings/main":
                    return self._respond({"message": "not found"}, 404)
                # the server's own `merge(settings.main, req.body)`
                fake.settings["main"].update(body)
                return self._respond(fake.settings["main"])

            def log_message(self, *_args):
                pass

        return Handler


class SeerrLoginMethods(unittest.TestCase):
    def run_script(self, fake: FakeSeerr, settings_path: Path, *args: str):
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = seerr.main([
                "--url", fake.url,
                "--settings", str(settings_path),
                *args,
            ])
        return code, buffer.getvalue()

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.dir.name) / "settings.json"
        self.settings = {
            "main": {
                "apiKey": API_KEY,
                "localLogin": True,
                "mediaServerLogin": True,
                "applicationUrl": "https://req.innotel.us",
                "applicationTitle": "Seerr",
            },
            "jellyfin": {"apiKey": "jellyfin-key"},
        }

    def tearDown(self) -> None:
        self.dir.cleanup()

    def write_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")

    def start(self) -> FakeSeerr:
        self.write_settings()
        return FakeSeerr(self.settings)

    def test_check_fails_while_local_sign_in_is_on(self):
        fake = self.start()
        try:
            code, out = self.run_script(fake, self.settings_path, "--check")
        finally:
            fake.stop()
        self.assertEqual(code, 1)
        self.assertIn("local sign-in is still enabled", out)
        self.assertEqual([("GET", None)], fake.requests, "a check must not write")

    def test_apply_turns_local_sign_in_off(self):
        fake = self.start()
        try:
            code, out = self.run_script(fake, self.settings_path, "--apply")
        finally:
            fake.stop()
        self.assertEqual(code, 0, out)
        self.assertFalse(fake.settings["main"]["localLogin"])
        self.assertTrue(fake.settings["main"]["mediaServerLogin"],
                        "the Jellyfin sign-in is the Cerulean identity — it stays on")

    def test_apply_sends_one_field_so_nothing_else_is_reset(self):
        fake = self.start()
        try:
            self.run_script(fake, self.settings_path, "--apply")
        finally:
            fake.stop()
        posts = [body for method, body in fake.requests if method == "POST"]
        self.assertEqual([{"localLogin": False}], posts)
        self.assertEqual("https://req.innotel.us", fake.settings["main"]["applicationUrl"])
        self.assertEqual({"apiKey": "jellyfin-key"}, fake.settings["jellyfin"],
                         "a sibling section must come through untouched")

    def test_an_already_correct_posture_passes_without_writing(self):
        self.settings["main"]["localLogin"] = False
        fake = self.start()
        try:
            code, out = self.run_script(fake, self.settings_path, "--check")
        finally:
            fake.stop()
        self.assertEqual(code, 0, out)
        self.assertIn("only the Jellyfin (Cerulean) sign-in", out)
        self.assertEqual([("GET", None)], fake.requests)

    def test_a_missing_settings_file_cannot_run(self):
        fake = self.start()
        try:
            code, out = self.run_script(fake, Path(self.dir.name) / "absent.json")
        finally:
            fake.stop()
        self.assertEqual(code, 2)
        self.assertIn("cannot read", out)

    def test_a_rejected_api_key_cannot_run(self):
        fake = self.start()
        # The file still holds the good key; the server no longer accepts it.
        fake.settings["main"]["apiKey"] = "something-else"
        try:
            code, out = self.run_script(fake, self.settings_path)
        finally:
            fake.stop()
        self.assertEqual(code, 2)
        self.assertIn("401", out)


if __name__ == "__main__":
    unittest.main()
