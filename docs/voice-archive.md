# The voice archive: stored recordings on the worker's disk

Added 2026-09-30 (branch `feat/voice-archive`, schema V43). Off until the owner
turns it on (`VOICE_ARCHIVE_ENABLED=true`); with it off nothing changes.

**The owner's ask:** "improve the storage of that audio", answered "Move to
the worker's big disk". Every stored dictation (V42, `orchestrator/app/dictation.py`)
lives in `/data/voice/<user>/<session>/` on the head's root NVMe. That disk
also holds the OS, all of `/var/lib/docker` (production Postgres included) and
`/data/video`, and it keeps one copy with no backup. The worker's disk is
3.7 TB and 17% used.

| Piece | Where |
|---|---|
| The mover, the read path, restores, the CLI | `orchestrator/app/voice_archive.py` (one daemon thread in the existing orchestrator) |
| Where each recording's audio is | `voice_sessions.archive_state` and friends (migration V43, `orchestrator/app/db.py`) |
| The store | `compose/voice-store/server.py`, image `compose/voice-store/Dockerfile`, project `compose/compose.voice-store.yaml` (worker only) |
| Deploying it | `scripts/voice-store.sh` |
| The packet filter | `scripts/host-guard.sh` (port 30011 in both worker sets) |
| Alert rules | `monitoring/prometheus/rules/voice-archive.yml`, tests `monitoring/prometheus/tests/voice_archive.yml` |
| Tests | `orchestrator/tests/test_voice_archive.py`, `compose/voice-store/tests/` |

Nothing new runs on the head (owner rule of 2026-09-16): no container, no
port, no process. The head's side is one thread and one migration.

---

## What moves, and what does not

Only `source.<ext>` moves, the recording exactly as the browser encoded it.
It moves only when the recording is **finished**: done or failed, not
deleted, no retranscription running, and `VOICE_ARCHIVE_AFTER_S` (24 h)
after it finished. That file is 99.3% of a recording's bytes (measured on the
head, 2026-09-30: 2,239,266 of 2,255,317 B).

What stays on the head: `transcript.json`/`.txt` and
`parts`/`plan`/`results.jsonl`, about 0.42 MB per hour of audio. So the
Recordings list, the previews, the session state, the transcript and the admin
transcript never wait for the worker.

Recording, decoding and transcription never touch the worker. The fsync, flock
and acknowledgement path of `dictation.append_part` is unchanged. If the
worker is down, the only things that stop are playback and retranscription of
recordings that already moved.

## The states (V43)

| `archive_state` | Meaning |
|---|---|
| `local` | Only the head has the audio. Every recording starts here, and every row that existed before V43 is `local`, which is true. |
| `copied` | The store holds a copy that was **read back and hashed**, AND the head file still exists: it was just copied, it is held, or it was brought back. |
| `archived` | The store holds the only copy and the head file is gone (`head_released_at`). A restore takes it back to `copied`. |

The other V43 columns:

- `head_hold_until`: keeps a `copied` recording on the head, for a
  retranscription or a continuation that brought it back (`VOICE_ARCHIVE_HOLD_S`, 6 h).
- `archive_attempts`, `archive_next_at`, `archive_error`: the mover's
  per-recording backoff. `archive_error` holds a reason code only, never a
  message.
- `remote_purged_at`: the store's copy of a DELETED recording is gone too.
  `audio_deleted_at` alone only says the head's copy is gone.

The migration is additive and idempotent. It adds columns with constant
defaults (a metadata-only change), one CHECK, three partial indexes and the
one-row table `voice_archive_owner` (section "Who owns a recording"), and
needs no backfill. Like V36, it sets `lock_timeout` to 3 s.

## One pass of the mover

It runs every `VOICE_ARCHIVE_INTERVAL_S` (60 s) while `VOICE_ARCHIVE_ENABLED`
is on:

1. **Health.** Call the store's `/health`. If the store is down, the pass does
   nothing more.
2. **Purge.** For every deleted recording whose store copy is not known to be
   gone (`audio_deleted_at` set, `remote_purged_at` not), whatever its
   `archive_state`, send `DELETE`. A verified copy can sit behind a `local`
   row: deleted, or retranscribed, while it was copied, or a mover killed
   between its upload and its UPDATE. The store's `DELETE` is idempotent.
3. **Release.** Release `copied` recordings whose hold has passed.
4. **Copy.** Claim up to `VOICE_ARCHIVE_BATCH` (20) due recordings, oldest
   first, with `FOR UPDATE SKIP LOCKED`, so two processes during a rolling
   recreate never take the same one. For each:
   - re-hash the head file against `source_sha256`;
   - `PUT` it paced at `VOICE_ARCHIVE_RATE_BYTES_PER_S` (20 MiB/s, in 64 KiB
     steps). Before it answers 201, the store checks the length and sha256 and
     fsyncs the file and its folder;
   - `GET` it back and hash it again;
   - mark it `copied`, on conditions re-checked in the same UPDATE;
   - release it.

On top of that, the head sweep runs hourly and the reconcile against the
store's inventory runs daily.

