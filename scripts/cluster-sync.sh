#!/usr/bin/env bash
# Preflight/sync for the two-node DGX Spark cluster: make the WORKER host ready.
#
#   1. the pinned vLLM image the head uses is present on the worker, and so
#      is the pinned python image the worker sentinel runs on
#   2. the main model directory exists on the worker at the same path
#      (rsync; nothing is re-downloaded when the files are already there)
#   3. a worker env file + compose file + the per-rank engine env + the
#      sentinel program are shipped to ~/.techsara-cluster/ (the sentinel:
#      contract §6.4), and the sentinel token is the one the head's
#      engine-controller renders -- checked, never printed
#
# Idempotent and non-destructive: it never deletes anything on either node.
# Usage: scripts/cluster-sync.sh [--image-only|--model-only|--env-only] [--via-link-2]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
# shellcheck source=lib/engine-lock.sh
. "$CLUSTER_LIB_DIR/engine-lock.sh"

# The sentinel program lives with the controller on the head and is copied,
# never edited on the worker (like compose.cluster-worker.yaml itself).
SENTINEL_SRC_DIR="$ROOT/monitoring/engine-controller"
SENTINEL_FILES=(sentinel.py common.py)
# The launcher's per-rank vLLM process environment (candidate B; empty file
# when nothing is set) and the engine controller's own secret layer.
ENGINE_ENV_LOCAL="$RUNTIME_DIR/engine.env"
CONTROLLER_ENV="$RUNTIME_DIR/controller.env"
# The keys the worker's compose file interpolates that the launcher does not
# prefix CLUSTER_ (the healthcheck's last-resort tier, contract §6).
WORKER_PLAIN_KEYS='MAIN_MODEL|MAIN_MODEL_CONTAINER_PATH|MODEL_MAX_CONTEXT|VLLM_PORT|TECHSARA_CLUSTER_MODE|VLLM_HEALTHCHECK_KILL_AFTER'

# The image the sentinel runs on is whatever the head's engine-controller
# service resolves to (digest-pinned in compose.dgx-spark.yaml), read back
# from the rendered config exactly like head_vllm_image does for the engine,
# so the two nodes cannot drift.
head_controller_image() {
  head_compose config --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["engine-controller"]["image"])'
}

