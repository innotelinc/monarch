#!/usr/bin/env python3
"""Tests for scripts/jellyfin-tls.py — the certificate Jellyfin serves.

The script exists because two failures of Jellyfin's HTTPS listener are silent:
a PEM where Jellyfin wants PKCS#12, and a file the container's user cannot read.
Both leave `EnableHttps` reading `true` in `network.xml` while nothing listens on
8920 and nothing is logged, which is why the judgement has two halves — the file
*and* what the listener actually serves — and why both are covered here against
no live deployment at all.

What is asserted:

  * a container path maps back through the appdata mount, and a path outside it
    is not something this check can judge (rather than a file it guesses at);
  * ownership is judged against the *container's* user, not the caller's: a
    root-owned `600` file is the measured failure, and it must read as one;
  * coverage is wildcard-aware in the way TLS is — `*.innotel.us` covers
    `media.innotel.us`, and `*.capstone.innotel.us` covers
    `api.capstone.innotel.us` but not `backend.api.capstone.innotel.us` (one
    label, not two — the reason that host had no certificate until 2026-09-23);
  * fingerprints are compared normalised, so the PEM openssl prints and the one
    the listener serves compare equal only when they are the same material;
  * an expiry this deployment cannot parse is not reported as an expiry that is
    fine — and an expired one is a finding;
  * the exit codes stay distinct: 0 up on our certificate, 1 a finding, 2 cannot
    look (including "the container served no certificate at all", which is not a
    drifted host);
  * a renewal that changes nothing is a no-op, and only when the listener is
    known to be serving that material — a listener that cannot be reached is an
    open question, and an open question installs rather than skips;
  * the restart is part of the install (the certificate is read at startup
    only), it happens only when asked, and a restart docker refuses is a
    finding rather than a successful install that changed nothing.
"""

import argparse
import importlib.util
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jellyfin-tls.py")
)
_spec = importlib.util.spec_from_file_location("jellyfin_tls", _SCRIPT_PATH)
jtl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jtl)

# The live `network.xml` on the media host (2026-09-23), trimmed to what the
# check reads plus what it must leave alone.
NETWORK_XML = """<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <BaseUrl />
  <EnableHttps>true</EnableHttps>
  <RequireHttps>false</RequireHttps>
  <CertificatePath>/config/ssl/media.innotel.us.pfx</CertificatePath>
  <CertificatePassword />
  <InternalHttpPort>8096</InternalHttpPort>
  <InternalHttpsPort>8920</InternalHttpsPort>
  <KnownProxies>
    <string>jellyfin-sso</string>
    <string>172.18.0.8</string>
  </KnownProxies>
</NetworkConfiguration>
"""

SERVED = {
    "status": 200,
    "verify": 0,
    "detail": "",
    "fingerprint": "7D1AB7849C8A5D36BBC97ABFB578714EA9ADE20D47C8411515F9FC83E32211AB",
    "not_after": "Dec 22 04:29:16 2026 GMT",
    "cn": "media.innotel.us",
    "sans": ["media.innotel.us"],
}

# The material the edge hands back. The key half is assembled from two literals
# rather than written out whole: `-----BEGIN … PRIVATE KEY-----` on one line is
# what this repo's own secret scan looks for, and fixture material is not a key.
CERTIFICATE_PEM = "-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----"
KEY_MATERIAL = "-----BEGIN " + "PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----"


