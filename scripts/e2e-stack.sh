#!/usr/bin/env bash
#
# e2e-stack.sh — an ISOLATED copy of the application tier for end-to-end
# verification of a branch, on the same box as production, touching none of it.
#
# WHAT IS ISOLATED, AND WHY EACH ONE MATTERS
#   database   techsara_e2e_test          a QA run truncates and writes freely
#   /data      volume techsara-e2e_data    uploads, video analyses, LanceDB
#   /reports   volume techsara-e2e_reports transcript artifacts
#   ports      127.0.0.1:8081 / :3001      loopback only, never the LAN
#   images     …:e2e tags built from the branch, never :cuda / :portable
#
# WHAT IS SHARED, DELIBERATELY: the model engines (main vLLM, router, embed,
# reranker, whisper, OCR). They are the expensive, stateless half, they hold
# no per-user state, and a QA run that used mocks instead would prove nothing
# about the real media pipeline. The load is small and the video job paces
# itself against live chat, so a fixture analysis costs production a few
# seconds of decode, not an outage.
#
# WHAT THIS SCRIPT WILL NOT DO: touch a production container, volume, port or
# database; write to .env; or deploy. `down` removes only what `up` created.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="techsara-e2e"
ORCH="${PROJECT}-orchestrator"
FRONT="${PROJECT}-frontend"
DB="techsara_e2e_test"
DATA_VOL="${PROJECT}_data"
REPORTS_VOL="${PROJECT}_reports"
ORCH_PORT="${E2E_ORCH_PORT:-8081}"
FRONT_PORT="${E2E_FRONT_PORT:-3001}"
ORCH_IMAGE="sf-local-ai-orchestrator:e2e"
FRONT_IMAGE="sf-local-ai-frontend:e2e"
PROD_ORCH="sf-local-ai-orchestrator-1"
PG="sf-local-ai-postgres-1"

say() { printf '\033[34m[e2e]\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 2; }

env_get() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-; }

guard_not_production() {
  # Belt and braces: every name this script touches must carry the project
  # prefix. A typo that pointed any of this at sf-local-ai-* would take
  # production down, so the check is on the values, not on good intentions.
  case "$ORCH$FRONT$DATA_VOL$REPORTS_VOL" in
    *sf-local-ai*) die "refusing to run: a target name looks like production" ;;
  esac
  [ "$DB" != "$(env_get POSTGRES_DB)" ] || die "refusing to run: test DB equals the production database"
  [ "$ORCH_PORT" != "8080" ] && [ "$FRONT_PORT" != "3000" ] || die "refusing to bind a production port"
}

build() {
  say "building $ORCH_IMAGE from the working tree"
  # The `# syntax=` first line makes BuildKit fetch a frontend image from
  # Docker Hub, whose DNS is intermittent on this box; stripping it uses
  # BuildKit's built-in frontend and every real layer is cached locally.
  tail -n +2 "$ROOT/orchestrator/Dockerfile.cuda" > "$ROOT/.runtime/Dockerfile.e2e.nosyntax"
  docker build --pull=false -f "$ROOT/.runtime/Dockerfile.e2e.nosyntax" -t "$ORCH_IMAGE" "$ROOT/orchestrator" >/dev/null
  say "building $FRONT_IMAGE from the working tree"
  docker build --pull=false -f "$ROOT/frontend/Dockerfile" -t "$FRONT_IMAGE" "$ROOT/frontend" >/dev/null
  say "images built"
}

db_up() {
  local user; user="$(env_get POSTGRES_USER)"
  docker exec "$PG" psql -U "$user" -d postgres -Atc \
    "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 \
    || docker exec "$PG" psql -U "$user" -d postgres -Atc "CREATE DATABASE $DB" >/dev/null
  say "database $DB ready (schema is created by the orchestrator's own migrations at startup)"
}

