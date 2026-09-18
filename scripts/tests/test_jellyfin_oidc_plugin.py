#!/usr/bin/env python3
"""Unit tests for jellyfin-oidc-plugin.py — the pinned Jellyfin OIDC plugin.

The cases these encode are the ways a plugin install goes wrong quietly: a build
that is not the pinned one (the plugin's own meta.json carries no sourceUrl, so
nothing else would notice), and a download whose *assembly* does not match even
though the zip looked right. `--install` must refuse both without writing into
the plugins directory, because Jellyfin loads whatever is there.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "jellyfin-oidc-plugin.py"

ASSEMBLY = "Jellyfin.Plugin.OIDC.dll"
GOOD_ASSEMBLY = b"MZ-fake-assembly-for-tests"
META = {"guid": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f90", "name": "OIDC RBAC",
        "versions": [{"version": "1.0.10.0"}]}


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_oidc_plugin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(path: Path, assembly: bytes = GOOD_ASSEMBLY, name: str = ASSEMBLY,
             extra: dict | None = None) -> Path:
    """A release zip shaped like the real one: assembly + meta.json at the root."""
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(name, assembly)
        bundle.writestr("meta.json", json.dumps(META))
        for member, payload in (extra or {}).items():
            bundle.writestr(member, payload)
    return path


class Pin:
    """A pin file and a plugins directory, both in a temp tree."""

    def __init__(self, assembly_bytes: bytes = GOOD_ASSEMBLY):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.manifest = self.root / "pin.json"
        self.manifest.write_text(json.dumps({
            "repo": "Ezeqielle/jellyfin-plugin-oidc",
            "tag": "v1.0.10",
            "version": "1.0.10.0",
            "asset": "oidc-rbac.zip",
            "asset_bytes": 0,
            "asset_sha256": "0" * 64,
            "assembly": ASSEMBLY,
            "assembly_sha256": sha(assembly_bytes),
            "plugin_dir": "OIDC-RBAC",
        }), encoding="utf-8")

    def with_asset(self, blob: bytes) -> "Pin":
        pin = json.loads(self.manifest.read_text())
        pin["asset_sha256"] = sha(blob)
        pin["asset_bytes"] = len(blob)
        self.manifest.write_text(json.dumps(pin), encoding="utf-8")
        return self

    def install(self, assembly_bytes: bytes = GOOD_ASSEMBLY, folder: str = "OIDC-RBAC") -> Path:
        target = self.plugins / folder
        target.mkdir()
        (target / ASSEMBLY).write_bytes(assembly_bytes)
        (target / "meta.json").write_text(json.dumps(META), encoding="utf-8")
        return target

    def zip_blob(self, assembly: bytes = GOOD_ASSEMBLY) -> bytes:
        archive = self.root / "asset.zip"
        make_zip(archive, assembly)
        return archive.read_bytes()

    def cleanup(self):
        self.tmp.cleanup()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin.main(args)
    return code, out.getvalue(), err.getvalue()


class ManifestTests(unittest.TestCase):
    def test_the_repo_pin_is_valid_and_complete(self):
        pin = plugin.load_pin(Path(__file__).resolve().parents[2] / "init"
                              / "jellyfin-oidc-plugin.json")
        self.assertEqual(pin["repo"], "Ezeqielle/jellyfin-plugin-oidc")
        self.assertEqual(pin["tag"], "v1.0.10")
        self.assertEqual(len(pin["asset_sha256"]), 64)
        self.assertEqual(len(pin["assembly_sha256"]), 64)
        self.assertTrue(pin["release_url"].endswith(pin["asset"]))

    def test_a_pin_missing_a_field_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pin.json"
            path.write_text(json.dumps({"repo": "x/y"}), encoding="utf-8")
            with self.assertRaises(plugin.CannotRun):
                plugin.load_pin(path)


class CheckTests(unittest.TestCase):
    def test_the_pinned_build_passes(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        pin.install()
        code, out, _ = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 0, out)
        self.assertIn("ok:", out)

    def test_a_different_build_is_drift(self):
        # The installed assembly is not the pinned one: a swap nobody wrote down.
        pin = Pin()
        self.addCleanup(pin.cleanup)
        pin.install(assembly_bytes=b"MZ-some-other-build")
        code, _, err = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 1)
        self.assertIn("but the pin is", err)

    def test_nothing_installed_is_a_failure_not_a_skip(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        code, _, err = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 1)
        self.assertIn("no SSO button", err)

    def test_the_assembly_is_found_outside_the_pinned_folder(self):
        # Jellyfin scans every subdirectory, so the folder name is not the
        # contract — the assembly is.
        pin = Pin()
        self.addCleanup(pin.cleanup)
        pin.install(folder="SomeOtherFolderName")
        code, out, _ = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 0, out)

    def test_an_unreadable_plugins_dir_cannot_be_judged(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        code, _, err = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.root / "nope")])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_status_reports_both_sides(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        pin.install()
        code, out, _ = run(["--status", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 0)
        self.assertIn("v1.0.10", out)
        self.assertIn("status      ok", out)


class InstallTests(unittest.TestCase):
    def test_install_writes_the_pinned_build(self):
        pin = Pin().with_asset(b"")            # asset hash is re-pinned below
        self.addCleanup(pin.cleanup)
        blob = pin.zip_blob()
        pin.with_asset(blob)
        source = pin.root / "mirror.zip"
        source.write_bytes(blob)
        code, out, _ = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", source.as_uri()])
        self.assertEqual(code, 0, out)
        self.assertIn("Restart Jellyfin", out)
        installed = pin.plugins / "OIDC-RBAC" / ASSEMBLY
        self.assertEqual(installed.read_bytes(), GOOD_ASSEMBLY)
        self.assertTrue((pin.plugins / "OIDC-RBAC" / "meta.json").is_file())

    def test_install_is_idempotent(self):
        pin = Pin().with_asset(b"")
        self.addCleanup(pin.cleanup)
        blob = pin.zip_blob()
        pin.with_asset(blob)
        source = pin.root / "mirror.zip"
        source.write_bytes(blob)
        run(["--install", "--manifest", str(pin.manifest), "--plugins-dir", str(pin.plugins),
             "--source-url", source.as_uri()])
        code, out, _ = run(["--check", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins)])
        self.assertEqual(code, 0, out)

    def test_a_zip_that_is_not_the_pin_is_refused(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        blob = pin.zip_blob()                  # hash is never pinned
        source = pin.root / "mirror.zip"
        source.write_bytes(blob)
        code, _, err = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", source.as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("the pin says", err)
        self.assertFalse((pin.plugins / "OIDC-RBAC").exists())

    def test_a_zip_whose_assembly_differs_is_refused_before_writing(self):
        # The zip hash matches the pin, but the assembly inside it does not: the
        # exact case a hash-on-the-zip-only check would miss.
        pin = Pin()
        self.addCleanup(pin.cleanup)
        blob = pin.zip_blob(assembly=b"MZ-swapped-build")
        pin.with_asset(blob)
        source = pin.root / "mirror.zip"
        source.write_bytes(blob)
        code, _, err = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", source.as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("does not match the pin", err)
        self.assertFalse((pin.plugins / "OIDC-RBAC").exists())

    def test_a_zip_without_the_assembly_is_refused(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        archive = pin.root / "wrong.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("something-else.dll", GOOD_ASSEMBLY)
        blob = archive.read_bytes()
        pin.with_asset(blob)
        code, _, err = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", archive.as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("does not contain", err)

    def test_an_unreachable_source_is_reported_not_crashed(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        code, _, err = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", (pin.root / "missing.zip").as_uri()])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)


class RealPinTests(unittest.TestCase):
    """The deployment's own pin, installed into a temp directory."""

    def test_the_repo_pin_installs_and_verifies(self):
        pin = Pin()
        self.addCleanup(pin.cleanup)
        repo_pin = plugin.load_pin(Path(__file__).resolve().parents[2] / "init"
                                   / "jellyfin-oidc-plugin.json")
        # The repository's pin describes a 420 KB release; here the same shape is
        # reproduced with a stand-in assembly so the test stays offline.
        payload = repo_pin | {"assembly_sha256": sha(GOOD_ASSEMBLY), "asset_bytes": 0}
        archive = pin.root / "asset.zip"
        make_zip(archive, GOOD_ASSEMBLY)
        blob = archive.read_bytes()
        payload["asset_sha256"] = sha(blob)
        payload["asset_bytes"] = len(blob)
        pin.manifest.write_text(json.dumps(payload), encoding="utf-8")
        mirror = pin.root / "mirror.zip"
        mirror.write_bytes(blob)
        code, out, _ = run(["--install", "--manifest", str(pin.manifest),
                            "--plugins-dir", str(pin.plugins),
                            "--source-url", mirror.as_uri()])
        self.assertEqual(code, 0, out)
        # And the install is recognised by the SSO check's own assembly rule.
        self.assertTrue((pin.plugins / "OIDC-RBAC" / ASSEMBLY).is_file())


if __name__ == "__main__":
    unittest.main()
