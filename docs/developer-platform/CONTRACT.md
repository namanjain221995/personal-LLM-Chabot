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

Internal services (vLLM main and router, embeddings, OCR, reranker, speech,
PostgreSQL, pgAdmin, SearXNG, the engine controller, exporters) are never
reachable from any of the three surfaces. Since 2026-09-13 `/v1` reaches the
model engines on a caller's behalf — authenticated, metered, and through the
orchestrator only (§11) — but no engine port is published, and `/v1` never
proxies a caller-supplied URL.

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
  → quota + rate + concurrency        → 429 with Retry-After, only with PUBLIC_API_ENFORCE_LIMITS (§12.1)
  → request validation                → 400/413/422
  → capability + capacity gate        → 400 wrong endpoint for the model; 503 at capacity (§12.3)
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
| POST | `/v1/responses` | `responses.write` | sync, streaming or background; every chat-kind model |
| GET | `/v1/responses/{id}` | `responses.read` | project-scoped |
| POST | `/v1/responses/{id}/cancel` | `responses.write` | idempotent |
| POST | `/v1/chat/completions` | `responses.write` | compatibility shape, same models and engine paths as `/v1/responses` |
| POST | `/v1/embeddings` | `embeddings.write` | `techsara-embed`; synchronous |
| POST | `/v1/rerank` | `rerank.write` | `techsara-rerank`; synchronous |
| POST | `/v1/audio/transcriptions` | `audio.write` | `techsara-whisper`; `multipart/form-data`; synchronous |
| GET | `/v1/usage` | `usage.read` | project-scoped, bounded range |
| GET | `/v1/openapi.json` | none | the public schema only |

2026-09-13, owner request: `/v1` offers **every model TechSara runs**, not only
the main chat model. The three rows `/v1/embeddings`, `/v1/rerank` and
`/v1/audio/transcriptions` are new, and `/v1/responses` and
`/v1/chat/completions` now also serve `techsara-8b-vision` and `techsara-ocr`.
Exposing a model means an authenticated, metered `/v1` route proxies to its
engine; no engine port is published and no bind changes (§11).
`.github/workflows/scripts/public-api-surface.txt` lists exactly these eleven
operations.

### Scopes

The closed vocabulary (`apiplatform/scopes.py`) is **seven** scopes. The
sentence is `SCOPE_DESCRIPTIONS`, shown verbatim by the console and the OpenAPI
document.

| scope | sentence | default |
|---|---|---|
| `models.read` | List the models this key may use. | yes |
| `responses.read` | Read responses created by this project. | yes |
| `responses.write` | Create and cancel responses. | yes |
| `usage.read` | Read this project's usage counters. | no (opt-in) |
| `embeddings.write` | Create embeddings. | yes |
| `rerank.write` | Rerank documents against a query. | yes |
| `audio.write` | Transcribe audio. | yes |

* Each route requires exactly its one scope; no scope implies another, and
  there is no implication table.
* `responses.write` covers every chat-kind model (`techsara-35b`,
  `techsara-8b-vision`, `techsara-ocr`): they share the two endpoints, and a
  scope names what a credential may *call*, not which model. Which models a key
  may use is the key's and project's `allowed_models` allowlist (empty = every
  available public model).
* The three new write scopes are in `DEFAULT_SCOPES`: each spends engine time
  exactly as `responses.write` already did, and a default key that answered
  `403` on `/v1/embeddings` would look broken. `usage.read` stays opt-in.
* **Stored keys keep their stored scopes.** A key created before 2026-09-13
  does not hold `embeddings.write`, `rerank.write` or `audio.write`; its owner
  creates a new key. Projects with an empty allowlist DO gain
  `techsara-8b-vision` and `techsara-ocr` on existing `responses.write` keys.
  Both facts are in the public changelog.

**Model resolution** on every route that takes a `model`, after scope and
origin: the id is resolved against the key's allowlist, the `public_models`
overrides and what this deployment has configured. Unknown, not permitted,
disabled and not configured are one answer, `404 model_not_found`. A permitted
model on an endpoint its kind does not serve is
`400 invalid_request_error`, `param: model`, with the message
"The model `techsara-embed` does not support /v1/responses." — the model's
existence is already known to that caller, so saying so discloses nothing.

Not exposed, deliberately: Salesforce, RAG and web search, deep research,
uploads, artifacts, memory, conversation history, admin analytics, tool
calling, audio translation and speech synthesis. Each would need its own
product, scope and threat review.

## 8. Request contract

### 8.1 `POST /v1/responses`

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
`assistant`). `content` is a string or, **on a `user` message only**, a list of
content parts:

```json
{"role": "user", "content": [
  {"type": "input_text", "text": "What does this slide say?"},
  {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo…", "detail": "auto"}
]}
```

Validation, all enforced server-side:

| field | rule | failure |
|---|---|---|
| body | ≤ 20 MiB on `/v1/responses` and `/v1/chat/completions` (`PUBLIC_API_MAX_MEDIA_BODY_BYTES`); ≤ 1 MiB on every other JSON route (`PUBLIC_API_MAX_BODY_BYTES`); checked before parsing, at the edge and in the orchestrator; JSON above 64 KiB is parsed off the event loop | 413 `request_too_large` |
| text | all text in the body — `instructions` plus every string `content` and `input_text` — ≤ 1 MiB (`PUBLIC_API_MAX_BODY_BYTES`), checked after parsing: the 1 MiB text rule survives the larger media cap | 413 `request_too_large` |
| `model` | in the key's allowlist and available on this deployment | 404 `model_not_found` |
| `model` kind | a chat-kind model | 400 `invalid_request_error`, `param: model` |
| `input` | non-empty; its byte upper bound ≤ the model's `max_input_tokens` | 400 / 400 `context_length_exceeded` |
| image part | `image_url` is a `data:` URL of type `image/png`, `image/jpeg`, `image/webp` or `image/gif`, base64; its magic bytes match the declared type; `detail` absent or `auto`; on a `user` message; the model's `vision` is true; the request's image count ≤ the model's `limits.max_images_per_request` | 400 `invalid_request_error` |
| image size | each image decoded ≤ 10 MiB (`PUBLIC_API_MAX_IMAGE_BYTES`); base64 is decoded off the event loop. A request-validation failure like every other image rule (`models.validate_image_data_url`), so a `400`, not a `413`: the body as a whole was within its cap | 400 `invalid_request_error` |
| `max_output_tokens` | 1 … the model's ceiling (and the project's `max_output_tokens`); within the ceiling it is **clamped** to the remaining window (§8.3), never refused for size | 400 `invalid_request_error` above the ceiling |
| `temperature` | 0.0 … 2.0; absent → the model's default (0.2; 0.0 on `techsara-ocr`) | 400 |
| `metadata` | ≤ 16 keys, key ≤ 64 chars, value ≤ 512 chars, strings only | 400 |
| `stream` + `background` | both true is refused | 400 |

