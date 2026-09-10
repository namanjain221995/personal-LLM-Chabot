# Upload → analysis → answer: the lifecycle contract

One contract for the four things a person is actually waiting on, kept
apart because they fail apart: the **upload** (bytes reaching the server),
the **message** (the question being accepted and answered), the **analysis**
(what the server does to the media, which outlives the chat turn), and what
this browser tab **knows** about the server right now.

Before this, one flag (`streaming` / `datasetUpload`) stood in for all four,
a turn was saved as "sent" before its request existed, an interrupted answer
was saved as an empty assistant message, and "no stream in this tab" was read
as "nothing running anywhere". The 2026-09-09 incidents in
[ROOT-CAUSES.md](ROOT-CAUSES.md) are each one of those confusions.

## Identities

| identity | minted by | where it lives | why it exists |
|---|---|---|---|
| `attachment_id` | browser, at selection (`crypto.randomUUID()`) | `meta.attachments[].attachment_id` | files are matched by identity, never by position or filename (two `invoice.pdf` in one turn is legal) |
| `upload_id` | server, at upload start | `uploads` row; `meta.attachments[].id` once the bytes are on the server | the durable reference a request carries instead of bytes |
| chunked session | server, at `/uploads/chunked/init` | `upload_sessions` row (owner, conversation, size, parts, accepted parts, expiry, state) | so a reload can ask what already arrived and send only the rest |
| `intent_id` | browser, when Send is pressed | `meta.intent.id` on the user turn; `chat_requests` row on the server once accepted | one logical send, however many times it is retried |
| `generation_id` | server, at acceptance | `chat_requests.generation_id`, `messages.generation_id` (unique per conversation) | one answer per generation, however many viewers or persists |
| `analysis_id` | server, sha256 of the bytes | `video_analyses` | the media work, shared by every conversation that attaches the same file |
| `attempt` | server, per run of a generation or analysis | `chat_requests.attempt`, `video_analyses.attempt` | old events from an earlier attempt can never overwrite a newer one |

## Upload lifecycle (per attachment)

```
selected ──► uploading ──► finalizing ──► uploaded
               │  ▲            │
               │  └─ resume ───┘  (chunked: discover accepted parts, send the rest)
               ├──► interrupted   (tab closed / network lost before all bytes)
               ├──► rejected      (413, 415, quota, corrupt — terminal, actionable)
               ├──► cancelled     (the person removed the chip)
               └──► expired       (session past its TTL; server has reclaimed the parts)
```

* `uploading → uploaded` is only ever declared by the server (`complete`
  answered, or the single-shot POST answered).
* `interrupted` is the browser's word for "I lost the tab/network before the
  server said uploaded"; it is never the server's, and it is provisional
  until the server has been asked (`GET /uploads/chunked/{conv}/{id}`).
* `resume` needs the bytes. With the same `File` still in memory (same tab)
  it is automatic. After a reload the person is asked to re-select the file;
  the browser accepts it only when name and size match the session.
* A rejected or expired upload never retries by itself.

## Message lifecycle (per send intent)

```
draft ──► waiting_for_attachments ──► submitting ──► accepted ──► processing ──► completed
                 │                         │              │            │
                 │                         │              │            └──► failed (terminal, with a reason)
                 │                         │              └──► interrupted ──► (resume) ──► processing
                 └──► (an attachment       └──► unsent: the request never
                      failed: the turn          reached the server (network,
                      keeps its words and       auth, tab closed) — the words
                      its files; the            and files stay; Send now
                      person chooses)           re-uses the same intent_id
```

* The turn is saved to history the moment Send is pressed (so a reload keeps
  the words and the chips) **with** `meta.intent = {id, state}`.
* `accepted` is written only when the server has answered `POST /chat` with
  its `generation_id` (the first SSE event). Until then the state is
  provisional and a reload reconciles it with `GET /chat/requests/{intent_id}`:
  the server's answer wins over the browser's memory.
* A lost acknowledgement is **acceptance unknown**, not failure: the browser
  asks the server; only a definite "no such intent" makes the turn `unsent`.
