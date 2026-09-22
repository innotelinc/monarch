#!/usr/bin/env python3
"""Unit tests for scripts/clipbucket-install.py.

What is pinned here is everything that would otherwise be discovered by a broken
site: the installer's SQL order, its two substitution vocabularies, the password
hash `pass_code()` defines, and the decision that separates "installed" from
"serving the installer" — which is the state a host was in while its own
operations doc called it migrated.

Nothing here needs docker: the rendering and the verdict are pure functions, and
the plumbing around them is three `docker exec` calls.

Run:  python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "clipbucket_install", REPO / "scripts" / "clipbucket-install.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class SqlRendering(unittest.TestCase):
    def test_the_prefix_placeholder_is_substituted(self):
        self.assertEqual(ci.render_sql("INSERT INTO `{tbl_prefix}users` ..."), "INSERT INTO `cb_users` ...")

    def test_the_database_name_placeholder_is_substituted(self):
        self.assertEqual(ci.render_sql("{dbname}.cb_config"), "clipbucket.cb_config")

    def test_sql_without_placeholders_is_untouched(self):
        text = "CREATE TABLE IF NOT EXISTS `cb_x` (id INT);\n"
        self.assertEqual(ci.render_sql(text), text)

    def test_braces_that_are_not_placeholders_survive(self):
        # JSON-ish payloads live in these files, so an over-eager substitution
        # would corrupt seed data rather than fail loudly.
        text = "INSERT INTO cb_config VALUES ('{tbl_prefix}not_a_placeholder');"
        self.assertEqual(ci.render_sql(text), text.replace("{tbl_prefix}", "cb_"))


class ConfigRendering(unittest.TestCase):
    TEMPLATE = (
        "<?php\n$DBHOST = '_DB_HOST_';\n$DBNAME = '_DB_NAME_';\n$DBUSER = '_DB_USER_';\n"
        "$DBPASS = '_DB_PASS_';\n$DBPORT = '_DB_PORT_';\ndefine('TABLE_PREFIX', '_TABLE_PREFIX_');\n"
    )

    def test_every_placeholder_the_template_names_is_replaced(self):
        rendered = ci.render_config(self.TEMPLATE, "s3cret")
        for leftover in ("_DB_HOST_", "_DB_NAME_", "_DB_USER_", "_DB_PASS_", "_DB_PORT_", "_TABLE_PREFIX_"):
            self.assertNotIn(leftover, rendered)
        self.assertIn("$DBPASS = 's3cret';", rendered)
        self.assertIn("define('TABLE_PREFIX', 'cb_');", rendered)

    def test_a_password_with_sed_metacharacters_survives_verbatim(self):
        """`&` and `/` are exactly why this is Python and not a sed in the apply."""
        password = r"a&b/c\d!e"
        rendered = ci.render_config(self.TEMPLATE, password)
        self.assertIn(f"$DBPASS = '{password}';", rendered)

    def test_the_template_header_is_preserved(self):
        rendered = ci.render_config("<?php\n// Database Host\n$DBHOST = '_DB_HOST_';\n", "x")
        self.assertIn("// Database Host", rendered)


class PasswordHash(unittest.TestCase):
    """`pass_code()` from the app: hash('sha512', $password . $userid . $salt)."""

    def test_matches_a_pinned_vector(self):
        self.assertEqual(
            ci.admin_password_hash("hunter2", 1, "s4lt"),
            "56fe3bc7164b9e8c865573ad6a671c7ebcfe7f525b7d952b64909e6937868d1d"
            "29c88abe55708fa57cba4d8e9f873c25edb1c13dff11c777f4e04f4fc3b25ec8",
        )

    def test_the_userid_is_part_of_the_hash(self):
        self.assertNotEqual(
            ci.admin_password_hash("hunter2", 1, "s4lt"),
            ci.admin_password_hash("hunter2", 2, "s4lt"),
        )

    def test_the_salt_is_part_of_the_hash(self):
        self.assertNotEqual(
            ci.admin_password_hash("hunter2", 1, "s4lt"),
            ci.admin_password_hash("hunter2", 1, "other"),
        )


class VersionRow(unittest.TestCase):
    """The wizard's `version` step: latest.json names the release file to read."""

    def test_version_and_revision_come_from_the_release_file(self):
        self.assertEqual(
            ci.version_row({"stable": "553"}, {"version": "5.5.3", "revision": 187}),
            ("5.5.3", 187),
        )

    def test_a_release_file_without_a_version_falls_back_to_its_name(self):
        # 553 is 5.5.3; the file is named after the version with the dots removed.
        self.assertEqual(ci.version_row({"stable": "553"}, {"revision": 7}), ("5.5.3", 7))

    def test_a_missing_revision_is_zero_not_an_error(self):
        self.assertEqual(ci.version_row({"stable": "553"}, {"version": "5.5.3"}), ("5.5.3", 0))


