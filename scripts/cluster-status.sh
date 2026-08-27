#!/usr/bin/env bash
# Cluster status report. Every line comes from a live check; nothing is assumed.
# Usage: scripts/cluster-status.sh [--probe] [--brief]
#   --probe  also send one streaming chat completion and sample GPU utilisation
#            on BOTH nodes while it runs (proves both GB10s participate)
# Exit status is non-zero when a critical check fails.
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
PROBE=0; BRIEF=0
for a in "$@"; do case "$a" in --probe) PROBE=1 ;; --brief) BRIEF=1 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) die "unknown option $a" ;; esac; done
cluster_load_settings

node_report() { # node_report LABEL IP IP2   (runs locally)
  local label="$1" ip="$2" ip2="$3" ifn hca st mtu gpu util
  gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)"
  util="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -n1)"
  printf '  Reachable: YES (%s)\n  GPU: %s\n  GPU utilization: %s%%\n' "$(hostname)" "${gpu:-none}" "${util:-?}"
  printf '  GPU processes: %s\n' "$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null | wc -l)"
  local i=A; for a in "$ip" "$ip2"; do
    [ -n "$a" ] || { i=B; continue; }
    ifn="$(detect_ifname_for_ip "$a" || true)"; hca="$(detect_hca_for_ifname "${ifn:-none}" 2>/dev/null || true)"; st="$(rdma_state_for_hca "${hca:-none}" 2>/dev/null || true)"; mtu="$(mtu_for_ifname "${ifn:-none}" || true)"
    printf '  RDMA link %s: %s  (%s %s hca=%s mtu=%s)\n' "$i" "${st:-NOT FOUND}" "${ifn:-?}" "$a" "${hca:-none}" "${mtu:-?}"
    if [ "$st" = "ACTIVE" ]; then check_pass "$label link $i ACTIVE" >/dev/null; else check_fail "$label link $i not ACTIVE" >/dev/null; fi
    i=B
  done
  [ -n "$gpu" ] && check_pass "$label GPU visible" >/dev/null || check_fail "$label GPU not visible" >/dev/null
}

echo "========================================"
echo "DGX CLUSTER STATUS   ($(date -Is))"
echo "========================================"
echo "Mode: ${CLUSTER_MODE}   TP=${CLUSTER_TENSOR_PARALLEL_SIZE} PP=${CLUSTER_PIPELINE_PARALLEL_SIZE}   gpu-mem-util=${CLUSTER_GPU_MEMORY_UTILIZATION}"
echo
echo "Node 1 (head, ${CLUSTER_HEAD_IP:-?})"
node_report "node1" "${CLUSTER_HEAD_IP:-}" "${CLUSTER_HEAD_IP_2:-}"
echo
echo "Node 2 (worker, ${CLUSTER_WORKER_IP:-?})"
if [ "$CLUSTER_MODE" = "dual" ]; then
  if remote="$(ssh_worker "$(detect_snippet)
echo HOST=\$(hostname)
echo GPU=\$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)
echo UTIL=\$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -n1)
echo PROCS=\$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | wc -l)
for a in '${CLUSTER_WORKER_IP}' '${CLUSTER_WORKER_IP_2:-}'; do [ -n \"\$a\" ] || { echo LINK=; continue; }; ifn=\$(detect_ifname_for_ip \"\$a\"); hca=\$(detect_hca_for_ifname \"\${ifn:-none}\" 2>/dev/null); st=\$(rdma_state_for_hca \"\${hca:-none}\" 2>/dev/null); echo LINK=\${st:-NOT FOUND}\\|\${ifn:-?}\\|\$a\\|\${hca:-none}\\|\$(mtu_for_ifname \"\${ifn:-none}\"); done" 2>/dev/null)"; then
    rh="$(printf '%s\n' "$remote" | sed -n 's/^HOST=//p')"; gpu="$(printf '%s\n' "$remote" | sed -n 's/^GPU=//p')"
    printf '  Reachable: YES (%s)\n  GPU: %s\n  GPU utilization: %s%%\n  GPU processes: %s\n' "$rh" "${gpu:-none}" "$(printf '%s\n' "$remote" | sed -n 's/^UTIL=//p')" "$(printf '%s\n' "$remote" | sed -n 's/^PROCS=//p')"
    check_pass "node2 reachable" >/dev/null
    [ -n "$gpu" ] && check_pass "node2 GPU visible" >/dev/null || check_fail "node2 GPU not visible" >/dev/null
    i=A; printf '%s\n' "$remote" | sed -n 's/^LINK=//p' | while IFS='|' read -r st ifn a hca mtu; do
      [ -n "$st" ] && printf '  RDMA link %s: %s  (%s %s hca=%s mtu=%s)\n' "$i" "$st" "$ifn" "$a" "$hca" "$mtu"; i=B; done
    printf '%s\n' "$remote" | sed -n 's/^LINK=//p' | grep -q '^ACTIVE' && check_pass "node2 link A ACTIVE" >/dev/null || check_fail "node2 link A not ACTIVE" >/dev/null
  else
    echo "  Reachable: NO (ssh $CLUSTER_WORKER_SSH failed)"; check_fail "node2 unreachable" >/dev/null
  fi
