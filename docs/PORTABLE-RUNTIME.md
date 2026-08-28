# Portable runtime and launcher

This document is the operational source of truth for TechSara's cross-platform
launcher introduced on 2026-08-11. It describes the implementation in
`techsara*`, `launcher/`, `config/`, `compose.yaml`, and `compose/`.

## 1. Goals and boundaries

The launcher provides one command surface for macOS, Linux, and Windows while
preserving the same application and data plane. It is responsible for:

- non-destructive host and Docker capability detection;
- deterministic, memory- and disk-aware profile selection;
- revision-pinned model acquisition and validation;
- a shared, pinned native Apple runtime with project-scoped process ownership;
- generated configuration and local secret creation;
- Docker Compose construction and validation;
- staged startup, API probes, bounded fallback, and graceful degradation;
- ownership-safe lifecycle management for native processes;
- status, diagnostics, redacted logs, and non-destructive shutdown.

It does not install or reconfigure Docker, Docker Desktop, WSL2, GPU drivers,
the NVIDIA Container Toolkit, or host security controls. It never silently
falls back to a public model service.

## 2. Entrypoints

| Platform | Entrypoint | Bootstrap behavior |
|---|---|---|
| macOS/Linux | `./techsara` | Finds or installs the pinned `uv` binary, then runs `techsara_cli` |
| Windows PowerShell | `.\techsara.ps1` | Equivalent pinned bootstrap and Python module invocation |
| Windows Command Prompt | `techsara.cmd` | Delegates to `techsara.ps1` |
| Tests/development | `python3 -m launcher.techsara_cli` | Direct module entry when imports are available |

Bootstrap downloads are placed under `TECHSARA_HOME`, defaulting to
`~/.techsara`, and their manifest SHA-256 values are checked before execution.
The pinned `uv` version and installer URLs are declared in
`config/model-manifest.yaml`.

## 3. Command contract

```text
techsara up [--dry-run] [--profile ID] [--model HF_ID]
            [--skip-ocr] [--offline] [--verbose]
techsara restart [the same options as up]
techsara down [--dry-run]
techsara status
techsara doctor [--offline]
techsara logs [--tail 1..10000] [--service NAME]
techsara models
techsara redetect
techsara update-models [--offline] [--dry-run]
```

`restart` performs the same ownership-scoped stop as `down`, followed by the
normal `up` flow. `redetect` refreshes the stored hardware and selected-profile
documents but does not start services. `models` inspects the current selected
model set; `update-models` ensures that same immutable set is cached.

`status` reports detected hardware, the selected profile and models, the
installed pinned runtime, the model cache and per-model install state, owned
native PIDs with their ports and health, Docker service state, live orchestrator
/frontend/native endpoint health, per-feature capability state, the recorded
capability-probe results, and every degraded reason.

`doctor` runs non-destructive checks only — Docker/Compose, architecture, total
and available memory, free disk, container GPU or Apple/Metal prerequisites,
model-cache writability, the model manifest, the pinned macOS runtime, the
environment file, secret and runtime-directory permissions, artifact-host
reachability, the resolved Compose configuration, live orchestrator/frontend
endpoints, project-owned native listeners, and (on native macOS profiles) real
container-to-host model reachability. It exits non-zero only for the blocking
prerequisites listed in `_BLOCKING_CHECKS`; everything else is reported as a
degraded capability with exact remediation. `--offline` skips the network
reachability checks and stops detection from pulling the tiny GPU probe image.

Health probes and the printed frontend/orchestrator URLs follow the configured
`FRONTEND_PORT`, `ORCHESTRATOR_PORT`, and bind address rather than assuming
`3000`/`8080`. A wildcard publish address is always dialled over loopback.

### Launcher dry-run

`up --dry-run` detects hardware, checks Docker prerequisites, selects a
profile, plans model/runtime work, builds a secret-free environment, writes
temporary generated and secret env files outside the project, and executes
`docker compose config --quiet`. `_start_compose` then returns a planned result
without walking the staged service actions.

The temporary directory is automatically removed. Dry-run does not create the
project `.runtime/` tree, write selection or state documents, persist secrets,
install runtimes, download models, build images, start containers, wait on live
health, or signal processes.

This guarantee covers the Python launcher. On a clean host, the wrapper may
first install the checksum-verified uv bootstrap and managed Python 3.12. With
`--offline`, missing bootstrap/runtime prerequisites fail instead.

`down --dry-run` prints the JSON-form Compose command and lists owned native
processes it would stop without changing either.

## 4. Detection

`launcher/techsara_cli/hardware.py` normalizes:

- operating-system name/version and host/native architecture;
- CPU name and logical core count;
- total and currently available system memory;
- Apple chip, unified memory, native arm64 support, and Rosetta translation;
- NVIDIA GPU name, count, per-device total/free memory, driver, and compute
  capability;
