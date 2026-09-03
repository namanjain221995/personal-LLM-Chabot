# Configuration Reference

Started in Phase 1 of the reasoning-modes + code-interpreter mission with
the reasoning knobs; Phase 4 will centralize the remaining subsystems here.
Every value is an environment variable read once at orchestrator startup
(`orchestrator/app/config.py`).

## The effort ladder (2026-08-19 collapse)

| Level | Thinking | Tools | Extra |
|---|---|---|---|
| `fast` | off | none | answers straight away |
| `think` | **unbounded** | agent + search | default level |
| `max` | **unbounded** | agent + search (planning forced with search) | best-of-N with a judge |

Legacy wire values are accepted forever and normalized at the API boundary:
`low → fast`, `medium → think`, `high → think`, `extra_high → max`. Two
deliberate consequences: legacy *low* loses its search-only allowance, and
legacy *high* searches at Think depth (15 sources) — the old High research
depth now lives at Max.

## Unbounded thinking — the trade-off, stated plainly

**Budgets are OFF by default** (`THINKING_BUDGET_MODE=off`): this is a local
deployment with no per-token cost, so thinking runs until the model closes
it naturally. That buys maximum answer quality and costs **variable
latency**: hard questions at Think/Max may reason for **5–20+ minutes**
(the measured decode rate is ~46.6 tok/s single-stream, lower when Max runs
its N candidates concurrently). Nothing cuts a long thought — only two
physical guards exist:

- the **context window** (prompt + 65,536-token completion floor inside the
  262,144 window), and
- the **hang guard** (below), which only catches degenerate repetition
  loops, never real thinking.

**How to watch it live:**

```sh
# The thinking stream in the UI: the "Thinking…" panel updates live.
# Server side — per-generation usage telemetry (chunks ≈ tokens) and guards:
docker logs -f sf-local-ai-orchestrator-1 2>&1 | grep -E "generation usage|WALL CLOCK|best-of"
```

Every thinking generation logs `generation usage: <reasoning> + <answer>
chunks in <seconds>` — with budgets off this is the record of what
unbounded thinking actually costs, and the data any future budget decision
should be made from.

## Reasoning env vars

| Var | Default | Meaning |
|---|---|---|
| `THINKING_BUDGET_MODE` | `off` | `off`: unbounded thinking, no cutoff, no regeneration. `client`: re-enables the Phase 1 client-side enforcement exactly as built. |
| `MAX_OUTPUT_TOKENS` | `65536` | Completion floor for thinking-on requests (streaming, collector, and tools paths), so thinking + answer always fit. |
| `GEN_WALL_CLOCK_S` | `1800` | Hang guard per generation stream: past it the stream is killed, an ERROR is logged, and what was produced is returned with an inline note. 1800 s ≈ 84k tokens at 46.6 tok/s — far beyond any real answer; it exists for degenerate loops only. Also guards each best-of-N candidate via the non-streaming collector. |
| `EXTRA_HIGH_SAMPLES` | `3` | Best-of-N candidates at `max`, generated CONCURRENTLY; a thinking-off guided-JSON judge picks the winner (losers logged at INFO). `1` disables sampling. |

### Re-enabling budgets (if ever needed)

1. Set `THINKING_BUDGET_MODE=client` (and optionally tune
   `THINKING_BUDGET_HIGH` → Think, `THINKING_BUDGET_EXTRA_HIGH` → Max;
   `THINKING_BUDGET_MEDIUM` is retired by the ladder collapse).
2. Restart the orchestrator. Enforcement resumes exactly as built in
   Phase 1: max_tokens grows by the budget, reasoning chunks are counted
   (1 chunk = 1 token on this build), and past budget ×
   `THINKING_BUDGET_GRACE` (1.25) the stream is force-closed and the answer
   regenerates thinking-off on the original ceiling.
3. `SERVER_THINKING_BUDGET` stays `false` on this vLLM build: probed
   2026-08-19 under three key spellings and silently ignored (600/600
   reasoning tokens vs a budget of 64). If a future vLLM upgrade claims
   support, re-run the probe before flipping it, and never with tools
   attached (a server-side cut inside `<think>` can corrupt tool-call
   arguments).

Budget values were derived from the measured decode rate — see the
derivation kept below for the client mode.

### Measured basis (2026-08-19, this DGX Spark)

Decode rate on `Qwen/Qwen3.6-35B-A3B-NVFP4` (vLLM 0.20.1 NGC 26.05,
thinking on, warm, single stream): runs 43.4 and 49.7 → **mean 46.6 tok/s**.
Verified: one streamed chunk = one completion token on this build.

