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
7. **Candidate B — deployed to production 2026-09-12 08:39Z** (`docs/availability/CANDIDATE-B.md`,
   `scripts/cluster-ab.py`, `scripts/cluster-cpu.sh`): the post-`f6326f5` build
   (`nightly-385dce36…`, image `vllm/vllm-openai@sha256:819ec9c0…`, image ID `5a0f8b91…` on both
   nodes) with `--gdn-prefill-backend flashinfer` on both ranks (`MAIN_MODEL_IMAGE`,
   `CLUSTER_GDN_PREFILL_BACKEND`), `VLLM_USE_V2_MODEL_RUNNER=0` and
   `VLLM_ALLREDUCE_USE_FLASHINFER=0` in both ranks' process environment (`.runtime/engine.env`,
   2 variables, sha256-matched on the worker), `ulimits core: 1` and the kernel-cache volumes in
   force from this start. Both ranks logged `Using FlashInfer GDN prefill kernel
   (requested=flashinfer, head_k_dim=128).` on the first start (08:41:04Z, `Worker_TP0` and
   `Worker_TP1`) and on every restart a record captured (drill 16 × 3, drills 3 and 6; §5,
   `CANDIDATE-B.md` §6.3). The A/B matrix is measured (§5); the 120-min
   soak is in progress at the time of writing; the 48–72 h canary is **still unproven**. The
   secondary engine knobs (`--long-prefill-token-threshold` + the LONG lane,
   `--max-num-batched-tokens 4096` vs `8192`, `--max-num-seqs 10`, then Track A
   `--moe-backend flashinfer_b12x` fifth) are not run. Seven clean production days before the
   class is called closed. Record: `.runtime/logs/techsara-up-candidateB-20260912T0839Z.log`.
8. Docs: `CONTRACT.md` v2, `ADR-0002`, `SLO.md` (SLO B = request continuity availability; the
   acceptance checklist), `MEMORY-BUDGET.md`, `RUNBOOK.md`, `ARCHITECTURE.md`, `docs/MONITORING.md`,
   this report.

Not done here, tracked: a released vLLM build that closes the fault class does not exist
(`VLLM-UPGRADE-RESEARCH.md`) — Candidate B is deployed and under soak, its promotion decision
waits for the 48–72 h canary; router-to-worker (memory only); no Alertmanager/notification
channel (needs credentials); the second tenant's client-side patches are the owner's call;
Option C (+2 Sparks) is the only way to serve answers *during* a reload; drills 1, 2 and 7 and
a forced wait past `LLM_QUEUE_MAX_WAIT_S` were not run today (§5); the wedge rule's blind spot
under a long solo prefill (§7.2) is open.

## 5. Verification

What was deployed and what the live drills of 2026-09-12 measured. Every drill record is under
`.runtime/drills/<utc>/` in the deploy checkout (gitignored) and prints UTC and IST; the wrapper
logs are `.runtime/logs/drill*.log`. Prometheus (`127.0.0.1:9090`) holds the controller series
for the whole day. `SLO.md` §5 carries the same numbers against the objectives; §6 there is the
acceptance checklist with a status per row.

### 5.1 Deployment, 08:29Z–08:44Z (14:00–14:14 IST)

| UTC | What | Record |
|---|---|---|
| 08:29Z | routine `./techsara up` on the availability branch: the running head left untouched (preserve flag; the launcher warned it still ran the pre-controller definition), the worker sentinel shipped and started (`sha256` of `sentinel.py`/`common.py` matched, token equal on both nodes), the engine controller started (code sha `273824e40483`, healthy after 7 s), **the legacy shell watchdog removed** (`sf-local-ai-vllm-watchdog-1`), the orchestrator restarted with continuity, admission and the engine-state client (`healthy/degraded contract verified`) | `.runtime/logs/techsara-up-availability-20260912T0829Z.log` |
| 08:36:57Z | Prometheus restarted with the new configuration (`process_start_time_seconds{job="prometheus"}`; last successful config load 08:36:59Z): **13 groups / 85 rules** (`availability`, `vllm-availability` 18 alerts, `vllm-state` 5 recording rules among them) | `GET 127.0.0.1:9090/api/v1/rules`, counted 10:53Z; `prometheus_config_last_reload_success_timestamp_seconds` |
| 08:39Z | **one pair reload onto candidate B**: worker container 08:39:29Z, head 08:39:59Z; `sf-local-ai-worker_kernel-cache` volume created; head log: GDN line 08:41:04Z on both ranks, `Dynamo bytecode transform 6.67 s`, `Compiling a graph … 11.21 s`, `torch.compile took 20.56 s`, CUDA graphs 51 piecewise + 35 full (≈ 10 s), `init engine … 61.28 s`, `Application startup complete` 08:43:45.67Z (**≈ 3 m 46 s** container start → API accepting); the launcher counted `vllm: ready after 229s`; controller healthy 7 s later; verify probe 23 pass / 2 fail (the two fails are the since-boot Xid baseline, 2 per node) | `.runtime/logs/techsara-up-candidateB-20260912T0839Z.log`, `.runtime/incidents/20260912T084520Z/head-logs-1.txt`, `.runtime/incidents/20260911T223140Z-vllm-down/80-verify-engine-B-first-start.txt` |

