#!/usr/bin/env python3
"""verify-sso.py — prove Monarch's sign-in posture still holds on a live box.

The media apps are the hard case: Radarr, Sonarr, Lidarr, Whisparr, Bazarr,
Prowlarr, qBittorrent, SABnzbd and Jellyseerr ship nothing but a local username
and password form, and every one of them is switched to trust-the-proxy. So the
whole posture rests on two things holding at once, and both are asserted here:

  1. Each public name demands Cerulean Authentik. Every gateway is driven
     through a real authorization-code flow with a temporary Authentik identity,
     and the sealed session (`_innotel_sso`) must then open the app. This is what
     catches a gateway whose redirect_uri was never registered, or whose client
     secret was rotated without re-deploying it.
  2. The group check is real. The same flow with an identity that is *not* in
     SSO_REQUIRED_GROUP must be refused — either by Authentik (application bound
     to the group) or by the gateway (403). A gateway that admits every identity
     that can authenticate is a door, not a gate.
  3. The app ports are not a second door. With their own logins switched off,
     `<host>:7878` would be an unauthenticated media manager, so every app UI
     must answer on loopback and refuse on the host's LAN address. The shared
     session store is checked the same way.

The temporary identities are deleted on the way out, including when a check
fails. Nothing here is destructive: no container is started, stopped or edited.

Config (environment, falling back to this repo's .env):

    SSO_REQUIRED_GROUP          group that may sign in (default cerulean-platform)
    MONARCH_SSO_IDP             IdP origin (default https://auth.cerulean.innotel.us)
    AUTHENTIK_API_URL           API origin, default the IdP origin
    AUTHENTIK_BOOTSTRAP_TOKEN   Authentik API token (admin). Required.
    MONARCH_SSO_BASE            base domain (default MONARCH_DOMAIN,
                                else monarch.innotel.us)
    LAN_IP                      the host's LAN address (default: auto-detected)

Exit codes: 0 = pass, 1 = a check failed, 2 = cannot run (unconfigured or the
deployment is unreachable).

Usage:
    python3 scripts/verify-sso.py
    python3 scripts/verify-sso.py --verbose
"""

import argparse
import http.cookiejar
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FLOW = "default-authentication-flow"
MEMBER_USER = "e2e-monarch-sso"
OUTSIDER_USER = "e2e-monarch-outsider"
SESSION_COOKIE = "_innotel_sso"

# Public names this zone owns, in the order it is worth checking them. Every one
# is fronted by an oauth2-proxy gateway (radarr-sso … jellyseerr-sso) and every
# gateway is a client of the same Authentik application.
SUBDOMAINS = [
    ("radarr", "radarr.{base}"),
    ("sonarr", "sonarr.{base}"),
    ("lidarr", "lidarr.{base}"),
    ("whisparr", "whisparr.{base}"),
    ("bazarr", "bazarr.{base}"),
    ("prowlarr", "prowlarr.{base}"),
    ("qbittorrent", "qbittorrent.{base}"),
    ("sabnzbd", "sabnzbd.{base}"),
    # Jellyseerr answers on two names: the subscriber-facing request portal on
    # the apex domain, and this zone's alias for it.
    ("jellyseerr", "req.innotel.us"),
    ("jellyseerr (alias)", "req.{base}"),
]

# (label, port) — bound to 127.0.0.1 only. Every one of these apps is configured
# to trust the reverse proxy for identity, so a LAN answer here is precisely the
# hole this test exists to catch. qBittorrent's 6881 is deliberately not listed:
# that is the BitTorrent peer port, which has to stay reachable.
LAN_ONLY_PORTS = [
    ("radarr", 7878),
    ("sonarr", 8989),
    ("lidarr", 8686),
    ("whisparr", 6969),
    ("bazarr", 6767),
    ("prowlarr", 9696),
    ("qbittorrent", 8080),
    ("sabnzbd", 8082),
    ("jellyseerr", 5055),
]

