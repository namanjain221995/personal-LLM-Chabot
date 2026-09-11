# Recovery drills

Three things this stack claims to survive: the orchestrator being recreated
while an analysis is running, the frontend being recreated while a part is
uploading, and the host rebooting under all of it. Nothing in CI exercises any
of them — `deploy.sh`'s health gate and the pipeline's verify job stop at
`/health`, `/login`, `/v1/models` and one 8-token completion (INF-15) — so
they are drills, run by hand, deliberately.

**These are destructive. Run them on purpose, announced, never during someone
else's session, and never as part of a deploy.** Each one recreates a
container or reboots a node. Nothing here is wired into `deploy.sh`,
`deploy-smoke.sh` or `pipeline.yml`, and it should stay that way: a drill that
runs automatically is a drill nobody watches.

Read `docs/upload-reliability/OPERATIONS.md` §5 first — it states what each
drill is supposed to prove.

---

## Setup, once per session

```bash
cd /home/techsphere/Documents/project/personal-LLM-Chabot

# The compose chain, read back from the launcher's own state.json. NEVER type
# a subset of the -f files: a subset silently resolves the orchestrator to the
# stale :cpu image (scripts/lib/deploy-common.sh).
. scripts/lib/deploy-common.sh
PREFIX="$(dr_compose_prefix)"

# Read-only database access, as the application user, inside its container.
pg() { docker exec -e PGOPTIONS="-c default_transaction_read_only=on" \
         sf-local-ai-postgres-1 psql -U techsara -d techsara -tA -c "$1"; }

# What the box is working on — the number every assertion below reads.
# /metrics carries it today; /health carries it too once main.py's handler
# passes `work` through (see docs/upload-reliability/OPERATIONS.md §6).
work() {
  curl -s localhost:8080/health \
    | python3 -c 'import json,sys; d=json.load(sys.stdin).get("work"); print(json.dumps(d, indent=2)) if d else None'
  curl -s localhost:8080/metrics \
    | grep -E '^(live_generations|video_queue_depth|upload_sessions_open|chat_requests_interrupted|video_running_stage_age_seconds)'
}

export VIDEO_SMOKE_EMAIL='<the drill account>'
export VIDEO_SMOKE_PASSWORD='<its password>'
```

A test video long enough that an analysis is still running a minute later —
ten minutes of footage or more. A 400 MB file also gives drill 2 seven parts
to work with.

Record the start state before every drill:

```bash
work
pg "SELECT status, count(*) FROM video_analyses GROUP BY status"
pg "SELECT status, count(*) FROM chat_requests GROUP BY status"
pg "SELECT status, count(*) FROM upload_sessions GROUP BY status"
```

---

## Drill 1 — orchestrator recreated mid-analysis

**Claim.** The analysis survives; the generation waiting on it does not, and
is left resumable rather than lost or blank.

### Run

Terminal A — start a real turn over a long video and leave it streaming:

```bash
scripts/video_smoke.py --video /path/to/long.mp4 \
    --ask "summarise the decisions" --base http://127.0.0.1:8080
```

Terminal B — wait until the analysis is genuinely running, then recreate:

```bash
until [ "$(pg "SELECT count(*) FROM video_analyses WHERE status='running'")" -gt 0 ]; do sleep 2; done
pg "SELECT id, status, stage, attempt, lease_owner, lease_expires_at FROM video_analyses ORDER BY id DESC LIMIT 1"
date -u +%T; work

eval "$PREFIX" up -d --no-deps --force-recreate orchestrator
```

### Assert

1. **The stop was graceful, not a kill.** The container exited on its own
   inside the 120 s grace:

   ```bash
   docker inspect sf-local-ai-orchestrator-1 --format '{{.State.ExitCode}} {{.State.StartedAt}}'
   ```

   Exit code `0` (uvicorn ran its shutdown) — **not** `137`, which is SIGKILL
   and means the graceful timeout did not fit inside `stop_grace_period`.

2. **The shutdown hook ran.** In the log of the container that stopped:

   ```bash
   docker logs sf-local-ai-orchestrator-1 2>&1 | grep -E 'interrupted for shutdown|interrupted by a restart|requeued'
   ```

   Expect `marked N open chat request(s) interrupted for shutdown` from the
   OLD process. Its absence is the INF-3 regression: the process was killed
   before the hook.

3. **The request is interrupted and resumable, not failed and not blank.**

   ```bash
   pg "SELECT intent_id, status, attempt, resumable FROM chat_requests ORDER BY updated_at DESC LIMIT 3"
   pg "SELECT role, length(content), meta->>'error' FROM messages ORDER BY id DESC LIMIT 2"
   ```

   Expect `interrupted` + `resumable=t`. Expect **no** assistant row with
   `length(content)=0` and an empty `meta` — that is RC-2, the blank answer.

