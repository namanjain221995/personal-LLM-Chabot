# Two-node DGX Spark cluster (`CLUSTER_MODE=dual`)

Added 2026-08-25. This page is the operator reference for running the main
model across **both** DGX Sparks. Everything below was measured on the two
machines it describes; numbers are from `.runtime/logs/cluster-bench-*.txt`,
`scripts/cluster-test.sh` and `perftest`, not estimates.

## What it is (and is not)

Each DGX Spark has one NVIDIA GB10 with 128 GB of *unified* memory. Linux on
Node 1 never sees Node 2's GPU, and the two memories are never one address
space. What dual mode does is **tensor-parallel sharding**: vLLM's multi-node
`mp` executor runs one worker process per node, each holding half of every
weight matrix (11.35 GiB per node for the current 35B-A3B; the 27B was 10.6 GiB
instead of 20.8 GiB) and half of every
attention/KV head. Every forward pass is computed by both GB10s, and the halves
are combined with NCCL all-reduces over the two direct 200G RoCE links.

```text
                       USERS ──► frontend :3000 ──► orchestrator :8080
                                                          │
                                        OPENAI_BASE_URL=http://vllm:8000/v1
                                                          │  (extra_hosts vllm → host-gateway)
                                                          ▼
   NODE 1  spark-0e68 (head)                 NODE 2  spark-476e (worker)
   ┌────────────────────────────────┐        ┌────────────────────────────────┐
   │ vllm  (node-rank 0, host net)  │        │ vllm-worker (node-rank 1,      │
   │   API server :8000             │        │   --headless, host net)        │
   │   Worker_TP0 ─ GB10 ─ ½ weights│◄──────►│   Worker_TP1 ─ GB10 ─ ½ weights│
   │ vllm-router, vllm-embed,       │ NCCL   │ project: sf-local-ai-worker     │
   │ vllm-reranker, vllm-ocr,       │ RoCE   │ (nothing else from the stack)   │
   │ postgres, searxng, pgadmin,    │ ×2     └────────────────────────────────┘
   │ orchestrator, sync-worker      │
   └────────────────────────────────┘
        10.100.184.1 ═══ link A (rocep1s0f1,  enp1s0f1np1)  ═══ 10.100.184.2
        10.100.185.1 ═══ link B (roceP2p1s0f1, enP2p1s0f1np1) ═══ 10.100.185.2
        192.168.9.54 ─── 1 GbE LAN (never carries cluster traffic) ─── 192.168.9.68
```

Only the main model is distributed. Router, embeddings, reranker, OCR, the
database and the application stay exactly where they were (Node 1), and the
application keeps using the single endpoint it always used.

## Why tensor parallel, and why not pipeline parallel

* vLLM `0.26.1rc1` (the pinned `vllm/vllm-openai@sha256:24f2f897…` image) has a
  native multi-node multiprocessing executor (`--nnodes 2 --node-rank N
  --master-addr … --master-port …`, worker with `--headless`). Ray is not
  installed in the image and is not needed.
* `--pipeline-parallel-size 2` was tried on 2026-08-25 and rejected by vLLM:
  `Pipeline parallelism is not supported for this model. Supported models
  implement the SupportsPP interface` — the multimodal
  `Qwen3_5ForConditionalGeneration` class used by the previous main model
  (`RadixArk/Qwen3.8-27B-NVFP4`) does not implement it (only the text-only
  `Qwen3_5ForCausalLM` does). The current main model's
  `Qwen3_5MoeForConditionalGeneration` class is the MoE sibling of that class;
  PP was not re-tried with it. TP=2 is the two-GPU layout this runtime executes.
* TP=2 is also the layout that helps a chatbot most on this hardware: decode is
  memory-bandwidth-bound, and each node now streams half the weights per
  token. Measured with the dense 27B, single-stream decode went from **20 tok/s to 24–27 tok/s**
  (the first baseline in this work counted SSE chunks, which MTP packs two
  tokens into, and understated single-node decode by half; the number here
  counts tokens from `usage`).