### 5.2 The drills

All on candidate B, warm kernel caches after the first start. "Detection" is the time from the
break to the controller leaving READY; "READY" is the time from the break to READY through the
full readiness sequence (non-streaming, streaming, token progress, both GPUs, rank alive).

| Drill | Record | Detection | READY | What the record shows |
|---|---|---|---|---|
| 16 — three coordinated restarts (`POST /recover`, manual) | `20260912T084519Z/drill-16-tp2-repeated-startup.log`, **PASS 19/0** | — | **188 / 186 / 180 s** after each request | worker restarted 2–3 s before the head every time (08:45:33 vs 08:45:36, 08:50:41 vs 08:50:44, 08:55:49 vs 08:55:52); the GDN line on `Worker_TP0` and `Worker_TP1` every time; a real 4-token completion after each; `torch.compile` 4.96 / 3.27 s and `init engine` 27.2 / 28.8 s on the reuse starts (`.runtime/incidents/20260912T085030Z`, `…085538Z` head logs) vs 20.56 / 61.28 s cold; budget 0 of 3 left at 08:58:39Z |
| 3 — `kill -9 VLLM::Worker_TP1` on the worker | `20260912T094545Z/drill-3-kill-worker-rank-process.log`, 11 pass / 1 fail | **6 s** (`worker_rank_dead`) | **176 s** | worker container restarted 7 s after the kill (09:45:46 → 09:45:53), head 3 s later; worker before head; rank alive; canary TTFT 0.051 s; verify probe: both GPUs 93 % / 91 %. The one FAIL is `cluster-verify-engine.sh --probe` exiting non-zero on its since-boot Xid count (2 per node, unchanged all day) and, from this drill on, one line matching its fault pattern in the worker container's retained log (`died unexpectedly` is in the pattern, `cluster-verify-engine.sh:94`; consistent with the executor logging the rank this drill killed — the captured diagnostics do not show the line, so this is an inference) — re-baselined in commit `e3faf59` |
| 4 — `kill -9 VLLM::EngineCore` on the head | `20260912T095049Z/drill-4-kill-head-enginecore.log`, 11 pass / 1 fail | **4 s** (`head_engine_dead: /health 503`) | **172 s** | both containers restarted, worker first (09:51:00.45 vs 09:51:00.77); canary TTFT 0.044 s; both GPUs 93 % / 92 %; the same baseline FAIL |
| 5 — `kill -9` the head's `vllm serve` API, **first run** | `20260912T095548Z/drill-5-kill-head-api.log`, 14 pass / 5 fail | 5 s (DEGRADED 09:55:54Z) | **767 s** | **the finding of the day.** Docker's restart policy brought the head back in 5 s (RestartCount 1); the controller went STARTING and did **not** re-pair the worker, so the new head waited at the rendezvous for a rank still joined to the old head. The head was restarted again at 10:01:23Z (RestartCount 2 — the shape of the head's last-resort healthcheck tier, 8 misses after the start period) and the worker at 10:06:06Z (RestartCount 1, the worker's tier); READY at 10:08:36Z. Fixed the same day: commit `6a667f6`, rule "head restarted externally → worker re-paired" (category `head_restarted_externally`, outcome `repaired_worker`, no head restart, budget untouched) |
| 15 / 14 on that run | same record, `drill-5-chat-{a,b}.json` | — | — | both `POST /chat` (5 s apart, same `intent_id`) answered HTTP 200 after 779.9 / 774.9 s; the status line `Main model is recovering—your request is safely queued.` was read; the same generation completed (143 chars), first token after READY; `llm_queued_generations` rose (1 at 09:56:15Z) and **stayed 1** after the resume (the second finding, fixed in `6a667f6`); one assistant row for the intent sent twice. The drill's own "sentence not shown" FAIL was an ASCII-escaped em-dash in its grep (fixed in `6a667f6`) |
| 6 — `docker restart -t 10` of the head by hand, no lock | `20260912T104332Z/drill-6-restart-head-only.log`, 5 pass / 1 fail (baseline) | **3 s** (DEGRADED 10:43:36Z) | **165 s** | the controller re-paired the worker in 12 s (worker restarted 10:43:46Z, `head restarted outside the controller; worker re-paired`), no second head restart (head RestartCount 0, worker 0); both GPUs 93 % / 90 % |
| 5 — **re-run** after `6a667f6` | `20260912T104624Z/drill-5-kill-head-api.log`, 17 pass / 2 fail | **5 s** | **161 s** | STARTING with `re-pairing the worker` at 10:46:31Z; worker restarted 10:46:39Z, 11 s after the new head; incident `20260912T104339Z` category `head_restarted_externally`, `budget_remaining=3`; the two FAILs are the drill's own barrier (it still expected RECOVERING; the re-pair shape was made a pass in `e3faf59`) and the Xid baseline |
| 15 / 14 on the re-run | same record | — | — | both chats HTTP 200 after 174.8 / 169.9 s, the sentence read (compared unescaped), the same generation completed (130 chars) after READY, `llm_queued_generations` 1 → **0 within 60 s**, `llm_queue_wait_seconds` count 1 / sum 171.2 s, `llm_resumed_generations_total{outcome="resumed"}` = 1 after the orchestrator restart of 10:42Z; one assistant row for the intent sent twice |

