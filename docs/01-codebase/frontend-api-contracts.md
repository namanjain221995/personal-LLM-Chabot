# API contract reference — browser ⇄ Next.js ⇄ orchestrator

The authoritative wire specification for the platform's HTTP and SSE surface. Two tiers:

```
browser ──same-origin──▶ Next.js route handler ──ORCHESTRATOR_URL──▶ FastAPI orchestrator
         /api/*                (runtime: 'nodejs')                    :8080
```

Every one of the 10 Next.js handlers is a thin forwarder. The browser **never** learns
`ORCHESTRATOR_URL` — it is read server-side only (see
[frontend.md §8](./frontend.md)).

> **Authentication: NONE.** Not on any of the 10 Next.js routes, and not on any of the 19
> orchestrator routes. [`orchestrator/app/auth.py:89-97`](../../orchestrator/app/auth.py#L89-L97):
> `current_user()` deletes its `Request` argument and returns `local_user()`; `require_user()` is
> `return current_user(request)` and its own docstring says *"Never 401s now."* The module docstring
> at [`auth.py:17-20`](../../orchestrator/app/auth.py#L17-L20) states the consequence explicitly:
> *"there is now no authentication whatsoever. Anyone who can reach the port can read every
> conversation and query the Salesforce data."* Both services are published on `0.0.0.0`
> (`docker-compose.yml:272-273` orchestrator, `:347-349` frontend). This is `SEC-01`.
>
> Six Next.js handlers still forward a `cookie` header
> (`chat:160-162`, `chat/active:21-23`, `chat/attach/[id]:49-52`, `chat/compact:21-23`,
> `chat/stop:22-24`, `upload:27-29`) and [`lib/proxy.ts:26-27,56-58`](../../frontend/lib/proxy.ts#L26-L27)
> relays cookies both ways. **All of it is dead code** — nothing upstream issues or reads a cookie.
> The comments on those lines ("Owner-scoped: only the user who started a generation may stop it",
> [`chat/stop/route.ts:21`](../../frontend/app/api/chat/stop/route.ts#L21)) describe behaviour that
> no longer exists.

---

## 1. Next.js route handlers — summary

All ten declare `export const runtime = 'nodejs'` and `export const dynamic = 'force-dynamic'`.

| # | Method | Path | Auth | Request | Success | Error statuses |
|---:|---|---|---|---|---|---|
| 1 | GET | `/api/auth/me` | **NONE** | — | 200 `{username, local}` | 502 ×2 |
| 2 | POST | `/api/chat` | **NONE** | `ChatRequestBody` JSON | 200 `text/event-stream` | 400 ×2, 502 ×2 |
| 3 | GET | `/api/chat/active` | **NONE** | — | 200 `{active: string[]}` | *none* — every failure becomes 200 `{active: []}` |
| 4 | GET | `/api/chat/attach/{id}` | **NONE** | — | 200 `text/event-stream` | 400 ×2, 404 ×2, 502 ×2 |
| 5 | POST | `/api/chat/compact` | **NONE** | `{conversation_id, messages[]}` | 200 `{compacted, …}` | upstream status passthrough, 502 |
| 6 | POST | `/api/chat/stop` | **NONE** | `{conversation_id, session_id}` | 200 `{stopped}` | upstream status passthrough, 502 |
| 7 | GET/POST/PUT/DELETE | `/api/history/{...path}` | **NONE** | see §1.7 | upstream status + body verbatim | 404 (not allowlisted), 502; upstream 400/404/409/422 pass through |
| 8 | GET | `/api/reports` | **NONE** | — | 200 `{reports: […]}` | 502 ×2 — **no caller: dead route** |
| 9 | GET | `/api/reports/{filename}` | **NONE** | — | 200 file stream | 400, 404, 502 ×2, **uncaught 500** |
| 10 | POST | `/api/upload` | **NONE** | `multipart/form-data` | upstream status + body | 404 (mock), upstream 404/413, 502 |

**Zero of the twelve upstream `fetch` calls sets a timeout** (`auth/me:18`, `chat:154`,
`active:17`, `attach:43`, `compact:16`, `stop:17`, `reports:20`, `reports/[filename]:45`,
`upload:20`, `proxy:33`, plus client-side `streams:87,97,311,372` and `historyApi:129,237`).
Bare `catch {}` / `.catch(() => …)` blocks across the API + wire layer: **26**.

---

### 1.1 `GET /api/auth/me`

[`frontend/app/api/auth/me/route.ts`](../../frontend/app/api/auth/me/route.ts) (34 LOC)

| | |
|---|---|
| **Method · Path** | `GET /api/auth/me` |
| **Auth** | **NONE** |
| **Request** | No body, no params. The handler takes **no arguments at all** (`:13`), so `req.signal` cannot be forwarded |
| **Upstream** | `GET ${ORCHESTRATOR_URL}/auth/me`, `cache: 'no-store'` (`:18-20`) — no cookie forwarded |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | verbatim upstream JSON `{"username": str, "local": true}` (from [`auth.py:100-103`](../../orchestrator/app/auth.py#L100-L103)) | `:27` |
| 502 | `{message: "The orchestrator responded with status <n>."}` | `:22-25` |
| 502 | `{message: "The orchestrator is unreachable."}` | `:28-33` |

**Notes** — Uses a raw `fetch`, not `proxyToOrchestrator`, so it does **not** relay `Set-Cookie`. A
non-JSON 200 upstream makes `await upstream.json()` (`:27`) throw inside the `try`, and is therefore
reported as "unreachable" — misleading but not a crash. Consumed by
[`lib/auth.ts:20`](../../frontend/lib/auth.ts#L20) (`fetchMe`), whose only remaining job is scoping
the localStorage cache key.

---

### 1.2 `POST /api/chat` — the SSE endpoint

[`frontend/app/api/chat/route.ts`](../../frontend/app/api/chat/route.ts) (185 LOC)

| | |
|---|---|
| **Method · Path** | `POST /api/chat`, `Content-Type: application/json` |
| **Auth** | **NONE** |
| **Upstream** | `POST ${ORCHESTRATOR_URL}/chat` (`:154-166`), `signal: req.signal` forwarded (`:165`) |

**Request schema** — `ChatRequestBody`
([`lib/orchestrator.ts:16-31`](../../frontend/lib/orchestrator.ts#L16-L31)):

```ts
{
  messages?: { role: string; content: string }[];
  session_id?: string;
  image?: string;            // base64, optionally a data: URL
  conversation_id?: string;
  mode?: 'salesforce' | 'assistant';
  model?: 'smart' | 'fast';
  effort?: 'fast' | 'low' | 'medium' | 'high';
  agent?: boolean;
  pdf?: string;              // base64
  pdf_filename?: string;
  web_search?: 'off' | 'auto' | 'on';
}
```

**What the client actually sends** — [`lib/streams.ts:314-332`](../../frontend/lib/streams.ts#L314-L332):

```js
{
  messages: turns.map(m => ({ role: m.role, content: foldModelContent(m.content, m.meta?.pasted) }))
                 .filter(m => m.content),
  session_id:      conversationId,
  conversation_id: conversationId,
  mode:  prefs.salesforce ? 'salesforce' : 'assistant',
  model: prefs.model,          // 'smart' | 'fast'
  effort: prefs.effort,        // 'fast' | 'low' | 'medium' | 'high'
  agent: prefs.agent,          // boolean
  web_search: prefs.webSearch, // 'off' | 'auto' | 'on'
  ...(image ? { image } : {}),
  ...(pdf ? { pdf, pdf_filename: pdfName ?? undefined } : {}),
}
```

Every literal union matches the orchestrator's Pydantic `Literal`s at
[`main.py:191-199`](../../orchestrator/app/main.py#L191-L199). **No mismatch.**

**Translation to the orchestrator body** —
[`toOrchestratorChatRequest`](../../frontend/lib/orchestrator.ts#L70-L101) (`:70-101`) produces
`OrchestratorChatRequest` (`:34-48`): always `message: string` (required) and `image_base64: string | null`,
plus `session_id` defaulting to `'default'` (`:85`), and the optionals spread only when defined
(`:82-95`, `:97`, `:99`). Cross-check against
[`main.py:176-239`](../../orchestrator/app/main.py#L176-L239):

| Orchestrator property | Definition | Frontend behaviour | Verdict |
|---|---|---|---|
| `ChatRequest.text` | `main.py:206-212` — `message` when non-blank, else the last non-blank user message | this module always sets `message` | ✔ consistent |
| `ChatRequest.image_data` | `main.py:215-216` — `image_base64 or image` | this module sets `image_base64` | ✔ |
| `ChatRequest.history_messages` | `main.py:219-231` — drops the trailing user turn | sending both `message` and full `messages` does not duplicate the question | ✔ |
| `ChatRequest._require_input` | `main.py:233-239` — raises when there is no text, image or pdf | `orchestrator.ts:78` returns `null` first → 400 locally, so the 422 never reaches the browser | ✔ pre-empted |

**Responses**

| Status | Body / content type | `file:line` |
|---:|---|---|
| 200 | `text/event-stream` — the upstream body piped through untouched, with `SSE_HEADERS` (`:22-27`: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`, `X-Accel-Buffering: no`) | `:184` |
| 400 | `{message: "Request body must be JSON with a messages array."}` — JSON parse failure | `:128-131` |
| 400 | `{message: "The request contains no user message or image to send."}` — `toOrchestratorChatRequest` returned `null` | `:146-149` |
| 502 | `{message: "The orchestrator is unreachable."}` — the `fetch` threw | `:168-171` |
| 502 | `{message: "The orchestrator responded with status <n>."}` — upstream non-ok **or** upstream had no body | `:174-181` |

**MOCK_MODE** — `process.env.MOCK_MODE === 'true'` (`:134`) short-circuits to `mockStream(body)`
(`:40-121`, 82 LOC), which emits: `ping` (`:70`, deliberately unknown, to prove the decoder drops
it) → `sleep(450)` (`:72`) → reasoning deltas (`:74-78`) → step running/final pairs (`:80-99`) →
tokens (`:101-106`) → `meta` (`:108`) → `done` (`:109`). It emits **no `status` and no `research`**
events, so the web-search UI paths are untestable under mock mode. `DX-02`.

**Contract defects**

- Upstream **422** — a Pydantic validation failure, e.g. `model` outside `Literal["smart","fast"]`
  ([`main.py:192`](../../orchestrator/app/main.py#L192)) — is flattened to a 502 whose only content
  is the number `422`. The client then reports **"The orchestrator is unreachable"**
  ([`streams.ts:335-337`](../../frontend/lib/streams.ts#L335-L337) → `markUnreachable` `:172-173`),
  which is false.
- The upstream error body is discarded entirely (`:174-181`); only the status number survives.
- `await req.json()` (`:126`) buffers the whole body in memory, then `JSON.stringify(chatRequest)`
  (`:164`) makes a second full copy. There is **no size bound anywhere on this path** —
  `Composer`'s 10/25 MB caps are client-side only, and Starlette sets no body limit. `REL-01`.
- No timeout on the upstream fetch (`:154`). No retry.
- Once the body is piped (`:184`) there is no way to signal a later error; the orchestrator owns the
  terminal `error` frame.
- `req.signal` **is** threaded to the upstream fetch (`:165`), so a client abort propagates — but
  the orchestrator's generation is deliberately detached
  ([`main.py:62-76`](../../orchestrator/app/main.py#L62-L76)) and keeps running until
  `POST /chat/stop`.

---

### 1.3 `GET /api/chat/active`

[`frontend/app/api/chat/active/route.ts`](../../frontend/app/api/chat/active/route.ts) (29 LOC)

| | |
|---|---|
| **Method · Path** | `GET /api/chat/active` |
| **Auth** | **NONE** |
| **Request** | No body, no params |
| **Upstream** | `GET ${ORCHESTRATOR_URL}/chat/active`, `cache: 'no-store'` (`:17-24`) |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `{active: []}` when `MOCK_MODE=true` | `:12` |
| upstream status | upstream JSON `{"active": [conversation_key, …]}` (from [`main.py:725-737`](../../orchestrator/app/main.py#L725-L737)) | `:25` |
| **200** | `{active: []}` on **any** thrown error — the failure is reported as a successful empty list, not a 502 | `:27` |

**Notes** — The only route in the tree that converts a transport failure into a 200. That is
deliberate (a failed poll must not paint an error banner) but it also hides a persistently-down
orchestrator from the sidebar. `await upstream.json()` (`:25`) throws on a non-JSON upstream
response and is swallowed into `{active: []}`. `req.signal` is **not** forwarded, so an abandoned
poll keeps the upstream socket open. Polled every 8,000 ms from
[`ChatApp.tsx:301`](../../frontend/components/ChatApp.tsx#L301) with no in-flight guard.

---

### 1.4 `GET /api/chat/attach/{id}`

[`frontend/app/api/chat/attach/[id]/route.ts`](../../frontend/app/api/chat/attach/%5Bid%5D/route.ts) (67 LOC)

| | |
|---|---|
| **Method · Path** | `GET /api/chat/attach/{id}` |
| **Auth** | **NONE** |
| **Request** | Path param `id`; no body. Next 15 async params: `const {id} = await params` (`:24`) |
| **Validation** | `decodeURIComponent(id)` **inside try/catch** (`:27-32`), then `SAFE_ID = /^[\w-]{1,64}$/` (`:18`, tested `:33`), then re-encoded with `encodeURIComponent` (`:44`) |
| **Upstream** | `GET ${ORCHESTRATOR_URL}/chat/attach/{enc}` with `signal: req.signal` (`:43-53`) |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `text/event-stream` — the upstream body piped, `SSE_HEADERS` (`:11-16`) | `:66` |
| 400 | `{message: 'invalid conversation id'}` — malformed percent-escape | `:31` |
| 400 | `{message: 'invalid conversation id'}` — fails `SAFE_ID` | `:34` |
| 404 | `{message: 'no active generation'}` — `MOCK_MODE=true` | `:37` |
| 404 | `{message: 'no active generation'}` — upstream 404 | `:61-64` |
| 502 | `{message: 'no active generation'}` — any other upstream non-ok. **The 502 branch reuses the 404 message**, so an orchestrator 500 reads as "no active generation" | `:61-64` |
| 502 | `{message: 'The orchestrator is unreachable.'}` | `:55-58` |

**Path-traversal analysis — safe.** `SAFE_ID` rejects `.`, `/`, `\` and `%`, and it exactly matches
the orchestrator's `_CONVERSATION_ID_RE = ^[A-Za-z0-9_-]{1,64}$`
([`history.py:33`](../../orchestrator/app/history.py#L33)). This is the **only** dynamic route in the
tree that guards `decodeURIComponent`; the comment at `:25-26` records that the unguarded call
*"surfaced as an unhandled 500"* — a fix that was never applied to
`/api/reports/[filename]` (§1.9). No timeout — correct here, it is a long-lived stream.
`req.signal` **is** forwarded (`:46`), so a client disconnect releases the upstream socket.

---

### 1.5 `POST /api/chat/compact`

[`frontend/app/api/chat/compact/route.ts`](../../frontend/app/api/chat/compact/route.ts) (34 LOC)

| | |
|---|---|
| **Method · Path** | `POST /api/chat/compact`, JSON |
| **Auth** | **NONE** |
| **Request** | `{conversation_id: string, messages: {role, content}[]}`, built at [`ChatApp.tsx:349-356`](../../frontend/components/ChatApp.tsx#L349-L356). Forwarded as **raw `await req.text()`** (`:25`) — never parsed or validated here |
| **Upstream model** | `CompactRequest` = `{conversation_id: str, messages: Optional[List[ChatMessage]]}` ([`main.py:740-742`](../../orchestrator/app/main.py#L740-L742)). **Match** |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `{compacted: false, reason: 'mock mode'}` when `MOCK_MODE=true` | `:11` |
| upstream status | upstream JSON — `{compacted: true, folded_turns: int, covers_through: int}` ([`main.py:775-779`](../../orchestrator/app/main.py#L775-L779)) or `{compacted: false, reason: "nothing older to summarize"}` (`main.py:774`) | `:27` |
| 404 | passthrough of the orchestrator's `conversation not found` ([`main.py:759-760`](../../orchestrator/app/main.py#L759-L760)) | `:27` |
| 422 | passthrough — an empty body reaches the orchestrator and returns its 422 verbatim | `:27` |
| 502 | `{compacted: false, reason: 'orchestrator unreachable'}` | `:29-32` |

**Notes** — Compaction runs a full LLM summarisation pass upstream
([`main.py:772`](../../orchestrator/app/main.py#L772) → `compaction.compact(..., force=True)`), so
this is the route most likely to sit open for minutes — and it has **no timeout** and does **not**
forward `req.signal`, so navigating away leaves the upstream call running. A non-JSON upstream
response makes `await upstream.json()` (`:27`) throw and be reported as "orchestrator unreachable".
Client-side re-entry is guarded by `compacting` state
([`ChatApp.tsx:344`](../../frontend/components/ChatApp.tsx#L344)), but nothing prevents two browser
tabs compacting the same conversation concurrently.

---

### 1.6 `POST /api/chat/stop`

[`frontend/app/api/chat/stop/route.ts`](../../frontend/app/api/chat/stop/route.ts) (32 LOC)

| | |
|---|---|
| **Method · Path** | `POST /api/chat/stop`, JSON |
| **Auth** | **NONE** |
| **Request** | `{conversation_id: id, session_id: id}` — both set to the same value ([`streams.ts:90`](../../frontend/lib/streams.ts#L90)), defensive against the orchestrator's `conversation_id or session_id` key resolution ([`main.py:716`](../../orchestrator/app/main.py#L716)). Forwarded as raw `await req.text()` (`:26`) |
| **Upstream model** | `StopRequest` = `{conversation_id: Optional[str], session_id: str = "default"}` ([`main.py:688-690`](../../orchestrator/app/main.py#L688-L690)). **Match** |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `{stopped: false}` when `MOCK_MODE=true` | `:12` |
| upstream status | `{stopped: bool}` | `:28` |
| 502 | `{stopped: false}` | `:30` |

**Notes** — Bare `catch {}` `:29`. No timeout; `req.signal` not forwarded. The caller
fire-and-forgets with `.catch(() => undefined)`
([`streams.ts:91`](../../frontend/lib/streams.ts#L91)) and never inspects `{stopped}`, so a stop
that silently fails leaves the GPU generating with **no user-visible signal**. `stopStream`
(`streams.ts:82-92`) also aborts the local reader *before* awaiting the server call.

---

### 1.7 `GET|POST|PUT|DELETE /api/history/{...path}`

[`frontend/app/api/history/[...path]/route.ts`](../../frontend/app/api/history/%5B...path%5D/route.ts) (73 LOC)

All four verbs delegate to one private `handle(req, ctx)` (`:17-57`); the verbs are exported at
`:59`, `:63`, `:67`, `:71`. This is the **only** route family that forwards the upstream status
verbatim rather than flattening it to 502
([`lib/proxy.ts:61`](../../frontend/lib/proxy.ts#L61)), so clients see the real 400/404/409/422.

**Allowlist** (`:22-32`) — a request passes only when it is
`GET /api/history/search` (`parts.length === 1 && parts[0] === 'search'`) **or**
`parts[0] === 'conversations' && parts.length <= 3`. Anything else → 404
`{message: 'Unknown history endpoint.'}` (`:28-31`).

**Query-string handling** — an explicit allowlist, not a passthrough: on the `search` branch only
`q` (defaulting to `''`) and `limit` are rebuilt (`:39-45`); on the `conversations` branch only
`?archived=` and only when `parts.length === 1` (`:47-52`). Every other query parameter on every
other path is silently dropped.

| Method · Path | Upstream | Request body | Responses |
|---|---|---|---|
| `GET /api/history/conversations[?archived=<bool>]` | [`history.py:80-85`](../../orchestrator/app/history.py#L80-L85) | — | 200 `ServerConversationSummary[]` (**bare array**) |
| `POST /api/history/conversations` | `history.py:88-102` | `{id?: string, title: string}` | 200 conversation · 400 bad id (regex `history.py:93-97`) · 400 empty title (`history.py:69-73`) · 409 id exists (`history.py:101-102`) |
| `GET /api/history/conversations/{id}` | `history.py:105-113` | — | 200 `{id, title, messages: [{role, content, meta}]}` · 404 |
| `PUT /api/history/conversations/{id}` | `history.py:116-131` | `{title?, pinned?, archived?}`, `extra="forbid"` | 200 · 400 · 404 · 422 on any unknown key |
| `POST /api/history/conversations/{id}/messages` | `history.py:134-148` | `{role, content, meta?}` | 200 message · 400 (`len(role) > 32`, `history.py:140-142`) · 404 |
| `PUT /api/history/conversations/{id}/messages` | `history.py:151-190` | `{messages: MessageIn[]}` | 200 `{id, count}` · 400 · 404 · **409 `MessageCountWouldShrink`** |
| `POST /api/history/conversations/{id}/truncate` | `history.py:200-236` | `{keep: int, expected_total: int}`, `extra="forbid"` | 200 `{id, count}` · 400 · 404 · **409 `ConversationChanged`** |
| `GET /api/history/conversations/{id}/summary` | `history.py:239-254` | — | 200 `{summary, covers_through, updated_at?}` · 404 |
| `DELETE /api/history/conversations/{id}` | `history.py:257-263` | — | 200 `{ok: true}` · 404 |
| `GET /api/history/search?q=&limit=` | `history.py:266-288` | — | 200 `{results: [...]}` · 400 when `q` > 100 chars (`history.py:283-287`) |
| anything else | — | — | 404 `{message: 'Unknown history endpoint.'}` (`:27-32`) |
| orchestrator down | — | — | 502 `{message: 'The orchestrator is unreachable.'}` ([`proxy.ts:44-47`](../../frontend/lib/proxy.ts#L44-L47)) |

**Client bindings** — [`lib/historyApi.ts:119-192`](../../frontend/lib/historyApi.ts#L119-L192)
emits exactly these URLs; every call is ≤ 3 path segments after `conversations`, so **every client
call is reachable through the allowlist**. Verified.

**Allowlist-invariant defect (verified experimentally, not exploitable today)** —
`encodeURIComponent('..') === '..'`, because `.` is an unreserved character. The joined string is
handed to `fetch`, whose WHATWG URL parser performs dot-segment removal:

```
['conversations','..','..']          -> http://x:8080/
['conversations','..','search']      -> http://x:8080/history/search
['conversations','%2e%2e','%2e%2e']  -> http://x:8080/history/conversations/%252e%252e/%252e%252e
```

So the invariant the comment at `:25-27` asserts ("Only the documented /history/conversations… tree
and /history/search are proxied") **does not hold**: `GET /api/history/conversations/../..` reaches
the orchestrator's `/`, and `…/../X` reaches `/history/X` for any single segment `X`. The blast
radius is bounded because (a) `parts.length > 3` is rejected so no deeper path can be assembled,
(b) `encodeURIComponent` escapes any embedded `/` to `%2F`, which the URL parser does **not** treat
as a separator, and (c) neither `/` nor `/history/` has a FastAPI route. Reachable targets are
therefore `/` (404) and `/history/<single-segment>`, of which only `search` exists — and it is
reached with `q=''` because the query string is empty on the 3-part branch. **Not currently
exploitable; the stated invariant is nevertheless false.**

**Dead surface** — `GET .../{id}/summary` is reachable through the proxy and exists upstream, but
its only frontend caller is [`SummaryPanel.tsx:38`](../../frontend/components/SummaryPanel.tsx#L38),
which bypasses `lib/historyApi.ts` and calls it directly.

**Body handling** — no try/catch here; every failure is handled inside `proxyToOrchestrator`, which
buffers the entire request body (`proxy.ts:39`) and the entire response body (`proxy.ts:60`) with no
size bound. The largest body on this path is the whole-thread
`PUT .../messages`, which carries every message's `meta` — including `meta.data` (SQL result rows)
and `meta.chart_data`. `REL-01`.

---

### 1.8 `GET /api/reports`

[`frontend/app/api/reports/route.ts`](../../frontend/app/api/reports/route.ts) (36 LOC)

| | |
|---|---|
| **Method · Path** | `GET /api/reports` |
| **Auth** | **NONE**; no cookie forwarding |
| **Request** | No params, no body |
| **Upstream** | `GET ${ORCHESTRATOR_URL}/reports`, `cache: 'no-store'` (`:20-22`) |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `MOCK_REPORTS` — a **bare array** of `{name, size, mtime, type}` ([`fixtures.ts:378-397`](../../frontend/lib/fixtures.ts#L378-L397)) when `MOCK_MODE=true` | `:13` |
| 200 | upstream body — an **object** `{"reports": [{filename, size_bytes, modified}]}` ([`main.py:257-259`](../../orchestrator/app/main.py#L257-L259) → [`core/report_paths.py:51-70`](../../orchestrator/app/core/report_paths.py#L51-L70)) | `:29` |
| 502 | `{message: "The orchestrator responded with status <n>."}` | `:23-28` |
| 502 | `{message: "The orchestrator is unreachable."}` | `:30-35` |

**This route is dead code.** `rg -n "fetch\('/api/reports"` over `frontend/` returns nothing; the
only `/api/reports` reference in the app is the download `href` in
[`FileCards.tsx:18`](../../frontend/components/FileCards.tsx#L18), which targets the `[filename]`
route. The mock/real shape divergence (bare array vs. `{reports: […]}`) is therefore latent, but it
is a trap for the next caller — see §5.

---

### 1.9 `GET /api/reports/{filename}`

[`frontend/app/api/reports/[filename]/route.ts`](../../frontend/app/api/reports/%5Bfilename%5D/route.ts) (70 LOC)

| | |
|---|---|
| **Method · Path** | `GET /api/reports/{filename}` |
| **Auth** | **NONE**; no cookie forwarding |
| **Validation** | `SAFE_FILENAME = /^[\w][\w.\- ]{0,199}$/` (`:13`) **plus** explicit `..`, `/`, `\` checks (`:22-27`) |
| **Upstream** | `GET ${ORCHESTRATOR_URL}/reports/${encodeURIComponent(decoded)}`, `cache: 'no-store'` (`:45-48`) |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 200 | `application/octet-stream` placeholder text with `Content-Disposition: attachment; filename="<decoded>"` when `MOCK_MODE=true` | `:31-39` |
| 200 | upstream stream, `Content-Type` and `Content-Disposition` taken from upstream or defaulted | `:55-63` |
| 400 | `{message: 'Invalid filename.'}` | `:28` |
| 404 | `{message: "The orchestrator responded with status 404."}` when upstream 404s | `:50-53` |
| 502 | same message shape, for any other upstream non-ok | `:50-53` |
| 502 | `{message: 'The orchestrator is unreachable.'}` | `:64-69` |
| **500** | **uncaught `URIError`** — see below | `:20` |

**Path traversal — safe.** `SAFE_FILENAME` forbids `/`, `\`, `%`, `:` and any leading dot (`^[\w]`),
and the three `includes` checks at `:24-26` are belt-and-braces. A double-encoded payload
`/api/reports/%252e%252e%252fetc%252fpasswd` is decoded once by Next and once at `:20` to
`../etc/passwd`, then rejected at `:24-25`. The orchestrator re-validates independently
([`main.py:265`](../../orchestrator/app/main.py#L265) → `resolve_report_file`). **No traversal.**

**Header injection — safe.** `:36` and `:61` interpolate `decoded` into a `Content-Disposition`
value; `SAFE_FILENAME` excludes `\r`, `\n` and `"`, so no CRLF or quote break-out is possible.

**Defect — uncaught 500.** `decodeURIComponent(filename)` at **`:20` is not wrapped in try/catch**
and throws `URIError` on a malformed escape (verified in Node: `decodeURIComponent('%')` →
`URIError`). This is precisely the bug that
[`chat/attach/[id]/route.ts:25-32`](../../frontend/app/api/chat/attach/%5Bid%5D/route.ts#L25-L32)
documents as having *"surfaced as an unhandled 500"* and fixed — the same fix was never applied
here. Also: `_req.signal` is not forwarded, so an aborted download leaves the upstream fetch
running; and because `decoded` is double-decoded relative to the raw URL, a legitimate report whose
name contains a literal `%` cannot be downloaded.

---

### 1.10 `POST /api/upload`

[`frontend/app/api/upload/route.ts`](../../frontend/app/api/upload/route.ts) (47 LOC)

| | |
|---|---|
| **Method · Path** | `POST /api/upload`, `multipart/form-data` |
| **Auth** | **NONE** |
| **Request** | `FormData` with `file` (a `File`) and `conversation_id` (string), built at [`ChatApp.tsx:440-444`](../../frontend/components/ChatApp.tsx#L440-L444) |
| **Upstream** | `POST ${ORCHESTRATOR_URL}/uploads` with `body: req.body`, `duplex: 'half'`, `signal: req.signal` (`:20-35`) — a **true streaming** proxy, no buffering of the request |
| **Upstream signature** | `file: UploadFile = File(...)`, `conversation_id: str = Form(...)` ([`uploads.py:66-71`](../../orchestrator/app/uploads.py#L66-L71)). **Match** |

**Responses**

| Status | Body | `file:line` |
|---:|---|---|
| 404 | `{message: 'uploads are disabled in mock mode'}` when `MOCK_MODE=true` | `:15` |
| upstream status | upstream body, **always relabelled `content-type: application/json`** | `:36-40` |
| — | upstream success `{upload_id, filename, bytes, files, notes, profile}` ([`uploads.py:150-157`](../../orchestrator/app/uploads.py#L150-L157)) | |
| — | upstream 404 (uploads disabled / conversation not found, `uploads.py:73,78`) | |
| — | upstream **413** when the streamed size exceeds `settings.upload_max_mb` (`uploads.py:55-61`) | |
| 502 | `{detail: 'The orchestrator is unreachable.'}` — note the key is `detail`, not `message`, unlike every other route | `:42-45` |

**Validation performed here: none.** No content-type check, no size check, no field check, no
filename check. The 10/25/200 MB limits
([`Composer.tsx:42,65,68`](../../frontend/components/Composer.tsx#L42)) are **client-side only**.
The only real cap is server-side and streaming, so bytes above the cap still transit the Next.js
process before being rejected.

**Notes** — The hardcoded `content-type: application/json` at `:39` mislabels any non-JSON upstream
body (e.g. a FastAPI HTML 500 page), making `await res.json()` at
[`ChatApp.tsx:445`](../../frontend/components/ChatApp.tsx#L445) throw and surface as *"That dataset
could not be read."* rather than the real error. No timeout. `req.signal` **is** forwarded (`:34`),
so aborting the browser upload aborts the upstream leg. The docstring at `:5-8` is the clearest
statement in the repo of the memory problem `/api/chat` still has: *"Images and PDFs travel as
base64 inside the chat body, which is fine at 10-25 MB but would hold ~270 MB in memory for a 200 MB
archive."* The fix was applied to datasets and **not** to the chat body path (`REL-01`).

---

## 2. Orchestrator routes

19 routes total: 8 declared directly on the `app` object in
[`main.py`](../../orchestrator/app/main.py), plus the `/auth` (1), `/history` (10) and `/uploads` (2)
routers mounted at [`main.py:57-59`](../../orchestrator/app/main.py#L57-L59). The **only**
middleware installed is `CORSMiddleware`
([`main.py:47-53`](../../orchestrator/app/main.py#L47-L53)) — no auth middleware, no rate limiter,
no body-size limit.

### 2.1 Routes on `app`

| Method | Path | Request model | Response | Auth | Status codes | `path:LINE` |
|---|---|---|---|---|---|---|
| GET | `/health` | — | `{status, service, version, checks}` | **NONE** | 200 always | [`main.py:242-254`](../../orchestrator/app/main.py#L242-L254) |
| GET | `/reports` | — | `{reports: [{filename, size_bytes, modified}]}` | **NONE** | 200 | `main.py:257-259` |
| GET | `/reports/{filename}` | path param | `FileResponse` | **NONE** | 200 · 400 (`ReportPathError`) · 404 (`report not found`) | `main.py:262-271` |
| POST | `/chat` | `ChatRequest` | `StreamingResponse` `text/event-stream` | **NONE** | 200 · 404 (`conversation not found`, `main.py:344`) · 422 (`_require_input`) | `main.py:274-685` |
| POST | `/chat/stop` | `StopRequest` | `{stopped: bool}` | **NONE** | **200 always** | `main.py:712-722` |
| GET | `/chat/active` | — | `{active: [str]}` | **NONE** | 200 | `main.py:725-737` |
| POST | `/chat/compact` | `CompactRequest` | `{compacted, folded_turns?, covers_through?, reason?}` | **NONE** | 200 · 401 (**unreachable**) · 404 | `main.py:745-779` |
| GET | `/chat/attach/{conversation_id}` | path param | `StreamingResponse` SSE | **NONE** | 200 · 404 (`no active generation`) | `main.py:782-796` |

**`GET /health`** ([`main.py:242-254`](../../orchestrator/app/main.py#L242-L254)) returns
`{"status": report["status"], "service": "orchestrator", "version": app.version, "checks": …}` where
`status` is `"ok"` only when every probe is ok, else `"degraded"`
([`health.py:130`](../../orchestrator/app/health.py#L130)). **It returns HTTP 200 even when
degraded** — any monitor keying on the status code sees a healthy service. `checks` carries one
entry per deduplicated vLLM endpoint plus `duckdb` and `app_db`, and each failing entry embeds raw
exception text and internal URLs (`health.py:42,45,64,91`) on an unauthenticated endpoint.
`settings.health_probe_timeout` bounds only the HTTP probes; the two `asyncio.to_thread` calls
(`health.py:122-123`) have no timeout, and `_check_app_db` runs a full SQLite schema+migration write
transaction on every call (`health.py:79` → `db.py:203-204`).

**`POST /chat/stop`** returns `{"stopped": false}` — indistinguishable from "not found" — when the
generation is absent, already done, or fails `_owns` (`main.py:717-720`). It never 404s.

**`GET /chat/attach/{conversation_id}`** 404s once the generation is finished
(`main.py:789-791`) — the answer is in history at that point. Note the orchestrator does **not**
apply `_CONVERSATION_ID_RE` here (`history.py:33` is enforced only on
`POST /history/conversations`); the id regex on this path is entirely a frontend guard (§1.4).

**`POST /chat/compact`** contains a dead 401 branch at `main.py:756-757` — `current_user` never
returns `None` ([`auth.py:89-92`](../../orchestrator/app/auth.py#L89-L92)).

**`_owns` / `_viewer_id`** ([`main.py:693-709`](../../orchestrator/app/main.py#L693-L709)) are
structurally correct owner checks used by `/chat/stop`, `/chat/active` and `/chat/attach` — but
since every caller resolves to the same `local_user()` id, the comparison is a tautology and all
three routes are open to any client that can reach the port. `SEC-01`.

### 2.2 `/auth` router — [`auth.py:32`](../../orchestrator/app/auth.py#L32)

| Method | Path | Request | Response | Auth | Status codes | `path:LINE` |
|---|---|---|---|---|---|---|
| GET | `/auth/me` | — | `{"username": str, "local": true}` | **NONE** | 200 | [`auth.py:100-103`](../../orchestrator/app/auth.py#L100-L103) |

`SESSION_COOKIE = "ts_session"` ([`auth.py:35`](../../orchestrator/app/auth.py#L35)) is declared but
has no reader anywhere in `orchestrator/app`.

### 2.3 `/history` router — [`history.py:30`](../../orchestrator/app/history.py#L30)

Every handler takes `user: sqlite3.Row = Depends(require_user)` (`history.py:82, 90, 107, 120, 138,
155, 204, 241, 260, 270`) — the dependency that **never 401s**. Every handler is a plain `def`, so
FastAPI runs them in the anyio threadpool; this is the one module that does not block the event loop.

| Method | Path | Request model | Response | Status codes | `path:LINE` |
|---|---|---|---|---|---|
| GET | `/history/conversations?archived=<bool>` | query | bare `list` of summaries | 200 | `history.py:80-85` |
| POST | `/history/conversations` | `ConversationIn{id?: str, title: str}` `:38-40` | conversation dict | 200 · 400 (id fails `^[A-Za-z0-9_-]{1,64}$`, `:93-97`) · 400 (empty title, `:69-73`) · 409 (`IntegrityError`, `:101-102`) | `history.py:88-102` |
| GET | `/history/conversations/{id}` | path | `{…, messages: [...]}` | 200 · 404 | `history.py:105-113` |
| PUT | `/history/conversations/{id}` | `ConversationUpdate{title?, pinned?, archived?}`, `extra="forbid"` `:43-54` | conversation dict | 200 · 400 · 404 · 422 (unknown key) | `history.py:116-131` |
| POST | `/history/conversations/{id}/messages` | `MessageIn{role, content, meta?}` `:57-60` | message dict | 200 · 400 (`len(role) > 32`, `:140-142`) · 404 | `history.py:134-148` |
| PUT | `/history/conversations/{id}/messages` | `MessagesReplaceIn{messages}` `:63-66` | `{id, count}` | 200 · 400 · 404 · **409 `MessageCountWouldShrink`** (`:180-187`) | `history.py:151-190` |
| POST | `/history/conversations/{id}/truncate` | `TruncateIn{keep: int, expected_total: int}`, `extra="forbid"` `:193-197` | `{id, count}` | 200 · 400 (`:213-218`) · 404 · **409 `ConversationChanged`** (`:219-230`) | `history.py:200-236` |
| GET | `/history/conversations/{id}/summary` | path | `{summary, covers_through, updated_at?}` | 200 · 404 | `history.py:239-254` |
| DELETE | `/history/conversations/{id}` | path | `{ok: true}` | 200 · 404 | `history.py:257-263` |
| GET | `/history/search?q=&limit=` | query | `{results: [...]}` | 200 · 400 (`q` > 100 chars, `:283-287`) | `history.py:266-288` |

The two **409s are the load-bearing data-integrity guards**: `MessageCountWouldShrink` refuses a
whole-thread replace that would lose messages, and `ConversationChanged` gives `truncate` optimistic
concurrency on `expected_total`. `truncate` additionally calls `db.clear_summary` (`:235`) so the
rolling summary cannot describe deleted turns. `role` validation is **length-only** (`:140-142`), so
`role: "system"` can be persisted into a thread and later replayed to the model.

There is **no pagination** on `list_conversations` (`:80-85`) or on `get_conversation`'s
`db.list_messages` (`:112`); both return every row.

### 2.4 `/uploads` router — [`uploads.py:28`](../../orchestrator/app/uploads.py#L28)

| Method | Path | Request | Response | Auth | Status codes | `path:LINE` |
|---|---|---|---|---|---|---|
| POST | `/uploads` | multipart `file: UploadFile` + `conversation_id: str = Form(...)` | `{upload_id, filename, bytes, files, notes, profile}` | `Depends(require_user)` (never 401s) | 200 · 400 (`ArchiveError` `:122-127`, generic `:128-136`) · 404 (uploads disabled `:72-73`, not owner `:76-78`) · **413** (`:55-61`) · **uncaught 500** (see below) | `uploads.py:66-157` |
| GET | `/uploads/{conversation_id}` | path | `{uploads: [...]}` | `Depends(require_user)` | 200 · 404 | `uploads.py:160-172` |

**Validation actually performed**: size only (streamed cap at `:55`, bounded by `upload_max_mb` plus
one 1 MiB chunk). **No MIME check** — `file.content_type` is never read. **No extension rejection** —
the extension only selects the extraction path (`:97`, `:101`). Filename sanitisation is
`os.path.basename` only (`:82`).

**Uncaught 500** — `_stream_to_disk` is called at `:92`, **outside** the try block that starts at
`:96`. A multipart `filename` of `"/"` makes `os.path.basename("/") == ""`, so `dest` becomes
`<root>/_original/` and `open(dest, "wb")` raises `IsADirectoryError` → a 500 with a traceback
rather than a 4xx, leaving the partially-written tree on disk.

---

## 3. SSE event reference

### 3.1 The allowlist — 8 events, both sides in agreement

**Orchestrator** — [`orchestrator/app/sse.py:34-44`](../../orchestrator/app/sse.py#L34-L44):

```python
ALLOWED_EVENTS  = ("token", "meta", "done", "error")   # sse.py:34
V2_EVENTS       = ("reasoning", "step")                # sse.py:36
PROGRESS_EVENTS = ("status",)                          # sse.py:39
RESEARCH_EVENTS = ("research",)                        # sse.py:43
ALL_EVENTS      = ALLOWED_EVENTS + V2_EVENTS + PROGRESS_EVENTS + RESEARCH_EVENTS  # sse.py:44
```

`sse_event()` raises `ValueError` on any other name
([`sse.py:51-52`](../../orchestrator/app/sse.py#L51-L52)), so the wire set is **exactly these
eight**. Frame format is `f"event: {event}\ndata: {payload}\n\n"` with
`json.dumps(dict(data or {}), ensure_ascii=False, default=str)` (`sse.py:53-54`).

**Frontend** — [`frontend/lib/sse.ts:126-218`](../../frontend/lib/sse.ts#L126-L218): a `switch` with
switch arms at `:129` (`token`), `:130` (`reasoning`), `:131` (`status`), `:137` (`step`), `:166`
(`research`), `:202` (`meta`), `:204` (`done`), `:206` (`error`), plus `default: return null` at
`:216-217`. `ChatStreamEvent` (`:105-118`) is an 8-arm union.

> **The two sides agree exactly.** Eight names emitted, eight names decoded, one-to-one, no
> orphan on either side. Any future ninth event is silently dropped by the frontend
> (`default: return null`, `:216-217`) — the documented tolerance requirement at `sse.ts:122-125`,
> and the reason the mock stream deliberately emits a `ping`
> ([`app/api/chat/route.ts:70`](../../frontend/app/api/chat/route.ts#L70)) as a live proof.

### 3.2 Per-event payload shapes

| Event | Orchestrator payload | Emitted at | Frontend decode | Validation applied | Result |
|---|---|---|---|---|---|
| `token` | `{"text": str}` | `sse.py:58`; engines `sql.py:436`, `chat.py:101`, `agent.py:602`, `rag.py:143`, `report.py:280`, `url.py:96`, `repo.py:166`, `dataset.py:100`, `search.py:413` | `sse.ts:129-136` | `typeof parsed.text === 'string'`, else `null` | `{kind:'token', text}` → appended to `message.content` ([`streams.ts:183-192`](../../frontend/lib/streams.ts#L183-L192)) |
| `reasoning` | `{"text": str}` | `sse.py:75`; `chat.py:98`, `agent.py:599` | `sse.ts:130` (shares the `token` arm) | same string guard | `{kind:'reasoning', text}` → `message.reasoning` (`streams.ts:195-200`); starts the thinking clock on the first delta (`:196`) |
| `status` | `{"text": str}` | `main.py:415`; `search.py:403,464,477`, `url.py:32,41,44,49`, `repo.py:31,37,40`, `sql.py:317`, `compaction.py:274` | `sse.ts:131` (shares the `token` arm) | same string guard | `{kind:'status', text}` → `message.searchStatus`, **cleared by the next `token`** (`streams.ts:191,193-194`) |
| `step` | `{"id": int, "title": str, "status": "running"\|"done"\|"failed", "detail"?: str}` | `sse.py:78-85` (`step_event`, validates `status` at `:80-81`); `agent.py:382` | `sse.ts:137-165` | `typeof id === 'number'` **and** `typeof title === 'string'` (`:144-146`); `status ∈ {running,done,failed}` (`:147-152`); `detail` copied only when a string (`:160-162`) | `{kind:'step', step}` → `mergeStep` (`sse.ts:229-238`), which merges by `id` with `{...list[idx], ...step}` so an update omitting `detail` keeps the earlier one |
| `research` | `{"phase":"query","query":str,"results":[{title,url,domain}]}` or `{"phase":"reading"\|"read","count":int}` | `search.py:229-239` (`query`), `:446,478` (`reading`), `:454,485` (`read`) | `sse.ts:166-201` | `phase==='query'` requires a string `query` (`:174`); `results` defaults to `[]` if not an array (`:175`); each result kept **only** when `o.url` is a string, `title` falling back to the url and `domain` to `''` (`:182-191`). `phase==='reading'\|'read'` requires `typeof count === 'number'` (`:196-198`). Anything else → `null` (`:200`) | `{kind:'research', phase, query?, count?}` → merged into `message.research` (`streams.ts:201-236`); the elapsed clock is measured **client-side** |
| `meta` | engine keys + central merge — see §4 | `sse.py:62`; one per answer, merged at [`main.py:364-381`](../../orchestrator/app/main.py#L364-L381) | `sse.ts:202-203` | **NONE.** `return {kind:'meta', meta: JSON.parse(ev.data)}` — a raw unchecked cast to `Meta` | `{kind:'meta', meta}` → `foldStreamState(...)` (`streams.ts:239-250`) then persisted |
| `done` | `{"session_id": str}` | `sse.py:66`; **`main.py:647` is the only emitter** | `sse.ts:204-205` | none needed | `{kind:'done'}` — **the payload is discarded**; `finalize({status:'done'})` and `break` (`streams.ts:256-261`) |
| `error` | `{"message": str(exc)}` | `sse.py:70`; **`main.py:672` is the only emitter** | `sse.ts:206-215` | `message` must be a string, else the literal fallback `'The engine reported an error without details.'` | `{kind:'error', message}` → `finalize({status:'error', errorMessage})` and `break` (`streams.ts:251-255`) |

**Asymmetry worth flagging**: `step` and `research` are validated field by field; `meta` — the only
event carrying `sql`, `data`, `chart`, `chart_data`, `citations`, `sources` and `report_files` into
the UI — is not validated at all (`sse.ts:203`). Every field of `interface Meta`
([`types.ts:101-152`](../../frontend/lib/types.ts#L101-L152)) is therefore an unverified assumption
about the wire, and a malformed persisted `meta.research` throws inside `ResearchPanel`'s render
with no error boundary above it.

### 3.3 Event order guarantee

Declared at [`main.py:284-286`](../../orchestrator/app/main.py#L284-L286) and
[`sse.py:11-15`](../../orchestrator/app/sse.py#L11-L15):

```
(reasoning | step | status | research | token)*  →  exactly one meta  →  done
                                                 └→ error  (terminal INSTEAD of done)
```

- **Deltas** — `reasoning`, `step`, `status`, `research` and `token` may interleave freely and repeat
  any number of times. There is no ordering constraint among them.
- **Exactly one `meta`** per answer. Enforced structurally: the `emit()` closure
  ([`main.py:364-381`](../../orchestrator/app/main.py#L364-L381)) intercepts `event == "meta"`,
  merges the central keys and assigns `gen.final_meta` — every engine emits exactly one.
- **`done` is terminal on success**, `{"session_id": str}`, emitted only at `main.py:647`.
- **`error` is terminal on failure**, `{"message": str(exc)}`, emitted only at `main.py:672`, and
  **replaces** `done` — the two never both appear.
- The frontend enforces the same shape: `consume` (`streams.ts:180-269`) sets `sawTerminal = true`
  and `break`s out of the `for await` on either `error` (`:251-255`) or `done` (`:256-261`).

**Undocumented third terminal state.** The cancellation path
(`main.py:668-669` → `gen.finish()` → `follow()` breaks at `main.py:115`) closes the stream with
**no terminal frame at all**. The frontend absorbs this at
[`streams.ts:264-268`](../../frontend/lib/streams.ts#L264-L268), finalising as `'done'` when the body
ends without a terminal frame. This is real behaviour that neither
[`sse.py:20-24`](../../orchestrator/app/sse.py#L20-L24) nor the `/chat` docstring mentions.

**Attach replay preserves order.** `GET /chat/attach/{id}`
([`main.py:782-796`](../../orchestrator/app/main.py#L782-L796)) replays every buffered event from
`LiveGeneration.events` in order, then streams live — so a reattaching client observes the identical
sequence. `LiveGeneration.follow()` (`main.py:105-120`) supports multiple concurrent subscribers.

### 3.4 Transport-level facts

- **No keep-alive / heartbeat frame** and no `id:` or `retry:` field. A long silent generation
  relies entirely on `X-Accel-Buffering: no` (`main.py:684`) and on no intermediate proxy having an
  idle timeout.
- The frontend parser handles `id:` and `retry:` by ignoring them
  ([`sse.ts:94`](../../frontend/lib/sse.ts#L94)) and treats a leading `:` as a comment/keep-alive
  (`sse.ts:78`), so a future heartbeat would be transparent.
- `SSEParser.feed` (`sse.ts:34-64`) is CRLF-correct across chunk boundaries via `pendingCR`
  (`:36-41`, `:52-53`), and `readChatStream` decodes with `{stream: true}` (`:293`) so split
  multibyte sequences are safe.
- **No timeout and no idle detection** on `readChatStream` (`sse.ts:283-301`): if the orchestrator
  holds the connection open without sending, the generator awaits `reader.read()` forever, the
  message stays `status: 'streaming'`, and `Composer` stays disabled
  ([`Composer.tsx:139`](../../frontend/components/Composer.tsx#L139)).
- `finally { reader.releaseLock() }` (`sse.ts:298-300`) **never calls `reader.cancel()`**, so a
  consumer `break` releases the lock without cancelling the underlying stream.
- `sse_event` is called from `LiveGeneration.follow()` (`main.py:118`) rather than from `publish()`
  (`main.py:95-98`), so an invalid event name would raise inside the *reader* after the frame was
  already buffered — the stream dies mid-flight instead of the publisher failing fast.

---

## 4. `meta` key inventory

One `meta` event per answer. Its keys come from two sources: whatever the engine puts in the dict,
plus a central merge performed by the `emit()` closure at
[`main.py:364-381`](../../orchestrator/app/main.py#L364-L381).

### 4.1 Central keys — added to every `meta` regardless of engine

| Key | Type | Source | `path:LINE` | Declared in `Meta`? |
|---|---|---|---|---|
| `mode` | `"salesforce" \| "assistant"` | `meta_extras(route)` | `main.py:295-318`, merged `:368` | ✔ [`types.ts:117`](../../frontend/lib/types.ts#L117) |
| `model` | served model id (string) | `meta_extras` — `vision` → `settings.vision_model` (`main.py:308`), `agent` → smart id, `sql`/`rag`/`report` → smart id, else `llm.served_model_id(request.model)` | `main.py:295-318` | ✔ `types.ts:119` |
| `effort` | `"fast"\|"low"\|"medium"\|"high"` | `meta_extras`; **hard-coded to `"medium"` for sql/rag/report** (`main.py:314`), absent for `vision` | `main.py:295-318` | ✔ `types.ts:121` |
| `generation_id` | string | `gen.generation_id` — the idempotency key so an answer watched by two attached clients is stored once | `main.py:369` | ✔ `types.ts:141` |
| `input_trimmed` | `{dropped_turns: int, clipped_messages: int}` | `context.get_trim_notice()`, **only when non-empty** | `main.py:373-375` | ✔ `types.ts:147` |
| `context` | `ContextUsage` — `{tokens_used, usable_budget, window, reserved_output, fraction, summarized_turns, compacted?}` | `context_state`, **only when non-empty** | `main.py:376-377` | ✔ `types.ts:149` + `:154-167` |
| `auto` | `{agent: bool, search: bool}` — what auto-orchestration decided | `orchestration_state`, **only when non-empty** | `main.py:378-379` | ✘ **absent from `Meta`** |

### 4.2 Engine keys

| Key | Type | Emitted by | `path:LINE` | Declared in `Meta`? |
|---|---|---|---|---|
| `route` | `Engine` | every engine | `sql.py:394,452`, `rag.py:150`, `chat.py:104`, `agent.py:605`, `search.py:494-503`, `report.py:282`, `vision.py:95`, `document.py:37,75`, `repo.py:167`, `url.py:97`, `dataset.py:101,118` | ✔ `types.ts:102` — **but the union omits `'dataset'`** |
| `sql` | `string` | sql engine, agent | `sql.py:394,452`; `agent.py:467-503` | ✔ `types.ts:103` |
| `data` | `DataRow[]` | sql engine | `sql.py:394,452` | ✔ `types.ts:104` |
| `truncated` | `boolean` | sql engine | `sql.py:394,452` | ✔ `types.ts:105` |
| `chart` | `ChartSpec` | sql engine (optional) | `sql.py:394,452` | ✔ `types.ts:106` |
| `chart_data` | `DataRow[]` | sql engine (optional — histogram bins, funnel in trusted stage order) | chart pipeline | ✔ `types.ts:113`; consumed with a `data` fallback at [`ProofDrawer.tsx:61`](../../frontend/components/ProofDrawer.tsx#L61) for threads persisted before the key existed |
| `citations` | `Citation[]` `{record_id, object, url}` | rag engine, agent | `rag.py:150`; `agent.py:467-503` | ✔ `types.ts:114` |
| `sources` | `WebSource[]` `{n, title, url, domain}` | search engine, agent | `search.py:494-503`; `agent.py:467-503` | ✔ `types.ts:131` |
| `report_files` | `ReportFile[]` | report engine, sql engine, agent | `report.py:282`; `sql.py:394`; `agent.py:467-503` | ✔ `types.ts:115` |
| `steps` | `AgentStep[]` (final statuses) | agent engine | `agent.py:605` | ✔ `types.ts:123` |
| `search_unavailable` | `true` | search engine when search was requested but unavailable | `search.py:416` | ✔ `types.ts:133` |
| `code_sources` | `CodeSource[]` | repo engine | — | ✔ `types.ts:135` |
| `datasets` | `[{filename, bytes, status, files}]` | dataset engine | [`dataset.py:119-127`](../../orchestrator/app/engines/dataset.py#L119-L127) | ✘ **absent from `Meta`** — see §5 |

### 4.3 Client-side keys — written by the browser, never by the server

| Key | Type | Written at | Purpose |
|---|---|---|---|
| `reasoning` | `string` | [`sse.ts:262`](../../frontend/lib/sse.ts#L262) (`foldStreamState`) | the accumulated `reasoning` deltas, folded onto `meta` so the accordion survives the history round-trip |
| `reasoning_seconds` | `number` | `sse.ts:263-265` | the client-measured "Thought for N s" |
| `research` | `Research` | `sse.ts:259-261`, with `active: false` | the accumulated `research` events; the elapsed clock is measured in the browser |
| `pasted` | `PastedText[]` | set on the **user** message by [`ChatApp.tsx:408-420`](../../frontend/components/ChatApp.tsx#L408-L420) | keeps PASTED chips collapsed in the UI while `foldModelContent` inlines them for the model |

`foldStreamState` is careful never to clobber a server value: each carry is guarded by
`&& !out.<key>` / `== null` (`sse.ts:259`, `:262`, `:263`).

---

## 5. Contract divergences

All five are confirmed by reading both sides. Divergences 1, 2 and 4 all trace to the same root
cause: `meta` is the only SSE event the frontend does not validate
([`sse.ts:203`](../../frontend/lib/sse.ts#L203)), so a key the server adds and the frontend never
declared fails silently instead of loudly.

| # | Divergence | Server side | Frontend side | Impact |
|---:|---|---|---|---|
| 1 | **`'dataset'` is not in the `Engine` union** | [`engines/dataset.py:101`](../../orchestrator/app/engines/dataset.py#L101) and `:118` emit `{"route": "dataset"}` | [`types.ts:8`](../../frontend/lib/types.ts#L8) — `'sql'\|'rag'\|'vision'\|'report'\|'chat'\|'agent'\|'search'\|'url'\|'repo'` | Does not throw (`meta` is cast unvalidated), but every `switch (meta.route)` consumer falls to its default — [`EngineBadge.tsx:63-64`](../../frontend/components/EngineBadge.tsx#L63-L64) prints the raw route with Chat styling |
| 2 | **`auto` is undeclared** | [`main.py:378-379`](../../orchestrator/app/main.py#L378-L379) sets `data["auto"] = dict(orchestration_state)` | no `auto` key on `interface Meta` (`types.ts:101-152`) | Passes through JSON untouched, but is unreadable to typed consumers; the UI cannot show what auto-orchestration decided |
| 3 | **Three shapes for one report-file concept** | `/reports` returns `{filename, size_bytes, modified}` ([`core/report_paths.py:62-66`](../../orchestrator/app/core/report_paths.py#L62-L66)) | `ReportFile` is `{filename, type, size?}` (`types.ts:74-78`); `MOCK_REPORTS` is `{name, size, mtime, type}` ([`fixtures.ts:379-397`](../../frontend/lib/fixtures.ts#L379-L397)) | Latent — `/api/reports` has no caller. `meta.report_files` (which the engines emit) is what actually feeds `FileCards`, and it matches `ReportFile` |
| 4 | **`meta.datasets` is undeclared** | [`dataset.py:115-129`](../../orchestrator/app/engines/dataset.py#L115-L129) emits `"datasets": [{filename, bytes, status, files}]` | no `datasets` key on `interface Meta`; `grep -n datasets frontend/lib/types.ts` → no hit | The dataset engine's only structured output is invisible to `ProofDrawer` ([`ProofDrawer.tsx:37-67`](../../frontend/components/ProofDrawer.tsx#L37-L67) builds no section for it), so a dataset answer renders with a badge showing the raw string `dataset` and no proof panel at all |
| 5 | **`orchestrator.ts` docstring drift** | `ChatRequest.message` is `Optional[str] = None` with a `@model_validator` (`main.py:185`, `:233-239`) | [`lib/orchestrator.ts:6-8`](../../frontend/lib/orchestrator.ts#L6-L8) still documents `message: str (min_length=1)` | Documentation only; behaviour is correct |

**Non-divergences, verified** — the four `Literal` unions (`mode`, `model`, `effort`,
`web_search`) match exactly between `streams.ts:314-332` and `main.py:191-199`; `CompactRequest`
matches; `StopRequest` matches; the multipart upload fields match; the conversation-id regex
`SAFE_ID` (`attach/[id]:18`) matches `_CONVERSATION_ID_RE` (`history.py:33`); the search query cap
(`SEARCH_MAX_QUERY = 100`, `searchPalette.ts:276`) matches `_MAX_QUERY_LENGTH = 100`
(`history.py:35`); and the context-meter constants `8192` / `512` / `131072`
([`contextMeter.ts:45-46`](../../frontend/lib/contextMeter.ts#L45-L46)) match `MODEL_MAX_OUTPUT`
(`config.py:128`), `CONTEXT_SAFETY_MARGIN` (`config.py:131`) and the model window
(`context.py:4`) — though those three are environment-overridable server values duplicated in the
browser with no endpoint exposing the real budget.

---

## 6. Findings index

| ID | Where | Nature |
|---|---|---|
| `SEC-01` | all 10 Next.js routes; all 19 orchestrator routes; [`auth.py:89-97`](../../orchestrator/app/auth.py#L89-L97), `main.py:56`, `docker-compose.yml:272-273` | No authentication anywhere; every port published on `0.0.0.0`. `_owns`/`_viewer_id` (`main.py:693-709`) are tautologies. Six frontend cookie-forwarding branches and `proxy.ts`'s bidirectional cookie relay are dead code |
| `REL-01` | [`app/api/chat/route.ts:126,164`](../../frontend/app/api/chat/route.ts#L126); [`lib/proxy.ts:39,60`](../../frontend/lib/proxy.ts#L39); `main.py:176-199` | `ChatRequest.image`/`.pdf` are unbounded base64 strings; `/api/chat` buffers the body twice; `proxy.ts` buffers both directions; Starlette installs no body limit and the only middleware is CORS (`main.py:47-53`) |
| `OBS-01` | every route handler; [`lib/streams.ts:311,372`](../../frontend/lib/streams.ts#L311); [`lib/historyApi.ts:129`](../../frontend/lib/historyApi.ts#L129); `sse.py` | No correlation/trace id is generated in the browser, threaded through the Next.js tier, carried on a `meta` key, or logged by the orchestrator. `generation_id` is an idempotency key, not a trace id — it is created server-side and never travels upstream from the browser |
| `DX-02` | [`app/api/chat/route.ts:134`](../../frontend/app/api/chat/route.ts#L134) and the `MOCK_MODE` branch of eight other routes; [`lib/fixtures.ts`](../../frontend/lib/fixtures.ts); [`lib/mockApi.ts`](../../frontend/lib/mockApi.ts) | `MOCK_MODE=true` silently substitutes fabricated answers, fake Salesforce record ids and `lightning.force.com` URLs across nine of the ten routes, and the variable is undocumented in `.env.example`. The mock stream also omits `status` and `research`, so those UI paths are untestable in mock mode |
| `TEST-02` | all 10 route handlers | None of the eight critical paths (chat SSE, attach/replay, stop, compact, history sync, truncate, upload, report download) has end-to-end coverage; frontend tests are `lib/`-only unit tests (`vitest.config.mts:5-6`) |
