#!/usr/bin/env bash
# Deploy a commit to THIS machine: fast-forward the production checkout, rebuild
# the application images, recreate the containers whose definition changed, and
# refuse to call it a success until the stack actually answers.
#
#   scripts/deploy.sh [--ref origin/main] [--branch NAME] [--full] [--dry-run]
#                     [--no-rollback]
#   scripts/deploy.sh --print-v1-gateway-sha
#
#   --full          `techsara down` first, so EVERY container is recreated.
#                   Costs 6-10 extra minutes because the main model reloads.
#                   Without it the launcher recreates only what changed, which
#                   for an app-only change leaves the model containers running.
#   --branch NAME   leave the production checkout ON local branch NAME, fast-
#                   forwarded to the deployed commit, instead of on a detached
#                   HEAD. Also settable as DEPLOY_BRANCH=NAME; empty or unset
#                   keeps the detached-HEAD behaviour exactly as it was.
#   --dry-run       resolve and report; change nothing.
#   --no-rollback   leave a failed deploy in place for inspection.
#   --print-v1-gateway-sha
#                   print the v1-gateway's code digest for this checkout and
#                   exit (no lock, no git). Export it as V1_GATEWAY_CODE_SHA
#                   before any hand-run `docker compose` with the launcher's
#                   chain, or that command renders the gateway as `:unpinned`.
#
# The public /v1 relay (v1-gateway, added 2026-09-13) exists to keep developer
# connections alive THROUGH the deploys this script runs, so this script
# reports what each deploy does to it and to public work in flight. It does
# not wait for either by default (deploys never wait); a hand-run deploy can
# opt in to a bounded wait: see "the v1 gateway and public work in flight".
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
#     which still ships the right code. A rollback undoes the fast-forward this
#     same run made - by compare-and-swap, so it can only ever put the branch
#     back where this run found it - and never rewinds it any further.
#   * On a failed health gate it rolls back to the commit that was live before -
#     but ONLY if that is honest. See "the schema boundary" below.
#
# Recoverability, added 2026-09-07. Four things this script now does that
# "rebuild the tag and restart" did not:
#
#   * RECORD BEFORE REPLACING. scripts/deploy-record.sh writes what is running -
#     image IDS (not tags), the rendered configuration with every env value
#     hashed, the applied migration list, the PostgreSQL major - into
#     .runtime/releases/<stamp>/ before anything is recreated. After the
#     recreate those facts are gone, and a rollback needs all of them.
#   * PROMOTE BY DIGEST. scripts/deploy-preflight.sh builds the application
#     images ONCE, records each `{{.Id}}` in a release manifest, and after
#     `techsara up` VERIFIES that the running containers were created from
#     exactly those ids. A tag is a mutable pointer; assuming the rebuilt tag is
#     the tested artifact is a guess, and this is where the guess gets checked.
#   * DRAIN. scripts/deploy-drain.sh reports the stop_grace_period each
#     recreated service will actually get and waits for a quiet moment before
#     the SIGTERM. There is no second stack to shift traffic to on this
#     hardware, so a graceful stop is the whole of what "drain" can honestly
#     mean here.
#   * THE SCHEMA BOUNDARY. orchestrator/app/db.py migrates FORWARD ONLY. An
#     image rollback does not roll the database back, so this script compares
#     the migration table in the target COMMIT against the version the live
#     database has APPLIED, and refuses rather than starting old code on a
#     newer schema. That check guards the deploy AND the automatic rollback -
#     the rollback especially, because it is the one that fires unattended.
#     It never restores a database: a pre-deploy dump is older than every row
#     written since, and restoring it to fix a code problem deletes real data.
#   * THE ROLLBACK GATE CANNOT FAIL OPEN (2026-09-23). The rollback used to
#     consult the same schema_gate() as the forward deploy, and that function
#     returns 0 without checking anything when the live schema version cannot
#     be read. A rollback is only reached when the health gate failed or
#     `techsara up` failed - i.e. when /health is down and the compose project
#     may be mid-recreate - so the unreadable case was not an edge, it was the
#     expected one. Now the verdict is computed at the forward gate while the
#     stack is still healthy, printed, recorded in the release directory, and
#     only CONSULTED at rollback time. Missing, unparseable, forward-only, or
#     a live version that cannot be read: all four REFUSE.
#     The FORWARD gate is unchanged, including its fail-open. It runs with a
#     human watching; the rollback does not.
set -euo pipefail

ROOT="${TECHSARA_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REF="${DEPLOY_REF:-origin/main}"
# Empty means "detached HEAD", which is what this script did before the setting
# existed. Any non-empty value is a LOCAL branch name to land the checkout on.
DEPLOY_BRANCH="${DEPLOY_BRANCH:-}"
FULL=0; DRY=0; ROLLBACK=1; PRINT_V1_GATEWAY_SHA=0

# The v1-gateway's code digest: sha256 over `sha256sum` of its image inputs
# (Dockerfile, server.cjs, every regular file under lib/), sorted by path
# bytes. launcher/techsara_cli/cli.py v1_gateway_code_sha is the same
# computation (launcher/tests/test_v1_gateway_pin.py holds the two equal). A
# README or test edit under gateway/ is not an input, so it recreates nothing.
v1_gateway_code_sha() {  # v1_gateway_code_sha ROOT -> the digest, or nothing when ROOT has no gateway
  local gw="$1/gateway" inputs=(Dockerfile server.cjs)
  [ -f "$gw/Dockerfile" ] && [ ! -L "$gw/Dockerfile" ] && [ -f "$gw/server.cjs" ] && [ ! -L "$gw/server.cjs" ] || return 0
  [ -d "$gw/lib" ] && inputs+=(lib)
  ( cd "$gw" && find "${inputs[@]}" -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 )
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --branch) [ $# -ge 2 ] || { echo "--branch needs a branch name (use '' for a detached HEAD)" >&2; exit 2; }
              DEPLOY_BRANCH="$2"; shift ;;
    --full) FULL=1 ;;
    --dry-run) DRY=1 ;;
    --no-rollback) ROLLBACK=0 ;;
    --print-v1-gateway-sha) PRINT_V1_GATEWAY_SHA=1 ;;
    # Print the whole header block, so adding to it cannot desync a line range.
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$PRINT_V1_GATEWAY_SHA" = 1 ]; then
  printf '%s\n' "$(v1_gateway_code_sha "$ROOT")"
  exit 0
fi

cd "$ROOT"

# The recoverability plumbing: the verified compose chain, the shared deploy
# lock, image-id reads and the schema-version readers. Sourced rather than
# duplicated so there is exactly one definition of "which compose files" and
# "which lock".
# shellcheck source=lib/deploy-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/deploy-common.sh"
DR_ROOT="$ROOT"
# The engine recovery lock (contract §6.3): the engine controller holds it
# while it restarts the vLLM pair, and `apply` below holds it across
# `techsara down`/`up` so a deploy and an automatic recovery never restart
# the pair at the same time. Same root as the deploy lock.
ENGINE_LOCK_ROOT="$ROOT"
# shellcheck source=lib/engine-lock.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/engine-lock.sh"

