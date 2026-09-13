#!/usr/bin/env bash
# Document OCR (baidu/Unlimited-OCR) off the head node.
#
#   scripts/ocr.sh up       put the image and the weights on the node that has
#                           room, start the engine, record its address in .env
#   scripts/ocr.sh down     stop and remove it (the weights stay) and hand OCR
#                           back to the head's own vllm-ocr
#   scripts/ocr.sh stop     stop it; the same hand-back
#   scripts/ocr.sh status   container state + the engine's own /v1/models
#   scripts/ocr.sh logs     follow the engine log
#   scripts/ocr.sh verify   OCR a real image end to end and print what it read
#   scripts/ocr.sh url      print the endpoint the orchestrator should use
#
# WHY. The head's own OCR service (`vllm-ocr`, started by `./techsara up`)
# reserves 0.10-0.14 of the head's 121 GB of unified memory -- about 17 GB --
# for a 3.3B model that serves single 8,192-token pages a handful at a time.
# Measured 2026-09-09 the head sat at 113/121 GB while the worker had 69 GB
# free and a GPU that idles between the main model's tensor-parallel steps.
# This script runs the SAME engine (same image digest, same flags, a KV budget
# sized for the job) on the worker, and tells the launcher where it went.
#
# NOTHING HERE TOUCHES THE LLM. The engine is its own Compose project on its
# own port; `down` leaves vLLM, the orchestrator and the frontend running, and
# in dual mode it never comes near sf-local-ai-worker, which is the main
# model's tensor-parallel rank 1.
#
# THE HAND-OVER IS TWO STEPS, ON PURPOSE. `up` starts the engine and writes
# OCR_REMOTE_BASE_URL into .env; the orchestrator keeps using whatever it was
# started with until `./techsara up` (a routine up -- the main model is not
# restarted) regenerates its environment, repoints it, and stops the head's
# vllm-ocr because its Compose profile is no longer enabled. Until then both
# engines serve, which is the safe state: OCR never goes dark in between.
#
# WHERE IT LANDS. In dual mode: the worker (Spark 2), because that is where
# the memory is. In single mode: this node, since there is nowhere else.
# OCR_NODE=head|worker overrides the choice.
set -euo pipefail

# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

OCR_PROJECT="${OCR_PROJECT:-sf-local-ai-ocr}"
OCR_PORT="${OCR_PORT:-30004}"
OCR_MODEL="${OCR_MODEL:-baidu/Unlimited-OCR}"
# Pinned, like every other model in config/model-manifest.yaml, and the same
# revision the launcher installed on the head: the directory name below is
# derived from it, so a different revision is a different directory and never
# a silent swap of what the platform transcribes with.
OCR_MODEL_REVISION="${OCR_MODEL_REVISION:-07dea832e22aefee32ad281d4b80551282e1c168}"
# The digest the head's vllm-ocr runs (compose/compose.dgx-spark.yaml). The
# worker must run the same bytes: a different vLLM build is a different set
# of model-loader quirks for a --trust-remote-code model.
OCR_IMAGE="${OCR_IMAGE:-vllm/vllm-openai@sha256:24f2f8975d011ea7f7066a547886a08a1fd3c4bf0880463487fae4f01ce723c6}"
OCR_MODEL_CACHE="${OCR_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}"
OCR_MAX_CONTEXT="${OCR_MAX_CONTEXT:-8192}"
# The engine's outer memory ceiling as a fraction of the node's total; the KV
# budget inside it is fixed at 3 GiB in the compose file. 0.10 is the value
# the head runs with today, and it is enough: weights are 7.5 GB of the 12.
OCR_GPU_MEMORY_UTILIZATION="${OCR_GPU_MEMORY_UTILIZATION:-0.10}"
# How long `up` waits for /v1/models: weights are read from disk and the
# vision encoder is compiled on a first start, which takes a few minutes.
OCR_READY_TIMEOUT_S="${OCR_READY_TIMEOUT_S:-600}"
# The worker's management interface; its address is what the engine binds
# (see ocr_bind_address for why it is not the RoCE rail).
OCR_MANAGEMENT_IFNAME="${OCR_MANAGEMENT_IFNAME:-enP7s7}"
REMOTE_DIR="${WORKER_REMOTE_DIR:-\$HOME/.techsara-cluster}"
#: Which node carries the engine: "worker" or "head". Defaults to the worker
#: in dual mode -- the head is the loaded machine -- and the head otherwise.
OCR_NODE="${OCR_NODE:-}"

