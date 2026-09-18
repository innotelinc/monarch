#!/usr/bin/env python3
"""Make every download land in the downloads tree, not in a library.

WHY THIS EXISTS. Two halves have to agree for a download to be filed correctly,
and each one fails invisibly on its own:

  * each *arr tells its qBittorrent client a CATEGORY NAME. Servarr spells it
    after the media type (`tvCategory`, `movieCategory`, `musicCategory`) - there
    is no plain `category` field - so a client created with `category` set is
    created with nothing set, and the app keeps whatever name it was given
    before;
  * qBittorrent maps that name to a SAVE PATH. A name qBittorrent does not know
    saves to the default path. A name it does know whose path points at
    /data/media/<type> saves straight into the library - which is why each app
    warns "Download client qBittorrent places downloads in the root folder
    /data/media/<type>", and why an unfinished album appears in the music
    library for Jellyfin to scan.

The repo's `init/init.py` now reconciles both, but only while it runs: a
deployment that was configured by hand before that fix keeps the hand-set
categories forever, because the arr only reports the mismatch as a *warning* on
its own Health page. This is the side that can be run on a live host, that
restarts nothing, and that can be checked on a timer.

WHAT IT ASKS AND WHAT IT DOES. Both halves come from the invariants manifest
(`/docker/appdata/init/invariants.json`, written by monarch-init) so there is one
source of truth and no second copy of the map here: `qbt.category_paths` is the
name -> path map, `arr_apps[].category` is the name each app must send. Applying
corrects a drifted path, creates a missing category, removes a stray one (a
duplicate such as `radarr` next to `movies`, or a leftover whose path sits inside
a library), and PUTs the arr's corrected download client.

Usage:
    scripts/arr-download-categories.py --check     # exit 2 if anything drifted
    scripts/arr-download-categories.py             # correct it (restarts nothing)
    scripts/arr-download-categories.py --dry-run   # report, change nothing

Credentials come from MONARCH_USERNAME/MONARCH_PASSWORD (the same pair
monarch-seed writes into qBittorrent's WebUI), or from .env in the repo root.

Exit codes: 0 everything agrees (or, applying, corrected); 1 qBittorrent, an
*arr or the manifest could not be read; 2 --check found drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path(os.environ.get("MONARCH_INVARIANTS",
                                       "/docker/appdata/init/invariants.json"))
DEFAULT_APPDATA = Path("/docker/appdata")
DEFAULT_QBT = "http://localhost:8080"
CATEGORY_FIELDS = ("tvCategory", "movieCategory", "musicCategory", "category")


class Drift(RuntimeError):
    pass


def call(url: str, method: str = "GET", body: dict | None = None, opener=None,
         raw_form: bool = False, headers: dict | None = None, timeout: float = 20.0):
    """One request; returns (status, parsed-or-text)."""

    def send(opener_call):
        payload = None
        if body is not None:
            payload = urllib.parse.urlencode(body).encode() if raw_form else json.dumps(body).encode()
        request = urllib.request.Request(url, data=payload, method=method)
        if payload and not raw_form:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with opener_call(request, timeout=timeout) as response:
            raw = response.read()
        try:
            return response.status, json.loads(raw or b"null")
        except ValueError:
            return response.status, raw[:400].decode("utf-8", "replace")

    try:
        # An OpenerDirector is not a context manager; opening one per call is the
        # price of having no session to reuse.
        return send(opener.open if opener is not None
                    else urllib.request.build_opener().open)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"null")
        except ValueError:
            return error.code, raw[:400].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        raise Drift(str(error)) from error


def category_field(resource: dict) -> dict | None:
    """The field holding this app's download-client category, or None."""
    fields = {f.get("name"): f for f in resource.get("fields") or []}
    for name in CATEGORY_FIELDS:
        if name in fields:
            return fields[name]
    return None


