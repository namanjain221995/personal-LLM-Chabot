# Evidence — `orch-context` (orchestrator entrypoint, graph, context lifecycle, SSE)

> **⚠ Superseded in part (2026-08-10).** The app-state layer described below was
> `/data/app.sqlite3` (stdlib `sqlite3`). It is now PostgreSQL — see
> [`data-model.md`](../01-codebase/data-model.md) and the CHANGELOG entry
> "App state moved from SQLite to PostgreSQL". Every `sqlite3` reference,
> `db.py` line number and finding about SQLite locking below is a snapshot of
> the pre-migration code and has NOT been re-derived. The DuckDB warehouse and
> LanceDB sections are unaffected and remain accurate.

Scope: `orchestrator/app/{main,graph,context,compaction,summarize,memory,memory_recall,recall,history,sse}.py`,
all read in full. Supporting files read in full for cross-referencing: `orchestrator/app/auth.py`,
`orchestrator/app/config.py`, `orchestrator/app/health.py`, `orchestrator/app/uploads.py`,
`orchestrator/app/llm.py`, `orchestrator/app/core/report_paths.py`. Partially read (cited ranges only):
`orchestrator/app/db.py` (lines 140-260, 343-493, 560-760, 900-1064), `orchestrator/app/engines/*`
(targeted ranges cited inline), `frontend/lib/streams.ts` (lines 100-340), `docker-compose.yml` (lines 208-290),
`orchestrator/Dockerfile` (line 52).

LOC per `wc -l`: main 796, graph 117, context 275, compaction 360, summarize 116, memory 35,
memory_recall 75, recall 144, history 288, sse 85. **Total 2291.**

---

## Cross-cutting facts established up front

- **There is no authentication anywhere in the service.** `auth.py:89-92` `current_user()` deletes the
  `Request` argument and returns `local_user()`; `auth.py:95-97` `require_user()` is `return current_user(request)`
  and its own docstring says "Never 401s now". `auth.py:17-20` states this explicitly as a security note.
  Consequence: every `signed_in is not None` / `user is None` branch in `main.py` and `history.py` is
  dead-on-the-false-side, and every `user_id` ownership comparison compares the single local id to itself.
- **Single process, single event loop.** `orchestrator/Dockerfile:52` →
  `uvicorn app.main:app --host 0.0.0.0 --port 8080` with no `--workers`. All module-level dicts in
  `main.py:125,130`, `compaction.py:35,41`, `context.py:66`, `memory.py:35` are therefore process-global and
  consistent, and every blocking call inside an `async def` stalls **all** concurrent SSE streams.
- **`db.connect()` is not a pooled/cheap call.** `db.py:195-205` opens a fresh `sqlite3.connect`, sets
  `journal_mode=WAL` and `foreign_keys=ON`, then runs `con.executescript(_SCHEMA)` (db.py:203) and
  `migrate(con)` (db.py:204) on **every** invocation. `migrate()` (db.py:153-192) runs two `PRAGMA table_info`
  queries, a `DELETE FROM messages WHERE generation_id IS NOT NULL AND id NOT IN (SELECT MIN(id) … GROUP BY …)`
  (db.py:181-186), a `CREATE UNIQUE INDEX IF NOT EXISTS` (db.py:187-191) and `con.commit()` (db.py:192).
  Every `db.*` helper in this evidence file wraps `closing(connect())`, so each one pays that cost.
- **Only `main.py` imports `sse.py`** (`rg` confirms: `orchestrator/app/main.py:25` is the sole non-test
  importer). Engines never format SSE; they call the `emit(event, data)` closure defined at `main.py:364`.

---

### orchestrator/app/main.py  (796 LOC)

**Purpose** — FastAPI entrypoint: mounts the auth/history/uploads routers, owns the detached-generation
registry (`LiveGeneration`), and implements `/health`, `/reports*`, `/chat`, `/chat/stop`, `/chat/active`,
`/chat/compact`, `/chat/attach/{id}`.

**Public surface**

| Symbol | Signature / route | `path:LINE` |
|---|---|---|
| `lifespan` | `async ctxmgr lifespan(_app: FastAPI)` | `main.py:27-38` |
| `app` | `FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` | `main.py:41` |
| CORS middleware | `CORSMiddleware(allow_origins=settings.cors_allow_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])` | `main.py:47-53` |
| router mounts | `auth_router` / `history_router` / `uploads_router` | `main.py:57,58,59` |
| `LiveGeneration` | `class LiveGeneration` | `main.py:62` |
| `LiveGeneration.__init__` | `(conversation_id: Optional[str], user_id: Optional[int])` | `main.py:78-93` |
| `LiveGeneration.publish` | `async (event: str, data: dict) -> None` | `main.py:95-98` |
| `LiveGeneration.finish` | `async () -> None` | `main.py:100-103` |
| `LiveGeneration.follow` | `async () -> AsyncIterator[str]` | `main.py:105-120` |
| `_live_generations` | `dict` — conv_key → LiveGeneration | `main.py:125` |
| `_background_tasks` | `set` — strong refs to detached compaction tasks | `main.py:130` |
| `_spawn_background_compaction` | `(conv_key: str, history: list, *, base_url: str, model: str) -> None` | `main.py:133-145` |
| `_finalize_generation` | `async (conv_key: str, gen: LiveGeneration) -> None` | `main.py:148-168` |
| `ChatMessage` | `BaseModel{role: str, content: str = ""}` | `main.py:171-173` |
| `ChatRequest` | see below | `main.py:176-239` |
| `ChatRequest.pdf_data` / `.text` / `.image_data` / `.history_messages` | properties | `main.py:201-231` |
| `ChatRequest._require_input` | `@model_validator(mode="after")` | `main.py:233-239` |
| `StopRequest` | `BaseModel{conversation_id: Optional[str], session_id: str="default"}` | `main.py:688-690` |
| `_viewer_id` | `(http_request: Request) -> Optional[int]` | `main.py:693-698` |
| `_owns` | `(gen: LiveGeneration, viewer: Optional[int]) -> bool` | `main.py:701-709` |
| `CompactRequest` | `BaseModel{conversation_id: str, messages: Optional[List[ChatMessage]]}` | `main.py:740-742` |

**Complete route table** (this app object; `Depends` column is the *only* auth-shaped dependency in the codebase)

| Method | Path | Request model | Response | Auth dependency | Status codes | `path:LINE` |
|---|---|---|---|---|---|---|
| GET | `/health` | — | `dict{status, service, version, checks}` | **none** | 200 | `main.py:242-254` |
| GET | `/reports` | — | `dict{reports: [{filename, size_bytes, modified}]}` | **none** | 200 | `main.py:257-259` |
| GET | `/reports/{filename}` | path param | `FileResponse` | **none** | 200 / 400 (`ReportPathError`) / 404 (not a file) | `main.py:262-271` |
| POST | `/chat` | `ChatRequest` | `StreamingResponse` `text/event-stream` | **none** (`current_user` called at `main.py:327` but never 401s) | 200 / 404 (`conversation not found`, `main.py:344`) / 422 (`_require_input`) | `main.py:274-685` |
| POST | `/chat/stop` | `StopRequest` | `dict{stopped: bool}` | **none** (`_viewer_id`) | 200 always | `main.py:712-722` |
| GET | `/chat/active` | — | `dict{active: [str]}` | **none** (`_viewer_id`) | 200 | `main.py:725-737` |
| POST | `/chat/compact` | `CompactRequest` | `dict{compacted, folded_turns?, covers_through?, reason?}` | **none** (`current_user`; 401 at `main.py:757` is unreachable) | 200 / 401 (dead) / 404 | `main.py:745-779` |
| GET | `/chat/attach/{conversation_id}` | path param | `StreamingResponse` SSE | **none** (`_viewer_id`) | 200 / 404 | `main.py:782-796` |
| GET | `/auth/me` | — | `dict{username, local: true}` | none | 200 | `auth.py:100-103` |
| GET | `/history/conversations?archived=<bool>` | query | `list` | `Depends(require_user)` (never 401s) | 200 | `history.py:80-85` |
| POST | `/history/conversations` | `ConversationIn{id?, title}` | `dict` | `Depends(require_user)` | 200 / 400 (id regex `history.py:93-97`, empty title `history.py:72`) / 409 (`history.py:102`) | `history.py:88-102` |
| GET | `/history/conversations/{id}` | path | `dict{…, messages}` | `Depends(require_user)` | 200 / 404 | `history.py:105-113` |
| PUT | `/history/conversations/{id}` | `ConversationUpdate` (`extra="forbid"`) | `dict` | `Depends(require_user)` | 200 / 400 / 404 / 422 | `history.py:116-131` |
| POST | `/history/conversations/{id}/messages` | `MessageIn{role, content, meta?}` | `dict` | `Depends(require_user)` | 200 / 400 (role len `history.py:142`) / 404 | `history.py:134-148` |
| PUT | `/history/conversations/{id}/messages` | `MessagesReplaceIn{messages}` | `dict{id, count}` | `Depends(require_user)` | 200 / 400 / 404 / 409 (`MessageCountWouldShrink`) | `history.py:151-190` |
| POST | `/history/conversations/{id}/truncate` | `TruncateIn{keep, expected_total}` (`extra="forbid"`) | `dict{id, count}` | `Depends(require_user)` | 200 / 400 / 404 / 409 (`ConversationChanged`) | `history.py:200-236` |
| GET | `/history/conversations/{id}/summary` | path | `dict{summary, covers_through, updated_at?}` | `Depends(require_user)` | 200 / 404 | `history.py:239-254` |
| DELETE | `/history/conversations/{id}` | path | `dict{ok: true}` | `Depends(require_user)` | 200 / 404 | `history.py:257-263` |
| GET | `/history/search?q=&limit=` | query | `dict{results: [...]}` | `Depends(require_user)` | 200 / 400 (`q` > 100 chars, `history.py:283-287`) | `history.py:266-288` |
| POST | `/uploads` | multipart `file` + `conversation_id` Form | `dict{upload_id, filename, bytes, files, notes, profile}` | `Depends(require_user)` | 200 / 400 / 404 (disabled or not owner) / 413 (`uploads.py:58-61`) | `uploads.py:66-157` |
| GET | `/uploads/{conversation_id}` | path | `dict{uploads: [...]}` | `Depends(require_user)` | 200 / 404 | `uploads.py:160-172` |

