#!/usr/bin/env python3
"""
monarch-init: one-shot automatic wiring of the whole Monarch media platform.

Runs once after `docker compose up -d` (container monarch-init, image
python:3.12-slim, stdlib only - no pip packages needed). It configures:

  * Jellyfin      - first-run wizard (creates the admin user with the shared
                    credentials), adds media libraries, logs in and exports
                    the admin token (usable as an API key) to
                    /docker/appdata/init/jellyfin-api-key.txt
  * Sonarr/Radarr/
    Lidarr/Whisparr - forms authentication with the shared credentials, root
                    folder, qBittorrent download client, hardlink settings
  * Prowlarr      - forms authentication, qBittorrent download client,
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
  * jellyfin-api-key.txt  - Jellyfin admin token (use as JELLYFIN_API_KEY in
                            .env for the subscription platform)
  * status.json           - per-service result of the last run
"""

import base64
import json
import os
import re
import sqlite3
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

# Authentik connection (used to provision the LDAP outpost Jellyfin logins
# authenticate against, and the paid_users access gate).
AUTHENTIK_BASE_URL = os.environ.get("AUTHENTIK_BASE_URL", "http://authentik-server:9000").strip().rstrip("/")
AUTHENTIK_BOOTSTRAP_TOKEN = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "").strip()

# Jellyfin LDAP-Auth plugin (authenticates logins against the Authentik LDAP
# outpost). The bind user/token/group/base DN must match what Magnate
# provisions in Authentik (same defaults in docker-compose.yml).
LDAP_PLUGIN_NAME = "LDAP-Auth"
LDAP_PLUGIN_CATALOG_REPO = "https://repo.jellyfin.org/files/plugin/manifest.json"
LDAP_PLUGIN_GH_RELEASES = "https://api.github.com/repos/jellyfin/jellyfin-plugin-ldapauth/releases/latest"
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
LDAP_SEARCH_ROLE = "jellyfin-ldap-search"
LDAP_BIND_FLOW_SLUG = "default-provider-authorization-implicit-consent"
LDAP_INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"

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


def ak_ensure_service_account(username: str):
    """Ensure the LDAP bind service account exists; return (user, token_identifier)."""
    existing = ak_find_user(username=username)
    if existing:
        return existing, f"service-account-{username}-password"
    status, _, j = ak_request("POST", "/core/users/service_account/", body={
        "name": username, "create_group": False, "expiring": False})
    if status not in (200, 201):
        raise RuntimeError(f"service account creation failed ({status})")
    return j, f"service-account-{username}-password"


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


