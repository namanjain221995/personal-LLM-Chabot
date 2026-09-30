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
| a text-only document | a `documents` row with no upload behind it: a PDF sent inside the chat request (every PDF before 2026-09-02), or one handed to the artifact studio. Only its extracted text was ever kept. | Document |
| a recording | a `voice_sessions` row that is not cancelled and whose audio is not deleted | Voice recording |

Archive members (`x.zip/a.txt`) and archive manifests (`x.zip (archive
contents)`) fold under the archive's upload and are not listed on their own.

**Not listed:**
- **Pictures.** They were never stored on the account. The only copy is the
  sending browser's IndexedDB, plus `IMAGE_MEMORY_TTL_S` (2 h) of server memory
  for follow-up questions. The page says so.
- `/v1` developer files. They belong to a project, not a person, and the `/api`
  console already lists them.
- Generated artifacts and report files. They are not uploads.

### What each row says is stored

The server decides `availability` the same way the download route does:

| `availability` | Badge | Meaning | Actions |
|---|---|---|---|
| `available` | Stored | the original bytes are on disk | Download; Preview for PDFs, images and text up to 25 MB; a player for recordings |
| `text_only` | Text only | the bytes were swept; the text the chat read is kept | Preview (the text) |
| `summary_only` | Summary only | the bytes were swept (or the file was an archive, whose bytes are deleted on extraction); the spreadsheet profile is kept | Preview (the tables) |
| `processing` | Processing | a recording still being made or transcribed | none |
| `expired` | Removed | nothing of it is kept | none |

Rules behind the table:
- A video or audio file stays `available` after the 24 h workspace sweep while
  any chat links its analysis. Its bytes are hard-linked into
  `VIDEO_DATA_DIR/<sha256>/source.<ext>`.
- A recording's state comes from its database row alone. No stat is made, so
  where the voice store lives costs this list nothing.
- Deleting is offered **for recordings only**, through the same
  `DELETE /audio/sessions/{id}` the Recordings page uses. A chat's files go
  when the chat is deleted, and each row says so.

### Retention sentence

The sentence at the top of the page is built from the deployment's settings,
which come with every response. It stays true when a TTL changes:

> Files you attach to a chat are kept for 24 hours; after that the chat keeps
> what it read (a document's text, a spreadsheet's summary). Videos and audio
> files stay while their chat exists. Voice recordings stay until you delete
> them. Pictures stay only in the browser you sent them from.

| Setting | Production value | Effect |
|---|---|---|
| `WORKSPACE_TTL_HOURS` | 24 | uploaded originals are swept after this many hours; the sweep also enforces `WORKSPACE_QUOTA_GB` (20), so a large upload can evict sooner |
| `VOICE_RETENTION_DAYS` | 0 | 0 keeps recordings until the owner deletes them |
| `VIDEO_ORPHAN_TTL_HOURS` | 72 | video bytes are reaped this long after the last chat link goes |
| `IMAGE_MEMORY_TTL_S` | 7200 | how long the server remembers a picture for follow-up questions |

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
| `kind` | comma list of `document`, `dataset`, `video`, `audio`, `recording` |
| `q` | at most 100 characters; matched literally (`%` and `_` are escaped) against the file name, and the chat title for chat files |
| `since`, `until` | ISO 8601, half-open `[since, until)`; a time without a zone is UTC |
| `min_bytes`, `max_bytes` | half-open `[min, max)`; a text-only document has no size and drops out of any size filter |
| `sort` | `newest` (default), `oldest`, `largest`, `name` |
| `limit` | 1-100, default 50 |
| `cursor` | the previous page's `next_cursor`; one minted under another sort is refused |

Anything else in the query string is ignored. There is no `user_id` parameter.

