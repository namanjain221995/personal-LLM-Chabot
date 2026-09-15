# AS3 integration (2026-09-15)

The five verified tracks were applied in this order: intent-capability, styling-engine, charts, prompt-edits, agentic-selfcheck. The only textual conflict was `orchestrator/app/config.py`; both flag blocks were kept. Everything below is the wiring the tracks left for this commit, plus defects that appeared only when the tracks ran together. Tests: `orchestrator/tests/test_artifact_as3_integration.py`.

## Wiring the tracks left open
- **Chart v2 is the spec's chart.** `spec.Chart`/`spec.Series` are `chart_spec.Chart`/`Series`, a superset, so old `spec.json` files still load. `spec.schema_for(kind, tables=)` splices in `chart_spec.guided_schema`: it has no output fields, `type/title/data` are required, and style is limited to what a person asks for. For documents and decks, the binding's table ids and columns are enums of the material's real tables. `compose.write_section` uses the same guided chart.
- **Renderers.** XLSX (`render/xlsx.is_v2_native`) and PPTX send every non-legacy chart type, and any chart with requested styling, to `render/chart_native` (a native chart, or a picture where the application has none). The "Chart data" sheet is kept last. DOCX/PDF draw every type as an image.
- **Chart images are live formats.** `FORMATS_FOR_KIND` gains `png`/`svg` after the native formats. `formats.decide` reads a png/svg only when chart words are present ("pie chart as png"). A standalone image is named `<title>-v<N>-chart-<n>.<fmt>` and is keyed per chart (the pipeline's `sheet` part). Its old name, `chart-<n>.png`, collided with the renderer's scratch images, which publish deletes.
- **Requested styling on a create.** `engines/artifact.compose_for_pipeline` runs `style.apply_request` on the created spec. A phrase the parser cannot read goes to ONE `style.extract_patch_llm` call, given sheet/column/heading names only. The model's legacy `SheetStyle` gives way: an invented whole-column `highlight` is dropped, and an unrequested light header becomes the house dark header. A CSV asked for with styling the parser can read also gets the XLSX.
- **The binding repair runs.** `_post_process` calls `chart_data.repair_binding` before `resolve_spec`, and runs whenever the spec has a chart, even with no table in the material. A binding to nothing becomes a note, never numbers the model wrote.
- `main.py` passes the UI's owner-checked `artifact_id` to the engine.

## Defects found only in the combined tree
- **Exports failed with the real composer.** `md_import`/`docx_to_document` return a DocumentSpec body; the composer now wraps it in the envelope. Before the fix, every export failed with "The content could not be written".
- **Previous answer.** With `gathered=`, an export read the last assistant turn ("You're welcome!") instead of the substantial answer.
- **Undo.** `lexicon.undo_signal` is a search, so "restore version 1" and "make it like the last version but red" planned as an undo. `edits.undo_signal` now counts only when the whole request is an undo.
- **Style ops.** An empty StylePatch still dumped its list defaults, so "delete rows …" gained a `set_style` op, and "make it landscape" carried the orientation twice.
- **Sections.** `lexicon.strip_style_clauses` removed "background" as a section name, so the composer uses its own verified clause regex.
- **Chart styles.** Fonts came back casefolded, and a bare 3-letter word ("bed") was accepted as a hex colour.
- **Self-check chart values.** A literal (old) chart failed `values_match`; now the file is held to its spec. A sheet chart is recomputed over its own rows. A table this job does not carry is not verifiable, rather than a failure.
- **Header colour.** A requested header colour beats the model's legacy column highlight.
- **Negative numbers.** A conditionally filled column drops the `[Red]` negative number format, which drew red text on the red fill.
- **Style parser, live phrasings.** Now read: "Status red where Blocked", "green where Done", "negative growth in red", "Overdue wale red me", "colour scale on the Score column", "the Risks section paragraphs in italic", "headings in Arial 16".
- **Style model.** It can return conditional rules and colour scales. Its explanations no longer reach the answer as "Not applied" lines.
- **Chart request.** "scatter of Salary vs Experience … in a pdf" was read as no request.
- **New sections.** When writing an inserted section, the model is told that a heading alone is not a section.

## Not done
- The ChatApp host for "Edit with a prompt" is not wired, so the controls stay hidden.
- The `restore` proxy route is not wired.
- The Dockerfile font packages are proposed, not built.
