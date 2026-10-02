# My files

`/files` (account menu → **My files**, directly above Recordings) lists
everything a signed-in person uploaded, across every chat, with their voice
recordings. Added 2026-09-30.

The owner asked: "if any user uploads anything in our AI app, it is stored,
right, but it does not show on the user side". The answer was **only partly
stored, and shown nowhere**. Uploads live in nine tables and three byte stores.
Before this page, the only upload a person could list for themselves was a
voice recording (`/recordings`). An admin could list a member's uploads, but
the member could not list their own.

Nothing here needs a database migration or a new service, and no admin view
changes.

---

## What a person sees

| Row | Comes from | Kind shown |
|---|---|---|
| an upload | an `uploads` row with `status='ready'`, in a chat the person owns | Document (`notes='document'`), Video (`notes='video'`), Audio (a `'video'` row whose name ends in one of `video.api.AUDIO_EXTENSIONS`), Spreadsheet or data (anything else) |
| a text-only document | a `documents` row with no ready upload behind it: a PDF sent inside the chat request (every PDF before 2026-09-02), or one handed to the artifact studio. Only its extracted text was ever kept. | Document |
| a recording | a `voice_sessions` row that is not cancelled and whose audio is not deleted | Voice recording |
| a picture (since 2026-10-02) | a `chat_media` row (V44, docs/chat-media/) whose own `user_id` is the person AND whose chat the person owns | Picture, named `Picture.<ext>` from its type (the table keeps no file name), shown by its thumbnail |

Archive members (`x.zip/a.txt`) and archive manifests (`x.zip (archive
contents)`) fold under the archive's upload and are not listed on their own.

**Not listed:**
- A picture sent before 2026-10-02 until the browser that still holds it opens
  its chat again (the backfill, docs/chat-media/CONTRACT.md §10).
- `/v1` developer files. They belong to a project, not a person, and the `/api`
  console already lists them.
- Generated artifacts and report files. They are not uploads.

### What each row says is stored

The server decides `availability` the same way the download route does:

| `availability` | Badge | Meaning | Actions |
|---|---|---|---|
| `available` | Stored | the original bytes are on disk: the workspace copy, the lasting copy, the video store, or a picture's full file | Download; Preview for PDFs, images and text up to 25 MB; a player for recordings |
| `text_only` | Text only | the bytes were swept and no lasting copy was kept; the text the chat read is kept | Preview (the text) |
| `summary_only` | Summary only | the bytes were swept and no lasting copy was kept; the spreadsheet profile is kept | Preview (the tables) |
| `processing` | Processing | a recording still being made or transcribed | none |
| `expired` | Removed | nothing of it is kept | none |

Rules behind the table:
- Since 2026-10-02 a document or dataset (zip and tar included) gets a
  **lasting copy** when its upload finishes, single-shot or chunked:
  `<CHAT_FILES_DIR>/<conversation>/<upload>/original`, a hard link to the
  workspace original (a fsynced copy across filesystems). It stays while the
  chat exists, so such a row stays `available` after the 24 h sweep, and an
  archive uploaded as a dataset is downloadable (its workspace original still
  goes at extraction). Below `CHAT_MEDIA_MIN_FREE_GIB` free on that disk no
  copy is made and the row ages exactly as before
  (`chat_files_lasting_total{purpose,result="no_space"}`).
- A picture is `available` while its full file is on disk under
  `CHAT_MEDIA_DIR`, else `expired`. The page builds every picture URL itself
  from the row's chat and `attachment_id`.
- A video or audio file stays `available` after the 24 h workspace sweep while
  any chat links its analysis. Its bytes are hard-linked into
  `VIDEO_DATA_DIR/<sha256>/source.<ext>`. A leftover `source.<ext>.part` (a
  cross-device copy that a crash cut short) never counts, for this list, the
  download route or the pipeline (`video.store.source_path`).
- A text-only document folds only under an upload that is itself listed
  (`status='ready'`). A rejected or failed upload of the same name no longer
  hides the text the chat kept.
- A recording's state comes from its database row alone. No stat is made, so
  where the voice store lives costs this list nothing.
