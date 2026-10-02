# Chat media: pictures are stored on the server

Every picture sent in a chat is now kept on the server for as long as the chat
exists, and shows in that chat on any device or browser after login. Added
2026-10-02, schema **V44**. The build contract is [`CONTRACT.md`](CONTRACT.md);
late decisions between the tracks are in [`NOTES.md`](NOTES.md).

This page covers the orchestrator side: what is stored and where, the routes,
the metrics, how to operate and roll it back, and what it does not do.

## Why it exists

A photo sent from a phone showed above the message on the phone. The same chat
opened on a desktop showed the question and the answer, and no photo. The only
lasting copy was in the sending browser's IndexedDB:

- the bytes reached the orchestrator only inline in the `/chat` body;
- the request snapshot (`_request_snapshot`) strips them;
- `engines/image_memory.py` (the V41 `conversation_images` row) keeps a chat's
  latest picture for two hours, and only so the model can answer follow-ups.

Production chat `4ab7ac45…` had a vision answer, and no bytes anywhere.

---

## What is stored, and where

**One row per picture** in `chat_media` (V44). It holds the owner, the chat,
the composer's `attachment_id`, the sha256, the mime type, the size, the
dimensions, `has_thumb` and `source`.

`source` says how the picture arrived:

| `source` | Meaning |
|---|---|
| `chat` | inline bytes on `/chat` |
| `upload` | the upload route |
| `backfill` | a browser re-sending a photo it still holds from before V44 |

**The bytes are files.** They are not stored in the database (no bytea).

```
CHAT_MEDIA_DIR/<user_id>/<conversation_id>/<media_id>/full.<jpg|png|webp|gif>
                                                      /thumb.webp   (only when made)
```

- Directories are `0700`. Files are `0600`.
- `CHAT_MEDIA_DIR` defaults to `/data/chat-media`. That is the
  `sf-local-ai_data` volume, on the head's root NVMe.
- It is outside `WORKSPACE_DIR`, so the 24 h workspace sweep and the 20 GB
  quota never touch it.
- `media_id` is minted by the server (`uuid4().hex`). Nothing a client sends
  ever becomes a path component.

**A thumbnail is made** only when the long edge is over 512 px or the file is
over 200 KiB. It is a WebP, 512 px on the long edge, quality 80, with EXIF
orientation applied, alpha kept, and the first frame of a GIF. For a smaller
picture, `size=thumb` serves the full file.

**What is accepted.** Only JPEG, PNG, WebP and GIF are kept, and only when all
four of these agree:

1. the file's magic bytes;
2. the file ends the way a whole file of that format ends: a JPEG EOI after
   its last scan, the PNG `IEND` chunk with its CRC, a GIF whose blocks walk to
   the trailer, a WebP as long as its RIFF size. A truncated file fails here;
3. Pillow, opened for that one format only, and its `verify()`;
4. a full decode.

Step 2 does not trust Pillow. Pillow's truncation check is the process-wide
switch `ImageFile.LOAD_TRUNCATED_IMAGES`, and WeasyPrint turns it on when it is
imported, which the artifact renderer does in this process. With it on, Pillow
decodes a cut JPEG or GIF without an error.

Everything else is refused: SVG, HTML renamed `.png`, HEIC, BMP, TIFF, and
truncated files. Further limits:

- Pillow's decompression-bomb guard stays on.
- A decode is capped at 40 MP, or 89 MP for a JPEG. This is the same bound
  `image_memory` uses.
- At most 10 MiB per picture and 5 per request. These are the composer's own
  `MAX_IMAGE_BYTES` and `MAX_IMAGES`.

**Write order.** The steps run in this order:

1. Write to a temporary name in the same directory.
2. `fsync` the file.
3. Rename it into place.
4. `fsync` the directory.
5. Insert the row.

`UNIQUE (user_id, conversation_id, attachment_id)` decides a race. The first
write wins, and the loser removes its own files. So the bytes behind a URL never
change, which is why `Cache-Control: immutable` is safe.

**The reference lives on the message, and the browser writes it.** The user
message's `meta.images` is `[{attachment_id, name?, mime?, width?, height?}]`.
The server never writes into a message's meta: the history PUT replaces meta
whole, so the next push from any tab would erase it.

---

## Routes

Every route uses `require_user`. `conversation_id` must match
`^[A-Za-z0-9_-]{1,64}$`. The F034 reserved shape `^u\d+-` is refused.

