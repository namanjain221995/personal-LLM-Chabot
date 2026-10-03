# Benchmark results

STATUS: the measurement method and the tolerances are frozen (B-04a, cycle 9, 2026-10-04). The baseline NUMBERS are not measured yet: the evaluation run belongs in the 05:00–07:00 IST low-traffic window (B-04b). Until B-04b writes them below, no optimisation can be compared, and Phase C must not start.

## What is measured

- **Cases.** `scripts/aiq/eval_set.py`, the 16 synthetic cases of B-02 (§11's nine situations, §13's seven request-understanding traps). Each run records the set's sha256; runs of different sets never pool and never compare.
- **Runner.** `scripts/aiq/run_evalset.py` sends each case to an orchestrator over HTTP as the browser does (sign-in, `/uploads` for attachments, `/chat` with the seeded history, effort, web search and deep-research flags), follows the SSE stream, reads the per-generation trace (`/chat/trace/{id}`), scores every turn with the B-02 checks and writes `results.json` (schema 1).
- **Statistics.** `scripts/aiq/baseline.py freeze` pools three or more run directories into `baseline.json` plus the markdown tables for this file; `baseline.py compare` checks a candidate run against a frozen baseline and exits 1 when it does not pass (2 for unusable input).
- **Per turn.** Client clock: first SSE event (the server's acceptance), first token event, first non-blank answer token, stream end. Server clock, from the trace: routing (MODE_RESOLVED), retrieval pre-pass (KNOWLEDGE_PREPARED), prompt preparation (CONTEXT_ASSEMBLED), admission (MODEL_PROMPT_PREPARED, MODEL_DISPATCHED), prefill (MODEL_FIRST_CHUNK), generation (MODEL_STREAM_ENDED), FIRST_ANSWER_TOKEN, rerank. Token usage as the serving engine reported it for the first eight traced main-model calls. Model and application versions from the trace.
- **Workload classes (§24).**

  | Class | Cases |
  |---|---|
  | direct_fast | EV01 EV02 EV03 RQ01 RQ03 RQ05 RQ06 RQ07 |
  | evidence_fast (uploaded document) | EV05 RQ02 |
  | live_search_fast (web search auto) | EV04 RQ04 |
  | long_context (Fast, about 52K tokens of history) | EV07 |
  | think | EV06 EV08 |
  | max (deep research, web search on) | EV09 |

- **Not covered by this baseline.** Large-document jobs (no case yet), prefill throughput at 128K–1M tokens (Phase E, its own window), single-agent versus multi-agent comparisons (Phase F), the browser and its proxies (the runner talks to the orchestrator directly), per-user output speed under concurrency.

## Conditions every number carries

- The dev stack `llmdev` on the worker node (`ops/dev/README.md`, AD-010): the orchestrator's CPU image, a fresh database, synthetic accounts.
- Production's main engine (`Qwen/Qwen3.6-35B-A3B-NVFP4`), shared with production users through the dev cap: at most two dev requests in flight, no priority. Other users' load is part of the noise.
- Router, agent and vision calls answered by the main model; embeddings, reranking and web search off (NH-013). So the live-search and Max cases measure the path without a search engine, and document questions run without semantic retrieval.
- `--workers 1`. One fresh account per run directory (the runner refuses an account that already has conversations or saved facts), because cross-chat recall and saved facts would otherwise feed one repeat's answers into the next.

## Expected failures in the baseline

These fail for reasons the baseline must record, not hide. Loosening a check to make them pass is not allowed.

- EV04, RQ04, EV09 need read sources; the dev stack has no search engine.
- EV09 `citation_passages` cannot pass: no endpoint exposes the text of the passages a run read (backlog P1).
- EV09 `citations` also fails on deep-research answers because the deep-research sources panel carries no `read` flag (backlog P1).

## Tolerance method (frozen)

`baseline.py` writes the full rule text into every baseline (`method`); this is the summary.

**Quality.** Case pass = every check passed and no error; case score = the fraction of checks passed (errors score 0). A candidate fails when:

- a case's pass rate falls below the baseline's by more than one repeat (rate < baseline − 1/min(baseline repeats, candidate repeats), exact fractions);
- the same holds for any single check of a case, so a case that never fully passes (EV09) is still gated check by check; a check the baseline ran only in some records (for example `job_completed`, emitted only when a file was made) is gated on its failure rate instead;
- the overall mean case score drops by more than 0.05;
- Fast turns that reasoned (`thinking_off` failed) occur at a higher rate than in the baseline, or a Fast turn lacks that check;
- a case fails more turns (errors, HTTP failures, time-outs, streams without `done`) than the baseline by more than one repeat;
- a baseline case is missing from the candidate.

**Latency.** Unit = one turn of one case. Gated: first answer token and stream end in every class, and first SSE event (the UI acknowledgment of §24) in the Fast classes; first token is reported only. Failed turns are excluded and counted.

- Noise σ = repeat-to-repeat log standard deviation of the same unit, pooled over the units of the class; the σ used is the larger of the class's own and the one pooled over its effort family (Fast classes, or Think and Max), so a class with one case does not estimate its noise from three samples, and a noisy Max class does not loosen the Fast tolerances.
- Statistic: the geometric mean over units of (candidate median ÷ baseline median). Allowed: 1 + max(0.20, 2σ) + abs ÷ scale, where scale is the geometric mean of the baseline unit medians and abs is 0.25 s (direct_fast), 0.5 s (evidence_fast), 1.0 s (live_search_fast, long_context), 2.0 s (think), 10.0 s (max), doubled for the stream end.
- p95 gates only with at least 20 samples on both sides; below that it is the largest sample and is reported only.
- A class needs at least 3 samples per side and one baseline unit with two samples; otherwise it is `insufficient_samples`, which never counts as a pass.
- Expected false blocks and power, from a Monte Carlo of this exact freeze and compare (400 trials a row, the 16 cases at plausible medians, lognormal repeat noise σ, "spikes" = 10 % of requests slowed 1.5–3× by other load; latency gates only, quality held passing):

  | Setup | Unchanged build blocked | 1.3× slower blocked | 1.5× slower blocked |
  |---|---|---|---|
  | 3 vs 3 runs, σ 0.10 | 3.0 % | 99.75 % | 100 % |
  | 3 vs 3 runs, σ 0.15 | 8.5 % | 96.5 % | 100 % |
  | 3 vs 3 runs, σ 0.15, spikes | 19.25 % | 75.25 % | 97 % |
  | 3 vs 3 runs, σ 0.30, spikes | 23.5 % | 72.25 % | 94.5 % |
  | 5 vs 3 runs, σ 0.15 | 4.75 % | 95 % | 100 % |
  | 5 vs 3 runs, σ 0.15, spikes | 17.5 % | 62 % | 93.75 % |

  With Fast noise lower than Think/Max noise (σ 0.08–0.15 against 0.35–0.60, 300 trials a cell), an unchanged build is blocked in 11–17 % of comparisons and a Fast-only 1.3× slowdown in 87–99 % (54–57 % with spikes). Pooling σ over every class instead caught that slowdown in only 35–70 %; a per-class σ alone blocked unchanged builds in 13 % and, with spikes, 33–42 %. So a latency pass in a spiky window is weak evidence, and five baseline runs are better than three.
- When a latency gate fails, re-run the BASELINE commit in the same window as a tie-break: if it fails the same gate, the window is slow and the latency verdict is void.

**Refusals (exit 2).** Fewer than 3 records for a case; an unfinished, interrupted or deadline-cut run; record counts that do not match the runs' repeats; duplicate conversation ids; different eval-set hashes or worker counts; labels or run names with URLs, addresses, host names or invisible characters (the baseline is committed to this public repository); a stored baseline whose statistics do not recompute from its own samples.

## Procedure for B-04b (the baseline run)

1. Inside the window only: `--not-before 05:00 --deadline 07:00`. Each run includes the engine-heavy cases (EV07's 52K-token prompt, EV09's deep research), and the dev README keeps engine-heavy work to 06:00–07:00, the only hour quiet on every B-05 measure: start before 06:00 only while the main engine reports no other requests (`vllm:num_requests_running` 0), otherwise at 06:00 with three runs.
2. Per run, seed a fresh account (`ops/dev/devstack.sh seed <name>`, password on stdin from a gitignored 0600 file) and run all 16 cases with `--repeats 1 --workers 1` and the conditions above as `--label`. Five runs where the window allows (fewer false blocks), never fewer than three.
3. `freeze` refuses what cannot be compared (see Refusals) and records, without refusing, any deviation from this procedure (more than one repeat per run, more than one worker, an unchecked or used account) at the top of its markdown.
4. `baseline.py freeze <run dirs> --out scripts/aiq/runs/evalset-baseline-<date>/baseline.json --markdown <scratch>.md`; commit `baseline.json` (it holds counts, timings and check names only) and paste the tables below.

## Baseline

Not measured yet (B-04b).

## Smoke run (not a baseline)

2026-10-04 02:30 IST, outside the window, one run, two short Fast cases, to prove the runner against the real dev stack; the main engine had 0 requests running or waiting just before. EV01 (greeting) and RQ03 (names only) passed every check; first answer token 0.37 s and 0.60 s, stream end 0.92 s and 1.05 s; prompt 178 and 2,416 tokens, completion 16 and 12; the trace's model id `Qwen/Qwen3.6-35B-A3B-NVFP4`. A second smoke after the final fixes (c4fbcca6's runner, 03:25 IST, a second fresh account): both cases passed again, first answer token 0.13 s and 0.46 s, stream end 0.50 s and 0.84 s. On the same tree, `--not-before 05:00` refused to start at 03:25 (exit 2, no run directory) and `baseline.py freeze` of the two smoke runs refused with `cases with fewer than 3 records cannot be frozen (EV01: 2, RQ03: 2); pool more runs` (exit 2). One or two samples each: these numbers support no comparison.
