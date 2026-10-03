# The dev stack on the worker node

An isolated copy of TechSara's application plane for development, integration
tests and evaluations. It runs on the **worker** node's Docker daemon as the
Compose project `llmdev`, and it borrows production's main model engine
through a proxy that never lets it have more than two requests in flight.

| Service | What | Limits |
|---|---|---|
| `postgres` | a fresh, empty database (volume `llmdev_pgdata`) | 2 GiB, 2 CPUs |
| `orchestrator` | the CPU image (`orchestrator/Dockerfile.cpu`), tagged `llmdev-orchestrator:cpu` | 6 GiB, 4 CPUs |
| `frontend` | the web app, tagged `llmdev-frontend:portable` | 1 GiB, 2 CPUs |
| `inference-cap` | the engine proxy, built from `ops/dev/inference_cap/` | 1 GiB (it may buffer up to 512 MiB of queued bodies), 1 CPU |

Not started: `sync-worker` (it logs in to the production Salesforce org),
`v1-gateway` (public `/v1` traffic), `pgadmin`, `searxng`, and every model
engine.

## Files

| File | Committed | What |
|---|---|---|
| `stack.vars` | yes | names, the env-file paths and the outside-service switches; no hosts, no secrets, no ports (the overlay writes them as literals) |
| `compose.dev.yaml` | yes | the overlay on `compose.yaml` |
| `init-env.sh` | yes | writes the three files below; runs no docker command |
| `devstack.sh` | yes | `up`, `down`, `status`, `logs`, `smoke`, `seed` |
| `.env` | no (gitignored) | synthetic secrets: `POSTGRES_PASSWORD`, `SESSION_SECRET`, `API_KEY_PEPPER`; random per worktree, mode 0600 |
| `.runtime/orchestrator.env` | no | the generated orchestrator settings; engine URLs point only at `http://inference-cap:9100/...` |
| `.runtime/engines.env` | no | the cap's upstreams (`CAP_UPSTREAMS`) and limit (`CAP_MAX_INFLIGHT=2`); the only file with engine addresses |
| `tests/test_dev_stack.py` | yes | renders, script and env-file tests (no daemon needed) |
| `tests/test_inference_cap.py` | yes | the cap's own tests, against fake upstreams on loopback |

## Isolation

- **Names.** Project, images and volumes are `llmdev*`, as literals in the
  overlay as well as through `TECHSARA_STACK=llmdev`, so a shell that exports
  another `TECHSARA_STACK` still cannot reach production's volumes or image
  tags. `devstack.sh` refuses to run when the shell sets `TECHSARA_STACK`, one
  of the three env-file paths to anything else, or `COMPOSE_PROFILES`. The
  autopilot guard renders the project before every mutating Compose command
  and refuses anything that resolves to a `sf-local-ai*` name.
- **Shell variables.** A variable exported in the shell outranks every
  `--env-file` in Compose interpolation, so `devstack.sh` also refuses to run
  when any of `POSTGRES_PASSWORD`, `POSTGRES_USER`, `POSTGRES_DB`,
  `SESSION_SECRET`, `API_KEY_PEPPER`, `ORCHESTRATOR_PORT`, `FRONTEND_PORT`,
  `TECHSARA_BIND_ADDRESS` or `OCR_REMOTE_BASE_URL` is set, even to an empty
  value: they would replace the generated secrets, move a published port or
  point OCR at production's engine. Unset them first (`env -u NAME ...`).
- **Daemon.** `devstack.sh` requires `DOCKER_HOST=ssh://...`, a Docker daemon
  reached through an SSH daemon, and never sets or exports `DOCKER_HOST`
  itself. It has no host data, so it does not know which host is the worker
  and cannot by itself keep the stack off the head node: any `ssh://`
  destination that is this host runs the stack on this host. `ssh://localhost`
  would do exactly that, so the script refuses the names that always mean
  this host (`localhost`, `127.*`, `0.0.0.0`, `[::1]`); this host's own name
  or LAN address still passes. What pins the stack to the worker (operator
  rule: nothing new on the head node) is the operator naming the worker, and
  for the autopilot the installed guard, which accepts `DOCKER_HOST` only
  when it names the worker.
- **Ports.** Loopback of the worker only, written as literals in the overlay:
  orchestrator `127.0.0.1:28080`, frontend `127.0.0.1:23000`. Postgres is not
  published (production's file publishes `127.0.0.1:5432`).
- **Memory.** Every running service has a hard `mem_limit` (swap included),
  `cpus`, `pids_limit` and `oom_score_adj: 1000`, so under memory pressure the
  kernel kills dev containers before anything else; `restart: "no"` keeps a
  crashed one down and visible.
- **Data.** A fresh database and fresh volumes; never a copy of production
  data. Accounts come from `devstack.sh seed`.
- **Outside services.** Salesforce (live reads and credentials), paid search
  keys, speech to text, the voice archive, video analysis, OCR, the web
  knowledge crawler, the engine controller poller and the v1-gateway relay
  are off. Model downloads are off (`HF_HUB_OFFLINE=1`).
- **No bind mounts.** With `DOCKER_HOST=ssh://` a bind source would resolve on
  the worker's filesystem, so the overlay keeps only named volumes (the brain
  packs mount is dropped).
