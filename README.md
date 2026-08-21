# TechSara Local AI Platform

TechSara is a local-first Salesforce analytics and chat platform. It combines a
Next.js frontend, a FastAPI orchestrator, PostgreSQL application state, a
DuckDB/LanceDB analytics plane, an optional Salesforce sync worker, and a local
inference runtime selected for the host.

The supported entrypoint is `techsara`. It detects the operating system,
architecture, memory, accelerator, Docker state, and free disk; selects a safe
profile; installs only the shared pinned runtime and revision-pinned models it
needs; validates the resulting Compose configuration; starts components in
dependency order; and performs real health and API probes before reporting
success.

For the full runtime design, see
[`docs/PORTABLE-RUNTIME.md`](docs/PORTABLE-RUNTIME.md).

## Quick start

### Prerequisites

- Docker Engine or Docker Desktop, running Linux containers.
- Docker Compose v2.24 or newer.
- On Linux, an account that may open the Docker socket: membership in the
  `docker` group (`sudo usermod -aG docker "$USER"`, then `newgrp docker` or a
  fresh login), or a rootless Docker installation.
- On macOS/Linux, `curl` or `wget` and `sha256sum` or `shasum` when the pinned
  uv bootstrap is not already cached.
- Enough free disk for the selected model set plus download staging space.
- On Windows with NVIDIA acceleration: WSL2, Docker Desktop Linux containers,
  and a working Docker GPU path.
- On Apple Silicon: run from a native arm64 terminal, not a Rosetta shell.

The launcher diagnoses prerequisites but does not install or reconfigure
Docker, WSL2, GPU drivers, or the NVIDIA Container Toolkit.

### macOS and Linux

```bash
cp .env.example .env
# Edit .env if you want Salesforce sync, search, or external-development mode.

./techsara up --dry-run
./techsara up
```

Open <http://localhost:3000>. The orchestrator health endpoint is available at
<http://localhost:8080/health>.

### Windows

PowerShell:

```powershell
Copy-Item .env.example .env
.\techsara.ps1 up --dry-run
.\techsara.ps1 up
```

Command Prompt:

```bat
copy .env.example .env
techsara.cmd up --dry-run
techsara.cmd up
```

`techsara.cmd` delegates to the PowerShell bootstrap so Windows has one
implementation path.

The POSIX and PowerShell wrappers bootstrap a pinned `uv` binary when it is not
already available in TechSara's shared runtime directory. Installer artifacts
are SHA-256 verified before execution.

## What is selected

Selection is deterministic and conservative. Automatic selection may
downshift a profile when currently available system memory, per-device/free
VRAM, Docker GPU readiness, or aggregate download disk requirements make the
nominal tier unsafe. No generic profile assumes that two GPUs behave like one
contiguous-memory device.

| Host tier | Runtime | Main model policy | Optional capabilities |
|---|---|---|---|
| Apple Silicon 16–24 GiB | Native vLLM-Metal | Qwen3 8B MLX 4-bit | text only |
| Apple Silicon 32–47 GiB | Native vLLM-Metal | Qwen3 14B MLX 4-bit | embeddings |
| Apple Silicon 48–79 GiB | Native vLLM-Metal | Qwen3.6 35B MLX 4-bit | embeddings, reranking |
| Apple Silicon 80–127 GiB | Native vLLM-Metal | Qwen3.6 35B MLX 4-bit | embeddings, reranking, higher concurrency |
| Apple Silicon 128+ GiB | Native vLLM-Metal | Qwen3.6 35B MLX 6-bit | embeddings, reranking |
| NVIDIA DGX Spark | CUDA vLLM containers | Qwen3.6 35B NVFP4 | separate vision router, embeddings, reranking, OCR |
| NVIDIA, at least 70 GiB free per usable device | CUDA vLLM containers | Qwen3.6 35B FP8 | vision, embeddings, reranking |
| NVIDIA, at least 40 GiB free per usable device | CUDA vLLM containers | Qwen3 30B FP8 | embeddings |
| NVIDIA, at least 20 GiB free per usable device | CUDA vLLM containers | Qwen3 14B AWQ | embeddings |
| NVIDIA, at least 10 GiB free per usable device | CUDA vLLM containers | Qwen3 8B AWQ | text only |
| CPU-only, nominally 8+ GiB system memory | llama.cpp container | Qwen3 0.6B GGUF Q8 | minimal local chat |
| No safe local fit | Application-only | no local model | UI and data services only |
| Explicit local endpoint | External development | user-supplied local OpenAI-compatible server | declared capabilities only |

