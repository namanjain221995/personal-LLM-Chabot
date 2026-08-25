#!/usr/bin/env bash
# Shared helpers for scripts/cluster-*.sh (two-node DGX Spark vLLM cluster).
# Source this file; do not execute it.
#
# Settings come from two places, in this order (later wins):
#   1. .env                    user-owned CLUSTER_* keys (see .env.example)
#   2. .runtime/generated.env  launcher-generated keys (CLUSTER_ENGINE_ARGS,
#                              CLUSTER_NCCL_*, CLUSTER_API_BIND_ADDRESS, ...)
# Nothing here reads secrets.env.

set -euo pipefail

CLUSTER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$CLUSTER_LIB_DIR/../.." && pwd)"
RUNTIME_DIR="$ROOT/.runtime"
LOG_DIR="$RUNTIME_DIR/logs"
ENV_FILE="$ROOT/.env"
GENERATED_ENV="$RUNTIME_DIR/generated.env"
SECRETS_ENV="$RUNTIME_DIR/secrets.env"
STATE_JSON="$RUNTIME_DIR/state.json"
WORKER_ENV_LOCAL="$RUNTIME_DIR/cluster-worker.env"
HEAD_OVERLAY_REL="compose/compose.cluster-dgx-spark.yaml"
WORKER_COMPOSE_LOCAL="$ROOT/compose/compose.cluster-worker.yaml"
WORKER_REMOTE_DIR='$HOME/.techsara-cluster'   # expanded by the remote shell
HEAD_PROJECT="sf-local-ai"
WORKER_PROJECT="sf-local-ai-worker"
NCCL_BENCH_SCRIPT="$CLUSTER_LIB_DIR/nccl_allreduce_bench.py"

# ---------------------------------------------------------------- output ----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""; C_OFF=""
fi
CHECK_PASS=0; CHECK_WARN=0; CHECK_FAIL=0
log_info() { printf '%s[info]%s %s\n' "$C_BLU" "$C_OFF" "$*"; }
log_dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
check_pass() { CHECK_PASS=$((CHECK_PASS+1)); printf '  %sPASS%s  %s\n' "$C_GRN" "$C_OFF" "$*"; }
check_warn() { CHECK_WARN=$((CHECK_WARN+1)); printf '  %sWARN%s  %s\n' "$C_YEL" "$C_OFF" "$*"; }
check_fail() { CHECK_FAIL=$((CHECK_FAIL+1)); printf '  %sFAIL%s  %s\n' "$C_RED" "$C_OFF" "$*"; }
die() { printf '%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 2; }
section() { printf '\n%s== %s ==%s\n' "$C_BLU" "$*" "$C_OFF"; }
check_summary() {
  printf '\n%d passed, %d warnings, %d failed\n' "$CHECK_PASS" "$CHECK_WARN" "$CHECK_FAIL"
  [ "$CHECK_FAIL" -eq 0 ]
}

# ------------------------------------------------------------- env files ----
# env_get FILE KEY -> prints the value of the LAST assignment (no shell eval;
# surrounding single/double quotes are stripped, inline comments are not).
env_get() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1)" || return 1
  line="${line#*=}"
  case "$line" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

