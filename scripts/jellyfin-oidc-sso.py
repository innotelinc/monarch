#!/usr/bin/env python3
"""jellyfin-oidc-sso.py — the SSO button Jellyfin's own login page offers.

WHY THIS EXISTS
---------------
`jellyfin-sso` no longer gates the page (see docker-compose.yml, 2026-09-18).
The gate had to go because it asked for a browser OIDC flow from clients that
do not have one — an Android TV / Fire TV client that opens `…/web/#/login` in a
webview landed on Authentik instead of the Jellyfin form, and Quick Connect had
no page to enter its code on. What replaced it is not "no authentication":

  * browsers land on Jellyfin's sign-in page, which carries the **Cerulean
    Authentik** button, and
  * native clients sign in against the Authentik LDAP outpost — which is what
    `scripts/jellyfin-login-methods.py` guards, and `scripts/verify-ldap.py`
    exercises.

The LDAP half is provisioned by `monarch-init` and checked. The OIDC half is
**deployment state with two halves that must agree**, and nothing looked at
them until this script:

  1. the plugin's config (`…/plugins/configurations/Jellyfin.Plugin.OIDC.xml`)
     has to name an enabled provider on the Cerulean Authentik issuer with this
     zone's client id, and
  2. that provider on Authentik has to carry the plugin's callback URI —
     `https://media[.magnate].innotel.us/sso/OIDC/Callback/authentik`.

They fail in opposite-looking ways. A config whose Authority was never filled
in *renders a button* that dies inside Authentik; a provider missing the
callback dies at the redirect with `invalid_request: redirect_uri does not
match`, before any login form appears — the same failure mode the oauth2-proxy
names have, one layer in. Both are silent from the login page, so both are
asserted here rather than left to whoever tries the button next.

WHERE THE TWO HALVES COME FROM, HONESTLY
----------------------------------------
The provider id (`authentik`), the display name and the callback path are the
plugin's; the Authority, client id, secret and `ServerBaseUrl` are this zone's
(`MONARCH_SSO_*` in .env, shared with the gateways — the SSO button and the
gateways are clients of the *same* Authentik application, so a user signed in
one way is signed in as far as the other is concerned).

The plugin binary itself is **not installed from this repo** — it ships with no
`sourceUrl` in its own `meta.json`, so there is nothing to pin. That is recorded
in `docs/operations.md` rather than papered over here, and the check therefore
reports what is installed (name and version) so that a rebuild that loses it,
or a version swap nobody wrote down, is visible in `drift-check`.

USAGE
-----
    python3 scripts/jellyfin-oidc-sso.py --check    # report only (default)
    python3 scripts/jellyfin-oidc-sso.py --check --plugins-dir ./data/plugins

Runs on the media host, beside Jellyfin's appdata. The Authentik half is only
checked when `AUTHENTIK_BASE_URL`/`AUTHENTIK_BOOTSTRAP_TOKEN` are configured
(they are on a deployment that `monarch-init` has run): without them the plugin
half is still judged, and the provider half is reported as not-checked rather
than passed.

Exit codes: 0 = the SSO button is wired end to end, 1 = it is not (or a half is
missing), 2 = cannot run (no plugin installed, config unreadable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_PLUGINS_DIR = Path("/docker/appdata/jellyfin/data/plugins")
CONFIG_NAME = "Jellyfin.Plugin.OIDC.xml"
PLUGIN_ASSEMBLY = "Jellyfin.Plugin.OIDC.dll"
# The callback path the plugin builds (`Callback/{providerId}` under its own
# prefix) — measured on the deployment, and the value registered on the
# provider. Kept as a template so a second provider would be reported, not
# silently ignored.
CALLBACK_TEMPLATE = "https://{host}/sso/OIDC/Callback/{provider}"
# The public names Jellyfin answers on, i.e. the names a browser can reach the
# login page at. Both must be registered: the plugin builds its redirect_uri
# from its own ServerBaseUrl, and a user who signed in on the other name would
# otherwise be refused at the callback.
DEFAULT_PUBLIC_NAMES = ("media.innotel.us", "media.magnate.innotel.us")


class CheckError(RuntimeError):
    """The posture is not what the deployment claims."""


class CannotRun(RuntimeError):
    """Not enough on disk (or an unreadable config) to judge it."""


# ── the plugin half (on disk) ───────────────────────────────────────────────


def installed_plugins(plugins_dir: Path) -> list[dict]:
    """Every plugin the Jellyfin appdata holds, as its meta.json describes it."""
    found = []
    if not plugins_dir.is_dir():
        raise CannotRun(f"{plugins_dir} is not a directory — has Jellyfin run?")
    for entry in sorted(plugins_dir.iterdir()):
        meta = entry / "meta.json"
        if not meta.is_file():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload["_dir"] = entry.name
        found.append(payload)
    return found


def find_oidc_plugin(plugins_dir: Path, plugins: list[dict]) -> Path | None:
    """The folder holding the OIDC plugin, matched on the assembly the server loads.

    Matched on the assembly rather than on `name`: the plugin's display name is
    "OIDC RBAC" and it is a fork whose owner has changed hands upstream, while
    `Jellyfin.Plugin.OIDC.dll` is what actually injects the button into the login
    form.
    """
    for plugin in plugins:
        directory = plugins_dir / plugin.get("_dir", "")
        if (directory / PLUGIN_ASSEMBLY).is_file():
            return directory
    return None


# ── the plugin's config (the other half of "it is configured") ──────────────


def parse_providers(config_path: Path) -> list[dict]:
    """Every `<OidcProviderConfig>` in the plugin's XML, lower-cased keys."""
    try:
        tree = ET.parse(config_path)
    except OSError as error:
        raise CannotRun(f"cannot read {config_path} ({error})") from None
    except ET.ParseError as error:
        raise CannotRun(f"{config_path} is not valid XML ({error})") from None

    providers = []
    for node in tree.getroot().iter("OidcProviderConfig"):
        provider = {}
        for child in node:
            provider[child.tag.strip().lower()] = (child.text or "").strip()
        providers.append(provider)
    return providers


