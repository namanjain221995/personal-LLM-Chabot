# 00 — Repository inventory

Regenerated on 2026-08-11 after the portable launcher, profiles, manifests,
Compose overlays, tests, and documentation were present.

This is an inventory of the current version-controlled plus unignored working
tree, not a repetition of the 2026-07-31 due-diligence snapshot that previously
occupied this file.

## 1. Method and scope

The file universe was produced with:

```bash
git ls-files --cached --others --exclude-standard
```

It contains **504 paths**. This deliberately excludes `.git/`, ignored `.env`
and backups, `.runtime/`, Python virtualenvs/caches, `node_modules/`, `.next/`,
local data/reports/models, and other generated artifacts covered by
`.gitignore`. The two checked-in ZIP inputs, documentation renders, product
screenshots, and untracked-but-unignored current source are included.

No current LOC or full-suite-pass total is asserted. Both would mix active
working-tree changes with historical measurements and would become stale more
quickly than the path inventory.

## 2. Files by top-level area

| Area | Files | Current role |
|---|---:|---|
| repository root | 11 | wrappers, base/legacy Compose, user template, README/changelog, two input archives |
| `compose/` | 6 | runtime overlays and Windows modifier |
| `config/` | 3 | hardware profiles, model/runtime manifest, native runtime install input |
| `launcher/` | 24 | cross-platform Python implementation, stdlib tests, and one Docker-backed overlay validation module |
| `orchestrator/` | 140 | FastAPI application, core/engines/search, tests, scripts and images |
| `sync-worker/` | 33 | Salesforce ingestion/indexing worker and tests |
| `frontend/` | 115 | Next.js UI/API proxy, state/contract tests and static assets |
| `pgadmin/` | 2 | server registration and legacy passfile helper |
| `searxng/` | 1 | local search configuration |
| `docs/` | 110 | current runtime docs plus historical audit/evidence/diagrams |
| `screenshots/` | 60 | numbered product verification images |
| **Total** | **504** | |

### File types

| Type | Count | Notes |
|---|---:|---|
| Python | 185 | launcher, orchestrator, worker, tests and scripts |
| PNG | 87 | 60 screenshots, 24 rendered diagrams, product assets |
| TypeScript | 63 | frontend libraries/routes/tests |
| Markdown | 39 | root/component/current/historical docs |
| TSX | 36 | frontend page/components |
| SVG | 25 | 24 rendered diagrams plus favicon |
| PlantUML | 24 | diagram sources |
| YAML | 10 | Compose/config/sync data |
| JSON | 5 | npm/config/pgAdmin data |
| Text | 5 | Python requirements and native runtime input |
| Dockerfile | 5 | frontend, worker, legacy/CPU/CUDA orchestrator |
| Other | 5 extensionless, 2 MJS, 2 shell, 2 YML, 2 ZIP, and one each CMD/CSS/CSV/example/MTS/PS1/WebP | wrappers, config and assets |

## 3. Root and portable runtime inventory

### Root (11)

| Path | Status/purpose |
|---|---|
| `.env.example` | user-owned configuration template; no AWS settings or machine-specific model path |
| `.gitignore` | secrets, runtime state, data, model/cache, Python/Node/build exclusions |
| `README.md` | current user quick start, matrix, lifecycle, security and troubleshooting |
| `CHANGELOG.md` | append-only dated product history; portable entry added without replacing current user material |
| `techsara` | POSIX pinned uv/Python bootstrap and CLI wrapper |
| `techsara.ps1` | PowerShell equivalent |
| `techsara.cmd` | Command Prompt delegation to PowerShell |
| `compose.yaml` | current portable application/data-plane base |
| `docker-compose.yml` | superseded pre-launcher DGX topology, kept as the stop/rollback path for stacks started from it; not used by the launcher |
| `AI Friendly All Objects and Fields -Preprod.zip` | checked-in input/reference archive |
| `Preprod All Objects and Fields Zip.zip` | checked-in input/reference archive |