When a copy fails, that recording backs off: `min(3600, 60 x 2^(n-1))` s,
plus or minus 20%, with the reason stored in `archive_error`. A failure of
the STORE itself (unreachable, timeout, tls, auth, storage_full, busy, 5xx)
stops the rest of the pass, so a refused token (401) is tried once per pass,
not twenty times. A head file that cannot be read (EIO, EACCES) backs off
that recording only (`local_unreadable`); the rest of the pass goes on.

**Release** is the one step that deletes the only local copy, so three things
guard it:

1. It runs under `flock(<session>/.archive.lock)`, the lock every restore
   takes too.
2. Its UPDATE requires the row to be `copied`, finished, not deleted, not
   being retranscribed, and past any hold. The row must also not be an earlier
   part of a continuation that is still recording or finishing (the chain is
   walked recursively).
3. The file is unlinked only after that UPDATE changed the row.

A crash between the UPDATE and the unlink leaves an extra head file, which
the head sweep removes.

## Playback and download

`GET /audio/sessions/{id}/audio`, and the super admin's audited route, share
one read path (`voice_archive.recording_response`). Ownership is checked
before anything reaches the store, and every part of the store URL comes from
the row.

| Situation | Answer |
|---|---|
| The head file exists (every `local` or `copied` recording) | `FileResponse`, as before |
| `archived` | The store's copy streamed through (one pooled client per event loop, up to 32 connections, 64 KiB chunks). `Range` and `If-Range` are forwarded; 200/206/416 with `Content-Range`, `Content-Length` and `Accept-Ranges` are passed back, and a multi-range answer keeps its `multipart/byteranges` type. A Range the store refuses (400, malformed) gets the same 400 the head gives. `Content-Disposition`, `Cache-Control: no-store` and `X-Recording-Complete` are unchanged. |
| The store is down, answers 5xx or times out | **503 `archive_unavailable`**, `Retry-After: 30`, "This recording is kept on the archive server, which isn't answering right now. Nothing is lost; try again in a few minutes." |
| All 32 connections are carrying recordings (waited 5 s) | **503 `archive_busy`**, `Retry-After: 5`, "Many recordings are playing from the archive server right now. Nothing is lost; try again in a moment." |
| The store has no copy of an `archived` recording | **410 `audio_missing`**, counted, and the row flagged `remote_missing` |
| The recording was deleted | 410 `audio_deleted`, as before |

The super admin's download is audited only when audio is actually served
(200/206).

**Why 32 connections.** Every proxied playback or download holds one pooled
connection for its whole stream, and a paused player keeps its connection
until the browser lets it go (about 20 s in Chromium). With the first
build's 8, the ninth listener waited out the pool (30 s) and was told the
archive "isn't answering" while it was fine (review 2026-09-30, real
Chromium). The store serves 64 at once (`LIMIT_CONCURRENCY` in
`server.py`): a full pool from each of the two event loops that talk to it.

One narrow race is left on purpose. If the head copy is released in the
milliseconds between the existence check and the file being opened, that one
request fails (a 500, or a response cut short), and the next one streams from
the store. It can happen at most once per recording, when its grace ends. A
fix would mean serving from a file opened before the check, with Range
handled by hand, or making every playback hold the lock a release takes,
which would make a retranscription wait behind a long download. The other
half of that window is closed: a request that read the row while it still
said `local` re-reads it before answering 410, and streams from the store.

The Recordings page (`frontend/components/recordings/RecordingItem.tsx`)
reacts to a failed player with one `Range: bytes=0-0` request
(`lib/recordings.ts` `diagnosePlayback`). That request tells apart a format
this browser cannot play, an archive server that is not answering, audio the
archive lost, and audio that is gone, and the page shows the matching sentence.

## Bringing a recording back

`ensure_local(row)` runs under the same flock as release and re-reads the row
first. It never creates a session folder, the same rule as
`_write_private_json`: a discarded recording must not come back.

- **Not archived:** it only sets the hold. That hold is what stops a release
  between a retranscription's checks and its own UPDATE.
- **Archived:** it downloads into `.restore-<uuid>`, checks the length and
  sha256, fsyncs, renames into place, fsyncs the folder, and marks the row
  `copied` with the hold.
  - Below the head's `VOICE_MIN_FREE_BYTES` floor (250 GiB): 507 `storage_full`.
  - Store down: 503 `archive_unavailable`.

Callers:

- **Retranscribe** (`dictation.retranscribe`) calls it BEFORE its conditional
  UPDATE, and that UPDATE now also requires `archive_state IN ('local','copied')`.
  If the store is down, it answers 503 and the row is unchanged, and the
  refusal does not count against the hourly retries
  (`VOICE_RETRANSCRIBE_PER_HOUR`): they are counted once the audio is here.
  If the head's disk fills during the download, it answers 507
  `storage_full` and leaves nothing behind. After the retranscription the
  next pass releases the file again **without a second upload**: the bytes
  never change after finish, so the sha256 is the same.
- **A continuation** (`_Live._resolve_chain`) calls it for every earlier
  recording it decodes whose audio is not `local`. That includes `copied`
  ones, because a release whose UPDATE began before the continuation's row was
  committed can still unlink the file after the decoder read the row. If the
  store is down the session waits (`waiting_on: "archive"`, retried every
  30 s, up to `VOICE_ARCHIVE_RESTORE_WAIT_S`) instead of failing. The
  recording bar says why: "Transcript 2:00 behind — the earlier part of this
  recording is on the archive server, which isn't answering right now;
  nothing is lost" (`frontend/lib/voice.ts`, `VOICE_MESSAGES.behind`).

