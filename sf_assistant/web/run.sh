#!/usr/bin/env bash
# sf-assistant web page: ./run.sh up | logs | status | new-codes | restart-page
#               MCP server: ./run.sh up-mcp | mcp-logs | mcp-status | new-mcp-token | restart-mcp
#               ask service: ./run.sh up-ask | restart-ask | new-ask-token
#
#   up            build the image and start the container (docker compose -p sf-assistant up -d --build);
#                 creates ../.private (mode 700). It never creates or prints codes: sign-in answers
#                 "no access codes" until the owner runs ./run.sh new-codes himself, in his own terminal
#   logs          follow the container log
#   status        container state and the page's /api/status
#   new-codes     new access code + admin code, printed once (old ones stop working, signed-in sessions end).
#                 Run it only in your own terminal: anything that records the output (a script, an agent's
#                 transcript, a CI log) keeps the plain codes. It refuses to print into a pipe or a file (the
#                 old codes then stay as they were)
#   restart-page  restart the container (after a change to server.py / pipeline.py, or to src/, prompts/ or knowledge/objects/)
#   up-mcp        build the image and start only the MCP container (sf-assistant-mcp); the page's container is
#                 not recreated (it keeps the image it runs on until its own next up). Creates the MCP container's
#                 three files in ../.private first: mcp_log.jsonl, mcp_token.json (empty until new-mcp-token), mcp_audit_key
#   mcp-logs      follow the MCP container log
#   mcp-status    MCP container state and its /healthz (no token needed, tells nothing about the data)
#   new-mcp-token new MCP bearer token, printed once (only its SHA-256 hash is stored in ../.private/mcp_token.json;
#                 older tokens stop working at their next request). Same rule as new-codes: your own terminal only
#                 (it refuses to print into a pipe or a file)
#   restart-mcp   restart the MCP container (after a change to src/, prompts/ or knowledge/objects/, or when
#                 mcp-status shows tokens "stale": the token file was replaced instead of rewritten in place)
#   up-ask        start only the ask service (sf-assistant-ask, its own compose project, compose.ask.yaml), on the image
#                 the page built: it never builds it, because the page and the MCP server run on that image, and never
#                 creates the socket's volume sf-local-ai_ask-sock, which the platform's compose owns. It stops when
#                 either is missing. Creates in ../.private first its log folder ask_logs/ (mode 700; one file a day,
#                 kept SFA_ASK_LOG_DAYS days, default 90) and ask_token.json (empty until new-ask-token). It starts
#                 nothing while the old single-file log ../.private/ask_log.jsonl is there: split it first, service
#                 stopped (../docs/runbook.md, "The ask log"), nor while ask_mcp.py does not write through
#                 RotatingAskLog. Stop it with docker stop sf-assistant-ask
#   restart-ask   recreate only the ask service on the same image, after the same checks as up-ask (after a change to
#                 src/, prompts/ or knowledge/objects/: it reads them once, at its start)
#   new-ask-token new bearer token of the ask service, printed once (only its SHA-256 hash is stored in
#                 ../.private/ask_token.json; older tokens stop working at their next request). Same rule as
#                 new-mcp-token: your own terminal only (it refuses to print into a pipe or a file)
#
# Settings: SFA_BIND (default 127.0.0.1; from the environment or web/.env, see web/.env.example), SFA_PORT (default
# 8600), SFA_ALLOWED_HOSTS (extra host names for the page, comma separated); MCP: SFA_MCP_BIND (default 127.0.0.1,
# the same way), SFA_MCP_PORT (default 8700),
# SFA_MCP_ALLOWED_HOSTS, SFA_MCP_RATE_PER_MIN (default 30); the ask service has no port, only a Unix socket. Only these
# compose projects are ever started or restarted here; nothing of the sf-local-ai stack is touched, and there is
# deliberately no "down".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRIVATE="$(cd "$HERE/.." && pwd)/.private"
PROJECT="sf-assistant"
SERVICE="sf-assistant-web"
MCP_SERVICE="sf-assistant-mcp"
ASK_PROJECT="sf-assistant-ask"
ASK_IMAGE="sf-assistant-web:local"
ASK_VOLUME="sf-local-ai_ask-sock"
# This machine's addresses stay out of the repository: the environment wins, then web/.env (git-ignored, see
# .env.example), then loopback, which no other device can reach.
env_file_value() {
  if [ -f "$HERE/.env" ]; then sed -n "s/^$1=//p" "$HERE/.env" | tail -n 1; fi
}
SFA_BIND="${SFA_BIND:-$(env_file_value SFA_BIND)}"
SFA_MCP_BIND="${SFA_MCP_BIND:-$(env_file_value SFA_MCP_BIND)}"
export SFA_BIND="${SFA_BIND:-127.0.0.1}"
export SFA_PORT="${SFA_PORT:-8600}"
export SFA_MCP_BIND="${SFA_MCP_BIND:-127.0.0.1}"
export SFA_MCP_PORT="${SFA_MCP_PORT:-8700}"
export SFA_UID="${SFA_UID:-$(id -u)}"
export SFA_GID="${SFA_GID:-$(id -g)}"

