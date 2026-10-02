#!/usr/bin/env bash
# Speech-to-text overflow: openai/whisper-large-v3 on the WORKER's CPU cores (and the head's).
#
#   scripts/whisper-cpu.sh up       convert the pinned weights (once), build the image, start the
#                                   replica on the worker, wait until it is ready, and add its
#                                   address to ASR_CPU_BASE_URLS in .env
#   scripts/whisper-cpu.sh down     stop and remove it (the converted weights stay) and take it
#                                   back out of .env; the other node's replica stays listed
#   scripts/whisper-cpu.sh status   container state + the replica's own /health
#   scripts/whisper-cpu.sh logs     follow the replica's log
#   scripts/whisper-cpu.sh verify   transcribe a real clip end to end, print it and the time taken
#   scripts/whisper-cpu.sh url      print the endpoint the orchestrator should use
#
#   Every command acts on the worker's copy. WHISPER_CPU_NODE=head acts on the head's instead.
#
# WHAT IT IS. A third copy of the SAME model the two GPU replicas run (scripts/whisper.sh), on ten
# Cortex-X925 cores of the worker and no GPU. The orchestrator prefers the GPU replicas and sends a
# clip here only when every one of them is already decoding, and only if the clip can finish inside
# its deadline at this copy's measured speed (ASR_CPU_* in orchestrator/app/config.py). Measured
# before it was built: docs/voice/CPU-REPLICA.md.
#
# THE WORKER BY DEFAULT, THE HEAD ONLY WHEN ASKED, AND DUAL MODE ONLY. The head's memory is off
# limits for anything new (owner, 2026-09-16). The owner's exception of 2026-09-30 allows this one
# copy there too, on the condition that chat decode drops by no more than 5 % while it is busy
# (docs/voice/CPU-REPLICA-HEAD.md, scripts/whisper-cpu-chat-gate.py). So the head's copy:
#   - starts only with WHISPER_CPU_NODE=head, and not below 20 GiB of MemAvailable there;
#   - compiles and converts NOTHING on the head: it runs the image the worker built and the q8_0
#     file the worker converted, both copied over ssh and checked against the worker's image ID and
#     the q8_0 pin (a compile is minutes of four jobs, a conversion loads 3 GB of fp16 weights);
#   - binds the Docker bridge gateway (172.17.0.1), like the head's GPU replica on 30007;
#   - runs on its own cores (WHISPER_CPU_HEAD_CPUSET/_CPUS/_THREADS, from the environment or .env;
#     the default is the 4-core cap that passed the chat gate, 16-19 / 4 / 4);
#   - is listed LAST in ASR_CPU_BASE_URLS, so the worker's copy takes overflow first.
# There is no single-node fallback: in single mode this script refuses.
#
# NOTHING HERE TOUCHES THE LLM OR THE GPU REPLICAS. Its own Compose project
# (sf-local-ai-whisper-cpu) on its own port (30008); `down` leaves vLLM, both GPU speech replicas,
# the orchestrator and the frontend running, and it never comes near sf-local-ai-worker, which is
# the main model's tensor-parallel rank 1. It does not use ASR_BASE_URLS either: that list is the
# GPU fleet, and the CPU copy lives in its own key so /v1 and the dictation pool sizes are unchanged.
#
# THE MODEL FILE IS MADE ON THE WORKER, FROM THE PINNED REVISION, AND CHECKED. `up` converts
# openai/whisper-large-v3@06f233fe06e7 (the GPU replicas' revision, already in the model cache) with
# whisper.cpp's own converter in a one-shot container of the image's `convert` stage (no network,
# read-only weights), quantises it to q8_0, and refuses to continue unless both files match the
# SHA-256 values below. The server checks the q8_0 hash again at every start.
set -euo pipefail

# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

