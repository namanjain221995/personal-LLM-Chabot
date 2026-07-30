# Evidence — frontend-api

Scope: the Next.js App Router route handlers under `frontend/app/api/**`, the two app-shell
files, and the client/server library modules that define the wire contract
(`lib/sse.ts`, `lib/streams.ts`, `lib/orchestrator.ts`, `lib/proxy.ts`, `lib/history.ts`,
`lib/historyApi.ts`, `lib/contextMeter.ts`, `lib/auth.ts`, `lib/types.ts`, `lib/errors.ts`).

All 22 assigned files read in full. Total assigned LOC: **3164**.

Cross-references into the orchestrator (`orchestrator/app/*.py`) were read only far enough to
verify the contract; those files are another agent's assignment and are cited, not documented.

---

## 0. Global facts established up front

### 0.1 There is no authentication anywhere in the system

`orchestrator/app/auth.py:89-92`:

```python
def current_user(request: Request) -> Optional[sqlite3.Row]:
    """The local user. Signature kept so callers in main.py are unchanged."""
    del request  # no cookie is read — there is no session any more
    return local_user()
```

`orchestrator/app/auth.py:95-97` — `require_user` (the FastAPI dependency every `/history/*`
route uses) "Never 401s now." The module docstring states this explicitly at
`orchestrator/app/auth.py:17-20`: *"there is now no authentication whatsoever. Anyone who can
reach the port can read every conversation and query the Salesforce data."*

Consequence for this assignment: **every cookie-forwarding branch in the Next.js API layer is
dead code.** It is present in:

| file:LINE | forwarded header |
|---|---|
| `frontend/app/api/chat/route.ts:160-162` | `cookie` |
| `frontend/app/api/chat/active/route.ts:21-23` | `cookie` |
| `frontend/app/api/chat/attach/[id]/route.ts:49-52` | `cookie` |
| `frontend/app/api/chat/compact/route.ts:21-23` | `cookie` |
| `frontend/app/api/chat/stop/route.ts:22-24` | `cookie` |
| `frontend/app/api/upload/route.ts:27-29` | `cookie` |
| `frontend/lib/proxy.ts:26-27` | `cookie` (up) |
| `frontend/lib/proxy.ts:56-58` | `set-cookie` (down) |