LOG_DIR="$ROOT/.runtime/logs"; mkdir -p "$LOG_DIR" "$ROOT/.runtime/locks"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/deploy-$STAMP.log"
say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
die() { printf '%s ERROR %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; exit 1; }
# Everything the sourced library prints lands in the same log as everything
# this script prints. A recovery reads one file, not two.
DR_LOG="$LOG"

# ------------------------------------------------- the branch name is untrusted
# DEPLOY_BRANCH arrives from a repository variable or a free-text
# workflow_dispatch input and is then handed to `git branch` and `git checkout`
# as a bare operand, with no `--` separator available on either. That is option
# injection: DEPLOY_BRANCH=-q really does turn `git branch --quiet "$b" "$sha"`
# into `git branch -q <sha>` and leaves a junk branch named after the SHA in the
# production checkout, and -D/-M aim the same trick at delete and rename.
# `git check-ref-format` alone is not enough - it happily accepts "-q", "-D",
# "--all" and "HEAD" - so the leading dash and HEAD are rejected explicitly.
if [ -n "$DEPLOY_BRANCH" ]; then
  case "$DEPLOY_BRANCH" in
    -*) die "DEPLOY_BRANCH='$DEPLOY_BRANCH' starts with '-'; git would read it as an option, not a branch name" ;;
    HEAD|@) die "DEPLOY_BRANCH='$DEPLOY_BRANCH' is a git rev shorthand, not a usable branch name; a local branch called that makes every later rev parse ambiguous" ;;
  esac
  git check-ref-format "refs/heads/$DEPLOY_BRANCH" 2>/dev/null \
    || die "DEPLOY_BRANCH='$DEPLOY_BRANCH' is not a valid git branch name"
fi

# ---------------------------------------------------------------- one at a time
# The same flock as before, with two changes that matter when the other holder
# is a person rather than a workflow.
#
#   * It WAITS (default 30 minutes) instead of failing instantly. A workflow
#     deploy that starts 40 seconds before a hand-run one should make the
#     second one queue, not fail - and requirement "never cancel a rollout
#     mid-flight" is honoured from the outside by queueing behind it.
#   * The holder writes its pid, actor, origin and commit into
#     .runtime/locks/deploy.holder, so whoever is blocked is told WHO by.
#
# GitHub Actions `concurrency: deploy-dgx-spark` serialises workflow runs
# against each other and nothing else. This lock is what actually stands
# between a workflow-triggered deploy and someone running this script by hand,
# because BOTH paths run this file. `scripts/deploy-lock.sh -- <cmd>` puts any
# other stack-touching command inside the same lock.
dr_lock_acquire "${DEPLOY_LOCK_WAIT:-1800}" "deploy.sh --ref $REF$([ "$FULL" = 1 ] && printf ' --full')"

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

# ----------------------------------------------------------- the schema gate
# Migrations in orchestrator/app/db.py go one way. `init_schema` applies what is
# missing and removes nothing, and there are no down migrations to remove it
# with. So the question a deploy has to answer BEFORE it recreates anything is
# not "is this commit newer" but "does this commit's code know about every
# migration the live database has already applied".
#
# If it does not, starting it is a schema DOWNGRADE of the code while the schema
# itself stays where it is. That does not fail cleanly - it fails as whatever
# the older code does when it meets a column, table or constraint it has never
# heard of, which can be a 500 on one route and silent wrong answers on another.
#
# The number for the target comes out of the COMMIT (git show <sha>:...db.py),
# so it is known before anything is built. The number for the database comes
# from /health, falling back to the database itself when the orchestrator is
# the thing that is down.
schema_gate() {  # schema_gate <sha> <what-this-is> -> 0 ok, 1 refuse
  local sha="$1" what="$2" target live
  live="$(dr_live_schema_version 2>/dev/null || true)"
  if [ -z "$live" ]; then
    say "  schema: cannot read the live schema version (orchestrator down and psql unavailable)."
    say "  schema: proceeding WITHOUT the compatibility check - this is the one case where"
    say "  schema: the check cannot be made, and it is worth knowing it was skipped."
    return 0
  fi
  target="$(dr_code_schema_version_from_git "$sha" 2>/dev/null || true)"
  if [ -z "$target" ]; then
    say "  schema: $sha has no readable migration table in orchestrator/app/db.py; skipping the check"
    return 0
  fi
  say "  schema: database has applied V$live; $what ($sha) knows V$target"
  if [ "$target" -ge "$live" ]; then
    [ "$target" -gt "$live" ] && say "  schema: it will apply V$((live + 1))..V$target on start (forward-only, not undoable)"
    return 0
  fi
  say "  schema: REFUSING - $what is BEHIND the database."
  say "  schema: V$((target + 1))..V$live have already run against this database and there are"
  say "  schema: no down migrations. Deploying V$target code does not move the schema back to"
  say "  schema: V$target; it runs V$target code on a V$live schema."
  say "  schema: Roll FORWARD instead - the schema is already where newer code wants it."
  say "  schema: To override deliberately: ALLOW_SCHEMA_DOWNGRADE=1"
  say "  schema: Do NOT 'fix' this by restoring a database backup. Any backup old enough"
  say "  schema: to be at V$target is older than every conversation and message written since."
  [ "${ALLOW_SCHEMA_DOWNGRADE:-0}" = 1 ] || return 1
  say "  schema: ALLOW_SCHEMA_DOWNGRADE=1 given; continuing anyway."
  return 0
}

say "checking schema compatibility before touching anything"
if ! schema_gate "$TARGET" "the commit being deployed"; then
  die "refusing to deploy $TARGET across a schema migration boundary. Nothing was\
 built, recreated or restarted; the stack is exactly as it was."
fi