`ChatRequest` fields (`main.py:184-199`): `messages: Optional[List[ChatMessage]]`, `message: Optional[str]`,
`session_id: str="default"`, `image: Optional[str]`, `image_base64: Optional[str]`,
`conversation_id: Optional[str]`, `mode: Literal["salesforce","assistant"]="salesforce"`,
`model: Literal["smart","fast"]="smart"`, `effort: Literal["fast","low","medium","high"]="medium"`,
`agent: bool=False`, `pdf: Optional[str]`, `pdf_filename: Optional[str]`,
`web_search: Literal["off","auto","on"]="off"`.

**Control flow — `POST /chat` (`main.py:274-685`)**

1. `text = request.text or "Analyze the attached image."` — `main.py:292`.
2. `meta_extras(route)` closure built (`main.py:295-318`): sets `mode`; `route=="vision"` → `settings.vision_model`
   with no `effort`; `"agent"` → smart id + request effort; `"sql"/"rag"/"report"` → smart id + hard-coded
   `effort="medium"` (`main.py:314`); everything else → `llm.served_model_id(request.model)` + request effort.
3. `signed_in = current_user(http_request)` — `main.py:327` (always the local row).
4. `conv_key_outer = request.conversation_id or request.session_id` — `main.py:329`.
5. Ownership gate: `db.conversation_owner(conv_key_outer)` inside `try/except Exception: conv_owner = None`
   (`main.py:338-341`), then `if conv_owner is not None and conv_owner != viewer: 404` (`main.py:343-344`).
6. Pre-empt an in-flight generation on the same key: `previous.task.cancel()` (`main.py:348-350`).
7. `gen = LiveGeneration(...)`; `_live_generations[conv_key_outer] = gen` — `main.py:352-356`.
8. `context_state` / `orchestration_state` dicts declared (`main.py:360,362`); `emit()` closure defined
   (`main.py:364-381`) — on `meta` it merges `meta_extras`, `generation_id`, `context.get_trim_notice()` →
   `input_trimmed`, `context_state` → `context`, `orchestration_state` → `auto`, stores `gen.final_meta`.
9. `worker()` defined (`main.py:383-677`):
   1. `context.reset_trim_notice()` — `main.py:385`.
   2. `history = request.history_messages or memory.history(request.session_id)` — `main.py:387`.
   3. `auto_web_search_allowed = request.mode == "assistant"` — `main.py:401`.
   4. Auto-orchestration: if text and no pdf/image and not `request.agent` → `orchestrate.decide(text, history, effort)`
      and `emit("status", …)` — `main.py:403-415`.
   5. `want_agent = request.agent or auto_plan.agent`; `orchestration_state` filled — `main.py:417-421`.
   6. Web-search gate — `main.py:423-449`: requires `settings.search_enabled`, `web_search != "off"`, no
      attachment, text present, and (assistant mode or explicit `"on"`). Rate-limit via
      `search.rate_ok(user_key)` where `user_key` is the user id or `"anon"` (`main.py:437`).
   7. Cross-chat recall: `recall_block(user_id, text, conversation_id)` prepended as a `system` message —
      `main.py:450-455`.
   8. `conv_key = request.conversation_id or request.session_id` — `main.py:457`.
   9. Phase 3 GitHub: `detect_github(text)`; else `db.get_repo_keys(conv_key)` in `try/except → False` —
      `main.py:463-473`.
   10. Phase 2 URLs: `extract_urls(...)`; if none, `db.get_url_documents(conv_key)` in `try/except → []`, then
       `select_relevant(d["text"], text, 6000)` blocks prepended as a `system` message — `main.py:479-510`.
   11. Phase 4 datasets: `db.get_uploads(conv_key)` in `try/except → False` — `main.py:518-529`.
   12. `full_history = list(history)` — `main.py:534`.
   13. **Context assembly**: if `signed_in and request.conversation_id` →
       `llm.resolve_model_choice(request.model)`, `recall.retrieve_block(conv_key, text)`, then
       `compaction.prepare(conv_key, full_history, text, base_url=…, model=…, emit=emit, retrieved=…)`;
       `context_state.update(info)` — `main.py:535-549`. Note `requested_max_tokens` is **not** passed.
   14. Engine dispatch, first match wins — `main.py:551-644`: pdf → `run_pdf_engine`; image → `run_vision_engine`;
       repo → `run_repo_engine`; urls → `run_url_engine`; `want_agent` → `run_agent_engine`; `want_search` →
       `run_search_engine`; `dataset_ready` → `run_dataset_engine`; `mode=="assistant"` → `run_chat_engine`;
       else `get_graph().ainvoke({...})` (`main.py:633-643`).
   15. `gen.answer = answer`; `memory.add_exchange(request.session_id, text, answer)`;
       `gen.publish("done", {"session_id": …})` — `main.py:645-647`.
   16. Background compaction spawned with `[*full_history, user, assistant]` — `main.py:654-667`.
   17. `except asyncio.CancelledError: gen.cancelled = True` — `main.py:668-669`; `except Exception:`
       `gen.failed = True` + `publish("error", {"message": str(exc)})` — `main.py:670-672`.
   18. `finally:` `contextlib.suppress(Exception)` around `await asyncio.shield(_finalize_generation(...))` —
       `main.py:673-677`.
10. `gen.task = asyncio.create_task(worker())` — `main.py:679`.
11. `return StreamingResponse(gen.follow(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})` — `main.py:681-685`.

**SSE event catalogue** (names validated in `sse.py:34-44`; payloads from every `emit(...)` call site found by
`rg -n 'await emit\("(token|meta|done|error|reasoning|step|status|research)"'`)

| Event | Payload | Emitters |
|---|---|---|
| `token` | `{"text": str}` | `report.py:280`, `rag.py:133,143`, `document.py:36`, `sql.py:287,313,329,358,436,449`, `dataset.py:100`, `repo.py:166`, `url.py:96`, `chat.py:101`, `agent.py:602`, `search.py:413` |
| `reasoning` | `{"text": str}` | `chat.py:98`, `agent.py:599`, `search.py:413` (kind-dispatched) |
| `status` | `{"text": str}` | `main.py:415`, `compaction.py:274`, `search.py:403,464,477`, `repo.py:31,37,40`, `url.py:32,41,44,49`, `sql.py:317` |
| `step` | `{"id": int, "title": str, "status": "running"\|"done"\|"failed", "detail"?: str}` | `agent.py:382`; helper `sse.step_event` `sse.py:78-85` |
| `research` | `{"phase":"query","query":str,"results":[{title,url,domain}]}` (`search.py:226-238`) / `{"phase":"reading"\|"read","count":int}` (`search.py:446,454,478,485`) | search engine |
| `meta` | engine keys **plus** central merge (`main.py:365-380`): `mode`, `model`, `effort`, `generation_id`, optional `input_trimmed{dropped_turns,clipped_messages}`, optional `context{tokens_used,usable_budget,window,reserved_output,fraction,summarized_turns,compacted?}`, optional `auto{agent,search}` | per-route: `{"route":"report","report_files":[…]}` `report.py:282`; `{"route":"vision"}` `vision.py:95`, `document.py:37,75`; `{"route":"rag","citations":[…]}` `rag.py:150`; `{"route":"sql","sql":str,"data":[…],"truncated":bool,"report_files"?:[…],"chart"?}` `sql.py:394,452`; `{"route":"chat"}` `chat.py:104`; `{"route":"agent","steps":[…],+sql/citations/report_files/sources}` `agent.py:605` / `agent.py:467-503`; `{"route":"search","sources":[{n,title,url,domain}]}` `search.py:494-503` or `{"route":"search","search_unavailable":true}` `search.py:416`; `{"route":"repo"}` `repo.py:167`; `{"route":"url"}` `url.py:97`; `{"route":"dataset"}` `dataset.py:101` |
| `done` | `{"session_id": str}` | `main.py:647` (only emitter) |
| `error` | `{"message": str(exc)}` | `main.py:672` (only emitter) |

