#!/usr/bin/env bash
# Speech-to-text (openai/whisper-large-v3) for the composer's microphone.
#
#   scripts/whisper.sh up       fetch the weights, build the image and start
#                               the engine on the node that has room for it
#   scripts/whisper.sh up --all-nodes
#                               a second engine on the head as well, and route
#                               between them — see WHISPER_NODES below
#   scripts/whisper.sh down     stop and remove it (the weights stay), and
#                               drop it from ASR_BASE_URLS in .env
#   scripts/whisper.sh status   container state + the engine's own /health
#   scripts/whisper.sh logs     follow the engine log
#   scripts/whisper.sh verify   transcribe a real clip end to end and print it
#   scripts/whisper.sh url      print the endpoint the orchestrator should use
#
# THIS EXISTS BECAUSE THE DEPLOYMENT DID NOT. The engine was running on the
# worker and serving correctly, but nothing that produced it was in git: the
# compose file and the server lived only in the worker's ~/.techsara-cluster,
# so the service could not be rebuilt, reviewed or rolled back. Everything
# here is recovered from that live deployment.
#
# NOTHING HERE TOUCHES THE LLM. Speech-to-text is its own Compose project on
# its own port; `down` leaves vLLM, the orchestrator and the frontend running,
# and in dual mode it never comes near sf-local-ai-worker, which is the main
# model's tensor-parallel rank 1.
#
# WHERE IT LANDS. In dual mode: the worker (Spark 2), because that is where
# the memory is — measured 2026-09-08, the head had 21 GB free against 64 GB
# on the worker. In single mode: this node, since there is nowhere else.
#
# TWO ENGINES (WHISPER_NODES=worker,head or --all-nodes). REPLICAS, not
# shards. whisper-large-v3 is 1.55B parameters, 3.1 GB in float16; either node
# holds it several times over. Splitting one across two Sparks would put every
# layer's activations on the RoCE link the main model's tensor-parallel
# traffic already uses, to save memory that was never short.
#
# AND BE PRECISE ABOUT WHAT THE SECOND ENGINE BUYS: a second concurrent
# SPEAKER, not a faster transcript. One clip is decoded by one engine. Two
# nodes double how many people can dictate at once; they do not halve the time
# any one of them waits. Start with one, and add the second when a workspace
# really does dictate concurrently — it costs the head node's chat throughput.
set -euo pipefail

# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

WHISPER_PROJECT="${WHISPER_PROJECT:-sf-local-ai-whisper}"
WHISPER_PORT="${WHISPER_PORT:-30007}"
WHISPER_MODEL="${WHISPER_MODEL:-openai/whisper-large-v3}"
# Pinned, like every other model in config/model-manifest.yaml: a moving
# "main" would change what the platform transcribes with, silently.
WHISPER_MODEL_REVISION="${WHISPER_MODEL_REVISION:-06f233fe06e710322aca913c1bc4249a0d71fce1}"
WHISPER_MODEL_CACHE="${WHISPER_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}"
WHISPER_BASE_IMAGE="${WHISPER_BASE_IMAGE:-vllm/vllm-openai:nightly}"
REMOTE_DIR="${WORKER_REMOTE_DIR:-\$HOME/.techsara-cluster}"
#: Which nodes carry an engine: "worker", "head", or both. Defaults to the
#: worker alone in dual mode — the head is the loaded machine.
WHISPER_NODES="${WHISPER_NODES:-}"

is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }

# The directory the weights live in, under <cache>/repos/. Same slug the
# launcher's model manager uses: org--name--<12 chars of the revision>.
model_dir_name() {
  printf '%s--%s' "${WHISPER_MODEL//\//--}" "${WHISPER_MODEL_REVISION:0:12}"
}