class SqlOrder(unittest.TestCase):
    """The order is ajax.php's, and two of its steps depend on it."""

    def test_configs_come_before_any_config_is_updated(self):
        self.assertLess(ci.SQL_FILES.index("configs.sql"), ci.SQL_FILES.index("add_admin.sql"))

    def test_categories_come_before_add_admin(self):
        # add_admin.sql looks up the 'Gurus' user category by name.
        self.assertLess(ci.SQL_FILES.index("categories.sql"), ci.SQL_FILES.index("add_admin.sql"))

    def test_structure_is_first_and_the_anonymous_user_is_last(self):
        self.assertEqual(ci.SQL_FILES[0], "structure.sql")
        self.assertEqual(ci.SQL_FILES[-1], "add_anonymous_user.sql")

    def test_every_listed_file_is_one_the_installer_ships(self):
        expected = {
            "structure.sql", "table_version.sql", "configs.sql", "languages.sql",
            "language_ENG.sql", "language_FRA.sql", "language_DEU.sql", "language_POR.sql",
            "language_ESP.sql", "ads_placements.sql", "countries.sql", "email_templates.sql",
            "pages.sql", "user_levels.sql", "categories.sql", "add_admin.sql",
            "add_anonymous_user.sql",
        }
        self.assertEqual(set(ci.SQL_FILES), expected)
        self.assertEqual(len(ci.SQL_FILES), len(expected), "a file is listed twice")


def installed(**overrides) -> dict:
    facts = {
        "docker": True,
        "container_running": True,
        "table_count": 82,
        "config_php": True,
        "admin_password": "$1$abcdef",
        "base_url": "https://tube.innotel.us",
        "version": "5.5.3.187",
        "install_me": False,
        "install_locked": True,
        "serves_installer": False,
    }
    facts.update(overrides)
    return facts


class Verdict(unittest.TestCase):
    def test_a_finished_install_has_no_problems(self):
        self.assertEqual(ci.evaluate(installed()), [])

    def test_a_running_browser_installer_is_a_problem(self):
        # Both halves are named: the file that leaves the door open, and the
        # symptom an operator actually reports.
        problems = ci.evaluate(installed(install_me=True, serves_installer=True))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("install.me" in p for p in problems), problems)
        self.assertTrue(any("cb_install" in p for p in problems), problems)

    def test_a_locked_installer_that_still_redirects_is_still_a_problem(self):
        problems = ci.evaluate(installed(install_me=False, serves_installer=True))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cb_install", problems[0])

    def test_an_empty_schema_is_reported_with_its_repair(self):
        problems = ci.evaluate(installed(table_count=0, config_php=False, admin_password=""))
        self.assertTrue(any("schema is empty" in p for p in problems), problems)
        self.assertTrue(any("--apply" in p for p in problems), problems)

    def test_a_missing_version_row_is_named(self):
        problems = ci.evaluate(installed(version=""))
        self.assertTrue(any("cb_version has no row" in p for p in problems), problems)

    def test_a_missing_config_php_is_named(self):
        problems = ci.evaluate(installed(config_php=False))
        self.assertTrue(any("config.php is missing" in p for p in problems), problems)

    def test_an_admin_without_a_password_is_named(self):
        problems = ci.evaluate(installed(admin_password=""))
        self.assertTrue(any("no password set" in p for p in problems), problems)

    def test_an_empty_base_url_is_named(self):
        problems = ci.evaluate(installed(base_url=""))
        self.assertTrue(any("base_url is empty" in p for p in problems), problems)

    def test_a_half_built_schema_is_a_problem(self):
        problems = ci.evaluate(installed(table_count=3))
        self.assertTrue(any("only 3 tables" in p for p in problems), problems)

    def test_a_stopped_container_cannot_be_judged(self):
        # Not a pass and not a finding: "nothing was evaluated" is its own answer,
        # which is what exit code 2 exists for.
        with self.assertRaises(ci.CantTell):
            ci.evaluate(installed(container_running=False))

    def test_an_unreadable_schema_cannot_be_judged(self):
        with self.assertRaises(ci.CantTell):
            ci.evaluate(installed(table_count=None))


if __name__ == "__main__":
    unittest.main()
