#!/usr/bin/env bash
# Preflight diagnostics for the two-node cluster (read-only; changes nothing).
# Usage: scripts/cluster-doctor.sh [--rdma] [--nccl]
#   --rdma  also run a 3-second ib_write_bw on each link (needs perftest on both)
#   --nccl  also run the two-node NCCL all-reduce test (scripts/cluster-test.sh)
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
DO_RDMA=0; DO_NCCL=0
for a in "$@"; do case "$a" in --rdma) DO_RDMA=1 ;; --nccl) DO_NCCL=1 ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; *) die "unknown option $a" ;; esac; done
cluster_load_settings

section "configuration (.env)"
if [ "$CLUSTER_MODE" = dual ]; then check_pass "CLUSTER_MODE=dual"; else check_warn "CLUSTER_MODE=${CLUSTER_MODE} (dual-node checks below still run against the configured addresses)"; fi
for k in CLUSTER_HEAD_IP CLUSTER_WORKER_IP; do [ -n "${!k:-}" ] && check_pass "$k=${!k}" || check_fail "$k is not set"; done
[ -n "${CLUSTER_HEAD_IP_2:-}" ] && check_pass "second rail configured: ${CLUSTER_HEAD_IP_2} <-> ${CLUSTER_WORKER_IP_2:-?}" || check_warn "no second rail configured (CLUSTER_HEAD_IP_2/CLUSTER_WORKER_IP_2); NCCL will use one link"
tp="${CLUSTER_TENSOR_PARALLEL_SIZE}"; pp="${CLUSTER_PIPELINE_PARALLEL_SIZE}"
[ "$((tp*pp))" = 2 ] && check_pass "parallelism TP=$tp x PP=$pp = 2 GPUs" || check_fail "TP($tp) x PP($pp) must equal 2 for two single-GPU nodes"
[ -n "${CLUSTER_ENGINE_ARGS:-}" ] && check_pass "generated.env has CLUSTER_ENGINE_ARGS" || check_warn "generated.env has no CLUSTER_ENGINE_ARGS yet (run ./techsara up or scripts/cluster-up.sh)"
[ -n "${CLUSTER_HEAD_IP:-}" ] || { check_summary; exit 1; }

section "Node 1 (head) network"
for pair in "A:${CLUSTER_HEAD_IP}" "B:${CLUSTER_HEAD_IP_2:-}"; do
  l="${pair%%:*}"; a="${pair#*:}"; [ -n "$a" ] || continue
  ifn="$(detect_ifname_for_ip "$a" || true)"
  if [ -z "$ifn" ]; then check_fail "link $l: no interface has $a"; continue; fi
  st="$(cat /sys/class/net/$ifn/operstate 2>/dev/null)"; [ "$st" = up ] && check_pass "link $l: $a on $ifn is up (speed $(cat /sys/class/net/$ifn/speed 2>/dev/null || echo ?) Mb/s, mtu $(mtu_for_ifname "$ifn"))" || check_fail "link $l: $ifn operstate=$st"
  hca="$(detect_hca_for_ifname "$ifn" 2>/dev/null || true)"
  if [ -n "$hca" ]; then rs="$(rdma_state_for_hca "$hca")"; [ "$rs" = ACTIVE ] && check_pass "link $l: RDMA device $hca ACTIVE (port rate $(cat /sys/class/infiniband/$hca/ports/1/rate 2>/dev/null))" || check_fail "link $l: RDMA device $hca state $rs"; else check_warn "link $l: no RDMA device behind $ifn (NCCL would use TCP)"; fi