# ------------------------------------------------- can this deploy be undone?
# Answered HERE, before a single container is touched, because this is the last
# moment the answer is knowable. The rollback runs after the health gate failed
# or `techsara up` failed - which is exactly when /health is down and the
# compose project may be mid-recreate, so the two reads that produce the live
# schema version can BOTH fail. schema_gate() used to return 0 in that case
# ("proceeding WITHOUT the compatibility check"), and the automatic rollback
# consulted that same gate: old code started on a newer schema, unattended,
# which is the one outcome the gate exists to prevent.
#
# So the verdict is computed now, said out loud now, written into the release
# record (deploy-record.sh --reversibility) and consulted - not re-derived - by
# the rollback below. A verdict that is missing or unreadable at that point
# makes the rollback REFUSE.
#
# The FORWARD gate above keeps its current behaviour, including its fail-open
# on an unreadable live version. That is a separate question and it is not
# widened here: the forward path runs with a human watching the run, and the
# rollback path does not.
REVERSIBILITY_VERDICT=""
compute_reversibility() {
  local live target previous sentence
  live="$(dr_live_schema_version 2>/dev/null || true)"
  target="$(dr_code_schema_version_from_git "$TARGET" 2>/dev/null || true)"
  previous="$(dr_code_schema_version_from_git "$PREVIOUS" 2>/dev/null || true)"
  REVERSIBILITY_VERDICT="$(dr_reversibility_verdict "$previous" "$target" "$live" 2>/dev/null || true)"
  if [ -z "$REVERSIBILITY_VERDICT" ]; then
    sentence="the reversibility of this deploy could NOT be computed (database=V${live:-?}, target=V${target:-?}, previous=V${previous:-?}), so an automatic rollback will REFUSE rather than guess."
    say "  reversibility: $sentence"
  else
    sentence="$(dr_reversibility_sentence "$REVERSIBILITY_VERDICT")"
    say "  reversibility: $sentence"
    say "  reversibility: $REVERSIBILITY_VERDICT"
  fi
  return 0
}
compute_reversibility

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
  local sha="$1" b="$DEPLOY_BRANCH" err counts ahead behind was on

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

  # (b) Decided BEFORE anything is moved, by a read-only ancestry test: if the
  #     branch holds commits the target does not contain, fast-forwarding is
  #     impossible and the ONLY safe move is to leave it alone. This has to run
  #     BEFORE the checkout below. Checking the branch out first would put the
  #     diverged tree on disk for as long as it takes to decide, and a deploy
  #     killed in that window (runner cancellation, the job's timeout-minutes)
  #     would leave production sitting on the wrong commit with no log saying so.
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

  # Recorded before anything moves so a rollback can undo exactly this move.
  was="$(git -C "$ROOT" rev-parse --verify --quiet "$b^{commit}")" \
    || { detach_to "$sha" "could not read the current tip of $b"; return $?; }
  behind="$(git -C "$ROOT" rev-list --count "$b..$sha" 2>/dev/null || printf '?')"

  # (c) git REFUSES this when the branch is checked out in another worktree.
  #     That is a real configuration on this box, so catch it instead of
  #     fighting it with --ignore-other-worktrees.
  if ! err="$(git -C "$ROOT" checkout --force --quiet "$b" 2>&1)"; then
    say "  branch: git refused to check out $b: ${err//$'\n'/ | }"
    detach_to "$sha" "it could not be checked out (checked out in another worktree?)"
    return $?
  fi

  # (d) The fast-forward itself - the ONLY command in this script that moves a
  #     branch. `--ff-only` cannot create a commit and cannot lose one, and the
  #     guard below makes sure it moves the branch we named rather than whatever
  #     HEAD happens to be attached to.
  on="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || printf '(detached)')"
  if [ "$on" != "$b" ]; then
    detach_to "$sha" "the checkout reported success but left HEAD on '$on', not on $b"
    return $?
  fi
  if ! git -C "$ROOT" merge --ff-only --quiet "$sha" >>"$LOG" 2>&1; then
    say "  branch: $b fast-forwards on paper but git merge --ff-only failed (see $LOG)"
    detach_to "$sha" "the fast-forward itself failed"
    return $?
  fi
  if [ "$behind" = 0 ]; then
    say "  branch: on $b, already at $sha - no fast-forward needed"
  else
    say "  branch: on $b, fast-forwarded $behind commit(s) to $sha"
    FF_BRANCH="$b"; FF_FROM="$was"; FF_TO="$sha"
  fi
  return 0
}

LANDED_ON="(detached)"   # set by land(), reported at the end and by the workflow
# Set only when land_on_branch() actually moved a branch, so that - and only
# that - can be undone if the deploy has to be rolled back.
FF_BRANCH=""; FF_FROM=""; FF_TO=""
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

# ------------------------------------------------- the engine's guard services
# ONE ACTOR RESTARTS THE PAIR, ACROSS A ROLLBACK TOO (contract §2, §6). A
# service that is gone from the compose files of the tree being applied is
# invisible to `up`: Compose leaves its container running as an orphan, with
# its restart policy and -- for these two -- its Docker socket. Rolling back
# to a tree that predates the engine controller would therefore leave the
# controller alive next to the old shell watchdog that tree starts (and the
# worker's sentinel next to whatever the old compose does), which is the
# multi-actor pattern of 2026-09-11, unattended, at 3 a.m. (review round 1).
# `--remove-orphans` is not the tool: the launcher's chain does not include
# the monitoring/tunnel overlays, so Prometheus and Grafana would count as
# orphans too. Two named guards, reconciled by hand against the rendered
# chain of the tree that was just applied, and logged in the deploy record.
ENGINE_CONTROLLER_CONTAINER="sf-local-ai-engine-controller-1"
LEGACY_WATCHDOG_CONTAINER="sf-local-ai-vllm-watchdog-1"
rendered_services() {  # the service names the applied tree's chain renders
  # The prefix is shell-quoted (shlex.join), consumed with eval like
  # deploy-drain.sh does; state.json is what the `techsara up` just wrote.
  local prefix
  prefix="$(dr_compose_prefix 2>/dev/null)" || return 1
  ( cd "$ROOT" && eval "$prefix" config --services 2>/dev/null )
}
retire_container_if_unrendered() {  # retire_container_if_unrendered NAME SERVICE SERVICES
  local name="$1" service="$2" services="$3" id
  printf '%s\n' "$services" | grep -qx "$service" && return 0
  id="$(docker ps -a --filter "name=^/${name}\$" --filter "label=com.docker.compose.service=${service}" --format '{{.ID}}' 2>/dev/null | head -n 1)"
  [ -n "$id" ] || return 0
  if docker rm -f "$id" >/dev/null 2>&1; then
    say "  guards: removed $name - the applied tree's compose chain has no '$service' service, and an orphaned guard would restart the engine on its own"
  else
    say "  guards: WARNING could not remove the orphaned $name; remove it by hand: docker rm -f $name"
  fi
}
reconcile_engine_guards() {
  local services
  services="$(rendered_services)" || { say "  guards: could not render the applied chain; leaving the guard containers as they are"; return 0; }
  retire_container_if_unrendered "$ENGINE_CONTROLLER_CONTAINER" engine-controller "$services"
  retire_container_if_unrendered "$LEGACY_WATCHDOG_CONTAINER" vllm-watchdog "$services"
  # The worker's sentinel: shipped and started by the applied tree's
  # cluster-sync.sh when that tree knows it; when the compose file the tree
  # ships no longer defines the service, the running one must go as well.
  if grep -q '^TECHSARA_CLUSTER_MODE=dual$' "$ROOT/.runtime/generated.env" 2>/dev/null \
     && [ -f "$ROOT/compose/compose.cluster-worker.yaml" ] \
     && ! grep -q '^  vllm-worker-sentinel:' "$ROOT/compose/compose.cluster-worker.yaml" \
     && [ -x "$ROOT/scripts/cluster-worker.sh" ]; then
    if "$ROOT/scripts/cluster-worker.sh" stop vllm-worker-sentinel >>"$LOG" 2>&1; then
      say "  guards: stopped the worker sentinel - the applied tree's worker compose does not define it"
    else
      say "  guards: WARNING could not stop the worker sentinel (scripts/cluster-worker.sh stop vllm-worker-sentinel)"
    fi
  fi
}