WHISPER_CPU_PROJECT="${WHISPER_CPU_PROJECT:-sf-local-ai-whisper-cpu}"
WHISPER_CPU_PORT="${WHISPER_CPU_PORT:-30008}"
WHISPER_MODEL="openai/whisper-large-v3"
WHISPER_MODEL_REVISION="06f233fe06e710322aca913c1bc4249a0d71fce1"
#: whisper.cpp's converter output for that revision (f16), and whisper-quantize's q8_0 of it.
#: Reproduced 2026-09-30 by two different builds of whisper.cpp v1.9.4 (927cfce3).
GGML_F16_SHA256="e4fb6f478cfec2e2bbd4b271654338b08492550bbf221361a29d890bc22fbe01"
GGML_Q8_0_SHA256="37efc6b68f300ab717465685f7c3e175a66c11cf92bb3ab9912e86f4116c465e"
WHISPER_CPU_IMAGE="sf-local-ai-whisper-cpu:wcpp-927cfce3-cap443-q8_0"
WHISPER_CPU_CONVERT_IMAGE="sf-local-ai-whisper-cpu-convert:wcpp-927cfce3-cap443"
WHISPER_MODEL_CACHE="${WHISPER_MODEL_CACHE:-${CLUSTER_WORKER_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}}"
WHISPER_CPU_READY_TIMEOUT_S="${WHISPER_CPU_READY_TIMEOUT_S:-300}"
WHISPER_MANAGEMENT_IFNAME="${WHISPER_MANAGEMENT_IFNAME:-enP7s7}"
REMOTE_DIR="${WORKER_REMOTE_DIR:-\$HOME/.techsara-cluster}"

#: Which copy a command acts on: "worker" (the default) or "head" (the owner's 2026-09-30 exception).
WHISPER_CPU_NODE="${WHISPER_CPU_NODE:-worker}"
case "$WHISPER_CPU_NODE" in
  worker|head) ;;
  *) die "WHISPER_CPU_NODE must be 'worker' or 'head', not '$WHISPER_CPU_NODE'" ;;
esac
#: The head's own model cache (the head runs the script, so this is a local path there).
WHISPER_CPU_HEAD_MODEL_CACHE="${WHISPER_CPU_HEAD_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}"
#: Nothing starts on the head below this much MemAvailable: the floor the owner's exception was
#: measured against (the replica itself peaks at 2.0 GiB and is capped at 4 GiB).
WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB="${WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB:-20}"

hf_dir_name() { printf '%s--%s' "${WHISPER_MODEL//\//--}" "${WHISPER_MODEL_REVISION:0:12}"; }
ggml_dir_name() { printf '%s-ggml' "$(hf_dir_name)"; }

require_cluster() {
  [ "${CLUSTER_MODE:-single}" = "dual" ] \
    || die "the CPU speech replica runs only in the two-node cluster, and CLUSTER_MODE is '${CLUSTER_MODE:-single}'. The head's memory is off limits for new services."
  # The head's copy needs the worker too: its image and model file come from there.
  [ -n "${CLUSTER_WORKER_SSH:-}" ] || die "CLUSTER_WORKER_SSH is not set"
}

# head_setting KEY DEFAULT: the head copy's placement from the environment, else .env, else DEFAULT.
head_setting() {
  local value="${!1:-}"
  [ -n "$value" ] || value="$(env_get "$ENV_FILE" "$1" 2>/dev/null || true)"
  printf '%s' "${value:-$2}"
}

