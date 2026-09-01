#!/usr/bin/env bash
# Public access for the workspace, through a Cloudflare Tunnel.
#
#   scripts/tunnel.sh up       start the tunnel (the site goes live)
#   scripts/tunnel.sh down     stop it (the site goes offline; app keeps running)
#   scripts/tunnel.sh status   container + tunnel connection state
#   scripts/tunnel.sh logs     follow cloudflared's log
#   scripts/tunnel.sh check    verify the public hostname end to end
#
# The tunnel dials OUT to Cloudflare, so nothing is port-forwarded and this
# machine needs no public address. It exposes exactly one thing: the hostname
# configured in the Cloudflare dashboard, pointed at frontend:3000. Model APIs,
# PostgreSQL and pgAdmin are not in that mapping and are not reachable.
#
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

PROJECT="${TECHSARA_PROJECT:-sf-local-ai}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-ai.techsarasolutions.com}"

is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }

# The SAME overlay chain the launcher uses, plus the tunnel file. A subset
# would recreate other services from definitions it cannot see — that is how
# the orchestrator once got silently downgraded from :cuda to :cpu.
tunnel_compose() {
  local files=(-f compose.yaml -f compose/compose.dgx-spark.yaml)
  case "${TECHSARA_PUBLISH_MODEL_PORTS:-${PUBLISH_MODEL_PORTS:-false}}" in
    true|1|yes) files+=(-f compose/compose.published-dgx-spark.yaml) ;;
  esac
  is_dual_mode && files+=(-f compose/compose.cluster-dgx-spark.yaml)
  files+=(-f compose/compose.cloudflare.yaml)
  ( cd "$ROOT" && docker compose \
      --project-name "$PROJECT" \
      --env-file .env \
      --env-file .runtime/secrets.env \
      --env-file .runtime/generated.env \
      "${files[@]}" \
      --profile tunnel "$@" )
}

require_token() {
  # Either file is fine — compose loads both, and both are gitignored and
  # 0600. .env is where the owner keeps it.
  if ! grep -qs '^CLOUDFLARE_TUNNEL_TOKEN=.' "$ROOT/.env" "$SECRETS_ENV"; then
    die "CLOUDFLARE_TUNNEL_TOKEN is not in .runtime/secrets.env.
  1. Cloudflare dashboard -> Zero Trust -> Networks -> Tunnels -> Create a tunnel
  2. Choose 'Cloudflared', name it (e.g. techsara-ai), and copy the TOKEN
  3. echo 'CLOUDFLARE_TUNNEL_TOKEN=<token>' >> .runtime/secrets.env
  4. In the tunnel's 'Public Hostname' tab add:
       Subdomain: ai     Domain: techsarasolutions.com
       Service:   HTTP   URL: frontend:3000
  Then run this again. The token is a bearer credential — never commit it."
  fi
}

cmd="${1:-status}"; shift || true

# Every branch that renders compose needs the token to EXIST, because the
# service declares it with `:?` (a tunnel with no credential is not a tunnel).
# `check` talks to the public hostname only, so it needs nothing.
case "$cmd" in
  up|down|status|logs) require_token ;;
esac

case "$cmd" in
  up)
    log_info "starting the Cloudflare tunnel"
    tunnel_compose up -d --no-deps cloudflared "$@"
    log_info "waiting for the tunnel to register with Cloudflare"
    for _ in $(seq 1 30); do
      if docker exec "${PROJECT}-cloudflared-1" \
           cloudflared tunnel --metrics 127.0.0.1:20241 ready >/dev/null 2>&1; then
        check_pass "tunnel is connected"
        break
      fi
      sleep 2
    done
    "$0" check
    ;;
  down)
    tunnel_compose stop cloudflared
    tunnel_compose rm -f cloudflared "$@"
    log_info "the public hostname is offline; the app is still running locally"
    ;;
  status)
    tunnel_compose ps cloudflared
    echo
    docker exec "${PROJECT}-cloudflared-1" \
      cloudflared tunnel --metrics 127.0.0.1:20241 ready 2>&1 | head -3 \
      || echo "  (tunnel is not running)"
    ;;
  logs)
    tunnel_compose logs --no-color -f cloudflared "$@"
    ;;
  check)
    echo "== public hostname =="
    code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "https://${PUBLIC_HOSTNAME}/login" || echo 000)"
    printf '  https://%s/login -> %s\n' "$PUBLIC_HOSTNAME" "$code"
    case "$code" in
      200) check_pass "the sign-in page is live" ;;
      000) echo "  not reachable yet — DNS may still be propagating, or the" \
                "Public Hostname is not configured in the dashboard" ;;
      *)   echo "  unexpected status; see: scripts/tunnel.sh logs" ;;
    esac
    echo
    echo "== the things that must NOT be public =="
    for probe in "https://${PUBLIC_HOSTNAME}:8002" "https://${PUBLIC_HOSTNAME}:5432"; do
      printf '  %-45s -> %s\n' "$probe" \
        "$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$probe" 2>/dev/null || echo 'refused (good)')"
    done
    echo "  (a tunnel only serves its mapped hostname; other ports are not routed)"
    ;;
  *)
    die "usage: $0 up|down|status|logs|check"
    ;;
esac
