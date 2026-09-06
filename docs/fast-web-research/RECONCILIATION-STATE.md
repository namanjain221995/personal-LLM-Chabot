# Reconciliation — what the previous reports claimed vs. what is actually true

Read-only verification pass, 2026-09-07 ~01:50 IST. Every prior report was treated as an
unverified claim. Nothing was fixed, nothing was restarted, no test suite was run, no source
file was modified. This file is the only thing written.

**Live conditions during this pass, recorded because they affect what "actual" means:**
`techsara_cli up` was running (PID 3913879), the main vLLM head had just been recreated
(`sf-local-ai-vllm-1` started `2026-09-06T20:16:04Z`), two pytest runs were in flight
(PIDs 3931987, 3932444) and an `availability_probe` was mid-run (PID 3817795). The working
tree was being edited by another workstream *while this pass ran* — the modified-file count
moved 31 → 33 between two `git status` calls.

**States used:** `implemented` (code present in the tree) / `tested` (an automated test exists
and asserts it) / `applied` (present in the running system) / `measured` (a number backed by a
measurement artefact) / `blocked` / `NOT-DONE`.

| Requirement/Item | Claimed | Actual | Evidence | State |
| --- | --- | --- | --- | --- |
| **Git baseline unmoved** | WORKLOG "Final state": `dev` @ `29aa0ab`, 0 staged, 0 commits | True. HEAD `29aa0ab36a41549936dfce061b2333040ac66e1a`, 0 staged, 0/0 vs `origin/dev`, one worktree | `git rev-parse`, `git diff --cached --stat` (empty), `git rev-list --left-right --count origin/dev...HEAD` → `0 0`, `git worktree list` → 1 | applied |
| **Working-tree size** | WORKLOG "Final state": "18 tracked files modified, 8 untracked paths added… 48 files" | **Stale by a wide margin.** 33 modified, 25 untracked top-level paths (73 untracked files with `-uall`) | `git status --porcelain` → 33 ` M` lines; untracked incl. `scripts/cluster-bench.sh` (M), `orchestrator/tools/availability_probe.py`, `tools/reindex_incremental.py`, `tools/tail_facts.py`, `tools/structured_real_world.py` | NOT-DONE (claim superseded) |
| **`MANIFEST.sha256` fingerprints the delivered tree** | WORKLOG: "Content fingerprints of all 48 files in `MANIFEST.sha256`" | **Manifest is stale and has 77 entries, not 48.** 5 of 77 files no longer match | `sha256sum -c docs/fast-web-research/MANIFEST.sha256` → 72 OK, **FAILED**: `MANIFEST.sha256` itself, `orchestrator/app/db.py`, `orchestrator/app/web_index.py`, `tests/test_knowledge_extraction_eval.py`, `tests/test_reprocessing.py` | NOT-DONE |
| **Pre-existing stash left alone** | Not claimed | One stash exists, `WIP on dev: 179b2b1` — a *different* base commit, so pre-dates this phase | `git stash list` | applied (informational) |
| **Migrations V1–V27 defined in source** | DEPLOYMENT-PLAN / WORKLOG: V22, V23, V24, V25, V26, V27 added | True. `_MIGRATIONS` is a contiguous tuple `(1…27)`, `LATEST_SCHEMA_VERSION` derived from it | `orchestrator/app/db.py:1453-1480`; `_MIGRATION_V21:1151`, `V22:1190`, `V23:1208`, `V24:1232`, `V25:1262`, `V26:1345`, `V27:1420` | implemented |
| **Migrations applied in the running DB** | DEPLOYMENT-PLAN: schema 23 → 24 → 26 → 27 | True, and it matches the source exactly. Versions **1–27, no gaps, no extras** | `docker exec sf-local-ai-postgres-1 psql -U techsara -d techsara -tAc "select version from schema_migrations order by version"` → `1..27`; applied_at: 21=2026-09-05T04:59:59Z, 22/23=09-06T15:08:55Z, 24=16:40:23Z, 25/26=18:25:42Z, 27=19:26:10Z | applied |
| **Defined-but-not-applied migrations** | — | **None.** Every version in `_MIGRATIONS` is recorded in `schema_migrations` | set(source)=set(db)={1..27} | applied |
| **Applied-but-not-defined migrations** | FINDINGS: "V21 is an orphan the source cannot explain" | **No longer true.** V21 was restored verbatim into the tree and is now a defined migration | `db.py:1151` `_MIGRATION_V21` ("RESTORED HISTORY, NOT NEW WORK… recovered VERBATIM from 34a7e3c / 829f17c") | implemented |
| **"Identifier 21 is retired permanently. Fresh databases simply never have a 21; the tests assert that gap."** | `MIGRATION-RECONCILIATION.md` §"The reconciliation" | **FALSE — the doc contradicts the shipped code.** V21 is registered as `(21, _MIGRATION_V21)` and the test asserts a *contiguous, sorted, unique* version list, i.e. the opposite of a gap. No test asserts a missing 21 | `db.py:1474`; `orchestrator/tests/test_crawl_durability.py:744-755` `test_no_migration_identifier_is_used_twice` asserts `versions == sorted(versions)` and `LATEST_SCHEMA_VERSION == max(versions)`; `grep` for a gap assertion returns nothing | NOT-DONE (stale doc — the code is fine, the document is wrong) |
| **V22 index reconciliation** | Old version-literal index dropped, `idx_web_pages_extract_stale` created | True in the live DB | `select indexname from pg_indexes where tablename='web_pages'` → `idx_web_pages_extract_stale`, `idx_web_pages_chunk_stale` present; `idx_web_pages_extract_version` **absent** | applied |
| **V23 backfill: 1,602 stranded pages → 0** | DEPLOYMENT-PLAN verification table | Holds. **0 rows** with `next_refresh_at IS NULL`, across all three origins | `count(*) filter (where next_refresh_at is null)` → 0; by origin: search 1872/0, crawl 214/0, research 123/0 | applied |
| **V24/V27 chunker versioning, cap 64 → 256, `CHUNKER_VERSION` 3** | COVERAGE-DECISION "APPLIED 2026-09-07" | True in tree, image and data. **All 2,209 pages at `chunk_version = 3`**; both backlogs 0 | `app/web_index.py:71` `_MAX_CHUNKS_PER_PAGE = 256`, `:220` `CHUNKER_VERSION = 3`; `select chunk_version, count(*)` → `3 \| 2209`; `/health.web_index` → `chunker_version: 3, pending_pages: 0, rechunk_pending: 0` | applied |
| **Index size after the cap raise (~19,887 rows)** | COVERAGE-DECISION: "16,015 → 19,887 rows (projection said 19,875)" | Consistent. Live index now **19,895 rows / 2,064 distinct pages** (8 more, from refresh traffic since) | `/health` read from inside the container; LanceDB sidecar `_techsara_embedding_index.json` → `{"chunker_version": 3, "dimension": 1024}`; store is 274 MB | applied / measured |
| **`EXTRACT_VERSION = 4` and the re-extraction backlog drains gradually** | DEPLOYMENT-PLAN 2nd deploy: "Progress so far: 12 of 2,208" | True and progressing, but **still 95% undrained**. 2,011 pages at 0, 86 at 3, **112 at 4** — 2,097 of 2,209 below current | `app/core/extract.py:55` `EXTRACT_VERSION = 4`; `select extract_version, count(*) from web_pages` | applied (backlog live) |
| **`web_crawl_frontier` (V25) durable crawl frontier** | FINDINGS "Crawl durability round" | Table exists and is empty — **the feature has never been exercised in production** | `select count(*) from web_crawl_frontier` → **0 rows** | applied / NOT-DONE (unexercised) |
| **V26 `last_changed_at` repair, predicate fully applied** | DEPLOYMENT-PLAN 3rd deploy: predicate matches 0 rows afterwards; NULLs 1,509 → 1,507 "live drift" | Consistent and still drifting the same direction: **1,489 NULLs** now | `count(*) filter (where last_changed_at is null)` → 1489 | applied |
| **R5 — no research run stuck at `running`** | WORKLOG: "closes only running rows… idempotent on restart" | Holds live: **0 of 19 runs in `running`**, across at least three orchestrator recreations | `select count(*) from research_runs where status='running'` → 0; `close_interrupted_research_runs` present in `app/db.py` and `app/main.py` | applied |
| **Production data unchanged by the deploys** | DEPLOYMENT-PLAN: 450 conv / 1,856 msg / 2,208 pages / 138 uploads | Intact, with normal live growth: **452 / 1,864 / 2,209 / 138** | single `psql` aggregate query | applied |
| **Deployed orchestrator image carries the tree's changes** | DEPLOYMENT-PLAN: "the exact deployed code" | **True, and stronger than claimed: byte-identical.** All 120 `app/**/*.py` files hash-match between the working tree and `/app/app` in the running container | `find app -name '*.py' \| xargs sha256sum` on both sides, sorted → `diff` empty. Only earlier apparent differences were `sort` locale ordering | applied |
| **Image is not older than the tree changes** | — | Image `sha256:261410ab…` built `2026-09-07T00:56:08+05:30`; container started `2026-09-06T20:01:33Z` (01:31 IST). **Only one `.py` under `app/`, `tools/`, `tests/` is newer than the image: `tests/test_exclusion_invariants.py` (01:02 IST)** — a test, not shipped behaviour | `docker image inspect`; `find … -newermt '2026-09-07 00:56:08'` → 1 file | applied |
| **No app-code bind mount (image is authoritative)** | MIGRATION-RECONCILIATION: "it does not bind-mount app code" | True. Mounts are `/reports`, HF cache, `/models` (ro), `/data`, `/data/brain` (ro) — no source bind | `docker inspect --format '{{range .Mounts}}…'` | applied |
| **`requirements.txt` change (numpy floor) is in the image** | WORKLOG / dependency change | True — tree and `/app/requirements.txt` are identical; the only added line is `numpy>=1.26` | `diff` of both files → identical | applied |
| **"Run the drain INSIDE the orchestrator container… the exact deployed code"** | DEPLOYMENT-PLAN 2nd deploy, twice | **Unsupported as written.** The image contains only `/app/app` and `/app/requirements.txt` — there is **no `/app/tools`**, so `tools/reindex_web.py` cannot have been run from the image. It must have been copied or piped in; that step is not recorded anywhere | `docker exec … ls -la /app` → `app/`, `requirements.txt` only; `find / -maxdepth 4 -name reindex_web.py` → nothing | NOT-DONE (runbook not reproducible) |
| **"sync-worker (Salesforce), frontend and every vLLM container were never touched; original start times, restarts=0"** | DEPLOYMENT-PLAN, all three deploys; WORKLOG "Final state" | **No longer true of the current system.** A `techsara up` (still running, PID 3913879) rebuilt `sf-local-ai-frontend:portable` (01:23:12) and `sf-local-ai-sync-worker:portable` (01:22:57) and recreated both containers, and recreated the main vLLM head | `docker inspect`: frontend started `2026-09-06T20:06:07Z`, sync-worker `20:05:57Z`, `sf-local-ai-vllm-1` `20:16:04Z` (all today); `docker images` build timestamps | NOT-DONE (claim invalidated after the fact) |
| **Orchestrator `RestartCount` stays 0** | DEPLOYMENT-PLAN post-deploy watch | Holds. `restarts=0` on orchestrator, frontend, sync-worker, vllm, router, embed, ocr | `docker inspect --format '{{.RestartCount}}'` | applied |
| **`vllm-reranker restarts=4` is pre-existing** | WORKLOG note | True and unchanged — still `restarts=4`, started `2026-09-05T23:37:54Z`, i.e. before every deploy in these docs | `docker inspect sf-local-ai-vllm-reranker-1` | applied |
| **`/health` exposes the index report** | DEPLOYMENT-PLAN: "`main.py` now returns it" | True and live | `/health` returns a top-level `web_index` block with `rows/distinct_pages/chunker_version/pending_pages/rechunk_pending` | applied |
| **Service health is green** | DEPLOYMENT-PLAN: "health all-ok" | **Currently `degraded`** — `vllm` check fails with `ConnectError: name resolution`, `context.status: degraded`. Cause is the in-flight `techsara up` recreating the head, not this phase's code | `/health` read from inside the orchestrator container | blocked (transient, external to the claim) |
| **C1 fix — `select_passages` on the search path** | WORKLOG: "fixed and independently verified" | Present in the tree and the image | `select_passages` in `app/web_memory.py`, `app/engines/search.py`; `_select_text` in `app/engines/deep_research.py`, `web_memory.py`, `engines/search.py` | implemented |
| **C2 / K4 fix — structured-data recovery incl. JSON-LD + microdata** | DEPLOYMENT-PLAN 2nd deploy: `core/structured.py` present, `EXTRACT_VERSION 4` | Present. `app/core/structured.py` exists (untracked) and ships in the image | file present in tree and in `/app/app/core/`; `_jsonld_records`, microdata walker, `[jsonld]`/`[microdata]` provenance markers | implemented |
| **K5 conditional requests / K6 robots on the refresh path** | DEPLOYMENT-PLAN: verified against real sites | Implemented in tree and image | `If-None-Match`/`If-Modified-Since` in `app/core/net.py`, `app/web_worker.py`, `app/engines/search.py`; `reserve_slot` in `app/core/robots.py`, `engines/search.py`, `engines/url.py` | implemented |
| **K7 — quarantine / servable-id filter on the vector path** | FINDINGS "Corpus integrity round" | Implemented (`servable_web_page_ids`, `set_web_page_quarantine`, `web_page_ids_for_urls`) but **never exercised in production**: quarantined page count is still 0 | `app/db.py`, `app/web_index.py`; `count(*) filter (where quarantined_at is not null)` → **0** | implemented / NOT-DONE (unexercised) |
| **K8 / K12 / R11 / R12 / R13 implemented** | FINDINGS: rounds 2 and 3 | All present in the tree: `_SHINGLE_CHARS` (`core/provenance.py`), `primary_weight` (`provenance.py` + `deep_research.py` call site), R13 admission control replacing `_RUN_LOCK` | `app/engines/deep_research.py:117, 2442, 2494, 2501, 2685`; `app/config.py` adds `deep_research_max_concurrent/max_per_user/queue_wait_s` | implemented |
| **`perf` — `_coverage_gap` double re-tokenisation** | FINDINGS "still open" | Addressed: rewritten against a shared tokenisation helper | `app/web_memory.py:618` ("Written for `engines.search._coverage_gap`, which used to do…"); `app/engines/search.py:900` | implemented |
| **`header-rescue` — cards and `<dl>` get no header carry** | FINDINGS "still open" | **Still open.** `_carry_table_header` matches pipe-table syntax only (`_TABLE_ROW_RE`, `_TABLE_RULE_RE`); the JSON-LD/microdata and card renderings have no header concept | `app/web_index.py:88-155`, `_MAX_CARRIED_HEADER_CHARS = 400` | NOT-DONE |
| **Fast-mode CPU ≈ 65% reduction; 168 turns, 0 errors** | WORKLOG | Backed by artefacts, not re-run here | `docs/fast-web-research/measurements/ab-*.json`, `fast-path-*.json`, `scenarios-*.json`, `ttft-*.json` (25 files, 2026-09-06/07) | measured |
| **Cap-256 deploy availability: "1,463 probes, 2.5 s unavailable window, 12 truncated, 2 refused"** | COVERAGE-DECISION | Substance correct, **probe count wrong**: the artefact records **4,203 probes** across three targets (route 1,801 / metrics 1,801 / health 601), 4,189 served, 99.667%. The 2.501 s window, 12 truncated and 2 refused are exact | `measurements/availability-cap256-deploy.json` → `summary.probes=4203`, `unavailable_intervals[].observed_duration_s = 2.501` | measured (figure misquoted) |
| **Tail-fact retrieval after the cap raise is weak (~1 in 5 prose, ~0 for big tables)** | COVERAGE-DECISION "the verification that qualifies the decision" | Self-reported honestly; the artefacts exist. Not independently re-verified in this pass | `measurements/tail-facts-20260907.json`, `tail-facts-20260907-grading.json`, `structured-real-world.json` | measured |
| **Per-page retrieval diversity / large-table ranking follow-on** | COVERAGE-DECISION: "not done, not in scope" | Confirmed absent | no ranking-diversity code in `web_index.retrieve` / `web_memory` | NOT-DONE (correctly declared) |
| **Test coverage added: "141 tests across five files"** | WORKLOG "Final state" | **Stale.** 315 test functions now exist across **11** new files (17/34/29/49/31/37/26/22/31/17/22) | `grep -c '^\s*\(async \)\?def test_'` per file in `orchestrator/tests/` | implemented |
| **Full suite green: "2,638 passed, 3 skipped, exit 0"** | WORKLOG "Final state" | **Cannot stand for the current tree.** That run predates ~174 of today's 315 new tests and the V27/cap-256 changes. Two partial pytest runs were in flight during this pass; no definitive full-suite result exists. WORKLOG itself already says "a definitive run is still owed" | `ps -eo args`: PIDs 3931987 and 3932444 running subsets against `techsara_crawlidx_test` / `techsara_deepres_test`; last recorded figure `WORKLOG.md:281` | blocked |
| **S4-risk — no test covers the `websearch_to_tsquery` path** | FINDINGS "still open" | Now covered | `websearch_to_tsquery` referenced in `app/db.py`, `app/web_memory.py` and `orchestrator/tests/test_fetch_hygiene.py` | tested |
| **CLUSTER-EVIDENCE: main model runs TP=2 across both nodes** | CLUSTER-EVIDENCE headline | Still true of the config as of this pass: the head process carries `--tensor-parallel-size 2 --nnodes 2 --node-rank 0`, and `.env` has `CLUSTER_TENSOR_PARALLEL_SIZE=2` | `ps -eo args \| grep 'vllm serve /models/repos/nvidia'`; `python3 scripts/lib/env_export.py .env` (values non-secret) | applied |
| **A "TP=2 → TP=1 single-node switch" gate measurement** | Not in any document — discovered in `ps` | **In flight, undocumented, and not yet reflected in the running config.** A 2,400 s `availability_probe` is writing to `measurements/availability-tp1-switch.json`, which does not exist yet; the head is still TP=2 | PID 3817795 `tools.availability_probe --note 'TP=2 -> TP=1 single-node switch (gate measurement)' --out ../docs/fast-web-research/measurements/availability-tp1-switch.json`; `ls` on that path → no such file | NOT-DONE (in progress) |
| **Background jobs alive** | WORKLOG baseline: "Running jobs at start: none" | Four relevant live processes: `techsara_cli up` (3913879), `availability_probe` (3817795), two `pytest` runs (3931987, 3932444). No reindex/drain job running | `ps -eo pid,etime,comm,args` | applied (informational) |
| **`.env` shell-sourcing trap fixed by tooling** | WORKLOG: `.env` line 251 unquoted value breaks `set -a && . ./.env` | Tooling exists: `scripts/lib/env_export.py`, `scripts/lib/env-load.sh`, `scripts/check-env-files.sh`, `launcher/tests/test_env_file_shapes.py` — all untracked, none committed | files present; `env_export.py` parsed 108 lines of `.env` cleanly in this pass | implemented |
| **Everything remains uncommitted / unshipped** | WORKLOG: "no commits" | True — and this is the largest standing risk: **~6,100 changed lines plus 25 new paths exist only in one working tree**, already deployed into production images | `git diff --stat` → 32 files, 6,140 insertions, 388 deletions (snapshot; now 33 files); `git log -1` unchanged at `29aa0ab` | blocked |