# The head copy's cores. Default: four Cortex-X925 cores (16-19), four cores' worth of time,
# four threads: the configuration that passed the owner's chat gate on Qwen/Qwen3.6-35B-A3B-NVFP4
# (decode +0.07 %, TTFT +2.5 %). Eight threads on 7-9,15-19 failed it (TTFT +10.1 % against a
# 10 % limit). Four threads decode about 1.8 times slower than eight, about 0.41 s per second of
# audio (docs/voice/CPU-REPLICA-HEAD.md).
head_placement() {
  local cpuset cpus threads
  cpuset="$(head_setting WHISPER_CPU_HEAD_CPUSET 16-19)"
  cpus="$(head_setting WHISPER_CPU_HEAD_CPUS 4)"
  threads="$(head_setting WHISPER_CPU_HEAD_THREADS 4)"
  [[ "$cpuset" =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] \
    || die "WHISPER_CPU_HEAD_CPUSET '$cpuset' is not a cpuset (for example 7-9,15-19)"
  [[ "$cpus" =~ ^[1-9][0-9]?$ ]] && [ "$cpus" -le 20 ] \
    || die "WHISPER_CPU_HEAD_CPUS '$cpus' must be a whole number of cores from 1 to 20"
  [[ "$threads" =~ ^[1-9][0-9]?$ ]] && [ "$threads" -le 20 ] \
    || die "WHISPER_CPU_HEAD_THREADS '$threads' must be from 1 to 20"
  printf 'WHISPER_CPU_CPUSET=%s WHISPER_CPU_CPUS=%s WHISPER_CPU_THREADS=%s' "$cpuset" "$cpus" "$threads"
}

# The owner's condition, as a floor: nothing starts on the head below 20 GiB of MemAvailable.
head_memory_floor() {
  local kb floor_kb
  [[ "$WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB" =~ ^[0-9]+$ ]] \
    || die "WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB '$WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB' must be a whole number of GiB"
  kb="$(awk '/^MemAvailable:/ {print $2}' "${WHISPER_CPU_MEMINFO:-/proc/meminfo}" 2>/dev/null || true)"
  [[ "$kb" =~ ^[0-9]+$ ]] || die "could not read MemAvailable on the head"
  floor_kb=$((WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB * 1024 * 1024))
  [ "$kb" -ge "$floor_kb" ] \
    || die "the head has $((kb / 1048576)) GiB available, under the ${WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB} GiB floor for its CPU speech replica; not starting it"
  check_pass "head memory: $((kb / 1048576)) GiB available (floor ${WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB} GiB)"
}

run_worker() { # run_worker - script on stdin
  # shellcheck disable=SC2086
  ssh -o BatchMode=yes -o ConnectTimeout=8 ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" "bash -s"
}

# The address a copy binds, for the node given (default: WHISPER_CPU_NODE, else the worker).
# The worker's: its MANAGEMENT address (enP7s7), read over ssh: never 0.0.0.0, never a RoCE address.
# Same rule, same reasons, as scripts/whisper.sh whisper_bind_address.
# The head's: a Docker bridge gateway, 172.17.0.1 unless WHISPER_CPU_HEAD_BIND says another one
# inside 172.16.0.0/12. The head's host guard does not judge 30008 (nor 30007, where the head's
# GPU replica listens on the same gateway), so the bind is the boundary: a LAN, rail or wildcard
# address would put an unauthenticated engine on the network, and loopback hides it from the
# orchestrator's container.
bind_address() {
  local address node="${1:-${WHISPER_CPU_NODE:-worker}}"
  if [ "$node" = head ]; then
    address="${WHISPER_CPU_HEAD_BIND:-172.17.0.1}"
    if [[ "$address" =~ ^172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
      printf '%s' "$address"
      return 0
    fi
    die "the head's CPU speech replica binds a Docker bridge gateway (172.16.0.0/12), not '$address'"
  fi
  address="$(ssh_worker "ip -4 -br addr show ${WHISPER_MANAGEMENT_IFNAME} 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" </dev/null)" || address=""
  case "$address" in
    "") die "could not read the worker's ${WHISPER_MANAGEMENT_IFNAME} address over ssh ($CLUSTER_WORKER_SSH)" ;;
    0.0.0.0|::|"[::]") die "the worker's ${WHISPER_MANAGEMENT_IFNAME} address read back as '$address'; the speech replica never binds a wildcard" ;;
    10.100.*) die "the worker's ${WHISPER_MANAGEMENT_IFNAME} address is a RoCE address ($address); refusing" ;;
  esac
  printf '%s' "$address"
}

endpoint() { printf 'http://%s:%s/v1' "$1" "$WHISPER_CPU_PORT"; }

# ------------------------------------------------------------------ files --

sync_files() {
  log_info "syncing the CPU speech stack to $CLUSTER_WORKER_SSH"
  ssh_worker "mkdir -p $REMOTE_DIR/whisper-cpu" </dev/null
  local remote_dir
  remote_dir="$(ssh_worker "echo $REMOTE_DIR" </dev/null)"
  # shellcheck disable=SC2086
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/compose.whisper-cpu.yaml" "$CLUSTER_WORKER_SSH:$remote_dir/" \
    || die "could not copy the compose file to the worker"
  # shellcheck disable=SC2086
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/whisper-cpu/Dockerfile" "$ROOT/compose/whisper-cpu/CMakeLists.txt" \
    "$ROOT/compose/whisper-cpu/wcpp_worker.cpp" "$ROOT/compose/whisper-cpu/server.py" \
    "$CLUSTER_WORKER_SSH:$remote_dir/whisper-cpu/" \
    || die "could not copy the image sources to the worker"
  check_pass "stack synced to $remote_dir"
}

