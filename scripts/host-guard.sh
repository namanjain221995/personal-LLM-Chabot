#!/usr/bin/env bash
# Host packet filter for the unauthenticated engine ports on the two DGX Sparks.
#
#   scripts/host-guard.sh plan    [--role head|worker]   print the exact ruleset (default; no root, changes nothing)
#   scripts/host-guard.sh apply   [--role head|worker]   root: self-test, then install it atomically with nft -f
#   scripts/host-guard.sh verify  [--role head|worker] [--no-remote]
#                                                        prove the table is there and the ports still answer
#   scripts/host-guard.sh remove                         root: delete ONLY this script's table (the rollback)
#   scripts/host-guard.sh explain PORT IFNAME SADDR [--role R]
#                                                        what the planned ruleset does to one NEW connection
#
# WHY (developer-platform audit F050/F042/F012/F065 on the head, F043/F051 on
# the worker; owner decision "option A", 2026-09-13). The main vLLM API
# (:8000), the engine controller (:9838), the exporters and the worker's OCR
# (:30004) and speech (:30007) engines have no authentication, and the audit
# reached them from the office LAN and the tailnet. Commit 229031c tried to
# close that with a narrower bind (head vLLM on the docker bridge gateway).
# That bind would have taken the two-node engine DOWN on its next recreate:
# the worker's own container healthcheck curls http://10.100.184.1:8000/health
# over the RoCE rail every 30 s and kill -9s its tensor-parallel rank after 8
# misses, and vLLM takes exactly one --host. Callers sit on lo, docker0, the
# app bridge AND the rail, so only 0.0.0.0 serves them all. The owner kept the
# binds production runs today and asked for this filter instead: it closes the
# exposure by INGRESS INTERFACE, which also covers the Linux weak-host case a
# bind cannot (a LAN host routing 10.100.184.2 via 192.168.9.68 reaches a
# rail-bound socket through enP7s7).
#
# WHAT IT NEVER DOES, and why each is load-bearing:
#   * It owns one table, `inet techsara_guard`, and nothing else. No
#     `flush ruleset`, no iptables, no Docker chain: Docker's NAT and
#     DOCKER-USER rules keep every published port (8080, 3000) and the
#     cloudflared tunnel working, and a flush would cut them all.
#   * `ct state established,related accept` is the FIRST rule, so installing
#     it never kills a request in flight (a 10-minute generation, the tenant's
#     batch) and replies to outbound traffic (tunnel, image pulls, the head's
#     scrapes of the worker) are never judged.
#   * Only TCP to the listed ports is judged (`meta l4proto != tcp accept`,
#     then `tcp dport != @guarded_ports accept`). Without the first line the
#     interface drops below would also eat UDP and ICMP arriving on the LAN:
#     DHCP renewals, IPv6 neighbour discovery, tailscale's direct WireGuard.
#     SSH (22), the torch master port (29501), the NCCL/Gloo ephemeral
#     listeners on the rails and every other TCP port fall straight through.
#   * The chain's policy is accept and its priority is -10: a verdict of drop
#     here is final, an accept hands the packet on to whatever else the host
#     runs, unchanged.
#   * `apply` refuses before touching nft if any consumer the 2026-09-13
#     consumer map proved legitimate, or any connection ESTABLISHED to a
#     guarded port right now, would be dropped for its next connection.
#
# Installing or removing it restarts nothing: no container, no engine, no
# model. Every value below can be overridden from the environment; the
# defaults are the addresses the consumer map measured on 2026-09-13.
set -euo pipefail

GUARD_TABLE="techsara_guard"
GUARD_STATE_DIR="${GUARD_STATE_DIR:-/run/techsara-host-guard}"
GUARD_NFT="${GUARD_NFT:-nft}"

