# Architecture — how the main model's outages are seen, recovered, and waited out safely

The design behind the 2026-09-12 availability programme, **v2 (strict one-model mode)**. The
binding names (states, signals, metrics, ports, file ownership) are in
[`CONTRACT.md`](CONTRACT.md); the decision and the options it rejected are
[`ADR-0002-high-availability.md`](ADR-0002-high-availability.md); what to do
when it fires is [`RUNBOOK.md`](RUNBOOK.md); the numbers it must meet are
[`SLO.md`](SLO.md); the incident it answers is
[`INCIDENT-2026-09-11-vllm.md`](INCIDENT-2026-09-11-vllm.md). Every file cited
below is cited by its path in this repository.

One sentence first, because every section below follows from it: **only
`nvidia/Qwen3.6-35B-A3B-NVFP4` answers a person, and while its single TP=2
instance reloads nobody is answered** — requests are accepted, durably
queued, and resumed on the same generation when the instance is proven READY.
There is no stand-in model. The router engine classifies; it never answers.

## 1. The primary: one engine on two machines

`nvidia/Qwen3.6-35B-A3B-NVFP4` with a 1,000,000-token window runs as **one**
vLLM engine, tensor-parallel across the two DGX Sparks (`--tensor-parallel-size
2 --nnodes 2 --distributed-executor-backend mp`, the `mp` executor over NCCL on
the two RoCE rails). The head container `sf-local-ai-vllm-1` on Spark 1 is node
rank 0 and the only HTTP endpoint (host network, `0.0.0.0:8000`); the worker
container `sf-local-ai-worker-vllm-worker-1` on Spark 2 is node rank 1,
`--headless`, its own Compose project. The compose definitions are
`compose/compose.dgx-spark.yaml` + `compose/compose.cluster-dgx-spark.yaml`
(head) and `compose/compose.cluster-worker.yaml` (worker, shipped to
`~/.techsara-cluster/` by `scripts/cluster-sync.sh`); why TP=2 and not
pipeline or two single-node replicas is measured in
[`../CLUSTER.md`](../CLUSTER.md).

Two facts of that topology decide everything below:

- **The two ranks are one `torch.distributed` process group.** A restarted head
  is a new group the old worker can never rejoin; a dead worker rank blocks
  the head's next collective until vLLM's `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS`
  (300 s) fires. vLLM has no cross-node death signal in any build
  ([`VLLM-UPGRADE-RESEARCH.md`](VLLM-UPGRADE-RESEARCH.md) §5): the head's
  `MultiprocWorkerMonitor` watches local children only.
- **It is a single replica.** Every fault that needs a reload — and the GDN
  prefill fault class of this build is unclosed upstream and fires roughly
  daily under the second tenant's concurrency-10 mixed load — costs 3 m 32 s
  (warm) to 5 m 20 s (cold) of the primary answering nothing. No detection
  speed changes that; only Option C of the ADR (two more Sparks, two TP=2
  replicas of the same model) would. What this programme changes is how long
  a sub-second fault stays *undetected*, how the pair is brought back, what
  happens to the requests that arrive meanwhile, and how load is admitted so
  the fault's trigger shape is reached less often.

## 2. The process model, and what each endpoint proves

Inside the head container there are three processes that matter, and they die
independently:

```
sf-local-ai-vllm-1 (Spark 1, rank 0)          sf-local-ai-worker-vllm-worker-1 (Spark 2, rank 1)
├─ vllm serve          the API process:         ├─ vllm serve --headless   the executor shell
│                      /health /v1/models       │
│                      /metrics /v1/chat/…       └─ VLLM::Worker_TP1        the rank-1 GPU worker
├─ VLLM::EngineCore    the scheduler                (VLLM::Worker while waiting at the rendezvous)
└─ VLLM::Worker_TP0    the rank-0 GPU worker  ←── NCCL over RoCE rails A/B ──→
```

