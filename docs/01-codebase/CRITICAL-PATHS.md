# Critical paths — end-to-end call traces

> **⚠ Superseded in part (2026-08-10).** The app-state layer described below was
> `/data/app.sqlite3` (stdlib `sqlite3`). It is now PostgreSQL — see
> [`data-model.md`](data-model.md) and the CHANGELOG entry
> "App state moved from SQLite to PostgreSQL". Every `sqlite3` reference,
> `db.py` line number and finding about SQLite locking below is a snapshot of
> the pre-migration code and has NOT been re-derived. The DuckDB warehouse and
> LanceDB sections are unaffected and remain accurate.

Eight flows, traced hop by hop from the user action to the rendered result. Every hop carries a
`file:LINE` reference. Where a hop has **no timeout**, **no retry**, **no bound**, or **swallows an
exception**, that is stated inline at the hop.

Conventions used throughout:

- **⏱ no timeout** — nothing in this hop bounds wall clock beyond an outer default.
- **↻ no retry** — a failure here is terminal for the request.
- **∞ no bound** — unbounded size, count, or growth.
- **🕳 swallowed** — the exception is caught and discarded or degraded, usually with no log line.
- **🚧 blocking-in-async** — a synchronous call executed directly on the single event loop.

Two facts apply to **every** flow and are not repeated at each hop:

