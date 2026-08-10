# Evidence — `orch-engines-io`

> **⚠ Superseded in part (2026-08-10).** The app-state layer described below was
> `/data/app.sqlite3` (stdlib `sqlite3`). It is now PostgreSQL — see
> [`data-model.md`](../01-codebase/data-model.md) and the CHANGELOG entry
> "App state moved from SQLite to PostgreSQL". Every `sqlite3` reference,
> `db.py` line number and finding about SQLite locking below is a snapshot of
> the pre-migration code and has NOT been re-derived. The DuckDB warehouse and
> LanceDB sections are unaffected and remain accurate.

Scope: `orchestrator/app/engines/{repo,url,document,vision,report,live_sf}.py` (911 LOC total).
Every claim below was read directly. Supporting modules (`core/net.py`, `core/repo.py`,
`core/salesforce.py`, `core/extract.py`, `core/pdf.py`, `core/report_paths.py`,
`core/exports.py`, `core/urls.py`, `core/repo_index.py`, `app/main.py`, `app/graph.py`,
`app/llm.py`, `app/db.py`, `app/config.py`) were read for cross-referencing and are cited
with `path:LINE` where used.

## SSE event inventory (all events these six engines can emit)

| event | emitted at | payload |
|---|---|---|
| `status` | `orchestrator/app/engines/repo.py:31`, `:37`, `:40`; `orchestrator/app/engines/url.py:32`, `:41`, `:44`, `:49` | `{"text": str}` |
| `token` | `orchestrator/app/engines/repo.py:131` (via `kind`), `:166`, `:173`; `orchestrator/app/engines/url.py:96`, `:104`; `orchestrator/app/engines/document.py:36`, `:72`; `orchestrator/app/engines/vision.py:86`; `orchestrator/app/engines/report.py:280` | `{"text": str}` |
| `reasoning` | same call sites as `token` — `llm.stream_chat_events` yields `("reasoning", …)` at `orchestrator/app/llm.py:261` and `("token", …)` at `orchestrator/app/llm.py:263`; the engines forward `kind` verbatim (`repo.py:131`, `url.py:104`, `document.py:72`, `vision.py:86`) | `{"text": str}` |
| `meta` | `orchestrator/app/engines/repo.py:135` + `:167` + `:176`; `orchestrator/app/engines/url.py:97` + `:108`; `orchestrator/app/engines/document.py:37` + `:75`; `orchestrator/app/engines/vision.py:95`; `orchestrator/app/engines/report.py:282` | `{"route": ...}` plus engine keys; `orchestrator/app/main.py:364-381` merges `mode`/`model`/`effort`/`generation_id`/`input_trimmed`/`context`/`auto` |
| `done` | not emitted by these engines — published by the request worker at `orchestrator/app/main.py:647` | `{"session_id": str}` |
| `error` | not emitted by these engines — published by the worker at `orchestrator/app/main.py:672` on any escaping exception | `{"message": str(exc)}` |

`route` values these engines set: `"repo"` (`repo.py:139`, `:167`, `:178`), `"url"`
(`url.py:97`, `:113`), `"vision"` (`document.py:37`, `:75`; `vision.py:95`), `"report"`
(`report.py:282`). Note `document.py` deliberately reports `route: "vision"`
(`document.py:5`, `:37`, `:75`).

---

### orchestrator/app/engines/repo.py  (183 LOC)

**Purpose** — Clone a pasted public GitHub repo into a per-conversation workspace, index its
source into line-numbered chunks, stream an onboarding overview; later turns answer code
questions from those chunks with `path:Lstart-Lend` citations.

**Public surface**
- `Emit = Callable[[str, dict], Awaitable[None]]` — `orchestrator/app/engines/repo.py:20`
- `_MAX_CONTEXT_CHARS = 60000` — `orchestrator/app/engines/repo.py:22`
- `async _clone_and_index(ref: GithubRef, conversation_id: str, emit: Emit) -> Optional[repolib.RepoOverview]` — `orchestrator/app/engines/repo.py:28-52`
- `_overview_messages(ref: GithubRef, ov: repolib.RepoOverview) -> List[dict]` — `orchestrator/app/engines/repo.py:55-74`
- `_qa_context(chunks: List[dict]) -> str` — `orchestrator/app/engines/repo.py:80-89`
- `_qa_messages(question: str, chunks: List[dict], history: Sequence[dict]) -> List[dict]` — `orchestrator/app/engines/repo.py:92-102`
- `_expand_for_code(kws: List[str]) -> List[str]` — `orchestrator/app/engines/repo.py:105-115`
- `async _code_qa(message, conversation_id, history, emit) -> str` — `orchestrator/app/engines/repo.py:118-150`
- `async run_repo_engine(message: str, ref: Optional[GithubRef], conversation_id: str, history: Sequence[dict], emit: Emit) -> str` — `orchestrator/app/engines/repo.py:153-183` (the only entry point)

