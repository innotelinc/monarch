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

   The repair is **hide, not delete** — `active='no'`, which is the switch the
   app's own browse query reads, so the item leaves the site and nothing is
   destroyed. It also has to be that: the import hardlinks the source into the
   file volume, so "the source is gone" and "the bytes are gone" are different
   facts, and a library that is unmounted for a minute must not become a database
   that has forgotten its films. Re-activating is automatic — the file coming
   back changes the fingerprint, the import re-runs, and `active` goes back to
   `yes`. `--remove` does the destructive version for the one case that wants it.

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
    python3 scripts/clipbucket-sync.py --apply --remove       # delete vanished rows outright

Driven by `systemd/monarch-clipbucket-sync.timer`; see docs/operations.md.

Exit codes: 0 in sync (or applied) · 1 drift (with `--check`) · 2 cannot judge.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
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
def catalogue_rows(container: str) -> dict[str, bool]:
    """`file_name -> active` for every row this tool owns, read once.

    Scoped by `file_directory` — the same scope the import writes — so a video
    added by hand in the app's own UI is left alone rather than hidden for the
    crime of not being in `/data/media`.
    """
    rows = cl.mysql_query(
        container,
        "SELECT file_name, active FROM cb_video "
        f"WHERE file_directory='{cl.MEDIA_DIR}';",
    )
    return {row[0]: (row[1] == "yes" if len(row) > 1 else True) for row in rows}


def set_active(container: str, names: list[str], active: str) -> None:
    quoted = ", ".join(f"'{cl.sql_quote(n)}'" for n in names)
    cl.mysql_exec(
        container,
        f"UPDATE cb_video SET active='{active}' WHERE file_directory='{cl.MEDIA_DIR}' "
        f"AND file_name IN ({quoted});",
    )


def converge(facts: dict, force: bool, remove: bool, state: str,
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
    # A row whose file is not on disk any more. `active='no'` rows are already
    # hidden, so they are not drift — reporting them again would make a converged
    # host look dirty forever.
    vanished = sorted(name for name, active in rows.items() if active and name not in on_disk)

    if dry_run:
        # The import's own verdict on the other direction: it is the thing that
        # knows what a complete item looks like.
        missing = cl.build_report(items, unparsed, facts)
        for path in missing.unparsed:
            print(f"  skipped      {path}: no season/episode in the name, so not guessed at", file=stream)
        for name in missing.missing_rows:
            print(f"  missing      {name}: no cb_video row — the library item is invisible", file=stream)
        for name in vanished:
            print(f"  vanished     {name}: its source is gone from the media root — hide it", file=stream)
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
        verb = "remove" if remove else "hide"
        print(f"  vanished     {name}: its source is gone from the media root — {verb} it", file=stream)

    if vanished:
        if remove:
            quoted = ", ".join(f"'{cl.sql_quote(n)}'" for n in vanished)
            cl.mysql_exec(
                container,
                f"DELETE FROM cb_video WHERE file_directory='{cl.MEDIA_DIR}' "
                f"AND file_name IN ({quoted});",
            )
            print(f"clipbucket-sync: removed {len(vanished)} vanished row(s)", file=sys.stderr)
        else:
            set_active(container, vanished, "no")
            print(f"clipbucket-sync: hid {len(vanished)} vanished row(s)", file=sys.stderr)

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
            "judge. Deletions are HIDDEN (active='no') unless --remove is given."
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
    parser.add_argument("--remove", action="store_true",
                        help="DELETE vanished rows instead of hiding them (default: hide)")
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
            facts, args.force, args.remove, args.state_file,
            args.media_root, args.movies_dir, args.tv_dir, args.settle,
            dry_run=args.check, stream=stream,
        )
    except cl.ImportError_ as exc:  # the container/DB answered with a problem
        print(f"clipbucket-sync: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