Not run today: drills 1 and 2 (monitoring-only failures), drill 7 (worker-only restart), drills 8
and 9 (network blips, manual), a wait forced past `LLM_QUEUE_MAX_WAIT_S=900` (the longest wait was
780 s), the two-request admission-lane test. No drill was re-run after `e3faf59`; the records of
drills 3, 4, 5 (re-run) and 6 therefore carry a FAIL verdict on their face for the two assertion
shapes that commit corrected, and the numbers above are read from their PASS lines.

On the "engine=primary" line of drills 15: the drill's chat client records `"engine": null` in
`drill-5-chat-{a,b}.json` — the terminal frame of a resumed generation carried no `engine` field
— and the assertion treats an absent field as the primary (`engine_failure_drills.sh:674`). The
positive proof that no other model answered is that none exists to answer (`fallback.py`
deleted; `grep -rn fallback orchestrator/app` finds no answer path) and the SQL of `SLO.md` §3,
which was not run today.

### 5.3 The A/B matrix and the soak

`docs/availability/ab/compare-A-vs-B-20260912.md` and the two SUMMARY files. A (production image
`24f2f897…`, run `20260912T0514Z`, 10 phases, 856 s) and B (`819ec9c0…`, run `20260912T0859Z`,
11 phases, 1,639 s), same harness, same seed: B **0 failed requests, 0 Xid, 0 restarts, no
alarms** over 11 phases; the ~950K needle 3/3 (949,9xx tokens, 1,189 tok/s prefill, 799 s);
`mixed` 10 min at c=10: **692 vs 550** requests; c16 306.7 vs 269.6 tok/s; c10 TTFT p95 0.32 vs
0.60 s; 32K prefill 8.6K vs 7.8K tok/s; 128K 5.2K vs 5.0K tok/s (TTFT 25.0 vs 26.3 s = 0.95 ×,
pass criterion ≤ 1.3 ×); one 2.8 s inter-token stall in `c10_short` on B. A's 950K figure was
not measured: the old shell watchdog restarted the head under the first A run at 01:20Z (§7.1).

The 120-min soak on B (`scripts/cluster-soak.py --minutes 120 --concurrency 10`, started 10:50Z,
`.runtime/logs/soak-B-20260912T1050Z.log`) is **in progress at the time of writing; result
appended by the lead.** The 48–72 h canary has not started: **still unproven.**

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
- **Candidate B is 11 clean A/B phases and one drill day old.** The upstream MTBFs are 9–72 h;
  the soak is in progress and the 48–72 h canary is still unproven. Rollback is §6.5 of
  `CANDIDATE-B.md` (a pair reload; requests queue meanwhile).
- **The wedge rule cannot tell a long solo prefill from a wedge** (§7.2): with budget available
  the controller would have restarted the pair under the ~950K needle. Open, owner: controller.

## 7. Two more events of 2026-09-12, both self-inflicted

### 7.1 01:20:01Z (06:50 IST) — the shell watchdog restarted the head under the A-baseline needle test

Source: `.runtime/incidents/20260911T223140Z-vllm-down/00-TIMELINE.md` (addendum),
`.runtime/ab/A-baseline-20260912T0109Z/needles.txt`, Prometheus `up{job="vllm-main"}` and
`process_start_time_seconds{job="vllm-main"}`.