Exact model IDs, immutable revisions, memory/download estimates, contexts,
features, required files, licenses, and runtime artifacts live in
[`config/model-manifest.yaml`](config/model-manifest.yaml). Profile composition
lives in [`config/hardware-profiles.yaml`](config/hardware-profiles.yaml).
The rows are candidate tiers; final available-memory reserves can still
downshift CPU/GPU/Mac selection or choose app-only.

Two consequences of applying the budget honestly rather than by name:

- **A 16 GiB Mac selects app-only, not the 16–24 GiB tier.** The smallest Mac
  candidate needs about 7.6 GiB resident, and macOS plus the Docker Desktop VM
  running PostgreSQL, the orchestrator, the frontend, and the sync worker needs
  the rest. Running both would swap. The tier becomes reachable from roughly
  24 GiB. `status` prints the exact shortfall, and the external-development
  profile still lets a 16 GiB Mac use a model server it already runs.
- **A GPU below a CUDA tier's minimum falls back to the CPU tier, not to
  app-only.** An 8 GiB card is under the smallest CUDA model's own declared
  minimum, so the machine serves the CPU model instead and never touches the
  device.

No model is selected onto hardware below the `minimum_memory_bytes` its own
manifest entry declares — measured against device memory on NVIDIA and against
the unified/system pool everywhere else.

### Important macOS runtime boundary

Docker Model Runner's vLLM backend is unavailable on macOS. TechSara therefore
does not treat Docker Model Runner presence as usable vLLM acceleration on a
Mac. Apple Silicon uses the pinned native vLLM-Metal runtime and authenticated
loopback bridges into the application containers.

The current pinned vLLM-Metal path is text-first. Embeddings and reranking are
enabled only on profiles that select them and must pass live probes. Vision and
OCR are intentionally disabled on all Mac profiles until a pinned runtime
passes a stable image/OCR contract.

## Launcher commands

```text
./techsara up [--dry-run] [--profile ID] [--model HF_ID] [--skip-ocr] [--offline] [--verbose]
./techsara down [--dry-run]
./techsara restart [up options]
./techsara status
./techsara doctor [--offline]
./techsara logs [--tail N] [--service NAME]
./techsara models
./techsara redetect
./techsara update-models [--offline] [--dry-run]
```

Key behavior:

- `up --dry-run` performs detection, selection, planned model/runtime checks,
  and `docker compose config --quiet` using automatically removed temporary env
  files. It does not create persistent runtime state, install runtimes,
  download models, start services, or write launcher state.
  On a clean host, the wrapper may first install checksum-verified uv and its
  managed Python; `--offline` forbids that bootstrap.
- `--offline` forbids downloads. Startup succeeds only when every required
  pinned artifact is already complete.
- `--verbose` prints the major detection, selection, validation, reconciliation,
  and startup phases without exposing secret values.
- `--profile` accepts a declared profile only when compatible with detected
  hardware and runtime prerequisites.
- `--model` accepts only a revision-pinned manifest entry with a backend
  compatible with the selected profile; it is not an arbitrary repository or
  shell argument.
- `down` stops the launcher-managed Compose project and project-owned native
  processes. It deliberately omits `--volumes` and preserves models, runtimes,
  databases, reports, configuration, and shared caches. It never stops a
  container it did not create: if `sf-local-ai` containers started from another
  Compose file are still running, it lists them and prints the command that
  stops them rather than reporting success.
- `status` is read-only. It reports detected hardware, the selected profile and
  models, the installed pinned runtime, model-cache state, owned native PIDs
  with ports and health, Docker services, live orchestrator/frontend/native
  endpoint health, per-feature capability state, recorded capability probes, and
  every degraded reason.
- `doctor` runs non-destructive checks across Docker/Compose, architecture,
  total and available memory, disk, GPU/Metal prerequisites, model cache, the
  model manifest, environment and permissions, artifact-host reachability, the
  resolved Compose configuration, live endpoints, project-owned native
  listeners, and — on native macOS profiles — real container-to-host model
  reachability. It exits non-zero only for genuinely blocking prerequisites and
  prints exact remediation for everything else. `--offline` skips the network
  checks. `logs` redacts known secret values.
