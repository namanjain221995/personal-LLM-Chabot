# Runbook — the main model's availability (v2, strict one-model mode)

Written 2026-09-12 after the 22:15Z outage of 2026-09-11
([`INCIDENT-2026-09-11-vllm.md`](INCIDENT-2026-09-11-vllm.md)); revised the
same day to CONTRACT v2. Every command below exists in this repository at the
path given and was read before it was cited; every number has a source. The
binding names (states, signals, metric names, ports) are in
[`CONTRACT.md`](CONTRACT.md); the design is [`ARCHITECTURE.md`](ARCHITECTURE.md);
the objectives are [`SLO.md`](SLO.md). Alert annotations link to the anchors
in [§17](#17-alerts-one-section-per-anchor).

Run everything on the **head** (spark-0e68, Node 1) from the repository root
unless a line says otherwise. Times in incident records are UTC; the people
reading them are in IST (+05:30) — `scripts/cluster-recover.sh` and the
drills print both.

## 0. Four facts that decide everything

1. **A process being alive proves nothing.** `/health` 200, `/v1/models` 200,
   `/metrics` 200 and `up{job="vllm-main"} == 1` are all answered by the head's
   API process; on 2026-09-11 all four stayed green for 5 min 03 s after the
   worker rank died. READY means the **readiness sequence** passed (CONTRACT
   §5 v2: a non-streaming completion, a streaming one, the token counters
   moved, both GPUs seen working, the rank process alive) and the routine
   canary keeps succeeding. The engine controller
   (`monitoring/engine-controller/controller.py`, compose service
   `engine-controller`, port 9838) is what runs that sequence and publishes
   the state everyone else reads.
2. **Exactly one actor restarts the pair: the controller.** The Docker
   healthchecks report; the sentinel acts only when the controller tells it
   to; `scripts/cluster-up.sh`, `scripts/cluster-down.sh`,
   `scripts/cluster-recover.sh`, `scripts/deploy.sh` and `./techsara up` all
   take the same `flock` on `.runtime/locks/engine-recovery.lock`
   (`scripts/lib/engine-lock.sh`, CONTRACT §6.3) before touching it — §12 says
   who may do what. A head restarted twice is two process groups the worker
   cannot both join.
3. **The one recovery entry point is `scripts/cluster-recover.sh`.** Never
   `docker restart sf-local-ai-vllm-1` on its own (§9 says why).
4. **Only `nvidia/Qwen3.6-35B-A3B-NVFP4` answers a person.** While it reloads,
   nobody is answered: requests are accepted, durably queued, the person reads
   one truthful line, and the same request resumes on the main model when it
   is READY (§10). There is no stand-in model; the router classifies and never
   answers. The only way to serve answers *during* a reload is a second TP=2
   replica (ADR-0002, Option C).

Nothing pages anyone. There is no Alertmanager; alerts are visible on
Prometheus `/alerts` (`127.0.0.1:9090`) and in Grafana (`127.0.0.1:3300`) only
([`../MONITORING.md`](../MONITORING.md), "There is no Alertmanager"). "Page a
human" below means: a person has to be looking.

## 1. Current status

```bash
scripts/cluster-status.sh            # both nodes, both containers, the controller's /state rendered, NCCL transport
scripts/cluster-status.sh --probe    # plus one streaming completion with GPU sampling on BOTH GB10s
curl -s http://127.0.0.1:9838/state | jq '.state,.state_code,.reason,.since,.incident,.recovery'
curl -s http://127.0.0.1:9838/state | python3 -m json.tool     # the whole document (CONTRACT §6.2)
./scripts/monitoring.sh verify       # what Prometheus holds: controller state, derived state, canary, queue depth, breaker
curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(queued_generations|engine_state_code|breaker_state)'   # the orchestrator's side
```

`cluster-status.sh` exits non-zero on a critical failure (a link down, a
container unhealthy, the API not answering) and treats a non-READY controller
as a warning, so the rest of the report still prints. Its "Engine controller"
block is `/state` rendered, nothing inferred.

The nine states (`state_code` in `/state`, `techsara_vllm_state_code` in
Prometheus; CONTRACT §2):

| code | state | it means | what to do |
|---|---|---|---|
| 0 | `MONITORING_UNKNOWN` | telemetry cannot be observed; inference **unproven**, not down | §4, then §2. Never restart anything on this state alone |
| 1 | `STARTING` | containers up, model loading, no canary success yet since the head started; cold-start budget 900 s | wait; `scripts/cluster-logs.sh head --tail 100` shows where the load is; a compile-error line there is §13 |
| 2 | `READY` | the readiness sequence passed and a canary succeeded within the last 2 probe intervals, `/metrics` fresh, both ranks alive | nothing |
| 3 | `BUSY` | READY and `num_requests_running > 0` | nothing |
| 4 | `DEGRADED` | the canary still succeeds but a non-critical signal failed (sentinel unreachable, metrics stale, canary TTFT > 10 s, the router classifier unhealthy, a GPU exporter unreachable) | read `.reason`; §4, §7, §8 |
| 5 | `WEDGED` | requests exist but neither token counter moved for ≥ 90 s, or the canary is outstanding ≥ 60 s while `/health` answers 200; two observations | the controller is about to recover (or cannot — `.reason` says why): §17 [`#wedged`](#wedged) |
| 6 | `RECOVERING` | a coordinated recovery holds the lock, from detection to the readiness sequence | watch `.recovery.step`; do not touch the pair |
| 7 | `QUEUEING` | derived in Prometheus only: primary not READY/BUSY **and** the orchestrator holds ≥ 1 accepted generation (`llm_queued_generations > 0`) | people are waiting with the truthful line; nothing answers them until READY; §10 |
| 8 | `DOWN` | the primary is not serving and the budget is exhausted or the last recovery failed; queued requests are held, never answered elsewhere | §17 [`#primary-down`](#primary-down), then §9 |

The other lines of `/state` worth reading first: `.signals.canary`
(`ok`, `ttft_s`, `consecutive_failures`, `last_success_at`),
`.signals.worker` (`reachable`, `rank_process_alive`, `restart_count`,
`last_fault`), `.signals.engine` (`requests_running`, `frozen_seconds`),
`.signals.gpus` (`head_util`, `worker_util`, `sampled_at` — the last
participation probe), `.signals.router` (the classifier's health — a DEGRADED
input, never a serving path), `.recovery` (`in_progress`, `step`,
`attempts_in_window`/`budget`, `cooldown_until`), `.incident` (`id`,
`category`, `attempts`, `ended_at`).

## 2. A real probe

```bash
scripts/cluster-verify-engine.sh --probe
```

Reads the **running** command line of both ranks from `docker inspect` on both
nodes (never from `.env`), checks the flags that matter (`--tensor-parallel-size
2`, `--nnodes 2`, `--distributed-executor-backend mp`, `--enable-chunked-prefill`,
no `--speculative-config`, `--no-enable-prefix-caching`), diffs the arguments
between the ranks, compares `generated.env` with the running head (drift), greps
both retained logs for CUDA fault lines and both kernel journals for `Xid`,
confirms NCCL `Using network IB`, prints both restart counters, and with
`--probe` sends one 160-token completion while sampling both GPUs at 4 Hz:
PASS needs `completion_tokens > 0` and a peak ≥ 30 % on **both** GB10s. Exit 1
on any FAIL. Known pre-existing FAIL on **both** nodes: "Xid lines in the
kernel log since boot" — 2 lines each on 2026-09-12 (head: the 07:05:33Z
rank-0 fault; worker: the 22:15:50Z rank-1 fault; both 2026-09-11, both the
Xid 13 + 31 pair of §6). Record the count and require it not to grow; a
reboot resets it.

The controller's routine canary, by hand (CONTRACT §5; the head is
host-network on port 8000 in dual mode). No tools, no thinking, no user,
nothing stored:

```bash
MODEL=$(curl -s http://127.0.0.1:8000/v1/models | jq -r '.data[0].id')
curl -sN -m 60 http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}],
       \"max_tokens\":4,\"temperature\":0,\"seed\":7,\"chat_template_kwargs\":{\"enable_thinking\":false},\"stream\":true}"
```

A healthy pair streams the first chunk in well under a second (TTFT ≈ 0.07–0.4 s
at c=1, `SLO.md` §2) and ends with `data: [DONE]`. A `-m 60` timeout with
`/health` still 200 is the wedge signature — do not repeat it in a loop; read
`/state` (§1).

The full **readiness sequence** the controller runs after every start and
recovery (CONTRACT §5 v2), in order, all five required before READY: (1) a
non-streaming 32-token completion, `temperature 0`, `seed 7`, thinking off;
(2) a streaming 32-token completion with TTFT measured and the terminal chunk
seen; (3) `vllm:generation_tokens_total` on `/metrics` increased by at least
the tokens received; (4) both GPU exporters (head `127.0.0.1:9835`, worker
`<CLUSTER_WORKER_MGMT_IP>:9835`, sampled every 250 ms) above
`PARTICIPATION_MIN_UTIL=30 %` at least once during a 256-token participation
probe; (5) the sentinel reports the rank process alive. `techsara_vllm_participation_ok`
is the recorded result of step 4; `.signals.gpus` holds the peak samples.

## 3. Both GPUs

A TP=2 completion cannot finish without rank 1 (the all-reduce is synchronous),
so a completed canary is already proof of both ranks; step 4 of the readiness
sequence records the evidence. Longer proof, with the GPUs watched:

```bash
scripts/cluster-verify-engine.sh --probe     # peak ≥ 30 % on both nodes during one completion, else FAIL
scripts/cluster-status.sh --probe            # "Both GPUs participating: YES/NO" from 2 Hz sampling on both nodes
curl -s http://127.0.0.1:9838/metrics | grep -E '^techsara_vllm_(both_ranks_ok|participation_ok|worker_rank_alive|worker_reachable) '
curl -s http://127.0.0.1:9838/state | jq '.signals.gpus'
```

`techsara_vllm_both_ranks_ok` is 1 only when the last canary succeeded **and**
the sentinel on Spark 2 says the `VLLM::Worker_TP1` process is alive (signal
13); `techsara_vllm_participation_ok` is 1 when both GPUs exceeded 30 % during
the last readiness sequence.

## 4. Monitoring targets

```bash
curl -s http://127.0.0.1:9090/api/v1/targets | jq '.data.activeTargets[]
  | select(.labels.job=="engine-controller" or .labels.job=="vllm-main")
  | {job:.labels.job, health, lastError, lastScrape}'
./scripts/monitoring.sh status               # every target with UP/DOWN and its last error
curl -s http://127.0.0.1:9838/healthz        # "ok" = the controller process is alive (liveness only)
curl -s http://127.0.0.1:9838/metrics | grep -E '^techsara_vllm_generated_at_seconds'   # the snapshot's own age: > 30 s = the tick is stuck
docker ps --filter name=engine-controller    # the container
curl -s http://127.0.0.1:8080/health | jq '.checks | to_entries[] | select(.value.engine) | .value.engine'   # what the orchestrator believes, and how old that belief is
```

The `engine-controller` job scrapes `host.docker.internal:9838` every 5 s
(`monitoring/prometheus/prometheus.yml`). The worker sentinel (`:9839`) is
deliberately **not** a scrape target: it binds to the RoCE rail-A address and
everything it knows is folded into `techsara_vllm_worker_*` by the controller.
The controller probes the head where the head actually listens
(`ENGINE_HEAD_API_URL`, launcher-generated from the rendered bind address); an
empty URL is "not observable" (`MONITORING_UNKNOWN`), never DOWN. The
orchestrator reaches the controller at `ENGINE_CONTROLLER_URL` (launcher-
generated per mode; `http://vllm:9838/state` in dual mode) — a permanently
`unknown` verdict (`llm_engine_state_code == -1`) means that URL is wrong for
the layout, and it is audible in the orchestrator log.

`MONITORING_UNKNOWN` (code 0, the Grafana tile's `noValue`) means exactly:
**no sample, or a sample that says "cannot observe"**. The controller is down,
Prometheus cannot reach it, its snapshot is older than 30 s
(`techsara_vllm_generated_at_seconds` — a stuck tick is not a verdict), or the
controller itself cannot observe (Docker socket unreadable and no canary
success in the window). It is never rendered as DOWN, no recording rule ends
in `or vector(0)`, and the only alert that fires on absence
(`VllmMonitoringUnknown`) says "cannot see", not "down"
([`../MONITORING.md`](../MONITORING.md), "What no data means"). Fix the
telemetry, then read the state; inference may be fine the whole time (drill 1
in `scripts/recovery-tests/engine_failure_drills.sh` proves it).

## 5. Logs and evidence

```bash
docker logs --tail 400 sf-local-ai-vllm-1                    # head: API server + EngineCore + Worker_TP0
docker logs --tail 200 sf-local-ai-engine-controller-1       # controller: one line per transition, DETECT/CONFIRM, step, restart
scripts/cluster-logs.sh worker --tail 400                    # worker (ssh): Worker_TP1 — CUDA faults land here; the sentinel's lines are interleaved (same project)
scripts/cluster-logs.sh nccl                                 # NCCL init/transport lines from both nodes
scripts/cluster-logs.sh head -f                              # follow
WORKER_IP=$(grep -m1 '^CLUSTER_WORKER_IP=' .runtime/generated.env | cut -d= -f2)
curl -s "http://$WORKER_IP:9839/diagnostics" | tail -100     # sentinel: the worker container's last 400 log lines (RoCE address; needs the token when one is set)
curl -s "http://$WORKER_IP:9839/state" | jq .                # sentinel's own view: container, rank_process_alive, last_fault, self_restarts_in_window
ls -t .runtime/incidents | head -5                           # one directory per incident id
```

The incident directory `.runtime/incidents/<id>/` (gitignored; bind-mounted
into the controller at `/run/techsara/incidents`; owned by the checkout owner
even when the controller creates it) is written **before** the controller
restarts anything. Files per attempt `n`: `head-logs-<n>.txt` (`docker logs
--tail 400`, de-multiplexed), `worker-diagnostics-<n>.txt` (sentinel
`/diagnostics`), `state-<n>.json` (the `/state` document at detection).
`scripts/cluster-recover.sh --force` writes its own `manual-<utc>/` directory
with `head-logs.txt`, `worker-logs.txt`, `head-inspect.json`,
`controller-state.json`, `head-metrics.txt` and `incident.txt` (actor, mode,
start/end in both clocks, result). The 2026-09-11 bundle is
`.runtime/incidents/20260911T223140Z-vllm-down/`.

Both engine logs rotate (`json-file`, 10 MB × 3): the 2026-09-11 head log lost
the incident window to rotation, which is why capture happens first.

## 6. Xid check (GPU faults)

`Xid 13` (graphics exception, class `0xcec0`) + `Xid 31` (MMU fault,
`FAULT_PTE VIRT_READ @0x0`) is the signature of this fault class
(`VLLM-UPGRADE-RESEARCH.md` §1.3). Both kernels:

```bash
journalctl -k --no-pager | grep -E 'Xid|NVRM' | tail -20                    # head
( . scripts/lib/cluster-common.sh; cluster_load_settings
  ssh_worker "journalctl -k --no-pager | grep -E 'Xid|NVRM' | tail -20" )   # worker, through the same ssh helper the scripts use
```

`ssh_worker` (`scripts/lib/cluster-common.sh`) is `ssh -o BatchMode=yes` to
`CLUSTER_WORKER_SSH` (default `<you>@<CLUSTER_WORKER_IP>`, key auth only); the
subshell is there because `cluster-common.sh` sets `-euo pipefail`. Reading the
kernel journal needs the `adm` or `systemd-journal` group;
`scripts/cluster-verify-engine.sh` counts Xid lines on both nodes and reports
them. The worker's log signature that goes with an Xid is
`RuntimeError: Triton Error [CUDA]: misaligned address` /
`c10::AcceleratorError` — the sentinel's `last_fault.signature` and
`.signals.worker.last_fault` in `/state` carry it.

## 7. Memory check

GB10 has no separate framebuffer: a "GPU allocation" is host memory, and the
head at 82 % used is normal ([`MEMORY-BUDGET.md`](MEMORY-BUDGET.md)). What
matters is `MemAvailable` and swap-out rate.

```bash
free -g; grep -E 'MemAvailable|SwapFree' /proc/meminfo; cat /proc/pressure/memory
docker stats --no-stream | sort -k4 -h | tail -8
( . scripts/lib/cluster-common.sh; cluster_load_settings; ssh_worker "free -g; cat /proc/pressure/memory" )
# Prometheus (the alert's own expression; > 500 pages/s for 5 m = HeadSwapActivity)
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=rate(node_vmstat_pswpout{node="spark-1"}[5m])' | jq -r '.data.result[]|.value[1]'
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=node:memory_used_percent:current' | jq -r '.data.result[]|[.metric.node,.value[1]]|@tsv'
```

Measured: `MemAvailable` on the head fell to 17.9 GiB under load and the head
paged out 1,286 pages/s during the 2026-09-11 reload; a model start beside the
other residents logged `NV_ERR_NO_MEMORY` twice earlier that day. Nothing new
may be loaded on the head (ADR-0002); the next memory step is the router to the
worker (§20). The 1M-token window's configured, tested and production-safe
figures are separate numbers (`MEMORY-BUDGET.md`); prompts above 131,072
tokens are admitted one at a time onto an idle engine (§10).

## 8. RDMA / NCCL check

```bash
scripts/cluster-doctor.sh            # read-only: links, RDMA state, MTU both ends, /dev/infiniband, ssh, docker/GPU, image, model, ports, ufw, memory
scripts/cluster-status.sh            # its "NCCL" block: RDMA/RoCE with N HCAs, or "TCP SOCKET FALLBACK"
cat /sys/class/infiniband/{rocep1s0f1,roceP2p1s0f1}/ports/1/state
docker logs sf-local-ai-vllm-1 2>&1 | grep -m3 'NCCL INFO NET/'
```

The two options are **not** read-only and share the fabric and the GPUs with
the live engine: `--rdma` runs `ib_write_bw` for 3 s per rail (needs `perftest`
on both nodes) and `--nccl` runs `scripts/cluster-test.sh`, a two-node NCCL
all-reduce inside the pinned vLLM image with `--gpus all` on both nodes. Run
them in an announced window, never during a recovery. Healthy figures: 108.9
Gb/s per rail (`ib_write_bw`), 171.6 Gb/s NCCL busbw (`../CLUSTER.md`).

NCCL cannot use RDMA (missing `/dev/infiniband`, memlock, an HCA down) → it
silently uses TCP sockets; `cluster-status.sh` warns `TCP SOCKET FALLBACK` and
Prometheus infers it as `NcclTcpFallback` (§17). On 2026-09-11 the fabric was
ruled out: both rails ACTIVE, `Using network IB`, zero RoCE errors, MTU
matched.

## 9. Coordinated restart

**The** command. It asks the controller, which owns the choreography and the
lock, and follows `/state` with both clocks until READY:

```bash
scripts/cluster-recover.sh                     # POST /recover to 127.0.0.1:9838, then follow /state (default --timeout 1200)
scripts/cluster-recover.sh --timeout 900
```

What the controller then does (CONTRACT §6.3; every step in `.recovery.step`
and in its log with the incident id): `confirm` (take the lock, count the
attempt, jitter 0–5 s) → `capture` (head log tail, sentinel `/diagnostics`,
`/state` → `.runtime/incidents/<id>/`) → `stop_pair` (sentinel `POST /restart`
on the **worker first**, so it is waiting at the rendezvous; then
`docker restart -t 10` of the head) → `wait_load` (`/health` 200 from the new
container start, budget `COLD_START_BUDGET_S=900`; measured 3 m 32 s warm,
5 m 20 s cold, less when the compiled-kernel caches are warm — §13) → `canary`
(the five-step readiness sequence of §2 on the **new** head) → `mark_ready`
(lock released, cooldown 120 s armed, READY) → **the orchestrator resumes every
queued generation exactly once** (§10) → `verify` (three consecutive canary
successes → `incident.ended_at`). A manual `POST /recover` is **subject to the
same budget and cooldown** as an automatic one and never bypasses the lock;
past the budget it is refused synchronously (`409`/`429`, "recovery budget
exhausted") and the escalation is `--force` below. The script waits until the
controller has taken the request (`.recovery.in_progress` observed true, or a
new incident id) before it judges anything, so its verdict is the outcome of
*this* recovery, not of the state it found. Exit 0 = the readiness sequence
passed on the recovered pair; 1 = it did not, or the controller stood down;
2 = usage or precondition. `403` = you are not on the head; `409` = a recovery
is already running — follow it, do not start another.

When the controller is down, or has stood down (`DOWN`, `budget_exhausted`):

```bash
scripts/cluster-recover.sh --force                        # same lock, same order, by hand; never POSTs
scripts/cluster-recover.sh --force --clear-kernel-cache   # the same, after emptying both kernel-cache volumes (§13)
```

`--force` takes the engine lock (waits up to `ENGINE_LOCK_WAIT`, default 600 s,
and prints the holder), captures diagnostics into
`.runtime/incidents/manual-<utc>/`, restarts the **worker** (`worker_compose
restart --timeout 5` — SIGKILL after 5 s also interrupts a core dump in
progress), then the head (`docker restart -t 10`), waits for `/health` 200
within `COLD_START_BUDGET_S`, requires a real completion (6 attempts, 10 s
apart), then runs `scripts/cluster-verify-engine.sh --probe`. If the controller
is answering and may still have a manual request pending, `--force` warns and
waits for it rather than restarting a pair the controller is about to restart;
the controller observes the manual restart as a cold start and runs the
readiness sequence on it.

The lock, read-only:

```bash
( . scripts/lib/engine-lock.sh; if engine_lock_is_held; then echo HELD; engine_lock_holder; else echo free; fi )
ls -l .runtime/locks/            # the lock file must be openable by the checkout owner; "cannot open lock (permissions)" ≠ "held"
```

**What NOT to do.** `docker restart sf-local-ai-vllm-1` by itself. A restarted
head is a new `torch.distributed` process group; the running worker is still
in the old one, joins the old head's TCP store, and is reset when that head
dies (2026-09-11 22:21:10 → 22:22:44: two worker restarts, one wasted
rendezvous). Since 2026-09-12 (`6a667f6`) the controller copes: it sees the
head's `started_at` move, re-pairs the worker through the sentinel and runs
the readiness sequence (§12; drill 6: READY 165 s after the bare restart) —
still a reload nobody is answered during, and still not a procedure. Nor
`scripts/cluster-worker.sh restart` alone: the head loses rank
1, hangs its next collective, and only vLLM's 300 s
`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` ends it. Nor anything while `.recovery.in_progress`
is `true` or the lock is held. Nor a restart of the orchestrator to "clear"
queued requests — the rows are durable and the sweep re-runs at start-up
(§10); restarting it only delays them. Drills 6 and 7 exist to prove the
controller copes with the first two mistakes; they are drills, not procedures.

Budget: 3 recoveries per 3600 s (`ENGINE_RECOVERY_BUDGET`,
`ENGINE_RECOVERY_WINDOW_S` in `.env` → compose), manual ones included. When it
is spent the controller goes `DOWN` / `budget_exhausted`, takes **no further
destructive action**, keeps probing, holds the queued requests (they are never
answered elsewhere), and returns to READY by itself if the readiness sequence
passes — a passing canary wins over a sentinel document that says otherwise. A
human decides whether to restart again (`--force`, or wait for the window) —
after reading `.runtime/incidents/<id>/`: a rank that dies on load is a
different problem from one that dies under mixed prefill+decode load, and a
compile-error signature in the head log is §13, not another restart.

## 10. Queued requests during a recovery

There is no other model. When the breaker for `main` is not CLOSED (observed
failures, or the controller reporting STARTING / WEDGED / RECOVERING / DOWN —
CONTRACT §8.2) every request class — plain chat, `chat_with_tools`, vision,
Deep Research, video, the artifact composer — is **accepted, persisted and
held** (CONTRACT §8.3 v2, `orchestrator/app/continuity.py`). Nothing is
configured for this; it is not a mode that can be off.

**What the person sees, exact text.** One line, once, and the stream stays
alive with heartbeats (no spinner; the client renders a queued state):

- on entry: `Main model is recovering—your request is safely queued.`
- if the wait outlives `LLM_QUEUE_MAX_WAIT_S=900`: `The main model is still
  recovering. Your request is kept and will resume automatically.` — the row
  stays `queued`; never a 500, never a smaller model's answer.
- a prompt above 131,072 tokens waiting for an idle engine (CONTRACT §6.7,
  `orchestrator/app/admission.py`): `Waiting for the model to finish current
  work before your large document (N ahead).`

**What happens underneath.** The V29 `chat_requests` row is written first
(status `queued`, `generation_id` and `intent_id` fixed for its whole life);
the worker waits on the engine-state client's READY **event** (nobody polls
the dead port); on READY the **same** generation resumes (`attempt` + 1,
`retry_reason=recovery`), exactly once — the V29 guards (`UNIQUE
generation_id`, the one-message-per-generation index, the compare-and-swap in
`db.resume_chat_request`) make a second answer impossible. Rows the process
did not get to (the wait expired, the orchestrator restarted) are taken by the
**resume sweep** when the controller reports READY and at orchestrator
start-up, under a lease, exactly once; a durable answer found first is
replayed instead of re-generated. The sweep's rules as implemented
(`continuity.py`, module docstring): a `queued` row is resumed **whatever its
age** (a parked request is a promise); an `interrupted` row is resumed only
within `LLM_RESUME_MAX_AGE_S=3600` of its `created_at` — the moment the send
was accepted, not the interruption; an `interrupted` attempt that had
**already streamed tokens is never re-run** — the partial the person read
stays, the row is settled `failed` with `The answer was interrupted after it
had started; what was written is kept above. Retry to ask again.` (or `… and
could not be resumed. …` when no partial exists), so a person may retry. Two
sweep triggers never drop each other.

**Watch it.**

```bash
curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(queued_generations|queue_wait_seconds_(sum|count)|resumed_generations_total|breaker_state|engine_state_code|admission_(lane_active|waiting))'
curl -s http://127.0.0.1:8080/health | jq '.checks | to_entries[] | select(.value.engine) | .value.engine'   # controller snapshot, breaker, queue depth
docker logs --tail 200 sf-local-ai-orchestrator-1 | grep -E 'llm\.breaker|continuity|resume'
```

`llm_queued_generations > 0` while the primary is not READY/BUSY is state 7
(`QUEUEING`) in Grafana's derived `cluster:vllm_service_state:code` and fires
`VllmRequestsQueued` (warning) after 2 m; `VllmQueueDrained` (info) follows
the sweep. `llm_resumed_generations_total{outcome}` counts `resumed`
(the normal case), `expired` (a row past its patience — never silently
dropped) and `duplicate_suppressed` (a second client or a second sweeper found
the answer already durable).

**Prove exactly-once after any incident** (the durability report's checks;
each must return **no rows** — `SLO.md` §3 has the full set):

```bash
docker exec -i sf-local-ai-postgres-1 psql -U "${POSTGRES_USER:-techsara}" -d "${POSTGRES_DB:-techsara}" <<'SQL'
-- DUPLICATES: more than one non-error assistant message for one intent
SELECT r.intent_id, count(DISTINCT m.id) AS answers
FROM chat_requests r
JOIN usage_events u ON u.meta->>'intent_id' = r.intent_id
JOIN messages m ON m.conversation_id = r.conversation_id AND m.generation_id = u.generation_id
               AND m.role = 'assistant' AND NOT (m.meta ? 'error')
GROUP BY r.intent_id HAVING count(DISTINCT m.id) > 1;
-- LOST: neither cancelled, nor queued, nor answered, and nobody is working on it
SELECT r.intent_id, r.status, r.attempt, r.updated_at
FROM chat_requests r
LEFT JOIN messages m ON m.conversation_id = r.conversation_id AND m.generation_id = r.generation_id AND m.role = 'assistant'
WHERE m.id IS NULL AND r.status NOT IN ('cancelled', 'queued') AND r.updated_at < now() - (900 * interval '1 second');
-- ANOTHER MODEL: must be 0
SELECT count(*) FROM usage_events WHERE meta ? 'engine' AND meta->>'engine' <> 'primary';
SQL
```

`chat_requests_interrupted` (orchestrator `/metrics`) must return to 0 after
every recovery; a `queued` row older than 900 s while the primary is READY is
a sweep that did not run — read the orchestrator log, then §11.

## 11. Primary restoration

Nothing to run. The sequence, and how to see each step:

1. The readiness sequence passes on the new pair → `mark_ready` → state
   `READY` (`/state .state`, `VllmPrimaryRestored` info after 30 s).
2. The orchestrator's breaker for `main` was held OPEN by the controller's
   verdict; when the verdict is READY/BUSY the hold lifts, the breaker goes
   `HALF_OPEN` after `LLM_BREAKER_COOLDOWN_S=10`, admits exactly **one**
   bounded canary call (settled on its first token, not on the response
   headers), and closes on success (`llm_breaker_state{engine="main"}` 1 → 2 → 0;
   `llm_breaker_transitions_total{engine="main",to="CLOSED"}`; one log line
   `llm.breaker engine=main from=HALF_OPEN to=CLOSED reason=…`).
3. The READY event wakes every waiting generation; the resume sweep takes the
   rest under a lease; `llm_queued_generations` → 0 and
   `llm_resumed_generations_total{outcome="resumed"}` climbs by the number that
   waited (`VllmQueueDrained` info).
4. `verify`: three consecutive canary successes → `.incident.ended_at` set —
   also when the engine came back by itself or by an operator's `--force`.

Confirm READY, both ranks and an empty queue:

```bash
curl -s http://127.0.0.1:9838/state | jq '.state,.signals.canary.ok,.signals.worker.rank_process_alive,.signals.gpus,.incident.ended_at'
curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(breaker_state|queued_generations|resumed_generations_total)'
scripts/cluster-verify-engine.sh --probe
```

Then the exactly-once checks of §10 and the incident's row in `SLO.md` §5.

## 12. Single recovery authority — who may do what

CONTRACT §2 and §6: **exactly one actor restarts the pair**. Everything else
reports, or acts only on that actor's instruction, or waits for its lock.

| actor | may | may not | when it acts |
|---|---|---|---|
| **engine controller** (`sf-local-ai-engine-controller-1`, head, Docker socket RW) | confirm a failure (§9's triggers), capture diagnostics, `POST /restart` to the sentinel, `docker restart` the head, publish state, run the readiness sequence | restart outside the budget/cooldown; delete a kernel cache; act on a stale or unproven signal past the budget | within one tick (5 s) of confirmation, under the lock |
| **worker sentinel** (`sf-local-ai-worker-vllm-worker-sentinel-1`, Node 2, that node's Docker socket) | observe the rank process and the fault signatures; answer `/state`, `/diagnostics`; restart the worker container **on the controller's `POST /restart`** (token-gated when `CLUSTER_SENTINEL_TOKEN` is set) | restart on its own (`SENTINEL_AUTONOMOUS=0` in its environment — `.env` key `CLUSTER_SENTINEL_AUTONOMOUS`, default 0 — in production; the bounded self-restart exists only for a deployment without a controller) | ≤ 5 s to see; acts only when told |
| **Docker healthchecks** (head in `compose/compose.cluster-dgx-spark.yaml`, worker in `compose/compose.cluster-worker.yaml`) | exit 1 so `docker ps` shows `unhealthy` | kill anything while a controller is alive | one documented last-resort tier only: `kill -9` after `VLLM_HEALTHCHECK_KILL_AFTER` consecutive misses (default 8 = 4 min), long after a live controller would have acted |
| `scripts/cluster-recover.sh` | ask the controller (`POST /recover`, loopback); `--force`: the same choreography by hand under the lock | POST and force in one run; bypass the budget through the controller | when a person runs it |
| `scripts/cluster-up.sh`, `scripts/cluster-down.sh`, `scripts/deploy.sh` (`--full`), `./techsara up` | restart the pair as a deployment, under the same lock | run while a recovery holds the lock (they wait, default 20 min) | in a change window |
| the orchestrator | open/close its breaker, queue and resume requests | touch any container | continuously |
| a person | everything above through the scripts | `docker restart` / `docker compose up` on `vllm` or `vllm-worker` by hand | after reading `/state` |

**A head restarted by someone else is re-paired, not restarted again** (rule
added 2026-09-12 after drill 5, commit `6a667f6`; CONTRACT §6.3). When the
head's `started_at` moves and the controller did not move it — Docker's
`restart: unless-stopped` after the API process died, or an operator's bare
`docker restart` — the controller does **not** open a budgeted pair recovery.
It takes the lock, records the incident with category
`head_restarted_externally`, and asks the sentinel (`POST /restart`) to
restart the **worker** so a fresh rank 1 waits at the new head's rendezvous;
the head is left alone; the budget is untouched; the outcome is published as
`recovery.external_repair` in `/state` and counted as
`techsara_vllm_recovery_attempts_total{outcome="repaired_worker"}`. The state
reads STARTING with the reason `head restarted outside the controller;
re-pairing the worker` until the readiness sequence passes. Before this rule
the new head waited at the rendezvous for a worker still joined to the old
head, and only the two last-resort healthcheck tiers ended it: 767 s to READY
in the first drill-5 run of 2026-09-12 (`.runtime/drills/20260912T095548Z/`).
After it: 165 s (drill 6) and 161 s (drill 5 re-run). If the sentinel refuses
or is unreachable the controller records that outcome (`refused`,
`unreachable`) and logs that rank 1 stays paired with the previous head until
the worker's last-resort tier or an operator restarts it. When another actor
holds the engine lock at that moment (`scripts/cluster-recover.sh --force`,
`cluster-up.sh`, a deploy) the controller records `lock_held` and leaves the
pair to that actor — the rule never races a restart in progress.

### 12.1 Drills of 2026-09-12 — the evidence

All records are under `.runtime/drills/<utc>/` in the deploy checkout
(gitignored; each prints UTC and IST) with the wrapper logs in
`.runtime/logs/drill*.log`. All on candidate B (`sha256:819ec9c0…`), warm
kernel caches after the first start of 08:39:59Z. "Detection" = the break
to the controller leaving READY; "READY" = the break to READY through the
full readiness sequence.

| Drill | Record (`.runtime/drills/…`) | Verdict on file | Detection | READY | Notes |
|---|---|---|---|---|---|
| 16 three coordinated restarts (`POST /recover`) | `20260912T084519Z/drill-16-tp2-repeated-startup.log` | PASS 19/0 | — | 188 / 186 / 180 s | worker 2–3 s before the head each time; GDN line on both ranks each time; `torch.compile` 4.96 / 3.27 s on the reuse starts vs 20.56 s cold; budget 0/3 left at 08:58:39Z |
| 3 `kill -9 VLLM::Worker_TP1` | `20260912T094545Z/drill-3-kill-worker-rank-process.log` | 11 pass / 1 fail | 6 s, `worker_rank_dead` | 176 s | worker container restarted 7 s after the kill; worker before head; the FAIL is the verify script's exit status: the since-boot Xid baseline (2 per node all day) plus, from this drill on, one fault-pattern line in the worker's retained log (`died unexpectedly`, the killed rank's own aftermath); re-baselined in `e3faf59` |
| 4 `kill -9 VLLM::EngineCore` | `20260912T095049Z/drill-4-kill-head-enginecore.log` | 11 pass / 1 fail | 4 s, `head_engine_dead` | 172 s | same baseline FAIL |
| 5 `kill -9` the head API, first run | `20260912T095548Z/drill-5-kill-head-api.log` | 14 pass / 5 fail | 5 s | **767 s** | the finding: Docker restarted the head in 5 s, the controller did not re-pair the worker; the two last-resort tiers ended it (head 10:01:23Z, worker 10:06:06Z). Fixed in `6a667f6` |
| 15 / 14 on it | `20260912T095548Z/drill-5-chat-{a,b}.json` | (in the record) | — | — | both chats 200 after 779.9 / 774.9 s; sentence read; same generation (143 chars) after READY; `llm_queued_generations` stayed 1 after the resume — fixed in `6a667f6`; one assistant row for the intent sent twice |
| 6 bare `docker restart -t 10` of the head | `20260912T104332Z/drill-6-restart-head-only.log` | 5 pass / 1 fail | 3 s | 165 s | worker re-paired in 12 s; no second head restart; the FAIL is the baseline |
| 5 re-run after `6a667f6` | `20260912T104624Z/drill-5-kill-head-api.log` | 17 pass / 2 fail | 5 s | 161 s | STARTING, `re-pairing the worker`; worker restarted 11 s after the new head; `head_restarted_externally`, budget 3/3; the FAILs are the drill's own RECOVERING barrier (made a pass in `e3faf59`) and the baseline |
| 15 / 14 on the re-run | `20260912T104624Z/drill-5-chat-{a,b}.json` | (in the record) | — | — | both chats 200 after 174.8 / 169.9 s; sentence read; same generation (130 chars) after READY; queue drained to 0 within 60 s; one assistant row |

Not run: drills 1, 2 (monitoring-only), 7 (worker-only restart), 8, 9
(network, manual), a wait forced past `LLM_QUEUE_MAX_WAIT_S`, the two-request
admission test. No drill was re-run after `e3faf59`, so the verdicts on file
are as listed. The wedge rule's behaviour under the ~950K needle
(WEDGED 09:04–09:16Z, restart refused only by the spent budget) is
`INCIDENT-2026-09-11-vllm.md` §7.2 and §17 `#wedged` below.

How to see that the rule holds: after any recovery, `RestartCount` moved by
exactly one on each container (`docker inspect sf-local-ai-vllm-1 --format
'{{.RestartCount}} {{.State.StartedAt}}'`; worker via `scripts/cluster-status.sh`),
the controller log names one incident id, and the healthcheck log line
`restarting the head` does **not** appear (`docker inspect sf-local-ai-vllm-1
| jq '.[0].State.Health.Log[-3:]'`). Drills 6 and 7 check the same thing.
Rolling back the compose files must remove the guards too (§16), or an
orphaned controller keeps restarting the pair beside the restored watchdog —
three actors, the 2026-09-11 pattern.

## 13. Kernel caches

Both ranks keep their compiled kernels — torch.compile artefacts and the
FlashInfer JIT output — on persistent named volumes mounted at
`/root/.cache/vllm` and `/root/.cache/flashinfer` (`vllm-kernel-cache` on the
head project, `sf-local-ai-worker_kernel-cache` on the worker; CONTRACT §6.6),
so a recovery reuses what the last successful start validated instead of
compiling again (cold compile measured 50 s; a FlashInfer GDN JIT is longer).
The two ranks compile their own copies on their own nodes.

```bash
docker volume inspect vllm-kernel-cache | jq '.[0].Mountpoint'
docker exec sf-local-ai-vllm-1 sh -c 'du -sh /root/.cache/vllm /root/.cache/flashinfer 2>/dev/null'
( . scripts/lib/cluster-common.sh; cluster_load_settings
  ssh_worker "docker exec sf-local-ai-worker-vllm-worker-1 sh -c 'du -sh /root/.cache/vllm /root/.cache/flashinfer'" )
docker logs sf-local-ai-vllm-1 2>&1 | grep -E 'torch\._dynamo|flashinfer\.jit|nvcc|cuda_nvrtc|Compiling|compilation' | tail -20   # the compile-error signatures
curl -s http://127.0.0.1:9838/metrics | grep -E '^techsara_vllm_cold_start_seconds'   # measured once per head start; a cache miss shows here first
```

**When a cache is the problem.** A start that stays in `STARTING` past its
usual time and ends in `DOWN` / `cold_start_timeout`, with one of the
compile-error signatures in the head (or worker) log — `torch._dynamo`,
`flashinfer.jit`, `nvcc`, `cuda_nvrtc` — is a stale or broken cache: an
artefact compiled by a different build, or a half-written file from a start
that was killed mid-compile. A new engine image (§14) always warrants a
clear; an engine-argument change usually does not.

**Clearing it.** The controller never deletes a cache. The one sanctioned
way empties **both** volumes under the engine lock and restarts the pair in
order:

```bash
scripts/cluster-recover.sh --force --clear-kernel-cache
```

Expect the next start to be slow once (the compile is repeated and the
first-start warm-up of the GDN kernels runs again); `techsara_vllm_cold_start_seconds`
records it, and the next recovery is back to the warm figure. Never `docker
volume rm` the cache of a running rank, and never clear one node only — the
two ranks must build identical `VllmConfig`s and identical kernels.

## 14. Candidate image switch

The fault class is open on the pinned build (`VLLM-UPGRADE-RESEARCH.md`); the
one candidate is the post-`f6326f5` build (the first nightly after #55715,
`nightly-385dce36…`, digest `sha256:819ec9c0…`) with the FlashInfer GDN prefill
kernel selected explicitly, evaluated **first** and by the A/B harness in a
scheduled change window ([`CANDIDATE-B.md`](CANDIDATE-B.md),
`scripts/cluster-ab.py`; the secondary tests — the partial-prefill and
batched-token knobs, then Track A `--moe-backend flashinfer_b12x` fifth — run
one variable at a time after it). This section is the switch itself; the
verdict is the soak's.

**Executed 2026-09-12 08:39Z** (`.runtime/logs/techsara-up-candidateB-20260912T0839Z.log`):
`MAIN_MODEL_IMAGE=vllm/vllm-openai@sha256:819ec9c0…` (image ID `5a0f8b91…`
on both nodes, `cluster-sync` 13 pass), `CLUSTER_GDN_PREFILL_BACKEND=flashinfer`,
`VLLM_USE_V2_MODEL_RUNNER=0` and `VLLM_ALLREDUCE_USE_FLASHINFER=0` in
`.runtime/engine.env` (2 variables, sha256-matched on the worker), the
kernel-cache volume created on the worker, one pair reload: worker 08:39:29Z,
head 08:39:59Z, `Application startup complete` 08:43:45Z (≈ 3 m 46 s; the
launcher counted `ready after 229s`), the GDN line on both ranks at 08:41:04Z.
Step 4 passed (verify probe 23/2, the 2 = the Xid baseline). The A/B matrix
is in `ab/compare-A-vs-B-20260912.md`; the 120-min soak started 10:50Z and is
in progress at the time of writing (result appended by the lead); the 48–72 h
canary is **still unproven**; the secondary tests are not run. Production
runs B now; the pinned digest `24f2f897…` is the rollback (step 5), cached on
both nodes.

1. **Before.** READY, `incident: null`, lock free (§1, §9), the 120-min soak
   baseline for the pinned build recorded, and the rollback insurance in
   place: the pinned digest is untagged upstream and can be garbage-collected,
   so `docker save` it on **both** nodes first (research report §6.2).
2. **Two keys, one source.** In `.env`: `MAIN_MODEL_IMAGE=vllm/vllm-openai@sha256:819ec9c0…`
   (a `repository@sha256:<64 hex>` reference only — the launcher refuses a
   tag, which would move under a deploy; it selects the image for the `vllm`
   head service and, through `scripts/cluster-sync.sh` reading the head's
   rendered image, the `vllm-worker` service — `vllm-router` and `vllm-ocr`
   stay on the proven digest so the trial has one variable) and
   `CLUSTER_GDN_PREFILL_BACKEND=flashinfer` (one of `flashinfer`, `triton`,
   `cutedsl`, or empty to leave the flag out;
   rendered into `CLUSTER_ENGINE_ARGS` as `--gdn-prefill-backend flashinfer`,
   byte-identical on both ranks through `.runtime/generated.env` and
   `worker.env`). `VLLM_ALLREDUCE_USE_FLASHINFER=0` on both ranks stays
   (research §6.1). Nothing else changes.
3. **Ship and reload the pair under the lock.** `scripts/cluster-sync.sh
   --image-only` (pulls or `docker save | load`s the image to the worker) then
   `scripts/cluster-up.sh` — a pair reload, ≈ 10–15 min end to end, during
   which requests queue (§10). Clear the kernel caches on the first start of a
   new build (§13: `scripts/cluster-recover.sh --force --clear-kernel-cache`
   in place of the plain `up` when the pair is already down under the lock).
4. **The both-rank grep.** Nothing counts until both ranks are proven on the
   same build with the same argument:

   ```bash
   scripts/cluster-verify-engine.sh --probe                 # running argv of BOTH ranks diffed; generated.env vs running head; both GPUs
   docker inspect sf-local-ai-vllm-1 --format '{{.Image}} {{.Config.Image}}'
   ( . scripts/lib/cluster-common.sh; cluster_load_settings
     ssh_worker "docker inspect sf-local-ai-worker-vllm-worker-1 --format '{{.Image}} {{.Config.Image}}'" )
   docker inspect sf-local-ai-vllm-1 | jq -r '.[0].Args[]' | grep -A1 -- '--gdn-prefill-backend'
   docker logs sf-local-ai-vllm-1 2>&1 | grep -m1 'Using FlashInfer GDN prefill kernel'     # the engine banner, not the launch script
   scripts/cluster-logs.sh worker --tail 400 | grep -m1 'Using FlashInfer GDN prefill kernel'
   ```

   Both image ids identical, both argv identical, the banner on both ranks,
   `--probe` PASS with both GPUs ≥ 30 %, Xid count on both kernels recorded
   and not growing. Then the soak (`scripts/cluster-soak.py`, 120 min, the
   research report's §6.4 criteria: needles 3/3, 262K prefill ≤ 1.3 × 78.5 s,
   bench within −10 %) and ≥ 7 clean days before the class is called closed.
5. **Rollback** (the trial fails, or anything looks wrong): restore the two
   keys (`MAIN_MODEL_IMAGE` back to the pinned digest or unset, `CLUSTER_GDN_PREFILL_BACKEND`
   unset), `scripts/cluster-sync.sh --image-only` (no pull needed while the
   image is cached), `scripts/cluster-recover.sh --force --clear-kernel-cache`
   or `scripts/cluster-up.sh` under the lock, then step 4 again against the
   pinned digest `sha256:24f2f897…`. An engine rollback is a pair reload too;
   requests queue meanwhile.

`nightly` by tag is never a candidate: the tag moves, and `docker
image inspect` of the cached `nightly` on both nodes shows a build with none
of the fixes (`sha256:7d5128a9…`).

## 15. CPU topology

Each Spark has 20 Arm cores in one NUMA node (10 × Cortex-X925 + 10 ×
Cortex-A725; `lscpu`). The engine's CPU cost at idle is NCCL's proxy and
busy-poll threads (head vLLM container ≈ 307 %, worker ≈ 165 % of one core;
`docs/ISSUE/gdn-spec-decode-remediation-2026-09-11.md`); everything else the
nodes do — OCR preprocessing and the ASR decode on Spark 2, ffmpeg, the sync
worker, embedding batches and crawling on Spark 1, the second tenant's
pipeline on Spark 2 — competes for the remaining cores by placement, not by
pinning.

```bash
scripts/cluster-cpu.sh            # read-only: the core layout of both nodes and what each engine process is running on (per-process measurement)
scripts/cluster-cpu.sh --help     # its options
top -H -p "$(docker inspect -f '{{.State.Pid}}' sf-local-ai-vllm-1)" -n1 -b | head -25      # the head's busiest threads
cat /proc/pressure/cpu; ( . scripts/lib/cluster-common.sh; cluster_load_settings; ssh_worker "cat /proc/pressure/cpu; uptime" )
```

What to look for: a rank whose NCCL threads are starved (CPU pressure `some`
climbing on a node while `VllmCanaryTtftHigh` fires without memory pressure
— §7 rules memory out first), or a background job that grew (the sync worker
at 55 % average was already "worth a look" in the remediation record). The
measured numbers so far do not warrant an affinity change
(`kernel.numa_balancing=0`, single NUMA node, no HugePages); measure with the
script before pinning anything, and pin nothing on one node only — a
lopsided pair shows up as the slower rank holding every all-reduce.

## 16. Rollback

The controller and sentinel are two compose services and two Python files;
the orchestrator changes are in its image. Three levels, least first:

1. **Observe only, no reload.** The controller keeps publishing `/state` and
   `/metrics` and logs the restart it *would* perform instead of doing it when
   `DRY_RUN=1` (`monitoring/engine-controller/README.md`). The knob is not in
   the compose environment today: add `DRY_RUN: "1"` under `engine-controller:
   environment:` in `compose/compose.dgx-spark.yaml` and run
   `TECHSARA_PRESERVE_MAIN_MODEL=1 scripts/cluster-up.sh`. The preserve flag is
   what keeps a running, answering head untouched even though its own
   definition changed in this merge (`ulimits`, the cache volumes); the
   controller is stateless and recreated on every `up`; the engine lock is
   held for the duration. The sentinel has the same knob: add it under
   `vllm-worker-sentinel: environment:` in `compose/compose.cluster-worker.yaml`,
   ship the file with `scripts/cluster-sync.sh --env-only`, then recreate
   **only** the sentinel with `scripts/cluster-worker.sh start
   vllm-worker-sentinel` — a bare `start` is `up -d` for the whole worker
   project and also recreates `vllm-worker` when its definition differs from
   the running container, which is a pair reload (see 3).
2. **Stop the guards.** `docker stop sf-local-ai-engine-controller-1` (the
   Docker healthchecks remain the last resort — their kill tier is the only
   thing that acts now, after 4 min of misses; the state series go absent →
   `MONITORING_UNKNOWN`; the orchestrator's breaker then opens only on
   observed failures and the queue still holds). On the worker: `docker stop
   sf-local-ai-worker-vllm-worker-sentinel-1` (nothing then reports the rank
   process; the sentinel restarts nothing on its own anyway).
   `TECHSARA_PRESERVE_MAIN_MODEL=1 scripts/cluster-up.sh` starts the
   controller again; `scripts/cluster-worker.sh start vllm-worker-sentinel`
   the sentinel.
3. **Revert the code.** `scripts/deploy.sh --ref <commit before the merge>`
   (routine, no `--full`) puts the previous compose files and orchestrator
   image back without touching the running pair
   (`TECHSARA_PRESERVE_MAIN_MODEL=1`). The reverted compose files do not know
   the guards, so `deploy.sh`'s apply removes them for a target ref that lacks
   them: the `engine-controller` container on the head and the
   `vllm-worker-sentinel` on the worker (round-1 major; the automatic rollback
   has no hand to do it). Check after any rollback, automatic or not:
   `docker ps --filter name=engine-controller` must be empty and
   `( . scripts/lib/cluster-common.sh; cluster_load_settings; ssh_worker docker ps --filter name=sentinel )`
   too; if either is still there, `docker rm -f sf-local-ai-engine-controller-1`
   and `ssh_worker docker rm -f sf-local-ai-worker-vllm-worker-sentinel-1` by
   hand — an orphaned controller keeps the Docker socket and its own restart
   rules beside the restored `vllm-watchdog`. Do **not** run
   `scripts/cluster-worker.sh start` (= `docker compose up -d`) if the worker
   container was created from the new file: its `ulimits: core: 1` and the
   cache volume are definition changes and `up -d` would recreate the worker,
   which is a pair reload. The old `vllm-watchdog` service returns with the
   old `compose.dgx-spark.yaml` on the next `up`. `.runtime/locks/`,
   `.runtime/incidents/` and the kernel-cache volumes are gitignored / named
   volumes and harmless to leave. The orchestrator's `queued` rows survive a
   rollback of its image only as `interrupted` rows the old code resumes on
   attach — run the exactly-once checks (§10) afterwards.

`ulimits: core: 1` and the kernel-cache volumes on the vLLM containers are
applied when a container is next **created** (the first `deploy.sh --full`
after this merge), not by `docker restart`; `docker inspect <ctr> --format
'{{json .HostConfig.Ulimits}} {{json .Mounts}}'` shows whether they are in
force. Reverting them needs the same: a `--full` deploy.

## 17. Alerts, one section per anchor

Every `Vllm*` alert carries labels `service`, `state` (fixed per alert — the
state it speaks for; the live state name is an annotation, never a label, so a
transition cannot reset the `for:` clock) and annotations `since`, `reason`,
`runbook` (one of these anchors) and `safe_command` — the read-only command
quoted under each heading (`monitoring/prometheus/rules/alerts.yml`, group
`vllm-availability`). "Page a human" = a person has to read this; there is no
receiver.

### <a id="wedged"></a>VllmEngineWedged (critical)

- **Fired:** controller state `WEDGED` (5) for 30 s: token counters frozen
  ≥ 90 s with requests running, or the canary outstanding ≥ 60 s while
  `/health` answers 200, two consecutive observations.
- **Means:** the API process is alive and lying; the engine completes nothing.
  Every in-flight request fails when vLLM's own 300 s execute timeout fires.
  This is the 2026-09-11 22:16–22:20 shape. New requests queue (§10).
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.state,.reason,.incident,.signals.engine,.recovery'`
- **Recover:** the controller is doing it (`.recovery.in_progress`). If
  `.reason` says it cannot (lock held by another actor, cooldown, budget):
  §9, `scripts/cluster-recover.sh` (or `--force` when it stood down).
- **Page:** if WEDGED persists > 5 min with `in_progress: false`, or the
  category is `budget_exhausted`.
- **Known false positive (2026-09-12 09:04–09:16Z,
  `INCIDENT-2026-09-11-vllm.md` §7.2):** one ~950K-token prompt prefilling
  alone. Neither token counter moves during a single long chunked prefill
  on this build, the canary queues behind it, and the rule confirms a wedge;
  the restart was refused only because the budget was spent. Before acting
  on WEDGED with `.signals.engine.requests_running == 1`, read
  `curl -s http://127.0.0.1:8000/metrics | grep -E '^vllm:(num_requests_running|prompt_tokens_total|kv_cache_usage_perc)'`
  twice, 30 s apart: a rising `kv_cache_usage_perc` with one request running
  is a prefill in progress, not a wedge (Prometheus, 09:03–09:15Z:
  `prompt_tokens_total` flat at 694,469 while `kv_cache_usage_perc` rose
  0.088 → 0.557; the prompt is counted only when its prefill finishes) — do
  not `--force`; wait for it
  (≈ 13 min at 950K) or cancel the request at its client. Owner of the fix:
  the controller workstream.

### <a id="primary-down"></a>VllmPrimaryDown (critical)

- **Fired:** controller state `DOWN` (8) for 1 m: the primary is not serving
  and the restart budget is exhausted or the last recovery failed.
- **Means:** **nobody is being answered.** Accepted requests are held in the
  queue (§10) and will resume when READY returns; nothing answers them
  meanwhile, by design. Either the budget is exhausted
  (`VllmRecoveryBudgetExhausted` fires too) or a recovery attempt failed
  (`cold_start_timeout`, a head restart the daemon refused) and the cooldown
  is running.
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.state,.reason,.incident,.recovery'`;
  `curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(queued_generations|breaker_state)'`;
  `ls -t .runtime/incidents | head -3`.
- **Recover:** §9 (`scripts/cluster-recover.sh`, `--force` after a
  stand-down); §13 if the last attempt's head log carries a compile-error
  signature; §6 / §7 if a rank dies on load.
- **Page:** always — this is the user-visible outage.

### <a id="ready-unproven"></a>VllmReadyUnproven (critical)

- **Fired:** the controller claims READY/BUSY (2/3) but
  `cluster:vllm_last_success_age_seconds > 120` for 1 m — four canary
  intervals without a proven completion.
- **Means:** the controller's snapshot is stale or the controller is wrong: a
  tick that stopped refreshing (an exception swallowed every tick), a canary
  that is never harvested. Treat the state as **unknown**, not as READY; the
  engine may be fine or may be the 2026-09-11 shape with the watcher asleep.
- **Read:** `curl -s http://127.0.0.1:9838/metrics | grep -E '^techsara_vllm_(generated_at_seconds|last_success_timestamp_seconds|synthetic_success)'`;
  `docker logs --tail 50 sf-local-ai-engine-controller-1`; then §2 by hand
  for the truth about inference.
- **Recover:** the controller, not the engine: `TECHSARA_PRESERVE_MAIN_MODEL=1
  scripts/cluster-up.sh` recreates it under the lock (or `docker restart
  sf-local-ai-engine-controller-1` — it is stateless). If the hand probe of §2
  also fails, §9.
- **Page:** yes, within minutes: a lying READY is the one state this
  programme exists to make impossible.

### <a id="recovery-budget-exhausted"></a>VllmRecoveryBudgetExhausted (critical)

- **Fired:** `techsara_vllm_last_failure_category{category="budget_exhausted"} == 1`
  while state is DOWN.
- **Means:** 3 recoveries inside 3600 s (manual ones count) and the engine is
  still dead. The controller has stopped restarting on purpose; something
  fails faster than a reload fixes it. Evidence for every attempt is under
  `.runtime/incidents/<id>/`. Requests stay queued; the controller keeps
  probing and returns to READY by itself if the readiness sequence passes.
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.recovery'; ls -t .runtime/incidents | head -3`;
  §5 for the files, §6 for Xid, §7 for memory, §13 for a compile signature.
- **Recover:** a human decides. `scripts/cluster-recover.sh --force` after
  reading the attempts (a rank dying on load ≠ a rank dying under load;
  `--clear-kernel-cache` when the log says compile); or wait for the window
  to pass (the controller resumes on its own).
- **Page:** always.

### <a id="worker-rank-absent"></a>VllmWorkerRankAbsent (critical)

- **Fired:** `techsara_vllm_worker_rank_alive == 0` for 1 m while the sentinel
  is reachable and the state is not STARTING/RECOVERING.
- **Means:** no `VLLM::Worker*` process in the worker container and no
  recovery in progress. The head hangs on its next collective; WEDGED follows
  within 90 s. If the controller has budget it acts within one observation;
  if this persists, the budget is gone — or the sentinel's `POST /restart` is
  being refused (a token mismatch between the nodes shows in the controller
  log as a refused restart, and in `/state .recovery.last`).
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.signals.worker,.recovery'`;
  `scripts/cluster-logs.sh worker --tail 200`; §6 on the worker.
- **Recover:** §9. A refused sentinel = re-run `scripts/cluster-sync.sh
  --env-only` and recreate the controller so both nodes carry the one token.
- **Page:** if it persists > 2 min.

### <a id="generation-frozen"></a>VllmGenerationFrozen (critical)

- **Fired:** `techsara_vllm_generation_frozen_seconds > 90` with
  `cluster:vllm_requests_running:current > 0`, 30 s.
- **Means:** requests are scheduled and neither `generation_tokens_total` nor
  `prompt_tokens_total` moved for 90 s — a dead rank with the head blocked in a
  collective, `/health` green (signal 12).
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.signals.engine,.signals.canary'`
- **Recover:** the controller confirms it as `wedged_frozen_tokens` on two
  observations; otherwise §9.
- **Page:** with `VllmEngineWedged`.

### <a id="repeated-recovery-failure"></a>VllmRepeatedRecoveryFailure (critical)

- **Fired:** `increase(techsara_vllm_recovery_attempts_total{outcome="failed"}[1h]) >= 2`.
- **Means:** the restart is not the fix. Each failed attempt has its
  `head-logs-<n>.txt`, `worker-diagnostics-<n>.txt`, `state-<n>.json`.
- **Read:** `ls -t .runtime/incidents | head -5; curl -s http://127.0.0.1:9838/state | jq '.recovery'`
- **Recover:** read before a third attempt; §6 (Xid on both nodes), §7
  (`NV_ERR_NO_MEMORY` on the head during a load), §13 (a compile signature),
  `scripts/cluster-doctor.sh`.
- **Page:** always.

### <a id="monitoring-unknown"></a>VllmMonitoringUnknown (warning)

- **Fired:** one of five branches, 1 m, each labelled with what is missing
  so two can fire side by side: `absent_over_time(techsara_vllm_state_code[2m])`
  (`missing="controller_state"`), `absent_over_time(up{job="vllm-main"}[2m])`
  (`missing="vllm_scrape_target"`), the controller present and reporting
  code 0 itself (`controller_observation`), the controller's own snapshot
  stamp `techsara_vllm_generated_at_seconds` more than 30 s old — its tick
  stopped and `/metrics` serves the last verdict with fresh scrape times
  (`controller_stale`), or a state exported with no stamp at all
  (`controller_stamp`, an older build). The derived state drops its sample at
  the same 30 s, so the tile and the alert agree.
- **Means:** monitoring cannot see. Inference may be fine. It is the **only**
  alert that fires on absence and it is worded that way on purpose.
- **Read:** `curl -s http://127.0.0.1:9838/healthz; docker ps --filter name=engine-controller; curl -s http://127.0.0.1:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job=="engine-controller" or .labels.job=="vllm-main") | {job:.labels.job,health,lastError}'`;
  `curl -s http://127.0.0.1:9838/state | jq '.state,.generated_at,.reason'`
  when the controller answers (code 0 names what it cannot observe: the
  Docker socket, the head TCP connect; a `generated_at` that stops moving is
  the stale branch); then §2 for the truth about inference.
- **Recover:** the telemetry, not the engine: `./techsara up` recreates the
  controller; `./scripts/monitoring.sh restart` for Prometheus. Never a pair
  restart on this alert.
- **Page:** no. Fix within the day.

### <a id="metrics-stale"></a>VllmMetricsStale (warning)

- **Fired:** the engine completes canaries (`techsara_vllm_synthetic_success == 1`)
  but the head's `/metrics` is not being read: `cluster:vllm_metrics_age_seconds
  > 60`, or the controller's own `techsara_vllm_head_metrics_ok == 0`, 1 m.
- **Means:** Prometheus has no fresh sample of `vllm:generation_tokens_total`:
  the `vllm-main` scrape (timeouts, a hung `/metrics` handler) or Prometheus
  itself. Dashboards are stale; the engine is not. (A scrape that fails
  outright shows as `VllmScrapeTargetDown`, not here.)
- **Read:** `curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' http://127.0.0.1:8000/metrics; ./scripts/monitoring.sh status`
- **Recover:** monitoring side only.
- **Page:** no.

### <a id="requests-queued"></a>VllmRequestsQueued (warning)

- **Fired:** `llm_queued_generations > 0` for 2 m.
- **Means:** people are waiting with the line `Main model is recovering—your
  request is safely queued.` while the primary is not READY/BUSY (state 7,
  `QUEUEING`). Nothing answers them until the readiness sequence passes; the
  same generations then resume. Two minutes is normal for a reload; the
  reload itself is ≈ 4–6 min.
- **Read:** `curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(queued_generations|queue_wait_seconds_count|engine_state_code)'`;
  `curl -s http://127.0.0.1:9838/state | jq '.state,.reason,.recovery.step'`.
- **Recover:** whatever the primary's state says (§1); nothing on the queue.
  If the primary is READY and the gauge stays > 0 for more than a minute the
  resume sweep did not run: `docker logs --tail 100 sf-local-ai-orchestrator-1
  | grep -E 'resume|continuity'`, then §10's SQL.
- **Page:** no, unless it lasts longer than a reload (> 8 min): then the
  primary is the problem, see its state (`VllmPrimaryDown` will say so).

### <a id="queue-backlog"></a>VllmQueueBacklog (warning)

- **Fired:** `cluster:vllm_requests_waiting:current > 5` for 5 m.
- **Means:** vLLM's own scheduler is queueing (KV pressure, a burst, the second
  tenant at concurrency 10 — the load shape that triggers the GDN fault).
  Not an outage; TTFT climbs (`VllmCanaryTtftHigh` follows). Distinct from
  `VllmRequestsQueued`, which is the orchestrator's queue for a primary that
  is not READY.
- **Read:** `scripts/cluster-status.sh` ("Metrics:" line: running, waiting,
  KV usage); `curl -s http://127.0.0.1:8000/metrics | grep -E '^vllm:(num_requests_(running|waiting)|kv_cache_usage_perc)'`;
  `curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_admission_'` (the
  orchestrator's lanes: how many are held before vLLM sees them).
- **Recover:** none. Capacity is `MEMORY-BUDGET.md`'s table (10 concurrent
  mixed is the tested maximum, and the NORMAL lane's limit).
- **Page:** no.

### <a id="canary-ttft-high"></a>VllmCanaryTtftHigh (warning)

- **Fired:** `techsara_vllm_synthetic_ttft_seconds > 10` on succeeding
  canaries for 5 m (healthy: ≈ 0.4 s at c=1).
- **Means:** the engine answers, slowly: queueing, KV pressure, head swap
  activity, or a LONG-lane prompt holding the engine (§10). The controller
  shows DEGRADED.
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.signals.canary,.signals.engine'`; §7; §15.
- **Recover:** none by restart. Reduce the load or the memory pressure.
- **Page:** no.

### <a id="head-swap-activity"></a>HeadSwapActivity (warning)

- **Fired:** `rate(node_vmstat_pswpout{node="spark-1"}[5m]) > 500` for 5 m
  (500 pages/s = 2 MB/s). Expected during a cold start (bursts of 500–10,000
  pages/s for 2–22 min lined up with the 07:05Z and 22:15Z reloads on
  2026-09-11), so the rule is silenced while the controller reports
  STARTING/RECOVERING — a 25 GiB rank being loaded evicts whatever else is
  resident by design — and fires only *without* a reload, which is the only
  time it matters. With no controller at all it still fires.
- **Means:** the LLM's working set — weights and KV are host memory on GB10 —
  is being evicted; TTFT climbs, then the engine stalls.
- **Read:** `free -g; docker stats --no-stream | sort -k4 -h | tail -8`; §7.
- **Recover:** stop what grew (developer tooling, `pg-test`, a stray container;
  `MEMORY-BUDGET.md` lists the residents). Never a pair restart for this.
- **Page:** no; if it coincides with `VllmCanaryTtftHigh` for > 15 min, yes.

### <a id="nccl-tcp-fallback"></a>NcclTcpFallback (warning, inferred)

- **Fired:** RoCE netdev tx > 10 MB/s while IB port tx < 1 MB/s on the same
  node, with real requests running, 3 m. **Not reported by NCCL** — inferred
  from the signature `../MONITORING.md` measured ("read the IB counters,
  never netdev"). Known false positive: a multi-minute rsync over the fabric
  address (a `scripts/cluster-sync.sh` model or image copy).
- **Means:** NCCL gave up on the IB transport and is on sockets; decode falls
  several times below the 100 tok/s baseline.
- **Read:** `scripts/cluster-status.sh; docker logs sf-local-ai-vllm-1 2>&1 | grep -m3 'NCCL INFO NET/'` —
  `NET/IB` at group init is RDMA, `NET/Socket` is the socket transport; §8.
- **Recover:** find why (`scripts/cluster-doctor.sh`: `/dev/infiniband`, HCA
  state, memlock), then a pair restart under the lock (§9) picks the transport
  again at init. Not observable until the next init otherwise.
- **Page:** no; fix in a window.

### <a id="roce-rail-missing"></a>RoceRailMissing (warning, best effort)

- **Fired:** one rail at exactly 0 B/s for 3 m while the other rail on the
  same node moves > 1 MB/s. Both rails carry every TP=2 collective (≈ 81/19,
  never 100/0; zero such minutes in 7 days of history).
- **Means:** NCCL is on one rail. The engine still works (one rail is
  PCIe-bound near 109 Gb/s); the redundancy is gone.
- **Read:** `scripts/cluster-status.sh; cat /sys/class/infiniband/{rocep1s0f1,roceP2p1s0f1}/ports/1/state`;
  `RoceLinkDown` says whether the port is down; else `NCCL_IB_HCA` and the
  head's `NET/IB` init lines.
- **Recover:** cable/port first; a pair restart (§9) re-selects rails at init.
- **Page:** no.

### <a id="scrape-target-down"></a>VllmScrapeTargetDown (warning; was `VllmDown`, critical)

- **Fired:** `up{job="vllm-main"} == 0` for 1 m, unless the controller reports
  STARTING (1) or RECOVERING (6).
- **Means:** exactly what it measures — the head's `/metrics` is not answering
  Prometheus and the controller is not restarting it. On 2026-09-11 the old
  `VllmDown` read 1 for the first five minutes of the outage and 0 for the five
  the reload was designed to take; neither was "the model is down". With no
  controller at all it still fires.
- **Read:** `curl -s http://127.0.0.1:9838/state | jq '.state,.reason,.signals.api'`
- **Recover:** read the state (§1) before assuming an outage; if `.signals.api.tcp`
  is false on a proven engine the controller confirms `head_api_dead` on two
  observations ≥ 10 s apart and recovers. Else §9.
- **Page:** only through the critical alerts it accompanies.

### <a id="recovery-timeline"></a>Recovery timeline — VllmRecoveryStarted, VllmRecoveredRestart, VllmPrimaryRestored, VllmQueueDrained (info)

Notices, not pages: they tell the story of one recovery in order.

- `VllmRecoveryStarted`: `techsara_vllm_recovery_in_progress == 1`. The
  controller holds the lock and walks capture → stop_pair → wait_load →
  canary (the readiness sequence) → verify; measured 3.5–5.5 min.
  `curl -s http://127.0.0.1:9838/state | jq '.recovery,.incident'`
- `VllmRecoveredRestart`: a recovery's readiness sequence passed within the
  last 30 m. Read the evidence while it is fresh: `ls -t .runtime/incidents |
  head -1` and the `state-<n>.json` / `head-logs-<n>.txt` /
  `worker-diagnostics-<n>.txt` in it (§5).
- `VllmPrimaryRestored`: READY/BUSY for 30 s after being ≥ WEDGED within 15 m.
  `curl -s http://127.0.0.1:9838/state | jq '.state,.since,.signals.canary'`
- `VllmQueueDrained`: `llm_queued_generations` back to 0 after being > 0
  within 15 m — the sweep resumed the last waiting generation.
  `curl -s http://127.0.0.1:8080/metrics | grep -E '^llm_(queued_generations|resumed_generations_total)'`.
  Every message answered after the wait carries `attempt: 2` and
  `retry_reason: recovery` in its usage row, and the primary as its engine.

After the four have fired in order: §11's confirmation, §10's exactly-once
checks, then write the incident's row into `SLO.md` §5 (detect → RECOVERING,
→ READY, both clocks, how many generations queued and resumed).

### <a id="rule-evaluation-failing"></a>PrometheusRuleEvaluationFailing (warning, `service=monitoring`)

- **Fired:** `increase(prometheus_rule_evaluation_failures_total[10m]) > 0`
  for 5 m.
- **Means:** a rule in the named group errors at evaluation time (a
  many-to-many join, a duplicate series in the lookback after a Prometheus
  restart, a typo after an edit). Every alert that group would raise is
  **silent** and every recording rule it owns has a gap — including
  `cluster:vllm_service_state:code`, so the engine tile can read
  `MONITORING_UNKNOWN` while the controller is fine. This is why the
  availability rules keep the live state name out of alert labels and join
  only on series from one scrape (`ARCHITECTURE.md` §6).
- **Read:** `curl -s http://127.0.0.1:9090/api/v1/rules | jq '.data.groups[] | select(.rules[]?.lastError != "") | {name, file, errors: [.rules[] | select(.lastError != "") | {name, lastError}]}'`;
  Prometheus `/rules` shows `lastError` per rule.
- **Recover:** fix the rule (`promtool check rules` and `promtool test rules`
  on `monitoring/prometheus/tests/` before the edit lands — the commands are
  in `../MONITORING.md`, "The rules, and how they are proven"), then
  `./scripts/monitoring.sh restart` (`prometheus.yml` is a single-file bind
  mount; `/-/reload` after a checkout reads the old inode). Never a pair
  restart for this.
- **Page:** no; fix within the day — while it fires, the `Vllm*` alerts of
  that group cannot be trusted to fire.

### <a id="config-drift"></a>VllmConfigDrift — not defined

There is no drift signal in Prometheus: `vllm:cache_config_info` carries the
live cache configuration as labels but nothing exports the *intended* one to
compare against, and a change after a deliberate deploy is not drift. The
drift check is:

```bash
scripts/cluster-verify-engine.sh          # running argv of BOTH ranks vs each other, and vs generated.env
```

Run it after every deploy, after every recovery, and as step 4 of a candidate
switch (§14). "generated.env carries arguments the running head does not (a
pending engine change?)" is the expected FAIL between a routine deploy that
changed an engine argument and the `--full` deploy that applies it (§19).
`REVIEW-MANIFEST.md` §3 is the 2026-09-11 drift check: none.

## 18. Boot and host recovery

What the repository does, checked against the compose files, the launcher
(`launcher/techsara_cli/cli.py`, `_start_compose`) and the head itself on
2026-09-12.

**Does Docker start on boot?** Yes, on both. Head: `systemctl is-enabled docker`
→ `enabled` (read on spark-0e68, 2026-09-12; `is-active` → `active`). Worker:
`enabled` / `active` too (spark-476e, read the same day over the repository's
own ssh helper:
`( . scripts/lib/cluster-common.sh; cluster_load_settings; ssh_worker "systemctl is-enabled docker; systemctl is-active docker" )`).

**Do the containers come back on their own?** Every service in `compose.yaml`,
`compose/compose.dgx-spark.yaml` (head `vllm`, `vllm-router`, `vllm-embed`,
`vllm-reranker`, `vllm-ocr`, `engine-controller`), `compose/compose.cluster-worker.yaml`
(`vllm-worker`, `vllm-worker-sentinel`), `compose.monitoring*.yaml`,
`compose.ocr.yaml`, `compose.whisper.yaml` and `compose.cloudflare.yaml` is
`restart: unless-stopped`. After a reboot Docker restarts every container that
was **running** when the host went down. It does **not** restart a container
that was stopped by hand before the reboot (`docker stop`, `./techsara down`,
`scripts/cluster-worker.sh stop|down`, `scripts/monitoring.sh stop|down`,
`scripts/ocr.sh stop|down`): `unless-stopped` remembers a manual stop.

**What the pair does after a reboot, by itself.**

- Head rebooted, worker up: the head's `vllm` container starts and waits at the
  rendezvous (`--distributed-timeout-seconds 300` per attempt, `restart:
  unless-stopped` if it exits). The old worker is in a dead process group. The
  controller comes back with the head, sees the worker's rank process (still
  `VLLM::Worker_TP1` of the old group, or gone) against a head `started_at`
  newer than the worker's, and restarts the worker through the sentinel's
  `POST /restart` as part of one coordinated recovery — the worker then joins
  the new head. The worker's own healthcheck kill tier is the last resort if
  the controller is not there (after `VLLM_HEALTHCHECK_KILL_AFTER` misses once
  the process is older than 15 min). Budget: one of three.
- Worker rebooted, head up: the head hangs on its next collective until vLLM's
  300 s timeout **unless** the controller acts first — it does: the sentinel is
  unreachable (DEGRADED), then reachable with a `started_at` newer than the
  head's → `worker_rank_dead` → coordinated recovery (worker restart, head
  restart, readiness sequence). Budget: one of three. Requests queue meanwhile.
- Both rebooted: both containers start, rendezvous, load (from the kernel-cache
  volumes, so no recompile); the controller starts with them (its lock file
  and incident directory are host files under `.runtime/`, created by the
  launcher as the operator so both sides can open them) and reports `STARTING`
  until the readiness sequence passes. If the worker is slow the head may
  cycle through 300 s rendezvous timeouts; after 900 s under the controller's
  observation without a completion the state is `DOWN` (`cold_start_timeout`)
  — **not a restart trigger**, the healthchecks are the last resort — and it
  returns to READY on its own when the sequence passes. A worker fault
  *during* that load is a trigger (the sentinel's `last_fault` newer than the
  head's start), so a rank that dies on load is retried under budget rather
  than waited out for 15 min.
- The orchestrator comes back and runs the resume sweep at start-up: every
  `queued` / `interrupted` row with a resumable snapshot resumes once the
  controller reports READY. Nothing is lost by the reboot.
- The monitoring stack, the OCR engine on the worker and the tunnel come back
  the same way, if they were running.

**What a human must run after a reboot, and why.**

1. `scripts/cluster-status.sh` — is everything back? `curl -s
   http://127.0.0.1:9838/state | jq .state` — READY? `./scripts/monitoring.sh
   status` — all targets UP? `curl -s http://127.0.0.1:8080/metrics | grep
   '^llm_queued_generations'` — 0?
2. If a container is missing because it had been stopped by hand, or after a
   `./techsara down`: `scripts/cluster-up.sh` (= `./techsara up` under the
   engine lock, then the status report; ordering below). If only the worker
   is missing: `scripts/cluster-worker.sh start` (needs
   `~/.techsara-cluster/worker.env` and the compose file on the worker;
   `scripts/cluster-sync.sh` ships them and `up` runs it). If only monitoring:
   `./scripts/monitoring.sh up`. If OCR on the worker: `scripts/ocr.sh up`.
3. `scripts/cluster-verify-engine.sh --probe` once READY — the drift check and
   both-GPU proof. Record the Xid count on both kernels (a reboot resets it).
   Then §10's exactly-once checks if anything was queued.

**What `./techsara up` does, in order** (`_start_compose`): build the three
application images → `postgres` (+ `searxng`) → `vllm-embed` → `vllm-reranker`
→ `vllm-router` → `vllm-ocr` (unless remote) → *main model*, under the engine
lock: with `TECHSARA_PRESERVE_MAIN_MODEL` set and the engine answering, probe
and leave it alone — but still ship the worker's sentinel files and token
(`scripts/cluster-sync.sh --env-only`) and start `vllm-worker-sentinel`
(never `vllm-worker`) so a routine deploy cannot leave the controller without
its sensor; otherwise in dual mode `scripts/cluster-sync.sh` (image, model,
worker env + compose + `sentinel.py`/`common.py`, the sentinel token) →
`scripts/cluster-worker.sh start` → `vllm` head (`wait_service` up to 2400 s)
→ a real probe → **then** `engine-controller` (never before the head is
proven, so its 900 s cold-start budget cannot race a first load the launcher
allows 2400 s for; created with the token the worker was shipped) → the
legacy `vllm-watchdog` container removed → `orchestrator` → `sync-worker` →
`frontend` (→ `pgadmin`).

**What is NOT automatic.**

- A manually stopped container (above). `techsara down` in dual mode also
  stops the worker; both stay down until `up`.
- Re-shipping the worker's model or image (`~/.techsara-cluster/`): only
  `scripts/cluster-sync.sh` does it, and only `up` (without the preserve
  flag) or a person runs it. A reboot does not need it; a checkout change
  that touches `compose.cluster-worker.yaml`, `sentinel.py` or `common.py`
  is shipped by any `up` (`--env-only`), but the *worker engine* is recreated
  only by a `--full` deploy or a plain `up`.
- `.runtime/generated.env` is not regenerated by a reboot; it is by `up`.
- The engine lock: an `flock` dies with the process, so a reboot never leaves
  it held; `.runtime/locks/engine-recovery.holder` may show stale metadata
  from the last holder — `engine_lock_is_held` reads the flock, not the file.
- The verification (`--probe`) and the Xid baseline. Nothing runs them.
- Nothing pages anyone at any point.
- Not answered here: the unrelated older `sf-local-ai` project on Node 2
  (`../CLUSTER.md`, limitation 4), which competes for its memory if it comes
  back too.

## 19. Deployment without avoidable outage

The rule (`docs/CLUSTER.md`, "Changing an engine argument"): **only a `--full`
deploy or a plain `./techsara up` without the preserve flag touches the
model.** `scripts/deploy.sh` without `--full` runs `./techsara up` with
`TECHSARA_PRESERVE_MAIN_MODEL=1`: a running, answering engine is probed and left
exactly as it is, even if its compose definition changed (`cli.py`,
`_start_compose`). What forces a reload of the **pair**: an engine argument
(`config/model-manifest.yaml` `startup_arguments`, any `.env` key that renders
into `CLUSTER_ENGINE_ARGS` — `MAIN_MODEL_MAX_LEN`, `CLUSTER_KV_CACHE_MEMORY_GIB`,
`CLUSTER_SPECULATIVE_CONFIG`, `MAIN_MODEL_ENABLE_PREFIX_CACHING`,
`CLUSTER_MAX_NUM_BATCHED_TOKENS`, `CLUSTER_GDN_PREFILL_BACKEND`, …), the image
(`MAIN_MODEL_IMAGE`, or the digest in `compose/compose.dgx-spark.yaml`), or any
other change to the `vllm` / `vllm-worker` service definitions (`ulimits`,
volumes, healthcheck, environment). vLLM's mp executor requires byte-identical
configuration on both ranks, so there is no rolling variant: the reload is the
whole pair, 3 m 32 s warm to 5 m 20 s cold plus start-up, ≈ 10–15 min end to
end (`deploy.sh` says so). **Every request that arrives meanwhile queues and
resumes** (§10); nothing answers it during the reload.

One-time note for the **first** routine deploy after this merge: `vllm-router`
(and `vllm-ocr` where it still runs on the head) gained `ulimits: core` in
`compose/compose.dgx-spark.yaml`, so `up` recreates them once — ≈ 1–2 min
without the router, which is the intent classifier (routing degrades to the
defaults; nothing else). The pair itself stays untouched under the preserve
flag and receives its `ulimits` and cache volumes at the next `--full` (§16).
The same first `up` ships the sentinel to the worker and starts it, and
creates the controller with the token — check with `scripts/cluster-status.sh`
that the "Engine controller" block shows the worker reachable, not DEGRADED
"sentinel unreachable".

The twelve steps, and what does each:

| # | step | who / command | notes |
|---|---|---|---|
| 1 | Check the state before you start | `scripts/cluster-status.sh`; `curl -s http://127.0.0.1:9838/state \| jq '.state,.incident,.recovery'`; the lock (§9); `llm_queued_generations` 0 | READY/BUSY, `incident: null`, lock free, nobody queued. Never deploy into a recovery |
| 2 | Decide the path | you | app-only → routine; engine arg / image / `vllm` definition → `--full` in an announced window; a candidate image is §14, not a deploy |
| 3 | Rehearse | `scripts/deploy.sh --dry-run --ref <sha>`; for an engine change `scripts/cluster-doctor.sh` and `scripts/cluster-sync.sh --image-only` first if the digest moved | dry-run resolves the target, the branch and the schema gate, changes nothing |
| 4 | One deploy at a time | `deploy.sh`: `dr_lock_acquire` (`.runtime/locks/deploy.lock`, waits 30 min, names the holder) | GitHub Actions `concurrency: deploy-dgx-spark` serialises workflows; this lock is what stands between a workflow and a person |
| 5 | Refuse a dirty tree, resolve the ref | `deploy.sh` preflight | the production checkout is a shared working directory; uncommitted tracked changes abort (`FORCE_DIRTY=1` discards them) |
| 6 | The schema gate | `deploy.sh` `schema_gate` | migrations are forward-only (the `queued` status is one); old code never starts on a newer schema — on the deploy and on the automatic rollback |
| 7 | Record before replacing | `scripts/deploy-record.sh` → `.runtime/releases/<stamp>/` | image ids, rendered config (values hashed), applied migrations, PostgreSQL major |
| 8 | Build once, pin by id | `scripts/deploy-preflight.sh build` → `manifest.json` | before any `down`, so a build failure leaves the stack up |
| 9 | One actor restarts the pair | `deploy.sh`: `engine_lock_acquire` (`scripts/lib/engine-lock.sh`, waits 20 min, prints the holder) around `techsara down`/`up` | a recovery 40 s from READY is waited out, not turned into a second restart; released before the health gate |
| 10 | Drain, then recreate | `scripts/deploy-drain.sh check` (grace periods), `wait orchestrator\|frontend\|sync-worker --deadline 90 --quiet-for 5`, `uploads --deadline 90`; then `./techsara up` (`--full`: `./techsara down` first) | advisory: a busy box cannot block a deploy; the grace period is what protects an in-flight answer. V29 marks open `chat_requests` `interrupted` on the orchestrator restart and the sweep resumes them; `queued` rows survive untouched |
| 11 | Prove it | `scripts/deploy-preflight.sh verify` (digest gate); `health()`: orchestrator `/health` with `vllm` and `app_db` ok, frontend, **a real completion**, `scripts/cluster-status.sh` in dual mode; on failure the schema-gated rollback (which also removes the guards a target ref does not define, §16) | a 200 on `/v1/models` is not proof, so the gate asks for `READY.` back |
| 12 | Verify by hand, then record | `scripts/cluster-verify-engine.sh --probe` (drift, both GPUs, Xid, restart counters); `scripts/deploy-smoke.sh --manifest <manifest> --with-model` (digest, schema, not looping, **model clock unchanged** on a routine deploy); `curl -s http://127.0.0.1:9838/state \| jq .state` READY; `llm_queued_generations` 0 and §10's SQL if anything queued; for an engine change the 120-min soak `scripts/cluster-soak.py` and the pass criteria of `VLLM-UPGRADE-RESEARCH.md` §6.4 | the controller sees a `--full` reload as STARTING (it is removed with the head stack by `down` and started by `up` after the head is proven); the orchestrator's breaker opens on the observed connection failures and every request queues until READY |

Rollback of a deploy: automatic on a failed gate (schema-gated;
`--no-rollback` to inspect), or `scripts/deploy-rollback.sh --list` /
`--to <record>`; `scripts/deploy.sh --ref <previous sha>` for a clean
re-deploy of the previous commit. The engine is never rolled back by a routine
path either: an engine-arg or image rollback is a `--full` deploy too (§14
for a candidate).

## 20. Follow-ups

From `ADR-0002-high-availability.md` ("Follow-ups"), owned there; listed here so
the runbook says what it cannot do yet:

1. **The fault class is open in every released vLLM build**
   (`VLLM-UPGRADE-RESEARCH.md`). **Track B is deployed** (2026-09-12 08:39Z,
   §14): the post-`f6326f5` candidate (`nightly-385dce36…`, `sha256:819ec9c0…`)
   with `--gdn-prefill-backend flashinfer` on both ranks; the A/B matrix passed
   its criteria (`CANDIDATE-B.md` §6.4); the 120-min soak is in progress; the
   promotion decision is taken after the **48–72 h canary**, which has not
   started. Rollback is the pinned digest `sha256:24f2f897…`, cached on both
   nodes (§14 step 5). Then the secondary tests, one variable at a time by
   `scripts/cluster-ab.py`: `--long-prefill-token-threshold` + the LONG lane,
   `--max-num-batched-tokens 4096` vs `8192`, `--max-num-seqs 10` vs default,
   and **fifth, Track A** `--moe-backend flashinfer_b12x`. The class counts as
   closed only after ≥ 7 days of production with no controller incident of
   category ≠ `none`.
2. **Router to the worker** (≈ 22 GiB off the head). Memory only; a separate
   change window.
3. **Option C hardware** (two more DGX Sparks, two TP=2 replicas of the same
   model) if the business needs answers *during* a reload; the SLO document
   says which objectives each architecture can meet.
4. **The second tenant** calls the raw port over the RoCE address at
   concurrency 10 — the load shape that fires the fault — and is outside the
   orchestrator's admission lanes, breaker and queue; its client-side patches
   are in `../ISSUE/interview-analysis-client/`.

Also open, from the incident report: no Alertmanager or notification channel
(needs credentials — every alert fires to nobody); **the wedge rule under a
long solo prefill** (`INCIDENT-2026-09-11-vllm.md` §7.2: the token counters do
not move during a single ~950K prefill, the controller called it WEDGED, and
only the spent budget stopped a pair restart — owner: controller); drills 1,
2 and 7 and a forced wait past `LLM_QUEUE_MAX_WAIT_S` not run (§12.1); the
`DUPLICATES`/`LOST`/`ANOTHER MODEL` SQL of §10 not run against production
today; drills 8 and 9 (network blips) are manual procedures printed by the
drill script and need a person at the worker's console.