# ensure_sentinel_token: the shared secret for the sentinel's POST /restart
# (header X-Sentinel-Token, contract §6.4). ONE source for both nodes:
# .runtime/controller.env (0600), the env_file only the head's
# engine-controller service reads -- never secrets.env, which compose.yaml
# feeds wholesale to the orchestrator. The launcher writes that file on every
# `techsara up` (environment.prepare_controller_secrets) with the same rule
# applied here for a hand run before any `up`: a value the operator set in
# .env wins and is copied in, otherwise the existing token is kept, otherwise
# one is minted. Written under umask 077 so the token never touches a
# permissive inode. Prints the token; never logs it.
ensure_sentinel_token() {
  local token configured
  configured="$(env_get "$ENV_FILE" CLUSTER_SENTINEL_TOKEN 2>/dev/null || true)"
  token="$(env_get "$CONTROLLER_ENV" CLUSTER_SENTINEL_TOKEN 2>/dev/null || true)"
  if [ -n "$configured" ] && [ "$configured" != "$token" ]; then
    token="$configured"
    log_info "CLUSTER_SENTINEL_TOKEN from .env differs from .runtime/controller.env; .env wins on both nodes, rewriting controller.env" >&2
  elif [ -z "$token" ]; then
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    log_info "minted CLUSTER_SENTINEL_TOKEN into .runtime/controller.env (the head's engine-controller reads it as an env_file)" >&2
  else
    printf '%s' "$token"; return 0
  fi
  ( umask 077; mkdir -p "$(dirname "$CONTROLLER_ENV")"
    printf '# Engine controller secret layer (scripts/cluster-sync.sh %s). The launcher rewrites this on every up.\nCLUSTER_SENTINEL_TOKEN=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$token" >"$CONTROLLER_ENV.tmp" && mv -f "$CONTROLLER_ENV.tmp" "$CONTROLLER_ENV" )
  chmod 0600 "$CONTROLLER_ENV"
  printf '%s' "$token"
}

# head_controller_token_sha: sha256 of the token the head's rendered compose
# config gives the engine-controller service (the controller.env env_file
# merged into its environment), so the value shipped to the worker can be
# compared with what the head actually runs without printing either.
head_controller_token_sha() {
  head_compose config --format json 2>/dev/null \
    | python3 -c 'import hashlib,json,sys; e=json.load(sys.stdin)["services"]["engine-controller"].get("environment") or {}; print(hashlib.sha256((e.get("CLUSTER_SENTINEL_TOKEN") or "").encode()).hexdigest())'
}

# running_controller_token_sha: the same digest for the container that is
# RUNNING now (empty when there is none, or when it is stopped -- a stopped
# controller is one the launcher paused for a pair restart and will recreate
# itself once the pair is proven; starting it from here would put a live
# controller beside that restart). Read through docker inspect and hashed
# in-process so the value is never printed.
running_controller_token_sha() {
  docker inspect sf-local-ai-engine-controller-1 --format '{{.State.Running}} {{json .Config.Env}}' 2>/dev/null \
    | python3 -c 'import hashlib,json,sys; running,_,raw=sys.stdin.read().partition(" "); env=dict(x.split("=",1) for x in json.loads(raw or "[]") if "=" in x); print(hashlib.sha256(env.get("CLUSTER_SENTINEL_TOKEN","").encode()).hexdigest() if running.strip()=="true" else "")' 2>/dev/null || true
}

DO_IMAGE=1; DO_MODEL=1; DO_ENV=1; VIA_LINK2=0
for a in "$@"; do
  case "$a" in
    --image-only) DO_MODEL=0; DO_ENV=0 ;;
    --model-only) DO_IMAGE=0; DO_ENV=0 ;;
    --env-only)   DO_IMAGE=0; DO_MODEL=0 ;;
    --via-link-2) VIA_LINK2=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown option: $a" ;;
  esac
done

cluster_load_settings
require_dual_mode
[ -n "${CLUSTER_ENGINE_ARGS:-}" ] || die "$GENERATED_ENV has no CLUSTER_ENGINE_ARGS yet. Run ./techsara up (or scripts/cluster-up.sh, which does this for you) so the launcher generates the cluster keys first."
[ -n "${MAIN_MODEL_CONTAINER_PATH:-}" ] || die "MAIN_MODEL_CONTAINER_PATH missing from $GENERATED_ENV"
[ -n "${TECHSARA_MODEL_CACHE:-}" ] || die "TECHSARA_MODEL_CACHE missing from $GENERATED_ENV"

section "worker host"
remote_host="$(ssh_worker hostname)" || die "cannot ssh to $CLUSTER_WORKER_SSH (set CLUSTER_WORKER_SSH / CLUSTER_WORKER_SSH_OPTS in .env; keys only, no passwords)"
log_info "worker: $remote_host ($CLUSTER_WORKER_SSH)"
ssh_worker "mkdir -p $WORKER_REMOTE_DIR"