* The model shards cleanly. `nvidia/Qwen3.6-35B-A3B-NVFP4` (switched to on
  2026-08-29): 40 layers (30 gated-delta-net + 10 full attention), 16 attention
  heads / 2 KV heads (head_dim 256) / 32 GDN value heads / 16 GDN key heads /
  256 routed experts (8 active) + 1 shared expert, MoE intermediate 512, vocab
  248320 — all even, and the NVFP4 block size (16) divides every per-rank
  dimension. vLLM picks the Marlin NVFP4 MoE kernel itself (`Using 'MARLIN'
  NvFp4 MoE backend`); passing `--moe-backend marlin` explicitly is rejected by
  the pinned build (`not supported for unquantized MoE`), so the manifest no
  longer carries it. The previous main model (`RadixArk/Qwen3.8-27B-NVFP4`,
  dense, 64 layers = 48 GDN + 16 full attention, 4 KV heads) sharded the same way.

## The interconnect, measured

| Test | Result |
|---|---|
| Link speed reported | 200 Gb/s (2X NDR), ConnectX‑7, PCIe Gen5 x4, FEC RS, MTU 1500 both ends |
| `ib_write_bw` / `ib_read_bw` / `ib_send_bw` (rdma_cm or explicit RoCE v2 GID, 4–8 QPs, 64 KiB–8 MiB) | **~13.3 Gb/s per link, per direction**, every variant; 24.8 Gb/s bidirectional; zero errors, retransmits or pause storms |
| `iperf3` TCP (1 or 8 streams) | 14–16 Gb/s per link |
| NCCL 2.30.7 all-reduce inside the vLLM image, **both HCAs** (`NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1`) | **22 Gb/s bus bandwidth** (≥4 MiB), 17.6 µs 4-byte latency, 26 Gb/s 64 MiB send/recv |
| NCCL, single HCA | 10–12 Gb/s, 13.9 µs |
| NCCL, TCP sockets (`NCCL_IB_DISABLE=1`, both interfaces) | 14.6 Gb/s at 512 MiB but 1.3 Gb/s at 64 KiB, 35.9 µs latency |

Both links are therefore used (NCCL channels alternate `NET/IB/0` and
`NET/IB/1`; `scripts/cluster-status.sh` prints the line), RDMA is roughly 2×
sockets on latency and small messages, and multi-rail is roughly 2× single
rail. What is **not** explained is the ~13 Gb/s per-link ceiling: it is the
same for RDMA and TCP, on both independent links, with clean counters and a
healthy PCIe link. Everything that could fix it needs root (neither node has
passwordless sudo): jumbo MTU, `mlxconfig`/`mlxlink` port diagnostics, the
SMMU (`iommu.passthrough=0`, DMA-FQ) on the kernel command line, IRQ/RPS
affinity. See *Limitations*. GPUDirect RDMA is disabled by NCCL on GB10
(unified memory) — expected, not a fault.

## Memory

`--gpu-memory-utilization 0.30` per node is the ceiling vLLM enforces (of the
121 GB pool each GB10 reports), and `--kv-cache-memory-bytes` (16 GiB per
node, `CLUSTER_KV_CACHE_MEMORY_GIB`) fixes the KV budget explicitly: weights
11.35 GiB + CUDA graphs 2.62 GiB + KV 16 GiB + activations ≈ 31 GiB per node
for the 35B-A3B, ≈ 2.98M tokens of fp8 KV per node (27B: weights 10.6 GiB +
graphs 1.3 GiB, ≈ 950k tokens; single-node 27B: 18.6 GiB / 542k tokens at
0.35). Why explicit rather than profiled: on GB10 "free GPU memory" is free
*system* memory, and it moves while vLLM profiles (page cache from the 22 GB
weight read is released, other containers breathe). vLLM asserts that free
memory did not *grow* during profiling (`Error in memory profiling. Initial
free memory 65.73 GiB, current free memory 89.74 GiB`, seen on the worker on
2026-08-25, killing the whole start-up) — with an explicit KV size it skips
that profiling path entirely and start-up becomes deterministic. The other
0.70 stays for router (0.17), OCR (0.14), embed/reranker (0.04 each), the
application and the OS on Node 1; Node 2 only carries the worker (≈ 30 GB)
next to whatever else runs there.

## Configuration (`.env`, user-owned)

Nothing is required: with `CLUSTER_MODE` unset (= `auto`) the launcher looks
for a second DGX Spark on the direct links every time it starts, and falls
back to the single-node layout - with a printed reason - when it finds none
or cannot reach it over ssh. Everything below is an override.

