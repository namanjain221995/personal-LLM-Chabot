#!/usr/bin/env bash
# Prove the schema layer survives the three things a deploy can ask of it:
# a FRESH install, an UPGRADE over live data, and a RESTORE of a dump.
#
#   scripts/deploy-db-rehearsal.sh [--provision] [--server DSN] [--keep]
#                                  [--image IMG] [--from-image IMG]
#                                  [--pg-image REF] [--expect-major N]
#                                  [--require-upgrade] [--json FILE]
#
# Nothing here touches production. Every database it creates is named
# test_rehearsal_*, it drops only what it created, and the migrations run out of
# a REAL orchestrator image rather than a copy of db.py — the code under test is
# the code that will ship.
#
# THE VERSION TRAP THIS SCRIPT REFUSES TO FALL INTO
#
# A rehearsal on the wrong PostgreSQL major proves nothing, and the wrong major
# is the easy mistake to make: the compose file pins postgres by sha256, which
# says nothing about its version, and the repository's general-purpose test
# server is a different major from production. So the deployed major is read
# from the RUNNING container (`postgres --version` inside it), the rehearsal
# server's major is read the same way, and a mismatch is a refusal rather than a
# footnote.
#
# `--provision` sidesteps the question entirely by starting a throwaway server
# from THE SAME IMAGE ID production is running — not the same tag, the same id —
# on a free port, and removing it at the end.
#
# RUNNING WHERE THERE IS NO DEPLOYED STACK (a hosted CI runner)
#
# A hosted runner has no production containers to read, so the two facts above
# have to be DECLARED: `--pg-image REF` names the PostgreSQL image the client
# tools and `--provision` use, and `--expect-major N` states the major the
# rehearsal server must report. Both are required together there — a runner
# that supplies only one is refused, because "some of production's identity"
# is the same guess this script exists to refuse.
#
# Where a deployed stack IS present the running containers remain the truth and
# the declared values are CHECKED against them: a `--pg-image` or
# `--expect-major` that disagrees with the box is a refusal, not an override.
# So the flags cannot be used to talk this script into a rehearsal against the
# wrong major on the one machine where that matters.
#
# WHAT EACH PHASE ACTUALLY PROVES
#
#   FRESH    An empty database reaches the latest version, with every migration
#            from 1..N recorded. Catches a migration that only works as a delta
#            against an already-populated schema.
#   UPGRADE  A database created by an OLDER orchestrator image, seeded with
#            users/conversations/messages through that image's own API, is
#            migrated forward by the CURRENT image. Asserts the version moved,
#            the seeded rows survived unchanged, and the migrations that were
#            already applied were NOT re-run (their applied_at must not move) —
#            which is the property `init_schema`'s "skip what is present" rule
#            is supposed to have.
#   RESTORE  A `pg_dump -Fc` of the upgraded database, restored into a fresh
#            database on a server of the SAME major, using a pg_restore of that
#            same major. Asserts the migration table and every seeded row came
#            back identical, and then that the current image treats the restored
#            database as already-migrated. That last step is the one that makes
#            it a rehearsal rather than a backup test: it proves the restore is a
#            database the application can actually be pointed at.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

PROVISION=0; KEEP=0
SERVER="${TEST_DATABASE_URL:-}"
IMAGE=""; FROM_IMAGE=""
# Declared identity, for a machine with no deployed stack. Env defaults so a
# CI job can set them once for every invocation instead of on every line.
DECLARED_PG_IMAGE="${DR_REHEARSAL_PG_IMAGE:-}"
DECLARED_MAJOR="${DR_REHEARSAL_EXPECT_MAJOR:-}"
# A skipped UPGRADE phase is a legitimate outcome on a laptop with no history
# and an ILLEGITIMATE one in a gate: "we could not test the upgrade" must not
# be reported in the same green as "the upgrade is safe".
REQUIRE_UPGRADE="${DR_REHEARSAL_REQUIRE_UPGRADE:-0}"
# Where to write the machine-readable outcome. It is written HERE, by the code
# that counted the assertions, so no caller has to re-derive pass/fail from an
# exit code and a log - and so a caller that forgets cannot invent one.
JSON_OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --provision) PROVISION=1 ;;
    --server) SERVER="${2:?--server needs a DSN}"; shift ;;
    --image) IMAGE="${2:?}"; shift ;;
    --from-image) FROM_IMAGE="${2:?}"; shift ;;
    --pg-image) DECLARED_PG_IMAGE="${2:?--pg-image needs an image reference}"; shift ;;
    --expect-major) DECLARED_MAJOR="${2:?--expect-major needs a PostgreSQL major}"; shift ;;
    --require-upgrade) REQUIRE_UPGRADE=1 ;;
    --json) JSON_OUT="${2:?--json needs a path}"; shift ;;
    --keep) KEEP=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) dr_die "unknown option: $1" ;;
  esac
  shift
