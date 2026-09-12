# Incident report — main model unavailable, 2026-09-11 22:15:50Z → 22:26:49Z

| | |
|---|---|
| **Incident id** | `20260911T221550Z` (evidence bundle: `.runtime/incidents/20260911T223140Z-vllm-down/`, gitignored, redacted summary below) |
| **Service** | main model `Qwen/Qwen3.6-35B-A3B-NVFP4`, one vLLM engine TP=2 across spark-0e68 (head, rank 0) and spark-476e (worker, rank 1), vLLM `0.26.1rc1.dev77+g6f91edf96`, image `vllm/vllm-openai@sha256:24f2f897…` (identical on both nodes; no configuration drift — `REVIEW-MANIFEST.md` §3) |
| **Start** | **2026-09-11 22:15:50.297 UTC** = **2026-09-12 03:45:50 IST** (rank 1 CUDA fault; last successful completion 22:15:48.761Z) |
| **End** | **2026-09-11 22:26:49 UTC** = **03:56:49 IST** (first successful real completion after the reload; API accepting from 22:26:19Z) |
| **Duration** | **11 min 00 s** of no inference; **5 min 03 s** of it reported healthy by every existing signal; 5 min 30 s reported DOWN (correctly) |
| **Detected by** | vLLM's own 300 s RPC timeout (22:20:49Z), after the shell watchdog had seen one 120 s probe timeout (22:18:15Z) without acting; a person saw the Grafana "vLLM DOWN" tile during the reload |
| **Impact** | **TechSara users: none** (zero `/chat` requests in 22:10–22:35Z; `llm_retry_total`, `llm_engine_unavailable_total`, `chat_requests_interrupted` all 0). **Second tenant** (interview-analysis pipeline on the worker, calling the raw port over 10.100.184.2): 9 in-flight requests → HTTP 500 at 22:20:49Z; every attempt until 22:26:19Z → connection refused; its earlier in-flight attempts hung up to 5 min. 12 queued requests were answered from 22:27:02Z. |
| **Root cause** | GPU MMU fault (kernel `Xid 13` + `Xid 31`, `FAULT_PTE VIRT_READ @0x0`) inside vLLM's Triton GDN prefill kernel (`fused_gdn_prefill_post_conv.py:215` ← `qwen_gdn_linear_attn.py:1348`) on rank 1 under 9 concurrent mixed prefill+decode requests — a known, **unclosed** upstream fault class (vLLM #49926, #37431; `VLLM-UPGRADE-RESEARCH.md`). The fault is the trigger; **the outage length is a detection and choreography problem**, itemised below. |

## 1. What happened, precisely

All times UTC (IST = +05:30). Full table with per-signal columns: `.runtime/incidents/…/00-TIMELINE.md`.

