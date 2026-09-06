# Findings ledger — Fast mode, search accuracy, public knowledge, deep research

Baseline `dev` @ `29aa0ab36a41549936dfce061b2333040ac66e1a`. Nothing here is committed.

Three read-only inspectors audited the current code (no live runs, no deployments, no LLM calls
on the search inspector). Their findings are recorded with the evidence that supports each one,
and separately from the ones I reproduced myself. **Where an inspector's claim is inference
rather than observation, it is marked so.** Inspector output is analysis, not authority; each
item below is disposed of on evidence.

## Convergent findings — found independently by more than one inspector

These carry the most weight: two inspectors working different files reached the same conclusion,
and for C1 I reproduced it a third time on my own fixture.

### C1 (Critical) — the answer row in a long table never reaches the model
Three independent observations of one failure:

* **Live search path head-truncates.** `engines/search.py:652` and `:594` call
  `extract.truncate_chars(...)`, a pure head slice (`core/extract.py:157-165`). Tier B is 2,500
  chars (`search.py:88`); on `max` effort that is 50 of 60 sources.
  `web_memory._passages` fixed exactly this on 2026-09-04 with `_best_window`
  (`web_memory.py:492-521`), whose docstring records *"11 of 60 eval answers were lost to head
  truncation alone, McNemar p < 0.001"* — and `engines/search.py` never imports it.
* **The store path's window picks the wrong part of a table.** On a 200-row fixture the
  knowledge inspector measured `_best_window` returning rows 1-50 for a query whose answer is at
  rank 137: the repeated header row scores highest because it contains every query term.
  `web_index.chunk_page` separately puts the header in chunk 0 and the answer in chunk 2, so a
  retrieved chunk is bare numbers with no column meaning.
* **Reproduced by me, on my own fixture** (`tests/fixtures/web_eval/leaderboard_long.html`,
  2026-09-06): the target row extracts to **char 19,831 of 20,136** — dropped at both the 8,000
  and 2,500 char tiers.

Why it is severe: the page is fetched, cited in the panel, and handed to the model with the
answer sliced off. The model then correctly reports non-coverage, and the user reads that as
"not ranked" — a fabricated negative delivered with a full citation panel behind it.

### C2 (High) — extraction silently discards non-`<table>` structured data
The knowledge inspector measured headings and card blocks lost, identical across four
trafilatura configurations. **I reproduced the same class of loss on a different element**: a
`<dl>/<dt>/<dd>` price list is dropped *entirely* — `hosting_costs.html` extracts to 165 chars
containing the surrounding prose and none of its four prices, under every option combination
(`include_tables`, `favor_recall`, `no_fallback`, `output_format="markdown"`).

The failure mode is the dangerous one: the prose survives, so the page reads as successfully
fetched and is cited, while carrying none of its data. Because it is identical across kwargs it
is trafilatura's precision filter, so the fix must be an augmentation pass, not a flag.

Pipe-table rows *do* survive — extraction is not prose-only. Card `<div>`s survive in my fixture
but with label and value concatenated (`Reasoning93.4`, `tok$12.00`), which corrupts tokenisation.

## Point 3 — search accuracy (inspector: search)

| # | Sev | Finding | Location |
| --- | --- | --- | --- |
| S1 | Critical | Head truncation on the live search path — see **C1** | `engines/search.py:652,594,714,815` |
| S2 | High | A terse follow-up is resolved for SearXNG **only**. `rewrite_queries` output is used solely at `search.py:986`; the reranker, TTL, stored-evidence retrieval and the `Question:` line of the answer prompt all get the raw phrase. No anaphora resolution exists anywhere (`grep coref\|referent\|resolve_followup` → nothing). Measured: `"but what is its score?"` → `['but','score']`; `"and the price?"` → `['price']` | `engines/search.py:250-281, 741, 948, 999, 1004, 1013` |
| S3 | High | No coverage check on the live path — absence is indistinguishable from "not present". The store path *has* `_answerability`; it is unreachable from `run_search_engine` | `engines/search.py:984-1036` |
| S4 | High | Version/variant tokens deleted. `_WORD=[a-z0-9]+` plus `len(w)>1` reduces `GPT-5.2` → `['gpt']` and `3.14.5` → `['14']`. `_collapse_duplicates` already documents this trap and works around it locally | `web_memory.py:431,466-483` |
| S5 | Med | Citation markers unverified; `meta.sources` is the *fetched* set, not the used set; a failed fetch is silently downgraded to a search snippet and rendered identically to a read page | `engines/search.py:668-676, 718-722, 1038-1047` |
| S6 | Med | SearXNG degradation invisible: `unresponsive_engines` never read; partial query failure emits no status/meta; a degraded result is still cached for 900 s | `search/searxng.py:44`, `engines/search.py:381-398` |