done

dr_need docker; dr_need python3

STAMP="$(date -u +%Y%m%d%H%M%S)"
FRESH_DB="test_rehearsal_fresh_$STAMP"
UPGRADE_DB="test_rehearsal_upgrade_$STAMP"
RESTORE_DB="test_rehearsal_restore_$STAMP"
WORK="$(mktemp -d)"
PROVISIONED=""
PASS=0; FAIL=0
# Set before the trap so an early failure cannot make cleanup itself fail under
# `set -u` - a cleanup that crashes leaves the provisioned container running.
SRV_HOST=""; SRV_PORT=""; SRV_USER=""; SRV_PASS=""; SRV_DB=""

cleanup() {
  local rc=$?
  if [ "$KEEP" = 1 ]; then
    dr_say "--keep: leaving $FRESH_DB, $UPGRADE_DB, $RESTORE_DB and $WORK in place"
  else
    if [ -n "$SRV_HOST" ]; then
      for db in "$FRESH_DB" "$UPGRADE_DB" "$RESTORE_DB"; do
        psql_admin "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" >/dev/null 2>&1 || true
      done
    fi
    rm -rf "$WORK"
  fi
  if [ -n "$PROVISIONED" ] && [ "$KEEP" != 1 ]; then
    dr_say "removing the provisioned rehearsal server $PROVISIONED"
    docker rm -f "$PROVISIONED" >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap cleanup EXIT

ok()  { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }

# ------------------------------------------------- what production actually is
# A declared major has to be a bare positive integer. `--expect-major 18.4`,
# `--expect-major ""` and `--expect-major $(something-that-failed)` all have to
# fail HERE, loudly, rather than downstream as a string comparison against
# `SHOW server_version` that can never match and reads as a version mismatch.
if [ -n "$DECLARED_MAJOR" ]; then
  case "$DECLARED_MAJOR" in
    ''|*[!0-9]*) dr_die "--expect-major must be a whole PostgreSQL major such as 18, not '$DECLARED_MAJOR'" ;;
  esac
fi

DEPLOYED_MAJOR="$(dr_deployed_pg_major || true)"
PG_IMAGE="$(dr_container_image_id "$(dr_container_for postgres)")"

if [ -n "$DEPLOYED_MAJOR" ] && [ -n "$PG_IMAGE" ]; then
  # There is a stack here. It is the truth, and a declaration that disagrees
  # with it is a refusal: the whole point of this script is that the rehearsal
  # happens on the major and the image production is actually running.
  dr_say "deployed PostgreSQL: major $DEPLOYED_MAJOR, image $PG_IMAGE"
  if [ -n "$DECLARED_MAJOR" ] && [ "$DECLARED_MAJOR" != "$DEPLOYED_MAJOR" ]; then
    dr_die "--expect-major $DECLARED_MAJOR contradicts the deployed server, which reports major $DEPLOYED_MAJOR.
 Refusing: on a machine with a stack, the stack decides."
  fi
  if [ -n "$DECLARED_PG_IMAGE" ]; then
    declared_id="$(dr_image_id "$DECLARED_PG_IMAGE")"
    if [ -z "$declared_id" ]; then
      dr_warn "--pg-image $DECLARED_PG_IMAGE is not present here; using the deployed image id instead"
    elif [ "$declared_id" != "$PG_IMAGE" ]; then
      dr_die "--pg-image $DECLARED_PG_IMAGE resolves to $declared_id, but the deployed postgres container runs $PG_IMAGE.
 Refusing: on a machine with a stack, the stack decides."
    fi
  fi
