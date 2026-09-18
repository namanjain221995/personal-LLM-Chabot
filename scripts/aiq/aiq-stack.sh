#!/usr/bin/env bash
#
# aiq-stack.sh — a private, isolated copy of the orchestrator for the QA
# acceptance harness (scripts/aiq/run.py). Derived from scripts/e2e-stack.sh
# (same isolation rules), with four differences:
#
#   * it runs the image PRODUCTION runs (pinned by image id at `up`), or a
#     CANDIDATE built from that image plus one branch's orchestrator/app — the
#     baseline must score what users get today, and the main checkout is the
#     deploy root (read only);
#   * its own project name / port / database server (techsara-e2e-aiq, :8082),
#     so it never collides with another session's techsara-e2e stack;
#   * no frontend (the harness talks to the orchestrator API the way the
#     browser's /api proxy does), memory and CPU caps on both containers, and
#     every secret and env file under $AIQ_RUNTIME, OUTSIDE the repository;
#   * it runs on the WORKER Spark (spark-476e) and refuses to run on the head
#     (spark-0e68): the head is at ~96/121 GB and nothing new may take its
#     memory (owner, 2026-09-16).
#
# It shares only the model engines (read-only inference) and never starts,
# stops or edits a production container, volume or database.
#
# ENGINES. The main and vision models are the head's vLLM at
# http://10.100.184.1:8000/v1, reachable from the worker. The router, embed
# and reranker are published only on the head's loopback, so they arrive
# through ONE reverse ssh tunnel started ON THE HEAD (`aiq-stack.sh tunnel`
# prints the command). The container therefore runs with host networking and
# binds 127.0.0.1:8082 itself, so 127.0.0.1 inside it is the worker's loopback
# where the tunnel lands. Without the tunnel, run with AIQ_NO_ROUTER=1 — but
# then BOTH the baseline and the candidate must run that way. SearXNG is
# unreachable either way, which is what the F07 search-fallback case wants.
#
# USAGE
#   aiq-stack.sh export-env > prod.env   # ON THE HEAD, read-only; copy to the worker
#   aiq-stack.sh ship-image              # ON THE HEAD, docker save | ssh docker load
#   aiq-stack.sh tunnel                  # prints the ssh command to run ON THE HEAD
#   aiq-stack.sh candidate <git-sha>     # build the candidate image from a dev sha
#   aiq-stack.sh up | seed [name] | status | logs [n] | down | purge
#   aiq-stack.sh run [run.py args…]      # up + seed + run + down
#   aiq-stack.sh all                     # the full suite with the gate, then down
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${AIQ_REPO:-$(cd "$HERE/../.." && pwd)}"
HEAD_HOST="${AIQ_HEAD_HOST:-spark-0e68}"
WORKER_HOST="${AIQ_WORKER_HOST:-spark-476e}"
HEAD_ADDR="${AIQ_HEAD_ADDR:-10.100.184.1}"
WORKER_ADDR="${AIQ_WORKER_ADDR:-10.100.184.2}"
RUNTIME="${AIQ_RUNTIME:-$HOME/.aiq-runtime}"
PROJECT="${AIQ_PROJECT:-techsara-e2e-aiq}"
case "$PROJECT" in *e2e*) ;; *) echo "project must contain 'e2e'" >&2; exit 2 ;; esac
ORCH="${PROJECT}-orchestrator"
DB="techsara_e2e_aiq"
DATA_VOL="${PROJECT}_data"
REPORTS_VOL="${PROJECT}_reports"
ORCH_PORT="${AIQ_ORCH_PORT:-8082}"
SRC_IMAGE="${AIQ_IMAGE:-sf-local-ai-orchestrator:cuda}"
PROD_ORCH="sf-local-ai-orchestrator-1"
PG="${PROJECT}-postgres"
PG_VOL="${PROJECT}_pgdata"
PG_USER="techsara_e2e"
PG_PORT="${AIQ_PG_PORT:-55433}"
PG_SECRET="$RUNTIME/pg.password"
ENV_FILE="$RUNTIME/orch.env"
PROD_ENV="${AIQ_PROD_ENV:-$RUNTIME/prod.env}"
PY="${AIQ_PYTHON:-$REPO/orchestrator/.venv/bin/python}"

