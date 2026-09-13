# CONTRACT-3 — the TechSara developer platform

The single source of truth for the public developer API, the console at `/api`,
the documentation at `/docs`, and the capability model behind them. Every
implementation wave builds against this file; a change here is a change of
contract and must be made here first.

Grounded in the read-only audit of 2026-09-12 (`AUDIT.md`, 300 first-party files)
and the primary-source rules in `STANDARDS.md`. Every "reuse X" below names a
symbol that exists today at the cited path.

---

## 1. Trust boundaries

Three surfaces, three different credentials, no overlap:

| surface | reached at | credential | may not |
|---|---|---|---|
| chat app | `https://ai.techsarasolutions.com/` | `ts_session` cookie (HttpOnly, SameSite=Lax) | be driven by an API key |
| developer console | `https://ai.techsarasolutions.com/api` | the same session **plus** `api.console.access` | be reached by a member without the capability |
| public developer API | `https://ai.techsarasolutions.com/v1/…` (and later `https://api.techsarasolutions.com/v1/…`) | `Authorization: Bearer <api key>` **only** | accept the session cookie |

**The API surface must never accept the session cookie.** A route that accepted
both would be a confused deputy: any page on the internet could drive it with the
user's cookie. `/v1` reads exactly one credential — the `Authorization` header —
and ignores `Cookie` entirely.

Internal services (vLLM main and router, embeddings, OCR, reranker, PostgreSQL,
pgAdmin, SearXNG, the engine controller, exporters) are never reachable from any
of the three surfaces. `/v1` never proxies a caller-supplied URL.

## 2. Where the code lives

| package | owns |
|---|---|
| `orchestrator/app/apiplatform/` | keys, projects, service accounts, scopes, quotas, idempotency, usage recording, webhooks |
| `orchestrator/app/publicapi/` | the `/v1` router: request/response models, the error envelope, streaming, background responses, the model registry, the OpenAPI document |
| `frontend/app/api/page.tsx` + `frontend/components/devplatform/` | the console |
| `frontend/app/docs/` | the documentation |
| `frontend/app/v1/[[...path]]/route.ts` | the same-origin public edge until `api.techsarasolutions.com` exists |

`orchestrator/app/main.py` and `orchestrator/app/db.py` are single-owner files:
one wave each, reviewed alone (see OWNERSHIP.md).

## 3. Mounting `/v1` (the two middlewares that would break it)

Audited facts: the app is `app = FastAPI(...)` at `main.py:219`; there is no
app-level dependency (`app.router.dependencies == []`); middleware order
outermost-first is `ServerErrorMiddleware → _reject_cross_site_writes →
CORSMiddleware → ExceptionMiddleware → router`.

1. **`_reject_cross_site_writes` (`main.py:243`)** 403s any non-GET whose
   `Origin` is not one of the three browser origins. A developer's browser app
   sends its own `Origin`, so every `POST /v1/responses` from a browser would be
   refused before the key is read. The middleware gains one exemption: paths
   under `/v1/` are skipped, because that surface is key-authenticated and
   cookie-blind, so CSRF does not apply to it.
2. **`CORSMiddleware` (`main.py:225`)** has a fixed three-origin allowlist with
   `allow_credentials=True`. It must not be widened. `/v1` does its own CORS:
   * preflight `OPTIONS /v1/*` answers 204 with `Access-Control-Allow-Origin`
     echoing the request `Origin`, `Allow-Methods: GET, POST, OPTIONS`,
     `Allow-Headers: authorization, content-type, idempotency-key`,
     `Max-Age: 600`. Preflight carries no credential and reveals nothing, so it
     is permissive by design;
   * the **actual** request is authorized: if it carries an `Origin` and the
     project has a non-empty `allowed_origins`, a non-matching origin is
     `403 origin_not_allowed`;
   * `Access-Control-Allow-Credentials` is **never** sent, so no browser can
     attach cookies to `/v1`.

## 4. Identity resolution

Every `/v1` request resolves, in this order, before any work:

```
Authorization: Bearer tsk_live_<public_id>_<secret>
  → key row (by public_id)            → 401 invalid_api_key if absent/revoked/expired
  → service account                   → 401 if disabled
  → project                           → 401 if disabled
  → workspace                         → 403 if the workspace is disabled
  → scopes ∩ endpoint requirement     → 403 insufficient_scope
  → model allowlist                   → 404 model_not_found (never 403: no existence disclosure)
  → quota + rate + concurrency        → 429 with Retry-After
  → request validation                → 400/413/422
  → execution
```

The resolved principal for the API is **not** a `Principal` (that is the browser
identity). It is `ApiCaller(workspace_id, project_id, service_account_id,
key_id, scopes, models, limits, environment)`.