elif [ -n "$DECLARED_MAJOR" ] && [ -n "$DECLARED_PG_IMAGE" ]; then
  # No stack here — a hosted runner. Both facts are declared, so both were
  # reviewed in the file that declares them.
  DEPLOYED_MAJOR="$DECLARED_MAJOR"
  PG_IMAGE="$DECLARED_PG_IMAGE"
  dr_say "no deployed stack here; rehearsing against the DECLARED PostgreSQL: major $DEPLOYED_MAJOR, image $PG_IMAGE"
elif [ -n "$DECLARED_MAJOR" ] || [ -n "$DECLARED_PG_IMAGE" ]; then
  dr_die "--pg-image and --expect-major have to be given TOGETHER. One without the other is
 half of production's identity, which is the guess this script exists to refuse."
else
  dr_die "cannot read the PostgreSQL version from $(dr_container_for postgres), and no
 --pg-image/--expect-major was declared. Refusing to rehearse against an unknown major."
fi

ORCH_IMAGE="${IMAGE:-$(dr_container_image_id "$(dr_container_for orchestrator)")}"
[ -n "$ORCH_IMAGE" ] || dr_die "no orchestrator image to test (pass --image)"
dr_say "orchestrator under test: $ORCH_IMAGE"

# ------------------------------------------------------------- the test server
if [ "$PROVISION" = 1 ]; then
  PORT=""
  for candidate in $(seq 55460 55499); do
    (echo >"/dev/tcp/127.0.0.1/$candidate") >/dev/null 2>&1 || { PORT="$candidate"; break; }
  done
  [ -n "$PORT" ] || dr_die "no free port in 55460-55499 to provision a rehearsal server on"
  PROVISIONED="techsara-dbrehearsal-$STAMP"
  dr_say "provisioning $PROVISIONED on 127.0.0.1:$PORT from the PRODUCTION image id"
  docker run -d --name "$PROVISIONED" \
    -e POSTGRES_USER=rehearsal -e POSTGRES_PASSWORD=rehearsal -e POSTGRES_DB=postgres \
    -e POSTGRES_INITDB_ARGS="--locale=C --encoding=UTF8" \
    -p "127.0.0.1:$PORT:5432" "$PG_IMAGE" >/dev/null \
    || dr_die "could not start the rehearsal server"
  for _ in $(seq 1 60); do
    docker exec "$PROVISIONED" pg_isready -U rehearsal -q 2>/dev/null && break
    sleep 1
  done
  SERVER="postgresql://rehearsal:rehearsal@127.0.0.1:$PORT/postgres"
fi

[ -n "$SERVER" ] || dr_die "no rehearsal server. Use --provision (recommended: it copies the production image id),
 or --server postgresql://user:pass@127.0.0.1:PORT/postgres, or set TEST_DATABASE_URL."

# Split the DSN once; every helper below reuses the pieces.
eval "$(DR_DSN="$SERVER" python3 - <<'PY'
import os, shlex
from urllib.parse import urlsplit, unquote
u = urlsplit(os.environ["DR_DSN"])
print("SRV_HOST=" + shlex.quote(u.hostname or "127.0.0.1"))
print("SRV_PORT=" + shlex.quote(str(u.port or 5432)))
print("SRV_USER=" + shlex.quote(unquote(u.username or "postgres")))
print("SRV_PASS=" + shlex.quote(unquote(u.password or "")))
print("SRV_DB=" + shlex.quote((u.path or "/postgres").lstrip("/") or "postgres"))
PY
)"

# psql/pg_dump/pg_restore always come from the PRODUCTION postgres image, so the
# client major can never be older than the server it is talking to. That is not
# theoretical: a pg_dump older than its server refuses outright.
pg_tool() {
  local tool="$1"; shift
  docker run --rm --network host -e PGPASSWORD="$SRV_PASS" \
    --entrypoint "$tool" "$PG_IMAGE" \
    -h "$SRV_HOST" -p "$SRV_PORT" -U "$SRV_USER" "$@"
}
psql_admin() { pg_tool psql -d "$SRV_DB" -v ON_ERROR_STOP=1 -tAc "$1"; }
psql_db()    { local db="$1"; shift; pg_tool psql -d "$db" -v ON_ERROR_STOP=1 -tAc "$1"; }