| UTC | IST | Event |
|---|---|---|
| 01:12:03 | 06:42:03 | `validate_long_context.py --sizes 32768,131072,262144,950000` against production (the A baseline): 32K 4.9 s, 128K 27.1 s, 262K 83.2 s, needles 3/3 each |
| ≈01:14 | ≈06:44 | the 950K request (949,915 prompt tokens) submitted while the second tenant had 3–4 requests running: the engine logged `Deferred: 1`, `Running: 3–4, Waiting: 2–4`, 0.2–0.6 tok/s, KV usage 25 % → 38 % over 6 min |
| 01:17:01 | 06:47:01 | shell watchdog: probe TIMED OUT (120 s) (1/2) — its 2-token probe was queued behind the 950K prefill |
| **01:20:01** | **06:50:01** | second timeout → `docker restart -t 30 sf-local-ai-vllm-1` (SIGTERM 01:20:02, SIGKILL 01:20:32, new head process 01:20:32Z per `process_start_time_seconds`); the 950K client saw `Server disconnected` at 381.1 s; the tenant's 3 running + 2 waiting requests were killed; `up{job="vllm-main"}` 0 from 01:20:30Z |
| 01:21:38 | 06:51:38 | the worker's healthcheck restarted the worker (RestartCount 3); re-paired |
| 01:25 | 06:55 | `up` 1 again; serving |

Impact: one A-baseline measurement lost (A has no 950K figure; the compare table shows `—`),
the second tenant's five requests failed, ≈ 5 min of no inference at night. Lessons, all folded
into the programme the same day: (1) "two probe timeouts = hang" cannot distinguish saturation
from a wedge — the v2 controller's rule 5 exempts a canary that times out while the token
counters move, bounded by `CANARY_STARVATION_S=300`; (2) a ~950K prefill on this engine takes
≈ 10–13 min and starves everything else — the reason for the exclusive LONG lane (CONTRACT §6.7);
(3) never run the 950K test with the shell watchdog armed — it was removed at 08:29Z.
`CANDIDATE-B.md` §8.2 attributes this kill to the head's healthcheck; the 30 s SIGTERM→SIGKILL gap
is the watchdog's `docker restart -t 30`, and the timeline addendum is the record.

### 7.2 09:04:15Z–09:16:00Z (14:34–14:46 IST) — the v2 controller called the ~950K needle on B a wedge; only the spent budget stopped a restart

Source: Prometheus `techsara_vllm_state_code`, `techsara_vllm_generation_frozen_seconds`,
`techsara_vllm_synthetic_probes_total{outcome="timeout"}`,
`techsara_vllm_recovery_attempts_total{outcome="budget_exhausted"}`,
`techsara_vllm_last_failure_category`, `llm_breaker_transitions_total` (all queried 10:55Z);
`docs/availability/ab/B-20260912T0859Z-SUMMARY.md` (`needle_950k`: controller states seen
`DEGRADED, WEDGED`).

The B matrix's `needle_950k` phase (one request, 949,9xx tokens, 799 s, answered 3/3) ran on an
otherwise idle engine from ≈09:02Z. vLLM's own counters, as Prometheus scraped them from the
head (`job="vllm-main"`, 1-min samples): `vllm:prompt_tokens_total` **flat at 694,469 from
09:03Z to 09:15Z**, then +950,436 at 09:16Z — the whole prompt is counted when the request
finishes its prefill, not per chunk; `vllm:generation_tokens_total` flat at 22,660;
`vllm:num_requests_running` 1; `vllm:kv_cache_usage_perc` **rising 0.088 → 0.557** over the same
minutes (the prefill's real progress); `techsara_vllm_head_metrics_ok` 1 throughout. So neither
counter the controller reads moved — its `generation_frozen_seconds` climbed from 15 s (09:02Z)
to 680 s (09:14Z) with 1 request running — and the 4-token canary queued behind the prefill:
12 consecutive timeouts (09:04Z → 09:16Z).
The controller went DEGRADED at 09:02:15Z, **WEDGED at 09:04:15Z**, and attempted a recovery;
the attempt was refused with `budget_exhausted` (counter 0 → 1 at ≈09:05Z,
`last_failure_category=budget_exhausted`) because drill 16 had spent the 3-per-hour budget at
08:58:39Z. The pair was **not** restarted; the needle completed; the controller returned to
DEGRADED at 09:16:00Z and BUSY at 09:16:15Z. The orchestrator's breaker opened on the WEDGED
verdict at 09:04:30Z and went HALF_OPEN at 09:16:15Z — a person's request in that window would
have been queued, not lost.

With budget available the controller would have restarted the pair under the needle: the same
design flaw as §7.1 (a legitimate long prefill is indistinguishable from a wedge by the token
counters alone), reproduced on the v2 controller. The saturation exemption of rule 5 requires the
counters to move; on this build they do not move during a single long prefill, while
`vllm:kv_cache_usage_perc` does. **Open finding,
owner: controller workstream.** The options are theirs to weigh: a controller-visible signal that
a long prefill is in progress (the LONG lane's admission, or a per-request progress figure from
the engine), or a longer bound when exactly one request is running. Until then a >131K prompt through the raw port (the second tenant, the A/B harness) can
cost a pair restart; the orchestrator's LONG lane sends the same shape.