- DGX Spark identity;
- Windows WSL2 readiness;
- Docker CLI, daemon, server architecture, Linux-container mode, Compose
  version, and an actual Docker GPU smoke result;
- selected model-cache path and free disk at that filesystem.

Malformed or unavailable detector output degrades to conservative unknowns
instead of authorizing an accelerator profile. An NVIDIA driver alone is not
sufficient: CUDA profiles require the container GPU probe to pass.

### The container GPU probe

The probe runs a container with `--gpus all` and asserts that the NVIDIA
container runtime actually injected `/dev/nvidiactl`. A host `nvidia-smi`, a
loaded driver, or an installed toolkit is never accepted on its own.

Candidate images are tried in order, always with `--pull never`, so an ordinary
re-detection on a configured host pulls nothing:

1. `TECHSARA_GPU_SMOKE_IMAGE`, when the operator sets it;
2. the pinned NGC and vLLM runtime images the NVIDIA profiles already need;
3. `GPU_PROBE_IMAGE` — a digest-pinned ~4 MiB BusyBox image.

Only if none of those is cached does the probe pull, and then only the tiny
image. This matters for a clean clone: before this ordering existed, detection
required a ~20 GiB runtime image to already be present, so a fresh NVIDIA or
DGX Spark host silently reported no container GPU and downshifted to the CPU
profile. `--offline` disables the pull; an uncached host then reports no
container GPU rather than guessing.

Windows GPU use additionally requires WSL2 and Docker Linux containers. WSL1,
Windows-container mode, or a failed Docker GPU probe selects a non-CUDA path and
records an actionable degraded reason.

## 5. Profile selection

Profile declarations are data in `config/hardware-profiles.yaml`. Selection
uses both total and currently available memory, keeps explicit OS/Docker/app/
runtime/safety reserves, and estimates loaded weights plus bounded runtime/KV
overhead. On a cold NVIDIA start, free VRAM per usable device is the binding
accelerator value. Aggregate memory across multiple GPUs is not treated as one
contiguous allocation because the generic profiles do not configure tensor
parallelism. The one explicit exception is `dgx-spark` with `CLUSTER_MODE=dual`,
which shards the main model across two DGX Sparks with vLLM tensor parallelism
(TP=2); even there each node is budgeted against its own GB10
(`CLUSTER_GPU_MEMORY_UTILIZATION`, default 0.30) and the two memories are never
one address space. See [`CLUSTER.md`](CLUSTER.md).

Two gates run before the budget arithmetic:

1. **Declared minimum.** No model is selected onto hardware below the
   `minimum_memory_bytes` its own manifest entry declares — measured against
   per-device memory on NVIDIA and against the unified/system pool elsewhere. A
   manual `--profile`/`--model` override that violates this is rejected as
   unsafe rather than downshifted.
2. **Tier floors match the manifest.** The `nvidia-minimal` floor is 10 GiB of
   free per-device VRAM, not 8, because that is the declared minimum of the
   smallest CUDA model in the manifest. A threshold below a model's own model
   card would select a tier that cannot hold weights, KV cache, and CUDA
   context together.

A GPU too small or too busy for any CUDA tier does not select app-only: the CPU
tier still serves the small GGUF model and never touches the device.

Applying the budget honestly makes one declared tier unreachable at its lower
bound. A 16 GiB Mac selects `app-only`, because the smallest Mac candidate needs
about 7.6 GiB resident and macOS plus the Docker Desktop VM running PostgreSQL,
the orchestrator, the frontend, and the sync worker needs the remainder;
`mac-16-24gb` becomes reachable from roughly 24 GiB. The degraded reason names
both sides of the shortfall and the total, and points at
`--profile external-development` for a model server the operator already runs.

For native Mac processes, reuse requires a matching ownership record, command
fingerprint, model, port, runtime version, PID identity, and healthy state. For
Docker selection, a running project-labeled `vllm` container is currently only
a warm-capacity hint; model/command/health compatibility is established later
by startup and API probes rather than by that initial hint.