if [ "$DO_IMAGE" = 1 ]; then
  section "vLLM image"
  image="$(head_vllm_image)" || die "could not resolve the head vllm image from docker compose config"
  log_info "head image: $image"
  if ssh_worker "docker image inspect '$image' --format '{{.Id}}'" >/dev/null 2>&1; then
    check_pass "image present on worker"
  else
    log_info "pulling on worker (same digest; ~20 GB)..."
    if ssh_worker "docker pull '$image'" >/dev/null; then
      check_pass "image pulled on worker"
    else
      log_info "pull failed; streaming the image over the cluster link with docker save | docker load"
      docker save "$image" | ssh_worker "docker load" || die "could not transfer the image to the worker"
      check_warn "image loaded via docker save/load: it has no registry digest on the worker, so CLUSTER_VLLM_IMAGE will use the image ID instead"
    fi
  fi
  image_id="$(ssh_worker "docker image inspect '$image' --format '{{.Id}}'" 2>/dev/null || true)"
  [ -n "$image_id" ] || image_id="$(docker image inspect "$image" --format '{{.Id}}')"
  local_id="$(docker image inspect "$image" --format '{{.Id}}')"
  if [ "$image_id" = "$local_id" ]; then check_pass "image ID matches head ($local_id)"; else check_fail "image ID differs: head=$local_id worker=$image_id"; fi
  WORKER_IMAGE_REF="$image"
  ssh_worker "docker image inspect '$image'" >/dev/null 2>&1 || WORKER_IMAGE_REF="$local_id"

  section "sentinel image"
  simage="$(head_controller_image)" || die "could not resolve the head engine-controller image from docker compose config (is compose.dgx-spark.yaml current?)"
  log_info "controller image: $simage"
  if ssh_worker "docker image inspect '$simage' --format '{{.Id}}'" >/dev/null 2>&1; then
    check_pass "sentinel image present on worker"
  else
    log_info "pulling on worker (same digest; ~50 MB)..."
    if ssh_worker "docker pull '$simage'" >/dev/null; then
      check_pass "sentinel image pulled on worker"
    else
      log_info "pull failed; streaming the image over the cluster link with docker save | docker load"
      # The head pulls this image only when the launcher starts the
      # controller, which is AFTER this script on a first `up`; a fresh head
      # has nothing to save yet (review round 1).
      docker image inspect "$simage" >/dev/null 2>&1 || docker pull "$simage" >/dev/null || die "the head has no $simage to stream and cannot pull it"
      docker save "$simage" | ssh_worker "docker load" || die "could not transfer the sentinel image to the worker"
      check_warn "sentinel image loaded via docker save/load: no registry digest on the worker, so CLUSTER_SENTINEL_IMAGE will use the image ID"
    fi
  fi
  SENTINEL_IMAGE_REF="$simage"
  ssh_worker "docker image inspect '$simage'" >/dev/null 2>&1 || SENTINEL_IMAGE_REF="$(docker image inspect "$simage" --format '{{.Id}}')"
fi

if [ "$DO_MODEL" = 1 ]; then
  section "model files"
  rel="${MAIN_MODEL_CONTAINER_PATH#/models/}"
  src="$TECHSARA_MODEL_CACHE/$rel"
  dst="$CLUSTER_WORKER_MODEL_CACHE/$rel"
  [ -d "$src" ] || die "main model directory not found on head: $src"
  ssh_worker "mkdir -p '$(dirname "$dst")'"
  target="$CLUSTER_WORKER_SSH"
  if [ "$VIA_LINK2" = 1 ] && [ -n "${CLUSTER_WORKER_IP_2:-}" ]; then
    target="${CLUSTER_WORKER_SSH%@*}@${CLUSTER_WORKER_IP_2}"
    log_info "copying over link 2 ($CLUSTER_WORKER_IP_2)"
  fi
  src_bytes="$(du -sb "$src" | cut -f1)"
  dst_bytes="$(ssh_worker "du -sb '$dst' 2>/dev/null | cut -f1" || true)"
  if [ "${dst_bytes:-0}" = "$src_bytes" ]; then
    check_pass "model already present on worker ($dst, $((src_bytes/1024/1024)) MiB)"
  else
    log_info "rsync $src -> $target:$dst ($((src_bytes/1024/1024)) MiB, resumable)"
    # shellcheck disable=SC2086
    rsync -a --partial --info=progress2 -e "ssh -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-}" "$src/" "$target:$dst/"
    dst_bytes="$(ssh_worker "du -sb '$dst' | cut -f1")"
    if [ "$dst_bytes" = "$src_bytes" ]; then check_pass "model synced ($((src_bytes/1024/1024)) MiB)"; else check_fail "size mismatch after rsync: head=$src_bytes worker=$dst_bytes"; fi
  fi
  for f in config.json tokenizer.json; do
    if ssh_worker "test -f '$dst/$f'"; then check_pass "worker has $f"; else check_fail "worker is missing $dst/$f"; fi
  done
fi

