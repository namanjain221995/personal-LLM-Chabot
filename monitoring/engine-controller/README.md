# Engine controller and worker sentinel

Two stdlib-only Python 3.12 programs that implement contract §5–§7.1 of
[`docs/availability/CONTRACT.md`](../../docs/availability/CONTRACT.md) (v2,
strict one-model mode). They replace the shell `vllm-watchdog` and exist
because of 2026-09-11 22:15:50Z: the worker rank of the TP=2 pair died, and
for five minutes `/health`, `/v1/models`, `/metrics`, Prometheus `up` and GPU
utilisation all said the engine was fine while `generation_tokens_total` sat
frozen with nine requests "running". Total outage 11 min 00 s, of which the
first five were invisible.

| file | runs on | what it is |
|---|---|---|
| `controller.py` | head (compose service `engine-controller`, host network, Docker socket RW) | **the single recovery authority**: the state machine, the readiness sequence and the routine canary, the recovery choreography, `/state` `/metrics` `/healthz` `/recover` on `:9838` |
| `sentinel.py` | worker (compose service `vllm-worker-sentinel`, host network, Docker socket RW, bound to the RoCE address) | a **sensor and an actuator**: watches the worker container's process table and log stream, reports within one poll, restarts the worker only on the controller's `POST /restart`; serves `/state` `/diagnostics` `/healthz` `/restart` on `:9839` |
| `common.py` | both | Docker Engine API over the unix socket, HTTP with separate connect/read timeouts, Prometheus text rendering with closed label sets, the vLLM `/metrics` and dgx-gpu parsers, bounded error kinds, an injectable clock, a sliding-window budget |
| `tests/` | dev box / CI | pytest, no Docker, no GPU — a fake Docker API on a temporary unix socket, a scriptable fake head engine, a fake sentinel, two fake GPU exporters |

The one belief everything follows from: **a process being alive proves
nothing; only a real completion does.** READY requires the §5 v2 readiness
sequence to have passed for the head container's *current* start, and a
completion success within the last two probe intervals since. Nothing that
happened against a previous head incarnation counts, ever.

## Running the tests

```sh
cd /path/to/worktree
/home/techsphere/Documents/project/personal-LLM-Chabot/orchestrator/.venv/bin/python -m pytest monitoring/engine-controller/tests -q
```

125 tests, ~75 s (the hung-API, handler-timeout and choreography-heartbeat
cases wait real seconds). The fixture points `MEMINFO_PATH` at a file saying
60 GiB, so no test depends on the memory of the box it runs on.
Lint: `ruff check monitoring/engine-controller`. Tests named after a review
finding (`[blocker]`/`[major]`/`[minor]` in their docstring) reproduce that
finding's scenario from `docs/availability/REVIEW-FINDINGS-round1.md` and
`REVIEW-FINDINGS-round2.md`.

A read-only smoke test against the live head, without touching anything
(`DRY_RUN=1` logs the restarts it would perform instead of doing them; the
readiness sequence still runs four small completions — the last two at once):

```sh
cd monitoring/engine-controller
DRY_RUN=1 CONTROLLER_PORT=19838 CONTROLLER_BIND=127.0.0.1 SENTINEL_URL= \
  HEAD_GPU_EXPORTER_URL=http://<dgx-gpu-exporter>:9835/metrics \
  RECOVERY_LOCK_PATH=/tmp/ctl/locks/engine-recovery.lock INCIDENT_DIR=/tmp/ctl/incidents \
  python3 controller.py
curl -s http://127.0.0.1:19838/state | python3 -m json.tool
curl -s http://127.0.0.1:19838/metrics | grep -v '^#'
curl -s -X POST -H 'Content-Type: application/json' -d '{"reason":"manual","category":"manual"}' http://127.0.0.1:19838/recover
```

(`/tmp/ctl/locks` must exist: a missing lock directory is refused, never created.)

## `controller.py`

### Environment