is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }

# The directory the weights live in, under <cache>/repos/. Same slug the
# launcher's model manager uses: org--name--<12 chars of the revision>.
model_dir_name() {
  printf '%s--%s' "${OCR_MODEL//\//--}" "${OCR_MODEL_REVISION:0:12}"
}

# WHICH NODE THIS COMMAND ACTS ON. One engine, one node: unlike whisper there
# is no fleet to read back from .env, because the orchestrator's OCR client
# talks to exactly one base URL.
ocr_node() {
  case "$OCR_NODE" in
    "") ;;
    head) printf 'head'; return ;;
    worker)
      is_dual_mode || die "OCR_NODE=worker needs the two-node cluster (TECHSARA_CLUSTER_MODE=dual in .runtime/generated.env); this deployment is single-node"
      printf 'worker'; return ;;
    *) die "OCR_NODE must be 'head' or 'worker'; got '$OCR_NODE'" ;;
  esac
  if is_dual_mode; then printf 'worker'; else printf 'head'; fi
}

ocr_host_label() {
  case "$1" in
    worker) printf 'worker (%s)' "$CLUSTER_WORKER_SSH" ;;
    *)      printf 'head (this node)' ;;
  esac
}

run_on() { # run_on <node> — script on stdin
  if [ "$1" = worker ] && is_dual_mode; then
    # shellcheck disable=SC2086
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
# (F043/F051) reached the unauthenticated engine from the office LAN. Its
# consumers all dial the management address, so the next `ocr.sh up` would
# have broken them out of lockstep: the running orchestrator and the e2e
# orchestrator carry OCR_BASE_URL/OCR_REMOTE_BASE_URL=http://192.168.9.68:30004/v1
# and are not recreated by this script (every page silently loses OCR), and
# Prometheus's file_sd target would move onto the rail monitoring must never
# use (monitoring/prometheus/prometheus.yml). A rail bind is not a boundary
# either: under Linux's weak-host model a LAN host routing 10.100.184.2 via
# the worker still reaches it on enP7s7. So the engine stays where production
# runs it, and the LAN/tailnet exposure is closed by the host packet filter
# (scripts/host-guard.sh, operator actions OA-4/OA-6: 30004 accepted on
# enP7s7 from the head 192.168.9.54 only, dropped on tailscale0). Do not
# rebind it without first moving every consumer above.
#
# What 229031c got right stays: an empty or wildcard answer stops the script
# instead of starting the engine on an empty or every-interface bind.
ocr_bind_address() {
  local address
  if [ "$1" = worker ] && is_dual_mode; then
    address="$(ssh_worker "ip -4 -br addr show ${OCR_MANAGEMENT_IFNAME:-enP7s7} 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" </dev/null)" || address=""
    case "$address" in
      "") die "could not read the worker's ${OCR_MANAGEMENT_IFNAME:-enP7s7} address over ssh ($CLUSTER_WORKER_SSH); set OCR_MANAGEMENT_IFNAME if its management interface is named differently" ;;
      0.0.0.0|::|"[::]") die "the worker's ${OCR_MANAGEMENT_IFNAME:-enP7s7} address read back as '$address'; the OCR engine never binds a wildcard" ;;
    esac
    printf '%s' "$address"
  else
    # The head's engine is reached by the orchestrator over the docker bridge,
    # so it binds the gateway address rather than loopback, which a container
    # cannot reach.
    printf '%s' "${OCR_HEAD_BIND:-172.17.0.1}"
  fi
}

