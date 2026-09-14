# CONTRACT-3 — the TechSara developer platform

The single source of truth for the public developer API, the console at `/api`,
the documentation at `/docs`, and the capability model behind them. Every
implementation wave builds against this file; a change here is a change of
contract and must be made here first.

Grounded in the read-only audit of 2026-09-12 (`AUDIT.md`, 300 first-party files)
and the primary-source rules in `STANDARDS.md`. Every "reuse X" below names a
symbol that exists today at the cited path.

**Revision 2026-09-13, no timeouts.** §8.3, §8.4–§8.6, §9–§14, §16, §18 and the
new §19 state the no-timeout design (revision 2): no wall clock anywhere on
`/v1`, a byte at least every 15 s, durable and resumable generations, capacity
waits that never expire, audio of any length, and the v1-gateway. The contract
moves first; the public documentation describes this behaviour only once
`NO_TIMEOUT_LIVE` (`frontend/content/docs/pages/longOutput.ts`) is true, which a
test ties to the code that serves it.

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
| `frontend/app/v1/[[...path]]/route.ts` | the same-origin public edge until `api.techsarasolutions.com` exists; relays to the v1-gateway when `V1_GATEWAY_URL` is set |
| `gateway/` | the v1-gateway (§19): a dependency-free Node relay that holds `/v1` client connections across orchestrator restarts |
| `frontend/server-preload.cjs` | the Next server's timers and SIGTERM drain policy for every frontend route |

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
  → capability                        → 400 wrong endpoint for the model
  → capacity gate                     → waits, never refuses, inside the body (§12.3)
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
| GET | `/v1/responses/{id}` | `responses.read` | project-scoped; `?stream=true[&starting_after=N]` replays the event log to the creating key only (§10) |
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

The closed vocabulary (`apiplatform/scopes.py`) is **nine** scopes. The
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
| `files.read` | Read, download and use this project's files. | yes |
| `files.write` | Upload and delete this project's files. | yes |

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
* `files.read` and `files.write` (2026-09-13) arrive with `/v1/files` and
  `/v1/uploads`. A `file_id` used as model input on `/v1/responses` or
  `/v1/chat/completions` needs `files.read` IN ADDITION to `responses.write`:
  a model can be asked to repeat a file, so "may use" means "may read".
  `GET /v1/uploads/{id}` (resume) takes `files.write`. Both are defaults as
  the Files design proposes (owner decision D1, pending); the recorded
  alternative keeps `files.read` opt-in, because a leaked default key could
  otherwise download every file of its project.
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
  "store": true,
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
| body | ≤ 20 MiB on `/v1/responses` and `/v1/chat/completions` (`PUBLIC_API_MAX_MEDIA_BODY_BYTES`); ≤ 8 MiB on `/v1/embeddings` and `/v1/rerank` (`PUBLIC_API_MAX_POOLING_BODY_BYTES`); ≤ 90 MiB on `/v1/audio/transcriptions` (`PUBLIC_API_MAX_AUDIO_BODY_BYTES`); ≤ 1 MiB on every other JSON route (`PUBLIC_API_MAX_BODY_BYTES`); checked before parsing, at the edge, the gateway and the orchestrator; JSON above 64 KiB is parsed off the event loop | 413 `request_too_large` |
| text | all text in the body — `instructions` plus every string `content` and `input_text` — ≤ 1 MiB (`PUBLIC_API_MAX_BODY_BYTES`), checked after parsing: the 1 MiB text rule survives the larger media cap | 413 `request_too_large` |
| `model` | in the key's allowlist and available on this deployment | 404 `model_not_found` |
| `model` kind | a chat-kind model | 400 `invalid_request_error`, `param: model` |
| `input` | non-empty; its byte upper bound ≤ the model's `max_input_tokens` | 400 / 400 `context_length_exceeded` |
| image part | `image_url` is a `data:` URL of type `image/png`, `image/jpeg`, `image/webp` or `image/gif`, base64; its magic bytes match the declared type; `detail` absent or `auto`; on a `user` message; the model's `vision` is true; the request's image count ≤ the model's `limits.max_images_per_request` | 400 `invalid_request_error` |
| image size | each image decoded ≤ 10 MiB (`PUBLIC_API_MAX_IMAGE_BYTES`); base64 is decoded off the event loop. A request-validation failure like every other image rule (`models.validate_image_data_url`), so a `400`, not a `413`: the body as a whole was within its cap | 400 `invalid_request_error` |
| `max_output_tokens` | 1 … the model's ceiling (and the project's `max_output_tokens`); within the ceiling it is **clamped** to the remaining window (§8.3), never refused for size | 400 `invalid_request_error` above the ceiling |
| `temperature` | 0.0 … 2.0; absent → the model's default (0.2; 0.0 on `techsara-ocr`) | 400 |
| `metadata` | ≤ 16 keys, key ≤ 64 chars, value ≤ 512 chars, strings only | 400 |
| `stream` + `background` | any combination (§14); both true streams the job's events | — |
| `store` | boolean, default `true`; `false` opts the request out of the durable log (§10, §14): cancelled on disconnect and on a restart, never resumable | 400 when not a boolean; 400 `param: store` with `background: true` |

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
`store`, `max_tokens`, `max_completion_tokens`, `temperature`, `stream_options`
(only `include_usage`). `store` has the meaning of §8.1. `max_completion_tokens` is an alias of `max_tokens`; sending
both is `400`. A message's `content` is a string or, on a `user` message, a list
of `{"type": "text", "text"}` and
`{"type": "image_url", "image_url": {"url": "data:…", "detail": "auto"}}` parts,
under exactly the image rules of §8.1. There is no `background`.

### 8.3 The output ceiling and the shared window (owner decision, 2026-09-13)

**Setting.** `PUBLIC_API_MAX_OUTPUT_TOKENS` (default 1,000,000). The chat
application's `MODEL_MAX_OUTPUT` is not changed and is no longer read by `/v1`.

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

**No wall clock** (no-timeout design, 2026-09-13). No `/v1` generation is ended
because of the time it has run. `planning.wall_clock_for`, the `streaming.Generation`
backstop and the sidecar wall clocks are deleted; `PUBLIC_API_GEN_WALL_CLOCK_S`,
`PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S` and `PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S`
are retired and ignored with one start-up warning each. The public path calls
`llm.stream_chat_events(…, wall_clock_s=0, wall_clock_marker=False,
read_timeout_s=None, admission_patient=True, on_dispatch=…)`; the chat
application's calls are byte-for-byte unchanged.

Liveness is judged from engine evidence, never from a clock
(`publicapi/liveness.MainGuard`, ticked at most every 15 s once an attempt is
dispatched and silent for ≥ `PUBLIC_API_LIVENESS_QUIET_S` 120 s):

* **engine** — `engine_state.proven_not_serving()` non-`None` for ≥ 30 s (WEDGED,
  RECOVERING, a real STARTING, any DOWN except a cold-start timeout on an old
  head), or the head changed;
* **lost** — the engine serving, `requests_running + requests_waiting == 0` on 3
  samples ≥ 10 s apart, and the attempt dispatched ≥
  `PUBLIC_API_LIVENESS_LOST_MIN_S` 300 s ago;
* **blind** — no verdict (`serving()` is `None`) and ≥ 3,600 s of silence.

Otherwise it waits with no limit: a 30-minute silent prefill, a KV-preemption
recompute and a controller restart during a prefill are all waited out. **An
interrupt is not a failure**: the engine stream is closed, the attempt recorded,
`engine_state.wait_not_bad()` awaited, and the run continues with
`continue_final_message` from the text already emitted (§14). What ends a
generation is listed in §18.