**Why only `data:` URLs.** vLLM fetches a remote image URL server-side and no
`--allowed-media-domains` is set, so an `http(s)` URL reaching an engine would
let a caller probe internal hosts from the cluster network. Every non-`data:`
URL (`http`, `https`, `file`, anything else) is refused before any engine is
called; a test proves an `http://` URL never reaches the stub engine.

**`techsara-ocr`** takes exactly one image per request (none, or more than one,
is `400`). When the request carries no text part and no `instructions`, the
server appends the text part `OCR` — the plain instruction that engine reads
correctly (2026-09-11: the "document parsing" prompt loops garbage and takes
ten times as long). Caller-supplied text is passed through unaltered, loops
included.

A parameter this platform cannot honour is **rejected, never silently ignored**.
Nothing in the body can change the project, workspace, key, model target, limits
or audit policy — those come from the key.

The API is stateless: it never reads a person's chat memory and never writes to
anyone's conversation history.

### 8.2 `POST /v1/chat/completions`

Accepted top-level fields, and nothing else: `model`, `messages`, `stream`,
`max_tokens`, `max_completion_tokens`, `temperature`, `stream_options` (only
`include_usage`). `max_completion_tokens` is an alias of `max_tokens`; sending
both is `400`. A message's `content` is a string or, on a `user` message, a list
of `{"type": "text", "text"}` and
`{"type": "image_url", "image_url": {"url": "data:…", "detail": "auto"}}` parts,
under exactly the image rules of §8.1. There is no `background`.

### 8.3 The output ceiling and the shared window (owner decision, 2026-09-13)

**Setting.** `PUBLIC_API_MAX_OUTPUT_TOKENS` (default 1,000,000). The chat
application's `MODEL_MAX_OUTPUT` is not changed and is no longer read by `/v1`.

**Not yet live on this deployment:** until `llm.stream_chat_events` accepts
`wall_clock_s` (an integration change to `llm.py`), every `techsara-35b`
generation is still stopped at the chat application's `GEN_WALL_CLOCK_S`
(4,200 s in `.env`: about 298,000–424,000 tokens at 71–101 tok/s), ending
`failed` with `timeout` and its partial output. The ceiling is accepted and the
clamp applies; only the length is capped. `registry.per_request_wall_clock_live()`
answers the question at run time, and the `max_output_tokens` description in
`GET /v1/openapi.json` carries the same caveat for exactly as long as it is true.
This paragraph goes in the change that lands the llm.py integration (a test
holds the two together).

**Ceilings** (registry, read at call time):

* `techsara-35b`: `max_output_tokens` = min(`PUBLIC_API_MAX_OUTPUT_TOKENS`,
  context window `MAIN_MODEL_MAX_LEN`) = **1,000,000**; default when omitted =
  min(`PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS`, ceiling) = **8,192**, unchanged.
* `techsara-8b-vision`: 24,576 ceiling, 8,192 default.
* `techsara-ocr`: 8,192 ceiling, 8,192 default.

**Resolution order** (`publicapi/planning.plan_generation`, shared by `/v1` and
the console playground):

1. `requested` = the caller's `max_output_tokens` (Chat Completions:
   `max_tokens` or `max_completion_tokens`), or the default. An explicit value
   above the model's ceiling, or above the project's `max_output_tokens`, is
   `400 invalid_request_error`, `param: max_output_tokens`; a project whose
   `max_output_tokens` is 0 may not generate (`400`).
2. The input over `max_input_tokens` (byte upper bound) is
   `400 context_length_exceeded`. **This is the only refusal about size.**
