#!/usr/bin/env bash
# Back up the knowledge stores of the running stack into backups/<stamp>/:
#
#   postgres.dump   pg_dump -Fc of the app database (conversations, messages,
#                   web_pages, web_claims, users, ... — the SOURCE OF TRUTH)
#   lancedb.tar     /data/lancedb (Salesforce chunks) and /data/lancedb-web
#                   (web chunks) from the data volume — DERIVED state, kept so a
#                   restore does not have to re-embed for hours
#   sidecars/       each directory's _techsara_embedding_index.json, verbatim
#   manifest.json   row counts (PostgreSQL and LanceDB), sizes, checksums and
#                   the exact restore commands
#
#   scripts/backup-knowledge.sh [--stamp STAMP] [--dest DIR] [--max-bytes N]
#                               [--dry-run]
#
# Order of operations, and why:
#   1. Size gate. The projected total (what backups/ already holds + the live
#      database size + the LanceDB directories) must stay under --max-bytes,
#      default 20 GiB. Measured 2026-09-03: PostgreSQL 87 MB, /data/lancedb
#      2.37 GB (137k Lance versions holding ~444 MB of live data),
#      /data/lancedb-web 66 MB — about 2.5 GB per run, so the cap holds eight
#      backups before the operator has to prune. The script NEVER deletes a
#      previous backup; refusing is the whole point of the cap.
#   2. pg_dump, run INSIDE the postgres container over its unix socket as the
#      database owner. The official image trusts local socket connections, so
#      no password is read, printed or passed on any command line.
#   3. docker pause the sync-worker (the only writer of /data/lancedb) while
#      the tar runs — measured 65 s for 2.5 GB on 2026-09-03; the whole run
#      took 81 s — then unpause, on every exit path, via trap. The
#      orchestrator keeps serving; its own web-index writer is append-only
#      (new fragment, then a new manifest) so a tar taken mid-write still
#      contains a consistent EARLIER version, which step 5 verifies.
#   4. Copy the sidecars out separately, byte for byte.
#   5. Verify: pg_restore --list must read the dump's TOC; the tar is extracted
#      into the orchestrator container's /tmp and every table is opened with
#      the same lancedb the app uses, counting rows (and distinct page_ids for
#      web_chunks) from the ARCHIVED copy — those are the numbers in the
#      manifest, not the live table's.
#   6. Work happens in backups/<stamp>.partial/ and is renamed into place only
#      when the manifest is written, so a directory named backups/<stamp>/ is
#      complete by construction. Re-running with the same stamp is a no-op
#      when it is complete, and restarts from scratch when only a partial
#      exists.
#
# Never prints or stores secrets: the manifest carries the database NAME and
# server version, nothing from the container's environment beyond that.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${TECHSARA_COMPOSE_PROJECT:-sf-local-ai}"
DEST="${BACKUP_DEST:-$ROOT/backups}"
STAMP="${BACKUP_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
MAX_BYTES="${BACKUP_MAX_BYTES:-21474836480}"   # 20 GiB, see header
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stamp) STAMP="$2"; shift ;;
    --dest) DEST="$2"; shift ;;
    --max-bytes) MAX_BYTES="$2"; shift ;;
    --dry-run) DRY=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$STAMP" in
  *[!A-Za-z0-9_.-]*|"") echo "error: --stamp must be [A-Za-z0-9_.-]+ (got '$STAMP')" >&2; exit 2 ;;
