# Artifact Studio — API

All routes require the session cookie (`require_user`); every artifact and job is looked up **with the caller's user_id in the WHERE clause**, so another person's id answers 404 exactly like a missing one. The Next.js proxy at `frontend/app/api/artifacts/[...path]/route.ts` forwards `GET`/`HEAD`/`POST` with the cookie, passes `Range` through, and never exposes the orchestrator address to browser code.

## Orchestrator (`orchestrator/app/artifacts/api.py`, prefix `/artifacts`)

| method | path | answers |
|---|---|---|
| GET | `/artifacts?conversation_id=` | `{artifacts: [ArtifactRef (current version)]}` — the caller's artifacts, newest first; filtered to one conversation when given |
| GET | `/artifacts/{artifact_id}` | `{artifact: {artifact_id, title, kind, current_version, created_at, updated_at}, versions: [ArtifactRef…]}` |
| GET | `/artifacts/{artifact_id}/v/{version}` | one `ArtifactRef`, plus `validation` and `assumptions` |
| GET/HEAD | `/artifacts/{artifact_id}/v/{version}/file/{format}?disposition=inline\|attachment` | the file. `attachment` (default): `Content-Disposition: attachment; filename="…"; filename*=UTF-8''…`, `Content-Length`. `inline`: `Content-Disposition: inline`. Both: correct MIME, `Cache-Control: private, no-store`, `ETag: "<sha256>"`, `Accept-Ranges: bytes`, `Range` honoured with `206`/`416`. |
| GET/HEAD | `/artifacts/{artifact_id}/v/{version}/preview` | `preview.pdf` inline, same headers as above |
| GET | `/artifacts/{artifact_id}/v/{version}/preview/{page}.png?w=240\|1400` | one rasterised page (1-based), `image/png`, generated on first request and cached under `previews/`; 404 past `preview_pages` |
| GET | `/artifacts/{artifact_id}/v/{version}/sheets?sheet=&rows=200&cols=50` | workbook grid: `{sheets: [{name, rows: N, cols: M}], sheet: {name, columns: […], rows: [[…]], truncated: bool, formulas: {"B12": "=SUM(B2:B11)"}}}` — values as stored, formulas shown as text, never evaluated |
| GET | `/artifacts/jobs/{job_id}` | `{job_id, artifact_id, version, status, stage, stage_title, progress: {detail, elapsed_s}, attempt, failure_category?, error?, artifact?: ArtifactRef (when terminal and published)}` |
| POST | `/artifacts/jobs/{job_id}/cancel` | owner-scoped, idempotent; `{status}`. A completed version is never deleted. |
| POST | `/artifacts/jobs/{job_id}/retry` | re-queues a `failed` job as a new attempt of the SAME version (no new artifact); `{job_id, status}` |
| POST | `/artifacts/{artifact_id}/convert` `{format}` | a new version with `operation: convert` rendered from the stored spec; `{job_id, artifact_id, version}`. Refuses a format the kind cannot take with 400 and a sentence. |

Edits happen through chat turns (`"make slide 4 shorter"`), which resolve the reference and create a new version with `operation: edit`, `parent_version`, and the instruction recorded.

Errors: `{detail: "<one sentence a person can act on>"}`; never a stack trace, DSN, host or path.

## Frontend proxy (`/api/artifacts/...`)

- Path segments are validated: only `[a-z0-9]{32}` ids, `v/<int>`, `file/<pdf|docx|pptx|xlsx>`, `preview`, `preview/<int>.png`, `sheets`, `jobs/<id>`, `cancel`, `retry`, `convert`. Anything else is 404 before an upstream request is made.
- Forwards `Cookie`, `Range`, `If-None-Match`; returns upstream `Content-Type`, `Content-Disposition`, `Content-Length`, `Content-Range`, `Accept-Ranges`, `ETag`, `Cache-Control`.
- Responses under `/api/artifacts/` carry no `X-Frame-Options: DENY` override need: the viewer renders page images and a grid, never an iframe.

## Chat

`POST /chat` is unchanged. When the turn is an artifact request the worker emits `step` events (ids 1–7) and `status` lines while the job runs, then the single final `meta` with `route: "artifact"` and `artifacts[]`. A disconnected viewer re-attaches through the existing `chat_requests` intent machinery; the job itself never depends on the viewer.

## Health and metrics

`/health` gains `work.artifacts: {queued, running, oldest_queued_age_s}` and a dependency check `artifacts: {status, renderers: {pdf, docx, pptx, xlsx}, volume_writable}`. Prometheus: `artifact_jobs_total{result}`, `artifact_stage_seconds{stage}`, `artifact_render_seconds{format}`, `artifact_queue_depth{state}`, `artifact_corrections_total{stage}`, `artifact_preview_seconds`, `artifact_download_total{format}` — no user, filename or id labels.