done
[ -e /dev/infiniband/rdma_cm ] && check_pass "/dev/infiniband present" || check_fail "/dev/infiniband missing (rdma-core / mlx5 not loaded)"
gw="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"; [ -n "$gw" ] && check_pass "docker bridge gateway (host-gateway) $gw" || check_warn "cannot read docker bridge gateway"
# Docker can hand GPUs to containers either through a registered `nvidia`
# runtime (daemon.json) or through its built-in device driver that shells out
# to the NVIDIA Container Toolkit hook; this deployment uses the latter on
# the head (no daemon.json) and the former on the worker. Both are fine.
if docker info --format '{{.Runtimes}}' 2>/dev/null | grep -q nvidia; then check_pass "docker GPU support: nvidia runtime registered"
elif command -v nvidia-container-runtime-hook >/dev/null 2>&1 || command -v nvidia-container-cli >/dev/null 2>&1 || command -v nvidia-ctk >/dev/null 2>&1; then check_pass "docker GPU support: NVIDIA Container Toolkit present ($(nvidia-ctk --version 2>/dev/null | head -n1 || echo hook))"
else check_fail "docker GPU support: neither an nvidia runtime nor the NVIDIA Container Toolkit was found"; fi
nvidia-smi -L 2>/dev/null | grep -q GB10 && check_pass "GPU: $(nvidia-smi -L | head -n1)" || check_fail "no NVIDIA GB10 visible on the head"

section "Node 2 (worker) network"
for a in "${CLUSTER_WORKER_IP}" "${CLUSTER_WORKER_IP_2:-}"; do [ -n "$a" ] || continue; ping -c 1 -W 2 "$a" >/dev/null 2>&1 && check_pass "ping $a" || check_fail "ping $a failed"; done
if rh="$(ssh_worker hostname 2>/dev/null)"; then
  check_pass "ssh $CLUSTER_WORKER_SSH -> $rh"
  facts="$(ssh_worker "$(detect_snippet)
