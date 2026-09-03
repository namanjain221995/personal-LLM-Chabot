# RAG / memory / search audit (2026-09-03)

Phase 2 of the master task. What the platform's knowledge layers actually
do, measured on the live deployment, and what was wrong with each. The
findings marked **fixed** are addressed by ADR-0001's implementation; the
rest are recorded with a recommendation and left for a later round because
they are outside the evidence pipeline or need a product decision.

Inventory and measurements come from a read-only fan-out over the code
(seven readers, one completeness critic) plus direct measurement inside the
orchestrator container. Numbers are from 2026-09-03 unless stated.

## 1. Stores

| store | engine | scope | rows / size | read filter | notes |
|---|---|---|---|---|---|
| `web_pages` | PostgreSQL | public | 1,347 / 53 MB | none (by design) | full text + tsvector; V14 provenance; V16 origin/introducer/quarantine (**new**) |
| `web_chunks` | LanceDB (`/data/lancedb-web`) | public | 9,017 rows / 66 MB | none | 3,200-char windows, 400 overlap; no ANN index; 419 versions / 209 fragments — never compacted (**fixed**: worker maintenance) |
| `web_claims` | PostgreSQL | public | 47 | kind | research resolutions; `page_id` always NULL (**fixed** by the claims hardening) |
| `chunks` (Salesforce) | LanceDB (`/data/lancedb`) | org | 89,954 rows / 3.1 GB on disk for ~0.5 GB live | none | 137k retained versions; flat scan 150 ms (**fixed**: sync-worker compaction + IVF_FLAT) |
| `message_embeddings` | PostgreSQL | user | 1,074 | `user_id` in SQL | cross-chat semantic recall; 500-newest window |
| `conversation_chunks` | PostgreSQL | conversation | 252 | `conversation_id` (owner checked first) | folded-turn recall; no model id column (follow-up) |
| `user_facts` | PostgreSQL | user | 12 | `user_id` | saved facts |
| `documents` / `url_documents` / `repo_chunks` | PostgreSQL | conversation | 92 / 2 / 0 | `conversation_id` + `conversation_owner` | whole text into prompt; keyword selection only |
| DuckDB warehouse, brain packs, learned SQL examples | files / PG | org | — | none | Salesforce mode; org-wide by design |

pgvector is **not available** in the deployed PostgreSQL 18 image (only
`pg_trgm`, `pg_stat_statements`), which settles D1 without a benchmark:
moving vectors into PostgreSQL would mean changing the database image.

## 2. Retrieval paths before the change

| route | evidence source | ranking | reranker | consults the store? |
|---|---|---|---|---|
| Fast / auto (plain chat) | `living_knowledge.prepare` → `web_memory.retrieve` | hybrid (dense + OR-lexical), recency, authority; supersession by age | no | yes |
| Think / auto, search decided | `run_search_engine` live | engine rank → raw-snippet rerank | raw `/score` | no for any "fresh-intent" wording (`_FRESH_RE`) |
| Fast / on, Think / on | `run_search_engine` live | same | raw `/score` | same |
| site Q&A (crawled site in the conversation) | `crawl.site_hits_for` | L2 distance only | no | scoped by URL prefix |
| research | `deep_research` | own ranking | raw `/score` | warm cache only |

Three evidence policies for one question, one of which never looked at the
store for the questions people most often ask ("who is", "what is the").
**Fixed**: one pipeline (`web_memory.retrieve`, wrapped by `app/knowledge`)
feeds Fast grounding, the search engine's stored-passage merge and the
research seed; the `_FRESH_RE` skip is gone.

## 3. Findings by layer

### 3.1 Candidate generation

