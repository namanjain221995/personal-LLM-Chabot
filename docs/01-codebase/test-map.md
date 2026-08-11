# Test map

This page inventories the current test entrypoints and configuration. Every
result below was produced by a command actually run on 2026-08-11 on the DGX
Spark host.

## 1. Current verification status

| Suite | Runner | Current test files | Result |
|---|---|---:|---|
| Portable launcher | pytest / stdlib `unittest` | 10 `test_*.py` modules | **264 passed, 266 subtests** |
| Orchestrator | pytest | 68 `test_*.py` modules | **1226 passed** |
| Sync worker | pytest | 14 `test_*.py` modules | **157 passed** |
| Frontend | Vitest | 30 modules (`*.test.ts` + `*.test.tsx`) | **425 passed** |
| Frontend types | `tsc --noEmit` | — | clean |
| Frontend lint | `next lint` | — | no warnings or errors |

Exact commands:

```bash
PYTHONPATH=launcher python3 -m pytest launcher/tests -q
# or, without pytest:
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s launcher/tests -v

cd orchestrator && TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/techsara_test \
    python3 -m pytest tests -q
cd sync-worker && python3 -m pytest tests -q
cd frontend && npm test && npx tsc --noEmit && npm run lint
```

The orchestrator suite requires a reachable PostgreSQL server; see
`orchestrator/tests/conftest.py` for the resolution order and the guard that
refuses any database name that is not unmistakably test-only.

Most launcher tests are isolated unit/contract tests. They use temporary
directories, constructed hardware fixtures, fake command runners, mocked HTTP,
and patched lifecycle boundaries; they do not build/pull/start Docker, download
models or runtimes, access live external services, start real model servers, or
signal real processes.

`test_compose_overlays.py` is the deliberate exception: it shells out to real
`docker compose config` for all 13 supported host fixtures, with every optional
profile enabled, and asserts the platform invariants (device reservations,
orchestrator image family, loopback-only publication, no published model API,
retained volumes, digest-pinned images including every Dockerfile `FROM`, no
developer home directory, and generated-environment delivery to the application
containers). `config` only resolves and renders — it starts, pulls, and creates
nothing — and the whole module skips when Docker Compose v2.24+ is unavailable.

## 2. Launcher suite

There is no third-party launcher test dependency. `launcher/tests/support.py`
provides constructed `HardwareInfo`, model/profile fixtures, fake command
results, temporary project/config layouts, completion markers, and helpers.

Static `test_*` method counts below describe source definitions; parameterized
subtests and dynamic cases may make runner accounting differ.

| Module | Definitions | Primary contracts |
|---|---:|---|
| `test_hardware.py` | 31 | Apple M5/memory tiers, Intel/Rosetta, Linux NVIDIA, DGX, CPU, Windows/WSL2, Docker state, tiered container GPU probe (cached-image reuse, clean-clone tiny-image pull, offline, failed pull), free VRAM, malformed detector output, cache defaults/overrides |
| `test_profiles.py` | 31 | every declared tier, deterministic selection, reserve budgets, context downshift, low available RAM, free/per-device VRAM, multi-GPU non-aggregation, disk safety, manual override validation, app-only degradation |
| `test_model_manager.py` | 25 | pinned destinations, complete/partial/legacy markers, resume, aggregate disk preflight, offline, token retry/redaction, required files/checksums, idempotency, unsafe path/symlink refusal |
| `test_runtime.py` | 46 | native arm64/runtime marker validation, verified artifact argv, partial/invalid preservation, process ownership/health/version/identity/ports, strict model/capability response schemas |
| `test_environment.py` | 19 | runtime layout, local secret reuse/permissions, Salesforce credential gates, container-path containment, complete-only installs, context clamp, capability env, local external URL and search-provider policy |
| `test_cli.py` | 66 | command dispatch, status/doctor/logs/down safety, pure launcher dry-run, staged Compose and native startup, role-specific probes, optional degradation, router fallback, one main retry, sync independent of embeddings, saved-state traversal rejection, wrapper contracts, published-endpoint resolution, doctor check coverage and offline behavior |
| `test_bridge.py` | 10 | bearer authentication, header stripping, bounded request bodies, target path/query behavior, secret-free errors, no request logging |
| `test_compose.py` | 6 | env-file precedence over inherited managed vars, preservation of unrelated host env, command construction, orchestrator health and reasoning-probe contracts |
| `test_utils.py` | 23 | argv-without-shell, validation, dotenv non-evaluation, redaction, atomic files/modes, live-owner locks, stale locks, resumable/hash-verified direct downloads, archive extraction safety |
| `test_compose_overlays.py` | 6 | real `docker compose config` for all 13 host fixtures with every optional profile, platform invariants, digest-pinned images and Dockerfile bases, no developer home directory, generated-environment delivery |

