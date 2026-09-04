#!/usr/bin/env bash
# Speech-to-text (Qwen3-ASR) for the composer's microphone.
#
#   scripts/asr.sh up       fetch the weights, build the image and start the
#                           engine on the node that has room for it
#   scripts/asr.sh up --all-nodes
#                           a second engine on the head as well, and route
#                           between them — see ASR_NODES below
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
# TWO ENGINES (ASR_NODES=worker,head or --all-nodes). REPLICAS, not shards.
# Splitting one 1.7B model across two Sparks would put every layer on a
# 13 Gb/s RoCE link the main model already uses, to save memory that was
# never short — the weights are 4.4 GB and either node holds them twice over.
# Two whole copies with least-active routing add throughput without one byte
# of cross-node traffic.
#
# Measured before doing it: one engine saturates at EIGHT concurrent clips
# (~123 seconds of audio per wall-second) and past that latency grows
# linearly — 1.0s at 8, 1.6s at 16, 2.7s at 32. A second engine is worth
# having only if a workspace really does dictate that concurrently, and it
# costs the head node's chat throughput to run one there. Start with one.
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
#: Which nodes carry an engine: "worker", "head", or both. Defaults to the
#: worker alone in dual mode — the head is the loaded machine.
ASR_NODES="${ASR_NODES:-}"

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

# The nodes to run on, resolved once. In single mode there is only ever this
# one, whatever anybody asked for.
asr_nodes() {
  if ! is_dual_mode; then printf 'head'; return; fi
  if [ -n "$ASR_NODES" ]; then printf '%s' "${ASR_NODES//,/ }"; return; fi
  printf 'worker'
}

asr_host_label() { # asr_host_label <node>
  case "$1" in
    worker) printf 'worker (%s)' "$CLUSTER_WORKER_SSH" ;;
    *)      printf 'head (this node)' ;;
  esac
}

