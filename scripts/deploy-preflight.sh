#!/usr/bin/env bash
# Build the application images ONCE, and pin what was built by its immutable
# id so the thing that gets deployed is provably the thing that was tested.
#
#   scripts/deploy-preflight.sh build   [--no-build] [--out DIR]
#   scripts/deploy-preflight.sh verify  [MANIFEST]
#   scripts/deploy-preflight.sh plan    [MANIFEST]
#
# WHY THIS EXISTS
#
# `techsara up` runs `docker compose build orchestrator sync-worker frontend`
# and then starts whatever `sf-local-ai-orchestrator:cuda` happens to point at.
# A tag is a mutable pointer. "Rebuild the tag and assume it is the tested
# artifact" is a guess, and it is wrong in every case that matters: a base
# image that moved, an `apt-get install` that resolved differently, a pip
# wheel that was yanked, a build that half-failed and left the old tag in
# place. The tested artifact and the deployed artifact then differ, and
# nothing in the pipeline notices.
#
# These images are built locally and never pushed, so they have no RepoDigest.
# Their immutable identity is the IMAGE ID — `{{.Id}}`, the sha256 of the image
# config, content-addressed exactly like a registry digest. That is what this
# script records, and what `verify` insists on afterwards.
#
# HOW THE DIGEST FLOWS FROM BUILD TO DEPLOY
#
#   1. `build` renders the FULL four-file compose chain (never a subset: a
#      subset resolves the orchestrator to the stale :cpu image) and builds the
#      three application services.
#   2. It reads back `{{.Id}}` for each and writes it into a release manifest
#      under .runtime/releases/<stamp>/manifest.json.
#   3. It also applies an immutable, content-addressed tag —
#      sf-local-ai-orchestrator:sha-<first 12 of the id> — so the exact
#      artifact survives someone moving `:cuda`, and can be named on a command
#      line without copying a 64-character id.
#   4. scripts/deploy.sh then runs `techsara up`, which rebuilds into a warm
#      cache and must therefore produce the SAME ids.
#   5. `verify` compares each RUNNING container's `{{.Image}}` — the id it was
#      actually created from, which does not change when a tag moves — against
#      the manifest. A mismatch fails the deploy instead of shipping an
#      unverified artifact.
#
# Step 5 is the load-bearing one. The manifest alone is a claim; `verify` is
# the part that makes the claim checkable.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

MODE="${1:-build}"; [ $# -gt 0 ] && shift || true
BUILD=1
OUT=""
MANIFEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --out) OUT="${2:?--out needs a directory}"; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    -*) dr_die "unknown option: $1" ;;
    *) MANIFEST="$1" ;;
  esac
  shift
done

dr_need docker; dr_need python3; dr_need git

latest_manifest() {
  local dir; dir="$(dr_releases_dir)"
  [ -d "$dir" ] || return 1
  find "$dir" -mindepth 2 -maxdepth 2 -name manifest.json -print 2>/dev/null | sort | tail -1
}

# ---------------------------------------------------------------------- build
do_build() {
  # The lock is taken even though a build does not touch a container: a build
  # MOVES the :cuda / :portable tags, and moving them under a deploy that is
  # between "recreate orchestrator" and "recreate frontend" is how the two
  # halves of one deploy end up on different code.
  dr_lock_acquire "${DEPLOY_LOCK_WAIT:-1800}" "deploy-preflight build"

  local stamp; stamp="$(dr_stamp)"
  local dir="${OUT:-$(dr_releases_dir)/$stamp}"
  mkdir -p "$dir"
  DR_LOG="$dir/preflight.log"

  dr_say "preflight: root=$DR_ROOT out=$dir"
  local prefix; prefix="$(dr_compose_prefix)"
  dr_say "preflight: compose chain verified (4 required files present, in order)"

  # What the mutable tags point at RIGHT NOW, before the build moves them.
  # Recorded so a build that turns out to be wrong can be undone by pointing
  # the tags back, without a second build.
  local svc tag before_ids=() after_ids=()
  for svc in "${DR_APP_SERVICES[@]}"; do
    tag="$(service_tag "$svc")"
    before_ids+=("$(dr_image_id "$tag")")
  done

  if [ "$BUILD" = 1 ]; then
    dr_say "preflight: building ${DR_APP_SERVICES[*]} through the full chain"
    ( cd "$DR_ROOT" && eval "$prefix" build "${DR_APP_SERVICES[@]}" ) >>"$DR_LOG" 2>&1 \
      || { tail -30 "$DR_LOG" >&2; dr_die "the build failed; nothing was deployed and no tag was promoted"; }
  else
    dr_say "preflight: --no-build, recording the images the tags already point at"
  fi

  local i=0 id short rel
  for svc in "${DR_APP_SERVICES[@]}"; do
    tag="$(service_tag "$svc")"
    id="$(dr_image_id "$tag")"
    [ -n "$id" ] || dr_die "$tag does not exist after the build - refusing to write a manifest that names an image nobody can run"
    after_ids+=("$id")
    short="${id#sha256:}"; short="${short:0:12}"
    rel="${tag%%:*}:sha-$short"
    # Idempotent: the tag is derived from the id, so re-tagging the same build
    # is a no-op rather than a moved pointer.
    docker tag "$id" "$rel" >/dev/null
    if [ "${before_ids[$i]}" != "$id" ]; then
      dr_say "  $svc: $tag MOVED ${before_ids[$i]:-<none>} -> $id"
    else
      dr_say "  $svc: $tag unchanged at $id"
    fi
    dr_say "  $svc: promoted as $rel"
    i=$((i + 1))
  done

  write_manifest "$dir" "${after_ids[@]}"
  dr_say "preflight: manifest written to $dir/manifest.json"
  printf '%s\n' "$dir/manifest.json"
}

