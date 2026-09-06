# Cluster rebalance and TP gate — 2026-09-07

What was changed, what it measured, and what was refuted. Everything here is a
measurement taken on this hardware unless it says otherwise.

## 1. The TP gate: TP=2 kept, on evidence

Both arms: same model and quantisation (`nvidia/Qwen3.6-35B-A3B-NVFP4`),
512 in / 128 out, 16 prompts, seed 1, `vllm bench serve`.

| metric | TP=2 c=1 | TP=1 c=1 | TP=2 c=4 | TP=1 c=4 |
|---|---|---|---|---|
| TTFT median (ms) | **208** | 351 | **374** | 666 |
| TTFT mean (ms) | **247** | 507 | **448** | 711 |
| TTFT P95 (ms) | **451** | 1139 | **1042** | 1579 |
| TPOT median (ms) | 13.54 | **11.20** | **19.33** | 20.51 |
| E2EL mean (ms) | 1946 | **1922** | **2968** | 3461 |
| output tok/s | 65.8 | **66.6** | **171.0** | 146.3 |

**Decision: TP=2.** It wins first-token latency at both concurrencies (−68 %
and −78 % on the median) and wins concurrent throughput (+14.5 %). TP=1 wins
only single-stream decode (−17 % TPOT), and at c=1 that gain is cancelled by
the TTFT loss — end to end the two are a wash (1922 vs 1946 ms). TP=1 also
leaves Node 2 idle.

The decision rule was fixed *before* the measurement: revert to TP=2 if TP=1
regressed decode by more than ~10 %. TP=1 did not regress decode at c=1 — it
improved it — so the rule did not decide this. The stated priority (first-token
latency, both machines contributing) did.

**Why TP=1's decode is faster, and why a faster wire will not change it.** A
decode all-reduce is a few KB. At 12.8 µs per round trip, cross-node tensor
parallelism costs *latency per collective*, not bandwidth. The link is already
running at 97 % of NVIDIA's healthy reference (§3), so there is no bandwidth
left to buy.

## 2. Memory imbalance: cause and fix

The dashboard's 96 % / 20 % was real, not reclaimable page cache. Measured on
Node 1 while it showed 94 %: **10.8 GiB available and 27.1 GiB pushed into
swap**, with only 8.4 GiB of buff/cache to reclaim.

Container cgroups accounted for just 5.7 GiB of the 113.8 GiB in use. The rest
is the GPU driver's unified-memory carveout, which does not appear as process
RSS — so `docker stats` and `ps` both under-report it by an order of magnitude.
`nvidia-smi --query-compute-apps` is the only view that shows it.

**Cause: five vLLM engines resident on Node 1, none on Node 2.**

| engine | before | after |
|---|---|---|
| `vllm-1` main (TP=1 at the time, all weights on Node 1) | 50.4 GiB | 26.6 GiB (TP=2, half on Node 2) |
| `vllm-router-1` | 19.9 GiB | 17.1 GiB |
| `vllm-ocr-1` | 19.9 GiB | 11.6 GiB |
| `vllm-reranker-1` | 4.9 GiB | 4.9 GiB |
| `vllm-embed-1` | 3.5 GiB | 3.5 GiB |
| **Node 1 total** | **74.5 GiB** | **63.6 GiB** |
| **Node 2 total** | 34.5 GiB | 35.2 GiB |

Two independent contributions: restoring TP=2 moved ~26 GiB of model weights
onto Node 2, and right-sizing the two oversized sidecars returned ~11 GiB.

| | before | after |
|---|---|---|
| Node 1 available RAM | 10.8 GiB | **28.3 GiB** |
| Node 1 swap in use | 27.1 GiB | **14.6 GiB** (draining) |
| dashboard split | 94 % / 20 % | **77 % / 48 %** |

### The sidecars were over-provisioned, and one setting was silently ignored

`--gpu-memory-utilization` is a fraction of **total** device memory, so on a
122 GiB GB10 a small-looking fraction is a large reservation. What the engines
themselves reported:

* **router** (`Qwen3-VL-8B-Instruct-FP8`) at 0.17 = 20.7 GiB: weights +
  non-torch 11.07 GiB, activation 2.26, CUDAGraph 0.32, and **7.36 GiB of KV =
  107,184 tokens**. It only ever serves classification calls —
  `llm.router_chat_completion` clips input to `ROUTER_INPUT_CHAR_CAP` (6,000
  chars, ~2K tokens) and caps output at 200 — so that is ~50× its largest
  request. Vision does **not** run here: `VISION_BASE_URL` points at the main
  model, so no image ever enlarges a router request.
* **OCR** at 0.14 = 17.0 GiB: weights + non-torch 2.22 GiB, activation 2.71,
  and **12.11 GiB of KV = 211,552 tokens** against an 8,192-token window —
  25 concurrent full-length requests for a service that is idle between
  uploads.

Now at 0.15 / 0.10: router holds 65,568 tokens (1.33 full-length requests,
~29× its real ones), OCR holds 57,088 tokens (6.97 concurrent full-length
requests). `ROUTER_MAX_CONTEXT` was deliberately **not** reduced — the window
is behaviour, and the memory was recoverable without touching it.