## 5. API keys

Format, one line, three parts after the environment:

```
tsk_live_<public_id>_<secret><checksum>
tsk_test_<public_id>_<secret><checksum>
```

* `public_id` = `secrets.token_hex(8)` (16 hex chars) — the lookup key, stored in
  clear, safe to log;
* `secret` = `secrets.token_urlsafe(32)` (256 bits of CSPRNG entropy);
* `checksum` = 6 chars, Base62 of CRC32 over `public_id + secret`, validated
  offline before any database work (a mismatch is the same generic 401);
* prefix `tsk_live_` / `tsk_test_` so a scanner and a human can tell a live key
  from a test key at a glance.

**Storage**: `key_hash = HMAC-SHA256(pepper, secret)`, compared with
`hmac.compare_digest`. 256 random bits is far above the 112-bit line at which
NIST requires a slow password hash, so a keyed digest is correct and indexable —
the pepper is what stops an attacker who has only the table. The pepper comes
from `API_KEY_PEPPER` when set; when it is not, the platform generates one on
first use and stores it in `platform_secrets` (V34), which keeps a fresh install
working without a deploy-time secret and is documented as the weaker of the two
(same store as the digests). Never store, log, return or display the secret after
creation. `last_four` is kept for recognition. The shape follows
`app/sharing.py:45-98` (`mint_token`/`split_token`), which already does this for
share links.

Lifecycle: create → (rotate, optional overlap window) → revoke. Revocation is a
single `UPDATE`, and the resolver reads the row on every request, so it takes
effect immediately everywhere — there is no cached key state.

## 6. Capabilities (browser side)

Added to `Cap` in `orchestrator/app/authn/rbac.py` — a str-Enum, no migration.
`SUPER_ADMIN` is `frozenset(Cap)` and gets every one automatically; `ADMIN` gets
only what is listed in `_ADMIN_CAPS`.

| capability | super admin | admin | member |
|---|---|---|---|
| `api.console.access` | yes | yes | no |
| `api.projects.read` | yes | yes | no |
| `api.projects.manage` | yes | yes | no |
| `api.keys.create` | yes | yes | no |
| `api.keys.revoke` | yes | yes | no |
| `api.usage.read` | yes | yes | no |
| `api.logs.read` | yes | yes | no |
| `api.webhooks.manage` | yes | yes | no |
| `api.models.manage` | yes | **no** | no |
| `api.limits.manage` | yes | **no** | no |

Model exposure and platform-wide limits are super-admin only: an admin may run
projects, not decide which models exist publicly or lift a workspace's ceiling.
Capabilities are recomputed per request (`principal._build`), so a demotion takes
effect on the next request — there is no session-refresh window to document.

Refusal style follows the admin surface: a missing capability is **404**, not
403, so the console's existence is not disclosed to a member.

## 7. Public endpoints

Base: `/v1`. Every response carries `X-Request-Id`.

| method | path | scope | notes |
|---|---|---|---|
| GET | `/v1/models` | `models.read` | only models this key may use |
| GET | `/v1/models/{model}` | `models.read` | 404 when not permitted |
| POST | `/v1/responses` | `responses.write` | sync, streaming or background |
| GET | `/v1/responses/{id}` | `responses.read` | project-scoped |
| POST | `/v1/responses/{id}/cancel` | `responses.write` | idempotent |
| POST | `/v1/chat/completions` | `responses.write` | compatibility shape, same engine path |
| GET | `/v1/usage` | `usage.read` | project-scoped, bounded range |
| GET | `/v1/openapi.json` | none | the public schema only |

Not exposed, deliberately: Salesforce, RAG and web search, deep research,
uploads, artifacts, memory, conversation history, admin analytics. Each would
need its own product, scope and threat review.

## 8. Request contract (`POST /v1/responses`)

```json
{
  "model": "techsara-35b",
  "input": "Explain retrieval-augmented generation.",
  "instructions": "Answer in British English.",
  "stream": true,
  "background": false,
  "max_output_tokens": 1000,
  "temperature": 0.2,
  "metadata": {"customer_request_id": "abc-123"}
}
```

`input` is a string or a list of `{role, content}` messages (`system`, `user`,
`assistant`; `content` is a string). Validation, all enforced server-side:

| field | rule | failure |
|---|---|---|
| body | ≤ 1 MiB (configurable) | 413 `request_too_large` |
| `model` | must be in the key's allowlist | 404 `model_not_found` |
| `input` | non-empty; ≤ `max_input_tokens` for the model | 400 / 400 `context_length_exceeded` |
| `max_output_tokens` | 1 … model ceiling | 400 `invalid_request_error` |
| `temperature` | 0.0 … 2.0 | 400 |
| `metadata` | ≤ 16 keys, key ≤ 64 chars, value ≤ 512 chars, strings only | 400 |
| `stream` + `background` | both true is refused | 400 |

