#!/usr/bin/env bash
# Write down what is running BEFORE anything replaces it.
#
#   scripts/deploy-record.sh [--out DIR] [--dump] [--note TEXT]
#
# A rollback is only as good as the description of what to roll back TO. "Redeploy
# the previous commit" is not that description: the previous commit does not say
# which image id was actually serving, which compose chain rendered it, which
# migrations the database had already applied, or which PostgreSQL major was
# under it. All four are needed, and all four stop being knowable the moment the
# containers are recreated.
#
# So this runs first, and it records:
#
#   * every container in the project, by IMAGE ID — not by tag. A tag is a
#     pointer someone can move; `{{.Image}}` is the immutable id the container
#     was actually created from, and it is the only answer to "what is serving"
#     that survives the next build. The monitoring services are brought up by a
#     different compose invocation, so containers are listed by PROJECT LABEL
#     rather than from the app chain, and the record covers the whole box.
#   * the rendered configuration from the full four-file chain, REDACTED —
#     every environment value is replaced by a hash of itself. That still
#     detects "the configuration changed" and still says which key changed,
#     without writing credentials into a file that will be read back by a
#     human, pasted into a ticket, or attached to a run summary.
#   * schema compatibility: the migrations the database has APPLIED, and the
#     highest migration the running image's CODE knows about. Those two numbers
#     are what makes a rollback safe or unsafe, and they are checked by
#     scripts/deploy-rollback.sh.
#   * the deployed PostgreSQL version, read from the running server. Not from
#     the image tag: the tag is a sha256 and says nothing.
#
# --dump additionally takes a `pg_dump -Fc` of the application database, using
# the pg_dump INSIDE the database container so the client can never be older
# than the server. Off by default because it is real I/O against production.
#
# Records are never pruned by this script. A backup that a later run can delete
# is not a backup.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

OUT=""; DUMP=0; NOTE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?--out needs a directory}"; shift ;;
    --dump) DUMP=1 ;;
    --note) NOTE="${2:-}"; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) dr_die "unknown option: $1" ;;
  esac
  shift
done

dr_need docker; dr_need python3

STAMP="$(dr_stamp)"
DIR="${OUT:-$(dr_releases_dir)/$STAMP}"
mkdir -p "$DIR"
chmod 700 "$DIR"
DR_LOG="$DIR/record.log"

dr_say "record: root=$DR_ROOT out=$DIR"

PREFIX="$(dr_compose_prefix)"
dr_say "record: compose chain verified"

# ------------------------------------------------- rendered config, redacted
# The full rendered document is hashed so a later run can prove the
# configuration changed even though this file never holds the values.
RENDERED="$(cd "$DR_ROOT" && eval "$PREFIX" config --format json)" \
  || dr_die "docker compose config failed - refusing to record a configuration nobody can render"

RENDERED_SHA="$(printf '%s' "$RENDERED" | sha256sum | cut -d' ' -f1)"
# Via a file, not a pipe: the Python program itself arrives on stdin as a
# heredoc, so stdin is not available for the document as well.
RENDERED_TMP="$(mktemp)"; trap 'rm -f "$RENDERED_TMP"' EXIT
printf '%s' "$RENDERED" >"$RENDERED_TMP"
DR_DIR="$DIR" DR_ROOT="$DR_ROOT" python3 - "$RENDERED_TMP" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

root = Path(os.environ["DR_ROOT"])
sys.path.insert(0, str(root / "launcher"))
try:
    from techsara_cli.utils import parse_env_file
except Exception:
    parse_env_file = None

doc = json.loads(Path(sys.argv[1]).read_text())


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


# Every environment value becomes a hash of itself: a diff still shows WHICH key
# moved, and by construction cannot show what it moved to.
for name, service in (doc.get("services") or {}).items():
    env = service.get("environment")
    if isinstance(env, dict):
        service["environment"] = {k: (digest(v) if isinstance(v, str) else v) for k, v in env.items()}
    elif isinstance(env, list):
        out = []
        for item in env:
            key, _, value = str(item).partition("=")
            out.append(f"{key}={digest(value)}")
        service["environment"] = out

# Belt and braces: anything that LOOKS like a credential in the env chain is
# scrubbed out of the whole document, not just out of `environment` — a
# password can also reach a container through `command:` or a URL.
secrets = set()
if parse_env_file is not None:
    for name in (".env", ".runtime/secrets.env", ".runtime/generated.env"):
        path = root / name
        if not path.is_file():
            continue
        try:
            values = parse_env_file(path)
        except Exception:
            continue
        for key, value in values.items():
            if not value or len(value) < 8:
                continue
            # A path is not a credential. TECHSARA_SECRET_ENV holds the LOCATION
            # of the secrets file, and blanking that out of the record removes
            # something a recovery actually needs while protecting nothing.
            if value.startswith("/") and Path(value).exists():
                continue
            if any(word in key.upper() for word in
                   ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "DSN", "PASS")):
                secrets.add(value)

