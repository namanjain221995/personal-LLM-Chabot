# Engine availability — the shared contract (v2, strict one-model mode)

> **v2 (2026-09-12 06:15 IST).** Product requirement: **only `nvidia/Qwen3.6-35B-A3B-NVFP4` may generate a user answer**, as one TP=2 instance (Spark 1 = rank 0, Spark 2 = rank 1). No 8B fallback answer, no external model, no second answer-generating model. The router stays an internal classifier only. During a recovery, requests are accepted and **durably queued**, the person reads a truthful message, and the **same logical generation resumes** when the main model is READY. v1 sections that spoke of a fallback answer are superseded by §8.3 v2 and §2 v2 below; v1 text is kept where it still holds.

Every workstream in the 2026-09-12 availability programme builds against this
file. It is the source of truth for the state names, the signals behind them,
the HTTP/JSON shapes, the metric names and the file ownership. Change it here
first; nowhere else.

## 1. The system this describes

- Primary engine: one vLLM engine, TP=2 across two DGX Sparks. Head container
  `sf-local-ai-vllm-1` (node rank 0, the only HTTP endpoint, host network,
  `0.0.0.0:8000`), worker container `sf-local-ai-worker-vllm-worker-1` on
  Node 2 (node rank 1, `--headless`, host network). The two ranks form ONE
  torch.distributed process group: a restarted head is a new group that the
  old worker can never rejoin, and a dead worker rank hangs the head's
  collective until vLLM's `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` (300 s).
- **No fallback answer engine (v2).** The router `vllm-router:30002`
  (`Qwen/Qwen3-VL-8B-Instruct-FP8`) remains **only** for internal routing
  (intent classification, freshness routing, frame captions) and must never
  produce a user-facing answer. Its health is a DEGRADED signal for routing,
  never a serving path. `FALLBACK_ENABLED` is removed from the product; the
  code path that answered from it is deleted, not merely disabled.
- Orchestrator: `sf-local-ai-orchestrator-1`, reaches the head at
  `http://vllm:8000/v1` (`vllm` → `host-gateway` in dual mode).

## 2. Top-level states (exactly these nine; codes are stable)

| code | state | meaning | who decides |
|---|---|---|---|
| 0 | `MONITORING_UNKNOWN` | telemetry cannot be observed; inference status unproven. **Never rendered as DOWN.** | controller when it cannot observe; Grafana `noValue`; alert on `absent()` |
| 1 | `STARTING` | containers up, model loading; no canary success yet since the head started; cold-start budget not exceeded | controller |
| 2 | `READY` | real canary succeeded within the last 2 probe intervals, `/metrics` fresh, both ranks alive | controller |
| 3 | `BUSY` | READY and `num_requests_running > 0` | controller |
| 4 | `DEGRADED` | canary still succeeds but a non-critical signal failed (worker sentinel unreachable, metrics stale, canary TTFT > 10 s, router (classifier) unhealthy, GPU exporter unreachable) | controller |
| 5 | `WEDGED` | requests exist but neither token counter moves for ≥ 90 s, or the canary is outstanding ≥ 60 s while `/health` still answers 200; confirmed by two consecutive observations | controller |
| 6 | `RECOVERING` | a coordinated recovery holds the lock: from detection to canary success | controller |
| 7 | `QUEUEING` (v2; was `FALLBACK_ACTIVE`) | primary not READY/BUSY **and** the orchestrator holds ≥ 1 accepted generation durably queued for it | orchestrator (`llm_queued_generations > 0`); the top-level Grafana state is derived (§7.3) |
| 8 | `DOWN` | the primary is not serving and the restart budget is exhausted or the last recovery failed; accepted requests stay queued (never lost, never answered by another model) until an operator acts | controller |

Rules that must hold everywhere:
- A process being alive proves nothing. `/v1/models` 200 proves nothing.
  GPU utilisation proves nothing. READY requires the **readiness sequence of
  §5 v2**: a non-streaming completion, a streaming completion, token-counter
  progress, both GPUs observed working, both ranks alive.
- `MONITORING_UNKNOWN` is a distinct value, never a synonym for DOWN, never
  produced by `or vector(0)` on a series whose absence is not zero.
- The controller alone performs destructive recovery. Docker healthchecks
  remain as the last resort (they act later than the controller by design).

## 3. Signals (the twenty, and the source of each)

