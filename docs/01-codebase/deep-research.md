# Deep Research — the iterative mode

Three modes answer a question in this app, and they are deliberately different
shapes:

| mode | how it answers | typical cost | when |
|---|---|---|---|
| **Normal chat** | the local model answers directly, grounded on local web evidence when the question is time-sensitive or a stored page matches strongly | ~1–3 s to first token | most questions |
| **Web search** | one pass: search → read ~8 pages → cited answer | ~20 s | a current fact |
| **Deep Research** | plan → search → *open* → *extract dated claims* → *follow links to primary sources* → *audit the gaps* → search again → *verify* → cited report | ~3–5 min | a question with several parts, or one where "current" matters |

Deep Research lives in
[`orchestrator/app/engines/deep_research.py`](../../orchestrator/app/engines/deep_research.py).
It adds the **loop**, the **evidence registry**, the **temporal resolution**
and the **report** — not a second search stack. Everything expensive (parallel
SearXNG queries, the cross-encoder reranker, SSRF-guarded fetch, readable
extraction, the warm-page store) is called directly out of `engines/search.py`.

Rebuilt on **2026-09-03** after a live test on a time-sensitive topic exposed
four weaknesses — none specific to that topic:

| weakness | what happened | what changed |
|---|---|---|
| stopped too early | the auditor judged "sufficient" from 400-character snippets; a fixed cap of three rounds bounded the rest | the auditor reads 1,000-character excerpts **and** a per-subquestion evidence-status table; the loop stops on evidence (below) and logs why; `UNKNOWN` is treated as *not found yet* until the follow-ups are exhausted |
| no notion of time | no date in any prompt; pages carried no publication date; an old article and the current official page ranked on topicality alone | every prompt carries today's date and the freshness level; every page carries its published/updated date and read time; claims carry `as_of`; CURRENT / SUPERSEDED / CONFLICTING is decided in code |
| snippets only | only what the search engine returned was read | the links inside the pages read are scored and the best few opened each round — the citation an article gives, the official page, the PDF |
| copies counted as confirmations | ten syndicated copies of one report were ten sources | near-duplicate detection at registration; a copy keeps its citation number but corroborates nothing |

## The loop

```
question
   │
   ├─ TEMPORAL FRAME ... today's date + freshness level (offline regex; the
   │                    router model is never consulted mid-run)
   ├─ PLAN ........... guided JSON → subquestions + first-round queries
   │                    (direct / primary-source / most-recent angles) + entities;
   │                    a time-sensitive question with no year gets one query
   │                    with the current year
   │
   ├─ round 1 ........ search (parallel) → RANK (reranker topicality × domain
   │        │          authority × source class; stale-year snippets down)
   │        │          → fetch → REGISTER (provenance, fingerprint, duplicate?,
   │        │          primary?) → FOLLOW LINKS → EXTRACT CLAIMS (guided JSON,
   │        │          with as_of dates) → RESOLVE (code)
   │        │
   │        └─ AUDIT .... guided JSON over the evidence-status table + excerpts:
   │                      sufficient? missing? contradictions? follow-up queries?
   │                      primary-source queries? entities to expand?
   │                      + code adds `site:` queries on the authoritative
   │                        domains already found, for UNKNOWN subquestions
   │        ┌──────────────┘
   ├─ round 2 ........ search only the gaps → …
   ├─ round n ........ (stop on evidence — see below)
   │
   ├─ VERIFY ......... guided JSON per subquestion: enough evidence? primary
   │                    opened? newer source likely? disagreement? a change over
   │                    time mistaken for a contradiction? confidence.
   │                    Low confidence + budget → ONE more targeted round.
   │
   └─ REPORT ......... streamed; carries the date, the evidence-status table and
                       dated, labelled sources; every claim cited [n]; then
                       citation-validated. Claims → web_claims; top primary
                       domains → the background crawl queue.
```

### Time

* `core/provenance.page_dates` reads publication and modification dates out of
  the page's own metadata (OpenGraph, JSON-LD, `<time>`, visible dates via
  `htmldate`; the `Last-Modified` header as a fallback). It never invents a
  date: an undated page is labelled *undated* and ranks as such.
* Every source the model sees is labelled: `official · published 2026-03-12 ·
  read 2026-09-02 · primary source`, or `news · undated · read … · same text as [3]`.