# ------------------------------------ the v1 gateway and public work in flight
# Added 2026-09-13 with the no-timeout developer API. Four facts drive it:
#
#   * The v1-gateway's image tag is its code digest, V1_GATEWAY_CODE_SHA. The
#     launcher derives it for its own Compose calls; the helpers this script
#     runs first (deploy-record.sh, deploy-preflight.sh, deploy-drain.sh)
#     render the same chain, so apply() exports the value of the tree it just
#     checked out. It is never written to an env file: those fold into the
#     orchestrator's and frontend's definitions.
#   * A routine deploy leaves the gateway running unless its definition
#     changed (its code or one of its own settings); --full removes it with
#     everything else. A recreate cuts every relay 2 s after SIGTERM, and the
#     clients resume.
#   * An orchestrator that suspends public runs on SIGTERM (the durable
#     runtime, with PUBLIC_API_RESUME_ENABLED on) cuts only the runs that are
#     not resumable (store:false, and a request whose input files were still
#     being prepared, which ends with a retryable failure); the next process
#     resumes the rest. One
#     that cannot (the code before that runtime shipped, i.e. the deploy that
#     ships it, or resume switched off) fails every run in flight.
#   * The gateway is on a public path only when the operator puts it there:
#     the tunnel's ^/v1 rule, or V1_GATEWAY_URL on the frontend (blank by
#     default, compose.yaml). v1_gateway_guard says which path /v1 takes and
#     warns when the frontend relays through the gateway while
#     PUBLIC_API_GATEWAY_PEERS is blank.
#   * DEPLOYS NEVER WAIT (no-timeout design, deploy_survival: "a deploy wait
#     can block security fixes forever"). By default both guards only SAY
#     what this deploy is about to cut. WHY NOT A BOUNDED WAIT BY DEFAULT
#     (review 2026-09-14): the waits run after the new commit is checked out
#     in the shared production checkout and under the deploy lock, and
#     nothing stops new work arriving meanwhile -- the old orchestrator keeps
#     admitting runs and the gateway keeps opening relays -- so under steady
#     traffic a count never reaches 0 and every deploy (about 70 s today)
#     would sit out the whole bound: up to 15 minutes, on the path that ships
#     security fixes. The gateway wait could not outlast the hours-long
#     streams it would be waiting for anyway.
#     A HAND-RUN deploy at a quiet moment may opt in with
#     DEPLOY_GATEWAY_DRAIN_DEADLINE / DEPLOY_PUBLIC_WORK_DEADLINE (seconds,
#     default 0 = report only), e.g. the first deploy that ships the durable
#     runtime, after the operator's in-flight check. Even then nothing waits
#     during an automatic ROLLBACK (restoring service comes first), for an
#     orchestrator that is not running (nothing is executing its runs), or
#     for a run created before the running orchestrator started (code that
#     cannot suspend cannot have resumed it either).
#
# Both guards run BEFORE the engine lock is taken, so a recovery is never held
# up by an opted-in wait, and before the connection drains, which stay closest
# to SIGTERM.
V1_GATEWAY_CONTAINER="sf-local-ai-v1-gateway-1"
# The gateway container's id when v1_gateway_guard looked, before `up`: the
# health gate fails a deploy on gateway health only when this deploy created
# or recreated the container (see v1_gateway_health).
V1_GATEWAY_ID_BEFORE=""

pin_v1_gateway() {  # pin_v1_gateway ROOT - export the tree's digest for every compose render that follows
  local sha
  sha="$(v1_gateway_code_sha "$1")"
  if [ -n "$sha" ]; then
    export V1_GATEWAY_CODE_SHA="$sha"
  else
    unset V1_GATEWAY_CODE_SHA
  fi
}

v1_gateway_relays() {  # the relays the running gateway reports on /healthz, or nothing when it cannot say
  docker exec "$V1_GATEWAY_CONTAINER" wget -q -O - http://127.0.0.1:8090/healthz 2>/dev/null \
    | python3 -c 'import json, sys
v = json.load(sys.stdin).get("relays")
print(v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else "")' 2>/dev/null || true
}

v1_gateway_rendered_hash() {  # the definition hash `up -d` will compare, or nothing
  # The gateway has no env_file, so `config --hash` is the whole comparison
  # (for a service with one, Compose also folds that file's keys in).
  local prefix
  prefix="$(dr_compose_prefix 2>/dev/null)" || return 0
  ( cd "$ROOT" && eval "$prefix" config --hash v1-gateway 2>/dev/null ) | awk '$1 == "v1-gateway" { print $2; exit }'
}

bounded_wait() {  # bounded_wait DEADLINE POLL PROBE - 0 once PROBE prints 0 or nothing, 1 when DEADLINE passes
  local deadline="$1" poll="$2" probe="$3" start=$SECONDS elapsed left value
  while :; do
    elapsed=$((SECONDS - start))
    [ "$elapsed" -lt "$deadline" ] || return 1
    left=$((deadline - elapsed))
    sleep "$(( poll < left ? poll : left ))"
    value="$("$probe")"
    if [ -z "$value" ] || [ "$value" = 0 ]; then return 0; fi
  done
}

v1_gateway_path_report() {  # which way the frontend's /v1 route goes after this deploy, and the one misconfiguration to warn about
  # Release review 2026-09-14 (high): with a fixed V1_GATEWAY_URL the deploy
  # that shipped the gateway put all public /v1 traffic on it before the
  # orchestrator trusted its address. The relay is opt-in now; say which path
  # this deploy renders, because nothing else in the log does.
  local url peers
  url="$(dr_env_value V1_GATEWAY_URL)"
  peers="$(dr_env_value PUBLIC_API_GATEWAY_PEERS)"
  if [ -z "$url" ]; then
    say "  v1-gateway: the frontend's /v1 route goes straight to the orchestrator (V1_GATEWAY_URL is blank)"
    return 0
  fi
  say "  v1-gateway: the frontend's /v1 route relays through $url (V1_GATEWAY_URL)"
  if [ -z "$peers" ]; then
    say "  v1-gateway: WARNING PUBLIC_API_GATEWAY_PEERS is blank: the orchestrator ignores the gateway's re-attach headers,"
    say "  v1-gateway: and unless PUBLIC_API_TRUSTED_PROXIES names the gateway it sees every caller at the gateway's address"
    say "  v1-gateway: (ip_allowlist). Set both in .env (gateway/README.md, Settings the operator owns) or blank V1_GATEWAY_URL."
  fi
}

