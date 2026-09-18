#!/usr/bin/env python3
"""Unit tests for verify-ldap.py — the check behind Jellyfin's LDAP login path.

The script was written for one outage (the outpost's API token drifting, then the
bind credential drifting) and wired into `drift-check`. What these tests pin is
the part that decides what the operator is told to do:

  * result code **0** -> pass, and the search really found the group's members;
  * result code **49** -> the bind credential drifted (exit 2);
  * **no reply at all** -> *unreachable* (exit 1), retried once first.

That third case is not hypothetical. The outpost logs `took-ms: 3316` for a bind
against the Cerulean Authentik while this script waited 3 seconds, so a perfectly
good credential was reported as drifted — the fix it printed sent an operator to
rotate a working secret. A late reply is now waited for (15s default), retried
once when nothing comes back, and reported as "nothing answered" rather than as a
credential to change.

A fake outpost speaks just enough BER for the two operations, so the suite runs
anywhere — no Authentik, no container.
"""
from __future__ import annotations

import importlib.util
import io
import socket
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parents[1] / "verify-ldap.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_ldap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ldap = _load()


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag, len(value)]) + value


def ldap_message(message_id: int, body: bytes) -> bytes:
    return tlv(0x30, tlv(0x02, bytes([message_id])) + body)


def bind_response(code: int) -> bytes:
    """[APPLICATION 1] with LDAPResult{code, matchedDN, diagnosticMessage}."""
    body = tlv(0x0A, bytes([code])) + tlv(0x04, b"") + tlv(0x04, b"")
    return ldap_message(1, tlv(0x61, body))


def search_result(entries: int, code: int = 0) -> bytes:
    """`entries` searchResEntry responses, then a searchResDone."""
    out = b""
    for index in range(entries):
        attributes = tlv(0x30, tlv(0x04, b"memberOf") + tlv(0x31, tlv(0x04, b"cn=paid_users")))
        entry = tlv(0x04, f"uid=user{index},ou=users,dc=innotel,dc=us".encode()) + attributes
        out += ldap_message(2 + index, tlv(0x64, entry))
    done = tlv(0x0A, bytes([code])) + tlv(0x04, b"") + tlv(0x04, b"")
    return out + ldap_message(9, tlv(0x65, done))


class FakeOutpost:
    """One connection, one bind, one search — the outpost's actual behaviour."""

    def __init__(self, bind_code: int | None = 0, entries: int = 2,
                 reply_delay: float = 0.0, silent: bool = False):
        self.bind_code = bind_code
        self.entries = entries
        self.reply_delay = reply_delay
        self.silent = silent
        self.connections = 0
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.host, self.port = self.socket.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _addr = self.socket.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            conn.recv(65536)                       # the bind request
            if self.silent:
                time.sleep(30)                     # up, but never answering
                return
            if self.reply_delay:
                time.sleep(self.reply_delay)
            if self.bind_code is not None:
                conn.sendall(bind_response(self.bind_code))
            if self.bind_code not in (0,):
                return                             # a refused bind ends the exchange
            conn.recv(65536)                       # the search request
            conn.sendall(search_result(self.entries))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self.socket.close()


class VerifyLdapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text(
            "AUTHENTIK_LDAP_BASE_DN=dc=innotel,dc=us\n"
            "AUTHENTIK_LDAP_BIND_USER=authentik-ldap\n"
            "AUTHENTIK_LDAP_BIND_TOKEN=ak-ldap-bind-token\n"
            "AUTHENTIK_LDAP_BIND_GROUP=paid_users\n",
            encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        # main() reads REPO_ROOT/.env; point it at the fixture instead.
        self.repo_root = ldap.REPO_ROOT
        ldap.REPO_ROOT = self.tmp.name
        self.addCleanup(lambda: setattr(ldap, "REPO_ROOT", self.repo_root))

    def run_script(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ldap.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def start(self, **kwargs) -> FakeOutpost:
        outpost = FakeOutpost(**kwargs)
        self.addCleanup(outpost.stop)
        return outpost

    # ── the pass ───────────────────────────────────────────────────────────

    def test_a_working_outpost_passes_and_reports_the_group(self):
        outpost = self.start()
        code, out, _ = self.run_script("--host", outpost.host, "--port", str(outpost.port))
        self.assertEqual(code, 0, out)
        self.assertIn("ok bind: result code 0", out)
        self.assertIn("ok search: 2 entries in paid_users", out)
        self.assertIn("PASS", out)

    def test_an_empty_group_fails_the_search_not_the_bind(self):
        # Nobody in the gate group means no login resolves — LDAP itself is fine.
        outpost = self.start(entries=0)
        code, _, err = self.run_script("--host", outpost.host, "--port", str(outpost.port))
        self.assertEqual(code, 3)
        self.assertIn("nobody is in 'paid_users'", err)

    # ── the credential, refused ────────────────────────────────────────────

    def test_a_refused_bind_is_exit_2_and_names_the_credential(self):
        outpost = self.start(bind_code=49)
        code, _, err = self.run_script("--host", outpost.host, "--port", str(outpost.port))
        self.assertEqual(code, 2)
        self.assertIn("result code 49", err)
        self.assertIn("has drifted from the bind user's password", err)
        # Refused on the first attempt: retrying a code 49 cannot change it.
        self.assertEqual(outpost.connections, 1)

    # ── the reply that is merely late ──────────────────────────────────────

    def test_a_late_reply_is_waited_for_rather_than_called_a_credential(self):
        # 3.3s is what the outpost actually logs for a bind. The old 3s wait
        # reported this as a drifted token, and the printed fix rotated a
        # credential that was fine.
        outpost = self.start(reply_delay=1.5)
        code, out, err = self.run_script("--host", outpost.host, "--port", str(outpost.port),
                                         "--timeout", "10")
        self.assertEqual(code, 0, err)
        self.assertIn("ok bind: result code 0", out)

    def test_a_silent_outpost_is_unreachable_and_is_retried(self):
        outpost = self.start(silent=True)
        code, out, err = self.run_script("--host", outpost.host, "--port", str(outpost.port),
                                         "--timeout", "0.3")
        self.assertEqual(code, 1)
        self.assertIn("no bind reply", err)
        # Nothing to rotate, and it says so rather than naming the credential.
        self.assertIn("nothing to rotate", err)
        self.assertNotIn("has drifted", err)
        self.assertEqual(outpost.connections, 2)

    def test_the_attempt_count_is_configurable(self):
        outpost = self.start(silent=True)
        code, _, _ = self.run_script("--host", outpost.host, "--port", str(outpost.port),
                                     "--timeout", "0.3", "--attempts", "1")
        self.assertEqual(code, 1)
        self.assertEqual(outpost.connections, 1)

    def test_nothing_listening_is_unreachable(self):
        # A port that was closed a moment ago: the connection itself fails.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
        probe.close()
        code, _, err = self.run_script("--host", host, "--port", str(port))
        self.assertIn(code, (1, 2))
        self.assertIn("unreachable", err)

    def test_the_default_wait_is_far_past_the_measured_latency(self):
        # The number that caused this: 3.0s against an outpost that logs 3316ms.
        self.assertGreaterEqual(ldap.DEFAULT_TIMEOUT_SECONDS, 10.0)

    def test_an_empty_bind_token_is_a_config_error_not_a_bind_attempt(self):
        self.env.write_text("AUTHENTIK_LDAP_BIND_TOKEN=\n", encoding="utf-8")
        outpost = self.start()
        code, _, err = self.run_script("--host", outpost.host, "--port", str(outpost.port))
        self.assertEqual(code, 2)
        self.assertIn("AUTHENTIK_LDAP_BIND_TOKEN is empty", err)
        self.assertEqual(outpost.connections, 0)


if __name__ == "__main__":
    unittest.main()
