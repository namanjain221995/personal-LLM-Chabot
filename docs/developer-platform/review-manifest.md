# Review manifest

The 300 first-party files read in the audit of 2026-09-12, with the depth
each was read at. `interface-only` means the file's exported surface and call
sites were read rather than its whole body, and the reason is given.

## Justified exclusions

Not reviewed line by line, deliberately: `node_modules/`, `.git/`, `.venv/`,
`__pycache__/`, build caches, model weights under `~/Documents/project/Model`,
PostgreSQL and LanceDB data volumes, `backups/`, `benchmarks/` outputs,
`screenshots/`, generated artifacts and user uploads under `data/`, and the
`.runtime/` incident and drill records. Their interfaces, permissions, mounts
and pinned versions were inspected where they bear on security; their contents
were not. `docs/` prose outside the programme's own area was read where it
described behaviour under review, not exhaustively.

| file | area | purpose | boundary | depth |
|---|---|---|---|---|
| `compose.yaml` | admin-usage | Canonical compose: orchestrator publishes ${TECHSARA_BIND_ADDRESS:-127.0.0.1}:8080 | internal-only | interface-only - grepped the port lines |
| `docker-compose.yml` | admin-usage | Legacy compose: orchestrator publishes "8080:8080" with no bind address (all interfaces) | public | interface-only - grepped the port lines |
| `frontend/app/admin/analytics/leaderboards/page.tsx` | admin-usage | People and Models boards, server-side paging, AdminTabs driven by a ?tab query param | admin (super admin only) | skimmed - read the imports, PeopleBoard wiring and the tab component at the bott |
| `frontend/app/admin/analytics/page.tsx` | admin-usage | The analytics console front page (Usage): sections, stat rows, chart, right-hand leaderboard rail | admin (super admin only via analytics.read) | skimmed - read the imports and the first 110 lines that establish the page compo |
| `frontend/app/admin/audit/page.tsx` | admin-usage | /admin/audit - keyset-paginated audit trail with an action filter, rendered through AdminTable | admin (audit.read, super admin only) | full - the page API key create/rotate/revoke events will surface in |
| `frontend/app/admin/layout.tsx` | admin-usage | The /admin shell: client-side gate on members.read, the hardcoded nav group definition, desktop rail + mobile  | authenticated (client courtesy gate only; server 404s without the capability) | full - it is the single definition of admin navigation and the page gate the Dev |
| `frontend/app/admin/members/page.tsx` | admin-usage | Members roster: AdminToolbar + search + two selects + AdminTable + Pagination + RowMenu + dialogs + toasts | authenticated | skimmed - read the row-menu builder, the toolbar block and the table/pagination  |
| `frontend/app/admin/page.tsx` | admin-usage | /admin Overview: two tabs (Overview / People) over the legacy /admin/api/analytics payload, StatTile row, Usag | authenticated | skimmed - read the header, data fetch and the table column definitions |
| `frontend/app/api/admin/[...path]/route.ts` | admin-usage | The catch-all Next proxy to /admin/api/*: re-encodes segments, streams the two download paths locally, forward | authenticated (authorization is entirely upstream) | full - every Developer console call will ride this |
| `frontend/app/globals.css` | admin-usage | Design tokens; the five --admin-* custom properties the admin area adds | none | skimmed - read the type scale, the admin token block (lines 120-175) and the lig |
| `frontend/components/ConfirmDialog.tsx` | admin-usage | The destructive-action confirmation modal (portalled, alertdialog, Escape cancels, focus on Cancel) | none | skimmed - read the props and the portal/focus mechanics |
| `frontend/components/CopyButton.tsx` | admin-usage | Clipboard button with a 1.6s confirmation and a non-secure-context fallback | none | skimmed - read props and the copy path |
| `frontend/components/EChart.tsx` | admin-usage | ECharts renderer isolated behind next/dynamic ssr:false, registering only five chart types | none | interface-only - read the header and the echarts.use registration list |
| `frontend/components/Providers.tsx` | admin-usage | Theme + toast context for the whole app; useToast() -> {toast(text, tone?)} with dedup by tone+text | none | skimmed - read the context definition, the exported hooks and the toast stack re |
| `frontend/components/admin/AdminDialog.tsx` | admin-usage | Portalled modal shell + Field, FIELD_INPUT, PRIMARY_BUTTON, SECONDARY_BUTTON constants | none | full - the dialog the create/rotate/revoke key flows should reuse |
| `frontend/components/admin/AdminMeContext.tsx` | admin-usage | Provider + useAdminMe() so pages read the resolved ME_PAYLOAD without re-fetching | authenticated | full |
| `frontend/components/admin/AdminTable.tsx` | admin-usage | The flat settings table (AdminColumn<T>, fixed vs auto layout, skeleton/empty/error states) and the offset Pag | none | full - the table component the API-keys list should reuse |
| `frontend/components/admin/FeatureToggles.tsx` | admin-usage | AccessSettingRow / AccessSettingsCard - the settings-list grid shared by the Access page and the per-member di | none | skimmed - read the row component and its grid rationale; the scope-picker analog |
| `frontend/components/admin/InviteDialog.tsx` | admin-usage | Create an invitation and reveal the one-time token link with CopyButton and a 'shown once' warning | authenticated | full - this is the exact show-once-secret pattern an API key create dialog shoul |
| `frontend/components/admin/RowMenu.tsx` | admin-usage | Per-row portalled overflow menu (RowMenuItem {id,label,icon,danger}) | none | full - the rotate/revoke row actions should use it |
| `frontend/components/admin/Switch.tsx` | admin-usage | The one admin switch: button role=switch, 44x24, aria-checked | none | full |
| `frontend/components/admin/analytics/AnalyticsChart.tsx` | admin-usage | The console's single chart component over ECharts (Series[], one y-axis, dashed horizontal grid, shared toolti | none | full - the chart component for API request/token time series |
| `frontend/components/admin/analytics/filters.tsx` | admin-usage | useQueryState, useRange, RangePicker, ModelPicker, ConsoleHeader - filter state lives in the query string | none | full - the range picker and console page header to reuse |
| `frontend/components/admin/analytics/format.ts` | admin-usage | compact/exact/duration/ratio/percent/bytes/uptime/hertz/rate/bucketLabel/updatedAt/initialOf, NOT_MEASURED em  | none | full |
| `frontend/components/admin/analytics/types.ts` | admin-usage | The console's wire contract: RANGES, Window, Coverage, Totals, MemberUsage, UsagePoint, RouteUsage, ModelUsage | none | skimmed - read the range/window/totals/member/overview/leaderboard/Infra section |
| `frontend/components/admin/analytics/ui.tsx` | admin-usage | Console furniture: Section, Delta, Stat, StatRow, ChartFrame, TelemetryUnavailable, CoverageNote, RailPanel, R | none | full - the empty/loading/error state vocabulary a Developer usage page must reus |
| `frontend/components/admin/analytics/useAnalytics.ts` | admin-usage | useAnalytics<T>(path, params) -> {data,loading,error,reload} with abort-the-previous-request semantics; useDeb | authenticated | full - the data-loading hook for Developer console pages |
| `frontend/components/admin/api.ts` | admin-usage | The admin fetch/auth helper: Me types, parseMe, can(), adminJson/adminPost/adminPut, AdminApiError, OFFLINE_ME | authenticated | full - this is the fetch/auth helper the Developer console must reuse |
| `frontend/components/admin/chips.tsx` | admin-usage | RoleChip and StatusChip (dot + word, never colour alone) | none | full |
| `frontend/components/admin/controls.tsx` | admin-usage | Toolbar vocabulary: CONTROL_HEIGHT, ADMIN_PRIMARY_BUTTON, ADMIN_SECONDARY_BUTTON, AdminSearchInput, AdminSelec | none | full - the form/filter controls the Developer console must reuse |
| `frontend/components/admin/icons.tsx` | admin-usage | 23 admin-only 24px-grid icons including IconKey, IconShield, IconServer | none | interface-only - read the header, the base() helper and the full export list |
| `frontend/components/admin/nav.ts` | admin-usage | A 14-line testable wrapper around window.location.assign. NOT a nav registry despite the filename | none | full |
| `frontend/components/admin/ui.tsx` | admin-usage | SkeletonLine, StatTile, ErrorPanel, PageHeader, AvatarInitial | none | full |
| `frontend/components/admin/useDebounced.ts` | admin-usage | useDebounced<T>(value, ms=300) for the search boxes | none | full |
| `frontend/lib/auth.ts` | admin-usage | SESSION_COOKIE, fetchMe, authRedirect (the pure middleware decision), handleSessionEnd, logout, PUBLIC_PAGES/P | public (authRedirect governs which pages are reachable signed out) | full - decides whether /api and /docs pages are gated |
| `frontend/lib/chartTheme.ts` | admin-usage | Resolves --ts-chart-1..5 and chrome colours from computed style for the canvas renderer, with literal fallback | none | skimmed - read the palette contract and the fallback rationale |
| `frontend/lib/format.ts` | admin-usage | formatBytes, formatWhen, formatDay, formatRelative, fileKind | none | interface-only - export list only |
| `frontend/lib/proxy.ts` | admin-usage | orchestratorUrl() and proxyToOrchestrator(): forwards cookie/content-type/x-forwarded-for/x-forwarded-proto/us | internal-only (server-side route handler) | full - a blocker for SSE and for API-key auth on the new surface |
| `frontend/middleware.ts` | admin-usage | Edge page gate; matcher excludes anything starting with 'api', '_next' or containing a dot | public | full |
| `frontend/next.config.mjs` | admin-usage | standalone output, four security headers, share-page noindex headers. No rewrites or redirects | public | full |
| `frontend/tailwind.config.ts` | admin-usage | Semantic colour/type/radius/duration tokens mapped onto CSS variables; max-w-admin 1180px | none | full |
| `orchestrator/app/admission.py` | admin-usage | Two-lane admission control in front of the main model (NORMAL semaphore, LONG one-at-a-time) | internal-only | interface-only - read the module contract header |
| `orchestrator/app/analytics/__init__.py` | admin-usage | Read-side aggregations over usage_events/messages/research_runs/web_searches/sf_intents/voice_transcriptions;  | internal-only | skimmed - read the module contract, totals(), the series preamble and leaderboar |
| `orchestrator/app/analytics/infra.py` | admin-usage | Prometheus reader for the infrastructure blocks; returns {available:false, reason, source} instead of raising | internal-only | interface-only - read the module header and the PROM_URL/TIMEOUT_S constants |
| `orchestrator/app/authn/admin_api.py` | admin-usage | /admin/api/* - overview, members, roles, status, sessions, password reset, content viewer, invitations, featur | admin (per-route capability, 404 on refusal) | full - read the header, every route signature, and the invitation/export/audit/d |
| `orchestrator/app/authn/analytics_api.py` | admin-usage | /admin/api/analytics/* - 12 routes, all behind one Gate = Depends(require_capability(Cap.ANALYTICS_READ)); Win | admin (super admin only) | skimmed - read the header, RANGES/ORDER_PATTERN/Gate, the Window class and the l |
| `orchestrator/app/authn/api.py` | admin-usage | /auth/* - login (with throttle + login_success/login_failure audit), logout, me, sessions, invitation accept;  | public (login) / authenticated (the rest) | skimmed - read the login audit block and _token_hash/_live_invitation in full |
| `orchestrator/app/authn/bootstrap.py` | admin-usage | First-run super admin bootstrap; writes the super_admin_bootstrapped audit event | internal-only | interface-only - read the record_audit call |
| `orchestrator/app/authn/features.py` | admin-usage | Feature enum, FeatureSpec registry, defaults/clean/resolve/allowed, ChatGate - the per-tool access layer disti | internal-only | skimmed - read the enum, the full FEATURES registry and resolve() |
| `orchestrator/app/authn/principal.py` | admin-usage | Principal dataclass, resolve/require_principal, require_capability(cap) (401/404), and audit() - the never-rai | internal-only | full - both the gate factory and the audit writer the build must reuse |
| `orchestrator/app/authn/rbac.py` | admin-usage | Role, Cap, ROLE_CAPS, capabilities(), can(), outranks(), assignable_roles() - the one place authorization sema | internal-only | full - the Developer Platform must add its Cap here |
| `orchestrator/app/authn/sessions.py` | admin-usage | Opaque server-side sessions, cookie flags (HttpOnly, SameSite=Lax, secure auto), client_meta() for audit IP/UA | internal-only | skimmed - read client_meta and the cookie-setting block |
| `orchestrator/app/authn/shares_api.py` | admin-usage | /admin/api/shares - share governance behind Cap.SHARES_MANAGE, two audit actions | admin (super admin only) | interface-only - grepped routes, the Gate and the audit call sites |
| `orchestrator/app/authn/store.py` | admin-usage | The data layer: members, invitations, sessions, login throttling, record_audit(), list_audit_events(), usage_b | internal-only | skimmed - read the audit and throttle sections and usage_by_member in full; grep |
| `orchestrator/app/db.py` | admin-usage | Schema and migrations: audit_events DDL (692-707), usage_events DDL (959-1007), _MIGRATIONS tuple to V33, LATE | internal-only | skimmed - read the two table DDLs, the migrations tuple and the V31/V32/V33 head |
| `orchestrator/app/main.py` | admin-usage | App assembly: FastAPI(), CORS, the cross-site-write middleware, router includes, _record_usage_event and its c | public (the app object; /docs and /openapi.json are unauthenticated) | skimmed - read the app/middleware/router block (190-290), _record_usage_event (4 |
| `orchestrator/app/usage.py` | admin-usage | usage.record() / record_async() - the only writer of usage_events; idempotent per generation_id, never raises | internal-only | full - the telemetry writer the Developer API must extend |
| `compose.yaml` | authn-authz | Portable compose; the correctly-bound variant | internal-only | interface-only — line 233 |
| `docker-compose.yml` | authn-authz | Production compose; orchestrator port publication | internal-only | interface-only — grep for 8080, line 555 |
| `docs/01-codebase/orchestrator-context.md` | authn-authz | Pre-retrofit auth.py description — stale | none | interface-only — lines 1080-1095 |
| `docs/01-codebase/security-model.md` | authn-authz | Pre-retrofit security snapshot — now stale and contradicts the live code | none | interface-only — lines 75-95 |
| `docs/AUTH.md` | authn-authz | The auth reference document | none | full — 353 lines |
| `docs/developer-platform/BASELINE.md` | authn-authz | The programme's recorded baseline (git, CI, tests) | none | full — 56 lines |
| `frontend/app/admin/layout.tsx` | authn-authz | Admin nav built from ME_PAYLOAD.capabilities | none (client) | interface-only — lines 140-200 |
| `frontend/app/api/admin/[...path]/route.ts` | authn-authz | Thin admin passthrough; authorization is entirely upstream | admin (delegated) | full — 138 lines |
| `frontend/app/api/auth/invitations/[token]/route.ts` | authn-authz | Invitation preview proxy — token travels in the URL path | public | full — 25 lines |
| `frontend/app/api/auth/invitations/accept/route.ts` | authn-authz | Invitation acceptance proxy (auto-login) | public | full — 19 lines |
| `frontend/app/api/auth/login/route.ts` | authn-authz | Login proxy | public | full — 18 lines |
| `frontend/app/api/auth/logout/route.ts` | authn-authz | Logout proxy | public | full — 17 lines |
| `frontend/app/api/auth/me/route.ts` | authn-authz | Session probe proxy | public | full — 21 lines |
| `frontend/components/admin/api.ts` | authn-authz | Client-side can()/invitableRoles()/assignableRoles() — UI courtesy only | none (client) | interface-only — lines 60-90 |
| `frontend/lib/auth.ts` | authn-authz | fetchMe, authRedirect, session-end routing, logout, capability list parsing | public | full — 370 lines |
| `frontend/lib/proxy.ts` | authn-authz | Server-side proxy to the orchestrator; builds a fresh header set (drops Origin) | internal-only (server component) | full — 78 lines |
| `frontend/middleware.ts` | authn-authz | Edge page gating on cookie presence | public | full — 36 lines |
| `orchestrator/Dockerfile` | authn-authz | uvicorn CMD — confirms no --no-access-log, so request lines are logged | internal-only | interface-only — lines 50-77 |
| `orchestrator/app/artifacts/api.py` | authn-authz | Artifact Studio routes — spot-checked for owner scoping | authenticated | interface-only — all 11 routes enumerated; lines 160-270 read |
| `orchestrator/app/audio_api.py` | authn-authz | Speech-to-text; one route gated on Cap.ANALYTICS_READ | authenticated + admin | interface-only — 2 routes enumerated |
| `orchestrator/app/auth.py` | authn-authz | Compatibility surface: UserRow, current_user, require_user, SESSION_COOKIE constant | internal-only | full — 47 lines |
| `orchestrator/app/authn/admin_api.py` | authn-authz | /admin/api/* — members, roles, status, sessions, password reset, audited content inspection, invitations, feat | admin (capability-gated, 404 on refusal) | full for every route signature (all 25 enumerated with their gate) plus lines 1- |
| `orchestrator/app/authn/analytics_api.py` | authn-authz | Super-admin analytics console; the canonical `Gate` super-admin-only declaration | admin (super-admin only) | interface-only — docstring, Gate definition, one route signature; the 10 routes  |
| `orchestrator/app/authn/api.py` | authn-authz | /auth surface: login, logout, me, password, sessions, preferences, invitations | public (login/logout/invitations) + authenticated (rest) | full — 436 lines |
| `orchestrator/app/authn/bootstrap.py` | authn-authz | First SUPER_ADMIN bootstrap CLI + ensure_identity_baseline on lifespan | internal-only (shell/container access) | full — 190 lines |
| `orchestrator/app/authn/features.py` | authn-authz | Feature (tool-access) enum and the workspace/member resolution layers; enforce_chat downgrade gate | internal-only | full — 292 lines |
| `orchestrator/app/authn/passwords.py` | authn-authz | Argon2id hashing, needs_rehash, timing-equalized verify, password policy | internal-only | full — 94 lines |
| `orchestrator/app/authn/principal.py` | authn-authz | Principal dataclass, per-request resolution + caching, require_principal, require_capability factory, audit() | internal-only | full — 175 lines |
| `orchestrator/app/authn/rbac.py` | authn-authz | The single authorization table: Role enum, Cap enum, ROLE_CAPS, outranks(), assignable_roles() | internal-only | full — 119 lines, this is the file a new capability must be edited into |
| `orchestrator/app/authn/sessions.py` | authn-authz | Session mint/resolve/explain, cookie set/clear and flags, client_meta (ip/user-agent) | internal-only | full — 231 lines |
| `orchestrator/app/authn/shares_api.py` | authn-authz | Super-admin share governance; second `Gate` instance | admin (super-admin only) | interface-only — docstring, Gate, one route; 3 routes enumerated |
| `orchestrator/app/authn/store.py` | authn-authz | All SQL for users, memberships, sessions, invitations, audit, login throttle, report ownership, preferences | internal-only | skimmed — symbol map for all 68 functions, then read lines 32-190, 324-360, 425- |
| `orchestrator/app/config.py` | authn-authz | Settings; the AUTH_* and CORS_ALLOW_ORIGINS block | internal-only | interface-only — lines 1165-1190 and 1196-1232 |
| `orchestrator/app/db.py` | authn-authz | Schema and migrations; _MIGRATION_V12 creates the whole identity layer | internal-only | interface-only — lines 600-700 (V12 DDL) and 1833-1880 (migration tuple, LATEST_ |
| `orchestrator/app/history.py` | authn-authz | Conversation/message CRUD | authenticated | interface-only — 12 routes enumerated, all Depends(require_user) |
| `orchestrator/app/identity.py` | authn-authz | ContextVar carrying the display name/email/workspace into the model's system prompt | internal-only | full — 38 lines |
| `orchestrator/app/main.py` | authn-authz | FastAPI app, CORS + cross-site-write middleware, router mounting, /health, /metrics, /reports, /chat and the c | public (/health, /metrics) + authenticated (everything else) | skimmed — lines 218-256, 985-1100, 1579-1700, 3510-3620, 3640-3860 read in full; |
| `orchestrator/app/memory_api.py` | authn-authz | Memory fact CRUD | authenticated | interface-only — 3 routes enumerated, all Depends(require_user) |
| `orchestrator/app/share_api.py` | authn-authz | Conversation sharing; contains the only route that answers without a session | public (/public/shares/{token}) + authenticated | interface-only — all 6 routes enumerated; lines 165-230 and 455-507 read in full |
| `orchestrator/app/sharing.py` | authn-authz | public_id + hashed-secret bearer-token model; the reusable pattern for API keys | internal-only | interface-only — lines 40-100 |
| `orchestrator/app/uploads.py` | authn-authz | Upload + chunked-upload routes | authenticated | interface-only — 8 routes enumerated, all Depends(require_user) |
| `orchestrator/app/video/api.py` | authn-authz | Video analysis status | authenticated | interface-only — 1 route, Depends(require_user) |
| `orchestrator/tests/test_authn_idor.py` | authn-authz | IDOR suite — 10 owner-scoping tests including anonymous /chat is 401 | none | interface-only — test-name listing |
| `orchestrator/tests/test_authn_rbac.py` | authn-authz | RBAC gating and rank-rule tests — confirms 404-not-403 and last-super-admin protection | none | interface-only — lines 1-70 and 130-151 |
| `.github/workflows/actionlint.conf` | cicd | Declares the `dgx-spark` self-hosted label so actionlint fails on a runs-on typo; deliberately not named .yml | internal-only | full - 15 lines |
| `.github/workflows/gitleaks-baseline.json` | cicd | 15 reviewed secret-scan fingerprints (rule/file/line only, no values) | public | skimmed - parsed the JSON and printed the key shape and first three entries; ind |
| `.github/workflows/pipeline.yml` | cicd | The single CI/CD workflow: 12 jobs, validate -> test -> scan -> ci-ok gate -> self-hosted deploy -> verify ->  | public - repo is public and `on: pull_request` is unfiltered, so the PR's own copy of this file executes | full - all 1083 lines read in four passes, plus a YAML dump of every job's name/ |
| `.github/workflows/scripts/arm64_gate.py` | cicd | Refuses a Dockerfile whose base has no linux/arm64 manifest or that hardcodes an x86 artifact | internal-only | full - 146 lines |
| `.github/workflows/scripts/ci_gate.py` | cicd | The aggregate release gate: asserts every required JOB ID in toJSON(needs) reported exactly 'success' | internal-only | full - 131 lines, plus executed three times with crafted NEEDS_JSON to verify sk |
| `.github/workflows/scripts/ruff_gate.py` | cicd | Correctness-only ruff gate (E9,F63,F7,F82) with a two-way baseline dict that is currently empty | internal-only | full - 92 lines |
| `.github/workflows/scripts/schema_parity.py` | cicd | Proves a fresh install and a staged upgrade reach the same structural schema; `invariants` checks the migratio | internal-only | interface-only plus full read of cmd_invariants (lines 169-194) and the docstrin |
| `.github/workflows/scripts/secret_gate.py` | cicd | Turns a gitleaks report into a verdict; cross-checks exit code against report content; two-way fingerprint bas | internal-only | full - 151 lines |
| `.github/workflows/scripts/workflow_policy.py` | cicd | P1-P6 static policy on workflow files: SHA pins, no pull_request_target, self-hosted branch restriction, least | internal-only | full - 271 lines, plus executed against a crafted workflow to prove the P4 inver |
| `README.md` | cicd | Section 22.1 documents the pipeline, DEPLOY_ON_PUSH, DEPLOY_FULL and DEPLOY_BRANCH | public | skimmed - lines 955-1010 (the CI/CD section) only |
| `docs/developer-platform/BASELINE.md` | cicd | This programme's pre-change baseline; records the two green pipeline runs on 4e28fcc and quotes the `CI passed | internal-only | skimmed - lines 1-60 |
| `frontend/package.json` | cicd | Defines the lint/test/build scripts the frontend job runs | none | interface-only - scripts block and the vitest pin |
| `frontend/vitest.config.mts` | cicd | Frontend test entry point used by the `frontend` job's `npm test` | none | full - 34 lines, plus an empirical no-test-files run to confirm exit 1 |
| `launcher/techsara_cli/compose.py` | cicd | Compose wrapper; down() is what a --full deploy invokes | internal-only | interface-only - lines 161-173, to confirm no -v/--volumes on the deploy path |
| `scripts/deploy-rollback.sh` | cicd | Puts previously-recorded image IDs back and refuses when the code being rolled back to does not know an applie | admin | interface-only - header and argument parser (lines 1-55); called by nothing in t |
| `scripts/deploy-smoke.sh` | cicd | Post-rollout invariants (digest, schema, not-looping, model clock) that the verify job does not check | admin | interface-only - header lines 1-30; called by nothing |
| `scripts/deploy.sh` | cicd | What the deploy job actually runs: lock, fetch, fast-forward the production checkout, build+promote by digest, | admin | skimmed - full header (lines 1-80) plus a grep map of every flag, DEPLOY_BRANCH  |
| `scripts/e2e-stack.sh` | cicd | Isolated copy of the application tier on loopback 8081/3001 with its own DB and volumes - the natural target f | internal-only | interface-only - header lines 1-40 |
| `scripts/setup-runner.sh` | cicd | One-time registration of this box as the GitHub Actions runner; writes the ~/.config/systemd/user/github-runne | admin | full - 76 lines |
| `.github/workflows/pipeline.yml` | database | The single Pipeline workflow; the `policy` job runs migration invariants, the `schema` job stages at V20 and s | none | interface-only — read lines 175-178, 270-300, 349-430 and the job-name index |
| `.github/workflows/scripts/schema_parity.py` | database | CI proof that a fresh install equals an upgrade; also the `invariants` check on the _MIGRATIONS tuple | none | full |
| `docker-compose.yml` | database | postgres service: image, max_connections=60, initdb locale C, APP_DATABASE_URL for the orchestrator | internal-only | interface-only — grepped lines 372-401 and 519 |
| `docs/developer-platform/BASELINE.md` | database | The programme's own baseline doc this audit feeds | none | skimmed — read the first 60 lines |
| `orchestrator/app/analytics/__init__.py` | database | Read-side aggregates over usage_events, messages, research_runs, web_searches, sf_intents, voice_transcription | admin | skimmed — read the module docstring, _model_clause/_bucket_expr, totals(), and t |
| `orchestrator/app/analytics/infra.py` | database | Infrastructure telemetry for the console | admin | skimmed — confirmed it owns NO PostgreSQL tables (Prometheus HTTP only; grep for |
| `orchestrator/app/artifacts/db.py` | database | The V31 accessor layer — the house pattern for owner-scoped accessors, idempotency-key acceptance in one trans | authenticated | full for 1-300; interface-only (symbol outline) beyond |
| `orchestrator/app/authn/admin_api.py` | database | Admin surface; confirms every query is scoped to principal.workspace_id and the content viewer checks db.get_c | admin | skimmed — grepped every workspace_id use and read 402-420 |
| `orchestrator/app/authn/analytics_api.py` | database | Analytics console routes; confirms workspace scoping comes from the Principal | admin | interface-only — grepped every workspace_id use |
| `orchestrator/app/authn/principal.py` | database | Principal dataclass and the FastAPI dependencies; proves workspace_id/user_id are server-derived, never client | authenticated | full |
| `orchestrator/app/authn/store.py` | database | Identity SQL: users, workspaces, memberships, feature layers, auth_sessions, invitations, audit, login throttl | authenticated | full for 1-200 and 420-575 and 900-1000; interface-only (symbol outline) for the |
| `orchestrator/app/config.py` | database | APP_DB_POOL_MIN/MAX, APP_DB_POOL_TIMEOUT, APP_DB_STATEMENT_TIMEOUT_MS, APP_DB_STARTUP_TIMEOUT, WORKSPACE_NAME | internal-only | interface-only — read 1225-1250 |
| `orchestrator/app/db.py` | database | The whole app-state schema (33 migrations), init_schema, pool/connection handling, and every accessor for user | internal-only | full for lines 1-2400 (module docstring, every migration V1 header + V12 + V15-V |
| `orchestrator/app/engines/url.py` | database | The url_documents write path (db.save_url_document) and read path keyed by conversation id alone | authenticated | skimmed — read 225-250 and 285-330 |
| `orchestrator/app/health.py` | database | _expected_schema_version / _check_app_db, and the in-flight work counts over chat_requests/upload_sessions/vid | public | interface-only — read 310-380 and 510-545 |
| `orchestrator/app/history.py` | database | Conversation CRUD routes; conversation-id regex and the ownership-scoped accessors | authenticated | skimmed — read 40-200 and grepped every conversation_id use |
| `orchestrator/app/main.py` | database | /chat ownership + durable-intent path, /chat/attach, /chat/requests/{intent_id}, /chat/trace/{trace_id}, the c | authenticated | skimmed — read 1090-1200, 1660-1960, 2460-2470, 2590-2650, 3000-3150, 3500-3600, |
| `orchestrator/app/uploads.py` | database | _own/_owned — the claim-the-id pattern that closed the pre-seeding hole for explicit conversation ids | authenticated | interface-only — read 495-535 |
| `orchestrator/app/usage.py` | database | The only writer of usage_events; ON CONFLICT DO NOTHING idempotency per generation_id; never raises | internal-only | full |
| `orchestrator/conftest.py` | database | sys.path insertion so `app` imports | none | full |
| `orchestrator/tests/conftest.py` | database | Session test database, TEST_DATABASE_URL resolution and safety guard, per-test TRUNCATE of _APP_TABLES, ambien | none | full |
| `orchestrator/tests/test_crawl_durability.py` | database | The migration-mechanism tests: no duplicate identifiers, no version literal in an index predicate, fresh-vs-up | none | full for lines 735-915 |
| `scripts/deploy-db-rehearsal.sh` | database | Fresh / upgrade / restore rehearsal against a throwaway server from production's own postgres image id | internal-only | skimmed — read the 46-line header contract and the argument parsing |
| `.env.example` | edge-devops | Documents TECHSARA_BIND_ADDRESS / TECHSARA_MODEL_BIND_ADDRESS / the tunnel keys (names only read, no values) | none | interface-only — read lines 805-835 |
| `.github/workflows/pipeline.yml` | edge-devops | The single Pipeline workflow: job names, the ci-ok gate and the self-hosted deploy/verify/recovery jobs | internal-only | interface-only — read lines 60-110, 669-770; grepped every job name and runs-on |
| `compose.yaml` | edge-devops | Platform-neutral base: postgres, pgadmin, searxng, orchestrator, sync-worker, frontend; defines the applicatio | internal-only (defines the boundary itself) | full for services, ports, networks, env_file anchors; comment prose skimmed |
| `compose/compose.cloudflare.yaml` | edge-devops | The public edge: two cloudflared tunnels, profiles tunnel / tunnel-grafana | public | full |
| `compose/compose.cluster-dgx-spark.yaml` | edge-devops | Dual-node head overlay: switches vllm to host networking and binds CLUSTER_API_BIND_ADDRESS | public-on-LAN (host network, 0.0.0.0) | full for lines 1-60, grepped networking/ports/bind lines throughout |
| `compose/compose.cluster-worker.yaml` | edge-devops | Node 2 worker: vLLM rank 1 + vllm-worker-sentinel | internal-only (sentinel binds the RoCE point-to-point address, token-gated) | interface-only — grepped network_mode/ports/SENTINEL_*, read lines 150-192 |
| `compose/compose.dgx-spark.yaml` | edge-devops | DGX overlay: vllm, vllm-router, vllm-embed, vllm-reranker, vllm-ocr, engine-controller | internal-only (inference network) except engine-controller on host network | interface-only — grepped service names, ports/expose/networks, engine-controller |
| `compose/compose.external-development.yaml` | edge-devops | App-only / external-endpoint dev mode, adds host.docker.internal | none | full |
| `compose/compose.monitoring.yaml` | edge-devops | Prometheus, Grafana, node-exporter, cadvisor, postgres-exporter, blackbox, dgx-gpu-exporter, data-stores-expor | authenticated (Grafana) / internal-only (Prometheus, exporters) / public-on-LAN (node-exporter 9100) | interface-only — grepped every ports/GF_*/bind line; read node-exporter (125-160 |
| `compose/compose.ocr.yaml` | edge-devops | Unlimited-OCR vLLM engine, host networking, binds OCR_BIND in-process | public-on-LAN when placed on the worker | interface-only — read lines 41 and 60-100 |
| `compose/compose.published-cpu.yaml` | edge-devops | Same for llama-cpp on the CPU family | internal-only by intent | full |
| `compose/compose.published-dgx-spark.yaml` | edge-devops | Opt-in overlay that publishes the model APIs to the host | internal-only by intent; actually LAN-exposed for the main model | full |
| `compose/compose.published-nvidia.yaml` | edge-devops | Same for the generic NVIDIA family (vllm, vllm-embed) | internal-only by intent | full |
| `compose/compose.whisper.yaml` | edge-devops | Whisper ASR sidecar, host networking, binds WHISPER_BIND in-process | public-on-LAN when placed on the worker; bridge-gateway-only on the head | interface-only — read lines 33-65 |
| `docker-compose.yml` | edge-devops | Superseded pre-launcher DGX file, kept only as a stop/rollback path | none (not used by any running stack) | interface-only — read the header and grepped every ports/networks/privileged lin |
| `docs/AUTH.md` | edge-devops | Tunnel operations, the 100 MB edge cap, and the statement that SSE passes the edge | none | interface-only — read lines 280-330 |
| `docs/developer-platform/BASELINE.md` | edge-devops | Programme baseline written by a sibling session in this same audit wave; read for context only, not used as ev | none | full |
| `frontend/app/api/chat/route.ts` | edge-devops | The SSE proxy: sets the streaming headers and pipes the orchestrator's body through untouched | authenticated (cookie forwarded to the orchestrator) | full |
| `frontend/lib/auth.ts` | edge-devops | authRedirect — the pure gating decision and its /api, /_next and dotted-asset exclusions | public | full for lines 1-290 |
| `frontend/middleware.ts` | edge-devops | Edge page gating; the matcher that decides which paths get bounced to /login | public | full |
| `frontend/next.config.mjs` | edge-devops | Security headers; confirms there are no Next rewrites — all proxying is route handlers | public | full |
| `launcher/techsara_cli/bridge.py` | edge-devops | Bearer-authenticated proxy in front of host-native model servers (Mac Metal profile only) | authenticated | interface-only — read lines 1-60 |
| `launcher/techsara_cli/cli.py` | edge-devops | Launcher: builds the compose -f chain, decides which overlays are layered | internal-only | interface-only — read _compose_files (213-240) and the surrounding controller he |
| `launcher/techsara_cli/cluster.py` | edge-devops | Generates the dual-node cluster keys including CLUSTER_API_BIND_ADDRESS | internal-only | interface-only — read lines 80-86 and 855-905 |
| `launcher/techsara_cli/environment.py` | edge-devops | Resolves TECHSARA_BIND_ADDRESS / TECHSARA_MODEL_BIND_ADDRESS, mints local secrets, computes the engine head UR | internal-only | interface-only — read 128-170, 480-530, 575-600 |
| `monitoring/engine-controller/controller.py` | edge-devops | Engine availability controller: state/metrics HTTP API on :9838, manual recovery, Docker socket | public-on-LAN for GET; loopback-only for POST /recover | interface-only — read the Config defaults (290, 356) and the whole Handler class |
| `monitoring/engine-controller/sentinel.py` | edge-devops | Worker-side restart sentinel; peer + shared-token gated | authenticated | interface-only — grepped the auth path (403-451) |
| `orchestrator/app/authn/sessions.py` | edge-devops | Where X-Forwarded-For is honoured when AUTH_TRUST_PROXY_HEADERS is on | authenticated | interface-only — grepped the forwarded-header path (178, 222-228) |
| `orchestrator/app/main.py` | edge-devops | FastAPI app construction and the SSE response builder | authenticated per-route, but /docs /redoc /openapi.json are open | interface-only — read the FastAPI() call (219) and _sse_response (1551-1556); ro |
| `orchestrator/app/sse.py` | edge-devops | SSE framing and the 15 s heartbeat that keeps Cloudflare from timing out a long generation | internal-only | interface-only — grepped HEARTBEAT_SECONDS |
| `pgadmin/servers.json` | edge-devops | pgAdmin pre-registered server; checked for committed credentials | internal-only | full — no password, uses PassFile |
| `scripts/deploy-smoke.sh` | edge-devops | Post-deploy health probes; all against 127.0.0.1 | internal-only | interface-only — grepped the curl targets |
| `scripts/deploy.sh` | edge-devops | Rolling deploy driven from the pipeline's self-hosted job | internal-only | interface-only — grepped bind addresses and compose references; confirmed it doe |
| `scripts/lib/cluster-common.sh` | edge-devops | Shared cluster settings loader; defaults CLUSTER_API_BIND_ADDRESS to 0.0.0.0 and maps it to a probe host | internal-only | interface-only — read lines 71-115 and 185-195 |
| `scripts/ocr.sh` | edge-devops | Places the OCR engine on the worker and records OCR_REMOTE_BASE_URL in .env | internal-only | interface-only — read lines 265-295 |
| `scripts/tunnel.sh` | edge-devops | up/down/status/logs/check for the Cloudflare tunnels; owns the compose chain used for the tunnel profiles | internal-only | full for lines 1-120 |
| `scripts/whisper.sh` | edge-devops | Places whisper on head or worker and derives its bind address | internal-only | interface-only — read lines 88-135 and 250-300 |
| `techsara` | edge-devops | POSIX entry point that bootstraps a pinned uv and hands off to techsara_cli | none | interface-only — read lines 1-60 |
| `compose.yaml` | frontend-app | frontend service: ORCHESTRATOR_URL, MOCK_MODE, port binding | internal-only | skimmed — the frontend service block (lines 301-331) |
| `frontend/AGENTS.md` | frontend-app | Next.js agent rules block auto-written by `next dev`; contains no project guidance | none | full — 12 lines, entirely the nextjs-agent-rules block |
| `frontend/Dockerfile` | frontend-app | Multi-stage standalone build; NODE_ENV=production, exec-form CMD | internal-only | full |
| `frontend/app/accept-invite/page.tsx` | frontend-app | /accept-invite page shell; token read client-side from the query | public | full |
| `frontend/app/access-removed/page.tsx` | frontend-app | /access-removed server component rendering the session-end explanation from the query string | public | full |
| `frontend/app/admin/analytics/leaderboards/page.tsx` | frontend-app | Leaderboards; calls the analytics/models API path | admin | interface-only — line 253-266 only, to confirm 'analytics/models' is an API path |
| `frontend/app/admin/layout.tsx` | frontend-app | Admin shell: /api/auth/me gate on members.read, capability-driven nav | admin | full |
| `frontend/app/admin/members/[id]/conversations/[cid]/page.tsx` | frontend-app | Read-only admin transcript viewer | admin | skimmed — head (lines 1-60) plus capability greps across app/admin |
| `frontend/app/admin/page.tsx` | frontend-app | /admin workspace analytics overview | admin | full |
| `frontend/app/api/admin/[...path]/route.ts` | frontend-app | Catch-all proxy to /admin/api/*; local streaming download path for 3 file endpoints | admin | full |
| `frontend/app/api/artifacts/[[...path]]/route.ts` | frontend-app | Optional catch-all proxy for Artifact Studio; closed path grammar, per-route query allowlist, bounded POST bod | authenticated | full |
| `frontend/app/api/audio/transcribe/route.ts` | frontend-app | POST — streams the recording to the ASR engine; query forwarded verbatim | authenticated | full |
| `frontend/app/api/auth/invitations/[token]/route.ts` | frontend-app | GET /auth/invitations/{token} preview; token encodeURIComponent'd | public | full |
| `frontend/app/api/auth/invitations/accept/route.ts` | frontend-app | POST /auth/invitations/accept (auto-login, Set-Cookie) | public | full |
| `frontend/app/api/auth/login/route.ts` | frontend-app | POST /auth/login via proxyToOrchestrator (carries Set-Cookie down) | public | full |
| `frontend/app/api/auth/logout/route.ts` | frontend-app | POST /auth/logout | public | full |
| `frontend/app/api/auth/me/route.ts` | frontend-app | GET /auth/me — the identity probe every client gate uses | public | full |
| `frontend/app/api/auth/password/route.ts` | frontend-app | POST /auth/password | authenticated | full |
| `frontend/app/api/auth/preferences/route.ts` | frontend-app | GET/PUT /auth/preferences | authenticated | full |
| `frontend/app/api/auth/sessions/revoke/route.ts` | frontend-app | POST /auth/sessions/revoke | authenticated | full |
| `frontend/app/api/auth/sessions/route.ts` | frontend-app | GET /auth/sessions | authenticated | full |
| `frontend/app/api/chat/active/route.ts` | frontend-app | GET — conversation ids still generating | authenticated | full |
| `frontend/app/api/chat/attach/[id]/route.ts` | frontend-app | GET — re-join a detached generation; SSE pipe; SAFE_ID validated | authenticated | full |
| `frontend/app/api/chat/compact/route.ts` | frontend-app | POST — compact a conversation on demand | authenticated | full |
| `frontend/app/api/chat/requests/[id]/route.ts` | frontend-app | GET — send-intent reconciliation; SAFE_ID validated; upstream body passed through unchanged | authenticated | full |
| `frontend/app/api/chat/route.ts` | frontend-app | POST /api/chat — the SSE chat proxy (and MOCK_MODE fixture stream) | authenticated | full |
| `frontend/app/api/chat/salesforce/[id]/route.ts` | frontend-app | GET — Salesforce clarification/starter state for a conversation | authenticated | full |
| `frontend/app/api/chat/salesforce/cancel/route.ts` | frontend-app | POST — drop a pending clarifying question | authenticated | full |
| `frontend/app/api/chat/stop/route.ts` | frontend-app | POST — cancel a detached generation | authenticated | full |
| `frontend/app/api/conversations/[id]/share/route.ts` | frontend-app | GET/POST/PATCH/DELETE share controls for a conversation owner | authenticated | full |
| `frontend/app/api/debug/error/route.ts` | frontend-app | Dev-only error simulator; 404s when NODE_ENV=production | internal-only | full |
| `frontend/app/api/history/[...path]/route.ts` | frontend-app | Catch-all proxy to /history/* behind classifyHistoryPath; search params allowlisted | authenticated | full |
| `frontend/app/api/public/shares/[token]/route.ts` | frontend-app | GET — the only anonymous endpoint; token is the credential, verified upstream | public | full |
| `frontend/app/api/reports/[filename]/route.ts` | frontend-app | GET report download proxy; isSafeReportName traversal guard | authenticated | full |
| `frontend/app/api/upload/chunked/[...path]/route.ts` | frontend-app | Chunked upload rail proxy; SAFE_SEGMENT path validation; streamed parts | authenticated | full |
| `frontend/app/api/upload/route.ts` | frontend-app | POST — dataset multipart upload, streamed (duplex half) | authenticated | full |
| `frontend/app/api/uploads/[conversation]/[upload]/file/route.ts` | frontend-app | GET — upload bytes by reference; shape validated before any fetch | authenticated | full |
| `frontend/app/api/uploads/[conversation]/document/route.ts` | frontend-app | GET — a stored document by conversation + original filename | authenticated | full |
| `frontend/app/api/uploads/[conversation]/route.ts` | frontend-app | GET — list a conversation's uploads | authenticated | full |
| `frontend/app/layout.tsx` | frontend-app | Root layout, theme-init inline script, metadata | public | full |
| `frontend/app/login/page.tsx` | frontend-app | /login page shell | public | full |
| `frontend/app/page.tsx` | frontend-app | / renders <ChatApp/> | authenticated | full |
| `frontend/app/share/[token]/page.tsx` | frontend-app | /share/<token> public read-only conversation; token handed to a client component | public | full |
| `frontend/components/AccountMenu.tsx` | frontend-app | Account menu; the /admin link is conditional on canAdmin (UI only) | authenticated | skimmed — lines 275-300 plus grep for 'admin' |
| `frontend/components/admin/api.ts` | frontend-app | Me/parseMe/can, adminJson/adminPost/adminPut, feature + analytics types | admin | full |
| `frontend/components/auth/LoginForm.tsx` | frontend-app | The /login form; full navigation on success | public | full |
| `frontend/components/auth/http.ts` | frontend-app | readDetail + OFFLINE_MESSAGE shared by the auth forms | public | full |
| `frontend/lib/auth.ts` | frontend-app | SESSION_COOKIE name, fetchMe, authRedirect (pure gate), sessionEndRoute, logout | public | full |
| `frontend/lib/historyRoutes.ts` | frontend-app | classifyHistoryPath — the allowlist deciding which /api/history/* paths forward | authenticated | full |
| `frontend/lib/mockApi.ts` | frontend-app | MOCK_MODE in-memory auth/history backend | public | skimmed — read handleMockAuth and the MOCK_ME/MOCK_SESSION constants (lines 45-1 |
| `frontend/lib/orchestrator.ts` | frontend-app | toOrchestratorChatRequest — UI body to orchestrator ChatRequest translation | authenticated | skimmed — read toOrchestratorChatRequest (lines 189-250) to confirm base64 image |
| `frontend/lib/proxy.ts` | frontend-app | Shared server-side proxy helper used by /api/auth, /api/history, /api/admin, /api/conversations, /api/public | authenticated | full |
| `frontend/lib/serverLog.ts` | frontend-app | key=value proxy error log, sanitizeForLog, requestIdOf | internal-only | full |
| `frontend/lib/sse.ts` | frontend-app | Incremental SSE parser; comment/keep-alive lines ignored | none | skimmed — read the parser core (lines 1-120) |
| `frontend/lib/streams.ts` | frontend-app | Per-conversation stream manager; the client side of /api/chat, /api/chat/stop, /api/chat/attach | authenticated | skimmed — read the /api/chat call site (1160-1230) and grepped abort/fetch sites |
| `frontend/middleware.ts` | frontend-app | Edge page gate: cookie-presence redirect to /login or home | public | full |
| `frontend/next.config.mjs` | frontend-app | standalone output, security headers (nosniff, XFO DENY, referrer, permissions), /share noindex | public | full |
| `frontend/package.json` | frontend-app | next ^16.3.3, react 19, vitest; scripts | none | full |
| `frontend/tests/admin-proxy.test.ts` | frontend-app | Tests the /api/admin proxy's cookie and query forwarding and the download split | none | skimmed — first 60 lines |
| `frontend/tests/auth-middleware.test.ts` | frontend-app | Unit tests for authRedirect (the pure decision), not for config.matcher | none | full |
| `frontend/tsconfig.json` | frontend-app | Path alias @/* → ./* | none | full |
| `frontend/vitest.config.mts` | frontend-app | Test config: tests/**/*.test.ts(x), node env by default | none | full |
| `orchestrator/app/authn/api.py` | frontend-app | Cross-check: login throttle keyed on email and ip | public | interface-only — lines 55-117 |
| `orchestrator/app/authn/sessions.py` | frontend-app | Cross-check: set_cookie attributes and client_meta's X-Forwarded-For trust | authenticated | interface-only — lines 160-231 |
| `orchestrator/app/main.py` | frontend-app | Cross-check: CORS + cross-site-write middleware, /chat 401 gate, /reports owner gate | authenticated | interface-only — lines 210-275, 1579-1668, 1074-1082 |
| `compose/compose.cluster-dgx-spark.yaml` | inference-model-registry | The live overlay: host-network vLLM head bound to CLUSTER_API_BIND_ADDRESS, ports reset | internal-only | interface-only - grepped network_mode/--host/ports |
| `compose/compose.ocr.yaml` | inference-model-registry | The OCR engine overlay: host-network, bound to OCR_BIND on port 30004 | internal-only | interface-only - grepped bind/ports |
| `docker-compose.yml` | inference-model-registry | Superseded single-node compose, kept as rollback; documents that it publishes model APIs on 0.0.0.0 | internal-only | interface-only - read lines 1-30 and 100-135 |
| `orchestrator/app/admission.py` | inference-model-registry | Two concurrency lanes in front of the main engine (NORMAL max 10, LONG 1 with idle-wait and a NORMAL closure) | internal-only | full - 452 lines read |
| `orchestrator/app/breaker.py` | inference-model-registry | CLOSED/OPEN/HALF_OPEN circuit breaker per engine, bounded CONTRACT-4 failure reasons, URL->engine registry | internal-only | full - 483 lines read |
| `orchestrator/app/config.py` | inference-model-registry | Settings: model URLs/ids, context windows, output ceilings, timeouts, breaker/admission/queue knobs, capabilit | internal-only | interface-only - read lines 95-160, 535-600, 1365-1594 (the model, timeout, avai |
| `orchestrator/app/context.py` | inference-model-registry | Prompt sizing: /tokenize round trips, window cache, fit_request trimming/clipping, character estimate | internal-only | full - 329 lines read |
| `orchestrator/app/continuation.py` | inference-model-registry | stream_long_completion: multi-segment generation over stream_chat_events with seam stripping; the only consume | internal-only | skimmed - read lines 240-470 |
| `orchestrator/app/continuity.py` | inference-model-registry | The durable queue for a turn while the main model cannot take it: Hold, park/resume, MODEL_RECOVERING, the REA | authenticated | full through line 640 (module contract, Hold, bind, sweep setup); sweep body 640 |
| `orchestrator/app/engine_state.py` | inference-model-registry | Polls the engine controller's GET /state, keeps the nine-state verdict, drives the breaker's external open and | internal-only | full - 609 lines read |
| `orchestrator/app/engines/__init__.py` | inference-model-registry | Shared engine helpers: history windowing and the DIAGRAM/CODE prompt blocks | internal-only | full - 100 lines read |
| `orchestrator/app/engines/chat.py` | inference-model-registry | The plain chat engine: system prompts, per-effort max_tokens and temperature | authenticated | skimmed - read lines 1-120 |
| `orchestrator/app/health.py` | inference-model-registry | Dependency probes plus engine_availability() (controller verdict, breakers, queue, lanes) surfaced on /health | public | interface-only - read lines 690-760 and grepped the rest |
| `orchestrator/app/llm.py` | inference-model-registry | All model clients: main-model chat/stream/tools/JSON, the _primary_send choke point, thinking switch, usage +  | internal-only | full - 1377 lines read |
| `orchestrator/app/main.py` | inference-model-registry | The FastAPI app and the /chat SSE entry point: LiveGeneration, the detached worker, usage events, stop/attach/ | authenticated | skimmed - read lines 243-530, 627-820, 988-1055, 1551-1760, 1840-1935, 2100-2260 |
| `orchestrator/app/model_capabilities.py` | inference-model-registry | Typed per-role capability records (context_length, output_limit, concurrency, extra_body allowlist) resolved f | internal-only | full - 293 lines read |
| `orchestrator/app/resilience.py` | inference-model-registry | resilient() wrapper: breaker gate, failure classification, wait_for_engine, GuardedStream, give-up/park | internal-only | full - 960 lines read |
| `orchestrator/app/sse.py` | inference-model-registry | The one SSE formatter: allowed event names, frame shape, 15 s keep-alive comment | authenticated | full - 104 lines read |
| `orchestrator/tests/test_admission.py` | inference-model-registry | Lane behaviour tests - used to check which admission paths are covered | none | interface-only - test names and the closed/first_token assertions |
| `scripts/lib/cluster-common.sh` | inference-model-registry | Generates the cluster env, including the API bind address default | internal-only | interface-only - read lines 80-115 |
| `scripts/ocr.sh` | inference-model-registry | Moves the OCR engine to the worker node and writes OCR_BIND/OCR_REMOTE_BASE_URL | internal-only | interface-only - read lines 270-295 and grepped OCR_BIND |
| `compose.yaml` | orchestrator-core | Orchestrator host port publication via TECHSARA_BIND_ADDRESS | internal-only | skimmed — line 233 and the frontend block |
| `compose/compose.cloudflare.yaml` | orchestrator-core | Public tunnel overlay — maps ONLY frontend:3000, never the orchestrator | public | full (1-80) |
| `compose/compose.cluster-dgx-spark.yaml` | orchestrator-core | vLLM head runs network_mode: host with --host $CLUSTER_API_BIND_ADDRESS | internal-only (intended); actually LAN-public, see findings | skimmed 20-60 |
| `frontend/app/api` | orchestrator-core | The Next.js proxy's explicit allow-list of orchestrator paths (30 route.ts files); /docs, /openapi.json, /metr | public (via the Cloudflare tunnel) | interface-only — directory listing of route.ts files |
| `launcher/techsara_cli/cluster.py` | orchestrator-core | Computes CLUSTER_API_BIND_ADDRESS; DEFAULT_API_BIND_ADDRESS = "0.0.0.0" | internal-only | skimmed 83, 855-905 |
| `orchestrator/Dockerfile` | orchestrator-core | The uvicorn command line — single worker, no body limit, 90s graceful shutdown | internal-only | full tail (last 25 lines) |
| `orchestrator/app/admission.py` | orchestrator-core | The two-lane admission gate in front of the main model — THE inference concurrency control | internal-only | full for the docstring (1-80), _admit/_Ticket (250-340), run() (388-433) |
| `orchestrator/app/artifacts/api.py` | orchestrator-core | 13 routes under /artifacts, require_user + require_artifacts | authenticated | interface-only — decorators and Depends lines |
| `orchestrator/app/audio_api.py` | orchestrator-core | POST /audio/transcribe (require_user + require_voice) and GET /audio/health (Cap.ANALYTICS_READ) | authenticated + admin | interface-only plus the /audio/health handler 262-290 |
| `orchestrator/app/auth.py` | orchestrator-core | Compatibility shim: UserRow, current_user, require_user, re-exports the /auth router | authenticated | full (47 lines) |
| `orchestrator/app/authn/admin_api.py` | orchestrator-core | 25 routes under /admin/api, each Depends(require_capability(Cap.*)) | admin | interface-only — every decorator + its Depends line enumerated |
| `orchestrator/app/authn/analytics_api.py` | orchestrator-core | 10 routes under /admin/api/analytics behind a module-level Gate = Depends(require_capability(Cap.ANALYTICS_REA | admin | interface-only |
| `orchestrator/app/authn/api.py` | orchestrator-core | 11 routes under /auth: login, logout, me, password, sessions, preferences, invitations | public (login/invitations) and authenticated (the rest, enforced in-handler) | interface-only — decorators plus the two set_cookie call sites |
| `orchestrator/app/authn/principal.py` | orchestrator-core | Principal dataclass, current_principal/require_principal/require_capability, audit() | authenticated | full for 1-60 and 100-180 (the dependency factories) |
| `orchestrator/app/authn/rbac.py` | orchestrator-core | Role and Cap enums; _ADMIN_CAPS; SUPER_ADMIN computed as everything | admin | skimmed 16-60 for the capability vocabulary |
| `orchestrator/app/authn/sessions.py` | orchestrator-core | ts_session cookie issue/clear; secure flag resolution | authenticated | interface-only — set_cookie/clear_cookie/_cookie_secure (170-215) |
| `orchestrator/app/authn/shares_api.py` | orchestrator-core | 4 routes under /admin/api/shares behind Gate = Depends(require_capability(Cap.SHARES_MANAGE)) | admin | interface-only |
| `orchestrator/app/config.py` | orchestrator-core | Settings singleton built from os.environ at import; `settings = Settings()` at line 1594 | internal-only | skimmed — head (1-60), tail (1555-1594), CORS block (1174-1202), admission block |
| `orchestrator/app/core/tracing.py` | orchestrator-core | TraceRecorder — where request_id and trace_id come from; sanitize() | internal-only | full for 1-180 |
| `orchestrator/app/db.py` | orchestrator-core | All persistence; get_query_trace scopes a trace to its owner | internal-only | interface-only — get_query_trace (6123-6150) and a grep for api_key tables (none |
| `orchestrator/app/engine_state.py` | orchestrator-core | Engine controller poller; describe() feeds /health | internal-only (but its output reaches public /health) | interface-only — line 269 where the controller URL is emitted |
| `orchestrator/app/health.py` | orchestrator-core | check_dependencies() + engine_availability() — what GET /health serves | public (served unauthenticated via GET /health) | interface-only plus engine_availability (716-760) and check_dependencies head; t |
| `orchestrator/app/history.py` | orchestrator-core | 12 routes under /history, every one Depends(require_user) | authenticated | interface-only plus 100-135 for the request-model shape |
| `orchestrator/app/llm.py` | orchestrator-core | Model call layer; _primary_send is the choke point where admission wraps every main-model call | internal-only | interface-only — symbol list plus 186-245 (_fit/_primary_send) and the embed sem |
| `orchestrator/app/main.py` | orchestrator-core | The FastAPI app object, middleware, lifespan, LiveGeneration/SSE registry, POST /chat and the 9 /chat* lifecyc | mixed: /health and /metrics public; /reports* and /chat* authenticated in-handler | full for all route definitions, app construction (219-290), LiveGeneration (291- |
| `orchestrator/app/memory_api.py` | orchestrator-core | 3 routes under /memory, Depends(require_user) | authenticated | interface-only |
| `orchestrator/app/metrics.py` | orchestrator-core | stdlib Prometheus exposition; closed label vocabularies; render() serves GET /metrics | public (served unauthenticated via GET /metrics) | full for lines 1-120 (cardinality design, label sets) and interface-only for inc |
| `orchestrator/app/resilience.py` | orchestrator-core | resilient() retry/breaker wrapper around every model call; ModelUnavailable; GuardedStream | internal-only | interface-only — module docstring (1-70) and the public symbol list; internals n |
| `orchestrator/app/share_api.py` | orchestrator-core | 6 sharing routes mounted at the app root, including the only anonymous endpoint GET /public/shares/{token} | public (one route) + authenticated | full for 168-240 and 455-507; the rest interface-only |
| `orchestrator/app/sse.py` | orchestrator-core | The single SSE framing module: event vocabulary, JSON data shape, heartbeat comment | none (pure formatting) | full (104 lines) |
| `orchestrator/app/uploads.py` | orchestrator-core | 9 routes under /uploads including the chunked rail; require_user + require_attachments on the create paths | authenticated | interface-only — decorators and Depends lines |
| `orchestrator/app/usage.py` | orchestrator-core | One durable usage_events row per completed turn (V18); never raises | internal-only | full (121 lines) |
| `orchestrator/app/video/api.py` | orchestrator-core | GET /video/{conversation_id}/{upload_id}/status, require_user + require_video | authenticated | interface-only |
