# Upload reliability — API contract

Additive changes to existing routes and a handful of new ones. Every route is
session-authenticated (`ts_session` cookie) and owner-checked: an upload
session, a chat request and a conversation belong to one user, and anything
that is not yours answers **404**, never 403 (a 403 would confirm the id
exists). Old clients that send none of the new fields get the old behaviour.

## Chunked uploads

### `POST /uploads/chunked/init`

Form fields: `conversation_id`, `filename`, `purpose` (`dataset|document|video`),
and, new and optional, `size` (total bytes), `parts` (expected part count),
`part_size` (bytes per part except the last).

Response (200):

```json
{ "upload_id": "<hex32>", "part_limit_bytes": 67108864, "max_parts": 128,
  "expires_at": "2026-09-11T14:00:00Z", "accepted_parts": [], "bytes_received": 0 }
```

The session row exists from this moment (`upload_sessions`, status
`uploading`). Refusals: 404 uploads disabled / video feature off, 400 bad
purpose, 413 `size` above the configured limit (said BEFORE any byte moves).

### `PUT /uploads/chunked/{conversation_id}/{upload_id}/part/{index}`

Body: the raw part. Optional header `X-Part-SHA256: <hex>`; when present the
server hashes the stream and refuses a mismatch with **422** (the part is
discarded, nothing is recorded).

The part is written to a temporary file and renamed into place only after
the body ended cleanly; a body cut short leaves NO accepted part (the
temporary file is removed). A part index that already exists is replaced;
quota counts it once.

Response (200):

```json
{ "received": 67108864, "accepted_parts": [0, 1, 2], "bytes_received": 201326592 }
```

Refusals: 404 unknown session / not yours / session no longer `uploading`,
400 index out of range, 413 part or total above the limit, 422 hash
mismatch, 409 the session is `finalizing`/`complete` (nothing else changes).

### `GET /uploads/chunked/{conversation_id}/{upload_id}`

What the server already has — the resume call.

```json
{ "upload_id": "…", "status": "uploading", "filename": "big.mp4", "purpose": "video",
  "expected_bytes": 400823191, "expected_parts": 7, "part_size": 67108864,
  "accepted_parts": [0, 1, 2, 3], "bytes_received": 268435456,
  "expires_at": "…", "result": null }
```

`result` is the `complete` response once status is `complete`. 404 when the
session is unknown, expired-and-swept, or someone else's.

### `POST /uploads/chunked/{conversation_id}/{upload_id}/complete`

Idempotent. Exactly one caller performs the finalisation (the row moves
`uploading → finalizing` under a row lock); a concurrent or later caller
waits for that result and receives the same body with the same status code.

Checks before assembling: every index in `0..n-1` present and `n` equals
`expected_parts` when it was declared; total bytes equal `expected_bytes`
when declared. A missing FINAL part is therefore a refusal, not a shorter
file:

```json
{ "detail": "parts are missing", "missing_parts": [6], "accepted_parts": [0,1,2,3,4,5] }
```

with **409**. Other refusals: 400 no parts, 413 total above the limit, 404
unknown. Success: the finalisation response as today (`upload_id`,
`filename`, `bytes`, `files`, `video: {analysis_id, status, reused}` …),
stored on the session as `result`.

### `DELETE /uploads/chunked/{conversation_id}/{upload_id}`

Cancels an `uploading` session and removes its parts. 204. Not permitted
once `finalizing`/`complete` (409) — the bytes belong to the conversation
then.

### Expiry

Open sessions expire `UPLOAD_SESSION_TTL_HOURS` (24) after `init`; the sweep
removes the parts of expired sessions and marks them `expired`. It never
touches `finalizing`/`complete` sessions or a session touched within the
TTL. Nothing under `/data/video/<hash>/` is affected (that is the analysis's
storage, not the session's).

## Chat

### `POST /chat` — new body field `intent_id`

`intent_id`: 1–64 chars of `[A-Za-z0-9_-]`, minted by the browser when Send
is pressed. Absent from old clients: the server mints one (no idempotency
for that client, no regression).

Behaviour when the server already knows the intent:

| existing status | response |
|---|---|
| `accepted` / `running` with a live generation | the same SSE stream (attach), no new generation |
| `completed` | a replay stream: the persisted answer as one `token`, its `meta`, then `done` |
| `interrupted` (process lost it) and `resumable` | a new attempt under a new `generation_id`; SSE as usual; `attempt` in `meta` |
| `failed` / `cancelled` | a new attempt (the person asked again) |
| known intent, different conversation or user | 409 |

A second `POST /chat` for the SAME conversation with a DIFFERENT intent
while one is live keeps today's rule (the earlier generation is cancelled):
that is a new question, not a retry.

The first SSE event of every stream is now `meta` with
`{"generation_id", "intent_id", "attempt"}` so the browser can mark the turn
`accepted` before any token exists.

### `GET /chat/requests/{intent_id}`

```json
{ "intent_id": "…", "conversation_id": "…", "status": "interrupted",
  "generation_id": "…", "attempt": 1, "resumable": true,
  "answer_persisted": false, "live": false }
```

404 when unknown or not yours. `live` says a generation for it is in this
process's registry right now; `answer_persisted` says an assistant message
with its `generation_id` is in history.

### `GET /chat/attach/{conversation_id}`

Unchanged when a live generation exists. When none exists but the
conversation's latest request is `interrupted` and `resumable`, the server
resumes it (same as a `POST /chat` with that intent) and streams the new
attempt. 404 only when there is nothing live and nothing to resume.

### `POST /chat/stop`

Unchanged for the viewer; additionally marks the request `cancelled`. A
cancel is explicit and authenticated; a viewer disconnecting is not a
cancel and never was.

### Startup / shutdown

On startup every `accepted`/`running` request is marked `interrupted`
(the process that held it is gone). Video analyses whose lease has expired
are requeued; a run with a live lease is left to its owner. On orderly
shutdown the same marking happens before the process exits, so a deploy's
rolling recreate leaves every open request resumable.

## Video analysis lease

Not a route. The pipeline claims `video_analyses.lease_owner` /
`lease_expires_at` for its process id before running a row and heartbeats
every `VIDEO_LEASE_TTL_S / 3`. `requeue_interrupted_video_analyses` only
requeues rows whose lease has lapsed.

## Metrics (Prometheus, existing conventions)

`upload_session_total{purpose,result}`, `upload_part_bytes_total{purpose}`,
`upload_finalize_seconds{purpose}`, `chat_request_total{result}`
(`accepted|attached|replayed|resumed|conflict`), `chat_request_resume_total`,
`video_lease_steal_total`. No user, conversation or intent ids as labels.

## History

### `PUT /history/conversations/{conversation_id}/messages` — new optional `expected_updated_at`

The thread replace becomes conditional. The client sends the conversation's
`updated_at` it last loaded (ISO string, exactly as `GET` returned it). If
the server's current value differs, the server answers **409**
`{"detail": "conversation changed", "updated_at": "<server's>", "messages": <count>}`
and writes nothing; the client reloads server truth and re-applies only its
LOCAL-ONLY tail (turns whose ids are not on the server). Without the field
the behaviour is unchanged (replace, never shrink). Old clients keep working;
new clients always send it, so an older tab can no longer overwrite an answer
the server persisted after the tab last loaded.

Every successful write of a conversation's messages (append, replace,
truncate, feedback) bumps `conversations.updated_at`, and `GET` returns it.