- **OR-lexical ranked by term frequency** (`ts_rank_cd` without
  normalisation, OR of stemmed terms): a Microsoft page ranked first for a
  question about a small company because it repeats "solution" 60 times.
  Baseline eval: Recall@5 0.217 for OR alone vs 0.683 for AND. **Fixed**:
  AND first, OR to fill, normalisation 32, ≤ 3 pages per domain, raw words
  (the Python stemmer and PostgreSQL's disagreed on `business`/`status`).
- **Page head instead of the matching passage**: a lexical hit handed the
  first 3,200 characters to the prompt. **Fixed**: the window with the most
  question terms.
- **Merged on the exact URL string** while PostgreSQL dedupes on `url_key`.
  **Fixed**: merged by `page_id`; near-duplicate collapse (shingles) so
  mirrors and syndicated copies are one item.
- **No ANN index, never compacted** (both LanceDB tables). Benchmark on a
  90k-row copy: flat 150 ms; IVF_FLAT 256 partitions, 50 probes: 9.4 ms at
  recall@10 = 0.995; IVF_PQ 0.81 recall (rejected); HNSW_SQ 0.88 (rejected).
  **Fixed**: worker builds IVF_FLAT above 50k rows, compacts every 12
  cycles, self-heals an empty index directory; reader-side
  `KNOWLEDGE_ANN_BYPASS` is the instant rollback.
- **Query embedding without the model's instruction** (Qwen3-Embedding is
  asymmetric). **Changed**: instruction-prefixed queries, documents as
  indexed; measured by the eval (see `04-eval-and-benchmarks.md`).

### 3.2 Scoring and sufficiency

- **Relevance = entity overlap**; sufficiency = two "relevant" passages.
  Two profile pages that never named the office holder were "enough".
  **Fixed**: the templated cross-encoder's answer probability decides
  relevance (≥ 0.30) and sufficiency (≥ 0.70, or two ≥ 0.50).
- **Reranker deployed but mis-prompted / unused**: raw `/score` pairs gave
  0.70 to a careers page and 0.34 to the board listing; the model's own
  template gives 0.09 vs 0.995. **Fixed**: one client (`app/rerank.py`),
  used by every path, with a canary and a degeneracy check so a wrong
  reranker degrades instead of amplifying.
- **Undated pages superseded dated ones** (fetch time stood in for content
  date; year-only dates looked eight months stale). **Fixed**: only
  answering passages take part; an undated page never retires a dated one;
  year-only dates are undated; a member-shared page never retires anything.
- **No STALE notion**: an answering passage dated 2023 with no newer rival
  was "sufficient". **Fixed**: content date older than 120 d (RECENT) or the
  max age (REALTIME) → not sufficient.
- **Volatile questions** ("latest release", "current price") shared the
  14-day RECENT window. **Fixed**: a volatile verdict caps the window at
  24 h and never lets Think stop early.

### 3.3 Routing and escalation

- Stage 1 was skipped whenever the auto classifier chose search, and the
  classifier's own `_FRESH_RE` is a second freshness lexicon. **Fixed**:
  stage 1 runs for every assistant text turn; a confident store cancels an
  auto search; a confirmed time-sensitive question the store cannot answer
  escalates Think to the search engine. `_FRESH_RE` remains in
  `should_search` (follow-up: replace with the verdict).
- **Fast + search OFF still fetched two pages**, outside the rate limit and
  unattributed. **Fixed**: OFF is a hard stop; the lookup carries user and
  conversation ids.
- Think + auto with search=false was handed stale evidence without the
  staleness note. **Fixed** (escalate or label).

### 3.4 Memory and recall

- **The assistant's own earlier answer was recalled as evidence** and
  cited against sources that did not contain it (0/3 vs 3/3 in the A/B).
  **Fixed**: assistant-authored snippets are excluded for questions that
  need evidence; the grounding block forbids citing earlier-conversation
  context.
- Backfill embedding ran inline before every answer. **Fixed**: background,
  one per user in flight.
- Thumbs-down answers are recalled like good ones; 500-newest window;
  240-char snippets from 8,000-char embeddings; facts block truncates at
  6,000 chars silently; no UI to review/delete facts on the public
  deployment. **Follow-ups** (product decisions).

### 3.5 Security and scope