- **No v1relay.** The orchestrator leaves the `v1relay` network, so its pinned
  `10.231.231.0/28` subnet is never created on the worker.

## What is shared with production

Only the main model engine, through `inference-cap`: the orchestrator's engine
URLs are `http://inference-cap:9100/<name>/...` and the cap forwards to the
upstream named `<name>` in `.runtime/engines.env`. It holds the whole dev stack
to **at most two in-flight engine requests**: every request with a body, and
every method other than GET and HEAD, takes one of two slots; bodiless GET and
HEAD (model lists, health probes) do not.

### Limits of the cap

- **Priority is not enforced.** The engines schedule first come, first served;
  they do not know which requests come from dev. The cap only limits the dev
  stack's share to two in-flight requests; a dev request already running
  competes with production's on equal terms.
- **Queue and buffer.** A request that finds both slots busy waits in a queue
  of at most `CAP_MAX_QUEUED` (4) requests, whose bodies are buffered in the
  cap's memory up to `CAP_MAX_BUFFERED_BYTES` (512 MiB) in total. A full
  queue or a full buffer is answered at once with `503` and `Retry-After`, so
  callers must retry later; a single body over `CAP_MAX_BODY_BYTES`
  (128 MiB) is refused outright. The container's `mem_limit` (1 GiB) is sized
  for that buffer plus the two requests in flight.
- **Not in CI yet.** `ops/dev/tests` are not run by CI: adding them changes
  `.github/`, which waits for the operator. Run them by hand (Tests, below).

Heavy and long-context tests (large prompts, long generations, concurrency
runs, evaluations) only in the measured low-traffic window: **05:00-07:00 IST**,
engine-heavy work from **06:00** (`docs/ai-platform-upgrade/ROLLOUT_AND_ROLLBACK.md`,
"Low-traffic window"). The dev orchestrator sees the main engine's full served
window (`max_model_len`), so nothing but the operator stops a 1M-token request.

## Engine modes

