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
# WHAT IT IS NOT ALLOWED TO DO: hold a stored secret (every credential here is
# generated for this run and dies with the runner), or publish anything on an
# address that is not loopback.
#
# WHAT IT DOES NOT DO, AND WHAT STILL STANDS BETWEEN IT AND A REAL ENGINE
# (corrected 2026-09-27; the earlier wording said flatly that a real engine
# could not be reached, and that was argued from configuration, not measured).
# No request is made to one: every *_BASE_URL(S) and ENGINE_CONTROLLER_URL in
# ci.env names the in-repo stub, `--chat-mode live` is refused by the job, and
# test_e2e_ci_policy.py pins both. On a HOSTED runner there is nothing else
# there. On a box that also runs production -- which is where this script is in
# fact run while it is being developed -- the ROUTE exists: the orchestrator
# container is started with `--add-host host.docker.internal:host-gateway` so
# it can reach the job's database, and this host has a listener on
# 0.0.0.0:8000 (measured today with `ss -ltn '( sport = :8000 )'`), which is
# the live engine. Only the reviewed ci.env stands between the two, so an edit
# that repoints one address is a production-reaching change, not a CI one.
#
# Usage:  stack.sh up | down | logs [container] [lines] | schema
#
# STACK_FORCE=1 overrides two refusals, and only for a human on a shared box:
# `up` taking over container names something else already holds, and `down`
# removing the fixed names when it cannot tell whether this shell started them.
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
# The extra DNS names the one stub container answers on, so each engine key
# has its OWN address and app/publicapi/registry.py does not withdraw five of
# the six public models. See the long comment in ci.env.
ENGINE_ALIASES="${STACK_ENGINE_ALIASES:-}"
DATA_VOL="${PROJECT}-data"
REPORTS_VOL="${PROJECT}-reports"
ENV_FILE="$STACK_TMP/e2e-ci.env"

# The images this job built. Never a moving tag, never a pull.
#
# REQUIRED BY up() AND BY NOTHING ELSE (2026-09-27). These used to be
# `${VAR:?...}` here, at top level, so `down` and `logs` -- which do not use an
# image tag at all -- exited 1 with "set E2E_CI_ORCH_IMAGE to the
# orchestrator-cpu image this job built". Measured on this branch before the
# fix: `( unset E2E_CI_ORCH_IMAGE E2E_CI_FRONT_IMAGE; bash stack.sh down )`
# exited 1 without removing anything. The job's teardown step is
# `if: always()`, and its FIRST step is `node --test e2e/ci/tests/`, so any
# failure before the build step left the stack up behind a confusing message.
ORCH_IMAGE="${E2E_CI_ORCH_IMAGE:-}"
FRONT_IMAGE="${E2E_CI_FRONT_IMAGE:-}"
# The stub engine runs on the SAME node base the frontend image pins, by
# digest. Nothing here pulls a moving tag, and no model image is pulled at all.
ENGINE_IMAGE="${E2E_CI_ENGINE_IMAGE:-node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293}"

ADMIN_PASSWORD_FILE="${E2E_ADMIN_PASSWORD_FILE:-}"
MEMBER_PASSWORD_FILE="${E2E_MEMBER_PASSWORD_FILE:-}"

# WHOSE STACK IS THIS (2026-09-27). The object names are FIXED in ci.env,
# because the workflow steps and test_e2e_ci_policy.py name them literally, so
# two invocations on one host collide on every one of them: up() `docker rm -f`s
# whatever holds the name and down() removes the shared volumes and network,
# whether or not this invocation created them. On a hosted runner nothing
# pre-exists and that is harmless. On the shared box it is not: a peer session
# had this exact stack up while this branch was being verified, and the
# verifier had to rename every object by hand to avoid destroying their
# containers and volumes.
#
# So every object up() creates is LABELLED with a token written under
# $STACK_TMP, and down() removes only what carries the token. $STACK_TMP is
# $RUNNER_TEMP inside the job, which up() and down() share, so the hosted job
# is unaffected.
STACK_LABEL="techsara.e2e.ci.stack"
STACK_ID_FILE="$STACK_TMP/e2e-ci-stack.id"

