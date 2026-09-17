#!/usr/bin/env python3
"""vault-env.py — resolve `vault://` references in an env file at container start.

The platform's secret store is **Cerulean Vault** (HashiCorp Vault, KV v2), so a
stack's `.env` may hold either plain values or `vault://<mount>/<path>#<key>`
references — the same grammar Cerulean, Onyx, Atlas, Distro and Zeus resolve.
Zeus resolves in TypeScript (`scripts/vault-env.mjs`); most of the estate is
Python, so this is the same contract as one stdlib-only script a compose
`entrypoint` can call before the real process starts:

    VAULT_TOKEN_FILE=./data/vault/token/monarch.token \\
        python3 scripts/vault-env.py .env | logger -t vault-env

    # or, writing the resolved file compose's `env_file` then reads:
    python3 scripts/vault-env.py --out /run/secrets/.env.resolved .env

Resolution rules (identical to Zeus's resolver):
  * `KEY=vault://<mount>/<path>#<key>`  → the value is fetched and the line is
    rewritten in place to `KEY=<resolved>`.
  * `KEY=plain` is left untouched.
  * A commented or malformed line is left untouched.
  * A leftover `infisical://` value is a hard error: Infisical is retired, and
    a stale reference must be moved with `scripts/vault-migrate.py`, not passed
    through as a credential that looks configured and is not.
  * Any reference that cannot be resolved exits non-zero **before** the real
    service starts — a stack configured with references but no reachable Vault
    must fail fast, not boot with literal `vault://` strings.

Environment contract (the Vault CLI's own order):
  VAULT_ADDR           base URL, e.g. http://192.168.1.46:8200
  VAULT_TOKEN          this stack's path-scoped token, or
  VAULT_TOKEN_FILE     a file holding it
  VAULT_NAMESPACE      Enterprise namespaces; unused on OSS Vault
  VAULT_SKIP_VERIFY    "1" to accept a self-signed certificate
  VAULT_CACERT         CA bundle for TLS

This file is mirrored verbatim into every member repo (see scripts/mesh.sh for
the mirror convention). Cerulean/scripts is canonical — it is the TrustOps
platform's own resolver and the store it talks to is its own.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

REF_PREFIX = "vault://"
RETIRED_PREFIXES = ("infisical://",)

# What the .env grammar accepts on the right-hand side: the reference is the
# whole value (no interpolation inside a value — compose does that layer, and
# mixing the two makes "what is the secret" unanswerable).
def parse_reference(value: str) -> tuple[str, str, str] | None:
    """Return (mount, path, key) for a `vault://` value, else None."""
    raw = value.strip()
    for retired in RETIRED_PREFIXES:
        if raw.startswith(retired):
            raise SystemExit(
                f"vault-env: refused {retired}... reference — Infisical is retired; "
                "move the value with scripts/vault-migrate.py"
            )
    if not raw.startswith(REF_PREFIX):
        return None
    rest = raw[len(REF_PREFIX):]
    if "#" not in rest:
        raise SystemExit(f"vault-env: malformed reference (no #key): {raw[:60]}...")
    mount_path, key = rest.rsplit("#", 1)
    if not mount_path or not key:
        raise SystemExit(f"vault-env: malformed reference: {raw[:60]}...")
    mount, _, path = mount_path.partition("/")
    if not mount or not path:
        raise SystemExit(f"vault-env: malformed reference (want mount/path#key): {raw[:60]}...")
    return mount, path, key


def vault_env() -> tuple[str, str, ssl.SSLContext | None]:
    addr = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    if not addr:
        raise SystemExit("vault-env: VAULT_ADDR is not set — no store to resolve against")
    token = (os.environ.get("VAULT_TOKEN") or "").strip()
    token_file = (os.environ.get("VAULT_TOKEN_FILE") or "").strip()
    if not token and token_file:
        try:
            token = open(token_file, encoding="utf-8").read().strip()
        except OSError as exc:
            raise SystemExit(f"vault-env: cannot read VAULT_TOKEN_FILE ({token_file}): {exc}") from exc
    if not token:
        raise SystemExit("vault-env: neither VAULT_TOKEN nor VAULT_TOKEN_FILE is set")

    ctx: ssl.SSLContext | None = None
    if addr.startswith("https://"):
        ctx = ssl.create_default_context()
        if (os.environ.get("VAULT_SKIP_VERIFY") or "").strip() == "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        cacert = (os.environ.get("VAULT_CACERT") or "").strip()
        if cacert:
            ctx.load_verify_locations(cacert)
    return addr, token, ctx


def read_secret(addr: str, token: str, ctx: ssl.SSLContext | None,
                mount: str, path: str, key: str) -> str:
    url = f"{addr}/v1/{mount}/data/{path}"
    req = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:120]
        raise SystemExit(f"vault-env: Vault returned HTTP {exc.code} for {mount}/{path}#{key}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"vault-env: cannot reach Vault at {addr}: {exc.reason}") from exc

    data = (payload.get("data") or {}).get("data") or {}
    if key not in data:
        # A KV v2 write that predates the key, or a wrong key name — say which,
        # without echoing the other keys' values.
        names = ", ".join(sorted(data)) or "(empty)"
        raise SystemExit(f"vault-env: key '{key}' not found at {mount}/{path} (available: {names})")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise SystemExit(f"vault-env: key '{key}' at {mount}/{path} is empty or not a string")
    return value


def render(value: str) -> str:
    """Quote for an env-file right-hand side (single-quote, double the inner)."""
    return "'" + value.replace("'", "''") + "'"


def resolve_file(env_path: str, addr: str, token: str, ctx: ssl.SSLContext | None,
                 strict: bool) -> tuple[list[str], int]:
    """Return (output lines, count of references resolved)."""
    out: list[str] = []
    resolved = 0
    with open(env_path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            # Only a `KEY=VALUE` line can carry a reference; comments, section
            # headers and blank lines pass through untouched.
            if stripped and not stripped.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                try:
                    ref = parse_reference(value)
                except SystemExit as exc:
                    if strict:
                        raise
                    print(f"vault-env: {env_path}:{lineno}: {exc}", file=sys.stderr)
                    ref = None
                if ref is not None:
                    mount, path, secret_key = ref
                    secret = read_secret(addr, token, ctx, mount, path, secret_key)
                    out.append(f"{key}={render(secret)}")
                    resolved += 1
                    continue
            out.append(line)
    return out, resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("env_file", help="the .env file to read (never modified)")
    parser.add_argument("--out", help="write the resolved env file here (default: stdout)")
    parser.add_argument("--strict", action="store_true", default=True,
                        help="abort on a malformed reference (default)")
    parser.add_argument("--lenient", dest="strict", action="store_false",
                        help="keep a malformed reference and warn instead of aborting")
    args = parser.parse_args()

    if not os.path.isfile(args.env_file):
        raise SystemExit(f"vault-env: no such env file: {args.env_file}")

    addr, token, ctx = vault_env()
    lines, resolved = resolve_file(args.env_file, addr, token, ctx, args.strict)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.chmod(args.out, 0o600)
    else:
        sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))

    print(f"vault-env: {resolved} reference(s) resolved from {addr}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
