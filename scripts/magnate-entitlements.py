#!/usr/bin/env python3
"""
magnate-entitlements.py - turn the Magnate billing decision into Jellyfin policy.

Magnate owns billing and the entitlement decision (plans, subscriptions,
trials, coupons); Monarch owns how a tier plays. Monarch held no code that
consumed either - `ENTITLEMENTS_API_TOKEN` appeared only in .env.example - so
tiers gated *access* (paid_users) but nothing constrained playback: no stream
limit, no quality ceiling, no download toggle.

What this does, per Jellyfin user:

  1. asks Magnate   GET <MAGNATE_URL>/api/entitlements?plan=<slug>&user=<name>
                    -> {"entitled": bool, "reason", "plan", "slug", "status",
                        "expires_at"}
     (the same contract Capstone consumes; Bearer ENTITLEMENTS_API_TOKEN when
     the server sets one);
  2. maps the returned plan slug to a playback policy in
     scripts/magnate-tiers.json (streams / quality_kbps / profiles / downloads);
  3. applies it to the Jellyfin user policy: MaxActiveSessions (when the build
     has the field), RemoteClientBitrateLimit, EnableContentDownloading.

A user Magnate does not consider entitled is REPORTED, never silently locked
out: disabling is opt-in with --disable-unentitled, because the local Jellyfin
admin is not a Magnate subscriber. Users in `exempt_users` are skipped.

Modes:

  --check         report only: what Magnate says and what Jellyfin has; exit 1
                  when any managed user's policy differs (drift-check runs this)
  (default)       apply the tier policy (idempotent - only changed fields POST)
  --disable-unentitled   also disable users Magnate says are not entitled

Usage:

  python3 scripts/magnate-entitlements.py --check
  python3 scripts/magnate-entitlements.py
  python3 scripts/magnate-entitlements.py --user cola --check
  python3 scripts/magnate-entitlements.py --disable-unentitled

Exit: 0 = converged (or --check found no drift) · 1 = drift or an error · 2 = not configured
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TIERS_FILE = SCRIPT_DIR / "magnate-tiers.json"
JELLYFIN_KEY_FILE = Path("/docker/appdata/init/jellyfin-api-key.txt")

DEFAULT_MAGNATE_URL = "https://magnate.innotel.us"
DEFAULT_JELLYFIN_URL = "http://localhost:8097"


# ── env ─────────────────────────────────────────────────────────────────────
def load_env(path):
    """Parse KEY=VALUE lines into os.environ (never overwrite)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def env(name, default=""):
    return os.environ.get(name, default).strip()


def http_json(url, token="", timeout=20, method="GET", body=None, headers=None):
    """Call a JSON endpoint; returns (status, parsed-body-or-None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": str(exc)}


# ── Jellyfin ────────────────────────────────────────────────────────────────
class Jellyfin:
    """Jellyfin admin API.

    The MediaBrowser Authorization header is the one this build accepts; the
    `?api_key=` query and `X-Emby-Token` both answer 401 (checked live against
    the pinned v12 image).
    """

    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def _call(self, path, method="GET", body=None):
        return http_json(f"{self.base}{path}", method=method, body=body,
                         headers={"Authorization": f"MediaBrowser Token={self.token}"})

    def users(self):
        status, body = self._call("/Users")
        if status != 200 or not isinstance(body, list):
            raise RuntimeError(
                "Jellyfin /Users returned HTTP "
                f"{status} (is JELLYFIN_API_KEY current? monarch-init writes "
                f"{JELLYFIN_KEY_FILE})")
        return body

    def policy(self, user_id):
        status, body = self._call(f"/Users/{user_id}")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"Jellyfin /Users/{user_id} returned HTTP {status}")
        return body

    def update_policy(self, user_id, policy):
        """POST a full UserPolicy; returns the HTTP status."""
        status, _ = self._call(f"/Users/{user_id}/Policy", method="POST", body=policy)
        return status


# ── Magnate ─────────────────────────────────────────────────────────────────
def magnate_entitlement(base, token, plan, user):
    """Magnate's answer for one user: (entitled|None, detail-string, body)."""
    from urllib.parse import urlencode
    query = urlencode({"plan": plan, "user": user})
    status, body = http_json(f"{base.rstrip('/')}/api/entitlements?{query}", token)
    if not isinstance(body, dict):
        return None, f"HTTP {status}", {}
    if status == 401:
        return None, "unauthorized (check ENTITLEMENTS_API_TOKEN)", body
    if body.get("reason") == "plan_not_found":
        return None, f"plan {plan!r} not found in Magnate", body
    return body.get("entitled"), str(body.get("reason") or ""), body


# ── policy mapping ──────────────────────────────────────────────────────────
def desired_fields(tier, current):
    """The Jellyfin policy fields a tier implies, limited to supported ones."""
    want = {
        # Jellyfin keeps this in bits per second; the tier file is kbit/s.
        "RemoteClientBitrateLimit": int(tier.get("quality_kbps", 0)) * 1000,
        "EnableContentDownloading": bool(tier.get("downloads", False)),
    }
    if "MaxActiveSessions" in current:          # added in newer Jellyfin builds
        want["MaxActiveSessions"] = int(tier.get("streams", 0))
    return want


