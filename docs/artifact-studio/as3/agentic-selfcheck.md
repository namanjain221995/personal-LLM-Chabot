# AS3 track: agentic self-check

The self-check reads every rendered file back and compares it with a checklist of what the person asked for. When a must-item fails, it tries one bounded repair. Anything still unmet is reported honestly. It runs in `artifacts/pipeline.py` after render → validate → preview → visual QA, and before PUBLISH.

## Modules

| File | Role |
|---|---|
| `orchestrator/app/artifacts/requirements.py` | Checklist: rule extractor (A), model proposer (B), merge (C) |
| `orchestrator/app/artifacts/inspect_files.py` | Observations taken from the produced bytes only (DOCX/XLSX/PPTX XML, pypdfium2 PDF objects, CSV text, PNG pixels, SVG scan) |
| `orchestrator/app/artifacts/selfcheck.py` | Evaluate, plan the repair, apply the strict acceptance rule, report, metrics, trace |
| `orchestrator/app/artifacts/pipeline.py` | `_try_revision` (set-aside/restore, shared with visual QA), `carry_code_owned`, the `_selfcheck` hook. Every change sits inside `AS3 agentic-selfcheck` markers |
| `orchestrator/app/artifacts/store.py` | `SELFCHECK_NAME`, published but never downloadable (`NOT_DOWNLOADABLE_NAMES`) |
| `orchestrator/app/config.py` | `ARTIFACT_SELFCHECK`, `ARTIFACT_SELFCHECK_REPAIR`, `ARTIFACT_SELFCHECK_BUDGET_{FAST,THINK,MAX}_S` (20/60/90). These are real Settings fields |
| `orchestrator/scripts/as3_selfcheck_score.py` | Independent scorer (LibreOffice → PDF, poppler words/fonts/pixels, raw XML). It shares no code with the inspector |

## Vocabulary

Each item is `(category, target, property, expected)`. The category enum is closed: format, content, style, chart, layout, data, language, faithfulness, preservation, house_style, security. A checklist holds at most 40 items. The full list of targets and properties is in `tests/fixtures/selfcheck/requests_labelled.py`.

A colour given by name ("dark blue", "lal", "नीला") is stored with `locator.color_name` and `shade`. It matches any colour in the same hue family (hue within 25°, plus a shade class). A hex code the person typed must match exactly, with ±2 per channel allowed. The reason: the style guide says renderers use text-safe or fill variants of a named hue. For example, a red highlight is rendered as `#FFC7CE`.

## Decisions that differ from the design (and why)

1. **Format items for formats the job did not select become should-items and are never repaired.** Example: in "convert this pdf to word", the format word names the source file. `formats.py` is the contract for which formats a job makes. The first version of the repair added the extra format as a new file, and an existing jobs test caught it.
2. **A file the checker cannot parse is `unverifiable`, not `fail`.** The validate stage already reopened the file with that format's own library.
3. **The rule extractor does not use `style.parse_style_request` for its decisions.** The styling track is not merged. The extractor has its own grammar, and the style parser, if present, is consulted only to add items. This is the stronger form of the circularity correction.
4. **Edit preservation compares files, not only specs.** A section whose spec is unchanged must have identical file text to the parent version's file. When the edits track writes `progress.edit.touched`, a changed section outside that set fails at spec level.
5. **Cell coverage is checked as a multiset when the format has real tables (DOCX/PPTX).** A repeated value such as "Open" can no longer hide a dropped row. A live-style seeded case (`blank_status_cell`) found this gap.

## Measured (2026-09-15, worktree on dev 5043001, Python 3.11, CI plugin)

**Tests.** `tests/test_artifact_*.py` (all artifact suites): **859 passed, 2 skipped** against a private Postgres. The new suites are `test_artifact_selfcheck.py` (38), `test_artifact_requirements.py` (21), `test_artifact_inspect_files.py` (68, including 61 seeded defect pairs) and `test_artifact_store.py` (+1). `test_artifact_jobs.py` changed in one assertion: the published directory now also holds `selfcheck.json`.

**Checklist on the labelled set** (40 requests, 154 items): rule-only recall **1.00**, precision **1.00** for style/format/layout/chart. Caveat: the same agent wrote the labels (frozen before the extractor) and the extractor, so this number is biased upward and is **not** the blind measurement the design asked for. With the live proposer (Think, 40 calls): recall 0.994, precision 0.915, contested share 0.5 %. Some proposer calls errored and fell back to the rule items. On 10 seeded misparses (the rule reader forced to misread, the proposer reading correctly): **10/10 contested**, none passed.