**Control flow** (new repo URL)
1. `run_repo_engine` sees `ref is not None` and `db.get_repo(conversation_id, ref.key) is None` — `orchestrator/app/engines/repo.py:162`.
2. `_clone_and_index` emits `status` "Cloning …" — `orchestrator/app/engines/repo.py:31`.
3. `repolib.enforce_quota_and_ttl()` — `orchestrator/app/engines/repo.py:32` → `orchestrator/app/core/repo.py:94-119`: TTL delete + quota eviction, walking every workspace twice (`core/repo.py:114` and `:118`).
4. `repolib.workspace_path(...)` sanitises the dir name with `re.sub(r"[^A-Za-z0-9_.-]", "_", …)` — `orchestrator/app/core/repo.py:122-124`.
5. `repolib.shallow_clone(ref, dest)` — `orchestrator/app/engines/repo.py:35` → `orchestrator/app/core/repo.py:151-215`: blocking `httpx.get` size pre-check (`core/repo.py:136-144`), `subprocess.run(git clone --depth 1 --no-tags --single-branch, timeout=180)` (`core/repo.py:182-184`) with `core.hooksPath=/dev/null`, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/true` (`core/repo.py:165-177`), post-clone file-count and size caps (`core/repo.py:197-206`), `git rev-parse HEAD` (`core/repo.py:209-212`).
6. `RepoError` → `status` event with the message, return `None` — `orchestrator/app/engines/repo.py:36-38`.
7. `status` "Indexing the code…" — `orchestrator/app/engines/repo.py:40`.
8. `repolib.build_overview(dest)` — `:41` → `orchestrator/app/core/repo.py:266-302` (walks all source files, reads README up to 8000 chars).
9. `index_repo(dest)` — `:42` → `orchestrator/app/core/repo_index.py:46-54`, up to 6000 chunks of 60 lines with 10-line overlap.
10. `db.save_repo` + `db.replace_repo_chunks` — `orchestrator/app/engines/repo.py:43-51` → `orchestrator/app/db.py:794-802`, `:824-841`.
11. Overview prompt built from README/tree — `orchestrator/app/engines/repo.py:64-74`; streamed via `llm.stream_chat_events(..., max_tokens=8000)` — `:170-172`.
12. Final `meta {"route":"repo","repo":{key,files}}` — `orchestrator/app/engines/repo.py:176-179`.

**Control flow** (follow-up code Q&A)
1. `run_repo_engine` falls through to `_code_qa` — `orchestrator/app/engines/repo.py:183`.
2. Keywords from the question, stem-expanded — `:121` (`_expand_for_code`, `:105-115`).
3. `db.search_repo_chunks(conversation_id, kws, limit=settings.repo_final_chunks)` — `:122` → `orchestrator/app/db.py:844-…` (LIKE scoring, doc-file penalty).
4. Empty result → second search on literals `["def","class","import"]` — `:125`.
5. Context assembled to `_MAX_CONTEXT_CHARS` (60 000) — `:80-89`.
6. Stream at `max_tokens=10000` — `:128-133`.
7. `meta {"route":"repo","code_sources":[{path,start_line,end_line,snippet[:1500]}]}` — `:135-149`.

**State & side effects**
- Filesystem: clones into `settings.workspace_dir` (`core/repo.py:124`); deletes workspaces on TTL/quota (`core/repo.py:110`, `:119`); `shutil.rmtree(dest)` on every failure path (`core/repo.py:162`, `:186`, `:189`, `:199`, `:205`) and removes `.git/hooks` (`core/repo.py:194`).
- DB writes: `repos` and `repo_chunks` tables (`orchestrator/app/db.py:794`, `:824`).
- Network egress: `https://api.github.com/repos/<owner>/<repo>` (`core/repo.py:137`) and `https://github.com/<owner>/<repo>.git` over git-https (`core/repo.py:67`, `:180`). Neither goes through `core/net.py`'s SSRF guard — the host is hard-coded, so that is acceptable, but it is real outbound internet traffic in a system documented as air-gapped.
- Subprocess: `git clone`, `git rev-parse` (`core/repo.py:182`, `:210`).
- GPU/model: `llm.stream_chat_events` twice (`repo.py:128`, `:170`) against the main vLLM model.
- Env reads: only through `settings` (see Config).
- Global mutation: none in this module.

