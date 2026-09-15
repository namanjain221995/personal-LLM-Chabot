#!/usr/bin/env bash
# Start back the services of this box that are stopped but should be running.
#
# WHY THIS EXISTS. `restart: unless-stopped` covers a crash and a reboot. It
# does NOT cover a service that something stopped on purpose: on 2026-09-15 a
# `techsara up` run from a SECOND checkout stopped searxng and left it down,
# and the auxiliary engines came back without their loopback ports, so the
# engine controller reported DEGRADED for twenty minutes. Docker was working
# as designed the whole time; nothing was watching.
#
# WHAT IT WILL NEVER DO.
#   * It never touches the main model container (MAIN_MODEL_CONTAINER): its
#     lifecycle belongs to the launcher and the engine controller, and a
#     restart there costs a cold start of a 35B model across two nodes.
#   * It never touches another Compose project: not the worker's
#     sf-local-ai-worker (tensor-parallel rank 1), not the OCR project, not
#     the e2e stack.
#   * It never CREATES a container. A service that was removed needs
#     `./techsara up`, which knows the image, the env files and the profile.
#   * It stops trying after MAX_STARTS_PER_HOUR: a container that dies again
#     each time is an incident for a person, not a loop to spin.
#
# TO EXCLUDE ONE SERVICE deliberately, write its container name on its own
# line in .runtime/reconcile-skip.
#
# Usage: scripts/service-reconcile.sh [once|install|uninstall|status]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${TECHSARA_COMPOSE_PROJECT:-sf-local-ai}"
MAIN_MODEL_CONTAINER="${MAIN_MODEL_CONTAINER:-${PROJECT}-vllm-1}"
SKIP_FILE="$ROOT/.runtime/reconcile-skip"
STATE_FILE="$ROOT/.runtime/reconcile-state"
LOG_FILE="$ROOT/.runtime/logs/reconcile.log"
MAX_STARTS_PER_HOUR="${RECONCILE_MAX_STARTS_PER_HOUR:-3}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
INTERVAL="${RECONCILE_INTERVAL:-2min}"

log() {
  mkdir -p "$(dirname "$LOG_FILE")"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE"
}

skipped() { # skipped NAME
  [ -f "$SKIP_FILE" ] || return 1
  grep -qxF "$1" "$SKIP_FILE"
}

# starts_last_hour NAME -> how many times THIS script started it in 3600 s.
starts_last_hour() {
  local name="$1" now cutoff
  now="$(date +%s)"; cutoff=$((now - 3600))
  [ -f "$STATE_FILE" ] || { printf '0'; return; }
  awk -v n="$name" -v c="$cutoff" '$1 == n && $2 >= c' "$STATE_FILE" | wc -l | tr -d ' '
}

record_start() { # record_start NAME
  mkdir -p "$(dirname "$STATE_FILE")"
  printf '%s %s\n' "$1" "$(date +%s)" >> "$STATE_FILE"
  # Keep the file small: only the last day matters to the rate limit.
  local cutoff; cutoff=$(( $(date +%s) - 86400 ))
  awk -v c="$cutoff" '$2 >= c' "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
  mv -f "$STATE_FILE.tmp" "$STATE_FILE" 2>/dev/null || true
}

reconcile_once() {
  local started=0 examined=0 name status policy
  while IFS=$'\t' read -r name status policy; do
    [ -n "$name" ] || continue
    examined=$((examined + 1))
    [ "$status" = "exited" ] || continue
    # unless-stopped and always are the policies that say "this should run".
    case "$policy" in unless-stopped|always) ;; *) continue ;; esac
    if [ "$name" = "$MAIN_MODEL_CONTAINER" ]; then
      log "skip $name (main model: launcher and engine controller own it)"
      continue
    fi
    if skipped "$name"; then
      log "skip $name (listed in .runtime/reconcile-skip)"
      continue
    fi
    local tries; tries="$(starts_last_hour "$name")"
    if [ "$tries" -ge "$MAX_STARTS_PER_HOUR" ]; then
      log "give up on $name ($tries starts in the last hour; a person should look)"
      continue
    fi
    if docker start "$name" >/dev/null 2>&1; then
      record_start "$name"
      started=$((started + 1))
      log "started $name (attempt $((tries + 1)) this hour)"
    else
      log "could not start $name"
    fi
  done < <(docker ps -a \
             --filter "label=com.docker.compose.project=$PROJECT" \
             --format '{{.Names}}\t{{.State}}' \
           | while IFS=$'\t' read -r n s; do
               printf '%s\t%s\t%s\n' "$n" "$s" \
                 "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$n" 2>/dev/null)"
             done)
  [ "$started" -eq 0 ] || log "reconcile: started $started of $examined containers"
  return 0
}

install_timer() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/techsara-reconcile.service" <<UNIT
[Unit]
Description=TechSara: start back stopped services of the $PROJECT stack
After=docker.service

[Service]
Type=oneshot
ExecStart=$ROOT/scripts/service-reconcile.sh once
UNIT
  cat > "$UNIT_DIR/techsara-reconcile.timer" <<UNIT
[Unit]
Description=TechSara service reconcile every $INTERVAL

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
AccuracySec=15s
Unit=techsara-reconcile.service

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now techsara-reconcile.timer
  log "installed techsara-reconcile.timer (every $INTERVAL, user scope)"
  systemctl --user list-timers techsara-reconcile.timer --no-pager || true
}

uninstall_timer() {
  systemctl --user disable --now techsara-reconcile.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/techsara-reconcile.timer" "$UNIT_DIR/techsara-reconcile.service"
  systemctl --user daemon-reload
  log "removed techsara-reconcile.timer"
}

case "${1:-once}" in
  once)      reconcile_once ;;
  install)   install_timer ;;
  uninstall) uninstall_timer ;;
  status)
    systemctl --user status techsara-reconcile.timer --no-pager || true
    [ -f "$LOG_FILE" ] && tail -20 "$LOG_FILE"
    ;;
  *) echo "usage: $0 [once|install|uninstall|status]" >&2; exit 2 ;;
esac
