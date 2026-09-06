# Two-node DGX Spark: what actually runs where, and what the dashboard means

Read-only investigation plus one instrumented generation. Nothing was reconfigured to produce this.

## The headline: the cluster is already correct

The main model runs **tensor-parallel across both machines**, and this is not inferred from config:

| | node 1 `spark-0e68` | node 2 `spark-476e` |
| --- | --- | --- |
| rank process | `VLLM::Worker_TP0` (host PID 3812989) | `VLLM::Worker_TP1` (host PID 3862534) |
| GPU held | 28,703 MiB | 28,727 MiB |
| age | 20 h 06 m | 20 h 06 m |

Engine config from the head's own log: `tensor_parallel_size=2, pipeline_parallel_size=1,
data_parallel_size=1`. Every GPU PID was mapped to its container through `/proc/<pid>/cgroup`, so
rank placement is established, not guessed from names.

## Both GPUs demonstrably participate in generation

The read-only pass deliberately would not claim this, because loaded weights and green health
checks do not prove participation. So it was measured: both hosts sampled at 100 ms while ONE real
400-token completion ran on the main model.

| | node 1 (TP0) | node 2 (TP1) |
| --- | --- | --- |
| idle before | 0% util, 9.7 W | 0% util, 11.0 W |
| **during the 4.82 s generation** | **67.0% avg, 85% peak, 20.1 W** | **70.4% avg, 77% peak, 22.1 W** |
| idle after | 29.6% (tail), 13.5 W | 5.9%, 14.6 W |

Both idle before, both at ~70% during the same window, both roughly doubling power draw, both
falling back to idle after. Node 2 is fractionally *higher*. Supporting argument from the
read-only pass: with TP=2 each rank holds half of every weight matrix, so rank 0 alone cannot emit
a token — and the head has served 80,800 generation tokens.

Interconnect is genuinely RDMA, not a TCP fallback: NCCL reports
`NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE`, with 64 channels alternating across
both rails (128 log lines each). GPUDirect RDMA is off, which is expected on GB10 unified memory.

## Explaining the dashboard without changing anything

A dashboard showing node 1 with roughly twice node 2's GPU figure is **correct and expected**, and
does not mean the second GPU is idle:

1. **`nvidia-smi` cannot report GPU memory on GB10.** `--query-gpu=memory.total,memory.used` returns
   `[N/A]`: unified memory has no separate VRAM pool. The only per-GPU numbers that exist are the
   per-process rows.
2. **The Prometheus gauge is a SUM of those process rows**, not a device total —
   81.4 GB for node 1, 39.4 GB for node 2, matching the process tables exactly.
3. **Node 1 hosts five models, node 2 hosts two.** Node 1: main rank 0 + router (Qwen3-VL-8B) +
   embedding + reranker + OCR. Node 2: main rank 1 + ASR. The ratio is auxiliary-model placement.
4. **Node 2's gauge even counts the desktop** — 431 MiB of `gnome-remote-desktop-daemon` is in that
   sum.

So the picture is a *memory* asymmetry from where the small models live, not a *compute* asymmetry
in the main model. The utilisation table above is the compute answer.

## Memory: an asymmetry, but not a live problem

| | node 1 | node 2 |
| --- | --- | --- |
| RAM available | 19 GiB of 121 | 58 GiB of 121 |
| swap in use | 21 GiB (`/swap.img` 16 G **100% full**) | 722 MiB |
| **PSI memory pressure** avg10/60/300 | **0.00 / 0.00 / 0.00** | **0.00 / 0.00 / 0.00** |

On GB10 unified memory, GPU allocations count as system RAM, so node 1's 101 GiB "used" is mostly
its five models. **The swap is cold pages parked historically — PSI shows no current stall on
either host**, so this is headroom risk, not an active latency cost. It would be wrong to claim a
speed-up from relieving it without measuring one.

## Discrepancies found between docs and reality

* `docs/CLUSTER.md` still states a 16 GiB KV budget in three places; the running system uses 8 GiB
  (the change and the `NV_ERR_NO_MEMORY` incident behind it ARE recorded later in the same file, so
  the earlier sections are stale).
* The doc says node 2 runs "nothing else from the stack". It also runs **ASR** (Qwen3-ASR-1.7B, a
  real service the orchestrator routes to) and two monitoring exporters. The stale co-tenant the
  doc's Limitations warns about is `Exited (0) 8 days ago` and nothing listens on node 2:8000.
* Served window is 1,000,000, not the 800,000 in the doc's example output.

## What is genuinely underused

Node 2 has **58 GiB free and 20 CPU cores essentially idle**. Every non-GPU workload — crawling,
parsing, indexing, the database, the frontend, SearXNG, the whole monitoring stack — runs on node 1.
That is the real opportunity, and it is CPU/memory placement, not GPU parallelism.