text = json.dumps(doc, indent=2, sort_keys=True)
for value in sorted(secrets, key=len, reverse=True):
    text = text.replace(value, "«redacted»")

Path(os.environ["DR_DIR"], "compose-config.redacted.json").write_text(text + "\n")
PY
chmod 600 "$DIR/compose-config.redacted.json"
dr_say "record: rendered config captured (env values hashed) sha256=${RENDERED_SHA:0:16}…"

# -------------------------------------------------------------- the containers
CONTAINERS="$(dr_project_containers)"
[ -n "$CONTAINERS" ] || dr_warn "no containers carry label com.docker.compose.project=$DR_PROJECT"

# --------------------------------------------------------------- schema facts
LIVE_SCHEMA="$(dr_live_schema_version 2>/dev/null || echo '')"
PG_MAJOR="$(dr_deployed_pg_major 2>/dev/null || echo '')"
PG_FULL="$(docker exec "$(dr_container_for postgres)" postgres --version 2>/dev/null || echo '')"
ORCH_IMAGE="$(dr_container_image_id "$(dr_container_for orchestrator)")"
CODE_SCHEMA=""
[ -n "$ORCH_IMAGE" ] && CODE_SCHEMA="$(dr_code_schema_version_from_image "$ORCH_IMAGE" 2>/dev/null || echo '')"

PG_USER="$(dr_env_value POSTGRES_USER)"; PG_USER="${PG_USER:-techsara}"
PG_DB="$(dr_env_value POSTGRES_DB)"; PG_DB="${PG_DB:-techsara}"

# The applied-migration LIST, not just the maximum. `init_schema` skips any
# version already present, so a gap (say 1..20 plus 27) is possible in
# principle and would make the maximum a lie about what the schema contains.
MIGRATIONS="$(docker exec "$(dr_container_for postgres)" psql -U "$PG_USER" -d "$PG_DB" \
    -tAF, -c 'SELECT version, applied_at FROM schema_migrations ORDER BY version' 2>/dev/null || true)"

dr_say "record: schema live=${LIVE_SCHEMA:-?} running-image-code=${CODE_SCHEMA:-?} postgres-major=${PG_MAJOR:-?}"

# ------------------------------------------------------------------- pg_dump
DUMP_FILE=""
if [ "$DUMP" = 1 ]; then
  DUMP_FILE="$DIR/appdb-${PG_DB}.pgdump"
  dr_say "record: pg_dump -Fc of $PG_DB (inside the database container, so client==server)"
  if docker exec "$(dr_container_for postgres)" \
       pg_dump -U "$PG_USER" -d "$PG_DB" -Fc --no-owner --no-privileges > "$DUMP_FILE" 2>>"$DR_LOG"; then
    chmod 600 "$DUMP_FILE"
    dr_say "record: dump written, $(du -h "$DUMP_FILE" | cut -f1)"
  else
    rm -f "$DUMP_FILE"; DUMP_FILE=""
    dr_warn "pg_dump failed - see $DR_LOG. The rest of the record is still valid."
  fi
fi

# ------------------------------------------------------------------ assemble
DR_DIR="$DIR" DR_STAMP="$STAMP" DR_NOTE="$NOTE" DR_CREATED="$(dr_now)" \
DR_CONTAINERS="$CONTAINERS" DR_RENDERED_SHA="$RENDERED_SHA" \
DR_LIVE_SCHEMA="$LIVE_SCHEMA" DR_CODE_SCHEMA="$CODE_SCHEMA" \
DR_PG_MAJOR="$PG_MAJOR" DR_PG_FULL="$PG_FULL" DR_MIGRATIONS="$MIGRATIONS" \
DR_DUMP="$DUMP_FILE" DR_PREFIX="$PREFIX" DR_PROJECT="$DR_PROJECT" DR_ROOT="$DR_ROOT" \
python3 - <<'PY'
import hashlib, json, os, subprocess
from pathlib import Path

root = Path(os.environ["DR_ROOT"])


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True).stdout.strip()


