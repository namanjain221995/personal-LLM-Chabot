#!/usr/bin/env bash
#
# e2e/ci/stack.sh — the throwaway application stack the `e2e-hosted` job runs
# the product in, on a HOSTED runner that holds nothing worth stealing.
#
# WHAT IT BRINGS UP (three containers on one private docker network):
#   <project>-engine        e2e/ci/engine.js, the OpenAI-compatible stub. NO
#                           host port: its peers reach it by container name.
#   <project>-orchestrator  the real orchestrator-cpu image this job built,
#                           published on 127.0.0.1 only. It runs its own
#                           migrations at boot against a genuinely empty
#                           database — which is the signal the `schema` job
#                           cannot give, because that one runs the SQL from
#                           the checkout and never the image's startup path.
#   <project>-frontend      the real frontend image this job built, published
#                           on 127.0.0.1 only.
# The database is the job's `services: postgres` container, pinned to the same
# digest the orchestrator and schema jobs use, reached across the docker host
# gateway.
#
# WHY IT IS NOT scripts/e2e-stack.sh. That script is for the PRODUCTION BOX:
# it shares the real model engines on purpose, it builds the CUDA image, and
# it starts from the production orchestrator's own environment. None of those
# is available, affordable or safe on a hosted runner, and the last one is a
# thing this stage must never learn to do.
#
# WHAT IT IS NOT ALLOWED TO DO: reach a real engine (`--chat-mode live` is
# refused by the job), hold a stored secret (every credential here is
# generated for this run and dies with the runner), or publish anything on an
# address that is not loopback.
#
# Usage:  stack.sh up | down | logs [container] [lines] | schema
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where generated credentials live. $RUNNER_TEMP on Actions (removed with the
# job); a mktemp directory anywhere else.
STACK_TMP="${STACK_TMP:-${RUNNER_TEMP:-}}"
if [ -z "$STACK_TMP" ]; then
  STACK_TMP="$(mktemp -d)"
fi

say() { printf '[e2e-ci] %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

# --------------------------------------------------------------- ci.env
# Read the checked-in defaults, refusing anything that is not a literal
# KEY=value. A `$(...)`, a backtick or a `${{` in this file would be shell
# injected into every stack that ever starts; there is no reason for one to be
# there, so it is a hard refusal rather than an escape.
load_ci_env() {
  local file="$HERE/ci.env" line key value
  [ -r "$file" ] || die "cannot read $file"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
      *'$('*|*'`'*|*'${'*) die "ci.env: refusing a line with shell expansion in it: ${line%%=*}=..." ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      [A-Z]*) ;;
      *) die "ci.env: '$key' is not a plain KEY=value name" ;;
    esac
    [ "$key" != "$line" ] || die "ci.env: '$line' is not KEY=value"
    export "$key=$value"
  done < "$file"
}
load_ci_env

PROJECT="${STACK_PROJECT:?ci.env must set STACK_PROJECT}"
NET="${STACK_NETWORK:?}"
ENGINE="${STACK_ENGINE_NAME:?}"
ORCH="${STACK_ORCH_NAME:?}"
FRONT="${STACK_FRONT_NAME:?}"
ORCH_PORT="${STACK_ORCH_PORT:?}"
FRONT_PORT="${STACK_FRONT_PORT:?}"
ENGINE_PORT="${STACK_ENGINE_PORT:?}"
DATA_VOL="${PROJECT}-data"
REPORTS_VOL="${PROJECT}-reports"
ENV_FILE="$STACK_TMP/e2e-ci.env"

# The images this job built. Never a moving tag, never a pull.
ORCH_IMAGE="${E2E_CI_ORCH_IMAGE:?set E2E_CI_ORCH_IMAGE to the orchestrator-cpu image this job built}"
FRONT_IMAGE="${E2E_CI_FRONT_IMAGE:?set E2E_CI_FRONT_IMAGE to the frontend image this job built}"
# The stub engine runs on the SAME node base the frontend image pins, by
# digest. Nothing here pulls a moving tag, and no model image is pulled at all.
ENGINE_IMAGE="${E2E_CI_ENGINE_IMAGE:-node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293}"

ADMIN_PASSWORD_FILE="${E2E_ADMIN_PASSWORD_FILE:-}"
MEMBER_PASSWORD_FILE="${E2E_MEMBER_PASSWORD_FILE:-}"

