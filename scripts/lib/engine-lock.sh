#!/usr/bin/env bash
# The engine recovery lock: ONE actor restarts the vLLM pair at a time.
#
#   . "$(dirname "$0")/lib/engine-lock.sh"
#   engine_lock_acquire [timeout-seconds] [purpose]
#   engine_lock_release
#   engine_lock_is_held        # read-only, never blocks; 0 = someone holds it
#   engine_lock_holder         # prints the holder metadata, if any
#
# WHY A SECOND LOCK NEXT TO deploy.lock. The deploy lock serialises deploys
# against each other; it says nothing about the engine controller
# (monitoring/engine-controller/controller.py), which restarts the pair on
# its own when a rank dies. On 2026-09-11 three different actors had an
# opinion about restarting the head inside ten minutes -- the shell watchdog,
# the container healthcheck and a human -- and a head restarted twice is two
# process groups the worker cannot both join. This file and the controller
# take an flock on the SAME inode: .runtime/locks/engine-recovery.lock on the
# host, bind-mounted into the controller at /run/techsara/locks/ (contract
# §6.3). Whoever holds it owns the pair until it releases; everyone else
# waits, or is told who to wait for.
#
# Taken by scripts/cluster-up.sh, scripts/cluster-down.sh,
# scripts/cluster-recover.sh and scripts/deploy.sh around `techsara up/down`,
# and by the launcher itself (launcher/techsara_cli/cli.py:EngineRecoveryLock)
# around the pair restart inside `techsara up` -- with the same
# ENGINE_LOCK_HELD_BY hand-off, so a bare `./techsara up` is locked too.
# It WAITS (default 20 minutes -- a recovery is measured at 3-6 minutes from
# detection to READY) rather than failing instantly, for the same reason the
# deploy lock does: a recovery that is 40 seconds from finishing should be
# waited out, not turned into a second restart.
#
# Reentrant within one process tree: cluster-up.sh holds it across
# `./techsara up`, which runs cluster-sync.sh and cluster-worker.sh; a child
# that sees ENGINE_LOCK_HELD_BY pointing at a live ancestor skips instead of
# deadlocking against its own parent (the deploy lock learned this the hard
# way -- scripts/lib/deploy-common.sh).
#
# Nothing here starts, stops or restarts anything.

ENGINE_LOCK_ROOT="${ENGINE_LOCK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENGINE_LOCK_DIR="$ENGINE_LOCK_ROOT/.runtime/locks"
ENGINE_LOCK_FILE="$ENGINE_LOCK_DIR/engine-recovery.lock"
ENGINE_LOCK_HOLDER_FILE="$ENGINE_LOCK_DIR/engine-recovery.holder"
ENGINE_LOCK_FD=""