Ignored `.env.bak-*`, `docker-compose.yml.bak-*`, local `.env`, and runtime
state are not repository inventory entries even when they exist on one machine.

### Compose overlays (6)

| Path | Selected for |
|---|---|
| `compose/compose.mac.yaml` | Apple Silicon with native host vLLM-Metal |
| `compose/compose.dgx-spark.yaml` | DGX Spark main/router/embed/OCR CUDA topology |
| `compose/compose.nvidia.yaml` | generic NVIDIA tiers |
| `compose/compose.windows-wsl2.yaml` | modifier after NVIDIA overlay on validated Windows/WSL2 |
| `compose/compose.cpu.yaml` | llama.cpp CPU-minimal inference |
| `compose/compose.external-development.yaml` | app-only and explicit local external endpoints |

### Declarative config (3)

| Path | Purpose |
|---|---|
| `config/hardware-profiles.yaml` | 13 profile declarations and feature/context/model composition |
| `config/model-manifest.yaml` | 16 immutable model entries plus uv, vLLM-Metal and container runtime pins |
| `config/vllm-metal-runtime.txt` | verified direct native artifacts used by runtime install |

## 4. Launcher inventory (23)

### Implementation (12)

| Path | Responsibility |
|---|---|
| `launcher/techsara_cli/__init__.py` | package metadata |
| `launcher/techsara_cli/__main__.py` | module entrypoint |
| `launcher/techsara_cli/cli.py` | commands, staged startup/fallback/reconciliation/state |
| `launcher/techsara_cli/hardware.py` | cross-platform detection and cache selection |
| `launcher/techsara_cli/profiles.py` | manifest loading, budgets, tiers and safe overrides |
| `launcher/techsara_cli/model_manager.py` | revision-pinned cache, resume, disk, validation and markers |
| `launcher/techsara_cli/runtime.py` | native runtime, owned processes and capability probes |
| `launcher/techsara_cli/bridge.py` | authenticated bounded container-to-host proxy |
| `launcher/techsara_cli/compose.py` | exact Compose argv/env, reconciliation and readiness |
| `launcher/techsara_cli/environment.py` | runtime layout, secrets and generated capability env |
| `launcher/techsara_cli/utils.py` | safe subprocess/files/env/locks/download/archive utilities |
| `launcher/techsara_cli/errors.py` | launcher error taxonomy |

### Tests/support (11)

```text
launcher/tests/__init__.py
launcher/tests/support.py
launcher/tests/test_bridge.py
launcher/tests/test_cli.py
launcher/tests/test_compose.py
launcher/tests/test_environment.py
launcher/tests/test_hardware.py
launcher/tests/test_model_manager.py
launcher/tests/test_profiles.py
launcher/tests/test_runtime.py
launcher/tests/test_utils.py
```

The nine `test_*.py` modules use stdlib `unittest`. See
[`01-codebase/test-map.md`](01-codebase/test-map.md) for the current run result
and the mocked/live boundary.

## 5. Application inventory

### Orchestrator (140)

| Subarea | Files | Inventory |
|---|---:|---|
| root/build | 7 | `.dockerignore`, `Dockerfile`, `Dockerfile.cpu`, `Dockerfile.cuda`, `conftest.py`, `requirements.txt`, `requirements-dev.txt` |
| `app/` | 65 | package/root modules plus `core/`, `engines/`, and `search/` listed below |
| `scripts/` | 2 | SQLite→PostgreSQL migration and conversation-title backfill |
| `tests/` | 66 | `__init__.py`, shared conftest, 64 `test_*.py` modules |

Root application modules:

```text
app/__init__.py       app/auth.py          app/compaction.py
app/config.py         app/context.py       app/db.py
app/embedding_index.py app/graph.py        app/health.py
app/history.py        app/llm.py           app/main.py
app/memory.py         app/memory_recall.py app/model_capabilities.py
app/recall.py         app/sse.py           app/summarize.py
app/titling.py        app/uploads.py
```