compose() { docker compose -p "$PROJECT" -f "$HERE/compose.yaml" "$@"; }
ask_compose() { docker compose -p "$ASK_PROJECT" -f "$HERE/compose.ask.yaml" "$@"; }

ensure_private() {
  mkdir -p "$PRIVATE"
  chmod 700 "$PRIVATE"
}

# The host firewall decides which LAN devices reach the page and the MCP port (docs/runbook.md "Firewall"). Warn
# (stderr only, nothing else changes) while ufw is not enabled.
firewall_note() {
  if grep -qsx 'ENABLED=no' /etc/ufw/ufw.conf; then
    echo "warning: the host firewall (ufw) is not enabled (/etc/ufw/ufw.conf: ENABLED=no), so every LAN device can" >&2
    echo "         reach ports ${SFA_PORT} and ${SFA_MCP_PORT}. Set it up first (docs/runbook.md#firewall), above all" >&2
    echo "         before handing out an MCP token: the token travels over plain HTTP." >&2
  fi
}

# The spelling help switch reaches both containers from this shell (compose.yaml: SFA_RESOLVER). Empty or unset is
# on (the code default since 2026-10-27); SFA_RESOLVER=off ./run.sh up turns it off. A value other than off, on or
# empty would only leave it off with a start-up line; refuse it here so a typo never reaches the page.
check_switch() {
  case "$(printf '%s' "${SFA_RESOLVER:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    ""|off|on) ;;
    *)
      echo "SFA_RESOLVER must be off, on or empty (got '${SFA_RESOLVER}'); nothing was started" >&2
      exit 2
      ;;
  esac
}

new_codes() {
  ensure_private
  python3 "$HERE/codes.py" new "$PRIVATE/codes.json"
}

# The MCP container mounts three files of .private (file bind mounts: they must exist first): its audit log
# (read-write), the token file and the audit key (read-only). Nothing else of .private is visible to it.
ensure_mcp_files() {
  ensure_private
  local f
  for f in mcp_log.jsonl mcp_token.json; do
    if [ ! -e "$PRIVATE/$f" ]; then
      (umask 077 && touch "$PRIVATE/$f")
    fi
    chmod 600 "$PRIVATE/$f"
  done
  if [ ! -s "$PRIVATE/mcp_audit_key" ]; then   # the audit log's HMAC key: 32 random bytes, made once, never printed
    (umask 077 && python3 -I -c 'import secrets; print(secrets.token_hex(32))' > "$PRIVATE/mcp_audit_key.tmp" \
      && mv -n "$PRIVATE/mcp_audit_key.tmp" "$PRIVATE/mcp_audit_key"; rm -f "$PRIVATE/mcp_audit_key.tmp")
  fi
  chmod 600 "$PRIVATE/mcp_audit_key"
  if [ ! -s "$PRIVATE/mcp_token.json" ]; then
    echo "No MCP token yet. Run ./run.sh new-mcp-token yourself, in your own terminal; the MCP server answers 503 until then." >&2
  fi
}

