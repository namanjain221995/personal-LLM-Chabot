# AS3 — Charts of any type, computed by code

Track: `charts`. Status: built and tested in an isolated worktree (2026-09-15); not merged, not deployed.

## What it does

A person asks for a chart ("pie of status", "plot sales by month as a line", "stacked bar Q1/Q2 by region", "scatter with a trend line", "histogram", "heatmap", "gantt-style timeline", in English, Hinglish, Hindi, Gujarati, or with typos). The model writes a **binding**: which table and which columns, what aggregation, which chart type, and only the styling the person asked for. **Code** computes every number from the real table. Renderers draw the result as native XLSX/PPTX charts, as PNG in DOCX, as SVG on the PDF/HTML path, or as standalone PNG/SVG files.

The model never writes a number that appears on an axis.

## Modules

| File | Role |
|---|---|
| `orchestrator/app/artifacts/chart_spec.py` | Chart v2 model. It is a superset of `spec.Chart`: `Binding`, `ChartStyle`, output fields, `ChartPatch`/`ChartRef`/`apply_patch`, `guided_schema(tables=)`, `chart_from_model`, `prompt_guide`, `CONTAINER_SUPPORT` |
| `orchestrator/app/artifacts/chart_data.py` | Computes the numbers: `resolve_spec`, `resolve_spec_async`, `resolve_chart`, `compute`, `repair_binding`, `recompute_matches`, `parse_prompt_data`, `tables_from_markdown`, date/number parsing |
| `orchestrator/app/artifacts/render/charts.py` | matplotlib renderer for all 20 types: `render_png`, `render_svg`, `render_standalone`, back-compat `render_chart_png`, `chart_warnings`, `inspect_figure` (tests) |
| `orchestrator/app/artifacts/render/chart_native.py` | `add_xlsx_chart` (openpyxl, "Chart data" sheet) and `add_pptx_chart` (python-pptx, plus combo/trendline XML) |
| `orchestrator/app/artifacts/render/validate.py` | `validate_png`, `validate_svg(_bytes)`, `count_native_charts`, `expected_charts=` on xlsx/pptx, `chart_data_mismatches` |
| `orchestrator/app/artifacts/types.py` | `png`/`svg` formats, `IMAGE_FORMATS`, `role_for_format` |

## Chart v2

- **Types (20).** Tier 1: `bar`, `horizontal_bar`, `stacked_bar`, `stacked_horizontal_bar`, `percent_stacked_bar`, `line`, `area`, `stacked_area`, `pie`, `donut`, `scatter` (with trendline), `histogram`, `combo` (dual axis). Tier 2: `box`, `heatmap`, `waterfall`, `funnel`, `gantt`, `radar`, `bubble`.
- **`data: Binding`.** Fields: `table_id`, `x`, `y[]`, `agg` (sum|avg|count|min|max|median|none), `group_by`, `date_bucket`, `date_order` (auto|dmy|mdy|ymd), `bins`, `filters` (at most 5; text at most 100 chars; numbers finite), `sort`, `top_n` (at most 50), `other_bucket`, `y2[]`, `start`/`end`/`label` (gantt), `size` (bubble), `trendline`.
- **`style: ChartStyle`.** Fields: palette, `color`, `series_colors`, `category_colors`, font family (allowlist), title/axis/legend/data-label text styles, legend position, data labels on/off/auto, number format (closed enum), y/x min/max, log y, gridlines, background, width/height/dpi. Colour names resolve in EN/Hinglish/hi/gu. `style.resolve_color` and `style.FONT_ALLOWLIST` take over once the styling track merges.
- **Output fields.** `categories`, `series` (with `x`, `sizes`, `axis`, `kind`), `extra` (bin edges, box stats, gantt spans, trend lines, heatmap rows) and `provenance` (table, provenance kind, rows used/total, agg, filters, date order/bucket, sampled, engine). They are marked `readOnly` and removed from the guided schema.
- **Legacy charts.** A chart with literal numbers and no `data` still loads, which keeps old `spec.json` files working. `resolve_spec` blocks such a chart in a **new** spec unless `allow_literal=True` is passed.

### The guided schema

