# Orchestrator — Context Lifecycle & Platform Spine

> **⚠ Superseded in part (2026-08-10).** The app-state layer described below was
> `/data/app.sqlite3` (stdlib `sqlite3`). It is now PostgreSQL — see
> [`data-model.md`](data-model.md) and the CHANGELOG entry
> "App state moved from SQLite to PostgreSQL". Every `sqlite3` reference,
> `db.py` line number and finding about SQLite locking below is a snapshot of
> the pre-migration code and has NOT been re-derived. The DuckDB warehouse and
> LanceDB sections are unaffected and remain accurate.

Sixteen modules: the FastAPI entrypoint, the LangGraph fallback router, the token-budget/compaction/recall
chain, the SSE formatter, and the platform layer (`llm`, `auth`, `db`, `config`, `health`, `uploads`).

| module | LOC | role |
|---|---|---|
| [`main.py`](../../orchestrator/app/main.py) | 796 | FastAPI app, 8 routes, `LiveGeneration` registry, engine dispatch |
| [`graph.py`](../../orchestrator/app/graph.py) | 117 | LangGraph router → 5 engine nodes (salesforce-mode fallback) |
| [`context.py`](../../orchestrator/app/context.py) | 275 | per-call token budgeting against the real served window |
| [`compaction.py`](../../orchestrator/app/compaction.py) | 360 | budget arithmetic + rolling-summary folding (sync + background) |
| [`summarize.py`](../../orchestrator/app/summarize.py) | 116 | incremental summary / condense prompts |
| [`memory.py`](../../orchestrator/app/memory.py) | 35 | in-process per-`session_id` transcript fallback |
| [`memory_recall.py`](../../orchestrator/app/memory_recall.py) | 75 | cross-chat keyword recall |
| [`recall.py`](../../orchestrator/app/recall.py) | 144 | within-chat semantic recall over folded turns |
| [`history.py`](../../orchestrator/app/history.py) | 288 | `/history/*` conversation CRUD + search |
| [`sse.py`](../../orchestrator/app/sse.py) | 85 | the single SSE frame formatter + event allowlist |
| [`llm.py`](../../orchestrator/app/llm.py) | 348 | the only vLLM/OpenAI client layer |
| [`auth.py`](../../orchestrator/app/auth.py) | 103 | collapses all identity to one local account |
| [`db.py`](../../orchestrator/app/db.py) | 1064 | app-state SQLite: users, conversations, messages, summaries, chunks, uploads, urls, repos |
| [`config.py`](../../orchestrator/app/config.py) | 271 | one `Settings` singleton built from `os.environ` at import |
| [`health.py`](../../orchestrator/app/health.py) | 131 | concurrent dependency probes behind `GET /health` |
| [`uploads.py`](../../orchestrator/app/uploads.py) | 172 | `POST /uploads` stream→extract→profile, `GET /uploads/{id}` |

Two facts govern every module below and are not repeated in each block:

1. **Single process, single event loop.** [`orchestrator/Dockerfile:52`](../../orchestrator/Dockerfile#L52)
   runs `uvicorn app.main:app --host 0.0.0.0 --port 8080` with no `--workers`. Every module-level dict
   (`main.py:125,130`, `compaction.py:35,41`, `context.py:66`, `memory.py:35`) is therefore process-global and
   internally consistent — and every blocking call inside an `async def` stalls **all** concurrent SSE streams.
2. **`db.connect()` is not cheap.** [`db.py:195-205`](../../orchestrator/app/db.py#L195) opens a fresh
   `sqlite3.connect`, sets `journal_mode=WAL` + `foreign_keys=ON`, runs `con.executescript(_SCHEMA)`
   (16 `CREATE … IF NOT EXISTS`) and then `migrate(con)` — including an **unconditional** full-scan
   `DELETE FROM messages … NOT IN (SELECT MIN(id) … GROUP BY …)` ([db.py:181-186](../../orchestrator/app/db.py#L181))
   and a `CREATE UNIQUE INDEX IF NOT EXISTS` + `commit()` — on **every** invocation. Measured call sites:
   31 inside `db.py` plus [`auth.py:48`](../../orchestrator/app/auth.py#L48),
   [`health.py:79`](../../orchestrator/app/health.py#L79) and [`main.py:36`](../../orchestrator/app/main.py#L36)
   = **34**.

---

## main.py

**Purpose** — FastAPI entrypoint: mounts the auth/history/uploads routers, owns the detached-generation
registry, and implements `/health`, `/reports*`, `/chat`, `/chat/stop`, `/chat/active`, `/chat/compact`, `/chat/attach/{id}`.

**Public surface**

| Symbol | Signature / route | `file:line` |
|---|---|---|
| `lifespan` | `async ctxmgr lifespan(_app: FastAPI)` | [main.py:27-38](../../orchestrator/app/main.py#L27) |
| `app` | `FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` | [main.py:41](../../orchestrator/app/main.py#L41) |
| CORS middleware | `CORSMiddleware(allow_origins=settings.cors_allow_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])` | [main.py:47-53](../../orchestrator/app/main.py#L47) |
| router mounts | `auth_router` / `history_router` / `uploads_router` | [main.py:57-59](../../orchestrator/app/main.py#L57) |
| `LiveGeneration` | `class` — see design note below | [main.py:62](../../orchestrator/app/main.py#L62) |
| `LiveGeneration.__init__` | `(conversation_id: Optional[str], user_id: Optional[int])` | [main.py:78-93](../../orchestrator/app/main.py#L78) |
| `LiveGeneration.publish` | `async (event: str, data: dict) -> None` | [main.py:95-98](../../orchestrator/app/main.py#L95) |
| `LiveGeneration.finish` | `async () -> None` | [main.py:100-103](../../orchestrator/app/main.py#L100) |
| `LiveGeneration.follow` | `async () -> AsyncIterator[str]` | [main.py:105-120](../../orchestrator/app/main.py#L105) |
| `_live_generations` | `dict` — conv_key → `LiveGeneration` | [main.py:125](../../orchestrator/app/main.py#L125) |
| `_background_tasks` | `set` — strong refs to detached compaction tasks | [main.py:130](../../orchestrator/app/main.py#L130) |
| `_spawn_background_compaction` | `(conv_key: str, history: list, *, base_url: str, model: str) -> None` | [main.py:133-145](../../orchestrator/app/main.py#L133) |
| `_finalize_generation` | `async (conv_key: str, gen: LiveGeneration) -> None` | [main.py:148-168](../../orchestrator/app/main.py#L148) |
| `ChatMessage` | `BaseModel{role: str, content: str = ""}` | [main.py:171-173](../../orchestrator/app/main.py#L171) |
| `ChatRequest` | 13 fields, see below | [main.py:176-239](../../orchestrator/app/main.py#L176) |
| `StopRequest` | `BaseModel{conversation_id: Optional[str], session_id: str = "default"}` | [main.py:688-690](../../orchestrator/app/main.py#L688) |
| `_viewer_id` | `(http_request: Request) -> Optional[int]` | [main.py:693-698](../../orchestrator/app/main.py#L693) |
| `_owns` | `(gen: LiveGeneration, viewer: Optional[int]) -> bool` | [main.py:701-709](../../orchestrator/app/main.py#L701) |
| `CompactRequest` | `BaseModel{conversation_id: str, messages: Optional[List[ChatMessage]]}` | [main.py:740-742](../../orchestrator/app/main.py#L740) |

Routes on the `app` object (the `Depends` column is the only auth-shaped dependency in the codebase):

| Method | Path | Request | Response | Auth | Status codes | `file:line` |
|---|---|---|---|---|---|---|
| GET | `/health` | — | `{status, service, version, checks}` | none | 200 | [main.py:242-254](../../orchestrator/app/main.py#L242) |
| GET | `/reports` | — | `{reports: [...]}` | none | 200 | [main.py:257-259](../../orchestrator/app/main.py#L257) |
| GET | `/reports/{filename}` | path | `FileResponse` | none | 200 / 400 `ReportPathError` / 404 | [main.py:262-271](../../orchestrator/app/main.py#L262) |
| POST | `/chat` | `ChatRequest` | `StreamingResponse` `text/event-stream` | none — `current_user` at [main.py:327](../../orchestrator/app/main.py#L327) never 401s | 200 / 404 / 422 | [main.py:274-685](../../orchestrator/app/main.py#L274) |
| POST | `/chat/stop` | `StopRequest` | `{stopped: bool}` | none (`_viewer_id`) | 200 always | [main.py:712-722](../../orchestrator/app/main.py#L712) |
| GET | `/chat/active` | — | `{active: [str]}` | none (`_viewer_id`) | 200 | [main.py:725-737](../../orchestrator/app/main.py#L725) |
| POST | `/chat/compact` | `CompactRequest` | `{compacted, folded_turns?, covers_through?, reason?}` | none — the 401 at [main.py:756-757](../../orchestrator/app/main.py#L756) is unreachable | 200 / 404 | [main.py:745-779](../../orchestrator/app/main.py#L745) |
| GET | `/chat/attach/{conversation_id}` | path | `StreamingResponse` SSE | none (`_viewer_id`) | 200 / 404 | [main.py:782-796](../../orchestrator/app/main.py#L782) |

`ChatRequest` fields ([main.py:184-199](../../orchestrator/app/main.py#L184)): `messages`, `message`,
`session_id="default"`, `image`, `image_base64`, `conversation_id`, `mode: Literal["salesforce","assistant"]="salesforce"`,
`model: Literal["smart","fast"]="smart"`, `effort: Literal["fast","low","medium","high"]="medium"`,
`agent: bool=False`, `pdf`, `pdf_filename`, `web_search: Literal["off","auto","on"]="off"`. Derived properties
`pdf_data`/`text`/`image_data`/`history_messages` at [main.py:201-231](../../orchestrator/app/main.py#L201);
`@model_validator(mode="after") _require_input` at [main.py:233-239](../../orchestrator/app/main.py#L233) rejects a
request carrying none of message / image / pdf.

### The `LiveGeneration` detached-generation design — a genuine strength

`LiveGeneration` ([main.py:62-120](../../orchestrator/app/main.py#L62)) decouples the model generation from the
HTTP request that started it. The design is coherent and each piece earns its place:

| Mechanism | Implementation | What it buys |
|---|---|---|
| Detached task | `gen.task = asyncio.create_task(worker())` [main.py:679](../../orchestrator/app/main.py#L679); the route returns immediately with `StreamingResponse(gen.follow())` [main.py:681-685](../../orchestrator/app/main.py#L681) | Closing the tab or navigating away does not cancel the model. Cancellation is an explicit act (`POST /chat/stop`, [main.py:712-722](../../orchestrator/app/main.py#L712)). |
| Replayable buffer | every frame appended to `self.events` under `asyncio.Condition` [main.py:95-98](../../orchestrator/app/main.py#L95); `follow()` replays from index 0 then blocks on `cond.wait()` [main.py:105-118](../../orchestrator/app/main.py#L105) | `GET /chat/attach/{id}` [main.py:782-796](../../orchestrator/app/main.py#L782) rebuilds the partial answer instantly after a reload, then streams live. Any number of concurrent readers. |
| Idempotency key | `self.generation_id = uuid.uuid4().hex` [main.py:84](../../orchestrator/app/main.py#L84), merged into every `meta` at [main.py:369](../../orchestrator/app/main.py#L369), enforced by the partial unique index `idx_messages_generation` [db.py:187-191](../../orchestrator/app/db.py#L187) | Two browsers watching one answer persist it exactly once. The race is resolved **in the database**, not in Python — `add_message` turns the `IntegrityError` into a no-op returning the winning row ([db.py:736-742](../../orchestrator/app/db.py#L736)). |
| Server-side rescue persist | `_finalize_generation` writes the answer only when `gen.subscribers == 0` and the run neither cancelled nor failed [main.py:153-168](../../orchestrator/app/main.py#L153) | An answer finishing with nobody attached still lands in history; when a reader **is** attached the frontend persists and the server stays out, so there is no duplicate. |
| Cancellation survivability | `finally: with contextlib.suppress(Exception): await asyncio.shield(_finalize_generation(...))` [main.py:673-677](../../orchestrator/app/main.py#L673) | Bookkeeping completes even while a `CancelledError` is propagating through the task. |
| Newest-send-wins | `previous.task.cancel()` [main.py:348-350](../../orchestrator/app/main.py#L348) | One live generation per conversation key; a new send supersedes the old. |
| Strong task refs | `_background_tasks.add(task)` + `add_done_callback(discard)` [main.py:144-145](../../orchestrator/app/main.py#L144) | Detached compaction tasks cannot be garbage-collected mid-run — the documented `asyncio` footgun, correctly handled. |

Three caveats that do not undo the design but bound it: `self.events` is unbounded
([main.py:85,97](../../orchestrator/app/main.py#L85)) so a long answer retains every token frame for the
generation's life; `follow()`'s `subscribers` counter is decremented in a `finally`
([main.py:119-120](../../orchestrator/app/main.py#L119)) so an abandoned async generator whose `aclose()` is
deferred keeps `subscribers > 0` and suppresses the rescue persist; and `previous.task.cancel()`
([main.py:350](../../orchestrator/app/main.py#L350)) is not awaited, so the old worker's `finally` — including
`db.add_message` — can run *after* the registry entry has already been replaced.

**Control flow** — `POST /chat` ([main.py:274-685](../../orchestrator/app/main.py#L274))

1. `text = request.text or "Analyze the attached image."` — [main.py:293](../../orchestrator/app/main.py#L293).
2. Build the `meta_extras(route)` closure ([main.py:295-318](../../orchestrator/app/main.py#L295)): `vision` →
   `settings.vision_model`, no `effort`; `agent` → smart id + request effort; `sql`/`rag`/`report` → smart id +
   hard-coded `effort="medium"` ([main.py:314](../../orchestrator/app/main.py#L314)); everything else →
   `llm.served_model_id(request.model)` + request effort.
3. `signed_in = current_user(http_request)` — [main.py:327](../../orchestrator/app/main.py#L327). Always the
   single local row.
4. `conv_key_outer = request.conversation_id or request.session_id` — [main.py:329](../../orchestrator/app/main.py#L329).
5. Ownership gate: `db.conversation_owner(conv_key_outer)` inside `try/except Exception: conv_owner = None`
   ([main.py:338-341](../../orchestrator/app/main.py#L338)), then 404 if `conv_owner is not None and conv_owner != viewer`
   ([main.py:343-344](../../orchestrator/app/main.py#L343)).
6. Pre-empt any in-flight generation on the same key ([main.py:348-350](../../orchestrator/app/main.py#L348)),
   construct `LiveGeneration`, register it ([main.py:352-356](../../orchestrator/app/main.py#L352)).
7. Declare `context_state` / `orchestration_state` ([main.py:360,362](../../orchestrator/app/main.py#L360)) and
   the `emit()` closure ([main.py:364-381](../../orchestrator/app/main.py#L364)) — on `meta` it merges
   `meta_extras`, `generation_id`, `context.get_trim_notice()` → `input_trimmed`, `context_state` → `context`,
   `orchestration_state` → `auto`, and stores `gen.final_meta`.
8. Define `worker()` ([main.py:383-677](../../orchestrator/app/main.py#L383)):
   1. `context.reset_trim_notice()` — [main.py:385](../../orchestrator/app/main.py#L385).
   2. `history = request.history_messages or memory.history(request.session_id)` — [main.py:387](../../orchestrator/app/main.py#L387).
   3. `auto_web_search_allowed = request.mode == "assistant"` — [main.py:401](../../orchestrator/app/main.py#L401).
   4. Auto-orchestration when text and no attachment and not `request.agent`: `orchestrate.decide(...)` +
      `emit("status", …)` — [main.py:403-415](../../orchestrator/app/main.py#L403).
   5. `want_agent = request.agent or auto_plan.agent`; `orchestration_state` filled —
      [main.py:417-421](../../orchestrator/app/main.py#L417).
   6. Web-search gate — [main.py:423-449](../../orchestrator/app/main.py#L423). See `orchestrator-search.md`.
   7. Cross-chat recall block prepended as a `system` message — [main.py:450-455](../../orchestrator/app/main.py#L450).
   8. `conv_key = request.conversation_id or request.session_id` — [main.py:457](../../orchestrator/app/main.py#L457).
   9. Phase 3 GitHub detection ([main.py:461-473](../../orchestrator/app/main.py#L461)); Phase 2 URL extraction
      and stored-page injection ([main.py:479-510](../../orchestrator/app/main.py#L479)); Phase 4 dataset
      readiness ([main.py:517-529](../../orchestrator/app/main.py#L517)).
   10. `full_history = list(history)` — [main.py:534](../../orchestrator/app/main.py#L534). **Context assembly**
       when `signed_in and request.conversation_id`: `llm.resolve_model_choice(request.model)` →
       `recall.retrieve_block(conv_key, text)` → `compaction.prepare(...)` → `context_state.update(info)` —
       [main.py:535-549](../../orchestrator/app/main.py#L535). Note `requested_max_tokens` is **not** passed.
   11. Engine dispatch, first match wins ([main.py:551-644](../../orchestrator/app/main.py#L551)): pdf → vision →
       repo → url → agent → search → dataset → assistant-chat → `get_graph().ainvoke(...)`.
   12. `gen.answer = answer`; `memory.add_exchange(...)`; `gen.publish("done", …)` —
       [main.py:645-647](../../orchestrator/app/main.py#L645).
   13. Background compaction spawned with `[*full_history, user, assistant]` —
       [main.py:654-667](../../orchestrator/app/main.py#L654).
   14. `except asyncio.CancelledError: gen.cancelled = True` ([main.py:668-669](../../orchestrator/app/main.py#L668));
       `except Exception:` → `gen.failed = True` + `publish("error", {"message": str(exc)})`
       ([main.py:670-672](../../orchestrator/app/main.py#L670)).
9. `gen.task = asyncio.create_task(worker())` ([main.py:679](../../orchestrator/app/main.py#L679)); return the
   `StreamingResponse` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`
   ([main.py:681-685](../../orchestrator/app/main.py#L681)).

The context-lifecycle ordering is worth stating plainly, because it is the spine of the whole service:

```
history (client-sent) → cross-chat recall block → stored URL pages
      → recall.retrieve_block (semantic, within-chat)
      → compaction.prepare (measure → maybe fold → assemble)
      → engine → llm.stream_chat_events → context.fit_request (final trim/clip)
      → answer → memory.add_exchange → background compaction (detached)
```

**State & side effects**

- Global mutation: `_live_generations` ([main.py:125](../../orchestrator/app/main.py#L125), written
  [:356](../../orchestrator/app/main.py#L356), popped [:151-152](../../orchestrator/app/main.py#L151));
  `_background_tasks` ([:130,144-145](../../orchestrator/app/main.py#L130)); `memory._sessions` via
  `memory.add_exchange` ([:646](../../orchestrator/app/main.py#L646)); the `context._trim_notice` ContextVar
  ([:385](../../orchestrator/app/main.py#L385)); `compaction._locks` / `_pending_notice` indirectly.
- DB writes: `db.add_message` ([main.py:162-168](../../orchestrator/app/main.py#L162)) — the only write from this
  module. DB reads: `db.conversation_owner` ([:339](../../orchestrator/app/main.py#L339)), `db.get_repo_keys`
  ([:471](../../orchestrator/app/main.py#L471)), `db.get_url_documents` ([:493](../../orchestrator/app/main.py#L493)),
  `db.get_uploads` ([:527](../../orchestrator/app/main.py#L527)), `db.list_messages` ([:770](../../orchestrator/app/main.py#L770)).
- Filesystem: `lifespan` creates `/data/app.sqlite3` + parents via `db.connect()`
  ([main.py:36-37](../../orchestrator/app/main.py#L36)); `FileResponse` reads from `settings.reports_dir`
  ([main.py:271](../../orchestrator/app/main.py#L271)).
- Network egress: none directly. All egress is via `llm.*`, `context.count_tokens` (`/tokenize`),
  `health.check_dependencies`, and the search/url/repo engines.
- GPU/model calls: `orchestrate.decide` ([:412](../../orchestrator/app/main.py#L412)), `search.should_search`
  ([:449](../../orchestrator/app/main.py#L449)), every engine at [:551-643](../../orchestrator/app/main.py#L551),
  `compaction.prepare` → `summarize`, `recall.retrieve_block` → `llm.embed_texts`.

**Dependencies** — Inbound: tests only (`tests/test_history.py:7`, `test_chat_modes.py:22`,
`test_endpoints.py:14`, `test_live_generation.py:14,192`, `test_conversation_integrity.py:18`,
`test_history_search.py:12`, `test_salesforce_toggle.py:18,66`, `test_history_v3.py:14`, `test_auth.py:19`,
`test_context_budget.py:329`); in production it is the uvicorn entrypoint.
Outbound: `context`/`db`/`llm` ([main.py:16](../../orchestrator/app/main.py#L16)), `auth.router` (:17),
`config.settings` (:18), `core.report_paths` (:19), `graph.get_graph` (:20), `health.check_dependencies` (:21),
`history.router` (:22), `uploads.router` (:23), `memory.memory` (:24), `sse.sse_event` (:25); lazily
`compaction` (:137, :536, :655, :752), `auth.current_user` (:324, :695, :753), `memory_recall.recall_block` (:325),
`engines.orchestrate` (:410), `engines.search` (:435, :604), `core.repo` (:464), `core.urls` (:488),
`recall` (:536), and the seven engine modules at :553-622.

**Config** — `settings.cors_allow_origins` (:49), `settings.reports_dir` (:259, :265), `settings.vision_model`
(:308), `settings.search_enabled` (:425), `settings.repo_analysis_enabled` (:463), `settings.url_analysis_enabled`
(:481), `settings.url_max_pages` (:490), `settings.dataset_uploads_enabled` (:519).

**Failure modes**

- `try/except Exception: conv_owner = None` ([main.py:338-341](../../orchestrator/app/main.py#L338)) **fails
  open**; the comment immediately above ([:336-337](../../orchestrator/app/main.py#L336)) asserts the opposite
  ("If the DB is unreachable this raises").
- `contextlib.suppress(Exception)` around `db.add_message` ([:161](../../orchestrator/app/main.py#L161)) and
  around `asyncio.shield(_finalize_generation(...))` ([:676](../../orchestrator/app/main.py#L676)) — a failed
  persist is silent, with no logging.
- Silent degradations: `repo_followup = False` ([:472-473](../../orchestrator/app/main.py#L472)),
  `stored = []` ([:494-495](../../orchestrator/app/main.py#L494)), `dataset_ready = False`
  ([:528-529](../../orchestrator/app/main.py#L528)).
- The catch-all at [:670](../../orchestrator/app/main.py#L670) sends `str(exc)` verbatim to the client
  ([:672](../../orchestrator/app/main.py#L672)) — raw exception text, no sanitisation, no logging.
- No timeout bounds `worker()` as a whole; only `settings.llm_request_timeout` (300 s,
  [config.py:264](../../orchestrator/app/config.py#L264)) applies per LLM call. No retry anywhere in the module.
- No request body limit: [main.py:47-53](../../orchestrator/app/main.py#L47) installs only `CORSMiddleware`, and
  `ChatRequest.image` / `.pdf` / `.messages` are unbounded ([main.py:184-199](../../orchestrator/app/main.py#L184)).
- `lifespan` ([main.py:27-38](../../orchestrator/app/main.py#L27)) has **no shutdown branch**:
  `_live_generations` and `_background_tasks` are never drained or cancelled on SIGTERM.

**Concurrency**

- Async throughout; the generation is a detached task deliberately outliving the HTTP request.
- Blocking `sqlite3` calls executed directly on the event loop: `db.conversation_owner` (:339),
  `db.get_repo_keys` (:471), `db.get_url_documents` (:493), `db.get_uploads` (:527),
  `memory_recall.recall_block` (:451), `db.list_messages` (:770), `db.add_message` (:162). None use
  `asyncio.to_thread` — contrast [health.py:122-123](../../orchestrator/app/health.py#L122), which does. Each
  also pays the full `connect()` schema+migrate cost.
- Shared mutable module state: `_live_generations`, `_background_tasks`.
- `LiveGeneration.cond` is created inside `__init__` ([main.py:87](../../orchestrator/app/main.py#L87)) on the
  running loop — loop-binding is safe.
- Race window: `previous.task.cancel()` ([:350](../../orchestrator/app/main.py#L350)) is not awaited, so the
  superseded worker's `finally` can persist after the registry entry is replaced.

**Complexity hotspots**

| Function | LOC | `file:line` |
|---|---|---|
| `chat()` | **412** (274-685), two nested closures, 9-branch dispatch chain at :551-643, 6-term search conjunction at :424-434 — cyclomatic well above 10 | [main.py:274](../../orchestrator/app/main.py#L274) |
| `worker()` | **295** (383-677) | [main.py:383](../../orchestrator/app/main.py#L383) |
| `meta_extras()` | 24, 4-way branch | [main.py:295](../../orchestrator/app/main.py#L295) |

**Findings** — `SEC-01`, `SEC-02`, `SEC-05`, `REL-01`, `REL-03`, `PERF-03`, `OBS-01`, `TEST-02`.

---

## graph.py

**Purpose** — LangGraph wiring for the salesforce-mode fallback path: one router node fanning out to five engine
nodes, all lazily imported.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `Emit` | `Callable[[str, dict], Awaitable[None]]` | [graph.py:13](../../orchestrator/app/graph.py#L13) |
| `ChatState` | `TypedDict(total=False){message, session_id, image_base64, history, route, answer, emit, model_choice, effort}` | [graph.py:16-26](../../orchestrator/app/graph.py#L16) |
| `_router_node` | `async (state) -> {"route": str}` | [graph.py:29-41](../../orchestrator/app/graph.py#L29) |
| `_sql_node` / `_rag_node` / `_vision_node` / `_report_node` / `_chat_node` | `async (state) -> dict` | [graph.py:44-48](../../orchestrator/app/graph.py#L44), [:51-55](../../orchestrator/app/graph.py#L51), [:58-64](../../orchestrator/app/graph.py#L58), [:67-71](../../orchestrator/app/graph.py#L67), [:74-87](../../orchestrator/app/graph.py#L74) |
| `build_graph` | `() -> CompiledGraph` | [graph.py:90-107](../../orchestrator/app/graph.py#L90) |
| `_compiled` | module global, `None` initially | [graph.py:110](../../orchestrator/app/graph.py#L110) |
| `get_graph` | `() -> CompiledGraph` | [graph.py:113-117](../../orchestrator/app/graph.py#L113) |

**Control flow**

1. `get_graph()` lazily calls `build_graph()` and caches it in `_compiled` ([graph.py:113-117](../../orchestrator/app/graph.py#L113)).
2. `build_graph()` registers 6 nodes ([:92-97](../../orchestrator/app/graph.py#L92)) with entry point `router` ([:99](../../orchestrator/app/graph.py#L99)).
3. A conditional edge keyed on `state["route"]` maps exactly `{"sql","rag","vision","report","chat"}`
   ([:100-104](../../orchestrator/app/graph.py#L100)); every engine node then goes to `END` ([:105-106](../../orchestrator/app/graph.py#L105)).
4. `_router_node` calls `engines.router.route_request(message, bool(image_base64), history)`
   ([:36-40](../../orchestrator/app/graph.py#L36)); that function returns a validated route or the `"rag"`
   fallback ([engines/router.py:103-133](../../orchestrator/app/engines/router.py#L103)), so the conditional map
   cannot `KeyError`.
5. `_chat_node` passes `model_choice`/`effort` through with defaults `"smart"`/`"medium"`
   ([:84-85](../../orchestrator/app/graph.py#L84)).

**State & side effects** — None of its own; all writes and egress happen inside the engines. `_compiled`
([graph.py:110,116](../../orchestrator/app/graph.py#L110)) is the only module-level mutation.

**Dependencies** — Inbound: [main.py:20,633](../../orchestrator/app/main.py#L20); `tests/test_imports.py:33-35`;
`tests/test_salesforce_toggle.py:66` monkeypatches `app.main.get_graph`. Outbound: `langgraph.graph`
([graph.py:11](../../orchestrator/app/graph.py#L11)); lazily `engines.router` (:30), `engines.sql` (:45),
`engines.rag` (:52), `engines.vision` (:59), `engines.report` (:68), `engines.chat` (:77).

**Config** — None. This module reads no environment and no `settings` attribute.

**Failure modes** — `state["emit"]` and `state["message"]` are accessed with `[]`
([graph.py:47,54,62,70,80-81](../../orchestrator/app/graph.py#L47)); a missing key raises `KeyError`, which
surfaces to the client as the terminal `error` event. `_vision_node` forwards `state.get("image_base64")`, which
may be `None` ([:62](../../orchestrator/app/graph.py#L62)). No timeouts, no retries.

**Concurrency** — Async nodes. `get_graph()` performs a non-atomic check-then-set on `_compiled`
([:115-116](../../orchestrator/app/graph.py#L115)); harmless because there is no `await` between the check and
the assignment on a single-threaded loop.

**Complexity hotspots** — None. The largest function, `build_graph`, is 18 LOC.

**Findings** — `QUAL-01` (`Emit` re-declared at [graph.py:13](../../orchestrator/app/graph.py#L13); no engine
ABC/Protocol exists, so the five node wrappers each hand-marshal state).

---

## context.py

**Purpose** — Per-request token budgeting against the *serving* model's real window, learned from vLLM's
`POST /tokenize`, plus the trim/clip machinery that makes an oversized prompt sendable.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_trim_notice` | `ContextVar[Optional[dict]]` | [context.py:28](../../orchestrator/app/context.py#L28) |
| `reset_trim_notice` / `get_trim_notice` | `() -> None` / `() -> Optional[dict]` | [context.py:31-36](../../orchestrator/app/context.py#L31) |
| `_record_trim` | `(dropped_turns: int, clipped_messages: int) -> None` | [context.py:39-48](../../orchestrator/app/context.py#L39) |
| `MIN_OUTPUT_TOKENS` / `_CHARS_PER_TOKEN` / `_MAX_FIT_ROUNDS` / `_MIN_CLIPPED_CHARS` | `256` / `3.0` / `24` / `2000` | [context.py:51](../../orchestrator/app/context.py#L51), [:55](../../orchestrator/app/context.py#L55), [:60](../../orchestrator/app/context.py#L60), [:63](../../orchestrator/app/context.py#L63) |
| `_window_cache` / `_lock` | `dict` base_url → max_model_len / `asyncio.Lock()` at import | [context.py:66-67](../../orchestrator/app/context.py#L66) |
| `service_root` | `(base_url: str) -> str` | [context.py:70-73](../../orchestrator/app/context.py#L70) |
| `estimate_tokens` / `estimate_messages` | `(text) -> int` / `(messages) -> int` | [context.py:76-77](../../orchestrator/app/context.py#L76), [:80-94](../../orchestrator/app/context.py#L80) |
| `count_tokens` | `async (base_url, model, messages) -> Tuple[int, Optional[int]]` | [context.py:97-123](../../orchestrator/app/context.py#L97) |
| `model_window` | `async (base_url, model) -> int` | [context.py:126-138](../../orchestrator/app/context.py#L126) |
| `_split_pinned` / `trim_to_fit` | `(messages) -> Tuple[list, list]` / `(messages, drop) -> list` | [context.py:141-152](../../orchestrator/app/context.py#L141), [:155-161](../../orchestrator/app/context.py#L155) |
| `clip_middle` / `_longest_content_index` / `clip_message_contents` | see source | [context.py:164-181](../../orchestrator/app/context.py#L164), [:184-190](../../orchestrator/app/context.py#L184), [:193-202](../../orchestrator/app/context.py#L193) |
| `fit_request` | `async (messages, *, base_url, model, requested_max_tokens=None) -> Tuple[List[dict], int]` | [context.py:205-275](../../orchestrator/app/context.py#L205) |

**Control flow** — `fit_request`

1. `window = await model_window(base_url, model)` ([:218](../../orchestrator/app/context.py#L218));
   `margin = settings.context_safety_margin` (:219); `ceiling = requested_max_tokens or settings.model_max_output` (:220).
2. `prompt_tokens, served_window = await count_tokens(...)`; a returned `served_window` overrides `window`
   ([:223-225](../../orchestrator/app/context.py#L223)).
3. Loop `for _ in range(_MAX_FIT_ROUNDS)` ([:229](../../orchestrator/app/context.py#L229)):
   `budget = window − prompt_tokens − margin`; break once `budget >= MIN_OUTPUT_TOKENS`
   ([:230-232](../../orchestrator/app/context.py#L230)).
4. Prefer dropping one oldest trimmable turn via `trim_to_fit(msgs, 1)` ([:235-238](../../orchestrator/app/context.py#L235)).
5. If nothing could be dropped, clip the longest message in place with `clip_middle` down to
   `max(len − shed, _MIN_CLIPPED_CHARS)` ([:241-255](../../orchestrator/app/context.py#L241)).
6. Re-count tokens each round — **one HTTP round-trip per round** ([:257](../../orchestrator/app/context.py#L257)).
7. Record and log the trim ([:259-270](../../orchestrator/app/context.py#L259)), then
   `max_tokens = max(1, min(ceiling, budget))` ([:272-274](../../orchestrator/app/context.py#L272)).

`model_window`: cache hit ([:128-130](../../orchestrator/app/context.py#L128)); otherwise double-checked under
`_lock` ([:131-133](../../orchestrator/app/context.py#L131)), probe with a 1-token message (:135),
`resolved = window or settings.model_max_context` (:136), and **cache the fallback permanently** (:137).

**State & side effects**

- Network egress: `POST {service_root(base_url)}/tokenize` ([context.py:110-112](../../orchestrator/app/context.py#L110)),
  targeting the vLLM services. Timeout `settings.tokenize_timeout` = 5.0 s
  ([config.py:135](../../orchestrator/app/config.py#L135)). A fresh `httpx.AsyncClient` is created **per call**
  ([:109](../../orchestrator/app/context.py#L109)) — no connection reuse.
- Global mutation: `_window_cache` ([:119,137](../../orchestrator/app/context.py#L119)); the ContextVar
  `_trim_notice` ([:32,44](../../orchestrator/app/context.py#L32)).
- Logging: `logging.getLogger(__name__).warning` on trim ([:261-270](../../orchestrator/app/context.py#L261)).
- No DB, no filesystem, no GPU inference of its own.

**Dependencies** — Inbound: [llm.py:24,26](../../orchestrator/app/llm.py#L24) (`fit_request` at
`llm.py:101,128,229,284`; `clip_message_contents` at `llm.py:286`);
[compaction.py:28](../../orchestrator/app/compaction.py#L28) (`model_window` :108, `count_tokens` :109,
`estimate_tokens` :187); [main.py:16](../../orchestrator/app/main.py#L16) (`reset_trim_notice` :385,
`get_trim_notice` :373); `tests/test_context_budget.py:13`, `tests/test_compaction.py:16`.
Outbound: `asyncio`, `contextvars`, `config.settings`; lazily `httpx` (:105) and `logging` (:261).

**Config** — `settings.tokenize_timeout` (:109), `settings.context_safety_margin` (:219),
`settings.model_max_output` (:220), `settings.model_max_context` (:136).

**Failure modes**

- `count_tokens` catches **all** exceptions ([:121-123](../../orchestrator/app/context.py#L121)) and returns
  `(estimate_messages(messages), _window_cache.get(base_url))`. `estimate_messages` counts only text parts of
  multimodal content ([:86-92](../../orchestrator/app/context.py#L86)), so an image payload is counted as ~0 tokens.
- `model_window` caches `settings.model_max_context` (262144 by default,
  [config.py:127](../../orchestrator/app/config.py#L127)) when the probe fails
  ([:136-137](../../orchestrator/app/context.py#L136)). Later successful `count_tokens` calls overwrite it
  ([:118-119](../../orchestrator/app/context.py#L118)), but while `/tokenize` is unreachable every budget
  computation uses the wrong window.
- `fit_request` can `break` with `budget < MIN_OUTPUT_TOKENS` at [:245](../../orchestrator/app/context.py#L245)
  (nothing clippable) and [:252](../../orchestrator/app/context.py#L252) (already at `_MIN_CLIPPED_CHARS`);
  `max_tokens` then floors at 1 ([:274](../../orchestrator/app/context.py#L274)).
- No retry on `/tokenize`.

**Concurrency** — Async. `_lock` is constructed at **import time**
([context.py:67](../../orchestrator/app/context.py#L67)); safe on Python ≥3.10, and the runtime image is
`nvcr.io/nvidia/vllm:26.05-py3` ([orchestrator/Dockerfile:14](../../orchestrator/Dockerfile#L14)).
`_window_cache` is read/written without the lock in `count_tokens` (:119) and in `model_window`'s fast path
(:128) — benign last-writer-wins. `_trim_notice` is a ContextVar, so each `asyncio.create_task` worker
([main.py:679](../../orchestrator/app/main.py#L679)) gets its own copy.

**Complexity hotspots** — `fit_request` ([context.py:205](../../orchestrator/app/context.py#L205)) is **71 LOC**
with a nested branch inside a bounded loop; the only function in the file over 60 LOC.

**Findings** — `REL-03` (`count_tokens` swallows every exception and silently substitutes a 3-chars-per-token
estimate; an image payload then counts as ~0 tokens), `OBS-01`.

---

## compaction.py

**Purpose** — Budget arithmetic plus the two-path (background / synchronous) rolling-summary compaction that keeps
a conversation inside the serving model's window.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `Emit` | `Callable[[str, dict], Awaitable[None]]` | [compaction.py:31](../../orchestrator/app/compaction.py#L31) |
| `_locks` / `_pending_notice` | `Dict[str, asyncio.Lock]` / `Dict[str, dict]` | [compaction.py:35](../../orchestrator/app/compaction.py#L35), [:41](../../orchestrator/app/compaction.py#L41) |
| `take_pending_notice` / `_lock_for` | `(conversation_id) -> Optional[dict]` / `-> asyncio.Lock` | [compaction.py:44-45](../../orchestrator/app/compaction.py#L44), [:48-53](../../orchestrator/app/compaction.py#L48) |
| `Budget` | `@dataclass{window, output_reserved, usable, used, breakdown}` + `fraction` property | [compaction.py:56-68](../../orchestrator/app/compaction.py#L56) |
| `usable_budget` / `output_reservation` | `(window, output_reserved) -> int` / `(requested, window=None) -> int` | [compaction.py:71-78](../../orchestrator/app/compaction.py#L71), [:81-97](../../orchestrator/app/compaction.py#L81) |
| `measure` | `async (messages, *, base_url, model, requested_max_tokens=None) -> Budget` | [compaction.py:100-118](../../orchestrator/app/compaction.py#L100) |
| `split_history` | `(history) -> Tuple[List[dict], List[dict]]` | [compaction.py:121-125](../../orchestrator/app/compaction.py#L121) |
| `MIN_KEEP_RECENT` / `_MAX_ADAPTIVE_ROUNDS` | `2` / `4` | [compaction.py:130](../../orchestrator/app/compaction.py#L130), [:132](../../orchestrator/app/compaction.py#L132) |
| `fold_boundary` / `assemble` | `(turn_count, covers_through, keep=None) -> int` / `(history, summary, covers_through, retrieved=None) -> List[dict]` | [compaction.py:135-146](../../orchestrator/app/compaction.py#L135), [:149-167](../../orchestrator/app/compaction.py#L149) |
| `_fold` | `async (conversation_id, turns, covers_through, new_boundary, existing) -> Optional[dict]` | [compaction.py:170-201](../../orchestrator/app/compaction.py#L170) |
| `compact` | `async (conversation_id, history, *, force=False, keep=None) -> Optional[dict]` | [compaction.py:204-239](../../orchestrator/app/compaction.py#L204) |
| `prepare` | `async (conversation_id, history, current_text, *, base_url, model, requested_max_tokens=None, emit=None, retrieved=None) -> Tuple[List[dict], dict]` | [compaction.py:242-327](../../orchestrator/app/compaction.py#L242) |
| `maybe_background_compact` | `async (conversation_id, history, *, base_url, model, requested_max_tokens=None) -> Optional[dict]` | [compaction.py:330-360](../../orchestrator/app/compaction.py#L330) |

**Control flow** — `prepare` (synchronous path)

1. `row = db.get_summary(conversation_id)` ([:258](../../orchestrator/app/compaction.py#L258)) — blocking sqlite,
   **outside the lock**.
2. `candidate = assemble(history, summary, covers, retrieved)` (:262);
   `probe = candidate + [{"role":"user", …}]` (:263).
3. `budget = await measure(probe, …)` (:264-269) → `context.model_window` + `context.count_tokens`
   ([:108-109](../../orchestrator/app/compaction.py#L108)): up to 2 HTTP calls.
4. If `budget.fraction > settings.context_compact_threshold` (:272): emit `status`
   `"Compacting conversation…"` (:274) and enter the adaptive loop (:280-307) — up to 4 iterations of
   `compact()` → `assemble` → `measure`, halving `keep` (:307) until `keep <= MIN_KEEP_RECENT` (:305).
5. When `compact()` returns `None`, re-read `db.get_summary` to detect a concurrent background fold
   ([:288-294](../../orchestrator/app/compaction.py#L288)).
6. Build `info` (:309-318): `tokens_used`, `usable_budget`, `window`, `reserved_output`, `fraction`,
   `summarized_turns`; add `compacted{folded_turns, background}` from this run (:320) or from
   `take_pending_notice` (:322-326).

`compact`: acquire `_lock_for(conversation_id)` (:217) → `split_history` (:219) → `db.get_summary` (:220) →
`covers = min(stored, len(turns))` (:222) → `boundary = len(turns) if force else fold_boundary(...)` (:223-225),
clamped so the in-flight turn is never folded via `min(boundary, len(turns) − 1)` (:227) → `if boundary <= covers:
return None` (:228) → `_fold(...)` (:230), which calls `summarize.summarize` (:181), optionally
`summarize.condense` (:182-185), `db.save_summary` (:186-188) and `recall.index_folded` (:190-196).

`maybe_background_compact`: `db.get_summary` (:341) → `assemble` (:343) → `measure` (:344-349) → return `None`
if `fraction <= settings.context_bg_compact_threshold` (:350) → `compact()` (:352) → park `_pending_notice`
(:353-357). The whole body sits inside `try/except Exception: return None` (:339, :359-360) with **no logging**.

Three thresholds drive the whole lifecycle:

| Threshold | Default | Defined | Read |
|---|---|---|---|
| `context_warn_threshold` | 0.60 | [config.py:146](../../orchestrator/app/config.py#L146) | **nowhere** — dead configuration, also shipped in `.env.example` |
| `context_bg_compact_threshold` | 0.70 | [config.py:148-150](../../orchestrator/app/config.py#L148) | [compaction.py:350](../../orchestrator/app/compaction.py#L350) |
| `context_compact_threshold` | 0.80 | [config.py:152-154](../../orchestrator/app/config.py#L152) | [compaction.py:272,303](../../orchestrator/app/compaction.py#L272) |

**State & side effects**

- DB writes: `db.save_summary` ([:186](../../orchestrator/app/compaction.py#L186)); indirectly
  `db.add_conversation_chunks` via `recall.index_folded` ([:194](../../orchestrator/app/compaction.py#L194)).
- DB reads: `db.get_summary` at :220, :258, :291, :341 — all blocking sqlite on the event loop.
- Network egress / GPU: `context.count_tokens` (`/tokenize`); `summarize.summarize` / `condense` →
  `llm.chat_completion` → `settings.openai_base_url`; embeddings via `recall.index_folded` → `llm.embed_texts`.
- Global mutation: `_locks` (:52), `_pending_notice` (:45, :354). Neither is ever evicted apart from
  `take_pending_notice`.

**Dependencies** — Inbound: [main.py:137](../../orchestrator/app/main.py#L137) (`maybe_background_compact`),
[main.py:536,540](../../orchestrator/app/main.py#L536) (`prepare`), [main.py:655](../../orchestrator/app/main.py#L655),
[main.py:752,772](../../orchestrator/app/main.py#L752) (`compact(force=True)`); `tests/test_compaction.py:16`.
Outbound: `context`, `db`, `summarize` ([:28](../../orchestrator/app/compaction.py#L28)), `config.settings` (:29);
lazily `recall` (:192) and `logging` (:232).

**Config** — `settings.context_safety_margin` (:78), `settings.model_max_output` (:93),
`settings.min_output_floor` (:94, :96), `settings.keep_recent_turns` (:144, :279),
`settings.summary_max_tokens` (:182), `settings.context_compact_threshold` (:272, :303),
`settings.context_bg_compact_threshold` (:350).

**Failure modes**

- `compact()` catches all exceptions and logs a warning ([:231-239](../../orchestrator/app/compaction.py#L231)) →
  returns `None`; the chat continues on an uncompacted prompt.
- `_fold`'s `recall.index_folded` failure is swallowed by a bare `except Exception: pass`
  ([:195-196](../../orchestrator/app/compaction.py#L195)) — semantic recall can be permanently broken with **zero** signal.
- `maybe_background_compact` catches everything and returns `None` with no log
  ([:359-360](../../orchestrator/app/compaction.py#L359)).
- No timeout on the summarization call beyond `settings.llm_request_timeout` (300 s). Worst case, `prepare`
  performs 4 sequential summarizations plus up to 5 `measure` calls before the user sees a token.
- `_locks` and `_pending_notice` grow without bound.

**Concurrency**

- `_lock_for` ([:48-53](../../orchestrator/app/compaction.py#L48)) is a check-then-create with no `await` between
  the two, so it cannot double-create on a single-threaded loop.
- `prepare`'s summary read at [:258](../../orchestrator/app/compaction.py#L258) is **outside** the lock, so it can
  observe a stale summary while a detached background compaction for the previous turn is inside `_lock_for` about
  to `save_summary`. The module docstring ([:19-20](../../orchestrator/app/compaction.py#L19)) claims the two
  "cannot double-fold or race each other" — true of the *fold* (`compact()` re-reads under the lock,
  [:220-222](../../orchestrator/app/compaction.py#L220)), not of the *measurement*, which the code partially
  compensates for at [:288-294](../../orchestrator/app/compaction.py#L288).
- Blocking sqlite inside `async def`: `db.get_summary` (:220, :258, :291, :341), `db.save_summary` (:186).
- Definition drift: `split_history` ([:121-125](../../orchestrator/app/compaction.py#L121)) keeps system messages
  in original order while `context._split_pinned` ([context.py:141-152](../../orchestrator/app/context.py#L141))
  treats only the **leading** run of system messages as pinned — two definitions of "pinned" in one request path.

**Complexity hotspots** — `prepare` ([compaction.py:242](../../orchestrator/app/compaction.py#L242)) is
**86 LOC** (242-327) with a nested adaptive loop containing four `await` points and two early breaks;
cyclomatic > 10.

**Findings** — `REL-03` (bare `except Exception: pass` at [:195-196](../../orchestrator/app/compaction.py#L195);
unlogged catch-all at [:359-360](../../orchestrator/app/compaction.py#L359)), `PERF-03`.

---

## summarize.py

**Purpose** — Incremental rolling-summary prompts: previous summary + newly folded turns → new summary, plus a
condense pass when the summary approaches its own cap.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_SYSTEM` / `_INSTRUCTIONS` | prompt constants ("Max ~1500 words") | [summarize.py:17-20](../../orchestrator/app/summarize.py#L17), [:22-31](../../orchestrator/app/summarize.py#L22) |
| `_MAX_TURN_CHARS` | `4000` | [summarize.py:35](../../orchestrator/app/summarize.py#L35) |
| `format_turns` | `(turns: Sequence[dict]) -> str` | [summarize.py:38-50](../../orchestrator/app/summarize.py#L38) |
| `build_messages` | `(existing: str, turns) -> List[dict]` | [summarize.py:53-63](../../orchestrator/app/summarize.py#L53) |
| `summarize` | `async (existing: str, turns) -> str` | [summarize.py:66-79](../../orchestrator/app/summarize.py#L66) |
| `condense` | `async (summary: str) -> str` | [summarize.py:82-105](../../orchestrator/app/summarize.py#L82) |
| `SUMMARY_HEADER` / `summary_block` | constant / `(summary: str) -> dict` | [summarize.py:108-111](../../orchestrator/app/summarize.py#L108), [:114-116](../../orchestrator/app/summarize.py#L114) |

**Control flow**

1. `format_turns` skips non-string / blank contents and truncates each turn to `_MAX_TURN_CHARS`
   ([:44-49](../../orchestrator/app/summarize.py#L44)).
2. `summarize` short-circuits and returns `existing` when the formatted transcript is blank
   ([:72-73](../../orchestrator/app/summarize.py#L72)).
3. `llm.chat_completion(build_messages(...), temperature=0.0, max_tokens=settings.summary_max_tokens)`
   ([:74-78](../../orchestrator/app/summarize.py#L74)) — the main model on `settings.openai_base_url`.
4. Falls back to `existing` if the model returns empty ([:79](../../orchestrator/app/summarize.py#L79)).
5. `condense` runs a second completion at the same cap ([:88-104](../../orchestrator/app/summarize.py#L88)) and
   falls back to `summary` (:105).

**State & side effects** — Network/GPU only: up to two `llm.chat_completion` calls per fold, both to
`settings.openai_base_url`. No DB, no filesystem, no globals, no direct env reads.

**Dependencies** — Inbound: [compaction.py:28](../../orchestrator/app/compaction.py#L28) (`summarize` :181,
`condense` :184, `summary_block` :163); `tests/test_compaction.py:16`. Outbound: `llm`
([summarize.py:14](../../orchestrator/app/summarize.py#L14)), `config.settings` (:15).

**Config** — `settings.summary_max_tokens` ([:77](../../orchestrator/app/summarize.py#L77),
[:103](../../orchestrator/app/summarize.py#L103)).

**Failure modes** — Nothing is caught here. The docstring at
[:69-71](../../orchestrator/app/summarize.py#L69) states it raises on model failure; the caller
`compaction._fold` → `compact` catches it ([compaction.py:231](../../orchestrator/app/compaction.py#L231)). No
timeout beyond `settings.llm_request_timeout` (300 s); no retry. The "Max ~1500 words" instruction
([:30](../../orchestrator/app/summarize.py#L30)) is unenforced — only `max_tokens` bounds the output.

**Concurrency** — Async, stateless, no shared mutable state, no blocking calls.

**Complexity hotspots** — None. Largest function `condense` is 24 LOC.

**Findings** — None. No listed finding applies: this module has no swallowed exception, no global state, no DB or
filesystem access, and its single failure path is handled by its caller. Its one design smell —
`_MAX_TURN_CHARS = 4000` head-only truncation ([:35,48](../../orchestrator/app/summarize.py#L35)) being a *third*
clipping policy alongside `context.clip_middle` and `context.clip_message_contents` — has no assigned ID.

---

## memory.py

**Purpose** — In-process per-`session_id` transcript trimmed to `SESSION_MAX_TURNS` exchanges. Explicitly a
fallback: [main.py:387](../../orchestrator/app/main.py#L387) prefers the client-supplied `history_messages`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `SessionMemory` | `class` | [memory.py:13](../../orchestrator/app/memory.py#L13) |
| `SessionMemory.__init__` | `(max_turns: int \| None = None)` | [memory.py:14-16](../../orchestrator/app/memory.py#L14) |
| `SessionMemory.history` | `(session_id: str) -> List[dict]` (a copy) | [memory.py:18-20](../../orchestrator/app/memory.py#L18) |
| `SessionMemory.add_exchange` | `(session_id, user_text, assistant_text) -> None` | [memory.py:22-29](../../orchestrator/app/memory.py#L22) |
| `SessionMemory.clear` | `(session_id: str) -> None` | [memory.py:31-32](../../orchestrator/app/memory.py#L31) |
| `memory` | module-level singleton `SessionMemory()` | [memory.py:35](../../orchestrator/app/memory.py#L35) |

**Control flow** — `add_exchange` appends two dicts ([:24-25](../../orchestrator/app/memory.py#L24)) and deletes
the overflow prefix `len(msgs) − max_turns*2` ([:27-29](../../orchestrator/app/memory.py#L27)).

**State & side effects** — `self._sessions: Dict[str, List[dict]]`
([memory.py:15](../../orchestrator/app/memory.py#L15)) is process-global via the singleton
([memory.py:35](../../orchestrator/app/memory.py#L35)). No DB, no filesystem, no network. Env read is indirect
through `settings.session_max_turns`.

**Dependencies** — Inbound: [main.py:24](../../orchestrator/app/main.py#L24) only (`memory.history` at
[main.py:387](../../orchestrator/app/main.py#L387), `memory.add_exchange` at
[main.py:646](../../orchestrator/app/main.py#L646)). `SessionMemory.clear`
([memory.py:31-32](../../orchestrator/app/memory.py#L31)) has **no caller anywhere** — dead code.
Outbound: `config.settings` ([memory.py:10](../../orchestrator/app/memory.py#L10)).

**Config** — `settings.session_max_turns` ([memory.py:16](../../orchestrator/app/memory.py#L16); default 20 → 40
messages, [config.py:263](../../orchestrator/app/config.py#L263)).

**Failure modes** — Nothing raises. There is no eviction of *sessions*, only of messages within a session
([:27-29](../../orchestrator/app/memory.py#L27)).

**Concurrency** — Fully synchronous, called from the async worker
([main.py:646](../../orchestrator/app/main.py#L646)). The work is trivial, so the blocking is irrelevant; the
unbounded `_sessions` growth is not.

**Complexity hotspots** — None. Largest function is 8 LOC.

**Findings** — None of the listed IDs apply. Worth flagging to the report author as an unassigned observation: the
frontend sends `session_id = conversationId` (`frontend/lib/streams.ts:321`), so `_sessions` accrues one entry per
conversation and is never freed for the process lifetime — an unbounded, effectively write-only cache, since
[main.py:387](../../orchestrator/app/main.py#L387) prefers `request.history_messages`.

---

## memory_recall.py

**Purpose** — Cross-chat recall: extract content keywords from the question, keyword-search the user's *other*
conversations, and render a system-context block.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_STOPWORDS` | `set[str]` (~60 entries, English only) | [memory_recall.py:17-25](../../orchestrator/app/memory_recall.py#L17) |
| `_WORD_RE` | `re.compile(r"[A-Za-z0-9_][A-Za-z0-9_'-]{2,}")` | [memory_recall.py:26](../../orchestrator/app/memory_recall.py#L26) |
| `keywords` | `(text: str, max_keywords: int = 8) -> List[str]` | [memory_recall.py:29-39](../../orchestrator/app/memory_recall.py#L29) |
| `format_recall_block` | `(hits: List[dict]) -> Optional[str]` | [memory_recall.py:42-54](../../orchestrator/app/memory_recall.py#L42) |
| `recall_block` | `(user_id: int, query: str, exclude_conversation_id: Optional[str], *, search=None, limit: int = 3) -> Optional[str]` | [memory_recall.py:57-75](../../orchestrator/app/memory_recall.py#L57) |

**Control flow**

1. `keywords(query)` — regex findall, lowercase, drop stopwords and duplicates, cap at 8
   ([:32-38](../../orchestrator/app/memory_recall.py#L32)).
2. `recall_block` returns `None` on empty keywords ([:69-70](../../orchestrator/app/memory_recall.py#L69)).
3. `search` defaults to `db.recall_conversations` via a lazy import ([:71-73](../../orchestrator/app/memory_recall.py#L71)).
4. `hits = search(user_id, kws, exclude_conversation_id, limit)` (:74) → `format_recall_block` (:75), producing
   `'- From "<title>": <snippet>'` lines under an instruction header
   ([:46-53](../../orchestrator/app/memory_recall.py#L46)).

**State & side effects** — DB read only, through the injected `search` callable →
[`db.recall_conversations`](../../orchestrator/app/db.py#L981), which opens a connection
([db.py:1007](../../orchestrator/app/db.py#L1007)) and issues a `GROUP BY` ranking query with N
`content LIKE ? ESCAPE '\'` terms plus one snippet query per hit
([db.py:1008-1013](../../orchestrator/app/db.py#L1008)). No filesystem, no network, no globals, no env reads.

**Dependencies** — Inbound: [main.py:325,451](../../orchestrator/app/main.py#L325) (`recall_block`);
[core/urls.py:13](../../orchestrator/app/core/urls.py#L13) and
[engines/repo.py:18](../../orchestrator/app/engines/repo.py#L18) import `keywords`;
`tests/test_memory_recall.py:2`. Outbound: `re`; lazily `db` (:71).

**Config** — None. `limit: int = 3` ([:63](../../orchestrator/app/memory_recall.py#L63)) and
`max_keywords: int = 8` ([:29](../../orchestrator/app/memory_recall.py#L29)) are hard-coded, not configurable.

**Failure modes** — **No `try`/`except` at all.** A `sqlite3` error from `recall_conversations` propagates to
[main.py:451](../../orchestrator/app/main.py#L451), is caught by the worker's catch-all
([main.py:670](../../orchestrator/app/main.py#L670)) and becomes a terminal `error` event — a recall failure kills
the whole answer. No timeout, no bound on the number of messages scanned.

**Concurrency** — A **synchronous function called from `async def worker()`**
([main.py:451](../../orchestrator/app/main.py#L451)). The `LIKE` scan is unindexed
([db.py:995](../../orchestrator/app/db.py#L995) builds one `m.content LIKE ? ESCAPE '\'` per keyword; there is no
index on `messages.content`) and runs over every message row the user owns, blocking the single event loop for its
duration.

**Complexity hotspots** — None. Largest function `recall_block` is 19 LOC.

**Findings** — `PERF-03` (every call pays the full `connect()` schema+migrate path). Unassigned observation for
the report author: the unindexed full-table `LIKE` scan runs synchronously on the event loop and is unbounded,
and this module duplicates the "recall" concern with `recall.py` (keyword-across-chats vs embedding-within-chat,
unrelated code paths and headers — [memory_recall.py:46-48](../../orchestrator/app/memory_recall.py#L46) vs
[recall.py:111-114](../../orchestrator/app/recall.py#L111)).

---

## recall.py

**Purpose** — Within-conversation semantic recall: embed every folded turn into SQLite and retrieve the top-k most
similar chunks for each new question.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_CHUNK_CHARS` / `_CHUNK_OVERLAP` / `_MIN_CHUNK_CHARS` | `1200` / `150` / `15` | [recall.py:31](../../orchestrator/app/recall.py#L31), [:32](../../orchestrator/app/recall.py#L32), [:36](../../orchestrator/app/recall.py#L36) |
| `pack_vector` / `unpack_vector` | `(Sequence[float]) -> bytes` / `(bytes) -> List[float]` | [recall.py:39-40](../../orchestrator/app/recall.py#L39), [:43-46](../../orchestrator/app/recall.py#L43) |
| `cosine` | `(a, b) -> float` | [recall.py:49-57](../../orchestrator/app/recall.py#L49) |
| `chunk_text` | `(text: str) -> List[str]` | [recall.py:60-75](../../orchestrator/app/recall.py#L60) |
| `index_folded` | `async (conversation_id, folded, first_ordinal) -> int` | [recall.py:78-108](../../orchestrator/app/recall.py#L78) |
| `RECALL_HEADER` | constant | [recall.py:111-114](../../orchestrator/app/recall.py#L111) |
| `retrieve_block` | `async (conversation_id, question, top_k=None) -> Optional[str]` | [recall.py:117-144](../../orchestrator/app/recall.py#L117) |

**Control flow**

`index_folded`: return 0 immediately if `settings.semantic_recall_enabled` is false
([:82-83](../../orchestrator/app/recall.py#L82)) → `ordinal = first_ordinal * 1000`
([:86](../../orchestrator/app/recall.py#L86)) → per folded turn, `chunk_text(content)` and append
`{ordinal, role, text}` ([:87-100](../../orchestrator/app/recall.py#L87)) →
`vectors = await llm.embed_texts(texts)` in one call ([:103](../../orchestrator/app/recall.py#L103)) →
`db.add_conversation_chunks(conversation_id, rows)` ([:107](../../orchestrator/app/recall.py#L107)) → per-row
`INSERT OR REPLACE` in one transaction ([db.py:449-463](../../orchestrator/app/db.py#L449)).

`retrieve_block`: bail if disabled or blank ([:126-127](../../orchestrator/app/recall.py#L126)) →
`db.get_conversation_chunks(conversation_id)` loads **every** chunk for the conversation
([:129](../../orchestrator/app/recall.py#L129) → [db.py:466-482](../../orchestrator/app/db.py#L466)) →
`query = (await llm.embed_texts([question]))[0]` ([:132](../../orchestrator/app/recall.py#L132)) → brute-force
`cosine` over all chunks in pure Python ([:133-135](../../orchestrator/app/recall.py#L133)), sort, take
`settings.retrieve_top_k` with score > 0 ([:136-138](../../orchestrator/app/recall.py#L136)) → render
`[role] text` lines under `RECALL_HEADER` ([:141-142](../../orchestrator/app/recall.py#L141)).

**State & side effects** — DB write `db.add_conversation_chunks` ([:107](../../orchestrator/app/recall.py#L107));
DB read `db.get_conversation_chunks` ([:129](../../orchestrator/app/recall.py#L129)). Network/GPU:
`llm.embed_texts` → `settings.embed_base_url` (`http://vllm-embed:30003/v1`,
[config.py:80-82](../../orchestrator/app/config.py#L80)), each input clipped to
`settings.embed_input_char_cap` inside [llm.py:343,346](../../orchestrator/app/llm.py#L343). No filesystem, no
globals, no direct env reads. Deliberate storage decision documented at
[recall.py:13-19](../../orchestrator/app/recall.py#L13): conversation embeddings live in SQLite, never in the
LanceDB Salesforce `chunks` table.

**Dependencies** — Inbound: [compaction.py:192-194](../../orchestrator/app/compaction.py#L192) (`index_folded`),
[main.py:536,539](../../orchestrator/app/main.py#L536) (`retrieve_block`). Outbound: `array`, `math`, `db`, `llm`
([recall.py:23-27](../../orchestrator/app/recall.py#L23)), `config.settings` (:28).

**Config** — `settings.semantic_recall_enabled` (:82, :126), `settings.retrieve_top_k` (:137).

**Failure modes**

- `retrieve_block` wraps everything in `try/except Exception: return None`
  ([:128,143-144](../../orchestrator/app/recall.py#L128)) — silent; an embedding-service outage degrades recall
  invisibly.
- `index_folded` has **no** internal error handling; its caller `compaction._fold` swallows the failure with a bare
  `except Exception: pass` ([compaction.py:195-196](../../orchestrator/app/compaction.py#L195)).
- `unpack_vector` ([:43-46](../../orchestrator/app/recall.py#L43)) does not validate blob length; a truncated blob
  yields a short vector that `cosine` rejects by length mismatch ([:51](../../orchestrator/app/recall.py#L51)) →
  silently scores 0.
- No timeout beyond `settings.llm_request_timeout`; no retry; no bound on the number of chunks loaded or scored.

**Concurrency** — Async, but `db.get_conversation_chunks` (:129) and `db.add_conversation_chunks` (:107) are
blocking sqlite calls on the event loop, and the cosine loop ([:133-135](../../orchestrator/app/recall.py#L133))
is pure-Python O(chunks × dim) with no `await` — a measurable event-loop stall on a long conversation.
`ordinal = first_ordinal * 1000` ([:86,100](../../orchestrator/app/recall.py#L86)) is a packing scheme that
silently collides if one turn produces more than 1000 chunks (a turn > 1.2 MB).

**Complexity hotspots** — None over 60 LOC; `index_folded` is 31 LOC, `retrieve_block` 28 LOC.

**Findings** — `REL-03` (silent `except Exception: return None` at
[:143-144](../../orchestrator/app/recall.py#L143)), `PERF-03`.

---

## history.py

**Purpose** — Server-side conversation CRUD, thread sync/truncate, rolling-summary read, and keyword search, all
under `require_user`.

**Public surface**

| Symbol | Route / signature | `file:line` |
|---|---|---|
| `router` | `APIRouter(prefix="/history", tags=["history"])` | [history.py:30](../../orchestrator/app/history.py#L30) |
| `_CONVERSATION_ID_RE` / `_MAX_TITLE_LENGTH` / `_MAX_QUERY_LENGTH` | `^[A-Za-z0-9_-]{1,64}$` / `200` / `100` | [history.py:33-35](../../orchestrator/app/history.py#L33) |
| `ConversationIn` / `ConversationUpdate` (`extra="forbid"`) | pydantic models | [history.py:38-40](../../orchestrator/app/history.py#L38), [:43-54](../../orchestrator/app/history.py#L43) |
| `MessageIn` / `MessagesReplaceIn` / `TruncateIn` (`extra="forbid"`) | pydantic models | [history.py:57-60](../../orchestrator/app/history.py#L57), [:63-66](../../orchestrator/app/history.py#L63), [:193-197](../../orchestrator/app/history.py#L193) |
| `_clean_title` / `_not_found` | helpers | [history.py:69-73](../../orchestrator/app/history.py#L69), [:76-77](../../orchestrator/app/history.py#L76) |
| `list_conversations` | `GET /history/conversations?archived=` | [history.py:80-85](../../orchestrator/app/history.py#L80) |
| `create_conversation` | `POST /history/conversations` → 200/400/409 | [history.py:88-102](../../orchestrator/app/history.py#L88) |
| `get_conversation` | `GET /history/conversations/{id}` | [history.py:105-113](../../orchestrator/app/history.py#L105) |
| `update_conversation` | `PUT /history/conversations/{id}` | [history.py:116-131](../../orchestrator/app/history.py#L116) |
| `add_message` | `POST /history/conversations/{id}/messages` | [history.py:134-148](../../orchestrator/app/history.py#L134) |
| `replace_messages` | `PUT /history/conversations/{id}/messages` → 409 on shrink | [history.py:151-190](../../orchestrator/app/history.py#L151) |
| `truncate_messages` | `POST /history/conversations/{id}/truncate` → 409 on `ConversationChanged` | [history.py:200-236](../../orchestrator/app/history.py#L200) |
| `get_summary` | `GET /history/conversations/{id}/summary` | [history.py:239-254](../../orchestrator/app/history.py#L239) |
| `delete_conversation` | `DELETE /history/conversations/{id}` | [history.py:257-263](../../orchestrator/app/history.py#L257) |
| `search_history` | `GET /history/search?q=&limit=` | [history.py:266-288](../../orchestrator/app/history.py#L266) |

**Control flow** (representative paths)

1. Every handler takes `user: sqlite3.Row = Depends(require_user)`
   ([history.py:82,90,107,120,138,155,204,241,260,270](../../orchestrator/app/history.py#L82)) →
   [`auth.require_user`](../../orchestrator/app/auth.py#L95), which never 401s.
2. `create_conversation`: id defaults to `uuid4().hex` (:92), is regex-validated (:93-97), title cleaned (:98),
   then `db.create_conversation` with `sqlite3.IntegrityError` → 409 (:99-102).
3. `replace_messages`: validate every role length (:165-170), then `db.replace_messages` in one transaction
   ([db.py:616-675](../../orchestrator/app/db.py#L616)), which refuses a shrink via `MessageCountWouldShrink` →
   409 (:180-187).
4. `truncate_messages`: range checks (:213-218), `db.truncate_messages` with the optimistic `expected_total` →
   `ConversationChanged` → 409 (:219-230), then **`db.clear_summary(conversation_id)`**
   ([:235](../../orchestrator/app/history.py#L235)) so the rolling summary cannot describe deleted turns.
5. `search_history`: trim `q`; empty → `{"results": []}` (:280-282); > 100 chars → 400 (:283-287); otherwise
   `db.search_conversations`, which clamps `limit` to `SEARCH_LIMIT_MAX = 100`
   ([db.py:1036](../../orchestrator/app/db.py#L1036)).

**State & side effects** — DB reads/writes only, all through `db.*` helpers, each of which opens its own
connection and re-runs the schema+migration. No filesystem, no network, no globals, no env reads.
`truncate_messages` additionally deletes `conversation_chunks` through `db.clear_summary`
([db.py:438-441](../../orchestrator/app/db.py#L438)).

**Dependencies** — Inbound: [main.py:22,58](../../orchestrator/app/main.py#L22);
`tests/test_conversation_integrity.py:323` (`TruncateIn`); the history test suites mount `app.main:app`.
Outbound: `re`, `sqlite3`, `uuid`, `fastapi`, `pydantic`, `db`
([history.py:27](../../orchestrator/app/history.py#L27)), `auth.require_user` (:28).

**Config** — None read directly. `db.SEARCH_LIMIT_DEFAULT`
([history.py:269](../../orchestrator/app/history.py#L269) → [db.py:902](../../orchestrator/app/db.py#L902)) is
the only external constant.

**Failure modes** — All error paths are explicit `HTTPException`s; nothing is swallowed.
`sqlite3.IntegrityError` is caught only in `create_conversation`
([:101](../../orchestrator/app/history.py#L101)); any other sqlite error in any handler surfaces as an unhandled
500. No timeouts, no retries. **No pagination** on `list_conversations`
([:80-85](../../orchestrator/app/history.py#L80) → [db.py:250](../../orchestrator/app/db.py#L250) returns every
row) or on `get_conversation`'s `db.list_messages` ([:112](../../orchestrator/app/history.py#L112) →
[db.py:343-354](../../orchestrator/app/db.py#L343) returns the entire thread).

**Concurrency** — **Every handler is a plain `def`**, so FastAPI runs them in the anyio threadpool — this is the
one module in this document that does *not* block the event loop. `db.replace_messages` and
`db.truncate_messages` are single-transaction ([db.py:627](../../orchestrator/app/db.py#L627),
[db.py:586](../../orchestrator/app/db.py#L586)); `db.add_message` relies on the unique index
`idx_messages_generation` ([db.py:187-191](../../orchestrator/app/db.py#L187)) for cross-client idempotency.
A residual race: `replace_messages` is atomic server-side, but the read-modify-write in the *client* that
produces `messages` is not, so two tabs syncing concurrently both pass the count check and the later write wins
whole.

**Complexity hotspots** — None. Largest handler `truncate_messages` is 37 LOC
([history.py:200-236](../../orchestrator/app/history.py#L200)).

**Findings** — `SEC-01` (every route's `Depends(require_user)` can never 401),
`DATA-03` (`delete_conversation` at [history.py:257-263](../../orchestrator/app/history.py#L257) →
[db.py:334-340](../../orchestrator/app/db.py#L334) orphans `uploads`/`url_documents`/`repos`/`repo_chunks`),
`PERF-03`. Unassigned observation: `role` validation is length-only
([history.py:140-142](../../orchestrator/app/history.py#L140)), so `role: "system"` can be persisted into a thread
and later replayed to the model; and `_CONVERSATION_ID_RE` is enforced **only** on `POST /history/conversations`
— `/chat`, `/chat/attach/{id}`, `/chat/compact` and `/uploads` accept arbitrary conversation-id strings.

---

## sse.py

**Purpose** — The single formatter for SSE frames, with an allowlist of event names and a step-status allowlist.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `ALLOWED_EVENTS` | `("token","meta","done","error")` | [sse.py:34](../../orchestrator/app/sse.py#L34) |
| `V2_EVENTS` | `("reasoning","step")` | [sse.py:36](../../orchestrator/app/sse.py#L36) |
| `PROGRESS_EVENTS` | `("status",)` | [sse.py:39](../../orchestrator/app/sse.py#L39) |
| `RESEARCH_EVENTS` | `("research",)` | [sse.py:43](../../orchestrator/app/sse.py#L43) |
| `ALL_EVENTS` | concatenation of the four — **8 event types** | [sse.py:44](../../orchestrator/app/sse.py#L44) |
| `STEP_STATUSES` | `("running","done","failed")` | [sse.py:46](../../orchestrator/app/sse.py#L46) |
| `sse_event` | `(event: str, data: Optional[Mapping]=None) -> str` | [sse.py:49-54](../../orchestrator/app/sse.py#L49) |
| `token_event` / `meta_event` / `done_event` / `error_event` / `reasoning_event` | thin wrappers | [sse.py:57-75](../../orchestrator/app/sse.py#L57) |
| `step_event` | `(id: int, title: str, status: str, detail: Optional[str]=None) -> str` | [sse.py:78-85](../../orchestrator/app/sse.py#L78) |

The 8 orchestrator-side event names at [sse.py:44](../../orchestrator/app/sse.py#L44) are exactly the 8 the
frontend decoder accepts (`frontend/lib/sse.ts:126-218`).

**Control flow** — `sse_event` validates `event in ALL_EVENTS` else raises `ValueError`
([:51-52](../../orchestrator/app/sse.py#L51)), then `json.dumps(dict(data or {}), ensure_ascii=False, default=str)`
(:53) and returns `f"event: {event}\ndata: {payload}\n\n"` (:54). `step_event` additionally validates `status`
([:80-81](../../orchestrator/app/sse.py#L80)) and includes `detail` only when not `None` (:83-84).

**State & side effects** — None. Pure formatting; no I/O, no globals, no env reads.

**Dependencies** — Inbound, over `orchestrator/app`: **`main.py:25` only**, used at
[main.py:118](../../orchestrator/app/main.py#L118). Engines never format SSE — they call the `emit(event, data)`
closure defined at [main.py:364](../../orchestrator/app/main.py#L364). Tests: `tests/test_sse.py:6`,
`tests/test_sse_v2.py:7`, `tests/test_chart_routes.py:285`, `tests/test_system_normalization.py:146`.
Outbound: `json`, `typing`.

**Config** — None.

**Failure modes** — `sse_event` raises `ValueError` for an unknown event name. Because it is called from
`LiveGeneration.follow()` ([main.py:118](../../orchestrator/app/main.py#L118)) rather than from `publish()`
([main.py:95-98](../../orchestrator/app/main.py#L95)), an invalid name raises inside the *reader*, after the frame
has already been buffered — the stream dies mid-flight instead of the publisher failing fast.
`json.dumps(..., default=str)` never raises on unserialisable values but silently stringifies them.
There is **no keep-alive/heartbeat frame** and no `id:`/`retry:` field, so a long silent generation depends on
`X-Accel-Buffering: no` ([main.py:684](../../orchestrator/app/main.py#L684)) and on no intermediate proxy having
an idle timeout. The docstring ([sse.py:20-24](../../orchestrator/app/sse.py#L20)) documents `done` and `error` as
the only terminal states; the cancellation path ([main.py:668-669](../../orchestrator/app/main.py#L668) →
`finish()` → `follow()` breaks at [main.py:115](../../orchestrator/app/main.py#L115)) closes the stream with **no
terminal frame at all** — a third, undocumented terminal state that `frontend/lib/streams.ts:261-265` silently
maps to `status: 'done'`.

**Concurrency** — Pure sync, no shared state.

**Complexity hotspots** — None. Largest function `step_event` is 8 LOC.

**Findings** — `OBS-01` (frames carry no correlation/trace id and no SSE `id:` field, so a browser event cannot be
tied back to an orchestrator or model call). Unassigned observation: `token_event`/`meta_event`/`done_event`/
`error_event`/`reasoning_event`/`step_event` ([sse.py:57-85](../../orchestrator/app/sse.py#L57)) have no
non-test callers.

---

## llm.py

**Purpose** — The single vLLM/OpenAI-compatible client layer: chat, streaming, reasoning-stream, router
classification, vision, embeddings. All four backends are vLLM behind OpenAI-compatible endpoints.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `LOCAL_API_KEY` | `"local-no-key"` placeholder | [llm.py:29](../../orchestrator/app/llm.py#L29) |
| `MODEL_CHOICES` / `REASONING_EFFORTS` | `("smart","fast")` / `("fast","low","medium","high")` | [llm.py:32](../../orchestrator/app/llm.py#L32), [:36](../../orchestrator/app/llm.py#L36) |
| `normalize_system` | `(messages: Sequence[dict]) -> List[dict]` | [llm.py:39](../../orchestrator/app/llm.py#L39) |
| `_client` | `(base_url: str, api_key: Optional[str] = None)` — the single client factory | [llm.py:72](../../orchestrator/app/llm.py#L72) |
| `_openai_client` | `()` | [llm.py:82](../../orchestrator/app/llm.py#L82) |
| `chat_completion` | `async (messages, *, model=None, temperature=0.2, max_tokens=None) -> str` | [llm.py:91](../../orchestrator/app/llm.py#L91) |
| `stream_chat_completion` | `async (…, thinking=True) -> AsyncIterator[str]` | [llm.py:117](../../orchestrator/app/llm.py#L117) |
| `resolve_model_choice` | `(choice: str) -> Tuple[str, str, str]` | [llm.py:158](../../orchestrator/app/llm.py#L158) |
| `served_model_id` | `(choice: str) -> str` | [llm.py:169](../../orchestrator/app/llm.py#L169) |
| `wants_thinking` / `thinking_body` | `(model_choice, effort) -> bool` / `(enabled) -> dict` | [llm.py:174](../../orchestrator/app/llm.py#L174), [:188](../../orchestrator/app/llm.py#L188) |
| `apply_reasoning_effort` | `(messages, effort, model_choice="smart") -> List[dict]` — **no-op passthrough** | [llm.py:198](../../orchestrator/app/llm.py#L198), returns at [:209](../../orchestrator/app/llm.py#L209) |
| `stream_chat_events` | `async (messages, *, model_choice="smart", effort="medium", temperature=0.2, max_tokens=None) -> AsyncIterator[Tuple[str,str]]` | [llm.py:212](../../orchestrator/app/llm.py#L212) |
| `router_chat_completion` | `async (messages, *, temperature=0.0, max_tokens=200) -> str` | [llm.py:270](../../orchestrator/app/llm.py#L270) |
| `vision_chat_stream` | `async (messages, *, temperature=0.2, max_tokens=None) -> AsyncIterator[str]` | [llm.py:303](../../orchestrator/app/llm.py#L303) |
| `embed_texts` | `async (texts, *, model=None) -> List[List[float]]` | [llm.py:330](../../orchestrator/app/llm.py#L330) |

**Control flow** — `stream_chat_events`, the hot path for every chat engine

1. `resolve_model_choice(model_choice)` → `(base_url, api_key, model_id)`
   ([llm.py:224](../../orchestrator/app/llm.py#L224)); `"fast"` → `settings.router_base_url` /
   `settings.router_model` ([:164-165](../../orchestrator/app/llm.py#L164)), anything else →
   `settings.openai_base_url` / `settings.llm_model` ([:166](../../orchestrator/app/llm.py#L166)).
2. `_client(base_url, api_key)` → **a brand-new `AsyncOpenAI` per call**
   ([llm.py:72-79](../../orchestrator/app/llm.py#L72)) with `timeout=settings.llm_request_timeout`.
3. `apply_reasoning_effort` (no-op) → `normalize_system` (folds every system block into one at index 0,
   [:53-69](../../orchestrator/app/llm.py#L53)) → `await context.fit_request(...)`, which probes
   `POST {root}/tokenize` and trims/clips until prompt+completion fit
   ([llm.py:229-234](../../orchestrator/app/llm.py#L229)).
4. `await client.chat.completions.create(..., stream=True, extra_body=thinking_body(wants_thinking(...)))`
   ([:235-243](../../orchestrator/app/llm.py#L235)).
5. Iterate chunks; skip empty `choices`; extract `delta.reasoning` / `delta.reasoning_content` /
   `delta.model_extra[...]` ([:253-259](../../orchestrator/app/llm.py#L253)) → yield `("reasoning", …)`; yield
   `("token", delta.content)` ([:244-263](../../orchestrator/app/llm.py#L244)).

`chat_completion` ([:99-114](../../orchestrator/app/llm.py#L99)) is identical minus the stream loop and always
forces `thinking_body(True)` ([:112](../../orchestrator/app/llm.py#L112)). `router_chat_completion`
([:283-300](../../orchestrator/app/llm.py#L283)) additionally clips every message to
`settings.router_input_char_cap` ([:286](../../orchestrator/app/llm.py#L286)) and forces `thinking_body(False)`
([:298](../../orchestrator/app/llm.py#L298)). `embed_texts` ([:342-348](../../orchestrator/app/llm.py#L342))
clips each input to `settings.embed_input_char_cap` and re-sorts `resp.data` by `.index`.

**State & side effects** — Network egress to `settings.openai_base_url`
([:84](../../orchestrator/app/llm.py#L84)), `settings.router_base_url` ([:165,283](../../orchestrator/app/llm.py#L165)),
`settings.vision_base_url` ([:314](../../orchestrator/app/llm.py#L314)), `settings.embed_base_url`
([:342](../../orchestrator/app/llm.py#L342)); indirect egress to `{root}/tokenize` via `context.fit_request`.
All of these are vLLM inference. No DB, no filesystem, no global mutation, no direct env reads. The module
docstring's claim that "Nothing here performs network I/O at import time"
([llm.py:18](../../orchestrator/app/llm.py#L18)) is verified — the `openai` import is deferred into `_client`
([:73](../../orchestrator/app/llm.py#L73)). **No token accounting**: `resp.usage` is never read anywhere in the
file; budgets come only from `context.fit_request`'s estimate.

**Dependencies** — Inbound: essentially every engine and context module — `summarize.py:14,74,88`;
`recall.py:27,103,132`; `main.py:16,310,313,316,538,657`; `engines/search.py:23,186,200,412,450,487`;
`engines/chat.py:20,90`; `engines/repo.py:13,128,170`; `engines/document.py:12,69`; `engines/vision.py:17,83`;
`engines/url.py:101`; `engines/sql.py:18,113,211,343,372,429`; `engines/rag.py:19,38,139`;
`engines/live_sf.py:20,71`; `engines/dataset.py:23,105`; `engines/report.py:20,133,201,218`;
`engines/router.py:115,126`; `engines/agent.py:210,282,357`; `engines/orchestrate.py:138`.
Outbound: `app.context` ([:24,26](../../orchestrator/app/llm.py#L24)), `app.config.settings` (:25),
`openai.AsyncOpenAI` (lazy, :73).

**Config** — `settings.llm_request_timeout` (:78), `settings.openai_base_url` (:84,103,130),
`settings.openai_api_key` (:84,166), `settings.llm_model` (:100,127,166), `settings.router_base_url`
(:165,283,288), `settings.router_model` (:165,289,293), `settings.router_input_char_cap` (:286),
`settings.vision_base_url` (:314), `settings.vision_model` (:316), `settings.embed_base_url` (:342),
`settings.embed_input_char_cap` (:343), `settings.embed_model` (:345).

**Failure modes**

- **No `try`/`except` anywhere in the file.** Any `openai.APIError`, `APITimeoutError`, `APIConnectionError` or
  vLLM 400 propagates to the caller. Nothing is swallowed here — a deliberate and correct choice.
- Timeout: `settings.llm_request_timeout` default **300.0 s** ([config.py:264](../../orchestrator/app/config.py#L264)),
  applied per attempt.
- Retry: not configured. `AsyncOpenAI` is constructed without `max_retries`
  ([llm.py:75-79](../../orchestrator/app/llm.py#L75)), so the SDK default applies — verified against the installed
  `openai 2.46.0`, `DEFAULT_MAX_RETRIES == 2`. Worst case per LLM call = 3 × 300 s = **900 s**.
- Circuit breaker: none anywhere in `orchestrator/app`.
- Client lifetime: the `AsyncOpenAI` returned by `_client` is never `.close()`d and never reused — a new
  `httpx.AsyncClient` connection pool per LLM call, discarded without `aclose()`.
- `vision_chat_stream` does **not** call `context.fit_request` ([:314-321](../../orchestrator/app/llm.py#L314)) —
  the only client path with no context sizing and no clipping; an oversized multimodal payload is an unhandled 400.
  It is also dead code (no production caller; `engines/vision.py:83` uses `stream_chat_events`).
- `embed_texts` sends `input=[]` when `texts` is empty ([:346](../../orchestrator/app/llm.py#L346)) — vLLM 400,
  uncaught.
- Streaming generators ([:146,244,322](../../orchestrator/app/llm.py#L146)) have no `try/finally` closing the
  stream; abandoning the generator on client disconnect leaves the HTTP response unclosed.

**Concurrency** — Fully async. No blocking calls inside `async def` (the `openai` import at
[:73](../../orchestrator/app/llm.py#L73) is cached by `sys.modules` after the first call). No module-level mutable
state — `LOCAL_API_KEY`, `MODEL_CHOICES`, `REASONING_EFFORTS` are immutable. Shared state
(`context._window_cache`) lives in `context.py` and is guarded there.

**Complexity hotspots** — `stream_chat_events` ([llm.py:212](../../orchestrator/app/llm.py#L212)) is 52 LOC
(measured with `ast`) — the largest, and under 60. Duplication is the real cost: the
`_client → fit_request → create` triple is repeated four times
([:99-113](../../orchestrator/app/llm.py#L99), [:126-145](../../orchestrator/app/llm.py#L126),
[:224-243](../../orchestrator/app/llm.py#L224), [:283-299](../../orchestrator/app/llm.py#L283)).

**Findings** — `PERF-04`, `OBS-01`. Unassigned observations: `vision_chat_stream`
([:303-327](../../orchestrator/app/llm.py#L303)) and `apply_reasoning_effort`
([:198-209](../../orchestrator/app/llm.py#L198)) are dead; docstring drift at
[:83](../../orchestrator/app/llm.py#L83) and [config.py:45](../../orchestrator/app/config.py#L45) still names
"gpt-oss-120b" while `settings.llm_model` defaults to `Qwen/Qwen3.6-35B-A3B-NVFP4`
([config.py:56](../../orchestrator/app/config.py#L56)).

---

## auth.py

**Purpose** — Collapses all identity to ONE local account. There is no login, no password check and no session;
`require_user` is a dependency that can never fail.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `router` | `APIRouter(prefix="/auth", tags=["auth"])` | [auth.py:32](../../orchestrator/app/auth.py#L32) |
| `SESSION_COOKIE` | `"ts_session"` — documented as ignored | [auth.py:35](../../orchestrator/app/auth.py#L35) |
| `DEFAULT_LOCAL_USERNAME` | `"local"` | [auth.py:37](../../orchestrator/app/auth.py#L37) |
| `_cached_user_id` | `Optional[int] = None` — module-level mutable global | [auth.py:39](../../orchestrator/app/auth.py#L39) |
| `_local_username` / `_oldest_user` | `() -> str` / `() -> Optional[sqlite3.Row]` | [auth.py:42](../../orchestrator/app/auth.py#L42), [:46](../../orchestrator/app/auth.py#L46) |
| `local_user` | `() -> sqlite3.Row` | [auth.py:52](../../orchestrator/app/auth.py#L52) |
| `current_user` | `(request: Request) -> Optional[sqlite3.Row]` | [auth.py:89](../../orchestrator/app/auth.py#L89) |
| `require_user` | `(request: Request) -> sqlite3.Row` — FastAPI dependency | [auth.py:95](../../orchestrator/app/auth.py#L95) |
| `me` | `GET /auth/me` → `{"username": …, "local": True}` | [auth.py:100-103](../../orchestrator/app/auth.py#L100) |

**Control flow** — `require_user` ([auth.py:95](../../orchestrator/app/auth.py#L95)) → `current_user`
([:89](../../orchestrator/app/auth.py#L89)), which discards the `Request` (`del request`,
[:91](../../orchestrator/app/auth.py#L91)) → `local_user` ([:52](../../orchestrator/app/auth.py#L52)):

1. If `_cached_user_id` is set, `db.get_user_by_id` and return; on `None`, clear the cache and continue
   ([:54-59](../../orchestrator/app/auth.py#L54)).
2. If `LOCAL_USERNAME` is set, `db.get_user_by_username`; on hit, cache and return
   ([:61-66](../../orchestrator/app/auth.py#L61)).
3. If `LOCAL_USERNAME` is not set, `_oldest_user()` → `SELECT * FROM users ORDER BY id LIMIT 1`; on hit, cache and
   return ([:68-72](../../orchestrator/app/auth.py#L68)).
4. Otherwise create the account with the literal hash `"!local-no-login"`
   ([:79](../../orchestrator/app/auth.py#L79)), swallow `sqlite3.IntegrityError` as a race
   ([:80-81](../../orchestrator/app/auth.py#L80)), re-read, cache, return; raise `RuntimeError` if both create and
   lookup fail ([:83-84](../../orchestrator/app/auth.py#L83)).

**State & side effects** — Mutates the module-level global `_cached_user_id`
([:59,65,71,85](../../orchestrator/app/auth.py#L59)). DB reads via `db.get_user_by_id` (:56),
`db.get_user_by_username` (:63,82), and a raw `SELECT` on a connection it opens itself (:48-49).
**DB write**: `db.create_user(username, "!local-no-login")` ([:79](../../orchestrator/app/auth.py#L79)) on a fresh
install. **Connection leak**: `_oldest_user` ([:48](../../orchestrator/app/auth.py#L48)) calls `db.connect()` and
never closes it — no `closing(...)`, no `con.close()`. Env read: `os.environ.get("LOCAL_USERNAME")` at
[:43](../../orchestrator/app/auth.py#L43) and [:61](../../orchestrator/app/auth.py#L61). No network, no GPU.

**Dependencies** — Inbound: [main.py:17](../../orchestrator/app/main.py#L17) (`auth_router`, mounted at
[main.py:58](../../orchestrator/app/main.py#L58)), [main.py:324,327](../../orchestrator/app/main.py#L324),
[main.py:695,697](../../orchestrator/app/main.py#L695), [main.py:753,755](../../orchestrator/app/main.py#L753);
[history.py:28](../../orchestrator/app/history.py#L28) + every route dependency;
[uploads.py:24,70,162](../../orchestrator/app/uploads.py#L24). Outbound: `os`, `sqlite3`, `typing`, `fastapi`,
`app.db` ([auth.py:30](../../orchestrator/app/auth.py#L30)).

**Config** — `LOCAL_USERNAME` (env) at [auth.py:43,61](../../orchestrator/app/auth.py#L43). It is **not** present
in `Settings` ([config.py](../../orchestrator/app/config.py)) and **not** set in `docker-compose.yml` — the only
env var in the orchestrator read outside `config.py`.

**Failure modes** — `except sqlite3.IntegrityError: pass` ([:80-81](../../orchestrator/app/auth.py#L80)),
deliberate for the concurrent-create race. `RuntimeError` at [:84](../../orchestrator/app/auth.py#L84) becomes a
500. Any DB failure in `local_user` propagates into **every** request. `require_user` **never raises 401/403** —
its own docstring says so ([:96](../../orchestrator/app/auth.py#L96)). No rate limiting, no CSRF token, no origin
check beyond CORS, no bearer token, no mTLS.

**Concurrency** — Synchronous. `local_user()` is called both from the anyio threadpool (the sync `history.py` /
`uploads.py` routes) *and* directly on the event loop from `async def chat`
([main.py:327](../../orchestrator/app/main.py#L327)) and `async def chat_compact`
([main.py:755](../../orchestrator/app/main.py#L755)) — a blocking `sqlite3` round trip plus the full `connect()`
schema+migrate path inside the event loop on every chat request. `_cached_user_id` is read-modify-written without
a lock ([:54-59, :65, :71, :85](../../orchestrator/app/auth.py#L54)); two threads can both miss and both call
`db.create_user`, which is why [:80](../../orchestrator/app/auth.py#L80) swallows `IntegrityError`. The window is
benign (both resolve to the same row) but it is a real unguarded global.

**Complexity hotspots** — `local_user` ([auth.py:52](../../orchestrator/app/auth.py#L52)) is 35 LOC with four
early-return branches. No function over 60 LOC.

**Findings** — `SEC-01` (this module *is* the finding: [auth.py:95-97](../../orchestrator/app/auth.py#L95) plus
[auth.py:17-20](../../orchestrator/app/auth.py#L17) stating it outright), `PERF-03`, `COST-01`.
Unassigned observations: the `_oldest_user` connection leak
([auth.py:48](../../orchestrator/app/auth.py#L48)); the dead `SESSION_COOKIE` constant
([auth.py:35](../../orchestrator/app/auth.py#L35)); `argon2-cffi` and `itsdangerous` still installed and unused
(`orchestrator/requirements.txt`); the stale CORS comment at
[main.py:46](../../orchestrator/app/main.py#L46) claiming the `ts_session` cookie flows.

---

## db.py

**Purpose** — The entire app-state persistence layer: stdlib `sqlite3` at `settings.app_db_path`, WAL, one
short-lived connection per operation. Holds users, conversations, messages, summaries, embedded chunks, uploads,
fetched URLs, cloned repos and repo chunks.

**Public surface**

| Group | Symbols | `file:line` |
|---|---|---|
| Schema/lifecycle | `_SCHEMA`; `_ADDED_CONVERSATION_COLUMNS`; `_ADDED_MESSAGE_COLUMNS`; `utcnow()`; `migrate(con)`; `connect()` | [db.py:22-134](../../orchestrator/app/db.py#L22), [:141-144](../../orchestrator/app/db.py#L141), [:146](../../orchestrator/app/db.py#L146), [:149](../../orchestrator/app/db.py#L149), [:153](../../orchestrator/app/db.py#L153), [:195](../../orchestrator/app/db.py#L195) |
| Users | `create_user(username, password_hash) -> int`; `get_user_by_username(username)`; `get_user_by_id(user_id)` | [db.py:212](../../orchestrator/app/db.py#L212), [:223](../../orchestrator/app/db.py#L223), [:230](../../orchestrator/app/db.py#L230) |
| Conversations | `_conversation_dict(row)`; `list_conversations(user_id, archived=False)`; `create_conversation(user_id, conversation_id, title)`; `get_conversation(user_id, conversation_id)`; `update_conversation(...)`; `delete_conversation(user_id, conversation_id)`; `conversation_owner(conversation_id)` | [db.py:241](../../orchestrator/app/db.py#L241), [:250](../../orchestrator/app/db.py#L250), [:267](../../orchestrator/app/db.py#L267), [:286](../../orchestrator/app/db.py#L286), [:296](../../orchestrator/app/db.py#L296), [:334](../../orchestrator/app/db.py#L334), [:357](../../orchestrator/app/db.py#L357) |
| Messages | `list_messages(conversation_id)` — **no `user_id` parameter**; `MessageCountWouldShrink`; `ConversationChanged`; `truncate_messages(...)`; `replace_messages(...)`; `add_message(...)` | [db.py:343](../../orchestrator/app/db.py#L343), [:370](../../orchestrator/app/db.py#L370), [:562](../../orchestrator/app/db.py#L562), [:571](../../orchestrator/app/db.py#L571), [:616](../../orchestrator/app/db.py#L616), [:678](../../orchestrator/app/db.py#L678) |
| Summary / chunks | `get_summary`; `save_summary`; `clear_summary`; `add_conversation_chunks`; `get_conversation_chunks` | [db.py:394](../../orchestrator/app/db.py#L394), [:411](../../orchestrator/app/db.py#L411), [:426](../../orchestrator/app/db.py#L426), [:444](../../orchestrator/app/db.py#L444), [:466](../../orchestrator/app/db.py#L466) |
| Uploads | `save_upload`; `get_uploads`; `get_upload` | [db.py:490](../../orchestrator/app/db.py#L490), [:518](../../orchestrator/app/db.py#L518), [:542](../../orchestrator/app/db.py#L542) |
| URL documents | `save_url_document`; `get_url_documents`; `get_url_document_urls` | [db.py:755](../../orchestrator/app/db.py#L755), [:769](../../orchestrator/app/db.py#L769), [:780](../../orchestrator/app/db.py#L780) |
| Repos | `save_repo`; `get_repo`; `get_repo_keys`; `replace_repo_chunks`; `search_repo_chunks(…, limit=12)` | [db.py:794](../../orchestrator/app/db.py#L794), [:805](../../orchestrator/app/db.py#L805), [:815](../../orchestrator/app/db.py#L815), [:824](../../orchestrator/app/db.py#L824), [:844](../../orchestrator/app/db.py#L844) |
| Search | `SEARCH_LIMIT_DEFAULT=50`; `SEARCH_LIMIT_MAX=100`; `like_contains_pattern`; `snippet_window`; `_SEARCH_SQL`; `recall_conversations(…, limit=3)`; `search_conversations(...)` | [db.py:902-903](../../orchestrator/app/db.py#L902), [:910](../../orchestrator/app/db.py#L910), [:928](../../orchestrator/app/db.py#L928), [:958-975](../../orchestrator/app/db.py#L958), [:981](../../orchestrator/app/db.py#L981), [:1022](../../orchestrator/app/db.py#L1022) |

### `app.sqlite3` — complete DDL summary

All objects live in `_SCHEMA` ([db.py:22-134](../../orchestrator/app/db.py#L22)) except the last row, which
`migrate()` creates.

| Object | Columns / definition | FK / cascade | `file:line` |
|---|---|---|---|
| `users` | `id INTEGER PK AUTOINCREMENT`, `username TEXT NOT NULL UNIQUE COLLATE NOCASE`, `password_hash TEXT NOT NULL`, `created_at TEXT NOT NULL` | — | [db.py:23-28](../../orchestrator/app/db.py#L23) |
| `conversations` | `id TEXT PK` (**client-supplied**, [history.py:92](../../orchestrator/app/history.py#L92)), `user_id INTEGER NOT NULL`, `title`, `created_at`, `updated_at`, `pinned INTEGER NOT NULL DEFAULT 0`, `archived INTEGER NOT NULL DEFAULT 0` | `REFERENCES users(id) ON DELETE CASCADE` | [db.py:29-37](../../orchestrator/app/db.py#L29) |
| `messages` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL`, `role`, `content`, `meta TEXT`, `created_at`, `generation_id TEXT` | `REFERENCES conversations(id) ON DELETE CASCADE` | [db.py:38-51](../../orchestrator/app/db.py#L38) |
| `conversation_summaries` | `conversation_id TEXT PK`, `summary TEXT NOT NULL`, `covers_through INTEGER NOT NULL` (a *count*, not a row id — makes folding idempotent, [db.py:58-61](../../orchestrator/app/db.py#L58)), `token_estimate`, `updated_at` | `REFERENCES conversations(id) ON DELETE CASCADE` | [db.py:55-65](../../orchestrator/app/db.py#L55) |
| `conversation_chunks` | `id PK`, `conversation_id NOT NULL`, `ordinal INTEGER NOT NULL`, `role`, `text`, `embedding BLOB NOT NULL`, `created_at`, `UNIQUE(conversation_id, ordinal)` | `REFERENCES conversations(id) ON DELETE CASCADE` | [db.py:69-78](../../orchestrator/app/db.py#L69) |
| `uploads` | `id TEXT PK`, `conversation_id TEXT NOT NULL`, `filename`, `bytes INTEGER`, `status`, `profile TEXT`, `notes TEXT`, `created_at` | **none** | [db.py:84-93](../../orchestrator/app/db.py#L84) |
| `url_documents` | `id PK`, `conversation_id TEXT NOT NULL`, `url`, `title`, `text`, `fetched_at`, `UNIQUE(conversation_id, url)` | **none** | [db.py:102-110](../../orchestrator/app/db.py#L102) |
| `repos` | `id PK`, `conversation_id TEXT NOT NULL`, `repo_key`, `url`, `sha`, `cloned_at`, `UNIQUE(conversation_id, repo_key)` | **none** | [db.py:114-122](../../orchestrator/app/db.py#L114) |
| `repo_chunks` | `id PK`, `conversation_id TEXT NOT NULL`, `repo_key`, `path`, `start_line`, `end_line`, `text` | **none** | [db.py:123-131](../../orchestrator/app/db.py#L123) |

Indexes:

| Index | Definition | `file:line` |
|---|---|---|
| `idx_conversation_chunks_conv` | `ON conversation_chunks(conversation_id, ordinal)` — redundant with the `UNIQUE` constraint's implicit index | [db.py:79-80](../../orchestrator/app/db.py#L79) |
| `idx_uploads_conversation` | `ON uploads(conversation_id, created_at)` | [db.py:94-95](../../orchestrator/app/db.py#L94) |
| `idx_conversations_user` | `ON conversations(user_id, updated_at DESC)` | [db.py:96-97](../../orchestrator/app/db.py#L96) |
| `idx_messages_conversation` | `ON messages(conversation_id, id)` | [db.py:98-99](../../orchestrator/app/db.py#L98) |
| `idx_url_documents_conv` | `ON url_documents(conversation_id, id)` | [db.py:111-112](../../orchestrator/app/db.py#L111) |
| `idx_repo_chunks_conv` | `ON repo_chunks(conversation_id, repo_key, id)` | [db.py:132-133](../../orchestrator/app/db.py#L132) |
| `idx_messages_generation` | `UNIQUE ON messages(conversation_id, generation_id) WHERE generation_id IS NOT NULL` — partial unique index, created in `migrate()`, the documented race fix for two clients persisting one detached generation ([db.py:173-179](../../orchestrator/app/db.py#L173)) | [db.py:187-191](../../orchestrator/app/db.py#L187) |

**No index exists on `messages.content`** — the `LIKE` scans at
[db.py:963](../../orchestrator/app/db.py#L963), [:972](../../orchestrator/app/db.py#L972) and
[:995](../../orchestrator/app/db.py#L995) are full table scans.

**Control flow** — `connect()` ([db.py:195-205](../../orchestrator/app/db.py#L195)), executed by **every**
accessor in the file: `Path(settings.app_db_path)` (:197) → `path.parent.mkdir(parents=True, exist_ok=True)`
(:198, a filesystem write) → `sqlite3.connect(str(path))` (:199, **no `timeout=` argument** ⇒ the stdlib 5.0 s
busy timeout; `check_same_thread` left at `True`) → `row_factory = sqlite3.Row` (:200) →
`PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON` (:201-202) → `con.executescript(_SCHEMA)` (:203) →
`migrate(con)` (:204). `migrate` ([db.py:153-192](../../orchestrator/app/db.py#L153)) runs two
`PRAGMA table_info` queries, conditional `ALTER TABLE … ADD COLUMN` for `pinned`/`archived`/`generation_id`, the
**unconditional** dedupe `DELETE` ([:181-186](../../orchestrator/app/db.py#L181)), the partial
`CREATE UNIQUE INDEX IF NOT EXISTS` ([:187-191](../../orchestrator/app/db.py#L187)) and `con.commit()` (:192).

`add_message` ([db.py:678-748](../../orchestrator/app/db.py#L678)): `utcnow()` + extract `meta["generation_id"]`
(:695-696) → `with closing(connect()) as con, con:` (:697) → ownership probe
`SELECT 1 FROM conversations WHERE id=? AND user_id=?` (:698-703) → nested closure `_existing_generation_row()`
(:704-719) → `INSERT INTO messages (…, generation_id)` (:721-735) → on `sqlite3.IntegrityError`, if a
`generation_id` was supplied return the winning row with `"deduplicated": True`, **otherwise re-raise**
(:736-743) → `UPDATE conversations SET updated_at` (:744-746).

`replace_messages` ([db.py:616-675](../../orchestrator/app/db.py#L616)): ownership probe (:628-633) → `COUNT(*)`
raising `MessageCountWouldShrink` when the payload is shorter (:634-641) →
`DELETE FROM messages WHERE conversation_id = ?` — **every row** (:642-644) → re-INSERT each message with a single
shared `created_at` (:645-670), de-duplicating repeated in-payload `generation_id` (:652-656) → bump `updated_at`
(:671-674). `truncate_messages` ([db.py:571-613](../../orchestrator/app/db.py#L571)) uses optimistic concurrency
on `expected_total`, raising `ConversationChanged` (:593-600).

`search_repo_chunks` ([db.py:844-894](../../orchestrator/app/db.py#L844)) builds SQL by f-string concatenation of
*placeholder* fragments (:854-874) and binds every user value as a parameter in a hand-ordered list (:877-883),
using `ESCAPE '\'` consistently — no injection, only the number of `?` varies with input.
`search_conversations` ([db.py:1022-1064](../../orchestrator/app/db.py#L1022)) trims the query, clamps `limit` to
`[1, SEARCH_LIMIT_MAX]` (:1036), runs `_SEARCH_SQL` with a named `:pattern` (:1038-1045), then builds
`snippet`/`matched_in` (:1046-1063).

**State & side effects** — Filesystem: `path.parent.mkdir(...)` on every `connect()`
([:198](../../orchestrator/app/db.py#L198)); the SQLite file plus `-wal` and `-shm` at `settings.app_db_path`.
DB writes: `create_user` (:216), `create_conversation` (:271), `update_conversation` (:324),
`delete_conversation` (:336), `truncate_messages` (:603,609), `replace_messages` (:642,657,671), `add_message`
(:722,744), `save_summary` (:415), `clear_summary` (:434,438), `add_conversation_chunks` (:451), `save_upload`
(:500), `save_url_document` (:760), `save_repo` (:795), `replace_repo_chunks` (:829,833) — **plus** the migration
`DELETE` + `CREATE INDEX` + `commit()` on every `connect()`. Network egress: **none**. GPU calls: **none**.
Global mutation: **none** — the module has no module-level mutable state. Import-time side effects: none.

**Dependencies** — Inbound: [auth.py:30,48,56,63,79,82](../../orchestrator/app/auth.py#L30);
[history.py:27](../../orchestrator/app/history.py#L27) (every route);
[uploads.py:23,76,124,130,141,164,167](../../orchestrator/app/uploads.py#L23);
[health.py:76,79,81](../../orchestrator/app/health.py#L76); [recall.py:27](../../orchestrator/app/recall.py#L27);
[memory_recall.py:71](../../orchestrator/app/memory_recall.py#L71);
[main.py:16,162,339,469,487,527,758,770](../../orchestrator/app/main.py#L16);
[engines/repo.py:13,122](../../orchestrator/app/engines/repo.py#L13); `engines/dataset.py:23`; `engines/url.py:15`.
Outbound: stdlib `json`, `sqlite3`, `contextlib.closing`, `datetime`, `pathlib.Path`, `typing`
([db.py:13-18](../../orchestrator/app/db.py#L13)); `app.config.settings` (:20).

**Config** — `settings.app_db_path` ([db.py:197](../../orchestrator/app/db.py#L197)) — the only setting this
module reads. Note it is **not set in `docker-compose.yml`**, so the `/data/app.sqlite3` default
([config.py:255](../../orchestrator/app/config.py#L255)) is what runs.

**Failure modes** — `create_user` and `create_conversation` raise `sqlite3.IntegrityError` on duplicates
([:213-214](../../orchestrator/app/db.py#L213), [:268](../../orchestrator/app/db.py#L268));
`truncate_messages` raises `ConversationChanged` (:600); `replace_messages` raises `MessageCountWouldShrink`
(:641); `add_message` re-raises `IntegrityError` when no `generation_id` was supplied (:743) → HTTP 500.
**Nothing is swallowed inside `db.py`** — there is no bare `except` in the file; swallowing happens in callers
([main.py:340,472,494,528,161](../../orchestrator/app/main.py#L340)). No busy-timeout tuning (:199) ⇒
`sqlite3.OperationalError: database is locked` after 5 s under write contention, surfacing as a 500. No bound on
`list_messages` (:343-354), `get_conversation_chunks` (:466), `get_url_documents` (:769) or `get_uploads` (:518) —
whole-table reads per conversation with unbounded row counts and unbounded `text`/`embedding` sizes. No
pruning/TTL for `uploads`, `url_documents`, `repos`, `repo_chunks` or `conversation_chunks`.

**Concurrency** — Every function is **synchronous**. Callers in `history.py` are plain `def` routes so FastAPI
runs them in the anyio threadpool; callers in `main.py` are `async def` and call these blocking functions
directly on the event loop ([main.py:339,471,493,527,758,770,162](../../orchestrator/app/main.py#L339)). No
connection is shared across tasks or threads: `connect()` returns a fresh connection and `closing(...)` closes it
in the same call, so `check_same_thread=True` is safe under this pattern. Cross-client races are resolved in SQL —
the partial unique index `idx_messages_generation` ([:187-191](../../orchestrator/app/db.py#L187)) and
`truncate_messages`'s optimistic `expected_total` check ([:599-600](../../orchestrator/app/db.py#L599)).

**Complexity hotspots** (measured with `ast`)

| Function | LOC | `file:line` |
|---|---|---|
| `add_message` | **71** — nested function defined inside a transaction context, plus a try/except-with-fallback-return | [db.py:678](../../orchestrator/app/db.py#L678) |
| `replace_messages` | 60 | [db.py:616](../../orchestrator/app/db.py#L616) |
| `search_repo_chunks` | 51 — three separately-built SQL fragments plus a hand-ordered parameter list whose correctness depends on a comment ([db.py:875-876](../../orchestrator/app/db.py#L875)) | [db.py:844](../../orchestrator/app/db.py#L844) |
| `search_conversations` / `truncate_messages` | 43 each | [db.py:1022](../../orchestrator/app/db.py#L1022) / [db.py:571](../../orchestrator/app/db.py#L571) |
| `migrate` | 40 | [db.py:153](../../orchestrator/app/db.py#L153) |
| `recall_conversations` | 39 | [db.py:981](../../orchestrator/app/db.py#L981) |

**Findings** — `PERF-03` (this module is the finding's origin: [db.py:195-205](../../orchestrator/app/db.py#L195)),
`DATA-03` ([db.py:84,102,114,123](../../orchestrator/app/db.py#L84) declare no FK, so
[`delete_conversation`](../../orchestrator/app/db.py#L334) orphans them permanently — and since conversation ids
are client-supplied, a recreated id inherits the old rows). Unassigned observations: the ownership probe
`SELECT 1 FROM conversations WHERE id = ? AND user_id = ?` is written verbatim three times
([:588-590](../../orchestrator/app/db.py#L588), [:629-631](../../orchestrator/app/db.py#L629),
[:699-701](../../orchestrator/app/db.py#L699)); `migrate`'s destructive `DELETE` is written as a one-time repair
but has no guard making it one-time; `list_messages` ([:343](../../orchestrator/app/db.py#L343)) is the only
conversation accessor with no ownership parameter.

---

## config.py

**Purpose** — One `Settings` object built from `os.environ` at import time; the single source of truth for every
tunable in the orchestrator.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_TRUTHY` | `{"1","true","yes","on"}` | [config.py:13](../../orchestrator/app/config.py#L13) |
| `_bool` / `_int` / `_float` | `(name, default) -> bool/int/float` | [config.py:16](../../orchestrator/app/config.py#L16), [:23](../../orchestrator/app/config.py#L23), [:30](../../orchestrator/app/config.py#L30) |
| `CHART_TRIGGER_MODES` | `("explicit","hybrid")` | [config.py:38](../../orchestrator/app/config.py#L38) |
| `Settings` / `Settings.__init__` | class / 225-LOC constructor | [config.py:41](../../orchestrator/app/config.py#L41), [:44](../../orchestrator/app/config.py#L44) |
| `settings` | module-level singleton, constructed on import | [config.py:271](../../orchestrator/app/config.py#L271) |

~90 environment variables are read. Grouped by concern, with the line of each block:

| Block | Lines | Notable members |
|---|---|---|
| Model endpoints | [config.py:46-83](../../orchestrator/app/config.py#L46) | `openai_base_url` (the **only** base URL not `.rstrip("/")`-normalised, :46), `llm_model`, `router_base_url`, `vision_base_url`, `embed_base_url`, `embed_model` |
| Rerank | [:86-93](../../orchestrator/app/config.py#L86) | `rerank_enabled`, `rerank_model` |
| Data plane paths | [:96-100](../../orchestrator/app/config.py#L96) | `duckdb_path`, `lancedb_dir`, `lancedb_table`, `parquet_dir`, `reports_dir` |
| Salesforce | [:103-126](../../orchestrator/app/config.py#L103) | `sf_client_id`, `sf_client_secret`, `sf_private_key_b64`, `sf_api_version`, `sf_live_timeout` (**raw `float(...)`, not `_float`**, :123), `sf_live_enabled` (ad-hoc truthiness, :124-126) |
| Token budget | [:127-160](../../orchestrator/app/config.py#L127) | `model_max_context` 262144, `model_max_output` 8192, `context_safety_margin` 512, `tokenize_timeout` 5.0, `context_warn_threshold` 0.60 (**0 readers**), `context_bg_compact_threshold` 0.70, `context_compact_threshold` 0.80, `keep_recent_turns` 8, `summary_max_tokens` 2000, `min_output_floor` 1024 |
| Semantic recall | [:163-167](../../orchestrator/app/config.py#L163) | `semantic_recall_enabled`, `retrieve_top_k` 6, `context_meter_enabled` (**0 readers**) |
| Uploads / archives / profiling | [:171-188](../../orchestrator/app/config.py#L171) | `dataset_uploads_enabled`, `upload_max_mb` 200, `archive_max_*`, `profile_*` |
| Web search | [:192-204](../../orchestrator/app/config.py#L192) | see `orchestrator-search.md` |
| URL / repo phases | [:207-217](../../orchestrator/app/config.py#L207) | `url_analysis_enabled`, `url_max_pages` 5, `repo_analysis_enabled`, `workspace_dir`, `workspace_ttl_hours`, `workspace_quota_gb` |
| Charts / exports / RAG | [:230-239](../../orchestrator/app/config.py#L230) | `chart_trigger_mode` (**the only whitelist-validated setting**, :230-231), `sql_preview_row_cap`, `export_row_cap`, `rag_top_k`, `rag_final_k` |
| App plumbing | [:244-268](../../orchestrator/app/config.py#L244) | `cors_allow_origins` (`*` accepted verbatim, :244-250), `app_db_path`, `session_secret_file` (**0 readers**), `session_max_turns` 20, `llm_request_timeout` 300.0, `schema_cache_ttl` (**0 readers**), `health_probe_timeout` 2.0 |

Dead settings verified with `rg -c 'settings\.<name>' orchestrator/app/`: `default_max_context`,
`report_max_context`, `schema_cache_ttl`, `agent_base_url`, `agent_model`, `context_meter_enabled`,
`session_secret_file` — all **0**. `context_warn_threshold` ([config.py:146](../../orchestrator/app/config.py#L146))
has 0 readers anywhere in the repository despite being shipped in `.env.example`.

**Control flow** — Importing `config` runs `from .core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP`
([:11](../../orchestrator/app/config.py#L11), pure constants) and then `settings = Settings()`
([:271](../../orchestrator/app/config.py#L271)), which executes `__init__`
([:44-268](../../orchestrator/app/config.py#L44)) top to bottom. `_bool` ([:16-20](../../orchestrator/app/config.py#L16)):
`None` or blank ⇒ default, else case-insensitive membership in `_TRUTHY` — anything else is `False`.
`_int` / `_float` ([:23-34](../../orchestrator/app/config.py#L23)): `None` or blank ⇒ default, else `int(raw)` /
`float(raw)` with **no try/except and no range check**.

**State & side effects** — Env reads only (~90). No network, no filesystem, no DB, no GPU. The object is a
process-lifetime singleton; changing an env var after import has no effect. Tests mutate `settings` attributes
directly (`orchestrator/tests/conftest.py:25`, `tests/test_history.py:13`).

**Dependencies** — Inbound: everything —
`llm.py:25`, `db.py:20`, `health.py:21`, `uploads.py:25`, `search/base.py:13`, `context.py`, `main.py:18`, every
engine. Outbound: `os` ([:9](../../orchestrator/app/config.py#L9)), `app.core.exports`
([:11](../../orchestrator/app/config.py#L11)).

**Config** — This module *is* the config surface; see the table above. Secret-bearing settings, by **name** only:
`openai_api_key` [config.py:48](../../orchestrator/app/config.py#L48), `sf_client_secret`
[:119](../../orchestrator/app/config.py#L119), `sf_private_key_b64` [:121](../../orchestrator/app/config.py#L121),
`tavily_api_key` [:195](../../orchestrator/app/config.py#L195), `brave_api_key`
[:196](../../orchestrator/app/config.py#L196). `.env`, `.env.bak-*` and `secrets/` are git-ignored
(`.gitignore:10,47,13`) and `git ls-files` shows only `.env.example` tracked — **no secret is committed**.

**Failure modes**

- **`_int`/`_float` raise an unhandled `ValueError` at import.** `UPLOAD_MAX_MB=200MB` ⇒ `int("200MB")` at
  [:27](../../orchestrator/app/config.py#L27) ⇒ `import app.main` fails and uvicorn never starts, with a raw
  traceback and no indication of which variable is bad.
- `sf_live_timeout` ([:123](../../orchestrator/app/config.py#L123)) uses raw
  `float(os.environ.get("SF_LIVE_TIMEOUT", "45"))` rather than `_float`, so `SF_LIVE_TIMEOUT=`
  (present-but-empty — the shape docker-compose's `${VAR:-}` produces) is `float("")` ⇒ `ValueError` at import,
  where every other numeric setting tolerates it.
- `sf_live_enabled` ([:124-126](../../orchestrator/app/config.py#L124)) uses a hand-rolled truthiness test;
  `SF_LIVE_ENABLED=off` evaluates **True** while `_bool` would give **False**.
- No range validation anywhere except `chart_trigger_mode`. Negative or zero values are accepted for
  `upload_max_mb`, `search_max_results`, `keep_recent_turns`, `rag_top_k`, `workspace_quota_gb` and the three
  context thresholds.
- `cors_allow_origins` accepts `*` verbatim ([:244-250](../../orchestrator/app/config.py#L244)); combined with
  `allow_credentials=True` ([main.py:50](../../orchestrator/app/main.py#L50)) that is the classic CORS
  misconfiguration.
- Nothing is swallowed — there is no `try`/`except` in the file.

**Concurrency** — Synchronous, executed once at import. `settings` is a shared mutable object read from every
thread and from the event loop; nothing in `app/` mutates it at runtime (only tests do), so there is no production
race.

**Complexity hotspots** — `Settings.__init__` ([config.py:44](../../orchestrator/app/config.py#L44)) is
**225 LOC** — the largest function in the orchestrator. It is straight-line, so cyclomatic complexity is low
(~8 branches from `or` fallbacks and the `chart_trigger_mode` guard), but it is one unreadable block with 20
comment-delimited sections and no grouping into sub-objects.

**Findings** — `SEC-06` (the dead AWS Secrets Manager block is solicited by `.env.example:7-14` with **0**
references anywhere in code — `config.py` reads none of those variables). Unassigned observations: 7 dead
settings; `_int`/`_float` crash the process at import on a malformed value; `SF_LIVE_TIMEOUT` and
`SF_LIVE_ENABLED` use bespoke coercion inconsistent with the rest of the file.

---

## health.py

**Purpose** — Concurrent dependency probes behind `GET /health`: the deduplicated vLLM endpoints, the DuckDB
warehouse, and the app SQLite DB.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `service_root` | `(base_url: str) -> str` | [health.py:24](../../orchestrator/app/health.py#L24) |
| `_probe_vllm` | `async (client: httpx.AsyncClient, base_url: str) -> dict` | [health.py:36](../../orchestrator/app/health.py#L36) |
| `_check_duckdb` | `(path: str) -> dict` | [health.py:48](../../orchestrator/app/health.py#L48) |
| `_check_app_db` | `(path: str) -> dict` | [health.py:68](../../orchestrator/app/health.py#L68) |
| `check_dependencies` | `async () -> {"status": "ok"\|"degraded", "checks": {name: {...}}}` | [health.py:94](../../orchestrator/app/health.py#L94) |

The HTTP route itself lives in `main.py`: `@app.get("/health")` / `async def health()` —
[main.py:242-254](../../orchestrator/app/main.py#L242).

**Control flow** — `check_dependencies` ([health.py:94-131](../../orchestrator/app/health.py#L94))

1. Build the fixed list `[("vllm", openai_base_url), ("vllm-router", router_base_url), ("vllm-vision",
   vision_base_url), ("vllm-embed", embed_base_url)]` ([:105-110](../../orchestrator/app/health.py#L105)).
2. Dedupe by **exact URL string** ([:111-117](../../orchestrator/app/health.py#L111)); with the shipped compose
   values `vllm` and `vllm-vision` collapse into one probe.
3. `async with httpx.AsyncClient(timeout=settings.health_probe_timeout)`
   ([:119](../../orchestrator/app/health.py#L119)).
4. `asyncio.gather` of N `_probe_vllm` coroutines **plus** `asyncio.to_thread(_check_duckdb, ...)` **plus**
   `asyncio.to_thread(_check_app_db, ...)` ([:120-124](../../orchestrator/app/health.py#L120)).
5. `_probe_vllm` ([:36-45](../../orchestrator/app/health.py#L36)): `GET {service_root(base)}/health`; any
   exception → `{"status": "error", "detail": f"{type(exc).__name__}: {exc}"}` (:41-42); non-200 →
   `detail = f"HTTP {resp.status_code} from {url}"` (:45).
6. `_check_duckdb` ([:48-65](../../orchestrator/app/health.py#L48)): lazy `import duckdb`,
   `duckdb.connect(path, read_only=True, config={"enable_external_access": False})` (:54-58), `SELECT 1`,
   `close()` in `finally`.
7. `_check_app_db` ([:68-91](../../orchestrator/app/health.py#L68)): lazy `from . import db` (:76),
   `db.closing(db.connect())` (:79) — **this runs the whole `_SCHEMA` + `migrate()` write path** — then
   `PRAGMA table_info(messages)` asserting `generation_id` is present (:80-88).
8. Zip names to results, append `duckdb` and `app_db` ([:125-129](../../orchestrator/app/health.py#L125));
   overall is `"ok"` only if every check is `"ok"`, else `"degraded"` ([:130](../../orchestrator/app/health.py#L130)).

**State & side effects** — Network egress: `GET http://vllm:30000/health`, `http://vllm-router:30002/health`,
`http://vllm-embed:30003/health` (derived from `settings.*_base_url`,
[:38](../../orchestrator/app/health.py#L38)). Filesystem: opens `settings.duckdb_path` read-only
([:54](../../orchestrator/app/health.py#L54)); opens/creates `settings.app_db_path` **read-write** via
`db.connect()` ([:79](../../orchestrator/app/health.py#L79)), which `mkdir`s the parent and executes the
migration DDL/DML. **DB writes: yes, indirectly** — every `/health` hit re-runs `executescript(_SCHEMA)` and
`migrate()`'s `DELETE … CREATE UNIQUE INDEX … commit()`. No GPU calls, no global mutation, no direct env reads.

**Dependencies** — Inbound: [main.py:21](../../orchestrator/app/main.py#L21), called at
[main.py:248](../../orchestrator/app/main.py#L248). Outbound: `asyncio`, `typing`, `httpx`
([health.py:19](../../orchestrator/app/health.py#L19)), `app.config.settings` (:21), lazy `duckdb` (:51), lazy
`app.db` (:76).

**Config** — `settings.health_probe_timeout` (:119), `settings.openai_base_url` (:106),
`settings.router_base_url` (:107), `settings.vision_base_url` (:108), `settings.embed_base_url` (:109),
`settings.duckdb_path` (:122), `settings.app_db_path` (:123).

**Failure modes**

- All three probes catch bare `Exception` by design ([:41](../../orchestrator/app/health.py#L41),
  [:63](../../orchestrator/app/health.py#L63), [:90](../../orchestrator/app/health.py#L90), the last annotated
  `# noqa: BLE001`) so `/health` never 500s.
- **`settings.health_probe_timeout` bounds only the HTTP probes.** The two `asyncio.to_thread(...)` calls
  ([:122-123](../../orchestrator/app/health.py#L122)) have **no timeout**; `db.connect()` inherits sqlite3's 5 s
  busy timeout and DuckDB its own lock behaviour, so `/health` can take well over the documented 2 s despite the
  docstring's "/health stays fast even when every dependency is down"
  ([:10-12](../../orchestrator/app/health.py#L10)).
- No retry, no caching — every call re-probes everything and re-runs the SQLite migration.
- **`/health` returns HTTP 200 even when `status == "degraded"`**
  ([main.py:243-254](../../orchestrator/app/main.py#L243) returns a plain dict). Any monitor keying on the status
  code sees a healthy service.
- **Liveness, not readiness, for the model layer**: vLLM's `GET /health` reports that the engine process is up. It
  does **not** confirm the *configured model id* is served, so a vLLM started with a different
  `--served-model-name` reports `ok` here while every `chat.completions.create` 404s. `GET /v1/models` would catch
  that and is not probed.
- Information disclosure: `detail` strings embed exception text and internal URLs
  ([:42,45,64,91](../../orchestrator/app/health.py#L42)) on an endpoint with no authentication.

**Concurrency** — `check_dependencies` is async and the two blocking probes are correctly moved off the loop with
`asyncio.to_thread` ([:122-123](../../orchestrator/app/health.py#L122)) — the reference pattern the rest of the
service does not follow. No shared mutable state; `seen` and `vllm_targets` are per-call locals.
`asyncio.gather` without `return_exceptions=True` is safe here because every probe swallows its own exceptions.

**Complexity hotspots** — `check_dependencies` ([health.py:94](../../orchestrator/app/health.py#L94)) is 38 LOC.
No function over 60 LOC. The positional result slicing at [:126,128-129](../../orchestrator/app/health.py#L126)
(`results[:-2]`, `results[-2]`, `results[-1]`) silently breaks if another `to_thread` probe is appended.

**Findings** — `SEC-01` (unauthenticated, and it discloses internal URLs and exception text), `PERF-03` (a SQLite
migration write per `/health` call), `OBS-01`. Unassigned observations: `/health` 200s while degraded; it probes
liveness rather than model readiness; `service_root` ([health.py:24-33](../../orchestrator/app/health.py#L24)) is
duplicated verbatim in [context.py:70-73](../../orchestrator/app/context.py#L70).

---

## uploads.py

**Purpose** — `POST /uploads` streams a dataset/archive to disk under the per-conversation workspace, extracts it,
profiles it, and stores the profile in SQLite; `GET /uploads/{id}` lists a conversation's uploads and marks
TTL-swept ones expired.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `router` | `APIRouter(prefix="/uploads", tags=["uploads"])` | [uploads.py:28](../../orchestrator/app/uploads.py#L28) |
| `_CHUNK` | `1024 * 1024` | [uploads.py:30](../../orchestrator/app/uploads.py#L30) |
| `upload_root` | `(conversation_id: str, upload_id: str) -> str` | [uploads.py:33](../../orchestrator/app/uploads.py#L33) |
| `bytes_available` | `(conversation_id: str, upload_id: str) -> bool` | [uploads.py:38](../../orchestrator/app/uploads.py#L38) |
| `_stream_to_disk` | `async (upload: UploadFile, dest: str) -> int` | [uploads.py:44](../../orchestrator/app/uploads.py#L44) |
| `create_upload` | `POST /uploads` — `async (file: UploadFile = File(...), conversation_id: str = Form(...), user = Depends(require_user)) -> dict` | [uploads.py:66-157](../../orchestrator/app/uploads.py#L66) |
| `list_uploads` | `GET /uploads/{conversation_id}` — sync `def` | [uploads.py:160-172](../../orchestrator/app/uploads.py#L160) |

**Control flow** — `create_upload`

1. `if not settings.dataset_uploads_enabled: 404` ([:72-73](../../orchestrator/app/uploads.py#L72)).
2. Ownership: `db.conversation_owner(conversation_id)`; reject only when `owner is not None and owner !=
   user["id"]` ([:76-78](../../orchestrator/app/uploads.py#L76)) — an **unknown conversation id is accepted**.
3. `upload_id = uuid4().hex`; `root = upload_root(...)`;
   `filename = os.path.basename(file.filename or "upload.bin")`; `raw_path = root/_original/<filename>`
   ([:80-83](../../orchestrator/app/uploads.py#L80)).
4. `enforce_quota_and_ttl()` wrapped in **`except Exception: pass`** ([:85-90](../../orchestrator/app/uploads.py#L85)).
5. `size = await _stream_to_disk(file, raw_path)` ([:92](../../orchestrator/app/uploads.py#L92)) — **outside the
   try block**. `_stream_to_disk` ([:45-63](../../orchestrator/app/uploads.py#L45)) caps at
   `upload_max_mb * 1024 * 1024`, `os.makedirs(dirname(dest))`, then a 1 MiB read loop; on overflow it closes,
   `os.unlink(dest)` and raises `HTTPException(413)` (:55-61).
6. Extraction branch ([:96-121](../../orchestrator/app/uploads.py#L96)): zip container and not `.xlsx` →
   `archive.extract`; `.tar/.tar.gz/.tgz` or gzip sniff → `archive.extract`; else a single file, with
   `archive.check_zip_container(..., label="spreadsheet")` for `.xlsx` and then `shutil.copy2` into `extracted/`.
   `plan.skipped` / `plan.nested_archives` fold into `notes`; `profiles = profiler.profile_directory(extract_dir)`.
7. `except archive.ArchiveError` → `rmtree(root)`, `db.save_upload(status="rejected", notes=str(exc))`,
   `HTTPException(400, detail=str(exc))` ([:122-127](../../orchestrator/app/uploads.py#L122)).
8. `except Exception` → `rmtree(root)`, `db.save_upload(status="failed", notes=type(exc).__name__)`, generic
   `HTTPException(400)` ([:128-136](../../orchestrator/app/uploads.py#L128)).
9. `rmtree(root/_original)` (:139), `db.save_upload(..., "ready", profiler.profile_json(profiles), …)`
   (:141-149), return `{upload_id, filename, bytes, files, notes[:20], profile}` (:150-157).

Validation actually performed: **size** yes (streamed cap at [:55](../../orchestrator/app/uploads.py#L55));
**MIME** none — `file.content_type` is never read; **extension** only to choose the extraction path
([:97,101](../../orchestrator/app/uploads.py#L97)), never to reject; **filename** `os.path.basename` only
([:82](../../orchestrator/app/uploads.py#L82)); **content** zip-bomb caps via `core/archive.py` and the
`archive_max_*` settings.

Storage path: `settings.workspace_dir + "/uploads/" + sanitized_conversation_id[:64] + "/" + upload_id`
([:33-35](../../orchestrator/app/uploads.py#L33)), with `_original/` and `extracted/` sub-paths.

**State & side effects** — Filesystem writes: `os.makedirs` ([:48,111](../../orchestrator/app/uploads.py#L48)),
the streamed file (:49-62), `shutil.copy2` (:112), `shutil.rmtree` (:123,129,139), plus everything
`archive.extract` writes under `extract_dir`. Filesystem **deletes outside this request's tree**:
`enforce_quota_and_ttl` ([:88](../../orchestrator/app/uploads.py#L88) →
[core/repo.py:94-119](../../orchestrator/app/core/repo.py#L94)) `rmtree`s top-level entries of
`settings.workspace_dir`. DB writes: `db.save_upload` at [:124,130,141](../../orchestrator/app/uploads.py#L124).
DB reads: `db.conversation_owner` (:76,164), `db.get_uploads` (:167). No network egress, no GPU calls, no
module-level mutable state, no direct env reads.

**Dependencies** — Inbound: [main.py:23](../../orchestrator/app/main.py#L23) (mounted at
[main.py:59](../../orchestrator/app/main.py#L59)); `db.get_uploads` is also read by
[main.py:527](../../orchestrator/app/main.py#L527) to set `dataset_ready`. Outbound: `os`, `shutil`, `sqlite3`,
`uuid`, `typing`, `fastapi`; `app.db` ([:23](../../orchestrator/app/uploads.py#L23)), `app.auth.require_user`
(:24), `app.config.settings` (:25), `app.core.archive` + `app.core.profile` (:26); lazy
`app.core.repo.enforce_quota_and_ttl` (:86).

**Config** — `settings.workspace_dir` (:35), `settings.upload_max_mb` (:46,60),
`settings.dataset_uploads_enabled` (:72); indirectly `workspace_ttl_hours` / `workspace_quota_gb` through
`enforce_quota_and_ttl`.

**Failure modes**

- `except Exception: pass` around `enforce_quota_and_ttl` ([:89-90](../../orchestrator/app/uploads.py#L89)) —
  deliberate ("housekeeping only; never blocks an upload") but a wedged quota sweep is invisible.
- `except Exception` at [:128](../../orchestrator/app/uploads.py#L128) swallows the real cause and stores only
  `type(exc).__name__` (:132) — no message, no traceback, and there is **no logging call anywhere in the file**.
- **`_stream_to_disk` sits outside the try/except** ([:92](../../orchestrator/app/uploads.py#L92) vs. the `try` at
  [:96](../../orchestrator/app/uploads.py#L96)), so an `OSError` there leaves the partially-written tree on disk
  **and** returns a 500 with a traceback rather than a 4xx. Reachable: a multipart `filename` of `"/"` makes
  `os.path.basename("/") == ""`, so `dest` becomes `<root>/_original/` and `open(dest, "wb")` raises
  `IsADirectoryError`; a `filename` of `".."` gives `dest = <root>/_original/..`, also a directory.
- `HTTPException(413)` at [:58](../../orchestrator/app/uploads.py#L58) is raised inside the `with open(...)` block
  after `out.close()` and `os.unlink(dest)`; the partial parent directories remain.
- `bytes_available` ([:41](../../orchestrator/app/uploads.py#L41)) calls `any(os.scandir(root))` and never closes
  the iterator — it relies on refcount finalisation for the directory fd.
- No timeout on the handler; a 200 MB archive extraction plus profiling runs unbounded. No retry. No bound on the
  **number** of uploads per conversation or per process, and no TTL/pruning of the `uploads` rows in SQLite.

**Concurrency**

- `create_upload` is `async def` ([:67](../../orchestrator/app/uploads.py#L67)) but **almost all of its work is
  blocking**: `archive.extract` (:99,103), `archive.check_zip_container` (:110), `shutil.copy2` (:112),
  `profiler.profile_directory` (:121), `shutil.rmtree` (:123,129,139), `enforce_quota_and_ttl` (:88) and every
  `db.*` call (:76,124,130,141) run **on the event loop**. Only `await upload.read()`
  ([:51](../../orchestrator/app/uploads.py#L51)) yields. A 200 MB zip extraction stalls every other request,
  including live SSE chat streams.
- `list_uploads` is a sync `def` ([:161](../../orchestrator/app/uploads.py#L161)) → threadpool. Correct.
- Race window: `enforce_quota_and_ttl` runs concurrently with any other in-flight upload's writes;
  [core/repo.py:108,119](../../orchestrator/app/core/repo.py#L108) `rmtree` a top-level workspace entry with no
  lock, so a concurrent upload writing under that entry loses its files mid-write.

**Complexity hotspots** — `create_upload` ([uploads.py:67](../../orchestrator/app/uploads.py#L67)) is **91 LOC**
(measured with `ast`): a 3-way extraction branch, two exception handlers, and two `db.save_upload` call sites
duplicating the same 8-argument list. Cyclomatic ≈ 12.

**Findings** — `SEC-01` (`Depends(require_user)` never 401s; the permissive ownership rule at
[:77,165](../../orchestrator/app/uploads.py#L77) accepts an unknown conversation id, contrasting with the strict
form at [main.py:759](../../orchestrator/app/main.py#L759)), `REL-03`, `DATA-03` (the `uploads` table has no FK,
so `delete_conversation` orphans its rows). Unassigned observations: `_stream_to_disk` outside the try/except
yields a 500 on a hostile multipart filename; no MIME or extension rejection; `enforce_quota_and_ttl` treats
`<workspace_dir>/uploads` as a single top-level entry ([core/repo.py:94-119](../../orchestrator/app/core/repo.py#L94))
while every upload lives at `<workspace_dir>/uploads/<conv>/<id>`, so one TTL expiry deletes every conversation's
extracted uploads at once.