## Deletes and retention

`discard`, the admin delete and cancel keep their order: first the row is set
to cancelled with `audio_deleted_at`, then the head folder goes. After that,
the store is asked to delete its copy at once (2 s timeout, not waited on by
the request). The purge step retries whatever that misses, keyed on the
delete and not on `archive_state` (a copy verified just as the person
deleted the recording sits behind a `local` row), and the reason is visible
as `voice_archive_purge_pending`. Retention (`VOICE_RETENTION_DAYS`, still
0 = keep) marks the rows deleted, and the same purge removes the worker
copies.

## Who owns a recording

Added in the fix round of 2026-09-30, for the review's HIGH finding. The
store's token says who may talk to the store. It cannot say which
deployment a recording belongs to. The first build's daily reconcile deleted
every stored object that its OWN database had no row for, after a day. So an
e2e stack, a candidate or a developer's orchestrator, running on its own
database but given production's `VOICE_ARCHIVE_URL`, token and certificate,
deleted production's only copies ten minutes after it started. That was
measured: `{'orphan_deleted': 1}`, then 410 `audio_missing` on production's
playback. `scripts/e2e-stack.sh` also copied `VOICE_ARCHIVE_ENABLED` from
production, as it copies every `*_ENABLED` flag.

Now:

- **Every object names its owner.** Every `PUT`, `DELETE` and quarantine call
  carries `X-Archive-Owner`: 32 hex characters kept in the deployment's
  DATABASE, in the one-row table `voice_archive_owner` (V43), and made on
  first use. Another database is another owner, whatever settings it was
  given. The store records the owner in `manifest.json` and returns it in
  the inventory.
- **The store enforces it.** A `DELETE`, quarantine or restore from another
  owner is `409 owner_mismatch`, and nothing changes. The same bytes `PUT`
  by another owner are 409 too, and are never adopted. `verify` checks this
  on the live store: another owner's `DELETE` must answer 409.
- **Only a positive tombstone deletes.** The orchestrator deletes a store
  copy only when one of its own rows says the recording was deleted
  (`audio_deleted_at`), or when `recall-all` has brought the recording back
  with its bytes checked. A missing row is never a reason. A copy whose row
  vanished while it was being copied is kept.
- **Orphans are set aside, never deleted by the reconcile.** One of this
  deployment's objects that no row knows is counted (`orphan_waiting`) and,
  once it was stored more than **14 days** ago, set aside on the store
  (`orphan_quarantined`). It moves under `.quarantine/`, which takes it out
  of reads, the inventory, the scan and the scrub, and it is kept whole.
  Another deployment's objects (`other_owner`) and objects with no recorded
  owner (`unowned`) are never set aside or deleted.
- **Only an operator purges, and not soon.** The store refuses to purge
  anything set aside less than `VOICE_STORE_QUARANTINE_MIN_AGE_S` (30 days)
  ago, whoever asks, and only its owner may purge it.

A true orphan has three possible causes: a users row an operator deleted by
hand (the V42 cascade), a database restored from a backup older than the
recording, or a COPY of this database running as a second deployment. With
that last one, the copy shares the owner. The two-week grace lets a
short-lived copy come and go without touching anything. After that, a
production recording the copy set aside is still whole, and production
flags it `remote_missing` (`VoiceArchiveCopiesMissing`) until an operator
restores it.

The operator's commands, inside the orchestrator container:

```bash
docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive quarantine-list          # this deployment's; --all for every owner
docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive quarantine-restore <uid>/<sid>
docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive quarantine-purge          # set aside 30+ days ago; --older-than-days N
```

Never change or delete the `voice_archive_owner` row. A new owner would make
every stored recording belong to "another deployment". Nothing would be
lost, but none of them could be deleted: deletes would stay pending and
`VoiceArchivePurgeStuck` would fire. `scripts/e2e-stack.sh` no longer
inherits `VOICE_ARCHIVE_ENABLED` from production. A stack that should run
the mover says so in its `E2E_EXTRA_ENV_FILE`.

**A copy of production's database is the one case the owner cannot tell
apart.** A restored backup or a clone carries production's owner row, and
its rows say which recordings were deleted. A copy given this store's URL
and token can therefore delete production's copy of any recording that is
deleted IN THE COPY. The URL and token alone are enough for this, because
every deployment with them asks the store to delete a copy when a recording
is deleted, with or without `VOICE_ARCHIVE_ENABLED`. So, before a copy of
production's database is pointed at this store (a disaster-recovery drill,
say), make it a deployment of its own, in the copy:

```sql
DELETE FROM voice_archive_owner;  -- in the COPY only: a new owner is made on first use
```

Production's recordings then stay readable from the copy, but the copy can
no longer delete or set aside any of them.

**The privacy lag, stated plainly:** if the store is down when someone
deletes a recording, the delete takes effect for them at once, but the
worker's copy stays until the store answers again.

## Quota and free space

- The per-person quota (`VOICE_USER_QUOTA_BYTES`, 50 GiB) is computed from
  the database, so where the audio lives does not change it.
- The head's 250 GiB floor still guards create, part, decode and
  retranscribe, and now restore too.
