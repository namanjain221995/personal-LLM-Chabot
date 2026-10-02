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
| `MAX_LOOP_ENABLED` | `true` | Max's plan → draft → check → critique → revise loop (`orchestrator/app/core/max_loop.py`), **Max only** — neither Fast nor Think is affected at any value. It takes the asks that name sections or elements (`max_loop.wants_loop`, decided in code from the contract's own counts, no model call); a short ask keeps best-of-N, whose judge window fits it. `false` = Max keeps best-of-N for everything, and a sectioned Max turn then says so in `meta.effort_degraded` with reason `max_loop_disabled` rather than looking like the loop ran. |
| `MAX_OUTPUT_TOKENS` | `65536` | Completion floor for thinking-on requests (streaming, collector, and tools paths), so thinking + answer always fit. |
| `GEN_WALL_CLOCK_S` | `1800` | Hang guard per generation stream: past it the stream is killed, an ERROR is logged, and what was produced is returned with an inline note. 1800 s ≈ 84k tokens at 46.6 tok/s — far beyond any real answer; it exists for degenerate loops only. Also guards each best-of-N candidate via the non-streaming collector. |
| `EXTRA_HIGH_SAMPLES` | `3` | Best-of-N candidates at `max`, generated CONCURRENTLY; a thinking-off guided-JSON judge picks the winner (losers logged at INFO). `1` disables sampling. A Max turn that does not get the N drafts it asked for says so in `meta.effort_degraded` rather than passing itself off as Max — whether none of them came back, some of them did (`meta.best_of_compared` is how many the judge actually saw, beside the `meta.best_of` that was asked for), or the operator switched sampling off here. The level is the ONLY thing that decides this: the request's `model` value (`smart`/legacy `fast`) resolves to the same weights and no longer gates best-of-N **or the Max loop** or thinking (2026-09-27). A turn that lost more than one thing at once carries the rest in `meta.effort_degraded.also`, a list of `{reason, detail}` pairs, so no downgrade is dropped to report another. |

**Fast never thinks (owner rule, 2026-09-17).** There is no setting for this. A
turn whose `effort` is Fast is marked for its whole life (`llm.mark_fast_turn`,
set by the chat worker), and while that mark is set every main-model request
this process builds sends `enable_thinking` false — whatever effort, `thinking=`
flag or answer plan the caller passed, and with no thinking budget and no
`MAX_OUTPUT_TOKENS` floor. The two PR #71 settings that let a Fast turn think —
the adaptive-thinking switch and the thinking budget it spent — are gone with
the behaviour they configured, so setting either one does nothing; the
classifier in `app/core/effort_policy.py` remains, with no runtime caller
(`git log --grep 'adaptive-thinking'` for their names and why they went). The
public `/v1` API never enters the chat worker and is unaffected.

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
| `RERANK_MAX_INFLIGHT` / `RERANK_RESERVED_SLOTS` | 8 / 4 | concurrent scoring calls; slots reserved for the knowledge pipeline's stage 1 |
| `RERANK_WAIT_S` / `RERANK_WAIT_FAST_S` / `RERANK_WAIT_THINK_S` | 1.5 / 1.0 / 2.0 | how long a bulk caller / Fast stage 1 / Think stage 1 waits for a slot before keeping its own order |
| `RERANK_STAGE_TIMEOUT_S` | 2.0 | per-call deadline for stage 1 |
| `RERANK_CANARY_ENABLED` / `RERANK_BREAKER_S` | true / 300 | a fixed query/answer/non-answer triple is scored at first use and each worker cycle; a wrong answer disables the reranker for this long |
| `EMBED_TIMEOUT_S` / `EMBED_BATCH_TIMEOUT_S` | 4 / 90 | read timeouts for query embeddings / index batches |
| `EMBED_MAX_INFLIGHT` / `EMBED_WAIT_S` | 8 / 1.0 | concurrent query embeddings; wait before retrieving lexical-only |

### Vector index policy

| variable | default | meaning |
|---|---|---|
| `WEB_INDEX_ANN_MIN_ROWS` | 10000 | web chunks above which the worker builds an IVF_FLAT index (lowered from 50000 on 2026-09-14: recall at fixed probes only rises as the table shrinks) |
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

