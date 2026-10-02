# Chat media: every upload is stored on our server and shows on every device

Status: build contract, 2026-10-02. Branch `feat/chat-media`, worktree
`/home/techsphere/Documents/project/personal-LLM-Chabot-chatmedia`, based on origin/dev 21800739
(schema V43). This feature takes **V44**.

## 1. The defect

A photo sent from a phone showed above the user bubble on the phone. The same chat opened on a
desktop (same account) showed the question and the answer but no photo.

- The image exists only as `ChatMessage.imageDataUrl(s)` in the sending browser's IndexedDB
  (`frontend/lib/idbCache.ts`, `images` store). `frontend/lib/history.ts` `toServerPayload()` sends
  role/content/meta/feedback only.
- The bytes reach the orchestrator only inline in the `/chat` body. `_request_snapshot` strips
  them. `engines/image_memory.py` (V41 `conversation_images`) keeps the latest image per chat for
  2 h for model follow-ups only.
- Production evidence: user message 16135 in conversation 4ab7ac45-... has meta keys
  `branch,intent` only; 0 uploads rows; 0 conversation_images rows.
- Side defects in the same area:
  - RC-3a: a document attached AFTER an image is dropped. `isPdf = attachments[0]?.kind === 'pdf'`
    and `pdfName`/`meta.attachments` are built only when the first attachment is a document.
  - RC-3b: regenerate/edit/retry on a device without the local bytes silently sends no image
    (`attachmentsForResend` returns `images: []`, `missing: false`).
  - RC-3c: the background document upload writes its `id` back with the turn list captured at send;
    landing after the answer it can shrink the thread, the PUT gets 409, and the `id` is lost.
  - Document / dataset / zip originals are deleted by the 24 h workspace sweep (dataset originals at
    once, `uploads.py` after extraction), so a second device sees "expired" on a live chat.

## 2. Owner decisions (2026-10-02)

- Every upload (image, video, document, dataset, audio, anything) is kept on our server for the life
  of the chat and shows in the chat on any device or browser after login, like ChatGPT.
- Storage: a **Postgres row per item** (owner, chat, sha256, mime, size); the **bytes on disk** under
  the `/data` volume. Not bytea.
- Must be fast: thumbnails for the chat bubble, long private immutable caching, nothing added to the
  latency of a send.
- The AI must be able to read stored images later: follow-ups after the 2 h V41 TTL and after
  restarts, and regenerate/edit/retry from another device, load the bytes server-side.
- Defaults taken (owner did not object; revisit if asked): keep for the life of the chat, no per-user
  quota beyond the disk free-space floor; old photos are backfilled by the browser that still holds
  them; deleting a chat removes its stored bytes promptly (best effort) with a reaper as backstop. The
  UI never promises an erasure time.

## 3. Storage: images

### V44 table `chat_media`

```sql
CREATE TABLE IF NOT EXISTS chat_media (
    media_id        text        PRIMARY KEY,           -- 32 lowercase hex, uuid4().hex, server-minted
    user_id         integer     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id text        NOT NULL,
    attachment_id   text        NOT NULL,              -- client-minted, ^[A-Za-z0-9_-]{8,64}$
    sha256          text        NOT NULL,              -- of the full file as stored
    mime            text        NOT NULL,              -- image/jpeg | image/png | image/webp | image/gif
    bytes           bigint      NOT NULL,
    width           integer,
    height          integer,
    has_thumb       boolean     NOT NULL DEFAULT false,
    source          text        NOT NULL,              -- 'chat' | 'upload' | 'backfill'
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chat_media_owner_attachment UNIQUE (user_id, conversation_id, attachment_id),
    CONSTRAINT chat_media_source CHECK (source IN ('chat','upload','backfill'))
);
CREATE INDEX IF NOT EXISTS chat_media_conversation ON chat_media (conversation_id);
```

