# Evidence — `orch-platform` (orchestrator platform layer)

Scope: `orchestrator/app/__init__.py`, `llm.py`, `db.py`, `auth.py`, `config.py`, `health.py`,
`uploads.py`, `search/{__init__,base,brave,searxng,tavily}.py`.
Total assigned LOC: **2285** (`wc -l`, verified).
TODO/FIXME/HACK/XXX markers across all assigned files: **none** (`rg -n 'TODO|FIXME|HACK|XXX'` → empty).
Every line reference below was read with the Read tool or produced by `rg -n` against the file.

---

### orchestrator/app/__init__.py  (1 LOC)

**Purpose** — Package docstring only; marks `orchestrator/app` as a Python package.

**Public surface** — none. Single line: `orchestrator/app/__init__.py:1` — module docstring
`"""TechSara Local AI Analysis Platform — orchestrator service (FastAPI + LangGraph)."""`.

**Control flow** — n/a (no executable statements).

**State & side effects** — none. Importing the package executes nothing.

**Dependencies** — inbound: every `from app import …` / `from . import …` inside `orchestrator/`
(e.g. `orchestrator/app/main.py:16`). outbound: none.

**Config** — none.

**Failure modes** — none.

**Concurrency** — n/a.

**Complexity hotspots** — none.

**Notable** — Deliberately empty: no side-effectful package `__init__`, so `import app.db` does not
drag in FastAPI. This is what lets `health.py:76` do a lazy `from . import db`.

---

### orchestrator/app/llm.py  (348 LOC)

**Purpose** — The single vLLM/OpenAI-compatible client layer: chat, streaming, reasoning-stream,
router classification, vision, embeddings. All four backends are vLLM behind OpenAI endpoints.

**Public surface**
- `LOCAL_API_KEY = "local-no-key"` — `orchestrator/app/llm.py:29` (placeholder key, not a secret).
- `MODEL_CHOICES = ("smart", "fast")` — `llm.py:32`.
- `REASONING_EFFORTS = ("fast", "low", "medium", "high")` — `llm.py:36`.
- `normalize_system(messages: Sequence[dict]) -> List[dict]` — `llm.py:39`.
- `_client(base_url: str, api_key: Optional[str] = None)` — `llm.py:72` (private but the single
  client factory; monkeypatched by `orchestrator/tests/test_llm_clients.py:3`).
- `_openai_client()` — `llm.py:82`.
- `async chat_completion(messages, *, model=None, temperature=0.2, max_tokens=None) -> str` — `llm.py:91`.
- `async stream_chat_completion(messages, *, model=None, temperature=0.2, max_tokens=None, thinking=True) -> AsyncIterator[str]` — `llm.py:117`.
- `resolve_model_choice(choice: str) -> Tuple[str, str, str]` — `llm.py:158`.
- `served_model_id(choice: str) -> str` — `llm.py:169`.
- `wants_thinking(model_choice="smart", effort="medium") -> bool` — `llm.py:174`.
- `thinking_body(enabled: bool) -> dict` — `llm.py:188`.
- `apply_reasoning_effort(messages, effort, model_choice="smart") -> List[dict]` — `llm.py:198` (no-op passthrough, `llm.py:209`).
- `async stream_chat_events(messages, *, model_choice="smart", effort="medium", temperature=0.2, max_tokens=None) -> AsyncIterator[Tuple[str, str]]` — `llm.py:212`.
- `async router_chat_completion(messages, *, temperature=0.0, max_tokens=200) -> str` — `llm.py:270`.
- `async vision_chat_stream(messages, *, temperature=0.2, max_tokens=None) -> AsyncIterator[str]` — `llm.py:303`.
- `async embed_texts(texts, *, model=None) -> List[List[float]]` — `llm.py:330`.

**Control flow** (representative path — `stream_chat_events`, the hot path for every chat engine)
1. `llm.py:224` `resolve_model_choice(model_choice)` → `(base_url, api_key, model_id)`; `"fast"` →
   `settings.router_base_url`/`settings.router_model` (`llm.py:164-165`), anything else →
   `settings.openai_base_url`/`settings.llm_model` (`llm.py:166`).
2. `llm.py:225` `_client(base_url, api_key)` → **a brand-new `AsyncOpenAI` per call**
   (`llm.py:73-79`), `timeout=settings.llm_request_timeout`.
3. `llm.py:229-234` `apply_reasoning_effort` (no-op) → `normalize_system` (folds every system block
   into one at index 0, `llm.py:53-69`) → `await context.fit_request(...)` which probes
   `POST {root}/tokenize` (`orchestrator/app/context.py:105-121`) and trims/clips until
   prompt+completion fit; returns `(sized, budget)`.
4. `llm.py:235-243` `await client.chat.completions.create(..., stream=True, extra_body=thinking_body(wants_thinking(...)))`.
5. `llm.py:244-263` iterate chunks; skip empty `choices`; extract `delta.reasoning` /
   `delta.reasoning_content` / `delta.model_extra[...]` (`llm.py:253-259`) → yield `("reasoning", …)`;
   yield `("token", delta.content)`.

Non-streaming path (`chat_completion`, `llm.py:99-114`) is identical minus the stream loop and
always forces `thinking_body(True)` (`llm.py:112`).
`router_chat_completion` (`llm.py:283-300`) additionally clips every message to
`settings.router_input_char_cap` (`llm.py:286`) and forces `thinking_body(False)` (`llm.py:298`).
`embed_texts` (`llm.py:342-348`) clips each input to `settings.embed_input_char_cap` and re-sorts
`resp.data` by `.index`.

**State & side effects**
- Network egress: HTTP POST to `settings.openai_base_url` (`llm.py:84`), `settings.router_base_url`
  (`llm.py:165`, `llm.py:283`), `settings.vision_base_url` (`llm.py:314`),
  `settings.embed_base_url` (`llm.py:342`). Indirect egress to `{root}/tokenize` via
  `context.fit_request` (`orchestrator/app/context.py:112`).
- GPU/model calls: all of the above are vLLM inference.
- No DB writes, no filesystem writes, no global mutation, no env reads (env is read once in
  `config.py`). Module docstring asserts "Nothing here performs network I/O at import time"
  (`llm.py:18`) — verified: only `from . import context` / `from .config import settings`
  (`llm.py:24-26`), and the `openai` import is deferred into `_client` (`llm.py:73`).
- **No token accounting**: `resp.usage` is never read anywhere in the file (`rg -n 'usage' llm.py`
  → no hits). Budgets come only from `context.fit_request`'s estimate.

**Dependencies**
- inbound (verified with `rg -n`): `orchestrator/app/summarize.py:14,74,88`;
  `orchestrator/app/recall.py:27,103,132`; `orchestrator/app/main.py:16,310,313,316,538,657`;
  `orchestrator/app/engines/search.py:23,186,200,412,450,487`;
  `orchestrator/app/engines/chat.py:20,90`; `engines/repo.py:13,128,170`;
  `engines/document.py:12,69`; `engines/vision.py:17,83`; `engines/url.py:101`;
  `engines/sql.py:18,113,211,343,372,429`; `engines/rag.py:19,38,139`;
  `engines/live_sf.py:20,71`; `engines/dataset.py:23,105`; `engines/report.py:20,133,201,218`;
  `engines/router.py:115,126`; `engines/agent.py:210,282,357`; `engines/orchestrate.py:138`.
- outbound: `app.context` (`llm.py:24,26`), `app.config.settings` (`llm.py:25`), `openai.AsyncOpenAI`
  (lazy, `llm.py:73`).

**Config** — `settings.llm_request_timeout` (`llm.py:78`), `settings.openai_base_url`
(`llm.py:84,103,130`), `settings.openai_api_key` (`llm.py:84,166`), `settings.llm_model`
(`llm.py:100,127,166`), `settings.router_base_url` (`llm.py:165,283,288`), `settings.router_model`
(`llm.py:165,289,293`), `settings.router_input_char_cap` (`llm.py:286`), `settings.vision_base_url`
(`llm.py:314`), `settings.vision_model` (`llm.py:316`), `settings.embed_base_url` (`llm.py:342`),
`settings.embed_input_char_cap` (`llm.py:343`), `settings.embed_model` (`llm.py:345`).

**Failure modes**
- **No `try`/`except` anywhere in the file.** Any `openai.APIError`, `APITimeoutError`,
  `APIConnectionError` or vLLM 400 propagates to the caller. Nothing is swallowed here.
- **Timeout**: `settings.llm_request_timeout` default **300.0 s** (`config.py:264`). Applied per
  attempt.
- **Retry**: not configured. `AsyncOpenAI` is constructed without `max_retries` (`llm.py:75-79`), so
  the SDK default applies — verified against the installed SDK: `openai 2.46.0`,
  `openai._constants.DEFAULT_MAX_RETRIES == 2`. Worst case per LLM call = 3 × 300 s = **900 s**.
- **Circuit breaker**: none. `rg -rn 'circuit|breaker|backoff' orchestrator/app/` returns no
  breaker implementation.
- **Client lifetime**: the `AsyncOpenAI` returned by `_client` (`llm.py:75`) is never `.close()`d and
  never reused — a new `httpx.AsyncClient` connection pool per LLM call, discarded without an
  `aclose()`.
- **`vision_chat_stream` does not call `context.fit_request`** (`llm.py:314-321`) — the only client
  path with no context sizing and no clipping; an oversized multimodal payload is an unhandled 400.
- **`embed_texts`** sends `input=[]` when `texts` is empty (`llm.py:346`) — vLLM 400, uncaught.
- Streaming generators (`llm.py:146`, `llm.py:244`, `llm.py:322`) have no `try/finally` closing the
  stream; abandoning the generator (client disconnect) leaves the HTTP response unclosed.

**Concurrency** — Fully `async`. No blocking calls inside `async def` (the `openai` import at
`llm.py:73` is the only sync work and is cached by `sys.modules` after the first call). No
module-level mutable state — `LOCAL_API_KEY`, `MODEL_CHOICES`, `REASONING_EFFORTS` are immutable.
No locks needed. Shared state (`context._window_cache`) lives in `context.py`, guarded by
`context._lock` (`orchestrator/app/context.py:130`).

**Complexity hotspots** — `stream_chat_events` `llm.py:212` = **52 LOC** (measured via `ast`); the
largest here but under 60. No function in this file exceeds 60 LOC.

**Notable**
- **Dead code**: `vision_chat_stream` (`llm.py:303-327`) has **no production caller** —
  `rg -n 'vision_chat_stream' --type py .` returns only `llm.py:303` and
  `orchestrator/tests/test_system_normalization.py:125`. `engines/vision.py:83` uses
  `llm.stream_chat_events(..., model_choice="smart")` instead.
- **Dead code**: `apply_reasoning_effort` (`llm.py:198-209`) is documented as "still a no-op
  passthrough" and returns `list(messages)`; `MODEL_CHOICES` (`llm.py:32`) has no consumer in
  `orchestrator/app/`.