- Health probes and printed URLs follow `FRONTEND_PORT`/`ORCHESTRATOR_PORT` and
  the configured bind address instead of assuming 3000/8080.

## Startup and graceful degradation

The launcher starts and verifies the platform in stages:

1. Validate the generated Compose model.
2. Build the orchestrator, sync-worker, and frontend images.
3. Start PostgreSQL and optional search, then wait for readiness.
4. On NVIDIA, start/probe embeddings, then the DGX separate router, then DGX
   OCR. A failed optional embedding/OCR service is stopped and disabled; a
   failed router falls back to main.
5. Start and probe the main model. A failed first start gets one retry at the
   profile's next safer context with concurrency reduced to one.
6. Probe declared vision behavior and disable that optional capability if it
   fails.
7. Start the orchestrator and validate its health contract.
8. Start Salesforce sync when credentials are complete, independently of
   whether embeddings are available. Warehouse sync can continue while vector
   indexing is degraded.
9. Start the frontend and optional pgAdmin service.

There is no automatic cloud fallback. `external-development` accepts explicit
local or container-network URLs only; public cloud URLs are rejected by the
launcher.

## Configuration ownership

Copy [`.env.example`](.env.example) to `.env` and edit only user-owned settings:
Salesforce credentials, optional search/API keys, feature limits, ports, and
explicit external-development endpoints. Search providers are validated as
`searxng`, `tavily`, or `brave`; only the local SearXNG selection starts the
SearXNG Compose service automatically.

The launcher owns `.runtime/generated.env` and `.runtime/secrets.env`:

- `.runtime/generated.env` is non-secret, mode `0644`, and contains the selected
  model endpoints, container paths, contexts, and capability flags.
- `.runtime/secrets.env` is mode `0600` and holds generated local credentials
  plus native bridge API aliases.
- Compose precedence is `.env` → `secrets.env` → `generated.env`; generated
  selection values win even when the invoking shell exported stale variables.

Do not copy launcher-managed model URLs, model IDs, container paths, or
capability flags back into `.env`. `./techsara redetect` refreshes only stored
hardware/profile selection; run `./techsara up` to regenerate the runtime env.

Salesforce sync is optional. Set `SF_CLIENT_ID`, `SF_USERNAME`,
`SF_LOGIN_URL`, and either `SF_CLIENT_SECRET`, `SF_PRIVATE_KEY_B64`, or
`SF_PRIVATE_KEY_HOST_FILE`. With no complete credential set, the local UI and
analytics services still start and status reports sync as unavailable.

## First run, downloads, and offline reuse

The first non-dry `up` can download a large model set. Manifest estimates range
from about 0.6 GB for CPU-minimal to more than 40 GB for the full DGX set,
before temporary staging allowance. The launcher checks all missing models as a
group using 120% of each declared download plus 1 GiB shared headroom, then
checks each model again before downloading.

Downloads are revision-qualified and sequential. An interruption leaves the
`.partial` directory for the Hugging Face client to resume; completed revisions
are reused without downloading. Put an optional `HF_TOKEN` in `.env` for
gated/private repositories. The launcher tries anonymously first, then retries
once with the token; an exported `HF_TOKEN` takes precedence.

Default model locations are:

- macOS: `~/Library/Caches/TechSara/models`;
- Linux: `${XDG_CACHE_HOME:-~/.cache}/techsara/models`;
- Windows: `%LOCALAPPDATA%\TechSara\models`.

Set `TECHSARA_MODEL_CACHE` in the shell before starting the wrapper to move the
cache; detection occurs before project `.env` is parsed. `TECHSARA_HOME`, also
a shell setting, controls the shared uv, managed Python, download, and native
runtime root.

After one complete online start, `./techsara up --offline` reuses the exact
cached artifacts. It fails with a cache-miss message instead of accessing the
network. `update-models` ensures the currently selected manifest revisions; it
does not discover or rewrite newer upstream revisions.

## State, caches, and updates

Project-local launcher state is under `.runtime/`:

```text
.runtime/
  hardware.json
  selected-profile.json
  generated.env
  secrets.env
  state.json
  capabilities.json
  locks/
  logs/
  pids/
```

Shared launcher assets default to `${TECHSARA_HOME:-~/.techsara}`. Model cache
defaults are platform-appropriate and may be overridden with
`TECHSARA_MODEL_CACHE`. The launcher can reuse a compatible legacy sibling
`vllm_models` directory, but the repository contains no machine-specific
absolute model path.

