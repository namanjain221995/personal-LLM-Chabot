# Platform release regression suite

One command that walks the whole platform the way people use it, in a real
headless Chrome plus plain HTTP, against one base URL:

- sign in, a wrong password, log out (and the session is dead server-side)
- who may open what signed out: `/docs` at every depth is public; `/`, `/api`
  and `/admin/*` redirect to `/login`; `/v1` ignores cookies
- chat: send and stream, the conversation in the sidebar and the history API,
  rename, share link opened by a signed-out visitor, delete
- uploads: a document attached to a first message; a chunked part whose
  connection drops mid-body (HTTP); a 92 MiB document that resumes in the
  browser without resending the part the server already holds
- the artifacts panel, when the account has a generated file to open
- admin: Members, Invitations, Access, the Developer → API platform link, and a
  member being refused
- the API console: every tab, create a project, create a key (secret shown
  once, works on `/v1`), the models list, the playground, revoke the key
  (`/v1` refuses it at once), and `/v1/models` publishing all six model ids
- every page at 360, 768 and 1440 px: no horizontal overflow and no console
  errors. Overflow is measured per element, not only on the document: the chat,
  admin and console shells are `h-dvh overflow-hidden` with panes that scroll
  inside, so their document never gets wider than the screen. A pane that
  scrolls sideways, or content cut off at the screen edge by an
  `overflow:hidden` ancestor, is a failure too (see "What counts as overflow")

Each check is a sentence (`node run.js --list` prints them all). A failure is
kept verbatim, with a screenshot of every page the check had open.

## Install

Node 20+ and the system Google Chrome. `puppeteer-core` is pinned exactly
(`23.11.1`) and never downloads a browser.

```bash
cd e2e/platform
npm ci
npm test          # self-tests: report rendering, stubs, the base-URL guard, and
                  # the overflow / console detectors in real Chrome against
                  # pages with KNOWN defects (including the app-shell layouts)
                  # and against layouts that scroll on purpose
```

## Run it against the isolated e2e stack

The stack from `scripts/e2e-stack.sh` serves the frontend on
`http://127.0.0.1:3001`. The suite needs an admin account (API console,
admin pages) and a member account (chat, uploads). Passwords come from the
environment or from a file named in the environment; there are no defaults.

```bash
cd e2e/platform
export E2E_ADMIN_EMAIL=e2e-devadmin@test.local
export E2E_ADMIN_PASSWORD_FILE=/path/to/admin-password-file
export E2E_MEMBER_EMAIL=e2e-artifacts@test.local
export E2E_MEMBER_PASSWORD_FILE=/path/to/member-password-file

node run.js                                   # everything, ~2.5 minutes
node run.js --only chat,uploads               # suites, or id prefixes
node run.js --only gating.docs --skip responsive
node run.js --widths 360,390,768,1024,1440    # more viewports
node run.js --out /tmp/platform-e2e --label "branch X"
node run.js --skip auth.login-wrong-password  # on a stack someone is signing in to right now
```

Output: `out/<timestamp>/results.md` (the table and every failure verbatim),
`results.json`, and `screenshots/`. Exit code `0` all passed, `1` something
failed, `2` usage error (or a refused base URL, see below).

| Variable / flag | Default | Meaning |
|---|---|---|
| `--base`, `E2E_BASE_URL` | `http://127.0.0.1:3001` | The frontend under test. |
| `E2E_ADMIN_EMAIL`, `E2E_ADMIN_PASSWORD` or `E2E_ADMIN_PASSWORD_FILE` | `e2e-devadmin@test.local`, none | Needs `api.console.access` and `members.read`. |
| `E2E_MEMBER_EMAIL`, `E2E_MEMBER_PASSWORD` or `E2E_MEMBER_PASSWORD_FILE` | `e2e-artifacts@test.local`, none | A plain member with attachments on. |
| `--chat-mode`, `E2E_CHAT_MODE` | `stub` | `stub` or `live`, see "Engine load". |
| `--widths`, `E2E_WIDTHS` | `360,768,1440` | Responsive viewports. |
| `E2E_EXPECTED_MODELS` | the six owner-decided ids | What `v1.models-published` requires. |
| `--only`, `--skip` | all | Comma-separated suite names or check-id prefixes. |
| `--out`, `E2E_OUT`; `--label`, `E2E_LABEL` | `out/<timestamp>` | Where results go; a title for the report. |
| `CHROME` | `/usr/bin/google-chrome` | The browser binary. |
| `--headful`, `E2E_HEADFUL=1` | off | Watch it run. |
| `E2E_ALLOWED_PORTS` | `3001,3002,3900-3999` | Loopback ports that are test stacks. Any other loopback port is refused. |
| `E2E_ALLOW_REMOTE=1` | off | Allow a base URL that is not a loopback address. |
| `E2E_TARGET_FRONTEND_IMAGE`, `E2E_TARGET_ORCHESTRATOR_IMAGE`, `E2E_TARGET_GIT` | empty | What is under test, written verbatim into `results.md` (image id, commit, dirty or clean). |

