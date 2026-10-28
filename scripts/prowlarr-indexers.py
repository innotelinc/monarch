#!/usr/bin/env python3
"""Add every public indexer Prowlarr can actually use, and keep them honest.

WHY THIS IS A SCRIPT AND NOT A ONE-OFF. Prowlarr ships 624 indexer definitions
and this stack had two of them, neither of which worked: both were Cloudflare-
fronted (1337x, 0Magnet) and neither carried the `cloudflare` tag, so every
search answered "blocked by CloudFlare Protection". An indexer that is present
but broken is worse than an absent one — it makes a search look empty rather
than unconfigured — so "add indexers" here means *add the ones that answer*, and
`--check` means *the ones already in Prowlarr still answer*.

WHAT IT ADDS. Every definition marked `public` (no account needed). `private`
definitions want credentials that do not exist here and are never attempted;
`semiPrivate` ones are only attempted with --privacy. A candidate is added only
after it passes Prowlarr's own indexer test, and a failure that names Cloudflare
is retried once through the FlareSolverr proxy (the `cloudflare` tag) before it
is given up on.

Usage:
    scripts/prowlarr-indexers.py --check         # do the ones already there work?
    scripts/prowlarr-indexers.py --repair        # tag the blocked ones for the proxy
    scripts/prowlarr-indexers.py --dry-run       # what would be added
    scripts/prowlarr-indexers.py                 # add and verify
    scripts/prowlarr-indexers.py --privacy public,semiPrivate

Run it where Prowlarr's API is reachable and its config.xml is readable — the
host running the stack, i.e. `docker compose exec` is not needed.

Exit codes: 0 nothing to do / everything checked out; 1 could not talk to
Prowlarr or no API key; 2 --check found an indexer that does not answer (or,
with --offline, that Prowlarr holds none at all).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:9696"
DEFAULT_APPDATA = Path("/docker/appdata")
API = "/api/v1"
CLOUDFLARE_TAG = "cloudflare"
# A public indexer needs no account, so a *failure* is a fact about the site:
# dead, Cloudflare-blocked, or not answering from here.
DEFAULT_PRIVACY = ("public",)


class ProwlarrError(RuntimeError):
    pass


class Prowlarr:
    def __init__(self, base: str, key: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def call(self, method: str, path: str, body: object | None = None,
             timeout: float | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{API}{path}", data=data, method=method)
        request.add_header("X-Api-Key", self.key)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"null")
            except ValueError:
                return error.code, raw[:400].decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            raise ProwlarrError(f"{method} {path}: {error}") from error
        try:
            return response.status, json.loads(raw or b"null")
        except ValueError:
            return response.status, raw[:400].decode("utf-8", "replace")

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: object):
        return self.call("POST", path, body)

    def indexers(self) -> list[dict]:
        status, body = self.get("/indexer")
        if status != 200 or not isinstance(body, list):
            raise ProwlarrError(f"indexer list unreachable (HTTP {status})")
        return body

    def schema(self) -> list[dict]:
        status, body = self.get("/indexer/schema")
        if status != 200 or not isinstance(body, list):
            raise ProwlarrError(f"indexer schema unreachable (HTTP {status})")
        return body

    def app_profile_id(self) -> int:
        """The app profile a new indexer must be filed under.

        Prowlarr refuses `'App Profile Id' must be greater than '0'` without it:
        the profile is what decides which *arrs the indexer syncs to.
        """
        status, profiles = self.get("/appprofile")
        if status == 200 and isinstance(profiles, list) and profiles:
            return int(profiles[0]["id"])
        raise ProwlarrError(f"no app profile to file indexers under (HTTP {status})")

    def tag_id(self, label: str) -> int:
        """The tag's id, creating it if the proxy has none yet."""
        status, tags = self.get("/tag")
        if status == 200 and isinstance(tags, list):
            for tag in tags:
                if tag.get("label") == label:
                    return int(tag["id"])
        status, created = self.post("/tag", {"label": label})
        if status in (200, 201) and isinstance(created, dict) and created.get("id") is not None:
            return int(created["id"])
        raise ProwlarrError(f"could not obtain the '{label}' tag (HTTP {status})")

    def test(self, indexer: dict) -> tuple[bool, str]:
        """Prowlarr's own test for one candidate — adds nothing."""
        try:
            status, body = self.call("POST", "/indexer/test", indexer, timeout=self.timeout * 3)
        except ProwlarrError as error:
            return False, str(error)
        if status in (200, 201) and body in ({}, [], None, ""):
            return True, ""
        return False, _reason(body)

    def add(self, indexer: dict) -> tuple[bool, str]:
        try:
            status, body = self.post("/indexer", indexer)
        except ProwlarrError as error:
            return False, str(error)
        if status in (200, 201):
            return True, ""
        return False, _reason(body)

    def update(self, indexer: dict) -> tuple[bool, str]:
        """PUT the whole resource — Prowlarr has no per-field endpoint."""
        try:
            status, body = self.call("PUT", f"/indexer/{indexer['id']}", indexer)
        except ProwlarrError as error:
            return False, str(error)
        if status in (200, 202):
            return True, ""
        return False, _reason(body)


