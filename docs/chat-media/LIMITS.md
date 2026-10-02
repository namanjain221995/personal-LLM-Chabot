# Upload limits: NO limit (owner decision 2026-10-03, replaces the "raise safely" plan)

The owner: "no limit, users can upload unlimited". This replaces the earlier 20-per-message plan.
Per person there is already no limit (PR #94: no total, no quota, kept for the life of the chat).
Now the per-file and per-message upload limits go too.

| What | Before | After |
|---|---|---|
| One photo | 10 MB on the original | **any size** (the browser shrinks to 1600 px first; the stored-file checks apply to the shrunk file) |
| Photos per message | 5 (this branch so far: 20) | **no limit in the app** (technical ceiling 999 = vLLM's per-prompt maximum; the UI never mentions it below that) |
| Files per message | 5 (this branch so far: 20) | **no limit in the app** (same technical ceiling 999 for request validation) |
| One document / spreadsheet / archive | 512 MB | **no app limit**: the server's `UPLOAD_MAX_MB` (production `.env`: 102400 = 100 GB) is the only size rule; big files go through the existing chunked upload |
| One video / audio file | 4 GB / 4 h | **no app limit on size**: `VIDEO_MAX_UPLOAD_MB` follows `UPLOAD_MAX_MB`; a video longer than the analysis window is accepted and stored; the analysis covers the first `VIDEO_MAX_DURATION_S` (4 h) and the answer says so instead of refusing |
| Per person total | unlimited | unlimited |

What stays, and why (these are not user limits):
- The global free-disk floor (`CHAT_MEDIA_MIN_FREE_GIB`, 250 GiB) protects the database and the model
  from a full disk. 2.4 TB is free today.
- Cloudflare refuses any single request body over 100 MB. Nothing may depend on one big request: photos
  over the inline budget go by reference (`POST /api/chat-media` in batches under the budget, then
  `image_refs` on `/chat`); documents and videos already use the chunked upload (64 MiB parts).
- Reading has physical bounds, and they must degrade gracefully, never crash the server and never refuse
  the upload:
  - **Model context (1M tokens).** Many photos: when their image tokens would not fit the turn's budget,
    send them to the model at a smaller size (e.g. 896 px ≈ 527 tokens each, 640 px ≈ 300) rather than
    dropping any; if even that cannot fit, send what fits and say how many were read.
  - **Router (Qwen3-VL-8B, 65,536 tokens).** Never hand it more images than fit; routing needs few.
  - **Head memory is off limits** (owner rule). A huge document or dataset must never be read into
    memory whole. Check every extraction path (PDF, DOCX, PPTX, text, CSV/TSV/XLSX/Parquet/JSON profile,
    archive listing) for whole-file reads; bound them (stream, page/row/byte budgets) and let the answer
    say what part was read. The stored file is always kept whole and downloadable on every device.
  - **Video.** One analysis job at a time; the window above keeps one upload from holding the slot for a
    day.

## Work already on this branch (keep what fits, change the numbers)

- Backend commit 110436b7 raised the server counts to 20 and fb57ce62 wrote the request budget in
  NOTES.md. Change every such cap to the "no limit" rule above (ceiling 999 where a bound is needed for
  validation).
- The frontend track's work is UNCOMMITTED in the worktree (by-reference sending over the budget, shrink-
  first size rule, new wording, tests including `frontend/tests/upload-limits.test.tsx`). Continue from it:
  review it, change 20 to "no limit", commit it.

## Words

No message may state a limit that no longer exists. Toasts like "You can attach up to N images" go
away. Where reading is partial (huge document, very long video, too many photos for the model at full
size), the answer or a note says exactly what was read.

## Tests

- 100 photos in one message: accepted, sent by reference over the budget, all stored, all shown on a
  second device, the model call fits (smaller size), nothing refused.
- A 30 MB original photo is accepted (shrunk).
- 50 documents in one message: accepted, all chips on a second device; the document context budget is
  shared, not multiplied.
- A document larger than 512 MB (sparse file in tests) is accepted by the frontend and the server, stored
  with its lasting copy, and its extraction stays within a memory bound (assert no whole-file read).
- A video over 4 GB / longer than 4 h is accepted and stored; the analysis covers the window and says so.
- No toast or doc still claims an old limit (grep the tree for "up to 5", "up to 20", "512 MB", "4 GB"
  in user-facing strings).