| # | signal | source | metric |
|---|---|---|---|
| 1 | head host reachable | Prometheus `up{job="node",node="spark-1"}` | existing |
| 2 | worker host reachable | Prometheus `up{job="node",node="spark-2"}` | existing |
| 3 | head container running | controller ← Docker API `/containers/{name}/json` | `techsara_vllm_head_container_running` |
| 4 | worker container running | controller ← sentinel `/state` | `techsara_vllm_worker_container_running` |
| 5 | head process alive | controller ← Docker API `/containers/{id}/top` (`vllm serve` and `VLLM::EngineCore`) | `techsara_vllm_head_engine_process_alive` |
| 6 | worker rank process alive | sentinel ← Docker API `/top` (`VLLM::Worker_TP1`) + log signatures | `techsara_vllm_worker_rank_alive` |
| 7 | API TCP listener | controller: TCP connect to head `127.0.0.1:8000` | `techsara_vllm_head_api_tcp_up` |
| 8 | `/health` ready | controller: GET `/health` | `techsara_vllm_head_health_ok` |
| 9 | `/metrics` scrape available | controller: GET `/metrics` | `techsara_vllm_head_metrics_ok` |
| 10 | metrics sample fresh | Prometheus: `time() - timestamp(vllm:generation_tokens_total{service="main"})` | recording rule `cluster:vllm_metrics_age_seconds` |
| 11 | synthetic completion | controller canary (§5) | `techsara_vllm_synthetic_success` and friends |
| 12 | token generation progressing | controller: `vllm:generation_tokens_total` + `vllm:prompt_tokens_total` deltas vs `num_requests_running` | `techsara_vllm_generation_frozen_seconds` |
| 13 | both ranks participating | a TP=2 completion cannot finish without rank 1 (synchronous all-reduce) **and** sentinel says rank alive; longer proof: `scripts/cluster-verify-engine.sh --probe` (both GPUs sampled) | `techsara_vllm_both_ranks_ok` |
| 14 | orchestrator can reach the model | orchestrator `/health` dependency probe | existing `health` + `llm_engine_state_code` |
| 15 | primary model active | orchestrator breaker CLOSED | `llm_breaker_state{engine="main"} == 0` |
| 16 | requests durably queued for the primary (v2) | orchestrator | `llm_queued_generations` |
| 17 | restart count | controller ← Docker `RestartCount` (head) and sentinel (worker) | `techsara_vllm_container_restart_count{rank}` |
| 18 | time since last successful real completion | controller | `techsara_vllm_last_success_timestamp_seconds` |
| 19 | current recovery state | controller | `techsara_vllm_recovery_in_progress`, `techsara_vllm_recovery_step` (one-hot) |
| 20 | last failure category | controller | `techsara_vllm_last_failure_category{category}` (one-hot, bounded set §4) |

## 4. Failure categories (bounded label set — nothing else may appear)

`none`, `worker_rank_dead`, `head_engine_dead`, `head_api_dead`,
`wedged_frozen_tokens`, `canary_timeout`, `canary_http_error`,
`connect_error`, `budget_exhausted`, `manual`, `cold_start_timeout`,
`head_restarted_externally` (v2: Docker or an operator restarted the head; the controller re-pairs the worker without a pair restart and without spending the budget).

Orchestrator error classes (breaker input, `llm_breaker_failures_total{reason}`):
`connection`, `readiness`, `request_timeout`, `queue_timeout`, `engine_dead`,
`worker_lost`, `capacity`, `malformed`, `cancelled`. Only `connection`,
`readiness`, `engine_dead`, `worker_lost`, `capacity` (429) open the breaker.
`malformed` (4xx) is never retried; `cancelled` is never counted;
`request_timeout` (a read timeout) is never retried and never opens the
breaker by itself.

## 5. The synthetic canary and the readiness sequence (controller)

**v2 readiness sequence (all must pass, in this order, before READY):**
1. non-streaming completion, 32 tokens, `temperature 0`, `seed 7`, thinking off;
2. streaming completion, 32 tokens, TTFT measured, terminal chunk seen;
3. token progress: `vllm:generation_tokens_total` on `/metrics` increased by ≥ the tokens received;
4. both GPUs participated: during a 256-token participation probe the controller samples the GPU exporters on both nodes (head `127.0.0.1:9835`, worker `<CLUSTER_WORKER_MGMT_IP>:9835`, every 250 ms) and requires **both** to exceed `PARTICIPATION_MIN_UTIL=30 %` at least once; on a TP=2 engine a finished completion already proves the all-reduce ran on both ranks, and the sample is the recorded evidence;
5. the sentinel reports the rank process alive (`VLLM::Worker*` present, `_TP1` suffix = joined).
The routine canary (below) is step 2 alone; steps 1–5 run after every start/recovery and whenever the controller has not proven the engine since the head's `started_at`.