- The store enforces its own 250 GiB floor (`VOICE_STORE_MIN_FREE_BYTES`)
  BEFORE reading a byte of a body, counting uploads already in flight. That
  floor protects the worker's Docker, model caches, OCR and speech engines,
  and the other tenant's Postgres. The mover also pre-checks it through
  `/health`.
- Recording never looks at the worker's disk.

## The store

`compose/voice-store/server.py` is a Starlette app, pinned to the
orchestrator image's versions (starlette 1.7.0, uvicorn 0.46.0), with no
FastAPI and no pydantic.

| Route | What it does |
|---|---|
| `PUT /v1/recordings/<uid>/<sid>/source.<ext>` | Requires `Content-Length` (chunked: 411), `X-Content-SHA256` and `X-Archive-Owner` (400 `owner_required`). The ids are validated (uid digits, sid 32 hex) and the extension must be one of `dictation._EXTENSIONS`, checked by a test. The body streams into `.incoming-<uuid>` (O_EXCL, 0600) and is hashed as it arrives. A length or sha mismatch is 422 and leaves nothing behind. Then the file is fsynced, **linked** into place (a link never overwrites) and the folder fsynced, and `manifest.json` (with the owner) is written the same way. The same bytes again from the same owner: 200. Different bytes, or another owner: 409, and the stored file is never replaced. At most 2 PUTs run at once (503 busy). |
| `GET`/`HEAD` same path | A `FileResponse` with Range, 206/416 and If-Range; the ETag is the sha256 |
| `DELETE /v1/recordings/<uid>/<sid>` | Requires `X-Archive-Owner`. 204 (also when there is nothing); 409 `owner_mismatch` for another owner's recording, which stays |
| `POST /v1/recordings/<uid>/<sid>/quarantine` | Its owner only: moves the recording under `.quarantine/` (out of reads, the inventory and the scrub), whole |
| `GET /v1/inventory?after=&limit=` | What is stored, each with its owner, for the reconcile |
| `GET /v1/quarantine?after=&limit=` | What is set aside, each with its owner and `quarantined_at` |
| `POST /v1/quarantine/<uid>/<sid>/restore` | Its owner only: puts it back (409 if a recording is stored under that name again) |
| `DELETE /v1/quarantine/<uid>/<sid>` | Its owner only, and 409 `too_recent` until `VOICE_STORE_QUARANTINE_MIN_AGE_S` (30 days) after it was set aside |
| `GET /health`, `GET /metrics` | No token (the host guard closes the port). `ready`, `free_bytes`, `min_free_bytes`, `reserved_bytes`, counts, scrub state |

Everything under `/v1` needs `Authorization: Bearer <token>`. The comparison
is constant-time, against a comma-separated list so a token can be rotated
without a gap. Temporary files are swept after 1 h. A weekly scrub re-hashes
every object at 50 MB/s and reports mismatches; with one copy it cannot
repair them.

The container runs as uid 1000, with a read-only root filesystem,
`cap_drop: ALL`, no-new-privileges, init, `pids_limit` 64 and a core ulimit
of 1. It runs on the A725 cores `0-4,10-14` with `cpus: 1`, so the X925 cores
stay free for the CPU whisper replica. It uses about 40 MB. Its
`oom_score_adj` is 500 (docs/developer-platform/OPERATIONS.md section 14),
and it has no `mem_limit` (`launcher/tests/test_oom_score_adj.py`).

It binds `VOICE_STORE_BIND`, which is required and has no default. That is the
worker's management address, read from `enP7s7` by the script. It never binds
a wildcard, and never a `10.100.x` rail address.

## Security

- **Token:** `secrets.token_urlsafe(32)`, minted once by
  `scripts/voice-store.sh`.
  - On the head: `.runtime/secrets.env` as `VOICE_ARCHIVE_TOKEN`, delivered
    through `env_file` and deliberately never listed under `environment:`.
  - On the worker: `~/.techsara-cluster/voice-store/store.env` (0600, umask
    077), sent over ssh **stdin**, never on a command line.
  - It is never logged.
- **TLS (the default):** a P-256 key and a self-signed certificate made ON the
  worker (the key never leaves it; 0600), with the SAN `IP:<bind address>`.
  The public PEM goes into `.env` as `VOICE_ARCHIVE_TLS_CERT_B64`, and the
  orchestrator pins it: `ssl.create_default_context(cadata=...)`, no CA
  bundle, hostname checked. An `https://` URL without the certificate is
  refused. `--plain-http` exists and matches the whisper hop's cleartext, but
  then the bearer token and whole recordings cross the office LAN in the
  clear. TLS cost nothing measurable (below).
- **The client never uses a proxy** (`trust_env=False`) and never follows
  redirects. It drops an idle pooled connection after 2 s
  (`_KEEPALIVE_EXPIRY_S`), well before the store closes one after 5 s
  (`KEEP_ALIVE_TIMEOUT_S`); a test compares the two. With httpx's default
  of 5 s the two were equal, and a request that reused a connection the
  store was closing at that moment failed with nothing wrong (503
  `archive_unavailable` to someone seeking in a moved recording). Measured
  against the candidate store over the LAN, with idle gaps within 1.5 ms of
  5 s: 32 of 240 requests failed (`RemoteProtocolError`) at the 5 s expiry,
  0 of 240 at 2 s.
