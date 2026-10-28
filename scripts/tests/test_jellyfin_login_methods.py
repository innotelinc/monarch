#!/usr/bin/env python3
"""Unit tests for jellyfin-login-methods.py — Jellyfin's own user store.

The case these encode: Cerulean Authentik is the identity, the LDAP outpost is the
credential store behind Jellyfin's sign-in page, and the local accounts Monarch
keeps on purpose are the break-glass `admin` plus whatever the deployment declares
in `JELLYFIN_LOCAL_ACCOUNTS` (the TV/native clients, which cannot run a browser
OIDC flow). Every other account Jellyfin holds locally is a second password
outside Authentik, so it is reported and — with `--apply` — disabled.

A fake Jellyfin answers the two calls the script makes (`GET /Users`,
`POST /Users/{id}/Policy`), so the suite runs anywhere, including CI. Three
properties are pinned because they are the ones that would be wrong quietly:

  * `--check` fails while a stray local account can sign in, and passes when the
    only ones left are the declared ones — including that a declared name is
    matched case-insensitively and is never disabled by `--apply`, because a
    deployment that declares an account and then has the tool disable it is worse
    than one that never declared it;
  * `--apply` **replaces** a policy (`POST /Users/{id}/Policy` is not a merge), so
    the body must carry the account's whole existing policy with `IsDisabled`
    changed — a body holding only `IsDisabled` strips library access, parental
    settings and the enabled-folders list as a side effect;
  * a build that does not report `AuthenticationProviderId` exits 2 instead of
    passing, because "every account is external" and "no account says which
    provider owns it" are different facts and only one of them is good news.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parents[1] / "jellyfin-login-methods.py"

DEFAULT_PROVIDER = "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
LDAP_PROVIDER = "Jellyfin.Plugin.LDAP_Auth.AuthenticationProvider"


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_login_methods", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


jellyfin = _load()

API_KEY = "test-api-key"


def account(name, *, provider=DEFAULT_PROVIDER, admin=False, disabled=False, top_level=False, **policy):
    """A `UserDto` as `GET /Users` serialises one.

    Jellyfin 12 keeps the owning provider inside `Policy`, which is the default
    here; `top_level=True` is the older shape, where the field is a key of the
    account itself.
    """
    body = {"IsAdministrator": admin, "IsDisabled": disabled}
    if provider is not None:
        body["AuthenticationProviderId"] = provider
    body.update(policy)
    user = {
        "Id": f"id-{name}",
        "Name": name,
        "Policy": body,
    }
    if provider is not None and top_level:
        user["AuthenticationProviderId"] = provider
    return user


class FakeJellyfin:
    """A Jellyfin that only knows the two calls the script makes."""

    def __init__(self, users: list[dict], *, applied: bool = True, authorize: bool = True):
        self.users = users
        self.applied = applied
        self.authorize = authorize
        self.policies: list[tuple[str, dict]] = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, payload, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization") or ""
                # This build reads the token from Authorization only.
                return fake.authorize and API_KEY in header

            def do_GET(self):
                if not self._authorized():
                    return self._respond({"message": "unauthorized"}, 401)
                if self.path != "/Users":
                    return self._respond({"message": "not found"}, 404)
                return self._respond(fake.users)

            def do_POST(self):
                if not self._authorized():
                    return self._respond({"message": "unauthorized"}, 401)
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "Users" or parts[2] != "Policy":
                    return self._respond({"message": "not found"}, 404)
                fake.policies.append((parts[1], body))
                for user in fake.users:
                    if user["Id"] == parts[1] and fake.applied:
                        # The endpoint replaces the policy with what it is given.
                        user["Policy"] = body
                return self._respond(None, 204)

            def log_message(self, *_args):
                pass

        return Handler


class JellyfinLoginMethods(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "jellyfin-api-key.txt"
        self.key_file.write_text(API_KEY + "\n", encoding="utf-8")
        self.servers: list[FakeJellyfin] = []
        self.env = {}

    def tearDown(self) -> None:
        for server in self.servers:
            server.stop()
        self.tmp.cleanup()

    def fake(self, users, **kwargs) -> FakeJellyfin:
        server = FakeJellyfin(users, **kwargs)
        self.servers.append(server)
        return server

    def run_script(self, *args: str) -> tuple[int, str, str]:
        """Call main() directly with the test's key file, capturing both streams."""
        import io

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jellyfin.main(["--key-file", str(self.key_file), *args])
        return code, out.getvalue(), err.getvalue()

    # ── the posture ────────────────────────────────────────────────────────

    def test_passes_when_only_the_break_glass_account_is_local(self):
        server = self.fake([account("admin"), account("ana", provider=LDAP_PROVIDER)])

        code, out, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 0)
        self.assertIn("only accounts that can sign in locally are the 1 this deployment "
                      "declares", out)
        self.assertEqual(server.policies, [])

    def test_a_declared_local_account_is_not_a_stray(self):
        # The TV/native-client case: Jellyfin's own store is the only thing the
        # client can sign in against, so the deployment declares the account.
        server = self.fake([
            account("admin"),
            account("tvbox"),
            account("ana", provider=LDAP_PROVIDER),
        ])

        code, out, _ = self.run_script("--url", server.url, "--also-local", "tvbox")

        self.assertEqual(code, 0)
        self.assertIn("declared in the deployment: 2 (admin, tvbox)", out)
        self.assertEqual(server.policies, [])

    def test_a_declared_local_account_is_matched_case_insensitively(self):
        # Jellyfin's account names are case-insensitive; a declaration that
        # stopped matching on case would read as a stray and get disabled.
        server = self.fake([account("admin"), account("TVBox")])

        code, _, _ = self.run_script("--url", server.url, "--also-local", "tvbox")

        self.assertEqual(code, 0)

    def test_apply_never_disables_a_declared_local_account(self):
        server = self.fake([account("admin"), account("tvbox"), account("stray")])

        code, _, _ = self.run_script("--apply", "--url", server.url,
                                     "--also-local", "tvbox")

        self.assertEqual(code, 0)
        self.assertEqual([user_id for user_id, _ in server.policies], ["id-stray"])

    def test_an_undeclared_local_account_suggests_declaring_it(self):
        # The remediation for a client that cannot use the OIDC button is a
        # declaration, not a disable — saying only "re-run with --apply" would
        # talk an operator into locking their TV out.
        server = self.fake([account("admin"), account("tvbox")])

        code, out, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 1)
        self.assertIn("JELLYFIN_LOCAL_ACCOUNTS", out)

    def test_fails_while_a_stray_local_account_can_sign_in(self):
        server = self.fake([
            account("admin"),
            account("old-wizard", admin=True),
            account("ana", provider=LDAP_PROVIDER),
        ])

        code, out, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 1)
        self.assertIn("local sign-in enabled for 'old-wizard'", out)
        self.assertIn("an administrator", out)
        self.assertIn("re-run with --apply", out)
        # Reporting is not acting.
        self.assertEqual(server.policies, [])

    def test_an_ldap_backed_administrator_is_not_a_local_account(self):
        server = self.fake([account("admin"), account("ops", provider=LDAP_PROVIDER, admin=True)])

        code, _, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 0)

    def test_a_build_that_still_puts_the_provider_on_the_account_is_read_too(self):
        # The pre-12 shape. Reading only one of the two places would call an
        # Authentik-backed account local and offer to disable it.
        server = self.fake([
            account("admin", top_level=True),
            account("ops", provider=LDAP_PROVIDER, top_level=True),
        ])

        code, out, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 0)
        self.assertIn("external", out)

    def test_a_local_account_already_disabled_is_left_alone(self):
        # Not a way in, and possibly a directory user's history waiting to be
        # handed back to the LDAP provider.
        server = self.fake([account("admin"), account("retired", disabled=True)])

        code, out, _ = self.run_script("--url", server.url)

        self.assertEqual(code, 0)
        self.assertIn("retired", out)
        self.assertEqual(server.policies, [])

    def test_the_break_glass_account_is_never_disabled(self):
        server = self.fake([account("admin", admin=True)])

        code, _, _ = self.run_script("--apply", "--url", server.url)

        self.assertEqual(code, 0)
        self.assertEqual(server.policies, [])

    # ── --apply ────────────────────────────────────────────────────────────

    def test_apply_disables_the_stray_and_carries_its_whole_policy_back(self):
        server = self.fake([
            account("admin"),
            account("stray", parentId="p-1", EnabledFolders=["3", "7"]),
        ])

        code, out, _ = self.run_script("--apply", "--url", server.url)

        self.assertEqual(code, 0)
        self.assertEqual([user_id for user_id, _ in server.policies], ["id-stray"])
        sent = server.policies[0][1]
        self.assertIs(sent["IsDisabled"], True)
        # The endpoint replaces the policy, so everything else has to travel with it.
        self.assertEqual(sent["EnabledFolders"], ["3", "7"])
        self.assertEqual(sent["parentId"], "p-1")
        self.assertIn("disabled 1 local account(s)", out)

    def test_apply_reads_back_rather_than_trusting_the_write(self):
        # A server that accepts the request and drops the field is exactly the
        # failure this exists to catch.
        server = self.fake([account("admin"), account("stray")], applied=False)

        code, _, err = self.run_script("--apply", "--url", server.url)

        self.assertEqual(code, 1)
        self.assertIn("still enabled: 'stray'", err)

    def test_apply_disables_every_stray_not_just_the_first(self):
        server = self.fake([account("admin"), account("one"), account("two")])

        code, _, _ = self.run_script("--apply", "--url", server.url)

        self.assertEqual(code, 0)
        self.assertEqual(sorted(user_id for user_id, _ in server.policies), ["id-one", "id-two"])

    # ── cannot run ─────────────────────────────────────────────────────────

    def test_a_build_that_does_not_report_the_provider_is_not_a_pass(self):
        # Neither source says: not a pass, and not a "no local accounts" either.
        server = self.fake([
            account("admin", provider=None),
            account("mystery", provider=None),
        ])

        code, out, err = self.run_script(
            "--url", server.url, "--db", str(Path(self.tmp.name) / "absent.db"))

        self.assertEqual(code, 2)
        self.assertIn("cannot judge", err)
        self.assertIn("AuthenticationProviderId", err)
        self.assertNotIn("only account that can sign in locally", out)

    def test_the_database_supplies_the_provider_when_the_api_is_silent(self):
        """Jellyfin 12 drops the field from the DTO; the column survives.

        Measured on this deployment (12.0.0): a UserDto carries Configuration,
        EnableAutoLogin, HasConfiguredEasyPassword, HasConfiguredPassword,
        HasPassword, Id, LastActivityDate, LastLoginDate, Name, Policy, ServerId
        — no provider at all. Without the database fallback the posture cannot be
        enforced on a current build, which is how the local accounts stayed local.
        """
        db = Path(self.tmp.name) / "jellyfin.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE Users (NormalizedUsername TEXT, Username TEXT, "
            "AuthenticationProviderId TEXT)"
        )
        connection.executemany(
            "INSERT INTO Users VALUES (?, ?, ?)",
            [
                ("admin", "admin", DEFAULT_PROVIDER),
                ("ana", "ana", LDAP_PROVIDER),
                ("stray", "STRAY", DEFAULT_PROVIDER),
            ],
        )
        connection.commit()
        connection.close()

        server = self.fake([
            account("admin", provider=None),
            account("ana", provider=None),
            account("stray", provider=None),
        ])

        code, out, _ = self.run_script("--url", server.url, "--db", str(db))

        # Found from the database, so the strays are visible and the check fails
        # for the real reason rather than "cannot judge".
        self.assertEqual(code, 1)
        self.assertIn("local sign-in enabled for 'stray'", out)
        self.assertIn("read from", out)
        # Matched case-insensitively — NormalizedUsername is not the display name.
        self.assertNotIn("local sign-in enabled for 'ana'", out)

    def test_apply_uses_the_database_provider_too(self):
        db = Path(self.tmp.name) / "jellyfin.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE Users (NormalizedUsername TEXT, Username TEXT, "
            "AuthenticationProviderId TEXT)"
        )
        connection.executemany(
            "INSERT INTO Users VALUES (?, ?, ?)",
            [("admin", "admin", DEFAULT_PROVIDER), ("stray", "stray", DEFAULT_PROVIDER)],
        )
        connection.commit()
        connection.close()

        server = self.fake([account("admin", provider=None), account("stray", provider=None)])

        code, _, _ = self.run_script("--apply", "--url", server.url, "--db", str(db))

        self.assertEqual(code, 0)
        self.assertEqual([user_id for user_id, _ in server.policies], ["id-stray"])

    def test_a_database_that_says_nothing_does_not_become_a_pass(self):
        db = Path(self.tmp.name) / "jellyfin.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE Users (NormalizedUsername TEXT, Username TEXT, "
            "AuthenticationProviderId TEXT)"
        )
        connection.execute("INSERT INTO Users VALUES ('mystery', 'mystery', NULL)")
        connection.commit()
        connection.close()

        server = self.fake([account("admin", provider=None), account("mystery", provider=None)])

        code, _, err = self.run_script("--url", server.url, "--db", str(db))

        self.assertEqual(code, 2)
        self.assertIn("cannot judge", err)

    def test_a_rejected_key_says_how_to_re_mint_it(self):
        server = self.fake([account("admin")], authorize=False)

        code, _, err = self.run_script("--url", server.url)

        self.assertEqual(code, 2)
        self.assertIn("rejected the admin API key", err)
        self.assertIn("jellyfin-admin-password.py --set", err)

    def test_an_unreachable_jellyfin_is_cannot_run(self):
        code, _, err = self.run_script("--url", "http://127.0.0.1:1")

        self.assertEqual(code, 2)
        self.assertIn("cannot reach Jellyfin", err)

    def test_no_key_anywhere_is_cannot_run(self):
        missing = Path(self.tmp.name) / "absent.txt"

        import io

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jellyfin.main(["--key-file", str(missing), "--url", "http://127.0.0.1:1"])

        self.assertEqual(code, 2)
        self.assertIn("no Jellyfin API key", err.getvalue())

    def test_no_accounts_is_cannot_run(self):
        server = self.fake([])

        code, _, err = self.run_script("--url", server.url)

        self.assertEqual(code, 2)
        self.assertIn("reported no accounts", err)