| Hardware profile | Main model key | Context candidates | Concurrency | Declared optional features |
|---|---|---:|---:|---|
| `mac-16-24gb` | `mac-qwen3-8b-4bit` | 8,192 → 4,096 | 1 | none |
| `mac-32-47gb` | `mac-qwen3-14b-4bit` | 16,384 → 8,192 → 4,096 | 1 | embeddings |
| `mac-48-79gb` | `mac-qwen36-35b-4bit` | 32,768 → 16,384 → 8,192 | 1 | embeddings, reranking |
| `mac-80-127gb` | `mac-qwen36-35b-4bit` | 32,768 → 16,384 → 8,192 | 2 | embeddings, reranking |
| `mac-128gb-plus` | `mac-qwen36-35b-6bit` | 65,536 → 32,768 → 16,384 | 2 | embeddings, reranking |
| `dgx-spark` | `dgx-qwen38-27b-nvfp4` | 262,144 → 131,072 → 65,536 | 4 | vision router, embeddings, reranking, OCR; `CLUSTER_MODE=dual` serves the main model across two nodes ([`CLUSTER.md`](CLUSTER.md)) |
| `nvidia-large` | `nvidia-qwen36-35b-fp8` | 32,768 → 16,384 → 8,192 | 2 | vision, embeddings, reranking |
| `nvidia-medium` | `nvidia-qwen3-30b-fp8` | 16,384 → 8,192 → 4,096 | 1 | embeddings |
| `nvidia-small` | `nvidia-qwen3-14b-awq` | 16,384 → 8,192 → 4,096 | 1 | embeddings |
| `nvidia-minimal` | `nvidia-qwen3-8b-awq` | 8,192 → 4,096 | 1 | none |
| `local-minimal` | `cpu-qwen3-06b-q8` | 4,096 → 2,048 | 1 | none |
| `app-only` | none | 4,096 configuration ceiling | 0 | none |
| `external-development` | explicit local endpoint | 8,192 default | 1 | explicitly declared only |

The actual selected profile ID for a Mac includes normalized chip and memory
information, while `hardware_profile_id` retains the declaration key above.

Automatic downshift chains stay within a backend family. If no local model fits
the safe budget, the result is `app-only`; the launcher does not choose a cloud
provider. Explicit incompatible/oversized profile or model overrides fail
instead of silently changing the request.

### Manual override rules

- `--profile` must be a declared profile and pass the same OS, architecture,
  accelerator, WSL2, Docker, and memory safety gates.
- `--model` must exactly match an ID in the revision-pinned manifest and use a
  backend compatible with the profile family.
- Context values are capped by the model manifest. The generated global and
  per-role context values cannot widen a model beyond that limit.
- External-development URLs must be HTTP(S) and explicitly local: loopback,
  `host.docker.internal`, or a container-network hostname. Embedded
  credentials and public/cloud hosts are rejected.

## 6. Model policy

`config/model-manifest.yaml` is schema-versioned and records, per model:

- repository ID and immutable commit revision;
- backend and quantization;
- approximate download and loaded-weight sizes;
- model limit and the context tested for the profile;
- minimum/recommended memory;
- declared chat, reasoning, tools, structured output, vision, embedding,
  reranking, OCR, streaming, and tokenizer behavior;
- startup arguments, endpoint type, health probe, required files, and license
  metadata;
- optional legacy directory names and allowlists.

