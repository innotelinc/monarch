#!/usr/bin/env python3
"""Unit tests for jellyfin-plugin-pin.py — the pinned Jellyfin plugin builds.

The cases these encode are the ways a plugin install goes wrong quietly, and one
of them is not quiet at all:

  * a build that is not the pinned one (the OIDC plugin's `meta.json` carries no
    `sourceUrl` and the LDAP plugin an empty version list, so nothing else would
    notice), and
  * **two copies of one auth plugin** — LDAP-Auth v23 installed beside v24.
    Jellyfin loads both, the plugin's configuration type is cast across two load
    contexts, and every authentication throws `InvalidCastException`: the login
    form answers HTTP 500 for a correct password exactly as it does for a wrong
    one, which reads as "the password is wrong" to whoever is typing it.

`--install` must refuse a download whose zip *or* assembly does not match the pin
without writing into the plugins directory, because Jellyfin loads whatever is
there.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "jellyfin-plugin-pin.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

OIDC_ASSEMBLY = "Jellyfin.Plugin.OIDC.dll"
LDAP_ASSEMBLY = "LDAP-Auth.dll"
GOOD_OIDC = b"MZ-fake-oidc-assembly-for-tests"
GOOD_LDAP = b"MZ-fake-ldap-assembly-for-tests"
META = {"name": "OIDC RBAC", "guid": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f90",
        "versions": [{"version": "1.0.10.0"}]}


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_plugin_pin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pin = _load()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(path: Path, assembly: str, payload: bytes, extra: dict | None = None) -> Path:
    """A release zip shaped like the real ones: assembly + meta.json at the root."""
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(assembly, payload)
        bundle.writestr("meta.json", json.dumps(META))
        for member, data in (extra or {}).items():
            bundle.writestr(member, data)
    return path


class Pins:
    """A pin file and a plugins directory, both in a temp tree."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.manifest = self.root / "pins.json"
        self.blobs: dict[str, bytes] = {}
        payloads = {"oidc": (OIDC_ASSEMBLY, GOOD_OIDC, "OIDC-RBAC"),
                    "ldap": (LDAP_ASSEMBLY, GOOD_LDAP, "LDAP Authentication_24.0.0.0")}
        plugins = []
        for name, (assembly, data, folder) in payloads.items():
            archive = self.root / f"{name}.zip"
            make_zip(archive, assembly, data)
            blob = archive.read_bytes()
            self.blobs[name] = blob
            plugins.append({
                "name": name,
                "title": f"{name} (test)",
                "repo": f"example/{name}",
                "tag": f"v{name}",
                "version": "1.0.0.0",
                "asset": f"{name}.zip",
                "asset_bytes": len(blob),
                "asset_sha256": sha(blob),
                "assembly": assembly,
                "assembly_sha256": sha(data),
                "plugin_dir": folder,
                "release_url": f"https://example.invalid/{name}.zip",
            })
        self.write(plugins)

    def write(self, plugins: list[dict]) -> None:
        self.manifest.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")

    def pins(self) -> list[dict]:
        return json.loads(self.manifest.read_text())["plugins"]

    def repin(self, name: str, field: str, value) -> None:
        plugins = self.pins()
        for entry in plugins:
            if entry["name"] == name:
                entry[field] = value
        self.write(plugins)

    def install(self, name: str, folder: str | None = None,
                payload: bytes | None = None) -> Path:
        entry = pin.pin_named(self.pins(), name)
        target = self.plugins / (folder or entry["plugin_dir"])
        target.mkdir(parents=True)
        (target / entry["assembly"]).write_bytes(
            payload if payload is not None else
            (GOOD_OIDC if entry["assembly"] == OIDC_ASSEMBLY else GOOD_LDAP))
        (target / "meta.json").write_text(json.dumps(META), encoding="utf-8")
        return target

    def mirror(self, name: str) -> str:
        """The pinned zip, on local disk, as a file:// URL."""
        path = self.root / f"mirror-{name}.zip"
        path.write_bytes(self.blobs[name])
        return path.as_uri()

    def cleanup(self):
        self.tmp.cleanup()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = pin.main(args)
    return code, out.getvalue(), err.getvalue()


