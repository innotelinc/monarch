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


ALBUM_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
TRACK_NUMBER = re.compile(r"^\s*\d{1,3}[\s._-]+(?=\S)")


def place(path: Path, root: Path) -> dict[str, str]:
    """Where a catalogued file sits in the library, in the words a person uses.

    A flat list of 140 tracks is not a music library, and the titles alone do not
    say which album a file belongs to. The layout the importer writes already
    answers it - `music/<Artist>/<Album>/<file>`, `tv/<Show>/Season NN/<file>`,
    `movies/<Title (Year)>/<file>` - so this reads the path back instead of
    keeping a second copy of the answer in the database.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return {}
    if len(parts) < 2:
        return {}
    top = parts[0].lower()
    if top == "music":
        album = ALBUM_YEAR.sub("", parts[2]) if len(parts) >= 4 else ""
        return {"artist": parts[1], "album": album}
    if top == "tv":
        return {"show": parts[1], "season": parts[2] if len(parts) >= 3 else ""}
    if top in {"movies", "adult"}:
        return {"collection": parts[1]}
    return {}


def track_title(title: str, artist: str = "", album: str = "") -> str:
    """A track's own name, without the artist, album and number around it.

    `XXXTENTACION - ! - 14 - I don't even speak spanish lol` sits under the
    artist and album headings already, and the row repeated all three before the
    song: the path says which prefixes are the catalogue's own words, so those
    are removed before the number.
    """
    name = (title or "").strip()
    prefixes = [prefix for prefix in (
        f"{artist} - {album} - " if artist and album else "",
        f"{artist} - " if artist else "",
    ) if prefix]
    for prefix in prefixes:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    name = TRACK_NUMBER.sub("", name).strip()
    # `03 Bruno Mars - I Just Might`: the number came first, the artist did not.
    for prefix in prefixes:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    return name.strip() or title


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