def apply_tier(policy, tier, disable=False):
    """Return (changed_fields, notes) after folding a tier into a Jellyfin policy."""
    changes, notes = {}, []
    for key, value in desired_fields(tier, policy).items():
        if policy.get(key) != value:
            changes[key] = value
    if disable and not policy.get("IsDisabled"):
        changes["IsDisabled"] = True
    tier_streams = int(tier.get("streams", 0))
    if "MaxActiveSessions" not in policy and tier_streams:
        notes.append("stream limit not enforceable on this Jellyfin build "
                     "(no MaxActiveSessions field)")
    return changes, notes


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="report only; exit 1 when a policy differs")
    parser.add_argument("--user", help="only this Jellyfin user")
    parser.add_argument("--disable-unentitled", action="store_true",
                        help="also disable users Magnate does not entitle")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--tiers", default=str(TIERS_FILE))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    tiers = json.loads(Path(args.tiers).read_text(encoding="utf-8"))
    plan = env("MAGNATE_ENTITLEMENTS_PLAN", tiers.get("default_plan", "basic"))
    magnate_url = env("MAGNATE_URL", DEFAULT_MAGNATE_URL)
    ent_token = env("ENTITLEMENTS_API_TOKEN")
    jellyfin_url = env("JELLYFIN_URL", DEFAULT_JELLYFIN_URL)
    jellyfin_key = env("JELLYFIN_API_KEY")
    if not jellyfin_key and JELLYFIN_KEY_FILE.is_file():
        jellyfin_key = JELLYFIN_KEY_FILE.read_text().strip()

    if not jellyfin_key:
        print("NOT CONFIGURED: no JELLYFIN_API_KEY and no "
              f"{JELLYFIN_KEY_FILE} - run monarch-init first.", file=sys.stderr)
        return 2

    jf = Jellyfin(jellyfin_url, jellyfin_key)
    exempt = {u.lower() for u in tiers.get("exempt_users", [])}
    plans = tiers.get("plans", {})

    try:
        users = jf.users()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report, drifted, errors = [], 0, 0
    for user in sorted(users, key=lambda u: str(u.get("Name", "")).lower()):
        name = str(user.get("Name", ""))
        if args.user and name.lower() != args.user.lower():
            continue
        if name.lower() in exempt:
            report.append({"user": name, "status": "exempt", "changes": {}})
            continue

        entitled, detail, body = magnate_entitlement(magnate_url, ent_token, plan, name)
        slug = str(body.get("slug") or "")
        tier = plans.get(slug) or plans.get(tiers.get("default_plan", "basic"), {})

        if entitled is None:
            errors += 1
            report.append({"user": name, "status": f"magnate-unavailable ({detail})",
                           "changes": {}})
            continue
        if not entitled:
            if args.disable_unentitled:
                policy = jf.policy(user.get("Id"))
                changes, _ = apply_tier(policy, tier, disable=True)
                if changes and not args.check:
                    jf.update_policy(user.get("Id"), {**policy, **changes})
                report.append({"user": name, "status": f"unentitled ({detail})",
                               "changes": changes})
                drifted += 1 if args.check else 0
            else:
                report.append({"user": name, "status": f"unentitled ({detail})",
                               "changes": {}, "note": "not touching "
                               "(no --disable-unentitled)"})
            continue

        policy = jf.policy(user.get("Id"))
        changes, notes = apply_tier(policy, tier)
        if changes:
            drifted += 1
            if not args.check:
                status = jf.update_policy(user.get("Id"), {**policy, **changes})
                if status not in (200, 204):
                    errors += 1
                    report.append({"user": name,
                                   "status": f"apply failed (HTTP {status})",
                                   "changes": changes})
                    continue
        report.append({"user": name,
                       "status": f"{slug or plan} (active)" if changes else
                                 f"{slug or plan} (in sync)",
                       "changes": changes, "notes": notes})

    if args.json:
        print(json.dumps({"plan": plan, "magnate": magnate_url, "report": report},
                         indent=2))
    else:
        print(f"Magnate {magnate_url} · plan {plan} · Jellyfin {jellyfin_url}")
        for row in report:
            changed = ", ".join(f"{k}={v}" for k, v in row["changes"].items()) or "-"
            print(f"  {row['user']:20} {row['status']:34} {changed}")
            for note in row.get("notes", []):
                print(f"  {'':20} NOTE: {note}")
        if args.check:
            print(f"\n  {'CHECK FAILED: ' + str(drifted) + ' user(s) drifted' if drifted else 'CHECK OK: every policy matches its tier'}")
        else:
            print(f"\n  applied: {sum(1 for r in report if r['changes'])} user(s)")

    if errors:
        print(f"ERROR: {errors} user(s) could not be reconciled", file=sys.stderr)
        return 1
    return 1 if (args.check and drifted) else 0


if __name__ == "__main__":
    sys.exit(main())