A parameter this platform cannot honour is **rejected, never silently ignored**.
Nothing in the body can change the project, workspace, key, model target, limits
or audit policy — those come from the key.

The API is stateless: it never reads a person's chat memory and never writes to
anyone's conversation history.

## 9. Response and error envelope

Success (non-streaming):

```json
{
  "id": "resp_3f8a…", "object": "response", "created_at": 1789200000,
  "status": "completed", "model": "techsara-35b",
  "output": [{"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "…"}]}],
  "usage": {"input_tokens": 37, "output_tokens": 112, "total_tokens": 149}
}
```

`usage` is `null` — never `0` — when the engine did not report counts, because
`llm.get_usage()` returns `None` for "not measured" and a zero would be a lie
(and an under-charge).

Error, everywhere, including mid-stream:

```json
{"error": {"message": "The API key is invalid.", "type": "authentication_error",
           "code": "invalid_api_key", "param": null, "request_id": "req_…"}}
```

| code | status | when |
|---|---|---|
| `invalid_request_error` | 400 | malformed or out-of-range |
| `invalid_api_key` | 401 | missing, malformed, unknown, revoked, expired |
| `insufficient_scope` | 403 | key lacks the scope |
| `origin_not_allowed` | 403 | browser origin outside the project's allowlist |
| `model_not_found` | 404 | unknown model, or one this key may not use |
| `response_not_found` | 404 | not this project's response |
| `idempotency_conflict` | 409 | same key, different body |
| `request_too_large` | 413 | body over the cap |
| `context_length_exceeded` | 400 | prompt over the model's input ceiling |
| `rate_limit_error` | 429 | RPM/TPM exceeded — `Retry-After` |
| `quota_exceeded` | 429 | daily/monthly token quota — `Retry-After` |
| `concurrency_limit_exceeded` | 429 | too many in flight — `Retry-After` |
| `model_recovering` | 503 | engine restarting — `Retry-After`, retry-safe |
| `model_unavailable` | 503 | engine down |
| `timeout` | 504 | generation exceeded the wall clock |
| `internal_error` | 500 | anything else — never a traceback |

No response body may contain a traceback, SQL, an environment value, a container
name, an internal hostname, a filesystem path or a private IP.

## 10. Streaming (SSE)

Headers: `Content-Type: text/event-stream`, `Cache-Control: no-store, no-cache,
no-transform`, `X-Accel-Buffering: no`, `Connection: keep-alive`, no
`Content-Length`.

Events, each `event: <name>` plus a JSON `data` whose `type` repeats the name and
which carries a `sequence_number` starting at 1 and increasing by exactly 1:

```
response.created → response.in_progress → response.output_text.delta (×N)
  → response.output_text.done → response.completed
```

with `response.queued` when the engine is recovering and the request is allowed
to wait, and the terminals `response.failed` and `error`. A heartbeat comment
(`: ping`) goes out at least every 15 s so an idle proxy cannot close the
connection. The terminal event carries `usage`; non-terminal events carry
`usage: null`. Exactly one terminal event is ever emitted.

The generator is always closed: `finally: await stream.aclose()`. Without it an
abandoned request holds an admission lane and an open upstream response until
garbage collection (`llm.py`, the pattern in `continuation.py`).

## 11. Execution path

```
/v1/responses
  → quota gate (per key, BEFORE admission)
  → llm.stream_chat_events(messages, model_choice="smart", effort=…,
                           temperature=…, max_tokens=…)     [llm.py:756]
  → admission.run (the shared NORMAL/LONG lanes)
  → breaker + engine state
  → vLLM main model
```

`/v1` calls `stream_chat_events` and nothing lower. It never builds its own vLLM
client, never calls the router, embeddings, OCR or the reranker, and never
registers in `_live_generations` (that registry is one-per-conversation and a
second request would cancel the first — every API request gets its own key).

`llm.reset_usage()` is called **inside** the task that runs the generation and
`llm.get_usage()` is read in its `finally`, because usage is a `ContextVar`.

The quota gate sits in front of admission on purpose: the ten NORMAL lanes are
shared with the chat app, so without it one developer key could starve every
signed-in person.

## 12. Limits

Per project (and optionally tightened per key):

| limit | default | enforced |
|---|---|---|
| requests / minute | 60 | sliding window, durable |
| input tokens / minute | 200,000 | durable counter |
| output tokens / minute | 60,000 | durable counter |
| concurrent requests | 4 | in-process semaphore + durable row |
| daily tokens | 2,000,000 | durable, survives restart |
| body bytes | 1 MiB | before parsing |
| input tokens | the model's ceiling | before admission |
| output tokens | 8,192 default, model ceiling max | clamped |