- Request: `POST /v1/chat/completions`, `{"model": <first id of /v1/models>,
  "messages": [{"role":"user","content":"Reply with the single word: ok"}],
  "max_tokens": 4, "temperature": 0, "seed": 7,
  "chat_template_kwargs": {"enable_thinking": false}, "stream": true}`.
  Streaming so TTFT is measured; no tools, no web, no memory, no conversation
  row, no user, no private data. Nothing about the response body is stored
  except its length and timing.
- Interval: 30 s while READY/BUSY, 10 s while STARTING/RECOVERING/WEDGED.
  Measured cost of the current 2-token probe: 1,440/day, ~0.4 s each at
  c=1 — negligible; the interval is a config knob `CANARY_INTERVAL_S`.
- Timeout: 60 s (`CANARY_TIMEOUT_S`). Connect timeout 5 s.
- Records: success, HTTP status, connect latency, TTFT, total latency,
  terminal completion seen (`[DONE]` or `finish_reason`), tokens received,
  error category (bounded), timestamp.

## 6. The engine controller (replaces the shell watchdog) — **the single recovery authority (v2)**

Exactly one actor restarts the pair: the controller. The sentinel is a sensor
and an actuator that acts **only on the controller's `POST /restart`**
(`SENTINEL_AUTONOMOUS=0` is the default and the production setting); the
Docker healthchecks of the head and worker are **report-only** (they exit 1
so `docker ps` shows unhealthy) except one documented last-resort tier that
fires only after `VLLM_HEALTHCHECK_KILL_AFTER` consecutive misses (default 8 =
4 min, i.e. long after the controller would have acted) so it can never race
a live controller; the shell watchdog is gone. Manual recovery (`POST
/recover` from loopback) is subject to the same budget and cooldown as an
automatic one.

Runs on the head as compose service `engine-controller` (host network,
Docker socket RW — the same trust as today's watchdog, digest-pinned
`python:3.12-slim`). One process; one recovery at a time.

### 6.1 HTTP (bind `0.0.0.0:9838`; `GET` only except where stated)

- `GET /state` → the JSON document in §6.2. Read-only.
- `GET /metrics` → Prometheus text (§7.1). Read-only.
- `GET /healthz` → 200 `ok` (controller liveness).
- `POST /recover` → `{"reason": "manual", "category": "manual"}`; accepted
  **only from 127.0.0.1** (peer check) — the runbook's `scripts/cluster-recover.sh`
  uses it. Anything else → 403. No other mutation exists.

### 6.2 `/state` document

```json
{
  "schema": 1,
  "generated_at": 1757630000.0,
  "state": "READY", "state_code": 2,
  "reason": "canary ok in 0.41s",
  "since": 1757629000.0,
  "primary_ready": true,
  "router_available": true,
  "incident": null,
  "signals": {
    "head_container": {"running": true, "health": "healthy", "restart_count": 1, "started_at": 1757620000.0, "engine_process_alive": true, "observed_at": 1757630000.0},
    "worker": {"reachable": true, "container_running": true, "rank_process_alive": true, "restart_count": 2, "started_at": 1757620000.0, "last_fault": null, "observed_at": 1757630000.0},
    "api": {"tcp": true, "health": 200, "models": 200, "metrics": 200, "observed_at": 1757630000.0},
    "engine": {"requests_running": 3, "requests_waiting": 0, "generation_tokens_total": 123.0, "prompt_tokens_total": 456.0, "frozen_seconds": 0.0, "observed_at": 1757630000.0},
    "canary": {"ok": true, "http_status": 200, "connect_s": 0.01, "ttft_s": 0.2, "total_s": 0.4, "tokens": 2, "terminal": true, "error_category": "none", "at": 1757630000.0, "last_success_at": 1757630000.0, "consecutive_failures": 0},
    "router": {"configured": true, "health": 200, "observed_at": 1757630000.0},
    "gpus": {"head_util": 93.0, "worker_util": 93.0, "sampled_at": 1757630000.0}
  },
  "recovery": {"in_progress": false, "step": "idle", "attempts_in_window": 0, "budget": 3, "window_s": 3600, "cooldown_until": null, "last": null}
}
```
`incident`, when not null: `{"id": "20260911T221550Z", "started_at": ..., "category": "worker_rank_dead", "attempts": 1, "ended_at": null}`.
`worker.last_fault`, when not null: `{"at": ..., "signature": "misaligned address"}` —
signature is one of a bounded list (`misaligned address`, `illegal memory
access`, `AcceleratorError`, `WorkerProc hit an exception`, `died unexpectedly`,
`EngineDeadError`, `NCCL error`, `out of memory`).

### 6.3 Detection → recovery