`guided_schema(tables=[...])` has these properties:
- It requires `type`, `title` and `data`.
- Column fields are **enums of the real column names**: numeric columns for `y`/`y2`/`size`, date columns for gantt `start`/`end`, and table ids for `table_id`. The decoder cannot name a column that does not exist.
- Binding fields are ordered with `group_by` right after `x`, and type-specific fields come last. Each type-specific field says which chart type it belongs to.
- `style` offers only `color`, `series_colors`, `category_colors`, `legend_position`, `data_labels`, `number_format`, `y_min`, `y_max` and `log_y`.

The live pilot showed why: when the whole schema was offered, the model filled width, dpi, background and fonts that nobody asked for.

`chart_from_model(raw)` drops a style value that does not validate (for example `color: "pie"`) and adds a note, instead of losing the chart. `repair_binding(chart, tables, instruction)` is deterministic:
- It drops fields that belong to other types.
- It moves a stray `y2` to `y`.
- It turns a text `y` on a grouped chart into `group_by` with a count.
- On a grouped type, or a line/bar over dates, it fills a missing `group_by` when exactly one low-cardinality text column is named in the request or the title.

It never invents a column or a number.

## Computing the numbers (`chart_data`)

- **Engines.** Up to 200,000 rows go through pandas. Up to 2,000,000 rows go through duckdb over the same frame. Beyond that, the first 2,000,000 rows are used and a note plus a caption clause say so. The caps are module constants that tests lower to exercise each path.
- **Aggregation.** Sum/avg/count/min/max/median, per `(x, group_by)`. When `top_n`, pie slices (at most 7, so 6 + Other) or the 200-category cap fold categories into "Other", code **re-aggregates the folded rows**, so an average of "Other" is a real average and not a sum of averages. More than 8 groups become 7 + Other.
- **Dates.** Day-month order is decided per column:
  - any first field > 12 → dd-mm;
  - any second field > 12 → mm-dd;
  - every value ambiguous (or mixed forms) → dd-mm, with a note.

  An explicit `date_order` wins. Buckets are day/week/month/quarter/year. The automatic bucket is day up to about 2 months, month up to 3 years, then year. More than 200 buckets coarsen to the next size, with a note.
- **Other types.** Histogram uses `numpy.histogram_bin_edges` (auto, capped at 50 bins). Box plots use numpy percentiles with Tukey 1.5 IQR whiskers. Scatter/bubble drop incomplete rows (with a count) and sample to 5,000 points by stride after the trend line is fitted. The trend line is `numpy.polyfit(deg=1)` with R². Waterfall keeps the natural order and draws a running total. Funnel sorts by value. Gantt reads start/end through the same date policy. Heatmap pivots `x × group_by`, capped at 50 × 50.
- **Ordering.** Dates and numbers sort ascending. Month and weekday names keep calendar order. Categorical bars, pies and funnels sort by value, largest first. Other types keep first-appearance order.
- **Column binding.** Matching tries, in order: exact; case, space and underscore insensitive; a close match (with a note); a unique containment (with a note). A missing column or table makes the chart a **callout** in a document (for example "The column 'Zone' was not found in sales_daily.csv."), a bullet on a slide, or a dropped chart on a sheet. Each carries a note.
- **Captions** are built from provenance when absent, for example "Sum of Amount by month · sales_daily.csv · 150 rows". Filters, sampling and table provenance are appended. A table from an earlier assistant answer (`answer<N>`) is always labelled "from the assistant's earlier answer", even under a caption the model wrote. Prompt-typed data is labelled "figures typed in the request".
- **Typed figures → `prompt<N>`.** `parse_prompt_data` handles these forms: `Jan 10, Feb 12`, `north 120, south 95`, `x = 1,2,3 and y = 4,5,6`, `(1,52) (2,55)`, `Rent 40%`, `key: value` lines, Devanagari/Gujarati digits, Indian and Western grouping (`1,20,000`), and suffixes (`k`, `lakh`, `crore`, `million`, `हजार`, `લાખ`, …). Labels are words that each start with a letter, so a number never hides inside a label.
- **Markdown answer tables → `answer<N>`.** `tables_from_markdown`.
- **`recompute_matches(chart, tables)`.** Selfcheck uses it: it recomputes the chart and compares categories, series names and values (rel_tol 1e-9). A literal chart is reported as not verifiable.
- **Before the spec swap.** `resolve_spec` works on pydantic specs (envelope or body) and on their dicts. Until `spec.Chart` is swapped for `chart_spec.Chart`, a pydantic spec that cannot hold v2 fields is downgraded: the four legacy types keep their computed numbers, and other types become a note.

