#!/usr/bin/env python3
"""Answer "is the stack drifted right now, and is the heal getting anywhere?"

`scripts/drift-check.sh` runs four times a day from a systemd timer and records
each verdict in `$MONARCH_STATE_DIR/drift-last`. That file is the answer, but it
is not readable as one: it is eight `key=value` lines plus a line per finding, and
two facts that decide what to DO about the verdict live in files beside it - the
heal clock and streak (`drift-heal-last`, whether the repair is getting anywhere
or standing down) and the findings the last standing-down alert carried
(`drift-standdown-alert`). Reading all three is what this does, in one screen, so
nobody has to know the layout to ask.

Exit codes follow the house rule for a read-only check - 0 clean, 1 drift, 2
cannot judge - so this can be used like the check itself:

    scripts/drift-status.py            # one screen, exit with the verdict
    scripts/drift-status.py --json     # the same facts, for a dashboard
    scripts/drift-status.py --state-dir /tmp/state   # a rehearsal, not this host

CANNOT JUDGE means nothing has been recorded against this state directory, or the
verdict is older than two timer ticks (`--stale-after`, default 13h): the timer
runs at 00/06/12/18 with RandomizedDelaySec=300, so a verdict that old is a
memory of the stack rather than a reading of it - usually a stopped timer, which
is itself the finding. The report says which of the two it is.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

DEFAULT_STATE_DIR = "/docker/appdata/init"
# Two ticks of the 00/06/12/18 timer plus its 300s jitter and a margin: past this,
# the newest verdict is old enough that "the timer is not running" fits better than
# "the timer has not come round yet".
DEFAULT_STALE_AFTER = 13 * 3600

# What `heal=` means in the verdict, in the words of the reader who has to act on
# it. The check writes these; the wording lives here because this is the surface
# that explains them.
HEAL_MEANING = {
    "none": "nothing was repaired - this run only checked",
    "healed": "the last repair reconciled the stack and the re-check came back clean",
    "recheck": "this was a re-check after a repair, and it still finds drift - the repair did not take",
    "suppressed": "the repair was suppressed by the rate limit - the drift is persistent",
    "stood_down": "the heal has STOOD DOWN and will not try again until somebody resets it",
}


def human_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "in the future"
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def read_verdict(path: Path) -> dict:
    """The recorded verdict, or {"recorded": False, "why": ...} when there is none.

    A file that exists but cannot be read as a verdict is not the same as no file:
    the first says the check wrote something this does not understand, which is a
    finding in itself, so it is reported rather than smoothed over.
    """
    if not path.is_file():
        return {"recorded": False, "why": f"no verdict recorded at {path}"}
    fields: dict[str, str] = {}
    findings: list[str] = []
    try:
        # errors=replace: a half-written file is possible in principle (the check
        # renames into place, so not really), and a decode error there must not be
        # the only thing this tool can say.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {"recorded": False, "why": f"{path} could not be read: {error}"}
    for line in text.splitlines():
        if line.startswith("finding="):
            findings.append(line[len("finding="):])
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    verdict = fields.get("verdict", "")
    if verdict not in ("ok", "drift"):
        return {"recorded": False, "why": f"{path} does not carry a verdict ({verdict or 'empty'})"}
    recorded = dict(fields)
    recorded["recorded"] = True
    recorded["findings"] = findings
    try:
        recorded["epoch"] = int(fields.get("epoch", ""))
    except ValueError:
        recorded["epoch"] = None
    try:
        recorded["issues"] = int(fields.get("issues", "0"))
    except ValueError:
        recorded["issues"] = len(findings)
    return recorded


def read_heal_state(path: Path) -> dict:
    """`<epoch> <count> <standdown>` - forgiving, like the check's own read.

    The file predates two of its three fields, and a state file written by hand or
    by an older version holds fewer; a field that is not a number reads as 0 rather
    than failing the read, because this is bookkeeping and the verdict is the
    finding.
    """
    state = {"attempt_epoch": None, "count": 0, "standdown_alert_epoch": None}
    try:
        parts = path.read_text(encoding="utf-8").split()
    except OSError:
        return state
    keys = ("attempt_epoch", "count", "standdown_alert_epoch")
    for key, value in zip(keys, parts):
        try:
            number = int(value)
        except ValueError:
            continue
        state[key] = number
    return state


def read_record(path: Path) -> list[str]:
    """The findings the last standing-down alert carried, one per line."""
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []


def collect(state_dir: Path, stale_after: int, now: int) -> dict:
    verdict = read_verdict(state_dir / "drift-last")
    heal = read_heal_state(state_dir / "drift-heal-last")
    alert_findings = read_record(state_dir / "drift-standdown-alert")
    age = None
    if verdict.get("epoch"):
        age = now - verdict["epoch"]
    stale = age is not None and age > stale_after
    if not verdict["recorded"]:
        status = "cannot-judge"
    elif stale:
        status = "stale"
    else:
        status = verdict["verdict"]
    return {
        "status": status,
        "verdict": verdict,
        "heal": heal,
        "alert_findings": alert_findings,
        "age": age,
        "stale": stale,
        "stale_after": stale_after,
    }


def report(data: dict, state_dir: Path, now: int) -> str:
    verdict = data["verdict"]
    heal = data["heal"]
    lines = [f"monarch drift status - {socket.gethostname()}"]

    if not verdict["recorded"]:
        lines.append(f"  verdict   cannot judge - {verdict['why']}")
        lines.append("  next      the check has not completed a run against this state directory;")
        lines.append("            'systemctl status monarch-drift-check.timer' says whether it ran")
        return "\n".join(lines)

    age = data["age"]
    if verdict["verdict"] == "ok":
        headline = "ok - the stack matches the invariants it was built from"
    else:
        headline = f"DRIFT - {verdict['issues']} finding(s), not repaired"
    if data["stale"]:
        headline += f" (STALE: recorded {human_age(age)}, and the check runs every 6h)"
    lines.append(f"  verdict   {headline}")
    lines.append(f"  when      {verdict.get('at', 'unknown')}, {human_age(age)}")
    lines.append(f"  manifest  {verdict.get('manifest', 'unknown')}")
    if verdict["findings"]:
        lines.append("  findings")
        for finding in verdict["findings"]:
            lines.append(f"    - {finding}")

    did = verdict.get("heal", "")
    lines.append(f"  heal      {did} - {HEAL_MEANING.get(did, 'no verdict recorded for what it did')}")
    streak = int(heal.get("count") or 0)
    attempt_age = None
    if heal.get("attempt_epoch"):
        attempt_age = now - int(heal["attempt_epoch"])
    lines.append(
        f"  streak    {streak} attempt(s) in a row have not cleared it"
        f"; last attempt {human_age(attempt_age)}"
    )
    if did == "stood_down" or streak:
        lines.append(
            "  reset     'scripts/drift-check.sh --reset-streak' lets the heal try again"
            " (run it after looking at why)"
        )
    if data["alert_findings"]:
        alerted = heal.get("standdown_alert_epoch")
        alerted_age = now - int(alerted) if alerted else None
        lines.append(
            f"  last standdown alert  {human_age(alerted_age)}, carrying"
            f" {len(data['alert_findings'])} finding(s) - a repeat only goes out if the"
            " drift has changed since"
        )
    lines.append(f"  state     {state_dir}")
    return "\n".join(lines)


def as_json(data: dict, state_dir: Path) -> str:
    verdict = data["verdict"]
    heal = data["heal"]
    payload = {
        "host": socket.gethostname(),
        "state_dir": str(state_dir),
        "status": data["status"],
        "verdict": verdict.get("verdict") if verdict["recorded"] else None,
        "recorded": verdict["recorded"],
        "why": verdict.get("why"),
        "at": verdict.get("at"),
        "epoch": verdict.get("epoch"),
        "age_seconds": data["age"],
        "stale": data["stale"],
        "stale_after_seconds": data["stale_after"],
        "manifest": verdict.get("manifest"),
        "issues": verdict.get("issues"),
        "findings": verdict.get("findings", []),
        "heal": verdict.get("heal"),
        "heal_streak": int(heal.get("count") or 0),
        "heal_last_epoch": heal.get("attempt_epoch"),
        "standdown_alert_epoch": heal.get("standdown_alert_epoch"),
        "standdown_alert_findings": data["alert_findings"],
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Say whether the Monarch stack is drifted right now, and what the heal has been doing."
    )
    parser.add_argument("--state-dir", default=os.environ.get("MONARCH_STATE_DIR", DEFAULT_STATE_DIR),
                        help=f"where the check keeps its state (default: $MONARCH_STATE_DIR, else {DEFAULT_STATE_DIR})")
    parser.add_argument("--stale-after", type=int, default=DEFAULT_STALE_AFTER,
                        help=f"seconds after which a verdict is too old to judge by (default: {DEFAULT_STALE_AFTER})")
    parser.add_argument("--json", action="store_true",
                        help="the same facts as JSON, for a dashboard or another program")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    now = int(time.time())
    data = collect(state_dir, args.stale_after, now)

    if args.json:
        print(as_json(data, state_dir))
    else:
        print(report(data, state_dir, now))
        if data["status"] == "cannot-judge":
            print(f"drift-status: {data['verdict']['why']} - cannot judge", file=sys.stderr)
        elif data["status"] == "stale":
            print(
                f"drift-status: the newest verdict is {human_age(data['age'])} old"
                f" (> {args.stale_after}s) - the timer is not keeping up, so this is a"
                " memory of the stack rather than a reading of it",
                file=sys.stderr,
            )

    if data["status"] == "ok":
        return 0
    if data["status"] == "drift":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