| what answers | who answers it | what a 200 proves |
|---|---|---|
| `GET /health` | the API process | the API process is alive. On 2026-09-11 it answered 200 for 5 min 03 s after rank 1 died |
| `GET /v1/models` | the API process | the model *name* is registered — vLLM answers this minutes before it can generate, and for as long as the process lives |
| `GET /metrics` | the API process | the counters can be read. Their *movement* is the evidence (`vllm:generation_tokens_total` sat frozen at 2,138,504 with 9 requests "running") |
| Prometheus `up{job="vllm-main"}` | the scrape of `/metrics` | the listener is open |
| `docker inspect … .State.Health` | the container healthcheck | the same `/health`, or a process-table check, every 30 s |
| **the readiness sequence** (CONTRACT §5 v2): a non-streaming completion, a streamed one that reaches a terminal chunk, `generation_tokens_total` moved by ≥ the tokens received, **both GPU exporters > 30 % during a 256-token participation probe**, the sentinel's rank process alive | **both ranks, and the counters** | **the engine completes work on both machines.** A TP=2 step cannot finish without rank 1's half of every all-reduce; the GPU samples are the recorded evidence |

That last row is the only proof, and it is the contract's rule (CONTRACT §2):
*a process being alive proves nothing; READY requires the readiness sequence.*
The five steps run after every start and every recovery and whenever the
controller has not proven the engine since the head's `started_at`; the
routine canary between them is step 2 alone (a streamed 4-token completion
every 30 s, 10 s while not READY). The Docker healthchecks in
`compose/compose.cluster-dgx-spark.yaml` and `compose/compose.cluster-worker.yaml`
are **report-only** (they exit 1 so `docker ps` shows unhealthy) except one
documented last-resort tier that fires only after `VLLM_HEALTHCHECK_KILL_AFTER`
consecutive misses (default 8 = 4 min, long after the controller would have
acted), so a healthcheck can never race a live controller. On 2026-09-11 the
v0 healthcheck's 5xx branch and the controller's one-observation trigger would
have restarted the head seconds apart; that is the race the tier removes.

## 3. The engine controller and the worker sentinel — the single recovery authority

Two stdlib-only Python programs in `monitoring/engine-controller/`
(`controller.py`, `sentinel.py`, `common.py`; design notes and every
environment knob in `monitoring/engine-controller/README.md`; tests in
`monitoring/engine-controller/tests/`). They replace the shell `vllm-watchdog`
that `compose/compose.dgx-spark.yaml` carried until 2026-09-12, whose one
signature (two 120 s probe timeouts, 60 s apart) always lost the race to vLLM's
own 300 s timeout.

```
                       SIGNALS IN (CONTRACT §3)                                   STATES OUT (CONTRACT §2)
                       ─────────────────────────                                  ────────────────────────
 Docker API  ──── head container running / RestartCount / started_at ───┐         0 MONITORING_UNKNOWN
 (head sock)  ─── head process table: vllm serve, EngineCore, Worker_TP0 ┤         1 STARTING
 head API   ───── TCP :8000, /health, /v1/models, /metrics ─────────────┤         2 READY
 (ENGINE_HEAD_API_URL, launcher-generated from the real bind address)    │         3 BUSY
 /metrics    ──── generation_tokens_total, prompt_tokens_total,         │         4 DEGRADED
                  num_requests_running → frozen_seconds ────────────────┤ ─────►  5 WEDGED
 canary      ──── streamed 4-token chat completion every 30 s (10 s     │         6 RECOVERING
                  while not READY): ok, TTFT, total, terminal chunk ────┤        (7 QUEUEING is derived in
 readiness   ──── the five-step sequence after every start/recovery;    │         Prometheus from the orchestrator's
 sequence         both GPU exporters sampled at 4 Hz (participation) ───┤         llm_queued_generations, §6 below)
 sentinel    ──── GET /state on Spark 2: container running, started_at, │         8 DOWN
 (:9839,RoCE)     rank_process_alive (VLLM::Worker*), last_fault ───────┤
 router      ──── GET /health of the classifier (a DEGRADED input only) ┘
                                                                                       │
                          published on :9838 as GET /state (JSON, CONTRACT §6.2)  ◄────┘
                          and GET /metrics (techsara_vllm_*, CONTRACT §7.1)
```