def ak_role_add_user(role_pk: str, user_pk: int) -> bool:
    status, _, _ = ak_request("POST", f"/rbac/roles/{role_pk}/add_user/",
                              body={"pk": int(user_pk)})
    return status in (200, 204)


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

    sa, bind_token_id = ak_ensure_service_account(LDAP_BIND_USER)
    sa_pk = sa.get("user_pk") or sa.get("pk")
    ak_ensure_token(bind_token_id, sa_pk, LDAP_BIND_TOKEN,
                    "Jellyfin LDAP bind user (monarch stack)")

    provider = ak_ensure_ldap_provider()
    provider_pk = provider.get("pk")
    ak_ensure_application(provider_pk)

    outpost = ak_ensure_outpost(provider_pk)
    outpost_pk = outpost.get("pk")
    # The outpost's own API token (auto-created with the outpost) is pinned
    # to the value the `authentik-ldap` container uses as AUTHENTIK_TOKEN.
    ak_ensure_token(f"ak-outpost-{outpost_pk}-api", None, LDAP_OUTPOST_TOKEN,
                    "LDAP outpost API token (monarch stack)", intent="api")

    role = ak_ensure_role(LDAP_SEARCH_ROLE)
    role_pk = role.get("pk")
    ok = ak_role_assign_permission(
        role_pk, "authentik_providers_ldap.search_full_directory",
        model="authentik_providers_ldap.ldapprovider", object_pk=provider_pk)
    if not ok:
        # Some versions only expose it as a global permission.
        ak_role_assign_permission(role_pk, "authentik_providers_ldap.search_full_directory")
    ak_role_add_user(role_pk, sa_pk)

    admin_group = ak_group(LDAP_ADMIN_GROUP)
    _log(f"LDAP provisioning OK: provider={provider_pk} outpost={outpost_pk} "
         f"bind={LDAP_BIND_USER} group={LDAP_BIND_GROUP} "
         f"admin_group={admin_group.get('name')}")
    _results["authentik-ldap"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------

def jellyfin_wizard_pending() -> bool:
    status, _, _ = _http(JELLYFIN_BASE, "/Startup/Configuration")
    if status == 200:
        return True
    # Fallback: some versions only report it via the public info endpoint;
    # a completed wizard no longer exposes the /Startup endpoints.
    status, _, j = _http(JELLYFIN_BASE, "/System/Info/Public")
    if status == 200 and isinstance(j, dict):
        return bool(j.get("StartupWizardCompleted", True)) is False
    return False


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

    # Log in and keep the admin token as the de-facto API key.
    auth_header = (
        'MediaBrowser Client="Monarch Init", Device="Linux", '
        'DeviceId="monarch-init-001", Version="1.0.0"'
    )
    status, text, j = _http(
        JELLYFIN_BASE, "/Users/AuthenticateByName", method="POST",
        body={"Username": USER, "Pw": PASS},
        headers={"X-Emby-Authorization": auth_header},
    )
    token = None
    if status in (200, 201) and isinstance(j, dict):
        token = j.get("AccessToken")
    if not token:
        # A wizard marked complete with zero users is a dead end: reset the
        # flag so the next Jellyfin start re-runs the first-run wizard (and
        # our flow above creates the admin).
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
                _issues.append(
                    "Jellyfin had no users while the wizard was marked complete - "
                    "set IsStartupWizardCompleted=false in system.xml, restart "
                    "jellyfin, then re-run monarch-init."
                )
        else:
            _issues.append("Could not log in to Jellyfin with the shared credentials "
                           "(check the Jellyfin admin user, then set JELLYFIN_API_KEY manually)")
            _log("WARNING: Jellyfin login failed - export the Jellyfin API key manually.")
        return False

    key_file = os.path.join(INIT_DIR, "jellyfin-api-key.txt")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(token)
    ensure_owner(key_file)
    _log("Exported Jellyfin admin token -> " + key_file)
    _log("Set JELLYFIN_API_KEY in .env to the contents of that file (used by the "
         "subscription platform).")

    # Add the media libraries (read-only media mount is fine - metadata lives in
    # the Jellyfin config volume).
    for lib in JELLYFIN_LIBRARIES:
        status, _, _ = _http(
            JELLYFIN_BASE, "/Library/VirtualFolders", method="POST",
            body={
                "LibraryOptions": {"EnableInternetProviders": True},
                "CollectionType": lib["type"],
                "Name": lib["name"],
                "RefreshLibrary": False,
                "Paths": [lib["path"]],
            },
            headers={"X-Emby-Token": token},
        )
        if status in (200, 204):
            _log(f"Added Jellyfin library '{lib['name']}' -> {lib['path']}")
        else:
            _issues.append(f"Jellyfin library '{lib['name']}' could not be added (HTTP {status})")

    _results["jellyfin"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Jellyfin LDAP-Auth plugin (Authentik login gate)
# ---------------------------------------------------------------------------


def jellyfin_headers(token):
    return {"X-Emby-Token": token}


def jellyfin_plugin_installed(token) -> bool:
    status, _, j = _http(JELLYFIN_BASE, "/Plugins", headers=jellyfin_headers(token))
    if status == 200 and isinstance(j, list):
        return any(p.get("Name") == LDAP_PLUGIN_NAME for p in j)
    return False


def install_ldap_plugin_via_catalog(token) -> bool:
    """Try the official Jellyfin plugin catalog (best effort)."""
    repo = urllib.parse.quote(LDAP_PLUGIN_CATALOG_REPO, safe="")
    status, _, entries = _http(
        JELLYFIN_BASE, f"/Plugins/Repository?repositoryUrl={repo}",
        headers=jellyfin_headers(token))
    if status != 200 or not isinstance(entries, list):
        return False
    entry = next((e for e in entries if e.get("Name") == LDAP_PLUGIN_NAME), None)
    if not entry:
        return False
    body = {"Name": entry.get("Name"), "Version": entry.get("Version"),
            "RepositoryUrl": LDAP_PLUGIN_CATALOG_REPO}
    status, _, _ = _http(
        JELLYFIN_BASE, f"/Plugins/Install?repositoryUrl={repo}", method="POST",
        body=body, headers=jellyfin_headers(token))
    return status in (200, 202, 204)


def install_ldap_plugin_via_release(token) -> bool:
    """Fallback: download the plugin zip straight from the GitHub release."""
    try:
        with urllib.request.urlopen(LDAP_PLUGIN_GH_RELEASES, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING: could not query GitHub for the LDAP plugin release: {exc}")
        return False
    asset = next((a for a in release.get("assets", []) if a.get("name", "").endswith(".zip")), None)
    if not asset:
        _log("WARNING: no .zip asset on the LDAP plugin GitHub release.")
        return False
    plugins_dir = os.path.join(APPDATA, "jellyfin", "config", "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    tmp = os.path.join(plugins_dir, asset["name"])
    try:
        urllib.request.urlretrieve(asset["browser_download_url"], tmp)
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(plugins_dir)
        os.remove(tmp)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING: LDAP plugin download/extract failed: {exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


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
  <PasswordResetUrl>http://localhost:9000/if/user/</PasswordResetUrl>
</PluginConfiguration>
"""
    path = os.path.join(APPDATA, "jellyfin", "config", "plugins",
                        LDAP_PLUGIN_NAME, f"{LDAP_PLUGIN_NAME}.xml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return path, xml


def _config_unchanged(path: str, xml: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read() == xml
    except OSError:
        return False


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
    if not LDAP_BIND_TOKEN:
        _issues.append("jellyfin-ldap: AUTHENTIK_LDAP_BIND_TOKEN is not set in docker-compose.yml")
        return False

    path, xml = write_ldap_plugin_config()
    _log(f"LDAP-Auth plugin config written -> {path}")

    needs_restart = not _config_unchanged(path, xml)
    if not jellyfin_plugin_installed(token):
        _log("Installing the LDAP-Auth plugin (catalog, then GitHub release)...")
        ok = install_ldap_plugin_via_catalog(token)
        if not ok:
            ok = install_ldap_plugin_via_release(token)
        if not ok:
            _issues.append("jellyfin-ldap: could not install the LDAP-Auth plugin automatically - "
                           "install it in Jellyfin Dashboard > Plugins > Catalog (name: LDAP-Auth). "
                           "The config file is already in place.")
            return False
        needs_restart = True
        _log("LDAP-Auth plugin installed.")
    else:
        _log("LDAP-Auth plugin already installed.")

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
# Free US guide sources with both streams in the iptv-org playlist and EPG
# data. Add more site names (see https://github.com/iptv-org/epg/tree/master/sites)
# to grow the guide; the channel list is re-assembled on first boot.
LIVETV_EPG_SITES = ["xumo.tv", "i24news.tv"]
LIVETV_EPG_RAW = ("https://raw.githubusercontent.com/iptv-org/epg/master/"
                  "sites/{site}/{site}.channels.xml")


def _fetch_livetv_channels():
    """Assemble an EPG channel list from iptv-org's per-site files.

    Returns XML text, or None if every site fetch failed. Entries without an
    xmltv_id are dropped (no guide data) and duplicates are removed.
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
            if key and key.group(1) in seen:
                continue
            if key:
                seen.add(key.group(1))
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


def set_monarch_app_auth(base, api, key):
    status, _, j = _http(base, f"/api/{api}/config/host", headers={"X-Api-Key": key})
    if status != 200 or not isinstance(j, dict):
        return False, "config/host unreachable"
    if j.get("authenticationMethod") not in (None, "", "none"):
        return True, "auth already configured"
    j["authenticationMethod"] = "forms"
    j["username"] = USER
    j["password"] = PASS
    # Newer *arr versions require passwordConfirmation to match password or
    # the PUT is rejected with HTTP 400 (and an empty body).
    j["passwordConfirmation"] = PASS
    status, _, _ = _http(base, f"/api/{api}/config/host", method="PUT", body=j,
                         headers={"X-Api-Key": key})
    if status in (200, 202):
        return True, "forms auth set"
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


def ensure_qbt_client(base, api, key, category):
    """Add (or confirm) the qBittorrent download client via the schema."""
    status, _, j = _http(base, f"/api/{api}/downloadclient", headers={"X-Api-Key": key})
    if status == 200 and isinstance(j, list):
        for client in j:
            if client.get("implementation") == "QBittorrent":
                return True, "exists"

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
        "category": category,
        "urlBase": "",
    }
    for field in payload.get("fields", []):
        name = field.get("name")
        if name in values:
            field["value"] = values[name]
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
    if status == 200 and isinstance(j, dict) and j.get("authenticationMethod") in (None, "none", ""):
        j["authenticationMethod"] = "forms"
        j["username"] = USER
        j["password"] = PASS
        # Newer versions require passwordConfirmation to match password or
        # the PUT is rejected with HTTP 400.
        j["passwordConfirmation"] = PASS
        status, _, _ = _http(PROWLARR_BASE, "/api/v1/config/host", method="PUT",
                             body=j, headers={"X-Api-Key": key})
        _log(f"Prowlarr: forms auth set (HTTP {status})")
    else:
        _log("Prowlarr: auth already configured")

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

    # Categories matching each *arr root folder (hardlinks-friendly layout).
    categories = {
        "movies": "/data/torrents/movies",
        "tv": "/data/torrents/tv",
        "music": "/data/torrents/music",
        "xxx": "/data/torrents/xxx",
    }
    status, _, existing = _http(QBT_BASE, "/api/v2/torrents/categories", opener=opener)
    if status == 200 and isinstance(existing, dict):
        have = set(existing.keys())
    else:
        have = set()
    for cat, save_path in categories.items():
        if cat in have:
            continue
        # The WebUI API takes form-encoded params, not a JSON body.
        st, _, _ = _http(QBT_BASE, "/api/v2/torrents/createCategory", method="POST",
                         body={"category": cat, "savePath": save_path},
                         opener=opener, raw_form=True)
        if st in (200, 201):
            _log(f"qBittorrent category '{cat}' -> {save_path}")
        else:
            _issues.append(f"qBittorrent: category '{cat}' could not be created (HTTP {st})")

    # Default save path + no temp dir so category paths are used as-is.
    # setPreferences takes its settings as a `json` form field.
    prefs = {"save_path": "/data/torrents", "temp_path_enabled": False}
    st, _, _ = _http(QBT_BASE, "/api/v2/app/setPreferences", method="POST",
                     body={"json": json.dumps(prefs)}, opener=opener, raw_form=True)
    if st in (200, 204):
        _log("qBittorrent default save path set to /data/torrents")
    else:
        _issues.append(f"qBittorrent: setPreferences failed (HTTP {st})")

    _results["qbittorrent"] = "configured"
    return True


# ---------------------------------------------------------------------------
# Bazarr (best effort)
# ---------------------------------------------------------------------------

@arrived("bazarr setup")
def configure_bazarr():
    _log("--- Bazarr ---")
    if not wait_for(BAZARR_BASE, "/api/system/status", "Bazarr"):
        return False

    status, _, settings = _http(BAZARR_BASE, "/api/system/settings")
    if status != 200 or not isinstance(settings, dict):
        _issues.append("bazarr: /api/system/settings unreachable - configure manually")
        _log("WARNING: Bazarr settings are not readable via API yet - manual setup needed.")
        return False

    general = settings.get("general", {}) or {}
    if general.get("auth") not in (None, "", "none"):
        _log("Bazarr already has authentication configured - skipped (re-run reset it).")
        return False

    general["username"] = USER
    general["password"] = PASS
    general["auth"] = "basic"

    sonarr_key = api_key_for("sonarr")
    radarr_key = api_key_for("radarr")
    if sonarr_key:
        settings["sonarr"] = [{
            "name": "Sonarr", "host": "sonarr", "port": 8989,
            "api_key": sonarr_key, "base_url": "", "ssl": False,
        }]
    if radarr_key:
        settings["radarr"] = [{
            "name": "Radarr", "host": "radarr", "port": 7878,
            "api_key": radarr_key, "base_url": "", "ssl": False,
        }]

    for method in ("PUT", "POST"):
        st, _, _ = _http(BAZARR_BASE, "/api/system/settings", method=method,
                         body=settings)
        if st in (200, 201, 202):
            _log(f"Bazarr: auth + Sonarr/Radarr connections saved (via {method}).")
            _results["bazarr"] = "configured"
            return True
    _issues.append("bazarr: settings could not be saved via API - configure manually")
    return False


# ---------------------------------------------------------------------------
# Jellyseerr (best effort)
# ---------------------------------------------------------------------------

def _jellyseerr_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _jellyseerr_login(opener):
    """Sign in as the admin user so the settings endpoints accept the session."""
    status, _, j = _http(JELLYSEERR_BASE, "/api/v1/auth/login", method="POST",
                         body={"username": USER, "password": PASS}, opener=opener)
    return status == 200 and isinstance(j, dict) and bool(j.get("id"))


def _jellyseerr_enable_jellyfin_login(opener):
    """Settings -> Users: let subscribers sign in with their Jellyfin accounts."""
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

    jellyfin_token = ""
    try:
        with open(os.path.join(INIT_DIR, "jellyfin-api-key.txt"), "r") as fh:
            jellyfin_token = fh.read().strip()
    except OSError:
        pass
    if not jellyfin_token:
        _issues.append("jellyseerr: no Jellyfin token available to connect - "
                       "finish Jellyfin setup first.")
        return False

    # mediaServerType: Jellyfin is 2 in the Jellyfin branch of jellyseerr and
    # 3 in some forks - try the idempotent one first, then the alternative.
    payload = {
        "username": USER,
        "password": PASS,
        "email": "admin@innotel.us",
        "main": {
            "name": "Jellyfin",
            "mediaServerType": 2,
            "url": "http://jellyfin:8096",
            "externalUrl": "http://localhost:8096",
            "apiKey": jellyfin_token,
        },
    }
    status = 0
    for server_type in (2, 3):
        payload["main"]["mediaServerType"] = server_type
        status, _, _ = _http(JELLYSEERR_BASE, "/api/v1/settings/initialize",
                             method="POST", body=payload, opener=opener)
        if status in (200, 201, 204):
            _log(f"Jellyseerr initialized against Jellyfin (mediaServerType={server_type}).")
            break
    if status not in (200, 201, 204):
        _issues.append("jellyseerr: /settings/initialize failed - finish the wizard "
                       "manually at http://<host>:5055, then connect Jellyfin.")
        return False

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

def main() -> int:
    _log(f"monarch-init starting (user '{USER}').")
    _log("Timeout for each service: up to 15 minutes on first boot while images start.")

    configure_jellyfin()
    configure_authentik_ldap()
    configure_jellyfin_ldap()
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
    except OSError as exc:
        _log(f"WARNING: could not write {INIT_DIR}/status.json: {exc}")

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