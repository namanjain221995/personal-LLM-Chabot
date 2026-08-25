# Infrastructure — portable Docker Compose topology

This page documents the launcher-managed infrastructure implemented in
[`compose.yaml`](../../compose.yaml), [`compose/`](../../compose/), and
[`launcher/techsara_cli/compose.py`](../../launcher/techsara_cli/compose.py).
It supersedes the DGX-only topology previously described here.

The root [`docker-compose.yml`](../../docker-compose.yml) remains in the
repository as a legacy DGX-era artifact. The portable launcher does not use it.

## 1. Composition model

The project name is always `sf-local-ai`. The launcher combines the base file
with a hardware-selected overlay:

| Selected family | Files, in order | Inference placement |
|---|---|---|
| Apple Silicon | `compose.yaml`, `compose/compose.mac.yaml` | native vLLM-Metal on host; app/data in Docker |
| DGX Spark | `compose.yaml`, `compose/compose.dgx-spark.yaml`; with `CLUSTER_MODE=dual`, `compose/compose.cluster-dgx-spark.yaml` last | pinned CUDA containers; dual mode shards the main model across two nodes |
| Generic Linux NVIDIA | `compose.yaml`, `compose/compose.nvidia.yaml` | pinned CUDA containers |
| Windows NVIDIA | base, NVIDIA overlay, `compose/compose.windows-wsl2.yaml` | CUDA in WSL2 Docker Linux containers |
| CPU | `compose.yaml`, `compose/compose.cpu.yaml` | llama.cpp container |
| app-only/external | `compose.yaml`, `compose/compose.external-development.yaml` | disabled or explicit local endpoint |

The Mac and Windows modifier overlays add `host.docker.internal` host-gateway
resolution where containers must call a host service. They do not publish a
model port.

When `PUBLISH_MODEL_PORTS=true` the per-family `compose/compose.published-*.yaml`
follows the runtime overlay. On DGX Spark with `CLUSTER_MODE=dual` the launcher
layers `compose/compose.cluster-dgx-spark.yaml` after that, last; Node 2 runs
`compose/compose.cluster-worker.yaml` as a separate project
(`sf-local-ai-worker`, started by `scripts/cluster-worker.sh`, not by the
launcher). See [`../CLUSTER.md`](../CLUSTER.md).

Use `techsara up`, not a bare `docker compose up`. The launcher supplies
generated env files, immutable model paths, selected profiles, optional Compose
profiles, prerequisite checks, staged startup, real API probes, fallback, and
native process ownership.

## 2. Environment sources and precedence

The Compose `env_file` contract is:

1. project `.env`, optional;
2. `${TECHSARA_SECRET_ENV:-.runtime/secrets.env}`, optional;
3. `${TECHSARA_GENERATED_ENV:-.runtime/generated.env}`, required and last.

The frontend consumes generated config only. Orchestrator receives the full
runtime chain. Service-level `environment` entries then map user credentials
and stable storage locations explicitly.

Docker Compose gives exported process variables precedence over `--env-file`.
Before invocation, `ComposeManager._environment()` reapplies `.env` → secrets
→ generated to the child environment. This prevents an old exported
`MAIN_MODEL`, `TECHSARA_MODEL_CACHE`, bind address, or endpoint from defeating
the launcher selection while preserving unrelated values such as `PATH` and
`DOCKER_HOST`.

`.runtime/generated.env` contains no secret material. `.runtime/secrets.env` is
written with mode `0600`; generated env uses `0644`. Known secret values are
redacted from Compose failures and log output.

## 3. Base services

| Service | Image/build | Networks | Host publication | Persistent mounts | Readiness |
|---|---|---|---|---|---|
| `postgres` | digest-pinned PostgreSQL | `application` | `127.0.0.1:${POSTGRES_PORT:-5432}` | `sf-local-ai_pgdata` | `pg_isready` |
| `pgadmin` | digest-pinned pgAdmin; profile `admin` | `application` | `127.0.0.1:${PGADMIN_PORT:-5050}` | `sf-local-ai_pgadmin`, read-only server config | waits for PostgreSQL; process readiness |
| `searxng` | digest-pinned SearXNG; profile `search` | `application` | none | repository config | `/healthz` |
| `orchestrator` | CPU or CUDA Dockerfile selected by overlay | `application`, `inference` | `${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${ORCHESTRATOR_PORT:-8080}` | data, reports, HF cache | `/health` plus launcher health-contract validation |
| `sync-worker` | `sync-worker/Dockerfile` | `application`, `inference` | none | data, read-only sync config | running-state check |
| `frontend` | multi-stage Next.js standalone image | `application` | `${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${FRONTEND_PORT:-3000}` | none | HTTP `/` |

