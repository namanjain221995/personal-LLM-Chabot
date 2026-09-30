#!/usr/bin/env bash
# Host packet filter for the unauthenticated engine ports on the two DGX Sparks.
#
#   scripts/host-guard.sh plan    [--role head|worker]   print the exact ruleset (default; no root, changes nothing)
#   scripts/host-guard.sh apply   [--role head|worker] [--degraded]
#                                                        root: self-test, then install it atomically with nft -f
#   scripts/host-guard.sh verify  [--role head|worker] [--no-remote]
#                                                        prove the table is there and the ports still answer
#   scripts/host-guard.sh remove                         root: delete ONLY this script's table (the rollback)
#   scripts/host-guard.sh explain PORT IFNAME SADDR [--role R]
#                                                        what the planned ruleset does to one NEW connection
#   scripts/host-guard.sh install-boot --role head|worker [--dry-run]
#                                                        root: install the boot copy, the role file and the
#                                                        systemd unit that re-applies the guard at every boot
#   scripts/host-guard.sh uninstall-boot [--dry-run]     root: disable and delete that unit; the table stays
#   scripts/host-guard.sh apply-at-boot                  what the unit runs; not meant for hands
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
# AT BOOT (2026-09-22). The table lives in kernel memory and NOTHING used to
# re-apply it: it was applied on 2026-09-16, the reboot of 2026-09-21T18:11
# dropped it, and the unauthenticated engine ports were open to the office LAN
# and the tailnet for hours until the owner re-applied them by hand. Never
# persist the table through the stock /etc/nftables.conf: that file begins
# with `flush ruleset`, which deletes Docker's NAT and filter rules and cuts
# every published port and the tunnel. Instead, on EACH node, once:
#
#   sudo scripts/host-guard.sh install-boot --dry-run --role head    # read it first
#   sudo scripts/host-guard.sh install-boot --role head              # on the head
#   sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker   # on the worker
#   sudo systemctl restart techsara-host-guard.service               # prove it without a reboot
#   scripts/host-guard.sh verify                                     # must say the unit is enabled
#
# install-boot copies THIS script to /usr/local/sbin/techsara-host-guard,
# writes the node's role to /etc/techsara/host-guard.conf (the role is stored,
# never guessed from a hostname), writes and enables
# /etc/systemd/system/techsara-host-guard.service, and records the source
# checksum so `verify` says out loud when the boot copy and the checkout have
# drifted apart. `--dry-run` prints the unit file, the role file and every
# command it would run, and needs no root. `uninstall-boot` reverses it and
# deliberately leaves the loaded table alone.
#
# The unit runs `apply-at-boot` once, after network-online.target and BEFORE
# docker.service, so the filter is in the kernel before dockerd starts the
# engines that listen on the guarded ports. The ordering is ordering only,
# never Requires=: a guard that cannot install must not keep the cluster down.
#
# apply-at-boot never exits quietly:
#   0  the full ruleset is installed.
#   3  DEGRADED. The full apply failed (usually an interface that was renamed
#      or had no address yet), so the fallback ruleset was installed instead:
#      the office LAN and the tailnet are still dropped on the guarded ports,
#      and the final catch-all drop is an accept. The unit is left FAILED on
#      purpose, the state file says GUARD_MODE=degraded, and `verify` fails
#      until a person fixes the host.
#   1  NO FILTER. Not even the fallback could be installed. The unit is FAILED
#      and the engine ports are open: that is an incident.
#
# WHY THE FALLBACK IS SHAPED THAT WAY. At boot, dropping too much is worse
# than dropping too little on one class of traffic: the worker's vLLM rank
# curls the head's engine port over RoCE rail A and kill -9s its own
# tensor-parallel rank after 8 misses, so a boot-time rule that eats the rail
# takes the two-node model down. The rails, loopback, the Docker bridges and
# every port outside the guarded set (ssh above all -- these nodes have no
# console) therefore fail OPEN, while the office LAN and the tailnet fail
# CLOSED on the guarded ports, because those two are the exposure the audit
# actually reached the box through and the cost of being wrong there is one
# operator losing a path they should not have been using.
#
# Installing or removing it restarts nothing: no container, no engine, no
# model. Every value below can be overridden from the environment; the
# defaults are the addresses the consumer map measured on 2026-09-13.
set -euo pipefail

