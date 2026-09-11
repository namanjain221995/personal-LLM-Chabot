# Operations — limits, drains, restarts

What an operator needs that the contract does not cover: the real size and
timeout chains with the values this box is actually running, what a deploy
does to work in flight, and what a reboot leaves behind.

Every value below was read from the deployed `.env`, the compose chain and the
source on 2026-09-11. Nothing here changes configuration; the recommendations
at the end are recommendations.

---

## 1. The size chain, end to end

A byte on its way from a person's disk to `/data` passes six ceilings. They
are listed in the order it meets them, with the DEPLOYED value.

| # | Where | Limit | Set by |
|---|---|---|---|
| 1 | Browser: single-shot vs chunked | **90 MiB** (94,371,840 B) | `CHUNK_THRESHOLD_BYTES`, `frontend/lib/uploadDocument.ts` |
| 2 | Browser: one part | **64 MiB** (67,108,864 B) | `CHUNK_PART_BYTES`, same file |
| 3 | Cloudflare edge (`ai.techsarasolutions.com` only) | **100 MB** per request body | the plan; not configurable here |
| 4 | Next route handlers (`/api/upload`, `/api/upload/chunked/*`) | **no body limit**; streamed | App Router route handlers stream `req.body`; `duplex: 'half'` keeps a part out of proxy memory |
| 5 | Orchestrator, per part | **90 MiB** (`_PART_CAP`), max **128** parts (`_MAX_PARTS`) | `orchestrator/app/uploads.py` |
| 6 | Orchestrator, whole file | video **4,096 MB** (`VIDEO_MAX_UPLOAD_MB`, default — not set in `.env`); everything else **102,400 MB** (`UPLOAD_MAX_MB`) | `.env:348`, `app/config.py` |
| 7 | Workspace | **20 GB** (`WORKSPACE_QUOTA_GB`), **24 h** (`WORKSPACE_TTL_HOURS`) | `.env:183-184` |
| 8 | The volume | `/data` is on `/dev/nvme0n1p2`: **3.7 TB, 2.7 TB free** (23 % used); the `sf-local-ai_data` volume holds 133 GB, of which `/data/workspaces` is 2.6 GB and `/data/video` 884 MB | the host |

### What actually binds

* **The edge, not the app.** 64 MiB (67.1 MB) against a 100 MB wall leaves
  33 % of headroom, which is why chunking works publicly. A single-shot
  upload at the threshold is 94.4 MB **plus multipart framing** against the
  same 100 MB wall — about 5 % of margin. That is the tightest number in the
  whole chain and it is the one nobody controls.
* **Parts, not `UPLOAD_MAX_MB`.** 128 parts × 64 MiB = **8 GiB** is the real
  ceiling for a document or a dataset. `UPLOAD_MAX_MB=102400` (100 GiB) is
  above it by a factor of twelve, so the "refused before a byte moves" 413 at
  `POST /uploads/chunked/init` can never fire for a document — the refusal
  comes later, from the part count.
* **Video is the one honest cap.** 4 GiB is enforced at init (declared
  `size`), at every part (accepted + streaming) and at `complete`, so a video
  can never be refused late for a total it was told was fine early.
* **A big upload costs its size twice.** The parts stay on disk while
  `complete` concatenates them into `_original/`, so a 4 GiB video peaks at
  ~8 GiB of workspace — 40 % of `WORKSPACE_QUOTA_GB` for ONE upload
  (F-04). Two at once put the sweep into eviction on a disk that is 23 %
  full.

### Where the bytes live

```
<WORKSPACE_DIR=/data/workspaces>/uploads/<conversation_id>/<upload_id>/
    _parts/<index>                 an accepted part
    _parts/<index>.<nonce>.tmp     a part still streaming
    _original/<filename>           the assembled file
<VIDEO_DATA_DIR=/data/video>/<sha256>/source.<ext>   HARD LINK to the above
```

The hard link is why an analysis outlives its upload: the 24 h workspace sweep
unlinks the workspace copy and the inode survives on the `/data/video` link
(`video/store.adopt_source`, proved in
`orchestrator/tests/test_workspace_sweep.py`).