Client-mode budgets (`budget ≈ target_minutes × 60 × 46.6`):

| Effort (canonical) | Env | Tokens | Thinking target |
|---|---|---|---|
| think | `THINKING_BUDGET_HIGH` | 12,000 | ~4.3 min |
| max | `THINKING_BUDGET_EXTRA_HIGH` | 24,000 | ~8.6 min |

### Related pre-existing knobs

| Var | Default | Meaning |
|---|---|---|
| `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` | `8192` | Answer reservation (context budgeting), fast |
| `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` | `16384` | Answer reservation, think and max |


## Research, knowledge and attachment knobs (2026-09-03)

All read by `orchestrator/app/config.py`; every default is the measured
choice on this deployment, and every one is an env var in `.env.example`.

### Deep Research — the loop stops on evidence, the caps are ceilings

| Env var | Default | What it governs |
|---|---|---|
| `DEEP_RESEARCH_MAX_ITERATIONS` | `5` | Ceiling on rounds (search + open + extract + assess). The loop normally stops earlier — see stop reasons below. |
| `DEEP_RESEARCH_MAX_SOURCES` | `36` | Ceiling on pages registered as sources (top 10 keep ~8k chars in the report prompt, the rest 2.5k). |
| `DEEP_RESEARCH_LINKS_PER_ROUND` | `6` | Links opened *from* the pages a round read: the citation an article gives, the official page a summary points at, PDFs. Scored by keyword overlap with the plan, the target's authority, and its source class. |
| `DEEP_RESEARCH_VERIFY` | `true` | The self-correction pass before the report. |
| `DEEP_RESEARCH_MIN_CONFIDENCE` | `0.6` | Below this a subquestion's resolved claim earns one more targeted round. |
| `DEEP_RESEARCH_DUPLICATE_THRESHOLD` | `0.6` | Word-shingle Jaccard above which two pages are the same report (a copy keeps its citation number, corroborates nothing). |
| `DEEP_RESEARCH_MIN_GAIN` | `0.15` | Two consecutive rounds below this share of new evidence stop the loop. |
| `DEEP_RESEARCH_BACKGROUND_CRAWL` / `…_CRAWL_PAGES_PER_DOMAIN` / `…_CRAWL_MAX_DOMAINS` | `true` / `40` / `3` | After the report, the top primary domains are queued for a bounded background crawl. |

Stop reasons (`meta.research_run.stop_reason`, and the `research[…] assess:` log line):
`sufficient` · `no_information_gain` · `duplicate_rate` · `no_new_queries` ·
`iteration_cap` · `source_cap` · `timeout`.

### Background crawl queue

| Env var | Default | What it governs |
|---|---|---|
| `WEB_BACKGROUND_CRAWL_ENABLED` | `true` | The queue as a whole (`web_crawls` rows with status `queued`, drained by the knowledge worker one job at a time). |
| `WEB_SHARE_CRAWL_ENABLED` | `true` | Sharing a URL queues its site. The page itself is always stored in the global corpus. |
| `WEB_SHARE_CRAWL_MAX_PAGES` / `WEB_SHARE_CRAWL_MAX_MINUTES` | `150` / `8` | Per-job caps. Stored pages are free, so a large site finishes over several shares. |

### Living knowledge (Fast mode)

