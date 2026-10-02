# Upload limits, raised safely (owner decision 2026-10-03)

The owner wants uploads unlimited. Per person they already are: no total, no per-user quota, kept for
the life of the chat (PR #94, V44). For the limits per file and per message, the owner chose the
"raise safely" option:

| Limit | Before | After |
|---|---|---|
| One photo | 10 MB, checked on the ORIGINAL file | **any size**: the browser shrinks every photo to 1600 px first and the size rule applies to what is actually sent |
| Photos per message | 5 | **20** |
| Files per message (documents, datasets, archives, videos, audio) | 5 | **20** |
| One document or spreadsheet | 512 MB | unchanged (bigger files risk the head's memory) |
| One video | 4 GB / 4 h | unchanged (one long job blocks the single video slot) |
| Per person, total | unlimited | unlimited |

Only the global free-disk floor (`CHAT_MEDIA_MIN_FREE_GIB`, 250 GiB) stays; it protects the database
and the model from a full disk.

## What must hold

- **Photo size.** A photo the browser can shrink is accepted whatever its original size (a 30 MB, 48 MP
  phone photo must work). A photo the browser cannot decode or shrink (for example HEIC in desktop
  Chrome) keeps a size rule on what would be sent (10 MB) and says so clearly. The server's stored-file
  checks (`chat_media.MAX_IMAGE_BYTES`, pixel and decode bounds) stay: they apply to the shrunk file.
- **20 photos.** Every cap that says 5 for photos becomes 20 and stays in one place per side where
  possible: `frontend/components/Composer.tsx MAX_IMAGES`, `frontend/lib/orchestrator.ts MAX_IMAGES`,
  `orchestrator/app/main.py MAX_IMAGES` (and the `image_ids` / `image_refs` validators),
  `chat_media.py` per-POST `MAX_FILES` and any test pins. Find every other one (grep).
- **No request-size wall.** Cloudflare refuses a request body over 100 MB, and `/api/chat` caps at
  128 MiB. Twenty shrunk photos are about 10-20 MB of base64, which is fine, but twenty photos that could
  not be shrunk could reach 270 MB. When the inline images of one send would exceed a safe budget (pick
  one well under 100 MB, e.g. 48 MB of base64), the browser must upload them to `POST /api/chat-media`
  first (batches that each stay under the budget) and send `image_refs` instead of inline bytes. The
  server path for `image_refs` already exists and already loads the bytes for the engines.
- **Bodies.** `body_cap_for` for `POST /chat-media/{conv}` and its pinned test must allow a batch of
  photos up to the same budget; the Next proxy's POST cap likewise.
- **The model.** vLLM accepts up to 999 images per prompt (`limit_per_prompt` default, vLLM
  0.28.1rc1), so no model restart is needed. Check every engine that receives the turn's images:
  - the main model (vision engine) gets all of them;
  - the router (Qwen3-VL-8B, 65,536-token window) must not be sent 20 full-size images for routing; if
    it receives images today, cap or shrink what it gets so the routing call cannot overflow;
  - `image_memory` budgets (`IMAGE_MEMORY_MAX_CHARS`, `IMAGE_MEMORY_DB_CHARS`) must degrade gracefully
    (keep what fits; the chat_media store fallback covers the rest) and never fail the turn.
  Measure once against the real main model, briefly and at low traffic: one request with 20 shrunk
  1600 px images and `max_tokens` small, report time to first token and prompt tokens. One request only.
- **20 files.** Every per-message cap on documents/datasets/videos (`Composer.tsx MAX_DOCS`, the
  `/chat` request lists such as `document_uploads` / `video_uploads` / `pdf_uploads` and their
  `fail_fast` validators, any engine-side cap, the document context budget) becomes 20, or is shown to
  be already higher. The document context budget is shared, not multiplied.
- **Words.** Every toast or note that states a limit must state the new numbers. No message may
  promise "unlimited" for things that are still capped (document 512 MB, video 4 GB).
- **Tests.** Change every pinned test that encodes the old numbers so it encodes the new ones, and add
  tests: a 30 MB original photo that shrinks is accepted; 20 photos accepted, the 21st refused with the
  new wording; a send whose inline payload would exceed the budget goes by reference; 20 documents in
  one message; the router never receives more than its safe share.
