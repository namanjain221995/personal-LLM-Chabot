#!/usr/bin/env bash
# Speech-to-text (Qwen3-ASR) for the composer's microphone.
#
#   scripts/asr.sh up       fetch the weights, build the image and start the
#                           engine on the node that has room for it
#   scripts/asr.sh down     stop and remove it (the weights stay)
#   scripts/asr.sh stop     stop without removing
#   scripts/asr.sh status   container state + the engine's own /v1/models
#   scripts/asr.sh logs     follow the engine log
#   scripts/asr.sh verify   transcribe a real clip end to end and print it
#   scripts/asr.sh bench    latency at 5 / 15 / 30 / 60 seconds of audio
#   scripts/asr.sh url      print the endpoint the orchestrator should use
#
# NOTHING HERE TOUCHES THE LLM. Speech-to-text is its own Compose project on
# its own port; `down` leaves vLLM, the orchestrator and the frontend running,
# and in dual mode it never comes near sf-local-ai-worker, which is the main
# model's tensor-parallel rank 1.
#
# WHERE IT LANDS. In dual mode: the worker (Spark 2), because that is where
# the memory is — measured 2026-09-04, the head had 76.1 GB of GPU memory
# allocated to 30.0 GB on the worker. In single mode: this node, since there
# is nowhere else. Either way the engine binds ONE address and the
# orchestrator is told which.
#
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

ASR_PROJECT="${ASR_PROJECT:-sf-local-ai-asr}"
ASR_PORT="${ASR_PORT:-30006}"
ASR_MODEL="${ASR_MODEL:-Qwen/Qwen3-ASR-1.7B}"
# Pinned, like every other model in config/model-manifest.yaml: a moving
# "main" would change what the platform transcribes with, silently.
ASR_MODEL_REVISION="${ASR_MODEL_REVISION:-7278e1e70fe206f11671096ffdd38061171dd6e5}"
# The model cache ON THE HOST THAT WILL SERVE. In dual mode that is the
# worker's, which mirrors the head's layout (cluster-sync.sh copies the main
# model into the same path), so $HOME resolves correctly on either machine.
ASR_MODEL_CACHE="${ASR_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}"
ASR_GPU_MEMORY_UTILIZATION="${ASR_GPU_MEMORY_UTILIZATION:-0.08}"
ASR_BASE_IMAGE="${ASR_BASE_IMAGE:-vllm/vllm-openai:nightly}"
REMOTE_DIR="${WORKER_REMOTE_DIR:-\$HOME/.techsara-cluster}"

is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }

# The directory the weights live in, under <cache>/repos/. Same slug the
# launcher's model manager uses: org--name--<12 chars of the revision>.
model_dir_name() {
  printf '%s--%s' "${ASR_MODEL//\//--}" "${ASR_MODEL_REVISION:0:12}"
}

# ---------------------------------------------------------------- placement --
#
# Every command needs to know two things: which host runs the engine, and
# which address it binds. `run_there` hides the difference so the rest of the
# script never branches on it again.

asr_host_label() { if is_dual_mode; then printf 'worker (%s)' "$CLUSTER_WORKER_SSH"; else printf 'this node'; fi; }

run_there() { # run_there <<'EOS' ... EOS   — run a script on the ASR host
  if is_dual_mode; then
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 \
      ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" "bash -s"
  else
    bash -s
  fi
}

# The address the engine binds. In dual mode that is the worker's MANAGEMENT
# address — never a 10.100.x RoCE address, which belongs to the tensor-parallel
# fabric this must not compete with. Overridable for a host whose management
# NIC is not enP7s7.
asr_bind_address() {
  if [ -n "${ASR_BIND:-}" ]; then printf '%s' "$ASR_BIND"; return; fi
  if is_dual_mode; then
    run_there <<'EOS' | tr -d '[:space:]'
ip -4 -br addr show enP7s7 2>/dev/null | awk '{print $3}' | cut -d/ -f1
EOS
  else
    printf '127.0.0.1'
  fi
}

# ------------------------------------------------------------------ weights --

ensure_weights() { # ensure_weights <bind>
  local dir; dir="$(model_dir_name)"
  log_info "checking for $ASR_MODEL on $(asr_host_label)"
  if run_there <<EOS >/dev/null 2>&1
test -f "$ASR_MODEL_CACHE/repos/$dir/config.json"
EOS
  then
    check_pass "weights already present ($dir)"
    return 0
  fi
  log_info "fetching $ASR_MODEL@${ASR_MODEL_REVISION:0:12} (~4.4 GB) — once, then cached"
  run_there <<EOS || die "could not fetch the model weights"
set -e
docker run --rm \
  -v "$ASR_MODEL_CACHE":/models \
  --entrypoint python3 "$ASR_BASE_IMAGE" -c "
from huggingface_hub import snapshot_download
snapshot_download('$ASR_MODEL', revision='$ASR_MODEL_REVISION',
                  local_dir='/models/repos/$dir')
print('weights ready')
"
EOS
  check_pass "weights fetched"
}

# -------------------------------------------------------------------- files --

sync_files() {
  is_dual_mode || return 0
  log_info "syncing the ASR stack to $CLUSTER_WORKER_SSH"
  ssh_worker "mkdir -p $REMOTE_DIR/asr"
  local remote_dir; remote_dir="$(ssh_worker "echo $REMOTE_DIR")"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/compose.asr-worker.yaml" "$CLUSTER_WORKER_SSH:$remote_dir/" \
    || die "could not copy the compose file to the worker"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/asr/Dockerfile" "$CLUSTER_WORKER_SSH:$remote_dir/asr/" \
    || die "could not copy the Dockerfile to the worker"
  check_pass "stack synced"
}