- Deleting is offered **for recordings only**, through the same
  `DELETE /audio/sessions/{id}` the Recordings page uses. On a recording still
  in progress, the confirmation says that deleting it stops it and removes
  what was saved so far, in the Recordings page's own words
  (`lib/recordings.IN_PROGRESS_DELETE_NOTE`).
- A chat's files leave this list when the chat is deleted, and each row says
  so ("Deleting its chat removes it from this list."). The delete route then
  erases, best effort and after the response, the chat's lasting copies, its
  workspace upload copies and its pictures; a failure is counted
  (`chat_media_erase_total`) and the reapers finish later. No row promises an
  erasure time.

### Retention sentence

The sentence at the top of the page is built from the deployment's settings,
which come with every response (`frontend/lib/myfiles.ts retentionSentences`).
It stays true when a TTL changes. With `files_kept_with_chat: true` and
`pictures: "kept_with_chat"` (since 2026-10-02):

> Files you attach to a chat stay while the chat exists. One that could not be
> kept was removed after up to 24 hours; the chat keeps what it read of it (a
> document's text, a spreadsheet's summary). Pictures stay while their chat
> exists. One sent before pictures were kept appears here once the browser that
> sent it opens its chat again. Deleting a chat takes its files off this list
> at once; the server erases their stored copies later. Voice recordings stay
> until you delete them.

