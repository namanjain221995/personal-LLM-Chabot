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
| the API | the browser | ids and integers only in paths (artifact ids, versions, 16-hex file ids); relative URLs built by code on both sides; `inline` vs `attachment` are different actions; a ZIP is streamed, never assembled in memory; page images rasterised once under a process-wide PDFium lock |
| a version | the cards | one FileCard per file under a version header; "Download all" only when the server offers `download_all_url`; legacy `report_files` through the same card via an adapter; the panel steps through files and versions |

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

## Data by code (since 2026-09-12)

```
the turn's raw text (tabs, newlines intact) + the last three user turns
  └ tables.parse_table   delimiter by evidence (tab · | · runs of spaces · csv.Sniffer), header = first line,
                          one row per line with its source line number, blanks None, widths padded and recorded,
                          never a row dropped or merged; the largest block in a longer message; 10k rows / 5 MB cap
  └ tables.forward_fill  a leading group column (host/candidate/owner…) filled from the row above only on evidence,
                          each fill recorded → DataTable(id="paste1") in the material + a transform report
composer  ── the prompt lists "TABLE paste1: 9 columns × 34 rows: Host, …" and orders rows_from: "paste1", rows: []
          ── "N sample records" → Sheet.generator (a recipe per column) and rows: []
          ── _fill_code_made_rows BEFORE parse_body: rows copied verbatim / tables.generate_rows(seed) exactly N;
             a recipe the code cannot follow becomes one it can, with a note (text→name/label/choice, derived→range)
          ── _rewrite_columns AFTER the draft: one column, 40-row JSON batches, one reply per row,
             tables.apply_rewrites keeps the original when a timestamp, a quoted span or a figure would change
          ── requested_sections(instruction) checked against the headings → one correction; caps floor
render    ── workbook: xlsx (primary, SheetStyle: borders, bold header, fills, red highlight, wrap, text ids)
             + one CSV per sheet (data; RFC 4180; formula leads neutralised; exactly the rows)
             + docx/pdf companions: one landscape section per sheet, repeated header, methodology note from transform.json
validate  ── every file reopened; xlsx rows per sheet and csv rows vs the spec ("the CSV has 499 data rows; 500 were required")
pipeline  ── file_id = sha1(artifact:version:role:format:sheet)[:16] minted here; a generator count that the CSV does not match is refused
sentence  ── from the published spec and the reopened files: "Created the CSV dataset with 500 validated records." /
             "Done — I preserved 34 audit rows and created four files. 19 blank source fields stay blank; …"
```

The model never types a row that code could copy or generate; the model never sees a file, an id or a URL; the person never reads a sentence whose counts the code did not check.

## Versions and lineage

`artifacts(id, user_id, conversation_id, title, kind, current_version)` → `artifact_versions(artifact_id, version, job_id, parent_version, operation, instruction, status, files, validation, warnings, …)` → `artifact_jobs(id, …, idempotency_key UNIQUE, lease_owner, lease_expires_at, …)`. An edit or a conversion is a new version under the same artifact with `parent_version` and the instruction; a conversion renders the stored spec with no model call; earlier versions stay on disk and in the listing; nothing is edited in place.

## Effort

Effort never decides whether a file is made. Fast: one model call, thinking off, one repair, one correction (placeholders, or an edit that asked for less and got more), deterministic validation. Think: an outline first, a content review after, two corrections, web sources when the request needs them and the mode allows. Max: the same plus a visual review of the rendered pages by the vision-capable model with one correction pass — an orchestrated workflow, described to the person as exactly that.

## What was deliberately not built

- pdf.js in the browser: the viewer shows server-rendered page images (one canonical preview for PDF, DOCX and PPTX; the same spec, the same pages), which cost no worker bundle and give the same look everywhere; text selection and search inside the panel are a follow-up.
- LibreOffice: a PPTX/DOCX is previewed from the spec that made it, not by converting the Office file; the downloaded file is the authoritative one and is labelled so.
- Live Salesforce inside the job: Salesforce mode gets one guarded query through the existing SQL engine at acceptance; per-section data plans (the legacy report engine's shape) are a follow-up.
- A per-user fair scheduler: one render slot, first come first served, with a per-person cap on open jobs.
