#!/usr/bin/env python3
"""Tests that `monarch-init` only rewrites (and restarts Jellyfin for) a real change.

The LDAP-Auth plugin config file is shared. init writes it from the deployment's
own values, and the plugin then owns part of it: `<LdapUsers>`, the Jellyfin
accounts it has linked to LDAP identities. The plugin also rewrites the file in
its own shape, including an `encoding="utf-8"` XML declaration.

So comparing the file whole - which is what `needs_restart = previous != xml` did -
is true on every single run. Measured on monarch on 2026-10-06: every managed
value identical, and init restarted Jellyfin anyway. That is ~40s of Jellyfin
outage per run, and it is the 503 window `drift-check` used to read as *libraries
missing* and *an app's stored API key no longer authenticates*.

`ldap_config_changed()` compares the values init MANAGES (parsed, not byte-wise),
and `write_ldap_plugin_config()` carries the plugin's `<LdapUsers>` across so the
links survive and the file converges.

No network and no real appdata: `APPDATA` and the LDAP_* values are pointed at a
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

# The values the tests write the config with. None of them is a real credential.
TEST_VALUES = {
    "LDAP_SERVER": "authentik-ldap",
    "LDAP_PORT": "3389",
    "LDAP_BIND_USER": "authentik-ldap",
    "LDAP_BIND_TOKEN": "test-bind-token",
    "LDAP_BIND_GROUP": "paid_users",
    "LDAP_ADMIN_GROUP": "jellyfin_admins",
    "LDAP_BASE_DN": "dc=test,dc=invalid",
    "LDAP_PUBLIC_URL": "https://auth.test.invalid",
}



class TheLdapConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {"APPDATA": init_mod.APPDATA}
        for name in TEST_VALUES:
            self.saved[name] = getattr(init_mod, name)
        init_mod.APPDATA = self.tmp.name
        for name, value in TEST_VALUES.items():
            setattr(init_mod, name, value)
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.saved.items():
            setattr(init_mod, name, value)

    @property
    def path(self) -> Path:
        return Path(init_mod.ldap_plugin_config_path())

    def write(self) -> str:
        """Write the config the way init does, and return what it wrote."""
        _, xml = init_mod.write_ldap_plugin_config()
        return xml

    def plugin_rewrite(self, xml: str, users: str) -> str:
        """The file as the plugin leaves it: a link record inside `<LdapUsers>`.

        This is the shape measured on monarch - every managed value untouched, a
        `<LdapUser>` record added.
        """
        assert "<LdapUsers />" in xml, "the template no longer writes the element"
        return xml.replace("<LdapUsers />", users, 1)

    def test_a_plugin_rewritten_file_is_not_a_change(self) -> None:
        template = self.write()
        self.path.write_text(
            self.plugin_rewrite(template, LINKED_USER), encoding="utf-8")
        on_disk = self.path.read_text(encoding="utf-8")

        self.assertNotEqual(on_disk, template,
                            "the fixture must differ byte-wise, or it proves nothing")
        self.assertFalse(
            init_mod.ldap_config_changed(on_disk, template),
            "a plugin-owned element and the declaration were read as drift")

    def test_a_moved_managed_value_is_still_a_change(self) -> None:
        # The whole point of the comparison: a rotated bind token must still be
        # noticed, or Jellyfin keeps serving the old one out of memory.
        template = self.write()
        on_disk = self.plugin_rewrite(template, LINKED_USER)
        moved = on_disk.replace(TEST_VALUES["LDAP_BIND_TOKEN"], "a-new-bind-token")
        self.assertTrue(init_mod.ldap_config_changed(on_disk, moved))

    def test_the_plugins_linked_users_survive_a_rewrite(self) -> None:
        self.write()
        self.path.write_text(
            self.plugin_rewrite(self.write(), LINKED_USER), encoding="utf-8")
        rewritten = self.write()
        self.assertIn("<LdapUser>", rewritten,
                      "writing over the plugin's links drops every linked account")
        self.assertIn("linked-user-id", rewritten)

    def test_an_unparseable_file_counts_as_a_change(self) -> None:
        template = self.write()
        self.assertTrue(init_mod.ldap_config_changed("<not xml at all", template))

    def test_a_missing_file_counts_as_a_change(self) -> None:
        self.assertTrue(init_mod.ldap_config_changed(None, self.write()))

    def test_the_comparison_ignores_the_xml_declaration(self) -> None:
        template = self.write()
        without = re.sub(r'\s+encoding="utf-8"', "", template, count=1)
        self.assertNotEqual(without, template)
        self.assertFalse(init_mod.ldap_config_changed(without, template))


LINKED_USER = (
    "<LdapUsers>\n"
    "      <LdapUser>\n"
    "        <LinkedJellyfinUserId>linked-user-id</LinkedJellyfinUserId>\n"
    "        <LdapUid>dhunter</LdapUid>\n"
    "        <ProfileImageHash />\n"
    "      </LdapUser>\n"
    "    </LdapUsers>"
)


if __name__ == "__main__":
    unittest.main()
