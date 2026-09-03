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

Flags:  --dry-run    print the plan without touching NPM
        --skip-ssl   skip certificate creation (hosts without a cert)
        --hosts-only manage proxy hosts only (no certificate work)

The script is idempotent: existing proxy hosts are updated in place, and
certificate issuance is only triggered when no matching wildcard cert exists.

DNS: when NPM_FORWARD_HOST is an IP and the DNS_TSIG_* variables are set
(BIND + RFC 2136), the script writes the subdomain A records itself via
nsupdate. The wildcard certificate uses the NPM_DNS_PROVIDER=rfc2136 DNS
challenge (TXT records signed with the same TSIG key), so no manual DNS
edits are needed.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

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
    ("api",       "billing-api",              8001, False),
    ("auth",      "authentik-server",         9000, False),
    ("media",     "jellyfin",                 8096, True),
    ("tv",        "iptv",                     3001, False),
    ("admin",     "nginx-proxy-manager",       81, False),
    ("subscribe", "jellyfin-subscription",    3000, False),
    ("req",       "jellyseerr",               5055, True),
]

HOSTS_CONF = os.path.join(SCRIPT_DIR, "npm-hosts.conf")


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


def load_hosts(domain):
    """Subdomain map from npm-hosts.conf (or built-in defaults).

    Conf format: one host per line - `<sub> <forward> <port> [websockets]`
    where forward is the container name (or a host name/port for custom rows)
    and `@` means the apex (MONARCH_DOMAIN itself — the main interface users
    log into). Lines starting with '#' are comments.
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
                ws = len(parts) > 3 and parts[3].lower() in ("yes", "true", "1")
                hosts.append((sub, forward, int(port), ws))
    else:
        hosts = DEFAULT_HOSTS

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

    def upsert_proxy_host(self, token, host_id, domain, forward_host, forward_port,
                          certificate_id, websockets, dry_run=False,
                          host_fields=None):
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
            "advanced_config": "client_max_body_size 0;",
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
# Dynamic DNS via BIND TSIG (RFC 2136)
# ---------------------------------------------------------------------------


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


def dns_upsert_a(tsig, fqdn, ip, ttl=300, dry_run=False):
    """Upsert <fqdn> A <ip> on the BIND server via nsupdate (TSIG)."""
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

    hosts, hosts_src = load_hosts(domain)

    print(f"Monarch -> Nginx Proxy Manager: {npm_url}")
    print(f"  domain: {domain}  ({len(hosts)} proxy hosts from {hosts_src})")

    if not npm_email or not npm_pass:
        print("ERROR: NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD are not set (see .env.sample).")
        sys.exit(2 if not args.dry_run else 0)

    client = NpmClient(npm_url)
    token = None if args.dry_run else client.login(npm_email, npm_pass, quiet=True)
    if not token and not args.dry_run:
        print("  NPM did not accept the configured admin login - checking whether "
              "this is a first-run instance that needs its admin created...")
        client.bootstrap_first_admin(npm_email, npm_pass)
        token = client.login(npm_email, npm_pass)
    if not token and not args.dry_run:
        sys.exit(1)

    # Newer NPM versions tightened the API schema (renamed fields, strict
    # additionalProperties) - fetch the OpenAPI schema to adapt. Legacy NPM
    # versions don't expose it and the old field names are used as fallback.
    schema = client.get_schema(token) if not args.dry_run else None
    host_fields = client.proxy_host_field_names(schema)
    if schema:
        print("  NPM API schema detected - adapting field names "
              f"(websockets={host_fields['websockets']}, "
              f"caching={host_fields['caching']}).")

    # ---- wildcard certificate -----------------------------------------
    if not args.skip_ssl and not args.hosts_only:
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
    if not args.dry_run:
        existing_hosts = client.get_proxy_hosts(token)
    else:
        existing_hosts = []

    for host in hosts:
        domain_name = host["domain"]
        if forward_mode == "container":
            forward_host = host["forward"]
        else:
            # "host.docker.internal", or an IP/hostname for a REMOTE NPM
            # (NPM_MODE=remote): the remote server forwards to this host.
            forward_host = forward_mode
        existing = client.find_host(existing_hosts, domain_name)
        host_id = str(existing.get("id")) if existing else None
        client.upsert_proxy_host(token, host_id, domain_name, forward_host,
                                 host["port"], cert_id or None,
                                 host["websockets"], dry_run=args.dry_run,
                                 host_fields=host_fields)
        # Keep DNS in sync: write the A record for the subdomain when the
        # forward target is an IP and TSIG credentials are configured.
        if tsig and is_ip_address(forward_host):
            dns_upsert_a(tsig, domain_name, forward_host,
                         dry_run=args.dry_run)

    print("")
    print("Done. First point DNS at this host:  *.%s  and %s  ->  <public IP>"
          % (domain, domain))
    print("Then open https://%s (main interface) or https://app.%s (Homarr)"
          % (domain, domain))
    print("NPM admin UI: https://admin.%s  (or http://<host>:81)" % domain)


if __name__ == "__main__":
    main()