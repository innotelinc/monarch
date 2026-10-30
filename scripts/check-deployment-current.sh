#!/bin/sh
# check-deployment-current.sh — is this checkout the branch it deploys from?
#
# WHY THIS EXISTS
# ---------------
# Measured on the media host 2026-10-30: `/opt/monarch` was 35 commits behind
# `origin/main` and carried that same work as *uncommitted edits* in its tree.
# Every check on this branch — the Jellyfin login-methods check, the board-tile
# probe, the splash guard — was in the repository and not in the thing that runs
# at 06:00, so `drift-check` reported a posture it had never measured. Nothing
# could see it: the old checks passed, and they were the only ones running.
#
# This is the check that would have. It is deliberately read-only: `git ls-remote`
# asks the remote for its branch tip and writes no ref, so a check can never
# change the thing it is checking (a `git fetch` here would quietly repair the
# condition it exists to report).
#
# TWO STATES, BECAUSE THEY ARE FIXED DIFFERENTLY
# ----------------------------------------------
# * BEHIND — a `git pull` catches up, and the count says how far.
# * LOCAL EDITS — somebody edited the deployment in place. On the host above this
#   was the *same* work as the missing commits, which is why the gap was hard to
#   read: `git status` looked like work-in-progress rather than a stale checkout,
#   and the next pull collides on every file.
# AHEAD and DIVERGED are reported separately for the same reason.
#
# Exit codes, this repo's convention: 0 = HEAD is the remote's tip and the tree is
# clean; 1 = behind, ahead, diverged, or carrying local edits; 2 = cannot look (no
# git, no remote, no network, no such ref) — which stays a note in drift-check,
# because a host without a checkout is not a host whose deployment is stale.
#
# Env overrides exist for the tests, which build throwaway repos: MONARCH_REPO,
# MONARCH_DEPLOY_REMOTE (origin), MONARCH_DEPLOY_REF (refs/heads/main) and
# MONARCH_GIT_TIMEOUT (20s per remote call).
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=${MONARCH_REPO:-$(dirname -- "$script_dir")}
remote=${MONARCH_DEPLOY_REMOTE:-origin}
ref=${MONARCH_DEPLOY_REF:-refs/heads/main}
limit=${MONARCH_GIT_TIMEOUT:-20}
git_bin=${GIT:-git}

command -v "$git_bin" >/dev/null 2>&1 || { echo "no git on PATH"; exit 2; }
[ -e "$repo/.git" ] || { echo "$repo is not a git checkout"; exit 2; }

head=$(git -C "$repo" rev-parse HEAD 2>/dev/null) || {
  echo "cannot read HEAD in $repo"; exit 2;
}
git -C "$repo" config --get "remote.$remote.url" >/dev/null 2>&1 || {
  echo "no '$remote' remote in $repo"; exit 2;
}

# Ask the remote directly. `ls-remote` is the read-only half of `fetch`: it
# returns the tip without writing it anywhere, which keeps this safe to run from
# a timer on a host whose job is to report, not repair.
tip=$(timeout "$limit" "$git_bin" -C "$repo" ls-remote "$remote" "$ref" 2>/dev/null \
      | awk 'NR == 1 {print $1}')
[ -n "$tip" ] || { echo "cannot ask $remote for $ref (no network, or no such ref)"; exit 2; }

# For the messages only: `refs/heads/main` in a sentence reads like a path, and
# an operator is looking for "behind origin/main".
ref_name=$(printf '%s' "$ref" | sed 's|^refs/heads/||')

short=$(git -C "$repo" rev-parse --short=12 "$head" 2>/dev/null || echo "$head")
tip_short=$(printf '%s' "$tip" | cut -c1-12)
dirty=$(git -C "$repo" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
[ -n "$dirty" ] || dirty=0

if [ "$head" = "$tip" ]; then
  if [ "$dirty" -eq 0 ]; then
    echo "on $remote/$ref_name ($short), tree clean"
    exit 0
  fi
  echo "on $remote/$ref_name ($short) but the tree carries $dirty local edit(s)"
  exit 1
fi

# The relationship only counts when the objects are here. A tip this checkout has
# never fetched cannot be measured against, and guessing "behind" from a missing
# object would be the same class of error as the silence this check replaces.
if git -C "$repo" cat-file -e "$tip^{commit}" 2>/dev/null; then
  if git -C "$repo" merge-base --is-ancestor "$tip" "$head" 2>/dev/null; then
    count=$(git -C "$repo" rev-list --count "$tip..$head" 2>/dev/null || echo "?")
    echo "ahead of $remote/$ref_name by $count commit(s) ($short) - pushed?"
  elif git -C "$repo" merge-base --is-ancestor "$head" "$tip" 2>/dev/null; then
    count=$(git -C "$repo" rev-list --count "$head..$tip" 2>/dev/null || echo "?")
    echo "behind $remote/$ref_name by $count commit(s) ($short -> $tip_short)"
  else
    echo "diverged from $remote/$ref_name ($short vs $tip_short)"
  fi
else
  echo "not at $remote/$ref_name ($short vs $tip_short) - run git pull to see how far"
fi
[ "$dirty" -eq 0 ] || echo "  and the tree carries $dirty local edit(s)"
exit 1