| Role | Default | Why |
|---|---|---|
| main | real: the production main engine via the cap | the only engine reachable from the worker |
| router, agent | **shared**: the main model answers them (the launcher's `router_model: shared` semantics) | the router listens only on the head's loopback |
| vision | the main model (as in production) | |
| embed | **disabled** (`EMBED_MODEL=disabled`, `disabled.invalid`) | head loopback only; semantic recall and dense web search are off |
| rerank | **disabled** (`RERANK_BACKEND=disabled`) | head loopback only; answers are not judged by the cross-encoder |
| OCR, ASR, video, search | disabled | production engines and stores; outside services |

If the operator makes the router, embedding or reranker engine reachable from
the worker (for example a tunnel bound to an address the worker can reach),
pass it to `init-env.sh` with `--router`, `--embed` or `--rerank`; it is then
probed, added to the cap's upstreams and used through `/router`, `/embed` or
`/rerank`. Each counts against the same two-request limit.

## Commands

All from the worktree root. `<worker>` is the worker's SSH destination and
`<head-address>:<port>` the main engine's address as the worker reaches it
(both in the host configuration, never in the repository).

```bash
# 1. Env files (no docker; re-run whenever an engine address or model changes).
#    Probes GET /v1/models on every engine given; refuses loopback, https,
#    credentials in the URL, and a dead engine. Secrets are created once and kept.
ops/dev/init-env.sh --main http://<head-address>:<port>
# optional: --router http://<addr>:<port> --embed http://<addr>:<port> --rerank http://<addr>:<port>

# 2. Build on the worker and start; waits up to 900 s for every healthcheck.
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh up

# 3. A super admin for the dev workspace. The password is read from stdin
#    (no echo on a terminal) and reaches the container on stdin, never argv.
#    A bare name logs in as <name>@dev.test.
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh seed alice

# 4. Checks: orchestrator /health (overall + per-check status only),
#    frontend /login HTTP status, the cap's counters.
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh smoke
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh status
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh logs orchestrator

# 5. Stop. Containers and the network go; volumes and images stay.
DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh down
```

To use the web app, forward the worker's loopback ports, for example
`ssh -N -L 23000:127.0.0.1:23000 -L 28080:127.0.0.1:28080 <worker>`, then open
`http://127.0.0.1:23000`.

`/health` reporting `degraded` is expected: its `duckdb` check fails because
the dev stack has no Salesforce warehouse (no sync-worker ever fills it). The
container healthcheck needs only `checks.app_db.status == "ok"`.

Notes for the autopilot guard: it renders the project (`docker compose ...
config`) while it reviews `devstack.sh`, so `init-env.sh` must have run first
or every `devstack.sh` call is refused. It treats every `*.env` file as a
secret, so never `cat` the three env files; `init-env.sh` prints what it wrote
(model ids, window, which roles are real, shared or disabled, and the upstream
addresses, which are not secrets but are never committed).

## Parity gaps against production

- **Image.** The CPU orchestrator image (`Dockerfile.cpu`); production's DGX
  overlay builds and runs its own orchestrator image.
- **Engines.** Router shared with the main model (production runs a separate
  router model), embeddings and reranking off by default, no OCR, ASR or
  video. Classification, routing and agent sub-steps therefore run on the main
  model, with its latency.
- **Data.** Empty database, empty warehouse and vector stores (no
  sync-worker), no brain packs, no web index.
- **Services.** No SearXNG, web knowledge worker, v1-gateway, engine
  controller (the circuit breaker sees only observed failures), monitoring,
  tunnel or pgAdmin.
- **Generated settings.** `init-env.sh` writes the keys that change behaviour:
  engine URLs and model ids for main, router, agent, vision, embed, OCR and
  rerank; `EMBED_VIA`; `OCR_ENABLED`, `RERANK_BACKEND`, `RERANK_ENABLED`,
  `SEARCH_ENABLED`, `SEARCH_PROVIDER`, `SEARXNG_URL`, `ASR_ENABLED`,
  `VIDEO_ANALYSIS_ENABLED`, `VOICE_ARCHIVE_ENABLED`, `SF_LIVE_ENABLED`,
  `WEB_KNOWLEDGE_WORKER_ENABLED`, `ENGINE_CONTROLLER_URL` (empty);
  `MODEL_MAX_CONTEXT`, `DEFAULT_MAX_CONTEXT`, `REPORT_MAX_CONTEXT` (all the
  served `max_model_len`, as the launcher writes them),
  `MAIN_MODEL_NATIVE_CONTEXT` (empty), `MODEL_CONCURRENCY`; and the per-role
  capability keys `{MAIN,ROUTER,AGENT,VISION,EMBED,OCR,RERANKER}_*` derived
  from `config/model-manifest.yaml` as the launcher does when the served model
  is listed there (only the window keys otherwise). Deliberate differences:
  `*_CONCURRENCY` is 2 (the cap) instead of the profile's 4, and a disabled
  role gets `*_CONCURRENCY=1` because the orchestrator refuses 0.
- **Not written** (launcher keys for engines, overlays or the controller that
  the dev orchestrator does not read or cannot know from `/v1/models`):
  `TECHSARA_PROFILE`, `TECHSARA_HARDWARE_PROFILE`, `TECHSARA_RUNTIME_BACKEND`,
  `TECHSARA_MODEL_CACHE`, the `TECHSARA_CLUSTER_*` and `CLUSTER_*` keys,
  `MAIN_STARTUP_ARGUMENTS`, `MAIN_MODEL_ROPE_OVERRIDE`,
  `MAIN_MODEL_KV_BYTES_PER_TOKEN`, `MAIN_MODEL_KV_USABLE_FRACTION`,
  `MAIN_MODEL_ENABLE_PREFIX_CACHING` and the prefix-caching, speculative and
  GDN argument keys, `MAIN_GPU_MEMORY_UTILIZATION`, `VLLM_SHM_SIZE`, the
  published model port keys, the controller's health, exporter and code-sha
  keys, and the `*_MODEL_CONTAINER_PATH` keys. Production's own `.env`
  overrides are not copied either: dev runs the code defaults.
- **Frontend name.** `NEXT_PUBLIC_APP_NAME` is set at run time, so
  server-rendered pages say "(dev)"; anything the build inlined keeps the
  default.

## Cleanup

`down` removes the containers and the `llmdev_application` and
`llmdev_inference` networks. It never removes volumes or images, so the next
`up` finds the same database. What stays on the worker: volumes
`llmdev_data`, `llmdev_reports`, `llmdev_pgdata`, `llmdev_hf-cache` and images
`llmdev-orchestrator:cpu`, `llmdev-frontend:portable`,
`llmdev-inference-cap:dev`. Removing the volumes (a fresh database) is an
operator action; the autopilot never removes volumes. Deleting `ops/dev/.env`
while `llmdev_pgdata` exists orphans the database: its password lives in the
volume.

## Tests

```bash
python3 -m pytest ops/dev/tests/test_dev_stack.py ops/dev/tests/test_inference_cap.py -q -p no:cacheprovider
```

`tests/test_dev_stack.py` renders the dev and the production chains with
`docker compose ... config` (no daemon; skipped without Docker Compose), runs
`init-env.sh` against a fake `/v1/models` server on loopback, and runs
`devstack.sh` against a stub `docker`. `tests/test_inference_cap.py` runs the
cap against fake upstream engines on loopback (in process, except the exit
code and SIGTERM drain tests). Neither is run by CI yet
(see "Limits of the cap").
