#!/usr/bin/env bash
# Put the previously-recorded images back, and refuse to pretend that undid the
# database.
#
#   scripts/deploy-rollback.sh --list
#   scripts/deploy-rollback.sh --to DIR|MANIFEST|RECORD [--dry-run] [--yes]
#                             [--services "orchestrator frontend"]
#                             [--i-accept-schema-drift]
#
# THE LIE THIS SCRIPT EXISTS TO NOT TELL
#
# "Roll back the deploy" sounds like one action. It is two, and only one of them
# is reversible.
#
#   * The IMAGES are reversible. They are content-addressed; the previous ones
#     are still on disk; pointing the tags back and recreating the containers
#     restores the previous code exactly.
#   * The DATABASE is not. orchestrator/app/db.py has twenty-seven forward
#     migrations and zero down migrations. `init_schema` applies whatever is not
#     yet in `schema_migrations` and never removes anything. Once V26 has run,
#     V26 has run. Starting the V25 image afterwards does not un-run it — it
#     starts old code on top of a newer schema, and the failure that produces is
#     not a clean crash, it is whatever that old code does when it meets a
#     column it does not know about.
#
# So the check is: does the code being rolled BACK TO know about every migration
# the database has already APPLIED? If it does not, this script stops. Not a
# warning that scrolls past at 3 a.m. — a refusal, with the list of migrations
# that are ahead, and an explicit flag to override it once a human has read
# them.
#
# AND IT NEVER RESTORES A DATABASE. A dump taken before the deploy is older than
# every row written since. Automatically restoring it to "fix" a rollback would
# silently delete real user data — conversations, messages, uploads — to fix a
# code problem. If a restore really is the answer, this script prints the
# command and the operator runs it deliberately, having decided what the data
# loss is worth.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

TO=""; DRY=0; YES=0; ACCEPT_DRIFT=0; LIST=0
SERVICES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="${2:?--to needs a release directory, manifest.json or record.json}"; shift ;;
    --services) SERVICES="${2:?}"; shift ;;
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --i-accept-schema-drift) ACCEPT_DRIFT=1 ;;
    --list) LIST=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) dr_die "unknown option: $1" ;;
  esac
  shift
done

dr_need docker; dr_need python3