class PinFileTests(unittest.TestCase):
    def test_the_repo_pin_file_is_valid_and_covers_both_plugins(self):
        pins = pin.load_pins(REPO_ROOT / "init" / "jellyfin-plugins.json")
        self.assertEqual([p["name"] for p in pins], ["oidc", "ldap"])
        for entry in pins:
            self.assertEqual(len(entry["asset_sha256"]), 64, entry["name"])
            self.assertEqual(len(entry["assembly_sha256"]), 64, entry["name"])
            self.assertTrue(entry["release_url"].endswith(entry["asset"]), entry["name"])

    def test_the_ldap_pin_is_the_build_the_deployment_runs(self):
        # The v24 assembly, not the v23 that was installed beside it and made
        # every authentication fail with HTTP 500.
        entry = pin.pin_named(pin.load_pins(REPO_ROOT / "init" / "jellyfin-plugins.json"), "ldap")
        self.assertEqual(entry["tag"], "v24")
        self.assertEqual(entry["assembly_sha256"],
                         "79b5d443a3ef3da9dff79a3a7df5620804b39d8e590f756b074b679e85c800cf")

    def test_a_pin_missing_a_field_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins.json"
            path.write_text(json.dumps({"plugins": [{"name": "x"}]}), encoding="utf-8")
            with self.assertRaises(pin.CannotRun):
                pin.load_pins(path)

    def test_an_empty_pin_file_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins.json"
            path.write_text(json.dumps({"plugins": []}), encoding="utf-8")
            with self.assertRaises(pin.CannotRun):
                pin.load_pins(path)

    def test_an_unknown_name_is_reported_not_guessed(self):
        with self.assertRaises(pin.CannotRun):
            pin.pin_named(pin.load_pins(REPO_ROOT / "init" / "jellyfin-plugins.json"), "nginx")


