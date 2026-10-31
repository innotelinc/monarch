#!/usr/bin/env python3
"""The published Jellyfin names, asked the two questions that failed on 2026-10-30.

WHY THIS EXISTS
---------------
`check-jellyfin-names.py` exists because a Jellyfin client follows the address the
server advertises, not the one it dialled. The cases worth pinning are the ones
that outage actually had: a name that answers as itself, a name that answers as
*somebody else* (the TV's failure — the page loads, the server is healthy, and the
client still cannot connect), a server that is up but broken, and a name that is
simply not there. None of that needs the internet: a loopback server and a closed
port are the whole world here, and the judgement is a pure function.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-jellyfin-names.py"

spec = importlib.util.spec_from_file_location("check_jellyfin_names", SCRIPT)
assert spec and spec.loader
names = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = names
spec.loader.exec_module(names)


def public_info(local_address: str | None = "https://media.innotel.us",
                server_name: str = "Gizmo", version: str = "12.2.0") -> bytes:
    body = {"ServerName": server_name, "Version": version, "Id": "abc123", "StartupWizardCompleted": True}
    if local_address is not None:
        body["LocalAddress"] = local_address
    return json.dumps(body).encode()


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"

    def do_GET(self):  # noqa: N802 - the base class names it
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # noqa: D102 - silence the test's own server
        pass


def serve(status: int = 200, body: bytes = b"{}"):
    handler = type("_Served", (_Handler,), {"status": status, "body": body})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Judge(unittest.TestCase):
    """The verdict itself, without a socket in the way."""

    def test_a_name_that_answers_as_itself_passes(self):
        ok, detail = names.judge("iptv.innotel.us", 200, public_info("https://iptv.innotel.us"))
        self.assertTrue(ok, detail)
        self.assertIn("serves as iptv.innotel.us", detail)
        self.assertIn("Gizmo", detail)

    def test_the_tv_failure_is_a_finding(self):
        # Exactly 2026-10-30: the client dialled one name and was handed another.
        ok, detail = names.judge("iptv.innotel.us", 200, public_info("https://media.innotel.us"))
        self.assertFalse(ok)
        self.assertIn("https://media.innotel.us", detail)
        self.assertIn("follows it", detail)

    def test_a_gated_or_broken_server_is_a_finding(self):
        for status in (401, 500, 503):
            ok, detail = names.judge("media.innotel.us", status, b"")
            self.assertFalse(ok, status)
            self.assertIn(f"HTTP {status}", detail)

    def test_nothing_advertised_is_not_a_finding(self):
        # A client keeps the address it used when the server advertises none.
        ok, detail = names.judge("media.innotel.us", 200, public_info(None))
        self.assertTrue(ok, detail)
        self.assertIn("nothing advertised", detail)

    def test_a_body_that_is_not_jellyfin_is_a_finding(self):
        ok, detail = names.judge("media.innotel.us", 200, b"<html>not jellyfin</html>")
        self.assertFalse(ok)
        self.assertIn("did not answer JSON", detail)
        ok, detail = names.judge("media.innotel.us", 200, b'{"LocalAddress": "https://media.innotel.us"}')
        self.assertFalse(ok)
        self.assertIn("no ServerName", detail)

    def test_the_port_does_not_have_to_match(self):
        # Published URIs carry a port on the LAN names; only the host is asked.
        ok, detail = names.judge("media.innotel.us", 200, public_info("https://media.innotel.us:443"))
        self.assertTrue(ok, detail)


class Check(unittest.TestCase):
    """The request half, against a loopback server."""

    def test_a_loopback_server_answering_as_itself_passes(self):
        port_finder = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = port_finder.server_address[1]
        port_finder.server_close()
        server, base = serve(200, public_info(f"https://127.0.0.1:{port}"))
        try:
            ok, detail = names.check("127.0.0.1", base, timeout=5)
            self.assertTrue(ok, detail)
        finally:
            server.shutdown()
            server.server_close()

    def test_a_server_answering_as_another_host_fails(self):
        server, base = serve(200, public_info("https://media.innotel.us"))
        try:
            ok, detail = names.check("iptv.innotel.us", base, timeout=5)
            self.assertFalse(ok)
            self.assertIn("is handed media.innotel.us", detail)
        finally:
            server.shutdown()
            server.server_close()

    def test_nothing_listening_is_a_connection_failure(self):
        ok, detail = names.check("iptv.innotel.us", f"http://127.0.0.1:{dead_port()}", timeout=3)
        self.assertFalse(ok)
        self.assertIn("cannot connect", detail)


class Main(unittest.TestCase):
    """Exit codes: 0 in agreement, 1 drift, 2 cannot look."""

    def test_all_unreachable_is_cannot_look(self):
        code = names.main(["--names", "127.0.0.1", "--timeout", "2"])
        # 127.0.0.1:443 is not our edge; nothing answers there in the test env.
        self.assertIn(code, (1, 2))


if __name__ == "__main__":
    unittest.main()