4. **The analysis was requeued, not restarted from zero.**

   ```bash
   pg "SELECT id, status, stage, attempt FROM video_analyses ORDER BY id DESC LIMIT 1"
   docker logs sf-local-ai-orchestrator-1 2>&1 | grep 'requeued'
   ```

   `attempt` increments by exactly 1. `stage` should resume at or after where
   it stopped — the finished stages' files are on disk:

   ```bash
   docker exec sf-local-ai-orchestrator-1 ls -la /data/video/<sha256>/
   ```

   `probe.json`, `audio.wav`, `transcript.json` for stages already done.

5. **A lease with a living owner is not stolen.** `lease_owner` changes to the
   new process and `video_lease_steal_total` increments by at most 1:

   ```bash
   curl -s localhost:8080/metrics | grep video_lease_steal_total
   ```

6. **The answer completes.** Re-attach the way a browser does and confirm the
   turn finishes and is persisted:

   ```bash
   pg "SELECT status FROM chat_requests WHERE intent_id='<the id from step 3>'"
   ```

   `completed`, with an assistant message carrying its `generation_id`.

### Fails if

Exit code 137; no shutdown-hook line; a `chat_requests` row still `running`
with no live process; an assistant message with empty content; `attempt`
jumping by more than 1 (two processes ran the same analysis).

---

## Drill 2 — frontend recreated mid-upload

**Claim.** An in-flight 64 MiB part is allowed to finish (5 m grace), and if
it is not, exactly one part is lost and the browser re-sends only that part.

### Run

Terminal A — a chunked upload of a 400 MB file, through the frontend's own
route handlers, which is the path a browser takes:

```bash
scripts/video_smoke.py --video /path/to/400mb.mp4 --ask "what is this" \
    --via-frontend http://127.0.0.1:3000
```

Terminal B — as soon as a session exists and parts are landing:

```bash
until [ "$(pg "SELECT count(*) FROM upload_sessions WHERE status='uploading'")" -gt 0 ]; do sleep 1; done
UP=$(pg "SELECT id FROM upload_sessions WHERE status='uploading' ORDER BY created_at DESC LIMIT 1")
pg "SELECT id, status, bytes_received, jsonb_object_keys(accepted_parts) FROM upload_sessions WHERE id='$UP'"

date -u +%T
eval "$PREFIX" up -d --no-deps --force-recreate frontend
date -u +%T
```

### Assert

1. **The frontend was given its grace.** The time between the two `date`
   calls is how long Docker waited. With nothing else in flight it should be
   under a couple of seconds — Next closes idle keep-alive sockets and exits.
   With a part still streaming it may be up to 5 minutes, and:

   ```bash
   docker inspect sf-local-ai-frontend-1 --format '{{.State.ExitCode}}'
   ```

   `0` or `143` (SIGTERM handled). **`137` means SIGKILL** — INF-1 has
   regressed, or the part genuinely exceeded 300 s and Node dropped it first.

2. **No half-part was accepted.** The parts on disk are whole files named by
   bare index; a part cut mid-stream leaves only a `.tmp`, which nothing
   counts:

   ```bash
   CONV=$(pg "SELECT conversation_id FROM upload_sessions WHERE id='$UP'")
   docker exec sf-local-ai-orchestrator-1 ls -la /data/workspaces/uploads/$CONV/$UP/_parts/
   ```

   Every accepted index is a plain number; any `<index>.<nonce>.tmp` is
   in-flight rubbish and must NOT appear in `accepted_parts`.

3. **The row and the disk agree.**

   ```bash
   pg "SELECT bytes_received, accepted_parts FROM upload_sessions WHERE id='$UP'"
   ```

   Each entry's `bytes` equals the size of the file at that index. The resume
   endpoint reports the same set:

   ```bash
   curl -s -H "Cookie: ts_session=<...>" \
     localhost:8080/uploads/chunked/$CONV/$UP | python3 -m json.tool
   ```

4. **Resume sends only what is missing.** Re-run the same upload; the client
   asks the resume endpoint first and PUTs only the absent indexes. Watch the
   byte counter:

   ```bash
   curl -s localhost:8080/metrics | grep upload_part_bytes_total
   ```

   It should grow by roughly one part, not by the whole file.

5. **Nothing else's parts were touched.** If a second conversation had an
   upload in flight, its directory is intact — this is the F-01 assertion:

   ```bash
   docker exec sf-local-ai-orchestrator-1 ls /data/workspaces/uploads/
   ```

### Fails if

Exit code 137 with a part under 300 s old; an accepted part whose on-disk size
disagrees with the row; `accepted_parts` losing indexes that were already
acknowledged; another conversation's `uploads/` directory disappearing.

---

## Drill 3 — host reboot

**Claim.** Everything durable comes back and everything in memory is marked as
lost rather than silently forgotten. This is the drill that reproduces
2026-09-10, when both nodes rebooted at 14:03 UTC.