Triggers (DETECT → CONFIRM), as implemented in v2:
1. `worker_rank_dead`: (a) the worker container's `started_at` is newer than
   the last **proven** completion of the current head (a worker that
   legitimately bounces after the head during a recovery and then serves is
   left alone); (b) `worker.last_fault.at` newer than both the head's and the
   worker's own `started_at` — one observation in normal operation, two looks
   ≥ 10 s apart during `wait_load` (a rank that joined the OLD head's store and
   died is re-created by Docker within seconds and pairs with the new head);
   (c) `rank_process_alive == false` on a container older than the grace and
   only after the rank was seen alive for this head incarnation (never inferred
   from a name that was never observed — single-node TP=1 has no `Worker_TP`).
   Rule 2′ (process table): the head's EngineCore/rank process seen alive then
   vanished, two ticks.
2. `/health` 5xx → `head_engine_dead`. One observation.
3. TCP refused / connect error after the engine was READY → `head_api_dead`.
   Two consecutive observations (≥ 10 s apart).
4. `engine.frozen_seconds ≥ 90` with `requests_running > 0` **and** the canary
   outstanding ≥ 60 s → `wedged_frozen_tokens`. Two observations.
5. Two consecutive canary timeouts on a **proven** engine (each counted only if
   `/health` answered 200 when that probe started) → `canary_timeout`,
   regardless of what `/health` answers now (a hung API is a wedge too) —
   **unless** a fresh `/metrics` sample shows the token counters moving with
   requests running (saturation, not a wedge; bounded by `CANARY_STARVATION_S`).
   All per-incarnation counters reset when the head's `started_at` changes, so a
   restart the controller did not perform never inherits stale timeouts.

Choreography (states published at every step; every step logged with the
incident id; diagnostics captured **before** anything is restarted):
```
DETECT → CONFIRM → (orchestrator sees RECOVERING and fails over)
→ CAPTURE_DIAGNOSTICS   head `docker logs --tail 400`, sentinel /diagnostics (worker tail), /state → .runtime/incidents/<id>/
→ STOP_STALE_PAIR       sentinel POST /restart (worker first: it must be waiting at the rendezvous), then Docker restart of the head (t=10)
→ WAIT_FOR_MODEL_LOAD   poll /health until 200, budget COLD_START_BUDGET_S=900 (measured: 5 m 20 s cold, 3 m 32 s warm)
→ RUN_REAL_CANARY       the §5 v2 readiness sequence (non-stream, stream, token progress, both GPUs, rank alive)
→ MARK_READY            state READY; the orchestrator's breaker closes on the controller's READY and its own half-open canary
→ RESUME_QUEUED         the orchestrator resumes every durably queued generation exactly once (§8.3 v2)
→ VERIFY_STABILITY      3 consecutive canary successes → incident.ended_at set
```
Locking: an `flock` on the host file `.runtime/locks/engine-recovery.lock`
(bind-mounted RW). `scripts/lib/engine-lock.sh` takes the same lock for
`cluster-up.sh`, `cluster-down.sh`, `cluster-recover.sh` and `deploy.sh`'s
model-restart path, so no two actors restart the pair at once.

Budget and backoff: at most `RECOVERY_BUDGET=3` recoveries per
`RECOVERY_WINDOW_S=3600` (manual ones included); when exhausted → state `DOWN`, category
`budget_exhausted`, **no further destructive action**, queued requests stay
queued and are never answered elsewhere, critical alert, evidence preserved; the controller keeps probing and returns to
READY by itself if the canary passes. Cooldown `RECOVERY_COOLDOWN_S=120` after
a recovery ends before another may start; jitter 0–5 s before acting.

### 6.4 The worker sentinel (compose service `vllm-worker-sentinel`, Node 2)

- Binds to `CLUSTER_WORKER_IP:9839` (the RoCE rail-A address — a
  point-to-point link only the head can reach; never the office LAN).
  Optional `CLUSTER_SENTINEL_TOKEN` (header `X-Sentinel-Token`) required on
  `POST` when set; when unset, `POST` is accepted only from `CLUSTER_HEAD_IP`.
- `GET /state` → `{"container": {"running": true, "health": "healthy",
  "restart_count": 2, "started_at": ...}, "rank_process_alive": true,
  "last_fault": null, "self_restarts_in_window": 0, "observed_at": ...}`.
- `GET /diagnostics` → the worker container's last 400 log lines (text).
- `POST /restart` → Docker `POST /containers/{id}/restart?t=5` on the worker
  container (SIGKILL after 5 s interrupts a core dump in progress). Returns
  `{"restarted_at": ...}`.
