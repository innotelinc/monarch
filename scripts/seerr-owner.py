#!/usr/bin/env python3
"""seerr-owner.py — the Owner is a row id, so hand it to the account people use.

WHY THIS EXISTS
---------------
Seerr has exactly one **Owner**, and it is not a permission. Measured in the image
this stack runs (`ghcr.io/seerr-team/seerr`, v3.4.1, commit 69f73a6):

    server/routes/user/index.ts
      canMakePermissionsChange():
        // Only let the owner grant admin privileges
        !(hasPermission(Permission.ADMIN, permissions) && user?.id !== 1)
      PUT /      (bulk):  const isOwner = req.user?.id === 1;
      PUT /:id:           if (user.id === 1 && req.user?.id !== 1) -> refused
                          // Only let the owner user modify themselves

`Permission.ADMIN` is a capability, but the Owner badge — and the right to grant
it to anybody else — follows `user.id === 1`, which is whatever account completed
setup first. No endpoint moves it, so an install whose first account was a
break-glass admin (or an account nobody signs in as) is stuck with that account
as the only Owner, and `localLogin: false` may make it unreachable entirely.

The same image is the reason this is a database write and not a call:
`src/components/Settings/Users` has no owner field to set, and the users API
exposes `permissions` only — `Owner` is derived from the row.

WHAT IT DOES
------------
SWAPS the two accounts instead of deleting or renumbering either one:

* every `user` column except `id` is exchanged between the current Owner (id 1)
  and `--account` — identity (jellyfinUserId/username/email/avatar), tokens,
  quotas, `createdAt` and `permissions` all move with the account;
* every foreign key that points at either row is exchanged with it, discovered
  from `PRAGMA foreign_key_list` rather than listed here, so a table added by a
  future Seerr release follows the identity too (`media_request.requestedById`,
  `watchlist.requestedById`, `user_settings.userId`, ...);
* live sessions are re-pointed the same way, because a session belongs to an
  identity: without this, whoever is signed in would silently become the other
  account the moment the swap commits.

After it, `--account` **is** id 1: Owner, keeping its own requests and history.
The account that held id 1 keeps its row id and its data as a normal user, which
is the point — that account is usually the break-glass admin and it has to keep
working.

Idempotent: it reports and changes nothing when the named account already owns
Seerr, so it is safe in a timer.

USAGE
-----
    python3 scripts/seerr-owner.py --check                  # who owns Seerr (default)
    python3 scripts/seerr-owner.py --apply                  # hand it to --account
    python3 scripts/seerr-owner.py --apply --account dhunter
    python3 scripts/seerr-owner.py --db /path/db.sqlite3 --check

Runs on the media host, where Seerr's database is bind-mounted at
`/docker/appdata/jellyseerr/db/db.sqlite3`. Seerr may be running: the writes are
one transaction (SQLite WAL).

Exit codes: 0 = the named account owns Seerr (or was just made the Owner by
--apply), 1 = check mode found somebody else, 2 = cannot run (no database, no
such account).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("SEERR_DB", "/docker/appdata/jellyseerr/db/db.sqlite3"))
# What init/init.py writes and scripts/drift-check.sh reads: the one place the
# account this estate wants as Seerr's owner is named.
DEFAULT_MANIFEST = Path(os.environ.get("MONARCH_INVARIANTS",
                                       "/docker/appdata/init/invariants.json"))
DEFAULT_ACCOUNT = os.environ.get("MONARCH_SEERR_OWNER", "dhunter")
# The Owner is the first user row. `server/routes/user/index.ts` compares against
# this literal, so it is the contract, not a preference.
OWNER_ID = 1
# Permission.ADMIN (server/lib/permissions.ts). Reported, never granted: the swap
# exchanges capabilities along with the accounts.
ADMIN_PERMISSION = 2
# Unique columns cannot be written over one another directly, so each row parks
# its identity on one of these for the length of the transaction.
SENTINEL = "__seerr-owner-swap-{}__"
# Columns that name the account, in the order a person would recognise it.
NAME_COLUMNS = ("jellyfinUsername", "username", "email")


class SeerrError(RuntimeError):
    pass


def default_account(manifest: Path | None = None) -> str:
    """The account that should own Seerr, from init's invariants manifest.

    Read rather than repeated: the manifest is what `monarch-init` writes and what
    `drift-check.sh` judges, so the name in the check is the name that was asked
    for. The env var and then the literal are the fallbacks, so the script still
    runs against a bare database on a host that has no manifest yet.
    """
    try:
        payload = json.loads(Path(manifest or DEFAULT_MANIFEST).read_text(encoding="utf-8"))
        wanted = str((payload.get("jellyseerr") or {}).get("owner") or "").strip()
    except (OSError, ValueError, AttributeError):
        wanted = ""
    return wanted or DEFAULT_ACCOUNT


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SeerrError(
            f"no Seerr database at {path} — pass --db, or run this on the media host"
        )
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def unique_columns(db: sqlite3.Connection, table: str) -> list[str]:
    """Columns under a UNIQUE constraint — `email`, on Seerr's user table."""
    found: list[str] = []
    for index in db.execute(f'PRAGMA index_list("{table}")'):
        if not index[2]:
            continue
        for info in db.execute(f'PRAGMA index_info("{index[1]}")'):
            if info[2] and info[2] not in found:
                found.append(info[2])
    return found