guard_names() {
  # Every object this script creates or destroys must carry `e2e-ci`, and none
  # may look like production. The check is on the VALUES, because a typo that
  # pointed `docker rm -f` at a production name is the one mistake here that
  # cannot be undone.
  local all="$PROJECT$NET$ENGINE$ORCH$FRONT$DATA_VOL$REPORTS_VOL"
  case "$all" in
    *sf-local-ai*) die "refusing to run: a target name looks like production" ;;
  esac
  case "$PROJECT$NET$ENGINE$ORCH$FRONT" in
    *e2e-ci*) ;;
    *) die "refusing to run: the names do not all carry 'e2e-ci'" ;;
  esac
  # lib/config.js refuses 3000 and 8080 on every host as production ports;
  # this stack must not bind them either, whatever ci.env says.
  case "$FRONT_PORT:$ORCH_PORT" in
    3000:*|*:8080|8080:*|*:3000) die "refusing to bind a production port ($FRONT_PORT/$ORCH_PORT)" ;;
  esac
}

# A fresh 32-byte hex value, written with mode 600 and never echoed. Generated
# PER RUN: a throwaway stack has no use for a durable credential, and a
# durable one on a public repository's hosted runners is a liability.
generate_secret() { # generate_secret <path>
  local path="$1"
  if [ ! -s "$path" ]; then
    ( umask 077; openssl rand -hex 32 > "$path" )
  fi
}