The orchestrator and sync-worker are both connected to `application` and
`inference`. The worker needs inference access for embeddings but can continue
Salesforce warehouse synchronization when embeddings are disabled.

`postgres` and the public application services use the `application` network.
The `inference` network is declared `internal: true`; model sidecars are not
reachable from an external Docker network by default. The documented exception
is `CLUSTER_MODE=dual`, where the `vllm` head leaves the inference network for
host networking (§4.2, §9).

## 4. Runtime overlays

### 4.1 Apple Silicon

`compose.mac.yaml` contains no model container. It adds host-gateway resolution
to orchestrator and sync-worker. The launcher starts native arm64 vLLM-Metal
model upstreams on loopback and bearer-authenticated bridge processes for
container-to-host access. The bridge listeners intentionally bind host
`0.0.0.0` on 18100/18103/18105 so Docker can reach them; they cap request
bodies, strip authorization/hop-by-hop headers before forwarding to loopback,
and do not log requests. Host firewall policy remains relevant.

Docker Model Runner vLLM is unavailable on macOS. Its presence is not accepted
as a Mac inference backend. The native vLLM-Metal path is preferred and
required for the declared Mac profiles.

The pinned runtime is text-first. Mac vision and OCR remain deliberately
disabled pending a stable pinned image/OCR probe contract. Selected embedding
and reranking components are optional and disabled at runtime if their real
probe fails.

### 4.2 DGX Spark

`compose.dgx-spark.yaml` preserves the measured multi-service topology:

| Service | Internal port | Model/role | Host port |
|---|---:|---|---|
| `vllm` | 30000 | Qwen3.8 27B NVFP4 main (`RadixArk/Qwen3.8-27B-NVFP4`, served as `Qwen/Qwen3.8-27B-NVFP4`) | none (`expose` only) |
| `vllm-router` | 30002 | Qwen3-VL 8B FP8 router/vision | none |
| `vllm-embed` | 30003 | Qwen3 Embedding 0.6B; profile `embeddings` | none |
| `vllm-reranker` | 30005 | Qwen3 Reranker 0.6B (`/score`); profile `reranker` | none |
| `vllm-ocr` | 30004 | Unlimited-OCR; profile `ocr` | none |
| `vllm` with `CLUSTER_MODE=dual` | — (host networking) | node-rank 0 of a two-node TP=2 engine; Node 2 runs `vllm-worker` (node-rank 1, `--headless`) | host `CLUSTER_API_BIND_ADDRESS:${VLLM_PORT}` (8000): `0.0.0.0` when `PUBLISH_MODEL_PORTS=true`, else the Docker bridge gateway |

In dual mode only the `vllm` row changes: the head keeps the same image digest,
model path, and served model id, gains `/dev/infiniband`, unlimited memlock,
`IPC_LOCK`, and the NCCL environment, and the orchestrator and sync-worker
resolve `vllm` to the host via `extra_hosts`. Everything else stays on Node 1
as above. Details, measurements, and operations: [`../CLUSTER.md`](../CLUSTER.md).

All model paths are generated container paths beneath `/models`. The selected
host cache is mounted read-only. Images are digest-pinned from the model
manifest; models run with `HF_HUB_OFFLINE=1` because acquisition is a launcher
responsibility.

The CUDA orchestrator image includes the in-process reranker and requests all
NVIDIA devices through a Compose device reservation.

### 4.3 Generic NVIDIA

`compose.nvidia.yaml` provides:

- `vllm` on internal port 30000;
- optional profile `embeddings`, `vllm-embed` on 30003;
- a CUDA orchestrator for the optional in-process reranker.

Generic NVIDIA profiles share the main model for router/agent roles. Startup
arguments, context, concurrency, GPU-memory utilization, and model paths come
from generated env and the pinned manifest. No generic profile configures
tensor parallelism; the selector consequently budgets per-device/free VRAM,
not aggregate VRAM. The only layout that does configure it is the DGX Spark
`CLUSTER_MODE=dual` overlay (`CLUSTER_TENSOR_PARALLEL_SIZE=2` across two
nodes, `launcher/techsara_cli/cluster.py`), and even that budgets each node
against its own GB10 at `CLUSTER_GPU_MEMORY_UTILIZATION` (0.30), never the
sum.

The Windows WSL2 overlay is layered only after detection confirms WSL2, Linux
containers, and a passing Docker GPU smoke probe.

### 4.4 CPU

