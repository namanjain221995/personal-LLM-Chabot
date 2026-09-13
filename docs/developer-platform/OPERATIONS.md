# Running the developer platform

The operator's manual for `/v1`, its API keys, quotas, webhooks and the
developer console. It says what each setting does, how to give a team a key,
what to do when a key leaks, where to look when something is wrong, and how to
back the release out.

Written 2026-09-13 from the code on `feat/developer-platform-security`. Every
setting, table and log line named here was read in the tree. Nothing here has
been run against production. Commands marked **not executed** were written
from the code and have not been run by the author; run them on the e2e stack
first.

Companion documents: [`API.md`](API.md) (the `/v1` surface, endpoint by
endpoint), [`DISPOSITION.md`](DISPOSITION.md) (every audit finding and what
happened to it), [`CONTRACT.md`](CONTRACT.md), [`SCHEMA-V34.md`](SCHEMA-V34.md).

---

## 0. Read this first: what works in this tree, and what does not yet

Checked against the working tree on 2026-09-13, first at about 02:00 IST and
again at 02:50 IST after a second wave of changes landed. Files were still
being edited at the second check. Re-check each line before a release.

| piece | state | how it was checked |
|---|---|---|
| `/v1` router | **mounted**: the error envelope on 401 (cookie ignored), 404/405 for unknown paths and methods, the CORS preflight answering 204 | imported `app.main` and probed it with `TestClient` (no database, no engine) |
| developer console API (`/admin/api/developers/*`) | **mounted** (`main.CONSOLE_API_MOUNTED`); an unauthenticated call answers 401 | the same probe |
| console BFF → orchestrator | forwards to `/admin/api/developers`, matching the router's prefix | read both constants |
| FastAPI `/docs`, `/redoc`, `/openapi.json` | **off** unless `ORCHESTRATOR_DEV_DOCS` is on | the probe: `GET /docs` → 404 |
| public OpenAPI document | valid 3.1, eight operations, matches `public-api-surface.txt` | ran `.github/workflows/scripts/api_contract.py` |
| webhook signing secret | returned **once**, in the create response; never again. There is **no route that rotates** a webhook secret | read `console_api.create_webhook` and its routes |
| retention sweep `db.prune_api_platform` | **scheduled** by the orchestrator's lifespan: first run 60 s after start, then every `ARTIFACT_MAINTENANCE_INTERVAL_S` (1800 s) | read `app/main.py` `_api_platform_prune_loop` |
| background responses orphaned by a restart | repaired **lazily**: `GET /v1/responses/{id}` closes such a row as `failed` (`model_unavailable`) when it is read. No start-up sweep; `background.reconcile_interrupted` still has no caller | read `router.get_response`, `background.repair_if_orphaned` |
| end-to-end run of `scripts/devapi_smoke.py` | **not run by the author** | — |
| orchestrator and frontend test suites | **not run by the author** | — |

Do not insert `api_keys` rows by hand: `key_hash` is an HMAC under a pepper the
database may not hold, and a hand-made row skips the audit log.

---

## 1. The pieces

| piece | where | what it owns |
|---|---|---|
| public edge | `frontend/app/v1/[[...path]]/route.ts` | carries `/v1/*` from `ai.techsarasolutions.com` to the orchestrator; forwards `Authorization`, never `Cookie`; streams unbuffered |
| `/v1` router | `orchestrator/app/publicapi/` | the eight endpoints, the error envelope, streaming, background responses, the model registry, the public OpenAPI |
| key and quota engine | `orchestrator/app/apiplatform/` | key minting and hashing, the resolver, scopes, quotas and rate-limit headers, idempotency, the console API |
| webhooks | `orchestrator/app/apiplatform/webhooks/` | payloads, signatures, SSRF-checked delivery, the in-process delivery loop |
| schema | V34 in `orchestrator/app/db.py` | eleven new tables (see `SCHEMA-V34.md`) |
| console | `frontend/app/api/page.tsx`, `frontend/components/devplatform/` | the `/api` page; needs `api.console.access` |
| documentation | `frontend/app/docs/`, `frontend/content/docs/` | the `/docs` pages; any signed-in person may read them |

The API is served by the same orchestrator process as the chat application. It
shares the chat application's ten NORMAL admission lanes and the main model;
the per-project quota gate sits in front of admission so that one key cannot
take all of them.

---

## 2. Settings

### Orchestrator

These reach the orchestrator container through `env_file` in `compose.yaml`:
`.env`, then `.runtime/secrets.env`, then `.runtime/generated.env`. None of the
platform settings are in `.env.example` yet.