Runtime corroboration for S6, read-only from the SearXNG container: the most recent real query
(00:10 today) logged `wikipedia`, `duckduckgo web` and `yandex` engine timeouts against a 3.0 s
timeout. **The last observed search ran on a materially reduced engine pool and the application
recorded none of it.**

## Point 4 — public knowledge (inspector: knowledge)

| # | Sev | Finding | Evidence |
| --- | --- | --- | --- |
| K1 | **Critical** | `next_refresh_at` is absent from the `upsert_web_page` INSERT column list, so nothing ever schedules a new page. Live: origin `research` 123/123 NULL, `crawl` 214/214 NULL, `search` 1265/1871 NULL → **1,602 of 2,208 pages (73%) can never be refreshed**. The 606 scheduled rows are exactly the V13 backfill survivors | `db.py:1726,1802-1806,817` |
| K2 | Critical | `_best_window` returns the table head — see **C1** | `web_memory.py:492` |
| K3 | High | `chunk_page` splits the table header from its rows — see **C1** | `web_index.py:64` |
| K4 | High | Extraction drops cards/headings — see **C2** | `core/extract.py:100,138` |
| K5 | High | **No conditional requests anywhere.** Repo-wide grep for `If-None-Match`/`If-Modified-Since`/`304` = 0 hits. `etag` (265 rows) and `last_modified` (366 rows) are written and never read; `web_worker.py:18-22` documents 304 handling that cannot happen. Every refresh is a full re-download and re-extract | `core/net.py:359-366`, `engines/search.py:1150` |
| K6 | High | **Robots is checked only on the crawl path.** The search-result read (`search.py:612`), pasted-link read (`url.py:194`) and `refetch_page` (`search.py:1150`) — i.e. the entire refresh worker — fetch with no robots check and no politeness delay. All six paths *do* go through SSRF-guarded `safe_fetch` | `engines/crawl.py:281,305` |
| K7 | High | Purge leaves orphan vectors: `--drop-vectors` is opt-in, so a default purge leaves LanceDB chunks that `web_index.retrieve` still returns. No in-app quarantine path exists at all (live `quarantined_at` count: 0) | `tools/knowledge_admin.py:203,227,359` |
| K8 | Med | `last_changed_at` conflated with first-seen for **1,338/2,208 (61%)** of rows, systematically over-rating freshness | `db.py:816,1802` |
| K9 | Med | No store-time quality gate: 263 rows under 400 chars stored as `fetch_status=200`, incl. a 4-char page | `engines/crawl.py:243,246,313` |
| K10 | Med | `_MAX_CHUNKS_PER_PAGE=64` = a hard 204,800-char index ceiling; 50 live rows exceed it and are silently half-indexed | `web_index.py:55` |
| K11 | Med | Sitemap `<lastmod>` — the one free freshness signal a site publishes — is parsed away | `engines/crawl.py:87` |
| K12 | Low | Crawl frontier is in-process only; a resume after 24 h re-fetches everything | `engines/crawl.py:283,396,123` |