- No autonomy by default (v2): it reports the rank process gone or a fatal log
  signature (§6.2 list) within one poll (≤ 5 s) and restarts the worker only
  on the controller's `POST /restart`. `SENTINEL_AUTONOMOUS=1` (a bounded
  self-restart, 3 per hour) exists only for a deployment without a controller
  and is off in production. The rank process is matched as `VLLM::Worker`
  (its title while waiting at the rendezvous) and `VLLM::Worker_TP1` (joined);
  a waiting worker is never "absent".

### 6.5 Core dumps

`ulimits: core: {soft: 1, hard: 1}` on every vLLM container (head, worker,
router, OCR). The kernel aborts a **pipe** core dump when `RLIMIT_CORE == 1`
(`fs/coredump.c`: "RLIMIT_CORE is set to 1, aborting core") *before* apport
is spawned; `core: 0` does NOT work with a pipe pattern — apport 2.28.3
(`/usr/share/apport/apport:1054`) reads the whole core into the report
regardless of the limit. Measured on spark-476e, same image, same 1.5 GB
heap: `core=unlimited` 18.3 s held + 4.2 MB report; `core=1` 1.28 s, no
report. The 25 GiB rank process took 3 m 59 s (03:45:50 → 03:49:49 IST).

### 6.6 Compiled-kernel caches (v2)