- Docstring drift: `_openai_client` says "gpt-oss-120b" (`llm.py:83`) and the section header
  `llm.py:88` says the same, but `settings.llm_model` defaults to `Qwen/Qwen3.6-35B-A3B-NVFP4`
  (`config.py:56`). `config.py:45` carries the same stale comment.
- Magic numbers: `temperature=0.2` default (`llm.py:95,122,217,307`), `temperature=0.0` +
  `max_tokens=200` for the router (`llm.py:273-274`).
- Duplication: the `_client → fit_request → create` triple is repeated four times
  (`llm.py:99-113`, `126-145`, `224-243`, `283-299`).

---

### orchestrator/app/db.py  (1064 LOC)

**Purpose** — The entire app-state persistence layer: stdlib `sqlite3` at `settings.app_db_path`,
WAL, short-lived connection per operation. Holds users, conversations, messages, summaries,
embedded chunks, uploads, fetched URLs, cloned repos and repo chunks.

**Public surface**
Schema/lifecycle
- `_SCHEMA` (module constant, DDL script) — `db.py:22-134`.
- `_ADDED_CONVERSATION_COLUMNS = (("pinned", …), ("archived", …))` — `db.py:141-144`.
- `_ADDED_MESSAGE_COLUMNS = (("generation_id", "TEXT"),)` — `db.py:146`.
- `utcnow() -> str` — `db.py:149`.
- `migrate(con: sqlite3.Connection) -> None` — `db.py:153`.
- `connect() -> sqlite3.Connection` — `db.py:195`.

Users
- `create_user(username: str, password_hash: str) -> int` — `db.py:212`.
- `get_user_by_username(username: str) -> Optional[sqlite3.Row]` — `db.py:223`.
- `get_user_by_id(user_id: int) -> Optional[sqlite3.Row]` — `db.py:230`.

Conversations / messages
- `_conversation_dict(row) -> dict` — `db.py:241`.
- `list_conversations(user_id: int, archived: bool = False) -> List[dict]` — `db.py:250`.
- `create_conversation(user_id: int, conversation_id: str, title: str) -> dict` — `db.py:267`.
- `get_conversation(user_id: int, conversation_id: str) -> Optional[dict]` — `db.py:286`.
- `update_conversation(user_id, conversation_id, title=None, pinned=None, archived=None) -> Optional[dict]` — `db.py:296`.
- `delete_conversation(user_id: int, conversation_id: str) -> bool` — `db.py:334`.
- `list_messages(conversation_id: str) -> List[dict]` — `db.py:343` (**no `user_id` parameter**).
- `conversation_owner(conversation_id: str) -> Optional[int]` — `db.py:357`.
- `class MessageCountWouldShrink(Exception)` — `db.py:370`, `__init__(existing, incoming)` `db.py:381`.
- `class ConversationChanged(Exception)` — `db.py:562`, `__init__(expected, actual)` `db.py:565`.
- `truncate_messages(user_id, conversation_id, keep: int, expected_total: int) -> Optional[dict]` — `db.py:571`.
- `replace_messages(user_id, conversation_id, messages: List[dict]) -> Optional[dict]` — `db.py:616`.
- `add_message(user_id, conversation_id, role, content, meta=None) -> Optional[dict]` — `db.py:678`.

Summary / chunks (Phase A/B)
- `get_summary(conversation_id) -> Optional[dict]` — `db.py:394`.
- `save_summary(conversation_id, summary, covers_through, token_estimate) -> None` — `db.py:411`.
- `clear_summary(conversation_id) -> None` — `db.py:426`.
- `add_conversation_chunks(conversation_id, chunks: List[dict]) -> None` — `db.py:444`.
- `get_conversation_chunks(conversation_id) -> List[dict]` — `db.py:466`.

Uploads (Phase 4)
- `save_upload(upload_id, conversation_id, filename, size, status, profile=None, notes=None) -> None` — `db.py:490`.
- `get_uploads(conversation_id) -> List[dict]` — `db.py:518`.
- `get_upload(upload_id) -> Optional[dict]` — `db.py:542`.

URL documents (Phase 2)
- `save_url_document(conversation_id, url, title, text) -> None` — `db.py:755`.
- `get_url_documents(conversation_id) -> List[dict]` — `db.py:769`.
- `get_url_document_urls(conversation_id) -> set` — `db.py:780`.

Repos (Phase 3)
- `save_repo(conversation_id, repo_key, url, sha) -> None` — `db.py:794`.
- `get_repo(conversation_id, repo_key) -> Optional[dict]` — `db.py:805`.
- `get_repo_keys(conversation_id) -> List[str]` — `db.py:815`.
- `replace_repo_chunks(conversation_id, repo_key, chunks: List[dict]) -> None` — `db.py:824`.
- `search_repo_chunks(conversation_id, keywords: List[str], limit: int = 12) -> List[dict]` — `db.py:844`.

Search (V4 §2)
- `SEARCH_LIMIT_DEFAULT = 50` — `db.py:902`; `SEARCH_LIMIT_MAX = 100` — `db.py:903`.
- `_LIKE_ESCAPE = "\\"` `db.py:905`; `_SNIPPET_WIDTH = 120` `db.py:906`; `_ELLIPSIS = "…"` `db.py:907`.
- `like_contains_pattern(needle: str) -> str` — `db.py:910`.
- `snippet_window(content, needle, width=_SNIPPET_WIDTH) -> str` — `db.py:928`.
- `_SEARCH_SQL` — `db.py:958-975`.
- `_RECALL_SNIPPET_CHARS = 240` — `db.py:978`.
- `recall_conversations(user_id, keywords, exclude_conversation_id, limit=3) -> List[dict]` — `db.py:981`.
- `search_conversations(user_id, query, limit=SEARCH_LIMIT_DEFAULT) -> List[dict]` — `db.py:1022`.

**Full DDL inventory** (all in `_SCHEMA`, `db.py:22-134`)
| object | lines | notes |
|---|---|---|
| `users(id PK AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)` | `db.py:23-28` | |
| `conversations(id TEXT PK, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, title, created_at, updated_at, pinned INTEGER DEFAULT 0, archived INTEGER DEFAULT 0)` | `db.py:29-37` | id is **client-supplied** (`orchestrator/app/history.py:92`) |
| `messages(id PK AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role, content, meta TEXT, created_at TEXT, generation_id TEXT)` | `db.py:38-51` | |
| `conversation_summaries(conversation_id TEXT PK REFERENCES conversations(id) ON DELETE CASCADE, summary, covers_through INTEGER, token_estimate INTEGER, updated_at)` | `db.py:55-65` | |
| `conversation_chunks(id PK, conversation_id REFERENCES conversations(id) ON DELETE CASCADE, ordinal, role, text, embedding BLOB, created_at, UNIQUE(conversation_id, ordinal))` | `db.py:69-78` | |
| `idx_conversation_chunks_conv ON conversation_chunks(conversation_id, ordinal)` | `db.py:79-80` | redundant with the UNIQUE constraint's implicit index |
| `uploads(id TEXT PK, conversation_id TEXT NOT NULL, filename, bytes INTEGER, status, profile TEXT, notes TEXT, created_at)` | `db.py:84-93` | **no FK to conversations** |
| `idx_uploads_conversation ON uploads(conversation_id, created_at)` | `db.py:94-95` | |
| `idx_conversations_user ON conversations(user_id, updated_at DESC)` | `db.py:96-97` | |
| `idx_messages_conversation ON messages(conversation_id, id)` | `db.py:98-99` | |
| `url_documents(id PK, conversation_id TEXT NOT NULL, url, title, text, fetched_at, UNIQUE(conversation_id, url))` | `db.py:102-110` | **no FK** |
| `idx_url_documents_conv ON url_documents(conversation_id, id)` | `db.py:111-112` | |
| `repos(id PK, conversation_id TEXT NOT NULL, repo_key, url, sha, cloned_at, UNIQUE(conversation_id, repo_key))` | `db.py:114-122` | **no FK** |
| `repo_chunks(id PK, conversation_id TEXT NOT NULL, repo_key, path, start_line, end_line, text)` | `db.py:123-131` | **no FK** |
| `idx_repo_chunks_conv ON repo_chunks(conversation_id, repo_key, id)` | `db.py:132-133` | |
| `idx_messages_generation UNIQUE ON messages(conversation_id, generation_id) WHERE generation_id IS NOT NULL` | created in `migrate`, `db.py:187-191` | partial unique index |

**No index exists on `messages.content`** — the LIKE scans at `db.py:963`, `db.py:972`,
`db.py:995` are full table scans.

**Control flow** — `connect()` (`db.py:195-205`), executed by **every** accessor in the file
1. `db.py:197` `Path(settings.app_db_path)`.
2. `db.py:198` `path.parent.mkdir(parents=True, exist_ok=True)` — filesystem write.
3. `db.py:199` `sqlite3.connect(str(path))` — **no `timeout=` argument** ⇒ stdlib default busy
   timeout of 5.0 s; **no `check_same_thread=False`**.
4. `db.py:200` `con.row_factory = sqlite3.Row`.
5. `db.py:201-202` `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`.
6. `db.py:203` `con.executescript(_SCHEMA)` — 16 `CREATE … IF NOT EXISTS` statements, every time.
7. `db.py:204` `migrate(con)`:
   - `db.py:161` `PRAGMA table_info(conversations)`;
   - `db.py:164-166` conditional `ALTER TABLE conversations ADD COLUMN pinned|archived`;
   - `db.py:167` `PRAGMA table_info(messages)`;
   - `db.py:169-171` conditional `ALTER TABLE messages ADD COLUMN generation_id`;
   - `db.py:181-186` **unconditional** `DELETE FROM messages WHERE generation_id IS NOT NULL AND id
     NOT IN (SELECT MIN(id) … GROUP BY conversation_id, generation_id)` — a full scan + group-by of
     `messages` on **every connection**;
   - `db.py:187-191` `CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_generation`;
   - `db.py:192` `con.commit()`.

`add_message` (`db.py:678-748`)
1. `db.py:695-696` `utcnow()`, extract `meta["generation_id"]`.
2. `db.py:697` `with closing(connect()) as con, con:` — the second `con` is the transaction CM.
3. `db.py:698-703` ownership probe `SELECT 1 FROM conversations WHERE id=? AND user_id=?` → `None`
   when not owned.
4. `db.py:704-719` nested closure `_existing_generation_row()`.
5. `db.py:721-735` `INSERT INTO messages (…, generation_id)`.
6. `db.py:736-743` on `sqlite3.IntegrityError`: if `generation_id` set → return the winning row
   with `"deduplicated": True`; **otherwise re-raise** (`db.py:743`).
7. `db.py:744-746` `UPDATE conversations SET updated_at`.

`replace_messages` (`db.py:616-675`)
1. `db.py:628-633` ownership probe.
2. `db.py:634-641` `COUNT(*)`; raise `MessageCountWouldShrink` if the payload is shorter.
3. `db.py:642-644` `DELETE FROM messages WHERE conversation_id = ?` — **every row**.
4. `db.py:645-670` re-INSERT each message with `created_at = now` (one timestamp for all,
   `db.py:626`, `db.py:667`), de-duplicating repeated `generation_id` in-payload (`db.py:652-656`).