**Context lifecycle**

- **Token budgeting**: `compaction.measure()` (`compaction.py:100-118`) → `context.model_window` +
  `context.count_tokens`; `usable = window − output_reservation − CONTEXT_SAFETY_MARGIN`
  (`compaction.py:71-78`); `fraction = used/usable` (`compaction.py:66-68`).
- **Warn threshold**: `settings.context_warn_threshold` is defined at `config.py:146` and set in
  `.env.example:96` — **`rg` finds no other reference in the repository**. It is dead configuration; nothing
  server-side emits a warn state.
- **Background compaction**: threshold `settings.context_bg_compact_threshold` (0.70, `config.py:148-150`),
  entered from `maybe_background_compact` (`compaction.py:330-360`), spawned detached at `main.py:654-667`
  via `_spawn_background_compaction` (`main.py:133-145`). Its notice is parked in
  `compaction._pending_notice` (`compaction.py:354-357`) and attached to the *next* reply
  (`compaction.py:322-326`).
- **Hard/synchronous compaction**: threshold `settings.context_compact_threshold` (0.80, `config.py:152-154`),
  in `compaction.prepare` (`compaction.py:272-307`), adaptive with `_MAX_ADAPTIVE_ROUNDS = 4`
  (`compaction.py:132`) halving `keep` down to `MIN_KEEP_RECENT = 2` (`compaction.py:130`).
- **Summary persistence**: `db.save_summary(conversation_id, summary, new_boundary, estimate)` at
  `compaction.py:186-188` → `db.py:411-423` (`INSERT … ON CONFLICT(conversation_id) DO UPDATE`).
  Cleared on truncate at `history.py:235` → `db.clear_summary` (`db.py:426-441`, also deletes
  `conversation_chunks`).
- **Race window between compaction and an in-flight stream**: `prepare()` reads `db.get_summary` **outside**
  the per-conversation lock (`compaction.py:258-260`) while a detached background compaction for the previous
  turn may be inside `_lock_for()` (`compaction.py:217`) about to `save_summary`. The module docstring
  (`compaction.py:19-20`) claims "they cannot double-fold or race each other" — that is true only of the
  *fold* itself (`compact()` re-reads under the lock, `compaction.py:220-222`); the *measurement* in
  `prepare()` can be stale, which the code partially compensates for at `compaction.py:288-294`.
  A second, unguarded window: `main.py:348-350` cancels a previous generation without awaiting it, so the old
  worker's `finally` (`main.py:673-677`) — including `db.add_message` — can run *after* the new generation has
  already replaced the registry entry.

**State & side effects**

- Global mutation: `_live_generations` (`main.py:125`, written `main.py:356`, popped `main.py:151-152`),
  `_background_tasks` (`main.py:130`, `main.py:144-145`), `memory._sessions` via `memory.add_exchange`
  (`main.py:646`), `context._trim_notice` ContextVar (`main.py:385`), `compaction._locks` /
  `_pending_notice` (indirect).
- DB writes: `db.add_message` (`main.py:162-168`) — the only write from this module; reads
  `db.conversation_owner` (339), `db.get_repo_keys` (471), `db.get_url_documents` (493), `db.get_uploads`
  (527), `db.list_messages` (770). Every one is a blocking `sqlite3` call executed directly on the event loop.
- Filesystem: `lifespan` creates `/data/app.sqlite3` + parent dirs via `db.connect()` (`main.py:36-37` →
  `db.py:197-198`); `FileResponse` reads from `settings.reports_dir` (`main.py:271`).
- Network egress: none directly; all egress is via `llm.*`, `context.count_tokens` (`/tokenize`),
  `health.check_dependencies`, and the search/url/repo engines.
- GPU/model calls: `orchestrate.decide` (412), `search.should_search` (449), every engine at 551-643,
  `compaction.prepare` → `summarize.summarize` (LLM), `recall.retrieve_block` → `llm.embed_texts`.
- Env reads: only indirectly through `settings` (`config.py`) and `auth._local_username` (`auth.py:43`,
  `auth.py:61` read `LOCAL_USERNAME`).

**Dependencies**

- Inbound (`rg`): tests only — `tests/test_history.py:7`, `test_chat_modes.py:22`, `test_endpoints.py:14`,
  `test_live_generation.py:14,192`, `test_conversation_integrity.py:18`, `test_history_search.py:12`,
  `test_salesforce_toggle.py:18,66`, `test_history_v3.py:14`, `test_auth.py:19`, `test_context_budget.py:329`.
- Outbound: `context`, `db`, `llm` (16); `auth.router` (17); `config.settings` (18);
  `core.report_paths` (19); `graph.get_graph` (20); `health.check_dependencies` (21); `history.router` (22);
  `uploads.router` (23); `memory.memory` (24); `sse.sse_event` (25); lazily `compaction` (137, 536, 655, 752),
  `auth.current_user` (324, 695, 754), `memory_recall.recall_block` (325),
  `engines.orchestrate` (410), `engines.search` (435, 604), `core.repo` (464), `core.urls` (488),
  `recall` (536), `engines.document` (553), `engines.vision` (562), `engines.repo` (570), `engines.url` (577),
  `engines.agent` (592), `engines.dataset` (610), `engines.chat` (622).

**Config** — `settings.cors_allow_origins` (49), `settings.reports_dir` (259, 265),
`settings.vision_model` (308), `settings.search_enabled` (425), `settings.repo_analysis_enabled` (463),
`settings.url_analysis_enabled` (481), `settings.url_max_pages` (490), `settings.dataset_uploads_enabled` (519).

**Failure modes**

- `try/except Exception: conv_owner = None` (`main.py:338-341`) **fails open** — the comment at
  `main.py:336-337` asserts the opposite ("If the DB is unreachable this raises").
- `contextlib.suppress(Exception)` around `db.add_message` (`main.py:161`) and around
  `asyncio.shield(_finalize_generation(...))` (`main.py:676`) — a failed persist is silent, no logging.
- `except Exception: repo_followup = False` (`main.py:472-473`), `stored = []` (`main.py:494-495`),
  `dataset_ready = False` (`main.py:528-529`) — silent.
- Catch-all `except Exception as exc` (`main.py:670`) sends `str(exc)` verbatim to the client
  (`main.py:672`) — raw exception text, no sanitisation, no logging.
- No timeout bounds the whole `worker()` task. Only per-LLM-call `settings.llm_request_timeout` (300 s,
  `config.py:264`, applied in `llm._client`, `llm.py:78`) applies.
- No retry anywhere in this module.
- No bound on `LiveGeneration.events` (`main.py:85,97`) — every token frame is retained for the generation's
  whole life.
- No request body size limit: `main.py:47-53` installs only `CORSMiddleware`; `rg` finds no other
  `add_middleware`. `ChatRequest.image`/`.pdf` are unbounded base64 strings (`main.py:187-197`), and
  `engines/document.py` / `engines/vision.py` contain no size guard (`rg 'max_mb|MAX_|b64decode'` → one hit,
  `document.py:40 shown = len(images)`).
- `lifespan` (`main.py:27-38`) has **no shutdown branch**: `_live_generations` and `_background_tasks` are
  never drained or cancelled on SIGTERM.

**Concurrency**

- Async throughout; the generation is a detached `asyncio.Task` (`main.py:679`), deliberately outliving the
  HTTP request (`main.py:62-76`).
- Blocking calls inside `async def`: `db.conversation_owner` (339), `db.get_repo_keys` (471),
  `db.get_url_documents` (493), `db.get_uploads` (527), `memory_recall.recall_block` → `db.recall_conversations`
  (451), `db.list_messages` (770), `db.add_message` (162). None use `asyncio.to_thread` (contrast
  `health.py:122-123`, which does).
