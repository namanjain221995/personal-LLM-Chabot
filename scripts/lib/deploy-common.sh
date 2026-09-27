#!/usr/bin/env bash
# Shared plumbing for the deployment-recoverability scripts.
#
#   . "$(dirname "$0")/lib/deploy-common.sh"
#
# Nothing here starts, stops or recreates a container. Everything is either a
# read, a lock, or a computation. The scripts that source this decide when to
# act; this file only makes sure they act on FACTS rather than on assumptions.
#
# The three facts this file exists to protect:
#
#   1. THE COMPOSE CHAIN. `sf-local-ai` is rendered from FOUR files in order —
#      compose.yaml, compose/compose.dgx-spark.yaml,
#      compose/compose.published-dgx-spark.yaml,
#      compose/compose.cluster-dgx-spark.yaml — plus THREE --env-file layers.
#      Run a SUBSET and the orchestrator silently resolves to
#      `sf-local-ai-orchestrator:cpu`: same project, same service name, a
#      completely different (and stale) image. That has bitten this repo
#      before. So the chain is never typed here — it is read back from
#      .runtime/state.json, which is what the launcher itself used, and then
#      CHECKED against the required set.
#
#   2. THE IMAGE IDENTITY. The three application images are built locally and
#      never pushed, so they have no RepoDigest. Their immutable identity is
#      the image ID (`docker image inspect --format '{{.Id}}'`), which is the
#      sha256 of the image config and is exactly as content-addressed as a
#      registry digest. A TAG is a pointer and can be moved; the ID cannot.
#      Everything downstream compares IDs.
#
#   3. THE SCHEMA BOUNDARY. Migrations in orchestrator/app/db.py only go
#      forward — there are no down migrations and `init_schema` skips versions
#      already present in `schema_migrations`. Rolling an image back therefore
#      does NOT roll the database back, and a rollback across a migration
#      boundary leaves old code in front of a newer schema. This file can read
#      both numbers so a caller can refuse instead of guess.
set -o pipefail

# --------------------------------------------------------------------- basics
DR_ROOT="${TECHSARA_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DR_PROJECT="${TECHSARA_COMPOSE_PROJECT:-sf-local-ai}"

#: The application images. Everything else in the chain is already pinned to a
#: registry digest by the compose files themselves, so these three are the only
#: mutable tags a deploy can get wrong. (Read by the scripts that source this
#: file -- deploy-preflight.sh, deploy-rollback.sh -- hence the SC2034 waiver.)
# shellcheck disable=SC2034
DR_APP_SERVICES=(orchestrator sync-worker frontend)

#: The compose files that MUST all be present in the chain, in this order.
# shellcheck disable=SC2034
DR_REQUIRED_COMPOSE_FILES=(
  compose.yaml
  compose/compose.dgx-spark.yaml
  compose/compose.published-dgx-spark.yaml
  compose/compose.cluster-dgx-spark.yaml
)

dr_now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
dr_stamp(){ date -u +%Y%m%d-%H%M%SZ; }

DR_LOG="${DR_LOG:-}"
# To the log as well when one is set; plain stdout otherwise.
_dr_out() { if [ -n "$DR_LOG" ]; then tee -a "$DR_LOG"; else cat; fi; }
dr_say()  { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | _dr_out; }
dr_warn() { printf '%s WARN  %s\n' "$(date -u +%H:%M:%S)" "$*" | _dr_out >&2; }
dr_die()  { printf '%s ERROR %s\n' "$(date -u +%H:%M:%S)" "$*" | _dr_out >&2; exit 1; }

dr_need() { command -v "$1" >/dev/null 2>&1 || dr_die "$1 is not on PATH"; }

