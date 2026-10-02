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

## fix-be (backend fixes from the QA and security attacks, 2026-10-02 22:40 IST)

Commits e132b958, fa0a749b, 6c7eae31. What changed that other tracks may rely on:

- Store ceiling: `chat_media.MAX_STORE_PIXELS = 16_000_000` for EVERY format,
  JPEG included, judged from the header before `verify`/`load`. Above it the
  picture is refused as `unsupported` (415 on POST, skipped + counted on
  /chat). The old 40 MP / 89 MP ceilings are gone from chat_media
  (image_memory keeps its own for inline pictures).
- Decode pool: `chat_media.run_decode(fn, *args)` runs on a 2-worker
  ThreadPoolExecutor (`DECODE_WORKERS`, threads `chat-media-decode_N`). The
  POST route's `inspect`, the whole /chat background store per picture
  (`_store_inline_one`), a heal, and image_memory's store-fallback read
  (`_stored_read`, which may `_fit` a 10 MiB original) all go through it. A
  burst queues; it never fills the default executor.
- JPEG end check is a marker-segment walk to the PRIMARY picture's EOI
  (`_jpeg_reaches_eoi`); bytes after it are ignored. MPO (Pillow's name for
  a multi-picture JPEG) is stored as `image/jpeg`, `full.jpg`. A cut file is
  still refused, with or without an EXIF thumbnail or a trailer.
- Heal: when a row exists but its full file is gone, a retry of the SAME
  bytes (sha256 equal) under the same attachment id rewrites `full.<ext>`
  (and `thumb.webp` when the row has one) into the row's own directory. POST
  answers `created: false` with the unchanged row; the metric counts
  `result="stored"`. Different bytes never replace it (the row stays 410).
  The frontend's new repair (32e3d9d5) only re-sends ids the LIST lacks, so
  a row-without-file is healed only by a send/backfill/upload retry of the
  same bytes; nothing re-sends those on its own today.
  If-None-Match still answers 304 for a row whose file is gone: the tag is
  the sha256, so the browser's cached copy is exactly right.
- 16-bit greyscale PNG thumbnails are scaled to 8 bits (were pure white).
- /chat: a ref-only turn with no words gets "Analyze the attached image."
  like an inline one.