for a in '${CLUSTER_WORKER_IP}' '${CLUSTER_WORKER_IP_2:-}'; do [ -n \"\$a\" ] || continue; ifn=\$(detect_ifname_for_ip \"\$a\"); hca=\$(detect_hca_for_ifname \"\${ifn:-none}\" 2>/dev/null); echo LINK=\$a\\|\${ifn:-}\\|\$(cat /sys/class/net/\${ifn:-none}/operstate 2>/dev/null)\\|\${hca:-}\\|\$(rdma_state_for_hca \"\${hca:-none}\" 2>/dev/null)\\|\$(mtu_for_ifname \"\${ifn:-none}\"); done
echo IB=\$(test -e /dev/infiniband/rdma_cm && echo yes || echo no)
echo RUNTIME=\$( (docker info --format '{{.Runtimes}}' 2>/dev/null | grep -q nvidia) && echo nvidia || { command -v nvidia-container-runtime-hook >/dev/null 2>&1 || command -v nvidia-ctk >/dev/null 2>&1; } && echo toolkit || echo none)
echo GPU=\$(nvidia-smi -L 2>/dev/null | head -n1)
echo MEM=\$(awk '/MemTotal/{t=\$2} /MemAvailable/{a=\$2} END{printf \"%d %d\", t/1024/1024, a/1024/1024}' /proc/meminfo)
echo UFW=\$(grep -E '^ENABLED=' /etc/ufw/ufw.conf 2>/dev/null | cut -d= -f2)
echo PROJECTS=\$(docker compose ls --format json 2>/dev/null | python3 -c 'import json,sys; print(\",\".join(p[\"Name\"] for p in json.load(sys.stdin)))' 2>/dev/null)" 2>/dev/null)"
  i=A; printf '%s\n' "$facts" | sed -n 's/^LINK=//p' | while IFS='|' read -r a ifn st hca rs mtu; do
    if [ -z "$ifn" ]; then check_fail "worker link $i: no interface has $a"; elif [ "$st" != up ]; then check_fail "worker link $i: $ifn operstate=$st"; else check_pass "worker link $i: $a on $ifn up (mtu $mtu)"; fi
    if [ -n "$hca" ]; then [ "$rs" = ACTIVE ] && check_pass "worker link $i: RDMA $hca ACTIVE" || check_fail "worker link $i: RDMA $hca state $rs"; else check_warn "worker link $i: no RDMA device"; fi
    hm="$(mtu_for_ifname "$(detect_ifname_for_ip "$([ $i = A ] && echo "$CLUSTER_HEAD_IP" || echo "${CLUSTER_HEAD_IP_2:-}")" || echo none)" || true)"
    [ "$hm" = "$mtu" ] && check_pass "link $i: MTU matches on both ends ($mtu)" || check_fail "link $i: MTU mismatch head=$hm worker=$mtu"
    i=B
  done
  [ "$(printf '%s\n' "$facts" | sed -n 's/^IB=//p')" = yes ] && check_pass "worker /dev/infiniband present" || check_fail "worker /dev/infiniband missing"
  rt="$(printf '%s\n' "$facts" | sed -n 's/^RUNTIME=//p')"; case "$rt" in nvidia|toolkit) check_pass "worker docker GPU support: $rt" ;; *) check_fail "worker docker GPU support missing (no nvidia runtime, no NVIDIA Container Toolkit)" ;; esac
  g="$(printf '%s\n' "$facts" | sed -n 's/^GPU=//p')"; [ -n "$g" ] && check_pass "worker GPU: $g" || check_fail "worker GPU not visible"
  read -r wt wa < <(printf '%s\n' "$facts" | sed -n 's/^MEM=//p')
  lt="$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)"; la="$(awk '/MemAvailable/{printf "%d", $2/1024/1024}' /proc/meminfo)"
  need="$(python3 -c "print(int(${CLUSTER_GPU_MEMORY_UTILIZATION}*${lt})+4)")"
  hrun="$(head_container_id || true)"
  if [ -n "$hrun" ]; then check_pass "head memory: ${la} GiB available of ${lt} (head vllm already running)"; elif [ "$la" -ge "$need" ]; then check_pass "head memory: ${la} GiB available of ${lt} (needs ~${need} GiB for util ${CLUSTER_GPU_MEMORY_UTILIZATION})"; else check_warn "head memory: only ${la} GiB available of ${lt}; util ${CLUSTER_GPU_MEMORY_UTILIZATION} needs ~${need} GiB (vLLM sizes memory at profile time)"; fi
  wneed="$(python3 -c "print(int(${CLUSTER_GPU_MEMORY_UTILIZATION}*${wt:-0})+4)")"
  if worker_compose ps -q 2>/dev/null | grep -q .; then check_pass "worker memory: ${wa} GiB available of ${wt} (worker already running)"; elif [ "${wa:-0}" -ge "$wneed" ]; then check_pass "worker memory: ${wa} GiB available of ${wt} (needs ~${wneed} GiB)"; else check_warn "worker memory: only ${wa} GiB available of ${wt}; needs ~${wneed} GiB"; fi
  u="$(printf '%s\n' "$facts" | sed -n 's/^UFW=//p')"; [ "$u" = yes ] && check_warn "worker ufw ENABLED=yes: allow ${CLUSTER_MASTER_PORT}/tcp and NCCL/vLLM dynamic ports from ${CLUSTER_HEAD_IP}" || check_pass "worker ufw not enabled (${u:-absent})"
  pj="$(printf '%s\n' "$facts" | sed -n 's/^PROJECTS=//p')"; [ -n "$pj" ] && log_dim "  worker compose projects: $pj (the worker uses its own project '$WORKER_PROJECT')"
else
  check_fail "ssh $CLUSTER_WORKER_SSH failed (BatchMode). Configure a key in ~/.ssh/config or CLUSTER_WORKER_SSH_OPTS='-i <key>'"
fi