# `|| true` is load-bearing, and it took a test to notice. Under `set -e` plus
# `pipefail` an ASSIGNMENT whose command substitution fails takes the exit
# status of that substitution and ends the script THERE -- so an unreachable
# server, which is this script's most likely real failure and the only one a
# hosted runner is likely to produce, exited 2 with no diagnostic at all and the
# dr_die below never ran. Measured before the fix: exit 2, last line
# "orchestrator under test: ...", nothing else.
SERVER_MAJOR="$(psql_admin 'SHOW server_version' 2>/dev/null | sed -n 's/^\([0-9][0-9]*\).*/\1/p' || true)"
[ -n "$SERVER_MAJOR" ] || dr_die "cannot reach the rehearsal server at $SRV_HOST:$SRV_PORT as $SRV_USER"

dr_say "rehearsal server: $SRV_HOST:$SRV_PORT major $SERVER_MAJOR"
if [ "$SERVER_MAJOR" != "$DEPLOYED_MAJOR" ]; then
  dr_warn "REFUSING: the rehearsal server is PostgreSQL $SERVER_MAJOR, production is $DEPLOYED_MAJOR."
  dr_warn "A migration, a dump and a restore all behave differently across majors, so a"
  dr_warn "green run here would be evidence about a database this deploy will never meet."
  dr_warn "Re-run with --provision to get a server from production's own image id."
  dr_die "major version mismatch ($SERVER_MAJOR != $DEPLOYED_MAJOR); nothing was created"
fi
ok "rehearsal server major $SERVER_MAJOR matches the deployed major $DEPLOYED_MAJOR"

dsn_for() { printf 'postgresql://%s:%s@%s:%s/%s' "$SRV_USER" "$SRV_PASS" "$SRV_HOST" "$SRV_PORT" "$1"; }

# Run a snippet inside an orchestrator image, against one of the test databases.
in_image() {  # in_image <image> <database> <python>
  docker run --rm --network host -e APP_DATABASE_URL="$(dsn_for "$2")" \
    --entrypoint python "$1" -c "$3"
}

LATEST="$(dr_code_schema_version_from_image "$ORCH_IMAGE")" \
  || dr_die "cannot read the migration table out of $ORCH_IMAGE"
dr_say "the image under test knows migrations up to V$LATEST"

# ============================================================ 1. FRESH INSTALL
printf '\n== FRESH INSTALL ==\n'
psql_admin "CREATE DATABASE \"$FRESH_DB\"" >/dev/null
if in_image "$ORCH_IMAGE" "$FRESH_DB" 'from app.db import init_schema; init_schema()' >"$WORK/fresh.log" 2>&1; then
  ok "init_schema completed on an empty database"
else
  bad "init_schema failed on an empty database"; sed 's/^/      /' "$WORK/fresh.log"
fi
got="$(psql_db "$FRESH_DB" 'SELECT COALESCE(MAX(version),0) FROM schema_migrations' | tr -d '[:space:]')"
[ "$got" = "$LATEST" ] && ok "fresh database is at V$got" || bad "fresh database is at V$got, expected V$LATEST"
missing="$(psql_db "$FRESH_DB" "SELECT string_agg(g::text, ',') FROM generate_series(1, $LATEST) g
  WHERE g NOT IN (SELECT version FROM schema_migrations)" | tr -d '[:space:]')"
[ -z "$missing" ] && ok "every migration 1..$LATEST is recorded, with no gaps" \
                  || bad "migrations missing from schema_migrations: $missing"
# Re-running must be a no-op. A migration that is not idempotent shows up here
# and nowhere else until two orchestrators start at once in production.
before="$(psql_db "$FRESH_DB" "SELECT md5(string_agg(version::text||applied_at::text, ',' ORDER BY version)) FROM schema_migrations")"
if in_image "$ORCH_IMAGE" "$FRESH_DB" 'from app.db import init_schema; init_schema()' >>"$WORK/fresh.log" 2>&1; then
  after="$(psql_db "$FRESH_DB" "SELECT md5(string_agg(version::text||applied_at::text, ',' ORDER BY version)) FROM schema_migrations")"
  [ "$before" = "$after" ] && ok "a second init_schema changed nothing (idempotent)" \
                           || bad "a second init_schema rewrote schema_migrations"
else
  bad "a second init_schema on an already-migrated database failed"
fi

