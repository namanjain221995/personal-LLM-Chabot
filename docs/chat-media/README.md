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
2. the file ends the way a whole file of that format ends: a JPEG whose
   marker segments walk to the primary picture's EOI (bytes after it, such as
   an MPO's further pictures or a motion photo's video, are ignored), the PNG
   `IEND` chunk with its CRC, a GIF whose blocks walk to the trailer, a WebP as
   long as its RIFF size. A truncated file fails here;
3. Pillow, opened for that one format only, and its `verify()`;
4. a full decode.

Step 2 does not trust Pillow. Pillow's truncation check is the process-wide
switch `ImageFile.LOAD_TRUNCATED_IMAGES`, and WeasyPrint turns it on when it is
imported, which the artifact renderer does in this process. With it on, Pillow
decodes a cut JPEG or GIF without an error.

Everything else is refused: SVG, HTML renamed `.png`, HEIC, BMP, TIFF, and
truncated files. Further limits:

- Pillow's decompression-bomb guard stays on.
- A picture over 16 MP (`chat_media.MAX_STORE_PIXELS`, every format, read from
  the header before any decode) is refused as unsupported. The composer never
  sends more than 1600 px on the long edge. `image_memory` keeps its own,
  higher bound for inline pictures.
- Every decode (the upload route's check, the `/chat` background store, a heal
  and the follow-up fallback's read) runs on a 2-thread pool
  (`chat_media.DECODE_WORKERS`), so a burst queues instead of filling the
  default executor with ~0.5 GiB decodes.
- A multi-picture JPEG (Pillow's `MPO`) is stored as `image/jpeg`, `full.jpg`.
- At most 10 MiB per picture, checked on the bytes sent (the composer has
  already shrunk a photo to 1600 px), and 20 per request (5 until 2026-10-03,
  [`LIMITS.md`](LIMITS.md)). The browser batches its uploads so one POST holds
  at most `chat_media.BATCH_BUDGET_BYTES` (48 MiB) of pictures.

**Write order.** The steps run in this order:

1. Write to a temporary name in the same directory.
2. `fsync` the file.
3. Rename it into place.
4. `fsync` the directory.
5. Insert the row.

`UNIQUE (user_id, conversation_id, attachment_id)` decides a race. The first
write wins, and the loser removes its own files. So the bytes behind a URL never
change, which is why `Cache-Control: immutable` is safe.

**Heal.** When a row exists but its full file is gone, a retry of the SAME
bytes (equal sha256) under the same attachment id rewrites `full.<ext>` (and
`thumb.webp` when the row has one) into the row's own directory. The route
answers `created:false` with the unchanged row, and the metric counts
`result="stored"`. Different bytes never replace a row; it stays 410. Nothing
re-sends such bytes on its own today: the browser's repair re-sends only ids
the list lacks.

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
| `POST /chat-media/{conv}` | Multipart. Up to 20 `file` parts, then `attachment_id` text parts in the same order. `source` is `upload` (default) or `backfill`. Every picture is checked before any is stored. An `attachment_id` that already has a row answers that row with `created:false`; its bytes are compared only when the row's file is gone (the heal above). **400** bad shape (count mismatch, a bad or duplicate id, more than 20 files, a bad `source`); **403** the account may not attach; **404** not yours; **408** the body was cut off; **413** over 10 MiB; **415** not a verified raster; **507** below the free-space floor; **500** `store_failed`. Every refusal is `{"code","detail"}` except 403 and 408, which keep the upload rail's `{"detail"}`. Body cap: **64 MiB** (`main.body_cap_for`): one 48 MiB batch plus framing. |
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
  happens only for your own row; anyone else gets the 404. A matching
  `If-None-Match` still answers 304 for such a row: the tag is the sha256, so
  the browser's cached copy is exactly right.

The admin route:

- It is gated like every other inspection read: `Cap.WORKSPACE_CONTENT_READ`
  plus `_inspectable_member`. An admin never reads a super admin's pictures.
  The refusal is a 404 and writes no audit event.
- Each 200 and each 304 writes an `admin_viewed_chat_media` audit event. The
  resource is `chat_media`/`media_id`, and the meta is
  `{conversation_id, size}`.
- It answers `Cache-Control: private, no-store` on 200, 304 and 410 (the
  recordings rail's rule), not the member route's year-long `immutable`. So an
  admin's second view reaches the server and writes a second audit event.

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
  - They go ahead of any inline pictures, and count against the same cap of 20
    (`main.MAX_IMAGES`; 5 until 2026-10-03). A send whose inline pictures
    would pass the 48 MiB budget goes by reference.
  - A ref that cannot be loaded answers 422
    `{"detail":{"code":"image_ref_missing","missing":[…]}}` before a stream or
    a `chat_requests` row exists.
  - The request snapshot keeps both lists of ids and never the bytes. A turn
    that sent only refs stays resumable.
  - A turn with refs and no words gets "Analyze the attached image." as its
    question, like an inline picture.
- An account without the ATTACHMENTS feature has both lists cleared with the
  rest of its attachments (the usual "Photos and files" notice), never a 422.
- Each list holds at most 20 ids matching `^[A-Za-z0-9_-]{8,64}$`; anything else
  is pydantic's 422 (a malformed body), which the Next proxy never forwards.

**Follow-up questions.** `image_memory.hydrate` falls back to the store when
neither the process nor the V41 row has a live picture. It follows the branch
the person sees: `/chat`'s `messages` are passed as `visible`, and
`chat_media.latest_turn_images` takes the newest picture turn ON that path
(matched by its words, or, for a photo with no words, by the assistant message
stored under it), with that turn's question and answer. A picture on an
edited-away branch is never read into a later turn. `turns_after` counts user
turns on the path after it, not the question being asked now. Only the 20
newest picture turns are compared, and nothing is written back to the V41 row.
A request with no `messages` keeps the stored-order behaviour. A chat with no
stored picture pays one index probe (the plan gates on it) and behaves as
before. Turn the fallback off with `IMAGE_MEMORY_STORE_FALLBACK=0`.

**Sharing.** `sharing.PRIVATE_META_KEYS` has `images`, so a chat with stored
photos cannot get a public link. `sharing.evaluate` reads that provenance
(private meta keys, private routes, attachments) from EVERY user and assistant
message, empty ones included, so a chat whose only turn is a photo with no
words answers 422 "uploaded photos" too. The snapshot never carried pictures in
any case: it is an allowlist.

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
- **`/chat` accepts only `data:<type>;base64,` data URLs for storage.** A data
  URL with parameters (`data:image/png;name=x.png;base64,...`) is counted
  `unsupported` and not stored. The composer never sends one.

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
  My files and `GET /uploads/{conv}` count a lasting copy as `available`
  (`expired` only when neither copy is on disk). The member route also serves
  a kept file whose name the path resolver refuses (a leading dot, a
  backslash) from the lasting copy, with `os.path.basename` of the name.
- **Regenerate, edit and retry.** main.py `_resolve_document_refs` falls back
  to the lasting copy (`_kept_document`) when the workspace `_original` is
  gone, if THIS conversation's uploads row names it. An archive re-extracts
  into the workspace, which the TTL sweeps again later. With no lasting copy
  the old "no longer available on the server" sentence stands.
- **Stored names.** `uploads._upload_filename`: an empty name, `.` and `..`
  become `upload.bin`; a name over 240 bytes keeps its extension (up to 32
  bytes) and loses the end of its stem. Before, single-shot kept a 245-255
  byte name and the chunked rail failed with a 500.
- **Conversation ids** are matched with `fullmatch` at every claim site
  (uploads `_own`, `lasting_path`, `erase_conversation_files`, history POST
  /history/conversations with a 400, and `/chat`'s claim with a 422), so a
  trailing newline is refused.
- **Deletion.** `uploads.erase_conversation_files` removes
  `<CHAT_FILES_DIR>/<conv>` AND `<WORKSPACE_DIR>/uploads/<conv>`: the lasting
  copy is a hard link to the workspace file, so removing only one name would
  leave the bytes on disk until the sweep.
- **Reaper.** main.py's `_upload_session_sweep_loop` calls
  `uploads.maybe_reap_lasting_files` after each ten-minute upload-session
  sweep (first pass ten minutes after start), at most one pass per
  `CHAT_MEDIA_REAP_INTERVAL_S` per process. No request path reaps:
  `sweep_expired_upload_sessions`, which POST /uploads also runs, does not. It removes a
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
  has no lasting copy and still expires after the sweep. A dataset listed
  `ready` after the sweep has no `extracted/` (the lasting copy restores only
  the original); the dataset engine behaves for it as before.
  `/data/chat-files` is not in `scripts/backup-knowledge.sh` (nor are
  `/data/chat-media`, `/data/video` or `/data/voice`).
- **Disk is not bounded per member.** The lasting copy is a hard link, so the
  workspace quota (`WORKSPACE_QUOTA_GB`) and the 24 h TTL free nothing for
  documents and datasets any more. The only guard is the global free-space
  floor, shared with every member's new pictures and copies. This is the
  owner's "no per-user quota" default (CONTRACT §2); the `DiskFillingUp`
  alert (root over 85% full) fires before the 250 GiB floor. Raise it with the
  owner before release.
- **Malformed byte ranges answer 400.** Starlette 1.6 `FileResponse` refuses
  an unknown unit (`items=0-1`), a malformed or a reversed range. RFC 9110
  says an unknown unit should be ignored (200). No browser player sends one;
  this was true before this branch.

---

## Where the build differs from CONTRACT.md

Each is deliberate and recorded in NOTES.md under the named track.

| CONTRACT | As built | Track |
|---|---|---|
| §3 "Pillow's decompression-bomb guard stays on" and nothing more | Also a 16 MP store ceiling for every format and a 2-thread decode pool | fix-be |
| §3 "On a UNIQUE conflict the existing row wins ... new files are removed" | Still true, except that the SAME bytes rewrite a row's missing file (heal) | fix-be |
| §3 accepted types jpeg/png/webp/gif | An MPO JPEG is accepted and stored as `image/jpeg`; bytes after the primary picture's EOI are ignored | fix-be |
| §4.1 errors 400/404/413/415/507 | Flat `{"code","detail"}` bodies, plus 403 (attachments gate) and 408 (cut body) with `{"detail"}`, plus 500 `store_failed` | be-media |
| §4.3 `size=thumb` answers `image/webp` with ETag `"<sha256>-t"` | For a picture with no thumbnail, `size=thumb` serves the full file with its own mime and ETag `"<sha256>"` | be-media |
| §4.3 410 when the row exists and the file does not | A matching `If-None-Match` still answers 304 first | fix-be |
| §4.4 `/admin/members/...`, "same headers as (3)" | `/admin/api/members/...`, with `Cache-Control: private, no-store` instead of `immutable` | be-media, fix-files |
| §5 `image_refs` "same order" as inline images | Refs come first, then inline pictures; the frontend never sends both in one request | be-media |
| §5 (not covered) | ATTACHMENTS feature off clears `image_ids`/`image_refs` (no 422); a ref-only turn with no words gets "Analyze the attached image." | be-media, fix-be |
| §6 "the newest USER message ... whose `meta.images` is non-empty" | The newest picture turn on the branch the person sees (`visible`), at most 20 compared; `turns_after` excludes the question being asked | fix-be |
| §7 `PRIVATE_META_KEYS` gains `images` | Also: `sharing.evaluate` reads provenance from empty messages, so a photo-only chat cannot be shared | fix-be |
| §8 reapers "started like the other background sweeps" | Media: `chat_media.reap_loop`, first pass 5 min after start. Lasting files: after main.py's upload-session sweep, first pass 10 min after start | be-media, fix-files |
| §9 `erase_conversation_files` removes `<CHAT_FILES_DIR>/<conv>` | Also removes `<WORKSPACE_DIR>/uploads/<conv>`, the hard link's other name | be-files |
| §9 admin download "gets the same fallback" | Same fallback, but its last answer stays 404 "The file has expired." (not 410) | be-files |
| §9 (not covered) | Regenerate/edit/retry of a document reads the lasting copy; upload names are shortened or replaced; conversation ids use `fullmatch` everywhere | fix-files |
| §9 My files picture rows | `name` is `Picture.<ext>` (chat_media has no file name); retention flags `files_kept_with_chat: true`, `pictures: "kept_with_chat"` | be-files, fe-files |
| §10 legacy line under any imageless turn followed by a vision answer | At most one line per thread: the first photo-less user turn under a vision answer, only when no earlier turn has a photo or a vision answer | fix-fe |
| §10 backfill only for turns without `meta.images` | Also a repair: a turn WITH `meta.images` whose ids the list lacks is re-sent under the same ids (2 min grace) | fix-fe |
| §10 "404/410 -> an Image unavailable tile" | A thumbnail retries 3 times (2 s, 6 s, 15 s) before the tile | fix-fe |
| §10 edit by reference | `runEdit` asks the list once before it writes the new version; a missing ref shows the re-attach toast and writes nothing | fix-fe |
| §10 CSP "`media-src 'self'` if `default-src` would block it" | Not needed: `default-src 'self'` admits the route; a test pins that `blob:` stays out | fe-files |
| §10 admin inspection uses the admin route "if that view renders MessageRow" | It does not, so no frontend calls the admin route yet | fe-images |
| (not in CONTRACT) | Logout answers `Clear-Site-Data: "cache"`, so a year-long cached photo is not served to the next person at the keyboard | fix-fe |
