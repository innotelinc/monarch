#!/usr/bin/env python3
"""
monarch-init: one-shot automatic wiring of the whole Monarch media platform.

Runs once after `docker compose up -d` (container monarch-init, image
python:3.12-slim, stdlib only - no pip packages needed). It configures:

  * Jellyfin      - first-run wizard (creates the admin user with the shared
                    credentials), adds media libraries, logs in and exports
                    the admin token (usable as an API key) to
                    /docker/appdata/init/jellyfin-api-key.txt, wires the
                    LDAP-Auth plugin at the Authentik outpost, and installs
                    the pinned plugins (init/jellyfin-plugins.json) plus the
                    OIDC plugin's config, so its login page offers Cerulean
                    Authentik
  * Sonarr/Radarr/
    Lidarr/Whisparr - external auth (the Cerulean Authentik gate in front of
                    each app is the only login), root folder, qBittorrent
                    download client, hardlink settings
  * Prowlarr      - external auth, qBittorrent download client,
                    registers the four *arr apps (full sync), and adds a
                    FlareSolverr indexer proxy (tag indexers 'cloudflare'
                    to route them through it)
  * qBittorrent   - verifies the pre-seeded WebUI login, creates the
                    movies/tv/music/xxx categories with save paths
  * Bazarr        - sets auth + connects Sonarr and Radarr (best effort)
  * Jellyseerr    - initializes against Jellyfin, connects Radarr/Sonarr and
                    enables Jellyfin sign-in (best effort)

Everything is idempotent - re-running is safe. Problems never kill the
stack: each step is wrapped, failures are collected and printed at the end
under "MANUAL ACTIONS NEEDED", and the script always exits 0 so the one-shot
container is not flagged as failed.

Secrets/state written under /docker/appdata/init/:
  * jellyfin-api-key.txt  - durable Jellyfin admin API key (use as
                            JELLYFIN_API_KEY in .env for the subscription
                            platform, AI recs and health analytics), falling
                            back to the session token if the key could not be
                            minted
  * status.json           - per-service result of the last run
  * invariants.json       - what monarch-init is supposed to maintain (the
                            drift check asserts against this)
"""

import base64
import fcntl
import ipaddress
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookiejar import CookieJar

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER = os.environ.get("MONARCH_USERNAME", "admin")
PASS = os.environ.get("MONARCH_PASSWORD", "monarch8")
APPDATA = "/docker/appdata"
INIT_DIR = "/docker/appdata/init"

# The account that owns Seerr. Seerr's Owner is `user.id === 1` and nothing else
# (`server/routes/user/index.ts`: `canMakePermissionsChange` refuses to let
# anybody but row 1 grant admin, and the PUT /:id guard refuses to let anybody
# but row 1 modify row 1), so an install whose first account was the break-glass
# Jellyfin admin is owned by an account the operator never signs in as. The name
# goes into the invariants manifest, where scripts/seerr-owner.py reads it and
# scripts/drift-check.sh judges it, so all three mean the same account.
SEERR_OWNER = os.environ.get("MONARCH_SEERR_OWNER", "dhunter")

JELLYFIN_BASE = "http://jellyfin:8096"
JELLYSEERR_BASE = "http://jellyseerr:5055"
QBT_BASE = "http://qbittorrent:8080"
PROWLARR_BASE = "http://prowlarr:9696"
BAZARR_BASE = "http://bazarr:6767"

MONARCH_APPS = [
    {"svc": "sonarr", "port": 8989, "api": "v3", "category": "tv",     "media": "tv"},
    {"svc": "radarr", "port": 7878, "api": "v3", "category": "movies", "media": "movies"},
    {"svc": "lidarr", "port": 8686, "api": "v1", "category": "music",  "media": "music"},
    {"svc": "whisparr", "port": 6969, "api": "v3", "category": "xxx",  "media": "xxx"},
]

PROWLARR_APP_IMPLS = {
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "lidarr": "Lidarr",
    "whisparr": "Whisparr",
}

JELLYFIN_LIBRARIES = [
    {"name": "Movies", "type": "movies", "path": "/data/media/movies"},
    {"name": "TV Shows", "type": "tvshows", "path": "/data/media/tv"},
    {"name": "Music", "type": "music", "path": "/data/media/music"},
    {"name": "Other", "type": "mixed", "path": "/data/media/xxx"},
]

# qBittorrent categories, one per *arr: named by the category that app's
# download client sends, saving INSIDE the downloads tree. Both halves matter,
# and each one is a different failure when it is wrong:
#
#   * the NAME has to be the string the app sends (`tvCategory` in Sonarr and
#     Whisparr, `movieCategory` in Radarr, `musicCategory` in Lidarr). A download
#     tagged with a category qBittorrent does not know is saved to the DEFAULT
#     path, so the name is what decides whether the per-category path is used;
#   * the PATH has to sit outside every library root. A category pointing at
#     /data/media/<type> is exactly what makes each app warn "Download client
#     qBittorrent places downloads in the root folder /data/media/<type>", and it
#     drops an unfinished album into the music library for Jellyfin to scan.
#
# Derived from MONARCH_APPS so the name an app is told and the name that exists
# here cannot be two different strings. Shared with configure_qbittorrent(),
# scripts/arr-download-categories.py and the invariants manifest, so the drift
# check asserts the same map init applies.
QBT_CATEGORIES = {app["category"]: f"/data/torrents/{app['media']}" for app in MONARCH_APPS}

# How a Servarr app spells "the category this download client files under". There
# is no plain `category` field in any of these schemas - that name matches
# nothing, which is how every app went without one: the client was created from
# the schema with `category` set, no field matched, and the value was dropped
# silently while the client was reported as configured.
CATEGORY_FIELDS = ("tvCategory", "movieCategory", "musicCategory", "category")

# Every hostname the *arr apps answer to (init/arr-allowlist.txt, mounted at
# /init). An *arr refuses any Host it was not told about with a bare 400 - so
# without this, Prowlarr's own app test and indexer sync (which run over the
# compose network as `http://sonarr:8989` and back as `http://prowlarr:9696`)
# were every one of them refused, and no indexer ever reached a *arr. The file
# is shared with scripts/arr-allowed-hosts.py, which is the side that can
# restart an app - this setting is only read at startup.
ARR_ALLOWLIST_FILE = os.environ.get("MONARCH_ARR_ALLOWLIST", "/init/arr-allowlist.txt")

# Host-published ports (must match docker-compose.yml) - used for the
# invariants manifest the drift check probes on localhost.
PORTS = {
    "sonarr": 8989, "radarr": 7878, "lidarr": 8686, "whisparr": 6969,
    "prowlarr": 9696, "qbt": 8080, "jellyfin": 8097,
    "jellyseerr": 5055, "bazarr": 6767,
}

# Authentik connection (used to provision the LDAP outpost Jellyfin logins
# authenticate against, and the paid_users access gate).
AUTHENTIK_BASE_URL = os.environ.get("AUTHENTIK_BASE_URL", "http://authentik-server:9000").strip().rstrip("/")
AUTHENTIK_BOOTSTRAP_TOKEN = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "").strip()

# Jellyfin LDAP-Auth plugin (authenticates logins against the Authentik LDAP
# outpost). The bind user/token/group/base DN must match what Magnate
# provisions in Authentik (same defaults in docker-compose.yml).
#
# The plugin is installed from Jellyfin's own catalog when it can be. That fetch
# used to fall back to "the latest GitHub release" and, when even that failed, to
# a hand-installed folder - which is how LDAP-Auth v23 ended up sitting beside v24
# and made every authentication throw InvalidCastException (HTTP 500 on the login
# form for a right password and a wrong one alike). The fallback is the pin now.
LDAP_PLUGIN_NAME = "LDAP-Auth"
LDAP_PLUGIN_NAMES = ("LDAP-Auth", "LDAP Authentication")   # the catalog's, and its meta.json's
LDAP_PLUGIN_CATALOG_REPO = "https://repo.jellyfin.org/files/plugin/manifest.json"
LDAP_SERVER = os.environ.get("AUTHENTIK_LDAP_SERVER", "authentik-ldap")
LDAP_PORT = os.environ.get("AUTHENTIK_LDAP_PORT", "3389")
LDAP_BIND_USER = os.environ.get("AUTHENTIK_LDAP_BIND_USER", "authentik-ldap")
LDAP_BIND_TOKEN = os.environ.get("AUTHENTIK_LDAP_BIND_TOKEN", "")
LDAP_BIND_GROUP = os.environ.get("AUTHENTIK_LDAP_BIND_GROUP", "paid_users")
LDAP_ADMIN_GROUP = os.environ.get("AUTHENTIK_LDAP_ADMIN_GROUP", "jellyfin_admins")
LDAP_BASE_DN = os.environ.get("AUTHENTIK_LDAP_BASE_DN", "dc=innotel,dc=us")
LDAP_OUTPOST_NAME = os.environ.get("AUTHENTIK_LDAP_OUTPOST", "jellyfin-ldap")
LDAP_APP_SLUG = os.environ.get("AUTHENTIK_LDAP_APP_SLUG", "jellyfin-ldap")
LDAP_OUTPOST_TOKEN = os.environ.get("AUTHENTIK_LDAP_TOKEN", "ak-ldap-outpost-2026")
# The address a *browser* is sent to, which is not the one this script talks to:
# `AUTHENTIK_BASE_URL` is the LAN address of the Cerulean Authentik, so a
# password-reset link built from it answers nothing for the person clicking it.
# `MONARCH_SSO_AUTHENTIK_BASE` is the published name the SSO gateways already
# send users to.
LDAP_PUBLIC_URL = (
    os.environ.get("MONARCH_SSO_AUTHENTIK_BASE")
    or os.environ.get("AUTHENTIK_PUBLIC_URL")
    or AUTHENTIK_BASE_URL
).strip().rstrip("/")
LDAP_SEARCH_ROLE = "jellyfin-ldap-search"
# The LDAP provider's bind flow is executed ANONYMOUSLY by the outpost (the
# outpost answers its identification/password stages with the bind DN+
# password). Authorization-designation flows are gated require_authenticated
# and can never be planned for that, so binds must use an authentication
# flow (authentik docs: LDAP binds run the default-authentication-flow or a
# dedicated LDAP auth flow with an identification + password stage).
LDAP_BIND_FLOW_SLUG = "default-authentication-flow"
LDAP_INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"

# Jellyfin plugins, pinned. Both are load-bearing and neither records which
# build it is (the OIDC plugin's meta.json ships no sourceUrl; the LDAP plugin
# reports an empty versions list), so `init/jellyfin-plugins.json` records the
# release and BOTH hashes per plugin - the zip, and the assembly inside it. It is
# the same file `scripts/jellyfin-plugin-pin.py` and `drift-check.sh` read, so a
# fresh install, a repair and the drift check all mean the same build when they
# say "the plugin". The OIDC plugin is the *Cerulean Authentik* button on
# Jellyfin's own login page, which Jellyfin's catalog does not carry.
PLUGIN_PINS_FILE = os.environ.get("JELLYFIN_PLUGIN_PINS", "/init/jellyfin-plugins.json")
OIDC_PLUGIN_CONFIG = "Jellyfin.Plugin.OIDC.xml"
# The provider id is the plugin's (it builds `…/sso/OIDC/Callback/{id}` from it
# and the deployment registered exactly that path), the rest is this zone's -
# the same Authentik application the oauth2-proxy gateways already use.
OIDC_PROVIDER_ID = "authentik"
OIDC_DISPLAY_NAME = "Cerulean Authentik"
OIDC_BUTTON_COLOR = "#6366f1"
MONARCH_SSO_AUTHENTIK_BASE = (
    os.environ.get("MONARCH_SSO_AUTHENTIK_BASE")
    or os.environ.get("AUTHENTIK_PUBLIC_URL")
    or "https://auth.cerulean.innotel.us"
).strip().rstrip("/")
MONARCH_SSO_APP = os.environ.get("MONARCH_SSO_APP", "monarch-media")
MONARCH_SSO_CLIENT_ID = os.environ.get("MONARCH_SSO_CLIENT_ID", MONARCH_SSO_APP)
MONARCH_SSO_CLIENT_SECRET = os.environ.get("MONARCH_SSO_CLIENT_SECRET", "")
# The plugin derives its redirect_uri (`{base}/sso/OIDC/Callback/authentik`)
# from this, and the provider has a callback registered for EVERY public name
# Jellyfin answers on - so the two have to agree on one of them, not on "the"
# name. The default is what this deployment runs.
MONARCH_SSO_SERVER_BASE_URL = (
    os.environ.get("MONARCH_SSO_SERVER_BASE_URL") or "https://media.magnate.innotel.us"
).strip().rstrip("/")
# Group -> Jellyfin permissions. `paid_users` is the access gate the whole
# platform uses (the LDAP outpost's search filter is the same group), and
# `jellyfin_admins` is the only group that gets administrative rights.
OIDC_ROLE_MAPPINGS = [
    {
        "role": LDAP_BIND_GROUP,
        "is_admin": "false",
        "live_tv_management": "false",
        "media_playback": "true",
        "content_deletion": "false",
        "priority": "10",
    },
    {
        "role": LDAP_ADMIN_GROUP,
        "is_admin": "true",
        "live_tv_management": "true",
        "media_playback": "true",
        "content_deletion": "true",
        "priority": "20",
    },
]

# ---------------------------------------------------------------------------
# Small HTTP helpers
# ---------------------------------------------------------------------------

_results = {}
_issues = []


def _log(msg: str) -> None:
    print(f"[monarch-init] {msg}", flush=True)


def _http(base, path, method="GET", body=None, headers=None, opener=None,
          timeout=30, raw_form=False):
    """Perform an HTTP request. Returns (status, body_text, body_json_or_None)."""
    url = base.rstrip("/") + path
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        if raw_form:
            data = urllib.parse.urlencode(body).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            try:
                j = json.loads(text) if text else None
            except ValueError:
                j = None
            return resp.status, text, j
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            j = json.loads(text) if text else None
        except ValueError:
            j = None
        return exc.code, text, j
    except Exception as exc:  # network level errors
        return 0, str(exc), None


