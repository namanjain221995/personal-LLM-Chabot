# Fast / Search / Knowledge / Research — worklog

**Phase:** implementation + isolated verification only. **No commits, no deploy, no training.**
All changes are left **unstaged and uncommitted** in this working tree for review.

---

## Baseline (recorded before any edit)

| Fact | Value | How checked |
| --- | --- | --- |
| Working path | `/home/techsphere/Documents/project/personal-LLM-Chabot` | `git rev-parse --show-toplevel` |
| Branch / HEAD | `dev` @ **`29aa0ab36a41549936dfce061b2333040ac66e1a`** | `git rev-parse` |
| vs `origin/dev` | 0 ahead / 0 behind | `git rev-list --count` |
| Pending changes at start | **0 staged, 0 unstaged, 0 untracked** | `git status --porcelain -uall` |
| Worktrees | exactly one (this checkout) | `git worktree list` |
| Running jobs at start | none | `ps` |

**This is the restored baseline.** The abandoned candidate modules are absent and stay absent:
`core/evidence.py`, `core/fetch_policy.py`, `core/structured.py`, `core/feeds.py`,
`core/historical.py`, `inference_admission.py`, `maintenance.py`, `engines/code.py`,
`research_checkpoint.py`. Orphaned branches (`rescue/*`, `feat/general-ai-*`, `backup/*`) are
**not** consulted; every problem is re-derived against the current files.

### Runtime evidence (read-only, not modified)

| Item | Value |
| --- | --- |
| Main model | `Qwen/Qwen3.6-35B-A3B-NVFP4`, `max_model_len=1000000` |
| Router model | `Qwen/Qwen3-VL-8B-Instruct-FP8`, `max_model_len=49152` |
| Engine queue at baseline | `num_requests_running=0`, `num_requests_waiting=0` |
| Host | 121 GiB RAM, ~18 GiB available, 15 GiB swap in use |

Production containers are **not** touched by this phase.

### Settings that govern the Fast ladder

| Setting | Default | Note |
| --- | --- | --- |
| `LIVING_KNOWLEDGE_ENABLED` | true | pre-pass runs on the assistant path |
| `KNOWLEDGE_PREPARE_DEADLINE_S` | **12.0** | worst-case wait before Fast answers ungrounded |
| `KNOWLEDGE_RERANK` | true | cross-encoder on the pre-pass |
| `KNOWLEDGE_RERANK_CANDIDATES` | 12 | |

Already correct at HEAD and **not** to be "fixed": the knowledge pre-pass is dispatched with
`asyncio.ensure_future` (`main.py:1189`) and awaited under `wait_for(shield(...), deadline)`
(`main.py:1672`), degrading to an ungrounded answer with a `knowledge_degraded_total` metric
rather than hanging.

---

## Task ledger

| ID | Workstream | Owner | Allowed files | State |
| --- | --- | --- | --- | --- |
| T1 | Baseline + eval set | manager | `docs/fast-web-research/**`, `orchestrator/tests/fixtures/web_eval/**` | in progress |
| T2 | Search accuracy audit | inspector (read-only) | — | running |
| T3 | Public-knowledge audit | inspector (read-only) | — | running |
| T4 | Deep-research audit | inspector (read-only) | — | running |

File ownership for later edits (kept non-overlapping):
manager → `main.py`, `context.py`, `llm.py`, `living_knowledge.py`;
search → `engines/search.py`, `web_memory.py`;
knowledge → `core/extract.py`, `web_index.py`, `web_worker.py`, `engines/crawl.py`;
research → `engines/deep_research.py`.

---

## Restart instructions

Baseline revision is `29aa0ab`. To see this phase's work: `git status` / `git diff` (nothing is
committed). To discard it entirely: `git checkout -- <paths>` and delete untracked additions under
`docs/fast-web-research/` and `orchestrator/tests/fixtures/web_eval/`.

---

## 2026-09-06 — implementation round

Baseline unchanged: `dev` @ `29aa0ab36a41549936dfce061b2333040ac66e1a`, nothing staged, nothing
committed, HEAD not moved. Four workstreams ran in parallel on non-overlapping files, each with
its own isolated test database (`ws_search_*`, `ws_know_*`, `ws_research_*`, `mgr_fastweb_*`)
so concurrent pytest runs could not contaminate each other — a mistake made earlier in this
engagement and deliberately designed out here.