- **Packet filter:** port 30011 is judged by the host guard and admitted from
  the head's management address only. `voice-store.sh up` refuses to start
  the production store until the worker's LIVE ruleset
  (`/run/techsara-host-guard/ruleset.nft`, readable without root) lists the
  port in both sets.
- **At rest:** unencrypted, as on the head. The files are 0600 and owned by
  the worker's `techsphere` account.

## Measured (2026-09-30)

Throwaway stores ran as `trackB-*`/`trackb-voice-store` on ports 30195/30196
and were removed afterwards.

**Network.** The management LAN is `enP7s7`, 1 GbE, MTU 1500, on both nodes.

| Measurement | Result |
|---|---|
| iperf3 head → worker | 952 / 940 Mb/s, 0 retransmits |
| Idle ping RTT | 0.128 / 0.275 / 0.513 ms (min / avg / max) |

**Uploads (probe, fsync + atomic rename + folder fsync).**

| Recording | Plain HTTP | TLS |
|---|---|---|
| 30 s | 12–23 ms | 16–29 ms |
| 1 h (57.9 MB) | 0.61–0.66 s | 0.51–0.52 s |
| 2 h | 1.26–1.28 s | 1.00–1.02 s |

The TLS handshake took 1.9–2.0 ms. A client without the pinned certificate is
refused.

**Pacing and its effect on ping.** The first candidate paced a 356 MB pass
in 1 MiB steps, and ping averaged 1.69 ms (max 4.26). Each step leaves at
line rate, so the step size is the burst that other traffic queues behind.
With 64 KiB steps, a 2 h recording (230 MB) was copied and read back in
22.2 s at the 20 MiB/s cap, with ping averaging **0.320 ms** (max 1.65),
against 0.275 idle. A second run of the whole candidate, taken while three
full test-suite shards loaded the head, measured 0.455 ms (max 2.02) for the
same copy and 0.396 ms (max 2.19) for the 356 MB pass. Uncapped, ping
averaged 0.968 ms. The cap allows at most 1.81 TB a day. The third run
(below) measured 0.359 ms (max 2.08) for the 2 h copy and 0.299 ms (max 1.88)
for the 356 MB pass.

**The candidate orchestrator against the real worker, over pinned TLS.**
There were three runs, each with the store brought up and removed by
`scripts/voice-store.sh up|verify|down --candidate`. The third ran the final
code rebased onto main `30cee881`, while `nvidia/Qwen3.8-27B-NVFP4` served
chat.

| Check | Result (runs 1 / 2 / 3) |
|---|---|
| 30 s speech, 5 min, 1 h and 2 h recordings | archived in one pass (34.5 / 34.7 / 34.6 s), sha256 equal on both sides |
| Head folders after the move | 2.4–38 KB each (metadata only) |
| A 64 KiB Range 200 MB into the 2 h recording, through the proxy | 19.7 / 33.7 / 81.7 ms, the right bytes |
| Full 5 min download through the proxy | 0.10 / 0.11 / 0.13 s |
| Retranscribing an archived recording | accepted in 57 / 181 / 123 ms (restore included), transcribed, left `copied` |
| Deleting one | gone on both sides |
| `voice-store.sh verify --candidate` | 8 of 8 checks pass |
| `recall-all` (the rollback), run 3 only, three times | three 2 h recordings (691 MB) back on the head in 6.6–7.0 s, sha256 checked, rows `local`, the store's copies deleted |
| `python -m app.voice_archive status`, `pass`, `reconcile`, run 3 only | all answer, exit 0 |

**Chat on the main model while recordings move (run 3, re-measured on
`nvidia/Qwen3.8-27B-NVFP4` after the 2026-09-30 swap).** A single Fast
stream (thinking off, 200 tokens, the same prompt every time) ran without a
break for 13 minutes. Meanwhile 60 s windows alternated between nothing and
the mover's exact traffic: a 2 h recording hashed on the head, PUT and read
back at the 20 MiB/s cap over pinned TLS, deleted, on repeat. There were 29
samples inside each kind of window.

| Per sample | Archive traffic on | Off |
|---|---|---|
| Median decode rate, all samples | 18.81 tok/s | 17.96 tok/s |
| Samples in the engine's normal mode (median time per token under 60 ms) | 22 of 29 | 16 of 29 |
| ... median time per token | 49.3 ms | 49.2 ms |
| ... median longest gap between two tokens | 116 ms | 119 ms |
| ... median time to first token | 0.18 s | 0.19 s |
| Samples in a slow mode (77–85 ms per token, 12–13 tok/s) | 7 of 29 | 13 of 29 |

So the traffic had no measurable effect: the time per token in the normal
mode differs by 0.1 ms. The slow mode came and went with other work on the
engine, not with the archive: it was more common with the archive off. In
those samples the worker GPU drew about 1.3 W more (36.9 W against 35.6 W)
and the head GPU about 4.6 W less (26.1 W against 30.7 W), so rank 1 was
slowed by other GPU work on the worker and rank 0 waited for it. Other
tracks' speech evaluations were running on the worker at the time. Two
earlier measurements that ran the passes back to back, without that
interleaving, had put the slow-mode samples inside the passes by chance, and
an idle sample right after a pass showed the same slow mode.

