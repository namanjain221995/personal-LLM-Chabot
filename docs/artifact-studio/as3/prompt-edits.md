# AS3 — prompt-driven edits

A person changes an existing file by asking ("make the headings dark blue", "add a column for owner after Status", "update section 3 with …", "change the title", "make it landscape", "undo that", "restore version 1") and gets the NEXT VERSION of the same artifact with everything else preserved.

Code: `orchestrator/app/artifacts/edits.py`, the "AS3 edits" section of `orchestrator/app/engines/artifact.py`, `compose.write_section` / `compose.revise_section`, `tables.freeze_generated`, `POST /artifacts/{id}/restore` in `artifacts/api.py`, and the browser's `VersionSwitcher.tsx`. Tests: `orchestrator/tests/test_artifact_edits.py` (35), additions to `test_artifact_compose.py`, `test_artifact_api.py`, `test_artifact_tables.py`, `test_artifact_engine.py`, and `frontend/tests/artifact-versions.test.tsx` plus `artifact-lib.test.ts`.

## 1. Why ops, not a rewrite

Before AS3 an edit handed the whole parent spec to the composer and asked for the whole document back. Every edit was a retype: a colour change could reword section 4, drop a citation or lose row 212 of a pasted table. Now the model, when it is needed at all, only names what to change, as a short list of typed operations over an outline of the document. Code applies them. Every block, sheet and slide an op did not touch stays canonical-JSON identical to the parent.

## 2. The flow (engine, before acceptance)

```
edit turn ─► pick the artifact ─► plan ─► apply (code) ─► nothing changed? ─► one sentence, NO job, NO version
                                                    └─► accept (payload in material.json) ─► job: pending sections (≤ 3 scoped calls) ─► render ─► publish
```

- **Picking the artifact.** The UI's `artifact_id` comes first (it must be one of the caller's own artifacts). Then a title the words name, then the artifact of the last artifact turn, then element-word affinity ("column" means a workbook, "slide" a deck, "section" a document), then the newest. Two equally named artifacts get one question and no job.
- **Planning.** Step 1 is the deterministic pre-planner, with 0 model calls. It covers style, orientation, title/subtitle, undo, restore, rename column, rename sheet, rename heading, add a blank column, and delete rows by a named condition. It is taken only when it consumes at least 90% of the instruction's content words. Step 2 is ONE strict-JSON call: thinking off, 800 tokens, 6 s at Fast and 12 s otherwise. Its prompt holds an OUTLINE: headings with section numbers, sheets, columns with types, and up to 12 distinct values of each column that has at most 30. It never holds the body. When that call fails or times out, the plan falls back to `regenerate`, the old whole-document edit, and the sentence then lists the sections that changed.
- **Applying.** Deterministic ops change a working copy, validated after each op. An op that would make the spec invalid is reverted and reported as not applied. `replace_section`, `insert_section`, `replace_slide`, `insert_slide` and `regenerate` become pending writes. The keyed preservation guard then puts back any unit that changed without being touched.
- **The no-op rule (critic correction 1).** `db.create_artifact_job` inserts the version row at acceptance, so a no-op can only be caught before acceptance. If every op was not applied, or the child is canonical-JSON equal to the parent with nothing pending, the turn answers "I didn't change **X** — …" and nothing is written.
- **The payload.** After acceptance the engine writes `{"edit": {spec, pending, applied, not_applied, restore_version, …}}` into the job's `material.json`. That file is scratch and is removed at publication. `pipeline._material()` does not forward unknown keys, so `compose_for_pipeline` reads the raw file, waiting up to 5 s in case the maintenance drain picked the row first. Jobs whose `format_reason` starts with `edit plan` take this path; any other edit job keeps the old path.

## 3. The ops

`set_title, set_subtitle, set_orientation, set_page, set_style{patch}, set_chart{target, patch}, replace_section, insert_section, delete_blocks, rename_heading, add_column{fill: blank|constant|copy_of|derived}, rename_column, rename_sheet, delete_column, reorder_columns, add_rows, delete_rows{where}, update_cells{where, column, value}, set_slide_title, replace_slide, insert_slide, delete_slide, restore_version, regenerate` are pydantic models with `extra="forbid"`, discriminated on `op`. The model's flat JSON is mapped onto them by `_flat_to_op`, which tolerates the odd field placements seen live, such as a new heading in `value` or a cell change written as `rows: [[column, value]]`. An item that still does not validate is rejected and said, never guessed.

`SectionRef` resolves heading text with typo tolerance (NFKC, casefold, SequenceMatcher and token overlap), "section N" (the Nth top-level heading), "last section" and "appendix". A tie, or a best score between 0.45 and 0.62, is `ambiguous target` and asks one question.

