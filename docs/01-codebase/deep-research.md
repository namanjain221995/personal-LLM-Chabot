# Deep Research — the iterative mode

Three modes answer a question in this app, and they are deliberately different
shapes:

| mode | how it answers | typical cost | when |
|---|---|---|---|
| **Normal chat** | the local model answers directly | ~8 s | no web needed |
| **Web search** | one pass: search → read ~8 pages → cited answer | ~20 s | a current fact |
| **Deep Research** | plan → search → read → *audit the gaps* → search again → cited report | ~2–3 min | a question with several parts |

Deep Research lives in
[`orchestrator/app/engines/deep_research.py`](../../orchestrator/app/engines/deep_research.py).
It adds the **loop**, the **evidence registry** and the **report** — not a
second search stack. Everything expensive (parallel SearXNG queries, the
cross-encoder reranker, SSRF-guarded fetch, readable extraction, the warm-page
store) is called directly out of `engines/search.py`.

## The loop

```
question
   │
   ├─ PLAN ......... guided JSON → subquestions + first-round queries
   │
   ├─ round 1 ...... search (parallel) → rerank → fetch → register sources
   │        │
   │        └─ AUDIT .... guided JSON → sufficient? what is missing?
   │                      contradictions? follow-up queries?
   │        ┌──────────────┘
   ├─ round 2 ...... search only the gaps → …
   ├─ round 3 ...... (cap)
   │
   └─ REPORT ....... streamed, every claim cited [n], then citation-validated
```

It stops on the **first** of: the auditor says the evidence is sufficient, the
iteration cap, the source cap, the wall-clock timeout, or the auditor asking
only for queries that were already run.

## Zero paid API

Every model call is the local vLLM/Qwen deployment; every search is the local
SearXNG. No OpenAI, Anthropic, Tavily, Exa, Perplexity, Serper or paid Brave
key is required or contacted. The optional hosted providers remain in
`app/search/` for operators who want them, and are not on this path.

## Citation integrity

This is the part that justifies a separate engine rather than a longer search.

* Only pages that were **actually fetched** become sources, numbered
  contiguously; `meta.sources` is built from that registry.
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

Measured on this deployment (2026-08-30):

* main model decodes **~70 tok/s** (1136 tokens in 16.1 s), so a ~1500-token
  report is ~22 s;
* a guided-JSON planning call costs **~2 s**;
* SearXNG answers several parallel queries in **~2 s**;
* reranking 40 documents costs **~50 ms**;
* page extraction is serialised on **one** worker shared with the crawler.

So the network and extraction side dominates, and three rounds plus a report
lands at **~2–3 minutes**. A live run of the mission's own test question:
3 iterations, 24 sources, 15 cited, 0 fabricated, **170 s**.

Every budget is an env var — see `.env.example` (`DEEP_RESEARCH_*`).

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
| `step` | the pipeline stages (Planning → Searching → Analyzing → Writing), each `running` then `done` |
| `status` | the live line, e.g. "Following up on gaps (round 2) — 5 queries…" |
| `research` | each query and the links it returned (the Research panel) |
| `token` | the report, **streamed** |
| `meta` | `route: "deep_research"`, `sources[]`, and a `research_run` summary |

`meta.research_run` records `iterations`, `queries`, `subquestions`,
`sources_found`, `sources_cited`, `missing`, `contradictions`, `elapsed_s` and
`invalid_citations_removed`.

## Persistence

Migration **V11** adds `research_runs` — one row per run: the question, status
(`running`/`done`/`cancelled`/`failed`), counts, the final report, and the
citation registry as `jsonb` so a stored report's `[n]` markers still resolve
months later. The pages themselves are **not** copied: they live once in the
global V8 `web_pages` store.

`research_runs` is in `_SIDE_TABLES`, so deleting a conversation deletes its
runs — including the question text.

## Turning it on

The composer's **+** menu has a "Deep research" row; turning it on turns
Salesforce off (research is web work). The pref is deliberately **not sticky**
across reloads: a multi-minute report the user forgot they armed is a worse
surprise than an extra click.

Server-side it needs `DEEP_RESEARCH_ENABLED=true` (default) **and**
`SEARCH_ENABLED=true`. Without a search provider the request degrades to the
ordinary engines and says so, rather than pretending to research.

## Tests

[`orchestrator/tests/test_deep_research.py`](../../orchestrator/tests/test_deep_research.py)
— 29 offline tests covering citation integrity (including a fabricated `[99]`),
every termination path, degradation (dead provider, malformed plan, failed
fetch), category routing, the single-run lock, the SSE contract (only legal
event names, exactly one `meta`), persistence, and the dispatch order that
keeps the agent planner from swallowing the request.