build_images() {
  log_info "building $WHISPER_CPU_IMAGE on the worker (first build compiles whisper.cpp: a few minutes)"
  run_worker <<EOS || die "the image build failed on the worker"
set -e
cd $REMOTE_DIR/whisper-cpu
# BuildKit runs the compile inside the daemon, where a cpuset cannot be set; the Dockerfile bounds
# it to four jobs (BUILD_JOBS) instead. It runs once per whisper.cpp pin, for a few minutes.
docker build -t "$WHISPER_CPU_IMAGE" .
EOS
  check_pass "image $WHISPER_CPU_IMAGE"
}

# ------------------------------------------------------------------ model --

ensure_model() {
  local hf ggml
  hf="$(hf_dir_name)"; ggml="$(ggml_dir_name)"
  log_info "checking the q8_0 model on the worker"
  if run_worker <<EOS >/dev/null 2>&1
set -e
f="$WHISPER_MODEL_CACHE/repos/$ggml/ggml-large-v3-q8_0.bin"
test -f "\$f"
echo "$GGML_Q8_0_SHA256  \$f" | sha256sum -c --status -
EOS
  then
    check_pass "q8_0 model present and matches its pin ($ggml)"
    return 0
  fi
  log_info "converting $WHISPER_MODEL@${WHISPER_MODEL_REVISION:0:12} to whisper.cpp q8_0 on the worker (once)"
  run_worker <<EOS || die "could not convert the model on the worker"
set -e
cache="$WHISPER_MODEL_CACHE/repos"
src="\$cache/$hf"
out="\$cache/$ggml"
test -f "\$src/model.safetensors" || { echo "the GPU replica's weights are not in \$src; run scripts/whisper.sh up first" >&2; exit 1; }
cd $REMOTE_DIR/whisper-cpu
docker build --target convert -t "$WHISPER_CPU_CONVERT_IMAGE" .
work="\$(mktemp -d "\$cache/.whisper-cpu-convert.XXXXXX")"
trap 'rm -rf "\$work"' EXIT
# No network, weights read-only, output into a scratch directory next to the cache.
docker run --rm --network none --cpuset-cpus 5-9,15-19 --user "\$(id -u):\$(id -g)" \
  -e HOME=/out -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -v "\$src":/hf:ro -v "\$work":/out \
  "$WHISPER_CPU_CONVERT_IMAGE" \
  sh -c 'python3 /opt/convert-h5-to-ggml.py /hf /opt/openai-whisper /out > /out/convert.log 2>&1 \
         && mv /out/ggml-model.bin /out/ggml-large-v3-f16.bin \
         && whisper-quantize /out/ggml-large-v3-f16.bin /out/ggml-large-v3-q8_0.bin q8_0 > /out/quantize.log 2>&1'
echo "$GGML_F16_SHA256  \$work/ggml-large-v3-f16.bin" | sha256sum -c - \
  || { echo "the f16 conversion does not match its pin" >&2; exit 1; }
echo "$GGML_Q8_0_SHA256  \$work/ggml-large-v3-q8_0.bin" | sha256sum -c - \
  || { echo "the q8_0 file does not match its pin" >&2; exit 1; }
mkdir -p "\$out"
# Only the q8_0 file is kept: the f16 intermediate (3.1 GB) has served its purpose.
mv -f "\$work/ggml-large-v3-q8_0.bin" "\$out/ggml-large-v3-q8_0.bin"
chmod 0644 "\$out/ggml-large-v3-q8_0.bin"
EOS
  check_pass "q8_0 model converted and verified ($ggml)"
}

# ------------------------------------------------------------------ head --

# The head's copy runs exactly what the worker's runs: the image the worker built, loaded here
# under the same ID, and the q8_0 file the worker converted, checked here against the same pin.
fetch_image_from_worker() {
  local want have
  want="$(ssh_worker "docker image inspect -f '{{.Id}}' '$WHISPER_CPU_IMAGE' 2>/dev/null" </dev/null || true)"
  [[ "$want" == sha256:* ]] \
    || die "the worker has no $WHISPER_CPU_IMAGE to copy; run scripts/whisper-cpu.sh up (the worker's copy) first"
  have="$(docker image inspect -f '{{.Id}}' "$WHISPER_CPU_IMAGE" 2>/dev/null || true)"
  if [ "$have" != "$want" ]; then
    log_info "copying $WHISPER_CPU_IMAGE from the worker; nothing is compiled on the head"
    ssh_worker "docker save '$WHISPER_CPU_IMAGE'" </dev/null | docker load >/dev/null \
      || die "could not copy $WHISPER_CPU_IMAGE from the worker"
    have="$(docker image inspect -f '{{.Id}}' "$WHISPER_CPU_IMAGE" 2>/dev/null || true)"
    [ "$have" = "$want" ] || die "the image on the head is '${have:-none}', not the worker's $want"
  fi
  check_pass "image $WHISPER_CPU_IMAGE is the worker's build (${want:7:12})"
}