run_on() { # run_on <node> <<'EOS' ... EOS
  if [ "$1" = worker ] && is_dual_mode; then
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
asr_bind_address() { # asr_bind_address <node>
  if [ -n "${ASR_BIND:-}" ] && [ "$1" = worker ]; then printf '%s' "$ASR_BIND"; return; fi
  if [ "$1" = worker ] && is_dual_mode; then
    run_on worker <<'EOS' | tr -d '[:space:]'
ip -4 -br addr show enP7s7 2>/dev/null | awk '{print $3}' | cut -d/ -f1
EOS
  else
    # The head's engine is reached by the orchestrator over the docker bridge,
    # so it binds the gateway address rather than loopback, which a container
    # cannot reach.
    printf '%s' "${ASR_HEAD_BIND:-172.17.0.1}"
  fi
}

# ------------------------------------------------------------------ weights --

ensure_weights() { # ensure_weights <node>
  local node="$1" dir; dir="$(model_dir_name)"
  log_info "checking for $ASR_MODEL on $(asr_host_label "$node")"
  if run_on "$node" <<EOS >/dev/null 2>&1
test -f "$ASR_MODEL_CACHE/repos/$dir/config.json"
EOS
  then
    check_pass "weights present on $node ($dir)"
    return 0
  fi
  log_info "fetching $ASR_MODEL@${ASR_MODEL_REVISION:0:12} (~4.4 GB) onto $node"
  run_on "$node" <<EOS || die "could not fetch the model weights onto $node"
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
  check_pass "weights fetched onto $node"
}

# -------------------------------------------------------------------- files --

sync_files() { # sync_files <node>
  [ "$1" = worker ] || return 0
  is_dual_mode || return 0
  log_info "syncing the ASR stack to $CLUSTER_WORKER_SSH"
  # </dev/null for the same reason as asr_compose — see the note there.
  ssh_worker "mkdir -p $REMOTE_DIR/asr" </dev/null
  local remote_dir; remote_dir="$(ssh_worker "echo $REMOTE_DIR" </dev/null)"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/compose.asr-worker.yaml" "$CLUSTER_WORKER_SSH:$remote_dir/" \
    || die "could not copy the compose file to the worker"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/asr/Dockerfile" "$CLUSTER_WORKER_SSH:$remote_dir/asr/" \
    || die "could not copy the Dockerfile to the worker"
  check_pass "stack synced to the worker"
}

asr_compose() { # asr_compose <node> <bind> <args...>
  local node="$1" bind="$2"; shift 2
  local env="ASR_BIND=$bind ASR_PORT=$ASR_PORT ASR_MODEL='$ASR_MODEL' \
ASR_MODEL_CONTAINER_PATH=/models/repos/$(model_dir_name) \
ASR_MODEL_CACHE='$ASR_MODEL_CACHE' \
ASR_GPU_MEMORY_UTILIZATION=$ASR_GPU_MEMORY_UTILIZATION \
ASR_BASE_IMAGE='$ASR_BASE_IMAGE'"
  if [ "$node" = worker ] && is_dual_mode; then
    # </dev/null is load-bearing. Without it ssh reads the caller's stdin —
    # which, inside `while read ... done < <(each_node)`, is the LIST OF
    # NODES. The first worker command swallowed the rest of the fleet and the
    # loop ran exactly once, silently deploying to one machine when two were
    # asked for.
    ssh_worker "cd $REMOTE_DIR && $env docker compose \
      --project-name $ASR_PROJECT -f compose.asr-worker.yaml $*" </dev/null
  else
    ( cd "$ROOT/compose" && eval "$env" docker compose \
        --project-name "$ASR_PROJECT" -f compose.asr-worker.yaml "$@" )
  fi
}

# ------------------------------------------------------------------ readiness --

wait_ready() { # wait_ready <node> <bind> <seconds>
  local node="$1" bind="$2" budget="${3:-600}" waited=0
  log_info "waiting for the $node engine to load"
  while [ "$waited" -lt "$budget" ]; do
    if curl -fsS -m 4 "http://$bind:$ASR_PORT/v1/models" >/dev/null 2>&1; then
      check_pass "$node engine ready on http://$bind:$ASR_PORT"
      return 0
    fi
    sleep 5; waited=$((waited + 5))
  done
  check_fail "the $node engine did not come up within ${budget}s"
  asr_compose "$node" "$bind" logs --tail 30 vllm-asr || true
  return 1
}

# The orchestrator reads ASR_BASE_URLS (comma-separated) — or ASR_BASE_URL for
# a single engine. Recording them in .env, the first --env-file Compose reads,
# is what makes the fleet survive a restart of the stack.
record_endpoints() { # record_endpoints <url> [<url>...]
  local urls; urls="$(printf '%s,' "$@")"; urls="${urls%,}"
  local env_file="$ROOT/.env"
  touch "$env_file"
  local first="$1"
  _set_env ASR_ENABLED true
  _set_env ASR_BASE_URL "$first"
  _set_env ASR_BASE_URLS "$urls"
  check_pass "recorded ASR_BASE_URLS=$urls in .env"
  log_info "restart the orchestrator to pick it up:  ./techsara up"
}

_set_env() { # _set_env KEY VALUE — idempotent, in .env
  local key="$1" value="$2" env_file="$ROOT/.env"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

# Every node the fleet runs on, as "node bind" pairs.
each_node() {
  local node
  for node in $(asr_nodes); do
    printf '%s %s\n' "$node" "$(asr_bind_address "$node")"
  done
}

# ----------------------------------------------------------------- commands --

cmd="${1:-status}"; shift || true

# `up --all-nodes` is the shorthand for ASR_NODES=worker,head.
for arg in "$@"; do
  [ "$arg" = --all-nodes ] && ASR_NODES="worker,head"
done
set -- "${@/--all-nodes/}"

case "$cmd" in
  up)
    urls=()
    # Read the fleet into an array before touching it. A `while read` fed by
    # a process substitution is one stray `ssh` away from losing its list;
    # this cannot be.
    mapfile -t fleet < <(each_node)
    for entry in "${fleet[@]}"; do
      read -r node bind <<<"$entry"
      [ -n "$bind" ] || die "could not determine the address for $node (set ASR_BIND / ASR_HEAD_BIND)"
      log_info "speech-to-text on $(asr_host_label "$node"), bound to $bind:$ASR_PORT"
      ensure_weights "$node"
      sync_files "$node"
      asr_compose "$node" "$bind" up -d --build vllm-asr || die "compose up failed on $node"
      wait_ready "$node" "$bind" "${ASR_START_BUDGET_S:-900}" || exit 1
      urls+=("http://$bind:$ASR_PORT/v1")
    done
    record_endpoints "${urls[@]}"
    [ "${#urls[@]}" -gt 1 ] && log_info "requests will go to whichever engine has the fewest in flight"
    ;;
  down)
    while read -r node bind; do
      asr_compose "$node" "$bind" down --remove-orphans || true
      check_pass "speech-to-text stopped on $node (weights and image kept)"
    done < <(each_node)
    ;;
  stop|restart)
    while read -r node bind; do
      asr_compose "$node" "$bind" "$cmd" || true
    done < <(each_node)
    ;;
  logs)
    while read -r node bind; do
      printf '\n--- %s ---\n' "$node"
      asr_compose "$node" "$bind" logs --tail "${ASR_LOG_TAIL:-120}" || true
    done < <(each_node)
    ;;
  status)
    printf 'model  : %s@%s\n\n' "$ASR_MODEL" "${ASR_MODEL_REVISION:0:12}"
    while read -r node bind; do
      printf '%s — %s:%s\n' "$(asr_host_label "$node")" "${bind:-?}" "$ASR_PORT"
      asr_compose "$node" "$bind" ps 2>/dev/null | tail -n +2 || true
      if curl -fsS -m 5 "http://$bind:$ASR_PORT/v1/models" >/dev/null 2>&1; then
        check_pass "answering on http://$bind:$ASR_PORT"
      else
        check_warn "not answering on http://$bind:$ASR_PORT"
      fi
      printf '\n'
    done < <(each_node)
    ;;
  url)
    while read -r node bind; do printf 'http://%s:%s/v1\n' "$bind" "$ASR_PORT"; done < <(each_node)
    ;;
  verify)
    read -r node bind < <(each_node)
    python3 "$ROOT/scripts/asr_bench.py" --base-url "http://$bind:$ASR_PORT/v1" \
      --model "$ASR_MODEL" --verify "$@"
    ;;
  bench)
    read -r node bind < <(each_node)
    python3 "$ROOT/scripts/asr_bench.py" --base-url "http://$bind:$ASR_PORT/v1" \
      --model "$ASR_MODEL" "$@"
    ;;
  *)
    sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
