#!/usr/bin/env bash
# Two-node NCCL all-reduce test inside the pinned vLLM image (the same NCCL
# build vLLM uses), one process per node, GPU 0 on each.
#   scripts/cluster-test.sh [--socket] [--hca LIST] [--port N]
#   --socket    force TCP sockets (NCCL_IB_DISABLE=1) for comparison
#   --hca LIST  restrict NCCL to these HCAs (default: the generated multi-rail list)
# Prints bus bandwidth per message size, latency, the transport NCCL selected,
# and PASS/FAIL (data is validated: all-reduce of ones must equal world size).
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
SOCKET=0; PORT=29600; HCA=""
while [ $# -gt 0 ]; do case "$1" in --socket) SOCKET=1 ;; --hca) HCA="$2"; shift ;; --port) PORT="$2"; shift ;; -h|--help) sed -n '2,9p' "$0"; exit 0 ;; *) die "unknown option $1" ;; esac; shift; done
cluster_load_settings; require_dual_mode
mkdir -p "$LOG_DIR"
image="$(head_vllm_image 2>/dev/null || env_get "$WORKER_ENV_LOCAL" CLUSTER_VLLM_IMAGE || true)"
[ -n "$image" ] || die "cannot determine the vLLM image"
ifn="$(detect_ifname_for_ip "$CLUSTER_HEAD_IP" || true)"; [ -n "$ifn" ] || die "no local interface carries $CLUSTER_HEAD_IP"
[ -n "$HCA" ] || HCA="${CLUSTER_NCCL_IB_HCA:-$(detect_hca_for_ifname "$ifn" 2>/dev/null || true)}"
remote_facts="$(ssh_worker "$(detect_snippet); ifn=\$(detect_ifname_for_ip '$CLUSTER_WORKER_IP'); echo \$ifn; h1=\$(detect_hca_for_ifname \"\$ifn\" 2>/dev/null); h2=''; [ -n '${CLUSTER_WORKER_IP_2:-}' ] && h2=\$(detect_hca_for_ifname \"\$(detect_ifname_for_ip '${CLUSTER_WORKER_IP_2:-}')\" 2>/dev/null); echo \$h1\${h2:+,\$h2}")"
w_ifn="$(printf '%s\n' "$remote_facts" | sed -n 1p)"; w_hca="$(env_get "$WORKER_ENV_LOCAL" CLUSTER_WORKER_NCCL_IB_HCA 2>/dev/null || printf '%s\n' "$remote_facts" | sed -n 2p)"
[ -n "$w_ifn" ] || die "no interface on the worker carries $CLUSTER_WORKER_IP"
ssh_worker "docker image inspect '$image' >/dev/null 2>&1" || die "image not on the worker; run scripts/cluster-sync.sh --image-only"

envs=(-e WORLD_SIZE=2 -e MASTER_ADDR="$CLUSTER_HEAD_IP" -e MASTER_PORT="$PORT" -e NCCL_DEBUG=INFO -e NCCL_DEBUG_SUBSYS=INIT,NET -e NCCL_IB_DISABLE="$SOCKET")
[ "$SOCKET" = 1 ] && log_info "transport: TCP sockets forced" || log_info "transport: RDMA, NCCL_IB_HCA=head:${HCA:-auto} worker:${w_hca:-auto}"
common=(--rm --network host --gpus all --device /dev/infiniband --ulimit memlock=-1:-1 --ipc host --entrypoint python3)
ssh_worker "mkdir -p $WORKER_REMOTE_DIR" && scp_to_worker "$NCCL_BENCH_SCRIPT" ".techsara-cluster"
cleanup() { docker rm -f nccl-bench >/dev/null 2>&1 || true; ssh_worker "docker rm -f nccl-bench >/dev/null 2>&1 || true" || true; }
trap cleanup EXIT
cleanup
renvs="-e RANK=1 -e WORLD_SIZE=2 -e MASTER_ADDR=$CLUSTER_HEAD_IP -e MASTER_PORT=$PORT -e NCCL_DEBUG=INFO -e NCCL_DEBUG_SUBSYS=INIT,NET -e NCCL_IB_DISABLE=$SOCKET -e NCCL_SOCKET_IFNAME=$w_ifn ${w_hca:+-e NCCL_IB_HCA=$w_hca}"
ssh_worker "nohup docker run --name nccl-bench --rm --network host --gpus all --device /dev/infiniband --ulimit memlock=-1:-1 --ipc host $renvs -v $WORKER_REMOTE_DIR/nccl_allreduce_bench.py:/bench.py:ro --entrypoint python3 '$image' /bench.py > $WORKER_REMOTE_DIR/nccl-rank1.log 2>&1 </dev/null &"
sleep 3
log0="$LOG_DIR/nccl-rank0.log"
timeout 600 docker run --name nccl-bench "${common[@]}" "${envs[@]}" -e RANK=0 -e NCCL_SOCKET_IFNAME="$ifn" ${HCA:+-e NCCL_IB_HCA="$HCA"} -v "$NCCL_BENCH_SCRIPT:/bench.py:ro" "$image" /bench.py > "$log0" 2>&1 || { tail -n 30 "$log0"; die "NCCL test failed on the head (log: $log0)"; }
using="$(grep -E 'NET/(IB|Socket) : Using' "$log0" | sed -E 's/^.*NCCL INFO //' | tail -n1)"
ib="$(grep -cE 'via NET/IB' "$log0" || true)"; sock="$(grep -cE 'via NET/Socket' "$log0" || true)"
echo; echo "NCCL: $using"
printf '%s\n' "$(grep -h NCCL_BENCH_RESULT "$log0" | sed 's/.*NCCL_BENCH_RESULT //')" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("NCCL %s  torch %s  %s <-> world_size %d" % (".".join(map(str, d["nccl"])), d["torch"], d["gpu"], d["world_size"]))
print("all_reduce 4 B latency: %.1f us   64 MiB send/recv: %.2f Gb/s" % (d["allreduce_4B_latency_us"], d["p2p_64MB_Gbps"]))
print("%10s %10s %12s %12s" % ("size", "time", "algbw", "busbw"))
for r in d["results"]:
    print("%7.3f MB %8.3f ms %8.3f GB/s %8.2f Gb/s" % (r["size_MB"], r["time_ms"], r["algbw_GBs"], r["busbw_Gbps"]))
if d["errors"]:
    print("ERRORS:", d["errors"]); sys.exit(1)
' && data_ok=1 || data_ok=0
[ "$data_ok" = 1 ] && check_pass "NCCL all-reduce data validated across both GB10s" || check_fail "NCCL all-reduce data mismatch"
if [ "$SOCKET" = 0 ]; then
  [ "$ib" -gt 0 ] && [ "$sock" -eq 0 ] && check_pass "transport RDMA/RoCE ($ib channel endpoints over $(printf '%s' "$using" | grep -oE '/RoCE' | wc -l) HCA(s))" || check_fail "expected RDMA but NCCL used sockets ($sock endpoints)"
else
  [ "$sock" -gt 0 ] && check_pass "transport TCP sockets (as requested)" || check_warn "socket transport not observed"
fi
log_dim "logs: $log0 and worker:$WORKER_REMOTE_DIR/nccl-rank1.log"
check_summary
