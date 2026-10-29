"""Jellyfin notification: the two calls, their headers, and their failures."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import jellyfin


class Recorder(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict, dict]] = []
    status = 204

    def log_message(self, *args):  # keep pytest output readable
        pass

    def _record(self, body: bytes = b"") -> None:
        self.calls.append((
            self.path,
            {key.lower(): value for key, value in self.headers.items()},
            json.loads(body) if body else {},
        ))
        if self.path == "/System/Info":
            payload = json.dumps({"ServerName": "jellyfin-test", "Version": "10.9.0"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(self.status)
        self.end_headers()

    def do_GET(self):
        self._record()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(length))


@pytest.fixture()
def server():
    Recorder.calls = []
    Recorder.status = 204
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_configured_info_reads_the_server_identity(server):
    client = jellyfin.Jellyfin(server, "token-123")
    assert client.configured is True
    info = client.info()
    assert info["reachable"] is True
    assert info["server"] == "jellyfin-test"
    assert info["version"] == "10.9.0"
    assert Recorder.calls[-1][1]["x-emby-token"] == "token-123"


def test_refresh_reports_paths_then_rescans(server, tmp_path):
    video = tmp_path / "movies" / "Film (2020)" / "Film.2020.1080p.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")

    client = jellyfin.Jellyfin(server, "token-123")
    report = client.refresh([video])

    assert report == {"configured": True, "updated": 1, "refreshed": True}
    paths = [call[0] for call in Recorder.calls]
    assert paths == ["/Library/Media/Updated", "/Library/Refresh"]
    assert Recorder.calls[0][2] == {"Updates": [{"Path": str(video.parent), "UpdateType": "Created"}]}
    assert Recorder.calls[0][1]["x-emby-token"] == "token-123"


def test_a_refused_update_never_raises(server, tmp_path):
    Recorder.status = 401
    client = jellyfin.Jellyfin(server, "bad")
    report = client.refresh([tmp_path / "x.mkv"])
    assert report["updated"] == 0
    assert report["refreshed"] is False


def test_unreachable_and_unconfigured_are_answers_not_exceptions():
    dead = jellyfin.Jellyfin("http://127.0.0.1:9", "token")
    info = dead.info()
    assert info["configured"] is True and info["reachable"] is False
    assert dead.refresh([Path("/nope")])["refreshed"] is False

    unset = jellyfin.Jellyfin("", "")
    assert unset.configured is False
    assert unset.info() == {"configured": False, "reachable": False}
    assert unset.refresh([Path("/nope")]) == {"configured": False, "updated": 0, "refreshed": False}