- Per-user and per-conversation stores are filtered in SQL and authorised
  by `conversation_owner` — confirmed, now stated as a contract
  (`app/knowledge`) with cross-user tests.
- **Uploads could pre-seed an unowned conversation id** (accepted when the
  owner was None; the next /chat sender inherited them). **Fixed**: the
  uploader claims the id first, as /chat does.
- **Pasted URLs enter the shared corpus** (by design since 2026-09-03, at
  the owner's request) — but without provenance, with inherited authority,
  and with capability URLs (pre-signed links, share tokens) stored and
  shown to other users. **Fixed**: V16 origin/introducer/quarantine; share
  origin capped at neutral authority and never able to retire evidence;
  credential-shaped URLs refused at the share boundary; UGC hosts
  (sites./docs.google.com, gist, medium…) never reference/primary;
  `tools/knowledge_admin.py` to list, quarantine and purge by introducer.
- Research claims persisted the LLM-written sub-question (from the asker's
  message and history). **Fixed**: only source-grounded claims with a
  quoted sentence and a resolved page id; origin user/conversation
  recorded for purge, never rendered.
- Private system blocks (facts, recall, document excerpts) reached the
  search query rewriter and hence the search provider. **Fixed**:
  system-stripped turns for outbound queries.
- SSRF: `is_global` check, streamed bodies with a hard cap, per-hop
  re-validation, DNS pinning where the HTTP client allows it. **Fixed** to
  the extent documented in `core/net.py`.
- Salesforce vector corpus is org-wide and includes sensitive long-text
  fields synced from the CRM. **Follow-up**: field-level classification in
  the sync worker (not an evidence-pipeline change).

### 3.6 Performance and reliability

- Per-call HTTP clients (no pooling): **fixed** (one client per loop and
  endpoint).
- Embedding calls carried the generation read timeout (up to 4,200 s) on
  the request path; the indexer sent up to 2,560 chunks in one request.
  **Fixed**: 4 s query timeout, 8 in flight with a 1 s wait, batches of 64,
  a query LRU (one embed per turn instead of three).
- Reranker had no in-flight bound; the search engine could monopolise it.
  **Fixed**: reserved slots for stage 1, per-effort waits, per-call deadline.
- The whole pre-answer stage now has a deadline (12 s) after which the
  answer proceeds without grounding and the metric says so.
- Deployment context lengths in `generated.env` disagree with the sidecars'
  `--max-model-len` (embed/reranker 4,096 vs 32,768 advertised). **Follow-up**
  (launcher).
- Host memory: 96 GB used, 18 GB swap on the 121 GB node. **Follow-up**
  (capacity).

### 3.7 Data/migration hygiene

- "Deleting the LanceDB directory rebuilds the index" was false (the
  watermark stayed set). **Fixed**: self-heal + `reset_web_index_watermark`
  + `tools/reindex_web.py`.
- Sidecar written in two non-atomic steps. **Fixed** (single atomic write
  with the dimension known).
- No backup tooling. **Fixed**: `scripts/backup-knowledge.sh`.
- Two diverged copies of `embedding_index.py` and unpinned `lancedb`.
  **Fixed**: identical pins asserted by a test; shared-source unification is
  a follow-up.

## 4. Numbers that drove the decisions

| measurement | value |
|---|---|
| query embedding, cold / warm | 283 ms / ~20 ms |
| dense flat scan, 9k rows / 90k rows | 19 ms / 150 ms |
| IVF_FLAT (256 partitions, 50 probes) on 90k rows | 9.4 ms, recall@10 0.995, build 8.9 s |
| templated rerank, 8 / 15 / 30 passages | 82 / 105 / 172 ms |
| Fast TTFT, plain chat, before | 0.9–1.0 s |
| Think/auto on a store-answerable question, before | 17.5–18.9 s |
| baseline retrieval (60 corpus-derived questions): legacy R@5 / MRR / answer@5 | 0.617 / 0.505 / 0.517 |
| dense-only / AND-lexical / OR-lexical R@5 | 0.783 / 0.683 / 0.217 |