| Model key | Immutable model ID | Backend | Quantization |
|---|---|---|---|
| `mac-qwen3-8b-4bit` | `mlx-community/Qwen3-8B-4bit` | vLLM-Metal | MLX 4-bit |
| `mac-qwen3-14b-4bit` | `mlx-community/Qwen3-14B-4bit` | vLLM-Metal | MLX 4-bit |
| `mac-qwen36-35b-4bit` | `mlx-community/Qwen3.6-35B-A3B-4bit` | vLLM-Metal | MLX 4-bit |
| `mac-qwen36-35b-6bit` | `mlx-community/Qwen3.6-35B-A3B-6bit` | vLLM-Metal | MLX 6-bit |
| `mac-qwen3-embed-06b-8bit` | `mlx-community/Qwen3-Embedding-0.6B-8bit` | vLLM-Metal | MLX 8-bit |
| `mac-qwen3-reranker-06b-8bit` | `mku64/Qwen3-Reranker-0.6B-mlx-8Bit` | vLLM-Metal | MLX 8-bit |
| `dgx-qwen38-27b-nvfp4` | `RadixArk/Qwen3.8-27B-NVFP4` (served as `Qwen/Qwen3.8-27B-NVFP4`; previous `dgx-spark` main model, still in the manifest) | vLLM CUDA | NVFP4/ModelOpt |
| `dgx-qwen36-35b-nvfp4` | `nvidia/Qwen3.6-35B-A3B-NVFP4` (served as `Qwen/Qwen3.6-35B-A3B-NVFP4`; the `dgx-spark` main model since 2026-08-29, single or dual node) | vLLM CUDA | NVFP4/ModelOpt |
| `dgx-qwen3-vl-8b-fp8` | `Qwen/Qwen3-VL-8B-Instruct-FP8` | vLLM CUDA | FP8 |
| `qwen3-embed-06b` | `Qwen/Qwen3-Embedding-0.6B` | vLLM CUDA | BF16 |
| `qwen3-reranker-06b` | `Qwen/Qwen3-Reranker-0.6B` | Transformers CUDA | BF16 |
| `unlimited-ocr` | `baidu/Unlimited-OCR` | vLLM CUDA | BF16 |
| `nvidia-qwen36-35b-fp8` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM CUDA | FP8 |
| `nvidia-qwen3-30b-fp8` | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` | vLLM CUDA | FP8 |
| `nvidia-qwen3-14b-awq` | `Qwen/Qwen3-14B-AWQ` | vLLM CUDA | AWQ 4-bit |
| `nvidia-qwen3-8b-awq` | `Qwen/Qwen3-8B-AWQ` | vLLM CUDA | AWQ 4-bit |
| `cpu-qwen3-06b-q8` | `Qwen/Qwen3-0.6B-GGUF` | llama.cpp CPU | GGUF Q8_0 |

Downloads are sequential so large model acquisitions do not compete for disk
bandwidth or staging headroom. Before the first download, the manager sums all
missing models at 120% of their declared download size and adds 1 GiB shared
overhead. A second per-model check applies the same 120% plus 1 GiB rule.

Each managed path is revision-qualified. Downloads go to `.partial`, resume
there after failure, validate required files and any declared file hashes,
write `.complete.json`, then atomically rename to the final directory. Invalid
complete directories are preserved for manual inspection and are never
overwritten. Cache, destination, staging, and legacy paths are containment- and
symlink-checked.

`HF_TOKEN` from the process environment, or from `.env` when not exported, is
withheld for the anonymous attempt and used for one authenticated retry. Error
output is redacted and partial data remains resumable. `--offline` never
invokes a downloader.

The production manifest currently relies chiefly on immutable Hugging Face
revision pins plus required-file validation; it does not provide a SHA-256 for
every model file. This is a weaker content-integrity guarantee than a complete
per-file hash manifest and should not be described as one.

## 7. macOS native runtime

Docker Model Runner vLLM is unavailable on macOS. TechSara's Mac path is a
native arm64 vLLM-Metal environment, with only application/data services in
Docker.

The runtime manifest pins:

- Python 3.12;
- vLLM-Metal 0.2.0 at a specific release tag/commit;
- vLLM 0.21.0 source artifact;
- direct wheel/source URLs and SHA-256 values.

The manager installs under `~/.techsara/runtimes/vllm-metal-0.2.0` by default,
using a `.partial` sibling and atomic promotion. Existing invalid or partial
runtimes are preserved rather than overwritten. Runtime validation requires a
matching marker, native arm64 Python, importable `vllm`/`vllm_metal`, and the
expected vLLM version.

The first install path uses `uv pip install --require-hashes` with the two
verified local artifacts. Because environment interpolation in requirements
files is not portable across all uv/pip versions, there is a fallback that
installs those same verified direct artifacts. That fallback may resolve
transitive dependencies from package indexes without a complete hash-locked
closure. `pip freeze` output is captured in `runtime.json`, but this is a
record of the result, not a fully reproducible dependency lock.

The pinned runtime is text-first. Main chat, embeddings, and reranking are
started only where selected and are capability-probed. Mac vision and OCR stay
intentionally disabled until a pinned release passes stable image probes.

Native model processes use direct loopback ports 18000/18003/18005. Separate
bridges listen for container traffic on 18100/18103/18105, require a generated
bearer token, and forward to the loopback upstream without forwarding the
client Authorization header.

## 8. Runtime state and ownership

Project state:

| Path | Content | Persistence/security |
|---|---|---|
| `.runtime/hardware.json` | normalized detector result | regenerated |
| `.runtime/selected-profile.json` | resolved profile, model specs, budget and degraded reasons | regenerated |
| `.runtime/generated.env` | secret-free endpoints, paths, contexts and capabilities | mode `0644` |
| `.runtime/secrets.env` | generated DB/session/search/native bridge secrets | mode `0600` |
| `.runtime/state.json` | Compose files/profiles, model/runtime status, startup result | atomic write |
| `.runtime/capabilities.json` | live native capability probe results | atomic write |
| `.runtime/locks/` | launcher/model/runtime locks | live-owner aware |
| `.runtime/logs/` | native process logs | rotated at 10 MiB |
| `.runtime/pids/` | native ownership records | mode `0600` |

Shared defaults:

```text
${TECHSARA_HOME:-~/.techsara}/
  bin/uv
  downloads/
  model-cache/huggingface/
  runtimes/vllm-metal-0.2.0/