| variable | default | what it does |
|---|---|---|
| `API_KEY_PEPPER` | blank | The HMAC key every API key secret is hashed under. Set it to at least 32 characters of random text, in `.runtime/secrets.env`. Shorter than 32 is **refused**: every well-formed key then gets `500 internal_error` (the resolver treats "cannot verify any key" as a server fault, not a bad credential) and key creation answers 503. When blank, the platform generates a pepper on first use and stores it in `platform_secrets` — which works, but puts the pepper in the same database as the digests. **Never change it once keys exist**: every existing key stops matching at once. |
| `PUBLIC_API_MAX_BODY_BYTES` | 1048576 | The `/v1` body cap, applied before parsing. Set the frontend's variable of the same name to the same value (below). |
| `PUBLIC_API_IDEMPOTENCY_TTL_HOURS` | 24 | How long an `Idempotency-Key` is remembered. |
| `PUBLIC_API_DEFAULT_RPM` | 60 | Fallback requests-per-minute when a project row does not answer. New projects take the column default (also 60). |
| `PUBLIC_API_DEFAULT_INPUT_TPM` | 200000 | Fallback input tokens per minute. |
| `PUBLIC_API_DEFAULT_OUTPUT_TPM` | 60000 | Fallback output tokens per minute. |
| `PUBLIC_API_DEFAULT_MAX_CONCURRENCY` | 4 | Fallback concurrent requests. |
| `PUBLIC_API_DEFAULT_DAILY_TOKEN_QUOTA` | 2000000 | Fallback tokens per UTC day. |
| `PUBLIC_API_RATELIMIT_JITTER_SECONDS` | 3.0 | Random seconds added to `Retry-After` so throttled clients do not all return in the same second. |
| `PUBLIC_API_TRUSTED_PROXIES` | blank | Comma-separated addresses or CIDRs whose `X-Forwarded-For` `/v1` believes when it checks a project's `ip_allowlist` and records `last_used_ip`. **Blank trusts nobody**: the address is the socket peer, which for traffic through the public edge is the frontend container, so an allowlisted project reached through `ai.techsarasolutions.com` is refused. Set it to the frontend's Docker network (`sf-local-ai_application`, `172.18.0.0/16` on the head on 2026-09-13 — confirm with `docker network inspect sf-local-ai_application`). |
| `PUBLIC_API_KEY_ROTATION_OVERLAP_HOURS` | 168 | How long a rotated-out key keeps working beside its replacement when the rotation request does not say. At most 30 days. |
| `PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS` | twice the generation wall clock, at least 3600 | How long an unfinished `Idempotency-Key` claim is honoured before a retry may take it over (a process that died mid-request). |
| `ARTIFACT_MAINTENANCE_INTERVAL_S` | 1800 | Existing setting; also the cadence of the platform retention sweep (§7). |
| `ORCHESTRATOR_DEV_DOCS` | off | `1`/`true`/`yes`/`on` mounts FastAPI's `/docs`, `/redoc` and `/openapi.json`, which describe **every** internal route. Development boxes only. |
| `MAX_REQUEST_BODY_BYTES` | 134217728 (128 MiB) | Body cap for every non-`/v1`, non-upload route. A non-integer value is ignored with a warning. |
| `UPLOAD_MAX_MB`, `VIDEO_MAX_UPLOAD_MB` | existing | The larger of the two, plus 1 MiB, is the body cap on `/uploads*`. |
| `AUTH_TRUST_PROXY_HEADERS` | false (true in production) | Existing setting for the **browser** side (sessions, audit, login lockout). `/v1` does not read it; it uses `PUBLIC_API_TRUSTED_PROXIES`. See §8. |
| `SSE_HEARTBEAT_SECONDS` | existing | `/v1` streams use this interval, capped at 15 seconds. |

There is **no switch that turns `/v1` off**, and none that stops the webhook
delivery loop: `webhooks/worker.enabled()` reads a `webhook_worker_enabled`
setting that `app/config.py` does not define, so the loop always starts. None
of the settings above is in `.env.example` yet.

### Frontend

The frontend container receives **only** `.runtime/generated.env` plus the
three variables named in its `environment:` block in `compose.yaml`
(`ORCHESTRATOR_URL`, `MOCK_MODE`, `NEXT_PUBLIC_APP_NAME`). A variable placed in
`.env` does not reach it. The three below must be added to that block (or to
the generator) to take effect, and none of them is there today.

| variable | default | what it does |
|---|---|---|
| `TRUSTED_CLIENT_IP_HEADER` | blank | The one inbound header the proxies believe for the client address. **Set it to `cf-connecting-ip` in production.** Blank means the frontend sends the orchestrator no `X-Forwarded-For` at all — the orchestrator then records the frontend container's address for every sign-in, audit event, session and per-address login lockout. Before this release the proxy copied `cf-connecting-ip` from the request unconditionally, which let any caller who reached port 3000 directly forge it (audit F002). |
| `TRUSTED_FORWARDED_PROTO` | blank | `https` or `http`: the scheme the public edge terminated, sent as `X-Forwarded-Proto`. Anything else is ignored. |
| `PUBLIC_API_MAX_BODY_BYTES` | 1048576 | The `/v1` edge's body cap. Keep it equal to the orchestrator's. |

### Browser capabilities (`authn/rbac.py`)

| capability | super admin | admin | member |
|---|---|---|---|
| `api.console.access` | yes | yes | no |
| `api.projects.read`, `api.projects.manage` | yes | yes | no |
| `api.keys.create`, `api.keys.revoke` | yes | yes | no |
| `api.usage.read`, `api.logs.read` | yes | yes | no |
| `api.webhooks.manage` | yes | yes | no |
| `api.models.manage` | yes | no | no |
| `api.limits.manage` | yes | no | no |

A missing capability answers 404, not 403.

---

## 3. Before the first key

