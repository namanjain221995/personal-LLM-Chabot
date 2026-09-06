# Deployment plan — expected interruption, procedure, rollback

Written before execution, as required. Nothing here has been run yet.

## What actually changes

`git diff --name-only` touches **only `orchestrator/`**. The frontend and sync-worker sources are
untouched, so neither image changes and neither container needs recreating.

**One container is recreated: `sf-local-ai-orchestrator-1`.**

## Expected interruption

| service | effect | duration |
| --- | --- | --- |
| **orchestrator** | chat, `/chat`, history, admin and every API route unavailable while the container is replaced | **~20–60 s** typical; `stop_grace_period` is 2m, so a request in flight is allowed to finish before the old container stops |
| **frontend** | not recreated. The UI stays up but its API calls fail for the orchestrator's downtime — a user mid-conversation sees a failed request, not a broken page | — |
| **sync-worker (Salesforce)** | **not touched.** Its source, image and container are unchanged, so the Salesforce sync is not interrupted at all | none |
| **vLLM head / router / embed / rerank / OCR** | **not touched.** No model reload — that would cost 6–10 minutes | none |
| **postgres, searxng, monitoring** | not touched | none |

A `techsara up` would additionally re-run capability detection and regenerate `.runtime/generated.env`.
That is not wanted here: the current generated env is what the live stack is running, and
re-detection has previously disabled healthy services. The plan is therefore the surgical
`docker compose up -d --no-deps orchestrator` against the **exact four-file chain** the running
container was created from. A subset of that chain silently resolves the orchestrator to
`sf-local-ai-orchestrator:cpu` instead of `:cuda`.

## The migration runs during this restart

`init_schema` is applied in the FastAPI lifespan, before the first request is served, under
`pg_advisory_xact_lock` inside a transaction. So the restart is also when V22 and V23 apply:

* **V21** — already recorded in production (2026-09-05); skipped, exactly as intended.
* **V22** — drops the version-hardcoded partial index, creates `idx_web_pages_extract_stale`.
* **V23** — schedules the 1,602 stranded pages across a 24h window.

A failed migration fails startup rather than surfacing on the first user request, and PostgreSQL's
transactional DDL rolls back a partial apply. Rehearsed against a real restore of production data:
both the upgrade-from-V21 and fresh-install paths converge on byte-identical schemas.

## Why this waited for the fetch work

V23 makes 1,602 pages refreshable, and the worker fetches 8 pages per 300 s = ~2,304/day.
Deploying the migration *before* conditional requests and robots would have converted a dormant
defect into ~2,300 unconditional, robots-ignoring third-party downloads a day. With K5 and K6 in,
an unchanged page costs a 304 with no body, no extraction and no re-embedding, and a disallowed
path is not fetched at all.

## Preconditions (all must hold before execution)

1. Full suite green on the version-matched isolated instance, no competing writers.
2. Backup present and **proven restorable**: `/home/techsphere/backups/techsara-fastweb-20260906/`
   — 33 MB custom-format dump, schema snapshot, migration ledger. Restoring it requires a
   **PostgreSQL 18.x** client/server; the 16.15 test server cannot read it.
3. Rollback images tagged: `sf-local-ai-orchestrator:rollback-20260906` (= `5b38c1917f8c`, the
   image running now), manifest at `rollback-manifest.txt`.
4. Engine idle check — do not recreate while a long generation is streaming.

## Procedure

```
# 1. build (does not touch running containers)
docker compose --project-name sf-local-ai \
  --env-file .env --env-file .runtime/secrets.env --env-file .runtime/generated.env \
  -f compose.yaml -f compose/compose.dgx-spark.yaml \
  -f compose/compose.published-dgx-spark.yaml -f compose/compose.cluster-dgx-spark.yaml \
  build orchestrator

# 2. recreate ONLY the orchestrator
... up -d --no-deps --force-recreate orchestrator

# 3. verify: health, schema version 23, backfill applied, data intact, a real answer
```

## Rollback

```
docker tag sf-local-ai-orchestrator:rollback-20260906 sf-local-ai-orchestrator:cuda
... up -d --no-deps --force-recreate orchestrator
```

The old image starts against the migrated schema. That is safe in this direction: V22 only
replaces an index the old code never queried, and V23 only fills a column the old code reads
(`next_refresh_at`) and whose extra rows it handles normally — the old refresh worker simply has
more pages to choose from. `extract_version` keeps its `DEFAULT 0` and the old code ignores it.
**The database is not rolled back**, and it does not need to be. If it ever must be, restore the
dump onto an 18.x server; that discards everything written since the backup.

