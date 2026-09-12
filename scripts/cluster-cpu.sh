#!/usr/bin/env bash
# Map and measure the CPU side of the two-node vLLM engine — both DGX Sparks,
# read-only, nothing applied.
#
#   scripts/cluster-cpu.sh                 # topology, container limits, per-process/thread CPU,
#                                          # load, the "some cores must stay free" check; table + JSON
#   scripts/cluster-cpu.sh --recommend     # + a cpuset PLAN derived from the MEASURED topology (proposal only)
#   scripts/cluster-cpu.sh --gdr           # GPUDirect RDMA evidence from the running ranks and the image,
#                                          # and what enabling it would need (a separate experiment)
#   options: --seconds N (sampling window, default 5)  --json PATH  --image REF (for --gdr, default: the head's)
#            --probe (keep 8 streamed 800-token completions in flight during the window, so the
#                     busy-poll threads are visible; the engine is otherwise sampled as found)
#   From a worktree without .env: CLUSTER_MODE=dual CLUSTER_HEAD_IP=... CLUSTER_WORKER_IP=... scripts/cluster-cpu.sh
#
# The GB10 is a 20-core big.LITTLE part (10 x Cortex-X925 + 10 x Cortex-A725)
# and the engine's hottest threads are busy-pollers: the rank's main thread,
# EngineCore's loop and the NCCL proxy spin at ~100 % of a core each even at
# idle (measured 2026-09-12: Worker_TP0 99 % with the GPU at 0 %). Which core
# class they land on, and whether anything is left for the kernel, the NIC
# IRQs and the sidecars, is what this script shows. Core classes are read
# from MIDR_EL1 (part number bits [15:4]: 0xd85 = X925, 0xd87 = A725) and
# cross-checked against lscpu's MAXMHZ, never guessed from core ids.
#
# NCCL proxy threads keep the process comm ("VLLM::Worker") unless the engine
# runs with NCCL_SET_THREAD_NAME=1, so they are identified heuristically: the
# non-main threads of the rank process with sustained CPU in the window.
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

MODE=measure; SECONDS_WINDOW=5; JSON_OUT=""; GDR_IMAGE=""; PROBE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --recommend) MODE=recommend ;;
    --gdr) MODE=gdr ;;
    --probe) PROBE=1 ;;
    --seconds) SECONDS_WINDOW="$2"; shift ;;
    --json) JSON_OUT="$2"; shift ;;
    --image) GDR_IMAGE="$2"; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
cluster_load_settings
require_dual_mode

HEAD_CTR="${VLLM_WATCHDOG_TARGET:-sf-local-ai-vllm-1}"
WORKER_CTR="sf-local-ai-worker-vllm-worker-1"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ts="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$JSON_OUT" ] || JSON_OUT="$LOG_DIR/cluster-cpu-$ts.json"