class Classifying(unittest.TestCase):
    """The classifier itself, without a server in the way."""

    def test_the_default_provider_is_local(self):
        self.assertTrue(jellyfin.is_local(DEFAULT_PROVIDER))

    def test_a_plugin_provider_is_not_local(self):
        self.assertFalse(jellyfin.is_local(LDAP_PROVIDER))

    def test_a_provider_id_is_read_in_either_spelling(self):
        self.assertEqual(jellyfin.provider_of({"AuthenticationProviderId": " x "}), "x")
        self.assertEqual(jellyfin.provider_of({"authenticationProviderId": "y"}), "y")
        self.assertIsNone(jellyfin.provider_of({"Name": "ana"}))

    def test_the_policy_is_asked_before_the_account_itself(self):
        """Jellyfin 12 serialises the field in `Policy`, not on the account."""
        self.assertEqual(
            jellyfin.provider_of({
                "Policy": {"AuthenticationProviderId": LDAP_PROVIDER},
                "Name": "ana",
            }),
            LDAP_PROVIDER,
        )
        # And the account is still read when the policy says nothing about it.
        self.assertEqual(
            jellyfin.provider_of({
                "Policy": {"IsAdministrator": False},
                "authenticationProviderId": "z",
            }),
            "z",
        )

    def test_an_absent_provider_is_unknown_rather_than_local(self):
        self.assertIsNone(jellyfin.Account({"Name": "ana"}).local)

    def test_an_unreadable_database_returns_no_providers(self):
        self.assertEqual(jellyfin.providers_from_db(Path("/nonexistent/jellyfin.db")), {})


if __name__ == "__main__":
    unittest.main()