```

Models use process-environment `TECHSARA_MODEL_CACHE` when set; detection occurs
before project `.env` is parsed. Otherwise the defaults are
`~/Library/Caches/TechSara/models` on macOS,
`%LOCALAPPDATA%\TechSara\models` on Windows, and
`${XDG_CACHE_HOME:-~/.cache}/techsara/models` on Linux. A compatible existing
`VLLM_MODELS_DIR` or sibling `vllm_models` directory can be adopted as a legacy
cache, but `.env.example` contains no machine-specific path.

Process records include the project root, PID, OS process-start identity,
command fingerprint, model, runtime version, port, log, time, and health. A
record is reused only when it is owned, alive, identity-stable, compatible, and
healthy. Stop rechecks identity immediately before `SIGTERM`; it never signals
an unowned or PID-reused process. `SIGKILL` is used only after a bounded grace
period and another identity check.

## 9. Generated environment and Compose

The base file is `compose.yaml`. Exactly one runtime overlay is selected (plus
the Windows WSL2 modifier where applicable), followed by the modifiers below:

| Profile family | Overlay |
|---|---|
| Mac | `compose/compose.mac.yaml` |
| DGX Spark | `compose/compose.dgx-spark.yaml` |
| Generic NVIDIA | `compose/compose.nvidia.yaml` |
| Windows NVIDIA | NVIDIA overlay, then `compose/compose.windows-wsl2.yaml` |
| CPU | `compose/compose.cpu.yaml` |
| app-only/external | `compose/compose.external-development.yaml` |

Two modifiers may follow the runtime overlay: the per-family
`compose/compose.published-*.yaml` when `PUBLISH_MODEL_PORTS=true` (§12), and —
only on `dgx-spark` with `CLUSTER_MODE=dual` — `compose/compose.cluster-dgx-spark.yaml`,
layered last, which turns `vllm` into node-rank 0 of a two-node engine. Node 2
runs `compose/compose.cluster-worker.yaml` as its own project
(`sf-local-ai-worker`), outside the launcher; see [`CLUSTER.md`](CLUSTER.md).

The environment chain is:

1. project `.env`, optional — user settings;
2. `.runtime/secrets.env`, optional — launcher-managed local secrets;
3. `.runtime/generated.env`, required and last — launcher-selected non-secret
   runtime contract.

Docker Compose normally gives inherited shell variables precedence over env
files. `ComposeManager` deliberately reapplies the same file order to the
subprocess environment, preserving unrelated variables such as `PATH` and
`DOCKER_HOST` while preventing stale exported managed variables from
overriding the selected configuration.

Important generated fields include selected profile/backend, cache and model
container paths, local service URLs/model IDs, per-role capability flags,
contexts, concurrency, selected Compose env paths, validated search state and
provider (`searxng`, `tavily`, or `brave`), and bounded runtime arguments.
`TECHSARA_CLUSTER_MODE` is always generated (`single` unless `.env` sets
`CLUSTER_MODE=dual`); in dual mode the validated `CLUSTER_*` keys follow,
including the detected `CLUSTER_NCCL_SOCKET_IFNAME`/`CLUSTER_NCCL_IB_HCA`,
`CLUSTER_API_BIND_ADDRESS`, and the single `CLUSTER_ENGINE_ARGS` string both
nodes interpolate, while `OPENAI_BASE_URL`/`VISION_BASE_URL` become
`http://vllm:${VLLM_PORT}/v1` ([`CLUSTER.md`](CLUSTER.md#configuration-env-user-owned)). Only complete/legacy-complete installs are mapped into real
containers; dry-run may map explicit `planned` installs inside its temporary
environment.

## 10. Staged startup

The non-dry run is serialized with a launcher lock:

1. Detect hardware and enforce Docker/Compose/Linux-container prerequisites.
2. Select or safely reuse a profile.
3. Create/reuse local secrets without overwriting `.env`.
4. Preflight and ensure the full revision-pinned model set.
5. On Mac, ensure the native runtime and start optional native components,
   bridges, then main; probe each before marking healthy.
6. Write hardware, profile, generated env, and native capability results.
7. Validate the fully merged Compose configuration.
8. Build orchestrator, sync-worker, and frontend images.
9. Start PostgreSQL; start SearXNG when requested; wait for health.
10. Start optional CUDA embeddings, then the DGX separate router, then DGX OCR.
    Perform role-specific real API requests from an orchestrator container.
    Embedding/OCR failure stops and disables the component; router failure
    repoints router/agent roles to main.