def user_references(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every (table, column) declared as a foreign key onto `user.id`.

    Discovered, not listed: the point of swapping rather than renumbering is that
    every reference follows the identity, and a hardcoded list is how the next
    release's new table (or `user_settings`) gets missed.
    """
    references: list[tuple[str, str]] = []
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        table = row[0]
        for fk in db.execute(f'PRAGMA foreign_key_list("{table}")'):
            if fk[2] == "user":
                references.append((table, fk[3]))
    return references


def account_row(db: sqlite3.Connection, account: str) -> int | None:
    """The user id whose Jellyfin name, Seerr username or email is `account`."""
    fields = [name for name in NAME_COLUMNS if name in columns(db, "user")]
    if not fields:
        raise SeerrError("the user table has none of the columns this matches on")
    for row in db.execute(f'SELECT id, {", ".join(fields)} FROM "user"'):
        for field in fields:
            if str(row[field] or "").strip().lower() == account:
                return int(row["id"])
    return None


def identity(db: sqlite3.Connection, user_id: int) -> dict | None:
    row = db.execute('SELECT * FROM "user" WHERE id=?', (user_id,)).fetchone()
    return dict(row) if row is not None else None


def describe(row: dict) -> str:
    name = next((str(row.get(c) or "").strip() for c in NAME_COLUMNS if str(row.get(c) or "").strip()), "")
    permissions = int(row.get("permissions") or 0)
    admin = "ADMIN" if permissions & ADMIN_PERMISSION else "no admin"
    return (f"id {row['id']:<3} {name or '(no name)':<14} "
            f"type {row.get('userType')}  permissions {permissions} ({admin})")


def exchange_ids(db: sqlite3.Connection, table: str, column: str,
                 owner_id: int, target_id: int) -> int:
    """Point every row at the other account: owner -> target, target -> owner.

    Done in three statements through -1 rather than a `CASE ... WHEN`, so a table
    carrying a UNIQUE constraint on the column (Seerr's `user_settings.userId`
    does) cannot collide with itself halfway through.
    """
    where = f'"{column}"=?'
    moved = db.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IN (?,?)',
        (owner_id, target_id),
    ).fetchone()[0]
    if not moved:
        return 0
    db.execute(f'UPDATE "{table}" SET "{column}"=-1 WHERE {where}', (owner_id,))
    db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE {where}', (owner_id, target_id))
    db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE {where}', (target_id, -1))
    return moved


def reassign_sessions(db: sqlite3.Connection, owner_id: int, target_id: int) -> int:
    """Re-point the sessions of both accounts, so a sign-in keeps its identity."""
    if not table_exists(db, "session"):
        return 0
    moved = 0
    for row in db.execute('SELECT id, json FROM "session"').fetchall():
        try:
            payload = json.loads(row["json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        signed_in = payload.get("userId")
        if signed_in not in (owner_id, target_id):
            continue
        payload["userId"] = target_id if signed_in == owner_id else owner_id
        db.execute('UPDATE "session" SET json=? WHERE id=?', (json.dumps(payload), row["id"]))
        moved += 1
    return moved


def swap_accounts(db: sqlite3.Connection, target_id: int,
                  owner_id: int = OWNER_ID) -> dict[str, int]:
    """Exchange the two accounts, in one transaction. Returns what moved."""
    fields = columns(db, "user")
    select = ", ".join(f'"{field}"' for field in fields)
    saved = {
        uid: dict(db.execute(f'SELECT {select} FROM "user" WHERE id=?', (uid,)).fetchone())
        for uid in (owner_id, target_id)
    }
    exchanged = [field for field in fields if field != "id"]
    unique = unique_columns(db, "user")
    moved: dict[str, int] = {}

    db.execute("BEGIN")
    try:
        # The rows cannot simply be written over each other: `email` is UNIQUE, so
        # each row parks its identity on a sentinel first and the copies then
        # cannot collide. Deferred keys let the foreign keys land on both sides
        # mid-transaction (and are a no-op where enforcement is off).
        db.execute("PRAGMA defer_foreign_keys = ON")
        for uid, tag in ((owner_id, "a"), (target_id, "b")):
            for field in unique:
                db.execute(f'UPDATE "user" SET "{field}"=? WHERE id=?',
                           (SENTINEL.format(tag), uid))
        for field in exchanged:
            if field in unique:
                continue
            db.execute(f'UPDATE "user" SET "{field}"=? WHERE id=?',
                       (saved[target_id][field], owner_id))
            db.execute(f'UPDATE "user" SET "{field}"=? WHERE id=?',
                       (saved[owner_id][field], target_id))
        for field in unique:
            db.execute(f'UPDATE "user" SET "{field}"=? WHERE id=?',
                       (saved[target_id][field], owner_id))
            db.execute(f'UPDATE "user" SET "{field}"=? WHERE id=?',
                       (saved[owner_id][field], target_id))

        # The activity follows the identity, not the row number.
        for table, column in user_references(db):
            count = exchange_ids(db, table, column, owner_id, target_id)
            if count:
                moved[f"{table}.{column}"] = count
        sessions = reassign_sessions(db, owner_id, target_id)
        if sessions:
            moved["session.json"] = sessions
        db.commit()
    except Exception:
        db.rollback()
        raise
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="hand the Owner account to --account (default: report only)")
    parser.add_argument("--check", action="store_true",
                        help="report only, the default")
    parser.add_argument("--account", default=default_account(),
                        help="the account that should own Seerr (default: the account "
                             f"in {DEFAULT_MANIFEST}, else {DEFAULT_ACCOUNT!r})")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"Seerr's database (default {DEFAULT_DB})")
    args = parser.parse_args(argv)
    account = args.account.strip().lower()

    print(f"seerr-owner [{'apply' if args.apply else 'check'}] {args.db}")
    try:
        db = connect(args.db)
        owner = identity(db, OWNER_ID)
    except SeerrError as error:
        print(f"seerr-owner: {error}", file=sys.stderr)
        return 2
    if owner is None:
        print("seerr-owner: no user row 1 — Seerr has not been set up yet", file=sys.stderr)
        return 2

    try:
        target = account_row(db, account)
    except SeerrError as error:
        print(f"seerr-owner: {error}", file=sys.stderr)
        return 2

    print(f"  Owner        {describe(owner)}")
    if target is None:
        print(f"seerr-owner: no Seerr account matches {args.account!r} — the account "
              f"has to sign in once (Jellyfin/Cerulean) before it can own Seerr",
              file=sys.stderr)
        return 2
    if target != OWNER_ID:
        print(f"  {args.account:<12} {describe(identity(db, target))}")

    if target == OWNER_ID:
        print(f"\nok: {args.account!r} is the Owner"
              + ("" if int(owner.get("permissions") or 0) & ADMIN_PERMISSION
                 else " — note: the owner row has no ADMIN permission"))
        return 0

    if not args.apply:
        print(f"\n{args.account!r} is not the Owner: the badge and the right to grant "
              f"admin follow row id 1.\n"
              f"re-run with --apply to swap the two accounts (nothing is deleted).")
        return 1

    try:
        moved = swap_accounts(db, target)
    except sqlite3.Error as error:
        print(f"seerr-owner: the swap failed and was rolled back ({error})", file=sys.stderr)
        return 1

    print(f"\nswapped: {args.account!r} is now id {OWNER_ID} (Owner), and the account that "
          f"held it is id {target}")
    for what, count in sorted(moved.items()):
        print(f"  {count:>3} row(s) re-pointed  {what}")

    after = account_row(db, account)
    if after != OWNER_ID:
        print(f"seerr-owner: the write did not take (row {after})", file=sys.stderr)
        return 1
    print(f"  Owner        {describe(identity(db, OWNER_ID))}")
    print(f"  id {target:<9} {describe(identity(db, target))}")
    print(f"\nok: {args.account!r} owns Seerr; sign in again through Jellyfin/Cerulean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