**Inspector.** 61 file-level defects, injected by editing OOXML parts, openpyxl/python-pptx objects or pdfium page objects: **61/61 detected, 0/61 false fails on the twins**. Properties the files cannot show (PPTX page numbers, DOCX chart type) come back `unverifiable`. Formula-like text without `quotePrefix`, unexpected formulas, non-http(s)/mailto external rels and unsafe SVG are all flagged.

**Repair.**
- Strict acceptance rule: 10/10 unit combinations.
- 10/10 end-to-end seeded repairs that fix one item and break another (renamed or dropped headings, dropped rows, changed cells) are rejected, and the pre-repair DOCX/PDF bytes publish unchanged (sha256 checked).
- Orientation is repaired by code with 0 model calls.
- The style repair path needs the edits/styling tracks. Without them, style items are reported as unmet, not repaired.

**Budgets.**
- Fast: 0 model calls (asserted).
- Think: 1 proposer call + 1 content repair (asserted ≤ 2).
- Checklist + inspect + evaluate on a 40,000-character export (DOCX+PDF): **0.26 s**. Re-render for a repair: about 1.8 s.
- Self-check wall time for the 20 live jobs: 0.00 to 0.36 s each (outside the re-render).

**Live end to end** (20 jobs, real composer at Fast, real renderer, 20 model calls; samples under `scratchpad/as3/agentic-selfcheck/samples/live`):
- As run: 1 repaired (landscape), 7 clean, 12 unmet.
- **Independent scorer, as run: 0 refuted of 28 scorable claims (false-satisfied 0 %)**, 4 unscorable.
- The 10 unsatisfiable requests (colours, fonts, margins, row fill; styling not merged): **10/10 name the unmet item and are completed_with_warnings.**
- One false `unmet` was found live: "Risks" against the heading "2. Risk Assessment". It was fixed with stemmed word matching, and PDF headings are now observed. After re-evaluation with the fixed checker (no new model calls): 32 claims, 0 refuted, 10/10 unsatisfiable named.
- The scorer's first run refuted one claim. That was a scorer bug: it located the first word "Hazard" in a heading instead of in the table header. The scorer was fixed to match whole phrases on one row. This is disclosed because the fix came after seeing the result.