**Dependencies**
- Inbound: `orchestrator/app/main.py:570-574` (`from .engines.repo import run_repo_engine`), gated by `settings.repo_analysis_enabled` and `detect_github` at `orchestrator/app/main.py:463-473`. No other caller (`rg` shows only main.py and this module's own tests-free surface).
- Outbound: `. (engines/__init__)` → `DIAGRAM_INSTRUCTION`, `recent_turns` (`repo.py:12`); `..db`, `..llm` (`:13`); `..config.settings` (`:14`); `..core.repo` (`:15-16`); `..core.repo_index.chunk_file, index_repo` (`:17`); `..memory_recall.keywords` (`:18`).

**Config**
- `settings.repo_final_chunks` — `orchestrator/app/engines/repo.py:122` ← `REPO_FINAL_CHUNKS`, default 12 (`orchestrator/app/config.py:217`).
- Indirect, via `core/repo.py`: `WORKSPACE_DIR` (`config.py:214`), `WORKSPACE_TTL_HOURS` (`:215`), `WORKSPACE_QUOTA_GB` (`:216`), `REPO_MAX_MB` (`:212`), `REPO_MAX_FILES` (`:213`), `REPO_ANALYSIS_ENABLED` (`:211`).

**Failure modes**
- Only `RepoError` is handled (`repo.py:36`). Anything else raised by `enforce_quota_and_ttl`, `build_overview`, `index_repo`, `db.save_repo`, or `db.replace_repo_chunks` escapes to the worker and becomes a terminal `error` SSE event (`orchestrator/app/main.py:670-672`).
- `_github_repo_size_kb` swallows `httpx.HTTPError/ValueError/KeyError` and returns `None` (`core/repo.py:147-148`) — the pre-clone size guard silently disappears whenever GitHub is slow, rate-limited, or unreachable.
- `git rev-parse` failure is swallowed to `sha = ""` (`core/repo.py:213-214`).
- `enforce_quota_and_ttl` has no `try` around `os.path.getmtime` (`core/repo.py:108`) — a workspace deleted concurrently raises `FileNotFoundError` out of the engine.
- No retry on clone; no bound on `index_repo` wall time (only a 6000-chunk cap).
- `_qa_context` bounds prompt size to 60 000 chars, but `emit("meta", …)` returns up to `repo_final_chunks` × 1500 chars of code to the browser unbounded by any other check (`repo.py:144`).

**Concurrency**
- `run_repo_engine` / `_clone_and_index` / `_code_qa` are `async def`, but every expensive call inside them is **synchronous and blocking**: `enforce_quota_and_ttl` (`repo.py:32`), `shallow_clone` (`:35`, contains `httpx.get` at `core/repo.py:136` and `subprocess.run(timeout=180)` at `core/repo.py:182`), `build_overview` (`:41`), `index_repo` (`:42`), and all `db.*` sqlite3 calls (`:43`, `:44`, `:122`, `:125`, `:162`). None is wrapped in `asyncio.to_thread`. Contrast with `core/net.py:121`, where the author explicitly moved a blocking `getaddrinfo` off-loop for exactly this reason.
- No module-level mutable state.

**Complexity hotspots** — none over 60 LOC. Largest: `run_repo_engine` `repo.py:153-183` (31 LOC), `_code_qa` `repo.py:118-150` (33 LOC).

**Notable**
- `chunk_file` is imported at `orchestrator/app/engines/repo.py:17` and never used — dead import.
- Magic numbers: `_MAX_CONTEXT_CHARS = 60000` (`:22`), `ov.tree[:6000]` / `ov.readme[:6000]` (`:70-71`), `langs[:8]` (`:56`), `recent_turns(history, 4)` (`:101`), `limit=6` fallback (`:125`), `max_tokens=10000` (`:129`) / `8000` (`:171`), `snippet[:1500]` (`:144`), stem length `4` (`:111-112`).
- Duplication: the "stream → collect parts → emit meta" block is repeated verbatim at `:128-133` and `:170-175`, and again in `url.py:101-106`, `document.py:69-74`, `vision.py:83-88`.
- No TODO/FIXME/HACK markers in this file.

---

### orchestrator/app/engines/url.py  (123 LOC)

**Purpose** — Fetch user-pasted URLs through the SSRF-safe path, extract readable text, store
it per conversation, and answer from all stored pages with `[n]` citations.

**Public surface**
- `Emit` — `orchestrator/app/engines/url.py:20`
- `_PER_DOC_CHARS = 12000` — `:23`; `_TOTAL_DOC_CHARS = 90000` — `:24`
- `async fetch_and_store(conversation_id: str, url: str, emit: Emit) -> Optional[dict]` — `:27-54`
- `_context_block(docs: List[dict], question: str) -> str` — `:57-64`
- `_answer_messages(question: str, docs: List[dict], history: Sequence[dict]) -> List[dict]` — `:67-77`
- `async run_url_engine(message: str, urls: List[str], conversation_id: str, history: Sequence[dict], emit: Emit) -> str` — `:80-123`

**Control flow**
1. `run_url_engine` reads already-fetched URLs — `orchestrator/app/engines/url.py:88` → `orchestrator/app/db.py:780-787`.
2. For each new URL (already capped at `settings.url_max_pages` by the caller, `orchestrator/app/main.py:490`), `fetch_and_store` — `url.py:89-91`. Sequential, not concurrent.
3. `status` "Reading <hostname>…" — `:32`.
4. `net.safe_fetch(url, timeout_ms=settings.fetch_timeout_ms, max_bytes=settings.fetch_max_bytes, accept="text/html,application/pdf,text/plain")` — `:34-39` → `orchestrator/app/core/net.py:103-162`: DNS resolved off-loop and every IP checked against private/loopback/link-local/reserved/multicast/site-local (`core/net.py:45-55`, `:58-87`), scheme allow-list `{http,https}` (`core/net.py:94`), manual redirects with per-hop revalidation, max 3 hops (`core/net.py:135-148`), split timeouts (`core/net.py:124-126`), hard body cap (`core/net.py:153-155`).
5. `UnsafeURLError` → `status` "Skipped … (blocked address)" and `None` — `url.py:40-42`. `FetchError` → `status` "Couldn't reach …" — `:43-45`.
6. `extract.extract_readable(content_type, body, url)` — `:47` → `orchestrator/app/core/extract.py:64-97`: PDF via `render_pdf` (`core/extract.py:55-61`), text/plain, HTML via trafilatura with `_strip_tags` fallback (`core/extract.py:80-95`).
7. `db.save_url_document` — `url.py:53` → `orchestrator/app/db.py:755-766` (upsert on `(conversation_id, url)`).
8. All stored docs are loaded — `url.py:93` → `orchestrator/app/db.py:769-777`.
9. `_context_block` gives each doc `share = min(12000, max(1000, 90000 // len(docs)))` and `select_relevant(...)` — `url.py:60-63` → `orchestrator/app/core/urls.py:52-85`.
10. Stream at `max_tokens=12000` — `url.py:101-106`.
11. `meta {"route":"url","sources":[{n,title,url,domain}]}` — `:108-122`.

**State & side effects**
- Network egress: arbitrary user-supplied `http(s)` URLs via `core/net.py` (the only SSRF choke point; correctly implemented for this path).
- DB writes: `url_documents` upsert (`orchestrator/app/db.py:759-766`).
- GPU/model: one `llm.stream_chat_events` call (`url.py:101`).
- No filesystem writes, no subprocess, no global mutation.

**Dependencies**
- Inbound: `orchestrator/app/main.py:577-581` only. `fetch_and_store` has no other caller (verified with `rg -n "fetch_and_store"`).
- Outbound: `engines/__init__` (`DIAGRAM_INSTRUCTION`, `recent_turns`) — `url.py:14`; `..db`, `..llm` — `:15`; `..config.settings` — `:16`; `..core.extract`, `..core.net` — `:17`; `..core.urls.select_relevant` — `:18`.

**Config**
- `settings.fetch_timeout_ms` — `orchestrator/app/engines/url.py:36` ← `FETCH_TIMEOUT_MS`, default 8000 (`orchestrator/app/config.py:198`).
- `settings.fetch_max_bytes` — `:37` ← `FETCH_MAX_BYTES`, default 5 000 000 (`orchestrator/app/config.py:199`).
- Caller-side: `URL_ANALYSIS_ENABLED` (`config.py:207`), `URL_MAX_PAGES` (`config.py:208`, applied at `main.py:490`).

**Failure modes**
- Only `net.UnsafeURLError`, `net.FetchError` and `extract.UnsupportedContentError` are caught (`url.py:40`, `:43`, `:48`). A malformed PDF reaching `_extract_pdf_text` raises `pypdfium2.PdfiumError` out of `extract_readable` (`core/extract.py:60`) and terminates the whole turn with an `error` event.
- `db.save_url_document` failures are unhandled (`:53`).
- No per-request total time budget: N URLs × `fetch_timeout_ms` sequentially, plus DNS.
- The `_TOTAL_DOC_CHARS` budget is not actually enforced — `share` is floored at 1000 (`:61`) and `budget` is never decremented, so with >90 stored docs the block exceeds 90 000 chars. Downstream `context.fit_request` (`orchestrator/app/llm.py:229`) is the only real backstop.
- `docs` is every page ever stored for the conversation, unbounded and never expired (`orchestrator/app/db.py:769-777`).

**Concurrency**
- `async def` throughout; `net.safe_fetch` is properly async. But `extract.extract_readable` (trafilatura / pypdfium2 rendering, `url.py:47`), `select_relevant` chunk-and-score over up to 5 MB of text (`url.py:62`), and all `db.*` sqlite3 calls (`:53`, `:88`, `:93`) run inline on the event loop.
- URLs are fetched strictly sequentially (`url.py:89-91`) — no `asyncio.gather`.
- No module-level mutable state.

**Complexity hotspots** — none over 60 LOC. Largest: `run_url_engine` `url.py:80-123` (44 LOC).

**Notable**
- **Untrusted page text is placed in the prompt with no delimiting or instruction to distrust it** — `url.py:63` builds `f"[{i}] {title} ({url})\n{body}"` and `:75` concatenates it into a `user` message. The system prompt (`:70-74`) says only "Use only their content"; nothing tells the model to ignore instructions inside the page.
- Worse, the caller **re-injects the same stored text as a `system` message on every subsequent turn** — `orchestrator/app/main.py:502-510` — and `recent_turns` keeps every system message forever regardless of age by design (`orchestrator/app/engines/__init__.py:16-19`).
- Magic numbers: `12000`, `90000` (`:23-24`), `max(1000, …)` (`:61`), `recent_turns(history, 4)` (`:76`), `max_tokens=12000` (`:102`), `6000` chars in the main.py re-injection (`orchestrator/app/main.py:499`).
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/document.py  (76 LOC)

**Purpose** — Render an uploaded base64 PDF to page images plus extracted text and send both
to the multimodal main model; reports itself as `route: "vision"`.

**Public surface**
- `Emit` — `orchestrator/app/engines/document.py:15`
- `_SYSTEM` (str constant) — `:17-23`
- `async run_pdf_engine(message: str, pdf_base64: str, filename: Optional[str], history: Sequence[dict], emit: Emit) -> str` — `:26-76`

**Control flow**
1. `render_pdf(pdf_base64)` — `orchestrator/app/engines/document.py:33` → `orchestrator/app/core/pdf.py:27-67`: `base64.b64decode` (`core/pdf.py:37`), `pdfium.PdfDocument` (`:38`), loop over `min(total, 6)` pages (`:44`) extracting the text layer (`:46-48`) and rendering at `RENDER_SCALE = 2.0` to PNG data URLs (`:50-57`), text capped at `MAX_TEXT_CHARS = 24000` (`:64`).
2. Empty render → `token` note + `meta {"route":"vision"}` and return — `document.py:34-38`.
3. Prompt assembly: header `f'Document: {filename}\n\n'` (`:42`), instruction (`:41`), extracted text part (`:45-48`), one `image_url` part per rendered page (`:49-50`), truncation note (`:51-57`).
4. Messages: `_SYSTEM + DIAGRAM_INSTRUCTION` + `recent_turns(history, 4)` + the multimodal user turn — `:59-63`.
5. Stream at `model_choice="smart", effort="medium", max_tokens=12000` — `:69-71`.
6. `meta {"route":"vision"}` — `:75`.

**State & side effects**
- No filesystem writes, no DB writes, no network egress except the vLLM call.
- GPU/model: one `llm.stream_chat_events` (`:69`) with up to 6 PNG images at ~144 DPI in the payload.
- Memory: the whole decoded PDF plus up to 6 rendered PIL bitmaps and their base64 encodings are held simultaneously (`core/pdf.py:37-57`).
- No env reads, no global mutation.

**Dependencies**
- Inbound: `orchestrator/app/main.py:553-557` only (`request.pdf_data` branch). Not wired into `graph.py`.
- Outbound: `engines/__init__` (`DIAGRAM_INSTRUCTION`, `recent_turns`) — `:11`; `..llm` — `:12`; `..core.pdf.render_pdf` — `:13`.

**Config** — none read in this file. `MAX_PDF_PAGES=6`, `RENDER_SCALE=2.0`, `MAX_TEXT_CHARS=24000` are hard-coded constants in `orchestrator/app/core/pdf.py:18-20`, not configurable.

**Failure modes**
- No `try/except` anywhere. `binascii.Error` from bad base64 (`core/pdf.py:37`) and `pypdfium2.PdfiumError` from a corrupt or password-protected PDF (`core/pdf.py:38`) propagate to the worker and surface as a raw `error` event (`orchestrator/app/main.py:670-672`).
- No upper bound on the uploaded base64 length: `ChatRequest.pdf: Optional[str]` has no `max_length` (`orchestrator/app/main.py:196`), and the model validator only checks non-emptiness (`orchestrator/app/main.py:233-239`).
- No timeout on rendering; `pdf.close()` is in a `finally` (`core/pdf.py:66-67`) so the handle is released.

**Concurrency**
- `run_pdf_engine` is `async def` but `render_pdf` at `:33` is 100 % synchronous CPU/allocation work executed on the event loop.
- No shared mutable state.

**Complexity hotspots** — none. `run_pdf_engine` is 51 LOC (`document.py:26-76`).

**Notable**
- The user-controlled `filename` is interpolated straight into the prompt (`:42`) with no sanitisation — a filename such as `report.pdf\n\nSYSTEM: ignore prior instructions` becomes prompt text.
- Emits `route: "vision"` rather than a distinct `document` route (`:37`, `:75`) — deliberate per `:5`, but it means the UI cannot distinguish PDF turns from image turns, and `meta_extras` then labels the model as `settings.vision_model` (`orchestrator/app/main.py:307-308`) even though the call was actually made with `model_choice="smart"` (`document.py:70`), i.e. the main model resolved at `orchestrator/app/llm.py:166`. **`meta.model` is wrong for every PDF turn.**
- Magic numbers: `recent_turns(history, 4)` (`:61`), `max_tokens=12000` (`:70`).
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/vision.py  (96 LOC)

**Purpose** — Send an attached image as OpenAI multimodal content to the main (thinking)
model, with an invoice/contract structured-extraction system prompt.

**Public surface**
- `Emit` — `orchestrator/app/engines/vision.py:19`
- `_JSON_BLOCK_RE` — `:21`; `_DATA_URL_RE = ^data:image/[\w.+-]+;base64,` — `:22`; `_SYSTEM` — `:24-35`
- `to_data_url(image_base64: str) -> str` — `:38-44`
- `build_user_content(message: str, image_base64: str) -> List[dict]` — `:47-52`
- `extract_json_block(text: str) -> Optional[dict]` — `:55-63`
- `async run_vision_engine(message: str, image_base64: Optional[str], history: Sequence[dict], emit: Emit) -> str` — `:66-96`

**Control flow**
1. `image_base64` falsy → `raise ValueError` — `orchestrator/app/engines/vision.py:72-73`.
2. Messages: `[{"role":"system","content":_SYSTEM}, {"role":"user","content": build_user_content(...)}]` — `:75-78`.
3. `to_data_url` passes a `data:image/…;base64,` string through unchanged, otherwise prefixes `data:image/png;base64,` — `:41-44`.
4. Stream at `model_choice="smart", effort="medium", max_tokens=8000` — `:83-85`.
5. `meta {"route":"vision"}` — `:95`.

**State & side effects**
- GPU/model: one `llm.stream_chat_events` (`:83`).
- No DB writes, no filesystem writes, no non-model network egress, no env reads, no global mutation.

**Dependencies**
- Inbound: `orchestrator/app/main.py:562-566` (image attachment branch) and `orchestrator/app/graph.py:58-64` (`_vision_node`). Test import at `orchestrator/tests/test_llm_clients.py:20` (`build_user_content`, `to_data_url`) and `orchestrator/tests/test_imports.py:19`.
- Outbound: `..llm` only (`:17`) plus stdlib `json`, `re` (`:13-14`).

**Config** — none read in this file. `settings.vision_model` is referenced only in `orchestrator/app/main.py:308` when labelling the meta.

**Failure modes**
- `ValueError` on missing image (`:73`) escapes to the worker → `error` event.
- `extract_json_block` is **dead code** — defined at `:55-63`, never called anywhere (`rg -n "extract_json_block"` finds only this definition); the docstring at `:92-95` explains the design change that orphaned it.
- No validation that the payload is actually an image, and no size bound: `ChatRequest.image_base64`/`image` have no `max_length` (`orchestrator/app/main.py:214-216`).
- No timeout of its own; relies on the OpenAI client defaults inside `llm._client`.

**Concurrency** — `async def`; the only blocking work is the (small) regex match. No shared state.

**Complexity hotspots** — none. `run_vision_engine` is 31 LOC (`vision.py:66-96`).

**Notable**
- **`history` is accepted at `vision.py:69` and never used.** The messages list at `:75-78` contains only the system prompt and the current image turn; `recent_turns` is not imported (`:17` imports only `llm`). Every other engine passes `recent_turns(history, 4)` (`repo.py:101`, `url.py:76`, `document.py:61`). A follow-up question about an already-analysed image therefore reaches the model with zero conversational context.
- `DIAGRAM_INSTRUCTION` is also omitted here, unlike `repo.py:73`, `url.py:76`, `document.py:60`.
- Magic numbers: `max_tokens=8000` (`:84`).
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/report.py  (283 LOC)

**Purpose** — Plan report sections with the main model, fill them via the sql/rag engines,
render charts to PNG, assemble Markdown, and convert to both `.docx` and `.pdf` with pandoc
into `REPORTS_DIR`.

**Public surface**
- `Emit` — `orchestrator/app/engines/report.py:30`; `log` — `:32`
- `_THINK_RE` — `:34`; `_FENCE_RE` — `:35`; `MAX_SECTIONS = 6` — `:37`; `_PLAN_SYSTEM` — `:39-47`
- `_parse_plan(raw: str, fallback_title: str) -> dict` — `:50-86`
- `_markdown_table(columns: Sequence[str], rows: Sequence[Sequence], max_rows: int = 20) -> str` — `:89-99`
- `async _run_pandoc(md_path: Path, out_path: Path, resource_dir: Path) -> None` — `:102-122`
- `async _sql_section(sec: dict, index: int, tmp_dir: Path) -> List[str]` — `:125-151`
- `async _section_chart(sec, index, tmp_dir, columns, rows) -> List[str]` — `:154-196`
- `async _rag_section(sec: dict) -> List[str]` — `:199-211`
- `async run_report_engine(message: str, history: Sequence[dict], emit: Emit) -> str` — `:214-283`

**Control flow**
1. Planning call: `llm.chat_completion(_PLAN_SYSTEM + user message, temperature=0.2, max_tokens=5000)` — `orchestrator/app/engines/report.py:218-225`.
2. `_parse_plan` strips `<think>` blocks and code fences, brace-slices, JSON-parses, normalises `kind` to `sql`/`rag`, defaults to a single "Overview" rag section, caps at `MAX_SECTIONS` — `:50-86`.
3. `base_name = f"{slugify(plan['title'], fallback='report')}-{stamp}"` with `stamp = time.strftime("%Y%m%d-%H%M%S")` — `:228-229`. `slugify` reduces to `[a-z0-9-]{,40}` (`orchestrator/app/core/exports.py:18-24`), so the filename cannot traverse.
4. `reports_dir.mkdir(parents=True, exist_ok=True)` — `:231`.
5. `tempfile.TemporaryDirectory(prefix="report-")` — `:234`.
6. Per section: heading appended, then `_sql_section` or `_rag_section`, wrapped in `except Exception` that records the failure into `section_errors` and inlines `f"> Section could not be generated: {exc}"` into the report — `:242-251`.
7. `_sql_section` → `generate_and_run_sql(sec["instruction"], fetch_cap=settings.sql_preview_row_cap + 1)` (`:127-129` → `orchestrator/app/engines/sql.py:179-207`), a 2-4 sentence prose call (`:133-146`), `_markdown_table(columns, rows)` (`:147`, 20 rows), then `_section_chart` when `sec["chart"]` (`:149-150`).
8. `_section_chart` → `build_chart(..., mode=settings.chart_trigger_mode, ask_model=_ask_chart_model, force=True)` (`:167-178` → `orchestrator/app/core/chart_pipeline.py:94-117`), `PNG_SUPPORTED` policy check (`:181-188`), `render_chart_png(spec, columns, rows, png_path)` (`:190` → `orchestrator/app/core/charts_png.py:69-…`), zero-size check (`:191-192`), returns a Markdown image reference by bare filename (`:193`). Whole body wrapped in `except Exception: log.warning(...); return []` (`:194-196`).
9. `_rag_section` → `select_context` (`:200` → `orchestrator/app/engines/rag.py:91`), prose via `rag_answer_messages` (`:201-203`), `build_citations(hits, base_url=settings.sf_lightning_base_url)` (`:205`).
10. `md_path.write_text(...)` inside the temp dir — `:253-254`.
11. `outputs = [reports_dir/<base>.docx, reports_dir/<base>.pdf]`, then `await _run_pandoc(...)` for each — `:256-258`.
12. `_run_pandoc` builds `["pandoc", md, "--standalone", "--resource-path", tmp, "-o", out]`, adding `--pdf-engine=weasyprint` for `.pdf` (`:103-113`), `asyncio.create_subprocess_exec` + `communicate()` (`:114-117`), `RuntimeError` with the first 500 chars of stderr on non-zero exit (`:118-122`).
13. `report_files = [{filename, type, size}]` for files that exist — `:262-270`.
14. `token` summary (`:280`) then `meta {"route":"report","report_files":[...]}` (`:282`).

**State & side effects**
- Filesystem writes: `settings.reports_dir` (`:230-231`), `.docx` and `.pdf` written there by pandoc (`:256-258`); temp dir for `.md` and `chart-<n>.png` (`:234`, `:189`, `:253`).
- Subprocess: `pandoc` (and, for PDF, weasyprint in-process to pandoc) — `:114-116`.
- DB/warehouse: DuckDB reads via `generate_and_run_sql` (`:127`), vector-store reads via `select_context` (`:200`).
- GPU/model: planning (`:218`), one prose call per sql section (`:133`), one per rag section (`:201`), plus the chart decision call inside `build_chart` (`_ask_chart_model` → `orchestrator/app/engines/sql.py:210-211`).
- Global mutation: none. `log` is module-level (`:32`).

**Dependencies**
- Inbound: `orchestrator/app/graph.py:67-71` (`_report_node`), reached only through the router class `report` (`graph.py:103`). Also imported by `orchestrator/tests/test_imports.py:16`.
- Outbound: `..llm` (`:20`), `..config.settings` (`:21`), `..core.chart_pipeline.build_chart` (`:22`), `..core.charts_png.PNG_SUPPORTED, render_chart_png` (`:23`), `..core.citations.build_citations` (`:24`), `..core.exports.slugify` (`:25`), `.rag._answer_messages` + `.rag.select_context` (`:26-27`), `.sql._ask_chart_model` + `.sql.generate_and_run_sql` (`:28`). Note two **private** cross-module imports (`rag._answer_messages`, `sql._ask_chart_model`).

**Config**
- `settings.sql_preview_row_cap` — `orchestrator/app/engines/report.py:128` ← `SQL_PREVIEW_ROW_CAP`, default 500 (`orchestrator/app/config.py:234`).
- `settings.chart_trigger_mode` — `:172` ← `CHART_TRIGGER_MODE`, default `"explicit"` (`orchestrator/app/config.py:231`).
- `settings.sf_lightning_base_url` — `:205` ← `SF_LIGHTNING_BASE_URL` (`orchestrator/app/config.py:103`).
- `settings.reports_dir` — `:230` ← `REPORTS_DIR`, default `/reports` (`orchestrator/app/config.py:100`); mounted as the `reports` volume at `docker-compose.yml:270`.

**Failure modes**
- `_run_pandoc` has **no timeout** — `await proc.communicate()` at `:117` waits forever. weasyprint fetching a remote resource embedded in the Markdown will hang the generation indefinitely.
- The `_run_pandoc` loop at `:257-258` is **not** guarded: if `.docx` succeeds and `.pdf` fails (missing weasyprint, missing pandoc), `RuntimeError` escapes `run_report_engine`, the whole turn ends in an `error` event, and `report_files` is never emitted even though the `.docx` exists on disk.
- `FileNotFoundError` if `pandoc` is absent from the image also escapes at `:114`.
- Swallowed: `_parse_plan` catches `json.JSONDecodeError/ValueError` and falls back (`:62-63`); `_section_chart` catches bare `Exception` (`:194`); `build_chart` itself catches bare `Exception` (`orchestrator/app/core/chart_pipeline.py:115-117`); the per-section handler catches bare `Exception` and pastes `str(exc)` into the report body (`:249-251`).
- No overall time budget for a 6-section report (6 × (SQL generation + retry + execution + prose call) plus planning plus 2 pandoc runs).
- Filename collision: two reports with the same title generated within the same second produce the same `base_name` (second resolution, `:228`) and silently overwrite.

**Concurrency**
- `async def` throughout; sections are executed **strictly sequentially** (`:242-251`).
- Blocking calls on the event loop: `md_path.write_text` (`:254`), `png_path.exists()/stat()` (`:191`), `p.stat()` (`:268`), `reports_dir.mkdir` (`:231`), `tempfile.TemporaryDirectory` teardown (`:234`), and — the significant one — `render_chart_png` (`:190`), a synchronous matplotlib render.
- No shared mutable module state; `section_errors`, `md_lines`, `outputs` are per-call locals.

**Complexity hotspots**
- `run_report_engine` — `orchestrator/app/engines/report.py:214-283`, **70 LOC**. Largest function in this assignment. It does planning, filename derivation, temp-dir management, the section loop, error accumulation, two subprocess conversions, result stat-ing, summary text and emission.
- `_section_chart` — `:154-196`, 43 LOC (18 of them docstring/comment).
- `_parse_plan` — `:50-86`, 37 LOC.

**Notable**
- `_markdown_table` interpolates raw values into pipe-delimited rows (`:92-97`) with no escaping — a Salesforce value containing `|` or a newline breaks the table for every downstream row.
- `plan['title']` is model-generated and is written into the Markdown H1 unescaped (`:237`) and into the filename via `slugify` (`:229`).
- `--standalone` pandoc Markdown permits raw HTML/inline CSS; combined with `--pdf-engine=weasyprint` (`:113`), any HTML that reaches `md_lines` from model output or warehouse data is rendered by an engine that resolves `file:` and `http:` resources.
- Magic numbers: `MAX_SECTIONS = 6` (`:37`), `rows[:30]` in the prose sample (`:131`), `max_rows = 20` (`:89`), `rows[:50]` for charting (`:170`), `instruction[:60]` (`:76`), `message[:80]` (`:226`), `stderr[:500]` (`:121`), `max_tokens` 5000/3000/5000 (`:224`, `:145`, `:202`).
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/live_sf.py  (150 LOC)

**Purpose** — Turn a natural-language question into one SOQL query for the live production
Salesforce org, and answer org-shape questions from the describe API instead.

**Public surface**
- `_FENCE_RE` — `orchestrator/app/engines/live_sf.py:24`; `_THINK_RE` — `:25`; `_SOQL_SYSTEM` — `:27-40`
- `extract_soql(raw: str) -> str` — `:43-50`
- `_object_hint() -> str` — `:53-58`
- `async write_soql(question: str, history: Sequence[dict] = ()) -> str` — `:61-86`
- `_SCHEMA_RE` — `:91-93`; `_COUNT_OR_LIST_RE` — `:94`; `_OBJECT_NAME_RE` — `:96`
- `is_schema_question(text: str) -> bool` — `:99-108`
- `async fetch_schema(question: str) -> Tuple[str, str]` — `:111-137`
- `async fetch_live(question: str, history: Sequence[dict] = ()) -> Tuple[str, List[Dict[str, Any]]]` — `:140-145`
- `describe_rows(rows: List[Dict[str, Any]], limit: int = 30) -> str` — `:148-150`

**Control flow** (`fetch_live`)
1. `write_soql(question, history)` — `orchestrator/app/engines/live_sf.py:145`.
2. `_object_hint()` lists synced object names from `schema_cache()` — `:53-58`, wrapped in a bare `except Exception: return ""` (`:57-58`).
3. `sf_dictionary.hint_for(question)` prepends real API names — `:62`, `:68-70`.
4. `llm.chat_completion([_SOQL_SYSTEM, f"{context}Question: {question}"], temperature=0.0, max_tokens=6000)` — `:71-82`. **The user's raw question is the user turn.**
5. `extract_soql` strips `<think>` and fences, then regex-slices from the first `SELECT` to end and collapses whitespace — `:43-50`.
6. Empty → `raise salesforce.UnsafeSoql(...)` — `:85`.
7. `salesforce.run_soql(soql)` — `:145` → `orchestrator/app/core/salesforce.py:144-174`: `guard_soql` (`core/salesforce.py:55-90`), `_authenticate` (`:111-141`), `GET {instance}/services/data/{version}/query?q=…` with `timeout=settings.sf_live_timeout` (`:148-153`), one 401 re-auth retry (`:154-162`), `records[:MAX_ROWS]` with `MAX_ROWS = 200` (`core/salesforce.py:29`, `:174`).

**Control flow** (`fetch_schema`)
1. `_OBJECT_NAME_RE.findall(question)` — `:115`; first 3 distinct names — `:119`.
2. `sf.describe_object(name)` per name, `except Exception: continue` — `:120-123`; object name validated against `^[A-Za-z][A-Za-z0-9_]*$` inside `core/salesforce.py:213-214`.
3. Otherwise `sf.list_objects()` — `:129` → `core/salesforce.py:191-208`, returns every queryable non-deprecated object.
4. Returns `("describe"|"sobjects", text)` — `:127`, `:133-137`.

**State & side effects**
- Network egress: `POST {SF_LOGIN_URL}/services/oauth2/token` (`core/salesforce.py:124-131`) and `GET {instance_url}/services/data/{SF_API_VERSION}/...` (`core/salesforce.py:149-153`, `:180-185`, `:199`, `:215-217`) — **the production Salesforce org**.
- Global mutation: `core/salesforce.py:108` `_token = _Token()` module-level singleton; `_authenticate` writes `_token.value`, `_token.instance`, `_token.at` (`core/salesforce.py:138-140`) and `run_soql` clears `_token.value` on 401 (`core/salesforce.py:155`).
- GPU/model: one `llm.chat_completion` per `write_soql` (`:71`).
- No filesystem or DB writes in this module.

**Dependencies**
- Inbound: `orchestrator/app/engines/sql.py:318-319` (`describe_rows`, `fetch_live`, `fetch_schema`, `is_schema_question`); `orchestrator/app/engines/agent.py:291` and `:316` (`describe_rows`, `fetch_live`) — note `agent.py:288-310` and `agent.py:312-329` are two identical `if step.kind == "salesforce" and salesforce:` blocks, so the second is unreachable dead code. Test: `orchestrator/tests/test_live_salesforce.py:287`, `:297`.
- Outbound: `..llm` (`:20`), `..core.salesforce` (`:21`), `..core.schema_cache.format_schema, schema_cache` (`:22`), lazy `..core.sf_dictionary.hint_for` (`:62`), lazy `..core.salesforce as sf` (`:113`).

**Config** — none read directly in this file. Via `core/salesforce.py`: `SF_CLIENT_ID` (`config.py:118`), `SF_CLIENT_SECRET` (`:119`), `SF_LOGIN_URL` (`:120`), `SF_PRIVATE_KEY_B64` (`:121`), `SF_API_VERSION` default `v61.0` (`:122`), `SF_LIVE_TIMEOUT` default 45 s (`:123`). Gated by `SF_LIVE_ENABLED` default true (`config.py:124-125`, checked at `orchestrator/app/engines/sql.py:306`).

**Failure modes**
- `_object_hint` bare `except Exception` → `""` (`:57-58`): a broken schema cache silently degrades SOQL quality with no log line.
- `fetch_schema` bare `except Exception: continue` (`:121-122`): every describe failing yields an empty `blocks` list and silently falls through to the full `list_objects()` listing.
- `write_soql` raises `UnsafeSoql` when the model returns no query (`:85`). `run_soql` raises `SalesforceUnavailable` on auth failure or a non-200 (`core/salesforce.py:134`, `:171`).
- `_authenticate` does **not** wrap the token POST in `try` (`core/salesforce.py:123-131`) — a `httpx.ConnectError`/`ReadTimeout` escapes as an httpx exception, not `SalesforceUnavailable`. `agent.py:295` catches only `(SalesforceUnavailable, UnsafeSoql)`, so a network blip on the token endpoint aborts the agent step chain.
- `resp.json()` at `core/salesforce.py:137`, `:173`, `:188` is unguarded — a non-JSON gateway page raises `json.JSONDecodeError`.
- No retry other than the single 401 re-auth (`core/salesforce.py:154-162`); no rate limit, no circuit breaker, no per-user quota on live org calls.
- `guard_soql`'s LIMIT enforcement is anchored to the very end of the string (`core/salesforce.py:84`), so a query ending in `OFFSET n` or `FOR VIEW` gets a second `LIMIT 200` appended and is rejected by Salesforce as MALFORMED_QUERY.
- `_FORBIDDEN` (`core/salesforce.py:33-36`) matches anywhere including inside string literals: `SELECT Id FROM Account WHERE Name = 'Delete Inc'` is refused with "forbidden keyword: DELETE".

**Concurrency**
- All I/O paths are `async`. `schema_cache()` (`:56`) and `hint_for` (`:62`) are synchronous.
- `core/salesforce.py:108` `_token` is shared mutable module state with **no lock**: two concurrent requests that both see a stale token both execute the token POST (`core/salesforce.py:114-141`) and both write `_token.value`. Last write wins; benign but wasteful, and it doubles the org's OAuth session count under load.
- A fresh `httpx.AsyncClient` is constructed per call (`core/salesforce.py:123`, `:148`, `:157`, `:180`) — no connection pooling across requests.

**Complexity hotspots** — none over 60 LOC. Largest: `fetch_schema` `live_sf.py:111-137` (27 LOC), `write_soql` `live_sf.py:61-86` (26 LOC).

**Notable**
- `history` is a declared parameter of `write_soql` (`:61`) and `fetch_live` (`:141`) and is **never used** — the SOQL prompt at `:72-75` contains only the system message and the current question. Callers pass real history (`sql.py:351`, `agent.py:294`) believing it matters.
- `extract_soql`'s `re.search(r"SELECT\s.+", text, re.S|re.I)` (`:49`) grabs everything from the first `SELECT` to end-of-string, including any trailing model prose, which then hits `guard_soql`.
- `describe_rows` caps at 30 rows (`:148`) while `MAX_ROWS` is 200 (`core/salesforce.py:29`) — 170 fetched rows are discarded before the prompt, and `sql.py:376` separately previews `sql_preview_row_cap` (500) of them.
- Magic numbers: `max_tokens=6000` (`:81`), `[:3]` objects (`:119`), `limit=30` (`:148`), `MAX_ROWS=200` and `TTL=25*60` (`core/salesforce.py:29`, `:97`).
- No TODO/FIXME/HACK markers.

---

## Cross-cutting facts verified

- **`POST /chat` is unauthenticated.** `orchestrator/app/main.py:55-56` states "/chat and /reports* remain auth-free"; the route at `orchestrator/app/main.py:274-275` has no auth dependency. `current_user` (`:327`) is used only for personalisation and the conversation-ownership check at `:338-344`, which is skipped entirely when `conv_owner is None` (a conversation id nobody owns). The container publishes `8080:8080` (`docker-compose.yml:272-273`).
- **`GET /reports` and `GET /reports/{filename}` are unauthenticated.** `orchestrator/app/main.py:257-259` lists every file in `REPORTS_DIR`; `:262-271` serves it. Path safety is solid (`orchestrator/app/core/report_paths.py:23-48` blocks `..`, separators, absolutes, dotfiles, NUL, and symlink escape) — but there is no owner concept at all.
- `settings.cors_allow_origins` (`orchestrator/app/main.py:49`) restricts browsers only, not direct HTTP clients.
- The SSRF choke point (`orchestrator/app/core/net.py`) is used by `url.py` only. `core/repo.py:137` (`httpx.get` to api.github.com) bypasses it, which is safe because the host is a literal.
- `orchestrator/app/main.py:502-510` re-injects previously fetched page text as a **`system`** message; `orchestrator/app/engines/__init__.py:16-19` keeps every system message forever.
- No TODO/FIXME/HACK markers exist in any of the six assigned files (`rg -n "TODO|FIXME|HACK|XXX"` returned nothing).