Model downloads are revision-pinned, resumable through staging directories,
and committed only after required-file validation. The model set is checked as
a group for free disk before downloads start. A completion marker records the
selected revision. Some manifest entries describe required files rather than
per-file SHA-256 values; revision pinning and required-file checks are not the
same guarantee as hashing every weight file.

The native vLLM-Metal runtime verifies the direct vLLM source artifact and
vLLM-Metal wheel hashes before installation. Current fallback installation can
still resolve transitive Python dependencies without a complete hash-locked
closure. Treat the recorded installed package set as audit evidence, not as a
fully reproducible transitive lock.

## Security boundary

- Frontend, orchestrator, PostgreSQL, and optional pgAdmin publications bind to
  `127.0.0.1` by default. Model containers use `expose` on an internal inference
  network and do not publish host ports.
- Mac native model upstreams bind to loopback. Container-facing bridge
  listeners bind host `0.0.0.0` on 18100/18103/18105 so Docker can reach them;
  they require a launcher-generated bearer token, bound request bodies, and do
  not forward that token to the upstream model process.
- Published ports default to loopback. `TECHSARA_BIND_ADDRESS` in `.env` opts
  into a wider address (literal IP only; anything else is rejected rather than
  guessed). Model APIs are additionally gated behind `PUBLISH_MODEL_PORTS`,
  because they are unauthenticated. PostgreSQL and pgAdmin stay on `127.0.0.1`
  regardless of either setting.
- Generated configuration is secret-free; local secrets are kept in a
  user-readable-only file. Logs and command errors redact known values.
- Model/cache/runtime paths are containment-checked, and managed symlink roots
  that would escape those directories are rejected.

There is no application login/session boundary; the UI uses one stable local
identity. Loopback binding and the host firewall are therefore security
assumptions, not substitutes for authentication. Any reverse proxy or
non-loopback publication creates a new trust boundary and needs deliberate
authentication, TLS, and access-control review.

## Compose layout

The launcher always combines [`compose.yaml`](compose.yaml) with one runtime
overlay:

- `compose/compose.mac.yaml`
- `compose/compose.dgx-spark.yaml`
- `compose/compose.nvidia.yaml`
- `compose/compose.cpu.yaml`
- `compose/compose.external-development.yaml`
- plus `compose/compose.windows-wsl2.yaml` for validated Windows NVIDIA hosts

The named volumes are `sf-local-ai_data`, `sf-local-ai_reports`,
`sf-local-ai_pgdata`, `sf-local-ai_pgadmin`, and `sf-local-ai_hf-cache`.

Every image used by these files is pinned by immutable digest, including the
base image of each of the four build Dockerfiles.

The root `docker-compose.yml` and `orchestrator/Dockerfile` are retained,
unchanged apart from a `SUPERSEDED` header, as the pre-launcher DGX-era
artifacts. They exist so a stack started from them can still be stopped with
them and so a launcher-managed start can be rolled back. They are not portable:
they hard-code one developer's model directory, use floating `:nightly`/
`:latest` tags, and publish model APIs on `0.0.0.0`. Directly invoking Compose
also skips hardware selection, model validation, secret generation, staged
health checks, fallback behavior, and ownership-safe native lifecycle
management.

## Troubleshooting

Start with:

```bash
./techsara doctor
./techsara status
./techsara logs --tail 300
./techsara up --dry-run --verbose
```

Common cases:

- **Pinned uv/Python missing offline:** run once without `--offline` on a
  networked host, or pre-populate the pinned artifacts under `TECHSARA_HOME`.
- **"not allowed to use its socket":** the daemon is running and answering, but
  this account may not open it. `./techsara doctor` prints the exact fix for the
  host, which on Linux is usually `sudo usermod -aG docker "$USER"` followed by
  `newgrp docker`. `newgrp` is required because group changes do not apply to a
  login session that started before them, so `usermod` alone leaves the very
  next `./techsara up` failing in the same shell.
- **Docker check fails:** start the daemon, enable Linux containers, and update
  Compose to v2.24+. Windows CUDA also needs WSL2; NVIDIA hosts need a passing
  Docker GPU smoke test, not only a working host driver. The probe prefers an
  already-cached runtime image and otherwise pulls one digest-pinned ~4 MiB
  image; set `TECHSARA_GPU_SMOKE_IMAGE` in the shell to use your own.