| UTC | IST | Event |
|---|---|---|
| 22:15:48.761 | 03:45:48 | last successful completion (tenant, HTTP 200); 9 requests running, throughput normal |
| **22:15:50.297** | **03:45:50** | rank 1 (`VLLM::Worker_TP1`, Spark 2): `RuntimeError: Triton Error [CUDA]: misaligned address` → `c10::AcceleratorError` → `terminate called`; kernel Xid 13 + 31, pid 1415964. apport starts writing a **3.5 GB** crash report of the 25 GiB process |
| 22:16:05 | 03:46:05 | head engine logger: `0.0 tokens/s, Running: 9` — Prometheus `generation_tokens_total` frozen at 2,138,504 from the 22:16:00 sample; `up{job="vllm-main"}` still 1; `/health`, `/v1/models`, `/metrics` all 200 (they are answered by the API process, not the engine) |
| 22:16:50 | 03:46:50 | head EngineCore: `No available shared memory broadcast block found in 60 seconds` — an INFO line, repeated every minute; no health signal changes |
| 22:18:15 | 03:48:15 | shell watchdog: `generation TIMED OUT (120s) (1/2)` — first external detection; by design it needs two |
| 22:19:49 | 03:49:49 | apport finishes the crash report; only now can the worker's executor reap the dead rank |
| 22:20:41 | 03:50:41 | worker executor: `Worker proc VllmWorker-1 died unexpectedly` → shuts down |
| **22:20:49** | **03:50:49** | head: `RPC call to sample_tokens timed out` (`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS`=300 s) → `EngineDeadError` → 10 × HTTP 500 → API server exits; the container's `vllm serve` process hangs in teardown. Watchdog: "fast HTTP error (94 s) — not counted" |
| 22:21:00 | 03:51:00 | Prometheus `up{job="vllm-main"}`=0 → **Grafana "vLLM DOWN"** (the screenshot) |
| 22:21:10 → 22:22:44 | | worker container restarts (#1), the new rank joins the **old** head's TCP store, dies with `Connection reset by peer` when the old head is killed |
| **22:22:47** | **03:52:47** | head healthcheck: 4th consecutive connection-refused miss → `kill -9` → Docker restarts the head (RestartCount 1); worker restarts again (#2) 500 ms earlier |
| 22:22:56 → 22:26:19 | | new pair: API process 22:22:56, NCCL 22:23:17, weights 22:24:41 (70 s), KV cache + CUDA graphs 22:25:53, `Application startup complete` 22:26:19 |
| **22:26:49** | **03:56:49** | first real completion (watchdog probe) — "PROVEN ready"; tenant traffic answered from 22:27:02 |

### Why the screenshot looked the way it did
- **vLLM DOWN** = `up{job="vllm-main"}==0`: true for 22:21:00–22:26:30 (listener closed). Correct — but it says nothing about the 5 minutes before, when the engine was already dead and `up==1`.
- **Running / Waiting / Tokens: No data** = the series vanished with the target; **Requests/sec 0** = `rate()` over a 5-minute window whose samples had stopped moving.
- **Spark 1 GPU 96 %** = rank 0 spinning in the NCCL collective waiting for rank 1 (35 W — busy-wait, not work), then the reload; **Spark 2 0 %** = rank 1 dead, then loading weights (CPU-bound).
- **Spark 1 memory 82 % vs Spark 2 28 %** = the head hosts rank 0 (25 GiB) + router (21 GiB) + embed + reranker + whisper + PostgreSQL + tooling with 12 GiB in swap; the worker hosts rank 1 + OCR + whisper (`MEMORY-BUDGET.md`). Not a cause: the fault was on the node with 40+ GiB free.

## 2. Why it took 11 minutes (the five independent delays)

| # | Delay | Cause | Fix in this programme |
|---|---|---|---|
| 1 | 4 min 51 s — dead rank unreaped | apport (host `core_pattern` pipe) held the 25 GiB process while writing a 3.5 GB report; the executor only notices a *reaped* child | `ulimits: core: 1` on every vLLM container — the kernel aborts a pipe dump at `RLIMIT_CORE==1` before apport is spawned (measured 18.3 s → 1.28 s on the same image; `core: 0` does **not** work: apport 2.28 reads the core regardless) |
| 2 | 5 min 00 s — head blind to a dead remote rank | vLLM has no cross-node death signal; only the 300 s RPC timeout; `/health`, `/v1/models`, `/metrics` are served by the API process | worker **sentinel** (sees the rank gone or a fatal signature in ≤ 5 s and reports it; restarts the worker only on the controller's `POST /restart`) + head **controller** (treats a dead rank / a fault newer than the head's start as `worker_rank_dead` and restarts the pair at once, worker first; frozen-token + canary detection for the wedge case in ≤ 2.5 min) |
| 3 | watchdog needed 2 × 120 s timeouts + 60 s gap; the 300 s engine timeout won the race every time | design of the shell watchdog (one signature, two strikes) | replaced by the controller's nine-state machine with a real streaming canary every 30 s |
| 4 | 1 min 58 s — head stuck in teardown | healthcheck needs 4 × 30 s connection-refused misses; `unless-stopped` never fires because the process does not exit | controller `head_api_dead` on two observations (≤ 15 s) |
| 5 | worker re-paired with the old head's store, failed, restarted twice | the worker's rejoin loop cannot know the head is being replaced | coordinated choreography: sentinel restarts the worker **first**, then the head, under one lock, then a real canary decides READY |

Contributing: a request that arrived during the outage was neither kept nor told the truth (the second tenant's in-flight requests got HTTP 500, then connection refused; an orchestrator user would have seen a spinner and a generic error); the wedge phase had no alert rule and no Alertmanager is configured (every alert of the last 12 days fired to nobody — still true, now stated in `docs/MONITORING.md`); the second tenant runs at the exact load shape that triggers the fault and calls the raw port, so it gets no orchestrator-side protection (its client patches: `docs/ISSUE/interview-analysis-client/`).

## 3. Hypothesis matrix (A–L) — verdicts

| | Category | Verdict | Evidence |
|---|---|---|---|
| A | monitoring-only failure | **ruled out** as the cause; **confirmed** as a *symptom* (5 min false-UP, then a true DOWN) | `40-prometheus-window.txt`, head log `/health` 200 lines 22:16–22:20 |
| B | API alive / EngineCore dead | **confirmed** for 22:15:50–22:20:49 (API answered, engine hung), then API exited | head log 35307–35502 |
| C | distributed rank failure | **confirmed**: rank 1 process died; rank 0 spun; the restarted worker joined the old group | worker log 22:15:50, 22:22:44; head 22:16:50 |
| D | configuration drift | **ruled out**: rendered argv == running argv on both ranks, same image ID, same healthchecks | `05-cluster-verify-engine-probe.txt`, `REVIEW-MANIFEST.md` §3 |
| E | speculative decoding | **ruled out** as a factor tonight (MTP off since 06:26Z on both ranks); the fault fires without it | `03-head-cmd.json`, `23-worker-healthcheck-cmd.json` |
| F | CUDA / driver fault | **confirmed** (Xid 13 + 31 on Spark 2 at 03:45:50 IST); driver 580.173.02 / CUDA 13.0 unchanged since 09-01 | `32-worker-kernel-apport.txt` |
| G | unified-memory pressure | **ruled out** as cause (Spark 2 had 40–45 GiB available; no OOM/`NV_ERR_NO_MEMORY` in the window); **contributing** to reload time on the head (swap-out 1,286 pages/s at 22:30Z) | `52-swap-timeseries.txt`, `30-head-kernel-grep.txt` |
| H | scheduler / capacity wedge | **confirmed** as the *form* the failure took on the head (9 running, tokens frozen, timeout eventually terminated it) | Prometheus `num_requests_running`=9 frozen |
| I | NCCL / RDMA | **ruled out**: both rails ACTIVE, `Using network IB`, zero RoCE errors, MTU matched; the collective hung because a peer died, not because the fabric did | `04-`, `06-cluster-doctor.txt`, `05-` |
| J | container / restart-loop problem | **confirmed** (delays 1, 4, 5 above); no loop: RestartCount head 1, worker 2 | `01-`, `21-` |
| K | port / routing | **ruled out** (listener bound 0.0.0.0:8000 until the API exited; Prometheus target correct; orchestrator reached `vllm:8000` before and after) | `70-orchestrator-window.log` |
| L | other workload on Spark 1 | **ruled out** as cause: the 96 % was rank 0's spin (35 W); router/embed/reranker/whisper idle in the window; no benchmark running; `litellm-dgx` and `zealous_williamson` are stray but idle | `50-head-system-state.txt`, `53-head-memory-map.txt` |

## 4. Corrective actions (this programme)

Implemented on `feat/vllm-availability-2026-09-12` (→ `dev` → PR to `main`), to `CONTRACT.md` v2
(strict one-model mode: only `nvidia/Qwen3.6-35B-A3B-NVFP4` answers a person; nothing stands in
for it while it reloads):
1. `ulimits: core: 1` on head, worker, router, OCR (`compose/*.yaml`).
2. Engine controller (`monitoring/engine-controller/controller.py`, compose service
   `engine-controller`, started by `./techsara up`) — replaces the shell watchdog and is the
   **single recovery authority**; nine states; the five-step readiness sequence (non-streaming
   completion, streaming completion, token-counter progress, both GPUs observed working, rank
   alive) before any READY and a real streaming canary between; coordinated recovery with lock,
   budget (3/h, manual included), cooldown; `/state`, `/metrics`, loopback `POST /recover`.
3. Worker sentinel (`sentinel.py`, compose service `vllm-worker-sentinel`, shipped by
   `scripts/cluster-sync.sh` on every `up`) — sees a dead rank or a fatal signature in ≤ 5 s and
   reports it; restarts the worker **only on the controller's `POST /restart`**
   (`SENTINEL_AUTONOMOUS=0`); bound to the RoCE rail-A address, token-gated. The Docker
   healthchecks become report-only with one last-resort kill tier
   (`VLLM_HEALTHCHECK_KILL_AFTER`, default 8 misses = 4 min).
4. Orchestrator — **request continuity and admission, no fallback answer**: circuit breaker and
   controller-state client (`app/breaker.py`, `app/engine_state.py`; a streamed call settles its
   permit on the first token); `app/continuity.py` accepts and durably queues every request
   class while the primary is not READY (row status `queued`, the exact line
   `Main model is recovering—your request is safely queued.`, the SSE stream kept alive), waits
   on the READY event, and resumes the **same** generation exactly once; the resume sweep at
   READY and at start-up; `app/admission.py` with two lanes (NORMAL ≤ 131,072 tokens, 10
   concurrent; LONG one at a time onto an idle engine, the trigger shape of this incident) with
   a durable, truthful wait. The v1 router-as-answer path (`app/fallback.py`, `FALLBACK_*`,
   `llm_fallback_active`) is deleted, not disabled (ADR-0002, "Fallback answer model rejected by
   product requirement").
5. Monitoring: `engine-controller` scrape job, `cluster:vllm_service_state:code` with state 7 =
   `QUEUEING` derived from `llm_queued_generations`, snapshot freshness
   (`techsara_vllm_generated_at_seconds`), `MONITORING_UNKNOWN` distinct from DOWN, the alerts of
   CONTRACT §7.4 (`VllmPrimaryDown`, `VllmReadyUnproven`, `VllmRequestsQueued`, `VllmQueueDrained`
   among them; stable alert identity — no live state name in labels), `VllmDown` →
   `VllmScrapeTargetDown` (warning), dashboard state tiles, promtool unit tests.
6. Scripts: `scripts/lib/engine-lock.sh` (taken by `cluster-up/down/recover.sh`, `deploy.sh` and
   the launcher's own `up`), `scripts/cluster-recover.sh` (follows *its* recovery; `--force
   --clear-kernel-cache`), `scripts/recovery-tests/engine_failure_drills.sh` (drill 15 proves the
   queue and the same-generation resume), `cluster-status.sh` controller section,
   `cluster-doctor.sh` false-FAIL fix; the launcher generates `ENGINE_HEAD_API_URL` and
   `ENGINE_CONTROLLER_URL` from the real bind address and layout; compiled-kernel cache volumes
   on both ranks (`/root/.cache/vllm`, `/root/.cache/flashinfer`).
7. **Candidate B plan** (`docs/availability/CANDIDATE-B.md`, `scripts/cluster-ab.py`,
   `scripts/cluster-cpu.sh`): the post-`f6326f5` build (`nightly-385dce36…`, the first with the
   FlashInfer SM120 GDN prefill kernel) with `--gdn-prefill-backend flashinfer` on both ranks
   (`MAIN_MODEL_IMAGE`, `CLUSTER_GDN_PREFILL_BACKEND`) is evaluated **first**, one variable at a
   time, behind the 120-min soak and the research report's §6.4 criteria; then the secondary
   engine knobs (`--max-num-partial-prefills 1`, `--max-long-partial-prefills 1`,
   `--max-num-batched-tokens 4096` vs `8192`, `--max-num-seqs 10`) and, fifth, Track A
   `--moe-backend flashinfer_b12x`. Seven clean production days before the class is called closed.
8. Docs: `CONTRACT.md` v2, `ADR-0002`, `SLO.md` (SLO B = request continuity availability; the
   acceptance checklist), `MEMORY-BUDGET.md`, `RUNBOOK.md`, `ARCHITECTURE.md`, `docs/MONITORING.md`,
   this report.

Not done here, tracked: a released vLLM build that closes the fault class does not exist
(`VLLM-UPGRADE-RESEARCH.md`) — Candidate B needs a change window; router-to-worker (memory
only); no Alertmanager/notification channel (needs credentials); the second tenant's client-side
patches are the owner's call; Option C (+2 Sparks) is the only way to serve answers *during* a
reload.

## 5. Verification

Filled in from the drills and the soak (see `SLO.md` §5 and the pull request): each drill records UTC + IST timestamps, the state transitions observed through `/state`, the time to RECOVERING and to READY, and what happened to the orchestrator request issued during the drill: it must have been accepted, told `Main model is recovering—your request is safely queued.`, and resumed on the **same** generation by the main model once READY (drills 5, 14, 15; the exactly-once SQL of `SLO.md` §3 returning no rows). No drill may show an answer from another model.

## 6. Remaining risks

- The fault will recur at roughly the same rate until an upstream fix or a validated candidate
  (Candidate B first); the programme bounds each occurrence to ≈ 4–6 min of the primary
  answering nothing, with every request kept and resumed — not zero downtime.
- **Nothing answers during a reload, by requirement.** Every request class waits for the reload;
  the wait is truthful and durable. Answers *during* a reload need Option C (+2 Sparks).
- The router is a single instance on the head and a classifier only; a head-node failure takes
  the primary and routing together (Option C, or the router to the worker for memory).
- The second tenant calls the raw port and is outside the breaker, the queue and the admission
  lanes; it still sees HTTP 500 / connection refused during a reload.
- Nothing pages a human: alerts are visible on Prometheus/Grafana only.
