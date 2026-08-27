# TechSara Local AI Platform

**A local-first, private AI analyst for a Salesforce org.** One command starts a
chat UI, an orchestrator that turns questions into SQL / SOQL / retrieval /
web research / reports, a PostgreSQL application database, a DuckDB + LanceDB
analytics plane kept in sync with Salesforce, and a local inference runtime
chosen for the machine it runs on — a Mac, a single NVIDIA GPU, a CPU-only
box, or two NVIDIA DGX Sparks working as one model.

Nothing leaves the machine: models run locally, Salesforce data is mirrored
locally, and web search (optional) goes through a self-hosted SearXNG.

```bash
cp .env.example .env      # add Salesforce credentials if you want the CRM synced
./techsara up             # detects the hardware, picks models, starts everything
open http://localhost:3000
```

> This README is the map of the whole repository. Every section names the
> files it describes. Deep dives live in [`docs/`](docs/README.md); the
> history of every change, with the reasoning and the numbers, is in
> [`CHANGELOG.md`](CHANGELOG.md) (63 dated entries).

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Repository map](#3-repository-map)
4. [Quick start](#4-quick-start)
5. [Hardware profiles and models](#5-hardware-profiles-and-models)
6. [The launcher (`./techsara`)](#6-the-launcher-techsara)
7. [Two-node DGX Spark cluster](#7-two-node-dgx-spark-cluster)
8. [Services, ports, networks, volumes](#8-services-ports-networks-volumes)
9. [Configuration reference (`.env`)](#9-configuration-reference-env)
10. [How a chat message is answered](#10-how-a-chat-message-is-answered)
11. [The engines](#11-the-engines)
12. [Salesforce: sync, warehouse, live queries](#12-salesforce-sync-warehouse-live-queries)
13. [The Salesforce Brain (knowledge packs)](#13-the-salesforce-brain-knowledge-packs)
14. [Salesforce Intelligence Mode](#14-salesforce-intelligence-mode)
15. [Memory, context and compaction](#15-memory-context-and-compaction)
16. [Charts, reports, uploads, documents, web, repos](#16-charts-reports-uploads-documents-web-repos)
17. [Data stores](#17-data-stores)
18. [Frontend](#18-frontend)
19. [Orchestrator API reference](#19-orchestrator-api-reference)
20. [Security model](#20-security-model)
21. [Operations](#21-operations)
22. [Development and tests](#22-development-and-tests)
23. [Documentation index](#23-documentation-index)
24. [Known limitations and drift](#24-known-limitations-and-drift)

---

## 1. What it does

A user opens the chat, optionally flips the **Salesforce** pill, and asks in
plain language. The platform:

| Ask | What happens | Where |
|---|---|---|
| "How many interviews were scheduled last week per recruiter?" | Writes **one guarded `SELECT`** against the locally synced warehouse (DuckDB), runs it, computes authoritative totals in Python, streams a narrative, shows the SQL, the rows, a CSV/XLSX export and — when it fits — a chart | `orchestrator/app/engines/sql.py` |
| "What does the onboarding SOP say about the welcome call?" | Retrieves **long-text fields and knowledge packs** (LanceDB + the Brain), reranks, answers with record citations | `engines/rag.py`, `core/brain.py` |
| "Show me Jayesh's latest status" (ambiguous) | **Salesforce Intelligence Mode** resolves the request, asks *one* targeted clarifying question if needed, compiles a validated query plan (never free-form SOQL), runs it, resumes the original request after the answer | `engines/sf_intel.py`, `core/sf_intel/` |
| Toggle **Live Salesforce** | Queries the org directly (read-only integration user); live values win when merged with warehouse rows | `engines/live_sf.py` |
| Attach a PDF/DOCX, images, a CSV/XLSX/ZIP dataset | Reads **every page** (OCR sidecar for scanned pages), answers about uploaded data from a safe profile or the full table when small, generates real PDF/DOCX reports | `engines/document.py`, `dataset.py`, `dataset_report.py` |
| Paste a URL or a GitHub repo link | Fetches the pages (SSRF-safe) or shallow-clones the repo (code never executed) and answers with sources / `path:Lstart-Lend` citations | `engines/url.py`, `repo.py` |
| Turn on **Web search** (assistant mode) | Multi-query search through SearXNG/Tavily/Brave, reads the sources, cites them; at *Max* effort runs deep research | `engines/search.py` |
| "Write a report on Q3 placements" | Plans sections, fills them with SQL/RAG, renders charts, emits `.docx` + `.pdf` | `engines/report.py` |
| Thumbs up on a SQL answer | That SQL becomes a **few-shot example** for similar future questions; a thumbs-down anywhere disqualifies it globally | `core/learned_examples.py` |
| Chat across sessions | **Cross-chat memory**: durable facts extracted in the background, semantic + keyword recall over earlier conversations, rolling compaction so long threads never overflow the model window | `facts.py`, `memory_semantic.py`, `compaction.py` |

UI features (all in [`frontend/`](frontend/)): ChatGPT-style shell with sidebar
(pinned / recent / archived), search palette (`Ctrl/Cmd+K`), effort picker
(**Fast / Think / Max**), `+` menu (files, web search, Salesforce, live
Salesforce), paste-as-chip for long text, reasoning accordion, agent step
timeline, activity/research drawer, proof drawer (SQL · sources · code · data ·
chart · files), nine chart types (ECharts), Mermaid diagrams with zoom and PNG
export, context meter with **Compact now**, background generation that
survives reloads and re-attaches from any tab, Markdown export, dark/light
themes, mobile layout.

---

## 2. Architecture

```text
                    ┌──────────────────────────────────────────────────────────────┐
  browser  ───────► │  frontend  (Next.js 16, :3000)                                │
                    │  one page (/) + /api/* proxies — the browser never sees       │
                    │  the orchestrator or any model endpoint                       │
                    └───────────────────────────┬──────────────────────────────────┘
                                                │ SSE
                    ┌───────────────────────────▼──────────────────────────────────┐
                    │  orchestrator  (FastAPI 0.2.0, :8080)                         │
                    │  /chat → gates → memory → compaction → SF Intelligence →      │
                    │  engine chain (vision · document · repo · url · agent ·       │
                    │  search · dataset · chat · live SF · LangGraph router →       │
                    │  sql | rag | vision | report | chat)                          │
                    └──┬──────────────┬─────────────────┬──────────────────────────┘
                       │              │                 │
     ┌─────────────────▼──┐   ┌───────▼────────┐   ┌────▼──────────────────────────┐
     │ PostgreSQL (app     │   │ data volume    │   │ inference network (internal)  │
     │ state, schema v7)   │   │ /data/         │   │  vllm        main model        │
     │ users, conversations│   │  warehouse.duckdb   vllm-router  routing/fast     │
     │ messages, memory,   │   │  lancedb/      │   │  vllm-embed  embeddings        │
     │ SF intelligence     │   │  parquet/      │   │  vllm-reranker / vllm-ocr      │
     └─────────────────────┘   │  brain/ (packs)│   │  (or llama-cpp on CPU, or      │
                               │  workspaces/   │   │   native vLLM-Metal on a Mac)  │
     ┌─────────────────────┐   └───────▲────────┘   └────────────────────────────────┘
     │ sync-worker         │           │
     │ Salesforce → Parquet│───────────┘          searxng (optional, internal)
     │ → DuckDB → LanceDB  │                      pgadmin  (optional, 127.0.0.1:5050)
     └─────────────────────┘
```

Four application components, each its own Docker image built from this repo:

| Component | Directory | Stack | Size |
|---|---|---|---|
| Frontend | [`frontend/`](frontend/) | Next.js 16.3 (App Router), React 19, TypeScript 5.9 strict, Tailwind 3.4, ECharts 6, Mermaid 11, Vitest 3 | 84 TS/TSX files, ~15.8k lines |
| Orchestrator | [`orchestrator/`](orchestrator/) | FastAPI, LangGraph, OpenAI SDK (to vLLM), psycopg 3, DuckDB, LanceDB, pandas, matplotlib, pandoc + WeasyPrint | 86 Python files, ~26.8k lines |
| Sync worker | [`sync-worker/`](sync-worker/) | Python 3.11, httpx, PyJWT, pyarrow, DuckDB, LanceDB | 12 files, ~2.9k lines |
| Launcher | [`launcher/`](launcher/) + [`techsara`](techsara) | Python 3.12 standard library only, bootstrapped by a pinned `uv` | 13 files, ~6.2k lines |

Everything else is pinned third-party images: PostgreSQL, pgAdmin, SearXNG,
`vllm/vllm-openai`, `nvcr.io/nvidia/vllm`, `ghcr.io/ggml-org/llama.cpp` — all
referenced by immutable digest in [`compose.yaml`](compose.yaml) and
[`compose/`](compose/).

---

## 3. Repository map

```text
.
├── techsara / techsara.ps1 / techsara.cmd   the entrypoint (POSIX sh, PowerShell, CMD shim)
├── launcher/techsara_cli/      the launcher: hardware.py (detection), profiles.py (selection),
│                               model_manager.py (pinned downloads), environment.py (generated.env),
│                               compose.py (Compose driver), cluster.py (2-node DGX), runtime.py
│                               (native Mac vLLM-Metal), bridge.py (Mac loopback bridges), cli.py
├── launcher/tests/             355 stdlib tests
├── compose.yaml                platform-neutral base: postgres, pgadmin, searxng, orchestrator,
│                               sync-worker, frontend, networks, volumes
├── compose/                    one runtime overlay per host family + published-ports overlays +
│                               the two-node cluster overlay and the Node-2 worker file
├── config/hardware-profiles.yaml   13 profiles (Mac tiers, DGX Spark, NVIDIA tiers, CPU, app-only, external)
├── config/model-manifest.yaml      16 revision-pinned models, runtime pins (uv, vllm-metal, images)
├── config/vllm-metal-runtime.txt   lock input for the native Mac runtime
├── orchestrator/app/
│   ├── main.py                 FastAPI app, /chat SSE, detached generations, /health, /reports
│   ├── config.py               every environment variable with its default
│   ├── llm.py                  model clients, effort ladder, tool calling with fallbacks
│   ├── engines/                sql, rag, vision, document, ocr, report, chat, agent, search, url,
│   │                           repo, dataset, dataset_report, live_sf, sf_intel, router, orchestrate
│   ├── core/                   sql_guard, net (SSRF), archive (zip bombs), salesforce (SOQL guard),
│   │                           brain (packs), org_brief (grounding), sf_dictionary, schema_cache,
│   │                           chart_* (spec/decision/pipeline/profile/png), citations, exports,
│   │                           pdf/docx, report_render, learned_examples, best_of, clarify, profile
│   ├── core/sf_intel/          Salesforce Intelligence Mode: interpret → plan → validate → execute
│   ├── db.py                   PostgreSQL pool + 7 migrations
│   ├── memory*.py, facts.py, recall.py, compaction.py, summarize.py, titling.py
│   ├── history.py, uploads.py, memory_api.py, auth.py, health.py, sse.py, context.py
│   └── search/                 searxng, tavily, brave providers
├── orchestrator/scripts/       validate_packs.py, compile_brain_source.py, validate_long_context.py,
│                               build_dictionary_from_metadata.py, migrate_sqlite_to_postgres.py, backfill_titles.py
├── orchestrator/tests/         80 files, ~1,240 tests (needs a test PostgreSQL)
├── sync-worker/syncworker/     main.py (cycle), sf_auth.py (JWT / client credentials), sf_client.py
│                               (REST + Bulk 2.0), objects.py (config + CLI), storage.py (Parquet + DuckDB),
│                               rag_index.py + chunking.py + embedding_index.py (LanceDB), secrets.py
├── sync-worker/config.yaml     which objects/fields/rag_fields to sync (mounted live, read-only)
├── sync-worker/tests/          157 tests
├── frontend/app/               page.tsx (the app), layout.tsx, api/* proxy routes
├── frontend/components/        39 components (ChatApp, Composer, MessageRow, ProofDrawer, ChartView, ...)
├── frontend/lib/               32 headless modules (sse, streams, history, chartOption, clarification, ...)
├── frontend/tests/             32 files, 436 cases
├── brain/packs/                21 YAML knowledge packs the model reads on every Salesforce question
├── brain/sources/              the raw documents, SOPs, org schema and metadata they were compiled from
├── scripts/                    two-node cluster tooling: cluster-status/doctor/test/bench/logs/sync/worker/up/down
├── pgadmin/ searxng/           pre-registered pgAdmin server, SearXNG settings
├── docs/                       current runtime docs + a 2026-07-31 audit layer (see §23)
├── docker-compose.yml          SUPERSEDED pre-launcher DGX file, kept only to stop/roll back an old stack
├── screenshots/                60 UI evidence shots matching the changelog
├── PROJECT_DOCUMENTATION.txt   documentation of the Salesforce org application itself (Zoom portal, job requirements)
├── Training_Module_Feature_Map_and_Memory.txt, customer-success-*.txt, *.zip
│                               source material for brain packs (also under brain/sources/)
└── .runtime/  data/  chatgptdata/   git-ignored: launcher state, DuckDB backup, private ChatGPT export
```

---

## 4. Quick start

### Prerequisites

- Docker Engine or Docker Desktop with Linux containers, **Docker Compose v2.24+**.
- Linux: your account can open the Docker socket (`sudo usermod -aG docker "$USER"`, then `newgrp docker` or log in again). NVIDIA hosts need the **NVIDIA Container Toolkit** and a passing Docker GPU path (the launcher smoke-tests it).
- macOS: Apple Silicon, run from a **native arm64** terminal (not Rosetta). Docker Model Runner is *not* used; the launcher installs a pinned native vLLM-Metal runtime.
- Windows: WSL2 + Docker Desktop; NVIDIA needs a working WSL2 GPU path.
- `curl`/`wget` and `sha256sum`/`shasum` for the one-time, checksum-verified `uv` bootstrap.
- Disk for the selected model set (0.6 GB CPU-minimal … >40 GB for the DGX set) plus staging room.

The launcher diagnoses all of this (`./techsara doctor`) but does not install Docker, drivers or toolkits.

### Start

```bash
cp .env.example .env         # optional: Salesforce credentials, search, ports
./techsara up --dry-run      # detection + selection + Compose validation, writes nothing
./techsara up                # first run downloads the pinned models, then starts in stages
```

Windows: `.\techsara.ps1 up` (PowerShell) or `techsara.cmd up` (CMD).

Then open <http://localhost:3000>. Health: <http://localhost:8080/health>.
pgAdmin (when `COMPOSE_PROFILES=admin`): <http://127.0.0.1:5050>.

What `./techsara up` decides for you:

| Machine | Result |
|---|---|
| Mac M-series (e.g. M5 Max) | native vLLM-Metal main model sized to your RAM, embeddings/reranker on the larger tiers, app containers in Docker |
| one NVIDIA GPU (10–70+ GiB free) | CUDA vLLM containers, model tier by free VRAM |
| one DGX Spark (GB10, 128 GB unified) | the full DGX set: 27B main model + separate vision router + embeddings + reranker + OCR |
| **two DGX Sparks cabled together** | the same, plus the main model **sharded across both GPUs** (auto-detected over the RoCE links, see §7) |
| CPU only, ≥8 GiB | llama.cpp with a 0.6B model |
| not enough for any of the above | application-only (UI + data services, no local model) |
| you already run an OpenAI-compatible server | `./techsara up --profile external-development` with the endpoint in `.env` |

### First run, downloads, offline reuse

Downloads are revision-pinned, resumable (`.partial` staging) and committed only
after required-file validation; the whole set is disk-checked as a group first.
`HF_TOKEN` in `.env` (or exported) enables gated repos. After one complete online
start, `./techsara up --offline` reuses everything and never touches the network.
Model cache locations: macOS `~/Library/Caches/TechSara/models`, Linux
`${XDG_CACHE_HOME:-~/.cache}/techsara/models`, Windows `%LOCALAPPDATA%\TechSara\models`;
override with shell-level `TECHSARA_MODEL_CACHE` (set *before* running the wrapper).

### Stop

```bash
./techsara down     # stops the Compose project (and the Node-2 worker in dual mode); keeps every volume
```

---

## 5. Hardware profiles and models

Selection ([`launcher/techsara_cli/profiles.py`](launcher/techsara_cli/profiles.py))
is deterministic and conservative: a tier is chosen from RAM / per-device free
VRAM, reserves for OS, Docker, the application and safety are subtracted, and
each candidate model must fit its own `minimum_memory_bytes` *plus* KV cache for
the first context candidate that fits. Automatic selection downshifts rather
than guesses; `--profile` / `--model` overrides are validated against the
detected hardware and raise instead of silently degrading.

| Profile (`config/hardware-profiles.yaml`) | Runtime | Main model | Router | Embed / Rerank / OCR | Context candidates | Conc. |
|---|---|---|---|---|---|---|
| `mac-16-24gb` | native vLLM-Metal | Qwen3 8B MLX 4-bit | shared | – / – / – | 8192, 4096 | 1 |
| `mac-32-47gb` | native vLLM-Metal | Qwen3 14B MLX 4-bit | shared | ✓ / – / – | 16384 … | 1 |
| `mac-48-79gb` | native vLLM-Metal | Qwen3.6 35B-A3B MLX 4-bit | shared | ✓ / ✓ / – | 32768 … | 1 |
| `mac-80-127gb` | native vLLM-Metal | Qwen3.6 35B-A3B MLX 4-bit | shared | ✓ / ✓ / – | 32768 … | 2 |
| `mac-128gb-plus` | native vLLM-Metal | Qwen3.6 35B-A3B MLX 6-bit | shared | ✓ / ✓ / – | 65536 … | 2 |
| `dgx-spark` | CUDA vLLM containers | **RadixArk/Qwen3.8-27B-NVFP4** (served as `Qwen/Qwen3.8-27B-NVFP4`) | **Qwen3-VL-8B FP8** (separate) | ✓ / ✓ / **✓ Unlimited-OCR** | **262144**, 131072, 65536 | 4 |
| `nvidia-large` (≥70 GiB/device) | CUDA vLLM | Qwen3.6 35B-A3B FP8 | shared | ✓ / ✓ / – | 32768 … | 2 |
| `nvidia-medium` (≥40 GiB) | CUDA vLLM | Qwen3 30B-A3B FP8 | shared | ✓ / – / – | 16384 … | 1 |
| `nvidia-small` (≥20 GiB) | CUDA vLLM | Qwen3 14B AWQ | shared | ✓ / – / – | 16384 … | 1 |
| `nvidia-minimal` (≥10 GiB) | CUDA vLLM | Qwen3 8B AWQ | shared | – | 8192, 4096 | 1 |
| `local-minimal` (CPU, ≥8 GiB) | llama.cpp | Qwen3 0.6B GGUF Q8 | shared | – | 4096, 2048 | 1 |
| `app-only` | none | – | – | – | – | 0 |
| `external-development` | your server | from `.env` | from `.env` | from `.env` | 8192 | 1 |

"shared" router = the main model also classifies requests. Vision and OCR are
deliberately **off on every Mac profile** until the pinned Metal runtime passes
an image contract. The 16 model entries (HF id, immutable revision, quantization,
download/loaded bytes, min/recommended memory, backend, license, startup flags)
are in [`config/model-manifest.yaml`](config/model-manifest.yaml); the same file
pins `uv` 0.11.32, `huggingface_hub`, the vLLM-Metal 0.2.0 wheel + vLLM 0.21.0
source hashes, and the three container image digests.

Two honest consequences of the budget: a 16 GiB Mac selects **app-only** (the
smallest Mac model plus Docker Desktop would swap; the tier starts around 24 GiB),
and a GPU under the smallest CUDA tier falls to the **CPU** tier, not app-only.

---

## 6. The launcher (`./techsara`)

The shell/PowerShell shims install a checksum-verified `uv` 0.11.32 under
`${TECHSARA_HOME:-~/.techsara}` and run `python -m techsara_cli` with a managed
Python 3.12; the launcher itself uses only the standard library.

```text
./techsara up [--dry-run] [--profile ID] [--model HF_ID] [--skip-ocr] [--offline] [--verbose]
./techsara down [--dry-run]
./techsara restart [up options]
./techsara status
./techsara doctor [--offline]
./techsara logs [--tail N] [--service NAME]
./techsara models
./techsara update-models [--offline] [--dry-run]
./techsara redetect
```

**What `up` does, in order** ([`cli.py`](launcher/techsara_cli/cli.py) `_cmd_up` / `_start_compose`):

1. Detect hardware ([`hardware.py`](launcher/techsara_cli/hardware.py)): OS, arch, RAM, GPU (`nvidia-smi`), Docker/Compose, a real container GPU smoke test, DGX Spark signature, model-cache location.
2. Select a profile and context (§5); on `dgx-spark` decide the cluster mode (§7).
3. Ensure secrets (`.runtime/secrets.env`, generated if blank in `.env`), ensure the pinned models (download/resume/validate), on a Mac install the native runtime.
4. Write `.runtime/{hardware.json, selected-profile.json, generated.env}` and validate the resolved Compose model (`docker compose config`).
5. Reconcile: stop project-owned optional services that are no longer desired; never touch containers it did not create.
6. Build the three application images; start **postgres** (+ **searxng** if search is on) and wait for health.
7. NVIDIA: start and *probe* **vllm-embed** (failure → embeddings disabled), **vllm-reranker** (DGX; failure → in-process reranker), **vllm-router** (failure → main model routes), **vllm-ocr** (failure → OCR disabled). In dual mode: prepare the worker host and start the worker (§7).
8. Start the **main model** and probe chat (and vision). One failed start gets a single retry at the next safer context with concurrency 1 (single mode only).
9. Start the **orchestrator** and verify its `/health` contract, then **sync-worker**, **frontend**, **pgadmin**.
10. Write `.runtime/state.json` + `capabilities.json` with everything that happened, including every degraded reason.

Every wait uses the service's Docker healthcheck; a service that restarts three
times *during the wait* fails fast with its last log lines. Probes are real API
calls made from a one-off orchestrator container on the internal network.

`down` runs `docker compose down --timeout 120` **without `-v`**, stops only
native processes it owns, and lists foreign `sf-local-ai` containers instead of
touching them. `status` is read-only. `doctor` exits non-zero only for blocking
prerequisites and prints exact remediation for everything else. `logs` redacts
known secrets. Health probes and printed URLs follow `FRONTEND_PORT` /
`ORCHESTRATOR_PORT` and the bind address.

**Configuration ownership.** You own `.env`. The launcher owns
`.runtime/generated.env` (secret-free, mode 0644: selected endpoints, container
paths, contexts, capability flags, cluster keys) and `.runtime/secrets.env`
(mode 0600). Compose precedence is `.env` → `secrets.env` → `generated.env`, so
generated selection values win even over stale exported shell variables. Do not
copy generated values back into `.env`.

---

## 7. Two-node DGX Spark cluster

Two DGX Sparks connected back-to-back over their 200G ConnectX‑7 links can serve
the main model together. `./techsara up` on the head decides this on its own
(`CLUSTER_MODE=auto`, the default):

| Machine | What `./techsara up` does |
|---|---|
| Mac, generic NVIDIA, CPU | nothing cluster-related runs — no ssh, no `ip` commands |
| one DGX Spark | single-node stack, plus one line: `Cluster: single - no second DGX Spark answered …` |
| two DGX Sparks cabled | finds the peer on the RoCE links (neighbour table → the `.1/.2` convention → TCP/22), verifies it over ssh (GB10, Docker, `/dev/infiniband`), copies the pinned image and the model to it once, starts the vLLM **worker** there and the **head** here, and shards the main model with **tensor parallelism (TP=2)** over NCCL/RoCE. `./techsara down` stops the worker too. |

Everything else (router, embeddings, reranker, OCR, PostgreSQL, the application)
stays on Node 1 and keeps using the single `http://vllm:8000/v1` endpoint; the
application code did not change. `CLUSTER_MODE=single` forces the single-node
layout on a pair, `dual` forces the cluster and fails loudly if the peer is
unusable. The only manual prerequisite is ssh key authentication to Node 2.

Measured on the two Sparks (`spark-0e68` head, `spark-476e` worker; details,
topology, failure tests and limitations in [`docs/CLUSTER.md`](docs/CLUSTER.md)):

| | Single node | Dual (TP=2) |
|---|---|---|
| Single-request decode | 20 tok/s | **24–27 tok/s** |
| Concurrency 4 (512 in / 128 out) | 54 tok/s | 53 tok/s |
| Concurrency 16 | **123 tok/s** | 105 tok/s |
| KV cache | 542k tokens | ~950k tokens **per node** |
| NCCL over both RoCE rails | – | 22 Gb/s, 17.6 µs (each link caps at ~13 Gb/s — see §24) |

So dual mode buys faster replies and roughly double context headroom, not peak
throughput, on this fabric. Pipeline parallelism was tried and refused by vLLM
for this multimodal model class. The head and worker heal themselves after a
crash (measured: worker killed → completions back in 584 s with no operator).

```bash
scripts/cluster-status.sh --probe   # both nodes, RDMA links, NCCL transport, live GPU activity on both GB10s
scripts/cluster-doctor.sh --rdma    # preflight: links, MTU, ssh, docker/GPU, image, model, ports, firewall, memory
scripts/cluster-test.sh             # two-node NCCL all-reduce inside the vLLM image (validated data + transport)
scripts/cluster-bench.sh            # vllm bench serve against the live API with per-node GPU sampling
scripts/cluster-logs.sh head|worker|nccl|all [-f]
```

Files: [`compose/compose.cluster-dgx-spark.yaml`](compose/compose.cluster-dgx-spark.yaml)
(head overlay, layered last), [`compose/compose.cluster-worker.yaml`](compose/compose.cluster-worker.yaml)
(Node 2, its own Compose project `sf-local-ai-worker`), [`launcher/techsara_cli/cluster.py`](launcher/techsara_cli/cluster.py),
[`scripts/`](scripts/).

---

## 8. Services, ports, networks, volumes

Compose project `sf-local-ai`. Base file [`compose.yaml`](compose.yaml) plus one
runtime overlay from [`compose/`](compose/), in this order: runtime overlay →
`compose.windows-wsl2.yaml` (Windows NVIDIA) → `compose.published-<family>.yaml`
(only with `PUBLISH_MODEL_PORTS=true`) → `compose.cluster-dgx-spark.yaml` (dual mode).

| Service | Image | Port (internal → published) | Network | Volumes | Compose profile |
|---|---|---|---|---|---|
| `postgres` | `postgres@sha256:9a8afca5…` | 5432 → `127.0.0.1:5432` (always loopback) | application | `pgdata` | always |
| `pgadmin` | `dpage/pgadmin4@sha256:2f4ce946…` | 80 → `127.0.0.1:5050` | application | `pgadmin`, `pgadmin/servers.json` | `admin` |
| `searxng` | `searxng/searxng@sha256:75e3528c…` | 8080 (internal only) | application | `./searxng` | `search` |
| `orchestrator` | built (`Dockerfile.cpu`, or `Dockerfile.cuda` on NVIDIA) | 8080 → `${TECHSARA_BIND_ADDRESS}:${ORCHESTRATOR_PORT}` | application + inference | `data`, `reports`, `hf-cache`, `./brain/packs:/data/brain:ro` | always |
| `sync-worker` | built | – | application + inference | `data`, `./sync-worker/config.yaml:ro` | always |
| `frontend` | built (3-stage Node 20 alpine) | 3000 → `${TECHSARA_BIND_ADDRESS}:${FRONTEND_PORT}` | application | – | always |
| `vllm` | `vllm/vllm-openai@sha256:24f2f897…` | 30000 (→ `VLLM_PORT` 8000 if published) | inference | model cache `:ro`, `hf-cache` | DGX / NVIDIA |
| `vllm-router` | same | 30002 (→ 8002) | inference | same | DGX only |
| `vllm-embed` | `nvcr.io/nvidia/vllm@sha256:654e563e…` | 30003 (→ 8003) | inference | same | `embeddings` |
| `vllm-reranker` | same | 30005 (→ 8005) | inference | same | `reranker` (DGX) |
| `vllm-ocr` | `vllm/vllm-openai@…` | 30004 (→ 8004) | inference | same | `ocr` (DGX) |
| `llama-cpp` | `ghcr.io/ggml-org/llama.cpp@sha256:2a8440d3…` | 30000 (→ 8000) | inference | model cache | CPU profile |

- Network **`application`** is a normal bridge; **`inference`** is `internal: true` — model containers are `expose`-only and reachable solely from the orchestrator and sync-worker unless `PUBLISH_MODEL_PORTS=true`.
- Named volumes (the persistent data boundary): `sf-local-ai_pgdata`, `sf-local-ai_data` (DuckDB, LanceDB, Parquet, workspaces, brain mount point), `sf-local-ai_reports`, `sf-local-ai_pgadmin`, `sf-local-ai_hf-cache`.
- On a Mac the model servers are **native host processes**; containers reach them through authenticated loopback bridges on 18100/18103/18105 (`launcher/techsara_cli/bridge.py`).
- Model services on DGX share one 128 GB unified pool with measured `--gpu-memory-utilization` shares: main 0.35, router 0.17, OCR 0.14, embed 0.04, reranker 0.04 (dual mode: main 0.30 per node + explicit 16 GiB KV).
- Compose profiles are derived automatically: `embeddings`, `reranker`, `ocr` from the selected models; `search` from `SEARCH_ENABLED=true` + `SEARCH_PROVIDER=searxng`; `admin` from `COMPOSE_PROFILES`.

---

## 9. Configuration reference (`.env`)

Copy [`.env.example`](.env.example) (388 commented lines) to `.env`. Groups and
the keys that matter most — every key is documented in the example file:

| Group | Keys |
|---|---|
| Models / auth | `HF_TOKEN` (gated downloads) |
| Salesforce | `SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL` (**your My Domain URL**, not login.salesforce.com), `SF_API_VERSION=v61.0`, `SF_LIGHTNING_BASE_URL`, `SF_LIVE_ENABLED=true`, and **one** credential: `SF_CLIENT_SECRET` (client-credentials, needed for *live* queries) or `SF_PRIVATE_KEY_B64` / `SF_PRIVATE_KEY_HOST_FILE` (JWT bearer, sync only) |
| Local secrets | `POSTGRES_DB/USER/PASSWORD`, `SESSION_SECRET`, `PGADMIN_DEFAULT_EMAIL/PASSWORD`, `SEARXNG_SECRET` — blank = generated into `.runtime/secrets.env`; pool knobs `APP_DB_POOL_*`, `APP_DB_STATEMENT_TIMEOUT_MS` |
| Ports / exposure | `FRONTEND_PORT=3000`, `ORCHESTRATOR_PORT=8080`, `POSTGRES_PORT`, `PGADMIN_PORT`, `TECHSARA_BIND_ADDRESS=127.0.0.1` (literal IP only), `PUBLISH_MODEL_PORTS=false`, `VLLM_PORT=8000`, `VLLM_ROUTER_PORT=8002`, `VLLM_EMBED_PORT=8003`, `VLLM_OCR_PORT=8004`, `LLAMA_CPP_PORT=8000` |
| Optional services | `SEARCH_ENABLED`, `SEARCH_PROVIDER=searxng|tavily|brave`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `COMPOSE_PROFILES=search,admin` |
| Two-node cluster | `CLUSTER_MODE=auto|single|dual`; optional overrides `CLUSTER_HEAD_IP`, `CLUSTER_WORKER_IP`, `_2` variants, `CLUSTER_WORKER_SSH`, `CLUSTER_MASTER_PORT=29501`, `CLUSTER_TENSOR_PARALLEL_SIZE=2`, `CLUSTER_PIPELINE_PARALLEL_SIZE=1`, `CLUSTER_GPU_MEMORY_UTILIZATION=0.30`, `CLUSTER_KV_CACHE_MEMORY_GIB=16`, `CLUSTER_NCCL_*`, `CLUSTER_SPECULATIVE_CONFIG`, `CLUSTER_MAX_NUM_BATCHED_TOKENS` |
| Search / fetch / repos | `SEARCH_MAX_RESULTS`, `SEARCH_SOURCE_CHAR_BUDGET`, `SEARCH_RATE_PER_MIN`, `SEARCH_CACHE_TTL`, `FETCH_TIMEOUT_MS`, `FETCH_MAX_BYTES`, `URL_ANALYSIS_ENABLED`, `URL_MAX_PAGES`, `REPO_ANALYSIS_ENABLED`, `REPO_MAX_MB`, `REPO_MAX_FILES`, `WORKSPACE_TTL_HOURS=24`, `WORKSPACE_QUOTA_GB=20` |
| Model window | **`MAIN_MODEL_MAX_LEN`** — the served context window (see [§9.1](#91-the-context-window)); `MODEL_MAX_OUTPUT=8192`, `CONTEXT_SAFETY_MARGIN`, `TOKENIZE_TIMEOUT`, `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS`, `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS=16384`, `MAIN_MODEL_CONTEXT_SAFETY_MARGIN=8192`; `GEN_WALL_CLOCK_S=1800` (raise it alongside a very large window) |
| Salesforce Intelligence | `SALESFORCE_INTELLIGENCE_MODE_ENABLED`, `SALESFORCE_CONTEXTUAL_CLARIFICATION_ENABLED`, `SALESFORCE_STARTER_CARD_ENABLED`, `SALESFORCE_MAX_CLARIFICATION_ROUNDS=2`, `SALESFORCE_MULTI_SELECT_CLARIFICATION`, `CLARIFY_MODE=ambiguous|always|off`, `ROUTER_INPUT_CHAR_CAP`, `EMBED_INPUT_CHAR_CAP` |
| Brain / learning | `BRAIN_ENABLED=true`, `BRAIN_MAX_CHARS`, `LEARNED_EXAMPLES_ENABLED=true`, `LEARNED_EXAMPLES_K=2` |
| Memory / context | `CONTEXT_WARN_THRESHOLD=0.60`, `CONTEXT_BG_COMPACT_THRESHOLD=0.70`, `CONTEXT_COMPACT_THRESHOLD=0.80`, `KEEP_RECENT_TURNS=8`, `SUMMARY_MAX_TOKENS=2000`, `MIN_OUTPUT_FLOOR=1024`, `SEMANTIC_RECALL_ENABLED`, `RETRIEVE_TOP_K=6`, `CONTEXT_METER_ENABLED` |
| Charts / uploads | `CHART_TRIGGER_MODE=explicit|hybrid`, `CHART_FUNNEL_STAGE_ORDER`, `DATASET_UPLOADS_ENABLED`, `UPLOAD_MAX_MB=200`, `ARCHIVE_MAX_UNCOMPRESSED_MB`, `ARCHIVE_MAX_FILES`, `ARCHIVE_MAX_RATIO`, `ARCHIVE_MAX_DEPTH=1` |
| Sync worker | `SYNC_INTERVAL_MINUTES=5`, `SYNC_AUTO_FIELDS=true`, `SYNC_AUTO_OBJECTS=true`, `SYNC_MAX_FIELDS=4000`, `SYNC_REPORT_NEW_OBJECTS` |
| Frontend | `MOCK_MODE=false`, `NEXT_PUBLIC_APP_NAME=TechSara AI` |
| External-development profile only | `OPENAI_BASE_URL`, `MAIN_MODEL`, `OPENAI_API_KEY`, `ROUTER_*`, `EMBED_*`, `RERANK_*`, `OCR_*`, `MAIN_SUPPORTS_*`, `MAIN_CONTEXT_LENGTH`, … |
| **Shell-only** (read before `.env`) | `TECHSARA_HOME`, `TECHSARA_MODEL_CACHE`, `TECHSARA_GPU_SMOKE_IMAGE` |

The complete list of orchestrator settings with defaults is
[`orchestrator/app/config.py`](orchestrator/app/config.py); reasoning knobs are
explained in [`docs/CONFIG.md`](docs/CONFIG.md).

---

### 9.1 The context window

`MAIN_MODEL_MAX_LEN` in `.env` is the one knob for the served window. Leave it
unset and the profile's own value is used (262,144 on DGX Spark). Set it larger
and the launcher enables **YaRN** rope scaling automatically — it reads the
model's real geometry, computes the factor, and passes the override to both
nodes:

```text
MAIN_MODEL_MAX_LEN=800000     # -> "Context: 800,000 tokens (model is natively
                              #     262,144; YaRN factor 3.06 enabled)"
```

Measured on this deployment (2026-08-25):

| | Native | Extended |
|---|---|---|
| Served window | 262,144 | **800,000** (max input 775,424) |
| Rope | `default` | `yarn`, factor 3.06, `mrope_section` preserved |
| KV pool / node | 933,232 tokens | 967,766 tokens (1.21× at full length) |
| Short-prompt A/B (10 deterministic prompts) | baseline | 5/10 byte-identical, none worse, `4871*39` newly **correct** |
| Needle recall at 20K / 100K | found | found, same latency |

Three honest costs before you raise it:

1. **A cold single prompt is slow.** Prefill measures `7.34e-4·n + 1.93e-9·n²`
   seconds on this cluster (fit to five points, ±0.1 s): 262K ≈ 5.4 min,
   400K ≈ 10 min, **800K ≈ 30 min**. That is why `GEN_WALL_CLOCK_S` is raised
   to 4200 alongside it — the 1800 s default would kill the request just as it
   finished prefilling. A conversation that *grows* to 800K is unaffected:
   prefix caching means each turn only prefills the new tokens.
2. **Concurrency at full length drops** to 1.21× — one 800K request at a time.
   Ordinary chats are unaffected, because vLLM allocates KV blocks on demand
   (a 657-token chat uses 657 tokens, not 800,000).
3. **YaRN is static**, so the scaling applies at every length. The A/B above
   found no degradation, but it is a 10-prompt probe, not a benchmark.

The launcher refuses a window the KV pool provably cannot hold, with the
arithmetic and the exact `CLUSTER_KV_CACHE_MEMORY_GIB` you would need. The
ceiling is 4× native (1,048,576) and, at the default 16 GiB KV budget,
~922,000 tokens. Set `MAIN_MODEL_MAX_LEN=262144` to serve the model exactly as
it was trained.


## 10. How a chat message is answered

`POST /chat` in [`orchestrator/app/main.py`](orchestrator/app/main.py) streams
Server-Sent Events. The steps, in order:

1. **Identity + ownership.** There is one local user; every conversation row is scoped to it and a foreign id is a plain 404.
2. **Detached generation.** A new send cancels any running generation for the same conversation. Frames are buffered in a `LiveGeneration` so a reload can `GET /chat/attach/{id}` and replay; the `generation_id` is an idempotency key so two attached tabs persist the answer once. If nobody is attached when it finishes, the server persists it.
3. **Auto-orchestration** (`engines/orchestrate.py`): one cheap router-model call decides whether the question wants the **agent** and/or **web search**. Effort gates it: *Fast* → neither, *Think*/*Max* → both allowed. Explicit toggles win.
4. **Gates.** Web search needs `SEARCH_ENABLED`, assistant mode, no attachment; **Salesforce mode never searches the web**. The status line is emitted *after* the gates so the UI never promises a search that won't happen.
5. **Memory injection** (§15): assistant mode gets saved facts + semantic/keyword recall across chats; Salesforce mode gets keyword recall only (CRM answers must not come from chat memory).
6. **Context detection.** GitHub link, pasted URLs (only when the links *are* the request — a long paste with links inside is a document), previously fetched pages and uploaded documents re-injected as question-relevant excerpts on every later turn, uploaded datasets.
7. **Compaction** (§15) assembles the history within the model window and reports `meta.context` for the context meter.
8. **Salesforce Intelligence Mode** (§14) runs first in Salesforce mode; a clarification answer *owns the turn*.
9. **Engine chain**, first match wins: SF Intelligence handled it → PDF → image → GitHub repo → URL list → agent → web search → uploaded dataset → assistant chat → live Salesforce → the **LangGraph router graph** (`graph.py`), which classifies into `sql | rag | vision | report | chat`.
10. **`meta`** is enriched (route, model, effort, SQL, rows, chart, citations, sources, steps, report files, context, clarification) and **`done`** is sent; the exchange is stored, cross-chat facts are extracted and background compaction runs detached.

**One model, three efforts.** `smart` = the main model (thinking on for *Think*/*Max*, off for *Fast*); `fast` = the router model with thinking off. Legacy `low/medium/high/extra_high` map to `fast/think/think/max`. *Max* additionally runs best-of-3 candidates with a judge for chat answers and deeper agent/search budgets. Thinking budgets are **off by default** (the served vLLM build ignores the server-side budget; client enforcement is never applied when tools are attached). `GEN_WALL_CLOCK_S` (1800 s) is a hang guard, not a budget.

**Tool calling** (`llm.chat_with_tools`): vLLM's native tool parser first (`qwen3_xml`), then guided-JSON, then unconstrained + validation + one repair, then a deterministic fallback.

SSE events ([`sse.py`](orchestrator/app/sse.py)): `token`, `reasoning`, `status` (phase/text), `step` (agent), `research`, `meta` (once), `done` / `error` (terminal), plus a `: keep-alive` comment every 15 s (`SSE_HEARTBEAT_SECONDS`) because the Node proxy cuts idle bodies at 300 s.

---

## 11. The engines

[`orchestrator/app/engines/`](orchestrator/app/engines/):

| Engine | `meta.route` | What it does | Chosen when |
|---|---|---|---|
| `router.py` | – | Strict-JSON classifier on the router model with few-shots; images force `vision`; unparseable → main-model retry → `rag` | Salesforce-mode questions that reached the graph |
| `orchestrate.py` | – | Auto-plan `{agent, search}` | Every text turn unless an attachment / explicit agent |
| `sql.py` | `sql` | Schema slice (≤40 tables × 140 columns, brain-pinned tables first) → one guarded `SELECT` → DuckDB **read-only, no external access** → one retry with the real column list → 500-row preview, 100k-row export, chart, **deterministic summary computed in Python over up to 100k rows** → streamed narrative. Falls back to live Salesforce when the warehouse cannot answer or is locked | router `sql`, live toggle, agent SQL steps, report sections |
| `rag.py` | `rag` | Embed → LanceDB top-30 → rerank (in-process / remote / off) → top-8 → cited answer; also serves brain knowledge for "how does X work" | router `rag`, default fallback |
| `vision.py` | `vision` | Up to 5 images through the multimodal main model; invoices/contracts return a JSON block first | any attached image |
| `document.py` + `ocr.py` | `vision` | PDF/DOCX/plain: every page's text layer, OCR sidecar (`baidu/Unlimited-OCR`) for thin pages (≤40), first pages also as images; full text stored for later turns | `pdf` attached |
| `report.py` | `report` | ≤6 planned sections filled via sql/rag, matplotlib PNGs, Markdown → `.docx` + `.pdf` (pandoc + WeasyPrint) into `/reports` | router `report` |
| `chat.py` | `chat` | Plain streamed completion (assistant or Salesforce small-talk prompt) | assistant mode, router `chat` |
| `agent.py` | `agent` | PLAN → EXECUTE (≤8 steps of `sql|rag|llm|web|salesforce`, concurrency 3, `step` events) → SYNTHESIZE; grounded on the user's own sentence, not the planner's paraphrase | agent toggle or auto-plan |
| `search.py` | `search` | Query rewrite → 1–6 queries → provider → SSRF-safe fetch (16 concurrent) → extraction → tiered budget → cited answer; cache + per-user rate limit; deep research at *Max* | assistant mode + search wanted |
| `url.py` | `url` | Fetch pasted links safely, store page text, answer with sources | links are the request |
| `repo.py` | `repo` | Shallow capped clone of a public GitHub repo (hooks off, never executed) → 60-line chunks → overview / code Q&A with `path:Lstart-Lend` | GitHub URL or an indexed repo in the conversation |
| `dataset.py` / `dataset_report.py` | `dataset` | Answers from a safe **profile** of uploaded CSV/XLSX/ZIP data (full rows when ≤200) wrapped in "DATA, NOT INSTRUCTIONS" delimiters; report variant computes headline facts in Python | uploads exist in the conversation |
| `live_sf.py` | feeds `sql` | Model writes SOQL → guarded → REST → merged with warehouse rows, live values win; live `describe` for schema questions | warehouse can't answer, or **Live Salesforce** |
| `sf_intel.py` | `sql`/`chat`/`clarify` | Salesforce Intelligence Mode (§14) | Salesforce mode |

---

## 12. Salesforce: sync, warehouse, live queries

### Credentials

Identity: `SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL` (My Domain). Plus one of,
in strict precedence: `SF_CLIENT_SECRET` (client-credentials grant — the only
grant the orchestrator's **live** path supports), `SF_PRIVATE_KEY_FILE`
(compose pins `/data/sf_jwt_key.pem`, which *you* place in the data volume),
`SF_PRIVATE_KEY_B64` (the launcher stages a host key file into this), or the
default path. JWT assertions are RS256 with a 180 s validity; tokens are cached
25 minutes and invalidated on 401. Secrets never appear in logs or reprs.

### The sync worker ([`sync-worker/`](sync-worker/))

A loop every `SYNC_INTERVAL_MINUTES` (5; a pass takes ~3 min, so changes land in
5–8 minutes) over the objects in [`config.yaml`](sync-worker/config.yaml) plus,
with `SYNC_AUTO_OBJECTS=true`, every custom object and Task/Event/EmailMessage
found in the org (currently **~1,020 objects / ~18,800 fields**). Per object:

1. `describe` → drop fields hidden by field-level security; adopt new fields (`SYNC_AUTO_FIELDS`, capped by `SYNC_MAX_FIELDS`) except compound/encrypted/credential types; create the table so "how many X" answers 0, not "table not found".
2. **Full** extract (no watermark yet) through **Bulk API 2.0** with a REST SOQL fallback for objects Bulk refuses, or **incremental** REST SOQL from the watermark. 24 objects without a watermark field are fully re-extracted each cycle.
3. Each batch → Parquet (`/data/parquet/<Object>/…`) → DuckDB upsert (every column `VARCHAR`, self-healing types) → LanceDB chunks for long-text `rag_fields`.
4. **Deletes are propagated**: exact reconciliation on full extracts, recycle-bin (`IsDeleted`) queries on incremental ones.
5. Watermark written last, set to the cycle *start*, so mid-extract edits are re-fetched (upserts are idempotent).

Embedding failures never block data: record ids are parked in `_rag_index_pending`
and retried (≤500 per cycle). An authentication error aborts the cycle to avoid
Salesforce login lockout. Backoff 30 s → 30 min on failures.

CLI inside the container: `python -m syncworker.objects resync <Object>` clears a
watermark so the next cycle backfills newly added fields (a config edit alone
does not backfill history).

### DuckDB locking — expected behaviour

DuckDB allows one writer *or* many readers. The worker opens a connection per
write (milliseconds), the orchestrator opens read-only and waits up to 4–6 s;
if the warehouse is still busy the question **falls back to live Salesforce**
instead of erroring. The one path without a retry is `GET /health`'s DuckDB
check, so **`status: degraded` with `checks.duckdb.status == "error"` during a
sync write is normal and transient**, not an outage.

### Live Salesforce and the field dictionary

`core/salesforce.py` guards every SOQL (one `SELECT`, injected `LIMIT`, read-only
user). `core/sf_dictionary.py` + `orchestrator/scripts/build_dictionary_from_metadata.py`
build a field dictionary (formulas, roll-ups, picklists, lookups, help text)
from a metadata retrieve, merged with brain field notes, and `core/org_brief.py`
turns dictionary + brain + schema into the grounding block every SQL/SOQL
generation sees.

---

## 13. The Salesforce Brain (knowledge packs)

[`brain/packs/`](brain/packs/) holds **21 YAML packs (~1,000 knowledge chunks)**
compiled from the raw documents in [`brain/sources/`](brain/sources/) (SOPs,
module docs, an org-wide knowledge base, the org schema, a full metadata
retrieve, the 38 active production validation rules). The orchestrator mounts
the directory read-only at `/data/brain` and **hot-reloads on file change** —
drop or edit a pack and the next question uses it, no rebuild, no restart.

Pack schema ([`core/brain.py`](orchestrator/app/core/brain.py)):

| Key | Effect |
|---|---|
| `triggers` | words/phrases that pull the pack's `rules` and `tables` into a question (single words are stemmed; phrases matched whole) |
| `tables` | warehouse tables pinned into the schema slice |
| `rules` | trap-avoiding rules injected into SQL and SOQL generation (≤5 KB per pack, ≤24 KB per prompt) |
| `metrics` | canonical measures with `name`, `table`, `definition`, `sql`, `aliases` |
| `glossary` | term → meaning, injected when the term appears |
| `field_notes` | per-object / per-field help merged into the field dictionary (enrich-only; unknown fields are dropped; **last pack in filename order wins**) |
| `knowledge` | prose chunks (`title`, `keywords` — **single words only** —, `text`), lexically ranked across all packs, top 3 within `BRAIN_MAX_CHARS` |

Packs today: core people & companies, B2B staffing, bench marketing, external and
internal interviews, onboarding/HR, background checks, DocuSign, invoicing +
QuickBooks, training/LMS and the training portal implementation (the largest,
308 chunks), portal auth, Zoom recordings, internal ops, Apex reference, the CS
candidate-lifecycle and internal-operations SOPs, and production validation rules.

Workflow: put the source in `brain/sources/` → `python orchestrator/scripts/compile_brain_source.py`
→ review (**never teach a field the warehouse does not have**) → `python orchestrator/scripts/validate_packs.py`
(the ship gate: phantom fields, phantom columns in metric SQL, duplicate keys)
→ commit. Full guide: [`brain/README.md`](brain/README.md); design rationale:
[`docs/06-agent-design/SF-BRAIN.md`](docs/06-agent-design/SF-BRAIN.md).

---

## 14. Salesforce Intelligence Mode

The Salesforce pill is an agent, not a filter ([`docs/06-agent-design/SALESFORCE-INTELLIGENCE-MODE.md`](docs/06-agent-design/SALESFORCE-INTELLIGENCE-MODE.md),
code in [`orchestrator/app/core/sf_intel/`](orchestrator/app/core/sf_intel/) and
[`engines/sf_intel.py`](orchestrator/app/engines/sf_intel.py)):

- **Interpret** the request against conversation state, the brain vocabulary (with bounded spelling repair), the field dictionary and known people (full-name resolution runs before any "which Jayesh?" question).
- **Clarify** when genuinely ambiguous (`CLARIFY_MODE=ambiguous`): exactly one targeted question, ≤2 rounds, multi-select and custom answers allowed, rendered in the composer as a control (not a message), persisted with a resume token so it survives reloads, idempotent on double submission, and cancelled explicitly when stale (a pending question would otherwise block the conversation).
- **Plan, never free-form SOQL**: `plan.py` compiles a validated `SalesforceQueryPlan` — object queryable by this connection, every field readable, relationship depth ≤3, operator allowlist, date literals validated, `LIMIT ≤ 200`, ≤40 fields, ≤20 filters. Rejected plans ask the user instead. There is **no write path**.
- **Execute, paginate, calculate, verify, answer**, then update the conversation's Salesforce state and **resume the original request**.
- Progress phases are streamed as `status` events (the UI's phase star is backend-driven only); reasoning deltas are deliberately not forwarded on this route.

---

## 15. Memory, context and compaction

- **Context budget** (`context.py`, `compaction.py`): the served window is read from vLLM (`/v1/models`, `/tokenize`), usable input = window − reserved output − margin. Thresholds on the *usable* budget: warn at 60 %, background-compact at 70 %, compact at 80 %; the last `KEEP_RECENT_TURNS=8` turns are kept verbatim, older turns fold into a rolling summary (`conversation_summaries`, ≤2000 tokens) and embedded chunks (`conversation_chunks`) for in-conversation recall (`RETRIEVE_TOP_K=6`). The UI's context meter shows exactly these numbers; **Compact now** calls `POST /chat/compact`.
- **Cross-chat memory** (`facts.py`, `memory_semantic.py`, `memory_recall.py`, `/memory/facts` API): durable first-person facts are extracted by a background model call and stored as plain rows the user can list and delete; recall over *other* conversations is semantic (message embeddings in PostgreSQL, cosine, min score 0.30) plus keyword; nothing is fine-tuned, and chat embeddings never enter the Salesforce LanceDB corpus. Salesforce mode gets keyword recall only.
- **Titles** (`titling.py`): generated by the small model from the first exchange; a user rename always wins (`title_source`).
- **Learned examples** (`core/learned_examples.py`): thumbs-up SQL answers become few-shots; a thumbs-down disqualifies that SQL globally; derived from `messages` at query time, cached 300 s.
- The **262,144-token window** was verified end to end with `orchestrator/scripts/validate_long_context.py` (239,938 input tokens, needles at 64K/128K/200K/240K).

---

## 16. Charts, reports, uploads, documents, web, repos

- **Charts** (`core/chart_*`, `frontend/lib/chartOption.ts`): nine types (bar, line, area, pie, scatter, horizontal bar, donut, funnel, histogram). `CHART_TRIGGER_MODE=explicit` charts on request; `hybrid` also charts four deterministic shapes (time series, single-metric category comparison, trusted stage funnel, small part-to-whole). The model only ever sees column *profiles*, never cell values; the frontend adapter is a security boundary (no wire field can become an ECharts function). Charts are decided *before* the narrative streams so the model stops drawing ASCII bars.
- **Reports**: `.docx` + `.pdf` in the `reports` volume, downloadable through `/reports/{filename}` with path-traversal-safe names; also CSV/XLSX exports of SQL results (100k-row cap).
- **Uploads**: images (≤5 × 10 MB) and PDFs travel inline; datasets stream to `POST /uploads` (≤200 MB, ZIP/XLSX inspected with four independent bomb caps, depth 1, never `extractall`) into `/data/workspaces` (24 h TTL, 20 GB quota); a profile — never the raw file — is what the model sees.
- **Documents**: every page read, scanned pages OCR'd, stored in `documents` and re-injected on later turns.
- **Web search**: SearXNG (self-hosted, `COMPOSE_PROFILES=search`, ten engines beyond the defaults because the stock ones CAPTCHA), Tavily or Brave; all fetches go through `core/net.py` (DNS resolved first, private/loopback/metadata ranges refused, redirects re-checked, size/time bounded).
- **Repos**: public GitHub, shallow clone, size/file caps, hooks disabled, code never executed.

---

## 17. Data stores

| Store | Where | Contents |
|---|---|---|
| **PostgreSQL 18** (app state) | `pgdata` volume, `APP_DATABASE_URL` (no default — fails loudly) | schema **v7** ([`db.py`](orchestrator/app/db.py), 7 transactional migrations under an advisory lock): `users`, `conversations`, `messages` (+ `feedback`, `generation_id`), `conversation_summaries`, `conversation_chunks`, `uploads`, `url_documents`, `documents`, `repos`, `repo_chunks`, `sf_intents`, `sf_clarifications`, `sf_conversation_state`, `user_facts`, `message_embeddings`, `schema_migrations` |
| **DuckDB** warehouse | `/data/warehouse.duckdb` (`data` volume) | one `VARCHAR` table per synced Salesforce object + `_sync_meta`, `_rag_index_pending`; opened read-only by the orchestrator |
| **Parquet** | `/data/parquet/<Object>/` | one file per sync batch (landing zone; not read back, no retention) |
| **LanceDB** | `/data/lancedb`, table `chunks` | 800-word chunks of long-text fields with `object`, `record_id`, `field`; a sidecar pins the embedding model + dimension and refuses mismatched indexes rather than silently re-labelling them |
| **Reports** | `reports` volume, `/reports` | generated documents and exports |
| **Brain** | `./brain/packs` → `/data/brain:ro` | knowledge packs |
| **Workspaces** | `/data/workspaces` | uploads and repo clones (TTL/quota) |
| **Field dictionary** | `/data/sf_dictionary.json` | org metadata overlay |
| **Model / runtime caches** | `TECHSARA_MODEL_CACHE`, `hf-cache` volume, `~/.techsara` | pinned weights, compile/autotune caches, uv + Python + vLLM-Metal |

pgAdmin is pre-registered against the app database (`pgadmin/servers.json`);
the warehouse and the vector index are files, not PostgreSQL.

---

## 18. Frontend

[`frontend/`](frontend/) — Next.js **16.3** App Router, React 19, TypeScript
strict, Tailwind with semantic tokens (dark is primary, `html.light` overrides,
no hard-coded hex), self-hosted fonts, `output: 'standalone'` in a 3-stage
digest-pinned `node:20-alpine` image, non-root runtime.

- **One page** (`app/page.tsx` → `components/ChatApp.tsx`); deep links use `?c=<conversationId>`. There is no login page and no reports page any more (older screenshots show them).
- **API routes** (`app/api/*`) are server-side proxies to `ORCHESTRATOR_URL` (never exposed to the browser): `/api/chat` (SSE passthrough, undici timeout → 504, refused → 502, upstream 503 → 503, abort → 499), `/api/chat/{active,stop,compact,attach/[id],salesforce/[id],salesforce/cancel}`, `/api/history/[...path]` (an **allowlist**, forwarding cookies both ways), `/api/reports/[filename]` (safe-name checked), `/api/upload` (streamed multipart), `/api/auth/me`.
- **Streaming**: a spec-compliant incremental SSE parser (`lib/sse.ts`) typed into `token | reasoning | status | step | research | meta | done | error`; unknown events are ignored, never fatal. Streams are keyed per conversation in a module-level map (`lib/streams.ts`), so switching chats never aborts a generation; failures are recorded on the message (`unreachable` / `interrupted` / `error` / `stopped`).
- **History**: server-backed with a synchronous in-memory mirror persisted write-behind to IndexedDB (`lib/history.ts`, `lib/idbCache.ts`); the cache can never shrink a server thread (409); the only sanctioned shrink is a confirmed regenerate.
- **Headless logic in `lib/`, pixels in `components/`** — menus, palette, composer menu, clarification, context meter and phases are pure modules, which is why 28 of 32 test files run in plain Node.
- `MOCK_MODE=true npm run dev` serves fixtures for UI-only work without models.
- Conventions for contributors: read `frontend/AGENTS.md` (Next 16 differs from training data; route `params` are Promises), anything `position: fixed` is portalled to `<body>`, proxies are allowlists, progress indicators are backend-driven only, tolerate unknown wire fields, `npm ci` (lockfile committed).

---

## 19. Orchestrator API reference

All under `http://<orchestrator>:8080`. There is no authentication (see §20).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | dependency probes (`vllm`, `vllm-router`, `vllm-embed`, `duckdb`, `app_db`, …), served window vs configured, serving flags; `status: ok|degraded`, always HTTP 200 |
| `POST` | `/chat` | the SSE chat endpoint (`ChatRequest`: `messages` or `message`, `conversation_id`, `mode: salesforce|assistant`, `sf_live`, `model: smart|fast`, `effort: fast|think|max`, `agent`, `web_search: off|auto|on`, `images[]`, `pdf`, `clarification`) |
| `GET` | `/chat/attach/{conversation_id}` | re-join a running generation (replays the buffer, then live; 404 once finished) |
| `POST` | `/chat/stop` · `GET /chat/active` · `POST /chat/compact` | cancel, list running, force compaction |
| `GET` | `/chat/salesforce/{conversation_id}` · `POST /chat/salesforce/cancel` | starter options + pending clarification; dismiss it |
| `GET/POST/PUT/DELETE` | `/history/conversations[/{id}[/messages[/{message_id}/feedback]|/truncate|/title|/summary]]`, `GET /history/search?q=` | conversations, messages, thumbs, titles, summaries, search |
| `POST` | `/uploads` · `GET /uploads/{conversation_id}` | dataset uploads |
| `GET/POST/DELETE` | `/memory/facts[/{id}]` | cross-chat facts |
| `GET` | `/reports` · `/reports/{filename}` | generated files |
| `GET` | `/auth/me` | the single local identity |

`meta.route` values: `sql | rag | vision | report | chat | agent | search | url | repo | dataset | clarify`.
Contracts in detail: [`docs/01-codebase/frontend-api-contracts.md`](docs/01-codebase/frontend-api-contracts.md).

---

## 20. Security model

- **No application login.** Every request resolves to one local account; conversations are still owner-scoped (foreign ids → 404). *Anyone who can reach the port can read every conversation and query the Salesforce data* — the boundary is network exposure: frontend, orchestrator, PostgreSQL and pgAdmin bind to `127.0.0.1` by default; model APIs are `expose`-only on an internal network unless `PUBLISH_MODEL_PORTS=true` (they are unauthenticated). `TECHSARA_BIND_ADDRESS` widens exposure deliberately; PostgreSQL and pgAdmin never leave loopback. In dual cluster mode the main model's head listens on the host (`0.0.0.0:8000` when published, otherwise the Docker bridge gateway only) because NCCL/RoCE needs host networking.
- **Salesforce**: read-only integration user; SQL through `core/sql_guard.py` (one `SELECT`, no writes/DDL/extensions/file or network table functions, comment-smuggling rejected) on a **read-only DuckDB with external access disabled**; SOQL through a guard with forced `LIMIT`; Intelligence Mode compiles validated plans, the model never writes query text; no write path exists.
- **SSRF**: every outbound fetch goes through `core/net.py` (private/loopback/link-local/metadata ranges refused after DNS, re-checked per redirect, bounded).
- **Archives / uploads / reports**: zip-slip, symlink, device and bomb protections; report names path-safe; repo clones are data, never executed.
- **Prompt injection**: dataset content and column profiles are delimited as data; chart selection never sees cell values; titles treat bodies as untrusted; raw chain-of-thought never leaves the Salesforce Intelligence route.
- **Secrets**: `.env` is git-ignored with its backup patterns; generated config is secret-free; logs/errors redact known values; the sync worker keeps the JWT key in the data volume, not in `.env`.
- **Supply chain**: every image, base image, `uv`, wheel and model is pinned by digest/hash/revision; Python transitive dependencies of the orchestrator/sync worker are ranged, not hash-locked (documented gap).
- CORS is a fixed origin allowlist with credentials.

Historical threat assessment (2026-07-31, pre-dates loopback binding):
[`docs/01-codebase/security-model.md`](docs/01-codebase/security-model.md).

---

## 21. Operations

### Daily

```bash
./techsara status                 # hardware, profile, models, services, endpoint health, degraded reasons, cluster line
./techsara doctor                 # non-destructive checks with exact remediation
./techsara logs --tail 300 [--service vllm]
scripts/cluster-status.sh --probe # two-node: both GPUs, RDMA, NCCL transport, a live request
```

`/health` reference: `status` is `ok` only when every required dependency
passes; `degraded` with `duckdb: error` during a sync write is expected (§12);
`context` reports the served vs configured window.

### Runtime state

`.runtime/` (git-ignored): `hardware.json`, `selected-profile.json`,
`generated.env` (0644), `secrets.env` (0600), `state.json`, `capabilities.json`,
`cluster-worker.env` (dual mode), `locks/`, `logs/`, `pids/`. Shared assets:
`${TECHSARA_HOME:-~/.techsara}` (uv, Python, downloads, native runtime) and the
model cache. A launcher that was killed leaves `.runtime/locks/launcher.lock`;
it is treated as stale only after 6 hours — delete it by hand if no launcher runs.

### Preservation and backups

`./techsara down` never removes volumes. **Never** run `docker compose down -v`,
`docker volume rm` or a prune: `sf-local-ai_pgdata` is every conversation,
`sf-local-ai_data` is the warehouse, vector index and uploads. Back up `.env`,
`.runtime/`, the volumes and the model cache. A `data/warehouse-backup-*.duckdb`
snapshot may exist at the repo root (git-ignored).

### Upgrading

- Models: edit [`config/model-manifest.yaml`](config/model-manifest.yaml) (revision + files), then `./techsara update-models`; `./techsara up` re-selects.
- Images: change the digest in `compose.yaml` / `compose/*.yaml`; the overlay tests assert every image is digest-pinned. In dual mode both nodes must run the same digest (`scripts/cluster-sync.sh` enforces it).
- Brain packs: drop files in `brain/packs/` (live); validate first.
- Sync scope: edit `sync-worker/config.yaml` (live, read-only mount); `resync <Object>` to backfill.

### Troubleshooting

- **Nothing selected / a smaller profile**: check *available* memory, per-device free VRAM, Docker GPU readiness and the degraded reasons in `status` / `--verbose`.
- **"not allowed to use its socket"**: `sudo usermod -aG docker "$USER"` then `newgrp docker` (group changes do not apply to an existing session).
- **Docker check fails**: daemon running, Linux containers, Compose ≥ 2.24, WSL2 on Windows, a passing GPU smoke test on NVIDIA (override the probe image with `TECHSARA_GPU_SMOKE_IMAGE`).
- **Insufficient disk / gated download**: move the cache with shell-level `TECHSARA_MODEL_CACHE`; add `HF_TOKEN`. Partial downloads resume; invalid model directories are preserved, never overwritten — move them aside manually.
- **Mac has no native inference**: use a native arm64 terminal; Rosetta and Intel Macs cannot run vLLM-Metal. After a reboot, run `./techsara up` again (native processes are not OS services).
- **Embedding / OCR / vision disabled, router fell back**: see `status` degraded reasons and `logs`; these are optional roles. If `generated.env` shows `EMBED_ENABLED=false` / `RERANK_BACKEND=inprocess` / `ROUTER_BASE_URL=http://vllm:…` after an `up` on a healthy stack, re-run `./techsara up` (a launcher bug that read Docker's lifetime restart count was fixed 2026-08-25).
- **Salesforce sync does not start**: provide the three identity keys and one credential; `SF_LOGIN_URL` must be the My Domain URL; embeddings do not gate warehouse sync.
- **Two-node cluster not healthy**: `scripts/cluster-status.sh` names the side; `scripts/cluster-doctor.sh --rdma`, `scripts/cluster-logs.sh worker`; the head waits 5 minutes for the worker before restarting; see [`docs/CLUSTER.md`](docs/CLUSTER.md#failure-behaviour).
- **Port already in use**: stop the other listener or change the user-owned port; the launcher never replaces an unowned process.
- **Rollback to the pre-launcher stack**: [`docker-compose.yml`](docker-compose.yml) is kept only so a stack started from it can be stopped with it; do not add features there.

---

## 22. Development and tests

```bash
# Launcher (standard library only)
PYTHONPATH=launcher python3 -m pytest launcher/tests -q
# or: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s launcher/tests -v

# Orchestrator (needs a reachable test PostgreSQL; the guard refuses non-test database names)
cd orchestrator && python3 -m pip install -r requirements-dev.txt && \
  TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/techsara_test python3 -m pytest tests -q

# Sync worker
cd sync-worker && python3 -m pip install -r requirements-dev.txt && python3 -m pytest tests -q

# Frontend
cd frontend && npm ci && npm test && npx tsc --noEmit && npm run build
MOCK_MODE=true npm run dev        # UI-only development
```

| Suite | Scope | Size |
|---|---|---|
| Launcher | mocks + temp dirs; `test_compose_overlays.py` renders all 13 host fixtures (and the DGX dual-mode fixture) through real `docker compose config` and asserts platform invariants (loopback publication, digest-pinned images, no developer paths); skips without Compose ≥ 2.24 | 355 tests, all green on 2026-08-25 |
| Orchestrator | SSE contract, routing, Salesforce Intelligence (engine, plans, clarification), org brief / brain / dictionary, charts, live Salesforce, security (SQL guard, SSRF, archives, report paths, DB guard), history, memory, context budgets, reasoning; every app table is truncated before each test | 80 files, ~1,240 tests |
| Sync worker | discovery, JWT/secrets, Bulk fallback, upserts, deletes, watermarks, chunking, embedding integrity, CLI | 157 tests |
| Frontend | Vitest; 28 pure-logic files in Node, 4 component files in jsdom (`// @vitest-environment jsdom`) | 32 files, 436 cases |

Notes: CI runs these suites on every pull request and on every push to `main` or
`dev` (see §22.1); the local commands above stay the fast inner loop.
`npm run lint` (`next lint`) no longer exists on Next 16; use `npx tsc --noEmit`
and the editor's ESLint. Commit subjects
follow Conventional Commits (`feat(reasoning): …`, `fix(logging): …`); the
narrative and numbers go into a dated `CHANGELOG.md` entry.

### 22.1 Continuous integration and deployment

Two workflows. [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the
four suites on GitHub-hosted runners — the launcher one across Python 3.11 and
3.12, so six job runs — behind a `ci-ok` gate job. [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)
runs on a **self-hosted runner on the DGX Spark itself**, because the deploy
target *is* this machine: it owns the GPU, the 41 GB model cache, the named
volumes and the `.env`. Nothing is copied anywhere and the workflow needs no
secret.

**What a merge to `main` does.** CI runs; only if it goes green does Deploy
start (`workflow_run`, never `push`, so untested code cannot reach the box).
Deploy then runs [`scripts/deploy.sh`](scripts/deploy.sh) against the production
checkout, which takes a lock, refuses a dirty tree, resolves the target commit
**remote-first**, moves the checkout, runs `./techsara up`, and then refuses to
call it a success until the stack answers: orchestrator `/health`, the frontend,
and a *real* chat completion (`/v1/models` answers even when the engine is
dead). A `degraded` health status is tolerated — DuckDB legitimately reports an
error while the sync worker holds the single-writer lock — but `app_db` or the
main model being down is fatal. A failed gate rolls back to the commit that was
live before. `down -v` is never used, so no volume is ever destroyed.

**Two repository variables** (Settings → Secrets and variables → Actions →
Variables) control the parts you are most likely to want to change:

| Variable | Unset (default) | Set | What it costs |
|---|---|---|---|
| `DEPLOY_FULL` | `techsara up` recreates only the services whose definition changed. An app-only merge leaves `vllm`, `router`, `ocr`, `reranker` and `embed` running — correct, because those images are pinned upstream and contain none of this repo's code. Merge #17 took **55 s**. | `true` → `techsara down` first, so **every** container is recreated, models included. | **~17 min** end to end; the 27B alone reloads in ~441 s. Volumes are preserved — you pay time, not data. |
| `DEPLOY_BRANCH` | The checkout is left on a **detached HEAD** at the deployed commit. | e.g. `dev` → the checkout is left **on that branch**, fast-forwarded to the deployed commit. | Nothing. Use it because this checkout is also a working directory, and walking into "detached HEAD" after every merge is confusing. |

**`DEPLOY_BRANCH` fast-forwards; it never rewrites.** If the branch is behind
the deployed commit it is fast-forwarded onto it. If it holds commits the
deployed commit does not contain — you have local work, or it has diverged — the
deploy **leaves the branch exactly where it is**, logs the divergence counts from
`git rev-list --left-right --count`, and falls back to a detached HEAD so the
*code being served is still correct*. It never resets, rebases, force-moves or
discards a commit, and it aborts before starting anything if `HEAD` does not end
up exactly at the requested commit. The same rule applies to a rollback: a
rollback moves HEAD to the earlier commit but does not rewind the branch,
because rewinding discards commits.

**A one-off, from the Actions UI.** Actions → *Deploy* → *Run workflow*, which
takes three inputs: `ref` (branch or SHA, default `main`), `full` (tick it to
recreate every container for this run only), and `branch` (the branch to land
the checkout on for this run; blank uses `DEPLOY_BRANCH`, and `-` forces a
detached HEAD). The same knobs exist on the script:

```bash
scripts/deploy.sh --ref main --branch dev --full     # or --dry-run to just resolve
```

The job summary reports the deployed commit, which branch the checkout ended on,
and whether every container was recreated or only the changed ones.

---

## 23. Documentation index

Current (2026-08):

- [`docs/README.md`](docs/README.md) — index and reading order
- [`docs/PORTABLE-RUNTIME.md`](docs/PORTABLE-RUNTIME.md) — launcher design: detection, selection, model policy, state, fallback, upgrades
- [`docs/CLUSTER.md`](docs/CLUSTER.md) — the two-node DGX Spark cluster: measurements, configuration, failure behaviour
- [`docs/CONFIG.md`](docs/CONFIG.md) — effort ladder and reasoning knobs
- [`docs/ARCHITECTURE_CURRENT.md`](docs/ARCHITECTURE_CURRENT.md) — cited recon map (2026-08-19)
- [`docs/00-INVENTORY.md`](docs/00-INVENTORY.md) — repository inventory
- [`docs/01-codebase/infra-docker-compose.md`](docs/01-codebase/infra-docker-compose.md), [`data-model.md`](docs/01-codebase/data-model.md), [`test-map.md`](docs/01-codebase/test-map.md), [`CRITICAL-PATHS.md`](docs/01-codebase/CRITICAL-PATHS.md)
- [`docs/06-agent-design/`](docs/06-agent-design/) — Salesforce Intelligence Mode, the Brain, the analyst-agent design
- [`brain/README.md`](brain/README.md) — how to add knowledge
- [`docs/finetune-postmortem/`](docs/finetune-postmortem/) — why a fine-tune on the personal ChatGPT export was aborted (2026-08-21)
- [`CHANGELOG.md`](CHANGELOG.md) — every change with its reasoning and measurements

Historical (2026-07-31 audit — module-level detail is still useful, but line
numbers, counts, topology and finding status are not current): the rest of
`docs/01-codebase/`, `docs/02-diagrams/` (24 PlantUML sources + renders),
`docs/03-report/` (findings, backlog, quick wins), `docs/04-VERIFICATION.md`,
`docs/ASSUMPTIONS.md`, `docs/_evidence/`.

---

## 24. Known limitations and drift

- **Cluster fabric**: both RoCE links measure ~13 Gb/s per direction for RDMA *and* TCP with clean counters; the cause needs root-level investigation (`mlxlink`, `mlxconfig`, MTU 9000, `iommu.passthrough`) and neither node has passwordless sudo. Until fixed, dual mode is a latency/capacity win, not a throughput win.
- **No pipeline parallelism** for the multimodal `Qwen3_5ForConditionalGeneration` class in the pinned vLLM build.
- **Node 2 runs an unrelated older `sf-local-ai` stack** (Qwen3.6-35B on port 8000) that competes for its memory; it was left untouched.
- **Health semantics**: `/health` may report `degraded` transiently during sync writes (§12); `/v1/models` on the main model answers even when the engine is dead — use `scripts/cluster-status.sh --probe` or a real completion as the truth.
- **Sync worker**: Parquet landing files are never read back and never pruned; a Bulk job stuck `InProgress` polls forever; API-limit warnings do not throttle; a full-size Long Text Area can exceed the CSV field limit and abort that object's extract; `SYNC_AUTO_FIELDS=off` parses as *true* (only `0/false/no` disable it).
- **Docs drift**: `docs/01-codebase/data-model.md` and `sync-worker.md` describe the 48-object era (now ~1,020 objects, deletes *are* propagated, 7 migrations); some frontend docs still say Next 15 and four effort levels (it is Next 16 and three).
- **Housekeeping**: a stray git-tracked DuckDB file named `→` sits at the repo root (a shell-redirection accident); `brain/sources/` contains a duplicated 1 MB knowledge base; two top-level `customer-success-*.txt` files duplicate `brain/sources/`.
- **Reproducibility**: the npm lockfile is committed; the two Python services use ranged requirements without hashes.
- **Single user, no auth** by design (§20).