When the room left for a continuation is below `MIN_OUTPUT_TOKENS` +
`CONTEXT_SAFETY_MARGIN` (256 + 512), the run settles as an output-limit stop
(`finish_reason` `length`, `incomplete_details.reason` `max_output_tokens`), not as
a failure. **Where usage comes from** when a stream is closed before vLLM's last
chunk (cancel, failure, interrupt): the server counts — `output_tokens` = the
deltas received, `input_tokens` = the exact `/tokenize` count `llm._fit` took, or
the planner's estimate — and `usage_events.meta.usage_source` says
`counted_at_stop`; `engine` otherwise (§16). Input is counted once however many
attempts a run takes.

**Every mode suits long work.** A synchronous request commits after
`PUBLIC_API_SYNC_COMMIT_S` (12 s by default, never more than 15) and writes a
space at least every 15 s (§10), so
Cloudflare's 125 s first-byte limit and every idle timer on the path are
satisfied. A client that disconnects detaches rather than cancels (§14): keyed
requests and Responses streams keep generating for
`PUBLIC_API_STREAM_ORPHAN_GRACE_S` 600 s, other synchronous requests and chat
streams for `PUBLIC_API_UNKEYED_ORPHAN_GRACE_S` 120 s, attachable meanwhile;
`store: false` is cancelled at the disconnect, gives its gate and admission slot
back, and records `cancelled` with what the engine produced.

### 8.4 `POST /v1/embeddings`

Body (≤ 8 MiB, `PUBLIC_API_MAX_POOLING_BODY_BYTES`; `extra="forbid"`):

```json
{"model": "techsara-embed", "input": ["first text", "second text"], "encoding_format": "float"}
```

* `input`: a non-empty string, or 1–2,048 non-empty strings
  (`PUBLIC_API_EMBED_MAX_INPUTS`, request shape, not usage). Token-array input
  is `400`.
* `encoding_format`: `float` (default) or `base64` (little-endian float32).
* `dimensions`, `user` and any other field → `400` naming it.
* Each input ≤ 4,096 tokens; over it is `400 context_length_exceeded`,
  `param: input.<i>`. **Checked before any gate**: an input whose UTF-8
  bytes + 2 ≤ 4,096 cannot overflow; longer inputs are counted with the
  engine's `/tokenize` (CPU only, no KV) through ONE process-wide gate per
  engine (`PUBLIC_API_TOKENIZE_CONCURRENCY` 8, which chat never passes
  through), for at most `PUBLIC_API_LENGTH_CHECK_BUDGET_S` (8 s, ≤ 15) before
  the status line. The time spent comes off the commit window. What is not
  counted by then is counted inside the committed response (retried, never
  skipped); counts are remembered by text sha256, so an over-length input
  found after the commit — which drops the connection — is a real `400` on
  the SDK's retry. A `4xx` from `/tokenize` (no tokenizer route) sends the
  input at the window weight and the engine's own `400` names it; that refusal
  is remembered the same way, so a retry is refused before its status line. Inputs are
  embedded as given: no query instruction is added and nothing is clipped.
* **Memory guard, before the body is read**: accepted-but-unfinished
  embeddings and rerank work is charged against one process-wide budget,
  `PUBLIC_API_POOLING_MEMORY_BYTES` (512 MiB) — twice the body (three times
  for rerank) plus 1,024 × 56 bytes per float vector (× 20 for base64). A
  request that does not fit is `503 model_unavailable`, `Retry-After: 5`,
  before the status line: first on its declared `Content-Length` (not yet
  counted against quota), then on its parsed input count (counted, settled as
  nothing ran). A request alone is always admitted. Vectors are held as
  doubles and rendered one at a time.

`200`:

```json
{"object": "list", "model": "techsara-embed",
 "data": [{"object": "embedding", "index": 0, "embedding": [0.0123, -0.0456]}],
 "usage": {"prompt_tokens": 12, "total_tokens": 12}}
```

Vectors have 1,024 dimensions. `usage` is `null` when any engine call did not
report counts. Inputs are packed into engine calls of ≤ 16 inputs and ≤ the gate
budget, sent in order under gate `embed`, which waits with no limit (§12.3);
`data[].index` preserves input order. The response is a
`keepalive.CommittedJSONResponse(failure_mode="abort")` (§10).

Engine calls: read timeout `PUBLIC_API_POOLING_SILENCE_S` 600 s, then the
`liveness.SidecarWitness` verdict decides — `progressing` re-sends, `unknown`
re-sends up to 3 times, `lost` once, `stalled` fails `503 model_unavailable`.
A connection that breaks with the call out is re-sent once. An engine restart
is **implicated** in a call when its witness (held for the whole call, re-sends
included) saw `process_start_time_seconds` change, or when the call's connection
broke and the next send found the engine refusing connections. The first
implicated restart under a call of several inputs re-sends them one at a time;
the second fails the request `503 model_unavailable` with
`x-should-retry: false` and `param: input.<i>`, and that input (by sha256) is
refused the same way **before any gate or status line** for
`PUBLIC_API_POISON_QUARANTINE_S` (3,600 s) — so an SDK retry of a request that
already committed is refused with a header it obeys, and the engine goes down
twice per input, not twice per retry. Before commit those are real statuses;
after commit the connection is aborted, and a gateway-tagged request is first
recomputed by the gateway's re-POST (§19).

### 8.5 `POST /v1/rerank`

Body (≤ 8 MiB, `PUBLIC_API_MAX_POOLING_BODY_BYTES`; `extra="forbid"`):

```json
{"model": "techsara-rerank", "query": "How do I rotate a key?",
 "documents": ["Keys are rotated in the console.", {"text": "Webhooks are signed."}],
 "top_n": 1, "return_documents": true}
```

* `query`: non-empty string. `documents`: 1–1,000 strings or `{text}` objects
  (`PUBLIC_API_RERANK_MAX_DOCUMENTS`). `top_n` ≥ 1, default all.
  `return_documents` default false. `instruction` optional, ≤ 512 characters,
  default `rerank.DEFAULT_INSTRUCTION`.
* The server applies the model's template (`PREFIX`, `<Instruct>`, `<Query>`,
  `<Document>`, `SUFFIX` from `app/rerank.py`) with **no truncation**; a
  templated pair over 4,096 tokens is `400 context_length_exceeded`,
  `param: documents.<i>`, checked before any gate or commit exactly as §8.4.

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
to the public surface. Waits, commit and engine failures behave as §8.4, under
gate `rerank`.

### 8.6 `POST /v1/audio/transcriptions`

`multipart/form-data`, streamed to disk as it arrives (`publicapi/multipart.py`
streaming reader handing the file part to a `disk_ledger.DiskSink`, never
`UploadFile`): the file part
goes to `PUBLIC_API_ASR_CACHE_DIR` through a `DiskSink` — sha256 on the way,
fsync and replace, 0700 directory and 0600 files, caps enforced while reading,
reserved against free disk first. An `application/json` body
`{model, file_id, language, response_format, stream}` is also accepted.

| field | rule |
|---|---|
| `file` | required unless `file_id`; the part's `Content-Type` in `audio_api.ALLOWED_TYPES`; ≤ 89 MiB (`PUBLIC_API_MAX_AUDIO_BYTES` 93,323,264); **any duration** |
| `file_id` | instead of `file`: a project-scoped audio or video file of the Files API, used in place (never copied or deleted); needs `files.read` too; a malformed, deleted, expired or foreign id is the same `404 file_not_found` |
| `model` | required, `techsara-whisper` |
| `language` | optional ISO-639-1 code, or `auto` (the default) |
| `response_format` | optional `json` (default), `text` or `verbose_json`; `srt` and `vtt` are `400` |
| `timestamp_granularities[]` | optional, only `segment`, only with `verbose_json` |
| `stream` | optional boolean; `true` answers `text/event-stream` |
| anything else | `temperature`, `prompt` and any other field is `400` naming it |

Body ≤ 90 MiB (`PUBLIC_API_MAX_AUDIO_BODY_BYTES` 94,371,840). `PUBLIC_API_MAX_AUDIO_SECONDS`
is removed: there is no duration limit. An undecodable file is `400` with a fixed
sentence — never the decoder's stderr.