# ------------------------------------------------------------ node snippet --
# POSIX sh, fed on stdin to `sh -s -- <container> <seconds>` locally and to
# the worker through ssh, so both nodes are measured the same way. It prints
# raw counters in ##sections; the arithmetic happens once, in python below.
node_snippet() {
  cat <<'SNIP'
CTR="$1"; WIN="$2"
echo "##host"; hostname; uname -r
echo "##lscpu"; lscpu -e=CPU,CORE,SOCKET,NODE,MAXMHZ,MINMHZ,ONLINE 2>/dev/null
echo "##lscpu_model"; lscpu 2>/dev/null | grep -E '^(Model name|CPU\(s\)|NUMA node\(s\)|L3 cache|Thread\(s\) per core)'
echo "##midr"; for c in /sys/devices/system/cpu/cpu[0-9]*; do n=${c##*cpu}; m=$(cat "$c/regs/identification/midr_el1" 2>/dev/null); cap=$(cat "$c/cpu_capacity" 2>/dev/null); echo "$n ${m:-?} ${cap:-?}"; done
echo "##containers"
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  docker inspect -f '{{.Name}} cpuset={{.HostConfig.CpusetCpus}} nanocpus={{.HostConfig.NanoCpus}} shares={{.HostConfig.CpuShares}} period={{.HostConfig.CpuPeriod}} quota={{.HostConfig.CpuQuota}} pid={{.State.Pid}}' "$c" 2>/dev/null
done
echo "##irq"; grep -E 'mlx5|roce|nvidia' /proc/interrupts 2>/dev/null | awk '{s=0; for(i=2;i<=NF;i++){ if($i ~ /^[0-9]+$/) s+=$i; else break } printf "%s %d %s\n", $1, s, $NF}' | sort -k2 -rn | head -12
echo "##loadavg"; cat /proc/loadavg
echo "##clk"; getconf CLK_TCK 2>/dev/null || echo 100
sample() { # sample TAG: /proc/stat per-core lines + per-process/thread jiffies of the engine container
  echo "##stat_$1"; grep -E '^cpu[0-9]+ ' /proc/stat
  echo "##procs_$1"
  for pid in $(docker top "$CTR" -eo pid 2>/dev/null | tail -n +2); do
    [ -r "/proc/$pid/stat" ] || continue
    awk -v pid="$pid" '{
      o = index($0, "("); c = index($0, ")"); comm = substr($0, o + 1, c - o - 1); gsub(/ /, "_", comm)
      n = split(substr($0, c + 2), f, " "); tid = $1
      # /proc/PID/stat aggregates every thread; /proc/PID/task/PID/stat is the
      # main thread alone, so the latter is skipped for the P line.
      if (tid == pid && FILENAME ~ /task/) next
      if (tid == pid) print "P", pid, comm, f[12] + f[13], f[22], f[37]
      else print "T", pid, tid, comm, f[12] + f[13], f[37]
    }' "/proc/$pid/stat" /proc/"$pid"/task/*/stat 2>/dev/null
  done
  echo "##affinity_$1"
  for pid in $(docker top "$CTR" -eo pid 2>/dev/null | tail -n +2); do
    a=$(grep -E '^Cpus_allowed_list' "/proc/$pid/status" 2>/dev/null | awk '{print $2}'); echo "$pid ${a:-?}"
  done
}
sample a; sleep "$WIN"; sample b
echo "##nccl_env"; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CTR" 2>/dev/null | grep -E '^(NCCL_SET_THREAD_NAME|NCCL_NET_GDR_LEVEL|NCCL_IB_HCA|NCCL_SOCKET_IFNAME|NCCL_DEBUG|VLLM_ALLREDUCE_USE_FLASHINFER|VLLM_USE_V2_MODEL_RUNNER)='
echo "##end"
SNIP
}

collect_node() { # collect_node head|worker -> raw text
  if [ "$1" = head ]; then
    node_snippet | sh -s -- "$HEAD_CTR" "$SECONDS_WINDOW"
  else
    node_snippet | ssh_worker sh -s -- "$WORKER_CTR" "$SECONDS_WINDOW"
  fi
}

