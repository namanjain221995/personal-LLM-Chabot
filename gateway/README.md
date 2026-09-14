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
| Body | A declared over-cap body gets `413` before a byte is read; a `GET`, `HEAD` or `OPTIONS` that declares any body gets `413` too. JSON bodies are buffered: up to 1 MiB in memory, then spilled to a 0600 file in the 0700 spool directory, because a retry must send the same bytes. Every byte kept in memory is reserved first against one total for all relays, and a body that cannot get memory spills at once. Every spilled byte is reserved first against a total spool quota and a free-space floor; a refusal is `503` with `Retry-After: 30` and the connection closed. Whether a generation asks for a stream is read from the bytes as they arrive; the body is never parsed or kept. Audio, `POST /v1/files` and upload parts stream through with backpressure. |
| Pre-commit | Nothing has reached the client yet. A failure before the request was fully written is retried with the same attempt id every 2 s. After 110 s the client gets `503 model_unavailable` with `Retry-After: 30`. A failure after the orchestrator may hold the whole request is retried only where a second send cannot do the work twice: `GET`/`HEAD`/`OPTIONS`, embeddings and rerank, an upload part `PUT`, upload `complete` and `cancel`, and the generation and audio routes once the orchestrator has answered with `X-TechSara-Run`. Anything else gets `503` with `x-should-retry: false`. Those requests always use a new orchestrator connection; a pooled connection that turns out to be closed is retried at once on a new one. |
| Silent orchestrator | For routes whose response shape the request decides (`/v1/responses`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `GET /v1/responses/{id}`), the gateway commits after 15 s of silence. A stream gets `200` and `: ping`. JSON gets `200` and one space. A keepalive byte follows every 15 s. |
| Relay | Headers pass through the same allowlists as `frontend/app/v1/[[...path]]/route.ts`. SSE is relayed frame by frame and internal `: ts-seq=N` comments are removed. 300 s with no byte from the orchestrator counts as a failure. |
| Re-attach | After commit, a failed upstream is re-POSTed with `X-TechSara-Attempt` and `X-TechSara-Resume-After: N`. The client keeps getting heartbeats. Backoff runs 1 → 10 s, for up to 1,800 s of continuous orchestrator absence; then the client connection is cut. A partly relayed JSON object, a run the orchestrator calls `none`, or a `404` cuts it at once. So does an answer that does not name the same run (`reason: run_mismatch`), such as a rolled-back build that launches afresh, which is never spliced into the stream. |
| SIGTERM | The listener closes and idle keep-alive sockets close. Relays still running 2 s later are destroyed, so clients see an incomplete read and resume. The process then exits. |
| fd guard | Above 70% of `RLIMIT_NOFILE`, new `/v1` requests get `503` with `Retry-After: 30` before any header is written. `/healthz` keeps answering. |

The gateway is not an authentication boundary. It adds no credential. It
strips the cookie, every client-sent `x-techsara-*` header and every
client-described forwarding header. The orchestrator resolves Bearer keys,
and honours the attach headers only from `PUBLIC_API_GATEWAY_PEERS` peers.

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
| `V1_GATEWAY_MEMORY_BUDGET_BYTES` | 268435456 | total body bytes all relays may hold in memory; past it bodies spill to the spool |
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

## Operating it

Added 2026-09-13 with the deployment wiring (`compose.yaml` service
`v1-gateway`, `launcher/techsara_cli/cli.py`, `scripts/deploy.sh`).

### What a deploy does to it

- **The pin.** The image is `sf-local-ai-v1-gateway:<V1_GATEWAY_CODE_SHA>`, a
  sha256 over this directory's image inputs only: `Dockerfile`, `server.cjs`
  and every file under `lib/`. Editing this README, `test/` or `testkit/`
  does not change it. `./techsara up` computes it, builds the image only when
  that tag is missing, and runs `up -d --no-deps v1-gateway`, never
  `--force-recreate`. Compose therefore recreates the container only when the
  digest or one of the gateway's own settings changed.
- **Deploys never wait.** `scripts/deploy.sh` says what a deploy cuts and
  goes ahead. It logs one of:
  - `v1-gateway unchanged (code sha …)`: it keeps every connection.
  - `v1-gateway: will be recreated - … cutting N relay(s) now`: the
    recreated gateway cuts relays still open 2 s after SIGTERM, and those
    clients resume.
- **`--full`.** `techsara down` removes the gateway with everything else, so a
  full deploy always counts as a recreate.