| Env var | Default | What it governs |
|---|---|---|
| `LIVING_KNOWLEDGE_EVIDENCE_CHARS` | `3600` | Characters of passages in a grounded answer (was 900). |
| `LIVING_KNOWLEDGE_TOPICAL` / `LIVING_KNOWLEDGE_TOPICAL_MIN_SCORE` | `true` / `0.4` | Ground a *timeless* question on a stored passage that matches on BOTH signals (vector agreement and the question's words on the page); the score is a floor. |
| `FRESHNESS_FAST_DEADLINE_S` | `8.0` | The most a Fast answer waits for the two-page live lookup (was 12). |

### Attachments

| Env var | Default | What it governs |
|---|---|---|
| `OCR_VISION_DEADLINE_S` / `OCR_VISION_MAX_TOKENS` | `10.0` / `1500` | The image route's OCR pass is time-boxed and capped; past the deadline the answer proceeds from the pixels. PDF scans keep the full budget. |
| `DOCUMENT_PREWARM_ENABLED` / `DOCUMENT_PREWARM_MAX_MB` | `true` / `64` | Extract a document at upload time so the send reads a cache. |

## The knowledge "brain" (ADR-0001, 2026-09-03)

Full rationale in `docs/07-brain/`. Every knob below has a sensible default;
none needs to be set for a normal deployment. Flags are read at process
start (a change = orchestrator recreate).

### One evidence pipeline

| variable | default | meaning |
|---|---|---|
| `KNOWLEDGE_RERANK` | true | the templated cross-encoder judges every candidate passage; its answer probability decides relevance, sufficiency and order |
| `KNOWLEDGE_RERANK_CANDIDATES` | 12 | hybrid candidates judged per time-sensitive question (STATIC: at most 8, only past the pre-gate) |
| `KNOWLEDGE_RELEVANT_THRESHOLD` / `KNOWLEDGE_ANSWER_THRESHOLD` | 0.30 / 0.70 | relevant (may be cited, may retire older evidence) / sufficient (no live lookup) |
| `KNOWLEDGE_LOCAL_FIRST` / `KNOWLEDGE_LOCAL_FIRST_CONFIDENCE` | true / 0.85 | a confident store cancels an auto-decided web search; Think escalates when the store cannot answer a confirmed time-sensitive question |
| `KNOWLEDGE_PREPARE_DEADLINE_S` | 12 | the whole pre-answer stage's budget; past it the answer proceeds ungrounded and the metric says so |
| `KNOWLEDGE_STALE_AFTER_RECENT_S` | 10368000 (120 d) | an answering passage older than this by its OWN date is stale for a RECENT question |
| `KNOWLEDGE_EVIDENCE_CACHE_TTL_S` / `_SIZE` | 60 / 256 | public-scope evidence cache (0 disables); keyed on the corpus generation |
| `RECALL_ASSISTANT_ANSWERS_FOR_FACTS` | false | recall the assistant's own earlier answers for evidence questions (the audited failure) |

### Reranker and embedding backpressure

| variable | default | meaning |
|---|---|---|
| `RERANK_MAX_INFLIGHT` / `RERANK_RESERVED_SLOTS` | 4 / 2 | concurrent scoring calls; slots reserved for the knowledge pipeline's stage 1 |
| `RERANK_WAIT_S` / `RERANK_WAIT_FAST_S` / `RERANK_WAIT_THINK_S` | 1.5 / 0.25 / 1.0 | how long a bulk caller / Fast stage 1 / Think stage 1 waits for a slot before keeping its own order |
| `RERANK_STAGE_TIMEOUT_S` | 2.0 | per-call deadline for stage 1 |
| `RERANK_CANARY_ENABLED` / `RERANK_BREAKER_S` | true / 300 | a fixed query/answer/non-answer triple is scored at first use and each worker cycle; a wrong answer disables the reranker for this long |
| `EMBED_TIMEOUT_S` / `EMBED_BATCH_TIMEOUT_S` | 4 / 90 | read timeouts for query embeddings / index batches |
| `EMBED_MAX_INFLIGHT` / `EMBED_WAIT_S` | 8 / 1.0 | concurrent query embeddings; wait before retrieving lexical-only |

### Vector index policy

| variable | default | meaning |
|---|---|---|
| `WEB_INDEX_ANN_MIN_ROWS` | 50000 | web chunks above which the worker builds an IVF_FLAT index |
| `WEB_INDEX_NPROBES` | 50 | partitions probed per query when an index exists (recall@10 0.995 measured) |
| `WEB_INDEX_OPTIMIZE_EVERY` | 12 | worker cycles between compactions of the web index |
| `KNOWLEDGE_ANN_BYPASS` | false | force flat scans (reader-side rollback, no data change) |
| `RAG_ANN_MIN_ROWS` (sync-worker) | 50000 | Salesforce chunks above which the sync-worker builds its IVF_FLAT index |
| `RAG_OPTIMIZE_EVERY_CYCLES` / `RAG_OPTIMIZE_KEEP_DAYS` (sync-worker) | 12 / 7 | compaction cadence and version retention for the Salesforce table (the retention window is the rollback window for `restore(version)`) |

Operator tools (inside the orchestrator container): `python -m tools.rag_eval`
(retrieval eval), `python -m tools.reindex_web` (build-alongside reindex /
watermark reset), `python -m tools.knowledge_admin` (list / quarantine /
purge shared pages by domain, origin or introducer); on the host,
`scripts/backup-knowledge.sh` before any change to the stores.