GUARD_TABLE="techsara_guard"
GUARD_STATE_DIR="${GUARD_STATE_DIR:-/run/techsara-host-guard}"
GUARD_NFT="${GUARD_NFT:-nft}"

# -- the boot path -----------------------------------------------------------
# The unit runs a COPY under /usr/local/sbin, not the script in the deploy
# checkout: the deploy job checks main out in the shared working tree on every
# push, several sessions share that tree, and the worker has no checkout at
# all. Root code that runs at boot must not be whatever the tree happens to
# hold at that moment. install-boot is the only thing that writes the copy,
# always from the repository file, and it records the source checksum so
# `verify` can prove the two have not drifted.
GUARD_BOOT_SCRIPT="${GUARD_BOOT_SCRIPT:-/usr/local/sbin/techsara-host-guard}"
GUARD_BOOT_CONF="${GUARD_BOOT_CONF:-/etc/techsara/host-guard.conf}"
GUARD_BOOT_UNIT_NAME="${GUARD_BOOT_UNIT_NAME:-techsara-host-guard.service}"
GUARD_BOOT_UNIT="${GUARD_BOOT_UNIT:-/etc/systemd/system/${GUARD_BOOT_UNIT_NAME}}"
# How long apply-at-boot waits for the addresses the rules name before giving
# up on the full ruleset. network-online.target promises nothing about them on
# these nodes: neither NetworkManager-wait-online nor systemd-networkd-wait-
# online is enabled, so the target is reached as soon as the manager is up.
GUARD_BOOT_WAIT_SECS="${GUARD_BOOT_WAIT_SECS:-90}"
GUARD_BOOT_POLL_SECS="${GUARD_BOOT_POLL_SECS:-2}"
# 1 renders and installs the fallback ruleset instead of the guard (--degraded).
GUARD_DEGRADED="${GUARD_DEGRADED:-0}"