v1_gateway_guard() {  # before `up`: say what this deploy does to the gateway; wait (bounded) before a recreate only when opted in
  local want="${V1_GATEWAY_CODE_SHA:-}" running have rendered why relays image
  local deadline="${DEPLOY_GATEWAY_DRAIN_DEADLINE:-0}" poll="${DEPLOY_GATEWAY_DRAIN_POLL:-5}"
  V1_GATEWAY_ID_BEFORE="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{.Id}}' 2>/dev/null || true)"
  running="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{.State.Running}}' 2>/dev/null || true)"
  if [ -z "$want" ]; then
    if [ "$running" = true ]; then
      say "  v1-gateway: WARNING this tree has no gateway/, but $V1_GATEWAY_CONTAINER is still running and is"
      say "  v1-gateway: left alone. If the tunnel routes ^/v1 to it, delete that route now (gateway/README.md,"
      say "  v1-gateway: rollback): its orchestrator path goes away with this tree's orchestrator."
    fi
    return 0
  fi
  v1_gateway_path_report
  if [ "$running" != true ]; then
    say "  v1-gateway: not running; this deploy creates it (code sha ${want:0:12}). It takes no public traffic until the tunnel routes ^/v1 to it or V1_GATEWAY_URL points the frontend's /v1 route at it"
    return 0
  fi
  image="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || true)"
  case "$image" in
    *:unpinned)
      say "  v1-gateway: WARNING it runs $image - a hand-run \`docker compose\` without V1_GATEWAY_CODE_SHA recreated it"
      say "  v1-gateway: (export it first: scripts/deploy.sh --print-v1-gateway-sha); this deploy puts it back on the pinned image" ;;
  esac
  if [ "$FULL" = 1 ]; then
    why="--full removes every container, this one included"
  else
    have="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' 2>/dev/null || true)"
    rendered="$(v1_gateway_rendered_hash)"
    if [ -n "$rendered" ] && [ "$rendered" = "$have" ]; then
      say "  v1-gateway unchanged (code sha ${want:0:12}, definition ${have:0:12}): it keeps every /v1 connection through this deploy"
      return 0
    fi
    if [ -n "$rendered" ]; then
      why="its definition changed (${have:0:12} -> ${rendered:0:12}, code sha ${want:0:12})"
    else
      why="its rendered definition could not be read, so a change is assumed"
    fi
  fi
  relays="$(v1_gateway_relays)"
  case "$relays" in
    '') say "  v1-gateway: will be recreated - $why; it cannot report its relays, so not waiting"; return 0 ;;
    0)  say "  v1-gateway: will be recreated - $why; it is relaying nothing"; return 0 ;;
  esac
  if [ "${DEPLOY_ROLLING_BACK:-0}" = 1 ] || [ "$deadline" -le 0 ]; then
    say "  v1-gateway: will be recreated - $why - cutting $relays relay(s) now ($([ "${DEPLOY_ROLLING_BACK:-0}" = 1 ] && printf 'rolling back' || printf 'deploys never wait; DEPLOY_GATEWAY_DRAIN_DEADLINE opts a hand-run deploy in'))"
    say "  v1-gateway: they are cut 2 s after SIGTERM; their clients see an incomplete read and resume"
    return 0
  fi
  say "  v1-gateway: will be recreated - $why; waiting up to ${deadline}s for its $relays relay(s) to finish"
  if bounded_wait "$deadline" "$poll" v1_gateway_relays; then
    say "  v1-gateway: its relays finished (or it stopped reporting them); going ahead"
  else
    say "  v1-gateway: WARNING still relaying $(v1_gateway_relays) connection(s) after ${deadline}s; deploying anyway."
    say "  v1-gateway: those clients see an incomplete read and resume (SDK retries attach to the run)."
  fi
  return 0
}

public_api_psql_ro() {  # public_api_psql_ro SQL - one read-only value from the app database, or nothing
  local pg user db
  pg="$(dr_container_for postgres)"
  docker inspect "$pg" >/dev/null 2>&1 || return 0
  user="$(dr_env_value POSTGRES_USER)"; user="${user:-techsara}"
  db="$(dr_env_value POSTGRES_DB)"; db="${db:-techsara}"
  docker exec -e PGOPTIONS="-c default_transaction_read_only=on" "$pg" \
    psql -U "$user" -d "$db" -tA -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null | tr -d '[:space:]' || true
}

public_api_in_flight() {  # public_api_in_flight [SINCE] - queued + in-progress public runs (created at or after SINCE), or nothing when unreadable
  local since="${1:-}" present out filter=""
  present="$(public_api_psql_ro "SELECT to_regclass('public.api_responses') IS NOT NULL")"
  case "$present" in
    t) : ;;
    f) printf '0\n'; return 0 ;;
    *) return 0 ;;
  esac
  if [ -n "$since" ]; then
    # Docker's RFC 3339 StartedAt, checked before it goes near SQL.
    [[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$ ]] || return 0
    filter=" AND created_at >= '$since'::timestamptz"
  fi
  out="$(public_api_psql_ro "SELECT count(*) FROM api_responses WHERE status IN ('queued', 'in_progress')$filter")"
  case "$out" in
    ''|*[!0-9]*) return 0 ;;
    *) printf '%s\n' "$out" ;;
  esac
}

live_orchestrator_suspends_public_runs() {  # 0 when the RUNNING orchestrator ships the durable runtime AND has resume on
  local orchestrator enabled
  orchestrator="$(dr_container_for orchestrator)"
  docker exec "$orchestrator" test -f /app/app/publicapi/durable.py >/dev/null 2>&1 || return 1
  # The kill switch (config.py public_api_resume_enabled: unset or blank is
  # on; otherwise on only for 1/true/yes/on). Off, a suspended run FAILS, so
  # the file alone says nothing about what a restart cuts (review 2026-09-14).
  enabled="$(docker exec "$orchestrator" printenv PUBLIC_API_RESUME_ENABLED 2>/dev/null || true)"
  enabled="$(printf '%s' "$enabled" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  case "$enabled" in
    ''|1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

public_api_not_resumable_in_flight() {  # queued + in-progress runs that are not resumable (foreground or store:false), or nothing when unreadable
  local present out
  present="$(public_api_psql_ro "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'api_responses' AND column_name = 'resumable')")"
  [ "$present" = t ] || return 0
  out="$(public_api_psql_ro "SELECT count(*) FROM api_responses WHERE status IN ('queued', 'in_progress') AND resumable IS NOT TRUE")"
  case "$out" in
    ''|*[!0-9]*) return 0 ;;
    *) printf '%s\n' "$out" ;;
  esac
}

PUBLIC_WORK_SINCE=""
public_api_live_in_flight() {  # the probe the hold polls: runs the running orchestrator can still be executing
  public_api_in_flight "$PUBLIC_WORK_SINCE"
}

public_work_guard() {  # before `up`: report public /v1 work in flight; wait (bounded) only when it cannot survive and a hand-run deploy opted in
  local n cut deadline="${DEPLOY_PUBLIC_WORK_DEADLINE:-0}" poll="${DEPLOY_PUBLIC_WORK_POLL:-15}"
  # The durable runtime's own read-only report (running, suspended, queued,
  # quarantined, re-prefill tokens) when the applied tree ships it.
  if grep -q 'deploy-drain.sh api' "$ROOT/scripts/deploy-drain.sh" 2>/dev/null; then
    "$ROOT/scripts/deploy-drain.sh" api >>"$LOG" 2>&1 \
      || say "  public api: the deploy-drain api report failed (see $LOG); continuing"
  fi
  n="$(public_api_in_flight)"
  case "$n" in
    '') say "  public api: cannot read the in-flight /v1 runs (database unreachable); not waiting"; return 0 ;;
    0)  say "  public api: no queued or in-progress /v1 runs"; return 0 ;;
  esac
  local orchestrator started
  orchestrator="$(dr_container_for orchestrator)"
  if [ "$(docker inspect "$orchestrator" --format '{{.State.Running}}' 2>/dev/null || true)" != true ]; then
    say "  public api: $n queued/in-progress /v1 run(s), but the orchestrator is not running, so nothing is executing them; not waiting"
    return 0
  fi
  if live_orchestrator_suspends_public_runs; then
    say "  public api: $n queued/in-progress /v1 run(s); the running orchestrator suspends the resumable ones on SIGTERM and the next one resumes them (each resume re-prefills); not waiting"
    cut="$(public_api_not_resumable_in_flight)"
    case "$cut" in
      ''|0) : ;;
      # WHY (assembler, 2026-09-14): "(store:false)" alone was wrong for this
      # build: the router does not launch foreground generations durably yet,
      # so every sync or streaming /v1 run is non-resumable and is cut.
      *) say "  public api: $cut of them are not resumable (foreground sync/stream runs, or store:false) and will be cut: their clients get an incomplete read" ;;
    esac
    return 0
  fi
  started="$(docker inspect "$orchestrator" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
  PUBLIC_WORK_SINCE="$started"
  n="$(public_api_live_in_flight)"
  case "$n" in
    '') say "  public api: cannot tell which /v1 runs the running orchestrator started; not waiting"; return 0 ;;
    0)  say "  public api: no /v1 run started since the orchestrator did (older rows cannot be running); not waiting"; return 0 ;;
  esac
  say "  public api: $n queued/in-progress /v1 run(s) on an orchestrator that CANNOT suspend them - replacing it fails them"
  if [ "${DEPLOY_ROLLING_BACK:-0}" = 1 ]; then
    say "  public api: rolling back; not waiting"
    return 0
  fi
  if [ "$deadline" -le 0 ]; then
    say "  public api: not waiting (deploys never wait; DEPLOY_PUBLIC_WORK_DEADLINE opts a hand-run deploy in) - they end failed"
    return 0
  fi
  say "  public api: waiting up to ${deadline}s for them to finish"
  if bounded_wait "$deadline" "$poll" public_api_live_in_flight; then
    say "  public api: in-flight /v1 runs finished (or became unreadable); continuing"
  else
    say "  public api: WARNING $(public_api_live_in_flight) run(s) still in flight after ${deadline}s; deploying anyway - they end failed"
  fi
  return 0
}