5. `db.py:671-674` bump `updated_at`.

`truncate_messages` (`db.py:571-613`) — ownership probe (`db.py:587-592`), `COUNT(*)` optimistic
check raising `ConversationChanged` (`db.py:593-600`), `DELETE … id NOT IN (SELECT id … LIMIT keep)`
(`db.py:603-608`), bump `updated_at` (`db.py:609-612`).

`search_repo_chunks` (`db.py:844-894`) — builds SQL by f-string concatenation of *placeholder*
fragments (`db.py:854-874`) and binds every user value as a parameter in a hand-ordered list
(`db.py:877-883`). Uses `ESCAPE '\'` consistently (`db.py:855-860`). No injection: only `?` counts
vary with input.

`search_conversations` (`db.py:1022-1064`) — trims the query (`db.py:1033`), clamps
`limit` to `[1, SEARCH_LIMIT_MAX]` (`db.py:1036`), runs `_SEARCH_SQL` with a named `:pattern`
(`db.py:1038-1045`), then builds `snippet`/`matched_in` (`db.py:1046-1063`).

**State & side effects**
- Filesystem: `path.parent.mkdir(...)` on every `connect()` (`db.py:198`); SQLite file + `-wal` +
  `-shm` at `settings.app_db_path`.
- DB writes: `create_user` `db.py:216`; `create_conversation` `db.py:271`; `update_conversation`
  `db.py:324`; `delete_conversation` `db.py:336`; `truncate_messages` `db.py:603,609`;
  `replace_messages` `db.py:642,657,671`; `add_message` `db.py:722,744`; `save_summary` `db.py:415`;
  `clear_summary` `db.py:434,438`; `add_conversation_chunks` `db.py:451`; `save_upload` `db.py:500`;
  `save_url_document` `db.py:760`; `save_repo` `db.py:795`; `replace_repo_chunks` `db.py:829,833`;
  plus the migration DELETE + CREATE INDEX on **every** `connect()` (`db.py:181-191`).
- Network egress: **none**. GPU/model calls: **none**. Global mutation: **none** (no module-level
  mutable state). Env reads: **none directly** — only `settings.app_db_path` (`db.py:197`).
- Import-time side effects: none (docstring `db.py:8-9`; verified — only `from .config import
  settings` at `db.py:20`).

**Dependencies**
- inbound (`rg -n`): `orchestrator/app/auth.py:30,48,56,63,79,82`;
  `orchestrator/app/history.py:27` (every route: `history.py:100,110,112,122,140,158,165,206,216,229,243,252,261,270`);
  `orchestrator/app/uploads.py:23,76,124,130,141,164,167`;
  `orchestrator/app/health.py:76,79,81`;
  `orchestrator/app/recall.py:27`; `orchestrator/app/memory_recall.py:71`;
  `orchestrator/app/main.py:16,162,339,469,487,527,758,770`;
  `orchestrator/app/engines/repo.py:13,122`; `engines/dataset.py:23`; `engines/url.py:15`.
- outbound: stdlib `json`, `sqlite3`, `contextlib.closing`, `datetime`, `pathlib.Path`, `typing`
  (`db.py:13-18`); `app.config.settings` (`db.py:20`).

**Config** — `settings.app_db_path` — `db.py:197` (the only setting this module reads).

**Failure modes**
- `create_user` raises `sqlite3.IntegrityError` on a duplicate username (`db.py:213-214`, caught at
  `auth.py:80` and `history.py:101`).
- `create_conversation` raises `sqlite3.IntegrityError` on a duplicate id (`db.py:268`, caught at
  `history.py:101`).
- `truncate_messages` raises `ConversationChanged` (`db.py:600`).
- `replace_messages` raises `MessageCountWouldShrink` (`db.py:641`).
- `add_message` re-raises `sqlite3.IntegrityError` when no `generation_id` was supplied
  (`db.py:743`) → HTTP 500.
- **Nothing is swallowed inside db.py** — there is no bare `except` in the file. Swallowing happens
  in callers (`main.py:340`, `main.py:472`, `main.py:494`, `main.py:528`, `main.py:159` via
  `contextlib.suppress(Exception)`).
- **No busy timeout tuning** (`db.py:199`) ⇒ `sqlite3.OperationalError: database is locked` after
  5 s under write contention, surfacing as a 500.
- **No bound** on `list_messages` (`db.py:343-354`), `get_conversation_chunks` (`db.py:466`),
  `get_url_documents` (`db.py:769`), `get_uploads` (`db.py:518`) — whole-table reads for a
  conversation, unbounded row count and unbounded `text`/`embedding` size.
- **No pruning/TTL** for `uploads`, `url_documents`, `repos`, `repo_chunks`,
  `conversation_chunks` — rows only disappear via the FK cascade, which those four tables do not
  have (`db.py:84,102,114,123`).

**Concurrency**
- Every function is **synchronous**. Callers in `orchestrator/app/history.py` are plain `def`
  routes (`history.py:81,89,106,117,135,152,201,240,258,267`) so FastAPI runs them in the anyio
  threadpool. Callers in `orchestrator/app/main.py` are **`async def`** and call these blocking
  functions on the event loop: `main.py:275 async def chat` → `main.py:339 db.conversation_owner`,
  `main.py:471 _dbr.get_repo_keys`, `main.py:493 _db.get_url_documents`, `main.py:527
  db.get_uploads`; `main.py:746 async def chat_compact` → `main.py:758,770`. Each of those blocking
  calls also runs the full `executescript(_SCHEMA)` + `migrate()` path.
- No connection is shared across tasks or threads: `connect()` returns a fresh connection and
  `closing(...)` closes it in the same call (`db.py:215,224,231,257,270,287,323,335,344,363,395,414,433,449,468,499,520,543,586,627,697,759,771,782,795,806,817,828,884,1007,1037`).
  `check_same_thread` is left at its default `True`, which is safe under this pattern.
- No module-level mutable state ⇒ no in-process race. Cross-process/cross-thread races are handled
  by SQL: the partial unique index `idx_messages_generation` (`db.py:187-191`) is the documented
  race fix (`db.py:173-179`); `truncate_messages` uses optimistic concurrency on `expected_total`
  (`db.py:599-600`).
- Race window: `replace_messages` `DELETE` then re-`INSERT` inside one transaction
  (`db.py:642-670`) is atomic, but the read-modify-write in the **client** that produces `messages`
  is not — two tabs syncing concurrently both pass the count check and the later write wins whole.

**Complexity hotspots** (measured with `ast`)
- `add_message` — `db.py:678`, **71 LOC**; contains a nested function definition inside a
  transaction context (`db.py:704`) plus a try/except-with-fallback-return branch.
- `replace_messages` — `db.py:616`, **60 LOC**.
- `search_repo_chunks` — `db.py:844`, **51 LOC**; three separately-built SQL fragments plus a
  hand-ordered parameter list whose correctness depends on a comment (`db.py:875-876`).
- `search_conversations` — `db.py:1022`, **43 LOC**. `truncate_messages` — `db.py:571`, **43 LOC**.
- `migrate` — `db.py:153`, **40 LOC**. `recall_conversations` — `db.py:981`, **39 LOC**.
- No function in this file exceeds 60 LOC except `add_message`.

**Notable**
- No TODO/FIXME/HACK markers.
- Magic numbers: `_SNIPPET_WIDTH = 120` (`db.py:906`), `_RECALL_SNIPPET_CHARS = 240` (`db.py:978`),
  `SEARCH_LIMIT_DEFAULT = 50` / `SEARCH_LIMIT_MAX = 100` (`db.py:902-903`), `limit: int = 12`
  (`db.py:845`), `limit: int = 3` (`db.py:985`), doc-file score penalty `2` (`db.py:866`), path
  weight `2` (`db.py:856`).
- Duplication: the ownership probe `SELECT 1 FROM conversations WHERE id = ? AND user_id = ?` is
  written three times verbatim (`db.py:588-590`, `db.py:629-631`, `db.py:699-701`).
- Duplication: `like_contains_pattern` + `ESCAPE '\'` appears in three separately hand-built SQL
  strings (`db.py:852-861`, `db.py:963-972`, `db.py:995`).
- `migrate`'s destructive `DELETE` (`db.py:181-186`) is written as a one-time repair but has no
  guard making it one-time.
- `idx_conversation_chunks_conv` (`db.py:79-80`) duplicates the index SQLite already creates for
  `UNIQUE(conversation_id, ordinal)` (`db.py:77`).
- `list_messages` (`db.py:343`) is the only conversation accessor with **no ownership parameter**;
  correctness depends entirely on callers checking first (`history.py:110-112`, `main.py:758` do;
  `main.py:770` relies on the check at `main.py:758`).
- `_conversation_dict` (`db.py:241`) is applied in `list_conversations`/`get_conversation` but
  `search_conversations` builds booleans by hand (`db.py:1054-1055`) — two code paths for the same
  transformation.

---

### orchestrator/app/auth.py  (103 LOC)

**Purpose** — Collapses all identity to ONE local account. There is no login, no password check and
no session; `require_user` is a dependency that can never fail.

**Public surface**
- `router = APIRouter(prefix="/auth", tags=["auth"])` — `auth.py:32`.
- `SESSION_COOKIE = "ts_session"` — `auth.py:35` (documented as ignored, `auth.py:34`).
- `DEFAULT_LOCAL_USERNAME = "local"` — `auth.py:37`.
- `_cached_user_id: Optional[int] = None` — `auth.py:39` (module-level mutable global).
- `_local_username() -> str` — `auth.py:42`.
- `_oldest_user() -> Optional[sqlite3.Row]` — `auth.py:46`.
- `local_user() -> sqlite3.Row` — `auth.py:52`.
- `current_user(request: Request) -> Optional[sqlite3.Row]` — `auth.py:89`.
- `require_user(request: Request) -> sqlite3.Row` — `auth.py:95` — FastAPI dependency.
- `GET /auth/me` → `me() -> dict` — `auth.py:100-103`, returns `{"username": …, "local": True}`.

**Control flow** — `require_user` (`auth.py:95`) → `current_user` (`auth.py:89`) discards the
`Request` (`auth.py:91`) → `local_user` (`auth.py:52`):
1. `auth.py:54-59` if `_cached_user_id` is set, `db.get_user_by_id` and return; on `None` clear the
   cache and continue.
2. `auth.py:61-66` if `LOCAL_USERNAME` is set, `db.get_user_by_username`; on hit cache + return.
3. `auth.py:68-72` if `LOCAL_USERNAME` is **not** set, `_oldest_user()` → `SELECT * FROM users ORDER
   BY id LIMIT 1`; on hit cache + return.
4. `auth.py:77-86` create the account with the literal hash `"!local-no-login"` (`auth.py:79`),
   swallow `sqlite3.IntegrityError` as a race (`auth.py:80-81`), re-read, cache, return; raise
   `RuntimeError` if both create and lookup fail (`auth.py:83-84`).

