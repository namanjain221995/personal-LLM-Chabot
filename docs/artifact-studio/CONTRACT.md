# Artifact Studio — the contract

What every part agrees on before any part is built. Code: `orchestrator/app/artifacts/{types,spec,formats}.py` and `orchestrator/tests/test_artifact_spec.py` (the executable half of this document).

## 1. Identities

| identity | minted by | shape | lifetime |
|---|---|---|---|
| `artifact_id` | server, at job acceptance | uuid4 hex (32 chars) | forever; the thing a person edits, converts, reopens |
| `version` | server, per operation | integer from 1 | immutable once published |
| `job_id` | server, at acceptance | uuid4 hex | the unit of work; one job produces exactly one version |
| `idempotency_key` | the chat turn: `sha256(user_id, conversation_id, generation_id or intent_id, operation, normalised instruction)` | text, UNIQUE | a retried acceptance for the same turn returns the same job |
| filename | server, `slug(title)-v<version>.<ext>` | display + download name; never identity | within its version directory |

The model never sees or produces any of these.

## 2. Kinds, formats, templates

- Kinds: `document`, `presentation`, `workbook`.
- Formats: `pdf`, `docx` (document); `pptx`, `pdf` (presentation — the PDF is the canonical preview and a shareable copy); `xlsx` (workbook). Nothing else is promised.
- Templates: document `executive_report | brief | sop | technical_report | research_report | proposal | meeting_summary | generic`; presentation `ceo | training | quarterly_review | generic`; workbook `tracker | dashboard | data | generic`. `TEMPLATE_VERSION` and `RENDERER_VERSION` are stored on every version.
- Format selection when unnamed: `formats.decide()` — explicit wins; otherwise the kind's default set (`docx+pdf` document, `pdf+docx` brief, `pdf` printable, `pptx+pdf` presentation, `xlsx` workbook). The rule that fired is stored in the job's metadata.

## 3. The spec

`ArtifactSpec {spec_version: 1, kind, document | presentation | workbook}` — pydantic v2, `extra="forbid"`. One typed body:

- **DocumentSpec**: title, subtitle, audience, purpose, tone, template_id, cover, toc, confidential, orientation, `blocks[]`, `sources[]`, `assumptions[]`. Blocks: `heading(level 1-3)`, `paragraph`, `bullets`, `numbered`, `table`, `chart`, `callout(note|tip|warning|quote)`, `kpis`, `page_break`.
- **PresentationSpec**: title, subtitle, audience, template_id, `slides[]` with `layout ∈ title|section|bullets|two_column|chart|table|comparison|timeline|kpis|closing`, per-slide `notes`, `sources[]`.
- **WorkbookSpec**: title, template_id, `sheets[]` with typed `columns[] (text|integer|number|currency|percent|date)`, `rows[][]`, `totals[]` (column + fn — the RENDERER writes the formula), `freeze_header`, `autofilter`, `charts[]`.
- **Chart**: `type ∈ bar|horizontal_bar|line|pie`, `categories[]`, `series[{name, values[]}]` — data, never an image or a description.
- **Citation**: `{id, title, url?(http(s) only), retrieved_at?, note?}`; a block's `sources[]` must name ids in the manifest or the spec is invalid.

Refused: unknown block/layout/chart types, extra fields, ragged tables, series/category length mismatch, pie with ≠ 1 series, citations to unknown ids, non-http URLs, formula fields from the model, prose over `MAX_TEXT_CHARS`, a newer `spec_version`. Corrected with a warning: sheet names with forbidden characters, overlong bullets, headings/columns over their caps. `placeholders_in()` finds `lorem ipsum`, `[insert …]`, `TBD`, `TODO` — a spec with any is sent back for one correction.

`schema_for(kind)` is the guided-JSON schema for the model (the body alone). `parse_body(kind, dict)` validates; `validation_summary(exc)` is what goes back to the model for the single repair pass.

## 4. Lifecycle