Core modules:

```text
core/__init__.py      core/archive.py       core/chart_data.py
core/chart_decision.py core/chart_pipeline.py core/chart_profile.py
core/chart_spec.py    core/charts_png.py    core/citations.py
core/docx.py          core/exports.py       core/extract.py
core/net.py           core/org_brief.py     core/pdf.py
core/profile.py       core/repo.py          core/repo_index.py
core/report_paths.py  core/salesforce.py    core/schema_cache.py
core/sf_dictionary.py core/sql_guard.py     core/urls.py
```

Engine/search modules:

```text
engines/__init__.py   engines/agent.py      engines/chat.py
engines/dataset.py    engines/document.py   engines/live_sf.py
engines/ocr.py        engines/orchestrate.py engines/rag.py
engines/repo.py       engines/report.py     engines/router.py
engines/search.py     engines/sql.py        engines/url.py
engines/vision.py
search/__init__.py    search/base.py        search/brave.py
search/searxng.py     search/tavily.py
```

Scripts:

```text
scripts/migrate_sqlite_to_postgres.py
scripts/backfill_titles.py
```

The 64 orchestrator test modules are enumerated by filename in the test map's
suite inventory/coverage groups and are discoverable as
`orchestrator/tests/test_*.py`; no generated tests are hidden from the file
count.

### Sync worker (33)

| Subarea | Files | Inventory |
|---|---:|---|
| root/build | 6 | `.dockerignore`, `Dockerfile`, `conftest.py`, `config.yaml`, `requirements.txt`, `requirements-dev.txt` |
| `syncworker/` | 12 | `__init__`, config, discovery, embedding index, main, objects, RAG index, Salesforce auth/client, secrets, storage, watermark |
| `tests/` | 15 | `__init__.py` plus 14 test modules |

```text
syncworker/__init__.py          syncworker/config.py
syncworker/discovery.py         syncworker/embedding_index.py
syncworker/main.py              syncworker/objects.py
syncworker/rag_index.py         syncworker/sf_auth.py
syncworker/sf_client.py         syncworker/secrets.py
syncworker/storage.py           syncworker/watermark.py

tests/test_bulk_fallback.py     tests/test_chunking.py
tests/test_config.py            tests/test_delete_sync.py
tests/test_discovery.py         tests/test_embedding_integrity.py
tests/test_embeddings.py        tests/test_empty_tables.py
tests/test_jwt.py               tests/test_limits.py
tests/test_objects_cli.py       tests/test_secrets.py
tests/test_upsert.py            tests/test_watermark.py
```

### Frontend (115)

| Subarea | Files | Notes |
|---|---:|---|
| root/build | 12 | Docker/build/lint/Next/Tailwind/TypeScript/Vitest/npm config, lock and README |
| `app/` | 11 | page/layout/style plus auth/chat/history/upload route handlers |
| `components/` | 34 | chat shell, composer/sidebar, reasoning/agent, proof/data/chart/source/UI components |
| `lib/` | 29 | stream/history/cache/API/contracts/formatting/preferences/chart/search helpers |
| `public/` | 5 | favicon/logo/touch-icon assets |
| `tests/` | 24 | Node-environment Vitest contract/state modules |

Root/build files:

```text
.dockerignore        .eslintrc.json       Dockerfile
README.md            next-env.d.ts        next.config.mjs
package.json         package-lock.json    postcss.config.mjs
tailwind.config.ts   tsconfig.json        vitest.config.mts
```

App files:

```text
app/globals.css                        app/layout.tsx
app/page.tsx                           app/api/auth/me/route.ts
app/api/chat/route.ts                  app/api/chat/active/route.ts
app/api/chat/attach/[id]/route.ts      app/api/chat/compact/route.ts
app/api/chat/stop/route.ts             app/api/history/[...path]/route.ts
app/api/upload/route.ts
```

Components:

```text
ActivityPanel.tsx      AgentTimeline.tsx      AttachMenu.tsx
ChartErrorBoundary.tsx ChartView.tsx          ChatApp.tsx
CitationChips.tsx      CodeCitations.tsx      Composer.tsx
ConfirmDialog.tsx      ContextMeter.tsx       ConversationMenu.tsx
CopyButton.tsx         DataTable.tsx          EChart.tsx
EmptyState.tsx         EngineBadge.tsx        FileCards.tsx
Markdown.tsx           MermaidBlock.tsx       MessageRow.tsx
ModelPicker.tsx        PastedChip.tsx         ProofDrawer.tsx
Providers.tsx          ReasoningAccordion.tsx ResearchPanel.tsx
SearchPalette.tsx      Sidebar.tsx            SqlBlock.tsx
SummaryPanel.tsx       TechSaraMark.tsx       WebSources.tsx
icons.tsx
```

Libraries:

```text
attachments.ts        auth.ts               chartFormat.ts
chartOption.ts        chartTheme.ts         citations.ts
composerMenu.ts       contextMeter.ts       conversationMenu.ts
csv.ts                errors.ts             exportMarkdown.ts
feedback.ts           fixtures.ts           format.ts
history.ts            historyApi.ts         historyRoutes.ts
idbCache.ts           mermaid.ts            mockApi.ts
orchestrator.ts       pasted.ts             prefs.ts
proxy.ts              searchPalette.ts      sse.ts
streams.ts            types.ts
```

Frontend tests:

```text
attachments.test.ts          chartOption.test.ts
chat-contract.test.ts        citations.test.ts
composer-menu.test.ts        contextMeter.test.ts
conversation-menu.test.ts    errors.test.ts
export-markdown.test.ts      feedback-server.test.ts
feedback.test.ts             history-cache-engine.test.ts
history-proxy-paths.test.ts  history-server.test.ts
history.test.ts              idbCache.test.ts
mermaid.test.ts              pasted.test.ts
prefs.test.ts                research.test.ts
sse.test.ts                  streams.test.ts
title-generation.test.ts     websearch.test.ts
```

Public assets are `apple-touch-icon.png`, `favicon.png`, `favicon.svg`,
`techsara-logo.webp`, and `techsara-mark.png`.

### Supporting config (3)

```text
pgadmin/servers.json
pgadmin/setup-passfile.sh
searxng/settings.yml
```

`setup-passfile.sh` belongs to the retained historical/manual workflow; the
portable launcher does not execute it.

## 6. Documentation and visual assets

### Documentation (110)

| Subarea | Files | Content |
|---|---:|---|
| docs root | 5 | this inventory, docs index, portable runtime, assumptions, historical verification |
| `docs/01-codebase/` | 13 | README, critical paths, data/API/frontend/orchestrator/security/sync/infra/test maps |
| `docs/02-diagrams/` | 75 | README/style/render script; 24 `.puml`, 24 SVG and 24 PNG files |
| `docs/03-report/` | 4 | improvement report, findings CSV, JIRA backlog, quick wins |
| `docs/06-agent-design/` | 1 | agent-design README |
| `docs/_evidence/` | 12 | historical per-subsystem audit notes |

Current operational documents are:

```text
docs/PORTABLE-RUNTIME.md
docs/00-INVENTORY.md
docs/README.md
docs/01-codebase/CRITICAL-PATHS.md
docs/01-codebase/infra-docker-compose.md
docs/01-codebase/test-map.md
```

Other codebase/report/evidence/diagram pages originated in the 2026-07-31 audit
or later focused updates. Deployment diagrams 02/06/19/21 and their findings
should not be treated as current portable-topology evidence without redraw.

### Product screenshots (60)

`screenshots/` contains the numbered series `01-empty-dark.png` through the
three `56-window-*` and two `57-window-*` variants. There are 60 files because
the 56/57 feature sequences contain parallel variants. They are product
evidence assets, not executable tests or current deployment diagrams.