write_env_file() {
  local session_file="$STACK_TMP/e2e-ci-session.key"
  local pepper_file="$STACK_TMP/e2e-ci-pepper.key"
  generate_secret "$session_file"
  generate_secret "$pepper_file"
  rm -f "$ENV_FILE"
  ( umask 077; : > "$ENV_FILE" )
  # AN ALLOWLIST, spelled out. Everything this container's environment holds
  # comes from the checked-in ci.env or is generated just above; nothing is
  # copied out of another container, and no value reaches here from `inputs.`
  # or `vars.`.
  {
    # The application half of ci.env. STACK_* and STUB_* name containers,
    # ports and the stub's own settings and have no business inside the
    # orchestrator; E2E_* belongs to the suite, on the runner.
    grep -E '^[A-Z][A-Z0-9_]*=' "$HERE/ci.env" | grep -vE '^(STACK_|STUB_|E2E_)'
    printf 'APP_DATABASE_URL=postgresql://%s:%s@%s:%s/%s\n' \
      "${STACK_PG_USER}" "${STACK_PG_PASSWORD:-test}" "${STACK_PG_HOST}" "${STACK_PG_PORT}" "${STACK_PG_DB}"
    printf 'SESSION_SECRET=%s\n' "$(cat "$session_file")"
    printf 'API_KEY_PEPPER=%s\n' "$(cat "$pepper_file")"
    # The stub answers instantly; a long engine deadline would only turn a
    # broken stub into a slow job.
    printf 'LLM_REQUEST_TIMEOUT=60\n'
  } >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

wait_for() { # wait_for <label> <seconds> <command...>
  local label="$1" budget="$2"; shift 2
  local i
  for i in $(seq 1 "$budget"); do
    if "$@" >/dev/null 2>&1; then
      say "$label ready after ${i}s"
      return 0
    fi
    sleep 1
  done
  return 1
}

engine_healthy() {
  docker exec "$ENGINE" node -e "fetch('http://127.0.0.1:${ENGINE_PORT}/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
}

orch_healthy() { curl -fsS -m 15 "http://127.0.0.1:${ORCH_PORT}/health" >/dev/null; }
front_healthy() { curl -fsS -m 10 "http://127.0.0.1:${FRONT_PORT}/login" >/dev/null; }

seed_account() { # seed_account <username> <role> <password-file>
  local name="$1" role="$2" file="$3"
  [ -s "$file" ] || die "no password file for $name ($file)"
  # THE PASSWORD GOES ON STDIN AND NOWHERE ELSE. Not in argv (the docker
  # client's /proc/<pid>/cmdline is world-readable), not in the environment
  # (`docker inspect` reads that back). The program text is what `-c` carries,
  # and it holds no secret.
  { tr -d '\r\n' < "$file"; printf '\n'; } \
    | docker exec -i "$ORCH" python3 -c "$(cat "$HERE/seed.py")" "$name" "$role"
}

up() {
  guard_names
  command -v docker >/dev/null || die "docker is not on PATH"
  command -v openssl >/dev/null || die "openssl is not on PATH"
  [ -n "$ADMIN_PASSWORD_FILE" ] && [ -s "$ADMIN_PASSWORD_FILE" ] || die "E2E_ADMIN_PASSWORD_FILE is unset or empty"
  [ -n "$MEMBER_PASSWORD_FILE" ] && [ -s "$MEMBER_PASSWORD_FILE" ] || die "E2E_MEMBER_PASSWORD_FILE is unset or empty"

  write_env_file
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  docker volume create "$DATA_VOL" >/dev/null
  docker volume create "$REPORTS_VOL" >/dev/null

  say "starting $ENGINE (stub engine, no host port)"
  docker rm -f "$ENGINE" >/dev/null 2>&1 || true
  # --read-only: the stub writes nothing, so nothing may write to it either.
  # No -p: the only things that may reach it are its peers on $NET.
  docker run -d --name "$ENGINE" \
    --network "$NET" --network-alias "$ENGINE" \
    --read-only --tmpfs /tmp:rw,size=16m \
    -v "$HERE:/srv:ro" \
    -e "STUB_ENGINE_PORT=$ENGINE_PORT" \
    -e "STUB_ENGINE_MODELS=${STUB_ENGINE_MODELS}" \
    -e "STUB_ENGINE_MAX_MODEL_LEN=${STUB_ENGINE_MAX_MODEL_LEN}" \
    -e "STUB_ENGINE_EMBED_DIM=${STUB_ENGINE_EMBED_DIM}" \
    --init --restart no "$ENGINE_IMAGE" node /srv/engine.js >/dev/null
  wait_for "stub engine" 60 engine_healthy || { docker logs "$ENGINE" --tail 50; die "stub engine did not answer /health"; }

  say "starting $ORCH on 127.0.0.1:$ORCH_PORT (migrations run at boot)"
  docker rm -f "$ORCH" >/dev/null 2>&1 || true
  docker run -d --name "$ORCH" \
    --network "$NET" --network-alias "$ORCH" \
    --env-file "$ENV_FILE" \
    --add-host host.docker.internal:host-gateway \
    -v "$DATA_VOL:/data" -v "$REPORTS_VOL:/reports" \
    -p "127.0.0.1:$ORCH_PORT:8080" \
    --init --restart no "$ORCH_IMAGE" >/dev/null
  # Generous, and deliberately not generous enough to hide a hang: a first
  # boot runs every migration from V0. The number is UNMEASURED until a real
  # Actions run reports one.
  if ! wait_for "orchestrator" 300 orch_healthy; then
    docker logs "$ORCH" --tail 200
    die "orchestrator did not become healthy"
  fi

  say "starting $FRONT on 127.0.0.1:$FRONT_PORT"
  docker rm -f "$FRONT" >/dev/null 2>&1 || true
  docker run -d --name "$FRONT" \
    --network "$NET" --network-alias "$FRONT" \
    -e "ORCHESTRATOR_URL=http://$ORCH:8080" \
    -e "NEXT_PUBLIC_APP_NAME=TechSara AI (e2e-ci)" \
    -p "127.0.0.1:$FRONT_PORT:3000" \
    --init --restart no "$FRONT_IMAGE" >/dev/null
  if ! wait_for "frontend" 120 front_healthy; then
    docker logs "$FRONT" --tail 200
    die "frontend did not serve /login"
  fi

  seed_account "${E2E_ADMIN_EMAIL%%@*}" super_admin "$ADMIN_PASSWORD_FILE"
  seed_account "${E2E_MEMBER_EMAIL%%@*}" member "$MEMBER_PASSWORD_FILE"
  schema
  say "up: orchestrator http://127.0.0.1:$ORCH_PORT   frontend http://127.0.0.1:$FRONT_PORT"
}

schema() {
  say "schema: $(docker exec "$ORCH" python3 -c 'from app import db; print(db.LATEST_SCHEMA_VERSION)' 2>/dev/null || echo '?')"
}

down() {
  guard_names
  docker rm -f "$FRONT" "$ORCH" "$ENGINE" >/dev/null 2>&1 || true
  docker volume rm "$DATA_VOL" "$REPORTS_VOL" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  # The generated credentials go with the stack, not at the end of the job:
  # $RUNNER_TEMP is removed by the runner, but a file that held a session
  # signing key should not outlive the thing it signed for.
  rm -f "$ENV_FILE" "$STACK_TMP/e2e-ci-session.key" "$STACK_TMP/e2e-ci-pepper.key"
  say "removed containers, volumes, network and the generated environment"
}

logs() {
  local which="${1:-$ORCH}" lines="${2:-120}"
  docker logs "$which" --tail "$lines" 2>&1 || true
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  schema) schema ;;
  logs) shift; logs "$@" ;;
  *) die "usage: $0 up|down|schema|logs [container] [lines]" ;;
esac
