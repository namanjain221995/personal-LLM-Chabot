#!/usr/bin/env bash
# Speech-to-text overflow: openai/whisper-large-v3 on the WORKER's CPU cores.
#
#   scripts/whisper-cpu.sh up       convert the pinned weights (once), build the image, start the
#                                   replica on the worker, wait until it is ready, and record its
#                                   address as ASR_CPU_BASE_URLS in .env
#   scripts/whisper-cpu.sh down     stop and remove it (the converted weights stay) and take it
#                                   back out of .env
#   scripts/whisper-cpu.sh status   container state + the replica's own /health
#   scripts/whisper-cpu.sh logs     follow the replica's log
#   scripts/whisper-cpu.sh verify   transcribe a real clip end to end, print it and the time taken
#   scripts/whisper-cpu.sh url      print the endpoint the orchestrator should use
#
# WHAT IT IS. A third copy of the SAME model the two GPU replicas run (scripts/whisper.sh), on ten
# Cortex-X925 cores of the worker and no GPU. The orchestrator prefers the GPU replicas and sends a
# clip here only when every one of them is already decoding, and only if the clip can finish inside
# its deadline at this copy's measured speed (ASR_CPU_* in orchestrator/app/config.py). Measured
# before it was built: docs/voice/CPU-REPLICA.md.
#
# WORKER ONLY, AND DUAL MODE ONLY. The head's memory is off limits for anything new (owner,
# 2026-09-16), so there is no head variant and no single-node fallback: this script refuses.
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

hf_dir_name() { printf '%s--%s' "${WHISPER_MODEL//\//--}" "${WHISPER_MODEL_REVISION:0:12}"; }
ggml_dir_name() { printf '%s-ggml' "$(hf_dir_name)"; }

require_worker() {
  [ "${CLUSTER_MODE:-single}" = "dual" ] \
    || die "the CPU speech replica runs on the worker only, and CLUSTER_MODE is '${CLUSTER_MODE:-single}'. The head's memory is off limits for new services."
  [ -n "${CLUSTER_WORKER_SSH:-}" ] || die "CLUSTER_WORKER_SSH is not set"
}

run_worker() { # run_worker - script on stdin
  # shellcheck disable=SC2086
  ssh -o BatchMode=yes -o ConnectTimeout=8 ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" "bash -s"
}

# The worker's MANAGEMENT address (enP7s7), read over ssh: never 0.0.0.0, never a RoCE address.
# Same rule, same reasons, as scripts/whisper.sh whisper_bind_address.
bind_address() {
  local address
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

compose_worker() { # compose_worker <bind> ARGS...
  local bind="$1"; shift
  ssh_worker "cd $REMOTE_DIR && WHISPER_BIND=$bind WHISPER_CPU_PORT=$WHISPER_CPU_PORT \
    WHISPER_CPU_MODEL_DIR='$WHISPER_MODEL_CACHE/repos/$(ggml_dir_name)' \
    docker compose --project-name $WHISPER_CPU_PROJECT -f compose.whisper-cpu.yaml $*" </dev/null
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
  applied="$(ssh_worker "cat $GUARD_APPLIED_RULESET 2>/dev/null" </dev/null)" || applied=""
  printf '%s\n' "$applied" | ruleset_guards_port "$WHISPER_CPU_PORT" \
    || die "the worker's packet filter does not close port $WHISPER_CPU_PORT to the office LAN and the tailnet ($GUARD_APPLIED_RULESET must list it in guarded_ports and head_lan_ports). The replica has no authentication, so it was not started. Owner, as root: $GUARD_OWNER_STEPS"
  boot="$(ssh_worker "$GUARD_BOOT_COPY plan --role worker 2>/dev/null" </dev/null)" || boot=""
  printf '%s\n' "$boot" | ruleset_guards_port "$WHISPER_CPU_PORT" \
    || die "the worker's packet filter closes port $WHISPER_CPU_PORT now, but its boot copy ($GUARD_BOOT_COPY) does not, so the next reboot would reopen it while Docker restarts the replica. It was not started. Owner, as root: $GUARD_OWNER_STEPS"
  check_pass "the worker's packet filter closes $WHISPER_CPU_PORT to the office LAN and the tailnet, now and after a reboot"
}

# ------------------------------------------------------------------ commands --

cmd="${1:-status}"; shift || true
case "$cmd" in
  up)
    require_worker
    require_host_guard
    bind="$(bind_address)"
    sync_files
    build_images
    ensure_model
    compose_worker "$bind" up -d
    wait_ready "$bind"
    _set_env ASR_CPU_BASE_URLS "$(endpoint "$bind")"
    check_pass "recorded ASR_CPU_BASE_URLS=$(endpoint "$bind") in .env"
    log_info "the orchestrator uses it after its next restart (./techsara up); the GPU replicas are unchanged"
    ;;
  down|stop)
    require_worker
    bind="$(bind_address)"
    compose_worker "$bind" "$([ "$cmd" = down ] && echo down || echo stop)"
    _set_env ASR_CPU_BASE_URLS ""
    check_pass "recorded ASR_CPU_BASE_URLS= (empty) in .env"
    log_info "restart the orchestrator to stop routing to it:  ./techsara up"
    ;;
  status)
    require_worker
    bind="$(bind_address)"
    compose_worker "$bind" ps || true
    curl -fsS -m 5 "http://$bind:$WHISPER_CPU_PORT/health" || echo "  replica not answering"
    echo
    ;;
  logs)
    require_worker
    compose_worker "$(bind_address)" logs -f
    ;;
  url)
    require_worker
    endpoint "$(bind_address)"; echo
    ;;
  verify)
    require_worker
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