**The heaviest drill: the main model reloads, so the site answers nothing for
roughly 10–15 minutes.** Announce it. Do not run it to "check something
quickly".

### Run

Set up three things in flight, then reboot:

```bash
# 1. an analysis running
scripts/video_smoke.py --video /path/to/long.mp4 --ask "summarise" &
until [ "$(pg "SELECT count(*) FROM video_analyses WHERE status='running'")" -gt 0 ]; do sleep 2; done

# 2. a chunked upload part-way through (leave it interrupted on purpose)
#    start scripts/video_smoke.py --via-frontend with a 400 MB file and Ctrl-C
#    it after two or three parts have landed.

# 3. record everything BEFORE the reboot
work | tee /tmp/before-reboot.json
pg "SELECT id, status, stage, attempt, lease_owner, lease_expires_at FROM video_analyses WHERE status IN ('queued','running')"
pg "SELECT intent_id, status, attempt FROM chat_requests WHERE status IN ('accepted','running')"
pg "SELECT id, status, bytes_received FROM upload_sessions WHERE status IN ('uploading','finalizing')"

sudo reboot
```

### Assert, after the stack is back

Give it the full model load; `./techsara up` is not needed if `restart:
unless-stopped` did its job.

1. **Every container came back and Postgres came back FIRST enough.**

   ```bash
   docker ps --format '{{.Names}}\t{{.Status}}' | sort
   docker logs sf-local-ai-orchestrator-1 2>&1 | head -40
   ```

   Docker ignores `depends_on` at boot, so the orchestrator can start before
   its database; `wait_for_database` in the lifespan is what turns that into
   one clean start instead of a crash loop. Expect no restart loop:

   ```bash
   docker inspect sf-local-ai-orchestrator-1 --format '{{.RestartCount}}'
   ```

2. **Chat requests were marked interrupted at STARTUP** (there was no orderly
   shutdown to do it):

   ```bash
   docker logs sf-local-ai-orchestrator-1 2>&1 | grep 'interrupted by a restart'
   pg "SELECT intent_id, status, resumable FROM chat_requests WHERE status='interrupted'"
   ```

   Every request that was `accepted`/`running` before the reboot is now
   `interrupted` and `resumable=t`. None is still `running`.

3. **Analyses were requeued only after their leases lapsed.**

   ```bash
   docker logs sf-local-ai-orchestrator-1 2>&1 | grep 'requeued'
   pg "SELECT id, status, attempt, lease_owner FROM video_analyses ORDER BY id DESC LIMIT 5"
   ```

   The row is `queued` or `running` again with `attempt` +1. A reboot leaves
   the lease unreleased, so this happens once `VIDEO_LEASE_TTL_S` (90 s) has
   passed — not instantly.

4. **The analysis's inputs survived the reboot AND the workspace sweep.**

   ```bash
   docker exec sf-local-ai-orchestrator-1 ls -la /data/video/<sha256>/
   docker exec sf-local-ai-orchestrator-1 stat -c '%h %n' /data/video/<sha256>/source.*
   ```

   The source is there. Its link count is 2 while the workspace copy exists
   and 1 after the sweep has taken it — either is correct; 0 files is not.

5. **The interrupted upload can be resumed, not restarted.**

   ```bash
   pg "SELECT id, status, bytes_received, expires_at FROM upload_sessions WHERE id='<from before>'"
   ```

   Still `uploading`, same `bytes_received`, `expires_at` still in the future.
   Re-select the same file in the browser (name and size must match) and only
   the missing parts move.

6. **A session caught mid-finalise was returned to uploading.**

   ```bash
   docker logs sf-local-ai-orchestrator-1 2>&1 | grep 'stale finalizing'
   ```

7. **The work snapshot agrees with the database.**

   ```bash
   work
   ```

   `chat_requests_interrupted` matches step 2's row count;
   `video_queue_depth{state="queued"}` / `{state="running"}` match step 3.
   A disagreement means the snapshot is stale, not that the rows are wrong —
   it is cached for 5 s and the gauges refresh only when `/health` is served.

### Fails if

A `chat_requests` row still `accepted`/`running` with no process; an analysis
requeued while another process still held a live lease (`attempt` jumping by
2+); an upload session gone or expired early; `/data/video/<hash>/source.*`
missing; the orchestrator in a restart loop.

---

## After any drill

Leave the box in a known state and say so in the channel:

```bash
work
pg "SELECT status, count(*) FROM chat_requests GROUP BY status"
pg "SELECT status, count(*) FROM upload_sessions GROUP BY status"
pg "SELECT status, count(*) FROM video_analyses GROUP BY status"
docker ps --format '{{.Names}}\t{{.Status}}' | sort
```

Rows left `interrupted` are fine — they are resumable by design and a browser
picks them up. Rows left `running` with no process are not: they are what
`ChatRequestsLeftInterrupted` and `VideoAnalysisStalled` exist to catch, and
finding one here means a drill found a real bug.