**State & side effects**
- Mutates the module-level global `_cached_user_id` at `auth.py:59, 65, 71, 85`.
- DB reads via `db.get_user_by_id` (`auth.py:56`), `db.get_user_by_username` (`auth.py:63, 82`),
  raw `SELECT` on a connection it opens itself (`auth.py:48-49`).
- **DB write**: `db.create_user(username, "!local-no-login")` (`auth.py:79`) on a fresh install.
- **Connection leak**: `_oldest_user` (`auth.py:48`) calls `db.connect()` and never closes it — no
  `closing(...)`, no `con.close()`; the connection object goes out of scope unreferenced.
- Env read: `os.environ.get("LOCAL_USERNAME")` at `auth.py:43` and `auth.py:61`.
- No network egress, no GPU calls, no filesystem writes beyond what `db.connect()` does.

**Dependencies**
- inbound: `orchestrator/app/main.py:17` (`auth_router`, mounted at `main.py:58`),
  `main.py:324,327`, `main.py:695,697`, `main.py:753,755`;
  `orchestrator/app/history.py:28` + every route dependency
  (`history.py:82,90,107,120,138,155,204,241,259,270`);
  `orchestrator/app/uploads.py:24,70,162`.
- outbound: `os`, `sqlite3`, `typing` (`auth.py:24-26`); `fastapi.APIRouter`, `fastapi.Request`
  (`auth.py:28`); `app.db` (`auth.py:30`).

**Config** — `LOCAL_USERNAME` (env) — `auth.py:43`, `auth.py:61`. Not present in `Settings`
(`config.py`) and not set in `docker-compose.yml`.

**Failure modes**
- `except sqlite3.IntegrityError: pass` — `auth.py:80-81` (deliberate, race on concurrent create).
- `RuntimeError` at `auth.py:84` when both create and lookup fail — becomes a 500.
- Any DB failure in `local_user` propagates into **every** request, including `GET /health`
  indirectly and every `/history/*` route.
- `require_user` **never raises 401/403** — `auth.py:96` docstring says so explicitly.
- No rate limiting, no CSRF token, no origin check beyond CORS (`main.py:48-53`), no bearer token,
  no mTLS.

**Concurrency** — Synchronous. `local_user()` is called from the anyio threadpool (`history.py`,
`uploads.py` sync routes) *and* directly on the event loop from `async def chat`
(`main.py:275` → `main.py:327`) and `async def chat_compact` (`main.py:746` → `main.py:755`) — a
blocking `sqlite3` round trip (plus the full `connect()` schema+migrate path) inside the event loop
on every chat request. `_cached_user_id` (`auth.py:39`) is read-modify-written without a lock at
`auth.py:54-59` and `auth.py:65/71/85`; two threads can both miss the cache and both call
`db.create_user`, which is why `auth.py:80` swallows `IntegrityError`. The window is benign (both
resolve to the same row) but it is a real unguarded global.

**Complexity hotspots** — `local_user` `auth.py:52` = **35 LOC**, four early-return branches. No
function over 60 LOC.

**Notable**
- **The security model is "none".** `auth.py:17-20` states it: "there is now no authentication
  whatsoever. Anyone who can reach the port can read every conversation and query the Salesforce
  data." The compose file publishes `"8080:8080"` (`docker-compose.yml:273`) with **no host-IP
  bind**, i.e. `0.0.0.0`.
- Dead constant: `SESSION_COOKIE` (`auth.py:35`) has no reader in `orchestrator/app/`
  (`rg -n 'SESSION_COOKIE'` → `auth.py:35` only, plus tests).
- Password hashing dependencies remain installed but unused: `argon2-cffi>=23.1` and
  `itsdangerous>=2.1` (`orchestrator/requirements.txt`, "V2 auth" block).
- `main.py:46` still comments "allow_credentials so the ts_session cookie flows" — stale.
- `main.py:702-708` `_owns()` compares `gen.user_id` to `_viewer_id(request)`; since every caller
  resolves to the same `local_user()` id, this check is a tautology and `/chat/attach`,
  `/chat/stop`, `/chat/active` are open to any client that can reach the port.
- `main.py:756-757` `if user is None: raise HTTPException(401)` is unreachable — `current_user`
  never returns `None` (`auth.py:92`).

---

### orchestrator/app/config.py  (271 LOC)

**Purpose** — One `Settings` object built from `os.environ` at import time; the single source of
truth for every tunable in the orchestrator.

**Public surface**
- `_TRUTHY = {"1", "true", "yes", "on"}` — `config.py:13`.
- `_bool(name: str, default: bool) -> bool` — `config.py:16`.
- `_int(name: str, default: int) -> int` — `config.py:23`.
- `_float(name: str, default: float) -> float` — `config.py:30`.
- `CHART_TRIGGER_MODES = ("explicit", "hybrid")` — `config.py:38`.
- `class Settings` — `config.py:41`; `Settings.__init__` — `config.py:44`.
- `settings = Settings()` — `config.py:271` (module-level singleton, constructed on import).