Both ranks mount a persistent named volume at `/root/.cache/vllm` and
`/root/.cache/flashinfer` (`vllm-kernel-cache` on the head project,
`sf-local-ai-worker_kernel-cache` on the worker) so a recovery reuses the
validated torch.compile / FlashInfer JIT artefacts instead of recompiling
(cold `torch.compile` measured 50 s). The candidate's SM120 GDN prefill
kernels (CuTe-DSL) are compiled **in memory** on every rank start — measured on
the worker GB10: ≈ 5 s for the single-sequence variant during the profile
warm-up and ≈ 3 s for the multi-sequence variant at the first step that
batches ≥ 2 prefills — and are not covered by any on-disk cache; the
readiness sequence therefore includes a two-request participation probe so
both variants are compiled before READY. A stale or broken cache is detected by the controller as a `cold_start_timeout` or a
compile-error signature in the head log (`torch._dynamo`, `flashinfer.jit`,
`nvcc`, `cuda_nvrtc`); the runbook's `scripts/cluster-recover.sh
--clear-kernel-cache` empties both volumes under the lock and restarts the
pair; the controller never deletes a cache by itself. Warm-up of the GDN
kernels is done once per build by the first start under the lock (the two
ranks compile their own copies on their own nodes; the TP startup race is
assessed per pinned build in `docs/availability/CANDIDATE-B.md`).

### 6.7 Long-context admission (v2, orchestrator)

Two lanes in `orchestrator/app/admission.py`, applied to every orchestrator
request before it reaches vLLM (the second tenant's raw-port traffic is outside
it and is documented as such):
- **NORMAL** — prompt ≤ `ADMISSION_LONG_THRESHOLD_TOKENS=131072`: a semaphore of
  `ADMISSION_NORMAL_MAX=10` concurrent generations.
- **LONG** — above the threshold: one at a time (`ADMISSION_LONG_MAX=1`); before
  it starts, the orchestrator waits until the engine reports
  `requests_running ≤ ADMISSION_LONG_IDLE_MAX=0` (from `/metrics`, via the
  controller's engine sample) for `ADMISSION_LONG_WAIT_S=600` at most, and holds
  the NORMAL semaphore fully until its first token so no new prefill mixes with
  the large one. A lane wait is durable through the ordinary V29 `accepted`/`running` row (it happens while the engine is serving; only a RECOVERY wait parks a row as `queued`) and truthful:
  `Waiting for the model to finish current work before your large document (N ahead).`
Metrics: `llm_admission_lane_active{lane}`, `llm_admission_waiting{lane}`,
`llm_admission_wait_seconds{lane}` (histogram), `llm_admission_rejections_total{reason}`.
Engine knobs evaluated one at a time in `CANDIDATE-B.md` (verified against both
images: `--max-num-partial-prefills` / `--max-long-partial-prefills` do **not**
exist in either build; the scheduler's only long-prefill knob is
`--long-prefill-token-threshold`): `--long-prefill-token-threshold 131072`,
`--max-num-batched-tokens 4096` vs `8192`, `--max-num-seqs 10` vs default —
plus the orchestrator LONG lane above, which is what actually serialises very
large prefills. The advertised window stays 1,000,000 tokens;
configured / tested / production-safe are reported separately (MEMORY-BUDGET.md).

## 7. Metrics and rules

### 7.1 Controller (`techsara_vllm_*`, scraped as job `engine-controller`)

Gauges unless stated. Labels are bounded sets only — never prompt text, ids,
filenames, hostnames of users, or free-form errors.

```
techsara_engine_controller_up                      1
techsara_vllm_state_code                           0..8 (§2)
techsara_vllm_state{state="..."}                   one-hot over the nine names
techsara_vllm_state_since_timestamp_seconds
techsara_vllm_primary_ready                        0/1
techsara_vllm_router_available                     0/1 (internal classifier only — never a serving path)
techsara_vllm_participation_ok                     0/1 (both GPUs seen working during the last readiness sequence)
techsara_vllm_generated_at_seconds                 the snapshot's own timestamp (freshness)
techsara_vllm_synthetic_success                    0/1 (last canary)
techsara_vllm_synthetic_connect_seconds
techsara_vllm_synthetic_ttft_seconds
techsara_vllm_synthetic_duration_seconds
techsara_vllm_synthetic_tokens
techsara_vllm_synthetic_probes_total{outcome="ok|timeout|http_error|connect_error"}   counter
techsara_vllm_last_success_timestamp_seconds
techsara_vllm_consecutive_probe_failures
techsara_vllm_generation_frozen_seconds
techsara_vllm_head_container_running               0/1
techsara_vllm_head_engine_process_alive            0/1
techsara_vllm_head_api_tcp_up                      0/1
techsara_vllm_head_health_ok                       0/1
techsara_vllm_head_metrics_ok                      0/1
techsara_vllm_worker_reachable                     0/1 (sentinel answered)
techsara_vllm_worker_container_running             0/1
techsara_vllm_worker_rank_alive                    0/1
techsara_vllm_both_ranks_ok                        0/1
techsara_vllm_container_restart_count{rank="0|1"}
techsara_vllm_recovery_in_progress                 0/1
techsara_vllm_recovery_step{step="idle|detect|confirm|capture|stop_pair|wait_load|canary|mark_ready|verify"}  one-hot
techsara_vllm_recovery_attempts_total{outcome="started|succeeded|failed|budget_exhausted|repaired_worker"}   counter
techsara_vllm_restart_budget_remaining
techsara_vllm_incident_start_timestamp_seconds     0 when none
techsara_vllm_last_failure_category{category="..."} one-hot over §4
techsara_vllm_cold_start_seconds                   last measured head start → READY
techsara_vllm_recovery_duration_seconds            last measured detect → READY
```

### 7.2 Orchestrator (`llm_*`, existing job `orchestrator`)

```
llm_engine_state_code                 what the orchestrator last read from the controller; -1 when unreachable/stale
llm_breaker_state{engine="main"}      0 CLOSED, 1 OPEN, 2 HALF_OPEN
llm_breaker_transitions_total{engine,to}
llm_breaker_failures_total{engine,reason}   reasons from §4
llm_queued_generations                accepted generations waiting for the primary (gauge)
llm_queue_wait_seconds                 histogram, seconds a generation waited for READY
llm_resumed_generations_total{outcome="resumed|expired|duplicate_suppressed"}
llm_admission_lane_active{lane="normal|long"}
llm_admission_waiting{lane="normal|long"}
llm_admission_wait_seconds{lane="normal|long"}
llm_admission_rejections_total{reason="capacity|timeout"}
```
(`llm_retry_total`, `llm_engine_wait_seconds`, `llm_engine_unavailable_total`
already exist and stay.)

### 7.3 Recording rules (Prometheus)

```
cluster:vllm_metrics_age_seconds         time() - max(timestamp(vllm:generation_tokens_total{service="main"}))
cluster:vllm_service_state:code          the derived top-level state (v2):
    7 (QUEUEING) when techsara_vllm_state_code not in {2,3,8} and llm_queued_generations > 0
    otherwise techsara_vllm_state_code
    plus freshness: when time() - techsara_vllm_generated_at_seconds > 30 the controller's verdict is stale → no sample (MONITORING_UNKNOWN)
    (no `or vector(0)`: absence stays absent; Grafana noValue = MONITORING_UNKNOWN)