The container ran as uid 1000 with a read-only root filesystem, cpuset
`0-4,10-14` with 1 CPU, `oom_score_adj` 500, `cap_drop ALL` and
no-new-privileges. It bound only `192.168.9.68` and used 25 MiB of memory.

**Failure drills.**

| Drill | Result |
|---|---|
| `docker stop` the store | playback 503 `archive_unavailable` with `Retry-After: 30`; the pass reported `store_down`; a delete stayed pending; after `docker start` the purge drained |
| `kill -9` the store mid-PUT | no partial object, only an `.incoming-*` file (swept after 1 h); the row stayed `local` with `unreachable`; the retry archived it |
| Floor set above the free space | the store answered 507 before the body; the row waited with `storage_full` |
| Wrong token | the store counted exactly one 401; the pass stopped; `voice_archive_errors_total{reason="auth"}` |

**Capacity** at the measured 57.9 MB per hour of audio:

- the worker, down to its 250 GiB floor: about 50,270 h;
- the head's backlog if the worker stays down, down to the head's floor:
  about 40,000 h;
- the per-person quota: about 927 h.

**Footprint.** sha256 on the head runs at 1.46–1.80 GB/s. The store probe
used 33 MiB (plain) / 44 MiB (TLS).

## Configuration

The orchestrator (`orchestrator/app/config.py`; `.env`, except the token):

| Variable | Default | Meaning |
|---|---|---|
| `VOICE_ARCHIVE_ENABLED` | `false` | Starts the mover. URL and token alone already let the orchestrator play, restore and delete archived recordings. |
| `VOICE_ARCHIVE_URL` | empty | `https://192.168.9.68:30011`, written by `voice-store.sh up` |
| `VOICE_ARCHIVE_TOKEN` | empty | `.runtime/secrets.env` only |
| `VOICE_ARCHIVE_TLS_CERT_B64` | empty | The store's certificate, pinned; written by `voice-store.sh up` |
| `VOICE_ARCHIVE_AFTER_S` | `86400` | Grace after finish before a recording moves |
| `VOICE_ARCHIVE_INTERVAL_S` | `60` | Pass interval |
| `VOICE_ARCHIVE_BATCH` | `20` | Recordings claimed per pass |
| `VOICE_ARCHIVE_RATE_BYTES_PER_S` | `20971520` | Copy and read-back pace |
| `VOICE_ARCHIVE_HOLD_S` | `21600` | How long a restored recording stays on the head |
| `VOICE_ARCHIVE_RESTORE_WAIT_S` | `86400` | How long a continuation waits for a store that is down |

The store (`compose/compose.voice-store.yaml`):

| Variable | Default | Meaning |
|---|---|---|
| `VOICE_STORE_BIND` | required | The worker's management address |
| `VOICE_STORE_PORT` | `30011` | |
| `VOICE_STORE_TOKENS` | required | In `store.env`, comma-separated during a rotation |
| `VOICE_STORE_MIN_FREE_BYTES` | 250 GiB | 507 below it |
| `VOICE_STORE_MAX_OBJECT_BYTES` | 64 GiB | 413 above it |
| `VOICE_STORE_MAX_CONCURRENT_PUTS` | `2` | |
| `VOICE_STORE_SCRUB_INTERVAL_S` / `_BYTES_PER_S` | 7 d / 50 MB/s | |
| `VOICE_STORE_QUARANTINE_MIN_AGE_S` | 30 d | A recording set aside cannot be purged sooner, whoever asks |
| `VOICE_STORE_TLS_CERT` / `_KEY` | `/tls/*.pem` | Empty = plain HTTP |

## Metrics and alerts

Everything comes from the ORCHESTRATOR's `/metrics` (job `orchestrator`).
The mover relays the store's `/health`, so there is no separate scrape job
and no TLS configuration in Prometheus.

| Metric | What it counts |
|---|---|
| `voice_archive_enabled` | 1 while this process runs the mover |
| `voice_archive_last_pass_timestamp_seconds` | When the last pass ended |
| `voice_archive_backlog_sessions` / `_bytes` | Finished recordings still only on the head |
| `voice_archive_overdue_sessions`, `voice_archive_overdue_oldest_seconds` | Backlog past the grace (should be 0) |
| `voice_archive_copied_sessions`, `voice_archive_archived_sessions` / `_bytes` | Recordings in each state |
| `voice_archive_purge_pending` | Deleted recordings with a copy still on the store |
| `voice_archive_remote_missing` | Archived recordings the store lost |
| `voice_archive_store_up`, `_free_bytes`, `_min_free_bytes`, `_objects`, `_bytes`, `_scrub_mismatches` | From the store's `/health` |
| `voice_archive_copied_total`, `_released_total`, `_purged_total` | Counters |
| `voice_archive_errors_total{reason}` | reason is one of 16 closed values |
| `voice_archive_proxy_total{result}` | ok, partial, not_satisfiable, bad_request, busy, unavailable, missing |
| `voice_archive_restored_total{result}` | restored, held, unavailable, missing, mismatch, no_space, deleted |
| `voice_archive_reconcile_total{result}` | orphan_quarantined, orphan_waiting, other_owner, unowned, deleted_row_purged, repaired, remote_missing, foreign |