section "Node 1 host"
u="$(grep -E '^ENABLED=' /etc/ufw/ufw.conf 2>/dev/null | cut -d= -f2)"; [ "$u" = yes ] && check_warn "ufw ENABLED=yes: allow ${CLUSTER_MASTER_PORT}/tcp, ${VLLM_PORT}/tcp and NCCL/vLLM dynamic ports from ${CLUSTER_WORKER_IP}" || check_pass "ufw not enabled (${u:-absent})"
systemctl is-active --quiet nftables 2>/dev/null && check_warn "nftables is active; verify cluster traffic between ${CLUSTER_HEAD_IP} and ${CLUSTER_WORKER_IP} is allowed" || check_pass "nftables not active"
if [ -z "$(head_container_id || true)" ]; then
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${CLUSTER_MASTER_PORT}$" && check_fail "port ${CLUSTER_MASTER_PORT} (torch master) already in use" || check_pass "port ${CLUSTER_MASTER_PORT} (torch master) free"
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${VLLM_PORT}$" && check_warn "port ${VLLM_PORT} (API) in use (by the single-node vllm? the launcher recreates it)" || check_pass "port ${VLLM_PORT} (API) free"
else
  check_pass "head vllm container is running"
fi
image="$(head_vllm_image 2>/dev/null || true)"
if [ -n "$image" ]; then
  docker image inspect "$image" >/dev/null 2>&1 && check_pass "image present on head" || check_fail "image $image missing on head"
  ssh_worker "docker image inspect '$image'" >/dev/null 2>&1 && check_pass "image present on worker" || check_warn "image not on worker yet (scripts/cluster-sync.sh pulls it)"
fi
if [ -n "${MAIN_MODEL_CONTAINER_PATH:-}" ]; then
  rel="${MAIN_MODEL_CONTAINER_PATH#/models/}"; [ -f "$TECHSARA_MODEL_CACHE/$rel/config.json" ] && check_pass "model on head: $TECHSARA_MODEL_CACHE/$rel" || check_fail "model missing on head: $TECHSARA_MODEL_CACHE/$rel"
  ssh_worker "test -f '$CLUSTER_WORKER_MODEL_CACHE/$rel/config.json'" 2>/dev/null && check_pass "model on worker: $CLUSTER_WORKER_MODEL_CACHE/$rel" || check_warn "model not on worker yet (scripts/cluster-sync.sh copies it)"
fi

if [ "$DO_RDMA" = 1 ]; then
  section "RDMA bandwidth (ib_write_bw, 3 s per link, head -> worker)"
  command -v ib_write_bw >/dev/null || check_fail "ib_write_bw not installed on head (apt: perftest)"
  for pair in "${CLUSTER_HEAD_IP}:${CLUSTER_WORKER_IP}:18515" "${CLUSTER_HEAD_IP_2:-}:${CLUSTER_WORKER_IP_2:-}:18516"; do
    IFS=: read -r h w port <<<"$pair"; [ -n "$h" ] && [ -n "$w" ] || continue
    hca="$(detect_hca_for_ifname "$(detect_ifname_for_ip "$h")" 2>/dev/null || true)"; [ -n "$hca" ] || continue
    whca="$(ssh_worker "$(detect_snippet); detect_hca_for_ifname \"\$(detect_ifname_for_ip '$w')\"" 2>/dev/null || true)"
    ssh_worker "nohup ib_write_bw -d '$whca' -R -F --report_gbits -s 1048576 -q 4 -D 3 -p $port >/dev/null 2>&1 </dev/null &" ; sleep 2
    bw="$(timeout 30 ib_write_bw -d "$hca" -R -F --report_gbits -s 1048576 -q 4 -D 3 -p "$port" "$w" 2>/dev/null | awk '/^ *[0-9]+ +[0-9]+ /{print $4}' | tail -n1)"
    [ -n "$bw" ] && check_pass "$hca -> $w: ${bw} Gb/s average (1 MiB writes, 4 QPs)" || check_fail "ib_write_bw $hca -> $w produced no result"
  done
fi
if [ "$DO_NCCL" = 1 ]; then "$CLUSTER_LIB_DIR/../cluster-test.sh"; fi
check_summary
