# v1-gateway

A dependency-free Node 20 relay for the public developer API, `/v1` only.
It holds the developer's connection while the orchestrator and the frontend
are recreated by routine deploys, so a default openai-python or openai-node
call survives them.

```
developer → Cloudflare → cloudflared → v1-gateway :8090 → orchestrator :8080 → engines
                                   └─ everything else → frontend :3000
```

cloudflared sends `^/v1(/|$)` here once the operator adds that path rule.
Until then, and for LAN callers, the Next `/v1` route keeps working.

## What it does

| Step | Behaviour |
|---|---|
| Body | A declared over-cap body gets `413` before a byte is read; a `GET`, `HEAD` or `OPTIONS` that declares any body gets `413` too. JSON bodies are buffered: up to 1 MiB in memory, then spilled to a 0600 file in the 0700 spool directory, because a retry must send the same bytes. Every spilled byte is reserved first against a total spool quota and a free-space floor; a refusal is `503` with `Retry-After: 30` and the connection closed. Whether a generation asks for a stream is read from the bytes as they arrive; the body is never parsed or kept. Audio, `POST /v1/files` and upload parts stream through with backpressure. |
| Pre-commit | Nothing has reached the client yet. A failure before the request was fully written is retried with the same attempt id every 2 s. After 110 s the client gets `503 model_unavailable` with `Retry-After: 30`. A failure after the orchestrator may hold the whole request is retried only where a second send cannot do the work twice: `GET`/`HEAD`/`OPTIONS`, embeddings and rerank, an upload part `PUT`, upload `complete` and `cancel`, and the generation and audio routes once the orchestrator has answered with `X-TechSara-Run`. Anything else gets `503` with `x-should-retry: false`. Those requests always use a new orchestrator connection; a pooled connection that turns out to be closed is retried at once on a new one. |
| Silent orchestrator | For routes whose response shape the request decides (`/v1/responses`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `GET /v1/responses/{id}`), the gateway commits after 15 s of silence. A stream gets `200` and `: ping`. JSON gets `200` and one space. A keepalive byte follows every 15 s. |
| Relay | Headers pass through the same allowlists as `frontend/app/v1/[[...path]]/route.ts`. SSE is relayed frame by frame and internal `: ts-seq=N` comments are removed. 300 s with no byte from the orchestrator counts as a failure. |
| Re-attach | After commit, a failed upstream is re-POSTed with `X-TechSara-Attempt` and `X-TechSara-Resume-After: N`. The client keeps getting heartbeats. Backoff runs 1 → 10 s, for up to 1,800 s of continuous orchestrator absence; then the client connection is cut. A partly relayed JSON object, a run the orchestrator calls `none`, or a `404` cuts it at once. So does an answer that does not name the same run (`reason: run_mismatch`), such as a rolled-back build that launches afresh, which is never spliced into the stream. |
| SIGTERM | The listener closes and idle keep-alive sockets close. Relays still running 2 s later are destroyed, so clients see an incomplete read and resume. The process then exits. |
| fd guard | Above 70% of `RLIMIT_NOFILE`, new `/v1` requests get `503` with `Retry-After: 30` before any header is written. `/healthz` keeps answering. |

The gateway is not an authentication boundary. It adds no credential. It
strips the cookie, every client-sent `x-techsara-*` header and every
client-described forwarding header. The orchestrator resolves Bearer keys,
and honours the attach headers only from `PUBLIC_API_TRUSTED_PROXIES` peers.

## Settings

Read once at start. Blank, non-numeric, zero or negative means the default.