def _reason(body: object) -> str:
    """The first human sentence out of a Prowlarr validation failure."""
    if isinstance(body, list) and body:
        return str(body[0].get("errorMessage") or body[0])[:200]
    if isinstance(body, dict) and body:
        return str(body.get("errorMessage") or body.get("message") or body)[:200]
    return str(body)[:200]


def _definition_key(entry: dict) -> str:
    """The definition's id — the thing Prowlarr enforces uniqueness on.

    NOT the display name: for a Cardigann definition the two differ, because the
    stored `definitionName` is the definition's id (`btdirectory`) while the
    schema's `name` is the human label (`BTdirectory`). Comparing labels re-tries
    every indexer already held, and Prowlarr answers each one "Should be unique"
    — a request to every tracker in the list, and a report that calls a present
    indexer a failed candidate while a genuinely new one slips through as
    "already there".
    """
    return str(entry.get("definitionName") or entry.get("name")
               or entry.get("implementation") or "")


def parse_privacy(value: str) -> tuple[str, ...]:
    """The privacy classes named on the command line, compared case-insensitively.

    A definition reports `public`, `semiPrivate` or `private`, and it is
    compared lowercased — so `--privacy public,semiPrivate`, the invocation this
    script's own usage block and docs/operations.md both show, matched nothing
    for the second class and ran the narrower set without saying so: sixty-four
    definitions were never attempted. The case comes from the command line, so
    it is normalised here.
    """
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def _needs_flaresolverr(reason: str) -> bool:
    return bool(re.search(r"cloudflare|cloud flare|blocked by", reason, re.IGNORECASE))


def _candidate(entry: dict, tag: int | None = None, app_profile: int | None = None) -> dict:
    payload = json.loads(json.dumps(entry))  # the schema entry IS the resource
    payload["enable"] = True
    payload["name"] = entry.get("name") or entry.get("implementation")
    if tag is not None:
        payload["tags"] = sorted(set(payload.get("tags") or []) | {tag})
    if app_profile is not None:
        payload["appProfileId"] = app_profile
    return payload


def check(prowlarr: Prowlarr, workers: int, offline: bool = False) -> int:
    """Every indexer Prowlarr is holding must still answer its own test.

    --offline stops at "Prowlarr holds an enabled indexer": testing thirty
    public trackers is real traffic, and a timer that did it every six hours
    is a way to earn a ban. Reachability and a non-empty list are what the
    "searches look empty" failure needs; the deep test is the operator's.
    """
    indexers = prowlarr.indexers()
    if offline:
        enabled = [i for i in indexers if i.get("enable")]
        print(f"Prowlarr holds {len(indexers)} indexer(s), {len(enabled)} enabled")
        return 0 if enabled else 2
    if not indexers:
        print("Prowlarr holds no indexers — nothing to check (run without --check to add them)")
        return 0

    def probe(indexer: dict) -> tuple[str, bool, str]:
        ok, reason = prowlarr.test(indexer)
        return indexer.get("name") or str(indexer.get("id")), ok, reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, indexers))

    broken = [(name, reason) for name, ok, reason in results if not ok]
    for name, reason in broken:
        print(f"FAIL {name}: {reason}")
    print(f"{len(results) - len(broken)}/{len(results)} indexer(s) answer")
    return 2 if broken else 0


def repair(prowlarr: Prowlarr, tag: int, workers: int) -> int:
    """Give the indexers already in Prowlarr the proxy they turn out to need.

    The two this stack had were both Cloudflare-fronted and neither was tagged,
    so both were in the list and neither could search — the exact state that
    looks like "no indexers" from a *arr. A dead site is left alone: it is not a
    tagging problem, and the operator should see it rather than a silent tag.
    """
    indexers = prowlarr.indexers()

    def probe(indexer: dict) -> tuple[dict, bool, str]:
        ok, reason = prowlarr.test(indexer)
        return indexer, ok, reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, indexers))

    fixed = 0
    for indexer, ok, reason in results:
        if ok or not _needs_flaresolverr(reason):
            continue
        if tag in (indexer.get("tags") or []):
            print(f"  still blocked, already via FlareSolverr: {indexer.get('name')}: {reason}")
            continue
        candidate = json.loads(json.dumps(indexer))
        candidate["tags"] = sorted(set(candidate.get("tags") or []) | {tag})
        if prowlarr.test(candidate)[0]:
            ok, detail = prowlarr.update(candidate)
            if ok:
                print(f"  tagged for FlareSolverr: {candidate.get('name')}")
                fixed += 1
            else:
                print(f"  could not tag {candidate.get('name')}: {detail}")
        else:
            print(f"  blocked even through FlareSolverr: {candidate.get('name')}: {reason}")
    return fixed


