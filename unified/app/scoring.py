"""Quality profiles and the decision to grab.

Scoring is deliberately explicit and inspectable: the UI shows the number and
the reasons for every candidate, so "why did it download the 720p one" is a
question with an answer rather than a mystery. A profile is a plain dict stored
as JSON in settings, so it can be edited without a redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import Release, title_key

RESOLUTION_RANK = {"2160p": 5, "1080p": 4, "1080i": 3, "720p": 2, "576p": 1, "480p": 1}
SOURCE_RANK = {
    "remux": 70, "bluray": 60, "web-dl": 50, "webrip": 35,
    "hdtv": 20, "dvdrip": 15, "hdrip": 10,
    "cam": 0, "telesync": 0, "screener": 0,
}
CODEC_RANK = {"av1": 20, "x265": 18, "x264": 12, "vp9": 10, "mpeg2": 4, "xvid": 0}

DEFAULT_PROFILE = {
    "name": "default",
    "resolutions": ["2160p", "1080p", "720p"],
    "sources": ["remux", "bluray", "web-dl", "webrip", "hdtv"],
    "codecs": ["av1", "x265", "x264"],
    "require_hdr": False,
    "min_size_gb": 0.15,
    "max_size_gb": 70.0,
    "upgrade_allowed": True,
    "cutoff": "1080p web-dl",
}


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    score: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "score": self.score, "reasons": self.reasons}


def _size_gb(size: int | None) -> float | None:
    return None if not size else round(size / 1024 ** 3, 2)


def evaluate(release: Release, profile: dict | None = None,
             wanted_kind: str | None = None) -> Verdict:
    """Score one candidate against a profile. Rejections carry their reason."""
    profile = {**DEFAULT_PROFILE, **(profile or {})}
    reasons: list[str] = []
    score = 0

    if wanted_kind and release.kind != wanted_kind and not (
        wanted_kind == "episode" and release.pack
    ):
        return Verdict(False, 0, [f"kind is {release.kind}, wanted {wanted_kind}"])

    if release.source in {"cam", "telesync", "screener"}:
        return Verdict(False, 0, [f"source {release.source} is never acceptable"])

    if not release.resolution:
        return Verdict(False, 0, ["no resolution in the name"])
    if release.resolution not in profile["resolutions"]:
        return Verdict(False, 0, [f"{release.resolution} is not in the profile"])

    if release.source and release.source not in profile["sources"]:
        return Verdict(False, 0, [f"source {release.source} is not in the profile"])

    if release.codec and release.codec not in profile["codecs"]:
        return Verdict(False, 0, [f"codec {release.codec} is not in the profile"])

    if profile.get("require_hdr") and not release.hdr:
        return Verdict(False, 0, ["profile requires HDR"])

    # Resolution dominates: the profile lists its resolutions best-first.
    order = profile["resolutions"]
    score += max(0, (len(order) - order.index(release.resolution))) * 100
    reasons.append(f"resolution {release.resolution} +{max(0, (len(order) - order.index(release.resolution))) * 100}")

    if release.source:
        bonus = SOURCE_RANK.get(release.source, 0)
        score += bonus
        reasons.append(f"source {release.source} +{bonus}")

    if release.codec:
        bonus = CODEC_RANK.get(release.codec, 0)
        score += bonus
        reasons.append(f"codec {release.codec} +{bonus}")

    if release.hdr:
        score += 10
        reasons.append(f"{release.hdr} +10")
    if release.proper:
        score += 5
        reasons.append("proper +5")
    if release.repack:
        score += 5
        reasons.append("repack +5")
    if release.pack:
        score += 5
        reasons.append("season pack +5")

    gb = _size_gb(getattr(release, "size", None))
    if gb is not None:
        if gb < profile["min_size_gb"]:
            return Verdict(False, 0, reasons + [f"{gb} GB is below the minimum"])
        if gb > profile["max_size_gb"]:
            return Verdict(False, 0, reasons + [f"{gb} GB is above the maximum"])
        reasons.append(f"size {gb} GB")

    return Verdict(True, score, reasons)


def better_than(candidate: Release, current_quality: str | None,
                profile: dict | None = None) -> bool:
    """Whether a candidate is an upgrade over what the catalog already has.

    With no current copy any accepted candidate wins. With one, the profile's
    own ordering decides — never "newer is better".
    """
    profile = {**DEFAULT_PROFILE, **(profile or {})}
    if not current_quality:
        return True
    if not profile.get("upgrade_allowed", True):
        return False
    current = (current_quality or "").lower()
    current_res = next((r for r in RESOLUTION_RANK if r in current), None)
    candidate_rank = RESOLUTION_RANK.get(candidate.resolution or "", 0)
    current_rank = RESOLUTION_RANK.get(current_res or "", 0)
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    current_source = next((s for s in SOURCE_RANK if s in current), None)
    if candidate.source and SOURCE_RANK.get(candidate.source, 0) != SOURCE_RANK.get(current_source or "", 0):
        return SOURCE_RANK.get(candidate.source, 0) > SOURCE_RANK.get(current_source or "", 0)
    if candidate.hdr and "hdr" not in current:
        return True
    return candidate.proper and "proper" not in current


def cutoff_met(current_quality: str | None, profile: dict | None = None) -> bool:
    """Whether the catalog already holds the profile's cutoff quality."""
    profile = {**DEFAULT_PROFILE, **(profile or {})}
    cutoff = (profile.get("cutoff") or "").lower().split()
    quality = (current_quality or "").lower()
    return bool(cutoff) and all(part in quality for part in cutoff)


def best(candidates: list[Release], title: str, year: int | None = None,
         season: int | None = None, episode: int | None = None,
         profile: dict | None = None, wanted_kind: str | None = None,
         current_quality: str | None = None) -> tuple[Release | None, Verdict]:
    """Pick the highest-scoring acceptable candidate, or explain why not."""
    from .parser import matches

    scored: list[tuple[int, Release, Verdict]] = []
    rejected: list[str] = []
    for release in candidates:
        if not matches(release, title, year, season, episode):
            continue
        verdict = evaluate(release, profile, wanted_kind)
        if not verdict.accepted:
            rejected.extend(verdict.reasons[:1])
            continue
        if not better_than(release, current_quality, profile):
            rejected.append(f"{release.quality} is not an upgrade on {current_quality}")
            continue
        scored.append((verdict.score, release, verdict))

    if not scored:
        detail = "; ".join(dict.fromkeys(rejected)) or "no candidate matched the title"
        return None, Verdict(False, 0, [detail])

    scored.sort(key=lambda row: (row[0], row[1].proper, row[1].repack), reverse=True)
    score, release, verdict = scored[0]
    return release, Verdict(True, score, verdict.reasons)


def dedupe_key(release: Release) -> str:
    """Content identity for dedupe: title, year, episode and quality family.

    Two indexers returning the same episode at the same quality are the same
    grab; a different resolution is a different one.
    """
    return "|".join([
        title_key(release.title),
        str(release.year or ""),
        str(release.season if release.season is not None else ""),
        str(release.episode if release.episode is not None else ""),
        release.resolution or "",
        release.source or "",
    ])
