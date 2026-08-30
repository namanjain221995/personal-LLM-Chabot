#!/usr/bin/env bash
# Grafana + Prometheus observability for the two-node DGX Spark cluster.
#
#   scripts/monitoring.sh up       start Grafana/Prometheus here and the two
#                                  exporters on Spark 2
#   scripts/monitoring.sh down     stop and remove (keeps the metric history)
#   scripts/monitoring.sh stop     stop without removing
#   scripts/monitoring.sh status   containers + Prometheus target health
#   scripts/monitoring.sh logs     follow logs (add a service name to filter)
#   scripts/monitoring.sh restart  restart the head stack
#   scripts/monitoring.sh verify   prove both Sparks are actually reporting
#   scripts/monitoring.sh url      print the Grafana URL and how to log in
#
# Nothing here touches the LLM. The monitoring profile is not a dependency of
# any inference service, so `down` leaves vLLM, the orchestrator and the
# frontend running.
#
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

MONITORING_PROJECT="${MONITORING_PROJECT:-sf-local-ai}"
WORKER_MONITORING_PROJECT="${WORKER_MONITORING_PROJECT:-sf-local-ai-monitoring}"
GRAFANA_PORT="${GRAFANA_PORT:-3300}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
SECRETS_FILE="$SECRETS_ENV"

# cluster-common resolves CLUSTER_MODE from the GENERATED env (what is
# actually deployed) rather than .env's intent, so "auto" has already become
# "dual" or "single" by here.
is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }

# --------------------------------------------------------------------------
# Grafana password: generated once into .runtime/secrets.env (gitignored,
# 0600), never printed, never committed. This is why the compose file uses
# ${GRAFANA_ADMIN_PASSWORD:?...} - a missing password must stop the stack, not
# silently produce an admin/admin Grafana on a LAN with no firewall.
# --------------------------------------------------------------------------
ensure_grafana_password() {
  if grep -q '^GRAFANA_ADMIN_PASSWORD=' "$SECRETS_FILE" 2>/dev/null; then
    return 0
  fi
  local generated
  generated="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  mkdir -p "$(dirname "$SECRETS_FILE")"
  touch "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE"
  printf '\n# Grafana admin password (generated %s by scripts/monitoring.sh).\nGRAFANA_ADMIN_PASSWORD=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$generated" >>"$SECRETS_FILE"
  log_info "generated a Grafana admin password into .runtime/secrets.env"
}

# THE FULL OVERLAY CHAIN IS MANDATORY. `docker compose up` with a SUBSET of
# the project's files does not just ignore the missing ones - it recreates any
# running service using the definition it can see. Invoking this with only
# compose.yaml + the monitoring overlay silently rebuilt the orchestrator from
# the base file and downgraded it from the :cuda image to :cpu. The monitoring
# profile must therefore be layered on top of exactly the same chain the
# launcher uses, so every other service renders identically and is left alone.
head_monitoring_compose() {
  local files=(-f compose.yaml -f compose/compose.dgx-spark.yaml)
  case "${TECHSARA_PUBLISH_MODEL_PORTS:-${PUBLISH_MODEL_PORTS:-false}}" in
    true|1|yes) files+=(-f compose/compose.published-dgx-spark.yaml) ;;
  esac
  is_dual_mode && files+=(-f compose/compose.cluster-dgx-spark.yaml)
  files+=(-f compose/compose.monitoring.yaml)
  ( cd "$ROOT" && docker compose \
      --project-name "$MONITORING_PROJECT" \
      --env-file .env \
      --env-file .runtime/secrets.env \
      --env-file .runtime/generated.env \
      "${files[@]}" \
      --profile monitoring "$@" )
}

# The worker's exporters bind to its MANAGEMENT address, never to the RoCE
# addresses - monitoring must not share the fabric it measures.
worker_mgmt_ip() {
  if [ -n "${MONITORING_WORKER_BIND:-}" ]; then
    printf '%s' "$MONITORING_WORKER_BIND"
    return
  fi
  ssh_worker "ip -4 -br addr show enP7s7 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" \
    | tr -d '[:space:]'
}

worker_monitoring_compose() {
  local bind="$1"; shift
  ssh_worker "cd $WORKER_REMOTE_DIR && MONITORING_WORKER_BIND=$bind docker compose \
    --project-name $WORKER_MONITORING_PROJECT \
    -f compose.monitoring-worker.yaml $*"
}

sync_worker_monitoring() {
  local bind="$1"
  log_info "syncing monitoring files to $CLUSTER_WORKER_SSH"
  ssh_worker "mkdir -p $WORKER_REMOTE_DIR"
  # scp, not rsync: two small files, and rsync is not guaranteed on the worker.
  local remote_dir
  remote_dir="$(ssh_worker "echo $WORKER_REMOTE_DIR")"
  scp -q -o BatchMode=yes ${CLUSTER_WORKER_SSH_OPTS:-} \
    "$ROOT/compose/compose.monitoring-worker.yaml" \
    "$ROOT/monitoring/exporters/dgx-gpu/dgx_gpu_exporter.py" \
    "$CLUSTER_WORKER_SSH:$remote_dir/" \
    || die "could not copy monitoring files to the worker"
  check_pass "worker files synced (bind $bind)"
}

prom_query() { # prom_query <promql> -> raw JSON
  curl -fsS --max-time 10 --get "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query" \
    --data-urlencode "query=$1" 2>/dev/null || printf '{}'
}

prom_scalar() { # prom_scalar <promql> -> first sample value, or "-"
  prom_query "$1" | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)["data"]["result"]
except Exception:
    r = []