---

## Top discrepancies

1. **`MIGRATION-RECONCILIATION.md` states the opposite of the shipped code.** It says
   "identifier 21 is retired permanently… fresh databases simply never have a 21; the tests
   assert that gap." In fact `db.py:1151/1474` defines and registers V21 (restored verbatim),
   and `test_crawl_durability.py:744` asserts a *contiguous, sorted* version list. The code is
   right; the document is wrong and will mislead the next operator.

2. **"Run the drain inside the orchestrator container… the exact deployed code" is not
   reproducible.** The image has no `/app/tools` at all — only `/app/app` and
   `requirements.txt`. Whatever ran the reindex was injected by an unrecorded step, so the
   runbook cannot be replayed as written.

3. **The "nothing else was touched" guarantee has been invalidated since it was written.**
   A `techsara up` (still running) rebuilt and recreated `frontend` and `sync-worker`
   (Salesforce) and recreated the main vLLM head, ~35 minutes after the last documented deploy.
   `/health` is `degraded` right now on the `vllm` check as a result.

4. **No definitive full-suite result exists for the current tree.** The headline
   "2,638 passed, 3 skipped" predates roughly 174 of the 315 tests now present and the entire
   V27/cap-256 slice. Two partial runs were still executing during this pass.

5. **`MANIFEST.sha256` no longer describes the tree** — 5 of 77 entries fail, including
   `db.py` and `web_index.py`, i.e. exactly the files the last deploy changed. The "48 files"
   figure in WORKLOG is also wrong (77 entries, 33 modified + 25 new paths today).

