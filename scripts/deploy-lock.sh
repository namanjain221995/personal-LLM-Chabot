#!/usr/bin/env bash
# Run something under the SAME lock every deploy path takes.
#
#   scripts/deploy-lock.sh --status
#   scripts/deploy-lock.sh [--wait SECONDS] [--why TEXT] -- COMMAND [ARGS...]
#
# WHY A SCRIPT AND NOT JUST THE WORKFLOW'S `concurrency:`
#
# GitHub Actions' `concurrency: group: deploy-dgx-spark` serialises WORKFLOW
# RUNS against each other. It knows nothing about a person in a terminal, and
# the person is the one who is about to be surprised: two `techsara up` runs on
# the same project fight over the same containers and over the launcher's own
# lock, and the loser leaves the stack half-recreated.
#
# scripts/deploy.sh, deploy-preflight.sh and deploy-rollback.sh all take a real
# flock on .runtime/locks/deploy.lock, and the workflow reaches the box only by
# running deploy.sh — so those paths are already serialised against each other,
# whichever direction they came from. What this wrapper adds is a way to put
# ANY other stack-touching command inside the same lock:
#
#   scripts/deploy-lock.sh --why "manual model swap" -- ./techsara up
#   scripts/deploy-lock.sh --why "restart the router" -- docker compose ... up -d
#
# `./techsara up` run bare is the remaining hole, and it is a hole this
# workstream cannot close from scripts/ alone: the launcher would have to take
# the lock itself. Until it does, running it through this wrapper is the
# convention that keeps the guarantee.
#
# --status is read-only and never blocks, so it is safe to call from anything.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

WAIT="${DEPLOY_LOCK_WAIT:-1800}"; WHY=""; STATUS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status) STATUS=1 ;;
    --wait) WAIT="${2:?--wait needs seconds}"; shift ;;
    --why) WHY="${2:?--why needs a description}"; shift ;;
    --) shift; break ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) dr_die "unknown option: $1 (put the command after --)" ;;
  esac
  shift
done

HOLDER="$DR_ROOT/.runtime/locks/deploy.holder"

if [ "$STATUS" = 1 ]; then
  if dr_lock_is_held; then
    printf 'deploy lock: HELD\n'
    if [ -s "$HOLDER" ]; then sed 's/^/  /' "$HOLDER"; else printf '  (the holder left no metadata)\n'; fi
    exit 0
  fi
  printf 'deploy lock: free\n'
  exit 0
fi

[ $# -gt 0 ] || dr_die "nothing to run. Put the command after --, or use --status."

dr_lock_acquire "$WAIT" "${WHY:-$1}"
dr_say "lock held; running: $*"
set +e
"$@"
rc=$?
set -e
dr_say "command exited $rc; releasing the lock"
exit "$rc"