guard_names() {
  # Every object this script creates or destroys must carry `e2e-ci`, and none
  # may look like production. The check is on the VALUES, because a typo that
  # pointed `docker rm -f` at a production name is the one mistake here that
  # cannot be undone.
  local all="$PROJECT$NET$ENGINE$ORCH$FRONT$DATA_VOL$REPORTS_VOL$ENGINE_ALIASES"
  case "$all" in
    *sf-local-ai*) die "refusing to run: a target name looks like production" ;;
  esac
  case "$PROJECT$NET$ENGINE$ORCH$FRONT" in
    *e2e-ci*) ;;
    *) die "refusing to run: the names do not all carry 'e2e-ci'" ;;
  esac
  # Each alias, individually: `*e2e-ci*` over the whole joined string would pass
  # while one entry in the list named anything at all.
  local alias
  for alias in $(printf '%s' "$ENGINE_ALIASES" | tr ',' ' '); do
    case "$alias" in
      *e2e-ci*) ;;
      *) die "refusing to run: engine alias '$alias' does not carry 'e2e-ci'" ;;
    esac
  done
  # lib/config.js refuses 3000 and 8080 on every host as production ports;
  # this stack must not bind them either, whatever ci.env says.
  case "$FRONT_PORT:$ORCH_PORT" in
    3000:*|*:8080|8080:*|*:3000) die "refusing to bind a production port ($FRONT_PORT/$ORCH_PORT)" ;;
  esac
}

# The containers, volumes and networks carrying this invocation's ownership
# token. One `docker ... ls` per kind; `--filter label=k=v` is an exact match.
owned() { # owned <containers|volumes|networks> <token>
  local kind="$1" token="$2"
  case "$kind" in
    containers) docker ps -a --filter "label=$STACK_LABEL=$token" --format '{{.Names}}' ;;
    volumes)    docker volume ls --filter "label=$STACK_LABEL=$token" --format '{{.Name}}' ;;
    networks)   docker network ls --filter "label=$STACK_LABEL=$token" --format '{{.Name}}' ;;
  esac
}