### Hardware fixtures represented

- Apple Silicon M5 and generic 16–24, 32–47, 48–79, 80–127, and 128+ GiB
  tiers;
- native arm64, Intel Mac, and Rosetta execution;
- DGX Spark and generic NVIDIA 8/20/40/70+ GiB free-VRAM tiers;
- low-free-memory GPU, multi-GPU fragmentation, unknown/malformed GPU data,
  driver-present but Docker-GPU-unavailable cases;
- Linux CPU/no-GPU and low-RAM app-only fallback;
- Windows WSL1 false-positive resistance, WSL2/Linux-container/GPU gates;
- Docker absent, daemon stopped, old/missing Compose, non-Linux containers;
- insufficient disk and aggregate multi-model staging requirements.

### Safety properties represented

- arbitrary profile names, model IDs, revisions, URLs, paths, and shell
  fragments do not cross command boundaries;
- only manifest-pinned backend-compatible model overrides are accepted;
- dry-run does not create persistent launcher state or invoke live lifecycle;
- generated env is secret-free, private env is mode `0600`, and exported
  managed variables cannot override the declared file chain;
- model/runtime roots and saved Compose paths reject traversal and symlink
  escapes;
- partial and invalid installs are preserved; complete installs are
  idempotently reused;
- a stale PID record cannot signal a reused/unowned process, including identity
  change immediately before `SIGTERM`;
- HTTP 2xx is insufficient for capability success without the required JSON or
  SSE shape;
- optional embedding/OCR failures degrade, router failure shares main, and main
  receives at most one safer retry;
- Salesforce sync starts whenever credentials are ready, even when embeddings
  are disabled/degraded;
- top-level bearer material and known secrets are redacted from failures.

### What still needs live integration coverage

The unit suite does not prove that a particular host can pull images, run GPU
containers, install the native dependency closure, load every selected model,
or pass end-to-end browser traffic. A release qualification should exercise at
least one machine in each supported runtime family and record:

1. wrapper bootstrap on a clean host and `up --dry-run`;
2. real `docker compose config --quiet` for every base/overlay combination;
3. first install, interrupted/resumed model download, offline reuse, and down/up
   idempotency;
4. live model health plus capability probes;
5. optional-component failure, router fallback, and main-context retry;
6. reboot behavior (native Mac processes require `techsara up` again);
7. data/volume preservation across `down` and upgrade;
8. Windows WSL2 and NVIDIA container access on an actual Windows host.

## 3. Orchestrator suite

Configuration:

- Runner: pytest 8+, declared in `orchestrator/requirements-dev.txt`.
- Test path: `orchestrator/tests/` (64 `test_*.py` modules).
- Import bootstrap: `orchestrator/conftest.py` and
  `orchestrator/tests/conftest.py`.
- There is no repository `pytest.ini`, `pyproject.toml`, `tox.ini`, or central
  coverage configuration.
- Development requirements intentionally omit heavy/lazy `transformers` and
  `weasyprint`; source tests mock or avoid those runtime paths.

Run from a prepared development environment:

```bash
cd orchestrator
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Major coverage areas by module group:

| Group | Representative test modules |
|---|---|
| HTTP, SSE, auth/local identity, health | `test_endpoints`, `test_sse*`, `test_auth`, `test_live_generation`, `test_test_database_guard` |
| Conversation/history/PostgreSQL | `test_history*`, `test_conversation_integrity`, `test_message_feedback`, `test_titling`, `test_recall_db` |
| LLM routing/capabilities | `test_llm_clients`, `test_chat_modes`, `test_router*`, `test_orchestrate`, `test_model_capability_layer`, `test_effort_depth` |
| Salesforce/SQL/RAG | `test_live_salesforce`, `test_salesforce_toggle`, `test_sql_*`, `test_rag`-adjacent recall/embedding tests, `test_schema_grounding` |
| Search/URL/repository | `test_search_*`, `test_url*`, `test_repo`, `test_net_ssrf`, `test_agent_web_step` |
| Uploads/documents/reports/charts | `test_archive_safety`, `test_dataset_profile`, `test_full_documents`, `test_report_*`, `test_chart*`, `test_exports` |
| Context/memory | `test_context_budget`, `test_compaction`, `test_memory_recall`, `test_recall` |

Some integration-flavored tests use local temporary PostgreSQL/DuckDB/filesystem
state or intentionally probe a closed loopback port. Read individual modules
before assuming the suite is entirely pure unit work.

## 4. Sync-worker suite

Configuration:

- Runner: pytest `>=8,<9`, declared by `sync-worker/requirements-dev.txt` on top
  of runtime requirements.
- Test path: `sync-worker/tests/` (14 `test_*.py` modules).
- Import bootstrap: `sync-worker/conftest.py`.
- No dedicated pytest or coverage configuration.

Run:

```bash
cd sync-worker
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Coverage includes Salesforce secret formats/JWT behavior, discovery and object
configuration, limits/chunking, Bulk API fallback, watermark/upsert/delete,
empty tables, embedding response failures, and embedding-model/dimension/index
compatibility.

The old documentation command that mounted tests into an already-running
Compose service is not the canonical portable command: it omits the selected
overlay/env chain and depends on runtime image state. Use the host development
environment above, or construct the saved launcher Compose command deliberately
for an integration run.

## 5. Frontend suite

Configuration:

- Runner: Vitest `^3.2.0`.
- Config: `frontend/vitest.config.mts`.
- Include: `tests/**/*.test.ts` and `tests/**/*.test.tsx` (30 modules).
- Environment: Node by default. Component tests opt into jsdom with a
  `// @vitest-environment jsdom` docblock, so the node default stays cheap for
  the pure-logic modules that are most of the suite. Added 2026-08-11 with
  Salesforce Intelligence Mode, for the behaviour that only exists in a DOM:
  focus movement, roving tabindex, ARIA wiring and the double-click guard.
- Exact dependency graph: `frontend/package-lock.json`; use `npm ci` for a
  clean reproducible install.

Run:

```bash
cd frontend
npm ci
npm test
npx tsc --noEmit
npm run build
```

The `lint` script delegates to `next lint`; with the current Next.js toolchain,
verify that command separately rather than treating its presence in
`package.json` as proof that lint ran.

The Vitest modules cover SSE/stream state, API contracts, history cache/server
sync/proxy route classification/title generation/feedback, IndexedDB fallback,
preferences and composer menus, attachments/pasted content, citations,
research/web search, errors, export, Mermaid, and chart options. They do not
mount React components in a browser DOM and are not an end-to-end UI suite.

## 6. Manifests, locks, and test reproducibility

| Area | Dependency source | Reproducibility boundary |
|---|---|---|
| Launcher | Python standard library; pinned bootstrap/runtime data in `config/model-manifest.yaml` | no test dependency install |
| Orchestrator | `requirements.txt`, `requirements-dev.txt` ranges | no generated Python lock or hashes |
| Sync worker | `requirements.txt`, `requirements-dev.txt` bounded ranges | no generated Python lock or hashes |
| Frontend | `package.json`, `package-lock.json` | npm lockfile present |
| Native Mac runtime | direct source/wheel hashes and `config/vllm-metal-runtime.txt` | fallback may resolve unhashed transitive dependencies; freeze recorded after install |
| Containers | digest pins for core service/runtime images; application builds install dependency ranges | base identity pinned, application package closure not fully locked |

## 7. CI status

No repository CI workflow currently runs these four suites together. Until one
is added, release notes should list commands, dates, environments, and results
separately. Do not combine historical application counts with a newer launcher
run into a synthetic all-green total.
