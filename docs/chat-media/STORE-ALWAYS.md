# The server stores every photo it receives, whatever page version sent it (2026-10-03)

## What happened in production (01:59 IST, 2026-10-03, 14 minutes after PR #94 went live)

- The owner sent an invoice photo from the phone (conversation `b6bd792d-…`, intent
  `248ff0ab…`). The answer took 7.1 s (chat_requests created 20:29:04.7Z, updated 20:29:11.8Z).
- The `/chat` request carried NO `image_ids`: the stored request snapshot has keys
  `mode,agent,model,effort,message,sf_live,messages,intent_id,session_id,web_search,deep_research,conversation_id`.
  The browser tab had been opened BEFORE the deploy (the frontend container restarted at 20:14:29Z),
  so it still ran the old JavaScript, which never stores photos and never writes `meta.images`.
- `chat_media_writes_total{source="chat"}` stayed 0. The only stored row is a BACKFILL 36 s later
  (`bf-217efc…`, 20:29:40Z) after the page reloaded with the new code.
- Meanwhile, the chat showed "Photo not stored on the server (sent before photos were saved)…".

The owner's requirement: when a person uploads, the photo is stored at once, every time; showing and
reading it is fast. Storage must not depend on which version of the page sent the photo.

## What to build

### 1. Server: store every inline image it receives (orchestrator)

- In the `/chat` intake hook that stores inline images (`chat_media`, source `chat`), when the request
  carries inline images but no usable `image_ids` (absent, or a count mismatch), mint the ids on the
  server instead of skipping:
  - with a valid `intent_id` (32 hex): `ix-<intent_id>-<index>` (index 0..N-1 in send order);
  - without one: skip as today (no stable way to link it), and count it.
  The pattern `^[A-Za-z0-9_-]{8,64}$` admits these. Never mint for a ref (`image_refs`) turn.
- Same background store, same checks, same metrics (add a `result` or `source` label value only if
  the closed registry allows it cleanly; otherwise reuse `source="chat"`).
- `GET /chat-media/{conv}` already lists the viewer's items with `attachment_id`; the frontend uses it
  to find `ix-` items.
- `image_memory` store fallback: today it finds the newest user message with `meta.images`. Also
  accept a user message whose `meta.intent.id` has `ix-<intent>-*` rows in `chat_media` (viewer-scoped),
  so the AI can re-read such a photo later.

### 2. Frontend: show a photo stored this way, on every device

- For a user message with no `meta.images` and no local bytes, and a `meta.intent.id`: look up the
  conversation's stored items (one `GET /api/chat-media/{conv}` per conversation per page load, cached;
  never one request per message) and render the items whose `attachment_id` starts with
  `ix-<intent.id>-`, ordered by index, exactly like `meta.images` thumbs (thumb URL, click opens full).
  Then write `meta.images` for that message once (ids, mime, width, height from the list) through the
  normal store path, so later loads need no lookup and `image_refs` work for regenerate/edit/retry.
  Re-apply after a 409 like the backfill does; one push per conversation.
- The legacy note ("Photo not stored on the server…") shows only when this lookup finds nothing.
- Backfill on the sending browser: before uploading `bf-` copies, check the same list; if `ix-<intent>-*`
  items exist for that message, adopt them (write `meta.images` with those ids) instead of uploading a
  duplicate.

### 3. Frontend: an old tab must not keep running old code after a deploy

- Expose the running build's id to the client and a cheap `GET /api/version` (no auth needed, no
  secrets; `cache-control: no-store`) that returns the server's current build id. Use Next.js's build
  id (`.next/BUILD_ID` / `process.env` set at build) or `NEXT_DEPLOYMENT_ID`; pick what works with the
  production Dockerfile and `next start`.
- The client checks on `visibilitychange` to visible, on window `focus`, and at most every 5 minutes
  while visible. When the server's id differs from the page's: if the composer is empty (no draft text,
  no attachments) and nothing is streaming, reload at once; otherwise show a small non-blocking banner
  "A new version is available — Reload" and reload automatically after the current send finishes and
  the composer is empty. Never lose a draft: the existing draft persistence (if any) must survive the
  reload; if draft text is not persisted today, persist it in sessionStorage across this reload only.
- No check storms: one in flight at a time; failures are silent.

## Tests (must fail before, pass after)

- Backend: `/chat` with inline images, a valid `intent_id`, no `image_ids` stores `ix-<intent>-0..N-1`
  rows; with `image_ids` it behaves exactly as today; with no intent id nothing is stored and it is
  counted; a ref turn never mints; ownership and F034 unchanged; image_memory fallback finds an
  `ix-` photo after the V41 entry is gone.
- Frontend: a second device (fresh store) shows the `ix-` photo for a message without `meta.images`
  and then writes `meta.images` once (count PUTs); the legacy note only when the list has nothing; the
  sender's backfill adopts `ix-` rows instead of uploading; `/api/version` mismatch with an empty
  composer reloads, with a draft shows the banner and keeps the draft; equal ids do nothing.
