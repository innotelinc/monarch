#!/usr/bin/env python3
"""Unit tests for seerr-owner.py — handing the Owner account to the account in use.

The failure this pins is a lockout nobody sees coming: Seerr's Owner is
`user.id === 1` and nothing else, so an install whose first account was the
break-glass Jellyfin admin is owned by an account the operator does not use —
and with `localLogin: false` alongside it, an account that can only be reached
by signing in as that identity.

What is checked here:

* the account is found by any of the names a person would use (Jellyfin login
  name, Seerr username, email);
* --check never writes, and answers non-zero while somebody else owns Seerr;
* --apply exchanges the two accounts — identity, permissions and activity — and
  deletes nothing, so the displaced admin keeps working;
* the unique constraint on `email` and the unique one on `user_settings.userId`
  survive a swap that touches both rows at once;
* every foreign key onto `user.id` follows its identity, discovered from the
  schema, including the one inside `session.json`;
* re-running is a no-op, because this is safe to put on a timer.

No Seerr, no docker, no network: the schema is built here, with the constraints
the live database carries.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


owner = _load("seerr_owner", "seerr-owner.py")


# Mirrors the shapes that make the swap non-trivial: a UNIQUE email on `user`,
# a UNIQUE userId on `user_settings`, foreign keys onto `user.id` from two
# tables, and a session whose user id lives inside a JSON blob.
SCHEMA = """
CREATE TABLE "user" (
  "id" integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  "email" varchar NOT NULL,
  "username" varchar,
  "permissions" integer NOT NULL DEFAULT (0),
  "avatar" varchar NOT NULL,
  "createdAt" datetime NOT NULL DEFAULT (CURRENT_TIMESTAMP),
  "updatedAt" datetime NOT NULL DEFAULT (CURRENT_TIMESTAMP),
  "password" varchar,
  "userType" integer NOT NULL DEFAULT (1),
  "jellyfinUsername" varchar,
  "jellyfinAuthToken" varchar,
  "jellyfinUserId" varchar,
  "movieQuotaLimit" integer,
  CONSTRAINT "UQ_email" UNIQUE ("email")
);
CREATE TABLE "media_request" (
  "id" integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  "requestedById" integer REFERENCES "user" ("id") ON DELETE CASCADE,
  "modifiedById" integer REFERENCES "user" ("id") ON DELETE CASCADE,
  "title" varchar
);
CREATE TABLE "watchlist" (
  "id" integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  "requestedById" integer REFERENCES "user" ("id") ON DELETE CASCADE
);
CREATE TABLE "user_settings" (
  "id" integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  "userId" integer REFERENCES "user" ("id") ON DELETE CASCADE,
  "region" varchar,
  CONSTRAINT "UQ_settings_user" UNIQUE ("userId")
);
CREATE TABLE "session" ("id" varchar PRIMARY KEY, "json" varchar);
"""


USER_COLUMNS = ('"id","email","username","permissions","avatar","createdAt","updatedAt",'
                '"password","userType","jellyfinUsername","jellyfinAuthToken",'
                '"jellyfinUserId","movieQuotaLimit"')


def user_row(user_id: int, name: str, permissions: int = 2, token: str = "") -> tuple:
    return (user_id, f"{name}@innotel.us", name, permissions, "/avatar",
            "2026-09-03 06:31:16", "2026-09-03 06:31:16", None, 3,
            name, token, f"jellyfin-{user_id}", None)


def seeded(path: Path, *, second_user_id: int = 3) -> None:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    insert = f'INSERT INTO "user" ({USER_COLUMNS}) VALUES (' + ",".join("?" * 13) + ")"
    db.execute(insert, user_row(1, "admin", token="admin-token"))
    db.execute(insert, user_row(second_user_id, "dhunter", token="dhunter-token"))
    db.execute('INSERT INTO "media_request" ("requestedById","modifiedById","title") '
               'VALUES (?,?,?)', (second_user_id, second_user_id, "Dune"))
    db.execute('INSERT INTO "watchlist" ("requestedById") VALUES (?)', (1,))
    db.execute('INSERT INTO "user_settings" ("userId","region") VALUES (?,?)', (1, "us"))
    db.execute('INSERT INTO "user_settings" ("userId","region") VALUES (?,?)', (second_user_id, "ca"))
    db.execute('INSERT INTO "session" VALUES (?,?)',
               ("sess-owner", json.dumps({"cookie": {"path": "/"}, "userId": 1})))
    db.execute('INSERT INTO "session" VALUES (?,?)',
               ("sess-dhunter", json.dumps({"cookie": {"path": "/"}, "userId": second_user_id})))
    db.commit()
    db.close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite3"
        seeded(self.db)

    def run_owner(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = owner.main(["--db", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def query(self, sql: str, *params):
        db = sqlite3.connect(self.db)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()


class Reporting(Base):
    def test_check_reports_the_current_owner(self) -> None:
        code, out, _ = self.run_owner("--check")
        self.assertEqual(code, 1)
        self.assertIn("id 1", out)
        self.assertIn("admin", out)
        self.assertIn("is not the Owner", out)

    def test_check_writes_nothing(self) -> None:
        before = self.query('SELECT "jellyfinUsername" FROM "user" WHERE id=1')
        self.run_owner("--check", "--account", "dhunter")
        self.assertEqual(before, self.query('SELECT "jellyfinUsername" FROM "user" WHERE id=1'))

    def test_an_unknown_account_cannot_be_made_the_owner(self) -> None:
        code, _, err = self.run_owner("--apply", "--account", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("no Seerr account matches", err)
        self.assertEqual(len(self.query('SELECT id FROM "user"')), 2)

    def test_no_owner_row_is_not_setup(self) -> None:
        db = sqlite3.connect(self.db)
        db.execute('DELETE FROM "user"')
        db.commit()
        db.close()
        code, _, err = self.run_owner("--check")
        self.assertEqual(code, 2)
        self.assertIn("has not been set up", err)


class Swapping(Base):
    def test_apply_makes_the_account_the_owner(self) -> None:
        code, out, _ = self.run_owner("--apply", "--account", "dhunter")
        self.assertEqual(code, 0)
        self.assertIn("dhunter", out)
        self.assertEqual(self.query('SELECT "jellyfinUserId" FROM "user" WHERE id=1'),
                         [("jellyfin-3",)])

    def test_nothing_is_deleted_and_the_admin_keeps_working(self) -> None:
        self.run_owner("--apply", "--account", "dhunter")
        rows = self.query('SELECT id, "jellyfinUsername", "jellyfinAuthToken", permissions '
                          'FROM "user" ORDER BY id')
        self.assertEqual([row[0] for row in rows], [1, 3])
        self.assertEqual(rows[0][1], "dhunter")
        self.assertEqual(rows[1][1], "admin")
        # The displaced owner keeps its own id, token and capability.
        self.assertEqual(rows[1][2], "admin-token")
        self.assertEqual(rows[1][3], 2)

    def test_the_requests_follow_the_person(self) -> None:
        self.run_owner("--apply", "--account", "dhunter")
        self.assertEqual(self.query('SELECT DISTINCT "requestedById" FROM "media_request"'), [(1,)])
        self.assertEqual(self.query('SELECT "requestedById" FROM "watchlist"'), [(3,)])

    def test_two_rows_with_unique_columns_swap_without_a_conflict(self) -> None:
        # Both accounts hold a settings row, and the column is UNIQUE: a naive
        # copy would trip the constraint halfway through.
        self.run_owner("--apply", "--account", "dhunter")
        self.assertEqual(self.query('SELECT "userId", "region" FROM "user_settings" ORDER BY "userId"'),
                         [(1, "ca"), (3, "us")])

    def test_a_signed_in_session_keeps_its_identity(self) -> None:
        self.run_owner("--apply", "--account", "dhunter")
        sessions = dict(self.query('SELECT id, json FROM "session"'))
        self.assertEqual(json.loads(sessions["sess-owner"])["userId"], 3)
        self.assertEqual(json.loads(sessions["sess-dhunter"])["userId"], 1)

    def test_it_is_idempotent(self) -> None:
        self.run_owner("--apply", "--account", "dhunter")
        snapshot = self.query('SELECT * FROM "user" ORDER BY id')
        code, out, _ = self.run_owner("--apply", "--account", "dhunter")
        self.assertEqual(code, 0)
        self.assertIn("is the Owner", out)
        self.assertEqual(snapshot, self.query('SELECT * FROM "user" ORDER BY id'))
        self.assertEqual(self.query('SELECT "requestedById" FROM "watchlist"'), [(3,)])

    def test_the_swap_rolls_back_when_a_write_fails(self) -> None:
        original = owner.exchange_ids

        def boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(owner, "exchange_ids", boom):
            code, _, err = self.run_owner("--apply", "--account", "dhunter")
        self.assertEqual(code, 1)
        self.assertIn("rolled back", err)
        self.assertEqual(self.query('SELECT "jellyfinUsername" FROM "user" WHERE id=1'), [("admin",)])
        self.assertEqual(self.query('SELECT "requestedById" FROM "media_request"'), [(3,)])
        self.assertIs(owner.exchange_ids, original)


class FindingTheAccount(Base):
    def test_matched_by_jellyfin_name_username_or_email(self) -> None:
        db = sqlite3.connect(self.db)
        db.execute('UPDATE "user" SET email=? WHERE id=3', ("d.hunter@innotel.us",))
        db.commit()
        db.close()
        for account in ("dhunter", "DHUNTER", "d.hunter@innotel.us"):
            with self.subTest(account=account):
                code, out, _ = self.run_owner("--check", "--account", account)
                self.assertEqual(code, 1)
                self.assertIn("dhunter", out)


class WhereTheNameComesFrom(unittest.TestCase):
    """One place names the account: the manifest init writes and drift-check reads."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manifest = Path(self.tmp.name) / "invariants.json"

    def test_the_manifest_names_the_account(self) -> None:
        self.manifest.write_text(json.dumps({"jellyseerr": {"owner": "someone"}}), encoding="utf-8")
        self.assertEqual(owner.default_account(self.manifest), "someone")

    def test_a_missing_manifest_falls_back(self) -> None:
        with mock.patch.object(owner, "DEFAULT_ACCOUNT", "fallback"):
            self.assertEqual(owner.default_account(self.manifest), "fallback")

    def test_a_manifest_without_the_field_falls_back(self) -> None:
        self.manifest.write_text(json.dumps({"jellyseerr": {"port": 5055}}), encoding="utf-8")
        with mock.patch.object(owner, "DEFAULT_ACCOUNT", "fallback"):
            self.assertEqual(owner.default_account(self.manifest), "fallback")

    def test_a_manifest_that_is_not_json_falls_back(self) -> None:
        self.manifest.write_text("not json", encoding="utf-8")
        with mock.patch.object(owner, "DEFAULT_ACCOUNT", "fallback"):
            self.assertEqual(owner.default_account(self.manifest), "fallback")


class TheSchemaIsReadNotAssumed(unittest.TestCase):
    def test_foreign_keys_onto_the_user_table_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            seeded(path)
            db = sqlite3.connect(path)
            self.addCleanup(db.close)
            self.assertIn(("media_request", "requestedById"), owner.user_references(db))
            self.assertIn(("watchlist", "requestedById"), owner.user_references(db))
            self.assertIn(("user_settings", "userId"), owner.user_references(db))

    def test_unique_columns_come_from_the_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            seeded(path)
            db = sqlite3.connect(path)
            self.addCleanup(db.close)
            self.assertEqual(owner.unique_columns(db, "user"), ["email"])
            self.assertEqual(owner.unique_columns(db, "user_settings"), ["userId"])


if __name__ == "__main__":
    unittest.main()