**Every setting, default, validation** (all inside `Settings.__init__`)
| attribute | env var | default | line | validation |
|---|---|---|---|---|
| `openai_base_url` | `OPENAI_BASE_URL` | `http://vllm:30000/v1` | 46 | none (no `rstrip("/")`, unlike the sidecars) |
| `openai_api_key` | `OPENAI_API_KEY` | `local` | 48 | none |
| `llm_model` | `MAIN_MODEL` ∥ `LLM_MODEL` | `Qwen/Qwen3.6-35B-A3B-NVFP4` | 53-57 | none |
| `router_base_url` | `ROUTER_BASE_URL` | `http://vllm-router:30002/v1` | 61-63 | `.rstrip("/")` |
| `router_model` | `ROUTER_MODEL` | `Qwen/Qwen3-VL-8B-Instruct-FP8` | 64-66 | none |
| `agent_base_url` | `AGENT_BASE_URL` | `http://vllm-router:30002/v1` | 70-72 | `.rstrip("/")` — **0 readers** |
| `agent_model` | `AGENT_MODEL` | `Qwen/Qwen3-VL-8B-Instruct-FP8` | 73-75 | **0 readers** |
| `vision_base_url` | `VISION_BASE_URL` | `http://vllm:30000/v1` | 76-78 | `.rstrip("/")` |
| `vision_model` | `VISION_MODEL` | `Qwen/Qwen3.6-35B-A3B-NVFP4` | 79 | none |
| `embed_base_url` | `EMBED_BASE_URL` | `http://vllm-embed:30003/v1` | 80-82 | `.rstrip("/")` |
| `embed_model` | `EMBED_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | 83 | none |
| `rerank_enabled` | `RERANK_ENABLED` | `True` | 86 | `_bool` |
| `rerank_model` | `RERANKER_MODEL` ∥ `RERANK_MODEL` | `Qwen/Qwen3-Reranker-0.6B` | 89-93 | none |
| `duckdb_path` | `DUCKDB_PATH` | `/data/warehouse.duckdb` | 96 | none |
| `lancedb_dir` | `LANCEDB_DIR` | `/data/lancedb` | 97 | none |
| `lancedb_table` | `LANCEDB_TABLE` | `chunks` | 98 | none |
| `parquet_dir` | `PARQUET_DIR` | `/data/parquet` | 99 | none |
| `reports_dir` | `REPORTS_DIR` | `/reports` | 100 | none |
| `sf_lightning_base_url` | `SF_LIGHTNING_BASE_URL` | `https://techsara.lightning.force.com` | 103-105 | `.rstrip("/")` |
| `default_max_context` | `DEFAULT_MAX_CONTEXT` | `32768` | 109 | `_int` — **0 readers** |
| `report_max_context` | `REPORT_MAX_CONTEXT` | `65536` | 110 | `_int` — **0 readers** |
| `sf_client_id` | `SF_CLIENT_ID` | `""` | 118 | none |
| `sf_client_secret` | `SF_CLIENT_SECRET` | `""` | 119 | none (secret) |
| `sf_login_url` | `SF_LOGIN_URL` | `""` | 120 | none |
| `sf_private_key_b64` | `SF_PRIVATE_KEY_B64` | `""` | 121 | none (secret) |
| `sf_api_version` | `SF_API_VERSION` | `v61.0` | 122 | none |
| `sf_live_timeout` | `SF_LIVE_TIMEOUT` | `45` | 123 | **raw `float(...)`, not `_float`** — empty string ⇒ `ValueError` at import |
| `sf_live_enabled` | `SF_LIVE_ENABLED` | `true` | 124-126 | ad-hoc `not in ("0","false","no")` — **different semantics from `_bool`** |
| `model_max_context` | `MODEL_MAX_CONTEXT` | `262144` | 127 | `_int` |
| `model_max_output` | `MODEL_MAX_OUTPUT` | `8192` | 128 | `_int` |
| `context_safety_margin` | `CONTEXT_SAFETY_MARGIN` | `512` | 131 | `_int` |
| `tokenize_timeout` | `TOKENIZE_TIMEOUT` | `5.0` | 135 | `_float` |
| `router_input_char_cap` | `ROUTER_INPUT_CHAR_CAP` | `6000` | 138 | `_int` |
| `embed_input_char_cap` | `EMBED_INPUT_CHAR_CAP` | `8000` | 141 | `_int` |
| `context_warn_threshold` | `CONTEXT_WARN_THRESHOLD` | `0.60` | 146 | `_float`, **no range check** |
| `context_bg_compact_threshold` | `CONTEXT_BG_COMPACT_THRESHOLD` | `0.70` | 148-150 | `_float`, no range check |
| `context_compact_threshold` | `CONTEXT_COMPACT_THRESHOLD` | `0.80` | 152-154 | `_float`, no range check |
| `keep_recent_turns` | `KEEP_RECENT_TURNS` | `8` | 155 | `_int` |
| `summary_max_tokens` | `SUMMARY_MAX_TOKENS` | `2000` | 156 | `_int` |
| `min_output_floor` | `MIN_OUTPUT_FLOOR` | `1024` | 160 | `_int` |
| `semantic_recall_enabled` | `SEMANTIC_RECALL_ENABLED` | `True` | 163 | `_bool` |
| `retrieve_top_k` | `RETRIEVE_TOP_K` | `6` | 164 | `_int` |
| `context_meter_enabled` | `CONTEXT_METER_ENABLED` | `True` | 167 | `_bool` — **0 readers** |
| `dataset_uploads_enabled` | `DATASET_UPLOADS_ENABLED` | `True` | 171 | `_bool` |
| `upload_max_mb` | `UPLOAD_MAX_MB` | `200` | 172 | `_int`, no lower bound |
| `archive_max_uncompressed_mb` | `ARCHIVE_MAX_UNCOMPRESSED_MB` | `2048` | 175-177 | `_int` |
| `archive_max_files` | `ARCHIVE_MAX_FILES` | `10000` | 178 | `_int` |
| `archive_max_ratio` | `ARCHIVE_MAX_RATIO` | `200` | 179 | `_int` |
| `archive_max_depth` | `ARCHIVE_MAX_DEPTH` | `1` | 182 | `_int` |
| `profile_sample_rows` | `PROFILE_SAMPLE_ROWS` | `5` | 184 | `_int` |
| `profile_cell_chars` | `PROFILE_CELL_CHARS` | `200` | 185 | `_int` |
| `profile_top_values` | `PROFILE_TOP_VALUES` | `5` | 186 | `_int` |
| `profile_max_files` | `PROFILE_MAX_FILES` | `40` | 187 | `_int` |
| `profile_max_columns` | `PROFILE_MAX_COLUMNS` | `60` | 188 | `_int` |
| `search_enabled` | `SEARCH_ENABLED` | `False` | 192 | `_bool` |
| `search_provider` | `SEARCH_PROVIDER` | `searxng` | 193 | **not validated here** — validated late in `search/base.py:58` |
| `searxng_url` | `SEARXNG_URL` | `""` | 194 | `.rstrip("/")` |
| `tavily_api_key` | `TAVILY_API_KEY` | `""` | 195 | none (secret) |
| `brave_api_key` | `BRAVE_API_KEY` | `""` | 196 | none (secret) |
| `search_max_results` | `SEARCH_MAX_RESULTS` | `100` | 197 | `_int`, **no upper bound** |
| `fetch_timeout_ms` | `FETCH_TIMEOUT_MS` | `8000` | 198 | `_int` |
| `fetch_max_bytes` | `FETCH_MAX_BYTES` | `5_000_000` | 199 | `_int` |
| `search_source_char_budget` | `SEARCH_SOURCE_CHAR_BUDGET` | `8000` | 202 | `_int` |
| `search_rate_per_min` | `SEARCH_RATE_PER_MIN` | `10` | 203 | `_int` |
| `search_cache_ttl` | `SEARCH_CACHE_TTL` | `900.0` | 204 | `_float` |
| `url_analysis_enabled` | `URL_ANALYSIS_ENABLED` | `True` | 207 | `_bool` |
| `url_max_pages` | `URL_MAX_PAGES` | `5` | 208 | `_int` |
| `repo_analysis_enabled` | `REPO_ANALYSIS_ENABLED` | `True` | 211 | `_bool` |
| `repo_max_mb` | `REPO_MAX_MB` | `300` | 212 | `_int` |
| `repo_max_files` | `REPO_MAX_FILES` | `20000` | 213 | `_int` |
| `workspace_dir` | `WORKSPACE_DIR` | `/data/workspaces` | 214 | none |
| `workspace_ttl_hours` | `WORKSPACE_TTL_HOURS` | `24` | 215 | `_int` |
| `workspace_quota_gb` | `WORKSPACE_QUOTA_GB` | `20` | 216 | `_int` |
| `repo_final_chunks` | `REPO_FINAL_CHUNKS` | `12` | 217 | `_int` |
| `chart_trigger_mode` | `CHART_TRIGGER_MODE` | `explicit` | 230-231 | **the only whitelist-validated setting** — unknown ⇒ `explicit` |
| `sql_preview_row_cap` | `SQL_PREVIEW_ROW_CAP` | `PREVIEW_ROW_CAP` = 500 | 234 | `_int` |
| `export_row_cap` | `EXPORT_ROW_CAP` | `EXPORT_ROW_CAP` = 100_000 | 235 | `_int` |
| `rag_top_k` | `RAG_TOP_K` | `30` | 238 | `_int` |
| `rag_final_k` | `RAG_FINAL_K` | `8` | 239 | `_int` |
| `cors_allow_origins` | `CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | 244-250 | split on `,`, blanks dropped; **`*` accepted verbatim** |
| `app_db_path` | `APP_DB_PATH` | `/data/app.sqlite3` | 255 | none; **not set in docker-compose.yml** |
| `session_secret_file` | `SESSION_SECRET_FILE` | `/data/.session_secret` | 258-260 | **0 readers** (auth has no session) |
| `session_max_turns` | `SESSION_MAX_TURNS` | `20` | 263 | `_int` |
| `llm_request_timeout` | `LLM_REQUEST_TIMEOUT` | `300.0` | 264 | `_float` |
| `schema_cache_ttl` | `SCHEMA_CACHE_TTL` | `300.0` | 265 | `_float` — **0 readers** |
| `health_probe_timeout` | `HEALTH_PROBE_TIMEOUT` | `2.0` | 268 | `_float` |

Dead-setting counts verified with `rg -c 'settings\.<name>' orchestrator/app/`:
`default_max_context 0`, `report_max_context 0`, `schema_cache_ttl 0`, `agent_base_url 0`,
`agent_model 0`, `context_meter_enabled 0`, `session_secret_file 0`.

**Control flow**
1. Import `config` → `config.py:11` `from .core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP`
   (pure constants, `orchestrator/app/core/exports.py:15-16`).
2. `config.py:271` `settings = Settings()` executes `__init__` (`config.py:44-268`) top to bottom;
   ~90 `os.environ.get` reads, each coerced by `_bool`/`_int`/`_float` or used raw.
3. `_bool` (`config.py:16-20`): `None` or blank ⇒ default; else membership in `_TRUTHY`
   (case-insensitive) — anything else is `False`.
4. `_int` (`config.py:23-27`) / `_float` (`config.py:30-34`): `None` or blank ⇒ default; else
   `int(raw)` / `float(raw)` — **no try/except, no range check**.

**State & side effects** — env reads only (~90). No network, no filesystem, no DB, no GPU
(docstring `config.py:4-5`). The object is a process-lifetime singleton; changing an env var after
import has no effect. Tests mutate `settings` attributes directly
(`orchestrator/tests/conftest.py:25`, `tests/test_history.py:13`, etc.).

**Dependencies**
- inbound: everything. Directly `from .config import settings` in `llm.py:25`, `db.py:20`,
  `health.py:21`, `uploads.py:25`, `search/base.py:13`, `context.py`, `main.py:18`, every engine.
- outbound: `os` (`config.py:9`), `app.core.exports` (`config.py:11`).

**Config** — see the table; every env var this module consumes is listed with its line.

**Failure modes**
- **`_int`/`_float` raise unhandled `ValueError` at import.** `UPLOAD_MAX_MB=200MB` ⇒
  `int("200MB")` at `config.py:27` ⇒ the whole `import app.main` fails and uvicorn never starts,
  with a raw traceback and no indication of which variable is bad.
- `sf_live_timeout` (`config.py:123`) uses raw `float(os.environ.get("SF_LIVE_TIMEOUT", "45"))`
  rather than `_float`, so `SF_LIVE_TIMEOUT=` (present-but-empty, the shape docker-compose's
  `${VAR:-}` produces) is `float("")` ⇒ `ValueError` at import, where every other numeric setting
  tolerates it.
- `sf_live_enabled` (`config.py:124-126`) uses a hand-rolled truthiness test; `SF_LIVE_ENABLED=off`
  evaluates **True** while `_bool` would give **False**.
- No range validation anywhere except `chart_trigger_mode`. Negative or zero values are accepted for
  `upload_max_mb`, `search_max_results`, `keep_recent_turns`, `rag_top_k`, `workspace_quota_gb`, the
  three context thresholds, etc.
- `cors_allow_origins` accepts `*` verbatim (`config.py:244-250`); combined with
  `allow_credentials=True` (`main.py:50`) that is the classic CORS misconfiguration.
- Nothing is swallowed (no `try`/`except` in the file).

**Concurrency** — Synchronous, executed once at import. `settings` is a shared mutable object read
from every thread and the event loop; nothing mutates it at runtime in `app/` (only tests do), so
there is no race in production.

**Complexity hotspots** — `Settings.__init__` — `config.py:44`, **225 LOC** (measured with `ast`);
the largest function in the entire assignment. Straight-line, so cyclomatic complexity is low
(~8 branches from `or` fallbacks and the `chart_trigger_mode` guard), but it is a single
unreadable block with 20 comment-delimited sections and no grouping into sub-objects.

**Notable**
- No TODO/FIXME/HACK markers.
- Stale comments: `config.py:45` "gpt-oss-120b via vLLM" while the default model is
  `Qwen/Qwen3.6-35B-A3B-NVFP4` (`config.py:56`); `config.py:112-117` the "Phase 6 token budget"
  comment block is interrupted by unrelated Salesforce settings (`config.py:118-126`) before the
  settings it describes (`config.py:127-131`) — the comment and its code are separated by 9 lines
  of a different concern.
- Dead settings (7, listed above) — `session_secret_file` in particular documents a
  session-cookie signing mechanism that `auth.py` removed entirely.
- `.env.example:56` ships `SEARCH_MAX_RESULTS=100`; the live `.env` sets `SEARCH_MAX_RESULTS=10`
  (value read for config-drift purposes; not a secret). This value is passed verbatim to
  `provider.search(q, settings.search_max_results)` at `orchestrator/app/engines/search.py:266`.
- `openai_base_url` (`config.py:46`) is the only base URL **not** `.rstrip("/")`-normalised;
  `health.service_root` (`health.py:30-33`) and `context.service_root` compensate, but
  `llm._client` passes it to `AsyncOpenAI` raw.
- Secret-bearing settings (names only, per the no-values rule): `openai_api_key` `config.py:48`,
  `sf_client_secret` `config.py:119`, `sf_private_key_b64` `config.py:121`, `tavily_api_key`
  `config.py:195`, `brave_api_key` `config.py:196`. `.env`, `.env.bak-*` and `secrets/` are all
  git-ignored (`.gitignore:10`, `.gitignore:47`, `.gitignore:13`) and `git ls-files` shows only
  `.env.example` tracked — **no secret is committed**.

---

### orchestrator/app/health.py  (131 LOC)

**Purpose** — Concurrent dependency probes behind `GET /health`: the deduplicated vLLM endpoints,
the DuckDB warehouse, and the app SQLite DB.

**Public surface**
- `service_root(base_url: str) -> str` — `health.py:24`.
- `async _probe_vllm(client: httpx.AsyncClient, base_url: str) -> dict` — `health.py:36`.
- `_check_duckdb(path: str) -> dict` — `health.py:48`.
- `_check_app_db(path: str) -> dict` — `health.py:68`.
- `async check_dependencies() -> dict` — `health.py:94`; returns
  `{"status": "ok"|"degraded", "checks": {name: {"status": …, "detail"?: …}}}`.

The HTTP route lives in `main.py`: `@app.get("/health")` / `async def health()` —
`orchestrator/app/main.py:242-243`, body `main.py:248-254`.

**Control flow** — `check_dependencies` (`health.py:94-131`)
1. `health.py:105-110` build the fixed list `[("vllm", openai_base_url), ("vllm-router",
   router_base_url), ("vllm-vision", vision_base_url), ("vllm-embed", embed_base_url)]`.
2. `health.py:111-117` dedupe by **exact URL string**; with the shipped compose values
   (`docker-compose.yml:229,233,237,240`) `vllm` and `vllm-vision` collapse to one probe.
3. `health.py:119` `async with httpx.AsyncClient(timeout=settings.health_probe_timeout)`.
4. `health.py:120-124` `asyncio.gather` of N `_probe_vllm` coroutines **plus**
   `asyncio.to_thread(_check_duckdb, settings.duckdb_path)` **plus**
   `asyncio.to_thread(_check_app_db, settings.app_db_path)`.
5. `_probe_vllm` (`health.py:36-45`): `GET {service_root(base)}/health`; any exception →
   `{"status": "error", "detail": f"{type(exc).__name__}: {exc}"}` (`health.py:41-42`); non-200 →
   `detail = f"HTTP {resp.status_code} from {url}"` (`health.py:45`).
6. `_check_duckdb` (`health.py:48-65`): lazy `import duckdb` (`health.py:51`), `duckdb.connect(path,
   read_only=True, config={"enable_external_access": False})` (`health.py:54-58`), `SELECT 1`,
   `close()` in `finally`; any exception → error dict (`health.py:63-64`).
7. `_check_app_db` (`health.py:68-91`): lazy `from . import db` (`health.py:76`),
   `db.closing(db.connect())` (`health.py:79`) — **this runs the whole `_SCHEMA` + `migrate()` write
   path**, then `PRAGMA table_info(messages)` and asserts `generation_id` is present
   (`health.py:80-88`); bare `except Exception` → error dict (`health.py:90-91`).
8. `health.py:125-129` zip names to results, append `duckdb` and `app_db`.
9. `health.py:130` overall = `"ok"` only if every check is `"ok"`, else `"degraded"`.

**State & side effects**
- Network egress: `GET http://vllm:30000/health`, `http://vllm-router:30002/health`,
  `http://vllm-embed:30003/health` (derived from `settings.*_base_url`, `health.py:38`).
