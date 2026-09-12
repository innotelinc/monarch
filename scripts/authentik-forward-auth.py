#!/usr/bin/env python3
"""
authentik-forward-auth.py - provision the Monarch SSO forward-auth provider.

The media-stack admin apps (Radarr, Sonarr, Lidarr, Whisparr, Bazarr,
Prowlarr, qBittorrent, Sabnzbd) do not speak OIDC - they only have a local
username/password form. The way to put **Cerulean Authentik** in front of
their login is nginx `auth_request`: Nginx Proxy Manager asks the Authentik
embedded outpost whether the request carries a valid SSO session, and only
proxies the app when it does. The Jellyfin stack's Jellyseerr is gated the
same way.

This script creates the Authentik side of that contract:

  1. a domain-level proxy provider ("Monarch NPM Forward Auth",
     mode=forward_domain, cookie_domain=<MONARCH_DOMAIN>) so ONE provider
     covers every <app>.<MONARCH_DOMAIN> host;
  2. the matching Application (slug monarch-npm-forward-auth);
  3. attaches the provider to the Authentik **embedded outpost** so the
     outpost serves /outpost.goauthentik.io/api/v3/... for it.

The nginx half lives in scripts/npm-proxy-hosts.py (the `fa` column of
scripts/npm-hosts.conf injects the auth_request snippet into each proxy host).

Idempotent - safe to re-run; existing provider/application are reused and only
updated when a field actually differs.

Environment (.env is read when present):
  AUTHENTIK_BASE_URL          e.g. http://192.168.1.46:9000 (Cerulean Authentik)
  AUTHENTIK_BOOTSTRAP_TOKEN   Authentik API token. One is minted at
                              /docker/appdata/init/ on first boot by monarch-init.
  MONARCH_DOMAIN              base domain, default monarch.innotel.us
  AUTHENTIK_FORWARD_PROVIDER  provider name, default "Monarch NPM Forward Auth"
  AUTHENTIK_FORWARD_APP_SLUG  application slug, default monarch-npm-forward-auth
  AUTHENTIK_FORWARD_AUTH_FLOW authorization flow slug, default
                              default-provider-authorization-implicit-consent
  AUTHENTIK_FORWARD_INVALIDATION_FLOW invalidation flow slug, default
                              default-invalidation-flow
  AUTHENTIK_FORWARD_GROUP     optional group that must be a member to pass the
                              gate ('' = any authenticated Authentik user)

Usage:
  python3 scripts/authentik-forward-auth.py            # create/update
  python3 scripts/authentik-forward-auth.py --check    # verify only, exit 1 on drift
  python3 scripts/authentik-forward-auth.py --dry-run  # print the plan
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ (never overwrite)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Drop a trailing inline comment (" # ..."), unless it is inside quotes.
        if not value[:1] in ('"', "'"):
            value = value.split(" #", 1)[0].rstrip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class ApiError(Exception):
    pass


class Ak:
    """Minimal Authentik API client (stdlib only)."""

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def _call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}/api/v3{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = (exc.read() or b"").decode(errors="replace")[:300]
            raise ApiError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"cannot reach Authentik at {self.base}: {exc.reason}") from exc

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    def patch(self, path, body):
        return self._call("PATCH", path, body)

    def find(self, list_path: str, **filters) -> dict | None:
        """First result of list_path whose fields match every `filters` value.

        Matching happens CLIENT-side on purpose: Authentik ignores unknown or
        unsupported query parameters on its list endpoints, so a server-side
        `?name=` filter silently returns the unfiltered first page - which would
        match an unrelated provider and then clobber it.
        """
        path = list_path if "?" in list_path else f"{list_path}?page_size=200"
        data = self.get(path)
        results = (data or {}).get("results", []) if isinstance(data, dict) else (data or [])
        for item in results:
            if all(item.get(k) == v for k, v in filters.items()):
                return item
        return None


def flatten_flows(ak: Ak) -> dict[str, str]:
    """Map flow slug -> pk for the whole (small) flow list."""
    data = ak.get("/flows/instances/?page_size=200")
    return {f["slug"]: f["pk"] for f in (data or {}).get("results", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify only - no writes, exit 1 on drift")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without changing anything")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = parser.parse_args()
    load_env(Path(args.env_file))

    base = env("AUTHENTIK_BASE_URL")
    token = env("AUTHENTIK_BOOTSTRAP_TOKEN")
    domain = env("MONARCH_DOMAIN", "monarch.innotel.us")

    provider_name = env("AUTHENTIK_FORWARD_PROVIDER", "Monarch NPM Forward Auth")
    app_slug = env("AUTHENTIK_FORWARD_APP_SLUG", "monarch-npm-forward-auth")
    auth_flow_slug = env("AUTHENTIK_FORWARD_AUTH_FLOW",
                         "default-provider-authorization-implicit-consent")
    inval_flow_slug = env("AUTHENTIK_FORWARD_INVALIDATION_FLOW",
                          "default-invalidation-flow")
    gate_group = env("AUTHENTIK_FORWARD_GROUP", "")
    external_host = env("AUTHENTIK_FORWARD_EXTERNAL_HOST") or f"https://auth.{domain}"

    if not base or not token:
        print("FAIL AUTHENTIK_BASE_URL / AUTHENTIK_BOOTSTRAP_TOKEN not set "
              "(see .env.sample). monarch-init mints the token on first boot.",
              file=sys.stderr)
        return 2

    ak = Ak(base, token)
    drift: list[str] = []
    try:
        flows = flatten_flows(ak)
    except ApiError as exc:
        print(f"FAIL could not read Authentik flows: {exc}", file=sys.stderr)
        return 1

    auth_flow = flows.get(auth_flow_slug)
    inval_flow = flows.get(inval_flow_slug)
    if not auth_flow or not inval_flow:
        print(f"FAIL flows not found (authorization={auth_flow_slug!r}, "
              f"invalidation={inval_flow_slug!r}). Available: "
              f"{', '.join(sorted(flows))}", file=sys.stderr)
        return 1

    if gate_group:
        try:
            group = ak.find("/core/groups/", name=gate_group)
        except ApiError as exc:
            print(f"FAIL could not look up group {gate_group!r}: {exc}", file=sys.stderr)
            return 1
        if not group:
            print(f"FAIL AUTHENTIK_FORWARD_GROUP={gate_group!r} does not exist in "
                  "Authentik - create it first or leave the setting empty.",
                  file=sys.stderr)
            return 1
        print(f"PASS access gate group: {gate_group} (pk {group['pk']})")
    else:
        print("PASS access gate: any authenticated Authentik user "
              "(set AUTHENTIK_FORWARD_GROUP to require a group)")

    # ---- provider ----------------------------------------------------------
    try:
        provider = ak.find("/providers/proxy/", name=provider_name)
    except ApiError as exc:
        print(f"FAIL could not list proxy providers: {exc}", file=sys.stderr)
        return 1

    want = {
        "name": provider_name,
        "authorization_flow": auth_flow,
        "invalidation_flow": inval_flow,
        "external_host": external_host,
        "mode": "forward_domain",
        "cookie_domain": domain,
    }
    if provider is None:
        if args.check:
            drift.append(f"proxy provider {provider_name!r} is missing")
            print(f"DRIFT provider {provider_name!r} missing")
        elif args.dry_run:
            print(f"  [dry-run] would create proxy provider {provider_name!r} "
                  f"(forward_domain, cookie_domain={domain}, host={external_host})")
        else:
            provider = ak.post("/providers/proxy/", want)
            print(f"PASS created proxy provider {provider_name!r} "
                  f"(pk {provider['pk']}, forward_domain, {domain})")
    else:
        changed = {k: v for k, v in want.items() if provider.get(k) != v}
        if changed:
            if args.check:
                drift.append(f"proxy provider {provider_name!r} differs "
                             f"({', '.join(sorted(changed))})")
                print(f"DRIFT provider {provider_name!r} differs in "
                      f"{', '.join(sorted(changed))}")
            elif args.dry_run:
                print(f"  [dry-run] would update proxy provider {provider_name!r} "
                      f"({', '.join(sorted(changed))})")
            else:
                provider = ak.patch(f"/providers/proxy/{provider['pk']}",
                                    {**provider, **want})
                print(f"PASS updated proxy provider {provider_name!r} "
                      f"({', '.join(sorted(changed))})")
        else:
            print(f"PASS proxy provider {provider_name!r} already correct "
                  f"(pk {provider['pk']})")

    provider_pk = provider.get("pk") if provider else None

    # ---- application -------------------------------------------------------
    if provider_pk:
        try:
            application = ak.find("/core/applications/", slug=app_slug)
        except ApiError as exc:
            print(f"FAIL could not list applications: {exc}", file=sys.stderr)
            return 1
        if application is None:
            if args.check:
                drift.append(f"application {app_slug!r} is missing")
                print(f"DRIFT application {app_slug!r} missing")
            elif args.dry_run:
                print(f"  [dry-run] would create application {app_slug!r} "
                      f"-> provider pk {provider_pk}")
            else:
                ak.post("/core/applications/",
                        {"name": provider_name, "slug": app_slug,
                         "provider": provider_pk})
                print(f"PASS created application {app_slug!r}")
        elif application.get("provider") != provider_pk:
            if args.check:
                drift.append(f"application {app_slug!r} points at provider "
                             f"{application.get('provider')}, expected {provider_pk}")
                print(f"DRIFT application {app_slug!r} provider mismatch")
            elif args.dry_run:
                print(f"  [dry-run] would repoint application {app_slug!r} "
                      f"-> provider pk {provider_pk}")
            else:
                ak.patch(f"/core/applications/{app_slug}/",
                         {"provider": provider_pk})
                print(f"PASS repointed application {app_slug!r} -> pk {provider_pk}")
        else:
            print(f"PASS application {app_slug!r} already bound to the provider")

    # ---- embedded outpost attachment --------------------------------------
    try:
        outposts = (ak.get("/outposts/instances/?page_size=100") or {}).get("results", [])
    except ApiError as exc:
        print(f"FAIL could not list outposts: {exc}", file=sys.stderr)
        return 1
    embedded = next((o for o in outposts if "embedded" in (o.get("name") or "").lower()),
                    None)
    if embedded is None:
        print("FAIL no embedded outpost found - enable the Authentik embedded "
              "outpost (Applications -> Outposts) and re-run.", file=sys.stderr)
        return 1

    attached = list(embedded.get("providers") or [])
    if provider_pk and provider_pk not in attached:
        if args.check:
            drift.append(f"provider pk {provider_pk} not attached to outpost "
                         f"{embedded['name']!r}")
            print(f"DRIFT provider pk {provider_pk} not attached to "
                  f"{embedded['name']!r}")
        elif args.dry_run:
            print(f"  [dry-run] would attach provider pk {provider_pk} to outpost "
                  f"{embedded['name']!r}")
        else:
            ak.patch(f"/outposts/instances/{embedded['pk']}/",
                     {**embedded, "providers": attached + [provider_pk]})
            print(f"PASS attached provider pk {provider_pk} to outpost "
                  f"{embedded['name']!r}")
    else:
        print(f"PASS provider already served by outpost {embedded['name']!r}")

    print()
    if args.check:
        if drift:
            print(f"FAIL {len(drift)} item(s) out of date", file=sys.stderr)
            return 1
        print("CHECK OK: Monarch forward-auth provider is in place")
        return 0

    print(f"Authentik half done. The nginx half is scripts/npm-proxy-hosts.py:")
    print(f"  the `fa` column of scripts/npm-hosts.conf gates a host on "
          f"*.{domain}.")
    print(f"  outpost (NPM -> Authentik, server-side): http://<this host>:9000")
    print(f"  browser sign-in target: {external_host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