Additive only (migrations are forward-only once deployed). Update `LATEST_SCHEMA_VERSION` users:
`tests/test_api_platform_db.py` (43 -> 44), `tests/conftest.py _APP_TABLES`, `_SIDE_TABLES` in
db.py (so `delete_conversation` deletes the rows), and `schema_parity.py invariants` must pass.

### Bytes on disk

- Root: `CHAT_MEDIA_DIR`, default `/data/chat-media` (inside the persistent `sf-local-ai_data`
  volume, OUTSIDE `WORKSPACE_DIR`, so the 24 h workspace sweep and the 20 GB quota never touch it).
- Layout: `<CHAT_MEDIA_DIR>/<user_id>/<conversation_id>/<media_id>/full.<ext>` and, when made,
  `thumb.webp`.
- Write order: temp file in the same directory, fsync, rename, fsync the directory; THEN insert the
  row. On a UNIQUE conflict the existing row wins (first write wins) and the new files are removed.
  A media URL therefore never changes content, which is what makes immutable caching safe.
- Accepted types: magic bytes AND a Pillow decode (`Image.open(...).verify()` then a real open for
  size) must agree on jpeg, png, webp or gif. Everything else (heic, svg, bmp, tiff, html renamed
  .png, truncated files) is refused: 415 on the upload route; skipped with a metric on the `/chat`
  path (the chat itself never fails because of storage).
- Limits: <= 10 MiB decoded per image as sent (the composer's `MAX_IMAGE_BYTES`; it shrinks the
  original, which may be any size), and since 2026-10-03 no count limit: 999 per request is only
  the technical ceiling (`MAX_IMAGES`, LIMITS.md); a request is bounded by its bytes (48 MiB batch
  budget). Pillow's decompression-bomb guard stays on.
- Thumbnail: when the long edge > 512 px or the file > 200 KB, write `thumb.webp`, long edge 512 px,
  quality 80, EXIF orientation applied, alpha kept, first frame for GIF. Otherwise `has_thumb=false`
  and `size=thumb` serves the full file.
