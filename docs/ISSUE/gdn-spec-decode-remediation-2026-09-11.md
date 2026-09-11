# GDN / MTP engine crash — remediation record (2026-09-11)

**Follows:** [`gdn-spec-decode-fault-report.md`](gdn-spec-decode-fault-report.md) (analysis, 2026-09-10)
**System:** two DGX Spark GB10 (spark-0e68 head, spark-476e worker), `Qwen/Qwen3.6-35B-A3B-NVFP4`, vLLM `0.26.1rc1.dev77+g6f91edf96`, TP=2 / nnodes=2
**Branch:** `fix/gdn-mtp-spec-decode` (forked from `main` at 9b4ffaf)

Every claim below is tagged **VERIFIED** (observed on the running system), **CHANGED** (a file or setting this programme altered), **TESTED** (a test or measurement that was actually run, with its result) or **NOT TESTED** (stated so).

---

## 1. Root cause

**VERIFIED** — from the installed source, not inferred:

- `vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn.py:1249-1255` runs `mixed_qkv.index_select(0, non_spec_token_indx)` only inside `if spec_sequence_masks is not None`, and `vllm/v1/attention/backends/gdn_attn.py:189-198` sets `spec_sequence_masks = None` whenever `not self.use_spec_decode`. Without `--speculative-config` the branch cannot execute.
- Three of the four CUDA faults in the worker's retained log were *reported* at that branch (see §1.1 for why the reporting line is not the faulting kernel): 2026-09-01 19:46:49Z (`illegal memory access`), 2026-09-01 23:36:49Z and 2026-09-02 08:28:54Z (`misaligned address`), each matched by `Xid 13` (Misaligned Address, GPC 3) and `Xid 43` in the worker's kernel log, each followed by the head's collective hanging and a 9–15 minute reload.
- The fourth, 2026-09-10 21:31:42Z, is a **different site**: `torch.ops.vllm.bmm_fp8` → FlashInfer `fp8_gemm_sm100` → cuDNN `execute` → `cuLaunchKernelEx` err 1 (`CUDA_ERROR_INVALID_VALUE`) inside the compiled graph of the attention projections (the checkpoint's 130 FP8 `linear_attn`/`self_attn` projections), under the same mixed load, with no Xid. Its relation to MTP is plausible (spec-decode shapes the batches) but **not proven**; the soak in §7 is the evidence that matters for it.
- Both nodes: identical image digest `vllm/vllm-openai@sha256:24f2f89…`, byte-identical engine arguments, NCCL 2.30.7 over both RoCE rails (`Using network IB`, no socket fallback). The fault-to-serving timeline of the 2026-09-10 event, from Prometheus + both logs: fault 21:31:42Z → GPUs pinned at 96 % with `generation_tokens_total` frozen → head engine died at +300 s (`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS`, an environment default inside vLLM — **not** `--distributed-timeout-seconds` as the analysis and `docs/CLUSTER.md` said) → container healthcheck killed the API process → Docker's restart policy restarted the container at 21:38:25Z → worker rejoined 21:41:36Z → serving 21:45:43Z. **14 minutes.**

The report's "it is not hardware" discriminator stands: since the 2026-09-10 reboot both kernels showed 0 Xid until the event below, and the OCR and whisper engines on the very GPU that threw every Xid restarted 9 and 0 times (lifetime) against the main engine's 23 engine starts in 11.7 days (Prometheus `changes(process_start_time_seconds[14d])`).

### 1.1 What the soak then proved — the report's conclusion was too narrow (**VERIFIED** 2026-09-11 07:05:33Z)

27 minutes into the mixed-workload soak (§7, concurrency 10, MTP **off**, prefix caching **off**) **rank 0 faulted**: `RuntimeError: Triton Error [CUDA]: misaligned address` raised by `qwen_gdn_linear_attn.py:1348 _forward_core` → `vllm/third_party/flash_linear_attention/ops/fused_gdn_prefill_post_conv.py:215 fused_post_conv_prep`, with `Xid 13` (Graphics Exception, class 0xcec0) and `Xid 31` (MMU fault, GPC2, `@ 0x0`) in the head's kernel log, then `terminate called after throwing c10::AcceleratorError`. That is the GDN layer's **prefill path on a mixed prefill+decode batch** — the code takes it only when `split_non_spec = (spec_sequence_masks is None and num_prefills > 0 and num_decodes > 0)`, i.e. exactly *without* spec-decode — not the `index_select` branch.

Consequences:

- The misalignment bug in this build's GDN Triton kernels is **not confined to spec-decode**. It fires on mixed prefill+decode batches — which is what the uniform benchmark rungs never produce and what the analysis pipeline and the soak produce continuously. The three "`index_select`" faults of 1–2 September are almost certainly the same asynchronous CUDA error surfacing at the next synchronising call (`index_select` checks errors; the Triton launch that preceded it does not), which the analysis read as the crash site. MTP made mixed steps more frequent; it is not required.
- The engine change stands on its own merits — it is faster (§3) and removes one manifestation — but **it does not make this vLLM build crash-free under mixed load**. The durable fix is a vLLM build whose GDN kernels are fixed (§13, item 1); until then the recovery path (§11) and the orchestrator's outage tolerance (§4) are what limit the damage.
- Recovery, this time, unassisted: fault 07:05:33Z → EngineCore waited 3 × 60 s on the dead worker process (`No available shared memory broadcast block`) → API exit and Docker restart 07:09:20Z → the worker's healthcheck restarted rank 1 at 07:14:14Z (its 10-miss rule; now 4) → re-paired inside the head's rendezvous window → serving 07:17:20Z. **11 min 47 s**, no watchdog action needed (it correctly saw a fast 5xx, not a hang). Logs preserved: `.runtime/logs/head-fault-20260911T070843Z.log`, `worker-at-head-fault-…`, `head-kernel-…`; apport wrote `/var/crash/_usr_bin_python3.12.0.crash` (845 KB) during those minutes.

## 2. What was changed

### Engine configuration (**CHANGED**, **VERIFIED** on both running containers)

| | Before (2026-09-10 21:38Z start) | After (2026-09-11 06:26Z start) |
|---|---|---|
| `--speculative-config` | `{"method":"mtp","num_speculative_tokens":1}` on both ranks | **absent on both ranks** (`speculative_config=None` in the engine log; no `Loading drafter model`) |
| prefix caching | `--enable-prefix-caching` (vLLM: "experimental" in `mamba_cache_mode=align`) | **`--no-enable-prefix-caching`** on both ranks, stated explicitly |
| CUDA graphs | `FULL_AND_PIECEWISE is not supported with spec-decode … setting cudagraph_mode=PIECEWISE` — 51 piecewise graphs | `Capturing CUDA graphs (mixed prefill-decode, PIECEWISE)` **and** `Capturing CUDA graphs (decode, FULL)` — 51 + 35 graphs, 33 s, 3.40 GiB head / 4.41 GiB worker |
| GPU KV cache | 1,494,824 tokens (1.49× at 1M) | **1,663,201 tokens (1.66× at 1M)** — the 2112-token Mamba-align block padding is gone; same 8 GiB budget |
| unchanged | `--tensor-parallel-size 2 --pipeline-parallel-size 1 --nnodes 2 --distributed-executor-backend mp --max-model-len 1000000` + YaRN 3.82, `--kv-cache-memory-bytes 8589934592 --kv-cache-dtype fp8 --enable-chunked-prefill --max-num-batched-tokens 8192 --distributed-timeout-seconds 300` | same |

Proof, from `docker inspect` on both nodes plus the retained logs and kernel journals: `scripts/cluster-verify-engine.sh --probe` → **25 passed, 0 failed** at 06:33Z (it reported 3 FAILs against the previous engine, as designed).

### Configuration files (**CHANGED**)

- `.env` (gitignored, backed up first to `.runtime/env-backups/.env.20260911T054952Z`, mode 0600): `CLUSTER_SPECULATIVE_CONFIG=` (new, empty) and `MAIN_MODEL_ENABLE_PREFIX_CACHING=false` (was `true`). Nothing else; no secret was read or printed.
- `.env.example`: same two defaults, with the reasons and the measured cost/benefit.

### Source (**CHANGED**, commits on `fix/gdn-mtp-spec-decode`)

| Commit | What |
|---|---|
| `dbdcd1a` | `docs/ISSUE/gdn-spec-decode-fault-report.md` — the analysis was an untracked file and was lost from the working tree during a branch switch by another session on 2026-09-11; restored verbatim and tracked. |
| `8d587e7` | **launcher**: `CLUSTER_SPECULATIVE_CONFIG` absent/empty ⇒ no flag at all (was: absent ⇒ MTP on); the MTP JSON is an explicit opt-in. One validator (`cluster.speculative_config_argument`) renders both the dual-mode `CLUSTER_ENGINE_ARGS` and a new `MAIN_MODEL_SPECULATIVE_ARGUMENT` for the single-node overlays, so `CLUSTER_MODE=auto` degrading to single can no longer bring MTP back by itself. `MAIN_MODEL_ENABLE_PREFIX_CACHING` becomes a real switch (`--enable-prefix-caching` / `--no-enable-prefix-caching`) on both ranks and in `compose.dgx-spark.yaml` / `compose.nvidia.yaml`, re-emitted normalised into `generated.env` so the orchestrator's `/health` belief is the launch. The overlays default the two new keys to empty so a `generated.env` from an older launcher still renders. Tests: `launcher/tests` 474 passed; the overlays are rendered through Docker Compose with both switches in both positions and with a stale `generated.env`. |
| `2a8ac60` | Narrative corrections (which faults, which vLLM default, which timeout knob) + the stale-`generated.env` test. |
| `5769690` | **orchestrator**: `app/resilience.py` (§4). |
| (this doc's commit) | `scripts/cluster-verify-engine.sh`, `scripts/cluster-soak.py`, explicit KV budgets for embed/reranker, whisper CUDA-death healthcheck, docs. |

## 3. Measurements — before and after (**TESTED**)

Same tool (`scripts/cluster-bench.sh` = `vllm bench serve` inside the head container, random prompts, `--seed 7`), same engine otherwise, both nodes' GPUs sampled. BEFORE was measured at 06:11–06:13Z on the MTP+prefix-cache engine after the worker-side pipeline had finished; AFTER at 07:06–07:08Z on the warm new engine (a first pass right after the restart carried a cold-shape TTFT of 312 ms at c=1 that disappeared on the repeat).

| Workload | BEFORE | AFTER | Δ |
|---|---|---|---|
| c=1, 512 in / 128 out — output tok/s | 69.4 | **100.7** | **+45 %** |
| c=1 TTFT p50 / p95 | 123 / 489 ms | **90 / 95 ms** | −27 % / −81 % |
| c=1 TPOT p50 | 12.84 ms | **9.27 ms** | −28 % |
| c=4, 512/128 — output tok/s | 158.6 | **238.7** | **+50 %** |
| c=4 TTFT p50 / TPOT p50 | 358 ms / 21.8 ms | 271 ms / 14.2 ms | −24 % / −35 % |
| c=10, 2048 in / 256 out — output tok/s | 250.4 | **283.9** | +13 % |
| c=10 TTFT p50 / E2E p99 | 1319 ms / 13.8 s | 1301 ms / **9.6 s** | ≈ / −31 % |
| 32K prompt, c=1 — TTFT (= prefill) | 4.70 s (7.0K tok/s) | **4.10 s (8.0K tok/s)** | −13 % |
| one probe completion, 133 tok | 76.7 tok/s incl. prefill | 103.1 tok/s | +34 % |
| engine start → `Application startup complete` | 7 min 29 s (2026-09-09, warm compile cache) | 5 min 20 s (06:26:21 → 06:31:41Z, **cold** `torch.compile` 50 s — the cache key changed) | |
| GPU during a request | head 83 % / worker 77 % peak | head 92 % / worker 90 % peak | both ranks work every request |

Why the report's expected "10–20 % decode cost" became a gain: on live traffic the MTP draft was accepted 86 % of the time (1.86 tokens per step, Prometheus 24 h), but with spec-decode FlashInfer on GB10 (sm 12.1, `UNIFORM_SINGLE_TOKEN_DECODE`) forces `cudagraph_mode=PIECEWISE`; without it the decode steps run as FULL CUDA graphs. On a TP=2 pair whose per-step time is launch overhead plus a small all-reduce, removing that overhead is worth more than the draft. Prefix caching's contribution is a separate, smaller trade (§6).

Prometheus baseline for the 24 h before the change (probe-corrected where the watchdog's 1,440 daily 2-token probes skew the quantiles): requests 3,184; MTP acceptance 0.861; prefix-cache hit ratio 0.128–0.160 of prompt tokens (0.081 over 7 d); head node memory used 77.7 % avg / 86.8 % max with 6.5 GiB average swap in use. The exact PromQL is in the audit transcript and in `docs/MONITORING.md`'s conventions; re-run at +24 h with identical windows.

## 4. Outage tolerance in the orchestrator (**CHANGED**, **TESTED**)

`orchestrator/app/resilience.py` — one wrapper every model call in `app/llm.py` now opens through:

- Retries only what a restart can fix: `openai.APIConnectionError` (refused/reset/DNS), a **connect** timeout, 5xx/429, a bare `APIError` (a dying stream's error chunk), raw httpx transport errors. Never a 4xx, never a **read** timeout (a generation that ran the wall clock must not be re-run), never a cancellation.
- On a connection-class error it polls `{root}/health` + `/v1/models` until 200 (every `LLM_HEALTH_POLL_S`=5 s), then backs off with full jitter (2·2ⁿ s, cap 30 s) before re-issuing. Bounded by two windows: `LLM_INTERACTIVE_RECOVERY_S`=120 s for a chat (then `MODEL_UNAVAILABLE`, which the client already renders) and `LLM_RECOVERY_WINDOW_S`=1200 s for a background job (`recovery_window()`), covering the measured 13-minute reload.
- Streams retry only the open. The chat worker routes "The model is restarting — waiting…" to its `status` event. Every wait/give-up is counted in Prometheus (`llm_retry_total`, `llm_engine_wait_seconds`, `llm_engine_unavailable_total`) and logged with the prefix `llm.resilient`, because the head's container log lost the entire 2026-09-10 incident window to rotation.
- Two catch-alls narrowed to HTTP 400: `_open_stream` used to switch token telemetry off for the whole process on the first connection error; `json_completion` used to re-send unconstrained into a dead port.
- `main._failure_sentence` now classifies the SDK's exceptions (they are not `httpx.TransportError` subclasses — a 13-minute outage was reported to the person as `APPLICATION_ERROR`).
- Video pipeline: a required stage that cannot reach its engine (`ModelUnavailable`, `ASRUnavailable`, `ASRBusy`) **defers** the job — status back to `queued`, `error="waiting for the model (attempt N)…"`, finished stages kept, the deferred stage not stamped done — bounded by `VIDEO_MAX_ATTEMPTS`=5, and the maintenance drain leaves a deferred row alone for `VIDEO_RETRY_DELAY_S`=300 s. An optional stage fails soft. An OCR-engine outage is no longer cached as "no readable text" for the life of those bytes. Idempotency: stage outputs are content-hash-keyed files plus the row's `stages` map; the V29 lease prevents two runners on one video.
- Deep Research reports the outage in the app's words instead of "Connection error.".

Tests: `orchestrator/tests/test_llm_resilience.py` (17) and `test_video_resilience.py` (7); the full suite (3,130 + 24) passed with the windows zeroed in `conftest.py` so tests that hand callers a dead client stay fast. The orchestrator image was rebuilt and rolled by the same `./techsara up` (06:33Z); production is running it.

### The worker-side pipeline (the report's §7) — **NOT applied here**

`interview_analysis/models/client.py` lives in a different repository on the worker (`~/Documents/GitHub/interview-analysis-v2`, uncommitted work in progress, a sweep running from it during this programme). The equivalent fix — health-gated transport retries with a 20-minute window, SDK retries off, logging capture so sweep logs are no longer 0 bytes, and Phase-3 recovery for jobs stranded at `preprocessed` — is delivered as two idempotent patch scripts with instructions in [`interview-analysis-client/`](interview-analysis-client/). Applying them is the owner's call.

## 5. Cluster topology, GPU, CPU and memory (**VERIFIED** 2026-09-11)

**Topology.** Head spark-0e68 (192.168.9.54 mgmt, 10.100.184.1 rail A, 10.100.185.1 rail B) runs rank 0 and the only HTTP endpoint (`0.0.0.0:8000`, host network); worker spark-476e (192.168.9.68 / .184.2 / .185.2) runs rank 1 `--headless`. Both: 20-core ARM (10× Cortex-X925 + 10× A725), 1 NUMA node, 121.7 GiB LPDDR5X unified memory, GB10, driver 580.173.02, CUDA 13.0, Docker 29.2.1. Two ConnectX-7 per node, each PCIe Gen5 ×4 (kernel: 126 Gb/s available), port f1 of each ACTIVE at 200 Gb/s RoCE, port f0 unplugged. Management, rail A and rail B are three distinct ports/subnets.

**NCCL / RDMA path.** `NCCL version 2.30.7+cuda13.3`, `Using network IB`, 64 channels alternating `NET/IB/0` and `NET/IB/1`, no `NET/Socket` fallback in either log. RDMA hardware counters (`/sys/class/infiniband/*/ports/1/counters`, ×4 B) show both rails carrying equal load (lifetime 1.06 TB + 0.88 TB per direction per node; a passive 10 s production window moved ~4.1 GB per direction per rail on both nodes) with **zero** RoCE errors, retransmits or CNPs since boot. Two documented, untested improvements: **GPUDirect RDMA is disabled** on every HCA (`GDR 0`; no `nvidia_peermem`, the image's libmlx5 lacks `mlx5dv_reg_dmabuf_mr`), so NCCL host-bounces — the ~200 % CPU of `VLLM::Worker_TP0` at idle is that proxy; and the rail netdevs run **MTU 1500** (RoCE `active_mtu` 1024 of a possible 4096). Rail A additionally carries vLLM's rank0→rank1 TCP message queue (~19 GB/day) and the torch store on :29501. The fabric memo's 109 Gb/s per rail is a host-bounce ceiling, not a link ceiling.

**Both GPUs participate.** During one request both GPUs rise from 0 % to 90–96 % (verify script, bench sampling); the c=10 bench averaged 76 % head / 77 % worker. `nvidia-smi` `memory.used` is N/A on GB10 (unified memory): attribute GPU allocations from `--query-compute-apps` and the `/proc/meminfo` residual, which agree within ~2 GiB.

**Per-node memory map (GiB, GPU allocation + container cgroup [+ swap]), before the restart:**

```
DGX Spark 1 (head, 121.7 total)          DGX Spark 2 (worker, 121.7 total)
Main LLM rank 0     40.0  [+3.8 swap]    Main LLM rank 1     ~40
Router (VL-8B FP8)  22.0  [+1.6]         OCR (Unlimited-OCR)  13.8 GPU + 8.8 RSS
Reranker 0.6B        4.8  [+1.3]         Whisper replica #1    5.3
Embed 0.6B           3.5  [+1.3]         ir-team-automation pg 0.04
Whisper replica #2   6.0  [+1.0]         page cache           ~40 (reclaimable)
other containers     7.5  (sync-worker 2.9, postgres 1.9, pg-test 0.9, …)
dev tooling         24.8  (two vscode-server trees, Claude sessions, pytest)
swap in use       12.5–17.6 (48 G + 16 G swapfiles; 204 GiB paged out in 7 d)
MemAvailable     21–30                   MemAvailable          39–47
```

The head is the constrained node — every inference process has pages in swap, and rank-0 stalls stall both ranks — while the worker has ~40 GiB reclaimable and zero swap. Placement verdicts (from traffic, size and latency, not from "two GPUs exist"): main LLM **TP=2** (measured latency and concurrency win); **router → worker** (17 GiB resident, 0.01 req/s, LAN RTT irrelevant on a 3 s call — the single largest movable block; specified below, **not implemented** in this programme); embed and reranker **head** (1.1–1.2 GiB weights, on the chat path, 12–180 ms calls); OCR **worker** (as deployed since 2026-09-09); whisper **one replica per node** (the only sidecar whose replica has paid for itself: the worker copy served CUDA-dead for 12 h on 2026-09-10 while `/health` said ready — fixed in `compose/whisper/server.py`, see §8). Nothing else warrants TP=2: every sidecar is ≤ 10 GiB of weights and would put per-layer activations on the fabric the main model's decode already uses.

**CPU.** Idle burn is the NCCL proxy and busy-poll (head vLLM container ~307 %, worker ~165 %); the 20 cores per node are otherwise lightly used (24 h avg 7.1 % head / 2.5 % worker; load1 max 20.8 / 18.5). CPU-heavy background work already runs on both hosts by placement rather than by pretending the CPUs are one: OCR preprocessing and the ASR decode for the worker replica on Spark 2; ffmpeg/frame extraction, the sync worker (55 % avg — 340 CPU-min/day, unexplained, worth a look), embedding batches and web crawling on Spark 1. `kernel.numa_balancing=0`, single NUMA node per box, no HugePages: no affinity change is warranted by the numbers.

## 6. Prefix caching decision (**CHANGED**, with the trade recorded)

Off, for now. The report's "0.99×, none" was true on 2026-08-29 (0 hits of 40,863 queries: the Mamba-align block is 2112 tokens, so a prefix shorter than that never hits); by 2026-09-11 longer conversations cleared the block and 8.1 % (7 d) / 16.0 % (24 h) of prompt tokens hit, all from prompts > 2112 tokens. That is a TTFT saving on the long-conversation tail, not throughput, against a feature the engine itself labels experimental, a JIT-compiled Mamba kernel during inference, and 10 % of the KV budget lost to padding. With the GDN incident still being closed out, stability outranks it; it is a one-line `.env` change and a reload to bring back, and the measured 32K prefill (4.1 s) bounds the cost of re-prefilling a long history. Recommended: re-evaluate after 7 days of clean running, measuring TTFT on multi-turn chats with the hit ratio PromQL in `docs/CLUSTER.md`.

## 7. Tests executed

| Test | Result |
|---|---|
| A — smoke (one completion, both GPUs sampled) | **PASS** — 133 tokens in 1.29 s, head 92 % / worker 90 % |
| B — repeated sequential (bench c=1, 8 + 8 + 8 requests) | **PASS** — 100.7 tok/s, TTFT p99 95 ms |
| C — concurrent (bench c=4 ×32, c=10 ×40) | **PASS** — 238.7 / 283.9 tok/s, 0 failures |
| D — mixed workload (`scripts/cluster-soak.py`: 60 % short chat turns, 30 % 3–9K-token documents, 10 % 24–32K pastes, thinking on for a third, all in flight together at concurrency 10) | see E |
| E — sustained soak, concurrency 10, both nodes monitored every 30 s for CUDA faults, Xid, engine restarts, frozen `generation_tokens_total`, watchdog events | **FAIL at 27 min** — 1,400+ requests clean (short TTFT p50 0.42 s, medium 0.85 s, long 3.0 s), then the rank-0 GDN prefill fault of §1.1; the monitor caught it on the first sample (faults 7, Xid 2, tokens frozen at 286,496 with 10 running) and the recovery in §1.1 followed. Log: `.runtime/logs/cluster-soak-20260911-120831.jsonl`. The harness had no backoff on a dead port (fixed the same hour). |
| Launcher suite | 474 passed (`env -u TECHSARA_MODEL_CACHE python3 -m unittest discover -s launcher/tests`) |
| Orchestrator suite | 3,154 collected, all passed against a private test database (`TEST_DATABASE_URL=…/test_gdn_lead` on the throwaway `pg-test`) |
| CI ruff gate (E9/F63/F7/F82) | clean |
| Compose validation | the full 4-file chain + 3 env files + 4 profiles renders (`docker compose config`); the verify script and every `scripts/cluster-*.sh` run against the live stack |
| Long context (§9) | 8K / 32K / 131K / 262K / **500K** accepted with 3/3 needles each; 950K **not run** (memory-constrained head, §9) |
| Failure testing (§10) | router restart under Fast chats: PASS; engine restart under a Smart chat + a video job: PASS (chat → `MODEL_UNAVAILABLE` after 120 s; video waited 75 s and completed) |

## 8. Other changes in this programme

- **Embed and reranker engines** get an explicit `--kv-cache-memory-bytes 2 GiB` (`EMBED_KV_CACHE_MEMORY_BYTES` / `RERANKER_KV_CACHE_MEMORY_BYTES`): on 2026-09-09 the embed engine computed a *negative* KV budget three starts in a row while the head's free memory moved under it. Verified after the restart: `GPU KV cache size: 18,720 tokens`, ready in 42 s.
- **Whisper healthcheck** (`compose/whisper/server.py`): after `WHISPER_CUDA_DEATH_THRESHOLD`=3 consecutive CUDA-runtime failures the server sets `ready=false`, records why on `/health` (with a `cuda_failures` counter) and exits so `restart: unless-stopped` gives it a fresh CUDA context. **Deployed** 07:26–07:28Z, one replica at a time (head 22 s load, worker 24 s), `ASR_BASE_URLS` unchanged; the death logic was unit-checked inside the built image.
- **Worker healthcheck** (`compose/compose.cluster-worker.yaml`): a head whose API process exits outright answers 000, not 5xx, so the worker's "head unreachable" rule was the only thing re-pairing it — and its ten misses (5 min) were a third of the 07:05Z outage. Now four misses (2 min), the same as the head's own rule; the head's rendezvous waits 300 s for the worker, so re-pairing on the first attempt stays likely. Shipped to the worker and applied at the 07:29Z restart (`Healthcheck.Retries: 4` on the running container).
- **`scripts/cluster-soak.py`** — backs off on a dead port instead of hammering it (the first run drowned the monitor in identical lines).
- **`orchestrator/app/continuation.py`** — a first segment that dies before producing a single token now propagates the exception (seen live: the Fast-mode answer path turned a 120 s `ModelUnavailable` into an empty "successful" answer with no error event); a partial answer is still kept. `resilience.py` announces the wait once per outage per turn even when the route classification and the answer wait in sibling tasks, and rounds the window it names.
- **`scripts/cluster-verify-engine.sh`** — proves the engine configuration from `docker inspect` on both nodes, compares the two argument lists, greps both retained logs and kernel journals, and (`--probe`) sends one completion with GPU sampling on both Sparks.
- **`scripts/cluster-soak.py`** — the mixed-workload soak with the two-node monitor and a PASS/FAIL verdict.
- **`docs/CLUSTER.md`** — the "changing an engine argument" runbook (there was none; a routine deploy deliberately cannot restart the engine), corrected timeout attribution, the measured tuning rows.
- **`README.md`** — the CI section described two workflows deleted on 2026-09-02; `DEPLOY_ON_PUSH` semantics (unset = deploy, contrary to the workflow's own header); the withdrawn "13 Gb/s per link" fabric claim; test counts.

## 9. Long-context validation

`orchestrator/scripts/validate_long_context.py --base-url http://127.0.0.1:8000/v1 --sizes 8192,32768,131072,262144 --max-output 128`, then `--sizes 500000`, real-tokenizer prompts via `/tokenize`, three needles at 2 % / 50 % / 97 % (**TESTED** 07:35–07:41Z, engine idle):

| Requested | Prompt tokens | Latency | Needles | 2026-08-29 (MTP on) |
|---|---|---|---|---|
| 8,192 | 8,119 | 1.3 s | 3/3 | — |
| 32,768 | 32,725 | 4.5 s | 3/3 | — |
| 131,072 | 131,005 | 26.1 s | 3/3 | — |
| 262,144 | 262,051 | **78.5 s** | 3/3 | 109 s |
| 500,000 | 499,805 | **242.4 s** | 3/3 | 248 s (449,844 tokens) |
| ~950,000 | — | **not run** | — | 878 s |

The 500K run was guarded (abort on head `MemAvailable` < 6 GiB; it never dropped below 28 GiB). 950K was **deliberately not run**: on 2026-08-29 that prefill exhausted unified memory and restarted the engine until the head had ~37 GiB of headroom, the head has 27–30 GiB today (§5), and the day had already cost two engine restarts. The 1M window is served (`max_model_len 1,000,000`, KV pool 1,663,201 tokens = 1.66× a 1M request) and 500K is proven; the practical tested ceiling on this head, as it is loaded today, is 500K. The `NV_ERR_NO_MEMORY` kernel lines logged 07:33:03–07:34:01Z are the engine's *start-up* allocation retries (the same pattern at every boot), not the 500K run.

## 10. Failure testing

On the isolated e2e tier (`scripts/e2e-stack.sh`, rebuilt from this branch, shared engines, a seeded QA user) — **TESTED**:

**A — router engine restart under Fast-mode chats** (07:18:51Z, `docker restart sf-local-ai-vllm-router-1`, replica back ~2.5 min later): eight sequential chats; q1–q2 before the restart answered normally; q3 and q4 received the `status` event "The model is restarting — waiting for it to come back…"; q4 waited 41 s and completed with 72 tokens; q5–q8 normal. q3 spanned the whole 120 s interactive window and exposed the empty-answer swallow in `continuation.py` (fixed above; the orchestrator had classified it correctly — `ModelUnavailable` after 120 s and 1 attempt).

**B — main-engine restart under a Smart chat and a video job** (07:29:13Z, a clean pair restart that also applied the worker-healthcheck change): the Smart chat, started while the engine was down, showed the "restarting" status at 0.2 s and was closed after 120 s as `failed` with the `MODEL_UNAVAILABLE` sentence (persisted on its `chat_requests` row and in the worker's log). The video job (`analysis_id 22`, submitted at 07:33Z while the engine was still down) ran probe → audio → transcript → frames → OCR → vision normally, then its **fusion** stage hit `APIConnectionError`, logged `llm.resilient what=json_completion … still waiting for the engine (60s of 1200s)`, saw the engine back after 75.4 s, completed fusion (84.4 s stage time) and delivered the answer 96 s after submission. No stranded row, no retry loop, no operator action.

**C — unplanned: the 07:05Z fault itself** (§1.1) — the full self-healing chain end to end: engine self-exit, Docker restart, worker self-restart, re-pair, 11 m 47 s.

## 11. Watchdog and timeouts (**VERIFIED**, decisions recorded)

- The watchdog (`compose/compose.dgx-spark.yaml`) is sound for its mandate: it arms only after a proven completion, counts only ≥100 s generation timeouts, restarts at 2/2 and stands down for 45 min. In the 2026-09-10 event it counted 1/2 at 21:34:25Z, then saw a fast 5xx (the engine had already died at +300 s) and correctly left the restart to the container healthcheck, which killed the API process; Docker's restart policy did the rest. The worker's own healthcheck restarts rank 1 when the head reports 5xx or is unreachable for 5 min after a 15-min grace, and the new rank 1 re-rendezvous; observed again at 06:26Z today (worker recreated at 06:26:18Z, head at 06:26:33Z, paired and serving by 06:31:41Z).
- Gap kept open, documented: the 300 s during which the head's API answers `/health` 200 while the collective is dead is `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS`, not `--distributed-timeout-seconds`. **Kept at 300 s** (Phase 15): chunked prefill bounds every RPC to one 8,192-token step, so a shorter clock would not abort a 1M-token prefill — but a first-shape Triton JIT compile has cost up to ~70 s inside a step and a swapping head can stall one, and the watchdog already proves a hang in ~5 min; the 2–3 minutes a 150 s clock would save per incident is not worth a false engine death until those two stalls are measured. No Prometheus rule catches the wedge phase (GPU 96 % at 13–17 W, tokens frozen, `up==1`) and **no Alertmanager is configured** — every alert of the last 11.7 days fired to nobody. Both are follow-ups (§13).

## 12. Security (**VERIFIED**)

- `.env` (0600), `.env.*`, `.runtime/`, `backups/`, `*.bak*` are gitignored; `git ls-files` carries no secret-shaped values; the `.env` edit was backed up under `.runtime/env-backups/` (ignored, 0700/0600). No secret value appears in any file or log this programme produced; `docker inspect` output was masked before being read.
- `worker.env` shipped to the worker carries only `MAIN_MODEL*`, `MODEL_MAX_CONTEXT`, `VLLM_PORT`, `TECHSARA_CLUSTER_MODE` and `CLUSTER_*` keys — no token.
- Findings for the owner, **not changed** (each needs a decision or root): the vLLM API on the head, the OCR and whisper engines answer on the LAN/tailnet without an API key (by the `.env` `PUBLISH_MODEL_PORTS` choice; note `PUBLISH_MODEL_PORTS=false` would bind the head to the Docker bridge and break the worker healthcheck and the watchdog in dual mode — the right fix is a host firewall rule or `--api-key`); `portainer` on `0.0.0.0:9000` with a writable Docker socket; `iperf3` on `*:5201` on both nodes; RDP on the worker's `*:3389/3390`; an interactive `alpine` container (`zealous_williamson`) with the production data volume mounted.

## 13. Known limitations and follow-ups

1. **The fault class is not closed on this vLLM build** (§1.1). The GDN Triton kernels in `0.26.1rc1.dev77+g6f91edf96` (nightly 2026-07-29) misalign on mixed prefill+decode batches whether or not spec-decode is on. The durable fix is a build whose GDN/FLA kernels carry the later fixes (candidates from the 2026-09-03 diagnosis: v0.28.0 with "GDN gates aligned with speculative tokens" #51812, the fused post-conv kernel work #51674, and later); it must be pulled to both nodes (same digest; the router and OCR share it), validated on sm 12.1 / CUDA 13 / the NVFP4 checkpoint / YaRN 1M, and accepted with `scripts/cluster-verify-engine.sh --probe` plus **`scripts/cluster-soak.py --minutes 120 --concurrency 10`** — the reproducer that fired at 27 minutes today. Until then, the worker-side analysis pipeline (concurrency 10, mixed) should expect a fault every few hours and needs its client patched (`interview-analysis-client/`); the orchestrator's own jobs now survive the reload.
2. **Router to the worker** — the single largest head-memory fix (≈22 GiB) — is specified, not done: a `router` Compose profile + `ROUTER_REMOTE_BASE_URL` in `launcher/techsara_cli/environment.py` (mirror `remote_ocr_url`), the launcher probing the remote engine instead of starting `vllm-router`, `scripts/router.sh` + `compose/compose.router.yaml` (mirror `scripts/ocr.sh` / `compose.ocr.yaml`, host-bound to 192.168.9.68:30002, same image digest, explicit KV budget), a `file_sd` scrape target, and tests for the profile logic. It touches the intent classifier on every chat turn and Fast mode, so it deserves its own change window.
3. **Head swap.** Until the router moves: stop the non-production residents the owner does not need (`pg-test`, `litellm-dgx`, `techsara-e2e-*` between QA runs, `portainer`, `zealous_williamson`) and the second vscode-server tree; do not add swap; lower `vm.swappiness` only after the residents are trimmed.
4. **Alerting**: add an Alertmanager and a route; a rule on `changes(process_start_time_seconds{job="vllm-main"}[1h]) > 1`; a wedge rule keyed on a blackbox *completion* probe rather than GPU utilisation; larger `chat_*` histogram buckets in `orchestrator/app/metrics.py` (they cap at 30 s).
5. **Container logs**: the head's json-file log (3 × 10 MB, ~1,600 NCCL lines per start) lost the incident window and both post-reboot start-ups; capture start-up logs to `.runtime/logs/` (done for today's start) and consider a larger `max-size` for the `vllm` service only.
6. **GPUDirect RDMA / MTU 9000** — maintenance-window experiments; the fabric is healthy without them.
7. **`MONITORING_ENABLED`** has no consumer in the repo (dead key); the monitoring stack shares the `sf-local-ai` project and survives `techsara down`.
8. The legacy root `docker-compose.yml` (pre-launcher, historical) still hard-codes `--enable-prefix-caching` and would bring MTP back through nothing — it carries no speculative flag — but it is not what any script runs.

## 14. Rollback

Engine only, ~6 minutes, no code change: in `.env` set `CLUSTER_SPECULATIVE_CONFIG='{"method":"mtp","num_speculative_tokens":1}'` and/or `MAIN_MODEL_ENABLE_PREFIX_CACHING=true`, then `./techsara up` from a shell on the head (or `scripts/deploy.sh --full`), then `scripts/cluster-verify-engine.sh --probe` (it will report the flags it finds). The launcher's previous behaviour (MTP on when the key is absent) is `git revert 8d587e7 2a8ac60` — but the `.env` line is the switch that matters, and it is explicit either way now. The orchestrator change is independent and rolls with a routine deploy; `LLM_INTERACTIVE_RECOVERY_S=0 LLM_RECOVERY_WINDOW_S=0` in `.env` turns the waiting off without a code change.
