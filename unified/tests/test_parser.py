from app.parser import matches, normalise, parse, title_key


def test_movie_release():
    release = parse("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.HEVC-CMRG")
    assert release.title == "Dune Part Two"
    assert release.year == 2024
    assert release.resolution == "2160p"
    assert release.source == "web-dl"
    assert release.codec == "x265"
    assert release.audio == "ddp"
    assert release.hdr == "dolby vision"
    assert release.group == "CMRG"
    assert release.kind == "movie"


def test_episode_release():
    release = parse("Severance.S02E07.1080p.WEB-DL.DDP5.1.H.264-NTb")
    assert release.title == "Severance"
    assert release.season == 2
    assert release.episode == 7
    assert release.kind == "episode"
    assert release.resolution == "1080p"


def test_episode_range():
    release = parse("The.Bear.S03E01-E03.1080p.WEB-DL.x264-GROUP")
    assert (release.season, release.episode, release.episode_end) == (3, 1, 3)


def test_season_pack():
    release = parse("Andor.S01.2160p.WEB-DL.DV.HDR.DDP5.1.x265-TEPES")
    assert release.season == 1
    assert release.episode is None
    assert release.pack is True


def test_brackets_and_extension_removed():
    release = parse("[SubsPlease] Frieren - 12 [1080p].mkv")
    assert "[" not in release.raw or release.title
    assert release.resolution == "1080p"


def test_cam_release_is_recognised():
    release = parse("Some.Movie.2025.HDCAM.x264-GROUP")
    assert release.source == "cam"


def test_audio_only_is_track():
    release = parse("Radiohead - In Rainbows - 15 Step.flac")
    assert release.kind == "track"


def test_title_key_normalises():
    assert title_key("Dune: Part Two") == title_key("dune part two")
    assert title_key("  The   Bear ") == "the bear"


def test_normalise_strips_extension():
    assert normalise("Show.S01E01.mkv") == "Show S01E01"


def test_matches_requires_same_title_and_episode():
    release = parse("Severance.S02E07.1080p.WEB-DL.DDP5.1.H.264-NTb")
    assert matches(release, "Severance", season=2, episode=7)
    assert not matches(release, "Severance", season=2, episode=8)
    assert not matches(release, "Silo", season=2, episode=7)


def test_matches_movie_needs_no_episode():
    release = parse("Dune.Part.Two.2024.2160p.WEB-DL.HEVC-CMRG")
    assert matches(release, "Dune Part Two", year=2024)
    assert not matches(release, "Dune Part Two", year=2021)


def test_pack_covers_episode():
    pack = parse("Andor.S01.1080p.WEB-DL.x265-TEPES")
    assert matches(pack, "Andor", season=1, episode=5)
