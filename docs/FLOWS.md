# TechSara — Flow Diagrams

Every path a request can take through this system, as a picture. If you read
one file to understand how the project works, read this one.

Diagrams are Mermaid, so they render on GitHub, in most editors, **and in this
chatbot's own UI** — it turns fenced `mermaid` blocks into real, zoomable
diagrams.

> Companion docs: [`README.md`](../README.md) for prose and configuration,
> [`docs/01-codebase/deep-research.md`](01-codebase/deep-research.md) for the
> research engine in depth, [`docs/02-diagrams/`](02-diagrams/) for the
> PlantUML C4/sequence suite.

---

## Contents

1. [The whole system at a glance](#1-the-whole-system-at-a-glance)
2. [The models, and what each one is for](#2-the-models-and-what-each-one-is-for)
3. [How a request picks its engine](#3-how-a-request-picks-its-engine)
4. [Mode 1 — Normal chat](#4-mode-1--normal-chat)
5. [Mode 2 — Web search](#5-mode-2--web-search)
6. [Mode 3 — Deep Research](#6-mode-3--deep-research)
7. [Web search vs Deep Research, side by side](#7-web-search-vs-deep-research-side-by-side)
8. [The site crawler](#8-the-site-crawler)
9. [Web memory: what gets stored and reused](#9-web-memory-what-gets-stored-and-reused)
10. [Citations: how a `[n]` is kept honest](#10-citations-how-a-n-is-kept-honest)
11. [Salesforce](#11-salesforce)
12. [Streaming: what the browser receives](#12-streaming-what-the-browser-receives)
13. [Data stores](#13-data-stores)
14. [Where everything runs](#14-where-everything-runs)

---

## 1. The whole system at a glance

```mermaid
flowchart TB
    U([User]) --> FE["Next.js frontend<br/>port 3000"]
    FE -->|"POST /api/chat"| PROXY["Next API route<br/>app/api/chat"]
    PROXY -->|"POST /chat, SSE"| ORCH["FastAPI orchestrator<br/>port 8080"]

    ORCH --> PRE["Pre-passes<br/>auto-plan · memory · repo · crawl · URL · datasets"]
    PRE --> DISPATCH{"Engine dispatch<br/>13 branches"}

    DISPATCH --> CHAT["chat"]
    DISPATCH --> SEARCH["search"]
    DISPATCH --> DR["deep_research"]
    DISPATCH --> SF["Salesforce engines<br/>sql · rag · sf_intel · live_sf"]
    DISPATCH --> OTHER["vision · document · report<br/>url · repo · crawl · dataset · agent"]

    CHAT & SEARCH & DR & SF & OTHER --> LLM["vLLM · Qwen<br/>local GPU"]
    SEARCH & DR --> SX["SearXNG<br/>internal only"]
    SX --> WEB([Public web])
    SEARCH & DR --> FETCH["SSRF-guarded fetch<br/>+ readable extraction"]
    FETCH --> WEB

    SF --> DUCK[("DuckDB warehouse<br/>read-only snapshot")]
    ORCH --> PG[("PostgreSQL<br/>app state")]
    SEARCH & DR --> LANCE[("LanceDB<br/>vector indexes")]

    LLM --> ORCH
    ORCH -->|"SSE events"| FE
    FE --> U

    classDef edge fill:#2563EB,color:#fff,stroke:none
    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef gpu fill:#16A34A,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    classDef ext fill:#64748B,color:#fff,stroke:none
    class FE,PROXY edge
    class ORCH,PRE,CHAT,SEARCH,DR,SF,OTHER,FETCH app
    class LLM gpu
    class PG,DUCK,LANCE data
    class SX,WEB ext
```

**Nothing leaves the machine except web searches and page fetches**, and both
are off unless enabled. All inference is local.

---

## 2. The models, and what each one is for

Five models run as separate vLLM services on the DGX Spark profile. They are
**not** interchangeable — each is sized for its job.

```mermaid
flowchart LR
    subgraph GPU["Local GPU — no external API"]
        MAIN["<b>MAIN</b><br/>Qwen3.6-35B-A3B-NVFP4<br/>256-expert MoE, 3B active<br/>1M context · ~70 tok/s"]
        ROUTER["<b>ROUTER</b><br/>Qwen3-VL-8B-Instruct-FP8<br/>small + fast"]
        EMBED["<b>EMBED</b><br/>Qwen3-Embedding-0.6B<br/>1024-dim vectors"]
        RERANK["<b>RERANK</b><br/>Qwen3-Reranker-0.6B<br/>cross-encoder"]
        OCR["<b>OCR</b><br/>baidu/Unlimited-OCR"]
    end

    MAIN --> M1["Every user-facing answer"]
    MAIN --> M2["Research planning + gap analysis<br/>guided JSON"]
    MAIN --> M3["Vision — images and PDF pages"]
    ROUTER --> R1["Route classification"]
    ROUTER --> R2["Search query rewriting"]
    EMBED --> E1["Salesforce RAG chunks"]
    EMBED --> E2["Web page chunks"]
    RERANK --> K1["Ordering search results<br/>before spending a fetch"]
    RERANK --> K2["Ordering RAG hits"]
    OCR --> O1["Pages with a thin text layer"]

    classDef gpu fill:#16A34A,color:#fff,stroke:none
    class MAIN,ROUTER,EMBED,RERANK,OCR gpu
```

| Role | Model | Port | Why this one |
|---|---|---|---|
| **Main** | `Qwen3.6-35B-A3B-NVFP4` | 8000 | MoE: 35B of quality at 3B-active speed. Measured **70 tok/s** decode, 1M-token window. Answers, plans, and sees images. |
| **Router** | `Qwen3-VL-8B-Instruct-FP8` | 8002 | Classifying a route or rewriting a query is mechanical. Doing it on the main model made every search wait seconds before the first fetch. |
| **Embed** | `Qwen3-Embedding-0.6B` | 8003 | 1024-dim vectors for both the Salesforce corpus and web memory. |
| **Rerank** | `Qwen3-Reranker-0.6B` | 8005 | A **cross-encoder**: scores (query, document) jointly. 40 documents in **52 ms**. |
| **OCR** | `baidu/Unlimited-OCR` | 8004 | Only for PDF pages whose text layer is too thin to read. |

> **The reranker had to be served correctly to work at all.** It was originally
> launched with `--runner pooling` and no conversion, so vLLM loaded it as a
> plain *embedding* model — `Supported tasks: ['embed', 'token_embed']`, no
> `score` task — and `/score` returned cosine similarity instead of relevance.
> Ranking "DGX Spark memory bandwidth" put *Download - NVIDIA* first and the
> real hardware guide 11th of 14. It now runs with `--convert classify` and a
> `Qwen3ForSequenceClassification` override; score spread doubled and the junk
> collapsed. See [CHANGELOG](../CHANGELOG.md) 2026-08-30.

---

## 3. How a request picks its engine

Order matters enormously here — each branch must outrank the ones that would
otherwise swallow it.

```mermaid
flowchart TD
    START([POST /chat]) --> CLAR{"Pending<br/>clarification?"}
    CLAR -->|yes| SFI["sf_intel — resume"]
    CLAR -->|no| PDF{"PDF attached?"}
    PDF -->|yes| DOC["document + ocr"]
    PDF -->|no| IMG{"Image attached?"}
    IMG -->|yes| VIS["vision"]
    IMG -->|no| REPO{"GitHub URL, or<br/>repo in this chat?"}
    REPO -->|yes| RP["repo"]
    REPO -->|no| CRAWL{"'index this site' + URL?"}
    CRAWL -->|yes| CR["crawl"]
    CRAWL -->|no| SITEQA{"Crawled site has<br/>relevant chunks?"}
    SITEQA -->|yes| SQA["site Q&A"]
    SITEQA -->|no| URLQ{"Pasted links<br/>are the request?"}
    URLQ -->|yes| URLE["url"]
    URLQ -->|no| DRQ{"<b>Deep Research pill on?</b>"}
    DRQ -->|yes| DRE["<b>deep_research</b>"]
    DRQ -->|no| AGQ{"Agent wanted?"}
    AGQ -->|yes| AG["agent"]
    AGQ -->|no| SQ{"Web search wanted?"}
    SQ -->|yes| SE["search"]
    SQ -->|no| DSQ{"Datasets uploaded?"}
    DSQ -->|yes| DS["dataset"]
    DSQ -->|no| MODE{"mode?"}
    MODE -->|assistant| CH["chat"]
    MODE -->|salesforce| ROUTE["router → sql / rag / report / chat"]

    classDef hot fill:#7C3AED,color:#fff,stroke:none
    class DRE,SE hot
```

**Why Deep Research sits above the agent.** The auto-planner classifies exactly
the multi-part phrasing Deep Research targets as `agent=true` — and at effort
*Max* with search on, it **forces** it. One branch lower and the pill would be
silently eaten by the planner.

**Why it sits below pasted URLs and the crawler.** A research question that
happens to quote a link should still research; those pre-passes are explicitly
skipped when the pill is on, so the link cannot divert it into the single-page
reader.

---

## 4. Mode 1 — Normal chat

No web, no database. Roughly **9 seconds**.

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant O as Orchestrator
    participant M as MAIN model

    U->>O: "Explain Python dictionaries"
    O->>O: build context — history + memory + system prompt
    O->>M: stream_chat_events(effort)
    loop tokens
        M-->>O: token delta
        O-->>U: event: token
    end
    O-->>U: event: meta {route:"chat"}
    O-->>U: event: done
```

---

## 5. Mode 2 — Web search

One pass: search, read a handful of pages, answer with citations. Roughly
**20 seconds**.

```mermaid
flowchart TD
    Q([User question]) --> RW["<b>Rewrite queries</b><br/>ROUTER model<br/>1 / 3 / 6 by effort"]
    RW --> SX["<b>Search in parallel</b><br/>SearXNG, all queries at once"]
    SX --> MERGE["<b>Round-robin merge</b><br/>rank 1 of every query, then rank 2…<br/>dedupe by normalised URL<br/>cap per domain"]
    MERGE --> RR["<b>Rerank</b><br/>cross-encoder scores title+snippet<br/>~50 ms"]
    RR --> WARM{"Stored copy<br/>fresh enough?"}
    WARM -->|yes| STORE[("web_pages<br/>PostgreSQL")]
    WARM -->|no| FETCH["<b>Fetch</b> — SSRF-guarded<br/>16 concurrent · 5 MB · 8 s"]
    FETCH --> EXTRACT["<b>Extract readable text</b><br/>trafilatura, single worker"]
    EXTRACT -.->|write-behind| STORE
    STORE --> TIER
    EXTRACT --> TIER["<b>Budget the prompt</b><br/>top 10 keep 8000 chars<br/>the tail is trimmed"]
    TIER --> MEM["<b>Add memory</b><br/>dated chunks from pages<br/>read in earlier chats"]
    MEM --> ANS["<b>MAIN model</b><br/>answer, citing [n]"]
    ANS --> OUT([Streamed answer + sources])

    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    class RW,SX,MERGE,RR,FETCH,EXTRACT,TIER,MEM,ANS app
    class STORE data
```

**Graceful degradation is the rule at every step.** One engine 403s → the
others still answer. One URL times out → its snippet is used instead. Search
is entirely unavailable → the model answers from its own knowledge *and says
so*. A dead reranker → engine order is kept. Nothing here can fail the turn.

---

## 6. Mode 3 — Deep Research

The iterative one, rebuilt on 2026-09-03. Plan → search → **open** the
pages → **extract dated claims** → **follow the links** those pages give →
resolve what each subquestion currently has → audit what is missing → search
again → **verify** the important claims → report. Roughly **3–5 minutes**.

```mermaid
flowchart TD
    Q([Research question]) --> T["<b>0 · TEMPORAL FRAME</b><br/>today's date · freshness level<br/>(offline regex, never the router)"]
    T --> PLAN["<b>1 · PLAN</b><br/>MAIN model, guided JSON<br/>→ subquestions · queries (direct,<br/>primary-source, most-recent) · entities"]
    PLAN --> ROUND["<b>2 · ROUND</b>"]

    subgraph ROUNDBOX ["one round"]
        ROUND --> ROUTE["route each query<br/>science pool ↔ general pool"]
        ROUTE --> SEARCH["search in parallel<br/>SearXNG"]
        SEARCH --> SKIP["drop URLs already read<br/>this run"]
        SKIP --> RANK["<b>rank candidates</b><br/>reranker topicality × domain authority<br/>× source class · stale-year snippets down"]
        RANK --> FETCH["fetch + extract<br/>reuses the search pipeline<br/>+ published/updated dates"]
        FETCH --> REG["<b>register sources</b><br/>provenance · fingerprint ·<br/>DUPLICATE? · PRIMARY?"]
        REG --> LINKS["<b>follow links</b><br/>the citation an article gives,<br/>the official page, the PDF"]
        LINKS --> CLAIMS["<b>extract claims</b><br/>MAIN model, guided JSON<br/>claim · value · as_of · current/historical"]
        CLAIMS --> RESOLVE["<b>resolve (code)</b><br/>CURRENT · SUPERSEDED ·<br/>CONFLICTING · UNKNOWN + confidence"]
    end

    RESOLVE --> THIN{"Fewer than<br/>min_sources?"}
    THIN -->|yes| MORE["search the plan's<br/>remaining angles"]
    MORE --> ROUND
    THIN -->|no| AUDIT["<b>3 · AUDIT</b><br/>MAIN model, guided JSON, sees the<br/>evidence-status table + real excerpts<br/>sufficient? · missing? · contradictions? ·<br/>follow-ups · primary-source queries"]

    AUDIT --> STOP{"<b>stop on evidence?</b><br/>sufficient & nothing UNKNOWN ·<br/>no information gain (2 rounds) ·<br/>duplicate rate · budget ·<br/>nowhere left to look"}
    STOP -->|no| FOLLOW["follow-ups + primary-source queries<br/>+ site: queries on authoritative<br/>domains already found"]
    FOLLOW --> ROUND
    STOP -->|yes| VERIFY["<b>4 · VERIFY</b><br/>MAIN model, guided JSON per subquestion<br/>enough? primary opened? newer likely?<br/>disagree? changed over time? confidence"]
    VERIFY -->|low confidence & budget| VROUND["one more targeted round"]
    VROUND --> REPORT
    VERIFY -->|confident| REPORT

    REPORT["<b>5 · REPORT</b><br/>MAIN model, streamed<br/>current date · evidence-status table ·<br/>dated, labelled sources · every claim [n]"]
    REPORT --> VALIDATE["<b>6 · VALIDATE CITATIONS</b><br/>remove any [n] with no source<br/>code blocks left untouched"]
    VALIDATE --> SAVE[("research_runs · <b>web_claims</b><br/>report · registry · dated claims")]
    VALIDATE --> CRAWLQ[("crawl queue<br/>top primary domains")]
    VALIDATE --> OUT([Report + real sources + research summary])

    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    class T,PLAN,ROUTE,SEARCH,SKIP,RANK,FETCH,REG,LINKS,CLAIMS,RESOLVE,AUDIT,MORE,FOLLOW,VERIFY,VROUND,REPORT,VALIDATE app
    class SAVE,CRAWLQ data
```

### Time, in one table

| Signal | Where it comes from | What it changes |
|---|---|---|
| today's date + freshness level | the clock; `freshness.classify_offline` | every prompt is dated; a time-sensitive question gets a query with the current year and a recency-weighted resolution |
| a page's published / updated date | `core/provenance.page_dates` (page metadata, JSON-LD, `<time>`, `Last-Modified`) — never invented | the source label the model reads; the ranking; supersession |
| a claim's `as_of` | extracted with the claim (an effective date, event date, or the article's own date) | which value is CURRENT and which is history |

**CURRENT / SUPERSEDED / CONFLICTING / UNKNOWN** are decided *in code*
(`deep_research._resolve`): claims are grouped by value; the best-supported
group wins on recency × authority × independent corroboration; an earlier value
with an earlier date is **superseded** (a change over time), a different value
of comparable date and authority is a **conflict** (surfaced, both cited), and
a subquestion with no claims is **unknown** — which the auditor is told is
*not found yet*, not *does not exist*, until the follow-ups are exhausted.

### When the loop stops

It stops on the **first** of these, and says which (`meta.research_run.stop_reason`,
the Activity panel's *Research summary*, and the `research[…] assess:` log line):

| Stop reason | Meaning | Default |
|---|---|---|
| `sufficient` | the auditor is satisfied and no subquestion is UNKNOWN | — |
| `no_information_gain` | two consecutive rounds added < `DEEP_RESEARCH_MIN_GAIN` of new evidence | `0.15` |
| `duplicate_rate` | a round's pages were mostly copies of pages already read | 70 % |
| `no_new_queries` | the auditor, the primary-source pass and the `site:` fallback produced nothing new | — |
| `iteration_cap` | rounds (verification included) | `DEEP_RESEARCH_MAX_ITERATIONS=5` |
| `source_cap` | pages registered | `DEEP_RESEARCH_MAX_SOURCES=36` |
| `timeout` | wall clock | `DEEP_RESEARCH_TIMEOUT_S=600` |

### What the user watches while it runs

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser
    participant O as Orchestrator

    O-->>U: step "Planning the research" · running
    O-->>U: step "Planned the research" · done + subquestions
    O-->>U: status "Searching the web — 5 queries…"
    O-->>U: research {phase:"query", query, results[]}
    O-->>U: research {phase:"query", query:"↳ links followed from …", results[]}
    O-->>U: status "Extracting claims from 10 source(s)…"
    O-->>U: step "Searching the web" · done — 10 new; 3 links followed; 2 duplicates; 14 claims; 3 primary
    O-->>U: status "Checking what is still missing…"
    O-->>U: step "Analyzed evidence" · done — gaps · not found yet: …
    O-->>U: status "Following up on gaps (round 2)…"
    Note over O,U: …rounds repeat until a stop reason…
    O-->>U: step "Verifying claims" · done — confidence 0.82 (or: one more targeted round)
    O-->>U: status "Writing the report from 28 sources…"
    loop streamed
        O-->>U: token
    end
    O-->>U: step "Wrote the report" · done — 19 of 28 cited · stopped: sufficient · confidence 0.82
    O-->>U: meta {route:"deep_research", sources[] (dated, typed, primary/duplicate flags), research_run{stop_reason, rounds[], resolutions[], …}}
    O-->>U: done
```

No new event types were invented. The SSE vocabulary is **closed** — an unknown
name raises inside the response generator and would kill the stream with no
error frame — so Deep Research reuses `step`, `status`, `research`, `token` and
`meta`, all of which the frontend already renders; the links it follows appear
in the Research panel as their own query group.

### What the log shows

Every decision is one `INFO` line prefixed `research[<id>]`: the plan, each
round's queries, every page **opened** (with its label: class, published,
read, primary, duplicate-of), the links **followed**, the claims extracted,
each subquestion's **resolution**, the auditor's verdict and the **stop
reason**, each verification verdict, and the final summary. `grep research\[`
on the orchestrator log reconstructs a run.

### Concurrency guards

```mermaid
flowchart LR
    R1["Run A — user 1"] --> ADM{"_Admission"}
    R2["Run B — user 2"] --> ADM
    R3["Run C — user 1 again"] --> ADM
    ADM -->|"slot free"| GO["runs"]
    ADM -->|"per-user limit"| OWN["refused: your own run<br/>is still going"]
    ADM -->|"ceiling busy"| WAIT["queues up to 45s"]
    WAIT -->|"slot frees"| GO
    WAIT -->|"still full"| NO["refused, quoting when<br/>the earliest finishes"]
    GO --> SEM{"_LLM_SEM (2)<br/>shared by ALL runs"}
    SEM --> GPU["MAIN model<br/>shared with interactive chat"]

    classDef app fill:#7C3AED,color:#fff,stroke:none
    class R1,R2,R3,GO,OWN,WAIT,NO,GPU app
```

One run at a time per orchestrator process. A second would double every budget
against the same SearXNG and the same GPU, so it is refused rather than both
being starved.

---

## 7. Web search vs Deep Research, side by side

```mermaid
flowchart LR
    subgraph WS ["Web search — ~20 s"]
        direction TB
        A1["rewrite → 1-6 queries"] --> A2["search once"]
        A2 --> A3["read ~8 pages"]
        A3 --> A4["answer with citations"]
    end

    subgraph DR ["Deep Research — ~2-3 min"]
        direction TB
        B1["plan → subquestions"] --> B2["search"]
        B2 --> B3["read"]
        B3 --> B4["audit the gaps"]
        B4 -->|"missing something"| B2
        B4 -->|"enough"| B5["write a cited report"]
    end

    classDef app fill:#7C3AED,color:#fff,stroke:none
    class A1,A2,A3,A4,B1,B2,B3,B4,B5 app
```

| | Web search | Deep Research |
|---|---|---|
| Searches | 1 round, 1–6 queries | up to 3 rounds, ~5 queries each |
| Pages read | ~8 | up to 24 |
| Model calls | 2 (rewrite + answer) | 5+ (plan, audit per round, report) |
| Knows what it is missing | no | **yes — that is the loop** |
| Output | a paragraph or two | a structured report with sections |
| Fabricated citations | prompt-discouraged | **removed and counted** |
| Turn it on | "Web search" in the **+** menu | "Deep research" in the **+** menu |

Use web search for *a fact*. Use Deep Research for *a decision*.

---

## 8. The site crawler

`index this site <url>` walks one site into the same web store.

```mermaid
flowchart TD
    IN(["'index this site https://docs.example.com'"]) --> INTENT{"intent + URL?<br/>words inside a URL don't count"}
    INTENT -->|no| ELSE["…other engines"]
    INTENT -->|yes| ROBOTS["<b>robots.txt</b><br/>via the SSRF-guarded fetcher<br/>4xx = allowed · 5xx = decline"]
    ROBOTS -->|declined| STOP(["politely refuses"])
    ROBOTS -->|allowed| SITEMAP{"sitemap?"}
    SITEMAP -->|yes| FRONTIER["frontier = sitemap URLs"]
    SITEMAP -->|no| WALK["frontier = root, walk links"]
    FRONTIER --> LOOP
    WALK --> LOOP

    LOOP["<b>crawl loop</b><br/>3 concurrent · politeness delay<br/>scope checked at enqueue AND after redirect"] --> FRESH{"already stored<br/>and fresh?"}
    FRESH -->|yes| FREE["free — reuse it,<br/>and follow its stored links"]
    FRESH -->|no| GET["fetch + extract + store"]
    FREE --> CAP
    GET --> CAP{"caps hit?<br/>1000 pages / 15 min"}
    CAP -->|no| LOOP
    CAP -->|yes| INDEX["embed new pages<br/>into the web vector index"]
    INDEX --> DONE(["'55 pages stored and searchable'<br/>ask anything about the site"])

    classDef app fill:#7C3AED,color:#fff,stroke:none
    class ROBOTS,FRONTIER,WALK,LOOP,GET,FREE,INDEX app
```

### The background queue (2026-09-03)

The same crawler now also runs **behind** the chat. Two things enqueue a
bounded job (`web_crawls` row, status `queued`, with its own page/minute caps):

* **sharing a URL** — the pasted page is answered from immediately *and* stored
  in the global corpus, and its site is queued (`WEB_SHARE_CRAWL_MAX_PAGES=150`,
  8 min). The status line says *"Indexing docs.example.com in the background —
  later questions can draw on the whole site."* and `meta.site_crawl` records it;
* **a Deep Research run** — its top primary domains (up to 3, 40 pages each).

The knowledge worker drains the queue one job at a time, after its index and
refresh passes, and is **woken** the moment a job is queued (`web_worker.kick()`)
rather than waiting for its five-minute cycle. A restart requeues a background
job that was running; a foreground crawl cut off the same way is closed as
`capped` so its pages stay usable. Same scope, already queued or crawled within
24 h → not queued again; stored pages cost nothing, so a large site finishes over
several shares. Type `/crawl <url>` in the composer for the foreground version
with live progress.

---

## 9. Web memory: what gets stored and reused

Every page the system reads is kept, so the next question is cheaper.

```mermaid
flowchart LR
    S["search / research / crawl"] --> P["page fetched"]
    P --> HASH{"content hash<br/>changed?"}
    HASH -->|new or changed| STORE[("web_pages<br/>full text · links · hash")]
    HASH -->|unchanged| BUMP["just bump<br/>the freshness clock"]
    STORE --> MARK["indexed_at = NULL<br/>→ needs embedding"]
    MARK --> CHUNK["chunk 3200 chars / 400 overlap"]
    CHUNK --> EMB["EMBED model"]
    EMB --> LANCE[("LanceDB<br/>web_chunks")]

    Q(["a later question,<br/>any conversation"]) --> LANCE
    LANCE --> HITS["relevant paragraphs<br/>from pages read days ago"]
    HITS --> ANS["added to the answer<br/>as dated sources"]

    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    class S,P,CHUNK,EMB,HITS,ANS,MARK,BUMP app
    class STORE,LANCE data
```

The vector index is **derived state** — deleting it is always safe, because
PostgreSQL rebuilds it from `indexed_at IS NULL`.

---

## 9a. The living knowledge layer (freshness)

**The problem it solves.** The model's weights are frozen at its training
cut-off. On 2026-08-31 it answered *"who's vice president of india"* with the
previous holder — confidently — while 19 pages already stored on this machine
named the current one. The evidence was stored, indexed and retrievable; it was
simply never consulted, because `web_index.retrieve` was only ever called from
inside the search engine. With Web Search off, no code path reached it.

Now every assistant turn runs a cheap pre-answer stage first.

```mermaid
flowchart TD
    Q["User question"] --> C{"Freshness classifier<br/>regex, ~6 microseconds"}
    C -->|STATIC<br/>'what is photosynthesis'| W["Answer from the model.<br/>No retrieval, no cost."]
    C -->|RECENT / REALTIME| R["Hybrid retrieval over the local corpus"]

    R --> R1["dense vectors (LanceDB)"]
    R --> R2["lexical full-text (PostgreSQL)"]
    R1 --> RANK["Rank: similarity + surface match<br/>+ source authority + age"]
    R2 --> RANK
    RANK --> SUP["Drop clearly superseded pages<br/>flag genuine conflicts"]
    SUP --> ENOUGH{"Fresh enough<br/>to answer?"}

    ENOUGH -->|yes| G["Ground the prompt with dated,<br/>cited passages -> answer"]
    ENOUGH -->|"no, Fast mode"| S["ONE query, 2 sources,<br/>8s deadline"]
    ENOUGH -->|"no, think/max"| F["Hand to the full search engine"]
    C -->|STATIC, strong local match| TOP["Topical grounding (2026-09-03):<br/>a site indexed here or a page<br/>research read answers it, cited"]
    R --> CL["Resolved research claims<br/>(web_claims, dated) join the evidence"]
    ENOUGH -->|"no, offline"| STALE["Answer from cache AND say<br/>how old it is"]

    S --> STORE["Store + index"]
    STORE --> G
```

**Why the answer improves for everyone.** The page store is global and public,
so one person's search warms the corpus for the next person's question — which
is what turns a one-off lookup into durable local knowledge. Private material
(who asked, from which conversation, uploads, private URLs) never enters it:
`web_pages` has no `user_id` or `conversation_id` column to leak through.

**Ranking, in plain terms.** Embedding distance alone cannot tell
*"Vice President of India"* from *"Vice President of the United States"* — the
two are nearly the same vector — and it has no opinion at all about whether a
page is from 2025 or 2026. So four signals are combined, weighted by how
volatile the question is:

| Signal | What it catches |
|---|---|
| dense similarity | topical relevance |
| lexical overlap | the exact entity (`india` vs `united states`) |
| source authority | `.gov`/`.nic.in` over a content farm |
| recency | a page read 2 days ago over one read 400 days ago |

For a time-sensitive question, evidence far older than the best available is
**dropped**, not merely ranked lower — handing the model both names and hoping
it picks the newer one is how a confident wrong answer happens. When two
comparably fresh, comparably authoritative sources genuinely disagree, the
prompt says so and the model is told not to feign certainty.

**Keeping it warm.** A background task inside the orchestrator drains the
embedding backlog (which previously only advanced when someone happened to run
a search) and re-reads pages past their TTL, most-retrieved first. Volatility
is inferred from the page — an office-holder page is re-read daily,
documentation every three weeks — and a changed content hash automatically
re-queues the page for embedding. No extra container: the queue is a
PostgreSQL column, so a restart resumes rather than forgets.

---

## 10. Citations: how a `[n]` is kept honest

```mermaid
flowchart TD
    F["pages actually fetched"] --> REG["<b>source registry</b><br/>1 → url/title, 2 → url/title, …"]
    REG --> PROMPT["prompt: 'cite only [1]…[24]'"]
    PROMPT --> GEN["model writes the report"]
    GEN --> CHECK{"each [n]<br/>in the registry?"}
    CHECK -->|yes| KEEP["kept"]
    CHECK -->|no| DROP["<b>removed</b> + counted in<br/>meta.research_run.invalid_citations_removed"]
    KEEP --> META["meta.sources — the same registry"]
    DROP --> META
    META --> UI["UI renders the source list"]

    CODE["arr[0] inside a code fence"] -.->|"held out of the pass"| GEN

    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef risk fill:#DC2626,color:#fff,stroke:none
    class REG,PROMPT,GEN,KEEP,META,UI app
    class DROP risk
```

Two things this deliberately does **not** do: it never touches `arr[0]` inside
a code block (that is a subscript, not a citation), and it never reflows the
document — removing a marker takes only its own adjacent space, so YAML
indentation and nested lists survive.

---

## 11. Salesforce

```mermaid
flowchart TB
    SFORG([Salesforce org]) -->|"Bulk API, read-only user"| SW["sync-worker<br/>every 30 min"]
    SW --> PARQ[("Parquet landing")]
    SW --> DUCK[("DuckDB warehouse")]
    SW --> LANCE[("LanceDB<br/>long-text chunks")]
    DUCK -->|"CHECKPOINT + atomic copy"| SNAP[("warehouse.read.duckdb<br/>the snapshot readers open")]

    Q([Salesforce question]) --> INTEL["sf_intel<br/>plan · validate · compile"]
    INTEL --> ASK{"ambiguous?"}
    ASK -->|yes| CLAR(["asks one clarifying question"])
    ASK -->|no| SQL["guarded SELECT<br/>one statement, no writes"]
    SQL --> SNAP
    SNAP --> ROWS["rows → deterministic<br/>Python summary"]
    ROWS --> ANSWER(["answer + table + chart + export"])

    INTEL -.->|"warehouse can't answer,<br/>or Live toggle"| LIVE["live_sf<br/>model writes SOQL → guard → REST"]
    LIVE --> SFORG

    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    classDef ext fill:#64748B,color:#fff,stroke:none
    class SW,INTEL,SQL,ROWS,LIVE app
    class PARQ,DUCK,LANCE,SNAP data
    class SFORG ext
```

The snapshot exists because DuckDB allows many readers **or** one writer. The
sync worker writes to the live file and publishes an atomic copy; readers only
ever open the copy, which removed the lock failures entirely.

---

## 12. Streaming: what the browser receives

The SSE vocabulary is closed — these eight names and no others.

```mermaid
flowchart LR
    subgraph EV ["SSE events"]
        T["<b>token</b><br/>answer text"]
        RE["<b>reasoning</b><br/>thinking deltas"]
        ST["<b>status</b><br/>one live line"]
        SP["<b>step</b><br/>pipeline stage<br/>running → done/failed"]
        RS["<b>research</b><br/>queries + links found"]
        ME["<b>meta</b><br/>ONE per turn — route, sources"]
        ER["<b>error</b>"]
        DN["<b>done</b>"]
    end

    T --> MSG["message body"]
    RE --> ACC["reasoning accordion"]
    ST --> LIVE["live status line<br/>with a ticking clock"]
    SP --> TL["pipeline timeline"]
    RS --> RP["research panel"]
    ME --> SRC["sources · badges · charts"]

    classDef app fill:#7C3AED,color:#fff,stroke:none
    class T,RE,ST,SP,RS,ME,ER,DN app
```

`meta` is special: the frontend **replaces** its local copy wholesale each time,
so exactly one is emitted per turn.

---

## 13. Data stores

```mermaid
flowchart TB
    subgraph PG ["PostgreSQL — app state, schema v11"]
        direction LR
        C["conversations · messages"]
        M["user_facts · summaries · chunks"]
        W["web_searches · web_results · web_pages"]
        R["web_crawls · <b>research_runs</b>"]
        U["uploads · documents · repos"]
    end

    subgraph FILES ["Files"]
        direction LR
        D[("DuckDB warehouse<br/>+ read snapshot")]
        L1[("LanceDB — Salesforce chunks")]
        L2[("LanceDB — web chunks")]
        RP[("reports")]
        WS[("workspaces")]
    end

    classDef data fill:#D97706,color:#fff,stroke:none
    class C,M,W,R,U,D,L1,L2,RP,WS data
```

Two **separate** LanceDB directories on purpose: the Salesforce RAG engine
renders every hit from its table as a CRM record citation, so a web page mixed
into it would surface as if it came from Salesforce.

---

## 14. Where everything runs

```mermaid
flowchart TB
    subgraph N1 ["DGX Spark node 1"]
        FE["frontend :3000"]
        OR["orchestrator :8080"]
        PGS["postgres :5432"]
        SXS["searxng — internal only"]
        SWS["sync-worker"]
        V1["vllm MAIN :8000"]
        V2["vllm-router :8002"]
        V3["vllm-embed :8003"]
        V4["vllm-ocr :8004"]
        V5["vllm-reranker :8005"]
    end
    subgraph N2 ["DGX Spark node 2 — cluster mode"]
        W["vllm worker<br/>tensor-parallel half of MAIN"]
    end
    V1 <-->|"NCCL over RoCE"| W

    classDef edge fill:#2563EB,color:#fff,stroke:none
    classDef app fill:#7C3AED,color:#fff,stroke:none
    classDef gpu fill:#16A34A,color:#fff,stroke:none
    classDef data fill:#D97706,color:#fff,stroke:none
    class FE edge
    class OR,SWS,SXS app
    class V1,V2,V3,V4,V5,W gpu
    class PGS data
```

Frontend, orchestrator, PostgreSQL and pgAdmin bind to `127.0.0.1` by default.
SearXNG has **no published port at all** — only the orchestrator reaches it,
over the internal Docker network.

---

## Where to go next

| You want | Read |
|---|---|
| Configuration, ports, quick start | [`README.md`](../README.md) |
| Deep Research in depth — budgets, why each default | [`docs/01-codebase/deep-research.md`](01-codebase/deep-research.md) |
| Why free search engines 403 / 429 / CAPTCHA | [`README.md` §10a](../README.md#10a-three-ways-to-answer-chat-web-search-deep-research) |
| The web search subsystem in code detail | [`docs/01-codebase/orchestrator-search.md`](01-codebase/orchestrator-search.md) |
| Formal C4 / sequence diagrams | [`docs/02-diagrams/`](02-diagrams/) |
| Every change, with its measurements | [`CHANGELOG.md`](../CHANGELOG.md) |