- Shared mutable module state: `_live_generations`, `_background_tasks`.
- `follow()` (`main.py:105-120`) increments `self.subscribers` on first iteration and decrements in `finally`
  — an abandoned async generator whose `aclose()` is deferred keeps `subscribers > 0`, which suppresses the
  server-side persist at `main.py:157`.
- `LiveGeneration.cond` is an `asyncio.Condition` created in `__init__` (`main.py:87`) — created inside the
  running loop, so loop-binding is safe.

**Complexity hotspots**

- `main.py:274` `chat()` — **412 LOC** (lines 274-685), containing two nested closures. Cyclomatic complexity
  well above 10: the engine dispatch alone is a 9-branch chain (`main.py:551-643`) and the search gate is a
  6-term conjunction (`main.py:424-434`).
- `main.py:383` `worker()` — **295 LOC** (lines 383-677).
- `main.py:295` `meta_extras()` — 24 LOC, 4-way branch.

**Notable**

- No `TODO`/`FIXME`/`HACK` markers in this file (verified by `rg`).
- Magic numbers: `select_relevant(..., 6000)` (`main.py:499`), `"effort": "medium"` hard-coded for
  sql/rag/report (`main.py:314`), `"Analyze the attached image."` (`main.py:292`).
- Duplication: `conv_key_outer` (`main.py:329`) and `conv_key` (`main.py:457`) compute the identical
  expression; the ownership gate uses the former, every per-conversation store uses the latter.
- Dead code: every `signed_in is None` / `user is None` branch (`main.py:342, 354, 437, 450, 535, 654, 756-757`)
  is unreachable because `auth.current_user` never returns `None` (`auth.py:89-92`).
- `_owns` (`main.py:701-709`) and `_viewer_id` (`main.py:693-698`) are structurally correct but currently
  compare one identity to itself.

---

### orchestrator/app/graph.py  (117 LOC)

**Purpose** — LangGraph wiring for the salesforce-mode fallback path: one router node fanning out to five
engine nodes, all lazily imported.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `Emit` | `Callable[[str, dict], Awaitable[None]]` | `graph.py:13` |
| `ChatState` | `TypedDict(total=False){message, session_id, image_base64, history, route, answer, emit, model_choice, effort}` | `graph.py:16-26` |
| `_router_node` | `async (state) -> dict{"route": str}` | `graph.py:29-41` |
| `_sql_node` | `async (state) -> dict{"answer": str}` | `graph.py:44-48` |
| `_rag_node` | `async (state) -> dict` | `graph.py:51-55` |
| `_vision_node` | `async (state) -> dict` | `graph.py:58-64` |
| `_report_node` | `async (state) -> dict` | `graph.py:67-71` |
| `_chat_node` | `async (state) -> dict` | `graph.py:74-87` |
| `build_graph` | `() -> CompiledGraph` | `graph.py:90-107` |
| `_compiled` | module global, `None` initially | `graph.py:110` |
| `get_graph` | `() -> CompiledGraph` | `graph.py:113-117` |

**Control flow**

1. `get_graph()` lazily calls `build_graph()` and caches in the `_compiled` global (`graph.py:113-117`).
2. `build_graph()` registers 6 nodes (`graph.py:92-97`), entry point `router` (`graph.py:99`).
3. Conditional edge keyed on `state["route"]` mapping exactly `{"sql","rag","vision","report","chat"}`
   (`graph.py:100-104`). Every engine node then goes to `END` (`graph.py:105-106`).
4. `_router_node` calls `engines.router.route_request(message, bool(image_base64), history)`
   (`graph.py:36-40`). That function returns a validated route or the `"rag"` fallback
   (`engines/router.py:103-133`), so the conditional map cannot KeyError.
5. `_chat_node` passes `model_choice`/`effort` through with defaults `"smart"`/`"medium"`
   (`graph.py:84-85`).

**State & side effects** — none of its own; all writes/egress happen inside the engines. `_compiled` is the
sole module-level mutation (`graph.py:110,116`).

**Dependencies** — Inbound: `main.py:20,633`; `tests/test_imports.py:33-35`;
`tests/test_salesforce_toggle.py:66` monkeypatches `app.main.get_graph`. Outbound: `langgraph.graph`
(`graph.py:11`), lazily `engines.router` (30), `engines.sql` (45), `engines.rag` (52), `engines.vision` (59),
`engines.report` (68), `engines.chat` (77).

**Config** — none.

**Failure modes** — `state["emit"]` and `state["message"]` are accessed with `[]` (`graph.py:47,54,62,70,80-81`);
a missing key is a `KeyError` that surfaces as the terminal `error` event. `_vision_node` passes
`state.get("image_base64")` which may be `None` (`graph.py:62`). No timeouts, no retries.

**Concurrency** — async nodes; `get_graph()` has a non-atomic check-then-set on `_compiled`
(`graph.py:115-116`), harmless because there is no `await` between the check and the assignment on a
single-threaded loop.

**Complexity hotspots** — none; largest function `build_graph` is 18 LOC.

**Notable** — No TODO markers. `ChatState` declares `route`/`answer` which the graph writes but `main.py`
only reads `answer` (`main.py:644`); the `route` that reaches `meta_extras` comes from the engine's own meta
payload, not from graph state.

---

### orchestrator/app/context.py  (275 LOC)

**Purpose** — Per-request token budgeting against the *serving* model's real window, learned from vLLM's
`POST /tokenize`, plus the trim/clip machinery that makes an oversized prompt sendable.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `_trim_notice` | `ContextVar[Optional[dict]]` | `context.py:28` |
| `reset_trim_notice` | `() -> None` | `context.py:31-32` |
| `get_trim_notice` | `() -> Optional[dict]` | `context.py:35-36` |
| `_record_trim` | `(dropped_turns: int, clipped_messages: int) -> None` | `context.py:39-48` |
| `MIN_OUTPUT_TOKENS` | `= 256` | `context.py:51` |
| `_CHARS_PER_TOKEN` | `= 3.0` | `context.py:55` |
| `_MAX_FIT_ROUNDS` | `= 24` | `context.py:60` |
| `_MIN_CLIPPED_CHARS` | `= 2000` | `context.py:63` |
| `_window_cache` | `dict` base_url → max_model_len | `context.py:66` |
| `_lock` | `asyncio.Lock()` at import time | `context.py:67` |
| `service_root` | `(base_url: str) -> str` | `context.py:70-73` |
| `estimate_tokens` | `(text: str) -> int` | `context.py:76-77` |
| `estimate_messages` | `(messages: Sequence[dict]) -> int` | `context.py:80-94` |
| `count_tokens` | `async (base_url, model, messages) -> Tuple[int, Optional[int]]` | `context.py:97-123` |
| `model_window` | `async (base_url, model) -> int` | `context.py:126-138` |
| `_split_pinned` | `(messages) -> Tuple[List[dict], List[dict]]` | `context.py:141-152` |
| `trim_to_fit` | `(messages, drop: int) -> List[dict]` | `context.py:155-161` |
| `clip_middle` | `(text: str, max_chars: int) -> str` | `context.py:164-181` |
| `_longest_content_index` | `(messages) -> Optional[int]` | `context.py:184-190` |
| `clip_message_contents` | `(messages, cap: int) -> List[dict]` | `context.py:193-202` |
| `fit_request` | `async (messages, *, base_url, model, requested_max_tokens=None) -> Tuple[List[dict], int]` | `context.py:205-275` |

**Control flow — `fit_request`**

1. `window = await model_window(base_url, model)` (`context.py:218`); `margin = settings.context_safety_margin`
   (219); `ceiling = requested_max_tokens or settings.model_max_output` (220).
2. `prompt_tokens, served_window = await count_tokens(...)`; `served_window` overrides `window`
   (`context.py:223-225`).
3. Loop `for _ in range(_MAX_FIT_ROUNDS)` (`context.py:229`): compute `budget = window − prompt_tokens − margin`;
   break when `budget >= MIN_OUTPUT_TOKENS` (230-232).
4. Prefer dropping one oldest trimmable turn via `trim_to_fit(msgs, 1)` (235-238).
5. If nothing dropped, clip the longest message in place with `clip_middle` to
   `max(len − shed, _MIN_CLIPPED_CHARS)` (241-255).
6. Re-count tokens each round — **one HTTP round-trip per round** (`context.py:257`).
7. Record + log the trim (`context.py:259-270`), then `max_tokens = max(1, min(ceiling, budget))`
   (`context.py:272-274`).

**`model_window` control flow** — cache hit (128-130); otherwise under `_lock` double-check (131-133),
probe with a 1-token message (135), `resolved = window or settings.model_max_context` (136),
**cache the fallback permanently** (137).

**State & side effects**