# -- interfaces (identical names on both Sparks) -----------------------------
GUARD_LAN_IFNAME="${GUARD_LAN_IFNAME:-enP7s7}"               # office LAN 192.168.8.0/22
GUARD_TAILNET_IFNAME="${GUARD_TAILNET_IFNAME:-tailscale0}"
GUARD_RAIL_A_IFNAME="${GUARD_RAIL_A_IFNAME:-enp1s0f1np1}"     # RoCE rail A
GUARD_RAIL_B_IFNAME="${GUARD_RAIL_B_IFNAME:-enP2p1s0f1np1}"   # RoCE rail B
# -- networks and addresses --------------------------------------------------
GUARD_RAIL_A_SUBNET="${GUARD_RAIL_A_SUBNET:-10.100.184.0/24}"
GUARD_RAIL_B_SUBNET="${GUARD_RAIL_B_SUBNET:-10.100.185.0/24}"
# Every Docker bridge network Docker allocates by default lives in 172.16/12
# (head today: docker0 172.17, sf-local-ai_application 172.18, _inference 172.19).
GUARD_DOCKER_SUBNET="${GUARD_DOCKER_SUBNET:-172.16.0.0/12}"
GUARD_HEAD_LAN_IP="${GUARD_HEAD_LAN_IP:-192.168.9.54}"
GUARD_WORKER_LAN_IP="${GUARD_WORKER_LAN_IP:-192.168.9.68}"
GUARD_HEAD_RAIL_IP="${GUARD_HEAD_RAIL_IP:-10.100.184.1}"
GUARD_WORKER_RAIL_IP="${GUARD_WORKER_RAIL_IP:-10.100.184.2}"
GUARD_HEAD_RAIL_B_IP="${GUARD_HEAD_RAIL_B_IP:-10.100.185.1}"
GUARD_WORKER_RAIL_B_IP="${GUARD_WORKER_RAIL_B_IP:-10.100.185.2}"
# -- ports -------------------------------------------------------------------
# Head: vLLM API 8000, the aux model publishes 8001-8005 (loopback today, judged
# anyway so a future 0.0.0.0 publish on a host-network engine is still closed),
# node_exporter 9100 (live *:9100), GPU exporter 9835, engine controller 9838.
GUARD_HEAD_PORTS="${GUARD_HEAD_PORTS:-8000-8005, 9100, 9835, 9838}"
# Worker: node_exporter 9100, GPU exporter 9835, sentinel 9839, OCR 30004, speech 30007.
GUARD_WORKER_PORTS="${GUARD_WORKER_PORTS:-9100, 9835, 9839, 30004, 30007}"
# The worker ports the head reaches over the office LAN (the orchestrator's OCR
# and ASR clients, Prometheus, the controller's GPU probe). The sentinel is not
# one: the controller polls it over rail A.
GUARD_WORKER_LAN_PORTS="${GUARD_WORKER_LAN_PORTS:-9100, 9835, 30004, 30007}"

CLUSTER_WORKER_SSH="${CLUSTER_WORKER_SSH:-$(id -un 2>/dev/null || echo techsphere)@${GUARD_WORKER_RAIL_IP}}"

die()  { printf 'host-guard: error: %s\n' "$*" >&2; exit 2; }
say()  { printf 'host-guard: %s\n' "$*"; }
warn() { printf 'host-guard: warning: %s\n' "$*" >&2; }

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

# ------------------------------------------------------------------ role ----
# Which Spark this is, from the rail address it carries. Only read when --role
# is not given, so `plan --role` works on a laptop or a CI runner.
detect_role() {
  local addrs
  addrs="$(ip -o -4 addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)" || true
  if grep -qxF "$GUARD_HEAD_RAIL_IP" <<<"$addrs"; then echo head; return; fi
  if grep -qxF "$GUARD_WORKER_RAIL_IP" <<<"$addrs"; then echo worker; return; fi
  die "cannot tell head from worker (neither $GUARD_HEAD_RAIL_IP nor $GUARD_WORKER_RAIL_IP is a local address); pass --role head|worker"
}

