"""Subtitles: the identity helpers, the file contract and one full fetch."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app import db, subtitles
from app.settings import settings


def make_video(path: Path, size: int = 200_000) -> Path:
    """A file with real bytes, so the hash and the size are meaningful."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytes((index * 7) % 251 for index in range(65536))
    tail = bytes((index * 11) % 253 for index in range(65536))
    filler = b"\0" * (size - 131072)
    path.write_bytes(payload + filler + tail)
    return path


def expected_hash(path: Path) -> str:
    size = path.stat().st_size
    raw = path.read_bytes()
    total = size
    for offset in range(0, 65536, 8):
        total += struct.unpack("<Q", raw[offset:offset + 8])[0]
        total += struct.unpack("<Q", raw[size - 65536 + offset:size - 65536 + offset + 8])[0]
    return f"{total & 0xFFFFFFFFFFFFFFFF:016x}"


def test_moviehash_matches_the_opensubtitles_algorithm(tmp_path):
    video = make_video(tmp_path / "Film.2020.1080p.mkv")
    assert subtitles.moviehash(video) == expected_hash(video)
    assert len(subtitles.moviehash(video)) == 16
    # Too small to hash: no hash rather than a wrong one.
    small = tmp_path / "tiny.mkv"
    small.write_bytes(b"x" * 100)
    assert subtitles.moviehash(small) == ""


def test_languages_in_reads_only_trailing_tokens():
    assert subtitles.languages_in("Film.2020.1080p.WEB-DL.en.srt") == ["en"]
    assert subtitles.languages_in("Show.S01E02.eng.srt") == ["en"]
    assert subtitles.languages_in("The English Patient.srt") == []
    assert subtitles.languages_in("Film.es-ES.srt") == ["es-es"]
    assert subtitles.languages_in("Film.2020.mkv") == []


def test_subtitle_path_and_existing_siblings(tmp_path):
    video = make_video(tmp_path / "Film (2020)" / "Film.2020.1080p.mkv")
    assert subtitles.subtitle_path(video, "en").name == "Film.2020.1080p.en.srt"

    assert subtitles.missing_languages(video, ["en", "es"]) == ["en", "es"]
    subtitles.subtitle_path(video, "en").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert subtitles.missing_languages(video, ["en", "es"]) == ["es"]
    assert subtitles.existing_subtitles(video)["en"].name.endswith(".en.srt")

    # An untagged subtitle is still "a subtitle is present", under "".
    other = make_video(tmp_path / "Other" / "Other.mkv")
    (other.parent / "Other.srt").write_text("1\n")
    assert list(subtitles.existing_subtitles(other)) == [""]
    assert subtitles.missing_languages(other, ["en"]) == ["en"]


def test_pick_best_prefers_a_hash_match_then_srt():
    loose = subtitles.Candidate("p", "1", "en", downloads=9000, rating=9.0)
    hashed = subtitles.Candidate("p", "2", "en", downloads=10, rating=1.0, hash_match=True)
    assert subtitles.pick_best([loose, hashed], "en").identifier == "2"

    srt = subtitles.Candidate("p", "3", "en", downloads=100, format="srt")
    other = subtitles.Candidate("p", "4", "en", downloads=100, format="sub")
    assert subtitles.pick_best([other, srt], "en").identifier == "3"

    spanish = subtitles.Candidate("p", "5", "es", downloads=9999)
    assert subtitles.pick_best([spanish], "en") is None


def test_write_subtitle_is_atomic_validated_and_never_overwrites(tmp_path):
    video = make_video(tmp_path / "Film.mkv")
    payload = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    written = subtitles.write_subtitle(video, "en", payload)
    assert written.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))

    # A second write keeps the first subtitle.
    again = subtitles.write_subtitle(video, "en", b"different")
    assert again.read_bytes() == payload

    with pytest.raises(subtitles.SubtitleError):
        subtitles.write_subtitle(video, "es", b"this is not a subtitle")

    # Gzip and a BOM are both normal payloads.
    import gzip
    gz = gzip.compress(b"\xef\xbb\xbf" + payload)
    assert subtitles.write_subtitle(video, "fr", gz).read_bytes() == payload