---

## 2. The SSE timeout chain

| Layer | Value | Set by |
|---|---|---|
| Browser `EventSource`/fetch reader | no timeout of its own | — |
| Cloudflare | no idle cap that matters at 15 s heartbeats | — |
| Next proxy → orchestrator (undici `fetch`) | **300 s body idle** (`UND_ERR_BODY_TIMEOUT`) | undici default, unchanged |
| Orchestrator SSE heartbeat | **15 s** (`SSE_HEARTBEAT_SECONDS`, default — not set in `.env`) | `orchestrator/app/sse.py` |
| Generation wall clock | **4,200 s** (`GEN_WALL_CLOCK_S`) | `.env:143` |
| Orchestrator → vLLM | **4,200 s** (`LLM_REQUEST_TIMEOUT`, unset so it follows `GEN_WALL_CLOCK_S`) | `app/config.py` |
| vLLM connect | 10 s (`LLM_CONNECT_TIMEOUT`) | `app/config.py` |

**The invariant.** `SSE_HEARTBEAT_SECONDS` must stay well under the 300 s
undici body timeout, and `LLM_REQUEST_TIMEOUT` must never be shorter than
`GEN_WALL_CLOCK_S` — a shorter transport timeout makes the SDK retry a
generation that is still running, burning the GPU twice and finishing
neither. Both are currently satisfied by defaults rather than by declarations,
which is the risk (see the recommendations).

### New: what a stop does to a stream

`--timeout-graceful-shutdown 90` (added 2026-09-11, in all three orchestrator
Dockerfiles) means uvicorn waits at most 90 s for in-flight responses before
force-closing them and running the lifespan shutdown hook, inside the
container's 120 s `stop_grace_period`. A generation older than 90 s at SIGTERM
is therefore CUT. That is the designed path:

1. uvicorn force-closes the response; the browser's stream ends.
2. The lifespan hook — which now always runs — marks every `accepted`/
   `running` `chat_requests` row **`interrupted`**.
3. The browser asks `GET /chat/requests/{intent_id}`, sees `interrupted` +
   `resumable`, and `GET /chat/attach/{conversation_id}` resumes it under a
   new attempt.

Before the flag, uvicorn waited forever, the 120 s grace expired, SIGKILL
landed, and step 2 never happened — so step 3 had nothing true to read
(INF-3).

---

## 3. Stop grace periods