def repairs_only(prowlarr: Prowlarr, workers: int) -> int:
    """Tag the indexers already held that turn out to need the proxy.

    Separate from `add` because it is the other half of the same fault and the
    cheaper one: a stack can hold fifty indexers that all answer and fifteen
    that are blocked, and fixing the fifteen should not mean re-testing six
    hundred definitions. Refuses when Prowlarr has no indexer proxy configured -
    the tag would be written, the indexer would still be blocked, and the run
    would report a repair that changed nothing.
    """
    status, proxies = prowlarr.get("/indexerproxy")
    if status != 200 or not isinstance(proxies, list) or not proxies:
        print("no indexer proxy is configured in Prowlarr, so the 'cloudflare' tag "
              "would change nothing - configure FlareSolverr first "
              "(Settings -> Indexers -> Indexer Proxies)", file=sys.stderr)
        return 1
    tag = prowlarr.tag_id(CLOUDFLARE_TAG)
    fixed = repair(prowlarr, tag, workers)
    print(f"tagged {fixed} indexer(s) for '{(proxies[0] or {}).get('name') or 'the proxy'}'")
    return 0


def add(prowlarr: Prowlarr, privacy: tuple[str, ...], workers: int, dry_run: bool) -> int:
    existing = prowlarr.indexers()
    present = {_definition_key(indexer) for indexer in existing}
    candidates = [entry for entry in prowlarr.schema()
                  if (entry.get("privacy") or "").lower() in privacy]
    todo = [entry for entry in candidates if _definition_key(entry) not in present]

    print(f"{len(candidates)} definition(s) marked {', '.join(privacy)}; "
          f"{len(existing)} already in Prowlarr, {len(todo)} to try")
    if dry_run:
        for entry in todo:
            print(f"  would try {entry.get('name')} ({entry.get('protocol')})")
        return 0

    tag = prowlarr.tag_id(CLOUDFLARE_TAG)
    app_profile = prowlarr.app_profile_id()
    tagged = repair(prowlarr, tag, workers)
    if tagged:
        print(f"repaired {tagged} indexer(s) already present")

    def attempt(entry: dict) -> tuple[str, str, str]:
        """Returns (name, outcome, detail) where outcome is added|flaresolverr|skipped."""
        name = entry.get("name") or entry.get("implementation") or "?"
        direct = _candidate(entry, app_profile=app_profile)
        ok, reason = prowlarr.test(direct)
        if not ok and _needs_flaresolverr(reason):
            proxied = _candidate(entry, tag, app_profile)
            ok, via_proxy = prowlarr.test(proxied)
            if ok:
                added, detail = prowlarr.add(proxied)
                return name, "flaresolverr" if added else "skipped", detail or f"proxy: {reason}"
            reason = via_proxy or reason
        if not ok:
            return name, "skipped", reason
        added, detail = prowlarr.add(direct)
        return name, "added" if added else "skipped", detail

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, todo))

    counts: dict[str, int] = {}
    for name, outcome, detail in results:
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "skipped":
            print(f"  skip {name}: {detail}")
        else:
            print(f"  {outcome:<12} {name}{' (via FlareSolverr)' if outcome == 'flaresolverr' else ''}")

    print(f"added {counts.get('added', 0)}, via FlareSolverr {counts.get('flaresolverr', 0)}, "
          f"skipped {counts.get('skipped', 0)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"Prowlarr base URL (default {DEFAULT_BASE})")
    parser.add_argument("--api-key", default="",
                        help="Prowlarr API key (default: read config.xml from --appdata)")
    parser.add_argument("--appdata", default=str(DEFAULT_APPDATA),
                        help="where the apps' config.xml files are")
    parser.add_argument("--privacy", default=",".join(DEFAULT_PRIVACY),
                        help="comma-separated privacy classes to attempt "
                             f"(default {','.join(DEFAULT_PRIVACY)})")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent indexer tests (default 4)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for one indexer request (default 30)")
    parser.add_argument("--check", action="store_true",
                        help="only report whether the indexers already in Prowlarr answer")
    parser.add_argument("--repair", action="store_true",
                        help="only tag the indexers already in Prowlarr that need the proxy")
    parser.add_argument("--offline", action="store_true",
                        help="with --check, only ask that Prowlarr holds an enabled indexer "
                             "(no tracker is contacted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be attempted, change nothing")
    args = parser.parse_args(argv)

    key = args.api_key.strip()
    if not key:
        config = Path(args.appdata) / "prowlarr" / "config.xml"
        try:
            match = re.search(r"<ApiKey>([^<]+)</ApiKey>",
                              config.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            print(f"cannot read {config}: {error}", file=sys.stderr)
            return 1
        if not match:
            print(f"no <ApiKey> in {config}", file=sys.stderr)
            return 1
        key = match.group(1)

    prowlarr = Prowlarr(args.base, key, timeout=args.timeout)
    try:
        if args.check:
            return check(prowlarr, args.workers, offline=args.offline)
        if args.repair:
            return repairs_only(prowlarr, args.workers)
        return add(prowlarr, parse_privacy(args.privacy), args.workers, args.dry_run)
    except ProwlarrError as error:
        print(f"prowlarr: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
