# Interconnect verification: adjudicating the GPUDirect, RoCE-version and throughput-gap claims

Two-node DGX Spark (GB10): `spark-0e68` (10.100.184.1 / 10.100.185.1) and `spark-476e`
(10.100.184.2 / 10.100.185.2). Branch `dev`.

**Method and constraints.** Everything below is read-only inspection — `lsmod`, `modinfo`,
`/proc/driver/nvidia`, `/sys/class/infiniband`, `/sys/class/net`, `/sys/bus/pci`, `ibv_devinfo`,
`ethtool`, `docker inspect`, `docker logs`, `nm -D`, and one `cuDeviceGetAttribute` query — plus
NVIDIA documentation. **No fabric benchmark was run**: the main model engine was reloading across
both nodes with NCCL initialising over the fabric, so `ib_write_bw` / `ib_send_bw` / `ib_read_bw`
and nccl-tests were all deliberately withheld. No service was restarted, no container stopped,
nothing reconfigured. There is no sudo on either node; nothing requiring root was attempted.

Every figure is labelled **MEASURED** (observed here, with the command) or **INFERRED**
(reasoning from those observations).

> **Note on how this document changed.** An earlier draft of this file concluded that Claim 1 was
> refuted — that GPUDirect RDMA was disabled only by a stale library in the model's container
> image. That conclusion was **wrong**, and it was overturned by a direct runtime query
> (`CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED = 0`) plus NVIDIA's own porting guide. The failed
> hypothesis is kept in §1.4 because knowing *why* it was wrong is load-bearing for the
> throughput question.

---

## Verdicts at a glance

| # | Claim | Verdict |
|---|---|---|
| 1 | "GPUDirect RDMA is not available on this platform" | **CONFIRMED** — by NVIDIA documentation *and* by direct runtime query. It is architectural and cannot be fixed here. |
| 2 | "The RoCE version explains the throughput gap" | **REFUTED** — the link is RoCE v2, and the 108.91 Gb/s measurement was itself taken on the RoCE v2 GID. |
| 3 | "108.91 Gb/s vs ~22 Gb/s shows a host-staging bottleneck" | **Comparison INVALID as constructed**, and the stated cause is **not supported**. The gap is real and *larger* than reported (~9x, not ~5x), but the absence of GPUDirect cannot explain it — NVIDIA's own healthy floor of 184 Gb/s is achieved *with* host-memory staging. |
| — | Stored note: "RoCE links cap at 13 Gb/s each" | **REFUTED — units error.** 13.3 was GB/s recorded as Gb/s (ratio 8.19). |
| — | "Both QSFP ports are cabled / dual-rail = two cables" | **REFUTED.** Only **one** QSFP port has carrier. The two "rails" are the two PCIe Gen5 x4 links feeding that *single* port — exactly as NVIDIA documents. |

---

## Platform baseline (all MEASURED)

```
$ uname -a
Linux spark-0e68 6.17.0-1031-nvidia #31-Ubuntu SMP PREEMPT_DYNAMIC ... aarch64
$ nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
NVIDIA GB10, 580.173.02, [N/A]          # [N/A] = unified memory, no discrete VRAM pool
$ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX Open Kernel Module for aarch64  580.173.02
```

```
$ for d in /sys/class/infiniband/*/; do echo "$(basename $d): $(cat $d/ports/1/state) \
    rate=$(cat $d/ports/1/rate) link_layer=$(cat $d/ports/1/link_layer)"; done
rocep1s0f0:   1: DOWN    rate=40 Gb/sec (4X QDR)   link_layer=Ethernet
rocep1s0f1:   4: ACTIVE  rate=200 Gb/sec (2X NDR)  link_layer=Ethernet
roceP2p1s0f0: 1: DOWN    rate=40 Gb/sec (4X QDR)   link_layer=Ethernet
roceP2p1s0f1: 4: ACTIVE  rate=200 Gb/sec (2X NDR)  link_layer=Ethernet

$ lspci -nn | grep -i mellanox     # all four are Mellanox MT2910 ConnectX-7 [15b3:1021]
0000:01:00.0  0000:01:00.1  0002:01:00.0  0002:01:00.1
$ ethtool enp1s0f1np1 | grep -E 'Speed|Port'
        Speed: 200000Mb/s
        Port: Direct Attach Copper
$ ip link show enp1s0f1np1
4: enp1s0f1np1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...
```

`ibv_devinfo -d rocep1s0f1 -v`: `vendor_part_id: 4129` (0x1021 = ConnectX-7),
`fw_ver: 28.45.4028`, `board_id: NVD0000000087`. Node 2 is identical on every line above.

---

## Topology correction: one cable, two PCIe rails — not two cables

This was assumed rather than checked, and it changes how every throughput number must be read.