ensure_model_head() {
  local dir file partial
  dir="$WHISPER_CPU_HEAD_MODEL_CACHE/repos/$(ggml_dir_name)"
  file="$dir/ggml-large-v3-q8_0.bin"
  if [ -f "$file" ] && echo "$GGML_Q8_0_SHA256  $file" | sha256sum -c --status -; then
    check_pass "q8_0 model present on the head and matches its pin ($(ggml_dir_name))"
    return 0
  fi
  log_info "copying the q8_0 model from the worker (converted and checked there, never on the head)"
  mkdir -p "$dir"
  partial="$(mktemp "$dir/.ggml-large-v3-q8_0.bin.XXXXXX")"
  # An interrupted copy must not leave 1.7 GB behind in the model cache.
  # shellcheck disable=SC2064
  trap "rm -f -- '$partial'" EXIT
  if ! ssh_worker "cat '$WHISPER_MODEL_CACHE/repos/$(ggml_dir_name)/ggml-large-v3-q8_0.bin'" </dev/null >"$partial"; then
    rm -f "$partial"
    die "the worker has no q8_0 model to copy; run scripts/whisper-cpu.sh up (the worker's copy) first"
  fi
  if ! echo "$GGML_Q8_0_SHA256  $partial" | sha256sum -c --status -; then
    rm -f "$partial"
    die "the q8_0 file copied from the worker does not match its pin"
  fi
  # uid 10008 reads it through a read-only mount.
  chmod 0644 "$partial"
  mv -f "$partial" "$file"
  trap - EXIT
  check_pass "q8_0 model copied from the worker and verified ($(ggml_dir_name))"
}

# ------------------------------------------------------------------ endpoint --