## Point it at a candidate container

The suite only needs a base URL, so a candidate is any frontend container you
can reach on loopback. The one below runs a candidate frontend image in front
of the e2e stack's orchestrator without touching the shared stack's containers,
network, or ports (the 2026-09-13 candidate run used exactly this):

```bash
# 1. Build the candidate from the tree under test (or reuse an image you have).
docker build --pull=false -f frontend/Dockerfile -t sf-local-ai-frontend:candidate frontend

# 2. Run it on loopback only. Host networking lets it reach the e2e
#    orchestrator on 127.0.0.1:8081; HOSTNAME=127.0.0.1 keeps it off the LAN.
docker run -d --name e2e-platform-candidate-frontend --network host \
  -e HOSTNAME=127.0.0.1 -e PORT=3901 \
  -e ORCHESTRATOR_URL=http://127.0.0.1:8081 \
  --init --restart no sf-local-ai-frontend:candidate

# 3. Run the suite against it, recording exactly what was built, then remove it.
#    A tag says nothing about uncommitted edits in the tree it was built from.
E2E_TARGET_FRONTEND_IMAGE="$(docker image inspect sf-local-ai-frontend:candidate --format '{{.Id}}')" \
E2E_TARGET_GIT="$(git rev-parse --short HEAD) $(test -z "$(git status --porcelain -- frontend)" && echo clean || echo dirty)" \
  node run.js --base http://127.0.0.1:3901 --label "candidate"
docker rm -f e2e-platform-candidate-frontend
```

To test a candidate ORCHESTRATOR too, start it the way `scripts/e2e-stack.sh up`
does (same env file, its own database), on a free loopback port, and set the
candidate frontend's `ORCHESTRATOR_URL` to that port. Do not run
`scripts/e2e-stack.sh up` for this while other people are using the shared stack:
it removes and recreates `techsara-e2e-orchestrator` and `techsara-e2e-frontend`.

## Which base URLs it refuses

The suite writes real data, so `run.js` checks the base URL before it opens a
browser (`lib/config.js`, `baseRefusal`), and exits `2` when:

1. the port is `3000` or `8080`, on any host, whatever the flags. Those are the
   production frontend and orchestrator ports on the platform host, and
   production publishes them on its loopback address too, so "loopback" alone
   does not mean "a test stack". A candidate can use any other port.
2. the host is a loopback address (`127.0.0.0/8`, `::1`, `localhost`, checked as
   IP literals, so `127.example.com` is NOT loopback) but the port is not in
   `E2E_ALLOWED_PORTS`.
3. the host is not a loopback address and `E2E_ALLOW_REMOTE=1` is not set.

## Engine load, and what the stub proves

The e2e stack deliberately shares the production model engines. A regression
run must not load them, so the default `--chat-mode stub` answers the three
engine-bound browser calls *inside the browser*, in the exact wire format the
orchestrator uses:

- `POST /api/chat`: the orchestrator's stream order: the leading
  `meta {generation_id, trace_id, request_id, intent_id, attempt}` (the intent
  id echoed from the request), `status`, `token`s, the engine `meta` carrying
  the same ids, `done`
- `POST /api/history/conversations/<id>/title`: "not generated" (titling asks a model)
- `POST /api/devplatform/playground/execute`: a Responses-API event stream

Everything else is real: history sync, sharing, uploads, the console API,
`/v1` authentication. `chat.send-stream` checks that the send's intent is
stored `completed` with the stream's `generation_id`, that exactly one
assistant message is stored and it carries that `generation_id`, and that after
a reload the answer is on screen once and still stored once.

