#!/usr/bin/env bash
# Grafana + Prometheus observability for the DGX Spark fleet.
#
#   scripts/monitoring.sh up       start Grafana/Prometheus here and the exporters
#                                  on every known Spark, and turn discovery on
#   scripts/monitoring.sh down     stop and remove (keeps the metric history)
#   scripts/monitoring.sh stop     stop without removing
#   scripts/monitoring.sh status   containers + Prometheus target health
#   scripts/monitoring.sh logs     follow logs (add a service name to filter)
#   scripts/monitoring.sh restart  restart the head stack
#   scripts/monitoring.sh verify   prove every Spark is actually reporting
#   scripts/monitoring.sh url      print the Grafana URL and how to log in
#   scripts/monitoring.sh fleet [list|discover|add TARGET|forget NODE|enroll NODE]
#                                  which Sparks exist (see scripts/lib/fleet.py)
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

fleet() { python3 "$ROOT/scripts/lib/fleet.py" "$@"; }

# The Sparks this head knows about, apart from itself: `name address ssh` lines.
fleet_nodes() {
  fleet list | awk 'NR>1 && $1 ~ /^spark-/ && $1 != "spark-1" {print $1}'
}

# cAdvisor is gone from compose.monitoring.yaml, so Compose can no longer name
# it, and `up --remove-orphans` is not an option here: it would also take
# cloudflared, which lives in a file outside this chain. The container is
# removed by its own name, and only that one.
retire_cadvisor() {
  docker rm -f "${MONITORING_PROJECT}-cadvisor-1" >/dev/null 2>&1 || true
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

# `action`, not `cmd`: cluster-common's head_compose declares a local ARRAY
# of that name, and one name for two shapes trips shellcheck (SC2178/SC2128).
action="${1:-status}"; shift || true
case "$action" in
  up)
    ensure_grafana_password
    log_info "starting Grafana + Prometheus on this node"
    write_ocr_scrape_target
    # Before Prometheus starts, so its target files exist, and before spark-probe
    # starts, because a missing bind source would be created as root and then
    # discovery, which runs as this user, could not write to it.
    fleet render
    # Named services only: a bare `up -d` would reconcile every service in the
    # project, restarting the LLM stack for a monitoring change.
    head_monitoring_compose up -d --no-deps \
      prometheus grafana node-exporter dgx-gpu-exporter spark-probe spark-local blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    retire_cadvisor
    # Every other Spark, each on its own: one being away must not stop the rest.
    log_info "starting exporters on the other Sparks"
    fleet sync || log_info "some Sparks were not updated; see above. ./scripts/monitoring.sh fleet list"
    fleet install-timer || log_info "discovery timer not installed; ./scripts/monitoring.sh fleet discover runs one pass by hand"
    "$0" url
    ;;
  down)
    fleet uninstall-timer || true
    head_monitoring_compose stop \
      prometheus grafana node-exporter dgx-gpu-exporter spark-probe spark-local blackbox-exporter \
      postgres-exporter data-stores-exporter
    head_monitoring_compose rm -f \
      prometheus grafana node-exporter dgx-gpu-exporter spark-probe spark-local blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    if is_dual_mode; then
      worker_monitoring_compose "$(worker_mgmt_ip)" down || true
    fi
    log_info "metric history is kept in the sf-local-ai_prometheus volume"
    ;;
  stop)
    head_monitoring_compose stop \
      prometheus grafana node-exporter dgx-gpu-exporter spark-probe spark-local blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    # Best effort on the worker: a stop must never fail because Node 2 is away.
    if is_dual_mode; then worker_monitoring_compose "$(worker_mgmt_ip)" stop || true; fi
    ;;
  restart)
    head_monitoring_compose restart \
      prometheus grafana node-exporter dgx-gpu-exporter spark-probe spark-local blackbox-exporter \
      postgres-exporter data-stores-exporter "$@"
    ;;
  fleet)
    fleet "${@:-list}"
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
    echo "== every Spark reporting? =="
    fleet list | sed 's/^/  /'
    echo
    for node in spark-1 $(fleet_nodes); do
      printf '  %-8s GPU %5s C  %6s W  %5s %% util   mem %5s %%  RoCE tx %10s B/s   containers %4s   databases %3s\n' \
        "$node" \
        "$(prom_scalar "dgx_gpu_temperature_celsius{node=\"$node\"}")" \
        "$(prom_scalar "dgx_gpu_power_watts{node=\"$node\"}")" \
        "$(prom_scalar "dgx_gpu_utilization_percent{node=\"$node\"}")" \
        "$(prom_scalar "node:memory_used_percent:current{node=\"$node\"}")" \
        "$(prom_scalar "node:roce_transmit_bytes_per_second:sum{node=\"$node\"}")" \
        "$(prom_scalar "spark_containers{node=\"$node\",status=\"running\"}")" \
        "$(prom_scalar "spark_db_instances{node=\"$node\"}")"
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
    # The engine controller (contract §6/§7.1) is scraped as job
    # `engine-controller`; the derived top-level state is the recording rule
    # cluster:vllm_service_state:code (contract §7.3). "-" here is
    # MONITORING_UNKNOWN by definition (no sample), never DOWN -- the
    # dashboard renders the same absence as noValue.
    echo "== engine controller =="
    ctl_up="$(prom_scalar 'up{job="engine-controller"}')"
    if [ "$ctl_up" = "-" ]; then
      echo "  scrape target: not configured or never scraped (prometheus.yml job engine-controller; monitoring workstream)"
    else
      printf '  scrape target up       %s\n' "$ctl_up"
    fi
    state_code="$(prom_scalar 'techsara_vllm_state_code')"
    state_name="$(prom_query 'techsara_vllm_state{}==1' | python3 -c '
import json, sys
r = json.load(sys.stdin).get("data", {}).get("result", [])
print(r[0]["metric"].get("state", "?") if r else "MONITORING_UNKNOWN (no sample)")
')"
    printf '  controller state       %s (%s)\n' "$state_name" "$state_code"
    printf '  derived service state  %s   (cluster:vllm_service_state:code; 7=QUEUEING 8=DOWN)\n' "$(prom_scalar 'cluster:vllm_service_state:code')"
    printf '  canary success         %s   ttft %s s   last success %s s ago\n' \
      "$(prom_scalar 'techsara_vllm_synthetic_success')" \
      "$(prom_scalar 'techsara_vllm_synthetic_ttft_seconds')" \
      "$(prom_scalar 'cluster:vllm_last_success_age_seconds')"
    printf '  worker rank alive      %s   both ranks ok %s   restart budget left %s\n' \
      "$(prom_scalar 'techsara_vllm_worker_rank_alive')" \
      "$(prom_scalar 'techsara_vllm_both_ranks_ok')" \
      "$(prom_scalar 'techsara_vllm_restart_budget_remaining')"
    # v2, strict one-model mode: nothing answers for the main model; accepted
    # generations wait in a durable queue and resume on READY (contract §8.3).
    printf '  orchestrator queue     %s generation(s) queued for the primary   breaker(main) %s (0 closed, 1 open, 2 half-open)\n' \
      "$(prom_scalar 'llm_queued_generations')" \
      "$(prom_scalar 'llm_breaker_state{engine="main"}')"
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
    die "usage: $0 up|down|stop|restart|status|logs|verify|url|fleet"
    ;;
esac