# ------------------------------------------------------------ the compose chain
# Returns the compose command PREFIX (binary, project name, every --env-file,
# every -f, every --profile) exactly as the launcher last used it, with the
# trailing subcommand stripped. Callers append their own verb.
#
# Refuses to return a chain that is missing any of DR_REQUIRED_COMPOSE_FILES:
# a subset is not "a smaller deploy", it is a DIFFERENT deploy that resolves
# the orchestrator to the stale :cpu image.
dr_compose_prefix() {
  local state="$DR_ROOT/.runtime/state.json"
  [ -f "$state" ] || dr_die "no $state - the launcher has never run here, so the compose chain is unknown. Refusing to guess it."
  local out
  out="$(DR_ROOT="$DR_ROOT" python3 - "$state" <<'PY'
import json, os, shlex, sys

root = os.environ["DR_ROOT"]
required = [
    "compose.yaml",
    "compose/compose.dgx-spark.yaml",
    "compose/compose.published-dgx-spark.yaml",
    "compose/compose.cluster-dgx-spark.yaml",
]
try:
    state = json.load(open(sys.argv[1]))
except Exception as exc:            # noqa: BLE001 - the message is the point
    sys.exit(f"state.json is unreadable: {exc}")

cmd = state.get("compose_command")
if not isinstance(cmd, list) or len(cmd) < 4:
    sys.exit("state.json has no usable compose_command")

# Keep the flag/value pairs that select the project, the env files, the compose
# files and the profiles; stop at the first bare word, which is the subcommand.
PAIRED = {
    "--project-name", "-p", "--env-file", "-f", "--file",
    "--profile", "--project-directory",
}
prefix, i = [], 0
while i < len(cmd):
    token = cmd[i]
    if i < 2 and token in ("docker", "compose"):
        prefix.append(token); i += 1; continue
    if token in PAIRED and i + 1 < len(cmd):
        prefix += [token, cmd[i + 1]]; i += 2; continue
    break
if len(prefix) < 2:
    sys.exit("compose_command does not start with a docker compose invocation")

files = [prefix[i + 1] for i, t in enumerate(prefix) if t in ("-f", "--file")]
rel = [os.path.relpath(os.path.realpath(f), root) for f in files]
missing = [f for f in required if f not in rel]
if missing:
    sys.exit(
        "the recorded compose chain is a SUBSET of the required one.\n"
        "  recorded: " + ", ".join(rel) + "\n"
        "  missing : " + ", ".join(missing) + "\n"
        "  A subset silently resolves orchestrator to sf-local-ai-orchestrator:cpu.\n"
        "  Re-run `./techsara up` so state.json records the full chain, then retry."
    )
if rel[: len(required)] != required:
    sys.exit(
        "the recorded compose chain has the required files in the WRONG ORDER.\n"
        "  recorded: " + ", ".join(rel) + "\n"
        "  required: " + ", ".join(required) + "  (later files override earlier ones)"
    )
for path in files:
    if not os.path.isfile(path):
        sys.exit(f"compose file recorded in state.json no longer exists: {path}")

print(shlex.join(prefix))
PY
  )" || dr_die "cannot establish the compose chain:
$out"
  printf '%s\n' "$out"
}

