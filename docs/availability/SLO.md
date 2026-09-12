# Service-level objectives for the main model (v2, strict one-model mode)

Every number here is either **measured** on this cluster (with the source) or a **target** the
2026-09-12 programme is built to meet; targets are re-measured by the drills in
`scripts/recovery-tests/engine_failure_drills.sh` and the soak in `scripts/cluster-soak.py`, and
this file is updated with what was observed. Availability is computed from **real synthetic
completions** (`techsara_vllm_synthetic_success`), never from container uptime. Only
`nvidia/Qwen3.6-35B-A3B-NVFP4` answers a person (`CONTRACT.md` v2 §1); no objective below is met
by another model answering.

## 1. The four SLOs

| SLO | Indicator (PromQL / SQL) | Objective | Architecture that can meet it |
|---|---|---|---|
| **A. Primary-model availability** | `avg_over_time(techsara_vllm_synthetic_success[30d])` | 99.5 % on this build (one ≈ 5-min reload per day is 99.65 %); **99.9 %** once the GDN fault class is closed by a validated build (`CANDIDATE-B.md`) | single TP=2 replica (today) |
| **B. Request continuity availability** — an accepted request is **never lost, never duplicated, never answered by another model, and resumes automatically** on the primary when it is READY | `increase(llm_resumed_generations_total{outcome="resumed"}[30d]) / increase(llm_resumed_generations_total{outcome=~"resumed\|expired"}[30d])`; `max_over_time(llm_queue_wait_seconds…)` per incident; the drill matrix (drills 5, 14, 15) | **100 %** of generations queued during a recovery resume on the same `generation_id` within `LLM_QUEUE_MAX_WAIT_S=900` of READY; `expired` = 0 outside a `DOWN`/`budget_exhausted` incident; `engine` is `primary` on every attempt | orchestrator continuity (§8.3 v2) + resume sweep (§8.4 v2), today. **Answers *during* a reload need Option C** (two TP=2 replicas, +2 Sparks) — no orchestrator work changes that |
| **C. Exactly-once durability** | the two SQL checks of §3 (`LOST`, `DUPLICATES`) return **no rows**; `llm_resumed_generations_total{outcome="duplicate_suppressed"}` counts the guard doing its job | **zero** accepted generations lost silently; **zero** duplicate final answers, in the drill matrix and in production | orchestrator V29 + breaker + continuity (today) |
| **D. Monitoring accuracy** | false-UP and false-DOWN minutes counted in each incident review (`INCIDENT-*.md`); `VllmReadyUnproven` firings | **zero** false-UP minutes (a dead engine must never read READY; READY claimed without a proven completion for 120 s is itself an alert), **zero** false-DOWN minutes from missing data alone (absence → MONITORING_UNKNOWN) | controller + rules (today) |

## 2. Detection, recovery and continuity objectives (measured → target)