**Pipeline** (`publicapi/audio_jobs.py`):

1. job key = sha256(project, audio sha256, language, format class, chunking
   parameters); a start with the same key joins the running job;
2. decode just in time — only when the job is at most one place behind the head
   of the `asr` queue — under `PUBLIC_API_DECODE_CONCURRENCY` 2 as `nice -n 19
   ionice -c 3 ffmpeg -protocol_whitelist pipe …`, killed after 120 s without
   progress, with the decoded PCM (32,000 B/s) reserved first in the process-wide
   `DiskLedger` (free space minus `PUBLIC_API_MIN_FREE_DISK_BYTES`); an owned
   source is deleted right after decode;
3. windows of ≤ 90 s planned at pauses (3 s overlap), dispatched one at a time
   under gate `asr` (waits with no limit) to the replica dictation does not
   prefer, each with a `/health` watch every 15 s, 1,800 s of silence and one
   re-send;
4. a content-addressed window cache (sha256 of project, model, language and the
   exact clip bytes; 24 h) so a retry reuses finished windows;
5. word-level seam alignment and loop collapsing, with a holdback so streamed
   text is never retracted.

`200` for `json`: `{"text": "…", "usage": {"type": "duration", "seconds": 42}}`
(`seconds` = ceil of decoded samples / 16,000). `text`: a `text/plain` body —
after a commit it carries leading spaces, which the documentation tells callers to
strip. `verbose_json`: `{task: "transcribe", language: <lowercase language name
or null>, duration, text, segments: [{id, start, end, text}], usage}`. A clip
the silence gate judges empty returns empty text. `stream: true`: `: ping` first
and every 15 s, `: queued` while waiting, `transcript.text.delta` and one
`transcript.text.done`; an error object on failure; no `id:`, `retry:` or
`event:` lines. Non-stream formats are `CommittedJSONResponse(failure_mode="abort")`;
the gateway re-attaches with `X-TechSara-Attach-Job` before aborting a client
(§19). A tagged request is named `X-TechSara-Run: job:<job key>-<response_format>`,
because a `json` and a `text` request share one job and the empty re-attach body
cannot say which it was; the re-attach is neither counted nor metered again. A forced `language` forces the decoder: `en` on non-English speech
translates rather than transcribes (2026-09-10), so auto-detect is the
recommendation.

**All three sidecar endpoints**: the same `PublicRoute` (§9 envelope,
`X-Request-Id`, CORS without credentials); Bearer only; scope, then origin; one
`quotas.reserve(kind="sync")`; model resolution and the capability check of §7;
an `Idempotency-Key` header is `400`, `param: Idempotency-Key`; no
`api_responses` row; lengths and shape checked before any gate; gates wait with
no limit.

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
| `idempotency_conflict` | 409 | same key with a different body, or from a different credential (not the creating key or its service account) — `x-should-retry: false` |
| `request_too_large` | 413 | body over its route's cap, text over 1 MiB, an audio file part over 89 MiB |
| `context_length_exceeded` | 400 | prompt over the model's input ceiling; an embeddings input or a rerank pair over 4,096 tokens; no window left for one output token |
| `rate_limit_error` | 429 | RPM/TPM exceeded, only when limits are enforced — `Retry-After` |
| `quota_exceeded` | 429 | daily/monthly token quota, only when limits are enforced — `Retry-After` |
| `concurrency_limit_exceeded` | 429 | too many of the project's requests in flight, only when limits are enforced — `Retry-After` |
| `model_recovering` | 503 | engine restarting — `Retry-After`, retry-safe |
| `model_unavailable` | 503 | engine proven down or failing (§18), or a physical guard before headers: fd pressure ≥ 70 % (`Retry-After: 30`), free disk under `PUBLIC_API_MIN_FREE_DISK_BYTES` (`Retry-After: 60`) or, on embeddings and rerank, the pooling memory budget (`Retry-After: 5`, §8.4) — never "at capacity" on `/v1` — `Retry-After` ≤ 60, retry-safe unless `x-should-retry: false` |
| `timeout` | 504 | kept in the closed table; no `/v1` request is ended by a clock (§8.3) |
| `internal_error` | 500 | anything else — never a traceback |

The code table is closed. `errors.model_at_capacity(retry_after)` exists only for
callers that pass a finite wait (`capacity.hold(wait_s=…)`), and no `/v1` route
does.

**`x-should-retry`** (both SDKs read it before their own retry table) is `false`
on: a `409` for a different body or credential; a `500` after a generation
without an `Idempotency-Key` started; the second engine-fault failure of a run
(§14); an embeddings or rerank input implicated in two engine restarts, and
every request carrying it during its quarantine (§8.4); and the edge's
post-send `503` for an unkeyed generation when no gateway is in the path.

**A failure after commit** (§10) cannot change the status: on `/v1/responses` it
is a well-formed Response with `status: "failed"`, its `error` and its partial
output; on `/v1/chat/completions` a `chat.completion` with `choices: []` and
`error`. Both parse in openai-python and openai-node, which do not treat a `200`
with an error body as an exception; the documentation tells callers to check.

No response body may contain a traceback, SQL, an environment value, a container
name, an internal hostname, a filesystem path, a private IP, an internal
checkpoint name or an engine URL.

## 10. Streaming (SSE)

### 10.1 The byte invariant

**From the end of authentication and validation, a `/v1` response writes its
status line and a first body byte within 15 s, and never goes more than 15 s
between bytes after that.** Next does not flush a status line before the first
body byte, so "early headers" always means headers plus one byte. Cloudflare
waits 125 s for a first origin byte (then `524`); nothing on `/v1` relies on
that number.

* **SSE** (Responses and chat streams, the resume stream, transcription with
  `stream: true`): the status and a first frame go out at once —
  `response.created` for Responses, `: ping` for chat, transcription, the
  resume stream and `stream` + `background` (whose `response.created` is
  written when the dispatcher claims the job). Every capacity and admission
  wait happens in the body.
* **Non-stream JSON** (responses, chat completions, embeddings, rerank,
  transcription `json`/`text`/`verbose_json`) is
  `keepalive.CommittedJSONResponse`: the real status and body when ready within
  `PUBLIC_API_SYNC_COMMIT_S` (12 s by default, clamped to at most 15 s: the
  clock starts after authentication and the quota reads, and a later commit
  measured a first byte at 15.07 s); otherwise `200` with `Cache-Control:
  no-store, no-transform`, one space at once, a space every
  `events.HEARTBEAT_CEILING_S` (14 s, so a late timer tick stays inside 15 s),
  then the object. Failures after commit are §9's failed bodies (generation) or an
  aborted connection (sidecars).
* **The only refusals before the status line**: the concurrency slot (only with
  limits enforced), the fd guard (`503`, `Retry-After: 30`) and the disk guard
  (`503`, `Retry-After: 60`).

Headers: `Content-Type: text/event-stream`, `Cache-Control: no-store, no-cache,
no-transform`, `X-Accel-Buffering: no`, `Connection: keep-alive`, no
`Content-Length`.

### 10.2 The Responses grammar

Events, each `event: <name>` plus a JSON `data` whose `type` repeats the name and
which carries a `sequence_number` starting at 1 and increasing by exactly 1,
contiguous and never reused, in the JSON only:

```
response.created → response.queued → response.in_progress
  → response.output_item.added → response.content_part.added
  → response.output_text.delta (×N) → response.output_text.done
  → response.output_text.annotation.added (×K)
  → response.content_part.done → response.output_item.done
  → response.completed | response.failed | error
```