def wait_for(base, path, desc, timeout=900, interval=8, method="GET", **kw):
    """Poll until the endpoint answers with HTTP 200 (other statuses count as up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = _http(base, path, method=method, **kw)
        if status != 0:
            _log(f"{desc} is up (HTTP {status}).")
            return True
        if status == 0:
            time.sleep(interval)
    _issues.append(f"{desc} never became reachable at {base}{path}")
    _log(f"WARNING: {desc} never became reachable at {base}{path}")
    return False


def arrived(problem):
    """Do not treat a failure as fatal; record it and keep going."""
    def deco(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as exc:  # noqa: BLE001 - best effort by design
                _issues.append(f"{problem}: {exc}")
                _log(f"WARNING: {problem}: {exc}")
                return False
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def api_key_for(svc: str):
    """Read the API key from a *arr / Prowlarr config.xml on the appdata mount."""
    candidates = [
        os.path.join(APPDATA, svc, "config.xml"),
        os.path.join(APPDATA, svc, "config", "config.xml"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            xml = fh.read()
        m = re.search(r"<ApiKey>\s*([^<\s]+)\s*</ApiKey>", xml)
        if m:
            return m.group(1)
    return None


def ensure_owner(path: str, uid: int = 1000, gid: int = 1000) -> None:
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Authentik (LDAP outpost provisioning - Jellyfin login gate)
# ---------------------------------------------------------------------------

def ak_request(method, path, body=None, params=None):
    """Authentik API call; returns (status, text, json)."""
    if not AUTHENTIK_BASE_URL or not AUTHENTIK_BOOTSTRAP_TOKEN:
        return 0, "Authentik not configured", None
    headers = {"Authorization": f"Bearer {AUTHENTIK_BOOTSTRAP_TOKEN}"}
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return _http(AUTHENTIK_BASE_URL, f"/api/v3{path}{qs}",
                 method=method, body=body, headers=headers)


def ak_find_user(username=None):
    status, _, j = ak_request("GET", "/core/users/",
                              params={"username": username} if username else None)
    if status == 200 and isinstance(j, dict):
        results = j.get("results", [])
        return results[0] if results else None
    return None


def ak_group(name: str):
    status, _, j = ak_request("GET", "/core/groups/", params={"name": name})
    if status == 200 and isinstance(j, dict) and j.get("results"):
        return j["results"][0]
    status, _, j = ak_request("POST", "/core/groups/", body={"name": name})
    if status in (200, 201) and isinstance(j, dict):
        return j
    return {}


def ak_flow_pk(slug: str):
    # Newer Authentik versions serve flow instances under /flows/instances/
    # (the legacy /flows/ path returns the web UI shell, not JSON).
    for path in ("/flows/instances/", "/flows/"):
        status, _, j = ak_request("GET", path, params={"slug": slug})
        if status == 200 and isinstance(j, dict) and j.get("results"):
            return j["results"][0].get("pk")
    return None


def ak_ensure_bind_user(username: str) -> dict:
    """Ensure the LDAP bind user exists (create as a REGULAR user).

    Regular users are required here: authentik refuses to grant per-object
    permissions to internal service accounts over the API, and role->user
    membership has NO API route in Authentik 2025.6.x. The documented LDAP
    pattern (bind user + per-user "Search full LDAP directory" grant)
    therefore only works when the bind account is a normal user. Existing
    internal service accounts (older installs) are returned untouched and
    handled by the caller.
    """
    existing = ak_find_user(username=username)
    if existing:
        return existing
    status, _, _ = ak_request("POST", "/core/users/", body={
        "username": username,
        "name": "Jellyfin LDAP bind (monarch stack)",
        "path": "users",
    })
    if status not in (200, 201):
        raise RuntimeError(f"bind user creation failed ({status})")
    return ak_find_user(username=username) or {}


def ak_grant_ldap_search(bind_user: dict, provider_pk: str) -> tuple:
    """Grant directory-wide LDAP search to the bind user on the provider.

    Returns (ok, message). Regular users receive the per-object permission
    directly. Internal service accounts cannot hold per-object grants, so we
    (re)create the role-based permission bucket and tell the operator the one
    remaining step (membership) is a server-side operation.
    """
    pk = bind_user.get("user_pk") or bind_user.get("pk")
    if bind_user.get("type") != "internal_service_account":
        status, _, _ = ak_request(
            "POST", f"/rbac/permissions/assigned_by_users/{pk}/assign/",
            body={"model": "authentik_providers_ldap.ldapprovider",
                  "object_pk": str(provider_pk),
                  "permissions": ["authentik_providers_ldap.search_full_directory"]})
        if status in (200, 201, 204):
            return True, ""
        if status != 404:
            return False, f"per-user search grant failed (HTTP {status})"
        # Authentik 2026.8 removed the assigned_by_users route but exposes the
        # equivalent role membership API. Keep the permission object-scoped to
        # this LDAP provider; the role is not granted any global permissions.
        role = ak_ensure_role(LDAP_SEARCH_ROLE)
        role_pk = role.get("pk")
        if not role_pk or not ak_role_assign_permission(
                role_pk, "authentik_providers_ldap.search_full_directory",
                model="authentik_providers_ldap.ldapprovider", object_pk=provider_pk):
            return False, "LDAP search permission role could not be assigned"
        status, _, _ = ak_request(
            "POST", f"/rbac/roles/{role_pk}/add_user/",
            body={"pk": int(pk)})
        if status in (200, 201, 204):
            return True, ""
        return False, f"LDAP search role membership failed (HTTP {status})"
    role = ak_ensure_role(LDAP_SEARCH_ROLE)
    role_pk = role.get("pk")
    ak_role_assign_permission(
        role_pk, "authentik_providers_ldap.search_full_directory",
        model="authentik_providers_ldap.ldapprovider", object_pk=provider_pk)
    return False, (
        "bind user is an internal service account; add it to the "
        f"'{LDAP_SEARCH_ROLE}' role's group server-side (manage.py shell: "
        "User.objects.get(username=...).groups.add(Role.objects.get("
        f"name='{LDAP_SEARCH_ROLE}').group)) for search to apply")


def ak_token(identifier: str):
    status, _, j = ak_request("GET", "/core/tokens/", params={"identifier": identifier})
    if status == 200 and isinstance(j, dict) and j.get("results"):
        return j["results"][0]
    return None


def ak_ensure_token(identifier, user_pk, key_value, description="", intent="app_password"):
    """Ensure a token exists and pin its key to key_value (idempotent)."""
    if not ak_token(identifier):
        body = {"identifier": identifier, "intent": intent, "expiring": False,
                "description": description}
        if user_pk is not None:
            body["user"] = int(user_pk)
        status, _, _ = ak_request("POST", "/core/tokens/", body=body)
        if status not in (200, 201):
            raise RuntimeError(f"token creation failed ({status})")
    status, _, _ = ak_request("POST", f"/core/tokens/{identifier}/set_key/",
                              body={"key": key_value})
    if status not in (200, 204):
        raise RuntimeError(f"token set_key failed ({status})")


def ak_find_provider(name: str):
    # Some Authentik versions ignore the ?name= filter and return every
    # provider, so match client-side by exact name instead.
    status, _, j = ak_request("GET", "/providers/ldap/", params={"page_size": 200})
    if status == 200 and isinstance(j, dict):
        for provider in j.get("results", []):
            if provider.get("name") == name:
                return provider
    return None


def ak_ensure_ldap_provider() -> dict:
    existing = ak_find_provider(LDAP_OUTPOST_NAME)
    if existing:
        return existing
    body = {"name": LDAP_OUTPOST_NAME, "base_dn": LDAP_BASE_DN}
    auth_flow = ak_flow_pk(LDAP_BIND_FLOW_SLUG)
    inv_flow = ak_flow_pk(LDAP_INVALIDATION_FLOW_SLUG)
    if auth_flow:
        body["authorization_flow"] = auth_flow
    if inv_flow:
        body["invalidation_flow"] = inv_flow
    status, _, j = ak_request("POST", "/providers/ldap/", body=body)
    if status not in (200, 201):
        raise RuntimeError(f"LDAP provider creation failed ({status})")
    return j


def ak_find_application(slug: str):
    # Same as ak_find_provider: the ?slug= filter is ignored by some
    # Authentik versions, so match client-side by exact slug.
    status, _, j = ak_request("GET", "/core/applications/", params={"page_size": 200})
    if status == 200 and isinstance(j, dict):
        for app in j.get("results", []):
            if app.get("slug") == slug:
                return app
    return None


def ak_ensure_application(provider_pk: str) -> dict:
    existing = ak_find_application(LDAP_APP_SLUG)
    if existing:
        return existing
    status, _, j = ak_request("POST", "/core/applications/", body={
        "name": "Jellyfin LDAP", "slug": LDAP_APP_SLUG,
        "backchannel_providers": [str(provider_pk)]})
    if status not in (200, 201):
        raise RuntimeError(f"application creation failed ({status})")
    return j


def ak_find_outpost(name: str):
    # The ?name= filter is ignored by some Authentik versions (it returns
    # every outpost, including the embedded one), so match client-side by
    # exact name - otherwise the LDAP outpost would never be created.
    status, _, j = ak_request("GET", "/outposts/instances/", params={"page_size": 200})
    if status == 200 and isinstance(j, dict):
        for outpost in j.get("results", []):
            if outpost.get("name") == name:
                return outpost
    return None


def ak_ensure_outpost(provider_pk: str) -> dict:
    existing = ak_find_outpost(LDAP_OUTPOST_NAME)
    if existing:
        return existing
    status, _, j = ak_request("POST", "/outposts/instances/", body={
        "name": LDAP_OUTPOST_NAME, "type": "ldap",
        "providers": [str(provider_pk)],
        "config": {"authentik_host": AUTHENTIK_BASE_URL,
                    "authentik_host_insecure": True}})
    if status not in (200, 201):
        raise RuntimeError(f"LDAP outpost creation failed ({status})")
    return j


def ak_ensure_role(name: str) -> dict:
    status, _, j = ak_request("GET", "/rbac/roles/", params={"name": name})
    if status == 200 and isinstance(j, dict) and j.get("results"):
        return j["results"][0]
    status, _, j = ak_request("POST", "/rbac/roles/", body={"name": name})
    if status not in (200, 201):
        raise RuntimeError(f"role creation failed ({status})")
    return j


def ak_role_assign_permission(role_pk, permission, model=None, object_pk=None) -> bool:
    body = {"permissions": [permission]}
    if model and object_pk:
        body["model"] = model
        body["object_pk"] = str(object_pk)
    status, _, _ = ak_request(
        "POST", f"/rbac/permissions/assigned_by_roles/{role_pk}/assign/", body=body)
    return status in (200, 201, 204)


@arrived("authentik ldap provisioning")
def configure_authentik_ldap():
    """Idempotently provision the LDAP provider/outpost for Jellyfin logins.

    This used to live in billing-api; with Magnate now the source billing
    platform, monarch-init owns it so fresh installs stay self-wiring.
    """
    _log("--- Authentik LDAP outpost ---")
    if not AUTHENTIK_BASE_URL or not AUTHENTIK_BOOTSTRAP_TOKEN:
        _log("WARNING: Authentik not configured - skipping LDAP provisioning.")
        return False
    if not wait_for(AUTHENTIK_BASE_URL, "/-/health/ready/", "Authentik"):
        return False

    bind = ak_ensure_bind_user(LDAP_BIND_USER)
    bind_pk = bind.get("user_pk") or bind.get("pk")
    # The password LDAP clients (Jellyfin plugin, ldapsearch, ...) bind with.
    ak_request("POST", f"/core/users/{bind_pk}/set_password/",
               body={"password": LDAP_BIND_TOKEN})

    provider = ak_ensure_ldap_provider()
    provider_pk = provider.get("pk")
    ak_ensure_application(provider_pk)

    outpost = ak_ensure_outpost(provider_pk)
    outpost_pk = outpost.get("pk")
    # The outpost's own API token (auto-created with the outpost) is pinned
    # to the value the `authentik-ldap` container uses as AUTHENTIK_TOKEN.
    ak_ensure_token(f"ak-outpost-{outpost_pk}-api", None, LDAP_OUTPOST_TOKEN,
                    "LDAP outpost API token (monarch stack)", intent="api")

    granted, grant_msg = ak_grant_ldap_search(bind, provider_pk)
    if not granted:
        _log(f"WARNING: {grant_msg}")

    admin_group = ak_group(LDAP_ADMIN_GROUP)
    _log(f"LDAP provisioning OK: provider={provider_pk} outpost={outpost_pk} "
         f"bind={LDAP_BIND_USER} group={LDAP_BIND_GROUP} "
         f"admin_group={admin_group.get('name')}")
    _results["authentik-ldap"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------




def jellyfin_wizard_pending(timeout=600) -> bool:
    """Is the first-run wizard pending? Polls until the state is DEFINITIVE.

    During early boot Jellyfin answers HTTP before the /Startup endpoints are
    mounted and before system.xml has been fully written, so a single probe
    can misread "still starting up" as "wizard completed" (public info can
    lack the StartupWizardCompleted field entirely) - which made init skip the
    wizard on a genuinely fresh boot, fail to log in, and then hit a dead-end
    zombie state. Poll until we can tell for sure:
      * /Startup/Configuration answers 200      -> wizard IS pending
      * /System/Info/Public reports completed   -> wizard is DONE
    Anything else (connection refused, 4xx/5xx, missing field) means Jellyfin
    is not done starting up yet - keep waiting.
    """
    deadline = time.time() + timeout
    saw_pending = False
    while time.time() < deadline:
        # /Startup/Configuration answers 200 only while the wizard is pending
        # AND the setup endpoints are mounted - i.e. ready for our flow.
        status, _, _ = _http(JELLYFIN_BASE, "/Startup/Configuration")
        if status == 200:
            return True
        status, _, j = _http(JELLYFIN_BASE, "/System/Info/Public")
        if status == 200 and isinstance(j, dict):
            flag = j.get("StartupWizardCompleted")
            if flag is False:
                saw_pending = True  # wizard pending, endpoints still mounting
            elif flag is True:
                return False  # definitively completed - nothing to run
        time.sleep(5)
    # Timed out: only treat it as "done" if we never saw the wizard pending;
    # otherwise report pending so the caller attempts the setup flow again.
    return saw_pending


def jellyfin_user_count() -> int:
    """Number of users in Jellyfin's own DB (0 => broken first-run state)."""
    try:
        conn = sqlite3.connect(f"{APPDATA}/jellyfin/data/data/jellyfin.db")
        try:
            row = conn.execute("SELECT COUNT(*) FROM Users").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return -1  # unknown - don't guess