- Network egress: `POST {service_root(base_url)}/tokenize` (`context.py:110-112`) — targets the vLLM services
  (`http://vllm:30000`, `http://vllm-router:30002`, per `config.py:46,61`). Timeout
  `settings.tokenize_timeout` = 5.0 s (`config.py:135`). A fresh `httpx.AsyncClient` is created **per call**
  (`context.py:109`) — no connection reuse.
- Global mutation: `_window_cache` (119, 137); ContextVar `_trim_notice` (32, 44).
- Logging: `logging.getLogger(__name__).warning` on trim (`context.py:261-270`).
- No DB, no filesystem, no GPU calls of its own.

**Dependencies** — Inbound (`rg`): `llm.py:24,26` (`context.fit_request` at `llm.py:101,128,229,284`;
`clip_message_contents` at `llm.py:286`); `compaction.py:28` (`context.model_window` 108, `count_tokens` 109,
`estimate_tokens` 187); `main.py:16` (`reset_trim_notice` 385, `get_trim_notice` 373);
`tests/test_context_budget.py:13`, `tests/test_compaction.py:16`. Outbound: `asyncio`, `contextvars`,
`config.settings`, lazily `httpx` (105) and `logging` (261).

**Config** — `settings.tokenize_timeout` (109), `settings.context_safety_margin` (219),
`settings.model_max_output` (220), `settings.model_max_context` (136).

**Failure modes**

- `count_tokens` catches **all** exceptions (`context.py:121-123`) and returns
  `(estimate_messages(messages), _window_cache.get(base_url))`. `estimate_messages` counts only text parts of
  multimodal content (`context.py:86-92`), so an image payload is counted as ~0 tokens.
- `model_window` caches `settings.model_max_context` (262144 by default, `config.py:127`) when the probe fails
  (`context.py:136-137`). Subsequent successful `count_tokens` calls overwrite it (`context.py:118-119`), but
  while `/tokenize` stays unreachable the wrong window is used for every budget computation.
- `fit_request` can `break` out of the loop with `budget < MIN_OUTPUT_TOKENS` at `context.py:245` (no
  clippable content) and `context.py:252` (already at `_MIN_CLIPPED_CHARS`); `max_tokens` then floors at 1
  (`context.py:274`).
- No retry on `/tokenize`.

**Concurrency** — async. `_lock` is constructed at **import time** (`context.py:67`); safe on Python ≥3.10
(the runtime image is `nvcr.io/nvidia/vllm:26.05-py3`, `orchestrator/Dockerfile:14`). `_window_cache` is
read/written without the lock in `count_tokens` (119) and in `model_window`'s fast path (128) — benign
last-writer-wins. `_trim_notice` is a ContextVar, so each `asyncio.create_task` worker gets its own copy
(`main.py:679` copies the context at task creation).

**Complexity hotspots** — `fit_request` (`context.py:205`) is 71 LOC with a nested branch inside a bounded
loop; the only function in the file over 60 LOC.

**Notable** — No TODO markers. Magic numbers: `_CHARS_PER_TOKEN = 3.0` (55), `head = int(max_chars * 0.6)`
(`context.py:176`), `+ 4` per-message overhead and `+ 3` generation primer (`context.py:93-94`),
`shed_chars … + 1024` (`context.py:247`). `service_root` (`context.py:70-73`) is **duplicated verbatim** in
`health.py:24-33`.

---

### orchestrator/app/compaction.py  (360 LOC)

**Purpose** — Budget arithmetic plus the two-path (background / synchronous) rolling-summary compaction that
keeps a conversation inside the serving model's window.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `Emit` | `Callable[[str, dict], Awaitable[None]]` | `compaction.py:31` |
| `_locks` | `Dict[str, asyncio.Lock]` | `compaction.py:35` |
| `_pending_notice` | `Dict[str, dict]` | `compaction.py:41` |
| `take_pending_notice` | `(conversation_id) -> Optional[dict]` | `compaction.py:44-45` |
| `_lock_for` | `(conversation_id) -> asyncio.Lock` | `compaction.py:48-53` |
| `Budget` | `@dataclass{window, output_reserved, usable, used, breakdown}` + `fraction` property | `compaction.py:56-68` |
| `usable_budget` | `(window: int, output_reserved: int) -> int` | `compaction.py:71-78` |
| `output_reservation` | `(requested: Optional[int], window: Optional[int]=None) -> int` | `compaction.py:81-97` |
| `measure` | `async (messages, *, base_url, model, requested_max_tokens=None) -> Budget` | `compaction.py:100-118` |
| `split_history` | `(history) -> Tuple[List[dict], List[dict]]` | `compaction.py:121-125` |
| `MIN_KEEP_RECENT` | `= 2` | `compaction.py:130` |
| `_MAX_ADAPTIVE_ROUNDS` | `= 4` | `compaction.py:132` |
| `fold_boundary` | `(turn_count, covers_through, keep=None) -> int` | `compaction.py:135-146` |
| `assemble` | `(history, summary, covers_through, retrieved=None) -> List[dict]` | `compaction.py:149-167` |
| `_fold` | `async (conversation_id, turns, covers_through, new_boundary, existing) -> Optional[dict]` | `compaction.py:170-201` |
| `compact` | `async (conversation_id, history, *, force=False, keep=None) -> Optional[dict]` | `compaction.py:204-239` |
| `prepare` | `async (conversation_id, history, current_text, *, base_url, model, requested_max_tokens=None, emit=None, retrieved=None) -> Tuple[List[dict], dict]` | `compaction.py:242-327` |
| `maybe_background_compact` | `async (conversation_id, history, *, base_url, model, requested_max_tokens=None) -> Optional[dict]` | `compaction.py:330-360` |

**Control flow — `prepare` (the synchronous path)**

1. `row = db.get_summary(conversation_id)` (`compaction.py:258`) — **blocking sqlite, outside the lock**.
2. `candidate = assemble(history, summary, covers, retrieved)` (262); `probe = candidate + [{"role":"user",…}]`
   (263).
3. `budget = await measure(probe, …)` (264-269) → `context.model_window` + `context.count_tokens`
   (`compaction.py:108-109`): up to 2 HTTP calls.
4. If `budget.fraction > settings.context_compact_threshold` (272): `emit("status", {"text": "Compacting conversation…"})`
   (274) and enter the adaptive loop (280-307), up to 4 iterations of
   `compact()` → `assemble` → `measure`, halving `keep` (307) until `keep <= MIN_KEEP_RECENT` (305).
5. When `compact()` returns `None`, re-read `db.get_summary` to detect a concurrent background fold
   (`compaction.py:288-294`).
6. Build `info` (309-318): `tokens_used`, `usable_budget`, `window`, `reserved_output`, `fraction`,
   `summarized_turns`; add `compacted{folded_turns, background}` from this run (320) or from
   `take_pending_notice` (322-326).

**Control flow — `compact`**

1. Acquire `_lock_for(conversation_id)` (`compaction.py:217`).
2. `split_history` (219); `db.get_summary` (220); `covers = min(stored, len(turns))` (222).
3. `boundary = len(turns) if force else fold_boundary(len(turns), covers, keep)` (223-225), then clamped so the
   in-flight turn is never folded: `min(boundary, len(turns) − 1)` (227).
4. `if boundary <= covers: return None` (228).
5. `_fold(...)` (230): `summarize.summarize(existing, folded)` (`compaction.py:181`), optional
   `summarize.condense` when `len(summary)/3 > summary_max_tokens*0.9` (182-185), `db.save_summary` (186-188),
   then `recall.index_folded` inside `try/except: pass` (190-196).

**Control flow — `maybe_background_compact`** — `db.get_summary` (341) → `assemble` (343) → `measure` (344-349)
→ return `None` if `fraction <= settings.context_bg_compact_threshold` (350) → `compact()` (352) → park
`_pending_notice` (353-357). The whole body is inside `try/except Exception: return None` (339, 359-360)
with **no logging**.

**State & side effects**

- DB writes: `db.save_summary` (`compaction.py:186`); indirectly `db.add_conversation_chunks` via
  `recall.index_folded` (`compaction.py:194`).
- DB reads: `db.get_summary` at 220, 258, 291, 341 — all blocking sqlite on the event loop.
- Network egress: `context.count_tokens` `/tokenize`, and `summarize.summarize`/`condense` →
  `llm.chat_completion` → `settings.openai_base_url` (`llm.py:84,99`).
- GPU/model calls: one summarization completion per fold (`summarize.py:74-78`), plus optionally one condense
  (`summarize.py:88-104`), plus embeddings via `recall.index_folded` → `llm.embed_texts`
  (`recall.py:103`).
- Global mutation: `_locks` (52), `_pending_notice` (45, 354).
- Env reads: via `settings` only.