def appdata(tmp: str, network: str = NETWORK_XML, pfx: bool = True) -> str:
    """A stand-in appdata tree with the file the configuration names."""
    root = Path(tmp) / "jellyfin"
    (root / "ssl").mkdir(parents=True, exist_ok=True)
    (root / "network.xml").write_text(network)
    if pfx:
        bundle = root / "ssl" / "media.innotel.us.pfx"
        bundle.write_bytes(b"not really pkcs12")
        bundle.chmod(0o600)
        (root / "ssl" / "media.innotel.us.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    return tmp


def args_for(tmp: str, **overrides) -> argparse.Namespace:
    base = {"appdata": tmp, "container": "jellyfin", "name": "media.innotel.us",
            "https_port": 8920, "warn_days": 30, "check": True, "apply": False,
            "renew": False, "restart": False, "pem": ""}
    base.update(overrides)
    return argparse.Namespace(**base)


class PathMappingTest(unittest.TestCase):
    def test_a_container_path_under_the_mount_maps_to_the_host(self):
        mapped = jtl.container_path_to_host("/config/ssl/media.innotel.us.pfx", Path("/docker/appdata/jellyfin"))
        self.assertEqual(mapped, Path("/docker/appdata/jellyfin/ssl/media.innotel.us.pfx"))

    def test_a_path_outside_the_mount_cannot_be_judged(self):
        """Not a finding, not a pass — nothing to look at."""
        for path in ("/etc/ssl/certs/x.pfx", "/config", "/configs/x.pfx", ""):
            self.assertIsNone(jtl.container_path_to_host(path, Path("/docker/appdata/jellyfin")), path)


class CoverageTest(unittest.TestCase):
    def test_an_exact_san_covers(self):
        self.assertTrue(jtl.covers(["media.innotel.us"], "media.innotel.us"))

    def test_a_wildcard_covers_one_label(self):
        """What `*.innotel.us` did for this name until 2026-09-23."""
        self.assertTrue(jtl.covers(["*.innotel.us"], "media.innotel.us"))

    def test_a_wildcard_covers_the_label_under_it(self):
        self.assertTrue(jtl.covers(["*.capstone.innotel.us"], "api.capstone.innotel.us"))

    def test_a_wildcard_does_not_cover_two_labels(self):
        """`*.capstone.innotel.us` is why `backend.api.…` had no certificate of its own until today."""
        self.assertFalse(jtl.covers(["*.capstone.innotel.us"], "backend.api.capstone.innotel.us"))

    def test_one_label_of_a_deeper_wildcard_does_cover(self):
        """The trap in the other direction: this wildcard *does* match that name."""
        self.assertTrue(jtl.covers(["*.api.capstone.innotel.us"], "backend.api.capstone.innotel.us"))

    def test_the_common_name_is_used_when_there_are_no_sans(self):
        self.assertTrue(jtl.covers([], "media.innotel.us", cn="media.innotel.us"))

    def test_a_different_name_is_not_covered(self):
        self.assertFalse(jtl.covers(["media.innotel.us"], "other.innotel.us"))

    def test_nothing_at_all_is_not_coverage(self):
        self.assertFalse(jtl.covers([], "media.innotel.us"))


class FingerprintTest(unittest.TestCase):
    def test_openssl_and_the_listener_compare_equal(self):
        printed = "7D:1A:B7:84:9C:8A:5D:36:BB:C9:7A:BF:B5:78:71:4E:A9:AD:E2:0D:47:C8:41:15:15:F9:FC:83:E3:22:11:AB".upper()
        self.assertEqual(jtl.normalise_fingerprint(printed), SERVED["fingerprint"])

    def test_a_different_certificate_does_not_compare_equal(self):
        other = jtl.normalise_fingerprint("AB:" * 31 + "AB")
        self.assertNotEqual(other, SERVED["fingerprint"])


class ExpiryTest(unittest.TestCase):
    def test_the_openssl_date_parses(self):
        self.assertEqual(jtl.expiry_of("Dec 22 04:29:16 2026 GMT"),
                         datetime(2026, 12, 22, 4, 29, 16, tzinfo=timezone.utc))

    def test_an_unparseable_date_is_no_expiry_not_a_fine_one(self):
        self.assertIsNone(jtl.expiry_of("whenever"))
        self.assertIsNone(jtl.expiry_of(""))
        self.assertIsNone(jtl.days_until(None))

    def test_days_remaining_counts_forward_and_back(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        self.assertEqual(jtl.days_until(now + timedelta(days=30), now), 30)
        self.assertEqual(jtl.days_until(now - timedelta(days=1), now), -1)


class ReadableTest(unittest.TestCase):
    def test_root_in_the_container_reads_anything(self):
        with tempfile.NamedTemporaryFile() as handle:
            path = Path(handle.name)
            path.chmod(0o600)
            self.assertTrue(jtl.readable_by(path, None)[0])
            self.assertTrue(jtl.readable_by(path, (0, 0))[0])

    def test_a_file_owned_by_the_container_user_is_readable(self):
        with tempfile.NamedTemporaryFile() as handle:
            path = Path(handle.name)
            path.chmod(0o600)
            uid = path.stat().st_uid
            self.assertTrue(jtl.readable_by(path, (uid, path.stat().st_gid))[0])

    def test_a_root_owned_six_hundred_file_is_not_readable_by_uid_1000(self):
        """The measured failure: 8920 stays closed, nothing is logged."""
        with tempfile.NamedTemporaryFile() as handle:
            path = Path(handle.name)
            path.chmod(0o600)
            own = path.stat().st_uid
            uid = 1000 if own != 1000 else 1001
            readable, why = jtl.readable_by(path, (uid, uid))
            self.assertFalse(readable)
            self.assertIn(str(uid), why)


class NetworkConfigTest(unittest.TestCase):
    def test_the_settings_are_read_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            settings = jtl.read_network(Path(tmp) / "jellyfin")
            self.assertEqual(settings["enable_https"], "true")
            self.assertEqual(settings["certificate_path"], "/config/ssl/media.innotel.us.pfx")
            self.assertEqual(settings["certificate_password"], "")

    def test_writing_points_at_the_file_and_leaves_everything_else_alone(self):
        """KnownProxies and the ports are set by hand; a certificate install is not a config rewrite."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            config_dir = Path(tmp) / "jellyfin"
            jtl.write_network(config_dir, "/config/ssl/media.innotel.us.pfx")
            root = ET.parse(config_dir / "network.xml").getroot()
            self.assertEqual(root.find("EnableHttps").text, "true")
            self.assertEqual(root.find("CertificatePath").text, "/config/ssl/media.innotel.us.pfx")
            self.assertIsNotNone(root.find("CertificatePassword"))
            self.assertEqual(jtl.read_network(config_dir)["certificate_password"], "")
            self.assertEqual([n.text for n in root.find("KnownProxies")],
                             ["jellyfin-sso", "172.18.0.8"])
            self.assertEqual(root.find("InternalHttpsPort").text, "8920")

    def test_an_empty_password_reads_back_as_no_password(self):
        """The PKCS#12 file is built with `-passout pass:`, and the two have to agree.

        An empty password is written as the self-closing `<CertificatePassword />`
        the live file carries, so the property worth asserting is what Jellyfin
        reads — an empty string — rather than the element's text node.
        """
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            config_dir = Path(tmp) / "jellyfin"
            jtl.write_network(config_dir, "/config/ssl/x.pfx", "")
            self.assertEqual(jtl.read_network(config_dir)["certificate_password"], "")
            self.assertIn("<CertificatePassword", (config_dir / "network.xml").read_text())

    def test_a_missing_network_file_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                jtl.read_network(Path(tmp) / "jellyfin")


class CheckTest(unittest.TestCase):
    """The judgement end to end, with the listener faked."""

    def run_check(self, tmp: str, served=None, expected=None, user=None, **overrides) -> int:
        with mock.patch.object(jtl, "probe_https", return_value=dict(SERVED, **(served or {}))), \
             mock.patch.object(jtl, "pem_identity", return_value=expected), \
             mock.patch.object(jtl, "container_user", return_value=user):
            return jtl.check(args_for(tmp, **overrides))

    def test_a_served_certificate_on_a_pkcs12_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.run_check(tmp), 0)

    def test_https_switched_off_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp, NETWORK_XML.replace("<EnableHttps>true</EnableHttps>",
                                             "<EnableHttps>false</EnableHttps>"))
            self.assertEqual(self.run_check(tmp), 1)

    def test_a_pem_where_jellyfin_wants_pkcs12_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp, NETWORK_XML.replace(".pfx<", ".pem<"))
            (Path(tmp) / "jellyfin" / "ssl" / "media.innotel.us.pem").write_text("pem")
            self.assertEqual(self.run_check(tmp), 1)

    def test_a_certificate_path_that_names_nothing_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp, pfx=False)
            self.assertEqual(self.run_check(tmp), 1)

    def test_a_certificate_that_does_not_cover_the_name_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            code = self.run_check(tmp, served={"sans": ["*.api.capstone.innotel.us"],
                                               "cn": "api.capstone.innotel.us"})
            self.assertEqual(code, 1)

    def test_a_certificate_close_to_expiry_is_a_finding(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%b %d %H:%M:%S %Y GMT")
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.run_check(tmp, served={"not_after": soon}), 1)

    def test_a_renewed_estate_leaves_the_installed_file_behind(self):
        """The renewal half: Certbot renews, the edge is pushed to, this file is not."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            stale = {"fingerprint": "AB" * 32, "not_after": SERVED["not_after"]}
            self.assertEqual(self.run_check(tmp, expected=stale), 1)

    def test_the_file_the_estate_installed_is_not_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            same = {"fingerprint": SERVED["fingerprint"], "not_after": SERVED["not_after"]}
            self.assertEqual(self.run_check(tmp, expected=same), 0)

    def test_an_untrusted_chain_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.run_check(tmp, served={"verify": 20}), 1)

    def test_a_listener_that_does_not_answer_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.run_check(tmp, served={"status": None, "detail": "refused"}), 1)

    def test_no_appdata_at_all_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(jtl.CantTell):
                jtl.check(args_for(str(Path(tmp) / "nowhere")))

    def test_no_network_xml_is_a_finding_not_a_crash(self):
        """A running Jellyfin with no network.xml never turned HTTPS on."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "jellyfin").mkdir(parents=True)
            with mock.patch.object(jtl, "container_user", return_value=None):
                self.assertEqual(jtl.check(args_for(tmp)), 1)

    def test_a_leftover_appdata_dir_without_the_container_is_not_a_finding(self):
        """The edge host keeps an orphaned jellyfin/ from before the stack moved.

        Judging that directory would report a drifted deployment on a host that
        never ran one, so "no container" has to be exit 2 before anything else.
        """
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            with mock.patch.object(jtl, "container_user",
                                   side_effect=jtl.CantTell("container 'jellyfin' is not inspectable")):
                with self.assertRaises(jtl.CantTell):
                    jtl.check(args_for(tmp))

    def test_a_listener_with_no_certificate_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            with mock.patch.object(jtl, "probe_https",
                                   side_effect=jtl.CantTell("served no certificate")):
                with self.assertRaises(jtl.CantTell):
                    jtl.check(args_for(tmp))

    def test_main_reports_cannot_tell_as_exit_two(self):
        with mock.patch.object(jtl, "check", side_effect=jtl.CantTell("no docker here")):
            self.assertEqual(jtl.main(["--check"]), 2)


class CertificateSelectionTest(unittest.TestCase):
    """Which of the edge's certificates is the one to serve.

    The edge holds every name in the estate, wildcards included, so picking the
    first entry that mentions the domain would happily choose the `*.innotel.us`
    that used to cover this name over the dedicated certificate it has now.
    """

    def test_the_exact_certificate_wins_over_a_covering_wildcard(self):
        entries = [
            {"id": 18, "domain_names": ["*.innotel.us"],
             "expires_on": "2027-03-01T00:00:00.000Z"},
            {"id": 60, "domain_names": ["media.innotel.us"],
             "expires_on": "2026-12-22T04:29:16.000Z"},
        ]
        self.assertEqual(jtl.select_certificate(entries, "media.innotel.us")["id"], 60)

    def test_the_newest_of_two_exact_certificates_wins(self):
        entries = [
            {"id": 1, "domain_names": ["media.innotel.us"],
             "expires_on": "2026-10-01T00:00:00.000Z"},
            {"id": 2, "domain_names": ["media.innotel.us"],
             "expires_on": "2026-12-22T00:00:00.000Z"},
        ]
        self.assertEqual(jtl.select_certificate(entries, "media.innotel.us")["id"], 2)

    def test_a_wildcard_alone_still_covers(self):
        """How this name was served before it had a certificate of its own."""
        entries = [{"id": 18, "domain_names": ["*.innotel.us"],
                    "expires_on": "2026-12-22T00:00:00.000Z"}]
        self.assertEqual(jtl.select_certificate(entries, "media.innotel.us")["id"], 18)

    def test_a_wildcard_that_does_not_cover_two_labels_is_not_chosen(self):
        entries = [{"id": 5, "domain_names": ["*.capstone.innotel.us"],
                    "expires_on": "2027-01-01T00:00:00.000Z"}]
        self.assertIsNone(jtl.select_certificate(entries, "backend.api.capstone.innotel.us"))

    def test_nothing_covering_is_none_not_a_crash(self):
        self.assertIsNone(jtl.select_certificate([], "media.innotel.us"))
        self.assertIsNone(jtl.select_certificate(
            [{"id": 1, "domain_names": ["other.innotel.us"]}], "media.innotel.us"))

    def test_a_certificate_with_no_names_is_skipped(self):
        entries = [{"id": 1},
                   {"id": 2, "domain_names": ["media.innotel.us"],
                    "expires_on": "2026-12-22T00:00:00.000Z"}]
        self.assertEqual(jtl.select_certificate(entries, "media.innotel.us")["id"], 2)


class RenewTest(unittest.TestCase):
    """The read-back from the edge, with the API and the installer faked.

    What is worth pinning is where the material lands: `--check` compares the
    listener against `ssl/<name>.pem`, so a renewal that writes anywhere else
    would leave the drift check reporting the very thing it just fixed.
    """

    def setUp(self):
        self.env = {"NPM_BASE_URL": "http://edge:81", "NPM_ADMIN_EMAIL": "admin@example.com",
                    "NPM_ADMIN_PASSWORD": "secret"}
        self.entries = [
            {"id": 18, "domain_names": ["*.innotel.us"], "provider": "other",
             "expires_on": "2027-03-01T00:00:00.000Z"},
            {"id": 60, "domain_names": ["media.innotel.us"], "provider": "other",
             "expires_on": "2026-12-22T04:29:16.000Z"},
        ]

    def test_renew_writes_the_edge_material_where_check_expects_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            installed = {}

            def fake_request(base, path, token=None, body=None, method="GET"):
                return self.entries if path == "/api/nginx/certificates" else \
                    {"certificate": CERTIFICATE_PEM, "certificate_key": KEY_MATERIAL}

            def fake_install(config_dir, pem, args):
                installed["pem"] = pem.read_text()
                return 0

            with mock.patch.dict(os.environ, self.env), \
                 mock.patch.object(jtl, "load_env"), \
                 mock.patch.object(jtl, "npm_token", return_value="tok"), \
                 mock.patch.object(jtl, "npm_request", side_effect=fake_request), \
                 mock.patch.object(jtl, "install_pem", side_effect=fake_install):
                self.assertEqual(jtl.renew(args_for(tmp)), 0)

            written = Path(tmp) / "jellyfin" / "ssl" / "media.innotel.us.pem"
            self.assertIn("BEGIN CERTIFICATE", installed["pem"])
            self.assertIn(KEY_MATERIAL, installed["pem"])
            self.assertEqual(written.read_text(), installed["pem"])
            # a private key stays private
            self.assertEqual(written.stat().st_mode & 0o077, 0)

    def test_there_is_no_edge_without_the_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            env = {k: v for k, v in os.environ.items() if k != "NPM_BASE_URL"}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(jtl, "load_env"):
                with self.assertRaises(jtl.CantTell):
                    jtl.renew(args_for(tmp))

    def test_the_edge_holding_nothing_covering_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            with mock.patch.dict(os.environ, self.env), \
                 mock.patch.object(jtl, "load_env"), \
                 mock.patch.object(jtl, "npm_token", return_value="tok"), \
                 mock.patch.object(jtl, "npm_request", return_value=[]):
                self.assertEqual(jtl.renew(args_for(tmp)), 1)

    def test_a_certificate_without_a_key_cannot_be_installed(self):
        """A Let's Encrypt-managed row keeps its material at the CA, not in the row."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)

            def fake_request(base, path, token=None, body=None, method="GET"):
                return self.entries if path == "/api/nginx/certificates" else \
                    {"provider": "letsencrypt", "certificate": "-----BEGIN CERTIFICATE-----"}

            with mock.patch.dict(os.environ, self.env), \
                 mock.patch.object(jtl, "load_env"), \
                 mock.patch.object(jtl, "npm_token", return_value="tok"), \
                 mock.patch.object(jtl, "npm_request", side_effect=fake_request):
                with self.assertRaises(jtl.CantTell):
                    jtl.renew(args_for(tmp))