esac

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] error: %s\n' "$*" >&2; exit 1; }
human() { numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"; }

# Containers are found by their Compose labels, not by name: the launcher may
# scale or rename, and a name is one more thing to get wrong.
container_for() { # container_for SERVICE [STATUS...]
  local service="$1"; shift
  local args=(--filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$service")
  local s; for s in "$@"; do args+=(--filter "status=$s"); done
  docker ps "${args[@]}" --format '{{.Names}}' | head -n 1
}

command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
PG="$(container_for postgres running)";        [ -n "$PG" ]   || die "no running '$PROJECT' postgres container"
ORCH="$(container_for orchestrator running)";  [ -n "$ORCH" ] || die "no running '$PROJECT' orchestrator container (it mounts the data volume)"
SW="$(container_for sync-worker running paused || true)"      # optional service

FINAL="$DEST/$STAMP"
WORK="$DEST/$STAMP.partial"

# ------------------------------------------------------------ idempotency ----
if [ -f "$FINAL/manifest.json" ]; then
  log "backups/$STAMP is already complete ($(human "$(du -sb "$FINAL" | cut -f1)")); nothing to do"
  exit 0
fi
[ -e "$FINAL" ] && die "$FINAL exists but has no manifest.json — not a backup this script made; refusing to touch it"

# ------------------------------------------------------------- size gate ----
pg_bytes="$(docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select pg_database_size(current_database())"')"
[ -n "$pg_bytes" ] || die "could not size the database (is $PG healthy?)"
LANCE_DIRS="$(docker exec "$ORCH" sh -c 'for d in lancedb lancedb-web; do [ -d "/data/$d" ] && printf "%s\n" "$d"; done; true')"
lance_bytes=0
if [ -n "$LANCE_DIRS" ]; then
  # shellcheck disable=SC2086
  lance_bytes="$(docker exec "$ORCH" sh -c 'cd /data && du -sb "$@" | awk "{s+=\$1} END {print s+0}"' sh $LANCE_DIRS)"
fi
existing=0
[ -d "$DEST" ] && existing="$(du -sb "$DEST" | cut -f1)"
projected=$((existing + pg_bytes + lance_bytes))
log "containers: postgres=$PG orchestrator=$ORCH sync-worker=${SW:-none}"
log "sizes: database $(human "$pg_bytes"), lancedb dirs $(human "$lance_bytes") ($(printf '%s' "$LANCE_DIRS" | tr '\n' ' ')), backups/ now $(human "$existing")"
log "projected backups/ after this run: $(human "$projected") (cap $(human "$MAX_BYTES"))"
if [ "$projected" -gt "$MAX_BYTES" ]; then
  die "refusing: projected $(human "$projected") exceeds the cap $(human "$MAX_BYTES"). Prune old stamps under $DEST yourself (this script never deletes a backup) or raise --max-bytes."
fi
if [ "$DRY" = 1 ]; then
  log "dry run: would write $FINAL (pg_dump + tar of $(printf '%s' "${LANCE_DIRS:-nothing}" | tr '\n' ' ') + sidecars + manifest)"
  exit 0
fi

# ------------------------------------------------------------ workspace ----
mkdir -p "$DEST"
# A dump holds every conversation; a nested .gitignore keeps the whole
# directory out of `git add .` even when the root ignore file does not know
# about it. Written once; an operator may edit it.
[ -f "$DEST/.gitignore" ] || printf '# backups hold every conversation and page; never commit them\n*\n' > "$DEST/.gitignore"
if [ -d "$WORK" ]; then
  log "removing incomplete $WORK from an earlier failed run (it has no manifest)"
  rm -rf "$WORK"
fi
mkdir -p "$WORK/sidecars"

PAUSED_BY_US=0
VERIFY_DIR="/tmp/backup-verify-$STAMP"
cleanup() {
  local rc=$?
  if [ "$PAUSED_BY_US" = 1 ]; then
    docker unpause "$SW" >/dev/null 2>&1 && log "unpaused $SW" || log "WARNING: could not unpause $SW — run: docker unpause $SW"
    PAUSED_BY_US=0
  fi
  docker exec "$ORCH" rm -rf "$VERIFY_DIR" >/dev/null 2>&1 || true
  if [ "$rc" -ne 0 ]; then
    log "FAILED (exit $rc); partial output left in $WORK for inspection — the next run with --stamp $STAMP removes it"
  fi
}
trap cleanup EXIT

# --------------------------------------------------------------- pg_dump ----
log "pg_dump -Fc from $PG (socket auth inside the container; no password leaves it)"
docker exec "$PG" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$WORK/postgres.dump"
toc_entries="$(docker exec -i "$PG" pg_restore --list < "$WORK/postgres.dump" | grep -c '^[0-9]' || true)"
[ "${toc_entries:-0}" -gt 0 ] || die "pg_restore --list found no entries in the dump"
db_name="$(docker exec "$PG" sh -c 'printf %s "$POSTGRES_DB"')"
server_version="$(docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "show server_version"')"
pg_count() { # pg_count TABLE -> count, or "null" when the table does not exist (older schema)
  docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select count(*) from '"$1"'"' 2>/dev/null || printf 'null'
}
n_pages="$(pg_count web_pages)"; n_claims="$(pg_count web_claims)"; n_messages="$(pg_count messages)"; n_versions="$(pg_count web_page_versions)"
schema_version="$(docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select coalesce(max(version),0) from schema_migrations"' 2>/dev/null || printf 'null')"
log "dump ok: $toc_entries TOC entries, $(human "$(stat -c %s "$WORK/postgres.dump")"); web_pages=$n_pages web_claims=$n_claims messages=$n_messages"

# --------------------------------------------------- LanceDB (paused tar) ----
if [ -n "$LANCE_DIRS" ]; then
  if [ -n "$SW" ]; then
    if [ "$(docker inspect -f '{{.State.Paused}}' "$SW")" = "false" ]; then
      docker pause "$SW" >/dev/null; PAUSED_BY_US=1; log "paused $SW"
    else
      log "$SW was already paused; leaving it that way afterwards"
    fi
  fi
  started=$(date +%s)
  # shellcheck disable=SC2086
  docker exec "$ORCH" tar -C /data -cf - $LANCE_DIRS > "$WORK/lancedb.tar"
  log "tar ok: $(human "$(stat -c %s "$WORK/lancedb.tar")") in $(( $(date +%s) - started ))s"
  if [ "$PAUSED_BY_US" = 1 ]; then
    docker unpause "$SW" >/dev/null; PAUSED_BY_US=0; log "unpaused $SW"
  fi
  for d in $LANCE_DIRS; do
    if docker exec "$ORCH" test -f "/data/$d/_techsara_embedding_index.json"; then
      mkdir -p "$WORK/sidecars/$d"
      docker cp -q "$ORCH:/data/$d/_techsara_embedding_index.json" "$WORK/sidecars/$d/_techsara_embedding_index.json"
    fi
  done
else
  log "no LanceDB directory under /data yet — PostgreSQL only"
fi

# ---------------------------------------------------- verify + manifest ----
lance_json='{}'
if [ -n "$LANCE_DIRS" ]; then
  log "verifying the archive inside $ORCH (extract to $VERIFY_DIR, open every table)"
  docker exec -i "$ORCH" sh -c "rm -rf '$VERIFY_DIR' && mkdir -p '$VERIFY_DIR' && tar -C '$VERIFY_DIR' -xf -" < "$WORK/lancedb.tar"
  lance_json="$(docker exec -i -e "VERIFY_DIR=$VERIFY_DIR" "$ORCH" python - <<'PY'
import json, os, sys, warnings
warnings.simplefilter("ignore")
import lancedb
import pyarrow.compute as pc

root = os.environ["VERIFY_DIR"]
out = {}
for name in sorted(os.listdir(root)):
    directory = os.path.join(root, name)
    if not os.path.isdir(directory):
        continue
    entry = {"tables": {}, "sidecar": None}
    sidecar = os.path.join(directory, "_techsara_embedding_index.json")
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as fh:
            entry["sidecar"] = json.load(fh)
    conn = lancedb.connect(directory)
    for table_name in sorted(conn.table_names()):
        table = conn.open_table(table_name)
        rows = int(table.count_rows())
        info = {"rows": rows}
        # Read one non-vector column end to end: count_rows() answers from
        # fragment metadata, a column read touches every data file the
        # latest manifest references — a torn archive fails HERE, not on
        # restore day.
        scalar = next((f.name for f in table.schema if f.name != "vector"), None)
        if rows and scalar:
            col = table.search().select([scalar]).limit(rows).to_arrow()
            info["read_rows"] = len(col)
            if scalar == "page_id":
                info["distinct_page_ids"] = len(pc.unique(col["page_id"]))
            if len(col) != rows:
                info["problem"] = f"read {len(col)} rows, manifest says {rows}"
        entry["tables"][table_name] = info
    out[name] = entry
json.dump(out, sys.stdout)
PY
)"
  printf '%s' "$lance_json" | grep -q '"problem"' && die "archive verification found a torn table: $lance_json"
  docker exec "$ORCH" rm -rf "$VERIFY_DIR" >/dev/null 2>&1 || true
fi

pg_sha="$(sha256sum "$WORK/postgres.dump" | cut -d' ' -f1)"
pg_size="$(stat -c %s "$WORK/postgres.dump")"
tar_sha=null; tar_size=null
if [ -f "$WORK/lancedb.tar" ]; then
  tar_sha="\"$(sha256sum "$WORK/lancedb.tar" | cut -d' ' -f1)\""
  tar_size="$(stat -c %s "$WORK/lancedb.tar")"
fi

docker exec -i \
  -e "M_STAMP=$STAMP" -e "M_PROJECT=$PROJECT" -e "M_PG=$PG" -e "M_ORCH=$ORCH" -e "M_SW=${SW:-}" \
  -e "M_DB=$db_name" -e "M_SERVER=$server_version" -e "M_SCHEMA=$schema_version" -e "M_TOC=$toc_entries" \
  -e "M_PG_SHA=$pg_sha" -e "M_PG_SIZE=$pg_size" -e "M_TAR_SHA=$tar_sha" -e "M_TAR_SIZE=$tar_size" \
  -e "M_PAGES=$n_pages" -e "M_CLAIMS=$n_claims" -e "M_MESSAGES=$n_messages" -e "M_VERSIONS=$n_versions" \
  -e "M_LANCE=$lance_json" -e "M_DIRS=$(printf '%s' "$LANCE_DIRS" | tr '\n' ' ')" \
  "$ORCH" python - > "$WORK/manifest.json" <<'PY'
import datetime, json, os
e = os.environ
def num(v):
    return None if v in ("", "null") else int(v)
manifest = {
    "stamp": e["M_STAMP"],
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "tool": "scripts/backup-knowledge.sh",
    "compose_project": e["M_PROJECT"],
    "containers": {"postgres": e["M_PG"], "orchestrator": e["M_ORCH"], "sync_worker": e["M_SW"] or None},
    "postgres": {
        "file": "postgres.dump",
        "format": "pg_dump -Fc (custom archive; restore with pg_restore)",
        "database": e["M_DB"],
        "server_version": e["M_SERVER"],
        "schema_version": num(e["M_SCHEMA"]),
        "toc_entries": int(e["M_TOC"]),
        "bytes": int(e["M_PG_SIZE"]),
        "sha256": e["M_PG_SHA"],
        "counts": {
            "web_pages": num(e["M_PAGES"]),
            "web_claims": num(e["M_CLAIMS"]),
            "messages": num(e["M_MESSAGES"]),
            "web_page_versions": num(e["M_VERSIONS"]),
        },
    },
    "lancedb": {
        "file": "lancedb.tar" if e["M_TAR_SIZE"] != "null" else None,
        "bytes": num(e["M_TAR_SIZE"]),
        "sha256": json.loads(e["M_TAR_SHA"]),
        "directories": json.loads(e["M_LANCE"]),
        "note": (
            "Derived state. /data/lancedb-web is rebuilt from web_pages by "
            "`python -m tools.reindex_web build`; /data/lancedb by a full "
            "Salesforce resync. Row counts above were read from the ARCHIVED copy."
        ),
    },
    "restore": [
        "# PostgreSQL (into a running, EMPTY database of the same name — pg_restore does not drop what is there):",
        f"docker exec -i {e['M_PG']} sh -c 'pg_restore -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" --no-owner --clean --if-exists' < backups/{e['M_STAMP']}/postgres.dump",
        "# LanceDB (stop writers first: docker pause the sync-worker, then):",
        f"docker exec -i {e['M_ORCH']} tar -C /data -xf - < backups/{e['M_STAMP']}/lancedb.tar",
        "# then restart the orchestrator so it re-opens the tables: ./techsara up",
    ],
}
json.dump(manifest, __import__("sys").stdout, indent=2)
PY
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$WORK/manifest.json" 2>/dev/null \
  || docker exec -i "$ORCH" python -c "import json,sys; json.load(sys.stdin)" < "$WORK/manifest.json" \
  || die "manifest.json is not valid JSON"

mv "$WORK" "$FINAL"
log "complete: $FINAL ($(human "$(du -sb "$FINAL" | cut -f1)"))"
log "  postgres.dump  web_pages=$n_pages web_claims=$n_claims messages=$n_messages (schema v$schema_version, PostgreSQL $server_version)"
[ -n "$LANCE_DIRS" ] && log "  lancedb.tar    $(printf '%s' "$lance_json" | tr -d '\n' | sed 's/  */ /g' | cut -c1-300)"
log "  restore commands are in manifest.json"