The head API is probed where the head actually listens: the launcher generates
`ENGINE_HEAD_API_URL` from the rendered bind address (`0.0.0.0` → loopback,
`CLUSTER_API_BIND_ADDRESS` otherwise), and an empty URL makes the controller
report the head as **not observable** (`MONITORING_UNKNOWN`), never STARTING or
DOWN — a false DOWN is an outage of its own once the orchestrator acts on it
(round-1 blocker, `REVIEW-FINDINGS-round1.md`, sre). In single-node (TP=1) mode
there is no `VLLM::Worker_TP*` process and no sentinel; the controller must not
infer death from a process name it never saw.

**Detection → confirmation** (`controller.py`, CONTRACT §6.3): the five
triggers are `worker_rank_dead` (the sentinel saw the rank gone, a fatal log
signature newer than the head's `started_at`, or a worker start newer than the
head's — one observation, **also during the recovery's own `wait_load`**, so a
rank that dies on load fails the attempt with its real category instead of
burning the 900 s budget), `head_engine_dead` (`/health` 5xx — one
observation), `head_api_dead` (TCP refused after READY, two observations
≥ 10 s apart), `wedged_frozen_tokens` (counters frozen ≥ 90 s with requests
running **and** the canary outstanding ≥ 60 s while `/health` answers 200 — two
observations) and `canary_timeout` (two consecutive timeouts on a proven
engine, regardless of whether `/health` answers — a hung API process is exactly
the case; exempt only while a **fresh** `/metrics` sample shows the counters
moving: a saturated scheduler starving a 4-token probe is not a wedge). The
canary counters reset whenever the head's `started_at` changes, so timeouts
accumulated during a load never confirm against the head that just finished
loading. The confirmed category is published as its state (WEDGED / DOWN)
before RECOVERING, so the log always reads DETECT → CONFIRM → RECOVERING.

**The choreography**, every step written to `/state` (`.recovery.step`) and
logged with the incident id:

```
confirm        take the flock; open the incident (a fresh id if the last one ended, or the engine
               was proven READY since it started); attempts+1; jitter 0–5 s
capture        .runtime/incidents/<id>/head-logs-<n>.txt (docker logs --tail 400)
                                        worker-diagnostics-<n>.txt (sentinel /diagnostics)
                                        state-<n>.json  — BEFORE anything is restarted
stop_pair      sentinel POST /restart  (the WORKER first: it must be waiting at the rendezvous;
               a refused/unreachable sentinel is recorded, not swallowed)
               then docker restart -t 10 of the head; restart_at taken AFTER this returns
wait_load      /health 200 from the NEW container start; budget COLD_START_BUDGET_S=900
canary         the §5 v2 readiness sequence on the new head (a probe STARTED after started_at)
mark_ready     lock released; cooldown 120 s armed; READY; recovery_duration measured
resume_queued  the orchestrator resumes every durably queued generation exactly once (§4, §5)
verify         three consecutive canary successes → incident.ended_at — whenever an incident is
               open, not only after a controller-driven success (self-heal, operator restart)
```

Worker first because of §1: a worker restarted first is already waiting at the
rendezvous when the new head arrives; on 2026-09-11 the worker instead joined
the *old* head's TCP store and was reset, twice.

**The lock.** One `flock` on the host file `.runtime/locks/engine-recovery.lock`
(bind-mounted into the controller at `/run/techsara/locks`; created as the
operator by `launcher/techsara_cli/cli.py: _prepare_engine_controller_paths`,
and when the controller creates it first it hands it to the mount's owner, so
the shell side can always open it). `scripts/lib/engine-lock.sh` takes the same
inode for `scripts/cluster-up.sh`, `scripts/cluster-down.sh`,
`scripts/cluster-recover.sh` and `scripts/deploy.sh`'s `techsara down/up`
window, waiting (default 20 min) rather than failing, so a recovery forty
seconds from READY is waited out and never turned into a second restart. The
launcher's own `./techsara up` takes it too before it touches the pair (round-1
major). Whoever holds it owns the pair. The lock directory must be a real bind
mount: the controller refuses to create it inside its own overlay, where nobody
else could contend for it.

