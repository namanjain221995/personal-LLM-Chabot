# Artifact Studio — architecture

How a sentence in chat becomes a file a person can open, keep, edit and download. Companion to [CONTRACT.md](CONTRACT.md) (what the pieces agree on), [API.md](API.md) (the HTTP surface), [CURRENT_STATE.md](CURRENT_STATE.md) (what existed before and why this shape), [TEST_PLAN.md](TEST_PLAN.md) and [OPERATIONS.md](OPERATIONS.md).

## The path of one request

```
browser  ─ POST /chat "Create a one-page executive brief…" ────────────────────────────────┐
                                                                                            ▼
main.py  worker()                                                                 chat_requests row (intent_id)
  │  clarification / Salesforce Intelligence resolve the text
  │  artifacts/intent.decide()         rules on the RESOLVED text; the classifier only for the ambiguous band
  │  ── branch ABOVE video/documents/agent/search/dataset/plain chat ──
  ▼
engines/artifact.run_artifact_engine()
  │  which artifact a follow-up means     adb.list_artifacts(user, conversation) — by title, by kind, else newest
  │  kind · formats · template             artifacts/formats.decide(): explicit wins, the rule is recorded
  │  material                              conversation (uploads already pinned by main.py), previous answer,
  │                                        ONE guarded SQL query in Salesforce mode, ≤ N web sources at Think/Max
  │  pipeline.accept()  ── DB row BEFORE any model call; idempotency key = intent_id (+ parent) ──
  │  pipeline.ensure_running()  ─────────────────────────────────────────────────────────────┐
  │  subscribe → forward each stage as `step` (fixed ids 1-7) ◄───── _publish ◄───────────┐ │
  │  wait_for → one sentence + ONE meta {route: artifact, artifacts: [ArtifactRef]}        │ │
  ▼                                                                                        │ ▼
_store_answer: messages.meta carries the ref → a reload rebuilds the card            pipeline._run (lease, heartbeat)
                                                                                        │ compose  ── composer hook → compose.compose()
                                                                                        │            outline (think/max) · spec via json_completion ·
                                                                                        │            repair ×1 · placeholders ×1 · content review (think/max)
                                                                                        │            sources manifest CODE-BUILT from the material
                                                                                        │ render   ── subprocess: python -m app.artifacts.render.worker
                                                                                        │            argv, cwd=workdir, scrubbed env, RLIMIT_AS/CPU, killpg
                                                                                        │            html → weasyprint (deny-all fetcher) · docx · pptx · xlsx · charts
                                                                                        │ validate ── every file reopened; zip/rels/macros; page cap; sizes
                                                                                        │ preview  ── page count; page-1 thumbnails at 240 and 1400 px
                                                                                        │ [max]    ── visual review: pages → vision model → revise → re-render ONCE,
                                                                                        │            beside the good files; a failure keeps the good version
                                                                                        │ publish  ── manifest, fsync, os.replace(v<N>.tmp → v<N>), rows in one txn
                                                                                        ▼
                                             /reports/artifacts/<user>/<artifact>/v<N>/{manifest,spec,validation}.json + files + preview.pdf + previews/
                                                                                        ▲
browser  ArtifactCard → ArtifactPanel → PagesViewer / SheetViewer ──── /api/artifacts/… (Next proxy, grammar-checked) ──── artifacts/api.py (owner-scoped)
```

## Where responsibility changes hands