### The event loop

`resolve_spec` is synchronous and CPU-bound. Async callers must use `await resolve_spec_async(spec, tables, timeout_s=15)`, which is `asyncio.to_thread` under `asyncio.timeout`. It does not use `wait_for`; see memory `ci-python311-waitfor-hang`. The same deadline is passed into the thread so a timed-out computation stops at its next check. The test resolves 200,000 rows through the async wrapper while a 10 ms heartbeat runs, and asserts the heartbeat never gaps by 100 ms or more.

## Rendering

### PNG and SVG (`render/charts.py`)

- **Defaults.** Style-guide tokens are used unless `ResolvedStyle.chart_defaults` exists (duck-typed). The palette order is `#2F6FB2 #E07B00 #0E9D9A #C0566B #6D5AE6 #319047 #993F94 #B38C15`. The test measures the first five as pairwise at least 25 apart in CIELAB under simulated deuteranopia (Machado 2009, severity 1), and all eight against the OKLCH lightness band, the 0.10 chroma floor and an adjacent normal-vision OKLab ΔE of 15. Which colour each mark gets is `chart_colours.scheme_for`, consulted only after every explicit field of `chart.style`; the document-wide part of it is `ResolvedStyle.chart_plan`.
- **Report-graphic defaults.** Whole-number data gets integer ticks. The category axis has no tick marks. Value gridlines (#E8ECF1) come off when every bar already carries its number. The title (and subtitle) aligns to the figure's left edge, not the plot box's. Category labels wrap to two lines before anything is rotated, and rotate to 45 degrees only when the drawn labels still do not fit the slots they have (measured against the axes width, not counted in characters); the bottom legend is then placed under them. Stacked bars print their total.
- **Bars and data labels.** Bars start at zero unless `y_min` is set. Labels appear with 12 or fewer categories, placed outside bars in ink. Stacked segments get inside labels only where white or ink reaches 4.5:1 on that segment. Pie and donut percentages use white or ink, whichever reaches 4.5:1; slices under 3.5% get no inside label. Heatmap cell labels are also white or ink at 4.5:1.
- **Lines.** Three or more line series get distinct markers and dash styles. More than five series get direct end labels and no legend.
- **Numbers.** Thousands separators everywhere. Indian grouping (`₹12,34,567`) applies with `currency_INR`.
- **Layout.** Bottom legends are placed under the tick labels as actually drawn. Captions sit below everything. A title equal to the section heading is not repeated. Width follows orientation: 6.3 in portrait, 9.7 in landscape, 200 dpi. Standalone images are 1600 × 900 with the caption.
- **Fonts.**
  - The fallback order is: requested font → its metric twin and documented fallback (Georgia → Gelasio → Caladea → Liberation Serif; the same mapping the PDF uses, see styling-engine.md "Font mapping") → Carlito/Calibri → Liberation Sans → DejaVu Sans. An installed Devanagari or Gujarati font is added when the text uses that script.
  - `chart_warnings()` returns one sentence per script with no installed font.
  - matplotlib does not shape Indic conjuncts perfectly. The text is legible; DOCX/PPTX native text is not affected.
- **SVG output.**
  - `svg.fonttype='path'`: there are no `<text>` nodes.
  - A fixed `svg.hashsalt`, so output is deterministic.
  - No metadata, DOCTYPE or comments.
  - The heatmap uses `pcolormesh` and a non-rasterised colorbar, so the SVG contains no embedded `<image href="data:…">`.
- **Mathtext** is off.

### Native charts (`render/chart_native.py`)

- **XLSX.** Values go to a visible "Chart data" sheet (tab `#5F6B7A`, kept last by `keep_chart_data_last`). It holds one block per chart: title row, header row, then rows. Text cells use the formula-neutralising writer: `data_type 's'` plus `quotePrefix`.
  - Types: bar family, histogram (gap 0), line, area, pie, donut, scatter (with `c:trendline`), combo (bar plus a line on a secondary axis), radar and bubble.
  - Styling: series `srgbClr`, per-point `dPt`, axis `numFmt` `#,##0` with `sourceLinked=0` (`0%` for percent-stacked), `txPr` fonts, legend position, `outEnd` data labels, stacked inside labels only at 4.5:1, axis min/max/log, gridline colour, and `delete=0` on axes.
- **PPTX.** Labels have formula leads stripped before XlsxWriter embeds them.
  - Types: `CategoryChartData`, `XyChartData` and `BubbleChartData` with the same type set.
  - Combo and trend lines are written into the chart XML directly: the line series are moved into a `c:lineChart`, with a secondary `c:catAx`/`c:valAx` and `crosses=max`.
- **Image fallback.** Box, heatmap, waterfall, funnel and gantt are placed as a PNG. The result reports `mode="image"` with a note sentence. `expected_native_counts(charts, fmt)` gives the count the validator should see.

**For the styling track to integrate:**
- `render/xlsx.py` and `render/pptx.py` call `add_xlsx_chart` / `add_pptx_chart` for v2 charts.
- `render/docx.py` embeds `render_png` output at 200 dpi.
- `render/html.py` and `pdf.py` write `render_svg` output as `<name>.svg` in the assets dir, and `pdf.make_url_fetcher` serves `.svg` from assets only. That one-line change belongs to the styling track.
- `render/__init__.py` dispatches `png`/`svg` formats to `render_standalone`.

## Validation and security

- **PNG.** It must have the signature and `IHDR`. It must be between 400 × 300 and 8000 × 8000 pixels, read from `IHDR` without decoding, so a dimension bomb is refused before it inflates.
- **SVG (`validate_svg_bytes`).**
  - These are refused **before parsing**: `DOCTYPE`, `ENTITY`, `ELEMENT`, `ATTLIST`, `NOTATION` and non-xml processing instructions. The stdlib parser therefore never expands an entity (XXE, billion laughs).
  - These are refused after parsing: `script`, `foreignObject`, `iframe`, `object`, `embed`, `handler`, `listener`, `audio`, `video` and `canvas` elements; `on*` attributes; `href`/`xlink:href` to anything but `#fragment`; `url()` to anything but a fragment; `javascript:`; `@import`; CSS `expression(`; and `<set>`/`<animate>` that target `href` or `on*`.
  - The corpus in `tests/fixtures/charts/svg_corpus/` has 10 malicious files and is refused 10/10. The fragment-only file is accepted.
  - Serving headers (attachment plus a CSP sandbox) are the prompt-edits track's part, in `api.py`.
- **Native counts.** `validate_xlsx/pptx(expected_charts=N)` fails when the number of chart parts differs.
- **XLSX data.** `chart_data_mismatches(path, charts)` compares the "Chart data" cells with the computed values at rel_tol 1e-9.

## Formats (`types.py`)

- `FORMATS` adds `png` and `svg`.
- STAGED (verifier 2026-09-15): `FORMATS` and `FORMATS_FOR_KIND` are unchanged in this patch. The image formats live in `CHART_IMAGE_FORMATS_FOR_KIND` (document `png, svg`; presentation `png`; workbook `png`) and join `FORMATS`/`FORMATS_FOR_KIND` in the integration commit that also lands the render/__init__ png/svg dispatch and api.py's SVG attachment + sandbox-CSP headers. Before that, api.py would accept a png/svg convert that render_version refuses, and three existing tests (spec, formats, api) pinned the old tuples.
- `MIME_TYPES["svg"] = "image/svg+xml"`.
- `IMAGE_FORMATS = ("png", "svg")`.
- `role_for_format(kind, fmt, formats)`: an image is a `companion`, or `primary` when the version holds only images.
- No `*.sql` CHECK constraint lists formats. To re-verify before merging, grep the V-migrations for `formats`/`role` CHECKs.

**Two tests outside this track assert the old tuples and must be updated by their owners:**
- `tests/test_artifact_spec.py::test_every_kind_maps_to_real_formats_only` (styling track): `FORMATS_FOR_KIND` equality, and `PAGE_FORMATS ∪ GRID_FORMATS == FORMATS`, which becomes `∪ IMAGE_FORMATS`.
- `tests/test_artifact_formats.py::test_a_workbook_carries_word_and_pdf_but_a_document_carries_no_grid` (intent track): the workbook tuple.

## Fixtures and tests

- **`tests/fixtures/charts/`.** Built by `make_fixtures.py` with a fixed seed; everything is synthetic.
  - `tickets.xlsx`: 60 rows, status 24/14/11/8/3.
  - `sales_daily.csv`: 150 rows; monthly totals 64,514 / 71,319 / 74,708 / 58,936 / 63,092 / 77,350 / 23,072.
  - Other tables: `employees.csv`, `projects.csv` (dd-mm dates), `cashflow.csv`, `funnel.csv`, `units.docx` (Word table), `headcount.pdf` (for material_in), and `dates_dmy.csv` / `dates_mdy.csv` / `dates_ambiguous.csv`.
  - `ground_truth.json` is computed in plain Python, independently of pandas/duckdb.
  - `svg_corpus/` holds the SVG security corpus.
- **`tests/fixtures/chart_requests.py`.**
  - 71 authored requests (26 non-English or typo) with accepted types, a ground-truth key or check, an oracle binding, and a `score()` that checks VALUES and requested styles.
  - 50 `PROMPT_DATA_CASES`.
- **Test files:** `test_artifact_chart_spec.py`, `test_artifact_chart_data.py`, `test_artifact_chart_native.py`, `test_artifact_chart_requests.py` (offline oracles; live run opt-in with `AS3_LIVE=1`), and the updated `test_artifact_render_charts.py` and `test_artifact_render_validate.py`.

## Known limits

- The live model often omits `group_by` or a date filter ("Q1 and Q2"). It also reads "sales" as `Units` when a table has both `Units` and `Amount`. `repair_binding` catches the named-second-dimension case; the ambiguity needs the composer to ask or to state its reading in the answer sentence.
- matplotlib's Indic shaping is approximate.
- PPTX `bubbleScale` is ignored by LibreOffice; Excel and PowerPoint honour it.
- LibreOffice renders openpyxl percent-stacked axes with the `0%` format correctly. Excel is not available here to compare.


## Verifier fixes (2026-09-15)

- `to_number("-")` raised IndexError (`"" in "+-"` is True), crashing describe_table/prompt_guide on any table with a lone "-" cell.
- Frame construction above 200k rows held the GIL in single numpy/pandas calls: 3.8 s event-loop stalls at 2.1M rows. Arrays are now built in 16k chunks (worst gap ~95 ms at 2.1M, 3x faster).
- dd-mm/mm-dd detection for DESCRIBING a table (prompt_guide, guided_schema, repair_binding, inline) reads an even 20k-row sample; compute still reads every row. The described date span says "about" when sampled.
- Total rows ("Total", "**Total**", "Grand total", कुल, કુલ) are left out of aggregated charts with a note, unless a filter on x asks for them.
- Common missing tokens (N/A, #N/A, -, null, …) are blanks.
- Year-like numeric categories are labelled 2024, not 2,024.
- An average/min/max/median over no rows in a grouped chart is still drawn as 0 (Series values are floats) but a note says it is not a measured value. Residual: gaps instead of zeros need Optional values end to end.
- Colour keys are matched to computed category/series names case- and space-insensitively at resolve time; unknown keys get a note.
- Pie/donut/funnel over negative values is refused with a reason (it drew vanished wedges and wrong shares).
- Axis tick labels are clipped at 28 characters.
- SVG validation refuses backslashes (CSS escapes spelling `url(`) in style text and attributes.
- parse_prompt_data no longer reads "I have 3 kids and 2 dogs", "meeting at 10 and lunch at 1" or "iPhone 15 vs iPhone 16" as tables.