```text
CLUSTER_MODE=auto                     # auto (default) | single | dual
CLUSTER_HEAD_IP=10.100.184.1          # optional: pin link A instead of discovering it
CLUSTER_WORKER_IP=10.100.184.2        #   (discovery: RoCE interfaces with an address, then the
CLUSTER_HEAD_IP_2=10.100.185.1        #    neighbour table / the .1<->.2 convention, then TCP/22)
CLUSTER_WORKER_IP_2=10.100.185.2
CLUSTER_WORKER_SSH=techsphere@10.100.184.2   # optional; default <your user>@<discovered ip>, key auth only
CLUSTER_TENSOR_PARALLEL_SIZE=2        # TP × PP must be 2
CLUSTER_PIPELINE_PARALLEL_SIZE=1
CLUSTER_GPU_MEMORY_UTILIZATION=0.30
CLUSTER_KV_CACHE_MEMORY_GIB=16        # explicit per-node KV budget (see Memory: why not profiled)
# MAIN_MODEL_MAX_LEN=800000           # served window; above the model's native 262,144 the
                                      # launcher auto-enables YaRN and refuses windows this
                                      # KV budget cannot hold (35B-A3B: the 4x-native ceiling,
                                      # 1,048,576; the 27B capped at ~922,000 at 16 GiB)
CLUSTER_MASTER_PORT=29501             # torch.distributed rendezvous on the head
# optional: CLUSTER_NCCL_SOCKET_IFNAME, CLUSTER_NCCL_IB_HCA, CLUSTER_NCCL_DEBUG,
#           CLUSTER_SPECULATIVE_CONFIG, CLUSTER_MAX_NUM_BATCHED_TOKENS,
#           CLUSTER_WORKER_MODEL_CACHE, CLUSTER_WORKER_SSH_OPTS
```

The only manual prerequisite for dual mode is ssh key authentication from
Node 1 to Node 2 (the launcher never uses passwords); on a Mac, a single Spark
or any other profile the cluster code never runs.

The launcher validates these on `./techsara up` and generates the rest into
`.runtime/generated.env`: interface/HCA names detected from the IPs
(`CLUSTER_NCCL_SOCKET_IFNAME=enp1s0f1np1`,
`CLUSTER_NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1`), the API bind address, and one
shared engine-argument string `CLUSTER_ENGINE_ARGS` that **both** nodes
interpolate, so the two vLLM processes can never disagree on the model
configuration. `OPENAI_BASE_URL`/`VISION_BASE_URL` become
`http://vllm:${VLLM_PORT}/v1`.

Files involved:

| File | Runs on | Role |
|---|---|---|
| `compose/compose.cluster-dgx-spark.yaml` | Node 1 (layered last by the launcher) | turns service `vllm` into node-rank 0: host networking, `/dev/infiniband`, unlimited memlock, NCCL env; `extra_hosts vllm:host-gateway` on orchestrator and sync-worker |
| `compose/compose.cluster-worker.yaml` | Node 2, project `sf-local-ai-worker`, from `~/.techsara-cluster/` | node-rank 1, `--headless`; same image digest, same model path, own cache volume |
| `scripts/cluster-*.sh`, `scripts/lib/` | Node 1 | operations (below) |
| `launcher/techsara_cli/cluster.py` | Node 1 | validation, detection, `CLUSTER_ENGINE_ARGS` |

Why host networking: NCCL over RoCE needs the process to own the ConnectX‑7
netdevs (RoCE v2 GIDs are derived from them) and vLLM's cross-node message
queue must be reachable at the link address; a bridged container has neither.
The consequence is that the head's API listens on the host at
`CLUSTER_API_BIND_ADDRESS:VLLM_PORT` — `0.0.0.0` when `PUBLISH_MODEL_PORTS=true`
(what this deployment already used), otherwise the Docker bridge gateway
(`172.17.0.1`), which only containers on Node 1 can reach. The orchestrator and
sync-worker resolve the name `vllm` to that gateway via `extra_hosts`, which is
why no application code changed.

## Operations

