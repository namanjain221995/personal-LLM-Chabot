# Disposition — what happened to every audit finding

`AUDIT.md` lists 111 verified findings. This document gives each one a
disposition: **FIXED** in this branch (with the file and the team),
**PARTLY FIXED** (what landed and what is left), **DEFERRED** (why, and who
should own it), or **OPERATOR ACTION** (a setting or command only the
repository owner can apply, written out in full in
[Operator actions](#operator-actions)).

## How each row was checked

Written 2026-09-13 against the **uncommitted working tree** of
`feat/developer-platform-security` in
`/home/techsphere/Documents/project/personal-LLM-Chabot-devapi`, based on
`4e28fcc`. Nothing in this branch is committed yet, and other teams were still
editing it while this was written — the diff grew from 16 to 35 modified files
during the check. Every row was checked once at about 02:00 IST and the rows
touching files that changed afterwards were re-checked at 02:50 IST; files under
`orchestrator/app/publicapi/` and `apiplatform/` were still being saved then. **Re-run the checks
below before merging**; a row that says "not fixed" may have been fixed since,
and a row that says "fixed" describes code, not a deployment.

A finding is marked FIXED only when the change is visible in the tree:

* `git diff HEAD -- <file>` and `git status` to see what changed, file by file;
* reading the changed code at the cited symbol, and the test that pins it (test
  names are quoted so they can be found with `grep -rn`);
* for the orchestrator's mount state, importing the application and probing it
  with FastAPI's `TestClient` — no database, no engine — which is how "the
  developer console API is not mounted" and "`/docs` answers 404" were
  established;
* for CI, running the two policy scripts locally (outputs at the end).

**Not done, and not claimed:** no test suite was run for this document (the
orchestrator suite needs a private PostgreSQL; the frontend suite was not
started), no end-to-end run, and nothing was checked against the running
production stack. "FIXED" therefore means "the change and its test are in the
tree", not "verified in production".

**FIXED in code is not the same as live.** Every infrastructure row
(F050, F042, F012, F065, F055, F047, N025, F056) changes a bind address that
only takes effect when the affected container is recreated; for the main
engine that is a main-model restart in a production window (OA-3), which a
routine deploy does not do.

## Summary

| severity | findings | FIXED | PARTLY FIXED | DEFERRED | OPERATOR ACTION |
|---|---|---|---|---|---|
| P0 | 2 | 0 | 1 | 0 | 1 |
| P1 | 20 | 8 | 6 | 4 | 2 |
| P2 | 66 | 25 | 14 | 16 | 11 |
| P3 | 20 | 6 | 5 | 7 | 2 |
| none | 3 | 1 | 2 | 0 | 0 |
| **total** | **111** | **40** | **28** | **27** | **16** |

### Still blocking a public release

The audit marked two findings as release blockers. Neither is closed:

* **F064** — a fork pull request can run code on the production DGX. Only the
  repository owner can close it (OA-1 today, OA-2 durably).
* **F050** — the main model's raw API answers anyone on the LAN. The code that
  set the wildcard bind is changed, but the running engine keeps its bind until
  it is recreated (OA-3); the host filter (OA-4) closes it without a restart.

And, from checking this table, three things the release needs from the
operator that are not in `AUDIT.md` (full list in
[Gaps found during verification](#gaps-found-during-verification)):

* `TRUSTED_CLIENT_IP_HEADER` must be set on the frontend service (OA-19), or
  every browser session and audit event records the frontend container's address;
* `PUBLIC_API_TRUSTED_PROXIES` must name the frontend's network (OA-22), or every
  `/v1` request through the public hostname is judged by that same address
  against project IP allowlists;
* `API_KEY_PEPPER` must be set before the first key and never changed
  (OPERATIONS.md §3).

Earlier in the same check the console API was unmounted, its BFF pointed at the
wrong prefix, the webhook secret was never revealed and the retention sweep had
no caller; all four were fixed by other teams while this document was being
written, and are recorded below as closed.

## The table

`verdict` is the audit's (`CONFIRMED`, `ADJUSTED`, or `FOUND IN VERIFICATION`).
`owner` is the team that did the work, as named in `OWNERSHIP.md`, or — for a
wave not listed there — the test file that pins it; for open work it is the
proposed owner.

| id | sev | verdict | area | finding | disposition | what happened / what is left | owner |
|---|---|---|---|---|---|---|---|
| F064 | P0 | CONFIRMED | cicd | A fork pull request can execute arbitrary code on the production DGX as a sudo+docker user | **OPERATOR ACTION** | Cannot be fixed in code: on a `pull_request` event GitHub runs the workflow file from the pull request's own head, so every guard in `pipeline.yml` and `workflow_policy.py` is editable by the attacker. `on: pull_request:` is unchanged. Immediate remedy OA-1, durable fix OA-2. | repository owner |
| F050 | P0 | CONFIRMED | edge-devops | The main model's raw OpenAI API is unauthenticated and reachable from the office LAN, the … | **PARTLY FIXED** | Code: `launcher/techsara_cli/cluster.py` no longer lets `PUBLISH_MODEL_PORTS` move the dual-mode head to `0.0.0.0` (bridge gateway, loopback fallback); `.env.example` and the published overlays now bind 8000 to `TECHSARA_MODEL_BIND_ADDRESS`; pinned by `launcher/tests/test_network_exposure.py`. **Not live**: the running engine keeps `--host 0.0.0.0` until it is recreated, which is a main-model restart (OA-3). Host packet filter OA-4 is the backstop. | infrastructure hardening wave; repository owner for OA-3/OA-4 |
| F065 | P1 | ADJUSTED | cicd | The raw, unauthenticated vLLM OpenAI API is bound to 0.0.0.0:8000 and the verify gate is blind … | **PARTLY FIXED** | Half (1) is the F050 launcher/compose change, live only after OA-3. Half (2), a bind-address assertion in the `verify` job, was not added (no `ss -l` check in `pipeline.yml`). | CI/CD (verify assertion); repository owner (OA-3) |
| F066 | P1 | CONFIRMED | cicd | workflow_policy P4 accepts an INVERTED branch guard, so a self-hosted job restricted to "every … | **DEFERRED** | Not fixed. P4 still tests `refs/heads/<default>` as a substring and skips the pull_request check when the condition contains `!=`, so an inverted guard passes. Low value until OA-1/OA-2, because the policy job itself runs from the pull request head (F064). | CI/CD |
| F067 | P1 | CONFIRMED | cicd | The whole gate architecture assumes branch protection that does not exist; `CI passed` is not a … | **OPERATOR ACTION** | Repository settings, not code: required reviewers and a `main`-only branch policy on the `production` environment, and a ruleset on `main` requiring `CI passed` and one review. OA-5. | repository owner |
| N021 | P1 | FOUND IN VERIFICATION | cicd | P4 is skipped entirely when `runs-on` is an expression: a matrix can smuggle `self-hosted` past … | **DEFERRED** | Not fixed. `_runs_on_text` still stringifies a `${{ }}` expression, so a matrix can smuggle `self-hosted` past P4. | CI/CD |
| F034 | P1 | ADJUSTED | database | Bare /chat calls write private document and page text under a conversation key any other user … | **FIXED** | `orchestrator/app/main.py`: `session_id` validated to `^[A-Za-z0-9_-]{1,64}$` and client `conversation_id`s shaped `u<digits>-` refused on `ChatRequest` and `StopRequest`; the Salesforce routes refuse the shape. `orchestrator/app/uploads.py`: the claim, chunked-claim and read routes refuse it. `orchestrator/app/history.py` `create_conversation` now refuses it too (400). Tests in `tests/test_orchestrator_hardening.py`. | Orchestrator wiring |
| N010 | P1 | FOUND IN VERIFICATION | database | GET /chat/salesforce/{id} and POST /chat/salesforce/cancel fail OPEN on unowned ids, and the … | **FIXED** | `main.py` `salesforce_context` computes the starter card against a throwaway key unless the caller owns the id; `salesforce_cancel` refuses unless `owner == viewer`; both refuse the reserved shape. Tests `test_salesforce_context_*`, `test_cancelling_a_clarification_under_an_unowned_id_is_refused`. | Orchestrator wiring |
| F051 | P1 | ADJUSTED | edge-devops | The worker node's OCR and speech engines answer unauthenticated on the office LAN | **PARTLY FIXED** | `compose/compose.whisper.yaml` now requires `WHISPER_BIND` (no LAN-address default); `compose/compose.ocr.yaml` documents the exposure. `scripts/whisper.sh` and `scripts/ocr.sh` still derive the management address, so the engines stay on the LAN until rebound (OA-6). | infrastructure hardening wave; repository owner (OA-6) |
| F052 | P1 | CONFIRMED | edge-devops | Portainer is published on 0.0.0.0:9000 (and IPv6) with the Docker socket mounted — … | **OPERATOR ACTION** | An unmanaged container outside the repository. OA-7. | repository owner |
| N017 | P1 | FOUND IN VERIFICATION | edge-devops | The unauthenticated model APIs sit on the same Docker network as the Cloudflare tunnel, so one … | **DEFERRED** | Not fixed: `compose.yaml` is unchanged, so the model services still share the `application` network with `cloudflared`. | infrastructure owner |
| F042 | P1 | ADJUSTED | inference-model-registry | The raw vLLM OpenAI endpoint listens on every host interface with no authentication | **PARTLY FIXED** | Same code change as F050; not live until OA-3. The audit's recommended control is the host packet filter, OA-4. | infrastructure hardening wave; repository owner |
| F043 | P1 | CONFIRMED | inference-model-registry | The OCR model engine on the worker node is reachable unauthenticated on the LAN | **PARTLY FIXED** | Documented in `compose/compose.ocr.yaml`; the bind is unchanged. OA-6 (rebind to the RoCE rail) or OA-4 applied on the worker. | repository owner |
| F044 | P1 | ADJUSTED | inference-model-registry | A non-streaming LONG-lane request keeps the NORMAL lane closed for the whole generation, and … | **FIXED** | `orchestrator/app/admission.py`: a LONG admission arms a prefill grace (`closure_grace_s`, capped at `LONG_CLOSURE_MAX_S` = 1200 s) that reopens NORMAL without a first token, and a waiter defers its bound for at most that ceiling. Tests `test_a_non_streaming_long_request_reopens_the_normal_lane_*` and siblings in `tests/test_inference_hardening.py`. | inference hardening wave |
| F045 | P1 | CONFIRMED | inference-model-registry | json_completion and chat_completion_with_reasoning never record token usage, so usage_events … | **FIXED** | `orchestrator/app/llm.py`: both `json_completion` branches and the best-of-N path now call `_capture_usage`. Tests `test_a_guided_json_completion_records_the_tokens_the_engine_reported` and siblings. `/v1` was not affected (it uses `stream_chat_events` only). | inference hardening wave |
| N013 | P1 | FOUND IN VERIFICATION | inference-model-registry | A crafted non-Latin prompt bypasses the LONG admission lane entirely, because admission … | **FIXED** | `orchestrator/app/context.py` + `admission.py`: the lane decision uses the exact count `fit_request` measured, and a "certainly small" verdict only from `upper_bound_messages` (UTF-8 bytes). Test `test_a_gujarati_prompt_whose_character_estimate_is_under_half_the_threshold_takes_the_long_lane`. Note: `/v1`'s own quota gate still estimates input at three characters per token (`router._admit` → `quotas.reserve`). | inference hardening wave |
| F012 | P1 | ADJUSTED | orchestrator-core | The raw vLLM model API is listening on 0.0.0.0:8000 with no authentication — /v1 on this host … | **PARTLY FIXED** | Same as F050/F042: code narrowed, not live until OA-3; OA-4 closes it without a restart. While open, anyone on the LAN bypasses every `/v1` key, quota and record by calling port 8000. | infrastructure hardening wave; repository owner |
| F015 | P1 | CONFIRMED | orchestrator-core | A 422 on POST /chat echoes the ENTIRE request body back to an unauthenticated caller | **FIXED** | `main.py` `_validation_error_without_input_echo` returns only `type`, `loc`, `msg` for every route; `/v1` resolves the key before parsing. Tests `test_a_422_from_chat_does_not_echo_the_request_body_back_to_the_caller` and two more. | Orchestrator wiring |
| F016 | P1 | ADJUSTED | orchestrator-core | No request body size limit anywhere on the orchestrator; an unauthenticated caller's body is … | **FIXED** | `main.py` `RequestBodySizeLimitMiddleware` (pure ASGI): `Content-Length` refusal plus a counting `receive`; 128 MiB default (`MAX_REQUEST_BODY_BYTES`), uploads above every per-purpose cap, 1 MiB on `/v1` in the public envelope. Tests `test_a_declared_oversize_body_*`, `test_a_body_with_no_declared_length_*`, `test_a_chunked_oversize_v1_body_*`. | Orchestrator wiring |
| F017 | P1 | CONFIRMED | orchestrator-core | LiveGeneration.events grows without bound — every SSE frame of every in-flight generation is … | **DEFERRED** | Not fixed: `LiveGeneration.events` is still an unbounded list in `main.py`. `/v1` does not use `_live_generations`, so the API surface does not add to it. | Orchestrator wiring (main.py owner) |
| F018 | P1 | ADJUSTED | orchestrator-core | No rate limit, quota or concurrency cap per identity on /chat — one principal can occupy all 10 … | **PARTLY FIXED** | For `/v1`: per-project and per-key requests/minute, tokens/minute, daily tokens and concurrency are enforced in `apiplatform/quotas.py` before admission (`router._admit` → `quotas.reserve`). For `/chat` and the rest of the app there is still no per-principal cap. | API platform core (done); chat owner (remainder) |
| N004 | P1 | FOUND IN VERIFICATION | orchestrator-core | The bare-API conversation key `u<user_id>-<session_id>` shares a namespace with user-chosen … | **FIXED** | Closed by the F034 change: a client can no longer name or claim a `u<digits>-` key. | Orchestrator wiring |
| F076 | P2 | ADJUSTED | admin-usage | The orchestrator serves an unauthenticated OpenAPI schema and Swagger UI (/openapi.json, /docs) … | **FIXED** | `main.py` `_dev_docs_enabled`: `/docs`, `/redoc`, `/openapi.json` are not mounted unless `ORCHESTRATOR_DEV_DOCS` is on. Verified by import probe (`GET /docs` → 404). Tests `test_fastapi_does_not_serve_its_own_schema` and three more. | Orchestrator wiring |
| F077 | P2 | CONFIRMED | admin-usage | Nav links to /admin/analytics/models, a page that does not exist - the Models board lives on … | **DEFERRED** | Not fixed: `frontend/app/admin/layout.tsx` is unchanged. | admin frontend owner |
| F078 | P2 | ADJUSTED | admin-usage | The per-member usage table and its CSV export are gated on WORKSPACE_READ (an ordinary admin … | **FIXED** | `orchestrator/app/authn/admin_api.py`: `GET /admin/api/analytics` and its export now require `Cap.ANALYTICS_READ` (super admin), and viewing is audited as `analytics_viewed`. Test `test_the_per_member_usage_table_and_its_export_need_the_analytics_capability`. Behaviour change: an admin no longer sees the per-member usage table. | admin surface hardening wave |
| F079 | P2 | CONFIRMED | admin-usage | The two audited admin download routes record a blank user_agent and the proxy container's IP, … | **DEFERRED** | Not fixed: `frontend/app/api/admin/[...path]/route.ts` (`proxyDownload`) is unchanged and still forwards only the cookie. | Frontend BFF hardening |
| F080 | P2 | CONFIRMED | admin-usage | The audit log API returns each event's meta jsonb but the page has no column for it, so role … | **DEFERRED** | Not fixed: `frontend/app/admin/audit/page.tsx` is unchanged, so `meta` — including the `public_id` of a created or revoked API key — is not shown. OPERATIONS.md §9 tells operators to read it from `GET /admin/api/audit`. | admin frontend owner |
| F081 | P2 | ADJUSTED | admin-usage | The edge auth middleware never runs for any path beginning with the letters "api", so the … | **FIXED** | `frontend/middleware.ts` matcher is now `/((?!api/\|_next/\|v1(?:/\|$)\|.*\..*).*)`, so the console page `/api` is gated; `frontend/lib/auth.ts` `authRedirect` agrees. | Frontend BFF hardening / Frontend console |
| F082 | P2 | ADJUSTED | admin-usage | proxyToOrchestrator buffers whole request and response bodies and drops the Authorization … | **FIXED** | By a separate route rather than by changing `proxyToOrchestrator`: `frontend/app/v1/[[...path]]/route.ts` streams unbuffered, forwards `authorization` and `idempotency-key`, never `cookie`. Tests `frontend/tests/public-edge.test.ts`. | Frontend BFF hardening |
| N027 | P2 | FOUND IN VERIFICATION | admin-usage | The admin usage CSV export writes attacker-controlled display names and emails unescaped, while … | **FIXED** | `admin_api.py` `_csv_field` runs every usage-export cell through the artifact CSV writer's `cell_text` + `neutralise`. Test `test_a_formula_in_a_display_name_reaches_the_usage_export_as_inert_text`. | admin surface hardening wave |
| N028 | P2 | FOUND IN VERIFICATION | admin-usage | Every proxied POST/PUT body is buffered whole into the Next process before any authentication, … | **FIXED** | `frontend/lib/proxy.ts` `readBoundedBody` with `MAX_PROXY_BODY_BYTES` (32 MiB) and a declared-length pre-check; `/api/chat` bounded at 128 MiB, `/api/upload` bounded. Tests in `frontend/tests/bff-hardening.test.ts`. | Frontend BFF hardening |
| N029 | P2 | FOUND IN VERIFICATION | admin-usage | The only request throttle in the system is keyed to login email — there is no per-principal or … | **PARTLY FIXED** | `/v1` has a quota layer keyed by project and key in the orchestrator. Admin, analytics and chat routes still have no per-principal or per-address limit beyond login. | API platform core (done); remainder unowned — propose the orchestrator wiring owner |
| F026 | P2 | ADJUSTED | authn-authz | FastAPI /docs, /redoc and /openapi.json are unauthenticated on an 0.0.0.0-bound port and … | **FIXED** | Same change as F076. | Orchestrator wiring |
| F027 | P2 | ADJUSTED | authn-authz | AUTH_TRUST_PROXY_HEADERS=true plus a directly reachable orchestrator makes X-Forwarded-For … | **OPERATOR ACTION** | Browser side not fixed: `authn/sessions.py` still believes `X-Forwarded-For` whenever `AUTH_TRUST_PROXY_HEADERS=true`, so a LAN host that reaches :8080 chooses the address written to sessions, audit and the login lockout (OA-8). The BFF half is fixed (F002). The `/v1` half is fixed: `apiplatform/resolver.client_address` believes a forwarded address only from a peer in `PUBLIC_API_TRUSTED_PROXIES` (OA-22 sets it). | repository owner (OA-8, OA-22); peer-conditional trust for sessions deferred to the authn owner |
| F028 | P2 | ADJUSTED | authn-authz | Invitation-claim account takeover: an ADMIN can seize any disabled or removed account — … | **FIXED** | Closed on all three doors (2026-09-13). Issuing: `authn/invites.py` `claim_refusal` in `admin_api.create_invitation`, refusals audited as `invitation_refused`. Accepting: `authn/api.py` re-checks the rule with the inviter's rank read at acceptance (fails closed for a deactivated, removed or deleted inviter), `store.accept_invitation` no longer rewrites an existing membership's role and revokes the claimed account's old sessions, refusals audited as `invitation_claim_refused`. Restart: `bootstrap.ensure_identity_baseline` no longer hands a membership back to a removed (disabled, membership-less) account. Tests: `tests/test_authn_admin_hardening.py`, `tests/test_authn_invite_accept.py` (each shown failing without its fix). OA-9 remains as defence in depth for invitations issued before the release. |
| F029 | P2 | CONFIRMED | authn-authz | An ADMIN can read a SUPER_ADMIN's (and a peer admin's) conversations, uploads and reports — the … | **FIXED** | `admin_api.py` `_inspectable_member` applies `outranks` on every member content and session read. Test `test_an_admin_cannot_read_a_super_admins_conversations_uploads_or_reports_but_can_read_a_members`. | admin surface hardening wave |
| F033 | P2 | ADJUSTED | authn-authz | No request-body size limit anywhere: PUT /auth/preferences validates size only after the whole … | **FIXED** | Same middleware as F016. | Orchestrator wiring |
| N006 | P2 | FOUND IN VERIFICATION | authn-authz | An ADMIN can read per-person usage analytics through Cap.WORKSPACE_READ, contradicting … | **FIXED** | Same change as F078. | admin surface hardening wave |
| N008 | P2 | FOUND IN VERIFICATION | authn-authz | In-process SessionMemory is an unbounded dict keyed by an unvalidated client-supplied … | **PARTLY FIXED** | `session_id` is now bounded to 64 characters of `[A-Za-z0-9_-]` (F034 change). `SessionMemory` still has no key ceiling or idle TTL. | memory owner |
| F068 | P2 | CONFIRMED | cicd | The verify step named "A rolling deploy did not restart the main model" asserts nothing and can … | **FIXED** | `.github/workflows/pipeline.yml` verify step compares the main-model container's start time with the rollout and exits 1 when a rolling deploy restarted it. | CI/CD |
| F069 | P2 | CONFIRMED | cicd | The deploy summary's `tail -40 .runtime/logs/deploy-*.log` has never worked: GNU tail rejects … | **FIXED** | `pipeline.yml` now finds the newest deploy log and runs `tail -n 40` on it. | CI/CD |
| F071 | P2 | CONFIRMED | cicd | The pip-audit step can never report a non-success outcome, so the supply-chain summary table … | **FIXED** | `pipeline.yml` pip-audit records a real outcome per requirements file and the summary row shows it. | CI/CD |
| F074 | P2 | CONFIRMED | cicd | There is no rollback path in the pipeline, and the rollback script that exists is called by … | **PARTLY FIXED** | `pipeline.yml` now runs `scripts/deploy-rollback.sh --help`, asserts its flags and its refusal to restore the database, and publishes `--list` and the rollback commands in the run summary. The workflow_dispatch-only `rollback` job the audit proposed was not added. | CI/CD |
| F075 | P2 | CONFIRMED | cicd | DEPLOY_BRANCH is documented as a live repository variable but the pipeline stopped reading it; … | **OPERATOR ACTION** | The pipeline derives the branch from the deployed ref; the stale repository variable still exists. OA-10 deletes it. README.md's variable table and dispatch sentence were corrected in this branch (Docs and evidence). | repository owner (OA-10); Docs and evidence (README) |
| N022 | P2 | FOUND IN VERIFICATION | cicd | P5 only checks that a job DECLARES permissions, never what it declares — a job can grant itself … | **DEFERRED** | Not fixed: P5 still checks only that a job declares `permissions`, not what it grants. | CI/CD |
| N023 | P2 | FOUND IN VERIFICATION | cicd | No CI gate can ever see a newly published 0.0.0.0 port: the blocking trivy pass excludes … | **PARTLY FIXED** | `launcher/tests/test_network_exposure.py` now fails on a compose default that binds every interface or a LAN address, and on a published model port that follows the application bind. The `verify` job still has no live `ss` bind assertion. | infrastructure hardening wave; CI/CD (verify assertion) |
| F035 | P2 | ADJUSTED | database | cancel_parked_chat_requests has no owner predicate and can cancel another user's queued … | **DEFERRED** | Not fixed: `db.cancel_parked_chat_requests(conversation_id, *, keep)` still has no owner predicate. Reach is reduced by F034 (synthetic keys cannot be claimed). | Database |
| F036 | P2 | ADJUSTED | database | workspace_id is denormalised text with no foreign key on every table a billing or quota query … | **PARTLY FIXED** | Every V34 table carries `workspace_id` with a foreign key to `workspaces`. `usage_events`, `query_traces` and `conversation_shares` were not altered (that is a validating `ALTER` on live tables — F038's production window). | Database |
| F037 | P2 | ADJUSTED | database | One-membership-per-user resolution blocks a multi-tenant API: Principal silently picks the … | **FIXED** | For the API path, by design: the resolver takes the workspace from the key's project row and checks the key row agrees, never `Principal.membership()`. The browser's single-membership resolution is unchanged, as the audit advised. | API platform core |
| F038 | P2 | CONFIRMED | database | Migration DDL runs under a 15 s statement_timeout inside one all-or-nothing transaction, so a … | **DEFERRED** | Not fixed: migrations still run under the pooled 15 s `statement_timeout` in one transaction. V34 is unaffected — checked: its block is 11 `CREATE TABLE` and 30 `CREATE [UNIQUE] INDEX` statements on new empty tables, no `ALTER`, `UPDATE` or `INSERT`. | Database |
| F039 | P2 | ADJUSTED | database | V29 and V30 do not index the user_id foreign keys their cascades walk, against the rule V31 … | **DEFERRED** | Not fixed for V29/V30. V34 indexes every foreign key it declares (`test_every_v34_foreign_key_leads_an_index_because_a_cascade_walks_it`). | Database |
| F040 | P2 | CONFIRMED | database | No retention or pruning exists for the four append-only tables a public API will grow fastest | **PARTLY FIXED** | `db.prune_api_platform` (V34 tables, bounded batches) is now scheduled by the orchestrator lifespan: 60 s after start, then every `ARTIFACT_MAINTENANCE_INTERVAL_S` (`app/main.py` `_api_platform_prune_loop`). `usage_events`, `query_traces` and finished `chat_requests` still have no retention at all. | Orchestrator wiring (done); Database (remaining tables) |
| F041 | P2 | ADJUSTED | database | Test isolation depends on a hand-maintained table list, so a V34 table leaks state between … | **PARTLY FIXED** | The eleven V34 tables were added to `tests/conftest.py` `_APP_TABLES`. The guard test comparing the list with `information_schema.tables` was not added. | Database |
| N011 | P2 | FOUND IN VERIFICATION | database | chat_requests.intent_id is one global primary-key namespace across all users, so a … | **DEFERRED** | Not reachable from `/v1`, whose idempotency lives in `api_idempotency` scoped by `(project_id, endpoint, key)`. The `/chat` `chat_requests.intent_id` namespace is unchanged. | Database |
| N012 | P2 | FOUND IN VERIFICATION | database | web_pages is a single global corpus with no tenant predicate on retrieval — one API caller can … | **DEFERRED** | Not reachable from `/v1`: CONTRACT §7 exposes no retrieval or web search, and the router calls `stream_chat_events` only. Must be settled before any retrieval is exposed to API callers. | Database / web-memory owner |
| F053 | P2 | ADJUSTED | edge-devops | The orchestrator publishes on 0.0.0.0:8080 and serves FastAPI's /docs, /redoc and /openapi.json … | **PARTLY FIXED** | The schema pages are off (F076). The orchestrator port is still published on `TECHSARA_BIND_ADDRESS` (`compose.yaml` unchanged) and on every interface in the legacy `docker-compose.yml`. OA-8. | Orchestrator wiring (done); repository owner (OA-8) |
| F054 | P2 | ADJUSTED | edge-devops | The orchestrator container carries every secret in the project, including the Cloudflare tunnel … | **DEFERRED** | Not fixed: `compose.yaml` still passes every secret to the orchestrator. `API_KEY_PEPPER` joins them when set. | infrastructure owner |
| F055 | P2 | ADJUSTED | edge-devops | The engine controller's state and metrics API is on 0.0.0.0:9838 with no authentication | **PARTLY FIXED** | `monitoring/engine-controller/controller.py` defaults `CONTROLLER_BIND` to `127.0.0.1` and accepts a comma list; the compose overlays pass loopback plus, in dual mode, the bridge gateway. Tests in `launcher/tests/test_network_exposure.py`. Live only when the controller container is recreated (OA-3 or a controller-only recreate, outside a recovery window). | infrastructure hardening wave; repository owner |
| F056 | P2 | ADJUSTED | edge-devops | node_exporter publishes full host telemetry on 0.0.0.0:9100 with no firewall behind it | **PARTLY FIXED** | `compose/compose.monitoring.yaml` defaults node-exporter to `172.17.0.1:9100` (the worker overlay already defaulted to loopback). Live only after the monitoring stack is recreated (OA-12). | infrastructure hardening wave; repository owner (OA-12) |
| F057 | P2 | ADJUSTED | edge-devops | cadvisor receives the Docker socket through a read-only bind of /var/run, which does not make … | **DEFERRED** | Only the misleading comment was corrected (`compose/compose.monitoring.yaml` now records the root-equivalent risk as accepted). The docker-socket-proxy sidecar was not added. | infrastructure owner |
| F058 | P2 | ADJUSTED | edge-devops | AUTH_TRUST_PROXY_HEADERS is on while the orchestrator is directly reachable on 0.0.0.0:8080, so … | **OPERATOR ACTION** | Same as F027: browser side OA-8; `/v1` now uses `PUBLIC_API_TRUSTED_PROXIES`. | repository owner; authn owner |
| F059 | P2 | CONFIRMED | edge-devops | The public tunnel's ingress is not in version control — the public routing exists only in the … | **OPERATOR ACTION** | The tunnel ingress lives only in the Cloudflare dashboard. OA-13. Note: `/v1` today rides the existing `ai.techsarasolutions.com` → `frontend:3000` mapping, so it needs no ingress change; a future `api.techsarasolutions.com` would. | repository owner |
| F060 | P2 | CONFIRMED | edge-devops | Grafana's session cookie is issued without the Secure flag on a public HTTPS hostname | **OPERATOR ACTION** | OA-14. | repository owner |
| F061 | P2 | CONFIRMED | edge-devops | SearXNG falls back to a hard-coded secret when the generated env is absent | **DEFERRED** | Not fixed: `compose.yaml` SearXNG secret fallback unchanged. | infrastructure owner |
| F062 | P2 | CONFIRMED | edge-devops | An unmanaged container is holding the application data volume open | **OPERATOR ACTION** | OA-15. | repository owner |
| F063 | P2 | ADJUSTED | edge-devops | The cluster's torch-distributed master port and an iperf3 server listen on all interfaces | **OPERATOR ACTION** | OA-16. | repository owner |
| N018 | P2 | FOUND IN VERIFICATION | edge-devops | A second unmanaged container, litellm-dgx, is already an OpenAI gateway in front of the main … | **OPERATOR ACTION** | A second, unmanaged OpenAI gateway in front of the main model bypasses the developer platform entirely. OA-17. | repository owner |
| N019 | P2 | FOUND IN VERIFICATION | edge-devops | The public login page and the login API are served over plaintext HTTP with no HSTS | **OPERATOR ACTION** | OA-18. Matters more now: an API key sent to `http://…/v1` crosses the client's network in clear. | repository owner |
| N020 | P2 | FOUND IN VERIFICATION | edge-devops | The whole application is published on 0.0.0.0:3000, so every Cloudflare edge control is one hop … | **OPERATOR ACTION** | A decision, not a patch: whether the app is meant to be reachable on the LAN. `compose.yaml` still publishes the frontend on `TECHSARA_BIND_ADDRESS`. OA-8 covers the orchestrator; the frontend is the same choice. | repository owner |
| F001 | P2 | ADJUSTED | frontend-app | Request bodies are buffered whole in the Next process before any authentication, with no size … | **FIXED** | Same change as N028: bounded reads in `frontend/lib/proxy.ts`, `app/api/chat/route.ts`, `app/api/upload/route.ts`. | Frontend BFF hardening |
| F002 | P2 | ADJUSTED | frontend-app | The BFF launders a client-supplied Cf-Connecting-IP into a trusted X-Forwarded-For, forging … | **PARTLY FIXED** | BFF half fixed: `frontend/lib/proxy.ts` sends `X-Forwarded-For` only from the one header named by `TRUSTED_CLIENT_IP_HEADER` and never relays a client-supplied forwarding header (tests `forwarded identity — …` in `bff-hardening.test.ts`). Orchestrator half (peer-conditional trust) deferred with F027. **New operator step** OA-19: without `TRUSTED_CLIENT_IP_HEADER`, every audit event and the per-address login lockout see the frontend container's address. | Frontend BFF hardening; repository owner (OA-19) |
| F004 | P2 | CONFIRMED | frontend-app | The page gate will 307 the planned /v1 API and /docs pages to /login | **FIXED** | For `/v1`: excluded from the matcher (`v1(?:/\|$)`) and from `authRedirect`. `/docs` deliberately stays behind sign-in (decision recorded in `frontend/lib/auth.ts`). | Frontend BFF hardening |
| N002 | P2 | FOUND IN VERIFICATION | frontend-app | No Next proxy forwards Authorization, so an API-key credential cannot cross the BFF at all | **FIXED** | For `/v1`, by the dedicated edge route (F082), which forwards `authorization`. `proxyToOrchestrator` deliberately still does not. | Frontend BFF hardening |
| F046 | P2 | ADJUSTED | inference-model-registry | Image and other multimodal prompts are sized as text-only, so a large image prefill never … | **FIXED** | `orchestrator/app/context.py` `estimate_image_tokens` sizes an image from its header dimensions (ceiling for an unreadable one) and multimodal prompts count it. Tests `test_an_image_is_sized_from_its_pixels_*`, `test_a_prompt_of_large_images_and_a_short_question_takes_the_long_lane`. Not reachable from `/v1`, which accepts text only. | inference hardening wave |
| F047 | P2 | ADJUSTED | inference-model-registry | The engine controller's /state document is served unauthenticated on every host interface | **PARTLY FIXED** | Same controller change as F055; live after recreate. | infrastructure hardening wave; repository owner |
| F048 | P2 | ADJUSTED | inference-model-registry | The AsyncOpenAI client cache is cleared without closing the clients, leaking httpx connection … | **FIXED** | `llm.py` and `context.py` client caches are LRU and close what they evict. Tests `test_the_model_client_cache_closes_what_it_evicts_*`, `test_the_tokenize_client_cache_closes_what_it_evicts`. | inference hardening wave |
| F049 | P2 | ADJUSTED | inference-model-registry | Module and function docs still describe gpt-oss-120b and a 131072 window, which the /docs build … | **FIXED** | `llm.py`/`context.py` docstrings no longer name a retired model or a fixed window (one historical sentence in `llm.py` remains, worded as history). Test `test_the_inference_docs_name_no_retired_model_and_no_fixed_context_window`. Also moot for publishing: FastAPI's schema is off and the public document is hand-built. | inference hardening wave |
| N014 | P2 | FOUND IN VERIFICATION | inference-model-registry | One user-triggerable 400 on any streaming call permanently disables token telemetry for the … | **FIXED** | `llm.py`: a refusal of `stream_options.include_usage` pauses the option for `_USAGE_RETRY_S` (600 s) instead of for the process lifetime, a 400 that is not about the option propagates, and a counter records the pause. Tests `test_a_bad_request_on_a_stream_propagates_*`, `test_a_runtime_that_refuses_the_usage_option_is_asked_again_*`. Relevant to `/v1`: while usage is not measured, token quotas do not advance. | inference hardening wave |
| N015 | P2 | FOUND IN VERIFICATION | inference-model-registry | The orchestrator's /health and /metrics are unauthenticated on 0.0.0.0:8080 and publish live … | **DEFERRED** | Not fixed: `/health` and `/metrics` unchanged. OA-8 limits who can reach them. | Orchestrator wiring |
| N016 | P2 | FOUND IN VERIFICATION | inference-model-registry | User-submitted OCR images cross the office LAN in cleartext because OCR_BASE_URL points at the … | **OPERATOR ACTION** | OA-6 (move OCR onto the RoCE rail, rewriting `OCR_BASE_URL`/`OCR_REMOTE_BASE_URL` in lockstep). | repository owner |
| F013 | P2 | ADJUSTED | orchestrator-core | /docs, /redoc and /openapi.json are enabled and unauthenticated on a 0.0.0.0-published port, … | **FIXED** | Same change as F076. | Orchestrator wiring |
| F014 | P2 | ADJUSTED | orchestrator-core | GET /health is unauthenticated and returns internal hostnames, container paths, engine capacity … | **DEFERRED** | Not fixed: `GET /health` still returns the full document unauthenticated. OA-8 limits reach. | Orchestrator wiring |
| F019 | P2 | ADJUSTED | orchestrator-core | BUILD BLOCKER — the app-wide CSRF middleware and the 3-origin CORS allowlist will break every … | **FIXED** | `main.py`: `_reject_cross_site_writes` skips `/v1` (`_is_public_api_path`, which does not match `/v1beta…`); `BrowserCorsExceptPublicApi` leaves the credentialed allowlist unwidened and off `/v1`; the router answers its own preflight without `Access-Control-Allow-Credentials`. Tests in `tests/test_publicapi_mount.py`; preflight verified by probe. | Orchestrator wiring + Public API surface |
| F020 | P2 | ADJUSTED | orchestrator-core | BUILD BLOCKER — the generation registry is one-per-conversation-key and the second concurrent … | **FIXED** | For `/v1`, by design: every API request runs its own `streaming.Generation` over `llm.stream_chat_events` and never registers in `_live_generations`. The chat registry is unchanged. | Public API surface |
| F021 | P2 | CONFIRMED | orchestrator-core | The CSRF 403 is emitted outside CORSMiddleware, so browsers see an opaque CORS error instead of … | **DEFERRED** | Not fixed: `_reject_cross_site_writes` is still declared after `app.add_middleware(BrowserCorsExceptPublicApi, …)`, so it remains outermost and its 403 carries no CORS headers. Does not affect `/v1`, which is exempt. | Orchestrator wiring |
| F022 | P2 | ADJUSTED | orchestrator-core | GET /chat/trace/{trace_id} returns sanitized exception text that can carry an internal hostname … | **DEFERRED** | Not fixed for `/chat/trace/{id}`. `/v1` exposes no trace route and discards the text of any exception it did not construct (`errors.from_unexpected`). | Orchestrator wiring |
| F023 | P2 | CONFIRMED | orchestrator-core | No response carries a request id; the correlation id exists but never leaves via a header, and … | **PARTLY FIXED** | Every `/v1` response now carries `X-Request-Id`, including 404/405 for unknown paths and middleware 413s (verified by probe), and the envelope echoes it. Chat routes still send none. | Public API surface (done); Orchestrator wiring (chat) |
| N005 | P2 | FOUND IN VERIFICATION | orchestrator-core | `session_id` is the only client-supplied identifier on ChatRequest with no validation — … | **FIXED** | Same change as F034. | Orchestrator wiring |
| F030 | P3 | ADJUSTED | authn-authz | The cross-site-write middleware is inert for all real browser traffic and has no … | **PARTLY FIXED** | The `/v1` exemption (c) is done (F019). The stale comment (a) and the Origin forwarding / `Sec-Fetch-Site` fallback (b) are not. | Orchestrator wiring; Frontend BFF hardening |
| F031 | P3 | ADJUSTED | authn-authz | Production CORS allowlist still contains http://localhost:3000 and http://127.0.0.1:3000 with … | **OPERATOR ACTION** | OA-20. | repository owner |
| N007 | P3 | FOUND IN VERIFICATION | authn-authz | GET /admin/api/members/{user_id}/sessions has no outranks guard - an admin can list a super … | **FIXED** | Same `_inspectable_member` change as F029. Test `test_an_admin_cannot_list_or_revoke_a_super_admins_sessions_but_can_for_a_member`. | admin surface hardening wave |
| N009 | P3 | FOUND IN VERIFICATION | authn-authz | chat_requests shares one conversation_id namespace between owner-checked ids and synthetic … | **PARTLY FIXED** | The synthetic namespace can no longer be named by a client (F034). `cancel_parked_chat_requests` and `latest_chat_request` are still not user-scoped. | Database |
| F070 | P3 | CONFIRMED | cicd | The `launcher (3.11)` matrix leg reports success when it discovers zero tests | **FIXED** | `.github/workflows/scripts/unittest_gate.py` with `--min-tests` fails a leg that discovers too few tests; the launcher leg uses it. | CI/CD |
| F072 | P3 | CONFIRMED | cicd | Eight of twelve jobs declare no `timeout-minutes` and inherit the 6-hour default | **FIXED** | Every job in `pipeline.yml` declares `timeout-minutes`, and `workflow_policy.py` P7 fails a job that does not. Run locally: `workflow policy: OK (P1-P8)`. | CI/CD |
| F073 | P3 | CONFIRMED | cicd | The recovery job publishes the full unauthenticated /health payload into a public repository's … | **DEFERRED** | Not fixed: the recovery job still writes `curl … /health \| head -60` into `$GITHUB_STEP_SUMMARY` of a public repository. | CI/CD |
| N024 | P3 | FOUND IN VERIFICATION | cicd | pip-audit never scans the orchestrator's production dependency set | **DEFERRED** | Not fixed: the pip-audit loop still reads `orchestrator/requirements-dev.txt` and `sync-worker/requirements.txt`, never `orchestrator/requirements.txt`. | CI/CD |
| N025 | P3 | FOUND IN VERIFICATION | cicd | The engine controller binds 0.0.0.0 by default and serves /state unauthenticated on the LAN | **PARTLY FIXED** | Same controller change as F055; live after recreate. | infrastructure hardening wave; repository owner |
| N026 | P3 | FOUND IN VERIFICATION | cicd | AUTH_TRUST_PROXY_HEADERS=true while the orchestrator port is LAN-reachable, so the audit … | **OPERATOR ACTION** | Same as F027: OA-8 for the browser side; `/v1` fixed by `PUBLIC_API_TRUSTED_PROXIES`. | repository owner; authn owner |
| F003 | P3 | ADJUSTED | frontend-app | The middleware matcher has a hole at exactly `/api` — a console page there would render for … | **FIXED** | Same matcher change as F081. | Frontend BFF hardening |
| F005 | P3 | CONFIRMED | frontend-app | proxyToOrchestrator discards every response header except content-type — Retry-After and … | **FIXED** | `frontend/lib/proxy.ts` `relayedResponseHeaders` relays `Retry-After`, the rate-limit headers and `Set-Cookie`. Test `relays the retry and rate-limit headers a 429 is useless without`. | Frontend BFF hardening |
| F006 | P3 | ADJUSTED | frontend-app | proxyToOrchestrator sends no abort signal, so a closed tab leaves the upstream request running … | **PARTLY FIXED** | `proxyToOrchestrator` now passes `AbortSignal.any([req.signal, AbortSignal.timeout(30 s)])` (answers 499/504). `frontend/app/api/chat/active/route.ts` was not changed. | Frontend BFF hardening |
| F007 | P3 | ADJUSTED | frontend-app | MOCK_MODE is a complete authentication bypass reachable through a single environment variable | **DEFERRED** | Not fixed: `frontend/lib/mockApi.ts` does not refuse `MOCK_MODE=true` in production. OA-21 keeps it off. | Frontend; repository owner (OA-21) |
| F008 | P3 | CONFIRMED | frontend-app | The admin nav links to /admin/analytics/models, which has no page | **DEFERRED** | Not fixed (same as F077). | admin frontend owner |
| F009 | P3 | CONFIRMED | frontend-app | The BFF strips Origin, disabling the orchestrator's CSRF second layer for all browser writes | **DEFERRED** | Not fixed: `frontend/lib/proxy.ts` still neither forwards nor checks `Origin` on browser writes. | Frontend BFF hardening |
| F010 | P3 | CONFIRMED | frontend-app | proxyToOrchestrator round-trips request bodies through a UTF-8 string, corrupting any binary … | **FIXED** | `frontend/lib/proxy.ts` forwards the request body as bytes. Test `forwards bytes that are not valid UTF-8 unchanged`. | Frontend BFF hardening |
| F011 | P3 | CONFIRMED | frontend-app | Two route-handler comments assert an auth posture the orchestrator no longer has | **DEFERRED** | Not fixed: `frontend/app/api/reports/[filename]/route.ts` and `main.py` still say `/reports` is auth-free. | Frontend BFF hardening; Orchestrator wiring |
| N001 | P3 | FOUND IN VERIFICATION | frontend-app | The matcher's dot rule excludes any path with a dot in ANY segment, while authRedirect only … | **DEFERRED** | Not fixed: the matcher's `.*\..*` still excludes a dot in any segment while `authRedirect` checks only the last one. | Frontend BFF hardening |
| N003 | P3 | FOUND IN VERIFICATION | frontend-app | There is no rate limiting anywhere in the frontend BFF, and the login throttle is the only rate … | **PARTLY FIXED** | Rate limiting exists for `/v1` in the orchestrator, keyed by key and project. The BFF and the browser routes still have none beyond the login throttle. | API platform core (done); remainder as N029 |
| F032 | none | ADJUSTED | authn-authz | The edge middleware never runs for the literal path /api — the planned developer console page … | **FIXED** | Same matcher change as F081. | Frontend BFF hardening |
| F024 | none | CONFIRMED | orchestrator-core | No route declares a response_model, so the generated OpenAPI describes no response shapes at all | **PARTLY FIXED** | `/v1` publishes a hand-built OpenAPI 3.1 document with response schemas (`publicapi/openapi.py`), checked in CI by `api_contract.py` (run locally: 8 operations, all matching). The routes return `JSONResponse` rather than declaring `response_model`, and chat routes still declare none. | Public API surface |
| F025 | none | ADJUSTED | orchestrator-core | Single uvicorn worker with all request lifecycle state in process memory — the /v1 surface … | **PARTLY FIXED** | `/v1` background work is durable (`api_responses`, `api_webhook_deliveries`) rather than in `_live_generations`. A row orphaned by a restart is closed as `failed` when it is read (`background.repair_if_orphaned` on `GET /v1/responses/{id}`); there is still no start-up sweep, so an orphan nobody reads stays open. | Public API surface (done); Orchestrator wiring (start-up sweep) |

---

## Operator actions

Settings and commands that cannot be applied from code. Each is written for the
repository owner, names what it closes, and says how to confirm it worked.
**None of these has been executed by the author.** Where a command changes the
network, test it on one address first and keep the removal command at hand.

### OA-1 — Require approval for every external contributor's workflow run (F064, immediate)

Today `approval_policy` is `first_time_contributors`, so an account with one
merged commit (the audit found one besides the owner) runs workflows on the
self-hosted production runner with no click at all.

* **Settings**: repository → Settings → Actions → General → "Approval for running
  fork pull request workflows from contributors" → **Require approval for all
  external contributors** → Save.
* **Or the API** (the REST method is `PUT`; the audit text says `PATCH` — if the
  API refuses, the Settings page is authoritative):

  ```bash
  gh api -X PUT repos/namanjain221995/personal-LLM-Chabot/actions/permissions/fork-pr-contributor-approval \
    -f approval_policy=all_external_contributors
  ```

* **Confirm**:
  `gh api repos/namanjain221995/personal-LLM-Chabot/actions/permissions/fork-pr-contributor-approval`
  must print `{"approval_policy":"all_external_contributors"}`.

What it does not do: the approval click is now the control. Before pressing
"Approve and run" on any external pull request, read its changes under
`.github/`; never approve one that adds or edits a job, a `runs-on`, or a
script a job calls. The workflow file that runs is the one **in the pull
request**, so `pipeline.yml`'s own guards and `workflow_policy.py` protect
nothing here.

### OA-2 — Take the self-hosted runner off the public repository (F064, durable)

The runner `spark-0e68` (labels `self-hosted, Linux, ARM64, dgx-spark`) runs as
`techsphere`, who is in the `docker` and `sudo` groups. As long as it is
registered to a public repository, a single mistaken approval is root on the
production box. Two ways to end that; choose one.

**A. Move the runner to a private deployment repository (keeps this repository public).**

1. Create a private repository, for example
   `namanjain221995/personal-LLM-Chabot-deploy`, holding one workflow that
   accepts `repository_dispatch` (type `deploy`) and `workflow_dispatch`, runs on
   `[self-hosted, dgx-spark]`, checks out the public repository at the
   dispatched commit **only if that commit is on `main`**, and runs
   `scripts/deploy.sh`.
2. On `spark-0e68`, as `techsphere`, in the runner's directory:

   ```bash
   systemctl --user list-units 'actions.runner*'          # the unit name
   systemctl --user stop <that unit>
   ./config.sh remove --token "$(gh api -X POST repos/namanjain221995/personal-LLM-Chabot/actions/runners/remove-token --jq .token)"
   ./config.sh --unattended --labels dgx-spark \
     --url https://github.com/namanjain221995/personal-LLM-Chabot-deploy \
     --token "$(gh api -X POST repos/namanjain221995/personal-LLM-Chabot-deploy/actions/runners/registration-token --jq .token)"
   systemctl --user start <that unit>
   ```

3. In this repository, replace the `deploy`, `verify` and `recovery` jobs'
   self-hosted steps with one GitHub-hosted job, on push to `main` only, that
   sends the dispatch with a fine-grained token scoped to the private
   repository alone (stored as a repository secret; secrets are not given to
   fork pull request runs). That is a `pipeline.yml` change for the CI/CD team.
4. **Confirm**:
   `gh api repos/namanjain221995/personal-LLM-Chabot/actions/runners --jq .total_count`
   prints `0`.

**B. Make this repository private.** Settings → General → Danger Zone → Change
visibility. Workflows from fork pull requests do not run in a private
repository unless explicitly enabled, and the run summaries (F073) stop being
public. The cost is that the repository is no longer public.

Longer term, whichever is chosen: take `techsphere` out of the `docker` group
in favour of a socket proxy, so a job is not root-equivalent by default.

### OA-3 — Recreate the main engine so the new bind takes effect (F050, F042, F012, F065)

The launcher change stops `PUBLISH_MODEL_PORTS=true` from putting the dual-mode
head on `0.0.0.0`; the running engine still has `--host 0.0.0.0` in its argv.

In a production window, after the branch is on `main`:

```bash
./techsara redetect                       # rewrites .runtime/generated.env (CLUSTER_API_BIND_ADDRESS)
grep CLUSTER_API_BIND_ADDRESS .runtime/generated.env   # expect the bridge gateway, e.g. 172.17.0.1 — not 0.0.0.0
scripts/deploy.sh --ref main --full       # recreates every container, the TP=2 pair included
ss -ltnH 'sport = :8000'                  # the listener must not be 0.0.0.0
```

Cold start is budgeted at up to 900 s. Before the window, confirm the consumers
still reach the new address: the orchestrator and sync worker
(`vllm:host-gateway`), the engine controller, Prometheus, the unmanaged
`litellm-dgx` container (`host.docker.internal:8000`), and the
interview-analysis pipeline on the worker, which calls the raw port over the
RoCE address — **that one will lose access**, because after the recreate the
head listens on the Docker bridge gateway only. Move it to the orchestrator's
`/v1` with an API key before the window.
From a LAN host, `curl -m 5 http://192.168.9.54:8000/v1/models` must then fail
to connect.

### OA-4 — Host packet filter for the engine and controller ports (F050, F042, F012, F047, F055, N025)

Closes the LAN, tailnet and RoCE exposure of `:8000` and `:9838` on the head
**without a restart**, and stays as a backstop after OA-3. Both processes use
host networking, so the host's input hook sees their traffic.

First check the bridge subnets on this host:
`docker network inspect -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' $(docker network ls -q)`.
With the audit-time values (bridges `172.17–172.19.0.0/16`, RoCE rails
`10.100.184.0/24` and `10.100.185.0/24`):

```bash
sudo nft add table inet techsara_guard
sudo nft 'add chain inet techsara_guard input { type filter hook input priority -10 ; policy accept ; }'
sudo nft add rule inet techsara_guard input iifname "lo" tcp dport '{ 8000, 9838 }' accept
sudo nft add rule inet techsara_guard input ip saddr '{ 172.17.0.0/16, 172.18.0.0/16, 172.19.0.0/16 }' tcp dport '{ 8000, 9838 }' accept
sudo nft add rule inet techsara_guard input ip saddr '{ 10.100.184.0/24, 10.100.185.0/24 }' tcp dport 8000 accept
sudo nft add rule inet techsara_guard input tcp dport '{ 8000, 9838 }' drop
```

Before persisting, prove every consumer listed in OA-3 still works, plus
`/health`'s engine check and one real chat. Undo:
`sudo nft delete table inet techsara_guard`. Persist by adding the same table to
`/etc/nftables.conf` once verified.

### OA-5 — Environment approval and a ruleset on `main` (F067)

```bash
OWNER_ID="$(gh api users/namanjain221995 --jq .id)"
gh api -X PUT repos/namanjain221995/personal-LLM-Chabot/environments/production --input - <<JSON
{"reviewers":[{"type":"User","id":${OWNER_ID}}],
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST repos/namanjain221995/personal-LLM-Chabot/environments/production/deployment-branch-policies \
  -f name=main -f type=branch

gh api -X POST repos/namanjain221995/personal-LLM-Chabot/rulesets --input - <<'JSON'
{"name": "main", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
 "rules": [
  {"type": "required_status_checks",
   "parameters": {"strict_required_status_checks_policy": false,
                  "required_status_checks": [{"context": "CI passed"}]}},
  {"type": "pull_request",
   "parameters": {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": true,
                  "require_code_owner_review": false, "require_last_push_approval": false,
                  "required_review_thread_resolution": false}}]}
JSON
```

The environment rule turns every deploy into an approval click. On the review
count: GitHub does not let an author approve their own pull request, so with a
single maintainer a count of 1 blocks every merge — set it to `0` (the
`CI passed` requirement still holds) or add the owner as a bypass actor.
Confirm with `gh api repos/namanjain221995/personal-LLM-Chabot/rulesets` and
`gh api repos/namanjain221995/personal-LLM-Chabot/environments/production`.

### OA-6 — The worker's OCR and speech engines off the office LAN (F043, F051, N016)

Both are host-network on the worker (`:30004` OCR, `:30007` speech) and bound to
its management address. `scripts/ocr.sh` and `scripts/whisper.sh` still derive
that address, so the no-code remedy is a filter on the **worker** accepting only
the head:

```bash
HEAD_LAN=192.168.9.54        # the head's management address at audit time; confirm with `ip -4 addr` on the head
sudo nft add table inet techsara_guard
sudo nft 'add chain inet techsara_guard input { type filter hook input priority -10 ; policy accept ; }'
sudo nft add rule inet techsara_guard input iifname "lo" tcp dport '{ 30004, 30007 }' accept
sudo nft add rule inet techsara_guard input ip saddr "{ ${HEAD_LAN}, 10.100.184.0/24, 10.100.185.0/24 }" tcp dport '{ 30004, 30007 }' accept
sudo nft add rule inet techsara_guard input tcp dport '{ 30004, 30007 }' drop
```

Confirm with `scripts/ocr.sh verify` and `scripts/whisper.sh verify` from the
head, one real image through chat (`/health` cannot see a degenerate OCR
answer), and a refused connect from another LAN host. The durable fix — bind to
`CLUSTER_WORKER_IP` and rewrite `OCR_BASE_URL`, `OCR_REMOTE_BASE_URL` and the
speech URL in lockstep, which also takes user images off the 1 GbE LAN (N016) —
is deferred to the infrastructure owner.

### OA-7 — Portainer (F052)

An unmanaged container publishing `0.0.0.0:9000` with the Docker socket
mounted read-write. If nobody needs it: `docker rm -f portainer`. If it stays,
recreate it bound to loopback and reach it over Tailscale:
`docker run -d --name portainer --restart unless-stopped -p 127.0.0.1:9000:9000 -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:<pinned version>`,
and change its admin password. Confirm: `ss -ltnH 'sport = :9000'` shows only
`127.0.0.1`.

### OA-8 — The orchestrator port and forwarded-address trust (F027, F053, F058, N026, N020; limits F014, N015)

While `:8080` is reachable from the LAN and `AUTH_TRUST_PROXY_HEADERS=true`, a
LAN host chooses its own client address: it can evade the per-address login
lockout, forge audit and session addresses, **pass a project's API
`ip_allowlist`**, and write any `last_used_ip`. It can also read `/health` and
`/metrics`.

* If the app does not need to be reachable on the LAN (N020 is that decision):
  set `TECHSARA_BIND_ADDRESS=127.0.0.1` in `.env` and let the next deploy
  recreate the orchestrator and frontend. The tunnel reaches the frontend over
  the Docker network and is unaffected. (This variable also binds the frontend's
  `:3000`.)
* If the app must stay on the LAN, filter `:8080` only. It is a Docker-published
  port, so the rule belongs in `DOCKER-USER`, not the host input chain:

  ```bash
  sudo iptables -I DOCKER-USER -i enP7s7 -p tcp -m conntrack --ctorigdstport 8080 --ctdir ORIGINAL -j DROP
  sudo iptables -I DOCKER-USER -i tailscale0 -p tcp -m conntrack --ctorigdstport 8080 --ctdir ORIGINAL -j DROP
  ```

  (`enP7s7` is the head's management interface named in the audit; confirm with
  `ip -br link`.) Undo with the same lines using `-D` in place of `-I`.

Confirm from a LAN host: `curl -m 5 http://192.168.9.54:8080/health` fails to
connect, while `curl -s http://127.0.0.1:8080/health` on the box still answers.
The legacy `docker-compose.yml` publishes `"8080:8080"` on every interface; do
not use it on this host.

### OA-9 — Revoke invitations issued before the F028 fix

Defence in depth only. Both the issuing and the accepting side now refuse an
invitation that would claim an account the inviter does not outrank, so an
old invitation can no longer take an account over. Revoking the stale ones
still tidies the list. Pending invitations addressed to an existing account
(read-only):

```sql
SELECT i.id, i.email, i.role, i.invited_by, i.created_at, i.expires_at, u.id AS existing_user_id, u.status
  FROM workspace_invitations i
  JOIN users u ON lower(u.email) = lower(i.email)
 WHERE i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
```

Revoke each one that is not a deliberate re-onboarding from Admin →
Invitations (or `POST /admin/api/invitations/{id}/revoke`). Invitations expire
after `AUTH_INVITATION_TTL_DAYS` (7), so this is needed once, right after the
release.

### OA-10 — Delete the stale `DEPLOY_BRANCH` variable (F075)

```bash
gh variable delete DEPLOY_BRANCH --repo namanjain221995/personal-LLM-Chabot
gh variable list --repo namanjain221995/personal-LLM-Chabot     # DEPLOY_BRANCH must be gone
```

The pipeline derives the branch from the deployed ref
(`DEPLOY_BRANCH: ${{ inputs.branch || github.ref_name }}`), so this changes
nothing at runtime. The README table was corrected in this branch.

### OA-11 — Retention for the tables the platform sweep does not cover (F040)

The developer platform's own tables are pruned automatically: the orchestrator
lifespan runs `db.prune_api_platform` 60 s after start and then every
`ARTIFACT_MAINTENANCE_INTERVAL_S`, together with the rotated-key sweep, and the
loop survives any exception. What remains is a policy decision, not a command:
`usage_events`, `query_traces` and `chat_requests` still have no retention at
all and grow without bound. Choose a retention window for each and schedule it
the same way; until then, watch their size.

### OA-12 — Recreate node-exporter on the head (F056)

The compose default is now `172.17.0.1:9100`. Recreate the monitoring stack the
way it is normally started (`./techsara up`), not with a hand-picked subset of
`-f` files — a partial chain silently changes other services. Confirm
`ss -ltnH 'sport = :9100'` shows `172.17.0.1` and the `node` target in
Prometheus is UP. On a host whose Docker bridge is not `172.17.0.1`, set
`MONITORING_NODE_BIND` to its gateway first.

### OA-13 — Put the tunnel ingress under version control (F059)

Migrate the tunnel to a locally managed configuration: create a credentials
file for the tunnel, commit `compose/cloudflared/config.yml` with the ingress
(`ai.techsarasolutions.com` → `http://frontend:3000`, catch-all
`http_status:404`), mount it read-only, and run
`tunnel --no-autoupdate --config /etc/cloudflared/config.yml run <tunnel-id>`,
keeping only the credentials JSON in `.runtime/`. Until then, any dashboard
edit can publish an internal service with no review.

### OA-14 — Grafana's session cookie (F060)

Set `GRAFANA_COOKIE_SECURE=true` in `.env` and recreate Grafana through the
launcher. Confirm the `grafana_session` cookie carries `Secure` over the public
hostname.

### OA-15 — The unmanaged container holding the data volume (F062)

`docker inspect zealous_williamson` showed an `alpine` container started by
hand with `sf-local-ai_data` mounted. Find out who started it; if nobody needs
it, `docker rm -f zealous_williamson`.

### OA-16 — The torch master port and the stray iperf3 server (F063)

Find the iperf3 server with `ss -ltnp 'sport = :5201'`, stop it (the audit saw
pid 2308), and remove whatever starts it. The master port `29501` has no bind
flag; restrict it to the RoCE rails on the head:

```bash
sudo nft add rule inet techsara_guard input ip saddr '{ 10.100.184.0/24, 10.100.185.0/24 }' tcp dport 29501 accept
sudo nft add rule inet techsara_guard input iifname "lo" tcp dport 29501 accept
sudo nft add rule inet techsara_guard input tcp dport 29501 drop
```

(Uses the table from OA-4.) Confirm the pair still forms after the next engine
restart before persisting.

### OA-17 — `litellm-dgx` (N018)

A second OpenAI gateway to the main model, outside the repository, with its
master key in a world-readable file. If it is not needed:
`docker rm -f litellm-dgx` and delete `/home/techsphere/litellm-dgx/`. If it is:
`chmod 600 /home/techsphere/litellm-dgx/config.yaml`, move the master key into
`.runtime/secrets.env`, pin the image by digest and bring it into compose.
Every call through it bypasses the developer platform's keys, quotas and
records.

### OA-18 — HTTPS only, with HSTS, at the Cloudflare edge (N019)

Cloudflare dashboard → the zone → SSL/TLS → Edge Certificates → **Always Use
HTTPS** on; **HTTP Strict Transport Security** on, starting with a short
`max-age` and raising it once confident. Confirm
`curl -sI http://ai.techsarasolutions.com/` answers `301` to `https://` (this
box resolves the name AAAA-only; use `--resolve` or another machine).

### OA-19 — Tell the frontend which client-address header to trust (F002, new with this release)

The BFF no longer copies `cf-connecting-ip` from any request. Without the
setting below, the orchestrator records the frontend container's address for
every sign-in, session and audit event, and the per-address login lockout
treats every internet visitor as one address. Add to the `frontend` service's
`environment:` in `compose.yaml` (a variable in `.env` does not reach that
container):

```yaml
      TRUSTED_CLIENT_IP_HEADER: cf-connecting-ip
      TRUSTED_FORWARDED_PROTO: https
```

Apply it **with** the release. Confirm: sign in through the public hostname and
check that the new session in Settings → Sessions shows your own address.

### OA-20 — Production CORS origins (F031)

Set `CORS_ALLOW_ORIGINS=https://ai.techsarasolutions.com` in the production
`.env` (keep the localhost entries in `.env.example` and local overrides), and
let the next deploy recreate the orchestrator.

### OA-21 — Keep `MOCK_MODE` off in production (F007)

`compose.yaml` passes `MOCK_MODE: ${MOCK_MODE:-false}` to the frontend, and mock
mode signs every visitor in. Confirm `grep -n MOCK_MODE .env .runtime/*.env`
shows nothing or `false`, and
`docker exec sf-local-ai-frontend-1 printenv MOCK_MODE` prints `false`.

### OA-22 — Tell `/v1` which proxy's forwarded address to believe (F027, new with this release)

`/v1` now believes `X-Forwarded-For` only from a peer listed in
`PUBLIC_API_TRUSTED_PROXIES`. Every public API request arrives from the
frontend container, so with the setting empty a project's `ip_allowlist` sees
the container's address, and `api_keys.last_used_ip` records it. Find the
frontend's network and set it for the orchestrator (for example in
`.runtime/secrets.env` or `.env`, both of which reach that container):

```bash
docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' sf-local-ai_application   # 172.18.0.0/16 on 2026-09-13
echo 'PUBLIC_API_TRUSTED_PROXIES=172.18.0.0/16' >> .env
```

Apply with the release. Combine with OA-19, so the frontend puts the real client
address into `X-Forwarded-For` in the first place. Confirm by calling `/v1/models`
through the public hostname with a key and reading that key's `last_used_ip` in
the console.

---

## Deferred work, by proposed owner

| proposed owner | findings |
|---|---|
| CI/CD | F066, N021, N022, F073, N024; the remainders of F065 and N023 (a live bind assertion in `verify`) and F074 (a dispatch-only rollback job); the dispatch job of OA-2 |
| Orchestrator wiring (`app/main.py`) | F017, F021, F022, F014, N015; remainders of F023 (chat routes) and F030(a); a start-up sweep for orphaned background responses (F025) |
| Database | F035, F038, F039, N011; remainders of F036, F040 (`usage_events`, `query_traces`, `chat_requests` retention), F041, N009 |
| Frontend BFF hardening | F079, F009, F011, N001; remainders of F006 and F030(b) |
| admin frontend | F077, F008, F080 |
| authn owner | peer-conditional forwarded-header trust for sessions, audit and login lockout (F027, F058, N026); the accept side of F028 |
| infrastructure | N017, F054, F057, F061; the durable part of F043/F051/N016; `TRUSTED_CLIENT_IP_HEADER` and `PUBLIC_API_TRUSTED_PROXIES` in `compose.yaml` |
| Webhooks | a route that rotates a webhook signing secret with an overlap |
| memory owner | the remainder of N008 |
| Public API surface | the open contract disagreements in API.md §12 |

## Gaps found during verification

Not in `AUDIT.md`. Each was found by reading the tree while checking a row
above and is reported to the programme. Status at the 02:50 IST re-read.

| # | gap | status | evidence |
|---|---|---|---|
| G1 | The developer console's API router was not included by `app/main.py`; its own suite mounted it in a fixture, so the tests passed against routes the running app did not have. | **closed** — `main.CONSOLE_API_MOUNTED` is true; an unauthenticated `GET /admin/api/developers/overview` answers 401 (probe) | `app/main.py` |
| G2 | The console BFF forwarded to `/admin/api/devplatform`; the router's prefix is `/admin/api/developers`. | **closed** — `CONSOLE_UPSTREAM_BASE = '/admin/api/developers'` | `frontend/app/api/devplatform/[...path]/route.ts` |
| G3 | A webhook signing secret was generated and never returned, while the console dialog rendered `created.secret`. | **partly closed** — the create response now carries the secret once; there is still no route that rotates it, though the dialog tells users to rotate a lost one | `apiplatform/console_api.py` `create_webhook` |
| G4 | `db.prune_api_platform` had no caller. | **closed** — scheduled by the lifespan (`_api_platform_prune_loop`) | `app/main.py` |
| G5 | `background.reconcile_interrupted` has no caller. | **partly closed** — an orphaned background row is repaired when read (`repair_if_orphaned`); no start-up sweep | `publicapi/router.py` `get_response` |
| G6 | A concurrency 429 left a `queued` row and poisoned its `Idempotency-Key`. | **closed** — the slot is taken before the row is written and `_nothing_ran` releases the claim | `publicapi/router.py` `_generate`, `_SlotStream` |
| G7 | `TRUSTED_CLIENT_IP_HEADER` (frontend) and `PUBLIC_API_TRUSTED_PROXIES` (orchestrator) are required for correct client addresses after this release and are set in no compose file. | **open** — OA-19, OA-22 | `grep -rn` across `compose.yaml`, `compose/`, `.env.example` returns nothing |
| G8 | None of the platform's settings (`API_KEY_PEPPER`, `PUBLIC_API_*`, `ORCHESTRATOR_DEV_DOCS`, `MAX_REQUEST_BODY_BYTES`, `TRUSTED_*`) is in `.env.example`; `webhook_worker_enabled`, `dev_docs_enabled` and `max_request_body_bytes` are read through `getattr` from a `Settings` that does not define them. | **open** | `.env.example`, `app/config.py` |
| G9 | Console key rotation revoked the old key with no overlap. | **closed** — `overlap_hours` (default 168, at most 720) | `apiplatform/console_api.py` `rotate_key` |
| G10 | The code disagrees with CONTRACT.md. | **12 open, 7 closed** — listed in `API.md` §12 | `API.md` §12 |

## What was run to write this

Real commands, real output, 2026-09-13.

```text
$ python .github/workflows/scripts/api_contract.py --orchestrator orchestrator \
    --expected .github/workflows/scripts/public-api-surface.txt
public OpenAPI document generated by app.publicapi.openapi:public_openapi
OpenAPI 3.1.0: well-formed, 8 operation(s), all matching .github/workflows/scripts/public-api-surface.txt
  ok  GET /v1/models
  ok  GET /v1/models/{model}
  ok  GET /v1/openapi.json
  ok  GET /v1/responses/{id}
  ok  GET /v1/usage
  ok  POST /v1/chat/completions
  ok  POST /v1/responses
  ok  POST /v1/responses/{id}/cancel

$ python .github/workflows/scripts/workflow_policy.py --dir .github/workflows --default-branch main
checked 1 workflow file(s): pipeline.yml
workflow policy: OK (P1-P8)
exit=0
```

Import probe of `app.main` with `TestClient` (no lifespan, no database), summarised — first run about 02:00 IST, re-run at 02:49 IST:

```text
first run (02:00):
PUBLIC_API_MOUNTED = True
included router prefixes: /auth /history /uploads /audio /video /artifacts /memory (share) /admin/api /admin/api/analytics /admin/api/shares /v1
GET /v1/does-not-exist            -> 405 {"detail":"Method Not Allowed"}, no X-Request-Id
POST /v1/responses 2 MiB          -> 413 request_too_large envelope, request_id null, no X-Request-Id

re-run (02:49):
PUBLIC_API_MOUNTED True CONSOLE_API_MOUNTED True
GET /v1/models no auth            -> 401 invalid_api_key, X-Request-Id present
GET /v1/models cookie only        -> 401 invalid_api_key (cookie ignored)
OPTIONS /v1/responses (Origin)    -> 204, Allow-Origin echoed, no Allow-Credentials
GET /v1/does-not-exist            -> 404 invalid_request_error envelope, X-Request-Id present
DELETE /v1/models                 -> 405 envelope, Allow: GET
POST /v1/responses 2 MiB          -> 413 request_too_large envelope, X-Request-Id present
GET /docs                         -> 404
GET /admin/api/developers/overview -> 401 {"detail":"Sign in required."}
GET /v1/openapi.json              -> 200, openapi 3.1.0, 8 paths
```

Not run: the orchestrator and frontend test suites, `scripts/devapi_smoke.py`,
anything against production.