# cluster_load_settings: populate CLUSTER_* and friends from .env + generated.env.
cluster_load_settings() {
  local k v
  for k in CLUSTER_MODE CLUSTER_HEAD_IP CLUSTER_WORKER_IP CLUSTER_HEAD_IP_2 CLUSTER_WORKER_IP_2 \
           CLUSTER_MASTER_PORT CLUSTER_WORKER_SSH CLUSTER_WORKER_SSH_OPTS CLUSTER_WORKER_MODEL_CACHE \
           CLUSTER_WORKER_NCCL_SOCKET_IFNAME CLUSTER_WORKER_NCCL_IB_HCA CLUSTER_NCCL_DEBUG \
           CLUSTER_TENSOR_PARALLEL_SIZE CLUSTER_PIPELINE_PARALLEL_SIZE CLUSTER_GPU_MEMORY_UTILIZATION \
           PUBLISH_MODEL_PORTS VLLM_PORT; do
    v="$(env_get "$ENV_FILE" "$k" 2>/dev/null || true)"
    [ -n "$v" ] && printf -v "$k" '%s' "$v"
  done
  for k in TECHSARA_CLUSTER_MODE CLUSTER_HEAD_IP CLUSTER_WORKER_IP CLUSTER_HEAD_IP_2 CLUSTER_WORKER_IP_2 \
           CLUSTER_MASTER_PORT CLUSTER_TENSOR_PARALLEL_SIZE CLUSTER_PIPELINE_PARALLEL_SIZE \
           CLUSTER_GPU_MEMORY_UTILIZATION CLUSTER_NCCL_SOCKET_IFNAME CLUSTER_NCCL_IB_HCA CLUSTER_NCCL_DEBUG \
           CLUSTER_API_BIND_ADDRESS CLUSTER_ENGINE_ARGS CLUSTER_WORKER_SSH MAIN_MODEL MAIN_MODEL_CONTAINER_PATH \
           TECHSARA_MODEL_CACHE MODEL_MAX_CONTEXT VLLM_PORT TECHSARA_PUBLISH_MODEL_PORTS; do
    v="$(env_get "$GENERATED_ENV" "$k" 2>/dev/null || true)"
    [ -n "$v" ] && printf -v "$k" '%s' "$v"
  done
  # A generated TECHSARA_CLUSTER_MODE reflects what is actually deployed; the
  # .env CLUSTER_MODE is the user's intent for the next `up`.
  CLUSTER_MODE="${TECHSARA_CLUSTER_MODE:-${CLUSTER_MODE:-single}}"
  CLUSTER_MASTER_PORT="${CLUSTER_MASTER_PORT:-29501}"
  VLLM_PORT="${VLLM_PORT:-8000}"
  CLUSTER_WORKER_SSH="${CLUSTER_WORKER_SSH:-$(id -un)@${CLUSTER_WORKER_IP:-}}"
  CLUSTER_WORKER_MODEL_CACHE="${CLUSTER_WORKER_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-}}"
  CLUSTER_API_BIND_ADDRESS="${CLUSTER_API_BIND_ADDRESS:-0.0.0.0}"
  CLUSTER_TENSOR_PARALLEL_SIZE="${CLUSTER_TENSOR_PARALLEL_SIZE:-2}"
  CLUSTER_PIPELINE_PARALLEL_SIZE="${CLUSTER_PIPELINE_PARALLEL_SIZE:-1}"
  CLUSTER_GPU_MEMORY_UTILIZATION="${CLUSTER_GPU_MEMORY_UTILIZATION:-0.30}"
  export CLUSTER_MODE CLUSTER_HEAD_IP CLUSTER_WORKER_IP CLUSTER_HEAD_IP_2 CLUSTER_WORKER_IP_2 \
         CLUSTER_MASTER_PORT CLUSTER_WORKER_SSH CLUSTER_WORKER_MODEL_CACHE VLLM_PORT
}

require_dual_mode() {
  [ "${CLUSTER_MODE:-single}" = "dual" ] || die "CLUSTER_MODE is '${CLUSTER_MODE:-single}'. Set CLUSTER_MODE=dual (plus CLUSTER_HEAD_IP / CLUSTER_WORKER_IP) in .env for the two-node cluster; single-node mode is just ./techsara up."
  [ -n "${CLUSTER_HEAD_IP:-}" ] || die "CLUSTER_HEAD_IP is not set in .env"
  [ -n "${CLUSTER_WORKER_IP:-}" ] || die "CLUSTER_WORKER_IP is not set in .env"
}

api_host() {
  case "${CLUSTER_API_BIND_ADDRESS:-0.0.0.0}" in
    0.0.0.0|"") printf '127.0.0.1' ;;
    *) printf '%s' "$CLUSTER_API_BIND_ADDRESS" ;;
  esac
}
api_url() { printf 'http://%s:%s' "$(api_host)" "${VLLM_PORT:-8000}"; }

