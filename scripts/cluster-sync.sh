#!/usr/bin/env bash
# Preflight/sync for the two-node DGX Spark cluster: make the WORKER host ready.
#
#   1. the pinned vLLM image the head uses is present on the worker
#   2. the main model directory exists on the worker at the same path
#      (rsync; nothing is re-downloaded when the files are already there)
#   3. a worker env file + compose file are shipped to ~/.techsara-cluster/
#
# Idempotent and non-destructive: it never deletes anything on either node.
# Usage: scripts/cluster-sync.sh [--image-only|--model-only|--env-only] [--via-link-2]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

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
    ssh_worker "test -f '$dst/$f'" && check_pass "worker has $f" || check_fail "worker is missing $dst/$f"
  done
fi

if [ "$DO_ENV" = 1 ]; then
  section "worker environment"
  [ -n "${WORKER_IMAGE_REF:-}" ] || WORKER_IMAGE_REF="$(head_vllm_image)"
  # Interface/HCA names on the WORKER are detected there (they may differ).
  remote_facts="$(ssh_worker "$(detect_snippet); ifn=\$(detect_ifname_for_ip '$CLUSTER_WORKER_IP'); echo IFNAME=\$ifn; hca=\$(detect_hca_for_ifname \"\$ifn\" 2>/dev/null); echo HCA=\$hca; ifn2=''; hca2=''; if [ -n '${CLUSTER_WORKER_IP_2:-}' ]; then ifn2=\$(detect_ifname_for_ip '${CLUSTER_WORKER_IP_2:-}'); hca2=\$(detect_hca_for_ifname \"\$ifn2\" 2>/dev/null); fi; echo IFNAME2=\$ifn2; echo HCA2=\$hca2")"
  w_ifname="$(printf '%s\n' "$remote_facts" | sed -n 's/^IFNAME=//p')"
  w_hca="$(printf '%s\n' "$remote_facts" | sed -n 's/^HCA=//p')"
  w_hca2="$(printf '%s\n' "$remote_facts" | sed -n 's/^HCA2=//p')"
  w_ifname="${CLUSTER_WORKER_NCCL_SOCKET_IFNAME:-$w_ifname}"
  [ -n "$w_ifname" ] || die "no interface on the worker carries $CLUSTER_WORKER_IP (set CLUSTER_WORKER_NCCL_SOCKET_IFNAME in .env to override)"
  w_hcas="${CLUSTER_WORKER_NCCL_IB_HCA:-$(printf '%s' "$w_hca${w_hca2:+,$w_hca2}")}"
  [ -n "$w_hcas" ] && check_pass "worker RDMA HCAs: $w_hcas (iface $w_ifname)" || check_warn "no RDMA HCA found for the worker interfaces; NCCL will use TCP sockets"
  {
    echo "# Generated by scripts/cluster-sync.sh on $(hostname) at $(date -Is). Do not edit; edit .env on the head and re-run."
    grep -E '^(MAIN_MODEL|MAIN_MODEL_CONTAINER_PATH|MODEL_MAX_CONTEXT|VLLM_PORT|TECHSARA_CLUSTER_MODE|CLUSTER_[A-Z0-9_]+)=' "$GENERATED_ENV"
    echo "CLUSTER_VLLM_IMAGE=$WORKER_IMAGE_REF"
    echo "CLUSTER_WORKER_MODEL_CACHE=$CLUSTER_WORKER_MODEL_CACHE"
    echo "CLUSTER_WORKER_NCCL_SOCKET_IFNAME=$w_ifname"
    echo "CLUSTER_WORKER_NCCL_IB_HCA=$w_hcas"
    echo "CLUSTER_WORKER_SSH=$CLUSTER_WORKER_SSH"
  } > "$WORKER_ENV_LOCAL"
  chmod 0644 "$WORKER_ENV_LOCAL"
  scp_to_worker "$WORKER_COMPOSE_LOCAL" "$WORKER_ENV_LOCAL" ".techsara-cluster"
  ssh_worker "cd $WORKER_REMOTE_DIR && mv -f cluster-worker.env worker.env"
  if worker_compose config --quiet; then check_pass "worker compose config validates on $remote_host"; else check_fail "worker compose config is invalid (see above)"; fi
  log_dim "$(sed -n '1,3p' "$WORKER_ENV_LOCAL")"
fi

check_summary