# The model cache on a node. The worker's is whatever the launcher recorded
# for the cluster (CLUSTER_WORKER_MODEL_CACHE, which defaults to the head's
# TECHSARA_MODEL_CACHE), so the weights land next to the main model's shard.
node_cache() {
  case "$1" in
    worker) printf '%s' "${CLUSTER_WORKER_MODEL_CACHE:-$OCR_MODEL_CACHE}" ;;
    *)      printf '%s' "$OCR_MODEL_CACHE" ;;
  esac
}

# -------------------------------------------------------------------- image --

# The image is ~20 GB. Docker Hub is tried first because it is the path that
# leaves the worker with a registry digest; when the Hub is unreachable from
# that box (its DNS is flaky, see docs/CLUSTER.md) the bytes come from the
# head's own daemon instead, over ssh. Read-only on the head either way.
ensure_image() { # ensure_image <node>
  local node="$1"
  log_info "checking for the vLLM image on $(ocr_host_label "$node")"
  if run_on "$node" <<EOS >/dev/null 2>&1
docker image inspect '$OCR_IMAGE' >/dev/null 2>&1
EOS
  then
    check_pass "image present on $node"
    return 0
  fi
  if [ "$node" != worker ] || ! is_dual_mode; then
    log_info "pulling $OCR_IMAGE (~20 GB) on this node"
    docker pull "$OCR_IMAGE" || die "could not pull the image; ./techsara up pulls it as part of the head's stack"
    check_pass "image pulled"
    return 0
  fi
  log_info "pulling $OCR_IMAGE (~20 GB) on the worker"
  if ssh_worker "docker pull '$OCR_IMAGE'" </dev/null; then
    check_pass "image pulled on the worker"
    return 0
  fi
  docker image inspect "$OCR_IMAGE" >/dev/null 2>&1 \
    || die "the pull failed on the worker and this node does not have $OCR_IMAGE either; ./techsara up pulls it"
  log_info "the pull failed on the worker (Docker Hub unreachable); streaming the image from this node with docker save | ssh docker load -- ~20 GB, several minutes, and it prints nothing until the load finishes"
  docker save "$OCR_IMAGE" | ssh_worker "docker load" || die "could not transfer the image to the worker"
  # A loaded image keeps its content ID but not the registry's manifest
  # digest, so from here on Compose is handed the ID (see image_ref).
  check_warn "image loaded via docker save/load: it has no registry digest on the worker, so Compose will be given the image ID instead"
}

# The reference Compose can resolve ON THAT NODE: the digest where the image
# was pulled, the image ID where it arrived by save/load, and nothing (the
# compose file's own default) where it is absent -- `down` and `status` must
# still render the project on a node that has never had the image.
image_ref() { # image_ref <node>
  local node="$1" local_id
  if run_on "$node" <<EOS >/dev/null 2>&1
docker image inspect '$OCR_IMAGE' >/dev/null 2>&1
EOS
  then
    printf '%s' "$OCR_IMAGE"
    return 0
  fi
  local_id="$(docker image inspect "$OCR_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
  [ -n "$local_id" ] || return 0
  if run_on "$node" <<EOS >/dev/null 2>&1
docker image inspect '$local_id' >/dev/null 2>&1
EOS
  then
    printf '%s' "$local_id"
  fi
}

# ------------------------------------------------------------------ weights --