cluster:vllm_last_success_age_seconds    time() - techsara_vllm_last_success_timestamp_seconds
cluster:vllm_incident_duration_seconds   time() - techsara_vllm_incident_start_timestamp_seconds  (only when > 0)
```

### 7.4 Alerts (names are fixed; each carries service, state, since, reason, runbook, safe command; no request content)

Critical: `VllmEngineWedged` (state 5 for 30 s), `VllmPrimaryDown` (state 8 for 1 m —
nothing answers users; queued requests are held), `VllmReadyUnproven`
(READY/BUSY claimed but `cluster:vllm_last_success_age_seconds > 120`), `VllmRecoveryBudgetExhausted`
(category budget_exhausted), `VllmWorkerRankAbsent` (`worker_rank_alive == 0`
for 1 m while state ∉ {RECOVERING, STARTING}), `VllmGenerationFrozen`
(`generation_frozen_seconds > 90` and running > 0), `VllmRepeatedRecoveryFailure`
(`increase(techsara_vllm_recovery_attempts_total{outcome="failed"}[1h]) >= 2`).

Warning: `VllmMonitoringUnknown` (`absent_over_time(techsara_vllm_state_code[2m])`
or `absent_over_time(up{job="vllm-main"}[2m])` — worded as *monitoring*, not as
inference down), `VllmMetricsStale` (`cluster:vllm_metrics_age_seconds > 60`
while `techsara_vllm_synthetic_success == 1`), `VllmRequestsQueued` (`llm_queued_generations > 0` for 2 m),
`VllmQueueBacklog` (exists), `VllmCanaryTtftHigh` (> 10 s for 5 m),
`HeadSwapActivity` (`rate(node_vmstat_pswpout{node="spark-1"}[5m]) > 500`),
`NcclTcpFallback` (from `scripts/cluster-status.sh`-style evidence — if not
observable in Prometheus, document as not observable), `RoceRailMissing`
(one of the two rails 0 B/s during a canary — best effort), `VllmRecoveredRestart`
(info-level: recovery succeeded), `VllmConfigDrift` (only if a drift signal
exists — else document as not observable).

Info: `VllmRecoveryStarted`, `VllmPrimaryRestored`, `VllmQueueDrained`.

The existing `VllmDown` (`up{job="vllm-main"} == 0`) is **renamed**
`VllmScrapeTargetDown` and demoted to warning: it means the scrape target
disappeared, which during a reload is expected and is already covered.

## 8. Orchestrator contract

### 8.1 Engine-state client (`app/engine_state.py`)
Polls `ENGINE_CONTROLLER_URL` (`http://vllm:9838/state`) every
`ENGINE_STATE_POLL_S=5`; a snapshot older than 15 s or unreachable →
`unknown`. **Unknown never opens the breaker** — only observed failures do.

### 8.2 Circuit breaker (`app/breaker.py`)
Per engine (`main` only in v2). CLOSED → OPEN after
`LLM_BREAKER_FAILURES=3` counted failures within `LLM_BREAKER_WINDOW_S=30`,
**or** immediately when the controller reports WEDGED/RECOVERING/DOWN/STARTING
(external open). OPEN → HALF_OPEN after `LLM_BREAKER_COOLDOWN_S=10`;
HALF_OPEN admits exactly one canary call, held for at most
`LLM_BREAKER_CANARY_HOLD_S=60`; success (a streaming canary settles on its
first body chunk, not on headers) → CLOSED; a failure with an opening reason →
OPEN; a failure with a non-opening reason (4xx, read timeout, cancellation)
proves nothing and leaves HALF_OPEN for the next caller.
While OPEN, no caller polls the dead port: `resilient()` consults the breaker
before the first attempt, and only the breaker's single half-open canary
touches the engine. Every transition is a metric and one log line
(`llm.breaker engine=main from=… to=… reason=…`).

### 8.3 Continuity in one-model mode (`app/continuity.py`, replaces v1 fallback routing)

When the breaker is OPEN, or the controller reports STARTING / WEDGED /
RECOVERING / DOWN, a user request is **never** answered by another model and
never fails with a generic error. The chat worker:
1. persists the request first (the V29 `chat_requests` row, status `queued`,
   the logical `generation_id` and `intent_id` fixed for its whole life);
2. emits exactly one status line — `Main model is recovering—your request is
   safely queued.` — and keeps the SSE stream alive with heartbeats (no
   endless spinner: the client renders the queued state);
3. waits for READY on the engine-state client's event (never polling the dead
   port) up to `LLM_QUEUE_MAX_WAIT_S=900` (cold start + margin);
4. resumes the **same** generation (same `generation_id`, `attempt` + 1,
   `retry_reason=recovery`) exactly once — the V29 guards make a second
   answer impossible;
5. if the wait expires, the row stays `queued` with the truthful line
   `The main model is still recovering. Your request is kept and will resume
   automatically.` — the stream ends with an SSE `error` frame
   `{code: "MODEL_RECOVERING", resumable: true}` (never a 500, never a
   smaller-model answer); the client renders a queued state, not a failure,
   keeps polling `/chat/requests`, and attaches when the resume sweep (§8.4)
   runs the row on READY.
