#!/usr/bin/env python3
"""The block npm-theme.py writes onto an NPM proxy host.

These judge the compose/merge/strip rules directly: a re-run must not double the
block, an unrelated `advanced_config` must survive verbatim, and a revert must
remove exactly the marked region.
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The script sits in scripts/ and this test in scripts/tests/; accept either so
# the file can be run from a checkout without the split.
SCRIPT = next(p for p in (os.path.join(os.path.dirname(HERE), "npm-theme.py"),
                          os.path.join(HERE, "npm-theme.py")) if os.path.exists(p))
SPEC = importlib.util.spec_from_file_location("npm_theme", SCRIPT)
npm_theme = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(npm_theme)


class BlockTest(unittest.TestCase):
    def setUp(self):
        os.environ["MONARCH_THEME_BASE"] = "https://monarch.innotel.us/monarch"
        self.block = npm_theme.theme_block()

    def test_block_is_marked_and_injects_the_three_assets(self):
        self.assertIn(npm_theme.BEGIN, self.block)
        self.assertIn(npm_theme.END, self.block)
        self.assertIn("arr-theme.css", self.block)
        self.assertIn("arr-title.js", self.block)
        self.assertIn("monarch-crown.svg", self.block)
        self.assertIn('proxy_set_header Accept-Encoding "";', self.block)
        self.assertIn("sub_filter_once off;", self.block)
        self.assertIn("</head>", self.block)

    def test_base_is_configurable(self):
        os.environ["MONARCH_THEME_BASE"] = "https://example.test/t"
        try:
            self.assertIn("https://example.test/t/arr-theme.css", npm_theme.theme_block())
        finally:
            os.environ["MONARCH_THEME_BASE"] = "https://monarch.innotel.us/monarch"

    def test_merge_preserves_unrelated_config(self):
        existing = "client_max_body_size 0;"
        merged = npm_theme.merge(existing, self.block)
        self.assertIn("client_max_body_size 0;", merged)
        self.assertIn(npm_theme.BEGIN, merged)

    def test_merge_is_idempotent(self):
        once = npm_theme.merge("", self.block)
        twice = npm_theme.merge(once, self.block)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(npm_theme.BEGIN), 1)

    def test_merge_replaces_a_stale_block_but_keeps_other_lines(self):
        stale = npm_theme.BEGIN + "\nold line\n" + npm_theme.END + "\nkeep_me on;\n"
        merged = npm_theme.merge(stale, self.block)
        self.assertNotIn("old line", merged)
        self.assertIn("keep_me on;", merged)

    def test_strip_removes_only_the_region(self):
        config = "before on;\n" + self.block + "\nafter on;\n"
        stripped = npm_theme.strip_block(config)
        self.assertIn("before on;", stripped)
        self.assertIn("after on;", stripped)
        self.assertNotIn(npm_theme.BEGIN, stripped)
        self.assertNotIn("arr-theme.css", stripped)

    def test_strip_of_empty_is_empty(self):
        self.assertEqual(npm_theme.strip_block(""), "")

    def test_writable_fields_exclude_read_only(self):
        for immutable in ("id", "created_on", "modified_on", "owner_user_id"):
            self.assertNotIn(immutable, npm_theme.WRITABLE)
        self.assertIn("advanced_config", npm_theme.WRITABLE)


if __name__ == "__main__":
    unittest.main()
