#!/usr/bin/env python3
"""jellyfin-tls.py — the certificate Jellyfin serves, and the file behind it.

WHY THIS EXISTS
---------------
`media.innotel.us` is published at the edge, and the edge terminates TLS with the
Cerulean-issued certificate for that name (`*.innotel.us` before 2026-09-23, a
dedicated `media.innotel.us` certificate since). Jellyfin is also *asked* to
serve its own HTTPS listener with that same material, because anything that
reaches the app directly — a client on the LAN, the compose network, an operator
following a redirect — should see our certificate and not a self-signed one.

That listener is the part that fails silently, and both failures were measured
on the media host on 2026-09-23:

  * **The file has to be PKCS#12.** `network.xml`'s `CertificatePath` was pointed
    at the PEM Cerulean hands out. Jellyfin started, logged nothing, and simply
    never opened 8920 — `Connection refused`, no error anywhere. Converting the
    material to a `.pfx` (empty password) brought it up on the same restart. This
    is the trap worth a check, because "HTTPS is on" reads as true in the config
    file while nothing is listening.
  * **The file has to be readable by the user Jellyfin runs as.** A root-owned
    `600` file at the path reproduces exactly the same no-listener, no-error
    state. The container runs as uid 1000 in this deployment, so the check
    compares the file's mode and owner against the container's own user rather
    than against whoever is running this script.
  * **The certificate is a snapshot, and the estate renews certificates on a
    timer.** Cerulean renews `media.innotel.us` 90 days at a time (announced as
    Let's Encrypt's ~60-day policy) and pushes the result to the edge
    automatically; nothing pushes it *into* this filesystem. So the file on disk
    is the certificate that was current the day it was installed, and the day it
    expires the app starts serving an expired one. `--check` compares the
    certificate the listener actually serves against the `.pem` installed beside
    it, and reports the approach of `notAfter` before it arrives, so the drift is
    a finding here and not an outage on a phone.

WHAT IS ASSERTED
----------------
`--check` (the default, and read-only) judges the configuration file and then
the listener itself — the second is what makes the first trustworthy, since
neither failure above is visible in the file:

  1. `{config}/network.xml` exists and has `EnableHttps` true with a
     `CertificatePath` set;
  2. the path resolves through the appdata mount to a file that exists, is
     PKCS#12, and is readable by the container's user;
  3. the listener answers `https://{name}:{port}/System/Info/Public` with 200 and
     a *trusted* chain (`ssl_verify_result` 0 — an expired or self-signed
     certificate is a finding, not a warning);
  4. the leaf it serves covers `--name`, and its SHA-256 fingerprint matches the
     `.pem` beside the PKCS#12 file when one is there (the renewal half);
  5. `notAfter` is further away than `--warn-days` (default 30).

`--apply` re-installs the material: it converts a PEM (certificate + key, the
shape Cerulean's material endpoint returns) into the PKCS#12 file, makes it
readable by the container's user, and points `network.xml` at it with an empty
password — then prints the restart, which it does not do itself.

`--renew` is that same install with the fetching done for you, because the
renewal Cerulean performs lands somewhere this stack can already reach: the edge.
Cerulean pushes a renewed certificate to NPM and nowhere else, so NPM is where
the current material always is — and this stack already holds the credentials for
it (`NPM_BASE_URL`, `NPM_ADMIN_EMAIL`, `NPM_ADMIN_PASSWORD`, the values
`npm-proxy-hosts.py` drives the remote NPM with). `--renew` reads the certificate
covering the name back from the edge (an exact match before a covering wildcard,
newest expiry first), writes it beside the bundle as the `.pem` that `--check`
compares against, and installs it — which is what turns "a renewal is still a
manual step here" into one command. It refuses to install nothing: if the edge
holds no certificate for the name, that is the finding.

It is also a no-op when there is nothing to do. Renewing is a *stream* of the
same material until the estate actually renews, so `--renew` compares what the
edge holds against what the listener is serving and, when they are the same,
says so and installs nothing — which is what makes it safe to run on a timer
(and to run with `--restart`, below) rather than a weekly restart of a media
server for nothing.

THE RESTART IS PART OF THE INSTALL
----------------------------------
Jellyfin reads `network.xml` and the certificate **at startup only**, so an
install is not visible on 8920 until the app is restarted. `--restart` does that
itself with `docker restart` — no API key to have configured, and it still works
when the app is not answering HTTP, which is one of the states an install is run
from. A restart docker refuses is a finding, not a note: the material on disk is
new, `--check` compares it against the certificate the listener returns, and an
un-restarted app is exactly that mismatch.

The schedule is `systemd/monarch-jellyfin-tls.timer`, which runs `--renew
--restart` weekly — the write half of what `monarch-drift-check` watches
read-only every six hours.

USAGE
-----
    python3 scripts/jellyfin-tls.py --check              # report only (default)
    python3 scripts/jellyfin-tls.py --renew              # fetch from the edge and install
    python3 scripts/jellyfin-tls.py --renew --restart    # ... and restart Jellyfin (the timer)
    python3 scripts/jellyfin-tls.py --apply --pem /tmp/media.innotel.us.pem

Runs on the media host (it needs `docker` to reach the container's own listener,
the way `clipbucket-library.py` does) or anywhere the appdata mount and the
docker CLI are both present. Config via environment:

    APPDATA                 host appdata root (default /docker/appdata)
    JELLYFIN_CONTAINER      container name (default jellyfin)
    JELLYFIN_TLS_NAME       name the certificate must cover (default media.innotel.us)
    JELLYFIN_HTTPS_PORT     the listener's port (default 8920)

`--renew` also reads the repo's `.env` for the edge's address and credentials
(`NPM_BASE_URL`, `NPM_ADMIN_EMAIL`, `NPM_ADMIN_PASSWORD`), the same values
`npm-proxy-hosts.py` drives the remote NPM with; a real environment variable
wins over the file.

Paths:

    config          $APPDATA/jellyfin/network.xml            (Jellyfin's /config)
    certificate     /config/ssl/<name>.pfx  in the container
                    $APPDATA/jellyfin/ssl/<name>.pfx         on the host

Exit codes: 0 = the listener is up on our certificate; 1 = it is not (a finding
is printed); 2 = cannot look (no appdata, no docker, no container, or the
container has no curl/openssl) — which is not a drifted host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_APPDATA = "/docker/appdata"
DEFAULT_CONTAINER = "jellyfin"
DEFAULT_NAME = "media.innotel.us"
DEFAULT_HTTPS_PORT = 8920
DEFAULT_WARN_DAYS = 30

# What Jellyfin resolves inside the container, and where that is on this host.
CONTAINER_CONFIG = "/config"
CONTAINER_SSL_DIR = "/config/ssl"
PKCS12_SUFFIXES = (".pfx", ".p12")

# openssl on the host and inside the container: the fingerprint/dates of a PEM,
# and of the certificate a listener serves.
FINGERPRINT_RE = re.compile(r"sha256 Fingerprint=\s*([0-9A-Fa-f:]{90,})")
SUBJECT_CN_RE = re.compile(r"(?:^|\n)subject=.*?\bCN\s*=\s*([^,\n]+)")
SAN_DNS_RE = re.compile(r"DNS:([^,\s]+)")
NOT_AFTER_RE = re.compile(r"notAfter=(.+)")


class CantTell(Exception):
    """The deployment could not be judged from here (exit 2)."""


def container_path_to_host(container_path: str, config_dir: Path,
                           container_config: str = CONTAINER_CONFIG) -> Path | None:
    """The host path a container path under the appdata mount maps to, or None.

    `CertificatePath` is resolved *inside the container*, so the only way to look
    at the file from here is to map it back through the same mount — and a path
    outside it is not a file this check can judge.
    """
    prefix = container_config.rstrip("/") + "/"
    if not container_path.startswith(prefix):
        return None
    return config_dir / container_path[len(prefix):]


def network_file(config_dir: Path) -> Path:
    return config_dir / "network.xml"


def read_network(config_dir: Path) -> dict[str, str]:
    """The three settings that decide whether HTTPS works, as written."""
    path = network_file(config_dir)
    if not path.is_file():
        raise FileNotFoundError(path)
    root = ET.parse(path).getroot()

    def text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    return {"enable_https": text("EnableHttps"),
            "certificate_path": text("CertificatePath"),
            "certificate_password": text("CertificatePassword")}


def write_network(config_dir: Path, certificate_path: str, password: str = "") -> Path:
    """Point `network.xml` at a PKCS#12 file, leaving every other setting alone.

    The settings Jellyfin does not manage through the API are the ones an
    operator set by hand (KnownProxies, the ports, the published-URI rules), so
    this rewrites only the three fields it owns.
    """
    path = network_file(config_dir)
    tree = ET.parse(path)
    root = tree.getroot()

    def set_text(tag: str, value: str) -> None:
        node = root.find(tag)
        if node is None:
            node = ET.SubElement(root, tag)
        node.text = value

    set_text("EnableHttps", "true")
    set_text("CertificatePath", certificate_path)
    set_text("CertificatePassword", password)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def docker(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    if shutil.which("docker") is None:
        raise CantTell("docker is not available here")
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              input=input_text, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise CantTell(f"docker could not run: {error}") from error


def container_user(container: str) -> tuple[int, int] | None:
    """The uid/gid the container runs as, or None when the image default applies.

    The image default is root, and a root-owned file is readable by it — but this
    deployment runs Jellyfin as uid 1000, which is exactly why a root-owned `600`
    file leaves 8920 closed with no error. Reading the container's own answer
    beats assuming one.
    """
    proc = docker(["inspect", container, "--format", "{{.Config.User}}"])
    if proc.returncode != 0:
        raise CantTell(f"container '{container}' is not inspectable (docker compose up -d {container})")
    spec = (proc.stdout or "").strip()
    if not spec:
        return None
    parts = spec.split(":")
    try:
        uid = int(parts[0])
    except ValueError:
        return None  # a name, not a uid: resolved by the image, not worth guessing at
    gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else uid
    return uid, gid


def readable_by(path: Path, container_user_id: tuple[int, int] | None) -> tuple[bool, str]:
    """Whether the container's user can read the file, and why not."""
    stat = path.stat()
    mode = stat.st_mode & 0o777
    if container_user_id is None:
        # Root (the image default) reads anything; so does any uid with the owner bit.
        return True, "the container runs as the image default (root)"
    uid, gid = container_user_id
    if uid == 0:
        return True, "the container runs as root"
    if stat.st_uid == uid and mode & 0o400:
        return True, "owned by the container's uid"
    if stat.st_gid == gid and mode & 0o040:
        return True, "readable by the container's group"
    if mode & 0o004:
        return True, "world-readable"
    return False, (f"mode {mode:04o}, owned by uid {stat.st_uid}:gid {stat.st_gid}, "
                   f"but the container runs as uid {uid}:gid {gid}")