1. **Pepper.** Generate one and add it to `.runtime/secrets.env`
   (`API_KEY_PEPPER=<at least 32 random characters>`). Keep a copy in the same
   place the other production secrets are kept. Do it before the first key is
   minted — changing it later invalidates every key.
   Example generator, which prints the value to your terminal only:
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. **Client address.** Add `TRUSTED_CLIENT_IP_HEADER: cf-connecting-ip` and
   `TRUSTED_FORWARDED_PROTO: https` to the frontend service's `environment:`,
   and set `PUBLIC_API_TRUSTED_PROXIES` for the orchestrator to the frontend's
   Docker network (§2), or project IP allowlists judge every public request by
   the frontend container's address.
3. **Check the mounts.** After the orchestrator starts, its log must **not**
   contain `the public developer API (/v1) is NOT mounted`,
   `the developer console API (/admin/api/developers) is NOT mounted` or
   `the API key pepper store could not be wired`. Then, from the box:
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/v1/models`
   must print `401`, and
   `curl -s http://127.0.0.1:8080/v1/openapi.json | head -c 80` must start with
   `{"openapi":"3.1.0"`. (**Not executed** against production.)
4. **Close the side doors** listed in §8. A quota in front of the orchestrator
   means nothing to anyone who can reach the model's own port.

---

## 4. Provisioning a project and a key

A project is the unit of limits, allowed models, allowed origins, IP allowlist
and retention. Keys belong to a project. Service accounts are optional
groupings of keys inside a project.

In the console at `/api` (or the console API directly, `/admin/api/developers`,
with a session cookie):

1. **Create the project** — `POST /projects`
   `{"name": "…", "environment": "live" | "test", "allowed_models": [], "allowed_origins": [], "retention_days": 30, "metadata": {}}`.
   Needs `api.projects.manage`. `environment` defaults to `live`. The name must
   be unique in the workspace (case-insensitive). Audit event
   `api_project_created`.
   * `allowed_models: []` means every public model; list ids to restrict.
   * `allowed_origins: []` means any browser origin may use the key; list
     origins (exact strings, e.g. `https://app.example.com`) to restrict.
   * `ip_allowlist` can only be set afterwards, with
     `PATCH /projects/{id}` (`api.projects.manage`). Entries are addresses or
     CIDR blocks; a malformed entry is ignored with a warning. Read §8 before
     relying on it.
   * `retention_days` (1–365, default 30) sets `expires_at` on every response
     row created **from then on**.
2. **Set limits if the defaults are wrong** — `PUT /projects/{id}/limits`
   `{"rpm", "input_tpm", "output_tpm", "max_concurrency", "daily_token_quota", "max_input_tokens", "max_output_tokens"}`.
   Needs `api.limits.manage`, which only a super admin has. Audit event
   `api_limits_changed`, with each changed field's before and after.
   `max_input_tokens` and `max_output_tokens` are stored but the `/v1` request
   path does not read them today (API.md §12, item 4).
3. **Create the key** — `POST /projects/{id}/keys`
   `{"name": "…", "scopes": ["models.read", "responses.write", "responses.read"], "expires_in_days": 90, "service_account_id": null}`.
   Needs `api.keys.create`. The project must be active.
   * scopes default to `models.read`, `responses.read`, `responses.write`;
     add `usage.read` only for a credential that reads usage;
   * `expires_in_days` (1–365) defaults to 90;
   * the environment comes from the project: a `test` project mints
     `tsk_test_…` keys.
   The response is `{"key": {…}, "secret": "tsk_live_…"}`. **The `secret` is the
   only copy that will ever exist.** Hand it to the team over the channel you use
   for other production secrets; nobody, including this server, can show it
   again. The audit event `api_key_created` records the key's `public_id`,
   `last_four`, environment and scopes — never the secret.
4. **Check it works**, from wherever the team will call it:

   ```bash
   curl -s https://ai.techsarasolutions.com/v1/models \
     -H "Authorization: Bearer $TECHSARA_API_KEY"
   ```

   (This box resolves the public hostname AAAA-only; use `curl --resolve` here,
   or call `http://127.0.0.1:8080/v1/models` locally.)

Model exposure is a separate, super-admin decision: `PUT /models/{model_id}`
`{"enabled": false}` (`api.models.manage`) removes a model from `/v1` for every
project **in that workspace** (the `public_models` table is keyed by workspace
and model; audit `api_model_disabled`). A database row can only disable a model
the code declares, never add one.

---

## 5. Quotas and limits — how they behave

What a caller experiences, and where the numbers live:

| limit | default | refusal | where counted |
|---|---|---|---|
| requests per minute | 60 per project; optionally a lower number per key | `429 rate_limit_error`, `Retry-After` to when the sliding window frees a slot | `api_usage_minute` (per project and key, one row per minute) |
| input tokens per minute | 200,000 | `429 rate_limit_error` | same; the prompt's estimate (three characters per token) is reserved at admission and the unspent part returned afterwards |
| output tokens per minute | 60,000 | `429 rate_limit_error` | same; checks tokens already spent, so it bites on the request **after** the one that crossed the line |
| tokens per day | 2,000,000 | `429 quota_exceeded`, `Retry-After` to the next **UTC** midnight | `api_usage_daily` (per project) |
| concurrent requests | 4 per project; optionally a lower number per key | `429 concurrency_limit_exceeded`, `Retry-After` about 1 s | process memory, shared by synchronous, streaming and background work |
| the shared admission lanes | chat application's | `429 concurrency_limit_exceeded`, `Retry-After: 5` | `app/admission.py` |
| input tokens per request | the model's (and the project's `max_input_tokens`, if set) | `400 context_length_exceeded` | estimated before admission |
| output tokens per request | 8192 by default, up to the model's (and the project's `max_output_tokens`, if set) | `400 invalid_request_error` | — |