# The shared oauth2-proxy session store (see the npm repo's compose.cerulean.yml)
# is published on the docker0 gateway only: every bridge reaches that, the LAN
# cannot.
SESSION_STORE_PORT = 16380
SESSION_STORE_HOST = "172.17.0.1"

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"


class CannotRun(Exception):
    """Configuration or reachability problem — exit 2, not a test failure."""


class CheckFailed(Exception):
    """An assertion about the deployment failed — exit 1."""


class IdpDenied(Exception):
    """Authentik rendered its "Permission denied" page instead of issuing a
    code: the identity authenticated but the application is not bound to it."""

    def __init__(self, body):
        super().__init__("Authentik refused the authorization")
        self.body = body


# ── config ─────────────────────────────────────────────────────────────────


def read_env_file():
    """Parse this repo's .env into a dict (ignores blanks and comments)."""
    vals = {}
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            vals[key.strip()] = val.strip().strip('"').strip("'")
    return vals


def detect_lan_ip():
    """The host's LAN address, as a LAN client would see it (no packets sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


class Config:
    def __init__(self, args):
        env_file = read_env_file()

        def pick(*names, default=""):
            for name in names:
                if os.environ.get(name):
                    return os.environ[name]
                if env_file.get(name):
                    return env_file[name]
            return default

        self.base = (args.base or pick("MONARCH_SSO_BASE", "MONARCH_DOMAIN",
                                       default="monarch.innotel.us")).strip("/")
        # The public IdP host, not AUTHENTIK_BASE_URL: this zone points that at
        # the internal address, and the flow must run where a browser would.
        self.idp = (args.idp or pick("MONARCH_SSO_IDP", default="")
                    or "https://auth.cerulean.innotel.us").rstrip("/")
        self.api = pick("AUTHENTIK_API_URL", default=self.idp).rstrip("/") + "/api/v3"
        self.token = pick("AUTHENTIK_BOOTSTRAP_TOKEN", "AUTHENTIK_API_TOKEN")
        self.group = pick("SSO_REQUIRED_GROUP", default="cerulean-platform")
        self.lan_ip = (args.host_ip or pick("LAN_IP") or detect_lan_ip())
        self.session_store_host = pick("DOCKER_BRIDGE_GATEWAY", default=SESSION_STORE_HOST)
        self.password = "E2e-Sso-" + os.urandom(6).hex() + "!Aa1"
        self.verbose = args.verbose
        self.targets = [(label, host.format(base=self.base)) for label, host in SUBDOMAINS]

        if not self.token:
            raise CannotRun(
                "no Authentik API token: set AUTHENTIK_BOOTSTRAP_TOKEN (env or .env)"
            )
        if not self.lan_ip:
            raise CannotRun("could not determine the host's LAN address (set LAN_IP)")


# ── HTTP ───────────────────────────────────────────────────────────────────


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """A cookie-jar-backed client that never follows redirects, so the OIDC hops
    can be asserted one at a time."""

    def __init__(self, cfg, base=None):
        self.cfg = cfg
        self.base = base or cfg.idp
        self.jar = http.cookiejar.CookieJar()

    def _trace(self, method, url, status):
        if self.cfg.verbose:
            print(f"         {method} {url[:96]} -> {status}", file=sys.stderr)

    def _open(self, req, timeout=30):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )
        # A read timeout is retried once before it is reported. This check walks
        # nine apps through the IdP in quick succession, and the one thing that
        # kept happening on a healthy deployment was a single stalled read —
        # which, unhandled, surfaced as a traceback from inside urllib rather
        # than as a result. A genuine outage fails the retry too, and then says
        # so in the vocabulary of this file instead of `TimeoutError`.
        for attempt in (1, 2):
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return (resp.status, resp.headers.get("Location"),
                            resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as err:
                return (err.code, err.headers.get("Location"),
                        (err.read() or b"").decode("utf-8", "replace"))
            except (TimeoutError, socket.timeout) as err:
                if attempt == 2:
                    raise CheckFailed(
                        f"no response from {req.full_url[:80]} within {timeout}s "
                        f"(tried twice): {err}"
                    ) from err

    def cookie(self, name):
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def get(self, url):
        if url.startswith("/"):  # IdP-relative
            url = self.base + url
        status, location, body = self._open(urllib.request.Request(url))
        self._trace("GET", url, status)
        return status, location, body

    def post(self, url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        # Authentik's flow executor requires the CSRF cookie echoed back.
        req.add_header("X-authentik-CSRF", self.cookie("authentik_csrf") or "")
        req.add_header("Referer", self.base + "/")
        status, location, body = self._open(req)
        self._trace("POST", url, status)
        return status, location, body

    def follow_json(self, url, hops=8):
        """Authentik bounces a POST -> 302 -> GET before handing back the next
        flow stage; follow until the JSON stage arrives."""
        for _ in range(hops):
            status, location, body = self.get(url)
            if status == 200:
                return json.loads(body)
            if status == 302 and location:
                url = location
                continue
            raise CheckFailed(f"expected a JSON stage, got HTTP {status} for {url}")
        raise CheckFailed("too many redirects inside Authentik's auth flow")

    def follow_to_code(self, url, hops=8, allow_denial=False):
        """Follow redirects until the OAuth2 redirect_uri carries ?code=.

        With `allow_denial`, a rendered page in place of the code is reported as
        IdpDenied rather than a broken hop — Authentik answers the final
        authorize step with its "Permission denied" page (HTTP 200) when the
        identity is not bound to the application."""
        for _ in range(hops):
            status, location, body = self.get(url)
            if status == 200 and allow_denial:
                raise IdpDenied(body)
            if status == 302 and location:
                if "code=" in location:
                    return location
                url = location
                continue
            raise CheckFailed(f"authorize returned {status} instead of a code: {body[:300]}")
        raise CheckFailed("no authorization code after too many redirects")


# ── Authentik admin API ────────────────────────────────────────────────────


class AuthApi:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.cfg.api + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.cfg.token)
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raise CannotRun(
                f"{method} {path} -> HTTP {err.code}: {(err.read() or b'').decode()[:300]}"
            )

    def find_group(self, name):
        """`superuser_full_list` matters: the plain list is policy-filtered for
        service accounts and can hide real groups."""
        query = "/core/groups/?superuser_full_list=true&name=" + urllib.parse.quote(name)
        for group in self.call("GET", query)["results"]:
            if group.get("name") == name:
                return group["pk"]
        return None

    def delete_user(self, username):
        for stale in self.call(
            "GET", "/core/users/?username=" + urllib.parse.quote(username)
        )["results"]:
            self.call("DELETE", f"/core/users/{stale['pk']}/")

    def make_user(self, username, label, groups=()):
        """Create an active internal user with a random password; return its pk."""
        self.delete_user(username)
        user = self.call(
            "POST",
            "/core/users/",
            {
                "username": username,
                "name": label,
                "email": f"{username}@innotel.us",
                "is_active": True,
                "path": "users",
                "type": "internal",
            },
        )
        pk = user["pk"]
        self.call("POST", f"/core/users/{pk}/set_password/", {"password": self.cfg.password})
        for group in groups:
            self.call("POST", f"/core/groups/{group}/add_user/", {"pk": pk})
        return pk


# ── the flow ───────────────────────────────────────────────────────────────


def require(condition, message):
    """A hop assertion: raise on failure, print nothing on success."""
    if not condition:
        raise CheckFailed(message)


def check(condition, message):
    if condition:
        print(f"  {OK}  {message}")
    else:
        raise CheckFailed(message)


def port_state(ip, port, timeout=4.0):
    """True when a TCP connection is accepted."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def sso_login(client, cfg, app, username, allow_idp_denial=False):
    """Drive a full authorization-code flow against one gateway, leaving the
    sealed session in the client's jar.

    Returns `(kind, status, body)`. `kind` is "flow" for a completed dance
    (`status` is then the callback hop), or "idp-denied" when Authentik refused
    to issue a code — which is what an identity the application is not bound to
    sees, and is a refusal just as final as a gateway 403.
    """
    status, location, _ = client.get(app + "/")
    require(status == 302, f"GET {app}/ -> HTTP {status} (expected 302 to the IdP)")
    require(cfg.idp in (location or ""),
            f"{app} redirected to {(location or '-')[:80]} instead of the IdP")
    require("client_id=" in (location or ""), "the authorize URL carries no client_id")

    status, location, body = client.get(location)
    if allow_idp_denial and status == 200:
        return "idp-denied", status, body
    require(status == 302 and location, f"authorize -> HTTP {status}: {body[:160]}")

    # Authentik hands back a flow URL on the host it actually serves; pin the
    # client to that origin so the session/CSRF cookies line up.
    flow = urllib.parse.urlparse(urllib.parse.urljoin(client.base, location))
    client.base = f"{flow.scheme}://{flow.netloc}"
    executor = (client.base + "/api/v3/flows/executor/" + AUTH_FLOW + "/?"
                + urllib.parse.urlencode({"query": urllib.parse.urlparse(location).query}))
    stage = client.follow_json(executor)
    for _ in range(6):
        component = stage.get("component")
        if component == "xak-flow-redirect":
            break
        if component == "ak-stage-identification":
            payload, label = {"uid_field": username}, "username"
        elif component == "ak-stage-password":
            payload, label = {"password": cfg.password}, "password"
        else:
            raise CheckFailed(f"unexpected Authentik stage {component}")
        status, next_url, body = client.post(executor, payload)
        if status not in (200, 302):
            raise CheckFailed(f"{label} rejected (HTTP {status}): {body[:200]}")
        stage = client.follow_json(next_url or executor)
    require(stage.get("component") == "xak-flow-redirect",
            "Authentik's flow never handed back the authorize URL")

    try:
        callback = client.follow_to_code(stage["to"], allow_denial=allow_idp_denial)
    except IdpDenied as denied:
        return "idp-denied", 200, denied.body
    status, _, callback_body = client.get(callback)
    return "flow", status, callback_body


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base domain for the public names")
    parser.add_argument("--idp", help="IdP origin (default the public Cerulean one)")
    parser.add_argument("--host-ip", help="the host's LAN address (default: detected)")
    parser.add_argument("--verbose", action="store_true", help="trace every HTTP hop")
    args = parser.parse_args()

    try:
        cfg = Config(args)
    except CannotRun as err:
        print(f"SKIP: {err}", file=sys.stderr)
        return 2

    api = AuthApi(cfg)
    print("Monarch SSO verification")
    print(f"  idp     : {cfg.idp}")
    print(f"  api     : {cfg.api}")
    print(f"  base    : {cfg.base}")
    print(f"  group   : {cfg.group or '(none)'}")
    print(f"  host ip : {cfg.lan_ip}")
    print(f"  targets : {len(cfg.targets)} public names")
    print()

    member_pk = outsider_pk = None
    failures = 0
    try:
        # ── reachability (a failure here is a skip, not a bad deployment) ──
        try:
            status, _, _ = Client(cfg).get(cfg.idp + "/")
        except (urllib.error.URLError, OSError) as err:
            raise CannotRun(f"{cfg.idp} is unreachable: {err}") from err
        if status >= 500:
            raise CannotRun(f"the IdP answered HTTP {status}")

        # ── 1. temporary identities ────────────────────────────────────────
        print("[1] temporary Authentik identities")
        group_pk = api.find_group(cfg.group) if cfg.group else None
        if cfg.group and not group_pk:
            raise CannotRun(f"Authentik group {cfg.group!r} not found")
        member_pk = api.make_user(MEMBER_USER, "Monarch SSO Verification",
                                  [group_pk] if group_pk else [])
        outsider_pk = api.make_user(OUTSIDER_USER, "Monarch SSO Outsider")
        print(f"  {OK}  {MEMBER_USER} (pk={member_pk}) in {cfg.group or 'no group'}")
        print(f"  {OK}  {OUTSIDER_USER} (pk={outsider_pk}) in no group")

        # ── 2. every public name, through a real login ─────────────────────
        print("[2] each gateway demands Authentik, then opens for the member")
        for label, host in cfg.targets:
            app = f"https://{host}"
            client = Client(cfg)
            print(f"  -- {label} ({host})")
            try:
                _, status, _ = sso_login(client, cfg, app, MEMBER_USER)
                require(status in (302, 200), f"callback -> HTTP {status} (expected a redirect)")
            except CheckFailed as err:
                print(f"  {BAD}  {err}")
                failures += 1
                continue
            check(client.cookie(SESSION_COOKIE) is not None,
                  f"{SESSION_COOKIE} session cookie issued")
            status, _, body = client.get(app + "/")
            check(status < 400, f"GET {app}/ with the session -> HTTP {status} "
                                f"({len(body)} bytes, expected < 400)")

        # ── 3. the group check is real ─────────────────────────────────────
        print("[3] an identity outside the required group is refused")
        label, host = cfg.targets[0]
        app = f"https://{host}"
        outsider = Client(cfg)
        try:
            # Two refusals are possible and either one is correct: Authentik can
            # decline to issue a code at all (the application is bound to the
            # group), or it issues one and the gateway's --allowed-group check
            # rejects it with 403. Which one fires depends on how the application
            # is bound, so accept both rather than pinning the test to one.
            kind, status, body = sso_login(outsider, cfg, app, OUTSIDER_USER,
                                           allow_idp_denial=True)
            if kind == "idp-denied":
                check("denied" in body.lower(),
                      f"{label}: Authentik refused the non-member's authorization")
            else:
                if status != 403:
                    status, _, _ = outsider.get(app + "/")
                check(status == 403, f"{label}: non-member -> HTTP {status} (expected 403)")
        except CheckFailed as err:
            print(f"  {BAD}  {label}: {err}")
            failures += 1

        # ── 4. the app ports are not a second door ─────────────────────────
        print("[4] app ports answer on loopback only")
        for label, port in LAN_ONLY_PORTS:
            check(not port_state(cfg.lan_ip, port),
                  f"{label}: {cfg.lan_ip}:{port} refused on the LAN — its gateway is the only door")
            check(port_state("127.0.0.1", port), f"{label}: 127.0.0.1:{port} answers")

        # ── 5. the shared session store is off the LAN too ─────────────────
        print("[5] the shared SSO session store is off the LAN")
        check(not port_state(cfg.lan_ip, SESSION_STORE_PORT),
              f"session store: {cfg.lan_ip}:{SESSION_STORE_PORT} refused on the LAN")
        check(port_state(cfg.session_store_host, SESSION_STORE_PORT),
              f"session store: {cfg.session_store_host}:{SESSION_STORE_PORT} answers "
              f"(the address every gateway dials)")

        if failures:
            print(f"\n{BAD} — {failures} target(s) did not pass", file=sys.stderr)
            return 1
        print("\nPASS — every public name is Authentik-only and the app ports are closed")
        return 0
    except CannotRun as err:
        print(f"\nSKIP: {err}", file=sys.stderr)
        return 2
    except CheckFailed as err:
        print(f"\n{BAD} — {err}", file=sys.stderr)
        return 1
    finally:
        for username, pk in ((MEMBER_USER, member_pk), (OUTSIDER_USER, outsider_pk)):
            if not pk:
                continue
            try:
                api.call("POST", f"/core/users/{pk}/set_password/",
                         {"password": os.urandom(24).hex()})
                api.call("DELETE", f"/core/users/{pk}/")
                print(f"[cleanup] deleted temporary user {username} (pk={pk})")
            except CannotRun as err:
                print(f"[cleanup] WARNING: could not delete pk={pk}: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