up() {
  guard_not_production
  [ "${E2E_SKIP_BUILD:-0}" = "1" ] || build
  db_up
  docker volume create "$DATA_VOL" >/dev/null
  docker volume create "$REPORTS_VOL" >/dev/null
  rm -f "$ROOT/.runtime/e2e.env"
  touch "$ROOT/.runtime/e2e.env"; chmod 600 "$ROOT/.runtime/e2e.env"

  # The production container's environment IS the contract with the model
  # engines (base URLs, model ids, capability flags, secrets). Copying it and
  # then overriding only what must differ keeps this stack honest: it is the
  # same application, not a lookalike with different settings.
  docker inspect "$PROD_ORCH" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -vE '^(APP_DATABASE_URL|WORKSPACE_DIR|VIDEO_DATA_DIR|LANCEDB_DIR|LANCEDB_WEB_DIR|LANCEDB_VIDEO_DIR|REPORTS_DIR|PATH|HOSTNAME|HOME)=' \
    >> "$ROOT/.runtime/e2e.env"
  local user pass
  user="$(env_get POSTGRES_USER)"; pass="$(env_get POSTGRES_PASSWORD)"
  {
    printf 'APP_DATABASE_URL=postgresql://%s:%s@postgres:5432/%s\n' "$user" "$pass" "$DB"
    printf 'WORKSPACE_DIR=/data/workspace\nVIDEO_DATA_DIR=/data/video\n'
    printf 'LANCEDB_DIR=/data/lancedb\nLANCEDB_WEB_DIR=/data/lancedb-web\nLANCEDB_VIDEO_DIR=/data/lancedb-video\n'
    printf 'REPORTS_DIR=/reports\n'
  } >> "$ROOT/.runtime/e2e.env"

  docker rm -f "$ORCH" "$FRONT" >/dev/null 2>&1 || true
  say "starting $ORCH on 127.0.0.1:$ORCH_PORT"
  docker run -d --name "$ORCH" \
    --network sf-local-ai_application \
    --env-file "$ROOT/.runtime/e2e.env" \
    -v "$DATA_VOL:/data" -v "$REPORTS_VOL:/reports" \
    -v "$ROOT/brain/packs:/data/brain:ro" \
    -v "$HOME/Documents/project/Model:/models:ro" \
    --add-host host.docker.internal:host-gateway \
    --add-host vllm:host-gateway \
    -p "127.0.0.1:$ORCH_PORT:8080" \
    --restart no "$ORCH_IMAGE" >/dev/null
  docker network connect sf-local-ai_inference "$ORCH" 2>/dev/null || true

  say "starting $FRONT on 127.0.0.1:$FRONT_PORT"
  docker run -d --name "$FRONT" \
    --network sf-local-ai_application \
    -e "ORCHESTRATOR_URL=http://$ORCH:8080" \
    -e "NEXT_PUBLIC_APP_NAME=TechSara AI (e2e)" \
    -p "127.0.0.1:$FRONT_PORT:3000" \
    --init --restart no "$FRONT_IMAGE" >/dev/null

  local i
  # Generous: a first start runs every migration and probes six engines
  # before it answers, and a cold page cache makes that slower still.
  for i in $(seq 1 180); do
    curl -fsS -m 3 "http://127.0.0.1:$ORCH_PORT/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS -m 3 "http://127.0.0.1:$ORCH_PORT/health" >/dev/null 2>&1 \
    || die "orchestrator did not become healthy — docker logs $ORCH"
  for i in $(seq 1 60); do
    curl -fsS -m 3 "http://127.0.0.1:$FRONT_PORT/login" >/dev/null 2>&1 && break
    sleep 2
  done
  say "up:  orchestrator http://127.0.0.1:$ORCH_PORT   frontend http://127.0.0.1:$FRONT_PORT"
  say "schema: $(docker exec "$ORCH" python3 -c 'from app import db; print(db.LATEST_SCHEMA_VERSION)' 2>/dev/null || echo '?')"
}

seed() { # seed <username> <password> — a member with attachments + video on
  local name="${1:?username}" pw="${2:?password}"
  docker exec -i "$ORCH" python3 - "$name" "$pw" <<'PYSEED'
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
store.set_credentials(uid, password_hash=passwords.hash_password(pw),
                      email=f"{name}@test.local", display_name=name)
workspace = store.ensure_workspace(settings.workspace_name)
ws = workspace["id"]  # a text id, not an integer
store.upsert_membership(ws, uid, "member")
# The two features an MP4 needs: the upload rail and the video engine.
store.set_member_feature_overrides(ws, uid, {"attachments": True, "video_analysis": True})
print(f"user {uid} {name}@test.local ready")
PYSEED
}

down() {
  guard_not_production
  docker rm -f "$ORCH" "$FRONT" >/dev/null 2>&1 || true
  say "containers removed (volumes $DATA_VOL/$REPORTS_VOL and database $DB kept — use 'purge' to drop them)"
}

purge() {
  guard_not_production
  down
  docker volume rm "$DATA_VOL" "$REPORTS_VOL" >/dev/null 2>&1 || true
  docker exec "$PG" psql -U "$(env_get POSTGRES_USER)" -d postgres -Atc "DROP DATABASE IF EXISTS $DB" >/dev/null
  say "purged"
}

status() {
  docker ps --filter "name=$PROJECT" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
  # 20 s, not 3: /health probes six model engines and answers in about three
  # seconds when they are idle, longer while a video is being analysed. A
  # tight timeout here reported a working stack as a dead one.
  curl -fsS -m 20 "http://127.0.0.1:$ORCH_PORT/health" 2>/dev/null | head -c 200 || echo "orchestrator not answering"
  echo
}

case "${1:-status}" in
  up) up ;;
  down) down ;;
  purge) purge ;;
  build) build ;;
  seed) shift; seed "$@" ;;
  status) status ;;
  logs) shift; docker logs "${1:-$ORCH}" --tail "${2:-60}" ;;
  *) die "usage: $0 up|down|purge|build|seed <email> <password>|status|logs [container] [lines]" ;;
esac
