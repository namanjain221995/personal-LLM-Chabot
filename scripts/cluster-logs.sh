#!/usr/bin/env bash
# Cluster logs.  Usage: scripts/cluster-logs.sh head|worker|nccl|all [-f] [--tail N]
#   head    vLLM head (node-rank 0, API server + engine) on Node 1
#   worker  vLLM worker (node-rank 1) on Node 2
#   nccl    NCCL transport/init lines from both nodes (proves RDMA vs TCP)
#   all     tail of both
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
cluster_load_settings
what="${1:-all}"; shift || true
args=("$@"); [ ${#args[@]} -eq 0 ] && args=(--tail 200)
case "$what" in
  head)   head_compose logs --no-color "${args[@]}" vllm ;;
  worker) require_dual_mode; worker_compose logs --no-color "${args[*]}" ;;
  nccl)
    echo "### head (Node 1)"; head_compose logs --no-color --tail 5000 vllm 2>&1 | grep -E "NCCL (INFO|WARN)|Using network|via NET/|NET/(IB|Socket) : Using|DP group leader|world_size" | tail -n 60
    if [ "$CLUSTER_MODE" = "dual" ]; then echo; echo "### worker (Node 2)"; worker_compose logs --no-color --tail 5000 2>&1 | grep -E "NCCL (INFO|WARN)|Using network|via NET/|NET/(IB|Socket) : Using|headless|world_size" | tail -n 60; fi ;;
  all)
    echo "### head (Node 1)"; head_compose logs --no-color "${args[@]}" vllm
    if [ "$CLUSTER_MODE" = "dual" ]; then echo; echo "### worker (Node 2)"; worker_compose logs --no-color "${args[*]}"; fi ;;
  *) die "usage: $0 head|worker|nccl|all [-f] [--tail N]" ;;
esac