```bash
./techsara up                    # THE command: detects the pair, syncs the worker host, starts worker + head + stack
./techsara down                  # stops Node 1 and, in dual mode, the worker (volumes kept)
./techsara status                # includes the cluster line and the auto-detection reason
scripts/cluster-status.sh --probe   # the report below, plus a live request with GPU sampling on both nodes
scripts/cluster-logs.sh head|worker|nccl|all [-f] [--tail N]
scripts/cluster-doctor.sh [--rdma] [--nccl]   # preflight: links, RDMA, MTU, ssh, docker/GPU, image, model, ports, firewall, memory
scripts/cluster-test.sh [--socket]            # two-node NCCL all-reduce inside the vLLM image (validated data + transport)
scripts/cluster-bench.sh [--concurrency N ...] # vllm bench serve against the API, with per-node GPU sampling
scripts/cluster-sync.sh          # image/model/env to the worker (idempotent; `up` runs it)
scripts/cluster-worker.sh start|stop|down|restart|status|logs
scripts/cluster-up.sh / cluster-down.sh       # aliases of ./techsara up / down + status report
```

In dual mode `up` runs `scripts/cluster-sync.sh` and `scripts/cluster-worker.sh
start` right before its `vllm` stage (the worker must be waiting when the head
rendezvous starts), prints the head/worker line, and a failure there points at
`cluster-status.sh` instead of the single-node context-lowering retry (which
cannot fix a missing worker). Both containers use `restart: unless-stopped` and wait up
to `--distributed-timeout-seconds 300` for each other, so after a reboot of
either machine the pair re-rendezvous on its own.

Example `scripts/cluster-status.sh --probe` output (2026-08-29, 35B-A3B):

```text
========================================
DGX CLUSTER STATUS   (2026-08-29T00:20:04+05:30)
========================================
Mode: dual   TP=2 PP=1   gpu-mem-util=0.30
Node 1 (head, 10.100.184.1)
  Reachable: YES (spark-0e68)
  GPU: NVIDIA GB10
  RDMA link A: ACTIVE  (enp1s0f1np1 10.100.184.1 hca=rocep1s0f1 mtu=1500)
  RDMA link B: ACTIVE  (enP2p1s0f1np1 10.100.185.1 hca=roceP2p1s0f1 mtu=1500)
Node 2 (worker, 10.100.184.2)
  Reachable: YES (spark-476e)
  GPU: NVIDIA GB10
  RDMA link A: ACTIVE  (enp1s0f1np1 10.100.184.2 hca=rocep1s0f1 mtu=1500)
  RDMA link B: ACTIVE  (enP2p1s0f1np1 10.100.185.2 hca=roceP2p1s0f1 mtu=1500)
Distributed runtime
  Head (vllm, node-rank 0): running / healthy
  Worker (vllm-worker, node-rank 1): running / healthy
vLLM
  Model: Qwen/Qwen3.6-35B-A3B-NVFP4  (max_model_len 800000)
  Engine: tensor_parallel=2 pipeline_parallel=1 world_size=2
  Per-node GPU KV cache size: 2,977,319 tokens
  Distributed GPUs: 2
NCCL
  Transport: RDMA/RoCE (2 HCA(s), 256 channel endpoints)
  NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE [RO]; OOB enp1s0f1np1:10.100.184.1<0>
Probe (streaming chat completion + GPU sampling on both nodes)
  TTFT: 0.069 s   completion_tokens: 160   decode: 76.2 tok/s   total: 2.17 s
  Node 1 GB10 during probe: max 81% avg 48% (9 samples)
  Node 2 GB10 during probe: max 76% avg 34% (9 samples)
  Both GPUs participating: YES
========================================
12 passed, 0 warnings, 0 failed
```

## Performance: single node vs dual node

Same model, same flags, `vllm bench serve` with random 512-token prompts and
128 generated tokens (`--ignore-eos`), plus a real 200-token chat request.

