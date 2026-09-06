#!/usr/bin/env bash
# Serving benchmark against the live OpenAI-compatible API using vLLM's own
# `vllm bench serve` (inside the head container: no extra dependencies), with
# GPU-utilisation sampling on BOTH nodes for the duration of the run.
#   scripts/cluster-bench.sh [--input-len 512] [--output-len 128] [--num-prompts 16] [--concurrency 4] [--seed 1]
# Results are printed and saved under .runtime/logs/cluster-bench-<timestamp>.txt
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
IN=512; OUT=128; N=16; C=4; SEED=1
while [ $# -gt 0 ]; do case "$1" in --input-len) IN="$2"; shift ;; --output-len) OUT="$2"; shift ;; --num-prompts) N="$2"; shift ;; --concurrency) C="$2"; shift ;; --seed) SEED="$2"; shift ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) die "unknown option $1" ;; esac; shift; done
cluster_load_settings
mkdir -p "$LOG_DIR"
hid="$(head_container_id || true)"; [ -n "$hid" ] || die "head vllm container is not running"
curl -fsS -m 5 "$(api_url)/v1/models" >/dev/null || die "API not responding at $(api_url)"
[ -n "${MAIN_MODEL:-}" ] && [ -n "${MAIN_MODEL_CONTAINER_PATH:-}" ] || die "MAIN_MODEL / MAIN_MODEL_CONTAINER_PATH missing from generated.env"

# `vllm bench serve` runs INSIDE the head container, so the address it dials is
# the CONTAINER's view of the API, not the host's. In dual mode the head is
# host-networked and the two coincide; in single mode it sits on a bridge and
# listens on its own internal port, which the host merely publishes as
# $VLLM_PORT. Passing the host's address into the container then connects to
# nothing -- and `vllm bench serve` answers that with "Successful requests: 0",
# every metric 0.00, and exit status 0. A silent zero shaped exactly like a
# measurement. So resolve the endpoint by probing from inside the container,
# and refuse to report anything if none of the candidates answers.
container_port_for_host_port() {
  docker inspect -f '{{range $p, $c := .NetworkSettings.Ports}}{{if $c}}{{$p}}={{(index $c 0).HostPort}}{{"\n"}}{{end}}{{end}}' "$hid" 2>/dev/null \
    | awk -F= -v hp="$VLLM_PORT" '$2==hp {sub("/.*","",$1); print $1; exit}'
}
resolve_bench_endpoint() {
  local cand h p
  for cand in "$(api_host):$VLLM_PORT" "127.0.0.1:$(container_port_for_host_port)" "127.0.0.1:$VLLM_PORT"; do
    h="${cand%:*}"; p="${cand##*:}"
    [ -n "$h" ] && [ -n "$p" ] || continue
    if docker exec "$hid" curl -fsS -m 5 -o /dev/null "http://$h:$p/v1/models" 2>/dev/null; then
      printf '%s %s' "$h" "$p"; return 0
    fi
  done
  return 1
}
# Capture first: `|| die` after `read` binds to READ, and a here-string always
# supplies a trailing newline, so read succeeds on empty input and the message
# never fires.
bench_ep="$(resolve_bench_endpoint)" \
  || die "the API is up on the host but unreachable from inside the head container; tried $(api_host):$VLLM_PORT and 127.0.0.1:$(container_port_for_host_port)"
read -r BH BP <<<"$bench_ep"
[ -n "${BH:-}" ] && [ -n "${BP:-}" ] || die "could not resolve an in-container API endpoint for the benchmark"

# Report the parallelism the ENGINE is actually running, read from its argv --
# not the .env intent, which stays at its cluster value even when the engine
# was started single-node and makes every single-mode result read "TP=2".
cl="$(docker inspect -f '{{join .Config.Entrypoint " "}} {{join .Config.Cmd " "}}' "$hid" 2>/dev/null || true)"
eff_arg() { printf '%s' "$cl" | grep -oE -- "--$1[= ]+[0-9]+" | tail -1 | grep -oE '[0-9]+$' || true; }
TP_EFF="$(eff_arg tensor-parallel-size)"; TP_SRC="engine argv"
[ -n "$TP_EFF" ] || { TP_EFF=1; TP_SRC="vLLM default, flag absent"; }
PP_EFF="$(eff_arg pipeline-parallel-size)"; PP_SRC="engine argv"
[ -n "$PP_EFF" ] || { PP_EFF=1; PP_SRC="vLLM default, flag absent"; }

ts="$(date +%Y%m%d-%H%M%S)"; out="$LOG_DIR/cluster-bench-$ts.txt"
s1="$(mktemp)"; s2="$(mktemp)"
trap 'kill $P1 ${P2:-} 2>/dev/null || true; rm -f "$s1" "$s2"' EXIT
nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500 > "$s1" 2>/dev/null & P1=$!
P2=""; if [ "$CLUSTER_MODE" = dual ]; then ssh_worker "timeout 1800 nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500" > "$s2" 2>/dev/null & P2=$!; fi
log_info "vllm bench serve: ${IN} in / ${OUT} out, ${N} prompts, concurrency ${C}, seed ${SEED}  ->  $out"
{
  echo "# cluster-bench $ts  mode=$CLUSTER_MODE TP=$TP_EFF ($TP_SRC) PP=$PP_EFF ($PP_SRC) util=$CLUSTER_GPU_MEMORY_UTILIZATION  in=$IN out=$OUT n=$N c=$C  endpoint=$BH:$BP"
  docker exec "$hid" vllm bench serve --backend openai-chat --host "$BH" --port "$BP" --endpoint /v1/chat/completions \
    --model "$MAIN_MODEL" --tokenizer "$MAIN_MODEL_CONTAINER_PATH" --trust-remote-code --dataset-name random \
    --random-input-len "$IN" --random-output-len "$OUT" --num-prompts "$N" --max-concurrency "$C" --ignore-eos \
    --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,95,99 --seed "$SEED" 2>&1 | grep -vE 'it/s\]|^\s*$|Warning|warn'
} | tee "$out"
kill $P1 2>/dev/null || true; [ -n "$P2" ] && { kill $P2 2>/dev/null || true; }; wait 2>/dev/null || true
summ() { awk 'NF{n++; if($1+0>m)m=$1+0; s+=$1; if($1+0>0)b++} END{if(n) printf "max %d%%  avg %.0f%%  busy-samples %d/%d", m, s/n, b, n; else printf "no samples"}' "$1"; }
# `[ ... ] && echo` as the LAST command in this group makes the whole group
# exit 1 in single mode, and with `set -euo pipefail` a failing pipeline ends
# the script right here -- which is exactly where the zero-request guard below
# stops being reachable. Use an `if` so the group's status is the tee's.
{ echo; echo "GPU utilisation during the run:"; echo "  Node 1 GB10: $(summ "$s1")"
  if [ "$CLUSTER_MODE" = dual ]; then echo "  Node 2 GB10: $(summ "$s2")"; fi
} | tee -a "$out"
rm -f "$s1" "$s2"
# A run that completed no requests is not a result. Fail loudly so it can never
# be copied into a comparison table as a row of honest-looking zeros.
ok="$(awk '/Successful requests:/{print $3; exit}' "$out")"
[ "${ok:-0}" -gt 0 ] 2>/dev/null || die "benchmark completed 0 requests (endpoint $BH:$BP) -- refusing to report zeros as a measurement; see $out"