## Post-deploy watch

* `web_page_refused_total`, `search_engine_unresponsive_total`, `knowledge_degraded_total`
* refresh-worker outcomes: `not_modified` should dominate once the backfill starts draining
* orchestrator `RestartCount` must stay 0 after the recreate

---

# EXECUTED — 2026-09-06

Build 20 s (cached layers) → new image `c3e5d6705635`; previous image preserved as
`sf-local-ai-orchestrator:rollback-20260906` (`5b38c1917f8c`).

Recreate returned in **2 s**; healthy in **7 s** — shorter than the 20–60 s predicted.
`--remove-orphans` was deliberately NOT passed: compose warned about eleven monitoring containers
that belong to a different file set, and the flag would have deleted them.

## Verification

| check | result |
| --- | --- |
| migrations applied at startup | `schema_version=23`, 23 rows, **V21 preserved**, V22 and V23 applied |
| backfill | stranded **1,602 → 0**; 2,208 scheduled |
| unrelated data | conversations 450, messages 1856, web_pages 2208, uploads 138 — all unchanged |
| index reconciled | only `idx_web_pages_extract_stale` remains |
| **Salesforce sync-worker** | `restarts=0`, start time unchanged — **never touched** |
| vLLM head / router / embed, frontend | `restarts=0`, start times unchanged — no model reload |
| orchestrator | `restarts=0`, running the new image |
| startup log | zero errors, zero tracebacks; refresh worker started |

## The fixes verified running inside the deployed image

```
EXTRACT_VERSION = 3
C2 <dl> recovery: B200=True 7.20=True 3.10=True
C1 page 11,737 chars, answer at 11,718
  budget 8000: head-slice keeps it = False   select_passages keeps it = True
  budget 2500: head-slice keeps it = False   select_passages keeps it = True
S4: _content_words('GPT-5.2 elo') = ['gpt-5.2', 'elo']
K5 safe_fetch accepts headers = True;  K6 robots allowed/reserve_slot = True
db.touch_web_page_unchanged, db.close_interrupted_research_runs = present
```

## Production behaviour, first refresh cycle

```
web knowledge worker started (every 300s, 8 pages/cycle)
GET https://en.wikipedia.org/robots.txt      200
GET https://docs.nvidia.com/robots.txt       200
GET https://docs.python.org/robots.txt       200
GET https://planet.kernel.org/robots.txt     404      <- fail-open, page still fetched
GET https://drwho.virtadpt.net/drwho.plan.txt  304 Not Modified
web knowledge worker: indexed=210 refreshed=7 unchanged=1 blocked=0 failed=0 crawled=0
```

**K5 and K6 are working against real sites.** The 304 cost one conditional round trip and no body,
no extraction and no re-embedding — which is the whole reason the migration waited for this work.
`extract_version` moved 7 pages to 3 while 2,201 remain at 0, so the re-extraction drains
gradually inside the ordinary per-cycle budget, exactly as the recovered V21 intended.

---

# SECOND DEPLOY — the extraction/chunking slice

## What changes in the running system

| | running image | tree |
| --- | --- | --- |
| `EXTRACT_VERSION` | 3 | **4** (JSON-LD + microdata recovery) |
| `CHUNKER_VERSION` | 1 | **2** (table header repeated into every chunk holding its rows) |
| latest migration | V23 | **V24** (`web_pages.chunk_version`) |
| `core/structured.py` | absent | present |

## Expected interruption

Identical in shape to the first deploy: **one container recreated** (`orchestrator`), ~20–60 s,
2 m grace so in-flight requests finish. `frontend`, `sync-worker` (Salesforce), every vLLM
container, postgres, searxng and monitoring are **not touched** — only `orchestrator/` source
changed. V24 applies in the lifespan before the first request is served.

## Two backlogs it creates, deliberately, and their cost

**1. Re-extraction — 2,208 pages, one UNCONDITIONAL fetch each.** Every stored page is below
`EXTRACT_VERSION 4`. Re-extraction needs the original HTML, which is not stored, so a conditional
request is useless here: a 304 returns no body and the page could never be repaired. The refresh
worker therefore drops the validators for exactly these pages and keeps them for everything else.