else
  echo "  (single-node mode: no worker)"
fi

echo
echo "Distributed runtime"
hid="$(head_container_id || true)"
if [ -n "$hid" ]; then
  read -r hstate hhealth < <(head_compose ps --format json vllm 2>/dev/null | compose_ps_state)
  printf '  Head (vllm, node-rank 0): %s / %s\n' "$hstate" "$hhealth"
  [ "$hhealth" = "healthy" ] && check_pass "head healthy" >/dev/null || check_fail "head not healthy ($hstate/$hhealth)" >/dev/null
else
  echo "  Head (vllm): NOT RUNNING"; check_fail "head container not running" >/dev/null
fi
if [ "$CLUSTER_MODE" = "dual" ]; then
  read -r wstate whealth < <(worker_compose ps --format json 2>/dev/null | compose_ps_state || echo "absent -")
  printf '  Worker (vllm-worker, node-rank 1): %s / %s\n' "$wstate" "$whealth"
  [ "$whealth" = "healthy" ] && check_pass "worker healthy" >/dev/null || check_fail "worker not healthy ($wstate/$whealth)" >/dev/null
fi

echo
echo "vLLM"
models_json="$(curl -fsS -m 5 "$(api_url)/v1/models" 2>/dev/null || true)"
if [ -n "$models_json" ]; then
  printf '%s' "$models_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print("  Model: %s  (max_model_len %s)"%(m["id"], m.get("max_model_len"))) for m in d["data"]]'
  check_pass "API responds at $(api_url)" >/dev/null
else
  echo "  API: NOT RESPONDING at $(api_url)"; check_fail "API not responding" >/dev/null
fi
if [ -n "$hid" ]; then
  # Every grep below MUST tolerate no match. The engine's start-up banner rolls
  # out of the log window after a day or so of request logging, and under
  # `set -euo pipefail` an empty grep inside $(...) aborts the whole script -
  # which made this report exit 1 on a perfectly healthy cluster once the head
  # had been up ~41 hours, and failed a deploy health gate for no reason.
  logs="$(docker logs "$hid" 2>&1 | tail -n 20000 || true)"
  # Parallelism comes from the RUNNING CONTAINER'S ARGV, which cannot roll over,
  # and only falls back to the log banner if that is somehow unavailable.
  tp="$(docker inspect "$hid" --format '{{json .Config.Cmd}}' 2>/dev/null \
        | python3 -c 'import json,sys
try:
    c=json.load(sys.stdin); print(c[c.index("--tensor-parallel-size")+1])
except Exception: print("")' 2>/dev/null || true)"
  pp="$(docker inspect "$hid" --format '{{json .Config.Cmd}}' 2>/dev/null \
        | python3 -c 'import json,sys
try:
    c=json.load(sys.stdin); print(c[c.index("--pipeline-parallel-size")+1])