`compose.cpu.yaml` starts a digest-pinned llama.cpp server on internal port
30000 with the revision-pinned GGUF file mounted read-only. It provides minimal
local chat without embeddings, reranking, vision, or OCR.

### 4.5 Application-only and external development

`compose.external-development.yaml` adds host-gateway resolution but no model
service. `app-only` emits disabled model endpoints. `external-development`
copies only explicit, validated local/container OpenAI-compatible endpoints and
declared capability fields from `.env` into generated config. Public/cloud
hosts and URL credentials are rejected; there is no automatic cloud fallback.

## 5. Named volumes and preservation

| Compose key | Explicit volume name | Contents |
|---|---|---|
| `data` | `sf-local-ai_data` | DuckDB warehouse, LanceDB index, Parquet, workspaces, staged Salesforce key |
| `reports` | `sf-local-ai_reports` | generated report files |
| `pgdata` | `sf-local-ai_pgdata` | PostgreSQL application state, accounts, conversations |
| `pgadmin` | `sf-local-ai_pgadmin` | pgAdmin state |
| `hf-cache` | `sf-local-ai_hf-cache` | runtime Hugging Face cache |

Model weights are not stored in a Docker volume. The selected host model cache
is bind-mounted read-only as `/models` into inference services.

`techsara down` executes `docker compose down --timeout 120` without `-v` or
`--volumes`. It also stops only native processes whose project, PID start
identity, command fingerprint, model, runtime and port ownership match. All
volumes, caches, downloads, runtime installs, reports, and configuration are
therefore preserved.

## 6. Build contexts and runtime users

| Component | Dockerfile | Base/runtime posture |
|---|---|---|
| Orchestrator portable | `orchestrator/Dockerfile.cpu` | digest-pinned Python 3.11 slim, no CUDA runtime |
| Orchestrator CUDA | `orchestrator/Dockerfile.cuda` | digest-pinned NVIDIA vLLM base, supports in-process reranker |
| Sync worker | `sync-worker/Dockerfile` | Python 3.11 slim; stable non-root UID 10001 |
| Frontend | `frontend/Dockerfile` | Node 20 Alpine multi-stage; non-root runtime user |

The base orchestrator image seeds shared data directories with ownership usable
by the sync worker. The CUDA orchestrator remains root because that is the
tested CUDA/reranker runtime posture; this is a known hardening boundary, not a
claim that every application container is non-root.

Application dependency files use compatible version ranges rather than a
complete hash lock. Runtime/model container images and the three base service
images in `compose.yaml` are digest-pinned. The direct native vLLM/vLLM-Metal
artifacts are hash-verified, while their fallback transitive Python dependency
resolution is not yet fully hash-locked.

## 7. Profiles

Compose profiles are added by the launcher:

- `embeddings` when the selected profile declares an embedding model;
- `reranker` when the selected profile declares a reranker model and the
  reranker feature (on DGX Spark: `vllm-reranker` on internal port 30005);
- `ocr` when the selected profile declares OCR and `--skip-ocr` was not used;
- `search` when enabled provider `searxng` needs the local service, or when
  explicitly requested in `COMPOSE_PROFILES`; Tavily/Brave do not start it;
- `admin` when `admin` or `pgadmin` appears in `COMPOSE_PROFILES`.

An optional component can be selected but still be removed from the effective
runtime after a failed live probe. The launcher stops it and republishes
capability flags/endpoints before application startup.

## 8. Startup and health contracts

The launcher does not use one blanket `compose up`. It validates config, builds
application images, and starts services individually:

1. PostgreSQL, then optional SearXNG.
2. Optional embedding service and an actual embedding request from a temporary
   orchestrator container.
3. Optional separate router and an actual chat request.
4. Optional OCR and an image-bearing OCR request.
5. Main model and an actual chat request. One force-recreate retry is allowed
   at the next safer context and concurrency one.
6. Declared vision support gets an additional image-bearing request and is
   disabled if this optional contract fails.
7. Orchestrator, then host-side validation of a structured `/health` response
   and application DB status.
8. Sync-worker when Salesforce credentials are complete, independently of
   embedding health.
9. Frontend, then optional pgAdmin.

Readiness uses bounded polling of `docker compose ps --format json`. An exited
service fails immediately; health-required services must report `running` and
`healthy`. API readiness validates response bodies rather than accepting HTTP
success alone.

## 9. Ports and trust boundaries

The launcher generates `TECHSARA_BIND_ADDRESS=127.0.0.1`. Published application
ports therefore remain on loopback under the supported flow. Model services
use `expose` only and the internal inference network.