The comments on those lines ("Owner-scoped: only the user who started a generation may stop
it", `frontend/app/api/chat/stop/route.ts:21`) describe behaviour that no longer exists.

`frontend/lib/auth.ts:1-9` is honest about it: *"There is no sign-in, no sign-up, no session
cookie and no route gating."*

### 0.2 `ORCHESTRATOR_URL` is server-side only — verified

`rg -n "ORCHESTRATOR_URL|NEXT_PUBLIC" frontend/` returns `ORCHESTRATOR_URL` only in:
`frontend/app/api/auth/me/route.ts:15`, `frontend/app/api/upload/route.ts:18`,
`frontend/lib/proxy.ts:10`, `frontend/app/api/chat/stop/route.ts:15`,
`frontend/app/api/chat/compact/route.ts:14`, `frontend/app/api/chat/attach/[id]/route.ts:40`,
`frontend/app/api/chat/route.ts:139`, `frontend/app/api/chat/active/route.ts:15`, plus
`frontend/README.md:29`. Every one of those is a module with `export const runtime = 'nodejs'`
or a server-only helper. **No `NEXT_PUBLIC_ORCHESTRATOR_URL` exists**; the only
`NEXT_PUBLIC_*` var is `NEXT_PUBLIC_APP_NAME` (`frontend/app/layout.tsx:15`,
`frontend/components/ChatApp.tsx:68`). The orchestrator URL is therefore **not exposed to the
browser bundle**. Correct.

### 0.3 Both ports are published on 0.0.0.0

`docker-compose.yml:272-273` — orchestrator `ports: - "8080:8080"`.
`docker-compose.yml:347-349` — frontend `ports: - "3000:3000"`, `ORCHESTRATOR_URL:
http://orchestrator:8080`.
Neither is bound to `127.0.0.1`. Combined with §0.1 this is the top-severity finding.

### 0.4 There is no `middleware.ts` and no security headers

`frontend/next.config.mjs` (read in full, 8 lines) sets only `output: 'standalone'`,
`reactStrictMode: true`, `poweredByHeader: false`. No `headers()`, no CSP, no CORS policy, no
rate limiting. `ls frontend/middleware.*` → none.

### 0.5 Complete SSE event inventory — orchestrator vs frontend

Orchestrator's authoritative allowlist, `orchestrator/app/sse.py:34-44`:

```
ALLOWED_EVENTS   = ("token", "meta", "done", "error")
V2_EVENTS        = ("reasoning", "step")
PROGRESS_EVENTS  = ("status",)
RESEARCH_EVENTS  = ("research",)
```

`sse_event()` raises `ValueError` on any other name (`orchestrator/app/sse.py:51-52`), so the
wire set is exactly those seven.

| event | emitted at | exact payload | parsed at | frontend result |
|---|---|---|---|---|
| `token` | `orchestrator/app/sse.py:58`, `engines/sql.py:436`, `engines/chat.py:101`, `engines/agent.py:602`, `engines/rag.py:143`, `engines/report.py:280`, `engines/url.py:96`, `engines/repo.py:166`, `engines/dataset.py:100` | `{"text": str}` | `frontend/lib/sse.ts:129-136` | `{kind:'token', text}` → appended to `message.content` (`frontend/lib/streams.ts:183-192`) |
| `reasoning` | `orchestrator/app/sse.py:75`, `engines/chat.py:98`, `engines/agent.py:599` | `{"text": str}` | `frontend/lib/sse.ts:130` | `{kind:'reasoning', text}` → `message.reasoning` (`frontend/lib/streams.ts:195-200`) |
| `status` | `orchestrator/app/main.py:415`, `engines/search.py:403,464,477`, `engines/url.py:32,41,44,49`, `engines/repo.py:31,37,40`, `engines/sql.py:317` | `{"text": str}` | `frontend/lib/sse.ts:131` | `{kind:'status', text}` → `message.searchStatus`, cleared by the next `token` (`frontend/lib/streams.ts:191,193-194`) |
| `step` | `orchestrator/app/sse.py:82-85`, `engines/agent.py:382` | `{"id": int, "title": str, "status": "running"\|"done"\|"failed", "detail"?: str}` | `frontend/lib/sse.ts:137-165` | `{kind:'step', step}` → `mergeStep` (`frontend/lib/sse.ts:229-238`) |
| `research` | `engines/search.py:229-239` (phase `query`), `engines/search.py:446,478` (`reading`), `engines/search.py:454,485` (`read`) | `{"phase":"query","query":str,"results":[{title,url,domain}]}` or `{"phase":"reading"\|"read","count":int}` | `frontend/lib/sse.ts:166-201` | `{kind:'research', phase, query?, count?}` |
| `meta` | `orchestrator/app/sse.py:62`, one per answer, merged with `meta_extras` at `orchestrator/app/main.py:364-379` | engine keys + `mode`,`model`,`effort`,`generation_id`, optional `input_trimmed`,`context`,`auto` | `frontend/lib/sse.ts:202-203` | `{kind:'meta', meta: JSON.parse(data)}` — **no validation at all** |
| `done` | `orchestrator/app/sse.py:66`, `main.py:647` | `{"session_id": str}` | `frontend/lib/sse.ts:204-205` | `{kind:'done'}` — payload discarded |
| `error` | `orchestrator/app/sse.py:70`, `main.py:672` | `{"message": str}` | `frontend/lib/sse.ts:206-215` | `{kind:'error', message}` |

**Mismatches found:**

1. `orchestrator/app/engines/dataset.py:101` and `:118` emit `{"route": "dataset"}`. The
   frontend's `Engine` union at `frontend/lib/types.ts:8` is
   `'sql'|'rag'|'vision'|'report'|'chat'|'agent'|'search'|'url'|'repo'` — **`'dataset'` is
   absent.** Because `meta` is cast without validation (`frontend/lib/sse.ts:203`) this does not
   throw, but every `switch (meta.route)` consumer (e.g. `frontend/components/EngineBadge.tsx`)
   hits its default branch.
2. `orchestrator/app/main.py:378-379` sets `data["auto"] = dict(orchestration_state)`. No `auto`
   key exists on `interface Meta` (`frontend/lib/types.ts:101-152`). Passes through untouched;
   unreadable to typed consumers.
3. The frontend never emits or expects a `ping` event on the real path, but its own MOCK stream
   sends one deliberately (`frontend/app/api/chat/route.ts:70`) to prove unknown types are
   dropped by `frontend/lib/sse.ts:216-217`. Verified: the `default: return null` branch handles it.
4. The mock stream (`frontend/app/api/chat/route.ts:40-121`) emits **no `status` and no
   `research` events**, so web-search UI paths are untestable under `MOCK_MODE=true`.

---

## 1. `frontend/app/api/auth/me/route.ts`  (34 LOC)

**Purpose** — GET the single local identity from the orchestrator so the UI can label the user
and `lib/history.ts` can scope its localStorage cache key.

**Public surface**
- `export const runtime = 'nodejs'` — `frontend/app/api/auth/me/route.ts:10`
- `export const dynamic = 'force-dynamic'` — `:11`
- `export async function GET(): Promise<Response>` — `:13`

**HTTP contract**
- `GET /api/auth/me`; **no request body, no params, no auth**.
- 200 → verbatim upstream JSON. Upstream is `orchestrator/app/auth.py:100-103` returning
  `{"username": str, "local": true}`.
- 502 `{message: "The orchestrator responded with status <n>."}` when upstream is non-2xx (`:22-25`).
- 502 `{message: "The orchestrator is unreachable."}` on any thrown error (`:28-33`).

**Control flow**
1. `:14-15` read `ORCHESTRATOR_URL`, default `http://localhost:8080`.
2. `:18-20` `fetch(`${orchestratorUrl}/auth/me`, {cache:'no-store'})` — **no cookie forwarded,
   no timeout, no `req` parameter at all** (the handler takes no arguments, `:13`).
3. `:21-26` non-ok → 502.
4. `:27` `Response.json(await upstream.json())` — 200 with upstream body.
5. `:28-33` bare `catch {}` → 502.

**State & side effects** — network egress to `ORCHESTRATOR_URL/auth/me` only. No DB, no FS, no
globals.

**Dependencies** — inbound: `frontend/lib/auth.ts:20` (`fetchMe`), called from
`frontend/components/ChatApp.tsx`. Outbound: global `fetch`.

**Config** — `process.env.ORCHESTRATOR_URL` at `:15`.

**Failure modes** — `await upstream.json()` at `:27` is **outside** the try/catch's protection
in the sense that it *is* inside the `try` (the `try` spans `:17-28`), so a non-JSON 200
upstream is caught and reported as "unreachable" — misleading but not a crash. **No timeout**;
a hung orchestrator hangs this route until the Node socket timeout. No retry.

**Concurrency** — async, stateless, no shared state.

**Complexity hotspots** — none (single function, 21 LOC).

**Notable** — this route uses raw `fetch` instead of `proxyToOrchestrator` (`frontend/lib/proxy.ts:21`),
so unlike `/api/history/*` it does **not** relay `Set-Cookie`. The file docstring at `:4-7`
correctly notes "This is NOT a login check any more".

---

## 2. `frontend/app/api/chat/route.ts`  (185 LOC)

**Purpose** — the SSE chat endpoint. Either serves a canned fixture stream (`MOCK_MODE=true`)
or translates the UI body to the orchestrator's `ChatRequest` and pipes the upstream SSE body
through byte-for-byte.

**Public surface**
- `runtime = 'nodejs'` — `:19`; `dynamic = 'force-dynamic'` — `:20`
- `const SSE_HEADERS` — `:22-27`: `Content-Type: text/event-stream; charset=utf-8`,
  `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`, `X-Accel-Buffering: no`
- `function sseFrame(event: string, data: unknown): Uint8Array` — `:29-33` (module-private)
- `function tokenize(text: string): string[]` — `:36-38` (module-private)
- `function mockStream(body: ChatRequestBody): Response` — `:40-121` (module-private, **82 LOC**)
- `export async function POST(req: Request): Promise<Response>` — `:123-185`

**HTTP contract**
- `POST /api/chat`, `Content-Type: application/json`. **No auth.**
- Request body = `ChatRequestBody` (`frontend/lib/orchestrator.ts:16-31`):
  `{ messages?: {role,content}[], session_id?, image?, conversation_id?, mode?, model?,
  effort?, agent?, pdf?, pdf_filename?, web_search? }`.
  What the client actually sends is at `frontend/lib/streams.ts:314-332`.
- 200 `text/event-stream` — the SSE frames of §0.5.
- 400 `{message: "Request body must be JSON with a messages array."}` — `:128-131` (JSON parse failure).
- 400 `{message: "The request contains no user message or image to send."}` — `:146-149`
  (`toOrchestratorChatRequest` returned null).
- 502 `{message: "The orchestrator is unreachable."}` — `:168-171` (fetch threw).
- 502 `{message: "The orchestrator responded with status <n>."}` — `:174-181` (upstream non-ok
  **or** upstream had no body).

**Control flow (real path)**
1. `:126` `body = await req.json()` — **buffers the entire request body in memory**, unbounded.
2. `:134-136` `MOCK_MODE === 'true'` → `mockStream(body)`.
3. `:138-139` read `ORCHESTRATOR_URL`.
4. `:144` `toOrchestratorChatRequest(body)` (`frontend/lib/orchestrator.ts:70-101`).
5. `:145-150` null → 400.
6. `:154-166` `POST ${orchestratorUrl}/chat` with `Content-Type: application/json`, optional
   `cookie`, `body: JSON.stringify(chatRequest)` (second full in-memory copy), `signal: req.signal`.
7. `:174-181` non-ok / no body → 502; **the upstream error body is discarded**, only its status
   number reaches the caller.
8. `:184` `new Response(upstream.body, {headers: SSE_HEADERS})` — the stream is *not* re-parsed.

**Control flow (`mockStream`, `:40-121`)**
1. `:41` `lastUserContent(body)`; `:42-48` `pickFixtureEngine(...)` → fixture.
2. `:49-53` tokenize answer and (for `model==='smart'`) the reasoning.
3. `:55-60` build `meta` = fixture meta + `mode`/`model`/`effort` overrides.
4. `:63-118` `ReadableStream.start`: `ping` (`:70`) → `sleep(450)` (`:72`) → reasoning deltas
   (`:74-78`) → step running/final pairs (`:80-99`) → tokens (`:101-106`) → `meta` (`:108`) →
   `done` (`:109`) → close.
5. `:115-117` `cancel()` sets the module-local `cancelled` flag checked at `:75,81,90,102,107`.

**State & side effects** — network egress `POST ORCHESTRATOR_URL/chat`. In mock mode: timers
(`setTimeout` via the `sleep` closure at `:65-66`) and `Math.random()` (`:77,89,105`). No FS, no DB.

**Dependencies** — inbound: `frontend/lib/streams.ts:311` (`fetch('/api/chat', …)`), reached from
`ChatApp.tsx:456,466,525,578`. Outbound: `@/lib/fixtures` (`FIXTURES`, `MOCK_MODEL_IDS`,
`pickFixtureEngine`, `:12`), `@/lib/orchestrator` (`lastUserContent`,
`toOrchestratorChatRequest`, `ChatRequestBody`, `:13-17`).

**Config** — `MOCK_MODE` (`:134`), `ORCHESTRATOR_URL` (`:139`).

**Failure modes**
- `catch {}` at `:127` (JSON parse), `:111-113` (mock stream — comment "Client disconnected
  mid-stream — nothing to clean up"), `:167` (fetch).
- **No timeout** on the upstream fetch (`:154`); no retry; no request-body size bound.
- Upstream 422 (a Pydantic validation failure, e.g. `model` outside
  `Literal["smart","fast"]`, `orchestrator/app/main.py:192`) is flattened to a 502 whose only
  content is the number `422`. The client then reports **"The orchestrator is unreachable"**
  (`frontend/lib/streams.ts:335-337` → `markUnreachable`, `:172-173`), which is false.
- Once the SSE body is piped (`:184`) there is no way to signal a later error; the orchestrator
  is responsible for the terminal `error` frame.

**Concurrency** — async. `mockStream`'s `cancelled` is per-invocation closure state, not module
state — safe. `req.signal` is threaded to the upstream fetch (`:165`) so a client abort
propagates, but the orchestrator's generation is deliberately detached
(`orchestrator/app/main.py:62-76`) and keeps running.

**Complexity hotspots** — `mockStream` `:40-121` = **82 LOC**, with a 56-LOC nested `async
start(controller)` and 6 `cancelled` guards.

**Notable**
- Magic numbers in the mock pacing: `450` ms (`:72`), `0.05`/`90`/`8+16` (`:77`),
  `500+500` (`:89`), `0.06`/`120`/`14+26` (`:105`).
- `SSE_HEADERS` (`:22-27`) is duplicated verbatim in
  `frontend/app/api/chat/attach/[id]/route.ts:11-16`.
- No TODO/FIXME/HACK markers.

---

## 3. `frontend/app/api/chat/active/route.ts`  (29 LOC)

**Purpose** — list conversation ids the orchestrator is still generating for, so the sidebar can
show spinners and re-attach after reload.

**Public surface** — `runtime` `:7`, `dynamic` `:8`, `export async function GET(req: Request)` `:10`.

**HTTP contract**
- `GET /api/chat/active`. No body, no params, no auth.
- 200 `{active: []}` when `MOCK_MODE=true` (`:12`).
- Upstream status + upstream JSON otherwise (`:25`). Upstream:
  `orchestrator/app/main.py:725-737` returns `{"active": [conversation_key, ...]}`.
- 200 `{active: []}` on any thrown error (`:27`) — **note the failure is reported as a
  successful empty list, not a 502.**

**Control flow** — `:11-13` mock short-circuit → `:14-15` env → `:17-24` fetch with
`cache:'no-store'` + optional cookie → `:25` re-wrap.

**State & side effects** — network egress `GET ORCHESTRATOR_URL/chat/active`.

**Dependencies** — inbound: `frontend/lib/streams.ts:97` (`fetchServerActive`), polled every
8000 ms from `frontend/components/ChatApp.tsx:301` and used at `:182,210,282`. Outbound: `fetch`.

**Config** — `MOCK_MODE` `:11`, `ORCHESTRATOR_URL` `:15`.

**Failure modes** — bare `catch {}` at `:26`. `await upstream.json()` at `:25` throws on a
non-JSON upstream response (e.g. an HTML 502 from an intermediary) and is swallowed into
`{active: []}`. **No timeout.** `req.signal` is **not** forwarded, so an abandoned poll keeps
the upstream socket open.

**Concurrency** — async; the 8 s poll can overlap a slow response (no in-flight guard at
`ChatApp.tsx:301`).

**Complexity hotspots** — none.

**Notable** — the only route in the tree that converts a transport failure into a 200. That is
deliberate (a failed poll must not paint an error), but it also hides a persistently down
orchestrator from the sidebar.

---

## 4. `frontend/app/api/chat/attach/[id]/route.ts`  (67 LOC)

**Purpose** — re-join a detached server-side generation; the orchestrator replays its buffered
SSE events then streams live.

**Public surface**
- `runtime` `:8`, `dynamic` `:9`
- `const SSE_HEADERS` — `:11-16` (duplicate of `frontend/app/api/chat/route.ts:22-27`)
- `const SAFE_ID = /^[\w-]{1,64}$/` — `:18`
- `export async function GET(req, {params}: {params: Promise<{id: string}>})` — `:20-23`

**HTTP contract**
- `GET /api/chat/attach/{id}`. No body, no auth.
- 400 `{message: 'invalid conversation id'}` — `:31` (malformed percent-escape) and `:34`
  (fails `SAFE_ID`).
- 404 `{message: 'no active generation'}` — `:37` (mock mode) and `:62-64` when upstream 404s.
- 502 `{message: 'The orchestrator is unreachable.'}` — `:55-58`.
- 502 `{message: 'no active generation'}` — `:61-64` for any other upstream non-ok. **The 502
  branch reuses the 404 message**, so a 500 from the orchestrator is reported to the client as
  "no active generation" with status 502.
- 200 `text/event-stream` — `:66`.

**Control flow**
1. `:24` `const {id} = await params` (Next 15 async params).
2. `:27-32` `decodeURIComponent(id)` inside try/catch — the comment at `:25-26` records that an
   unguarded call *"surfaced as an unhandled 500"*.
3. `:33-35` `SAFE_ID.test(decoded)` — `[\w-]{1,64}`, exactly matching the orchestrator's
   `_CONVERSATION_ID_RE = ^[A-Za-z0-9_-]{1,64}$` (`orchestrator/app/history.py:34`). **No path
   traversal is possible here**: `.`, `/`, `\`, `%` are all rejected, and the value is
   re-encoded with `encodeURIComponent` at `:44`.
4. `:36-38` mock mode → 404.
5. `:43-53` `fetch(.../chat/attach/<enc>)` with `signal: req.signal` and optional cookie.
6. `:60-65` non-ok → 404/502.
7. `:66` pipe `upstream.body`.

**State & side effects** — network egress `GET ORCHESTRATOR_URL/chat/attach/{id}`.

**Dependencies** — inbound: `frontend/lib/streams.ts:372-375` (`attachStream`), called from
`frontend/components/ChatApp.tsx:215,627`. Outbound: `fetch`.

**Config** — `MOCK_MODE` `:36`, `ORCHESTRATOR_URL` `:40`.

**Failure modes** — `catch {}` at `:30` and `:54`. **No timeout** (correct here — it is a long
stream). `req.signal` **is** forwarded (`:45`), so client disconnect releases the upstream socket.

**Concurrency** — async; no shared state.

**Complexity hotspots** — none (GET is 48 LOC, straight-line).

**Notable** — this is the **only** dynamic route in the tree that guards `decodeURIComponent`;
`frontend/app/api/reports/[filename]/route.ts:20` has the identical call unguarded (see §8).

---

## 5. `frontend/app/api/chat/compact/route.ts`  (34 LOC)

**Purpose** — forward "Compact now" to the orchestrator's rolling-summary compaction.

**Public surface** — `runtime` `:6`, `dynamic` `:7`, `export async function POST(req: Request)` `:9`.

**HTTP contract**
- `POST /api/chat/compact`, JSON.
- Request body (built at `frontend/components/ChatApp.tsx:349-356`):
  `{conversation_id: string, messages: {role,content}[]}`. Upstream model
  `CompactRequest` at `orchestrator/app/main.py:740-742` = `{conversation_id: str,
  messages?: ChatMessage[]}`. **Match.**
- 200 `{compacted: false, reason: 'mock mode'}` in `MOCK_MODE` (`:11`).
- Upstream status + upstream JSON (`:27`). Upstream success shape
  `{compacted: true, folded_turns: int, covers_through: int}`
  (`orchestrator/app/main.py:775-779`) or `{compacted: false, reason: "nothing older to
  summarize"}` (`:774`).
- 502 `{compacted: false, reason: 'orchestrator unreachable'}` (`:29-32`).

**Control flow** — `:10-12` mock → `:13-14` env → `:16-26` `POST /chat/compact` with
`Content-Type: application/json`, optional cookie, `body: await req.text()` (**raw
passthrough — the body is never parsed or validated here**) → `:27` re-wrap.

**State & side effects** — network egress. Upstream side effect: writes the rolling summary row
(`orchestrator/app/main.py:772` → `compaction.compact(..., force=True)`).

**Dependencies** — inbound: `frontend/components/ChatApp.tsx:348`. Outbound: `fetch`.

**Config** — `MOCK_MODE` `:10`, `ORCHESTRATOR_URL` `:14`.

**Failure modes** — bare `catch {}` at `:28`; a non-JSON upstream response makes
`await upstream.json()` (`:27`) throw and be reported as "orchestrator unreachable".
**No timeout** — and compaction runs a full LLM summarization pass upstream, so this is the
route most likely to sit open for minutes. **`req.signal` is not forwarded**, so a user
navigating away leaves the upstream call running.

**Concurrency** — async. Client-side re-entry is guarded by `compacting` state
(`frontend/components/ChatApp.tsx:344`), but nothing prevents two browser tabs compacting the
same conversation concurrently.

**Complexity hotspots** — none.

**Notable** — no validation that `conversation_id` is present; an empty body reaches the
orchestrator and returns its 422 verbatim with status 422.

---

## 6. `frontend/app/api/chat/stop/route.ts`  (32 LOC)

**Purpose** — cancel a detached generation server-side (closing the SSE stream no longer stops it).

**Public surface** — `runtime` `:7`, `dynamic` `:8`, `export async function POST(req: Request)` `:10`.

**HTTP contract**
- `POST /api/chat/stop`, JSON.
- Request body sent by the client: `{conversation_id: id, session_id: id}`
  (`frontend/lib/streams.ts:90`). Upstream `StopRequest` at `orchestrator/app/main.py:688-690` =
  `{conversation_id?: str|None, session_id: str = "default"}`; the key used is
  `body.conversation_id or body.session_id` (`orchestrator/app/main.py:716`). **Match.**
- 200 `{stopped: false}` in `MOCK_MODE` (`:12`).
- Upstream status + `{stopped: bool}` (`:28`).
- 502 `{stopped: false}` (`:30`).

**Control flow** — `:11-13` mock → `:14-15` env → `:17-27` `POST /chat/stop` with raw
`await req.text()` body → `:28` re-wrap.

**State & side effects** — network egress; upstream cancels an `asyncio.Task`
(`orchestrator/app/main.py:721`).

**Dependencies** — inbound: `frontend/lib/streams.ts:87-91` (`stopStream`). Outbound: `fetch`.

**Config** — `MOCK_MODE` `:11`, `ORCHESTRATOR_URL` `:15`.

**Failure modes** — bare `catch {}` `:29`. No timeout. `req.signal` not forwarded. The caller
fire-and-forgets with `.catch(() => undefined)` (`frontend/lib/streams.ts:91`), so a stop that
silently fails leaves the GPU generating with no user-visible signal.

**Concurrency** — async, stateless.

**Complexity hotspots** — none.

**Notable** — `stopStream` (`frontend/lib/streams.ts:82-92`) aborts the local reader *before*
awaiting the server call and never inspects the `{stopped}` result.

---

## 7. `frontend/app/api/history/[...path]/route.ts`  (73 LOC)

**Purpose** — allowlisted proxy for the orchestrator's conversation CRUD and chat search.

**Public surface**
- `runtime` `:12`, `dynamic` `:13`
- `type Ctx = { params: Promise<{ path: string[] }> }` — `:15`
- `async function handle(req: Request, ctx: Ctx)` — `:17-57` (module-private)
- `export async function GET / POST / PUT / DELETE (req, ctx)` — `:59, :63, :67, :71`

**HTTP contract** (all four verbs share `handle`)

| method + path | upstream | request body | responses |
|---|---|---|---|
| `GET /api/history/conversations[?archived=<bool>]` | `GET /history/conversations` (`orchestrator/app/history.py:80-86`) | — | 200 `ServerConversationSummary[]` (bare array) |
| `POST /api/history/conversations` | `POST /history/conversations` (`:88`) | `{id?: string, title: string}` | 200 conversation, 400 bad id, 400 empty title, 409 id exists |
| `GET /api/history/conversations/{id}` | `GET .../{id}` (`:105`) | — | 200 `{id,title,messages:[{role,content,meta}]}`, 404 |
| `PUT /api/history/conversations/{id}` | `PUT .../{id}` (`:116`) | `{title?,pinned?,archived?}` (`extra='forbid'`) | 200, 404, 422 on unknown key |
| `POST /api/history/conversations/{id}/messages` | `POST .../messages` (`:134`) | `{role,content,meta?}` | 200 message, 400 bad role, 404 |
| `PUT /api/history/conversations/{id}/messages` | `PUT .../messages` (`:151`) | `{messages: MessageIn[]}` | 200, 400, 404, **409 "refusing to shrink"** |
| `POST /api/history/conversations/{id}/truncate` | `POST .../truncate` (`:200`) | `{keep:int, expected_total:int}` | 200, 400, **409 conversation changed**, 404 |
| `GET /api/history/conversations/{id}/summary` | `GET .../summary` (`:239`) | — | 200 `{summary,covers_through,updated_at?}`, 404 — **no frontend caller** |
| `GET /api/history/search?q=&limit=` | `GET /history/search` (`:266`) | — | 200 `{results:[…]}`, 400 if `q` > 100 chars |
| anything else | — | — | **404 `{message: 'Unknown history endpoint.'}`** (`:27-32`) |
| orchestrator down | — | — | 502 `{message: 'The orchestrator is unreachable.'}` (`frontend/lib/proxy.ts:44-47`) |

**Control flow**
1. `:18-19` `const {path} = await ctx.params; const parts = path ?? []`.
2. `:22-23` `isSearch = parts.length === 1 && parts[0] === 'search' && req.method === 'GET'`.
3. `:27-32` reject unless `isSearch` or (`parts[0] === 'conversations'` **and** `parts.length <= 3`).
4. `:33-35` `MOCK_MODE=true` → `handleMockHistory(req, parts)` (`frontend/lib/mockApi.ts:151`).
5. `:37` `const params = new URL(req.url).searchParams`.
6. `:39-45` search branch: rebuild the query string from `q` (defaulting to `''`) and optional
   `limit` — an explicit allowlist, comment at `:40`.
7. `:47-52` non-search: forward `?archived=` **only when `parts.length === 1`**. Every other
   query parameter on every other path is dropped.
8. `:53-56` `proxyToOrchestrator(req, '/history/' + parts.map(encodeURIComponent).join('/') + query)`.

**Path-traversal analysis (verified experimentally)**
`encodeURIComponent('..') === '..'` — the dot is unreserved and is not escaped. The joined
string is then handed to `fetch`, whose WHATWG URL parser performs dot-segment removal. Measured
with Node:

```
['conversations','..','..']    -> http://x:8080/
['conversations','..','search'] -> http://x:8080/history/search
['conversations','%2e%2e','%2e%2e'] -> http://x:8080/history/conversations/%252e%252e/%252e%252e
```

So the allowlist claimed at `:26-27` ("Only the documented /history/conversations… tree and
/history/search are proxied") **is defeated**: `GET /api/history/conversations/../..` reaches
the orchestrator's `/`, and `…/../X` reaches `/history/X` for any single segment `X`.
The blast radius is bounded because (a) `parts.length > 3` is rejected so no deeper path can be
assembled, (b) `encodeURIComponent` escapes any embedded `/` to `%2F` which the URL parser does
**not** treat as a separator, and (c) `/` and `/history/` have no FastAPI routes. Reachable
targets are therefore `/` (404) and `/history/<single-segment>` — of which only `search` exists,
and it is reached with `q=''` because the query string is empty on the 3-part branch. **Not
currently exploitable, but the invariant the comment asserts does not hold.**

**State & side effects** — network egress through `proxyToOrchestrator`; upstream writes to the
SQLite conversation/message tables. In mock mode, mutates the in-memory store in
`frontend/lib/mockApi.ts`.

**Dependencies** — inbound: `frontend/lib/historyApi.ts:117` (`const BASE =
'/api/history/conversations'`) and `:237` (`/api/history/search`); those are used by
`frontend/lib/history.ts` and `frontend/components/SearchPalette.tsx`. Outbound:
`@/lib/mockApi` (`handleMockHistory`, `:9`), `@/lib/proxy` (`proxyToOrchestrator`, `:10`).

**Config** — `MOCK_MODE` `:33`; `ORCHESTRATOR_URL` indirectly via `frontend/lib/proxy.ts:10`.

**Failure modes** — no try/catch here; every failure is handled inside `proxyToOrchestrator`.
No timeout. No body-size limit (the whole-thread `PUT .../messages` body is buffered by
`await req.text()` at `frontend/lib/proxy.ts:39`).

**Concurrency** — async; four exported verbs all delegate to one function, no shared state.

**Complexity hotspots** — none (`handle` is 41 LOC).

**Notable** — `GET .../{id}/summary` exists upstream (`orchestrator/app/history.py:239`) and is
reachable through this proxy (3 parts) but **no frontend code calls it** — dead surface.

---

## 8. `frontend/app/api/reports/route.ts`  (36 LOC)

**Purpose** — list generated report files.

**Public surface** — `runtime` `:8`, `dynamic` `:9`, `export async function GET()` `:11`.

**HTTP contract**
- `GET /api/reports`. No params, no body, **no auth, no cookie forwarding**.
- `MOCK_MODE=true` → 200 with `MOCK_REPORTS` — a **bare array** of
  `{name, size, mtime, type}` (`frontend/lib/fixtures.ts:378-397`).
- Real mode → 200 with the upstream body, which is an **object**
  `{"reports": [{filename, size_bytes, modified}]}`
  (`orchestrator/app/main.py:257-259` → `orchestrator/app/core/report_paths.py:51-70`).
- 502 on upstream non-ok (`:23-28`) or thrown (`:30-35`).

**Control flow** — `:12-14` mock → `:16-17` env → `:20-22` `fetch(.../reports, {cache:'no-store'})`
→ `:23-28` non-ok 502 → `:29` return upstream JSON.

**State & side effects** — network egress `GET ORCHESTRATOR_URL/reports`.

**Dependencies** — inbound: **none.** `rg -n "fetch\('/api/reports"` over `frontend/` returns
nothing; the only `/api/reports` reference is the download `href` in
`frontend/components/FileCards.tsx:18`, which targets the `[filename]` route. **This route is
dead code.** Outbound: `@/lib/fixtures` (`MOCK_REPORTS`, `:6`).

**Config** — `MOCK_MODE` `:12`, `ORCHESTRATOR_URL` `:17`.

**Failure modes** — bare `catch {}` `:30`. No timeout, no retry.

**Concurrency** — async, stateless.

**Complexity hotspots** — none.

**Notable** — **shape divergence between mock and real**: mock returns `Array<{name,…}>`, real
returns `{reports: Array<{filename,…}>}`. The `ReportFile` type
(`frontend/lib/types.ts:74-78`) uses `filename`/`type`/`size`, matching neither exactly. Because
nothing consumes the route the divergence is latent, but it is a trap for the next caller.

---

## 9. `frontend/app/api/reports/[filename]/route.ts`  (70 LOC)

**Purpose** — download proxy for one report file.

**Public surface**
- `runtime` `:10`, `dynamic` `:11`
- `const SAFE_FILENAME = /^[\w][\w.\- ]{0,199}$/` — `:13`
- `export async function GET(_req, {params}: {params: Promise<{filename: string}>})` — `:15-18`

**HTTP contract**
- `GET /api/reports/{filename}`. No auth, **no cookie forwarding**.
- 400 `{message: 'Invalid filename.'}` — `:28`, when the name fails `SAFE_FILENAME` **or**
  contains `..`, `/`, `\` (`:22-27`).
- `MOCK_MODE=true` → 200 `application/octet-stream` placeholder text with
  `Content-Disposition: attachment; filename="<decoded>"` (`:31-39`).
- 200 with the upstream stream, `Content-Type` and `Content-Disposition` taken from upstream or
  defaulted (`:55-63`).
- 404 when upstream 404s, 502 for any other upstream non-ok (`:49-54`).
- 502 `{message: 'The orchestrator is unreachable.'}` (`:64-69`).

**Control flow**
1. `:19` `const {filename} = await params`.
2. `:20` `const decoded = decodeURIComponent(filename)` — **NOT wrapped in try/catch.**
3. `:22-29` sanitize (regex + explicit `..`, `/`, `\` checks).
4. `:31-39` mock branch.
5. `:41-42` env.
6. `:45-48` `fetch(.../reports/${encodeURIComponent(decoded)}, {cache:'no-store'})`.
7. `:49-54` status mapping.
8. `:55-63` pipe `upstream.body` with headers.

**Path-traversal analysis** — safe. `SAFE_FILENAME` (`:13`) forbids `/`, `\`, `%`, `:` and any
leading dot (`^[\w]`), and the three explicit `includes` checks at `:24-26` are belt-and-braces.
A double-encoded payload `/api/reports/%252e%252e%252fetc%252fpasswd` is decoded once by Next
and once at `:20` to `../etc/passwd`, then rejected at `:24-25`. The orchestrator re-validates
independently (`orchestrator/app/main.py:265` → `resolve_report_file`). **No traversal.**

**Header-injection analysis** — `:36` and `:61` interpolate `decoded` into a
`Content-Disposition` value. `SAFE_FILENAME` excludes `\r`, `\n` and `"`, so no CRLF or quote
break-out is possible. Safe.

**State & side effects** — network egress `GET ORCHESTRATOR_URL/reports/{filename}`.

**Dependencies** — inbound: `frontend/components/FileCards.tsx:18`
(`href={`/api/reports/${encodeURIComponent(f.filename)}`}`). Outbound: `fetch`.

**Config** — `MOCK_MODE` `:31`, `ORCHESTRATOR_URL` `:42`.

**Failure modes**
- **`decodeURIComponent` at `:20` throws `URIError` on a malformed escape.** Verified in Node:
  `decodeURIComponent('%')` → `URIError`. This is the exact bug that
  `frontend/app/api/chat/attach/[id]/route.ts:25-32` documents as having *"surfaced as an
  unhandled 500"* and fixed there — the same fix was never applied here.
- bare `catch {}` at `:64`.
- No timeout; the response body is streamed (`:55`) so file size is not a memory concern, but
  `_req.signal` is **not** forwarded (the parameter is named `_req` and unused apart from the
  signature), so an aborted download leaves the upstream fetch running.

**Concurrency** — async, stateless.

**Complexity hotspots** — none (GET is 56 LOC, straight-line).

**Notable** — `decoded` is double-decoded relative to the raw URL (Next decodes route params,
then `:20` decodes again). Harmless given the sanitizer, but it means a legitimate report whose
name contains a literal `%` cannot be downloaded.

---

## 10. `frontend/app/api/upload/route.ts`  (47 LOC)

**Purpose** — stream a multipart dataset upload through to the orchestrator without buffering.

**Public surface** — `runtime` `:10`, `dynamic` `:11`, `export async function POST(req: Request)` `:13`.

**HTTP contract**
- `POST /api/upload`, `multipart/form-data`. **No auth.**
- Client body (`frontend/components/ChatApp.tsx:440-444`): `FormData` with `file` (the `File`)
  and `conversation_id` (string). Upstream signature
  `orchestrator/app/uploads.py:66-71`: `file: UploadFile = File(...)`,
  `conversation_id: str = Form(...)`. **Match.**
- 404 `{message: 'uploads are disabled in mock mode'}` in `MOCK_MODE` (`:15`).
- Upstream status + upstream body, always relabelled `content-type: application/json` (`:36-40`).
  Upstream success is `{upload_id, files, notes, …}`; failures include 404 (uploads disabled /
  conversation not found, `orchestrator/app/uploads.py:73,78`) and **413** when the streamed
  size exceeds `settings.upload_max_mb` (`orchestrator/app/uploads.py:56-60`).
- 502 `{detail: 'The orchestrator is unreachable.'}` (`:42-45`).

**Control flow**
1. `:14-16` mock short-circuit.
2. `:17-18` env.
3. `:20-35` `fetch(.../uploads, {method:'POST', headers: {content-type?, cookie?}, body: req.body,
   duplex:'half', signal: req.signal})`. `duplex: 'half'` is required by undici when the body is
   a stream (`:32-33`), and is cast in via `as RequestInit & {duplex:'half'}` (`:35`).
4. `:36` `const body = await upstream.text()` — **the response is fully buffered** (fine; it is
   a small JSON object).
5. `:37-40` re-wrap with the upstream status.

**Validation performed here: none.** No content-type check, no size check, no field check, no
filename check. The 10/25/200 MB limits (`frontend/components/Composer.tsx:42,65,68`) are
**client-side only**. The only real cap is server-side and streaming
(`orchestrator/app/uploads.py:44-61`), so bytes above the cap still transit the Next.js process
before being rejected.

**State & side effects** — network egress `POST ORCHESTRATOR_URL/uploads`. Upstream writes to
`settings.workspace_dir/uploads/<conv>/<upload_id>` and to SQLite.

**Dependencies** — inbound: `frontend/components/ChatApp.tsx:444`. Outbound: `fetch`.

**Config** — `MOCK_MODE` `:14`, `ORCHESTRATOR_URL` `:18`.

**Failure modes** — bare `catch {}` `:41`. The hardcoded `content-type: application/json` at
`:39` mislabels any non-JSON upstream body (e.g. a FastAPI HTML 500 page), making
`await res.json()` at `frontend/components/ChatApp.tsx:445` throw and surface as
"That dataset could not be read." rather than the real error. **No timeout.**

**Concurrency** — async; `req.signal` is forwarded (`:34`), so aborting the browser upload
aborts the upstream leg.

**Complexity hotspots** — none.

**Notable** — the docstring at `:5-8` is the clearest statement in the repo of the memory
problem that `/api/chat` still has: *"Images and PDFs travel as base64 inside the chat body,
which is fine at 10-25 MB but would hold ~270 MB in memory for a 200 MB archive."* The fix was
applied to datasets and **not** to the chat body path.

---

## 11. `frontend/app/layout.tsx`  (44 LOC)

**Purpose** — root App Router layout: self-hosted fonts, metadata, pre-hydration theme script.

**Public surface**
- `const APP_NAME` — `:15` (`NEXT_PUBLIC_APP_NAME ?? 'TechSara AI'`)
- `export const metadata: Metadata` — `:17-25` (title, description, `/favicon.png`,
  `/apple-touch-icon.png`)
- `const themeInit` — `:31` (module-private inline script string)
- `export default function RootLayout({children}: {children: ReactNode})` — `:33-44`

**Control flow** — `:35` `<html lang="en" suppressHydrationWarning>` → `:36-38` `<head>` with
`<script dangerouslySetInnerHTML={{__html: themeInit}} />` → `:39-41` `<body><Providers>`.

**State & side effects** — the inline script reads `localStorage.getItem('techsara.theme')`
(`:31`) and mutates `document.documentElement.classList` and `.style.colorScheme` before paint.
Font CSS side-effect imports at `:5-10` (`@fontsource/ibm-plex-sans` 400/500/600/700,
`@fontsource/jetbrains-mono` 400/500) — **self-hosted, zero CDN egress** (comment `:4`).

**Dependencies** — inbound: Next.js framework (implicit). Outbound: `next` (`Metadata`, `:1`),
`react` (`ReactNode`, `:2`), `./globals.css` (`:12`), `@/components/Providers` (`:13`).

**Config** — `process.env.NEXT_PUBLIC_APP_NAME` at `:15` — **this one IS in the client bundle by
design** (branding only).

**Failure modes** — the theme script's `try/catch` (`:31`) swallows a `localStorage` access
error and falls back to `'dark'`. No other failure paths.

**Concurrency** — synchronous server component.

**Complexity hotspots** — none.

**Notable** — `dangerouslySetInnerHTML` at `:37` with a **static, non-interpolated** string —
not an XSS vector, but it does mean a future `script-src` CSP would need `'unsafe-inline'` or a
nonce. `'techsara.theme'` is a magic string duplicated wherever the theme toggle writes it.

---

## 12. `frontend/app/page.tsx`  (5 LOC)

**Purpose** — the `/` route; renders the chat shell.

**Public surface** — `export default function ChatPage()` — `:3-5`.

**Control flow** — returns `<ChatApp />` (`:4`).

**State & side effects** — none in this file.

**Dependencies** — inbound: Next.js router. Outbound: `@/components/ChatApp` (`:1`).

**Config** — none.

**Failure modes / Concurrency / Complexity** — none.

**Notable** — no `export const dynamic`; `ChatApp` is a client component so the page is a thin
server wrapper. The `?c=<id>` deep link is handled inside `ChatApp`
(`frontend/components/ChatApp.tsx:126-127` `window.history.replaceState`), not here.

---

## 13. `frontend/lib/sse.ts`  (301 LOC)

**Purpose** — hand-rolled, spec-compliant SSE parser plus the typed mapping from raw frames to
the chat contract. The docstring at `:4-9` records the decision **not** to use the Vercel AI SDK
because its data-stream protocol drops the custom `meta` event.

**Public surface**
- `export interface SSEEvent { event: string; data: string }` — `:16-21`
- `export class SSEParser` — `:23`
  - `feed(chunk: string): SSEEvent[]` — `:34-64`
  - `private processLine(line: string): SSEEvent | null` — `:66-96`
  - private fields: `buffer` `:24`, `eventType` `:25`, `dataLines` `:26`, `pendingCR` `:28`
- `export type ChatStreamEvent` — `:105-118`, the 8-arm union:
  `token | reasoning | status | step | research | meta | done | error`
- `export function toChatStreamEvent(ev: SSEEvent): ChatStreamEvent | null` — `:126-222`
- `export function mergeStep(steps: AgentStep[] | undefined, step: AgentStep): AgentStep[]` — `:229-238`
- `export function foldStreamState(meta: Meta, live: {reasoning?, reasoningSeconds?, steps?, research?}): Meta` — `:246-278`
- `export async function* readChatStream(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatStreamEvent>` — `:283-301`

**Control flow — `SSEParser.feed` (`:34-64`)**
1. `:35` empty chunk → `[]`.
2. `:36-41` if the previous chunk ended in `\r`, swallow a leading `\n` (split CRLF).
3. `:42` append to `buffer`.
4. `:46-62` loop: `buffer.search(/[\r\n]/)` (`:47`); if none, break. Slice the line (`:49`),
   compute `sepLen` — 2 for `\r\n` (`:54-56`), set `pendingCR` when `\r` is the last byte
   (`:52-53`). Advance the buffer (`:58`), `processLine` (`:60`), collect (`:61`).

**Control flow — `processLine` (`:66-96`)**
1. `:67-77` blank line dispatches: returns `null` if there is neither data nor an event type
   (`:69`); otherwise emits `{event: this.eventType || 'message', data: dataLines.join('\n')}`
   and resets.
2. `:78` a line starting with `:` is a comment/keep-alive → `null`.
3. `:80-90` split on the first `:`; strip one leading space from the value (`:89`).
4. `:92-93` `event` sets the type, `data` pushes; `id:`/`retry:` ignored (`:94`).

**Control flow — `toChatStreamEvent` (`:126-222`)** — a `switch` on `ev.event` inside a single
`try` whose `catch` returns `null` (`:219-221`):
- `:129-136` `token`/`reasoning`/`status`: require `typeof parsed.text === 'string'`, else null.
- `:137-165` `step`: require `typeof id === 'number'` **and** `typeof title === 'string'`
  (`:144-146`), and `status ∈ {running,done,failed}` (`:147-152`); `detail` copied only when it
  is a string (`:160-162`).
- `:166-201` `research`:
  - `phase === 'query'` → require `typeof query === 'string'` (`:174`); `results` defaults to
    `[]` if not an array (`:175`); each result is kept **only** when `o.url` is a string, with
    `title` falling back to the url and `domain` to `''` (`:182-191`).
  - `phase === 'reading' | 'read'` → require `typeof count === 'number'` (`:196-198`).
  - anything else → null (`:200`).
- `:202-203` `meta`: **`return {kind:'meta', meta: JSON.parse(ev.data)}` — zero validation, a
  raw unchecked cast to `Meta`.**
- `:204-205` `done`: payload discarded.
- `:206-215` `error`: `message` must be a string, else the literal fallback
  `'The engine reported an error without details.'`.
- `:216-217` `default: return null` — unknown event types are silently dropped, which is the
  documented requirement (`:122-125`).

**Control flow — `foldStreamState` (`:246-278`)**
1. `:255` shallow-copy meta.
2. `:259-261` carry client-measured `research` onto meta when the server did not send one, with
   `active: false`.
3. `:262` carry `reasoning`; `:263-265` carry `reasoning_seconds`.
4. `:266-276` merge steps: live steps adopt the meta step's final `status` (`:268-271`), then
   meta-only steps are appended (`:272-274`).

**Control flow — `readChatStream` (`:283-301`)**
1. `:286-288` `body.getReader()`, `new TextDecoder()`, `new SSEParser()`.
2. `:290-297` read loop; `decoder.decode(value, {stream: true})` handles split multibyte;
   each parsed frame is mapped and yielded only when non-null (`:294-295`).
3. `:298-300` `finally { reader.releaseLock() }` — **note: `reader.cancel()` is never called**,
   so an early `break` by the consumer (`frontend/lib/streams.ts:255,260`) releases the lock but
   does not cancel the underlying stream.

**State & side effects** — none. Pure parsing; no I/O, no globals.

**Dependencies** — inbound: `frontend/lib/streams.ts:24` (`foldStreamState`, `mergeStep`,
`readChatStream`), `frontend/tests/sse.test.ts`, `frontend/tests/research.test.ts`,
`frontend/tests/websearch.test.ts`. Outbound: type-only imports of `AgentStep`, `ResearchQuery`,
`Meta`, `Research` from `./types` (inline `import('./types')` at `:109,112,116,230-232,249-252`).

**Config** — none.

**Failure modes** — the single `try/catch` at `:127/:219` swallows every `JSON.parse` failure
into `null` (a malformed frame is dropped with no telemetry). `readChatStream` has no timeout and
no idle detection: if the orchestrator holds the connection open without sending, the generator
awaits `reader.read()` forever.

**Concurrency** — `SSEParser` is stateful but per-instance; `readChatStream` constructs a fresh
one (`:288`). No module-level mutable state. Safe for concurrent streams.

**Complexity hotspots** — `toChatStreamEvent` `:126-222` = **97 LOC**, 8 switch arms plus ~14
nested type guards. This is the largest function in the file and the second largest in the
assignment.

**Notable**
- The asymmetry is stark: `step` and `research` are validated field-by-field, `meta` is not
  validated at all (`:203`). `meta` is the only event that carries `sql`, `data`, `chart` and
  `report_files` into the UI.
- `mergeStep` at `:236` uses `{...list[idx], ...step}`, so an update that omits `detail` keeps
  the earlier one — documented at `:225-227`.
- No TODO/FIXME/HACK.

---

## 14. `frontend/lib/streams.ts`  (393 LOC)

**Purpose** — module-level registry of live per-conversation generations, so switching chats or
reloading never kills a running answer.

**Public surface**
- `export type StreamStatus = 'streaming'|'done'|'stopped'|'error'|'unreachable'` — `:27-32`
- `export interface LiveStreamView {conversationId, messages, status}` — `:34-38`
- `interface LiveStream extends LiveStreamView` — `:40-48` (private): adds `controller`,
  `assistantId`, `reasoningStartedAt`, `researchStartedAt`, `reasoningSeconds?`, `sawToken`
- `const streams = new Map<string, LiveStream>()` — `:50` — **module-level mutable state**
- `const listeners = new Set<(id: string) => void>()` — `:51` — **module-level mutable state**
- `export function subscribeStreams(fn): () => void` — `:53-58`
- `function notify(id: string): void` — `:60-62` (private)
- `export function getLiveStream(id): LiveStreamView | null` — `:64-68`
- `export function isStreaming(id): boolean` — `:70-72`
- `export function streamingIds(): string[]` — `:75-79`
- `export function stopStream(id): void` — `:82-92`
- `export async function fetchServerActive(): Promise<string[]>` — `:95-106`
- `export function attachBaseTurns(messages): ChatMessage[]` — `:110-114`
- `export function messagesDiscardedByRegenerate(messages, messageId): number` — `:124-131`
- `function updateAssistant(s, patch)` — `:133-144` (private)
- `function settleReasoningClock(s)` — `:147-155` (private)
- `function finalize(s, patch)` — `:157-163` (private)
- `function markUnreachable(s)` — `:165-178` (private)
- `async function consume(s, body)` — `:180-269` (private, **90 LOC**)
- `function register(conversationId, turns): LiveStream` — `:271-295` (private)
- `export interface StartStreamOptions {conversationId, turns, prefs, image?, pdf?, pdfName?}` — `:297-304`
- `export async function startStream(opts): Promise<void>` — `:307-348`
- `export async function attachStream(conversationId): Promise<boolean>` — `:355-393`

**Exact request body posted to `/api/chat`** — `:314-332`:

```js
{
  messages: turns.map(m => ({role: m.role, content: foldModelContent(m.content, m.meta?.pasted)}))
                 .filter(m => m.content),
  session_id: conversationId,
  conversation_id: conversationId,
  mode: prefs.salesforce ? 'salesforce' : 'assistant',
  model: prefs.model,            // 'smart' | 'fast'
  effort: prefs.effort,          // 'fast' | 'low' | 'medium' | 'high'
  agent: prefs.agent,            // boolean
  web_search: prefs.webSearch,   // 'off' | 'auto' | 'on'
  ...(image ? {image} : {}),
  ...(pdf ? {pdf, pdf_filename: pdfName ?? undefined} : {}),
}
```

Every literal union matches the orchestrator's Pydantic `Literal`s at
`orchestrator/app/main.py:191-199` (`mode`, `model`, `effort`, `web_search`) and
`frontend/lib/prefs.ts:20` (`WebSearchMode = 'off'|'auto'|'on'`). **No mismatch.**

**Control flow — `startStream` (`:307-348`)**
1. `:309` `register(conversationId, turns)` — **unconditionally overwrites any existing entry for
   this id** (`:292` `streams.set(...)`).
2. `:311-334` POST `/api/chat` with `signal: s.controller.signal`.
3. `:335-338` `!res.ok || !res.body` → `markUnreachable(s)` and return.
4. `:339` `await consume(s, res.body)`.
5. `:340-347` `AbortError` → `finalize(status:'stopped')`; anything else → `markUnreachable`.

**Control flow — `attachStream` (`:355-393`)**
1. `:356` early-return `true` if a stream for this id is already `streaming`.
2. `:362` seed from the cache; `:364` **`await getHistoryStore().load(id, {force:true})`** —
   server truth preferred (comment `:357-361` explains why cache-seeding destroyed threads).
3. `:369` `attachBaseTurns(base?.messages ?? [])` — everything up to and including the last user turn.
4. `:370` `register(...)`.
5. `:372-375` `GET /api/chat/attach/{id}` with the abort signal.
6. `:376-380` non-ok → `streams.delete(id)`, notify, return `false` (caller loads history).
7. `:381-382` `await consume(...)` → `true`.
8. `:383-392` `AbortError` → finalize `'stopped'`, return true; otherwise delete + return false.

**Control flow — `consume` (`:180-269`)** — `for await (const ev of readChatStream(body))`:
- `token` `:183-192`: on the first token, `settleReasoningClock` (`:185-187`); append text and
  clear `searchStatus` (`:188-192`).
- `status` `:193-194`: set `searchStatus`.
- `reasoning` `:195-200`: start the clock on the first delta (`:196`), append.
- `research` `:201-236`: start `researchStartedAt` (`:202`), compute `elapsedMs` (`:203`);
  `query` merges by identical query text rather than duplicating (`:206-215`); `reading`
  accumulates `+= count` (`:216-225`); `read` accumulates (`:227-235`).
- `step` `:237-238`: `mergeStep`.
- `meta` `:239-250`: settle the clock, set `research.active = false`, and store
  `foldStreamState(ev.meta, {reasoning, reasoningSeconds, steps, research})`.
- `error` `:251-255`: `sawTerminal = true`, `finalize({status:'error', errorMessage})`, `break`.
- `done` `:256-261`: `sawTerminal = true`, `finalize({status:'done'})`, `break`.
- `:262` `notify(conversationId)` after every event.
- `:264-268` if the body ended without a terminal frame, finalize as `'done'` anyway.

**State & side effects**
- **Module-level mutable state**: `streams` Map (`:50`) and `listeners` Set (`:51`) — both live
  for the lifetime of the browser tab.
- **localStorage writes** via `getHistoryStore().saveMessages(...)` at `:161` (`finalize`) and
  `:176` (`markUnreachable`); each of those also enqueues a background server push
  (`frontend/lib/history.ts:644-647`).
- **Network egress**: `POST /api/chat/stop` (`:87`), `GET /api/chat/active` (`:97`),
  `POST /api/chat` (`:311`), `GET /api/chat/attach/{id}` (`:372`).
- No GPU/model calls directly; those happen behind `/api/chat`.

**Dependencies** — inbound: `frontend/components/ChatApp.tsx` (`startStream` at `:456,466,525,578`;
`attachStream` at `:215,627`; `fetchServerActive` at `:182,210,282`), `frontend/tests/streams.test.ts`.
Outbound: `./history` (`getHistoryStore`, `newId`, `:21`), `./pasted` (`foldModelContent`, `:22`),
`./prefs` (`ChatPrefs` type, `:23`), `./sse` (`foldStreamState`, `mergeStep`, `readChatStream`,
`:24`), `./types` (`ChatMessage`, `:25`).

**Config** — none directly (all URLs are same-origin relative paths).

**Failure modes**
- `.catch(() => undefined)` at `:91` — a failed stop is invisible.
- `catch { return [] }` at `:103-105` — a failed active-poll is invisible.
- `catch {}` at `:366-368` — a failed force-load falls back to a possibly stale cache.
- `markUnreachable` (`:165-178`) is used for **every** non-2xx from `/api/chat`, including a
  400 "no user message" and a 502 wrapping an upstream 422. The message it persists
  (`:172-174`) claims the orchestrator is unreachable even when it answered.
- No timeout anywhere; a stream that never sends a terminal frame keeps `status: 'streaming'`
  forever (the spinner never clears and Composer stays disabled,
  `frontend/components/Composer.tsx:139`).

**Concurrency**
- `notify` iterates `[...listeners]` (`:61`) so a listener that unsubscribes during dispatch is safe.
- **Race 1 — `register` overwrite (`:292`).** `startStream` does not check for an existing
  stream. If two `startStream` calls land on the same `conversationId`, the first `LiveStream`
  object is still referenced by its running `consume()` loop; its `controller` is never aborted.
  When the orchestrator cancels the previous generation
  (`orchestrator/app/main.py:348-350`) that first body ends without a terminal frame, hits the
  `!sawTerminal` branch (`:264-268`) and calls `finalize` → `saveMessages(conversationId,
  <its own older message list>)`, clobbering the newer turns in the cache. The server-side
  409-on-shrink guard (`orchestrator/app/history.py:151-186`) prevents permanent loss, but the
  local view is wrong until a forced reload.
- **Race 2 — `attachStream` double-register.** The guard at `:356` is checked *before* the
  `await` at `:364`. Two concurrent calls (the 8 s poll at `frontend/components/ChatApp.tsx:301`
  firing while a previous attach is still awaiting `load(force:true)`) both pass, both
  `register`, and both open an SSE reader on the same generation —
  `LiveGeneration.follow()` supports multiple subscribers
  (`orchestrator/app/main.py:105-120`). Both then `finalize` and `saveMessages`.
- `consume` mutates `s.messages` by reallocation (`updateAssistant` at `:137-143` uses `.map`),
  so readers never see a torn array — but two `LiveStream` objects for one id write to the same
  history key.

**Complexity hotspots** — `consume` `:180-269` = **90 LOC**, an 8-arm if/else-if chain with a
3-branch nested reducer for `research` (`:204-236`). Cyclomatic complexity well above 10.

**Notable**
- `stopStream` sends both `conversation_id` and `session_id` set to the same id (`:90`) —
  defensive against the orchestrator's `conversation_id or session_id` key resolution
  (`orchestrator/app/main.py:716`).
- `messagesDiscardedByRegenerate` (`:124-131`) exists purely to drive a confirmation dialog;
  its docstring at `:118-123` documents the data-loss it guards.
- No TODO/FIXME/HACK.

---

## 15. `frontend/lib/orchestrator.ts`  (101 LOC)

**Purpose** — pure contract translation between the frontend's `/api/chat` body and the
orchestrator's `POST /chat` body. Kept pure so it is unit-tested
(`frontend/tests/chat-contract.test.ts`, referenced at `:12`).

**Public surface**
- `export interface ChatRequestBody` — `:16-31`:
  `messages?`, `session_id?`, `image?`, `conversation_id?`, `mode?`, `model?`, `effort?`,
  `agent?`, `pdf?`, `pdf_filename?`, `web_search?`
- `export interface OrchestratorChatRequest` — `:34-48`:
  `message: string` (**required**), `messages?`, `session_id: string`,
  `image_base64: string | null`, plus the same optionals
- `export const IMAGE_ONLY_PROMPT = 'Analyze the attached image.'` — `:54`
- `export const PDF_ONLY_PROMPT = 'Read this document and summarize the key points.'` — `:55`
- `export function lastUserContent(body: ChatRequestBody): string` — `:58-63`
- `export function toOrchestratorChatRequest(body): OrchestratorChatRequest | null` — `:70-101`

**Control flow — `toOrchestratorChatRequest`**
1. `:73` `text = lastUserContent(body).trim()` — the **last** message with `role === 'user'`
   (`:59-62` reverses then finds).
2. `:74-75` pull `image` and `pdf`, defaulting to `null`.
3. `:76-77` `message = text || (image ? IMAGE_ONLY_PROMPT : pdf ? PDF_ONLY_PROMPT : '')`.
4. `:78` empty → **return null** (the caller turns this into a 400,
   `frontend/app/api/chat/route.ts:145-150`).
5. `:79-100` build the payload: always `message`, `session_id` (default `'default'`, `:85`),
   `image_base64` (`:86`); conditionally spread `messages` when non-empty (`:82-84`),
   `conversation_id`/`mode`/`model`/`effort`/`agent` only when `!== undefined` (`:89-95`),
   `pdf` + `pdf_filename` when a pdf is present (`:97`), `web_search` when defined (`:99`).

**Contract cross-check against `orchestrator/app/main.py:176-238`**
- `ChatRequest.text` (`:206-212`) prefers `message` when non-blank, else the last non-blank user
  message — consistent with the `message` this module always sets.
- `ChatRequest.image_data` (`:215-216`) is `image_base64 or image` — this module sets
  `image_base64`. ✔
- `ChatRequest.history_messages` (`:219-231`) drops the trailing user turn, so sending both
  `message` and the full `messages` array does **not** duplicate the current question. ✔
- `_require_input` (`:233-238`) raises when there is no text, image or pdf — this module's
  `return null` at `:78` pre-empts that, so the 422 never reaches the browser. ✔

**State & side effects** — none. Pure functions.

**Dependencies** — inbound: `frontend/app/api/chat/route.ts:13-17`,
`frontend/tests/websearch.test.ts`, `frontend/tests/chat-contract.test.ts`. Outbound: none.

**Config** — none.

**Failure modes** — none raise. `body.messages` may be `undefined` (`:60` defaults to `[]`).
A `messages` array containing non-string `content` would produce `undefined` on `.trim()` —
but the type is `string` and the value comes from `frontend/lib/streams.ts:318` where it is
always a string.

**Concurrency** — pure/sync.

**Complexity hotspots** — none (`toOrchestratorChatRequest` is 32 LOC).

**Notable** — the docstring at `:6-8` still says the orchestrator expects
`{message: str (min_length=1), session_id, image_base64?}`, but the real model
(`orchestrator/app/main.py:185`) is `message: Optional[str] = None` with a
`@model_validator`. Documentation drift, not a behavioural bug. **No `image`-size or
`pdf`-size validation happens anywhere in this module or its caller.**

---

## 16. `frontend/lib/proxy.ts`  (64 LOC)

**Purpose** — the shared server-side forwarder for `/api/history/*`. Relays cookies both ways.

**Public surface**
- `export function orchestratorUrl(): string` — `:9-11`
- `function setCookiesOf(headers: Headers): string[]` — `:14-19` (private; prefers undici's
  `getSetCookie()`, falls back to a single `get('set-cookie')`)
- `export async function proxyToOrchestrator(req: Request, upstreamPath: string): Promise<Response>` — `:21-64`

**Control flow**
1. `:25-29` build the forwarded header set: **only `cookie` and `content-type`.** Every other
   inbound header (`authorization`, `accept`, `user-agent`, `x-*`) is dropped.
2. `:33-42` `fetch(`${orchestratorUrl()}${upstreamPath}`, {method: req.method, headers,
   body: GET/HEAD ? undefined : await req.text(), cache:'no-store', redirect:'manual'})`.
3. `:43-48` fetch threw → 502 `{message:'The orchestrator is unreachable.'}`.
4. `:50-58` build the response headers: `content-type` from upstream or `application/json`
   (`:51-54`), `cache-control: no-store` (`:55`), then **append every upstream `Set-Cookie`
   verbatim** (`:56-58`).
5. `:60-63` `new Response(await upstream.arrayBuffer(), {status: upstream.status, headers})`.

**State & side effects** — network egress to `ORCHESTRATOR_URL + upstreamPath`. **No** DB/FS
access of its own.

**Dependencies** — inbound: `frontend/app/api/history/[...path]/route.ts:10` — **this is the
only consumer.** The docstring at `:1-2` claims it also serves `/api/auth/*`, which is false:
`frontend/app/api/auth/me/route.ts:18` uses a raw `fetch`. Outbound: `fetch`.

**Config** — `process.env.ORCHESTRATOR_URL` at `:10`.

**Failure modes**
- Bare `catch {}` at `:43`.
- `await req.text()` (`:39`) **buffers the entire request body**. The largest body that flows
  through here is `PUT /history/conversations/{id}/messages`, which carries a whole conversation
  thread including every message's `meta` — and `meta.data` holds SQL result rows
  (`frontend/lib/types.ts:105`) and `meta.chart_data` (`:113`). No size bound.
- `await upstream.arrayBuffer()` (`:60`) **buffers the entire response body** — same concern in
  the other direction for `GET /history/conversations/{id}`.
- No timeout, no retry.
- `redirect: 'manual'` (`:41`) means a 3xx is returned to the browser with its `Location`
  header **dropped** (only `content-type`, `cache-control` and `set-cookie` are copied), so a
  redirect becomes an opaque empty response.

**Concurrency** — async, stateless.

**Complexity hotspots** — none.

**Notable**
- Set-Cookie is reflected verbatim (`:56-58`). No upstream currently sets one (§0.1), so this is
  dormant — but it is an unfiltered cookie-injection channel if the orchestrator ever does.
- Response status is preserved (`:61`), unlike every other route in the tree which flattens
  upstream failures to 502. This is the only route family where a client sees the real 409/422.

---

## 17. `frontend/lib/history.ts`  (851 LOC)

**Purpose** — conversation store. Synchronous `HistoryStore` interface over a localStorage
cache, with background push/pull against `/api/history/*`.

**Public surface**
- `const STORAGE_KEY = 'techsara.history.v1'` — `:40`; `const SYNC_KEY = 'techsara.history.sync.v1'` — `:41`; `const TITLE_MAX = 40` — `:42`
- `export interface StorageLike {getItem, setItem, removeItem}` — `:45-49`
- `export interface HistoryStore` — `:51-66`: `list()`, `listArchived()`, `get(id)`,
  `create(firstMessage)`, `rename(id,title)`, `remove(id)`, `saveMessages(id,messages)`,
  `setPinned(id,pinned)`, `setArchived(id,archived)`
- `export interface ServerHistoryStore extends HistoryStore` — `:69-104`: `setActiveUser`,
  `migrateLocalConversations`, `refresh`, `refreshArchived`, `load(id,{force?})`,
  `exportMarkdown(id)`, `truncateMessages(id,keep)`, `flush()`
- `export function titleFromFirstMessage(text: string): string` — `:106-112`
- `export function newId(): string` — `:114-119` (`crypto.randomUUID()` with a
  `c-<base36>-<random>` fallback)
- `function isQuotaError(err): boolean` — `:121-130` (private)
- `interface Cache {readAll, writeAll, clear}` — `:134-138` (private)
- `function createCache(storage, onEvict?): Cache` — `:140-190` (private)
- `function summarize(conversations, wantArchived): ConversationSummary[]` — `:198-220` (private)
- `function storeOverCache(cache): HistoryStore` — `:222-290` (private)
- `export function createHistoryStore(storage, onEvict?): HistoryStore` — `:296-301`
- `interface SyncState {username?, migrated?, pushed, dirty, deleted}` — `:305-320` (private)
- `function sameIds(a, msgs)` — `:322-324`; `function isPrefix(prefix, msgs)` — `:326-331` (private)
- `export interface ServerHistoryStoreOptions {storage, api?, onEvict?}` — `:333-337`
- `export function createServerHistoryStore(options): ServerHistoryStore` — `:339-808` (**470 LOC**)
- `export function setEvictListener(fn): void` — `:815-819`
- `const NOOP_STORAGE: StorageLike` — `:821-825`
- `export function getHistoryStore(): ServerHistoryStore` — `:827-851`

**Control flow — cache (`createCache`, `:140-190`)**
1. `readAll` `:145-153`: parse `STORAGE_KEY`; a corrupt payload returns `[]` (`:151-152`).
2. `writeAll` `:157-180`: try `setItem`; on a quota error, find the oldest by `updatedAt`
   (`:165-170`), splice it out (`:171`), fire `onEvict` (`:172-177`), retry. Loops until it fits
   or the list is empty (`:164`).
3. `clear` `:182-188`: `removeItem` inside try/catch.

**Control flow — `createServerHistoryStore` (`:339-808`)**
1. `:342-344` build cache, the local (v1) store over it, and the `HistoryApi`.
2. `:347-348` `chains: Map<string, Promise<void>>` and `inFlight: Set<Promise<void>>` —
   per-instance mutable state.
3. `readSync`/`mutateSync` `:350-378` read and write `SYNC_KEY`; both swallow errors.
4. `mergeServerRows` `:407-450`: unknown ids are added with `messages: []` and marked
   `pushed[id] = 'unknown'` (`:413-425`); known non-dirty ids adopt the server's title (`:429-432`),
   a *newer* `updated_at` only (`:433-437`), and both flags (`:438-446`).
5. `pushAll` `:466-498`: `api.create` (409 tolerated, `:470-473`) → `api.replaceMessages`; a 409
   means the server has **more** messages, so it pulls server truth instead of overwriting
   (`:479-488`) → re-apply flags (`:491-493`) → record `pushed` ids (`:494-497`).
6. `syncConversation` `:501-525`: if `pushed` is a strict prefix of the local messages, append
   only the delta (`:511-520`); otherwise `pushAll` (`:524`).
7. `pushPatch` `:531-539`: `api.update`, and on 404 fall back to `pushAll`.
8. `loadConversation` `:542-591`: three skip conditions (`:549-559`), then `api.get` (`:561`);
   **`force` + a shorter server thread keeps the local copy** (`:562-564`); server messages get
   synthetic ids `srv-<id>-<i>` (`:567`) and synthetic `createdAt` values
   `now - (len - i)` (`:572`); the result is upserted and `pushed` recorded (`:583-586`);
   `catch { return cached }` at `:588-590`.
9. `enqueue` `:593-599`: chains per-conversation pushes; **`.catch(() => markDirty(id))`** at
   `:595` — every background sync failure is swallowed into a dirty flag.
10. Returned object `:601-807`. Notable members: `remove` (`:621-642`) skips the DELETE when the
    conversation was never pushed; `truncateMessages` (`:651-666`) calls the **server first**
    (`:659`) and only then shrinks locally; `setActiveUser` (`:687-713`) **clears the entire
    cache** when the username changes (`:693`); `refresh` (`:730-789`) is a 4-phase reconcile.

**Control flow — `refresh` (`:730-789`, 60 LOC)**
1. `:733-742` replay pending deletes (404 tolerated).
2. `:746-750` re-push every dirty conversation and re-apply its flags.
3. `:754-765` fetch the active list and the archived list; a network failure on the archived
   fetch re-throws (`:764`), a server rejection is tolerated as "pre-V3 backend".
4. `:769-782` for each local conversation absent from the server: if it was previously pushed →
   delete locally (`:772-777`); otherwise push it (`:780`).
5. `:784` `mergeServerRows(serverList)`; `:786-788` `catch { return false }`.

**State & side effects**
- **localStorage writes**: `STORAGE_KEY` (`:161`) and `SYNC_KEY` (`:374`, `:696-703`).
- **localStorage removal**: `:184` (`clear`), reached from `setActiveUser` (`:693`).
- **Network egress**: everything through `HistoryApi` → `/api/history/conversations*` (see §18).
- **Module-level mutable state**: `browserStore` (`:812`) and `evictListener` (`:813`).

**Dependencies** — inbound: `frontend/lib/streams.ts:21`, `frontend/components/ChatApp.tsx`,
`frontend/lib/prefs.ts`, `frontend/tests/history-server.test.ts`, `frontend/tests/prefs.test.ts`,
`frontend/tests/history.test.ts`. Outbound: `./types` (`:18-23`), `./historyApi` (`:24-34`),
`./exportMarkdown` (`:36-39`).

**Config** — none (relative URLs only).

**Failure modes**
- Swallowed: `:151-152` (corrupt cache), `:185-187` (storage unavailable), `:365-367` (corrupt
  sync state), `:375-377` (sync write), `:595` (**every** background push failure),
  `:588-590` (load failure), `:705-707` (sync write on user change), `:786-788` (whole refresh),
  `:794-797` (archived refresh).
- `writeAll` (`:157-180`) can loop while evicting; the loop terminates only when the payload
  fits or the array is empty (`:164`). A single conversation larger than the quota re-throws.
- **Silent data loss**: `onEvict` fires (`:172`) but the eviction is not undoable and the
  conversation is not re-fetchable if the server never had it.
- `refresh` returning `false` (`:787`) is indistinguishable between "offline" and "server
  rejected everything".

**Concurrency**
- `enqueue` (`:593-599`) serializes per conversation but **not** across conversations, and the
  chain is never pruned — `chains` grows monotonically for the tab's lifetime (a slow leak).
- `saveMessages` → `enqueue(syncConversation)` (`:644-647`) races with `loadConversation`; the
  server's 409-on-shrink is the real guard.
- `getHistoryStore()` (`:827-851`) memoizes only in the browser branch (`:844-849`); the SSR
  branch (`:828-843`) **constructs a brand-new store on every call**.
- `readSync()` is called inside loops (`:427`, `:719`, `:746`, `:772`) — each call does a
  `JSON.parse` of the whole sync blob.

**Complexity hotspots**
- `createServerHistoryStore` `:339-808` = **470 LOC** — the largest function in the assignment.
- `refresh` `:730-789` = **60 LOC**, 4 phases, 3 nested loops, 2 try/catch levels.
- `loadConversation` `:542-591` = **50 LOC**, 5 early-return conditions.
- `pushAll` `:466-498` = **33 LOC**, 2 nested try/catch with error-class dispatch.

**Notable**
- Magic numbers/strings: `TITLE_MAX = 40` (`:42`), `STORAGE_KEY`/`SYNC_KEY` (`:40-41`),
  `1e11` seconds-vs-ms threshold lives in `historyApi.ts:43` not here.
- `newId()` fallback (`:118`) uses `Math.random()` — non-cryptographic, but it is only a cache
  key and the server validates the format (`orchestrator/app/history.py:34`).
- The long comment at `:452-465` documents a **previously shipped data-destroying bug**
  (delete-and-recreate) and the 409 guard that replaced it. Worth preserving in the architecture doc.
- No TODO/FIXME/HACK.

---

## 18. `frontend/lib/historyApi.ts`  (256 LOC)

**Purpose** — typed fetch client for `/api/history/*`, with an injectable `fetch` so the sync
logic is testable offline.

**Public surface**
- `export interface ServerConversationSummary {id, title, updated_at?, created_at?, pinned?, archived?}` — `:9-17` (the optional fields are typed `unknown`)
- `export interface ServerMessage {role, content, meta?}` — `:19-23`
- `export interface ServerConversation {id, title, messages}` — `:25-29`
- `export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>` — `:31-34`
- `export function toEpoch(value: unknown, fallback: number): number` — `:41-53`
- `export class HistoryApiError extends Error {status: number}` — `:56-64`
- `export function isNotFound(err): boolean` — `:66-68` (status 404)
- `export function isConflict(err): boolean` — `:71-73` (status 409)
- `export function isUnreachable(err): boolean` — `:76-78` (**not a HistoryApiError, or status 0**)
- `export interface ConversationPatch {title?, pinned?, archived?}` — `:81-85`
- `export interface ListOptions {archived?: boolean}` — `:87-90`
- `export interface HistoryApi` — `:92-115`: `list`, `get`, `create`, `update`, `remove`,
  `appendMessage`, `replaceMessages`, `truncateMessages`
- `const BASE = '/api/history/conversations'` — `:117`
- `export function createHistoryApi(fetchFn?: FetchLike): HistoryApi` — `:119-192`
- `export interface ServerSearchResult {id, title, updated_at?, pinned?, archived?, snippet?, matched_in?}` — `:197-206`
- `export interface SearchOptions {limit?, signal?, fetchFn?}` — `:208-213`
- `export async function searchConversations(query, options): Promise<unknown>` — `:226-256`

**Exact URLs and bodies emitted** (`createHistoryApi`, `:155-191`)

| method | URL | body | maps to |
|---|---|---|---|
| `list({archived})` `:156-160` | `GET /api/history/conversations` + `?archived=true` when set | — | `orchestrator/app/history.py:80` |
| `get(id)` `:161-169` | `GET /api/history/conversations/{enc(id)}` | — | `:105` |
| `create(id,title)` `:170-172` | `POST /api/history/conversations` | `{id,title}` or `{title}` | `:88` |
| `update(id,patch)` `:173-175` | `PUT /api/history/conversations/{enc(id)}` | `ConversationPatch` | `:116` |
| `remove(id)` `:176-178` | `DELETE /api/history/conversations/{enc(id)}` | — | `:257` |
| `appendMessage(id,m)` `:179-181` | `POST /api/history/conversations/{enc(id)}/messages` | `{role,content,meta}` | `:134` |
| `replaceMessages(id,ms)` `:182-184` | `PUT /api/history/conversations/{enc(id)}/messages` | `{messages}` | `:151` |
| `truncateMessages(id,keep,expectedTotal)` `:185-190` | `POST /api/history/conversations/{enc(id)}/truncate` | `{keep, expected_total}` | `:200` |
| `searchConversations(q,{limit})` `:226-256` | `GET /api/history/search?q=&limit=` | — | `:266` |

All of these are ≤ 3 path segments after `conversations`, so they pass the proxy allowlist at
`frontend/app/api/history/[...path]/route.ts:27`. **Verified: every client call is reachable.**

**Control flow — `request` (`:122-153`)**
1. `:129-138` `doFetch(BASE + path, {method, cache:'no-store', …body})`.
2. `:139-141` fetch threw → `throw new HistoryApiError(0, 'History server unreachable.')`.
3. `:142-147` `!res.ok` → `HistoryApiError(res.status, 'History request failed with status N.')`
   — **the upstream `detail` body is discarded**, so a 409 "refusing to shrink conversation from
   12 to 3 messages" reaches the caller as just the number 409.
4. `:148-152` parse JSON; a non-JSON body returns `null`.

**Control flow — `toEpoch` (`:41-53`)**
1. `:42-44` finite number → `value < 1e11 ? value*1000 : value` (seconds vs milliseconds).
2. `:45-51` string → if it already has a `Z`/offset use it, else `value.replace(' ','T') + 'Z'`
   (SQLite naive-UTC `CURRENT_TIMESTAMP`); `Date.parse`.
3. `:52` fallback.

**Control flow — `searchConversations` (`:226-256`)**
1. `:230-231` build `?q=` + optional `limit`.
2. `:237-240` `GET /api/history/search?…` with `cache:'no-store'` and the abort signal.
3. `:241-244` an `AbortError` is **rethrown unchanged** so the caller can distinguish
   "superseded" from "failed"; anything else becomes `HistoryApiError(0, …)`.
4. `:245-250` non-ok → `HistoryApiError(res.status, …)`.
5. `:251-255` JSON, or `null`.

**State & side effects** — network egress to the same-origin `/api/history/*` paths only.

**Dependencies** — inbound: `frontend/components/SearchPalette.tsx`, `frontend/lib/history.ts:24-34`,
`frontend/lib/searchPalette.ts`, `frontend/tests/history-server.test.ts`. Outbound: `./types` (`Meta`, `:7`).

**Config** — none.

**Failure modes**
- **No timeout on any request.** `createHistoryApi`'s `request` (`:129`) passes no signal at all
  — only `searchConversations` supports abort (`:239`).
- `catch { return null }` at `:151-152` and `:253-254` conflate "204 No Content" with "malformed JSON".
- `isUnreachable` (`:76-78`) returns `true` for **any non-`HistoryApiError` throwable**,
  including a `TypeError` from a coding bug. In `frontend/lib/history.ts:764` that causes the
  whole `refresh()` to abort, so a programming error masquerades as "offline".
- `searchConversations` returns `Promise<unknown>` (`:229`) — the response is entirely unvalidated
  and is narrowed downstream in `frontend/lib/searchPalette.ts`.

**Concurrency** — stateless; the injected `fetchFn` closure is the only captured state.

**Complexity hotspots** — none (`createHistoryApi` is 74 LOC but is a flat object literal of
9 one-line methods; `request` is 32 LOC).

**Notable**
- `ServerConversationSummary`'s timestamp/flag fields are typed `unknown` (`:12-16`) precisely
  because the backend returns SQLite `0/1` for booleans — handled at
  `frontend/lib/history.ts:419-420,441` (`value === true || value === 1`).
- The comment at `:218-224` explains why `searchConversations` is deliberately outside
  `HistoryApi`.
- No TODO/FIXME/HACK.

---

## 19. `frontend/lib/contextMeter.ts`  (147 LOC)

**Purpose** — pure maths for the context-usage ring and its popover.

**Public surface**
- `export function latestUsage(messages: ChatMessage[]): ContextUsage | null` — `:24-30`
- `export type MeterState = 'calm'|'warn'|'high'|'critical'` — `:32`
- `export const WARN_AT = 0.6` — `:34`; `HIGH_AT = 0.85` — `:35`; `PULSE_AT = 0.95` — `:36`
- `export const DEFAULT_RESERVED_OUTPUT = 8192` — `:45`
- `export const DEFAULT_USABLE_BUDGET = 131072 - 8192 - 512` (= 122368) — `:46`
- `export function estimateDraftTokens(text: string): number` — `:49-51` (`ceil(len/4)`)
- `export function meterState(fraction): MeterState` — `:53-59`
- `export function meterColor(state): string` — `:62-72` (CSS custom properties)
- `export function meterPercent(fraction): number` — `:74-77`
- `export interface MeterView {fraction, percent, state, pulsing, tokensUsed, usableBudget, breakdown}` — `:79-87`
- `export function meterView(usage, draft): MeterView` — `:95-113`
- `export function buildBreakdown(usage, draftTokens): {label,tokens,heldBack?}[]` — `:120-138`
- `export function breakdownTotal(rows): number` — `:141-147`

**Control flow**
- `latestUsage` `:25-29`: scan messages backwards for the first `meta.context`.
- `meterView` `:99-112`: `draftTokens` (`:99`) → `usable = usage?.usable_budget || DEFAULT`
  (`:100`, note `||` not `??`, so a server-reported `0` falls back) → `used = tokens_used +
  draftTokens` (`:101`) → `fraction` guarded against `usable <= 0` (`:102`).
- `buildBreakdown` `:125-137`: three rows; "Reserved for reply" is marked `heldBack: true`
  (`:134`) and the whole list is `.filter(row => row.tokens > 0)` (`:137`).
- `breakdownTotal` `:145-146`: sums only non-`heldBack` rows.

**Cross-check with the orchestrator** — `DEFAULT_RESERVED_OUTPUT = 8192` matches
`orchestrator/app/config.py:128` (`MODEL_MAX_OUTPUT`, default 8192); the `512` matches
`orchestrator/app/config.py:131` (`CONTEXT_SAFETY_MARGIN`, default 512); `131072` matches the
main model window described at `orchestrator/app/context.py:4`. **Consistent today**, but these
are three environment-overridable server values hardcoded in the browser — see Notable.

**State & side effects** — none. Pure.

**Dependencies** — inbound: `frontend/components/ChatApp.tsx`, `frontend/components/ContextMeter.tsx`,
`frontend/tests/contextMeter.test.ts`. Outbound: `./types` (`ChatMessage`, `ContextUsage`, `:14`).

**Config** — none read at runtime; the three constants at `:45-46` **duplicate** server config.

**Failure modes** — none raise. `meterState` (`:54`) and `meterPercent` (`:75`) both guard
`!Number.isFinite`. `estimateDraftTokens` guards `text` being falsy (`:50`).

**Concurrency** — pure/sync.

**Complexity hotspots** — none (largest function is `meterView` at 19 LOC).

**Notable**
- Magic numbers: `0.6`/`0.85`/`0.95` (`:34-36`), `8192`/`131072`/`512` (`:45-46`), `4`
  chars-per-token (`:50`).
- If an operator sets `MODEL_MAX_OUTPUT=32768` or runs a model with a different window, the
  meter's pre-first-reply reading is wrong until the first `meta.context` arrives. There is no
  endpoint that exposes the server's real budget for the first request.
- The comment at `:129-136` documents a **shipped bug that was fixed**: the reserved-output row
  used to be summed in, making the popover read 16,747 while the ring read 3%.
- No TODO/FIXME/HACK.

---

## 20. `frontend/lib/auth.ts`  (29 LOC)

**Purpose** — the residue of the removed login: fetch the local username.

**Public surface**
- `export type FetchLike = typeof fetch` — `:11`
- `export type MeResult = {ok: true; username: string} | {ok: false; status: number}` — `:13-15`
- `export async function fetchMe(fetchFn: FetchLike = fetch): Promise<MeResult>` — `:18-29`

**Control flow**
1. `:20` `fetchFn('/api/auth/me', {cache: 'no-store'})`.
2. `:21` `!res.ok` → `{ok: false, status: res.status}`.
3. `:22-25` require `typeof body.username === 'string'`, else `{ok:false, status: res.status}`
   (i.e. a 200 with a bad shape reports `status: 200` with `ok: false`).
4. `:26-28` throw → `{ok: false, status: 0}` — "status 0 means network failure" (`:17`).

**State & side effects** — one same-origin GET. No writes.

**Dependencies** — inbound: `frontend/components/ChatApp.tsx` only. Outbound: global `fetch`.

**Config** — none.

**Failure modes** — bare `catch {}` at `:26`. No timeout. No retry.

**Concurrency** — async, stateless.

**Complexity hotspots** — none.

**Notable** — the module docstring (`:1-9`) is the clearest in-repo statement of the security
posture: *"There is no sign-in, no sign-up, no session cookie and no route gating: this app runs
as a single local user."* The username's only remaining job is scoping the localStorage cache
(`frontend/lib/history.ts:687-713`), which is why the endpoint survives.

---

## 21. `frontend/lib/types.ts`  (262 LOC)

**Purpose** — the shared type surface, and the frontend's declaration of the `meta` contract.

**Public surface (all `export`)**
- `type Engine` — `:8`: `'sql'|'rag'|'vision'|'report'|'chat'|'agent'|'search'|'url'|'repo'`
- `type ModelChoice = 'smart'|'fast'` — `:15`
- `type ReasoningEffort = 'fast'|'low'|'medium'|'high'` — `:22`
- `type ChatMode = 'salesforce'|'assistant'` — `:25`
- `interface AgentStep {id: number; title: string; status: 'running'|'done'|'failed'; detail?: string}` — `:28-33`
- `type ChartType` — `:39-48`: `bar|line|area|pie|scatter|horizontal_bar|donut|funnel|histogram`
- `interface ChartSpec {type, x_key, y_keys, title, stacked, bins?, show_legend?, show_values?}` — `:56-66`
- `interface Citation {record_id, object, url}` — `:68-72`
- `interface ReportFile {filename, type, size?}` — `:74-78`
- `type DataRow = Record<string, unknown>` — `:80`
- `interface PastedText {id, content, lines, chars}` — `:87-92`
- `interface Meta` — `:101-152`: `route`, `sql?`, `data?`, `truncated?`, `chart?`, `chart_data?`,
  `citations?`, `report_files?`, `mode?`, `model?`, `effort?`, `steps?`, `reasoning?`,
  `reasoning_seconds?`, `pasted?`, `sources?`, `search_unavailable?`, `code_sources?`,
  `generation_id?`, `input_trimmed?`, `context?`, `research?`
- `interface ContextUsage {tokens_used, usable_budget, window, reserved_output, fraction, summarized_turns, compacted?}` — `:154-167`
- `interface CodeSource {path, start_line, end_line, snippet}` — `:170-175`
- `interface WebSource {n, title, url, domain}` — `:178-183`
- `interface ResearchResult {title, url, domain}` — `:186-190`
- `interface ResearchQuery {query, results}` — `:193-196`
- `interface Research {queries, reading?, read?, elapsedMs?, active?}` — `:203-213`
- `type MessageStatus = 'streaming'|'done'|'stopped'|'error'` — `:215`
- `interface ChatMessage` — `:217-240`: `id`, `role`, `content`, `meta?`, `status?`,
  `errorMessage?`, `imageDataUrl?`, `pdfName?`, `reasoning?`, `reasoningSeconds?`, `steps?`,
  `searchStatus?`, `research?`, `createdAt`
- `interface Conversation {id, title, createdAt, updatedAt, pinned?, archived?, messages}` — `:242-252`
- `interface ConversationSummary {id, title, createdAt, updatedAt, pinned?, archived?}` — `:254-262`

**Control flow / State & side effects** — none; declaration-only module, zero runtime output.

**Dependencies** — inbound: 37 modules (17 components, 10 tests, 10 libs) per
`rg -ln "from '@/lib/types'|from './types'"`. Outbound: none.

**Config** — none.

**Failure modes** — none at runtime. **The risk is that these are compile-time-only
assertions**: `frontend/lib/sse.ts:203` casts the parsed `meta` straight to `Meta` with no
runtime check, so every field here is an unverified assumption about the wire.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Notable**
- **`Engine` (`:8`) is missing `'dataset'`**, which `orchestrator/app/engines/dataset.py:101,118`
  emits. Confirmed mismatch.
- `Meta` has no `auto` key despite `orchestrator/app/main.py:378-379` setting one.
- `ReportFile` (`:74-78`) uses `filename`/`type`/`size`; the orchestrator's `/reports` listing
  uses `filename`/`size_bytes`/`modified` (`orchestrator/app/core/report_paths.py:62-66`) and
  `MOCK_REPORTS` uses `name`/`size`/`mtime`/`type` (`frontend/lib/fixtures.ts:379-397`).
  **Three different shapes for the same concept.**
- `ModelChoice` (`:15`) is documented at `:11-14` as vestigial ("There is now ONE
  (Qwen3.6-35B-A3B)") but is still sent on every request (`frontend/lib/streams.ts:321`) and
  still validated upstream (`orchestrator/app/main.py:192`).
- No TODO/FIXME/HACK.

---

## 22. `frontend/lib/errors.ts`  (104 LOC)

**Purpose** — turn a raw engine/model error string into a plain-language sentence plus a
disclosure with the original.

**Public surface**
- `export function trimNotice(info: {dropped_turns: number; clipped_messages: number}): string` — `:19-38`
- `export interface FriendlyError {message: string; detail: string | null}` — `:40-45`
- `export function extractUpstreamMessage(raw: string): string | null` — `:48-54`
- `const CONTEXT_OVERFLOW = /maximum context length|context length is|too many tokens/i` — `:56`
- `const CONNECTION = /connection|unreachable|refused|timeout|timed out|ECONN/i` — `:57`
- `const OUT_OF_MEMORY = /out of memory|CUDA|OOM/i` — `:58`
- `const NOT_FOUND_MODEL = /model .* does not exist|not found/i` — `:59`
- `export function friendlyError(raw?: string | null): FriendlyError` — `:61-104`

**Control flow — `friendlyError`**
1. `:62-65` empty input → `{message: 'The engine reported an error.', detail: null}`.
2. `:66` `upstream = extractUpstreamMessage(text) ?? text`.
3. `:68-74` `CONTEXT_OVERFLOW` → "This conversation is too long for the selected model. Switch
   the model picker to Smart, or start a new chat."
4. `:75-81` `CONNECTION` → "The model server did not respond…"
5. `:82-88` `OUT_OF_MEMORY` → "The model server ran out of memory on this request…"
6. `:89-95` `NOT_FOUND_MODEL` → "The selected model is not available on this machine right now."
7. `:99-103` fallback: show the isolated sentence if one was extracted, keeping the full payload
   as `detail`.

**Control flow — `extractUpstreamMessage` (`:48-54`)** — two alternative regexes for
double-quoted (`:51`) then single-quoted (`:52`) `"message": "..."`, then unescape `\\(.)`
(`:53`). Handles both JSON and Python-repr dicts.

**Control flow — `trimNotice` (`:19-38`)** — four branches on `(clipped > 0, dropped > 0)`,
with correct singular/plural handling (`:26`, `:34`).

**State & side effects** — none. Pure.

**Dependencies** — inbound: `frontend/components/MessageRow.tsx`, `frontend/tests/errors.test.ts`.
Outbound: none.

**Config** — none.

**Failure modes** — none raise. **Classification bugs:**
- `NOT_FOUND_MODEL` (`:59`) — the alternation's second arm is a bare `not found`, so **any**
  error containing that phrase is labelled "The selected model is not available on this machine
  right now." The orchestrator emits `"conversation not found"` at
  `orchestrator/app/main.py:344,760`, `orchestrator/app/uploads.py:78` and
  `orchestrator/app/history.py:77`, and `"report not found"` at
  `orchestrator/app/main.py:269`. Every one of those would be mislabelled.
- `CONNECTION` (`:57`) matches a bare `timeout`, so a DuckDB or Salesforce query timeout is
  reported as "The model server did not respond."
- Ordering matters: `CONTEXT_OVERFLOW` is tested first, so a "context length" message inside a
  connection error wins.

**Concurrency** — pure/sync.

**Complexity hotspots** — `friendlyError` `:61-104` = 44 LOC, 6 branches — under threshold.

**Notable**
- The `CONTEXT_OVERFLOW` copy at `:71-72` tells the user to *"Switch the model picker to
  Smart"*, but `frontend/lib/types.ts:11-14` records that the picker no longer chooses a model —
  it chooses effort, and there is only one model. **Stale, unactionable user-facing advice.**
- The regexes are unanchored and case-insensitive; `/CUDA/i` (`:58`) will match the substring
  "cuda" anywhere, including a file path.
- No TODO/FIXME/HACK.

---

## 23. Consolidated route table (the API-contracts deliverable)

| # | Method | Path | Auth | Request | Success | Errors |
|---|---|---|---|---|---|---|
| 1 | GET | `/api/auth/me` | none | — | 200 `{username, local}` | 502×2 |
| 2 | POST | `/api/chat` | none | `ChatRequestBody` JSON | 200 `text/event-stream` | 400×2, 502×2 |
| 3 | GET | `/api/chat/active` | none | — | 200 `{active: string[]}` | none (failures → 200 `{active:[]}`) |
| 4 | GET | `/api/chat/attach/{id}` | none | — | 200 `text/event-stream` | 400×2, 404×2, 502×2 |
| 5 | POST | `/api/chat/compact` | none | `{conversation_id, messages[]}` | 200 `{compacted, folded_turns?, covers_through?, reason?}` | upstream status passthrough, 502 |
| 6 | POST | `/api/chat/stop` | none | `{conversation_id, session_id}` | 200 `{stopped: bool}` | upstream status passthrough, 502 |
| 7 | GET/POST/PUT/DELETE | `/api/history/{...path}` | none | see §7 table | upstream status + body verbatim | 404 (not allowlisted), 502; upstream 400/404/409/422 pass through |
| 8 | GET | `/api/reports` | none | — | 200 `{reports:[…]}` (real) / bare array (mock) | 502×2 — **no caller** |
| 9 | GET | `/api/reports/{filename}` | none | — | 200 file stream | 400, 404, 502×2, **uncaught 500** |
| 10 | POST | `/api/upload` | none | `multipart/form-data` `{file, conversation_id}` | upstream status + body | 404 (mock), upstream 404/413, 502 |

**Every one of the ten routes sets `runtime = 'nodejs'` and `dynamic = 'force-dynamic'`.**
**Zero of the ten perform any authentication or authorization check.**

---

## 24. Metrics

- Total assigned LOC: **3164** (`wc -l` over the 22 files).
- TODO / FIXME / HACK / XXX markers in the assigned files: **0** (verified with `rg -n
  "TODO|FIXME|HACK|XXX"` over all 22 paths).
- Largest function: `createServerHistoryStore` — `frontend/lib/history.ts:339-808`, **470 LOC**.
- Runners-up: `toChatStreamEvent` (`frontend/lib/sse.ts:126-222`, 97 LOC);
  `consume` (`frontend/lib/streams.ts:180-269`, 90 LOC);
  `mockStream` (`frontend/app/api/chat/route.ts:40-121`, 82 LOC);
  `createHistoryApi` (`frontend/lib/historyApi.ts:119-192`, 74 LOC);
  `refresh` (`frontend/lib/history.ts:730-789`, 60 LOC).
- Bare `catch {}` / `.catch(() => …)` blocks in the assigned files: **26**
  (`auth/me:28`; `chat:111,127,167`; `active:26`; `attach:30,54`; `compact:28`; `stop:29`;
  `reports:30`; `reports/[filename]:64`; `upload:41`; `layout:31`; `sse:219`;
  `streams:91,103,366`; `proxy:43`; `history:151,185,365,375,588,595,705,786,794`;
  `historyApi:139,151,241,253`; `auth:26`).
- Upstream `fetch` calls with **no timeout**: **all 12** (`auth/me:18`, `chat:154`,
  `active:17`, `attach:43`, `compact:16`, `stop:17`, `reports:20`, `reports/[filename]:45`,
  `upload:20`, `proxy:33`, plus the client-side `streams:87,97,311,372` and
  `historyApi:129,237`).