### Evaluation set — built BEFORE optimisation, expected values derived by hand

`orchestrator/tests/fixtures/web_eval/` — 7 HTML fixtures + `cases.json` with 15 cases covering
stable facts, changing facts, exact scores, short follow-ups, fresh/stale pages, missing
evidence, multi-source comparison and negative cases. **Every expected value was transcribed by
hand from the fixture HTML, not produced by running the code under test**, so a disagreement
between the file and the code is a finding about the code until the HTML says otherwise.

Two fixtures were designed to fail at baseline, and did:
* `leaderboard_long.html` — the answer row extracts to char **19,831 of 20,136**, past both the
  8,000 and 2,500 character tiers.
* `hosting_costs.html` — a `<dl>` price list that `extract_readable` dropped **entirely**,
  returning 165 chars of surrounding prose and none of its four prices.

### Measurement — and two corrections to my own method

Point 2 asked for first-token latency distinguished from a spinner event. Getting that right
took two corrections, both recorded because they change how the numbers should be read:

1. **The first harness could not measure TTFT at all.** `httpx.ASGITransport` buffers the whole
   response, so all ~22 token events carried the same timestamp and `first_token_ms` equalled
   `total_ms`. Those numbers were completion time wearing TTFT's name. Fixed by running the app
   under uvicorn and measuring over a real socket, where the stream streams.
2. **Wall-clock TTFT is too noisy on this box to support a claim.** Three runs of the *same*
   configuration gave c=1 TTFT p50 of 275 / 705 / 746 ms — a 2.7x spread, larger than the
   effect being measured, because the shared production model dominates. So the headline
   metric is **server CPU per request**, which measures the event-loop work actually changed
   and is stable to ~±13%.

A third defect was in the harness's fixture: it seeded recall chunks on the base conversation id
while requests used per-request ids, so recall scored zero candidates and the cosine change was
not exercised at all in the first runs.

### Fast-mode result (Point 2)

Server CPU per request, 24 requests per level, isolated DB, live model shared:

| configuration | c=1 | c=8 |
| --- | --- | --- |
| legacy shapes | 177.1 ms | 139.2 ms |
| numpy **with BLAS** | 995.4 ms | 307.1 ms |
| current (einsum) | **57.5 / 50.8 ms** | **52.5 / 48.3 ms** |

≈ **65% less CPU per request**. The middle row is the important one: the obvious `matrix @ query`
spelling made things 5.6x *worse*, because OpenBLAS wakes a thread per core and busy-waits.
Measured over a 2 s window in a quiesced process: einsum 23,264 iterations at a 1.00x
CPU-to-wall ratio; `@` 24,337 iterations (4.6% more) billing **31.8 s of CPU for 2 s of wall
time**. On a box shared with vLLM that is pure harm. A regression test pins it.

TTFT moved in the right direction at every level measured but is reported as directional only,
for the variance reason above.

### Dispositions

C1 (answer row never reaches the model) — **fixed and independently verified**: on
`leaderboard_long.html`, `truncate_chars` and `_best_window` both still lose `82.7`;
`select_passages` recovers it at both the 8,000 and 2,500 budgets.

C2 (structured data silently discarded) — **fixed and verified**: `hosting_costs.html` now
yields all four prices, and the negative control still holds (`no_answer.html` yields no score,
so the augmentation pass recovers data rather than inventing it).

K1 (73% of the corpus unrefreshable) — **fixed and verified** against an isolated database:
new pages are scheduled, empty-text pages correctly are not, a worker-set deadline survives
re-upsert, and a V21 backfill heals the stranded rows. **The migration has NOT been run against
production.**

R5 (interrupted research runs stuck at 'running' forever) — **fixed and verified**: closes only
running rows, leaves finished ones untouched, idempotent on restart.

---

## 2026-09-06 — expanded authorization: migration reconciliation

Scope widened by the owner to allow dependency changes, migrations, container rebuilds and
controlled restarts. Still uncommitted, still `dev`, Salesforce untouched.

Full detail in `MIGRATION-RECONCILIATION.md`. Headlines:

* The orphaned V21 was **recovered from git** (`34a7e3c` / `829f17c`), not reconstructed — which
  mattered, because a column-level schema diff had missed that V21 also created an index.
* Identifier 21 is retired permanently; V22 reconciles `extract_version`, V23 backfills
  `next_refresh_at`.
* **The recovery procedure was itself broken and that is why it was tested first:** production is
  PostgreSQL 18.4, the test server is 16.15, and `pg_restore` refused the dump outright. The
  rehearsal moved to the version-matched isolated instance.
* Upgrade-from-V21 and fresh-install converge on byte-identical schemas; the backfill takes
  1,602 stranded pages to 0 on a real restore of production data.

### A gap the wiring left, found by checking rather than trusting

`EXTRACT_VERSION = 3` and the worker's stale-extractor query landed, but only `engines/crawl.py`
passed `extract_version` to the store. `engines/search.py` (two call sites) and `engines/url.py`
did not — and `search` is **1,871 of 2,208 live rows**. Those pages would have stored 0, been
immediately due for re-extraction, re-fetched, stored 0 again: a permanent refetch loop across
most of the corpus. All four call sites now pass it, `GREATEST()` prevents a downgrade, and
`test_every_upsert_web_page_caller_records_the_extractor_version` parses the AST of every call
site so the next omission fails a test instead of shipping.

Verified: legacy page stores 0 → re-extraction moves it to 3 → a write that omits the argument
leaves it at 3.

### A reported failure that was not real

The search engineer reported 5 failing tests and attributed them to pre-existing code, having
re-run against a pristine `git archive HEAD` export and seen the same failures. The failures do
not reproduce: **38 passed** on a correctly configured environment. Cause: its setup used
`set -a && . ./.env`, and `.env` line 251 (`NEXT_PUBLIC_APP_NAME`) holds an unquoted value
containing a space, so the shell aborts that assignment and **22 later settings never load** —
including `UPLOAD_MAX_MB`, which those tests depend on. Its control experiment varied the code
while holding the actual cause constant. Compose reads the file correctly via `--env-file`; only
shell sourcing breaks, so this is a trap for scripts, not a production defect.

---

## 2026-09-06 — second implementation round (from the adversarial critic)

### Deep research: C1 residual and R8

An independent skeptic pass upheld all 18 implemented claims and refuted none, but its
completeness critic found that Deep Research still carried the very defect this phase's headline
fix exists for. Fixing it surfaced something worse.

**The search-path fix made the deep-research defect more likely to fire, not less.** Measured
2026-09-06 on `leaderboard_long.html`:

```
raw page                                  20,136 chars, answer at 19,841
after the FIXED query-centred fetch (8000) 8,000 chars, answer at  7,829   <- pushed to the END
  old _trim_evidence (head slice 2500)     answer present: False
  new _trim_evidence (select     2500)     answer present: True
```

Query-centred selection deliberately concentrates the wanted passage — and because the kept
passages stay in document order, a passage 19 k into the page lands at the end of the kept text.
A later head slice is then close to guaranteed to cut exactly the passage the selection existed to
preserve. A partial fix was worse than none on that path.

The engineer found **three further head slices of the same page in the same file**, none named in
the ledger, and the worst is not `_trim_evidence`:

* `_extract_claims` (2,500) — this excerpt *becomes* the evidence. A fact outside it is never
  extracted, never resolved, never verified, however often the page is cited.
* `_assess` (1,000) — an auditor reading only the lede reports a gap the page actually closes,
  which costs a whole extra round.
* `_verify` (700) — below the ~900-char floor at which selection can reach a distant passage;
  never worse than before, not always enough. Recorded in the code rather than papered over.

All now use the same `_select_text` the search path uses, so the two paths cannot drift.

**R8** — the wall-clock budget now actually bounds plan, each round, the audit, verify and the
report stream, with the partial report preserved and a truthful `stop_reason`. Previously the only
guard under the report stream was `llm.py`'s `GEN_WALL_CLOCK_S` of **1,800 s** — three times a
whole research run.

**It corrected my briefing**: I said `_trim_evidence` had no test; it does
(`test_deep_research.py:563`), which is why the new parameter defaults to the old behaviour and
that test still passes untouched.

