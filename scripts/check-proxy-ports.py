#!/usr/bin/env python3
"""
check-proxy-ports.py - the edge map and the compose publishes must agree.

scripts/npm-hosts.conf is the single source of truth for the NPM proxy hosts,
and every row forwards to this host at the port **docker-compose.yml
publishes** (docs/operations.md -> Subdomains & Nginx Proxy Manager). Nothing
enforced that: `tv` forwarded to 3001 while the IPTV guide published 3011, and
host :3001 belongs to the Zeus portal, so a Monarch subdomain served another
project's UI. `npm-proxy-hosts.py --check` could not catch it - it diffs the
live NPM against the same conf, so a wrong conf row matches perfectly.

This check is static and mode-independent:

  * docker-compose.yml is parsed for each service's published host ports and
    container ports (short and long port syntax, ${VAR:-default} resolved);
  * npm-hosts.conf is parsed by the deployer's OWN loader, so ${VAR:-default}
    resolves exactly as it would at deploy time (imported, not re-implemented);
  * a row fails when its port is neither a published host port nor the
    container port for its forward target.

Rows that name something outside docker-compose.yml - `authentik-server` runs
in the shared Cerulean stack - are reported as unverifiable, never as failures;
container-forward mode (NPM_FORWARD_HOST=container) is why the container port
is accepted as well as the published one.

Usage:

  python3 scripts/check-proxy-ports.py
  python3 scripts/check-proxy-ports.py --compose docker-compose.yml --hosts-conf scripts/npm-hosts.conf

Exit code: 0 = every row resolves to a real publish; 1 = at least one cannot
work, or the conf could not be parsed.
"""

import argparse
import importlib.util
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

SERVICE_RE = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$")
PORTS_RE = re.compile(r"^    ports:\s*$")
SERVICE_KEY_RE = re.compile(r"^    \S")
LONG_KEYS = {"target", "published", "host_ip", "protocol", "mode", "name",
             "app_protocol"}


def expand(value):
    """Resolve ${VAR} / ${VAR:-default} from the environment (as compose does)."""
    out, i = "", 0
    while True:
        start = value.find("${", i)
        if start < 0:
            return out + value[i:]
        end = value.find("}", start)
        if end < 0:
            return out + value[i:]
        out += value[i:start]
        name, _, default = value[start + 2:end].partition(":-")
        out += os.environ.get(name.strip(), "").strip() or default
        i = end + 1


def _clean(value):
    return expand(value.strip().strip('"').strip("'"))


def _port(value):
    """An int for a plain port, else None (ranges and odd specs are ignored)."""
    value = value.strip()
    return int(value) if value.isdigit() else None


def _add_short(entry, service):
    """`- 8080:80`, `- "127.0.0.1:8088:8088"`, `- ${VAR:-8082}:8080`, `- 8080/tcp`."""
    spec = _clean(entry).split("/", 1)[0]
    parts = spec.split(":")
    if len(parts) == 1:
        port = _port(parts[0])
        if port:
            service["container"].add(port)
        return
    published, container = _port(parts[-2]), _port(parts[-1])
    if published:
        service["published"].add(published)
    if container:
        service["container"].add(container)


def _add_long(text, service):
    key, _, value = text.partition(":")
    key = key.strip().lower()
    if key not in LONG_KEYS:
        return
    port = _port(_clean(value))
    if not port:
        return
    service["published" if key == "published" else "container"].add(port)


def compose_publishes(path):
    """service -> {"published": {...}, "container": {...}} from a compose file."""
    services = {}
    current = None
    in_ports = False
    for raw in open(path, "r", encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = SERVICE_RE.match(line)
        if header:
            current = header.group(1)
            services.setdefault(current, {"published": set(), "container": set()})
            in_ports = False
            continue
        if current is None:
            continue
        if PORTS_RE.match(line):
            in_ports = True
            continue
        if not in_ports:
            continue
        if SERVICE_KEY_RE.match(line):     # the next key under this service
            in_ports = False
            continue
        if stripped.startswith("- "):
            entry = stripped[2:].strip()
            key = entry.partition(":")[0].strip().lower()
            if key in LONG_KEYS:           # long syntax: `- target: 80`
                _add_long(entry, services[current])
            else:                          # short syntax
                _add_short(entry, services[current])
            continue
        _add_long(stripped, services[current])   # long-syntax continuation
    return services


def load_deployer():
    """Import npm-proxy-hosts.py so the conf parses exactly as it deploys."""
    spec = importlib.util.spec_from_file_location(
        "npm_proxy_hosts", os.path.join(SCRIPT_DIR, "npm-proxy-hosts.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compose", default=os.path.join(REPO_ROOT, "docker-compose.yml"),
                        help="compose file to read the publishes from")
    parser.add_argument("--hosts-conf", default=None,
                        help="subdomain map to check (default: the deployer's)")
    args = parser.parse_args()

    if not os.path.isfile(args.compose):
        print(f"FAIL compose file not found: {args.compose}", file=sys.stderr)
        return 2

    module = load_deployer()
    env_file = os.path.join(REPO_ROOT, ".env")
    if os.path.isfile(env_file):
        module.load_env(env_file)          # same precedence as a deploy run
    if args.hosts_conf:
        module.HOSTS_CONF = args.hosts_conf
    domain = os.environ.get("MONARCH_DOMAIN") or "monarch.innotel.us"

    try:
        hosts, hosts_src = module.load_hosts(domain)
    except SystemExit as exc:              # a port that did not resolve
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    publishes = compose_publishes(args.compose)

    print(f"Monarch edge map -> docker-compose publishes ({domain})")
    print(f"  map:      {hosts_src}")
    print(f"  compose:  {args.compose}")

    ok, problems, unverified = 0, [], []
    for host in hosts:
        service, port = host["forward"], host["port"]
        entry = publishes.get(service)
        if entry is None:
            unverified.append((host["domain"], service, port))
            continue
        if port in entry["published"] or port in entry["container"]:
            ok += 1
            continue
        published = ", ".join(str(p) for p in sorted(entry["published"])) or "none"
        container = ", ".join(str(p) for p in sorted(entry["container"])) or "none"
        problems.append(
            f"{host['domain']} -> {service}:{port} - docker-compose publishes "
            f"{published} and the container listens on {container} for {service}")

    for domain_name, service, port in unverified:
        print(f"  unverified: {domain_name} -> {service}:{port} is not a service "
              "in that compose file (external dependency - not checked)")
    if problems:
        print("")
        for problem in problems:
            print(f"  ERROR {problem}")
        print(f"\n  {len(problems)} row(s) cannot work; {ok} match, "
              f"{len(unverified)} unverified")
        return 1
    print(f"\n  OK: all {ok} row(s) forward to a published or container port"
          + (f" ({len(unverified)} external row(s) not checked)" if unverified else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