**Defect found and fixed:** `launcher/techsara_cli/environment.py:645-646`
emitted `OCR_GPU_MEMORY_UTILIZATION` and `ROUTER_GPU_MEMORY_UTILIZATION` into
`generated.env` as literals. `generated.env` is the **last** `--env-file`
Compose is given, so it outranks the user's `.env`: setting either key in
`.env` changed nothing and reported nothing, and the compose default
`${ROUTER_GPU_MEMORY_UTILIZATION:-0.17}` was unreachable by a user value. They
are now read from the user environment through `_aux_gpu_memory_utilization`,
which validates the range and refuses a bad value **by name** rather than
silently falling back. `EMBED_GPU_MEMORY_UTILIZATION` had the same defect and
got the same fix. Covered by `SidecarMemoryKnobTests` (7 tests).

**Operational note, learned the hard way:** restart sidecars **one at a time**.
Recreating both at once made the router fail with `AssertionError: Error in
memory profiling. Initial free memory 48.68 GiB, current free memory 50.31 GiB`
— vLLM profiles free memory at startup and a second engine releasing memory
mid-profile aborts it. Sequential restarts succeeded with `RestartCount = 0`.
This is the same hazard the launcher's sequential start already avoids.

## 3. Interconnect: the reported limits were wrong

**The fabric is healthy.** Re-measured with the engine live:

| test | result |
|---|---|
| `ib_write_bw -d rocep1s0f1 -x 3 -F --report_gbits -D 6` (65536 B, GID 3 = RoCE v2) | **108.91 Gb/s** rail A, **108.82 Gb/s** rail B |
| `scripts/cluster-test.sh` (NCCL 2.29.7, both HCAs) | **algbw 21.446 GB/s @ 512 MB = 171.57 Gb/s busbw**, 177.77 Gb/s 64 MiB send/recv, 12.8 µs 4-byte latency, RDMA/RoCE over 2 HCAs |

NVIDIA's healthy two-node DGX Spark reference is 22.1 GB/s busbw. We measure
21.4 GB/s — **97 % of it**. There is no fabric bottleneck.

**Claim 1 — "GPUDirect RDMA is unavailable": CONFIRMED.** NVIDIA's DGX Spark
Porting Guide states that on GB10 `cudaMalloc` memory cannot be coherently
accessed by I/O peripherals, so GPUDirect RDMA is unsupported and
`nvidia-peermem`, `dma-buf` and GDRCopy do not work. Runtime capability bits
agree: `GPU_DIRECT_RDMA_SUPPORTED = 0`, `DMA_BUF_SUPPORTED = 0`. Do not chase
the `dlvsym failed on mlx5dv_reg_dmabuf_mr` line in NCCL logs — a newer libmlx5
resolves the symbol and registration then fails anyway. This costs nothing
here: both figures above are already host-memory numbers.

**Claim 2 — "the RoCE version explains the gap": REFUTED, and self-defeating.**
`ib_write_bw -x 3` *is* the RoCE v2 GID, so 108.91 Gb/s is itself a RoCE v2
number. v1 and v2 differ in encapsulation and routability, not throughput, and
there is no router in the path (`Port: Direct Attach Copper`). RoCE v2/IPv4 is
in fact 12 bytes *leaner* per packet than v1.

**Claim 3 — "108.9 Gb/s vs 22 Gb/s shows a host-staging bottleneck": REFUTED.**
The comparison was invalid and the conclusion drawn from it was wrong.
Three unit traps, all of which were fallen into:

1. **NCCL reports GB/s, not Gb/s.** A busbw of "22" is 22 GB/s = 176 Gb/s.
   This repo's `scripts/lib/nccl_allreduce_bench.py:59` emits
   `busbw_Gbps = busbw_GBs * 8` and `cluster-test.sh` labels it Gb/s, so the
   repo's own output *is* gigabits — but a raw nccl-tests figure is not. Check
   which one you are reading before comparing anything.
2. **`perftest --report_gbits` is decimal Gb/s; its default is MiB/s.**
   Converting the default with 8/1000 is wrong — the factor is 119.2.
3. **Never compare `ib_write_bw -b` against NCCL busbw.** `-b` sums both
   directions; busbw counts one. For n=2, `busbw = algbw × 2(n-1)/n = algbw`
   exactly, so the bus-bandwidth convention deflates nothing.

The stored project note "RoCE links cap at 13 Gb/s each (22 Gb/s NCCL
dual-rail)" is superseded. 108.91 / 13.3 = 8.19 — the bits/bytes factor. It
survived for two weeks because it sat next to `iperf3`'s **genuine** 14–16 Gb/s:
TCP really is CPU-bound at MTU 1500 on these cores, so a real number appeared
to corroborate a wrong one. Jumbo MTU is not the fix either — 108.91 Gb/s was
achieved at MTU 1500. `docs/CLUSTER.md`, `docs/MONITORING.md` and
`docs/VOICE.md` argued against expert parallelism and against putting
activations on the link using the 8×-too-slow figure; all three are corrected,
with the original measurements kept for the record.

