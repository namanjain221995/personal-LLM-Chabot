# The path an MP4 takes, and where responsibility changes hands

Read with [CONTRACT.md](CONTRACT.md) (what each state means) and
[API.md](API.md) (the wire). This page is the map: every box, and — more
importantly — every arrow where one component stops being responsible and
another starts. Every incident in [ROOT-CAUSES.md](ROOT-CAUSES.md) happened
on an arrow, not in a box.

```
  BROWSER                          NEXT.JS (same origin)            ORCHESTRATOR (FastAPI)
  ───────                          ─────────────────────            ──────────────────────
  Composer.tsx
   selection ──mint attachment_id──┐
   early upload starts at ATTACH   │
                                   ▼
   uploadDocument.ts ────────► /api/upload            ────────► POST /uploads            ┐
     ≤90 MiB single-shot           (streams, no buffer)          purpose=video           │
                                                                                          │
     >90 MiB chunked:                                                                     │
       init  ──────────────────► /api/upload/chunked/init ─────► POST /uploads/chunked/init
                                                                   upload_sessions row ───┼─► POSTGRES
       part  ──X-Part-SHA256───► …/part/{i}            ─────► PUT  …/part/{i}             │   (durable from
                                                                   tmp → fsync → rename   │    init, not from
       (resume) ───────────────► GET …/{id}            ─────► GET  …/{id}                 │    complete)
       complete ───────────────► …/complete            ─────► POST …/complete             │
                                                                   one winner, replayable ┘
                                                                          │
                                                                          ▼
                                                              _finalise_video → sha256
                                                              video_analyses (content-keyed)
                                                              video_attachments (who may see it)
                                                                          │
                                                                          ▼
                                                              video/pipeline.py — DETACHED job
                                                              lease + heartbeat, stages to files
                                                              /data/video/<sha256>/…
                                                               ┌── speech ──┐ ┌── screen ──────┐
                                                               │ audio      │ │ frames         │
                                                               │ transcript │ │ ocr → captions │
                                                               └────────────┘ └────────────────┘
                                                                     └── fusion → index → artifacts
  ChatApp.tsx                                                              │
   Send ──mint intent_id──► persist user turn (local + server)             │
   startStream ───────────► /api/chat          ─────────► POST /chat  ─────┘ subscribes
                                                            chat_requests row (intent → generation)
                                                            LiveGeneration (in memory, detached)
        SSE ◄──────────────  passthrough  ◄───────────── meta / step / token / done
                                                            answer persisted BEFORE `done`
                                                            messages.generation_id (unique)
   reload / reopen:
   /api/chat/active ──────► GET /chat/active     (which conversations are live)
   /api/chat/requests/{i} ► GET /chat/requests/{i} (what became of MY send)
   /api/chat/attach/{c} ──► GET /chat/attach/{c}   (re-join, or resume an interrupted one)
   /api/history … ────────► GET/PUT /history/…     (durable thread, conditional replace)
```

## Where responsibility changes hands

| # | arrow | who owns it before | who owns it after | what used to go wrong here |
|---|---|---|---|---|
| 1 | selection → early upload | the browser's `File` object | the upload session, once `init` answers | the upload was bound to the conversation active at ATTACH time, so sending from another chat linked it to the wrong one |
| 2 | part accepted | the socket | the renamed part file + its `upload_sessions` row | a cut body left a partial file wearing a valid part's name |
| 3 | `complete` answered | the parts | the assembled `_original` + the `uploads` row | a lost acknowledgement meant a full re-upload; a missing final part assembled a short file |
| 4 | `_finalise_video` | the request | the detached analysis job (content-keyed) | nothing: this arrow was already right, and is why the analysis survived every incident |
| 5 | Send pressed | the browser's intent | `chat_requests` once the server records it | there was no server-side record, so a lost acknowledgement was indistinguishable from "never sent" |
| 6 | generation finishes | `LiveGeneration` in memory | `messages` row, written by the server before `done` | whichever browser happened to be attached was the only writer; a tab that closed took the answer with it |
| 7 | process restarts | the running process | `chat_requests` (interrupted) + the analysis lease | the generation vanished with no record while its analysis finished anyway |
| 8 | browser reloads | the live stream | durable history + the three status routes | "no stream in this tab" was read as "nothing running on the server" |
| 9 | thread pushed | the tab's copy | `messages`, conditionally on `updated_at` | an older tab could overwrite an answer the server had just persisted |

## Two facts that shape everything

**The analysis is not the answer.** `video_analyses` is keyed by the sha256 of
the bytes and shared by every conversation that attaches the same file; the
answer to *your* question is a `messages` row keyed by a generation. They
fail, resume and expire independently, which is why a restart could leave a
finished analysis with no answer, and why the fix had to be on the request
side.

**The browser is a viewer, not a participant.** Once bytes are on the server
and an intent is recorded, closing the tab changes nothing about what the
server does — only about who is watching. Everything the browser stores
(IndexedDB history, the in-tab stream registry) is a cache of a server fact,
and where the two disagree the server wins.

## What is NOT on this path

The main model, router, embedding, reranker, whisper and OCR engines are
reached over HTTP and hold no per-user state; they are shared with the rest
of the product and are out of scope here except as a source of transient
failure. The sync-worker, Salesforce ingestion, the web/knowledge layer and
Deep Research share the database and the LanceDB directories but never the
upload or chat-request tables.
