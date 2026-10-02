# Chat media: late facts and decisions between tracks

Append under your track's heading. Newest facts at the bottom of a section.

## be-media (orchestrator: V44, routes, /chat, image_memory, deletion)

Routes and bodies, as built:

- The admin route lives under the admin router's prefix:
  `GET /admin/api/members/{user_id}/chat-media/{conversation_id}/{attachment_id}?size=thumb|full`.
  CONTRACT §4.4 writes it as `/admin/members/...`; that is shorthand. Gate:
  `Cap.WORKSPACE_CONTENT_READ` + `_inspectable_member` (an admin never reads a
  super admin; 404, no audit). Each 200 and 304 writes the audit action
  `admin_viewed_chat_media` (resource_type `chat_media`, resource_id the
  media_id, meta `{conversation_id, size}`). An admin audit-log view that maps
  action names to words will want a label for it.
- Every refusal of the `/chat-media` routes has the flat body
  `{"code": ..., "detail": "<sentence>"}`. Codes: `not_found` 404 (one body for
  someone else's chat, the F034 key, a malformed id and a missing picture),
  `bad_request` 400, `too_large` 413, `unsupported_type` 415,
  `insufficient_storage` 507, `media_missing` 410, `store_failed` 500. Two
  exceptions keep the upload rail's `{"detail": "..."}`: 403 when the account
  may not attach (the same `uploads.require_attachments` gate as `/uploads`;
  a backfill should stop on 403 as on 507) and 408 when the body was cut off.
- More than 5 `file` parts is 400 (Starlette's own form limit, re-wrapped as
  `bad_request`).
- `POST /chat-media/{conv}` does NOT claim an unowned conversation id (unlike
  `uploads._own`). A chat with no `conversations` row is allowed; rows are
  scoped to the viewer, so each person sees only their own under such an id.
- `size=thumb` for a picture with no thumbnail (512 px or smaller and 200 KiB
  or less) serves the FULL file, with the full file's mime and the full
  file's ETag `"<sha256>"` (not `-t`): the tag always names the bytes sent.
- The stored `width`/`height` are the DISPLAY dimensions: EXIF orientations
  5-8 are swapped, as a browser shows the full image.

/chat:

- `image_ids` and `image_refs` hold at most 5 ids each, each matching
  `^[A-Za-z0-9_-]{8,64}$`; anything else is pydantic's 422 (a malformed body).
  A COUNT mismatch between `image_ids` and the inline images is not a 4xx:
  nothing of that turn is stored, and it is logged.
- `image_ids` indexes the INLINE pictures only (`images`, or the single
  `image`/`image_base64`), never the refs.
- Merge order: pictures loaded from `image_refs` come FIRST, in the order
  sent, then the turn's inline `images`. The 5-picture cap counts both
  (pydantic 422 "at most 5 images per message").
- A turn with only `image_refs` and no text is valid.
- The 422 for an unloadable ref is exactly
  `{"detail": {"code": "image_ref_missing", "missing": ["<id>", ...]}}`, in
  the order sent, deduplicated. It comes before any stream, any
  `chat_requests` row and any engine call. A ref to another account's picture,
  or to a row whose file is gone, is "missing" too.
- An account without the ATTACHMENTS feature has `image_refs` and `image_ids`
  cleared with the rest of its attachments (the usual "Photos and files"
  notice), never a 422.
- The store runs BEHIND the turn. A second device that opens the chat within
  that window gets 404 for the picture until the row lands (milliseconds to
  seconds). The frontend's "Image unavailable" tile covers it; one retry a few
  seconds later would hide it.

image_memory:

- The store fallback runs only when neither the process nor the V41 row has a
  live entry. `turns_after` = user messages after the picture's message, NOT
  counting a trailing unanswered user message (the turn being asked now, when
  the history push beat /chat). Not written back to the V41 row.

For the files track (CONTRACT §9):

- `settings.chat_files_dir` exists (default `/data/chat-files`); the floor is
  `settings.chat_media_min_free_gib` (GiB, float). `chat_media.has_room()`
  measures CHAT_MEDIA_DIR only; the lasting copy needs its own probe of
  CHAT_FILES_DIR (`shutil.disk_usage`, nearest existing ancestor) against the
  same floor.
- `uploads.erase_conversation_files(conversation_id)` exists (removes
  `<CHAT_FILES_DIR>/<conv>`, counts `chat_media_erase_total{store="files"}`)
  and history.py's delete route already calls it, after the response.
- `chat_files_lasting_total{purpose,result}` is registered: purpose in
  {document, dataset}, result in {stored, no_space, error}.
  `metrics.inc("chat_files_lasting_total", "<help>", purpose=..., result=...)`.
- There is NO reaper for CHAT_FILES_DIR yet. `main.py` starts
  `chat_media.reap_loop()`; the files track can add its pass there or start
  its own loop beside it.
- tests/conftest.py now points `settings.chat_media_dir` and
  `settings.chat_files_dir` at the test's tmp_path for every test, and sets
  `settings.chat_media_min_free_gib = 0.0`: a hosted CI runner has ~84 GiB
  free, under the 250 GiB floor, so a store test that passes locally would be
  refused on CI. A test OF the floor sets it back itself (see
  test_chat_media_api.test_below_the_free_space_floor_new_bytes_are_507,
  which also monkeypatches `shutil.disk_usage`).

Metrics beyond CONTRACT §11 (closed, in `metrics._LABELS_BY_METRIC`):
`chat_media_erase_total{store=media|files, result=ok|error}` (chat deletion's
immediate erase) and `chat_media_reaped_total{kind=row|dir}`.

Known gap, not fixed here (sharing.py beyond PRIVATE_META_KEYS is not this
track's): `sharing.evaluate` inspects only messages with non-empty content
(`_is_shareable_message`). A user message that carries ONLY a picture and no
text is skipped, so its `meta.images` does not block a public link. The same
holds for `meta.attachments` today. No picture can leak (the snapshot is an
allowlist); the vision answer's text can. A frontend that sends a non-empty
content for a picture-only turn, or a later change to `evaluate`, closes it.

Operational switch: `CHAT_MEDIA_MIN_FREE_GIB` set far above the disk size
stops all new writes (507 / skipped) with no deploy; reads, deletion and the
reaper keep working. `IMAGE_MEMORY_STORE_FALLBACK=0` turns off the follow-up
fallback.

Late fact, for anyone who relies on Pillow to reject a cut file (found by the
related-suite run, 2026-10-02): WeasyPrint sets the process-wide
`PIL.ImageFile.LOAD_TRUNCATED_IMAGES = True` when it is imported
(weasyprint/images.py), and the artifact renderer imports it inside the
orchestrator. From then on Pillow decodes a JPEG cut to a third, or a GIF cut
anywhere, without an error. `chat_media.inspect` therefore checks the file's
own end structure first (`_ends_whole`: JPEG EOI after the last scan, PNG
IEND+CRC, GIF block walk to the trailer, WebP RIFF size) and never toggles the
switch (the renderer's threads rely on it). Any other check in this process
that trusts `load()` to catch truncation has the same hole.

Read after the frontend notes below: the frontend never sends inline images
and `image_refs` in one request, so the "refs first" merge order only matters
to other clients. Nothing here needs to change for fe-images or fe-files.

## fe-images (frontend: meta.images, render, resend, backfill, proxies, RC-3a, RC-3c)

What the browser sends and stores, as built:

- `meta.images` entries are `{attachment_id, name?, mime?, width?, height?}`
  and nothing else. `mime` is the type of the data URL actually sent (the
  composer re-encodes large photos to JPEG or PNG), `width`/`height` the
  pixels as sent and drawn (EXIF applied), measured by the composer at attach
  time and simply absent when a send beats the measurement. A backfilled entry
  has no `name`; its `mime`/`width`/`height` come from the POST response.
  Built by `lib/chatMedia.ts imagesMetaFor`; never rewritten afterwards.
- `image_ids` goes on the /chat body only when it pairs index for index with
  the INLINE images (`image` alone for one photo, `image` + `images` for
  several). It is never sent next to `image_refs`, and the frontend never
  sends inline images and refs in one request: a resend uses this tab's bytes
  when it has them (with their ids), otherwise refs for every photo.
- The Next chat proxy forwards `image_ids` / `image_refs` only when every id
  matches `^[A-Za-z0-9_-]{8,64}$` and there are 1..5 of them (all or nothing),
  so the orchestrator's pydantic 422 for a malformed id is never reached from
  this app. A wordless resend with only `image_refs` gets the image-only
  prompt. The proxy relays exactly one refusal beyond a category: a 422 whose
  `detail.code` is `image_ref_missing` becomes `{"code":"image_ref_missing",
  "missing":[ids]}` (ids filtered to the id shape). The browser then withdraws
  the send (no error row, nothing persisted) and shows the existing
  "Re-attach the file to regenerate/edit/retry ..." toast.
- `/api/chat-media/[conversation]` (GET list, POST multipart) and
  `/api/chat-media/[conversation]/[attachment]?size=` (GET bytes) are the only
  browser paths. The bytes route forwards the cookie, `If-None-Match` and
  `accept-encoding: identity`, forwards ONLY `size` upstream (the bubble's one
  retry adds `&retry=1`, which never reaches the orchestrator), relays
  200/304/404/410 and the §4.3 headers, and always sets nosniff and
  `default-src 'none'; sandbox` on a 200 even if upstream omits them. Checked
  in a real Chromium against a fake orchestrator: Next passes the
  `private, max-age=31536000, immutable` Cache-Control through unchanged, and
  after a reload no thumbnail request reaches the orchestrator.
- POST refuses a `Sec-Fetch-Site` other than same-origin/none (403) before any
  fetch, like the recording proxy: a multipart form post is a simple request.

Backfill (lib/chatMedia.ts createBackfill, hosted only by ChatApp):

- One POST per user turn (<= 5 photos), parts interleaved `file`,
  `attachment_id`, ..., then `source=backfill`; the same picture twice in one
  turn is sent once (the server refuses a repeated id) and referenced twice.
- Outcomes: 400/413/415 = refused for the rest of the page session (not
  retried until a reload); 404/5xx = try at the next open; 401/403/507 or no
  WebCrypto (plain-http LAN) = stop the backfill for the page's life;
  a network failure = drop the queue until a chat is opened again.
- It reads the photos from IndexedDB's write-once `images` store through the
  new `history.localImages` (the in-memory thread loses them whenever a
  hydrate replaces it) and writes through the new `history.amendMessages`,
  which does not touch `updatedAt`, so an old chat does not jump to the top of
  Recents.

History invariants other tracks may rely on:

- `meta.images` is carried FORWARD and never erased by the browser: the
  store's `saveMessages`, its 409 recoveries (conversation changed and
  shrink) and the reload reconcile all put back a reference that only the
  older copy is missing (`threadReconcile.withStoredImages`, matched by
  position + user role + identical content). A server-written `meta.images`
  would still be overwritten by a client push; nothing server-side should
  write it.
- Photos and documents are held in separate index spaces in the tab
  (`rememberAttachmentFiles(..., 'image')`), and an internal drag of a photo
  carries `space: 'image'`. `uploadRefFor(index)` counts among
  `meta.attachments` only.

Not done here, for whoever owns them:

- The admin transcript viewer (`app/admin/members/[id]/conversations/[cid]`)
  does not render MessageRow or any attachment, so nothing in the frontend
  calls the admin chat-media route yet. A viewer that wants photos needs its
  own STREAMING proxy for `/admin/api/members/{uid}/chat-media/...`: the
  generic `/api/admin/[...path]` rides `proxyToOrchestrator`, which buffers and
  caps at 32 MiB.
- be-media's sharing gap stands: a photo-only turn is still sent with empty
  `content` (the bubble shows no text and the model gets the image-only
  prompt). Closing it belongs in `sharing.evaluate`, not in invented content.
- `AttachmentPreview` gained a `missing` kind ("This photo is no longer stored
  on the server.") for a stored photo whose full size answers 404/410. The
  video/audio player and the expired-document wording (CONTRACT §10 "Other
  kinds") are not in this track.

## fe-files (frontend: players, uploads file proxy, expired documents, My files pictures)

What the My files page reads for a stored chat picture (CONTRACT §9), so the
backend files track can match it. Written before the backend rows landed;
the parser accepts the variants marked "or".

- One row per `chat_media` row, in the same `items` list as every other kind:
  `{"id": "media:<media_id>", "source": "media", "kind": "image",
    "name": "<shown name>", "bytes": <int>, "created_at": "<iso>",
    "conversation": {"id": "<conv>", "title": "<title>"},
    "availability": "available" | "expired",
    "attachment_id": "<the chat_media.attachment_id>",
    "media": {"width": <int|null>, "height": <int|null>, "mime": "<mime>"} | null,
    "can": {"download": true|false, "preview": "image" | null, "delete": false}}`.
- `source` may also be `image` or `chat_media`; the id prefix must equal it,
  as for every other source.
- The page BUILDS every picture URL itself, from `conversation.id` and the
  attachment id: `/api/chat-media/<conv>/<attachment_id>?size=thumb` for the
  row's thumbnail, `?size=full` for Preview and Download. It never puts a
  server-sent URL in an `<img src>`. So the row MUST carry the attachment id:
  `attachment_id` (or `media.attachment_id`), or a `thumb_url` /
  `thumbnail_url` of the form `.../chat-media/<conv>/<attachment_id>?...`
  from which it is read. A picture row with none of these is dropped (the
  page cannot show it), like any other row it cannot act on.
- `name` may be null or empty for a picture (chat_media has no file name):
  the page then shows "Picture" plus the extension of `media.mime`.
- `kind=image` must be accepted by the `kind` filter (comma list) and the
  summary's `kinds` must carry `image: {count, bytes}`. The type chip
  "Pictures" is shown only when the summary reports an `image` count, so an
  orchestrator without picture rows never gets a `?kind=image` it would 400.
- `retention.pictures`: `"browser_only"` keeps today's sentence ("Pictures
  stay only in the browser you sent them from"). Any other value (suggested:
  `"kept_with_chat"`) makes the page say pictures stay while their chat
  exists, and that an older picture appears once the browser that sent it
  opens its chat again (the backfill).

fe-files, as built (2026-10-02 evening):

- Retention: the page also reads `retention.files_kept_with_chat` (boolean).
  `true` once documents and datasets get their lasting copy (CONTRACT §9):
  the first sentence becomes "Files you attach to a chat stay while the chat
  exists. One that could not be kept was removed after up to 24 hours; ..."
  and a swept row's note speaks about that file ("it was kept for up to 24
  hours") instead of stating a rule every file obeys. Absent or false keeps
  today's sentences. The backend files track should send it when the lasting
  copy ships; nothing else on the page depends on it.
- Players: a chip whose name is video or audio (`mediaKindFor`), or any chip
  on the `video` rail, opens `<video>`/`<audio controls preload="metadata"
  controlsList="nodownload">` with `src` = `/api/uploads/{conv}/{id}/file`.
  Opening fetches nothing. No upload id = no player (the dialog keeps its
  `unavailable` sentence). On a media `error` the dialog asks the same URL
  once for `Range: bytes=0-0`: 410 -> expired, 404 -> "no longer on the
  server", other non-2xx or offline -> "couldn't be loaded", 2xx -> "This
  browser can't play <EXT> files." (e.g. an .mkv or .avi codec).
- The uploads file proxy forwards `Range` and `If-Range` (plus the cookie,
  plus `accept-encoding: identity`; fetch() adds a second `identity` to a
  ranged request, so the orchestrator sees "identity, identity") and relays
  200/206 with content-type, content-disposition, content-length,
  content-range and accept-ranges, and 416 with its `bytes */<size>`.
  Checked in bundled Chromium and Google Chrome against a fake orchestrator
  serving Starlette 1.6 FileResponse: the WebM player's first request is
  `bytes=0-` -> 206; Chrome's seeks asked `bytes=393216-` and `bytes=65536-`,
  the WAV's seek `bytes=229376-`, all 206 through the proxy; a past-the-end
  range -> 416 `bytes */412440`. No CSP violation: the page policy needed
  no `media-src` (the fallback to `default-src 'self'` admits the route);
  `edge-csp.test.ts` now pins that blob: stays out.
- Opening never downloads a file the dialog cannot draw from bytes
  (`previewKindFor(name) === 'none'`: .zip, .pptx, .html, .parquet, .mkv
  with no id ...). On a device without the bytes such a chip now says
  "Preview is not available for this file type", the same everywhere,
  instead of "no longer available in this browser session".
- Kept previews: a PDF or text document (document rail) whose bytes answer
  410, or that has no upload id at all (a small inline document whose
  background upload never landed), shows `GET /uploads/{conv}/document?name=`
  under "The file itself has expired and is no longer stored." (410 only) or
  "The file itself can't be opened here." A CSV/TSV/JSON dataset whose bytes
  answer 410 shows its stored profile's table. A workbook listed `expired`
  shows its summary under the expired line, and with no usable summary says
  "This upload has expired..." (it used to say "this browser session").
  `ServerPreviewLoaders` now return `{value, expired?}` (`Kept<T>` in
  AttachmentPreview.tsx); MessageRow and MyFileRow are the only callers.
- My files: picture rows as written above; a 404/410 on the full picture
  turns the row into Removed. Video and audio UPLOAD rows on My files still
  have Download only (no player): not in this track's brief.
- Not changed, for whoever owns ChatApp: dragging a sent video back into the
  composer (`reuseAttachment`) still downloads the whole file to re-attach
  it, as before.

## coordinator (2026-10-02 21:35 IST)
- 9c27466d merges origin/dev 307155b4 into this branch: Next.js 16.3.3 -> 16.3.6 and anyio 4.14.2 in
  compose/voice-store (two CRITICAL Trivy advisories). Not part of this feature; ignore those three
  files when reviewing. frontend/node_modules was reinstalled (next 16.3.6).
- PR #92 (release w1, V43) is merged to main; V44 here stays next in line.

## qa-frontend (attack on be-media / fe-images / fe-files frontend, 2026-10-02 21:50 IST)

- A text-only follow-up about a picture is answered by `run_vision_engine`
  (main.py `elif image_followup_images`, and the `image_followup.unavailable`
  branch), which emits `meta.route = "vision"`. `showsLegacyPhotoNote` reads
  any imageless user turn followed by a vision answer as a pre-storage photo,
  so the "Photo not stored on the server" line shows under every such
  follow-up, on every device, in new chats too. Whoever fixes it: the
  orchestrator could tag follow-up answers (e.g. `meta.image_followup: true`)
  and the predicate skip them; legacy rows have no tag, so the predicate also
  needs a thread-level rule (no earlier user turn with photos or a note).
- `runEdit` persists the new version BEFORE `startStream`. A 422
  `image_ref_missing` then withdraws the stream but the unanswered edit stays
  stored (and pushed). The regenerate and retry paths persist nothing first.
- The backfill only looks at turns WITHOUT `meta.images`. A turn whose
  browser-written `meta.images` never got stored (the /chat never reached
  intake, a feature-gated account, a skipped store) shows "Image unavailable"
  everywhere else for good, even while the sending browser still holds the
  bytes.
- Proxies held against encoded traversal (%2e%2e, %2F, %252F, NUL, RTL
  override, >64 chars, bad `size`): 400 before any fetch, all three routes.
- `npm run build` rewrites `frontend/next-env.d.ts` (`.next/dev/types` ->
  `.next/types`); put the committed copy back after a build.

## security-attack (image store, proxies, 2026-10-02 22:00 IST)

- `chat_media.inspect` admits any raster up to 40 MP (89 MP for JPEG) and
  decodes it in the DEFAULT asyncio executor with no concurrency bound. A
  38-byte lossless WebP of 16383x2440 costs ~600 MiB peak per inspect, a
  1 MiB progressive JPEG of 9450x9450 ~505 MiB (draft does not cap a
  progressive scan's coefficient buffer), a 166 KiB PNG of 6320x6320
  ~460 MiB. 16 at once in one process: +9.5 GiB. The composer never sends
  more than 1600 px on the long edge, so the store needs neither ceiling;
  a lower store ceiling plus a small process-wide semaphore around
  `inspect` (and around image_memory's `_fit` of stored originals) closes it.
- The byte routes' `private, max-age=31536000, immutable` keeps every photo
  in the browser's HTTP cache for a year AFTER logout. Logout wipes
  IndexedDB "for the next person at this keyboard" but sends no
  `Clear-Site-Data: "cache"`. The admin route has the same header, so a
  second view by the admin is served from cache and never audited.
- The backfill reads IndexedDB image records by MESSAGE INDEX. Those
  records are write-once and dropped only past the thread's end, so after a
  thread was rewritten elsewhere (edit, then regrown) a stale photo is
  uploaded and written into an unrelated text turn's `meta.images`.
- Sharing: `sharing.evaluate` skips empty-content turns, so a photo-only
  turn's `meta.images` never blocks a public link; `vision` is not in
  `PRIVATE_ROUTES` either. Confirmed with a pure `evaluate` call.
- Held: authz on every attachment_id -> bytes path (viewer-scoped rows plus
  conversation owner), F034, path building from validated ids only, reaper
  symlink handling (`is_dir(follow_symlinks=False)` + rmtree's fd walk),
  the free-space floor on both write paths, CSRF (orchestrator Origin
  check on :8080, `Sec-Fetch-Site` on the Next POST), nosniff and the
  sandbox CSP on 200, and logs without names. gitleaks `dir` over every
  in-scope file: no leaks. Gitleaks `git` in this worktree scans nothing
  (the .git file points outside the mount).

## qa-be (backend QA attack, 2026-10-02 22:00 IST)

Confirmed by attack tests (uncommitted: orchestrator/tests/test_attack_chat_media_be.py,
frontend/tests/attack-be-store-failure-never-repaired.test.ts); none is a cross-user leak.

- The image_memory store fallback is BRANCH-BLIND: `latest_turn_images` takes the newest user
  message with `meta.images` in the flat, append-only message list (lib/branching.ts), so a photo
  turn on an edited-away branch is read into a later text turn ("what does the photo show?" ->
  vision with that photo; IMAGE_MEMORY_STORE_FALLBACK=0 -> chat). `turns_after` counts other
  branches' turns too.
- A failed background store (/chat `image_ids`) is never repaired: the message already has
  `meta.images`, and the backfill skips every turn that has it (lib/chatMedia.ts:571), so the
  browser holding the bytes never re-sends them. POST /chat-media under the SAME attachment id
  would heal it (idempotent).
- The sharing gap is live over HTTP: a chat whose only user turn is a photo (empty content) gets a
  public link (200) carrying the vision answer.
- A row whose file is gone stays 410 forever: a re-POST of the identical bytes answers
  `created:false` and writes nothing; If-None-Match still answers 304.
- Thumbnails: a 16-bit greyscale PNG thumbnails as pure white. Refused as unsupported although
  every browser shows them: MPO JPEGs (camera/phone multi-picture) and a JPEG with an appended
  trailer containing FF DA (motion photos). The composer re-encodes anything over 1600 px, so these
  reach the store only at 1600 px or less.
- A ref-only /chat turn with no `message` hands the engine an empty question (the placeholder is
  computed before refs load); the Next proxy fills IMAGE_ONLY_PROMPT, so only direct callers see it.
- Held: V44 over a V43 database with data (schema_parity compare IDENTICAL, idempotent re-run,
  user cascade, delete_conversation clears rows), F034 and IDOR on every route and on image_refs,
  an unowned chat later claimed by another account, 8 concurrent stores of one attachment id (one
  row, one directory), crash between files and row (no files left), CMYK / animated GIF / animated
  WebP / palette+transparency / LA / EXIF 6 / 1x1 / 20000^2 bomb, RTL/unicode/traversal ids, 10k-row
  list (1.8 MB, 0.11 s), five ~10 MiB pictures in one POST (49 MiB, 200 in 0.6 s).

## be-files (orchestrator: lasting originals, download fallback, reaper, My files)

As built (commit fa6d3277):

- `uploads.lasting_path(conv, upload_id)` = `<CHAT_FILES_DIR>/<conv>/<upload_id>/original`
  (None unless conv matches `^[A-Za-z0-9_-]{1,64}$` and the id is 32 hex).
  `uploads.keep_lasting_copy` makes it for purposes `document` and `dataset`
  only (hard link, fsynced copy on EXDEV), from both finalisers, so single-shot
  and chunked alike. Never raises; never refuses the upload.
- `uploads.lasting_has_room()` probes CHAT_FILES_DIR (not CHAT_MEDIA_DIR)
  against `settings.chat_media_min_free_gib`. A test of the floor sets the
  setting high (`1e9`); conftest's 0.0 default means every other test gets
  copies.
- `uploads.kept_original(conv, upload_id, notes)` is the shared fallback:
  lasting copy, then the video store for `notes == 'video'`. The member route
  ends in 410, the admin route keeps its 404 "The file has expired.".
- Range: Starlette 1.6 `FileResponse` already answers 206/416/If-Range on
  both routes; nothing was written for it, `tests/test_chat_files.py` pins it.
- `uploads.erase_conversation_files` now ALSO removes
  `<WORKSPACE_DIR>/uploads/<conv>` (the hard link's other name). Still one
  `chat_media_erase_total{store="files"}` count per call.
- Lasting-copy reaper: `uploads.reap_lasting_files()` /
  `maybe_reap_lasting_files()`. It is called at the end of
  `uploads.sweep_expired_upload_sessions()`, which main.py's
  `_upload_session_sweep_loop` already runs every 600 s, and throttles itself
  to `CHAT_MEDIA_REAP_INTERVAL_S` with a module global `_LASTING_REAP_DUE`
  (tests reset it with monkeypatch). Reaped directories are counted as
  `chat_media_reaped_total{kind="dir"}` (the closed label set has no
  `files` value).
- My files: `KINDS` gains `image`, `SOURCES` gains `media`. Picture row =
  the fe-files shape exactly: `id "media:<media_id>"`, `source "media"`,
  `kind "image"`, `name "Picture.<ext>"` (from the mime, so name sort, name
  search and cursors stay non-null), `media {width, height, mime}`,
  `attachment_id`, `can {download, preview "image"|null, delete false}`,
  `availability` from the full file on disk. Scoped by `m.user_id = viewer`
  AND `conversations.user_id = m.user_id`, F034 excluded. Retention:
  `files_kept_with_chat: true`, `pictures: "kept_with_chat"`. The summary's
  `kinds` always carries `image`.

be-files needs (not done, files I may not edit):

- main.py could start the lasting-copy reaper beside `chat_media.reap_loop()`
  instead of it riding the upload-session sweep; behaviour would not change.
- `tests/test_document_uploads.py::test_a_swept_document_is_still_an_honest_410`
  pinned "swept means 410" and failed once documents kept a lasting copy. I
  changed that one test (one line: it also removes the lasting copy) and
  committed it with this track, since the change is this track's behaviour.
- `GET /uploads/{conv}` (`list_uploads`, `bytes_available`) still says
  `expired` from the workspace copy alone. Left as is: the dataset engine
  reads `extracted/`, which the lasting copy does not restore.
- Test runs on pg-test-hand currently need
  `TEST_DATABASE_ALLOW_SHARED_SERVER=chatmedia_qa_be_fresh,chatmedia_qa_be_v43`:
  another track created those two databases without the `_test` suffix, and
  conftest refuses a server holding non-test databases.

## qa-files (attack on be-files: lasting originals, download fallback, reaper, My files pictures, 2026-10-02 22:10 IST)

Confirmed by attack tests (uncommitted: orchestrator/tests/test_attack_chat_files_qa.py,
frontend/tests/attack-myfiles-real-rows.test.ts). None is a cross-user leak or data loss.

- The lasting-copy reaper runs INSIDE upload requests. `uploads._sweep_quietly()` awaits
  `sweep_expired_upload_sessions` in POST /uploads and POST /uploads/chunked/init, and that now
  ends in `maybe_reap_lasting_files()`. Each live chat directory with an upload older than the
  grace costs one DB query per pass. With 3000 such chats on the test DB, the one upload that hits
  a due pass took 1233 ms against 20 ms otherwise. The first upload after a restart always pays it
  (`_LASTING_REAP_DUE = 0`). Moving the call into main.py's `_upload_session_sweep_loop` (or a
  loop beside `chat_media.reap_loop()`) takes it off the request path.
- Two places still call a kept file gone. `GET /uploads/{conv}` says `expired` after the sweep
  while the file route serves the lasting copy, and a workbook (.xlsx) preview in the chat prints
  "The file itself has expired and is no longer stored" over it. main.py `_resolve_document_refs`
  reads only the workspace `_original`, so any regenerate, edit or retry of a document turn older
  than WORKSPACE_TTL_HOURS fails with "<name> is no longer available on the server". The frontend
  always resends a document that has an id by reference (`pdf_uploads`). Both need the lasting copy
  as a fallback (`uploads.lasting_file`).
- A file whose name the path resolver refuses (leading dot such as `.env`, or a backslash) gets a
  lasting copy and lists on My files as `available` with `can.download: true`, but the member route
  answers 404 "upload not found". `resolve_upload_file` raises before the lasting fallback is
  reached. This was already true before this branch; the lasting copy (named `original`) could
  serve it.
- Starlette answers a Range with an unknown unit (`items=0-1`), a reversed range or a malformed
  one with 400. RFC 9110 says an unknown unit MUST be ignored (200). This is Starlette's behaviour
  and was already true; no browser player sends these.
- Already true and unchanged: a 300-byte file name (ENAMETOOLONG), `..` and `dir/` crash POST
  /uploads with a 500 (`_stream_to_disk`), before any lasting-copy code runs.
- Held: suffix ranges, multi-range (multipart/byteranges), past the end (416), and If-Range across
  the sweep (same inode, same validator, 206). Also held: chunked complete replayed after the
  sweep (one copy, counted once), the EXDEV copy fallback (separate inode, no `.tmp` left), the
  floor (zip dataset: counted no_space, then 410), and a chat id deleted by one user and recreated
  by another (only the old copy is reaped; the new owner gets 404 for the old upload id). The
  newest, oldest, largest and name sorts page correctly with pictures and uploads named
  "Picture.png" (10 items, limit 2, no duplicates). `kind=image` works alone, and the summary
  includes `image`. A picture stored under an unowned id that someone else later claims is listed
  for nobody. The backend's real page and summary JSON parse in frontend/lib/myfiles.ts: the
  picture's download URL is `/api/chat-media/<conv>/<att>?size=full`, and `filesKeptWithChat` and
  `picturesKept` are true.
- Shared-worktree note: another agent's uncommitted app/chat_media.py edit briefly made a 32x24
  PNG answer 415 during this run. A retry passed. Treat a sudden 415 in a picture test as that.

## security-files (attack on be-files lasting copies, 2026-10-02 22:30 IST)

Proof file (uncommitted): orchestrator/tests/test_attack_lasting_files_sec.py. No cross-user leak found.

- Disk: a lasting copy is a hard link, so the workspace quota (WORKSPACE_QUOTA_GB 20) and the 24 h
  TTL now free nothing for documents and datasets: eviction drops the workspace link (nlink 2 -> 1)
  and every byte stays. Nothing per member bounds the lasting store; the only guard is the global
  CHAT_MEDIA_MIN_FREE_GIB floor on a root filesystem (3.7 TiB, 2.4 TiB free here) that also holds
  the OS, Postgres and the models. One member can keep ~2.1 TiB, and at the floor every member's
  new photos and lasting copies are skipped (/chat silently, uploads revert to 24 h expiry). This
  is the owner's "no per-user quota" default; flag it before release, or cap lasting bytes per user.
- `lasting_path` and `erase_conversation_files` use `_CONVERSATION_ID_RE.match` (`$` admits a
  trailing "\n"); `_own` does too, so a direct API call can own `conv\n`. Its workspace copy sits
  at the SANITISED `uploads/conv/...` while its lasting copy is `chat-files/conv\n/...`; erase then
  misses the workspace copy, and the reaper (`fullmatch`) never collects the lasting dir. Self-only
  impact (frontend ids are UUIDs and the Next proxies refuse "\n"). Fix: `fullmatch` in both.
- Held: authz on every byte path (row + conversation owner; a re-claimed id after delete reaches
  nothing), the admin route (owner re-derived, audited before the response, no-store at the Next
  proxy), html/svg/quoted names served `attachment` with a guessed type from the lasting copy on
  200 and 206 (member and admin), Starlette 1.6 Range (max 100 ranges, merged, streamed 64 KiB),
  reaper containment (symlinked chat/upload dirs and an `original` symlink untouched; 3.12 rmtree
  is fd-based), My files picture rows (double-scoped, F034 excluded, bound params).
