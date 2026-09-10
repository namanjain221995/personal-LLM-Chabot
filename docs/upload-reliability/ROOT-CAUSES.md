# Root causes — the 2026-09-09 upload incidents

Evidence is the database (`video_attachments`, `video_analyses`, `messages`,
`usage_events`) and the orchestrator log where the container still had it.
Times are UTC. Personal file names are the user's own; no content is quoted.

## Timeline of the reported attempts (user 11)

| conversation | what happened | evidence |
|---|---|---|
| `83e7de14…` | 16:59:27 a turn with TWO videos saved to history (no upload ids). 16:59:34 the 21 MB video's upload completed → analysis 20 started at upload time. The 400 MB video's chunked upload was mid-flight (parts 0–3 of 7) when the page was reloaded (`ClientDisconnect` in `uploads.chunked_part`). No `POST /chat` was ever received for this conversation. Analysis 20 finished 17:02:39. | `messages` has one user row, attachments without `id`; no assistant row; no `usage_events`; log has the upload calls and the disconnect, no chat call |
| `abb4647d…` | the same 400 MB file, uploaded in full (chunked complete 16:59:47), sent; analysis 21 (47:39 of audio) done 17:05:44; answer persisted 17:05:44 by the server. | assistant row + usage event with `generation_id` |
| `9922ba6e…` | 16:30:29 a 70 MB video sent (analysis 19 started). 16:34 the orchestrator was recreated by a deploy: the in-memory generation died; the analysis was requeued (attempt 2) and finished 16:40:50. **16:57:53 an assistant message with empty content was persisted** — no `usage_events` row, no error text. The person saw a blank answer. | assistant row `content=''`, `meta={}`; analysis 19 `attempt=2`; log line "requeued 1 video analysis(es) interrupted by a restart" at 16:34:18 |
| `11701867…` | 17:16:44 a 1:10:09 video sent; answered 17:25:21. Worked. | assistant row + usage event |
| (today) | both nodes rebooted at 14:03 (19:33 IST) — in-memory generations lost again. | `last -x reboot` on both nodes; container `StartedAt` |

The two screenshots in the report are therefore NOT one attempt: live
progress ("Transcribing 21% · Picking frames 70%") is a turn whose request
reached the server; the red "never sent" notice is a turn whose request did
not. Both can be true of the same conversation on different attempts.

## Confirmed root causes

### RC-1 — a turn is saved as sent before its request exists, and a reload has no way to tell

`frontend/components/ChatApp.tsx` send path: the user turn is appended and
`persist()`ed, then an async block awaits **every** upload (`Promise.all`)
before `startStream()` posts `/chat`. A reload during that wait leaves the
saved turn with chips and nothing else. Commit `1ded7fd` added
`meta.send_state` and a notice, but the notice's condition
(`!isStreaming(activeId)`) treats "no stream in this tab" as "nothing running
on the server", so it can also appear over a turn whose generation IS running
until `/chat/active` + `attachStream` have completed after a reload. There is
no server-side record of the intent to ask.

### RC-2 — an interrupted answer is persisted as a blank assistant message

`frontend/lib/streams.ts` `markInterrupted()` / `markUnreachable()` put
`status: 'error'` and `errorMessage` on the message object and call
`saveMessages()`. `frontend/lib/historyApi.ts` serialises a message as
`{role, content, meta}` — `status` and `errorMessage` are not in `meta`, so
the server stores `content=''`. After a reload the row renders as an empty
answer with no error and no action. (`9922ba6e`, 16:57:53.)

### RC-3 — an accepted generation is only in memory; a restart or reboot loses it and nothing resumes it

`orchestrator/app/main.py` `LiveGeneration` is an in-process object; `/chat/active`
and `/chat/attach/{id}` read the process registry. A deploy (`deploy.sh`
recreates the orchestrator), a crash, or a host reboot (twice on
2026-09-10) ends every running generation; the video **analysis** is
requeued by `video_pipeline.start()` and completes, but the question that
was waiting on it is never answered, and the browser's re-attach gets a
404 it cannot distinguish from "finished".

### RC-4 — the history PUT replaces the thread with no version, so an older browser copy can overwrite newer server rows

`orchestrator/app/db.py` `replace_messages`: DELETE all rows, INSERT the
client's list; refuses only to shrink. A tab holding an older thread of the
same length overwrites a newer server-persisted answer (the server persists
completed answers itself when no viewer is attached). Dedup exists only for
appends carrying `generation_id`.

### RC-5 — chunked upload sessions are not durable state

`orchestrator/app/uploads.py` chunked rail keeps parts on disk under the
workspace and nothing in the database until `complete`. There is no
endpoint to ask what arrived, no expected-parts/size recorded at `init`, no
per-session ownership beyond the conversation, and `complete` is not
idempotent. A reload cannot resume; it can only start over.

## Plausible risks (not yet reproduced)

* `Promise.all` in the send path fails on the first rejection while sibling
  uploads continue; the catch path then filters `meta.attachments` by `id`,
  so a later sibling completion writes into a list whose indexes moved.
* A double Send (double-click, two tabs) starts two generations for the same
  question; the second `POST /chat` cancels the first (`previous.task.cancel()`).
* The single-shot `/uploads` path (≤ 90 MB) reports no byte progress; the
  100 MB Cloudflare edge wall applies on the public path only.

## Why the earlier fix (`1ded7fd`) did not make the experience dependable

It addressed RC-1's symptom on one path (reload before the request) with a
browser-only marker and a notice, but: the marker cannot be reconciled with
the server (no intent on the server), the notice can misfire while a
generation is live (RC-1), interrupted generations still persist blank
answers (RC-2), nothing resumes an accepted request after a restart (RC-3),
and uploads still restart from zero (RC-5).