# -------------------------------------------------------------------- gdr --
gdr_mode() {
  local image head_log worker_log
  image="${GDR_IMAGE:-$(docker inspect -f '{{.Config.Image}}' "$HEAD_CTR" 2>/dev/null || true)}"
  section "GPUDirect RDMA — evidence from the RUNNING ranks (NCCL_DEBUG=INFO lines)"
  head_log="$(docker logs "$HEAD_CTR" 2>&1 | grep -E 'NCCL INFO (NET/IB : GPU Direct RDMA|NET/IB : Using|Connected all rings|dlvsym failed|.*[Dd][Mm][Aa]-?[Bb][Uu][Ff]|.*peermem|Symmetric memory|NCCL version)' | sort -u || true)"
  worker_log="$(ssh_worker "docker logs $WORKER_CTR 2>&1 | grep -E 'NCCL INFO (NET/IB : GPU Direct RDMA|NET/IB : Using|Connected all rings|dlvsym failed|.*[Dd][Mm][Aa]-?[Bb][Uu][Ff]|.*peermem|Symmetric memory|NCCL version)' | sort -u" || true)"
  for node in head worker; do
    if [ "$node" = head ]; then lg="$head_log"; else lg="$worker_log"; fi
    if [ -z "$lg" ]; then check_warn "$node: no NCCL INFO lines in the retained log (NCCL_DEBUG=INFO not set, or the log rotated)"; continue; fi
    printf '%s\n' "$lg" | sed 's/^/    /'
    if printf '%s' "$lg" | grep -q 'GPU Direct RDMA Enabled'; then check_pass "$node: NCCL reports GPU Direct RDMA ENABLED"
    elif printf '%s' "$lg" | grep -q 'GPU Direct RDMA Disabled'; then check_warn "$node: NCCL reports GPU Direct RDMA DISABLED on every HCA (sends are staged through host memory)"
    else check_warn "$node: no 'GPU Direct RDMA' verdict line found"; fi
    printf '%s' "$lg" | grep -q 'use ring PXN 0 GDR 0' && log_dim "    $node: 'Connected all rings ... GDR 0' — no GDR on any channel"
    printf '%s' "$lg" | grep -q 'dlvsym failed on mlx5dv_reg_dmabuf_mr' && log_dim "    $node: the image's libmlx5 lacks mlx5dv_reg_dmabuf_mr (NCCL's data-direct path); ibv_reg_dmabuf_mr is the plain dmabuf path"
  done

  section "kernel side, both nodes"
  # The same POSIX snippet on both nodes (stdin-fed, so no quoting of $ in ssh).
  kernel_snippet() {
    cat <<'SNIP'
pm=$(lsmod | awk '$1=="nvidia_peermem"{print "loaded"}')
peers=$(find /sys/kernel/mm/memory_peers -mindepth 1 -maxdepth 1 -printf '%f,' 2>/dev/null)
ko=$(find "/lib/modules/$(uname -r)" -name 'nvidia-peermem.ko*' 2>/dev/null | head -1)
drv=$(cat /sys/module/nvidia/version 2>/dev/null)
topo=$(nvidia-smi topo -m 2>/dev/null | awk 'NR>1 && $1=="GPU0"{print $2"/"$3"/"$4"/"$5}')
echo "${pm:-not-loaded} ${peers:-none} ${ko:-absent} ${drv:-?} ${topo:-?}"
SNIP
  }
  for node in head worker; do
    if [ "$node" = head ]; then
      read -r pm peers ko drv topo < <(kernel_snippet | sh -s || echo "? ? ? ? ?")
    else
      read -r pm peers ko drv topo < <(kernel_snippet | ssh_worker sh -s || echo "? ? ? ? ?")
    fi
    log_info "$node: driver ${drv:-?}; nvidia_peermem ${pm:-not-loaded} (module file: ${ko:-absent}); /sys/kernel/mm/memory_peers: ${peers:-none}; GPU0->NIC0..3 distance: ${topo:-?}"
  done

  section "the image: rdma-core symbols NCCL looks up (${image:-no image})"
  if [ -n "$image" ]; then
    docker run --rm -i --entrypoint python3 "$image" - <<'PY' 2>/dev/null | sed 's/^/    /' || check_warn "could not run python3 inside $image"
import ctypes
for lib, syms in (("libibverbs.so.1", ["ibv_reg_dmabuf_mr", "ibv_reg_mr_iova2"]), ("libmlx5.so.1", ["mlx5dv_reg_dmabuf_mr", "mlx5dv_query_device"])):
    try:
        h = ctypes.CDLL(lib)
        for s in syms:
            print(f"{lib} {s}: {'present' if hasattr(h, s) else 'ABSENT'}")
    except OSError as exc:
        print(f"{lib}: load failed ({exc})")
PY
    docker run --rm --entrypoint sh "$image" -c 'dpkg-query -W libibverbs1 ibverbs-providers 2>/dev/null; strings /usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so.2 2>/dev/null | grep -E "^NCCL version [0-9]" | head -1' 2>/dev/null | sed 's/^/    /' || true
  fi

  section "the GPU: CUDA device attributes (from the pinned image, 1-second query; the decisive facts)"
  # cuDeviceGetAttribute is the same check NCCL's ncclIbDmaBufSupport/ncclGdrSupport run.
  docker run --rm -i --gpus all --entrypoint python3 "${image:-vllm/vllm-openai}" - <<'PY' 2>/dev/null | sed 's/^/    /' || check_warn "could not query CUDA device attributes (no --gpus in this docker, or the image has no cuda-python)"
from cuda.bindings import driver as cu
def chk(r):
    # cuda-python returns (err,) for void calls and (err, value) otherwise.
    if r[0] != cu.CUresult.CUDA_SUCCESS:
        raise RuntimeError(r[0])
    return r[1] if len(r) > 1 else None
chk(cu.cuInit(0)); dev = chk(cu.cuDeviceGet(0)); A = cu.CUdevice_attribute
for name in ("CU_DEVICE_ATTRIBUTE_INTEGRATED", "CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED", "CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED",
             "CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED", "CU_DEVICE_ATTRIBUTE_MULTICAST_SUPPORTED", "CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS"):
    print(name, chk(cu.cuDeviceGetAttribute(getattr(A, name), dev)))
print("driver", chk(cu.cuDriverGetVersion()))
PY

  section "what enabling GPUDirect RDMA would need (a separate, documented experiment — never combined with the GDN A/B)"
  cat <<'TXT'
    NCCL enables GDR for an HCA only when ONE of these registers GPU memory with the NIC:
      1. DMA-BUF: the CUDA driver must report CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED=1 for the GPU and
         libibverbs must have ibv_reg_dmabuf_mr (present in the image) — on the GB10 (integrated GPU,
         coherent LPDDR5X shared with the CPU) the 580.173.02 driver reports 0: nothing NCCL-side can change that;
         it needs a driver release that exports GB10 allocations as dma-bufs.
      2. nvidia_peermem: the module ships with the 580-open package but the driver also reports
         CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED=0 for this integrated GPU; loading it is an
         unsupported experiment and must not be tried on the production pair.
    Even with (1) or (2): NCCL_NET_GDR_LEVEL (default PXB) would still refuse GDR here because the GPU-NIC
    distance is NODE (nvidia-smi topo -m), so the experiment also sets NCCL_NET_GDR_LEVEL=SYS on BOTH ranks
    (byte-identical env via scripts/cluster-sync.sh), and NCCL_DEBUG=INFO must show
    'NET/IB : GPU Direct RDMA Enabled for HCA ...' on both ranks before any number is compared.
    Measure with scripts/cluster-ab.py (decode_burst and c10_short are the latency-bound all-reduce shapes)
    against a fresh A baseline, one variable at a time. What is lost today is one GPU->host copy per chunk on
    the same physical memory; the NCCL busbw already measured 171.6 Gb/s (97 % of NVIDIA's reference), so the
    upside is decode-step latency, not bandwidth.
TXT
  check_summary
}

if [ "$MODE" = gdr ]; then gdr_mode; exit $?; fi

# ---------------------------------------------------------------- measure --
section "sampling both nodes for ${SECONDS_WINDOW}s (per-core idle, per-process and per-thread CPU of the engine containers)"
PROBE_PIDS=""
if [ "$PROBE" = 1 ]; then
  # Eight decodes pinned at 800 tokens (ignore_eos) outlive a 5-10 s window
  # at ~70-85 tok/s each, so the rank, EngineCore and the NCCL proxy are busy
  # while both nodes are sampled. Synthetic prompt, nothing stored.
  model="$(curl -fsS -m 5 "$(api_url)/v1/models" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || true)"
  if [ -n "$model" ]; then
    body='{"model":"'"$model"'","messages":[{"role":"user","content":"Write a long essay about cluster scheduling."}],"max_tokens":800,"min_tokens":800,"ignore_eos":true,"temperature":0,"stream":true,"chat_template_kwargs":{"enable_thinking":false}}'
    for _ in 1 2 3 4 5 6 7 8; do
      curl -s -m 120 -o /dev/null -H 'Content-Type: application/json' -d "$body" "$(api_url)/v1/chat/completions" &
      PROBE_PIDS="$PROBE_PIDS $!"
    done
    sleep 1
    log_info "probe: 8 streamed 800-token completions in flight against $(api_url)"
  else
    check_warn "probe requested but $(api_url)/v1/models did not answer; sampling the engine as found"
  fi
fi
# Both nodes are sampled at the same time (the worker over ssh in the
# background) so their windows overlap — sequential sampling would measure
# the worker after the head's load had already drained.
tmp_head="$(mktemp)"; tmp_worker="$(mktemp)"
trap 'rm -f "$tmp_head" "$tmp_worker"' EXIT
collect_node worker > "$tmp_worker" 2>/dev/null & wpid=$!
collect_node head > "$tmp_head" || die "could not sample the head"
wait "$wpid" || check_warn "could not sample the worker over ssh ($CLUSTER_WORKER_SSH)"
head_raw="$(cat "$tmp_head")"; worker_raw="$(cat "$tmp_worker")"
worker_raw="${worker_raw:-##end}"
if [ -n "$PROBE_PIDS" ]; then
  # A space-separated pid list, split on purpose.
  # shellcheck disable=SC2086
  wait $PROBE_PIDS 2>/dev/null || true
fi

RECOMMEND=0; [ "$MODE" = recommend ] && RECOMMEND=1
export RECOMMEND JSON_OUT SECONDS_WINDOW HEAD_CTR WORKER_CTR
python3 - "$head_raw" "$worker_raw" <<'PY'
import json, os, re, sys

RECOMMEND = os.environ["RECOMMEND"] == "1"
WIN = float(os.environ["SECONDS_WINDOW"])
PART_NAMES = {0xd85: "Cortex-X925 (big)", 0xd87: "Cortex-A725 (LITTLE)", 0xd82: "Cortex-X4", 0xd81: "Cortex-A720", 0xd80: "Cortex-A520"}


def sections(raw):
    out, cur = {}, None
    for line in raw.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def parse_node(raw, ctr):
    s = sections(raw)
    node = {"container": ctr, "host": (s.get("host") or ["?"])[0], "kernel": (s.get("host") or ["?", "?"])[1] if len(s.get("host") or []) > 1 else "?"}
    # -- topology: MIDR part number is authoritative; lscpu MAXMHZ is the cross-check
    cores = {}
    for line in s.get("lscpu", [])[1:]:
        p = line.split()
        if len(p) >= 5 and p[0].isdigit():
            cores[int(p[0])] = {"core": int(p[1]), "socket": int(p[2]), "numa": int(p[3]), "max_mhz": float(p[4]) if p[4] != "-" else None}
    for line in s.get("midr", []):
        p = line.split()
        if len(p) >= 2 and p[0].isdigit():
            cpu = int(p[0])
            c = cores.setdefault(cpu, {})
            try:
                midr = int(p[1], 16)
                part = (midr >> 4) & 0xFFF
                c["midr"] = p[1]
                c["part"] = f"0x{part:x}"
                c["model"] = PART_NAMES.get(part, f"part 0x{part:x}")
            except ValueError:
                c["model"] = "?"
            c["capacity"] = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
    max_mhz = max((c.get("max_mhz") or 0) for c in cores.values()) if cores else 0
    for cpu, c in cores.items():
        by_midr = "big" if c.get("part") == "0xd85" else "little" if c.get("part") == "0xd87" else None
        by_mhz = "big" if c.get("max_mhz") and c["max_mhz"] >= max_mhz - 1 else "little"
        c["cls"] = by_midr or by_mhz
        c["cls_agree"] = by_midr is None or by_midr == by_mhz
    node["cores"] = cores
    node["big"] = sorted(cpu for cpu, c in cores.items() if c["cls"] == "big")
    node["little"] = sorted(cpu for cpu, c in cores.items() if c["cls"] == "little")
    node["model_lines"] = s.get("lscpu_model", [])
    # -- containers
    ctrs = []
    for line in s.get("containers", []):
        m = re.match(r"/?(\S+) cpuset=(\S*) nanocpus=(\d+) shares=(\d+) period=(\d+) quota=(-?\d+) pid=(\d+)", line)
        if m:
            ctrs.append({"name": m.group(1), "cpuset": m.group(2) or "(all)", "nanocpus": int(m.group(3)), "shares": int(m.group(4)),
                         "period": int(m.group(5)), "quota": int(m.group(6)), "pid": int(m.group(7))})
    node["containers"] = ctrs
    node["irq_top"] = s.get("irq", [])
    la = (s.get("loadavg") or [""])[0].split()
    node["loadavg"] = [float(x) for x in la[:3]] if len(la) >= 3 else None
    clk = int((s.get("clk") or ["100"])[0]) if (s.get("clk") or ["100"])[0].isdigit() else 100
    # -- per-core idle over the window
    def stat(tag):
        out = {}
        for line in s.get(f"stat_{tag}", []):
            p = line.split()
            vals = [int(x) for x in p[1:]]
            out[int(p[0][3:])] = (sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0))  # total, idle+iowait
        return out
    a, b = stat("a"), stat("b")
    idle = {}
    for cpu in a:
        if cpu in b and b[cpu][0] > a[cpu][0]:
            idle[cpu] = round(100.0 * (b[cpu][1] - a[cpu][1]) / (b[cpu][0] - a[cpu][0]), 1)
    node["idle_pct"] = idle
    # -- per-process / per-thread CPU over the window
    def procs(tag):
        P, T = {}, {}
        for line in s.get(f"procs_{tag}", []):
            p = line.split()
            if p[0] == "P" and len(p) >= 6:
                P[int(p[1])] = {"comm": p[2], "jiffies": int(p[3]), "rss_pages": int(p[4]), "cpu": int(p[5])}
            elif p[0] == "T" and len(p) >= 6:
                T[int(p[2])] = {"pid": int(p[1]), "comm": p[3], "jiffies": int(p[4]), "cpu": int(p[5])}
        return P, T
    Pa, Ta = procs("a")
    Pb, Tb = procs("b")
    proc_rows = []
    for pid, p in Pb.items():
        q = Pa.get(pid)
        pct = round((p["jiffies"] - q["jiffies"]) / clk / WIN * 100, 1) if q else None
        proc_rows.append({"pid": pid, "comm": p["comm"], "cpu_pct": pct, "rss_mb": round(p["rss_pages"] * 4096 / 2**20), "last_cpu": p["cpu"],
                          "threads": sum(1 for t in Tb.values() if t["pid"] == pid) + 1})
    thread_rows = []
    for tid, t in Tb.items():
        q = Ta.get(tid)
        if not q:
            continue
        pct = (t["jiffies"] - q["jiffies"]) / clk / WIN * 100
        if pct >= 2.0:
            thread_rows.append({"pid": t["pid"], "tid": tid, "comm": t["comm"], "cpu_pct": round(pct, 1), "last_cpu": t["cpu"],
                                "cls": cores.get(t["cpu"], {}).get("cls", "?")})
    thread_rows.sort(key=lambda r: -r["cpu_pct"])
    # NCCL proxy heuristic: the rank process's busiest non-main threads that
    # are not CUDA/gloo/zmq/watchdog threads.
    rank_pids = {r["pid"] for r in proc_rows if r["comm"].startswith("VLLM::Worker")}
    for r in thread_rows:
        named = r["comm"].lower()
        r["role"] = ("nccl_proxy?" if r["pid"] in rank_pids and not any(k in named for k in ("cuda", "gloo", "zmq", "watchd", "heartb", "tcpstore", "_tp"))
                     else "nccl_proxy" if "nccl" in named and "proxy" in named else "")
    for r in proc_rows:
        r["cls"] = cores.get(r["last_cpu"], {}).get("cls", "?")
    node["procs"] = sorted(proc_rows, key=lambda r: -(r["cpu_pct"] or 0))
    node["threads"] = thread_rows[:16]
    aff = {}
    for line in s.get("affinity_b", []):
        p = line.split()
        if len(p) == 2:
            aff[int(p[0])] = p[1]
    node["affinity"] = aff
    node["nccl_env"] = s.get("nccl_env", [])
    # -- the free-cores check: at least 4 cores with >= 50 % idle in the window
    free = sorted((cpu for cpu, v in idle.items() if v >= 50.0))
    least = sorted(idle.items(), key=lambda kv: kv[1])[:4]
    node["free_cores"] = free
    node["least_idle_4"] = least
    node["free_cores_ok"] = len(free) >= 4
    return node