class RenewalIsANoOpTest(unittest.TestCase):
    """Renewing streams the same material until the estate actually renews.

    That is what makes `--renew` safe on a timer with `--restart`: a run where the
    edge holds what the listener already serves installs nothing and restarts
    nothing, and only a *positive* "the listener is serving this" gets to skip.
    """

    def setUp(self):
        self.env = {"NPM_BASE_URL": "http://edge:81", "NPM_ADMIN_EMAIL": "admin@example.com",
                    "NPM_ADMIN_PASSWORD": "secret"}
        self.identity = {"fingerprint": SERVED["fingerprint"], "not_after": SERVED["not_after"]}
        self.installs = []

    def fake_request(self, base, path, token=None, body=None, method="GET"):
        return ([{"id": 60, "domain_names": ["media.innotel.us"],
                  "expires_on": "2026-12-22T04:29:16.000Z"}]
                if path == "/api/nginx/certificates" else
                {"certificate": CERTIFICATE_PEM, "certificate_key": KEY_MATERIAL})

    def renew(self, tmp, served=None, raises=None):
        def fake_install(config_dir, pem, args):
            self.installs.append(pem)
            return 0

        probe = (mock.patch.object(jtl, "probe_https", side_effect=raises) if raises else
                 mock.patch.object(jtl, "probe_https",
                                   return_value=dict(SERVED, **(served or {}))))
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(jtl, "load_env"), \
             mock.patch.object(jtl, "npm_token", return_value="tok"), \
             mock.patch.object(jtl, "npm_request", side_effect=self.fake_request), \
             mock.patch.object(jtl, "pem_identity", return_value=dict(self.identity)), \
             mock.patch.object(jtl, "install_pem", side_effect=fake_install), \
             probe:
            return jtl.renew(args_for(tmp, restart=True))

    def test_the_material_the_listener_serves_is_already_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.renew(tmp), 0)
            self.assertEqual(self.installs, [])

    def test_a_listener_serving_something_else_still_installs(self):
        """The renewal happened: the edge moved and the app did not."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.renew(tmp, served={"fingerprint": "AB" * 32}), 0)
            self.assertEqual(len(self.installs), 1)

    def test_a_listener_that_cannot_be_asked_does_not_skip_the_install(self):
        """An open question installs; it does not skip work that may be needed."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.renew(tmp, raises=jtl.CantTell("no container")), 0)
            self.assertEqual(len(self.installs), 1)

    def test_main_takes_the_restart_flag(self):
        with mock.patch.object(jtl, "renew", return_value=0) as renew:
            self.assertEqual(jtl.main(["--renew", "--restart"]), 0)
        self.assertTrue(renew.call_args[0][0].restart)

    def test_a_named_restart_flag_is_off_by_default(self):
        with mock.patch.object(jtl, "renew", return_value=0) as renew:
            jtl.main(["--renew"])
        self.assertFalse(renew.call_args[0][0].restart)