say() { printf '[aiq-stack] %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

# Names, ports and the host itself. Called before anything is created.
guard() {
  case "$ORCH$DATA_VOL$REPORTS_VOL$PG$PG_VOL" in
    *sf-local-ai*) die "refusing: a target name looks like production" ;;
  esac
  [ "$ORCH_PORT" != "8080" ] && [ "$ORCH_PORT" != "8081" ] || die "refusing to bind 8080/8081"
  [ "$PG_PORT" != "5432" ] || die "refusing to bind the production database port"
  local here; here="$(hostname)"
  [ "$here" != "$HEAD_HOST" ] || die "this is the head ($HEAD_HOST): it is at ~96/121 GB and the QA stack must not take its memory. Run this on $WORKER_HOST."
  [ "$here" = "$WORKER_HOST" ] || die "the QA stack runs on $WORKER_HOST only (this is '$here'); set AIQ_WORKER_HOST to move it deliberately"
  case "$RUNTIME" in "$REPO"|"$REPO"/*) die "AIQ_RUNTIME ($RUNTIME) must live OUTSIDE the repository" ;; esac
}

pg_image() { awk '/^  postgres:/{f=1} f && /image:/{print $2; exit}' "$REPO/compose.yaml"; }
pg_psql() { docker exec "$PG" psql -h 127.0.0.1 -U "$PG_USER" -d "$DB" -Atc "$1"; }

# ------------------------------------------------------------ head-side --

export_env() { # ON THE HEAD: the production environment, minus its paths and secrets-by-path
  [ "$(hostname)" = "$HEAD_HOST" ] || die "export-env reads the production container, which runs on $HEAD_HOST"
  docker inspect "$PROD_ORCH" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -vE '^(APP_DATABASE_URL|WORKSPACE_DIR|VIDEO_DATA_DIR|LANCEDB_DIR|LANCEDB_WEB_DIR|LANCEDB_VIDEO_DIR|REPORTS_DIR|PATH|HOSTNAME|HOME)='
}

ship_image() { # ON THE HEAD: send the production image to the worker, once
  [ "$(hostname)" = "$HEAD_HOST" ] || die "ship-image sends the head's image to the worker"
  local id; id="$(docker image inspect "$SRC_IMAGE" --format '{{.Id}}')"
  say "sending $SRC_IMAGE ($id) to $WORKER_ADDR — several GB, once"
  docker save "$SRC_IMAGE" | ssh "techsphere@$WORKER_ADDR" docker load
}

tunnel() { # printed, not run: it belongs to the head and must be stopped after the run
  cat <<EOF
Run this ON THE HEAD ($HEAD_HOST) for the duration of a run, then stop it:

  ssh -N -T \\
      -R 8002:127.0.0.1:8002 \\
      -R 8003:127.0.0.1:8003 \\
      -R 8005:127.0.0.1:8005 \\
      techsphere@$WORKER_ADDR

One ssh client process, a few MB, no container. It forwards the router (8002),
the embedder (8003) and the reranker (8005) to the worker's loopback, which is
where the QA container looks for them (host networking).

Without it: AIQ_NO_ROUTER=1 aiq-stack.sh up — and then the baseline must be
re-measured the same way, because a run without the router is a different
configuration, not a worse one.
EOF
}

# ------------------------------------------------------------ the stack --

db_up() {
  mkdir -p "$RUNTIME"; chmod 700 "$RUNTIME"
  [ -s "$PG_SECRET" ] || ( umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$PG_SECRET" )
  docker volume create "$PG_VOL" >/dev/null
  if ! docker ps --format '{{.Names}}' | grep -qx "$PG"; then
    docker rm -f "$PG" >/dev/null 2>&1 || true
    local image; image="$(pg_image)"; [ -n "$image" ] || die "no postgres image in compose.yaml"
    say "starting $PG on 127.0.0.1:$PG_PORT"
    # Host networking for the orchestrator means the database is reached over
    # the loopback too, on its own port; fsync off because it is throwaway.
    docker run -d --name "$PG" --memory=1g --cpus=2 \
      -p "127.0.0.1:$PG_PORT:5432" \
      -e POSTGRES_DB="$DB" -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$(cat "$PG_SECRET")" \
      -e POSTGRES_INITDB_ARGS="--locale=C --encoding=UTF8" -e PGDATA=/var/lib/postgresql/18/docker \
      -v "$PG_VOL:/var/lib/postgresql" --restart no "$image" \
      postgres -c shared_buffers=256MB -c max_connections=60 -c fsync=off -c synchronous_commit=off \
        -c full_page_writes=off -c shared_preload_libraries=pg_stat_statements >/dev/null
  fi
  local i
  for i in $(seq 1 90); do [ "$(pg_psql 'SELECT 1' 2>/dev/null)" = 1 ] && break; sleep 1; done
  [ "$(pg_psql 'SELECT 1' 2>/dev/null)" = 1 ] || die "database did not become ready"
}

write_env() {
  [ -s "$PROD_ENV" ] || die "no production environment at $PROD_ENV — run 'aiq-stack.sh export-env' on $HEAD_HOST and copy it here"
  ( umask 077; : > "$ENV_FILE" )
  grep -vE '^(OPENAI_BASE_URL|VISION_BASE_URL|ROUTER_BASE_URL|EMBED_BASE_URL|RERANK_BASE_URL|SEARXNG_BASE_URL|APP_DATABASE_URL|WORKSPACE_DIR|VIDEO_DATA_DIR|LANCEDB_DIR|LANCEDB_WEB_DIR|LANCEDB_VIDEO_DIR|REPORTS_DIR|PATH|HOSTNAME|HOME)=' "$PROD_ENV" >> "$ENV_FILE"
  {
    printf 'OPENAI_BASE_URL=http://%s:8000/v1\n' "$HEAD_ADDR"
    printf 'VISION_BASE_URL=http://%s:8000/v1\n' "$HEAD_ADDR"
    printf 'APP_DATABASE_URL=postgresql://%s:%s@127.0.0.1:%s/%s\n' "$PG_USER" "$(cat "$PG_SECRET")" "$PG_PORT" "$DB"
    printf 'WORKSPACE_DIR=/data/workspace\nVIDEO_DATA_DIR=/data/video\n'
    printf 'LANCEDB_DIR=/data/lancedb\nLANCEDB_WEB_DIR=/data/lancedb-web\nLANCEDB_VIDEO_DIR=/data/lancedb-video\n'
    printf 'REPORTS_DIR=/reports\n'
  } >> "$ENV_FILE"
  if [ "${AIQ_NO_ROUTER:-0}" = "1" ]; then
    say "no router / embed / reranker (AIQ_NO_ROUTER=1) — the baseline must match"
  else
    {
      printf 'ROUTER_BASE_URL=http://127.0.0.1:8002/v1\n'
      printf 'EMBED_BASE_URL=http://127.0.0.1:8003/v1\n'
      printf 'RERANK_BASE_URL=http://127.0.0.1:8005\n'
    } >> "$ENV_FILE"
  fi
}

up() {
  guard
  local image_id; image_id="$(docker image inspect "$SRC_IMAGE" --format '{{.Id}}')" \
    || die "image $SRC_IMAGE is not on this host — run 'aiq-stack.sh ship-image' on $HEAD_HOST"
  say "image $SRC_IMAGE = $image_id"
  mkdir -p "$RUNTIME"; chmod 700 "$RUNTIME"
  echo "$image_id" > "$RUNTIME/image.id"
  db_up
  docker volume create "$DATA_VOL" >/dev/null; docker volume create "$REPORTS_VOL" >/dev/null
  write_env
  docker rm -f "$ORCH" >/dev/null 2>&1 || true
  say "starting $ORCH on 127.0.0.1:$ORCH_PORT (host networking, for the head's tunnel)"
  # Host networking, but the server binds the LOOPBACK on its own port: the
  # container can reach the tunnelled router without the stack ever listening
  # on this machine's network, or on 8080.
  docker run -d --name "$ORCH" --memory=6g --cpus=4 \
    --network host --env-file "$ENV_FILE" \
    -v "$DATA_VOL:/data" -v "$REPORTS_VOL:/reports" \
    -v "$REPO/brain/packs:/data/brain:ro" \
    --restart no "$image_id" \
    uvicorn app.main:app --host 127.0.0.1 --port "$ORCH_PORT" --timeout-graceful-shutdown 90 >/dev/null
  local i
  for i in $(seq 1 180); do curl -fsS -m 15 "http://127.0.0.1:$ORCH_PORT/health" >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS -m 15 "http://127.0.0.1:$ORCH_PORT/health" >/dev/null 2>&1 || die "orchestrator not healthy — docker logs $ORCH"
  say "up: http://127.0.0.1:$ORCH_PORT"
}

candidate() { # candidate <git-sha> — the production image plus that commit's orchestrator/app
  guard
  local sha="${1:?usage: aiq-stack.sh candidate <git-sha>}"
  local base; base="$(docker image inspect "$SRC_IMAGE" --format '{{.Id}}')"
  local ctx; ctx="$(mktemp -d "$RUNTIME/candidate.XXXXXX")"
  trap 'rm -rf "$ctx"' RETURN
  # The worker may not hold a checkout: then pass a tar made on the head with
  #   git archive <sha> orchestrator/app > app.tar
  if [ -n "${AIQ_APP_TAR:-}" ]; then
    tar -x -C "$ctx" --strip-components=1 -f "$AIQ_APP_TAR"
  else
    git -C "$REPO" archive "$sha" orchestrator/app | tar -x -C "$ctx" --strip-components=1
  fi
  [ -d "$ctx/app" ] || die "no orchestrator/app in $sha (set AIQ_APP_TAR when this host has no checkout)"
  printf 'FROM %s\nCOPY app /app/app\n' "$base" > "$ctx/Dockerfile"
  local tag="aiq-candidate:${sha:0:12}"
  say "building $tag from $SRC_IMAGE + orchestrator/app at $sha"
  docker build -q -t "$tag" "$ctx" >/dev/null
  # A candidate is only comparable while its installed packages are identical.
  local a b
  a="$(docker run --rm --entrypoint python3 "$base" -m pip freeze 2>/dev/null | sort)"
  b="$(docker run --rm --entrypoint python3 "$tag" -m pip freeze 2>/dev/null | sort)"
  if [ "$a" != "$b" ]; then
    diff <(printf '%s\n' "$a") <(printf '%s\n' "$b") || true
    die "the candidate's packages differ from production's: it is not comparable with the baseline"
  fi
  printf '%s\n' "$a" | grep -i '^matplotlib==' || die "matplotlib is missing from the image"
  say "candidate ready: $tag (pip freeze identical to production)"
  echo "$tag"
}

seed() { # seed <username> — writes the password to $RUNTIME/user.password
  local name="${1:-aiq-eval}"
  mkdir -p "$RUNTIME"; chmod 700 "$RUNTIME"
  [ -s "$RUNTIME/user.password" ] || ( umask 077; head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$RUNTIME/user.password" )
  docker exec -i "$ORCH" python3 - "$name" "$(cat "$RUNTIME/user.password")" <<'PYSEED'
import sys
from app import db
from app.authn import passwords, store
from app.config import settings
name, pw = sys.argv[1], sys.argv[2]
row = db.get_user_by_username(name)
if row is None:
    try:
        db.create_user(name, "!e2e")
    except db.IntegrityError:
        pass
    row = db.get_user_by_username(name)
uid = int(row["id"])
store.set_credentials(uid, password_hash=passwords.hash_password(pw), email=f"{name}@test.local", display_name=name)
ws = store.ensure_workspace(settings.workspace_name)["id"]
store.upsert_membership(ws, uid, "member")
store.set_member_feature_overrides(ws, uid, {"attachments": True})
print(f"user {uid} {name}@test.local ready")
PYSEED
}

run_suite() { # run [run.py args…] — up, seed, run, tear down whatever happens
  up
  seed aiq-eval
  local rc=0
  AIQ_PASSWORD="$(cat "$RUNTIME/user.password")" AIQ_RUNTIME="$RUNTIME" \
    "$PY" "$HERE/run.py" --base "http://127.0.0.1:$ORCH_PORT" --container "$ORCH" "$@" || rc=$?
  down
  return "$rc"
}

down() {
  guard
  docker rm -f "$ORCH" >/dev/null 2>&1 || true
  docker stop -t 30 "$PG" >/dev/null 2>&1 || true
  docker rm -f "$PG" >/dev/null 2>&1 || true
  say "containers removed (volumes kept; 'purge' drops them)"
}

purge() {
  down
  docker volume rm "$DATA_VOL" "$REPORTS_VOL" "$PG_VOL" >/dev/null 2>&1 || true
  say "purged"
}

status() {
  docker ps --filter "name=$PROJECT" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
  curl -fsS -m 20 "http://127.0.0.1:$ORCH_PORT/health" 2>/dev/null | head -c 300 || echo "orchestrator not answering"; echo
}

case "${1:-status}" in
  up) up ;; down) down ;; purge) purge ;; seed) shift; seed "$@" ;; status) status ;;
  logs) docker logs "$ORCH" --tail "${2:-80}" ;;
  export-env) export_env ;;
  ship-image) ship_image ;;
  tunnel) tunnel ;;
  candidate) shift; candidate "$@" ;;
  run) shift; run_suite "$@" ;;
  all) shift; run_suite --workers "${AIQ_WORKERS:-2}" --gate --baseline "${AIQ_BASELINE:-$HERE/runs/baseline-20260917}" "$@" ;;
  *) die "usage: $0 up|seed [name]|run [args]|all|status|logs [n]|down|purge|candidate <sha>|export-env|ship-image|tunnel" ;;
esac