service_tag() {
  # The tag compose will resolve for this service, read from the RENDERED
  # config rather than assumed. `sf-local-ai-orchestrator:cuda` is only correct
  # for this hardware profile; on another profile the same service is :cpu, and
  # hardcoding either is how the subset-chain bug got shipped in the first
  # place.
  local svc="$1" prefix
  prefix="$(dr_compose_prefix)"
  ( cd "$DR_ROOT" && eval "$prefix" config --format json ) 2>/dev/null \
    | DR_SVC="$svc" python3 -c '
import json, os, sys
svc = os.environ["DR_SVC"]
d = json.load(sys.stdin)
image = (d.get("services", {}).get(svc, {}) or {}).get("image")
if not image:
    raise SystemExit(f"service {svc} has no image in the rendered config")
print(image)'
}

write_manifest() {
  local dir="$1"; shift
  local ids=("$@")
  local head branch dirty live_schema code_schema prefix
  head="$(git -C "$DR_ROOT" rev-parse HEAD 2>/dev/null || echo '')"
  branch="$(git -C "$DR_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  [ "$branch" = HEAD ] && branch=""
  dirty="$(git -C "$DR_ROOT" status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
  live_schema="$(dr_live_schema_version 2>/dev/null || echo '')"
  code_schema="$(dr_code_schema_version_from_image "${ids[0]}" 2>/dev/null || echo '')"
  prefix="$(dr_compose_prefix)"

  DR_DIR="$dir" DR_HEAD="$head" DR_BRANCH="$branch" DR_DIRTY="$dirty" \
  DR_LIVE_SCHEMA="$live_schema" DR_CODE_SCHEMA="$code_schema" DR_PREFIX="$prefix" \
  DR_SERVICES="${DR_APP_SERVICES[*]}" DR_IDS="${ids[*]}" DR_CREATED="$(dr_now)" \
  python3 - <<'PY'
import json, os, subprocess

services = os.environ["DR_SERVICES"].split()
ids = os.environ["DR_IDS"].split()
prefix = os.environ["DR_PREFIX"].split()
files = [prefix[i + 1] for i, t in enumerate(prefix) if t in ("-f", "--file")]

def created(image):
    try:
        return subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Created}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return ""

images = {}
for svc, image_id in zip(services, ids):
    short = image_id.split(":", 1)[-1][:12]
    images[svc] = {
        "id": image_id,                       # the immutable digest
        "promoted_tag": None,                 # filled below
        "created": created(image_id),
    }