ensure_weights() { # ensure_weights <node>
  local node="$1" dir cache
  dir="$(model_dir_name)"; cache="$(node_cache "$node")"
  log_info "checking for $OCR_MODEL on $(ocr_host_label "$node")"
  if run_on "$node" <<EOS >/dev/null 2>&1
test -f "$cache/repos/$dir/config.json"
EOS
  then
    check_pass "weights present on $node ($dir)"
    return 0
  fi
  if [ "$node" != worker ] || ! is_dual_mode; then
    die "the weights are not in $cache/repos/$dir on this node; ./techsara up installs them (the launcher's model manager owns the head's cache)"
  fi
  [ -f "$OCR_MODEL_CACHE/repos/$dir/config.json" ] \
    || die "the weights are not in $OCR_MODEL_CACHE/repos/$dir on this node either; ./techsara up installs them"
  # A COPY, NOT A DOWNLOAD. The head already holds the pinned revision the
  # launcher verified; copying it is 6.4 GB over the management link and
  # cannot fetch a different revision by accident. rsync so an interrupted
  # copy resumes instead of starting over.
  log_info "copying $OCR_MODEL@${OCR_MODEL_REVISION:0:12} (6.4 GB) to $CLUSTER_WORKER_SSH:$cache/repos/$dir"
  ssh_worker "mkdir -p '$cache/repos/$dir'" </dev/null \
    || die "could not create $cache/repos/$dir on the worker"
  # The ssh options stay inside ONE quoted -e string: rsync splits it itself.
  rsync -a --info=progress2 \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=8 ${CLUSTER_WORKER_SSH_OPTS:-}" \
    "$OCR_MODEL_CACHE/repos/$dir/" "$CLUSTER_WORKER_SSH:$cache/repos/$dir/" \
    || die "could not copy the weights to the worker"
  check_pass "weights copied onto $node"
}

# -------------------------------------------------------------------- files --

sync_files() { # sync_files <node>
  [ "$1" = worker ] || return 0
  is_dual_mode || return 0
  log_info "syncing the OCR compose file to $CLUSTER_WORKER_SSH"
  ssh_worker "mkdir -p $REMOTE_DIR" </dev/null
  local remote_dir
  remote_dir="$(ssh_worker "echo $REMOTE_DIR" </dev/null)"
  scp_to_worker "$ROOT/compose/compose.ocr.yaml" "$remote_dir" \
    || die "could not copy the compose file to the worker"
  check_pass "compose file synced"
}

# ----------------------------------------------------------------- endpoint --

# The launcher reads OCR_REMOTE_BASE_URL from .env: when it is set, generated
# OCR_BASE_URL becomes that address and the head's `ocr` Compose profile is
# left off, so `./techsara up` stops starting vllm-ocr on the head. Recording
# it here is the ONLY automated link between "the engine started" and "the
# orchestrator knows where it is", exactly as ASR_BASE_URLS is for whisper.
record_endpoint() { # record_endpoint <url>
  touch "$ROOT/.env"
  _set_env OCR_REMOTE_BASE_URL "$1"
  check_pass "recorded OCR_REMOTE_BASE_URL=$1 in .env"
  write_ocr_scrape_target
  log_info "the orchestrator is still using the engine it was started with."
  log_info "switch it over and retire the head's vllm-ocr:  ./techsara up"
  log_info "(a routine up: the main model is not restarted; or recreate just the orchestrator with the launcher's compose chain)"
}

# The mirror of record_endpoint, and it exists because stopping the engine
# while .env still names it is worse than never having started it: the
# orchestrator would send every page to a dead address and proceed
# pixels-only, silently, for as long as nobody noticed. Clearing the key
# sends the launcher back to the head's own engine on its next up.
forget_endpoint() {
  touch "$ROOT/.env"
  _set_env OCR_REMOTE_BASE_URL ""
  check_pass "cleared OCR_REMOTE_BASE_URL in .env -- OCR goes back to the head's vllm-ocr"
  log_info "start the head's engine and repoint the orchestrator:  ./techsara up"
  write_ocr_scrape_target
}

