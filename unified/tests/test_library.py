from pathlib import Path

from app import library
from app.parser import parse

MAPPING = {"movies": "movie", "tv": "episode", "music": "track"}


def write(path: Path, size: int = 2048, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload * size)
    return path


def test_scan_classifies_by_top_level_directory(tmp_path):
    write(tmp_path / "movies" / "Dune.Part.Two.2024.1080p.WEB-DL.x264-GRP.mkv")
    write(tmp_path / "tv" / "Severance" / "Severance.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    rows = library.scan(tmp_path, MAPPING)
    kinds = {row["kind"] for row in rows}
    assert kinds == {"movie", "episode"}
    assert all(row["fingerprint"] for row in rows)


def test_scan_is_idempotent(tmp_path):
    write(tmp_path / "movies" / "Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP.mkv")
    first = library.scan(tmp_path, MAPPING)
    second = library.scan(tmp_path, MAPPING)
    assert [r["fingerprint"] for r in first] == [r["fingerprint"] for r in second]
    assert len(first) == 1


def test_scan_skips_samples(tmp_path):
    write(tmp_path / "movies" / "Dune.Part.Two.2024.sample.mkv")
    assert library.scan(tmp_path, MAPPING) == []


def test_duplicate_quality_keeps_larger_file(tmp_path):
    write(tmp_path / "movies" / "Dune.Part.Two.2024.1080p.WEB-DL.x264-A.mkv", size=1024)
    write(tmp_path / "movies" / "copy" / "Dune.Part.Two.2024.1080p.WEB-DL.x264-B.mkv", size=4096)
    rows = library.scan(tmp_path, MAPPING)
    assert len(rows) == 1
    assert rows[0]["size"] == 4096


def test_plan_dedupe_finds_identical_bytes(tmp_path):
    payload = b"identical-content"
    a = write(tmp_path / "a.mkv", size=100, payload=payload)
    b = write(tmp_path / "b.mkv", size=100, payload=payload)
    write(tmp_path / "c.mkv", size=100, payload=b"different")

    report = library.plan_dedupe([a, b, tmp_path / "c.mkv"])
    assert report["count"] == 1
    assert report["wasted_bytes"] > 0
    group = report["groups"][0]
    assert len(group["remove"]) == 1
    assert group["keep"] != group["remove"][0]


def test_target_path_for_movie(tmp_path):
    release = parse("Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP.mkv")
    destination = library.target_path(Path("/downloads/dune.mkv"), release, tmp_path)
    assert destination == tmp_path / "movies" / "Dune Part Two (2024)" / "dune.mkv"


def test_target_path_for_episode(tmp_path):
    release = parse("Severance.S02E07.1080p.WEB-DL.x264-GRP.mkv")
    destination = library.target_path(Path("/downloads/ep.mkv"), release, tmp_path)
    assert destination == tmp_path / "tv" / "Severance" / "Season 02" / "ep.mkv"


def test_link_or_copy_never_overwrites(tmp_path):
    source = write(tmp_path / "src" / "file.mkv")
    destination = tmp_path / "dst" / "file.mkv"
    assert library.link_or_copy(source, destination) in {"linked", "copied"}
    assert library.link_or_copy(source, destination) == "exists"


def test_find_video_skips_samples(tmp_path):
    write(tmp_path / "download" / "movie.sample.mkv", size=10)
    big = write(tmp_path / "download" / "movie.mkv", size=60 * 1024 * 1024)
    assert library.find_video(tmp_path / "download") == big