def jellyfin_reset_wizard_flag() -> bool:
    """Flip IsStartupWizardCompleted back to false in system.xml.

    Jellyfin >= 10.11 only creates its default admin through the startup
    wizard, so a "completed" wizard with zero users is a dead end: the
    /Startup endpoints 401 and there is no other way in. Resetting the flag
    makes the wizard re-run on the next container start.
    """
    path = f"{APPDATA}/jellyfin/system.xml"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _log(f"WARNING: could not read {path} to reset the wizard flag: {exc}")
        return False
    new_text = re.sub(
        r"<IsStartupWizardCompleted>\s*true\s*</IsStartupWizardCompleted>",
        "<IsStartupWizardCompleted>false</IsStartupWizardCompleted>",
        text,
        flags=re.IGNORECASE,
    )
    if new_text == text:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except OSError as exc:
        _log(f"WARNING: could not write {path} to reset the wizard flag: {exc}")
        return False
    try:
        ensure_owner(path)
    except Exception:
        pass
    return True


@arrived("jellyfin setup")
def jellyfin_admin_permissions() -> dict[str, bool]:
    """The policy flags a member of the admin group holds.

    Read from OIDC_ROLE_MAPPINGS rather than restated, so the account this is
    applied to and the account that logs in through the provider cannot drift.
    """
    for mapping in OIDC_ROLE_MAPPINGS:
        if mapping.get("role") == LDAP_ADMIN_GROUP:
            return {
                "EnableContentDeletion": mapping.get("content_deletion") == "true",
                "EnableLiveTvManagement": mapping.get("live_tv_management") == "true",
            }
    return {}


def jellyfin_ensure_admin_permissions(token) -> bool:
    """Give the local admin the rights the group mapping only grants on login.

    The OIDC plugin applies the role mapping when a user *logs in through the
    provider*, so it never reaches `MONARCH_USERNAME` - a local account, and the
    one an operator uses to run the DVR. Without `EnableLiveTvManagement`
    Jellyfin offers no way to delete a recording, so the Recordings library only
    ever grows and the fix is a shell on the host.

    `POST /Users/{id}/Policy` replaces the whole policy rather than merging into
    it, so this reads the policy, folds the flags in, and writes it back - and
    leaves it alone when it already agrees.
    """
    wanted = jellyfin_admin_permissions()
    if not wanted:
        return False
    status, _, users = _http(JELLYFIN_BASE, "/Users", headers=jellyfin_headers(token))
    if status != 200 or not isinstance(users, list):
        _issues.append(f"Jellyfin: could not list users to check the admin policy (HTTP {status})")
        return False
    admin = next((user for user in users if user.get("Name") == USER), None)
    if admin is None:
        _issues.append(f"Jellyfin: no account named '{USER}' to grant the admin rights to")
        return False
    policy = dict(admin.get("Policy") or {})
    missing = {name: value for name, value in wanted.items() if policy.get(name) != value}
    if not missing:
        _log(f"Jellyfin: '{USER}' already holds the admin rights - policy left alone")
        return True
    policy.update(missing)
    status, _, _ = _http(JELLYFIN_BASE, f"/Users/{admin['Id']}/Policy", method="POST",
                         body=policy, headers=jellyfin_headers(token))
    if status in (200, 204):
        _log(f"Jellyfin: gave '{USER}' " + ", ".join(sorted(missing)))
        return True
    _issues.append(f"Jellyfin: could not set the admin policy on '{USER}' (HTTP {status})")
    return False