```
$ for n in enp1s0f0np0 enp1s0f1np1 enP2p1s0f0np0 enP2p1s0f1np1; do
    echo "$n pci=$(basename $(readlink -f /sys/class/net/$n/device)) \
      phys_port_name=$(cat /sys/class/net/$n/phys_port_name) \
      switch_id=$(cat /sys/class/net/$n/phys_switch_id) \
      carrier=$(cat /sys/class/net/$n/carrier)"; done

enp1s0f0np0    pci=0000:01:00.0  phys_port_name=p0  switch_id=690e82000347bb4c  carrier=0
enp1s0f1np1    pci=0000:01:00.1  phys_port_name=p1  switch_id=690e82000347bb4c  carrier=1
enP2p1s0f0np0  pci=0002:01:00.0  phys_port_name=p0  switch_id=690e82000347bb4c  carrier=0
enP2p1s0f1np1  pci=0002:01:00.1  phys_port_name=p1  switch_id=690e82000347bb4c  carrier=1
```

**MEASURED:** all four interfaces share **one** `phys_switch_id` — a single ConnectX-7 ASIC.
They resolve to **two** physical ports, `p0` and `p1`. Both `p1` interfaces have **carrier=1**;
both `p0` interfaces have **carrier=0**. The two `p1` interfaces sit on **different PCI domains**
(0000 and 0002).

NVIDIA documents this exact arrangement in the DGX Spark User Guide, *ConnectX-7 Networking*:

> "The NIC connects independently to the two external QSFP ports, and it connects to the SoC
> through **two independent PCIe Gen 5 x4 links**. As a result, each QSFP port has two PCIe
> addresses […] **Each QSFP port appears as two independent Linux Ethernet interfaces.** As a
> result, plugging in two cables shows a total of four Linux Ethernet interfaces."
> — https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html

**INFERRED, with high confidence:** exactly **one QSFP cable is carrying the link** (port `p1`),
and what this project calls "dual rail" is the **two PCIe Gen5 x4 links serving that one 200 Gb/s
port**. If a second DAC is physically seated in port `p0`, it has no carrier on either node and is
contributing nothing.

This is not a defect. It is NVIDIA's supported topology, and a second cable is documented as
*useless*:

> "**Use only one cable per link.** This is true for both direct connections and switch
> connections. **Connecting two devices with two cables will not improve performance.**"
> — NVIDIA Sync User Guide, Cluster Assistant, https://docs.nvidia.com/sync/latest/cluster-assistant.html

> "**NOTE:** Full bandwidth can be achieved with just one QSFP cable. When two QSFP cables are
> connected, all four interfaces must be assigned IP addresses to obtain full bandwidth."
> — Connect Two Sparks playbook, https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks

The repo's configuration is therefore **correct**: both rails of the cabled port are addressed, on
separate /24s (10.100.184.0/24 and 10.100.185.0/24), matching NVIDIA's playbook, which likewise
splits the two logical interfaces of one port across two subnets.

### The per-rail ceiling

```
$ for d in /sys/bus/pci/devices/*/; do s=$(cat $d/current_link_speed 2>/dev/null); \
    [ -n "$s" ] && echo "$(basename $d) cur=$s x$(cat $d/current_link_width)"; done
0000:00:00.0 cur=32.0 GT/s PCIe x4     0002:00:00.0 cur=32.0 GT/s PCIe x4
0000:01:00.0 cur=32.0 GT/s PCIe x4     0002:01:00.0 cur=32.0 GT/s PCIe x4
0000:01:00.1 cur=32.0 GT/s PCIe x4     0002:01:00.1 cur=32.0 GT/s PCIe x4
```

**MEASURED:** PCIe Gen5 **x4** on every NIC function and both host bridges, both nodes —
independently confirming NVIDIA's "two independent PCIe Gen 5 x4 links".

**INFERRED (arithmetic):** Gen5 x4 = 32 GT/s x 4 = 128 Gb/s raw; after 128b/130b encoding,
**126.03 Gb/s (15.75 GB/s) per rail**. Two rails = ~252 Gb/s of PCIe behind a 200 Gb/s port, so
the **port** is the binding constraint for the pair, while a **single rail** is capped near
126 Gb/s. The reported 108.91 Gb/s single-rail result is **86.4% of one rail's PCIe ceiling** and
**~54% of the 200 Gb/s port** — i.e. it is exactly "one of the two halves of the port, running
well". Nothing is wrong with it.

It also disposes of an entry on `docs/CLUSTER.md`'s remediation list: **jumbo MTU is not what
caps RDMA.** 108.91 Gb/s was achieved *at MTU 1500* (measured 1500 on all four interfaces, both
nodes). MTU may matter for TCP; it is not the RDMA ceiling.

---

## Claim 1 — "GPUDirect RDMA is not available on this platform"

### VERDICT: **CONFIRMED.** Documented by NVIDIA, and confirmed by direct runtime query. Not fixable on this hardware.

### 1.1 Documentary evidence