# ================================================================= 2. UPGRADE
printf '\n== UPGRADE (old image -> data -> new image) ==\n'
if [ -z "$FROM_IMAGE" ]; then
  # An older orchestrator image that is actually on this box, preferring the
  # newest one that is still behind the image under test. Real historical code
  # beats a hand-truncated migration list.
  FROM_IMAGE="$(
    for tag in $(docker images --format '{{.Repository}}:{{.Tag}}' \
                 | grep '^sf-local-ai-orchestrator:' | grep -v ':cuda$' | grep -v ':sha-'); do
      v="$(dr_code_schema_version_from_image "$tag" 2>/dev/null || true)"
      [ -n "$v" ] && [ "$v" -lt "$LATEST" ] && printf '%s %s\n' "$v" "$tag"
    done | sort -rn | head -1 | awk '{print $2}'
  )"
fi

if [ -z "$FROM_IMAGE" ] && [ "$REQUIRE_UPGRADE" = 1 ]; then
  # Deliberately a FAILURE and not a `dr_die`: the fresh-install phase above has
  # already produced real findings, and throwing them away would make a missing
  # baseline image indistinguishable from a broken migration.
  bad "no older orchestrator image to upgrade FROM, and --require-upgrade was given.
      The caller promised a baseline and did not supply one; refusing to report a
      green upgrade rehearsal that never ran."
elif [ -z "$FROM_IMAGE" ]; then
  dr_warn "no older orchestrator image is present, so the upgrade phase cannot use real"
  dr_warn "historical code. Skipping it rather than faking an old schema by deleting"
  dr_warn "rows from schema_migrations - that would test a state that never existed."
elif ! FROM_VERSION="$(dr_code_schema_version_from_image "$FROM_IMAGE")"; then
  # The SAME silent-exit trap as the SERVER_MAJOR line above, on the other
  # command substitution in this script that is allowed to fail. The baseline
  # image is built from an ARBITRARY historical commit, so "its /app/app/db.py
  # cannot be read" is an ordinary outcome -- a Dockerfile that has since been
  # renamed, a layout that moved, an image that is not an orchestrator at all.
  #
  # Measured today on this branch with `--from-image postgres:18-alpine` before
  # this guard existed: EXIT=1 whose last line was
  # "== UPGRADE (old image -> data -> new image) ==", with no ERROR line, no
  # SUMMARY and NO arm JSON written at all -- so `db_rehearsal.py verdict` could
  # only say "NOT PROVED `fresh-and-upgrade`: this arm reported nothing", which
  # is the wording reserved for a step that was deleted or never ran.
  #
  # `bad` and not `dr_die`, for the reason the branch above already gives: the
  # fresh-install assertions are real findings, the RESTORE phase falls back to
  # the fresh database on its own, and a verdict that says WHICH arm failed and
  # why beats one that says an arm was silent. The run is red either way.
  bad "cannot read the migration table out of the baseline image $FROM_IMAGE.
      No old schema means no upgrade to rehearse; refusing to report an upgrade
      arm that did not run. Is that image an orchestrator image?"
else
  dr_say "upgrading from $FROM_IMAGE (V$FROM_VERSION) to the image under test (V$LATEST)"
  psql_admin "CREATE DATABASE \"$UPGRADE_DB\"" >/dev/null

  if in_image "$FROM_IMAGE" "$UPGRADE_DB" 'from app.db import init_schema; init_schema()' >"$WORK/upgrade-old.log" 2>&1; then
    ok "the OLD image migrated an empty database to V$FROM_VERSION"
  else
    bad "the old image could not create its own schema"; sed 's/^/      /' "$WORK/upgrade-old.log"
  fi

  # Seeded through the OLD image's own API, so the rows are shaped the way that
  # version of the application actually wrote them.
  in_image "$FROM_IMAGE" "$UPGRADE_DB" '
import app.db as db
uid = db.create_user("rehearsal-user", "not-a-real-hash")
db.create_conversation(uid, "rehearsal-conv", "Rehearsal conversation")
for i in range(5):
    db.add_message(uid, "rehearsal-conv", "user" if i % 2 == 0 else "assistant", f"seeded message {i}")