_engine_lock_say()  { printf '%s engine-lock: %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
_engine_lock_warn() { printf '%s engine-lock: %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# engine_lock_holder: print who holds (or last held) the lock. Scripts write
# key=value metadata to the .holder file; the controller writes one line
# ("engine-controller pid=... at=...") INTO the lock file itself
# (monitoring/engine-controller/controller.py:_try_lock). Both are shown, so an
# operator blocked by a recovery is told who, not just "busy".
engine_lock_holder() {
  local shown=0
  if [ -s "$ENGINE_LOCK_HOLDER_FILE" ]; then sed 's/^/    /' "$ENGINE_LOCK_HOLDER_FILE"; shown=1; fi
  if [ -s "$ENGINE_LOCK_FILE" ]; then sed 's/^/    /' "$ENGINE_LOCK_FILE"; shown=1; fi
  [ "$shown" = 1 ] || printf '    (the holder left no metadata: an older script, the controller mid-write, or a crash)\n'
}

# _engine_lock_open VARNAME: open the lock file on a fresh descriptor, for
# writing when this user may (so the holder note can be cleared), read-only
# otherwise -- flock(2) works on O_RDONLY. The controller creates the file
# as root 0644 whenever it is missing, and before this fallback every
# operator script read such a file as "permanently held" and deploy.sh
# rolled back on it (review round 1). Returns 1 only when the file cannot
# be opened at all, which is a permissions problem, not a held lock.
_engine_lock_open() {
  local __var="$1" __fd
  if { exec {__fd}>>"$ENGINE_LOCK_FILE"; } 2>/dev/null; then
    printf -v "$__var" '%s' "$__fd"; return 0
  fi
  if { exec {__fd}<"$ENGINE_LOCK_FILE"; } 2>/dev/null; then
    printf -v "$__var" '%s' "$__fd"; return 0
  fi
  return 1
}

# engine_lock_is_held: 0 when another process holds the lock, 1 when it is free
# or does not exist. Read-only; opens and immediately closes its own descriptor.
engine_lock_is_held() {
  [ -e "$ENGINE_LOCK_FILE" ] || return 1
  # A subshell: its descriptor (and the flock, if it got one) die with it.
  # A file that cannot be opened either way reads as "held" -- the
  # conservative answer -- and says so on stderr.
  if ( fd=""; _engine_lock_open fd || { _engine_lock_warn "cannot open $ENGINE_LOCK_FILE (permissions); treating it as held"; exit 2; }; flock -n "$fd" ); then
    return 1
  fi
  return 0
}

# engine_lock_acquire [timeout] [purpose]: block until the lock is ours or the
# timeout passes. On timeout, prints the holder and returns 1 -- callers under
# `set -e` exit; callers that prefer to decide should test the status.
engine_lock_acquire() {
  local timeout="${1:-1200}" purpose="${2:-engine restart}"

  if [ -n "${ENGINE_LOCK_HELD_BY:-}" ] && kill -0 "$ENGINE_LOCK_HELD_BY" 2>/dev/null; then
    _engine_lock_say "already held by pid $ENGINE_LOCK_HELD_BY (an ancestor of this process); not re-locking for '$purpose'"
    return 0
  fi

  mkdir -p "$ENGINE_LOCK_DIR" || { _engine_lock_warn "cannot create $ENGINE_LOCK_DIR"; return 1; }
  [ -e "$ENGINE_LOCK_FILE" ] || : >>"$ENGINE_LOCK_FILE" 2>/dev/null || true
  if ! _engine_lock_open ENGINE_LOCK_FD; then
    _engine_lock_warn "cannot open $ENGINE_LOCK_FILE (permissions: $(stat -c '%U:%G %a' "$ENGINE_LOCK_FILE" 2>/dev/null || echo '?')); this is NOT a held lock -- chown it to $(id -un) and retry"
    return 1
  fi

  if ! flock -n "$ENGINE_LOCK_FD"; then
    _engine_lock_say "another actor is restarting the engine pair; waiting up to ${timeout}s for '$purpose'"
    engine_lock_holder
    if ! flock -w "$timeout" "$ENGINE_LOCK_FD"; then
      _engine_lock_warn "the engine recovery lock is still held after ${timeout}s; refusing to run '$purpose' beside it."
      _engine_lock_warn "current holder:"
      engine_lock_holder >&2
      _engine_lock_warn "check: curl -s http://127.0.0.1:9838/state | python3 -m json.tool   (the controller's view)"
      eval "exec ${ENGINE_LOCK_FD}>&-"
      ENGINE_LOCK_FD=""
      return 1
    fi
  fi

  # Written INSIDE the lock, so it always describes the process that holds it.
  # The lock file's own content is the controller's note from ITS last hold;
  # cleared here so nobody reads a released controller as the current holder.
  { : >"$ENGINE_LOCK_FILE"; } 2>/dev/null || true
  {
    printf 'purpose=%s\n' "$purpose"
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$(hostname 2>/dev/null || echo '?')"
    printf 'actor=%s\n' "${GITHUB_ACTOR:-${SUDO_USER:-${USER:-unknown}}}"
    printf 'origin=%s\n' "${GITHUB_RUN_ID:+github-actions run ${GITHUB_RUN_ID}}${GITHUB_RUN_ID:-manual shell}"
    printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$ENGINE_LOCK_HOLDER_FILE" 2>/dev/null || true

  export ENGINE_LOCK_HELD_BY=$$
  # Release on any exit, WITHOUT clobbering a trap the caller already set: the
  # existing handler (if any) runs after ours.
  local existing
  existing="$(trap -p EXIT | sed -E "s/^trap -- '(.*)' EXIT$/\1/")"
  # shellcheck disable=SC2064
  trap "engine_lock_release${existing:+; $existing}" EXIT
  _engine_lock_say "acquired for '$purpose' (pid $$)"
  return 0
}

engine_lock_release() {
  [ "${ENGINE_LOCK_HELD_BY:-}" = "$$" ] || return 0
  unset ENGINE_LOCK_HELD_BY
  : >"$ENGINE_LOCK_HOLDER_FILE" 2>/dev/null || true
  if [ -n "$ENGINE_LOCK_FD" ]; then
    eval "exec ${ENGINE_LOCK_FD}>&-" 2>/dev/null || true
    ENGINE_LOCK_FD=""
  fi
  _engine_lock_say "released"
}
