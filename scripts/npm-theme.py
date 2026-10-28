#!/usr/bin/env python3
"""
npm-theme.py - put the Monarch theme into the *arr apps at the edge.

The media apps (Radarr, Sonarr, Lidarr, Whisparr, Prowlarr) ship no theming
hook, so the theme is injected where they are already proxied: each app's Nginx
Proxy Manager **proxy host** gains an `advanced_config` block that adds a
`<link>` to `arr-theme.css`, a crown favicon and the tab-title script (all
served from the public apex, `https://monarch.innotel.us/monarch/`).

Why the edge and not the images: nothing is rebuilt, the change is one field per
host, `--check` can prove it, and `--revert` puts every host back byte for byte.
The apps' own forwards, certificates and access lists are never touched.

The block is delimited by markers, so an `advanced_config` that already carries
something else (Bazarr's `client_max_body_size 0;`, say) keeps it verbatim; a
re-run replaces only the marked region.

Configuration comes from environment variables or the repo's .env:

  NPM_BASE_URL        NPM API base (default http://localhost:81)
  NPM_ADMIN_EMAIL     NPM login (required)
  NPM_ADMIN_PASSWORD  NPM password (required)
  MONARCH_THEME_BASE  where the theme assets are served
                      (default https://monarch.innotel.us/monarch)

Flags:  --check   read-only (default): fail when a listed host's block differs
        --apply   write the block (and remove it from hosts no longer listed)
        --revert  remove the block everywhere, leaving other config alone

Exit codes: 0 = as intended, 1 = drift or a failed write, 2 = cannot run.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# public name -> app label. The names the media apps moved to on 2026-10-11, plus
# the prowlarr alias the edge still serves.
THEMED = {
    "movies.innotel.us": "Radarr",
    "tv.innotel.us": "Sonarr",
    "mp3.innotel.us": "Lidarr",
    "xxx.innotel.us": "Whisparr",
    "index.innotel.us": "Prowlarr",
    "prowlarr.monarch.innotel.us": "Prowlarr",
}

BEGIN = "# >>> monarch-theme (managed by scripts/npm-theme.py)"
END = "# <<< monarch-theme"

# Fields NPM accepts back on a PUT. Read-only fields (id, created_on, …) are
# deliberately absent: NPM rejects unknown/immutable keys.
WRITABLE = (
    "domain_names", "forward_scheme", "forward_host", "forward_port",
    "access_list_id", "certificate_id", "ssl_forced", "caching_enabled",
    "block_exploits", "advanced_config", "meta", "allow_websocket_upgrade",
    "http2_support", "forward_scheme", "locations", "hsts_enabled",
    "hsts_subdomains", "enabled",
)


def theme_base() -> str:
    return os.environ.get("MONARCH_THEME_BASE", "https://monarch.innotel.us/monarch").rstrip("/")


def theme_block() -> str:
    """The advanced_config block: three sub_filters and nothing else."""
    base = theme_base()
    inject = (
        f'<link rel="stylesheet" href="{base}/arr-theme.css">'
        f'<link rel="icon" type="image/svg+xml" href="{base}/monarch-crown.svg">'
        f'<script src="{base}/arr-title.js" defer></script>'
    )
    return "\n".join([
        BEGIN,
        "# Injected by scripts/npm-theme.py. sub_filter needs the upstream to",
        '# answer uncompressed, which is what clearing Accept-Encoding does.',
        'proxy_set_header Accept-Encoding "";',
        "sub_filter_once off;",
        "sub_filter_types text/html;",
        f"sub_filter '</head>' '{inject}</head>';",
        END,
    ])


def strip_block(config: str) -> str:
    """Remove a previously written block, preserving everything else, in order."""
    if not config:
        return ""
    out, inside = [], False
    for line in config.splitlines():
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out).strip("\n")


def merge(config: str, block: str) -> str:
    """Append `block` to `config`, replacing any block already there."""
    stripped = strip_block(config)
    return f"{stripped}\n{block}\n" if stripped else f"{block}\n"


class Npm:
    def __init__(self, base, email, password, verbose=False):
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.token = ""
        self.verbose = verbose

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            return err.code, (err.read() or b"").decode("utf-8", "replace")[:400]

    def login(self):
        status, body = self._request("POST", "/api/tokens",
                                     {"identity": self.email, "secret": self.password})
        if status != 200 or not isinstance(body, dict) or not body.get("token"):
            raise SystemExit(f"npm-theme: login failed (HTTP {status}): {body}")
        self.token = body["token"]

    def hosts(self):
        status, body = self._request("GET", "/api/nginx/proxy-hosts")
        if status != 200 or not isinstance(body, list):
            raise SystemExit(f"npm-theme: cannot list proxy hosts (HTTP {status}): {body}")
        return body

    def update(self, host_id, config):
        status, body = self._request("PUT", f"/api/nginx/proxy-hosts/{host_id}",
                                     {"advanced_config": config})
        return status, body


def load_env(path):
    if not os.path.isfile(path):
        return
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def resolve(host, name):
    """True when `name` is one of the domains this proxy host answers."""
    return name in (host.get("domain_names") or [])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--check", action="store_true", help="report drift only (default)")
    parser.add_argument("--apply", action="store_true", help="write the block")
    parser.add_argument("--revert", action="store_true", help="remove the block everywhere")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.apply and args.revert:
        parser.error("--apply and --revert are mutually exclusive")

    load_env(os.path.join(REPO_ROOT, ".env"))
    base = os.environ.get("NPM_BASE_URL", "http://localhost:81")
    email = os.environ.get("NPM_ADMIN_EMAIL", "")
    password = os.environ.get("NPM_ADMIN_PASSWORD", "")
    if not email or not password:
        print("npm-theme: NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD are required", file=sys.stderr)
        return 2

    npm = Npm(base, email, password, args.verbose)
    npm.login()
    hosts = npm.hosts()
    block = theme_block()

    mode = "revert" if args.revert else ("apply" if args.apply else "check")
    print(f"npm-theme [{mode}] {base} · {len(THEMED)} app(s)")

    wanted = {}
    for name, label in THEMED.items():
        found = next((h for h in hosts if resolve(h, name)), None)
        if found is None:
            print(f"  {name:<32} (no proxy host — skipped)")
            continue
        wanted[found["id"]] = (name, label, found)

    drift = 0
    changed = 0
    for host_id, (name, label, host) in sorted(wanted.items(), key=lambda kv: kv[1][0]):
        current = host.get("advanced_config") or ""
        desired = "" if args.revert else merge(current, block)
        if current == desired:
            print(f"  {name:<32} {label:<10} ok")
            continue
        drift += 1
        if not (args.apply or args.revert):
            print(f"  {name:<32} {label:<10} DRIFT (run --apply)")
            continue
        status, body = npm.update(host_id, desired)
        if status not in (200, 201):
            print(f"  {name:<32} {label:<10} WRITE FAILED HTTP {status}: {str(body)[:160]}",
                  file=sys.stderr)
            return 1
        print(f"  {name:<32} {label:<10} {'reverted' if args.revert else 'themed'}")
        changed += 1

    # A host that still carries the block but is no longer listed (or a revert)
    # is drift too, in both directions.
    for host in hosts:
        if host["id"] in wanted:
            continue
        if (host.get("advanced_config") or "").find(BEGIN) >= 0:
            drift += 1
            print(f"  {'(unlisted)':<32} {'':<10} still themed -> {'reverting' if args.revert else 'run --revert'}")

    if mode == "check":
        print(f"\n{'CHECK OK: every listed host carries the Monarch theme' if not drift else f'CHECK FAILED: {drift} host(s) drifted'}")
        return 1 if drift else 0
    print(f"\n{mode} complete: {changed} host(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