def enabled_providers(providers: list[dict]) -> list[dict]:
    """Those the plugin will render a button for.

    `Enabled` is absent in an older config layout, and absent means enabled —
    the safe reading is the one that reports a button that would not appear,
    not the one that invents a provider the plugin ignores.
    """
    return [p for p in providers if str(p.get("enabled", "true")).lower() != "false"]


def judge_provider(provider: dict, expected_client_id: str,
                   expected_issuer_host: str) -> list[str]:
    """Every way this provider disagrees with the Cerulean deployment."""
    problems = []
    provider_id = provider.get("providerid") or "(no ProviderId)"
    authority = provider.get("authority", "")
    client_id = provider.get("clientid", "")

    if not provider.get("providerid"):
        problems.append("a provider has no ProviderId, so its callback cannot be built")
    if not authority:
        problems.append(f"provider {provider_id!r} has no Authority")
    else:
        host = urllib.parse.urlparse(authority).netloc.split(":")[0]
        if host != expected_issuer_host:
            problems.append(
                f"provider {provider_id!r} authenticates against {host!r}, "
                f"expected {expected_issuer_host!r}"
            )
    if not client_id:
        problems.append(f"provider {provider_id!r} has no ClientId")
    elif client_id != expected_client_id:
        problems.append(
            f"provider {provider_id!r} presents client {client_id!r}, "
            f"expected {expected_client_id!r}"
        )
    if not provider.get("clientsecret"):
        problems.append(f"provider {provider_id!r} has no ClientSecret")
    return problems


def callbacks_for(provider: dict, public_names: tuple[str, ...]) -> list[str]:
    """The callback URIs this provider's deployment must have registered."""
    provider_id = provider.get("providerid", "")
    if not provider_id:
        return []
    return [CALLBACK_TEMPLATE.format(host=host, provider=provider_id)
            for host in public_names]


# ── the provider half (Authentik) ───────────────────────────────────────────


def provider_redirect_uris(base_url: str, token: str, client_id: str) -> list[str]:
    """The redirect URIs registered on the OAuth2 provider with this client id."""
    url = base_url.rstrip("/") + "/api/v3/providers/oauth2/?page_size=200"
    request = urllib.request.Request(url)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise CannotRun(
            f"Authentik answered HTTP {error.code} for /providers/oauth2/"
            f"{' — the token was rejected' if error.code in (401, 403) else ''}"
        ) from None
    except (OSError, json.JSONDecodeError) as error:
        raise CannotRun(f"cannot reach Authentik at {base_url} ({error})") from None

    uris = []
    for provider in payload.get("results", []):
        if provider.get("client_id") != client_id:
            continue
        for entry in provider.get("redirect_uris") or []:
            if isinstance(entry, dict):
                value = entry.get("url")
            else:
                value = str(entry)
            if value:
                uris.append(value.strip())
    return uris