print("seeded", uid)
' >"$WORK/seed.log" 2>&1 && ok "seeded users/conversations/messages through the old image" \
   || { bad "seeding through the old image failed"; sed 's/^/      /' "$WORK/seed.log"; }

  MSG_BEFORE="$(psql_db "$UPGRADE_DB" 'SELECT count(*) FROM messages' | tr -d '[:space:]')"
  DIGEST_BEFORE="$(psql_db "$UPGRADE_DB" "SELECT md5(string_agg(role||content, ',' ORDER BY id)) FROM messages")"
  APPLIED_BEFORE="$(psql_db "$UPGRADE_DB" "SELECT md5(string_agg(version::text||applied_at::text, ',' ORDER BY version)) FROM schema_migrations WHERE version <= $FROM_VERSION")"

  if in_image "$ORCH_IMAGE" "$UPGRADE_DB" 'from app.db import init_schema; init_schema()' >"$WORK/upgrade-new.log" 2>&1; then
    ok "the NEW image migrated V$FROM_VERSION -> V$LATEST over live data"
  else
    bad "the upgrade migration failed"; sed 's/^/      /' "$WORK/upgrade-new.log"
  fi

  got="$(psql_db "$UPGRADE_DB" 'SELECT COALESCE(MAX(version),0) FROM schema_migrations' | tr -d '[:space:]')"
  [ "$got" = "$LATEST" ] && ok "upgraded database is at V$got" || bad "upgraded database is at V$got, expected V$LATEST"

  MSG_AFTER="$(psql_db "$UPGRADE_DB" 'SELECT count(*) FROM messages' | tr -d '[:space:]')"
  DIGEST_AFTER="$(psql_db "$UPGRADE_DB" "SELECT md5(string_agg(role||content, ',' ORDER BY id)) FROM messages")"
  [ "$MSG_BEFORE" = "$MSG_AFTER" ] && ok "all $MSG_AFTER seeded messages survived the upgrade" \
                                   || bad "message count changed across the upgrade: $MSG_BEFORE -> $MSG_AFTER"
  [ "$DIGEST_BEFORE" = "$DIGEST_AFTER" ] && ok "seeded message contents are byte-identical after the upgrade" \
                                         || bad "seeded message contents changed across the upgrade"

  APPLIED_AFTER="$(psql_db "$UPGRADE_DB" "SELECT md5(string_agg(version::text||applied_at::text, ',' ORDER BY version)) FROM schema_migrations WHERE version <= $FROM_VERSION")"
  [ "$APPLIED_BEFORE" = "$APPLIED_AFTER" ] \
    && ok "migrations 1..$FROM_VERSION were skipped, not re-applied (applied_at unchanged)" \
    || bad "already-applied migrations were re-run by the upgrade"
fi

# ============================================== 3. VERSION-COMPATIBLE RESTORE
printf '\n== RESTORE REHEARSAL (same major, dump -> restore -> deployable) ==\n'
SOURCE_DB="$UPGRADE_DB"
psql_admin "SELECT 1 FROM pg_database WHERE datname = '$SOURCE_DB'" | grep -q 1 || SOURCE_DB="$FRESH_DB"
dr_say "dumping $SOURCE_DB with pg_dump from the production image (client major $DEPLOYED_MAJOR)"

if pg_tool pg_dump -d "$SOURCE_DB" -Fc --no-owner --no-privileges > "$WORK/rehearsal.pgdump" 2>"$WORK/dump.log"; then
  ok "pg_dump -Fc produced $(du -h "$WORK/rehearsal.pgdump" | cut -f1)"
else
  bad "pg_dump failed"; sed 's/^/      /' "$WORK/dump.log"
fi

psql_admin "CREATE DATABASE \"$RESTORE_DB\"" >/dev/null
# pg_restore reads the archive from stdin, so the file goes in through the
# container's stdin rather than a bind mount - no path juggling, and nothing
# from the host filesystem is exposed to the container.
if docker run --rm -i --network host -e PGPASSWORD="$SRV_PASS" --entrypoint pg_restore "$PG_IMAGE" \
      -h "$SRV_HOST" -p "$SRV_PORT" -U "$SRV_USER" -d "$RESTORE_DB" --no-owner --no-privileges \
      < "$WORK/rehearsal.pgdump" >"$WORK/restore.log" 2>&1; then
  ok "pg_restore into a fresh database on the same major"
else
  bad "pg_restore reported errors"; tail -15 "$WORK/restore.log" | sed 's/^/      /'
fi