What stub mode does NOT cover: the orchestrator's own persist of the answer.
In production the `/chat` worker writes the answer keyed by `generation_id`
and the browser's save is reconciled against it. With the stub, the stored
answer comes only from the browser's history save. It also serves the whole
stream in one response, so token timing and the 15-second heartbeat are not
exercised, and it does not prove the engine answers. `--chat-mode live` sends for real: two short chat turns (one carries a
48 KiB text document), their title generation on the router model, and one
playground run. Only use it on a stack whose engines you may load. In live
mode the chat check passes when a Stop button was seen and has gone again, or
when the error page shows. An engine that is down still passes, as long as the
page says so instead of spinning forever.

Uploads are plain text. The 48 KiB and 256 KiB documents are extracted locally,
and the 92 MiB one is above the prewarm cap, so it is only stored. No OCR,
embedding or model runs on any of them.

## What a run writes, and what it cleans up

| Created | Cleaned up |
|---|---|
| Conversations (`Regression check e2e-…`, `e2e-chunk-…`, `e2e-big-…`) with their uploads | Deleted at the end of the run, pass or fail |
| One public share link | Removed with its conversation by `chat.delete` (which checks it stops opening), revoked again at the end, and checked to be closed |
| One API project per run (`e2e project …`) | **Disabled**, because projects cannot be deleted. They accumulate; the console lists the newest 100 |
| One API key | Revoked by `console.key-revoke`, and revoked again at the end whatever happened (registered the moment its secret exists), then checked to be refused by `/v1` |
| HTTP sign-ins | Logged out after each check and after each cleanup |
| One wrong-password attempt | Cannot be undone, see below |
| Fixture files under `out/<run>/fixtures/` | Deleted after each upload check |

Every cleanup checks its HTTP answer. The outcome of each one is printed, and
listed under "Cleanup" in `results.md`. A cleanup that failed makes the run
exit `1` even when every check passed, because it means data or a live key was
left behind.

A check that times out keeps running in the background (a promise cannot be
cancelled). Once the runner moves on, that check's sessions refuse new pages
and requests, so its polling loops stop at their next call.

**The login lockout.** `auth.login-wrong-password` makes one failed sign-in per
run. The orchestrator counts failures per email AND per client address: 8
failures within 15 minutes lock that key for 5 minutes, and a successful
sign-in clears only the email key. On a stack without trusted proxy headers
the client address is the frontend container's, so a lock refuses EVERY
sign-in through that frontend, other people's included. Do not rerun the suite
in a tight loop. If someone else is testing sign-in on the same stack, add
`--skip auth.login-wrong-password`. When the lock is already active, the check
fails with `login throttled (429)` instead of passing, so the failures of
later sign-ins are not blamed on the product.

## What counts as overflow

`measureOverflow` (`lib/browser.js`) looks at every visible element and text
run whose right edge is past the viewport's (the width asked for, or less when
a scrollbar takes some). The nearest ancestor that clips or scrolls
horizontally, and itself fits the screen, decides:

- no such ancestor: **past the right edge of the page**
- `overflow-x: hidden` or `clip`: **cut off at the screen edge** (not when that
  ancestor truncates with an ellipsis)
- `overflow-x: auto` or `scroll`: **scrolls sideways inside** that pane, unless
  the pane scrolls sideways by design: a `pre`, `code`, `table` or `textarea`;
  a `tablist`, `toolbar`, `menubar`, `grid`, `table` or `listbox` role; the
  wrapper of a single table; an element marked `data-scroll-x`; or a short
  strip (under 60% of the viewport height) that does not scroll vertically,
  like a chip row

Ignored: anything under `aria-hidden` or `inert`, invisible elements, and
elements wholly off-screen that were moved there on purpose (out-of-flow or
transformed, like a closed drawer). An in-flow column pushed off-screen by its
siblings is still counted. The failure names the first few elements, how each
one overflows and the container responsible.

## Reading a failure

- `results.md` has one row per check. Under "Failures, verbatim" each failed
  check has its full assertion message, a stack trace pointing into `suites/`,
  any browser console errors, and screenshot paths.
- A check that depends on an earlier one (`chat.rename` needs `chat.send-stream`)
  is SKIPPED, with the reason, when that one failed. Skips never hide as passes.
