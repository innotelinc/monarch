#!/usr/bin/env python3
"""release-version.py — the version this release run should cut.

The workflow used to derive it from `gh release list --limit 1`, which returns
the most recently **created** release record. That is not the highest version,
and it is not even necessarily a published one, so the derivation could walk
backwards and never recover:

    v1.22  draft    created 2026-09-13   <- --limit 1 returns this one
    v1.23  latest   created 2026-09-12   <- actually the newest version

v1.22's record was created a day *after* v1.23's (both were drafts first), so
from 2026-09-14 every scheduled run read "last release: v1.22", computed v1.23,
and died on

    release with the same tag name already exists: v1.23

Seven consecutive runs failed that way and no release was cut, because the next
run recomputed the same v1.23 from the same list. Ordering is not a version —
the versions are the tags.

So the version is taken as the **highest** version seen, from both sources that
can hold one:

  * the repository's `v*` tags, which is the durable record (`gh release create`
    always creates its tag), and
  * the tags of existing releases, **drafts included** — a draft reserves its
    tag name exactly as a published release does, so reusing it fails the same
    way. This is the set that answers "is this name taken", which is the
    question that has to be asked before `gh release create`.

A tag that does not parse as `vMAJOR[.MINOR[.PATCH]]` is ignored (and named on
stderr), so a stray `nightly` or `backup-2026-09` tag cannot become the base of
the next release. A major bump resets the lower components; a minor bump resets
the patch.

Usage:

  python3 scripts/release-version.py                     # bump minor
  python3 scripts/release-version.py --bump major
  python3 scripts/release-version.py --tag v1.23         # this run IS for v1.23

The version is the only thing on stdout, so a caller can use it directly:

  VERSION="$(python3 scripts/release-version.py --bump minor)"

Everything else — which sources were read, what was highest, what was ignored —
goes to stderr, which is where a workflow log wants it.

Exit code: 0 = a version on stdout; 1 = nothing could be derived (no sources
readable), because a release run that cannot name itself must not proceed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# v1 / v1.2 / v1.2.3 are all versions; v1.2.3-rc1 and v1.2.3+build are not
# accepted, deliberately: a pre-release tag ascending the release line would let
# a test build become the base of the next scheduled release.
VERSION_RE = re.compile(r"^v(\d+)(?:\.(\d+))?(?:\.(\d+))?$")

BUMP_MINOR = "minor"
BUMP_MAJOR = "major"


# ── version arithmetic (pure: the decision table is the part worth testing) ──
def parse_version(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for a release tag, or None if it is not one."""
    match = VERSION_RE.match(tag.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor or 0), int(patch or 0)


def components_of(tag: str) -> int:
    """How many components a version is written with (v1.23 → 2, v1.20.1 → 3)."""
    return len(tag.strip().lstrip("v").split("."))


def format_version(parts: tuple[int, int, int], components: int = 2) -> str:
    """`parts` written the way this repository writes its versions.

    The component count is preserved from the version being built on, because
    the tags in a repository have a house style (here: v1.23, not v1.23.0) and
    a released version that does not look like its predecessors is a needless
    difference in every script that matches on one.
    """
    if components <= 1:
        return f"v{parts[0]}"
    if components == 2:
        return f"v{parts[0]}.{parts[1]}"
    return "v{}.{}.{}".format(*parts)


def _highest(versions) -> tuple[tuple[int, int, int], int] | None:
    """(parsed, component count) of the highest version, or None.

    Comparison is by the parsed tuple, so v1.9 does not outrank v1.10 and the
    order the caller supplies is irrelevant — which is the whole point: the
    input arrives in createdAt order, and that is what broke this.
    """
    best: tuple[tuple[int, int, int], int] | None = None
    for candidate in versions:
        parsed = parse_version(candidate)
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, components_of(candidate))
    return best


def highest_version(versions) -> str | None:
    """The highest release version among `versions`, ignoring anything else."""
    highest = _highest(versions)
    return format_version(highest[0], highest[1]) if highest else None


# A repository with no releases yet is seeded at v1.0.0 — the canonical first
# release, and the version the workflow started repositories at before this.
SEED_VERSION = "v1.0.0"


def next_version(versions, bump: str = BUMP_MINOR) -> str:
    """The next version after the highest one in `versions`."""
    if bump not in (BUMP_MINOR, BUMP_MAJOR):
        raise ValueError(f"unknown bump {bump!r}")
    highest = _highest(versions)
    if highest is None:
        return SEED_VERSION
    (major, minor, _patch), components = highest
    if bump == BUMP_MAJOR:
        return format_version((major + 1, 0, 0), components)
    return format_version((major, minor + 1, 0), components)


