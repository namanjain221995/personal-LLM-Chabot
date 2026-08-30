# `01-codebase/` — the permanent architecture reference

Twelve documents describing what this monorepo actually is, module by module and path by path. Every
factual claim in this directory carries a `path/to/file:LINE` reference back to the source; nothing is
inferred from a filename. Where something is genuinely unverified it says so.

Measured scope: **251 in-scope files · 43,189 LOC** — orchestrator 118 files/21,377 LOC · frontend
96/15,563 · sync-worker 28/4,024 · root 7/2,146 · searxng 2/79. Tests: 83 files/13,878 LOC against
168 source files/29,311 LOC (**0.47 test LOC per source LOC**), **1,141 passing, 0 failing**.
**Zero** `TODO`/`FIXME`/`HACK` markers anywhere in application source, and **no dead modules** — every
module is imported or is a documented entrypoint; the only dead symbol in the codebase is
`is_safe_select`.

---

## The architecture in one paragraph

A browser loads a single Next.js page and talks to **exactly one origin — itself**. Every backend call
goes through a same-origin `/api/*` route handler running in the Node runtime, which forwards to
`ORCHESTRATOR_URL`; the orchestrator's address is never exposed to the bundle. The FastAPI orchestrator
receives `POST /chat`, resolves the (single, local, unauthenticated) identity, registers a detached
`LiveGeneration` task that deliberately outlives the HTTP request, and returns a `StreamingResponse`
immediately. Inside that task, a cheap pre-flight classifier decides whether the turn deserves agent
planning or web search, a compaction pass sizes the prompt against the model's *real* window learned from
vLLM's `/tokenize`, and then a nine-branch dispatch picks one of the routed engines — PDF, vision, repo,
URL, agent, search, dataset, plain chat, or the LangGraph router, which in turn fans out to
sql / rag / vision / report / chat. Every engine streams through one `emit(event, data)` closure onto an
eight-name SSE allowlist (`token, meta, done, error, reasoning, step, status, research`); the browser's
hand-rolled parser accepts exactly the same eight and folds them into one message object per turn, ending
with a single `meta` frame that carries the SQL, the rows, the chart spec, the citations and the context
reading the proof drawer renders. The four model servers are all vLLM behind OpenAI-compatible endpoints
on one GB10 GPU. Out of band and entirely separately, a single-threaded sync worker polls Salesforce
every 30 minutes over JWT-bearer or client-credentials OAuth, pulls each object by Bulk API 2.0 (first
run) or incremental REST SOQL on `SystemModstamp` (thereafter), lands each batch as Parquet, upserts it
into DuckDB, re-embeds its long-text fields into LanceDB through the vLLM embedding server, and commits a
per-object watermark **last** — which is the one property that makes a re-sync idempotent. The
orchestrator reads that same DuckDB file `read_only=True` and that same LanceDB directory; the two
services share the `data` volume and never coordinate beyond DuckDB's single-writer rule.

```
browser ──/api/*──▶ Next.js handler ──ORCHESTRATOR_URL──▶ FastAPI orchestrator
                    (thin forwarder)                       │
                                                           ├─ pre-flight classifier (orchestrate)
                                                           ├─ compaction / context budget
                                                           ├─ engine dispatch ──▶ 13 engines
                                                           │                        │
                                                           │                        ▼
                                                           │                   4× vLLM (GB10)
                                                           └─ SSE ◀── emit() ── 8 event names
                                                                  │
                                                                  ▼
                                                            browser decoder ▶ UI + proof drawer

sync worker ──JWT/OAuth──▶ Salesforce ──▶ Parquet ──▶ DuckDB ──▶ LanceDB   (every 30 min, out of band)
                                                        ▲            ▲
                                                        └── read_only ┴── orchestrator reads
```

---

## What each document covers