# ------------------------------------------------------------------- the lock
# ONE deploy at a time, across every entry point that bothers to take it.
#
# The GitHub Actions `concurrency:` group only serialises WORKFLOWS. It does
# nothing about a human at a terminal, and the human is the likelier of the two
# to be surprised. This lock is a real file lock on the production checkout, so
# the workflow-triggered deploy and the hand-run script contend for the same
# object and one of them waits.
#
#   dr_lock_acquire <timeout-seconds> <purpose>
#
# Waits rather than failing instantly: a deploy that is 40 seconds from
# finishing should be waited out, not turned into an error. Requirement 4 says
# never cancel a rollout mid-flight, and the way to honour that from the
# OUTSIDE is to queue behind it.
DR_LOCK_FD=""
DR_LOCK_HOLDER_FILE=""
dr_lock_acquire() {
  local timeout="${1:-900}" purpose="${2:-deploy}"

  # Reentrancy. deploy.sh takes the lock and then runs deploy-preflight.sh,
  # which would take it again from a NEW file descriptor and block against its
  # own parent until the timeout - a deadlock that looks exactly like a stuck
  # deploy. The holder exports its pid; a descendant that sees it skips.
  # Deliberately narrow: it only matches THIS pid tree, so an exported value
  # left over in an unrelated shell cannot silently disable the lock for a
  # deploy that is genuinely concurrent.
  if [ -n "${DEPLOY_LOCK_HELD_BY:-}" ] && kill -0 "$DEPLOY_LOCK_HELD_BY" 2>/dev/null; then
    dr_say "lock: already held by pid $DEPLOY_LOCK_HELD_BY (this process is its child); not re-locking for '$purpose'"
    return 0
  fi

  local dir="$DR_ROOT/.runtime/locks"
  mkdir -p "$dir"
  local lock="$dir/deploy.lock"
  DR_LOCK_HOLDER_FILE="$dir/deploy.holder"

  exec {DR_LOCK_FD}>>"$lock" || dr_die "cannot open $lock"
  if ! flock -w "$timeout" "$DR_LOCK_FD"; then
    dr_warn "the deploy lock is held and did not free within ${timeout}s."
    if [ -s "$DR_LOCK_HOLDER_FILE" ]; then
      dr_warn "current holder:"
      sed 's/^/    /' "$DR_LOCK_HOLDER_FILE" >&2
    else
      dr_warn "the holder left no metadata (an older deploy.sh, or a crash)."
    fi
    dr_die "refusing to run '$purpose' concurrently with another deploy"
  fi

  # Written INSIDE the lock, so it always describes the process that holds it.
  {
    printf 'purpose=%s\n' "$purpose"
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$(hostname 2>/dev/null || echo '?')"
    printf 'actor=%s\n' "${GITHUB_ACTOR:-${SUDO_USER:-${USER:-unknown}}}"
    printf 'origin=%s\n' "${GITHUB_RUN_ID:+github-actions run ${GITHUB_RUN_ID}}${GITHUB_RUN_ID:-manual shell}"
    printf 'started_at=%s\n' "$(dr_now)"
    printf 'head=%s\n' "$(git -C "$DR_ROOT" rev-parse HEAD 2>/dev/null || echo '?')"
  } >"$DR_LOCK_HOLDER_FILE"

  export DEPLOY_LOCK_HELD_BY=$$
  trap dr_lock_release EXIT INT TERM
}

dr_lock_release() {
  [ "${DEPLOY_LOCK_HELD_BY:-}" = "$$" ] || return 0
  unset DEPLOY_LOCK_HELD_BY
  if [ -n "$DR_LOCK_HOLDER_FILE" ]; then : >"$DR_LOCK_HOLDER_FILE" 2>/dev/null || true; fi
  if [ -n "$DR_LOCK_FD" ]; then eval "exec ${DR_LOCK_FD}>&-" 2>/dev/null || true; fi
  DR_LOCK_FD=""
}

# True if the lock is currently held by someone else. Read-only, never blocks.
dr_lock_is_held() {
  local lock="$DR_ROOT/.runtime/locks/deploy.lock"
  [ -e "$lock" ] || return 1
  ( exec {fd}>>"$lock"; flock -n "$fd" ) && return 1 || return 0
}

# ------------------------------------------------------------------- images
# The immutable id of an image reference, or empty if it does not exist here.
dr_image_id() { docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || true; }

# The immutable id of the image a RUNNING container was created from. This is
# the only honest answer to "what is actually serving": the container keeps the
# id it started with even after someone moves the tag out from under it.
dr_container_image_id() { docker inspect "$1" --format '{{.Image}}' 2>/dev/null || true; }

# The tag the container was ASKED for, which may now point somewhere else.
dr_container_image_ref() { docker inspect "$1" --format '{{.Config.Image}}' 2>/dev/null || true; }