| Objective | 2026-09-11 22:15Z (before) | Target | How it is met |
|---|---|---|---|
| Rank death → state ≠ READY | 5 min 03 s (`up==1`, `/health` 200 throughout) | **≤ 30 s** | sentinel poll 5 s + controller poll 5 s + one confirmation |
| Wedge (tokens frozen, process alive) → WEDGED | 5 min (vLLM's own 300 s RPC timeout) | **≤ 2 min 30 s** | frozen ≥ 90 s + canary outstanding ≥ 60 s, two observations |
| Dead rank reaped | 4 min 51 s (apport core dump) | **≤ 10 s** | `ulimits: core: 1` (kernel aborts the pipe dump) + the controller's `POST /restart` to the sentinel |
| Head stuck in teardown → restarted | 1 min 58 s (4 × 30 s healthcheck misses) | **≤ 15 s** | controller `head_api_dead` on two observations |
| A request that arrives during the outage: accepted, persisted, told the truth | generic error / spinner (second tenant: HTTP 500 and connection refused) | **≤ 10 s** from detection to the status line `Main model is recovering—your request is safely queued.` (row status `queued`) | breaker external-open on RECOVERING; `continuity.py`; the orchestrator polls the controller every 5 s |
| Queued request resumed on the recovered primary | never (in-flight requests were lost) | **≤ 30 s** after READY, same `generation_id`, `attempt` + 1, `retry_reason=recovery`, exactly once | the READY event + the resume sweep under a lease (§8.4 v2) |
| Primary READY again (fault → the §5 v2 readiness sequence passed) | 11 min 00 s | **≤ 6 min** warm (3 m 32 s reload + detection + readiness sequence); ≤ 8 min cold | choreography in CONTRACT §6.3; compiled-kernel caches reused (§6.6) |
| READY is proven, not claimed | `/health` 200 = "healthy" | every READY preceded by: non-streaming completion, streaming completion, token-counter progress, both GPUs > 30 % during the participation probe, rank alive | CONTRACT §5 v2; `techsara_vllm_participation_ok` |
| Monitoring-only failure → user-visible inference error | n/a (not exercised) | **none** | drill 1; `VllmMonitoringUnknown` is a warning, never a page for inference |
| Bounded restart loops, one authority | one restart / 45 min (shell watchdog) + healthchecks + an autonomous worker restart — three actors | **3 per hour, then stand down; exactly one actor** (the controller; healthchecks report-only until `VLLM_HEALTHCHECK_KILL_AFTER`) | controller budget + cooldown; sentinel acts only on `POST /restart`; the engine lock |
| Both GPUs participate in every primary readiness test | verified (93 % / 93 % peak, `05-cluster-verify-engine-probe.txt`) | every readiness sequence, every drill and soak | §5 v2 step 4; `scripts/cluster-verify-engine.sh --probe` |
| Long prompt admitted only onto an idle engine | nine mixed requests in flight at the fault | prompts > 131,072 tokens: one at a time, started at `requests_running ≤ 0`, wait ≤ 600 s and told | CONTRACT §6.7 admission lanes |

## 3. Exposed availability metrics (Prometheus) and the two SQL checks

```
primary availability (30d)         avg_over_time(techsara_vllm_synthetic_success[30d])
continuity (30d)                   increase(llm_resumed_generations_total{outcome="resumed"}[30d])
                                   / increase(llm_resumed_generations_total{outcome=~"resumed|expired"}[30d])
requests waiting now               llm_queued_generations            (state 7 QUEUEING when > 0 and the primary is not READY/BUSY)
queue wait                         histogram_quantile(0.99, rate(llm_queue_wait_seconds_bucket[1h]))
mean time to detect                avg(techsara_vllm_recovery_duration_seconds - <reload>)  — reported per incident in INCIDENT-*.md
mean time to recover               avg_over_time(techsara_vllm_recovery_duration_seconds[30d])
cold start                         techsara_vllm_cold_start_seconds  (measured once per head start; a cache miss shows here first)
breaker open → closed              llm_breaker_transitions_total{engine="main",to="OPEN"} → {to="CLOSED"} — reported per incident
restart count                      techsara_vllm_container_restart_count{rank}
recovery success rate              increase(...{outcome="succeeded"}[30d]) / increase(...{outcome="started"}[30d])
request error rate                 rate(llm_engine_unavailable_total[1h]) / rate(request_total[1h])
admission                          llm_admission_lane_active{lane}, llm_admission_waiting{lane},
                                   histogram_quantile(0.99, rate(llm_admission_wait_seconds_bucket{lane="long"}[1h])),
                                   rate(llm_admission_rejections_total{reason}[1h])
stale / lost requests (live proxy) chat_requests_interrupted (existing) — must return to 0 after every recovery
```

There is no `chat_requests_lost_total`: the process that loses a request is dead and cannot
count it. Loss and duplication are proven from the database (the durability report's checks,
run against planted rows in `orchestrator/tests/test_generation_durability.py`; each must return
**no rows**). `900 s` is the reconnect loop's patience:

```sql
-- DUPLICATES: more than one non-error assistant message for one intent
SELECT r.intent_id, count(DISTINCT m.id) AS answers
FROM chat_requests r
JOIN usage_events u ON u.meta->>'intent_id' = r.intent_id
JOIN messages m ON m.conversation_id = r.conversation_id
               AND m.generation_id = u.generation_id
               AND m.role = 'assistant' AND NOT (m.meta ? 'error')
GROUP BY r.intent_id HAVING count(DISTINCT m.id) > 1;

-- LOST: a request that is neither cancelled nor answered, and nobody is working on it
SELECT r.intent_id, r.status, r.attempt, r.updated_at
FROM chat_requests r
LEFT JOIN messages m ON m.conversation_id = r.conversation_id
                    AND m.generation_id = r.generation_id AND m.role = 'assistant'
WHERE m.id IS NULL AND r.status NOT IN ('cancelled', 'queued')
  AND r.updated_at < now() - (900 * interval '1 second');

-- QUEUED TOO LONG: a queued row older than LLM_QUEUE_MAX_WAIT_S while the primary is READY
SELECT r.intent_id, r.attempt, r.updated_at
FROM chat_requests r
WHERE r.status = 'queued' AND r.updated_at < now() - (900 * interval '1 second');
```

`ANOTHER MODEL` needs no query: every attempt's `usage_events.meta->>'engine'` is `primary`; a
value of anything else is a defect (`SELECT count(*) FROM usage_events WHERE meta ? 'engine' AND
meta->>'engine' <> 'primary'` must be 0).

## 4. Why not 99.99 %, and why nothing is served during a reload

99.99 % is 4.3 minutes a month. One cold reload of a single TP=2 replica is 5 m 20 s. No amount
of detection speed makes a single replica of a two-node engine meet 99.99 % for the *primary*;
that needs Option C (two TP=2 replicas, +2 DGX Sparks) or a primary small enough to run on one
Spark with the other as a hot replica (Option B, rejected for capability).

In strict one-model mode there is no user-visible availability figure separate from SLO A:
while the single 35B instance reloads, nobody is answered, by requirement. What SLO B measures
instead is that the reload costs a person a *wait*, never a *loss*: the request is kept, the
person is told, and the same generation resumes on the main model. Serving answers *during* the
reload is Option C's property alone.

## 5. What was actually measured in the 2026-09-12 programme

(filled in by the drill and soak runs — see the incident report's "Verification" section; an
objective that was not exercised is marked **not measured**, not assumed.)

| Measurement | Value | Source |
|---|---|---|
| rank death → RECOVERING (drill 3, `kill -9 VLLM::Worker_TP1`) | **6 s** to leave READY, RECOVERING with `worker_rank_dead` | `.runtime/drills/20260912T094545Z/drill-3-*.log` |
| head EngineCore death → RECOVERING (drill 4) | **4 s**, `head_engine_dead` | `.runtime/drills/20260912T095049Z/drill-4-*.log` |
| dead rank reaped | no core-dump hold observed: the worker container restarted 7 s after the kill (09:45:46 → 09:45:53) | drill 3 record (`ulimits core: 1` in force on both ranks) |
| fault → READY, warm caches (candidate B) | drill 3 **176 s**, drill 4 **172 s**; three coordinated restarts (drill 16) 180 / 186 / 188 s; the pair reloaded worker-first every time | drill 3, 4, 16 records; `techsara_vllm_recovery_duration_seconds` |
| head API death (drill 5, `vllm serve` exits, Docker restarts the head in 5 s) | detection 5 s, but READY only after **767 s**: the new head waited at the rendezvous for a worker still paired with the old head — the v2 controller did not re-pair the worker (finding, fixed the same day: rule "head restarted externally → worker re-paired") | `.runtime/drills/20260912T095548Z/drill-5-*.log`, controller log 09:55–10:09Z |
| queued request: accepted (200), status line shown, row `queued`, `llm_queued_generations` > 0 | **yes** — the line `Main model is recovering—your request is safely queued.` was read; the drill's own grep failed on an ASCII-escaped em-dash (script bug, fixed) | drill 15 record |
| queued request resumed after READY, same generation, primary only | **yes**: the same generation completed (143 chars), first token after READY, `engine=primary` | drill 15 record |
| duplicate-final-answer check (drill 14) | **one assistant message** for one intent sent twice and resumed once | drill 14 record |
| `llm_queued_generations` drains after the resume | **no** — stayed 1 until the next `/health` refresh (finding, fixed the same day) | drill 15 record |
| readiness sequence and participation | non-stream ✓ stream ✓ progress 64 tokens ✓; two concurrent 200–215-token probes; **head 94 % / worker 92–93 %** on every start today (first B start, three drill-16 starts, drills 3/4/5) | controller log, `.signals.gpus` |
| cold start on candidate B | first (cold torch.compile 20.6 s, graph capture 11 s, init 61 s): container start 08:39:59Z → API accepting ≈ 08:43:45Z (**≈ 3 m 46 s**); warm-cache restarts reached READY 172–188 s after the request | `docker logs sf-local-ai-vllm-1`, drill records |
| A/B matrix, 11 phases, candidate B | **0 failed requests, 0 Xid, 0 restarts**; ~950K needle 3/3 (949,9xx tokens, 1,189 tok/s prefill, 803 s); mixed 10 min at c=10: 692 requests vs 550 on A; c16 306.7 vs 269.6 tok/s; c10 TTFT p95 0.32 vs 0.60 s; 32K prefill 8.6K vs 7.8K tok/s; 128K 5.2K vs 5.0K tok/s; one 2.8 s inter-token stall in c10 on B | `docs/availability/ab/compare-A-vs-B-20260912.md` |
| 120-min soak on candidate B | see §5.1 (filled in when the soak completes) | `scripts/cluster-soak.py` |
| 48–72 h canary | **not measured — still unproven** | production run after the merge |

## 6. Acceptance checklist

The assignment's acceptance criteria for the v2 programme. Each status is what the evidence of
2026-09-12 supports — **met**, **met with note**, or **not measured** — with the record (a drill
record under `.runtime/drills/<utc>/` in the deploy checkout, a Prometheus query, a test run with
counts and exit code). An item without evidence is not accepted. Statuses set 2026-09-12 ≈ 11:00Z
by the docs workstream from the records named; the soak and the canary are still running.

| # | Criterion | Evidence expected | Status |
|---|---|---|---|
| 1 | Only `nvidia/Qwen3.6-35B-A3B-NVFP4` ever answers a person: `orchestrator/app/fallback.py`, `FALLBACK_*`, `llm_fallback_active` and the two fallback alerts are deleted; `usage_events.meta->>'engine'` is `primary` on every attempt | `grep` over `orchestrator/app`, `.env.example`, rules; the `ANOTHER MODEL` count of §3 = 0 | **met with note** — `grep -rn 'fallback' orchestrator/app` finds no answer path and no `fallback.py`; no `FALLBACK_*` key in `.env.example` (only the comment at line 635 saying there is none); no `llm_fallback_active` in `monitoring/prometheus/rules/`; the drills' resumed answers name no other engine (the terminal frame carries no `engine` field — `drill-5-chat-{a,b}.json` record `null`, and the assertion treats absence as the primary). The `ANOTHER MODEL` SQL was not run today |
| 2 | A request accepted during a recovery is persisted (`queued`), the person reads exactly `Main model is recovering—your request is safely queued.`, the stream heartbeats | drill 5 / 15 record; `llm_queued_generations` > 0 during the drill | **met** — `.runtime/drills/20260912T104624Z/drill-5-kill-head-api.log`: both chats HTTP 200, `the person read the queued sentence` PASS, `llm_queued_generations` 1 at 10:46:45Z (Prometheus); first run `20260912T095548Z` read the same sentence (its grep FAIL was an escaped em-dash, fixed in `6a667f6`) |
| 3 | The same logical generation resumes exactly once when READY returns (`attempt` + 1, `retry_reason=recovery`); `DUPLICATES` and `LOST` return no rows | drill 14 + 15 records; `llm_resumed_generations_total{outcome="resumed"}` +1, `duplicate_suppressed` for the second client | **met with note** — drill 14: one assistant row for the intent sent twice (both runs); drill 15: the same generation completed after READY, first token after READY; `llm_resumed_generations_total{outcome="resumed"}` = 1 after each run (Prometheus 10:09Z and 10:52Z), `llm_queue_wait_seconds` count 1 / sum 171.2 s after the re-run. `duplicate_suppressed` was not observed (the second client re-attached to the live generation instead); the `DUPLICATES` and `LOST` SQL were not run today |
| 4 | A wait that outlives `LLM_QUEUE_MAX_WAIT_S` leaves the row `queued` with the second truthful line and the resume sweep picks it up on READY / orchestrator start-up | drill record with a forced long recovery; `outcome="expired"` = 0 while the primary is not DOWN | **not measured** — no wait outlived 900 s today (the longest was 779.9 s in the first drill-5 run); the second line and the expired path are covered by `orchestrator/tests/test_continuity.py` only |
| 5 | Exactly one recovery authority: during drills 6 and 7 only the controller restarts the pair (healthchecks report-only; sentinel acts only on `POST /restart`; `SENTINEL_AUTONOMOUS=0`) | drill 6 / 7 records: one head restart, one worker restart, `RestartCount` deltas | **met with note** — drill 6 (`20260912T104332Z`): an operator's bare `docker restart` of the head; the controller re-paired the worker through the sentinel in 12 s, no second head restart (head RestartCount 0, worker 0), READY 165 s; drills 3/4: `RestartCount` 0 → 0 on both containers with `StartedAt` moved once each (the controller's own restarts do not increment Docker's counter). Drill 7 (worker-only restart) was not run |
| 6 | READY is preceded by the §5 v2 readiness sequence (non-streaming, streaming, token progress, both GPUs > 30 %, rank alive); `techsara_vllm_participation_ok == 1` | drill 3–5 records; `/state .signals.gpus` | **met** — controller `/state` after every start today: `readiness.passed=true`, two participation probes (≈ 200 tokens each), `.signals.gpus` head 94 % / worker 92 % (10:49Z), `techsara_vllm_participation_ok == 1`; the verify probe saw both GPUs 93–94 % / 90–93 % in every drill record |
| 7 | Rank death → state ≠ READY ≤ 30 s; dead rank reaped ≤ 10 s; head teardown → restart ≤ 15 s; fault → READY ≤ 6 min warm | drill 3, 4, 5 records | **met with note** — rank death → RECOVERING **6 s** (drill 3); EngineCore death **4 s** (drill 4); head API death seen in **5 s** (drill 5, both runs; Docker restarted the head in 5 s so `head_api_dead` did not need to fire); fault → READY **176 / 172 / 161 s**; three coordinated restarts 188 / 186 / 180 s (drill 16). The dead rank's reap was measured indirectly: the worker container restarted 7 s after the `kill -9` (09:45:46 → 09:45:53); no apport hold was observed. The head-teardown → restart objective (≤ 15 s) was not exercised: the API process exits on `kill -9`, it does not hang in teardown |
| 8 | Zero false-UP minutes and zero false-DOWN minutes from missing data in drills 1 and 2; `VllmReadyUnproven` fires on a frozen controller snapshot (promtool case) | drill 1 / 2 records; `promtool test rules` output | **not measured** live — drills 1 and 2 were not run today. `promtool test rules` on `monitoring/prometheus/tests/{alerts,recording}_availability.yml`: SUCCESS, SUCCESS (46 + 21 test groups, 88 + 42 eval points; `docker run --entrypoint promtool prom/prometheus:latest`, exit 0, 2026-09-12 ≈ 10:58Z); `promtool check rules`: 49 + 36 rules, exit 0. The controller's WEDGED verdict under the ~950K needle (INCIDENT §7.2) was not a false-UP but a false-not-serving for 11 min 45 s while the engine was prefilling |
| 9 | A recovery reuses the compiled-kernel caches (no `torch._dynamo` / `flashinfer.jit` / `nvcc` recompile in the head log); `--clear-kernel-cache` empties both volumes under the lock and the next start recompiles once | `techsara_vllm_cold_start_seconds` before/after; head log grep | **met with note** — reuse proven: head log `torch.compile took 20.56 s` and `init engine … 61.28 s` on the first B start (`.runtime/incidents/20260912T084520Z/head-logs-1.txt`) vs **4.96 / 3.27 / 4.94 s** and **27.2 / 28.8 / 26.7 s** on the drill-16 and drill-3 restarts (`…085030Z`, `…085538Z`, `…095052Z` head logs), FlashInfer autotuner `Config cache hit`; `techsara_vllm_cold_start_seconds` 166 / 166 / 161 / 162 / 156 / 157 / 152 s across the day (428 s = the drill-5 first run). `--clear-kernel-cache` was not exercised today |
| 10 | Admission lanes: a prompt > 131,072 tokens waits for `requests_running ≤ 0`, runs alone, and the person reads the `Waiting for the model…` line; `llm_admission_*` populated | a two-request test (one long, one normal) with timestamps | **not measured** — no two-request lane test was run; the ~950K needle went through the raw port (the A/B harness), not the orchestrator. `llm_admission_lane_active{lane="normal"}` = 0, no `llm_admission_rejections_total` series at 10:52Z |
| 11 | Budget and cooldown apply to manual recoveries; `POST /recover` past the budget is refused synchronously; `scripts/cluster-recover.sh` reports the outcome of *its* recovery (waits for `in_progress`) | controller tests + a manual run record | **met with note** — drill 16 spent the budget with three manual `POST /recover` (`budget remaining after the drill: 0` at 08:58:39Z); the 09:27Z drill attempts were refused by the drill script's precondition (`0 left of 3`), and the controller refused its own automatic attempt at ≈09:05Z (`techsara_vllm_recovery_attempts_total{outcome="budget_exhausted"}` 0 → 1). `scripts/cluster-recover.sh` waited for the controller to take each request (`queued at the controller (manual_pending…)` → `the controller took the request`). A synchronous 409/429 on `POST /recover` past the budget was not exercised live (controller tests: 125 passed, 75 s, `python3 -m pytest monitoring/engine-controller/tests -q`, exit 0) |
| 12 | Candidate B (post-`f6326f5`, `--gdn-prefill-backend flashinfer`) evaluated first with `scripts/cluster-ab.py`: 120-min soak pass per research §6.4, needles 3/3, 262K prefill ≤ 1.3×; then the secondary tests one at a time (Track A `flashinfer_b12x` fifth) | `CANDIDATE-B.md` results table | **met in part** — the A/B matrix is measured (`docs/availability/ab/compare-A-vs-B-20260912.md`): B 0 failed / 0 Xid / 0 restarts over 11 phases, needle 3/3 at 949,9xx tokens, `prefill_128k` TTFT p50 25.0 vs 26.3 s (0.95 ×, criterion ≤ 1.3 ×), c10/c16 TTFT p95 0.53 × / 0.55 × A, `mixed` 692 vs 550 requests; A's 950K figure not measured (INCIDENT §7.1). The 120-min soak is **in progress** (started 10:50Z); the `validate_long_context.py` run and the orchestrator smoke on B are not recorded; the secondary tests are not run |
| 13 | Test suites green with counts: controller (`monitoring/engine-controller/tests`), orchestrator availability + `test_generation_durability.py` (13), launcher, `promtool check`/`test rules`, `shellcheck` on every changed script | the lead's summary with commands, counts, exit codes | **met in part** — run by the docs workstream 2026-09-12: controller `python3 -m pytest monitoring/engine-controller/tests -q` → **125 passed**, exit 0; `promtool check rules` → 49 + 36 rules, exit 0; `promtool test rules` → SUCCESS × 2, exit 0. Orchestrator availability + durability, launcher and `shellcheck` counts are the lead's (not run here; `shellcheck` is not installed on the head) |
| 14 | Every `Vllm*` alert's `runbook:` annotation resolves to an anchor in `RUNBOOK.md` §13 (`#primary-down`, `#ready-unproven`, `#requests-queued`, …) | the link check of the docs workstream + `grep -o 'RUNBOOK.md#[a-z-]*' monitoring/prometheus/rules/alerts.yml` | **met** — `grep -o 'RUNBOOK.md#[a-z-]*' monitoring/prometheus/rules/alerts.yml`: 18 distinct anchors, every one present as `<a id="…">` in `RUNBOOK.md` §17 (the 19th hit, `#anchor`, is in a comment); relative links across `docs/availability/` checked by script, 0 broken (2026-09-12 ≈ 11:00Z) |
| 15 | ≥ 7 days of production with no controller incident of category ≠ `none` before the fault class is called closed | `techsara_vllm_last_failure_category` history | **not measured — still unproven.** Candidate B has served since 08:39Z (2 h at the time of writing); the soak is running; the 48–72 h canary has not started. Today's `last_failure_category` history: `manual` × 3 (drill 16), `budget_exhausted` (the needle, INCIDENT §7.2), `worker_rank_dead`, `head_engine_dead`, `head_restarted_externally` × 2 — all drills |