| Service | Grace | Why |
|---|---|---|
| orchestrator | 2m | 90 s of uvicorn graceful drain + 30 s for the lifespan hook |
| **frontend** | **5m** (was: unset → Docker's 10 s) | Node's own `server.requestTimeout` is 300 s, the hard ceiling on receiving one complete request body — so 5 m is the longest grace that can still save an in-flight 64 MiB part. Covers 64 MiB down to ~219 KB/s of client upstream. |
| sync-worker | 10m | a Salesforce sync cycle |
| postgres | 1m | checkpoint |
| vllm | 2m | engine teardown |

The frontend's 5 m is a **ceiling, not a delay**. Compose recreates the
frontend after the orchestrator (`depends_on: service_healthy`), so by the
time SIGTERM reaches it the upstream SSE responses have already ended, and a
proxy with nothing in flight exits in under a second: Next 16's SIGTERM
handler calls `server.close()`, and Node ≥ 19 closes idle keep-alive sockets
on close. Only a **frontend-only** recreate while a stream is live pays the
full five minutes.

Escape hatch for an urgent frontend-only deploy: stop it with an explicit
shorter timeout first, then bring it up.

```bash
docker compose … stop -t 30 frontend      # cuts at 30 s instead of 300
docker compose … up -d --no-deps frontend
```

The SIGTERM chain was verified rather than assumed (see the comment block in
`frontend/Dockerfile`): compose `init: true` → tini at PID 1 → exec-form CMD →
`node server.js` → Next's own handler. **Do not** rewrite that CMD in shell
form; `/bin/sh` does not forward signals and the whole chain reverts to
SIGKILL-only.

---

## 4. The drain, as it runs

`scripts/deploy.sh` step (3), before `./techsara up`. Every step is bounded
and advisory — none of them can block a deploy.

```
1. deploy-drain.sh check                      instant   audits stop_grace_period
                                                        of every service this
                                                        deploy would recreate
2. deploy-drain.sh wait orchestrator          ≤ 90 s    quiet window on :8080
   deploy-drain.sh wait frontend              ≤ 90 s    quiet window on :3000
   deploy-drain.sh wait sync-worker           ≤ 90 s    (no published port →
                                                        returns immediately)
3. deploy-drain.sh uploads                    ≤ 90 s    waits for upload_sessions
                                                        in status 'finalizing'
                                                        to clear (read-only SQL)
4. ./techsara up                                        SIGTERM, then the grace
                                                        periods in §3
```

Worst case added by the drain: **4 × 90 s = 6 minutes**, before the recreate
itself. `DEPLOY_DRAIN_DEADLINE` and `DEPLOY_FINALIZE_DEADLINE` (both default
90) shorten it.

`scripts/deploy-rollback.sh` now runs the identical sequence — it previously
skipped the audit and the upload drain entirely, on the path taken when a
deploy has already gone wrong.

**What the upload drain guarantees:** at the instant it returned 0, no session
was in `finalizing`. **What it does not:** nothing stops a `complete` from
arriving a millisecond later. There is no admission control, and adding one
would mean refusing uploads during every deploy. It says nothing about
`uploading` sessions on purpose — those are built to survive a restart.

---

## 5. What a restart or a reboot does to each thing in flight

Assumes the committed V29 backend (`de46510`) and the changes above.

| In flight | Orchestrator recreate | Frontend recreate | Host reboot |
|---|---|---|---|
| **Upload mid-part** (part 4 of 7 streaming) | The part is force-closed at 90 s. No accepted part is recorded — the `.tmp` is discarded — so `GET /uploads/chunked/{conv}/{id}` still reports parts 0–3 and the browser re-sends part 4. Parts already on disk survive: they are files, and the sweep now protects an `uploading` session's directory. | The PUT is allowed up to 5 minutes to finish; if it does not, same as the left column. | Same, plus: nothing resumes by itself. The browser must still be open (bytes in memory) or the person re-selects the file — name and size must match the session. Sessions live 24 h (`UPLOAD_SESSION_TTL_HOURS`). |
| **Upload finalizing** | The drain waits up to 90 s for it first. If it is killed anyway, the row sits `finalizing`; the next process's startup hook (`reset_stale_finalizing_upload_sessions`) returns it to `uploading` and the browser's retry of `complete` succeeds — `complete` is idempotent, and one caller wins the row lock. | Not affected (the finalisation runs in the orchestrator). | Same as the left column: recovered at the next start, not before. |
| **Running analysis** | Survives. The row is durable, each finished stage's file is on disk, the lease is released on an orderly stop and requeued at the next start; a resume skips stages whose file exists. Its in-memory progress and its SSE subscribers do NOT survive. The source bytes survive the workspace sweep because they are hard-linked into `/data/video/<hash>/`. | Not affected. | Survives, but the lease is not released — the next start requeues it only after `VIDEO_LEASE_TTL_S` (90 s) has lapsed, which by then it has. |
| **Live generation** | Cut at 90 s. The lifespan hook marks the `chat_requests` row `interrupted`; the answer is resumable under a new attempt and the browser re-attaches. If the answer had already completed, the server persisted it itself before `done`, so a viewer that never came back still finds it in history. | The proxied stream is cut (after up to 5 minutes). The generation itself keeps running in the orchestrator and the browser re-attaches to it. | Cut with no hook: the rows stay `accepted`/`running` until the next START, whose hook marks them `interrupted`. Same recovery, delayed by the boot. |

The one thing none of this recovers: a turn whose `POST /chat` never reached
the server at all. That is `unsent`, and it is the browser's to re-send with
the same `intent_id` (CONTRACT.md, "Message lifecycle").

---

## 6. Observability

`GET /health` now carries a `work` section — additive, never part of `status`:

```json
"work": {
  "status": "ok",
  "live_generations": 1,
  "video": {"queued": 0, "running": 1, "oldest_running_age_s": 42},
  "uploads": {"uploading": 2, "finalizing": 0},
  "chat_requests_interrupted": 0
}
```

Counts only — no user, conversation, intent or file identifiers, because
Prometheus scrapes `/health` unauthenticated from inside the Docker network.
Cached 5 s. `"status": "unknown"` means the database could not be asked, which
is deliberately distinguishable from "nothing in flight".

> **One line is still missing.** `health.check_dependencies()` returns the
> section, but `main.py`'s `/health` handler copies a fixed set of keys into
> its response and does not yet copy `work` — and `main.py` is outside this
> slice's ownership. The exact diff is in the SRE report. **Until it lands,
> read these numbers from `/metrics`, not from `/health`**; the gauges below
> are published either way, because the snapshot is computed whenever
> `/health` is served.

The same snapshot publishes gauges into `/metrics` (scraped every 15 s):
`live_generations`, `video_queue_depth{state}`,
`video_running_stage_age_seconds`, `upload_sessions_open{state}`,
`chat_requests_interrupted`. The alert group `upload-reliability` in
`monitoring/prometheus/rules/alerts.yml` reads exactly those, plus the
existing `upload_session_total{purpose,result}` counter.

Quick look without a browser (the second command works today; the first works
once the `main.py` line lands):

```bash
curl -s localhost:8080/health | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("work", "not exposed yet — read /metrics"), indent=2))'
curl -s localhost:8080/metrics | grep -E '^(live_generations|video_queue_depth|upload_sessions_open|chat_requests_interrupted|video_running_stage_age_seconds)'
```

---

## 7. Recommended `.env` changes — NOT APPLIED

Each is a one-line edit with the reason it is worth making. None of them is
required for the changes above to work.

| Key | Now | Recommend | Why |
|---|---|---|---|
| `WORKSPACE_QUOTA_GB` | `20` | `200` | One 4 GiB video transiently needs ~8 GiB (parts + assembled original), so two concurrent large uploads put the sweep into eviction while `/data` is 23 % full with 2.7 TB free. 200 GB is 7 % of the volume and ~75× the current 2.6 GB workspace. The quota is meant to stop runaway growth, not to fire on normal use. |
| `UPLOAD_MAX_MB` | `102400` | `8192` | It was raised to 100 GiB to get one 400 MB video through when the rail applied a single limit to everything. The rail is purpose-aware now (`VIDEO_MAX_UPLOAD_MB` governs video), so this governs documents and datasets only — where 100 GiB is not a limit at all and disables the "refused before a byte moves" 413. 8,192 MB is the real ceiling anyway (128 parts × 64 MiB), so init and `complete` would finally agree. |
| `VIDEO_MAX_UPLOAD_MB` | unset (`4096`) | `4096`, written down | It is enforced at three points and quoted in user-facing refusals; leaving the only copy of the number in a Python default means a library upgrade or a refactor can move it silently. |
| `UPLOAD_SESSION_TTL_HOURS` | unset (`24`) | `24`, written down | It must equal `WORKSPACE_TTL_HOURS` (24). The sweep protects a directory whose session is open **and unexpired**; if the session TTL were the longer of the two, the workspace would delete parts a live session still expects. Two numbers that must agree should both be visible. |
| `SSE_HEARTBEAT_SECONDS` | unset (`15`) | `15`, written down | The only thing keeping a silent generation alive through the Next proxy's 300 s undici body timeout. An operator raising it to "reduce noise" past 300 s re-creates the "orchestrator is unreachable" incident exactly. |
| `LLM_REQUEST_TIMEOUT` | unset (follows `GEN_WALL_CLOCK_S`) | leave unset | This is the correct state: one number decides what "too long" means. If it is ever set, it must be ≥ `GEN_WALL_CLOCK_S` (4,200) or the SDK retries a generation that is still running. |

New knobs the deploy scripts read (defaults in the scripts, no `.env` entry
needed): `DEPLOY_DRAIN_DEADLINE` (90), `DEPLOY_FINALIZE_DEADLINE` (90),
`DR_MIN_GRACE` (30).