## Dictation (speech to text)

| Variable | Default | What it bounds |
|---|---|---|
| `ASR_TIMEOUT_S` | `600` | How long the orchestrator waits for one engine to answer one clip. |
| `ASR_MAX_AUDIO_SECONDS` | `600` | The longest recording accepted; the composer stops recording at it. |
| `ASR_CPU_BASE_URLS` | empty | The CPU overflow replicas, tried in this order: the worker's, then the head's when it runs (`scripts/whisper-cpu.sh` merges it; `WHISPER_CPU_NODE=head` for the head's copy). Empty: no CPU replica. |
| `ASR_CPU_FIXED_S` | `8.5` | The CPU replica's per-clip overhead in its decode estimate (pre-pass + one encoder window). |
| `ASR_CPU_S_PER_AUDIO_S` | `0.45` | The CPU replica's seconds of decoding per second of audio in that estimate (the slowest measured: Hindi-English long form). |
| `ASR_CPU_DEADLINE_MARGIN` | `1.5` | A clip goes to the CPU replica only when estimate x this fits its deadline. |

**The CPU replica is overflow, never first choice** (docs/voice/CPU-REPLICA.md).
A clip goes to it only when every GPU replica already has a clip in flight (or
is standing down after a failure), and only when `(ASR_CPU_FIXED_S + seconds x
ASR_CPU_S_PER_AUDIO_S) x ASR_CPU_DEADLINE_MARGIN` fits the clip's deadline: the
session window timeout (`VOICE_SESSION_WINDOW_TIMEOUT_S`) for a recording
window, `ASR_TIMEOUT_S` otherwise. A WAV clip's length is read from its header;
a clip of unknown length (WebM on the legacy path) is costed at
`ASR_MAX_AUDIO_SECONDS`. A clip that does not fit is not sent: it waits for a
GPU replica exactly as before. That judgement assumes the replica is free when
the router's own in-flight count says so, and the count forgets a decode whose
caller let go of it (a cancelled call, a read timeout, an orchestrator
restart). So the replica refuses any clip with a 503 while it is decoding,
and the router takes that clip to the GPU queue in the same call: no clip
waits behind a decode on the CPU. While a GPU replica is free the CPU replica is
only the last resort after every GPU replica has failed the same call.
`ASR_CPU_BASE_URLS` lists each replica once (a repeated URL, with or without
a trailing slash, is dropped). The legacy dictation pool lends a CPU replica's
slot only to a clip the router is about to send there, so `ASR_MAX_CONCURRENT`
per GPU replica still holds when the CPU replica cannot take a clip.

**Measured basis for `ASR_TIMEOUT_S` (2026-09-18, the worker Spark).**
Whisper's sequential long-form pass took 0.45 s per second of audio on a
quiet engine (300 s in 132.8 s, 595 s in 268.3 s) and 0.73 s per second with
other clips queued on the same replica (300 s in 219.7 s; the engine decodes
one clip at a time and the wait counts). The old default of 240 s therefore
failed every dictation longer than about 530 s even when quiet, inside the
600 s the UI allows, and because the timeout was treated as an outage the
clip was re-sent to the other replica, which could not finish it either.
360 s would still fail a 600 s clip under load (about 440 s). 600 s — one
second per second of the longest allowed clip — clears the loaded rate by
about 37%, and waiting longer costs nothing now that a timeout is never
re-sent and the route heartbeats.

**A timeout is not an outage.** Only a connection error, a 5xx, or a
connect/write/pool timeout (the clip never fully reached an engine) moves a
clip to the other replica. A read timeout stands the replica down but is not
re-sent, because the engine still holds the clip and a second replica would
decode it in vain. The person gets a 504 worded from the clip's length: "Try
a shorter one" only when decoding it on a busy replica (0.73 s per second of
audio) needs at least half of `ASR_TIMEOUT_S`; a shorter clip met a stuck or
queued engine and is asked to try again. The trade-off, accepted: a short
clip on an engine that accepts connections but never answers now waits the
full `ASR_TIMEOUT_S` instead of being re-sent after it.