# compose.ask.yaml mounts only the log folder ask_logs writable, so the ask service must write through
# web/ask_log.py (RotatingAskLog). An ask_mcp.py that still opens the single file .private/ask_log.jsonl finds no
# writable file in its read-only container, exits 2 at its start and restarts in a loop: start nothing then (the
# writer swap ships with this compose file; docs/runbook.md "The ask log").
check_ask_writer() {
  if [ -f "$HERE/ask_mcp.py" ] && ! grep -q 'RotatingAskLog(' "$HERE/ask_mcp.py"; then
    echo "$HERE/ask_mcp.py still writes the single-file log .private/ask_log.jsonl, which compose.ask.yaml no longer" >&2
    echo "mounts: the service would stop at its start. Ship the ask_mcp.py that writes through RotatingAskLog" >&2
    echo "(web/ask_log.py) together with this compose file (docs/runbook.md, \"The ask log\"). Nothing was started" >&2
    exit 2
  fi
}

# The ask container mounts two paths of .private, which must exist first: its log folder ask_logs (read-write;
# web/ask_log.py writes one file per business day there, mode 600, and deletes only its own daily files older than
# 90 days) and the token file (read-only, a file bind mount). Nothing else of .private is visible to it. The
# single-file log of before 2026-11-25 (ask_log.jsonl) is split into the folder first, with the service stopped
# (../scripts/split_ask_log.py; docs/runbook.md "The ask log"): until then up-ask and restart-ask start nothing.
ensure_ask_files() {
  if [ -e "$PRIVATE/ask_log.jsonl" ] || [ -L "$PRIVATE/ask_log.jsonl" ]; then
    echo "the old single-file ask log $PRIVATE/ask_log.jsonl is still there. With the service stopped" >&2
    echo "(docker stop sf-assistant-ask), split it first: python3 -I $HERE/../scripts/split_ask_log.py is the dry run," >&2
    echo "the same with --apply splits (docs/runbook.md, \"The ask log\"). Nothing was started" >&2
    exit 2
  fi
  ensure_private
  if [ -L "$PRIVATE/ask_logs" ]; then
    echo "$PRIVATE/ask_logs is a symbolic link; the ask log folder must be a real folder. Nothing was started" >&2
    exit 2
  fi
  if [ ! -d "$PRIVATE/ask_logs" ]; then
    (umask 077 && mkdir "$PRIVATE/ask_logs")
  fi
  chmod 700 "$PRIVATE/ask_logs"
  if [ ! -e "$PRIVATE/ask_token.json" ]; then
    (umask 077 && touch "$PRIVATE/ask_token.json")
  fi
  chmod 600 "$PRIVATE/ask_token.json"
  if [ ! -s "$PRIVATE/ask_token.json" ]; then
    echo "No token yet. Run ./run.sh new-ask-token yourself, in your own terminal; the ask service answers 503 until then." >&2
  fi
}