- **Public work in flight.** When the running orchestrator suspends public
  runs (the durable runtime, with `PUBLIC_API_RESUME_ENABLED` on), the deploy
  reports the count, and separately the runs that are not resumable
  (`store:false`) and will be cut. When it cannot (a build without the
  durable runtime, which means the deploy that ships it, or resume switched
  off), it reports that the runs in flight will end failed.
- **Opting in to a wait (hand-run deploys only).** `DEPLOY_GATEWAY_DRAIN_DEADLINE`
  and `DEPLOY_PUBLIC_WORK_DEADLINE` (seconds, default `0`) make a deploy wait,
  bounded, for `/healthz` `relays` or for those runs to reach 0. They are off
  by default because the wait holds the deploy lock with the new commit
  already checked out, and nothing stops new work arriving meanwhile, so
  under steady traffic it only ever ends at its bound. Use them at a quiet
  moment, for example for the first deploy of the durable runtime after the
  in-flight check. Nothing waits during an automatic rollback.
- **Health gate.** After `up`, a deploy fails (and rolls back) if the
  gateway is not running its pinned image, or if the deploy created or
  recreated it and it is not healthy. A gateway the deploy left untouched
  that is unhealthy is a `WARNING` in the deploy log, not a failure: failing
  would roll back an unrelated change, and the rollback, with the same
  untouched container, would fail the same way. Restarting it is your
  decision, because a restart cuts every connection it holds:
  `docker logs --tail 200 sf-local-ai-v1-gateway-1`, then
  `docker restart sf-local-ai-v1-gateway-1`. Whether a request relayed through
  it reaches the orchestrator is reported as a warning only.
- **Never in an env file.** Do not put `V1_GATEWAY_CODE_SHA` in `.env`,
  `.runtime/secrets.env` or `generated.env`; the launcher refuses to start
  if you do. Compose folds those files into the orchestrator's and the
  frontend's definitions, so the key would recreate them whenever the
  gateway changed.
- **Hand-run Compose.** Before any `docker compose` with the launcher's chain,
  run:

  ```
  export V1_GATEWAY_CODE_SHA="$(scripts/deploy.sh --print-v1-gateway-sha)"
  ```

  Without it the command renders the gateway as `:unpinned`, and
  `deploy-preflight.sh plan` reports a recreate that is not coming.

### Settings the operator owns

These live in `.env`. A change there changes the orchestrator's definition,
so it takes effect at the next deploy.

| Variable | Why |
|---|---|
| `PUBLIC_API_GATEWAY_PEERS` | Must be exactly `10.231.231.2`, the gateway's pinned address on the internal `v1relay` network. Without it the orchestrator ignores the attach headers, the gateway never sees `X-TechSara-Run`, and re-attach stays off: requests still work, but they do not survive an orchestrator restart. Exact addresses only: an entry wider than /32 is ignored with a warning. |
| `PUBLIC_API_TRUSTED_PROXIES` | Must also include `10.231.231.2`, so the orchestrator believes the `X-Forwarded-For` the gateway writes. This list alone no longer lets a peer attach. |
| `TRUSTED_CLIENT_IP_HEADER` | Optional. Set to `cf-connecting-ip` only if a project's `ip_allowlist` must see the caller's address through the tunnel. |
| `V1_GATEWAY_SPOOL_MAX_BYTES`, `PUBLIC_API_MIN_FREE_DISK_BYTES` | Spool quota and free-space floor. Blank uses the defaults above. |
| `V1_GATEWAY_MEMORY_BUDGET_BYTES` | Body bytes held in memory across all relays. Blank uses 256 MiB. |

`v1relay` is `10.231.231.0/28`, outside Docker's default address pools
(`172.17-31.0.0/16`, `192.168.0.0/16`) and this host's RoCE, LAN and
Tailscale ranges. `./techsara up` checks it against every Docker network and
host route before the first start that creates the network, and stops with
what overlaps. To look for yourself before the first deploy (read-only;
it prints `free` or each overlap):