def env_from_file(path: Path) -> dict:
    """This repo's .env, for the values the checks share with the stack."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.split("#")[0].strip().strip('"').strip("'")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only (the default)")
    parser.add_argument("--plugins-dir", type=Path, default=DEFAULT_PLUGINS_DIR,
                        help=f"Jellyfin's plugin directory (default {DEFAULT_PLUGINS_DIR})")
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).resolve().parent.parent / ".env",
                        help="the stack's .env, for AUTHENTIK_BASE_URL and MONARCH_SSO_*")
    parser.add_argument("--client-id", default="",
                        help="the Authentik client id the SSO button presents")
    parser.add_argument("--authentik-base", default="", help="Authentik origin for the API")
    parser.add_argument("--issuer-host", default="auth.cerulean.innotel.us",
                        help="the Authentik issuer host the plugin must authenticate against")
    parser.add_argument("--names", default="",
                        help="comma-separated public names a callback must be registered for")
    args = parser.parse_args(argv)

    env = env_from_file(args.env_file)
    client_id = args.client_id or os.environ.get("MONARCH_SSO_CLIENT_ID") \
        or env.get("MONARCH_SSO_CLIENT_ID", "monarch-media")
    authentic_base = args.authentik_base or os.environ.get("AUTHENTIK_BASE_URL") \
        or env.get("AUTHENTIK_BASE_URL", "")
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN") \
        or env.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    names = tuple(n.strip() for n in (args.names.split(",") if args.names
                                      else DEFAULT_PUBLIC_NAMES) if n.strip())

    print(f"jellyfin-oidc-sso [check] {args.plugins_dir}")
    try:
        plugins = installed_plugins(args.plugins_dir)
        if not plugins:
            raise CannotRun(f"no plugins with a meta.json under {args.plugins_dir}")
    except CannotRun as error:
        print(f"jellyfin-oidc-sso: {error}", file=sys.stderr)
        return 2

    directory = find_oidc_plugin(args.plugins_dir, plugins)
    if directory is None:
        print("jellyfin-oidc-sso: no installed plugin carries "
              f"{PLUGIN_ASSEMBLY} — the login page has no SSO button. Install the "
              "OIDC plugin into Jellyfin's plugin directory (see docs/operations.md "
              "for the version this deployment runs) or restore the jellyfin-sso "
              "page gate.", file=sys.stderr)
        return 1

    meta_path = directory / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    version = ""
    for candidate in meta.get("versions", []) if isinstance(meta, dict) else []:
        if isinstance(candidate, dict) and candidate.get("version"):
            version = candidate["version"]
            break
    print(f"  plugin      {meta.get('name') or directory.name} "
          f"{version or '(version not recorded)'} in {directory.name}")

    config_path = args.plugins_dir / "configurations" / CONFIG_NAME
    try:
        providers = parse_providers(config_path)
    except CannotRun as error:
        print(f"jellyfin-oidc-sso: {error}", file=sys.stderr)
        return 2
    if not providers:
        print(f"jellyfin-oidc-sso: {config_path} declares no OidcProviderConfig, so the "
              "login page renders no SSO button", file=sys.stderr)
        return 1

    enabled = enabled_providers(providers)
    print(f"  providers   {len(enabled)} enabled of {len(providers)}")
    if not enabled:
        print("jellyfin-oidc-sso: every configured provider is disabled", file=sys.stderr)
        return 1

    failures = 0
    for provider in enabled:
        problems = judge_provider(provider, client_id, args.issuer_host)
        for problem in problems:
            print(f"  FAIL  {problem}", file=sys.stderr)
        failures += len(problems)
        if problems:
            continue
        print(f"  ok          {provider.get('providerid')} -> "
              f"{provider.get('authority')} (client {provider.get('clientid')})")

    wanted = []
    for provider in enabled:
        wanted.extend(callbacks_for(provider, names))
    if not wanted:
        print("  note        no callback could be built (a provider has no ProviderId)")

    if not authentic_base or not token:
        print("  note        Authentik is not configured here (set AUTHENTIK_BASE_URL and "
              "AUTHENTIK_BOOTSTRAP_TOKEN) — the registered callbacks were not checked")
    else:
        try:
            registered = provider_redirect_uris(authentic_base, token, client_id)
        except CannotRun as error:
            print(f"  note        {error} — the registered callbacks were not checked")
            registered = None
        if registered is not None:
            print(f"  registered  {len(registered)} redirect URI(s) on {client_id}")
            missing = [uri for uri in wanted if uri not in registered]
            for uri in missing:
                print(f"  FAIL  {uri} is not registered on {client_id} — the SSO button "
                      "dies at the callback with `redirect_uri does not match`",
                      file=sys.stderr)
            failures += len(missing)
            if not missing:
                print(f"  ok          both Jellyfin names carry their "
                      f"{wanted[0].split('/sso/')[1]} callback")

    if failures:
        print(f"\nFAIL — {failures} problem(s): the Jellyfin login page's SSO button is "
              "not wired end to end", file=sys.stderr)
        return 1
    print("\nok: the Jellyfin login page offers Cerulean Authentik, and the provider "
          "accepts its callback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