**Cabling.** Both QSFP ports are cabled, but only the `p1` interface pair
carries a link (`p0` shows `carrier=0`). NVIDIA documents that one QSFP port
reaches the SoC through two independent PCIe Gen5 x4 links and appears as two
Linux interfaces — so "dual rail" here is the two PCIe rails of **one**
200 Gb/s port, which is the supported topology and is what the configuration
already assumes. A single-device `ib_write_bw` is PCIe-bound near ~109 Gb/s
(Gen5 x4 ≈ 126 Gb/s) and can never show 200. Cabling was not changed.

## 4. Harness defect fixed

`scripts/cluster-bench.sh` reported `Successful requests: 0`, every metric
`0.00`, and **exit status 0** in single-node mode. `vllm bench serve` runs
*inside* the head container, but the script passed it the **host's** address.
In dual mode the head is host-networked and the two coincide; in single mode it
sits on a bridge and listens on its own internal port, which the host merely
publishes as `$VLLM_PORT`, so the benchmark dialled nothing and reported a row
of honest-looking zeros. The header also printed `TP=2` from `.env` while the
engine was actually running TP=1.

Fixed: the endpoint is now resolved by probing from **inside** the container;
TP/PP are read from the **engine's own argv** (falling back to vLLM's default of
1 and saying so) rather than from `.env` intent; and a run that completes zero
requests now fails loudly instead of exiting 0.

## 5. Settled late, and what is still open

* Perfect memory equality would need application services moved too — Node 1
  also carries PostgreSQL, the orchestrator, frontend, SearXNG, Prometheus,
  Grafana and the Salesforce sync worker. The remaining ~28 GiB gap is the
  sidecar stack (37.5 GiB on Node 1 against ASR's 8.2 GiB on Node 2).
  Relocating OCR (11.6 GiB, entirely off the Fast path) to Node 2 following the
  existing `compose.asr-worker.yaml` pattern is the next lever; it needs the
  model synced to Node 2 and `OCR_BASE_URL` repointed, and is **not** done.
* `NCCL_SOCKET_IFNAME` points bootstrap at a RoCE data rail rather than the
  management NIC that NVIDIA's playbooks use. Not changed, not measured.

* **Within-page chunk selection (settled 2026-09-07 — no defect).** Measured
  against the live Qwen3-Embedding-0.6B, not the toy fixture. Through the
  production path with the oversized tracker present, `retrieve(top_k=6)`
  returns all six distinct pages and 2 of the 5 figures under the test's
  keyword-soup query — so the strict xfail's own assertion passed on the real
  model; the toy was reversing the two comparisons (robotics, climate) the real
  embedder decides by 0.0022 and 0.0034 plain L2. Asked as a caller actually
  asks — both callers pass the user's own words — the figure chunk comes back
  from 5 of 5 pages and the tracker drops from first (0.384) to last (0.652).
  Controls: on varied prose the figure chunk wins 5/5 under both phrasings, in
  the head chunk or the tail; equalising chunk length changes nothing. The
  cause was the query restating the fixture's own boilerplate, not chunk
  selection. `max_chunks_per_page` stays at 1: raising it to 2 does NOT make
  the old test pass (the `top_k` cut drops exactly the second chunk of each
  page) and costs 6 -> 4 distinct pages on the real embedder and 2/5 -> 1/5
  figures — so the trade this was held open for does not exist. No product code
  changed; the xfail is replaced by
  `test_the_chunk_kept_for_a_page_is_its_nearest_one` (the "best chunk, not the
  first" invariant, offline) and an opt-in real-embedder eval gated on
  `EMBED_EVAL_BASE_URL`.

* **Extractor refresh backlog (open, not started).** 2,063 of 2,209 pages sit
  below `EXTRACT_VERSION = 4` (1,989 at version 0, 74 at 3, 146 at 4), so the
  JSON-LD/microdata structured recovery is not yet reaching most stored
  knowledge. `web_pages` has no raw-HTML column — only the already-extracted
  `text` — so this needs **one network fetch per page**, not a local re-parse.
  At the configured `web_refresh_max_pages_per_cycle = 8` /
  `WEB_WORKER_INTERVAL_S = 300` the theoretical floor is ~21.6 h, but the
  observed rate is ~0.25 pages/min (16 pages in ~65 min) because 326
  deadline-due pages compete for the same 8 slots and a 6-hour `fetched_at`
  spacing guard holds rows back — so **realistically 5–6 days**. 27 pages with
  `refresh_failures > 0` are excluded entirely. Knock-on: a re-fetch changes
  `content_hash`, which resets `indexed_at` to NULL, so all ~2,063 re-enter the
  index queue (~2 h 18 m of embedding at the measured 15 pages/min, spread
  across the refresh's own pace). The **chunk/index drain is separately
  complete** — 0 pending, 0 stale, all 2,209 pages at `chunk_version = 3` — so
  do not conflate the two backlogs.

* **7 pages still exceed the 717,200-char ceiling**, dropping 4,563,338 chars
  between them; the largest is a 2.37 M-char SEC archive. This is reported, not
  silent: `web_index.py` increments `web_index_page_truncated_total` and logs
  the URL and shortfall.