- Free-space floor: `CHAT_MEDIA_MIN_FREE_GIB` default 250 (the project's convention). Below it: 507
  `{"code":"insufficient_storage"}` on the upload route; skip + metric on the `/chat` path.
- All file work runs off the event loop (`asyncio.to_thread` or the existing CPU pool).

## 4. Orchestrator routes (new module `orchestrator/app/chat_media.py`, router included in main.py)

Common rules for every route below:
- `require_user`. `conversation_id` must match `^[A-Za-z0-9_-]{1,64}$`. The F034 refusal
  (`^u\d+-` reserved keys) applies exactly as `uploads.py` / `history.py` / `main.py` apply it.
- Ownership: the row's `user_id` must equal the viewer AND `db.conversation_owner(conv)` must be the
  viewer or None (a brand-new chat whose row the history PUT has not created yet). Anything else is
  a 404 with the same body as "never existed".

1. `POST /chat-media/{conversation_id}` — multipart/form-data. Repeated parts `file` (<= 5) and
   repeated text parts `attachment_id` in the same order (counts must match: else 400). Optional text
   part `source` = `upload` (default) or `backfill`.
   200: `{"items":[{"attachment_id","media_id","mime","width","height","bytes","sha256","created"}]}`
   — `created:false` when (user, conv, attachment_id) already existed (idempotent retry; the stored
   item is returned unchanged). Errors: 400 bad shape, 404 not yours, 413 too large, 415 unsupported
   type, 507 insufficient storage. Add a `body_cap_for` entry of 64 MiB and pin it in
   `tests/test_orchestrator_hardening.py`.
2. `GET /chat-media/{conversation_id}` — the viewer's items for that chat:
   `{"items":[{"attachment_id","media_id","mime","width","height","bytes","created_at"}]}`.
3. `GET /chat-media/{conversation_id}/{attachment_id}?size=thumb|full` (default `full`).
   200 with the bytes. Headers: `Content-Type` = row mime (`image/webp` for a thumb),
   `Content-Length`, `X-Content-Type-Options: nosniff`,
   `Content-Security-Policy: default-src 'none'; sandbox`,
   `Content-Disposition: inline; filename="image.<ext>"`,
   `Cache-Control: private, max-age=31536000, immutable`, `ETag: "<sha256>"` (full) or
   `"<sha256>-t"` (thumb). `If-None-Match` matching -> 304 with no body. 404 for not found / not
   yours / F034. 410 `{"code":"media_missing"}` only when the row exists and the file does not.
   Stream the file (FileResponse), never read it into memory whole.
4. Super-admin inspection: `GET /admin/members/{user_id}/chat-media/{conversation_id}/{attachment_id}?size=`
   behind `_inspectable_member` in `authn/admin_api.py`, audited exactly like the other inspection
   reads, same headers as (3). Inline is acceptable here because only verified rasters are stored.

## 5. `/chat` changes (main.py `ChatRequest`)

- `image_ids: Optional[List[str]] = Field(default=None, fail_fast=True)` — the attachment_ids of the
  inline images, index i <-> `images[i]` (or `[0]` <-> the single `image`/`image_base64`). Each must
  match the attachment_id pattern; a length mismatch is ignored for storage (logged), never a 4xx.
  When present, after the ATTACHMENTS feature gate and the F034 check, the server stores every inline
  image with `source='chat'` in a background task that never delays the first token and whose failure
  is logged + counted, never surfaced as a chat error. This applies to EVERY route that carries images
  (vision, document+image, artifact, ...), so hook it at request intake, not inside one engine branch.
- `image_refs: Optional[List[str]] = Field(default=None, fail_fast=True)` — attachment_ids of images
  ALREADY stored for (viewer, conversation), sent INSTEAD of bytes (regenerate/edit/retry on a device
  without local bytes). Before routing, the server loads them (thumb is NOT used; the full file) and
  treats them exactly like inline `images` (same order, same `MAX_IMAGES` cap counted together with
  inline images). If any ref cannot be loaded, answer HTTP 422
  `{"detail":{"code":"image_ref_missing","missing":["<attachment_id>", ...]}}` BEFORE the stream
  starts, so the client can ask the person to attach the image again.
- Both fields are excluded from `_request_snapshot` like the inline bytes (the ids may stay; no bytes).

## 6. The AI reads stored images later (`engines/image_memory.py`)

V41 semantics stay as they are. Add a fallback in `hydrate`: when neither the process nor the V41 row
has a live entry for (viewer, conversation), find the newest USER message in that conversation's
stored history (`messages` table) whose `meta.images` is non-empty; load those attachment_ids'
full files from `chat_media` (viewer-scoped); context = that message's content + the next assistant
message's content (lowercased, as `remember` stores it); turns-since = the number of user messages
after it. Then the existing word test runs unchanged. Gate: env `IMAGE_MEMORY_STORE_FALLBACK`
(default on). A conversation with no stored images behaves exactly as today.

## 7. Reference on the user message: `meta.images` (written by the browser)

```ts
meta.images?: Array<{ attachment_id: string; name?: string; mime?: string; width?: number; height?: number }>
```

- Written by the browser at send, on the user message, in the same order as the images sent. It rides
  the first history push. It never contains data URLs, server ids, progress, or timestamps (the
  history `syncKey` hashes all of `meta`; volatile fields would cause push storms).
- `imageDataUrl(s)` stay browser-only exactly as today (instant local render; the P2-04b pin in
  `frontend/tests/upload-id-persistence.test.ts` stays true).
- Display URL is derived, never stored: `/api/chat-media/{conversationId}/{attachment_id}?size=thumb|full`.
- `sharing.py PRIVATE_META_KEYS` gains `"images"` (a chat with stored photos is private like one with
  `attachments`).

## 8. Deletion

- `DELETE /history/conversations/{id}` (history.py): rows go with `delete_conversation`
  (`_SIDE_TABLES`); then, off the event loop and best effort, `chat_media.erase_conversation(conv,
  user_id)` removes `<CHAT_MEDIA_DIR>/<user_id>/<conv>` and `uploads.erase_conversation_files(conv)`
  removes `<CHAT_FILES_DIR>/<conv>` (section 9). Failures are logged + counted; the reaper finishes.
- Reapers (bounded cadence, at most once per `CHAT_MEDIA_REAP_INTERVAL_S` default 3600 per process,
  started like the other background sweeps): a directory or row whose conversation has no
  `conversations` row (or whose user is gone) AND that is older than `CHAT_MEDIA_ORPHAN_GRACE_H`
  (default 24) is removed. The grace exists because `/chat` stores media before the first history PUT
  creates the conversation row.
- Account deletion: `ON DELETE CASCADE` removes rows; the reaper removes the directories.

## 9. Storage: every other upload kind (lasting originals)

- Root: `CHAT_FILES_DIR`, default `/data/chat-files`, outside `WORKSPACE_DIR`.
- When an upload finishes (single-shot and chunked complete) for purposes `document` and `dataset`
  (zip/tar travel as `document`): hard-link (same filesystem; fall back to copy) the original to
  `<CHAT_FILES_DIR>/<conversation_id>/<upload_id>/original` and keep the original filename in the
  row as today. A dataset original is no longer simply deleted after extraction: its lasting copy is
  made first. Below the free-space floor (`CHAT_MEDIA_MIN_FREE_GIB`), skip the lasting copy (old
  behaviour) and count it.
- Videos and audio already last in `VIDEO_DATA_DIR/<sha256>/source.<ext>` while attached (origin/dev
  has the `_video_source` fallback in `download_upload`); no copy for them.
- `GET /uploads/{conv}/{id}/file` order: workspace `_original` -> lasting copy -> video store -> 410.
  The admin download route gets the same fallback. Range requests answer 206 (needed by the
  `<video>`/`<audio>` players); check what Starlette's FileResponse already does before writing any.
- `myfiles.py` availability: an item with a lasting copy is `available`, not `expired`.
- `uploads.erase_conversation_files(conversation_id)` removes `<CHAT_FILES_DIR>/<conv>`; a reaper
  like section 8 covers orphans.
- My files lists stored chat images too (kind `image`, from `chat_media`, viewer-scoped, with the
  thumbnail URL); every UNION branch aliases its own columns (see docs/MY-FILES.md trap 1).

## 10. Frontend

### Images (send, render, resend, backfill)
- Send: the user message gets `meta.images` (section 7) from the composer's image attachments
  (`attachment_id` is already minted at selection). The `/chat` body gets `image_ids` parallel to
  `images` through `lib/streams.ts`, `lib/orchestrator.ts` and `app/api/chat/route.ts`.
- Render (`MessageRow.tsx`): local `imageDataUrl(s)` when present (instant); otherwise, per
  `meta.images` entry, `<img src=".../api/chat-media/{conv}/{attachment_id}?size=thumb" width height
  loading="lazy" decoding="async">` with the stored width/height to avoid layout shift; click opens the
  full image (`size=full`) in the existing preview. 404/410 -> an "Image unavailable" tile. A legacy
  turn (no `meta.images`, no local bytes) whose next assistant message has `meta.route === 'vision'`
  shows one muted line: "Photo not stored on the server (sent before photos were saved), so it only
  shows on the device that sent it."
- Resend (`lib/attachments.ts attachmentsForResend` and callers): local bytes when present; else
  `meta.images` -> send `image_refs`; else today's `missing` path. A 422 `image_ref_missing` shows the
  existing re-attach notice.
- Backfill (old photos): when a chat is opened in a browser whose IndexedDB still holds
  `imageDataUrl(s)` for a user message that has no `meta.images`, upload them to
  `POST /api/chat-media/{conv}` with `source=backfill` and deterministic attachment ids
  `bf-<first 32 hex of sha256(dataUrl)>`, then set `meta.images` on that message and persist through
  the normal store path (one push per chat, re-applied after a 409). Single-flight per conversation
  across tabs (`navigator.locks` when available), at most 3 chats per idle tick, never blocks
  rendering, stops on 507 or offline. Only the viewer's own normal chats (not shared views, not admin
  inspection).
