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
  `settings.chat_files_dir` at the test's tmp_path for every test.

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