Its new tests were checked against the old behaviour by temporarily reintroducing the defects from
a backup and confirming the tests fail, then restoring byte-identically (`diff -q`). Reported
suite at that moment: 2,637 passed, 3 skipped — against a tree another engineer was still editing,
so a definitive run is still owed.

---

## Final state — 2026-09-06

**Git:** branch `dev`, HEAD `29aa0ab36a41549936dfce061b2333040ac66e1a` — unmoved. **0 staged,
0 commits.** 18 tracked files modified, 8 untracked paths added. Content fingerprints of all 48
files in `MANIFEST.sha256`.

**Deployed:** orchestrator only, image `c3e5d6705635`, rollback image
`sf-local-ai-orchestrator:rollback-20260906` (`5b38c1917f8c`). Migrations V22 and V23 applied at
startup; V21 preserved as recorded history. Salesforce sync-worker, all vLLM containers and the
frontend were never touched — original start times, `restarts=0`.

**Verified in production:** schema 23, stranded pages 1,602 → 0, data unchanged (450 conversations
/ 1,856 messages / 2,208 web_pages / 138 uploads), health all-ok, zero tracebacks, robots.txt
honoured against real sites, a real `304 Not Modified` served with no body and no re-embedding.

**Note on `sf-local-ai-vllm-reranker-1 restarts=4`:** pre-existing. It last started
2026-09-05T23:37:54Z, before this phase's deploy, and the orchestrator has made 34 successful
rerank calls since the restart.

**Disposable resources removed** after confirming ownership and zero connections: 11 test
databases (4 on the pg16 server, 7 on the pg18 audit instance). Preserved: the production database
and its `orchtest` / `techsara_test` / `techsara_share_test` neighbours, the 4-day-old
`techsara_test` on the audit instance (not mine), and 163 pre-existing databases on the pg16
server. Kept deliberately: `/home/techsphere/backups/techsara-fastweb-20260906/` (dump + schema
snapshot + migration ledger + rollback manifest, mode 700, outside the repo because it contains
private conversations and uploads).

**Tests added this phase:** 141 across five files — 13 fast-path/refresh, 34 search accuracy,
29 knowledge extraction, 34 deep-research integrity, 31 fetch hygiene — plus 8 fixtures with
hand-derived expected values. Full suite on the version-matched isolated instance with no
competing writers: **2,638 passed, 3 skipped, exit 0**.

---

## 2026-09-06 — slice: extraction reaches stored knowledge + measured Fast performance

### A fabrication the fixture tests could not see

The new scenario benchmark (`plain` / `cached` / `search` / `followup`, real socket, answer
quality checked beside latency) found the Fast path inventing facts on follow-up turns:

```
"what about 5.2?"  -> "GPT-5.2 scored 91.3"                       (the page says 82.7)
"and the B200?"    -> "$3.50 per GPU-hour [1]"                    (the page says $6.75)
"what about 5.2?"  -> "GPT-5.2 is not a publicly released model"  (it is row 12)
```

**Root cause, measured.** Finding S2's follow-up resolution was implemented in
`engines/search.py`. With `web_search="off"` the answer comes from the LIVING-KNOWLEDGE path
instead, and `living_knowledge.prepare` had **no history parameter at all**:

```
"What does an H100 cost per GPU-hour on Orbital Compute?"  1584 chars, 2 sources, 'local'
"and the B200?"                                               0 chars, 0 sources, 'static_model'
"and the B200 price on Orbital Compute?"                   1584 chars, 2 sources, 'local'
```

`_topical`'s gate needs a strong dense score AND lexical overlap — right for a self-standing
question, wrong for a follow-up, which carries one content word, misses both, and falls through
to `static_model`: the model answering from its own memory with no evidence. The $3.50 was not a
retrieval miss, it was **no retrieval at all**.

### The fix, and a second measurement that changed it

`living_knowledge.resolve_from_history` restores the subject from `conversation_turns` (which
drops every pinned system block — this string can become a web-search query) before retrieval,
classification or escalation. **No model call**: the referent is already in the conversation and
this is the latency path.

The first version copied the search path's shape and bracketed the recovered terms. That path
DISPLAYS its resolved string; this one EMBEDS it, and the punctuation was load-bearing:

```
"what about 5.2? (gpt-5 benchlm reasoning)"   dense 0.308   below the 0.35 gate — no retrieval
"what about 5.2? gpt-5 benchlm reasoning"     dense 0.493   passes; cross-encoder answer +1.00
```

Same terms, same order, two characters different — and with the brackets the model invented 89.2.

### Result

| follow-up quality | c=1 | c=8 |
| --- | --- | --- |
| baseline | 16/24 | 18/24 |
| + history resolution | 22/24 | 23/24 |
| + unbracketed retrieval string | **24/24** | **24/24** |

### Fast-mode measurement, all four scenarios (12 conversations per level, real socket)

| scenario | c | turns | err | TTFT p50/p95 ms | total p50/p95 ms | quality | CPU/turn |
| --- | --- | --- | --- | --- | --- | --- | --- |
| plain | 1 | 12 | 0 | 374 / 414 | 1287 / 1379 | 12/12 | 58.3 ms |
| plain | 8 | 12 | 0 | 1323 / 1323 | 2093 / 2229 | 12/12 | 49.2 ms |
| cached | 1 | 12 | 0 | 737 / 817 | 1366 / 1967 | 12/12 | 72.5 ms |
| cached | 8 | 12 | 0 | 1784 / 1784 | 2871 / 3518 | 12/12 | 53.3 ms |
| search (real SearXNG) | 1 | 12 | 0 | 371 / 393 | 1868 / 2458 | 12/12 | 86.7 ms |
| search (real SearXNG) | 8 | 12 | 0 | 1371 / 1371 | 3789 / 4032 | 12/12 | 76.7 ms |
| followup (fixed) | 1 | 24 | 0 | 708 / 973 | 1961 / 4837 | 24/24 | 97.5 ms |
| followup (fixed) | 8 | 24 | 0 | 1351 / 1832 | 4192 / 9375 | 24/24 | 88.8 ms |

Zero errors in 168 turns. **CPU is reported separately and is not part of the latency claim**:
49-98 ms per turn against the pre-phase legacy shapes' 177 ms (c=1) / 139 ms (c=8).

A harness bug was found and fixed in the smoke test before any of this was believed: token frames
are `{"text": "..."}` objects and the accumulator was appending the parsed dict, so every quality
check compared against a stringified dict and reported false misses.

---

## 2026-09-06 — focused step: the deferred ledger items

Session resumed after a network drop. Nothing was lost: `dev` @ `29aa0ab`, 0 staged, 0 commits,
26 modified files and 17 new paths intact; live stack healthy at schema 24 with
`chunker_version 2` and both indexing backlogs at 0. The re-extraction backlog was draining on its
own schedule as designed (12 → 36 pages at `EXTRACT_VERSION 4`), and Salesforce sync-worker, vLLM
and frontend were still untouched since 2026-09-05.

Three engineers dispatched on the gaps deliberately deferred: crawl-frontier durability plus the
`last_changed_at` conflation (K12/K8), deep-research concurrency (R13), and corpus integrity
(K7 orphan vectors, R11 near-duplicates, R12 conflict of interest).

### Manager's own two items

**S5 residual — the knowledge path's sources looked equally used.** The search path has emitted
`read` / `from_store` / `cited` since finding S5; the living-knowledge path emitted every retrieved
source with none of them, so a page that merely matched the query was presented exactly like the
one the answer quoted. `web_memory.as_source` now carries all three (a stored page was read end to
end when fetched and is being served from the store, so those two are true by construction), and
`main.py` stamps `cited` from the answer that was **actually streamed**, not from the model's
intent. Verified end to end: a real answer citing `[1]` produced
`n=1 read=True from_store=True cited=True`.

**K10 re-measured, and it is much bigger than the ledger said.** See FINDINGS.md: the ledger called
it "50 rows"; measured across the whole corpus the 64-chunk cap leaves **26.5% of 57,686,110 stored
characters out of the index entirely**. Raising it to 256 would reach 92.1% coverage for a one-off
+47 s of embedding and +15.8 MB of vectors. Not applied — it changes index size and the retrieval
mix, so it is the owner's call, and the V24 machinery now drains such a re-chunk automatically.