def path_drift(live: dict, want: dict[str, str | None]) -> list[tuple[str, str | None, str]]:
    """(name, live path, wanted path) for every category missing or at a wrong path.

    A wanted path of None means the manifest did not record one (it predates the
    map): the category still has to exist, and its path is simply not something
    this can judge.
    """
    out = []
    for name, path in sorted(want.items()):
        current = (live.get(name) or {}).get("savePath")
        if path is None:
            if name not in live:
                out.append((name, None, path))
            continue
        if current != path:
            out.append((name, current, path))
    return out


def strays(live: dict, want: dict[str, str]) -> list[str]:
    """Names qBittorrent holds that the manifest does not name."""
    return sorted(set(live) - set(want))


def held(strays_: list[str], torrents: list[dict]) -> dict[str, int]:
    """How many torrents each stray category still files.

    A stray that still holds torrents must NOT be removed: qBittorrent strips the
    category from every torrent filed under it, so removing first leaves those
    downloads unlabelled and invisible to the app that queued them (seeding fine,
    gone from its queue). They have to be moved to the name their app now sends
    first - which is what migrate_torrents does.
    """
    counts = {name: 0 for name in strays_}
    for torrent in torrents or []:
        category = torrent.get("category") or ""
        if category in counts:
            counts[category] += 1
    return {name: n for name, n in counts.items() if n}


def api_key(appdata: Path, svc: str) -> str:
    config = appdata / svc / "config.xml"
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                      config.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise Drift(f"no <ApiKey> in {config}")
    return match.group(1)


def credentials(repo: Path) -> tuple[str, str]:
    """MONARCH_USERNAME/PASSWORD from the environment, else from .env."""
    user = os.environ.get("MONARCH_USERNAME", "")
    password = os.environ.get("MONARCH_PASSWORD", "")
    if user and password:
        return user, password
    env = repo / ".env"
    try:
        text = env.read_text(encoding="utf-8")
    except OSError:
        return user, password
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return user or values.get("MONARCH_USERNAME", ""), \
        password or values.get("MONARCH_PASSWORD", "")