# This file, by an absolute path: apply-at-boot re-executes it as a child so a
# failed apply cannot leave the boot path half-done, and install-boot copies it.
GUARD_SELF="${BASH_SOURCE[0]}"
case "$GUARD_SELF" in
  /*) ;;
  *) GUARD_SELF="$(cd "$(dirname "$GUARD_SELF")" && pwd)/$(basename "$GUARD_SELF")" ;;
esac

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

# The title and the command list at the top of this file, down to "# WHY".
usage() { awk 'NR == 1 { next } /^# WHY/ { exit } /^#/ { sub(/^# ?/, ""); print }' "$0"; }

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
#
# THE DEGRADED FALLBACK (GUARD_DEGRADED=1, `--degraded`) is the same ruleset
# with its last rule -- the catch-all drop -- turned into an accept. It is
# what apply-at-boot installs when the full apply fails, and it is the whole
# of the fail-open/fail-closed decision: the office LAN and the tailnet are
# still dropped by name on the guarded ports, and every other ingress is let
# through, because the interface whose name or address we could not confirm
# may be rail A, and a boot-time drop there kills the worker's rank after 8
# missed healthchecks and takes the two-node model down.
render_ruleset() {
  local role="$1" catch_all banner=""
  catch_all='counter drop comment "any other ingress: a spoofed source, a new interface"'
  if [ "${GUARD_DEGRADED:-0}" = 1 ]; then
    catch_all='counter accept comment "DEGRADED fallback: no catch-all drop, only the office LAN and the tailnet are closed"'
    banner=" DEGRADED FALLBACK, not the guard."
  fi
  printf '# techsara host guard, role=%s.%s Generated by scripts/host-guard.sh; install with apply, never by hand.\n' "$role" "$banner"
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
    ${catch_all}
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
    ${catch_all}
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
    # The fallback promises exactly one thing: the office LAN and the tailnet
    # are closed on the guarded ports. Every other "drop" row is an accept
    # there on purpose, so it is not a case against the fallback.
    if [ "${GUARD_DEGRADED:-0}" = 1 ] && [ "$expect" = drop ] \
       && [ "$ifname" != "$GUARD_LAN_IFNAME" ] && [ "$ifname" != "$GUARD_TAILNET_IFNAME" ]; then
      continue
    fi
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

# check_interfaces ROLE -> 0 when every interface and address the rules name
# is on this host. Separate from live_preflight because apply-at-boot polls it
# while the network comes up; it therefore RETURNS instead of dying.
check_interfaces() {
  local role="$1" failed=0 addrs name addr
  addrs="$(ip -o addr show 2>/dev/null)" \
    || { printf '  PREFLIGHT FAIL: cannot read interface addresses with ip\n' >&2; return 1; }
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
  return "$failed"
}

live_preflight() {
  local role="$1" ruleset="$2" failed=0
  check_interfaces "$role" || failed=1

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
DRY_RUN=0
POSITIONAL=()
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --role) ROLE="${2:-}"; shift 2 ;;
      --role=*) ROLE="${1#--role=}"; shift ;;
      --no-remote) NO_REMOTE=1; shift ;;
      --force-peers) GUARD_FORCE_PEERS=1; shift ;;
      --degraded) GUARD_DEGRADED=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
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
  local ruleset file mode=full
  if [ "${GUARD_DEGRADED:-0}" = 1 ]; then mode=degraded; fi
  ruleset="$(render_ruleset "$ROLE")"
  if [ "$mode" = degraded ]; then
    warn "installing the DEGRADED FALLBACK, not the guard: the office LAN and the tailnet stay closed on the guarded ports and every other ingress is accepted. Fix the host and re-run apply without --degraded."
  fi
  say "role=$ROLE mode=$mode; self-testing the ruleset against the consumer map"
  self_test "$ROLE" "$ruleset" || die "refusing to apply: a declared consumer would be dropped (or an exposure left open). Nothing was changed."
  [ "$(id -u)" = 0 ] || die "apply needs root (sudo scripts/host-guard.sh apply). Nothing was changed."
  command -v "$GUARD_NFT" >/dev/null 2>&1 || die "nft is not installed. Nothing was changed."
  if [ "$mode" = degraded ]; then
    # Every live check exists to protect the catch-all drop from a wrong
    # interface name. The fallback has no catch-all drop, so a missing
    # interface cannot cut the rails, ssh or a peer that is connected now.
    say "skipping the live preflight: the fallback has no catch-all drop"
  else
    say "checking this host's interfaces and the connections established to guarded ports"
    live_preflight "$ROLE" "$ruleset" || die "refusing to apply: see PREFLIGHT FAIL above. Nothing was changed."
  fi
  mkdir -p "$GUARD_STATE_DIR"
  file="$(mktemp "${GUARD_STATE_DIR}/ruleset.XXXXXX")"
  printf '%s\n' "$ruleset" >"$file"
  "$GUARD_NFT" -c -f "$file" || { rm -f "$file"; die "nft rejected the ruleset in check mode. Nothing was changed."; }
  "$GUARD_NFT" -f "$file"   || { rm -f "$file"; die "nft -f failed; the transaction is atomic, so the previous state stands."; }
  mv -f "$file" "$GUARD_STATE_DIR/ruleset.nft"
  {
    printf 'GUARD_TABLE=inet %s\n' "$GUARD_TABLE"
    printf 'GUARD_ROLE=%s\n' "$ROLE"
    printf 'GUARD_MODE=%s\n' "$mode"
    printf 'GUARD_RULESET_SHA256=%s\n' "$(printf '%s\n' "$ruleset" | sha256_of)"
    printf 'GUARD_APPLIED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$GUARD_STATE_DIR/state"
  chmod 0644 "$GUARD_STATE_DIR/state" "$GUARD_STATE_DIR/ruleset.nft"
  "$GUARD_NFT" list table inet "$GUARD_TABLE" >/dev/null || die "the table is not listed after nft -f; run verify"
  say "installed table inet $GUARD_TABLE (role=$ROLE, mode=$mode). Nothing was restarted. Next: scripts/host-guard.sh verify"
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

# ------------------------------------------------------------- at boot ----
# boot_role: the role at boot is READ, never guessed. --role wins; otherwise
# GUARD_ROLE, which systemd loads from GUARD_BOOT_CONF as the unit's
# EnvironmentFile. detect_role is deliberately NOT a fallback here: at boot,
# an address that has not come up yet would make the head look like neither
# node, and a wrong role installs the wrong node's ruleset.
boot_role() {
  if [ -z "$ROLE" ] && [ -n "${GUARD_ROLE:-}" ]; then ROLE="${GUARD_ROLE}"; fi
  case "$ROLE" in
    head|worker) ;;
    *) die "apply-at-boot will not guess the role. Expected GUARD_ROLE=head or GUARD_ROLE=worker in $GUARD_BOOT_CONF (write it with: sudo $GUARD_SELF install-boot --role head|worker), or --role on the command line." ;;
  esac
}

# The addresses the rules name must be up before the full ruleset is safe.
# network-online.target does not promise that on these nodes: no wait-online
# service is enabled, so the target is reached as soon as the network manager
# is, and an interface can still be seconds away from its address.
boot_wait_for_fabric() {
  local role="$1" waited=0
  while ! check_interfaces "$role" 2>/dev/null; do
    if [ "$waited" -ge "$GUARD_BOOT_WAIT_SECS" ]; then return 1; fi
    [ "$waited" = 0 ] && say "waiting up to ${GUARD_BOOT_WAIT_SECS}s for the interfaces and addresses the rules name"
    sleep "$GUARD_BOOT_POLL_SECS"
    waited=$(( waited + GUARD_BOOT_POLL_SECS ))
  done
  [ "$waited" = 0 ] || say "the interfaces and addresses the rules name were all up after ${waited}s"
  return 0
}

# What the systemd unit runs. Exit 0 = the guard; 3 = the degraded fallback;
# 1 = no filter at all. 3 and 1 are failures on purpose: a node whose engine
# ports are open must never look healthy in systemctl or in the journal.
cmd_apply_at_boot() {
  boot_role
  say "boot apply: role=$ROLE, script=$GUARD_SELF"
  if ! boot_wait_for_fabric "$ROLE"; then
    warn "after ${GUARD_BOOT_WAIT_SECS}s the interfaces and addresses the rules name are still not all here; trying the full ruleset anyway"
  fi
  # Re-executed as a child, so a `die` inside apply ends that attempt and not
  # this one, and the fallback starts from a clean process.
  if bash "$GUARD_SELF" apply --role "$ROLE"; then
    say "boot apply: the full ruleset is installed"
    return 0
  fi
  warn "======== HOST GUARD: THE FULL RULESET DID NOT INSTALL ========"
  warn "falling back to the degraded ruleset. The office LAN and the tailnet"
  warn "stay CLOSED on the guarded ports; the rails, loopback, the Docker"
  warn "bridges and every unguarded port (ssh above all) are ACCEPTED, because"
  warn "a boot-time drop on rail A kills the worker's tensor-parallel rank"
  warn "after 8 missed healthchecks and takes the model down."
  if GUARD_DEGRADED=1 bash "$GUARD_SELF" apply --role "$ROLE"; then
    warn "======== HOST GUARD: DEGRADED ON THIS NODE ========"
    warn "this is the fallback, not the guard. Usually an interface was renamed"
    warn "or had no address. Fix the host, then:"
    warn "    sudo $GUARD_BOOT_SCRIPT apply --role $ROLE"
    warn "$GUARD_BOOT_UNIT_NAME stays failed until someone does, on purpose."
    return 3
  fi
  warn "======== HOST GUARD: NO FILTER ON THIS NODE ========"
  warn "neither the full ruleset nor the degraded fallback could be installed."
  warn "the unauthenticated engine ports are reachable from the office LAN and"
  warn "the tailnet right now. This is an incident. On the console:"
  warn "    sudo $GUARD_BOOT_SCRIPT plan --role $ROLE"
  warn "and read the failure printed above."
  return 1
}

# run/write_file: execute, or under --dry-run print exactly what would happen.
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '  + %s\n' "$*"; return 0; fi
  "$@"
}

write_file() {  # write_file PATH MODE  (contents on stdin)
  local path="$1" mode="$2" content
  content="$(cat)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  + write %s (mode %s):\n' "$path" "$mode"
    printf '%s\n' "$content" | sed 's/^/  | /'
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" >"$path"
  chmod "$mode" "$path"
}

boot_unit_text() {
  cat <<UNIT
# ${GUARD_BOOT_UNIT}
# Written by scripts/host-guard.sh install-boot. Do not edit it here: re-run
# install-boot, or the boot copy and the repository drift apart.
[Unit]
Description=TechSara host packet filter on the unauthenticated engine ports (nftables table inet ${GUARD_TABLE})
Documentation=file://${GUARD_BOOT_SCRIPT}
Documentation=man:nft(8)
# The rules name interfaces and addresses that must be up, so this runs after
# the network; the engines that listen on the guarded ports are started by
# dockerd, so the filter must be in the kernel before dockerd. nftables.service
# is ordered before us because its stock /etc/nftables.conf begins with
# "flush ruleset" and would delete this table and Docker's rules.
Wants=network-online.target
After=network-online.target nftables.service
Before=docker.service
# Ordering only, never Requires= or BindsTo=: a guard that cannot install must
# not keep the cluster down, and no Condition*= either, because an unmet
# condition SKIPS a unit silently and a node with no filter must never be
# silent.

[Service]
Type=oneshot
# The effect is a kernel table that outlives the process. Without
# RemainAfterExit the unit would read "inactive (dead)" a second after a good
# boot, which is indistinguishable from "never ran". Re-apply with
# \`systemctl restart\`: \`start\` on an active oneshot does nothing.
RemainAfterExit=yes
# The role is read from this file, never guessed from a hostname.
EnvironmentFile=${GUARD_BOOT_CONF}
ExecStart=${GUARD_BOOT_SCRIPT} apply-at-boot
# apply-at-boot waits up to ${GUARD_BOOT_WAIT_SECS}s for the fabric addresses.
TimeoutStartSec=$(( GUARD_BOOT_WAIT_SECS + 90 ))
# Exit 3 = the degraded fallback is loaded, exit 1 = no filter at all. Both
# leave the unit failed on purpose, so systemctl --failed and the journal show
# it. No Restart=: a second identical attempt cannot fix a renamed interface.
# No ExecStop=: stopping or disabling this unit must NEVER re-open the raw
# model API. The rollback is an explicit \`${GUARD_BOOT_SCRIPT} remove\`.

[Install]
WantedBy=multi-user.target
UNIT
}

boot_conf_text() {  # boot_conf_text ROLE SOURCE_PATH SOURCE_SHA256
  cat <<CONF
# ${GUARD_BOOT_CONF} -- which Spark this is.
# systemd reads it as the EnvironmentFile of ${GUARD_BOOT_UNIT_NAME}, and
# ${GUARD_BOOT_SCRIPT} reads GUARD_ROLE from it at boot. Written by
# scripts/host-guard.sh install-boot; do not edit by hand.
GUARD_ROLE=$1
GUARD_INSTALLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
GUARD_SOURCE_PATH=$2
GUARD_SOURCE_SHA256=$3
CONF
}

cmd_install_boot() {
  [ -n "$ROLE" ] || die "install-boot needs --role head|worker and will not guess one (this host's rail addresses look like '$(detect_role 2>/dev/null || echo unknown)'). The role is written to $GUARD_BOOT_CONF and read back at boot."
  local source_sha
  source_sha="$(sha256_of <"$GUARD_SELF")"
  if [ "$DRY_RUN" != 1 ]; then
    [ "$(id -u)" = 0 ] || die "install-boot needs root (sudo $GUARD_SELF install-boot --role $ROLE). Nothing was changed; --dry-run prints everything it would do."
    command -v systemctl >/dev/null 2>&1 || die "there is no systemctl on this host; install-boot has nothing to install into."
  fi
  say "install-boot role=$ROLE, source $GUARD_SELF ($source_sha)"
  [ "$DRY_RUN" = 1 ] && say "--dry-run: nothing below is executed."
  if command -v systemctl >/dev/null 2>&1; then
    local nftables_state
    nftables_state="$(systemctl is-enabled nftables.service 2>/dev/null || true)"
    case "$nftables_state" in
      enabled|enabled-runtime)
        warn "nftables.service is $nftables_state on this host. Its stock /etc/nftables.conf begins with 'flush ruleset', which deletes Docker's NAT and filter rules AND this table. Disable it (sudo systemctl disable nftables.service) before relying on the guard at boot." ;;
    esac
  fi
  # An operator on the worker may well be running the boot copy itself.
  if [ "$GUARD_SELF" -ef "$GUARD_BOOT_SCRIPT" ] 2>/dev/null; then
    say "$GUARD_BOOT_SCRIPT is this very file; leaving it as it is"
  else
    run install -D -m 0755 "$GUARD_SELF" "$GUARD_BOOT_SCRIPT"
  fi
  boot_conf_text "$ROLE" "$GUARD_SELF" "$source_sha" | write_file "$GUARD_BOOT_CONF" 0644
  boot_unit_text | write_file "$GUARD_BOOT_UNIT" 0644
  run systemctl daemon-reload || die "systemctl daemon-reload failed; $GUARD_BOOT_UNIT_NAME is NOT active yet."
  # enable, never `enable --now`: starting it here would apply the ruleset as a
  # side effect of an install. The table already loaded stays exactly as it is.
  run systemctl enable "$GUARD_BOOT_UNIT_NAME" \
    || die "systemctl enable failed; the unit file is written but NOT enabled, so it will not run at the next boot."
  if [ "$DRY_RUN" = 1 ]; then say "--dry-run: nothing above was executed."; return 0; fi
  say "installed. The table loaded right now was not touched; the unit takes effect at the next boot."
  say "To prove the boot path without rebooting (it re-installs the same bytes in one nft transaction):"
  say "    sudo systemctl restart $GUARD_BOOT_UNIT_NAME && systemctl status $GUARD_BOOT_UNIT_NAME --no-pager"
  say "    $GUARD_SELF verify --role $ROLE"
}

cmd_uninstall_boot() {
  if [ "$DRY_RUN" != 1 ]; then
    [ "$(id -u)" = 0 ] || die "uninstall-boot needs root (sudo $GUARD_SELF uninstall-boot). Nothing was changed; --dry-run prints everything it would do."
  fi
  say "uninstall-boot: $GUARD_BOOT_UNIT_NAME and $GUARD_BOOT_CONF"
  [ "$DRY_RUN" = 1 ] && say "--dry-run: nothing below is executed."
  # `disable`, never `disable --now`: stopping a unit must not be a way to
  # re-open the raw model API, so the loaded table is left exactly as it is.
  run systemctl disable "$GUARD_BOOT_UNIT_NAME" \
    || warn "systemctl disable reported an error (the unit may already be gone); continuing"
  run rm -f "$GUARD_BOOT_UNIT"
  run rm -f "$GUARD_BOOT_CONF"
  run systemctl daemon-reload || warn "systemctl daemon-reload failed; run it by hand"
  say "table inet $GUARD_TABLE is STILL LOADED and $GUARD_BOOT_SCRIPT is still there -- it is the rollback tool on a node with no checkout."
  say "    sudo $GUARD_BOOT_SCRIPT remove    # if the filter should go too"
  say "    sudo rm -f $GUARD_BOOT_SCRIPT     # if the boot copy should go too"
  say "The next reboot will leave the engine ports open again."
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
    grep -qxF "GUARD_MODE=degraded" "$GUARD_STATE_DIR/state" \
      && report FAIL "the loaded ruleset is the DEGRADED FALLBACK, not the guard: the office LAN and the tailnet are closed on the guarded ports and nothing else is. Fix the host, then sudo $GUARD_SELF apply --role $ROLE" \
      || true
  fi

  # -- will it be there after the next reboot? -------------------------------
  # The table is kernel memory. It was applied on 2026-09-16, the reboot of
  # 2026-09-21T18:11 dropped it, and the ports were open for hours.
  if [ -r "$GUARD_BOOT_CONF" ]; then
    local boot_role boot_sha self_sha unit_state
    boot_role="$(awk -F= '$1 == "GUARD_ROLE" { print $2; exit }' "$GUARD_BOOT_CONF")"
    [ "$boot_role" = "$ROLE" ] \
      && report PASS "$GUARD_BOOT_CONF stores role=$boot_role; the unit reads it and never guesses" \
      || report FAIL "$GUARD_BOOT_CONF stores role='$boot_role' but this is a $ROLE: re-run sudo $GUARD_SELF install-boot --role $ROLE"
    if [ -r "$GUARD_BOOT_SCRIPT" ]; then
      boot_sha="$(sha256_of <"$GUARD_BOOT_SCRIPT")"
      self_sha="$(sha256_of <"$GUARD_SELF")"
      [ "$boot_sha" = "$self_sha" ] \
        && report PASS "the boot copy $GUARD_BOOT_SCRIPT is byte-identical to this script" \
        || report FAIL "the boot copy $GUARD_BOOT_SCRIPT has DRIFTED from this script; re-run sudo $GUARD_SELF install-boot --role $ROLE"
    else
      report FAIL "$GUARD_BOOT_CONF is here but the boot copy $GUARD_BOOT_SCRIPT is not; the unit would fail at the next boot"
    fi
    if command -v systemctl >/dev/null 2>&1; then
      unit_state="$(systemctl is-enabled "$GUARD_BOOT_UNIT_NAME" 2>/dev/null || true)"
      [ "$unit_state" = enabled ] \
        && report PASS "$GUARD_BOOT_UNIT_NAME is enabled: the guard is re-applied at every boot" \
        || report FAIL "$GUARD_BOOT_UNIT_NAME is '${unit_state:-not installed}': the guard will NOT come back after a reboot"
    fi
  else
    report WARN "nothing re-applies this table at boot (no $GUARD_BOOT_CONF): the next reboot leaves the engine ports open. Fix with sudo $GUARD_SELF install-boot --role $ROLE"
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
    apply-at-boot) cmd_apply_at_boot ;;
    install-boot) cmd_install_boot ;;
    uninstall-boot) cmd_uninstall_boot ;;
    verify) cmd_verify ;;
    remove) cmd_remove ;;
    explain) cmd_explain ;;
    help) usage ;;
    *) usage >&2; die "unknown command '$command'" ;;
  esac
}

main "$@"