except Exception: print("")' 2>/dev/null || true)"
  eng="$(printf '%s\n' "$logs" | grep -E "Initializing a V1 LLM engine" | tail -n1 || true)"
  [ -n "$tp" ] || tp="$(printf '%s' "$eng" | grep -oE 'tensor_parallel_size=[0-9]+' | cut -d= -f2 || true)"
  [ -n "$pp" ] || pp="$(printf '%s' "$eng" | grep -oE 'pipeline_parallel_size=[0-9]+' | cut -d= -f2 || true)"
  ws="$(printf '%s\n' "$logs" | grep -oE 'world_size=[0-9]+' | tail -n1 | cut -d= -f2 || true)"
  kv="$(printf '%s\n' "$logs" | grep -oE 'GPU KV cache size: [0-9,]+ tokens' | tail -n1 || true)"
  printf '  Engine: tensor_parallel=%s pipeline_parallel=%s world_size=%s\n' "${tp:-?}" "${pp:-?}" "${ws:-?}"
  [ -n "$kv" ] && printf '  Per-node %s\n' "$kv"
  product=$(( ${tp:-0} * ${pp:-0} ))
  if [ "${ws:-0}" = "2" ] || [ "$product" = 2 ]; then
    check_pass "Distributed GPUs: 2" >/dev/null; echo "  Distributed GPUs: 2"
  elif [ "$product" = 0 ] && [ -z "${ws:-}" ]; then
    # Neither argv nor the log window could tell us. Unknown is not failure:
    # the worker's own health and the NCCL transport are checked separately.
    echo "  Distributed GPUs: unknown (engine banner has rolled out of the log window)"
    check_warn "could not determine the parallel layout" >/dev/null
  else
    echo "  Distributed GPUs: ${ws:-$product}"
    [ "$CLUSTER_MODE" = dual ] && check_fail "engine is not 2-way distributed" >/dev/null
  fi
  m="$(curl -fsS -m 5 "$(api_url)/metrics" 2>/dev/null | grep -E '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc)\{' | sed -E 's/\{[^}]*\}//' | tr '\n' ' ' || true)"
  [ -n "$m" ] && echo "  Metrics: $m"
  echo
  echo "NCCL"
  using="$(printf '%s\n' "$logs" | grep -E 'NET/(IB|Socket) : Using' | tail -n1 | sed -E 's/^.*NCCL INFO //' || true)"
  ib="$(printf '%s\n' "$logs" | grep -cE 'via NET/IB' || true)"; sock="$(printf '%s\n' "$logs" | grep -cE 'via NET/Socket' || true)"
  if [ "$ib" -gt 0 ] && [ "$sock" -eq 0 ]; then
    hcas="$(printf '%s' "$using" | grep -oE '\[[0-9]+\][A-Za-z0-9_]+:[0-9]+/RoCE' | wc -l)"
    echo "  Transport: RDMA/RoCE (${hcas} HCA(s), ${ib} channel endpoints)"; echo "  ${using:-}"; check_pass "NCCL transport RDMA/RoCE" >/dev/null
  elif [ "$sock" -gt 0 ]; then
    echo "  Transport: TCP SOCKET FALLBACK (${sock} channel endpoints) -- RDMA not in use"; echo "  ${using:-}"; check_warn "NCCL fell back to TCP sockets" >/dev/null
  else
    echo "  Transport: no NCCL init lines in the current log window (single node, or logs rotated)"
    [ "$CLUSTER_MODE" = dual ] && check_warn "NCCL transport unknown" >/dev/null
  fi
fi

if [ "$PROBE" = 1 ] && [ -n "$models_json" ]; then
  echo
  echo "Probe (streaming chat completion + GPU sampling on both nodes)"
  mid="$(printf '%s' "$models_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')"
  s1="$(mktemp)"; s2="$(mktemp)"
  nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500 > "$s1" 2>/dev/null & P1=$!
  P2=""; if [ "$CLUSTER_MODE" = dual ]; then ssh_worker "timeout 120 nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 500" > "$s2" 2>/dev/null & P2=$!; fi
  sleep 1
  python3 - "$(api_url)" "$mid" <<'PY'
import json, sys, time, urllib.request
url, mid = sys.argv[1] + "/v1/chat/completions", sys.argv[2]
body = {"model": mid, "messages": [{"role": "user", "content": "Write 120 words about distributed inference on two machines."}],
        "max_tokens": 160, "stream": True, "temperature": 0, "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False}}
req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
t0 = time.time(); first = None; usage = None; text = ""
with urllib.request.urlopen(req, timeout=300) as r:
    for line in r:
        line = line.decode().strip()
        if not line.startswith("data:"): continue
        p = line[5:].strip()
        if p == "[DONE]": break
        d = json.loads(p)
        if d.get("usage"): usage = d["usage"]
        for c in d.get("choices", []):
            delta = c.get("delta", {}).get("content")
            if delta:
                if first is None: first = time.time()
                text += delta
t1 = time.time()
n = (usage or {}).get("completion_tokens") or len(text.split())
print("  TTFT: %.3f s   completion_tokens: %s   decode: %.1f tok/s   total: %.2f s" % ((first or t1) - t0, n, n / max(t1 - (first or t0), 1e-6), t1 - t0))
print("  Reply: %r" % text[:90])
PY
  sleep 1; kill $P1 2>/dev/null || true; [ -n "$P2" ] && { kill $P2 2>/dev/null || true; }
  wait 2>/dev/null || true
  summ() { awk 'NF{n++; if($1+0>m)m=$1+0; s+=$1} END{if(n) printf "max %d%% avg %.0f%% (%d samples)", m, s/n, n; else printf "no samples"}' "$1"; }
  echo "  Node 1 GB10 during probe: $(summ "$s1")"
  m1="$(awk 'NF{if($1+0>m)m=$1+0} END{print m+0}' "$s1")"
  if [ "$CLUSTER_MODE" = dual ]; then
    echo "  Node 2 GB10 during probe: $(summ "$s2")"
    m2="$(awk 'NF{if($1+0>m)m=$1+0} END{print m+0}' "$s2")"
    if [ "${m1:-0}" -gt 0 ] && [ "${m2:-0}" -gt 0 ]; then check_pass "both GPUs active during inference" >/dev/null; echo "  Both GPUs participating: YES"; else check_fail "GPU activity not observed on both nodes (node1 max ${m1}%, node2 max ${m2}%)" >/dev/null; echo "  Both GPUs participating: NO"; fi
  fi
  rm -f "$s1" "$s2"
fi

echo "========================================"
check_summary