**The wait is visible and survives the proxy.** `/audio/transcribe` answers
within 15 s of starting work: a streamed `200` that sends a whitespace byte
every 15 s (leading whitespace is legal JSON) and then the JSON. A failure
after that point is carried in the body as `{"detail", "status"}`. Refusals
known before any work (401, 403, 404, 413, 415, 422, 429, a busy 503) keep
their own status line. A client that hangs up does not cancel the work: the
speech server cannot stop a decode it has started, so the dictation slot
(`ASR_MAX_CONCURRENT` per engine) stays taken until the engine answers — that
slot is the only bound on how much decoding members can queue.

### Stored recordings: the voice archive (2026-09-30)

A finished recording's audio can move from the head's disk to a store on the
worker's disk; transcripts and the list stay on the head. Off unless
`VOICE_ARCHIVE_ENABLED=true`. Design, measurements, deploy steps and runbook:
[`voice-archive.md`](voice-archive.md).

| Variable | Default | What it does |
|---|---|---|
| `VOICE_ARCHIVE_ENABLED` | `false` | Starts the mover (one thread in the orchestrator). |
| `VOICE_ARCHIVE_URL` | empty | The store, `https://<worker management address>:30011`; written by `scripts/voice-store.sh up`. With URL and token set, archived recordings stay playable even with the mover off. |
| `VOICE_ARCHIVE_TOKEN` | empty | The store's bearer token. `.runtime/secrets.env` only, never `.env` or `environment:`. |
| `VOICE_ARCHIVE_TLS_CERT_B64` | empty | The store's self-signed certificate, pinned (no CA bundle). Required for an `https://` URL. |
| `VOICE_ARCHIVE_AFTER_S` | `86400` | How long a finished recording stays on the head before it moves. |
| `VOICE_ARCHIVE_INTERVAL_S` | `60` | Seconds between passes. |
| `VOICE_ARCHIVE_BATCH` | `20` | Recordings copied per pass. |
| `VOICE_ARCHIVE_RATE_BYTES_PER_S` | `20971520` | Copy and read-back pace: 20 MiB/s is 17% of the 1 GbE management LAN (average ping +0.05–0.2 ms at it). |
| `VOICE_ARCHIVE_HOLD_S` | `21600` | A recording brought back for a retranscription or a continuation stays on the head at least this long. |
| `VOICE_ARCHIVE_RESTORE_WAIT_S` | `86400` | How long a continuation waits for a store that is down before it decodes alone. |

## Chat media: stored pictures and lasting files (2026-10-02)

Every picture sent in a chat is kept on the server for the life of the chat
and shows on every device (schema V44, `orchestrator/app/chat_media.py`).
Layout, routes, metrics, rollback and limits:
[`chat-media/README.md`](chat-media/README.md).

| Variable | Default | What it does |
|---|---|---|
| `CHAT_MEDIA_DIR` | `/data/chat-media` | Pictures: `<dir>/<user>/<conversation>/<media_id>/full.<ext>` and `thumb.webp`. On the `/data` volume and outside `WORKSPACE_DIR`, whose 24 h sweep and 20 GB quota would delete them. |
| `CHAT_FILES_DIR` | `/data/chat-files` | Lasting copies of document and dataset originals: `<dir>/<conversation>/<upload_id>/original`. Same volume, same reason. |
| `CHAT_MEDIA_MIN_FREE_GIB` | `250` | Below this much free space on that filesystem, new bytes are refused (507 on `POST /chat-media/{conv}`; skipped and counted on `/chat`, which never fails for it). The project's floor for the head's root NVMe. A value far above the disk size stops new writes without a deploy. |
| `CHAT_MEDIA_REAP_INTERVAL_S` | `3600` | The orphan reaper runs at most this often per process (minimum 60). A deleted chat's bytes are removed at once by the delete route; this is the backstop. |
| `CHAT_MEDIA_ORPHAN_GRACE_H` | `24` | How old a row or directory with no owning chat must be before the reaper removes it (minimum 1). `/chat` stores a picture before the browser's first history push creates the chat's row. |
| `IMAGE_MEMORY_STORE_FALLBACK` | on | Follow-up questions read the chat's stored picture when the 2 h V41 row is gone (expired, or a restart). `0` restores the behaviour before V44. |
