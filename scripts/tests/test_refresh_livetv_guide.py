#!/usr/bin/env python3
"""Tests for the XMLTV cache clearing in scripts/refresh-livetv-guide.py.

Jellyfin reuses `cache/xmltv/<provider-id>.xml` across Refresh Guide runs, so a
dial regenerated on disk is invisible until that copy is gone. The cache clear
must remove the downloaded guides and nothing else, and a host that has never
downloaded one must not be treated as broken.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "refresh-livetv-guide.py"
spec = importlib.util.spec_from_file_location("refresh_livetv_guide", SCRIPT)
assert spec and spec.loader
refresh_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh_mod)


class ClearingTheCache(unittest.TestCase):
    def test_removes_downloaded_guides_and_leaves_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "provider-a.xml").write_text("<tv/>", encoding="utf-8")
            (root / "provider-b.xml").write_text("<tv/>", encoding="utf-8")
            keep = root / "notes.json"
            keep.write_text("{}", encoding="utf-8")

            self.assertEqual(refresh_mod.clear_xmltv_cache(root), 2)
            self.assertFalse((root / "provider-a.xml").exists())
            self.assertFalse((root / "provider-b.xml").exists())
            self.assertTrue(keep.exists())

    def test_a_missing_cache_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "xmltv"
            self.assertEqual(refresh_mod.clear_xmltv_cache(missing), 0)


if __name__ == "__main__":
    unittest.main()
