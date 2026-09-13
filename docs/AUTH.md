# Authentication, workspaces and administration

TechSara is an enterprise workspace since 2026-09-01: every person signs in,
every conversation/upload/memory/report belongs to its owner, and workspace
administrators manage members through an audited admin surface. This document
is the reference for how identity works and how to operate it.

## The model in one page

- **User** — a person. `users` table: email (login identifier), display name,
  Argon2id password hash, status (`active`/`disabled`).
- **Workspace** — the company space. One per deployment ("TechSara's
  Workspace" by default, `WORKSPACE_NAME` to change); the schema supports more.
- **WorkspaceMembership** — user ↔ workspace with a **role**:
  `super_admin`, `admin`, or `member`.
- **Session** — an opaque server-side row. The browser holds an HttpOnly
  `ts_session` cookie `"<id>.<secret>"`; PostgreSQL stores only
  `sha256(secret)`. Logout/revocation kills the row, so access truly ends.
- **Invitation** — the only way an account is created. High-entropy one-use
  token, stored hashed, expiring (`AUTH_INVITATION_TTL_DAYS`, default 7),
  revocable. No public signup exists.
- **AuditEvent** — append-only trail of security-relevant actions.

## Sessions and persistent login

Signing in with "Stay signed in" (the default) gives a persistent session:

- The cookie lives `AUTH_SESSION_ABSOLUTE_DAYS` (default 90).
- The server-side expiry **rolls forward with activity**: any request after a
  quiet spell extends it to now + `AUTH_SESSION_DAYS` (default 30), but never
  past the absolute ceiling. Log in Monday, reboot, come back next week —
  still signed in. Go quiet for a month — signed out.
- Unticking "Stay signed in" gives a browser-session cookie and a server
  lifetime of `AUTH_SESSION_UNREMEMBERED_HOURS` (default 24).

Password changes revoke every *other* session. Deactivating a member revokes
all of theirs immediately. Users see and revoke their own sessions under
Settings → Sessions; admins can revoke a member's sessions from the admin
area.

## Roles and capabilities (RBAC)

Roles never appear as string comparisons in routes. Every admin route asks for
a **capability**; `orchestrator/app/authn/rbac.py` is the single table mapping
roles to capabilities:

| Capability | member | admin | super_admin |
|---|---|---|---|
| workspace.read | – | ✓ | ✓ |
| members.read / members.manage | – | ✓ | ✓ |
| invites.manage | – | ✓ (members only) | ✓ (any role) |
| workspace_content.read (audited content viewer) | – | ✓ | ✓ |
| sessions.manage | – | ✓ | ✓ |
| roles.manage | – | – | ✓ |
| audit.read | – | – | ✓ |
| analytics.read (per-member usage table and its export — since 2026-09-13) | – | – | ✓ |
| workspace.manage / settings.manage | – | – | ✓ |
| api.console.access (the developer console at `/api`) | – | ✓ | ✓ |
| api.projects.read / api.projects.manage | – | ✓ | ✓ |
| api.keys.create / api.keys.revoke | – | ✓ | ✓ |
| api.usage.read / api.logs.read / api.webhooks.manage | – | ✓ | ✓ |
| api.models.manage (which models `/v1` exposes) | – | – | ✓ |
| api.limits.manage (a project's rate, token and concurrency ceilings) | – | – | ✓ |

The `api.*` capabilities (2026-09-13) gate the developer platform's **browser**
side only. A request to the public API at `/v1` carries an API key, which
resolves to a project and a set of scopes — never to a person or a capability —
so a leaked key cannot be mistaken for an administrator. See
[`docs/developer-platform/OPERATIONS.md`](developer-platform/OPERATIONS.md).

Guard-rails baked into the API (not the UI):

- An admin can never manage an equal-or-higher role (no deactivating,
  removing, resetting or revoking another admin or a super admin).
- Since 2026-09-13 the same rank rule applies to **reading**: an admin gets 404
  for a super admin's or a peer admin's sessions, conversations, uploads and
  reports, and the member detail withholds their usage counts. Reading your own
  is always allowed.
- The workspace can never lose its last active super admin — demotion,
  deactivation and removal all answer 409.
- Nobody can deactivate or remove themselves.
- "Remove member" deletes the membership, disables the account and revokes
  sessions — the person's data is deliberately **kept** (deleting a departed
  employee's work is a separate, explicit decision).

## Member privacy

Ownership is enforced in SQL on the backend, keyed by the server-resolved
session — never by anything the client sends. A member who edits URLs, IDs,
cookies or request bodies gets **404** for another member's conversations,
messages, uploads, reports, memory facts, Salesforce clarification state, and
active generations (stop/attach). Report files live in one directory on disk,
so downloads are authorised by the `report_files` ownership table, not by
knowing a filename.

Administrative access is the one exception, and it is: read-only, behind
`workspace_content.read`, limited to accounts the viewer outranks (or their
own), and **audited** — every viewed conversation and
downloaded file writes an `audit_events` row (admin, target, resource,
timestamp, source address). There is no impersonation: nothing lets an admin
act *as* a member or feed a member's content into their own model context.

Users are told: the invitation-accept page carries the standard notice that
workspace content may be accessible to authorized administrators in
accordance with company policy.

## The model knows who it is talking to

Each chat request sets a request-scoped identity line ("You are assisting
NAME (email) in WORKSPACE…") that prompt builders append to their system
prompt (`orchestrator/app/identity.py`). It is derived only from the server
session — a client cannot spoof it — and carries nothing security-relevant.
Memory (facts, cross-chat recall, embeddings) is keyed by `user_id` in SQL
and never crosses accounts.

## The sign-in pages

The sign-in pages wear the **logo's colour**, not the product accent: the mark
(`public/techsara-mark.png`) is deep indigo `#1a2480`, so the button, checkbox,
focus rings and field focus all use it there — white on it scores 13:1 (AAA).
The app's Signal Teal remains the in-product accent everywhere else. It is done
by overriding the accent tokens for the `.auth-light` subtree, so no component
hard-codes a colour.

`/login` and `/accept-invite` are **white in both themes** — `.auth-light` in
`globals.css` re-declares the light tokens for that subtree, so the components
still use the ordinary design vocabulary (`bg-bg`, `text-ink`, `accent`) and it
simply resolves to the paper palette. The workspace illustrations are drawn on
white; a dark page framed them in a black box.

The brand column shows one of the illustrations in `frontend/public/illustrator/`
(the `.webp` files are trimmed, downscaled derivatives of the `.png` originals
beside them — the 2000×2000 sources are ~470 KB each). **Which one is random per
page load**, so two people signing in see different artwork and the same person
sees a new one after signing out and back in (logout is a full navigation to
`/login`). It then cross-fades every 8 s, and the dots below it jump to a chosen
one and stop the drift. Under `prefers-reduced-motion` the choice is still
random but nothing moves. The caption is plain server-rendered markup — it does
not wait on JavaScript, so a slow or blocked hydration never leaves the panel
captionless.

Adding artwork: drop a `<slug>.webp` in that folder and add an entry (slug,
title, body, and the two tint stops sampled from the drawing) to `ILLUSTRATIONS`
in `components/auth/IllustrationPanel.tsx`. `tests/login-illustration.test.tsx`
asserts every entry is reachable.

## When a session ends — and the person is told why (2026-09-03)

Removing or deactivating a member used to produce, for that person, a bare
401, a bounce to the sign-in form, and then *"Incorrect email or password"*
on every attempt. The login form is deliberately generic (it must never
confirm which accounts exist), so it can never explain. The explanation now
lives one step earlier, where it is safe:

* every revocation records **why** (`auth_sessions.revoke_reason`, migration
  V15): `logout`, `user_revoked`, `password_changed`, `admin_revoked`,
  `password_reset`, `account_disabled`, `account_removed`;
* `GET /auth/me` with a cookie that no longer opens a session answers **401
  with a body** — `{detail, code, ended_at, workspace?, contact?}` where `code`
  is `session_expired` · `session_revoked` · `account_disabled` ·
  `account_removed` · `signed_out`. It explains **only when the cookie's
  secret still matches the stored hash** — proof that browser held the real
  session — so a forged or absent cookie learns nothing (`signed_out`, no
  workspace, no contacts). For the two account-level codes the body carries
  the workspace name and the active admins' emails (super admins first), and
  the dead cookie is cleared so the edge gate stops bouncing the person back
  into an app that only 401s;
* the client (`lib/auth.ts: handleSessionEnd`) routes on that code from every
  place a 401 surfaces — the boot probe, a send, the 8 s heartbeat, the admin
  area. A removed or deactivated account has its **local data wiped** (the
  same wipe logout performs — this may be a shared machine) and lands on
  **`/access-removed`**, a public page in the sign-in layout that says what
  happened, when, who to contact (mailto links), and offers *Sign in with a
  different account*. Every other end goes to `/login` exactly as before.

The login form's wording is unchanged and still generic: a removed member who
returns days later without the cookie sees the same message as a wrong
password, by design.

## First-time setup (bootstrap)

A fresh (or upgraded) deployment has no credentialed account, so nobody can
log in until the first SUPER_ADMIN is established:

```bash
./techsara auth bootstrap --email you@company.com --name "Your Name"
# prompts for a password (never echoed, never in argv/logs)
```

On a pre-auth install this **adopts the existing local account** — every
conversation, upload and memory row keeps its owner and appears under the new
login. It also claims report files generated before ownership tracking.
Non-interactive use: set `AUTH_BOOTSTRAP_PASSWORD` in the environment.
Re-running it resets that same account's password (recovery path — requires
shell access to the host, which is already equivalent to DB access).

## Inviting people

Admin area → Members → **Invite member** (name, email, role). The UI shows a
one-time accept link (`/accept-invite?token=…`) — copy it and hand it over on
any channel you trust; no SMTP is required or used. Tokens are single-use,
expire after `AUTH_INVITATION_TTL_DAYS`, are stored hashed, and can be
revoked from the Pending invites tab. Re-inviting an address revokes the
earlier pending invite.

Inviting an address that **already has an account** re-onboards that account
rather than creating a new one, so since 2026-09-13 it follows the rank rule:
an active member is a 409 ("already a member"); a deactivated account may be
invited only by someone who outranks its role; a removed account, whose former
role is no longer known, only by a super admin. A refusal that stopped someone
reaching an account above their rank is audited as `invitation_refused`
(`orchestrator/app/authn/invites.py`). The rule is applied when an invitation is
**issued**; an invitation issued before that date is not re-checked when it is
accepted, so revoke any still pending for an existing account (the query is in
`docs/developer-platform/DISPOSITION.md`, OA-9).

## Feature access — which tools a person may use

Two different questions, deliberately two different mechanisms:

| | question | where it lives |
|---|---|---|
| **Capability** (RBAC) | may this account *administer* — invite members, read the audit log? | `authn/rbac.py`, a property of the ROLE |
| **Feature** | may this account *use* — web search, Salesforce, file uploads? | `authn/features.py`, a property of the PERSON |

An admin may want the whole workspace on the synced Salesforce copy but only
two analysts allowed to query the live org, and that has nothing to do with
who can invite people.

**The five tools**: photos/files/datasets, web search, deep research,
Salesforce, live Salesforce. Deep research needs web search; live Salesforce
needs Salesforce — a dependency that cannot dangle in either direction.

**Resolution**, each layer overriding the one before:

1. the built-in default (everything on)
2. the workspace default — Admin → **Access** (`workspaces.feature_defaults`)
3. the per-member override — Members → ⋯ → **Manage access**
   (`workspace_memberships.features`)
4. the dependency invariants
5. super admins get everything, so a workspace cannot lock itself out

Only keys someone actually set are stored, at either layer, so changing a
built-in default still reaches everyone who never overrode it.

**The client is never the gate.** `/auth/me` carries the resolved map and the
composer hides what is off (and corrects sticky prefs, so a member cannot sit
in a Salesforce mode whose trust footer the server would not honour), but:

- `/chat` re-resolves from the database and **downgrades** a blocked tool
  with one status line — never a 403 mid-conversation;
- the upload routes **refuse** with 403, because that is the door bytes
  actually arrive through.

Both changes are audited (`workspace_access_changed`, `member_access_changed`).

## Usage analytics

Admin → **Overview** is the workspace's usage report: totals, a daily
message chart, which tools were run, and one row per member — including
members who used nothing, which is usually why the page is open. Windows are
7D/1M/3M/6M/12M; seat counts do not move with the window, only the usage
below them. **Export** downloads the same per-member table as CSV and records
an `analytics_exported` audit event.

Since 2026-09-13 the report and its export (`GET /admin/api/analytics`,
`/admin/api/analytics/export`) require `analytics.read`, which only a super
admin holds — per-person consumption was already meant to be super-admin-only,
and an ordinary admin could reach it through `workspace.read`. Opening the
report is audited as `analytics_viewed`. Every text cell in the CSV passes
through the same formula neutraliser the artifact CSV writer uses, so a display
name beginning with `=`, `+`, `-` or `@` arrives as text, prefixed with an
apostrophe. It reads `messages.meta->>'route'`, so
the tool columns are what actually ran, not what was requested.

## Login protection

- Argon2id password hashing (OWASP parameters), transparent re-hash on login
  when parameters change. Minimum password length 10; no composition rules.
- Failure responses are generic and identical for wrong-password, unknown
  email and disabled accounts, with equalized timing.
- Throttling: `AUTH_LOGIN_MAX_FAILS` failures (default 8) inside
  `AUTH_LOGIN_WINDOW_SECONDS` (900) locks that email — and separately that
  source address — for `AUTH_LOGIN_LOCK_SECONDS` (300). Short on purpose:
  brute force becomes impractical without handing an attacker a permanent
  denial-of-service button for any address they can type.

## CSRF and cookies

The session cookie is `HttpOnly; SameSite=Lax; Path=/`, `Secure` when the
request arrives over HTTPS (`AUTH_COOKIE_SECURE=auto`; set `true` behind a
TLS-terminating proxy along with `AUTH_TRUST_PROXY_HEADERS=true` — forwarded
headers are ignored otherwise, on purpose). SameSite=Lax blocks cross-site
POSTs; the orchestrator additionally refuses any state-changing request whose
`Origin` header is present but not an allowed origin. SSE streams are GETs
and unaffected.

**The one exemption is `/v1`** (2026-09-13). The public developer API reads the
`Authorization` header and never the cookie, so there is no ambient credential
for a hostile page to ride and the cross-site check does not apply; a
developer's browser app sends its own `Origin` on every call and would
otherwise be refused before its key was read. The exemption matches `/v1` and
`/v1/…` only, never a longer name such as `/v1beta`. `/v1` answers its own CORS:
a preflight from any origin gets 204, the actual request is checked against the
project's `allowed_origins`, and `Access-Control-Allow-Credentials` is never
sent. The browser CORS allowlist (`CORS_ALLOW_ORIGINS`, credentials allowed) is
unchanged and is not applied to `/v1`.

## Endpoint classification

- **Public**: `/health` (deploy gates and container healthchecks depend on
  it), `/auth/login`, `/auth/logout`, `/auth/invitations/*` (token-gated),
  and `/v1/openapi.json` (the public developer API's schema, which describes
  only `/v1`).
- **Authenticated**: everything else — `/chat*`, `/history/*`, `/uploads/*`,
  `/memory/*`, `/reports*`, `/auth/me|password|sessions|preferences`.
- **Capability-gated**: `/admin/api/*` (404 to anyone without the
  capability, so the surface does not confirm its own existence). The developer
  console's API, `/admin/api/developers/*`, follows the same rule with the
  `api.*` capabilities.
- **API-key authenticated**: `/v1/*` (2026-09-13). `Authorization: Bearer
  tsk_live_…` or `tsk_test_…` only; a `ts_session` cookie is ignored, and a
  request with no key is `401 invalid_api_key` whatever cookies it carries.
  Every refusal of a key — unknown, revoked, expired, disabled project or
  workspace, address outside the project's IP allowlist — is the same 401.
  Reference: [`docs/developer-platform/API.md`](developer-platform/API.md).

Three rules apply to every route since 2026-09-13:

- **FastAPI's own `/docs`, `/redoc` and `/openapi.json` are not served** unless
  `ORCHESTRATOR_DEV_DOCS` is on. They described every internal route, the admin
  surface included, to anyone who could reach the port.
- **Request bodies are capped before they are read**: 1 MiB on `/v1`
  (`PUBLIC_API_MAX_BODY_BYTES`), the largest upload limit plus 1 MiB on
  `/uploads*`, 128 MiB everywhere else (`MAX_REQUEST_BODY_BYTES`). Over the cap
  is a 413, whether the body declared its length or not. The frontend's proxies
  apply their own caps (32 MiB on the shared proxy, 128 MiB on `/api/chat`).
- **A 422 no longer echoes the request**: the body names each failing field's
  location and reason and drops pydantic's `input`, so a malformed chat request
  cannot reflect its conversation or attachments back.

## Serving it publicly (Cloudflare Tunnel)

This machine has no public address — it is behind NAT — so a DNS A record has
nothing to point at. `cloudflared` solves that by dialling OUT to Cloudflare
and holding the connection open: no port forwarding, no firewall rule, no
router change, and **only the hostname you map is reachable**. Model APIs,
PostgreSQL and pgAdmin are not in the mapping and cannot be reached from the
internet even if their host bindings are wrong.

```bash
scripts/tunnel.sh up      # site goes live
scripts/tunnel.sh check   # verify the public hostname end to end
scripts/tunnel.sh down    # offline in seconds; the app keeps running locally
```

One-time setup, in the Cloudflare dashboard (Zero Trust → Networks → Tunnels):
create a tunnel, copy its token into `.runtime/secrets.env` as
`CLOUDFLARE_TUNNEL_TOKEN`, then add a Public Hostname — subdomain `ai`, domain
`techsarasolutions.com`, service **HTTP** → **`frontend:3000`**. Cloudflare
creates the DNS record itself (a CNAME to `<tunnel-id>.cfargotunnel.com`); do
not add an A record.

Behind TLS the app needs three settings (already in `.env`):
`AUTH_COOKIE_SECURE=true` — `auto` inspects the scheme the *orchestrator* sees,
which is plain HTTP inside Docker, so cookies would lose the Secure flag;
`AUTH_TRUST_PROXY_HEADERS=true` so audit events record the employee's real
address; and the public origin in `CORS_ALLOW_ORIGINS`.

**The frontend must be told which header carries that address** (2026-09-13).
The proxies used to copy `cf-connecting-ip` off every request into
`X-Forwarded-For`, which let anyone who reached port 3000 directly — the LAN,
the tailnet — choose the address the orchestrator recorded and dodge the
per-address login lockout. Now they forward exactly one header, named by
`TRUSTED_CLIENT_IP_HEADER`, and nothing when it is unset. Set
`TRUSTED_CLIENT_IP_HEADER: cf-connecting-ip` (and `TRUSTED_FORWARDED_PROTO:
https`) in the **frontend service's `environment:`** in `compose.yaml` — the
frontend container does not read `.env` — or every session and audit event
records the frontend container's address. Cloudflare overwrites
`cf-connecting-ip` at its edge, so it is trustworthy for traffic that came
through the tunnel. For sessions, audit and the login lockout the orchestrator
still believes `X-Forwarded-For` from any peer while
`AUTH_TRUST_PROXY_HEADERS=true`, so keep `:8080` off the LAN
(`docs/developer-platform/DISPOSITION.md`, OA-8). The `/v1` surface does not
use that switch: it believes a forwarded address only from a peer listed in
`PUBLIC_API_TRUSTED_PROXIES`, which should name the frontend's Docker network.

The public developer API rides the same hostname: `https://ai.techsarasolutions.com/v1/…`
reaches `frontend:3000`, whose `/v1` route handler forwards it to the
orchestrator with the `Authorization` header and without cookies. No tunnel
change is needed for it.

**Known limit — the one thing the tunnel cannot do.** `UPLOAD_MAX_MB` is
102400 (100 GB) and the orchestrator genuinely handles it: uploads stream to
disk in 1 MB chunks and the profiler reads through DuckDB rather than loading
the file, so a 282 MB / 7,000,000-row CSV profiles in about two seconds and the
real bound is free space on `/data`. But **Cloudflare's edge caps a request
body at 100 MB on Free/Pro** (200 MB Business, 500 MB Enterprise), and no
setting on this side raises it. So: uploads that arrive over the LAN or
Tailscale can be huge; uploads through `ai.techsarasolutions.com` stop at
100 MB with a 413. If large files must work for remote staff, the fix is
chunked upload (split client-side under the cap, reassemble server-side), not
a config change.

Streaming works: SSE passes through, and the 15 s heartbeat
(`SSE_HEARTBEAT_SECONDS`) is what stops Cloudflare timing out a long
generation.

## Configuration

All optional, with defaults (see `.env.example`):

```
AUTH_SESSION_DAYS=30              AUTH_SESSION_ABSOLUTE_DAYS=90
AUTH_SESSION_UNREMEMBERED_HOURS=24
AUTH_COOKIE_SECURE=auto           AUTH_COOKIE_NAME=ts_session
AUTH_LOGIN_MAX_FAILS=8            AUTH_LOGIN_WINDOW_SECONDS=900
AUTH_LOGIN_LOCK_SECONDS=300       AUTH_INVITATION_TTL_DAYS=7
AUTH_TRUST_PROXY_HEADERS=false    WORKSPACE_NAME="TechSara's Workspace"
ORCHESTRATOR_DEV_DOCS=            MAX_REQUEST_BODY_BYTES=134217728
```

The frontend's `TRUSTED_CLIENT_IP_HEADER` and `TRUSTED_FORWARDED_PROTO` are set
on the frontend service, not in `.env` (above). The developer platform's own
settings — `API_KEY_PEPPER`, `PUBLIC_API_MAX_BODY_BYTES` and the
`PUBLIC_API_DEFAULT_*` limits — are described in
[`docs/developer-platform/OPERATIONS.md`](developer-platform/OPERATIONS.md) §2.

## Schema

Migration **V12** (`orchestrator/app/db.py`) — additive, transactional, no
data rewritten: extends `users` (email, display_name, status, timestamps) and
adds `workspaces`, `workspace_memberships`, `auth_sessions`,
`workspace_invitations`, `audit_events`, `login_throttle`, `report_files`,
`user_preferences`. Existing conversations were already keyed by `user_id`
and are untouched; the startup baseline gives every pre-existing user a
membership automatically.

Migration **V15** adds `auth_sessions.revoke_reason` (why a session ended —
see above). Migration **V17** adds the two feature-access layers:
`workspaces.feature_defaults` and `workspace_memberships.features`, both
`jsonb NOT NULL DEFAULT '{}'`. Additive with defaults, so the previous
release's statements keep working and a code rollback needs no schema change.

Migration **V34** (2026-09-13) adds the developer platform's eleven tables —
`api_projects`, `api_service_accounts`, `api_keys`, `api_responses`,
`api_idempotency`, `api_usage_minute`, `api_usage_daily`,
`api_webhook_endpoints`, `api_webhook_deliveries`, `public_models`,
`platform_secrets` — and alters no existing table. `api_keys` stores
`HMAC-SHA256(pepper, secret)`, never a key; the pepper is `API_KEY_PEPPER`, or a
generated value in `platform_secrets` when that is unset. Column list:
[`docs/developer-platform/SCHEMA-V34.md`](developer-platform/SCHEMA-V34.md).

## Operational notes

- The pre-auth test corpus runs under an ambient test identity
  (`orchestrator/tests/conftest.py`); the auth/RBAC/IDOR suites log in over
  real HTTP and exercise genuine session resolution.
- Audit events are append-only by convention; the Audit Log page
  (super admin) reads them with keyset pagination.
- Session rows dead longer than 30 days are pruned at orchestrator startup.
- Client caches (IndexedDB/localStorage) are keyed per user id and cleared on
  logout/account switch, so a shared computer never shows the previous
  person's cached conversations.
