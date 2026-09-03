# ADR-0001: The knowledge "brain" — one evidence pipeline, scoped, measured

Status: accepted 2026-09-03; amended the same day after a four-lens
adversarial critique (retrieval, security, performance, migration) whose
accepted objections are marked **[critique]** below; implementation log in §9
Deciders: engineering (autonomous, per the master task's mandate)
Inputs: `01-forensic-audit-2026-09-03.md`, `02-rag-audit.md`, the
benchmarks in `04-eval-and-benchmarks.md`, `orchestrator/tools/rag_eval.py`

## 1. Context

The platform already has every ingredient of a retrieval-augmented brain:
a durable page store with full-text search (PostgreSQL 18), a derived vector
index (LanceDB, Qwen3-Embedding-0.6B, 1024-d), a cross-encoder reranker
(Qwen3-Reranker-0.6B, own vLLM container), resolved research claims with
dates, per-user conversation memory (facts + message embeddings),
per-conversation document/URL/repo stores, a freshness classifier and a
background crawler/refresher.

What it lacks is *one* path through them. The forensic audit showed the
same public question taking three different evidence paths depending on an
effort setting and a toggle, a local retrieval that discards the passages
that answer, a reranker that is deployed but not used where it matters (and
mis-prompted where it is), and private recall of the assistant's own earlier
answers standing in for shared evidence.

## 2. Decision summary

| # | Decision | Why (evidence) |
|---|---|---|
| D1 | **Keep PostgreSQL + LanceDB. No new infrastructure.** | pgvector is not in the deployed PG 18 image (only `pg_trgm`); swapping the database image is a migration with no measured gain at 9k–90k vectors. LanceDB flat scan: 19 ms at 9k rows (web), 150 ms at 90k rows (Salesforce). ANN index added per D8 when a table crosses the measured threshold. Qdrant/Milvus/Weaviate would add a service, a second copy of the data and a new failure mode for a workload that fits in a 66 MB directory. |
| D2 | **One evidence pipeline** (`app/knowledge/`) used by Fast chat grounding, Think/forced search, research seeding and the agent's web step. | Same question → same candidates → same scoring → same packing, regardless of route. Routes differ only in *how far up the escalation ladder they may go*. |
| D3 | **Candidate generation is hybrid with AND-first lexical**: dense top-30 (LanceDB, instruction-prefixed query) ∪ lexical AND, then OR to fill (PostgreSQL FTS, `ts_rank_cd` normalisation 32, ≤ 3 pages per domain, quarantined pages excluded). The lexical query is built from **raw content words** — PostgreSQL stems once **[critique]**. A lexical hit contributes the ~3,200-char **window with the most question terms**, not the page head **[critique]**. Candidates merge by `page_id`, and near-duplicate texts collapse to one item **[critique]**. | The OR-only lexical half ranked a Microsoft page first for a question about a small company (term frequency of "solution"). AND ranks the answering pages 1–9. Python-stemmed words re-stemmed by PostgreSQL ('busines' → 'busin') match nothing under AND. Mirrors counted as corroboration and as two domains. |
| D4 | **The templated cross-encoder scores every candidate and decides answerability.** `app/rerank.py` is the single client (Qwen3-Reranker instruction template; yes/no probability; per-caller instruction). Relevance = answer-probability ≥ 0.30; sufficiency = best ≥ 0.70, or two ≥ 0.50, **and the copy is fresh for the verdict (fetch age) and the answering passage is not STALE (D5)**. A STATIC question judges at most 8 candidates and only when the strict pre-gate finds one; a time-sensitive one at most 12. The reranker is **verified**: a fixed canary triple at first use and every worker cycle trips a breaker on a wrong answer; a single call with degenerate (all-equal) scores is refused **[critique]**. Unjudged evidence (reranker busy/broken/tripped) may be sufficient only through the strict topical gate; a deployment with no reranker at all keeps the hybrid rule. | Raw `/score` scoring put a careers page above a board listing (0.70 vs 0.34); templated scoring gives 0.09 vs 0.995. Entity-overlap "relevance" declared two non-answering pages sufficient. Cost: 82 ms / 8 passages, 105 / 15, 172 / 30 (measured). A healthy-looking reranker returning garbage would otherwise be *amplified* by this design. |
| D5 | **Date semantics**: freshness for the *sufficiency* decision is **copy age** (when we last read the page); the page's own date is used for ranking, supersession and the **STALE** rule **[critique]**. A year-only date (Jan 1, 00:00) counts as undated. Supersession applies only among *answering* passages; an undated page never retires a dated one; a member-shared page never retires anything. STALE = the best answering passage's stated date is older than 120 d (RECENT) or the max age (REALTIME) → not sufficient, so Fast runs stage 2 and Think escalates. | The undated copy of a page superseded its dated twin; the org-chart page that answered was dropped for being 80 days old (its stated date) in favour of undated pages "read yesterday". With no newer rival, "X was appointed in 2023" would otherwise be "sufficient" forever. |
| D6 | **Progressive escalation, local first, at every effort**: (0) public evidence cache → (1) stored knowledge + claims (D2–D5) → (2) tiny live lookup (≤ 2 pages, 8 s deadline) when insufficient → (3) full live search (Think, or search forced on) with stored evidence *merged*, not skipped → (4) deep research → (5) agent. **Stage 1 runs for every assistant text turn**, including one the auto classifier wants to search **[critique]**; a store that answers with confidence ≥ 0.85 cancels an auto-decided search; a *confirmed* time-sensitive question the store cannot answer escalates Think to stage 3. A **volatile** verdict ("latest release", "current price", REALTIME) caps the freshness window at 24 h and never stops Think early **[critique]**. `web_search=off` is a hard stop at every effort **[critique]**. The whole pre-answer stage has a 12 s deadline. | Think spent 17–19 s and cited noise for a question the store answered in 1 s. The `_FRESH_RE` skip in `_memory_sources` is removed. Fast + search OFF fetched two pages outside the rate limit. |
| D7 | **Scopes are enforced at retrieval time**, in the query, by a `Viewer`/`Scope` contract: `public` (the web store, research claims), `user` (facts, cross-chat message recall), `conversation` (uploaded documents, pasted URLs, repos; ownership checked via `conversation_owner`, and an upload **claims** an unowned conversation id for the uploader **[critique]**). **The public store is member-writable** (a pasted link is stored globally and its site crawled — the owner's request), so every page carries **provenance** (V16: `origin`, introducer, `quarantined_at`) **[critique]**: a `share`-origin page is cited but capped at neutral authority, is never primary and never retires evidence; credential-shaped URLs (pre-signed links, share tokens, userinfo) are refused at the share boundary; user-generated-content hosts (sites./docs.google.com, gist, medium…) are never reference/primary; operators can list, quarantine and purge by introducer. Research claims persist only source-grounded, quoted material. Outbound search queries are built from the user's own turns, never from private system blocks. | Already largely true in SQL (`user_id = %s`, `conversation_id = %s`); this makes it a stated contract with tests. Without provenance a member could plant a "reference" page that retired the real answer for everyone; without the URL filter a pre-signed link became a citation shown to other users. |
| D8 | **Vector index policy**: flat scan below 50k rows; IVF_FLAT above it (sqrt(rows) partitions, 50 probes, refine 2 — recall@10 0.995 at 9 ms vs 150 ms flat on a 90k copy; IVF_PQ 0.81 and HNSW_SQ 0.88 rejected). **Built by each table's single writer** — the sync-worker for the Salesforce table, the orchestrator's web worker for `web_chunks` — never on a request **[critique]**. Both tables are compacted on a cadence (an hour's / a week's version retention). Rollback for an index is reader-side (`KNOWLEDGE_ANN_BYPASS` → flat scan, no data change). A chunker or model change is a **build-alongside into a new directory** (`tools/reindex_web.py`), validated by counts, pointed to by `LANCEDB_WEB_DIR`; the sidecar is written once, atomically, and records `chunker_version`. An empty index directory **self-heals** (the watermark is reset). | Flat scan is 19 ms at the web table's size and dominates nothing; the 90k-row Salesforce table is at 150 ms, 3.1 GB on disk for ~0.5 GB of data, and growing. |
| D9 | **Recall of the assistant's own earlier answers is not evidence.** For questions that need evidence (RECENT/REALTIME) assistant-authored snippets are excluded from the cross-chat block; user-stated facts stay. The prompt states that earlier-conversation context is never a citable source. | A/B: 0/3 vs 3/3 answers driven by a recalled answer, cited against sources that did not contain it. |
| D10 | **Caching**: an in-process query-embedding LRU (one embed per turn instead of three) and a public-scope evidence cache (normalised question → judged evidence, 60 s TTL). The cache key carries the **corpus generation** (bumped on every page write / index write) and the reranker's state; empty and degraded results are never cached; cached items are copies **[critique]**. **No answer cache.** | Answer caches are where private context leaks into public responses; the evidence cache is public-only by construction. Without the generation the Fast lookup's read-back returned the pre-fetch result. |
| D11 | **Chunking per content type, versioned**: web pages fixed windows (v1, recorded as `chunker_version` in the sidecar); a heading-aware v2 is adopted only if the RAG eval shows a gain; PDFs page-aware (already); claims sentence-level (already). | Fixed 3,200-char windows split facts across boundaries; the evidence of benefit must come from the eval, not from taste. |
| D12 | **Observability**: `chat_route_total`, `chat_ttft_seconds` and `chat_total_seconds` per route/effort (orchestrator-side); `knowledge_stage_seconds{stage=embed,dense_scan,lexical,meta,rerank}`; `knowledge_decision_total`, `knowledge_verdict_total`, `knowledge_escalation_total`, `knowledge_degraded_total{reason}`, `knowledge_evidence_cache_total`; `rerank_requests_total{outcome,kind}`, `rerank_seconds`, `rerank_queue_seconds`, `rerank_inflight`, `rerank_canary_ok/margin`; `embed_requests_total{outcome,kind}`, `embed_seconds`, `embed_queue_seconds`, `embed_batch_size`; `freshness_router_seconds`. Existing `metrics.py` registry; documented in `docs/MONITORING.md`. | You cannot tune an escalation ladder you cannot see. |
| D13 | **Backpressure**: bounded in-flight budgets for **both** sidecars **[critique]** — reranker 4 (2 reserved for stage 1; stage 1 waits 0.25 s at Fast / 1 s at Think, bulk callers 1.5 s; 2 s per-call deadline), embedding 8 (1 s wait, 4 s query timeout, batches of 64 for indexing). On a busy signal the stage degrades (hybrid order without the judge; lexical without dense), the verdict is marked degraded, **no escalation is spent on it**, and `meta.knowledge.degraded` says so. The model's own scheduler (vLLM `max_num_seqs`) remains the generation backstop. | Retrieval must never amplify a burst into a reranker/embedding pile-up that stalls generation for everyone — and a busy reranker must not silently reinstate the heuristics the audit condemned. |
| D14 | **No fine-tuning for knowledge. No paid APIs.** | The 2026-08 fine-tune post-mortem: a corpus-trained LoRA learned to fabricate names and ids. Knowledge is retrieval. |

## 3. The pipeline

```
question, Viewer, effort, web_search_pref
   │
   ├─ freshness verdict (offline rules → router)          [existing]
   ├─ stage 0: evidence cache (public scope only)          [new]
   ├─ stage 1: knowledge.retrieve()
   │     candidates: dense(30) ∪ lexical AND(15) ∪ lexical OR(15) ∪ claims(3)
   │     scope filter IN the query (public/user/conversation)
   │     rerank: templated cross-encoder → answer-probability per passage
   │     dates: content-date precision, undated = discounted fetch time
   │     partition: supersession only among answering, dated passages
   │     verdict: SUFFICIENT | STALE | INSUFFICIENT  (+ reasons, logged)
   ├─ stage 2: tiny live lookup (Fast, insufficient)      [existing, now merged]
   ├─ stage 3: full live search (Think / forced) — live ∪ stored, one rerank
   ├─ stage 4/5: deep research / agent                    [existing]
   │
   └─ pack(): numbered sources with dates + scope labels, budget by effort;
              earlier-conversation context is a separate, non-citable block
```

## 4. Alternatives considered

- **Qdrant / Milvus / Weaviate / Chroma**: rejected. All need a new service,
  a second copy of every vector and their own backup story. The measured
  bottleneck is *what* is retrieved and *how it is scored*, not scan speed.
- **pgvector**: attractive (one store, transactional, filters in SQL) but not
  installable in the deployed image; changing the database image for a
  90k-vector workload is not justified by any measurement. Revisit if the
  vector count passes ~5M or if filtered ANN becomes the bottleneck.
- **BM25 in LanceDB (full-text index)**: LanceDB 0.37 has one, but PostgreSQL
  already holds the text, the tsvector and the claims, and the scoped joins
  live there. Two lexical indexes would drift.
- **An answer cache**: rejected for privacy (see D10) — the platform serves
  a workspace where private and public context share a prompt.
- **Prompt-only fixes**: rejected; the model behaved correctly for the
  evidence it was given. The evidence was wrong.

## 5. Security invariants (tested)

1. A user can never retrieve another user's facts, message recall,
   documents, pasted URLs or repo chunks — enforced by `user_id` /
   `conversation_owner` predicates inside the queries, not by post-filtering.
2. The evidence cache stores only public-scope evidence ids and is keyed on
   the question alone; nothing user- or conversation-scoped enters it.
3. No engine may cite an earlier-conversation snippet as a numbered source.
4. Every evidence object carries `scope`; the packer asserts it.

## 6. Migration and rollback

**Step 0 of every change**: `scripts/backup-knowledge.sh` — a `pg_dump`
of the application database, a tarball of both LanceDB directories taken
with the sync-worker paused, the two sidecar files verbatim, and a manifest
of row counts. Seconds; refuses to run when the backup directory is full.

**Schema**: V16 is additive — every new column is NULLable or has a
DEFAULT, no existing value is rewritten, no index is created outside the
migration transaction (the table is 1,347 rows; use `CONCURRENTLY` once it
passes ~50k). A code rollback therefore needs no schema change: the
previous release's INSERTs and SELECTs run unchanged against a V16
database. There are no down migrations by design.

**Vector index**: never deleted in place. A chunker or model change is a
build-alongside (`python -m tools.reindex_web --dir /data/lancedb-web-v2`)
into a new directory with its own sidecar, validated by
`distinct page_id == indexable pages`, then pointed to by `LANCEDB_WEB_DIR`
(an `.env` edit + orchestrator recreate); rollback is the previous
directory. An in-table ANN index rolls back reader-side
(`KNOWLEDGE_ANN_BYPASS=true`) without touching data. An empty directory
self-heals (watermark reset); the explicit rebuild is
`python -m tools.reindex_web --reset-watermark`.

**Flags** (each read at process start; a flip is an orchestrator recreate,
~1–4 min, which drops in-flight streams — so revert under incident, not
casually):

| flag | default | OFF restores |
|---|---|---|
| `KNOWLEDGE_RERANK` | true | hybrid-score ranking and the pre-ADR sufficiency rule (no cross-encoder judgement) |
| `KNOWLEDGE_LOCAL_FIRST` | true | auto-decided searches always run; no escalation from stage 1 |
| `KNOWLEDGE_EVIDENCE_CACHE_TTL_S` | 60 | 0 disables the evidence cache |
| `RECALL_ASSISTANT_ANSWERS_FOR_FACTS` | false | true restores recalling the assistant's own earlier answers for evidence questions (the audited failure) |
| `RERANK_CANARY_ENABLED` | true | false stops the canary/breaker (a wrong reranker is then trusted) |
| `KNOWLEDGE_ANN_BYPASS` | false | true forces flat vector scans |
| `LIVING_KNOWLEDGE_ENABLED` | true | false removes stage 1 entirely (pre-2026-09-01 behaviour) |

**Partial deploy**: the orchestrator and the sync-worker share the LanceDB
sidecar contract from two copies of `embedding_index.py`; `lancedb` and
`pyarrow` are pinned to identical versions in both requirement files and a
test asserts it. Code that only *reads* new sidecar keys ships first; both
copies ignore unknown keys.

## 7. What "done" means (acceptance)

- The forensic reproduction gives the same evidence set and a correct,
  faithfully-cited answer for both accounts at Fast/auto, Think/auto and
  Fast/on, from the store, in ≈1 s (Fast) — measured before/after.
- RAG eval (`orchestrator/tools/rag_eval.py`, synthetic QA generated from
  the corpus, no hand-written entities): the primary metric is
  **answer@5** (the gold answer text is in what the prompt sees); Recall@5,
  MRR and NDCG@5 are diagnostics (they reward chunk identity and can be
  gamed by lexical entanglement **[critique]**). New ≥ old on answer@5 and
  not worse on the diagnostics, on the frozen dataset v1.
- Cross-user tests pass; a deliberately planted private document is never
  retrieved by another account through any route.
- Failure tests: reranker down, embedding down, LanceDB directory missing,
  PostgreSQL down — each degrades as specified and is visible in metrics.
- Load: measured at 1/5/10/25 concurrent Fast requests with TTFT p50/p95;
  50/100/250 modelled from the measured per-stage costs and the vLLM
  scheduler limits, with the backpressure knobs documented.

## 8. Consequences

- Fast answers for public questions the platform has already read become
  deterministic across users and effort levels, and stay ≈1 s.
- Think stops paying 15+ s for questions the store answers; it still
  searches when the store is stale or silent.
- Reranking quality improves everywhere the cross-encoder is used
  (Salesforce RAG, search candidate selection) because the template fix is
  shared.
- One more hop (rerank) on the Fast path: +80–170 ms, measured.

## 9. Implementation log

**2026-09-03 — landed on `dev` (one commit), before deployment**

- `app/rerank.py` (new): the shared templated client, canary + breaker,
  degeneracy check, reserved/shared slot pools, per-caller instruction.
  Used by `web_memory`, `engines/search._rerank_results`, `engines/rag`.
- `app/web_memory.py`: raw-word AND-first lexical candidates (normalisation
  32, ≤ 3 per domain, quarantine excluded); best-window passages; merge by
  page id; near-duplicate collapse; answerability stage; STALE rule;
  supersession only among answering passages, never by undated or
  member-shared pages; eTLD+1 conflict rule; UGC authority cap; generation-
  keyed public evidence cache; degraded reporting; `CITATION_RULE`.
- `app/knowledge/` (new): the scope contract and the public `retrieve`.
- `app/living_knowledge.py`: decisions and metrics, volatile verdicts,
  escalation, busy-degradation, claims freshness by `as_of`, attributed
  Fast lookup.
- `app/main.py`: stage 1 for every assistant turn, local-first / escalation
  before dispatch with a 12 s deadline, hard stop for search OFF, recall of
  assistant answers excluded for evidence questions, route/TTFT metrics,
  introducer ids threaded to the URL engine and the pre-pass.
- `app/freshness.py`: `Verdict.volatile`, router deadline.
- `app/llm.py`: pooled clients, `embed_query` (LRU + bound), batch timeouts.
- `app/web_index.py`: instruction-prefixed queries, page id on hits, LIKE
  escaping, 64-text index batches, atomic sidecar with `chunker_version`,
  `maintain()` (compaction, IVF_FLAT above 50k rows, self-heal),
  `KNOWLEDGE_ANN_BYPASS`.
- `app/db.py`: V16; `upsert_web_page` provenance; corpus generation;
  `reset_web_index_watermark`; page-scoped `get_unindexed_web_pages`.
- `app/engines/search.py`: stored passages merged on every route through
  the pipeline; introducer ids on stored pages; verdict-driven warm-cache
  TTL; system-stripped turns for outbound queries; attributed Fast lookup.
- `app/engines/crawl.py`, `engines/url.py`, `core/urls.py`: origin/introducer
  on crawled and shared pages; credential-shaped URLs refused at the share
  boundary; the dedupe oracle closed at the status line.
- `core/provenance.py`: UGC host class, per-host authority cap, stricter
  `is_primary`. `core/net.py`: `is_global`, streamed bodies, per-hop
  re-validation, DNS pinning.
- `engines/deep_research.py`: claims persisted only when the value is quoted
  from the source; page id resolved; origin recorded.
- `app/uploads.py`: an upload claims an unowned conversation id.
- `app/memory_semantic.py`: background embedding backfill; `include_assistant`.
- `app/health.py`: `web_index` entry. `app/web_worker.py`: maintenance step.
- `sync-worker/syncworker/rag_index.py`: compaction cadence and IVF_FLAT
  index for the Salesforce table (its single writer).
- Tools: `tools/rag_eval.py`, `tools/reindex_web.py`,
  `tools/knowledge_admin.py`, `scripts/backup-knowledge.sh`.
- Tests: 2,228 orchestrator tests pass locally (private test database);
  new files `test_rerank`, `test_knowledge_unified`, `test_knowledge_scopes`,
  `test_url_sharing`, `test_search_hygiene`, `test_knowledge_admin`,
  `test_dependency_pins`, and additions to the SSRF, provenance, research,
  crawl and web-memory suites.
- Backup taken before deployment: `backups/20260903T032930Z` (PostgreSQL
  schema v15, 1,347 pages / 47 claims / 1,486 messages; LanceDB 89,955 +
  9,017 rows, verified by re-opening every table).

**After deployment** — the forensic reproduction, the eval on dataset v1
(variant `unified`), the load test and the first `/metrics` readings are
recorded in `04-eval-and-benchmarks.md`.