**The privacy boundary holds — verified, not assumed.** `web_pages` has no owner column and the
public entry point takes no viewer, so cross-chat/cross-user reuse works as intended. All four
`upsert_web_page` callers take their body from `safe_fetch` of a public URL that passed the SSRF
blocklist; uploads and pasted documents go to conversation-scoped tables; `web_claims` has one
writer; Salesforce uses its own client and never reaches `upsert_web_page`. **No path found from
private data into the shared corpus.** Residual (not a retrieval leak): `introduced_by_user_id`
on a globally-readable row means DB/CLI access reveals who introduced a page.

## Point 5 — deep research (inspector: research)

The cycle is a genuine bounded loop, not search-then-summarise: plan → gather → rank → fetch →
register → follow links (1 hop) → persist/index → extract claims → resolve → assess → follow-up
→ stop-check → verify → trim → report → validate citations. Budgets exist at every level and
each limit produces a clean break with a `stop_reason` and a useful partial report.

| # | Sev | Finding | Location |
| --- | --- | --- | --- |
| R1 | **Critical (privacy)** | The report writer is fed the user's pinned memory blocks. `_report_messages` uses `recent_turns`, which keeps every system message — cross-chat recall, saved facts, shared-page excerpts. `_conversation_turns` was written for exactly this hazard and is used in `_plan` but not here. A saved personal fact can be written into a report that is exported and shared | `deep_research.py:1672` |
| R2 | High | An auditor outage is reported to the user as "sufficient evidence": `_assess` returns `{"sufficient": True}` on **any** exception, so `stop_reason` becomes `"sufficient"` and the step detail says "no gaps found" | `deep_research.py:1378-1381, 1426` |
| R3 | High | Resolved values are never checked against source text before the report. `_quote_in`, the module's own verbatim matcher, runs only in `_persist_claims` — *after* the report is written. A value no source states ships with a real `[n]` link and a confidence number | `deep_research.py:1769, 1874` |
| R4 | High | Cancellation destroys the run record: `_close_run(..., "", [], ...)` discards report and sources, so stopping at minute 9 of 10 leaves a row reporting 0 sources | `deep_research.py:2321` |
| R5 | Med | No restart reconciliation for `research_runs` — interrupted runs stay `'running'` forever. `web_crawls` has `requeue_interrupted_web_crawls`; `research_runs` has no equivalent | `db.py:2260`, `web_worker.py:254` |
| R6 | Med | A mid-run SearXNG outage is completely silent (`except SearchUnavailableError: continue`, no log, no counter) — a thin report is attributed to a mined web | `deep_research.py:979` |
| R7 | Med | A weaker disagreeing source is silently relabelled `superseded`, and the report then presents an open disagreement as a settled change over time with a temporal story no source stated | `deep_research.py:1294-1296, 1656` |
| R8 | Med | The wall-clock budget is advisory — checked only between rounds, nothing wraps a round/fetch/LLM call/report stream | `deep_research.py:2137,2207` |
| R9 | Med | Report truncation is silent; `llm.py` has no `finish_reason` handling at all | `deep_research.py:2243` |
| R10 | Med | Gaps found in early rounds are discarded — `state.missing` is reassigned, not accumulated | `deep_research.py:2148-2151` |
| R11 | Low | Near-duplicate detection fingerprints only the page opening, so a rewrite or an aggregator with its own lede counts as independent corroboration (+0.25 confidence) | `core/provenance.py:480` |
| R12 | Low | A publisher's own leaderboard gets every authority bonus and no penalty; no conflict-of-interest notion exists | `core/provenance.py:447` |
| R13 | Low | `_RUN_LOCK` is global to all users and refuses rather than queues: one person's 10-minute run fails every other user's request org-wide | `deep_research.py:109, 2001-2012` |

## Explicitly unverified

Carried forward from the inspectors so nothing is over-claimed:

* No live Deep Research run, no live search, no tests executed by any inspector.
* The **fraction** of the live corpus affected by C1 is unmeasured (the read-only `psql` count was
  blocked); the mechanism is proven by fixture, the prevalence is not.