`response.queued` is sent while the request waits for its engine (a gate, the
admission lane, a recovering engine), on any engine, followed by `: ping`
comments. The item and content-part events exist because both SDKs' `stream()`
helpers require them: `output_item.added` and `content_part.added` precede the
first delta (or `output_text.done` when there is no text); a failure after them
goes straight to `response.failed`. `response.output_text.annotation.added` is
sent once per `file_citation` annotation of an answer about the caller's files
(`{item_id, output_index, content_index, annotation_index, annotation}`), after
`output_text.done`; `content_part.done`, `output_item.done` and the terminal
Response repeat the annotations on the `output_text` part. On Chat Completions
they are the final chunk's `delta.annotations`. The same grammar is framed
from the durable log (`durable.RecordBuilder`) and, for `store: false`, in
memory (`streaming.responses_sse`). A heartbeat comment (`: ping`) goes out at least every
15 s (`events.HEARTBEAT_SECONDS` = min(`SSE_HEARTBEAT_SECONDS`, 14)) **for the
whole life of the stream, on every engine**. The terminal event carries `usage`; non-terminal events carry
`usage: null`. Exactly one terminal event is ever emitted. There are **never**
`id:` or `retry:` lines: openai-python's decoder breaks on an `id:` line
followed by a comment, and resumption is explicit (§10.3).

Every response snapshot in the stream carries `max_output_tokens` and
`incomplete_details` (§9): the planned value before generation, the **applied**
value on the terminal. On Chat Completions the chunk carrying `finish_reason`
carries the applied `max_output_tokens`, a wait is `: queued` comments, and a
failure is one error chunk with `choices: []` followed by `data: [DONE]`.

The generator is always closed: `finally: await stream.aclose()`.

### 10.3 Resuming

`GET /v1/responses/{id}?stream=true[&starting_after=N]` replays the events with
`sequence_number > N` (all without `N`), tails the run live with `: ping`, and
closes after the terminal event — at once, whatever `N`, for a run already
terminal. The checks, **in this order**: the project-scoped lookup (`404`); scope
`responses.read`; the creator check — the same `key_id`, or a key of the same
service account, else `404`; streamability — `400`, `param: stream`, when the
run was `store: false` (or not durable) or its events are past retention;
`starting_after` without `stream=true` is `400`, `param: starting_after`.
`stream` takes `true`/`false` (else `400`, `param: stream`); `stream=false`
without `starting_after` is the plain JSON read. At most 8 followers per
response; a ninth closes the oldest. A reader of a run that is suspended (a
restart) claims and resumes it in the process serving the read
(`router._resume_stream`, `durable.Runtime.attach_row`).

A stream that ends **without a terminal event** — the connection closed, or
the service restarted and ended every reader with an incomplete read — did not
finish: the client resumes it with this route. A synchronous call cut the
same way (after its committed `200`) is dropped mid-body; before the commit it
is `503 model_unavailable` with `Retry-After: 2`. Either way the SDK's retry
attaches (§13) instead of generating again.

Chat Completions has no resume route: a retry with the same `Idempotency-Key`
and the same credential replays the chunks from the start, then continues live
to `[DONE]`, with a stable completion id (§13).

Events are written ahead: no `sequence_number` reaches a client before it is
committed. They are kept `PUBLIC_API_EVENT_RETENTION_S` (3,600 s) after the
terminal event, then purged in batches.

### 10.4 Internal frames

A gateway-tagged request from a peer in `PUBLIC_API_GATEWAY_PEERS` also gets
`: ts-seq=N` after each data frame (§19). The gateway strips them, and so does
the Next edge, defensively; no public response ever carries one.

## 11. Execution path