class CheckTests(unittest.TestCase):
    def test_both_pinned_builds_pass(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("oidc")
        site.install("ldap")
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 0, out)
        self.assertIn("every pinned plugin is the installed build", out)

    def test_a_different_build_is_drift(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("oidc", payload=b"MZ-some-other-build")
        site.install("ldap")
        code, _, err = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 1)
        self.assertIn("but the pin is", err)

    def test_a_second_copy_of_one_auth_plugin_is_a_failure(self):
        # The exact outage: v23 left installed beside v24.
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("ldap")
        site.install("ldap", folder="LDAP-Auth")
        code, _, err = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 1)
        self.assertIn("2 installed folders carry LDAP-Auth.dll", err)
        self.assertIn("LDAP Authentication_24.0.0.0, LDAP-Auth", err)
        self.assertIn("fails every authentication", err)

    def test_a_retired_copy_is_not_a_second_copy(self):
        # The fix for that outage: the older folder is renamed, and Jellyfin
        # reports it as Superseded rather than loading it.
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("ldap")
        site.install("ldap", folder="LDAP-Auth.superseded-20260918-140321",
                     payload=b"MZ-the-old-v23")
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "ldap"])
        self.assertEqual(code, 0, out)

    def test_nothing_installed_is_a_failure_not_a_skip(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        code, _, err = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 1)
        self.assertIn("no installed plugin carries", err)

    def test_the_assembly_is_found_outside_the_pinned_folder(self):
        # Jellyfin scans every subdirectory, so the folder name is not the
        # contract — the assembly is.
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("oidc", folder="SomeOtherFolderName")
        site.install("ldap")
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 0, out)

    def test_one_plugin_can_be_judged_on_its_own(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("oidc")
        code, _, err = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "ldap"])
        self.assertEqual(code, 1)
        self.assertIn("LDAP-Auth.dll", err)

    def test_an_unreadable_plugins_dir_cannot_be_judged(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        code, _, err = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.root / "nope")])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_status_reports_both_sides_of_every_pin(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("oidc")
        site.install("ldap")
        code, out, _ = run(["--status", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 0)
        self.assertIn("oidc", out)
        self.assertIn("ldap", out)
        self.assertIn("status      ok", out)


class InstallTests(unittest.TestCase):
    def test_install_writes_the_pinned_build(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        code, out, _ = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins),
                            "--plugin", "oidc", "--source-url", site.mirror("oidc")])
        self.assertEqual(code, 0, out)
        self.assertIn("Restart Jellyfin", out)
        entry = pin.pin_named(site.pins(), "oidc")
        installed = site.plugins / entry["plugin_dir"] / entry["assembly"]
        self.assertEqual(installed.read_bytes(), GOOD_OIDC)
        self.assertTrue((site.plugins / entry["plugin_dir"] / "meta.json").is_file())

    def test_install_is_idempotent(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        run(["--install", "--manifest", str(site.manifest), "--plugins-dir",
             str(site.plugins), "--plugin", "oidc", "--source-url", site.mirror("oidc")])
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "oidc"])
        self.assertEqual(code, 0, out)

    def test_install_leaves_a_duplicate_alone_and_says_so(self):
        # Installing cannot fix two folders — retiring one is the operator's move.
        site = Pins()
        self.addCleanup(site.cleanup)
        site.install("ldap")
        site.install("ldap", folder="LDAP-Auth")
        code, _, err = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "ldap",
                            "--source-url", site.mirror("ldap")])
        self.assertEqual(code, 1)
        self.assertIn("2 installed folders carry", err)

    def test_a_zip_that_is_not_the_pin_is_refused(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        site.repin("oidc", "asset_sha256", "0" * 64)
        code, _, err = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "oidc",
                            "--source-url", site.mirror("oidc")])
        self.assertEqual(code, 1)
        self.assertIn("the pin says", err)
        self.assertFalse((site.plugins / "OIDC-RBAC").exists())

    def test_a_zip_whose_assembly_differs_is_refused_before_writing(self):
        # The zip hash matches the pin, but the assembly inside it does not: the
        # exact case a hash-on-the-zip-only check would miss.
        site = Pins()
        self.addCleanup(site.cleanup)
        swapped = site.root / "swapped.zip"
        make_zip(swapped, OIDC_ASSEMBLY, b"MZ-swapped-build")
        blob = swapped.read_bytes()
        site.repin("oidc", "asset_sha256", sha(blob))
        site.repin("oidc", "asset_bytes", len(blob))
        code, _, err = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "oidc",
                            "--source-url", swapped.as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("does not match the pin", err)
        self.assertFalse((site.plugins / "OIDC-RBAC").exists())

    def test_a_zip_without_the_assembly_is_refused(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        archive = site.root / "wrong.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("something-else.dll", GOOD_OIDC)
        blob = archive.read_bytes()
        site.repin("oidc", "asset_sha256", sha(blob))
        site.repin("oidc", "asset_bytes", len(blob))
        code, _, err = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "oidc",
                            "--source-url", archive.as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("does not contain", err)

    def test_an_unreachable_source_is_reported_not_crashed(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        code, _, err = run(["--install", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins), "--plugin", "oidc",
                            "--source-url", (site.root / "missing.zip").as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)

    def test_install_repairs_every_missing_plugin(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        for name in ("oidc", "ldap"):
            code, out, _ = run(["--install", "--manifest", str(site.manifest),
                                "--plugins-dir", str(site.plugins),
                                "--plugin", name, "--source-url", site.mirror(name)])
            self.assertEqual(code, 0, out)
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 0, out)


class RealPinTests(unittest.TestCase):
    """The repo's own pin file, installed into a temp directory."""

    def test_the_repo_pins_install_and_verify(self):
        site = Pins()
        self.addCleanup(site.cleanup)
        repo_pins = pin.load_pins(REPO_ROOT / "init" / "jellyfin-plugins.json")
        payloads = {"oidc": GOOD_OIDC, "ldap": GOOD_LDAP}
        rewritten = []
        for entry in repo_pins:
            name = entry["name"]
            archive = site.root / f"real-{name}.zip"
            make_zip(archive, entry["assembly"], payloads[name])
            blob = archive.read_bytes()
            site.blobs[name] = blob
            rewritten.append(entry | {"asset_sha256": sha(blob), "asset_bytes": len(blob),
                                      "assembly_sha256": sha(payloads[name])})
        site.write(rewritten)

        for entry in rewritten:
            code, out, _ = run(["--install", "--manifest", str(site.manifest),
                                "--plugins-dir", str(site.plugins),
                                "--plugin", entry["name"], "--source-url", site.mirror(entry["name"])])
            self.assertEqual(code, 0, out)
        code, out, _ = run(["--check", "--manifest", str(site.manifest),
                            "--plugins-dir", str(site.plugins)])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
