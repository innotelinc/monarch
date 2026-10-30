#!/usr/bin/env python3
"""Tests for scripts/livetv-monitor.py.

The monitor's decisions are what matter: a probe must not call an HTML error
page alive, a single timeout must not mark a channel dead, the first run must be
a baseline, a source change must be noticed, and a dead feed must only be
replaced when the same guide channel has a live sibling. The HTTP tests use a
local server, so they do not depend on any upstream provider.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "livetv-monitor.py"
spec = importlib.util.spec_from_file_location("livetv_monitor", SCRIPT)
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
# dataclasses resolve their module through sys.modules while the class is built.
sys.modules[spec.name] = monitor
spec.loader.exec_module(monitor)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path == "/good.m3u8":
            body, kind = (b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nseg.ts\n",
                          "application/vnd.apple.mpegurl")
        elif self.path == "/media.m3u8":
            body, kind = (b"#EXTM3U\n#EXTINF:1,\nseg.ts\n",
                          "application/vnd.apple.mpegurl")
        elif self.path == "/seg.ts":
            body, kind = b"G" * 32, "video/mp2t"
        elif self.path == "/html":
            body, kind = b"<html>not a stream</html>", "text/html"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the test output clean
        pass


class Probing(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_playlist_is_alive(self) -> None:
        result = monitor.probe(f"{self.base}/good.m3u8")
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "HLS playlist")

    def test_a_deep_probe_follows_to_a_segment(self) -> None:
        result = monitor.probe(f"{self.base}/good.m3u8", deep=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "HLS segment")

    def test_a_media_playlist_without_a_master_is_alive(self) -> None:
        result = monitor.probe(f"{self.base}/media.m3u8", deep=True)
        self.assertTrue(result.ok)

    def test_a_404_is_dead(self) -> None:
        result = monitor.probe(f"{self.base}/missing.m3u8")
        self.assertFalse(result.ok)
        self.assertEqual(result.http_status, 404)

    def test_an_html_error_page_is_not_a_playlist(self) -> None:
        result = monitor.probe(f"{self.base}/html")
        self.assertFalse(result.ok)
        self.assertIn("not an HLS playlist", result.detail)


def probe_result(ok: bool, detail: str = "") -> monitor.ProbeResult:
    return monitor.ProbeResult(ok, 200 if ok else 404, detail or ("ok" if ok else "fail"), 5)


def channel(number: str, url: str, ok: bool, dial: bool = True) -> monitor.ChannelResult:
    return monitor.ChannelResult(number, f"Channel {number}", url, "Test.us@East",
                                 probe_result(ok), dial=dial)


class State(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "monitor.sqlite"
        self.conn = monitor.open_db(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_a_feed_must_fail_twice_before_it_is_dead(self) -> None:
        # First failure: baseline/unknown, not a transition.
        transitions, ok, failed = monitor.record_probes(
            self.conn, [channel("35", "http://a", False)], "2026-01-01T00:00:00+00:00")
        self.assertEqual((ok, failed), (0, 1))
        self.assertEqual(transitions, [])
        row = self.conn.execute("SELECT status, fail_streak FROM channels WHERE number='35'").fetchone()
        self.assertEqual((row["status"], row["fail_streak"]), ("unknown", 1))

        # Second failure: dead, and that is the transition that fails over.
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("35", "http://a", False)], "2026-01-01T00:05:00+00:00")
        self.assertEqual([t["kind"] for t in transitions], ["lost"])
        row = self.conn.execute("SELECT status, fail_streak FROM channels WHERE number='35'").fetchone()
        self.assertEqual((row["status"], row["fail_streak"]), ("fail", 2))

        # A third failure is not a new event.
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("35", "http://a", False)], "2026-01-01T00:10:00+00:00")
        self.assertEqual(transitions, [])

        # Recovery is one transition and resets the streak.
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("35", "http://a", True)], "2026-01-01T00:15:00+00:00")
        self.assertEqual([t["kind"] for t in transitions], ["recovered"])
        row = self.conn.execute("SELECT status, fail_streak FROM channels WHERE number='35'").fetchone()
        self.assertEqual((row["status"], row["fail_streak"]), ("ok", 0))

    def test_a_new_url_on_the_same_number_starts_a_new_baseline(self) -> None:
        for at in ("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"):
            monitor.record_probes(self.conn, [channel("35", "http://old", False)], at)
        row = self.conn.execute("SELECT status FROM channels WHERE number='35'").fetchone()
        self.assertEqual(row["status"], "fail")
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("35", "http://new", True)], "2026-01-01T00:10:00+00:00")
        self.assertEqual(transitions, [])
        row = self.conn.execute("SELECT status, url FROM channels WHERE number='35'").fetchone()
        self.assertEqual((row["status"], row["url"]), ("ok", "http://new"))

    def test_a_watchlist_recovery_is_marked_as_off_the_dial(self) -> None:
        for at in ("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"):
            monitor.record_probes(self.conn, [channel("835.2", "http://a", False, dial=False)], at)
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("835.2", "http://a", True, dial=False)],
            "2026-01-01T00:10:00+00:00")
        self.assertEqual([t["kind"] for t in transitions], ["recovered"])
        self.assertFalse(transitions[0]["dial"])

    def test_the_first_run_is_a_baseline_for_a_dead_feed(self) -> None:
        transitions, _ok, _failed = monitor.record_probes(
            self.conn, [channel("42", "http://dead", False)], "2026-01-01T00:00:00+00:00")
        self.assertEqual(transitions, [])
        self.assertEqual(
            self.conn.execute("SELECT status FROM channels WHERE number='42'").fetchone()["status"],
            "unknown")

    def test_source_fingerprint_changes_only_when_the_file_does(self) -> None:
        source = Path(self.tmp.name) / "source.m3u"
        source.write_text("#EXTM3U\n", encoding="utf-8")
        self.assertEqual(monitor.record_sources(self.conn, [("test", str(source))],
                                                "2026-01-01T00:00:00+00:00"), [])
        self.assertEqual(monitor.record_sources(self.conn, [("test", str(source))],
                                                "2026-01-01T00:05:00+00:00"), [])
        source.write_text("#EXTM3U\n#EXTINF:-1,new\nhttp://new\n", encoding="utf-8")
        changes = monitor.record_sources(self.conn, [("test", str(source))],
                                         "2026-01-01T00:10:00+00:00")
        self.assertEqual([change["name"] for change in changes], ["test"])
        row = self.conn.execute("SELECT * FROM sources WHERE name='test'").fetchone()
        self.assertIsNotNone(row["changed_at"])

    def test_a_source_that_cannot_be_fetched_is_not_a_change(self) -> None:
        self.assertEqual(monitor.record_sources(
            self.conn, [("missing", str(Path(self.tmp.name) / "nope"))],
            "2026-01-01T00:00:00+00:00"), [])
        row = self.conn.execute("SELECT * FROM sources WHERE name='missing'").fetchone()
        self.assertIsNone(row["fingerprint"])
        self.assertIn("No such file", row["error"])

    def test_status_file_is_atomic_and_readable(self) -> None:
        path = Path(self.tmp.name) / "status.json"
        monitor.write_status(path, {"rows": 3, "down": []})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rows"], 3)

    def test_an_old_database_gains_the_watchlist_header_columns(self) -> None:
        old = Path(self.tmp.name) / "old.sqlite"
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE channels (number TEXT PRIMARY KEY, name TEXT, url TEXT, "
            "tvg_id TEXT, status TEXT, probe TEXT, http_status INTEGER, detail TEXT, "
            "latency_ms INTEGER, checked_at TEXT, last_ok TEXT, last_fail TEXT, "
            "ok_count INTEGER, fail_count INTEGER, fail_streak INTEGER)")
        conn.commit()
        conn.close()
        migrated = monitor.open_db(old)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(channels)")}
        self.assertIn("user_agent", columns)
        self.assertIn("referrer", columns)
        migrated.close()


class Failover(unittest.TestCase):
    def test_upstream_id_strips_the_dial_number(self) -> None:
        self.assertEqual(monitor.upstream_id("CNN.us@SD.42", "42"), "CNN.us@SD")
        self.assertEqual(monitor.upstream_id("USANetwork.us@East.835.1", "835.1"),
                         "USANetwork.us@East")

    def test_a_dead_row_beside_a_live_one_needs_a_rebuild(self) -> None:
        rows = [
            {"number": "35", "tvg_id": "USANetwork.us@East.35", "url": "http://primary"},
            {"number": "835", "tvg_id": "USANetwork.us@East.835", "url": "http://backup"},
        ]
        self.assertTrue(monitor.failover_needed(rows, {"http://primary": "fail",
                                                       "http://backup": "ok"}))

    def test_all_dead_rows_do_not_need_a_rebuild(self) -> None:
        rows = [
            {"number": "35", "tvg_id": "USANetwork.us@East.35", "url": "http://primary"},
            {"number": "835", "tvg_id": "USANetwork.us@East.835", "url": "http://backup"},
        ]
        self.assertFalse(monitor.failover_needed(rows, {"http://primary": "fail",
                                                        "http://backup": "fail"}))

    def test_a_live_watchlist_feed_is_promoted_to_a_dead_dial_row(self) -> None:
        rows = [
            {"number": "35", "tvg_id": "USANetwork.us@East.35", "url": "http://primary",
             "dial": True},
            {"number": "835.1", "tvg_id": "USANetwork.us@East.835.1", "url": "http://backup",
             "dial": False},
        ]
        self.assertTrue(monitor.failover_needed(rows, {"http://primary": "fail",
                                                       "http://backup": "ok"}))

    def test_a_dead_watchlist_feed_does_not_force_a_rebuild(self) -> None:
        # This is the normal post-failover state: the dead feed is off the dial
        # and the live one is serving the number.
        rows = [
            {"number": "35", "tvg_id": "USANetwork.us@East.35", "url": "http://primary",
             "dial": True},
            {"number": "835.2", "tvg_id": "USANetwork.us@East.835.2", "url": "http://dead",
             "dial": False},
        ]
        self.assertFalse(monitor.failover_needed(rows, {"http://primary": "ok",
                                                        "http://dead": "fail"}))


class EndToEnd(unittest.TestCase):
    def test_one_run_records_a_channel_and_writes_status(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                playlist = root / "dial.m3u"
                playlist.write_text(
                    "#EXTM3U\n"
                    "#EXTINF:-1 tvg-id=\"Test.us@East\" tvg-chno=\"5\" "
                    "tvg-name=\"Test\",Test\n"
                    f"http://127.0.0.1:{server.server_port}/good.m3u8\n",
                    encoding="utf-8")
                db = root / "monitor.sqlite"
                argv = ["livetv-monitor.py", "--playlist", str(playlist),
                        "--db", str(db), "--no-rebuild", "--json"]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(monitor, "default_sources", return_value=[]), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(monitor.main(), 0)
                summary = json.loads(output.getvalue())
                self.assertEqual((summary["rows"], summary["ok"], summary["failed"]),
                                 (1, 1, 0))
                with contextlib.closing(sqlite3.connect(db)) as check:
                    row = check.execute(
                        "SELECT status FROM channels WHERE number='5'").fetchone()
                self.assertEqual(row[0], "ok")
                status = root / "monitor.json"
                self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["rows"], 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