# WHICH NODES THIS COMMAND ACTS ON, in priority order:
#   1. WHISPER_NODES, when the caller says explicitly.
#   2. WHAT IS ACTUALLY RUNNING, read back from ASR_BASE_URLS in .env.
#   3. the worker, which is where a first `up` puts it.
#
# Step 2 is the one worth explaining. Without it `up --all-nodes` starts two
# engines and then `status`, `logs`, `url` and `down` all quietly act on the
# worker alone — so `status` reports a healthy fleet while an engine nobody
# can see is still holding a GPU, and `down` leaves it running. The commands
# that INSPECT or STOP the fleet must default to the fleet that exists, not to
# the fleet a fresh install would have.
whisper_nodes() {
  if ! is_dual_mode; then printf 'head'; return; fi
  if [ -n "$WHISPER_NODES" ]; then printf '%s' "${WHISPER_NODES//,/ }"; return; fi
  local recorded node_list="" url
  recorded="$(grep -E '^ASR_BASE_URLS=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr ',' ' ')"
  for url in $recorded; do
    case "$url" in
      *"$(whisper_bind_address head)"*) node_list="$node_list head" ;;
      *) node_list="$node_list worker" ;;
    esac
  done
  # Deduplicate, keeping the worker first so `logs` (which takes the first
  # node) follows the engine that is always present.
  local out=""
  for node in worker head; do
    case " $node_list " in *" $node "*) out="$out $node" ;; esac
  done
  if [ -n "$out" ]; then printf '%s' "${out# }"; return; fi
  printf 'worker'
}

whisper_host_label() {
  case "$1" in
    worker) printf 'worker (%s)' "$CLUSTER_WORKER_SSH" ;;
    *)      printf 'head (this node)' ;;
  esac
}

run_on() { # run_on <node> — script on stdin
  if [ "$1" = worker ] && is_dual_mode; then
    # </dev/null on the OUTER ssh so it cannot eat this script's stdin.
    ssh -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" "bash -s"
  else
    bash -s
  fi
}

# The address the engine binds. On the worker: its MANAGEMENT address (the
# enP7s7 address, 192.168.9.68 today), read over ssh -- never 0.0.0.0, and
# never a 10.100.x RoCE address.
#
# WHY NOT THE RAIL (owner decision, option A, 2026-09-13). Commit 229031c
# moved this bind to CLUSTER_WORKER_IP after the developer-platform audit
# (F051) reached the unauthenticated server from the office LAN. Every
# consumer dials the management address: the running orchestrator and the e2e
# orchestrator carry ASR_BASE_URL/ASR_BASE_URLS=http://192.168.9.68:30007/v1
# (this script does not recreate them), and scripts/asr_language_probe.py
# defaults to it. Worse, record_endpoints MERGES, so a rail bind would have
# left the dead 192.168.9.68 entry FIRST in ASR_BASE_URLS and dictation
# round-robining onto a refused address even after an orchestrator recreate.
# A rail bind is not a boundary either (weak-host model: a LAN host routing
# 10.100.184.2 via the worker reaches it on enP7s7). So the engine stays where
# production runs it, and the LAN/tailnet exposure is closed by the host
# packet filter (scripts/host-guard.sh, operator actions OA-4/OA-6: 30007
# accepted on enP7s7 from the head 192.168.9.54 only, dropped on tailscale0).
# Do not rebind it without first moving every consumer above.
#
# The old ssh read printed NOTHING when it failed, and the engine was started
# with an empty WHISPER_BIND; 229031c made that stop here, and so does this.
whisper_bind_address() {
  local address
  if [ "$1" = worker ] && is_dual_mode; then
    address="$(ssh_worker "ip -4 -br addr show ${WHISPER_MANAGEMENT_IFNAME:-enP7s7} 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" </dev/null)" || address=""
    case "$address" in
      "") die "could not read the worker's ${WHISPER_MANAGEMENT_IFNAME:-enP7s7} address over ssh ($CLUSTER_WORKER_SSH); set WHISPER_MANAGEMENT_IFNAME if its management interface is named differently" ;;
      0.0.0.0|::|"[::]") die "the worker's ${WHISPER_MANAGEMENT_IFNAME:-enP7s7} address read back as '$address'; the speech engine never binds a wildcard" ;;
    esac
    printf '%s' "$address"
  else
    # The head's engine is reached by the orchestrator over the docker bridge,
    # so it binds the gateway address rather than loopback, which a container
    # cannot reach.
    printf '%s' "${WHISPER_HEAD_BIND:-172.17.0.1}"
  fi
}

# ------------------------------------------------------------------ weights --