- Next proxies (new): `frontend/app/api/chat-media/[conversation]/route.ts` (GET list, POST multipart
  streamed with `duplex: 'half'`, 64 MiB cap, cookie forwarded) and
  `frontend/app/api/chat-media/[conversation]/[attachment]/route.ts` (GET bytes streamed; forwards
  `If-None-Match`; relays status 200/304/404/410 and the headers in section 4.3). Never use
  `proxyToOrchestrator` for bytes (it buffers, 32 MiB cap). Validate both path segments before any
  fetch. Never name a folder `data` (unanchored `.gitignore` rule).
- Admin inspection of a member's chat uses the admin route (section 4.4) if that view renders
  `MessageRow`.

### Other kinds
- RC-3a: decide `isPdf`/`pdfName`/`meta.attachments` from the DOCUMENT attachments, not
  `attachments[0]`; keep separate index spaces for images and documents.
- RC-3c: the background document upload patches the LATEST stored copy of that user message by
  `attachment_id`, never a list captured at send.
- Video/audio: `<video controls>` / `<audio controls>` with `src` = the uploads file URL (no blob
  fetch); the uploads file proxy forwards `Range` and relays `206`, `content-range`, `accept-ranges`.
  CSP: same-origin media must be allowed (`media-src 'self'` if `default-src` would block it).