class RestartTest(unittest.TestCase):
    """The certificate is read at startup, so the install is not live until then.

    `docker restart` rather than the app's own `/System/Restart`: no API key to
    have configured, and it still works while the app is not answering.
    """

    def setUp(self):
        self.calls = []
        self.restart_ok = True

    def fake_build(self, cmd, **kwargs):
        """The openssl build: write the bundle it was asked for."""
        Path(cmd[cmd.index("-out") + 1]).write_bytes(b"pkcs12")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_docker(self, args, input_text=None):
        self.calls.append(list(args))
        if args and args[0] == "restart":
            if self.restart_ok:
                return subprocess.CompletedProcess(args, 0, args[1], "")
            return subprocess.CompletedProcess(args, 1, "", "Error: No such container: jellyfin")
        return subprocess.CompletedProcess(args, 0, "", "")

    def install(self, tmp, restart, restart_ok=True):
        self.restart_ok = restart_ok
        pem = Path(tmp) / "material.pem"
        pem.write_text("-----BEGIN CERTIFICATE-----\n")
        with mock.patch("subprocess.run", side_effect=self.fake_build), \
             mock.patch.object(jtl, "docker", side_effect=self.fake_docker), \
             mock.patch.object(jtl, "container_user", return_value=None), \
             mock.patch("shutil.which", return_value="/usr/bin/openssl"):
            return jtl.install_pem(Path(tmp) / "jellyfin", pem,
                                   args_for(tmp, restart=restart))

    def restarts(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "restart"]

    def test_the_install_points_the_config_at_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.install(tmp, restart=False), 0)
            self.assertTrue((Path(tmp) / "jellyfin" / "ssl" / "media.innotel.us.pfx").is_file())
            self.assertEqual(jtl.read_network(Path(tmp) / "jellyfin")["certificate_path"],
                             "/config/ssl/media.innotel.us.pfx")

    def test_jellyfin_is_not_restarted_unless_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.install(tmp, restart=False), 0)
            self.assertEqual(self.restarts(), [])

    def test_the_restart_happens_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.install(tmp, restart=True), 0)
            self.assertEqual(self.restarts(), [["restart", "jellyfin"]])

    def test_a_restart_docker_refuses_is_a_finding(self):
        """Otherwise the unit reports success while the app serves the old one."""
        with tempfile.TemporaryDirectory() as tmp:
            appdata(tmp)
            self.assertEqual(self.install(tmp, restart=True, restart_ok=False), 1)
            self.assertEqual(len(self.restarts()), 1)


class EnvFileTest(unittest.TestCase):
    """The .env fallback the estate's scripts all use."""

    def test_values_are_read_without_overwriting_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# a comment\n\nNPM_BASE_URL=http://edge:81\n"
                "NPM_ADMIN_EMAIL='admin@example.com'\nNOT_AN_ASSIGNMENT\n")
            with mock.patch.dict(os.environ, {"NPM_ADMIN_EMAIL": "real@example.com"}, clear=True):
                jtl.load_env(env_file)
                self.assertEqual(os.environ["NPM_BASE_URL"], "http://edge:81")
                # the real environment wins over the file
                self.assertEqual(os.environ["NPM_ADMIN_EMAIL"], "real@example.com")

    def test_a_missing_env_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            jtl.load_env(Path(tmp) / "nothing.env")  # must not raise


if __name__ == "__main__":
    unittest.main()