At 8 pages per 300 s that is **~2,208 fetches over ~23 hours, about 1.5 per minute**, each robots-
checked, byte-capped and paced. This is the mechanism working as designed — the alternative is
that the extraction improvement never reaches anything already stored — but it IS real outbound
traffic and is recorded here rather than discovered later.

**2. Re-chunking — 15,959 chunks, ZERO fetches.** `chunk_version < 2` is repaired from the stored
text. Measured capacity: ~250 embedding calls, ~196 s of embedding + ~2.3 s local, **≈ 3.3
minutes**, 62.3 MiB of vectors. This is the "reindex within measured capacity" and it is run
deliberately after the deploy rather than left to drain at 8 pages a cycle (which would take ~21
hours for work that needs no network at all).

Both backlogs are visible: `/health` now reports `rechunk_pending` beside `pending_pages`, which
during a chunker migration is the larger queue by far.

## Ordering, and why

1. Deploy (V24 lands; both version constants advance).
2. Drain the **re-chunk** backlog immediately — it is local, bounded and cheap, and until it
   drains the index holds chunks whose table rows have no header.
3. Let the **re-extraction** backlog drain on the worker's own schedule. It is network work and
   there is no reason to rush it.

Run the drain INSIDE the orchestrator container: it shares `/data`, the new cross-process
`flock`, and the exact deployed code, so it coordinates with the live refresh worker instead of
racing it.

## Rollback

Unchanged in shape: retag `sf-local-ai-orchestrator:rollback-20260906` (or the image this deploy
replaces) and recreate. The old code tolerates the new schema — V24 only adds a column with a
DEFAULT and an index the old code never queries. Chunks written by chunker 2 are still valid
vectors for chunker 1's reader; they are simply better ones.

## SECOND DEPLOY — EXECUTED 2026-09-06

Recreate → healthy in **4 s**. `schema_version 24`, `EXTRACT_VERSION 4`, `CHUNKER_VERSION 2`,
`core/structured.py` present. Data unchanged (2208 / 450 / 1856 / 138). `sync-worker` (Salesforce),
every vLLM container and `frontend` kept their original start times and `restarts=0`.

A third recreate followed, for a one-field fix described below. Same shape, healthy in 4 s.

### `/health` was dropping the index report entirely

Claiming the backlog was "visible in /health" turned out to be false, and checking rather than
asserting caught it. `check_dependencies()` computes a full `web_index` report — rows, distinct
pages, model, dimension, chunker version, backlogs — and `/health` returned only
`status/service/version/checks/context`, **discarding the key**. So `health._check_web_index`'s
own promise that "a chunker bump is visible here first" had never been true from outside the
process. `main.py` now returns it. `status` is untouched: the container healthcheck gates on
`status`, and a stale index is a degraded answer, not an outage.

```
"web_index": {"status":"ok","directory":"/data/lancedb-web","table":"web_chunks",
              "rows":15966,"distinct_pages":2063,"model_id":"Qwen/Qwen3-Embedding-0.6B",
              "dimension":1024,"pending_pages":3,"rechunk_pending":2165}
```

`chunker_version` is absent from that output because the LIVE sidecar has no such key — it was
written 2026-08-30, before the key existed. It appears once the repair drains and the sidecar is
advanced, which is the correct order: mid-repair the table genuinely holds both shapes.

### The reindex, run in production within measured capacity

Run INSIDE the orchestrator container so it shares `/data`, the new cross-process `flock` and the
deployed code with the live refresh worker. **Local work only — re-chunking reads `web_pages.text`,
so it issues no HTTP request at all.**

**Resumability was verified by actually interrupting it**, not by argument. Stopped on a 75 s
budget after 3 batches:

```
batch 1 chunks+1250 backlog=2165 elapsed= 40.3s
batch 2 chunks+1110 backlog=2108 elapsed= 71.8s
batch 3 chunks+1041 backlog=2048 elapsed= 93.3s
stopping on the time budget with 1988 page(s) left
```

After the interruption: `status=ok`, **`rows=15966` and `distinct_pages=2063` unchanged**, backlog
2165 → 1988. Row totals holding steady across a partial repair is the delete-then-insert contract
doing its job — a resumed run cannot duplicate, because each page's old chunks are removed in the
same locked write that adds its new ones.

### A pre-existing problem the drain made loud

The K10 chunk-ceiling warnings fire on real pages, and the losses are large:

