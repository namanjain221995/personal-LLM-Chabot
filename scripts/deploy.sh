#!/usr/bin/env bash
# Deploy a commit to THIS machine: fast-forward the production checkout, rebuild
# the application images, recreate the containers whose definition changed, and
# refuse to call it a success until the stack actually answers.
#
#   scripts/deploy.sh [--ref origin/main] [--branch NAME] [--full] [--dry-run]
#                     [--no-rollback]
#
#   --full          `techsara down` first, so EVERY container is recreated.
#                   Costs 6-10 extra minutes because the 27B model reloads.
#                   Without it the launcher recreates only what changed, which
#                   for an app-only change leaves the model containers running.
#   --branch NAME   leave the production checkout ON local branch NAME, fast-
#                   forwarded to the deployed commit, instead of on a detached
#                   HEAD. Also settable as DEPLOY_BRANCH=NAME; empty or unset
#                   keeps the detached-HEAD behaviour exactly as it was.
#   --dry-run       resolve and report; change nothing.
#   --no-rollback   leave a failed deploy in place for inspection.
#
# Deliberate properties:
#   * ONE deploy at a time (flock), because two `techsara up` runs would fight
#     over the same containers and the launcher's own lock.
#   * Never `down -v`: the named volumes are the database, the warehouse, the
#     vector index and the reports.
#   * `.env`, `.runtime/` and the model cache are git-ignored, so resetting the
#     checkout cannot touch credentials, runtime state or the 41 GB of weights.
#   * A dirty production tree aborts the deploy rather than discarding someone's
#     work in progress.
#   * A configured DEPLOY_BRANCH is FAST-FORWARDED ONLY. This checkout is also
#     the machine owner's working directory, so the deploy will never reset,
#     rebase or force-move their branch: if the branch cannot fast-forward to
#     the deployed commit it is left untouched and the deploy detaches instead,
#     which still ships the right code.
#   * On a failed health gate it rolls back to the commit that was live before.
set -euo pipefail

ROOT="${TECHSARA_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REF="${DEPLOY_REF:-origin/main}"
# Empty means "detached HEAD", which is what this script did before the setting
# existed. Any non-empty value is a LOCAL branch name to land the checkout on.
DEPLOY_BRANCH="${DEPLOY_BRANCH:-}"
FULL=0; DRY=0; ROLLBACK=1
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --branch) DEPLOY_BRANCH="${2:-}"; shift ;;
    --full) FULL=1 ;;
    --dry-run) DRY=1 ;;
    --no-rollback) ROLLBACK=0 ;;
    # Print the whole header block, so adding to it cannot desync a line range.
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

cd "$ROOT"
LOG_DIR="$ROOT/.runtime/logs"; mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/deploy-$STAMP.log"
say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
die() { printf '%s ERROR %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; exit 1; }

# ---------------------------------------------------------------- one at a time
exec 9>"$ROOT/.runtime/locks/deploy.lock"
if ! flock -n 9; then
  die "another deploy holds $ROOT/.runtime/locks/deploy.lock"
fi

say "deploy start  root=$ROOT ref=$REF full=$FULL branch=${DEPLOY_BRANCH:-<detached>} log=$LOG"

# ------------------------------------------------------------------- preflight
[ -f "$ROOT/techsara" ] || die "$ROOT is not a TechSara checkout"
[ -f "$ROOT/.env" ] || die "$ROOT/.env is missing - the launcher needs it and it is never in git"
command -v docker >/dev/null || die "docker is not on PATH for this user"
docker info >/dev/null 2>&1 || die "this user cannot talk to the Docker socket"

DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ] && [ "${FORCE_DIRTY:-0}" != "1" ]; then
  say "refusing to deploy: the production checkout has uncommitted tracked changes"
  printf '%s\n' "$DIRTY" | tee -a "$LOG"
  die "commit, stash, or re-run with FORCE_DIRTY=1 (which DISCARDS the above)"
fi