An orchestrator without those two fields keeps the earlier sentences ("kept for
up to 24 hours", "Pictures stay only in the browser you sent them from").

"Up to", because the sweep also enforces `WORKSPACE_QUOTA_GB` and a large
upload can evict a file sooner. For the same reason a swept row's note says
"The file was removed (chat files are kept for up to 24 hours)", not "removed
after 24 hours". A recording links to Recordings "with its transcript" only
once its transcription is `done`.

| Setting | Production value | Effect |
|---|---|---|
| `WORKSPACE_TTL_HOURS` | 24 | uploaded originals are swept after this many hours; the sweep also enforces `WORKSPACE_QUOTA_GB` (20), so a large upload can evict sooner |
| `VOICE_RETENTION_DAYS` | 0 | 0 keeps recordings until the owner deletes them |
| `VIDEO_ORPHAN_TTL_HOURS` | 72 | video bytes are reaped this long after the last chat link goes |
| `IMAGE_MEMORY_TTL_S` | 7200 | how long the server remembers a picture for follow-up questions |
| `CHAT_FILES_DIR` | `/data/chat-files` | lasting copies of document and dataset originals; outside `WORKSPACE_DIR`, so neither the sweep nor the 20 GB quota touches them |
| `CHAT_MEDIA_MIN_FREE_GIB` | 250 | below this much free space no lasting copy (and no new picture) is stored |
| `CHAT_MEDIA_ORPHAN_GRACE_H` | 24 | a lasting copy whose chat or uploads row is gone is reaped only after this many hours |
| `CHAT_MEDIA_REAP_INTERVAL_S` | 3600 | at most one lasting-copy reaper pass per this many seconds per process |

---

## Routes

### Orchestrator (`orchestrator/app/myfiles.py`, router prefix `/files`)

Both routes need a session (`Depends(require_user)`; 401 without one). There
is **deliberately no feature gate** (ATTACHMENTS, VOICE_INPUT or
VIDEO_ANALYSIS): a person must always be able to find what is stored about
them, the rule `/audio/sessions` follows.

**`GET /files/mine`**

| Parameter | Meaning |
|---|---|
| `kind` | comma list of `document`, `dataset`, `image`, `video`, `audio`, `recording` |
| `q` | at most 100 characters; matched literally (`%` and `_` are escaped) against the file name, and the chat title for chat files |
| `since`, `until` | ISO 8601, half-open `[since, until)`; a time without a zone is UTC |
| `min_bytes`, `max_bytes` | half-open `[min, max)`; a text-only document has no size and drops out of any size filter |
| `sort` | `newest` (default), `oldest`, `largest`, `name` |
| `limit` | 1-100, default 50 |
| `cursor` | the previous page's `next_cursor`; one minted under another sort is refused |

A NUL character in any parameter, or in a cursor's name key, is a 400:
PostgreSQL text cannot hold one, and it used to surface as a 500.

The name sort's key is `left(lower(name), 512)`, in the `ORDER BY` and in the
keyset comparison alike, and the cursor's JSON keeps it as UTF-8
(`ensure_ascii=False`). Names that agree on their first 512 characters tie on
the key and `(source, item_id)` orders them, so paging stays exact. Before
this bound the key was the whole name: a 4,916-character archive member path,
or 2,500 CJK characters escaped to a 20,063-character cursor, made the next
page a 400 (or Node's 431), and "Name, A to Z" stopped there for good. A
cursor is now at most about 2.8 KB.

Anything else in the query string is ignored. There is no `user_id` parameter.

```json
{"items": [{"id": "upload:<32hex>", "source": "upload", "kind": "document",
            "name": "Q3.pdf", "bytes": 7045, "created_at": "2026-09-30T05:08:00+00:00",
            "conversation": {"id": "…", "title": "…"}, "availability": "available",
            "media": null, "text_name": "Q3.pdf",
            "can": {"download": true, "preview": "text", "delete": false}}],
 "next_cursor": "…" ,
 "retention": {"upload_hours": 24, "recording_days": 0, "video_kept_with_chat": true,
               "video_grace_hours": 72, "files_kept_with_chat": true,
               "pictures": "kept_with_chat", "picture_memory_hours": 2}}
```

A picture row:

```json
{"id": "media:<32hex media_id>", "source": "media", "kind": "image",
 "name": "Picture.png", "bytes": 48213, "created_at": "2026-10-02T09:14:00+00:00",
 "conversation": {"id": "…", "title": "…"}, "availability": "available",
 "media": {"width": 640, "height": 480, "mime": "image/png"}, "text_name": null,
 "can": {"download": true, "preview": "image", "delete": false},
 "attachment_id": "att-…"}
```

Field notes:
- `id` is `upload:<id>`, `text:<n>`, `recording:<id>` or `media:<media_id>`.
- `media` is `{status, duration_ms}` for video, audio and recordings, and
  `{width, height, mime}` for a picture (the display size).
- `attachment_id` is on picture rows only: the page builds
  `/api/chat-media/<conversation>/<attachment_id>?size=thumb|full` from it.
- `text_name` names the `documents` row a text preview reads, and is set only
  when `can.preview` is `"text"`. It is not always the file's own name: the
  composer sends a `.zip` on the DOCUMENT rail, and the chat then keeps
  `"<name> (archive contents)"`, the manifest. This was found end to end and
  is now pinned by `test_an_archive_sent_as_a_document_keeps_its_contents_text`.

**`GET /files/mine/summary`** takes the same filters, except `kind`, `sort`
and the paging parameters. It returns
`{kinds: {<kind>: {count, bytes}}, total: {count, bytes}, retention}`. It feeds
the type chips' counts and the "6 files, 273 KB in all." line.

A refused query is a flat `400 {"detail": "<sentence>", "reason": "bad_request"}`,
the shape `audio_api.py` uses.

### Next.js

- `/api/files/mine` and `/api/files/mine/summary` (`frontend/lib/myfilesProxy.ts`).
  - GET only; any other method gets 404.
  - Only the allowlisted parameters go upstream: first value only, at most
    16,384 characters, the orchestrator's own cursor bound. A browser-added
    `user_id` never reaches the orchestrator.
  - Client forwarding headers are not relayed.
  - `MOCK_MODE=true` is answered from `lib/mockApi.ts` `handleMockFiles`.
- These proxies carry **JSON only**, because `proxyToOrchestrator` buffers the
  whole body. File bytes stream through the existing
  `/api/uploads/{conv}/{upload}/file` and `/api/audio/sessions/{id}/audio`.
- The page is `frontend/app/files/page.tsx`, which renders
  `components/myfiles/MyFilesPage.tsx` inside the `<Suspense>` that
  `useSearchParams` needs.
- Filters live in the URL: `?q=&kind=&from=&to=&size=&sort=`.
  - The search waits 250 ms after the last keystroke.
  - Each new list request aborts the previous one.
  - Paging is keyset ("Show older files").

### The download fallback (`orchestrator/app/uploads.py`)

`GET /uploads/{conversation}/{upload}/file` used to answer 410 "expired" once
the 24 h sweep removed the workspace copy. Its order is now (`kept_original`):

1. the workspace copy (`extracted/<name>`, then `_original/<name>`);
2. the lasting copy, `<CHAT_FILES_DIR>/<conversation>/<upload>/original`
   (documents and datasets, since 2026-10-02);
3. for a row the video rail wrote (`notes == 'video'`) only, the video store:
   `db.get_video_by_upload`, then `video.store.source_path(sha256)`;
4. 410.

Every fallback runs after ownership and the (conversation, upload) scoping
were checked, and serves the file under the person's own filename. The admin
download (`/admin/api/members/{id}/uploads/{upload}/download`) shares the same
fallback after `_original` and still answers 404 "The file has expired." when
nothing is left. Both answer byte ranges (206 with `content-range`, 416 past
the end): that is Starlette's own `FileResponse`, pinned by
`tests/test_chat_files.py`, and the `<video>`/`<audio>` players depend on it.

A lasting-copy reaper (`uploads.reap_lasting_files`) rides the ten-minute
upload-session sweep main.py already runs, throttled to one pass per
`CHAT_MEDIA_REAP_INTERVAL_S`. It removes `<conversation>/<upload>`
directories whose chat or uploads row is gone, only past
`CHAT_MEDIA_ORPHAN_GRACE_H`, only names it makes, never through a symbolic
link, and asks the database once per chat directory with a candidate. A
database error stops the pass without removing anything. Counted as
`chat_media_reaped_total{kind="dir"}`.

---

## Security

- **Whose rows:** the session's user id and nothing else.
  - Uploads and documents carry no owner column, so ownership is
    `conversations.user_id`, the join the admin list trusts.
  - Super admins see only their own files here. Inspection stays on the
    audited admin routes.
- **Reserved chat ids (the F034 IDOR):** every chat branch (uploads, text,
  pictures) excludes `conversation_id ~ '^u[0-9]+-'`.
  - The risk: a legacy chat created as `u7-default` names its creator as
    owner, while user 7's bare `/chat` calls stored document text under that
    same key. Without the filter, the creator would see user 7's file names.
  - This mirrors `uploads._refuse_reserved_conversation_key`.
  - Pinned by `test_a_reserved_shape_conversation_never_lists` and
    `test_the_reserved_conversation_shape_never_lists_a_picture`.
- **Pictures are the caller's twice over:** the `chat_media` row's own
  `user_id` and the chat's owner must both be the caller. A row stored under
  an id somebody else later claimed lists for neither of them (the reaper
  removes it). Pinned by `test_another_persons_pictures_are_never_listed`.
- **No new id-based reads.** Every download, preview and delete goes to a route
  that already re-derives ownership. Cursors are typed, bound parameters that
  only position a keyset inside the caller's own rows.
- **Untrusted files:**
  - Previews keep `AttachmentPreview`'s allowlist (`previewKindFor` /
    `previewMimeFor`). The renderer is chosen from the name, never from the
    type the uploader claimed.
  - HTML and SVG are never drawn inline.
  - Document text renders in a `<pre>`.
  - Downloads are always attachments.
- **Metrics carry no content.** Labels are `view` and `result`, from closed sets
  in `metrics._LABELS_BY_METRIC`. File names and queries are never logged.
  Pinned by `test_the_metrics_count_each_outcome_and_never_carry_content`.

---

## Cost

The statement fetches the page's keys first, then decorates only those rows.
Measured on the private test database (PostgreSQL 18.6, production's planner
settings, 25 runs, first page, wall time including the tunnel):

| One person's uploads | p50 | p95 |
|---|---|---|
| 2,000 (+300 recordings) | 5.6 ms | 8.2 ms |
| 20,000 | 45.7-65.0 ms | 69.7 ms |

Other costs:
- Decorating the page's rows costs under 0.5 ms.
- Availability is at most two `stat()` calls per row: 0.135 ms for a 50-row
  page on the head's NVMe.
- The summary grows linearly: 2.4, 18.5 and 89.7 ms at 2k, 20k and 100k
  uploads.
- Production held about 8 upload rows **in total** on 2026-09-30.

**When to add the index:** when one person passes ~10,000 uploads, or when the
`myfiles_list_seconds` p95 passes 50 ms, add `uploads.user_id` with an index
on `(user_id, created_at DESC, id DESC)`. It measured 0.68-0.92 ms at every
scale, a 1.0 s backfill at 200k rows, and a 91 ms index build.

`test_the_first_page_is_one_bounded_statement_at_two_thousand_uploads` guards
the statement count.

---

## Metrics (orchestrator `/metrics`)

| Metric | Labels | What it answers |
|---|---|---|
| `myfiles_list_seconds` (histogram) | `view` = list / summary | how long one list or summary request takes |
| `myfiles_list_total` (counter) | `view`, `result` = ok / bad_request / error | how often the page is used, and whether it fails |

```promql
histogram_quantile(0.95, sum by (le) (rate(myfiles_list_seconds_bucket{view="list"}[1h])))
sum(rate(myfiles_list_total{result="error"}[15m]))
```

---

## Operating it

- **Deploy:** the normal orchestrator + frontend image rebuild. There is no
  migration, no new service, no environment variable and no new port.
- **Rollback:** revert the commit. The page and its two routes disappear.
  Nothing was written to the database.
- **Deleting:** a person deletes a recording from the page. A chat's rows go
  with the chat at once, but its bytes linger:
  - an upload's workspace copy until the first sweep after it is
    `WORKSPACE_TTL_HOURS` old. The sweep (`core/repo.enforce_quota_and_ttl`)
    runs only when someone uploads a file or a repository is cloned, so on a
    quiet box that can be well past 24 h;
  - a video's analysis-store copy until the reaper finds it unlinked for
    `VIDEO_ORPHAN_TTL_HOURS` (72 h), checked every
    `VIDEO_MAINTENANCE_INTERVAL_S`.

  This is why the page gives no erasure time. Per-file deletion, and erasing
  a deleted chat's bytes at once, are phase 2.
- **Empty or odd page:**
  - Read `myfiles_list_total{result="error"}` and the orchestrator log. The
    traceback names the statement; file names are not in it.
  - A person who sees "This server doesn't have My files yet." hit a frontend
    newer than the orchestrator.

---

## Verified

- **Orchestrator:** `tests/test_myfiles_api.py` (69 tests) and
  `tests/test_uploads_video_download.py` (8), after the verification fix round
  below (61 and 6 before it).
  - Run against the code of `main` 30cee881 (the branch rebased onto it, the
    two test files copied in), 61 of the first 67 fail: the routes answer 404 and a
    swept video 410. The other 6 pass on both sides because they pin
    behaviour that must not change (the admin views, another person's 404,
    the 410 when both stores are empty, document and dataset downloads, the
    writers' `notes` markers).
  - The 20 single-kind tests (4 of them failed before the `UNION` fix) and the
    metrics test were added after the first end-to-end run.
- **Frontend:**
  - `tests/my-files-lib.test.ts`, `tests/my-files-page.test.tsx` and
    `tests/my-files-proxy.test.ts`;
  - `tests/account-menu.test.tsx`, which pins the menu entry above Recordings.
  - On the code of `main` 30cee881 all four files fail (three modules do not
    exist; the menu has no "My files").
- **End to end, in headless Chromium** against a throwaway stack on the
  worker, run twice: on the first build (2026-09-30 morning) and again on the
  commit rebased onto `main` 30cee881 (evening), with the same results:
  - Setup: loopback ports 30190-30192, a private database, and the CI stub
    engine. Member 1 attached a PDF, a CSV, a ZIP, an MP3, an MP4 and a picture
    through the composer, and dictated 10 s through Chrome's fake microphone.
  - The list showed six rows, and no picture.
  - Search, the Audio filter and largest-first all gave the expected rows.
  - The downloaded PDF's sha256 matched the source.
  - Previews showed the PDF, the CSV as a table, and the ZIP's manifest text.
  - "Open chat" landed on the right chat.
  - Deleting the recording removed it from `/files` and `/recordings`, and
    focus moved to the empty state.
  - At 390×844: no horizontal scroll, filters open or closed, and no target
    under 44 px.
  - Member 2 saw nothing.
  - No console errors.
  - After the workspace copies were removed: the PDF showed Text only with a
    working text preview, the CSV showed Summary only, and the MP4 still
    downloaded with an identical sha256.
  - The light theme (`html.light`) draws the page from the same tokens, on a
    desktop and a phone.
  - The chat around it still renders: one streamed answer with a table, a
    fenced code block and a mermaid diagram drew all three, and "give it in
    docs" opened the artifact panel on the Word file it made. (The stub
    engine was a throwaway copy that answers that markdown to a marked
    prompt; nothing of it is committed.)
- **Two defects were found only end to end, and are now fixed and pinned:**
  1. `?kind=recording` was a 500. A `UNION` takes its column names from its
     first branch, and that filter leaves the recordings branch alone.
  2. A ZIP's text preview asked for the wrong `documents` row.
- **Independent verification (2026-09-30 evening) found one medium and six
  lows; all are fixed or documented, each with a test that failed first:**
  - (medium) Delete on a recording still in progress did not say it stops
    the recording: `deleting a recording › warns that deleting a recording
    still in progress stops it` in `my-files-page.test.tsx`.
  - Name paging stalled on a long name:
    `test_a_very_long_name_does_not_stall_name_paging[cjk|archive-path]`,
    with `test_names_that_share_a_long_prefix_still_page_exactly` guarding
    the bounded key.
  - NUL in `q` or a name cursor was a 500:
    `test_a_nul_character_is_a_flat_400_not_a_500` (both routes) and
    `test_a_nul_character_in_a_forged_name_cursor_is_a_400`.
  - A `.part` in the video store was served as the whole file:
    `test_a_partial_copy_in_the_video_store_is_not_stored`,
    `test_a_partial_copy_in_the_analysis_store_is_never_served` and
    `test_adopt_source_replaces_a_leftover_partial_copy`.
  - A rejected upload hid a text row of the same name:
    `test_a_refused_upload_does_not_hide_the_text_of_the_same_name`.
  - Wording that promised more than the server does: the retention and
    row-note tests in `my-files-lib.test.ts` and `my-files-page.test.tsx`.
  - The same video twice in one chat, and ASCII-only case folding, are
    listed under *Known limits*.

---

## Known limits (v1)

- **The same video attached twice in one chat.** `video_attachments` is
  `UNIQUE (conversation_id, analysis_id)` and `ON CONFLICT DO UPDATE` keeps
  only the newest `upload_id`. After the sweep the OLDER upload finds no link:
  it lists as Removed and its download is 410, although the bytes are still
  stored under the newer upload, which downloads. The list and the route
  agree, so nothing broken is offered. The proper fix keys the link per
  `(conversation_id, upload_id)` and needs a migration.
- **Search folds case for ASCII only.** The database runs `LC_CTYPE=C`, where
  `ILIKE` folds A-Z only: `étude` does not find `ÉTUDE.pdf`, and lowercase
  Cyrillic does not find uppercase. Every other `ILIKE` search in the app
  behaves the same. Arabic, Hebrew, CJK and emoji substring search works,
  chat titles in RTL included.

---

## Phase 2 (needs owner decisions)

1. **Per-file delete for chat uploads:**
   `DELETE /files/mine/uploads/{upload_id}`. It would:
   - re-derive the owner;
   - mark the row with a tombstone status `'deleted'` (no CHECK constraint, so
     no migration);
   - delete the document text, archive members included;
   - unlink the video;
   - remove the upload directory inside `uploads._inside_uploads`.
2. ~~Deleting a chat removes its bytes at once~~: done 2026-10-02 (best
   effort, reapers as backstop; docs/chat-media/).
3. ~~Retention of originals~~: decided 2026-10-02, kept for the life of the
   chat under `CHAT_FILES_DIR` (docs/chat-media/CONTRACT.md §9).
4. ~~Pictures stored on the account~~: done 2026-10-02 (`chat_media`, V44).
5. **"Made for you" and "Shared links" tabs.**
6. **Unfinished chunked uploads, with Cancel** (v1.1). The
   `DELETE /uploads/chunked/{conv}/{id}` route already exists.