| Variable | Default | Meaning |
|---|---|---|
| `V1_GATEWAY_PORT` / `V1_GATEWAY_HOST` | 8090 / 0.0.0.0 | listener |
| `ORCHESTRATOR_URL` | `http://localhost:8080` | upstream, `http://` only (same variable as `lib/proxy.ts`) |
| `V1_GATEWAY_HEARTBEAT_S` | 15 | byte invariant: self-commit delay and keepalive interval |
| `V1_GATEWAY_UPSTREAM_SILENCE_S` | 300 | upstream silence treated as a failure |
| `PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S` | 110 | pre-commit retry budget |
| `V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S` | 2 | pre-commit retry interval |
| `PUBLIC_API_GATEWAY_REATTACH_MAX_S` | 1800 | re-attach budget (continuous absence) |
| `V1_GATEWAY_REATTACH_BACKOFF_MIN_S` / `_MAX_S` | 1 / 10 | re-attach backoff |
| `V1_GATEWAY_SEQ_HOLD_S` | 1 | how long an SSE event waits for its `ts-seq` before it is released unconfirmed |
| `V1_GATEWAY_ATTACH` | `auto` | `auto`: re-POST a silent generation only once the orchestrator has answered with `X-TechSara-Run`; `on`; `off` |
| `V1_GATEWAY_BODY_IDLE_S` | 60 | destroy a socket whose expected body sends nothing for this long |
| `V1_GATEWAY_DRAIN_ABORT_S` | 2 | SIGTERM grace before relays are destroyed |
| `V1_GATEWAY_FD_PRESSURE_RATIO` | 0.7 | fd guard threshold |
| `V1_GATEWAY_SPOOL_DIR` | `/spool` | spilled bodies; stale `body-*` files are removed at start |
| `V1_GATEWAY_MEMORY_BODY_BYTES` | 1048576 | in-memory part of a buffered body |
| `V1_GATEWAY_SPOOL_MAX_BYTES` | 2147483648 | total bytes all relays may hold in the spool |
| `PUBLIC_API_MIN_FREE_DISK_BYTES` | 21474836480 | free-space floor for spilling; the orchestrator's own variable and default, and `0` turns it off as it does there |
| `TRUSTED_CLIENT_IP_HEADER`, `TRUSTED_FORWARDED_PROTO` | blank | as `lib/proxy.ts` |
| `PUBLIC_API_MAX_BODY_BYTES`, `_MEDIA_`, `_AUDIO_`, `PUBLIC_API_FILES_MAX_BODY_BYTES`, `PUBLIC_API_FILES_PART_MAX_BYTES` | 1 MiB, 20 MiB, 26 MiB, 68,157,440, 67,108,864 | per-path body caps, read per request from the orchestrator's own variables |

Fixed: `requestTimeout 0`, `headersTimeout 100000`, `keepAliveTimeout 95000`
(above cloudflared's 90 s).

## Logs

One JSON line per event on stdout. Relay lines carry the attempt id, the
method and the route, never a header value or a body. Watch for:
- `self_commit`
- `upstream_failed`
- `reattach_start`
- `reattach_ok` (with `tries`, `absent_ms`, `reattach_total`)
- `reattach_abort` (with `outcome`, and `reason: run_mismatch` when the answer named another run)
- `precommit_retrying`
- `precommit_exhausted`
- `precommit_outcome_unknown` (the orchestrator may have the request; answered `503`, `x-should-retry: false`)
- `precommit_stale_socket`
- `spool_refused` (with `reason`: `quota` or `disk`)
- `body_idle`
- `fd_pressure_refusal`
- `drain_start`, `drain_aborted`, `drain_exit`

## Tests

`node --test`, Node 20 built-ins only. The helpers live in `testkit/`, outside
`test/`, because `node --test` runs every file inside a `test` directory. The
stub orchestrator speaks the attach protocol and keeps run state on disk, so a
test can kill it and relaunch it mid-stream.

```
cd gateway
node --test                                        # test/*.test.cjs, about 4 minutes; real-time tests use the default timers
GATEWAY_SDK_PYTHON=/path/to/python \
GATEWAY_SDK_NODE_DIRS=/dir/with/openai6,/dir/with/openai7 \
GATEWAY_LONG_TESTS=1 node --test test/sdk.test.cjs # real SDKs, including the 400 s silent-orchestrator run
```

- `parity.test.cjs` reads `route.ts` and `next.config.mjs` and fails when the
  two edges disagree.
- `PENDING_*` in `lib/headers.cjs` lists the names the Files and no-timeout
  designs add to `route.ts` in the same wave. When `route.ts` gains one, the
  test prints it so it can move to the base list.
