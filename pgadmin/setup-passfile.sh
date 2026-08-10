#!/usr/bin/env bash
# Give pgAdmin the database password so the pre-registered connection opens
# without a prompt.
#
#     ./pgadmin/setup-passfile.sh
#
# Run this before the first `docker compose up -d pgadmin`, and again if you
# delete the `pgadmin` volume or change POSTGRES_PASSWORD. Re-running against a
# live install is safe — it also repairs the already-imported server row.
#
# TWO THINGS THAT ARE NOT OBVIOUS, both learned the hard way:
#
# 1. WHERE the file goes. pgAdmin resolves a server's `PassFile` RELATIVE TO
#    THE USER'S STORAGE DIRECTORY (/var/lib/pgadmin/storage/<email with @ as
#    _>/), not as an absolute container path. An absolute /var/lib/pgadmin/
#    pgpass is silently not found and the UI falls back to prompting — with a
#    "password authentication failed" error, which sends you looking for a
#    wrong password rather than a wrong path.
#
# 2. WHY it is written inside the volume instead of bind-mounted. pgAdmin runs
#    as uid 5050 and libpq refuses a passfile that is group- or world-readable,
#    so a host file would have to be chowned to 5050 — which needs root on the
#    host. Writing it from a throwaway root container sidesteps that, and keeps
#    the password out of the repository directory.
set -euo pipefail

cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "error: no .env in $(pwd) — copy .env.example first" >&2; exit 1; }

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set in .env}"
POSTGRES_USER="${POSTGRES_USER:-techsara}"
PGADMIN_EMAIL="${PGADMIN_DEFAULT_EMAIL:-admin@techsarasolutions.com}"

# pgAdmin's per-user storage directory name is the email with '@' replaced by '_'.
STORAGE_DIR="${PGADMIN_EMAIL/@/_}"
VOLUME="sf-local-ai_pgadmin"

docker volume create "$VOLUME" >/dev/null

docker run --rm \
    -v "$VOLUME":/v \
    -e PGPASS_USER="$POSTGRES_USER" \
    -e PGPASS_PW="$POSTGRES_PASSWORD" \
    -e PGPASS_DIR="$STORAGE_DIR" \
    alpine sh -c '
        set -e
        mkdir -p "/v/storage/$PGPASS_DIR"
        printf "postgres:5432:*:%s:%s\n" "$PGPASS_USER" "$PGPASS_PW" \
            > "/v/storage/$PGPASS_DIR/pgpass"
        # chown the WHOLE volume, not just the file: pgAdmin creates sessions/,
        # storage/ and its own sqlite config under here, and a root-owned volume
        # root leaves it crash-looping on
        # "Permission denied: /var/lib/pgadmin/sessions".
        chown -R 5050:5050 /v
        chmod 700 /v "/v/storage/$PGPASS_DIR"
        chmod 600 "/v/storage/$PGPASS_DIR/pgpass"
    '

echo "pgpass written to storage/$STORAGE_DIR/pgpass in the '$VOLUME' volume."

# servers.json is only imported on pgAdmin's FIRST start, so an install that
# already ran keeps whatever path it imported. Repair it in place.
if docker ps --format '{{.Names}}' | grep -q '^sf-local-ai-pgadmin-1$'; then
    docker exec sf-local-ai-pgadmin-1 /venv/bin/python - <<'PY' || true
import json, sqlite3
con = sqlite3.connect("/var/lib/pgadmin/pgadmin4.db")
changed = 0
for sid, raw in con.execute("SELECT id, connection_params FROM server").fetchall():
    params = json.loads(raw) if raw else {}
    if params.get("passfile") != "/pgpass":
        params["passfile"] = "/pgpass"
        con.execute("UPDATE server SET connection_params=? WHERE id=?", (json.dumps(params), sid))
        changed += 1
con.commit()
print(f"  repaired {changed} already-registered server(s)" if changed
      else "  registered server already points at the right passfile")
PY
    echo "  restart pgAdmin to pick it up:  docker compose restart pgadmin"
else
    echo "Now: docker compose up -d pgadmin   ->   http://127.0.0.1:5050"
fi