# --------------------------------------------------------------- ruleset ----
# The one text both `plan` prints and `apply` installs. The first two lines are
# nft's idempotent-replace idiom: create the table if it is absent, delete it,
# then define it -- all inside ONE nft -f transaction, so a re-run replaces the
# table atomically and there is never a moment with half a ruleset.
render_ruleset() {
  local role="$1"
  printf '# techsara host guard, role=%s. Generated by scripts/host-guard.sh; install with apply, never by hand.\n' "$role"
  printf 'table inet %s\n' "$GUARD_TABLE"
  printf 'delete table inet %s\n' "$GUARD_TABLE"
  printf 'table inet %s {\n' "$GUARD_TABLE"
  if [ "$role" = head ]; then
    cat <<EOF
  set guarded_ports {
    type inet_service
    flags interval
    elements = { ${GUARD_HEAD_PORTS} }
  }

  chain input {
    type filter hook input priority -10; policy accept;
    ct state established,related accept comment "never cut a request in flight"
    iifname "lo" accept comment "host processes: head healthcheck, engine controller canaries, CI verify, deploy.sh, cluster scripts"
    meta l4proto != tcp accept comment "udp, icmp: dhcp, neighbour discovery, tailscale wireguard never judged"
    tcp dport != @guarded_ports accept comment "only the engine ports are judged: ssh, 29501, NCCL, tunnel untouched"
    iifname "docker0" ip saddr ${GUARD_DOCKER_SUBNET} counter accept comment "default bridge: litellm-dgx via host.docker.internal"
    iifname "br-*" ip saddr ${GUARD_DOCKER_SUBNET} counter accept comment "compose bridges: orchestrator, sync-worker, Prometheus, e2e stack"
    iifname "${GUARD_RAIL_A_IFNAME}" ip saddr ${GUARD_RAIL_A_SUBNET} counter accept comment "rail A: worker healthcheck (kills its rank after 8 misses), interview-analysis tenant"
    iifname "${GUARD_RAIL_B_IFNAME}" ip saddr ${GUARD_RAIL_B_SUBNET} counter accept comment "rail B: failover path for the same consumers"
    iifname "${GUARD_LAN_IFNAME}" counter drop comment "office LAN (audit F050/F042/F012)"
    iifname "${GUARD_TAILNET_IFNAME}" counter drop comment "tailnet (audit F050)"
    counter drop comment "any other ingress: a spoofed source, a new interface"
  }
EOF
  else
    cat <<EOF
  set guarded_ports {
    type inet_service
    flags interval
    elements = { ${GUARD_WORKER_PORTS} }
  }

  set head_lan_ports {
    type inet_service
    flags interval
    elements = { ${GUARD_WORKER_LAN_PORTS} }
  }

  chain input {
    type filter hook input priority -10; policy accept;
    ct state established,related accept comment "never cut a request in flight"
    iifname "lo" accept comment "host-network healthchecks of OCR, speech, sentinel and the GPU exporter"
    meta l4proto != tcp accept comment "udp, icmp: dhcp, neighbour discovery, tailscale wireguard never judged"
    tcp dport != @guarded_ports accept comment "only the engine ports are judged: ssh, NCCL and Gloo rank ports untouched"
    iifname "${GUARD_RAIL_A_IFNAME}" ip saddr ${GUARD_RAIL_A_SUBNET} counter accept comment "rail A: engine controller to the sentinel"
    iifname "${GUARD_RAIL_B_IFNAME}" ip saddr ${GUARD_RAIL_B_SUBNET} counter accept comment "rail B: failover path"
    iifname "${GUARD_LAN_IFNAME}" ip saddr ${GUARD_HEAD_LAN_IP} tcp dport @head_lan_ports counter accept comment "the head (every head container masquerades as this address): OCR, ASR, Prometheus, controller GPU probe"
    iifname "docker0" ip saddr ${GUARD_DOCKER_SUBNET} counter accept comment "worker-local containers"
    iifname "br-*" ip saddr ${GUARD_DOCKER_SUBNET} counter accept comment "worker-local containers"
    iifname "${GUARD_LAN_IFNAME}" counter drop comment "office LAN (audit F043/F051)"
    iifname "${GUARD_TAILNET_IFNAME}" counter drop comment "tailnet"
    counter drop comment "any other ingress: a spoofed source, a new interface"
  }
EOF
  fi
  printf '}\n'
}

# ------------------------------------------------------------- evaluator ----
# What the RENDERED TEXT does to a new TCP connection (port, ingress interface,
# source address). It reads the ruleset back rather than the variables that
# produced it, so the self-test judges what nft would be given. It understands
# exactly the rule shapes render_ruleset emits; anything else is an error, not
# a silent accept.
ipv4_to_int() {
  local IFS=. a b c d
  read -r a b c d <<<"$1"
  echo $(( (a << 24) | (b << 16) | (c << 8) | d ))
}