# REFUSE A STACK SOMETHING ELSE IS ALREADY RUNNING. `docker rm -f` on a name a
# peer session holds is the one mistake in this script that destroys work
# outside it, and it used to be unconditional. On a hosted runner no container
# of these names can pre-exist, so this never fires there.
refuse_on_collision() {
  local found name
  found=""
  for name in "$ENGINE" "$ORCH" "$FRONT"; do
    if [ -n "$(docker ps -a --filter "name=^${name}\$" --format '{{.Names}}' 2>/dev/null)" ]; then
      found="$found $name"
    fi
  done
  [ -n "$found" ] || return 0
  if [ "${STACK_FORCE:-0}" = "1" ]; then
    say "STACK_FORCE=1: taking over$found"
    return 0
  fi
  die "refusing to run: the container(s)$found already exist. \
The names in ci.env are fixed, so bringing this stack up would 'docker rm -f' them and \
tearing it down would remove the volumes and network they share. \
Remove that stack first (bash e2e/ci/stack.sh down from the shell that started it), \
or set STACK_FORCE=1 to take the names over deliberately."
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

# A WALL-CLOCK DEADLINE, NOT A LOOP COUNT (2026-09-27). `for i in $(seq 1 300)`
# with `sleep 1` only costs 300 s when the probe returns immediately, which is
# what connection-refused does. A probe that BLOCKS costs its own timeout every
# iteration: orch_healthy is `curl -m 15`, so an orchestrator that accepts the
# connection and then hangs made this 300 x (15 + 1) = 80 minutes, above the
# job's `timeout-minutes: 60` -- the job would be killed by the ceiling instead
# of printing `docker logs` and "orchestrator did not become healthy", which is
# the whole point of the budget. With a deadline the worst case is the budget
# plus one probe (300 s + 15 s here).
wait_for() { # wait_for <label> <seconds> <command...>
  local label="$1" budget="$2"; shift 2
  local start deadline
  start="$(date +%s)"
  deadline=$(( start + budget ))
  while :; do
    if "$@" >/dev/null 2>&1; then
      say "$label ready after $(( $(date +%s) - start ))s"
      return 0
    fi
    [ "$(date +%s)" -lt "$deadline" ] || return 1
    sleep 1
  done
}

engine_healthy() {
  docker exec "$ENGINE" node -e "fetch('http://127.0.0.1:${ENGINE_PORT}/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
}

orch_healthy() { curl -fsS -m 15 "http://127.0.0.1:${ORCH_PORT}/health" >/dev/null; }

# A 200 FROM /health IS NOT A HEALTHY ORCHESTRATOR (2026-09-27).
# `/health` answers 200 with `"status": "degraded"`, so `curl -fsS` cannot tell a
# booted stack from a booted-broken one. Measured on the first local run of this
# stage: the stack came up reporting `degraded`, with `checks.duckdb.status ==
# "error"` and the engine controller unreachable, and every wait_for here was
# satisfied.
#
# The two checks that decide whether this stack can be tested at all are named
# explicitly; everything else that is not `ok` is PRINTED with its detail rather
# than silently tolerated. A warehouse file that does not exist yet on a stack
# five seconds old is not a reason to refuse, and it is a reason to say so.
assert_orch_health() {
  docker exec -i "$ORCH" python3 - <<'HEALTHPY'
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=20) as response:
    body = json.load(response)

checks = body.get("checks") or {}
print(f"orchestrator /health: status={body.get('status')!r}")
for name, check in sorted(checks.items()):
    if isinstance(check, dict) and check.get("status") != "ok":
        detail = str(check.get("detail") or "")[:300]
        print(f"  NOT OK  {name}: {check.get('status')} {detail}")

required = ("app_db", "vllm")
broken = [
    f"{name}={(checks.get(name) or {}).get('status')!r}"
    for name in required
    if (checks.get(name) or {}).get("status") != "ok"
]
if broken:
    sys.exit("the orchestrator answered /health but is not usable: " + ", ".join(broken))
print(f"  app_db ok at schema version {(checks.get('app_db') or {}).get('schema_version')}")
HEALTHPY
}
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
  # Here, not at top level: `down` and `logs` use no image tag, and the job's
  # teardown step must not fail because the build step never ran.
  [ -n "$ORCH_IMAGE" ] || die "set E2E_CI_ORCH_IMAGE to the orchestrator-cpu image this job built"
  [ -n "$FRONT_IMAGE" ] || die "set E2E_CI_FRONT_IMAGE to the frontend image this job built"
  [ -n "$ADMIN_PASSWORD_FILE" ] && [ -s "$ADMIN_PASSWORD_FILE" ] || die "E2E_ADMIN_PASSWORD_FILE is unset or empty"
  [ -n "$MEMBER_PASSWORD_FILE" ] && [ -s "$MEMBER_PASSWORD_FILE" ] || die "E2E_MEMBER_PASSWORD_FILE is unset or empty"
  refuse_on_collision

  # The ownership token, so down() can tell this stack from a peer's.
  if [ ! -s "$STACK_ID_FILE" ]; then
    ( umask 077; openssl rand -hex 8 > "$STACK_ID_FILE" )
  fi
  local token label
  token="$(tr -d '\r\n' < "$STACK_ID_FILE")"
  label="$STACK_LABEL=$token"
  say "stack token $token (from $STACK_ID_FILE); down removes only what carries it"

  write_env_file
  docker network inspect "$NET" >/dev/null 2>&1 \
    || docker network create --label "$label" "$NET" >/dev/null
  # FRESH /data AND /reports. A volume left behind by an earlier stack of these
  # fixed names would hand this one a warehouse, a workspace and a reports
  # directory it did not create, and "against a genuinely empty database" is
  # half of what this stage proves. Safe here and only here: refuse_on_collision
  # above has already established that no container of these names is running.
  docker volume rm "$DATA_VOL" "$REPORTS_VOL" >/dev/null 2>&1 || true
  docker volume create --label "$label" "$DATA_VOL" >/dev/null
  docker volume create --label "$label" "$REPORTS_VOL" >/dev/null

  say "starting $ENGINE (stub engine, no host port)"
  docker rm -f "$ENGINE" >/dev/null 2>&1 || true
  # --read-only: the stub writes nothing, so nothing may write to it either.
  # No -p: the only things that may reach it are its peers on $NET.
  # One --network-alias per engine key (see ci.env): six addresses, one process.
  # The `${a[@]+"${a[@]}"}` form below expands to NOTHING when the list is empty
  # instead of tripping `set -u`, which it does on bash 3.2 (macOS).
  local alias_args=() alias
  for alias in $(printf '%s' "$ENGINE_ALIASES" | tr ',' ' '); do
    alias_args+=(--network-alias "$alias")
  done
  # ONLY engine.js, not the whole directory. The stub is a network server; it has
  # no business reading ci.env, seed.py or redact.js, and a credential that ever
  # lands in ci.env by mistake must not be one `cat` away inside a container
  # that is listening on a socket.
  docker run -d --name "$ENGINE" --label "$label" \
    --network "$NET" --network-alias "$ENGINE" "${alias_args[@]+"${alias_args[@]}"}" \
    --read-only --tmpfs /tmp:rw,size=16m \
    -v "$HERE/engine.js:/srv/engine.js:ro" \
    -e "STUB_ENGINE_PORT=$ENGINE_PORT" \
    -e "STUB_ENGINE_MODELS=${STUB_ENGINE_MODELS}" \
    -e "STUB_ENGINE_MAX_MODEL_LEN=${STUB_ENGINE_MAX_MODEL_LEN}" \
    -e "STUB_ENGINE_EMBED_DIM=${STUB_ENGINE_EMBED_DIM}" \
    --init --restart no "$ENGINE_IMAGE" node /srv/engine.js >/dev/null
  wait_for "stub engine" 60 engine_healthy || { docker logs "$ENGINE" --tail 50; die "stub engine did not answer /health"; }

  say "starting $ORCH on 127.0.0.1:$ORCH_PORT (migrations run at boot)"
  docker rm -f "$ORCH" >/dev/null 2>&1 || true
  docker run -d --name "$ORCH" --label "$label" \
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
  if ! assert_orch_health; then
    docker logs "$ORCH" --tail 200
    die "the orchestrator answered /health but is not usable (see above)"
  fi

  say "starting $FRONT on 127.0.0.1:$FRONT_PORT"
  docker rm -f "$FRONT" >/dev/null 2>&1 || true
  docker run -d --name "$FRONT" --label "$label" \
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