- image_memory store fallback follows the branch: `hydrate(conv, viewer,
  visible=[(role, content), ...])` from /chat `messages`;
  `chat_media.latest_turn_images(uid, conv, visible)`. A picture turn counts
  only when it is on that path: by its words (exact, or the end of a turn
  after a blank line, which covers the browser's paste/quote folding), or,
  for a photo with no words, by an assistant message stored under it
  (`meta.branch.parent` = its `self`, or the physically next message) whose
  text is on the path. `turns_after` = user turns on the path after it,
  not counting the trailing question. `visible=None` (no `messages` sent)
  keeps the old stored-order behaviour; `[]` finds nothing. Only the 20
  newest picture turns are compared.
- sharing.evaluate reads provenance (PRIVATE_META_KEYS, PRIVATE_ROUTES,
  has_attachment) from EVERY user/assistant message, empty ones included.
  `shareable_messages`, the empty-chat refusal and the secret scan still use
  the non-empty ones. A photo-only chat now answers 422 "uploaded photos".

Not done here (files I may not edit), exact change:

- `orchestrator/app/authn/admin_api.py` `member_chat_media`: before
  `return response`, add `response.headers["Cache-Control"] = "private, no-store"`
  (the recordings rail's rule, audio_api.py). Today an admin's second view is
  served from the browser cache for a year and never audited. Test: two GETs
  of the admin route both answer 200 with `cache-control: private, no-store`
  and write two `admin_viewed_chat_media` audit rows.

Seen, not in my list, not fixed: `decode_inline` accepts only
`data:<type>;base64,` prefixes, so a data URL with parameters
(`data:image/png;name=x.png;base64,...`) is counted `unsupported` and not
stored (tests/test_attack_chat_media_be.py::test_data_url_with_parameters_is_stored
fails on e132b958 too). The composer never sends one (lib/images.ts checks
`data:${mime};base64,`). A `^data:[^,]*;base64,` prefix would accept it.

The uncommitted attack proofs that assert the OLD behaviour now fail as
intended: test_attack_chat_media_be.py::test_row_without_file_is_never_healed_by_a_retry,
::test_fallback_hands_the_model_a_picture_from_an_edited_away_branch (route
chat, 0 vision calls), and all three in test_attack_chat_media_sec.py.

## fix-fe (frontend fixes from the QA and security attacks, 2026-10-02 22:50 IST)

Commit 32e3d9d5. What changed that other tracks may rely on:

- Legacy line: `lib/chatMedia.legacyPhotoNoteId(thread)` picks AT MOST ONE turn
  per thread as read: the first photo-less user turn under a `route: vision`
  answer, and only when no earlier turn has a photo (local or `meta.images`)
  or a vision answer. ChatApp passes `legacyPhoto={m.id === legacyPhotoTurn}`.
  A second legacy photo turn further down gets no line (accepted trade-off).
  An orchestrator tag on follow-up answers (`meta.image_followup: true` in
  main.py's `elif image_followup_images` / `image_followup.unavailable`
  branches) would make this exact. Nothing on the frontend needs it today.
- Edit by reference: before it writes the new version, `runEdit` calls
  `GET /api/chat-media/{conv}` once (`storedAttachmentIds`). If any ref is
  missing from the list, it shows the re-attach toast and writes nothing. An
  unreadable list (offline, 5xx, a body without `items`) lets the server
  decide, as before. Residual: a row that is LISTED but whose file is gone
  still passes the check, gets the 422, and leaves the stored version. If
  the list skipped rows with no full file on disk, that would close it.
- Repair (backfill): on chat open, for a user turn that HAS `meta.images` and
  whose bytes this browser holds, the browser asks the list once per chat and
  POSTs the missing ids with `source=backfill`, under the SAME ids. It writes
  nothing to the thread. It skips: turns younger than 2 min
  (`REPAIR_GRACE_MS`, by `createdAt`), bytes not proven to be that turn's
  (see the next point), counts or recorded mimes that do not pair one to one,
  and an unreadable list. 400/413/415 refuse that turn for the page session;
  401/403/507 halt; 404/5xx mean try at the next open. It does NOT re-send
  ids the list already has, so fix-be's heal of a row without its file is
  not triggered by it.
- IndexedDB image records now carry `fp` = `idbCache.turnFingerprint(m)`
  (`role:len:fnv1a36(content)`), written by `put`. `loadAll` lays a record
  only on the turn it names. `loadImages` returns `Map<idx, {urls, boundTo?}>`.
  `history.localImages` returns a bare `string[]` for bytes carried by the
  message itself, and the record (unvouched) where a record without `fp`
  sits at that index. The backfill takes a named record only for its own
  turn. An unnamed (pre-fix) record is used only under a vision answer and
  never for a repair. No DB_VERSION bump.
  Residual: records are still write-once, so a new photo sent at an index
  that holds a stale record is not written to IndexedDB. It shows from the
  server copy after a reload.
- Stored thumbnails retry 3 times, after 2 s, 6 s and 15 s (`&retry=1..3`,
  dropped by the proxy), before showing "Image unavailable". In a real
  Chromium against a fake orchestrator, a photo whose store landed 5 s late
  showed on the second retry at 8.6 s.
- Logout: `app/api/auth/logout/route.ts` sets `Clear-Site-Data: "cache"` on
  every answer (mock, 502 and 499 included). `handleSessionEnd` POSTs
  `/api/auth/logout` for an access-ended account (removed or deactivated)
  before it wipes local data. Checked in Chromium 153 against the built app:
  without logout, a fresh page fetched a seen photo URL from cache with 0
  orchestrator requests. After the logout answer, it made 1 request, so the
  session check runs. Chromium consumes the header, so `fetch()` cannot see
  it. Curl does.
- NOT done here (orchestrator): the admin route's
  `Cache-Control: private, no-store` (exact change in fix-be above).

## coordinator (2026-10-02 22:00 IST)
- PR #92 is deployed: production is at V43 on main 4df0e3d6. V44 here is the next migration.
- Known CI flakes seen on #92's main run, not ours: the arm64 gate's `auth.docker.io` connection reset
  (Container images) and a timing flake in `test_health_dependency_cache`. Rerun alone before chasing.

## fix-files (backend fixes from the attacks on the lasting copies, 2026-10-02 23:10 IST)

Commit 509a473c. What changed that other tracks may rely on:

- `uploads.sweep_expired_upload_sessions()` no longer runs the lasting-copy
  reaper; it still runs inside POST /uploads and chunked init
  (`_sweep_quietly`). main.py's `_upload_session_sweep_loop` calls
  `uploads.maybe_reap_lasting_files` after each sweep (first pass ten minutes
  after start-up, then at most once per CHAT_MEDIA_REAP_INTERVAL_S). No
  request path reaps.
- `_resolve_document_refs` (main.py): when the workspace `_original` is gone,
  a `pdf_uploads` ref resolves to the lasting copy if THIS conversation's
  uploads row names it (`_kept_document`). The row's filename is the name and
  decides the type. An archive re-extracts into the workspace
  `<upload>/extracted/`, which the TTL sweeps again later. With no lasting
  copy the old "no longer available" sentence stands. A dataset id sent as a
  document ref now also resolves (direct API only; the browser never sends
  one).
- `GET /uploads/{conv}`: `expired` only when neither the workspace copy nor
  the lasting copy is on disk. A dataset listed `ready` after the sweep has
  no `extracted/`; the dataset engine's behaviour for it is unchanged.
- The member file route serves a kept file whose name the resolver refuses
  (leading dot, backslash) from the lasting copy. With no copy it is still
  404. Content-Disposition uses `os.path.basename(filename)`.
- Stored upload names: `uploads._upload_filename`. Empty, "." and ".."
  become `upload.bin`. Names over 240 bytes keep their extension (when it is
  32 bytes or less) and lose the end of the stem. That is 255 less room for
  the chunked rail's `<name>.assembling`, so a 245-255 byte name now gets
  shortened. Before, single-shot kept it and chunked failed with a 500.
- Conversation ids use `fullmatch` at every claim site: uploads `_own`,
  `lasting_path`, `erase_conversation_files`, history POST
  /history/conversations (400), and /chat's claim (422).
- Admin `member_chat_media` answers `Cache-Control: private, no-store` on
  200, 304 and 410. The Next admin proxy (`proxyToOrchestrator`) relays it.
  `tests/test_chat_media_lifecycle.py` pins it, and a second view writes a
  second audit row.

Not fixed, with reasons:

- Range with an unknown unit, malformed or reversed (`items=0-1`,
  `bytes=abc`, `bytes=10-5`) still answers 400. This is Starlette 1.6
  FileResponse behaviour and was the same before this branch. RFC 9110
  allows rejecting a malformed range. Only the unknown-unit case should be
  200, and no player sends it. The three
  `test_attack_ranges_on_a_lasting_file` proofs still fail.
- Per-member cap on the lasting store: not added. CONTRACT §2 records the
  owner default "no per-user quota beyond the disk free-space floor". The
  existing `DiskFillingUp` alert (root over 85% full) fires before the
  250 GiB floor (about 93% of 3.7 TiB). Raise this with the owner before
  release.
- The uncommitted `test_attack_chat_files_qa.py::test_attack_a_resent_document_ref_...`
  now gets past the "gone" error. It still fails, but only because its fake
  request has no `pdf_data` attribute.
  `tests/test_chat_files.py::test_a_resent_document_reads_its_lasting_copy`
  is the real proof. `test_attack_lasting_files_sec.py::test_trailing_newline_conversation_id`
  now skips ("refused upstream").

## coordinator (2026-10-02 22:35 IST) — for the Verify phase
- The UNTRACKED files `frontend/tests/attack-*.test.ts(x)` and `orchestrator/tests/test_attack_*.py` are
  QA proofs, not part of the branch. Some fail on purpose (deferred items: malformed Range -> 400 is
  Starlette's behaviour; no per-member quota on the lasting store is an owner decision). Do not chase
  their failures and do not commit them; the coordinator removes them before the push. Judge the gates
  on tracked files only (e.g. `git stash` is forbidden, so exclude them by path when you run suites).

## e2e (real-browser two-device run, 2026-10-02 22:55 IST)

Stack (b) on the head, briefly, all on 127.0.0.1 and removed afterwards: the orchestrator from a
`git archive` of 1348a56d (venv uvicorn :18961, V44 migrated at boot, private DB
chatmedia_e2e_test on pg-test-hand, now dropped), the Next build of the same snapshot (:3961),
and a copy of e2e/ci/engine.js (:18960) that also logs the number of image parts per completion.
Worker (a) was skipped: no orchestrator image or python:3.11 base cached there, and Docker Hub
pulls were failing today. ASR_BASE_URL must be overridden in any such stack: its default is the
production whisper on the worker.

Held (Playwright Chromium, A = 390x844 phone, B = 1440x900 fresh storage, same account):
- A sends a photo: the bubble shows it at once (data URL, 1200x900). /chat carries `image_ids`,
  the row and full.jpg + thumb.webp land, meta.images is pushed.
- B opens the chat: the thumb (512x384 webp, immutable, nosniff, sandbox CSP) sits in a
  213x160 box reserved before load; nothing moved after load. Click opens size=full (1200x900).
- B "Try again": the body has `image_refs` and no inline image; the engine got 1 image part.
- Backfill: a legacy turn (no meta.images, an IndexedDB record without `fp`, vision answer) was
  posted as `bf-<32 hex>`, meta.images reached the server, B showed the photo, the legacy line went.
- Delete: every media URL answers 404 to B (request context and fetch no-store); the chat's
  chat-media directory is gone. B's page fetch with the default cache still gets 200 for photos
  it had seen (immutable cache; Clear-Site-Data on logout is the only purge, by design).

Failed (3 of 3 runs), for whoever owns history sync:
- RC-3c is not closed for this sequence: image + PDF in turn 2, the upload answered 200 before
  the answer (uploads row exists), A's two PUTs carrying `attachments[0].id` were both 409
  (the first with an `expected_updated_at` older than A's own POST /messages append), A
  reconciled to the server copy and never pushed again. The stored attachment keeps
  `upload_state: "selected"` and no `id`, so no other device can open the PDF.
- B, which had the chat open earlier (and regenerated in it), navigated back to it after A's
  second turn: it fetched the conversation list but not the conversation, and kept showing its
  cached thread without A's turn (second photo and PDF missing). Root cause not isolated; it
  may predate this branch.
- Not run: video/audio playback (step 7). The stub stack has VIDEO_ANALYSIS_ENABLED=false and no
  ffmpeg on the head, and audio/video travel on the video rail.

## integrator (every CI gate over the branch, 2026-10-02 23:15 IST)

Code under test: 1348a56d (everything after it is docs only: 6684411e, 702149af, 47b18433).
Untracked attack proofs excluded by path everywhere (shard plan from a `git archive` of HEAD;
vitest `--exclude 'tests/attack-*'`). No gate failed, so no code was changed.

- Orchestrator, CI's 3 shards in parallel (shard_tests.py plan, 489 files, 163 each,
  `--check --of 3` OK), one DB each on pg-test-hand:
  s1 5459 passed, 23 skipped, 3 xfailed, 0 failed (1262 s);
  s2 5776 passed, 9 skipped, 0 failed (1152 s);
  s3 5679 passed, 12 skipped, 4 xfailed, 0 failed (1455 s).
  Total 16914 passed, 44 skipped, 7 xfailed, 0 failed. Every skip is an opt-in live test or a
  host tool (ffmpeg, pandoc, fonts); none is a chat-media, files, My files or sharing test.
  None of the known flakes fired.
- Goldens and contracts (also inside the shards): test_context_assembly_golden 44,
  test_prompt_final_send 56, test_contract 54, test_publicapi_contract 87: 241 passed.
- ruff_gate (pipeline paths, compose/voice-store included): clean, 0 documented findings.
  schema_parity invariants: V1..V44 contiguous. Schema job: fresh 0 -> 44, staged V20 -> 44,
  compare IDENTICAL (1996 structural lines), re-run 44 -> 44.
- Policy job: bash -n 43 files 0 failed; workflow_policy OK (P1-P8); CI-scripts unittest gate
  710 executed / floor 698; rollback rehearsal 9 scenarios; monitoring 35 / floor 33;
  launcher 694 executed, 1 skipped / floor 450; compose/voice-store 40 passed / floor 33.
  sync-worker not run here (the venv has no PyJWT); sync-worker/, launcher/, monitoring/,
  .github/ and scripts/ are identical to main 4df0e3d6.
- Frontend: vitest 207 files, 4023 passed, 11 skipped, 0 failed; tsc 0 errors; npm run lint
  0 problems; `eslint .` 0 errors, 41 warnings (34 in untouched test files, 7 in the untracked
  attack-chat-media-fe.test.tsx, 0 in any file this branch changed); npm run build OK (warm in
  the worktree, then cold over a `git archive` of HEAD: compiled 6.0 s, TypeScript 7.1 s,
  53 pages). frontend/next-env.d.ts restored from HEAD.
- gitleaks (pinned digest, from the main checkout read-only): 21800739..feat/chat-media =
  45 commits, 0 findings. secret_gate over that report fails only on 27 STALE baseline
  entries, which a range scan cannot report by construction. Full history of feat/chat-media:
  850 commits, 27 findings, all baselined, 0 new, 0 stale, gate rc 0.
- Leftovers: no debug prints, console.log, TODO/FIXME, .only/.skip or new suppressions beyond
  the codebase's `# noqa: BLE001` pattern; no unused imports added (the 4 F401 hits in changed
  files predate the branch); no unreferenced new function.
- README (702149af + this commit): stale statements fixed (40/89 MP ceiling, share-policy gap,
  per-chat upload list, lasting reaper's home) and a "Where the build differs from
  CONTRACT.md" table added. Its last row records the e2e run's open RC-3c failure: whoever
  fixes RC-3c removes that row.

## integrator (merge of origin/dev 3fead415, 2026-10-02 23:30 IST)

dev brought the My files and voice archive fix rounds and the CPU whisper
replica (no migration; V44 stays the only new one, V1..V44 contiguous). The
four conflicts were resolved under the dev owner's rules:

- myfiles.py: dev's `ready_names` CTE and hashed NOT IN fold stand as they
  are; the picture branch (`_MEDIA_BRANCH`) joins `chat_media` to
  `conversations` only, no document x upload join. `_DECORATE` selects both
  `vs.outcome AS media_outcome` and the `cm.*` picture columns; `_item(row,
  user_id)` sets `has_transcript` on recordings and `{width, height, mime}` on
  pictures. The cursor gained no field (a `media` source with a 32-hex id
  passes the existing checks, `_utf8` included).
- Retention wording (frontend/lib/myfiles.ts): dev's two sentences win
  verbatim, with or without `files_kept_with_chat`: "kept for N hours, then
  removed the next time the server clears out old files" and the swept note
  "The file was removed after N hours". fe-files' "stay while the chat
  exists ... removed after up to N hours" and "(it was kept for up to N
  hours)" are gone. With `files_kept_with_chat: true` the lasting copy gets
  its own sentence: "Documents and spreadsheets also keep a copy that stays
  while their chat exists, unless the server was short of space when they
  were sent." The video sentence now shows whenever `video_kept_with_chat`
  is set. Pinned in my-files-lib.test.ts ("a lasting copy gets its own
  sentence ...").

## fix-pdf-id (RC-3c: the document id lost to two 409s, 2026-10-02)

What changed that other tracks may rely on:

- `threadReconcile.withStoredUploadIds(next, kept)` puts an upload id that
  only `kept` knows back onto the same file: same position, a user turn on
  both sides, the same words, then entry by entry on `attachment_id`. Only an
  entry with no id takes one (and `upload_state: 'uploaded'` with it); an id
  is never replaced or removed. `withStoredRefs` = `withStoredImages` + this,
  and it now runs everywhere `withStoredImages` did: the store's
  `saveMessages` and `amendMessages`, both 409 recoveries in `pushAll`, and
  the view's `reconcileThread`.
- `pushAll`, conversation changed: the carried id makes the repaired copy
  differ from the server's, so the recovery re-pushes once with the fresh
  stamp (it used to adopt the server copy and stop). A second refusal in a
  row still writes nothing more, but the repaired copy (ids, photo refs,
  branches) now stays in the cache, marked dirty, so the next push (the next
  save or the mount refresh) carries it. Before, the second refusal left the
  server's copy in the cache. The shrink 409 path does the same.
- Not done, on purpose: `expected_updated_at` is not refreshed from the
  client's own POST /messages. The append answers `created_at`, not
  `updated_at`, and quoting it would be wrong anyway: it would let the next
  PUT overwrite a server write made between this tab's last GET and its
  append (RC-4). So the first PUT after an append is still refused once, and
  the carry plus the re-push recover it. No backend change.
- Proof: `frontend/tests/upload-id-409-carry.test.ts` uses a server that
  keeps the real V29 rule (every write moves `updated_at`; a stale stamp is
  refused). On the old code it reproduces the e2e result: one refused PUT and
  no re-push. With the fix: 2 PUTs for one refusal, 2 + 1 (next refresh) for
  two in a row, 2 per save against a server that moves after every read, and
  0 for a settled thread re-saved. Not re-run in a real browser.
- The README deviation row for RC-3c is removed.

## fix-stale-second-device (2026-10-02 23:55 IST)

The e2e failure "B fetched the list but not the conversation and kept its
cached thread" PREDATES this branch:

- origin/dev 3fead415 (detached scratch worktree, since removed), vitest
  through the real ChatApp and store: after phone A's turn, B's reload plus
  one poll tick made 1 list GET, 0 conversation GETs, and showed B's cached 4
  messages without A's 2.
- Real Chromium against a fake orchestrator, HEAD 16dcae1d without the fix:
  same result (list 2 = active + archived, conversation 0, no photo, no PDF
  chip). Bringing the tab back into view did nothing either.

Cause: `loadConversation` served the cache whenever its ids matched what this
browser last pushed, and `mergeServerRows` folded the list's newer
`updated_at` into the cached `updatedAt` without remembering that the cached
thread was older.

Fix (frontend/lib/history.ts, frontend/components/ChatApp.tsx):
- SyncState `seen[conv]` is the server's `updated_at` (epoch ms) at the last
  GET of the messages. `stale[]` lists chats whose list `updated_at` is newer
  than `seen`. A chat never read here (started in this browser, or cached by
  an older build) compares with the cached `updatedAt` instead. The cached
  `updatedAt` alone is not enough: a thumb, a rename or a title stamps it with
  the browser's clock, which would hide the other device's turn.
- `loadConversation` skips the cache shortcut for a stale chat. It clears the
  flag before the GET, so a second load at the same time serves the cache. It
  sets the flag again if the GET fails, except on a 404. A non-forced read
  keeps the cached id, `imageDataUrl(s)` and `pdfName` on every turn that is
  unchanged (threadReconcile's per-turn rule), so the next save is still an
  append.
- New optional store methods: `refreshActive()` (one GET of the active list,
  no writes) and `isStale(id)`. `mergeServerRows` skips ids whose delete is
  still pending.
- ChatApp `checkForUpdates` runs on a sidebar open and on window focus or
  visibilitychange (visible). It waits for the mount's refresh, runs one at a
  time, and at most once per 5 s. It reads the list once, and re-reconciles
  the open chat only when `isStale`. No timer; the 8 s poll still never reads
  the list.
- Cost: one list GET per sidebar open or tab return. One conversation GET
  when the list is newer than `seen`, never when it is equal. After this
  browser's own writes that is also one GET, because it cannot know the
  server's new stamp.

Limits: a tab that stays focused and visible does not pick up the other
device's turn on the open chat until a reopen, a focus or a reload (no
polling, by design). Archived chats are re-listed only by the mount's
refresh.

Proof: frontend/tests/second-device-stale-open.test.tsx (7 tests: reload
fetches once and shows A's turn with its `meta.images` thumb and PDF chip;
equal `updated_at` makes no GET; a tab coming back into view; a sidebar open
fetches only the chat that moved; the open chat streams normally and keeps
its ids; `seen` beats a rename's local stamp; a failed read stays stale but a
404 does not). Real Chromium against the fake orchestrator, fixed build: the
same reopen made 1 conversation GET and showed the phone photo (thumb
512x384) and the PDF chip. An unchanged reopen made 0 GETs. Focus made 1 list
and 1 conversation GET. Code block, table, mermaid diagram and the artifact
panel all rendered on the refreshed thread, and B's own send streamed.

Seen, not changed (the same on both builds): the first two opens of a chat in
a fresh browser both GET it. With the fake orchestrator, an answer whose
stream carried a `generation_id` was not pushed by the browser. I did not
look into why; the real orchestrator has stored the answer itself since V29.

## e2e2 (re-run of the parts that failed, HEAD 4436a3d7, 2026-10-03 00:05 IST)

Same stack (b) on the head, all on 127.0.0.1, torn down afterwards: a `git archive` of 4436a3d7,
venv uvicorn :18971 (V44 migrated at boot, private DB chatmedia_e2e2_test, now dropped), the Next
build of the same snapshot (:3971), the image-counting stub engine (:18970), a throwaway member.
Use full Chromium (`channel="chromium"`): the default headless shell has no PDF viewer, so the
PDF dialog shows "Preview could not be displayed." even though the file arrived (200, 7125 bytes).

All four held, in 3 full runs (run 1 headless shell, runs 2-3 full Chromium):
- Baseline: A's photo shows on B (fresh storage) as the 512x384 webp thumb, immutable cache.
- Image + PDF in one turn: the stored user message carries `attachments[0].id` with
  `upload_state: "uploaded"`; B shows both photos and the PDF chip; the chip fetches
  /api/uploads/{conv}/{id}/file (200, application/pdf, 7125 bytes) and the PDF renders.
  A's first PUT after its POST /messages append is still 409 (expected, fix-pdf-id); the re-push
  with the fresh stamp is 200. Also held with A's /api/upload held 6 s so it lands after the
  answer, on a first turn and on a second turn (409, re-push without the id, then the late
  upload's PUT carries it, n=4, 200); B opened that PDF.
- Stale second device: B had the chat open, A sent image + PDF, B went to / and back: exactly 1
  conversation GET (200) and 2 list GETs; A's turn, its photo and the PDF chip showed. In-app
  (New chat, then the sidebar row, 6 s later) after A's third photo turn: 1 conversation GET,
  1 list GET, the third photo showed.
- Regenerate on B: /chat body has `image_refs` = [the photo's attachment id], no inline image,
  200; the engine got 1 image part.

Not tested: a sidebar click inside the 5 s `UPDATE_CHECK_MIN_MS` window after an earlier check
skips the list read (ChatApp checkForUpdates), so a turn sent in that window would show only
after the next focus or reopen. The orchestrator log's Files API PermissionError was the
harness (no PUBLIC_API_FILES root set), not this branch.

## coordinator (limits, 2026-10-03 02:10 IST)
- Another session's branch (whisper accuracy) edits frontend/components/Composer.tsx (the voice
  language control), VoiceBar.tsx, useVoiceRecorder.ts, lib/voice.ts, asr.py, dictation.py, config.py
  and audio_api.py. Keep Composer.tsx changes to the attachment caps and their checks only; do not
  reformat or move other code there. Avoid config.py unless a new setting is unavoidable.

## limits: backend (orchestrator, 2026-10-03)

The numbers the frontend track reads (LIMITS.md):

- **Inline budget per request: 48 MiB = 50,331,648 bytes of picture payload.**
  - On `/chat` it is the sum of the inline data URLs' lengths (`image` + `images`, base64
    characters, prefix included). A send over it goes by reference: the pictures go to
    `POST /api/chat-media/{conv}` first, then `/chat` carries `image_refs` and no inline bytes.
  - On `POST /chat-media/{conv}` it is the sum of one batch's raw `file` bytes. Batch so that each
    POST stays at or under 48 MiB. One picture is at most 10 MiB (`chat_media.MAX_IMAGE_BYTES`,
    unchanged, checked on the bytes sent), so a batch always holds at least four.
- **Server caps behind it (unchanged numbers):** `/chat` body 128 MiB; `POST /chat-media/{conv}`
  body 64 MiB (48 MiB of pictures plus the framing of 20 parts fits); Cloudflare 100 MB.
- **Counts on the server:**
  - `main.MAX_IMAGES = 20`, inline and `image_refs` together. Over it: pydantic 422 whose text
    holds `at most 20 images per message`.
  - `image_ids` and `image_refs` each hold 1..20 ids (`at most 20 image ids per message`). The Next
    proxy's id-list filter must allow 1..20.
  - `chat_media.MAX_FILES = 20` `file` parts per POST (400 `At most 20 pictures per request.`).
  - `pdf_uploads`: at most 20 per message (`A message can carry at most 20 documents.`).
  - `video_uploads` (video and audio): at most 20 per message (was 3;
    `A message can carry at most 20 videos.`).
  - Datasets ride no `/chat` field; the server has no per-message dataset count.

Backend as built (limits, 2026-10-03):

- `chat_media.BATCH_BUDGET_BYTES = 48 MiB` is the number above, in code; a test pins that
  `body_cap_for("POST", "/chat-media/c")` (64 MiB) holds it plus framing, that it is under the
  /chat cap and under 100 MB. `_FORM_MAX_FIELDS` is `MAX_FILES + 11` (20 ids, a source, the old
  slack); a 21st `file` part is Starlette's 400, re-wrapped as `bad_request`.
- `engines/document.MAX_DOCS` 12 -> 24 (20 references, the inline PDF, an archive's manifest and
  members). All documents still share ONE `DOC_CONTEXT_CHARS` (48,000) excerpt; page images come
  from the first PDF only. Video: the pinned block and the answer frames were already shared
  budgets (`engines/video.py`), so 20 videos are 20 lookups, not 20 budgets.
- **The router never saw turn pictures.** `engines/router.route_request` gets `has_image` only (an
  image forces `vision` with no model call), and every `router_chat_completion` caller sends text.
  The only images the router reads are video frames, one per call (`video/screen.py`). It is now
  also a rule in code: `router_chat_completion` keeps at most `context.CLASSIFICATION_MAX_IMAGES`
  (8, = `publicapi.registry.ROUTER_MAX_IMAGES`) image parts, the newest.
- image_memory with 20 photos keeps the turn's first pictures that fit, in order: the process
  budget (`IMAGE_MEMORY_MAX_CHARS`, 24 M characters) usually holds all twenty shrunk photos; the
  V41 row (`IMAGE_MEMORY_DB_CHARS`, 8 M) holds the first 8-30, by how large the shrunk photos are
  (0.25-1 M characters each). Never a failed turn. The
  store fallback now stops reading pictures once it has the process budget's worth
  (`latest_turn_images(..., max_chars=)`), so twenty 10 MiB originals are never all in memory.
  Honest limit: after a restart within the row's two hours, a follow-up hydrates the ROW, so it
  sees only the pictures the row kept; the store fallback runs only when there is no row.
- **Measured once on the production main model** (Qwen/Qwen3.6-35B-A3B-NVFP4, 127.0.0.1:8000,
  2026-10-03, one other request running): 20 synthetic 1600x1200 JPEGs (3.50 MB, 4.67 M base64
  characters, body 4.67 MB), `max_tokens` 16, thinking off, streamed: **38,063 prompt tokens
  (~1,900 per picture), time to first token 22.08 s**, total 22.14 s, answer "there are 20
  pictures attached". No restart needed (vLLM's per-prompt image limit is 999). A twenty-photo
  turn therefore waits about 20 s before its first word even at Fast (the SSE heartbeat keeps the
  connection), and a follow-up that re-sends the remembered pictures pays most of it again: the
  engine runs `--no-enable-prefix-caching` (`prefix_cache_queries_total` 0); only vLLM's
  multimodal processor cache can be hit.

## limits: frontend, NO limit (2026-10-03)

What the browser does now (replaces the 20-per-message version of this section):

- **No count limit.** `frontend/lib/orchestrator.ts` `MAX_IMAGES = MAX_DOCUMENTS = 999`, the
  technical ceiling only (the backend's `main.MAX_IMAGES`, `chat_media.MAX_FILES`). The proxy's
  id-list filter (`forwardableAttachmentIds`) passes 1..999 ids. The composer says nothing about
  a number below it; only a pick past 999 is told "One message can carry 999 photos — send the
  rest in the next message." (or "999 files"). The "You can attach up to N" toasts are gone.
- **No size limit on what streams.** The composer's 512 MB (documents, datasets, archives) and
  4 GB (video, audio) checks and their toasts are gone; the server's `UPLOAD_MAX_MB` is the only
  size rule and its refusal reaches the chip in its words. Datasets used to post whole to
  `/api/upload` (dead past Cloudflare's 100 MB): over `CHUNK_THRESHOLD_BYTES` (90 MiB) they now
  take the chunked rail with `purpose=dataset` (`uploadDocumentFile`, `DocumentRef.files` carries
  the profiled-table count); smaller ones post once, as before. `/api/upload`'s 513 MiB cap stays
  for tabs loaded before this change.
- **Photo size:** unchanged from the 20-step: measured on what is sent. A shrunk photo is accepted
  whatever the original weighed; one the browser cannot shrink keeps the server's 10 MiB
  stored-file rule ("<name> is 11.0 MB and this browser couldn’t make it smaller. A photo sent as
  it is can be at most 10 MB.").
- **Many photos, many big files, one tab:** at most 3 photos decode at once
  (`images.MAX_PARALLEL_DECODES`: a decoded 48 MP photo is ~190 MB of pixels) and at most 2
  chunked parts are read for their SHA-256 at once across all uploads
  (`uploadDocument.MAX_PARALLEL_HASHES`: 64 MiB each). The composer's chip row scrolls past three
  rows (`max-h-48`), so the box and Send stay on screen with 100 chips, phone included.
- **By reference over the budget:** as in the 20-step (inline at or under 48 MiB of base64,
  otherwise `POST /api/chat-media` first, then `image_refs`), but a batch is now cut by its BYTES
  only (`MAX_MEDIA_BYTES_PER_REQUEST` = 48 MiB; `MAX_MEDIA_PER_REQUEST` = the 999 ceiling).
- **Real browser (Chromium, built app + fake orchestrator, 2026-10-03 ~02:40 IST):** 100 photos
  (95 noisy 2400x1800 JPEGs + five 40 MB 8000x6000 originals, 411 MB picked) became 100 chips in
  about 11 s, no toast, chip row 192 px, box and Send in view at 1280x900 and at 390x844, no
  horizontal scroll. The send stored them by reference and posted one ~4 KB `/chat` with 100
  `image_refs`; the bubble showed 100 photos and the answer's code block, table and mermaid
  diagram rendered. 50 documents (a 600 MB PDF + 49 small) and a 5 GB video attached with no
  toast; the PDF went chunked (`init` purpose=document, 629,145,600 bytes, 10 parts, complete) and
  `/chat` carried 50 `pdf_uploads`. A 600 MB CSV went chunked with purpose=dataset and no
  single-shot `/api/upload`. The 100 photos (shrunk in the browser) went as two
  `POST /chat-media`, 61 files in a 47.9 MiB body and 39 in 31.6 MiB.
- **Not done here:** the 100-photo bubble is 160 px thumbnails, about 50 rows tall on a desktop; a
  compact grid for many photos is a design follow-up. The model-context fit of 100 photos is the
  backend's (smaller sizes); not exercised against the real model from the browser.

## limits: backend, NO limit (orchestrator, 2026-10-03 03:00 IST)

Replaces the "Counts on the server" list above (20 everywhere). For the frontend track:

- **Counts are a technical ceiling only, 999 (vLLM's per-prompt image maximum):**
  `main.MAX_IMAGES = 999` (inline + `image_refs` together; 422 `at most 999 images per message`
  only at 1,000), `image_ids` / `image_refs` 1..999 ids each, `chat_media.MAX_FILES = 999` `file`
  parts per POST (`_FORM_MAX_FIELDS = MAX_FILES + 11`), `pdf_uploads` 999, `video_uploads` 999.
  The Next proxy's id-list filter may allow 1..999. Nothing below 1,000 is refused for its count.
- **Bytes still bound a request (not user limits):** batch at `chat_media.BATCH_BUDGET_BYTES`
  (48 MiB) per `POST /chat-media` and inline per `/chat`; body caps unchanged (`/chat` 128 MiB,
  `/chat-media` 64 MiB: 48 MiB plus the framing of 999 parts and their ids fits). One picture AS
  SENT is still at most 10 MiB (`chat_media.MAX_IMAGE_BYTES`); the original may be any size.
- **Chunked rail:** `uploads._MAX_PARTS` 128 -> 16,384. At the browser's 64 MiB parts 128 parts
  silently capped every upload at 8 GiB; `init` reports `max_parts` = 16,384 now. The only size
  rule is `UPLOAD_MAX_MB` (production 102400).
- **Video/audio:** `VIDEO_MAX_UPLOAD_MB` unset follows `UPLOAD_MAX_MB` (production .env does not
  set it). A file longer than `VIDEO_MAX_DURATION_S` (4 h) is accepted, kept whole, and its first
  4 h analysed (`-t` on the audio and frame extraction); the probe stage no longer fails, and the
  overview, the pinned block and every answer's prompt say "only its first 4:00:00 of 6:12:00
  was analysed".
- **What the model reads of many pictures (engines/vision.py `fit_images`, code, not the model):**
  the turn's pictures go as they came while their image tokens fit the turn's budget, else ALL of
  them at 896, 640 or 448 px on the long edge (the largest that fits), and only when even 448 px
  cannot hold them, the first ones that fit. The budget is `VISION_IMAGE_TOKEN_BUDGET` (default
  65,536 image tokens, never more than the window minus the answer's reserve), not the whole 1M
  window: vLLM's processor turns every picture into float32 patches in the HEAD's memory (~24 KB a
  token, in the API server and again in the engine core) and the prefill runs ~1,300-1,700 image
  tokens a second, so 65,536 is ~1.6 GB of patches per copy and ~40-50 s: 34 photos at 1600 px,
  110 at 896, 215 at 640, 414 at 448 (4:3). The whole window would be minutes and tens of GB; the
  owner can raise the knob. The model is told the size ("sent all 100 pictures at 896 px") or how
  many it got; when pictures were left out the ANSWER ends with a sentence counted by code: "_I
  read the first 414 of the 999 pictures in this message; the other 585 did not fit in one
  question. Send them in another message to ask about them._" (a picture that needed shrinking and
  would not decode is named as "could not be opened as a picture"). The same fit runs on pictures
  attached beside documents (engines/document.py). The table pre-pass (superlatives) runs only up
  to 5 pictures.
- **Measured once on the production main model** (127.0.0.1:8000, Qwen3.6-35B-A3B-NVFP4,
  2026-10-03 ~03:20 IST, one other request running, `max_tokens` 16, thinking off, streamed):
  40 synthetic 1600x1200 JPEGs fitted to 896 px: body 0.55 MB, **23,623 prompt tokens** (the code's
  estimate 23,680), **TTFT 17.93 s**, total 18.24 s.
- **Stored pictures by reference:** `/chat` reads originals until `chat_media.REFS_FULL_CHARS`
  (96 Mi data-URL characters), then each further one as a 448 px copy, so 999 refs never put
  999 originals in memory.
- **Router:** besides the 8-picture share, at most `context.CLASSIFICATION_MAX_IMAGE_TOKENS`
  (32,768, half its 65,536 window) of image tokens, newest first.
- **Documents (head memory):** a turn reads at most `DOC_WHOLE_READ_BYTES` (256 MiB) of documents
  into memory whole (every ordinary turn unchanged); past it a document is a `DocFile` the engine
  reads from disk: PDF opened by path (PDFium reads on demand) up to `DOC_MAX_PAGES` 2,000 pages,
  DOCX streamed through a pull parser that stops at the text budget, text read only as far as the
  budget. All of a turn's documents share `DOC_TURN_TEXT_CHARS` (8 M characters): 20 documents keep
  400,000 each as before, 50 keep 160,000, 999 keep 8,000. A document read in part says so in the
  prompt header and in a closing line of the answer ("_Read in part — **huge.txt**: only its first
  400,000 characters were read (600 MB file). The files are kept whole and can be downloaded._").
  The engine no longer drops documents past 24 in silence (`MAX_DOCS` 1,024, named if exceeded);
  a file turn (artifacts) reads every document attached to it (was 5) and reads a CSV dataset
  only up to 64 MB, saying how many rows.
- **Archives:** an archive past `ARCHIVE_MAX_FILES` or `ARCHIVE_MAX_UNCOMPRESSED_MB` is unpacked up
  to them and the rest listed ("15 more file(s): not unpacked: only the first 10,000 entries are
  (the archive itself is kept whole)"), not refused; a bomb-shaped member, a lying header and an
  .xlsx past the caps are still refused. A dataset archive with more files than
  `PROFILE_MAX_FILES` says "profiled the first 40 of N files".
- **Datasets:** the DuckDB profiler runs with `memory_limit` 2 GB (`PROFILE_DUCKDB_MEMORY`) and
  spills to `<tmp>/duckdb-profile-spill`; its default was 80% of the machine (97 GiB here).
- **Not changed:** the /v1 Files API (`apifiles/`, its own documented limits, e.g. office files
  512 MiB), `_MAX_ARCHIVE_IMAGES` (4 pictures attached from an archive; the rest are listed), and
  image_memory's budgets (a follow-up re-sends the first pictures that fit, as before).

## store-always (backend, STORE-ALWAYS.md §1, 2026-10-03)

What the orchestrator does now, for the frontend side to rely on:

- `/chat` with inline pictures and no usable `image_ids` (absent, or a count
  that does not match the inline pictures) stores them under
  `ix-<intent_id>-<index>`, index 0..N-1 in the order of `images` (the single
  `image`/`image_base64` spelling is index 0). Only when the request's OWN
  `intent_id` fullmatches `^[0-9a-f]{32}$` (newIntentId(): randomUUID without
  dashes). The base36 fallback newIntentId() makes without `crypto.randomUUID`
  is not that shape and is not minted.
- Same background store as `image_ids` (behind the turn, `source` `chat`,
  same checks, same metrics). A retry of the same send names the same ids
  and counts `duplicate`; no second copy.
- Never minted on a turn that sends `image_refs`. With no usable intent (none
  sent, or not 32 hex), nothing is stored and each picture is counted
  `chat_media_writes_total{source="chat",result="unlinked"}` (a new closed
  value; metrics.py, chat_media.WRITE_RESULTS and the pin in
  test_chat_media_api.py changed together). A bare call (no conversation)
  still stores nothing and counts nothing. Ownership and F034 unchanged: the
  hook still runs after the feature gate and the ownership check.
- The server still never writes `meta.images`. The rows are in
  `GET /chat-media/{conv}` (`attachment_id`, `mime`, `width`, `height`),
  listed oldest first: sort the `ix-<intent>-` items by the number after the
  last `-`, not by list order. They land milliseconds to seconds after the
  send, like every /chat store; a second device that looks too early sees
  none yet.
- image_memory's store fallback (`chat_media.latest_turn_images`, both the
  visible-path and the stored-order queries) treats a user message with no
  `meta.images` as a picture turn when its `meta.intent.id` is 32 hex and the
  viewer has `ix-<that intent>-*` rows in that chat; it loads indexes 0..4 in
  order (0..998 within `max_chars` since the merge with no limits: see
  "integrator, no limits + store-always" below). A message WITH
  `meta.images` uses those ids as before, so once the frontend writes
  `meta.images` (ix- ids) nothing changes for the model.
- Not changed, known: `sharing.evaluate` blocks a public link by
  `meta.images`, so a chat whose only photo is an `ix-` row with no
  `meta.images` yet is shareable exactly as an old-tab photo chat was before
  this change (the snapshot is an allowlist: no picture leaks, the vision
  answer's text can). It closes when the frontend writes `meta.images`.

## store-always (frontend, STORE-ALWAYS.md §2 and §3, 2026-10-03)

What the browser does now, for the other side to rely on:

- A user turn is looked up when it has no `meta.images`, no bytes in this
  browser, and a `meta.intent.id` that fullmatches `^[0-9a-f]{32}$` (the same
  rule the server mints by). Its photos are the list's
  `ix-<that intent>-<n>` items, ordered by `n`, shown exactly like
  `meta.images` (thumb URL, the listed width/height reserve the box, click
  opens `size=full`). MessageRow takes them as `serverImages`.
- The list (`GET /api/chat-media/{conv}`) is read ONCE per conversation per
  page load: `lib/chatMedia.createMediaListCache`, shared by the view and the
  backfill. A read that fails is dropped and tried at the next ask. ChatApp's
  `checkForUpdates` forgets a chat's entry when the chat is stale (another
  device wrote to it), so an old-page photo turn sent from a phone shows on a
  desktop that had the chat open, after one more read, on return.
  Cost: nearly every turn sent since V29 carries an intent, so opening any
  chat with text turns costs one list read per page load (ten text turns:
  one read, test-pinned). Never one per turn.
- "Photo not stored on the server…" is said only once the list has been read
  and holds no `ix-` row for that turn (or the turn has no 32-hex intent).
  Never while the read is pending, never when it failed.
- The backfill then writes `meta.images` = `[{attachment_id, mime?, width?,
  height?}]` (no `name`; the server does not know it) through
  `amendMessages`: one save per chat, re-applied once after a 409, no move in
  Recents. So the backend's sharing gap above closes once a device has opened
  the chat. On the sending browser a turn with `ix-` rows is adopted, never
  uploaded as `bf-`; while the list is unreadable, turns with a usable intent
  wait for the next open (no `bf-` duplicate), turns without one upload `bf-`
  as before.
- Build id: `lib/buildId.ts` reads `.next/BUILD_ID` from the working
  directory once per process, in production only (null otherwise). Next lists
  BUILD_ID among the standalone server's required files, so the image has it
  at `/app/.next/BUILD_ID` (checked: `.next/standalone/.next/BUILD_ID` after
  `npm run build`); no Dockerfile change. The root layout renders
  `<meta name="techsara-build">`; `GET /api/version` answers
  `{"build": "<id>" | null}`, `cache-control: no-store`, no session read, no
  orchestrator call (the middleware already skips `/api/`).
- Client (`components/useBuildCheck.ts`, hosted by ChatApp only): checks on
  focus, on visibilitychange to visible and every 5 min while visible; one
  request in flight, none within 15 s of the last, none while hidden,
  failures silent; a page without the meta never checks. On a different id:
  if nothing streams in this tab, no send is pending, no dataset uploads, no
  message is open for editing and the composer has nothing typed, attached,
  being read or dictated (`ComposerHandle.hasDraft()`, the only Composer.tsx
  change), it lets the history store flush (3 s at most) and reloads.
  Otherwise the banner "A new version is available" + Reload; the page
  reloads by itself the first second it is quiet (polled locally). Reload
  keeps typed text in sessionStorage `techsara.reloadDraft` (keyed to path +
  query, read once). Loop guard: sessionStorage `techsara.reloadedFor`, at
  most one automatic reload per server build per tab.
- Only pages loaded from this build on have the check; tabs open before it
  ship are not reloaded by it (the server's store-always covers them).

Proof in a real Chromium (Playwright 1.63, scratch venv), the production
layout (`.next/standalone` + static + public, `node --require
./server-preload.cjs server.js`) against a fake orchestrator, torn down after:
- Fresh device, old-page turn with `ix-…-1` listed before `ix-…-0`: both thumbs
  in send order (512 px thumbs, 1600x1200 and 800x800 boxes), no legacy note,
  1 list read, 1 PUT whose user turn carries both refs with type and size and
  keeps its intent; reload: thumbs from `meta.images`, 0 PUTs. A click opened
  the full 1600x1200 image. Code block, table, mermaid diagram and the
  artifact panel (page 1 rendered) all drew. 390 px: no horizontal scroll.
- A real deploy: tab open on build A, server swapped to build B on the same
  port. Empty composer: reloaded 0.31 s after focus, now on B, 1 version
  request. With a draft: banner, no reload for 2.5 s, draft intact; Reload ->
  B with the draft back in the composer and the session key gone.
- B against a simulated build C: draft, banner, Send: no reload mid-stream,
  reload after the answer finished and was pushed (append + PUT before the
  reload's first request); the page that came back (still B) showed the
  banner and did not reload again.

Residuals: an `ix-` photo has no `name`, so the full-size preview's badge
says "FILE" (`fileBadgeFor(name)`), as for `bf-` backfills. A regenerate or
edit before the backfill has written the refs (its idle tick, under 10 s)
takes the old "missing" path. A turn with only some of its `ix-` rows shows
those.

## integrator, no limits + store-always (release/2026-10-03-nolimits, 2026-10-03 03:50 IST)

Merge 53930008 = feat/upload-limits a941ae36 + fix/image-store-always 943f7eb9; follow-ups
56132f45 (backend tests, two comments) and c9baf51a (browser tests). Code under test: c9baf51a;
the docs commit after it changes no code.

Conflicts, both sides kept: `chat_media._latest_visible_turn_images` and `latest_turn_images`
name a turn's pictures by `_turn_attachment_ids` (its `meta.images`, else the `ix-` rows under
its intent) AND pass image_memory's `max_chars`; ChatApp.tsx's imports (`CHUNK_THRESHOLD_BYTES`
beside the build-check imports); this file (both sections).

Where the two tracks meet. No rule disagrees, so no precedence was needed; each meeting point is
now a test:

- **Ref turns never mint.** An over-budget send stores its photos under the composer's ids
  (`POST /chat-media`) and sends only `image_refs`: no inline picture reaches
  `schedule_inline_store`, and `by_reference` is true. A regenerate naming 101 `ix-` ids by
  reference passes the 1..999 id validators and mints nothing.
- **Minting has no count of its own.** Every inline picture of an id-less turn is named, so N is
  bounded only by `main.MAX_IMAGES` (999): `ix-<32 hex>-998` is 39 characters, inside
  `^[A-Za-z0-9_-]{8,64}$`; the browser's `SERVER_MINTED_ID` takes 1-3 digits and sorts by
  number. Test: 101 pictures, 101 rows, each id naming the picture at its place.
- **The store fallback reads every index, bounded by characters.** `_turn_attachment_ids` names
  indexes 0..998 (`MAX_FILES`); `_load_refs(..., max_chars)` stops at image_memory's budget.
  Test: twelve `ix-` pictures uploaded out of order come back 0..11; with a three-picture
  budget, the first three. Cost of naming all 999, measured on pg-test-hand: the `= ANY` probe
  0.06 ms to execute (0.3-0.6 ms to plan) on a chat of 204 rows, and the loader's pass over 999
  ids with 3 present 4.5 ms; once per follow-up that falls back to the store for a turn whose
  `meta.images` no device has written yet.
- **No double render.** MessageRow shows local bytes, else the turn's own `meta.images`, else
  `serverImages`, never two of them. ChatApp computes `serverImages` only for a turn with no
  `meta.images`, no local bytes and a 32-hex intent, and every send from this build writes
  `meta.images` before its request, inline or by reference. Test: a by-reference turn whose
  intent also has `ix-` rows shows its 6 references once, 0 list reads, no "not stored" line.
  Mutations it catches: a lookup despite the turn's own references (1 list read); MessageRow
  joining both sources (12 thumbnails for 6 photos).
- **The reload cannot cut a by-reference send.** From Send to the /chat request the composer is
  empty and no upload is pending; `startStream` registers the stream before
  `storeImagesForSend`, and `useBuildCheck`'s quiet check needs `streamingIds()` empty. Test: a
  deploy noticed mid-upload shows the banner; the 6 photos land, /chat names them, and the tab
  reloads once, after the answer. Mutation it catches: the stream dropped from the quiet check
  (a reload during the upload).

Fail before, pass after (each parent exported with `git archive`, plus the merged test file):
store-always alone fails both new backend tests (422 "at most 5 images per message"; 5 of 12
pictures read), no limits alone fails both (0 rows minted; no picture turn found); 2 passed on
the merge.

Gates on c9baf51a (head DGX, test DB rel2_int_test on pg-test-hand):
- Backend (test_chat_media_api/chat/lifecycle, test_image_memory_restart, test_multi_image,
  test_orchestrator_hardening, test_chat_requests, and every test file no limits added or
  changed): 304 passed, 1 skipped (ffmpeg not installed), 0 failed. The bare merge 53930008:
  302 passed, 1 skipped, 0 failed.
- Goldens and contracts (test_context_assembly_golden, test_prompt_final_send, test_contract,
  test_publicapi_contract): 241 passed, 0 failed.
- Frontend: vitest 214 files, 4105 passed, 11 skipped, 0 failed (the bare merge: 213 files, 4102
  passed, 11 skipped); `tsc --noEmit` 0 errors; lint clean; `next build` OK (TypeScript
  included, `/api/version` in the route list).
- ruff_gate clean (0 documented findings); schema_parity invariants V1..V44 contiguous; `bash -n`
  44 files, 0 failed; gitleaks 8.30.1 over 6da0e929..c9baf51a: 13 commits with a patch, ~305 KB,
  no leaks, and over the merge's own combined diff (`git show --cc`, ~28 KB, which `git log -p`
  leaves out): no leaks.

Not run here: the full orchestrator shards (artifact and chart suites), the Schema job (no
migration changed; V44 is still the newest) and a real browser.

Open:
- `GET /chat-media/{conv}` is not paginated, and store-always reads it once per chat per page
  load whenever a turn could have `ix-` photos (nearly every text turn). One item is ~228 bytes
  of JSON, so 999 stored photos are ~228 KB per read and 10,000 ~2.3 MB, now that a chat has no
  count limit.

## limits: frontend fix, photo batches (QA low, 2026-10-03 03:40 IST)

- **A batch that fails for a passing reason goes again, alone.** `uploadChatMediaInBatches` re-sends
  a `POST /chat-media/{conv}` that answered 429/502/503/504 or dropped the connection, up to
  `MAX_ATTEMPTS` (5) with the chunked rail's backoff (waits of 0.25-0.5, 0.5-1, 1-2 and 2-4 s;
  `lib/uploadDocument.ts` exports the same constants). Any other status is an answer and stops the
  send as before. The batches before it are not re-sent. The backfill uses the same function, so it
  retries the same way.
- **A retried send skips what landed.** `storeImagesForSend` (the by-reference path of a send) now
  reads `GET /chat-media/{conv}` once before its POSTs and sends only the ids the server does not list;
  an unknown answer (any failure) sends them all. For the backend: every by-reference send now costs
  one list read, and that list is unpaginated (about 200 bytes per picture in the chat).

## limits: backend fixes after the NO-limit QA attack (orchestrator, 2026-10-03 04:15 IST)

For the frontend track and the release:

- **Documents never read on the event loop.** Every PDFium call (text layer, page renders, OCR
  renders), the DOCX reader and the base64 round trips run in worker threads, from
  `_resolve_document_refs` through the engine and the upload prewarm (`core.pdf.PDFIUM_LOCK` still
  serialises PDFium). QA's 50 x 40-page PDFs: longest loop stall 6.01 s -> 0.02 s. Only the first
  document that renders is rendered now; the others' renders were ~80% of reading a PDF and were
  thrown away.
- **A follow-up on many photos says how many it sees.** image_memory remembers the turn's total. The
  V41 row's `images` holds one slot per picture of the turn, `''` for one past the durable budget,
  so the total survives a restart with no new column; the store fallback reports `total` from
  `meta.images`. The model is told, and the answer ends with a counted sentence: "_I could see 13 of
  the 100 pictures from that message here; send the others again to ask about them._"
- **`image_refs`:** each distinct id is read once however often it is named (the backfill names one
  picture twice when a turn carried it twice; 999 copies of one id were 999 reads and 990 decodes,
  ~135 s before /chat answered). Past `REFS_FULL_CHARS` a ref is its stored thumbnail (512 px
  WebP, `data:image/webp`), read and never decoded.
- **Zips are never listed whole.** `core.archive.open_zip` parses at most `ARCHIVE_MAX_FILES`
  central-directory entries and 16 MiB of directory; past either it opens a view of the file whose
  new zip64 end records name only those first entries. QA's 300,000-entry zip: listing peak 170 MiB
  -> 6 MiB. Same results on Python 3.11.16 and 3.12.3 (it uses zipfile's `_EndRecData` and
  `_ECD_*`, so a Python upgrade must keep `test_listing_a_huge_archive_parses_only_the_entries_it_unpacks`
  green). The .docx readers (`core.docx`, the on-disk document sniff) use it too: a crafted .docx
  listing 70,000 parts held 78 MB, now under 16 MiB.
- **Datasets past a reading cap are kept.** An .xlsx past the caps (entries, expanded bytes, a sheet
  past the expansion ratio) or a dataset archive with a bomb-shaped member is kept whole and
  downloadable and answered **200** with `files: 0`, `profile: []` and one note
  `stored whole but not profiled: <reason>` (it was deleted with a 400 stating a limit). Hostile
  structure (sizes that lie, an unreadable zip) is still refused. The frontend may show a dataset
  chip with no tables and that note.
- **`POST /chat-media`:** a request whose pictures' headers claim more than `MAX_REQUEST_PIXELS`
  (4,000 MP, each picture counted at most 16 MP) is refused before any decode:
  413 `{"detail": "Send these pictures in smaller batches."}`. The browser's fullest batch (999 x
  1600 x 1600 = 2,558 MP) never reaches it, so the frontend needs no change.
- **Open (not changed here):** `core.docx.extract_docx_text` (whole-read path, at most 256 MiB of
  documents a turn) still inflates `word/document.xml` whole, so a .docx bomb is a memory risk there;
  `artifacts/material_in._kind_of` still parses the directory of bytes it already holds. The
  follow-up re-sends the pictures image_memory kept, not the turn's stored copies by reference.