* R9 rests on a grep for `finish_reason` returning zero hits, not on reading the streaming code.
* R12's client half ("the client persists the raw stream") is inferred, not confirmed.
* Whether the refresh worker is actually running in production was not checked.
* K7's orphan-vector claim is inferred from the code, not observed in `/data/lancedb-web`.
* Existing tests may assert current behaviour; C1/S4 fixes may require updating an expectation.

## Discovered during implementation — schema drift between the live database and the restored source

**The production database is one migration ahead of this checkout, and the extra migration's DDL
is not in the source tree.** Found while verifying my own migration; it is a pre-existing
condition, not something this phase created.

* `schema_migrations` in the live `techsara` database records **version 21, applied
  2026-09-05 04:59:59Z** — the day before this phase began.
* `git show HEAD:orchestrator/app/db.py` contains no V21: the restored source stops at V20.
* Diffing the live schema against a fresh database built from this source shows exactly one
  difference: **`web_pages.extract_version smallint NOT NULL DEFAULT 0`**.
* All 2,208 live rows hold the default `0`, and **no file in this checkout references
  `extract_version`** — it is an orphan column.

So the abandoned candidate work applied a migration to production; the Phase 5 restoration
reverted the code but a database migration cannot be un-applied by restoring source. The column
itself is harmless — `NOT NULL DEFAULT 0` means existing inserts keep working, which is why
nothing has failed.

**The dangerous part is the version number, not the column.** `init_schema` skips any version
already present (`if version in applied: continue`, `db.py:1427`). A new migration numbered 21
would therefore be *silently skipped in production*: it would report success, apply nothing, and
leave the defect it was written to fix in place. This phase's refresh backfill was originally
numbered 21 and is now **V22** for exactly that reason, with the reason recorded in the migration
itself so nobody renumbers it back.

Two things the owner should decide, both outside this phase's scope:

1. Whether to adopt `extract_version` deliberately (the knowledge audit lists a per-page
   extractor version as a genuine provenance gap — K-provenance) or to drop the orphan column.
2. Whether any *other* deployed-but-unrestored behaviour exists. One column is what the schema
   diff can see; it cannot see data written by code that no longer exists.

## Adversarial verification of the implemented fixes

Every implemented fix was re-checked by an independent skeptic instructed to REFUTE it, defaulting
to "refuted" on ambiguity, and required to quote the file:line or probe output it relied on rather
than take the implementer's word.

**18 claims upheld, 0 refuted, 0 inconclusive** — S1, K2, S4, S3, S2, S5, S6, S-perf, R1, R2, R3,
R4, R6, R7, R10, M-cosine, M-tokenize, M-migration.

Two of the critic's findings were already fixed by the time it reported (the `extract_version`
call sites, and the migration-gap test breakage) — it snapshotted mid-flight. The rest stand:

### The most important one: this phase's own fix creates a load problem

**K1 makes K5 and K6 urgent, and nothing had connected them.** V23 schedules 1,602 previously
unreachable pages inside a 24h window; the refresh worker runs 8 pages per 300 s = **2,304 fetches
a day**. With K5 (conditional requests) and K6 (robots on the refresh path) still unimplemented,
every one of those is a full unconditional GET with no `If-None-Match` and no robots check.
Applying the migration as things stood would have converted a dormant defect into ~2,300 impolite
third-party downloads a day. **This now gates the deployment.**

### Still open after the implementation round