## 4. Safety (critic correction 3)

| gate | rule |
|---|---|
| any destructive op (`delete_blocks`, `delete_column`, `delete_rows`, `delete_slide`) | the instruction must contain delete vocabulary: delete, remove, drop, erase, hata do, हटाओ, કાઢી નાખો … |
| `delete_rows` over 25% of the rows or over 50 rows | the condition's value must appear in the instruction |
| `delete_blocks` of more than one section | each section must be named |
| `delete_column` | the column must be named |
| `update_cells` over 200 cells | not applied; one question is asked |
| `update_cells` / `add_column` constant values | the value must appear in the instruction |
| `add_rows` | only rows typed in the prompt, or a paste in this turn (matched by column name) |
| cells | written through the existing renderers: a formula lead gets quotePrefix in XLSX and is neutralised in CSV (tested end to end) |
| names | column names are capped at 64 characters; sheet names are cleaned with Excel's forbidden characters |

## 5. Tables and generators (critic correction 2)

A sheet copied from a paste or an upload is edited in code on its preserved rows. `rows_from` stays as provenance, and `_columns_of_table` is never re-applied. A generator sheet is frozen on its first edit by `tables.freeze_generated`, which rebuilds the rows with the unchanged `generate_rows` and drops the recipe. Seeding is never changed, so restoring an old version reproduces its rows.

## 6. Restore and undo

`restore_version N` creates a new version whose content is version N's `spec.json`, byte for byte (asserted), rendered in the lineage formats with 0 model calls. `undo` restores the current version's `parent_version`. A restore's parent is the version it replaced, so undoing a restore returns to the version before it. The target is the artifact named by `artifact_id`, else the picked one, else a candidate that actually has version N. `POST /artifacts/{id}/restore {version}` does the same from the API. It is owner-scoped, returns 409 when N is current or never finished, keys idempotency on `artifact_id:N:restore:from:v<current>`, and appends a short assistant note carrying the new card to the conversation.

## 7. Formats lineage

- An edit inherits the union of formats across the artifact's published versions.
- A convert keeps the formats and adds the new one (engine and API).
- A style request on a CSV-only workbook adds XLSX and says: "The CSV carries the data only; the formatting is in the Excel file."

## 8. The sentence

The sentence reads `Updated **X** v3: headings dark blue (#1F3864); Owner column added.`, followed by `Not applied: …` (at most 2), `Not confirmed in the file: …` (at most 3, from `progress.selfcheck.unmet` once selfcheck merges), and the data-only clause. A pending section that could not be written is dropped from the change list and said. A restore reads `Restored **X** to v1 — saved as v4.`. The word "Updated" never appears when nothing changed.

## 9. Security