dr_container_for() { printf '%s-%s-1' "$DR_PROJECT" "$1"; }

# Every container Docker considers part of this project, whichever compose
# invocation created it. The monitoring stack is brought up by a SEPARATE
# compose call (scripts/monitoring.sh) but carries the same project label, so
# listing by label is the only way to record the whole box.
dr_project_containers() {
  docker ps -a --filter "label=com.docker.compose.project=$DR_PROJECT" \
    --format '{{.Names}}' 2>/dev/null | sort
}

# --------------------------------------------------------------- schema facts
# What the LIVE database has actually had applied. /health reports it without
# opening a connection of our own; the database is the fallback when the
# orchestrator is the thing that is down (which is exactly when a rollback is
# being considered, so the fallback is not optional).
dr_live_schema_version() {
  local port payload version
  port="$(dr_env_value ORCHESTRATOR_PORT)"; port="${port:-8080}"
  payload="$(curl -fsS -m 10 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  if [ -n "$payload" ]; then
    version="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
v = (d.get("checks") or {}).get("app_db", {}).get("schema_version")
print(v if isinstance(v, int) else "")' 2>/dev/null)"
    [ -n "$version" ] && { printf '%s\n' "$version"; return 0; }
  fi
  # Fallback: ask PostgreSQL directly, inside its own container, as the
  # configured application user. Read-only.
  local pg user db
  pg="$(dr_container_for postgres)"
  user="$(dr_env_value POSTGRES_USER)"; user="${user:-techsara}"
  db="$(dr_env_value POSTGRES_DB)"; db="${db:-techsara}"
  version="$(docker exec "$pg" psql -U "$user" -d "$db" -tAc \
      'SELECT COALESCE(MAX(version), 0) FROM schema_migrations' 2>/dev/null | tr -d '[:space:]')"
  [ -n "$version" ] && { printf '%s\n' "$version"; return 0; }
  return 1
}

# The highest migration the CODE IN AN IMAGE knows how to apply.
#
# Read by COPYING db.py out of the image and parsing the migration table — the
# image is never executed. Executing it would need the app's environment, and
# "the rollback candidate will not boot" must not be indistinguishable from
# "the rollback candidate has a lower schema version".
dr_code_schema_version_from_image() {
  local image="$1" cid file version
  cid="$(docker create "$image" true 2>/dev/null)" || return 1
  file="$(mktemp)"
  if docker cp "$cid:/app/app/db.py" "$file" >/dev/null 2>&1; then
    version="$(dr_parse_schema_version "$file")"
  fi
  docker rm -f "$cid" >/dev/null 2>&1 || true
  rm -f "$file"
  [ -n "${version:-}" ] || return 1
  printf '%s\n' "$version"
}

# Same number, read out of a git commit without touching the working tree.
dr_code_schema_version_from_git() {
  local ref="$1" file version
  file="$(mktemp)"
  git -C "$DR_ROOT" show "${ref}:orchestrator/app/db.py" >"$file" 2>/dev/null || { rm -f "$file"; return 1; }
  version="$(dr_parse_schema_version "$file")"
  rm -f "$file"
  [ -n "$version" ] || return 1
  printf '%s\n' "$version"
}

# The _MIGRATIONS table is the authority, not LATEST_SCHEMA_VERSION: that name
# is computed from the table, so parsing the table cannot disagree with the
# running code, and it keeps working if the constant is ever renamed.
dr_parse_schema_version() {
  python3 - "$1" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
versions = [int(m) for m in re.findall(r"^\s*\((\d+),\s*_MIGRATION_V\d+\),", src, re.M)]
print(max(versions) if versions else "")
PY
}

