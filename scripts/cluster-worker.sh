#!/usr/bin/env bash
# Control the vLLM worker (node-rank 1) on Node 2 from Node 1.
# Usage: scripts/cluster-worker.sh start|stop|down|restart|status|logs [args]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
cluster_load_settings
require_dual_mode
cmd="${1:-status}"; shift || true
case "$cmd" in
  start)
    ssh_worker "test -f $WORKER_REMOTE_DIR/worker.env -a -f $WORKER_REMOTE_DIR/compose.cluster-worker.yaml" \
      || die "worker files missing on $CLUSTER_WORKER_SSH; run scripts/cluster-sync.sh first"
    worker_compose up -d "$@"
    ;;
  stop)     worker_compose stop --timeout 120 "$@" ;;
  down)     worker_compose down --timeout 120 "$@" ;;   # keeps the worker's cache volume
  restart)  worker_compose restart "$@" ;;
  status|ps) worker_compose ps "$@" ;;
  logs)     worker_compose logs --no-color "$@" ;;
  *) die "usage: $0 start|stop|down|restart|status|logs" ;;
esac