src_mig="$(psql_db "$SOURCE_DB" "SELECT md5(string_agg(version::text, ',' ORDER BY version)) FROM schema_migrations")"
dst_mig="$(psql_db "$RESTORE_DB" "SELECT md5(string_agg(version::text, ',' ORDER BY version)) FROM schema_migrations")"
[ "$src_mig" = "$dst_mig" ] && ok "the restored database has the identical migration table" \
                            || bad "the restored migration table differs from the source"

for table in users conversations messages; do
  a="$(psql_db "$SOURCE_DB" "SELECT count(*) FROM $table" 2>/dev/null | tr -d '[:space:]')"
  b="$(psql_db "$RESTORE_DB" "SELECT count(*) FROM $table" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$a" ] && [ "$a" = "$b" ] && ok "$table: $a row(s) restored intact" \
                                 || bad "$table: source has ${a:-?}, restore has ${b:-?}"
done

# The step that turns a backup test into a RESTORE REHEARSAL: is the restored
# database something the application can actually be pointed at?
if in_image "$ORCH_IMAGE" "$RESTORE_DB" '
from app.db import init_schema, schema_version
before = schema_version()
init_schema()
after = schema_version()
print(f"{before} {after}")
assert before == after, f"the restored database was NOT already migrated: {before} -> {after}"
' >"$WORK/restore-check.log" 2>&1; then
  ok "the current image treats the restored database as already migrated ($(cat "$WORK/restore-check.log" | tr '\n' ' '))"
else
  bad "the current image did not accept the restored database as-is"; sed 's/^/      /' "$WORK/restore-check.log"
fi

printf '\n== SUMMARY ==\n'
printf '  server        : PostgreSQL %s (production is %s)\n' "$SERVER_MAJOR" "$DEPLOYED_MAJOR"
printf '  image tested  : %s (V%s)\n' "$ORCH_IMAGE" "$LATEST"
printf '  upgraded from : %s\n' "${FROM_IMAGE:-<skipped: no older image on this box>}"
printf '  passed        : %d\n  failed        : %d\n' "$PASS" "$FAIL"

# The machine-readable outcome, written by the code that counted. `latest` and
# `from_version` travel with it because the reversibility proof needs the number
# this image migrates TO, and reading it a second time out of the image would be
# a second chance to read it differently.
if [ -n "$JSON_OUT" ]; then
  if [ "$FAIL" -eq 0 ]; then DR_J_OUTCOME=pass; else DR_J_OUTCOME=fail; fi
  DR_J_OUTCOME="$DR_J_OUTCOME" \
  DR_J_PASS="$PASS" DR_J_FAIL="$FAIL" DR_J_LATEST="$LATEST" \
  DR_J_FROM="${FROM_IMAGE:-}" DR_J_FROM_VERSION="${FROM_VERSION:-}" \
  DR_J_IMAGE="$ORCH_IMAGE" DR_J_MAJOR="$SERVER_MAJOR" \
  python3 - "$JSON_OUT" <<'PYJSON'
import json, os, sys

frm = os.environ.get("DR_J_FROM") or ""
from_v = os.environ.get("DR_J_FROM_VERSION") or ""
latest = os.environ["DR_J_LATEST"]
if not frm:
    moved = "the upgrade arm did NOT run: no baseline image was supplied"
elif not from_v:
    moved = (
        "the upgrade arm did NOT run: the migration table could not be read out of "
        f"the baseline image {frm}, so there was no old schema to migrate forward"
    )
elif from_v == latest:
    moved = (
        f"the schema did not move in this commit: the baseline {frm} and the image under "
        f"test are both V{latest}, so the upgrade arm proved that nothing runs and nothing "
        "is lost, and NOT that a migration is safe"
    )
else:
    moved = f"upgraded from {frm} (V{from_v}) to V{latest}"
record = {
    "arm": "fresh-and-upgrade",
    "outcome": os.environ["DR_J_OUTCOME"],
    "detail": (
        f"{os.environ['DR_J_PASS']} assertions passed, {os.environ['DR_J_FAIL']} failed "
        f"on PostgreSQL major {os.environ['DR_J_MAJOR']}; {moved}"
    ),
    "image": os.environ["DR_J_IMAGE"],
    "from_image": frm,
    "from_version": from_v,
    "latest": latest,
    "passed": int(os.environ["DR_J_PASS"]),
    "failed": int(os.environ["DR_J_FAIL"]),
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(json.dumps(record, indent=2) + "\n")
PYJSON
fi

[ "$FAIL" -eq 0 ] || exit 1