| Metric | 27B single node (TP=1, util 0.35) | 27B dual node TP=2 (util 0.30 per node) | **35B-A3B dual node TP=2 (current, 2026-08-29)** |
|---|---|---|---|
| Single request: TTFT (short prompt) | 0.16–0.20 s | 0.17–0.30 s | **0.07 s** |
| Single request: decode speed (tokens, from `usage`) | **20.0–20.1 tok/s** | **24.0–26.8 tok/s** (1.2–1.35×) | **76.2–80.6 tok/s** |
| Concurrency 4 (16 req): output tok/s | 54.0 | 53.4 | **135.4** |
| Concurrency 4: TPOT mean / TTFT p50 / p95 | 66.0 ms / 718 ms / 1501 ms | 65.8 ms / 737 ms / 1778 ms | 23.9 ms / 589 ms / 1590 ms |
| Concurrency 16 (64 req): output tok/s | **123.1** (666 total tok/s)¹ | 105.5 (570 total tok/s) | **243.3** |
| Concurrency 16: TPOT mean / TTFT p50 / p95 | 106.2 ms / 1.33 s / 6.33 s¹ | 126.8 ms / 1.61 s / 7.46 s | 53.2 ms / 1.85 s / 2.20 s |
| Per-node GPU KV cache (16 GiB budget) | – | 967,766 tokens | **2,977,319 tokens** |
| GPU busy during the c=4 run | Node 1 only | Node 1 86 % avg, Node 2 85 % avg (max 96 % both) | Node 1 max 94 % avg 34 %, Node 2 max 95 % avg 42 % (c=16: Node 1 max 96 % avg 48 %, Node 2 max 96 % avg 56 %) |
| MTP acceptance | 61.6 % | 68.7–69.7 % | 80.6 % (5,212 of 6,470 drafts, engine counters after the bench) |
| Weights resident per node | 20.75 GiB | 10.61 GiB | 11.35 GiB |
| KV cache | 18.6 GiB = 542k tokens | 17.4 GiB per node = 924k–1,000k tokens | 16 GiB per node = 2,977,319 tokens |
| Engine init (profile + KV + warm-up) | 301 s | 234–241 s | 176.5 s (compilation 58.7 s; CUDA graphs 25 s / 2.62 GiB; ≈5.5 min from container start to ready) |

The 35B-A3B column is the same `cluster-bench.sh` invocation (random 512 in /
128 out, `--ignore-eos`) on the same two nodes; the GPUs peaked at 94–96 %
during the bench runs and at 81 % / 76 % during the single `--probe` request.
It has no single-node column because the model was not run single-node on this
cluster.

¹ Measured on Node 2's identical GB10 with a temporary single-node instance
(same image and flags, util 0.30) so production did not have to be flipped a
third time; the single-stream and concurrency-4 rows are from Node 1.

Reading (27B measurements; the 35B-A3B has not been run single-node here, so
no single-vs-dual ratio exists for it): dual mode gives **20–35 % faster
per-user decode and roughly double
the KV budget**, costs nothing at concurrency 4, and **loses ~15 % of peak
throughput at concurrency 16**;
it does **not** raise prefill-heavy throughput, because a 512-token prefill
costs 128 all-reduces of ~5 MB each and at ~22 Gb/s that comm time is about
what the halved compute saves. With a 200 Gb/s fabric behaving like one, the
same layout would win on both axes; with this ~13 Gb/s-per-link one, TP=2 is a
latency and capacity win, not a throughput win. Both modes stay supported:
With the 27B, `CLUSTER_MODE=single` was the better choice for a purely throughput-bound
workload until the fabric ceiling is fixed; `dual` for snappier replies and
long contexts. Prefix caching keeps the (large) shared
Salesforce system prompt out of most prefills.

## Failure behaviour

Tested on 2026-08-25 by killing the worker under a running cluster (twice,
because the first design did not pass). What actually happens when the worker
disappears: the head's API server keeps answering `/v1/models` and `/health`
(200) while every completion hangs; after `--distributed-timeout-seconds`
(now 300 s) the cross-node RPC times out (`RPC call to sample_tokens timed
out`), the engine is marked dead and the API server **closes its listener
without exiting** - so nothing restarts on its own, and the orchestrator's
`/health` only then reports `vllm: error`. The current design, which is what
the overlay ships:

* **Head healthcheck** (`init: true`, tini at PID 1): `/health` 200 →
  healthy; 5xx → the `vllm serve` process is killed immediately; no answer
  for 4 consecutive checks (2 min) once the process is older than 15 min →
  killed. Killing the real process ends the container and
  `restart: unless-stopped` starts a fresh head that waits (up to 300 s per
  attempt) for a worker. A cold start (4–7 min) is never interrupted.
* **Worker healthcheck**: `vllm serve` + `VLLM::Worker` processes alive
  **and** head `/health` 200. A restarted head is a new process group the old
  worker can never rejoin, so the worker kills itself on head 5xx, or, once
  it has been up 15 min, after 10 unreachable checks (5 min).
