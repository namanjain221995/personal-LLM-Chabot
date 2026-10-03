#!/usr/bin/env bash
# Run the dev stack on the worker node, see ops/dev/README.md.
#
# Usage, from the worktree root, with the worker daemon named on the line:
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh up
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh status
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh logs [SERVICE]
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh smoke
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh seed USERNAME   [password on stdin]
#   DOCKER_HOST=ssh://WORKER ops/dev/devstack.sh down
#
# Every Compose call goes through compose_dev below, whose one line carries
# the literal global flags the autopilot guard renders and checks: the dev
# vars, the dev env file, the two compose files and project llmdev. Nothing
# here computes or exports DOCKER_HOST, changes directory, opens a shell on
# another host or starts a container outside the project.
#
# up     builds on the worker and waits up to 900 s for every healthcheck.
# down   stops and removes the containers and the network. It never removes
#        volumes or images, so the dev database survives a down and up.
# smoke  prints the overall and per-check status of the orchestrator health,
#        the HTTP status of the frontend login page and the cap counters.
# seed   creates or updates a super admin of the dev workspace. The password
#        is read from stdin and handed to the container on stdin, never argv.
#
# The autopilot guard parses this text and checks the words of column-0
# comments as if they were arguments, so these comments avoid quotes, shell
# operators and words that look like file names of credentials.
set -euo pipefail

die() {
  printf 'devstack: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'USAGE'
usage: DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh <command>

  up               build on the worker and start the stack, wait until healthy
  down             stop and remove the containers (volumes and images are kept)
  status           list the containers of project llmdev
  logs [service]   the last 200 log lines of one service, or of all
  smoke            orchestrator /health, frontend /login, inference-cap stats
  seed <username>  create or update a super admin; the password is read from stdin
Run from the worktree root, after ops/dev/init-env.sh.
USAGE
}

compose_dev() {
  docker compose --env-file ops/dev/stack.vars --env-file ops/dev/.env -f compose.yaml -f ops/dev/compose.dev.yaml -p llmdev "$@"
}

cmd="${1:-}"
if [ -z "$cmd" ] || [ "$cmd" = "-h" ] || [ "$cmd" = "--help" ] || [ "$cmd" = "help" ]; then
  usage
  [ -n "$cmd" ] || exit 2
  exit 0
fi
shift

if [ ! -f compose.yaml ] || [ ! -f ops/dev/compose.dev.yaml ] || [ ! -f ops/dev/stack.vars ]; then
  die "run from the worktree root (compose.yaml and ops/dev/ must be here)"
fi

case "${DOCKER_HOST:-}" in
  ssh://?*) ;;
  *) die "set DOCKER_HOST=ssh://<worker> on the command line; the dev stack runs only on the worker daemon" ;;
esac

# A shell variable outranks the dev vars file in Compose interpolation, and
# these would move the stack, its files or the set of services it starts.
# Refuse any value other than the dev one, even an empty one.
[ "${TECHSARA_STACK-llmdev}" = llmdev ] ||
  die "TECHSARA_STACK is set in the environment to another value; unset it"
[ "${TECHSARA_SECRET_ENV-ops/dev/.env}" = ops/dev/.env ] ||
  die "TECHSARA_SECRET_ENV is set in the environment to another value; unset it"
[ "${TECHSARA_GENERATED_ENV-ops/dev/.runtime/orchestrator.env}" = ops/dev/.runtime/orchestrator.env ] ||
  die "TECHSARA_GENERATED_ENV is set in the environment to another value; unset it"
[ "${TECHSARA_DEV_ENGINES_ENV-ops/dev/.runtime/engines.env}" = ops/dev/.runtime/engines.env ] ||
  die "TECHSARA_DEV_ENGINES_ENV is set in the environment to another value; unset it"
[ -z "${COMPOSE_PROFILES:-}" ] ||
  die "COMPOSE_PROFILES is set in the environment; the dev stack starts its default services only"