| # | What | Why it matters |
| --- | --- | --- |
| C1-residual | `deep_research._trim_evidence` still calls `truncate_chars` | On a 24-source run, sources 11-24 keep the exact head-slice defect the phase's headline fix exists for. It has no test at all. |
| perf | `_coverage_gap` re-tokenises the whole ~205k-char assembled corpus **twice per search**, once **before the first token** | 38.6 ms measured, about half in front of the first answer token, on the event loop — and redundant, since `select_passages` already tokenised those pages. Directly contradicts goal 2. |
| S4-risk | The new `_WORD` changes what reaches PostgreSQL | `_content_words` feeds `websearch_to_tsquery`; `gpt-5.2` now arrives as one hyphenated compound, so the AND-first lexical half may stop matching a page that writes "GPT 5.2". **No test covers the tsquery path at all.** Also measured ~60% slower than the regex it replaced. |
| R8 | Wall-clock budget still advisory | Nothing wraps a round, fetch batch, LLM call or the report stream; a nominal 10-minute run can take 20+ while holding a process-wide lock. |
| K7, K8, K10-cap, K12, R11, R12, R13 | Not implemented | Verified absent by unmodified files and zero grep hits, not assumed. |
| header-rescue | The table-header repair matches pipe tables only | The card and `<dl>` renderings this same phase added extraction for get no header. |
| S-perf | One-pass tokenisation has **no test** | Reverting `_term_positions` to per-window `_terms()` — the 65 ms shape its own docstring warns about — passes every test in the tree. |
| goal-2 evidence | Weakest of the four | The bench never performs a web search, so it exercises only `recall.cosine_many` and `context._tokenize_client`, not the search-path work that also runs on the Fast path. Goals 3/4/5 each have 40+ assertions against hand-derived fixtures; goal 2 has one bench script. |

### Carried over from the knowledge engineer

* **`core/structured.py` is not fully replaced.** The augmentation pass covers tables, card lists
  and headings but **not JSON-LD / microdata** — `<script type="application/ld+json">` is stripped
  wholesale, and that is often the only machine-readable place a price or date is stated.
* **Chunks already in LanceDB keep header-less rows.** Re-chunking happens only when a page's text
  changes, so a table page the new extractor does not alter keeps its old chunks. Needs an operator
  `tools/reindex_web.py` run.
* **`CHUNKER_VERSION` is still 1** though chunk shape changed; bumping it breaks two tests in a
  file that engineer did not own.
* **K10's ceiling in the ledger was wrong** — it is 179,600 chars, not 204,800, because the window
  advances by `chunk − overlap`. The cap was made observable rather than raised; raising it costs
  ~320 ms of embedding and ~256 KB of vectors per oversized page, for 50 rows. Owner's call.
* **A pre-existing unbounded call:** `trafilatura.extract_metadata` did not return within **280 s**
  on a synthetic 3.6 MB / 90k-element page. Probably beyond `fetch_max_bytes`, but it is unbounded
  on the extract pool.

## K10 re-measured — the chunk cap is far more significant than the ledger said

The ledger recorded this as "50 live rows exceed the ceiling and are silently half-indexed", which
made it sound marginal. Measured against the whole live corpus on 2026-09-06 (2,208 pages,
57,686,110 characters of stored text, chunk 3,200 / overlap 400 / stride 2,800):

| `_MAX_CHUNKS_PER_PAGE` | chunks | vs today | corpus coverage | one-off embedding | extra vectors |
| --- | --- | --- | --- | --- | --- |
| **64 (today)** | 16,125 | — | **73.5%** | — | — |
| 128 | 18,536 | +2,411 | 85.1% | +29 s | +9.9 MB |
| 256 | 19,990 | +3,865 | 92.1% | +47 s | +15.8 MB |
| 512 | 21,080 | +4,955 | 97.4% | +60 s | +20.3 MB |
| 1024 | 21,622 | +5,497 | 100.0% | +67 s | +22.5 MB |

**More than a quarter of everything this platform has stored is not in the index at all.** The
worst individual pages:

```
92.4% lost  2,188,344 of 2,367,944  sec.gov/Archives/edgar/...
91.1% lost  1,834,020 of 2,013,623  drwho.virtadpt.net/drwho.plan.txt
87.4% lost  1,245,628 of 1,425,228  dataswamp.org/~solene/index-full.html
84.1% lost    951,577 of 1,131,177  research.swtch.com/feed.atom
```

The cost of fixing it is small because the embedding rate (measured 12.2 ms/chunk against the live
service) applies to a one-off backlog, and 4 KB per vector is cheap. **Recommendation: 256** — 92%
coverage for 47 seconds of embedding and 16 MB.

