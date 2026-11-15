#!/usr/bin/env bash
# Update this checkout from origin/main without ever discarding local work.
#
# Runs from the systemd user timer in systemd/audiosync-update.timer on every
# device.  It fast-forwards a clean main branch, runs the unit tests, and
# reports the resulting commit.  A dirty tree, a detached HEAD, a branch that
# is ahead or diverged, or a failing test run is reported and left untouched:
# this script never resets, rebases, or force-pulls.
set -euo pipefail

BRANCH="main"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

log() { printf '[audiosync-update] %s\n' "$*"; }
fail() { log "error: $*"; exit 1; }
short() { git rev-parse --short "$1" 2>/dev/null || printf '?'; }

command -v git >/dev/null 2>&1 || fail "git is not installed"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
git rev-parse --git-dir >/dev/null 2>&1 || fail "$REPO is not a git checkout"

# A manual run and the timer must not update the same checkout at once.
if command -v flock >/dev/null 2>&1; then
  lock_dir="${XDG_RUNTIME_DIR:-/tmp}"
  exec 9>"$lock_dir/audiosync-update.lock"
  if ! flock -n 9; then
    log "another update is already running; nothing to do"
    exit 0
  fi
fi

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[ "$branch" = "$BRANCH" ] || fail "expected branch $BRANCH, found ${branch:-detached HEAD}; refusing to update"

if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  git status --short | sed 's/^/  /' >&2
  fail "working tree is dirty; refusing to update (commit or remove the changes first)"
fi

before="$(git rev-parse HEAD)"
export GIT_TERMINAL_PROMPT=0
if ! git fetch --quiet --prune origin "$BRANCH"; then
  fail "git fetch from origin/$BRANCH failed (network or remote unavailable)"
fi
remote="$(git rev-parse "origin/$BRANCH")"

if [ "$before" = "$remote" ]; then
  log "already up to date at $(short HEAD) on $BRANCH"
  exit 0
fi

if git merge-base --is-ancestor "$before" "$remote"; then
  log "fast-forwarding $(short "$before") -> $(short "$remote")"
  git merge --quiet --ff-only "origin/$BRANCH" || fail "fast-forward failed"
elif git merge-base --is-ancestor "$remote" "$before"; then
  fail "local $BRANCH is ahead of origin/$BRANCH ($(short "$before") vs $(short "$remote")); refusing to reset"
else
  fail "local $BRANCH has diverged from origin/$BRANCH ($(short "$before") vs $(short "$remote")); refusing to reset"
fi

after="$(git rev-parse HEAD)"
log "updated to $(short HEAD) on $BRANCH"
changed="$(git diff --name-only "$before" "$after")"
if [ -n "$changed" ]; then
  printf '%s\n' "$changed" | sed 's/^/  changed: /'
fi

# Code-only updates leave the running Snapcast units alone.  Restart a unit
# only when its own template changed; PipeWire and install.sh are never run.
restart=()
for unit in snapserver snapcast-capture snapclient; do
  if printf '%s\n' "$changed" | grep -qx "systemd/$unit.service" &&
     systemctl --user cat "$unit.service" >/dev/null 2>&1; then
    restart+=("$unit.service")
  fi
done
if [ "${#restart[@]}" -ne 0 ]; then
  log "restarting changed unit(s): ${restart[*]}"
  systemctl --user restart "${restart[@]}" || fail "restarting ${restart[*]} failed"
  sleep 2
  for unit in "${restart[@]}"; do
    log "$unit: $(systemctl --user is-active "$unit" 2>/dev/null || true)"
  done
else
  log "no audiosync unit definition changed; no unit restart"
fi

log "running unit tests"
test_log="$(mktemp "${TMPDIR:-/tmp}/audiosync-update-tests.XXXXXX")"
trap 'rm -f "$test_log"' EXIT
if ! QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -t . >"$test_log" 2>&1; then
  tail -n 40 "$test_log" >&2
  fail "test suite failed at $(short HEAD)"
fi
grep -E '^(Ran |OK)' "$test_log" | tail -n 2 | sed 's/^/  /'

if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  git status --short | sed 's/^/  /' >&2
  fail "tests left the working tree dirty; refusing to report success"
fi

log "success: $(short HEAD) on $BRANCH, clean tree, tests passed"
if systemctl --user cat snapclient.service >/dev/null 2>&1; then
  log "snapclient.service: $(systemctl --user is-active snapclient.service 2>/dev/null || true)"
fi