if [ ! -f ops/dev/.env ] || [ ! -f ops/dev/.runtime/orchestrator.env ] || [ ! -f ops/dev/.runtime/engines.env ]; then
  die "the env files of the dev stack are missing; run ops/dev/init-env.sh --main http://<head-address>:<port> first"
fi

case "$cmd" in
  up)
    [ "$#" -eq 0 ] || die "up takes no arguments"
    compose_dev up -d --build --wait --wait-timeout 900
    compose_dev ps
    ;;

  down)
    [ "$#" -eq 0 ] || die "down takes no arguments (volumes are never removed here)"
    compose_dev down
    ;;

  status)
    [ "$#" -eq 0 ] || die "status takes no arguments"
    compose_dev ps --all
    ;;

  logs)
    [ "$#" -le 1 ] || die "logs takes at most one service name"
    if [ "$#" -eq 1 ]; then
      case "$1" in
        postgres | orchestrator | frontend | inference-cap) ;;
        *) die "unknown service: $1 (postgres, orchestrator, frontend, inference-cap)" ;;
      esac
      compose_dev logs --no-color --tail 200 "$1"
    else
      compose_dev logs --no-color --tail 200
    fi
    ;;

  smoke)
    [ "$#" -eq 0 ] || die "smoke takes no arguments"
    rc=0
    # The orchestrator health document: overall status and each check status,
    # nothing else, because details can carry addresses and messages.
    compose_dev exec -T orchestrator python3 -c '
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=60) as response:
    payload = json.load(response)
print("orchestrator /health:", payload.get("status"))
for name, check in sorted((payload.get("checks") or {}).items()):
    status = check.get("status") if isinstance(check, dict) else check
    print("  %-28s %s" % (name, status))
sys.exit(0 if payload.get("status") in ("ok", "healthy", "degraded") else 1)
' || rc=1
    # The frontend login page: the HTTP status line only.
    if login="$(compose_dev exec -T frontend wget -S -O /dev/null http://127.0.0.1:3000/login 2>&1)"; then
      printf 'frontend /login: %s\n' "$(printf '%s\n' "$login" | grep -m1 'HTTP/' | sed 's/^ *//')"
    else
      printf 'frontend /login: FAILED %s\n' "$(printf '%s\n' "$login" | grep -m1 'HTTP/' | sed 's/^ *//')"
      rc=1
    fi
    # The inference cap: its own counters (in flight, limit, totals).
    compose_dev exec -T inference-cap python3 -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:9100/_cap/stats", timeout=10) as response:
    print("inference-cap /_cap/stats:", json.dumps(json.load(response), sort_keys=True))
' || rc=1
    exit "$rc"
    ;;

  seed)
    [ "$#" -eq 1 ] || die "usage: ops/dev/devstack.sh seed <username>   (password on stdin)"
    name="$1"
    case "$name" in
      *[!A-Za-z0-9._@+-]* | "" | -*) die "seed: the username may use letters, digits and . _ @ + - only" ;;
    esac
    # The password arrives on the stdin of this script and leaves on the
    # stdin of the container process: never in argv, never in the environment.
    pw=""
    if [ -t 0 ]; then
      IFS= read -r -s -p "password for $name: " pw || true
      printf '\n' >&2
    else
      IFS= read -r pw || true
    fi
    [ -n "$pw" ] || die "seed: no password on stdin; pipe it in or type it, never pass it as an argument"
    program=""
    IFS= read -r -d '' program <<'PYSEED' || true
import sys
from app.authn.bootstrap import bootstrap_super_admin
name = sys.argv[1]
pw = sys.stdin.readline().rstrip("\n")
if not pw:
    raise SystemExit("seed: no password arrived on stdin")
email = name if "@" in name else name + "@dev.test"
summary = bootstrap_super_admin(email=email, name=name, password=pw, adopt_legacy=False)
print("seed: %s is a super admin of %s (user %s); log in with that e-mail"
      % (summary["email"], summary["workspace"], summary["user_id"]))
PYSEED
    printf '%s\n' "$pw" | compose_dev exec -T orchestrator python3 -c "$program" "$name"
    ;;

  *)
    usage >&2
    die "unknown command: $cmd"
    ;;
esac