NVIDIA **DGX Spark Porting Guide**, "CUDA → GPUDirect RDMA"
(https://docs.nvidia.com/dgx/dgx-spark-porting-guide/porting/cuda.html):

> "DGX Spark uses a unified memory architecture. On CUDA contexts the system memory returned by
> the pinned device memory allocators (e.g. `cudaMalloc`) **cannot be coherently accessed by the
> CPU complex nor by I/O peripherals like PCI Express devices** (e.g. the Mellanox NIC).
>
> Hence **the GPUDirect RDMA technology is not supported**, and the mechanisms for direct I/O
> based on that technology, for example **`nvidia-peermem`** (for DOCA-Host), **`dma-buf`** or
> **GDRCopy**, **do not work**.
>
> A compliant application should programmatically introspect the relevant platform capabilities,
> e.g. by querying `CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED` […] or
> `CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORT` […] and leverage an appropriate fallback.
>
> For example, for Linux RDMA applications based on the ib verbs library, we suggest to allocate
> the communication buffers with the **`cudaHostAlloc`** API and to register them with the
> **`ibv_reg_mr`** function."

Same text is the pinned answer in NVIDIA's **DGX Spark / GB10 FAQ**
(https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344), which NVIDIA staff cite when
users report `nvidia-peermem` failing to load on DGX Spark
(https://forums.developer.nvidia.com/t/gpu-direct-rdma-not-working-on-dgx-spark-systems-nvidia-peermem-module-fails-to-load/349837).

Corroborated by the **CUDA GPUDirect RDMA** documentation
(https://docs.nvidia.com/cuda/gpudirect-rdma/index.html), §2.1.2:

> `CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED` — **False**: "Set on **all Blackwell-based
> SoCs**…"

and §2.1.1: *"The nv-p2p APIs will no longer be supported on NVIDIA Tegra platforms starting with
Nvidia Blackwell-based SoCs."*

### 1.2 Runtime evidence — the query NVIDIA tells you to make

Run on the host via `ctypes` against `libcuda.so.1` (read-only, no fabric traffic):

```
$ python3 -c '<cuDeviceGetAttribute for each id>'
  attr 18  INTEGRATED                             = 1
  attr 41  UNIFIED_ADDRESSING                     = 1
  attr 83  MANAGED_MEMORY                         = 1
  attr 116 GPU_DIRECT_RDMA_SUPPORTED              = 0     <-- documented answer, confirmed
  attr 117 GPU_DIRECT_RDMA_FLUSH_WRITES_OPTIONS   = 1
  attr 118 GPU_DIRECT_RDMA_WRITES_ORDERING        = 100
  attr 124 DMA_BUF_SUPPORTED                      = 0     <-- dma-buf route also closed
```

**MEASURED, and decisive.** Both capability bits NVIDIA names are **0** on this GPU. This is the
platform telling us directly, in the exact terms the vendor documentation specifies.

### 1.3 Supporting runtime evidence

```
$ lsmod | grep -c peermem                   -> 0        (both nodes)
$ ls /sys/kernel/mm/memory_peers/           -> No such file or directory   (both nodes)
$ modinfo nvidia_peermem | head -2
filename: /lib/modules/6.17.0-1031-nvidia/kernel/nvidia-580-open/nvidia-peermem.ko
version:  580.173.02
```

`nvidia-peermem.ko` is *present on disk* — but that is only because it ships with the generic
aarch64 driver package, and NVIDIA's own forum thread is titled "nvidia-peermem Module Fails to
Load" for this platform. **Presence on disk is not evidence of support**, and loading it needs
root in any case.

NCCL's log agrees, on both HCAs:

```
$ docker logs sf-local-ai-vllm-1 2>&1 | grep -i 'GPU Direct RDMA'
spark-0e68:548:548 [0] NCCL INFO NET/IB : GPU Direct RDMA Disabled for HCA 0 'rocep1s0f1'
spark-0e68:548:548 [0] NCCL INFO NET/IB : GPU Direct RDMA Disabled for HCA 1 'roceP2p1s0f1'
```

### 1.4 A hypothesis that looked strong and is wrong — recorded deliberately

The same NCCL log contains this, immediately before the GDR lines:

```
spark-0e68:548:548 [0] NCCL INFO dlvsym failed on mlx5dv_reg_dmabuf_mr -
    /lib/aarch64-linux-gnu/libmlx5.so: undefined symbol: mlx5dv_reg_dmabuf_mr,
    version MLX5_1.25 version MLX5_1.25
```

and that is a genuine, reproducible library-version mismatch:

| Image | `libmlx5.so.1` | exports `mlx5dv_reg_dmabuf_mr`? |
|---|---|---|
| `vllm/vllm-openai` (**runs the main model**) | `1.22.39.0` (rdma-core 39) | **NO** |
| Host OS (Ubuntu 24.04, rdma-core 50.0) | `1.24.50.0` | **NO** |
| `nvcr.io/nvidia/vllm` (already on this box) | `1.25.59.1` (rdma-core 59) | **YES**, at `@@MLX5_1.25` |

It is tempting — and this document originally did conclude — that supplying the newer libmlx5
would enable GDR. **`CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED = 0` kills that hypothesis outright.**
Even with the symbol resolved, the dma-buf registration it unlocks is unsupported by the GPU, so
NCCL would fail at the next check instead of the current one. NVIDIA names `dma-buf` explicitly
among the things that "do not work" on DGX Spark.

Keep the observation, discard the conclusion: **the missing symbol is a symptom on the same dead
path, not a fix.** (It remains a real image-hygiene issue and may matter for other RDMA features,
but it is not a throughput lever.)

### 1.5 Is "GPUDirect RDMA" even the right frame? — partly, and the reason is counter-intuitive

GB10 has unified, coherent LPDDR5X, and `nvidia-smi` returns `[N/A]` for `memory.total` because
there is no separate VRAM pool. It is natural to assume that coherence makes GPUDirect
*unnecessary* — that the NIC can just DMA the shared DRAM.

**NVIDIA's stated reason is the opposite of that.** From the porting guide: *"For performance
reasons, specifically for CUDA contexts associated to the iGPU, the system memory returned by the
pinned device memory allocators (e.g. cudaMalloc) **cannot be coherently accessed** by the CPU
complex nor by I/O peripherals."* `cudaMalloc` memory on GB10 is physically system DRAM but is
mapped in a way the NIC cannot safely DMA.

So the honest framing is: the *phrase* "GPUDirect RDMA" describes a topology this machine does not
have, but the *substance* — "the NIC cannot DMA the model's CUDA buffers, so they must be staged
through separately-allocated host memory" — is **true, documented, and architectural**. NVIDIA's
prescribed fallback is precisely that: `cudaHostAlloc` + `ibv_reg_mr`. There is no GPUDirect to
enable here, no module to load, and no cable to add.

---

## Claim 2 — "the RoCE version explains the throughput gap"

### VERDICT: **REFUTED.**

```
$ for i in 0 1 2 3; do echo "gid[$i]=$(cat /sys/class/infiniband/rocep1s0f1/ports/1/gids/$i) \
    type=$(cat /sys/class/infiniband/rocep1s0f1/ports/1/gid_attrs/types/$i)"; done
gid[0]=fe80:...:4ebb:47ff:fe82:0e6a  type=IB/RoCE v1
gid[1]=fe80:...:4ebb:47ff:fe82:0e6a  type=RoCE v2
gid[2]=0000:...:ffff:0a64:b801       type=IB/RoCE v1
gid[3]=0000:...:ffff:0a64:b801       type=RoCE v2
```

`0a64:b801` is 10.100.184.1 — the rail-A address — so **GID index 3 is the IPv4 RoCE v2 GID**,
exactly as previously observed. **MEASURED.**

**The claim defeats itself.** The 108.91 Gb/s figure came from
`ib_write_bw -d rocep1s0f1 -x 3 -F --report_gbits -D 6`, and `-x 3` *selects that RoCE v2 GID*.
The fast number is itself a RoCE v2 number. A transport that carries 108.91 Gb/s cannot be why a
different workload gets 22 Gb/s on the same wire.

Structurally, RoCE v1 and v2 differ in **encapsulation and routability** — v1 is a raw L2
ethertype confined to one broadcast domain; v2 rides UDP/IPv4 port 4791 and is routable — not in
anything that would cost 5-10x on a point-to-point DAC cable with no router in the path. And
there is no router in this path: `ethtool` reports `Port: Direct Attach Copper` on both rails.

NCCL is on RoCE too, not a TCP fallback:
`NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE [RO]`.

**MEASURED:** GID 3 = RoCE v2 on both rails; direct-attach copper; NCCL selected RoCE on both
HCAs. **INFERRED:** RoCE version is irrelevant to the gap.

---

## Claim 3 — units reconciliation, and whether "host staging" is the explanation

### VERDICT: the comparison is **NOT apples-to-apples** (three uncontrolled variables). After correction the gap is **larger** — ~9x, not ~5x. But **"no GPUDirect ⇒ host staging ⇒ 22 Gb/s" is not supported**, because host-memory staging is precisely what NVIDIA's own 184 Gb/s healthy floor is measured with.

### 3.1 Both numbers really are in gigabits — verified from source, not assumed

The "22 Gb/s busbw" does **not** come from official nccl-tests. It comes from a repo-local
script, `scripts/lib/nccl_allreduce_bench.py`:

```python
algbw = (n * 4) / dt / 1e9                       # bytes / seconds / 1e9  ->  GB/s
busbw = algbw * 2 * (world - 1) / world          # world = 2  ->  x 1.0
res["results"].append({..., "algbw_GBs": round(algbw, 3),
                            "busbw_GBs": round(busbw, 3),
                            "busbw_Gbps": round(busbw * 8, 2)})
```

and its consumer, `scripts/cluster-test.sh:46`:

```python
print("%7.3f MB %8.3f ms %8.3f GB/s %8.2f Gb/s" % (r["size_MB"], r["time_ms"],
                                                   r["algbw_GBs"], r["busbw_Gbps"]))
```

The column labelled `Gb/s` reads **`busbw_Gbps`**, computed as `busbw * 8`. **So 22 Gb/s is
22 gigabits/s = 2.75 GB/s.** There is no hidden factor of 8 on the NCCL side. (Cross-check: the
same script's `p2p_64MB_Gbps` is also an explicit `* 8`, and `CLUSTER.md` quotes it as "26 Gb/s
64 MiB send/recv" — internally consistent.)

`--report_gbits` likewise switches perftest's BW column from MB/sec to Gb/s. **Both figures are
in the same unit.** The naive comparison is not wrong about units — it is wrong about three other
things.

### 3.2 The busbw convention is not deflating anything

For all-reduce, `busbw = algbw x 2(n-1)/n`. At **n = 2** that factor is `2 x 1 / 2 = 1.0`, so
**busbw == algbw exactly**, and the script implements exactly that.

The factor is right on first principles: a ring all-reduce over 2 ranks is a reduce-scatter (each
rank sends N/2) plus an all-gather (each rank sends N/2), so each rank puts **N bytes on the wire
in each direction** in time `t` — a per-direction wire rate of `N/t` = algbw = busbw. **busbw here
is the true per-direction bytes-on-the-wire rate, summed across every rail the rank uses.** The
convention neither inflates nor deflates the 22.

### 3.3 The three uncontrolled variables

| Variable | `ib_write_bw` run | NCCL all-reduce | Effect |
|---|---|---|---|
| **Rails** | **one** (`-d rocep1s0f1`) | **both** (log shows channels alternating `NET/IB/0` / `NET/IB/1`) | The capability side must be **doubled**, not the NCCL side halved. Comparing 1 rail to 2 **understated the gap ~2x**. |
| **Direction** | **unidirectional** | **bidirectional** (all-reduce sends and receives at once) | Never established that 108.91 survives two-way load. Needs `-b`. |
| **Memory** | **host memory** — the host `perftest 24.01` has no `--use_cuda` at all (verified on both nodes) | CUDA memory, staged via host buffers | The variable the original report drew its conclusion about **without measuring it**. |

Message size is *not* a rescuing confound: ib_write_bw ran at 64 KiB; the 22 Gb/s is quoted at
>= 4 MiB. Larger messages should *favour* NCCL. Size cannot explain the gap away.

### 3.4 The corrected comparison

| Quantity | Value | Basis |
|---|---|---|
| Per-rail unidirectional RDMA, host memory | 108.91 / 108.82 Gb/s | MEASURED (coordinator) |
| One rail's PCIe Gen5 x4 ceiling | 126.03 Gb/s | INFERRED from MEASURED link width |
| **Port rating (both rails together)** | **200 Gb/s** | NVIDIA docs (see below) |
| **NVIDIA's healthy floor for this link** | **184 Gb/s** | NVIDIA Sync link-speed test |
| NCCL busbw, per direction, both rails, CUDA memory | 22 Gb/s | MEASURED (prior run) |
| **NCCL as a fraction of NVIDIA's own pass floor** | **~12%** | 22 / 184 |
| What the original comparison implied | ~20% (22 / 108.91) | one rail vs two |

The correct statement is not "108.91 vs 22". It is:

> One of the port's two PCIe rails moves 108.91 Gb/s of host memory unidirectionally. NVIDIA
> considers the two rails together healthy at >= 184 Gb/s. NCCL's all-reduce moves 22 Gb/s per
> direction across those same two rails. **NCCL is achieving roughly an eighth to a tenth of the
> link's documented healthy throughput.**

### 3.5 Why "host staging" is not, by itself, the answer

This is the part the previous report got backwards, and it matters for what to fix.

GPUDirect RDMA **is** unavailable (Claim 1, confirmed), so NCCL **must** stage through host
memory. That much is true and unavoidable. But it does not follow that staging explains 22 Gb/s:

1. **NVIDIA's own 184 Gb/s floor is a host-memory number.** The Sync link-speed test and the
   documented `cudaHostAlloc` + `ibv_reg_mr` path are exactly "RDMA over host-registered buffers"
   — the same path NCCL is forced onto. NVIDIA expects that path to clear 184 Gb/s.
2. **The measured 108.91 Gb/s is itself a host-memory number** (no `--use_cuda` exists in the
   installed perftest). So host-memory RDMA on this box demonstrably runs at ~109 Gb/s per rail.
3. On GB10 the staging copy is DRAM-to-DRAM inside one coherent pool with 273 GB/s of memory
   bandwidth. A copy alone cannot cap anything at 2.75 GB/s.

**INFERRED:** the residual ~9x is therefore **above the verbs layer** — in how NCCL implements
staging, not in the fact of it. The log points at a concrete candidate:

```
spark-0e68:548:548 [0] NCCL INFO 64 coll channels, 64 collnet channels, 0 nvls channels,
    64 p2p channels, 2 p2p channels per peer
spark-0e68:548:826 [0] NCCL INFO [Proxy Progress] Device 0 CPU core 1
spark-0e68:548:818 [0] NCCL INFO [Proxy Service] Device 0 CPU core 0
```

**MEASURED:** 64 channels, and a **single `[Proxy Progress]` thread pinned to one core**. With GDR
off, every byte of all 64 channels is copied into and out of registered host buffers and posted to
the verbs queues by that one thread on one Arm core. **INFERRED (hypothesis, testable):** that
serialisation — not the existence of the copy — is the ceiling. This is what §C of the benchmark
plan is designed to separate.

Two configuration observations that may bear on it, both **MEASURED** from `docker inspect`:

* `NCCL_SOCKET_IFNAME=enp1s0f1np1` — bootstrap/OOB is pointed at a **RoCE data rail**. NVIDIA's
  playbooks set `NCCL_SOCKET_IFNAME=enP7s7`, the **management** 10 GbE interface, for this.
* `NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1` is set explicitly; NVIDIA's playbooks set no
  `NCCL_IB_HCA` and no `NCCL_IB_DISABLE`, expecting auto-discovery. The explicit list happens to
  name the two correct devices here, so this is likely harmless.

---

## The stored "13 Gb/s per link" note: refuted as a units error

`docs/CLUSTER.md` and `CHANGELOG.md` assert a hard **~13.3 Gb/s per link, per direction** RDMA
ceiling ("every variant", "4-8 QPs, 64 KiB-8 MiB"), plus **24.8 Gb/s bidirectional**. The project
memory note repeats it as "RoCE links cap at 13 Gb/s each".

**VERDICT: refuted — units, not a different test.** Five independent corroborations:

1. **The ratio is essentially exactly 8.** 108.91 / 13.3 = **8.19** — the bits-to-bytes factor.
   13.3 GB/s x 8 = **106.4 Gb/s**, within **2.3%** of 108.91 Gb/s.
2. **`ib_write_bw` prints MB/sec unless given `--report_gbits`.** Converting that column to "GB/s"
   and then writing the label "Gb/s" is precisely the mistake that yields 13.3.
3. **The bidirectional figure corroborates it.** 24.8 read as GB/s = 198.4 Gb/s aggregate
   ≈ 99 Gb/s per direction per rail — ~91% of the unidirectional 108.91, the mild degradation a
   full-duplex link shows under two-way load. Read as Gb/s, 24.8 would be an inexplicable 1.86x of
   a supposedly hard 13.3 Gb/s ceiling.
4. **PCIe corroborates the GB/s reading.** 13.3 GB/s = 106.4 Gb/s = 84% of the measured 126.03
   Gb/s Gen5 x4 rail. A literal 13.3 Gb/s would be 10.6% of that bus on a healthy link with clean
   counters — physically odd, and the doc itself calls it "Unexplained".
5. **NVIDIA's 184 Gb/s floor corroborates it.** A real 13.3 Gb/s per rail would be ~7% of what
   NVIDIA treats as pass/fail-healthy, and NVIDIA Sync would have flagged the link.

**Why the wrong number survived:** it landed next to `iperf3`'s genuine 14-16 Gb/s. iperf3 reports
in real Gbits/sec, and 14-16 Gb/s is entirely plausible for TCP at MTU 1500 on 20 Arm cores — TCP
is CPU-bound here in a way RDMA is not. Two numbers that agreed looked like corroboration, and
produced the conclusion that "the fabric is the limitation" and that only root-level fixes could
help. **That conclusion should be treated as withdrawn:** raw RDMA already reaches ~109 Gb/s per
rail at MTU 1500 with no root-level change.

Follow-ups for whoever owns those documents (**no source files were edited by this workstream**):

* `docs/CLUSTER.md` — the interconnect table, the "~13 Gb/s per-link ceiling" paragraph, and the
  "GPUDirect RDMA is disabled … expected, not a fault" line (that last one is *correct in
  conclusion*, and can now cite NVIDIA's porting guide instead of asserting it).
* `CHANGELOG.md` (~line 605) — "The fabric is the limitation … caps at ~13.3 Gb/s per link".
* `docs/VOICE.md:133` and `docs/CLUSTER.md:273,298` reason *from* the 13 Gb/s figure when arguing
  against expert parallelism and against putting activations on the link. **Those trade-off
  arguments were computed against a link ~8x faster than assumed and should be revisited.**
* The stored project memory note "RoCE links cap at 13 Gb/s each" should be corrected.

---

## What the two QSFP ports are rated at, and whether dual-rail aggregates

**From documentation:**

* **200 Gb/s per port, and the port is the cap.** DGX Spark User Guide, *ConnectX-7 Networking*:
  *"Each DGX Spark has two QSFP ports … **Each port provides up to 200 Gigabits per second
  (Gb/s)**, but the incoming speed is also determined by the cable … Using a cable with higher
  speed is not beneficial, because **the port itself is capped at 200 Gb/s**."*
  — https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
* Product page spec table lists **`ConnectX-7 NIC @ 200 Gbps`**, `128 GB LPDDR5x coherent unified
  system memory`, `273 GB/s` memory bandwidth —
  https://www.nvidia.com/en-us/products/workstations/dgx-spark/
* Hardware Overview lists **`2x QSFP Network connectors (ConnectX-7)`** —
  https://docs.nvidia.com/dgx/dgx-spark/hardware.html
* Approved cables are QSFP112 400G DAC (Amphenol NJAAKK-N911 / NJAAKK0006; Luxshare LMTQF022-SD-R).
* **NVIDIA's only published performance threshold** is from NVIDIA Sync's Cluster Assistant:
  *"NVIDIA Sync then runs a speed test across the links **to check the lower bound of 184
  Gbit/s**."* — https://docs.nvidia.com/sync/latest/cluster-assistant.html

**Does dual-rail aggregate?** Two different questions, with two different answers:

* **Two PCIe rails of one port: YES, and both are required.** *"Full bandwidth can be achieved
  with just one QSFP cable. When two QSFP cables are connected, all four interfaces must be
  assigned IP addresses to obtain full bandwidth."* Each rail is a Gen5 x4 link (~126 Gb/s), so
  both are needed to reach the port's 200 Gb/s. **This system is configured correctly** and NCCL
  is observably using both.
* **Two cables between two Sparks: NO.** *"Connecting two devices with two cables will not improve
  performance."* A second cable is not a throughput lever, and on this system port `p0` has no
  carrier anyway.

**Not documented anywhere (searched, not found):** any official NVIDIA expected NCCL
all-reduce/busbw figure for a two-Spark cluster. The playbooks show
`all_gather_perf -b 16G -e 16G -f 2` with **no expected numbers**. So there is no vendor baseline
to compare 22 Gb/s against other than the 184 Gbit/s link floor.

---

## Summary: measured vs inferred

**MEASURED (read-only, reproducible from the commands above; both nodes unless noted)**
1. GB10, driver 580.173.02, unified memory (`memory.total = [N/A]`), `INTEGRATED = 1`.
2. **`CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED = 0`** and **`DMA_BUF_SUPPORTED = 0`**.
3. `nvidia_peermem` ships for this kernel but is **not loaded**; `/sys/kernel/mm/memory_peers/`
   absent.
4. NCCL 2.30.7 logs "GPU Direct RDMA Disabled" on both HCAs, preceded by a `dlvsym` failure on
   `mlx5dv_reg_dmabuf_mr` @ `MLX5_1.25`.
5. Model image ships libmlx5 **1.22.39.0** (no symbol); an NGC image on the same box ships
   **1.25.59.1** (has it) — irrelevant to GDR given (2), but a real image-hygiene gap.
6. One ConnectX-7 ASIC (single `phys_switch_id`), two physical ports; **only port `p1` has
   carrier**; its two interfaces are on independent PCI domains.
7. PCIe Gen5 **x4** on every NIC function and both host bridges.
8. Active ports negotiate **200 Gb/s (2X NDR)**, `Direct Attach Copper`; MTU **1500** on all four.
9. GID index 3 = **RoCE v2**, IPv4 10.100.184.1; NCCL selected RoCE on both HCAs.
10. NCCL: 64 channels alternating both rails, **one `[Proxy Progress]` thread on one core**.
11. `NCCL_SOCKET_IFNAME` points at a RoCE data rail, not the management NIC.
12. The "22 Gb/s busbw" is `busbw_Gbps` = `busbw_GBs * 8` — genuinely gigabits.
13. For n=2, `busbw = algbw x 2(n-1)/n = algbw`; the convention adds nothing.
14. Installed `perftest 24.01` has **no** `--use_cuda`; the NGC image carries the full official
    `nccl-tests` suite (`/usr/local/bin/all_reduce_perf`, …).

**INFERRED (reasoning, not observation)**
1. PCIe Gen5 x4 => 126.03 Gb/s per rail; 108.91 Gb/s = 86.4% of one rail, ~54% of the port.
2. Exactly one QSFP cable is carrying the link; "dual rail" = the two PCIe links of that one port.
3. The "13.3 Gb/s" figure is 13.3 GB/s mislabelled (ratio 8.19; five corroborations).
4. Corrected NCCL efficiency is ~12% of NVIDIA's 184 Gb/s floor, not ~20% of one rail.
5. The residual gap lies **above the verbs layer** — most likely NCCL's single proxy-progress
   thread staging 64 channels. **Hypothesis, not a measurement.**
6. The libmlx5 upgrade would **not** enable GDR (killed by `DMA_BUF_SUPPORTED = 0`).

**REQUIRES ROOT (flagged, not attempted)**
* `ethtool -m` transceiver readout (`Operation not permitted`) — would confirm cable seating.
* `modprobe nvidia_peermem` — **pointless here**; NVIDIA documents it as non-functional on Spark.
* MTU 9000 (would help TCP; **demonstrably not needed for RDMA**), `mlxconfig` / `mlxlink`, SMMU
  kernel command-line changes.

---

## Proposed benchmark plan — NOT YET RUN, awaiting approval

No fabric traffic has been generated by this workstream. Each test controls exactly one of the
variables identified above. **Run only once the engine reload has completed on both nodes** — all
of these contend with NCCL for the same link.

### A. Establish the capability side properly (host memory, message-size sweep)

```bash
# A1 - per-rail sweep. -a sweeps 2 B .. 8 MiB; -q 4 uses 4 QPs.
# node 2 (server)                              # node 1 (client)
ib_write_bw -d rocep1s0f1 -x 3 -F \            ib_write_bw -d rocep1s0f1 -x 3 -F \
  --report_gbits -a -q 4                         --report_gbits -a -q 4 10.100.184.2
# repeat with -d roceP2p1s0f1 against 10.100.185.2

# A2 - BIDIRECTIONAL: does 108.91 survive two-way load? (this is what all-reduce does)
ib_write_bw -d rocep1s0f1 -x 3 -F --report_gbits -b -s 65536 -D 10 -q 4 [10.100.184.2]

# A3 - BOTH RAILS AT ONCE: do the two PCIe rails really sum to the 200 Gb/s port?
#      launch simultaneously, distinct -p ports
ib_write_bw -d rocep1s0f1   -x 3 -F --report_gbits -s 1048576 -D 10 -q 4 -p 18515 [10.100.184.2]
ib_write_bw -d roceP2p1s0f1 -x 3 -F --report_gbits -s 1048576 -D 10 -q 4 -p 18516 [10.100.185.2]

# A4 - confirm the ceiling is not write-specific
ib_read_bw -d rocep1s0f1 -x 3 -F --report_gbits -a -q 4 [10.100.184.2]
ib_send_bw -d rocep1s0f1 -x 3 -F --report_gbits -a -q 4 [10.100.184.2]
```

**Expected if the analysis is right:** each rail plateaus at 105-115 Gb/s from ~64 KiB up; A3
totals ~184-200 Gb/s, matching NVIDIA's floor. **A3 is the number that replaces the bogus
"13 Gb/s per link" in the docs.**

### B. Re-measure NCCL with the official tool, in the same units

`all_reduce_perf` is already present in `nvcr.io/nvidia/vllm`. Official nccl-tests prints
**`algbw` and `busbw` in GB/s** — record both columns verbatim and multiply by 8 before comparing
with any `--report_gbits` figure, so the unit mistake is not repeated.

```bash
# one rank per node, host networking, NCCL_DEBUG on
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH \
NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1 NCCL_IB_DISABLE=0 \
NCCL_SOCKET_IFNAME=enP7s7 \
  all_reduce_perf -b 8 -e 512M -f 2 -g 1
```

Note `NCCL_SOCKET_IFNAME=enP7s7` — the management NIC, per NVIDIA's playbooks — rather than the
RoCE rail currently configured. Also run `sendrecv_perf` with the same sweep: it isolates raw
point-to-point transport from the collective algorithm.

### C. THE DECISIVE TEST — locate the loss above the verbs layer

Given GDR is architecturally unavailable, the only remaining question is whether NCCL's staging
implementation or its channel/proxy configuration is the ceiling. Run B's command with one change
at a time:

```bash
NCCL_MIN_NCHANNELS=8  NCCL_MAX_NCHANNELS=8    # 64 channels -> 8, same single proxy thread
NCCL_MIN_NCHANNELS=2  NCCL_MAX_NCHANNELS=2    # further
NCCL_IB_QPS_PER_CONNECTION=4                  # more QPs per connection
NCCL_BUFFSIZE=8388608                         # larger staging buffers (default 4 MiB)
NCCL_NET_GDR_LEVEL=SYS                        # expected to change nothing; confirms GDR is dead
NCCL_P2P_NET_CHUNKSIZE=1048576                # larger per-chunk transfers
```

**How to read C:** if reducing channel count or raising `NCCL_BUFFSIZE` moves busbw substantially,
the bottleneck is the proxy/staging configuration and is **tunable without root**. If nothing
moves and busbw stays ~22 Gb/s while A3 shows ~184-200 Gb/s on the same wire, the loss is inside
NCCL's host-staging path on this platform, and the honest answer becomes "a known cost of GB10's
no-GPUDirect architecture, quantified at ~8x" — which is a legitimate finding to report upstream
rather than something to fix locally.

### Not runnable as specified
Direct GPU-memory RDMA (`ib_write_bw --use_cuda`) would isolate the memory variable outright, but
`perftest 24.01` on both hosts lacks CUDA support and no CUDA-enabled perftest was found in any
image here. Given `DMA_BUF_SUPPORTED = 0` it would also be expected to fail registration, so it is
**not worth building**. B and C achieve the needed discrimination.

### Safety notes for approval
* Every test generates real fabric traffic and contends with NCCL — run only with the engine idle.
* **None** restarts a service, changes configuration, or needs root.
* B and C run in *new, ephemeral* containers (`docker run --rm`), never by restarting the running
  model containers.