def qbt_session(base: str, user: str, password: str):
    """A cookie-jar opener logged into the WebUI, or None with the reason."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, text = call(f"{base}/api/v2/auth/login", method="POST",
                        body={"username": user, "password": password},
                        opener=opener, raw_form=True)
    # >= 5.2 answers 204 with an empty body; older versions 200 with "Ok.".
    if not (status in (200, 204) and str(text).strip() in ("", "Ok.")):
        return None, f"WebUI login failed (HTTP {status})"
    return opener, ""


def arr_targets(manifest: dict) -> list[tuple[str, str, str, str]]:
    """(svc, port, api, category) for every *arr the manifest names."""
    out = []
    for app in manifest.get("arr_apps") or []:
        out.append((app["svc"], int(app["port"]), app["api"], app["category"]))
    return out


def migrate_torrents(base: str, opener, renames: list[tuple[str, str, str]],
                     dry_run: bool) -> list[str]:
    """Re-file torrents under the category their app now sends.

    A category change is not only a setting: the downloads already filed under
    the old name are found by the app *by that name*. Pruning the old category
    without moving them leaves them unlabelled and invisible to the app that
    queued them - seeding fine, and absent from its queue. So the rename is
    carried through to the torrents themselves.

    `renames` is (app, previous category, new category) for every app that was
    corrected.
    """
    findings: list[str] = []
    for svc, previous, current in renames:
        if not previous or previous == current:
            continue
        status, torrents = call(f"{base}/api/v2/torrents/info?category={urllib.parse.quote(previous)}",
                                opener=opener)
        if status != 200 or not isinstance(torrents, list) or not torrents:
            continue
        hashes = [t.get("hash") for t in torrents if t.get("hash")]
        if not hashes:
            continue
        if dry_run:
            print(f"{svc:<9} {len(hashes)} torrent(s) filed under {previous!r} would move to {current!r}")
            continue
        status, _ = call(f"{base}/api/v2/torrents/setCategory", method="POST",
                         body={"hashes": "|".join(hashes), "category": current},
                         opener=opener, raw_form=True)
        if status in (200, 201, 204):
            print(f"{svc:<9} {len(hashes)} torrent(s) re-filed {previous!r} -> {current!r}")
        else:
            findings.append(f"{svc}: {len(hashes)} torrent(s) could not be re-filed out of "
                            f"{previous!r} (HTTP {status})")
    return findings


def check_arrs(manifest: dict, appdata: Path, apply_fix: bool, dry_run: bool) -> tuple[list[str], bool, list[tuple[str, str, str]]]:
    """Does each app's qBittorrent client carry the manifest's category?

    Returns the findings, whether every app could be read at all, and every
    category change that was made (or would be), so the torrents already filed
    under the old name can be moved with it.
    """
    findings: list[str] = []
    renames: list[tuple[str, str, str]] = []
    reachable = True
    for svc, port, api, want in arr_targets(manifest):
        base = f"http://localhost:{port}/api/{api}"
        try:
            key = api_key(appdata, svc)
        except (Drift, OSError) as error:
            findings.append(f"{svc}: {error}")
            reachable = False
            continue
        try:
            status, clients = call(f"{base}/downloadclient", headers={"X-Api-Key": key})
        except Drift as error:
            findings.append(f"{svc}: {error}")
            reachable = False
            continue
        if status != 200 or not isinstance(clients, list):
            findings.append(f"{svc}: downloadclient unreachable (HTTP {status})")
            reachable = False
            continue
        client = next((c for c in clients
                       if isinstance(c, dict) and c.get("implementation") == "QBittorrent"), None)
        if client is None:
            findings.append(f"{svc}: no qBittorrent download client")
            continue
        field = category_field(client)
        if field is None:
            findings.append(f"{svc}: this build's QBittorrent client has no category field")
            continue
        if field.get("value") == want:
            print(f"{svc:<9} files under {want!r}")
            continue
        if not apply_fix or dry_run:
            findings.append(f"{svc}: sends category {field.get('value')!r}, expected {want!r}")
            continue
        previous = field.get("value")
        renames.append((svc, previous, want))
        field["value"] = want
        # Servarr has no per-field endpoint: the whole resource travels back.
        status, _ = call(f"{base}/downloadclient/{client['id']}", method="PUT", body=client,
                         headers={"X-Api-Key": key})
        if status in (200, 202):
            print(f"{svc:<9} category {previous!r} -> {want!r}")
        else:
            findings.append(f"{svc}: could not set the category (HTTP {status})")
    return findings, reachable, renames


def check_qbt(manifest: dict, base: str, opener,
              apply_fix: bool, dry_run: bool) -> tuple[list[str], bool]:
    """Are qBittorrent's categories exactly the manifest's map?"""
    want, paths_known = manifest_categories(manifest)
    if not want:
        return ["manifest carries no qBittorrent categories"], False
    default_path = (manifest.get("qbt") or {}).get("save_path", "/data/torrents")
    status, live = call(f"{base}/api/v2/torrents/categories", opener=opener)
    if status != 200 or not isinstance(live, dict):
        return [f"qbittorrent: categories unreachable (HTTP {status})"], False

    findings: list[str] = []
    wrong = path_drift(live, want)
    extra = strays(live, want)
    for name, current, path in wrong:
        # The manifest may not carry paths yet; a missing category still gets
        # created, at the default save path, and says so.
        target = path or default_path
        if not apply_fix or dry_run:
            if path is None:
                findings.append(f"qbittorrent: category {name!r} is missing")
            else:
                findings.append(f"qbittorrent: category {name!r} saves to {current!r}, "
                                f"expected {path!r}")
            continue
        verb = "editCategory" if name in live else "createCategory"
        status, _ = call(f"{base}/api/v2/torrents/{verb}", method="POST",
                         body={"category": name, "savePath": target},
                         opener=opener, raw_form=True)
        if status in (200, 201):
            print(f"qBittorrent {name!r}: {current!r} -> {target!r}")
        else:
            findings.append(f"qbittorrent: category {name!r} could not be set (HTTP {status})")
    if extra:
        status, torrents = call(f"{base}/api/v2/torrents/info", opener=opener)
        still_filing = held(extra, torrents if status == 200 and isinstance(torrents, list) else [])
        removable = [name for name in extra if name not in still_filing]
        if not apply_fix or dry_run:
            findings.append(f"qbittorrent: categories not in the manifest: {', '.join(extra)}")
        else:
            if removable:
                status, _ = call(f"{base}/api/v2/torrents/removeCategories", method="POST",
                                 body={"categories": "\n".join(removable)}, opener=opener,
                                 raw_form=True)
                if status in (200, 201, 204):
                    print(f"qBittorrent removed: {', '.join(removable)}")
                else:
                    findings.append(f"qbittorrent: could not remove {', '.join(removable)} "
                                    f"(HTTP {status})")
        for name, count in sorted(still_filing.items()):
            findings.append(f"qbittorrent: category {name!r} still files {count} torrent(s); "
                            f"removing it would strip them from the app that queued them "
                            f"(move them to the manifest's category first)")
    if not wrong and not extra:
        if paths_known:
            print(f"qbittorrent holds the manifest's {len(want)} categories at their paths")
        else:
            print(f"qbittorrent holds the manifest's {len(want)} categories; this "
                  f"manifest predates the path map, so the paths are not checked "
                  f"(re-run monarch-init to record them)")
    return findings, True


def manifest_categories(manifest: dict) -> tuple[dict[str, str | None], bool]:
    """(name -> save path, whether the manifest recorded paths at all).

    A manifest that predates the map carries only the names, and the honest
    answer for it is "the paths are unknown", not "every category saves to the
    default" - guessing the default would rewrite four correct
    /data/torrents/<type> paths to /data/torrents and call that a fix. The names
    still drive creation and pruning (both name-only decisions); the paths are
    reported as unverifiable until init writes the map.
    """
    qbt = manifest.get("qbt") or {}
    if qbt.get("category_paths"):
        return dict(qbt["category_paths"]), True
    return {name: None for name in qbt.get("categories") or []}, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                        help=f"the invariants manifest (default {DEFAULT_MANIFEST})")
    parser.add_argument("--appdata", default=str(DEFAULT_APPDATA),
                        help="where the apps' config.xml files are")
    parser.add_argument("--qbt", default=os.environ.get("MONARCH_QBT_URL", DEFAULT_QBT),
                        help=f"qBittorrent WebUI base (default {DEFAULT_QBT})")
    parser.add_argument("--check", action="store_true",
                        help="report drift and change nothing (exit 2 on drift)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, change nothing")
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"cannot read the manifest {args.manifest}: {error}", file=sys.stderr)
        return 1

    user, password = credentials(REPO)
    if not (user and password):
        print("MONARCH_USERNAME/MONARCH_PASSWORD are not set and .env has neither",
              file=sys.stderr)
        return 1

    applying = not (args.check or args.dry_run)
    # Order matters: the apps are corrected first (so their old category names are
    # known), the torrents filed under those names move with them, and only then
    # are the old names pruned - pruning a category still holding a download that
    # an app is tracking is how that download disappears from the app's queue.
    arr_findings, arr_ok, renames = check_arrs(manifest, Path(args.appdata),
                                               applying, args.dry_run)
    opener = None
    qbt_findings: list[str] = []
    qbt_ok = False
    if renames or not args.check:
        opener, error = qbt_session(args.qbt, user, password)
        if opener is None:
            qbt_findings = [f"qbittorrent: {error}"]
        else:
            if not args.check:
                qbt_findings += migrate_torrents(args.qbt, opener, renames, args.dry_run)
            qbt_findings_qbt, qbt_ok = check_qbt(manifest, args.qbt, opener, applying, args.dry_run)
            qbt_findings += qbt_findings_qbt
    else:
        qbt_ok = True
        print("qbittorrent: not asked (no app needed a category change)")

    findings = qbt_findings + arr_findings
    for finding in findings:
        print(f"FAIL {finding}", file=sys.stderr)
    if findings:
        return 2 if (qbt_ok or arr_ok) else 1
    return 0 if (qbt_ok and arr_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