3. **Clamp, never refuse**: `planned = max(1, min(requested, context_window −
   input_tokens − reserve))`. `techsara-35b`: input from
   `context.estimate_messages`, reserve 512 (`CONTEXT_SAFETY_MARGIN`); the engine
   call is sent `max_tokens = requested` and `llm._fit` re-clamps exactly with the
   `/tokenize` count. `techsara-8b-vision`: the estimate against the public window
   (the engine's larger window absorbs an under-estimate), reserve 64.
   `techsara-ocr`: the upper bound (text bytes + 2,048 tokens per image) because
   the public window equals the engine window, reserve 64; the value sent is
   `planned`. A remaining window below one token is `400 context_length_exceeded`.
   **One retry, never a refusal** (2026-09-13): when `/tokenize` cannot answer
   (its 5 s timeout, a transient failure, a payload it refuses) `llm._fit` clamps
   on the estimate, and a prompt the estimate under-counts (digits: one token
   each) can be sent past the window. When the main engine refuses a request as
   too long before producing anything, `streaming.Generation` retries it once
   with `max_tokens = min(requested, window − byte bound − 512)`; the byte bound
   cannot be exceeded and leaves at least 256 tokens, because `max_input_tokens`
   is the window minus that and the reserve.
4. **Applied** (exact): `techsara-35b` = `llm.get_applied_max_tokens()` once
   `llm.py` exposes it; until then `min(requested, window − usage.input_tokens −
   512)` when the engine reported usage, else `planned`; never more than the
   retry above sent, when it ran. Sidecars: `planned`. For `techsara-35b` the
   applied value **may be higher or lower** than the planned value: the plan
   counts the prompt with the 3-characters-per-token estimate, the engine with
   its tokenizer, and prose that tokenizes at about 4 characters per token
   leaves more room than was planned.

`max_input_tokens` for `techsara-35b` is 999,232 = 1,000,000 − 512 − 256
(`context.MIN_OUTPUT_TOKENS`), so `llm._fit` never trims a public prompt:
`fit_request` drops turns and clips messages below a 256-token budget, which
`/v1` must never do silently. In practice the text rule bounds input far lower
(1 MiB of text is roughly 250,000–350,000 prose tokens): **the 1M window is
reachable through output, not input.**

**Wall clock per request** (planning.py):

```
wall_clock_s = min(PUBLIC_API_GEN_WALL_CLOCK_S,
                   max(floor, prefill_allowance + planned_max_output_tokens / min_decode_tps))
```

| model | floor | prefill allowance | min decode tok/s |
|---|---|---|---|
| `techsara-35b` | `GEN_WALL_CLOCK_S` (4,200 s in `.env`) | `PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S` 900 s | `PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S` 50 |
| `techsara-8b-vision`, `techsara-ocr` | 600 s | 60 s | 20 |

`PUBLIC_API_GEN_WALL_CLOCK_S` defaults to 21,600 s (6 h). Examples: the 8,192
default → 4,200 s (the chat application's number, unchanged); 1,000,000 → 900 +
20,000 = 20,900 s (5 h 48 min). Measured basis: full-window prefill 878 s at
949,915 tokens (2026-08-29); decode 71–72 tok/s at 500K–1M context and about
101 tok/s single-stream after the 2026-09-11 remediation. 50 tok/s leaves room
for batch contention, not for sustained whisper or OCR saturation (24 and
20 tok/s measured).

A generation cut by its wall clock ends `failed` with code `timeout`, keeping
the partial text and its usage: `response.failed` on a stream (an error chunk
then `[DONE]` on Chat Completions), a failed row with its `output_text` for
background, a `504` envelope for a synchronous request with usage still
recorded. The chat application's in-band
`[generation stopped after Ns — wall-clock guard…]` token never reaches `/v1`
output. **Where that usage comes from**: vLLM reports usage in the stream's
last chunk only, and a wall-clock stop closes the stream before it, so the
server counts instead — `output_tokens` = the deltas it received (one streamed
chunk is one token on this deployment), `input_tokens` = the exact `/tokenize`
count `llm._fit` took for the call, or the planner's estimate when there is none
(always the estimate on `techsara-8b-vision` and `techsara-ocr`). The ledger row
says which: `usage_events.meta.usage_source` is `counted_at_stop` for these,
`engine` otherwise (§16).

**Enforcement.** `techsara-35b`: `llm.stream_chat_events(wall_clock_s=…,
wall_clock_marker=False)` once `llm.py` accepts them (`streaming.py`
feature-detects the signature); until then llm's own `GEN_WALL_CLOCK_S` applies
and a `/v1` generation is still cut at about 70 minutes. Independently the
`streaming.Generation` consumer cancels the producer at `wall_clock_s + 30 s`.
Sidecars: `publicapi/engines.stream_chat` enforces `wall_clock_s` itself.

**Synchronous requests are unsuitable for long work** (documented, not
refused). `ai.techsarasolutions.com` is behind Cloudflare, whose proxied-origin
response timeout is 100 s (HTTP 524) before any byte, and the Next `/v1` edge's
undici fetch has default 300 s header and body timeouts. A non-streaming
response sends no byte until it is done, so through the public hostname it must
finish in about 100 s — roughly 5,000 output tokens at 70–100 tok/s, and even the
8,192 default can exceed it. Public guidance: `stream: true` or
`background: true` above about 5,000 output tokens, always with an
`Idempotency-Key`. A synchronous request whose client disconnects is **stopped
at the disconnect** (2026-09-13): the server watches the connection while it
generates, cancels the generation, gives its capacity gate and admission slot
back, and records the response `cancelled` with what the engine produced (the
access log shows `499`). Before this, an abandoned synchronous request held
them until the whole generation ended — up to 5 h 48 min for 1,000,000 tokens.

### 8.4 `POST /v1/embeddings`

Body (≤ 1 MiB, `extra="forbid"`):

```json
{"model": "techsara-embed", "input": ["first text", "second text"], "encoding_format": "float"}
```

* `input`: a non-empty string, or 1–256 non-empty strings
  (`PUBLIC_API_EMBED_MAX_INPUTS`). Token-array input is `400`.
* `encoding_format`: `float` (default) or `base64` (little-endian float32).
* `dimensions`, `user` and any other field → `400` naming it.
* Each input ≤ 4,096 tokens, enforced exactly by the engine; over it is
  `400 context_length_exceeded`, `param: input.<i>`. Inputs are embedded as
  given: no query instruction is added and nothing is clipped (the chat
  application's `llm.embed_texts` clips at `EMBED_INPUT_CHAR_CAP`; `/v1` does
  not use it).

`200`:

```json
{"object": "list", "model": "techsara-embed",
 "data": [{"object": "embedding", "index": 0, "embedding": [0.0123, -0.0456]}],
 "usage": {"prompt_tokens": 12, "total_tokens": 12}}
```

Vectors have 1,024 dimensions. `usage` is `null` when any engine call did not
report counts. Inputs are packed into engine calls of ≤ 16 inputs and ≤ the gate
budget, sent in order under gate `embed`; `data[].index` preserves input order.
Engine unreachable or 5xx → `503 model_unavailable`, `Retry-After: 30`; gate wait
expired → `503 model_unavailable` ("at capacity"), `Retry-After: 5`; read timeout
60 s → `504 timeout`.

### 8.5 `POST /v1/rerank`

Body (≤ 1 MiB, `extra="forbid"`):

```json
{"model": "techsara-rerank", "query": "How do I rotate a key?",
 "documents": ["Keys are rotated in the console.", {"text": "Webhooks are signed."}],
 "top_n": 1, "return_documents": true}
```

* `query`: non-empty string. `documents`: 1–100 strings or `{text}` objects
  (`PUBLIC_API_RERANK_MAX_DOCUMENTS`). `top_n` ≥ 1, default all.
  `return_documents` default false. `instruction` optional, ≤ 512 characters,
  default `rerank.DEFAULT_INSTRUCTION`.
* The server applies the model's template (`PREFIX`, `<Instruct>`, `<Query>`,
  `<Document>`, `SUFFIX` from `app/rerank.py`) with **no truncation**; a
  templated pair over 4,096 tokens is `400 context_length_exceeded`,
  `param: documents.<i>`.

`200`:

```json
{"id": "rrk_<24 hex>", "object": "rerank", "model": "techsara-rerank",
 "results": [{"index": 0, "relevance_score": 0.93, "document": {"text": "Keys are rotated in the console."}}],
 "usage": {"input_tokens": 88, "total_tokens": 88}}
```

`id` is the request's ledger generation id (`rrk_<24 hex>`, §16).
`relevance_score` is the model's probability that the document answers the
query. Results are sorted by `relevance_score` descending then `index` ascending,
cut to `top_n`; `document` appears only with `return_documents`. Equal scores
are returned as they are — the chat client's "degenerate" refusal does not apply
to the public surface. Errors map as §8.4, under gate `rerank`.

### 8.6 `POST /v1/audio/transcriptions`

`multipart/form-data`, parsed **in memory** from the request stream with a
bounded reader (`publicapi/multipart.py`, never `UploadFile`, which spools to
disk):

| field | rule |
|---|---|
| `file` | required; the part's `Content-Type` in `audio_api.ALLOWED_TYPES`; ≤ 25 MiB (`PUBLIC_API_MAX_AUDIO_BYTES`) |
| `model` | required, `techsara-whisper` |
| `language` | optional ISO-639-1 code, or `auto` (the default) |
| `response_format` | optional `json` (default), `text` or `verbose_json`; `srt` and `vtt` are `400` |
| `timestamp_granularities[]` | optional, only `segment`, only with `verbose_json` |
| anything else | `temperature`, `prompt` and any other field is `400` naming it |

Body ≤ 26 MiB (`PUBLIC_API_MAX_AUDIO_BODY_BYTES`). Audio longer than 300 s
(`PUBLIC_API_MAX_AUDIO_SECONDS`) is `413 request_too_large`, as is the engine's
own 600 s refusal. An undecodable file is `400` with a fixed sentence — never the
decoder's stderr. The replica is the last healthy one in `settings.asr_base_urls`
(dictation breaks ties toward index 0); timeout `settings.asr_timeout_s`
(240 s) → `504`; unreachable or 5xx → `503 model_unavailable`, `Retry-After: 30`.

`200` for `json`: `{"text": "…", "usage": {"type": "duration", "seconds": 42}}`
(`seconds` = ceil of the engine's duration). `text`: a `text/plain` body.
`verbose_json`: `{task: "transcribe", language: <lowercase language name or
null>, duration, text, segments: [{id, start, end, text}], usage}`. A clip the
silence gate judges empty returns empty text. A forced `language` forces the
decoder: `en` on non-English speech translates rather than transcribes
(2026-09-10), so auto-detect is the recommendation.

**All three sidecar endpoints**: the same `PublicRoute` (§9 envelope,
`X-Request-Id`, CORS without credentials); Bearer only; scope, then origin; one
`quotas.reserve(kind="sync")`; model resolution and the capability check of §7;
an `Idempotency-Key` header is `400`, `param: Idempotency-Key`; no
`api_responses` row.

## 9. Response and error envelope

Success (non-streaming):

```json
{
  "id": "resp_3f8a…", "object": "response", "created_at": 1789200000,
  "status": "completed", "model": "techsara-35b",
  "output": [{"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "…"}]}],
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": {"input_tokens": 37, "output_tokens": 112, "total_tokens": 149}
}
```

`usage` is `null` — never `0` — when the engine did not report counts, because
`llm.get_usage()` returns `None` for "not measured" and a zero would be a lie
(and an under-charge).

Two keys added 2026-09-13, **always present** on the response object — the
synchronous body, every SSE snapshot, `response.completed` / `response.failed`,
`GET /v1/responses/{id}` and the background `202`:

* `max_output_tokens` — integer or `null`: the ceiling **applied** to this
  generation (§8.3). Snapshots before generation (`response.created`,
  `response.queued`, `response.in_progress`, the `202`) carry the planned value;
  the terminal event and `GET` carry the value the engine was actually allowed
  (§8.3 step 4), which for `techsara-35b` may be **higher or lower** than the
  planned one; for the sidecar models it equals it. `null` only for rows written
  before V35.
* `incomplete_details` — `{"reason": "max_output_tokens"}` when the engine
  stopped because it reached the applied ceiling (finish reason `length`),
  otherwise `null`. `status` stays `completed`: the V34 status CHECK has no
  `incomplete`. A documented deviation from the OpenAI-shaped object.

`chat.completion` (non-streaming) and the stream chunk carrying `finish_reason`
gain a top-level extension `"max_output_tokens"`: the applied integer.
`finish_reason` is `length` when the ceiling was hit (unchanged).

Error, everywhere, including mid-stream:

```json
{"error": {"message": "The API key is invalid.", "type": "authentication_error",
           "code": "invalid_api_key", "param": null, "request_id": "req_…"}}
```

| code | status | when |
|---|---|---|
| `invalid_request_error` | 400 | malformed or out-of-range; a model on an endpoint its kind does not serve (`param: model`); an image that breaks §8.1, including one over 10 MiB |
| `invalid_api_key` | 401 | missing, malformed, unknown, revoked, expired |
| `insufficient_scope` | 403 | key lacks the scope |
| `origin_not_allowed` | 403 | browser origin outside the project's allowlist |
| `model_not_found` | 404 | unknown model, one this key may not use, or one not configured on this deployment |
| `response_not_found` | 404 | not this project's response |
| `idempotency_conflict` | 409 | same key, different body; or same body while the first is still running — `Retry-After` |
| `request_too_large` | 413 | body over its route's cap, text over 1 MiB, audio over 25 MiB or 300 s |
| `context_length_exceeded` | 400 | prompt over the model's input ceiling; an embeddings input or a rerank pair over 4,096 tokens; no window left for one output token |
| `rate_limit_error` | 429 | RPM/TPM exceeded, only when limits are enforced — `Retry-After` |
| `quota_exceeded` | 429 | daily/monthly token quota, only when limits are enforced — `Retry-After` |
| `concurrency_limit_exceeded` | 429 | too many of the project's requests in flight, only when limits are enforced — `Retry-After` |
| `model_recovering` | 503 | engine restarting — `Retry-After`, retry-safe |
| `model_unavailable` | 503 | engine down; or at capacity — the shared admission lanes full, or a per-engine public capacity gate's bounded wait expired (§12.3) — `Retry-After`, retry-safe |
| `timeout` | 504 | generation exceeded its per-request wall clock (§8.3), partial output kept; a sidecar engine's read timeout |
| `internal_error` | 500 | anything else — never a traceback |

The code table is closed; capacity refusals on every engine are
`errors.model_at_capacity(retry_after)`, which is `model_unavailable`.

No response body may contain a traceback, SQL, an environment value, a container
name, an internal hostname, a filesystem path, a private IP, an internal
checkpoint name or an engine URL.

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

with `response.queued` when the main engine is recovering and the request is
allowed to wait (never on a sidecar engine), and the terminals `response.failed`
and `error`. A heartbeat comment (`: ping`) goes out at least every 15 s
(`events.HEARTBEAT_SECONDS`) **for the whole life of the stream, on every
engine** — a 1,000,000-token answer is a multi-hour stream, and the heartbeat is
what keeps every proxy between the caller and the edge from closing it. The
terminal event carries `usage`; non-terminal events carry `usage: null`. Exactly
one terminal event is ever emitted.

Every response snapshot in the stream carries `max_output_tokens` and
`incomplete_details` (§9): the planned value before generation, the **applied**
value on the terminal. On Chat Completions the chunk carrying `finish_reason`
carries the applied `max_output_tokens`. A wall-clock stop is `response.failed`
with code `timeout` and the partial text.

Capacity-gate waits for a streaming request happen **before** the status line,
bounded by `PUBLIC_API_GATE_WAIT_S` (30 s), so a refusal is a real HTTP `503`
with `Retry-After` and the silent pre-header wait stays under Cloudflare's 100 s.
Admission-lane waits (600 s) stay inside the body with heartbeats and end in
`response.failed` `model_unavailable` "at capacity".

The generator is always closed: `finally: await stream.aclose()`. Without it an
abandoned request holds an admission lane and an open upstream response until
garbage collection (`llm.py`, the pattern in `continuation.py`). Sidecar
streams go through the same `streaming.Generation` queue, so heartbeats, `aclose`
and the single-terminal rule are inherited, not reimplemented.

## 11. Execution path

```
/v1/responses, /v1/chat/completions
  → plan          publicapi/planning.plan_generation: model, ceilings, clamp,
                  wall clock, gate                                         (§8.3)
  → meter         quotas.reserve (limits only with PUBLIC_API_ENFORCE_LIMITS)
  ├─ techsara-35b
  │    → gate 'main.long' when input (byte bound) + planned output > 131,072,
  │      else gate 'main.extended' when planned output > 8,192, else no gate
  │    → llm.stream_chat_events(messages, model_choice="smart", effort="fast",
  │                             temperature=…, max_tokens=…, wall_clock_s=…,
  │                             wall_clock_marker=False)
  │    → admission.run (the shared NORMAL/LONG lanes) → breaker + engine state
  │    → vLLM main model (TP=2 across both Sparks)
  └─ techsara-8b-vision | techsara-ocr
       → gate 'router' | 'ocr'
       → publicapi/engines.stream_chat → router engine | OCR engine (worker)

/v1/embeddings | /v1/rerank | /v1/audio/transcriptions
  → meter → gate 'embed' | 'rerank' | 'asr' → publicapi/sidecars → engine
```

Rules:

* **Main model: `llm.stream_chat_events` and nothing lower.** `/v1` never builds
  a second client to the main engine; the breaker, admission lanes and `_fit`
  apply to public traffic exactly as to chat.
* **Router and OCR: `publicapi/engines`**, which resolves `EngineTarget(key,
  base_url, model, api_key)` from `settings` at call time, streams with
  `stream_options.include_usage`, and uses FIXED per-engine read timeouts
  (router and OCR 300 s) — never a caller-derived value, because `llm._client`'s
  cache key must not carry one (F048). Router reasoning is off
  (`llm.reasoning_extra_body(settings.router_capabilities, False)`) and
  `temperature` is always sent, so the checkpoint's own default never applies.
* **Embeddings, reranker, speech: `publicapi/sidecars`**, with their own
  engine calls (embed and rerank read timeout 60 s, whisper
  `settings.asr_timeout_s`). Not `llm.embed_texts` (clips and drops usage), not
  `rerank.score` (truncates, chat-only breaker, degenerate refusal), not
  `asr.provider()` (ignores `language`, drops `duration`).
* **Engines are never published.** No engine port is opened and no bind changes;
  every engine stays reachable only from the orchestrator. `/v1` never forwards a
  caller-supplied URL to any engine: images are validated `data:` URLs (§8.1).
* **Chat keeps priority.** Chat-application code paths
  (`llm.router_chat_completion`, `llm.embed_query`/`embed_texts`,
  `rerank.score`, `engines/ocr.read_images`, `asr.transcribe`) never pass
  through a public gate, so chat never waits on public queueing; only the public
  side yields (§12.3).
* `/v1` never registers in `_live_generations` (one-per-conversation; a second
  request would cancel the first).

`llm.reset_usage()` is called **inside** the task that runs the generation and
`llm.get_usage()` is read in its `finally`, because usage is a `ContextVar`. The
sidecar producer captures usage and finish reason in the same task for the same
reason.

## 12. Limits

### 12.1 Usage limits: none by default

Owner decision, 2026-09-13: `PUBLIC_API_ENFORCE_LIMITS=false`. There is no
requests-per-minute, tokens-per-minute, daily or monthly quota and no
per-project concurrency limit, no `429` for volume, and no `RateLimit` or
`RateLimit-Policy` header. Usage is still metered once per request (§16).

With `PUBLIC_API_ENFORCE_LIMITS=true` an operator gets, per project (and
optionally tightened per key):

| limit | default | enforced |
|---|---|---|
| requests / minute | 60 | sliding window, durable |
| input tokens / minute | 200,000 | durable counter |
| output tokens / minute | 60,000 | durable counter |
| concurrent requests | 4 | in-process semaphore + durable row |
| daily tokens | 2,000,000 | durable, survives restart |

and then headers on authenticated `/v1` responses: `RateLimit`
(`"requests";r=<remaining>;t=<seconds>`), `RateLimit-Policy`, and `Retry-After`
on every 429 and 503. Counters live in PostgreSQL — no Redis — updated
atomically with a single statement, token counts written **once per request**.

### 12.2 Technical ceilings (every deployment)

| model | kind | context window | max input tokens | max output tokens | default output | other |
|---|---|---|---|---|---|---|
| `techsara-35b` | chat | 1,000,000 | 999,232 | 1,000,000 | 8,192 | ≤ 16 images per request |
| `techsara-8b-vision` | chat | 24,576 | 24,320 | 24,576 | 8,192 | ≤ 8 images per request |
| `techsara-ocr` | chat | 8,192 | 7,936 | 8,192 | 8,192 | exactly 1 image per request |
| `techsara-embed` | embedding | 4,096 | 4,096 per input | none | none | ≤ 256 inputs, 1,024 dimensions |
| `techsara-rerank` | rerank | 4,096 | 4,096 per pair | none | none | ≤ 100 documents |
| `techsara-whisper` | transcription | none | none | none | none | ≤ 300 s and ≤ 25 MiB of audio |

| body | cap |
|---|---|
| `/v1/responses`, `/v1/chat/completions` | 20 MiB (`PUBLIC_API_MAX_MEDIA_BODY_BYTES`), of which text ≤ 1 MiB |
| `/v1/audio/transcriptions` | 26 MiB (`PUBLIC_API_MAX_AUDIO_BODY_BYTES`), file part ≤ 25 MiB |
| every other route | 1 MiB (`PUBLIC_API_MAX_BODY_BYTES`) |

Where the numbers come from (read from the running engines, 2026-09-12/13):

* **techsara-35b**: `--max-model-len 1000000`; KV pool 1,663,201 tokens =
  1.66 full windows.
* **techsara-8b-vision**: engine `--max-model-len 49152`, KV pool 52,512 tokens
  (1.07 windows). The **public window is half the engine's on purpose**: the chat
  application calls this engine on every turn for freshness routing (≤ 6,000
  characters + 200 output, about 2,300 tokens), so a public full-window request
  could hold the whole pool. `PUBLIC_API_ROUTER_CONTEXT_TOKENS` (24,576) may be
  raised to 49,152 only together with a larger router KV budget. The window
  served by the engine is read by a cached (300 s) `GET <router>/v1/models`
  probe and can only narrow the public number. `ROUTER_CONTEXT_LENGTH=65536` in
  `generated.env` is wrong versus the engine and is not trusted.
* **techsara-ocr**: `--max-model-len ${OCR_MAX_CONTEXT:-8192}`,
  `--max-num-seqs 8`; to be confirmed with one `GET /v1/models` at integration
  (the worker was not inspected directly).
* **techsara-embed** and **techsara-rerank**: `--max-model-len 4096`, KV pool
  18,720 tokens each (4.57 windows). `EMBED_CONTEXT_LENGTH` and
  `RERANKER_CONTEXT_LENGTH=32768` in `generated.env` are wrong versus the
  engines and are not trusted.
* **techsara-whisper**: engine `MAX_AUDIO_SECONDS` 600, one clip at a time per
  replica (`_gpu_lock`), about 7 s of audio per wall-second.

### 12.3 Capacity gates — not usage limits

The router, OCR, embeddings, reranker and speech engines are shared with the chat
application, and every engine shares a node with a rank of the TP=2 main model.
Public traffic to them must never starve chat. A capacity gate is **per engine,
shared by every project and key**, FIFO, with a bounded wait; its refusal is
`503 model_unavailable` "at capacity" with `Retry-After` — never `429`, never
per caller (`publicapi/capacity.py`: `hold(engine, *, weight_tokens, wait_s,
yield_to_chat)`).

A request is admitted when `in_flight < max_concurrent` and `used_tokens +
min(weight, budget) ≤ budget`; a request heavier than the whole budget is
admitted only when no other public request holds that engine. Released in
`finally`, cancellation-safe; configuration read at call time; gauges
`public_api_engine_in_flight` and `public_api_engine_waiting` by engine.

| gate | public concurrency | public KV budget (tokens) | weight | yields to chat | Retry-After |
|---|---|---|---|---|---|
| `main.long` | 1 | none | — | to a chat LONG request | 60 s |
| `main.extended` | 2 | none | — | to a chat LONG request | 60 s |
| `router` | 4 | 24,576 | input at its byte bound + planned output | no | 5 s |
| `ocr` | 2 | none | — | yes | 5 s |
| `embed` | 2 | 8,192 | Σ min(upper bound, 4,096) per engine call | no | 5 s |
| `rerank` | 2 | 8,192 | templated pair bound per engine call | no | 5 s |
| `asr` | 1, fleet-wide | none | — | yes, and to dictation | 5 s |

Why these numbers:

* **Sizes are counted at the byte bound** (2026-09-13, adversarial review): a
  gate's footprint uses the prompt's UTF-8 byte length (`context.upper_bound_
  messages`), never the 3-characters-per-token estimate. The Qwen pre-tokenizer
  isolates every digit, so a digit prompt estimated at a third of its tokens
  slipped under `main.long` and, on the router, four such requests the gate
  charged 24,032 tokens really demanded 56,028 — over the whole 52,512-token
  pool. The bound over-counts prose (up to about 4×), which only ever makes
  public work wait; the output clamp still uses the estimate.
* **`main.long`** applies to a `techsara-35b` request whose input (byte bound)
  plus planned output exceeds `PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS` (default
  `ADMISSION_LONG_THRESHOLD_TOKENS`, 131,072). The KV pool holds 1.66 full
  windows; one public long-footprint generation plus one chat full-window prompt
  exceeds it only when both reach full size, and vLLM allocates KV lazily as
  output grows. A cap of one makes that rare; engine-level priority scheduling is
  the real fix (operator decision).
* **`main.extended`** applies to every other `techsara-35b` request that plans
  more than `PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS` (default 8,192 — the public
  default, and the public ceiling before 2026-09-13). Measured by the review: with
  no gate below 131,072, ten public 130,000-token answers held all ten of the
  chat application's NORMAL admission slots for about 22 minutes each and a chat
  turn was refused. Two at a time plus one `main.long` leaves chat at least 7 of
  the 10 NORMAL slots against long-lived public work. A request at or under 8,192
  holds a slot for at most about 115 s at 71 tok/s — what public work could
  already do — and takes no public gate: the shared NORMAL lanes remain its gate.
* **Both main gates step aside for a chat LONG request** (a large-document turn
  in the admission LONG lane, `capacity.chat_long_admission_present`), for the
  whole gate wait. That request waits for the engine to be idle for up to
  `ADMISSION_LONG_WAIT_S` (600 s), and a multi-hour public generation that
  starts meanwhile keeps it waiting all of it. Not solved here: a public job
  ALREADY decoding still counts as "not idle" — the fix is in `admission._ahead`
  (integration; `capacity.public_long_lived_decoding()` is the count to leave
  out).
* **`router`**: the public budget is 47% of the 52,512-token pool, so chat keeps
  at least 27,936 tokens (about twelve concurrent freshness classifications).
* **`ocr`**: engine `--max-num-seqs 8`; chat `read_images` batches use 4 and
  video OCR 4, so 2 public leaves chat its share. The KV pool (about 6.4
  windows) is not binding. The engine shares the worker GPU with a main-model
  rank — chat decode measured 75 → about 20 tok/s during OCR (2026-09-09) — so
  public OCR yields.
* **`embed`** and **`rerank`**: the public budget is 44% of each 18,720-token
  pool; chat's query embeddings and reranks sit on the time-to-first-token path
  with 1–2 s slot waits and fail soft when busy, so they keep at least 10,528
  tokens.
* **`asr`**: whisper decodes one clip per replica, and saturating either Spark
  takes chat decode from 71.4 to about 24 tok/s because the main model is TP=2.
  One public clip at a time, on the replica dictation does not prefer, in the
  gaps between chats. A dictation must always find a free replica: a public clip
  starts only when a replica stays free AFTER it starts (dictations in flight + 1
  < replicas; on a single replica, only when no dictation runs), and while it
  decodes it is counted in dictation's least-active routing, so the next
  dictation goes to the other replica. Residual, documented: on a single-replica
  fleet a dictation that arrives during a public clip waits for it (whisper
  cannot be preempted mid-clip; at most about 43 s for a 300 s clip).

**Yield to chat**: before taking the slot, while a chat generation is in flight
(`video.pipeline._busy_probe`, the owner-accepted video pacing policy), the
public request waits in 1 s steps up to `PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S`
(10 s), then proceeds to the slot — bounded impact, the slot cap. `asr`
additionally waits while dictation is queued (`asr.POOL.waiting > 0`) or every
replica is busy, within the same wait bound.

**Wait bounds**: synchronous, streaming, embeddings, rerank and transcription
requests wait at most `PUBLIC_API_GATE_WAIT_S` (30 s) before the status line;
background jobs wait at most `PUBLIC_API_BACKGROUND_GATE_WAIT_S` (3,600 s) inside
the job while the row stays `queued`. **Measure before raising any default**:
router interference with chat time-to-first-token under public load has not been
measured; the OCR and whisper figures are from 2026-09-08/09.

### 12.4 Settings

Every `PUBLIC_API_*` value below follows `config.py`'s `_int` / `_float` rules
(blank means the default). Until `config.py` declares them, readers use
`getattr(settings, <lower name>, None)` with an `os.environ` fallback parsed by
the same rules, so the defaults hold either way.

| setting | default | governs |
|---|---|---|
| `PUBLIC_API_ENFORCE_LIMITS` | false | §12.1 |
| `PUBLIC_API_MAX_BODY_BYTES` | 1,048,576 | JSON body cap; the text rule |
| `PUBLIC_API_MAX_MEDIA_BODY_BYTES` | 20,971,520 | body cap on the two generation routes |
| `PUBLIC_API_MAX_IMAGE_BYTES` | 10,485,760 | one decoded image |
| `PUBLIC_API_MAX_AUDIO_BODY_BYTES` | 27,262,976 | transcription body |
| `PUBLIC_API_MAX_AUDIO_BYTES` | 26,214,400 | transcription file part |
| `PUBLIC_API_MAX_AUDIO_SECONDS` | 300 | audio duration |
| `PUBLIC_API_MAX_OUTPUT_TOKENS` | 1,000,000 | output ceiling (§8.3) |
| `PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS` | 8,192 | output default |
| `PUBLIC_API_GEN_WALL_CLOCK_S` | 21,600 | hard wall clock for any `/v1` generation |
| `PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S` | 900 | wall-clock formula |
| `PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S` | 50 | wall-clock formula |
| `PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS` | 131,072 | `main.long` threshold |
| `PUBLIC_API_MAIN_LONG_MAX_CONCURRENT` | 1 | `main.long` |
| `PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS` | 8,192 | `main.extended` threshold (planned output) |
| `PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT` | 2 | `main.extended` |
| `PUBLIC_API_GATE_WAIT_S` | 30 | gate wait before the status line |
| `PUBLIC_API_BACKGROUND_GATE_WAIT_S` | 3,600 | gate wait inside a background job |
| `PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S` | 10 | yield bound |
| `PUBLIC_API_ROUTER_CONTEXT_TOKENS` | 24,576 | `techsara-8b-vision` window |
| `PUBLIC_API_ROUTER_MAX_CONCURRENT` | 4 | `router` gate |
| `PUBLIC_API_ROUTER_KV_BUDGET_TOKENS` | 24,576 | `router` gate |
| `PUBLIC_API_OCR_CONTEXT_TOKENS` | 8,192 | `techsara-ocr` window |
| `PUBLIC_API_OCR_MAX_CONCURRENT` | 2 | `ocr` gate |
| `PUBLIC_API_EMBED_CONTEXT_TOKENS` | 4,096 | per input |
| `PUBLIC_API_EMBED_MAX_CONCURRENT` | 2 | `embed` gate |
| `PUBLIC_API_EMBED_KV_BUDGET_TOKENS` | 8,192 | `embed` gate |
| `PUBLIC_API_EMBED_MAX_INPUTS` | 256 | inputs per request |
| `PUBLIC_API_RERANK_CONTEXT_TOKENS` | 4,096 | per pair |
| `PUBLIC_API_RERANK_MAX_CONCURRENT` | 2 | `rerank` gate |
| `PUBLIC_API_RERANK_KV_BUDGET_TOKENS` | 8,192 | `rerank` gate |
| `PUBLIC_API_RERANK_MAX_DOCUMENTS` | 100 | documents per request |
| `PUBLIC_API_ASR_MAX_CONCURRENT` | 1 | `asr` gate |
| `PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS` | max(3,600, 2 × `GEN_WALL_CLOCK_S`, 2 × `PUBLIC_API_GEN_WALL_CLOCK_S` + `PUBLIC_API_BACKGROUND_GATE_WAIT_S`) = 46,800 | §13 |

**Transport timeouts** (the `LLM_REQUEST_TIMEOUT` invariant, restated for `/v1`):
every `/v1` generation is a streamed engine call, even `stream: false`, so the
httpx read timeout bounds the longest silence between chunks, not the whole
generation. Main: `llm_request_timeout` (= `GEN_WALL_CLOCK_S`, 4,200 s) must be
≥ the longest legitimate silence, the 878 s full-window prefill; a start-up check
logs an error when it is below `PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S`. Sidecars use
the fixed read timeouts of §11. `LLM_MAX_RETRIES` stays 0.

## 13. Idempotency

`Idempotency-Key` on `POST /v1/responses` and `POST /v1/chat/completions` **only**,
scoped by `(project_id, endpoint, key)`, retained 24 h. On `/v1/embeddings`,
`/v1/rerank` and `/v1/audio/transcriptions` the header is refused with
`400 invalid_request_error`, `param: Idempotency-Key`: those calls are
synchronous, keep no durable row to replay, and a silently ignored key would
promise a safety the server does not provide.

* same key + same body fingerprint → the original response is returned; the model
  is not invoked twice;
* same key + different fingerprint → `409 idempotency_conflict`;
* same key + same fingerprint while the first is still running → `409
  idempotency_conflict` with `Retry-After` (2026-09-13: it was a `429`, which
  named a rate limit the API no longer enforces);
* the claim is `INSERT … ON CONFLICT DO NOTHING RETURNING id` — zero rows means
  someone else claimed it, which is the race-free primitive.

**The in-flight lease** (2026-09-13): a claim left `in_flight` by a dead process
is taken over after `max(PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS, 2 ×
PUBLIC_API_GEN_WALL_CLOCK_S + PUBLIC_API_BACKGROUND_GATE_WAIT_S)` = **46,800 s
(13 h)** by default, below the 24 h TTL. Without it a retry arriving 2 h 20 min
into a 1,000,000-token generation would take over the claim and start a second
one. An operator who sets `PUBLIC_API_IDEMPOTENCY_TTL_HOURS` below the lease gets
a warning at start-up.

## 14. Background responses and webhooks

`background: true` returns `202` with a response id after the row is durable and
before any expensive work. Status is read with `GET /v1/responses/{id}`; the work
survives client disconnect; cancellation is idempotent.

**Queued for capacity** (2026-09-13): a background job on a gated engine (§12.3)
acquires its gate **inside** the detached job while the row stays `queued`,
waiting up to `PUBLIC_API_BACKGROUND_GATE_WAIT_S` (3,600 s); past that it ends
`failed` with `model_unavailable`. A cancel while queued is honoured by the
existing pre-generation check. There is no time-based lease on a background job:
nothing but its wall clock (§8.3) ends one, and `CANCEL_POLL_SECONDS` (3 s) plus
the 15 s heartbeat bound cancel latency.

**Restarts.** A running background job does not survive an orchestrator
restart, and the deploy chain restarts the orchestrator on every push to `main`.
The job is failed with the retry-safe `model_unavailable` "The service restarted
while this response was running." (`background.repair_if_orphaned`, lazily, on
the read path). A multi-hour generation is therefore exposed to every deploy
while it runs; the public documentation says so.

V35 persists `api_responses.max_output_tokens` (the planned value at creation,
the applied value at the end) and `finish_reason` (`stop` | `length`).

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

A code-level registry (`orchestrator/app/publicapi/registry.py`) is the source of
truth — infrastructure cannot expose a model by appearing in Compose, and a
database row cannot either.

**Six public ids**, all TechSara brand ids (`PUBLIC_MODEL_IDS`):

| public id | engine key | internal target (never leaves the server) | declared when |
|---|---|---|---|
| `techsara-35b` | `main` | `settings.llm_model` on `settings.openai_base_url` | always |
| `techsara-8b-vision` | `router` | `settings.router_model` on `settings.router_base_url` | `router_base_url` set and not equal to `openai_base_url` |
| `techsara-ocr` | `ocr` | `settings.ocr_model` on `settings.ocr_base_url` | `settings.ocr_enabled` |
| `techsara-embed` | `embed` | `settings.embed_model` on `settings.embed_base_url` | `embed_base_url` set |
| `techsara-rerank` | `rerank` | `settings.rerank_model` on `settings.rerank_base_url` (`/score`) | `rerank_backend` is remote and `rerank_base_url` set |
| `techsara-whisper` | `asr` | the whisper replicas in `settings.asr_base_urls` | `settings.asr_enabled` |

The router rule exists because a profile pointing the router at the main engine
would otherwise publish the main model under a second id.

`PublicModel` carries `id`, `engine` (the closed set `main | router | ocr | embed
| rerank | asr`), `kind` (`chat | embedding | rerank | transcription`), the
capability flags `chat, streaming, vision, tools, embeddings, rerank,
audio_transcription, ocr, background`, `endpoints`, `context_window`,
`max_input_tokens`, `max_output_tokens`, `default_max_output_tokens`, `limits`,
the gate parameters and the clamp basis. `tools` is false for every model.

* `declared_models()` = the models configured and available on this deployment;
  `catalogue()` = all six with `status` `available` or `not_configured`, for the
  console.
* **The database may only narrow.** A `public_models` row can disable a declared
  model; a row naming an id the code does not declare is ignored and cannot
  conjure one. A disabled or not-configured model is absent from `GET /v1/models`
  and `404` everywhere.
* **Internal names never leave.** `to_wire()` renders `id`, `object`, `owned_by`,
  `status`, `kind`, `capabilities`, `endpoints`, `context_window`,
  `max_input_tokens`, `max_output_tokens`, `default_max_output_tokens` and
  `limits` — never the internal target, the engine key or a URL. The usage ledger
  records the public id.
* **Guards, at construction.** `guard_public_id(id)` requires
  `^techsara-[a-z0-9]+(-[a-z0-9]+)*$` and refuses any id that is, or is a
  near-miss of, an internal model name, URL, `host:port` or bare host.
  `guard_internal_target()` refuses an ADDRESS-shaped internal target (a
  `PublicModel` names a served model NAME read from settings, never a URL) and an
  engine key outside the closed set. Engine URLs are resolved only by
  `publicapi/engines.target(engine)` at call time.
* `registry.py` still imports only `..config`: the `api_contract` CI job imports
  it in a child interpreter without the engine stack.

Real limits are read at call time from settings and, where a setting is known to
be wrong versus the engine (§12.2), narrowed by the engine's own served
`max_model_len` — never hard-coded into the documentation.

## 16. Recording

Every request writes exactly one row through the existing ledger —
`usage.record_async(route=…, model=<public id>, input_tokens=…,
output_tokens=…, ttft_ms=…, duration_ms=…, status=…, meta={"api_key_id": …,
"project_id": …, "request_id": …})` — so the console and the existing analytics
pages see API traffic without a second ledger being invented. Limits off does
not mean unmetered: `quotas.reserve` counts the request and `quotas.record_usage`
settles it once.

| route | generation id | tokens | meta adds |
|---|---|---|---|
| `v1_responses`, `v1_chat_completions` | `resp_<24 hex>` | from the engine's stream usage, or counted by the server after a wall-clock stop (§8.3); `None` = not measured, never 0 | `max_output_tokens_requested`, `max_output_tokens_applied`, `clamped`, `wall_clock_s`, `usage_source` (`engine` \| `counted_at_stop` \| `null` when not measured) |
| `v1_embeddings` | `emb_<24 hex>` | input = engine `prompt_tokens` summed (None if any call did not report); output 0 (pooling generates nothing — a measured truth) | `inputs`, `engine_calls` |
| `v1_rerank` | `rrk_<24 hex>` | input = `/score` `prompt_tokens`; output 0 | `documents`, `top_n` |
| `v1_audio_transcriptions` | `asr_<24 hex>` | both `None` in `usage_events`; 0 and 0 in the token ledgers so they stay token-true | `audio_seconds`, `processing_ms`, `response_format`, `language_forced` |

* Reservation for chat models: estimated input + planned output when limits are
  enforced; when not enforced the output reservation is capped at
  `PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS`, so a running 1,000,000-token job does not
  show +1,000,000 output tokens in today's usage for hours. Settled to the measured
  count at the end either way.
* Refusals after admission (capacity `503`, validation `400`, engine failure)
  count as a request and settle reservations back as `router._nothing_ran` does;
  statuses `completed`, `failed` (increments `api_usage_daily.errors`),
  `cancelled` when nothing ran.
* `GET /v1/usage` reports requests, tokens and errors. Audio seconds are **not** in
  it; they are in `usage_events.meta` only.
* The console's Request Logs read `api_responses`, so embeddings, rerank and
  transcription calls count in usage but do not appear there (known gap).
* Failure to write any ledger never fails the response.

Key create, rotate and revoke, project and webhook changes go through the
existing audit writer (`audit(principal, request, "api_key_created", …)`).

Prompt and output content are **not** stored by default. Request logs keep
metadata only: request id, time, project, model, status, tokens, duration,
stream/background flags, error code, key prefix. Images and audio are never
stored.

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
* It does not introduce Redis, a second path to the main model, or a second usage
  ledger. The router, OCR, embeddings, reranker and speech engines are reached
  through `publicapi/engines` and `publicapi/sidecars` (§11), each under a
  capacity gate, and each metered through the one ledger.
* It does not guarantee that a multi-hour generation survives an orchestrator
  restart (§14).
* It does not change the chat application's behaviour for people who never touch
  the developer platform.
