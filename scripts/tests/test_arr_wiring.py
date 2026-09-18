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
    tested thirty trackers every six hours is a way to earn a ban;
  * an indexer counts as *received* only when the app holds one pointing at
    Prowlarr's own proxy path, and "none received" is the finding while "fewer
    than Prowlarr has" is normal — Prowlarr skips an indexer that returns no
    results in that app's categories.

No Prowlarr, no docker, no network: the API is faked at the module boundary.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
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
sync = _load("arr_sync", "arr-sync.py")
categories = _load("arr_download_categories", "arr-download-categories.py")


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

    def __init__(self, indexers_: list[dict], schema: list[dict], tests: dict[str, tuple],
                 proxies: tuple = ({"name": "FlareSolverr"},)):
        self._indexers = indexers_
        self._schema = schema
        self._tests = tests
        self._proxies = list(proxies)
        self.added: list[dict] = []
        self.tagged: list[dict] = []
        self.tested: list[str] = []

    def get(self, path):
        if path == "/indexerproxy":
            return 200, self._proxies
        return 404, None

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

    def test_the_privacy_classes_are_matched_case_insensitively(self) -> None:
        # The invocation both the usage block and the docs show is
        # `--privacy public,semiPrivate`, and a definition reports
        # `semiPrivate`: compared as typed, the second class never matched and
        # the run quietly attempted the narrower set.
        self.assertEqual(indexers.parse_privacy("public,semiPrivate"),
                         ("public", "semiprivate"))
        self.assertEqual(indexers.parse_privacy(" public , , sEMIprivate "),
                         ("public", "semiprivate"))
        self.assertEqual(indexers.parse_privacy(""), ())

    def test_a_semi_private_definition_is_reached_by_the_documented_flag(self) -> None:
        schema = [{"name": "ClosedOne", "implementation": "ClosedOne",
                   "privacy": "semiPrivate", "protocol": "torrent"}]
        fake = FakeProwlarr([], schema, {"direct": (True, "")})
        indexers.add(fake, indexers.parse_privacy("public,semiPrivate"), workers=1, dry_run=False)
        self.assertEqual([c["name"] for c in fake.added], ["ClosedOne"])

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

    def test_the_definition_id_decides_not_the_display_name(self) -> None:
        # The live shape: a Cardigann definition is held as `btdirectory` and
        # labelled `BTdirectory`. Compared by label it is re-tried, Prowlarr
        # answers "Should be unique", and the run reports a present indexer as a
        # failed candidate - one request to that tracker for every one held.
        schema = [{"name": "BTdirectory", "definitionName": "btdirectory",
                   "implementation": "Cardigann", "privacy": "public", "protocol": "torrent"}]
        fake = FakeProwlarr([{"name": "BTdirectory", "definitionName": "btdirectory", "tags": []}],
                            schema, {"direct": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        # One probe: the repair pass looking at the indexer it already holds. A
        # candidate test would be a second one, which is the bug.
        self.assertEqual(fake.tested, ["direct"], "an indexer already held must not be tried again")
        self.assertEqual(fake.added, [])

    def test_a_definition_whose_label_matches_another_is_still_tried(self) -> None:
        # The other half of the same mistake: a same-named definition is not the
        # same definition, and skipping it is how an indexer never gets added.
        schema = [{"name": "Anidex", "definitionName": "anidex-fork",
                   "implementation": "Cardigann", "privacy": "public", "protocol": "torrent"}]
        fake = FakeProwlarr([{"name": "Anidex", "definitionName": "anidex", "tags": []}],
                            schema, {"direct": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual([c["name"] for c in fake.added], ["Anidex"])

    def test_an_existing_blocked_indexer_is_tagged_not_duplicated(self) -> None:
        fake = FakeProwlarr(
            [{"id": 3, "name": "1337x", "definitionName": "1337x", "tags": [],
              "appProfileId": 1}],
            [self.SCHEMA[0]],
            {"direct": (False, "blocked by CloudFlare Protection."), "proxy": (True, "")})
        indexers.add(fake, ("public",), workers=1, dry_run=False)
        self.assertEqual([c["name"] for c in fake.tagged], ["1337x"])
        self.assertEqual(fake.added, [])


class RepairingTheBlockedOnes(unittest.TestCase):
    """Only the proxy makes the tag mean anything, so it is asked for first."""

    def _run(self, fake):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = indexers.repairs_only(fake, workers=1)
        return code, out.getvalue()

    def test_without_a_proxy_it_refuses_instead_of_reporting_a_repair(self) -> None:
        fake = FakeProwlarr([{"id": 3, "name": "1337x", "definitionName": "1337x",
                             "tags": []}], [],
                           {"direct": (False, "blocked by CloudFlare Protection."),
                            "proxy": (True, "")}, proxies=())
        code, out = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.tagged, [], "a tag without a proxy changes nothing")
        self.assertIn("no indexer proxy", out)

    def test_an_existing_blocked_indexer_is_tagged(self) -> None:
        fake = FakeProwlarr([{"id": 3, "name": "1337x", "definitionName": "1337x",
                             "tags": [], "appProfileId": 1}], [],
                           {"direct": (False, "blocked by CloudFlare Protection."),
                            "proxy": (True, "")})
        code, out = self._run(fake)
        self.assertEqual(code, 0)
        self.assertEqual([c["name"] for c in fake.tagged], ["1337x"])
        self.assertEqual(fake.added, [], "a repair adds nothing")
        self.assertIn("FlareSolverr", out)


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


class WhatTheAppReceived(unittest.TestCase):
    """Prowlarr's proxy path is the only shape a delivered indexer has."""

    BASE = "http://prowlarr:9696"

    def _indexer(self, base: str) -> dict:
        return {"name": "1337x (Prowlarr)", "fields": [{"name": "baseUrl", "value": base}]}

    def test_a_delivered_indexer_points_at_prowlarr_proxy_path(self) -> None:
        self.assertTrue(sync.is_from_prowlarr(self._indexer("http://prowlarr:9696/37/"), self.BASE))
        self.assertTrue(sync.is_from_prowlarr(self._indexer("http://prowlarr:9696/37"), self.BASE))

    def test_a_lookalike_host_is_not_prowlarr(self) -> None:
        self.assertFalse(sync.is_from_prowlarr(self._indexer("http://prowlarr:9696.example/"),
                                               self.BASE))

    def test_a_hand_added_indexer_is_not_prowlarr(self) -> None:
        self.assertFalse(sync.is_from_prowlarr(self._indexer("http://192.168.1.50:9696/1/"), self.BASE))
        self.assertFalse(sync.is_from_prowlarr({"name": "x", "fields": []}, self.BASE))

    def test_the_apps_own_filed_url_is_what_is_compared(self) -> None:
        fields = [{"name": "baseUrl", "value": "http://sonarr:8989"},
                  {"name": "prowlarrUrl", "value": "http://prowlarr:9696/"}]
        self.assertEqual(sync.prowlarr_url(fields), "http://prowlarr:9696")
        self.assertEqual(sync.prowlarr_url([]), "")

    def test_zero_received_is_the_finding_only_when_there_is_something_to_send(self) -> None:
        self.assertEqual(sync.evaluate(22, 73), "delivered")
        self.assertEqual(sync.evaluate(0, 73), "not_registered")
        # Prowlarr holding nothing is "nothing to deliver", not a broken app.
        self.assertEqual(sync.evaluate(0, 0), "nothing_to_deliver")


class SyncingTheApps(unittest.TestCase):
    """An app that received nothing is the state that started this file."""

    SONARR = {"id": 2, "name": "Sonarr",
              "fields": [{"name": "prowlarrUrl", "value": "http://prowlarr:9696"}]}

    def _report(self, apps, tests, offered, counts, applying=False):
        fake = mock.Mock()
        fake.applications.return_value = list(apps)

        def answer(appdata, svc, port, api, prowlarr_base, timeout=20.0):
            got = counts[svc]
            if isinstance(got, Exception):
                raise got
            return got

        out = io.StringIO()
        with mock.patch.object(sync, "received", side_effect=answer), \
                redirect_stdout(out), redirect_stderr(out):
            code = sync.report(fake, Path("/appdata"), offered, tests, applying=applying)
        return code, out.getvalue()

    def test_an_app_holding_its_share_is_ok(self) -> None:
        code, out = self._report([self.SONARR], {"Sonarr": (True, "")}, 73, {"sonarr": 22})
        self.assertEqual(code, 0)
        self.assertIn("sonarr", out)

    def test_fewer_than_prowlarr_has_is_the_supported_state(self) -> None:
        # Prowlarr skips an indexer that returns nothing in the app's categories;
        # Sonarr holding 22 of 73 must not read as drift.
        code, _ = self._report([self.SONARR], {"Sonarr": (True, "")}, 73, {"sonarr": 1})
        self.assertEqual(code, 0)

    def test_an_app_that_received_none_is_the_finding(self) -> None:
        code, out = self._report([self.SONARR], {"Sonarr": (True, "")}, 73, {"sonarr": 0})
        self.assertEqual(code, 2)
        self.assertIn("0 of Prowlarr's 73", out)

    def test_nothing_to_deliver_is_not_a_finding(self) -> None:
        code, _ = self._report([self.SONARR], {"Sonarr": (True, "")}, 0, {"sonarr": 0})
        self.assertEqual(code, 0)

    def test_prowlarr_being_unable_to_reach_the_app_is_the_finding(self) -> None:
        code, out = self._report([self.SONARR],
                                 {"Sonarr": (False, "Unable to connect to indexer")}, 73,
                                 {"sonarr": 0})
        self.assertEqual(code, 2)
        self.assertIn("Prowlarr cannot talk to it", out)

    def test_an_app_that_is_not_local_is_not_counted(self) -> None:
        other = {"id": 9, "name": "Bazarr",
                 "fields": [{"name": "prowlarrUrl", "value": "http://prowlarr:9696"}]}
        code, out = self._report([other], {"Bazarr": (True, "")}, 73, {})
        self.assertEqual(code, 0)
        self.assertIn("not a local app", out)

    def test_an_app_that_answers_nothing_is_a_failure_not_drift(self) -> None:
        code, _ = self._report([self.SONARR], {"Sonarr": (True, "")}, 73,
                               {"sonarr": sync.ArrSyncError("connection refused")})
        self.assertEqual(code, 1)


class WhereADownloadLands(unittest.TestCase):
    """The category name the app sends and the path qBittorrent maps it to.

    Both halves fail invisibly: a client created with `category` set has no such
    field, so no app is ever told anything, and a category whose path is a
    library root drops an unfinished album into the music library. The app only
    hints at the second one, on its own Health page.
    """

    WANT = {"tv": "/data/torrents/tv", "movies": "/data/torrents/movies",
            "music": "/data/torrents/music", "xxx": "/data/torrents/xxx"}

    def test_the_apps_own_spelling_is_the_field_that_matters(self) -> None:
        sonarr = {"fields": [{"name": "tvCategory", "value": "tv-sonarr"},
                             {"name": "failedDownloadHandling", "value": True}]}
        lidarr = {"fields": [{"name": "musicCategory", "value": "lidarr"}]}
        self.assertEqual(categories.category_field(sonarr)["name"], "tvCategory")
        self.assertEqual(categories.category_field(lidarr)["name"], "musicCategory")
        # There is no plain `category` in these schemas - that is the bug.
        self.assertIsNone(categories.category_field({"fields": [{"name": "host"}]}))

    def test_a_category_pointing_at_a_library_root_is_drift(self) -> None:
        live = {"lidarr": {"savePath": "/data/media/music"}}
        self.assertEqual(categories.path_drift(live, {"lidarr": "/data/torrents/music"}),
                         [("lidarr", "/data/media/music", "/data/torrents/music")])

    def test_a_category_at_its_path_is_not_drift(self) -> None:
        live = {"movies": {"savePath": "/data/torrents/movies"}}
        self.assertEqual(categories.path_drift(live, {"movies": "/data/torrents/movies"}), [])

    def test_a_missing_category_is_drift_with_no_live_path(self) -> None:
        self.assertEqual(categories.path_drift({}, {"tv": "/data/torrents/tv"}),
                         [("tv", None, "/data/torrents/tv")])

    def test_a_name_the_manifest_does_not_carry_is_a_stray(self) -> None:
        live = {name: {} for name in (*self.WANT, "radarr", "downloads", "whisparr")}
        self.assertEqual(categories.strays(live, self.WANT),
                         ["downloads", "radarr", "whisparr"])

    def test_a_manifest_that_predates_the_map_reports_unknown_paths(self) -> None:
        # Not "every category saves to the default": guessing that would rewrite
        # four correct /data/torrents/<type> paths and call it a fix.
        old = {"qbt": {"categories": ["tv", "movies"], "save_path": "/data/torrents"}}
        self.assertEqual(categories.manifest_categories(old),
                         ({"tv": None, "movies": None}, False))
        new = {"qbt": {"categories": ["tv"],
                        "category_paths": {"tv": "/data/torrents/tv"}}}
        self.assertEqual(categories.manifest_categories(new),
                         ({"tv": "/data/torrents/tv"}, True))

    def test_an_unknown_path_is_never_called_drift_but_a_missing_name_is(self) -> None:
        self.assertEqual(categories.path_drift({"tv": {"savePath": "/elsewhere"}},
                                               {"tv": None}), [])
        self.assertEqual(categories.path_drift({}, {"tv": None}), [("tv", None, None)])

    def test_the_credentials_come_from_dot_env_when_the_environment_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "# monarch\nMONARCH_USERNAME=someone\nMONARCH_PASSWORD='secret value'\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(categories.credentials(Path(tmp)),
                                 ("someone", "secret value"))

    def test_an_app_that_sends_the_wrong_category_is_corrected(self) -> None:
        manifest = {"arr_apps": [{"svc": "lidarr", "port": 8686, "api": "v1",
                                  "category": "music"}]}
        client = {"id": 1, "implementation": "QBittorrent",
                  "fields": [{"name": "musicCategory", "value": "lidarr"}]}
        calls = []

        def fake_call(url, method="GET", body=None, **kwargs):
            calls.append((method, url, body))
            return (200, [client]) if method == "GET" else (202, None)

        with mock.patch.object(categories, "api_key", return_value="k"), \
                mock.patch.object(categories, "call", side_effect=fake_call), \
                redirect_stdout(io.StringIO()):
            findings, reachable = categories.check_arrs(manifest, Path("/appdata"), True, False)

        self.assertEqual(findings, [])
        self.assertTrue(reachable)
        method, url, body = calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/downloadclient/1"))
        self.assertEqual(categories.category_field(body)["value"], "music")

    def test_a_check_reports_the_wrong_category_and_changes_nothing(self) -> None:
        manifest = {"arr_apps": [{"svc": "radarr", "port": 7878, "api": "v3",
                                  "category": "movies"}]}
        client = {"id": 1, "implementation": "QBittorrent",
                  "fields": [{"name": "movieCategory", "value": "radarr"}]}
        seen = []
        out = io.StringIO()
        with mock.patch.object(categories, "api_key", return_value="k"), \
                mock.patch.object(categories, "call",
                                  side_effect=lambda url, method="GET", **k:
                                  (seen.append(method) or (200, [client]))), \
                redirect_stdout(out), redirect_stderr(out):
            findings, _ = categories.check_arrs(manifest, Path("/appdata"), False, False)
        self.assertEqual(len(findings), 1)
        self.assertIn("'radarr'", findings[0])
        self.assertEqual(seen, ["GET"], "a check must not PUT")

    def test_a_missing_qbittorrent_client_is_named(self) -> None:
        manifest = {"arr_apps": [{"svc": "sonarr", "port": 8989, "api": "v3",
                                  "category": "tv"}]}
        with mock.patch.object(categories, "api_key", return_value="k"), \
                mock.patch.object(categories, "call", return_value=(200, [])), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            findings, _ = categories.check_arrs(manifest, Path("/appdata"), True, False)
        self.assertEqual(findings, ["sonarr: no qBittorrent download client"])


if __name__ == "__main__":
    unittest.main()