asr_compose() { # asr_compose <bind> <args...>
  local bind="$1"; shift
  local env="ASR_BIND=$bind ASR_PORT=$ASR_PORT ASR_MODEL='$ASR_MODEL' \
ASR_MODEL_CONTAINER_PATH=/models/repos/$(model_dir_name) \
ASR_MODEL_CACHE='$ASR_MODEL_CACHE' \
ASR_GPU_MEMORY_UTILIZATION=$ASR_GPU_MEMORY_UTILIZATION \
ASR_BASE_IMAGE='$ASR_BASE_IMAGE'"
  if is_dual_mode; then
    ssh_worker "cd $REMOTE_DIR && $env docker compose \
      --project-name $ASR_PROJECT -f compose.asr-worker.yaml $*"
  else
    ( cd "$ROOT/compose" && eval "$env" docker compose \
        --project-name "$ASR_PROJECT" -f compose.asr-worker.yaml "$@" )
  fi
}

# ------------------------------------------------------------------ readiness --

wait_ready() { # wait_ready <bind> <seconds>
  local bind="$1" budget="${2:-600}" waited=0
  log_info "waiting for the engine to load (first start takes a few minutes)"
  while [ "$waited" -lt "$budget" ]; do
    if curl -fsS -m 4 "http://$bind:$ASR_PORT/v1/models" >/dev/null 2>&1; then
      check_pass "engine ready on http://$bind:$ASR_PORT"
      return 0
    fi
    sleep 5; waited=$((waited + 5))
  done
  check_fail "the engine did not come up within ${budget}s"
  asr_compose "$bind" logs --tail 30 vllm-asr || true
  return 1
}

# The orchestrator reads ASR_BASE_URL. Recording it in .env — the first
# --env-file Compose reads, and the file that already holds this deployment's
# intent — is what makes the endpoint survive a restart of the stack.
record_endpoint() { # record_endpoint <bind>
  local url="http://$1:$ASR_PORT/v1"
  local env_file="$ROOT/.env"
  touch "$env_file"
  if grep -q '^ASR_BASE_URL=' "$env_file"; then
    local current; current="$(grep '^ASR_BASE_URL=' "$env_file" | head -1 | cut -d= -f2-)"
    [ "$current" = "$url" ] && { check_pass "ASR_BASE_URL already correct"; return 0; }
    sed -i "s|^ASR_BASE_URL=.*|ASR_BASE_URL=$url|" "$env_file"
  else
    printf '\n# Speech-to-text endpoint (written %s by scripts/asr.sh).\nASR_ENABLED=true\nASR_BASE_URL=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$url" >>"$env_file"
  fi
  check_pass "recorded ASR_BASE_URL=$url in .env"
  log_info "restart the orchestrator to pick it up:  ./techsara up"
}

# ----------------------------------------------------------------- commands --

cmd="${1:-status}"; shift || true

case "$cmd" in
  up)
    bind="$(asr_bind_address)"
    [ -n "$bind" ] || die "could not determine the address to bind (set ASR_BIND)"
    log_info "speech-to-text will run on $(asr_host_label), bound to $bind:$ASR_PORT"
    ensure_weights "$bind"
    sync_files
    asr_compose "$bind" up -d --build vllm-asr "$@" || die "compose up failed"
    wait_ready "$bind" "${ASR_START_BUDGET_S:-900}" || exit 1
    record_endpoint "$bind"
    ;;
  down)
    bind="$(asr_bind_address)"
    asr_compose "$bind" down --remove-orphans "$@"
    check_pass "speech-to-text stopped (weights and image kept)"
    ;;
  stop|restart)
    bind="$(asr_bind_address)"
    asr_compose "$bind" "$cmd" "$@"
    ;;
  logs)
    bind="$(asr_bind_address)"
    asr_compose "$bind" logs --tail "${ASR_LOG_TAIL:-120}" "$@"
    ;;
  status)
    bind="$(asr_bind_address)"
    printf 'host   : %s\nbind   : %s:%s\nmodel  : %s@%s\n\n' \
      "$(asr_host_label)" "${bind:-?}" "$ASR_PORT" "$ASR_MODEL" "${ASR_MODEL_REVISION:0:12}"
    asr_compose "$bind" ps || true
    printf '\n'
    if curl -fsS -m 5 "http://$bind:$ASR_PORT/v1/models" >/dev/null 2>&1; then
      check_pass "engine answering on http://$bind:$ASR_PORT"
    else
      check_warn "engine not answering on http://$bind:$ASR_PORT"
    fi
    ;;
  url)
    printf 'http://%s:%s/v1\n' "$(asr_bind_address)" "$ASR_PORT"
    ;;
  verify)
    bind="$(asr_bind_address)"
    python3 "$ROOT/scripts/asr_bench.py" --base-url "http://$bind:$ASR_PORT/v1" \
      --model "$ASR_MODEL" --verify "$@"
    ;;
  bench)
    bind="$(asr_bind_address)"
    python3 "$ROOT/scripts/asr_bench.py" --base-url "http://$bind:$ASR_PORT/v1" \
      --model "$ASR_MODEL" "$@"
    ;;
  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