- Filesystem: opens `settings.duckdb_path` read-only (`health.py:54`); opens/creates
  `settings.app_db_path` **read-write** via `db.connect()` (`health.py:79`), which `mkdir`s the
  parent (`db.py:198`) and executes the migration DDL/DML (`db.py:203-204`).
- **DB writes**: yes, indirectly — every `/health` hit re-runs `executescript(_SCHEMA)` and
  `migrate()`'s `DELETE … CREATE UNIQUE INDEX … commit()` (`db.py:181-192`).
- No GPU calls, no global mutation, no direct env reads.

**Dependencies**
- inbound: `orchestrator/app/main.py:21` `from .health import check_dependencies`, called at
  `main.py:248`.
- outbound: `asyncio`, `typing` (`health.py:16-17`), `httpx` (`health.py:19`),
  `app.config.settings` (`health.py:21`), lazy `duckdb` (`health.py:51`), lazy `app.db`
  (`health.py:76`).

**Config** — `settings.health_probe_timeout` (`health.py:119`), `settings.openai_base_url`
(`health.py:106`), `settings.router_base_url` (`health.py:107`), `settings.vision_base_url`
(`health.py:108`), `settings.embed_base_url` (`health.py:109`), `settings.duckdb_path`
(`health.py:122`), `settings.app_db_path` (`health.py:123`).

**Failure modes**
- `_probe_vllm` catches **bare `Exception`** (`health.py:41`) — by design, `/health` must not 500.
- `_check_duckdb` catches bare `Exception` (`health.py:63`).
- `_check_app_db` catches bare `Exception` (`health.py:90`, annotated `# noqa: BLE001`).
- **`settings.health_probe_timeout` bounds only the HTTP probes.** The two
  `asyncio.to_thread(...)` calls (`health.py:122-123`) have **no timeout**; `db.connect()` inherits
  sqlite3's 5 s busy timeout (`db.py:199`) and DuckDB's own lock behaviour, so `/health` can take
  well over the documented 2 s despite the docstring's "/health stays fast even when every
  dependency is down" (`health.py:10-12`).
- No retry, no caching — every `/health` call re-probes everything and re-runs the SQLite
  migration.
- **`/health` returns HTTP 200 even when `status == "degraded"`** (`main.py:243-254` returns a plain
  dict). Any probe that keys on the status code sees a healthy service.
- **Liveness, not readiness, for the model layer**: vLLM's `GET /health` reports that the engine
  process is up. It does **not** confirm the *configured model id* is served, so a vLLM started with
  a different `--served-model-name` reports `ok` here while every `chat.completions.create` 404s.
  `GET /v1/models` would catch that; it is not probed.
- Information disclosure: `detail` strings embed exception text and internal URLs
  (`health.py:42,45,64,91`) on an endpoint with no authentication.

**Concurrency** — `check_dependencies` is `async`; the two blocking probes are correctly moved off
the loop with `asyncio.to_thread` (`health.py:122-123`). `_check_duckdb`/`_check_app_db` are
sync and documented as such (`health.py:49-50`, `health.py:76`). No shared mutable state; `seen`
and `vllm_targets` (`health.py:111-112`) are per-call locals. `asyncio.gather` without
`return_exceptions=True` is safe here because every probe swallows its own exceptions.

**Complexity hotspots** — `check_dependencies` — `health.py:94`, **38 LOC**. No function over
60 LOC.

**Notable**
- No TODO/FIXME/HACK markers.
- The result-slicing at `health.py:126` (`results[:-2]`) and `health.py:128-129` (`results[-2]`,
  `results[-1]`) is positional and silently breaks if another `to_thread` probe is appended —
  fragile coupling between `gather` order and unpack indices.
- `service_root` (`health.py:24-33`) is duplicated in `orchestrator/app/context.py` (used at
  `context.py:112`) — two copies of the `/v1`-stripping rule.
- Dedupe is by exact string (`health.py:113`), so `http://vllm:30000/v1` and
  `http://vllm:30000/v1/` would be probed twice — `openai_base_url` is the one base URL not
  `rstrip("/")`-normalised in `config.py:46`.
- `/health` has no rate limit and performs a SQLite write transaction per call.

---

### orchestrator/app/uploads.py  (172 LOC)

**Purpose** — `POST /uploads` streams a dataset/archive to disk under the per-conversation
workspace, extracts it, profiles it, and stores the profile in SQLite; `GET /uploads/{id}` lists a
conversation's uploads and marks TTL-swept ones expired.

**Public surface**
- `router = APIRouter(prefix="/uploads", tags=["uploads"])` — `uploads.py:28`.
- `_CHUNK = 1024 * 1024` — `uploads.py:30`.
- `upload_root(conversation_id: str, upload_id: str) -> str` — `uploads.py:33`.
- `bytes_available(conversation_id: str, upload_id: str) -> bool` — `uploads.py:38`.
- `async _stream_to_disk(upload: UploadFile, dest: str) -> int` — `uploads.py:44`.
- `POST /uploads` → `async create_upload(file: UploadFile = File(...), conversation_id: str =
  Form(...), user = Depends(require_user)) -> dict` — `uploads.py:66-157`.
- `GET /uploads/{conversation_id}` → `list_uploads(conversation_id: str, user =
  Depends(require_user)) -> dict` — `uploads.py:160-172`.

**Control flow** — `create_upload` (`uploads.py:67-157`)
1. `uploads.py:72-73` `if not settings.dataset_uploads_enabled: 404`.
2. `uploads.py:76-78` ownership: `db.conversation_owner(conversation_id)`; reject only when
   `owner is not None and owner != user["id"]` — an **unknown conversation id is accepted**.
3. `uploads.py:80-83` `upload_id = uuid4().hex`; `root = upload_root(...)`;
   `filename = os.path.basename(file.filename or "upload.bin")`;
   `raw_path = root/_original/<filename>`.
4. `uploads.py:85-90` `from .core.repo import enforce_quota_and_ttl; enforce_quota_and_ttl()`
   wrapped in **`except Exception: pass`**.
5. `uploads.py:92` `size = await _stream_to_disk(file, raw_path)` — **outside the try block**.
   `_stream_to_disk` (`uploads.py:45-63`): cap = `upload_max_mb * 1024 * 1024` (`uploads.py:46`),
   `os.makedirs(dirname(dest))` (`uploads.py:48`), 1 MiB read loop (`uploads.py:50-62`), on
   overflow `out.close()` + `os.unlink(dest)` + `HTTPException(413)` (`uploads.py:55-61`).
6. `uploads.py:96-121` extraction branch:
   - zip container and not `.xlsx` → `archive.extract` (`uploads.py:98-99`);
   - `.tar/.tar.gz/.tgz` or gzip sniff → `archive.extract` (`uploads.py:100-103`);
   - else single file → if it is a zip container, `archive.check_zip_container(..., label=
     "spreadsheet")` (`uploads.py:109-110`), then `shutil.copy2` into `extracted/`
     (`uploads.py:111-112`).
   - `uploads.py:115-119` fold `plan.skipped` / `plan.nested_archives` into `notes`.
   - `uploads.py:121` `profiles = profiler.profile_directory(extract_dir)`.
7. `uploads.py:122-127` `except archive.ArchiveError` → `rmtree(root)`, `db.save_upload(status=
   "rejected", notes=str(exc))`, `HTTPException(400, detail=str(exc))`.
8. `uploads.py:128-136` `except Exception` → `rmtree(root)`, `db.save_upload(status="failed",
   notes=type(exc).__name__)`, generic `HTTPException(400)`.
9. `uploads.py:139` `rmtree(root/_original)`.
10. `uploads.py:141-149` `db.save_upload(..., "ready", profiler.profile_json(profiles),
    "; ".join(notes[:20]) or None)`.
11. `uploads.py:150-157` return `{upload_id, filename, bytes, files, notes[:20], profile}`.

`list_uploads` (`uploads.py:161-172`) — same permissive ownership check (`uploads.py:164-166`),
`db.get_uploads` (`uploads.py:167`), then rewrite `status` to `"expired"` when
`bytes_available(...)` is false (`uploads.py:169-171`).

**Validation actually performed**
- **Size**: yes — streamed cap at `uploads.py:55`, bounded by `upload_max_mb` + one 1 MiB chunk.
- **MIME**: **none.** `file.content_type` is never read anywhere in the file.
- **Extension**: only used to *choose the extraction path* (`uploads.py:97,101`), never to reject.
  Any byte stream is accepted, written to disk and handed to `profile_directory`.
- **Filename**: `os.path.basename` only (`uploads.py:82`). Verified in Python 3:
  `basename("../../etc/passwd") == "passwd"` (safe), but `basename("..") == ".."` and
  `basename("/") == ""` (unsafe shapes that survive).
- **Content**: zip-bomb caps enforced by `core/archive.py` via `archive_max_*`
  (`config.py:175-182`); `.xlsx` routed through `check_zip_container` (`uploads.py:109-110`).

**Storage path** — `settings.workspace_dir + "/uploads/" + sanitized_conversation_id[:64] +
"/" + upload_id` (`uploads.py:33-35`); sanitizer keeps `isalnum()` plus `-` and `_` and truncates to
64 chars. Sub-paths `_original/` (`uploads.py:83`) and `extracted/` (`uploads.py:93`).