ensure_weights() { # ensure_weights <node>
  local node="$1" dir; dir="$(model_dir_name)"
  log_info "checking for $WHISPER_MODEL on $(whisper_host_label "$node")"
  if run_on "$node" <<EOS >/dev/null 2>&1
test -f "$WHISPER_MODEL_CACHE/repos/$dir/model.safetensors"
EOS
  then
    check_pass "weights present on $node ($dir)"
    return 0
  fi
  log_info "fetching $WHISPER_MODEL@${WHISPER_MODEL_REVISION:0:12} (~3.1 GB) onto $node"
  # ALLOW-LIST, not the whole repo. openai/whisper-large-v3 also ships fp32
  # shards, a flax checkpoint and pytorch .bin copies of the same weights —
  # about 24 GB in total, of which the server reads 3.1 GB. Downloading the
  # rest costs twenty minutes per node and nothing else.
  run_on "$node" <<EOS || die "could not fetch the model weights onto $node"
set -e
docker run --rm \
  -v "$WHISPER_MODEL_CACHE":/models \
  --entrypoint python3 "$WHISPER_BASE_IMAGE" -c "
from huggingface_hub import snapshot_download
snapshot_download('$WHISPER_MODEL', revision='$WHISPER_MODEL_REVISION',
                  local_dir='/models/repos/$dir',
                  allow_patterns=['*.json','*.txt','model.safetensors',
                                  'tokenizer*','normalizer.json','merges.txt',
                                  'vocab.json'],
                  ignore_patterns=['*fp32*','flax_model*','pytorch_model*'])
print('weights ready')
"
EOS
  check_pass "weights fetched onto $node"
}

# -------------------------------------------------------------------- files --

sync_files() { # sync_files <node>
  [ "$1" = worker ] || return 0
  is_dual_mode || return 0
  log_info "syncing the whisper stack to $CLUSTER_WORKER_SSH"
  ssh -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" \
    "mkdir -p $REMOTE_DIR/whisper" </dev/null
  local remote_dir
  remote_dir="$(ssh -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$CLUSTER_WORKER_SSH" "echo $REMOTE_DIR" </dev/null)"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/compose.whisper.yaml" "$CLUSTER_WORKER_SSH:$remote_dir/" \
    || die "could not copy the compose file to the worker"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/whisper/Dockerfile" "$ROOT/compose/whisper/server.py" \
    "$CLUSTER_WORKER_SSH:$remote_dir/whisper/" \
    || die "could not copy the whisper image sources to the worker"
  check_pass "stack synced"
}

# ----------------------------------------------------------------- endpoint --