**The budget.** At most `RECOVERY_BUDGET=3` recoveries per
`RECOVERY_WINDOW_S=3600`, **manual ones included**, a `RECOVERY_COOLDOWN_S=120`
cooldown after each. When the budget is spent the controller goes `DOWN` with
category `budget_exhausted`, takes **no further destructive action**, keeps
probing, preserves the evidence, holds the accepted requests queued (never
answered elsewhere), and returns to READY by itself if the readiness sequence
passes — a passing canary is the tie-breaker even against a sentinel document
that says otherwise. `POST /recover` past the budget is refused synchronously;
the escalation past the budget is a human with `scripts/cluster-recover.sh
--force` under the same lock, not the controller.

**The sentinel** (`sentinel.py`, compose service `vllm-worker-sentinel` in
`compose/compose.cluster-worker.yaml`) is what the head cannot be: a process on
Spark 2 with that node's Docker socket. Every 5 s it reads the worker
container's process table and its log stream for the bounded fault signatures
of CONTRACT §6.2 (`misaligned address`, `illegal memory access`, …). It answers
`GET /state` and `GET /diagnostics` to the controller and restarts the worker
**only on the controller's `POST /restart`** (`docker restart -t 5` — SIGKILL
after 5 s also interrupts a core dump). It has no autonomy in production
(`SENTINEL_AUTONOMOUS=0`; the bounded self-restart exists only for a
deployment without a controller). It binds to the RoCE rail-A address
(`CLUSTER_WORKER_IP:9839`); note that this is a local address on the worker, so
every process on that host (the second tenant's pipeline included) can reach
it, and that bridge containers on the head are MASQUERADEd to `CLUSTER_HEAD_IP`
on that link — the `X-Sentinel-Token` that `scripts/cluster-sync.sh` mints is
therefore the real gate, required on every endpoint when set, with one source
of truth for its value on both nodes. Prometheus does not scrape it; the
controller folds what it reports into `techsara_vllm_worker_*`.

**Core dumps.** `ulimits: core: {soft: 1, hard: 1}` on every vLLM container
(head, worker, router, OCR in `compose/*.yaml`). The host's `core_pattern` is
a pipe to apport, which reads the whole core into its report regardless of
`core: 0`; `RLIMIT_CORE == 1` makes the kernel abort a pipe dump before apport
is spawned. Measured on the same image: 18.3 s held → 1.28 s. On 2026-09-11 the
25 GiB rank was held 4 min 51 s — the largest single delay of the outage.

**Compiled-kernel caches** (CONTRACT §6.6). Both ranks mount a persistent named
volume at `/root/.cache/vllm` and `/root/.cache/flashinfer` (`vllm-kernel-cache`
on the head project, `sf-local-ai-worker_kernel-cache` on the worker), so a
recovery reuses the validated torch.compile / FlashInfer JIT artefacts instead
of recompiling them (cold compile measured 50 s; a FlashInfer GDN JIT is
longer — it is part of every reload's cost otherwise). A stale or broken cache
shows as `cold_start_timeout` or a compile-error signature in the head log
(`torch._dynamo`, `flashinfer.jit`, `nvcc`, `cuda_nvrtc`); the controller
never deletes a cache — `scripts/cluster-recover.sh --clear-kernel-cache`
empties both volumes under the lock and restarts the pair. The two ranks
compile their own copies on their own nodes; the TP start-up race between them
is assessed per pinned build in [`CANDIDATE-B.md`](CANDIDATE-B.md).

## 4. The orchestrator: breaker, controller verdict, continuity, admission

Four modules in `orchestrator/app/`, wired into `llm.py` and `resilience.py`
(the existing wait-and-retry layer, unchanged in what it retries):

**`engine_state.py`** polls `ENGINE_CONTROLLER_URL` (`http://vllm:9838/state`,
`vllm` → `host-gateway` in dual mode; the launcher generates a reachable URL
per mode) every `ENGINE_STATE_POLL_S=5` and keeps the last document in memory;
`llm_engine_state_code` shows what it believes and `/health` shows how old
that belief is. A snapshot older than the staleness bound (≥ 3 poll
intervals), an unreachable controller or a malformed document is **unknown**,
and unknown never opens the breaker: the controller is a monitoring component,
and a chat must not be queued because a sidecar restarted. The client also
exposes the READY transition as an **event** that continuity waits on.

**`breaker.py`** — one, for the `main` engine (v2 has no other):

```
            3 counted failures in 30 s (LLM_BREAKER_FAILURES / _WINDOW_S)
  CLOSED ──────────────────────────────────────────────────────────────► OPEN
    ▲     or the controller says STARTING / WEDGED / RECOVERING / DOWN      │
    │        (the external open — HELD open until the controller stops)     │ LLM_BREAKER_COOLDOWN_S = 10
    │                                                                       ▼
    └────────────── the ONE admitted canary call succeeds ◄──────────── HALF_OPEN
                    (an opening failure → OPEN again)
```

Only `connection`, `readiness`, `engine_dead`, `worker_lost` and `capacity`
(429) count towards opening (CONTRACT §4); a read timeout is a generation that
ran its whole wall clock and never opens anything by itself; a 4xx is the
caller's fault; a cancellation is not counted. A streamed call settles its
permit on the **first token** (or the terminal chunk), not when the response
headers arrive — vLLM sends headers before it schedules anything, and the
2026-09-11 shape for the orchestrator's own traffic was "200, hang, an
`EngineDeadError` chunk", which must count as `engine_dead` and must not close
a HALF_OPEN breaker. The HALF_OPEN canary is cheap and bounded (a 4-token probe
with a deadline), so a long non-streaming generation never holds every other
caller off a healthy primary. While OPEN, `resilience.resilient()` consults the
breaker **before** the first attempt and nobody polls the dead port — not even
`/health`, which is what the 2026-09-11 outage made worthless. Every transition
is one metric (`llm_breaker_transitions_total{engine,to}`) and one log line
(`llm.breaker engine=main from=… to=… reason=…`). A controller that reports
STARTING for one tick after *its own* restart against an old, serving head is
not an external open (round-1 major).

**`continuity.py`** (CONTRACT §8.3 v2; replaces the v1 `fallback.py`, which is
deleted) decides what happens to a request the breaker will not admit. Nothing
is diverted anywhere; the request is **kept**:

1. persist first — the V29 `chat_requests` row, status `queued`, the logical
   `generation_id` and `intent_id` fixed for its whole life;
2. one status line, exact text — `Main model is recovering—your request is
   safely queued.` — and the SSE stream kept alive with heartbeats so the
   client renders a queued state, not a spinner;
3. wait for READY on the engine-state client's event (never polling the dead
   port), up to `LLM_QUEUE_MAX_WAIT_S=900` (cold start plus margin);
4. resume the **same** generation exactly once (`attempt` + 1,
   `retry_reason=recovery`) — the V29 guards make a second answer impossible;
5. if the wait expires, the row stays `queued` with the second exact line —
   `The main model is still recovering. Your request is kept and will resume
   automatically.` — never a 500, never a smaller model; the resume sweep (§5)
   takes it when READY arrives and the browser's known-intent path attaches to
   it.

`chat_with_tools`, vision, Deep Research, video and the artifact stages queue
the same way: their existing recovery windows already wait, and the engine-state
event replaces their polling. There is no "eligible" class and no second
breaker. The assistant message's `meta` never carries an engine other than the
primary; the per-attempt usage row records `engine` (`primary`),
`retry_reason`, `terminal_state` (`main.py: _attempt_record`). Exposed as
`llm_queued_generations` (gauge), `llm_queue_wait_seconds` (histogram),
`llm_resumed_generations_total{outcome="resumed|expired|duplicate_suppressed"}`,
`llm_breaker_state{engine}`, `llm_breaker_failures_total{engine,reason}`
(`metrics.py` bounds every label). `health.py: engine_availability` puts the
controller snapshot, the breaker and the queue depth inside the main model's
`/health` check entry without moving its `status` — an open breaker means the
orchestrator is protecting itself, not that restarting it would help.

**`admission.py`** (CONTRACT §6.7) sits in front of every orchestrator request
to vLLM (the second tenant's raw-port traffic is outside it, by construction):

| lane | when | limit | before it starts |
|---|---|---|---|
| NORMAL | prompt ≤ `ADMISSION_LONG_THRESHOLD_TOKENS=131072` | `ADMISSION_NORMAL_MAX=10` concurrent generations | — |
| LONG | above the threshold | `ADMISSION_LONG_MAX=1` | the engine reports `requests_running ≤ ADMISSION_LONG_IDLE_MAX=0` (from the controller's engine sample), waited up to `ADMISSION_LONG_WAIT_S=600`; the NORMAL lane is held fully until the long request's first token, so no new prefill mixes with the large one |

Every wait is durable (row status `queued`) and truthful (`Waiting for the
model to finish current work before your large document (N ahead).`), and
counted (`llm_admission_lane_active{lane}`, `llm_admission_waiting{lane}`,
`llm_admission_wait_seconds{lane}`, `llm_admission_rejections_total{reason}`).
The point is the fault's trigger shape: nine mixed prefill+decode requests were
in flight at 22:15:50Z. Engine-side knobs that narrow the same shape
(`--max-num-partial-prefills 1`, `--max-long-partial-prefills 1`,
`--max-num-batched-tokens`, `--max-num-seqs`) are evaluated one at a time in
[`CANDIDATE-B.md`](CANDIDATE-B.md); the advertised window stays 1,000,000
tokens and what is configured, tested and production-safe is reported
separately in [`MEMORY-BUDGET.md`](MEMORY-BUDGET.md).

## 5. Request durability and the resume sweep

The invariants are the ones V29 already established (`orchestrator/app/db.py:
_MIGRATION_V29`, `docs/upload-reliability/`): a generation is identified by its
logical id for its whole life; `chat_requests` holds one row per send intent;
a repeated `POST /chat` with the same `intent_id` re-attaches to the live
generation or replays the durable answer, never a second row. This programme
adds what CONTRACT §8.4 v2 requires of every attempt and makes the rule
testable (`orchestrator/tests/test_generation_durability.py`, 13 tests):

- an automatic retry is allowed only **before the first token** and only when
  no completed assistant message exists for the generation (`main.py`: a
  durable answer under the row's generation is replayed whatever the row says,
  and a row a restart marked `interrupted` is healed to `completed` from that
  answer — `_heal_completed_request`; an explicit Stop is never rewritten as
  completed);
- after the first token the attempt is `interrupted`, the partial text is kept
  as a partial and no second answer is appended; the row, the usage ledger and
  the attempt record say the same thing about the same attempt;
- every attempt records `attempt`, `engine` (`primary`), `retry_reason`
  (bounded: `_RETRY_REASONS`, with `queued → recovery`) and `terminal_state`,
  and counts into `chat_request_attempts_total{terminal_state,engine}`;
- **the resume sweep**: when the controller reports READY (the engine-state
  event) and at orchestrator start-up, rows in `queued` / `interrupted` with a
  resumable snapshot are resumed exactly once under a lease
  (`db.resume_chat_request` is a compare-and-swap on the expected
  `generation_id`, so only one sweeper wins); a durable answer found first is
  replayed instead (`duplicate_suppressed`); a row past its patience is
  `expired` — counted, never silently dropped.

The `queued` status is new in v2 (the V29 CHECK constraint lists
`accepted/running/completed/failed/cancelled/interrupted`), so it arrives with a
schema migration alongside `continuity.py`. Drill 14 in
`scripts/recovery-tests/engine_failure_drills.sh` (two `POST /chat` with the
same `intent_id` during a head-API kill: exactly one assistant message) and
drill 15 (a request accepted during the kill is queued, told, and resumes on
the same generation) are the live counterparts; the two SQL checks in
[`SLO.md`](SLO.md) §3 are how an operator proves "never lost, never
duplicated" after any incident.

## 6. Monitoring semantics

The operator's table of the nine states, the signal behind each and the alert
that speaks for it is in
[`../MONITORING.md`](../MONITORING.md#engine-state--what-each-top-level-state-means-and-which-signal-drives-it);
the rules are `monitoring/prometheus/rules/recording.yml` (group `vllm-state`)
and `monitoring/prometheus/rules/alerts.yml` (group `vllm-availability`),
unit-tested in `monitoring/prometheus/tests/`. Four rules of construction
carry the design:

1. **The derived state.** `cluster:vllm_service_state:code` is the top-level
   state the overview tile and the alerts read: `7` (QUEUEING) when the
   controller's state is not READY/BUSY/DOWN and the orchestrator exports
   `llm_queued_generations > 0`; otherwise the controller's own
   `techsara_vllm_state_code`. DOWN (8) is the controller's verdict alone — the
   primary is not serving and the budget is exhausted or the last recovery
   failed — and it means *nobody answers, and the queue is held*.
2. **Freshness.** The controller exports its snapshot's own timestamp
   (`techsara_vllm_generated_at_seconds`); when it is older than 30 s the
   derived state has no sample. A frozen controller tick therefore reads as
   `MONITORING_UNKNOWN`, not as the last state it happened to publish, and
   `VllmReadyUnproven` (critical) fires when READY/BUSY is claimed but
   `cluster:vllm_last_success_age_seconds > 120` — the "process alive, metrics
   answering, engine dead" blind spot moved one level up, closed.
3. **Absence is never zero.** No rule ends in `or vector(0)`. When the
   controller is not scraped the derived series has no sample, Grafana renders
   `noValue` as `MONITORING_UNKNOWN`, and the alert that fires on absence or on
   an explicit code 0 (`VllmMonitoringUnknown`) is worded as *monitoring
   cannot see*. The old `VllmDown` is `VllmScrapeTargetDown`, a warning that
   means exactly what it measures.
4. **Alert identity is stable.** No alert joins the live state name into its
   labels (every state transition would reset its `for:` clock); the state an
   alert speaks for is a fixed label and the live name is an annotation. Two
   series in one rule come from the same scrape (`and on()` against the
   controller's own gauges), so they are present or absent together, and a
   host-level rule never depends on the controller being up.

Every `Vllm*` alert carries labels `service`, `state` and annotations `since`,
`reason`, `runbook` (an anchor in [`RUNBOOK.md`](RUNBOOK.md) §17) and
`safe_command` (read-only, or the one sanctioned recovery entry point). The
queue is visible on its own: `VllmRequestsQueued` (warning, `llm_queued_generations
> 0` for 2 m) while people wait, `VllmQueueDrained` (info) when the sweep has
resumed the last of them. There is no Alertmanager: the alerts are correct,
not yet delivered.

## 7. What the design does not do

- It does not make the primary redundant: SLO A stays at a single replica's
  99.5 % on this build ([`SLO.md`](SLO.md) §4 says why 99.99 % is out of reach
  without Option C).
- **It serves nothing during a reload.** No request class is answered while
  the single 35B instance reloads — by requirement. Every class queues with the
  truthful line and resumes; the wait is ≈ 4–6 min instead of 11. Answers
  *during* a reload need a second TP=2 replica of the same model (Option C,
  +2 Sparks).
- The router is a single instance on the head and is a classifier only; a
  head-node failure takes the primary and routing together, and nothing stands
  in for either. Moving the router to the worker (≈ 22 GiB freed;
  [`MEMORY-BUDGET.md`](MEMORY-BUDGET.md)) is a memory step, not a serving one.
- The second tenant's pipeline calls the raw port over the RoCE address and
  gets none of §4 (no breaker, no queue, no admission lanes); its client-side
  patches are in `../ISSUE/interview-analysis-client/`.
- No released vLLM build closes the fault class
  ([`VLLM-UPGRADE-RESEARCH.md`](VLLM-UPGRADE-RESEARCH.md)); the post-`f6326f5`
  candidate with `--gdn-prefill-backend flashinfer` is evaluated first and the
  secondary engine knobs one at a time ([`CANDIDATE-B.md`](CANDIDATE-B.md)),
  each behind the 120-min soak and seven clean days in production.
