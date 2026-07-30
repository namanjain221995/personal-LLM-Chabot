# Test map

Every test file in the repository, what it targets, what it actually asserts, and whether it touches a
real socket or the GPU. Then a gap analysis against the eight critical paths, and the ten highest-value
tests to add next.

## Verified run results

| Suite | Command | Result |
|---|---|---|
| Orchestrator | `cd orchestrator && python3 -m pytest tests/ -q` ([README.md:298](../../README.md#L298)) | **800 passed in 41.6 s** |
| Sync worker | `docker compose run --rm -v ./sync-worker/tests:/app/tests sync-worker python -m pytest tests/ -q` ([README.md:300-302](../../README.md#L300-L302)) | **104 passed in 1.1 s** |
| Frontend | `cd frontend && npm test` → `vitest run` ([frontend/package.json:10](../../frontend/package.json#L10)) | **237 passed, 16 files** |
| **Total** | | **1,141 passing · 0 failing** |

The reported counts are **post-expansion**: `def test_` / `it(` definitions number 659 (orchestrator),
97 (sync worker) and 224 (frontend); the difference is `@pytest.mark.parametrize` and `it.each`.

Ratio: **83 test files / 13,878 test LOC** against **168 source files / 29,311 source LOC** —
**0.47 test LOC per source LOC**.

## There is no CI — `TEST-01`

Verified absent: `.github/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`, `azure-pipelines.yml`,
`.travis.yml`, `.drone.yml`, `Makefile`, pre-commit config. Also absent: `pytest.ini`, `pyproject.toml`,
`setup.cfg`, `tox.ini` — the only pytest configuration in the repo is the three-line `sys.path` shim at
[orchestrator/conftest.py:5](../../orchestrator/conftest.py#L5), so there are **no markers, no
`asyncio_mode`, no `--strict-markers`, no coverage gate and no `filterwarnings`**. No coverage tooling is
installed at all (`.coverage` is gitignored at [.gitignore:62](../../.gitignore#L62) but nothing produces
it). Git history is a single commit.

**Nothing runs these 1,141 tests except a human typing the three commands above.** The frontend's own
`npx tsc --noEmit` and `npm run lint` ([frontend/README.md:39](../../frontend/README.md#L39)) are
documented as manual steps that nothing enforces.

The sync-worker suite is additionally *unreachable from the built image*:
[orchestrator/.dockerignore:5](../../orchestrator/.dockerignore#L5) excludes `tests/`, and the sync-worker
command has to bind-mount its tests back in.

---

## 1. Orchestrator — `orchestrator/tests/` (52 test files · 659 defs · 8,913 LOC)

`n` = `def test_` definitions before parametrize expansion.
Kind: **U** unit · **I** integration (HTTP `TestClient`, real SQLite, real DuckDB, real temp files) ·
**S** source-string introspection (asserts on the *text* of a module, not its behaviour).

| File | Target module(s) | n | What it actually asserts | Kind | Net/GPU |
|---|---|---|---|---|---|
| `test_agent.py` | `engines/agent.py` | 14 | Plan JSON validation (≤`MAX_STEPS`, kinds, dup ids), retry-then-llm-fallback, step events running→done, `STEP_CONCURRENCY` cap ≥2/≤3, `merge_step_meta` last-sql + union citations, one full offline `run_agent_engine` producing exactly one `meta` | U + I | no |
| `test_agent_salesforce_gate.py` | `engines/agent.py` | 3 | `salesforce=False` coerces sql/rag→llm and the sql engine is never called; history reaches the llm step; `salesforce=True` still runs sql | U | no |
| `test_agent_web_step.py` | `engines/agent.py`, `main.py` | 18 | `web` is a valid kind, both planner prompts offer it, `coerce_allowed(web=False)` downgrades it, web-step output+sources, prose `[n]` markers renumbered with the metadata, renumber happens before synthesis, `main.py` routes agent before search | U + **S** | no |
| `test_archive_safety.py` | `core/archive.py`, `core/profile.py` | 18 | Zip-slip / NUL / absolute names rejected, symlink+device members skipped, four bomb caps incl. a **lying central directory** caught while streaming, `.xlsx` faces container caps before openpyxl, `.pkl`/`.xlsm` refused, nested archives listed not opened, magic-byte sniffing | U (real temp files) | no |
| `test_auth.py` | `auth.py`, `db.py`, `main.py` | 13 | `/auth/login|register|logout` all 404, history reachable with no credentials, stale `ts_session` ignored, oldest existing account adopted, `LOCAL_USERNAME` overrides, resolution cached, per-user scoping holds, no argon2 hash, no session-secret file | I | no |
| `test_chart_data.py` | `core/chart_data.py` | 13 | Deterministic bin counts, clamping, every observation in exactly one bin, max value not dropped, constant column → 1 bin, non-numeric skipped, `'true'/'false'` not binned, stability across calls | U | no |
| `test_chart_decision.py` | `core/chart_decision.py`, `chart_profile.py` | 46 | Explicit trigger regex + natural phrasing + negatives, named-type extraction incl. horizontal>bar precedence, trusted stage order, explicit-mode decisions, hybrid rules, unaggregated/wide/record-listing refusals, suppression beats a chart word, `to_prompt_dict` carries no cell values | U | no |
| `test_chart_pipeline.py` | `core/chart_pipeline.py` | 19 | Deterministic path never calls the model, funnel rows reordered, histogram binned in Python, model path only for ambiguous shapes, model spec refused for ghost column / text measure / extra keys / garbage, a raising model call does not propagate, prompt carries no row values | U | no |
| `test_chart_routes.py` | `engines/sql.py`, `engines/agent.py` | 13 | Direct-SQL route charts an explicit request with exactly one `meta`; funnel ships stage-ordered `chart_data` beside query-ordered `data`; agent route carries the whole sql payload; both routes agree; chart intent read from the user message; a chart-model exception still yields answer+sql+table; events ⊆ `ALL_EVENTS`; JSON round-trip; legacy 5-key payload | I (real DuckDB) | no |
| `test_chart_spec.py` | `core/chart_spec.py` | 16 | Pydantic validation, `wire_dump()` emits exactly the five legacy keys, `model_dump()` carries the new optional ones, `x`/`y` aliases accepted not emitted, bad type / extra field / empty x / empty y rejected, fenced + garbage + non-dict parsing, column-membership enforced | U | no |
| `test_charts_png.py` | `core/charts_png.py` | 12 | `PNG_SUPPORTED ∪ PNG_TABLE_ONLY == CHART_TYPES` and disjoint, funnel is table-only, every supported type renders >1000 bytes, unsupported/empty/missing-column/all-zero-pie raise, raw dict refused, bar≠hbar and pie≠donut and stacked≠grouped bytes, `matplotlib.pyplot` still absent after reload | U (writes temp PNGs) | no |
| `test_chat_modes.py` | `engines/router.py`, `llm.py`, `main.py` | 18 | Router "chat" class + few-shots, smart/fast → main/router model, effort = `enable_thinking` not a system line, `stream_chat_events` yields reasoning/token pairs with the right `extra_body`, `POST /chat` mode=assistant bypasses router **and** DuckDB (both monkeypatched to raise), salesforce chat streams via the graph, meta reports the *serving* model | I | no |
| `test_citations.py` | `core/citations.py` | 6 | Default Lightning base URL, record URL building incl. trailing slash, citation shape, order-preserving dedupe with skip-on-missing-id, custom base | U | no |
| `test_compaction.py` | `compaction.py`, `summarize.py`, `recall.py`, `db.py` | 25 | Budget maths reserves *this* request's output, floor never breached, fold boundary monotone, assembly order system→summary→retrieved→recent, idempotent folding, incremental second pass, failure non-fatal, condense at cap, **3-way concurrent compaction folds once**, a turn-3 fact survives 200 turns, session isolation, background notice rides the next reply, **background-vs-synchronous race writes the summary exactly once**, a summarizer-omitted fact recovered by retrieval in one labelled block, adaptive keep-recent | U + I (real SQLite) | no |
| `test_config.py` | `config.py` | 12 | `MAIN_MODEL`/`RERANKER_MODEL` defaults and env overrides, all six vLLM sidecar defaults, trailing-slash stripping, `OPENAI_BASE_URL` default, CORS default has no `*` and includes localhost:3000, `CHART_TRIGGER_MODE` defaults to explicit and unknown values fall back | U | no |
| `test_context_budget.py` | `context.py`, `engines/__init__.py` | 23 | 8000-requested vs 8192-window clamped, large window untouched, budget never ≤0, oldest turns trimmed while pinned system blocks + newest survive, estimate is pessimistic, `service_root`, char clipping, `count_tokens` falls back to an estimate against a dead port, `recent_turns` keeps system blocks, oversized message clipped in the middle keeping head+tail, pathological loop terminates, trim notice reaches `/chat` meta | U + I | **yes — a real TCP connect to `http://127.0.0.1:9` at `:168-176`**, depends on that port being closed |
| `test_conversation_integrity.py` | `history.py`, `db.py`, `main.py`, `health.py` | 22 | PUT /messages refuses to shrink (409) and is atomic on a bad row, cross-owner 404, `MessageCountWouldShrink`, POST /chat ownership check, truncate is the only shrink and takes two integers, optimistic `expected_total`, generation-id dedupe incl. an **8-thread race**, migrations add `generation_id` and delete pre-existing duplicates, startup lifespan migrates before serving, `/health` reports `app_db` | I + real SQLite + OS threads | no |
| `test_dataset_profile.py` | `core/profile.py`, `engines/dataset.py`, `uploads.py` | 14 | Shape/type/null% profiling, unreadable file does not raise, file cap, `.pkl` never profiled, `clip()` truncation, top-values capped, **dual prompt-injection canary never reaches the assembled prompt**, profile wrapped in `DATA_START/END` with a distrust instruction, expiry fails soft, uploads never cross conversations, string columns report lengths | U + I | no |
| `test_effort_depth.py` | `engines/{search,agent,chat}.py` | 16 | `query_budget` low<medium<high and fast=0, cap applied, rewriting on the router model only, failed rewrite falls back, `step_budget` medium<high ≤`MAX_STEPS`, the planner prompt carries the level's count, `_SYNTH_TOKENS[high] > [medium]`, effort reaches `_run_step_impl`, temperature/`max_tokens` **literals present in source** | U + **S** | no |
| `test_endpoints.py` | `health.py`, `main.py` | 9 | `/health` ok/degraded with probes mocked, checks are exactly `{vllm, vllm-router, vllm-embed, duckdb, app_db}`, real DuckDB round-trip + missing-file error, `service_root`, `/reports` empty/list/serve/404, `..` in a filename ⇒ 400 | I | no |
| `test_exports.py` | `core/exports.py` | 5 | `slugify`, timestamped filename regex, xlsx round-trip with bold header + auto widths + dict coercion, xlsx cap, csv round-trip with `None`→`""` | U | no |
| `test_extract.py` | `core/extract.py` | 6 | HTML title+text with script dropped, plain-text passthrough, unsupported type raises, empty content-type defaults to HTML, boundary truncation, PDF dispatch via a monkeypatched `render_pdf` | U | no |
| `test_history.py` | `history.py` | 7 | History reachable with no credentials, create/list/detail shape incl. `pinned`/`archived`, client-supplied id + 409 + 400, message round-trip with meta, ordering by activity, rename/delete + 400/404, another owner's conversation is 404 on every verb | I | no |
| `test_history_search.py` | `history.py`, `db.py` | 33 | `GET /history/search`: owner scoping, title vs message match and snippet preference, one row per conversation, archived/pinned flags, case-insensitivity, **`%`/`_`/`\` are literal**, empty/whitespace/missing `q`, 100-char limit, `limit` default 50 / cap 100 / 422, snippet windowing head/middle/tail, pinned-first ordering, route does not shadow `/conversations` | I | no |
| `test_history_v3.py` | `db.py`, `history.py` | 16 | Migration of a real V2 DB adds `pinned`/`archived` without touching rows and is idempotent, defaults, PUT pin/archive round-trips and field subsets, archiving/pinning does not bump `updated_at`, unknown field ⇒ 422, empty title ⇒ 400, `?archived` filter, pinned-first ordering, owner scoping | I + real SQLite | no |
| `test_imports.py` | 18 app modules | 2 | The app imports with **torch/transformers/weasyprint/lancedb/matplotlib.pyplot absent from `sys.modules`**; the LangGraph graph compiles | U (import contract) | no |
| `test_live_generation.py` | `main.py` (`LiveGeneration`) | 7 | `follow()` replays the buffer then streams live, `_finalize_generation` persists only when detached and not cancelled, `/chat/active` empties and `/chat/attach` 404s after completion, `/chat/stop` on nothing is a no-op, a same-conversation resend cancels the previous task, attach/stop/active are owner-scoped, `_owns` identity matrix | U + I | no |
| `test_live_salesforce.py` | `core/salesforce.py`, `engines/{live_sf,sql,agent}.py` | 37 | SOQL guard adds/lowers `LIMIT`, refuses non-SELECT and stacked statements, tolerates a trailing `;`, allows subqueries, `COUNT()` gets no LIMIT; `merge_rows` overlay semantics; `configured()`; agent `salesforce` step incl. graceful `SalesforceUnavailable`; `references_a_known_table` blocks `SELECT 0 AS record_count`; schema-question detection; `describe_object` rejects an injected name; `wants_live_lookup`; the narrative prompt must say LOCAL SYNCED COPY | U + **S** | no |
| `test_llm_clients.py` | `llm.py`, `engines/{router,vision}.py` | 8 | Router call hits `ROUTER_BASE_URL` at temperature 0 with a small `max_tokens`; `route_request` parses; an image forces `vision` without building a client; `to_data_url`; multimodal content shape; the vision engine streams then emits exactly one `{"route":"vision"}` meta; embeddings payload `{model, input}` and results re-sorted by index | U (fake client) | no |
| `test_memory_recall.py` | `memory_recall.py` | 8 | Keyword extraction drops stopwords/short words, dedupes and caps; empty for stopword-only input; recall block formatting incl. an "ignore" instruction; injected search receives `(user_id, keywords, exclude, limit)`; no keywords ⇒ no search | U | no |
| `test_net_ssrf.py` | `core/net.py` | 9 | Private/loopback/link-local/metadata/IPv6 literals blocked, public literals pass, scheme must be http(s), missing host blocked, a hostname resolving to a private IP blocked, **mixed public+private (DNS rebinding) blocked**, `safe_fetch` re-validates a 302 to a private IP, body returned and `max_bytes` enforced | U (mocked `getaddrinfo` + `MockTransport`) | no |
| `test_orchestrate.py` | `engines/orchestrate.py` | 17 | Plan JSON parsing incl. prose-wrapped and only-literal-`true`, fast never calls the classifier, low may search but never agents, `allowances()` ceilings incl. unknown⇒medium, classifier failure degrades, input clipped, system blocks not fed to the classifier, few-shots teach both directions, high plans whenever it searches and is never weaker than medium | U | no |
| `test_recall.py` | `recall.py` | 12 | Vector pack/unpack round-trip, cosine edge cases, chunk overlap and trivial-content skip, folded turns indexed and retrieved with `RECALL_HEADER`, `top_k` bound, nothing before folding, disabled by flag, embedding failure returns `None` instead of raising, retrieval never crosses sessions, chunks deleted with the conversation | U + real SQLite | no |
| `test_recall_db.py` | `db.py` | 5 | Cross-chat recall finds the relevant other conversation, excludes the current one, is user-scoped, empty keywords ⇒ `[]`, `%` is literal | U + real SQLite | no |
| `test_repo.py` | `core/repo.py`, `core/repo_index.py`, `db.py` | 10 | GitHub URL detection (`.git`, `/tree/`, `/blob/`, non-GitHub), chunk line ranges with overlap, overview (languages/entry points/key configs/README), oversize repo rejected **before** clone, too-many-files rejected after clone with cleanup, chunk storage + keyword search + path weighting | U (subprocess monkeypatched) | no |
| `test_report_charts.py` | `engines/report.py` (`_sql_section` only) | 8 | A healthy section has prose+table+chart; a matplotlib exception leaves prose and table intact; a failing chart model leaves the section intact; an unsupported type yields the table and no PNG; empty result → no chart; `chart: false` → none; a zero-byte PNG is not embedded; report charts bypass the "did the user say chart" check | I | no |
| `test_report_paths.py` | `core/report_paths.py` | 7 | Valid filename resolves without requiring existence, 13 hostile filenames rejected (`..`, absolute, nested, backslash, NUL, dotfile, `report..v2.pdf`), **symlink escaping the reports dir rejected**, symlink inside allowed, listing skips dotfiles and subdirs, missing dir ⇒ `[]` | U | no |
| `test_router_parse.py` | `engines/router.py` | 12 | `parse_route` for plain/fenced/prose-wrapped/`<think>`-prefixed JSON, uppercase normalisation, every route in `ROUTES`, and `None` for garbage/unknown/wrong-key/non-dict/`None`/int | U | no |
| `test_row_caps.py` | `core/exports.py`, `config.py` | 7 | `PREVIEW_ROW_CAP == 500`, `EXPORT_ROW_CAP == 100_000`, truncation flags at and around each boundary, config defaults match | U | no |
| `test_salesforce_toggle.py` | `main.py`, `engines/{sql,chat,router}.py` | 20 | With Salesforce ON auto web-search detection never runs but the agent classifier does; explicit `web_search=on` bypasses the warehouse; assistant mode never touches the graph; the gate **literal** `auto_web_search_allowed = request.mode == "assistant"` is present in `main.py`; the SQL prompt warns checkboxes are text `'true'`; the narrative runs with `thinking=False`/`max_tokens=6000` and has an empty-answer fallback; the narrative is told the real row count; the agent gets `web=False` in Salesforce mode; short follow-ups inherit the previous question | I + **S** | no |
| `test_search_breadth.py` | `engines/search.py` | 16 | Round-robin merge so later queries are not discarded, rank-1-of-each leads, high>medium>low source counts, low still 10, URL normalisation dedupe (www/http/trailing slash/`utm_*`/`fbclid`), per-domain cap incl. subdomains, relaxation to `_MIN_SOURCES`, registrable-domain extraction, one dead query does not sink the others, all-dead raises, char tiering keeps the prompt <140k, answer prompt asks for breadth and disagreement | U | no |
| `test_search_engine.py` | `engines/search.py` | 6 | `should_search` heuristic, per-user rate limit, query rewriting parses JSON and caps at 3, happy path emits Searching/Reading statuses and a numbered+domained sources meta, provider unavailable ⇒ answer from knowledge with `search_unavailable: true` — **plus one vacuous test** (F-7b below) | U | no |
| `test_search_off.py` | *nothing* — re-implements the gate inline | 2 | **Vacuous** (F-7a below): asserts a locally recomputed boolean is `False`, then `assert True` | — | no |
| `test_search_providers.py` | `search/{searxng,tavily,brave,base}.py` | 6 | SearXNG result parsing with url-less rows skipped and `max_results` honoured, 502 ⇒ `SearchUnavailableError`, Tavily and Brave payload shapes, factory selects by `SEARCH_PROVIDER` and raises when the matching credential/URL is missing | U (`MockTransport`) | no |
| `test_sf_dictionary.py` | `core/sf_dictionary.py` | 10 | Export-row parsing, a question pulls the object it names with API name = label pairs, the named object outranks a field-sharing one, unrelated/common-word questions add nothing, a missing dictionary is not fatal, the hint warns a wrong name returns no rows, `MAX_OBJECTS` cap, both `sql._ask_sql` and `live_sf.write_soql` consult it | U + **S** | writes the **fixed path** `/tmp/many.json` (`:82,84`) instead of `tmp_path` |
| `test_sql_engine_meta.py` | `engines/sql.py` | 3 | DuckDB `enable_external_access=false`: `read_text`/`glob`/`read_csv(https://…)` all raise while normal queries work; exactly one `meta` with `route`/`data` as row objects/top-level `truncated`, emitted after every token; exports ride `report_files [{filename,type,size}]` | I (real DuckDB) | no |
| `test_sql_guard.py` | `core/sql_guard.py` | 14 | Accepts SELECT/CTE/comments/lowercase and keywords inside literals; rejects 15 write/DDL/PRAGMA/INSTALL/LOAD/SET/CALL forms, 4 multi-statement forms, 6 comment-smuggled forms (`DR/**/OP`), 10 file/network table functions incl. case+space, CTE-wrapped INSERT, junk/`EXPLAIN` | U | no |
| `test_sse.py` | `sse.py` | 6 | `ALLOWED_EVENTS` is exactly `{token, meta, done, error}`, exact byte framing of each, unknown event type raises, non-serializable values fall back to `str` | U | no |
| `test_sse_v2.py` | `sse.py` | 8 | v1 frames byte-identical, `V2_EVENTS={reasoning,step}`, `PROGRESS_EVENTS={status}`, `RESEARCH_EVENTS={research}`, `ALL_EVENTS` is their union, reasoning/step framing with and without `detail`, unknown step status raises, unknown event type still raises | U | no |
| `test_system_normalization.py` | `llm.py`, `sse.py` | 11 | The real 4-system-block shape folds into one leading system message; never >1 system and never after a turn; no-system untouched; input not mutated; block order and `\n\n` separation preserved; empty blocks dropped; multimodal list content passed by identity; all five send paths contain `normalize_system`; **every `emit("…")` in the package is on `ALL_EVENTS`** (filesystem walk) | U + package-wide **S** | no |
| `test_url_engine.py` | `engines/url.py`, `db.py` | 3 | URL-document upsert round-trip, fetch→extract→store→cite with a `Reading` status and `route: url` meta, **a follow-up on a stored URL performs zero fetches** | I + real SQLite | no |
| `test_urls.py` | `core/urls.py` | 8 | URL extraction dedupes, strips trailing punctuation, ignores non-http, honours a limit; chunking with overlap and small-text passthrough; `select_relevant` keeps the pertinent chunk within a char budget | U | no |
| `conftest.py` *(support)* | fixtures | 0 | `isolated_app_db` gives each test its own `tmp_path/appdb` and patches `settings.app_db_path` + `session_secret_file`; `reset_local_user` clears the private `auth._cached_user_id`; `as_user` switches `LOCAL_USERNAME` and materialises the row | — | no |
| `__init__.py` *(support)* | — | 0 | Empty package marker | — | no |

Plus [orchestrator/conftest.py](../../orchestrator/conftest.py) *(support, 5 LOC)* — a single
`sys.path.insert(0, dirname(__file__))` at `:5` so `import app.…` resolves from any cwd. **This is the
only pytest configuration that exists.**

---

## 2. Frontend — `frontend/tests/` (16 files · 224 `it()` · 3,195 LOC)

Runner `vitest run`, `environment: 'node'`, `include: tests/**/*.test.ts`
([frontend/vitest.config.mts:4-6](../../frontend/vitest.config.mts#L4-L6)). **No jsdom, no Testing
Library, no React rendering anywhere** — `.test.tsx` is not even matched, so all **5,618 LOC of
`frontend/components/` are untested**. Every file below targets a pure module. None touches the network
or the GPU.

| File | Target module(s) | n | What it actually asserts | Kind |
|---|---|---|---|---|
| `attachments.test.ts` | `lib/attachments.ts` | 6 | `base64FromDataUrl` strips/nulls correctly; a remembered PDF payload is returned for resend; an image is rebuilt from the persisted data URL after a reload; **a PDF turn reports `missing: true` rather than silently changing the question** | U |
| `chartOption.test.ts` | `lib/chartOption.ts`, `lib/chartTheme.ts` | 28 | All nine types build an option; unknown type ⇒ `null`; axis/stack/colour per type; donut vs pie radius; part-to-whole folds a tail into "Other"; **funnel `sort: 'none'` and original order**; histogram renders pre-binned rows; validation (`no-data`, `missing-x/y-column`, `no-numeric-values`, `'true'` not a measure, `scatter-needs-numeric-x`); legacy 5-key payload; `escapeHtml` on tooltip values; unknown spec keys never reach ECharts; theme tokens with a fake `window` | U |
| `chat-contract.test.ts` | `lib/orchestrator.ts` | 12 | `lastUserContent`; request mapping incl. `image_base64: null`, default `session_id`, image-only prompt substitution, whitespace-only ⇒ `null`, V2 fields forwarded, `agent:false` kept explicit, and v1 bodies carry exactly four keys | U |
| `contextMeter.test.ts` | `lib/contextMeter.ts` | 21 | Threshold boundaries 60/85/95 exactly, percent rounding/clamping, server total + debounced draft estimate, default budget before the first reply, breakdown rows and labels, **the popover total equals the ring's numerator (no double-counted reservation)**, `latestUsage` reads the newest reply that carried a reading | U |
| `conversation-menu.test.ts` | `lib/conversationMenu.ts` | 18 | Item list/order/labels incl. pinned/archived variants; each item calls the right store method; **delete needs a confirm step**; cancel destroys nothing; keyboard map (Escape/Tab/arrows with wrap/Home/End/Enter/Space); placement flips above and right-aligns and never clips | U |
| `errors.test.ts` | `lib/errors.ts` | 12 | Extracts the message from a python-repr and from real JSON; the context-overflow 400 becomes a plain sentence **with no braces/codes/token arithmetic** while the raw payload stays in `detail`; connection-refused and CUDA-OOM mappings; unknown errors fall back; `trimNotice` singular/plural | U |
| `export-markdown.test.ts` | `lib/exportMarkdown.ts` | 10 | Filename `<slug>-<id>.md`, never empty, ≤48 chars; **exact markdown bytes** for a 4-turn thread; SQL in a fenced `sql` block after the answer; citation record IDs listed; sections omitted when absent; an errored turn renders `_Error: …_`; always ends with a newline | U |
| `history-server.test.ts` | `lib/history.ts`, `lib/historyApi.ts` | 32 | Write-through create/append/rename/delete against a fake server that **re-implements the real 409 no-shrink and truncate concurrency rules**; incremental append; offline cache + dirty retry; pending deletes completed; server-side deletions dropped locally; lazy `load()`; one-time migration incl. "not marked done when unreachable"; pin/archive round-trip and rebuild-preserves-flags; export; account switching clears the cache; the three Phase-0-critical regressions; truncate is the only door | U (hand-written fake API) |
| `history.test.ts` | `lib/history.ts` | 11 | Title from first message with 40-char truncation and whitespace collapse; local CRUD and newest-first listing; meta persisted; rename trims and ignores blanks; pinned-first + hide-archived ordering; flag flips do not disturb recency; **QuotaExceeded drops the oldest repeatedly and reports each eviction**; non-quota errors rethrow | U |
| `mermaid.test.ts` | `lib/mermaid.ts` | 16 | `mermaid`/`mmd` fence detection; **`looksRenderable` is false while a block is still streaming** and ignores `%%` comments; filename slugging; zoom clamping incl. NaN; `prepareSvgForExport` sets concrete px, adds `xmlns` and a background rect before content; `svgNaturalSize` from the viewBox; `fitZoom` shrink/grow/floor | U |
| `pasted.test.ts` | `lib/pasted.ts` | 10 | Paste-to-chip thresholds by chars and by lines (both boundaries); line/char counting; `foldModelContent` prepends blocks in order, preserves code verbatim, drops whitespace-only parts; mime→extension mapping with fallbacks | U |
| `prefs.test.ts` | `lib/prefs.ts` | 6 | `DEFAULT_PREFS` is exactly `{salesforce:true, model:'smart', effort:'medium', agent:false, webSearch:'auto'}`; per-conversation independence; draft prefs adopted on first send and the draft slot resets; **prefs written by the removed toggles are migrated on read**; corrupt payloads sanitise per field; removal | U |
| `research.test.ts` | `lib/sse.ts`, `components/ResearchPanel.tsx` | 13 | `research` phases parse; malformed results dropped; unknown phases and bad JSON ⇒ `null`; live research folded onto `meta` and **marked inactive so a stored panel does not spin forever**; server-supplied research never overwritten; elapsed formatting incl. negatives; source counting dedupes a page found twice; domain ranking sums to the source count | U |
| `sse.test.ts` | `lib/sse.ts` | 22 | Framing: single event, full token→meta→done sequence, errors, **an event split mid-field across six chunks**, several events plus a partial tail in one chunk, CRLF incl. split pairs, multi-`data:` joining, comment/keep-alive lines, default `message` type; contract mapping for all six shapes; unknown types and bad JSON dropped; unknown meta keys pass through; `readChatStream` ignores `ping` and `shiny_future_event`; `mergeStep` upsert semantics; `foldStreamState` merge without mutating input | U |
| `streams.test.ts` | `lib/streams.ts` | 3 | `attachBaseTurns` keeps everything through the last user message and **drops a trailing assistant answer** so the server replay rebuilds it; empty threads | U |
| `websearch.test.ts` | `lib/sse.ts`, `lib/orchestrator.ts` | 4 | `status` event parsing and malformed rejection; `web_search` forwarded when set and omitted when not | U |

---

## 3. Sync worker — `sync-worker/tests/` (10 files · 97 defs · 1,190 LOC)

Not reachable from either documented suite command without a bind mount
([README.md:300-302](../../README.md#L300-L302)). Nothing here touches a real network or the GPU;
`test_jwt.py` does real RSA key generation on the CPU.

| File | Target module(s) | n | What it actually asserts | Kind | Net/GPU |
|---|---|---|---|---|---|
| `test_chunking.py` | `syncworker/chunking.py` | 7 | Empty/whitespace ⇒ `[]`; 50 words ⇒ one chunk; exactly 800 words ⇒ one chunk byte-identical; 801 ⇒ two chunks with a verified 100-token overlap; 1750 ⇒ `[800,800,350]` with lossless reassembly; custom sizes step correctly; invalid overlap/chunk sizes raise | U | no |
| `test_config.py` | `syncworker/config.py` + shipped `config.yaml` | 4 | The six core CRM objects are configured; ≥6 objects and no duplicate names; every object has `Id`+`SystemModstamp`; `rag_fields ⊆ fields`. **Tests 3 and 4 are tautological** — the loader already raises on both | U (reads real `config.yaml`) | no |
| `test_discovery.py` | `syncworker/main.py` (`adopt_new_fields`, `report_new_objects`) | 9 | A describe-only field is adopted; a `textarea` lands in both `fields` and `rag`; compound types never adopted (parametrised); no duplicates; `sync_max_fields` caps adoption; a describe failure leaves configured fields untouched; only `__c` objects reported as new; configured objects not reported; a reporting failure returns `[]` | U (fakes) | no |
| `test_embeddings.py` | `syncworker/rag_index.py` (`OpenAIEmbedder` only) | 6 | Exactly one request to `{BASE_URL}/embeddings` with body `{"model", "input"}`; vectors return in input order; 70 texts batch as `[32,32,6]` and concatenate in order; a trailing slash is normalised; a vector-count mismatch raises `RuntimeError`; a 503 raises for the caller to fail soft | U (`MockTransport`) | no |
| `test_jwt.py` | `syncworker/sf_auth.py` | 6 | Header `alg == RS256` and claims `iss/sub/aud` with `exp == now + 180`; a foreign public key raises `InvalidSignatureError`; a stale assertion raises `ExpiredSignatureError`; with `client_secret` the POST is `grant_type=client_credentials` and carries **no `assertion`**; the "My Domain" and "Run As" error hints fire | U | CPU-only 2048-bit RSA keygen |
| `test_limits.py` | `syncworker/sf_client.py` (`parse_limit_info`, `check_api_limits`) | 7 | Basic and multi-entry `Sforce-Limit-Info` parsing; malformed/missing ⇒ `None`; no warning below 0.80; exactly one WARNING at exactly 80 % carrying `api_used`/`api_total`; warns above; a malformed header never warns or crashes | U (`caplog`) | no |
| `test_objects_cli.py` | `syncworker/objects.py` | 30 | `add` produces `Id … SystemModstamp` in order and injects the required pair; `add-fields` merges rather than replaces; the last object cannot be removed; the comment header survives an edit; CLI output still loads in `load_object_configs`; invalid object/field names raise; sheet import keeps only describe-visible fields, drops compound types, routes `textarea` to `rag_fields`, skips FLS-hidden and non-readable objects with notes, trims at `MAX_FIELDS_PER_OBJECT`, and **an unreadable sheet leaves the config byte-identical** | U + I (temp files) | no |
| `test_secrets.py` | `syncworker/secrets.py` | 19 | Env-first path builds credentials and strips a trailing `/`; all four keys required; bad base64 raises naming `SF_PRIVATE_KEY_B64`; values never appear in `repr`; **`boto3` does not appear in the source** (the AWS-removal guard); a key file wins over base64; a certificate thumbprint is rejected by name (×3); missing file names the path; a certificate is named as such; non-PEM base64 rejected; a consumer secret alone is enough and is preferred over a key and is never shown in a `repr`; a blank secret falls through | U (temp PEMs) | no |
| `test_upsert.py` | `syncworker/storage.py` | 6 | First upsert creates the table; a second batch replaces changed rows **with a `GROUP BY Id HAVING count(*)>1` check proving no duplicates** (the idempotency proof); intra-batch duplicate Ids → last wins; empty batch is a no-op; a new column appearing later is added and the pre-existing row reads `None`; `normalize_records` makes every value `str` or `None` | I (real DuckDB) | no |
| `test_watermark.py` | `syncworker/storage.py` | 3 | Unknown object ⇒ `None`; set/get round-trip, update overwrites rather than duplicating, objects independent; the value survives close + reopen | I (real DuckDB) | no |
| `conftest.py` *(support)* | — | 0 | 5-line `sys.path` shim, same shape as the orchestrator's | — | no |
| `__init__.py` *(support)* | — | 0 | Empty package marker | — | no |

**File count reconciliation:** 52 + 16 + 10 = **78 test files**, plus five support files
(`orchestrator/conftest.py`, `orchestrator/tests/conftest.py`, `orchestrator/tests/__init__.py`,
`sync-worker/conftest.py`, `sync-worker/tests/__init__.py`) = **83**.

---

## 4. Gap analysis — the eight critical paths

Cross-referenced against [CRITICAL-PATHS.md](./CRITICAL-PATHS.md).

| # | Flow | End-to-end coverage? | Detail |
|---|---|---|---|
| 1 | **User message → SSE stream rendered** | **Halves covered, seam untested** | The server half is real: `POST /chat` is driven through `TestClient` and the raw SSE text is parsed and asserted — `test_chat_modes.py:221-311` checks the exact order `["reasoning","token","token","meta","done"]`, `test_context_budget.py:322-350` checks the trim notice reaches `meta`, `test_conversation_integrity.py:385-403` checks `generation_id`. The client half is real too (`sse.test.ts`, 22 `it()`). **No test ever feeds real orchestrator bytes into `readChatStream`.** Stop/abort is covered at the unit level (`test_live_generation.py`, 7 fns) but **the no-terminal-frame cancellation path is never asserted end to end**, and no test covers the `attachStream` double-register race. |
| 2 | **Router → engine selection → fallback** | **Partial — the fallback is untested** | Dispatch is covered (`test_router_parse.py` 12 fns, `test_llm_clients.py:80-97`, `test_chat_modes.py:29-48`). But [router.py:121-122](../../orchestrator/app/engines/router.py#L121-L122) and `:130-131` are bare `except Exception: pass`, and **no test asserts what route a raised exception or a garbage parse produces through `route_request`**. `parse_route → None` is tested only in isolation. The lenient-regex `<think>` defeat at `:77` is untested. |
| 3 | **Agent multi-step tool loop** | **Yes (offline)** | `test_agent.py:245-292` runs `run_agent_engine` with planner, sql step, llm step and synthesis all faked, asserting one `meta`, step order and merged payload. `web` and `salesforce` kinds covered separately (`test_agent_web_step.py:77-99`, `test_live_salesforce.py:191-221`). `rag` steps are only exercised with `select_context` monkeypatched. Not covered: the `gather` without `return_exceptions` on cancellation, and the unreachable duplicate branch at `agent.py:312-335`. |
| 4 | **NL → SQL → guard → DuckDB → caps → chart** | **Partial — the composition is untested** | Every stage is covered alone: prompt/dictionary (`test_sf_dictionary.py:88-94`), guard (`test_sql_guard.py`, 14 fns / 50 hostile inputs), known-table check (`test_live_salesforce.py:229-259`), execution lockdown (`test_sql_engine_meta.py:27-43`), meta shape (`:46-81`), charts (`test_chart_routes.py`). **No test drives a hostile model output through `run_sql_engine`** — every `fake_chat_completion` in the suite returns a benign `SELECT`. Nothing asserts that a model returning `DROP TABLE opportunities` produces an error-shaped SSE frame rather than executing, and **nothing covers `SEC-07`'s `E'…'` desync at all**. The `DATA-01` retry gap is likewise unasserted. |
| 5 | **RAG retrieval → rerank → budget → citation** | **None whatsoever** | `rg -l 'run_rag_engine' orchestrator/tests/` returns **nothing**; `rg -l 'engines.rag'` returns only `test_imports.py:15` (an import smoke test) and `test_agent.py:11` (which monkeypatches `select_context` away). LanceDB search, the Qwen3-Reranker load path, `RERANK_ENABLED=false` degradation, `RAG_TOP_K`/`RAG_FINAL_K`, the misleading `"does not exist"` regex catch and the citation contract that [README.md:52-53](../../README.md#L52-L53) calls a non-negotiable trust rule are **all unexercised**. |
| 6 | **Salesforce JWT → … → watermark commit** | **Outside both suites, and the linchpin is untested** | `orchestrator/tests/` contains nothing for it. `sync-worker/tests/` exists (10 files, 97 fns) but is unreachable from the documented commands without a bind mount. Within it, **`sync_object` — 89 LOC, the single most consequential function in the service — has zero direct coverage**, and so do `_request`, `bulk_query`, `soql_query`, `describe_fields`, `list_objects`, `build_full_soql`, `build_incremental_soql`, `write_parquet_batch` and the whole of `RagIndexer` (only `OpenAIEmbedder` is tested). Nothing exercises a real org anywhere. |
| 7 | **Upload → extract → report → PDF export** | **None end to end** | `POST /uploads` ([uploads.py:66](../../orchestrator/app/uploads.py#L66)) is referenced by **zero** tests — the streamed write, the `UPLOAD_MAX_MB` cap, the permissive ownership rule and the `DATASET_UPLOADS_ENABLED` 404 are untested at the HTTP layer. The libraries beneath it are well covered (`test_archive_safety.py` 18 fns, `test_dataset_profile.py` 14 fns). On the report side only `_sql_section` is tested; **`run_report_engine` and `_run_pandoc` have no test at all**, so the `.docx`/`.pdf` artefacts the README advertises are never produced in the suite. |
| 8 | **Context meter → compaction → summary** | **Yes, thoroughly** | `test_compaction.py` (25 fns, 548 LOC) covers budget maths, idempotent folding, the background-vs-synchronous race with a spy on `db.save_summary`, a 200-turn simulation, adaptive keep-recent and session isolation; `test_recall.py` (12) and `test_recall_db.py` (5) cover retrieval; `test_context_budget.py` (23) covers per-request sizing; `contextMeter.test.ts` (21) covers the browser maths. Untested: `POST /chat/compact` ([main.py:745](../../orchestrator/app/main.py#L745)) and `GET /history/conversations/{id}/summary` ([history.py:239](../../orchestrator/app/history.py#L239)). |

**Verdict — `TEST-02`.** 1,141 tests pass and the unit-level discipline is genuinely high (the suites
encode at least seven real, named production defects). But **six of eight critical paths have no
end-to-end assertion**, and two of them — RAG and upload→report→PDF — have essentially none at any level
above their helper libraries.

### Additional untested HTTP surface

Every route has at least one test except: `POST /chat/compact`
([main.py:745](../../orchestrator/app/main.py#L745)), `GET /history/conversations/{id}/summary`
([history.py:239](../../orchestrator/app/history.py#L239)), `POST /uploads`
([uploads.py:66](../../orchestrator/app/uploads.py#L66)), `GET /uploads/{conversation_id}`
([uploads.py:160](../../orchestrator/app/uploads.py#L160)).

### Tests that cannot fail

| id | File / lines | Why it cannot fail |
|---|---|---|
| F-7a | `orchestrator/tests/test_search_off.py:9-29` | The docstring claims `web_search='off'` makes zero outbound calls, "verified by exploding on any network use". It monkeypatches `get_provider` and `safe_fetch` to explode (`:12-16`), then **re-implements `main.py`'s gate locally** (`:21-25`) and asserts the locally computed boolean is `False` (`:26`). The real gate is never called, so the explosive stubs can never fire. The file ends with a literal `assert True` (`:29`). |
| F-7b | `orchestrator/tests/test_search_engine.py:111-116` | Named `test_search_off_does_no_network`; its only assertion is `assert settings.search_enabled in (True, False)`. |
| F-8 | `orchestrator/tests/test_conversation_integrity.py:553-566` | The comment says "a database missing the migrated column is reported, not hidden", but `:564` monkeypatches `_check_app_db` to *itself* and `:566` asserts `result["status"] == "ok" or "generation_id" in str(result)` — true whether the break is detected or silently reported healthy. |
| F-9 | `orchestrator/tests/test_chart_pipeline.py:197-199` | `assert run(...) is None or True` is a tautology; only the absence of an exception is really checked. |
| F-10 | `orchestrator/tests/test_history_v3.py:162-176` | Posts to `/auth/register` at `:165`, an endpoint that has returned 404 since login was removed (asserted at `test_auth.py:34-37`). The response is not checked, so the test proceeds as the default local user and still passes — the line is dead. |
| — | `sync-worker/tests/test_config.py:26-35` | Both assertions restate invariants that `load_object_configs` already raises on ([config.py:78-82](../../sync-worker/syncworker/config.py#L78-L82)). |

### Structural risks in the suites themselves

- **Six files assert on source strings, not behaviour**: `test_effort_depth.py:126-136`
  (`assert 'temperature = 0.3 if effort in ("medium", "high") else 0.6' in src`),
  `test_salesforce_toggle.py:154-159, 188-210`, `test_live_salesforce.py:309-317, 357-364`,
  `test_agent_web_step.py:184-190, 210-227`, `test_sf_dictionary.py:88-94`,
  `test_system_normalization.py:138-153`. A behaviour-preserving refactor breaks them; a
  literal-preserving behaviour change does not.
- `test_sf_dictionary.py:82,84` writes and reads the fixed path `/tmp/many.json` instead of `tmp_path` —
  it collides between concurrent runs and leaks outside the sandbox.
- `test_context_budget.py:168-176` performs a **real TCP connect** to `http://127.0.0.1:9/v1`; it depends
  on that port being closed on the host.
- `test_conversation_integrity.py:431-461` starts 8 OS threads against one SQLite file behind a
  `threading.Barrier` and asserts `errors == []`. SQLite lock contention plus the stdlib's 5-second
  default busy timeout ([db.py:199](../../orchestrator/app/db.py#L199)) makes this a flake candidate on a
  loaded machine.
- **No `pytest-asyncio`.** Every async test builds its own loop with `asyncio.run(...)`, which means a
  `ContextVar` set inside the coroutine is invisible afterwards — the suite documents and works around
  this at `test_context_budget.py:276-281`.
- **No component tests.** `frontend/vitest.config.mts:4-7` matches only `tests/**/*.test.ts` in a `node`
  environment, so `ChatApp.tsx` (916 LOC), `MermaidBlock.tsx` (429), `SearchPalette.tsx` (449),
  `Composer.tsx` (423) and the other 28 components are entirely unexercised.
- **Dependency skew is untested.** `orchestrator/requirements-dev.txt` omits `python-multipart`, which
  `app/uploads.py:66-71` needs **at decorator-evaluation time**; on a clean host installing exactly that
  file, **12 test modules fail at collection**. The defect is latent only because this machine happens to
  have the package installed. See `DX-01`.

---

## 5. The ten highest-value tests to add next

Ordered by risk retired per hour of work. Each entry names the file to create and the assertions it must
make.

### 1. `orchestrator/tests/test_sql_guard_e2e.py` — closes the flow-4 composition gap and pins `SEC-07`

The guard is tested in isolation over 50 hostile strings; the *engine* is never driven with a hostile
model output. Assert:

- With `_ask_sql` faked to return `DROP TABLE opportunities`, `run_sql_engine` emits **no `token` of
  answer text** and the exception surfaces as an `error`-shaped terminal frame — never an execution.
- With `_ask_sql` faked to return `SELECT E'\'' , 1; DROP TABLE opportunities`, **`guard_sql` returns
  without raising** (pinning the `SEC-07` desync as a known, deliberate state) **and** the subsequent
  `_execute` fails, and a follow-up `SELECT count(*) FROM information_schema.tables` proves the table
  still exists — i.e. the read-only connection, not the guard, is what saves it.
- With the first `_ask_sql` returning invalid SQL and the second returning
  `SELECT 0 AS record_count`, assert the retry result is executed **without** `references_a_known_table`
  being re-applied — pinning `DATA-01` — then flip the assertion when the fix lands.
- With the first `_ask_sql` raising `openai.APIConnectionError`, assert a second model call is made
  (documenting that a transport failure is misclassified as bad SQL, [sql.py:203](../../orchestrator/app/engines/sql.py#L203)).

### 2. `orchestrator/tests/test_rag_engine.py` — the only critical path with zero coverage

Fake `llm.embed_texts`, `lancedb.connect` and `llm.stream_chat_completion`. Assert:

- Happy path: `run_rag_engine` emits ≥1 `token` and **exactly one** `meta` with `route == "rag"` and a
  `citations` list.
- `RERANK_ENABLED=false` → `_rerank` is never called and the context is `hits[:RAG_FINAL_K]` in vector
  order.
- `_rerank` raising `RuntimeError("CUDA out of memory")` → the answer is still produced, the context is
  the vector-order cut, and **the degradation is observable** (assert a log record; today there is none —
  this is the assertion that forces the fix).
- An embedding-endpoint failure whose message contains `does not exist` must **not** be reported as
  "There's no Salesforce data on this machine yet" ([rag.py:130-136](../../orchestrator/app/engines/rag.py#L130-L136)).
- Citation filtering: when the answer mentions one of three record ids, `meta.citations` has length 1;
  when it mentions none, assert the intended behaviour of the `mentioned or citations` fallback at
  [rag.py:150](../../orchestrator/app/engines/rag.py#L150).
- `_context_block` output length is bounded — today it is not.

### 3. `orchestrator/tests/test_uploads_route.py` — an entire unauthenticated write endpoint with no tests

Drive `POST /uploads` and `GET /uploads/{id}` through `TestClient`. Assert:

- `DATASET_UPLOADS_ENABLED=false` → 404.
- A body one byte over `UPLOAD_MAX_MB` → **413**, and `os.listdir` of the upload root shows no partial
  file left behind.
- A multipart `filename` of `"/"` or `".."` → a **4xx**, not the current `IsADirectoryError` 500 from
  [uploads.py:92](../../orchestrator/app/uploads.py#L92) being outside the try block.
- A `conversation_id` owned by a different user → 404; a `conversation_id` that exists in no row → also
  rejected (pinning the permissive rule at `:76-78` as the thing to fix).
- A zip bomb → 400 with a persisted row whose `status == "rejected"`.
- `GET /uploads/{id}` rewrites `status` to `"expired"` once the workspace has been swept.

### 4. `orchestrator/tests/test_report_engine_e2e.py` — `run_report_engine` and `_run_pandoc` are untested

Fake the planner and section models; stub `asyncio.create_subprocess_exec`. Assert:

- A two-section plan produces both a `.docx` and a `.pdf` in `reports_dir` and a single `meta` whose
  `report_files` lists both with non-zero `size`.
- `_run_pandoc` receives `--pdf-engine=weasyprint` **only** for the `.pdf` target
  ([report.py:113](../../orchestrator/app/engines/report.py#L113)).
- **When the `.pdf` conversion fails, the `.docx` is still reported** — today the unguarded loop at
  `:257-258` lets `RuntimeError` escape and `report_files` is never emitted at all.
- A subprocess that never exits is abandoned within a bounded time — today
  `await proc.communicate()` at `:117` waits forever.
- Two reports with the same title generated in the same second produce **two distinct filenames**, not a
  silent overwrite (`:228-229`).

### 5. `orchestrator/tests/test_router_fallback.py` — closes the flow-2 gap

Assert, driving `route_request` (not `parse_route`):

- `router_chat_completion` raising → the main-model fallback at
  [router.py:126](../../orchestrator/app/engines/router.py#L126) **is** called.
- Both calls raising → the return value is exactly `"rag"`, and **a log record is emitted** (today both
  handlers are silent — this assertion forces the fix for `OBS-01` on this path).
- A reply of `<think>maybe report</think>{"route":"sql"}` classifies as `sql`.
- A reply of `<think>I think this is a report request</think>garbage` must **not** classify as `report` —
  today the lenient regex at `:77` scans the un-stripped text and would.
- `has_image=True` short-circuits to `"vision"` with **zero** model calls.

### 6. `frontend/tests/sse-contract.test.ts` + `orchestrator/tests/test_sse_golden.py` — welds the two halves of flow 1

The single highest-leverage pair, because the wire contract is currently asserted twice and agreed
nowhere.

- The Python test drives `POST /chat` through `TestClient` across all eight event types (including a
  `step` with and without `detail`, a `research` `query` phase, and a `meta` carrying `sql` + `data` +
  `chart` + `context`) and writes the **raw bytes** to a checked-in fixture, asserting the fixture is
  byte-stable.
- The Vitest test reads that same fixture, replays it through `readChatStream`
  ([sse.ts:283](../../frontend/lib/sse.ts#L283)), and asserts the decoded `ChatStreamEvent` sequence,
  the folded `Meta` shape, and that **no event is dropped by `toChatStreamEvent`'s `default: return
  null`** — which is exactly how a new server event would silently vanish today.
- Include the `route: "dataset"` case, which the frontend's `Engine` union at
  [types.ts:8](../../frontend/lib/types.ts#L8) does not contain.

### 7. `orchestrator/tests/test_chat_ownership.py` — pins `SEC-02`

Assert:

- With `db.conversation_owner` monkeypatched to raise `sqlite3.OperationalError`, `POST /chat` against a
  conversation owned by another user must **not** proceed to the engine — today
  [main.py:338-341](../../orchestrator/app/main.py#L338-L341) sets `conv_owner = None` and the request
  runs, reading that conversation's `url_documents` and `repo_chunks` into the prompt.
- The same failure mode for `POST /uploads` ([uploads.py:76-78](../../orchestrator/app/uploads.py#L76-L78))
  and `POST /chat/compact` ([main.py:758](../../orchestrator/app/main.py#L758)) — assert all three use the
  **same** rule, which today they do not (`is not None and !=` vs `is None or !=`).
- A conversation id that exists in no row is rejected, not silently accepted.

### 8. `sync-worker/tests/test_sync_object.py` — the 89-LOC linchpin has no direct coverage

With a fake `SalesforceClient`, a real temp-file `Store` and a fake `RagIndexer`, assert:

- `watermark is None` → `bulk_query(build_full_soql(...))` is called and `soql_query` is not.
- A watermark present → `soql_query(build_incremental_soql(..., watermark))` and `bulk_query` is not.
- `cycle_start` is captured **before** the first client call
  ([main.py:115](../../sync-worker/syncworker/main.py#L115)) and is the value committed at `:188`.
- Fields the describe call omits (FLS) never appear in the generated SOQL.
- **A raising `indexer.index_records` must not let the watermark advance** — today
  [main.py:174-188](../../sync-worker/syncworker/main.py#L174-L188) swallows it and commits anyway,
  producing a permanent index gap. This is the single most valuable assertion in the sync worker.
- A raising `store.upsert` mid-loop leaves the stored watermark unchanged, and a replay converges to the
  same DuckDB rows.

### 9. `sync-worker/tests/test_bulk_poll_bound.py` — pins the worker's worst hang

Against a fake HTTP transport, assert:

- A job that reports `InProgress` forever terminates within a bounded number of polls **or** a wall-clock
  deadline — today [sf_client.py:208-220](../../sync-worker/syncworker/sf_client.py#L208-L220) is
  `while True` with no cap, no deadline and no shutdown check.
- Setting `_StopFlag.stop` mid-poll aborts the loop.
- A `Failed`/`Aborted` job raises with the job id in the message.
- A results page containing a 131,072-character field does not abort the extract — today
  `csv.DictReader` at `:233` raises `_csv.Error: field larger than field limit`.

### 10. `orchestrator/tests/test_search_gate_real.py` — replaces the two vacuous tests

Delete `test_search_off.py` and `test_search_engine.py:111-116`; drive the **real** gate at
[main.py:423-449](../../orchestrator/app/main.py#L423-L449) through `TestClient` with
`get_provider` and `net.safe_fetch` monkeypatched to raise `AssertionError`. Assert:

- `mode="salesforce", web_search="off"` → the stubs are never called.
- `mode="salesforce", web_search="auto"` → the stubs are never called (the Salesforce-mode rule at
  `main.py:401`).
- `mode="salesforce", web_search="on"` → the provider **is** called.
- `mode="assistant", web_search="auto"` with `should_search` returning `True` → the provider is called.
- `SEARCH_ENABLED=false` → the stubs are never called regardless of `web_search`.
- The rate limiter refusing (`rate_ok → False`) emits the "Search rate limit reached" `status` frame and
  does **not** call the provider.

---

## 6. Two structural fixes worth more than any single test

1. **Add CI (`TEST-01`).** A single `.github/workflows/ci.yml` running the three suites plus
   `npx tsc --noEmit` and `npm run lint` on every push would have caught every documentation-vs-code
   drift listed in the evidence base. The suites already run offline in under 45 seconds combined.
2. **Add a lockfile and fix `requirements-dev.txt` (`DX-01`).** Every constraint in
   `orchestrator/requirements.txt` is an unbounded `>=` with no lockfile, so "1,141 tests pass" is a
   statement about one machine on one day. The sync worker caps majors and the frontend has
   `package-lock.json`; the orchestrator has neither.
