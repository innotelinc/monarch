#!/usr/bin/env python3
"""The board's tiles, asked whether they answer — offline, on loopback.

WHY THIS EXISTS
---------------
`check-board-tiles.py` is the only thing in the estate that clicks the dashboard's
links, so the cases that matter are the ones a person would insist on: a tile that
answers, a tile that is gated (401 is a working link behind sign-in, which most of
this board's tiles are), a tile that is up but broken (5xx), and a tile whose
service is simply not there. None of that needs the internet: two loopback servers
and a closed port are the whole board, and the database is a temp file.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-board-tiles.py"

spec = importlib.util.spec_from_file_location("check_board_tiles", SCRIPT)
assert spec and spec.loader
board = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = board
spec.loader.exec_module(board)


class _Handler(BaseHTTPRequestHandler):
    """One answer, on loopback: whatever the subclass says."""

    status = 200
    headers: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 - the base class names it
        self.send_response(self.status)
        for key, value in self.headers_extra().items():
            self.send_header(key, value)
        body = b"ok"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def headers_extra(self) -> dict[str, str]:
        return {}

    def log_message(self, *args):  # noqa: D102 - silence the test's own server
        pass


def serve(status: int = 200, headers: dict[str, str] | None = None):
    """A loopback server answering `status`, and its base URL."""
    handler = type("_Served", (_Handler,), {
        "status": status,
        "headers_extra": lambda self: headers or {},
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def dead_port() -> int:
    """A port nothing listens on, bound once so the OS picks a free one."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_board(path: Path, rows: list[tuple[str, str]], ping: str = "") -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE app (id TEXT PRIMARY KEY, name TEXT, description TEXT, "
                       "icon_url TEXT, href TEXT, ping_url TEXT)")
    for index, (name, href) in enumerate(rows):
        connection.execute("INSERT INTO app (id, name, href, ping_url) VALUES (?,?,?,?)",
                           (f"app{index}", name, href, ping or href))
    connection.commit()
    connection.close()
    return path


def run(*args: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = board.main(list(args))
    return code, out.getvalue(), err.getvalue()


class BoardTiles(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="board-tiles-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.servers: list[ThreadingHTTPServer] = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()

    def server(self, status: int = 200, headers: dict[str, str] | None = None) -> str:
        server, url = serve(status, headers)
        self.servers.append(server)
        return url

    def test_every_tile_answering_is_a_clean_run(self):
        db = make_board(self.tmp / "ok.sqlite", [("Jellyfin", self.server())])
        code, out, _ = run(str(db))
        self.assertEqual(code, 0, out)
        self.assertIn("all 1 tile(s) answer", out)

    def test_a_gated_tile_is_alive(self):
        # Most of this board is behind one oauth2-proxy, so 401/403 is the normal
        # answer from a working link — reporting those as dead would make the check
        # useless on the first run.
        for status in (401, 403):
            url = self.server(status)
            alive, detail = board.probe(url, 5)
            self.assertTrue(alive, detail)
            self.assertIn("gated, but answering", detail)
            db = make_board(self.tmp / f"gated{status}.sqlite", [("Jellyseerr", url)])
            code, out, _ = run(str(db))
            self.assertEqual(code, 0, out)
            self.assertNotIn("FAIL", out)

    def test_a_tile_the_app_itself_fails_is_a_finding(self):
        db = make_board(self.tmp / "broken.sqlite", [("Radarr", self.server(502))])
        code, out, _ = run(str(db))
        self.assertEqual(code, 1, out)
        self.assertIn("Radarr", out)
        self.assertIn("HTTP 502", out)

    def test_a_tile_whose_service_is_gone_is_a_finding(self):
        # The shape the board actually shipped with: a hostname that resolves to
        # nothing, or a port with no listener behind it.
        href = f"http://127.0.0.1:{dead_port()}/"
        db = make_board(self.tmp / "dead.sqlite", [("Onyx", href)])
        code, out, _ = run(str(db))
        self.assertEqual(code, 1, out)
        self.assertIn("Onyx", out)
        self.assertIn("cannot connect", out)

    def test_one_dead_tile_does_not_hide_the_others(self):
        db = make_board(self.tmp / "mixed.sqlite", [
            ("Jellyfin", self.server()),
            ("Onyx", f"http://127.0.0.1:{dead_port()}/"),
            ("Sonarr", self.server()),
        ])
        code, out, _ = run(str(db))
        self.assertEqual(code, 1, out)
        self.assertIn("1 of 3 tile(s)", out)

    def test_one_url_probed_once_and_still_reported_per_tile(self):
        # Two tiles can point at one service (apex and a named host); the probe is
        # not the thing being counted, the tiles are.
        url = self.server()
        db = make_board(self.tmp / "dup.sqlite", [("Monarch", url), ("Monarch (again)", url)])
        code, out, _ = run(str(db))
        self.assertEqual(code, 0, out)
        self.assertIn("all 2 tile(s) answer", out)
        self.assertIn("1 distinct URL(s)", out)

    def test_a_tile_with_no_href_says_so(self):
        db = make_board(self.tmp / "empty.sqlite", [("Placeholder", "")])
        code, out, _ = run(str(db))
        self.assertEqual(code, 1, out)
        self.assertIn("no href", out)

    def test_no_database_is_cannot_look(self):
        code, _, err = run(str(self.tmp / "absent.sqlite"))
        self.assertEqual(code, 2)
        self.assertIn("no Homarr database", err)

    def test_a_database_with_no_board_in_it_is_cannot_look(self):
        # An empty Homarr database is not a drifted board: it has not been set up.
        db = make_board(self.tmp / "blank.sqlite", [])
        code, _, err = run(str(db))
        self.assertEqual(code, 2)
        self.assertIn("no app rows", err)


if __name__ == "__main__":
    unittest.main()
