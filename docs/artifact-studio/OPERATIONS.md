# Artifact Studio — operations

## Switches

| setting | default | what it does |
|---|---|---|
| `ARTIFACTS_ENABLED` | `true` | off: a request for a file is answered in text; the API answers 404; the sweep still runs |
| feature `artifacts` (admin console, per member) | on | off for a member: text answers, 403 on the API |
| `ARTIFACT_MAX_CONCURRENT_JOBS` | 1 | render slots in this process |
| `ARTIFACT_MAX_OPEN_JOBS_PER_USER` | 3 | queued + running jobs one person may hold |
| `ARTIFACT_LEASE_TTL_S` | 90 | a job whose heartbeat stops for this long is requeued — the lease check runs every 30 s (`pipeline.REQUEUE_INTERVAL_S`), so a job orphaned by a restart is back in the queue within TTL + 30 s; until 2026-09-12 it waited for the 30-minute sweep |
| `ARTIFACT_STAGE_TIMEOUT_S` | 900 | any stage past this fails (compose is `max(this, LLM_RECOVERY_WINDOW_S + 60)`); a Think compose measured 252 s with thinking on for three calls |
| `ARTIFACT_RENDER_TIMEOUT_S` / `ARTIFACT_RENDER_MEMORY_MB` | 180 / 2048 | the render subprocess's wall clock and address space |
| `ARTIFACT_MIN_FREE_MB` | 512 | acceptance and render refuse below this much free space on the reports volume |
| `ARTIFACT_USER_QUOTA_MB` | 2048 | per-person ceiling, measured on disk (files, preview copy, page-image cache) |
| `ARTIFACT_TMP_TTL_HOURS` | 24 | working directories of failed jobs, and jobs queued this long, are removed |
| `ARTIFACT_QA_PAGES` / `ARTIFACT_QA_WIDTH` | 4 / 896 | Max-effort visual review: pages sent to the vision model, downscaled |

## Where the files are

`$REPORTS_DIR/artifacts/<user_id>/<artifact_id>/v<N>/` — on the `sf-local-ai_reports` volume the legacy `/reports` files share. `manifest.json`, `spec.json`, `validation.json` (0600), the rendered files, `preview.pdf`, `previews/<page>-<width>.png`. `v<N>.tmp/` is a job in progress or a failed one awaiting retry; it holds `material.json` — the conversation text the job composes from — and is removed by the sweep after the TTL, by cancel immediately, and by publication.

Deleting a user cascades through `artifacts` → `artifact_versions` / `artifact_jobs` in the database; the directory is the operator's to remove (`rm -rf $REPORTS_DIR/artifacts/<user_id>` after the row is gone). Deleting a conversation does not delete its artifacts (the same rule as `report_files`: a deliverable outlives the chat that made it).

## Health and metrics

`GET /health` → `artifacts: {status, renderers: {pdf, docx, pptx, xlsx}, volume_writable, free_mb}` and `work.artifacts: {queued, running, oldest_queued_age_s}`.

Prometheus: `artifact_jobs_total{result}`, `artifact_stage_seconds{stage}`, `artifact_render_seconds{format}`, `artifact_queue_depth{state}`, `artifact_oldest_queued_age_seconds`, `artifact_corrections_total{stage}` (`visual` is the Max pass), `artifact_preview_seconds`, `artifact_download_total{format,result}`, `artifact_lease_steal_total`. Label values are closed vocabularies; nothing user-controlled reaches a label.

Worth alerting on: `artifact_queue_depth{state="queued"} > 5` for 10 minutes (a stuck slot), `artifact_oldest_queued_age_seconds > 1800`, `rate(artifact_jobs_total{result="failed"}[1h]) / rate(artifact_jobs_total[1h]) > 0.3`, `artifact_lease_steal_total` increasing (a process died mid-job).

## Logs

Prefix `artifact job <8 chars> [<diagnostic ref>]`. A person-facing error never carries a path, a host, a DSN or a traceback; the `diagnostic_ref` on the job row (visible in `GET /artifacts/jobs/{id}`) is the key into the log. The render worker's stderr (its traceback, when there is one) is logged at WARNING as `artifact render worker reported <category>: …` whenever the worker reports an error, and at ERROR when it crashes or is killed — the line just before the job's `stage render failed` line.

## Restarts and deploys

A rolling deploy recreates the orchestrator: running jobs lose their process, their lease lapses within `ARTIFACT_LEASE_TTL_S`, the new process requeues them at startup and every 30 s after (the restart drill of 2026-09-12 caught a job stuck for the 30-minute sweep because its lease had not yet expired at startup), and each resumes at the first stage without an output file — a job interrupted during render does not compose again. The chat turn re-attaches through `chat_requests` and its acceptance is keyed on the intent id, so the retry finds the same job. Nothing about a deploy touches published versions.

`deploy-rollback.sh` refuses to roll below V31 (the migration is additive: three tables, no changes to existing ones).

## Rendering dependencies

`python-pptx==1.0.2`, `python-docx==1.1.2` (pure Python; `lxml` was already present), `openpyxl`, `matplotlib`, `weasyprint==70.0` (was already installed for pandoc's PDF path; the Artifact Studio drives it in-process with a deny-all URL fetcher — pinned because the fetcher contract changed between 69 and 70 and an unpinned rebuild broke every deck PDF in the container while the venv passed; move the pin with the renderer suites), `pypdfium2` (page images; NOT thread-safe — every call takes `core.pdf.PDFIUM_LOCK`). Fonts are the image's Liberation and DejaVu families; Indic, CJK and Arabic text renders as missing glyphs and is recorded as a warning on the version, not a failure. `GET /health` reports which renderers import.

## Runbook: a job that will not finish

1. `GET /artifacts/jobs/{job_id}` as the owner — `status`, `stage`, `progress.detail`, `failure_category`, `error`, `diagnostic_ref`.
2. `grep '<diagnostic_ref>' <orchestrator log>`.
3. `running` with an old `heartbeat_at` → the lease lapsed; the next maintenance pass (≤ `ARTIFACT_MAINTENANCE_INTERVAL_S`) requeues it. `queued` past the TTL → failed by the sweep with "never built"; ask again.
4. `renderer_failure` with `capabilities` showing a renderer false → the image is missing a library; rebuild.
5. `POST /artifacts/jobs/{id}/retry` re-runs a failed job as the same version; `cancel` stops a queued/running one and never touches a published version.

## Smoke

`VIDEO_SMOKE_EMAIL=… VIDEO_SMOKE_PASSWORD=… scripts/artifact_smoke.py --base http://127.0.0.1:8081 --effort fast --follow-ups` against the isolated stack (`scripts/e2e-stack.sh up`): a brief, an edit, a conversion, a deck and a workbook, every file downloaded and reopened by its own library, every preview page fetched. `--via-frontend http://127.0.0.1:3001` takes the browser's path through the proxy. `--effort max` exercises the visual review.