```json
{"items": [{"id": "upload:<32hex>", "source": "upload", "kind": "document",
            "name": "Q3.pdf", "bytes": 7045, "created_at": "2026-09-30T05:08:00+00:00",
            "conversation": {"id": "…", "title": "…"}, "availability": "available",
            "media": null, "text_name": "Q3.pdf",
            "can": {"download": true, "preview": "text", "delete": false}}],
 "next_cursor": "…" ,
 "retention": {"upload_hours": 24, "recording_days": 0, "video_kept_with_chat": true,
               "video_grace_hours": 72, "pictures": "browser_only", "picture_memory_hours": 2}}
```

Field notes:
- `id` is `upload:<id>`, `text:<n>` or `recording:<id>`.
- `media` is `{status, duration_ms}` for video, audio and recordings.
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

### The video download fallback (`orchestrator/app/uploads.py`)

`GET /uploads/{conversation}/{upload}/file` used to answer 410 "expired" for a
video or audio file once the 24 h sweep removed its workspace copy, even though
the bytes were still in the video store.

It now falls back to that store, and only for a row the video rail wrote
(`notes == 'video'`):
- The fallback runs after ownership and the (conversation, upload) scoping
  were checked.
- It looks up `db.get_video_by_upload`, then `video.store.source_path(sha256)`.
- It serves the file under the person's own filename.

The admin download route (`/admin/api/members/{id}/uploads/...`) is unchanged
by instruction. It still reads `_original` only (follow-up).

---

## Security

- **Whose rows:** the session's user id and nothing else.
  - Uploads and documents carry no owner column, so ownership is
    `conversations.user_id`, the join the admin list trusts.
  - Super admins see only their own files here. Inspection stays on the
    audited admin routes.
- **Reserved chat ids (the F034 IDOR):** both chat branches exclude
  `conversation_id ~ '^u[0-9]+-'`.
  - The risk: a legacy chat created as `u7-default` names its creator as
    owner, while user 7's bare `/chat` calls stored document text under that
    same key. Without the filter, the creator would see user 7's file names.
  - This mirrors `uploads._refuse_reserved_conversation_key`.
  - Pinned by `test_a_reserved_shape_conversation_never_lists`.
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
- **Deleting:** a person deletes a recording from the page. A chat's files go
  with the chat, but the bytes linger until the next sweep (up to 24 h) or
  reaper run (72 h and more for video). Per-file deletion is phase 2.
- **Empty or odd page:**
  - Read `myfiles_list_total{result="error"}` and the orchestrator log. The
    traceback names the statement; file names are not in it.
  - A person who sees "This server doesn't have My files yet." hit a frontend
    newer than the orchestrator.

---

## Verified

- **Orchestrator:** `tests/test_myfiles_api.py` (61 tests) and
  `tests/test_uploads_video_download.py` (6).
  - 39 of the first 45 failed on the base commit `fd6e9beb`; the other 6 pin
    behaviour that must not change.
  - The 20 single-kind tests (4 of them failed before the `UNION` fix) and the
    metrics test were added after the end-to-end run.
- **Frontend:**
  - `tests/my-files-lib.test.ts`, `tests/my-files-page.test.tsx` and
    `tests/my-files-proxy.test.ts`;
  - `tests/account-menu.test.tsx`, which pins the menu entry above Recordings.
- **End to end, in headless Chromium** against a throwaway stack on the
  worker:
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
- **Two defects were found only end to end, and are now fixed and pinned:**
  1. `?kind=recording` was a 500. A `UNION` takes its column names from its
     first branch, and that filter leaves the recordings branch alone.
  2. A ZIP's text preview asked for the wrong `documents` row.

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
2. **Deleting a chat removes its bytes at once**, instead of at the next
   sweep or reaper run.
3. **Retention of originals:**
   - (a) keep 24 h (today);
   - (b) keep N days on the worker's 2.9 TB disk, with the storage track;
   - (c) raise the TTL and quota on the head, which shares its disk with
     production Postgres.
4. **Pictures stored on the account**, which needs a storage location.
5. **"Made for you" and "Shared links" tabs.**
6. **Unfinished chunked uploads, with Cancel** (v1.1). The
   `DELETE /uploads/chunked/{conv}/{id}` route already exists.