Things that surprise people:

* **Every authenticated request counts**, reads included: `GET /v1/models`,
  `GET /v1/responses/{id}` and `GET /v1/usage` each spend one request of the
  per-minute allowance. So does a request that is then refused as malformed,
  too large or over the context limit — deliberately, so bad requests are not
  a free flood.
* **A quota refusal costs nothing.** It increments
  `api_usage_daily.rate_limited` and nothing else.
* **The decision is atomic per project**: `quotas.reserve` reads the window,
  decides and increments inside one transaction holding a PostgreSQL advisory
  lock on the project, so simultaneous requests cannot overshoot.
* **Tokens are written once, when the request ends**, from the engine's own
  counts. For a request whose usage the engine did not report, the input
  estimate reserved at admission stays counted and **no output tokens** are
  added — so if usage reporting stops (§11, "usage not measured"), the output
  limit and most of the daily quota stop advancing while request limits still
  hold.
* **Concurrency lives in process memory.** A restart resets it; two orchestrator
  processes (a blue/green overlap) each allow the full number. A limit of `0`
  refuses every request.
* **Idempotent replays count** as one request against the per-minute limit and
  spend no tokens.
* `RateLimit` and `RateLimit-Policy` advertise only requests and concurrency;
  token limits are enforced but not advertised.

Read-only queries for a project's state (`psql` against the application
database; **not executed**):

```sql
-- today and yesterday, per project
SELECT project_id, day, requests, input_tokens, output_tokens, errors, rate_limited
  FROM api_usage_daily WHERE day >= current_date - 1 ORDER BY day DESC, requests DESC;

-- the last five minutes, per key
SELECT project_id, key_id, bucket, requests, input_tokens, output_tokens
  FROM api_usage_minute WHERE bucket > now() - interval '5 minutes' ORDER BY bucket DESC;
```

---

## 6. Webhooks

A project may register up to ten HTTPS endpoints
(`POST /projects/{id}/webhooks`, `api.webhooks.manage`) subscribed to
`response.completed`, `response.failed` and `response.cancelled`. Only
**background** responses produce events. The create response carries the
endpoint's signing secret (`whsec_…`) **once**; no later call returns it. The
audit event records the URL with any credentials or query string redacted.

### What a delivery looks like

`POST` to the endpoint, JSON, with these headers
(`webhooks/signer.headers_for`):

```
Content-Type: application/json
User-Agent: TechSara-Webhooks/1.0
TechSara-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
TechSara-Event-Id: <event id>
TechSara-Event-Type: response.completed
TechSara-Delivery-Id: whd_<24 hex>
```

Body (`webhooks/sender.build_payload`, built from an allowlist):

```json
{"id": "<event id>", "object": "event", "type": "response.completed", "created_at": 1789200000,
 "data": {"response": {"id": "resp_…", "object": "response", "status": "completed",
   "model": "techsara-35b", "background": true, "created_at": …, "completed_at": …,
   "usage": {…} | null, "metadata": {…}}}}
```

A failed response adds `data.response.error: {code, message}`. The generated
text is included **only** when the endpoint has `include_output: true`; the
prompt never is.

### Verifying a signature

The consumer recomputes `HMAC-SHA256(secret, t + "." + raw_body)` over the raw
bytes (before any JSON parse), compares in constant time against every `v1=`
value in the header (during a secret rotation there are two), and rejects a
`t` more than 300 seconds from its own clock. `webhooks/signer.verify()` is the
reference implementation.

> **There is no rotation route for a webhook secret in this release.** The
> signer supports an overlap (`previous_secret`, both digests sent), but nothing
> in the console API sets it. A lost or leaked secret means deleting the
> endpoint and creating a new one.

### Retries

`webhooks/sender.py` and `webhooks/worker.py`:

* the loop wakes when a delivery is queued and otherwise sweeps
  `api_webhook_deliveries` for due rows, 20 at a time, at most 4 deliveries in
  flight;
* each attempt has a 10 s read timeout (3 s to connect), follows at most 3
  redirects (each re-checked), and reads at most 64 KiB of the reply;
* a 2xx is `delivered`;
* **any other status, a timeout or a connection error** is retried — 4xx
  included, because a consumer mid-deploy answers 404 for a minute;
* delays after attempts 1–5 are 10, 20, 40, 80 and 160 seconds, each ±25%
  (never under 1 s); the sixth failed attempt marks the delivery `failed`.
  About five minutes end to end;
* a URL that resolves to a loopback, private, link-local, unique-local,
  IPv4-mapped or metadata address, or carries credentials, is `dropped` on the
  first attempt and never retried;
* every attempt updates the endpoint's `last_delivery_at`,
  `last_delivery_status` and `consecutive_failures` (reset on success).
  Nothing disables an endpoint automatically.