**Quota / TTL** — Only `enforce_quota_and_ttl()` at `uploads.py:88`, called **before** the write and
with its exceptions discarded (`uploads.py:89-90`). No post-write quota check, no per-conversation
upload count limit, no TTL/pruning of the `uploads` **rows** in SQLite (`db.py:84-93` has no FK, so
`delete_conversation` does not remove them).

**State & side effects**
- Filesystem writes: `os.makedirs` (`uploads.py:48`, `uploads.py:111`), the streamed file
  (`uploads.py:49-62`), `shutil.copy2` (`uploads.py:112`), `shutil.rmtree`
  (`uploads.py:123,129,139`), plus everything `archive.extract` writes under `extract_dir`.
- Filesystem **deletes** outside this request's tree: `enforce_quota_and_ttl` (`uploads.py:88` →
  `orchestrator/app/core/repo.py:94-119`) `shutil.rmtree`s top-level entries of
  `settings.workspace_dir` (`core/repo.py:108`, `core/repo.py:119`).
- DB writes: `db.save_upload` at `uploads.py:124`, `uploads.py:130`, `uploads.py:141`.
- DB reads: `db.conversation_owner` (`uploads.py:76,164`), `db.get_uploads` (`uploads.py:167`).
- No network egress, no GPU calls, no module-level mutable state, no direct env reads.

**Dependencies**
- inbound: `orchestrator/app/main.py:23` `from .uploads import router as uploads_router`, mounted at
  `main.py:59`. `db.get_uploads` is also read by `main.py:527` to set `dataset_ready`.
- outbound: `os`, `shutil`, `sqlite3`, `uuid`, `typing` (`uploads.py:15-19`); `fastapi` (`uploads.py:21`);
  `app.db` (`uploads.py:23`); `app.auth.require_user` (`uploads.py:24`); `app.config.settings`
  (`uploads.py:25`); `app.core.archive`, `app.core.profile` (`uploads.py:26`); lazy
  `app.core.repo.enforce_quota_and_ttl` (`uploads.py:86`).

**Config** — `settings.workspace_dir` (`uploads.py:35`), `settings.upload_max_mb`
(`uploads.py:46,60`), `settings.dataset_uploads_enabled` (`uploads.py:72`). Indirectly
`workspace_ttl_hours`/`workspace_quota_gb` through `enforce_quota_and_ttl`.

**Failure modes**
- `except Exception: pass` around `enforce_quota_and_ttl` — `uploads.py:89-90`. Deliberate
  ("housekeeping only; never blocks an upload") but it also means a wedged quota sweep is invisible.
- `except Exception` at `uploads.py:128` swallows the real cause and stores only
  `type(exc).__name__` (`uploads.py:132`) — no message, no traceback, no logging call anywhere in
  the file (`rg -n 'logg' uploads.py` → no hits).
- **`_stream_to_disk` is outside the try/except** (`uploads.py:92` vs. the try at `uploads.py:96`),
  so an `OSError` there leaves the partially-written tree on disk **and** returns a 500 with a
  traceback rather than a 4xx. Reachable: a multipart `filename` of `"/"` makes `filename == ""`
  (`os.path.basename("/") == ""`), `dest` becomes `<root>/_original/`, and `open(dest, "wb")` raises
  `IsADirectoryError`. A `filename` of `".."` gives `dest = <root>/_original/..` — also a directory.
- `HTTPException(413)` at `uploads.py:58` is raised **inside** the `with open(...)` block after
  `out.close()` and `os.unlink(dest)`; the partial parent directories remain.
- `bytes_available` (`uploads.py:41`) calls `any(os.scandir(root))` and never closes the iterator —
  relies on refcount finalisation for the directory fd.
- No timeout on the whole handler; a 200 MB archive extraction + profiling runs unbounded.
- No retry anywhere.
- No bound on the **number** of uploads per conversation or per process.

**Concurrency**
- `create_upload` is `async def` (`uploads.py:67`) but **almost all of its work is blocking**:
  `archive.extract` (`uploads.py:99,103`), `archive.check_zip_container` (`uploads.py:110`),
  `shutil.copy2` (`uploads.py:112`), `profiler.profile_directory` (`uploads.py:121`),
  `shutil.rmtree` (`uploads.py:123,129,139`), `enforce_quota_and_ttl` (`uploads.py:88`), and every
  `db.*` call (`uploads.py:76,124,130,141`) run **on the event loop**. Only `await upload.read()`
  (`uploads.py:51`) yields. A 200 MB zip extraction stalls every other request, including SSE chat
  streams.
- `list_uploads` is a sync `def` (`uploads.py:161`) → runs in the threadpool. Correct.
- Race window: `enforce_quota_and_ttl` (`uploads.py:88`) runs concurrently with any other in-flight
  upload's writes; `core/repo.py:108`/`:119` `rmtree` a top-level workspace entry with no lock, so a
  concurrent upload writing under that entry loses its files mid-write.
- No module-level mutable state; `_CHUNK` (`uploads.py:30`) is an int constant.

**Complexity hotspots** — `create_upload` — `uploads.py:67`, **91 LOC** (measured with `ast`);
3-way extraction branch (`uploads.py:98-113`) plus two exception handlers plus two `db.save_upload`
call sites duplicating the same argument list. Cyclomatic complexity ≈ 12.

**Notable**
- No TODO/FIXME/HACK markers.
- `upload_root` truncates the sanitized conversation id to 64 chars (`uploads.py:34`) — two distinct
  ids sharing a 64-char sanitized prefix collide into one directory. `history.py:93` already caps
  conversation ids at 64 chars, so this is currently unreachable but is an undocumented coupling.
- The permissive ownership rule `if owner is not None and ...` (`uploads.py:77`, `uploads.py:165`)
  is written the same way in both routes; contrast with `main.py:758` `if owner is None or owner !=
  ...` which is the strict form. Two different rules for the same concept in one codebase.
- Duplication: the 8-argument `db.save_upload(...)` call appears three times (`uploads.py:124`,
  `uploads.py:130`, `uploads.py:141`).
- Magic numbers: `_CHUNK = 1024*1024` (`uploads.py:30`), `[:64]` (`uploads.py:34`), `notes[:20]`
  (`uploads.py:148,155`).
- The docstring promises "never a 500" for expired datasets (`uploads.py:10-11`) — true for the
  expiry path, false for the `_stream_to_disk` path above.

---

### orchestrator/app/search/__init__.py  (0 LOC)

**Purpose** — Empty file marking `orchestrator/app/search` as a package.

**Public surface** — none. The file is **zero bytes** (`wc -l` → 0).

**Control flow** — n/a.

**State & side effects** — none.

**Dependencies** — inbound: `orchestrator/app/engines/search.py:26`
(`from ..search.base import SearchResult, SearchUnavailableError, get_provider`),
`orchestrator/tests/test_search_providers.py`. outbound: none.

**Config** — none. **Failure modes** — none. **Concurrency** — n/a.
**Complexity hotspots** — none.

**Notable** — Unlike `orchestrator/app/__init__.py:1` it carries no docstring.

---

### orchestrator/app/search/base.py  (58 LOC)

**Purpose** — Provider abstraction + factory: `SEARCH_PROVIDER` → a `SearchProvider` instance, or
`SearchUnavailableError` when the required key/URL is missing.

**Public surface**
- `@dataclass class SearchResult` with `title: str`, `url: str`, `snippet: str` — `base.py:16-20`.
- `class SearchProvider(abc.ABC)` with `name: str = "base"` (`base.py:24`) and abstract
  `async search(self, query: str, max_results: int) -> List[SearchResult]` — `base.py:26-28`.
- `class SearchUnavailableError(RuntimeError)` — `base.py:31`.
- `get_provider() -> SearchProvider` — `base.py:36`.

**Control flow** — `get_provider` (`base.py:39-58`)
1. `base.py:39` `provider = (settings.search_provider or "searxng").lower()` — no `.strip()`.
2. `base.py:40-45` `"searxng"` → lazy `from .searxng import SearxngProvider`; raise if
   `settings.searxng_url` is empty; return `SearxngProvider(settings.searxng_url)`.
3. `base.py:46-51` `"tavily"` → lazy import; raise if `settings.tavily_api_key` empty; return.
4. `base.py:52-57` `"brave"` → lazy import; raise if `settings.brave_api_key` empty; return.
5. `base.py:58` anything else → `SearchUnavailableError(f"unknown SEARCH_PROVIDER {provider!r}")`.

**State & side effects** — none (constructs an object, reads `settings`). No network at import or in
`get_provider`. No caching: a **new provider instance per call**.

**Dependencies**
- inbound: `orchestrator/app/engines/search.py:26` (called at `engines/search.py:257`);
  `orchestrator/tests/test_search_providers.py:86,88,93,95,99`;
  `tests/test_search_off.py:15`, `tests/test_search_engine.py:68,103`, `tests/test_search_breadth.py:36`.
- outbound: `abc`, `dataclasses`, `typing` (`base.py:9-11`); `app.config.settings` (`base.py:13`);
  lazy `.searxng` / `.tavily` / `.brave` (`base.py:41,47,53`).

**Config** — `settings.search_provider` (`base.py:39`), `settings.searxng_url` (`base.py:43,45`),
`settings.tavily_api_key` (`base.py:49,51`), `settings.brave_api_key` (`base.py:55,57`).

**Failure modes** — Raises `SearchUnavailableError` in four places (`base.py:44,50,56,58`); caught
by `engines/search.py:267`. Nothing swallowed. No timeout/retry concerns (no I/O).
Note the error strings name the **variable** (`"TAVILY_API_KEY is not set"`), never a value — safe.

**Concurrency** — `get_provider` is sync and called from `async def _collect_results`
(`engines/search.py:257`); it does no I/O, so the loop block is negligible (first call pays a module
import). No shared mutable state.

**Complexity hotspots** — none; `get_provider` is 23 LOC with a 4-way branch.

**Notable**
- `settings.search_provider` is **not validated at config time** (`config.py:193` has no whitelist,
  unlike `chart_trigger_mode` at `config.py:230-231`), so a typo like `SEARCH_PROVIDER=searxn`
  surfaces only at the first search request as `unknown SEARCH_PROVIDER 'searxn'`.
- No `.strip()` on the provider name (`base.py:39`) — `SEARCH_PROVIDER=" searxng"` fails.
- No provider instance caching; `engines/search.py:257` calls `get_provider()` per search batch.
- `SearchProvider.name` (`base.py:24`) is used as a cache-key prefix at `engines/search.py:259,268`.

---

### orchestrator/app/search/brave.py  (45 LOC)

**Purpose** — Brave Search API provider. Hosted, external; egress leaves the machine.

**Public surface**
- `_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"` — `brave.py:10`.
- `class BraveProvider(SearchProvider)`, `name = "brave"` — `brave.py:13-14`.
- `__init__(self, api_key: str)` — `brave.py:16-17`.
- `async search(self, query: str, max_results: int) -> List[SearchResult]` — `brave.py:19`.

