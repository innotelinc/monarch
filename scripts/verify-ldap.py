#!/usr/bin/env python3
"""verify-ldap.py — does the Authentik LDAP outpost actually serve Jellyfin logins?

Jellyfin authenticates against Authentik through the `authentik-ldap` outpost
and the LDAP-Auth plugin. That path has two independent credentials and fails in
two different ways, neither of which Jellyfin reports usefully (the browser just
gets a 500 after a successful Authentik sign-in):

  1. the outpost's own API token — when it stops matching the key Authentik
     holds for the `jellyfin-ldap` outpost, the container logs
     `403 Forbidden (Token invalid/expired)` and never starts its LDAP listener;
  2. the bind credential AUTHENTIK_LDAP_BIND_TOKEN — when it drifts from the
     bind user's password in Authentik, the listener is up but every bind
     returns LDAP result code 49 (invalidCredentials).

This script asserts the whole path the plugin needs, on one connection: a simple
bind as the bind user, then the very search the plugin runs
(`memberOf=cn=<group>,ou=groups,<base>`). It uses only the standard library, so
it runs anywhere python3 does — including inside the Jellyfin container, which
already shares the outpost's compose network.

    python3 scripts/verify-ldap.py                 # resolve host via docker
    python3 scripts/verify-ldap.py --host 127.0.0.1 --port 3389

A bind that gets **no reply at all** is not a wrong credential. The outpost logs
`took-ms: 3316` for a bind against the Cerulean Authentik, and this script used to
wait 3 seconds — so a reply that was merely late was reported as "the bind token
has drifted", sending an operator to rotate a working credential while the real
state was "nothing answered in time". The wait is now far past the observed
latency, a silent attempt is retried once, and the two cases report differently:
a missing reply is *unreachable* (exit 1) and a result code is a *credential*
(exit 2).

Exit codes: 0 the login path works; 1 the outpost is unreachable (including "no
reply within the wait"); 2 the bind was refused; 3 the search failed or returned
nobody.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The outpost's own latency is the reason this is not 3 seconds. It logs
# `took-ms: 3316` for a bind against the Cerulean Authentik — a real reply that
# arrives *after* a 3-second wait, which read as "the bind token has drifted" and
# sent an operator to rotate a working credential. Wait well past the observed
# latency; this is a check, not a hot path.
DEFAULT_TIMEOUT_SECONDS = 15.0


# ── .env (comments stripped, because that is the bug this guards) ─────────────
def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            # An unquoted inline comment is stripped by Docker Compose but kept
            # by a naive reader — the exact drift that breaks this chain. Mirror
            # Compose: cut at ` #`, then trim.
            value = re.split(r"\s+#", value, maxsplit=1)[0]
            values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


# ── minimal LDAP over BER (no third-party client needed) ─────────────────────
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    return _tlv(0x02, n.to_bytes((n.bit_length() + 7) // 8 or 1, "big", signed=True))


def _octet(value: str | bytes) -> bytes:
    return _tlv(0x04, value.encode() if isinstance(value, str) else value)


def _bind_message(dn: str, password: str) -> bytes:
    return _tlv(0x30, _int(1) + _tlv(0x60, _int(3) + _octet(dn) + _tlv(0x80, password.encode())))


def _search_message(base: str, attribute: str, value: str) -> bytes:
    # equalityMatch [3] { attributeDescription, assertionValue }
    filt = _tlv(0xA3, _octet(attribute) + _octet(value))
    # baseObject, scope=2 (wholeSubtree), deref=0, size/time limits=0,
    # typesOnly=false, filter, attributes (REQUIRED, may be empty)
    body = _octet(base) + _int(2) + _int(0) + _int(0) + _int(0) + _tlv(0x01, b"\x00") + filt
    return _tlv(0x30, _int(2) + _tlv(0x63, body + _tlv(0x30, b"")))


def _iter_messages(stream: bytes):
    """Yield (protocolOp tag, protocolOp body) for each LDAPMessage in `stream`.

    Splitting on the outer SEQUENCE is what makes tag bytes inside payload data
    (an 'a' in a DN is 0x61, an 'e' is 0x65) harmless: the protocolOp tag is read
    from its real offset instead of being searched for as a byte.
    """
    index = 0
    while index + 2 <= len(stream) and stream[index] == 0x30:
        length = stream[index + 1]
        if length < 0x80:
            body_at, size = index + 2, length
        else:
            count = length & 0x7F
            if index + 2 + count > len(stream):
                return
            size = int.from_bytes(stream[index + 2:index + 2 + count], "big")
            body_at = index + 2 + count
        body = stream[body_at:body_at + size]
        index = body_at + size
        # LDAPMessage ::= SEQUENCE { messageID INTEGER, protocolOp CHOICE }
        if len(body) < 2 or body[0] != 0x02:
            continue
        op_at = 2 + body[1]
        if op_at + 1 >= len(body):
            continue
        op_tag = body[op_at]
        op_len = body[op_at + 1]
        if op_len < 0x80:
            op_body = body[op_at + 2:op_at + 2 + op_len]
        else:
            count = op_len & 0x7F
            size = int.from_bytes(body[op_at + 2:op_at + 2 + count], "big")
            op_body = body[op_at + 2 + count:op_at + 2 + count + size]
        yield op_tag, op_body


def _result_code(body: bytes) -> int | None:
    """The ENUMERATED resultCode that every LDAPResult begins with."""
    return body[2] if len(body) >= 3 and body[0] == 0x0A else None


def _collect(conn: socket.socket, idle: float, until_tag: int | None = None) -> bytes:
    """Read until the awaited message has arrived, the peer closes, or it goes idle.

    `until_tag` is the protocolOp that *ends* the exchange (0x61 for a bind
    response, 0x65 for searchResDone), and it is matched by parsing complete
    messages rather than by searching for a byte: payload data contains those
    bytes too (an 'a' in a DN is 0x61), and matching on the byte would stop a
    search mid-response.

    Waiting for the message rather than for the idle timer is what keeps the
    raised timeout cheap: a peer that answers in 3s and then holds the connection
    open is read in 3s, not in 3s plus the whole wait.
    """
    conn.settimeout(idle)
    data = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
        if until_tag is not None and any(tag == until_tag for tag, _ in _iter_messages(data)):
            break
    return data


# ── host discovery + failure hints ───────────────────────────────────────────
def _container_host() -> str | None:
    """The outpost container's address on the host, when docker can tell us."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "authentik-ldap", "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}\n{{end}}"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.strip():
            return line.strip()
    return None