```
python3 - <<'PY'
import ipaddress, subprocess
want = ipaddress.ip_network("10.231.231.0/28")
names = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True).stdout.split()
lines = subprocess.run(["docker", "network", "inspect", *names, "--format", "{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}"], capture_output=True, text=True).stdout.splitlines()
lines += ["route " + l.split()[0] for l in subprocess.run(["ip", "-4", "route", "show", "table", "all"], capture_output=True, text=True).stdout.splitlines() if l.split() and l.split()[0][0].isdigit()]
hits = [l for l in lines for item in l.split()[1:] if ":" not in item and ipaddress.ip_network(item, strict=False).overlaps(want) and ipaddress.ip_network(item, strict=False).prefixlen >= 8]
print("\n".join(hits) or "free")
PY
```

### Checks after the deploy that creates it (read-only)

```
docker inspect sf-local-ai-v1-gateway-1 --format '{{.Config.Image}} {{.State.Health.Status}}'
docker exec sf-local-ai-frontend-1 wget -qO- http://v1-gateway:8090/healthz
docker exec sf-local-ai-v1-gateway-1 timeout 20 wget -S -q -O /dev/null http://127.0.0.1:8090/v1/models 2>&1 | grep HTTP/
docker exec sf-local-ai-orchestrator-1 printenv PUBLIC_API_GATEWAY_PEERS PUBLIC_API_TRUSTED_PROXIES
```

Expected:
1. The pinned image and `healthy`.
2. `{"status":"ok","relays":0}`.
3. `HTTP/1.1 401` within a second: the orchestrator answered through the
   gateway. A `503` after about 110 s means the gateway cannot reach
   `orchestrator-v1relay`. Do not add the tunnel route until this check
   passes.
4. A list that contains `10.231.231.2`.

### Route `/v1` to it in the Cloudflare tunnel

The tunnel is remotely managed (`TUNNEL_TOKEN`), so its ingress is edited in
the Cloudflare dashboard, not in any file here. cloudflared picks the change
up live; no container restarts.

**1. Read-only first.** Record the current ingress, in the order cloudflared
evaluates it:

```
docker logs sf-local-ai-cloudflared-1 2>&1 | grep 'Updated to new configuration' | tail -1
```

Expected, as on 2026-09-13: one rule for `ai.techsarasolutions.com` to
`http://frontend:3000`, then the catch-all `http_status:404`. In the dashboard (Zero Trust →
Networks → Tunnels → this tunnel → Configure → Public Hostname), note:
- the plan tier;
- that the existing hostname has no path;
- that its Additional application settings hold no origin-request
  override (timeouts, chunked encoding, HTTP/2 origin).

**2. Add the route.** On the Public Hostname tab, Add a public hostname:

| Field | Value |
|---|---|
| Subdomain | `ai` |
| Domain | `techsarasolutions.com` |
| Path | `^/v1(/\|$)` |
| Service type | `HTTP` |
| URL | `v1-gateway:8090` |

Leave Additional application settings at their defaults and save. Rules are
matched top to bottom, so this row must sit **above** the existing
`ai.techsarasolutions.com` row, which has no path and would otherwise match
first. If it was added below, move it up.

**3. Verify the order in cloudflared's own log.** This is authoritative,
whatever the dashboard shows:

```
docker logs --since 5m sf-local-ai-cloudflared-1 2>&1 | grep 'Updated to new configuration' | tail -1
```

The rule for `http://v1-gateway:8090`, with path `^/v1(/|$)`, must come before
the `http://frontend:3000` rule. The log escapes the quotes in its JSON.

**4. Verify the traffic.** From a machine outside the LAN:

```
curl -sS -o /dev/null -w '%{http_code}\n' https://ai.techsarasolutions.com/v1/models
```

Expected `401`. Then, on the host:

```
docker logs --since 2m sf-local-ai-orchestrator-1 2>&1 | grep -c '10\.231\.231\.2:[0-9]* - "GET /v1/models'
```

A count of 1 or more means the request came through the gateway. `/`, `/docs`
and `/api` must still be served by the frontend.

**Rollback.** Delete that public hostname row. cloudflared logs a new
configuration without it, and `/v1` is served by the frontend's route again.
Also delete it before rolling back to a commit that has no `gateway/`:
`deploy.sh` warns when it sees that case.

### Watching it

- `docker logs sf-local-ai-v1-gateway-1`: `reattach_ok` and `reattach_abort`
  per orchestrator restart, `precommit_exhausted` when the orchestrator was
  away for more than 110 s, `fd_pressure_refusal` and `spool_refused` under
  load (with the memory budget spent, bodies spill, so `spool_refused` is
  also what a flood of bodies ends in).
- `/healthz` `relays` is the number of in-flight relays. `deploy.sh` reports
  it before a recreate.

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