The console API lists an endpoint's delivery history:
`GET /admin/api/developers/projects/{id}/webhooks/{endpoint_id}/deliveries?limit=&offset=`
(`api.webhooks.manage`), newest first. Or read the table:

```sql
SELECT id, event_type, response_id, status, attempt, max_attempts, http_status,
       left(error, 120) AS error, next_attempt_at, created_at, delivered_at
  FROM api_webhook_deliveries WHERE endpoint_id = '<whe_…>' ORDER BY created_at DESC LIMIT 50;
```

"Send test event" (`POST /projects/{id}/webhooks/{endpoint_id}/test`) queues a
`webhook.test` event with a unique id; it is not subscribable.

---

## 7. Retention and the pruning job

What is kept, and for how long, once the job runs:

| data | kept | removed by |
|---|---|---|
| `api_responses` rows | until `expires_at` = creation time + the project's `retention_days` at that moment (default 30) | `prune_api_platform` deletes rows past `expires_at` |
| `api_responses.output_text` (background only) | as above; if a project later **shortens** its retention, the text on older rows is cleared while the row stays | `prune_api_platform` |
| `api_idempotency` | `PUBLIC_API_IDEMPOTENCY_TTL_HOURS` (24) | `prune_api_platform` |
| `api_usage_minute` | 7 days | `prune_api_platform` |
| `api_usage_daily` | forever | nothing |
| `api_webhook_deliveries` in `delivered`, `failed` or `dropped` | 30 days | `prune_api_platform` |
| `usage_events` rows written for API traffic | forever | nothing (audit F040) |
| `audit_events` | forever | nothing |

Lengthening a project's retention does not extend rows already written.

**The job runs inside the orchestrator**: 60 seconds after start-up, then every
`ARTIFACT_MAINTENANCE_INTERVAL_S` (1800 s). A pass that removed anything logs
`api platform prune removed {…}`; a failed pass logs `api platform prune failed`
and is retried on the next tick. It deletes in bounded batches. To run one pass
by hand — for example to confirm it works after a release — **not executed:**

```bash
docker exec sf-local-ai-orchestrator-1 python -c \
  "from app import db; print(db.prune_api_platform())"
```

Run it on the e2e stack first (`techsara-e2e-orchestrator`).

---

## 8. Trust boundaries an operator has to hold

The platform's checks all live in the orchestrator. Three things outside it
decide whether they mean anything:

1. **The model's own port.** While the main engine's OpenAI API answers
   unauthenticated on the LAN (audit F050/F042/F012), anyone on that network
   can skip keys, scopes, quotas, usage records and audit entirely by calling
   port 8000. The bind stays `0.0.0.0` on purpose (owner option A,
   2026-09-13: the worker's healthcheck and the interview-analysis tenant dial
   the head over RoCE rail A, and a narrower bind would take the two-node
   engine down). The exposure is closed by the host packet filter,
   `scripts/host-guard.sh` — §13 — which the owner applies with no restart.
2. **The orchestrator's own port.** `:8080` is published on
   `TECHSARA_BIND_ADDRESS`. For `/v1`, a forwarded address is believed only from
   a peer in `PUBLIC_API_TRUSTED_PROXIES`, so a LAN host calling `:8080` directly
   cannot claim an allowlisted address — it is judged by its own. The browser
   side is not fixed: with `AUTH_TRUST_PROXY_HEADERS=true` any host that reaches
   `:8080` still chooses the address written to sessions, the audit log and the
   per-address login lockout (audit F027/F058/N026). Keep the port off the LAN
   (DISPOSITION.md OA-8). One consequence for `/v1`: if
   `PUBLIC_API_TRUSTED_PROXIES` does not cover the frontend's network, every
   request through the public hostname is judged by the frontend container's
   address.
3. **Plain HTTP at the edge.** Until "Always Use HTTPS" and HSTS are on for the
   public hostname (audit N019), a client that calls `http://…/v1` sends its key
   in clear over its own network.

---

## 9. When a key leaks

A key is `tsk_live_<public_id>_<secret…>`. The `public_id` — the 16 hex
characters after the second underscore — identifies the key and is safe to
paste into tickets and queries. Never paste the whole token.

**1. Revoke it.** Console → the project → the key → Revoke, or
`POST /admin/api/developers/projects/{project_id}/keys/{key_id}/revoke`
(`api.keys.revoke`). It takes effect on the next request everywhere: the
resolver reads the key row on every call and nothing caches key state. The
leaked token then gets the same `401 invalid_api_key` as a token that never
existed.

If the key cannot be found quickly, or several keys of one project are
affected, **disable the project** (`PATCH /projects/{id}` with
`{"status": "disabled"}`) — every key in it answers 401 from the next request.
Disabling the workspace does the same for every project in it.

**2. Replace it.** Two ways, and the difference matters:

* **Rotate** (`POST …/keys/{key_id}/rotate`, body `{"overlap_hours": 0}`,
  needs both `api.keys.create` and `api.keys.revoke`) mints a successor with the
  same name, scopes, allowed models, limits and expiry. With `overlap_hours: 0`
  the old key is refused from that moment — **use 0 for a leak**. Without it the
  old key keeps working for `PUBLIC_API_KEY_ROTATION_OVERLAP_HOURS` (168) so
  consumers can move; that is for routine rotation, never for a compromise.
  Rotations of one key are serialised; a second concurrent press gets 409.