def main(argv: list[str] | None = None) -> int:
    env = load_env(os.path.join(REPO_ROOT, ".env"))
    parser = argparse.ArgumentParser(description="Verify the Authentik LDAP outpost Jellyfin logs in through.")
    parser.add_argument("--host", default=None, help="outpost host (default: the container's address, else localhost)")
    parser.add_argument("--port", type=int, default=None, help="outpost port (default: AUTHENTIK_LDAP_PORT, 3389)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"idle time to wait for each reply, seconds "
                             f"(default {DEFAULT_TIMEOUT_SECONDS:g})")
    parser.add_argument("--attempts", type=int, default=2,
                        help="times to try when nothing answers at all (default 2): a "
                             "slow IdP is not a wrong credential")
    args = parser.parse_args(argv)

    server = setting(env, "AUTHENTIK_LDAP_SERVER", "authentik-ldap")
    port = args.port or int(setting(env, "AUTHENTIK_LDAP_PORT", "3389"))
    base_dn = setting(env, "AUTHENTIK_LDAP_BASE_DN", "dc=innotel,dc=us")
    bind_user = setting(env, "AUTHENTIK_LDAP_BIND_USER", "authentik-ldap")
    bind_group = setting(env, "AUTHENTIK_LDAP_BIND_GROUP", "paid_users")
    bind_token = setting(env, "AUTHENTIK_LDAP_BIND_TOKEN")
    bind_dn = f"cn={bind_user},ou=users,{base_dn}"

    host = args.host
    if not host:
        # The outpost port is not published on the host; prefer the address
        # docker knows for the container, then the compose name, then loopback.
        for candidate in (_container_host(), server, "127.0.0.1"):
            if not candidate:
                continue
            try:
                socket.create_connection((candidate, port), timeout=2).close()
                host = candidate
                break
            except OSError:
                continue
    if not host:
        print(f"FAIL unreachable: nothing answers on {server}:{port} (or a container address).", file=sys.stderr)
        print("     fix: docker compose up -d --force-recreate authentik-ldap", file=sys.stderr)
        return 1

    if not bind_token:
        print("FAIL config: AUTHENTIK_LDAP_BIND_TOKEN is empty in .env.", file=sys.stderr)
        print("     fix: set it (no trailing comment) and re-run monarch-init.", file=sys.stderr)
        return 2

    print(f"Outpost {host}:{port}  bind {bind_dn}  group {bind_group}")

    # One connection for both operations: the outpost treats a search on an
    # unbound connection as anonymous ("Anonymous BindDN not allowed", code 50).
    #
    # Retried only when *nothing* came back. A silent attempt is the outpost being
    # slow or mid-restart, and one retry is what separates that from a check that
    # goes red on a 3-second reply; an actual result code (49) is answered on the
    # first attempt, because retrying a refused credential cannot change it.
    bind_code = None
    bind_elapsed = 0.0
    attempts = max(1, args.attempts)
    for attempt in range(1, attempts + 1):
        try:
            conn = socket.create_connection((host, port), timeout=5)
            started = time.monotonic()
            conn.sendall(_bind_message(bind_dn, bind_token))
            bind_reply = _collect(conn, args.timeout, until_tag=0x61)
            bind_elapsed = time.monotonic() - started
        except OSError as error:
            print(f"FAIL unreachable: {error}", file=sys.stderr)
            print("     fix: docker compose up -d --force-recreate authentik-ldap", file=sys.stderr)
            return 1

        bind_codes = [_result_code(body) for tag, body in _iter_messages(bind_reply)
                      if tag == 0x61]
        bind_code = bind_codes[0] if bind_codes else None
        if bind_code is not None:
            break
        conn.close()
        if attempt < attempts:
            print(f"  note no bind reply within {args.timeout:g}s — retrying "
                  f"({attempt}/{attempts - 1})")

    if bind_code is None:
        conn.close()
        print(f"FAIL unreachable: no bind reply within {args.timeout:g}s "
              f"({attempts} attempt(s)) — the outpost is up but not answering.",
              file=sys.stderr)
        print("     fix: nothing to rotate. A slow or restarting outpost answers late, not",
              file=sys.stderr)
        print("          wrongly; check `docker logs authentik-ldap` for the reply time,",
              file=sys.stderr)
        print("          then raise --timeout or re-run. A bind that is *refused* reports code 49.",
              file=sys.stderr)
        return 1
    if bind_code != 0:
        conn.close()
        print(f"FAIL bind: result code {bind_code} (0 = success; 49 = invalidCredentials) "
              f"after {bind_elapsed:.1f}s.", file=sys.stderr)
        print("     fix: the bind token has drifted from the bind user's password in Authentik.",
              file=sys.stderr)
        print(f"          set the password for '{bind_user}' to AUTHENTIK_LDAP_BIND_TOKEN, rewrite",
              file=sys.stderr)
        print("          Jellyfin's LDAP-Auth.xml, then: docker restart jellyfin", file=sys.stderr)
        return 2
    print(f"  ok bind: result code 0 ({bind_elapsed:.1f}s)")

    try:
        started = time.monotonic()
        conn.sendall(_search_message(base_dn, "memberOf", f"cn={bind_group},ou=groups,{base_dn}"))
        search_reply = _collect(conn, args.timeout, until_tag=0x65)
        search_elapsed = time.monotonic() - started
    except OSError as error:
        print(f"FAIL search: {error}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    messages = list(_iter_messages(search_reply))
    entries = sum(1 for tag, _ in messages if tag == 0x64)
    done = [body for tag, body in messages if tag == 0x65]
    done_code = _result_code(done[-1]) if done else None
    if done_code not in (0, None):
        print(f"FAIL search: result code {done_code}.", file=sys.stderr)
        if done_code == 50:
            print("     fix: the bind user needs the LDAP 'search full directory' grant for this", file=sys.stderr)
            print("          provider (monarch-init applies it; it warns when it cannot).", file=sys.stderr)
        return 3
    print(f"  ok search: {entries} entr{'y' if entries == 1 else 'ies'} in {bind_group} "
          f"({search_elapsed:.1f}s)")
    if entries == 0:
        print(f"FAIL search: nobody is in '{bind_group}' — LDAP works but no login would resolve.", file=sys.stderr)
        print("     fix: Magnate's Stripe webhook grants membership on checkout; check the",
              file=sys.stderr)
        print("          subscriber is active there, or add the user in Authentik.", file=sys.stderr)
        return 3

    print("PASS the Jellyfin LDAP login path works end to end.")
    print(json.dumps({"host": host, "port": port, "bindDn": bind_dn, "group": bind_group, "entries": entries}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