class FakeProvider:
    name = "fake"
    configured = True

    def __init__(self, payload=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n", found=True):
        self.payload = payload
        self.found = found
        self.searches = 0
        self.downloaded = 0

    def search(self, request, languages):
        self.searches += 1
        if not self.found:
            return []
        return [subtitles.Candidate("fake", "42", languages[0], downloads=10, hash_match=True)]

    def download(self, candidate):
        self.downloaded += 1
        return self.payload


@pytest.fixture()
def conn():
    connection = db.connect(settings.db_path)
    yield connection
    connection.close()


def test_fetch_for_writes_the_file_and_records_it(tmp_path, conn):
    video = make_video(tmp_path / "Film (2021)" / "Film.2021.1080p.mkv")
    provider = FakeProvider()

    results = subtitles.fetch_for(conn, video, ["en"], provider)
    assert [row["status"] for row in results] == ["ok"]
    assert subtitles.subtitle_path(video, "en").exists()
    assert provider.downloaded == 1

    row = db.one(conn, "SELECT * FROM subtitles WHERE video_path = ? AND language = 'en'",
                 (str(video),))
    assert row["status"] == "ok" and row["source"] == "fake"

    # Second run finds nothing missing and does not touch the network.
    assert subtitles.fetch_for(conn, video, ["en"], provider) == []
    assert provider.searches == 1


def test_fetch_for_records_a_miss_and_a_bad_payload(tmp_path, conn):
    video = make_video(tmp_path / "Missing" / "Missing.mkv")
    assert [row["status"] for row in subtitles.fetch_for(conn, video, ["en"], FakeProvider(found=False))] == ["missing"]
    row = db.one(conn, "SELECT status, last_error FROM subtitles WHERE video_path = ?", (str(video),))
    assert row["status"] == "missing" and row["last_error"] == "no candidate"

    other = make_video(tmp_path / "Broken" / "Broken.mkv")
    bad = FakeProvider(payload=b"<html>not a subtitle</html>")
    assert [row["status"] for row in subtitles.fetch_for(conn, other, ["en"], bad)] == ["failed"]
    assert not subtitles.subtitle_path(other, "en").exists()


def test_run_pass_respects_the_batch_and_reports_disabled(conn):
    # run_pass works on the configured library root, which is the test sandbox.
    for index in range(3):
        make_video(settings.library_root / "tv" / f"Batch{index}" / f"Batch{index}.mkv")

    disabled = subtitles.run_pass(conn, settings, None)
    assert disabled["enabled"] is False
    assert "no subtitle provider" in disabled["reason"]

    provider = FakeProvider()
    report = subtitles.run_pass(conn, settings, provider, limit=2, delay=0)
    assert report["enabled"] is True and report["ok"] == 2
    assert provider.downloaded == 2


def test_opensubtitles_client_speaks_the_rest_api(monkeypatch, tmp_path):
    """The shipped provider against a fake API: login, search, download, file."""
    import gzip
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    seen: dict[str, object] = {}
    subtitle_text = b"1\n00:00:05,000 --> 00:00:07,000\nFound it\n"

    class Api(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/login":
                seen["login_key"] = self.headers.get("Api-Key")
                seen["login_body"] = body
                return self._json({"token": "token-abc"})
            if self.path == "/download":
                seen["download_auth"] = self.headers.get("Authorization")
                seen["download_body"] = body
                return self._json({"link": f"http://127.0.0.1:{self.server.server_port}/file/42"})
            return self._json({"error": "unexpected"}, status=404)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/subtitles":
                seen["query"] = parse_qs(parsed.query)
                return self._json({"data": [{"attributes": {
                    "language": "en", "release": "Film.2020.1080p-GRP",
                    "download_count": 4200, "ratings": 8.0,
                    "moviehash_match": True, "hearing_impaired": False,
                    "files": [{"file_id": 42, "file_name": "Film.en.srt"}],
                }}]})
            if parsed.path == "/file/42":
                payload = gzip.compress(subtitle_text)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json({"error": "unexpected"}, status=404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        provider = subtitles.OpenSubtitles("key-123", "user", "pass", base=base)
        assert provider.configured is True

        video = make_video(tmp_path / "Film (2020)" / "Film.2020.1080p.mkv")
        request = subtitles.request_for(video, "movie")
        assert request.title == "Film" and request.year == 2020

        candidates = provider.search(request, ["en"])
        assert len(candidates) == 1
        assert candidates[0].hash_match is True
        assert candidates[0].identifier == "42"
        assert seen["login_body"] == {"username": "user", "password": "pass"}
        assert seen["login_key"] == "key-123"
        assert seen["query"]["moviehash"] == [request.hash]
        assert seen["query"]["languages"] == ["en"]

        target = subtitles.write_subtitle(video, "en", provider.download(candidates[0]))
        assert target.read_bytes() == subtitle_text
        assert seen["download_auth"] == "Bearer token-abc"
        assert seen["download_body"] == {"file_id": 42}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_opensubtitles_stays_quiet_without_a_key(tmp_path):
    provider = subtitles.OpenSubtitles("")
    assert provider.configured is False
    assert provider.search(subtitles.request_for(make_video(tmp_path / "x.mkv")), ["en"]) == []


def test_missing_report_counts_without_the_network(tmp_path):
    make_video(tmp_path / "A" / "A.mkv")
    with_sub = make_video(tmp_path / "B" / "B.mkv")
    subtitles.subtitle_path(with_sub, "en").write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")

    report = subtitles.missing_report(tmp_path, ["en", "es"])
    assert report["videos"] == 2
    assert report["by_language"] == {"en": 1, "es": 2}
    assert len(report["missing"]) == 2
