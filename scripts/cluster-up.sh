#!/usr/bin/env bash
# Compatibility alias. Since 2026-08-25 `./techsara up` is the one command on
# every machine: on a DGX Spark it auto-detects a second Spark on the direct
# RoCE links (CLUSTER_MODE=auto), prepares the worker host, starts the worker
# and the head, and stages the rest of the stack; with one Spark, or on a Mac,
# it runs the normal single-node deployment. This wrapper adds two things: the
# engine recovery lock around the whole start (so the engine controller never
# restarts a pair the launcher is in the middle of starting -- contract §6.3),
# and the cluster status report at the end.
#   scripts/cluster-up.sh [techsara up options...]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
# shellcheck source=lib/engine-lock.sh
. "$CLUSTER_LIB_DIR/engine-lock.sh"

# The retired shell watchdog (replaced by the engine-controller service on
# 2026-09-12). A service removed from the compose file is invisible to `up`:
# Compose warns about the orphan and leaves it running beside the controller,
# each with its own idea of when to restart the head. The launcher removes it
# too (cli._retire_legacy_watchdog); this is the belt to that brace, for a
# launcher that failed before reaching that step. `--remove-orphans` is not
# used anywhere on purpose: the launcher's chain does not include the
# monitoring/tunnel overlays, so Prometheus and Grafana would count as orphans.
LEGACY_WATCHDOG="sf-local-ai-vllm-watchdog-1"
retire_legacy_watchdog() {
  local id
  id="$(docker ps -a --filter "name=^/${LEGACY_WATCHDOG}\$" --filter 'label=com.docker.compose.service=vllm-watchdog' --format '{{.ID}}' 2>/dev/null | head -n 1)"
  [ -n "$id" ] || return 0
  if docker rm -f "$id" >/dev/null 2>&1; then
    log_info "removed the legacy shell watchdog container $LEGACY_WATCHDOG (the engine controller replaces it)"
  else
    check_warn "could not remove $LEGACY_WATCHDOG; remove it by hand: docker rm -f $LEGACY_WATCHDOG"
  fi
}

engine_lock_acquire "${ENGINE_LOCK_WAIT:-1200}" "cluster-up.sh (techsara up)" || exit 2
( cd "$ROOT" && ./techsara up "$@" ); rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
retire_legacy_watchdog
engine_lock_release
cluster_load_settings
if [ "$CLUSTER_MODE" = dual ]; then "$CLUSTER_LIB_DIR/../cluster-status.sh"; else ( cd "$ROOT" && ./techsara status ); fi