6. **The corpus is 95% short of the extraction target it was deployed for.** 2,097 of 2,209
   pages are still below `EXTRACT_VERSION 4` (2,011 at version 0). Re-chunking is fully drained
   (all pages at `chunk_version 3`), but re-extraction needs one unconditional fetch per page
   and is progressing at the worker's 8-per-300 s budget.

7. **Two shipped features have never executed in production.** `web_crawl_frontier` (V25) holds
   0 rows and `quarantined_at` is 0 across 2,209 pages — K12 and K7 are implemented and tested
   but have no production evidence behind them.

8. **A TP=2 → TP=1 cluster switch is being measured right now and is in no document.** The
   probe's output file does not exist yet, `.env` still says
   `CLUSTER_TENSOR_PARALLEL_SIZE=2`, and the head still runs `--tensor-parallel-size 2`. Any
   claim about single-node performance is unsupported as of this pass.

9. **A misquoted measurement.** COVERAGE-DECISION reports "1,463 probes" for the cap-256
   availability window; the artefact says 4,203 across three targets. The load-bearing numbers
   (2.501 s unavailable, 12 truncated, 2 refused) are correct.

10. **Everything is still uncommitted.** ~6,100 changed lines across 33 files plus 25 untracked
    paths live in a single working tree that is *already deployed to production*. The working
    tree was being edited by another workstream during this very pass (modified count moved
    31 → 33). A `git checkout` here would also empty running containers' bind mounts.

## What was verified as genuinely correct

The migration ledger (source `1..27` == database `1..27`, no gaps, no orphans), the V23
backfill (0 unschedulable pages), the V22/V24 index reconciliation, the chunker-3 drain (all
2,209 pages, both backlogs 0, sidecar advanced), R5 (0 stuck research runs), production data
integrity (452/1,864/2,209/138), and — the strongest single result — that the running
orchestrator image is **byte-identical** to the working tree across all 120 `app/**/*.py`
files, so no claimed code change is missing from production.