* If you have already revoked the key in step 1, create a new key instead.

The new secret is shown once, as at creation.

**3. Find out what it was used for.** Read-only, **not executed**:

```sql
-- the key, when and where it was last used
SELECT id, project_id, name, status, created_at, expires_at, last_used_at, last_used_ip,
       revoked_at, revoked_by, rotated_from
  FROM api_keys WHERE public_id = '<public_id>';

-- what it asked for (metadata only; prompts are never stored)
SELECT created_at, id, request_id, model, status, background, streamed,
       input_tokens, output_tokens, error_code
  FROM api_responses WHERE key_id = '<key_…>' ORDER BY created_at DESC LIMIT 200;

-- its request volume by minute
SELECT bucket, requests, input_tokens, output_tokens
  FROM api_usage_minute WHERE key_id = '<key_…>' ORDER BY bucket DESC LIMIT 120;
```

`api_usage_minute` keeps seven days; `api_responses` keeps the project's
retention. `last_used_ip` is only as trustworthy as §8 point 2 allows.

**4. What the audit log shows.** Every key event goes through the existing
audit writer:

| action | when | `meta` |
|---|---|---|
| `api_key_created` | a key is minted | `project_id`, `public_id`, `last_four`, `environment`, `scopes` |
| `api_key_rotated` | a rotation | `project_id`, `replaced_key_id`, `replaced_public_id`, `public_id` (the successor), `overlap_seconds` |
| `api_key_revoked` | a revocation | `project_id`, `public_id`, `last_four` |
| `api_project_created` | a project is created | `name`, `environment` |
| `api_project_updated` | a project is edited (including disabling it) | `fields` — the names of the fields changed, not their values |
| `api_limits_changed` | limits | `changed`: each field's `from` and `to` |
| `api_webhook_created` | an endpoint is added | `project_id`, `url` (credentials and query redacted), `events` |
| `api_webhook_updated` | an endpoint is edited | `project_id`, `fields` |
| `api_webhook_deleted` | an endpoint is deleted | `project_id` |
| `api_webhook_test_sent` | "send test event" | `project_id`, `event_id` |
| `api_model_enabled`, `api_model_disabled` | model exposure (resource id = the model id) | `enabled` |

Each row also carries the actor, time, client address and user agent. The
Audit Log page (super admin) shows the action, actor and time but **not
`meta`** (audit F080, not fixed here), so read the key identifiers from the
API: `GET /admin/api/audit?action=api_key_revoked` (`audit.read`) returns
`meta` for each event.

**5. If the pepper leaked** as well as the database: the key secrets are 256
random bits, so the digests still cannot be reversed. Rotating the pepper
invalidates **every** key in the deployment at once; do it only with a
re-issue plan for every team.

**6. If a webhook signing secret leaked:** there is no rotation route in this
release. Delete the endpoint and create a new one; the new secret is shown once
in the create response.

---

## 10. Reading the request logs

The console's Logs tab (`GET /admin/api/developers/projects/{id}/logs?status=&limit=`,
`api.logs.read`, at most 200 rows) shows metadata only, newest first: response
id, `request_id`, model, status, background and streamed flags, input and output
tokens (null means not measured), time to first token, duration, error code,
the caller's own `metadata`, the key's name and last four characters, and the
created, started and completed times. No prompt and no generated text appear
there, and none is stored for synchronous or streamed requests.

**Following one request end to end.** A customer quotes the `X-Request-Id`
header or the `request_id` in an error body (`req_<32 hex>`). That id is:

* `api_responses.request_id` — the durable row;
* `usage_events.meta->>'request_id'` — the usage ledger row, beside
  `meta->>'api_key_id'` and `meta->>'project_id'`;
* in the orchestrator log line `unhandled error on the public API (request_id=req_…)`
  with the traceback, whenever a customer received `internal_error`.

```sql
SELECT r.created_at, r.id, r.status, r.error_code, r.error_message,
       u.ttft_ms, u.duration_ms, u.input_tokens, u.output_tokens
  FROM api_responses r
  LEFT JOIN usage_events u ON u.meta->>'request_id' = r.request_id
 WHERE r.request_id = 'req_…';
```

A request refused before its durable row was written — 401, 403, 404, 400,
413 and the rate and quota 429s — leaves no `api_responses` row. Refusals on
quota are counted in `api_usage_daily.rate_limited`; the rest are visible only
in the edge's own logs.

A `429 concurrency_limit_exceeded` also leaves no row: the slot is taken
before the row is written, and the request's `Idempotency-Key` claim is
released.

---

## 11. What to watch

There is no `/v1`-specific Prometheus metric or alert in this release. Watch
these instead.

**Orchestrator log lines** (`docker logs sf-local-ai-orchestrator-1`):