# REMOVES WHAT THIS INVOCATION STARTED, and nothing else. The names are fixed,
# so "remove the containers called techsara-e2e-ci-*" is not the same question
# as "remove MY containers" on a host where more than one session runs this
# script. The ownership token under $STACK_TMP answers the second one.
#
# No token file means this shell started nothing, which is the normal state of
# the job's `if: always()` teardown when the run failed before `up`. It removes
# nothing and says so, rather than reaching for a peer's stack or exiting 1.
# STACK_FORCE=1 restores the unconditional by-name teardown, for a human who
# started a stack from a shell whose $STACK_TMP is gone.
down() {
  guard_names
  local token="" removed name
  [ -s "$STACK_ID_FILE" ] && token="$(tr -d '\r\n' < "$STACK_ID_FILE")"
  if [ "${STACK_FORCE:-0}" = "1" ]; then
    say "STACK_FORCE=1: removing $FRONT $ORCH $ENGINE and their volumes and network by NAME"
    docker rm -f "$FRONT" "$ORCH" "$ENGINE" >/dev/null 2>&1 || true
    docker volume rm "$DATA_VOL" "$REPORTS_VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  elif [ -n "$token" ]; then
    removed=""
    # Containers first: a network with an endpoint on it cannot be removed.
    for name in $(owned containers "$token"); do
      docker rm -f "$name" >/dev/null 2>&1 || true
      removed="$removed $name"
    done
    for name in $(owned volumes "$token"); do
      docker volume rm "$name" >/dev/null 2>&1 || true
      removed="$removed $name"
    done
    for name in $(owned networks "$token"); do
      docker network rm "$name" >/dev/null 2>&1 || true
      removed="$removed $name"
    done
    rm -f "$STACK_ID_FILE"
    say "removed what stack token $token owned:${removed:- nothing}"
  else
    say "no stack token in $STACK_TMP: this shell started nothing, so nothing was removed \
(STACK_FORCE=1 removes the fixed names unconditionally)"
  fi
  # The generated credentials go with the stack, not at the end of the job:
  # $RUNNER_TEMP is removed by the runner, but a file that held a session
  # signing key should not outlive the thing it signed for.
  rm -f "$ENV_FILE" "$STACK_TMP/e2e-ci-session.key" "$STACK_TMP/e2e-ci-pepper.key"
  say "removed the generated environment"
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