def configure_jellyfin():
    _log("--- Jellyfin ---")
    if not wait_for(JELLYFIN_BASE, "/System/Info/Public", "Jellyfin"):
        return False

    os.makedirs(INIT_DIR, exist_ok=True)
    try:
        ensure_owner(INIT_DIR)
    except Exception:
        pass

    if not jellyfin_wizard_pending():
        _log("Jellyfin wizard already completed - reusing existing admin.")
    else:
        _log("Completing the Jellyfin first-run wizard...")
        # Jellyfin >= 10.11: POST /Startup/User only renames/sets the password
        # on the FIRST existing user and 404s when none exists. The default
        # user is created by GET /Startup/User (via UserManager.Initialize).
        # Calling Complete without it yields a wizard marked done with zero
        # users and no way to log in - so create the user first and check
        # every step's status.
        st, _, j = _http(JELLYFIN_BASE, "/Startup/User", method="GET")
        if st != 200 or not (isinstance(j, dict) and j.get("Name")):
            _issues.append("Jellyfin: could not create the initial admin user "
                           f"(GET /Startup/User -> HTTP {st}) - wizard NOT completed.")
            _log("WARNING: Jellyfin initial user could not be created - wizard NOT completed.")
            return False
        _log(f"Jellyfin: initial user '{j['Name']}' created - renaming to '{USER}'.")
        st, _, _ = _http(JELLYFIN_BASE, "/Startup/Configuration", method="POST", body={})
        if st not in (200, 204):
            _log(f"WARNING: /Startup/Configuration -> HTTP {st} (continuing)")
        st, _, _ = _http(JELLYFIN_BASE, "/Startup/User", method="POST",
                         body={"Name": USER, "Password": PASS})
        if st not in (200, 204):
            _issues.append(f"Jellyfin: could not set admin credentials (POST /Startup/User -> HTTP {st})")
            _log("WARNING: Jellyfin admin credentials not applied - wizard NOT completed.")
            return False
        st, _, _ = _http(JELLYFIN_BASE, "/Startup/Complete", method="POST", body={})
        if st not in (200, 204):
            _issues.append(f"Jellyfin: wizard completion failed (POST /Startup/Complete -> HTTP {st})")
            _log("WARNING: Jellyfin wizard completion failed.")
            return False
        time.sleep(3)

    # Log in and keep the admin token as the de-facto API key. Right after
    # the wizard is completed Jellyfin restarts itself (setup mode -> normal),
    # so on a slow first boot AuthenticateByName can 401 for a few seconds -
    # wait for the server to settle, then retry the login a few times.
    auth_header = (
        'MediaBrowser Client="Monarch Init", Device="Linux", '
        'DeviceId="monarch-init-001", Version="1.0.0"'
    )
    token = None
    for attempt in range(6):
        if attempt:
            time.sleep(10)
        status, text, j = _http(
            JELLYFIN_BASE, "/Users/AuthenticateByName", method="POST",
            body={"Username": USER, "Pw": PASS},
            # The pinned v12 build reads the MediaBrowser header from
            # `Authorization`; `X-Emby-Authorization` is rejected with HTTP 400
            # ("Value cannot be null. (Parameter 'request.App')").
            headers={"Authorization": auth_header},
        )
        if status in (200, 201) and isinstance(j, dict) and j.get("AccessToken"):
            token = j["AccessToken"]
            break
        _log(f"Jellyfin login attempt {attempt + 1}/6 -> HTTP {status} (server may still be settling after the wizard)")
    if not token:
        # A wizard marked complete with zero users is a dead end: reset the
        # flag so the next Jellyfin start re-runs the first-run wizard (and
        # our flow above creates the admin). If the flag is ALREADY false the
        # wizard is genuinely pending - Jellyfin just needs a restart so the
        # pending wizard re-runs and creates the admin.
        if jellyfin_user_count() == 0:
            if jellyfin_reset_wizard_flag():
                _issues.append(
                    "Jellyfin had no users while the wizard was marked complete - "
                    "reset IsStartupWizardCompleted in system.xml. Restart the "
                    "jellyfin container, then re-run monarch-init to create the "
                    "admin user and export JELLYFIN_API_KEY."
                )
                _log("WARNING: Jellyfin zombie state (0 users, wizard complete) - "
                     "wizard flag reset; restart jellyfin and re-run monarch-init.")
            else:
                # The flag was already false: no reset needed, the wizard is
                # simply pending. A Jellyfin restart makes the pending wizard
                # create the admin user, after which re-running init logs in.
                _issues.append(
                    "Jellyfin has no users and the first-run wizard did not "
                    "create the admin (login was denied). Restart the jellyfin "
                    "container so the pending wizard runs, then re-run "
                    "monarch-init."
                )
                _log("WARNING: Jellyfin has 0 users but the wizard flag is already "
                     "false - restart jellyfin so the pending wizard runs, then "
                     "re-run monarch-init.")
        else:
            _issues.append("Could not log in to Jellyfin with the shared credentials "
                           "(run scripts/jellyfin-admin-password.py --set to re-align the "
                           "local admin password with MONARCH_PASSWORD, then re-run "
                           "monarch-init)")
            _log("WARNING: Jellyfin login failed - export the Jellyfin API key manually.")
        return False

    # The credential Monarch's services read. Export the DURABLE API key, not the
    # session token above: a password change revokes every session token the
    # admin holds (that is how this file went stale once, taking monarch-recs,
    # monarch-health and magnate-entitlements with it). scripts/jellyfin-admin-
    # password.py uses the same key name, so a first boot and a repair converge
    # on one key instead of stacking a new one per run.
    api_key = jellyfin_ensure_api_key(token)
    if api_key:
        token = api_key
    else:
        _log("WARNING: no durable Jellyfin API key - falling back to the session "
             "token, which the next password change will revoke.")
    key_file = os.path.join(INIT_DIR, "jellyfin-api-key.txt")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(token)
    ensure_owner(key_file)
    _log("Exported Jellyfin admin credential -> " + key_file)
    _log("Set JELLYFIN_API_KEY in .env to the contents of that file (used by the "
         "subscription platform). It is a durable API key; run "
         "scripts/jellyfin-admin-password.py --set to mint or refresh it on an "
         "existing install.")

    # Add the media libraries (read-only media mount is fine - metadata lives in
    # the Jellyfin config volume). Jellyfin >= 10.11 takes name, collectionType
    # and paths as query parameters; only LibraryOptions goes in the body.
    # Idempotency guard: skip libraries that already exist (name match).
    # Without this, every init run creates Movies2, TV Shows3, ... duplicates.
    status, _, existing = _http(
        JELLYFIN_BASE, "/Library/VirtualFolders", headers=jellyfin_headers(token))
    existing_names = (
        {v.get("Name") for v in existing}
        if status == 200 and isinstance(existing, list) else set()
    )
    for lib in JELLYFIN_LIBRARIES:
        if lib["name"] in existing_names:
            _log(f"Jellyfin library '{lib['name']}' already exists - skipping")
            continue
        qs = urllib.parse.urlencode({
            "name": lib["name"],
            "collectionType": lib["type"],
            "paths": lib["path"],
            "refreshLibrary": "false",
        })
        status, _, _ = _http(
            JELLYFIN_BASE, f"/Library/VirtualFolders?{qs}", method="POST",
            body={"LibraryOptions": {"EnableInternetProviders": True}},
            headers=jellyfin_headers(token),
        )
        if status in (200, 204):
            _log(f"Added Jellyfin library '{lib['name']}' -> {lib['path']}")
        else:
            _issues.append(f"Jellyfin library '{lib['name']}' could not be added (HTTP {status})")

    jellyfin_ensure_admin_permissions(token)

    _results["jellyfin"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Jellyfin LDAP-Auth plugin (Authentik login gate)
# ---------------------------------------------------------------------------


def jellyfin_headers(token):
    """MediaBrowser Authorization header - the only spelling this build takes.

    The pinned v12 image answers 401 to `?api_key=` and `X-Emby-Token`; the
    MediaBrowser Authorization header is what works (scripts/magnate-entitlements.py
    uses the same one).
    """
    return {"Authorization": f"MediaBrowser Token={token}"}


def jellyfin_ensure_api_key(token, name=None) -> str:
    """Return Jellyfin's durable API key called <name>, creating it if needed.

    API keys live in Jellyfin's own ApiKeys table and are not tied to the user's
    password, so - unlike the session token AuthenticateByName returns - they
    survive a password change. The name matches the one
    scripts/jellyfin-admin-password.py uses. Returns "" when the endpoints are
    unavailable, so the caller can fall back and still finish the run.
    """
    name = name or os.environ.get("JELLYFIN_API_KEY_NAME", "monarch-admin")

    def find():
        status, _, body = _http(JELLYFIN_BASE, "/Auth/Keys",
                                headers=jellyfin_headers(token))
        if status != 200 or not isinstance(body, dict):
            return ""
        for key in body.get("Items") or []:
            if key.get("AppName") == name and key.get("AccessToken"):
                return key["AccessToken"]
        return ""

    existing = find()
    if existing:
        _log(f"Jellyfin API key '{name}' already exists - reusing it")
        return existing
    status, _, _ = _http(
        JELLYFIN_BASE, "/Auth/Keys?app=" + urllib.parse.quote(name),
        method="POST", headers=jellyfin_headers(token))
    if status not in (200, 204):
        _log(f"WARNING: could not create the Jellyfin API key '{name}' (HTTP {status}).")
        return ""
    # The list endpoint lags the create on this build.
    for _ in range(5):
        created = find()
        if created:
            _log(f"Created Jellyfin API key '{name}'")
            return created
        time.sleep(1)
    _log(f"WARNING: created the Jellyfin API key '{name}' but could not read it back.")
    return ""


def jellyfin_plugin_installed(token) -> bool:
    """Whether Jellyfin has an LDAP auth plugin loaded.

    Matched on the name it reports, not on the folder it lives in: upstream calls
    it "LDAP-Auth" in its catalog entry and "LDAP Authentication" in its own
    meta.json, and the folder is Jellyfin's to name. Retired copies report
    `Superseded` and are not loaded, so they do not count as installed — the
    build itself is `scripts/jellyfin-plugin-pin.py`'s job, not this call's.
    """
    status, _, j = _http(JELLYFIN_BASE, "/Plugins", headers=jellyfin_headers(token))
    if status == 200 and isinstance(j, list):
        return any(p.get("Name") in LDAP_PLUGIN_NAMES
                   and p.get("Status") != "Superseded" for p in j)
    return False


def install_ldap_plugin_via_catalog(token) -> bool:
    """Try the official Jellyfin plugin catalog (best effort)."""
    repo = urllib.parse.quote(LDAP_PLUGIN_CATALOG_REPO, safe="")
    status, _, entries = _http(
        JELLYFIN_BASE, f"/Plugins/Repository?repositoryUrl={repo}",
        headers=jellyfin_headers(token))
    if status != 200 or not isinstance(entries, list):
        return False
    entry = next((e for e in entries if e.get("Name") in LDAP_PLUGIN_NAMES), None)
    if not entry:
        return False
    body = {"Name": entry.get("Name"), "Version": entry.get("Version"),
            "RepositoryUrl": LDAP_PLUGIN_CATALOG_REPO}
    status, _, _ = _http(
        JELLYFIN_BASE, f"/Plugins/Install?repositoryUrl={repo}", method="POST",
        body=body, headers=jellyfin_headers(token))
    return status in (200, 202, 204)


def install_ldap_plugin_via_pin() -> bool:
    """Fallback install: the pinned release, verified, into the pinned folder.

    The catalog is asked first because it is upstream's own distribution, but it
    is not trusted to pick the *build*: this is the path that used to fetch
    "GitHub's latest release" and, when that failed, leave whatever folder was
    there. That is how LDAP-Auth v23 ended up installed beside v24, which made
    every authentication return HTTP 500. The pin is one build, in one folder,
    checked before it is written.
    """
    return install_pinned_plugin(pin_for("ldap"))


def ldap_plugin_config_path() -> str:
    """Where the LDAP-Auth plugin reads its config from.

    Plugin configs live in {data}/plugins/configurations/ (same as the bundled
    TMDb/MusicBrainz configs), named after the assembly.
    """
    return os.path.join(APPDATA, "jellyfin", "data", "plugins", "configurations",
                        f"{LDAP_PLUGIN_NAME}.xml")


def write_ldap_plugin_config() -> tuple[str, str]:
    """Write the LDAP-Auth plugin config file. Returns (path, xml)."""
    bind_dn = f"cn={LDAP_BIND_USER},ou=users,{LDAP_BASE_DN}"
    search_filter = f"(memberOf=cn={LDAP_BIND_GROUP},ou=groups,{LDAP_BASE_DN})"
    admin_filter = f"(memberOf=cn={LDAP_ADMIN_GROUP},ou=groups,{LDAP_BASE_DN})"
    xml = f"""<?xml version="1.0"?>
<PluginConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <LdapUsers />
  <LdapServer>{LDAP_SERVER}</LdapServer>
  <LdapPort>{LDAP_PORT}</LdapPort>
  <UseSsl>false</UseSsl>
  <UseStartTls>false</UseStartTls>
  <SkipSslVerify>false</SkipSslVerify>
  <LdapBindUser>{bind_dn}</LdapBindUser>
  <LdapBindPassword>{LDAP_BIND_TOKEN}</LdapBindPassword>
  <LdapBaseDn>{LDAP_BASE_DN}</LdapBaseDn>
  <LdapSearchFilter>{search_filter}</LdapSearchFilter>
  <LdapAdminBaseDn />
  <LdapAdminFilter>{admin_filter}</LdapAdminFilter>
  <EnableLdapAdminFilterMemberUid>false</EnableLdapAdminFilterMemberUid>
  <LdapSearchAttributes>uid, cn, mail, displayName</LdapSearchAttributes>
  <LdapClientCertPath />
  <LdapClientKeyPath />
  <LdapRootCaPath />
  <CreateUsersFromLdap>true</CreateUsersFromLdap>
  <AllowPassChange>false</AllowPassChange>
  <LdapUidAttribute>uid</LdapUidAttribute>
  <LdapUsernameAttribute>cn</LdapUsernameAttribute>
  <LdapPasswordAttribute>userPassword</LdapPasswordAttribute>
  <EnableLdapProfileImageSync>false</EnableLdapProfileImageSync>
  <RemoveImagesNotInLdap>false</RemoveImagesNotInLdap>
  <LdapProfileImageAttribute>jpegphoto</LdapProfileImageAttribute>
  <LdapProfileImageFormat>Default</LdapProfileImageFormat>
  <EnableAllFolders>true</EnableAllFolders>
  <EnabledFolders />
  <PasswordResetUrl>{LDAP_PUBLIC_URL}/if/user/</PasswordResetUrl>
</PluginConfiguration>
"""
    path = ldap_plugin_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    try:
        ensure_owner(path)
    except Exception:
        pass
    return path, xml


# ---------------------------------------------------------------------------
# Jellyfin OIDC plugin (the SSO button on Jellyfin's own login page)
#
# Two halves have to agree, and both are written here from the deployment's own
# values: the plugin binary (pinned in init/jellyfin-plugins.json) and its config
# (the provider, this zone's client id/secret, and the group -> library mappings). They fail in opposite-looking ways - a config with no Authority
# renders a button that dies inside Authentik, and a provider that never got the
# callback dies at the redirect with `redirect_uri does not match` - so
# `scripts/jellyfin-oidc-sso.py` judges both afterwards, in drift-check.
# ---------------------------------------------------------------------------

def load_plugin_pins() -> list[dict]:
    """Every pinned plugin, as init/jellyfin-plugins.json records it."""
    with open(PLUGIN_PINS_FILE, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    plugins = payload.get("plugins") if isinstance(payload, dict) else payload
    if not isinstance(plugins, list) or not plugins:
        raise ValueError(f"{PLUGIN_PINS_FILE} pins no plugins")
    for pin in plugins:
        for key in ("name", "asset", "asset_sha256", "assembly", "assembly_sha256",
                    "plugin_dir"):
            if not pin.get(key):
                raise ValueError(f"{PLUGIN_PINS_FILE} does not pin {key!r} for "
                                 f"{pin.get('name') or '(unnamed plugin)'}")
    return plugins


def pin_for(name: str) -> dict:
    """One pin by name."""
    pins = load_plugin_pins()
    for pin in pins:
        if pin["name"] == name:
            return pin
    raise ValueError(f"no plugin pin named {name!r} in {PLUGIN_PINS_FILE} "
                     f"(have: {', '.join(p['name'] for p in pins)})")


def plugins_dir() -> str:
    return os.path.join(APPDATA, "jellyfin", "data", "plugins")


def pinned_plugin_state(pin: dict) -> str:
    """'ok' | 'missing' | 'stale' | 'duplicate'.

    Matched on the pinned hash, not on the file existing: a plugin folder left
    behind by an older build still shows up in Jellyfin's plugin list, so "there
    is a .dll" is not the question the deployment needs answered. A second
    non-retired copy is its own answer — Jellyfin loads every folder that carries
    the assembly, and one auth plugin loaded twice fails every authentication.
    """
    import hashlib
    root = plugins_dir()
    found = []
    for entry in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder) or "superseded" in entry:
            continue
        if os.path.isfile(os.path.join(folder, pin["assembly"])):
            found.append(os.path.join(folder, pin["assembly"]))
    if not found:
        return "missing"
    if len(found) > 1:
        return "duplicate"
    digest = hashlib.sha256()
    with open(found[0], "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return "ok" if digest.hexdigest() == pin["assembly_sha256"] else "stale"


def install_pinned_plugin(pin: dict) -> bool:
    """Fetch one pinned release, verify BOTH hashes, then extract it.

    The zip's hash is checked and so is the assembly inside it: the assembly is
    what Jellyfin loads, and a zip that hashes correctly but carries a different
    build is exactly the swap nobody writes down.
    """
    import hashlib
    url = os.environ.get("JELLYFIN_PLUGIN_URL") or pin.get("release_url")
    if not url:
        url = (f"https://github.com/{pin['repo']}/releases/download/{pin['tag']}/"
               f"{pin['asset']}")
    _log(f"Fetching the {pin['name']} plugin {pin.get('tag', '')} from {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            blob = resp.read()
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING: could not download the {pin['name']} plugin: {exc}")
        return False
    if pin.get("asset_bytes") and len(blob) != int(pin["asset_bytes"]):
        _log(f"WARNING: {pin['asset']} is {len(blob)} bytes, the pin says "
             f"{pin['asset_bytes']} - not installing it.")
        return False
    digest = hashlib.sha256(blob).hexdigest()
    if digest != pin["asset_sha256"]:
        _log(f"WARNING: {pin['asset']} hashes to {digest}, the pin says "
             f"{pin['asset_sha256']} - not installing it.")
        return False

    plugin_dir = os.path.join(plugins_dir(), pin["plugin_dir"])
    os.makedirs(plugin_dir, exist_ok=True)
    tmp = os.path.join(plugin_dir, pin["asset"])
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        with zipfile.ZipFile(tmp) as zf:
            members = [n for n in zf.namelist() if n.endswith(pin["assembly"])]
            if not members:
                _log(f"WARNING: {pin['asset']} does not contain {pin['assembly']}.")
                return False
            inner = hashlib.sha256(zf.read(members[0])).hexdigest()
            if inner != pin["assembly_sha256"]:
                _log(f"WARNING: the {pin['assembly']} inside {pin['asset']} is not the "
                     "pinned build - not installing it.")
                return False
            zf.extractall(plugin_dir)
        os.remove(tmp)
        # Jellyfin runs as uid/gid 1000, and a plugin it cannot read is a plugin
        # it does not load.
        for root, _dirs, files in os.walk(plugin_dir):
            for name in files:
                ensure_owner(os.path.join(root, name))
            ensure_owner(root)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING: {pin['name']} plugin extract failed: {exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def oidc_plugin_config_path() -> str:
    """Where the OIDC plugin reads its provider config from."""
    return os.path.join(plugins_dir(), "configurations", OIDC_PLUGIN_CONFIG)


def write_oidc_plugin_config() -> tuple[str, str]:
    """Write the OIDC plugin config (providers + group mappings).

    Returns (path, xml). Read-before-write at the call site: this file is
    re-read by Jellyfin only on a restart, so a rotated client secret that is
    written but never loaded looks configured and is not.
    """
    authority = (f"{MONARCH_SSO_AUTHENTIK_BASE}"
                 f"/application/o/{MONARCH_SSO_APP}/")
    mappings = "\n".join(f"""    <RoleMapping>
      <RoleName>{m['role']}</RoleName>
      <IsAdmin>{m['is_admin']}</IsAdmin>
      <EnableAllLibraries>true</EnableAllLibraries>
      <LibraryIds />
      <LibraryNames />
      <EnableLiveTv>true</EnableLiveTv>
      <EnableLiveTvManagement>{m['live_tv_management']}</EnableLiveTvManagement>
      <EnableMediaPlayback>{m['media_playback']}</EnableMediaPlayback>
      <EnableRemoteAccess>true</EnableRemoteAccess>
      <EnableTranscoding>true</EnableTranscoding>
      <EnableContentDeletion>{m['content_deletion']}</EnableContentDeletion>
      <EnableCollectionManagement>{m['is_admin']}</EnableCollectionManagement>
      <EnableSubtitleManagement>{m['is_admin']}</EnableSubtitleManagement>
      <MaxParentalRating xsi:nil="true" />
      <Priority>{m['priority']}</Priority>
    </RoleMapping>""" for m in OIDC_ROLE_MAPPINGS)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<PluginConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Providers>
    <OidcProviderConfig>
      <ProviderId>{OIDC_PROVIDER_ID}</ProviderId>
      <DisplayName>{OIDC_DISPLAY_NAME}</DisplayName>
      <Authority>{authority}</Authority>
      <ClientId>{MONARCH_SSO_CLIENT_ID}</ClientId>
      <ClientSecret>{MONARCH_SSO_CLIENT_SECRET}</ClientSecret>
      <Scopes>openid profile email groups</Scopes>
      <RoleClaim>groups</RoleClaim>
      <UsernameClaim>preferred_username</UsernameClaim>
      <DisplayNameClaim>name</DisplayNameClaim>
      <PictureClaim>picture</PictureClaim>
      <SyncProfileImage>true</SyncProfileImage>
      <Enabled>true</Enabled>
      <ButtonColor>{OIDC_BUTTON_COLOR}</ButtonColor>
      <ButtonIcon />
      <AdditionalParameters />
      <ServerBaseUrl>{MONARCH_SSO_SERVER_BASE_URL}</ServerBaseUrl>
    </OidcProviderConfig>
  </Providers>
  <RoleMappings>
{mappings}
  </RoleMappings>
  <DefaultProvider>{OIDC_PROVIDER_ID}</DefaultProvider>
</PluginConfiguration>
"""
    path = oidc_plugin_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    try:
        ensure_owner(path)
    except Exception:
        pass
    return path, xml


@arrived("jellyfin OIDC SSO")
def configure_jellyfin_oidc():
    _log("--- Jellyfin OIDC (SSO button on its own login page) ---")
    if not MONARCH_SSO_CLIENT_SECRET:
        _issues.append(
            "jellyfin-oidc: MONARCH_SSO_CLIENT_SECRET is not set in .env - the login "
            "page's Cerulean Authentik button cannot sign anyone in. It is the same "
            "client the media gateways use (Authentik application "
            f"'{MONARCH_SSO_APP}').")
        return False

    try:
        pin = pin_for("oidc")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _issues.append(f"jellyfin-oidc: cannot read the plugin pins {PLUGIN_PINS_FILE} ({exc})")
        return False

    path = oidc_plugin_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            previous = fh.read()
    except OSError:
        previous = None
    path, xml = write_oidc_plugin_config()
    _log(f"OIDC plugin config written -> {path}")
    needs_restart = previous != xml

    state = pinned_plugin_state(pin)
    if state == "ok":
        _log(f"OIDC plugin {pin.get('version') or pin.get('tag')} already installed.")
    elif state == "duplicate":
        # Installing cannot fix this, and it is not cosmetic: Jellyfin loads
        # every folder that carries the assembly, and one auth plugin loaded
        # twice fails every authentication with HTTP 500.
        _issues.append(
            f"jellyfin-oidc: more than one installed plugin folder carries "
            f"{pin['assembly']} - Jellyfin loads all of them and logins fail with "
            "HTTP 500. Retire the older folder (rename it `….superseded-<date>`), "
            "then restart Jellyfin.")
        return False
    else:
        _log(f"OIDC plugin is {state} - installing {pin.get('tag')} "
             f"({pin['asset']}).")
        if not install_pinned_plugin(pin):
            _issues.append(
                f"jellyfin-oidc: could not install the pinned OIDC plugin "
                f"({pin['repo']} {pin.get('tag')}) - the login page will have no SSO "
                "button. Fetch/verify it with "
                "scripts/jellyfin-plugin-pin.py --install --plugin oidc.")
            return False
        needs_restart = True
        _log(f"OIDC plugin installed ({pin['assembly']} into {pin['plugin_dir']}).")

    if needs_restart:
        token = ""
        try:
            with open(os.path.join(INIT_DIR, "jellyfin-api-key.txt"), "r") as fh:
                token = fh.read().strip()
        except OSError:
            pass
        if not token:
            _issues.append("jellyfin-oidc: config written but no Jellyfin token to restart "
                           "with - restart Jellyfin so the plugin loads it")
            return False
        _log("Restarting Jellyfin so the plugin loads...")
        status, _, _ = _http(JELLYFIN_BASE, "/System/Restart", method="POST",
                             headers=jellyfin_headers(token))
        _log(f"Jellyfin restart triggered (HTTP {status}).")
        if not wait_for(JELLYFIN_BASE, "/System/Info/Public", "Jellyfin (after OIDC restart)",
                        timeout=900):
            return False
        time.sleep(10)

    _results["jellyfin-oidc"] = "configured"
    return True


@arrived("jellyfin ldap wiring")
def configure_jellyfin_ldap():
    _log("--- Jellyfin LDAP (Authentik login gate) ---")
    token = ""
    try:
        with open(os.path.join(INIT_DIR, "jellyfin-api-key.txt"), "r") as fh:
            token = fh.read().strip()
    except OSError:
        pass
    if not token:
        _issues.append("jellyfin-ldap: no Jellyfin admin token available - run Jellyfin setup first")
        return False
    if not AUTHENTIK_BASE_URL or not AUTHENTIK_BOOTSTRAP_TOKEN:
        # Authentik is not part of this deployment (e.g. the CI full-stack
        # check blanks it on purpose) - there is no LDAP outpost to point the
        # plugin at, and installing it would restart Jellyfin for nothing.
        _log("WARNING: Authentik not configured - skipping Jellyfin LDAP wiring.")
        return False
    if not LDAP_BIND_TOKEN:
        _issues.append("jellyfin-ldap: AUTHENTIK_LDAP_BIND_TOKEN is not set in docker-compose.yml")
        return False

    # Read BEFORE writing. This used to write first and compare afterwards, and a
    # file always equals what was just written to it — so `needs_restart` was never
    # true and a rotated bind token sat on disk while Jellyfin kept serving the old
    # one out of memory. That is the whole failure this ordering avoids: the plugin
    # re-reads its config only on a restart.
    path = ldap_plugin_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            previous = fh.read()
    except OSError:
        previous = None

    path, xml = write_ldap_plugin_config()
    _log(f"LDAP-Auth plugin config written -> {path}")

    needs_restart = previous != xml
    state = pinned_plugin_state(pin_for("ldap"))
    if state == "ok" and jellyfin_plugin_installed(token):
        _log("LDAP-Auth plugin already installed, and it is the pinned build.")
    elif state == "duplicate":
        _issues.append(
            "jellyfin-ldap: more than one installed plugin folder carries LDAP-Auth.dll - "
            "Jellyfin loads all of them, the plugin's config type is cast across two "
            "load contexts, and every authentication returns HTTP 500. Retire the older "
            "folder (rename it `….superseded-<date>`), then restart Jellyfin.")
        return False
    else:
        _log(f"LDAP-Auth plugin is {state} - installing the pinned build "
             f"(catalog first, then init/jellyfin-plugins.json)...")
        ok = install_ldap_plugin_via_catalog(token)
        if not ok:
            _log("Catalog install unavailable - falling back to the pinned release.")
            ok = install_ldap_plugin_via_pin()
        if not ok:
            _issues.append("jellyfin-ldap: could not install the pinned LDAP-Auth plugin - "
                           "install it in Jellyfin Dashboard > Plugins > Catalog (name: "
                           "LDAP-Auth), or run scripts/jellyfin-plugin-pin.py --install "
                           "--plugin ldap on the host. The config file is already in place.")
            return False
        needs_restart = True
        _log("LDAP-Auth plugin installed.")

    if needs_restart:
        _log("Restarting Jellyfin so the plugin loads the new config...")
        status, _, _ = _http(JELLYFIN_BASE, "/System/Restart", method="POST",
                             headers=jellyfin_headers(token))
        _log(f"Jellyfin restart triggered (HTTP {status}).")
        if not wait_for(JELLYFIN_BASE, "/System/Info/Public", "Jellyfin (after restart)",
                        timeout=900):
            return False
        time.sleep(10)

    if jellyfin_plugin_installed(token):
        _log("LDAP-Auth plugin loaded. Jellyfin logins now resolve against Authentik LDAP "
             f"({LDAP_SERVER}:{LDAP_PORT}, group cn={LDAP_BIND_GROUP}, "
             f"admins cn={LDAP_ADMIN_GROUP}).")
        _results["jellyfin-ldap"] = "configured"
        return True
    _issues.append("jellyfin-ldap: plugin not visible after restart - check Dashboard > Plugins")
    return False


# ---------------------------------------------------------------------------
# Jellyfin Live TV (native M3U tuner + XMLTV guide)
#
# Jellyfin ingests the iptv-org playlist directly as an M3U tuner (no
# TVHeadend/NextPVR needed) and uses the XMLTV guide generated by the iptv
# EPG container. Override LIVETV_M3U_URL with your own provider's playlist
# to use a paid source instead. The EPG channel list for the guide is
# assembled from iptv-org's site files (falling back to the bundled
# /init/livetv.channels.xml) and written to /opt/epg/channels.xml, which the
# iptv container reads on its next grab.
# ---------------------------------------------------------------------------

LIVETV_M3U_URL = os.environ.get(
    "LIVETV_M3U_URL",
    "https://iptv-org.github.io/iptv/countries/us.m3u")
LIVETV_GUIDE_URL = os.environ.get(
    "LIVETV_GUIDE_URL", "http://iptv:3000/guide.xml")
LIVETV_CHANNELS_SRC = "/init/livetv.channels.xml"   # bundled fallback
LIVETV_CHANNELS_DST = "/opt/epg/channels.xml"       # read by the iptv container
# Free guide sources with both streams in the iptv-org playlist and EPG data.
# Ordered most-useful first: the list is assembled in this order, so a channel
# covered by two sites is offered to the grabber from both and the richer guide
# wins when Jellyfin merges them.
#
# Chosen by measurement, not reputation: every site under
# https://github.com/iptv-org/epg/tree/master/sites was matched against the
# dial's 1,470 stream ids, and these are the ones that actually cover it.
# xumo.tv alone covers 128 and the playable US fast-channels; tvtv.us adds 163
# (the cable networks), tvpassport.com 91 and tvguide.com 63 (the same networks'
# listings from a second source), then the smaller FAST guides. Together they
# take the dial from 128 channels with a guide to 339 — the remaining streams are
# community-sourced FAST feeds no EPG site publishes.
#
# Override with LIVETV_EPG_SITES="a.com,b.com" to use a paid source instead.
LIVETV_EPG_SITES = [
    site.strip()
    for site in os.environ.get(
        "LIVETV_EPG_SITES",
        "xumo.tv,tvtv.us,tvpassport.com,tvguide.com,i24news.tv,"
        "watch.whaletvplus.com,distro.tv,watchyour.tv,epg.iptvx.one,gatotv.com",
    ).split(",")
    if site.strip()
]
LIVETV_EPG_RAW = ("https://raw.githubusercontent.com/iptv-org/epg/master/"
                  "sites/{site}/{site}.channels.xml")


def _fetch_livetv_channels():
    """Assemble an EPG channel list from iptv-org's per-site files.

    Returns XML text, or None if every site fetch failed. Entries without an
    xmltv_id are dropped (no guide data) and duplicates are removed.

    A duplicate is the same channel *from the same site* — `site_id` alone is
    not unique, because two sites may each label a channel `cnn`, and keying on
    it would drop the second site's copy and with it a second guide source.
    Distinct sites covering one xmltv_id are both kept, which is what lets the
    grabber merge their listings.
    """
    seen, parts = set(), []
    for site in LIVETV_EPG_SITES:
        url = LIVETV_EPG_RAW.format(site=site)
        try:
            with urllib.request.urlopen(url, timeout=25) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            _log(f"livetv: could not fetch {site} channel list ({exc})")
            continue
        for m in re.finditer(r"<channel\b[^>]*>.*?</channel>", text, re.S):
            tag = m.group(0)
            if 'xmltv_id=""' in tag:
                continue
            key = re.search(r'site_id="([^"]*)"', tag)
            if key:
                pair = (site, key.group(1))
                if pair in seen:
                    continue
                seen.add(pair)
            parts.append(tag)
    if not parts:
        return None
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n'
            + "\n".join(parts) + "\n</channels>\n")


def write_livetv_channels() -> bool:
    """Make /opt/epg/channels.xml exist for the iptv EPG grabber.

    Prefers a fresh list assembled from iptv-org's site files; falls back to
    the bundled list. Never overwrites a file that is already there (it may
    be a custom list the user placed themselves).
    """
    if not os.path.isdir(os.path.dirname(LIVETV_CHANNELS_DST)):
        _log(f"livetv: {os.path.dirname(LIVETV_CHANNELS_DST)} not mounted - "
             "skipping channels.xml (Live TV will have no guide)")
        return False
    if os.path.exists(LIVETV_CHANNELS_DST):
        _log("livetv: /opt/epg/channels.xml already exists - leaving it as-is "
             "(delete it to refresh from iptv-org)")
        return True
    text = _fetch_livetv_channels()
    if text is None and os.path.exists(LIVETV_CHANNELS_SRC):
        with open(LIVETV_CHANNELS_SRC, "r", encoding="utf-8") as fh:
            text = fh.read()
        _log("livetv: using the bundled channel list (site fetches failed)")
    if text is None:
        _issues.append("livetv: could not obtain an EPG channel list - "
                       "Live TV channels will work but the guide will be empty")
        return False
    try:
        with open(LIVETV_CHANNELS_DST, "w", encoding="utf-8") as fh:
            fh.write(text)
        _log(f"livetv: wrote {LIVETV_CHANNELS_DST} "
             f"({len(re.findall(r'<channel\b', text))} channels)")
        return True
    except OSError as exc:
        _issues.append(f"livetv: could not write {LIVETV_CHANNELS_DST}: {exc}")
        return False


@arrived("jellyfin livetv")
def configure_livetv():
    _log("--- Jellyfin Live TV (M3U tuner + XMLTV guide) ---")
    write_livetv_channels()

    token = ""
    try:
        with open(os.path.join(INIT_DIR, "jellyfin-api-key.txt"), "r") as fh:
            token = fh.read().strip()
    except OSError:
        pass
    if not token:
        _issues.append("livetv: no Jellyfin admin token available - run Jellyfin setup first")
        return False

    # Existing tuners/providers - keeps re-runs idempotent.
    status, _, cfg = _http(JELLYFIN_BASE, "/System/Configuration/livetv",
                           headers=jellyfin_headers(token))
    tuners = cfg.get("TunerHosts", []) if isinstance(cfg, dict) else []
    providers = cfg.get("ListingProviders", []) if isinstance(cfg, dict) else []

    if any(t.get("Type") == "m3u" and t.get("Url") == LIVETV_M3U_URL for t in tuners):
        _log("livetv: M3U tuner already configured.")
    else:
        status, _, _ = _http(
            JELLYFIN_BASE, "/LiveTv/TunerHosts", method="POST",
            body={
                "Type": "m3u",
                "Url": LIVETV_M3U_URL,
                "FriendlyName": "IPTV (iptv-org)",
                "ImportFavoritesOnly": False,
                "AllowHWTranscoding": False,
                "EnableStreamLooping": False,
                "TunerCount": 0,
                "Source": "",
                "UserAgent": "",
            },
            headers=jellyfin_headers(token))
        if status in (200, 201, 204):
            _log(f"livetv: added M3U tuner -> {LIVETV_M3U_URL} "
                 "(Jellyfin is refreshing channels in the background)")
        else:
            _issues.append(f"livetv: adding the M3U tuner failed (HTTP {status})")

    if any(p.get("Type") == "xmltv" and p.get("Path") == LIVETV_GUIDE_URL
           for p in providers):
        _log("livetv: XMLTV guide provider already configured.")
    else:
        status, _, _ = _http(
            JELLYFIN_BASE,
            "/LiveTv/ListingProviders?validateListings=false&validateLogin=false",
            method="POST",
            body={
                "Type": "xmltv",
                "Path": LIVETV_GUIDE_URL,
                "EnableAllTuners": True,
                "EnabledTuners": [],
                "PreferredLanguage": "en",
                "UserAgent": "",
            },
            headers=jellyfin_headers(token))
        if status in (200, 201, 204):
            _log(f"livetv: added XMLTV guide provider -> {LIVETV_GUIDE_URL}")
        else:
            _issues.append(f"livetv: adding the XMLTV guide provider failed (HTTP {status})")

    _log("livetv: done - if the guide is empty, run 'sudo docker restart iptv' to "
         "trigger an immediate EPG grab (the iptv container also re-grabs "
         "twice a day on its own).")
    _results["jellyfin-livetv"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Sonarr / Radarr / Lidarr / Whisparr
# ---------------------------------------------------------------------------

def monarch_app_base(svc, port):
    return f"http://{svc}:{port}"


def parse_arr_allowlist(text):
    """The names in init/arr-allowlist.txt, comment lines and blanks removed."""
    names = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        for name in line.replace(",", " ").split():
            if name and name not in names:
                names.append(name)
    return names


def arr_allowlist():
    """The shared allowlist, or [] when it is not mounted where we expect."""
    try:
        with open(ARR_ALLOWLIST_FILE, "r", encoding="utf-8") as handle:
            return parse_arr_allowlist(handle.read())
    except OSError:
        return []


def ensure_allowed_hosts(base, api, key):
    """Make an *arr answer its in-network callers, not only the edge name.

    Keeps every name already in the list (an operator's addition is not ours to
    drop) and reports that a restart is what applies the change: a running app
    has already read the old list, which is how four registered apps sat next to
    an empty indexer list in all four of them.
    """
    want = arr_allowlist()
    if not want:
        return False, f"no allowlist at {ARR_ALLOWLIST_FILE}"
    status, _, j = _http(base, f"/api/{api}/config/host", headers={"X-Api-Key": key})
    if status != 200 or not isinstance(j, dict):
        return False, "config/host unreachable"
    have = [h.strip() for h in (j.get("allowedHosts") or "").split(",") if h.strip()]
    missing = [h for h in want if h not in have]
    if not missing:
        return True, "allowed hosts complete"
    j["allowedHosts"] = ",".join(have + missing)
    # Servarr rejects the PUT unless these agree; there is no per-field endpoint.
    if j.get("password"):
        j["passwordConfirmation"] = j["password"]
    status, _, _ = _http(base, f"/api/{api}/config/host", method="PUT", body=j,
                         headers={"X-Api-Key": key})
    if status in (200, 202):
        return True, (f"allowed hosts extended (+{len(missing)}) - restart the app "
                      "to apply it")
    return False, f"allowed hosts not applied (HTTP {status})"


def set_monarch_app_auth(base, api, key):
    """Let Cerulean Authentik be the ONLY login for a *arr app.

    The Monarch NPM hosts put an Authentik auth_request gate in front of these
    apps (the `fa` flag in scripts/npm-hosts.conf), and the apps have no OIDC of
    their own. Their built-in Forms login would be a SECOND prompt after SSO, so
    it is switched to `external` - *arr's mode for "a reverse proxy already
    authenticated this user" - which stops the app from challenging.
    """
    status, _, j = _http(base, f"/api/{api}/config/host", headers={"X-Api-Key": key})
    if status != 200 or not isinstance(j, dict):
        return False, "config/host unreachable"
    if j.get("authenticationMethod") == "external":
        return True, "external auth already set"
    j["authenticationMethod"] = "external"
    # Older builds still validate these fields even in external mode.
    j["username"] = USER
    j["password"] = PASS
    # Newer *arr versions require passwordConfirmation to match password or
    # the PUT is rejected with HTTP 400 (and an empty body).
    j["passwordConfirmation"] = PASS
    j.setdefault("authenticationRequired", "disabledForLocalAddresses")
    status, _, _ = _http(base, f"/api/{api}/config/host", method="PUT", body=j,
                         headers={"X-Api-Key": key})
    if status in (200, 202):
        return True, "external auth set (Cerulean SSO is the only login)"
    return False, f"auth not applied (HTTP {status})"


def ensure_root_folder(base, api, key, path, want_name=None, extra=None):
    status, _, j = _http(base, f"/api/{api}/rootfolder", headers={"X-Api-Key": key})
    if status == 200 and isinstance(j, list):
        if any(str(rf.get("path", "")).rstrip("/") == path.rstrip("/") for rf in j):
            return True, "exists"
    body = {"path": path}
    if want_name:
        body["name"] = want_name
    if extra:
        body.update(extra)
    status, _, _ = _http(base, f"/api/{api}/rootfolder", method="POST", body=body,
                         headers={"X-Api-Key": key})
    if status in (200, 201):
        return True, "added"
    return False, f"could not add (HTTP {status})"


def lidarr_root_folder_defaults(base, key):
    """Lidarr v2 rejects rootfolder POSTs without profile IDs."""
    extra = {}
    st, _, profs = _http(base, "/api/v1/qualityprofile", headers={"X-Api-Key": key})
    if st == 200 and isinstance(profs, list) and profs:
        extra["defaultQualityProfileId"] = profs[0]["id"]
    st, _, profs = _http(base, "/api/v1/metadataprofile", headers={"X-Api-Key": key})
    if st == 200 and isinstance(profs, list) and profs:
        extra["defaultMetadataProfileId"] = profs[0]["id"]
    return extra


def category_field(resource: dict):
    """The field that holds this app's download-client category, or None.

    The spelling is the app's own: Sonarr and Whisparr send `tvCategory`,
    Radarr `movieCategory`, Lidarr `musicCategory`. Almost every Servarr
    resource calls a value like this `category`, which is why the bug here was
    invisible - a schema built with `category` set had no field by that name, so
    the value was dropped and the client was still reported as configured.
    """
    fields = {f.get("name"): f for f in resource.get("fields") or []}
    for name in CATEGORY_FIELDS:
        if name in fields:
            return fields[name]
    return None


def ensure_qbt_client(base, api, key, category):
    """Add the qBittorrent download client, or correct the one that exists.

    A client that is present with the WRONG category is the failure this used to
    miss. It returned "exists" on the implementation name alone, so a hand-set
    category survived every run of monarch-init: the app then asks qBittorrent
    for a category init never created, and either the download falls back to the
    default save path or - when the hand-set name exists too, as a stray
    `lidarr` -> /data/media/music did - it lands in the library root. That is the
    health warning "Download client qBittorrent places downloads in the root
    folder /data/media/music", and restarting init never cleared it.
    """
    status, _, j = _http(base, f"/api/{api}/downloadclient", headers={"X-Api-Key": key})
    clients = j if status == 200 and isinstance(j, list) else []
    existing = [c for c in clients
                if isinstance(c, dict) and c.get("implementation") == "QBittorrent"]
    if existing:
        client = existing[0]
        field = category_field(client)
        if field is None or field.get("value") == category:
            return True, "exists"
        previous = field.get("value")
        field["value"] = category
        # Servarr has no per-field endpoint: the whole resource travels back.
        st, _, _ = _http(base, f"/api/{api}/downloadclient/{client['id']}", method="PUT",
                         body=client, headers={"X-Api-Key": key})
        if st in (200, 202):
            return True, f"category {previous!r} -> {category!r}"
        return False, f"could not correct the category (HTTP {st})"

    status, _, schema = _http(base, f"/api/{api}/downloadclient/schema",
                              headers={"X-Api-Key": key})
    if status != 200 or not isinstance(schema, list):
        return False, "schema unreachable"
    payload = None
    for entry in schema:
        if entry.get("implementation") == "QBittorrent":
            payload = entry
            break
    if not payload:
        return False, "no QBittorrent schema"

    values = {
        "host": "qbittorrent",
        "port": 8080,
        "useSsl": False,
        "username": USER,
        "password": PASS,
        "urlBase": "",
    }
    for field in payload.get("fields", []):
        name = field.get("name")
        if name in values:
            field["value"] = values[name]
    field = category_field(payload)
    if field is None:
        return False, "this build's QBittorrent schema has no category field"
    field["value"] = category
    payload["name"] = "qBittorrent"
    payload["enable"] = True
    status, _, _ = _http(base, f"/api/{api}/downloadclient", method="POST",
                         body=payload, headers={"X-Api-Key": key})
    if status in (200, 201):
        return True, "added"
    return False, f"could not add (HTTP {status})"


def ensure_media_mgmt(base, api, key):
    status, _, j = _http(base, f"/api/{api}/config/mediamanagement",
                         headers={"X-Api-Key": key})
    if status != 200 or not isinstance(j, dict):
        return False, "config/mediamanagement unreachable"
    changed = False
    for field, val in (("copyUsingHardlinks", True),
                       ("importExtraFiles", True),
                       ("extraFileExtensions", "srt,sub,nfo")):
        if j.get(field) != val:
            j[field] = val
            changed = True
    if changed:
        status, _, _ = _http(base, f"/api/{api}/config/mediamanagement", method="PUT",
                             body=j, headers={"X-Api-Key": key})
        ok = status in (200, 202)
        return ok, ("updated" if ok else f"could not update (HTTP {status})")
    return True, "unchanged"


@arrived("sonarr/radarr/lidarr/whisparr setup")
def configure_monarch_apps():
    _log("--- *arr apps ---")
    for app in MONARCH_APPS:
        svc = app["svc"]
        key = api_key_for(svc)
        if not key:
            _issues.append(f"{svc}: API key not found in {APPDATA}/{svc}/config.xml")
            _log(f"WARNING: {svc} API key not found - skipped.")
            continue
        base = monarch_app_base(svc, app["port"])
        api = app["api"]

        if not wait_for(base, f"/api/{api}/system/status", f"{svc}",
                        timeout=900, headers={"X-Api-Key": key}):
            continue

        ok, msg = set_monarch_app_auth(base, api, key)
        _log(f"{svc}: auth -> {msg}")
        if not ok:
            _issues.append(f"{svc}: {msg}")

        ok, msg = ensure_allowed_hosts(base, api, key)
        _log(f"{svc}: allowed hosts -> {msg}")
        if not ok:
            _issues.append(f"{svc}: {msg}")

        media_root = f"/data/media/{app['media']}"
        want_name = "Music" if svc == "lidarr" else None
        extra = lidarr_root_folder_defaults(base, key) if svc == "lidarr" else None
        ok, msg = ensure_root_folder(base, api, key, media_root, want_name, extra)
        _log(f"{svc}: root folder {media_root} -> {msg}")
        if not ok and "exists" not in msg:
            _issues.append(f"{svc}: root folder {media_root} {msg}")

        ok, msg = ensure_qbt_client(base, api, key, app["category"])
        _log(f"{svc}: qBittorrent client -> {msg}")
        if not ok and "exists" not in msg:
            _issues.append(f"{svc}: qBittorrent client {msg}")

        ok, msg = ensure_media_mgmt(base, api, key)
        _log(f"{svc}: media management -> {msg}")

        _results[svc] = "configured"
    return True


# ---------------------------------------------------------------------------
# Prowlarr
# ---------------------------------------------------------------------------

def prowlarr_post(path, body, key):
    """POST with fallback for the renamed applications/downloadclients endpoints.

    Newer Prowlarr versions renamed /api/v1/apps to /api/v1/applications; the
    old name answers 405 (not 404), so treat both as "try the next candidate".
    """
    seen = set()
    for cand in (path, path.replace("/apps", "/applications"),
                 path.replace("/downloadclient", "/downloadclients")):
        if cand in seen:
            continue
        seen.add(cand)
        status, _, _ = _http(PROWLARR_BASE, cand, method="POST", body=body,
                             headers={"X-Api-Key": key})
        if status not in (404, 405):
            return status
    return 404


@arrived("prowlarr setup")
def configure_prowlarr():
    _log("--- Prowlarr ---")
    key = api_key_for("prowlarr")
    if not key:
        _issues.append("prowlarr: API key not found")
        return False
    if not wait_for(PROWLARR_BASE, "/api/v1/system/status", "Prowlarr",
                    timeout=900, headers={"X-Api-Key": key}):
        return False

    status, _, j = _http(PROWLARR_BASE, "/api/v1/config/host", headers={"X-Api-Key": key})
    if status == 200 and isinstance(j, dict) and j.get("authenticationMethod") != "external":
        # `external`: the Cerulean Authentik gate on prowlarr.<MONARCH_DOMAIN>
        # is the only login - no second prompt from Prowlarr's own Forms auth.
        j["authenticationMethod"] = "external"
        j["username"] = USER
        j["password"] = PASS
        # Newer versions require passwordConfirmation to match password or
        # the PUT is rejected with HTTP 400.
        j["passwordConfirmation"] = PASS
        j.setdefault("authenticationRequired", "disabledForLocalAddresses")
        status, _, _ = _http(PROWLARR_BASE, "/api/v1/config/host", method="PUT",
                             body=j, headers={"X-Api-Key": key})
        _log(f"Prowlarr: external auth set (HTTP {status})")
    else:
        _log("Prowlarr: external auth already configured")

    # The other half of the wiring: the *arrs reach Prowlarr at
    # `http://prowlarr:9696`, and a Host that is not listed is a 400 no matter
    # who is asking or what credential they hold.
    ok, msg = ensure_allowed_hosts(PROWLARR_BASE, "v1", key)
    _log(f"Prowlarr: allowed hosts -> {msg}")
    if not ok:
        _issues.append(f"prowlarr: {msg}")

    # qBittorrent download client (skip if one already exists).
    status, _, clients = _http(PROWLARR_BASE, "/api/v1/downloadclient",
                               headers={"X-Api-Key": key})
    if status == 200 and isinstance(clients, list) and any(
            c.get("implementation") == "QBittorrent" for c in clients):
        _log("Prowlarr: qBittorrent download client already exists - skipping.")
    else:
        status, _, schema = _http(PROWLARR_BASE, "/api/v1/downloadclient/schema",
                                  headers={"X-Api-Key": key})
        if status == 200 and isinstance(schema, list):
            payload = None
            for entry in schema:
                if entry.get("implementation") == "QBittorrent":
                    payload = entry
                    break
            if payload:
                values = {"host": "qbittorrent", "port": 8080, "useSsl": False,
                          "username": USER, "password": PASS, "category": "", "urlBase": ""}
                for field in payload.get("fields", []):
                    if field.get("name") in values:
                        field["value"] = values[field["name"]]
                payload["name"] = "qBittorrent"
                payload["enable"] = True
                st = prowlarr_post("/api/v1/downloadclient", payload, key)
                _log(f"Prowlarr: qBittorrent download client -> HTTP {st}")
            else:
                _issues.append("prowlarr: no QBittorrent schema found")
        else:
            _issues.append(f"prowlarr: downloadclient schema unreachable (HTTP {status})")

    # Register the *arr apps. Newer Prowlarr renamed the resource to
    # /api/v1/applications; try the modern name first, fall back to /apps.
    def prowlarr_list(path):
        for cand in (path, path.replace("/applications", "/apps")):
            status, _, j = _http(PROWLARR_BASE, cand, headers={"X-Api-Key": key})
            if status == 200 and isinstance(j, list):
                return j
        return None

    apps = prowlarr_list("/api/v1/applications")
    existing = {a.get("implementation") for a in apps} if apps else set()
    schema = None
    for cand in ("/api/v1/applications/schema", "/api/v1/apps/schema"):
        status, _, j = _http(PROWLARR_BASE, cand, headers={"X-Api-Key": key})
        if status == 200 and isinstance(j, list):
            schema = j
            break
    if not isinstance(schema, list):
        _issues.append("prowlarr: apps schema unreachable")
        return False

    for app in MONARCH_APPS:
        impl = PROWLARR_APP_IMPLS[app["svc"]]
        if impl in existing:
            _log(f"Prowlarr app {impl} already registered - skipping.")
            continue
        app_key = api_key_for(app["svc"])
        if not app_key:
            _issues.append(f"prowlarr: no API key for {app['svc']} app")
            continue
        payload = None
        for entry in schema:
            if entry.get("implementation") == impl:
                payload = entry
                break
        if not payload:
            _issues.append(f"prowlarr: no {impl} app schema")
            continue
        values = {
            "prowlarrUrl": "http://prowlarr:9696",
            "baseUrl": f"http://{app['svc']}:{app['port']}",
            "apiKey": app_key,
            "syncLevel": "fullSync",
        }
        for field in payload.get("fields", []):
            if field.get("name") in values:
                field["value"] = values[field["name"]]
        payload["name"] = impl
        st = prowlarr_post("/api/v1/apps", payload, key)
        if st in (200, 201):
            _log(f"Prowlarr app {impl} registered (sync level fullSync).")
        else:
            _issues.append(f"prowlarr: registering {impl} app failed (HTTP {st})")

    # FlareSolverr indexer proxy (README: tag indexers 'cloudflare' to route
    # them through it). Creates the proxy + tag on first run - idempotent, and
    # a no-op for indexers until one is tagged.
    try:
        status, _, proxies = _http(PROWLARR_BASE, "/api/v1/indexerproxy",
                                   headers={"X-Api-Key": key})
        if status == 200 and isinstance(proxies, list) and any(
                p.get("implementation") == "FlareSolverr" for p in proxies):
            _log("Prowlarr: FlareSolverr proxy already exists - skipping.")
        else:
            payload = None
            status, _, schema = _http(PROWLARR_BASE, "/api/v1/indexerproxy/schema",
                                      headers={"X-Api-Key": key})
            if status == 200 and isinstance(schema, list):
                for entry in schema:
                    if entry.get("implementation") == "FlareSolverr":
                        payload = entry
                        break
            if not payload:
                _issues.append("prowlarr: no FlareSolverr proxy schema found")
            else:
                values = {"host": "http://flaresolverr:8191/", "requestTimeout": 60}
                for field in payload.get("fields", []):
                    if field.get("name") in values:
                        field["value"] = values[field["name"]]
                # The proxy only routes indexers tagged 'cloudflare'.
                tag_id = None
                status, _, tags = _http(PROWLARR_BASE, "/api/v1/tag",
                                        headers={"X-Api-Key": key})
                if status == 200 and isinstance(tags, list):
                    for tag in tags:
                        if tag.get("label") == "cloudflare":
                            tag_id = tag.get("id")
                            break
                if tag_id is None:
                    status, _, created = _http(PROWLARR_BASE, "/api/v1/tag",
                                               method="POST",
                                               body={"label": "cloudflare"},
                                               headers={"X-Api-Key": key})
                    if status in (200, 201) and isinstance(created, dict):
                        tag_id = created.get("id")
                payload["name"] = "FlareSolverr"
                payload["tags"] = [tag_id] if tag_id else []
                payload["enable"] = True
                st = prowlarr_post("/api/v1/indexerproxy", payload, key)
                if st in (200, 201):
                    _log("Prowlarr: FlareSolverr proxy added - tag an indexer "
                         "'cloudflare' to route it through the proxy.")
                else:
                    _issues.append(f"prowlarr: adding FlareSolverr proxy failed (HTTP {st})")
    except Exception as exc:  # noqa: BLE001 - best effort
        _issues.append(f"prowlarr: FlareSolverr proxy setup failed: {exc}")

    _results["prowlarr"] = "configured"
    return True


# ---------------------------------------------------------------------------
# qBittorrent
# ---------------------------------------------------------------------------

def _interface_ipv4(name: str):
    """(address, netmask) of one IPv4 interface — asked of the kernel, no tools."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        packed = struct.pack("256s", name[:15].encode())
        # SIOCGIFADDR / SIOCGIFNETMASK; the IPv4 address sits at byte 20.
        address = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, packed)[20:24])
        netmask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x891b, packed)[20:24])
    return address, netmask