git -C "$ROOT" fetch --quiet --prune origin || die "git fetch failed"
# Accept a SHA, "origin/main", or a bare branch name. The REMOTE is tried FIRST
# for a bare name: this checkout keeps local branches that are not deployed from
# and go stale, and resolving "main" against the local branch shipped a commit
# 30 behind origin/main in testing. A SHA or an explicit origin/... is used
# as given.
if git -C "$ROOT" rev-parse --verify --quiet "${REF}^{commit}" >/dev/null && \
   case "$REF" in origin/*|refs/*) true ;; *) [ "${#REF}" -ge 7 ] && [ -z "${REF//[0-9a-fA-F]/}" ] ;; esac; then
  TARGET="$(git -C "$ROOT" rev-parse --verify "${REF}^{commit}")"      # SHA or explicit remote ref
else
  TARGET="$(git -C "$ROOT" rev-parse --verify --quiet "origin/${REF}^{commit}" \
         || git -C "$ROOT" rev-parse --verify --quiet "${REF}^{commit}")" \
    || die "cannot resolve '$REF' as a commit, origin/$REF, or a local ref"
fi
PREVIOUS="$(git -C "$ROOT" rev-parse HEAD)"
say "current=$PREVIOUS"
say "target =$TARGET  $(git -C "$ROOT" log -1 --pretty='%s' "$TARGET" | cut -c1-72)"

if [ "$TARGET" = "$PREVIOUS" ]; then
  say "already at the target commit; still reconciling containers so a manual "
  say "docker change cannot leave the box drifted from the repo"
fi
if [ -z "$DEPLOY_BRANCH" ]; then
  say "branch =<none>  DEPLOY_BRANCH is unset, so HEAD will be detached at the target"
elif ! git -C "$ROOT" show-ref --verify --quiet "refs/heads/$DEPLOY_BRANCH"; then
  say "branch =$DEPLOY_BRANCH  (no such local branch yet; it will be created)"
elif git -C "$ROOT" merge-base --is-ancestor "$DEPLOY_BRANCH" "$TARGET"; then
  say "branch =$DEPLOY_BRANCH  (fast-forwards to the target)"
else
  say "branch =$DEPLOY_BRANCH  (CANNOT fast-forward: $(git -C "$ROOT" rev-list --left-right --count "$DEPLOY_BRANCH...$TARGET" | tr -s '\t ' '/') ahead/behind; the branch would be left alone and HEAD detached)"
fi

if [ "$DRY" = 1 ]; then say "dry run: nothing changed"; exit 0; fi

# ------------------------------------------------------------ where HEAD lands
# This checkout is production AND the machine owner's working directory, so the
# deploy has two duties that pull against each other: the tree must end up at
# the deployed commit, and the owner's branches must never be rewritten. The
# rule that satisfies both is "fast-forward or nothing".

detach_to() {  # detach_to <sha> <why> - ship the right code, touch no branch
  local sha="$1" why="$2"
  say "  branch: NOT moving $DEPLOY_BRANCH - $why"
  say "  branch: the branch is left exactly where it was; nothing was reset,"
  say "  branch: rebased, force-moved or discarded. Deploying $sha on a DETACHED"
  say "  branch: HEAD instead, so the code being served is still correct."
  say "  branch: merge or rebase $DEPLOY_BRANCH yourself, then deploy again."
  git -C "$ROOT" checkout --detach --force --quiet "$sha"
}

land_on_branch() {  # land_on_branch <sha> - end up ON $DEPLOY_BRANCH at <sha>
  local sha="$1" b="$DEPLOY_BRANCH" err counts ahead behind

  # (a) A branch that exists only on the remote is created to track it. A branch
  #     that exists nowhere is created at the target - inventing a ref that
  #     points at nothing cannot destroy history, and it is the fresh-clone case.
  if ! git -C "$ROOT" show-ref --verify --quiet "refs/heads/$b"; then
    if git -C "$ROOT" show-ref --verify --quiet "refs/remotes/origin/$b"; then
      say "  branch: $b is not a local branch yet; creating it to track origin/$b"
      git -C "$ROOT" branch --quiet --track "$b" "origin/$b" \
        || { detach_to "$sha" "could not create $b from origin/$b"; return $?; }
    else
      say "  branch: $b exists neither locally nor on origin; creating it at the target"
      git -C "$ROOT" branch --quiet "$b" "$sha" \
        || { detach_to "$sha" "could not create $b"; return $?; }
    fi
  fi

  # (b) git REFUSES this when the branch is checked out in another worktree.
  #     That is a real configuration on this box, so catch it instead of
  #     fighting it with --ignore-other-worktrees.
  if ! err="$(git -C "$ROOT" checkout --force --quiet "$b" 2>&1)"; then
    say "  branch: git refused to check out $b: ${err//$'\n'/ | }"
    detach_to "$sha" "it could not be checked out (checked out in another worktree?)"
    return $?
  fi

  # (d) Decided before anything is moved, by a read-only ancestry test: if the
  #     branch holds commits the target does not contain, fast-forwarding is
  #     impossible and the ONLY safe move is to leave it alone.
  if ! git -C "$ROOT" merge-base --is-ancestor "$b" "$sha"; then
    counts="$(git -C "$ROOT" rev-list --left-right --count "$b...$sha" 2>/dev/null || printf '? ?')"
    ahead="${counts%%[[:space:]]*}"; behind="${counts##*[[:space:]]}"
    say "  branch: $b CANNOT be fast-forwarded to $sha"
    say "  branch: git rev-list --left-right --count $b...$sha -> $ahead $behind"
    say "  branch: $b holds $ahead commit(s) that $sha does not contain, and is"
    say "  branch: missing $behind commit(s) that it does - it has diverged."
    detach_to "$sha" "fast-forward is not possible ($ahead ahead / $behind behind)"
    return $?
  fi

  # (c) The fast-forward itself. Cannot create a commit and cannot lose one.
  behind="$(git -C "$ROOT" rev-list --count "$b..$sha" 2>/dev/null || printf '?')"
  if ! git -C "$ROOT" merge --ff-only --quiet "$sha" >>"$LOG" 2>&1; then
    say "  branch: $b fast-forwards on paper but git merge --ff-only failed (see $LOG)"
    detach_to "$sha" "the fast-forward itself failed"
    return $?
  fi
  if [ "$behind" = 0 ]; then
    say "  branch: on $b, already at $sha - no fast-forward needed"
  else
    say "  branch: on $b, fast-forwarded $behind commit(s) to $sha"
  fi
  return 0
}

LANDED_ON="(detached)"   # set by land(), reported at the end and by the workflow
land() {  # land <sha> - make the production checkout BE <sha>, non-destructively
  local sha="$1" head on
  if [ -n "$DEPLOY_BRANCH" ]; then
    land_on_branch "$sha" || return 1
  else
    # Detach rather than reset: the production checkout may be sitting on a
    # branch (it was on "dev"), and `reset --hard` would MOVE that branch to the
    # deployed commit, silently rewriting the developer's own branch pointer.
    # Detaching moves only HEAD, so branches are left exactly where they were.
    git -C "$ROOT" checkout --detach --force --quiet "$sha" || return 1
    say "  branch: DEPLOY_BRANCH is unset - detaching HEAD at $sha"
  fi
  git -C "$ROOT" clean -qfd -e .env -e .runtime || true

  # Never negotiable: whatever route was taken above, the tree that is about to
  # be built and started MUST be the commit we were asked to ship. Shipping a
  # checkout that does not match the request is worse than not deploying.
  head="$(git -C "$ROOT" rev-parse HEAD)"
  if [ "$head" != "$sha" ]; then
    die "aborting: HEAD is $head but the deploy target is $sha. Nothing was built\
 or restarted, so the containers are still serving the previous commit."
  fi
  on="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  if [ "$on" = HEAD ]; then on="(detached)"; fi
  LANDED_ON="$on"
  say "  checkout: HEAD=$sha  branch=$on"
  return 0
}

# --------------------------------------------------------------------- deploy
apply() {  # apply <sha> - move the checkout and bring the stack up
  local sha="$1"
  land "$sha" || return 1
  if [ "$FULL" = 1 ]; then
    # Every container goes, models included. `down` never passes -v, so the
    # database, warehouse, vector index and reports all survive; what is paid
    # for is time, not data: the 27B reloads (~6-10 min) and the auxiliary
    # model servers restart behind it.
    say "  full restart: techsara down, then up (volumes preserved; expect ~10-15 min)"
    ( cd "$ROOT" && ./techsara down ) >>"$LOG" 2>&1 || return 1
  fi
  say "  techsara up  (builds images, recreates changed services, staged health gates)"
  ( cd "$ROOT" && ./techsara up ) >>"$LOG" 2>&1
}

# ---------------------------------------------------------------- health gate
health() {
  local orch front api bind port
  # Ports and bind address follow the generated env, not an assumption.
  port="$(grep -m1 '^ORCHESTRATOR_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2)"; port="${port:-8080}"
  front="$(grep -m1 '^FRONTEND_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2)"; front="${front:-3000}"

  orch="$(curl -fsS -m 20 "http://127.0.0.1:${port}/health" 2>/dev/null)" || { say "  health: orchestrator did not answer on ${port}"; return 1; }
  local status
  status="$(printf '%s' "$orch" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","?"))' 2>/dev/null)"
  local checks
  checks="$(printf '%s' "$orch" | python3 -c '
import json,sys
d=json.load(sys.stdin)
bad=[k for k,v in d.get("checks",{}).items() if v.get("status")!="ok"]
print(",".join(bad) if bad else "-")' 2>/dev/null)"
  say "  health: orchestrator status=$status failing_checks=$checks"
  # `degraded` is a legitimate steady state: duckdb reports an error while the
  # sync worker holds the single-writer lock, and optional model roles may be
  # off. Only app_db and vllm being down mean the deploy is not serving.
  case "$checks" in
    *app_db*) say "  health: app_db is down"; return 1 ;;
    *vllm-router*|*vllm-embed*) : ;;
  esac
  case ",$checks," in *,vllm,*) say "  health: the main model is not answering"; return 1 ;; esac

  curl -fsS -m 20 -o /dev/null "http://127.0.0.1:${front}/" || { say "  health: frontend did not answer on ${front}"; return 1; }
  say "  health: frontend ok"

  # A real completion, not just /v1/models: the API server answers that even
  # when the engine behind it is dead.
  local reply
  reply="$(curl -fsS -m 180 -H 'Content-Type: application/json' \
      -d '{"model":"'"$(grep -m1 '^MAIN_MODEL=' "$ROOT/.runtime/generated.env" | cut -d= -f2)"'","messages":[{"role":"user","content":"Reply with the single word: READY."}],"max_tokens":8,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}' \
      "http://127.0.0.1:$(grep -m1 '^VLLM_PORT=' "$ROOT/.runtime/generated.env" | cut -d= -f2)/v1/chat/completions" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip()[:20])' 2>/dev/null)" || true
  if [ -z "$reply" ]; then say "  health: the model did not complete a request"; return 1; fi
  say "  health: model answered ${reply@Q}"

  if grep -q '^TECHSARA_CLUSTER_MODE=dual$' "$ROOT/.runtime/generated.env" 2>/dev/null; then
    if "$ROOT/scripts/cluster-status.sh" >>"$LOG" 2>&1; then say "  health: two-node cluster ok"
    else say "  health: cluster-status reported a failure"; return 1; fi
  fi
  return 0
}

if apply "$TARGET"; then
  if health; then
    say "DEPLOYED $TARGET  checkout is on $LANDED_ON"
    git -C "$ROOT" log -1 --pretty='  %h %s' "$TARGET" | tee -a "$LOG"
    exit 0
  fi
  say "health gate FAILED after deploying $TARGET"
else
  say "techsara up FAILED for $TARGET"
  tail -25 "$LOG" >&2 || true
fi

if [ "$ROLLBACK" != 1 ]; then die "left in place for inspection (--no-rollback)"; fi
if [ "$TARGET" = "$PREVIOUS" ]; then
  die "the checkout was already at $TARGET, so this was a reconcile rather than a\
 version change and there is no earlier commit to return to. The stack is in the\
 state the health gate rejected - inspect it with scripts/cluster-status.sh"
fi
say "ROLLING BACK to $PREVIOUS"
if [ -n "$DEPLOY_BRANCH" ]; then
  # A rollback wants an EARLIER commit, so landing $DEPLOY_BRANCH on it would
  # mean rewinding the branch - the one thing this script never does. HEAD goes
  # back, the branch stays put, and the log says so rather than surprising the
  # owner of the checkout.
  say "  note: HEAD is being moved back, but $DEPLOY_BRANCH is NOT rewound:"
  say "  note: rewinding a branch discards commits from it, and this script"
  say "  note: only ever fast-forwards. Expect to finish on a detached HEAD."
fi
if apply "$PREVIOUS" && health; then
  die "deploy of $TARGET failed; rolled back to $PREVIOUS and the stack is healthy again"
fi
die "deploy of $TARGET failed AND the rollback to $PREVIOUS did not come up healthy - this box needs a human"