# ------------------------------------------------ the reversibility verdict
# "Can this deploy be undone?" asked ONCE, while the answer is still knowable,
# and then consulted later instead of re-derived.
#
# WHY THIS EXISTS. deploy.sh's schema_gate() answers "may this code start on
# this database" by reading the LIVE schema version. That read goes to /health
# first and to `docker exec <pg> psql` second, and when it cannot be made the
# gate says "proceeding WITHOUT the compatibility check" and returns 0. On the
# FORWARD path that is a considered trade. On the ROLLBACK path it was a
# fail-open at exactly the wrong moment: the rollback is consulted only after
# the health gate failed or `techsara up` failed, so the /health read is down by
# definition and the whole answer rests on the `docker exec <pg> psql` fallback.
# That fallback usually answers - a rolling deploy recreates the application
# containers and leaves postgres alone - and does not when the database
# container was recreated too (a --full deploy), when the compose project is
# mid-recreate, or when Docker itself is unwell. On that path the gate returned
# 0 and older code was started on a newer schema, unattended: the outcome the
# rollback gate exists to prevent. Uncommon, and not acceptable for a step
# nobody is watching.
#
# The fix is not a better read at rollback time. There is no better read at
# rollback time; the box is broken by definition. The fix is to take the
# reading BEFORE anything is touched, while /health still answers, write the
# verdict down, and make the rollback consult THAT - refusing when it is
# missing rather than proceeding.
#
# Everything below is a PURE FUNCTION of its arguments: no docker, no curl, no
# git, no filesystem. That is what makes the decision unit-testable
# (.github/workflows/scripts/tests/test_rollback_reversibility.py drives these
# by sourcing this file), and a decision nobody can test is a decision nobody
# should trust with an unattended rollback at 3 a.m.

#: A non-negative decimal integer and nothing else. Each argument is checked
#: SEPARATELY: concatenating them first would let an empty value hide behind a
#: neighbour's digits ("" + "41" + "40" is all digits and means nothing).
_dr_is_uint() { case "${1-}" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

#: One `key=value` token out of a verdict line. A bash loop rather than sed or
#: awk so the KEY is never interpolated into another language's pattern, and
#: with globbing off for the split so a `*` in a tampered file cannot expand
#: into a directory listing.
_dr_verdict_field() {  # _dr_verdict_field LINE KEY -> the value, or 1
  local line="${1-}" key="${2-}" token rc=1
  local -                     # restores shell options on return (bash 4.4+)
  set -f
  for token in $line; do
    case "$token" in
      "$key"=*) printf '%s\n' "${token#*=}"; rc=0; break ;;
    esac
  done
  return "$rc"
}

# The forward computation, taken while the stack is still healthy.
#
#   dr_reversibility_verdict PREVIOUS TARGET LIVE
#
# PREVIOUS  the highest migration the currently-live COMMIT's code knows
# TARGET    the highest migration the commit being deployed knows
# LIVE      the migration version the database has APPLIED, right now
#
# The database after this deploy will hold max(LIVE, TARGET): migrations only
# go forward, `init_schema` applies what is missing, and nothing removes any.
# So the rollback is honest exactly when PREVIOUS knows at least that much.
#
# Prints ONE line of `key=value` tokens and returns 0; prints nothing and
# returns 1 when any input is not a non-negative integer, which is the caller's
# signal that no verdict could be formed (and therefore that the rollback must
# refuse rather than assume).
dr_reversibility_verdict() {
  local previous="${1-}" target="${2-}" live="${3-}" after verdict range='-'
  _dr_is_uint "$previous" && _dr_is_uint "$target" && _dr_is_uint "$live" || return 1
  if [ "$target" -ge "$live" ]; then after="$target"; else after="$live"; fi
  if [ "$previous" -ge "$after" ]; then
    verdict=reversible
  else
    verdict=forward-only
    range="V$((previous + 1))..V$after"
  fi
  printf 'verdict=%s previous=%s target=%s live_at_decision=%s after=%s range=%s\n' \
    "$verdict" "$previous" "$target" "$live" "$after" "$range"
}