# ASR_CPU_BASE_URLS in .env is the ONLY link between "the replica started" and "the orchestrator
# may use it", as ASR_BASE_URLS is for the GPU replicas (scripts/whisper.sh record_endpoints).
_set_env() { # _set_env KEY VALUE, idempotent, in .env
  local key="$1" value="$2" env_file="$ROOT/.env"
  touch "$env_file"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

# ASR_CPU_BASE_URLS lists every CPU copy, and the router offers an overflow clip to them IN THIS
# ORDER (RoutedProvider: the first free one takes it). So MERGE, never replace: `up` or `down` on
# one node leaves the other node's copy listed (scripts/whisper.sh record_endpoints; replacing is
# how a rebuild of one GPU engine silently halved the fleet on 2026-09-08). The head's copy (a
# Docker bridge gateway address; the worker's is its management address) always goes LAST: the
# head's cores also run the orchestrator, Postgres and the chat model's rank-0 engine loop.
cpu_endpoints_recorded() {
  grep -E '^ASR_CPU_BASE_URLS=' "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr ',' ' ' || true
}

# is_head_endpoint URL: the head copy's URL, a Docker bridge gateway (the worker's is 192.168.x).
is_head_endpoint() {
  [[ "$1" =~ ^http://172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}: ]]
}

# record_cpu_endpoint URL
record_cpu_endpoint() {
  local url
  local -a first=() last=() all=()
  for url in $(cpu_endpoints_recorded) "$1"; do
    case " ${first[*]-} ${last[*]-} " in *" $url "*) continue ;; esac
    if is_head_endpoint "$url"; then last+=("$url"); else first+=("$url"); fi
  done
  all=("${first[@]}" "${last[@]}")
  _set_env ASR_CPU_BASE_URLS "$(IFS=,; printf '%s' "${all[*]}")"
}

# forget_cpu_endpoint URL
forget_cpu_endpoint() {
  local url
  local -a kept=()
  for url in $(cpu_endpoints_recorded); do
    [ "$url" = "$1" ] || kept+=("$url")
  done
  _set_env ASR_CPU_BASE_URLS "$(IFS=,; printf '%s' "${kept[*]-}")"
}

compose_worker() { # compose_worker <bind> ARGS...
  local bind="$1"; shift
  ssh_worker "cd $REMOTE_DIR && WHISPER_BIND=$bind WHISPER_CPU_PORT=$WHISPER_CPU_PORT \
    WHISPER_CPU_MODEL_DIR='$WHISPER_MODEL_CACHE/repos/$(ggml_dir_name)' \
    docker compose --project-name $WHISPER_CPU_PROJECT -f compose.whisper-cpu.yaml $*" </dev/null
}

# The head's copy is run by this checkout's compose file, on the head's own Docker. Its placement
# is validated before `up`; for ps, logs and down a broken setting must not stand in the way.
# compose_head BIND ARGS...
compose_head() {
  local bind="$1" placement; shift
  placement="$(head_placement 2>/dev/null || true)"
  # shellcheck disable=SC2086
  env WHISPER_BIND="$bind" WHISPER_CPU_PORT="$WHISPER_CPU_PORT" \
    WHISPER_CPU_MODEL_DIR="$WHISPER_CPU_HEAD_MODEL_CACHE/repos/$(ggml_dir_name)" $placement \
    docker compose --project-name "$WHISPER_CPU_PROJECT" -f "$ROOT/compose/compose.whisper-cpu.yaml" "$@"
}

# compose_node BIND ARGS...: the copy WHISPER_CPU_NODE names.
compose_node() {
  if [ "$WHISPER_CPU_NODE" = head ]; then compose_head "$@"; else compose_worker "$@"; fi
}

wait_ready() { # wait_ready <bind>
  local bind="$1" deadline=$((SECONDS + WHISPER_CPU_READY_TIMEOUT_S)) body=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    body="$(curl -fsS -m 5 "http://$bind:$WHISPER_CPU_PORT/health" 2>/dev/null || true)"
    case "$body" in *'"ready":true'*) check_pass "replica ready at $bind:$WHISPER_CPU_PORT"; return 0 ;; esac
    sleep 5
  done
  die "the replica did not report ready within ${WHISPER_CPU_READY_TIMEOUT_S}s; last /health: ${body:-no answer}"
}

# ------------------------------------------------------------------ guard --

# THE PORT HAS NO AUTHENTICATION, so `up` never starts the replica on a worker whose packet filter
# lets the office LAN or the tailnet reach it, now or after the next reboot. The worker's guard
# judges only the ports it lists (scripts/host-guard.sh: `tcp dport != @guarded_ports accept`), so
# an unlisted port is open to every network the worker is on. Two readings, and neither needs root:
#   - the table loaded NOW: $GUARD_APPLIED_RULESET, written by `host-guard.sh apply`;
#   - the table the NEXT BOOT loads: `plan` of the boot copy $GUARD_BOOT_COPY, written by
#     `host-guard.sh install-boot`. A guard applied from the new script but not installed for boot
#     reopens the port at the next reboot, and `restart: unless-stopped` brings the replica back.
# Each must judge the port (set guarded_ports) and let the head's orchestrator through
# (set head_lan_ports).
GUARD_APPLIED_RULESET="/run/techsara-host-guard/ruleset.nft"
GUARD_BOOT_COPY="/usr/local/sbin/techsara-host-guard"
GUARD_OWNER_STEPS="copy this checkout's scripts/host-guard.sh to the worker's ~/.techsara-cluster/host-guard.sh, then on the worker: sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker && sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker && sudo bash ~/.techsara-cluster/host-guard.sh verify --role worker"

ruleset_guards_port() { # ruleset_guards_port PORT < ruleset (host-guard.sh's rendered form)
  awk -v port="$1" '
    /^[[:space:]]*set [A-Za-z_]+ \{/ { set = $2; next }
    set != "" && /elements = \{/ {
      line = $0
      sub(/.*elements = \{/, "", line)
      sub(/\}.*/, "", line)
      n = split(line, items, ",")
      for (i = 1; i <= n; i++) {
        item = items[i]
        gsub(/[[:space:]]/, "", item)
        if (item == port) { found[set] = 1 }
        else if (item ~ /^[0-9]+-[0-9]+$/) {
          split(item, range, "-")
          if (port + 0 >= range[1] + 0 && port + 0 <= range[2] + 0) { found[set] = 1 }
        }
      }
    }
    /^[[:space:]]*\}[[:space:]]*$/ { set = "" }
    END { exit !(("guarded_ports" in found) && ("head_lan_ports" in found)) }
  '
}

require_host_guard() {
  local applied boot
  # A missing file or boot copy reads as empty (no guard); only ssh itself failing is a read error.
  applied="$(ssh_worker "cat $GUARD_APPLIED_RULESET 2>/dev/null || true" </dev/null)" \
    || die "could not read the worker's packet filter over ssh ($CLUSTER_WORKER_SSH); nothing was started"
  printf '%s\n' "$applied" | ruleset_guards_port "$WHISPER_CPU_PORT" \
    || die "the worker's packet filter does not close port $WHISPER_CPU_PORT to the office LAN and the tailnet ($GUARD_APPLIED_RULESET must list it in guarded_ports and head_lan_ports). The replica has no authentication, so it was not started. Owner, as root: $GUARD_OWNER_STEPS"
  boot="$(ssh_worker "$GUARD_BOOT_COPY plan --role worker 2>/dev/null || true" </dev/null)" \
    || die "could not read the worker's boot-time packet filter over ssh ($CLUSTER_WORKER_SSH); nothing was started"
  printf '%s\n' "$boot" | ruleset_guards_port "$WHISPER_CPU_PORT" \
    || die "the worker's packet filter closes port $WHISPER_CPU_PORT now, but its boot copy ($GUARD_BOOT_COPY) does not, so the next reboot would reopen it while Docker restarts the replica. It was not started. Owner, as root: $GUARD_OWNER_STEPS"
  check_pass "the worker's packet filter closes $WHISPER_CPU_PORT to the office LAN and the tailnet, now and after a reboot"
}

# ------------------------------------------------------------------ commands --

cmd="${1:-status}"; shift || true
case "$cmd" in
  up)
    require_cluster
    [ "$WHISPER_CPU_NODE" = head ] || require_host_guard
    bind="$(bind_address)"
    if [ "$WHISPER_CPU_NODE" = head ]; then
      head_placement >/dev/null
      head_memory_floor
      fetch_image_from_worker
      ensure_model_head
      # --no-build: the image is the worker's, loaded above; a missing one must fail, not compile.
      compose_head "$bind" up -d --no-build
    else
      sync_files
      build_images
      ensure_model
      compose_worker "$bind" up -d
    fi
    wait_ready "$bind"
    record_cpu_endpoint "$(endpoint "$bind")"
    check_pass "recorded ASR_CPU_BASE_URLS=$(env_get "$ENV_FILE" ASR_CPU_BASE_URLS || true) in .env"
    log_info "the orchestrator uses it after its next restart (./techsara up); the GPU replicas are unchanged"
    if [ "$WHISPER_CPU_NODE" = head ]; then
      log_info "the head's copy is allowed while chat decode stays within 5 % of it idle: docs/voice/CPU-REPLICA-HEAD.md"
    fi
    ;;
  down|stop)
    require_cluster
    bind="$(bind_address)"
    compose_node "$bind" "$([ "$cmd" = down ] && echo down || echo stop)"
    forget_cpu_endpoint "$(endpoint "$bind")"
    check_pass "recorded ASR_CPU_BASE_URLS=$(env_get "$ENV_FILE" ASR_CPU_BASE_URLS || true) in .env"
    log_info "restart the orchestrator to stop routing to it:  ./techsara up"
    ;;
  status)
    require_cluster
    bind="$(bind_address)"
    compose_node "$bind" ps || true
    curl -fsS -m 5 "http://$bind:$WHISPER_CPU_PORT/health" || echo "  replica not answering"
    echo
    ;;
  logs)
    require_cluster
    bind="$(bind_address)"
    compose_node "$bind" logs -f
    ;;
  url)
    require_cluster
    bind="$(bind_address)"
    endpoint "$bind"; echo
    ;;
  verify)
    require_cluster
    bind="$(bind_address)"
    clip="${TMPDIR:-/tmp}/whisper-verify.flac"
    [ -f "$clip" ] || curl -fsSL -o "$clip" https://github.com/openai/whisper/raw/main/tests/jfk.flac \
      || die "could not fetch the verification clip"
    started=$SECONDS
    curl -fsS -m 300 -F "file=@$clip" -F "model=$WHISPER_MODEL" \
      "http://$bind:$WHISPER_CPU_PORT/v1/audio/transcriptions" || die "no answer from the replica"
    echo
    log_info "answered in $((SECONDS - started)) s (the JFK clip is 11 s of audio)"
    ;;
  *)
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