```
client → Cloudflare → cloudflared → v1-gateway (§19) → orchestrator → engines
          (LAN: frontend /v1 edge → v1-gateway, or the orchestrator directly)

/v1/responses, /v1/chat/completions
  → plan          publicapi/planning.plan_generation: model, ceilings, clamp,
                  gate                                                     (§8.3)
  → meter         quotas.reserve (limits only with PUBLIC_API_ENFORCE_LIMITS)
  → launch        publicapi/durable.launch: row, spec, lease, event log   (§14)
                  (attach first: gateway attempt, Idempotency-Key, implicit)
  ├─ techsara-35b
  │    → gate 'main.long' when planned output > 800,000,
  │      else 'main.extended' when planned output > 8,192, and 'main.normal'
  │      (6) for every public generation that does not take 'main.long' —
  │      all waiting with no limit (the prompt never picks a gate; admission's
  │      LONG lane sizes it)
  │    → llm.stream_chat_events(messages, model_choice="smart", effort="fast",
  │                             temperature=…, max_tokens=…, wall_clock_s=0,
  │                             wall_clock_marker=False, read_timeout_s=None,
  │                             admission_patient=True, on_dispatch=…)
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
  (`PUBLIC_API_SIDECAR_SILENCE_S` 1,800 s, used only when the witness verdict is
  unknown) — never a caller-derived value, because `llm._client`'s cache key
  must not carry one (F048). Witness verdicts (`progressing`, `stalled`, `lost`,
  `restarted`, `unknown`) drive failure or re-dispatch; nothing uses a wall clock. Router reasoning is off
  (`llm.reasoning_extra_body(settings.router_capabilities, False)`) and
  `temperature` is always sent, so the checkpoint's own default never applies.
* **Embeddings, reranker, speech: `publicapi/sidecars`** and
  `publicapi/audio_jobs`, with their own engine calls (embed and rerank read
  timeout `PUBLIC_API_POOLING_SILENCE_S` 600 s with witness-guided re-sends,
  whisper windows with 1,800 s of silence and one re-send). Not `llm.embed_texts` (clips and drops usage), not
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
| `techsara-embed` | embedding | 4,096 | 4,096 per input | none | none | ≤ 2,048 inputs, 1,024 dimensions |
| `techsara-rerank` | rerank | 4,096 | 4,096 per pair | none | none | ≤ 1,000 documents |
| `techsara-whisper` | transcription | none | none | none | none | any duration, ≤ 89 MiB of audio per request |

| body | cap |
|---|---|
| `/v1/responses`, `/v1/chat/completions` | 20 MiB (`PUBLIC_API_MAX_MEDIA_BODY_BYTES`), of which text ≤ 1 MiB |
| `/v1/embeddings`, `/v1/rerank` | 8 MiB (`PUBLIC_API_MAX_POOLING_BODY_BYTES`) |
| `/v1/audio/transcriptions` | 90 MiB (`PUBLIC_API_MAX_AUDIO_BODY_BYTES`), file part ≤ 89 MiB |
| every other route | 1 MiB (`PUBLIC_API_MAX_BODY_BYTES`) |

**The path to the API** (measured or published, 2026-09-13), which the byte
invariant (§10.1) is written against:

* Cloudflare waits **125 s** for the first origin byte, then answers `524`;
  between writes to the origin it allows **30 s** (the Proxy Write Timeout), and
  a request body is at most **100 MB**. Only Enterprise can change the first two.
* cloudflared: `connectTimeout` 30 s and `keepAliveTimeout` 90 s (defaults, no
  override). The v1-gateway's `keepAliveTimeout` is 95 s and the Next server's
  95 s, so the tunnel always closes an idle connection first.
* The Next standalone server's defaults (`requestTimeout` 300 s, measured `408`
  at 325 s) are replaced by `frontend/server-preload.cjs`: `requestTimeout` 0,
  `headersTimeout` 100,000 ms, `keepAliveTimeout` 95,000 ms and a 60 s body-idle
  guard. The `/v1` edge's upstream fetch has no duration timer: no header or body
  timeout on its hop to the gateway, and on the direct path only a 300 s silence
  limit (`V1_EDGE_UPSTREAM_SILENCE_S`, not counting a request body still
  uploading), which with the 15 s rule only a stuck orchestrator reaches.
* The tunnel itself drops connections (4 episodes in 9 days, 2026-09): no timer
  fixes that, which is why §10.3 exists.

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
shared by every project and key**, FIFO — and on `/v1` **its wait never
expires** (`publicapi/capacity.py`: `hold(engine, *, weight_tokens=0,
wait_s=None, yield_to_chat=False, abandon=None, on_wait=None)`). A wait ends only
on admission, the client abandoning, a public yield to chat (which suspends and
re-queues, never fails), the engine-down grace (§18), or a physical guard before
headers (§9). `on_wait` is called on entry, on every position change and at
least every 15 s; streams show it as `response.queued` or `: queued`, sync as
whitespace, background as a `queued` row. Only a caller passing a finite
`wait_s` can get `errors.model_at_capacity`, and none on `/v1` does.

A request is admitted when `in_flight < max_concurrent` and `used_tokens +
min(weight, budget) ≤ budget`; a request heavier than the whole budget is
admitted only when no other public request holds that engine. Released in
`finally`, cancellation-safe; configuration read at call time; gauges
`public_api_engine_in_flight`, `public_api_engine_waiting` and
`public_api_engine_oldest_wait_seconds` by engine.

| gate | public concurrency | public KV budget (tokens) | weight | yields to chat | wait |
|---|---|---|---|---|---|
| `main.normal` | 6 | none | — | no | no limit |
| `main.extended` | 2 — admission's LONG_OUTPUT seats | admission's KV budget | — | to a chat LONG request | no limit |
| `main.long` | 1, then one of admission's LONG_OUTPUT seats | admission's KV budget | — | to a chat LONG request, by suspending | no limit |
| `router` | 4 | 24,576 | input at its byte bound + planned output | no | no limit |
| `ocr` | 2 | none | — | yes | no limit |
| `embed` | 2 | 8,192 | Σ min(upper bound, 4,096) per engine call | no | no limit |
| `rerank` | 2 | 8,192 | templated pair bound per engine call | no | no limit |
| `asr` | 1, fleet-wide | none | — | yes, and to dictation | no limit |

Why these numbers:

* **Sizes are counted at the byte bound** (2026-09-13, adversarial review): a
  gate's footprint uses the prompt's UTF-8 byte length (`context.upper_bound_
  messages`), never the 3-characters-per-token estimate, because the Qwen
  pre-tokenizer isolates every digit.
* **The main gates are chosen from the planned output only** (integration
  2026-09-13). Sizing `main.long` by input at its byte bound plus output sent
  every ~123 KB document (35–47k real tokens) through the one-at-a-time gate,
  and one 1M-output job refused them all. A long prompt is admission's LONG
  lane, decided on the exact `/tokenize` count at the call.
* **One accounting.** A main gate admits the answer into admission's
  LONG_OUTPUT lane (`admission.preadmit`: its seats, 2, and its KV budget)
  BEFORE the status line — for a `/v1` caller with no time limit, the
  pre-admission is patient (merged with the no-timeout design 2026-09-14) — and hands the ticket to the generation
  (`admission.use_preadmitted`), together with the planned output: a retry
  inside the generation is admitted into LONG_OUTPUT again, a LONG prompt is
  charged prompt plus output, and a pre-admitted call waits out a LONG closure
  raised after its grant. An answer admission does not take into LONG_OUTPUT (a
  LONG prompt, or `ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS` set above
  `PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS`) is capped by the gate's own count,
  `PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT` (2).
* **`main.long`** applies to a `techsara-35b` request planning more than
  `PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS` (800,000). The KV pool is 1,663,201
  tokens: two such answers are the whole pool, so the gate runs them one at a
  time whatever `ADMISSION_KV_RESERVE_FRACTION` is; at 800,000 and below a
  second answer is admission's KV decision.
* **`main.extended`** applies to every other `techsara-35b` request that plans
  more than `PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS` (default 8,192 — the public
  default, and the public ceiling before 2026-09-13). Measured by the review: with
  no gate below 131,072, ten public 130,000-token answers held all ten of the
  chat application's NORMAL admission slots for about 22 minutes each and a chat
  turn was refused; in LONG_OUTPUT they hold at most its two seats. A request at
  or under 8,192 holds a slot for at most about 115 s at 71 tok/s and takes no
  public gate: the shared NORMAL lanes remain its gate.
* **`main.normal`** (`PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT` 6 =
  `ADMISSION_NORMAL_MAX` 10 − `PUBLIC_API_CHAT_NORMAL_RESERVE` 4) covers every
  public `techsara-35b` generation that does not take `main.long`, held from
  dispatch to the end of the attempt and released while suspended. At most six
  public generations are inside the NORMAL lane at once, so a public flood can
  never reach chat's `max_waiting` refusal (measured with the real Lane: 400
  patient waiters refused chat instantly).
  A `main.extended` answer holds `main.normal` too, taken after it, so the two
  gates cannot deadlock.
* **Both main gates step aside for a chat LONG request still before its first
  token** (waiting for its seat or KV, waiting for the engine to go idle, or
  holding the lanes closed; `capacity.chat_long_admission_present`), for the
  whole gate wait. A chat document already decoding does not hold public work.
* **Chat documents beside a public long answer are an owner setting**
  (`ADMISSION_CHAT_LONG_BESIDE_V1_ANSWERS`, app/admission.py). With `refuse`
  (the default) a decoding public long answer is work in front of a chat LONG
  request — the GDN fault rule — so while a public 1M answer runs (up to about
  5.8 h) a chat document above 131,072 tokens waits `ADMISSION_LONG_WAIT_S`
  (600 s) and is refused, and its KV cannot fit beside the answer's 1,008,176
  tokens either. With `proceed` the document goes in beside public long answers
  (a large prefill beside up to two decodes, chosen knowingly), within the
  managed limit. Either way, after such a refusal no new public long answer is
  admitted for `ADMISSION_V1_LONG_OUTPUT_CHAT_HOLD_S` (1,800 s) or until a chat
  document runs, so consecutive public jobs cannot keep chat documents out.
  The `refuse` default is the reviewed engineering default and is still
  **pending the owner's explicit decision** (2026-09-14); the costs, the
  choices and the decision record are in OPERATIONS.md §15.
* **Admission lanes are two-class** (`admission.py`): chat and patient (public)
  FIFO deques, one grant per release, chat first; `max_waiting` counts chat
  waiters only; patient waiters have no timeout. Chat's own bounds
  (`ADMISSION_NORMAL_WAIT_S`, `ADMISSION_LONG_WAIT_S` 600 s) are unchanged.
* **A patient LONG ticket is released at first token**, keeping a `kv_ledger`
  entry (input + planned output) until the stream ends. Chat LONG admission also
  requires its footprint + the ledger ≤ `PUBLIC_API_SHARED_KV_BUDGET_TOKENS`
  (1,400,000 of the 1,663,201-token pool).
* **Yield**: a chat LONG waiter blocked by a patient holder's prefill ticket or
  by the ledger calls that holder's yield callback; `durable.suspend(run,
  "yield")` closes its engine stream within 1 s and re-queues it at the head of
  `main.long` behind the chat request. The client sees heartbeats; a yield never
  counts as stalled or as an engine fault; the cost is one re-prefill.
* **`router`**: the public budget is 47% of the 52,512-token pool, so chat keeps
  at least 27,936 tokens.
* **`ocr`**: engine `--max-num-seqs 8`; chat `read_images` batches use 4 and
  video OCR 4, so 2 public leaves chat its share; public OCR yields.
* **`embed`** and **`rerank`**: the public budget is 44% of each 18,720-token
  pool, so chat keeps at least 10,528 tokens.
* **`asr`**: whisper decodes one clip per replica, and saturating either Spark
  takes chat decode from 71.4 to about 24 tok/s because the main model is TP=2.
  One public window at a time, on the replica dictation does not prefer; a public
  window starts only when a replica stays free after it starts; dictation-busy is
  a wait, not a refusal. Decodes run under `PUBLIC_API_DECODE_CONCURRENCY` 2.

**Yield to chat** before taking a slot, while a chat generation is in flight
(`video.pipeline._busy_probe`): up to `PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S` (10 s)
in 1 s steps, then the slot. Public NORMAL work does the same before a LONG
re-prefill.

### 12.4 Settings

Every `PUBLIC_API_*` value below follows `config.py`'s `_int` / `_float` rules
(blank means the default; a malformed value fails at start-up). `config.py`
declares them since the 2026-09-13 integration; readers still use
`getattr(settings, <lower name>, None)` with an `os.environ` fallback parsed by
the same rules, so a reader running beside an older `config.py` gets the same
defaults. The webhook settings are additionally clamped at use
(`apiplatform/webhooks/queue.py`).

| setting | default | governs |
|---|---|---|
| `PUBLIC_API_ENFORCE_LIMITS` | false | §12.1 |
| `PUBLIC_API_MAX_BODY_BYTES` | 1,048,576 | JSON body cap; the text rule |
| `PUBLIC_API_MAX_MEDIA_BODY_BYTES` | 20,971,520 | body cap on the two generation routes |
| `PUBLIC_API_MAX_POOLING_BODY_BYTES` | 8,388,608 | body cap on embeddings and rerank |
| `PUBLIC_API_MAX_IMAGE_BYTES` | 10,485,760 | one decoded image |
| `PUBLIC_API_MAX_AUDIO_BODY_BYTES` | 94,371,840 | transcription body |
| `PUBLIC_API_MAX_AUDIO_BYTES` | 93,323,264 | transcription file part |
| `PUBLIC_API_MAX_OUTPUT_TOKENS` | 1,000,000 | output ceiling (§8.3) |
| `PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS` | 8,192 | output default |
| `PUBLIC_API_SYNC_COMMIT_S` | 12, clamped to at most 15 | commit of a non-stream JSON response (§10.1) |
| `PUBLIC_API_STREAM_ORPHAN_GRACE_S` | 600 | a keyed request or Responses stream without a reader |
| `PUBLIC_API_UNKEYED_ORPHAN_GRACE_S` | 120 | any other request without a reader |
| `PUBLIC_API_IMPLICIT_ATTACH_WINDOW_S` | 3,600 | implicit attach (§13) |
| `PUBLIC_API_SUSPENDED_UNREAD_TTL_S` | 900 | a suspended run nobody reads is cancelled with no engine work |
| `PUBLIC_API_RESUME_ENABLED` | true | kill switch: false fails a suspended run `model_unavailable`, its log stays readable |
| `PUBLIC_API_RESUME_STAGGER_S` | 30 | background resumes after a restart, one per interval, oldest first |
| `PUBLIC_API_RESUME_MAX_STALLED_ATTEMPTS` | 3 | stalled attempts before failure (§18) |
| `PUBLIC_API_ENGINE_DOWN_GRACE_S` | 1,800 | engine proven down before an undispatched run fails |
| `PUBLIC_API_QUARANTINE_SERVING_S` | 300 | proven serving before a quarantined run is dispatched |
| `PUBLIC_API_QUARANTINE_MIN_RECOVERIES` | 2 | recoveries left in the controller budget before it is |
| `PUBLIC_API_LIVENESS_QUIET_S` | 120 | silence before the main liveness guard evaluates |
| `PUBLIC_API_LIVENESS_NOT_SERVING_S` | 30 | proven not serving before an interrupt |
| `PUBLIC_API_LIVENESS_LOST_MIN_S` | 300 | dispatched before the `lost` rule may fire |
| `PUBLIC_API_LIVENESS_UNKNOWN_SILENCE_S` | 3,600 | silence under an unknown verdict before an interrupt |
| `PUBLIC_API_SIDECAR_SILENCE_S` | 1,800 | router and OCR read timeout, used only when witnesses are unknown |
| `PUBLIC_API_POOLING_SILENCE_S` | 600 | embeddings and rerank read timeout before the witness decides (the Files API's direct engine helpers keep 60 s) |
| `PUBLIC_API_TOKENIZE_CONCURRENCY` | 8 | public `/tokenize` calls in flight per engine, process-wide |
| `PUBLIC_API_LENGTH_CHECK_BUDGET_S` | 8, clamped to at most 15 | length counting before the status line; the rest is counted after the commit |
| `PUBLIC_API_POOLING_MEMORY_BYTES` | 536,870,912 | accepted-but-unfinished embeddings and rerank work, process-wide |
| `PUBLIC_API_POISON_QUARANTINE_S` | 3,600 | an input implicated in two engine restarts is refused before its gate |
| `PUBLIC_API_DECODE_CONCURRENCY` | 2 | concurrent audio decodes |
| `PUBLIC_API_EVENT_RETENTION_S` | 3,600 | stream events kept after the terminal event |
| `PUBLIC_API_PENDING_EVENTS_MAX_BYTES` | 67,108,864 | unwritten events per process before the largest run suspends (`store`) |
| `PUBLIC_API_FOLLOWER_POLL_S` | 1 | the shared follower poller |
| `PUBLIC_API_MIN_FREE_DISK_BYTES` | 21,474,836,480 | disk guard for launches, audio decode and the gateway spool |
| `PUBLIC_API_FD_GUARD_RATIO` | 0.70 | fd guard before headers |
| `PUBLIC_API_BLOB_DIR` | `/data/publicapi/blobs` | content-addressed image blobs of running specs |
| `PUBLIC_API_ASR_CACHE_DIR` | under `/data` | audio ingest, decoded PCM and the window cache |
| `PUBLIC_API_TRUSTED_PROXIES` | blank | peers whose `X-Forwarded-For` is honoured |
| `PUBLIC_API_GATEWAY_PEERS` | blank | the v1-gateway's exact address(es), the only peers whose `X-TechSara-*` headers are honoured (§19); a network entry is ignored |
| `PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S` | 110 | the gateway's connect retry before a first byte (§19) |
| `PUBLIC_API_GATEWAY_REATTACH_MAX_S` | 1,800 | the gateway's re-attach budget after one (§19) |
| `PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT` | 6 | `main.normal` |
| `PUBLIC_API_CHAT_NORMAL_RESERVE` | 4 | NORMAL slots public work never takes |
| `PUBLIC_API_SHARED_KV_BUDGET_TOKENS` | 1,400,000 | chat LONG footprint + the patient KV ledger |
| `PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS` | 800,000 | `main.long` threshold (planned output) |
| `PUBLIC_API_MAIN_LONG_MAX_CONCURRENT` | 1 | `main.long` |
| `PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS` | 8,192 | `main.extended` threshold (planned output) |
| `PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT` | 2 | `main.extended` when admission does not take the answer |
| `PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S` | 10 | yield bound |
| `PUBLIC_API_ROUTER_CONTEXT_TOKENS` | 24,576 | `techsara-8b-vision` window |
| `PUBLIC_API_ROUTER_MAX_CONCURRENT` | 4 | `router` gate |
| `PUBLIC_API_ROUTER_KV_BUDGET_TOKENS` | 24,576 | `router` gate |
| `PUBLIC_API_OCR_CONTEXT_TOKENS` | 8,192 | `techsara-ocr` window |
| `PUBLIC_API_OCR_MAX_CONCURRENT` | 2 | `ocr` gate |
| `PUBLIC_API_EMBED_CONTEXT_TOKENS` | 4,096 | per input |
| `PUBLIC_API_EMBED_MAX_CONCURRENT` | 2 | `embed` gate |
| `PUBLIC_API_EMBED_KV_BUDGET_TOKENS` | 8,192 | `embed` gate |
| `PUBLIC_API_EMBED_MAX_INPUTS` | 2,048 | inputs per request |
| `PUBLIC_API_RERANK_CONTEXT_TOKENS` | 4,096 | per pair |
| `PUBLIC_API_RERANK_MAX_CONCURRENT` | 2 | `rerank` gate |
| `PUBLIC_API_RERANK_KV_BUDGET_TOKENS` | 8,192 | `rerank` gate |
| `PUBLIC_API_RERANK_MAX_DOCUMENTS` | 1,000 | documents per request |
| `PUBLIC_API_ASR_MAX_CONCURRENT` | 1 | `asr` gate |

**Retired** (one start-up warning each when set): `PUBLIC_API_GEN_WALL_CLOCK_S`,
`PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S`, `PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S`,
`PUBLIC_API_GATE_WAIT_S`, `PUBLIC_API_BACKGROUND_GATE_WAIT_S`,
`PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS`, `PUBLIC_API_MAX_AUDIO_SECONDS`.
Every one that code still reads is named, with its reader, in
`config.STILL_READ_RETIRED_SETTINGS`, and the warning says so.

**Transport timeouts.** Every `/v1` generation is a streamed engine call, even
`stream: false`. The public path to the main engine uses `read_timeout_s=None`
with TCP keepalive (`SO_KEEPALIVE`, `TCP_KEEPIDLE` 60, `TCP_KEEPINTVL` 15,
`TCP_KEEPCNT` 4) through `httpx.AsyncHTTPTransport(socket_options=…)`, so a
half-open peer raises in about 2 minutes; connect 10 s and write 60 s are kept,
and the client cache key includes the transport kind. The chat application keeps
`llm_request_timeout` (= `GEN_WALL_CLOCK_S`) unchanged; a call given its own
positive wall clock longer than that is sent with a per-request read timeout
equal to the clock (`llm._transport_timeout_for`). Sidecars use the silence
settings above. `LLM_MAX_RETRIES` stays 0. The database keeps `statement_timeout`
15 s; no pool connection is held across engine I/O.

## 13. Idempotency

`Idempotency-Key` on `POST /v1/responses` and `POST /v1/chat/completions` **only**,
scoped by `(project_id, endpoint, key)`, retained 24 h. On `/v1/embeddings`,
`/v1/rerank` and `/v1/audio/transcriptions` the header is refused with
`400 invalid_request_error`, `param: Idempotency-Key`: those calls keep no
durable row a key could name, and a silently ignored key would promise a safety
the server does not provide.

* same key + same body fingerprint + the same `key_id` or a key of the same
  service account → **attach**: a running request is joined (sync waits with
  whitespace keepalive and returns the same body; a stream replays from the start
  and tails; background returns its `202`), a finished one is replayed; the model
  is not invoked twice;
* same key + different fingerprint → `409 idempotency_conflict`,
  `x-should-retry: false`;
* same key + same fingerprint from a **different credential** → `409
  idempotency_conflict`, `x-should-retry: false` — replay and attach are
  creator-only (§10.3);
* a claim bound to a response is live while the run is open; there is no
  in-flight lease. A durable generation binds its claim to its response id at
  launch, so a retry attaches while the run is still generating; a run that
  ends `failed` releases the key (a retry runs again), whichever process
  settled it (the `router.v1` recorder, §14). A claim written before its row
  exists (the process died between the two) may be taken over after 300 s;
  a non-durable (`store: false`) run still answers `409` with `Retry-After`
  while it runs;
* the claim is `INSERT … ON CONFLICT DO NOTHING RETURNING id` — zero rows means
  someone else claimed it, which is the race-free primitive.

**Implicit attach** (openai-python, openai-node 6.49.0 and 7.15.0 all send
`x-stainless-retry-count`, measured): a `POST` with `x-stainless-retry-count ≥ 1`,
no `Idempotency-Key`, and the same `key_id`, route and `body_sha256` (over the raw
body bytes) as a run that is **orphaned or suspended** and was created within
`PUBLIC_API_IMPLICIT_ATTACH_WINDOW_S` (3,600 s) attaches instead of launching.
A retry count of 0 or a different body launches fresh.

**Attach order** in `router.py` (`_generate`): the gateway's attempt id (§19),
then `Idempotency-Key`, then implicit attach — all before a slot, a row or the
engine. An attach answers in the shape of the request that attached: a stream
replays from the gateway's `X-TechSara-Resume-After` (or from the start) and
tails, a synchronous call waits with whitespace keepalive and returns the body,
a background request gets its job's `202`. The model is never invoked twice.

## 14. Background responses and webhooks

`background: true` returns `202` with a response id after the row is durable and
before any expensive work. Status is read with `GET /v1/responses/{id}`; the work
survives client disconnect; cancellation is idempotent.

**Stream and background together** (2026-09-13, built 2026-09-14): allowed. The
job is queued as a durable row exactly as with `stream: false`; the request's
connection follows its log (`: ping` at once, `response.created` when the
dispatcher claims the job), leaving it cancels nothing, and a broken stream
resumes by §10.3. `background: true` with `store: false` is `400`,
`param: store`. A process whose durable runtime is not running answers a
`stream` + `background` request `503 model_unavailable` with `Retry-After`.

**Every generation is durable** — sync, stream and background, keyed or not, on
both dialects and on every chat-kind model — unless the request says
`store: false` (`publicapi/durable.py`, `durable_store.py`, V36; the router's
section "durable foreground runs"). `store: false` keeps the non-durable path:
no spec or events are stored, the run is cancelled on disconnect and on a
restart (recorded `cancelled`, charged the tokens counted at the stop,
`usage_source: counted_at_stop`), the gateway is told `X-TechSara-Run: none`,
and its row is closed `failed` (`model_unavailable`, "The service restarted
while this response was running.") by the next read or the durable sweep if a
restart cut it before its recorder wrote.

1. **Launch** writes the `api_responses` row (resumable, dialect, `key_id`,
   `attempt_token`, `body_sha256`, `enqueued_at`), the idempotency claim's
   response id, and the spec (messages, plan, tokens, temperature, gate) to
   `api_response_requests`; images become content-addressed blobs under
   `PUBLIC_API_BLOB_DIR` (0600 files, 0700 directory). A launch under the disk
   guard is `503`, `Retry-After: 60`.
2. **Queue.** Background runs are rows only: one dispatcher per process claims
   the oldest queued row with a compare-and-swap lease when its gate or lane has
   room. A queued background job holds no coroutine, memory, fd or lane waiter,
   and waits with no limit. Connection-held runs wait as coroutines (their
   bodies are held in memory while they wait; spilling large bodies to disk is
   not built).
3. **Run** under a lease (TTL 60 s, heartbeat 15 s). Each heartbeat renews all
   local leases in one statement and re-checks authorisation for all local runs
   in one batched query (the resolver's predicate over the stored key, service
   account and project rows, with the run's model); a revoked key closes the
   engine stream and settles `failed` with the resolver's single message.
4. **Events** are written ahead: deltas coalesce in a buffer flushed every
   100 ms, each job in its own savepoint with `FOR SHARE` on its row and
   `ON CONFLICT (response_id, sequence_number) DO NOTHING`; followers are woken
   only after commit. Unwritten events above `PUBLIC_API_PENDING_EVENTS_MAX_BYTES`
   suspend the largest run with reason `store`.
5. **Suspend on SIGTERM**: the chained handler calls `durable.suspend_all("restart")`
   — close engine streams, flush, write missing specs, release leases, abort every
   `/v1` reader with an incomplete read. Chat keeps its 90 s grace.
6. **Claim** takes `FOR UPDATE` on the row and resumes from `max(sequence_number)`
   and the logged text; authorisation is re-checked before dispatch.
7. **Resume.** Background runs resume after start-up one per
   `PUBLIC_API_RESUME_STAGGER_S`, oldest first, after chat's continuity sweep.
   Other runs stay suspended until a follower, the gateway, a same-key or an
   implicit attach arrives, and are cancelled with no engine work
   `PUBLIC_API_SUSPENDED_UNREAD_TTL_S` (900 s) after suspending unread.
8. **Continuation**: `spec.messages + [{role: "assistant", content: emitted}]`,
   `continue_final_message=True`, `add_generation_prompt=False`, `max_tokens =
   planned − generated`; the response text can only be extended. Router runs
   resume the same way; OCR runs that emitted nothing re-queue with their original
   `enqueued_at`.
9. **Orphan grace**: a client that leaves DETACHES; the run keeps generating
   with no reader for `PUBLIC_API_STREAM_ORPHAN_GRACE_S` (600 s) when it has an
   `Idempotency-Key` or is a Responses stream, `PUBLIC_API_UNKEYED_ORPHAN_GRACE_S`
   (120 s) otherwise (unkeyed sync and chat streams), so a re-attach or SDK retry
   finds it; then it settles `cancelled`. A run no reader picked up within 1 s of
   launch counts as orphaned from then. Background runs have none.
10. **Terminal**: settled once (usage summed over attempts, input counted once;
    `meta.attempts[]`, `resume_count`, `recomputed_prompt_tokens`, `yields`,
    `suspended_ms`), the webhook fired once, the spec and blob references deleted.
    Events are kept `PUBLIC_API_EVENT_RETENTION_S`. The request's own recorder
    runs when the launching process settles the run; when another process does,
    the serialisable `router.v1` recorder stored with the spec (`durable.RecorderRef`)
    writes the same usage row, settles the quota reservation and finishes the
    `Idempotency-Key` claim (released on `failed`). A foreground run's spec is
    written at launch when it is keyed or gateway-tagged, otherwise at the
    suspend; one whose spec was never written (a crash, not a SIGTERM) settles
    `failed` retry-safe when claimed.

**Quarantine.** An attempt ended by an engine incident it was dispatched into is
*implicated*. After one, the run is dispatched only alone, after
`PUBLIC_API_QUARANTINE_SERVING_S` (300 s) of proven serving, with at least
`PUBLIC_API_QUARANTINE_MIN_RECOVERIES` (2) recoveries left in the controller's
budget. A second implicated attempt fails it `model_unavailable` with
`x-should-retry: false`, partial output kept, never auto-resumed. Yields,
restarts and waits before dispatch never count.

**Restarts** therefore cost a re-prefill of the input plus the text so far
(prefix caching is off: at ~1,190 tok/s, 100,000 tokens ≈ 85 s), not the job.
`background.repair_if_orphaned` skips resumable rows.

V35 persists `api_responses.max_output_tokens` (the planned value at creation,
the applied value at the end) and `finish_reason` (`stop` | `length`).

A webhook endpoint belongs to a project: HTTPS only, subscribed events
(`response.completed`, `response.failed`, `response.cancelled`, each fired once at
the true terminal state, never at a suspend), a signing
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
| `v1_responses`, `v1_chat_completions` | `resp_<24 hex>` | from the engine's stream usage, or counted by the server when the stream closed early (§8.3); summed over attempts, input once; `None` = not measured, never 0 | `max_output_tokens_requested`, `max_output_tokens_applied`, `clamped`, `usage_source` (`engine` \| `counted_at_stop` \| `null` when not measured), `attempts`, `resume_count`, `recomputed_prompt_tokens`, `yields`, `suspended_ms` |
| `v1_embeddings` | `emb_<24 hex>` | input = engine `prompt_tokens` summed (None if any call did not report); output 0 (pooling generates nothing — a measured truth) | `inputs`, `engine_calls`; `resends` by reason (`progressing`, `unknown`, `lost`, `restarted`, `engine_error`) when any call was re-sent |
| `v1_rerank` | `rrk_<24 hex>` | input = `/score` `prompt_tokens`; output 0 | `documents`, `top_n`, `engine_calls`; `resends` as `v1_embeddings` |
| `v1_audio_transcriptions` | `asr_<24 hex>` | both `None` in `usage_events`; 0 and 0 in the token ledgers so they stay token-true | `audio_seconds` (ceil of decoded samples / 16,000), `processing_ms`, `response_format`, `language_forced`, `windows`, `engine_calls`; `joined` when the request followed a job another request started or a finished result |

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

Request logs keep metadata only: request id, time, project, model, status,
tokens, duration, stream/background flags, error code, key prefix.

**What the durable path stores** (owner decision requested with the no-timeout
design, 2026-09-13): while a main-model generation runs, its request spec
(`api_response_requests`), with images as content-addressed blob files under
`PUBLIC_API_BLOB_DIR`; and its output events, until `PUBLIC_API_EVENT_RETENTION_S`
(3,600 s) after it ends. The spec and blob references are deleted at the terminal
event; unreferenced blobs are reaped; retention purges cover events, specs and
blobs, in batches, before any cascade. Replay is creator-only (§10.3).
`store: false` stores none of it. Audio is kept on disk while it is transcribed,
and finished windows for 24 h. Background output text keeps its existing
retention.

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
* It does not keep a client connection through everything. **What still ends a
  request**: the engine proven down for `PUBLIC_API_ENGINE_DOWN_GRACE_S` (30 min)
  continuously; `PUBLIC_API_RESUME_MAX_STALLED_ATTEMPTS` (3) stalled attempts on a
  proven-serving engine; a second engine incident implicating the same run; a
  revoked key; the orchestrator unreachable from the gateway for 30 min; a client
  absent beyond the orphan grace; `store: false` during a restart. A tunnel drop
  or a gateway recreate cuts a client without a resume loop or SDK retries, and
  openai-node ≥ 7.5 gives up on a sync call after 3 × its own timeout.
* It does not change the chat application's behaviour for people who never touch
  the developer platform.

## 19. Internal attach protocol (v1-gateway only)

Never on the public wire. The v1-gateway (`gateway/`, Node 20, `node:`
built-ins only, image pinned by a digest of its inputs so a routine deploy never
recreates it) sits between cloudflared (path rule `^/v1(/|$)`) and the
orchestrator. It is a relay like the Next edge, not an auth boundary: the
orchestrator still resolves every key.

* **Request headers** from the gateway: `X-TechSara-Attempt` (a uuid4 on every
  request), `X-TechSara-Resume-After` (the last `: ts-seq=N` it relayed), and
  `X-TechSara-Attach-Job` (an audio job key, with an empty body). **Response
  header**: `X-TechSara-Run: <response id> | job:<key> | none`. **SSE comment**:
  `: ts-seq=N` after each data frame.
* **Trust**: honoured only when the socket peer is one of `PUBLIC_API_GATEWAY_PEERS`
  (exact addresses; not `PUBLIC_API_TRUSTED_PROXIES`, which only decides whose
  `X-Forwarded-For` is believed).
  The gateway and the Next edge drop any client-sent `x-techsara-*` header, and
  neither relays one back.
* **Attach** requires `attempt_token`, `key_id` and `body_sha256` (or the job
  key's project) to match. Then streams replay from `N + 1` and tail, sync waits
  with whitespace and returns the body, embeddings and rerank recompute. A run
  that never existed launches fresh (no event is ever sent before its row exists);
  one that cannot be replayed (`store: false`, events expired) answers `404`, and
  the gateway aborts the client.
* **Gateway behaviour**: before the first client byte it retries connect-phase
  failures with the same attempt for ≤ `PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S`
  (110 s), then answers `503 model_unavailable`, `Retry-After: 30`. After the first
  byte it keeps the client alive (`: ping`, or a space while only whitespace has
  been relayed) and re-attaches with 1 → 10 s backoff for ≤
  `PUBLIC_API_GATEWAY_REATTACH_MAX_S` (1,800 s) of continuous absence; 300 s of
  upstream silence counts as a failure. It aborts the client after a partly relayed
  JSON object, a `404`, or an orchestrator never seen to send `X-TechSara-Run`.
  On SIGTERM it closes the listener and destroys relays at +2 s.
* **The Next edge** (`frontend/app/v1/[[...path]]/route.ts`) relays to
  `V1_GATEWAY_URL` when set, streaming bodies, and falls back to the orchestrator
  only when the gateway refused the connection. Without the gateway it retries a
  refused connect every 2 s for ≤ 110 s (buffered bodies), answers `503`,
  `Retry-After: 30` after that, and `503` with `x-should-retry: false` after a
  post-send failure of an unkeyed generation. Its upstream fetch has no timer on
  the gateway hop and a 300 s silence limit on the direct path. The two edges' header allowlists are held equal by
  `gateway/test/parity.test.cjs`.