v1_gateway_health() {  # after `up`: the gateway this tree renders runs its digest; healthy is required only of one this deploy (re)created
  local want="${V1_GATEWAY_CODE_SHA:-}" image health status id
  [ -n "$want" ] || return 0
  image="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || true)"
  if [ "$image" != "sf-local-ai-v1-gateway:$want" ]; then
    say "  health: v1-gateway is running '${image:-nothing}', not sf-local-ai-v1-gateway:${want:0:12}"
    return 1
  fi
  health="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
  if [ "$health" != healthy ]; then
    id="$(docker inspect "$V1_GATEWAY_CONTAINER" --format '{{.Id}}' 2>/dev/null || true)"
    if [ -n "$id" ] && [ "$id" = "$V1_GATEWAY_ID_BEFORE" ]; then
      # WHY NOT A FAILURE (2026-09-14, review finding): this deploy did not
      # touch the container, so failing it rolls back an unrelated change,
      # and the rollback -- same gateway/, same untouched container -- fails
      # the same way. A gateway this deploy created or recreated still gates.
      say "  health: WARNING v1-gateway is '${health:-of unknown health}' and this deploy did not change it; not failing the deploy for it."
      say "  health: inspect \`docker logs --tail 200 $V1_GATEWAY_CONTAINER\`; \`docker restart $V1_GATEWAY_CONTAINER\` cuts every /v1 connection it holds (clients resume)"
      return 0
    fi
    say "  health: v1-gateway is '${health:-not running}', not healthy"
    return 1
  fi
  # Advisory, not a gate: through the gateway to the orchestrator. Any answer
  # the orchestrator itself gives (401 without a key) proves the relay path;
  # 502/503/504 or none means the gateway cannot reach it -- which matters
  # only once the tunnel routes ^/v1 there, and must not roll back a deploy.
  status="$(docker exec "$V1_GATEWAY_CONTAINER" timeout 20 wget -S -q -O /dev/null http://127.0.0.1:8090/v1/models 2>&1 \
    | awk '$1 ~ /^HTTP\// { print $2; exit }' || true)"
  case "$status" in
    ''|502|503|504)
      say "  health: WARNING v1-gateway could not reach the orchestrator (status ${status:-none}); keep ^/v1 off the tunnel until it can (gateway/README.md)" ;;
    *)
      say "  health: v1-gateway ok (code sha ${want:0:12}; /v1/models relayed, the orchestrator answered $status)" ;;
  esac
  return 0
}