| from | to | what crosses, and the rule |
|---|---|---|
| the words | `intent.decide` | a bounded 4,000-char prefix; deterministic rules; `ambiguous` only for one shape; the classifier's absence means "no file" |
| the turn | `pipeline.accept` | everything the job needs, persisted BEFORE a model call: instruction, kind, formats, template, effort, mode, parent, material.json; refused with a category when over quota, out of space, or over the open-job cap |
| the runner | the composer hook | a `ComposeContext` (material, parent spec, budget, progress); the composer announces `intent/gather/outline` itself and returns an `ArtifactSpec` or raises `StageFailure(category)`; `ModelUnavailable` defers the job |
| the model | `spec.parse_body` | JSON only, constrained by `schema_for(kind)`; unknown fields/blocks/layouts, ragged tables, non-http URLs, NaN, control characters, placeholder text all stop here; the manifest of sources is replaced by the material's |
| the runner | the render worker | one JSON file in the working directory; a separate process with limits; a JSON report or a categorised error back; never the database, never the network |
| the renderers | validation | a file is "made" only when its own library reopens it; PDFs are counted and capped; Office files are checked for macros and external relationships |
| the working directory | the published version | `os.replace` of the whole directory after fsync; the rows say `completed` only after that; a crash between the two is reconciled by `is_published` on the next attempt |
| the API | the browser | ids and integers only in paths; relative URLs built by code on both sides; `inline` vs `attachment` are different actions; page images rasterised once under a process-wide PDFium lock |

## What runs where

| concern | process | bound |
|---|---|---|
| intent, formats, acceptance | the request coroutine | 4,000 chars; O(n) rules |
| compose (model calls) | the runner task under the job's lease | `_max_tokens_for`; ≤ 1 outline + 1 spec + 1 repair + 1 placeholder + 1 review + 1 fix (+1 repair) per compose; pace() against live chat before compose |
| render / validate / preview | a subprocess per job | `ARTIFACT_RENDER_TIMEOUT_S` wall, `ARTIFACT_RENDER_MEMORY_MB` address space, RLIMIT_CPU, `MAX_PAGES`/`MAX_SLIDES`/`MAX_SHEETS`/`MAX_FILE_BYTES` |
| jobs at once | `ARTIFACT_MAX_CONCURRENT_JOBS` (1) | plus `ARTIFACT_MAX_OPEN_JOBS_PER_USER` (3) queued + running per person |
| visual QA | the runner, Max only | `ARTIFACT_QA_PAGES` pages at `ARTIFACT_QA_WIDTH`; one revision pass |
| page images on demand | the API, `asyncio.to_thread` | one PDFium lock process-wide; 2 rasterisations at a time; one render per (version, page, width); `MAX_PREVIEW_PAGES` |
| housekeeping | the maintenance task | requeue lapsed leases, drain the queue, fail jobs queued past the TTL, sweep abandoned `v<N>.tmp` — never a published dir |

## Versions and lineage

`artifacts(id, user_id, conversation_id, title, kind, current_version)` → `artifact_versions(artifact_id, version, job_id, parent_version, operation, instruction, status, files, validation, warnings, …)` → `artifact_jobs(id, …, idempotency_key UNIQUE, lease_owner, lease_expires_at, …)`. An edit or a conversion is a new version under the same artifact with `parent_version` and the instruction; a conversion renders the stored spec with no model call; earlier versions stay on disk and in the listing; nothing is edited in place.

## Effort

Effort never decides whether a file is made. Fast: one model call, thinking off, one repair, one placeholder correction, deterministic validation. Think: an outline first, a content review after, two corrections, web sources when the request needs them and the mode allows. Max: the same plus a visual review of the rendered pages by the vision-capable model with one correction pass — an orchestrated workflow, described to the person as exactly that.

## What was deliberately not built

- pdf.js in the browser: the viewer shows server-rendered page images (one canonical preview for PDF, DOCX and PPTX; the same spec, the same pages), which cost no worker bundle and give the same look everywhere; text selection and search inside the panel are a follow-up.
- LibreOffice: a PPTX/DOCX is previewed from the spec that made it, not by converting the Office file; the downloaded file is the authoritative one and is labelled so.
- Live Salesforce inside the job: Salesforce mode gets one guarded query through the existing SQL engine at acceptance; per-section data plans (the legacy report engine's shape) are a follow-up.
- A per-user fair scheduler: one render slot, first come first served, with a per-person cap on open jobs.
