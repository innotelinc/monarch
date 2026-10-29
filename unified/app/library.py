"""The library: scanning, dedupe and import paths.

Dedupe happens at two levels, which is what makes the merged platform smaller
than the stack it replaces:

  * *catalog* dedupe — the fingerprint (title/year/episode/quality) is unique,
    so rescanning is idempotent and two copies of the same release at the same
    quality never both appear;
  * *content* dedupe — the first megabyte's SHA-256 plus the file size proves
    two differently named files are the same bytes, which is what an old
    download directory full of renamed duplicates looks like.

Nothing here deletes anything. `plan_dedupe` returns a report; removing files
is a separate, explicit action.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .parser import AUDIO_EXT, VIDEO_EXT, Release, parse, title_key

SUBTITLE_EXT = {".srt", ".sub", ".ass", ".ssa", ".vtt"}
SAMPLE = re.compile(r"\b(sample|trailer)\b", re.I)


def is_media(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXT | AUDIO_EXT and not SAMPLE.search(path.name)


def kind_for(path: Path, root: Path, mapping: dict[str, str]) -> str:
    """Classify by the top-level library directory the file sits under."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "movie"
    top = relative.parts[0].lower() if relative.parts else ""
    return mapping.get(top, "movie")


def fingerprint(release: Release) -> str:
    """The catalog identity: one row per title/year/episode/quality."""
    return "|".join([
        release.kind,
        title_key(release.title),
        str(release.year or ""),
        "" if release.season is None else f"s{release.season:02d}",
        "" if release.episode is None else f"e{release.episode:02d}",
        (release.resolution or "") + "/" + (release.source or ""),
    ])


def iter_files(root: Path) -> Iterator[Path]:
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if is_media(path):
                yield path


def scan(root: Path, mapping: dict[str, str] | None = None,
         limit: int | None = None) -> list[dict]:
    """Walk a library root and return catalog rows, deduped by fingerprint."""
    mapping = mapping or {"movies": "movie", "tv": "episode", "music": "track", "adult": "movie"}
    rows: dict[str, dict] = {}
    for count, path in enumerate(iter_files(root), start=1):
        if limit is not None and count > limit:
            break
        release = parse(path.name, kind_for(path, root, mapping))
        fp = fingerprint(release)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        row = {
            "kind": release.kind,
            "title": release.title,
            "year": release.year,
            "season": release.season,
            "episode": release.episode,
            "quality": release.quality,
            "path": str(path),
            "size": size,
            "fingerprint": fp,
        }
        existing = rows.get(fp)
        # Same title and quality twice: keep the larger file, report the other.
        if existing is None or size > (existing.get("size") or 0):
            rows[fp] = row
    return list(rows.values())


def content_hash(path: Path, chunk: int = 1024 * 1024) -> str:
    """SHA-256 of the first megabyte plus the size — cheap identical-bytes proof."""
    digest = hashlib.sha256()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            digest.update(handle.read(chunk))
    except OSError:
        return ""
    digest.update(str(size).encode())
    return digest.hexdigest()


def plan_dedupe(paths: Iterable[Path]) -> dict:
    """Group files whose bytes are identical; keep the best-named copy.

    Returns the report the UI shows before anything is touched. Selection is
    quality first (a better encode wins even at a smaller size), then size.
    """
    groups: dict[str, list[Path]] = {}
    for path in paths:
        key = content_hash(path)
        if key:
            groups.setdefault(key, []).append(path)

    duplicates = []
    wasted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda p: (parse(p.name).quality, p.stat().st_size if p.exists() else 0),
            reverse=True,
        )
        keep, extras = ranked[0], ranked[1:]
        wasted += sum(p.stat().st_size for p in extras if p.exists())
        duplicates.append({
            "fingerprint": key,
            "keep": str(keep),
            "remove": [str(p) for p in extras],
            "bytes": sum(p.stat().st_size for p in extras if p.exists()),
        })
    return {"groups": duplicates, "wasted_bytes": wasted, "count": len(duplicates)}


def target_path(completed: Path, release: Release, root: Path) -> Path:
    """Where an imported file belongs in the library.

    Movies get `movies/Title (Year)/`, episodes `tv/Show/Season NN/`. The file
    name is kept as the release name so a later scan parses it back to exactly
    the same fingerprint.
    """
    if release.kind == "episode" and release.season is not None:
        folder = root / "tv" / release.title / f"Season {release.season:02d}"
    elif release.kind == "track":
        folder = root / "music" / release.title
    else:
        suffix = f" ({release.year})" if release.year else ""
        folder = root / "movies" / f"{release.title}{suffix}"
    return folder / completed.name


def link_or_copy(source: Path, destination: Path) -> str:
    """Hardlink when possible, copy when across filesystems. Never overwrite."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return "exists"
    try:
        os.link(source, destination)
        return "linked"
    except OSError:
        import shutil
        shutil.copy2(source, destination)
        return "copied"


def find_video(directory: Path, minimum_bytes: int = 1) -> Path | None:
    """The largest real video in a completed download (skips samples by name)."""
    best: Path | None = None
    best_size = minimum_bytes
    for path in iter_files(directory):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size >= best_size:
            best, best_size = path, size
    return best
