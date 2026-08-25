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
ts="$(date +%Y%m%d-%H%M%S)"; out="$LOG_DIR/cluster-bench-$ts.txt"
s1="$(mktemp)"; s2="$(mktemp)"
nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500 > "$s1" 2>/dev/null & P1=$!
P2=""; if [ "$CLUSTER_MODE" = dual ]; then ssh_worker "timeout 1800 nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500" > "$s2" 2>/dev/null & P2=$!; fi
log_info "vllm bench serve: ${IN} in / ${OUT} out, ${N} prompts, concurrency ${C}, seed ${SEED}  ->  $out"
{
  echo "# cluster-bench $ts  mode=$CLUSTER_MODE TP=$CLUSTER_TENSOR_PARALLEL_SIZE PP=$CLUSTER_PIPELINE_PARALLEL_SIZE util=$CLUSTER_GPU_MEMORY_UTILIZATION  in=$IN out=$OUT n=$N c=$C"
  docker exec "$hid" vllm bench serve --backend openai-chat --host "$(api_host)" --port "$VLLM_PORT" --endpoint /v1/chat/completions \
    --model "$MAIN_MODEL" --tokenizer "$MAIN_MODEL_CONTAINER_PATH" --trust-remote-code --dataset-name random \
    --random-input-len "$IN" --random-output-len "$OUT" --num-prompts "$N" --max-concurrency "$C" --ignore-eos \
    --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,95,99 --seed "$SEED" 2>&1 | grep -vE 'it/s\]|^\s*$|Warning|warn'
} | tee "$out"
kill $P1 2>/dev/null || true; [ -n "$P2" ] && { kill $P2 2>/dev/null || true; }; wait 2>/dev/null || true
summ() { awk 'NF{n++; if($1+0>m)m=$1+0; s+=$1; if($1+0>0)b++} END{if(n) printf "max %d%%  avg %.0f%%  busy-samples %d/%d", m, s/n, b, n; else printf "no samples"}' "$1"; }
{ echo; echo "GPU utilisation during the run:"; echo "  Node 1 GB10: $(summ "$s1")"; [ "$CLUSTER_MODE" = dual ] && echo "  Node 2 GB10: $(summ "$s2")"; } | tee -a "$out"
rm -f "$s1" "$s2"
