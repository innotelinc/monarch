#!/usr/bin/env python3
"""
npm-proxy-hosts.py - configure Nginx Proxy Manager for the Monarch stack.

Drives Nginx Proxy Manager's API to:

  1. log in with the admin credentials (NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD),
  2. request a WILDCARD Let's Encrypt certificate for
     *.MONARCH_DOMAIN (+ the apex) using a DNS challenge so every subdomain is
     covered by one certificate, and
  3. create (or update) a proxy host per subdomain -> service from
     npm-hosts.conf (defaults built in below), forcing HTTPS.

Configuration comes from environment variables or the repo's .env file:

  MONARCH_DOMAIN        primary domain, default monarch.innotel.us
  NPM_BASE_URL          NPM API base, default http://localhost:81
  NPM_ADMIN_EMAIL       NPM admin login (required to make changes)
  NPM_ADMIN_PASSWORD    NPM admin password (required to make changes)
  SSL_EMAIL             Let's Encrypt account email (required for the cert)
  NPM_DNS_PROVIDER      DNS challenge provider (default: cloudflare)
  NPM_DNS_CREDENTIALS   provider credentials as JSON, e.g.
                        {"auth_token":"..."} for Cloudflare
  CLOUDFLARE_API_TOKEN  convenience: used instead of NPM_DNS_CREDENTIALS when
                        the provider is cloudflare
  NPM_FORWARD_HOST      "container" (default, forwards to service names on the
                        compose network; local NPM only) - anything else is
                        used verbatim as the forward host, e.g.
                        "host.docker.internal" for host-published ports, or
                        this host's public IP/hostname when NPM_MODE=remote
  NPM_CERT_ID           optional: reuse an existing certificate id instead of
                        requesting a new wildcard cert
  TECHNITIUM_URL        Cerulean's Technitium DNS API base, used to create the A
                        record for a NEW subdomain (+ TECHNITIUM_TOKEN, or
                        TECHNITIUM_USER / TECHNITIUM_PASSWORD to log in)

Flags:  --dry-run    print the plan without touching NPM
        --skip-ssl   skip certificate creation (hosts without a cert)
        --hosts-only manage proxy hosts only (no certificate work)
        --check      read-only: fail when a live proxy host differs from
                     npm-hosts.conf, including a host in THIS domain that the
                     conf no longer lists (retired)
        --prune      delete the proxy hosts in this domain that npm-hosts.conf
                     no longer lists. Destructive, and scoped to MONARCH_DOMAIN:
                     a host is only a candidate when every domain name it serves
                     is inside this domain, so the other products sharing the
                     NPM are never touched.

The script is idempotent: existing proxy hosts are updated in place, and
certificate issuance is only triggered when no matching wildcard cert exists.

DNS: when NPM_FORWARD_HOST is an IP, the script points the subdomain at it in
Cerulean's DNS plane (Technitium HTTP API - TECHNITIUM_URL plus
TECHNITIUM_TOKEN, or TECHNITIUM_USER / TECHNITIUM_PASSWORD). A name that
already resolves is left alone: Monarch's hosts are CNAMEs to the apex and
Technitium refuses an A record where a CNAME exists, so this only ever adds
the record for a newly listed subdomain. The legacy BIND/TSIG path
(DNS_TSIG_*) is still read as a fallback and warns that Cerulean has moved to
Technitium.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOSTS = [
    # subdomain  forward (container name or host port)  port  websockets
    # "@" is the apex — the base MONARCH_DOMAIN itself, which is the main
    # interface users log into (the Homarr dashboard).
    ("@",         "homarr",                   7575, True),
    ("app",       "homarr",                   7575, True),
    ("auth",      "authentik-server",         9000, False),
    ("media",     "jellyfin",                 8096, True),
    # Host 3011 -> container 3000: host :3001 belongs to the Zeus portal, so
    # this forwards to the published IPTV guide (npm-hosts.conf carries the
    # same line for real deployments).
    ("tv",        "iptv",                     3011, False),
    # NPM Edge's admin UI, reached through the SSO gateway (oauth2-proxy,
    # `cerulean-npm-sso`) on the NPM container's own loopback — never the UI's
    # :81. The gateway completes a real OIDC code flow against Cerulean
    # Authentik and then proxies to :81, and only the identity headers it sets
    # are trusted (npm-hosts.conf carries the same line for real deployments).
    ("admin",     "127.0.0.1",               4180, False),
    ("req",       "jellyseerr",               5055, True),
]

HOSTS_CONF = os.path.join(SCRIPT_DIR, "npm-hosts.conf")



def subdomain_of(domain_name, domain):
    """'radarr.monarch.innotel.us' + 'monarch.innotel.us' -> 'radarr'."""
    return domain_name[: -len(domain) - 1] if domain_name.endswith("." + domain) else "@"


def our_domain(domain_name, domain):
    """True when <domain_name> is <domain> itself or a subdomain of it.

    This is what keeps --prune inside Monarch's own namespace on a shared NPM.
    """
    return domain_name == domain or domain_name.endswith("." + domain)


def load_env(path):
    """Parse KEY=VALUE lines from a .env file into os.environ (never overwrite)."""
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def env(name, default=""):
    return os.environ.get(name, default).strip()


def expand_env_refs(value):
    """Expand ${VAR} / ${VAR:-default} in a conf field from the env / .env.

    Lets npm-hosts.conf name the same variable the compose file derives a
    publish from (SABNZBD_PORT), so one .env value drives both the publish and
    the proxy host and the two cannot drift apart.
    """
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
        out += env(name.strip()) or default
        i = end + 1


def resolve_forward(host, forward_mode):
    """The upstream a row forwards to.

    Most rows name a compose service and follow the global NPM_FORWARD_HOST mode
    ("container" for a local NPM, or this host's IP for a REMOTE one). A row may
    instead name its own upstream - an IP or a dotted hostname - which then
    always wins. Container names never contain a dot, so the two forms cannot be
    confused: `admin 127.0.0.1 4180` reaches the NPM admin UI on its own
    loopback (where nginx runs, and the only place it believes the edge's
    identity headers), while every other row still follows the global mode.

    This is why admin.<domain> needs an override at all: the admin UI does not
    run on this Docker host (the compose "npm" profile is optional and is not
    running here), it runs on the shared NPM host, so forwarding to this host's
    published :2081 reaches nothing.
    """
    fwd = expand_env_refs(host["forward"])
    if is_ip_address(fwd) or "." in fwd:
        return fwd
    return fwd if forward_mode == "container" else forward_mode


def load_hosts(domain):
    """Subdomain map from npm-hosts.conf (or built-in defaults).

    Conf format: one host per line - `<sub> <forward> <port> [websockets]`
    where forward is the container name (or a host name/port for custom rows),
    the port is this host's published port (or ${VAR:-default}), and `@` means
    the apex (MONARCH_DOMAIN itself — the main interface users log into). Lines
    starting with '#' are comments.
    """
    hosts_src = os.path.isfile(HOSTS_CONF) and HOSTS_CONF or "built-in defaults"
    if os.path.isfile(HOSTS_CONF):
        with open(HOSTS_CONF, "r", encoding="utf-8", errors="replace") as fh:
            hosts = []
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                sub, forward, port = parts[0], parts[1], parts[2]
                # Trailing flags, order-independent: yes/true/1 -> websockets.
                flags = {p.lower() for p in parts[3:]}
                ws = bool(flags & {"yes", "true", "1"})
                resolved = expand_env_refs(port)
                if not resolved.isdigit():
                    raise SystemExit(
                        f"npm-hosts.conf: the port for '{sub}' is {port!r}, which "
                        f"resolved to {resolved!r} - set that variable, or give "
                        "the field a ${VAR:-default}"
                    )
                hosts.append((sub, forward, int(resolved), ws))
    else:
        hosts = list(DEFAULT_HOSTS)

    result = []
    for sub, forward, port, ws in hosts:
        if sub == "@":
            host_domain = domain
        else:
            host_domain = f"{sub}.{domain}" if sub else domain
        result.append({
            "domain": host_domain,
            "forward": forward,
            "port": int(port),
            "websockets": ws,
        })
    return result, hosts_src


# ---------------------------------------------------------------------------
# NPM API client (stdlib only - runs on the host, no pip needed)
# ---------------------------------------------------------------------------


class NpmClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, body=None, token=None, timeout=60):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                j = json.loads(raw) if raw else None
            except ValueError:
                j = raw
            return exc.code, j
        except Exception as exc:  # noqa: BLE001 - network errors
            return 0, str(exc)

    def login(self, email, password, quiet=False):
        status, body = self._request("POST", "/api/tokens",
                                     {"identity": email, "secret": password})
        if status == 200 and isinstance(body, dict) and body.get("token"):
            return body["token"]
        if not quiet:
            if status in (401, 403):
                print(f"  ERROR: NPM rejected the admin login ({status}).")
                print("  If this is the first run, open http://<host>:81 once, set your")
                print("  admin email + password (default admin@example.com / changeme),")
                print("  then re-run setup.sh.")
            else:
                print(f"  ERROR: NPM login failed (HTTP {status}): {body}")
        return None

    def bootstrap_first_admin(self, email, password):
        """Create the initial NPM admin account with the configured credentials.

        Current NPM images boot WITHOUT a default account - the UI asks you to
        create one on the first visit, and POST /api/users is accepted
        unauthenticated until a user exists. Older NPM images seeded
        admin@example.com / changeme, so fall back to logging in with that and
        creating the configured admin. Returns True when an account was made.
        """
        payload = {"name": "Admin", "nickname": "admin", "email": email,
                   "auth": {"type": "password", "secret": password}}
        # Right after the container starts, the UI answers 200 while the API
        # backend is still coming up (502 from openresty / 404 on the route), so
        # retry creation for a short window before giving up.
        status = created = None
        for _ in range(12):
            status, created = self._request("POST", "/api/users", body=payload)
            if status in (200, 201) and isinstance(created, dict) and created.get("id"):
                print(f"  Bootstrapped NPM admin {created.get('email')} "
                      "(first-run account created automatically).")
                return True
            if status in (404, 502, 503, 504, 0):
                time.sleep(5)
                continue
            break  # deterministic rejection (users already exist, bad payload, ...)
        # Users already exist (or older NPM): try the seeded default account.
        token = self.login("admin@example.com", "changeme", quiet=True)
        if token:
            status, created = self._request("POST", "/api/users", body=payload,
                                            token=token)
            if status in (200, 201) and isinstance(created, dict) and created.get("id"):
                print(f"  Bootstrapped NPM admin {created.get('email')} "
                      "(created next to the default account).")
                return True
            print("  NPM has existing users and the default-account login works, "
                  "but creating the configured admin failed "
                  f"(HTTP {status}) - set it in the NPM UI instead.")
        return False

    def get_schema(self, token=None):
        """Fetch the OpenAPI schema exposed by newer NPM versions (legacy
        versions don't have /api/schema - returns None)."""
        status, body = self._request("GET", "/api/schema", token=token, timeout=15)
        if status == 200 and isinstance(body, dict) and "paths" in body:
            return body
        return None

    @staticmethod
    def _post_props(schema, path):
        """Properties accepted by POST <path>, from the OpenAPI schema."""
        try:
            post = schema["paths"][path]["post"]
            rb = post["requestBody"]["content"]["application/json"]["schema"]
            return rb.get("properties", {}) or {}
        except (KeyError, TypeError):
            return {}

    @staticmethod
    def proxy_host_field_names(schema):
        """Map logical flags to the field names this NPM version accepts.

        Newer NPM (>= 2.12) renamed `websockets_support` ->
        `allow_websocket_upgrade` and `caching` -> `caching_enabled`, and
        rejects unknown properties, so pick the names from the live schema.
        """
        props = NpmClient._post_props(schema or {}, "/nginx/proxy-hosts")
        return {
            "websockets": ("allow_websocket_upgrade"
                           if "allow_websocket_upgrade" in props
                           else "websockets_support"),
            "caching": "caching_enabled" if "caching_enabled" in props else "caching",
        }

    def get_certificates(self, token):
        status, body = self._request("GET", "/api/nginx/certificates", token=token)
        return body if status == 200 and isinstance(body, list) else []

    def matching_cert(self, certs, domain):
        wildcard = f"*.{domain}"
        for cert in certs:
            names = cert.get("domain_names") or []
            if wildcard in names or domain in names:
                return cert
        return None

    def create_wildcard_cert(self, token, domain, email, provider, credentials,
                             schema=None):
        """Request the wildcard Let's Encrypt certificate via DNS challenge.

        Newer NPM versions changed the certificate schema (no top-level
        `email`, `dns_provider_credentials` instead of `credentials`, no
        `letsencrypt_agree`); adapt to whatever the live schema accepts.
        """
        body = {"provider": "letsencrypt",
                "domain_names": [f"*.{domain}", domain]}
        meta = {"dns_challenge": True, "dns_provider": provider}
        props = NpmClient._post_props(schema or {}, "/nginx/certificates")
        meta_props = (props.get("meta", {}) or {}).get("properties", {}) or {}
        if schema:
            if "dns_provider_credentials" in meta_props:
                meta["dns_provider_credentials"] = credentials_ini(provider, credentials)
                meta["propagation_seconds"] = 60
            if "letsencrypt_agree" in meta_props:
                meta["letsencrypt_agree"] = True
            if "credentials" in meta_props:
                meta["credentials"] = credentials
            if "key_type" in meta_props:
                meta["key_type"] = "rsa"
            if "email" in props:
                body["email"] = email
        else:
            meta.update({"letsencrypt_agree": True, "credentials": credentials})
            body["email"] = email
        body["meta"] = meta
        status, created = self._request("POST", "/api/nginx/certificates",
                                        body, token=token)
        if status not in (200, 201) or not isinstance(created, dict):
            print(f"  ERROR: certificate request failed (HTTP {status}): {created}")
            return None
        return created.get("id")

    def wait_for_cert(self, token, cert_id, timeout=600):
        """Poll until the cert is issued (has expires) or fails (has error)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, cert = self._request(
                "GET", f"/api/nginx/certificates/{cert_id}", token=token)
            if status == 200 and isinstance(cert, dict):
                if cert.get("error"):
                    print(f"  ERROR: certificate issuance failed: {cert['error']}")
                    print("  Check the DNS provider token (Zone:DNS:Edit) and "
                          "that the wildcard A record resolves.")
                    return False
                if cert.get("expires"):
                    print(f"  Certificate #{cert_id} issued "
                          f"(expires {cert['expires']})")
                    return True
            time.sleep(5)
        print(f"  ERROR: timed out waiting for certificate #{cert_id}")
        return False

    def get_proxy_hosts(self, token):
        status, body = self._request("GET", "/api/nginx/proxy-hosts", token=token)
        return body if status == 200 and isinstance(body, list) else []

    def find_host(self, hosts, domain):
        for host in hosts:
            names = host.get("domain_names") or []
            if domain in names:
                return host
        return None

    def delete_proxy_host(self, token, host_id):
        """Delete a proxy host: (status, body); 200/204 means it is gone."""
        return self._request("DELETE", f"/api/nginx/proxy-hosts/{host_id}",
                             token=token)

    def upsert_proxy_host(self, token, host_id, domain, forward_host, forward_port,
                          certificate_id, websockets, dry_run=False,
                          host_fields=None, advanced_config="client_max_body_size 0;"):
        host_fields = host_fields or {"websockets": "websockets_support",
                                      "caching": "caching"}
        body = {
            "domain_names": [domain],
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": forward_port,
            "certificate_id": certificate_id,
            "ssl_forced": bool(certificate_id),
            "block_exploits": True,
            host_fields["caching"]: False,
            host_fields["websockets"]: websockets,
            "access_list_id": 0,
            "advanced_config": advanced_config,
        }
        action = "update" if host_id else "create"
        if dry_run:
            print(f"  [dry-run] would {action} {domain} -> "
                  f"{forward_host}:{forward_port} (ws={websockets})")
            return
        path = f"/api/nginx/proxy-hosts/{host_id}" if host_id else "/api/nginx/proxy-hosts"
        method = "PUT" if host_id else "POST"
        status, body_resp = self._request(method, path, body, token=token)
        if status in (200, 201):
            print(f"  {action}d proxy host {domain} -> "
                  f"{forward_host}:{forward_port} (ws={websockets})")
        else:
            print(f"  ERROR: could not {action} proxy host {domain} "
                  f"(HTTP {status}): {body_resp}" if body_resp else
                  f"  ERROR: could not {action} proxy host {domain} "
                  f"(HTTP {status})")


# ---------------------------------------------------------------------------
# Dynamic DNS: Cerulean's Technitium HTTP API (the ecosystem's DNS plane)
# ---------------------------------------------------------------------------
# Cerulean owns DNS and replaced RFC2136/nsupdate/SSH+BIND with Technitium over
# HTTP - "no SSH, no TSIG, no nsupdate" (cerulean docs/stack.md).
# Monarch's A records go there. The legacy DNS_TSIG_* path below still works for
# a host that runs its own BIND, and says so out loud when it is used.


def technitium_login(url, user, password):
    """Exchange user/password for a Technitium session token."""
    data = technitium_api(url, "/api/user/login", {"user": user, "pass": password})
    token = data.get("token", "")
    if not token:
        raise RuntimeError(f"Technitium login rejected ({data.get('status')})")
    return token


def technitium_config():
    """(url, token) for the Technitium API, or None when unconfigured.

    A static API token wins (it never expires); otherwise TECHNITIUM_USER /
    TECHNITIUM_PASSWORD is exchanged for a session token on each run. Same
    variable names Cerulean itself uses, so one .env value serves both.
    """
    url = env("TECHNITIUM_URL").rstrip("/")
    if not url:
        return None
    token = env("TECHNITIUM_TOKEN")
    if token:
        return url, token
    user, password = env("TECHNITIUM_USER"), env("TECHNITIUM_PASSWORD")
    if user and password:
        return url, technitium_login(url, user, password)
    return None


def technitium_api(url, path, params, token=""):
    """One Technitium HTTP API call -> parsed JSON."""
    req = urllib.request.Request(f"{url}{path}?{urlencode(params)}", method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


def technitium_zone(url, token, fqdn):
    """The hosted zone that owns fqdn (longest match), or '' when none does."""
    data = technitium_api(url, "/api/zones/list",
                          {"pageNumber": 1, "zonesPerPage": 100}, token)
    names = [z.get("name", "").rstrip(".")
             for z in data.get("response", {}).get("zones", [])]
    matches = [n for n in names if n and (fqdn == n or fqdn.endswith("." + n))]
    return max(matches, key=len) if matches else ""


def technitium_records(url, token, fqdn, zone):
    """The records that already exist for fqdn: [(type, value), ...]."""
    data = technitium_api(url, "/api/zones/records/get",
                          {"domain": fqdn, "zone": zone, "listZone": "false"}, token)
    out = []
    for rec in data.get("response", {}).get("records", []):
        rdata = rec.get("rData", {})
        out.append((str(rec.get("type", "")).upper(),
                    rdata.get("ipAddress") or rdata.get("cname") or ""))
    return out


def dns_upsert_technitium(cfg, fqdn, ip, ttl=300, dry_run=False):
    """Point <fqdn> at <ip> in Cerulean's Technitium, unless it already resolves.

    A name that already has a record is LEFT ALONE: Monarch's hosts are CNAMEs
    to the apex (every product subdomain CNAMEs to `innotel.us`, which holds the
    A record), and Technitium refuses an A where a CNAME exists. Only a name
    with no record gets one - which is the case this automation exists for (a
    newly added subdomain in npm-hosts.conf).
    """
    url, token = cfg
    if dry_run:
        print(f"  [dry-run] would DNS: {fqdn} A {ip} via Technitium {url} "
              "(if it has no record yet)")
        return True
    try:
        zone = technitium_zone(url, token, fqdn)
        if not zone:
            print(f"  WARNING: no Technitium zone owns {fqdn} - skipping DNS "
                  "update (create the zone in Cerulean first).")
            return False
        existing = technitium_records(url, token, fqdn, zone)
        if existing:
            kinds = ", ".join(f"{t} {v}".strip() for t, v in existing)
            print(f"  DNS: {fqdn} already resolves ({kinds}) - left as is")
            return True
        data = technitium_api(url, "/api/zones/records/add", {
            "domain": fqdn, "zone": zone, "type": "A",
            "ipAddress": ip, "ttl": ttl, "overwrite": "true"}, token)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        print(f"  ERROR: Technitium DNS update failed for {fqdn}: {exc}")
        return False
    status = str(data.get("status", ""))
    if status not in ("ok", "success"):
        message = str(data.get("errorMessage") or status)
        if "CNAME" in message.upper():          # raced with another writer
            print(f"  DNS: {fqdn} already resolves (CNAME) - left as is")
            return True
        print(f"  ERROR: Technitium refused {fqdn} -> {ip}: {message}")
        return False
    print(f"  DNS: {fqdn} -> {ip} (A, TTL {ttl}) via Technitium ({zone})")
    return True


def tsig_config():
    """TSIG credentials for dynamic DNS updates, or None when unset."""
    server = env("DNS_TSIG_SERVER")
    key_name = env("DNS_TSIG_KEY_NAME")
    key_secret = env("DNS_TSIG_KEY_SECRET")
    if not (server and key_name and key_secret):
        return None
    return {
        "server": server,
        "key_name": key_name,
        "key_secret": key_secret,
        "algorithm": env("DNS_TSIG_KEY_ALGORITHM", "hmac-sha256"),
    }


def is_ip_address(value):
    try:
        import ipaddress
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def credentials_ini(provider, credentials):
    """Render DNS provider credentials as the INI text certbot expects.

    Newer NPM versions write dns_provider_credentials straight to certbot's
    credentials file (key = value lines), not JSON.
    """
    if provider == "rfc2136":
        return "\n".join(f"{k} = {v}" for k, v in credentials.items())
    if provider == "cloudflare":
        token = credentials.get("auth_token") or credentials.get("api_token")
        if token:
            return f"dns_cloudflare_api_token = {token}"
    return json.dumps(credentials)


def dns_upsert(technitium, tsig, fqdn, ip, ttl=300, dry_run=False):
    """Upsert the A record: Technitium first, legacy BIND/TSIG second."""
    if technitium:
        return dns_upsert_technitium(technitium, fqdn, ip, ttl, dry_run)
    if tsig:
        print("  WARNING: using the legacy BIND/TSIG path - Cerulean's DNS plane "
              "is Technitium (set TECHNITIUM_URL + TECHNITIUM_TOKEN or "
              "TECHNITIUM_USER/PASSWORD)")
        return dns_upsert_a(tsig, fqdn, ip, ttl, dry_run)
    return False


def dns_upsert_a(tsig, fqdn, ip, ttl=300, dry_run=False):
    """Upsert <fqdn> A <ip> on a BIND server via nsupdate (legacy TSIG path)."""
    script = (
        f"server {tsig['server']}\n"
        f"update delete {fqdn}. A\n"
        f"update add {fqdn}. {ttl} A {ip}\n"
        "send\n"
    )
    if dry_run:
        print(f"  [dry-run] would DNS: {fqdn} A {ip} @ {tsig['server']}")
        return True
    try:
        proc = subprocess.run(
            ["nsupdate", "-y",
             f"{tsig['algorithm']}:{tsig['key_name']}:{tsig['key_secret']}"],
            input=script, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print(f"  WARNING: nsupdate not found - skipping DNS update for {fqdn} "
              "(install bind9-dnsutils).")
        return False
    if proc.returncode != 0:
        print(f"  ERROR: DNS update failed for {fqdn}: {proc.stderr.strip()}")
        return False
    print(f"  DNS: {fqdn} -> {ip} (A, TTL {ttl}) @ {tsig['server']}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Auto-configure Nginx Proxy Manager for Monarch subdomains")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without changing anything")
    parser.add_argument("--skip-ssl", action="store_true",
                        help="skip wildcard certificate creation")
    parser.add_argument("--hosts-only", action="store_true",
                        help="manage proxy hosts only (no certificate work)")
    parser.add_argument("--check", action="store_true",
                        help="verify live NPM proxy hosts match npm-hosts.conf "
                             "(read-only; exit 1 on any drift)")
    parser.add_argument("--prune", action="store_true",
                        help="delete the proxy hosts in this domain that "
                             "npm-hosts.conf no longer lists")
    args = parser.parse_args()

    load_env(os.path.join(REPO_ROOT, ".env"))

    domain = env("MONARCH_DOMAIN", "monarch.innotel.us")
    npm_url = env("NPM_BASE_URL", "http://localhost:81")
    npm_email = env("NPM_ADMIN_EMAIL")
    npm_pass = env("NPM_ADMIN_PASSWORD")
    ssl_email = env("SSL_EMAIL")
    forward_mode = env("NPM_FORWARD_HOST", "container")
    cert_id = env("NPM_CERT_ID")
    tsig = tsig_config()
    try:
        technitium = technitium_config()
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        print(f"  WARNING: Technitium DNS automation unavailable ({exc}) - "
              "A records will not be written")
        technitium = None

    hosts, hosts_src = load_hosts(domain)

    print(f"Monarch -> Nginx Proxy Manager: {npm_url}")
    print(f"  domain: {domain}  ({len(hosts)} proxy hosts from {hosts_src})")
    if technitium:
        print(f"  DNS: A records via Technitium {technitium[0]}")
    elif tsig:
        print(f"  DNS: A records via the LEGACY BIND/TSIG path ({tsig['server']}) - "
              "Cerulean's DNS plane is Technitium (set TECHNITIUM_URL + a token)")
    else:
        print("  DNS: not configured - A records are left untouched "
              "(set TECHNITIUM_URL + TECHNITIUM_TOKEN or TECHNITIUM_USER/PASSWORD)")
    # No gate is written anywhere: this stack fronts the apps that have no OIDC
    # of their own with oauth2-proxy SSO gateways, and the apps do their own
    # Authentik OIDC where they can. See the npm repo's docs/stack.md.

    if not npm_email or not npm_pass:
        print("ERROR: NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD are not set (see .env.sample).")
        sys.exit(2 if not args.dry_run else 0)

    # --check and --prune read the live NPM even alongside --dry-run, so they
    # need a token; a plain --dry-run stays credential-free.
    live = not args.dry_run or args.check or args.prune
    client = NpmClient(npm_url)
    token = client.login(npm_email, npm_pass, quiet=True) if live else None
    if not token and live:
        print("  NPM did not accept the configured admin login - checking whether "
              "this is a first-run instance that needs its admin created...")
        client.bootstrap_first_admin(npm_email, npm_pass)
        token = client.login(npm_email, npm_pass)
    if not token and live:
        sys.exit(1)

    # Newer NPM versions tightened the API schema (renamed fields, strict
    # additionalProperties) - fetch the OpenAPI schema to adapt. Legacy NPM
    # versions don't expose it and the old field names are used as fallback.
    schema = client.get_schema(token) if live else None
    host_fields = client.proxy_host_field_names(schema)
    if schema:
        print("  NPM API schema detected - adapting field names "
              f"(websockets={host_fields['websockets']}, "
              f"caching={host_fields['caching']}).")

    # ---- wildcard certificate -----------------------------------------
    if not args.check and not args.skip_ssl and not args.hosts_only \
            and not args.prune:
        if args.dry_run:
            print(f"  [dry-run] would request wildcard cert for *.{domain} "
                  f"via {env('NPM_DNS_PROVIDER', 'cloudflare')}")
        else:
            certs = client.get_certificates(token)
            existing = client.matching_cert(certs, domain)
            if existing and existing.get("id"):
                cert_id = str(existing["id"])
                print(f"  Wildcard cert for *.{domain} already exists "
                      f"(#{cert_id}) - reusing.")
            elif cert_id:
                print(f"  Using NPM_CERT_ID {cert_id} as configured.")
            elif not ssl_email:
                print("  WARNING: SSL_EMAIL not set - skipping wildcard certificate. "
                      "Set SSL_EMAIL in .env and re-run (or use --skip-ssl).")
            else:
                provider = env("NPM_DNS_PROVIDER", "cloudflare")
                credentials = {}
                if env("NPM_DNS_CREDENTIALS"):
                    try:
                        credentials = json.loads(env("NPM_DNS_CREDENTIALS"))
                    except ValueError:
                        print("  ERROR: NPM_DNS_CREDENTIALS is not valid JSON.")
                        sys.exit(1)
                elif provider == "cloudflare" and env("CLOUDFLARE_API_TOKEN"):
                    credentials = {"auth_token": env("CLOUDFLARE_API_TOKEN")}
                if not credentials:
                    print(f"  ERROR: no credentials for DNS provider '{provider}' - "
                          "set NPM_DNS_CREDENTIALS (or CLOUDFLARE_API_TOKEN).")
                    sys.exit(1)
                print(f"  Requesting wildcard cert for *.{domain} via {provider} "
                      f"(email {ssl_email})...")
                new_id = client.create_wildcard_cert(token, domain, ssl_email,
                                                     provider, credentials,
                                                     schema=schema)
                if new_id and client.wait_for_cert(token, new_id):
                    cert_id = str(new_id)

    # ---- proxy hosts ---------------------------------------------------
    existing_hosts = client.get_proxy_hosts(token) if live else []

    # --prune: delete the proxy hosts in THIS domain that npm-hosts.conf no
    # longer lists. Deleting a line from the conf is deliberately not enough on
    # its own - a typo in the conf must never remove a live host - so it takes
    # this explicit flag. Scoped by domain: a host is a candidate only when
    # every name it serves is inside MONARCH_DOMAIN.
    if args.prune:
        desired = {h["domain"] for h in hosts}
        retired = []
        for host in existing_hosts:
            names = host.get("domain_names") or []
            if names and all(our_domain(n, domain) for n in names) \
                    and not any(n in desired for n in names):
                retired.append((host, names[0]))
        if not retired:
            print("  Nothing to prune: every proxy host in this domain is in "
                  "npm-hosts.conf.")
            sys.exit(0)
        for host, name in retired:
            if args.dry_run:
                print(f"  [dry-run] would delete {name} (id {host.get('id')} -> "
                      f"{host.get('forward_host')}:{host.get('forward_port')})")
                continue
            status, body = client.delete_proxy_host(token, host.get("id"))
            if status in (200, 204):
                print(f"  deleted {name} (id {host.get('id')}) - it was in this "
                      "domain but not in npm-hosts.conf")
            else:
                print(f"  ERROR: deleting {name} -> HTTP {status}: {body}")
                sys.exit(1)
        if args.dry_run:
            print(f"  [dry-run] {len(retired)} retired proxy host(s) would be "
                  "deleted")
        else:
            print(f"  pruned {len(retired)} retired proxy host(s) - "
                  "npm-hosts.conf is authoritative for this domain again")
        sys.exit(0)

    # --check: diff the live hosts against npm-hosts.conf and report drift.
    # The forward host resolves exactly like the upsert loop below, so the
    # check validates what a run of this script would actually configure.
    if args.check:
        drifted = matched = 0
        desired = {h["domain"] for h in hosts}
        for host in hosts:
            domain_name = host["domain"]
            forward_host = resolve_forward(host, forward_mode)
            existing = client.find_host(existing_hosts, domain_name)
            if existing is None:
                print(f"  DRIFT: {domain_name} -> missing in NPM "
                      f"(expected {forward_host}:{host['port']} "
                      f"ws={host['websockets']})")
                drifted += 1
                continue
            problems = []
            got_fwd = existing.get("forward_host")
            got_port = existing.get("forward_port")
            got_ws = existing.get(host_fields["websockets"])
            if got_fwd != forward_host:
                problems.append(f"forward_host={got_fwd!r} "
                                f"(expected {forward_host!r})")
            if str(got_port) != str(host["port"]):
                problems.append(f"forward_port={got_port!r} "
                                f"(expected {host['port']})")
            if bool(got_ws) != bool(host["websockets"]):
                problems.append(f"websockets={bool(got_ws)} "
                                f"(expected {bool(host['websockets'])})")
            # A forward-auth gate left on a host is drift: identity is Authentik
            # OIDC (directly, or through an oauth2-proxy gateway), never an
            # nginx auth_request.
            if "outpost.goauthentik.io" in (existing.get("advanced_config") or ""):
                problems.append("advanced_config still carries a forward-auth gate")
            if problems:
                print(f"  DRIFT: {domain_name} -> " + "; ".join(problems))
                drifted += 1
            else:
                print(f"  ok: {domain_name} -> {got_fwd}:{got_port} "
                      f"(ws={bool(got_ws)})")
                matched += 1
        # A host in this domain that the conf no longer lists is drift, not a
        # footnote: that is how the retired subscribe host stayed live for
        # weeks. Hosts outside this domain belong to other products sharing the
        # NPM and are reported as a note only.
        ours, foreign = [], []
        for host in existing_hosts:
            names = host.get("domain_names") or [""]
            if any(d in desired for d in names):
                continue
            name = names[0]
            (ours if all(our_domain(n, domain) for n in names)
             else foreign).append(name)
        for name in sorted(ours):
            print(f"  DRIFT: {name} -> live in NPM but not in npm-hosts.conf "
                  "(retired; remove it with: npm-proxy-hosts.py --prune)")
            drifted += 1
        print("")
        if drifted:
            print(f"  CHECK FAILED: {drifted} proxy host(s) drifted from "
                  f"npm-hosts.conf ({matched} match)")
            sys.exit(1)
        print(f"  CHECK OK: all {matched} proxy hosts match npm-hosts.conf")
        if foreign:
            shown = ", ".join(sorted(foreign)[:3])
            more = f" (+{len(foreign) - 3} more)" if len(foreign) > 3 else ""
            print(f"  (note: {len(foreign)} host(s) in NPM outside this domain "
                  f"are not managed here: {shown}{more})")
        sys.exit(0)

    for host in hosts:
        domain_name = host["domain"]
        forward_host = resolve_forward(host, forward_mode)
        existing = client.find_host(existing_hosts, domain_name)
        host_id = str(existing.get("id")) if existing else None
        client.upsert_proxy_host(
            token, host_id, domain_name, forward_host,
            host["port"], cert_id or None,
            host["websockets"], dry_run=args.dry_run,
            host_fields=host_fields,
            advanced_config="client_max_body_size 0;")
        # Keep DNS in sync: write the A record for the subdomain when the
        # forward target is an IP and a DNS mechanism is configured.
        if is_ip_address(forward_host):
            dns_upsert(technitium, tsig, domain_name, forward_host,
                       dry_run=args.dry_run)

    print("")
    print("Done. First point DNS at this host:  *.%s  and %s  ->  <public IP>"
          % (domain, domain))
    print("Then open https://%s (main interface) or https://app.%s (Homarr)"
          % (domain, domain))
    print("NPM admin UI: https://admin.%s  (or http://<host>:81)" % domain)


if __name__ == "__main__":
    main()