def network_shared_with(peer: str) -> str:
    """The CIDR of the network that reaches `peer`, or '' if it cannot be told.

    Used to tell qBittorrent which subnet to trust, so the SSO gateway's calls
    arrive without a second login. Discovered rather than configured: Docker
    allocates the subnet (`172.18.0.0/16` on the current host) and init already
    resolves `peer` by name on that same network, so the answer is read off the
    interface that contains the peer's address - a literal would silently stop
    matching on the next host to build this stack.
    """
    try:
        peer_ip = socket.gethostbyname(peer)
    except OSError:
        return ""
    target = ipaddress.ip_address(peer_ip)
    for _index, name in socket.if_nameindex():
        try:
            address, netmask = _interface_ipv4(name)
        except OSError:
            continue
        try:
            network = ipaddress.ip_network(f"{address}/{netmask}", strict=False)
        except ValueError:
            continue
        if target in network:
            return str(network)
    return ""


@arrived("qBittorrent setup")
def configure_qbittorrent():
    _log("--- qBittorrent ---")
    if not wait_for(QBT_BASE, "/api/v2/app/version", "qBittorrent WebUI"):
        return False

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    status, text, _ = _http(QBT_BASE, "/api/v2/auth/login", method="POST",
                            body={"username": USER, "password": PASS},
                            opener=opener, raw_form=True)
    # qBittorrent >= 5.2 returns 204 with an empty body on success (older
    # versions returned 200 with "Ok."). Either means the cookie is valid.
    if (status in (200, 204) and text.strip() in ("", "Ok.")):
        _log("qBittorrent WebUI login with the shared credentials: OK")
    else:
        _issues.append("qBittorrent WebUI login failed with the shared credentials. "
                       "The PBKDF2 hash may not match this qBittorrent version - grab the "
                       "temporary password from `docker logs qbittorrent` and change it in "
                       "the WebUI (Tools > Options > Web UI), then re-run monarch-init.")
        _log("WARNING: qBittorrent login failed - categories NOT created.")
        return False

    # ── Categories: create what is missing, correct what has drifted ────────
    # Reconciled rather than create-only. A category is not "done" because the
    # name exists: `lidarr` existed and pointed at /data/media/music, which is
    # how the downloads-in-the-library-root warning survived every run. A name
    # the manifest does not carry is pruned for the same reason - it is either a
    # duplicate of one it does (`radarr` alongside `movies`) or a stray whose
    # path sits inside a library, and either way the apps are told the
    # manifest's names, so a stray can only mislead.
    status, _, existing = _http(QBT_BASE, "/api/v2/torrents/categories", opener=opener)
    live = existing if status == 200 and isinstance(existing, dict) else {}
    for cat, save_path in QBT_CATEGORIES.items():
        current = (live.get(cat) or {}).get("savePath")
        if current == save_path:
            continue
        if cat in live:
            verb, path = "editCategory", f"'{cat}' {current!r} -> {save_path!r}"
        else:
            verb, path = "createCategory", f"'{cat}' -> {save_path!r}"
        # The WebUI API takes form-encoded params, not a JSON body.
        st, _, _ = _http(QBT_BASE, f"/api/v2/torrents/{verb}", method="POST",
                         body={"category": cat, "savePath": save_path},
                         opener=opener, raw_form=True)
        if st in (200, 201):
            _log(f"qBittorrent category {path}")
        else:
            _issues.append(f"qBittorrent: category '{cat}' could not be set (HTTP {st})")
    # A stray that still files torrents is reported, never removed: qBittorrent
    # strips the category from every torrent under it, so removing one first
    # leaves those downloads unlabelled and invisible to the app that queued
    # them - still seeding, gone from its queue. Moving them is a decision (the
    # app that queued them is what says where they belong), and it is the host
    # side's job: scripts/arr-download-categories.py knows which app sent what.
    strays = sorted(set(live) - set(QBT_CATEGORIES))
    if strays:
        st, _, torrents = _http(QBT_BASE, "/api/v2/torrents/info", opener=opener)
        filing: dict[str, int] = {}
        if st == 200 and isinstance(torrents, list):
            for torrent in torrents:
                cat = torrent.get("category") or ""
                if cat in strays:
                    filing[cat] = filing.get(cat, 0) + 1
        removable = [cat for cat in strays if cat not in filing]
        kept = [cat for cat in strays if cat in filing]
        if removable:
            st, _, _ = _http(QBT_BASE, "/api/v2/torrents/removeCategories", method="POST",
                             body={"categories": "\n".join(removable)},
                             opener=opener, raw_form=True)
            if st in (200, 201, 204):
                _log(f"qBittorrent categories removed (not in the manifest): "
                     f"{', '.join(removable)}")
            else:
                _issues.append("qBittorrent: stray categories could not be removed "
                               f"({', '.join(removable)}) - HTTP {st}")
        if kept:
            _log(f"qBittorrent categories kept, still filing torrents: {', '.join(kept)}")
            _issues.append(
                f"qBittorrent: {', '.join(kept)} are not in the manifest but still file "
                f"torrents; run scripts/arr-download-categories.py to move those downloads "
                f"to the categories the apps send (removing the category first would strip "
                f"them from the apps' queues)")

    # Default save path + no temp dir so category paths are used as-is.
    # setPreferences takes its settings as a `json` form field.
    prefs = {"save_path": "/data/torrents", "temp_path_enabled": False}
    # ── Cerulean SSO is the only door ───────────────────────────────────────
    # qBittorrent keeps a password of its own (monarch-seed writes it, and
    # drift-check logs in with it), so a user who reaches the WebUI through
    # qbittorrent-sso still meets a SECOND login: the one the app asks for. The
    # WebUI is published on loopback only and the gateway is the sole route to
    # it, so the app can be told to trust the subnet the gateway calls from and
    # never ask. The subnet is discovered, not configured: Docker picks it, and a
    # literal here would stop matching on the next host to build the stack.
    trust = network_shared_with("qbittorrent")
    if trust:
        prefs["bypass_auth_subnet_whitelist_enabled"] = True
        prefs["bypass_auth_subnet_whitelist"] = trust
    else:
        _issues.append("qBittorrent: could not discover the subnet the SSO gateway "
                       "shares with it, so the WebUI still asks for its own "
                       "password behind Cerulean")
    st, _, _ = _http(QBT_BASE, "/api/v2/app/setPreferences", method="POST",
                     body={"json": json.dumps(prefs)}, opener=opener, raw_form=True)
    if st in (200, 204):
        _log(f"qBittorrent default save path set to /data/torrents"
             + (f", WebUI auth bypassed for {trust} (the SSO gateway's subnet)"
                if trust else ""))
    else:
        _issues.append(f"qBittorrent: setPreferences failed (HTTP {st})")

    _results["qbittorrent"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Bazarr (best effort)
# ---------------------------------------------------------------------------

def bazarr_api_key() -> str:
    """Bazarr's API key from its config.yaml (auth section)."""
    try:
        with open(f"{APPDATA}/bazarr/config/config.yaml", "r",
                  encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"^\s*apikey:\s*(\S+)\s*$", text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return ""


@arrived("bazarr setup")
def configure_bazarr():
    _log("--- Bazarr ---")
    if not wait_for(BAZARR_BASE, "/api/system/status", "Bazarr"):
        return False

    key = bazarr_api_key()
    if not key:
        _issues.append("bazarr: API key not found in config.yaml - configure manually")
        return False
    auth_hdr = {"X-API-KEY": key}

    status, _, settings = _http(BAZARR_BASE, "/api/system/settings", headers=auth_hdr)
    if status != 200 or not isinstance(settings, dict):
        _issues.append("bazarr: /api/system/settings unreachable - configure manually")
        _log("WARNING: Bazarr settings are not readable via API yet - manual setup needed.")
        return False

    auth = settings.get("auth", {}) or {}
    _log("Bazarr local login: %s" % (auth.get("type") or "none"))

    # Bazarr's settings API takes form fields named settings-<section>-<key>;
    # the password is stored MD5-hashed by the server. `settings-auth-type` is
    # deliberately NOT sent: bazarr.<MONARCH_DOMAIN> already carries the
    # Cerulean Authentik auth_request gate, so Bazarr must keep its default of
    # no local login rather than gaining a second one. The API cannot express
    # "no auth" anyway - it accepts only None/basic/form, and rejects an empty
    # or "none" value with HTTP 406 - so the field is left untouched.
    form = {
        "settings-general-use_sonarr": "true",
        "settings-general-use_radarr": "true",
    }
    sonarr_key = api_key_for("sonarr")
    radarr_key = api_key_for("radarr")
    if sonarr_key:
        form.update({
            "settings-sonarr-ip": "sonarr",
            "settings-sonarr-port": "8989",
            "settings-sonarr-apikey": sonarr_key,
            "settings-sonarr-ssl": "false",
            "settings-sonarr-base_url": "/",
        })
    if radarr_key:
        form.update({
            "settings-radarr-ip": "radarr",
            "settings-radarr-port": "7878",
            "settings-radarr-apikey": radarr_key,
            "settings-radarr-ssl": "false",
            "settings-radarr-base_url": "/",
        })

    st, _, _ = _http(BAZARR_BASE, "/api/system/settings", method="POST",
                     body=form, headers=auth_hdr, raw_form=True)
    if st in (200, 201, 202, 204):
        _log("Bazarr: local login off + Sonarr/Radarr connections saved via API.")
        _results["bazarr"] = "configured"
        return True
    _issues.append(f"bazarr: settings could not be saved via API (HTTP {st}) - configure manually")
    return False


# ---------------------------------------------------------------------------
# Jellyseerr (best effort)
# ---------------------------------------------------------------------------

def _jellyseerr_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _jellyseerr_login(opener):
    """Sign in via Jellyfin (creates the admin user + Jellyfin connection on
    first run and sets the session cookie)."""
    # Once the Jellyfin connection is stored, /auth/jellyfin rejects a
    # hostname with 500 "Jellyfin hostname already configured" - try with it
    # first (first run), then without it (subsequent runs).
    bodies = ({
        "username": USER, "password": PASS,
        "hostname": "jellyfin", "port": 8096, "useSsl": False,
        "urlBase": "", "email": "admin@innotel.us",
        "serverType": 2,  # MediaServerType.JELLYFIN
    }, {
        "username": USER, "password": PASS,
        "email": "admin@innotel.us",
        "serverType": 2,
    })
    for body in bodies:
        status, _, j = _http(JELLYSEERR_BASE, "/api/v1/auth/jellyfin",
                             method="POST", body=body, opener=opener)
        if status == 200 and isinstance(j, dict) and bool(j.get("id")):
            return True
    return False


def _jellyseerr_enable_jellyfin_login(opener):
    """Settings -> Users: let subscribers sign in with their Jellyfin accounts."""
    # /api/v1/settings/public needs no auth. When Seerr is already initialized
    # AND Jellyfin sign-in is on, there is nothing to do - skip the credential
    # POST entirely. monarch-init runs from the drift-check timer's --heal, so
    # this path fires on a schedule; logging in with the admin password every
    # cycle wrote failed-sign-in noise into Seerr's log (and once a password
    # rotation lands, regular 401s) for zero effect.
    status, _, public = _http(JELLYSEERR_BASE, "/api/v1/settings/public")
    if (status == 200 and isinstance(public, dict)
            and public.get("initialized") and public.get("mediaServerLogin")):
        _log("Jellyseerr: already initialized with Jellyfin sign-in enabled.")
        return True
    if not _jellyseerr_login(opener):
        _issues.append("jellyseerr: admin login failed - Jellyfin sign-in was not "
                       "enabled (set it under Settings -> Users).")
        return False
    status, _, main = _http(JELLYSEERR_BASE, "/api/v1/settings/main", opener=opener)
    if status != 200 or not isinstance(main, dict):
        _issues.append("jellyseerr: /settings/main unreachable - Jellyfin sign-in "
                       "was not enabled.")
        return False
    if main.get("mediaServerLogin"):
        _log("Jellyseerr: Jellyfin sign-in already enabled.")
        return True
    main["mediaServerLogin"] = True
    status, _, _ = _http(JELLYSEERR_BASE, "/api/v1/settings/main",
                         method="PUT", body=main, opener=opener)
    if status in (200, 201, 204):
        _log("Jellyseerr: enabled Jellyfin sign-in under Settings -> Users.")
        return True
    _issues.append(f"jellyseerr: enabling Jellyfin sign-in failed (HTTP {status}).")
    return False


@arrived("jellyseerr setup")
def configure_jellyseerr():
    _log("--- Jellyseerr ---")
    if not wait_for(JELLYSEERR_BASE, "/api/v1/status", "Jellyseerr"):
        return False

    opener = _jellyseerr_opener()
    status, _, pub = _http(JELLYSEERR_BASE, "/api/v1/settings/public", opener=opener)
    if status == 200 and isinstance(pub, dict) and pub.get("initialized"):
        _log("Jellyseerr already initialized - skipped (it keeps its settings).")
        _jellyseerr_enable_jellyfin_login(opener)
        _results["jellyseerr"] = "already initialized"
        return True

    # Sign in via Jellyfin: this creates the admin user (id=1) and stores the
    # Jellyfin connection (host/port/api key) in one call. The endpoint needs
    # the Jellyfin server itself, so Jellyfin must be set up first.
    if not _jellyseerr_login(opener):
        _issues.append("jellyseerr: could not sign in via Jellyfin (check the "
                       "Jellyfin admin credentials) - finish the wizard manually "
                       "at http://<host>:5055.")
        return False
    _log("Jellyseerr: signed in via Jellyfin (admin user + connection set).")

    # With the admin session in place, mark the setup wizard as complete.
    status, _, _ = _http(JELLYSEERR_BASE, "/api/v1/settings/initialize",
                         method="POST", body={}, opener=opener)
    if status not in (200, 201, 204):
        _issues.append(f"jellyseerr: /settings/initialize failed (HTTP {status}) - "
                       "finish the wizard manually at http://<host>:5055.")
        return False
    _log("Jellyseerr initialized against Jellyfin.")

    # Connect Radarr + Sonarr so requests land in the *arr apps.
    for app, impl in (({"svc": "radarr", "port": 7878, "api": "v3"}, "radarr"),
                      ({"svc": "sonarr", "port": 8989, "api": "v3"}, "sonarr")):
        key = api_key_for(app["svc"])
        if not key:
            continue
        base = monarch_app_base(app["svc"], app["port"])
        api = app["api"]
        profile = root_dir = None
        st, _, j = _http(base, f"/api/{api}/qualityprofile", headers={"X-Api-Key": key})
        if st == 200 and isinstance(j, list) and j:
            profile = j[0]
        st, _, j = _http(base, f"/api/{api}/rootfolder", headers={"X-Api-Key": key})
        if st == 200 and isinstance(j, list) and j:
            root_dir = j[0].get("path")
        if not profile or not root_dir:
            _issues.append(f"jellyseerr: {app['svc']} profile/root folder not found - "
                           "connect it manually in Jellyseerr settings.")
            continue

        body = {
            "name": app["svc"].capitalize(),
            "hostname": app["svc"],
            "port": app["port"],
            "apiKey": key,
            "useSsl": False,
            "baseUrl": "",
            "activeProfileId": profile["id"],
            "activeProfileName": profile["name"],
            "activeDirectory": root_dir,
            "is4k": False,
            "minimumAvailability": "released",
            "tags": [],
            "externalUrl": f"http://localhost:{app['port']}",
            "syncEnabled": True,
            "preventSearch": False,
        }
        if impl == "sonarr":
            st, _, langs = _http(base, f"/api/{api}/languageprofile", headers={"X-Api-Key": key})
            if st == 200 and isinstance(langs, list) and langs:
                body["activeLanguageProfileId"] = langs[0]["id"]
        st, _, _ = _http(JELLYSEERR_BASE, f"/api/v1/settings/{impl}",
                         method="POST", body=body, opener=opener)
        if st in (200, 201, 204):
            _log(f"Jellyseerr: {impl} connected (profile '{profile['name']}' -> {root_dir}).")
        else:
            _issues.append(f"jellyseerr: connecting {impl} failed (HTTP {st})")

    _jellyseerr_enable_jellyfin_login(opener)
    _results["jellyseerr"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pin_summary() -> list:
    """The pinned plugin builds, for the invariants manifest.

    Never fatal: an unreadable pin file is already reported by the steps that
    install from it, and a manifest that could not be written would hide every
    other invariant from the drift check.
    """
    try:
        return [{"name": p["name"], "repo": p["repo"], "tag": p.get("tag", ""),
                 "version": p.get("version", ""), "assembly": p["assembly"],
                 "plugin_dir": p["plugin_dir"],
                 "assembly_sha256": p["assembly_sha256"]}
                for p in load_plugin_pins()]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def build_invariants() -> dict:
    """The invariants monarch-init is supposed to maintain, as data.

    Written to /docker/appdata/init/invariants.json so scripts/drift-check.sh
    asserts against EXACTLY what monarch-init configures - one source of
    truth means the two can never drift apart. Ports are the host-published
    ones (drift-check probes localhost).
    """
    arr_apps = []
    for app in MONARCH_APPS:
        arr_apps.append({
            "svc": app["svc"],
            "port": PORTS[app["svc"]],
            "api": app["api"],
            "category": app["category"],
            "media": app["media"],
            "root_folder": f"/data/media/{app['media']}",
        })
    return {
        "version": 1,
        "arr_apps": arr_apps,
        # The names every one of those apps must answer, as written by init and
        # checkable from the host (scripts/arr-allowed-hosts.py --check).
        "arr_allowed_hosts": arr_allowlist(),
        "prowlarr": {
            "port": PORTS["prowlarr"],
            "apps": [PROWLARR_APP_IMPLS[app["svc"]] for app in MONARCH_APPS],
            "download_client": "QBittorrent",
        },
        "qbt": {
            "port": PORTS["qbt"],
            "categories": sorted(QBT_CATEGORIES.keys()),
            # name -> save path, so the host side (scripts/arr-download-
            # categories.py) reconciles the same map init just applied instead
            # of carrying a second copy of it. A category whose NAME is right
            # and whose PATH is a library root is the failure that went
            # unnoticed - both halves have to be checkable.
            "category_paths": QBT_CATEGORIES,
            "save_path": "/data/torrents",
            # The WebUI's own password is never the door: qbittorrent-sso is, and
            # the app trusts the subnet the gateway calls from. Asserted live by
            # drift-check (a whitelist that got emptied puts the second login
            # back in front of every user).
            "sso_bypass": True,
        },
        "jellyfin": {
            "port": PORTS["jellyfin"],
            "libraries": [lib["name"] for lib in JELLYFIN_LIBRARIES],
            # The SSO button on Jellyfin's own login page: which provider it
            # offers, and which build of each plugin provides it. drift-check
            # judges the wiring live (jellyfin-oidc-sso.py,
            # jellyfin-plugin-pin.py); these are what it was configured from.
            "oidc_provider": OIDC_PROVIDER_ID,
            "oidc_client_id": MONARCH_SSO_CLIENT_ID,
            "plugin_pins": _pin_summary(),
        },
        "jellyseerr": {
            "port": PORTS["jellyseerr"],
            # Who owns it (Seerr's Owner is row id 1). Asserted live by
            # drift-check through scripts/seerr-owner.py --check.
            "owner": SEERR_OWNER,
        },
        # Bazarr keeps no local login: the Cerulean Authentik gate on
        # bazarr.<domain> is the only one (drift-check asserts exactly that).
        "bazarr": {"port": PORTS["bazarr"], "auth_type": "none"},
        "authentik": {"ldap_outpost": LDAP_OUTPOST_NAME},
    }


def main() -> int:
    _log(f"monarch-init starting (user '{USER}').")
    _log("Timeout for each service: up to 15 minutes on first boot while images start.")

    configure_jellyfin()
    configure_authentik_ldap()
    configure_jellyfin_ldap()
    configure_jellyfin_oidc()
    configure_livetv()
    configure_monarch_apps()
    configure_prowlarr()
    configure_qbittorrent()
    configure_bazarr()
    configure_jellyseerr()

    try:
        os.makedirs(INIT_DIR, exist_ok=True)
        with open(os.path.join(INIT_DIR, "status.json"), "w", encoding="utf-8") as fh:
            json.dump({"user": USER, "results": _results, "issues": _issues}, fh, indent=2)
        ensure_owner(os.path.join(INIT_DIR, "status.json"))
        with open(os.path.join(INIT_DIR, "invariants.json"), "w", encoding="utf-8") as fh:
            json.dump(build_invariants(), fh, indent=2)
        ensure_owner(os.path.join(INIT_DIR, "invariants.json"))
        _log(f"invariants manifest written to {INIT_DIR}/invariants.json")
    except OSError as exc:
        _log(f"WARNING: could not write {INIT_DIR}/status.json or invariants.json: {exc}")

    _log("=" * 60)
    _log("SUMMARY")
    for svc, state in _results.items():
        _log(f"  {svc:16s} {state}")
    if _issues:
        _log("")
        _log("MANUAL ACTIONS NEEDED:")
        for issue in _issues:
            _log(f"  * {issue}")
    else:
        _log("")
        _log("Everything the automation could reach is configured. Remaining manual "
             "steps are only the external ones: Stripe keys/domains in .env, "
             "indexers in Prowlarr, and adding paid users/groups in Authentik.")
    _log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())