Not applied: this changes index size and the retrieval mix (a single large page could contribute
256 chunks against a 7.3 average), so it is the owner's call. It is one constant plus a
`CHUNKER_VERSION` bump, and the V24 machinery now drains the re-chunk automatically.

## Corpus integrity round — K7, R11, R12

### An unlisted leak, found beyond the brief

`engines/crawl.site_hits_for` calls `web_index.retrieve` **directly** and renders chunk text into
an answer with no PostgreSQL round trip. `web_memory` drops quarantined rows in SQL; that path
never did. **A quarantined page was still being answered from, and a purged one still cited** —
the row was gone and its vectors still served. `retrieve` now asks PostgreSQL which page ids may
be served, at a measured 0.40 ms for 36 ids, in a worker thread, after the distance floor so only
returnable hits are looked up.

Verified independently by the manager on an isolated store: indexing two pages returns both;
quarantining one withholds it; deleting the other's row withholds it too — `retrieve` returns `[]`
for a corpus whose vectors are all orphaned.

### K7 — a purge is now whole

Dropping vectors is the DEFAULT; `--keep-vectors` is the deliberate exception and says what it
left; `--drop-vectors` still parses as a documented no-op for runbook compatibility. **A failed
vector delete now exits non-zero** — the old code printed the failure and returned 0, reporting a
clean purge over exactly the orphaned state K7 describes. The operator quarantine path gained
`--url`, the handle an operator actually holds (a link out of a citation panel).

`web_index._open()` now refuses any web directory that is, contains, or sits inside
`settings.lancedb_dir`, in both directions. `LANCEDB_WEB_DIR` is an environment variable, and a
typo in it pointed every write in that module — deletes included — at the Salesforce corpus.
Verified inert for production: the live pair does not overlap either way.

**The manager extended that guard to `health._check_web_index`**, which opens LanceDB itself and so
did not inherit it: an overlapping directory would have been refused by every write path and
reported healthy by `/health`. It now reports `misconfigured` with the reason. `status` is
deliberately not flipped — the container healthcheck gates on it, and this is a configuration
fact, not a dependency outage.

### R11 — fingerprint the page, not its opening

`_SHINGLE_CHARS` 20,000 → 60,000 (measured 1.67 ms → 5.03 ms per page; the retrieval caller
fingerprints 3,200-char windows, so its cost does not move), plus a third rule: ≥200 shared 6-grams
AND ≥50% of the shorter fingerprint. Measured on purpose-built fixtures:

```
wire ~ aggregator rewrite   shared=250  jaccard=0.391  containment=0.568  duplicate=True  (was False)
wire ~ independent report   shared= 31  jaccard=0.052  containment=0.166  duplicate=False
wire ~ unrelated page       shared=  0  jaccard=0.000  containment=0.000  duplicate=False
```

**A deliberate bias, recorded rather than hidden:** two genuinely independent articles that each
reproduce the same ≥200-word statement, where that statement is more than half of each, are also
called duplicates. The engineer probed for a separating signal and found none (two articles sharing
a 300-word quote sit at containment 0.60 against 0.568 for the real rewrite). The error direction
was chosen: a false duplicate withholds 0.25 confidence; a missed one grants 0.25 unearned.

### R12 — a publisher grading its own work

`primary_weight(url, kind, authority, entities)` compares the registrable name in the publishing
host against the entity a claim is about. **Implemented and tested; the call site belongs to
`engines/deep_research.py` and has been handed to that engineer.** `primary_weight` with no
entities is exactly `is_primary` as a float, so nothing re-ranks silently.

What it cannot detect, stated in its own docstring: **ownership** (`youtube.com` for Google,
subsidiaries, wire services, a review site the vendor owns — no ownership graph exists);
**products** (a claim about "GPT-5.2" on `openai.com` registers only if the plan also named
OpenAI); and **intent** (self-publication is not dishonesty — a vendor's API reference is the right
source for its own API, and this returns True for it).