| Document | Covers | Read it when |
|---|---|---|
| **[CRITICAL-PATHS.md](./CRITICAL-PATHS.md)** | End-to-end numbered call chains for the 8 flows that matter, with `file:LINE` at every hop and an inline note wherever a hop has no timeout, no retry, no bound, or swallows an exception. Ends each flow with "what breaks first". | **Start here.** It is the map the other eleven documents are the legend for. |
| **[orchestrator-context.md](./orchestrator-context.md)** | The platform spine: `main.py`, `graph.py`, `context.py`, `compaction.py`, `summarize.py`, `memory*.py`, `recall.py`, `history.py`, `sse.py`, plus `llm`, `auth`, `db`, `config`, `health`, `uploads`. | You need the request lifecycle, the token budget, the compaction machinery, or any HTTP route. |
| **[orchestrator-engines.md](./orchestrator-engines.md)** | All 15 modules in `engines/` — 11 routed engines, 2 pre-flight classifiers, 1 shared helper, 1 internal helper. | You need to know what a route actually does, or what an engine emits. |
| **[orchestrator-core.md](./orchestrator-core.md)** | The pure-logic layer under `engines/`: guards, parsers, profilers, renderers, protocol clients — and the three members that violate the package's own "no network, no GPU at import" contract. | You need `sql_guard`, `net`, `archive`, the chart pipeline, `salesforce`, `profile`, `extract`, `pdf`, `exports`. |
| **[deep-research.md](./deep-research.md)** | The iterative research mode: plan → search → read → audit the gaps → search again → cited report. Its loop, budgets, citation validation, category routing and the V11 `research_runs` table. | You are looking at Deep Research, citation integrity, or why a research run stopped when it did. |
| **[orchestrator-search.md](./orchestrator-search.md)** | The `search/` provider package (SearXNG, Tavily, Brave) plus the 504-LOC engine that drives it: rewrite → search → round-robin merge → SSRF-safe fetch → extract → cited answer. | You are looking at web search, SSRF, the fetch budget or the research panel. |
| **[data-model.md](./data-model.md)** | The three persistence layers — SQLite app state, the DuckDB warehouse, the LanceDB vector index — plus Parquet and ephemeral workspaces. Schemas, indexes, foreign keys and what is missing. | You need a schema, a retention answer, or to know who writes what. |
| **[frontend.md](./frontend.md)** | The app shell, the 32 components and the 14 `lib/` modules. | You are working in the browser tier. |
| **[frontend-api-contracts.md](./frontend-api-contracts.md)** | The authoritative wire spec: the 10 Next.js route handlers, the orchestrator routes behind them, request/response shapes, and the SSE event contract as both sides implement it. | You are changing anything that crosses a process boundary. |
| **[sync-worker.md](./sync-worker.md)** | The Salesforce → Parquet → DuckDB → LanceDB service: 11 modules, `config.yaml`, the auth stack, the watermark and its idempotency guarantees. | You need the warehouse to be right. |
| **[infra-docker-compose.md](./infra-docker-compose.md)** | The 355-line compose file: 7 services, 3 volumes, GPU reservations, healthchecks, published ports, the model-serving flags and the memory arithmetic. | You are deploying, or asking why a port is open. |
| **[security-model.md](./security-model.md)** | Authentication (there is none), authorization, the input guards that are genuinely good, the one that is provably weaker than its docstring, secret hygiene, and network exposure. | Before you expose this on any network. |
| **[test-map.md](./test-map.md)** | All 83 test files with counts and what each actually asserts; the gap analysis against the 8 critical paths; the tests that cannot fail; the 10 highest-value tests to add next. | You are about to change something and want to know what will notice. |

---

## Reading order for a newcomer

1. **[CRITICAL-PATHS.md](./CRITICAL-PATHS.md)** — flows 1, 2 and 4 first. Forty minutes here saves a day
   of grepping. You will finish knowing where a request goes and where it can hang.
2. **[frontend-api-contracts.md](./frontend-api-contracts.md)** — the two-tier proxy and the eight SSE
   event names. This is the narrowest, most stable interface in the system.
3. **[orchestrator-context.md](./orchestrator-context.md)** — `main.py`'s 412-LOC `chat()` handler and
   the `LiveGeneration` registry. Everything else in the orchestrator is downstream of this file.
4. **[data-model.md](./data-model.md)** — three stores, two writers, one shared volume. Read this before
   you touch persistence, because the ownership rules are not symmetric.
5. **[orchestrator-engines.md](./orchestrator-engines.md)** — then dip into
   **[orchestrator-core.md](./orchestrator-core.md)** and
   **[orchestrator-search.md](./orchestrator-search.md)** for whichever engine you landed in.
6. **[frontend.md](./frontend.md)** — the client tier, once you know what it is decoding.
7. **[sync-worker.md](./sync-worker.md)** — a self-contained service; read it whole or not at all.
8. **[security-model.md](./security-model.md)** and **[infra-docker-compose.md](./infra-docker-compose.md)**
   — read as a pair. The security posture is a property of the compose file as much as of the code.
9. **[test-map.md](./test-map.md)** — last, as a checklist for whatever you are about to change.

**If you have fifteen minutes**, read [security-model.md](./security-model.md) §(a) and flow 1 of
[CRITICAL-PATHS.md](./CRITICAL-PATHS.md). That is the shortest path to understanding both what the system
does well and what it does not defend.

---

## Conventions

- **Every claim has a line reference.** `[sql.py:200](../../orchestrator/app/engines/sql.py#L200)`.
  Paths are relative to this directory, which sits two levels below the repository root.
- **No secret values appear anywhere** — variable *names* and `file:LINE` only.
- **Finding IDs** (`SEC-01`, `PERF-01`, `DATA-02`, …) are stable across all twelve documents and are
  defined once in the audit report under [`../03-report/`](../03-report/). A module or path section
  listing a finding ID is asserting that the defect is present on that code, at those lines.
- Sections that are genuinely empty say **"None."** and why. Nothing is left as a placeholder.
