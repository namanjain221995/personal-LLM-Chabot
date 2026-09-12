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
| rank death → RECOVERING | **not measured** | drill 3 record |
| dead rank reaped | **not measured** | drill 3 record (`worker.started_at` − `last_fault.at`) |
| fault → READY (warm / cold) | **not measured** | drills 3–5, `techsara_vllm_recovery_duration_seconds` |
| queued request: status line seen, row `queued` | **not measured** | drill 5 / 15 record |
| queued request resumed after READY, same generation | **not measured** | drill 15 record, `llm_resumed_generations_total` |
| duplicate-final-answer check (drill 14) | **not measured** | drill 14 record + the `DUPLICATES` SQL |
| readiness sequence duration and participation (both GPUs) | **not measured** | `techsara_vllm_participation_ok`, `.signals.gpus` |
| cold start with warm kernel cache vs cleared cache | **not measured** | `techsara_vllm_cold_start_seconds` before/after `--clear-kernel-cache` |
| 120-min soak on the pinned build | **not measured** | `scripts/cluster-soak.py` |

## 6. Acceptance checklist

The assignment's acceptance criteria for the v2 programme, each marked **not yet measured**
until the lead fills in the evidence (a drill record under `.runtime/drills/<utc>/`, a soak
record, a Prometheus query, or a test run with counts and exit code). An item without evidence
is not accepted.

| # | Criterion | Evidence expected | Status |
|---|---|---|---|
| 1 | Only `nvidia/Qwen3.6-35B-A3B-NVFP4` ever answers a person: `orchestrator/app/fallback.py`, `FALLBACK_*`, `llm_fallback_active` and the two fallback alerts are deleted; `usage_events.meta->>'engine'` is `primary` on every attempt | `grep` over `orchestrator/app`, `.env.example`, rules; the `ANOTHER MODEL` count of §3 = 0 | **not yet measured** |
| 2 | A request accepted during a recovery is persisted (`queued`), the person reads exactly `Main model is recovering—your request is safely queued.`, the stream heartbeats | drill 5 / 15 record; `llm_queued_generations` > 0 during the drill | **not yet measured** |
| 3 | The same logical generation resumes exactly once when READY returns (`attempt` + 1, `retry_reason=recovery`); `DUPLICATES` and `LOST` return no rows | drill 14 + 15 records; `llm_resumed_generations_total{outcome="resumed"}` +1, `duplicate_suppressed` for the second client | **not yet measured** |
| 4 | A wait that outlives `LLM_QUEUE_MAX_WAIT_S` leaves the row `queued` with the second truthful line and the resume sweep picks it up on READY / orchestrator start-up | drill record with a forced long recovery; `outcome="expired"` = 0 while the primary is not DOWN | **not yet measured** |
| 5 | Exactly one recovery authority: during drills 6 and 7 only the controller restarts the pair (healthchecks report-only; sentinel acts only on `POST /restart`; `SENTINEL_AUTONOMOUS=0`) | drill 6 / 7 records: one head restart, one worker restart, `RestartCount` deltas | **not yet measured** |
| 6 | READY is preceded by the §5 v2 readiness sequence (non-streaming, streaming, token progress, both GPUs > 30 %, rank alive); `techsara_vllm_participation_ok == 1` | drill 3–5 records; `/state .signals.gpus` | **not yet measured** |
| 7 | Rank death → state ≠ READY ≤ 30 s; dead rank reaped ≤ 10 s; head teardown → restart ≤ 15 s; fault → READY ≤ 6 min warm | drill 3, 4, 5 records | **not yet measured** |
| 8 | Zero false-UP minutes and zero false-DOWN minutes from missing data in drills 1 and 2; `VllmReadyUnproven` fires on a frozen controller snapshot (promtool case) | drill 1 / 2 records; `promtool test rules` output | **not yet measured** |
| 9 | A recovery reuses the compiled-kernel caches (no `torch._dynamo` / `flashinfer.jit` / `nvcc` recompile in the head log); `--clear-kernel-cache` empties both volumes under the lock and the next start recompiles once | `techsara_vllm_cold_start_seconds` before/after; head log grep | **not yet measured** |
| 10 | Admission lanes: a prompt > 131,072 tokens waits for `requests_running ≤ 0`, runs alone, and the person reads the `Waiting for the model…` line; `llm_admission_*` populated | a two-request test (one long, one normal) with timestamps | **not yet measured** |
| 11 | Budget and cooldown apply to manual recoveries; `POST /recover` past the budget is refused synchronously; `scripts/cluster-recover.sh` reports the outcome of *its* recovery (waits for `in_progress`) | controller tests + a manual run record | **not yet measured** |
| 12 | Candidate B (post-`f6326f5`, `--gdn-prefill-backend flashinfer`) evaluated first with `scripts/cluster-ab.py`: 120-min soak pass per research §6.4, needles 3/3, 262K prefill ≤ 1.3×; then the secondary tests one at a time (Track A `flashinfer_b12x` fifth) | `CANDIDATE-B.md` results table | **not yet measured** |
| 13 | Test suites green with counts: controller (`monitoring/engine-controller/tests`), orchestrator availability + `test_generation_durability.py` (13), launcher, `promtool check`/`test rules`, `shellcheck` on every changed script | the lead's summary with commands, counts, exit codes | **not yet measured** |
| 14 | Every `Vllm*` alert's `runbook:` annotation resolves to an anchor in `RUNBOOK.md` §13 (`#primary-down`, `#ready-unproven`, `#requests-queued`, …) | the link check of the docs workstream + `grep -o 'RUNBOOK.md#[a-z-]*' monitoring/prometheus/rules/alerts.yml` | **not yet measured** |
| 15 | ≥ 7 days of production with no controller incident of category ≠ `none` before the fault class is called closed | `techsara_vllm_last_failure_category` history | **not yet measured** |
