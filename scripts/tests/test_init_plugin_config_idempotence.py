#!/usr/bin/env python3
"""Tests that `monarch-init` writes (and restarts Jellyfin for) only a real change.

Both plugin config files init writes are shared with the plugin that owns them.
The LDAP plugin keeps its linked accounts in `<LdapUsers>` and rewrites the whole
file in its own shape, including an `encoding="utf-8"` XML declaration, as soon as
it has linked one. So comparing the file whole - which is what `previous != xml`
did - is true on every single run.

Measured on monarch 2026-10-06: every managed value byte-identical, and init
restarted Jellyfin anyway. That is ~40s of Jellyfin outage per run, and it is the
503 window `drift-check` used to read as *libraries missing* and *an app's stored
API key no longer authenticates*.

`plugin_config_changed()` compares the values init MANAGES (parsed, not byte-wise)
and `apply_plugin_config()` writes only when they moved - so a no-op run neither
touches a plugin's file nor restarts Jellyfin for it. The LDAP renderer carries
the plugin's `<LdapUsers>` across so those links survive a real write.

No network and no real appdata: `APPDATA` and the config values are pointed at a
temp directory for the length of each test.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent.parent / "init" / "init.py"
spec = importlib.util.spec_from_file_location("monarch_init", INIT)
assert spec and spec.loader
init_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_mod)

# The values the tests render with. None of them is a real credential.
LDAP_VALUES = {
    "LDAP_SERVER": "authentik-ldap",
    "LDAP_PORT": "3389",
    "LDAP_BIND_USER": "authentik-ldap",
    "LDAP_BIND_TOKEN": "test-bind-token",
    "LDAP_BIND_GROUP": "paid_users",
    "LDAP_ADMIN_GROUP": "jellyfin_admins",
    "LDAP_BASE_DN": "dc=test,dc=invalid",
    "LDAP_PUBLIC_URL": "https://auth.test.invalid",
}

OIDC_VALUES = {
    "MONARCH_SSO_CLIENT_ID": "monarch-media",
    "MONARCH_SSO_CLIENT_SECRET": "test-client-secret",
    "MONARCH_SSO_APP": "monarch-media",
    "MONARCH_SSO_AUTHENTIK_BASE": "https://auth.test.invalid",
    "MONARCH_SSO_SERVER_BASE_URL": "https://media.test.invalid",
}

LINKED_USER = (
    "<LdapUsers>\n"
    "      <LdapUser>\n"
    "        <LinkedJellyfinUserId>linked-user-id</LinkedJellyfinUserId>\n"
    "        <LdapUid>dhunter</LdapUid>\n"
    "        <ProfileImageHash />\n"
    "      </LdapUser>\n"
    "    </LdapUsers>"
)


class ThePluginConfigs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        values = {"APPDATA": init_mod.APPDATA, **LDAP_VALUES, **OIDC_VALUES}
        self.saved = {name: getattr(init_mod, name) for name in values}
        init_mod.APPDATA = self.tmp.name
        for name, value in values.items():
            if name != "APPDATA":
                setattr(init_mod, name, value)
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.saved.items():
            setattr(init_mod, name, value)

    @property
    def ldap_path(self) -> Path:
        return Path(init_mod.ldap_plugin_config_path())

    @property
    def oidc_path(self) -> Path:
        return Path(init_mod.oidc_plugin_config_path())

    def ldap_render(self) -> str:
        return init_mod.render_ldap_plugin_config(init_mod.read_plugin_config(
            str(self.ldap_path)))

    # ── the comparison ────────────────────────────────────────────────────

    def test_a_plugin_rewritten_ldap_file_is_not_a_change(self) -> None:
        template = self.ldap_render()
        on_disk = template.replace("<LdapUsers />", LINKED_USER, 1)
        self.assertNotEqual(on_disk, template,
                            "the fixture must differ byte-wise, or it proves nothing")
        self.assertFalse(
            init_mod.plugin_config_changed(on_disk, template),
            "a plugin-owned element was read as drift")

    def test_a_moved_managed_value_is_still_a_change(self) -> None:
        # The whole point of the comparison: a rotated bind token must still be
        # noticed, or Jellyfin keeps serving the old one out of memory.
        template = self.ldap_render()
        moved = template.replace(LDAP_VALUES["LDAP_BIND_TOKEN"], "a-new-bind-token")
        self.assertTrue(init_mod.plugin_config_changed(template, moved))

    def test_the_comparison_ignores_the_xml_declaration(self) -> None:
        template = self.ldap_render()
        without = re.sub(r'\s+encoding="utf-8"', "", template, count=1)
        self.assertNotEqual(without, template)
        self.assertFalse(init_mod.plugin_config_changed(without, template))

    def test_a_missing_file_counts_as_a_change(self) -> None:
        self.assertTrue(init_mod.plugin_config_changed(None, self.ldap_render()))

    def test_an_unparseable_file_counts_as_a_change(self) -> None:
        self.assertTrue(
            init_mod.plugin_config_changed("<not xml at all", self.ldap_render()))

    # ── writing only when it moved ────────────────────────────────────────

    def test_apply_writes_when_the_file_is_missing_and_says_so_after(self) -> None:
        xml = self.ldap_render()
        self.assertTrue(init_mod.apply_plugin_config(
            str(self.ldap_path), xml, None, "LDAP-Auth"))
        self.assertTrue(self.ldap_path.is_file())
        # Second run: the file now says exactly what this deployment sets.
        previous = init_mod.read_plugin_config(str(self.ldap_path))
        self.assertFalse(
            init_mod.apply_plugin_config(str(self.ldap_path), xml, previous,
                                         "LDAP-Auth"),
            "a no-op run rewrote a plugin config it should have left alone")

    def test_apply_does_not_touch_a_no_op_run(self) -> None:
        xml = self.ldap_render()
        init_mod.write_plugin_config(str(self.ldap_path), xml)
        # The plugin rewrites it in its own shape, which is the state a no-op run
        # must leave exactly as it found it.
        self.ldap_path.write_text(
            xml.replace("<LdapUsers />", LINKED_USER, 1), encoding="utf-8")
        before = self.ldap_path.read_text(encoding="utf-8")

        previous = init_mod.read_plugin_config(str(self.ldap_path))
        self.assertFalse(init_mod.apply_plugin_config(
            str(self.ldap_path), self.ldap_render(), previous, "LDAP-Auth"))
        self.assertEqual(self.ldap_path.read_text(encoding="utf-8"), before,
                         "the run rewrote a file it had just said was unchanged")

    def test_a_real_write_keeps_the_plugins_linked_users(self) -> None:
        init_mod.write_plugin_config(str(self.ldap_path),
                                     self.ldap_render())
        self.ldap_path.write_text(
            self.ldap_render().replace("<LdapUsers />", LINKED_USER, 1),
            encoding="utf-8")

        # A rotated bind token: a genuine change, so it writes.
        init_mod.LDAP_BIND_TOKEN = "a-new-bind-token"
        previous = init_mod.read_plugin_config(str(self.ldap_path))
        self.assertTrue(init_mod.apply_plugin_config(
            str(self.ldap_path), self.ldap_render(), previous, "LDAP-Auth"))
        written = self.ldap_path.read_text(encoding="utf-8")
        self.assertIn("a-new-bind-token", written)
        self.assertIn("<LdapUser>", written,
                      "writing over the plugin's links drops every linked account")

    # ── OIDC is judged the same way ───────────────────────────────────────

    def test_the_oidc_config_is_compared_the_same_way(self) -> None:
        template = init_mod.render_oidc_plugin_config()
        rewritten = template.replace('<?xml version="1.0" encoding="utf-8"?>',
                                     '<?xml version="1.0"?>', 1)
        self.assertNotEqual(rewritten, template)
        self.assertFalse(
            init_mod.plugin_config_changed(rewritten, template),
            "the OIDC config is compared as bytes, so a rewritten file restarts "
            "Jellyfin on every run too")
        moved = template.replace(OIDC_VALUES["MONARCH_SSO_CLIENT_SECRET"],
                                 "a-rotated-secret")
        self.assertTrue(init_mod.plugin_config_changed(template, moved))

    def test_apply_leaves_a_matching_oidc_config_alone(self) -> None:
        xml = init_mod.render_oidc_plugin_config()
        self.assertTrue(init_mod.apply_plugin_config(
            str(self.oidc_path), xml, None, "OIDC"))
        previous = init_mod.read_plugin_config(str(self.oidc_path))
        self.assertFalse(init_mod.apply_plugin_config(
            str(self.oidc_path), xml, previous, "OIDC"))


if __name__ == "__main__":
    unittest.main()