Container commands still bind their internal listeners to `0.0.0.0`; that is a
container namespace detail, not a host publication. The host exposure boundary
is the loopback-only `ports` mapping.

`CLUSTER_MODE=dual` is the documented exception: the `vllm` head runs with
`network_mode: host` (NCCL over RoCE needs the ConnectX-7 netdevs and the
cross-node message queue must be reachable at the link address), so its API
is a host listener at `CLUSTER_API_BIND_ADDRESS:${VLLM_PORT}` — `0.0.0.0` when
`PUBLISH_MODEL_PORTS=true`, otherwise the Docker bridge gateway (`172.17.0.1`)
that only containers on Node 1 can reach. Cluster traffic is pinned to the
direct links; the host firewall is the boundary. See
[`../CLUSTER.md`](../CLUSTER.md#security-notes).

Changing bind addresses, publishing model ports, or placing a reverse proxy in
front creates a different security model. Add explicit authentication, TLS,
network policy, and origin review before doing so.

## 10. Operational commands

```bash
./techsara up --dry-run        # pure plan + merged Compose validation
./techsara up                  # staged start and probes
./techsara status              # read-only stored/native/Docker status
./techsara doctor              # non-destructive prerequisites/config checks
./techsara logs --tail 200     # Compose + native logs with redaction
./techsara down --dry-run      # show scoped stop plan
./techsara down                # preserve volumes/caches/configuration
```

`ComposeManager.display_command()` returns JSON argv, not a shell command
string, so paths are not presented with shell-evaluation semantics.

## 11. Verification

The Compose constructor, environment precedence, parsing, readiness, staged
fallback, and CLI lifecycle are covered by the launcher suite. On 2026-08-11:

```text
PYTHONPATH=launcher python3 -m pytest launcher/tests -q
264 passed, 266 subtests passed
```

As of 2026-08-25 the suite holds 318 tests, including
`launcher/tests/test_cluster.py` for the `CLUSTER_*` validation, detection,
and engine-argument contract.

Those are mocked/unit tests: they do not build images, start Docker, pull
models, access external networks, or signal real processes.

The overlays themselves are validated against Docker.
`launcher/tests/test_compose_overlays.py` renders all 13 supported host
fixtures — five Apple Silicon tiers, DGX Spark, four generic NVIDIA tiers,
Windows/WSL2 NVIDIA, CPU, and app-only — through real
`docker compose config --format json` with the `embeddings`, `ocr`, `search`,
and `admin` profiles all active, then asserts:

- application services (`orchestrator`, `frontend`, `postgres`) are present;
- the `data`, `reports`, and `pgdata` volumes survive every overlay;
- no model service (`vllm*`, `llama-cpp`) publishes a host port;
- every published port is bound to `127.0.0.1` or `::1`;
- non-NVIDIA families use `sf-local-ai-orchestrator:cpu` and declare zero
  device reservations, and define no CUDA model services;
- NVIDIA families use `sf-local-ai-orchestrator:cuda` and keep their
  `driver: nvidia` / `capabilities: [gpu]` reservation;
- the DGX overlay keeps its measured flags (`--kv-cache-dtype fp8`,
  `--reasoning-parser qwen3`, `--quantization modelopt`,
  `--attention-backend flashinfer`, chunked prefill,
  prefix caching, `--max-num-batched-tokens 8192`,
  `--gpu-memory-utilization 0.35`) and its served model id
  (`Qwen/Qwen3.8-27B-NVFP4`);
- the DGX fixture rendered again with `CLUSTER_MODE=dual` turns `vllm` into
  node-rank 0 on host networking with no `networks`/`expose`/`ports`, carries
  `--nnodes 2 --tensor-parallel-size 2 --gpu-memory-utilization 0.30`, keeps
  the other model services off host networking, gives orchestrator and
  sync-worker `vllm:host-gateway`, and binds `--host 0.0.0.0` only under the
  publish opt-in;
- the main model keeps its tool-calling flags (`--tool-call-parser qwen3_xml`,
  `--enable-auto-tool-choice`, added 2026-08-11 for Salesforce Intelligence
  Mode's request planner). They are additive: nothing else in the app sends
  `tools`, and the planner downgrades to guided JSON on a backend without them;
- every image is digest-pinned, including every `FROM` in the four build
  Dockerfiles;
- no bind source or build context escapes the project root or the selected
  cache, and no Compose source file contains a developer home directory;
- generated environment values reach the orchestrator, sync-worker, and
  frontend containers.

`docker compose config` only resolves and renders — it starts, pulls, and
creates nothing — and the module skips when Compose v2.24+ is unavailable.