**Dependencies** — Inbound (`rg`): `main.py:137` (`maybe_background_compact`), `main.py:536,540`
(`prepare`), `main.py:655` (background), `main.py:752,772` (`compact(force=True)`);
`tests/test_compaction.py:16`. Outbound: `context`, `db`, `summarize` (`compaction.py:28`),
`config.settings` (29), lazily `recall` (192) and `logging` (232).

**Config** — `settings.context_safety_margin` (78), `settings.model_max_output` (93),
`settings.min_output_floor` (94, 96), `settings.keep_recent_turns` (144, 279),
`settings.summary_max_tokens` (182), `settings.context_compact_threshold` (272, 303),
`settings.context_bg_compact_threshold` (350).

**Failure modes**

- `compact()` catches all exceptions and logs a warning (`compaction.py:231-239`) → returns `None`; the chat
  continues on an uncompacted prompt.
- `_fold`'s `recall.index_folded` failure is swallowed by a bare `except Exception: pass`
  (`compaction.py:195-196`) — semantic recall can be permanently broken with **zero** signal.
- `maybe_background_compact` catches everything and returns `None` with no log (`compaction.py:359-360`).
- No timeout on the summarization LLM call beyond `settings.llm_request_timeout` (300 s). Worst case
  `prepare` performs 4 sequential summarizations plus up to 5 `measure` calls before the user sees a token.
- `_locks` and `_pending_notice` are never evicted (`rg` finds no `pop` except `take_pending_notice`,
  `compaction.py:45`).

**Concurrency**

- `_lock_for` (`compaction.py:48-53`) is a check-then-create with no `await` between the two, so it cannot
  double-create on a single-threaded loop.
- `prepare`'s summary read at `compaction.py:258` is **outside** the lock — the stale-read window described in
  the main.py section above.
- Blocking calls inside async defs: `db.get_summary` (220, 258, 291, 341), `db.save_summary` (186).
- `Budget` is a plain dataclass, no shared mutation.

**Complexity hotspots** — `prepare` (`compaction.py:242`) is **86 LOC** (242-327) with a nested adaptive loop
containing four `await` points and two early breaks; cyclomatic complexity > 10.

**Notable** — No TODO markers. Magic numbers: `len(summary) / 3` as a token estimate (`compaction.py:182`,
duplicating `context._CHARS_PER_TOKEN = 3.0` rather than calling `context.estimate_tokens`),
`* 0.9` (182), `window // 2` (96), `round(..., 4)` (314).
`split_history` (`compaction.py:121-125`) keeps system messages in original order but `context._split_pinned`
(`context.py:141-152`) treats only the **leading** run of system messages as pinned — two different
definitions of "pinned" in the same request path.

---

### orchestrator/app/summarize.py  (116 LOC)

**Purpose** — Incremental rolling-summary prompts: previous summary + newly folded turns → new summary, and a
condense pass when the summary approaches its own cap.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `_SYSTEM` | prompt constant | `summarize.py:17-20` |
| `_INSTRUCTIONS` | prompt constant ("Max ~1500 words") | `summarize.py:22-31` |
| `_MAX_TURN_CHARS` | `= 4000` | `summarize.py:35` |
| `format_turns` | `(turns: Sequence[dict]) -> str` | `summarize.py:38-50` |
| `build_messages` | `(existing: str, turns) -> List[dict]` | `summarize.py:53-63` |
| `summarize` | `async (existing: str, turns) -> str` | `summarize.py:66-79` |
| `condense` | `async (summary: str) -> str` | `summarize.py:82-105` |
| `SUMMARY_HEADER` | constant | `summarize.py:108-111` |
| `summary_block` | `(summary: str) -> dict` | `summarize.py:114-116` |

**Control flow**

1. `format_turns` skips non-string / blank contents and truncates each turn to `_MAX_TURN_CHARS`
   (`summarize.py:44-49`).
2. `summarize` short-circuits and returns `existing` when the formatted transcript is blank
   (`summarize.py:72-73`).
3. `llm.chat_completion(build_messages(...), temperature=0.0, max_tokens=settings.summary_max_tokens)`
   (`summarize.py:74-78`) — the main model on `settings.openai_base_url`.
4. Falls back to `existing` if the model returns empty (`summarize.py:79`).
5. `condense` runs a second completion at the same cap (`summarize.py:88-104`) and falls back to `summary`
   (105).

**State & side effects** — Network/GPU: two possible `llm.chat_completion` calls per fold, both to
`settings.openai_base_url`. No DB, no filesystem, no globals, no direct env reads.

**Dependencies** — Inbound (`rg`): `compaction.py:28` (`summarize.summarize` 181, `summarize.condense` 184,
`summarize.summary_block` 163); `tests/test_compaction.py:16`. Outbound: `llm` (`summarize.py:14`),
`config.settings` (15).

**Config** — `settings.summary_max_tokens` (`summarize.py:77`, `summarize.py:103`).

**Failure modes** — the docstring at `summarize.py:69-71` states it raises on model failure; the caller
(`compaction._fold` → `compact`) catches it (`compaction.py:231`). No timeout beyond
`settings.llm_request_timeout`; no retry. The "Max ~1500 words" instruction (`summarize.py:30`) is
unenforced — only `max_tokens` bounds the output.

**Concurrency** — async, stateless, no shared mutable state.

**Complexity hotspots** — none; largest function `condense` is 24 LOC.

**Notable** — No TODO markers. `_MAX_TURN_CHARS = 4000` (35) is a magic number and is a *third* clipping
policy alongside `context.clip_middle` (`context.py:164`) and `context.clip_message_contents`
(`context.py:193`). Truncation here is head-only (`summarize.py:48`), unlike `clip_middle`'s head+tail policy.

---

### orchestrator/app/memory.py  (35 LOC)

**Purpose** — In-process per-`session_id` transcript, trimmed to `SESSION_MAX_TURNS` exchanges. Explicitly a
fallback: `main.py:387` prefers the client-supplied `history_messages`.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `SessionMemory` | `class` | `memory.py:13` |
| `SessionMemory.__init__` | `(max_turns: int \| None = None)` | `memory.py:14-16` |
| `SessionMemory.history` | `(session_id: str) -> List[dict]` (copy) | `memory.py:18-20` |
| `SessionMemory.add_exchange` | `(session_id, user_text, assistant_text) -> None` | `memory.py:22-29` |
| `SessionMemory.clear` | `(session_id: str) -> None` | `memory.py:31-32` |
| `memory` | module-level singleton `SessionMemory()` | `memory.py:35` |

**Control flow** — `add_exchange` appends two dicts (`memory.py:24-25`) and deletes the overflow prefix
`len(msgs) − max_turns*2` (`memory.py:27-29`).

**State & side effects** — `self._sessions: Dict[str, List[dict]]` (`memory.py:15`) — a process-global dict
via the `memory` singleton (`memory.py:35`). No DB, no filesystem, no network. Env read is indirect
(`settings.session_max_turns`, `config.py:263`).

**Dependencies** — Inbound (`rg`): `main.py:24` only (`memory.history` at `main.py:387`,
`memory.add_exchange` at `main.py:646`). `SessionMemory.clear` has **no caller anywhere** — dead code.
Outbound: `config.settings` (`memory.py:10`).

**Config** — `settings.session_max_turns` (`memory.py:16`; default 20 → 40 messages).

**Failure modes** — none raise. There is no eviction of *sessions*, only of messages within a session
(`memory.py:27-29`).

**Concurrency** — fully synchronous, called from the async worker (`main.py:646`). Cheap, so the blocking is
irrelevant; the unbounded `_sessions` growth is not.

**Complexity hotspots** — none; largest function 8 LOC.

**Notable** — No TODO markers. The frontend sends `session_id: conversationId` (`frontend/lib/streams.ts:321`),
so `_sessions` accrues one entry per conversation and is never freed for the process lifetime. Because
`main.py:387` prefers `request.history_messages` (always non-empty for the real UI), the stored data is
almost never read — it is a write-only cache.

---

### orchestrator/app/memory_recall.py  (75 LOC)

**Purpose** — Cross-chat recall: extract content keywords from the question, keyword-search the user's *other*
conversations, and render a system-context block.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `_STOPWORDS` | `set[str]` (~60 entries) | `memory_recall.py:17-25` |
| `_WORD_RE` | `re.compile(r"[A-Za-z0-9_][A-Za-z0-9_'-]{2,}")` | `memory_recall.py:26` |
| `keywords` | `(text: str, max_keywords: int = 8) -> List[str]` | `memory_recall.py:29-39` |
| `format_recall_block` | `(hits: List[dict]) -> Optional[str]` | `memory_recall.py:42-54` |
| `recall_block` | `(user_id: int, query: str, exclude_conversation_id: Optional[str], *, search=None, limit: int = 3) -> Optional[str]` | `memory_recall.py:57-75` |