containers = {}
for name in os.environ["DR_CONTAINERS"].split():
    raw = docker(
        "inspect", name, "--format",
        "{{.Image}}\t{{.Config.Image}}\t{{.State.Status}}\t{{.State.StartedAt}}"
        "\t{{.RestartCount}}\t{{index .Config.Labels \"com.docker.compose.service\"}}"
        "\t{{index .Config.Labels \"com.docker.compose.config-hash\"}}"
        "\t{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}",
    )
    if not raw:
        continue
    image_id, ref, status, started, restarts, service, cfg_hash, health = (raw.split("\t") + [""] * 8)[:8]
    digests = docker("image", "inspect", image_id, "--format", "{{json .RepoDigests}}") or "[]"
    try:
        repo_digests = json.loads(digests)
    except Exception:
        repo_digests = []
    containers[name] = {
        "service": service or None,
        # THE field. The tag can be re-pointed; this cannot.
        "image_id": image_id,
        "image_ref_requested": ref,
        # Present for registry images, empty for the three built here — which is
        # exactly why image_id is what the rollback uses.
        "repo_digests": repo_digests,
        "compose_config_hash": cfg_hash or None,
        "status": status,
        "health": health,
        "started_at": started,
        "restart_count": int(restarts) if restarts.isdigit() else None,
    }

# Env files: the KEYS and a per-value hash. Enough to prove "this key's value
# changed between the record and now", never enough to learn the value.
env_files = {}
try:
    import sys
    sys.path.insert(0, str(root / "launcher"))
    from techsara_cli.utils import parse_env_file
except Exception:
    parse_env_file = None
for name in (".env", ".runtime/secrets.env", ".runtime/generated.env"):
    path = root / name
    entry = {"present": path.is_file()}
    if path.is_file():
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        entry["mode"] = oct(path.stat().st_mode & 0o777)
        if parse_env_file is not None:
            try:
                values = parse_env_file(path)
                entry["keys"] = {
                    key: "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
                    for key, value in sorted(values.items())
                }
            except Exception as exc:                       # noqa: BLE001
                entry["keys_error"] = type(exc).__name__
    env_files[name] = entry

migrations = []
for line in os.environ["DR_MIGRATIONS"].splitlines():
    line = line.strip()
    if not line:
        continue
    version, _, applied = line.partition(",")
    if version.isdigit():
        migrations.append({"version": int(version), "applied_at": applied})

prefix = os.environ["DR_PREFIX"].split()

def git(*args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()

dump = os.environ["DR_DUMP"]
record = {
    "kind": "techsara.recovery-record",
    "version": 1,
    "created_at": os.environ["DR_CREATED"],
    "stamp": os.environ["DR_STAMP"],
    "note": os.environ["DR_NOTE"] or None,
    "project": os.environ["DR_PROJECT"],
    "git": {
        "head": git("rev-parse", "HEAD"),
        "branch": (git("rev-parse", "--abbrev-ref", "HEAD") or None),
        "subject": git("log", "-1", "--pretty=%s"),
        "dirty_tracked_files": len([l for l in git("status", "--porcelain", "-uno").splitlines() if l]),
    },
    "compose": {
        "files": [prefix[i + 1] for i, t in enumerate(prefix) if t in ("-f", "--file")],
        "env_files": [prefix[i + 1] for i, t in enumerate(prefix) if t == "--env-file"],
        "profiles": [prefix[i + 1] for i, t in enumerate(prefix) if t == "--profile"],
        "rendered_sha256": os.environ["DR_RENDERED_SHA"],
        "rendered_redacted": "compose-config.redacted.json",
    },
    "containers": containers,
    "env_files": env_files,
    "database": {
        "postgres_version": os.environ["DR_PG_FULL"] or None,
        "postgres_major": int(os.environ["DR_PG_MAJOR"]) if os.environ["DR_PG_MAJOR"] else None,
        # The schema the DATABASE is at. Forward-only: an image rollback does
        # not move this number back.
        "applied_schema_version": int(os.environ["DR_LIVE_SCHEMA"]) if os.environ["DR_LIVE_SCHEMA"] else None,
        "applied_migrations": migrations,
        # The schema the RUNNING CODE knows how to reach.
        "running_code_schema_version": int(os.environ["DR_CODE_SCHEMA"]) if os.environ["DR_CODE_SCHEMA"] else None,
        "dump": (
            {
                "file": os.path.basename(dump),
                "format": "pg_dump -Fc (custom)",
                "sha256": hashlib.sha256(Path(dump).read_bytes()).hexdigest(),
                "bytes": Path(dump).stat().st_size,
                # A custom-format dump restores into the SAME major with the
                # matching pg_restore. Recorded so the rehearsal can check it.
                "server_major": int(os.environ["DR_PG_MAJOR"]) if os.environ["DR_PG_MAJOR"] else None,
            }
            if dump else None
        ),
    },
}
path = Path(os.environ["DR_DIR"], "record.json")
path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

dr_say "record: written to $DIR/record.json"
dr_say "record: $(python3 - "$DIR/record.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
db = r["database"]
print("%d containers, schema %s, postgres %s, git %s" % (
    len(r["containers"]), db["applied_schema_version"],
    db["postgres_major"], (r["git"]["head"] or "?")[:12]))
PY
)"
printf '%s\n' "$DIR/record.json"