def release_tag(tag: str) -> str:
    """Validate a tag this run was triggered by, or raise.

    Returned verbatim: the run IS for that tag, so the tag is the version —
    normalising it here would cut a release under a name nobody pushed.
    """
    if parse_version(tag) is None:
        raise ValueError(
            f"refusing to cut a release from tag {tag!r}: it is not a release "
            f"version (expected vMAJOR[.MINOR[.PATCH]])"
        )
    return tag.strip()


def ignored(versions) -> list[str]:
    """The inputs that are not versions — reported, never silently dropped."""
    return sorted({v.strip() for v in versions if parse_version(v) is None and v.strip()})


# ── the sources ─────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd[:2])}: timed out"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tags_from_git() -> tuple[bool, list[str]]:
    """(readable?, the `v*` tags in the repository).

    `git fetch --tags` first: actions/checkout checks out one commit by default,
    so the tag list on a fresh runner is whatever came with it. A fetch failure
    (no network, no remote) is not fatal on its own — the release tags below are
    the other source, and both are read before a version is chosen.
    """
    _run(["git", "fetch", "--tags", "--force", "--quiet"])
    code, out = _run(["git", "tag", "--list", "v*"])
    if code != 0:
        print(f"release-version: git tag unavailable: {out.strip()}", file=sys.stderr)
        return False, []
    return True, [line.strip() for line in out.splitlines() if line.strip()]


def tags_from_releases() -> tuple[bool, list[str]]:
    """(readable?, every existing release's tag).

    Drafts are included: a draft holds its tag name exactly as a published
    release does, so reusing it fails the same way. This is the set that answers
    "is this name taken", which is the question asked before creating one.
    """
    code, out = _run(
        ["gh", "release", "list", "--limit", "200", "--json", "tagName,isDraft"]
    )
    if code != 0:
        print(f"release-version: gh release list unavailable: {out.strip()}", file=sys.stderr)
        return False, []
    try:
        entries = json.loads(out)
    except json.JSONDecodeError:
        print("release-version: gh release list returned something unparsable", file=sys.stderr)
        return False, []
    return True, [entry.get("tagName", "") for entry in entries if entry.get("tagName")]


def known_versions(git_tags=(), release_tags=()) -> list[str]:
    """The union of both sources, in no particular order (order is not used).

    Deduplicated: the two sources overlap by design — every release it cut has a
    tag — and a version counted twice would be reported as two versions in the
    log line that says how many were examined.
    """
    seen: dict[str, None] = {}
    for tag in list(git_tags) + list(release_tags):
        stripped = tag.strip()
        if stripped:
            seen.setdefault(stripped, None)
    return list(seen)


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bump", default=BUMP_MINOR, choices=(BUMP_MINOR, BUMP_MAJOR))
    parser.add_argument(
        "--tag",
        default="",
        help="the tag this run was triggered by (a tagged push); taken as the version",
    )
    # default=None, not "": an empty list has to be distinguishable from "this
    # source was not supplied". `--git-tags ""` means a repository with no tags
    # (which seeds v1.0.0); omitting it means "go and read them".
    parser.add_argument("--git-tags", default=None, help="comma-separated, for testing without a repo")
    parser.add_argument("--release-tags", default=None, help="comma-separated, for testing without gh")
    args = parser.parse_args(argv[1:])

    if args.tag:
        try:
            version = release_tag(args.tag)
        except ValueError as exc:
            print(f"release-version: {exc}", file=sys.stderr)
            return 1
        # stderr, so stdout stays the version alone.
        print(
            f"release-version: tagged run — cutting {version} (the tag is the version)",
            file=sys.stderr,
        )
        print(version)
        return 0

    if args.git_tags is not None:
        git_ok, git_tags = True, [t for t in args.git_tags.split(",") if t]
    else:
        git_ok, git_tags = tags_from_git()
    if args.release_tags is not None:
        # An explicit list means "these are the releases": an empty one is a
        # repository with none, not a source that could not be read — the
        # distinction the guard below depends on.
        rel_ok, release_tags = True, [t for t in args.release_tags.split(",") if t]
    else:
        rel_ok, release_tags = tags_from_releases()

    # "No releases yet" and "the sources could not be read" are different, and
    # only the first one may be released as v1.0.0. Answering the second with a
    # version would cut a release on a guess.
    if not git_ok and not rel_ok:
        print(
            "release-version: neither the tags nor the releases could be read, so this "
            "release cannot be named — refusing rather than guessing v1.0.0",
            file=sys.stderr,
        )
        return 1

    versions = known_versions(git_tags, release_tags)
    for stray in ignored(versions):
        print(f"release-version: ignoring {stray!r} — not a release version", file=sys.stderr)

    highest = highest_version(versions)
    version = next_version(versions, args.bump)
    if highest is None:
        print("release-version: no prior release — starting at v1.0.0", file=sys.stderr)
    else:
        print(
            f"release-version: highest of {len(versions)} version(s) is {highest}; "
            f"bumping {args.bump} → {version}",
            file=sys.stderr,
        )
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