Admission sentences (§6.7): `Waiting for the model to finish current work
before your large document (N ahead).` and, for the NORMAL lane when it is
full, `Waiting for a free slot on the main model (N ahead).`
`chat_with_tools`, vision, Deep Research, video and artifact stages queue the
same way (their existing recovery windows already wait; the engine-state
event replaces polling).
Config: `LLM_QUEUE_MAX_WAIT_S`, `ENGINE_CONTROLLER_URL`, `ENGINE_STATE_POLL_S`,
`LLM_BREAKER_*` as before. `FALLBACK_*` keys are removed.

### 8.4 Durability (unchanged invariants, now tested)
A generation is identified by its logical id for its whole life. An automatic
retry is allowed only **before the first token** and only when no completed
assistant message exists for that generation; after the first token the
attempt is marked `interrupted`, the partial text is kept as partial, and no
second answer is appended. A repeated client `POST /chat` with the same
`intent_id` returns the existing generation, never a second row. Every attempt
records `attempt`, `engine` (always `primary` in v2), `retry_reason`,
`terminal_state`. **Resume sweep (v2):** when the controller reports READY
(and at orchestrator start-up), rows in `queued`/`interrupted` with a resumable
snapshot are resumed exactly once under a lease; `llm_resumed_generations_total`
counts `resumed|expired|duplicate_suppressed`.

## 9. File ownership (no file has two owners)

| workstream | owns |
|---|---|
| controller | `monitoring/engine-controller/{controller.py,sentinel.py,common.py,README.md}`, `monitoring/engine-controller/tests/**` |
| orchestrator | `orchestrator/app/{engine_state.py,breaker.py,continuity.py,admission.py}` (v2: `fallback.py` deleted), edits in `orchestrator/app/{llm.py,config.py,resilience.py,health.py,metrics.py}` and the chat worker's status line in `orchestrator/app/main.py` (minimal), tests `orchestrator/tests/test_{breaker,continuity,admission,engine_state,llm_continuity,generation_durability}.py` |
| durability (phase 2) | `orchestrator/app/` request/generation store modules only + `orchestrator/tests/test_generation_durability.py` |
| monitoring | `monitoring/prometheus/prometheus.yml`, `monitoring/prometheus/rules/{alerts,recording}.yml`, `monitoring/prometheus/tests/**` (promtool unit tests), `monitoring/grafana/dashboards/dgx-cluster-overview.json`, `monitoring/grafana/dashboards/dgx-vllm-performance.json`, `docs/MONITORING.md` |
| sre | `compose/compose.dgx-spark.yaml`, `compose/compose.cluster-dgx-spark.yaml`, `compose/compose.cluster-worker.yaml`, `compose/compose.ocr.yaml`, `compose.yaml` (only if required), `scripts/cluster-sync.sh`, `scripts/cluster-recover.sh` (new), `scripts/lib/engine-lock.sh` (new), `scripts/cluster-up.sh`, `scripts/cluster-down.sh`, `scripts/cluster-status.sh`, `scripts/monitoring.sh`, `scripts/deploy*.sh` + `scripts/lib/deploy-common.sh` (lock only), `scripts/recovery-tests/engine_failure_drills.sh` (new), `launcher/techsara_cli/**` + `launcher/tests/**` (only what the compose changes require), `.env.example` |
| candidate | `docs/availability/CANDIDATE-B.md`, `scripts/cluster-ab.py` (the A/B harness), `scripts/cluster-cpu.sh` (CPU topology and per-process measurement) |
| docs (phase 2) | `docs/availability/{INCIDENT-2026-09-11-vllm.md,ARCHITECTURE.md,RUNBOOK.md,ADR-0002-high-availability.md,REVIEW-MANIFEST.md,SLO.md}`, `CHANGELOG.md`, `README.md` (index lines only), `docs/CLUSTER.md` (pointer lines only) |

Ports: controller `9838` (head, all interfaces, read-only + loopback POST),
sentinel `9839` (worker, RoCE address only). Both are documented in
`docs/MONITORING.md`'s port table by the monitoring workstream.

## 10. Testing rules

- Every new module has unit tests that run without Docker or a GPU (fake
  Docker API, fake HTTP engines).
- Prometheus rules are validated with `promtool check rules` and unit-tested
  with `promtool test rules` (absent-series and stale-series cases included).
- Dashboards must parse as JSON and every `expr` must reference metrics
  named in this file or already scraped.
- Compose files must pass `docker compose config` with the launcher's real
  file chain (`scripts/lib/deploy-common.sh: dr_compose_prefix`).
- Shell: `shellcheck` clean.
- Orchestrator tests: `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/test_ha_<workstream>` — one database per workstream, never the shared one.
- Python for tests: `/home/techsphere/Documents/project/personal-LLM-Chabot/orchestrator/.venv/bin/python -m pytest` run from the worktree.