- **A smaller profile or app-only was selected:** check available—not merely
  total—RAM/VRAM, per-device free VRAM, Docker GPU readiness, and the degraded
  reasons printed by `--verbose`/`status`.
- **Insufficient disk:** move the cache with shell-level
  `TECHSARA_MODEL_CACHE`, or free the aggregate estimate plus staging room.
- **Gated/private download fails:** add `HF_TOKEN` to `.env` or export it.
  Partial data is preserved for resume and secret text is redacted.
- **Model/runtime is invalid:** it is deliberately preserved. Inspect it, move
  that exact revision/runtime directory aside manually, and rerun; the launcher
  does not overwrite ambiguous data.
- **Mac has no native inference:** use a native arm64 terminal. Rosetta and
  Intel macOS cannot run the pinned vLLM-Metal profile. Docker Model Runner
  vLLM is not a Mac substitute.
- **Port already in use:** stop the unrelated listener or choose another user
  application port. The launcher never replaces an unowned native PID.
- **Embedding/OCR/vision is disabled:** inspect logs and degraded reasons.
  These roles are optional; Mac vision/OCR are intentionally off. A failed
  router uses main.
- **Salesforce sync does not start:** provide `SF_CLIENT_ID`, `SF_USERNAME`,
  `SF_LOGIN_URL`, and one supported secret/private-key assertion. Embedding
  health does not gate warehouse sync.
- **After a Mac reboot:** native processes are not OS services. Run
  `./techsara up` again.

### Remove the launcher while preserving data

Run `./techsara down` first. Back up `.env` and `.runtime/` if you will remove
the working copy. Do not use `docker compose down -v`, `docker volume rm`, or a
volume-prune command: the `sf-local-ai_data`, `reports`, `pgdata`, `pgadmin`,
and `hf-cache` volumes are the persistent data boundary.

Removing the repository/wrappers does not remove those named volumes, the
platform model cache, or `${TECHSARA_HOME:-~/.techsara}`. Keep those paths to
preserve model/runtime reuse. A later checkout can reconnect after
configuration is restored. A full purge is a separate destructive operation
and should happen only after verified backups.

## Development and tests

Launcher tests need only the Python standard library:

```bash
PYTHONPATH=launcher python3 -m pytest launcher/tests -q
# or, without pytest:
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s launcher/tests -v
```

Application suites:

```bash
cd orchestrator && TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/techsara_test \
    python3 -m pytest tests -q     # needs a reachable PostgreSQL; see tests/conftest.py
cd sync-worker && python3 -m pytest tests -q
cd frontend && npm test
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
```

All of the above were run on 2026-08-11 on a DGX Spark host:

| Suite | Result |
|---|---|
| Launcher | 273 passed, 285 subtests |
| Orchestrator | 1232 passed |
| Sync worker | 157 passed |
| Frontend | 425 passed (30 files) |
| Frontend types / lint / build | clean |

The 262,144-token window was verified end to end on the same host with
`orchestrator/scripts/validate_long_context.py`: 239,938 input tokens accepted
and fully recalled, with needles at the start, middle and end of the prompt
(64K / 128K / 200K / 240K all 3 of 3).

Most launcher tests use mocks and temporary directories and never touch live
Docker, the network, model servers, or real process signals. The one exception
is `launcher/tests/test_compose_overlays.py`, which renders every supported host
fixture through real `docker compose config` to prove each overlay resolves and
keeps its platform invariants; it skips when Docker Compose v2.24+ is absent and
starts, pulls, and creates nothing. See
[`docs/01-codebase/test-map.md`](docs/01-codebase/test-map.md) for scope and
configuration.

## Documentation

- [Portable runtime](docs/PORTABLE-RUNTIME.md)
- [Repository inventory](docs/00-INVENTORY.md)
- [Critical paths](docs/01-codebase/CRITICAL-PATHS.md)
- [Salesforce Intelligence Mode](docs/06-agent-design/SALESFORCE-INTELLIGENCE-MODE.md)
  — contextual clarification, the query-plan compiler, context budgeting
- [Compose and infrastructure](docs/01-codebase/infra-docker-compose.md)
- [Test map](docs/01-codebase/test-map.md)
- [Architecture documentation index](docs/README.md)
- [Changelog](CHANGELOG.md)