* `interrupted` means the server accepted and then lost the generation (a
  restart, a reboot). The request is durable (`chat_requests` holds what was
  asked), so it can be resumed without the person retyping anything.
  Re-attaching to the conversation resumes it.
* `completed` requires the assistant message to be durable on the server
  (persisted by the server itself when no viewer is attached, or by the
  viewer; both dedupe on `generation_id`).
* A second `POST /chat` with an `intent_id` the server already knows attaches
  to the existing generation, returns the durable answer, or resumes an
  interrupted one — it never starts a second generation.

## Media-analysis lifecycle (per analysis, shared)

```
queued ──► running ──► completed
             │   │
             │   └──► completed_with_limitations  (an optional stage failed: OCR,
             │                                     captions, index — the answer
             │                                     says what is missing)
             ├──► failed      (a required stage failed: probe, audio, transcript, frames)
             └──► cancelled   (an explicit, authenticated cancel; a viewer leaving is NOT a cancel)
```

* Stages are not a line: speech (audio → transcript) and screen (frames →
  OCR → captions) run side by side; fusion → index → artifacts follow both.
  Each stage carries its own `status`, `percent` (only when measurable),
  `elapsed_s`, and `detail`.
* Every stage's output is a file; a stage is `done` only after its file is
  durable. Resume skips stages whose file exists; `pipeline_version` on the
  row invalidates all of them together.
* A run holds a **lease** (`lease_owner`, `lease_expires_at`, heartbeat);
  startup reconciliation requeues only rows whose lease has expired, so a
  second process cannot steal a healthy run.
* The analysis outlives the chat turn: a browser that closes, a stream that
  drops, a generation that dies — none of them cancel it.

## Connection knowledge (per tab)

```
connected ──► reconnecting ──► connected
                  │
                  ├──► offline        (navigator says so; nothing is inferred about the server)
                  └──► status_unknown (a status request failed; say so, retry with backoff)
```

* "No live stream in this tab" says nothing about the server. On reload,
  reopen, or reconnect the tab asks `GET /chat/active` and, for the last
  turn, `GET /chat/requests/{intent_id}` before it claims anything.
* A failed status request is `status_unknown`, rendered as such
  ("Checking with the server…", then "Couldn't reach the server — retry"),
  never as "never sent" or "file missing".

## What the person sees, per state

| state | the row says | actions |
|---|---|---|
| upload `uploading` (chunked) | "Uploading video.mp4 — 3 of 7 parts" (bytes are measurable) | Cancel |
| upload `uploading` (single) | "Uploading video.mp4…" (no invented percent) | Cancel |
| upload `interrupted`, bytes still in memory | "Resuming upload…" | — |
| upload `interrupted`, bytes gone | "video.mp4 stopped uploading at 40 %. Re-select the file to resume." | Re-select, Remove |
| upload `rejected` | the server's sentence ("larger than 4 GB", "not a video") | Remove |
| message `unsent`, all files durable | "This wasn't sent." | Send now |
| message `unsent`, a file missing | "This wasn't sent — big.mp4 never finished uploading. small.mp4 is on the server." | Re-select big.mp4 · Send with small.mp4 only |
| message `accepted`/`processing` | the live stage list ("Transcribing 40% · Reading on-screen text 3/10") | Stop |
| message `interrupted` | "The server restarted while answering. Resuming…" (video turns) / "Resume" (others) | Resume |
| message `failed` | the reason, plainly | Retry (the failed stage), Edit |
| tab `status_unknown` | "Checking with the server…" → "Couldn't reach the server." | Retry |

## Compatibility

* Old history rows have no `meta.intent`; they render exactly as before. A
  row carrying only the 2026-09-09 `meta.send_state` is treated as
  `intent.state = 'waiting_for_attachments'` with no `id`, so the reconciliation
  can only conclude "unsent" (there is nothing to look up) — which is what
  that field meant.
* Old browsers send `POST /chat` without `intent_id`; the server mints one and
  behaves as before (no idempotency for that client, no regression).
* New browsers against an old backend: `GET /chat/requests/{id}` answers 404;
  the browser treats 404-from-a-missing-route the same as `status_unknown`
  (not "unsent") by checking the response's error code, and falls back to the
  `/chat/active` behaviour.