No label ever carries an id, a user or a path; `metrics.py` closes every set.

The data-stores exporter now walks `/data/voice` as store `voice`, which
should fall to the transcripts plus the recordings of the last day once the
archive is on.

Rules only, **no mail** (the owner's rule). The rules are
`VoiceArchiveStoreDown` (15 m), `VoiceArchiveBacklogOverdue` (6 h past the
grace), `VoiceArchiveCopiesMissing`, `VoiceArchivePurgeStuck` (1 h),
`VoiceArchiveStoreLowSpace` (within 10% of the floor),
`VoiceArchiveScrubMismatch` and `VoiceArchivePassStalled`.

## Deploying it

The branch merges with the flag off, so the merge alone changes no behaviour
beyond V43 (additive) and the read path. The route is branch → dev → dev CI
→ PR → that run green → merge, which needs the owner's click and never
`--admin`.

### Owner actions (root on the worker, once)

The only root step. It can be combined with Track C's port 30008.

After the merge, `scripts/voice-store.sh up` copies the updated
`scripts/host-guard.sh` to the worker's `~/.techsara-cluster/` and stops,
printing these commands. Then, on the worker (`ssh -t techsphere@192.168.9.68`):

```bash
sudo bash ~/.techsara-cluster/host-guard.sh plan --role worker     # read the ruleset first
sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker    # 30011 closed to all but the head
sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker  # or a reboot restores the old list
```

and from the head: `scripts/host-guard.sh verify --role worker`.

Nothing else needs root. The store runs in Docker as uid 1000, with a bind
mount under `$HOME`.

### Steps (from the head, in the deploy checkout, on main)

```bash
scripts/voice-store.sh up        # refuses (exit 2) until the guard lists 30011; then builds on the worker,
                                 # mints the token + TLS cert once, writes VOICE_ARCHIVE_URL and
                                 # VOICE_ARCHIVE_TLS_CERT_B64 to .env and VOICE_ARCHIVE_TOKEN to .runtime/secrets.env
scripts/voice-store.sh verify    # health, PUT 201, PUT again 200, GET sha, Range 206, no token 401,
                                 # another owner's DELETE 409, the owner's DELETE 204, 404
./techsara up                    # the orchestrator gets URL, cert and token (playback/restore ready; mover still off)
# set VOICE_ARCHIVE_ENABLED=true in .env, then
./techsara up
docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive status
```

If you recreate only the orchestrator by hand instead of `./techsara up`, use
the launcher's full `-f` chain and all three `--env-file` layers. A subset of
the chain silently downgrades the orchestrator to a stale `:cpu` image.

What to check afterwards:

- The first pass moves every recording that finished more than 24 h ago:
  today that is the 4 existing ones (2.25 MB, created 2026-09-29). Newer
  recordings move once their grace has passed.
- Compare `source_sha256` with `sha256sum ~/techsara-data/voice/<uid>/<sid>/source.*`
  on the worker.
- Play one and download one on the Recordings page in a real browser.
- Watch the `voice_archive_*` series and the data-stores `voice` series fall
  to metadata.

### A throwaway candidate (for a test, never production)

```bash
VOICE_STORE_PORT=30195 VOICE_STORE_PROJECT=trackb-voice-store \
VOICE_STORE_IMAGE=trackb-voice-store:test \
VOICE_STORE_DATA_DIR='$HOME/trackb-voice-store/data' VOICE_STORE_TOKEN_FILE=<0600 file> \
  scripts/voice-store.sh up --candidate      # then: verify --candidate, down --candidate
```

(From a checkout with no `.env`, such as a worktree, also set
`CLUSTER_MODE=dual CLUSTER_WORKER_SSH=techsphere@10.100.184.2`.)

A `--candidate`:

- never uses 30011;
- never writes `.env` or `.runtime/secrets.env`;
- keeps its compose file and sources in
  `~/.techsara-cluster/candidates/<project>/`, never over the production
  store's (`VOICE_STORE_REMOTE_DIR` overrides the directory);
- is exempt from the guard check, so only its token and TLS protect it.

Remove it afterwards, with its data, image and that folder.

### Rotating the token

```bash
scripts/voice-store.sh rotate-token           # the store accepts new + old; secrets.env holds the new one
./techsara up                                 # the orchestrator picks up the new token
scripts/voice-store.sh rotate-token --finish  # the store accepts only the new one
```

## Runbook

### When the store is down

`VoiceArchiveStoreDown`, `VoiceArchivePurgeStuck`.

Nothing is lost. Recording, transcription, the list and transcripts all keep
working. Playback and retranscription of recordings that already moved answer
503 `archive_unavailable`, and finished recordings wait on the head. Deletes
have already taken effect for the person, but the worker copies stay until
the store is back.

```bash
scripts/voice-store.sh status        # container state and /health
scripts/voice-store.sh logs
scripts/voice-store.sh up            # recreate (idempotent; keeps token, certificate and data)
```

When it is back, the next pass drains the purge and the backlog by itself.

### When recordings stop moving

`VoiceArchiveBacklogOverdue`, `VoiceArchivePassStalled`.

```bash
docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive status
docker logs sf-local-ai-orchestrator-1 2>&1 | grep -i "voice archive"
```

`archive_error` on the rows names the reason. `auth` means the token differs
between the head and the worker (`rotate-token`). `storage_full` means the
worker's disk (next section). `local_sha_mismatch` means the head file no
longer matches its recorded sha256: it is never copied, so investigate that
recording by hand. For one immediate pass, run
`docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive pass`.

### When the worker disk fills

`VoiceArchiveStoreLowSpace`. The store refuses uploads below 250 GiB free
(507, before any body), and recordings then wait on the head, whose own floor
(250 GiB) eventually refuses new recordings, exactly as before the archive.

Check `df -h /` and `du -sh ~/techsara-data/voice` on the worker. Free space
elsewhere on the worker, or decide on `VOICE_RETENTION_DAYS`.

### When the store lost a recording

`VoiceArchiveCopiesMissing`, `VoiceArchiveScrubMismatch`.

With one copy, lost or damaged audio cannot be recovered; the transcript is
still on the head. Before anything else writes to the worker's disk:

- read the store's log (`scripts/voice-store.sh logs`: the scrub names the
  objects);
