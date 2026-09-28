#!/usr/bin/env python3
"""scripts/clipbucket-sync.py — keep ClipBucket's catalogue in step with Jellyfin's library.

WHY THIS EXISTS
---------------
`scripts/clipbucket-library.py` is the *owner* of the import: the naming, the
thumbnails, the catalogue rows and the HEVC policy all live there. What it is not
is automatic — it is a command somebody has to remember to run. That is the whole
gap this closes. Measured 2026-09-28: two films (`Runner`, `The Mongoose`) had
been sitting in `/data/media` for days — Jellyfin listed them, `tube.innotel.us`
did not — and the only thing missing was a person typing the command.

WHAT IT ADDS OVER `--check` / `--apply`
---------------------------------------
1. **DELETIONS.** `clipbucket-library.py` walks the *library* and asks "is every
   item on disk in the catalogue?". A row whose source is GONE is never examined,
   so `--check` answers `in sync` while the site still lists a film Jellyfin
   deleted. (Its `missing_media` is a different thing: the row exists and the
   file THIS TOOL WROTE is missing. The source being gone is invisible to it.)
   This compares the other way as well and repairs that half.

   The repair **deletes the item**: its catalogue rows, its thumbnails and the
   media copy this tool wrote for it — the film is gone from `/data/media`, so it
   should be gone from the site, and leaving a hardlink behind would keep its bytes
   on disk forever. The rows are removed the way the import writes them (thumbs
   through `cb_video_image`/`cb_video_thumb`, series membership through
   `cb_collection_items`), so nothing is left dangling. `--hide` is the reversible
   variant (`active='no'`, the switch the app's own browse query reads) for a
   library that is only *temporarily* absent.

   Deleting is therefore guarded, because it is the one operation that cannot be
   undone by re-running: a run refuses outright when the library is entirely
   empty (`scan_library` found nothing), which is what an unmounted `/data/media`
   looks like and is otherwise indistinguishable from "every film was deleted".

2. **A CHEAP TRIGGER, so "automatic" costs nothing when nothing happened.** The
   import walks the whole library and probes every item with ffprobe; running that
   every two minutes to discover there is nothing to do is waste. So a run first
   asks whether the library *changed*, by fingerprinting it (path, size, mtime of
   every file), and only runs the import when it did. The deleted-half check is
   cheap on its own and runs every time.

   Files still being written are excluded from the fingerprint until their mtime
   is `--settle` seconds old. An `*arr` import writes a file and then renames it,
   and without that a run can catch a half-copied film — the same reason the
   import would happily hardlink one.

USAGE
-----
    python3 scripts/clipbucket-sync.py --check    # drift in either direction, write nothing
    python3 scripts/clipbucket-sync.py --apply    # converge, if anything changed
    python3 scripts/clipbucket-sync.py --apply --force        # import even if unchanged
    python3 scripts/clipbucket-sync.py --apply --hide         # hide instead of deleting

Driven by `systemd/monarch-clipbucket-sync.timer`; see docs/operations.md.

Exit codes: 0 in sync (or applied) · 1 drift (with `--check`) · 2 cannot judge.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# The import's own code, loaded rather than re-implemented: the identity of an
# item is its *path* and the mapping from path to `file_name` is a fact about the
# library tool's naming, not something a second file may guess at. (The estate's
# tests load a hyphenated script the same way.)
_SIBLING = os.path.join(HERE, "clipbucket-library.py")
_spec = importlib.util.spec_from_file_location("clipbucket_library", _SIBLING)
if _spec is None or _spec.loader is None:  # pragma: no cover - install problem
    raise SystemExit(f"clipbucket-sync: cannot load {_SIBLING}")
cl = importlib.util.module_from_spec(_spec)
# Registered BEFORE exec_module: the module defines `@dataclass` classes, and a
# frozen dataclass resolves `sys.modules[cls.__module__]` while the decorator
# runs — with the module absent, the whole import dies with `'NoneType' object
# has no attribute '__dict__'`. (The estate's own tests register it the same way.)
sys.modules[_spec.name] = cl
_spec.loader.exec_module(cl)

STATE_DEFAULT = os.environ.get("CLIPBUCKET_SYNC_STATE", "/var/lib/monarch/clipbucket-sync.fingerprint")
SETTLE_DEFAULT = int(os.environ.get("CLIPBUCKET_SYNC_SETTLE", "180"))
IMPORT_TIMEOUT = 3600


# ── the fingerprint (pure: unit-tested without a library) ────────────────────
def fingerprint(root: str, movies_dir: str, tv_dir: str, settle: int, now: float) -> str:
    """A digest of the library's *settled* files: path, size, mtime.

    Settled means "not written in the last `settle` seconds", which is what keeps
    a half-copied film out of the import. Sizes and mtimes rather than contents:
    an `*arr` import changes both, this only has to notice *that* something moved.
    """
    digest = hashlib.sha256()
    for sub in (movies_dir, tv_dir):
        base = os.path.join(root, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue  # vanished mid-walk; the next run sees it gone
                if now - stat.st_mtime < settle:
                    continue
                rel = os.path.relpath(path, root)
                digest.update(f"{rel}\0{stat.st_size}\0{int(stat.st_mtime)}\n".encode())
    return digest.hexdigest()


def read_state(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def write_state(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")


# ── the catalogue side ───────────────────────────────────────────────────────
def catalogue_rows(container: str) -> dict[str, tuple[int, bool]]:
    """`file_name -> (videoid, active)` for every row this tool owns, read once.

    Scoped by `file_directory` — the same scope the import writes — so a video
    added by hand in the app's own UI is left alone rather than deleted for the
    crime of not being in `/data/media`.
    """
    rows = cl.mysql_query(
        container,
        "SELECT file_name, videoid, active FROM cb_video "
        f"WHERE file_directory='{cl.MEDIA_DIR}';",
    )
    return {
        row[0]: (int(row[1]) if len(row) > 1 and row[1].isdigit() else 0,
                 (row[2] == "yes" if len(row) > 2 else True))
        for row in rows
    }


def vanished_names(rows: dict[str, tuple[int, bool]], on_disk: set) -> list[str]:
    """Catalogue rows still listed whose source is not on disk any more.

    An `active='no'` row is already hidden, so it is not drift — otherwise a host
    converged with `--hide` would report the same row dirty on every run forever.
    """
    return sorted(
        name for name, (_videoid, active) in rows.items() if active and name not in on_disk
    )


# Every table holding a row that points at `cb_video.videoid`, with the column it
# points through — read off the live schema's foreign keys. `cb_video_image` is
# the one that matters for ordering (see `remove_items`); `cb_video_subtitle` is
# the only ON DELETE CASCADE, listed anyway so this is "the whole set" rather
# than "the whole set except the one handled for us".
VIDEO_CHILD_TABLES = (
    ("cb_videos_categories", "id_video"),
    ("cb_video_conversion_queue", "videoid"),
    ("cb_video_embed", "videoid"),
    ("cb_video_image", "videoid"),
    ("cb_video_rates", "id_video"),
    ("cb_video_subtitle", "videoid"),
    ("cb_video_tags", "id_video"),
    ("cb_video_tmdb", "video_id"),
    ("cb_video_users", "videoid"),
    ("cb_video_views", "id_video"),
)


def set_active(container: str, names: list[str], active: str) -> None:
    quoted = ", ".join(f"'{cl.sql_quote(n)}'" for n in names)
    cl.mysql_exec(
        container,
        f"UPDATE cb_video SET active='{active}' WHERE file_directory='{cl.MEDIA_DIR}' "
        f"AND file_name IN ({quoted});",
    )


def remove_files(facts: dict, name: str) -> int:
    """The files this tool wrote for one item: its media copies and its thumbs.

    The media is a hardlink into the source library, so removing the row and
    leaving the file keeps a deleted film's bytes alive for as long as the volume
    is. Thumbnails live one directory down, named after the item
    (`thumbs/video/<dir>/<file_name>/`), which is why this is an `rmtree` rather
    than a glob over the files.
    """
    files_path = facts["files_path"]
    videos = os.path.join(files_path, "upload", "files", "videos", cl.MEDIA_DIR)
    thumbs = os.path.join(files_path, "upload", "files", "thumbs", "video", cl.MEDIA_DIR, name)
    removed = 0
    # `keep=""` never matches, so this returns every copy for the item, not the
    # one a repair would have kept.
    for candidate in cl.stale_media(videos, name, ""):
        try:
            os.remove(os.path.join(videos, candidate))
            removed += 1
        except OSError:
            pass  # already gone; the row removal is what matters
    if os.path.isdir(thumbs):
        shutil.rmtree(thumbs, ignore_errors=True)
        removed += 1
    return removed


def remove_items(container: str, facts: dict, named: dict[str, int]) -> int:
    """Delete the catalogue rows AND the files for these items. Returns files removed.

    The child rows are read off the live schema's foreign keys into `cb_video`,
    and it has to be *all* of them: nearly every one is RESTRICT or NO ACTION, so
    a table left out does not merely leave an orphan behind — MySQL aborts the
    statement at that point, and the item ends up half-deleted instead (media
    still on disk, `cb_video_image` already gone). Which is exactly what happened
    the first time this ran, on `cb_videos_categories`.

    Everything runs in one transaction for the same reason: a schema change that
    turns up a constraint this list does not know about then rolls back to the
    item still whole, which is a state the next run can judge.

    Thumbs are deleted through a join rather than by `videoid`, because they hang
    off `cb_video_image.id_video_image`.
    """
    ids = ", ".join(str(v) for v in named.values())
    statements = [
        # Before the image rows go, since it keys off them.
        "DELETE t FROM cb_video_thumb t JOIN cb_video_image i "
        f"ON i.id_video_image=t.id_video_image WHERE i.videoid IN ({ids})",
        *(f"DELETE FROM {table} WHERE {column} IN ({ids})" for table, column in VIDEO_CHILD_TABLES),
        # A series holds its episodes through `cb_collection_items`, keyed by
        # `object_id` with `type='videos'`.
        f"DELETE FROM cb_collection_items WHERE type='videos' AND object_id IN ({ids})",
        f"DELETE FROM cb_video WHERE videoid IN ({ids})",
    ]
    cl.mysql_exec(container, "START TRANSACTION; " + "; ".join(statements) + "; COMMIT;")
    return sum(remove_files(facts, name) for name in named)


def converge(facts: dict, force: bool, hide: bool, state: str,
             root: str, movies_dir: str, tv_dir: str, settle: int,
             dry_run: bool, stream) -> int:
    """Report and (unless `dry_run`) repair both directions. Returns an exit code."""
    container = facts["container"]
    # `scan_library` and the catalogue read are cheap (a directory walk and one
    # query); `build_report` and the import are not, because both probe every item
    # with ffprobe. So the expensive half is reached only from `--check` (which is
    # allowed to be slow) or through the fingerprint gate below.
    items, unparsed = cl.scan_library(root, movies_dir, tv_dir)
    on_disk = {item.file_name for item in items}

    rows = catalogue_rows(container)
    vanished = vanished_names(rows, on_disk)

    if dry_run:
        # The import's own verdict on the other direction: it is the thing that
        # knows what a complete item looks like.
        missing = cl.build_report(items, unparsed, facts)
        for path in missing.unparsed:
            print(f"  skipped      {path}: no season/episode in the name, so not guessed at", file=stream)
        for name in missing.missing_rows:
            print(f"  missing      {name}: no cb_video row — the library item is invisible", file=stream)
        for name in vanished:
            print(
                f"  vanished     {name}: its source is gone from the media root — "
                f"{'hide' if hide else 'delete'} it",
                file=stream,
            )
        if not (missing.drift or unparsed or vanished):
            print(f"clipbucket-sync: in sync ({len(on_disk)} item(s), {len(rows)} catalogue row(s))")
            return 0
        print(
            f"clipbucket-sync: {len(missing.missing_rows)} missing from the catalogue, "
            f"{len(missing.missing_media) + len(missing.missing_thumbs)} incomplete, "
            f"{len(missing.converted)} to re-copy, {len(vanished)} whose source is gone, "
            f"{len(unparsed)} skipped — run --apply",
            file=sys.stderr,
        )
        return 1

    # A library that is entirely absent is not a library that was emptied. The
    # import refuses to guess here too; so does hiding, and for a worse reason —
    # this is the one operation that could hide every film at once.
    if not on_disk:
        print(
            f"clipbucket-sync: nothing under {root} ({movies_dir}/, {tv_dir}/) — refusing to "
            "touch the catalogue (is the library mounted?)",
            file=sys.stderr,
        )
        return 2

    for name in vanished:
        verb = "hide" if hide else "delete"
        print(f"  vanished     {name}: its source is gone from the media root — {verb} it", file=stream)

    if vanished:
        if hide:
            set_active(container, vanished, "no")
            print(f"clipbucket-sync: hid {len(vanished)} vanished row(s)", file=sys.stderr)
        else:
            files = remove_items(container, facts, {n: rows[n][0] for n in vanished})
            print(
                f"clipbucket-sync: deleted {len(vanished)} vanished item(s) "
                f"({files} file(s)/directory(ies) removed)",
                file=sys.stderr,
            )

    # THE TRIGGER IS THE FINGERPRINT ALONE, deliberately. `missing_rows` would
    # also work — an item on disk with no row is the add case — but it counts a
    # file the fingerprint is still holding back as unsettled, so it would import
    # the half-written film the settle window exists to keep out. Once that file
    # settles, the fingerprint changes and the next run picks it up.
    # Faults that are NOT changes (`converted` drift the import cannot repair)
    # must not re-trigger either, or every run rewrites the same media forever.
    unchanged = fingerprint(root, movies_dir, tv_dir, settle, time.time()) == read_state(state)
    if not unchanged or force:
        print("clipbucket-sync: running the import (scripts/clipbucket-library.py --apply)", file=sys.stderr)
        proc = subprocess.run(
            [sys.executable, _SIBLING, "--apply", "--media-root", root,
             "--movies-dir", movies_dir, "--tv-dir", tv_dir],
            timeout=IMPORT_TIMEOUT,
        )
        if proc.returncode != 0:
            print(
                f"clipbucket-sync: the import exited {proc.returncode} — the catalogue may be "
                "partially converged (see its output above)",
                file=sys.stderr,
            )
            return 1
    else:
        print("clipbucket-sync: library unchanged — nothing to import", file=sys.stderr)

    write_state(state, fingerprint(root, movies_dir, tv_dir, settle, time.time()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep ClipBucket's catalogue in step with the Jellyfin library.",
        epilog=(
            "Exit: 0 in sync/applied · 1 drift (--check) or the import failed · 2 cannot "
            "judge. A vanished item is DELETED (rows + media + thumbnails); --hide "
            "only hides it (active='no') so the next import can bring it back."
        ),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="report drift both ways, write nothing")
    action.add_argument("--apply", action="store_true", help="converge the catalogue")
    parser.add_argument("--media-root", default=os.environ.get("CLIPBUCKET_MEDIA_ROOT", cl.MEDIA_ROOT_DEFAULT))
    parser.add_argument("--movies-dir", default=cl.MOVIES_SUBDIR_DEFAULT)
    parser.add_argument("--tv-dir", default=cl.TV_SUBDIR_DEFAULT)
    parser.add_argument("--container", default=os.environ.get("CLIPBUCKET_CONTAINER", ""))
    parser.add_argument("--state-file", default=STATE_DEFAULT, help="where the library fingerprint lives")
    parser.add_argument("--settle", type=int, default=SETTLE_DEFAULT,
                        help="seconds a file must be untouched before it counts (default 180)")
    parser.add_argument("--force", action="store_true", help="import even when the library is unchanged")
    parser.add_argument("--hide", action="store_true",
                        help="only hide vanished items (active='no'); the default deletes them")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    container = cl.resolve_container(args.container or cl.CONTAINER_DEFAULT)
    if not container:
        print("clipbucket-sync: no running clipbucket container — cannot judge (pass --container)", file=sys.stderr)
        return 2
    facts = {"container": container, "files_path": cl.mount_source(container, cl.FILES_VOLUME_DEST)}
    if not os.path.isdir(args.media_root):
        print(f"clipbucket-sync: the media root {args.media_root} is not a directory — cannot judge", file=sys.stderr)
        return 2

    stream = sys.stderr if args.quiet else sys.stdout
    try:
        return converge(
            facts, args.force, args.hide, args.state_file,
            args.media_root, args.movies_dir, args.tv_dir, args.settle,
            dry_run=args.check, stream=stream,
        )
    except cl.ImportError_ as exc:  # the container/DB answered with a problem
        print(f"clipbucket-sync: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