```
dataswamp.org/~solene/index-full.html   1,245,628 of 1,425,228 chars not indexed (87%)
suzymchale.com/journal/journal2023.html   692,061 of   871,661 chars not indexed (79%)
research.swtch.com/feed.atom              951,577 of 1,131,177 chars not indexed (84%)
```

This is not new and not caused by this slice — the 64-chunk cap has always truncated these pages;
K10 only made it observable, and the drain re-indexed every page at once so every warning surfaced
together. It stays on the ledger as an owner decision: raising the cap costs embedding time and
vector bytes per oversized page.

### Reindex completed, and the task-5 checks

Resumed run: **25 batches, 11,141 chunks, 179.2 s**, backlog → **0**. With the interrupted first
attempt that is 14,542 chunks in ~4.5 minutes against a predicted ~3.3 — over the estimate, and
the interrupted work was not wasted (its 220 repaired pages were not redone).

| task-5 criterion | evidence |
| --- | --- |
| unchanged/current pages avoid unnecessary work | a fully-current page (extract 4 + chunk 2) is in **neither** queue: `get_unindexed_web_pages(...) == 0` while 12 such pages exist; `rechunk_pending=0`; the worker's own line reports `indexed=105 … rechunk_pending=0` |
| outdated pages become correctly searchable | on real page 1603, **every** chunk containing table rows now carries the header rule — `with_table_rows=2 with_header_rule=2` |
| interrupted reindexing resumes safely | interrupted deliberately at 93.3 s; afterwards `rows` and `distinct_pages` unchanged, and no page exceeds the 64-chunk cap (max observed 4), so the resume neither duplicated nor skipped |
| new chats retrieve repaired facts with citations | a conversation that never saw the page answers `82.7` and `$6.75` with `[1]` and populated `meta.sources` — including the exact fact that was previously fabricated as `$3.50`. Verified on the isolated instance running identical code; production `/chat` needs a user credential I do not have and must not create |

Index after: `rows 15989, distinct_pages 2063, chunker_version 2, pending_pages 0,
rechunk_pending 0`. The sidecar advanced from **absent** to 2 only once the backlog emptied,
which is the honest order — mid-repair the table really does hold both shapes.

### Why `304 Not Modified` is currently zero, and why that is correct

The first deploy produced a real 304 within minutes. Right now the count is 0, and that is the
design working rather than a regression: **every remaining page is below `EXTRACT_VERSION 4`**, and
a page needing re-extraction is fetched UNCONDITIONALLY because a 304 returns no body and could
never repair it. Conditional requests resume for each page as it reaches version 4. Progress so
far: 12 of 2,208. `blocked=4` in the worker's line is robots.txt refusing pages — K6 working
against real sites.

---

# THIRD DEPLOY — the deferred ledger items (2026-09-06/07)

Orchestrator only, recreated in **4 s**. `frontend`, `sync-worker` (Salesforce) and every vLLM
container kept their original start times and `restarts=0`. Rollback image tagged
`sf-local-ai-orchestrator:rollback-20260907`.

Migrations **V25** (`web_crawl_frontier`) and **V26** (`last_changed_at` repair) applied at
startup. Schema 24 → **26**, 26 rows, contiguous.

## V26 verified against a prediction made BEFORE it ran

| check | predicted | actual |
| --- | --- | --- |
| rows the predicate matches afterwards | 0 | **0** |
| ambiguous rows deliberately left alone (`fetch_count > 1`) | 9 | **9** |
| genuine change records preserved | untouched | 692 |
| repeated `init_schema` | no-op | migrations stay 26, nulls stable |

**A measurement error of mine, recorded because it nearly became a false alarm.** I predicted the
*delta* (1,264 rows V26 would clear) and then compared it against an *absolute* (1,509 total NULLs
afterwards), having never measured the pre-existing NULL baseline. The two are not comparable and
the difference was not a defect. The invariant that actually matters — predicate fully applied,
ambiguous rows untouched, real changes preserved — is checked directly above.

Data intact throughout: 450 conversations, 1,856 messages, 2,208 web_pages, 138 uploads.
Health after: `status ok`, schema 26, `chunker_version 2`, `pending_pages 0`, `rechunk_pending 0`.

The NULL count moving 1,509 → 1,507 between two checks is not drift: it is the refresh worker
stamping `last_changed_at` on two pages whose content genuinely moved — the repaired semantics
working live, minutes after deployment.
