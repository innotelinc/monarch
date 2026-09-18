#!/usr/bin/env python3
"""Unit tests for the *arr wiring tools: the allowlist, and the indexers.

Both of these exist because of one shape of failure that reads as something else
entirely. An *arr answers HTTP 400 to any Host it was not told about, so
Prowlarr's app test and its indexer sync — which run over the compose network as
`http://sonarr:8989` and back as `http://prowlarr:9696` — were refused before
authentication, while the UI showed all four apps registered and every *arr with
an empty indexer list. That is "Prowlarr is not registering", and it was not.

What is pinned here is the part that decides what happens next:

  * the list is read from init/arr-allowlist.txt by BOTH readers, so the
    container side and the host side cannot disagree about what a name is;
  * merging keeps every name an operator already had — dropping one locks a
    caller out, which is the outage this file is about;
  * a candidate indexer is offered as Prowlarr needs it filed (an app profile,
    or Prowlarr refuses the whole thing), and a Cloudflare failure is the only
    one worth a second attempt through the proxy;
  * --check --offline answers without contacting a tracker, because a timer that
    tested thirty trackers every six hours is a way to earn a ban.

No Prowlarr, no docker, no network: the API is faked at the module boundary.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
ALLOWLIST = REPO / "init" / "arr-allowlist.txt"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hosts = _load("arr_allowed_hosts", "arr-allowed-hosts.py")
indexers = _load("prowlarr_indexers", "prowlarr-indexers.py")


class TheSharedAllowlist(unittest.TestCase):
    """One file, two readers — the container's and the host's."""

    def test_the_repository_list_parses_to_names(self) -> None:
        names = hosts.parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
        for required in ("localhost", "127.0.0.1", "prowlarr", "sonarr", "radarr",
                         "lidarr", "whisparr", "*.monarch.innotel.us"):
            self.assertIn(required, names, f"{required} missing from {ALLOWLIST}")

    def test_the_init_side_reads_the_same_file_the_same_way(self) -> None:
        init = _load("monarch_init_arr", str(REPO / "init" / "init.py"))
        text = ALLOWLIST.read_text(encoding="utf-8")
        self.assertEqual(init.parse_arr_allowlist(text), hosts.parse_allowlist(text))

    def test_comments_and_blanks_are_ignored(self) -> None:
        text = "# a comment\n\nprowlarr # inline\nsonarr,radarr\n"
        self.assertEqual(hosts.parse_allowlist(text), ["prowlarr", "sonarr", "radarr"])

    def test_a_name_is_not_repeated(self) -> None:
        self.assertEqual(hosts.parse_allowlist("sonarr\nsonarr,sonarr\n"), ["sonarr"])


class MergingTheList(unittest.TestCase):
    def test_an_operators_own_name_survives(self) -> None:
        have = ["localhost", "my.laptop.lan"]
        merged = hosts.merged(have, ["localhost", "prowlarr"])
        self.assertIn("my.laptop.lan", merged.split(","))
        self.assertIn("prowlarr", merged.split(","))

    def test_nothing_is_added_twice(self) -> None:
        self.assertEqual(hosts.missing_hosts(["prowlarr", "sonarr"], ["sonarr", "prowlarr"]), [])

    def test_only_the_missing_names_are_reported(self) -> None:
        self.assertEqual(hosts.missing_hosts(["prowlarr"], ["prowlarr", "radarr"]), ["radarr"])


class TheGuardIsAskedNotAssumed(unittest.TestCase):
    """The check asks the app, because the setting is not the behaviour.

    Lidarr 2.x answers 200 to a service-name Host and does not persist
    `allowedHosts` at all, so a config comparison reports permanent drift for an
    app that is fine. A fake app here enforces the real guard: 400 unless the
    Host header is one it was told about.
    """

    @classmethod
    def setUpClass(cls) -> None:
        class Guarded(BaseHTTPRequestHandler):
            allowed = {"localhost"}
            seen: list[str] = []

            def do_GET(self):  # noqa: N802 - http.server's interface
                Guarded.seen.append(self.headers.get("Host", ""))
                host = (self.headers.get("Host") or "").split(":")[0]
                self.send_response(200 if host in Guarded.allowed else 400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep the test output clean
                pass

        cls.handler = Guarded
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Guarded)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_probe_claims_to_be_the_name_it_tests(self) -> None:
        self.handler.seen.clear()
        self.assertEqual(hosts.probe(self.port, "v1", "key", "sonarr:8989"), 400)
        self.assertEqual(self.handler.seen, ["sonarr:8989"])

    def test_an_allowed_name_is_not_the_finding(self) -> None:
        self.handler.seen.clear()
        self.assertEqual(hosts.probe(self.port, "v1", "key", "localhost:8989"), 200)
        self.assertEqual(hosts.evaluate(200), "accepted")

    def test_the_guard_is_the_400(self) -> None:
        self.assertEqual(hosts.evaluate(400), "refused")
        # Not the guard: an app that answers and dislikes the request is fine.
        self.assertEqual(hosts.evaluate(401), "accepted")
        self.assertEqual(hosts.evaluate(404), "accepted")

    def test_nothing_answering_is_its_own_verdict(self) -> None:
        self.assertEqual(hosts.evaluate(None), "unreachable")
        self.assertEqual(hosts.probe(1, "v1", "key", "sonarr:8989", timeout=0.2), None)


class WritingTheAllowlist(unittest.TestCase):
    def test_the_put_carries_the_whole_config_with_matching_passwords(self) -> None:
        config = {"allowedHosts": "localhost", "password": "hash", "passwordConfirmation": ""}
        calls: list[tuple] = []

        def fake(port, api, path, key, body=None):
            calls.append((path, body))
            return (200, config) if body is None else (202, None)

        with mock.patch.object(hosts, "request", side_effect=fake):
            count = hosts.write_allowlist(8989, "v3", "key", ["localhost", "sonarr"], False)

        self.assertEqual(count, 1)
        path, body = calls[-1]
        self.assertEqual(path, "/config/host")
        # Servarr rejects the PUT with 400 unless these two agree.
        self.assertEqual(body["password"], body["passwordConfirmation"])
        self.assertEqual(body["allowedHosts"], "localhost,sonarr")

    def test_an_app_that_already_has_every_name_is_not_written_to(self) -> None:
        config = {"allowedHosts": "localhost,prowlarr", "password": "hash"}
        with mock.patch.object(hosts, "request", return_value=(200, config)) as call:
            count = hosts.write_allowlist(9696, "v1", "key", ["localhost", "prowlarr"], False)
        self.assertEqual(count, 0)
        self.assertEqual(call.call_count, 1, "an app that has every name must not be written to")

    def test_dry_run_reports_without_writing(self) -> None:
        config = {"allowedHosts": "localhost"}
        with mock.patch.object(hosts, "request", return_value=(200, config)) as call:
            count = hosts.write_allowlist(7878, "v3", "key", ["localhost", "radarr"], True)
        self.assertEqual(count, 1)
        self.assertEqual(call.call_count, 1, "a dry run must not PUT")


class ChoosingAnIndexer(unittest.TestCase):
    """What Prowlarr needs a candidate to be, before it will hold one."""

    ENTRY = {"name": "1337x", "implementation": "1337x", "privacy": "public",
             "protocol": "torrent", "fields": [], "tags": []}

    def test_a_candidate_carries_an_app_profile(self) -> None:
        # Without it Prowlarr answers "'App Profile Id' must be greater than '0'"
        # and adds nothing — which is what an empty run looks like.
        self.assertEqual(indexers._candidate(self.ENTRY, app_profile=1)["appProfileId"], 1)

    def test_a_candidate_is_enabled_and_named(self) -> None:
        payload = indexers._candidate(self.ENTRY)
        self.assertTrue(payload["enable"])
        self.assertEqual(payload["name"], "1337x")

    def test_the_proxy_tag_is_added_without_losing_the_others(self) -> None:
        entry = dict(self.ENTRY, tags=[7])
        self.assertEqual(sorted(indexers._candidate(entry, tag=1)["tags"]), [1, 7])

    def test_the_schema_entry_is_not_mutated(self) -> None:
        indexers._candidate(self.ENTRY, tag=1, app_profile=1)
        self.assertEqual(self.ENTRY["tags"], [])

    def test_only_a_cloudflare_failure_is_worth_the_proxy(self) -> None:
        self.assertTrue(indexers._needs_flaresolverr(
            "Unable to access 1337x.to, blocked by CloudFlare Protection."))
        self.assertFalse(indexers._needs_flaresolverr("Unable to resolve host name"))


class FakeProwlarr:
    """Just enough API for the two decisions that matter."""

    def __init__(self, indexers_: list[dict], schema: list[dict], tests: dict[str, tuple]):
        self._indexers = indexers_
        self._schema = schema
        self._tests = tests
        self.added: list[dict] = []
        self.tagged: list[dict] = []
        self.tested: list[str] = []

    def indexers(self):
        return self._indexers

    def schema(self):
        return self._schema

    def tag_id(self, label):
        return 1

    def app_profile_id(self):
        return 1

    def test(self, candidate):
        self.tested.append(("proxy" if candidate.get("tags") else "direct"))
        return self._tests.get("proxy" if candidate.get("tags") else "direct",
                               (False, "Unable to resolve host name"))

    def add(self, candidate):
        self.added.append(candidate)
        return True, ""

    def update(self, candidate):
        self.tagged.append(candidate)
        return True, ""


class AddingTheIndexers(unittest.TestCase):
    SCHEMA = [
        {"name": "1337x", "implementation": "1337x", "privacy": "public", "protocol": "torrent"},
        {"name": "Anidex", "implementation": "Anidex", "privacy": "public", "protocol": "torrent"},
        {"name": "DeadSite", "implementation": "DeadSite", "privacy": "public", "protocol": "torrent"},
        {"name": "PrivateOne", "implementation": "PrivateOne", "privacy": "private",
         "protocol": "torrent"},
    ]

    def test_only_public_definitions_are_attempted(self) -> None:
        fake = FakeProwlarr([], self.SCHEMA, {})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        # Three public definitions, and the private one is not tested at all:
        # it wants an account that does not exist here.
        self.assertEqual(len(fake.tested), 3)
        self.assertNotIn("PrivateOne", [c["name"] for c in fake.added])

    def test_a_working_indexer_is_added_directly(self) -> None:
        fake = FakeProwlarr([], self.SCHEMA, {"direct": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual(sorted(c["name"] for c in fake.added),
                         ["1337x", "Anidex", "DeadSite"])
        self.assertTrue(all("tags" not in c or not c["tags"] for c in fake.added))

    def test_a_cloudflare_blocked_one_is_retried_through_the_proxy(self) -> None:
        fake = FakeProwlarr([], [self.SCHEMA[0]],
                            {"direct": (False, "blocked by CloudFlare Protection."),
                             "proxy": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(fake.added[0]["tags"], [1])
        self.assertEqual(fake.tested, ["direct", "proxy"])

    def test_a_dead_one_is_never_added(self) -> None:
        fake = FakeProwlarr([], [self.SCHEMA[2]], {"direct": (False, "Unable to resolve host")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual(fake.added, [])
        self.assertEqual(fake.tested, ["direct"], "a dead site is not a tagging problem")

    def test_an_indexer_already_present_is_not_added_again(self) -> None:
        fake = FakeProwlarr([{"name": "Anidex", "definitionName": "Anidex", "tags": []}],
                            self.SCHEMA, {"direct": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertNotIn("Anidex", [c["name"] for c in fake.added])

    def test_an_existing_blocked_indexer_is_tagged_not_duplicated(self) -> None:
        fake = FakeProwlarr(
            [{"id": 3, "name": "1337x", "definitionName": "1337x", "tags": [],
              "appProfileId": 1}],
            [self.SCHEMA[0]],
            {"direct": (False, "blocked by CloudFlare Protection."), "proxy": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual([c["name"] for c in fake.tagged], ["1337x"])
        self.assertEqual(fake.added, [])


class CheckingTheIndexers(unittest.TestCase):
    def _run(self, fake) -> int:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = indexers.check(fake, workers=1, offline=False)
        return code, out.getvalue()

    def test_an_indexer_that_does_not_answer_is_the_finding(self) -> None:
        fake = FakeProwlarr([{"id": 1, "name": "1337x", "enable": True}], [],
                            {"direct": (False, "Unable to access 1337x.to, blocked")})
        code, out = self._run(fake)
        self.assertEqual(code, 2)
        self.assertIn("1337x", out)

    def test_everything_answering_is_ok(self) -> None:
        fake = FakeProwlarr([{"id": 1, "name": "1337x", "enable": True}], [],
                            {"direct": (True, "")})
        code, _ = self._run(fake)
        self.assertEqual(code, 0)

    def test_offline_only_asks_whether_an_enabled_indexer_exists(self) -> None:
        fake = FakeProwlarr([{"id": 1, "name": "1337x", "enable": True}], [], {})
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = indexers.check(fake, workers=1, offline=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.tested, [], "an offline check must not contact a tracker")

    def test_offline_with_nothing_enabled_is_the_finding(self) -> None:
        fake = FakeProwlarr([{"id": 1, "name": "1337x", "enable": False}], [], {})
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = indexers.check(fake, workers=1, offline=True)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