A picture is yours only when both of these hold:

- the row's `user_id` is you;
- the chat is yours, or has no `conversations` row yet (a brand-new chat whose
  first history push has not landed).

Every other case gets **the same 404 body**:
`{"code":"not_found","detail":"No such picture."}`.

| Route | What it does |
|---|---|
| `POST /chat-media/{conv}` | Multipart. Up to 5 `file` parts, then `attachment_id` text parts in the same order. `source` is `upload` (default) or `backfill`. Every picture is checked before any is stored. An `attachment_id` that already has a row answers that row with `created:false`, and its bytes are not read again. **400** bad shape (count mismatch, a bad or duplicate id, more than 5 files, a bad `source`); **403** the account may not attach; **404** not yours; **413** over 10 MiB; **415** not a verified raster; **507** below the free-space floor. Body cap: **64 MiB** (`main.body_cap_for`). |
| `GET /chat-media/{conv}` | The viewer's pictures in that chat, oldest first. |
| `GET /chat-media/{conv}/{attachment_id}?size=thumb\|full` | The bytes, streamed with `FileResponse`. |
| `GET /admin/api/members/{user_id}/chat-media/{conv}/{attachment_id}?size=` | The same response, for the audited conversation viewer. See below. |

The byte route behaves like this:

- **Headers** on every byte response, the 304 included:
  - `Content-Type`: the row's mime, or `image/webp` for a thumbnail;
  - `X-Content-Type-Options: nosniff`;
  - `Content-Security-Policy: default-src 'none'; sandbox`;
  - `Content-Disposition: inline; filename="image.<ext>"`;
  - `Cache-Control: private, max-age=31536000, immutable`;
  - `ETag: "<sha256>"`, or `"<sha256>-t"` when a thumbnail is served.

  The 200 also carries `Content-Length`.
- **`If-None-Match`.** A matching tag (`W/` and `*` included) answers 304 with
  no body.
- **A row whose file is gone** answers 410 `{"code":"media_missing"}`. This
  happens only for your own row; anyone else gets the 404.

The admin route:

- It is gated like every other inspection read: `Cap.WORKSPACE_CONTENT_READ`
  plus `_inspectable_member`. An admin never reads a super admin's pictures.
  The refusal is a 404 and writes no audit event.
- Each 200 and each 304 writes an `admin_viewed_chat_media` audit event. The
  resource is `chat_media`/`media_id`, and the meta is
  `{conversation_id, size}`.

**`/chat`** gained two fields:

- **`image_ids`.** These are the attachment ids of the inline pictures.
  - At intake, after the feature gate and the ownership check, the turn's
    pictures are stored **in a background task**.
  - This applies on every route: vision, document plus picture, and artifact.
  - The first token waits on none of it. A failure is logged and counted, and
    never surfaces as a chat error.
  - If the counts do not match, nothing is stored. This is logged and is never
    a 4xx.
  - A bare call (no `conversation_id`) stores nothing.
- **`image_refs`.** These are pictures that are already stored, sent instead of
  bytes.
  - They are loaded before anything durable happens: full files, never
    thumbnails.
  - They go ahead of any inline pictures, and count against the same cap of 5.
  - A ref that cannot be loaded answers 422
    `{"detail":{"code":"image_ref_missing","missing":[…]}}` before a stream or
    a `chat_requests` row exists.
  - The request snapshot keeps both lists of ids and never the bytes. A turn
    that sent only refs stays resumable.

**Follow-up questions.** `image_memory.hydrate` falls back to the store when
neither the process nor the V41 row has a live picture. It takes the newest
user message whose `meta.images` names stored pictures, together with that
turn's question and answer. A chat with no stored picture pays one index probe
(the plan gates on it) and behaves as before. Turn the fallback off with
`IMAGE_MEMORY_STORE_FALLBACK=0`.

**Sharing.** `sharing.PRIVATE_META_KEYS` has `images`, so a chat with stored
photos cannot get a public link. The snapshot never carried pictures in any
case: it is an allowlist.

---

## Deletion

- **A deleted chat.**
  1. Its rows go in the delete transaction (`db._SIDE_TABLES`).
  2. Right after the response, `history.py` removes
     `CHAT_MEDIA_DIR/<user>/<conv>` and `CHAT_FILES_DIR/<conv>`, off the event
     loop and best effort.
  3. A failure is counted (`chat_media_erase_total{result="error"}`), and the
     reaper finishes the job.