# The orchestrator reads ASR_BASE_URLS (comma-separated) — or ASR_BASE_URL for
# a single engine. Recording them in .env, the first --env-file Compose reads,
# is what makes the fleet survive a restart of the stack. It is also the ONLY
# automated link between "the engine started" and "the orchestrator knows
# where it is", which is why it is carried over verbatim from scripts/asr.sh.
record_endpoints() { # record_endpoints <url> [<url>...] — the ones just started
  # MERGE, never replace. `up` on one node (WHISPER_NODES=head, say, to
  # rebuild the head's engine) must not drop the other node's engine from the
  # list — that is how a rebuild of one engine silently halved the fleet on
  # 2026-09-08. Everything already recorded stays, in its order; the engines
  # just started are appended if they are new.
  local merged=() url seen
  for url in $(grep -E '^ASR_BASE_URLS=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr ',' ' ') "$@"; do
    seen=no
    for have in "${merged[@]-}"; do [ "$have" = "$url" ] && seen=yes; done
    [ "$seen" = no ] && merged+=("$url")
  done
  set -- "${merged[@]}"
  local urls; urls="$(printf '%s,' "$@")"; urls="${urls%,}"
  touch "$ROOT/.env"
  _set_env ASR_ENABLED true
  _set_env ASR_BASE_URL "$1"
  _set_env ASR_BASE_URLS "$urls"
  _set_env ASR_MODEL "$WHISPER_MODEL"
  _set_env ASR_BACKEND whisper
  check_pass "recorded ASR_BASE_URLS=$urls in .env"
  log_info "restart the orchestrator to pick it up:  ./techsara up"
}

# The mirror of record_endpoints, and it exists because stopping an engine
# while .env still lists it is worse than never having started it: the
# orchestrator keeps routing to a dead endpoint, eats a connection failure,
# stands it down for the cooldown, and retries it every twenty seconds
# forever. `down` and `stop` must therefore leave .env describing the fleet
# that is actually running.
forget_endpoints() { # forget_endpoints <url> [<url>...] — the ones just stopped
  local remaining=() url keep
  # Everything .env currently lists, minus the ones this command stopped.
  for url in $(grep -E '^ASR_BASE_URLS=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr ',' ' '); do
    keep=yes
    for stopped in "$@"; do [ "$url" = "$stopped" ] && keep=no; done
    [ "$keep" = yes ] && remaining+=("$url")
  done
  touch "$ROOT/.env"
  if [ ${#remaining[@]} -eq 0 ]; then
    # No engine left. OFF rather than pointing at nothing: the composer must
    # not offer a microphone button that cannot work.
    _set_env ASR_ENABLED false
    _set_env ASR_BASE_URLS ""
    check_pass "no engines left — recorded ASR_ENABLED=false in .env"
  else
    local urls; urls="$(printf '%s,' "${remaining[@]}")"; urls="${urls%,}"
    _set_env ASR_BASE_URL "${remaining[0]}"
    _set_env ASR_BASE_URLS "$urls"
    check_pass "recorded ASR_BASE_URLS=$urls in .env"
  fi
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

each_node() {
  local node
  for node in $(whisper_nodes); do
    printf '%s %s\n' "$node" "$(whisper_bind_address "$node")"
  done
}

whisper_compose() { # whisper_compose <node> <bind> ARGS...
  local node="$1" bind="$2"; shift 2
  local env="WHISPER_BIND=$bind WHISPER_PORT=$WHISPER_PORT \
WHISPER_MODEL='$WHISPER_MODEL' WHISPER_MODEL_REVISION='$WHISPER_MODEL_REVISION' \
WHISPER_MODEL_CACHE='$WHISPER_MODEL_CACHE'"
  if [ "$node" = worker ] && is_dual_mode; then
    ssh -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" \
      "cd $REMOTE_DIR && $env docker compose --project-name $WHISPER_PROJECT \
       -f compose.whisper.yaml $*" </dev/null
  else
    ( cd "$ROOT" && env $env docker compose --project-name "$WHISPER_PROJECT" \
      -f compose/compose.whisper.yaml "$@" )
  fi
}

# ----------------------------------------------------------------- commands --

cmd="${1:-status}"; shift || true
for arg in "$@"; do
  [ "$arg" = --all-nodes ] && WHISPER_NODES="worker,head"
done

case "$cmd" in
  up)
    urls=()
    while read -r node bind; do
      [ -n "$node" ] || continue
      log_info "bringing whisper up on $(whisper_host_label "$node") at $bind:$WHISPER_PORT"
      ensure_weights "$node"
      sync_files "$node"
      whisper_compose "$node" "$bind" up -d --build
      urls+=("http://$bind:$WHISPER_PORT/v1")
    done < <(each_node)
    record_endpoints "${urls[@]}"
    ;;
  down|stop)
    stopped=()
    while read -r node bind; do
      [ -n "$node" ] || continue
      whisper_compose "$node" "$bind" "$([ "$cmd" = down ] && echo down || echo stop)"
      stopped+=("http://$bind:$WHISPER_PORT/v1")
    done < <(each_node)
    forget_endpoints "${stopped[@]}"
    ;;
  status)
    while read -r node bind; do
      [ -n "$node" ] || continue
      printf '\n== %s (%s:%s) ==\n' "$(whisper_host_label "$node")" "$bind" "$WHISPER_PORT"
      whisper_compose "$node" "$bind" ps || true
      curl -fsS -m 5 "http://$bind:$WHISPER_PORT/health" || echo "  engine not answering"
      echo
    done < <(each_node)
    ;;
  logs)
    read -r node bind < <(each_node)
    whisper_compose "$node" "$bind" logs -f
    ;;
  url)
    while read -r node bind; do
      [ -n "$node" ] || continue
      printf 'http://%s:%s/v1\n' "$bind" "$WHISPER_PORT"
    done < <(each_node)
    ;;
  verify)
    # A REAL clip, end to end. Whisper's own canonical test file, so the
    # expected words are known and a wrong transcript is obvious. A health
    # probe proves the process is up; this proves it can hear.
    clip="${TMPDIR:-/tmp}/whisper-verify.flac"
    [ -f "$clip" ] || curl -fsSL -o "$clip" \
      https://github.com/openai/whisper/raw/main/tests/jfk.flac \
      || die "could not fetch the verification clip"
    while read -r node bind; do
      [ -n "$node" ] || continue
      printf '\n== %s ==\n' "$(whisper_host_label "$node")"
      curl -fsS -m 120 -F "file=@$clip" -F "model=$WHISPER_MODEL" \
        "http://$bind:$WHISPER_PORT/v1/audio/transcriptions" || echo "  no answer"
      echo
    done < <(each_node)
    ;;
  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