# ------------------------------------------------------------------ ssh ----
# ssh_worker CMD... : run a command on the worker host (BatchMode: keys only).
ssh_worker() {
  # shellcheck disable=SC2086
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=15 ${CLUSTER_WORKER_SSH_OPTS:-} "$CLUSTER_WORKER_SSH" -- "$@"
}
scp_to_worker() { # scp_to_worker LOCAL... REMOTE_RELATIVE_DIR
  local n=$#; local dest="${!n}"; local files=("${@:1:$((n-1))}")
  # shellcheck disable=SC2086
  scp -q -o BatchMode=yes -o ConnectTimeout=8 ${CLUSTER_WORKER_SSH_OPTS:-} "${files[@]}" "$CLUSTER_WORKER_SSH:$dest/"
}

# ------------------------------------------------------------- detection ----
# Interface / RDMA detection by IP (works locally; the same text is shipped
# to the worker through `ssh_worker "$(detect_snippet); ..."`).
detect_snippet() {
  cat <<'SNIP'
detect_ifname_for_ip() { ip -o -4 addr show 2>/dev/null | awk -v ip="$1" '$4 ~ ("^" ip "/") {print $2; exit}'; }
detect_hca_for_ifname() { local d; for d in /sys/class/infiniband/*; do [ -d "$d/device/net/$1" ] && { basename "$d"; return 0; }; done; return 1; }
rdma_state_for_hca() { rdma link show "$1/1" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="state") {print $(i+1); exit}}'; }
mtu_for_ifname() { cat "/sys/class/net/$1/mtu" 2>/dev/null; }
SNIP
}
eval "$(detect_snippet)"

# ---------------------------------------------------------------- compose ----
# head_compose ARGS... : docker compose for the Node 1 project with the same
# env-file chain and overlays the launcher uses (read back from state.json when
# it already records the cluster overlay, otherwise the dgx-spark defaults).
head_compose() {
  local -a cmd
  if [ -f "$STATE_JSON" ] && python3 - "$STATE_JSON" "$HEAD_OVERLAY_REL" <<'PY' >/dev/null 2>&1
import json, sys
state = json.load(open(sys.argv[1]))
sys.exit(0 if sys.argv[2] in state.get("compose_files", []) else 1)
PY
  then
    mapfile -t cmd < <(python3 - "$STATE_JSON" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
argv = state["compose_command"]
cut = argv.index("up") if "up" in argv else len(argv)
print("\n".join(argv[:cut]))
PY
)
  else
    cmd=(docker compose --project-name "$HEAD_PROJECT" --env-file "$ENV_FILE")
    [ -f "$SECRETS_ENV" ] && cmd+=(--env-file "$SECRETS_ENV")
    cmd+=(--env-file "$GENERATED_ENV" -f "$ROOT/compose.yaml" -f "$ROOT/compose/compose.dgx-spark.yaml")
    case "${TECHSARA_PUBLISH_MODEL_PORTS:-${PUBLISH_MODEL_PORTS:-false}}" in
      true|1|yes) cmd+=(-f "$ROOT/compose/compose.published-dgx-spark.yaml") ;;
    esac
    # The cluster overlay only renders with the dual-mode generated keys.
    case "${TECHSARA_CLUSTER_MODE:-${CLUSTER_MODE:-single}}" in
      dual) cmd+=(-f "$ROOT/$HEAD_OVERLAY_REL") ;;
    esac
  fi
  (cd "$ROOT" && "${cmd[@]}" "$@")
}

# worker_compose ARGS... : docker compose for the worker project on Node 2.
worker_compose() {
  ssh_worker "cd $WORKER_REMOTE_DIR && docker compose --project-name $WORKER_PROJECT --env-file worker.env -f compose.cluster-worker.yaml $*"
}

head_vllm_image() { head_compose config --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["vllm"]["image"])'; }
head_container_id() { head_compose ps -q vllm 2>/dev/null | head -n 1; }

# compose_service_state PROJECT-JSON-LINE -> "state health" for one service
compose_ps_state() { # compose_ps_state <json from `compose ps --format json`>
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print("absent -"); sys.exit(0)
rows = [json.loads(l) for l in raw.splitlines() if l.strip()] if raw[0] != "[" else json.loads(raw)
r = rows[0]
print(r.get("State", "?"), r.get("Health") or "-")
'
}
