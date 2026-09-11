# Artifact Studio — test plan and evidence

Every row names the test that exists, or says "not run". Counts are from `feat/artifact-studio` on 2026-09-11. Backend tests run offline against a private PostgreSQL test database (`TEST_DATABASE_URL=…/test_<name>`), with the model stubbed and — except in the renderer suites — the render subprocess replaced by a writer of real bytes; the renderer suites use the real libraries and reopen every file they write. Browser tests are vitest/jsdom.

## A. Intent (`tests/test_artifact_intent.py`, 55 cases; `test_artifact_spec.py` format policy, 11)

| case | test |
|---|---|
| explicit PDF / DOCX / PPTX / XLSX | `test_requests_that_must_create_a_file[…]` (21 phrasings, formats asserted) |
| generic "document", "best format", "all required files" | same table |
| edit / convert / export previous artifact or answer | `test_follow_ups_on_an_existing_artifact` (13), `test_going_back_to_a_version`, `test_follow_up_words_without_an_artifact_are_not_edits` |
| casual mention of PDF must not create | `test_requests_that_must_not_create_a_file` (14), `test_a_format_named_in_conversation_is_not_a_request` |
| named artifact wins over the latest; short titles ignored | `test_a_named_artifact_wins_over_the_latest`; engine `test_the_named_artifact_is_edited_not_the_latest` |
| ambiguous band → classifier, else no file | `test_the_ambiguous_band_is_narrow_and_defaults_to_no_file` |
| a 100 KB adversarial message | bounded prefix + bounded gap; measured 3 ms (security review re-run) |
| format policy table | `test_format_policy` (11), `test_template_follows_the_task_words` |

## B. Spec and composer (`test_artifact_spec.py` 20, `test_artifact_compose.py` 17)

Round trip; unknown block/layout/chart types refused; ragged tables, series/category mismatch, pie with two series; citations to unknown ids; non-http URLs; NaN/inf; control characters scrubbed; sheet names with forbidden characters and apostrophes; placeholders found; layouts relabelled from content. Composer: Fast = one call thinking off; invalid JSON repaired once with field paths; two invalid answers = `model_failure`; placeholder correction bounded; Think = outline → spec → review → correction, thinking on; clean review = no correction; edit carries the parent; caps trim with a warning; visual review sends images with thinking off; the classifier's confidence floor; **the sources manifest is code-built and an injected source never reaches the page**.

## C. Formats (renderer suites, 111 tests, real libraries)

| format | what is proven |
|---|---|
| PDF (`test_artifact_render_pdf.py`, `_html.py`) | every template renders and reopens with a page count; a loopback `<img>` produces NO request (fetcher spy); no blank trailing page after a `page_break`; `MAX_PAGES` enforced; every model string escaped; KPI values shrink instead of wrapping |
| DOCX (`_docx.py`) | reopened by python-docx; named styles; header/footer with PAGE/NUMPAGES fields; cover; tables; pictures; no field codes, hyperlinks, OLE or external relationships from text |
| PPTX (`_pptx.py`) | reopened by python-pptx; slide count; native chart and table; ten layouts; notes; footer; bullets fitted with a warning; chart labels never formulas |
| XLSX (`_xlsx.py`) | reopened by openpyxl; number formats; freeze panes; autofilter; totals as real `=SUM(...)`; native charts; `=HYPERLINK`, `+cmd`, `-2+3`, `@SUM` become text everywhere including the Dashboard sheet |
| validation (`_validate.py`) | zip integrity; vbaProject refused; external relationships refused; PDF header/page count/media boxes |
| the render interface (`_version.py`, `_samples.py`) | every kind × every format via `render_version`; the worker round-trips a job.json; `capabilities()` truthful |

## D. Durability (`test_artifact_jobs.py` 51, `test_artifact_store.py` 16)

Idempotent acceptance; lease claim / steal / release; heartbeat cancels on a lost lease; requeue at start; resume at the stage without an output; atomic publish (no `v<N>` on failure; `v<N>.tmp` swept after the TTL, never a published dir; a queued job's directory kept, a job queued past the TTL failed with a sentence); cancel owner-scoped, idempotent, never deleting a published version; retry re-attempts the same version; quota refusal (measured on disk); a stranger's ids answer None; edit → version 2 with `parent_version` 1; health counts; the render subprocess protocol, scrubbed env, limits, timeout kills the group.

## E. The turn and the API (`test_artifact_engine.py` 18, `test_artifact_chat.py` 3, `test_artifact_api.py` 8)

One meta with `artifacts[]` and never `report_files`; steps with fixed ids; the sentence, never the document; explicit format wins; policy and template from the words; edit → v2 keeps formats; convert renders the stored spec with no model call; an impossible conversion becomes a new artifact unless the person said "convert"; a refused acceptance says so and writes nothing; a failed job is reported with its safe error and the reference; export carries the previous answer; **a resumed turn under the same intent finds its job (one artifact)**; **open jobs capped per person**; **Max runs exactly one visual correction, Fast never looks, a failing correction keeps the good version**. `POST /chat` end to end through the real app: one meta, the durable row carries the reference, a question about PDFs stays text, the deployment switch. API: owner scoping on every route (a stranger sees 404), ids that are not ids are 404 before any lookup, `inline` vs `attachment`, RFC 5987 filename, ETag, Range 206/416, HEAD, page images rasterised once and cached, the sheet grid bounded and formulas as text, job status, cancel, retry (409 for a job that did not fail), convert (422 for a malformed body, 409 for a format the version has, the key names the artifact).

## F. Browser (133 tests)

Proxy: traversal, encoded segments, unknown routes and methods are 404 before any fetch; headers forwarded; a POST body over 1 KiB is 413 declared or not. Card: every field; Download distinct from Open; URLs built only from validated ids. Panel: focus in on open, back to the card on close, Escape scoped, status announced, desktop split vs mobile sheet. PagesViewer: lazy pages, object URLs released, fetches aborted on switch. SheetViewer: tabs, formulas marked, truncation notice. MessageRow: cards from `meta.artifacts`, nothing otherwise. Polling stops on terminal and on unmount.

## G. End to end, real model, real renderers (isolated stack, `scripts/artifact_smoke.py`)

| effort | result |
|---|---|
| fast, with follow-ups | brief → PDF (1 page, 14 KB) + DOCX (13 paragraphs, 1 table) in 14.3 s; "make it shorter, add a warning callout" → v2 in 9.5 s; "also as Word" → v3 in 1.8 s with no model call; CEO deck → PPTX (6 slides, 46 KB) + 6-page preview in 11.5 s; every file reopened by pypdfium2 / python-docx / python-pptx; every preview page fetched. The workbook turn and the re-run after the fixes are recorded in `.runtime/artifact-smoke-*.json`. |
| think / max | see the re-run section appended below after the final image |

Looking at the rendered pages (not only reopening the files) is what found the blank chart slide and the missing title slide; both are now spec-level corrections with tests.

## Not run

- A real browser at 1440 px with the sidebar open (the panel split is verified structurally in jsdom).
- Two orchestrator processes claiming the same job (the lease is tested in one process with a forged owner).
- The database unavailable mid-stage against a real PostgreSQL outage (tested with a raising accessor).
- The public Cloudflare path (100 MB edge limit is irrelevant here; downloads are small).
- Load: many concurrent people generating at once (one render slot; the per-person cap is tested, fairness between people is not).
- Indic/CJK text rendering (recorded as a warning; not asserted visually).
