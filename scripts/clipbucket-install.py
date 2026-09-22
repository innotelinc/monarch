#!/usr/bin/env python3
"""clipbucket-install.py — finish ClipBucket's install without a browser.

WHY THIS EXISTS
---------------
ClipBucket installs through a nine-step browser wizard (`upload/cb_install/`),
and a deployment has no browser in it. So the app comes up in the only state it
knows before the wizard has run — serving the installer — and that state is
indistinguishable from "the migration worked, there is just no content yet",
which is how a host came to be described as migrated while its `clipbucket/`
schema held a single entry (`db.opt`). Measured 2026-09-22 on the deployment:

    $ curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' localhost:8098/
    302 http://127.0.0.1/cb_install

The wizard is a sequence of AJAX calls into `cb_install/ajax.php`, and each one is
a small, documented step: import an ordered list of SQL files, write
`includes/config.php` from the installer's own template, then set the admin
account and the site settings. This script does the same steps from the host, so
the outcome is the wizard's outcome and the intermediate state is reportable.

WHAT IT DOES
------------
1. **Schema and seed data.** The 17 SQL files `cb_install/ajax.php` imports, in
   its order (`structure` → `table_version` → `configs` → `languages` →
   five translations → `ads_placements` → `countries` → `email_templates` →
   `pages` → `user_levels` → `categories` → `add_admin` →
   `add_anonymous_user`), with `{tbl_prefix}` and `{dbname}` substituted the way
   `install_execute_sql_file()` does.
2. **The version row.** The wizard's `version` step is not decoration: the app
   gates columns and queries on `Update::IsCurrentDBVersionIsHigherOrEqualTo()`,
   which reads `cb_version` — so an install without it takes pre-migration code
   paths against a current schema. Skipping it here was measured: every
   logged-in page answered HTTP 500 with `array_key_exists(): Argument #2
   ($array) must be of type array, false given` inside `User->get()`, because
   `User::init()` sets `user_data` from a query that only works once the version
   is recorded. The values come from the app's own `changelog/latest.json` →
   `changelog/<stable>.json`, exactly as `cb_install/ajax.php` reads them.
3. **`includes/config.php`**, rendered from `cb_install/config.php` (the
   installer's own template) with the compose's `MYSQL_PASSWORD`, so the app and
   the database agree by construction rather than by luck.
4. **The admin account**: `add_admin.sql` creates userid 1 with an empty
   password; this sets its username, email and a real password. CliBucket hashes
   with `pass_code()` — `hash('sha512', $password . $userid . $salt)` where the
   salt is `config('password_salt')` — so the hash is computed here from the salt
   the schema just imported.
5. **`base_url` and `site_title`**, so links stop being built from whatever Host
   header the request arrived with.
6. **The installer is locked.** `cb_install/ajax.php` refuses to run unless
   `files/temp/install.me` or `install.me.not` exists, and `install.me` is
   removed last: leaving it behind is how a finished site can still be
   re-installed from a browser by anyone who can reach it.

The database user and password are reconciled rather than assumed: the compose
hands the container `MYSQL_PASSWORD`, the entrypoint only creates the user when
the volume has no `clipbucket/` directory yet, and the app reads the same value
from `includes/config.php` this script writes. Where they disagree, the user is
aligned to the compose value and that is reported.

USAGE
-----
    python3 scripts/clipbucket-install.py --check                  # what state is it in
    python3 scripts/clipbucket-install.py --apply                  # finish the install
    python3 scripts/clipbucket-install.py --apply --domain tube.innotel.us
    python3 scripts/clipbucket-install.py --apply --admin-password '…'

`--check` is read-only and exits 1 when the install is not finished, which is
what makes it usable from `drift-check`. `--apply` is idempotent: the SQL list is
only replayed when the schema is empty (a second run would otherwise duplicate
seed rows), and the rest of the steps are plain updates.

Exit codes: 0 finished/ok, 1 not finished, 2 cannot tell (no container, no
docker, no database to read).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

CONTAINER_DEFAULT = "clipbucket"
# The web root, i.e. the directory the container's nginx serves (see
# clipbucket/nginx-clipbucket.conf) — the wizard's paths are relative to it.
APP_ROOT = "/srv/http/clipbucket/upload"
DB_NAME = "clipbucket"
DB_USER = "clipbucket"
DB_HOST = "localhost"
DB_PORT = "3306"
TABLE_PREFIX = "cb_"

# cb_install/ajax.php's `$files` (the dataimport step), followed by the two the
# next step imports. Order matters: `categories` is read by add_admin.sql's
# lookup of the 'Gurus' user category, and `configs` must precede any UPDATE of
# a config row.
SQL_FILES = (
    "structure.sql",
    "table_version.sql",
    "configs.sql",
    "languages.sql",
    "language_ENG.sql",
    "language_FRA.sql",
    "language_DEU.sql",
    "language_POR.sql",
    "language_ESP.sql",
    "ads_placements.sql",
    "countries.sql",
    "email_templates.sql",
    "pages.sql",
    "user_levels.sql",
    "categories.sql",
    "add_admin.sql",
    "add_anonymous_user.sql",
)

# The installer's placeholders (install_execute_sql_file + the create_files step).
SQL_PLACEHOLDERS = {"tbl_prefix": TABLE_PREFIX, "dbname": DB_NAME}
CONFIG_PLACEHOLDERS = {
    "_DB_HOST_": DB_HOST,
    "_DB_NAME_": DB_NAME,
    "_DB_USER_": DB_USER,
    "_DB_PORT_": DB_PORT,
    "_TABLE_PREFIX_": TABLE_PREFIX,
}

CONFIG_ROWS = ("base_url", "site_title", "site_slogan", "password_salt")
# The app reads its own release metadata from these, and cb_install/ajax.php's
# `version` step is the only writer of `cb_version`.
LATEST_JSON = "changelog/latest.json"
RELEASE_JSON = "changelog/{stable}.json"


class CantTell(Exception):
    """Nothing could be evaluated — not a pass, and not a finding either."""


# ── pure rendering: the installer's own substitutions ──────────────────────


def render_sql(text: str) -> str:
    """`{tbl_prefix}` / `{dbname}` → this stack's values, as the wizard does."""
    for key, value in SQL_PLACEHOLDERS.items():
        text = text.replace("{" + key + "}", value)
    return text