head = parse_node(sys.argv[1], os.environ["HEAD_CTR"])
worker = parse_node(sys.argv[2], os.environ["WORKER_CTR"])
nodes = {"head": head, "worker": worker}


def fmt_cores(cpus):
    return ",".join(str(c) for c in cpus) if cpus else "-"


def fmt_ranges(cpus):
    cpus = sorted(cpus)
    out, start = [], None
    for i, c in enumerate(cpus):
        if start is None:
            start = c
        if i + 1 == len(cpus) or cpus[i + 1] != c + 1:
            out.append(f"{start}-{c}" if start != c else str(c))
            start = None
    return ",".join(out) if out else "-"


for name, n in nodes.items():
    if not n["cores"]:
        print(f"\n== {name}: no sample ==")
        continue
    print(f"\n== {name} ({n['host']}, {n['kernel']}) ==")
    for line in n["model_lines"]:
        print(f"    {line.strip()}")
    agree = all(c.get("cls_agree", True) for c in n["cores"].values())
    print(f"  big    (MIDR part 0xd85, Cortex-X925): cpus {fmt_ranges(n['big'])}  [{len(n['big'])}]")
    print(f"  LITTLE (MIDR part 0xd87, Cortex-A725): cpus {fmt_ranges(n['little'])}  [{len(n['little'])}]  MIDR/MAXMHZ agree: {agree}")
    print(f"  load average: {n['loadavg']}")
    print("  containers (HostConfig): name | cpuset | nanocpus | shares | quota/period")
    for c in n["containers"]:
        print(f"    {c['name']:42s} {c['cpuset']:8s} {c['nanocpus']:>12d} {c['shares']:>6d} {c['quota']}/{c['period']}")
    print("  engine processes over the window: comm | pid | cpu % | rss MB | threads | last cpu (class) | affinity")
    for p in n["procs"]:
        print(f"    {p['comm']:18s} {p['pid']:>8d} {p['cpu_pct'] if p['cpu_pct'] is not None else '?':>6} {p['rss_mb']:>7d} {p['threads']:>7d}   cpu{p['last_cpu']} ({p['cls']})  {n['affinity'].get(p['pid'], '?')}")
    print("  busiest threads (>= 2 %): comm | pid/tid | cpu % | last cpu (class) | role")
    for t in n["threads"]:
        print(f"    {t['comm']:18s} {t['pid']}/{t['tid']:<9d} {t['cpu_pct']:>6.1f}   cpu{t['last_cpu']} ({t['cls']})  {t['role']}")
    print("  NIC/GPU IRQs by count: " + "; ".join(n["irq_top"][:6]))
    idle_line = " ".join(f"cpu{c}:{n['idle_pct'][c]:.0f}%" for c in sorted(n["idle_pct"]))
    print(f"  idle % per core ({WIN:.0f}s): {idle_line}")
    least = ", ".join(f"cpu{c} {v:.0f}% ({n['cores'][c]['cls']})" for c, v in n["least_idle_4"])
    verdict = "PASS" if n["free_cores_ok"] else "WARN"
    print(f"  {verdict}  some cores must remain free: {len(n['free_cores'])} cores >= 50 % idle ({fmt_ranges(n['free_cores'])}); least idle: {least}")
    if n["nccl_env"]:
        print("  NCCL env on the engine: " + " ".join(n["nccl_env"]))