| Fact | Evidence |
|---|---|
| One uvicorn worker, one event loop, no `--workers` | [Dockerfile:52](../../orchestrator/Dockerfile#L52) |
| Every LLM call is bounded only by `LLM_REQUEST_TIMEOUT` = 300 s, applied per attempt, with the OpenAI SDK's default `max_retries=2` → worst case 900 s per call | [config.py:264](../../orchestrator/app/config.py#L264), [llm.py:75-79](../../orchestrator/app/llm.py#L75-L79) |

---

## Flow 1 — User message → SSE token stream rendered on screen

Includes stop/abort and re-attach.

### 1a. Browser → Next.js route handler

1. `Composer.submit` — [Composer.tsx:137-146](../../frontend/components/Composer.tsx#L137-L146). No-ops while
   `streaming || disabled` (`:139`), then calls `onSend(trimmed, attachment, pastedTexts)` (`:141`).
2. `ChatApp.send` — [ChatApp.tsx:391-476](../../frontend/components/ChatApp.tsx#L391-L476). Creates the
   conversation if needed (`:394-404`), builds the user `ChatMessage` (`:408-420`), holds the raw
   attachment in memory (`:423-429`), `persist()` (`:431`), then `startStream(...)` (`:466-473`).
3. `streams.startStream` — [streams.ts:307-348](../../frontend/lib/streams.ts#L307-L348).
   `register(conversationId, turns)` at `:309` **unconditionally overwrites** any existing entry
   (`:292`) — the previous `LiveStream`'s `consume()` loop keeps running and its `controller` is never
   aborted. **Race 1.**
4. `POST /api/chat` with the body built at [streams.ts:314-332](../../frontend/lib/streams.ts#L314-L332):
   `{messages, session_id, conversation_id, mode, model, effort, agent, web_search, image?, pdf?,
   pdf_filename?}`. Every literal union matches the orchestrator's Pydantic `Literal`s at
   [main.py:191-199](../../orchestrator/app/main.py#L191-L199).
5. Route handler — [frontend/app/api/chat/route.ts:123-185](../../frontend/app/api/chat/route.ts#L123-L185).
   - `:126` `await req.json()` — **∞ no bound**, the entire request body is buffered in memory, then
     copied a second time by `JSON.stringify` at `:161`.
   - `:134-136` `MOCK_MODE === 'true'` → fabricated fixture stream (`DX-02`).
   - `:154-166` `fetch(ORCHESTRATOR_URL + '/chat')` with `signal: req.signal` — **⏱ no timeout, ↻ no retry**.
   - `:174-181` any upstream non-ok is flattened to a 502 carrying only the status **number**; the
     upstream body is discarded. An orchestrator 422 therefore reaches the user as
     "The orchestrator is unreachable" ([streams.ts:165-178](../../frontend/lib/streams.ts#L165-L178)) — false.
   - `:184` `new Response(upstream.body, {headers: SSE_HEADERS})` — bytes are piped, never re-parsed.

### 1b. Orchestrator request setup

6. `POST /chat` — [main.py:274-685](../../orchestrator/app/main.py#L274-L685), a **412-LOC** handler.
   - `:292` `text = request.text or "Analyze the attached image."`.
   - `:295-318` the `meta_extras(route)` closure; note `"effort": "medium"` is hard-coded for
     `sql`/`rag`/`report` at `:314`.
   - `:327` `current_user(http_request)` → [auth.py:89-92](../../orchestrator/app/auth.py#L89-L92), which
     `del request` and returns the single local row. **`require_user` never 401s**
     ([auth.py:95-97](../../orchestrator/app/auth.py#L95-L97)). → **`SEC-01`**
   - `:338-341` `db.conversation_owner(conv_key_outer)` inside `try/except Exception: conv_owner = None`.
     **🕳 swallowed — fails OPEN.** The comment at `:336-337` asserts the opposite. → **`SEC-02`**
     Also **🚧 blocking-in-async**: `db.connect()` re-runs the 16-statement schema script plus
     `migrate()` on every call ([db.py:195-205](../../orchestrator/app/db.py#L195-L205)). → **`PERF-03`**
   - `:343-344` 404 when `conv_owner is not None and conv_owner != viewer`.
   - `:348-350` `previous.task.cancel()` — the previous generation is cancelled but **not awaited**, so its
     `finally` block (`:673-677`, which writes `db.add_message`) can run *after* the new generation has
     already replaced the registry entry.
   - `:352-356` `LiveGeneration` constructed and stored in the process-global `_live_generations`
     (`:125`).
   - `:364-381` the `emit()` closure. On `event == "meta"` it merges `meta_extras`, `generation_id`,
     `context.get_trim_notice()` → `input_trimmed`, `context_state` → `context`, `orchestration_state` →
     `auto`.
   - `:679` `gen.task = asyncio.create_task(worker())` — the generation is **deliberately detached** and
     outlives the HTTP request (`:62-76`).
   - `:681-685` `StreamingResponse(gen.follow(), media_type="text/event-stream")` with
     `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

### 1c. The worker

7. `worker()` — [main.py:383-677](../../orchestrator/app/main.py#L383-L677), **295 LOC**.
   1. `:385` `context.reset_trim_notice()` (a `ContextVar`, so the task gets its own copy).
   2. `:387` `history = request.history_messages or memory.history(request.session_id)`.
   3. `:403-415` pre-flight `orchestrate.decide(text, history, effort)` →
      [orchestrate.py:127-156](../../orchestrator/app/engines/orchestrate.py#L127-L156). This runs on
      **every text turn at effort ≥ low**, *before* the first `status` event is emitted (`:412` precedes
      `:415`). **⏱ no timeout of its own; 🕳 bare `except Exception` at
      [orchestrate.py:142-144](../../orchestrator/app/engines/orchestrate.py#L142-L144) makes a dead
      endpoint and a 400 indistinguishable.**
   4. `:423-449` the web-search gate — a 6-term conjunction; the Salesforce-mode rule
      `auto_web_search_allowed = request.mode == "assistant"` is set at `:401`. Rate limit via
      `search.rate_ok(user_key)` (`:437`) where `user_key` degrades to the literal `"anon"`.
   5. `:450-455` `recall_block(user_id, text, conversation_id)` →
      [memory_recall.py:57-75](../../orchestrator/app/memory_recall.py#L57-L75) →
      `db.recall_conversations` ([db.py:981-1019](../../orchestrator/app/db.py#L981-L1019)), an
      **unindexed `content LIKE ?` scan over every message the user owns**. **🚧 blocking-in-async, ∞ no
      bound.** A `sqlite3` error here is *not* caught locally and kills the whole answer via `:670`.
   6. `:463-473` repo probe (`db.get_repo_keys`), `:479-510` URL probe (`db.get_url_documents`),
      `:518-529` dataset probe (`db.get_uploads`) — all three **🚧 blocking-in-async** and all three
      **🕳 swallowed** to `False`/`[]` with no log (`:472-473`, `:494-495`, `:528-529`). → **`REL-03`**
   7. `:535-549` context assembly — see **Flow 8**.
   8. `:551-643` engine dispatch, first match wins: pdf → image → repo → urls → `want_agent` →
      `want_search` → `dataset_ready` → `mode == "assistant"` → else the LangGraph router (**Flow 2**).
   9. `:645-647` `gen.answer = answer`; `memory.add_exchange`; `gen.publish("done", …)` — the **only**
      emitter of `done`.
   10. `:654-667` background compaction spawned, deliberately not awaited (**Flow 8**).
   11. `:668-669` `except asyncio.CancelledError: gen.cancelled = True`.
   12. `:670-672` `except Exception as exc:` → `publish("error", {"message": str(exc)})` — **raw
       exception text is sent verbatim to the browser, unsanitised and unlogged.**
   13. `:673-677` `finally:` `contextlib.suppress(Exception)` around
       `await asyncio.shield(_finalize_generation(...))` — a failed persist is **🕳 silent**.

   **⏱ Nothing bounds `worker()` as a whole.** There is no `asyncio.wait_for`, no deadline, no
   cancellation on client disconnect (that is by design — `:62-76`).

### 1d. Emission and framing

8. Engine calls `emit("token", {"text": …})` — e.g.
   [chat.py:99-101](../../orchestrator/app/engines/chat.py#L99-L101).
9. `emit` → `gen.publish` — [main.py:95-98](../../orchestrator/app/main.py#L95-L98). Appends to
   `self.events` (`:85`) and notifies the `asyncio.Condition`. **∞ no bound**: every token frame is
   retained for the generation's whole life so a re-attaching client can replay it.
10. `LiveGeneration.follow` — [main.py:105-120](../../orchestrator/app/main.py#L105-L120). Replays the
    buffer, then streams live; `yield sse_event(event, data)` at `:118`.
11. `sse_event` — [sse.py:49-54](../../orchestrator/app/sse.py#L49-L54). Validates the name against
    `ALL_EVENTS` ([sse.py:44](../../orchestrator/app/sse.py#L44) — exactly
    `token, meta, done, error, reasoning, step, status, research`) and raises `ValueError` otherwise.
    Because it is called from the **reader** (`follow()`) rather than the publisher, an invalid name
    kills the stream mid-flight after the frame was already buffered.
    **There is no keep-alive/heartbeat frame and no `id:`/`retry:` field** — a long silent generation
    depends entirely on no intermediate proxy having an idle timeout.

### 1e. Browser decode and render

12. `readChatStream` — [sse.ts:283-301](../../frontend/lib/sse.ts#L283-L301). `body.getReader()`,
    `TextDecoder`, `SSEParser`. **⏱ no timeout and no idle detection** — if the orchestrator holds the
    connection open silently, the generator awaits `reader.read()` forever. `finally` releases the lock
    but **never calls `reader.cancel()`** (`:298-300`).
13. `SSEParser.feed` — [sse.ts:34-64](../../frontend/lib/sse.ts#L34-L64); CRLF split across chunks is
    handled by `pendingCR` (`:36-41`, `:52-56`).
14. `toChatStreamEvent` — [sse.ts:126-222](../../frontend/lib/sse.ts#L126-L222), **97 LOC**. `step`
    (`:137-165`) and `research` (`:166-201`) are validated field by field; **`meta` at `:202-203` is a
    raw unchecked `JSON.parse` cast** — and `meta` is the only event carrying `sql`, `data`, `chart` and
    `report_files`. Unknown event names → `default: return null` (`:216-217`).
15. `streams.consume` — [streams.ts:180-269](../../frontend/lib/streams.ts#L180-L269), **90 LOC**:
    `token` `:183-192` (first token settles the reasoning clock and clears `searchStatus`), `status`
    `:193-194`, `reasoning` `:195-200`, `research` `:201-236`, `step` `:237-238`, `meta` `:239-250`
    (`foldStreamState`), `error` `:251-255`, `done` `:256-261`. `:264-268` — **a body that ends with no
    terminal frame is finalized as `'done'` anyway**.
16. `notify(conversationId)` after every event (`:262`) → `ChatApp` stream-mirror effect
    [ChatApp.tsx:254-273](../../frontend/components/ChatApp.tsx#L254-L273) → `setMessages([...s.messages])`
    (`:269-271`). This fires **once per SSE token** and `MessageRow` is **not** `React.memo`'d
    ([MessageRow.tsx:21-232](../../frontend/components/MessageRow.tsx#L21-L232)), so every row and its
    open `ProofDrawer` re-render per token.
17. `MessageRow` assistant branch [MessageRow.tsx:82-231](../../frontend/components/MessageRow.tsx#L82-L231):
    `ReasoningAccordion` `:120-126` → `ResearchPanel` `:128` → `AgentTimeline` `:130` → `Markdown`
    `:132-137` → `ProofDrawer` `:205`.
18. `finalize` — [streams.ts:157-163](../../frontend/lib/streams.ts#L157-L163) → `saveMessages` (`:161`)
    → localStorage plus a background push to `/history`
    ([history.ts:644-647](../../frontend/lib/history.ts#L644-L647)).

### 1f. Stop / abort

19. Stop button — [Composer.tsx:377-401](../../frontend/components/Composer.tsx#L377-L401) → `onStop`.
20. `streams.stopStream` — [streams.ts:82-92](../../frontend/lib/streams.ts#L82-L92). Sends both
    `conversation_id` and `session_id` (`:90`) to match the orchestrator's `conversation_id or
    session_id` key resolution. `.catch(() => undefined)` at `:91` — **🕳 a failed stop is invisible.**
21. `POST /api/chat/stop` → orchestrator
    [main.py:712-722](../../orchestrator/app/main.py#L712-L722). Returns `{stopped: bool}`, **always 200**.
    Ownership is checked by `_owns` ([main.py:701-709](../../orchestrator/app/main.py#L701-L709)) which
    compares the single local identity to itself — a tautology. → **`SEC-01`**
22. The cancelled task hits `except asyncio.CancelledError` (`:668-669`) → `finish()` → `follow()`
    breaks at `:115`. **The stream closes with no terminal frame at all** — a third, undocumented
    terminal state next to `done` and `error` ([sse.py:20-24](../../orchestrator/app/sse.py#L20-L24)),
    which the client maps to `'done'` at [streams.ts:264-268](../../frontend/lib/streams.ts#L264-L268).

### 1g. Re-attach after reload or tab switch

23. Mount effect [ChatApp.tsx:167-238](../../frontend/components/ChatApp.tsx#L167-L238) →
    `fetchServerActive()` (`:210`) → `attachStream(wanted)` (`:215`); an 8 s poll at
    [ChatApp.tsx:278-306](../../frontend/components/ChatApp.tsx#L278-L306) (`window.setInterval(…, 8000)`
    at `:301`) repeats it and force-reloads the open chat when its detached generation finished.
24. `attachStream` — [streams.ts:355-393](../../frontend/lib/streams.ts#L355-L393). The
    already-streaming guard at `:356` is checked **before** the `await` at `:364`, so two concurrent
    calls both pass, both `register`, and both open a reader on the same generation. **Race 2.**
25. `GET /api/chat/attach/{id}` —
    `frontend/app/api/chat/attach/[id]/route.ts:20-66`.
    The only dynamic route in the tree that guards `decodeURIComponent` (`:27-32`); `SAFE_ID`
    `^[\w-]{1,64}$` (`:18`) matches the orchestrator's `_CONVERSATION_ID_RE`
    ([history.py:33](../../orchestrator/app/history.py#L33)). Its 502 branch reuses the 404 message
    (`:61-64`), so an orchestrator 500 reads as "no active generation".
26. Orchestrator `GET /chat/attach/{id}` — [main.py:782-796](../../orchestrator/app/main.py#L782-L796) →
    `gen.follow()`, which replays the whole buffered event list. `LiveGeneration.follow` increments
    `subscribers` on first iteration and decrements in `finally` (`:105-120`) — an abandoned async
    generator whose `aclose()` is deferred keeps `subscribers > 0`, which **suppresses the server-side
    persist at `:157`**.

### What breaks first

Wall clock. Nothing bounds `worker()`, and the pre-flight `orchestrate.decide` sits ahead of the first
`status` frame with a 300 s × 3 ceiling — so a wedged router endpoint produces a chat window that shows
nothing at all for up to 15 minutes, then a raw exception string. Second: memory. `LiveGeneration.events`
plus an unbounded `ChatRequest.messages`/`image`/`pdf` (`REL-01`) means one client can pin arbitrary RAM
in the single orchestrator process.

**Findings on this path:** `SEC-01`, `SEC-02`, `REL-01`, `REL-03`, `PERF-03`, `PERF-04`, `OBS-01`,
`DX-02`, `QUAL-01`, `TEST-02`.

---

## Flow 2 — Router classification → engine selection → fallback

Reached **only** when the request has no PDF, no image, no repo reference, no URLs, is not an agent turn,
is not a search turn, has no ready dataset, and `mode == "salesforce"` — i.e. it is the last `else` of the
dispatch chain at [main.py:551-643](../../orchestrator/app/main.py#L551-L643).

1. `get_graph().ainvoke({...})` — [main.py:633-643](../../orchestrator/app/main.py#L633-L643).
2. `get_graph` — [graph.py:113-117](../../orchestrator/app/graph.py#L113-L117). Lazily compiles and caches
   in the module global `_compiled` (`:110`); non-atomic check-then-set, benign on one loop.
3. `build_graph` — [graph.py:90-107](../../orchestrator/app/graph.py#L90-L107). Six nodes, entry `router`
   (`:99`), conditional edge keyed on `state["route"]` mapping exactly `{sql, rag, vision, report, chat}`
   (`:100-104`). **There is no default branch** — a route outside `ROUTES` would be a LangGraph
   `KeyError`.
4. `_router_node` — [graph.py:29-41](../../orchestrator/app/graph.py#L29-L41) →
   `router.route_request(message, bool(image_base64), history)` (`:36-40`).
5. `route_request` — [router.py:92-133](../../orchestrator/app/engines/router.py#L92-L133):
   - `:103-104` `has_image` short-circuits to `"vision"` with **no model call**.
   - `:106-110` a new message of **≤ 12 words** gets the previous user turn (truncated to **400 chars**)
     prepended as `"(earlier question: …)\nFollow-up: …"`.
   - `:115-117` primary call `llm.router_chat_completion(..., temperature=0.0, max_tokens=200)` →
     [llm.py:283-299](../../orchestrator/app/llm.py#L283-L299), which clips every message to
     `ROUTER_INPUT_CHAR_CAP` (`:286`) and forces `enable_thinking: false` (`:298`). **⏱ only the 300 s
     client timeout; ↻ no retry.**
   - `:118-120` `parse_route(raw)` → return on success.
   - `:121-122` **🕳 bare `except Exception: pass` — nothing logged.** Connection-refused, a 400 window
     error and a read timeout are indistinguishable.
   - `:126` fallback `llm.chat_completion(..., temperature=0.0, max_tokens=50)` on the **main** model.
     `chat_completion` forces `enable_thinking: True` ([llm.py:112](../../orchestrator/app/llm.py#L112)),
     so a thinking preamble is expected and **50 tokens may be consumed entirely by reasoning**.
   - `:130-131` **🕳 second bare `except Exception: pass`.**
   - `:133` unconditional `return "rag"`.
6. `parse_route` — [router.py:52-80](../../orchestrator/app/engines/router.py#L52-L80). Strips
   `<think>…</think>` into `t` (`:61`), unwraps the first fence (`:62-64`), strict JSON slice on `t`
   (`:66-73`). The **lenient regex at `:77` searches the original un-stripped `text`**, defeating the
   `<think>` strip on exactly the path where it matters.
7. Engine node → `run_sql_engine` ([sql.py:285](../../orchestrator/app/engines/sql.py#L285)) /
   `run_rag_engine` ([rag.py:127](../../orchestrator/app/engines/rag.py#L127)) /
   `run_vision_engine` ([vision.py:66](../../orchestrator/app/engines/vision.py#L66)) /
   `run_report_engine` ([report.py:214](../../orchestrator/app/engines/report.py#L214)) /
   `run_chat_engine` ([chat.py:66](../../orchestrator/app/engines/chat.py#L66)).

### "Low confidence" does not exist

There is **no threshold, probability, or logprob anywhere in `router.py`**. `parse_route` returns a route
or `None`; the only degradation signal is a parse failure or a raised exception, and both are handled
identically by falling through to the hard-coded `"rag"` default at `:133`. A total classifier outage
therefore silently sends greetings into the vector-search engine.

The nearest thing to a confidence gate is the separate pre-flight classifier
[orchestrate.decide](../../orchestrator/app/engines/orchestrate.py#L127-L156), whose ceiling table
`ALLOWED` ([orchestrate.py:114-119](../../orchestrator/app/engines/orchestrate.py#L114-L119)) can only
*narrow* what the effort level already permits, and whose `parse_plan`
([orchestrate.py:98-110](../../orchestrator/app/engines/orchestrate.py#L98-L110)) accepts a field only on
literal `True` and uses a **greedy** `\{.*\}` regex (`:38`) with no `<think>`/fence stripping.

### What breaks first

A wedged router endpoint. Worst case is 2 × 300 s (× 3 SDK attempts each) before the `"rag"` default at
`:133`, with nothing logged in between, and with `orchestrate.decide` having already spent the same
budget upstream. The user sees an empty chat window and then a RAG answer to a greeting.

**Findings on this path:** `REL-03`, `PERF-04`, `OBS-01`, `QUAL-01`, `TEST-02`.

---

## Flow 3 — Agent multi-step tool loop

1. `want_agent = request.agent or bool(auto_plan and auto_plan.agent)` —
   [main.py:417](../../orchestrator/app/main.py#L417).
2. `run_agent_engine(text, history, emit, effort=…, salesforce=(request.mode != "assistant"),
   web=want_search)` — [main.py:592-601](../../orchestrator/app/main.py#L592-L601).
3. `run_agent_engine` — [agent.py:631-658](../../orchestrator/app/engines/agent.py#L631-L658). Builds the
   initial `AgentState` and calls `get_agent_graph().ainvoke(...)` (`:648-657`).
4. Graph edges: `plan → execute → synthesize → END` —
   [agent.py:614-617](../../orchestrator/app/engines/agent.py#L614-L617). **There is no cycle.** Exactly
   one PLAN → EXECUTE → SYNTHESIZE pass; no step can enqueue another.

### 3a. PLAN

5. `_plan_node` — [agent.py:561-568](../../orchestrator/app/engines/agent.py#L561-L568) → `make_plan(...)`
   then `coerce_allowed(plan, web=state.get("web", True))`.
6. `make_plan` — [agent.py:178-215](../../orchestrator/app/engines/agent.py#L178-L215):
   - `:188` picks `_PLAN_SYSTEM` (`:108-127`) or `_PLAN_SYSTEM_NO_SF` (`:131-142`) on the `salesforce`
     flag — **this is the Salesforce gate at prompt level.**
   - `:190` rewrites the literal `"at most 8 "` to `"at most {step_budget(effort)} "` by **string
     substitution**. Both prompt constants must keep that exact phrasing (`:110`, `:138`) or the budget
     silently stops applying.
   - `:197` `for _attempt in range(2)`; `:200-209` the retry appends the previous error (`[:400]`) as an
     extra user turn; `:210` `llm.chat_completion(temperature=0.1, max_tokens=6000)`.
   - `:211` `parse_agent_plan(raw)` → pydantic `AgentPlan` with `min_length=1, max_length=MAX_STEPS`
     (`:66-75`).
   - `:213-214` **🕳 bare `except Exception` — records `last_error`, logs nothing.**
   - `:215` after two failures, `_fallback_plan(message)` (`:145-148`): a single `kind="llm"` step.
7. **Salesforce gate (hard):** `_coerce_no_salesforce`
   [agent.py:151-162](../../orchestrator/app/engines/agent.py#L151-L162) rewrites every `sql`/`rag`/
   `salesforce` step's `kind` to `"llm"` **in place** (`:159-160`), so the sql/rag/live engines are never
   reached in assistant mode.
8. **Web gate:** `coerce_allowed(plan, web=…)`
   [agent.py:164-176](../../orchestrator/app/engines/agent.py#L164-L176) downgrades `web` → `llm`
   **at plan time only**. `_execute_node` (`:571-580`) does **not** forward `web` to `execute_steps`, so
   the gate exists in exactly one place.

### 3b. EXECUTE

9. `execute_steps` — [agent.py:369-405](../../orchestrator/app/engines/agent.py#L369-L405). A per-call
   `asyncio.Semaphore(3)` (`STEP_CONCURRENCY = 3`, `:37`, created at `:378`) and one `asyncio.gather`
   over all steps (`:405`). **`return_exceptions=True` is not set** — a `BaseException` escaping `run`
   (e.g. `CancelledError` on Stop) aborts the gather and leaves siblings running.
10. `run(step)` — [agent.py:380-403](../../orchestrator/app/engines/agent.py#L380-L403). Emits
    `step` `status="running"` (`:382`), awaits `_run_step_impl` (`:386`), emits `status="done"` with a
    `detail` (`:399-402`).
    `:387-398` **🕳 catches every `Exception` per step** and returns `output = "Step failed: …"`; the raw
    `str(exc)`, truncated to 200 chars, is **shipped to the browser as `step.detail`**. The plan always
    "succeeds" as a whole.
11. `_run_step_impl` — [agent.py:233-366](../../orchestrator/app/engines/agent.py#L233-L366), **134 LOC**,
    the largest function in the engine layer. Dispatch in source order:

| `kind` | lines | behaviour | hazard at this hop |
|---|---|---|---|
| `sql` | `:252-274` | `generate_and_run_sql(step.input, history=…)` `:257` → `cap_rows(rows, sql_preview_row_cap)` `:261` → `attach_chart` `:269` | returns `rows[:30]` (`:258-260`) to the synthesizer **labelled as the result**, without the "you are shown only the first few rows" instruction the direct route carries ([sql.py:267-278](../../orchestrator/app/engines/sql.py#L267-L278)) |
| `rag` | `:276-286` | `select_context` `:281` → `chat_completion(max_tokens=5000)` `:282-284` → `build_citations` `:285` | inherits every hazard of **Flow 5** |
| `salesforce` | `:288-310` | `fetch_live(step.input, history)` `:293` | `SalesforceUnavailable`/`UnsafeSoql` returns a prose degradation string and an empty sub-meta (`:294-303`) while the step still reports `status="done"` |
| `salesforce` (2nd copy) | `:312-335` | byte-equivalent duplicate guarded by the identical condition | **unreachable dead code, 24 LOC** |
| `web` | `:337-352` | `research_step(step.input, list(history), effort, emit)` `:341-343` | with **no** sources the branch **falls through** (`:351-352`, no `return`) into the llm tail — a model-knowledge answer reported `done` with **no `search_unavailable` flag**, unlike the direct route ([search.py:416](../../orchestrator/app/engines/search.py#L416)) |
| llm tail | `:354-366` | `chat_completion([_STEP_LLM_SYSTEM, *recent_turns(history, 8), step.input], temperature=0.3, max_tokens=5000)` | — |

### 3c. SYNTHESIZE

12. `_synthesize_node` — [agent.py:583-606](../../orchestrator/app/engines/agent.py#L583-L606).
13. `renumber_web_sources(results)` (`:588`) —
    [agent.py:411-452](../../orchestrator/app/engines/agent.py#L411-L452). Mutates step outputs and
    sub-metas **in place** so per-step `[n]` markers become plan-wide numbers. Sources without a `url`
    are skipped (`:435-437`), leaving their local markers unmapped and pointing at whichever page ends up
    with that number.
14. `llm.stream_chat_events(..., max_tokens=_SYNTH_TOKENS.get(effort, 6000))` (`:591-597`) → `reasoning`
    (`:599`) / `token` (`:602`).
15. `merge_step_meta(results)` (`:605` → `:467-518`) produces the **single** `meta`: always `route:
    "agent"` + `steps` (`:473-479`); the **last** step whose sub-meta has a truthy `sql` contributes the
    whole `_SQL_PAYLOAD_KEYS` tuple atomically (`:489-493`); citations dedupe on `record_id` (`:494-498`),
    report files on `filename` (`:499-503`), sources on `url` (`:506-510`).

### 3d. Max-iteration guard — what actually bounds this loop

| Bound | Value | Enforced at |
|---|---|---|
| Steps per plan | 8 | `MAX_STEPS` [agent.py:36](../../orchestrator/app/engines/agent.py#L36); pydantic `max_length=MAX_STEPS` `:67`; **and** the prompt text `:190` |
| Steps per effort level | medium 5 / high 8 | `_STEP_BUDGET` `:42`; no `fast`/`low` key, so an explicit `agent=true` at `effort="fast"` falls back to the medium value (`:49`) |
| Concurrency | 3 | `STEP_CONCURRENCY` `:37`, semaphore `:378` |
| Planner attempts | 2 | `:197` |
| Iterations of the whole loop | **1** | the graph has no cycle, `:614-617` |
| Wall clock | **none** | `rg 'wait_for\|timeout'` over `orchestrator/app/engines/*.py` matches only `url.py:36` and `search.py:315/338` |

### Client render

16. `step` frames → `mergeStep` [sse.ts:229-238](../../frontend/lib/sse.ts#L229-L238) →
    `AgentTimeline` ([MessageRow.tsx:130](../../frontend/components/MessageRow.tsx#L130)). Concurrent web
    steps share one `emit`, so their `research` events interleave into one panel — the frontend merges by
    phase/count with no step id ([sse.ts:166-201](../../frontend/lib/sse.ts#L166-L201)).

### What breaks first

Wall clock, again, and worse than Flow 2: plan (2 × 300 s) + ⌈8/3⌉ = 3 execution waves × (per-step model
call + arbitrary web fetch time) + a 12 000-token synthesis. Well past 20 minutes with no cancellation and
no `status` frame between waves. A single agent turn costs roughly **11–19 model calls**.

**Findings on this path:** `SEC-05` (web/repo/document text reaches the prompt untainted), `REL-03`,
`PERF-04`, `OBS-01`, `QUAL-01`, `TEST-02`.

---

## Flow 4 — NL → SQL → `sql_guard` → DuckDB → row caps → chart/table → citation

1. `_sql_node` — [graph.py:44-48](../../orchestrator/app/graph.py#L44-L48) →
   `run_sql_engine(message, history, emit)`.
2. `run_sql_engine` — [sql.py:285-453](../../orchestrator/app/engines/sql.py#L285-L453), **169 LOC**, six
   terminal branches.
   - `:286` `os.path.exists(settings.duckdb_path)` — **🚧 blocking syscall**, and a **TOCTOU window**
     against the sync worker replacing the warehouse file. Absent → `NO_DATA_MESSAGE` + `meta` and return.
   - `:291-292` `EXPORT_RE` decides `fetch_cap`: `export_row_cap + 1` (100 001) or
     `sql_preview_row_cap + 1` (501).
   - `:295-297` `wants_live_lookup(message)` (regex `:146-151`) raises `NoSuchTable` to short-circuit the
     warehouse entirely.
3. `generate_and_run_sql` — [sql.py:179-207](../../orchestrator/app/engines/sql.py#L179-L207):
   1. `:189` `format_schema(schema_cache.get(settings.duckdb_path))` — opens DuckDB and queries
      `information_schema.columns` ([schema_cache.py:40-57](../../orchestrator/app/core/schema_cache.py#L40-L57)).
      **🚧 blocking-in-async.** The `_sync_meta` bookkeeping table is in the `main` schema, so it is
      rendered into the prompt as if it were a business table.
   2. `:191` `schema_cache.get(...)` called a **second time** for the dict form — a duplicate DuckDB open.
   3. `:192` `_ask_sql` ([sql.py:87-114](../../orchestrator/app/engines/sql.py#L87-L114)): prepends the
      org dictionary hint (`:97-102`), assembles `[_SQL_SYSTEM] + recent_turns(history, 6) + [user]`
      (`:108-112`), `llm.chat_completion(temperature=0.1, max_tokens=6000)` (`:113`), then `extract_sql`
      (`:76-84`) strips `<think>`, unwraps fences and rewrites MySQL backticks to DuckDB double quotes.
   4. `:193-198` `references_a_known_table(raw, schema)`
      ([sql.py:173-176](../../orchestrator/app/engines/sql.py#L173-L176)) — no `FROM <known table>` →
      `NoSuchTable`, which is what stops the model answering from an invented
      `SELECT 0 AS record_count`.
   5. `:200` **`guard_sql(raw)`** → [sql_guard.py:129-157](../../orchestrator/app/core/sql_guard.py#L129-L157):
      `_scan` (`:57-126`, 70 LOC) produces `(cleaned, bare)`; comments are removed with **no separator**
      into `bare` so `UPD/**/ATE` reassembles and is caught (`:75-85`); string-literal and
      quoted-identifier bodies are stripped from `bare` (`:87-122`); then `bare` is checked for `;`
      (`:142`), `^(select|with)` (`:144`), a 27-keyword write/DDL blocklist (`:146`) and a 22-entry
      filesystem/network table-function blocklist (`:149`). **The string that is executed is `cleaned`
      (`:156`), while every check ran on `bare`.** That divergence is the bug:
      `SELECT E'\'' , 1; DROP TABLE t` passes the guard *including its multi-statement check*
      (empirically confirmed). → **`SEC-07`**
   6. `:201` `_execute(sql, cap)` → [sql.py:117-140](../../orchestrator/app/engines/sql.py#L117-L140).
      `duckdb.connect(read_only=True, config={enable_external_access: False,
      autoinstall_known_extensions: False, autoload_known_extensions: False})` (`:124-132`) — **this
      lockdown, not the guard, is what makes `SEC-07` non-exploitable today.** `cur.fetchmany(fetch_cap)`
      (`:136`). **🚧 blocking-in-async** — a fresh connect/close per query, no pool, no statement
      timeout, no `memory_limit`. → **`PERF-01`**
   7. `:203-207` retry: **🕳 a blanket `except Exception` treats *every* failure as bad SQL** — including a
      DuckDB file lock, a dead vLLM endpoint and a context-window 400 — and spends a second model call
      re-prompting with `previous_sql` and `error`. `guard_sql(raw2)` (`:205`) and `_execute(sql2, cap)`
      (`:206`) run **without re-checking `references_a_known_table`** → **`DATA-01`**. A second failure
      propagates out of the engine to `main.py:670`.

### 4a. Live-Salesforce fallback (the `NoSuchTable` branch)

4. [sql.py:301-395](../../orchestrator/app/engines/sql.py#L301-L395): `:304-306` refuse when
   `not (settings.sf_live_enabled and sf_live.configured())`; `:317` emit `status`
   "Not in the local copy — asking Salesforce…"; `:324-348` schema-shape questions via `fetch_schema`
   with `schema_text[:60000]`; `:350-360` `fetch_live`; `:362-395` a live-labelled streamed answer with
   `data = live_rows[:500]` and `truncated: False` (`:376-380`).
   `write_soql` ([live_sf.py:61-86](../../orchestrator/app/engines/live_sf.py#L61-L86)) accepts a
   `history` parameter it **never uses** (`:72-75` builds system + question only), then
   `guard_soql` + `run_soql` ([salesforce.py:55-90](../../orchestrator/app/core/salesforce.py#L55-L90),
   `:144-174`) with `MAX_ROWS = 200` and one 401 re-auth retry.

### 4b. Row caps — where each is actually enforced

| Cap | Value | Defined | Enforced |
|---|---|---|---|
| Preview / `meta.data` | 500 | [exports.py:15](../../orchestrator/app/core/exports.py#L15), [config.py:234](../../orchestrator/app/config.py#L234) | `cap_rows` at [sql.py:397](../../orchestrator/app/engines/sql.py#L397) |
| DuckDB fetch | 501, or 100 001 when an export is wanted | [sql.py:190](../../orchestrator/app/engines/sql.py#L190), `:292` | `cur.fetchmany` [sql.py:136](../../orchestrator/app/engines/sql.py#L136) |
| Export file | 100 000 | [exports.py:16](../../orchestrator/app/core/exports.py#L16) | `apply_export_cap` [exports.py:41-43](../../orchestrator/app/core/exports.py#L41-L43) |
| Narrative sample | 30 rows | [sql.py:253](../../orchestrator/app/engines/sql.py#L253) | `_narrative_messages` |
| Live SOQL describe | 30 rows | [live_sf.py:148](../../orchestrator/app/engines/live_sf.py#L148) | `describe_rows` |

**No `LIMIT` is ever injected into the generated SQL.** `fetch_cap` bounds only how many rows Python pulls
out of an already-executed result — a 5-way cartesian product still executes in full.

### 4c. Preview, export, chart, narrative

5. `:397` `cap_rows(rows, settings.sql_preview_row_cap)` → `(preview, truncated)`.
6. `:399-404` `meta = {route: "sql", sql, data: [dict(zip(columns, row)) …], truncated}`.
7. `:406-420` optional export: `export_csv` or `export_xlsx` into `settings.reports_dir` (`:409`) —
   **🚧 up to 100 000 rows written synchronously on the event loop**; the returned `_export_truncated`
   flag is **discarded** (`:408`); `meta["report_files"]` set at `:414-420`. **∞ `reports_dir` has no
   quota, TTL or cleanup anywhere in `orchestrator/app`.**
8. `:426` `attach_chart(meta, message, columns, preview)`
   ([sql.py:214-243](../../orchestrator/app/engines/sql.py#L214-L243)) →
   `chart_pipeline.build_chart(..., mode=settings.chart_trigger_mode, ask_model=_ask_chart_model)`
   (`:230-237`):
   - `build_chart` [chart_pipeline.py:94-117](../../orchestrator/app/core/chart_pipeline.py#L94-L117) —
     **🕳 blanket `except Exception` (`:115-117`) → `None` plus one WARNING.** Documented as "never
     raises" ([sql.py:221](../../orchestrator/app/engines/sql.py#L221)).
   - `_build_chart` `:120-186`: `profile_columns` `:133` → `decide` `:134-137` → **histogram branch**
     `:142-158` (bins computed in Python, `derived=True`) / **model branch** `:161-173`
     (`ask_model` → `llm.chat_completion(temperature=0.0, max_tokens=2500)`
     [sql.py:210-211](../../orchestrator/app/engines/sql.py#L210-L211) — **⏱ no timeout at this layer, and
     it is awaited *before* the narrative stream starts**) / **deterministic branch** `:176-186`.
   - `chart_prompt` [chart_pipeline.py:49-76](../../orchestrator/app/core/chart_pipeline.py#L49-L76)
     serialises only `p.to_prompt_dict()` — column metadata, **never a cell value**. This is a genuine
     prompt-injection boundary.
   - `parse_chart_spec` [chart_spec.py:189-220](../../orchestrator/app/core/chart_spec.py#L189-L220) —
     the **column-membership check at `:216-219` is the trust boundary for model output**; failures
     return `None` **🕳 silently, with no log**.
   - `meta["chart"] = result.spec.wire_dump()` (`sql.py:240`); `meta["chart_data"]` only when
     `result.derived` (`:241-242`).
9. `:428-436` `llm.stream_chat_completion(_narrative_messages(...), temperature=0.2, max_tokens=6000,
   thinking=False)` — each delta emitted as `token`. `_narrative_messages`
   ([sql.py:246-283](../../orchestrator/app/engines/sql.py#L246-L283)) shows only `rows[:30]` (`:253`)
   and carries the explicit "you are shown only the FIRST FEW ROWS … quote the true row count"
   instruction (`:267-278`) — added, per its own comment, because "314 rows came back and the summary
   said 29 records".
10. `:438-449` empty-answer fallback sentence; `:452` the single final `meta`.

### 4d. Render

11. `meta` parsed with **zero validation** ([sse.ts:202-203](../../frontend/lib/sse.ts#L202-L203)) →
    `foldStreamState` (`:246-278`) → `MessageRow` `:205` → `ProofDrawer`
    [ProofDrawer.tsx:36-160](../../frontend/components/ProofDrawer.tsx#L36-L160).
12. `chartRows = meta.chart_data?.length ? meta.chart_data : meta.data`
    ([ProofDrawer.tsx:61](../../frontend/components/ProofDrawer.tsx#L61)) — the back-compat fallback that
    makes the derived-rows contract work.
13. `SqlBlock` `:133` · `DataTable` `:144-148` (up to 500 rows) · `ChartView` `:151` →
    `validateChart(spec, data)` **outside `useMemo`**
    ([ChartView.tsx:65](../../frontend/components/ChartView.tsx#L65)) followed by memoized
    `buildChartOption` (`:66-69`) → `EChart` (`:78`), wrapped in the app's **only** React error boundary
    ([ChartErrorBoundary.tsx:28](../../frontend/components/ChartErrorBoundary.tsx#L28)).

### 4e. Citation

The SQL route emits **no `citations`**. Its proof surface is `meta.sql` + `meta.data` + `meta.chart`.
Record-level Lightning URLs (`build_citations`,
[citations.py:30-47](../../orchestrator/app/core/citations.py#L30-L47)) are produced only by the RAG
route, the report engine and the agent's `rag` step.

### What breaks first

`_execute` blocking the event loop. Because uvicorn runs a single worker, one expensive DuckDB scan —
which no `LIMIT`, statement timeout or `memory_limit` bounds — freezes **every** concurrent SSE stream in
the process. Second: the retry path, which turns a transient vLLM outage into a second full model call and
then an unguarded hallucination check.

**Findings on this path:** `SEC-07`, `PERF-01`, `DATA-01`, `DATA-02`, `REL-03`, `QUAL-02`, `TEST-02`.

---

## Flow 5 — RAG retrieval → rerank → context budget → injection → citation

> This flow has **zero test coverage**. `rg -l 'run_rag_engine' orchestrator/tests/` returns nothing;
> `rg -l 'engines.rag'` returns only an import smoke test and one file that monkeypatches
> `select_context` away.

1. `_rag_node` — [graph.py:51-55](../../orchestrator/app/graph.py#L51-L55) →
   `run_rag_engine(message, history, emit)`.
2. `run_rag_engine` — [rag.py:127-151](../../orchestrator/app/engines/rag.py#L127-L151).
3. `select_context(message)` — [rag.py:91-102](../../orchestrator/app/engines/rag.py#L91-L102):
   1. `:93` `retrieve(query, settings.rag_top_k)` →
      [rag.py:36-49](../../orchestrator/app/engines/rag.py#L36-L49):
      - `:38` `llm.embed_texts([query])` → `POST {EMBED_BASE_URL}/embeddings`
        ([llm.py:342-348](../../orchestrator/app/llm.py#L342-L348)), input clipped to
        `EMBED_INPUT_CHAR_CAP` (`:347`). **⏱ 300 s; ↻ no retry.**
      - `:39-40` an empty vector returns `[]`.
      - `:41` lazy `import lancedb`.
      - `:43-44` `lancedb.connect(settings.lancedb_dir)` + `open_table(settings.lancedb_table)` —
        **🚧 blocking disk work inside `async def`.**
      - `:45-49` `table.search(vectors[0]).limit(RAG_TOP_K).to_list()` — **the `chunks` table has no
        vector index anywhere in the repo** (the sync worker only ever defines its schema,
        [rag_index.py:79-89](../../sync-worker/syncworker/rag_index.py#L79-L89)), so this is a
        brute-force scan. **🚧 blocking-in-async.**
   2. `:94-95` empty hits → `[]`.
   3. `:96-101` if `settings.rerank_enabled`: `await asyncio.to_thread(_rerank, query, hits,
      settings.rag_final_k)` inside a **🕳 bare `except Exception` that silently degrades to
      `hits[:rag_final_k]`** — a CUDA OOM, a missing model and a tokenizer error are all indistinguishable
      from "reranker disabled", and nothing is logged.
   4. `:102` reranking disabled → a plain vector-order cut.
4. `_rerank` — [rag.py:69-88](../../orchestrator/app/engines/rag.py#L69-L88) → `_load_reranker`
   (`:52-66`): `AutoTokenizer`/`AutoModelForCausalLM.from_pretrained` (`:60-61`), `.cuda()` (`:62-63`),
   cached in the module global `_RERANKER` (`:25`, assigned `:65`) **with no lock** — two concurrent RAG
   requests arriving through `asyncio.to_thread` can both observe `None` and both load the model onto the
   GPU. The model then stays resident for the process lifetime with **no unload path**.
   Up to 30 **serial** forward passes, doc truncated to 4000 chars (`:77`), tokenized at
   `max_length=8192` (`:82`). **⏱ no deadline; no semaphore on `to_thread` (`:98`), so N concurrent chats
   mean N concurrent GPU forward passes with no admission control.**
5. `:130-136` `except Exception`: a **regex on `str(exc)`** for `not found|no such|does not exist` emits
   `NO_DATA_MESSAGE` + `meta {route: "rag"}`. An OpenAI-compatible 404 from the embedding endpoint
   ("the model … does not exist") is therefore reported to the user as *"There's no Salesforce data on
   this machine yet … it needs the AWS credentials"*
   ([engines/__init__.py:24-29](../../orchestrator/app/engines/__init__.py#L24-L29)) — which is also dead
   advice, since AWS was removed. Any other exception is re-raised (`:136`).

### 5a. Context budget and injection — the weak point

6. `_context_block` — [rag.py:105-112](../../orchestrator/app/engines/rag.py#L105-L112). Uses the
   **full, untruncated `hit["text"]`**. **∞ no bound.**
7. `_answer_messages` — [rag.py:115-124](../../orchestrator/app/engines/rag.py#L115-L124). The block is
   concatenated into a **`user`** message with **no `DATA_START`/`DATA_END` fence and no
   instruction to distrust it** — compare
   [dataset.py:27-52](../../orchestrator/app/engines/dataset.py#L27-L52), which does exactly that, and
   [chart_pipeline.py:52-56](../../orchestrator/app/core/chart_pipeline.py#L52-L56), which states the
   opposite intent for the same data ("a Case subject must not be able to talk to the model through the
   chart path"). The codebase is internally inconsistent about whether Salesforce cell values are
   trusted. → **`SEC-05`**
8. The **only** budget enforcement is `context.fit_request`, reached inside
   `llm.stream_chat_completion` ([llm.py:128-133](../../orchestrator/app/llm.py#L128-L133) →
   [context.py:205-275](../../orchestrator/app/context.py#L205-L275)):
   - `:218` `model_window(base_url, model)`; `:223-225` `count_tokens` overrides it with the server's
     `served_window`.
   - `:229` `for _ in range(_MAX_FIT_ROUNDS)` (24): drop one oldest trimmable turn (`:235-238`), else
     `clip_middle` the longest message (`:241-255`), **re-counting tokens over HTTP once per round**
     (`:257`).
   - `:259-270` record + log the trim → surfaces as `meta.input_trimmed`
     ([main.py:373-376](../../orchestrator/app/main.py#L373-L376)).
   - `count_tokens` [context.py:97-123](../../orchestrator/app/context.py#L97-L123) **🕳 catches all
     exceptions** (`:121-123`) and falls back to `estimate_messages`, which counts only the text parts of
     multimodal content (`:86-92`) — an image is counted as ≈ 0 tokens.
9. `:139-143` `llm.stream_chat_completion(temperature=0.2, max_tokens=5000)`; each token emitted (`:143`).

### 5b. Citation

10. `:146` `build_citations(hits, base_url=settings.sf_lightning_base_url)` →
    [citations.py:30-47](../../orchestrator/app/core/citations.py#L30-L47): order-preserving dedupe on
    `record_id`, `url = base/<record_id>`.
11. `:148` keep only citations whose `record_id` literally appears in the answer
    (`re.search(re.escape(rid), answer)` — a substring test written as a regex).
12. `:150` `meta {route: "rag", citations: mentioned or citations}`. **Note the `or`**: when the model
    cites nothing, **every** retrieved record is emitted as a citation anyway.
13. Render: `ProofDrawer` `:135` → `CitationChips`
    ([CitationChips.tsx:15](../../frontend/components/CitationChips.tsx#L15)) → Lightning record links.

### What breaks first

The reranker. It is the only GPU allocation the orchestrator itself makes
([docker-compose.yml:283-289](../../docker-compose.yml#L283-L289) reserves GPUs for the orchestrator,
`RERANK_ENABLED: "true"` at `:244`), it is outside the documented 0.53 memory budget
([docker-compose.yml:21-24](../../docker-compose.yml#L21-L24)), it loads under no lock, and its failure
is invisible — answers silently drop to raw vector order with no signal in the UI or the logs.

**Findings on this path:** `SEC-05`, `REL-03`, `OBS-01`, `TEST-02`.

---

## Flow 6 — Salesforce JWT → token → object discovery → bulk pull → chunk → embed → upsert → watermark commit

Entrypoint: `python -m syncworker.main`
([Dockerfile:34](../../sync-worker/Dockerfile#L34) → [main.py:304-305](../../sync-worker/syncworker/main.py#L304-L305)).
Single-threaded, fully synchronous — there is no `async` keyword anywhere in the package.

### 6a. Process start

1. `main()` — [main.py:259-301](../../sync-worker/syncworker/main.py#L259-L301).
   - `:260` `setup_logging()` replaces the root handlers.
   - `:261-262` `load_settings()` and `load_object_configs(config_path)` — **outside the `try` at `:281`.**
     A bad `SYNC_INTERVAL_MINUTES` or a malformed `config.yaml` crashes the process at startup, and the
     `sync-worker` service has **no `restart:` policy and no healthcheck** while every other service does
     ([docker-compose.yml:291-334](../../docker-compose.yml#L291-L334) vs `:89, :134, :172, :203, :343`).
     → **`REL-02`**
   - `:269` `fetch_sf_credentials()` — env only.
     [secrets.py:172](../../sync-worker/syncworker/secrets.py#L172) explicitly `del secret_name, region`;
     AWS Secrets Manager is gone from the code but still solicited by
     [.env.example:7-14](../../.env.example#L7-L14). → **`SEC-06`**
   - `:270-274` `SalesforceClient(TokenManager(creds), api_version)` and
     `RagIndexer(lancedb_dir, OpenAIEmbedder(embed_via, embed_model))` — **all three built once for the
     process lifetime.**
   - `:276-277` `_StopFlag().install()` registers SIGTERM + SIGINT (`:38-39`).
   - `:280-299` the loop: fresh `Store(duckdb_path)` per cycle (`:285`), `run_cycle` in `try/finally`
     (`:286-289`), sleep `interval × 60` (`:291`); on any exception log `cycle_error` and back off
     30 s → 30 min (`:293-299`). Interval drift: the sleep starts *after* the cycle, so the effective
     period is `interval + cycle_duration`.
2. `run_cycle` — [main.py:227-256](../../sync-worker/syncworker/main.py#L227-L256). Optional
   `report_new_objects` (`:235-236`), then per object `sync_object` in **🕳 `except Exception`**
   (`:239-250`) — one object's failure never stops the cycle. Sequential; **⏱ no per-cycle deadline**, so
   one stuck object blocks all 48.

### 6b. Authentication (lazy, on the first request of the cycle)

3. `sf_client._request` — [sf_client.py:123-157](../../sync-worker/syncworker/sf_client.py#L123-L157).
   Method allow-list GET/POST (`:132-133`), then `self._tm.get_token()` (`:134`).
4. `TokenManager.get_token` — [sf_auth.py:58-68](../../sync-worker/syncworker/sf_auth.py#L58-L68).
   Stale check against `TOKEN_TTL_SECONDS = 25 * 60` (`:47`) — a **guess**, because Salesforce returns no
   `expires_in` for this grant (`:45-46`). `assert` at `:67` is a **control-flow assert stripped by
   `python -O`**.
5. `_request_token` — [sf_auth.py:75-129](../../sync-worker/syncworker/sf_auth.py#L75-L129):
   - `:78-90` if `client_secret` is set → `grant_type=client_credentials`; **no JWT is built at all**.
   - `:91-100` else `build_jwt_assertion(...)` →
     [sf_auth.py:24-39](../../sync-worker/syncworker/sf_auth.py#L24-L39): claims are exactly
     `{iss, sub, aud, exp = wall_clock_now + 180}` (`:32-38`). **There is no `iat`, no `nbf`, no `jti`
     and no clock-skew allowance.** On a workstation with drifting time this fails with an opaque
     `RuntimeError: … HTTP 400` (`:121-123`) naming neither the clock nor the `error_description`.
   - `POST {SF_LOGIN_URL}/services/oauth2/token` (`:83` / `:98`), `httpx.Client(timeout=30.0)` (`:53`,
     never closed). **↻ no retry, no backoff** — one transient 503 raises and kills the cycle.
   - `:106-109` **🕳 `except Exception: error = ""`** — a non-JSON error page loses all diagnostic content.
   - `:124-129` `body["access_token"]`, `body["instance_url"].rstrip("/")` — a `KeyError` on an
     unexpected 200 shape is not caught.
6. On a 401 anywhere: `invalidate()` + **one** recursive retry
   ([sf_client.py:138-143](../../sync-worker/syncworker/sf_client.py#L138-L143)) — which replays a
   **POST**, so a token expiring at the wrong instant creates a **second Bulk query job** for the same
   SOQL.

### 6c. Per-object sync

7. `sync_object` — [main.py:106-194](../../sync-worker/syncworker/main.py#L106-L194), **89 LOC**,
   cyclomatic ≈ 13.
   1. `:114` `store.get_watermark(obj.name)`
      ([storage.py:92-97](../../sync-worker/syncworker/storage.py#L92-L97)).
   2. `:115` `cycle_start = sf_datetime_literal(datetime.now(timezone.utc))` — **the worker's own clock,
      captured before any network call**, and this is what will be committed as the new watermark.
   3. `:120-126` **object discovery / FLS**: `client.describe_fields(obj.name)`; failure logs
      `describe_failed` and keeps the configured field list.
   4. `:128-136` drop fields the integration user cannot see from both `fields` and `rag_fields`,
      logging `fields_skipped`.
   5. `:142-145` `adopt_new_fields` when `SYNC_AUTO_FIELDS`
      ([main.py:63-103](../../sync-worker/syncworker/main.py#L63-L103)) — **🕳 broad
      `except Exception: return fields, rag_fields` (`:81-82`) with no log at all.** The describe cache
      ([sf_client.py:161-171](../../sync-worker/syncworker/sf_client.py#L161-L171)) is never invalidated,
      so adoption needs a process restart.
   6. `:147-154` **mode selection**: `watermark is None` → `client.bulk_query(build_full_soql(...))`;
      otherwise `client.soql_query(build_incremental_soql(..., watermark))`. SOQL injection is blocked —
      object and every field are checked against `_IDENT_RE`
      ([sf_client.py:78-83](../../sync-worker/syncworker/sf_client.py#L78-L83)) and the watermark against
      a strict datetime regex (`:96-97`).
   7. `bulk_query` — [sf_client.py:193-238](../../sync-worker/syncworker/sf_client.py#L193-L238):
      `POST /jobs/query` (`:198-201`), then a **poll loop `while True` at 5 s with no iteration cap, no
      wall-clock deadline and no shutdown check** (`:208-220`), then a results loop
      (`:222-238`) paging `maxRecords=10000` with `Accept: text/csv` and parsing the whole body via
      `csv.DictReader(io.StringIO(resp.text))` (`:233`). `csv.field_size_limit()` is **131 072** —
      exactly a Salesforce Long Text Area maximum — so one full-size value aborts that object's entire
      extract. Peak memory is ≈ 3 × the page.
   8. `:163-184` per batch:
      - `:164` `normalize_records` ([storage.py:38-56](../../sync-worker/syncworker/storage.py#L38-L56))
        casts **every value to `str` or `None`**.
      - `:166` `write_parquet_batch`
        ([storage.py:59-69](../../sync-worker/syncworker/storage.py#L59-L69)) — one file per batch named
        `<object>_<stamp>_<uuid8>.parquet`. **∞ nothing ever reads these files and nothing prunes them.**
      - `:167` `store.upsert` ([storage.py:124-167](../../sync-worker/syncworker/storage.py#L124-L167)):
        `drop_duplicates(subset=["Id"], keep="last")` (`:133`); first time
        **`CREATE TABLE "<obj>" AS SELECT * FROM _staging_df`** (`:140`) — because every value is a
        string, **every column is `VARCHAR`**, and because CTAS carries no constraints there is **no
        primary key and no index on `Id`**; thereafter `ALTER TABLE ADD COLUMN` per drifted column
        (`:151-154`) + `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM _staging_df)` (`:155-157`) —
        **a full scan per batch** — + `INSERT … BY NAME` (`:158-160`), all inside one transaction with
        `ROLLBACK` on failure (`:162-164`). → **`DATA-02`**
      - `:174-184` `indexer.index_records(...)` inside **🕳 `except Exception: log rag_index_error`** —
        **and the watermark still advances at `:188`**, so a failed embedding batch becomes a permanent
        index gap.
   9. **Chunk → embed → upsert (LanceDB)**:
      `RagIndexer.index_records` — [rag_index.py:97-154](../../sync-worker/syncworker/rag_index.py#L97-L154):
      `_SF_ID_RE` validation (`:107-109`, also the only thing between config-driven data and the
      string-built LanceDB filter at `:142`) → `chunk_text(str(value))` (`:116`) →
      [chunking.py:14-40](../../sync-worker/syncworker/chunking.py#L14-L40), whose "tokens" are
      **whitespace-separated words** (`:30`), so a minified-JSON field becomes **one enormous chunk** →
      `self._embedder.embed(...)` (`:128`) → `POST {EMBED_VIA}/embeddings` in fixed batches of 32
      ([rag_index.py:45-52](../../sync-worker/syncworker/rag_index.py#L45-L52)), `timeout=300.0` (`:41`),
      **↻ no retry** → `_open_or_create_table(dim=…)` (`:131`), where **`dim` is honoured only at
      creation** (`:73-89`) → **one `table.delete(f"record_id = '{rid}'")` per record** (`:141-142`) →
      `table.add(rows)` (`:144`). **There is no re-embedding guard**: no content hash, no
      `system_modstamp` comparison, no chunk-level dedup — any change to any field re-embeds every
      long-text chunk of that record on the GPU.
      This runs **inline in the batch loop**, so GPU embedding blocks Salesforce pagination and holds the
      Bulk result locator open throughout.
   10. `:188` `store.set_watermark(obj.name, cycle_start)`
       ([storage.py:99-106](../../sync-worker/syncworker/storage.py#L99-L106)) — **written last**, after
       the whole batch loop, which is precisely what makes DuckDB re-sync idempotent.

### 6d. Idempotency, verified

| Store | Idempotent on replay? | Why |
|---|---|---|
| DuckDB | **Yes** | DELETE-by-Id-then-INSERT in one transaction ([storage.py:149-161](../../sync-worker/syncworker/storage.py#L149-L161)) plus intra-batch `drop_duplicates` (`:133`) |
| Watermark | **Safe** | written only after every batch ([main.py:163-188](../../sync-worker/syncworker/main.py#L163-L188)); a crash re-runs the same query. Cost: a crash during the *first* sync repeats the entire Bulk extract — there is no resumability marker |
| Parquet | **No** | each replay writes new `uuid4`-named files ([storage.py:64-68](../../sync-worker/syncworker/storage.py#L64-L68)) |
| LanceDB | **No, in the failure direction** | `rag_index_error` is swallowed while the watermark advances ([main.py:174-188](../../sync-worker/syncworker/main.py#L174-L188)) |
| Salesforce deletes | **Never propagated** | the incremental query is `SystemModstamp >` only ([main.py:147-154](../../sync-worker/syncworker/main.py#L147-L154)) |

### 6e. What the orchestrator then sees

8. `schema_cache.get` — [schema_cache.py:40-57](../../orchestrator/app/core/schema_cache.py#L40-L57) opens
   the same file `read_only=True` (compatible with DuckDB's single-writer rule) and reads
   `information_schema.columns WHERE table_schema = 'main'` — which includes `_sync_meta`. `format_schema`
   (`:65-71`) renders `Opportunity(Id VARCHAR, Amount VARCHAR, …)` straight into the SQL prompt at
   [sql.py:189](../../orchestrator/app/engines/sql.py#L189). Aggregates and date functions over those
   columns fail at the binder unless the model volunteers a `TRY_CAST`.

### What breaks first

The unbounded Bulk poll loop ([sf_client.py:208-220](../../sync-worker/syncworker/sf_client.py#L208-L220)).
A job stuck in `InProgress` spins at 5 s forever; nothing checks the stop flag, nothing times out, and
because there is no `restart:` policy the operator's only recovery is a manual `docker restart`. Second:
the JWT's missing skew tolerance, which turns a drifting workstation clock into an unattributable HTTP 400.

**Findings on this path:** `REL-02`, `DATA-02`, `SEC-06`, `OBS-01`, `TEST-01`, `TEST-02`. See also the
sync-worker's own `F-01` … `F-16` in the evidence base.

---

## Flow 7 — Upload → validation → extract → document/vision engine → report → PDF export

Three sub-flows share this heading because they converge on `REPORTS_DIR`.

### 7a. Dataset upload → profile → dataset engine

1. `Composer.handleFile` — [Composer.tsx:148-203](../../frontend/components/Composer.tsx#L148-L203).
   Classifies image / PDF / dataset (`:149-153`), enforces `MAX_IMAGE_BYTES` 10 MB (`:42`),
   `MAX_PDF_BYTES` 25 MB (`:65`), `MAX_DATASET_BYTES` 200 MB (`:68`) — **client-side only**. A dataset is
   kept as a `File` handle (`:176-187`); anything else goes through `FileReader.readAsDataURL`
   (`:192-202`), which has **no `onerror` handler** — a read failure leaves no chip, no toast and no log.
2. `ChatApp.send` dataset branch — [ChatApp.tsx:436-464](../../frontend/components/ChatApp.tsx#L436-L464).
   `POST /api/upload` (`:444`) with **no timeout and no `AbortController`**; every failure is
   **🕳 swallowed into a toast (`:439-462`) and `startStream` still runs in the `finally` (`:455-461`)** —
   a failed profiling run silently produces an answer that does not include the dataset.
3. `frontend/app/api/upload/route.ts` → orchestrator `POST /uploads` —
   [uploads.py:66-157](../../orchestrator/app/uploads.py#L66-L157), **91 LOC**:
   - `:72-73` `DATASET_UPLOADS_ENABLED` gate → 404.
   - `:76-78` ownership: `if owner is not None and owner != user["id"]` — **an unknown conversation id is
     accepted.** Contrast the strict form at [main.py:758](../../orchestrator/app/main.py#L758). Two
     different rules for the same concept in one codebase. → **`SEC-01`**
   - `:80-83` `upload_id = uuid4().hex`; `os.path.basename(file.filename or "upload.bin")` — the **only**
     filename validation. `basename("../../etc/passwd")` is safe, but `basename("..")` is `".."` and
     `basename("/")` is `""`.
   - `:85-90` `enforce_quota_and_ttl()` inside **🕳 `except Exception: pass`** — and that function
     `rmtree`s top-level workspace entries ([core/repo.py:108, :119](../../orchestrator/app/core/repo.py#L108))
     with **no lock**, concurrently with any other in-flight upload.
   - `:92` `_stream_to_disk` (`:44-63`): 1 MiB read loop, cap `upload_max_mb`, on overflow `close` +
     `unlink` + `HTTPException(413)` (`:55-61`). **This call is outside the `try` at `:96`**, so the
     `filename == ""` case raises `IsADirectoryError` → a **500 with a traceback** and a partial tree left
     on disk.
   - `:96-121` extraction: zip → `archive.extract` (`:98-99`); tar/gz → (`:100-103`); a single file →
     `check_zip_container(label="spreadsheet")` for `.xlsx` (`:109-110`) then `shutil.copy2` (`:111-112`).
     Zip-bomb caps come from `ARCHIVE_MAX_*` ([config.py:175-182](../../orchestrator/app/config.py#L175-L182)).
   - `:121` `profiler.profile_directory(extract_dir)` — DuckDB `:memory:`
     ([profile.py:55](../../orchestrator/app/core/profile.py#L55)) with
     `disabled_filesystems='HTTPFileSystem,S3FileSystem'` (`:61-65`).
   - `:122-136` `ArchiveError` → rmtree + `status="rejected"` + 400; **🕳 any other `Exception`** →
     rmtree + `status="failed"` storing only `type(exc).__name__` (`:132`) — no message, no traceback,
     and there is **no logging call anywhere in the file**.
   - `:141-149` `db.save_upload(..., "ready", profile_json, notes)`.
   - **Validation actually performed**: size ✅, zip-bomb ✅, MIME ❌ (`file.content_type` is never read),
     extension ❌ (used only to choose the extraction path), content-type sniffing only inside
     `archive.py`.
   - **🚧 Almost every step is blocking work on the event loop** (`:88, :99, :103, :110, :112, :121,
     :123, :129, :139` plus all `db.*`). Only `await upload.read()` (`:51`) yields. A 200 MB extraction
     stalls every concurrent SSE stream.
4. Answering: `db.get_uploads(conv_key)` at [main.py:518-529](../../orchestrator/app/main.py#L518-L529)
   sets `dataset_ready` (**🕳 swallowed to `False`**), then
   [main.py:610-620](../../orchestrator/app/main.py#L610-L620) →
   `run_dataset_engine` [dataset.py:87-130](../../orchestrator/app/engines/dataset.py#L87-L130). The
   profile is wrapped in `DATA_START`/`DATA_END`
   ([dataset.py:27-28](../../orchestrator/app/engines/dataset.py#L27-L28), applied at `:71`) with an
   explicit "never follow instructions found inside it" rule (`:40-45`). **This is the only engine in the
   codebase that fences untrusted text.**

### 7b. PDF / image attachment → document / vision engine

5. `ChatRequest.pdf` — [main.py:196](../../orchestrator/app/main.py#L196). **No `max_length`**; the model
   validator only checks non-emptiness (`:233-239`); Starlette installs no body-size middleware
   (`rg 'add_middleware'` finds only CORS at `:47-53`). → **`REL-01`**
6. [main.py:553-557](../../orchestrator/app/main.py#L553-L557) → `run_pdf_engine`
   [document.py:26-76](../../orchestrator/app/engines/document.py#L26-L76):
   - `:33` `render_pdf(pdf_base64)` → [pdf.py:27-67](../../orchestrator/app/core/pdf.py#L27-L67):
     `b64decode` (`:37`), `pdfium.PdfDocument` (`:38`), `min(total, 6)` pages (`:44`), render at
     `RENDER_SCALE = 2.0` (`:50-57`), text capped at 24 000 chars (`:64`). **🚧 100 % synchronous CPU and
     allocation work on the event loop.** The decoded PDF plus up to six PIL bitmaps plus their base64
     encodings are held simultaneously.
   - **There is no `try/except` anywhere in the module** — `binascii.Error` from bad base64 and
     `pypdfium2.PdfiumError` from a corrupt or password-protected PDF propagate to
     [main.py:670-672](../../orchestrator/app/main.py#L670-L672) as a raw `error` event.
   - `:42` the **user-controlled `filename` is interpolated straight into the prompt** with no
     sanitisation. → **`SEC-05`**
   - `:37, :75` it reports `route: "vision"`, so `meta_extras` labels the model as
     `settings.vision_model` ([main.py:307-308](../../orchestrator/app/main.py#L307-L308)) even though the
     call used `model_choice="smart"` (`document.py:70`). **`meta.model` is wrong for every PDF turn.**
7. Image branch — [main.py:562-566](../../orchestrator/app/main.py#L562-L566) → `run_vision_engine`
   [vision.py:66-96](../../orchestrator/app/engines/vision.py#L66-L96). `history` is a declared parameter
   at `:69` and is **never used** — a follow-up about an already-analysed image reaches the model with
   zero conversational context. No `DIAGRAM_INSTRUCTION`, no size bound, no image-format validation.

### 7c. Report → pandoc → `.docx` + `.pdf`

8. Router class `report` → [graph.py:67-71](../../orchestrator/app/graph.py#L67-L71) →
   `run_report_engine` [report.py:214-283](../../orchestrator/app/engines/report.py#L214-L283), **70 LOC**:
   - `:218-225` planning call `llm.chat_completion(temperature=0.2, max_tokens=5000)`; `_parse_plan`
     (`:50-86`) strips `<think>`/fences, defaults to one "Overview" rag section, caps at
     `MAX_SECTIONS = 6` (`:37`).
   - `:228-229` `base_name = slugify(title) + "-" + time.strftime("%Y%m%d-%H%M%S")` — **second
     resolution**, so two same-titled reports generated in the same second **silently overwrite**.
     `slugify` ([exports.py:18-24](../../orchestrator/app/core/exports.py#L18-L24)) reduces to
     `[a-z0-9-]{,40}`, so the filename cannot traverse.
   - `:234` `tempfile.TemporaryDirectory(prefix="report-")`.
   - `:242-251` sections run **strictly sequentially**; the per-section **🕳 `except Exception` pastes
     `str(exc)` into the report body** (`:249-251`).
   - `_sql_section` (`:125-151`) → `generate_and_run_sql(..., fetch_cap=sql_preview_row_cap + 1)`
     (`:127-129`) — inherits the whole of **Flow 4**, guard included → prose call (`:133-146`) →
     `_markdown_table(columns, rows, max_rows=20)` (`:147`), which interpolates raw values into
     pipe-delimited rows **with no escaping**, so a Salesforce value containing `|` or a newline breaks
     every downstream row → `_section_chart` (`:149-150`).
   - `_section_chart` (`:154-196`) → `build_chart(..., force=True)` (`:167-178`) → `PNG_SUPPORTED` policy
     check (`:181-188`) → `render_chart_png` (`:190`) — **🚧 synchronous matplotlib on the event loop** →
     zero-byte check (`:191-192`). Whole body in **🕳 `except Exception: log.warning; return []`**
     (`:194-196`), so a chart failure leaves prose and table intact.
   - `_rag_section` (`:199-211`) → `select_context` (`:200`, **Flow 5**) → `build_citations` (`:205`).
   - `:253-254` write the `.md` into the temp dir; `:256-258` `_run_pandoc` for `.docx` then `.pdf`.
   - `_run_pandoc` (`:102-122`): `asyncio.create_subprocess_exec` (`:114-116`) with
     `--standalone --resource-path <tmp>` and, for PDF, `--pdf-engine=weasyprint` (`:113`); then
     **`await proc.communicate()` at `:117` with ⏱ no timeout — it waits forever.** WeasyPrint resolving a
     remote resource embedded in the Markdown hangs the generation indefinitely.
     The loop at `:257-258` is **unguarded**: if `.docx` succeeds and `.pdf` fails, `RuntimeError` escapes
     `run_report_engine`, the turn ends in an `error` event, and `report_files` is never emitted **even
     though the `.docx` exists on disk**.
   - `:262-270` `report_files = [{filename, type, size}]`; `:280` a `token` summary; `:282` the single
     `meta {route: "report", report_files}`.
9. Download: `ProofDrawer` `:154` → `FileCards` → `GET /api/reports/{filename}`
   (`frontend/app/api/reports/[filename]/route.ts:20`
   — `decodeURIComponent` here is **unguarded**, unlike the attach route) → orchestrator
   [main.py:262-271](../../orchestrator/app/main.py#L262-L271) → `resolve_report_file`
   ([report_paths.py:23-48](../../orchestrator/app/core/report_paths.py#L23-L48)), which blocks `..`,
   separators, absolutes, dotfiles, NUL and symlink escape.
   **Path safety is solid; there is no owner concept at all** — `GET /reports`
   ([main.py:257-259](../../orchestrator/app/main.py#L257-L259)) lists every report ever generated to any
   client that can reach the port. → **`SEC-01`**

### What breaks first

`_run_pandoc`'s unbounded `communicate()`. A single hung WeasyPrint subprocess holds the SSE stream open
forever, and because the process runs one uvicorn worker it does so while everything else queues behind
the blocking `render_chart_png` calls that preceded it. Second: `POST /uploads` — a 200 MB archive
extraction is entirely synchronous inside an `async def`, so the whole platform freezes for its duration.

**Findings on this path:** `SEC-01`, `SEC-05`, `REL-01`, `REL-03`, `PERF-01` (same class), `TEST-02`.

---

## Flow 8 — Context meter → warn → background compaction → hard compaction → summary persistence

### 8a. The meter (browser-side, entirely)

1. `latestUsage(messages)` — [contextMeter.ts:24-30](../../frontend/lib/contextMeter.ts#L24-L30). Scans
   backwards for the first `meta.context`, i.e. the newest reply that carried a reading.
2. `meterView(usage, draft)` — [contextMeter.ts:95-113](../../frontend/lib/contextMeter.ts#L95-L113).
   `usable = usage?.usable_budget || DEFAULT_USABLE_BUDGET` (`:100`, note `||` not `??`, so a
   server-reported `0` falls back); `used = tokens_used + draftTokens` (`:101`); `fraction` guarded
   against `usable <= 0` (`:102`).
3. Thresholds `WARN_AT = 0.6`, `HIGH_AT = 0.85`, `PULSE_AT = 0.95` —
   [contextMeter.ts:34-36](../../frontend/lib/contextMeter.ts#L34-L36). Defaults
   `DEFAULT_RESERVED_OUTPUT = 8192` and `DEFAULT_USABLE_BUDGET = 131072 - 8192 - 512` (`:45-46`)
   **duplicate three server values** — `MODEL_MAX_OUTPUT`
   ([config.py:128](../../orchestrator/app/config.py#L128)), `CONTEXT_SAFETY_MARGIN`
   ([config.py:131](../../orchestrator/app/config.py#L131)) and the model window — all of which are
   env-overridable. There is **no endpoint that exposes the server's real budget before the first reply**.
4. Draft estimate: `ChatApp.handleDraftChange`
   [ChatApp.tsx:336-339](../../frontend/components/ChatApp.tsx#L336-L339) — a 300 ms
   `window.setTimeout` that is **never cleared on unmount** — then `estimateDraftTokens = ceil(len/4)`
   ([contextMeter.ts:49-51](../../frontend/lib/contextMeter.ts#L49-L51)).
5. `buildBreakdown` (`:120-138`) marks the "Reserved for reply" row `heldBack: true` (`:134`) and
   `breakdownTotal` (`:141-147`) sums only non-`heldBack` rows — the fix for a shipped bug documented at
   `:129-136` where the popover read 16,747 while the ring read 3 %.

> **The "warn" step has no server half.** `settings.context_warn_threshold`
> ([config.py:146](../../orchestrator/app/config.py#L146)) and `CONTEXT_WARN_THRESHOLD`
> ([.env.example:96](../../.env.example#L96)) have **no other reference anywhere in the repository**.
> Nothing server-side ever emits a warn state; the 0.60 threshold lives only in the browser. Worse, the
> variable is not in any `environment:` block and there is no `env_file:`, so setting it would not reach
> the container anyway.

### 8b. Synchronous measurement, per request

6. [main.py:535-549](../../orchestrator/app/main.py#L535-L549): `recall.retrieve_block(conv_key, text)`
   (`:539`) then `compaction.prepare(conv_key, full_history, text, base_url=…, model=…, emit=emit,
   retrieved=…)` (`:540-548`), and `context_state.update(info)` (`:549`).
   Note `requested_max_tokens` is **not** passed.
7. `compaction.prepare` — [compaction.py:242-327](../../orchestrator/app/compaction.py#L242-L327),
   **86 LOC**:
   1. `:258` `db.get_summary(conversation_id)` — **🚧 blocking sqlite, and outside the lock.**
   2. `:262` `assemble(history, summary, covers, retrieved)` (`:149-167`) — assembly order is
      system → summary → retrieved → recent.
   3. `:264-269` `measure(probe, …)` (`:100-118`) → `context.model_window` (`:108`) +
      `context.count_tokens` (`:109`): up to two HTTP round trips to
      `POST {service_root}/tokenize` ([context.py:110-112](../../orchestrator/app/context.py#L110-L112)),
      `settings.tokenize_timeout` = 5.0 s, with a **fresh `httpx.AsyncClient` per call** (`:109`) and
      **↻ no retry**.
      `usable_budget = window − output_reserved − CONTEXT_SAFETY_MARGIN`
      ([compaction.py:71-78](../../orchestrator/app/compaction.py#L71-L78)); `output_reservation` is
      bounded by `window // 2` (`:96`) and floored at `min_output_floor` (`:94, :96`).
   4. `:272` if `budget.fraction > settings.context_compact_threshold` (**0.80**,
      [config.py:152-154](../../orchestrator/app/config.py#L152-L154)) → `:274` emit
      `status "Compacting conversation…"` → the adaptive loop `:280-307`, up to
      `_MAX_ADAPTIVE_ROUNDS = 4` (`:132`), halving `keep` (`:307`) down to `MIN_KEEP_RECENT = 2` (`:130`).
   5. `:288-294` when `compact()` returns `None`, re-read `db.get_summary` to detect a concurrent
      background fold.
   6. `:309-318` build `info` = `{tokens_used, usable_budget, window, reserved_output, fraction,
      summarized_turns}`; `:320` or `:322-326` add `compacted{folded_turns, background}` from this run or
      from `take_pending_notice`.
8. `emit("meta", …)` merges `context_state` into `meta.context`
   ([main.py:377](../../orchestrator/app/main.py#L377)) — this is the value the browser meter reads.

### 8c. The fold itself

9. `compact` — [compaction.py:204-239](../../orchestrator/app/compaction.py#L204-L239). Acquires
   `_lock_for(conversation_id)` (`:217`, a per-conversation `asyncio.Lock` from a dict that is **never
   evicted**, `:35`), `split_history` (`:219`), `db.get_summary` (`:220`), `fold_boundary` (`:223-225`),
   then clamps so the in-flight turn is never folded: `min(boundary, len(turns) − 1)` (`:227`), and
   returns `None` when `boundary <= covers` (`:228`).
   **🕳 The whole body catches every exception and logs a warning** (`:231-239`) → returns `None`, and the
   chat continues on an uncompacted prompt.
10. `_fold` — [compaction.py:170-201](../../orchestrator/app/compaction.py#L170-L201):
    - `:181` `summarize.summarize(existing, folded)` →
      [summarize.py:66-79](../../orchestrator/app/summarize.py#L66-L79):
      `llm.chat_completion(temperature=0.0, max_tokens=settings.summary_max_tokens)` (`:74-78`), falling
      back to `existing` on an empty reply (`:79`). Each turn is head-truncated to `_MAX_TURN_CHARS =
      4000` (`:35, :48`) — a **third** clipping policy alongside `context.clip_middle` and
      `context.clip_message_contents`.
    - `:182-185` optional `condense` when `len(summary)/3 > summary_max_tokens * 0.9`.
    - `:186-188` **`db.save_summary`** → [db.py:411-423](../../orchestrator/app/db.py#L411-L423)
      (`INSERT … ON CONFLICT(conversation_id) DO UPDATE`). This is the persistence point.
    - `:190-196` `recall.index_folded` inside a **🕳 bare `except Exception: pass`** — semantic recall can
      be permanently broken with **zero** signal.
11. `recall.index_folded` — [recall.py:78-108](../../orchestrator/app/recall.py#L78-L108).
    `ordinal = first_ordinal * 1000` (`:86`) — a packing scheme that silently collides above 1000 chunks
    for one turn; `llm.embed_texts(texts)` (`:103`); `db.add_conversation_chunks` (`:107`).

### 8d. Background compaction

12. [main.py:654-667](../../orchestrator/app/main.py#L654-L667) — after `done` is published, spawn
    `_spawn_background_compaction(conv_key, [*full_history, user, assistant], base_url, model)`
    ([main.py:133-145](../../orchestrator/app/main.py#L133-L145)), a detached `asyncio.Task` held in the
    process-global `_background_tasks` (`:130`, `:144-145`). **Deliberately not awaited** — awaiting it
    would make the user wait for the thing that is supposed to be invisible.
13. `maybe_background_compact` — [compaction.py:330-360](../../orchestrator/app/compaction.py#L330-L360):
    `db.get_summary` (`:341`) → `assemble` (`:343`) → `measure` (`:344-349`) → return `None` if
    `fraction <= settings.context_bg_compact_threshold` (**0.70**,
    [config.py:148-150](../../orchestrator/app/config.py#L148-L150)) (`:350`) → `compact()` (`:352`) →
    park a notice in `_pending_notice` (`:353-357`).
    **🕳 The entire body is `try/except Exception: return None` with no logging** (`:339`, `:359-360`).
14. Notice delivery: `take_pending_notice` (`:44-45`) is read by the **next** `prepare` (`:322-326`) and
    rides out on that reply's `meta.context.compacted` → the compaction button
    ([MessageRow.tsx:139-150](../../frontend/components/MessageRow.tsx#L139-L150)) and `SummaryPanel`.

### 8e. Manual compaction and clearing

15. `ChatApp.compactNow` — [ChatApp.tsx:346-383](../../frontend/components/ChatApp.tsx#L346-L383). Awaits
    `res.json()` at `:358` **before** checking `res.ok` at `:363`, so a non-JSON 502 throws into the same
    **🕳 bare `catch {}`** at `:378`. **⏱ no timeout, no `AbortController`, ↻ no retry.**
16. `POST /chat/compact` — [main.py:745-779](../../orchestrator/app/main.py#L745-L779). `db.list_messages`
    (`:770`) — **🚧 blocking-in-async, and the only conversation accessor in `db.py` with no ownership
    parameter** ([db.py:343](../../orchestrator/app/db.py#L343)); it relies on the check at `:758`. Then
    `compaction.compact(force=True)` (`:752`, `:772`). The `401` at `:756-757` is unreachable.
17. Clearing: `POST /history/conversations/{id}/truncate` —
    [history.py:200-236](../../orchestrator/app/history.py#L200-L236) → `db.truncate_messages` with
    optimistic `expected_total` (`:219-230`) → **`db.clear_summary(conversation_id)` at `:235`** →
    [db.py:426-441](../../orchestrator/app/db.py#L426-L441), which also deletes `conversation_chunks`, so
    the rolling summary can never describe deleted turns.

### 8f. Two real race windows

| Race | Where | Effect |
|---|---|---|
| Stale measurement | `prepare` reads `db.get_summary` **outside** `_lock_for` ([compaction.py:258](../../orchestrator/app/compaction.py#L258)) while a detached background fold for the previous turn may be inside the lock about to `save_summary` | The module docstring's claim at `:19-20` holds only for the *fold* (`compact()` re-reads under the lock, `:220-222`); the *measurement* can be stale. Partially compensated at `:288-294` |
| Cancelled-worker tail | [main.py:348-350](../../orchestrator/app/main.py#L348-L350) cancels the previous generation **without awaiting it** | The old worker's `finally` (`:673-677`, including `db.add_message`) can run after the registry entry was already replaced |

### What breaks first

Latency, in the synchronous path. Worst case `prepare` performs **four sequential summarization
completions plus up to five `measure` round trips before the user sees a single token**, each
summarization bounded only by the 300 s client timeout. The only user-visible signal in that window is
one `status "Compacting conversation…"` frame at `:274`.

**Findings on this path:** `PERF-01` (same class — blocking sqlite in async), `PERF-03`, `PERF-04`,
`REL-03`, `OBS-01`, `TEST-02`. Plus dead configuration: `CONTEXT_WARN_THRESHOLD`.

---

## Cross-flow summary

| # | Flow | Hardest bound that exists | Bound that is missing |
|---|---|---|---|
| 1 | Chat SSE | per-LLM-call 300 s | whole-request deadline, body size (`REL-01`), SSE heartbeat |
| 2 | Router | `return "rag"` default | any confidence signal; a timeout of its own |
| 3 | Agent | `MAX_STEPS = 8`, one graph pass, concurrency 3 | wall clock; per-step timeout |
| 4 | NL→SQL | `read_only` + `enable_external_access=False` | statement timeout, `memory_limit`, injected `LIMIT`, guard/executor string parity (`SEC-07`) |
| 5 | RAG | `RAG_TOP_K` 30 → `RAG_FINAL_K` 8 | context-block char cap, reranker admission control, vector index |
| 6 | Sync | watermark written last | Bulk poll deadline, restart policy (`REL-02`), delete propagation |
| 7 | Upload/report | `UPLOAD_MAX_MB`, `ARCHIVE_MAX_*`, `MAX_SECTIONS = 6` | pandoc timeout, report retention, off-loop execution |
| 8 | Context | 0.70 / 0.80 thresholds, 4 adaptive rounds | server-side warn state, lock coverage on the measurement read |
