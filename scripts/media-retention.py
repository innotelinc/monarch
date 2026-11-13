#!/usr/bin/env python3
"""media-retention.py — keep the Monarch library inside its age and count limits.

Jellyfin is served from the layout in docs/operations.md:

    /data/media/movies/<Title (Year)>/<file>.mkv
    /data/media/tv/<Show>/Season 01/<Show> - S01E02 - <name>.mkv

Two rules apply, both measured from the media file's own mtime so a re-scan or
a metadata refresh cannot make an old title look new:

  * age — a title whose newest media file is older than ``--max-age-days`` is
    removed, from both the movies and the shows library;
  * count — a series keeps only its newest ``--keep-latest`` episodes.

The count rule exists because a show that keeps downloading forever fills the
disk with episodes nobody watches again, while the age rule alone would empty a
series that is simply between seasons. Applied together, an episode survives
only when it is both recent and among the newest few.

A title is deleted as a whole folder: the video, its ``.nfo``, subtitles and
poster go together, so Jellyfin never keeps a poster for a film that is gone.
Empty season and show folders are removed afterwards.

Nothing is deleted unless ``--apply`` is passed: the default run prints the
plan, which is what the timer in the header is safe to run first. A dry run
exits 0 when there is nothing to do and 0 when there is — callers read the
summary, not the status, because "would delete" is not a failure.

Env:
  MEDIA_ROOT        library root (default /data/media)

Install (root), sweeping nightly at 04:30:

  install -m 0755 scripts/media-retention.py /usr/local/sbin/media-retention.py
  echo '30 4 * * * root MEDIA_ROOT=/data/media /usr/local/sbin/media-retention.py --apply' \\
    > /etc/cron.d/media-retention
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# What counts as the media itself. A folder is a title when it holds one of
# these; everything else beside it (.nfo, .srt, .jpg, .trickplay) travels with
# the video it belongs to.
VIDEO_SUFFIXES = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm", ".ts", ".mpg", ".mpeg",
}

# Sidecars share the video's stem, e.g. "Film (2019).en.srt" beside
# "Film (2019).mkv". They are matched by stem prefix, not by suffix alone, so a
# poster named after the folder is not mistaken for a second title.
SIDECAR_SUFFIXES = {".nfo", ".srt", ".ass", ".vtt", ".sub", ".idx", ".jpg", ".jpeg", ".png", ".trickplay"}

SEASON_EPISODE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,4})")
EPISODE_ONLY = re.compile(r"(?:^|[^A-Za-z])[Ee](\d{1,4})(?:[^0-9]|$)")


@dataclass
class Plan:
    delete_files: list[Path] = field(default_factory=list)
    delete_dirs: list[Path] = field(default_factory=list)
    keep_files: int = 0
    bytes_freed: int = 0

    def add_title(self, folder: Path, files: list[Path]) -> None:
        """Queue a whole title folder, counting only the media for the size."""
        self.delete_dirs.append(folder)
        for path in files:
            self.delete_files.append(path)
            self.bytes_freed += _size(path)

    def add_file(self, path: Path) -> None:
        self.delete_files.append(path)
        self.bytes_freed += _size(path)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _videos(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES]


def _sidecars_of(video: Path) -> list[Path]:
    """Files that belong to one video: same stem, or the stem plus a language tag."""
    stem = video.stem
    found = []
    for sibling in video.parent.iterdir():
        if sibling == video or not sibling.is_file():
            continue
        if sibling.suffix.lower() not in SIDECAR_SUFFIXES:
            continue
        if sibling.stem == stem or sibling.stem.startswith(stem + ".") or sibling.name.startswith(stem + "."):
            found.append(sibling)
    return found


def _episode_key(video: Path) -> tuple[int, int, float]:
    """Order episodes by season and number; fall back to mtime when unnamed."""
    match = SEASON_EPISODE.search(video.name)
    if match:
        return (int(match.group(1)), int(match.group(2)), _mtime(video))
    only = EPISODE_ONLY.search(video.name)
    season = 1
    parent = SEASON_EPISODE.search(video.parent.name)
    if parent:
        season = int(parent.group(1))
    if only:
        return (season, int(only.group(1)), _mtime(video))
    return (season, 0, _mtime(video))


def plan_movies(movies_root: Path, cutoff: float, plan: Plan) -> None:
    if not movies_root.is_dir():
        return
    for title in sorted(movies_root.iterdir()):
        if title.is_dir():
            videos = _videos(title)
            if not videos:
                continue
            newest = max(_mtime(v) for v in videos)
            if newest >= cutoff:
                plan.keep_files += len(videos)
                continue
            plan.add_title(title, videos + [s for v in videos for s in _sidecars_of(v)])
        elif title.is_file() and title.suffix.lower() in VIDEO_SUFFIXES:
            if _mtime(title) >= cutoff:
                plan.keep_files += 1
                continue
            plan.add_file(title)
            for sidecar in _sidecars_of(title):
                plan.add_file(sidecar)


def plan_shows(tv_root: Path, cutoff: float, keep_latest: int, plan: Plan) -> None:
    if not tv_root.is_dir():
        return
    for show in sorted(tv_root.iterdir()):
        if not show.is_dir():
            continue
        videos = sorted(_videos(show), key=_episode_key)
        if not videos:
            continue
        newest = videos[-keep_latest:] if keep_latest > 0 else []
        keep = set(newest)
        for video in videos:
            recent = _mtime(video) >= cutoff
            if video in keep and recent:
                plan.keep_files += 1
                continue
            plan.add_file(video)
            for sidecar in _sidecars_of(video):
                plan.add_file(sidecar)


def apply_plan(plan: Plan, *, movies_root: Path, tv_root: Path) -> None:
    for path in plan.delete_files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    # A title folder goes when nothing but empty folders is left inside it.
    for folder in plan.delete_dirs:
        if not folder.exists():
            continue
        videos = _videos(folder)
        if videos:
            continue
        shutil.rmtree(folder, ignore_errors=True)
    # Then prune what the per-file removals emptied, deepest first, so a show
    # whose last episode went does not leave an empty "Season 03" behind.
    for root in (tv_root, movies_root):
        if not root.is_dir():
            continue
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                next(folder.iterdir())
            except StopIteration:
                try:
                    folder.rmdir()
                except OSError:
                    pass
            except OSError:
                pass


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drop movies and episodes past the age limit, and keep only a series' newest episodes.",
    )
    parser.add_argument("--root", default=os.environ.get("MEDIA_ROOT", "/data/media"),
                        help="library root holding movies/ and tv/ (default: $MEDIA_ROOT or /data/media)")
    parser.add_argument("--max-age-days", type=float, default=7.0,
                        help="remove media whose newest file is older than this many days (default: 7)")
    parser.add_argument("--keep-latest", type=int, default=3,
                        help="episodes each series keeps (default: 3; 0 keeps none)")
    parser.add_argument("--apply", action="store_true",
                        help="delete for real; without it the plan is only printed")
    args = parser.parse_args(argv)

    if args.max_age_days < 0:
        parser.error("--max-age-days cannot be negative")
    if args.keep_latest < 0:
        parser.error("--keep-latest cannot be negative")

    root = Path(args.root)
    movies_root = root / "movies"
    tv_root = root / "tv"
    if not movies_root.is_dir() and not tv_root.is_dir():
        print(f"media-retention: no library under {root} (looked for movies/ and tv/)", file=sys.stderr)
        return 2

    cutoff = time.time() - args.max_age_days * 86400
    plan = Plan()
    plan_movies(movies_root, cutoff, plan)
    plan_shows(tv_root, cutoff, args.keep_latest, plan)

    for path in plan.delete_files:
        print(f"  {'remove' if args.apply else 'would remove'} {path}")
    if args.apply:
        apply_plan(plan, movies_root=movies_root, tv_root=tv_root)

    verb = "removed" if args.apply else "would remove"
    print(
        f"media-retention: {verb} {len(plan.delete_files)} file(s) "
        f"({_human(plan.bytes_freed)}), kept {plan.keep_files}; "
        f"age limit {args.max_age_days:g}d, {args.keep_latest} episode(s) per series"
    )
    if not args.apply and plan.delete_files:
        print("media-retention: dry run — pass --apply to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