# The same verdict as one English sentence, derived FROM the verdict line so
# the sentence and the gate can never disagree. This is what the deploy log and
# the run summary say out loud, because "V41 > V40" is a fact an operator can
# act on and "schema gate passed" is not.
dr_reversibility_sentence() {  # dr_reversibility_sentence VERDICT_LINE
  local line="${1-}" verdict previous after
  verdict="$(_dr_verdict_field "$line" verdict)" || {
    printf 'no reversibility verdict was recorded, so an automatic rollback will REFUSE rather than guess.\n'
    return 1
  }
  previous="$(_dr_verdict_field "$line" previous)" || previous='?'
  after="$(_dr_verdict_field "$line" after)" || after='?'
  case "$verdict" in
    reversible)
      printf 'V%s code knows V%s, which is where the database will be after this deploy, so a failed health gate CAN be rolled back automatically.\n' \
        "$previous" "$after" ;;
    forward-only)
      printf 'V%s > V%s, so a failed health gate CANNOT be rolled back automatically - the database will have applied V%s and V%s code cannot start on it.\n' \
        "$after" "$previous" "$after" "$previous" ;;
    *)
      printf 'the recorded reversibility verdict (%s) is not one this script understands, so an automatic rollback will REFUSE.\n' "$verdict"
      return 1 ;;
  esac
}

# The rollback-time decision. FAIL-CLOSED: every path that is not a proof that
# the rollback is safe is a refusal.
#
#   dr_rollback_is_reversible VERDICT_LINE LIVE_NOW
#
# VERDICT_LINE  what dr_reversibility_verdict printed before the deploy began,
#               read back from the release record (or empty, if none was ever
#               written - which is itself a refusal).
# LIVE_NOW      the live schema version read at THIS moment, or empty when it
#               cannot be read. Empty is a REFUSAL, not a shrug: an unreadable
#               database is the state this gate was fooled by.
#
# Prints one line naming the decision and its reason. Returns 0 to proceed, 1
# to refuse.
dr_rollback_is_reversible() {
  local line="${1-}" live_now="${2-}" verdict previous range
  # Nothing at all (no release record, or a record written before this gate
  # existed) is a different failure from "something, but not a verdict", and an
  # operator reading the log at 3 a.m. should not have to guess which happened.
  if [ -z "${line//[[:space:]]/}" ]; then
    printf 'refuse reason=no-recorded-verdict\n'; return 1
  fi
  verdict="$(_dr_verdict_field "$line" verdict)" || verdict=''
  previous="$(_dr_verdict_field "$line" previous)" || previous=''
  case "$verdict" in
    reversible | forward-only) : ;;
    *) printf 'refuse reason=unparseable-verdict\n'; return 1 ;;
  esac
  _dr_is_uint "$previous" || { printf 'refuse reason=unparseable-verdict\n'; return 1; }
  # THE case this whole helper exists for: /health is down (a rollback is only
  # reached when the health gate or `techsara up` failed) and the psql fallback
  # did not answer either. Postgres is USUALLY still running when only the
  # application containers were recreated, so this is an uncommon path rather
  # than "the normal state when a rollback is being considered" - the wording
  # this comment used to carry, and the same overstatement scripts/deploy.sh
  # corrected in its own two copies. Uncommon is not impossible, and it is the
  # path on which a fail-open starts old code on a newer schema unattended.
  if ! _dr_is_uint "$live_now"; then
    printf 'refuse reason=live-schema-unreadable previous=%s\n' "$previous"; return 1
  fi
  # THE COMPARISON THAT DECIDES, and it is against the FRESH reading, not
  # against the verdict's own prediction.
  #
  # The verdict was taken before the deploy ran and says what the database
  # WOULD hold once the target's migrations applied. Whether they applied is
  # exactly what live_now answers, and the two cases differ:
  #
  #   * the HEALTH GATE failed. `techsara up` succeeded, the orchestrator
  #     started, init_schema ran: live_now is the target's version, it is
  #     above PREVIOUS, and the rollback is refused. This is the case the
  #     forward-only verdict predicted.
  #   * `techsara up` FAILED - a build error, a container that would not
  #     start. Nothing migrated anything, live_now is still where it was, and
  #     rolling back to PREVIOUS is both safe and the right thing to do.
  #     Refusing it on the verdict alone would make the automatic rollback
  #     useless on most releases, because db.py went V13 -> V41 in 22 days
  #     and nearly every release therefore carries a forward-only verdict.
  #
  # Fail-closed is about the UNKNOWN (handled above), not about refusing what
  # a good reading says is fine.
  if [ "$previous" -lt "$live_now" ]; then
    if [ "$verdict" = forward-only ]; then
      range="$(_dr_verdict_field "$line" range)" || range='-'
      printf 'refuse reason=forward-only previous=%s live=%s range=%s\n' "$previous" "$live_now" "$range"
    else
      # The verdict said reversible and the database moved anyway: something
      # other than this deploy migrated it. The fresh reading wins.
      printf 'refuse reason=database-moved-past-previous previous=%s live=%s\n' "$previous" "$live_now"
    fi
    return 1
  fi
  if [ "$verdict" = forward-only ]; then
    printf 'proceed previous=%s live=%s note=the-targets-migrations-did-not-apply\n' "$previous" "$live_now"
    return 0
  fi
  printf 'proceed previous=%s live=%s\n' "$previous" "$live_now"
}