* Claims are extracted **with** an `as_of` — the date the fact held according
  to the source (an effective date, an event date, or the article's date) —
  and a status hint (`current` / `historical` / `unclear`).
* `_resolve()` groups a subquestion's claims by value and picks the winner on
  recency × authority × independent corroboration. An earlier value with an
  earlier date is **SUPERSEDED** (a change over time); a different value of
  comparable date and authority is **CONFLICTING** (both cited in the report);
  no claims is **UNKNOWN**. A claim the source itself presents as history never
  competes for "current". Confidence comes from corroboration, authority,
  primary-source status, date freshness, and the absence of conflicts.
* The report prompt is explicit: web evidence outranks the model's memory;
  CURRENT facts are stated "as of"; SUPERSEDED values are history, not errors;
  CONFLICTING values are an open disagreement; UNKNOWN is "not found in the
  sources consulted" with what was searched.

### Sources

* **Ranking before fetching** combines the reranker's topical order with the
  domain's authority prior (`web_memory.authority_of`) and the structural
  source class (`core/provenance.source_type`: official, academic, docs,
  press, news, reference, code, community, social, blog, pdf). For a
  time-sensitive question a snippet whose only years are three or more years
  old is pushed down — never dropped, it may be the history the report needs.
* **Primary** = official / academic / first-party documentation / press
  announcements, or a high cached authority. Logged when found.
* **Duplicates**: word 6-gram fingerprints over the page's opening; Jaccard
  ≥ 0.6 (or containment ≥ 0.85 — the copy that trimmed the tail) marks the
  later page `same text as [n]`. Corroboration counts independent domains
  among non-duplicates.
* **Links followed**: from each page read, candidate links are scored by
  keyword overlap with the plan (URL path), the target's authority relative to
  the page linking to it (an article pointing at a more authoritative page is
  citing its source), the target's source class, and `.pdf`; session, share,
  tag and pagination links are excluded. At most two per page, six per round.
  They appear in the Research panel as their own group.

### When the loop stops

`meta.research_run.stop_reason`, also shown in the Activity panel and logged:

| reason | meaning |
|---|---|
| `sufficient` | the auditor is satisfied and no subquestion is UNKNOWN (or nothing is left to search) |
| `no_information_gain` | two consecutive rounds each added < `DEEP_RESEARCH_MIN_GAIN` new evidence per page attempted |
| `duplicate_rate` | ≥ 70 % of a round's pages were copies of pages already read and no new claims |
| `no_new_queries` | the auditor, the primary-source pass and the `site:` fallback produced nothing not yet run |
| `iteration_cap` / `source_cap` / `timeout` | `DEEP_RESEARCH_MAX_ITERATIONS=5` · `DEEP_RESEARCH_MAX_SOURCES=36` · `DEEP_RESEARCH_TIMEOUT_S=600` |

"Sufficient" with a subquestion still UNKNOWN and places left to look is **not**
a stop: that is the difference between *unknown* and *not found yet*.

## Zero paid API

Every model call is the local vLLM/Qwen deployment; every search is the local
SearXNG. No OpenAI, Anthropic, Tavily, Exa, Perplexity, Serper or paid Brave
key is required or contacted. The optional hosted providers remain in
`app/search/` for operators who want them, and are not on this path.

## Citation integrity

This is the part that justifies a separate engine rather than a longer search.

* Only pages that were **actually fetched** become sources, numbered
  contiguously; `meta.sources` is built from that registry (with each
  source's published date, class, primary and duplicate flags, and how it was
  found).
* The report prompt states the exact legal range and forbids inventing a
  citation, a URL or a source.
* After generation, `validate_citations()` **removes** any `[n]` outside the
  registry and counts it in `meta.research_run.invalid_citations_removed`.

The plain search engine has no such check: its only defence is a prompt
sentence, and the frontend strips `[n]` before rendering, so an invented `[99]`
there is *invisible* rather than caught. A report is a document people quote,
so here the marker is checked against the registry.

## Category routing

SearXNG's default general pool on this host reaches only google-cse, bing,
mwmbl and yahoo. Research-shaped subquestions are routed to
`categories=science` (arXiv, PubMed), which returns **full abstracts** —
1100–1900 character snippets against ~140 for bing — from a pool nothing else
here queries.

`categories=it` is deliberately **not** routed to despite being the healthiest
pool by count: its engines include Docker Hub and MDN, and a live run brought
back 9 Docker Hub image pages out of 23 sources ("vllm/vllm-openai — Docker
Image", "redislabs/memtier_benchmark"). A registry listing is not evidence.

## Budgets, and why they are what they are

Measured on this deployment:

* main model decodes **~70 tok/s**, so a ~1500-token report is ~22 s;
* a guided-JSON planning / audit / claims / verification call costs **2–8 s**
  (claims extraction reads up to ten 2,500-character excerpts);
* SearXNG answers several parallel queries in **~2 s**;
* reranking 40 documents costs **~50 ms**;
* page extraction is serialised on **one** worker shared with the crawler.

So the network and extraction side dominates, and a typical run — three to
four rounds, a verification pass and a report — lands at **3–5 minutes**.

Every budget is an env var — see `.env.example` (`DEEP_RESEARCH_*`) and
`docs/CONFIG.md`.

## Concurrency, and not starving chat

Two guards, both in the engine:

* `_LLM_SEM = asyncio.Semaphore(2)` — research reasoning never runs more than
  two generations at once. The engine is memory-bandwidth bound, so a third
  concurrent generation costs every other stream latency rather than adding
  throughput.
* `_RUN_LOCK` — **one research run at a time per orchestrator process.** A
  second request is refused with an honest sentence rather than silently
  halving both runs' budgets against the same SearXNG and the same GPU.

Search pacing matters too: Google's free CSE endpoint IP-blocked this host
during testing at roughly 15–20 queries in 3 minutes, and the block **outlived**
SearXNG's own 180 s bench — every retry re-armed it. `DEEP_RESEARCH_MAX_QUERIES_PER_ITERATION`
exists for that reason.

## What the user sees

No new SSE event types were invented — the vocabulary in `app/sse.py` is closed
and an unknown name raises *inside* the response generator, killing the stream
with no error frame. Deep Research reuses:

| event | carries |
|---|---|
| `step` | the pipeline stages (Planning → Searching → Analyzing → Verifying → Writing), each `running` then `done`, with per-round detail (new sources, links followed, duplicates, claims, primaries) and the stop reason on the last |
| `status` | the live line, e.g. "Following up on gaps (round 2) — 5 queries…", "Extracting claims from 10 source(s)…", "Cross-checking the important claims…" |
| `research` | each query and the links it returned (the Research panel); links followed appear as their own group |
| `token` | the report, **streamed** |
| `meta` | `route: "deep_research"`, `sources[]` (dated, typed, flagged), and a `research_run` summary |

`meta.research_run` records `iterations`, `queries`, `subquestions`,
`sources_found`, `sources_cited`, `missing`, `contradictions`, `elapsed_s`,
`invalid_citations_removed`, and since 2026-09-03 `stop_reason`, `today`,
`temporal`, `rounds[]` (per-round counts and information gain),
`links_followed`, `primary_sources`, `duplicates_dropped`, `stale_downranked`,
`claims`, `resolutions[]` (status, value, as-of, support, superseded,
conflicts, confidence per subquestion), `confidence` and `verification_rounds`.
The Activity panel renders it as *Research summary*.

## What the log shows

One `INFO` line per decision, prefixed `research[<id>]`: the temporal frame,
the plan, each round's queries, every page opened (with its label), the links
followed, primary sources found, claims extracted, each resolution, the
auditor's verdict with the stop decision, each verification verdict, and the
final summary. `docker logs sf-local-ai-orchestrator-1 | grep 'research\['`
reconstructs a run.

## Persistence

* Migration **V11** — `research_runs`: one row per run: the question, status
  (`running`/`done`/`cancelled`/`failed`), counts, the final report, and the
  citation registry as `jsonb`.
* Migration **V14** — `web_claims`: the resolved claims, dated, with their
  status and confidence. They are derived from public pages and shared the
  same way `web_pages` is: the Fast-mode knowledge layer
  (`living_knowledge.prepare`) reads matching CURRENT claims for any user's
  time-sensitive question. No user or conversation is recorded on a claim.
* The pages themselves live once in the global `web_pages` store, now with
  `published_at`, `modified_at`, `source_type`, `authority`; a changed page's
  previous text is kept in `web_page_versions`.
* After the report the top primary domains are queued for a bounded background
  crawl (`web_crawls`, kind `research`).

`research_runs` is in `_SIDE_TABLES`, so deleting a conversation deletes its
runs — including the question text.

## Turning it on

The composer's **+** menu has a "Deep research" row, and since 2026-09-03 the
composer accepts `/deep-research <question>` (also `/research`, `/deep`) —
type `/` to see the commands. Turning it on turns Salesforce off (research is
web work). The pref is deliberately **not sticky** across reloads: a
multi-minute report the user forgot they armed is a worse surprise than an
extra click.

Server-side it needs `DEEP_RESEARCH_ENABLED=true` (default) **and**
`SEARCH_ENABLED=true`. Without a search provider the request degrades to the
ordinary engines and says so, rather than pretending to research.

## Tests

* [`orchestrator/tests/test_deep_research.py`](../../orchestrator/tests/test_deep_research.py)
  — the original 29 offline tests: citation integrity (including a fabricated
  `[99]`), every termination path, degradation (dead provider, malformed plan,
  failed fetch), category routing, the single-run lock, the SSE contract, persistence,
  and the dispatch order that keeps the agent planner from swallowing the request.
* [`orchestrator/tests/test_research_resolution.py`](../../orchestrator/tests/test_research_resolution.py)
  — the 2026-09-03 rebuild: supersession vs conflict vs unknown, history hints,
  duplicate registration and independent corroboration, link scoring, the stop
  reasons, `site:` follow-ups on authoritative domains, the year augmentation,
  and the full offline loop with a verification round, persisted claims, meta
  and log lines.
* [`orchestrator/tests/test_provenance.py`](../../orchestrator/tests/test_provenance.py)
  — dates, source classes, primaries, near-duplicates.
