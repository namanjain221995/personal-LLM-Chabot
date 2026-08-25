#!/usr/bin/env bash
# Compatibility alias. Since 2026-08-25 `./techsara up` is the one command on
# every machine: on a DGX Spark it auto-detects a second Spark on the direct
# RoCE links (CLUSTER_MODE=auto), prepares the worker host, starts the worker
# and the head, and stages the rest of the stack; with one Spark, or on a Mac,
# it runs the normal single-node deployment. This wrapper only adds the
# cluster status report at the end.
#   scripts/cluster-up.sh [techsara up options...]
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
( cd "$ROOT" && ./techsara up "$@" ); rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
cluster_load_settings
if [ "$CLUSTER_MODE" = dual ]; then "$CLUSTER_LIB_DIR/../cluster-status.sh"; else ( cd "$ROOT" && ./techsara status ); fi
