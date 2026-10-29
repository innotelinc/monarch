from app.parser import parse
from app.scoring import (
    DEFAULT_PROFILE, best, better_than, cutoff_met, dedupe_key, evaluate,
)


def test_higher_resolution_wins():
    low, high = parse("Movie.2024.1080p.WEB-DL.x264-GRP"), parse("Movie.2024.2160p.WEB-DL.x265-GRP")
    assert evaluate(high).score > evaluate(low).score


def test_cam_is_rejected():
    verdict = evaluate(parse("Movie.2025.HDCAM.x264-GRP"))
    assert not verdict.accepted
    assert "never acceptable" in verdict.reasons[0]


def test_unknown_resolution_is_rejected():
    verdict = evaluate(parse("Some.Release.Without.Tags-GRP"))
    assert not verdict.accepted


def test_source_outside_profile_is_rejected():
    profile = {**DEFAULT_PROFILE, "sources": ["bluray"]}
    verdict = evaluate(parse("Movie.2024.1080p.WEB-DL.x264-GRP"), profile)
    assert not verdict.accepted
    assert "web-dl" in verdict.reasons[0]


def test_size_limits_are_enforced():
    release = parse("Movie.2024.1080p.WEB-DL.x264-GRP")
    release = type(release)(**{**release.__dict__, "size": 200})
    verdict = evaluate(release, {**DEFAULT_PROFILE, "min_size_gb": 1.0})
    assert not verdict.accepted
    assert "below the minimum" in verdict.reasons[-1]


def test_best_picks_highest_score_matching_title():
    candidates = [
        parse("Dune.Part.Two.2024.720p.WEB-DL.x264-GRP"),
        parse("Dune.Part.Two.2024.2160p.WEB-DL.x265-GRP"),
        parse("Silo.S02E01.2160p.WEB-DL.x265-GRP"),
    ]
    chosen, verdict = best(candidates, "Dune Part Two", year=2024, wanted_kind="movie")
    assert chosen is not None
    assert chosen.resolution == "2160p"
    assert verdict.accepted


def test_best_returns_reason_when_nothing_matches():
    chosen, verdict = best([parse("Silo.S02E01.1080p.WEB-DL.x264-GRP")],
                           "Dune Part Two", wanted_kind="movie")
    assert chosen is None
    assert "no candidate matched" in verdict.reasons[0]


def test_no_upgrade_when_quality_is_lower():
    assert not better_than(parse("Movie.2024.1080p.WEB-DL.x264-GRP"), "2160p web-dl x265")
    assert better_than(parse("Movie.2024.2160p.WEB-DL.x265-GRP"), "1080p web-dl x264")


def test_upgrade_blocked_by_profile():
    assert not better_than(
        parse("Movie.2024.2160p.WEB-DL.x265-GRP"), "1080p web-dl x264",
        {**DEFAULT_PROFILE, "upgrade_allowed": False},
    )


def test_first_copy_always_allowed():
    assert better_than(parse("Movie.2024.720p.WEB-DL.x264-GRP"), None)


def test_cutoff_met():
    assert cutoff_met("1080p web-dl x265")
    assert not cutoff_met("720p webrip x264")


def test_dedupe_key_is_quality_aware():
    a = parse("Movie.2024.1080p.WEB-DL.x264-GRP")
    b = parse("Movie.2024.1080p.WEB-DL.x264-OTHER")
    c = parse("Movie.2024.2160p.WEB-DL.x265-GRP")
    assert dedupe_key(a) == dedupe_key(b)
    assert dedupe_key(a) != dedupe_key(c)