**Control flow**

1. `keywords(query)` — regex findall, lowercase, drop stopwords and duplicates, cap at 8
   (`memory_recall.py:32-38`).
2. `recall_block` returns `None` on empty keywords (`memory_recall.py:69-70`).
3. `search` defaults to `db.recall_conversations` via a lazy import (`memory_recall.py:71-73`).
4. `hits = search(user_id, kws, exclude_conversation_id, limit)` (74) → `format_recall_block` (75), producing
   `'- From "<title>": <snippet>'` lines under an instruction header (`memory_recall.py:46-53`).

**State & side effects** — DB read only, through the injected `search` callable → `db.recall_conversations`
(`db.py:981-1019`), which opens a connection (`db.py:1007`) and issues a `GROUP BY` ranking query with N
`content LIKE ? ESCAPE '\'` terms plus one snippet query per hit (`db.py:1008-1013`). No filesystem, no
network, no globals, no env reads.

**Dependencies** — Inbound (`rg`): `main.py:325,451` (`recall_block`); `core/urls.py:13` and
`engines/repo.py:18` import `keywords`; `tests/test_memory_recall.py:2`. Outbound: `re`, lazily `db` (71).

**Config** — none.

**Failure modes** — no try/except: a `sqlite3` error from `recall_conversations` propagates to
`main.py:451`, is caught by the worker's catch-all (`main.py:670`) and becomes a terminal `error` event —
i.e. a recall failure kills the whole answer. No timeout, no bound on the number of messages scanned.

**Concurrency** — **synchronous function called from `async def worker()`** (`main.py:451`). The LIKE scan is
unindexed (`db.py:995` builds `m.content LIKE ? ESCAPE '\'` per keyword) and runs over every message row the
user owns, blocking the single event loop for its duration.

**Complexity hotspots** — none; largest function `recall_block` is 19 LOC.

**Notable** — No TODO markers. `limit: int = 3` (`memory_recall.py:63`) and `max_keywords: int = 8`
(`memory_recall.py:29`) are hard-coded, not configurable. The stopword list is English-only
(`memory_recall.py:17-25`). Duplicated concern with `recall.py`: two independent "recall" mechanisms
(keyword cross-chat vs embedding within-chat) with unrelated code paths and headers
(`memory_recall.py:46-48` vs `recall.py:111-114`).

---

### orchestrator/app/recall.py  (144 LOC)

**Purpose** — Within-conversation semantic recall: embed every folded turn into SQLite and retrieve the
top-k most similar chunks for each new question.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `_CHUNK_CHARS` | `= 1200` | `recall.py:31` |
| `_CHUNK_OVERLAP` | `= 150` | `recall.py:32` |
| `_MIN_CHUNK_CHARS` | `= 15` | `recall.py:36` |
| `pack_vector` | `(vector: Sequence[float]) -> bytes` | `recall.py:39-40` |
| `unpack_vector` | `(blob: bytes) -> List[float]` | `recall.py:43-46` |
| `cosine` | `(a, b) -> float` | `recall.py:49-57` |
| `chunk_text` | `(text: str) -> List[str]` | `recall.py:60-75` |
| `index_folded` | `async (conversation_id, folded, first_ordinal) -> int` | `recall.py:78-108` |
| `RECALL_HEADER` | constant | `recall.py:111-114` |
| `retrieve_block` | `async (conversation_id, question, top_k=None) -> Optional[str]` | `recall.py:117-144` |

**Control flow — `index_folded`**

1. Return 0 immediately if `settings.semantic_recall_enabled` is false (`recall.py:82-83`).
2. `ordinal = first_ordinal * 1000` (`recall.py:86`) — 1000 chunk slots per turn.
3. For each folded turn, `chunk_text(content)` and append `{ordinal, role, text}` (`recall.py:87-100`).
4. `vectors = await llm.embed_texts(texts)` (`recall.py:103`) — one call, all chunks.
5. `db.add_conversation_chunks(conversation_id, rows)` (`recall.py:107`) → per-row
   `INSERT OR REPLACE` in one transaction (`db.py:449-463`).

**Control flow — `retrieve_block`**

1. Bail if disabled or blank question (`recall.py:126-127`).
2. `db.get_conversation_chunks(conversation_id)` (`recall.py:129`) — loads **every** chunk for the
   conversation (`db.py:466-482`).
3. `query = (await llm.embed_texts([question]))[0]` (`recall.py:132`).
4. Brute-force `cosine` over all chunks in pure Python (`recall.py:133-135`), sort, take
   `settings.retrieve_top_k` with score > 0 (`recall.py:136-138`).
5. Render `[role] text` lines under `RECALL_HEADER` (`recall.py:141-142`).

**State & side effects**

- DB writes: `db.add_conversation_chunks` (`recall.py:107`).
- DB reads: `db.get_conversation_chunks` (`recall.py:129`).
- Network/GPU: `llm.embed_texts` → `settings.embed_base_url` (`http://vllm-embed:30003/v1`,
  `config.py:80-82`), clipped to `settings.embed_input_char_cap` inside `llm.py:343,346`.
- No filesystem, no globals, no direct env reads.

**Dependencies** — Inbound (`rg`): `compaction.py:192-194` (`index_folded`), `main.py:536,539`
(`retrieve_block`). Outbound: `array`, `math`, `db`, `llm` (`recall.py:23-27`), `config.settings` (28).

**Config** — `settings.semantic_recall_enabled` (82, 126), `settings.retrieve_top_k` (137).

**Failure modes**

- `retrieve_block` wraps everything in `try/except Exception: return None` (`recall.py:128, 143-144`) —
  silent; an embedding-service outage degrades recall invisibly.
- `index_folded` has **no** internal error handling; its caller `compaction._fold` swallows the failure with a
  bare `except Exception: pass` (`compaction.py:195-196`).
- `unpack_vector` (`recall.py:43-46`) does not validate blob length; a truncated blob yields a short vector
  which `cosine` rejects by length mismatch (`recall.py:51`) → silently scores 0.
- No timeout beyond `settings.llm_request_timeout`; no retry; no bound on the number of chunks loaded or
  scored.

**Concurrency** — async, but `db.get_conversation_chunks` (129) and `db.add_conversation_chunks` (107) are
blocking sqlite calls on the event loop, and the cosine loop (`recall.py:133-135`) is pure-Python O(chunks ×
dim) with no `await` — for a long conversation this is a measurable event-loop stall.

**Complexity hotspots** — none over 60 LOC; `index_folded` is 31 LOC, `retrieve_block` is 28 LOC.

**Notable** — No TODO markers. `ordinal = first_ordinal * 1000` (`recall.py:86,100`) is a magic packing
scheme that silently collides if a single turn produces more than 1000 chunks (a turn > 1.2 MB). The
`ordinal + len(texts)` expression (`recall.py:93`) uses the running length of the *whole* `texts` list, not
the per-turn index, so ordinals are non-contiguous across turns — harmless for the ORDER BY at `db.py:471`
but confusing. Note the deliberate storage decision documented at `recall.py:13-19`: conversation embeddings
live in SQLite, never in the LanceDB Salesforce `chunks` table.

---

### orchestrator/app/history.py  (288 LOC)

**Purpose** — Server-side conversation CRUD, thread sync/truncate, rolling-summary read, and full-text-ish
search, all under `require_user`.

**Public surface**

| Symbol | Signature | `path:LINE` |
|---|---|---|
| `router` | `APIRouter(prefix="/history", tags=["history"])` | `history.py:30` |
| `_CONVERSATION_ID_RE` | `^[A-Za-z0-9_-]{1,64}$` | `history.py:33` |
| `_MAX_TITLE_LENGTH` | `= 200` | `history.py:34` |
| `_MAX_QUERY_LENGTH` | `= 100` | `history.py:35` |
| `ConversationIn` | `{id: Optional[str], title: str}` | `history.py:38-40` |
| `ConversationUpdate` | `{title?, pinned?, archived?}`, `extra="forbid"` | `history.py:43-54` |
| `MessageIn` | `{role: str, content: str, meta: Optional[dict]}` | `history.py:57-60` |
| `MessagesReplaceIn` | `{messages: List[MessageIn]}` | `history.py:63-66` |
| `_clean_title` | `(title: str) -> str` | `history.py:69-73` |
| `_not_found` | `() -> HTTPException(404)` | `history.py:76-77` |
| `list_conversations` | `GET /history/conversations` | `history.py:80-85` |
| `create_conversation` | `POST /history/conversations` | `history.py:88-102` |
| `get_conversation` | `GET /history/conversations/{id}` | `history.py:105-113` |
| `update_conversation` | `PUT /history/conversations/{id}` | `history.py:116-131` |
| `add_message` | `POST /history/conversations/{id}/messages` | `history.py:134-148` |
| `replace_messages` | `PUT /history/conversations/{id}/messages` | `history.py:151-190` |
| `TruncateIn` | `{keep: int, expected_total: int}`, `extra="forbid"` | `history.py:193-197` |
| `truncate_messages` | `POST /history/conversations/{id}/truncate` | `history.py:200-236` |
| `get_summary` | `GET /history/conversations/{id}/summary` | `history.py:239-254` |
| `delete_conversation` | `DELETE /history/conversations/{id}` | `history.py:257-263` |
| `search_history` | `GET /history/search` | `history.py:266-288` |