Headers on every `/v1` response: `RateLimit` (`"requests";r=<remaining>;t=<seconds>`),
`RateLimit-Policy`, and `Retry-After` on every 429 and 503.

Counters live in PostgreSQL — no Redis is introduced, because there is one
orchestrator process and the durable counters must survive a restart. A
process-local semaphore handles burst and concurrency; the database holds the
minute, day and month ledgers, updated atomically with a single statement.
Token counts are written **once per request**, never per token.

## 13. Idempotency

`Idempotency-Key` on `POST /v1/responses` (and `/v1/chat/completions`), scoped by
`(project_id, endpoint, key)`, retained 24 h.

* same key + same body fingerprint → the original response is returned (or, if it
  is still running, the caller attaches to it); the model is not invoked twice;
* same key + different fingerprint → `409 idempotency_conflict`;
* the claim is `INSERT … ON CONFLICT DO NOTHING RETURNING id` — zero rows means
  someone else claimed it, which is the race-free primitive.

## 14. Background responses and webhooks

`background: true` returns `202` with a response id after the row is durable and
before any expensive work. Status is read with `GET /v1/responses/{id}`; the work
survives client disconnect; cancellation is idempotent.

A webhook endpoint belongs to a project: HTTPS only, subscribed events
(`response.completed`, `response.failed`, `response.cancelled`), a signing
secret, rotation with an overlap, test delivery, delivery history.

Signature: `TechSara-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`,
verified with a constant-time compare and a ±5-minute tolerance; every delivery
carries a unique `event_id` so a consumer can be idempotent. Retries are bounded
exponential backoff with jitter, a maximum attempt count, and a recorded history
— never infinite.

The payload carries the **response id and bounded metadata**, never the prompt or
the generated text, unless the project has explicitly opted in.

SSRF defence on every delivery: HTTPS only; resolve the hostname, reject
loopback, link-local, private, unique-local, IPv4-mapped-IPv6 and the cloud
metadata address; connect to the resolved-and-checked IP (so DNS cannot be
rebound between check and connect); at most 3 redirects, each re-validated; no
credentials in the URL; 10 s timeout; response body read and discarded to a small
bound.

## 15. Model registry

A code-level registry is the source of truth — infrastructure cannot expose a
model by appearing in Compose:

```python
PublicModel(id="techsara-35b", internal=settings.llm_model, chat=True,
            streaming=True, vision=True, tools=False, embeddings=False,
            max_input_tokens=…, max_output_tokens=…, status="available")
```

The database may only **narrow** this: a `public_models` row can disable a model
the code declares, never enable one it does not. The router, embeddings, OCR and
reranker are absent from the registry and have no reachable path from `/v1`.

Real limits are read at runtime from the engine (`/health` reports
`context.served_max_model_len`), never hard-coded into the documentation.

## 16. Recording

Every request writes exactly one row through the existing ledger —
`usage.record_async(route="v1_responses", model=…, input_tokens=…,
output_tokens=…, ttft_ms=…, duration_ms=…, status=…, meta={"api_key_id": …,
"project_id": …, "request_id": …})` — so the console and the existing analytics
pages see API traffic without a second ledger being invented.

Key create, rotate and revoke, project and webhook changes go through the
existing audit writer (`audit(principal, request, "api_key_created", …)`).

Prompt and output content are **not** stored by default. Request logs keep
metadata only: request id, time, project, model, status, tokens, duration,
stream/background flags, error code, key prefix.

## 17. Documentation and console

`/docs` is TechSara-branded, written here, and contains no other vendor's logo,
proprietary text or visual identity. Every example in it is executed against the
running API before it ships; an example that cannot be executed is marked as not
executed rather than presented as verified.

`/api` is server-guarded: the page resolves the session server-side and refuses
without `api.console.access` — never by hiding a menu item. It reuses the admin
design system (`AdminTable`, `Section`, `Stat`, `AnalyticsChart`, `RangePicker`,
`AdminDialog`, `RowMenu`, `useToast`, `ConfirmDialog`) so it looks like the
product rather than a bolted-on page, and the show-once secret flow copies
`InviteDialog`.

## 18. What this contract does not promise

* It does not hide request URLs from a browser's developer tools; security here
  is authorization, not obscurity.
* It does not make the BFF a security boundary: both the frontend and the
  orchestrator are reachable on the LAN, so every check that matters is in the
  orchestrator.
* It does not introduce Redis, a second inference path, or a second usage ledger.
* It does not change the chat application's behaviour for people who never touch
  the developer platform.
