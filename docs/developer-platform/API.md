# `/v1` — internal API reference

The reference an engineer on this codebase reads before changing, supporting or
debugging the public developer API. It is **derived from the code**, not from
the contract: every statement below was read out of `orchestrator/app/publicapi/`
(`router.py`, `models.py`, `errors.py`, `events.py`, `streaming.py`,
`background.py`, `registry.py`) and the pieces of `orchestrator/app/apiplatform/`
those files call (`resolver.py`, `scopes.py`, `quotas.py`, `keys.py`). Where the
code and `CONTRACT.md` disagree, the code is what is described here and the
disagreement is listed in [§12](#12-where-the-code-and-the-contract-disagree).

The customer-facing documentation is a different document, served by the
frontend at `/docs` (`frontend/content/docs/`). This one is for us.

Written 2026-09-13 against the working tree of
`feat/developer-platform-security`, and **re-read at 02:50 IST** after a second
wave rewrote `router.py`, `quotas.py`, `idempotency.py` and `background.py`
while the first draft was being written. Files were still changing at that
time; re-check §12 before relying on it. Line numbers are deliberately avoided;
symbols are named instead so the reference survives edits.

**2026-09-13, afternoon — the all-models wave.** The owner asked for every model
TechSara runs on `/v1` and for output up to 1,000,000 tokens. That wave's code
(`publicapi/registry.py`, `planning.py`, `engines.py`, `capacity.py`,
`sidecars.py`, `endpoints.py`, `multipart.py`, `apiplatform/scopes.py`, V35) was
being written in parallel with this update, so **§13 describes it from the
binding design, not from the code**, and says for each item which symbol to
read to confirm it. §1–§12 remain the code as it was read at 02:50 IST, with a
pointer to §13 wherever the wave changes them. Re-derive §13 from the tree after
the wave merges and fold it into the sections above.

---

## 1. Where it lives and how a request reaches it

| hop | what | file |
|---|---|---|
| public edge (today) | `https://ai.techsarasolutions.com/v1/…` → the Next.js route handler, which forwards `authorization`, `idempotency-key`, `content-type`, `origin` and a few more by name, never `cookie`, and pipes the body and the response stream through unbuffered | `frontend/app/v1/[[...path]]/route.ts` |
| orchestrator | `APIRouter(prefix="/v1", route_class=PublicRoute)` mounted by `app/main.py` inside a guarded import; `main.PUBLIC_API_MOUNTED` says whether it is there | `orchestrator/app/publicapi/router.py` |
| engine | `llm.stream_chat_events(..., model_choice="smart")` and nothing lower, through the shared admission lanes and the breaker — for `techsara-35b`. The other five models go through `publicapi/engines.py` or `publicapi/sidecars.py` under per-engine capacity gates (§13.3) | `orchestrator/app/publicapi/streaming.py` |

Middleware that treats `/v1` differently from the chat application
(`app/main.py`, matched by `_is_public_api_path`: `/v1` itself and anything
under `/v1/`, never `/v1beta…`):

* `_reject_cross_site_writes` skips `/v1` — the surface has no ambient
  credential for a hostile page to ride;
* `BrowserCorsExceptPublicApi` does not apply the browser CORS allowlist to
  `/v1`; the router answers its own CORS (§3);
* `RequestBodySizeLimitMiddleware` applies the `/v1` cap (1 MiB by default,
  `PUBLIC_API_MAX_BODY_BYTES`) before anything parses the body, and renders the
  413 in the `/v1` error envelope.

Checked on 2026-09-13 by importing the application (no database, no engine):
`main.PUBLIC_API_MOUNTED` and `main.CONSOLE_API_MOUNTED` are both true, the
`/v1` router carries eight operations plus the `OPTIONS /v1/{rest:path}`
preflight, and FastAPI's own `/docs`, `/redoc` and `/openapi.json` are not
mounted.

## 2. Authentication

One credential, one header:

```
Authorization: Bearer tsk_live_<public_id>_<secret><checksum>
Authorization: Bearer tsk_test_<public_id>_<secret><checksum>
```

`public_id` is 16 hex characters and is safe to log. `secret` is
`secrets.token_urlsafe(32)`. `checksum` is six Base62 characters of CRC32 over
`public_id + secret`; it lets a malformed token be refused without database
work, and it is not a security control (`apiplatform/keys.py`). The stored value
is `HMAC-SHA256(pepper, secret)`; the plaintext exists once, in the console
response that created it.

**The cookie is never read.** `router.resolve_caller` reads
`request.headers["authorization"]` and nothing else; an empty header is refused
before the resolver is called, so an unauthenticated flood costs no database
round trip. Verified: `GET /v1/models` with only a `ts_session` cookie answers
the same 401 as a request with nothing.

The resolver ladder (`apiplatform/resolver.resolve_api_caller`), every rung
answering the **same** `401 invalid_api_key`, with the same message:

1. token shape and checksum, offline;
2. `api_keys` row by `public_id` (an unknown id is still charged an HMAC against
   a dummy digest, so timing does not reveal existence);
3. the `tsk_live_` / `tsk_test_` prefix matches the row's environment;
4. constant-time digest comparison;
5. key `status = 'active'`, not past `expires_at`, not past a
   `rotation_expires_at`;
6. service account (if any) active;
7. project active;
8. workspace active;
9. the caller's address is inside the project's `ip_allowlist` (an empty list
   allows every address);
10. `touch_api_key` records `last_used_at` and `last_used_ip`.

The address used for step 9 is decided by `resolver.client_address` from the
socket peer and `X-Forwarded-For` together. The forwarding header is believed
**only when the socket peer is inside `PUBLIC_API_TRUSTED_PROXIES`** (addresses
or CIDRs); the chain is then read from the right, skipping trusted hops, and
the first untrusted hop is the client. With the setting empty — the default —
the socket peer is the address, which for traffic through the Next.js edge is
the frontend container. `AUTH_TRUST_PROXY_HEADERS` plays no part on `/v1`. See
OPERATIONS.md §2 and §8.

The result is an `ApiCaller` — workspace, project, service account, key id,
scopes, allowed models, allowed origins, limits and environment — never a
browser `Principal`.

### Scopes

A closed vocabulary of four (`apiplatform/scopes.Scope`) at the 02:50 read; seven
after the all-models wave, which adds `embeddings.write`, `rerank.write` and
`audio.write` (§13.5). An unknown scope
string stored on a key or a service account makes that key answer 401, with an
ERROR log line. Scopes imply nothing: `responses.write` does not grant
`responses.read`.

| scope | grants |
|---|---|
| `models.read` | `GET /v1/models`, `GET /v1/models/{model}` |
| `responses.write` | `POST /v1/responses`, `POST /v1/responses/{id}/cancel`, `POST /v1/chat/completions` |
| `responses.read` | `GET /v1/responses/{id}` |
| `usage.read` | `GET /v1/usage` |

A key created without explicit scopes gets `models.read`, `responses.read` and
`responses.write` (`scopes.DEFAULT_SCOPES`) — not `usage.read`.

A missing scope is `403 insufficient_scope`; the body names the required scope
and the response carries
`WWW-Authenticate: Bearer error="insufficient_scope", scope="<scope>"`.

## 3. Headers on every response

| header | when | value |
|---|---|---|
| `X-Request-Id` | every `/v1` response, including a 404 for an unknown path, a 405 for a wrong method and a 413 from the body-size middleware (verified by probe) | `req_<32 hex>`, the same id the error envelope's `request_id` carries and the usage ledger's `meta.request_id` records |
| `RateLimit-Policy` | authenticated responses only | `"requests";q=<rpm>;w=60, "concurrency";q=<max_concurrency>;qu="concurrent-requests"` (draft-ietf-httpapi-ratelimit-headers-11) |
| `RateLimit` | authenticated responses only | `"requests";r=<remaining>;t=<seconds left in the minute>`; `r` is the smaller of the project's and the key's remainder; `r=0` on a refusal |
| `Retry-After` | every 429 and 503 | integer seconds, minimum 1, jittered by up to `PUBLIC_API_RATELIMIT_JITTER_SECONDS` |
| `Allow` | a 405 | the methods the path accepts |
| `WWW-Authenticate` | `403 insufficient_scope` only | see §2 |
| `Access-Control-Allow-Origin` | when the request carried `Origin` | the request's own `Origin`, echoed; never `*` on an actual request |
| `Vary: Origin`, `Access-Control-Expose-Headers: X-Request-Id, RateLimit, RateLimit-Policy, Retry-After` | when the request carried `Origin` | — |
| `Access-Control-Allow-Credentials` | **never** | — |

The token limits (input and output tokens per minute, daily tokens) are
enforced but not advertised in `RateLimit-Policy`: the draft has no unit for
LLM tokens (`quotas.limit_headers`).

### CORS

`OPTIONS /v1/{anything}` answers **204** to any origin, with
`Access-Control-Allow-Methods: GET, POST, OPTIONS`,
`Access-Control-Allow-Headers: authorization, content-type, idempotency-key` and
`Access-Control-Max-Age: 600` (verified by probe). The preflight reads no
credential.

The **actual** request is authorised: when it carries `Origin` and the project's
`allowed_origins` is non-empty, an origin not in the list is
`403 origin_not_allowed`. A server-to-server call sends no `Origin` and is
unaffected. An empty `allowed_origins` allows every origin.

## 4. The order of checks, as implemented

`POST /v1/responses` and `POST /v1/chat/completions` (`router._generate` and
the two route functions):

```
resolve the key (401)                    router.resolve_caller → resolver
  → scope (403 insufficient_scope)       router._authorize
  → origin (403 origin_not_allowed)      router._authorize
  → body size (413), JSON, validation,
    estimated prompt over the input
    ceiling — all DEFERRED, not raised   router._parse_generating
  → rpm (project, then key) / input tpm /
    output tpm / daily quota (429)       quotas.reserve — atomic per project
  → the deferred 400/413, if any         raised now, after it was counted
  → model allowlist (404)                router._resolve_model
  → max_output_tokens ceiling (400)      router._max_tokens (model and project)
  → Idempotency-Key: 400 malformed,
    409 conflict, 429 still running,
    or a replay                          router._claim_idempotency / _replay
  → concurrency slot (429)               quotas.concurrency_slot / take_slot
  → durable api_responses row            router._claim_row, or background.start
  → generation                           streaming / background
  → one usage write + ledgers + row
    + the idempotency claim finished     router._recorder
```

A malformed or over-long request is deliberately counted against the
requests-per-minute limit before it is refused, so a flood of bad requests is
throttled like any other. Anything that fails **after** the reservation and
before generation (`_nothing_ran`) returns the unspent input-token reservation
and releases the idempotency claim, so a request refused for concurrency leaves
no row and does not poison its `Idempotency-Key`.

The concurrency slot is taken **before** the durable row is written in every
mode: for a streaming response inside `_SlotStream.__call__`, before the status
line, so the refusal is still a real 429; for background work inside
`background.start`.

The read routes (`GET /v1/models`, `GET /v1/models/{model}`,
`GET /v1/responses/{id}`, `POST /v1/responses/{id}/cancel`, `GET /v1/usage`)
resolve the key, check scope and origin, and reserve one request of kind
`read` — **reads count against requests per minute** and spend no tokens.

## 5. The error envelope

Every failure raised inside a `/v1` handler is rendered by `PublicRoute` as:

```json
{"error": {"message": "…", "type": "…", "code": "…", "param": null, "request_id": "req_…"}}
```

Five keys, always present. `param` is echoed only when it matches
`^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$`; otherwise it is `null`. `message` passes
through `errors.redact()`, which removes tracebacks, SQL statements,
`NAME=value` assignments, API-key tokens, internal URLs, private IPv4 addresses
and absolute container paths. An exception the code did not construct is
replaced wholesale by `internal_error` and logged server-side against the
request id (`errors.from_unexpected`).

The complete vocabulary (`errors._CODES`); constructing an `ApiError` with any
other code raises:

| code | HTTP | `type` | retryable | raised by |
|---|---|---|---|---|
| `invalid_request_error` | 400 (404 or 405 for an unknown path or method) | `invalid_request_error` | no | body not an object, not JSON, a validation failure, an unsupported field, `max_output_tokens` over the model or project ceiling, a project whose `max_output_tokens` is 0, a malformed `Idempotency-Key` (1–255 printable ASCII), a bad or over-long usage date range; an unknown `/v1` path (status 404) or method (status 405, with `Allow`) |
| `context_length_exceeded` | 400 | `invalid_request_error` | no | the prompt's estimate (three characters per token, `context.estimate_messages`) is over the smaller of the model's and the project's `max_input_tokens`; `param: input` |
| `invalid_api_key` | 401 | `authentication_error` | no | every rung of §2 |
| `insufficient_scope` | 403 | `permission_error` | no | `SCOPES[operation].check` |
| `origin_not_allowed` | 403 | `permission_error` | no | `_authorize` |
| `model_not_found` | 404 | `invalid_request_error` | no | unknown model, a model outside the key's allowlist, or one disabled by a `public_models` row — indistinguishable on purpose |
| `response_not_found` | 404 | `invalid_request_error` | no | `GET`/`cancel` on an id that is not this project's |
| `idempotency_conflict` | 409 | `invalid_request_error` | no, unless it carries `Retry-After` | same `Idempotency-Key`, different body fingerprint; also "a request with this Idempotency-Key is still running" (`Retry-After: 2`) |
| `request_too_large` | 413 | `invalid_request_error` | no | the middleware's `Content-Length` or counting check, or `_json_body` |
| `rate_limit_error` | 429 | `rate_limit_error` | yes | only with `PUBLIC_API_ENFORCE_LIMITS=true`: the project's or the key's requests per minute, input or output tokens per minute |
| `quota_exceeded` | 429 | `rate_limit_error` | yes | only with `PUBLIC_API_ENFORCE_LIMITS=true`: daily token quota; `Retry-After` runs to the next UTC midnight |
| `concurrency_limit_exceeded` | 429 | `rate_limit_error` | yes | only with `PUBLIC_API_ENFORCE_LIMITS=true`: the project's in-flight slots or the key's own share (`Retry-After` about 1 s) |
| `model_recovering` | 503 | `service_unavailable_error` | yes | breaker open / queued for recovery while the engine controller does not report DOWN or WEDGED |
| `model_unavailable` | 503 | `service_unavailable_error` | yes | the same, when the controller reports DOWN or WEDGED; the shared admission lanes refusing (`AdmissionRejected` → `Retry-After: 5`, "at capacity"); also the code a restart-orphaned background response is failed with |
| `timeout` | 504 | `timeout_error` | yes | an `asyncio.TimeoutError` out of the generation |
| `internal_error` | 500 | `server_error` | no | anything else, including a deployment with no usable API-key pepper (§12) |

Every 429 and 503 carries `Retry-After`; the constructor refuses to build one
without it.

## 6. Endpoints

### `GET /v1/models` — `models.read`

Only the models this key may use: the code-level registry
(`registry.declared_models()`), minus any id a `public_models` row disables
**for this key's workspace** (the table is keyed by `(workspace_id, id)`),
minus anything outside the key's allowlist. If the `public_models` read fails,
the declared set is served unchanged — a database failure can never add a model.

```json
{"object": "list", "data": [
  {"id": "techsara-35b", "object": "model", "owned_by": "techsara", "status": "available",
   "capabilities": {"chat": true, "streaming": true, "vision": <bool>, "tools": false, "embeddings": false},
   "max_input_tokens": <int>, "max_output_tokens": <int>}
]}
```

`max_input_tokens` and `max_output_tokens` are read from settings at call time
(`registry._main_limits`), so they follow the served deployment. `vision` is
advertised from the model's capabilities, but `/v1` accepts text content only
(§6.3). The internal checkpoint name is never rendered. **Superseded by the
all-models wave**: six ids, a wider model object and image input (§13.1, §13.2).

### `GET /v1/models/{model}` — `models.read`

One model object as above, or `404 model_not_found`.

### `POST /v1/responses` — `responses.write`

Request body (`models.ResponsesRequest`, `extra="forbid"`, strings stripped):

| field | type | rule | failure |
|---|---|---|---|
| `model` | string | 1–64 chars, `^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$`, and resolvable for this key | 400; 404 `model_not_found` |
| `input` | string, or list of `{role, content}` | non-empty; `role` ∈ `system`, `user`, `assistant`; `content` a non-blank **string** (no multimodal parts) | 400 |
| `instructions` | string, optional | non-blank when present; sent as the first system message | 400 |
| `stream` | bool, default false | not together with `background` | 400 |
| `background` | bool, default false | not together with `stream` | 400 |
| `max_output_tokens` | int, optional | ≥ 1; the ceiling is the smaller of the model's and the project's `max_output_tokens`; an explicit value over it is refused, not clamped; absent → `min(8192, ceiling)` | 400 `param: max_output_tokens` |
| `temperature` | number, optional | 0.0–2.0; absent → 0.2 | 400 |
| `metadata` | object of string → string | ≤ 16 keys, key 1–64 chars, value ≤ 512 chars | 400 |

Any other field — `top_p`, `tools`, `n`, `seed`, `workspace_id`, … — is a 400
naming the field. The body is never echoed in an error.

Every request runs at effort `fast` (thinking off, `streaming.PUBLIC_EFFORT`).

Optional header `Idempotency-Key` (§8).

**Synchronous** (`stream` and `background` false): `200` with the response
object; an engine failure is raised as its HTTP error (503, 504, 429), not a 200
carrying a failure.

```json
{"id": "resp_<24 hex>", "object": "response", "created_at": 1789200000,
 "status": "completed", "model": "techsara-35b",
 "output": [{"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "…"}]}],
 "usage": {"input_tokens": 37, "output_tokens": 112, "total_tokens": 149}}
```

`usage` is `null` when the engine reported no counts. `output` is `[]` when no
text was produced. A failed response additionally carries
`"error": {"code": "…", "message": "…"}`.

**Streaming** (`stream: true`): `200 text/event-stream`, §7.

**Background** (`background: true`): `202` with the stored row rendered as a
response object (`status: "queued"`, `output: []`, `usage: null`). The work
runs in a detached task that survives the client disconnecting; the outcome is
read with `GET /v1/responses/{id}` and, when the project has an endpoint,
delivered by webhook (OPERATIONS.md §6). A background request takes one of the
project's concurrency slots — the same slots synchronous and streaming requests
use (§9) — for the whole job, and is refused with
`429 concurrency_limit_exceeded` when none is free.

### `GET /v1/responses/{id}` — `responses.read`

The `api_responses` row for this project, rendered as a response object. An id
belonging to another project is `404 response_not_found`, never 403.

What the row can and cannot give back:

* `output` is populated **only for background responses** — a synchronous or
  streamed answer's text is not stored (CONTRACT §16), so it reads back with its
  status and counts and `output: []`;
* `usage` is present when either token column is non-null, else `null`;
* `status` is one of `queued`, `in_progress`, `completed`, `failed`,
  `cancelled`;
* a **background** row still `queued` or `in_progress` that was created before
  this process started and is not running here is closed as `failed` with
  `model_unavailable` at the moment it is read (`background.repair_if_orphaned`)
  — the restart repair happens lazily, on the read path.

The row is deleted when it passes its `expires_at` (the project's
`retention_days`, default 30) **and** the pruning job runs — see OPERATIONS.md
§7 for the state of that job.

### `POST /v1/responses/{id}/cancel` — `responses.write`

Idempotent (`background.request_cancel`):

* already `completed`, `failed` or `cancelled` → the row, unchanged, 200;
* a background job running in this process → sets `cancel_requested`; the job
  stops at its next chunk and writes `cancelled` itself, keeping the text and
  usage it already produced; the 200 body is the row at the moment of the call;
* no job in this process and the row still open → the row is closed as
  `cancelled` immediately;
* not this project's id → `404 response_not_found`.

Cancellation stops **background** work only. A synchronous or streaming request
has no job entry, so its row is marked `cancelled` while the generation keeps
running, and the generation's own finish then overwrites the status (§12).

### `POST /v1/chat/completions` — `responses.write`

The compatibility shape, translated into `ResponsesRequest` so there is one
validator, one quota gate, one generator and one ledger write
(`router._from_chat_completions`).

Accepted fields, and nothing else (any other top-level field is
`400 Unsupported field: <name>`):

| field | maps to |
|---|---|
| `model` | `model` |
| `messages` | `input` (same role and string-content rules) |
| `stream` | `stream` |
| `max_tokens` | `max_output_tokens` |
| `temperature` | `temperature` |
| `stream_options` | only `include_usage` is accepted |

There is no `background` on this endpoint.

Non-streaming `200`:

```json
{"id": "chatcmpl_<24 hex>", "object": "chat.completion", "created": 1789200000,
 "model": "techsara-35b",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "…"}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 37, "completion_tokens": 112, "total_tokens": 149}}
```

`usage` is `null` when not measured. `finish_reason` is `"length"` when the
engine reported a length stop, otherwise `"stop"` (`StreamOutcome.chat_finish_reason`).
The completion id is derived from the response id
(`chatcmpl_<the same 24 hex>`), so a replay returns the same id. A durable `api_responses` row is written for this endpoint too,
under the route name `v1_chat_completions`.

### `GET /v1/usage` — `usage.read`

This project's daily counters from `api_usage_daily`.

Query: `start_date`, `end_date` — ISO `YYYY-MM-DD`, inclusive. `end_date`
defaults to today (the server's local date via `date.today()`), `start_date` to
29 days before `end_date`. The range may be at most 93 days; `end_date` before
`start_date` or a malformed date is 400.

```json
{"object": "list", "start_date": "2026-08-15", "end_date": "2026-09-13",
 "data": [{"date": "2026-09-13", "requests": 12, "input_tokens": 3400,
           "output_tokens": 9100, "errors": 1, "rate_limited": 0}]}
```

Days with no activity have no row. `errors` counts requests whose final status
was not `completed` or `cancelled`; `rate_limited` counts refusals, which do not
consume allowance.

### `GET /v1/openapi.json` — no credential

The public OpenAPI **3.1** document, built by `publicapi/openapi.py` from the
error table, the scope vocabulary and the registry — never `app.openapi()`.
CI checks its operation set against `.github/workflows/scripts/public-api-surface.txt`
(§11).

### `OPTIONS /v1/{rest:path}` — no credential

The preflight of §3. Not in the OpenAPI document.

## 7. Streaming

Response headers: `Content-Type: text/event-stream`,
`Cache-Control: no-store, no-cache, no-transform`, `X-Accel-Buffering: no`,
`Connection: keep-alive`, plus `X-Request-Id` and the `RateLimit` headers.

The concurrency slot (project, and the key's share) is taken **before** the 200 is committed, so a caller at
its limit gets a real `429 concurrency_limit_exceeded` rather than a stream that
dies. The slot is released in a `finally` when the body ends, fails or the
client goes.

### `POST /v1/responses` with `stream: true`

Named events; each `data` is JSON whose `type` repeats the event name and whose
`sequence_number` starts at 1 and rises by exactly 1 (`events.SequencedEvents`
refuses to emit anything else):

```
response.created
[response.queued]              only when the engine is recovering at the start
response.in_progress
response.output_text.delta     ×N  {item_id, output_index, content_index, delta}
response.output_text.done           {item_id, output_index, content_index, text}
response.completed                  {response: <response object with usage>}
```

On any failure after the stream has started, the tail is replaced by exactly one
`response.failed` whose `response` carries `status: "failed"`, the partial
text, partial usage and `error: {code, message}`. The grammar also defines an
`error` terminal event (`{type, code, message, param, sequence_number}`), but
`streaming.responses_sse` does not emit it today.

Rules the emitter enforces: exactly one terminal; the lifecycle only moves
forward; `usage` is `null` on every non-terminal lifecycle event. Heartbeats are
SSE comments (`: ping`), carry no sequence number, and go out at least every
`min(SSE_HEARTBEAT_SECONDS, 15)` seconds while the engine is silent.

A client that disconnects gets no terminal frame; the generator is closed and
its usage is still recorded server-side.

### `POST /v1/chat/completions` with `stream: true`

Anonymous `data:` chunks of `object: "chat.completion.chunk"`, no `event:` line,
no sequence numbers:

1. deltas — the first carries `delta: {role: "assistant", content}`, the rest
   `delta: {content}`, `finish_reason: null`, `usage: null`;
2. one chunk with `delta: {}` and `finish_reason` of `"length"` when the engine
   reported a length stop, otherwise `"stop"`;
3. only when `stream_options.include_usage` was true: one chunk with
   `choices: []` and `usage: {prompt_tokens, completion_tokens, total_tokens}`
   (or `null` when not measured);
4. `data: [DONE]`.

On failure: one chunk with `choices: []` and
`error: {message, type, code, param}`, then `data: [DONE]`.

## 8. Idempotency

`Idempotency-Key` on either POST: 1–255 printable ASCII characters (a present
but blank or malformed header is a 400, not "no key"), scoped by
`(project_id, endpoint, key)` where `endpoint` is `v1_responses` or
`v1_chat_completions`, retained for `PUBLIC_API_IDEMPOTENCY_TTL_HOURS`
(default 24). The body fingerprint is SHA-256 of the canonical JSON (sorted
keys, tight separators, UTF-8).

* **First use** wins the claim; the request runs; the claim is finished with the
  response id when the response ends, or with no id when it failed.
* **Same key, different body**: `409 idempotency_conflict`.
* **Same key, same body, original still running**: `409 idempotency_conflict`,
  `param: Idempotency-Key`, `Retry-After: 2`. A claim left `in_flight` by a
  process that died is taken over after
  `PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS` (default twice the generation
  wall clock, at least an hour).
* **Same key, same body, original failed without a durable response**: the
  request runs again — that is not a double invocation.
* **Same key, same body, original finished** (`router._replay`):
  * a background original → `202` with its row;
  * a failed synchronous original → the original error, re-raised;
  * a finished original replayed as `stream: true` → a short stream rebuilt from
    the row (`response.created` then `response.completed` or `response.failed`;
    for chat completions the stop or error chunk, the usage chunk if asked for,
    and `[DONE]`). The deltas are not replayed, and text is present only where
    it was stored (background rows);
  * otherwise → `200` with the row as a response object, or as a
    `chat.completion` on the compatibility endpoint (content `null` when no text
    was stored, `finish_reason` `"stop"`).

A replay spends no tokens. The request it arrived as was counted against
requests per minute before the claim was read, and its input-token reservation
is returned.

## 9. Limits, as enforced

Per project, from the `api_projects` row; a key's own `rpm` and
`max_concurrency`, where set, add a second, tighter limit for that key
(`resolver._effective_limits`); the `PUBLIC_API_DEFAULT_*` settings are the
fallback for a column that does not answer.

| limit | default | how |
|---|---|---|
| requests / minute | 60 per project; optional per key | sliding window over `api_usage_minute`, decided and bumped in one transaction under `pg_advisory_xact_lock` on the project (`quotas.reserve`); every authenticated request counts, reads included |
| input tokens / minute | 200,000 | the prompt's estimate is reserved at admission; the unspent part is returned when the real count is recorded |
| output tokens / minute | 60,000 | checked against tokens **already** spent in the window, so it throttles the request after the one that crossed the line |
| daily tokens | 2,000,000 | `api_usage_daily`, UTC day |
| concurrent requests | 4 per project; optional per key | process-local counters under one lock (`quotas.take_slot`), shared by synchronous, streaming and background work; **0 refuses every request** |
| input tokens per request | smaller of the model's and the project's `max_input_tokens` | estimated before admission → `context_length_exceeded` |
| output tokens per request | `min(8192, ceiling)` by default; explicit values up to the smaller of the model's and the project's ceiling | `ResponsesRequest.resolve_max_output_tokens` |
| body | 1 MiB | `PUBLIC_API_MAX_BODY_BYTES`, before parsing, at both the Next edge and the orchestrator |

A refusal increments `api_usage_daily.rate_limited` and consumes nothing. Token
counts are written once per request, from what the engine reported. For a
request whose usage was not measured, the input-token estimate reserved at
admission stays counted and **no output tokens** are added (`quotas.record_usage`).

## 10. What is recorded per request

* one `usage_events` row through `usage.record_async`: `route` `v1_responses` or
  `v1_chat_completions`, `mode` `api`, `user_id` null, the workspace, model,
  token counts (null when not measured), TTFT, duration, status, error code, and
  `meta` = `{api_key_id, project_id, request_id, streamed}`;
* the minute and daily ledgers (`quotas.record_usage`);
* the `api_responses` row: status, duration, TTFT, token counts when measured,
  error code and a redacted error message, `metadata`, `instructions_present`
  (never the instructions), and `output_text` for background responses only.

No prompt is stored anywhere.

## 11. How the surface is held in place

* `.github/workflows/scripts/api_contract.py` builds the public document in a
  child interpreter, checks it is well-formed OpenAPI 3.1 and diffs its
  operations against `public-api-surface.txt`. Run locally on 2026-09-13:
  `OpenAPI 3.1.0: well-formed, 8 operation(s), all matching …public-api-surface.txt`.
* `errors._CODES` is closed; `scopes.Scope` is closed; `registry.guard_internal_target`
  refuses, at construction, a `PublicModel` whose target is the router,
  embeddings, OCR, reranker or ASR engine by name, URL, `host:port` or bare host.
* The orchestrator suites that pin this behaviour are
  `tests/test_publicapi_{routes,streaming,background,contract,mount}.py` and
  `tests/test_api_platform_{resolver,quotas,keys,webhooks,db,console}.py`.
  **They were not run as part of writing this document**; see DISPOSITION.md
  for what was and was not executed.

## 12. Where the code and the contract disagree

Each of these is reported to the programme as a finding. None is fixed by this
document. Status as of the 02:50 IST re-read; the first draft of this list had
sixteen items, and a second wave closed seven of them while it was being
written — those are listed at the end so the history is visible.

**Still open**

1. **Disabled workspace is 401, not 403.** CONTRACT §4 says 403; `resolver.py`
   answers `401 invalid_api_key` on purpose so a key holder cannot learn that
   the tenant exists. The contract has not been amended.
2. **The resolver has a rung the contract does not.** The project `ip_allowlist`
   refuses with 401 (§2 step 9); CONTRACT §4 does not list it.
3. **Admission runs before validation and before the model check.** CONTRACT §4
   orders scopes → model allowlist → quota → request validation; the code counts
   the request against quota first and raises a validation, size or context
   error afterwards, and resolves the model after that (§4). Deliberate — a bad
   request is not free — but not what the contract says.
4. **`RateLimit` headers are not on every `/v1` response.** CONTRACT §12 says
   every one; `quotas.limit_headers` needs an authenticated caller, so 401s,
   preflights, 404s and 405s, `/v1/openapi.json` and middleware 413s have none.
5. **An idempotent retry does not attach to a running request.** CONTRACT §13
   says the caller attaches; the code answers `429 rate_limit_error` with
   `Retry-After: 2` until the original finishes (§8).
6. **Cancel does not stop synchronous or streaming generations.** Only
   background jobs are registered in `background._jobs`; for any other open row
   `request_cancel` writes `cancelled` while the generation keeps running, and
   the generation's own finish then writes its terminal status over it.
7. **A client that disconnects mid-stream is recorded as `completed`.**
   `StreamOutcome.client_gone` is set in `streaming.responses_sse` and
   `chat_completions_sse` but the status is not changed; only a stream abandoned
   before its first byte is recorded `cancelled` (`router._record_abandoned`).
8. **`models.max_body_bytes()`'s docstring is stale.** It says there is no
   `public_api_max_body_bytes` setting and no `PUBLIC_API_MAX_BODY_BYTES`
   variable; `app/config.py` defines both, so the cap is configurable.
9. **`concurrency_limit_exceeded` also means "the shared lanes are full".**
   CONTRACT §9 defines it as the caller's own in-flight limit; an
   `AdmissionRejected` from the chat application's shared lanes is reported
   with the same code.
10. **A missing or too-short pepper is a 500, not a 401.** With no usable
    pepper a well-formed key reaches `keys.verify_secret`, which raises
    `PepperUnavailable`; the resolver lets it propagate and the caller gets
    `500 internal_error`. Deliberate in `resolver.py`; not in the contract's
    table.
11. **Model exposure is per workspace.** CONTRACT §15 describes `public_models`
    as a deployment-wide narrowing; V34 keys it by `(workspace_id, id)`, so a
    super admin's "disable" applies to their workspace only.
12. **The replayed stream is not the original stream.** A `stream: true` replay
    emits `response.created` and a terminal only — no `in_progress`, no deltas
    (§8) — so a client that renders deltas shows nothing for a replayed
    synchronous or streaming original.

**Closed by the second wave while this was written** (each checked in the tree
or by probe at the re-read): `context_length_exceeded` is now raised;
every `/v1` response including middleware 413s carries `X-Request-Id`; an
unknown `/v1` path or method answers 404 or 405 in the envelope; a replayed
chat completion returns a `chat.completion`; non-streaming chat completions
report `"length"` when the engine did; key rotation has an overlap window
(`overlap_hours`, default `PUBLIC_API_KEY_ROTATION_OVERLAP_HOURS` = 168, at
most 30 days); a concurrency 429 no longer leaves a `queued` row or poisons its
`Idempotency-Key`.

## 13. The all-models wave (2026-09-13) — as designed, to be re-read from the code

Binding design: the architect's brief of 2026-09-13 and `CONTRACT.md` §7–§16,
which were amended first. Nothing in this section was read out of code; each
item names the symbol to confirm once the wave has merged.

### 13.1 The catalogue

| public id | engine key | kind | reached through | declared when | confirm in |
|---|---|---|---|---|---|
| `techsara-35b` | `main` | chat | `llm.stream_chat_events` | always | `registry.declared_models` |
| `techsara-8b-vision` | `router` | chat | `publicapi/engines.stream_chat`, gate `router` | `router_base_url` set and ≠ `openai_base_url` | same |
| `techsara-ocr` | `ocr` | chat | `publicapi/engines.stream_chat`, gate `ocr` | `settings.ocr_enabled` | same |
| `techsara-embed` | `embed` | embedding | `publicapi/sidecars`, gate `embed` | `embed_base_url` set | same |
| `techsara-rerank` | `rerank` | rerank | `publicapi/sidecars` → `/score`, gate `rerank` | remote rerank backend with a base URL | same |
| `techsara-whisper` | `asr` | transcription | `publicapi/sidecars` → whisper replica, gate `asr` | `settings.asr_enabled` | same |

`registry.catalogue()` returns all six with `status` `available` or
`not_configured` (the console lists it); `declared_models()` returns only the
available ones (`/v1/models`). A `public_models` row still only narrows. The
model wire object gains `kind`, the capability flags `rerank`,
`audio_transcription`, `ocr`, `background`, `endpoints`, `context_window`,
nullable `max_input_tokens` / `max_output_tokens`, `default_max_output_tokens`
and `limits`. Internal names, engine keys and URLs never render
(`PublicModel.to_wire`). `guard_public_id` enforces
`^techsara-[a-z0-9]+(-[a-z0-9]+)*$`; `guard_internal_target` refuses an
address-shaped internal target and an engine key outside the closed set.

Ceilings per model are CONTRACT §12.2. Three `generated.env` values disagree
with the running engines and must not be trusted by the registry:
`ROUTER_CONTEXT_LENGTH=65536` (engine 49,152), `EMBED_CONTEXT_LENGTH` and
`RERANKER_CONTEXT_LENGTH=32768` (engines 4,096).

### 13.2 Generation: planning, clamp, wall clock

`publicapi/planning.plan_generation(request_model, model, *,
project_max_output_tokens)` → `GenerationPlan` (requested / planned / engine
`max_tokens`, estimated and bounded input, footprint, `wall_clock_s`,
temperature, `clamped`, gate). Order: explicit value over the ceiling → 400;
input over `max_input_tokens` → 400 `context_length_exceeded`; otherwise clamp
`planned = max(1, min(requested, window − input − reserve))`; applied value from
`llm.get_applied_max_tokens()` when `llm.py` exposes it, else the post-hoc formula
of CONTRACT §8.3. Wall clock `min(PUBLIC_API_GEN_WALL_CLOCK_S, max(floor,
prefill + planned / min_tps))`. Confirm in `planning.py`, and in
`streaming.Generation` for the `wall_clock_s + 30 s` backstop and the
`inspect.signature` feature-detection of `stream_chat_events(wall_clock_s=…,
wall_clock_marker=False)`.

**`llm.py` accepts `wall_clock_s` (integration 2026-09-13)**, so a `techsara-35b`
generation runs to its planned wall clock rather than `GEN_WALL_CLOCK_S`
(4,200 s), with a per-request read timeout of the same length when that is longer
than `LLM_REQUEST_TIMEOUT`. `LONG_OUTPUT_WALL_CLOCK_LIVE` in
`frontend/content/docs/samples.ts` is `true`, and
`frontend/tests/docs-site.test.tsx` holds it to `llm.py`'s signature.
`llm.get_applied_max_tokens()` is the `max_tokens` `_fit` sent (or the
forced-closure retry's). Pinned by `tests/test_llm_per_request_wall_clock.py`.

Response object keys `max_output_tokens` (planned on early snapshots, applied on
the terminal) and `incomplete_details` (`{"reason": "max_output_tokens"}` on a
`length` stop; `status` stays `completed`). `chat.completion` and the finish chunk
gain `max_output_tokens`. Chat Completions accepts `max_completion_tokens`.
V35: `api_responses.max_output_tokens`, `api_responses.finish_reason`.

Image input on the two generation routes: `data:` URLs only (png, jpeg, webp,
gif; magic bytes checked; ≤ 10 MiB decoded, `413`), per-model count, `user`
role only; body cap `PUBLIC_API_MAX_MEDIA_BODY_BYTES` 20 MiB with the 1 MiB
text rule after parsing; JSON above 64 KiB parsed off the event loop. On
`techsara-ocr` with no text and no instructions the server appends `OCR`.
Confirm in `publicapi/models.py`.

### 13.3 Capacity gates

`publicapi/capacity.hold(engine, *, weight_tokens, wait_s, yield_to_chat)`: per
engine, FIFO, bounded wait, refusal `errors.model_at_capacity(retry_after)` (503
`model_unavailable`). Numbers and their arguments: CONTRACT §12.3. Sync, stream
and sidecar requests wait ≤ `PUBLIC_API_GATE_WAIT_S` (30 s) **before** the status
line; background jobs wait ≤ `PUBLIC_API_BACKGROUND_GATE_WAIT_S` (3,600 s) inside
the job while `queued`. Chat-app paths never take a gate. Gauges
`public_api_engine_in_flight` / `public_api_engine_waiting` by engine.
`main.long` applies above a 131,072-token footprint (input at its byte bound +
planned output); `main.extended` (2 at a time) to any other flagship request
planning more than 8,192 output tokens; at or under 8,192 the shared admission
NORMAL lanes remain the only gate. Both main gates step aside while a chat
request is in the LONG admission lane. Router gate weights are also byte-bound.
Changed after the adversarial review of 2026-09-13 (CONTRACT §12.3).

A synchronous generation watches its connection
(`streaming.run_to_completion_watching`) and is cancelled at a disconnect. A
wall-clock stop records server-counted usage (`usage_source: counted_at_stop`).
The main engine's one window retry after an estimated clamp: CONTRACT §8.3.

### 13.4 The three new routes

`publicapi/endpoints.register(router)` is called from the bottom of `router.py`
inside an import guard, so an import failure leaves the eight older routes
working. Request and response shapes: CONTRACT §8.4–§8.6. Each resolves the key,
checks scope then origin, reserves one `sync` request, resolves the model and
checks its kind, refuses `Idempotency-Key` with 400, writes no `api_responses`
row, and records one `usage_events` row (routes `v1_embeddings`, `v1_rerank`,
`v1_audio_transcriptions`; meta in CONTRACT §16). The multipart body is parsed
in memory (`publicapi/multipart.py`), never through `UploadFile`.

### 13.5 Scopes

`Scope.EMBEDDINGS_WRITE`, `RERANK_WRITE`, `AUDIO_WRITE`, all in
`DEFAULT_SCOPES`; `usage.read` stays opt-in. Stored keys keep their stored
scopes, so keys created before the wave answer `403 insufficient_scope` on the
new routes. `responses.write` covers all three chat-kind models.

### 13.6 Idempotency lease

`apiplatform/idempotency.py`: in-flight lease `max(configured,
2 × PUBLIC_API_GEN_WALL_CLOCK_S + PUBLIC_API_BACKGROUND_GATE_WAIT_S)` = 46,800 s
(13 h) by default, below the 24 h TTL. Consequence documented publicly: a claim
orphaned by a restart answers `409` for up to 13 h, so a client retries a
restart-failed long job with a new key.

### 13.7 Outside this wave's reach (integration items)

* ~~`config.py` declares the `PUBLIC_API_*` settings of CONTRACT §12.4 (until
  then readers fall back to `os.environ` with the same parse rules).~~ Landed
  (integration 2026-09-13), with the three webhook settings, except
  `PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS`; pinned by
  `tests/test_public_api_settings_declared.py`.
* ~~`llm.py`: `wall_clock_s` / `wall_clock_marker` on `stream_chat_events`,
  `get_applied_max_tokens()`.~~ Landed (integration 2026-09-13).
* ~~`.github/workflows/scripts/public-api-surface.txt` gains the three routes; the
  `api_contract` gate is red until it does.~~ Landed (integration 2026-09-13,
  owner-approved): the surface file lists all eleven operations.
* ~~`main.py`: `body_cap_for` uses `publicapi.models.body_cap_for(method, path)`
  for `/v1` (20 MiB generation routes, 26 MiB transcriptions); until then the
  mounted app refuses image and audio bodies over 1 MiB.~~ Landed (integration
  2026-09-13): `main._public_api_body_cap`; the strict xfail in
  `test_publicapi_mount.py` is a plain test now.
* `admission.py`: `_ahead` leaves out `capacity.public_long_lived_decoding()`,
  so a chat LONG request is not held for the whole idle wait by a decoding
  public job (strict xfail in `test_publicapi_main_engine_priority.py`).
* `asr.py`: `RoutedProvider.reserve(index)`, the clean seam for
  `sidecars.counted_in_dictation_routing` (which uses `_active` until then).
* `scripts/docs_examples_run.py` needs specs for the new and re-ordered example
  blocks before the examples can be run and `EXAMPLES_EXECUTED` flipped.
* Engine-level priority scheduling (`--scheduling-policy priority`) is an operator
  decision and the real fix for KV pressure on the main engine.
* A deploy restarts the orchestrator and fails every running multi-hour
  generation; `scripts/deploy.sh` should wait or warn while
  `public_api_engine_in_flight{engine="main.long"} > 0`.

### 13.8 New disagreements to watch

13. **`status` stays `completed` on a `length` stop.** OpenAI-shaped clients
    expect `incomplete`; V34's status CHECK has no such value, so the signal is
    `incomplete_details`. Documented publicly as a deviation.
14. **Request Logs miss three routes.** The console's request log reads
    `api_responses`; embeddings, rerank and transcription calls count in usage
    but have no row there.
15. **Audio seconds are not in `GET /v1/usage`.** They are in
    `usage_events.meta.audio_seconds` only.
16. **The OCR engine's own default prompt is still `document parsing`**
    (`engines/ocr.py` `_DEFAULT_PROMPT`, overridable by `OCR_PROMPT`), the prompt
    that loops; the public path appends `OCR` instead. The chat application's
    read path is unchanged by this wave.