plan = None
if RECOMMEND:
    print("\n== proposed cpuset plan (PROPOSAL ONLY — nothing is applied; derived from the measured topology above) ==")
    plan = {}
    for name, n in nodes.items():
        if not n["cores"]:
            continue
        big, little = n["big"], n["little"]
        # The bigs split three ways: the rank's pollers (main thread,
        # EngineCore, NCCL proxy — three ~100 % spinners — plus the API server)
        # get six; the secondary vLLM engines on the node (router/embed/
        # reranker on the head, OCR on the worker: pollers too, but idle most
        # of the time) share two; two stay FREE for the kernel, the NIC
        # softirqs and ssh. Every other container lives on the LITTLE cores.
        rank_bigs, shared_bigs, reserve = big[:6], big[6:8], big[8:]
        if len(big) < 10:
            rank_bigs, shared_bigs, reserve = big[: max(1, len(big) - 2)], [], big[-2:] if len(big) >= 4 else []
        sidecar_words = ("watchdog", "sentinel", "controller", "whisper", "orchestrator", "postgres", "prometheus", "grafana",
                         "exporter", "cadvisor", "searxng", "cloudflared", "frontend", "sync-worker", "pgadmin", "blackbox")
        entries = []
        for c in n["containers"]:
            nm = c["name"]
            if nm == n["container"]:
                entries.append((nm, rank_bigs, "rank main thread + EngineCore + NCCL proxy: busy-pollers on big cores; API server rides along"))
            elif any(k in nm for k in sidecar_words):
                entries.append((nm, little, "sidecar: LITTLE cores only"))
            elif "vllm" in nm or "ocr" in nm:
                entries.append((nm, sorted(little + shared_bigs), "secondary vLLM engine: LITTLE cores plus two shared bigs for its own EngineCore/worker pollers"))
            else:
                entries.append((nm, little, "unclassified container: LITTLE cores (review)"))
        plan[name] = {"reserved_free": reserve, "rank": rank_bigs, "shared_engine_bigs": shared_bigs,
                      "entries": [{"container": e[0], "cpuset": fmt_ranges(e[1]), "why": e[2]} for e in entries]}
        print(f"\n  {name}: keep cpus {fmt_ranges(reserve)} (big) FREE for the kernel / NIC IRQs / ssh; rank on {fmt_ranges(rank_bigs)}; "
              f"secondary engines share {fmt_ranges(shared_bigs)} + LITTLE; sidecars on {fmt_ranges(little)}")
        for e in plan[name]["entries"]:
            print(f"    {e['container']:42s} cpuset {e['cpuset']:12s} {e['why']}")
    print("\n  How it would be applied (NOT done here): compose `cpuset: \"<list>\"` per service in the SRE-owned overlays for both nodes,")
    print("  rendered by ./techsara up under the engine lock (a cpuset change recreates the container = a pair restart); or, for a")
    print("  one-off measured trial only, `docker update --cpuset-cpus <list> <container>` on a sidecar (never on the engine pair).")
    print("  Measure before/after with scripts/cluster-ab.py (c10_short, decode_burst) — one variable at a time, never with the GDN A/B.")

out = {"generated_at_utc": os.environ.get("TS", ""), "window_s": WIN, "nodes": nodes, "plan": plan}
with open(os.environ["JSON_OUT"], "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1, default=str)
print(f"\nJSON: {os.environ['JSON_OUT']}")
PY
rc=$?
[ "$rc" = 0 ] || die "the measurement failed (python exit $rc)"
for node in head worker; do
  verdict="$(python3 -c 'import json,sys; n=json.load(open(sys.argv[1]))["nodes"][sys.argv[2]]; print("none" if not n.get("cores") else ("ok" if n["free_cores_ok"] else "warn"), len(n.get("free_cores", [])))' "$JSON_OUT" "$node" 2>/dev/null || echo "none 0")"
  case "$verdict" in
    "ok "*) check_pass "$node: ${verdict#ok } cores >= 50 % idle in the window (some cores remain free)" ;;
    "warn "*) check_warn "$node: only ${verdict#warn } cores >= 50 % idle in the window — fewer than 4 free cores" ;;
    *) check_warn "$node: not sampled" ;;
  esac
done
check_summary