# ------------------------------------------------ waiting inside a job ceiling
# scripts/deploy.sh is DESIGNED to wait: it queues behind a hand-run deploy for
# the deploy lock, and behind an automatic engine recovery for the engine lock.
# A person at a terminal can wait as long as those waits need. A GitHub Actions
# job cannot: `timeout-minutes` cancels it wherever the step happens to be, and
# if that is inside `techsara up` the box is left part-recreated with no health
# gate and no rollback - the one outcome the deploy path is arranged to prevent.
#
# Sizing each wait individually does not answer this, because the number of
# waits is not one. A single deploy job runs deploy.sh TWICE (the preflight dry
# run and the rollout), and the rollout's own apply() takes the engine lock once
# on the way forward and AGAIN for the rollback - engine_lock_release() unsets
# ENGINE_LOCK_HELD_BY, so the second acquire is a real acquire with a real
# timeout, not the re-entrant no-op it looks like. Four waits, not two.
#
# So the ceiling is expressed as ONE wall-clock budget for the whole invocation
# and every wait is clamped to what is left of it. A wait that has no time left
# becomes a non-blocking attempt, which FAILS DIAGNOSABLY - the script says
# which lock it could not get and who holds it - instead of the job being
# cancelled mid-`up`.
#
# Pure: the clock is an argument, so the arithmetic is unit-testable
# (.github/workflows/scripts/tests/test_deploy_lock_budget.py).
#
#   dr_wait_within_budget REQUESTED STARTED_EPOCH CEILING_S NOW_EPOCH
#
# CEILING_S of 0, empty or non-numeric means NO budget, and REQUESTED is
# returned unchanged. That is the hand-run default: nothing should quietly
# shorten an operator's wait because a workflow needed a ceiling.
dr_wait_within_budget() {
  local requested="${1-}" started="${2-}" ceiling="${3-}" now="${4-}" remaining
  _dr_is_uint "$requested" || return 1
  if ! _dr_is_uint "$ceiling" || [ "$ceiling" -eq 0 ]; then
    printf '%s\n' "$requested"
    return 0
  fi
  _dr_is_uint "$started" && _dr_is_uint "$now" || return 1
  remaining=$(( started + ceiling - now ))
  [ "$remaining" -lt 0 ] && remaining=0
  if [ "$requested" -gt "$remaining" ]; then
    printf '%s\n' "$remaining"
  else
    printf '%s\n' "$requested"
  fi
}