- After expiry, a document still previews from its stored text; the xlsx "no longer available in this
  browser session" message is replaced by the honest expired wording; files with no preview kind are
  not downloaded just to say "no preview".

## 11. Closed registries to update

- `metrics.py _LABELS_BY_METRIC` (closed): `chat_media_writes_total{source,result}` with result in
  `stored|duplicate|unsupported|too_large|no_space|error`; `chat_media_reads_total{size,result}` with
  result in `ok|not_modified|not_found|missing`; `chat_media_write_seconds{source}`;
  `chat_files_lasting_total{purpose,result}` with result in `stored|no_space|error`. Document them in
  `docs/MONITORING.md`.
- `body_cap_for` + its pinned test. `ChatRequest` list fields with `fail_fast=True`.
- SSE: no new event names.

## 12. Working rules for every agent on this branch

- Work only in this worktree. Never touch the main checkout
  (`/home/techsphere/Documents/project/personal-LLM-Chabot`, the production deploy root) or
  `personal-LLM-Chabot-devapi`.
- No `git stash`, `checkout`, `reset`, `rebase`, `push`, or `commit -a`. Commit only the paths you
  own: `git add <paths> && git commit -m "..."` (plain English; end with the
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` line). If `.git/index.lock` exists, wait
  and retry. Commit early.
- Python: `/home/techsphere/Documents/project/personal-LLM-Chabot/orchestrator/.venv/bin/python`,
  run from `orchestrator/`. Test DB: your own database on the `pg-test-hand` container
  (`postgresql://postgres:postgres@127.0.0.1:55432/<name>_test`; create it with
  `docker exec pg-test-hand createdb -U postgres <name>_test`). Never share a test DB between two
  concurrent pytest runs (fixtures truncate).
- Frontend: `cd frontend && npx vitest run <files>`, `npx tsc --noEmit`, `npm run lint`.
- Late facts or decisions go in `docs/chat-media/NOTES.md` (append, with your track name).