# --------------------------------------------------------------------- deploy
MANIFEST=""     # set by apply(): the release manifest the digest gate checks against
RECORD_DIR=""   # set by apply(): .runtime/releases/<stamp>/ for the pre-deploy record
RECORD_JSON=""  # set by apply(): that directory's record.json
apply() {  # apply <sha> - move the checkout and bring the stack up
  local sha="$1" record svc rc
  local -a record_args
  land "$sha" || return 1
  # Before the first render below (deploy-record.sh): every compose call in
  # this deploy names the gateway image of the tree just checked out.
  pin_v1_gateway "$ROOT"

  # (1) RECORD BEFORE REPLACING. Image ids, rendered configuration, applied
  #     migrations, PostgreSQL major. Every one of those stops being readable
  #     the moment the containers are recreated, and every one of them is
  #     needed to roll back to what was there a minute ago.
  record_args=(--note "pre-deploy state, target $sha")
  # The verdict computed at the forward gate, while /health still answered.
  # Passed as an ARGUMENT rather than re-derived inside the record script, so
  # there is exactly one reading and it is the healthy one.
  if [ -n "$REVERSIBILITY_VERDICT" ]; then
    record_args+=(--reversibility "$REVERSIBILITY_VERDICT")
  fi
  record="$("$ROOT/scripts/deploy-record.sh" "${record_args[@]}" 2>>"$LOG" | tail -1)" || record=""
  if [ -n "$record" ] && [ -f "$record" ]; then
    RECORD_JSON="$record"
    RECORD_DIR="$(dirname "$record")"
    say "  record: what is running now is captured in $record"
    # The sub-script's own narration goes into ITS log, because this shell
    # consumed its stdout to learn the path. Fold it back in so the deploy log
    # is still the single file a recovery has to read.
    cat "$(dirname "$record")/record.log" >>"$LOG" 2>/dev/null || true
  else
    say "  record: WARNING - could not capture the pre-deploy state (see $LOG)"
  fi

  # (2) BUILD ONCE, AND WRITE DOWN WHAT WAS BUILT. Before `techsara down`, so a
  #     build failure leaves the stack up rather than down with nothing to
  #     start. The ids in this manifest are what the gate after `techsara up`
  #     insists the containers were actually created from.
  MANIFEST="$("$ROOT/scripts/deploy-preflight.sh" build 2>>"$LOG" | tail -1)" || {
    say "  preflight: the image build FAILED; nothing was recreated"; return 1; }
  [ -f "$MANIFEST" ] || { say "  preflight: no manifest was written; refusing to deploy unverifiable images"; return 1; }
  cat "$(dirname "$MANIFEST")/preflight.log" >>"$LOG" 2>/dev/null || true
  say "  preflight: promoted $MANIFEST"
  # The ids, in the deploy log, in plain sight. This is the "digest flows from
  # build to deploy" hand-off made legible to whoever reads this file later.
  # A heredoc, not `python3 -c '...'`: a nested quote inside an f-string
  # replacement field is a syntax error before Python 3.12, and this listing is
  # decoration - it must never be the reason a deploy aborts, hence `|| true`.
  python3 - "$MANIFEST" <<'PY' | tee -a "$LOG" || true
import json, sys
manifest = json.load(open(sys.argv[1]))
for service, meta in sorted(manifest["images"].items()):
    print("    %-14s %s" % (service, meta["id"]))
PY

  # Public /v1 work and the gateway: say what this deploy cuts. Waiting is
  # off unless a hand-run deploy opts in (deploys never wait). Before the
  # engine lock.
  public_work_guard
  v1_gateway_guard

  # ONE ACTOR RESTARTS THE ENGINE PAIR AT A TIME. --full restarts it on
  # purpose; a routine deploy restarts it too whenever the launcher finds the
  # engine "not serving" (the preserve flag is ignored for a corpse) -- which
  # is exactly the moment the engine controller is restarting it as well.
  # Waiting here (default 20 min) rides out a recovery in progress; the lock
  # is released as soon as `techsara up` returns so the health gate below
  # never runs under it.
  engine_lock_acquire "${ENGINE_LOCK_WAIT:-1200}" "deploy.sh apply $sha$([ "$FULL" = 1 ] && printf ' --full')" \
    || { say "  engine lock: held by another actor (a recovery in progress?); not restarting anything"; return 1; }
  if [ "$FULL" = 1 ]; then
    # Every container goes, models included. `down` never passes -v, so the
    # database, warehouse, vector index and reports all survive; what is paid
    # for is time, not data: the main model reloads (~6-10 min) and the auxiliary
    # model servers restart behind it.
    say "  full restart: techsara down, then up (volumes preserved; expect ~10-15 min)"
    ( cd "$ROOT" && ./techsara down ) >>"$LOG" 2>&1 || { engine_lock_release; return 1; }
  fi
  # A routine deploy NEVER restarts the main model: reloading it is 15-25
  # minutes of the site answering nothing. The launcher probes the running
  # engine and leaves it alone; a definition change to vllm therefore waits
  # for --full, which is the operator asking for the reload out loud.
  # (3) DRAIN. There is no second stack to move traffic to, so draining here is
  #     two honest things: report the grace period each recreated service will
  #     actually get, and put the SIGTERM in a quiet moment rather than in the
  #     middle of a burst. Neither can block the deploy - a service that never
  #     goes quiet must not be able to hold production on a bad build.
  "$ROOT/scripts/deploy-drain.sh" check >>"$LOG" 2>&1 \
    || say "  drain: a service being recreated is on Docker's 10s default (see $LOG)"
  #     THE OUTCOME IS REPORTED, NOT SWALLOWED. This loop used to end in
  #     `|| true`, which flattened three different answers into silence:
  #       0  the service went quiet and the SIGTERM lands in a gap;
  #       2  the deadline passed while it was still busy - advisory, the
  #          stop_grace_period is what protects those requests now;
  #       1  deploy-drain.sh could not RUN the check at all.
  #     The third one had been firing on EVERY deploy since sync-worker was
  #     added to the list ("cannot determine sync-worker's listening port from
  #     the rendered config; pass --port"), and `|| true` ate it. That is
  #     tolerable for the sync worker and would have been invisible for the
  #     ORCHESTRATOR, which is the one service whose drain actually matters.
  #     A drain that stops working must say so.
  for svc in orchestrator frontend; do
    rc=0
    "$ROOT/scripts/deploy-drain.sh" wait "$svc" \
      --deadline "${DEPLOY_DRAIN_DEADLINE:-90}" --quiet-for 5 >>"$LOG" 2>&1 || rc=$?
    case "$rc" in
      0) say "  drain: $svc drained" ;;
      2) say "  drain: WARNING $svc was still busy when the ${DEPLOY_DRAIN_DEADLINE:-90}s deadline passed; recreating anyway - its stop_grace_period is what protects those requests now" ;;
      *) say "  drain: WARNING could not drain $svc - deploy-drain.sh exited $rc (see $LOG). The SIGTERM will land wherever it lands." ;;
    esac
  done
  #     The sync worker is NOT a listener: it publishes no port and opens only
  #     outbound Salesforce connections, so `wait` has no listening port to
  #     count established connections against and dies with exit 1. It is not
  #     drained, and that is correct rather than a gap: its 10m
  #     stop_grace_period (compose.yaml) is what lets a sync in flight finish,
  #     and `deploy-drain.sh check` above is what proves that period is still
  #     configured. Saying this in one line beats an error nobody reads.
  say "  drain: sync-worker publishes no port, so there is no connection drain for it; its 10m stop_grace_period finishes a sync in flight"
  #     LAST, closest to the SIGTERM: chunked uploads being finalised. A
  #     session killed mid-finalise sits `finalizing` until the next process's
  #     startup hook returns it to `uploading`, and the person is watching a
  #     spinner for the whole gap. It is bounded and advisory like everything
  #     above - exit 2 means it gave up waiting, not that the deploy is unsafe.
  "$ROOT/scripts/deploy-drain.sh" uploads \
    --deadline "${DEPLOY_FINALIZE_DEADLINE:-90}" >>"$LOG" 2>&1 \
    || say "  drain: an upload was still finalising when the deadline passed (see $LOG)"

  PRESERVE=1; [ "$FULL" = 1 ] && PRESERVE=
  say "  techsara up  (builds images, recreates changed services, staged health gates$([ -n "$PRESERVE" ] && printf '; main model preserved'))"
  ( cd "$ROOT" && TECHSARA_PRESERVE_MAIN_MODEL="$PRESERVE" ./techsara up ) >>"$LOG" 2>&1 || { engine_lock_release; return 1; }
  # Still under the lock: an orphaned guard removed here cannot be mid-recovery.
  reconcile_engine_guards
  engine_lock_release

  # (4) THE DIGEST GATE. `techsara up` builds again into a warm cache and must
  #     therefore land on the same ids. If it did not - a base image moved, an
  #     apt or pip resolution changed, someone re-pointed a tag mid-deploy -
  #     then what is now serving is NOT what was built and recorded above, and
  #     that is a failure, not a curiosity.
  if ! "$ROOT/scripts/deploy-preflight.sh" verify "$MANIFEST" >>"$LOG" 2>&1; then
    say "  digest gate FAILED: the running containers are not the promoted images"
    grep -E 'MISMATCH|manifest promoted|container running' "$LOG" | tail -6 | sed 's/^/    /' | tee -a "$LOG"
    return 1
  fi
  say "  digest gate: every application container is running its promoted image id"

  # (5) WHAT THIS APPLY PRODUCED, for a caller that must not guess. Both paths
  #     already exist under .runtime/releases/<stamp>/ and both are already
  #     printed into this log, but a CALLER that had to recover them would have
  #     to glob that directory - and a hand-run deploy running concurrently
  #     would make a glob pick the wrong release. So they are handed over
  #     explicitly. Written LAST and truncating, so the file always describes
  #     the apply that just completed, including a rollback's.
  #
  #     Nothing in this script knows about GitHub Actions; the caller points
  #     DEPLOY_RESULT_FILE wherever it likes and reads `key=value` back.
  if [ -n "${DEPLOY_RESULT_FILE:-}" ]; then
    if {
      printf 'sha=%s\n' "$sha"
      printf 'manifest=%s\n' "$MANIFEST"
      printf 'record=%s\n' "$RECORD_JSON"
      printf 'release_dir=%s\n' "$RECORD_DIR"
      printf 'reversibility=%s\n' "$REVERSIBILITY_VERDICT"
    } >"$DEPLOY_RESULT_FILE" 2>>"$LOG"; then
      say "  result: paths for this apply written to $DEPLOY_RESULT_FILE"
    else
      say "  result: WARNING could not write $DEPLOY_RESULT_FILE (see $LOG)"
    fi
  fi
}

