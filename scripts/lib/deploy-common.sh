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
#: mutable tags a deploy can get wrong.
DR_APP_SERVICES=(orchestrator sync-worker frontend)

#: The compose files that MUST all be present in the chain, in this order.
DR_REQUIRED_COMPOSE_FILES=(
  compose.yaml
  compose/compose.dgx-spark.yaml
  compose/compose.published-dgx-spark.yaml
  compose/compose.cluster-dgx-spark.yaml
)

dr_now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
dr_stamp(){ date -u +%Y%m%d-%H%M%SZ; }

DR_LOG="${DR_LOG:-}"
dr_say()  { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | { [ -n "$DR_LOG" ] && tee -a "$DR_LOG" || cat; }; }
dr_warn() { printf '%s WARN  %s\n' "$(date -u +%H:%M:%S)" "$*" | { [ -n "$DR_LOG" ] && tee -a "$DR_LOG" || cat; } >&2; }
dr_die()  { printf '%s ERROR %s\n' "$(date -u +%H:%M:%S)" "$*" | { [ -n "$DR_LOG" ] && tee -a "$DR_LOG" || cat; } >&2; exit 1; }

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
  [ -n "$DR_LOCK_HOLDER_FILE" ] && : >"$DR_LOCK_HOLDER_FILE" 2>/dev/null || true
  [ -n "$DR_LOCK_FD" ] && eval "exec ${DR_LOCK_FD}>&-" 2>/dev/null || true
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