- check the disk (`dmesg`, SMART);
- list the rows with `SELECT id, user_id FROM voice_sessions WHERE archive_error IN ('remote_missing','remote_sha_mismatch')`.

The daily reconcile clears the flag if the object reappears.

If the flagged recordings are ones the reconcile set aside (a copy of this
database running elsewhere, or a restored backup), they are not lost: look
at `quarantine-list --all`, then `quarantine-restore <uid>/<sid>` each one
(section "Who owns a recording").

### When the reconcile sets recordings aside, or sees another owner

`voice_archive_reconcile_total{result="orphan_quarantined"}` or
`{result="other_owner"}` rising, and a warning in the orchestrator's log.

- `orphan_quarantined`: this deployment's recordings that no row of its
  database knows, stored more than two weeks ago. They are kept whole under
  `.quarantine/`. Find out why the rows are gone before a
  `quarantine-purge`, which cannot take anything set aside less than 30 days
  ago.
- `other_owner`: another orchestrator, on its own database, writes to this
  store (an e2e stack or a candidate given its settings). Nothing of this
  deployment's can be touched by it, and nothing of its is touched here.
  Take the settings out of that stack. Its recordings can be removed by
  hand on the worker. Leftovers of a `verify` run that died are user `0`.

## Rollback

**Order matters.** Code that predates V43 cannot see the store, and it
answers 410 for anything archived.

1. Set `VOICE_ARCHIVE_ENABLED=false` in `.env` and run `./techsara up`. The
   mover stops. Playback of archived recordings still works, because URL and
   token remain.
2. `docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive recall-all`
   brings every recording back with sha256 checks and marks it `local`. It
   deletes the store's copies unless you pass `--keep-remote`, and it exits
   non-zero if any recording failed. It refuses (exit 2) while
   `VOICE_ARCHIVE_ENABLED` is still on, because the next pass would move
   everything out again; `--force` overrides that.
3. Only then roll back the code. V43's columns are additive and can stay.
4. `scripts/voice-store.sh down` stops the store. The data stays on the
   worker's disk until it is removed by hand.

## Risks the owner accepts

1. **One copy.** Today there is one copy on the head's single NVMe. After
   release there is one copy on the worker's single NVMe. There is no backup:
   the only volume backup (2026-08-20) predates V42. Losing the worker's disk
   loses every archived recording, and the scrub detects damage but cannot
   repair it. The owner decides between:
   - a longer grace (`VOICE_ARCHIVE_AFTER_S`), which keeps audio on the head
     longer, but still as the only copy until it moves;
   - a "keep both copies for N days" mode, which is not built: release would
     wait N days after `archived_at`;
   - an off-box backup.
2. **The worker is TP rank 1.** The store is pinned to the A725 cores with
   1 CPU, uses about 40 MB, writes at most 20 MiB/s and never touches RoCE.
   Nothing about it can restart vLLM, and chat measured the same with the
   archive's traffic on and off (49.3 against 49.2 ms per token on the 27B,
   "Measured" above).
3. **The LAN is shared** with the whisper hop and the tunnel's egress. At the
   cap, average ping rises by 0.05–0.2 ms (0.32–0.46 ms against 0.275 idle).
4. **The guard comes first.** Without it, 30011 would be reachable from the
   office LAN and the tailnet with only the token and TLS in the way. That is
   why `up` refuses to start the production store until the live ruleset
   lists the port. A reboot without `install-boot` would restore the old
   list.
5. **The head's footprint is not literally zero:** one thread, one migration
   and the proxy code.

## Options that were rejected

- **NFS or sshfs:**
  - needs root on both nodes;
  - puts every 5-second part's fsync and all PCM writes on the network;
  - a hard mount leaves orchestrator threads in D state when the worker
    reboots, which drains the AnyIO pool that database calls share, so chat
    stalls too;
  - a soft mount turns an outage into EIO and torn writes;
  - flock has a grace period on NFSv4 and does not exist on sshfs;
  - recording would stop whenever the worker is down.
- **MinIO / S3:** AGPL-3.0, a credential and console to manage, and about 10x
  the memory.
- **rsync over ssh:** an SSH key inside the orchestrator container, and no
  Range reads for playback.
- **Writing parts straight to the worker:** the same availability problem as
  NFS.