Bounded deliberately: `official` and `academic` keep the full weight regardless of self-interest —
the statistics-office and university-publishing-its-own-paper cases. Known residual, pinned as a
test: a standards body on a `.org` is demoted for a claim about itself, costing half of one bonus
term (0.1 → 0.05 on a 0.05–0.98 scale) while keeping `is_primary`, authority, rank, link score and
citation.

## Crawl durability round — K12, K8

### K12 — a durable frontier, keyed on the right thing

`web_crawl_frontier` (V25) is keyed by **`scope_prefix`, not `web_crawls.id`**. That is the
load-bearing decision and it is not obvious: a foreground "continue crawling" calls
`create_web_crawl` and gets a **brand-new row**, so keying on `crawl_id` would have made every
foreground resume start from an empty frontier — reproducing the exact defect the table exists to
fix. `scope_prefix` is already this project's identity for "a crawl of this site"
(`enqueue_web_crawl` dedupes on it); `crawl_id` is kept as a nullable FK for provenance.

**A row leaves `pending` only after the page settles, never when it is claimed.** The in-memory
code added to `visited` *before* fetching; persisting that shape would have converted "interrupted
mid-fetch" into "permanently skipped". The cost of the safer choice — an interruption may repeat up
to one batch of fetches — is tested explicitly rather than assumed away.

Proven by defect re-injection against the final code (backup → inject → run → restore →
`diff -q` byte-identical): forcing the frontier back in-process fails 6 of 31 tests, including both
resume cases and the mid-fetch retry. The K12 tests stub `crawl._store` so **no page ever reaches
the store** — without that, the 24 h store-TTL shortcut would have made them pass against the
broken code.

### K8 — the premise in the brief was wrong, and checking changed the fix

The brief assumed `last_changed_at` was actively over-rating freshness in ranking. It is not:
**nothing reads it.** `web_memory._page_meta` selects it (`web_memory.py:1190`) and never copies it
onto `Evidence`, which has no field for it; the only other reference is `web_worker._mark_changed`,
which *writes* it. Verified independently by the manager. So the harm is latent, not active — and a
separate nullable column would have been the wrong fix: a fourth thing for four call sites to
maintain in service of no reader, leaving the original column permanently ambiguous.

Fixed in place instead (V26), clearing only what is **provably unobserved**:

```sql
fetch_count = 1 AND first_seen_at = fetched_at AND last_changed_at = fetched_at
```

A change is observed by comparing two fetches; a page fetched once has no second observation, so
whatever wrote its timestamp wrote a default. Both re-fetch writers bump `fetch_count` (the upsert's
ON CONFLICT and the 304 path) and `first_seen_at` is never updated, so this is decidable from the
row alone.

**Rows with `fetch_count > 1` are deliberately left alone**, even though many are V13 backfill
artifacts: the upsert writes `fetched_at` and `last_changed_at` from a single `now`, so a page that
genuinely changed on its most recent fetch has them equal too. The two are indistinguishable, and
"probably invented" is not a licence to destroy a value that may be real. They self-correct at the
next observed change.

### Regression check the integrity engineer could not run

R11's near-duplicate rule and K7's servable-id filter both sit in the retrieval path, and neither
had been measured against answer quality. Re-run of the scenario benchmark afterwards, 12
conversations per level: **cached 12/12 at c=1 and c=8; followup 24/24 at c=1 and c=8; zero errors
in 72 turns.** No quality regression from either change.

### Manager's follow-ups from this round

* `tests/conftest.py::_APP_TABLES` now names `web_crawl_frontier` explicitly. CASCADE already
  reached it, but a NULL-`crawl_id` row (which a foreground resume produces by design) would
  otherwise survive every truncation — the same reason `research_runs` is listed there.
* `db.py` gained the three accessors the corpus-integrity work needed and could not add itself:
  `servable_web_page_ids`, `set_web_page_quarantine`, `web_page_ids_for_urls` (the last resolves
  either spelling, because a redirected page is stored under both and an operator holds whichever
  the citation panel printed). Verified: quarantine withholds, restore returns, empty inputs are
  no-ops, an unknown URL is simply absent.