**Production-shape export** (synthetic 40k-character audit plus the typo'd "provide a dox file" with landscape / dark blue headings / Georgia body):
- One DOCX. Faithfulness passes: all headings and all table cells.
- No style words became sections.
- Landscape is repaired by code.
- "Headings dark blue" passes: the default navy headings are dark blue.
- The Georgia body font is **unmet** and named in the warnings.
- Sample renders are in `samples/offline/{before,after}-repair`. The LibreOffice render of the repaired DOCX is landscape, 31 pages, with a navy title and headings, navy table headers with white bold text, and "Page 1 of 31" in the footer. The PDF before repair was portrait, 24 pages.

## Not done / open

- `T.STAGES` (types.py, charts track) has no `check` stage. The hook publishes `stage: "check"` events, but `engines/artifact.py` forwards only known stages, so the timeline does not show it yet.
- `engines/artifact._sentence` (prompt-edits track) does not yet read `progress.selfcheck.unmet` or `false_claim_guard`. The honesty contract currently holds through the job warnings, and the card shows them.
- Style/layout code repair through `edits.ops_for_style`/`ops_for_layout` and `compose.revise_section` is written against the interface only. Those modules are not in this worktree, and the adapter is guarded.
- `chart_data.recompute_matches` is used when present. Without it, chart values are compared with the spec's series.
- Visual QA carry of `spec.style` and chart bindings is tested with dict-level fakes. It cannot be tested end to end until the spec models carry these fields.
- The blind labelled set (a different author) and a held-out set are still owed. The recall of 1.00 above is not blind.
- `.env.example` documentation of the five new variables (file owned elsewhere).

## Adversarial verification (2026-09-15)

An independent verifier wrote 41 cases of its own (`scratchpad/as3/verify-selfcheck/cases/harness.py`): over-trigger traps, typo'd and multilingual creates, edits, per-element styling in each file type, formula injection, print layout and charts. Each case rendered real files and ran them through inspect and evaluate. 10 cases failed on the patch as built. These fixes followed:

- **Preservation always passed.** "Untouched" meant "the spec did not change", so a model that rewrote a section the person never named passed by construction. Nothing in this base supplies `progress.edit.touched`. Preservation now reads the request:
  - Sections, sheets and slides can be named by their words or by position ("section 3", "the last slide").
  - Content of the parent that is missing or changed in a unit the request did not name fails.
  - When no unit is named, one changed unit is allowed, because that is the edit itself.
  - A whole-file edit ("shorter", "translate", "undo") is unverifiable.
- **The content repair lost the job's tables.** The default repairer rebuilt `Material` without `tables`, `sources`, `row_count` or `transform`, so a model revision would retype rows or invent them. It now carries the whole material.
- **Crash inside a revision.** A restart mid-revision left the good files in `.before-revision/` beside a half-built candidate. The next attempt deleted that directory or published it. `_run_stages` now puts the set-aside files back before any stage runs, and `_try_revision` never deletes a non-empty set-aside directory.
- **Summary exports held to every cell.** Condensed exports ("one page summary of this", "short deck with the key points", "saransh") and every presentation now record the faithfulness items as should-items.
- **Rule extractor false items:**
  - "bar chart of apple vs orange sales" produced a series colour. A colour must now sit next to a mark noun.
  - "titled Regional Sales" produced a title style item. Latin element words now end at a word boundary.
  - "plot sales by month as a line" produced no chart type.
  - "line of credit ... chart" produced a line chart.
  - "a column for joining date" was read as column `jo`, and "status" as `st`, which a header "Start date" satisfied. The column name is now read whole.
  - "slices in red and green" dropped green.
- **"Blue" was met by near-black navy.** A colour name without a shade now needs a lightness between 0.15 and 0.90. "Dark blue" is still met by #0A1D37.
- **A missing value in a chart was a false mismatch.** The native chart draws a blank source cell as a gap. That gap now matches a 0 in the spec's series.

Open and not fixed here:
- The DOCX title style inherits `w:spacing w:val="5"` (0.25 pt letter spacing) from the python-docx default template. The house-style should-item `no_letter_spacing` fails on every DOCX. This is the renderer's problem.
- Styling a CSV does not require the XLSX companion unless the engine selected it.

## Fix round B2: false "unmet" on correct files (2026-09-15)

The integration live run reported items as unmet on files that were right. All causes below were reproduced on the live outputs. After the fix, re-evaluating the 506 stored items of those live versions (with the same checklists and files; preservation and faithfulness items are excluded because they need the parent version or the source answer) changed 12 results, all from a false fail to a pass, and changed nothing else.

- **A section-scoped style was checked as a global one.** "The Risks section paragraphs in italic" was applied correctly and reported as "not met: body text italic — docx: 8 of 9 show False".
  - The rule extractor now reads the section scope itself, without using `style.py`. It understands "the Risks section paragraphs", "paragraphs in the Risks section", "section called Next Steps", "section 2" and "Risks section ke paragraphs". The scope goes into `locator.section` or `locator.section_index`. When the word "paragraphs" is used, the item also gets `paragraphs_only`.
  - The DOCX and PDF inspectors now attach the heading path (`sections`) and the level-1 ordinal (`section_index`) to every element. They mark bullet and numbered items as `list`. The PDF inspector does this only when the spec's headings are known.
  - A scoped item is judged on its section only. If the file has no such section, or the reader cannot place text in sections, the result is unverifiable, never a pass. A global request on a file styled only in Risks still fails.
- **"A filled header row" failed on 8 of 8 workbooks that had a Notes or Chart data sheet.** The bold A1 label on those sheets ("Assumptions", a chart title) was read as the header row. The renderer's own `Notes`, `Notes-N` and `Chart data` sheets are no longer read as tables. A spec sheet that is itself called "Notes" still is. Formula text on those sheets is still scanned.
- **Page numbers failed on every landscape PDF.** The header and footer band was 7.5% of the page height, which is 45 pt on landscape A4, while the footer sits at 46–55 pt. The band is now measured on the long side of the page.
- **A PDF underline was never seen.** WeasyPrint draws `text-decoration: underline` as a stroked two-point line inside the glyph box, but only filled rectangles below the box were read. Such stroked rules now count as an underline when they sit below the baseline band and are no wider than the text, so table borders and strike-throughs are excluded.

Real file states seen in the same run, not self-check errors (owned by styling, B3):
- In `task-tracker`, the header cell C1 "Status" has a static fill of #9C0006.
- In `vendor-spend-tracker` and `facilities-cost-tracker`, A1 has a fill of #1F4E78 while the other header cells have the requested fill.