**Control flow**
1. `brave.py:20-23` headers `{"Accept": "application/json", "X-Subscription-Token": self.api_key}`.
2. `brave.py:24` `params = {"q": query, "count": max_results}`.
3. `brave.py:26` `async with httpx.AsyncClient(timeout=httpx.Timeout(12.0))` — **hardcoded 12 s**.
4. `brave.py:27-28` `GET`, `raise_for_status()`, `resp.json()`.
5. `brave.py:30-31` `except (httpx.HTTPError, ValueError)` → `SearchUnavailableError(f"Brave error:
   {exc}")`.
6. `brave.py:33-44` map `data["web"]["results"][:max_results]` → `SearchResult`, skipping entries
   without `url` (`brave.py:35-36`); `title` falls back to the url, `snippet` to `""`.

**State & side effects** — Network egress to `https://api.search.brave.com` (`brave.py:10`), with
the API key in the `X-Subscription-Token` request header (`brave.py:22`). No DB, no filesystem, no
GPU, no globals, no env reads (the key arrives via constructor from `base.py:57`).

**Dependencies** — inbound: `orchestrator/app/search/base.py:53,57`. outbound: `httpx`
(`brave.py:6`), `.base` (`brave.py:8`).

**Config** — none read directly; `settings.brave_api_key` is injected at `base.py:57` and
`max_results` comes from `settings.search_max_results` via `engines/search.py:266`.

**Failure modes**
- Catches `httpx.HTTPError` (covers timeouts, connect errors and `raise_for_status`) and `ValueError`
  (bad JSON) — `brave.py:30`. A malformed-but-valid-JSON body (`data.get("web")` not a dict) would
  raise `AttributeError`, **not** caught: `(data.get("web", {}) or {}).get("results", [])`
  (`brave.py:34`) protects against `None`/missing but not against `"web": "string"`.
- **No retry, no backoff** — one attempt.
- Timeout is a **hardcoded 12.0 s literal** (`brave.py:26`), not `settings.fetch_timeout_ms`.
- A new `httpx.AsyncClient` per search call (`brave.py:26`) — correctly closed by `async with`, but
  no connection reuse across the queries of one request (`engines/search.py:260-269` loops).
- **`count=max_results` is unbounded.** `settings.search_max_results` defaults to **100**
  (`config.py:197`, and `.env.example:56` ships `SEARCH_MAX_RESULTS=100`), which the Brave Web
  Search API rejects (`count` max 20) — the provider becomes permanently unavailable with the
  shipped defaults.
- Key leakage: the key is in a header, not the URL, so `str(exc)` from httpx (which prints the
  request URL) does not disclose it. Safe.

**Concurrency** — `async`; no blocking calls; no shared mutable state (`self.api_key` is written
once at `brave.py:17`).

**Complexity hotspots** — none; `search` is 27 LOC.

**Notable** — Magic number `12.0` (`brave.py:26`). Near-identical structure to
`tavily.py`/`searxng.py` (three copies of the same try/except/map shape). No `User-Agent` set. No
`safesearch` parameter (`searxng.py:23` sets one).

---

### orchestrator/app/search/searxng.py  (46 LOC)

**Purpose** — Default provider: queries an operator-run SearXNG JSON API.

**Public surface**
- `class SearxngProvider(SearchProvider)`, `name = "searxng"` — `searxng.py:16-17`.
- `__init__(self, base_url: str)` — `searxng.py:19-20`, stores `base_url.rstrip("/")`.
- `async search(self, query: str, max_results: int) -> List[SearchResult]` — `searxng.py:22`.

**Control flow**
1. `searxng.py:23` `params = {"q": query, "format": "json", "safesearch": "1"}`.
2. `searxng.py:25` `httpx.AsyncClient(timeout=httpx.Timeout(10.0))` — **hardcoded 10 s**.
3. `searxng.py:26-28` `GET f"{self.base_url}/search"`, `raise_for_status()`, `.json()`.
4. `searxng.py:29-30` `except (httpx.HTTPError, ValueError)` → `SearchUnavailableError`.
5. `searxng.py:32-45` iterate `data.get("results", [])[: max_results * 2]`, skip entries without
   `url`, append, and `break` once `len(out) >= max_results` (`searxng.py:44-45`).

**State & side effects** — Network egress to `settings.searxng_url` (injected at `base.py:45`);
in compose that is `http://searxng:8080` (`docker-compose.yml:253`), i.e. an internal service that
itself reaches the public internet. No DB/filesystem/GPU/global state.

**Dependencies** — inbound: `orchestrator/app/search/base.py:41,45`. outbound: `httpx`
(`searxng.py:11`), `.base` (`searxng.py:13`).

**Config** — none read directly; `settings.searxng_url` injected at `base.py:45`.

**Failure modes**
- Same catch set as Brave (`searxng.py:29`); a JSON body where `results` is not a list raises
  `TypeError` on slicing — **not** caught.
- **No retry, no backoff.** Hardcoded 10.0 s timeout (`searxng.py:25`), not `settings.fetch_timeout_ms`.
- **No SSRF guard** on `SEARXNG_URL` — documented as deliberate (`searxng.py:3-6`: "SEARXNG_URL is
  trusted infrastructure … not routed through the SSRF guard"). Correct as long as the variable is
  operator-set; it is read from the environment with no scheme/host validation (`config.py:194`).
- No auth header — the SearXNG instance is assumed unauthenticated on the compose network.
- New `AsyncClient` per call (`searxng.py:25`), closed by `async with`.

**Concurrency** — `async`; no blocking calls; `self.base_url` written once (`searxng.py:20`).

**Complexity hotspots** — none; `search` is 25 LOC.

**Notable** — Magic numbers `10.0` (`searxng.py:25`) and the `max_results * 2` over-fetch
(`searxng.py:33`). `safesearch: "1"` is hardcoded, not configurable. Structurally duplicated with
`brave.py`/`tavily.py`.

---

### orchestrator/app/search/tavily.py  (46 LOC)

**Purpose** — Tavily hosted search-for-LLMs provider. Hosted, external egress.

**Public surface**
- `_ENDPOINT = "https://api.tavily.com/search"` — `tavily.py:10`.
- `class TavilyProvider(SearchProvider)`, `name = "tavily"` — `tavily.py:13-14`.
- `__init__(self, api_key: str)` — `tavily.py:16-17`.
- `async search(self, query: str, max_results: int) -> List[SearchResult]` — `tavily.py:19`.

**Control flow**
1. `tavily.py:20-25` payload `{"api_key": …, "query": …, "max_results": …, "search_depth": "basic"}`.
2. `tavily.py:27` `httpx.AsyncClient(timeout=httpx.Timeout(12.0))` — **hardcoded 12 s**.
3. `tavily.py:28-30` `POST _ENDPOINT`, `raise_for_status()`, `.json()`.
4. `tavily.py:31-32` `except (httpx.HTTPError, ValueError)` → `SearchUnavailableError(f"Tavily
   error: {exc}")`.
5. `tavily.py:34-46` map `data.get("results", [])[:max_results]` → `SearchResult`, skipping entries
   without `url`.

**State & side effects** — Network egress to `https://api.tavily.com` (`tavily.py:10`) with the API
key **in the request body** (`tavily.py:21`). No DB/filesystem/GPU/globals/env reads.

**Dependencies** — inbound: `orchestrator/app/search/base.py:47,51`. outbound: `httpx`
(`tavily.py:6`), `.base` (`tavily.py:8`).

**Config** — none read directly; `settings.tavily_api_key` injected at `base.py:51`.

**Failure modes**
- Same catch set (`tavily.py:31`). **No retry, no backoff.** Hardcoded 12.0 s timeout.
- Key is in the JSON body, so httpx's exception text (which includes the URL, not the body) does not
  leak it — safe. Tavily's newer API expects `Authorization: Bearer`; the legacy `api_key`-in-body
  form used here is what the code sends.
- New `AsyncClient` per call, closed by `async with` (`tavily.py:27`).

**Concurrency** — `async`; no blocking calls; `self.api_key` written once (`tavily.py:17`).

**Complexity hotspots** — none; `search` is 28 LOC.

**Notable** — Magic numbers `12.0` (`tavily.py:27`) and the hardcoded `"search_depth": "basic"`
(`tavily.py:24`), which is not exposed as a setting. Third copy of the same provider shape.

---

## Cross-cutting facts (verified)

1. **Authentication does not exist.** `require_user` (`auth.py:95-97`) always returns the single
   local user; `auth.py:17-20` states it. `docker-compose.yml:273` publishes `"8080:8080"` with no
   host-IP bind. `main.py:702-708` `_owns()` compares two values that are always equal.
2. **`db.connect()` (`db.py:195-205`) executes 16 DDL statements plus a full-table
   `DELETE … NOT IN (SELECT MIN(id) … GROUP BY …)` over `messages` (`db.py:181-186`) on every single
   database operation**, of which a `POST /chat` performs at least six (`main.py:327` via
   `current_user`, `main.py:339`, `main.py:471`, `main.py:493`, `main.py:527`, `main.py:162`).
3. **`llm._client` (`llm.py:72-79`) creates and abandons an `AsyncOpenAI`/`httpx.AsyncClient` per
   call.** openai `2.46.0`, `DEFAULT_MAX_RETRIES == 2`, `settings.llm_request_timeout == 300.0`
   (`config.py:264`) ⇒ up to 900 s per LLM call. No circuit breaker exists anywhere
   (`rg -rn 'circuit|breaker|backoff' orchestrator/app/` → none).
4. **`GET /health` returns 200 while `status == "degraded"`** (`main.py:243-254`), probes liveness
   rather than model readiness (`health.py:38`), has no timeout on its two thread probes
   (`health.py:122-123`), and performs a SQLite migration write per call (`health.py:79`).
5. **`uploads` / `url_documents` / `repos` / `repo_chunks` have no foreign key**
   (`db.py:84,102,114,123`), so `delete_conversation` (`db.py:334-340`) orphans them; conversation
   ids are client-supplied (`history.py:92-97`), so a recreated id inherits the old rows.
6. **`enforce_quota_and_ttl` (`core/repo.py:94-119`) treats `<workspace_dir>/uploads` as a single
   top-level entry**, while every upload lives at `<workspace_dir>/uploads/<conv>/<id>`
   (`uploads.py:35`) — one TTL expiry deletes every conversation's extracted uploads at once.
7. **`Settings.__init__` (`config.py:44-268`, 225 LOC) validates exactly one setting**
   (`chart_trigger_mode`, `config.py:230-231`); everything else is coerced with an unguarded
   `int()`/`float()` at import time (`config.py:27`, `config.py:33`).
8. **No secret is committed.** `.env`, `.env.bak-*`, `secrets/` are ignored (`.gitignore:10,47,13`);
   `git ls-files` lists only `.env.example`. Secret-bearing settings by name:
   `openai_api_key` `config.py:48`, `sf_client_secret` `config.py:119`, `sf_private_key_b64`
   `config.py:121`, `tavily_api_key` `config.py:195`, `brave_api_key` `config.py:196`.