# The promoted tag is derived from the id, so it can be recomputed rather than
# trusted; recording it is a convenience, not the source of truth.
tags = {}
for svc in services:
    out = subprocess.run(
        ["docker", "image", "inspect", images[svc]["id"], "--format", "{{json .RepoTags}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        tags[svc] = json.loads(out or "[]")
    except Exception:
        tags[svc] = []
    short = images[svc]["id"].split(":", 1)[-1][:12]
    match = [t for t in tags[svc] if t.endswith(f":sha-{short}")]
    images[svc]["promoted_tag"] = match[0] if match else None
    images[svc]["all_tags"] = tags[svc]

manifest = {
    "kind": "techsara.release-manifest",
    "version": 1,
    "created_at": os.environ["DR_CREATED"],
    "git": {
        "head": os.environ["DR_HEAD"],
        "branch": os.environ["DR_BRANCH"] or None,
        "dirty_tracked_files": int(os.environ["DR_DIRTY"] or 0),
    },
    "compose": {"files": files, "profiles": [
        prefix[i + 1] for i, t in enumerate(prefix) if t == "--profile"
    ]},
    "schema": {
        # What the built code can migrate TO, and where the live database was
        # when this artifact was built. A rollback compares against these.
        "code_version": int(os.environ["DR_CODE_SCHEMA"]) if os.environ["DR_CODE_SCHEMA"] else None,
        "live_version_at_build": int(os.environ["DR_LIVE_SCHEMA"]) if os.environ["DR_LIVE_SCHEMA"] else None,
    },
    "images": images,
}
path = os.path.join(os.environ["DR_DIR"], "manifest.json")
with open(path, "w") as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

# --------------------------------------------------------------------- verify
# The gate. Everything above is bookkeeping until this runs.
do_verify() {
  local path="${MANIFEST:-$(latest_manifest || true)}"
  [ -n "$path" ] && [ -f "$path" ] || dr_die "no manifest to verify against (pass one, or run '$0 build' first)"
  dr_say "verify: against $path"

  local fail=0 svc want have container ref
  while read -r svc want; do
    container="$(dr_container_for "$svc")"
    have="$(dr_container_image_id "$container")"
    ref="$(dr_container_image_ref "$container")"
    if [ -z "$have" ]; then
      dr_warn "  $svc: no container named $container is present"
      fail=1; continue
    fi
    if [ "$have" = "$want" ]; then
      dr_say "  $svc: OK  running $want (asked for $ref)"
    else
      dr_warn "  $svc: MISMATCH"
      dr_warn "    manifest promoted : $want"
      dr_warn "    container running : $have  (created from the tag $ref)"
      dr_warn "    The tag was rebuilt or moved between the tested build and this"
      dr_warn "    container. What is serving is NOT the artifact that was tested."
      fail=1
    fi
  done < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
for svc, meta in sorted(m["images"].items()):
    print(svc, meta["id"])' "$path")

  [ "$fail" = 0 ] || dr_die "digest verification FAILED - the running stack does not match the promoted manifest"
  dr_say "verify: every application container is running its promoted digest"
}

# ----------------------------------------------------------------------- plan
# Read-only. What WOULD be recreated, and does anything already drift?
#
# Compose decides whether to recreate a container by comparing the rendered
# service definition's hash against the `com.docker.compose.config-hash` label
# on the running container. Reproducing that comparison here is how "recreate
# only what is required" stops being a hope and becomes a printed list.
do_plan() {
  local prefix; prefix="$(dr_compose_prefix)"
  dr_say "plan: compose chain verified"
  local desired
  desired="$( cd "$DR_ROOT" && eval "$prefix" config --hash='*' )" \
    || dr_die "docker compose config --hash failed"

  printf '\n%-18s %-10s %s\n' SERVICE ACTION REASON
  printf '%-18s %-10s %s\n' ------- ------ ------
  local svc want container have
  while read -r svc want; do
    container="$(dr_container_for "$svc")"
    have="$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' 2>/dev/null || true)"
    if [ -z "$have" ]; then
      printf '%-18s %-10s %s\n' "$svc" CREATE "no container named $container"
    elif [ "$have" = "$want" ]; then
      printf '%-18s %-10s %s\n' "$svc" keep "config hash matches"
    else
      printf '%-18s %-10s %s\n' "$svc" RECREATE "config hash ${have:0:12} -> ${want:0:12}"
    fi
  done <<<"$desired"

  printf '\n'
  local id tag
  for svc in "${DR_APP_SERVICES[@]}"; do
    tag="$(service_tag "$svc")"
    id="$(dr_image_id "$tag")"
    container="$(dr_container_for "$svc")"
    have="$(dr_container_image_id "$container")"
    if [ -n "$have" ] && [ -n "$id" ] && [ "$have" != "$id" ]; then
      dr_warn "$svc: the tag $tag has MOVED since the container started"
      dr_warn "  running: $have"
      dr_warn "  tag now: $id"
    fi
  done

  local live code
  live="$(dr_live_schema_version 2>/dev/null || echo '?')"
  code="$(dr_code_schema_version_from_image "$(service_tag orchestrator)" 2>/dev/null || echo '?')"
  dr_say "schema: live database=$live   image code=$code"
  if [ "$live" != '?' ] && [ "$code" != '?' ] && [ "$code" -lt "$live" ] 2>/dev/null; then
    dr_warn "the image on the orchestrator tag knows $code migrations but the database is at $live"
  fi
}

case "$MODE" in
  build)  do_build ;;
  verify) do_verify ;;
  plan)   do_plan ;;
  *) dr_die "usage: $0 {build|verify|plan} [options]" ;;
esac