| line | level | means |
|---|---|---|
| `the public developer API (/v1) is NOT mounted: …` | ERROR | the router failed to import; every `/v1` call is answered by the chat app instead |
| `the API key pepper store could not be wired (…)` | ERROR | keys cannot be verified: well-formed keys get `500 internal_error`, key creation 503 |
| `the developer console API (/admin/api/developers) is NOT mounted: …` | ERROR | the console at `/api` has no backend |
| `api platform prune removed {…}` / `api platform prune failed` | INFO / WARNING | the retention sweep's work, or its failure |
| `api key <public_id> carries a scope this release does not know; refusing it` | ERROR | a key row holds a scope string outside the four; it will answer 401 |
| `api key <public_id>: workspace on the key row and on its project disagree` | ERROR | data corruption; the key is refused |
| `api key <public_id> refused: <ip> is outside the project ip_allowlist` | WARNING | a caller from an unlisted address |
| `api key <public_id> refused on requests|input tokens|output tokens|daily tokens (project …)` | INFO | a quota refusal |
| `unhandled error on the public API (request_id=…)` | ERROR | a customer got `internal_error` |
| `public_models overrides unreadable; serving the declared set` | WARNING | the database read failed; nothing is disabled |
| `webhook sweep: due=… delivered=… retrying=… failed=… dropped=…` | INFO | the delivery loop's work |
| `webhook sweep failed` | WARNING | the loop's own query failed |
| `background response <id> failed` | WARNING | a background job ended in failure |
| `idempotency claim for project … could neither be won nor read` | ERROR | database trouble on the idempotency table |

**Database checks** (read-only, **not executed**):

```sql
-- background responses left open (a restart orphan is closed only when someone reads it)
SELECT project_id, count(*) FROM api_responses
 WHERE status IN ('queued','in_progress') AND created_at < now() - interval '30 minutes'
 GROUP BY project_id;

-- throttling and errors today
SELECT project_id, requests, errors, rate_limited FROM api_usage_daily
 WHERE day = current_date ORDER BY rate_limited DESC;

-- webhook endpoints that keep failing, and the backlog
SELECT id, project_id, url, consecutive_failures, last_delivery_status, last_delivery_at
  FROM api_webhook_endpoints WHERE consecutive_failures >= 3;
SELECT status, count(*) FROM api_webhook_deliveries GROUP BY status;

-- keys expiring in the next week (default lifetime is 90 days)
SELECT project_id, public_id, name, expires_at FROM api_keys
 WHERE status = 'active' AND expires_at < now() + interval '7 days';

-- usage not measured (token quotas are not advancing for these)
SELECT date_trunc('hour', created_at) AS hour, count(*) FILTER (WHERE input_tokens IS NULL) AS unmeasured, count(*)
  FROM api_responses WHERE created_at > now() - interval '1 day' AND status = 'completed'
 GROUP BY 1 ORDER BY 1 DESC;

-- the retention sweep is keeping up: this should stay near zero
SELECT count(*) FROM api_responses WHERE expires_at < now();
```

**Existing dashboards.** API traffic runs through the same admission lanes and
engine as chat, so the availability dashboards and alerts
(`docs/availability/RUNBOOK.md`) cover it. API requests appear in the
analytics console's usage data under routes `v1_responses` and
`v1_chat_completions`, mode `api`, with no user. The console's playground runs
are recorded under route `api_playground`, at effort `medium` — not the `fast`
effort every `/v1` request runs at, so a playground answer is not a faithful
preview of an API answer.

---

## 12. Releasing and rolling back

### What the release changes

* **Schema V34**: eleven new tables and thirty indexes. No existing table is
  altered and no row is rewritten (checked: the V34 block contains
  `CREATE TABLE` and `CREATE [UNIQUE] INDEX` statements only). It runs inside
  the ordinary start-up migration, on empty tables.
* **Orchestrator**: the `/v1` router, the webhook loop, the pepper wiring, the
  body-size middleware on every route, FastAPI's schema pages switched off, the
  422 handler that no longer echoes request bodies, the `u<digits>-` id
  refusal, the Salesforce routes failing closed.
* **Frontend**: the `/v1` edge, the `/api` console and `/docs` pages, the
  bounded and header-honest proxies, the middleware matcher change.
* **Infrastructure files.** The main engine's bind is unchanged (option A:
  `0.0.0.0` on the head, the management address for the worker's OCR and
  speech engines), so nothing in this release needs a main-model restart. The
  engine-controller and node-exporter binds take effect when those containers
  are next recreated. The engine exposure is closed separately by the host
  packet filter (§13), which is not part of a deploy and restarts nothing.

A routine rolling deploy carries the application changes. `API_KEY_PEPPER` and
the frontend variables of §2 must be in place **before** it.

### Rolling back

`scripts/deploy-rollback.sh` restores the previous images. It does **not** undo
migrations, and it refuses to roll back to a release that does not know every
migration the database has applied:

```bash
scripts/deploy-rollback.sh --list
scripts/deploy-rollback.sh --to <release> --dry-run
scripts/deploy-rollback.sh --to <release> --yes --i-accept-schema-drift
```

For **this** release the override is safe: V34 only adds tables, and the
previous release's code references none of them. After a rollback:

* the V34 tables and everything in them stay — projects, key digests, usage,
  webhook history. Rolling forward again finds them intact and every key still
  works, provided `API_KEY_PEPPER` is unchanged (or the generated pepper row in
  `platform_secrets` is still there);
* `/v1` disappears. The previous frontend has no `/v1` route, and its page
  middleware sends an unauthenticated request for `/v1/…` to `/login`, so API
  clients see a redirect rather than JSON. Tell API customers before rolling
  back;
