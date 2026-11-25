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

## Toggle-off isolation (2026-11-25)

With the Salesforce toggle off, no Salesforce answer, nothing written from one
and no company people detail reaches the prompt, and the assistant never
invents a person's phone or e-mail (`orchestrator/app/sf_isolation.py`,
`orchestrator/app/core/personal_details.py`). Read-time filters only: no stored
row is deleted or rewritten. The routing hardening (only a Salesforce-mode
request reaches a Salesforce engine; a request that names no mode is an
assistant-mode request) has no flag.

| Variable | Default | What it does |
|---|---|---|
| `TOGGLE_OFF_ISOLATION` | `true` | On a toggle-off turn: earlier Salesforce answers and questions, answers written after one before this guard, and quotes that are not clean text of the chat become placeholders in the history; the summary, folded chunks and compaction are skipped in a chat that holds Salesforce (a stored Salesforce answer, or a server row of a Salesforce-mode turn, stored answer or not); cross-chat recall leaves such chats out; artifacts made by a Salesforce-mode job are not offered; answers carry the `sf_isolation` stamp, which only the server writes, and a history PUT keeps it and `mode` by position. Also stops embedding NEW rows of a chat that holds Salesforce. `false` restores the behaviour before it (answers made while off count as unguarded later, which only hides more). |
| `TOGGLE_OFF_MASK_PERSONAL_DETAILS` | `true` | A phone number or e-mail in a toggle-off chat answer (and its reasoning, and the best-of judge's reason) that the prompt did not hold is shown as `[phone not available]` / `[email not available]`; meta `personal_details_masked`, metric `toggle_off_personal_details_masked_total`. Package versions, git and SSH targets, decimals, salary ranges, identifiers and counts are not masked. |
| `FACTS_BLOCK_THIRD_PARTY_CONTACTS` | `true` | Owner decision 1, in every mode: another person's phone, e-mail or address is never saved as a memory fact (nor in its `source_excerpt`), and one already saved is masked before facts reach a prompt; a fact the extractor saw masked is never rewritten. The person's own details stay. |
| `TOGGLE_OFF_PROMPT_RULE` | `true` | The rule against invented personal details in every assistant-mode answering prompt (chat, agent step, fallback and synthesis). `false` sends those prompts exactly as before the rule. The wording awaits the owner-approved model check. |
| `TOGGLE_OFF_DETECTOR_RULES` | `ABCD` | Which rules may answer a toggle-off turn with a fixed reply before any model call: A a lookup of the person's own company records (first person or a recent window of time; not code, a how-to or their own life), B takes Salesforce or the CRM as the source (not a product question), C a person's details by company role (of "our/my/this" person or a named one), D a short follow-up on a hidden answer (not one that points at a file). A, B and C never fire for a member without the Salesforce toggle. Empty, `off`, `none`, `false`, `0`, `no` or `disabled` turns A-D off; other characters are ignored. A quote of kept-out text (E) is always answered with a fixed reply and has no switch. Metric `toggle_off_needs_salesforce_total{rule}`. |
| `TOGGLE_OFF_STATE_TIMEOUT_S` | `2.0` | How long a toggle-off turn waits for the chat's Salesforce state. A slower or failed read counts as failed: for a member with Salesforce every earlier assistant turn is hidden on that turn, with its own status line and no "came from Salesforce" reply; for a member without it the context stays as sent. |

Other metrics: `toggle_off_hidden_turns_total{kind=sf|tainted|quote}` and
`toggle_off_state_read_failed_total{closed}`. A change needs an orchestrator
restart, like every flag here.

## Salesforce conversation memory (2026-11-25)

On a toggle-on sf_assistant turn, the bridge finds the stored plan of the
answer the person is replying to, sends it to sf_assistant with the recent
turns (contract v2), and stores the plan sf_assistant returns for the next
turn (Salesforce memory design of 2026-11-25, §2-§4, §11, §15). The plans
live in the table `sf_query_state` (migration V45), one row per answer, and
are deleted with their chat and their user. Nothing of it reaches the
browser, and a toggle-off turn never reads, writes or folds it. Every switch
is off by default: with all of them at their defaults the platform behaves
as before (v1 calls, no row read or written, no summary fold). A value other
than `true`/`false` (`1`/`0`, `yes`/`no`, `on`/`off`), or a non-number for a
number, stops the orchestrator at start-up with a message that names the
variable.

| Variable | Default | What it does |
|---|---|---|
| `SF_ASSISTANT_MEMORY` | `false` | The state lookup before an sf_assistant call, the contract-v2 arguments (parent `state`, `gap`, `summary`, up to 12 recent turns) and the row written after the answer is stored. `false`: v1 calls exactly as before, and the bridge never reads or writes `sf_query_state` (the retention sweep, when `CHAT_RETENTION_ENABLED` is on, still counts expired rows, and removes them once `CHAT_RETENTION_DRY_RUN` is `false`). While a service from before contract v2 refuses the call, the bridge asks with the v1 arguments for 10 minutes, apart from one turn a minute that tries contract v2 again. |
| `SF_ASSISTANT_MEMORY_PAIRS` | `6` | Whole (question, answer) pairs the bridge sends, newest first; a question no answer followed (a send that failed) is sent with the pair after it, or last. Held to 1-6: contract v2 takes at most 12 turns. Never fewer turns than the v1 call sends (the last 4), whatever this and the token budget say. |
| `SF_ASSISTANT_MEMORY_TOKENS` | `6000` | Token budget of those pairs (`context.estimate_tokens`); whole pairs are dropped from the oldest end, down to the pairs that hold the last 4 turns. sf_assistant applies its own cap (`SFA_MEMORY_TOKENS`) again. Minimum 1. |
| `SF_ASSISTANT_SUMMARY` | `false` | Folds each Salesforce pair that leaves the window into a short summary stored on the answer's row, in the background, with the platform's local main model only (`app/sf_summary.py`). Built from the person's own questions and code-written plan lines, never from answer text; only sf_assistant's follow-up resolver reads it. |

## Chat retention (2026-11-25)

One sweep on the maintenance loop, real work once a business day under an
advisory lock (`orchestrator/app/retention.py`; the approved periods, the
dry run, the backup before the first real run and the rollback are in
[`docs/retention.md`](retention.md)). Off by default. Turned on, it is a dry
run that only counts and logs until `CHAT_RETENTION_DRY_RUN=false`. The same
start-up rule applies to these values.

| Variable | Default | What it does |
|---|---|---|
| `CHAT_RETENTION_ENABLED` | `false` | Runs the sweep. `false`: it touches nothing, not even a count. |
| `CHAT_RETENTION_DRY_RUN` | `true` | Counts what each rule would delete (log line `retention dry-run: …`, gauge `retention_candidates{table}`) and deletes nothing. `false` deletes, after the backup in `docs/retention.md`. |
| `QUERY_TRACES_RETENTION_DAYS` | `180` | `query_traces` (and their events) older than this, plus traces over a day old whose chat is gone (a bare-API key's traces and test-case traces are spared). A value below 180 is raised to 180 with a warning: a shorter period needs the owner's word and a code change. |
| `CHAT_REQUESTS_RETENTION_DAYS` | `30` | `chat_requests` older than this in a finished status (completed, failed, cancelled), plus finished requests over a day old whose chat is gone (bare-API keys are spared). A resumable request is counted and logged, never deleted. A value below 30 is raised to 30. |
| `CHAT_RETENTION_BATCH` | `1000` | Rows per delete statement (held to 1-10,000); one transaction per batch. |
| `CHAT_RETENTION_MAX_BATCHES` | `20` | Batches per rule per run (held to 1-1,000). A run that stops at the cap leaves the day open, and the next tick goes on. |
| `CHAT_RETENTION_OLD_ROUTE_ORPHANS` | `false` | Owner decision D9, on the owner's word: also delete rows of the older Salesforce route (`sf_conversation_state`, `sf_intents`, `sf_clarifications`) whose chat is gone. A dry run counts them either way. |
| `CHAT_RETENTION_BACKUP_DATE` | empty | The day (`YYYY-MM-DD`) the pre-prune backup was taken. While it is more than 30 days old, every daily run logs a reminder to delete that backup (D11). |

Metrics: `retention_candidates{table}`, `retention_deleted_total{table}` and
`retention_runs_total{result}`. Log lines carry counts only, never content.
A change needs an orchestrator restart, like every flag here.