- **SVG (critic correction 5).** Every SVG response is `Content-Disposition: attachment`, with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` and nosniff, whatever disposition was requested; detection is by type or by `.svg` extension. PNG stays inline.
- **The browser.** The browser previews images only through `<img src>`.
- **The edit route (critic correction 4).** There is no `/edit` route. The UI edit box dispatches `techsara:artifact-edit {artifactId, text}` (or calls the host's `onEditPrompt`), and the host sends a normal chat turn with `artifact_id`.

## 10. Composer changes

- (a) `requested_sections` reads `strip_style_clauses(instruction)`, from the lexicon when it is merged or a local fallback otherwise, and is skipped for edits. `_NOT_SECTION_WORDS` gained colour, emphasis, orientation and font words, and a phrase containing `<n>pt` is never a section. The production-shape sentence now yields `[]`.
- (b) `_KIND_GUIDE` gets `chart_spec.prompt_guide` only, once charts merges.
- (c) `_reconcile_sources` keeps the parent's citations on edits.
- (d) `material_from_history` keeps the newlines of the last substantial answer.
- (e) A first H1 equal to the title is dropped, and `numeric_columns` are inferred by code.
- (f) `revise_section` gives a scoped selfcheck repair for edit jobs, guarded by `restore_pending_guard`.
- At Max, the visual reviewer never runs a whole-document revise on a planned edit (critic correction 6).

## 11. Measurements (2026-09-15, this worktree)

- **Pre-planner coverage.** 25/26 authored style prompts (15 document, 11 workbook, in EN, Hinglish and Hindi) are taken with 0 model calls. The 26th, "sheet with colors: header dark blue", goes to the model.
- **Preservation on 26 edits (13 on a 9-section document, 13 on a 4-sheet workbook).** The spec guard found 0 violations. File-grounded, 0/26 mismatches: DOCX paragraph texts of every untouched section and XLSX cell values of every untouched sheet equal the parent's rendered files.
- **Destructive gates.** 10/10 seeded over-reaching plans for "add a column for owner" were blocked with 0 rows lost. "delete rows where status is Closed" deleted exactly the 8 Closed rows of 60.
- **Live planner.** Qwen3.6-35B-A3B at Fast, model path forced on the same 26 prompts, 28 calls, concurrency 1:
  - 22/26 plans applied cleanly on the first run.
  - Three failures were field placements. After the tolerant mapping, a re-run (3 calls) applied 2; the third reply omitted the source column and is now read from the words "copies Value" (unit-tested on the recorded reply).
  - The remaining one ("make the header row bold") is a correct no-op, because the header is already bold.
  - 0 plans contained a destructive op the instruction did not ask for.
  - Planner latency: p50 0.62 s, p95 0.93 s. Two scoped section writes took 2.04 s and 1.62 s and changed only their section.
- **Accept-to-published.** For a deterministic orientation edit of a ~12,800-token document with real DOCX and 14-page PDF rendering, p50 was 0.54 s over 5 runs (in-process render; production adds the render subprocess and the preview rasteriser).
- **Suites (Python 3.11, CI plugin, private Postgres 18).** The 21 test_artifact_* files: 776 passed, 1 skipped (the SVG route test, until charts adds the format). test_fast_lane_route + test_fast_lane_classifier: 313 passed. The frontend `artifact-*` suites passed 222 tests, and `tsc --noEmit` is clean.

## 12. Integration owed by other files (not in this track's ownership)

- `main.py` (intent-capability track) must pass `artifact_id=request.artifact_id` and `gathered=` into `run_artifact_engine`. The engine accepts both today.
- `frontend/components/ChatApp.tsx` must listen for `ARTIFACT_EDIT_EVENT`, or pass `onEditPrompt` through `MessageRow` → `ArtifactCards`, and send the text with `withArtifactId(body, artifactId)`.
- `frontend/app/api/artifacts/[[...path]]/route.ts` needs a `restore` POST mapping for the API route to be reachable from the browser. The UI's restore button already goes through the chat path and does not need it.
- `artifacts/types.py` (charts track) adds `png` and `svg` to FORMATS and MIME_TYPES. Until then an SVG is never served at all; the header rule is tested on the response builder.
- `artifacts/store.py` (selfcheck track) may add nothing: the payload lives inside `material.json`, which is already scratch.
- `artifacts/style.py` (styling track): until it merges, styling of documents and decks is reported as not applied, and a workbook takes only the bold header and the four whole-column highlight colours of `SheetStyle`. Once `style.StylePatch`, `style.merge` and a `style` field on the body specs exist, `set_style` merges into `spec.style` with no change here.

## Verifier corrections (2026-09-15)

An adversarial pass (42 cases in `orchestrator/tests/test_artifact_edits_verify.py`) found and fixed:

- **Compound requests were swallowed by the pre-planner.** Its free-text captures ran to the end of the sentence. "change the title to Q3 Review then delete the Appendix section" set the title to that whole sentence with 0 model calls, and the delete was lost. A capture that contains a clause join (`,` `;` and / then / also / but / aur / और / અને), and is not quoted, now drops the deterministic match, so the model planner reads the whole request. Live (Fast, 8 calls): all compound prompts planned correctly.
- **Restore words inside an edit became a byte restore.** "use version 1 numbers in the Scope section" discarded the request. A restore is a byte copy only when the restore words cover ≥ 90% of the request (`edits.restore_request`). Otherwise a version-named edit is planned against version N, which is what it meant before AS3.
- **Deletes by location.** "remove the typo in the Scope section" could delete the whole section. "remove the blanks from the Score column" could delete the column. "remove Closed from the Status column" could delete rows. A section or column delete now needs the name used as the object of the delete, not after in/from/of (or before ka/में/માં). A row delete whose column is named only as a place, with no row noun, asks first.
- **Sheet charts went stale after row or column edits.** A chart filled from a sheet's cells is now refilled from the rows after every sheet op, so the XLSX chart ranges match the rows. A series whose column is gone is dropped, with a note.
- **A missing payload fell back to a whole-document rewrite.** A restore job now restores from its `format_reason`. Any other edit is re-planned over the job's parent spec.
- **"Background" disappeared as a section.** `requested_sections` returned [] for "a report with background, findings and recommendations". "background" is no longer a styling word, and colour words strip a clause only when they are used as styling.
- **Dead UI controls.** "Edit with a prompt" and "Restore vN" are hidden until a chat host handles them: the host either passes `onEditPrompt` or calls `registerArtifactEditHost()`.