_set_env() { # _set_env KEY VALUE — idempotent, in .env
  local key="$1" value="$2" env_file="$ROOT/.env"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

ocr_compose() { # ocr_compose <node> <bind> ARGS...
  local node="$1" bind="$2"; shift 2
  local -a assignments=(
    "OCR_BIND=$bind"
    "OCR_PORT=$OCR_PORT"
    "OCR_IMAGE=$(image_ref "$node")"
    "OCR_MODEL=$OCR_MODEL"
    "OCR_MODEL_CACHE=$(node_cache "$node")"
    "OCR_MODEL_CONTAINER_PATH=/models/repos/$(model_dir_name)"
    "OCR_MAX_CONTEXT=$OCR_MAX_CONTEXT"
    "OCR_GPU_MEMORY_UTILIZATION=$OCR_GPU_MEMORY_UTILIZATION"
  )
  if [ "$node" = worker ] && is_dual_mode; then
    # Shell-quoted for the remote shell, which parses this line again.
    local remote_env
    remote_env="$(printf '%q ' "${assignments[@]}")"
    ssh_worker "cd $REMOTE_DIR && $remote_env docker compose --project-name $OCR_PROJECT -f compose.ocr.yaml $*" </dev/null
  else
    ( cd "$ROOT" && env "${assignments[@]}" docker compose --project-name "$OCR_PROJECT" \
      -f compose/compose.ocr.yaml "$@" )
  fi
}

wait_ready() { # wait_ready <node> <bind> <url> — poll /v1/models
  local node="$1" bind="$2" url="$3" started now waited
  started="$(date +%s)"
  log_info "waiting for $url/models (up to $((OCR_READY_TIMEOUT_S / 60)) min; a first start reads 6.4 GB of weights)"
  while :; do
    if curl -fsS -m 5 -o /dev/null "$url/models" 2>/dev/null; then
      check_pass "engine answering at $url"
      return 0
    fi
    now="$(date +%s)"; waited=$((now - started))
    [ "$waited" -lt "$OCR_READY_TIMEOUT_S" ] || break
    # A line a minute, so a long load is visibly a load and not a hang.
    if [ $((waited % 60)) -lt 10 ] && [ "$waited" -ge 60 ]; then
      log_dim "  still loading ($((waited / 60)) min)"
    fi
    sleep 10
  done
  ocr_compose "$node" "$bind" ps || true
  die "the engine did not answer at $url/models within $((OCR_READY_TIMEOUT_S / 60)) min; scripts/ocr.sh logs"
}

# A REAL image, end to end. 460x47 px, 1-bit, "TechSara OCR check 2026" in
# DejaVu Sans Bold, rendered once with Pillow on 2026-09-09 and inlined so
# `verify` needs nothing but curl and python3 on this node: no container is
# started to make a test image, and nothing is fetched from the network.
VERIFY_TEXT="TechSara OCR check 2026"
VERIFY_PNG_B64='iVBORw0KGgoAAAANSUhEUgAAAcwAAAAvAQAAAABGB8ezAAAC2ElEQVR42u2WsW4bRxRFzywH4RQC
uOnYcfwHLJ1GXP+JfiFVjIAWn0g2qaJP0IckxhBw4ZIf4GIYpGAVjwIBGQrDfSl2KUU20hhwYUCv
2cFg7tx7330DrFG+sG4rvrieoc/QrwA9+v87cmi67/oz6K0xAAW0enLlptlYgNxvCXAlWHae2wbQ
BKqqZaKZmfaVmSnDwEJVc7+7Ui2MCgsGKiNND0QF9o+kme0RIDxlrciFIC2Se68C2Cc+EpTT2j1C
W8ggaOdpFBfSscaH08R5zqNgUvyE9bzwOv66YBigghp61oE8YJeF6VSdf8qqG5JNqWnHL6GCxE6o
ytEbmit/+OFHCxtDwhkiavOhUceukZ1UgCD/yfXm+qjru6TAn3nPUYBLujjvj24f7w/cbCUKCBX4
8n3TCVbJdxSyF2hdFEIvUPHsyaQ9C0E2fUeJbLe94CGc238KayYh81OGJSCYDMzdX5nEMMgSsBEw
0IfTjufp1XwMR7lQNxoTEYiC1g1xladE99ZNFxYUT+5VnTosFhkTdk2u3zi4BN+JNjiDgZpLB62L
rWPJqHSCC+eRAt8Rb3D+DlhCFDAQyBD4ADKFUvsKtdSnDu8asGAiQiYBLXgBlS5XdX2H9j7Cveun
sh+JAowZRAfzzolgYqAh0+UkEWLjW3J98poseCyC/WhjJs47lwGtIeCUgAVJsBGEdHFihUkLBYnU
DocBZkpNNi7SkA2vNMKyD62OYh8FF2QNf9yZTGYCcG7ZbgFUHUBI6aqglvo3NvBR+pHYv69/L6C1
luCIr8G/c+62UZvwP2eYvFnmG7mkPVjeszDV37YTPDg0vFuD58VRMv4aarCwAfjFAWsO1zLrXq/v
H3Hf4TPmEGnkwlGfwRnTATRVZqwZxhhJ4qUCSwAjNaAaF3GipkxWZaC7kGbl5UTVqIZZaIfaDvJs
2BqNs1WaFQa6UkHjQpN5/iF4hn7j0H8B3vp0mvb2DbYAAAAASUVORK5CYII='

# ----------------------------------------------------------------- commands --

cmd="${1:-status}"; shift || true

case "$cmd" in
  up|down|stop|status|logs|url|verify) ;;
  *)
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac

# Resolved once, after the command is known to be one: reading the worker's
# management address is an ssh round-trip, and a typo should not pay for it.
node="$(ocr_node)"
bind="$(ocr_bind_address "$node")"
url="http://$bind:$OCR_PORT/v1"

case "$cmd" in
  up)
    log_info "bringing OCR up on $(ocr_host_label "$node") at $bind:$OCR_PORT"
    ensure_image "$node"
    ensure_weights "$node"
    sync_files "$node"
    ocr_compose "$node" "$bind" up -d
    wait_ready "$node" "$bind" "$url"
    record_endpoint "$url"
    ;;
  down|stop)
    ocr_compose "$node" "$bind" "$([ "$cmd" = down ] && echo down || echo stop)"
    forget_endpoint
    ;;
  status)
    printf '\n== %s (%s:%s) ==\n' "$(ocr_host_label "$node")" "$bind" "$OCR_PORT"
    ocr_compose "$node" "$bind" ps || true
    curl -fsS -m 5 "$url/models" || echo "  engine not answering"
    echo
    ;;
  logs)
    ocr_compose "$node" "$bind" logs -f
    ;;
  url)
    printf '%s\n' "$url"
    ;;
  verify)
    # The request the orchestrator sends (engines/ocr.py): the image first,
    # then the model card's "document parsing" prompt, at temperature 0. A
    # health probe proves the process is up; this proves it can read.
    printf '\n== %s (%s) ==\n' "$(ocr_host_label "$node")" "$url"
    body="$(python3 - "$OCR_MODEL" "$VERIFY_PNG_B64" <<'PY'
import json, sys
model, png = sys.argv[1], sys.argv[2].replace("\n", "")
print(json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}},
        {"type": "text", "text": "document parsing"},
    ]}],
    "max_tokens": 256,
    "temperature": 0,
}))
PY
)"
    reply="$(curl -fsS -m 180 -H 'Content-Type: application/json' -d "$body" "$url/chat/completions")" \
      || die "no answer from $url/chat/completions"
    text="$(printf '%s' "$reply" | python3 -c 'import json, sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])')"
    printf 'expected : %s\nengine   : %s\n' "$VERIFY_TEXT" "$text"
    # The served model prefixes each line with its layout block ("text [x, y,
    # x, y]..."), so the check is for the words, not the whole line.
    case "$text" in
      *TechSara*) check_pass "the engine read the image" ;;
      *) check_fail "the engine answered but did not read the image"; exit 1 ;;
    esac
    ;;
esac