## 7. Entrypoints and generated state

| Surface | Entrypoint/config |
|---|---|
| portable lifecycle | `techsara*` → `launcher/techsara_cli/__main__.py` → `cli.main` |
| frontend | Next.js `app/page.tsx`, API handlers under `app/api/`, container `node server.js` |
| orchestrator | `uvicorn app.main:app` from CPU/CUDA Dockerfile |
| sync worker | `python -m syncworker.main` |
| application database | PostgreSQL service/`sf-local-ai_pgdata` |
| analytics/vector data | DuckDB/Parquet/LanceDB under `sf-local-ai_data` |
| local search | optional SearXNG Compose profile |
| test runners | stdlib unittest, pytest, pytest, Vitest respectively |

Generated/ignored runtime state:

```text
.runtime/{hardware.json,selected-profile.json,generated.env,secrets.env,
          state.json,capabilities.json,locks/,logs/,pids/}
${TECHSARA_HOME:-~/.techsara}/{bin/,downloads/,runtimes/}
platform model cache/{repos/,huggingface/,*.partial}
sf-local-ai_{data,reports,pgdata,pgadmin,hf-cache} Docker volumes
```

These are operational state, not source files. `techsara down` preserves them.

## 8. Dependency and lock inventory

| Component | Manifest/lock | Current guarantee |
|---|---|---|
| launcher/bootstrap/models/runtimes | `config/model-manifest.yaml`, `config/vllm-metal-runtime.txt` | immutable model revisions, pinned uv/runtime/image artifacts; direct native hashes |
| orchestrator runtime/dev | `requirements.txt`, `requirements-dev.txt` | compatible ranges, no generated Python hash lock |
| sync-worker runtime/dev | `requirements.txt`, `requirements-dev.txt` | bounded ranges, no generated Python hash lock |
| frontend | `package.json`, `package-lock.json` | npm lock present; use `npm ci` |
| containers | five Dockerfiles plus digest-pinned images in current Compose/manifest | base identities pinned; application package closures install ranges |

The vLLM-Metal compatibility fallback can resolve transitive dependencies
without a complete hash-locked/offline closure even though its two direct
artifacts are verified. The production model manifest also does not carry a
SHA-256 for every model weight; immutable revision + required-file validation
must not be described as full per-file hashing.

## 9. Test inventory and current result boundary

| Suite | Test modules | Command |
|---|---:|---|
| launcher | 9 | `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s launcher/tests -v` |
| orchestrator | 64 | `cd orchestrator && python3 -m pytest tests -q` |
| sync worker | 14 | `cd sync-worker && python3 -m pytest tests -q` |
| frontend | 24 | `cd frontend && npm test` |

Only the launcher suite was rerun for this inventory/documentation work. Its
final dated result is maintained in [`01-codebase/test-map.md`](01-codebase/test-map.md)
and must not be combined with historical application results into a synthetic
all-green total.

No repository CI configuration currently runs all four suites.

## 10. Current operational caveats

- `docker-compose.yml` and much of the 2026-07-31 audit remain historical; the
  launcher uses `compose.yaml` plus overlays.
- Normal host application publications are loopback, but Mac container-facing
  authenticated bridge listeners bind host `0.0.0.0` on 18100/18103/18105.
- The application has one stable local identity, not a login/session security
  boundary.
- `TECHSARA_HOME` and `TECHSARA_MODEL_CACHE` must be process environment values
  because they are needed before project `.env` parsing. `HF_TOKEN` may be in
  `.env`; an exported token wins.
- A running project-labeled Docker `vllm` container is an initial warm-capacity
  hint, not the strict native ownership/fingerprint compatibility check; live
  startup/API probing remains required.
- Search provider selection is validated as `searxng`, `tavily`, or `brave`;
  only local SearXNG needs its Compose profile automatically.
- There is no current live cross-platform acceptance record in this inventory.
  The launcher result is mocked unit/contract coverage.