# ---------------------------------------------------------------- health gate
health() {
  local orch front port
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

  v1_gateway_health || return 1

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
# The automatic rollback is the one that runs at three in the morning with
# nobody watching, so it gets the strictest gate in this file. Between `apply`
# and this line the new code may have applied migrations; rolling the image
# back does not un-apply them, and an automatic rollback that quietly starts
# older code on a newer schema turns one broken deploy into a broken deploy
# plus a database nobody can reason about.
#
# THIS USED TO FAIL OPEN. It called schema_gate "$PREVIOUS", and that function
# returns 0 without checking anything when the live schema version cannot be
# read - which is the NORMAL state here, because the only way to reach this
# line is that the health gate failed or `techsara up` failed, i.e. /health is
# down and the compose project may be mid-recreate, so both the /health read
# and the `docker exec <pg> psql` fallback can fail together. The gate returned
# 0, `apply "$PREVIOUS"` ran, and V40 code started on a V41 database.
#
# Now: the verdict was computed at the forward gate while the stack was still
# healthy, written into the release record, and only CONSULTED here. Every path
# that is not a proof of safety refuses:
#   * no recorded verdict (or an unparseable one)  -> REFUSE
#   * the live schema version unreadable right now -> REFUSE
#   * the verdict said forward-only                -> REFUSE
#   * the database moved past PREVIOUS since       -> REFUSE
# There is no branch left that proceeds on an unknown.
ROLLBACK_VERDICT="$REVERSIBILITY_VERDICT"
ROLLBACK_VERDICT_SOURCE="this run's forward gate (nothing was recorded)"
if [ -n "$RECORD_DIR" ] && [ -f "$RECORD_DIR/reversibility" ]; then
  ROLLBACK_VERDICT="$(cat "$RECORD_DIR/reversibility" 2>/dev/null || true)"
  ROLLBACK_VERDICT_SOURCE="$RECORD_DIR/reversibility"
fi
ROLLBACK_LIVE="$(dr_live_schema_version 2>/dev/null || true)"
ROLLBACK_OK=1
ROLLBACK_DECISION="$(dr_rollback_is_reversible "$ROLLBACK_VERDICT" "$ROLLBACK_LIVE")" || ROLLBACK_OK=0
ROLLBACK_REASON="${ROLLBACK_DECISION#*reason=}"; ROLLBACK_REASON="${ROLLBACK_REASON%% *}"
say "  rollback gate: verdict source: $ROLLBACK_VERDICT_SOURCE"
say "  rollback gate: verdict: ${ROLLBACK_VERDICT:-<none recorded>}"
say "  rollback gate: live schema right now: ${ROLLBACK_LIVE:-UNREADABLE}"
say "  rollback gate: decision: $ROLLBACK_DECISION"
# The deliberate, hand-set override keeps working for the ONE case it was
# written for: a KNOWN downgrade the operator has read about and accepted. It
# does not apply to "the verdict is missing" or "the database cannot be read",
# because there is nothing there to have accepted.
if [ "$ROLLBACK_OK" != 1 ] && [ "${ALLOW_SCHEMA_DOWNGRADE:-0}" = 1 ]; then
  case "$ROLLBACK_REASON" in
    forward-only | database-moved-past-previous)
      say "  rollback gate: ALLOW_SCHEMA_DOWNGRADE=1 given for a KNOWN downgrade; continuing anyway."
      ROLLBACK_OK=1 ;;
  esac
fi
if [ "$ROLLBACK_OK" != 1 ]; then
  say ""
  say "NOT ROLLING BACK. The deploy of $TARGET failed, and this run will not start"
  say "the previous commit $PREVIOUS automatically."
  case "$ROLLBACK_REASON" in
    live-schema-unreadable)
      say "Why: the live schema version cannot be read RIGHT NOW - the orchestrator is"
      say "down and psql did not answer - so nothing here can prove the rollback is safe."
      say "This is the case that used to fall through as 'proceeding WITHOUT the"
      say "compatibility check', which is how older code got started on a newer schema." ;;
    no-recorded-verdict | unparseable-verdict)
      say "Why: no usable reversibility verdict was recorded for this deploy, so what"
      say "the database will have applied by now is unknown. An unknown is a refusal." ;;
    *)
      say "Why: $PREVIOUS cannot be started safely because the database has moved past it." ;;
  esac
  say "The stack is left exactly as the health gate found it, running $TARGET."
  say ""
  say "This is deliberate. The alternatives are worse:"
  say "  * starting $PREVIOUS anyway would run code against a schema it does not know;"
  say "  * restoring a pre-deploy database dump would delete every conversation,"
  say "    message and upload written since the deploy started."
  say "Roll FORWARD: fix the defect and deploy a commit that knows the current schema."
  say "To override, having read the above: ALLOW_SCHEMA_DOWNGRADE=1 $0 --ref $PREVIOUS"
  say "(That override applies to a KNOWN downgrade. It does not, and will not, turn"
  say " 'the database cannot be read' into a reason to start older code on it.)"
  die "deploy of $TARGET failed and an automatic rollback could not be proved safe\
 ($ROLLBACK_REASON). Nothing was rolled back. This box needs a human."
fi

say "ROLLING BACK to $PREVIOUS"
# The public-work and gateway guards report during a rollback but never wait:
# the stack is failing its health gate, and restoring it comes first.
DEPLOY_ROLLING_BACK=1
if [ -n "$FF_BRANCH" ]; then
  # This run fast-forwarded $FF_BRANCH to a commit that then failed the health
  # gate. Undo exactly that move and nothing else, with a compare-and-swap:
  # `update-ref <ref> <new> <old>` writes only if the branch STILL holds the
  # value this run put there, and the value it restores is the one this run
  # read before touching anything. Every commit the branch held before this
  # deploy started is still on it afterwards, and every commit dropped from it
  # is one this same run added and is still reachable from $TARGET and origin.
  # That is an undo of our own fast-forward, not a rewind of anybody's work -
  # which is why it is the single `update-ref` in this file.
  if git -C "$ROOT" update-ref -m "deploy rollback: undo this deploy's fast-forward" \
       "refs/heads/$FF_BRANCH" "$FF_FROM" "$FF_TO" 2>>"$LOG"; then
    say "  branch: undid this deploy's own fast-forward of $FF_BRANCH"
    say "  branch: ($FF_TO -> $FF_FROM, compare-and-swap; nothing it held is gone)"
  else
    say "  branch: NOT undoing the fast-forward of $FF_BRANCH - it no longer points"
    say "  branch: at $FF_TO, so something else moved it and this is not ours to undo."
  fi
elif [ -n "$DEPLOY_BRANCH" ]; then
  say "  note: this deploy never moved $DEPLOY_BRANCH, so there is nothing to undo."
fi
if apply "$PREVIOUS" && health; then
  die "deploy of $TARGET failed; rolled back to $PREVIOUS and the stack is healthy\
 again. The checkout is on $LANDED_ON"
fi
die "deploy of $TARGET failed AND the rollback to $PREVIOUS did not come up healthy - this box needs a human"
