#!/usr/bin/env bash
# Stop the two-node cluster safely: Node 1 stack via the launcher (no volumes
# removed), then the worker on Node 2 (its cache volume is kept). Since
# 2026-08-25 `./techsara down` already stops the worker itself in dual mode;
# this wrapper is kept for muscle memory and for --worker-only/--head-only.
# Usage: scripts/cluster-down.sh [--head-only|--worker-only]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
cluster_load_settings
mode="${1:-all}"
if [ "$mode" != "--worker-only" ]; then
  section "Node 1: ./techsara down"
  ( cd "$ROOT" && ./techsara down )
fi
if [ "$mode" != "--head-only" ] && [ "$CLUSTER_MODE" = "dual" ]; then
  section "Node 2: worker down"
  if ssh_worker "test -f $WORKER_REMOTE_DIR/worker.env" 2>/dev/null; then
    worker_compose down --timeout 120 || check_warn "worker compose down failed (is the worker host up?)"
  else
    log_info "no worker deployment found on $CLUSTER_WORKER_SSH; nothing to stop"
  fi
fi
echo "done"
