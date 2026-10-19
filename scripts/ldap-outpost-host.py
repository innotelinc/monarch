#!/usr/bin/env python3
"""ldap-outpost-host.py — keep the LDAP outpost on a hostname Authentik's cookie can reach.

The outpost runs a bind as a short flow: GET the identification challenge, POST
the bind DN and password. Authentik keeps that flow in an `authentik_session`
cookie, and the cookie is scoped to the server's public cookie domain (this
deployment uses `.innotel.us`). Browsers do not send a cookie for `.innotel.us`
to `192.168.1.71`, and neither does the outpost: the second request arrives
without the flow session, Authentik restarts the flow, and the outpost loops
until it logs `exceeded stage recursion depth`. The LDAP client only sees
result code 49 (invalidCredentials), which reads like a drifted bind token even
when the password is perfect.

The outpost therefore has to reach Authentik on a *name* under that cookie
domain, pinned to the server's LAN address with an `extra_hosts` entry. The
compose file defaults to `auth.cerulean.innotel.us`; this script is the check
and the repair for a host whose `.env` (or an old checkout) still points the
outpost at the bare IP.

    python3 scripts/ldap-outpost-host.py --check   # 0 compatible, 1 not
    python3 scripts/ldap-outpost-host.py --fix     # write .env, recreate, verify

`--fix` is idempotent: on a compatible host it changes nothing. It writes only
the three AUTHENTIK_LDAP_AUTHORITY* keys, keeps a timestamped .env backup, and
then runs the same `verify-ldap.py` the drift check does.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.environ.get("MONARCH_ENV") or os.path.join(REPO_ROOT, ".env")
COMPOSE_FILE = os.path.join(REPO_ROOT, "docker-compose.yml")
DEFAULT_AUTHORITY = "auth.cerulean.innotel.us"
DEFAULT_PORT = "9000"
DEFAULT_IP = "192.168.1.71"


def load_env(path: str) -> dict[str, str]:
    """Read a compose-style .env, stripping inline comments the way Compose does."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = re.split(r"\s+#", value, maxsplit=1)[0]
            values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


def host_from_url(value: str) -> str:
    """Hostname from a URL (or a bare host[:port]); lower-case, no trailing dot."""
    parsed = urllib.parse.urlsplit(value if "//" in value else f"//{value}")
    return (parsed.hostname or "").strip().lower().rstrip(".")


def port_from_url(value: str, default: str = DEFAULT_PORT) -> str:
    parsed = urllib.parse.urlsplit(value if "//" in value else f"//{value}")
    return str(parsed.port or default)


def host_matches_cookie_domain(host: str, domain: str) -> bool:
    """Whether a request to `host` receives a cookie scoped to `domain`."""
    host = host.strip().lower().rstrip(".")
    domain = domain.strip().lower().lstrip(".").rstrip(".")
    if not domain:
        return True                          # host-only cookie
    return host == domain or host.endswith("." + domain)


def session_cookie_domain(base_url: str, flow_slug: str,
                          timeout: float = 5.0) -> str | None:
    """The Domain= Authentik scopes `authentik_session` to, or None if unknown."""
    url = f"{base_url.rstrip('/')}/api/v3/flows/executor/{flow_slug}/?query="
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        headers = error.headers.get_all("Set-Cookie") if error.headers else []
    except (OSError, ValueError):
        return None
    for header in headers:
        name, _, rest = header.partition("=")
        if name.strip().lower() != "authentik_session":
            continue
        for attribute in rest.split(";")[1:]:
            key, _, value = attribute.strip().partition("=")
            if key.lower() == "domain":
                return value.strip().strip('"').lstrip(".")
        return ""                            # host-only cookie
    return None