print("%.2f" % float(r[0]["value"][1]) if r else "-")
'
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  up)
    ensure_grafana_password
    log_info "starting Grafana + Prometheus on this node"
    # Named services only: a bare `up -d` would reconcile every service in the
    # project, restarting the LLM stack for a monitoring change.
    head_monitoring_compose up -d --no-deps \
      prometheus grafana node-exporter dgx-gpu-exporter cadvisor blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    if is_dual_mode; then
      bind="$(worker_mgmt_ip)"
      [ -n "$bind" ] || die "could not determine the worker's management IP (set MONITORING_WORKER_BIND)"
      sync_worker_monitoring "$bind"
      log_info "starting exporters on the worker ($bind)"
      worker_monitoring_compose "$bind" up -d
    else
      log_info "cluster is single-node; skipping the worker exporters"
    fi
    "$0" url
    ;;
  down)
    head_monitoring_compose stop \
      prometheus grafana node-exporter dgx-gpu-exporter cadvisor blackbox-exporter \
      postgres-exporter data-stores-exporter
    head_monitoring_compose rm -f \
      prometheus grafana node-exporter dgx-gpu-exporter cadvisor blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    if is_dual_mode; then
      worker_monitoring_compose "$(worker_mgmt_ip)" down || true
    fi
    log_info "metric history is kept in the sf-local-ai_prometheus volume"
    ;;
  stop)
    head_monitoring_compose stop \
      prometheus grafana node-exporter dgx-gpu-exporter cadvisor blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    is_dual_mode && worker_monitoring_compose "$(worker_mgmt_ip)" stop || true
    ;;
  restart)
    head_monitoring_compose restart \
      prometheus grafana node-exporter dgx-gpu-exporter cadvisor blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    ;;
  logs)
    head_monitoring_compose logs --no-color -f "$@"
    ;;
  status|ps)
    echo "== head (this node) =="
    head_monitoring_compose ps
    if is_dual_mode; then
      echo
      echo "== worker ($CLUSTER_WORKER_SSH) =="
      worker_monitoring_compose "$(worker_mgmt_ip)" ps || echo "  (worker exporters not running)"
    fi
    echo
    echo "== Prometheus targets =="
    curl -fsS --max-time 10 "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/targets?state=any" 2>/dev/null \
      | python3 -c '
import json, sys
try:
    ts = json.load(sys.stdin)["data"]["activeTargets"]
except Exception:
    print("  Prometheus is not answering"); sys.exit()
for t in sorted(ts, key=lambda x: (x["labels"].get("job", ""), x["labels"].get("instance", ""))):
    lab = t["labels"]
    who = lab.get("node") or lab.get("service") or lab.get("instance", "")
    mark = "UP  " if t["health"] == "up" else "DOWN"
    print("  [%s] %-14s %-14s %s" % (mark, lab.get("job", ""), who, t["scrapeUrl"]))
    if t["health"] != "up" and t.get("lastError"):
        print("           error: %s" % t["lastError"][:110])
' || true
    ;;
  verify)
    # Proof, not vibes: query Prometheus for one value per Spark.
    echo "== both Sparks reporting? =="
    for node in spark-1 spark-2; do
      printf '  %-8s GPU %5s C  %6s W  %5s %% util   mem %5s %%  RoCE tx %10s B/s\n' \
        "$node" \
        "$(prom_scalar "dgx_gpu_temperature_celsius{node=\"$node\"}")" \
        "$(prom_scalar "dgx_gpu_power_watts{node=\"$node\"}")" \
        "$(prom_scalar "dgx_gpu_utilization_percent{node=\"$node\"}")" \
        "$(prom_scalar "node:memory_used_percent:current{node=\"$node\"}")" \
        "$(prom_scalar "node:roce_transmit_bytes_per_second:sum{node=\"$node\"}")"
    done
    echo
    echo "== cluster aggregates =="
    printf '  combined GPU power   %s W\n'   "$(prom_scalar 'cluster:gpu_power_watts:sum')"
    printf '  max GPU temperature  %s C\n'   "$(prom_scalar 'cluster:gpu_temperature_celsius:max')"
    printf '  running requests     %s\n'     "$(prom_scalar 'cluster:vllm_requests_running:current')"
    printf '  waiting requests     %s\n'     "$(prom_scalar 'cluster:vllm_requests_waiting:current')"
    printf '  generation tokens/s  %s\n'     "$(prom_scalar 'cluster:vllm_generation_tokens_per_second:rate1m')"
    printf '  KV cache used        %s %%\n'  "$(prom_scalar 'cluster:vllm_kv_cache_usage_percent:current')"
    echo
    echo "== targets down =="
    prom_query 'up == 0' | python3 -c '
import json, sys
r = json.load(sys.stdin).get("data", {}).get("result", [])
if not r:
    print("  none - every target is up")
for s in r:
    m = s["metric"]
    print("  %s %s %s" % (m.get("job", ""), m.get("node") or m.get("service", ""), m.get("instance", "")))
'
    ;;
  url)
    echo "Grafana:    http://127.0.0.1:${GRAFANA_PORT}"
    echo "Prometheus: http://127.0.0.1:${PROMETHEUS_PORT}"
    echo
    echo "Log in as '${GRAFANA_ADMIN_USER:-admin}'. The password was generated into"
    echo ".runtime/secrets.env (mode 0600, gitignored). To read it:"
    echo "    grep '^GRAFANA_ADMIN_PASSWORD=' .runtime/secrets.env | cut -d= -f2-"
    ;;
  *)
    die "usage: $0 up|down|stop|restart|status|logs|verify|url"
    ;;
esac