# ----------------------------------------------------------------------- list
if [ "$LIST" = 1 ]; then
  dir="$(dr_releases_dir)"
  [ -d "$dir" ] || dr_die "no releases recorded under $dir"
  printf '%-24s %-10s %-8s %-8s %s\n' RELEASE KIND SCHEMA PGDUMP GIT
  printf '%-24s %-10s %-8s %-8s %s\n' ------- ---- ------ ------ ---
  for entry in "$dir"/*/; do
    [ -d "$entry" ] || continue
    python3 - "$entry" <<'PY'
import json, os, sys
from pathlib import Path
d = Path(sys.argv[1])
for name, kind in (("manifest.json", "manifest"), ("record.json", "record")):
    path = d / name
    if not path.is_file():
        continue
    try:
        doc = json.load(open(path))
    except Exception:
        continue
    if kind == "manifest":
        schema = (doc.get("schema") or {}).get("code_version")
        dump = "-"
    else:
        db = doc.get("database") or {}
        schema = db.get("applied_schema_version")
        dump = "yes" if db.get("dump") else "-"
    git = ((doc.get("git") or {}).get("head") or "")[:12]
    print("%-24s %-10s %-8s %-8s %s" % (d.name, kind, schema, dump, git))
PY
  done
  exit 0
fi

[ -n "$TO" ] || dr_die "nothing to roll back to. Use --list to see the recorded releases, then --to <one of them>."

# ---------------------------------------------------------- resolve the target
# Accepts a release directory, a manifest.json, or a record.json. All three
# ultimately yield the same thing: service -> IMAGE ID. Never a tag; a tag is
# what got us here.
if [ -d "$TO" ]; then
  if   [ -f "$TO/manifest.json" ]; then TARGET_DOC="$TO/manifest.json"
  elif [ -f "$TO/record.json" ];   then TARGET_DOC="$TO/record.json"
  else dr_die "$TO holds neither manifest.json nor record.json"; fi
elif [ -f "$TO" ]; then
  TARGET_DOC="$TO"
else
  # A bare stamp, resolved under .runtime/releases.
  if   [ -f "$(dr_releases_dir)/$TO/manifest.json" ]; then TARGET_DOC="$(dr_releases_dir)/$TO/manifest.json"
  elif [ -f "$(dr_releases_dir)/$TO/record.json" ];   then TARGET_DOC="$(dr_releases_dir)/$TO/record.json"
  else dr_die "cannot resolve '$TO' to a release directory, manifest.json or record.json"; fi
fi
TARGET_DIR="$(cd "$(dirname "$TARGET_DOC")" && pwd)"

dr_say "rollback: target document $TARGET_DOC"

# service<TAB>image-id, for the application services only.
TARGET_IMAGES="$(python3 - "$TARGET_DOC" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
kind = doc.get("kind", "")
out = {}
if kind == "techsara.release-manifest":
    for svc, meta in (doc.get("images") or {}).items():
        out[svc] = meta["id"]
elif kind == "techsara.recovery-record":
    for name, meta in (doc.get("containers") or {}).items():
        svc = meta.get("service")
        if svc in ("orchestrator", "sync-worker", "frontend"):
            out[svc] = meta["image_id"]
else:
    sys.exit(f"unrecognised document kind: {kind!r}")
if not out:
    sys.exit("the document names no application images")
for svc in sorted(out):
    print(f"{svc}\t{out[svc]}")
PY
)" || dr_die "cannot read the rollback target"

if [ -n "$SERVICES" ]; then
  TARGET_IMAGES="$(printf '%s\n' "$TARGET_IMAGES" | while IFS=$'\t' read -r svc id; do
      case " $SERVICES " in *" $svc "*) printf '%s\t%s\n' "$svc" "$id" ;; esac
    done)"
  [ -n "$TARGET_IMAGES" ] || dr_die "--services '$SERVICES' matched none of the services in $TARGET_DOC"
fi

# Every target image must still exist HERE. A rollback plan that names an image
# `docker image prune` collected is not a plan.
MISSING=0
while IFS=$'\t' read -r svc id; do
  [ -n "$svc" ] || continue
  if [ -z "$(dr_image_id "$id")" ]; then
    dr_warn "$svc: image $id is NOT present on this box (pruned?)"
    MISSING=1
  else
    dr_say "  $svc -> $id (present)"
  fi
done <<<"$TARGET_IMAGES"
[ "$MISSING" = 0 ] || dr_die "the rollback target names images that are not on this machine; nothing was changed"

# ------------------------------------------------------ THE SCHEMA BOUNDARY
LIVE_SCHEMA="$(dr_live_schema_version 2>/dev/null || true)"
[ -n "$LIVE_SCHEMA" ] || dr_die "cannot read the database's applied schema version. Refusing to roll back blind:\
 the whole point of this check is that the database does not roll back with the image."

TARGET_ORCH="$(printf '%s\n' "$TARGET_IMAGES" | awk -F'\t' '$1=="orchestrator"{print $2}')"
if [ -z "$TARGET_ORCH" ]; then
  dr_warn "the target does not include the orchestrator, so no schema check applies"
  TARGET_SCHEMA=""
else
  TARGET_SCHEMA="$(dr_code_schema_version_from_image "$TARGET_ORCH" 2>/dev/null || true)"
  [ -n "$TARGET_SCHEMA" ] || dr_die "cannot read the migration table out of $TARGET_ORCH; refusing to guess whether it is schema-compatible"
fi

dr_say ""
dr_say "SCHEMA COMPATIBILITY"
dr_say "  database has applied      : V$LIVE_SCHEMA"
dr_say "  rollback target code knows: V${TARGET_SCHEMA:-n/a}"

if [ -n "$TARGET_SCHEMA" ] && [ "$TARGET_SCHEMA" -lt "$LIVE_SCHEMA" ]; then
  dr_say ""
  dr_warn "REFUSING: this is a rollback ACROSS a schema migration boundary."
  dr_warn ""
  dr_warn "The database is at V$LIVE_SCHEMA. The code you are rolling back to stops at"
  dr_warn "V$TARGET_SCHEMA. Migrations are forward-only - there are no down migrations in"
  dr_warn "orchestrator/app/db.py - so starting the older image does NOT return the"
  dr_warn "schema to V$TARGET_SCHEMA. It runs V$TARGET_SCHEMA code against a V$LIVE_SCHEMA schema."
  dr_warn ""
  dr_warn "Migrations the database has that this code has never heard of:"
  # Named, dated and described, because "there is drift" is not enough to
  # decide with. The descriptions come out of the RUNNING image's db.py, which
  # is the code that applied them.
  RUNNING_ORCH="$(dr_container_image_id "$(dr_container_for orchestrator)")"
  if [ -n "$RUNNING_ORCH" ]; then
    cid="$(docker create "$RUNNING_ORCH" true 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      tmp="$(mktemp)"
      docker cp "$cid:/app/app/db.py" "$tmp" >/dev/null 2>&1 || true
      docker rm -f "$cid" >/dev/null 2>&1 || true
      DR_FROM="$TARGET_SCHEMA" DR_TO="$LIVE_SCHEMA" python3 - "$tmp" <<'PY' | while IFS= read -r line; do dr_warn "    $line"; done
import os, re, sys
src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
low, high = int(os.environ["DR_FROM"]), int(os.environ["DR_TO"])
for version in range(low + 1, high + 1):
    match = re.search(rf'_MIGRATION_V{version}\s*=\s*"""\s*\n(.*?)\n', src)
    note = match.group(1).lstrip("- ").strip() if match else "(no description found in the running image)"
    print(f"V{version}: {note[:96]}")
PY
      rm -f "$tmp"
    fi
  fi
  dr_warn ""
  dr_warn "Your options, in the order they are usually right:"
  dr_warn "  1. Roll FORWARD. Fix the defect and deploy a new build. This is almost"
  dr_warn "     always correct, because the schema is already where the new code wants it."
  dr_warn "  2. Roll back only the services that are NOT the orchestrator:"
  dr_warn "       $0 --to $TARGET_DIR --services 'frontend'"
  dr_warn "  3. Decide, having read the migrations above, that the old code tolerates"
  dr_warn "     the newer schema, and re-run with --i-accept-schema-drift."
  dr_warn ""
  dr_warn "What is NOT on that list is restoring the database. A pre-deploy dump is"
  dr_warn "older than every conversation, message and upload written since it was taken,"
  dr_warn "and restoring it deletes them. This script will not do that, and neither"
  dr_warn "should a script you write later."
  if [ -f "$TARGET_DIR/record.json" ]; then
    python3 - "$TARGET_DIR/record.json" "$TARGET_DIR" <<'PY' | while IFS= read -r line; do dr_warn "$line"; done
import json, sys
doc = json.load(open(sys.argv[1]))
dump = (doc.get("database") or {}).get("dump")
if dump:
    print("")
    print("A dump from that release does exist, if a human decides the loss is worth it:")
    print(f"  {sys.argv[2]}/{dump['file']}  (taken at schema V"
          f"{doc['database']['applied_schema_version']}, PostgreSQL {dump['server_major']})")
    print("  Restoring it DISCARDS every row written since. Read that sentence twice.")
PY
  fi
  if [ "$ACCEPT_DRIFT" != 1 ]; then
    dr_die "refusing the rollback (no --i-accept-schema-drift). Nothing was changed."
  fi
  dr_warn ""
  dr_warn "--i-accept-schema-drift given: proceeding with V$TARGET_SCHEMA code on a V$LIVE_SCHEMA schema."
elif [ -n "$TARGET_SCHEMA" ] && [ "$TARGET_SCHEMA" -gt "$LIVE_SCHEMA" ]; then
  dr_say "  the target code is AHEAD of the database; it will apply V$((LIVE_SCHEMA + 1))..V$TARGET_SCHEMA on start."
  dr_say "  That is a migration, not a rollback. It is forward-only and cannot be undone by this script."
elif [ -z "$TARGET_SCHEMA" ]; then
  dr_say "  not applicable: this rollback does not replace the orchestrator, so no code"
  dr_say "  is being moved relative to the schema. Nothing here says the OTHER services"
  dr_say "  are compatible with V$LIVE_SCHEMA - only that this script cannot tell, because"
  dr_say "  the migration table lives in the orchestrator image and it is not in scope."
else
  dr_say "  compatible: the target code knows every migration the database has applied."
fi

# ------------------------------------------------------------------- dry run
if [ "$DRY" = 1 ]; then
  dr_say ""
  dr_say "dry run: nothing was tagged, stopped or recreated."
  dr_say "It would have: recorded the current state, drained, re-pointed these tags"
  dr_say "to these image ids, recreated only these services, then verified the digests."
  exit 0
fi

# --------------------------------------------------------------------- do it
dr_lock_acquire "${DEPLOY_LOCK_WAIT:-1800}" "deploy-rollback to $(basename "$TARGET_DIR")"

if [ "$YES" != 1 ]; then
  printf 'Roll back the services above to those image ids? [type ROLLBACK to confirm] ' >&2
  read -r answer
  [ "$answer" = ROLLBACK ] || dr_die "not confirmed; nothing was changed"
fi

# The state being replaced is itself worth recording - the version that failed
# is evidence, and after the recreate its containers are gone.
dr_say "rollback: recording the state being replaced first"
"$HERE/deploy-record.sh" --note "state immediately before rollback to $(basename "$TARGET_DIR")" >/dev/null \
  || dr_warn "could not write a pre-rollback record; continuing"

PREFIX="$(dr_compose_prefix)"
ROLL_SERVICES=()
while IFS=$'\t' read -r svc id; do [ -n "$svc" ] && ROLL_SERVICES+=("$svc"); done <<<"$TARGET_IMAGES"

dr_say "rollback: draining ${ROLL_SERVICES[*]}"
for svc in "${ROLL_SERVICES[@]}"; do
  "$HERE/deploy-drain.sh" wait "$svc" --deadline "${DEPLOY_DRAIN_DEADLINE:-90}" --quiet-for 5 || true
done

# Re-point the mutable tags at the recorded ids. THIS is the promotion step: the
# id is the authority and the tag is made to agree with it, never the reverse.
# The previous target is remembered so an aborted rollback can be undone without
# a rebuild.
UNDO=()
while IFS=$'\t' read -r svc id; do
  [ -n "$svc" ] || continue
  tag="$(cd "$DR_ROOT" && eval "$PREFIX" config --format json | DR_SVC="$svc" python3 -c '
import json, os, sys
print((json.load(sys.stdin)["services"][os.environ["DR_SVC"]] or {})["image"])')"
  was="$(dr_image_id "$tag")"
  UNDO+=("$tag=$was")
  docker tag "$id" "$tag"
  dr_say "  $tag: $was -> $id"
done <<<"$TARGET_IMAGES"
printf '%s\n' "${UNDO[@]}" > "$DR_ROOT/.runtime/locks/rollback-undo-tags"

# --no-deps: only the named services. A rollback of the frontend must not
# recreate postgres, and must never touch the model containers.
dr_say "rollback: recreating ${ROLL_SERVICES[*]} (--no-deps, nothing else)"
( cd "$DR_ROOT" && eval "$PREFIX" up -d --no-deps --force-recreate "${ROLL_SERVICES[@]}" ) \
  || dr_die "compose up failed during the rollback; the stack is mid-change and needs a human"

# ------------------------------------------------------------------- verify
dr_say "rollback: verifying the containers are running the recorded image ids"
FAIL=0
while IFS=$'\t' read -r svc id; do
  [ -n "$svc" ] || continue
  have="$(dr_container_image_id "$(dr_container_for "$svc")")"
  if [ "$have" = "$id" ]; then dr_say "  $svc: OK $id"
  else dr_warn "  $svc: expected $id but the container is running $have"; FAIL=1; fi
done <<<"$TARGET_IMAGES"
[ "$FAIL" = 0 ] || dr_die "the rollback did not land on the recorded images"

PORT="$(dr_env_value ORCHESTRATOR_PORT)"; PORT="${PORT:-8080}"
for attempt in $(seq 1 30); do
  if curl -fsS -m 10 -o /dev/null "http://127.0.0.1:${PORT}/health"; then
    dr_say "rollback: orchestrator answers /health"
    break
  fi
  [ "$attempt" = 30 ] && dr_warn "the orchestrator did not answer /health within ~5 minutes after the rollback"
  sleep 10
done

NOW_SCHEMA="$(dr_live_schema_version 2>/dev/null || echo '?')"
dr_say "rollback: complete. Database schema is V$NOW_SCHEMA - unchanged by this rollback, as it must be."
