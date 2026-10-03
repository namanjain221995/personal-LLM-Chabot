# Rollout and rollback

The programme's release target is `dev` (operator decision 2). The operator releases `dev → main`; production rollout and rollback stay with the operator's existing mechanisms.

## The programme's release into `dev`

1. Definition of Done (§28) met; all tests, evaluations and the independent review pass; `FINAL_REPORT.md` written.
2. Merge the latest `origin/dev` into `autopilot/dev`, resolve conflicts, re-run everything, push `autopilot/dev`.
3. Wait for the draft PR's CI run on that exact commit.
4. Run `~/.llm-autopilot/bin/merge_to_dev.sh --dry-run`, then without `--dry-run`. It fast-forwards `origin/dev` only when every check on the commit passed and `FINAL_REPORT.md` exists.
5. Record the result in `DEPLOY_LOG.md`.

Rollback of `dev`: a revert commit on `autopilot/dev`, through the same gate (never a force push).

Thresholds for health, canaries per mode, error rate and p95 are defined here before the final release (Phase G), for the operator's `dev → main` release.

## Production mechanisms (operator-owned, for reference)

- Rollout: a merge to `main` runs the Pipeline; its `deploy` job runs `scripts/deploy.sh --ref $GITHUB_SHA` (rolling, keeps the main model running) and `verify` checks health and a real completion.
- Automatic rollback: `deploy.sh` rolls back when its recorded reversibility verdict allows; it restores images, never the database.
- Manual rollback: `scripts/deploy-rollback.sh --list`, `--to <release> --dry-run`, `--yes`.

## Low-traffic window (B-05, measured 2026-10-03)

For promotion gate 9 and the quiet window the autopilot guard applies to heavy harness runs. Read-only GETs to the production Prometheus query API; aggregate counters only, no request bodies or user identities. Hourly `query_range` (step 1 h) over all retained data, 2026-09-18 12:00 to 2026-10-03 22:00 IST; each point is the preceding IST hour:

- user chat turns: `sum(increase(chat_request_total{result="accepted"}[1h]))`
- engine completions: `sum(increase(vllm:request_success_total{job="vllm-main"}[1h]))` minus the health prober's real completions, `sum(increase(techsara_vllm_synthetic_probes_total{outcome="ok"}[1h]))`
- `/v1` API load: `sum by (origin) (increase(llm_admission_grants_total[1h]))`; in flight: `max(max_over_time(vllm:num_requests_running{job="vllm-main"}[1h]))`
- coverage: `count_over_time(up[1h])` per job; counter restarts: `resets(...[1h])`

Mean is over the days with data for that hour; p95 is the linear-interpolated 95th percentile over those days; max in flight is the highest 5-second sample. Sample: 317 of 370 hourly buckets (about 13.2 days; 13–14 values per hour of day, 2–4 of them weekend). A 53-hour ingestion gap (2026-09-19 13:00 to 2026-09-21 18:00 IST) is excluded, not counted as zero, and so are two hours with the main engine down (2026-09-25 02:00–04:00 IST, engine columns only); `increase()` absorbs the counter resets of restarts (11 orchestrator hours, 1 engine hour). Totals: 625 chat turns, 171 `/v1` admission grants, 45,746 engine completions, peak 33 in flight.

| Hour (IST) | Chat turns/h mean | p95 | Engine completions/h mean | p95 | Max in flight |
|---|---|---|---|---|---|
| 00–01 | 2.7 | 14 | 320 | 1021 | 23 |
| 01–02 | 3.5 | 14 | 200 | 893 | 26 |
| 02–03 | 3.0 | 16 | 357 | 1333 | 12 |
| 03–04 | 4.9 | 27 | 335 | 1336 | 14 |
| 04–05 | 2.0 | 10 | 125 | 670 | 13 |
| 05–06 | 0.1 | 0.4 | 66 | 361 | 20 |
| 06–07 | 0 | 0 | 3.3 | 18 | 1 |
| 07–08 | 1.3 | 6.8 | 17 | 90 | 3 |
| 08–09 | 3.6 | 17 | 41 | 165 | 3 |
| 09–10 | 1.1 | 5.0 | 106 | 516 | 3 |
| 10–11 | 1.3 | 7.4 | 59 | 292 | 4 |
| 11–12 | 0.2 | 0.8 | 94 | 455 | 4 |
| 12–13 | 0.6 | 3.4 | 97 | 505 | 6 |
| 13–14 | 2.4 | 15 | 19 | 94 | 3 |
| 14–15 | 2.7 | 14 | 39 | 207 | 1 |
| 15–16 | 1.7 | 11 | 78 | 430 | 13 |
| 16–17 | 0.7 | 3.6 | 173 | 914 | 22 |
| 17–18 | 0.5 | 2.8 | 62 | 331 | 17 |
| 18–19 | 0.8 | 3.9 | 52 | 288 | 10 |
| 19–20 | 3.7 | 18 | 72 | 310 | 10 |
| 20–21 | 3.1 | 12 | 347 | 1190 | 23 |
| 21–22 | 1.9 | 7.7 | 299 | 880 | 33 |
| 22–23 | 2.9 | 13 | 253 | 842 | 25 |
| 23–00 | 2.5 | 10 | 292 | 1036 | 24 |

Weekdays carry most turns, concentrated 19:00–04:00 IST with a bump at 08:00; 05:00–08:00 saw one turn in nine weekdays. Weekends (2–4 days per hour) are too few to rank alone: most of their turns fell at 00:00–02:00, 07:00–09:00 and 13:00–17:00, none at 05:00–07:00 or 17:00–24:00. `/v1` API traffic is negligible (171 grants, none between 04:00 and 12:00).

Result (rule: rank by mean chat turns; windows within 10 % are tied and the lower engine load wins):

- **Quietest 2 h: 05:00–07:00 IST.** 0.08 chat turns per window on average: one turn on one of 13 days, none at 06:00–07:00 on any day. This agrees with the Phase 0 estimate in `CURRENT_STATE.md` and with the guard's configured quiet window; no change recommended.
- **Quietest 4 h: 04:00–08:00 IST.** 3.4 chat turns per window against 3.2 for 09:00–13:00 (tied); engine load decides it (211 against 356 completions per window).
- 05:00–06:00 sometimes carries the tail of overnight background engine load (over 50 completions on 3 of 13 days, up to 20 in flight); 06:00–07:00 is the only hour quiet on every measure. Engine-heavy work should start at 06:00 or check in-flight first; gate 9's drain check applies in any case.

Limitations: about 625 user turns in 13 days, so single busy days dominate the means and p95 (the 03:00 p95 comes from one day). Retention is 15 days and 53 hours are missing, so this is 13 days, not 28. `increase()` extrapolates, and a counter created by its first increment after a restart misses that count (at most about 11 over the period). The probe subtraction is approximate. Re-measure before the final release or after traffic changes.