case "${1:-}" in
  up)
    check_switch
    ensure_private
    if ! ip -4 -o addr show 2>/dev/null | grep -q " inet ${SFA_BIND}/"; then
      echo "warning: ${SFA_BIND} is not an address of this machine right now (DHCP?)." >&2
      echo "         Start with SFA_BIND=<current LAN address> ./run.sh up" >&2
    fi
    if [ ! -s "$PRIVATE/codes.json" ]; then
      echo "No access codes yet. Run ./run.sh new-codes yourself, in your own terminal; sign-in answers 503 until then." >&2
    fi
    ensure_mcp_files
    firewall_note
    compose up -d --build
    echo
    echo "Page: http://${SFA_BIND}:${SFA_PORT}/  (the pipeline needs about a minute to load after a start)"
    ;;
  logs)
    compose logs -f --tail=200 "$SERVICE"
    ;;
  status)
    compose ps
    echo
    if command -v curl >/dev/null 2>&1; then
      curl -fsS --max-time 5 "http://${SFA_BIND}:${SFA_PORT}/api/status" && echo || echo "the page does not answer on ${SFA_BIND}:${SFA_PORT}"
    fi
    ;;
  new-codes)
    new_codes
    ;;
  restart-page)
    compose restart "$SERVICE"
    ;;
  up-mcp)
    check_switch
    if ! ip -4 -o addr show 2>/dev/null | grep -q " inet ${SFA_MCP_BIND}/"; then
      echo "warning: ${SFA_MCP_BIND} is not an address of this machine right now (DHCP?)." >&2
      echo "         Start with SFA_MCP_BIND=<current LAN address> ./run.sh up-mcp" >&2
    fi
    ensure_mcp_files
    firewall_note
    if ss -ltnH "sport = :${SFA_MCP_PORT}" 2>/dev/null | grep -q . \
        && ! docker ps --format '{{.Names}}' | grep -qx "$MCP_SERVICE"; then
      echo "port ${SFA_MCP_PORT} is already in use by another program; start with SFA_MCP_PORT=<free port>" >&2
      exit 1
    fi
    compose build "$SERVICE"
    compose up -d --no-deps "$MCP_SERVICE"
    echo
    echo "MCP: http://${SFA_MCP_BIND}:${SFA_MCP_PORT}/mcp  (Authorization: Bearer <token from ./run.sh new-mcp-token>)"
    ;;
  mcp-logs)
    compose logs -f --tail=200 "$MCP_SERVICE"
    ;;
  mcp-status)
    compose ps "$MCP_SERVICE"
    echo
    if command -v curl >/dev/null 2>&1; then
      curl -fsS --max-time 5 "http://${SFA_MCP_BIND}:${SFA_MCP_PORT}/healthz" && echo || echo "the MCP server does not answer on ${SFA_MCP_BIND}:${SFA_MCP_PORT}"
    fi
    ;;
  new-mcp-token)
    ensure_private
    firewall_note
    python3 -I "$HERE/../src/sf_assistant/server/auth.py" new "$PRIVATE/mcp_token.json" "${@:2}"
    ;;
  restart-mcp)
    compose restart "$MCP_SERVICE"
    ;;
  up-ask)
    check_switch
    check_ask_writer
    ensure_ask_files
    if ! docker image inspect "$ASK_IMAGE" >/dev/null 2>&1; then
      echo "no image ${ASK_IMAGE}: up-ask never builds it (the page and the MCP server run on it); ./run.sh up does" >&2
      exit 1
    fi
    if ! docker volume inspect "$ASK_VOLUME" >/dev/null 2>&1; then
      echo "no volume ${ASK_VOLUME}: the platform's compose creates it (writable by uid $(id -u)); up-ask creates nothing shared" >&2
      exit 1
    fi
    ask_compose up -d --no-build
    echo
    echo "Ask service: http://sf-assistant-ask/mcp on the Unix socket /run/sf-assistant-ask/ask.sock of the volume ${ASK_VOLUME}"
    echo "             (Authorization: Bearer <token from ./run.sh new-ask-token>)"
    ;;
  restart-ask)
    check_switch
    check_ask_writer
    ensure_ask_files
    if ! docker image inspect "$ASK_IMAGE" >/dev/null 2>&1; then
      echo "no image ${ASK_IMAGE}: restart-ask never builds it (the page and the MCP server run on it); ./run.sh up does" >&2
      exit 1
    fi
    if ! docker volume inspect "$ASK_VOLUME" >/dev/null 2>&1; then
      echo "no volume ${ASK_VOLUME}: the platform's compose creates it (writable by uid $(id -u)); restart-ask creates nothing shared" >&2
      exit 1
    fi
    ask_compose up -d --no-build --force-recreate
    ;;
  new-ask-token)
    ensure_private
    python3 -I "$HERE/../src/sf_assistant/server/auth.py" new "$PRIVATE/ask_token.json" --name ask --command new-ask-token "${@:2}"
    ;;
  *)
    sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