- **A deleted account.** The `users` FK cascades the rows. The reaper removes
  the directories. Removing a member does **not** delete the `users` row, so a
  removed member's pictures stay.
- **The reaper** runs in each process at most once per
  `CHAT_MEDIA_REAP_INTERVAL_S` (default 3600). The first pass runs 5 minutes
  after start. Each pass removes:
  - rows older than `CHAT_MEDIA_ORPHAN_GRACE_H` (default 24) whose chat has no
    row owned by the same user (never created, deleted, or claimed by someone
    else), together with their directories, at most 1000 rows per pass;
  - media directories that no row names, older than the grace.

  Names it did not create are never touched. The grace exists because `/chat`
  and the upload route can store a picture before the browser's first history
  push creates the chat's row.
- **Nothing promises an erasure time**, and the UI must not either.

---

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHAT_MEDIA_DIR` | `/data/chat-media` | Where the pictures live. |
| `CHAT_FILES_DIR` | `/data/chat-files` | Where the lasting copies of document and dataset originals live (CONTRACT §9). |
| `CHAT_MEDIA_MIN_FREE_GIB` | `250` | Below this free space, new bytes are refused: 507 on the route, skipped and counted on `/chat`. This is the project's floor for the head's root NVMe. |
| `CHAT_MEDIA_REAP_INTERVAL_S` | `3600` | How often the reaper runs, per process. The minimum is 60. |
| `CHAT_MEDIA_ORPHAN_GRACE_H` | `24` | How old an orphan must be before the reaper removes it. The minimum is 1. |
| `IMAGE_MEMORY_STORE_FALLBACK` | on | `0` turns off the follow-up fallback to stored pictures. |

There is no per-person quota beyond the free-space floor. That is the owner's
default from 2026-10-02.

---

## Metrics (orchestrator `/metrics`)

No label ever carries a user, a chat, an attachment id or a file name.

| Metric | Labels | What it answers |
|---|---|---|
| `chat_media_writes_total` | `source` = chat / upload / backfill; `result` = stored / duplicate / unsupported / too_large / no_space / error | Pictures written, and why some were not. |
| `chat_media_write_seconds` | `source` | Time to verify and durably store one picture. |
| `chat_media_reads_total` | `size` = thumb / full; `result` = ok / not_modified / not_found / missing | Byte reads, including the admin route. `missing` is a 410. |
| `chat_media_erase_total` | `store` = media / files; `result` = ok / error | Bytes removed at chat deletion. |
| `chat_media_reaped_total` | `kind` = row / dir | Orphans the reaper removed. |
| `chat_files_lasting_total` | `purpose` = document / dataset; `result` = stored / no_space / error | Lasting copies of uploads (the files track, CONTRACT §9). |

Signals worth watching:

- `result="no_space"` or `"error"` above zero on `chat_media_writes_total`;
- any `result="missing"` on `chat_media_reads_total`, meaning a file vanished
  under a live row;
- `chat_media_erase_total{result="error"}`.

---

## Operating it

- **Disk use.** Run `du -sh /data/chat-media` inside the orchestrator
  container. A composer picture is about 0.2–2 MB, because the browser
  downscales to 1600 px, plus a thumbnail of a few tens of KB.
- **Not backed up.** `scripts/backup-knowledge.sh` covers Postgres and LanceDB
  only. Like `/data/video` and `/data/voice`, the media are on one disk.
- **Stopping new writes without a deploy.** Set `CHAT_MEDIA_MIN_FREE_GIB` far
  above the disk size (for example `100000`) and restart the orchestrator.
  Uploads then answer 507 and `/chat` skips storing. Reads, deletion and the
  reaper keep working.
- **Rolling back.**
  - V44 is additive and forward-only. `scripts/deploy.sh` refuses to start
    code from before V44 on a database that has applied it, so the way back is
    a forward fix, not an image rollback.
  - The behaviour switches above (`IMAGE_MEMORY_STORE_FALLBACK=0`, the floor)
    turn off the new paths without touching the schema.
  - Dropping the table is never needed. If the feature were abandoned, the rows
    and `/data/chat-media` could be removed by hand after an owner decision.

---

## Honest limits

- **Pictures sent before V44 are on the server only if a browser backfills
  them.** The browser that sent a photo, and still holds it in IndexedDB, uploads
  it the next time it opens that chat. A photo whose only browser has logged out,
  cleared its site data or hit Safari's 7-day purge is gone for good.
- **There is a moment when a picture is not on the server yet.** The store runs
  behind the turn. Until it lands, which takes milliseconds to seconds, a second
  device shows "Image unavailable" for that picture.
- **The free-space floor applies to everyone together**, and there is no
  per-person quota.
- **A deleted chat's bytes go promptly but not atomically.** If the immediate
  erase fails, the reaper removes them on a later pass. The rows are always
  gone at once.
- **A row's `conversation_id` is client-chosen.** As with V41,
  `_SIDE_TABLES` deletes by conversation id alone. A colliding id can therefore
  drop another account's rows early: only an account that stored pictures under
  an id nobody owned yet, which the deleter then claimed. Their pictures are
  dropped early, never disclosed.
- **The share policy reads only non-empty messages** (`_is_shareable_message`).
  A user message with no text, only a picture, is not inspected for
  `meta.images`; the vision answer after it is still `route=vision`, which the
  policy allows. This gap is the same one `meta.attachments` has. The photo
  itself can never leak, because the snapshot is an allowlist.

---

## Lasting originals for every other upload (be-files, CONTRACT §9)

- **What is kept.** When a document or dataset upload finishes (single-shot
  `POST /uploads` or chunked `complete`; zip and tar travel as `document`),
  `uploads.keep_lasting_copy` hard-links the workspace original to
  `<CHAT_FILES_DIR>/<conversation>/<upload>/original` (default
  `/data/chat-files`, outside `WORKSPACE_DIR`). On one filesystem that writes
  no bytes; across filesystems it falls back to a fsynced copy. A dataset's
  copy is made before extraction drops the workspace original, so an uploaded
  archive is downloadable now. Videos and audio get no copy: the video store
  already keeps them while a chat links them.
- **The floor.** Below `CHAT_MEDIA_MIN_FREE_GIB` free on CHAT_FILES_DIR's own
  filesystem (`uploads.lasting_has_room`, nearest existing ancestor) no copy
  is made, the upload is never refused for it, and the file ages out with the
  workspace sweep as before. Outcomes:
  `chat_files_lasting_total{purpose=document|dataset, result=stored|no_space|error}`.
- **Reads.** `GET /uploads/{conv}/{id}/file` and the admin download share
  `uploads.kept_original`: workspace, lasting copy, video store (video rail
  rows only), then 410 (the admin route keeps its 404 "The file has
  expired."). Byte ranges (206/416) are Starlette's own `FileResponse`.
  My files counts a lasting copy as `available`.
- **Deletion.** `uploads.erase_conversation_files` removes
  `<CHAT_FILES_DIR>/<conv>` AND `<WORKSPACE_DIR>/uploads/<conv>`: the lasting
  copy is a hard link to the workspace file, so removing only one name would
  leave the bytes on disk until the sweep.
- **Reaper.** `uploads.reap_lasting_files` rides main.py's ten-minute
  upload-session sweep (`sweep_expired_upload_sessions`), at most one pass per
  `CHAT_MEDIA_REAP_INTERVAL_S` per process. It removes a
  `<conversation>/<upload>` directory past `CHAT_MEDIA_ORPHAN_GRACE_H` whose
  chat (deleted, or its account deleted) or uploads row is gone. It never
  follows a symbolic link and never judges a name it did not make. Counted as
  `chat_media_reaped_total{kind="dir"}`.
- **Operating it.** Disk use: hard links share the workspace's bytes for the
  first 24 h, then the lasting copy holds them alone. `du -sh /data/chat-files`
  inside the orchestrator container is the store's size. The same floor
  switch as pictures (`CHAT_MEDIA_MIN_FREE_GIB` set far above the disk) stops
  new copies with no deploy.
- **Honest limits.** An upload made before this change, or below the floor,
  has no lasting copy and still expires after the sweep. `GET /uploads/{conv}`
  (the per-chat list) still reports `expired` from the workspace copy alone;
  the file route and My files are the ones that know about the lasting copy.
  `/data/chat-files` is not in `scripts/backup-knowledge.sh` (nor are
  `/data/chat-media`, `/data/video` or `/data/voice`).