def probe_https(container: str, name: str, port: int) -> dict[str, object]:
    """Ask the container's own listener for the certificate it serves.

    Reached with `docker exec` rather than over the network on purpose: this
    deployment publishes Jellyfin's HTTP port to loopback only and does not
    publish 8920 at all (docker-compose.yml, 2026-09-18), so the listener is
    real and unreachable from the host — and `--resolve` makes the request carry
    the name, which is what a certificate is judged against.
    """
    # The certificate first, with openssl rather than curl: openssl presents the
    # name as SNI and hands back whatever certificate comes back, while curl's
    # request can be refused at the *HTTP* layer for a Host the server does not
    # serve (Jellyfin answers 400 with its error page). Reading the certificate
    # independently is what lets a wrong name be reported as "this certificate
    # does not cover it" instead of "nothing answered".
    cert = docker(["exec", container, "sh", "-c",
                   f"echo | openssl s_client -connect 127.0.0.1:{port} "
                   f"-servername {name} 2>/dev/null | openssl x509 -noout "
                   f"-fingerprint -sha256 -subject -enddate -ext subjectAltName"])
    if cert.returncode != 0 or "Fingerprint" not in (cert.stdout or ""):
        detail = (cert.stderr or "").strip().splitlines()
        raise CantTell("the HTTPS listener served no certificate: "
                       f"{detail[-1] if detail else 'nothing answered on port ' + str(port)}")

    text = cert.stdout or ""
    fingerprint = FINGERPRINT_RE.search(text)
    not_after = NOT_AFTER_RE.search(text)
    cn = SUBJECT_CN_RE.search(text)

    url = f"https://{name}:{port}/System/Info/Public"
    http = docker(["exec", container, "curl", "-sS", "--resolve",
                   f"{name}:{port}:127.0.0.1", "-o", "/dev/null",
                   "-w", "%{http_code} %{ssl_verify_result}", url])
    status: int | None = None
    verify: int | None = None
    detail = ""
    if http.returncode == 0:
        fields = (http.stdout or "").strip().split()
        if len(fields) >= 2:
            status, verify = int(fields[0]), int(fields[1])
        else:
            detail = f"unexpected probe output {(http.stdout or '').strip()!r}"
    else:
        lines = (http.stderr or http.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else "no output"

    return {
        "status": status,
        "verify": verify,
        "detail": detail,
        "fingerprint": normalise_fingerprint(fingerprint.group(1)) if fingerprint else "",
        "not_after": not_after.group(1).strip() if not_after else "",
        "cn": cn.group(1).strip() if cn else "",
        "sans": [s for s in SAN_DNS_RE.findall(text)],
    }


def pem_identity(pem_path: Path) -> dict[str, str] | None:
    """The fingerprint and expiry of a PEM's leaf, via openssl, or None.

    Used to answer the renewal question the listener alone cannot: is the
    certificate being served still the material that was installed here, or has
    the estate renewed behind it and left this file behind?
    """
    if not pem_path.is_file():
        return None
    if shutil.which("openssl") is None:
        return None
    try:
        proc = subprocess.run(["openssl", "x509", "-in", str(pem_path), "-noout",
                               "-fingerprint", "-sha256", "-enddate"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout or ""
    fingerprint = FINGERPRINT_RE.search(text)
    not_after = NOT_AFTER_RE.search(text)
    return {"fingerprint": normalise_fingerprint(fingerprint.group(1)) if fingerprint else "",
            "not_after": not_after.group(1).strip() if not_after else ""}


def normalise_fingerprint(value: str) -> str:
    return value.replace(":", "").replace(" ", "").upper()


# ── The renewal source: the edge, which Cerulean already pushes to ────────────
#
# Cerulean renews this certificate on a timer and pushes the result to NPM
# automatically and nowhere else, so the edge is the one place that always holds
# the current material. This stack already holds the credentials for it - the
# same `NPM_BASE_URL`, `NPM_ADMIN_EMAIL` and `NPM_ADMIN_PASSWORD` that
# `npm-proxy-hosts.py` drives the remote NPM with - so the read-back needs no
# credential of its own.

def load_env(path: Path) -> None:
    """Parse KEY=VALUE lines from a `.env` into os.environ, never overwriting.

    The edge's credentials live in this stack's `.env`, and the estate's
    convention is the script reads that file itself rather than making the
    operator source anything first (`npm-proxy-hosts.py` does the same). A real
    environment variable still wins, so a caller can point this elsewhere.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def repo_env_file() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def npm_request(base: str, path: str, token: str | None = None,
                body: dict | None = None, method: str = "GET") -> object:
    """One call against the edge's API, with every failure a "cannot tell"."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        raise CantTell(f"the edge answered {error.code} for {path}: "
                       f"{detail or error.reason}") from error
    except (urllib.error.URLError, OSError) as error:
        raise CantTell(f"the edge could not be reached at {base}: {error}") from error
    try:
        return json.loads(payload or b"null")
    except json.JSONDecodeError as error:
        raise CantTell(f"the edge answered {path} with something that is not JSON") from error


def npm_token(base: str, identity: str, secret: str) -> str:
    token = npm_request(base, "/api/tokens", method="POST",
                        body={"identity": identity, "secret": secret})
    value = token.get("token") if isinstance(token, dict) else None
    if not value:
        raise CantTell("the edge refused the admin credentials (no token)")
    return value


def select_certificate(entries: list, name: str) -> dict | None:
    """The edge's certificate for `name`: exact before wildcard, newest first.

    The edge holds every name in the estate, wildcards included, so "the first
    entry that mentions the domain" would happily pick the `*.innotel.us` that
    used to cover this name over the dedicated certificate it has now. The
    ranking is explicit for that reason.
    """
    best = None
    for entry in entries or []:
        names = [str(n) for n in (entry.get("domain_names") or [])]
        if name in names:
            rank = 1
        elif any(covers([n], name) for n in names):
            rank = 0
        else:
            continue
        key = (rank, str(entry.get("expires_on") or ""))
        if best is None or key > best[0]:
            best = (key, entry)
    return best[1] if best else None


def npm_material(base: str, token: str, cert_id: int) -> str:
    """A certificate's PEM (certificate + key) as the edge stores it."""
    record = npm_request(base, f"/api/nginx/certificates/{cert_id}", token) or {}
    meta = record.get("meta") or {}
    certificate = record.get("certificate") or meta.get("certificate") or ""
    key = record.get("certificate_key") or meta.get("certificate_key") or ""
    if not certificate or not key:
        # A Let's Encrypt-managed certificate keeps its material with the CA, not
        # in the row, so there is nothing to install from this end - and that is
        # a thing to say out loud rather than crash on.
        raise CantTell(f"the edge's certificate {cert_id} keeps no key "
                       f"(provider {record.get('provider')!r}), so there is nothing "
                       "to install from it")
    return certificate.rstrip() + "\n" + key.rstrip() + "\n"


def covers(sans: list[str], name: str, cn: str = "") -> bool:
    """Whether a certificate's names cover `name`, wildcards included.

    A wildcard covers exactly one label, which is why `*.innotel.us` covered
    `media.innotel.us` until this deployment gave the name its own certificate on
    2026-09-23 — and why `*.capstone.innotel.us` covers `api.capstone.innotel.us`
    but not `backend.api.capstone.innotel.us`, the host that had no covering
    certificate until it was issued one the same day.
    """
    candidates = [s for s in sans if s]
    if cn and not candidates:
        candidates = [cn]
    labels = name.split(".")
    for candidate in candidates:
        if candidate == name:
            return True
        if candidate.startswith("*."):
            if len(labels) == len(candidate.split(".")) and labels[1:] == candidate.split(".")[1:]:
                return True
    return False


def expiry_of(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.strptime(value, "%b %d %H:%M:%S %Y %Z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def days_until(moment: datetime | None, now: datetime | None = None) -> int | None:
    if moment is None:
        return None
    now = now or datetime.now(timezone.utc)
    return int((moment - now).total_seconds() // 86400)


def check(args) -> int:
    config_dir = Path(args.appdata) / "jellyfin"
    if not config_dir.is_dir():
        raise CantTell(f"no Jellyfin appdata at {config_dir} — this is not the media host "
                       "(set APPDATA, or pass --appdata)")

    # The container, before anything else, because this check is about the
    # listener a *running* Jellyfin serves. The edge host carries a leftover
    # `/docker/appdata/jellyfin` from before the media stack moved (no
    # `network.xml`, nothing listening), and judging that directory would report
    # a drifted deployment on a host that never ran one. A container that is not
    # there is "cannot look", not a finding.
    user = container_user(args.container)

    if not network_file(config_dir).is_file():
        return report([f"{network_file(config_dir)} does not exist, so Jellyfin serves HTTP only"
                       " — run this with --apply once its material is in place"])

    findings: list[str] = []
    try:
        settings = read_network(config_dir)
    except ET.ParseError as error:
        return report([f"{network_file(config_dir)} is not readable XML: {error}"])

    if settings["enable_https"].lower() != "true":
        findings.append("EnableHttps is not true — the HTTPS listener is off")
    if not settings["certificate_path"]:
        findings.append("CertificatePath is empty — there is no certificate to serve")
    else:
        mapped = container_path_to_host(settings["certificate_path"], config_dir)
        if mapped is None:
            findings.append(f"CertificatePath {settings['certificate_path']!r} is not under "
                            f"{CONTAINER_CONFIG}, so it cannot be judged from here")
        elif not mapped.is_file():
            # The measured silence: a path that names nothing is not an error the
            # server reports, it just never opens the port.
            findings.append(f"CertificatePath names {settings['certificate_path']}, but "
                            f"{mapped} does not exist")
        else:
            if mapped.suffix.lower() not in PKCS12_SUFFIXES:
                findings.append(f"{mapped.name} is not PKCS#12 — Jellyfin loads "
                                f"{'/'.join(PKCS12_SUFFIXES)} and leaves 8920 closed on a PEM")
            readable, why = readable_by(mapped, user)
            if not readable:
                findings.append(f"{mapped.name} is not readable by Jellyfin ({why})")

    # The listener is the only thing that proves the file was loaded, so it is
    # judged even when the configuration above already has something to say.
    served = probe_https(args.container, args.name, args.https_port)
    if served["status"] is None:
        findings.append(f"the certificate came back, but https://{args.name}:"
                        f"{args.https_port} did not answer ({served['detail']})")
    elif served["status"] != 200:
        findings.append(f"https://{args.name}:{args.https_port}/System/Info/Public answered "
                        f"{served['status']}")
    if served["verify"] not in (0, None):
        # 0 is "the chain verified". Anything else is an untrusted certificate:
        # expired, self-signed, or Jellyfin's own generated one.
        findings.append(f"the chain on https://{args.name}:{args.https_port} does not verify "
                        f"(ssl_verify_result {served['verify']})")
    if not covers(list(served["sans"]), args.name, str(served["cn"])):
        findings.append(f"the served certificate does not cover {args.name} "
                        f"(SAN {served['sans'] or 'none'}, CN {served['cn'] or 'none'})")

    expected = pem_identity(Path(args.appdata) / "jellyfin" / "ssl" / f"{args.name}.pem")
    if expected and served["fingerprint"] and expected["fingerprint"] != served["fingerprint"]:
        findings.append(f"the listener serves {served['fingerprint']} but "
                        f"{args.name}.pem holds {expected['fingerprint']} — the file on disk is "
                        "not the material that was installed (renew with --apply)")

    remaining = days_until(expiry_of(str(served["not_after"])))
    if remaining is not None and remaining < args.warn_days:
        state = "has expired" if remaining < 0 else f"expires in {remaining} day(s)"
        findings.append(f"the served certificate {state} ({served['not_after']}) — renew it in "
                        f"Cerulean, then re-run --apply with the new material")

    if findings:
        return report(findings)
    print(f"ok: Jellyfin serves {args.name} over TLS on {args.https_port} "
          f"({served['fingerprint'][:16]}…, {served['not_after']})")
    return 0


def report(findings: list[str]) -> int:
    for finding in findings:
        print(f"jellyfin-tls: {finding}", file=sys.stderr)
    return 1


def apply(args) -> int:
    """Install PEM material as the PKCS#12 file Jellyfin loads, and point at it."""
    config_dir = Path(args.appdata) / "jellyfin"
    if not network_file(config_dir).is_file():
        raise CantTell(f"no {network_file(config_dir)} to update — this is not the media host")

    pem = Path(args.pem) if args.pem else config_dir / "ssl" / f"{args.name}.pem"
    return install_pem(config_dir, pem, args)


def already_serving(args, fingerprint: str) -> bool:
    """Whether the listener is already serving `fingerprint`, and knows it is.

    Only a positive answer counts. A listener that cannot be reached, or a
    container with no openssl to ask, leaves the question open — and an open
    question installs the material for a second time rather than skipping work
    that was actually needed.
    """
    if not fingerprint:
        return False
    try:
        served = probe_https(args.container, args.name, args.https_port)
    except CantTell:
        return False
    return bool(served.get("fingerprint")) and served["fingerprint"] == fingerprint


def renew(args) -> int:
    """Fetch the certificate covering the name from the edge, and install it.

    The renewal Cerulean performs lands on the edge and nowhere else, so the copy
    this listener should be serving is the copy the edge is serving. Reading it
    back and installing it is the whole step that used to be manual.

    Renewal is a *stream* of the same material until the estate renews, so the
    step is skipped when there is nothing to do: the edge holding what the
    listener already serves is "already current", printed and exited 0 without an
    install and without a restart. That is what makes this safe on a timer.
    """
    config_dir = Path(args.appdata) / "jellyfin"
    if not network_file(config_dir).is_file():
        raise CantTell(f"no {network_file(config_dir)} to update — this is not the media host")

    load_env(repo_env_file())
    base = os.environ.get("NPM_BASE_URL", "").strip()
    identity = os.environ.get("NPM_ADMIN_EMAIL", "").strip()
    secret = os.environ.get("NPM_ADMIN_PASSWORD", "").strip()
    if not base:
        raise CantTell("NPM_BASE_URL is not set, so the edge cannot be asked for the "
                       "certificate (it is in this stack's .env)")
    if not identity or not secret:
        raise CantTell("NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD are not set, so the edge "
                       "cannot be asked for the certificate")

    token = npm_token(base, identity, secret)
    chosen = select_certificate(npm_request(base, "/api/nginx/certificates", token), args.name)
    if chosen is None:
        return report([f"the edge ({base}) holds no certificate covering {args.name} — "
                       "issue it in Cerulean (Certificates → the name → Issue) first"])

    pem = config_dir / "ssl" / f"{args.name}.pem"
    # What was here before the fetch, so "the edge renewed" and "the edge answered
    # with the certificate it already had" can be told apart. Both identities need
    # openssl; without it there is no way to compare, so the install happens.
    before = pem_identity(pem)
    pem.parent.mkdir(parents=True, exist_ok=True)
    pem.write_text(npm_material(base, token, chosen["id"]))
    # This file is a private key: readable by this process and the container's
    # user, and nothing else, the way --apply leaves the bundle it is built from.
    pem.chmod(0o600)

    after = pem_identity(pem)
    if before and after and before["fingerprint"] == after["fingerprint"] \
            and already_serving(args, after["fingerprint"]):
        print(f"{args.name} is already current: the edge's certificate {chosen['id']} is "
              f"the one the listener serves (expires {after['not_after'] or 'unknown'}) "
              "— nothing to install")
        return 0

    print(f"renewed {args.name} from the edge's certificate {chosen['id']} "
          f"(expires {chosen.get('expires_on') or 'unknown'})")
    return install_pem(config_dir, pem, args)


def install_pem(config_dir: Path, pem: Path, args) -> int:
    """Install PEM material as the PKCS#12 file Jellyfin loads, and point at it."""
    if not pem.is_file():
        raise CantTell(f"no PEM material at {pem} (pass --pem)")
    if shutil.which("openssl") is None:
        raise CantTell("openssl is not available here to build the PKCS#12 file")

    ssl_dir = config_dir / "ssl"
    ssl_dir.mkdir(parents=True, exist_ok=True)
    bundle = ssl_dir / f"{args.name}.pfx"
    # An empty password is what this deployment's file has, and Jellyfin reads an
    # empty `CertificatePassword` as "no password" — so the two have to agree.
    build = subprocess.run(["openssl", "pkcs12", "-export", "-in", str(pem),
                            "-out", str(bundle), "-passout", "pass:"],
                           capture_output=True, text=True)
    if build.returncode != 0:
        detail = (build.stderr or "").strip().splitlines()
        print(f"jellyfin-tls: openssl could not build {bundle}: "
              f"{detail[-1] if detail else 'no output'}", file=sys.stderr)
        return 1
    bundle.chmod(0o600)

    # Ownership is part of the install, not a nicety: the container reads this
    # file as its own user, and a root-owned 600 file leaves 8920 closed without
    # an error. When the mode/uid cannot be set from here, that is the finding.
    user = container_user(args.container)
    if user and user[0] != 0:
        try:
            os.chown(bundle, user[0], user[1])
        except OSError as error:
            print(f"jellyfin-tls: {bundle} is written, but could not be given to uid "
                  f"{user[0]} ({error}) — run this as root, or chown it before restarting",
                  file=sys.stderr)
            return 1

    container_path = f"{CONTAINER_SSL_DIR}/{bundle.name}"
    write_network(config_dir, container_path, "")
    print(f"installed {bundle} ({bundle.stat().st_size} bytes) and pointed network.xml at "
          f"{container_path}")

    # The last step, and the one that makes the install visible: the certificate is
    # read at startup, so until the app is restarted nothing is serving it.
    if getattr(args, "restart", False):
        return 0 if restart_container(args.container) else 1
    print("restart Jellyfin so it loads the certificate:  docker restart "
          f"{args.container}")
    return 0


def restart_container(container: str) -> bool:
    """Restart so the app re-reads `network.xml`, and say whether it happened.

    `docker restart` rather than the app's own `/System/Restart` endpoint: it
    needs no API key to be configured, and it still works when the app is not
    answering HTTP — which is one of the states an operator runs the install
    from. A restart docker will not perform is a finding, not a note: the
    material on disk is new, `--check` compares it against the certificate the
    listener returns, and an un-restarted app is exactly the mismatch it reports.
    """
    proc = docker(["restart", container])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        print(f"jellyfin-tls: the install is written, but {container} was not restarted "
              f"({detail[-1] if detail else 'docker restart failed'}) — it still serves the "
              "certificate from before this install until it is", file=sys.stderr)
        return False
    print(f"restarted {container} so it loads the certificate")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only (default)")
    parser.add_argument("--apply", action="store_true",
                        help="install the PEM material as PKCS#12 and point network.xml at it")
    parser.add_argument("--renew", action="store_true",
                        help="fetch the current certificate from the edge (NPM) and install it")
    parser.add_argument("--restart", action="store_true",
                        help="restart the container after installing, so it re-reads "
                             "network.xml (the certificate is read at startup only)")
    parser.add_argument("--pem", default="", help="PEM material (certificate + key) for --apply")
    parser.add_argument("--appdata", default=os.environ.get("APPDATA", DEFAULT_APPDATA),
                        help=f"host appdata root (default {DEFAULT_APPDATA})")
    parser.add_argument("--container", default=os.environ.get("JELLYFIN_CONTAINER", DEFAULT_CONTAINER),
                        help=f"Jellyfin container (default {DEFAULT_CONTAINER})")
    parser.add_argument("--name", default=os.environ.get("JELLYFIN_TLS_NAME", DEFAULT_NAME),
                        help=f"name the certificate must cover (default {DEFAULT_NAME})")
    parser.add_argument("--https-port", type=int,
                        default=int(os.environ.get("JELLYFIN_HTTPS_PORT", DEFAULT_HTTPS_PORT)),
                        help=f"the HTTPS listener's port (default {DEFAULT_HTTPS_PORT})")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS,
                        help=f"report a certificate this close to expiry (default {DEFAULT_WARN_DAYS})")
    args = parser.parse_args(argv)

    try:
        if args.apply:
            return apply(args)
        if args.renew:
            return renew(args)
        return check(args)
    except CantTell as error:
        print(f"jellyfin-tls: cannot judge this host — {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