if [ "$DO_ENV" = 1 ]; then
  section "worker sentinel program"
  for f in "${SENTINEL_FILES[@]}"; do
    [ -f "$SENTINEL_SRC_DIR/$f" ] || die "$SENTINEL_SRC_DIR/$f is missing on the head; compose.cluster-worker.yaml bind-mounts it, and Docker would create a DIRECTORY of that name on the worker in its place"
  done
  scp_to_worker "${SENTINEL_FILES[@]/#/$SENTINEL_SRC_DIR/}" ".techsara-cluster"
  for f in "${SENTINEL_FILES[@]}"; do
    if [ "$(ssh_worker "sha256sum $WORKER_REMOTE_DIR/$f 2>/dev/null | cut -d' ' -f1")" = "$(sha256sum "$SENTINEL_SRC_DIR/$f" | cut -d' ' -f1)" ]; then
      check_pass "$f shipped (sha256 matches)"
    else
      check_fail "$f on the worker does not match the head's copy"
    fi
  done

  section "engine process environment (both ranks)"
  # Written by the launcher; an older generated.env has no engine.env yet, in
  # which case an EMPTY file is shipped so the worker's env_file resolves the
  # same way the head's does (required: false on both).
  if [ ! -f "$ENGINE_ENV_LOCAL" ]; then
    printf '# (no CLUSTER_VLLM_* keys set; written by scripts/cluster-sync.sh because the launcher has not run yet)\n' >"$ENGINE_ENV_LOCAL"
  fi
  scp_to_worker "$ENGINE_ENV_LOCAL" ".techsara-cluster"
  if [ "$(ssh_worker "sha256sum $WORKER_REMOTE_DIR/engine.env 2>/dev/null | cut -d' ' -f1")" = "$(sha256sum "$ENGINE_ENV_LOCAL" | cut -d' ' -f1)" ]; then
    check_pass "engine.env shipped (sha256 matches; $(grep -c '^[A-Z]' "$ENGINE_ENV_LOCAL" || true) variable(s))"
  else
    check_fail "engine.env on the worker does not match the head's copy"
  fi

  section "worker environment"
  [ -n "${WORKER_IMAGE_REF:-}" ] || WORKER_IMAGE_REF="$(head_vllm_image)"
  [ -n "${SENTINEL_IMAGE_REF:-}" ] || SENTINEL_IMAGE_REF="$(head_controller_image)"
  sentinel_token="$(ensure_sentinel_token)"
  # Interface/HCA names on the WORKER are detected there (they may differ).
  remote_facts="$(ssh_worker "$(detect_snippet); ifn=\$(detect_ifname_for_ip '$CLUSTER_WORKER_IP'); echo IFNAME=\$ifn; hca=\$(detect_hca_for_ifname \"\$ifn\" 2>/dev/null); echo HCA=\$hca; ifn2=''; hca2=''; if [ -n '${CLUSTER_WORKER_IP_2:-}' ]; then ifn2=\$(detect_ifname_for_ip '${CLUSTER_WORKER_IP_2:-}'); hca2=\$(detect_hca_for_ifname \"\$ifn2\" 2>/dev/null); fi; echo IFNAME2=\$ifn2; echo HCA2=\$hca2")"
  w_ifname="$(printf '%s\n' "$remote_facts" | sed -n 's/^IFNAME=//p')"
  w_hca="$(printf '%s\n' "$remote_facts" | sed -n 's/^HCA=//p')"
  w_hca2="$(printf '%s\n' "$remote_facts" | sed -n 's/^HCA2=//p')"
  w_ifname="${CLUSTER_WORKER_NCCL_SOCKET_IFNAME:-$w_ifname}"
  [ -n "$w_ifname" ] || die "no interface on the worker carries $CLUSTER_WORKER_IP (set CLUSTER_WORKER_NCCL_SOCKET_IFNAME in .env to override)"
  w_hcas="${CLUSTER_WORKER_NCCL_IB_HCA:-$(printf '%s' "$w_hca${w_hca2:+,$w_hca2}")}"
  if [ -n "$w_hcas" ]; then check_pass "worker RDMA HCAs: $w_hcas (iface $w_ifname)"; else check_warn "no RDMA HCA found for the worker interfaces; NCCL will use TCP sockets"; fi
  # The file carries the token, so it is CREATED 0600 (install, not touch-
  # then-chmod) and written under umask 077: the previous 0644 inode in a
  # group-writable .runtime/ is replaced, never reused (review round 1).
  rm -f "$WORKER_ENV_LOCAL"
  install -m 0600 /dev/null "$WORKER_ENV_LOCAL"
  ( umask 077; {
    echo "# Generated by scripts/cluster-sync.sh on $(hostname) at $(date -Is). Do not edit; edit .env on the head and re-run."
    grep -E "^($WORKER_PLAIN_KEYS|CLUSTER_[A-Z0-9_]+)=" "$GENERATED_ENV"
    echo "CLUSTER_VLLM_IMAGE=$WORKER_IMAGE_REF"
    echo "CLUSTER_SENTINEL_IMAGE=$SENTINEL_IMAGE_REF"
    echo "CLUSTER_WORKER_MODEL_CACHE=$CLUSTER_WORKER_MODEL_CACHE"
    echo "CLUSTER_WORKER_NCCL_SOCKET_IFNAME=$w_ifname"
    echo "CLUSTER_WORKER_NCCL_IB_HCA=$w_hcas"
    echo "CLUSTER_WORKER_SSH=$CLUSTER_WORKER_SSH"
    # generated.env never carries the token (it lives in controller.env), so
    # it is appended exactly once, last.
    echo "CLUSTER_SENTINEL_TOKEN=$sentinel_token"
  } > "$WORKER_ENV_LOCAL" )
  # scp does not reliably carry mode bits across (SFTP mode), so the remote
  # copy is fixed up explicitly, in the same command that moves it into place.
  scp_to_worker "$WORKER_COMPOSE_LOCAL" "$WORKER_ENV_LOCAL" ".techsara-cluster"
  ssh_worker "cd $WORKER_REMOTE_DIR && chmod 0600 cluster-worker.env && mv -f cluster-worker.env worker.env"
  if worker_compose config --quiet; then check_pass "worker compose config validates on $remote_host"; else check_fail "worker compose config is invalid (see above)"; fi

  # THE TWO NODES MUST AGREE ON THE TOKEN (contract §6.4). Compared as
  # digests: the head's rendered engine-controller environment (env_file
  # merged in) against what was just written for the worker.
  shipped_sha="$(printf '%s' "$sentinel_token" | sha256sum | cut -d' ' -f1)"
  head_sha="$(head_controller_token_sha 2>/dev/null || true)"
  if [ -n "$head_sha" ] && [ "$head_sha" = "$shipped_sha" ]; then
    check_pass "sentinel token shipped in worker.env (0600) equals the one the head's engine-controller renders"
  else
    check_fail "the token shipped to the worker is NOT what the head's compose renders for engine-controller (is .runtime/controller.env the file the chain names?)"
  fi
  # A controller that is RUNNING with another token (or none: a routine
  # deploy that started it before the first sync) would get 403 on every
  # POST /restart and fall back to head-only recoveries; recreate it now,
  # under the engine lock so a recovery in progress is waited out (the
  # launcher's own `up` exports ENGINE_LOCK_HELD_BY and then this is a
  # no-op re-entry). Not when there is no controller container yet: the
  # launcher creates it after the pair is proven.
  running_sha="$(running_controller_token_sha)"
  if [ -n "$running_sha" ] && [ "$running_sha" != "$shipped_sha" ]; then
    if engine_lock_acquire "${ENGINE_LOCK_WAIT:-600}" "cluster-sync.sh (recreate engine-controller with the shipped token)"; then
      if head_compose up -d --no-deps engine-controller >/dev/null 2>&1; then
        check_pass "engine-controller recreated with the shipped token"
      else
        check_fail "could not recreate engine-controller; run ./techsara up (routine) so it is created with the token"
      fi
      engine_lock_release
    else
      check_fail "engine lock held; engine-controller keeps its old token until ./techsara up recreates it"
    fi
  fi
  log_dim "$(sed -n '1,3p' "$WORKER_ENV_LOCAL")"
fi

check_summary