def container_env(name: str, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", name, "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        env_name, _, value = line.partition("=")
        if env_name == key:
            return value.strip()
    return None


def desired(env: dict[str, str], cookie_domain: str,
            outpost_host: str | None) -> tuple[str, str, str]:
    """Pick the authority, its LAN address, and the port; raise when impossible."""
    candidates = [
        setting(env, "AUTHENTIK_LDAP_AUTHORITY"),
        host_from_url(setting(env, "MONARCH_SSO_AUTHENTIK_BASE")),
        host_from_url(setting(env, "AUTHENTIK_PUBLIC_URL")),
        DEFAULT_AUTHORITY,
    ]
    authority = ""
    for candidate in candidates:
        candidate = (candidate or "").strip().lower().rstrip(".")
        if candidate and host_matches_cookie_domain(candidate, cookie_domain):
            authority = candidate
            break
    if not authority:
        # A host under the domain is enough for the cookie; the extra_hosts entry
        # makes DNS irrelevant, and the server's own base URL is the last resort.
        authority = host_from_url(setting(env, "AUTHENTIK_BASE_URL"))
        if not host_matches_cookie_domain(authority, cookie_domain):
            raise ValueError(
                f"no host under Authentik's cookie domain {'.' + cookie_domain if cookie_domain else '(host-only)'} "
                "is configured; set AUTHENTIK_LDAP_AUTHORITY to a name the server "
                "answers on (e.g. auth.cerulean.innotel.us)")

    address = (setting(env, "AUTHENTIK_LDAP_AUTHORITY_IP")
               or setting(env, "AUTHENTIK_UPSTREAM_HOST")
               or host_from_url(setting(env, "AUTHENTIK_BASE_URL"))
               or DEFAULT_IP)
    port = (setting(env, "AUTHENTIK_LDAP_AUTHORITY_PORT")
            or port_from_url(setting(env, "AUTHENTIK_BASE_URL"), DEFAULT_PORT))
    return authority, address, port


def write_env(path: str, env: dict[str, str], authority: str,
              address: str, port: str) -> bool:
    """Write the three keys, keeping a timestamped backup. True when it changed."""
    wanted = {
        "AUTHENTIK_LDAP_AUTHORITY": authority,
        "AUTHENTIK_LDAP_AUTHORITY_IP": address,
        "AUTHENTIK_LDAP_AUTHORITY_PORT": port,
    }
    if all(env.get(key) == value for key, value in wanted.items()):
        return False

    lines: list[str] = []
    seen: set[str] = set()
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    for raw in existing.splitlines():
        key = raw.split("=", 1)[0].strip() if "=" in raw and not raw.lstrip().startswith("#") else ""
        if key in wanted:
            lines.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            lines.append(raw)
    missing = [key for key in wanted if key not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# The LDAP outpost must talk to Authentik on a name inside its")
        lines.append("# session-cookie domain; the name is pinned to the LAN address.")
        for key in missing:
            lines.append(f"{key}={wanted[key]}")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{path}.bak-ldap-host-{stamp}"
    if os.path.isfile(path):
        try:
            # Copy, not move: a failed write must leave the old .env in place.
            with open(path, "rb") as source, open(backup, "wb") as target:
                target.write(source.read())
            os.chmod(backup, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    if os.path.isfile(path):
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
    os.replace(tmp, path)
    return True


def compose_up() -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d",
             "--force-recreate", "authentik-ldap"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return result.returncode, (result.stdout + result.stderr).strip()


def wait_for_outpost(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["docker", "inspect", "authentik-ldap", "--format",
                 "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"],
                capture_output=True, text=True, timeout=10,
            )
            status = result.stdout.strip()
            if status in ("healthy", "running"):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep the Authentik LDAP outpost on Authentik's session-cookie domain.")
    parser.add_argument("--check", action="store_true",
                        help="report whether the outpost host can hold the flow cookie (default)")
    parser.add_argument("--fix", action="store_true",
                        help="write AUTHENTIK_LDAP_AUTHORITY* to .env and recreate the outpost")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --fix, say what would change without touching .env or docker")
    parser.add_argument("--no-verify", action="store_true",
                        help="with --fix, do not run verify-ldap.py afterwards")
    parser.add_argument("--cookie-domain", default=None,
                        help="override the domain Authentik scopes authentik_session to (tests)")
    parser.add_argument("--outpost-host", default=None,
                        help="override the outpost's AUTHENTIK_HOST (tests)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for the flow challenge (default 5)")
    args = parser.parse_args(argv)

    env = load_env(ENV_FILE)
    base_url = setting(env, "AUTHENTIK_BASE_URL")
    flow = setting(env, "AUTHENTIK_LDAP_FLOW", "default-authentication-flow")
    domain = args.cookie_domain
    if domain is None:
        domain = session_cookie_domain(base_url, flow, timeout=args.timeout) if base_url else None
    if domain is None:
        print("ok: Authentik is not reachable; the outpost host was not judged "
              "(the bind check reports the reachability failure)")
        return 0

    outpost_auth = (args.outpost_host
                    or os.environ.get("AUTHENTIK_HOST")
                    or container_env("authentik-ldap", "AUTHENTIK_HOST")
                    or f"http://{setting(env, 'AUTHENTIK_LDAP_AUTHORITY', DEFAULT_AUTHORITY)}:"
                       f"{setting(env, 'AUTHENTIK_LDAP_AUTHORITY_PORT', DEFAULT_PORT)}")
    outpost_host = host_from_url(outpost_auth)
    if not outpost_host:
        print("ok: the outpost's AUTHENTIK_HOST is not readable from here; not judged")
        return 0

    matches = host_matches_cookie_domain(outpost_host, domain)
    label = f".{domain}" if domain else "a host-only cookie"
    if matches:
        print(f"ok: authentik-ldap reaches Authentik as {outpost_host}, "
              f"inside its session-cookie domain {label}")
        return 0
    print(f"FAIL: authentik-ldap reaches Authentik as {outpost_host}, but Authentik "
          f"scopes its session cookie to {label} - the bind flow restarts and every "
          f"bind returns LDAP 49 regardless of the password", file=sys.stderr)
    if not args.fix:
        print("     fix: python3 scripts/ldap-outpost-host.py --fix", file=sys.stderr)
        return 1

    try:
        authority, address, port = desired(env, domain, outpost_host)
    except ValueError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"drift-check: pointing the LDAP outpost at http://{authority}:{port} "
          f"(pinned to {address}) so it can hold Authentik's {label} cookie")
    if args.dry_run:
        print("dry-run: would write AUTHENTIK_LDAP_AUTHORITY* and recreate authentik-ldap")
        return 0
    try:
        changed = write_env(ENV_FILE, env, authority, address, port)
    except OSError as error:
        print(f"FAIL: could not update {ENV_FILE}: {error}", file=sys.stderr)
        return 1
    print(f"drift-check: {ENV_FILE} {'updated' if changed else 'already had the right authority'}")
    code, output = compose_up()
    if code != 0:
        print(f"FAIL: 'docker compose up -d --force-recreate authentik-ldap' failed:", file=sys.stderr)
        print(output, file=sys.stderr)
        return 1
    if not wait_for_outpost():
        print("FAIL: authentik-ldap did not become healthy after the recreate", file=sys.stderr)
        return 1
    if args.no_verify:
        print("ok: authentik-ldap recreated")
        return 0
    verify = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify-ldap.py")
    result = subprocess.run([sys.executable, verify], cwd=REPO_ROOT)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