ipv4_in_cidr() {  # ADDR CIDR-or-ADDR
  local addr="$1" net="${2%/*}" bits=32
  [[ "$2" == */* ]] && bits="${2#*/}"
  [[ "$addr" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || return 1
  local mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  (( ( $(ipv4_to_int "$addr") & mask ) == ( $(ipv4_to_int "$net") & mask ) ))
}

port_in_set() {  # PORT SETNAME RULESET
  local port="$1" name="$2" ruleset="$3" elements element
  elements="$(awk -v n="$name" '
    $1 == "set" && $2 == n { inset = 1; next }
    inset && $1 == "elements" { sub(/.*\{/, ""); sub(/\}.*/, ""); print; exit }
    inset && $1 == "}" { exit }' <<<"$ruleset")"
  [ -n "$elements" ] || die "evaluator: set @$name is not defined in the ruleset"
  local IFS=,
  for element in $elements; do
    element="${element// /}"
    if [[ "$element" == *-* ]]; then
      (( port >= ${element%-*} && port <= ${element#*-} )) && return 0
    else
      (( port == element )) && return 0
    fi
  done
  return 1
}

# verdict PORT IFNAME SADDR RULESET -> prints "accept|drop<TAB>the matching rule"
verdict() {
  local port="$1" ifname="$2" saddr="$3" ruleset="$4" line rule in_chain=0
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    if [[ "$line" == "chain input {" ]]; then in_chain=1; continue; fi
    (( in_chain )) || continue
    [[ "$line" == "}" ]] && break
    [[ -z "$line" || "$line" == type\ * ]] && continue
    rule="$line"
    line="$(sed -E 's/ comment "[^"]*"$//; s/(^| )counter( |$)/\1/g' <<<"$line")"
    # A new connection is never ESTABLISHED; that rule protects the ones in flight.
    [[ "$line" == ct\ state\ established,related\ * ]] && continue
    # The evaluator judges TCP connections only, so the non-TCP pass-through never matches here.
    [[ "$line" == "meta l4proto != tcp accept" ]] && continue
    local matched=1
    # Captures are copied out at once: a later [[ ]] resets BASH_REMATCH.
    local pattern rest negate
    if [[ "$line" =~ ^iifname\ \"([^\"]+)\"\ ?(.*)$ ]]; then
      pattern="${BASH_REMATCH[1]}"; rest="${BASH_REMATCH[2]}"
      # shellcheck disable=SC2053  # the rule's name is a glob on purpose ("br-*")
      [[ "$ifname" == $pattern ]] || matched=0
      line="$rest"
    fi
    if [[ "$line" =~ ^ip\ saddr\ ([0-9./]+)\ ?(.*)$ ]]; then
      pattern="${BASH_REMATCH[1]}"; rest="${BASH_REMATCH[2]}"
      ipv4_in_cidr "$saddr" "$pattern" || matched=0   # an IPv6 source never matches `ip saddr`
      line="$rest"
    fi
    if [[ "$line" =~ ^tcp\ dport\ (!=\ )?@([a-z_]+)\ ?(.*)$ ]]; then
      negate="${BASH_REMATCH[1]}"; pattern="${BASH_REMATCH[2]}"; rest="${BASH_REMATCH[3]}"
      if port_in_set "$port" "$pattern" "$ruleset"; then
        [ -n "$negate" ] && matched=0
      else
        [ -z "$negate" ] && matched=0
      fi
      line="$rest"
    fi
    [[ "$line" == accept || "$line" == drop ]] || die "evaluator: cannot read rule: $rule"
    if (( matched )); then printf '%s\t%s\n' "$line" "$rule"; return; fi
  done <<<"$ruleset"
  printf 'accept\tchain policy accept\n'
}

# ------------------------------------------------------------ self-test ----
# The consumer map of 2026-09-13, written independently of the rules above:
# EXPECT PORT IFNAME SADDR WHO. A rule edit that drops one of these, or opens
# one of the "drop" rows, fails `apply` before nft is ever called.
declared_cases() {
  local role="$1"
  if [ "$role" = head ]; then
    cat <<EOF
accept 8000 ${GUARD_RAIL_A_IFNAME} ${GUARD_WORKER_RAIL_IP} worker vllm-worker healthcheck (kills its rank after 8 misses)
accept 8000 ${GUARD_RAIL_A_IFNAME} ${GUARD_WORKER_RAIL_IP} interview-analysis tenant on the worker
accept 8000 ${GUARD_RAIL_B_IFNAME} ${GUARD_WORKER_RAIL_B_IP} rail B failover
accept 8000 lo 127.0.0.1 head vLLM healthcheck, CI verify, deploy.sh health gate
accept 8000 lo ${GUARD_HEAD_LAN_IP} engine controller canaries (logged from the LAN address, arrive on lo)
accept 8000 br-51534c8adf93 172.18.0.19 orchestrator via vllm:host-gateway
accept 8000 br-51534c8adf93 172.18.0.14 sync-worker
accept 8000 br-51534c8adf93 172.18.0.15 Prometheus job vllm-main
accept 8000 docker0 172.17.0.4 litellm-dgx via host.docker.internal
accept 9838 br-51534c8adf93 172.18.0.19 orchestrator engine_state poller
accept 9838 br-51534c8adf93 172.18.0.20 techsara-e2e-orchestrator
accept 9838 br-51534c8adf93 172.18.0.15 Prometheus job engine-controller
accept 9838 lo 127.0.0.1 controller healthcheck, cluster-recover.sh, cluster-status.sh
accept 9100 br-51534c8adf93 172.18.0.15 Prometheus job node
accept 9835 lo 127.0.0.1 controller HEAD_GPU_EXPORTER_URL
accept 8002 lo 127.0.0.1 controller ROUTER_HEALTH_URL
accept 22 ${GUARD_LAN_IFNAME} 192.168.9.20 ssh from the office (never judged)
accept 22 ${GUARD_TAILNET_IFNAME} 100.64.0.9 ssh over the tailnet (never judged)
accept 29501 ${GUARD_RAIL_A_IFNAME} ${GUARD_WORKER_RAIL_IP} torch master port (never judged here)
accept 43477 ${GUARD_TAILNET_IFNAME} 100.64.0.9 tailscaled (never judged)
drop 8000 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN host to the raw model API
drop 8000 ${GUARD_LAN_IFNAME} ${GUARD_WORKER_LAN_IP} the worker over the LAN instead of the rail
drop 8000 ${GUARD_LAN_IFNAME} 172.18.0.19 a LAN packet spoofing a bridge address
drop 8000 ${GUARD_TAILNET_IFNAME} 100.64.0.9 tailnet host
drop 8000 ${GUARD_TAILNET_IFNAME} fd7a:115c:a1e0::9 tailnet host over IPv6
drop 8000 wlP9s9 192.168.50.3 an interface that comes up later
drop 9838 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN to the controller
drop 9100 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN to node_exporter
drop 9100 ${GUARD_TAILNET_IFNAME} fd7a:115c:a1e0::9 tailnet to node_exporter over IPv6
drop 8003 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN to an aux model publish
EOF
  else
    cat <<EOF
accept 30004 ${GUARD_LAN_IFNAME} ${GUARD_HEAD_LAN_IP} head orchestrator OCR client and Prometheus file_sd scrape
accept 30007 ${GUARD_LAN_IFNAME} ${GUARD_HEAD_LAN_IP} head orchestrator ASR client
accept 9100 ${GUARD_LAN_IFNAME} ${GUARD_HEAD_LAN_IP} head Prometheus job node
accept 9835 ${GUARD_LAN_IFNAME} ${GUARD_HEAD_LAN_IP} head Prometheus dgx-gpu and controller WORKER_GPU_EXPORTER_URL
accept 9839 ${GUARD_RAIL_A_IFNAME} ${GUARD_HEAD_RAIL_IP} head engine controller to the sentinel
accept 30004 lo ${GUARD_WORKER_LAN_IP} OCR container healthcheck
accept 30007 lo ${GUARD_WORKER_LAN_IP} whisper container healthcheck
accept 9835 lo ${GUARD_WORKER_LAN_IP} GPU exporter healthcheck
accept 9839 lo ${GUARD_WORKER_RAIL_IP} sentinel healthcheck
accept 22 ${GUARD_LAN_IFNAME} 192.168.9.20 ssh from the office (never judged)
accept 22 ${GUARD_RAIL_A_IFNAME} ${GUARD_HEAD_RAIL_IP} cluster scripts over ssh (never judged)
accept 22 ${GUARD_RAIL_B_IFNAME} ${GUARD_HEAD_RAIL_B_IP} cluster-sync.sh --via-link2 (never judged)
accept 33183 ${GUARD_RAIL_A_IFNAME} ${GUARD_HEAD_RAIL_IP} vLLM rank / Gloo ephemeral listener (never judged)
drop 30004 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN host to OCR
drop 30007 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN host to speech
drop 9100 ${GUARD_LAN_IFNAME} 192.168.9.20 office LAN host to node_exporter
drop 9839 ${GUARD_LAN_IFNAME} ${GUARD_HEAD_LAN_IP} the sentinel has no LAN consumer
drop 30004 ${GUARD_TAILNET_IFNAME} 100.64.0.9 tailnet host to OCR
drop 30007 ${GUARD_TAILNET_IFNAME} fd7a:115c:a1e0::9 tailnet host to speech over IPv6
EOF
  fi
}

# self_test ROLE RULESET -> prints each failing case; returns 1 if any failed.
self_test() {
  local role="$1" ruleset="$2" expect port ifname saddr who got failed=0
  while read -r expect port ifname saddr who; do
    [ -n "$expect" ] || continue
    got="$(verdict "$port" "$ifname" "$saddr" "$ruleset" | cut -f1)"
    if [ "$got" != "$expect" ]; then
      printf '  SELF-TEST FAIL: %s:%s from %s on %s would be %s, must be %s (%s)\n' \
        "$role" "$port" "$saddr" "$ifname" "$got" "$expect" "$who" >&2
      failed=1
    fi
  done < <(declared_cases "$role")
  return "$failed"
}

# ------------------------------------------------------- live preflight ----
# On the real host, before installing: the interface names and rail subnets
# above must be what this machine has, Docker's bridges must sit where the
# rules expect them, and nobody connected to a guarded port right now may lose
# the next connection.
strip_addr() {  # ss address[:port] -> address (brackets and ::ffff: removed)
  local a="${1%:*}"
  a="${a#[}"; a="${a%]}"; a="${a#::ffff:}"
  printf '%s' "${a%%%*}"
}

live_preflight() {
  local role="$1" ruleset="$2" failed=0 addrs name addr
  addrs="$(ip -o addr show 2>/dev/null)" || die "cannot read interface addresses with ip"
  for name in "$GUARD_RAIL_A_IFNAME" "$GUARD_LAN_IFNAME"; do
    grep -qE "^[0-9]+: ${name}[[:space:]@]" <<<"$addrs" \
      || { printf '  PREFLIGHT FAIL: interface %s is not on this host; the rules name it\n' "$name" >&2; failed=1; }
  done
  local rail_ok=0
  while read -r name addr; do
    [ "$name" = "$GUARD_RAIL_A_IFNAME" ] && ipv4_in_cidr "${addr%/*}" "$GUARD_RAIL_A_SUBNET" && rail_ok=1
    # A Docker-style bridge outside the accepted names would have its containers dropped.
    if [[ "$addr" =~ ^[0-9.]+/ ]] && ipv4_in_cidr "${addr%/*}" "$GUARD_DOCKER_SUBNET" \
        && [[ "$name" != docker0 && "$name" != br-* ]]; then
      printf '  PREFLIGHT FAIL: %s carries %s inside %s but is neither docker0 nor br-*\n' "$name" "$addr" "$GUARD_DOCKER_SUBNET" >&2
      failed=1
    fi
  done < <(awk '$3 == "inet" { sub(/@.*/, "", $2); print $2, $4 }' <<<"$addrs")
  (( rail_ok )) || { printf '  PREFLIGHT FAIL: %s has no address in %s\n' "$GUARD_RAIL_A_IFNAME" "$GUARD_RAIL_A_SUBNET" >&2; failed=1; }
  if [ "$role" = head ]; then
    grep -qE "^[0-9]+: ${GUARD_LAN_IFNAME}[[:space:]].* inet ${GUARD_HEAD_LAN_IP}/" <<<"$addrs" \
      || { printf '  PREFLIGHT FAIL: GUARD_HEAD_LAN_IP=%s is not on %s here\n' "$GUARD_HEAD_LAN_IP" "$GUARD_LAN_IFNAME" >&2; failed=1; }
  fi

  local local_field peer_field port peer iif got rule seen=""
  while read -r local_field peer_field; do
    [ -n "$local_field" ] || continue
    port="${local_field##*:}"
    [[ "$port" =~ ^[0-9]+$ ]] || continue
    port_in_set "$port" guarded_ports "$ruleset" || continue
    peer="$(strip_addr "$peer_field")"
    case " $seen " in *" $port/$peer "*) continue ;; esac
    seen="$seen $port/$peer"
    iif="$(ip -o route get "$peer" 2>/dev/null | awk '{ if ($1 == "local") { print "lo"; exit } for (i = 1; i < NF; i++) if ($i == "dev") { print $(i+1); exit } }')"
    [ -n "$iif" ] || iif="unknown"
    IFS=$'\t' read -r got rule < <(verdict "$port" "$iif" "$peer" "$ruleset")
    if [ "$got" != accept ]; then
      if [ "${GUARD_FORCE_PEERS:-0}" = 1 ]; then
        warn "established peer $peer on $iif to :$port will be refused on its next connection ($rule); continuing because --force-peers"
      else
        printf '  PREFLIGHT FAIL: %s (via %s) is connected to :%s now and its next connection would be dropped by: %s\n' "$peer" "$iif" "$port" "$rule" >&2
        failed=1
      fi
    fi
  done < <(ss -tnH state established 2>/dev/null | awk '{ print $(NF-1), $NF }')
  return "$failed"
}

# --------------------------------------------------------------- commands ----
ROLE=""
NO_REMOTE=0
POSITIONAL=()
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --role) ROLE="${2:-}"; shift 2 ;;
      --role=*) ROLE="${1#--role=}"; shift ;;
      --no-remote) NO_REMOTE=1; shift ;;
      --force-peers) GUARD_FORCE_PEERS=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) POSITIONAL+=("$1"); shift ;;
    esac
  done
  if [ -n "$ROLE" ]; then
    [[ "$ROLE" == head || "$ROLE" == worker ]] || die "--role must be head or worker, not '$ROLE'"
  fi
}

resolve_role() { [ -n "$ROLE" ] || ROLE="$(detect_role)"; }

sha256_of() { sha256sum | awk '{print $1}'; }

cmd_plan() {
  resolve_role
  local ruleset
  ruleset="$(render_ruleset "$ROLE")"
  printf '%s\n' "$ruleset"
  self_test "$ROLE" "$ruleset" >/dev/null 2>&1 \
    || { self_test "$ROLE" "$ruleset" || true; die "this plan fails its own consumer self-test; apply would refuse it"; }
}

cmd_explain() {
  [ "${#POSITIONAL[@]}" -eq 3 ] || die "usage: explain PORT IFNAME SADDR [--role head|worker]"
  resolve_role
  verdict "${POSITIONAL[0]}" "${POSITIONAL[1]}" "${POSITIONAL[2]}" "$(render_ruleset "$ROLE")"
}

cmd_apply() {
  resolve_role
  local ruleset file
  ruleset="$(render_ruleset "$ROLE")"
  say "role=$ROLE; self-testing the ruleset against the consumer map"
  self_test "$ROLE" "$ruleset" || die "refusing to apply: a declared consumer would be dropped (or an exposure left open). Nothing was changed."
  [ "$(id -u)" = 0 ] || die "apply needs root (sudo scripts/host-guard.sh apply). Nothing was changed."
  command -v "$GUARD_NFT" >/dev/null 2>&1 || die "nft is not installed. Nothing was changed."
  say "checking this host's interfaces and the connections established to guarded ports"
  live_preflight "$ROLE" "$ruleset" || die "refusing to apply: see PREFLIGHT FAIL above. Nothing was changed."
  mkdir -p "$GUARD_STATE_DIR"
  file="$(mktemp "${GUARD_STATE_DIR}/ruleset.XXXXXX")"
  printf '%s\n' "$ruleset" >"$file"
  "$GUARD_NFT" -c -f "$file" || { rm -f "$file"; die "nft rejected the ruleset in check mode. Nothing was changed."; }
  "$GUARD_NFT" -f "$file"   || { rm -f "$file"; die "nft -f failed; the transaction is atomic, so the previous state stands."; }
  mv -f "$file" "$GUARD_STATE_DIR/ruleset.nft"
  {
    printf 'GUARD_TABLE=inet %s\n' "$GUARD_TABLE"
    printf 'GUARD_ROLE=%s\n' "$ROLE"
    printf 'GUARD_RULESET_SHA256=%s\n' "$(printf '%s\n' "$ruleset" | sha256_of)"
    printf 'GUARD_APPLIED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$GUARD_STATE_DIR/state"
  chmod 0644 "$GUARD_STATE_DIR/state" "$GUARD_STATE_DIR/ruleset.nft"
  "$GUARD_NFT" list table inet "$GUARD_TABLE" >/dev/null || die "the table is not listed after nft -f; run verify"
  say "installed table inet $GUARD_TABLE (role=$ROLE). Nothing was restarted. Next: scripts/host-guard.sh verify"
}

cmd_remove() {
  [ "$(id -u)" = 0 ] || die "remove needs root (sudo scripts/host-guard.sh remove)."
  if "$GUARD_NFT" list table inet "$GUARD_TABLE" >/dev/null 2>&1; then
    "$GUARD_NFT" delete table inet "$GUARD_TABLE"
    say "deleted table inet $GUARD_TABLE; no other table or chain was touched"
  else
    say "table inet $GUARD_TABLE is not installed; nothing to remove"
  fi
  rm -f "$GUARD_STATE_DIR/state" "$GUARD_STATE_DIR/ruleset.nft"
}

# probe NAME EXPECT URL -- EXPECT is "answer" (any HTTP status) or "dropped"
# (curl times out: a DROP gives no answer, a missing listener refuses at once).
VERIFY_FAILED=0
report() { printf '  %-5s %s\n' "$1" "$2"; [ "$1" = FAIL ] && VERIFY_FAILED=1; return 0; }

probe_local() {
  local name="$1" url="$2" code
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null)" || code="000"
  if [ "$code" != 000 ]; then report PASS "$name answers ($url -> HTTP $code)"; else report FAIL "$name does not answer ($url)"; fi
}

cmd_verify() {
  resolve_role
  local ruleset planned_sha table_proof=0
  ruleset="$(render_ruleset "$ROLE")"
  planned_sha="$(printf '%s\n' "$ruleset" | sha256_of)"
  say "verify role=$ROLE"
  if [ "$(id -u)" = 0 ] && command -v "$GUARD_NFT" >/dev/null 2>&1; then
    if "$GUARD_NFT" list table inet "$GUARD_TABLE" >/dev/null 2>&1; then
      report PASS "table inet $GUARD_TABLE is installed (read with nft as root)"
      table_proof=1
      "$GUARD_NFT" list table inet "$GUARD_TABLE" | grep -E 'counter packets [0-9]+' | sed 's/^[[:space:]]*/        /' || true
    else
      report FAIL "table inet $GUARD_TABLE is not installed"
    fi
  elif [ -r "$GUARD_STATE_DIR/state" ]; then
    report PASS "state file $GUARD_STATE_DIR/state says the guard was applied since boot ($(grep '^GUARD_APPLIED_AT=' "$GUARD_STATE_DIR/state" | cut -d= -f2))"
    table_proof=1
  else
    report INFO "not root and no state file: the table cannot be listed; relying on the behavioural probes"
  fi
  if [ -r "$GUARD_STATE_DIR/state" ]; then
    grep -qxF "GUARD_RULESET_SHA256=$planned_sha" "$GUARD_STATE_DIR/state" \
      && report PASS "the installed ruleset is the one this checkout plans" \
      || report WARN "the installed ruleset differs from this checkout's plan; re-run sudo scripts/host-guard.sh apply"
  fi

  local negative_proof=0
  if [ "$ROLE" = head ]; then
    probe_local "vLLM API on loopback" "http://127.0.0.1:8000/health"
    probe_local "engine controller on loopback" "http://127.0.0.1:9838/healthz"
    if (( NO_REMOTE )); then
      report INFO "--no-remote: the rail and LAN probes from the worker were skipped"
    elif ssh -o BatchMode=yes -o ConnectTimeout=5 "$CLUSTER_WORKER_SSH" true 2>/dev/null; then
      local rail lan
      rail="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$CLUSTER_WORKER_SSH" \
        "curl -s -o /dev/null -m 5 -w '%{http_code}' http://${GUARD_HEAD_RAIL_IP}:8000/health; echo \" exit=\$?\"" 2>/dev/null)" || true
      [[ "$rail" == 200\ exit=0 ]] \
        && report PASS "worker -> ${GUARD_HEAD_RAIL_IP}:8000 over rail A answers 200 (the worker healthcheck's path)" \
        || report FAIL "worker -> ${GUARD_HEAD_RAIL_IP}:8000 over rail A did not answer 200 ($rail): the worker healthcheck kills its rank after 8 misses -- run sudo scripts/host-guard.sh remove NOW"
      lan="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$CLUSTER_WORKER_SSH" \
        "curl -s -o /dev/null -m 4 -w '%{http_code}' http://${GUARD_HEAD_LAN_IP}:8000/health; echo \" exit=\$?\"" 2>/dev/null)" || true
      if [[ "$lan" == *exit=28 ]]; then
        report PASS "worker -> ${GUARD_HEAD_LAN_IP}:8000 over the office LAN is dropped (timed out)"
        negative_proof=1
      else
        report FAIL "worker -> ${GUARD_HEAD_LAN_IP}:8000 over the office LAN was not dropped ($lan): the LAN still reaches the raw model API"
      fi
    else
      report WARN "cannot ssh to $CLUSTER_WORKER_SSH; the rail and LAN probes were skipped"
    fi
  else
    probe_local "OCR engine" "http://${GUARD_WORKER_LAN_IP}:30004/v1/models"
    probe_local "speech engine" "http://${GUARD_WORKER_LAN_IP}:30007/health"
    probe_local "GPU exporter" "http://${GUARD_WORKER_LAN_IP}:9835/healthz"
    report INFO "from an office laptop (not the head), curl -m 5 http://${GUARD_WORKER_LAN_IP}:30004/v1/models must time out; from the head, scripts/ocr.sh verify must still read the test image"
  fi
  if (( ! table_proof && ! negative_proof )); then
    report FAIL "no proof the guard is in place (no table listing, no state file, no dropped LAN probe)"
  fi
  (( VERIFY_FAILED )) && { say "verify FAILED"; exit 1; }
  say "verify passed"
}

main() {
  local command="plan"
  if [ $# -gt 0 ] && [[ "$1" != -* ]]; then command="$1"; shift; fi
  parse_args "$@"
  case "$command" in
    plan) cmd_plan ;;
    apply) cmd_apply ;;
    verify) cmd_verify ;;
    remove) cmd_remove ;;
    explain) cmd_explain ;;
    help) usage ;;
    *) usage >&2; die "unknown command '$command'" ;;
  esac
}

main "$@"
