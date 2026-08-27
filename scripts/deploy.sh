#!/usr/bin/env bash
# Deploy a commit to THIS machine: fast-forward the production checkout, rebuild
# the application images, recreate the containers whose definition changed, and
# refuse to call it a success until the stack actually answers.
#
#   scripts/deploy.sh [--ref origin/main] [--full] [--dry-run] [--no-rollback]
#
#   --full          `techsara down` first, so EVERY container is recreated.
#                   Costs 6-10 extra minutes because the 27B model reloads.
#                   Without it the launcher recreates only what changed, which
#                   for an app-only change leaves the model containers running.
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
#   * On a failed health gate it rolls back to the commit that was live before.
set -euo pipefail

ROOT="${TECHSARA_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REF="${DEPLOY_REF:-origin/main}"
FULL=0; DRY=0; ROLLBACK=1
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --full) FULL=1 ;;
    --dry-run) DRY=1 ;;
    --no-rollback) ROLLBACK=0 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
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

say "deploy start  root=$ROOT ref=$REF full=$FULL log=$LOG"

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
TARGET="$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")" || die "cannot resolve $REF"
PREVIOUS="$(git -C "$ROOT" rev-parse HEAD)"
say "current=$PREVIOUS"
say "target =$TARGET  $(git -C "$ROOT" log -1 --pretty='%s' "$TARGET" | cut -c1-72)"

if [ "$TARGET" = "$PREVIOUS" ]; then
  say "already at the target commit; still reconciling containers so a manual "
  say "docker change cannot leave the box drifted from the repo"
fi
if [ "$DRY" = 1 ]; then say "dry run: nothing changed"; exit 0; fi

# --------------------------------------------------------------------- deploy
apply() {  # apply <sha> - move the checkout and bring the stack up
  local sha="$1"
  # Detach rather than reset: the production checkout may be sitting on a
  # branch (it was on "dev"), and `reset --hard` would MOVE that branch to the
  # deployed commit, silently rewriting the developer's own branch pointer.
  # Detaching moves only HEAD, so branches are left exactly where they were.
  git -C "$ROOT" checkout --detach --force --quiet "$sha" || return 1
  git -C "$ROOT" clean -qfd -e .env -e .runtime || true
  if [ "$FULL" = 1 ]; then
    say "  full restart requested: techsara down (volumes are preserved)"
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
    say "DEPLOYED $TARGET"
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
if apply "$PREVIOUS" && health; then
  die "deploy of $TARGET failed; rolled back to $PREVIOUS and the stack is healthy again"
fi
die "deploy of $TARGET failed AND the rollback to $PREVIOUS did not come up healthy - this box needs a human"