def render_config(template: str, password: str) -> str:
    """`cb_install/config.php` → `includes/config.php`.

    Every value the template names is replaced, including the password, which is
    why this is a function with a test rather than a sed in the apply path.
    """
    values = dict(CONFIG_PLACEHOLDERS, _DB_PASS_=password)
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def version_row(latest: dict, release: dict) -> tuple[str, int]:
    """`(version, revision)` from the app's changelog, as the wizard's step reads it.

    Pure, because "which version does this install claim to be" decides which
    code paths run, and a wrong answer there is invisible until a query fails.
    """
    stable = str(latest["stable"])
    version = str(release.get("version") or "")
    if not version:
        # Fall back the way the file is named: 553 → 5.5.3.
        version = ".".join(stable) if len(stable) == 3 else stable
    return version, int(release.get("revision") or 0)


def admin_password_hash(password: str, userid: int, salt: str) -> str:
    """`pass_code()` from includes/functions.php:

        hash('sha512', $string . $userid . $salt)

    Reimplemented rather than shelled into the container: it is three operands in
    a row, and a mismatch is a login that fails with no explanation.
    """
    return hashlib.sha512(f"{password}{userid}{salt}".encode()).hexdigest()


def evaluate(facts: dict) -> list[str]:
    """The problems with a deployment, from facts a caller has already read.

    Pure so both directions are unit-testable: an empty list means "installed".
    """
    problems = []
    if not facts.get("container_running"):
        raise CantTell("the clipbucket container is not running")
    if not facts.get("docker"):
        raise CantTell("docker is not available here")

    tables = facts.get("table_count")
    if tables is None:
        raise CantTell("the clipbucket schema could not be read")
    if tables == 0:
        problems.append(
            "the clipbucket schema is empty — the installer never created tables "
            "(run: python3 scripts/clipbucket-install.py --apply)"
        )
    elif tables < 60:
        problems.append(f"the clipbucket schema has only {tables} tables; a finished install has ~80")

    if not facts.get("config_php"):
        problems.append("upload/includes/config.php is missing — the app has no database to talk to")
    if not facts.get("admin_password"):
        problems.append("the admin account (userid 1) has no password set")
    if not facts.get("base_url"):
        problems.append("config.base_url is empty — links are built from the request's Host header")
    if not facts.get("version"):
        problems.append(
            "cb_version has no row — the app gates its queries on the recorded "
            "version, and an install without it answers HTTP 500 on every logged-in page"
        )
    if facts.get("install_me"):
        problems.append(
            "files/temp/install.me is still present, so the browser installer is reachable "
            "and can re-install over this site"
        )
    if facts.get("serves_installer"):
        problems.append("the site is serving its installer (HTTP redirect to /cb_install)")
    return problems