- `artifacts.panel` SKIPS when the member account has no conversation with a
  generated file. Creating one takes a model generation, and the suite does
  not run those. `scripts/artifact_smoke2.py` makes some.
- Responsive failures list, per width, the overflow in pixels, the first
  offending elements with how they overflow and inside what, and each console
  error with its source line.
- A failure screenshot never shows a freshly created key secret: those
  elements are blanked before the capture.

## Layout

```
run.js              entry point: flags, ordering, cleanups, reports
lib/config.js       flags + environment, the loopback guard
lib/harness.js      registry, runner, verbatim failures, results.md
lib/http.js         fetch client that pins the Secure session cookie as a header
lib/browser.js      Chrome launch, a fresh incognito context per check, overflow meter, console collector
lib/stubs.js        the engine stubs
lib/upload.js       fixtures, hashing, a PUT with a deliberately dropped connection
suites/*.js         auth, gating, chat, uploads, artifacts, admin, console, responsive
tests/*.test.js     self-tests (npm test)
```

## Handling credentials while running it

Container environments on the platform host carry credentials, and the e2e
stack's orchestrator is started from the production environment file. Never
print a container's whole environment (`docker inspect … .Config.Env`, `docker
exec … env`). Filter it through an allowlist of variable NAMES before it
reaches a terminal or a log, for example
`docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(ORCHESTRATOR_URL|PORT)='`.
A redaction `sed` that looks for `PASSWORD=` does not catch a password inside
a `postgresql://user:password@host` URL.

## Known accepted risk: `npm audit`

`npm audit --omit=dev` reports 3 high-severity advisories in `extract-zip`,
through `@puppeteer/browsers` 2.6.1, which `puppeteer-core` 23.11.1 depends on.
`extract-zip` is used only to unpack a browser download, and this suite never
downloads a browser (it launches the system Chrome by path). The first
`puppeteer-core` releases without `extract-zip` (the 25.x line, with
`@puppeteer/browsers` 3.x) require Node 22.12 or newer, and the host this was
built on runs Node 20. Upgrade the pin together with Node.

## Verified 2026-09-13

- Self-tests: 18 of 18 pass (`npm test`), including the app-shell layouts the
  old document-only meter read as clean, and layouts that scroll or hide
  content on purpose, which must stay clean.
- The review's proof, repeated: a 1200px element injected into the main pane
  of `/admin/members` and `/api?tab=keys` at 360px (and a 2000px element into
  the chat pane of `/`) read as 0 before. Now each is reported as
  `scrolls sideways inside main.min-h-0.flex-1.overflow-y-auto` (chat:
  `div.relative.min-h-0.flex-1.overflow-y-auto`), and the same pages read clean
  without the injection.
- e2e stack, stub mode (`sf-local-ai-frontend:e2e`, built before the
  public-docs and admin-rail commit): **36 passed, 7 failed**; cleanups 7 done,
  0 failed.
  - Stale image: `gating.docs-public-every-depth`,
    `gating.docs-browser-signed-out`, `responsive.docs-index`,
    `responsive.docs-page`, `admin.api-platform-link`.
  - `v1.models-published`: `/v1/models` lists only `techsara-35b`.
  - `responsive.admin-invitations`, a real defect the old meter could not see:
    at 360px the status filter strip (`div.h-10.mt-6.flex.w-fit`) ends 3px past
    the screen edge, so the main pane scrolls sideways.
- A throwaway candidate frontend on 127.0.0.1:3901 from
  `sf-local-ai-frontend:responsive-reaudit` (built from a worktree with
  uncommitted frontend edits, so NOT a build of any commit), in front of the
  same e2e orchestrator: **40 passed, 1 failed, 2 skipped**; cleanups 6 done,
  0 failed. All 14 responsive pages were clean at 360, 768 and 1440px, the docs
  included. The failure is `console.key-create`: "no secret was shown after
  Create key; the dialog says: 'embeddings.write' is not a valid scope.", a
  newer frontend asking an older orchestrator for a scope it does not know. The
  two key-dependent checks were skipped.
- After both runs: 0 leftover e2e conversations on the member account; every
  e2e project disabled; no unrevoked key in any of them.
- Not exercised: `--chat-mode live`, because the only stack available shares
  production engines.