Job `status` (DB CHECK, metrics `state`/`result`): `queued → running → completed | completed_with_warnings | failed | cancelled`. Position within `running` is `stage ∈ intent | gather | outline | compose | render | validate | preview` (fixed step ids 1–7; `outline` is skipped at Fast and reported as such).

Failure `category ∈ invalid_request | source_unavailable | model_failure | renderer_failure | validation_failure | storage_failure | quota_exceeded | permission_denied | cancelled | dependency_unavailable`; the row carries a safe user-facing `error` and an internal `diagnostic_ref` (log correlation id) — never a stack trace, DSN, host or path.

Durability: the row is inserted before any model call; a worker claims it with a lease (`lease_owner`, `lease_expires_at`) renewed by heartbeat; startup requeues `running` rows whose lease lapsed; each stage writes its output file into the version's working directory so a requeued job resumes at the first stage without an output. A viewer disconnect detaches; explicit cancel is owner-scoped, idempotent, and never deletes a published version.

Publication is atomic: everything is written under `v<N>.tmp/`; after every selected format rendered, reopened and checksummed, `manifest.json` is written and the directory is renamed to `v<N>/`; only then is the version row marked completed. A `v<N>.tmp/` left behind belongs to a failed or interrupted job and the sweep may remove it.

## 5. Effort

| | fast | think | max |
|---|---|---|---|
| thinking | off | on | on |
| outline pass | no | yes | yes |
| research (web, assistant mode, when the request needs current facts) | no | ≤ 5 sources | ≤ 10 sources |
| content review pass | no | yes | yes |
| visual QA (page images → vision model, ≤ 4 pages) | no | no | yes |
| corrections | 1 (invalid output only) | 2 | 2 |
| caps: sections / slides / sheets | 8 / 12 / 3 | 12 / 20 / 5 | 16 / 30 / 8 |

Effort never decides whether a file is made. The UI describes Max as "Highest-quality orchestrated workflow …" and never as AGI.

## 6. Storage

`$REPORTS_DIR/artifacts/<user_id>/<artifact_id>/v<N>/` containing `manifest.json`, `spec.json`, `validation.json`, `<slug>-v<N>.<ext>` per format, `preview.pdf` (the canonical preview for docx/pptx; for a document with a pdf it is that pdf), `previews/<page>-<width>.png` (rasterised lazily from `preview.pdf`, cached). Never under the flat `/reports/<filename>` namespace, which cannot serve nested paths by design.

## 7. What the browser receives

Progress: `step` events `{id: STEP_IDS[stage], title: STAGE_TITLES[stage], status: running|done|failed, detail}` and `status` lines — the existing SSE contract, no new event names. Exactly one final `meta`:

```json
{"route": "artifact", "effort": "think", "artifacts": [ArtifactRef, ...]}
```

`ArtifactRef` (also the API's shape): `{artifact_id, version, job_id, title, kind, status, files: [{format, filename, mime_type, size, sha256, pages|slides|sheets, download_url, inline_url}], preview_kind: pages|grid|none, preview_pages, preview_url, thumbnail_url, warnings[], created_at, operation, parent_version?, status_url}`. Every URL is a relative API path built by code. `meta.report_files` is NOT emitted for artifacts (its names resolve through the flat route, which would 404).

After a reload the card rebuilds from `meta.artifacts`; a non-terminal `status` means the card polls `status_url` with backoff until it is terminal.

## 8. Security invariants

Owner-scoped everything (user_id on `artifacts`, `artifact_jobs`; 404 for another user's id). IDs are the only path components ever derived from a request. The model's text is escaped before HTML; the HTML renderer's fetcher refuses every URL; spreadsheet cells that begin with `= + - @ TAB CR` are neutralised; Office files are written by python-docx/python-pptx/openpyxl (no macros, no external relationships); renderers run in a subprocess with an argument array, a timeout, an address-space limit, a scrubbed environment and the version's working directory as cwd; previews are `Content-Disposition: inline; private, no-store`; downloads are `attachment` with an RFC 5987 filename.