| variable | default | meaning |
|---|---|---|
| `CONTROLLER_BIND` / `CONTROLLER_PORT` | `0.0.0.0` / `9838` | HTTP bind |
| `HEAD_CONTAINER` | `sf-local-ai-vllm-1` | the head container, by name |
| `HEAD_API_URL` | `http://127.0.0.1:8000` | the head's OpenAI API (host network → loopback) |
| `SENTINEL_URL` | *(empty)* | `http://<CLUSTER_WORKER_IP>:9839`. **Empty = single-node mode**: no worker signals, none counted against readiness, `techsara_vllm_worker_*` series not emitted |
| `CLUSTER_SENTINEL_TOKEN` | *(empty)* | sent as `X-Sentinel-Token` on **every** sentinel call (`/state`, `/diagnostics`, `/restart`) when set |
| `ROUTER_HEALTH_URL` | *(empty; `FALLBACK_HEALTH_URL` still read as a fallback spelling)* | the router's `/health`. The router is an **internal classifier only** (v2 §1): unreachable = `router_available=false` → DEGRADED, never a recovery trigger, never a serving path. Empty = not configured (never "unhealthy") |
| `ROUTER_PROBE_INTERVAL_S` | `30` | how often the router `/health` is probed |
| `HEAD_GPU_EXPORTER_URL` | `http://127.0.0.1:9835/metrics` | the head's dgx-gpu exporter, sampled during the participation probe. Unreachable = participation `unobserved` (DEGRADED reason, not a block on READY) |
| `WORKER_GPU_EXPORTER_URL` | *(empty = the worker GPU is not sampled)* | production passes `http://<CLUSTER_WORKER_MGMT_IP>:9835/metrics`. Empty in cluster mode (a sentinel is configured) = participation `unobserved` (one GPU seen is not "both"); empty on a single node = `skipped` |
| `GPU_UTIL_METRIC` | `dgx_gpu_utilization_percent` | the series sampled (maximum over GPUs) |
| `PARTICIPATION_MIN_UTIL` | `30` | both GPUs must reach this (%) at least once during the participation probe |
| `PARTICIPATION_MAX_TOKENS` | `256` | length of the participation probe |
| `PARTICIPATION_SAMPLE_S` / `PARTICIPATION_TAIL_S` | `0.25` / `2` | sampling period; how long sampling continues after the probe finishes (the exporter caches nvidia-smi for 1.5 s and nvidia-smi's utilisation is a trailing window) |
| `READINESS_MAX_TOKENS` | `32` | length of readiness steps 1 and 2 (`ignore_eos` so exactly this many are generated) |
| `READINESS_PROGRESS_RETRY_S` | `1` | pause between the up-to-three `/metrics` re-reads of step 3 |
| `CANARY_MAX_TOKENS` | `4` | the routine canary's `max_tokens` (the exact §5 body) |
| `CANARY_INTERVAL_S` / `CANARY_INTERVAL_FAST_S` | `30` / `10` | cadence while READY/BUSY / while STARTING, RECOVERING, WEDGED, unproven for this start, or a canary has just failed (the confirming probe is pulled forward) |
| `CANARY_TIMEOUT_S` / `CANARY_CONNECT_TIMEOUT_S` | `60` / `5` | read budget of one probe / TCP connect budget |
| `CANARY_MODEL` | *(empty)* | override the model id (default: first id of `/v1/models`) |
| `CANARY_OUTSTANDING_S` | `60` | the wedge rule's "canary outstanding ≥" threshold |
| `CANARY_STARVATION_S` | `300` | ceiling on the saturation exemption of triggers 5 and 7 (below) |
| `CANARY_FAIL_DEGRADED_MAX_S` | `120` | the bound on DEGRADED "awaiting confirmation" (trigger 7): a proven engine that has failed every canary for this long while `/health` still answers 200 is confirmed WEDGED, as `canary_timeout` or `canary_http_error` by the last failure's kind |
| `TTFT_DEGRADED_S` | `10` | canary TTFT above this → DEGRADED |
| `FROZEN_S` | `90` | token counters frozen ≥ this with requests running → wedge signal |
| `COLD_START_BUDGET_S` | `900` | STARTING → DOWN without a readiness proof; also the recovery's wait-for-model-load budget |
| `RECOVERY_BUDGET` / `RECOVERY_WINDOW_S` | `3` / `3600` | at most N recoveries per window, **manual ones included**; then DOWN + `budget_exhausted`, no further destructive action |
| `RECOVERY_COOLDOWN_S` | `120` | after a recovery ends (success or failure) before another may start, manual ones included |
| `RECOVERY_JITTER_S` | `5` | 0–N s random wait before acting |
| `HEAD_API_DEAD_GAP_S` | `10` | minimum seconds between the two `head_api_dead` observations (also between the two looks at a worker fault during `wait_load`) |
| `WORKER_RANK_GRACE_S` | `120` | a rank process absent this long after the worker container start counts as dead |
| `HEAD_RESTART_TIMEOUT_S` | `10` | `docker restart -t` for the head |
| `POLL_S` / `PROBE_TIMEOUT_S` | `5` / `5` | observation loop period / per-probe HTTP timeout |
| `CHOREOGRAPHY_HEARTBEAT_S` | `5` | while the tick thread is inside a blocking recovery call (sentinel `POST /restart` up to 125 s, `docker restart` up to 70 s, diagnostics up to 55 s) a helper thread re-stamps the published snapshot's `generated_at` this often, so the freshness rules (§7.3: 30 s, §8.1: 15 s) measure the controller's liveness, not a step's length. Stops on its own after 600 s (every call it covers has a shorter timeout; past that a stale snapshot is the truth) |
| `DOCKER_SOCKET` / `DOCKER_API_VERSION` | `/var/run/docker.sock` / `1.53` | |
| `RECOVERY_LOCK_PATH` | `${LOCK_DIR:-/run/techsara/locks}/engine-recovery.lock` | the host flock shared with `scripts/lib/engine-lock.sh`; `.runtime/locks` is bind-mounted at `LOCK_DIR`. **The directory must pre-exist** (a missing bind mount is refused, never created: a lock inside the container's own overlay is one nobody on the host contends for). The file is created `0666` and chowned to the directory's owner, so the shell wrappers run as the checkout owner can open it |
| `INCIDENT_DIR` | `${INCIDENTS_DIR:-/run/techsara/incidents}` | one directory per incident id, owned like the mount; `.runtime/incidents` is bind-mounted there |
| `HEAD_MIN_MEM_AVAILABLE_BYTES` | `32212254720` (30 GiB) | the head-memory precondition of STOP_STALE_PAIR: a fresh model load reads 21.8 GiB of weights through the page cache and the rank allocates ~25 GiB; on 2026-09-12 07:11 IST a new CUDA context failed with `NV_ERR_NO_MEMORY` at that edge while `free` still showed 32 GiB. Below this the controller logs a WARNING with the number and the runbook pointer (`docs/availability/RUNBOOK.md` §7) and holds **one tick** (`recovery.blocked = head_memory_low`), then restarts anyway — a dead engine must still be restarted; the warning is what the operator needs. `0` disables the check; an unreadable `/proc/meminfo` never holds anything |
| `MEMINFO_PATH` | `/proc/meminfo` | where `MemAvailable` is read. The controller runs with `network_mode: host` and no lxcfs, so the container's `/proc/meminfo` **is** the host's (verified 2026-09-12: identical `MemTotal`, `MemAvailable` within 11 MB of the host's reading at the same instant). Tests point it at a file |
| `DRY_RUN` | `0` | `1`: every restart is logged instead of performed; everything else (probes, the readiness sequence, the lock) runs for real |
| `CONTROLLER_LOG_LEVEL` | `INFO` | |

### HTTP (contract §6.1)

| method + path | who | answer |
|---|---|---|
| `GET /state` | anyone | the §6.2 JSON (plus additive fields: `readiness` (with `participation_probes[]`: per concurrent step-4 completion `ok`, `outcome`, `http_status`, `connect_s`, `ttft_s`, `total_s`, `tokens`, `terminal`), `signals.gpus`, `signals.head_memory` (`available_bytes` — null when unreadable, never 0 — `observed_at`, `min_bytes`, `low`), `head_container.rank_process_alive`, `head_container.docker_ok`, `worker.started_ago_s` (the sentinel's own measure of the worker container's age — a duration on Node 2's clock; null from an older sentinel), `worker.clock_skew_s` (the sentinel's `observed_at` minus the controller's read time: Node 2's clock against Node 1's, what an operator reads when NTP fails), `canary.kind`, `canary.health_at_start`, `canary.started_at`, `canary.in_flight`, `canary.outstanding_s`, `canary.consecutive_timeouts`, `canary.consecutive_http_errors`, `canary.failing_since` (when the first probe of the current failure streak started; null while the canary passes), `recovery.steps`, `recovery.blocked`, `recovery.blocked_detail`, `recovery.manual_pending`, `recovery.worker_restart`, `recovery.verify_successes`, `recovery.last.detail` and `recovery.last.step` (why and where a failed attempt ended — an exception is named by its class only), `recovery.head_start_origin` ∈ first/controller/external, `recovery.external_repair` (the last worker re-pair decision after an external head start: `head_started_at`, `incident_id`, `at`, `ended_at`, `outcome` ∈ ok/refused/unreachable/failed/dry_run/lock_held/lock_unavailable/not_needed/no_evidence, `detail`; null until one), `last_failure_category`, `cold_start_detail`, `cold_start_seconds`, `recovery_duration_seconds`, `single_node`, `dry_run`). `generated_at` keeps advancing through a blocking recovery step (`CHOREOGRAPHY_HEARTBEAT_S`); every other timestamp is the observation's own. Every error rendered here is a **bounded kind** (`worker.error` ∈ refused/timeout/unreachable/broken/http_NNN/malformed; `head_container.docker_error` ∈ socket_missing/refused/timeout/broken/malformed/api_NNN; `recovery.blocked` ∈ cooldown/lock_held/lock_unavailable/budget_exhausted/head_memory_low) — no socket path, address or exception text |
| `GET /metrics` | anyone | Prometheus text, every §7.1 v2 name |
| `GET /healthz` | anyone | `200 ok` (process liveness only) |
| `POST /recover` | **127.0.0.1 only**, else 403 (the peer is checked *before* the body is read) | body `{"reason":"manual","category":"manual"}` → `202` queued for the next tick, with the `incident` id that will be used; **`429`** `{"reason":"cooldown","retry_after_s":…}` or `{"reason":"recovery budget exhausted", …}` — a manual recovery is subject to the same budget and cooldown as an automatic one (the escalation past them is `scripts/cluster-recover.sh --force` under the engine lock, not this endpoint); `409` if a recovery is in progress. The accepted request is visible in the very next `/state` read (`recovery.manual_pending: true`, `recovery.step: "confirm"`), so a follower never reads a pre-request document |

Both HTTP servers apply a 10 s socket timeout per accepted connection
(`Handler.timeout`): a peer that never finishes its request, or announces a
body it never sends, frees its thread after that long.

### The readiness sequence (contract §5 v2)

Run whenever the engine is **not proven for the head container's current
start** — after every start and recovery, and after any external restart
(the head's `started_at` moved) — at the fast interval until it passes:

| step | what | fails when | `/state.readiness` |
|---|---|---|---|
| 1 | non-streaming `/v1/chat/completions`, `READINESS_MAX_TOKENS` tokens, `ignore_eos`, thinking off, temperature 0, seed 7 | not 200, no `finish_reason`, error object, **no tokens** (a `finish_reason` on empty text proves nothing) | `non_stream_ok` |
| 2 | streaming, same length; TTFT and total latency measured; terminal chunk seen, at least one content token | timeout, error chunk, no terminal chunk, **no tokens** (a bare `[DONE]`) | `stream_ok` (this step's timing is what `signals.canary` shows) |
| 3 | `vllm:generation_tokens_total` on `/metrics` grew by ≥ the tokens steps 1+2 **actually received, and by ≥ 1** (up to three re-reads `READINESS_PROGRESS_RETRY_S` apart: the stat logger records an iteration in the same loop turn that streams its last chunk) — with steps 1 and 2 each requiring a token the expectation is ≥ 2, and the floor keeps the step from ever degenerating to `delta ≥ 0` | `/metrics` unavailable, or the delta is short | `progress_ok`, `tokens_expected`, `tokens_delta` |
| 4 | **two** `PARTICIPATION_MAX_TOKENS` streaming completions started **together** (two sockets, two threads, both started before either is awaited — `PARTICIPATION_PROBES = 2`, deliberately not a knob) while both GPU exporters are sampled every `PARTICIPATION_SAMPLE_S` (plus `PARTICIPATION_TAIL_S`); **both completions must finish** and both GPUs must reach `PARTICIPATION_MIN_UTIL` at least once. Why two at once: the candidate build compiles its multi-sequence GDN prefill kernel in memory at the first scheduler step that batches ≥ 2 prefills (≈ 3 s per rank, on no on-disk cache — contract §6.6, `CANDIDATE-B.md` §2.6); one probe would leave that to the first two real users after READY. The pair runs even when no exporter is configured (the warm-up is theirs; the exporters only decide the verdict) | a probe that does not complete fails the step (`failed_step: participation`, detail `probe k/2 <outcome>: … (n/2 completed; both must)`; no GPU verdict). `failed`: both exporters answered and a GPU stayed below the threshold. **`unobserved`** (an exporter never answered, or answered without the series): evidence missing, not evidence against — on a TP=2 engine the finished completions already needed both ranks, so READY is not withheld; the state is DEGRADED with the reason saying so. `skipped`: no exporter configured on a single node (in cluster mode a missing worker exporter is `unobserved`) | `participation`; per probe `participation_probes[]` (TTFT, total, tokens, outcome); the maxima in `signals.gpus` and, after a recovery, in `<incident>/readiness-<attempt>.json` |
| 5 | the sentinel reports the rank process alive (`VLLM::Worker` waiting or `VLLM::Worker_TP1` joined) | the sentinel is reachable and says `false` (unreachable = not a block; DEGRADED) | `rank_alive` |

`passed` needs 1–3 true, 4 in {ok, unobserved, skipped}, 5 not false. The
proof records when the sequence **started**: it counts only if that is after
the head's `started_at`, so a sequence that began against the previous
container can never prove the new one.

The **routine canary** is step 2 alone with the exact §5 body (`max_tokens`
4, no `ignore_eos`), every `CANARY_INTERVAL_S` once proven. A terminal chunk
without a single content token is a **failure** (`http_error`, detail `no
tokens`), never a success with a warning. Its failures are what triggers 4,
5, 6 and 7 count; which of them a failure feeds is decided by its kind:

| the probe saw | outcome | counts toward |
|---|---|---|
| no response / stream still open past `CANARY_TIMEOUT_S` | `timeout` | `consecutive_timeouts` (5), the streak (7, as `canary_timeout`) |
| HTTP 5xx; a 200 with an error chunk, a stream without a terminal chunk, a non-JSON or error body, **no tokens** | `http_error` | `consecutive_http_errors` (6), the streak (7, as `canary_http_error`) |
| HTTP 4xx — the API *rejected the request* (a changed request shape after an upgrade, a 429 under load) | `http_error` | nothing: a restart cannot fix it and calling it a wedge would queue users on a serving engine; the DEGRADED reason says `not counted as an engine fault` |
| connect refused / timed out | `connect_error` | the streak only (7, as `canary_timeout`; a refused connect is trigger 3's) |
| the probe itself crashed (no HTTP status) | `http_error` | nothing: a bug here is not evidence about the engine |

`consecutive_*` counters and the streak reset on any success, on a new head
incarnation and when a recovery starts; a timeout resets the HTTP-error
streak and vice versa (the two rules count *consecutive* failures of one
kind — the bound of trigger 7 is what catches a mix).

`primary_ready` in `/state` = proven for this start **and** a completion
success within 2 × `CANARY_INTERVAL_S`.

### The state machine (contract §2)

Evaluated once per tick, first match wins:

| condition | state |
|---|---|
| a recovery holds the lock | `RECOVERING` (reason = current step) |
| Docker socket unobservable and not `primary_ready` | `MONITORING_UNKNOWN` |
| Docker socket unobservable but `primary_ready` | `DEGRADED` |
| a failure is confirmed but cannot be acted on (cooldown, lock held by another actor, lock directory missing, no budget) | `WEDGED` for `wedged_frozen_tokens`/`canary_timeout`, else `DOWN`; reason carries `blocked: <kind>`. **Exception:** `worker_rank_dead` (the one trigger resting on the sentinel's document alone) while `primary_ready` → `DEGRADED` "… but the canary passes": a forged or stale sentinel never pins DOWN over a fresh real completion. Evaluated *before* the connect-timeout rule below so a confirmed outage does not flap to UNKNOWN |
| budget exhausted and not `primary_ready` | `DOWN` (`budget_exhausted`) |
| head API connect **timed out / unreachable** (not refused) and not `primary_ready` | `MONITORING_UNKNOWN` |
| head container missing / exited | `DOWN`; `restarting`/`created` → `STARTING` |
| `primary_ready` | `DEGRADED` if any of: sentinel unreachable, sentinel sees no rank (worded `never seen for this head start: unconfirmed, not a trigger` when the rank was never reported alive for this head incarnation), `/health` ≠ 200, `/metrics` unusable, TTFT > `TTFT_DEGRADED_S`, the latest canary failed ("awaiting confirmation", with the bound: `WEDGED as <kind> in Ns unless a canary passes`), configured router unhealthy, a head process *seen alive for this incarnation* has vanished, participation `unobserved` — else `BUSY` if `requests_running > 0`, else `READY` |
| not proven for this start | `STARTING` (reason: which readiness step failed and why) until `COLD_START_BUDGET_S` has elapsed **under the controller's observation**, then `DOWN` (`cold_start_timeout`; not a restart trigger — probing continues and READY returns by itself when the sequence passes). `cold_start_detail` says what it looked like: `load` (never served a completion), `readiness` (completions worked, the sequence never passed), **`compile`** (a compile-error signature — `torch._dynamo`, `flashinfer.jit`, `nvcc`, `cuda_nvrtc` on an error-marked line — in the head log: the persistent kernel cache, contract §6.6; the reason names `scripts/cluster-recover.sh --clear-kernel-cache`; the controller never deletes a cache itself) |
| proven once since this start, canary now stale/failing/outstanding, nothing confirmed | `DEGRADED` "… awaiting confirmation" (the honest in-between: not READY, not yet WEDGED). **Bounded**: three consecutive HTTP errors (trigger 6) or `CANARY_FAIL_DEGRADED_MAX_S` of consecutive failures with `/health` 200 (trigger 7) confirm a wedge; only a failure that is not the engine's (a 4xx, a probe crash) can hold DEGRADED longer, and the reason says so |

### Per-incarnation rules (the review blockers)

- When the head's `started_at` moves: the probe counters, the last result,
  the proof, the pending triggers and any probe in flight are dropped. A
  result whose probe **started** before the current `started_at` is
  discarded at harvest (a success from the old head is not a proof; a
  timeout from it is not a wedge).
- A canary timeout counts toward trigger 5 only on a **proven** engine and
  only if `/health` was not 5xx when the probe started (`canary.health_at_start`).
  Timeouts while a head loads (vLLM binds `:8000` before the engine exists)
  are STARTING, the cold-start budget's business. `/health` *silent* with TCP
  up does count — that is the hung-API shape.
- Process names are evidence only when **seen alive for this incarnation and
  then gone** (`head_container.engine_process_alive` / `rank_process_alive`
  are `null` until first seen). A TP=1 engine (vLLM's `uni` executor — the
  router's real process table has only `vllm serve` and `VLLM::EngineCore`)
  has no `VLLM::Worker_TP` process and is never DEGRADED or restarted for
  lacking one. The same gate applies to the **worker's** rank as the sentinel
  reports it: trigger 1(c) fires only once the sentinel has reported
  `rank_process_alive: true` at least once since the current head start
  (a pair proven while the sentinel was unreachable, then a sentinel that
  does not recognise the build's process title, is DEGRADED — never a
  restart of a healthy pair).
- Cross-host time is never compared. The sentinel reports `started_ago_s`
  (its container's age on Node 2's clock alone) and trigger 1(a) sets it
  against the controller's own time since the last proven completion; a
  Node 2 clock ten minutes off no longer turns every recovery into a fresh
  `worker_rank_dead` (or hides a real one).
- `cold_start_seconds` is measured once per head start, and only for a start
  the controller watched from within 120 s of its beginning (redeployed beside
  an hour-old head, its own first proof is not the head's cold start; the
  series is omitted).
- **Every head start has an origin** (`recovery.head_start_origin`): `first`
  (the first start this controller process observed — bookkeeping only),
  `controller` (its own STOP_STALE_PAIR: the attempt was in progress, the
  head restart it issued was applied and not yet observed, or the start
  falls within `OWN_RESTART_WINDOW_S` = 120 s of `rec.restart_issued_at` —
  a daemon that applied it after the reply timeout and the re-inspects) or `external`
  (Docker's restart policy after `vllm serve` exited, an operator's `docker
  restart`). An external start with rank 1 still paired with the previous
  head is answered with **one worker re-pair per head incarnation** (below):
  the sentinel's `POST /restart`, never a head restart. What the sentinel
  said about rank 1 in the incarnation that just ended (`rank_joined`, the
  seen-alive gate) is read *before* the per-incarnation resets, because it
  is the evidence that the worker belongs to the dead head.

### Detection → confirmation (contract §6.3)

| # | category | condition | observations |
|---|---|---|---|
| 1 | `worker_rank_dead` | sentinel reachable and: (a) the head proven and the worker container started **after the last proven completion** — on one clock: the sentinel's `started_ago_s` (Node 2's duration) < the controller's time since that completion (an older sentinel without the field falls back to comparing its `started_at` with the completion time, two clocks, and the detail says so); (b) `last_fault.at` newer than the head's `started_at` **and** than the worker's own `started_at` (a fatal signature in the worker's current incarnation after the current head start — no `proven` gate, so a rank dying while the head loads is caught); (c) the head proven, the rank **seen alive for this head start** by the sentinel, and now no `VLLM::Worker` process at all ≥ `WORKER_RANK_GRACE_S` after the worker's start | 1 |
| 2 | `head_engine_dead` | `/health` 5xx | 1 |
| 2′ | `head_engine_dead` | `vllm serve`/`VLLM::EngineCore`/`VLLM::Worker_TP*` seen alive for this incarnation and now missing from the head's process table, on a proven engine (the 07:05Z case: rank 0 died and the head sat in a collective for 3 min) | 2 consecutive ticks |
| 3 | `head_api_dead` | TCP refused / connect error while the container runs, after READY | 2, ≥ `HEAD_API_DEAD_GAP_S` apart |
| 4 | `wedged_frozen_tokens` | `frozen_seconds ≥ FROZEN_S` with `requests_running > 0`, canary outstanding ≥ `CANARY_OUTSTANDING_S` (counted from the first probe since the last success, across timeouts), `/health` 200 | 2 |
| 5 | `canary_timeout` | two consecutive canary timeouts on a proven engine, **whatever `/health` says** (a hung API answers nothing) — **exempt while the engine is progressing** on a *fresh* `/metrics` sample (counters moved within `FROZEN_S` with requests running) and the canary has been outstanding < `CANARY_STARVATION_S`: a saturated FCFS scheduler starving a 4-token request is not a wedge | the two probes |
| 6 | `canary_http_error` | three consecutive canary HTTP errors **the engine answered** (5xx, an error chunk, a stream without a terminal chunk, no tokens — a 4xx never counts) on a proven engine, each counted only if `/health` was not 5xx when that probe started (a 5xx is trigger 2's). **No saturation exemption**: an error is an answer, not a request starved of a slot. State `WEDGED` (the API answers, completions do not) | the three probes |
| 7 | `canary_timeout` **or** `canary_http_error` (the last failure's kind) | the bound on DEGRADED: a proven engine whose every canary has failed for ≥ `CANARY_FAIL_DEGRADED_MAX_S` (counted from the start of the first failed probe) while `/health` answers 200 — whichever mix of timeouts and errors got it there (alternating kinds confirm neither 5 nor 6). For the timeout kind the saturation exemption of 5 still holds up to `CANARY_STARVATION_S`; a failure that is not the engine's (4xx, probe crash) never confirms | the streak's length |

Trigger 1(a) compares against the last proven completion rather than the
head's `started_at` (the contract's wording): once proven, that instant is
newer than the head's start anyway, and it removes the false positive of a
worker that legitimately started a moment after the head (either order
happens under the last-resort healthchecks) and then served completions.
The comparison is `started_ago_s < now − last_success_at`: the sentinel's
document is at most one of its polls old, which can only make the reported
age *smaller* than the truth, and a false positive would need a TP=2
completion finishing within that poll of the worker container's start —
which a rank that has to load the model cannot do. 1(b) is the contract's
fault-signature half, restored without the gate.

A confirmed category is published as its state (`WEDGED`/`DOWN`) **before**
`RECOVERING`, so the transition log always reads DETECT → CONFIRM →
RECOVERING.

### The choreography (contract §6.3)

Every step is written to `/state` (`recovery.step`, `recovery.steps[]`) and
logged with the incident id before the next begins:

```
confirm      head-memory precondition: MemAvailable < HEAD_MIN_MEM_AVAILABLE_BYTES (30 GiB) → WARNING
             with the number and the runbook pointer, recovery.blocked = head_memory_low for ONE tick,
             then the same confirmed failure proceeds at the next tick (a manual request is held, not
             dropped); unobserved memory never holds
             lock taken (flock, non-blocking; held by another actor → stand by, retry each tick)
             budget recorded, incident opened (or attempts+1 of the open one; an incident older than
             RECOVERY_WINDOW_S is closed and a new id opened), deadline armed (COLD_START_BUDGET_S from
             here, re-armed from the restart at wait_load: a recovery in progress always has one),
             the choreography heartbeat armed (generated_at keeps advancing through every blocking call
             below), jitter 0–RECOVERY_JITTER_S, any probe in flight discarded
capture      <INCIDENT_DIR>/<id>/head-logs-<attempt>.txt   (docker logs --tail 400, de-multiplexed)
                                 worker-diagnostics-<attempt>.txt (sentinel /diagnostics)
                                 state-<attempt>.json      (the /state document)
stop_pair    sentinel POST /restart (worker first, so it waits at the rendezvous) — outcome recorded as
             recovery.worker_restart ∈ ok|refused|unreachable|failed|skipped|dry_run and the
             techsara_vllm_recovery_worker_restart one-hot; a 401/403 is logged as a token mismatch
             then docker restart -t HEAD_RESTART_TIMEOUT_S of the head; a reply that arrives after the
             client's timeout is NOT a failure: the container is re-inspected and a moved
             StartedAt/FinishedAt (or Restarting) means the restart was applied
wait_load    /health 200 from the NEW container start (started_at ≥ the time the restart was issued),
             budget COLD_START_BUDGET_S → else the attempt fails, cold_start_timeout (+ cold_start_detail);
             a worker fault logged after the new head start, seen twice ≥ HEAD_API_DEAD_GAP_S apart,
             fails the attempt at once as worker_rank_dead instead (a worker that Docker's restart
             policy re-creates in the meantime clears the condition before the second look).
             No probes are started in this phase.
canary       the §5 v2 readiness sequence (fast interval); it counts only if it STARTED after both the
             new container's started_at and the moment stop_pair returned
mark_ready   lock released, cooldown armed, READY, recovery_duration measured, attempts_total{succeeded},
             readiness-<attempt>.json (per-step verdicts, both participation probes' timings, GPU
             maxima) written to the incident dir
verify       three consecutive canary successes → incident.ended_at; a failure resets the count
```

A failed head restart, a cold-start timeout or a worker fault during
`wait_load` ends the attempt (`attempts_total{failed}`, cooldown); the next
confirmation starts the next attempt of the same incident until the budget is
spent → `DOWN`, `budget_exhausted`, **no further destructive action**, probing
continues, READY again by itself when the readiness sequence passes.

So does an **unexpected exception** anywhere inside a recovery tick — between
the lock and `wait_load`, or while advancing `wait_load`/`canary`: it is
logged at ERROR **with its traceback**, the attempt is failed (lock released,
cooldown armed, `recovery.last.detail = "controller error during <step>:
<ExceptionClass>"`) and the next confirmation tries again. A RECOVERING that
nobody advances, with the flock held, is the one outcome worse than a failed
recovery: the orchestrator would queue every request forever and
`cluster-recover.sh` could not take the lock.

**An incident ends by any path**: three consecutive canary successes on a
proven engine close it whether the last attempt succeeded, failed, or the
budget was spent and an operator (or the last-resort healthcheck) brought the
pair back. The next failure then opens a new incident id with its own
evidence directory.

### The worker re-pair after an external head start (drill 5, 2026-09-12)

Drill 5 (`kill -9` of the head's `vllm serve`, 09:55:49Z): Docker's restart
policy brought the head container back within 5 s, the controller logged
`head container restarted` and went STARTING — and nothing re-paired the
worker. Rank 1 (`VLLM::Worker_TP1`) stayed in the dead head's process group,
the new head waited out `--distributed-timeout-seconds` (300 s) at the
rendezvous and exited, Docker restarted it again at 10:01:25Z, and only the
worker's last-resort healthcheck tier (age > 900 s, 8 misses) restarted the
worker at 10:06:06Z: READY 767 s after the break instead of ~180 s. v1's
worker healthcheck did this re-pair on a 5xx; v2 made the healthchecks
report-only and the controller the single authority, so the controller owns
the case:

```
observe      head.started_at moved while no recovery is in progress and the controller did not issue
             the restart (origin = external); cluster mode only (a sentinel is configured)
evidence     the worker is still the previous head's rank: the sentinel reported rank 1 joined (or the
             rank process alive) for the incarnation that just ended, OR the worker container is OLDER
             than the new head (started_ago_s > the head's age — one clock, as trigger 1(a); an older
             sentinel without the field falls back to two clocks). Waits, undecided, while the
             sentinel is unreachable (nothing to read, nobody to ask). NOT done when: the new head
             already answers /health 200 or is proven (rank 1 joined, whatever the timestamps say);
             the worker started AFTER the new head (its restart policy, its last-resort tier or an
             operator got there first); nothing says the rank was paired (never reported joined or
             alive for the previous head and the worker's age is unknown)
lock         the same flock as a recovery — held by another actor (cluster-up.sh, cluster-recover.sh)
             → that actor is restarting the pair and owns the worker too: skipped, one WARNING, and
             not retried for this head start; released right after the POST
incident     head_restarted_externally opened (or the open one kept), last_failure_category set,
             the STARTING state published with the reason before anything blocks
capture      <INCIDENT_DIR>/<id>/head-logs-repair-<head start stamp>.txt, worker-diagnostics-…,
             state-…json, and repair-<stamp>.json with the outcome afterwards
restart      sentinel POST /restart, the choreography heartbeat covering the wait; outcome recorded as
             recovery.external_repair.outcome ∈ ok|refused|unreachable|failed|dry_run|lock_held|
             lock_unavailable|not_needed|no_evidence; `ok` counts
             techsara_vllm_recovery_attempts_total{outcome="repaired_worker"}
after        STARTING carries `head restarted outside the controller; worker re-paired` in its reason
             until the readiness sequence passes; the incident ends by the usual path (three canary
             successes). ONCE per head incarnation whatever the sentinel answered: a refused (token
             mismatch) or unreachable sentinel is logged at ERROR and left to the operator or the
             worker's last-resort tier — a second POST could only restart a worker that has just come back
```

Not a pair restart: **no head is touched**, so it is neither charged to
`RECOVERY_BUDGET` nor blocked by the cooldown, and it counts as neither
`started` nor an incident `attempt`. It never fires for the controller's own
choreography (STOP_STALE_PAIR restarts the worker first; the head start it
then observes is its own even when the attempt failed before the start was
seen, or the daemon applied the restart late — within 120 s of
`rec.restart_issued_at`), never in single-node mode, and never while a recovery is in progress
(an external restart *during* the controller's own `wait_load` is bounded by
that attempt's cold-start budget, not re-paired).

### Metrics notes (contract §7.1 v2)

Every name in §7.1 is rendered with only the bounded labels:
`techsara_vllm_router_available` (renamed from `fallback_available`),
`techsara_vllm_participation_ok` (both GPUs seen working during the last
readiness sequence) and `techsara_vllm_generated_at_seconds` (the snapshot's
own timestamp) are the v2 additions; `techsara_vllm_recovery_worker_restart{outcome}`
is an extra one-hot beyond the contract list, and
`techsara_vllm_head_mem_available_bytes` (the head host's `MemAvailable` as of
the last tick — the input of the head-memory precondition, and what the
runbook's memory check reads) is an extra gauge. The bounded sets carry two
2026-09-12 additions: the category `head_restarted_externally` (the one-hot
`techsara_vllm_last_failure_category`, and the incident's category) and the
outcome `repaired_worker` on `techsara_vllm_recovery_attempts_total` (a
worker re-pair after an external head start — not a `started` attempt, not
budgeted, so `techsara_vllm_restart_budget_remaining` does not move). Series whose value is not known
are **omitted rather than faked**: `head_mem_available_bytes` when
`/proc/meminfo` cannot be read; `head_container_running` and
`head_engine_process_alive` when the Docker socket is unobservable (or the
process was never seen); `worker_container_running`, `worker_rank_alive` and
`container_restart_count{rank="1"}` when the sentinel did not answer, and all
`worker_*` in single-node mode; `synthetic_*` before the first probe;
`cold_start_seconds` / `recovery_duration_seconds` before their first
measurement (and `cold_start_seconds` for a start the controller did not
watch). `last_success_timestamp_seconds` is `0` when there has been no
success since the controller started (the recording rule's age is then the
controller's uptime, which is the truth). Alert authors must treat absence
explicitly (`absent()` / `or on() vector(…)` is *their* decision, not a `0`
minted here).

### Logging

`[controller] <UTC time> LEVEL …`. One line per state transition
(`state A -> B reason=… incident=…`), per DETECT/CONFIRM, per recovery step
(`incident=<id> attempt=<n> step=<step>`), per restart, one per readiness
sequence outcome (with both participation probes' TTFT/total), one WARNING per
confirmed failure when the head is short of memory before STOP_STALE_PAIR,
one WARNING per worker re-pair after an external head start (with the
evidence and the incident id; the `head container restarted` INFO says
whether the start was the controller's own or `NOT performed by the
controller`), one WARNING **per kind** of Docker API error other than a 404 (`docker API
unusable: api_400 — …`, with the `DOCKER_API_VERSION` hint for a 4xx; an
INFO when it answers again — never once per tick, never only at DEBUG: while
it holds no recovery can start), one ERROR with traceback per exception
inside a recovery tick. Nothing private: the probe prompt is a constant,
response text is discarded as read; exception text and paths stay in the
log and never reach `/state`.

## `sentinel.py`

### Environment

| variable | default | meaning |
|---|---|---|
| `SENTINEL_BIND` / `SENTINEL_PORT` | `127.0.0.1` / `9839` | bind to `CLUSTER_WORKER_IP` (the RoCE rail-A address) in the cluster |
| `WORKER_CONTAINER` | `sf-local-ai-worker-vllm-worker-1` | |
| `CLUSTER_HEAD_IP` | *(empty → loopback only)* | the only peer allowed to `POST /restart` — always, token or not |
| `CLUSTER_SENTINEL_TOKEN` | *(empty)* | when set, **every endpoint** requires a matching `X-Sentinel-Token` (constant-time compare); `/healthz` is exempt for a *local* peer only (the container's own healthcheck) |
| `SENTINEL_AUTONOMOUS` | **`0`** | `1`: the bounded self-restart below. Off by default and in production (v2 §6.4): the controller is the only actor |
| `SENTINEL_POLL_S` | `5` | |
| `WORKER_RANK_PROCESS` / `WORKER_RANK_JOINED_SUFFIX` | `VLLM::Worker` / `_TP` | process title that proves the rank is alive (`VLLM::Worker` while waiting at the rendezvous — verified in the pinned image's `multiproc_executor.py` `setup_proc_title_and_log_prefix`), and the suffix that means it joined (`VLLM::Worker_TP1`). A waiting worker is never "absent" |
| `SENTINEL_RANK_GRACE_S` | `120` | (autonomous only) rank absent this long after the container start (and after our own last restart) → restart |
| `SENTINEL_RESTART_TIMEOUT_S` | `5` | `docker restart -t` (SIGKILL after 5 s also interrupts a core dump in progress) |
| `SENTINEL_SELF_RESTART_BUDGET` / `SENTINEL_SELF_RESTART_WINDOW_S` | `3` / `3600` | (autonomous only) self-restarts per window; beyond it the sentinel stands down and says so |
| `SENTINEL_DIAGNOSTICS_LINES` | `400` | `/diagnostics` tail |
| `DOCKER_SOCKET` / `DOCKER_API_VERSION` / `DRY_RUN` / `SENTINEL_LOG_LEVEL` | as above | |

### HTTP (contract §6.4)

| method + path | who | answer |
|---|---|---|
| `GET /state` | anyone on the link; the token when configured | `{"container": {"running","health","restart_count","started_at",…}, "started_ago_s", "rank_process_alive", "rank_joined", "last_fault": null or {"at","signature"}, "autonomous", "self_restarts_in_window", "observed_at", …}` — `started_ago_s` = `observed_at − container.started_at`, both on this host's clock: the container's age as a duration, which the controller compares with its own clock (§6.3 trigger 1(a)) so the two hosts' wall clocks are never set against each other; `null` when there is no container |
| `GET /diagnostics` | same | the worker container's last 400 log lines, plain text |
| `GET /healthz` | a local peer, or anyone with the token | `200 ok` |
| `POST /restart` | `CLUSTER_HEAD_IP` **and** the token when configured (checked *before* the body is read) | `docker restart -t 5` → `{"restarted_at": …}`; `403` otherwise; `502` `{"error": "<bounded docker kind>"}` if Docker refused |

### What it reports, and (only if asked) what it does

Each tick: inspect the container, read its process table, fetch the log
lines since the last one scanned (Docker `since` is whole seconds; the line
timestamps de-duplicate), and look for the bounded signatures of §6.2
(`misaligned address`, `illegal memory access`, `AcceleratorError`,
`WorkerProc hit an exception`, `died unexpectedly`, `EngineDeadError`,
`NCCL error`, `out of memory`). Faults from a previous incarnation are never
re-reported; `last_fault` keeps its timestamp (the controller compares it
with the head's and the worker's own `started_at`).

With `SENTINEL_AUTONOMOUS=1` only: a signature newer than the container's
current start (and newer than our own last restart), or a rank process
absent past the grace, restarts the container — within the budget, and
re-checked **under the restart lock** so the head's `POST /restart` landing
in between never causes a second restart of the freshly started worker.

Validated against the preserved logs: the scanner finds the 07:05:33Z
`misaligned address` line in `head-fault-20260911T070854Z.log` and nothing
in 4,185 lines of the healthy head log.

## Compose wiring (owned by the sre workstream)

`engine-controller` (head): `python@sha256:…` (3.12-slim), `network_mode: host`,
`/var/run/docker.sock` RW, `./monitoring/engine-controller:/app:ro`,
`./.runtime/locks:/run/techsara/locks`, `./.runtime/incidents:/run/techsara/incidents`
(both directories must exist on the host before the container starts),
`command: ["python", "-u", "/app/controller.py"]`, port 9838 on the host;
`SENTINEL_URL=http://${CLUSTER_WORKER_IP}:9839` and
`WORKER_GPU_EXPORTER_URL=http://${CLUSTER_WORKER_MGMT_IP}:9835/metrics` only
in the cluster overlay; `ROUTER_HEALTH_URL` (was `FALLBACK_HEALTH_URL`) and
`HEAD_GPU_EXPORTER_URL` pointing at an address the host network can reach
(the dgx-gpu exporter sits on the `application` bridge: publish `9835` on
`127.0.0.1` or pass its bridge address). `HEAD_MIN_MEM_AVAILABLE_BYTES` is
not plumbed through compose yet: the 30 GiB default applies until the sre
workstream adds an `ENGINE_HEAD_MIN_MEM_AVAILABLE_BYTES` pass-through beside
the other `ENGINE_*` keys. `/proc/meminfo` needs no mount — with
`network_mode: host` and no lxcfs the container reads the host's.

`vllm-worker-sentinel` (worker): same image, `network_mode: host`,
`/var/run/docker.sock` RW, `./sentinel.py` and `./common.py` bind-mounted
into `/app` (shipped by `scripts/cluster-sync.sh`), `SENTINEL_BIND=${CLUSTER_WORKER_IP}`,
`CLUSTER_HEAD_IP`, optional `CLUSTER_SENTINEL_TOKEN`, `SENTINEL_AUTONOMOUS`
unset (off).

Both healthchecks: the stdlib `urlopen(…/healthz)` one-liner the exporters
use, from inside the container (a local peer: no token needed).

## Rolling back

Rolling the compose files back (`git checkout <previous> && ./techsara up`,
or `deploy.sh`) restores the shell `vllm-watchdog` but leaves both of these
programs running as Compose orphans — three actors restarting the pair on
their own rules is the exact 2026-09-11 pattern this programme ends. Remove
them explicitly, controller first:

```sh
# head
docker rm -f sf-local-ai-engine-controller-1
# worker (from the head; read-only ssh helper from scripts/lib/cluster-common.sh)
ssh <worker> 'cd ~/.techsara-cluster && docker compose --env-file worker.env -f compose.cluster-worker.yaml rm -sf vllm-worker-sentinel'
```

`.runtime/locks/engine-recovery.lock` and `.runtime/incidents/` are safe to
leave in place: the lock is only ever held while a recovery runs (nothing
holds it after the container is gone), and the incident directories are
evidence. If a recovery was in progress at rollback time, `docker ps` shows
whether the head is back; `scripts/cluster-status.sh` reports the pair
without the controller.