**Control flow (representative paths)**

1. Every handler takes `user: sqlite3.Row = Depends(require_user)` (`history.py:82, 90, 107, 120, 138, 155,
   204, 241, 260, 270`) → `auth.require_user` (`auth.py:95-97`), which never 401s.
2. `create_conversation`: id defaulted to `uuid4().hex` (92), regex-validated (93-97), title cleaned (98),
   `db.create_conversation` with `sqlite3.IntegrityError` → 409 (99-102).
3. `replace_messages`: validate every role length (165-170), then `db.replace_messages` in one transaction
   (`db.py:627-675`) which refuses shrink via `MessageCountWouldShrink` → 409 (180-187).
4. `truncate_messages`: range checks (213-218), `db.truncate_messages` with optimistic
   `expected_total` → `ConversationChanged` → 409 (219-230), then **`db.clear_summary(conversation_id)`**
   (235) so the rolling summary cannot describe deleted turns.
5. `search_history`: trims `q`, empty → `{"results": []}` (280-282), > 100 chars → 400 (283-287),
   otherwise `db.search_conversations` which clamps `limit` to `SEARCH_LIMIT_MAX = 100` (`db.py:1036`).

**State & side effects** — DB reads/writes only, all through `db.*` helpers, each opening its own connection
(and re-running the schema+migration, `db.py:203-204`). No filesystem, no network, no globals, no env reads.
`truncate_messages` additionally deletes `conversation_chunks` through `db.clear_summary` (`db.py:438-441`).

**Dependencies** — Inbound (`rg`): `main.py:22,58`; `tests/test_conversation_integrity.py:323`
(`TruncateIn`); the history test suites mount `app.main:app`. Outbound: `re`, `sqlite3`, `uuid`, `fastapi`,
`pydantic`, `db` (`history.py:27`), `auth.require_user` (28).

**Config** — none read directly; `db.SEARCH_LIMIT_DEFAULT` (`history.py:269` → `db.py:902`).

**Failure modes** — All error paths are explicit `HTTPException`s; nothing is swallowed. `sqlite3.IntegrityError`
is caught only in `create_conversation` (`history.py:101`); any other sqlite error in any handler surfaces as
an unhandled 500. No timeouts, no retries, no pagination on `list_conversations` (`history.py:80-85` →
`db.py:250-…` returns every row) or on `get_conversation`'s `db.list_messages` (`history.py:112` →
`db.py:343-354` returns the entire thread).

**Concurrency** — **every handler is a plain `def`**, so FastAPI runs them in the threadpool — this is the one
module in the assignment that does *not* block the event loop. `db.replace_messages` and
`db.truncate_messages` are single-transaction (`db.py:627`, `db.py:586`); `db.add_message` relies on the
unique index `idx_messages_generation` (`db.py:187-191`) for cross-client idempotency.

**Complexity hotspots** — none; largest handler `truncate_messages` is 37 LOC (`history.py:200-236`).

**Notable** — No TODO markers. `_MAX_TITLE_LENGTH = 200` (34), `len(role) > 32` (141, 167) and
`_MAX_QUERY_LENGTH = 100` (35) are hard-coded. `role` validation is length-only — any string is accepted
(`history.py:140-142`), so `role: "system"` can be persisted into a thread and later replayed to the model.
`_CONVERSATION_ID_RE` (33) is enforced **only** on `POST /history/conversations`; `/chat`,
`/chat/attach/{id}`, `/chat/compact` and `/uploads` accept arbitrary conversation-id strings.

---

### orchestrator/app/sse.py  (85 LOC)

**Purpose** — The single formatter for SSE frames, with an allow-list of event names and a step-status
allow-list.

**Public surface**

| Symbol | Signature / value | `path:LINE` |
|---|---|---|
| `ALLOWED_EVENTS` | `("token","meta","done","error")` | `sse.py:34` |
| `V2_EVENTS` | `("reasoning","step")` | `sse.py:36` |
| `PROGRESS_EVENTS` | `("status",)` | `sse.py:39` |
| `RESEARCH_EVENTS` | `("research",)` | `sse.py:43` |
| `ALL_EVENTS` | concatenation of the four | `sse.py:44` |
| `STEP_STATUSES` | `("running","done","failed")` | `sse.py:46` |
| `sse_event` | `(event: str, data: Optional[Mapping]=None) -> str` | `sse.py:49-54` |
| `token_event` | `(text: str) -> str` | `sse.py:57-58` |
| `meta_event` | `(data: Mapping) -> str` | `sse.py:61-62` |
| `done_event` | `(data: Optional[Mapping]=None) -> str` | `sse.py:65-66` |
| `error_event` | `(message: str) -> str` | `sse.py:69-70` |
| `reasoning_event` | `(text: str) -> str` | `sse.py:73-75` |
| `step_event` | `(id: int, title: str, status: str, detail: Optional[str]=None) -> str` | `sse.py:78-85` |

**Control flow** — `sse_event` validates `event in ALL_EVENTS` else `ValueError` (`sse.py:51-52`), then
`json.dumps(dict(data or {}), ensure_ascii=False, default=str)` (53) and returns
`f"event: {event}\ndata: {payload}\n\n"` (54). `step_event` additionally validates `status`
(`sse.py:80-81`) and includes `detail` only when not `None` (83-84).

**State & side effects** — none. Pure formatting; no I/O, no globals, no env reads.

**Dependencies** — Inbound (`rg` over `orchestrator/app`): **`main.py:25` only** (used at `main.py:118`).
Tests: `tests/test_sse.py:6`, `tests/test_sse_v2.py:7`, `tests/test_chart_routes.py:285`,
`tests/test_system_normalization.py:146`. Outbound: `json`, `typing`.

**Config** — none.

**Failure modes** — `sse_event` raises `ValueError` for an unknown event. Because it is called from
`LiveGeneration.follow()` (`main.py:118`) rather than from `publish()` (`main.py:95-98`), an invalid event
name raises inside the *reader*, after the frame has already been buffered — the stream dies mid-flight
instead of the publisher failing fast. `json.dumps(..., default=str)` never raises on unserialisable values
but silently stringifies them. `error_event`/`done_event`/`token_event`/`meta_event`/`reasoning_event`
(`sse.py:57-75`) have **no callers in `orchestrator/app`** — only `main.py` uses `sse_event` directly.

**Concurrency** — pure sync, no shared state.

**Complexity hotspots** — none; largest function `step_event` is 8 LOC.

**Notable** — No TODO markers. There is **no keep-alive/heartbeat frame** and no `id:`/`retry:` field, so a
long silent generation relies on `X-Accel-Buffering: no` (`main.py:684`) and on no intermediate proxy having
an idle timeout. The docstring (`sse.py:20-24`) documents `done` and `error` as the only terminal states;
the cancellation path (`main.py:668-669` → `finish()` → `follow()` breaks at `main.py:115`) closes the stream
with **no terminal frame at all** — a third, undocumented terminal state that
`frontend/lib/streams.ts:261-265` silently maps to `status: 'done'`.

---

## Metrics summary

- Total LOC across the 10 assigned files: **2291**.
- Largest function: `orchestrator/app/main.py:274` `chat()` — **412 LOC** (274-685), with nested
  `worker()` at `main.py:383` — 295 LOC.
- Other functions > 60 LOC: `compaction.py:242` `prepare()` — 86 LOC; `context.py:205` `fit_request()` — 71 LOC.
- `TODO`/`FIXME`/`HACK`/`XXX` markers in the assigned files: **none** (verified by `rg` over all ten files).
- Dead configuration: `CONTEXT_WARN_THRESHOLD` / `settings.context_warn_threshold`
  (`config.py:146`, `.env.example:96`) — no reader anywhere in the repo.
- Dead code: `memory.SessionMemory.clear` (`memory.py:31-32`); `sse.token_event`/`meta_event`/`done_event`/
  `error_event`/`reasoning_event`/`step_event` (`sse.py:57-85`) — no non-test callers.