# ------------------------------------------------------- the model clock
# Did the main model's clock do what THIS deploy asked of it?
#
# This is one function because two callers disagreed about it, and wiring them
# together turned that disagreement into a red pipeline. The verify job's own
# step exits 0 when `needs.deploy.outputs.was_full == 'true'`, because `--full`
# reloads the models on purpose and asserting the clock did not move would be
# asserting the opposite of what was asked for. scripts/deploy-smoke.sh's
# MODEL CLOCK check had no such exemption - harmless while nothing called it,
# and a failed verify plus a recovery job on the first deliberate `--full`
# deploy once something did.
#
#   dr_model_clock_verdict RECORDED_STARTED_AT OBSERVED_STARTED_AT RESTART_EXPECTED
#
# Prints exactly one verdict word, and exits 0 when what was observed is what
# was asked for and 1 when it is not:
#
#   preserved     the instants are equal and no reload was asked for   -> 0
#   reloaded      they differ and a reload WAS asked for               -> 0
#   restarted     they differ and nothing asked for that               -> 1
#   not-reloaded  they are equal and a reload was asked for            -> 1
#   unreadable    either instant is missing                            -> 1
#
# RESTART_EXPECTED is 1 only for `--full`. Anything else - empty, 0, a word -
# means a rolling deploy, because the assertion that the engine was PRESERVED
# is the one that must not be switched off by a typo.
#
# An empty instant is not a verdict of its own: only the caller knows whether
# that is a missing baseline or a missing container, so it says which.
# Unit-tested at .github/workflows/scripts/tests/test_deploy_smoke_model_clock.py.
dr_model_clock_verdict() {
  local recorded="${1-}" observed="${2-}" expected="${3-}"
  if [ -z "$recorded" ] || [ -z "$observed" ]; then
    printf 'unreadable\n'; return 1
  fi
  if [ "$expected" = 1 ]; then
    if [ "$recorded" = "$observed" ]; then
      printf 'not-reloaded\n'; return 1
    fi
    printf 'reloaded\n'; return 0
  fi
  if [ "$recorded" = "$observed" ]; then
    printf 'preserved\n'; return 0
  fi
  printf 'restarted\n'; return 1
}

# ------------------------------------------------------------------ env reads
# A single value from the merged --env-file chain, via the launcher's canonical
# parser. Never echoes anything but the one key asked for, and callers only ask
# for ports and database names.
dr_env_value() {
  local key="$1"
  DR_KEY="$key" python3 - "$DR_ROOT" <<'PY' 2>/dev/null
import os, sys
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "launcher"))
try:
    from techsara_cli.utils import parse_env_file
except Exception:
    raise SystemExit(0)
from pathlib import Path
merged = {}
for name in (".env", ".runtime/secrets.env", ".runtime/generated.env"):
    path = Path(root) / name
    if path.is_file():
        try:
            merged.update(parse_env_file(path))
        except Exception:
            pass
print(merged.get(os.environ["DR_KEY"], ""))
PY
}

# The MAJOR version of the PostgreSQL that is actually deployed. Asked of the
# running server, never inferred from a tag: `postgres@sha256:...` says nothing
# about its version, and a rehearsal on the wrong major proves nothing.
dr_deployed_pg_major() {
  local pg out
  pg="$(dr_container_for postgres)"
  out="$(docker exec "$pg" postgres --version 2>/dev/null)" || return 1
  # "postgres (PostgreSQL) 18.4" and "postgres (PostgreSQL) 16.15 (Debian ...)"
  # both have to yield the major, so skip everything up to the first digit.
  printf '%s\n' "$out" | sed -n 's/.*PostgreSQL[^0-9]*\([0-9][0-9]*\).*/\1/p'
}

dr_releases_dir() { printf '%s\n' "$DR_ROOT/.runtime/releases"; }
