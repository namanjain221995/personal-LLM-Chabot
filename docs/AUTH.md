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
| workspace.manage / settings.manage | – | – | ✓ |

Guard-rails baked into the API (not the UI):

- An admin can never manage an equal-or-higher role (no deactivating,
  removing, resetting or revoking another admin or a super admin).
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
`workspace_content.read`, and **audited** — every viewed conversation and
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

## Endpoint classification

- **Public**: `/health` (deploy gates and container healthchecks depend on
  it), `/auth/login`, `/auth/logout`, `/auth/invitations/*` (token-gated).
- **Authenticated**: everything else — `/chat*`, `/history/*`, `/uploads/*`,
  `/memory/*`, `/reports*`, `/auth/me|password|sessions|preferences`.
- **Capability-gated**: `/admin/api/*` (404 to anyone without the
  capability, so the surface does not confirm its own existence).

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
address (the frontend proxy forwards Cloudflare's `cf-connecting-ip`); and the
public origin in `CORS_ALLOW_ORIGINS`.

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
```

## Schema

Migration **V12** (`orchestrator/app/db.py`) — additive, transactional, no
data rewritten: extends `users` (email, display_name, status, timestamps) and
adds `workspaces`, `workspace_memberships`, `auth_sessions`,
`workspace_invitations`, `audit_events`, `login_throttle`, `report_files`,
`user_preferences`. Existing conversations were already keyed by `user_id`
and are untouched; the startup baseline gives every pre-existing user a
membership automatically.

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