* Docker semantics to know: a process that dies is restarted by the policy;
  `docker stop`/`docker kill` are *manual* stops and are not - use
  `scripts/cluster-worker.sh start` / `docker start` after those.
* Net effect, measured 2026-08-25 (`.runtime/logs/failure-test-3.log`,
  worker's `vllm serve` process killed with `kill -9` inside its container):
  Docker restarted the worker within seconds; the head timed out the RPC at
  +316 s, its process exited on its own under `init: true` and Docker
  restarted it at +331 s; both nodes rendezvoused, reloaded, and completions
  were answered again at **+584 s** - no operator action. During those
  ~10 minutes `/v1/models` may still answer while completions fail, and the
  orchestrator's `/health` reports `vllm: error`. During the outage `/v1/models` may still answer; completions
  fail; `scripts/cluster-status.sh` shows which side is unhealthy.
* NCCL cannot use RDMA (missing `/dev/infiniband`, memlock, HCA down) → it
  silently uses TCP sockets; `cluster-status.sh` reports `TCP SOCKET FALLBACK`
  as a warning and `cluster-test.sh` fails its transport check.
* Model missing on the worker → `cluster-sync.sh` copies it (rsync over the
  200G link, ~40–45 s for a 21–22 GB checkpoint); the worker fails fast otherwise.
* Port `CLUSTER_MASTER_PORT` or `VLLM_PORT` in use → the launcher rejects the
  configuration when they collide; `cluster-doctor.sh` checks the host.

## Security notes

* Cluster traffic (torch rendezvous on `CLUSTER_MASTER_PORT`, vLLM's message
  queue, NCCL) is pinned to the direct links by `NCCL_SOCKET_IFNAME` /
  `GLOO_SOCKET_IFNAME` / `VLLM_HOST_IP`; nothing is routed over the LAN. Those
  listeners are nevertheless bound on the host namespace; the host firewall is
  the boundary (`ufw` is installed but disabled on both nodes; the doctor
  reports that).
* The head API is published on the host in dual mode (see *host networking*
  above). `PUBLISH_MODEL_PORTS=false` narrows it to the Docker bridge gateway.
* No secrets are copied to the worker: `worker.env` holds model paths,
  addresses and engine flags only.
* `ssh` to the worker uses the existing key in `~/.ssh/config`; nothing is
  stored in the repository.

## Limitations (real ones)

1. **Fabric ceiling ~13 Gb/s per link.** Root-level investigation needed
   (`sudo mlxlink -d <pci> -m`, `sudo mlxconfig -d <pci> q`, `sudo ethtool -m`,
   `dmesg | grep mlx5`, trying `iommu.passthrough=1`, MTU 9000 on both ends).
   Until then TP=2 improves latency, not prefill throughput.
2. **No pipeline parallelism** for this model class in this vLLM build.
3. **MTU stays 1500** (no sudo). If you get root: `ip link set dev enp1s0f1np1
   mtu 9000` **on both nodes for both links**, persist via netplan, re-run
   `scripts/cluster-doctor.sh --rdma` and `scripts/cluster-test.sh`.
4. Node 2 also runs an unrelated older `sf-local-ai` project (Qwen3.6‑35B on
   port 8000, ~38 GB). It was left alone; it competes for Node 2's GPU when it
   is busy and for memory (65 GB were free when the worker was sized).
5. Both nodes must run the **same image digest**; `cluster-sync.sh` enforces
   it. Upgrading vLLM means updating the digest in
   `compose/compose.dgx-spark.yaml` and re-running `cluster-sync.sh`.
6. `./techsara up` re-runs on a long-lived stack used to stop and disable
   `vllm-embed`/`vllm-reranker` and fall the router back to the main model
   because Docker's lifetime `RestartCount` was read as a live restart loop;
   fixed on 2026-08-25 (`ComposeManager.wait_service`). If you see
   `EMBED_ENABLED=false` / `RERANK_BACKEND=inprocess` / `ROUTER_BASE_URL=
   http://vllm:8000/v1` in `.runtime/generated.env` after an `up`, that is
   the symptom: re-run `./techsara up` with the fixed launcher.
7. A restart of the head takes ~4–6 minutes (27B: weights 45 s; 35B-A3B: weights 73 s, then profiling +
   compilation + CUDA graphs ~3.5 min) — the same order as single node.