* background responses in flight are lost; their rows stay `queued` or
  `in_progress` (a later roll-forward closes each one as `failed` when it is
  read);
* undelivered webhooks stay `pending` in `api_webhook_deliveries` and are sent
  when a release with the delivery loop starts again, if they have not been
  pruned by then.

To remove the platform's data after a deliberate, permanent rollback, drop the
eleven tables named in `SCHEMA-V34.md` and delete row `34` from
`schema_migrations` — in a maintenance window, after a backup, and only once
nobody expects those keys to work again.

---

## 13. The host packet filter on the engine ports

The raw model engines have no authentication, so they must not be reachable
from the office LAN or the tailnet. On 2026-09-13 the repository owner chose to
keep their binds (option A) — the head vLLM API on `0.0.0.0:8000`, the worker's
OCR and speech engines on `192.168.9.68` — because the head's callers sit on
loopback, the Docker bridges and RoCE rail A at once, and the worker's
`vllm-worker` healthcheck (`http://10.100.184.1:8000/health`, `kill -9` of its
rank after 8 misses) would take the TP=2 engine down under any narrower head
bind. `scripts/host-guard.sh` closes the exposure by ingress interface instead.
The full reasoning, the rule tables and the consumer each rule serves are in
DISPOSITION.md OA-3 (withdrawn), OA-4 and OA-6.

**What applying it costs: nothing running.** It adds one nftables table,
`inet techsara_guard`. No container, engine or model restarts; established
connections are accepted first, so a generation in flight survives; only TCP to
the guarded ports is judged, so SSH, the Cloudflare tunnel, the torch master
port and the NCCL listeners are untouched; it never flushes the ruleset and
never touches Docker's chains.

| node | guarded tcp ports | accepted from | dropped |
|---|---|---|---|
| head | 8000-8005, 9100, 9835, 9838 | lo; docker0 and br-* from 172.16.0.0/12; enp1s0f1np1 from 10.100.184.0/24; enP2p1s0f1np1 from 10.100.185.0/24 | enP7s7, tailscale0, any other ingress (IPv4 and IPv6) |
| worker | 9100, 9835, 9839, 30004, 30007 | lo; both rails; enP7s7 from the head 192.168.9.54 (not 9839); local Docker bridges | enP7s7 from anyone else, tailscale0, any other ingress |

`apply` refuses, before calling nft, if the rules would drop a consumer in its
built-in consumer table, if an interface it names is missing, or if a peer
connected right now to a guarded port would lose its next connection.

### Head (OA-4)

```bash
sudo scripts/host-guard.sh plan       # the exact ruleset; changes nothing, needs no root
sudo scripts/host-guard.sh apply      # self-test + preflight, nft -c -f, one atomic nft -f
scripts/host-guard.sh verify          # table/state, loopback probes; from the worker: rail -> 200, LAN -> timeout
gh variable set ENGINE_EXPOSURE_ENFORCE --body true --repo namanjain221995/personal-LLM-Chabot   # only after verify passes
```

Run `verify` straight after `apply`. **If its rail probe fails, remove the
guard immediately** — the worker healthcheck kills its rank after 4 minutes of
misses:

```bash
sudo scripts/host-guard.sh remove     # rollback: deletes table inet techsara_guard, nothing else
```

Also check one real chat, every Prometheus target UP, and from an office
laptop `curl -m 5 http://192.168.9.54:8000/v1/models` timing out.

### Worker (OA-6)

```bash
W=techsphere@10.100.184.2
scp scripts/host-guard.sh "$W":.techsara-cluster/host-guard.sh
ssh -t "$W" 'sudo bash ~/.techsara-cluster/host-guard.sh plan --role worker'
ssh -t "$W" 'sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker'
ssh    "$W" 'bash ~/.techsara-cluster/host-guard.sh verify --role worker'
# rollback:
ssh -t "$W" 'sudo bash ~/.techsara-cluster/host-guard.sh remove'
```

Then from the head `scripts/ocr.sh verify` and `scripts/whisper.sh verify`
must still succeed, the worker's Prometheus targets stay UP, and from an
office laptop `curl -m 5 http://192.168.9.68:30004/v1/models` times out.

### Day to day

* `scripts/host-guard.sh explain 8000 enp1s0f1np1 10.100.184.2` — what the
  ruleset does to one new connection, without root.
* `sudo nft list table inet techsara_guard` — the rules with per-rule
  counters. The rail-A accept on the head climbs with the worker healthcheck;
  a climbing `enP7s7` drop counter is someone on the LAN trying the raw port.
* A new consumer of a guarded port (a service on another network, a new
  bridge with a custom name) must be added to the script's rules **and** its
  consumer table, then `apply` re-run; `apply` refuses while such a peer is
  connected and would be cut.
* The table does not survive a reboot. Never persist it through the stock
  `/etc/nftables.conf` (it starts with `flush ruleset`, which deletes Docker's
  rules); use the oneshot systemd unit in DISPOSITION.md OA-4.
* Not covered: Docker-published ports (`0.0.0.0:8080`, `:3000`, `:9000`)
  traverse Docker's FORWARD path, not the input hook (DISPOSITION.md OA-7,
  OA-8).
