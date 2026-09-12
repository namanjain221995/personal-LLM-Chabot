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

Five runs on 2026-09-11 against the isolated stack, each on the image rebuilt from the branch at that point. What each run found is a fix with a unit test in the sections above; the run after it is the evidence the fix took.

| run | result | what it found |
|---|---|---|
| Fast #1 (before the security fixes) | brief PDF+DOCX 14.3 s; edit v2 9.5 s; "also as Word" v3 1.8 s (no model call); CEO deck PPTX+PDF 11.5 s; workbook | the blank chart slide and the missing title slide (spec-level relabelling, §B); bullets trimmed on five of six slides (caps in the prompt); "$59/ month" wrapping (KPI shrink) |
| Fast #2 (security fixes) | brief, edit, convert PASS; **deck FAIL** "the layout engine reported an error"; workbook PASS | the rebuilt image resolved `weasyprint>=61` to 70.0, whose fetcher contract a plain function cannot meet; the worker's traceback never reached the log → `AssetFetcher`, the 70.0 pin, stderr logged on an error report (§C, §D) |
| Fast #3 | brief, edit (2 pages, for "shorter"), convert, deck (7 slides) PASS; **workbook FAIL** after 188 s "did not return the document as JSON" | totals counted from 1 ("column 5 is out of range"), the repair ran to max_tokens → totals by header text, positions-from-1 shifted as a set, `finish_reason` recorded and "cut off" said; "shorter means shorter" correction; `material.json` had dropped `previous_answer` and `notes` (§B, §E) |
| Fast #4 | brief, edit (1 page), convert, deck PASS; **workbook FAIL** "could not produce a valid document structure" | `categories: ["Plan"]` — the header where the cells belong — twice → `Sheet._charts_from_columns`; the second validation failure is now logged by field path; the first brief had invented a $49 current price, competitors at $55–65, 1,000 teams and 95% retention → `unsupported_figures` warning at every effort, reviewer hint at Think/Max, the role prompt's NUMBERS rule (§B) |
| **Fast #5** | **PASS, all five turns**: brief PDF+DOCX 12.4 s (1 page; "Not given" where the material had no figure); edit 13.1 s (1 page, warning callout added); Word 2.2 s; CEO deck 6 slides 10.0 s with the warning `figures not in the material (derived or assumed): $10, 20.4%, 5880, 7080`; workbook 10.3 s — 5 typed columns, 3 rows, `=SUM(E2:E4)`, a bar chart over the plan names | — |
| **Think** | **PASS, all three**: brief 282 s (outline 41 s → write → review → 4 corrections), deck 378 s (6 slides), workbook 314 s; every turn ran outline → review → correction with thinking on | before this run a probe showed the outline and review calls ending inside the reasoning block (`json_completion` sized thinking-on calls at the caller's ceiling) → the pool is sized like `stream_chat`; the stage timeout is 900 s. **Looking at the pages**: the brief's correction pass returned one KPI row on an empty page with `template_id` changed to `generic`; the deck's correction replaced its figures with `[Verified …]` placeholders and drew two zero bars → a correction is held against the draft it corrects (§B: gutted / placeholders / emptied → not applied, said on the version), the template is pinned to the request's decision, bracketed phrases are placeholders, an all-zero chart is refused, a hollow first draft is repaired once |
| **Max** | **PASS, all three**: brief 460 s — the review's correction tried to swap the figures for placeholders and was **not applied** (the version says so), the visual review of the rendered pages found a layout fault and its revision was re-rendered beside the good files ("the layout was corrected after a visual check"), 2 pages, 11 blocks, every missing figure written as "Not given"; deck 434 s, 7 slides, the visual check passed with no revision; workbook 323 s, `=SUM`, a chart over the plan names | the guard added after the Think run fired on its first real correction; nothing new |

Measured on the DGX pair (Qwen3.6-35B-A3B-NVFP4, TP=2, chat idle): Fast ≈ 10–14 s per document (one call, thinking off); Think ≈ 280–380 s (three to four calls with thinking on at ~46 tok/s); Max ≈ 320–460 s (Think plus the visual pass, thinking off, ~6 s per look, plus a re-render when it revises). Effort changes depth, never availability — and the deterministic checks (figures, placeholders, hollow, gutted, shorter, caps) run at every effort.

Looking at the rendered pages (not only reopening the files) is what found the blank chart slide, the missing title slide, the invented figures, the gutted brief and the placeholder deck; none of them fails a schema.

## H. Artifact Studio 2 (2026-09-12): CSV, file identity, generated and pasted tables, one card per file

Counts are from `dev` on 2026-09-12; every backend suite ran on a private PostgreSQL database, every render suite with the real libraries; the browser tests are vitest/jsdom; the end-to-end runs used the isolated stack (`scripts/e2e-stack.sh`) with the real model and renderers and `scripts/artifact_smoke2.py`, which reopens every byte it checks.

| area | tests | what is proven |
|---|---|---|
| Intent and formats (`test_artifact_intent.py` 111, `test_artifact_formats.py` 76) | 187 | Scenario A is a create with `csv` and `row_count=500`; Scenario B is a **create of a new artifact** after an existing one (the first-clause rule — every one of the five phrasings that used to become an edit); the four Scenario D questions stay text; the Scenario E follow-ups stay edit/edit/edit/convert; "Share XLSX, Word, PDF, and CSV" → all four in order; "XLSX or CSV" → both; typos (`cvs`, `spread sheet`, `xlxs`, `powerpint`); `raw_text` keeps tabs and newlines |
| Spec (`test_artifact_spec.py` 42) | 42 | `csv` in `FORMATS_FOR_KIND["workbook"]`; `file_id_for` deterministic and 16-hex; `download_name(part)`; refs with and without ids build the right URLs and `download_all_url`; `rows_from`, `generator` (recipes must match the sheet's columns), `rewrite`, `style` validate; a filled sheet keeps its provenance |
| Tables (`test_artifact_tables.py` 92) | 92 | the 34-row messy paste parses to 34 × 9 with 44 blanks in the right cells, no shifted columns, an inch mark cannot merge rows; forward-fill on evidence, recorded per row; markdown, comma and space-aligned tables; the 10k-row / 5 MB cap; date/number helpers; `generate_rows` exactly N, unique, ranges, `only_when` blanks, determinism; `apply_rewrites` keeps the original when a timestamp or a quoted span is lost |
| CSV (`test_artifact_render_csv.py` 21) | 21 | RFC 4180 bytes (CRLF, no BOM, quoting), integral floats, control characters, formula leads neutralised but `-2` / `1,000` / `12%` untouched, `validate_csv` refuses 499 for 500, ragged rows, zero bytes, NUL, a 200,000-char cell; the grid reader's bounds |
| Renderers (`test_artifact_render_*.py`) | 146 → 164 | a workbook to xlsx + csv + docx + pdf from one spec; per-sheet CSVs with part names; `SheetStyle` (thin black borders, bold header, dark header fill, red highlight pairs 9C0006/FFC7CE, wrap, text ids, blanks stay blank); the tabular Word document is landscape with `w:tblHeader`; the tabular PDF is landscape with a repeated header and no blank trailing page; row counts by reopening; `grid_for` for csv and xlsx; `/health` names csv |
| Composer (`test_artifact_compose.py` 37) | 37 | `rows_from` copies the paste verbatim (trailing blank rows left out with a note); a generator makes exactly the rows asked for, the person's count winning; recipes the generator cannot follow become ones it can (the three shapes the first real run failed on); rewrite batches with acceptance and rejection; requested sections checked and corrected once; caps never trim a requested section |
| Engine (`test_artifact_engine.py`) | 18 → 33 | Scenario A end to end with the real renderer (500 rows validated, the dataset sentence); Scenario C (the paste → four files, the transform sentence); tables from the previous turn; uploads from the workspace; every sentence shape, never "Updated" on a create; the `/f/{file_id}` URL |
| Jobs, store, API, chat (`test_artifact_jobs.py` 62, `_store` 18, `_api` 14, `_chat` 6) | 100 | ids minted by the pipeline and stable across a retry; a CSV whose rows differ from the generator's count is refused; legacy rows upgraded on the wire; `transform.json` reaches the render job; `/f/{id}` owner-scoped and 404 before lookup for a bad id; `/zip` streams three entries with the right names and refuses past the bound; `/grid` for csv and xlsx; an artifact turn emits no `memory_updated` and writes no `user_facts` row; a failed first attempt no longer turns the retry into an edit; **a lease orphaned by a restart is requeued within seconds** |
| Browser (`tests/artifact-*.test.tsx`, `file-cards.test.tsx`) | 133 → 207 | one FileCard per file, four cards under one header with "Download all" and the right href/aria-label; legacy `report_files` through the same card; keys unique for two CSVs; card body opens, Download never opens; 680 px clamp; long names truncate with the full name in `title`; keyboard; prev/next order and disabled ends; CSV grid via `grid?file=`; Escape returns focus; the mobile sheet at 400 px; preview error → download fallback; proxy grammar for `f/zip/grid` and its refusals; no memory chip without `memory_updated`; a version without file cards offers Progress/Details |

### End to end (isolated stack, real model, `scripts/artifact_smoke2.py`, Fast)

| scenario | result |
|---|---|
| **A** 500-record CSV dataset | 11 s. One workbook artifact, one CSV (`text/csv; charset=utf-8`), **exactly 500 data rows + header, 11 columns**, unique ids, scores 0–100, completion time only on Completed rows, the grid answers `total_rows: 500`; the sentence: "Created the CSV dataset with 500 validated records."; no fenced CSV in the answer. The first run failed on three recipe shapes the model wrote (`text` without a pool, `choice` without values, `derived` over a column the sheet lacks) — now decided by code with a note |
| **B** the AI-in-Indian-Businesses PDF after a poem | 21 s. A **new artifact at v1** (not v2 of the poem), a 4-page PDF, "Created …", every requested section present (executive summary, use cases, benefits, risks, roadmap, comparison, recommendations, conclusion). Before this branch the same turn published the report as v2 of the poem, as Word, saying "Updated" |
| **C** the 34-row messy audit paste → XLSX, CSV, Word, PDF | 20 s. Four files under one card group with Download all; XLSX 34 rows, bold header, borders on every cell, the "Ratio of Interview Post-Session" header 9C0006 with FFC7CE cells, header frozen; CSV 34 rows with the source's 19 blanks (44 before the 25 evidence-based host fills); Word 34-row table in a landscape section; PDF landscape, 3–4 pages, the highlighted header on every page; the ZIP holds all four with the recorded sizes; the sentence: "Done — I preserved 34 audit rows and created four files. 19 blank source fields stay blank; 25 host names were filled from the row above; comments were rewritten for clarity without changing the findings. The CSV carries the data only; the formatting is in the Excel and Word and PDF files." |
| **D** four questions about CSV/PDF/DOCX after an artifact | all four answered as text, `route=chat`, no artifact |
| **E** deck, "Make slide 4 shorter.", "Add a comparison chart.", "Create a PDF version too." | v2 and v3 "Updated" on the same artifact, v4 "Converted" with a PDF, 13 / 15 / 1 s |
| every artifact turn | no `memory_updated` in the meta |
| **restart drill** (`--restart-during-c`) | the orchestrator container restarted 7 s into the compose; the stream broke; the job was requeued and **published 26 s after the restart** with all four files. The first drill sat for 15 minutes: the startup pass ran before the dead lease had expired and the next pass was the 30-minute sweep — fixed (30 s lease check) |

### Looking at it

`docs/artifact-studio/evidence/2026-09-12-*.png`: the four-file group at 1440 px (cards aligned with the assistant column, header notes once, Download all), the grid panel at 1440 px and 400 px (sticky header, row numbers, column letters, "34 rows · 9 columns"), the deck's page panel (thumbnails, zoom, page 1/9), the CSV dataset card. What looking found and fixed: a "With notes" chip repeated on every card, a one-sheet CSV titled "Book — Sheet", dates shown as `T00:00:00`, a two-line per-cell note ahead of the cards.

### Measured (Fast, DGX pair, chat idle)

CSV dataset 500 rows 11 s (one model call, rows by code in ~10 ms); audit paste → four files 17–22 s (one compose call + one 34-row rewrite batch; render 1.2 s for all four); report PDF 21 s; deck edits 13–15 s; conversion 1–2 s; ZIP of four files served in < 100 ms. Sample sizes: 3–5 runs each, same box, one person at a time.

## Not run

- A real browser at 1440 px with the sidebar open (the panel split is verified structurally in jsdom).
- Two orchestrator processes claiming the same job (the lease is tested in one process with a forged owner).
- The database unavailable mid-stage against a real PostgreSQL outage (tested with a raising accessor).
- The public Cloudflare path (100 MB edge limit is irrelevant here; downloads are small).
- Load: many concurrent people generating at once (one render slot; the per-person cap is tested, fairness between people is not).
- Indic/CJK text rendering (recorded as a warning; not asserted visually).