# ── docker plumbing ───────────────────────────────────────────────────────


def docker(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args], input=input_text, capture_output=True, text=True
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment-dependent
        raise CantTell("docker is not available here") from exc


def container_running(container: str) -> bool:
    proc = docker(["inspect", "-f", "{{.State.Running}}", container])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def in_container(container: str, *args: str, input_text: str | None = None,
                 env: dict | None = None):
    argv = ["exec", "-i"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [container, *args]
    return docker(argv, input_text=input_text)


def read_app_file(container: str, path: str) -> str | None:
    proc = in_container(container, "cat", path)
    if proc.returncode != 0:
        return None
    return proc.stdout


def write_app_file(container: str, path: str, text: str) -> None:
    proc = in_container(container, "sh", "-c", f"cat > {path}", input_text=text)
    if proc.returncode != 0:
        raise CantTell(f"could not write {path}: {proc.stderr.strip()}")


def mysql(container: str, sql: str, *, database: str | None = None,
          user: str = "root", password: str | None = None) -> str:
    """Run SQL through the container's own client.

    Root connects over the socket with no password on these builds (the
    entrypoint's own `mysql -uroot` calls do the same); the app user is
    authenticated with MYSQL_PWD so the value never lands in the SQL text.
    """
    args = ["mysql", "-u", user, "-N", "-B"]
    if database:
        args.append(database)
    env = {"MYSQL_PWD": password} if password else {}
    proc = in_container(container, *args, input_text=sql, env=env)
    if proc.returncode != 0:
        raise CantTell(f"mysql failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def mysql_value(container: str, sql: str, **kwargs) -> str:
    out = mysql(container, sql, **kwargs).strip()
    return out.splitlines()[0].strip() if out else ""


# ── reading the state (used by --check and before --apply) ────────────────


def read_facts(container: str, port: int, probe: bool) -> dict:
    facts = {
        "docker": True,
        "container_running": container_running(container),
    }
    if not facts["container_running"]:
        return facts

    facts["config_php"] = read_app_file(container, f"{APP_ROOT}/includes/config.php") is not None
    facts["install_me"] = (
        in_container(container, "test", "-f", f"{APP_ROOT}/files/temp/install.me").returncode
        == 0
    )
    facts["install_locked"] = (
        in_container(container, "test", "-f", f"{APP_ROOT}/files/temp/install.me.not").returncode
        == 0
    )

    try:
        facts["table_count"] = int(
            mysql_value(
                container,
                "SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_schema = '{DB_NAME}';",
            )
        )
    except (CantTell, ValueError):
        facts["table_count"] = None

    if facts.get("table_count"):
        try:
            facts["version"] = mysql_value(
                container,
                f"SELECT CONCAT(version, '.', revision) FROM {TABLE_PREFIX}version WHERE id = 1;",
                database=DB_NAME,
            )
            facts["base_url"] = mysql_value(
                container, f"SELECT value FROM {TABLE_PREFIX}config WHERE name = 'base_url';",
                database=DB_NAME,
            )
            facts["admin_password"] = mysql_value(
                container,
                f"SELECT password FROM {TABLE_PREFIX}users WHERE userid = 1;",
                database=DB_NAME,
            )
            facts["admin_user"] = mysql_value(
                container, f"SELECT username FROM {TABLE_PREFIX}users WHERE userid = 1;",
                database=DB_NAME,
            )
        except CantTell:
            pass

    if probe:
        facts["serves_installer"] = _serves_installer(port)
    return facts


def _serves_installer(port: int) -> bool:
    """Does the site answer with a redirect to its installer?

    The caller-visible symptom, and the one thing a table count cannot tell you:
    an install can be complete and still be locked out of itself by a leftover
    `install.me`.
    """
    try:
        # No redirects: the 302 to /cb_install IS the finding.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: D102
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(f"http://127.0.0.1:{port}/", timeout=10)
            location = resp.headers.get("Location", "")
            body = resp.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "")
            body = exc.read(4000).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - the port is simply not answering
        return False
    return "cb_install" in f"{location}{body}"


# ── the apply ─────────────────────────────────────────────────────────────


def apply(container: str, args, log) -> None:
    facts = read_facts(container, args.port, probe=False)
    if not facts["container_running"]:
        raise CantTell(f"container '{container}' is not running (docker compose up -d clipbucket)")

    password = in_container(container, "printenv", "MYSQL_PASSWORD").stdout.strip()
    if not password:
        raise CantTell(
            "the container has no MYSQL_PASSWORD, so there is no credential the app "
            "and the database can agree on (set CLIPBUCKET_DB_PASSWORD in .env)"
        )

    # The installer refuses to run without one of the two sentinels, and it is
    # the honest signal that an install is in progress.
    if not facts["install_me"] and not facts["install_locked"]:
        write_app_file(container, f"{APP_ROOT}/files/temp/install.me", "1\n")
        log("created files/temp/install.me (the installer's own lock)")

    if not facts.get("table_count"):
        log(f"importing {len(SQL_FILES)} SQL files in the installer's order")
        for name in SQL_FILES:
            path = f"{APP_ROOT}/cb_install/sql/{name}"
            text = read_app_file(container, path)
            if text is None:
                raise CantTell(f"{path} is missing — is the app source complete?")
            mysql(container, render_sql(text), database=DB_NAME)
            log(f"  {name}")
    else:
        log(f"schema already has {facts['table_count']} tables — not replaying the SQL list")

    # The version row, as cb_install/ajax.php's `version` step writes it: the
    # app gates columns and queries on it, so an install that skips it runs
    # pre-migration code against a current schema (and 500s on every logged-in
    # page, which is how this step came to be here).
    latest_text = read_app_file(container, f"{APP_ROOT}/{LATEST_JSON}")
    if latest_text is None:
        raise CantTell(f"{LATEST_JSON} is missing — the app source is incomplete")
    latest = json.loads(latest_text)
    release_text = read_app_file(
        container, f"{APP_ROOT}/{RELEASE_JSON.format(stable=latest.get('stable'))}"
    )
    if release_text is None:
        raise CantTell(f"changelog/{latest.get('stable')}.json is missing (it names the current release)")
    version, revision = version_row(latest, json.loads(release_text))
    mysql(
        container,
        f"INSERT INTO {TABLE_PREFIX}version SET version = '{version}', revision = {revision}, id = 1 "
        f"ON DUPLICATE KEY UPDATE version = '{version}', revision = {revision};",
        database=DB_NAME,
    )
    log(f"recorded the app's version (cb_version = {version}, revision {revision})")

    config_template = read_app_file(container, f"{APP_ROOT}/cb_install/config.php")
    if config_template is None:
        raise CantTell(f"{APP_ROOT}/cb_install/config.php is missing (the installer's template)")
    write_app_file(container, f"{APP_ROOT}/includes/config.php", render_config(config_template, password))
    log("wrote includes/config.php from the installer's template")

    # Credential agreement: the entrypoint creates this user only on a fresh
    # volume, so on a volume that already has a schema it can hold a password
    # that no longer matches the compose.
    try:
        mysql(container, "SELECT 1;", user=DB_USER, password=password, database=DB_NAME)
    except CantTell:
        mysql(
            container,
            f"ALTER USER '{DB_USER}'@'{DB_HOST}' IDENTIFIED BY '{password}';\nFLUSH PRIVILEGES;",
        )
        log(f"aligned the {DB_USER} database user to the compose's MYSQL_PASSWORD")

    salt = mysql_value(
        container, f"SELECT value FROM {TABLE_PREFIX}config WHERE name = 'password_salt';",
        database=DB_NAME,
    )
    if not salt:
        raise CantTell("config.password_salt is missing, so no password can be hashed")
    admin_hash = admin_password_hash(args.admin_password, 1, salt)
    mysql(
        container,
        f"UPDATE {TABLE_PREFIX}users SET username = '{args.admin_user}', "
        f"email = '{args.admin_email}', password = '{admin_hash}', usr_status = 'Ok' "
        "WHERE userid = 1;",
        database=DB_NAME,
    )
    log(f"set the admin account (userid 1) to '{args.admin_user}' <{args.admin_email}>")

    for name, value in (("base_url", args.base_url), ("site_title", args.site_title)):
        mysql(
            container,
            f"UPDATE {TABLE_PREFIX}config SET value = '{value}' WHERE name = '{name}';",
            database=DB_NAME,
        )
        log(f"config.{name} = {value}")

    in_container(container, "rm", "-f", f"{APP_ROOT}/files/temp/install.me")
    write_app_file(container, f"{APP_ROOT}/files/temp/install.me.not", "1\n")
    in_container(
        container, "sh", "-c",
        f"chown 1000:1000 {APP_ROOT}/files/temp/install.me.not "
        f"{APP_ROOT}/includes/config.php",
    )
    log("locked the installer (install.me removed, install.me.not written)")


# ── cli ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Finish ClipBucket's install without a browser.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report the state, change nothing (default)")
    mode.add_argument("--apply", action="store_true", help="perform the install steps")
    ap.add_argument("--container", default=CONTAINER_DEFAULT)
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLIPBUCKET_PORT", "8098")),
                    help="the app's host port, for the 'is it serving the installer' probe")
    ap.add_argument("--no-probe", action="store_true", help="skip the HTTP probe")
    ap.add_argument("--domain", default=os.environ.get("CLIPBUCKET_DOMAIN", "tube.innotel.us"))
    ap.add_argument("--site-title", default=os.environ.get("CLIPBUCKET_SITE_TITLE", "Monarch Clips"))
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--admin-email", default=None, help="defaults to admin@<domain>")
    ap.add_argument(
        "--admin-password",
        default=os.environ.get("CLIPBUCKET_ADMIN_PASSWORD"),
        help="defaults to CLIPBUCKET_ADMIN_PASSWORD, else a generated one is printed once",
    )
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    args.base_url = f"https://{args.domain}"
    args.admin_email = args.admin_email or f"admin@{args.domain}"

    generated = False
    if args.admin_password is None:
        args.admin_password = secrets.token_urlsafe(12)
        generated = True

    def log(message: str) -> None:
        print(f"clipbucket-install: {message}")

    try:
        if args.apply:
            apply(args.container, args, log)
            if generated:
                print(
                    f"clipbucket-install: admin password for '{args.admin_user}': "
                    f"{args.admin_password}  (printed once — record it now)"
                )

        facts = read_facts(args.container, args.port, probe=not args.no_probe)
        problems = evaluate(facts)
    except CantTell as exc:
        print(f"clipbucket-install: cannot tell — {exc}", file=sys.stderr)
        return 2

    if problems:
        for problem in problems:
            print(f"clipbucket-install: {problem}", file=sys.stderr)
        return 1

    print(
        f"clipbucket-install: installed — {facts['table_count']} tables, "
        f"config.php present, base_url {facts.get('base_url') or '-'}, "
        f"admin '{facts.get('admin_user') or '-'}', installer locked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