11. Start and probe main inference. On the first failure, stop it and retry
    once at `startup_retry_context` with concurrency one. A second failure is
    terminal. In dual mode the head (`vllm`, node-rank 0) waits for the Node 2
    worker to rendezvous; the launcher prints the head/worker line first, and
    a failure points at `scripts/cluster-status.sh` instead of the context
    retry, which cannot supply a missing worker. `scripts/cluster-up.sh` starts
    the worker around this stage ([`CLUSTER.md`](CLUSTER.md#operations)).
12. For profiles declaring vision, perform the additional role-specific image
    probe and disable vision if that optional contract fails.
13. Start orchestrator and validate `/health`, including the application DB
    check.
14. If Salesforce credentials are complete, start sync-worker even when
    embeddings degraded; warehouse sync remains useful without vector updates.
15. Start frontend, then optional pgAdmin.
16. Persist the final profile degradation and state documents.

Native Mac `CapabilityProber` success requires valid response schemas, not
merely HTTP 200: it checks the selected model in `/v1/models`, chat message and
SSE shapes, structured-output JSON when declared, embedding/reranker results,
and tokenize shape/context bounds as applicable. Docker and external startup
use narrower role-specific synthetic chat/embedding/OCR/vision requests from a
temporary orchestrator container, followed by the orchestrator health contract.

## 11. Preservation and recovery

`techsara down` invokes Compose without `-v`/`--volumes` and stops only native
processes proven to belong to the current project. It preserves:

- `sf-local-ai_data` (DuckDB, LanceDB, staged JWT key, workspaces);
- `sf-local-ai_reports`;
- `sf-local-ai_pgdata` (accounts and conversation history);
- `sf-local-ai_pgadmin`;
- `sf-local-ai_hf-cache`;
- model and runtime caches;
- `.env`, generated state, logs, and partial resumable downloads.

An invalid model/runtime is intentionally not auto-deleted or overwritten.
Move it aside manually after inspection, then rerun. This preservation policy
avoids turning a transient validation or version mismatch into data loss.

## 12. Network and secret boundary

- Frontend: `127.0.0.1:${FRONTEND_PORT:-3000}`.
- Orchestrator: `127.0.0.1:${ORCHESTRATOR_PORT:-8080}`.
- PostgreSQL: `127.0.0.1:${POSTGRES_PORT:-5432}`.
- Optional pgAdmin: `127.0.0.1:${PGADMIN_PORT:-5050}`.
- Model containers: internal `expose` only on the `inference` network.
- The inference network is `internal: true`.
- Documented exception: with `CLUSTER_MODE=dual` the `vllm` head runs with host
  networking (NCCL over RoCE needs the host netdevs) and its API listens on the
  host at `CLUSTER_API_BIND_ADDRESS:${VLLM_PORT}` — `0.0.0.0` when
  `PUBLISH_MODEL_PORTS=true`, otherwise the Docker bridge gateway
  (`172.17.0.1`), reachable only by containers on Node 1. Router, embeddings,
  reranker, and OCR stay expose-only. See
  [`CLUSTER.md`](CLUSTER.md#security-notes).

`TECHSARA_BIND_ADDRESS` is generated as `127.0.0.1`; user environment cannot
silently override it through the normal launcher flow. Native Mac model
upstreams bind loopback, but Docker-facing bridge listeners intentionally bind
host `0.0.0.0` on ports 18100/18103/18105. The bridges require a generated
bearer token, cap request bodies, strip authorization/hop-by-hop headers before
forwarding, and do not log requests. Host firewall policy remains part of this
boundary. Secret values are never included in generated env/displayed Compose
argv, and known values are redacted from errors/log output.

The app has no login/session gate and reports one stable local identity. This
is a local-host, single-user boundary, not a hardened multi-user/public service
boundary. Reverse proxying, changing bindings, or sharing the Docker socket
requires a separate threat model and explicit authentication/TLS/access rules.

### Network exposure

Publication is loopback-by-default and widened only by explicit configuration:

| Setting | Default | Effect |
|---|---|---|
| `TECHSARA_BIND_ADDRESS` | `127.0.0.1` | Host address for the frontend and orchestrator. Must be a literal IP; a hostname or free text is rejected rather than interpolated into a port mapping. |
| `PUBLISH_MODEL_PORTS` | `false` | When true, layers a per-family `compose/compose.published-*.yaml` overlay that publishes the model APIs. |
| `VLLM_PORT` / `VLLM_ROUTER_PORT` / `VLLM_EMBED_PORT` / `VLLM_OCR_PORT` / `VLLM_RERANKER_PORT` / `LLAMA_CPP_PORT` | `8000` / `8002` / `8003` / `8004` / `8005` / `8000` | Host ports used by that overlay. |
| `CLUSTER_MODE` | `single` | `dual` (dgx-spark only) moves the `vllm` head onto host networking, so `VLLM_PORT` is bound on the host regardless of the published overlay: `0.0.0.0` with `PUBLISH_MODEL_PORTS=true`, otherwise the Docker bridge gateway. |

There is one overlay per family rather than one shared file, because Compose
merges an unknown service name into a new, imageless service — a single file
naming `vllm-ocr` would break every profile that does not define it.

PostgreSQL and pgAdmin remain on `127.0.0.1` under every combination.

The model endpoints have no authentication. Publishing them on a non-loopback
address exposes unauthenticated inference to every host that can route to the
machine; the overlays say so at the top of each file. Reconciliation reflects
the choice: a model container with a published port is stopped as pre-launcher
drift when the opt-in is off, and left alone when it is on.

## 13. Extending profiles and updating pins

The launcher intentionally has no "latest model" operation. Changes to
hardware tiers, model revisions, runtime artifacts, or images are reviewed
source changes.

### Add or replace a model

1. Confirm the backend can load the exact artifact on the intended platform;
   record upstream model-card/license information.
2. Add a unique entry to `config/model-manifest.yaml` with an immutable
   40-character revision, realistic download/loaded-memory estimates, actual
   context limits and tested context, backend/quantization, every declared
   capability, endpoint/health behavior, required files, and only the startup
   arguments the launcher/runtime permits.
3. Add per-file SHA-256 values where stable upstream artifacts make that
   practical. Never imply required-file validation hashes unlisted weights.
4. Reference the model key from a hardware profile, or use the exact manifest
   model ID with `--model` for a compatible test profile.
5. Extend model-manager, profile-budget, environment/capability, command-safety,
   and backend/runtime tests. Validate offline reuse and an interrupted resume.
6. Run a real first load plus role-specific API/capability probes on the target
   platform before enabling the feature by default.

Changing a revision produces a new revision-qualified cache directory; the
manager does not overwrite the prior revision. `techsara update-models` ensures
the revisions already declared by the checked-out source—it does not discover
or edit upstream revisions.

### Add a hardware profile

1. Add the profile shape to `config/hardware-profiles.yaml`: family/backend,
   overlay, models, contexts in descending fallback order, concurrency, and
   conservative features.
2. Update the detector/selection threshold and downshift chain in
   `launcher/techsara_cli/profiles.py`; declaration alone does not make a new
   hardware tier automatically selectable.
3. Add or extend a Compose overlay only when the existing family overlay cannot
   express the runtime. Keep model paths read-only, model ports expose-only,
   and base application/data services in `compose.yaml`. Cluster (`CLUSTER_*`)
   keys are not profile data: they are validated, detected, and rendered into
   `CLUSTER_ENGINE_ARGS` by `launcher/techsara_cli/cluster.py`, paired with
   `compose/compose.cluster-*.yaml`.
4. Add constructed fixtures for nominal, boundary, insufficient available
   memory/VRAM/disk, multiple devices, malformed detection, manual override,
   and degradation. Validate generated env and exact overlay/profile argv.
5. Run `up --dry-run --verbose`, real merged Compose validation, cold/warm
   startup, probe/fallback, down/up preservation, offline reuse, and reboot
   recovery on the actual hardware.

### Update runtime or image pins

- Replace URLs/digests and hashes atomically in the manifest; never leave a
  moving tag as the trusted identity.
- For vLLM-Metal, verify native arm64 Python/import/version, direct artifact
  hashes, a clean install, resolved dependency set, and every selected Mac
  model role. A new runtime version installs beside the old one.
- For containers, validate each base/overlay merge, image architecture, GPU
  visibility, model load, healthcheck, and synthetic API request.
- Update the dated manifest verification field, tests, runtime docs, inventory,
  and changelog with the evidence actually collected.

### Upstream verification anchors

The following time-sensitive claims were checked against primary upstream
pages on 2026-08-11:

- Docker's [Model Runner inference-engine matrix](https://docs.docker.com/ai/model-runner/inference-engines/)
  lists vLLM for Linux x86_64 and Windows/WSL2 and explicitly marks macOS
  unsupported. This is the source for the DMR boundary; TechSara's native
  vLLM-Metal choice is a separate runtime.
- The pinned vLLM-Metal tag/commit is published in the
  [official vLLM-Metal release](https://github.com/vllm-project/vllm-metal/releases/tag/v0.2.0-20260527-105045).
- The bootstrap version exists in the
  [official uv 0.11.32 release](https://github.com/astral-sh/uv/releases/tag/0.11.32).

Every model revision, model-card license/capability claim, artifact hash, and
container digest remains an upstream claim that must be revalidated when its
manifest entry changes. The launcher verifies configured immutable identities;
it does not prove that upstream metadata can never be corrected later.

## 14. Troubleshooting and recovery

Use `techsara doctor`, `status`, redacted `logs`, and `up --dry-run --verbose`
before changing state.

| Symptom | Check/remediation |
|---|---|
| Bootstrap fails offline | Pinned uv and managed Python must already exist under `TECHSARA_HOME`; run one verified online bootstrap or pre-stage them. |
| Docker prerequisite failure | Start Docker, use Linux containers, update Compose to v2.24+, and on Windows enable WSL2. NVIDIA needs a passing container smoke test. |
| Unexpected smaller/app-only profile | Inspect available RAM/VRAM, per-device free VRAM, Docker GPU status, and printed degraded reasons; total capacity alone does not authorize a tier. |
| Disk preflight failure | Free space or export `TECHSARA_MODEL_CACHE` to a larger filesystem before invoking the wrapper. Allow aggregate download ×1.20 plus shared overhead. |
| Gated/private model failure | Put `HF_TOKEN` in `.env` or export it. The anonymous attempt happens first; partial staging remains resumable. |
| Invalid model/runtime | Inspect and manually move aside only the named invalid revision/runtime. The launcher refuses automatic overwrite/deletion. |
| Rosetta/Intel Mac | Launch a native arm64 terminal on Apple Silicon; the pinned vLLM-Metal runtime cannot run under Rosetta/Intel. DMR vLLM is unavailable on macOS. |
| Port collision | Stop/reconfigure the unrelated listener. Native lifecycle never adopts or kills an unowned process. |
| Optional feature disabled | Read degraded reasons and role logs. Embedding/OCR/vision may disable after probe; router may share main. Mac vision/OCR are intentionally off. |
| Salesforce sync absent | Supply identity plus one assertion method. Sync is independent of embeddings but remains credential-gated. |
| External endpoint rejected | Use explicit HTTP(S) loopback IP, `localhost`, `host.docker.internal`, or a single-label container hostname; public/deceptive hosts and URL credentials are rejected. |
| Mac services absent after reboot | Native model/bridge processes are not OS boot services; rerun `techsara up`. |

To remove a checkout while preserving data, run `techsara down`, back up `.env`
and `.runtime` as needed, and keep the five `sf-local-ai_*` volumes, model-cache
path, and `TECHSARA_HOME`. Do not use `down -v`, explicit volume removal, or
volume pruning. A later checkout can reconnect after restoring configuration.
A full purge is intentionally a separate destructive procedure.

## 15. Verification status and known limitation

Everything below was run on 2026-08-11 on the DGX Spark host (Ubuntu 24.04.4,
aarch64, NVIDIA GB10, 121.7 GiB unified memory).

| Suite | Command | Result |
|---|---|---|
| Launcher | `PYTHONPATH=launcher python3 -m pytest launcher/tests -q` | 264 passed, 266 subtests (318 tests as of 2026-08-25, after `test_cluster.py` was added) |
| Orchestrator | `TEST_DATABASE_URL=… .venv/bin/python -m pytest tests -q` | 1014 passed |
| Sync worker | `.venv/bin/python -m pytest tests -q` | 157 passed |
| Frontend | `npm run test` | 310 passed (24 files) |
| Frontend types | `npx tsc --noEmit` | clean |
| Frontend lint | `npm run lint` | no warnings or errors |

Launcher coverage includes hardware fixtures and malformed detector output; all
profile tiers; memory/VRAM/disk downshift; override safety; model resume/
offline/marker/symlink behavior; native runtime architecture and artifact
validation; process ownership and PID-reuse defense; strict capability schemas;
environment containment and secret boundaries; Compose command/env precedence;
published-endpoint resolution; doctor coverage; and mocked CLI lifecycle/
fallback/dry-run behavior. `launcher/tests/test_cluster.py` (2026-08-25) covers
`CLUSTER_*` validation, interface/HCA detection fallbacks, the bind address
following the publish opt-in, and the exact `CLUSTER_ENGINE_ARGS` string.

`launcher/tests/test_compose_overlays.py` is the one module that does invoke
Docker. It renders every supported host fixture — five Apple Silicon memory
tiers, DGX Spark, four generic NVIDIA tiers, Windows/WSL2 NVIDIA, CPU, and
app-only — through real `docker compose config` with all optional profiles
active, and asserts the platform invariants: application services present, data
/reports/pgdata volumes retained, no model API published to a host port, every
published port loopback-bound, CUDA-free orchestrator image and zero device
reservations off NVIDIA, NVIDIA device reservations retained on NVIDIA, the
DGX overlay's measured vLLM flags unchanged, digest-pinned images everywhere
(including every `FROM` in the four build Dockerfiles), no developer home
directory in any overlay, and generated environment values arriving in the
orchestrator, sync-worker, and frontend containers. It also renders the DGX
Spark fixture a second time with `CLUSTER_MODE=dual` and asserts the head
overlay's host networking, stripped `networks`/`expose`/`ports`, the
`--nnodes 2 --node-rank 0` argv, and the API bind address following the publish
opt-in ([`CLUSTER.md`](CLUSTER.md)). It skips when Docker
Compose v2.24+ is unavailable. `config` resolves and renders only: it starts,
pulls, and creates nothing.

The other launcher tests intentionally avoid live Docker lifecycle, downloads,
external network calls, real model servers, and real process signalling.

On this host, `techsara up --dry-run`, `techsara doctor`, `techsara status`, and
`techsara models` were executed live. A live `techsara up` was **not** run: this
machine is serving the production stack from the superseded `docker-compose.yml`,
and `up` would reconcile those publicly-published model services. The staged
live-start path is therefore covered by the dry run plus mocked lifecycle tests,
not by an executed production start.

The remaining supply-chain limitation is the native runtime's transitive
dependency closure: direct vLLM/vLLM-Metal artifacts are hash-verified and the
resolved package set is recorded, but the compatibility fallback is not fully
offline/no-index/hash-locked for every transitive package.
