# AS3 — Styling engine

Status: implemented in the `as3-styling-engine` patch (not merged, not deployed). Owner of the
track: the styling engine. Consumers: intent-capability, prompt-edits, charts, agentic-selfcheck.

## What it does

A person can ask for any font family, size, bold/italic/underline, text or background colour and
alignment for any element — title, subtitle, a heading (all, a level, one by its text), paragraphs,
bullets, table header/body/totals, a column, a row, a spreadsheet cell range, KPI values/labels,
callouts, captions, slide titles/body, the header/footer, chart title/axes/legend/labels — plus
page size, orientation, margins, page numbers, header/footer text and a slide background, in
English, Hinglish, Hindi and Gujarati, with typos. Every file looks professional without being
asked: **TechSara Classic**.

```
request text ──► style.parse_style_request (deterministic)  ──► StylePatch ─┐
                   └─ unparsed style phrases ─► style.extract_patch_llm ────┤ (ONE call, 300 tokens, 5 s)
                                                                             ▼
                          spec.style = style.merge(spec.style, patch)   (code-written, never the model)
                                                                             ▼
render_version ─► style.normalize_spec_style (drop rules whose targets do not exist, with notes)
               ─► style.resolve(spec) ─► ResolvedStyle ─► docx.py · html.py→pdf · pptx.py · xlsx.py
               ─► style.warnings_for(spec, formats)   one sentence per unsupported group
               ─► font substitution + contrast sentences
```

`style.apply_request(spec, text, formats=...)` does the parse, merge, normalise and the CSV rule in
one call for the engine (prompt-edits / intent-capability wire it in; this track does not touch
`engines/artifact.py`).

## Decisions (and the critic corrections they implement)

1. **Style is not in the guided schema.** `DocumentSpec/PresentationSpec/WorkbookSpec.style` and
   `Column.format/align` are `SkipJsonSchema` fields: persisted in spec.json, validated, invisible to
   `schema_for(kind)`. The schema JSON lengths are asserted equal to the base tree
   (document 8286, presentation 6186, workbook 14670 characters). `spec.parse_body` drops them from
   model output; `spec.load` keeps them.
2. **Banding never hides a requested fill.** Excel paints conditional-format fills above cell
   fills, so user fills are CF rules too. `xlsx.sheet_cf_plan` adds rules highest priority first:
   user fills on named cells (legacy highlighted column, row, cell range) → user conditions and
   colour scales → user fills on a whole column or the table body → automatic status/score/due-date
   colours → banding `=MOD(ROW(),2)=0`. Only a rule that paints a fill carries `stopIfTrue`; a
   font-only rule ("Owner column bold") merges with the lower rules as Excel merges non-conflicting
   formats, so the column keeps its banding and status colours, and "Status red for Blocked" stays
   visible on a column the person also filled grey (verifier fix). Asserted from the rule
   priorities in the file and from the styled grid.
3. **Injection surfaces.** CF formulas: column letters from code, user values only through
   `style.xlsx_formula_literal` (quotes doubled, control characters removed, 100 characters) or
   `xlsx_search_literal` (SEARCH wildcards escaped). Excel header/footer text: `&` doubled
   (`&F secret` → `&&F secret`). CSS: only validated `#RRGGBB`, allowlisted font stacks (constant
   strings) and clamped numbers are interpolated; rules for one element are classes/data attributes
   built from integers (`ts-e3`, `table.ts-t2 td[data-col="4"]`), never text in a selector. DOCX
   header/footer text is a plain run; the only fields are the constant `PAGE`/`NUMPAGES`/`TOC`.
   Fonts outside `FONT_ALLOWLIST` are refused by validation (`Georgia; } @import url(x)`).
4. **Denial of service.** A cell range is clipped to the used range before any cell is touched
   (`style.clip_a1`); a range over 10,000 cells is carried by its single CF rule, not per-cell
   styles. `A1:XFD1048576` on a 50×5 sheet renders in 0.165 s.
5. **Contrast.** Computed with the WCAG formula in code (`style.contrast_ratio`) and tested. A
   colour the SYSTEM picks (text on a user fill, totals text, labels) flips between white and ink
   `#1F2937`; an explicit user pair is honoured and a pair under 3:1 earns one warning
   ("Yellow text on white is hard to read (contrast 1.4:1); it was kept as asked."). A NAMED
   orange/amber/yellow/teal/green/pink used as text on white becomes the same hue's text-safe
   variant, with a note; a hex code is used as given.
6. **Chart palette for colour-vision deficiency.** `style.CHART_PALETTE` =
   blue #2F6FB2, orange #E07B00, teal #0E9D9A, rose #C0566B, purple #6D5AE6, brown #8A5A44,
   green #3F8F4F, grey #5F6B7A; the first five are ≥ 25 CIELAB apart under simulated deuteranopia
   (tested). `theme.PALETTE` is unchanged because `core/charts_png.py` must match it; the charts
   track moves the painters to `ResolvedStyle.chart_defaults`.
7. **Parity is checked on produced files.** `tests/artifact_file_readers.py` reads DOCX XML (style
   inheritance resolved), PPTX runs, XLSX cells + evaluated CF, and PDF page objects through PDFium
   (text fill colour, font name, size with form-XObject matrices applied, background rectangles,
   underline bars, alignment in the container). It never reads a ResolvedStyle.
8. **The support matrix is declared, per (format, kind).** `style.support_matrix(fmt, kind)`; every
   declared "supported" cell is exercised on a produced file (687 cells across docx/pdf document,
   docx/pdf workbook companion, pptx, pdf deck preview, xlsx); every unsupported group a spec uses
   yields exactly one sentence. Charts drawn as images (DOCX/PDF/PNG/SVG) are declared unsupported
   here until render/charts.py reads `chart_defaults`; native PPTX chart title/axis/legend/labels are
   supported.

## The style model (`orchestrator/app/artifacts/style.py`)

- `TextStyle{font_family, size_pt 6–72, bold, italic, underline, color, background, align}` —
  every field optional; colours validated to `#RRGGBB` (names resolve by code); fonts must be on
  `FONT_ALLOWLIST` (40 families with CSS stacks, Office names and metric substitutes).
- `StyleTarget{kind, level?, text?, index?, section?, first?, sheet?, name?, a1?}` — locators are
  validated per kind. A paragraph target is "the first paragraph" or "paragraphs in section X";
  there is no free paragraph index. For a sheet `row.index` is the spreadsheet row (header = 1).
- `StyleRule`, `CondRule{column, op eq|ne|in|contains|gt|gte|lt|lte|between|blank|date_past,
  value(s) ≤ 100 chars / finite numbers, style, whole_row}`, `ColorScale`.
- `StyleSpec{preset classic|modern|minimal|boardroom|teal, page, fonts, base_size_pt, colors,
  header_footer{page_numbers, header_text ≤120, footer_text ≤120}, rules ≤50, conditional ≤30,
  scales ≤10, banded, freeze_header, auto_status_colors, auto_score_scale}`; `StylePatch` is the
  same shape, all optional, plus `clear: [ClearRule{target, props}]` and `reset`. `merge(base,
  patch)`: `reset` starts from the house style; each `ClearRule` takes `props` off every rule on its
  target (every rule of that kind when the target has no locator; a rule left empty is dropped; a
  column colour clear also drops that column's conditional colours); then scalars override; a rule
  on the same target merges property by property; conditions/scales with the same key replace.
  The parser emits `clear` for removals ("remove the yellow from row 5", "row 5 no fill",
  "हेडिंग से लाल रंग हटाओ"), `reset` for "remove all formatting", and nothing for negations
  ("don't make the headings blue") — none of them can add the colour they name.
- `resolve(spec) → ResolvedStyle{tokens, body_face, heading_face, sizes, page, rules, conditional,
  scales, element(kind, **locator), chart_defaults{palette, font_family, font_stack, title_size_pt,
  axis_size_pt, grid_color, axis_text_color, title_color, label_color_for(fill)}, warnings}`.
- `normalize_spec_style(spec)`, `support_matrix`, `warnings_for`, `requested_pairs`,
  `xlsx_formula_literal`, `xlsx_search_literal`, `xlsx_header_footer_text`, `clip_a1`,
  `formats_with_style`, `apply_request`, `patch_fields`, `strip_style_clauses` (a local fallback for
  the lexicon).

## Renderers

- **XLSX** — header #1F3864 white bold 11 pt, height 24, medium rule; thin #E5E9F0 grid (never
  black); banding; freeze A2 (B2 for an id column past 8 columns); widths from content (8–50, dates
  12, currency ≥ 14, text > 50 characters wraps); number formats per `Column.format` (INR in
  lakh/crore grouping); per-VALUE status colours in status-like columns (Pass is green, never red);
  3-colour scale on score/percent columns; past due dates in red text unless the status is done;
  totals `=SUBTOTAL(109/101/103/104/105, …)` styled #DCE6F2 / bold #1F3864; landscape past six
  columns, fit to one page wide, header row repeated, 0.5 in margins, "Page &P of &N"; tab colours;
  sheet notes on the Notes sheet. The legacy `SheetStyle` (header fill, borders, wrap, highlighted
  column pairs) renders as before.
- **DOCX** — named styles from the resolver; Title without the template's border and character
  spacing; 2 pt rule under the title block; H1 hairline rule; keep-with-next on headings;
  docDefaults and theme1.xml major/minor fonts set (complex-script slot "Nirmala UI" for Indic text);
  `TS Table` style; header with a right tab stop (title | date or CONFIDENTIAL); footer with author or
  requested text and PAGE/NUMPAGES; KPI tiles with a 3 pt top bar; callouts with the status fill and
  a 3 pt left bar; page size/orientation/margins from the page style; per-element overrides as run
  and paragraph properties; every added XML child placed in schema order (tested).
- **HTML→PDF** — custom properties from the resolver, generic element CSS after print.css, element
  and table rules as class/data-attribute selectors, `@page` size/orientation/margins, optional page
  numbers, Indic font stacks. The WeasyPrint fetcher serves `.png` and `.svg` from the assets
  directory only.
- **PPTX** — band colour (template, or the chosen palette), title/subtitle, slide titles (per slide
  number), body and bullets, tables (header, body, column, row), KPI tiles, captions, footer, native
  chart title/axis/legend/label fonts, slide background.
- **Tabular Word/PDF companion** — the same header, column and row rules, cells formatted with
  `theme.format_cell`, a computed totals row.
- **Preview** — `grid_for`/`sheet_grid` add `header_styles`, `cell_styles` and `display` by
  evaluating the workbook's own CF rules in Python in priority order with stopIfTrue (a small
  formula evaluator: TRIM/OR/AND/NOT/ISNUMBER/SEARCH/LEN/MOD/ROW/TODAY, comparisons, literals, and
  SUBTOTAL/SUM/AVERAGE/COUNTA/MIN/MAX for totals). `SheetViewer.tsx` paints them with inline styles
  (only `#RRGGBB` accepted). `preview.rasterise_image` serves `preview_kind: "image"`.
- **CSV + styling** — CSV stays data only; a styled request whose formats are CSV-only also gets the
  XLSX, and the sentence "The CSV carries the data only; the formatting is in the Excel file."

## TechSara Classic — the style guide

(The binding design's style guide, as implemented. Every ratio is WCAG, recomputed in
`tests/test_artifact_style.py`.)

**0. Overrides.** A colour the SYSTEM picks reaches 4.5:1 by flipping white/ink. An explicit user
pair is honoured; under 3:1 one warning. A user colour is never silently changed (except a named
colour used as text, which takes its text-safe variant with a note). DOCX/PDF never get a
full-page background. Colour is never the only signal.

**1. Palette.** ink #1F2937 (14.7:1); muted #5F6B7A (5.4:1 white, 5.0:1 band); caption #6B7280 on
white only (4.46:1 on band); primary navy #1F3864 (white on it 11.6:1); accent #2E5597 (H2, rules,
KPI bar); hairline #D0D7E2; grid #E5E9F0; band #F3F6FA; totals #DCE6F2 with bold #1F3864; status
pairs success #E3F2E6/#1E6B34, warning #FFF1C7/#7A4F00, danger #FDE4E4/#9B1C1C, info
#E3EDF8/#1F4E79, neutral #EEF0F3/#374151; score scale #F8696B/#FFEB84/#63BE7B with ink; data bars
#5B9BD5. Marks-only colours (never text on white): teal #0E9D9A (3.3:1), orange #E07B00 (3.0:1),
amber #B7791F (3.6:1); text-safe variants teal #0B7A77, orange #B35F00, amber #8A5A00 (and yellow
#806600, green #2E7D32, pink #B02A6B). Presets: classic; modern (#0F4C81, rules #0E9D9A, text accent
#0B7A77, Segoe UI→Carlito); minimal (#111827, #4B5563, header #F3F4F6 with ink); boardroom (#0A1D37,
rules #B7791F, text accent #8A5A00, headings Cambria→Caladea); teal (#0B5563, #0E9D9A, #0B7A77).
Named colours: dark blue/navy #1F3864, blue #2F6FB2, light blue #DCE6F2, dark green #1E6B34, green
#3F8F4F, light green #E3F2E6, red #C62828, dark red #9B1C1C, light red #FDE4E4, orange #E07B00,
amber #B7791F, yellow #FFD54F, light yellow #FFF1C7, purple #6D5AE6, pink #D63384, grey #6B7280,
light grey #EEF0F3, black, white, brown #8A5A44, teal #0E9D9A, gold #B7791F, maroon #7B1E1E; the
Hinglish/Hindi/Gujarati names (neela/नीला/વાદળી, gehra neela/गहरा नीला/ઘેરો વાદળી, hara/हरा/લીલો,
lal/लाल/લાલ, peela/पीला/પીળો, narangi/नारंगी/નારંગી, kala/काला/કાળો, safed/सफ़ेद/સફેદ,
gulabi/गुलाबी/ગુલાબી, baingani/बैंगनी/જાંબલી, bhura/भूरा/ભૂરો, sleti/स्लेटी/રાખોડી) map to the same.

**2. Type.** Body Calibri 11 pt (Carlito in the container), 1.25 line height, 6 pt after; title
28 pt bold #1F3864, no letter spacing, 2 pt rule under the title block; subtitle 14 pt muted; H1
18 pt bold #1F3864 with a 0.75 pt hairline rule, H2 14 pt bold #2E5597, H3 12 pt bold ink; caption
9 pt italic. Fonts from the allowlist; an uninstalled font is replaced by its metric twin in the PDF
and the answer names it.

**3. DOCX/PDF.** A4 portrait by default (landscape when asked or a table has more than seven
columns); margins 22/20/20/20 mm (narrow 12.7, wide 25.4); header 9 pt with the title and the date
or CONFIDENTIAL; footer with the author and "Page X of Y"; tables with a #1F3864 header, white bold,
repeated, zebra #F3F6FA, hairline rows, numbers right-aligned, rows never split; KPI tiles on the
band with a 3 pt #2E5597 bar; callouts in status fills with a 3 pt left bar; cover band in the
primary with a 32 pt white title and a 4 pt accent bar.

**4. XLSX.** As in "Renderers" above; CF priority banding < automatic < user column/body fills <
user conditions < user row/range fills; security: formula-lead text cells written with quotePrefix, CF literals escaped, header/footer
`&` doubled, no external links.

**5. PPTX.** 16:9; band in the primary; slide titles bold; body ≥ 14 pt; table header #1F3864
white bold, zebra; slide backgrounds allowed.

**6. Charts.** Body font; title 12 pt bold ink; axis 10 pt muted; horizontal gridlines #E5E9F0;
CVD-ordered palette; labels outside bars in ink; every number computed by code (charts track).

**7. CSV.** Data only; styling requested → CSV and a styled XLSX, one sentence.

**8. Images.** PNG/SVG downloads; SVG served as an attachment with a sandbox CSP (prompt-edits).

**9. Sentence.** Name what was applied, what could not be and why; never claim a style the file
check did not confirm.

## Interfaces for other tracks

- intent-capability: `style.parse_style_request(text, kind)`, `style.strip_style_clauses(text)`
  (fallback), `style.COLOR_NAMES` keys.
- prompt-edits: `style.StylePatch`, `style.merge`, `style.apply_request`, `style.normalize_spec_style`,
  `style.formats_with_style`, `style.warnings_for`; restyle = merge a patch into the parent spec's
  style (code carries `spec.style` across revisions; `parse_body` drops model-written style).
- charts: `ResolvedStyle.chart_defaults` (palette, fonts, sizes, `label_color_for`), `style.FONT_ALLOWLIST`;
  `render/__init__` dispatches `png`/`svg` to `charts.render_standalone(spec, fmt, out_dir, resolved)`
  when types.FORMATS lists them (dead code until then); `pdf.py` serves `.svg` from assets.
- selfcheck: `style.resolve`, `style.parse_style_request`, `tests/artifact_file_readers.py` shows the
  file-grounded reading approach.

## Measurements (2026-09-15, aarch64 host, Python 3.11)

- Parser, 80 authored phrases (25 EN / 20 Hinglish / 15 hi / 15 gu / 5 typos), field-level
  accuracy: **0.925 on the first run**, 1.000 after fixing five general gaps the misses showed
  (quoted text, "with colors", subtitle in Devanagari, one-word column names, Hinglish
  "<value> <colour>"); residual model call needed on 1/80 (1.3%).
- Held-out 20 phrases written with the set and scored only after the freeze: **0.875** field-level
  accuracy, residual call on 10%. Misses: "titles" on a deck read as the deck title, "મધ્યમાં"
  (centre) not in the vocabulary, "body font Calibri 11" size without "pt".
- Live residual call (Qwen3.6-35B-A3B on the local engine, concurrency 1): 12 hard phrases, 9 calls,
  11/12 fully correct after merge with the deterministic patch, p50 1.03 s, max 2.30 s. The miss:
  "section titles" applied to H2/H3 instead of H1. Total live calls this track: 20.
- Declared support matrix: 687 supported cells, all exercised on produced files (42 matrix tests).
- Render cost vs base (best of runs): 10-section report DOCX+PDF 0.68 → 0.59 s; 10,000-row XLSX
  2.10 → 1.74 s (style objects shared); 3,000-row XLSX+PDF 8.32 → 8.94 s. Styled grid window of 500
  rows from a 10,000-row workbook: 0.60 s. `A1:XFD1048576` on 50×5: 0.165 s.
- Back-compat: every paragraph/table text of the sample document and deck and every cell of the
  sample workbook equal the base renderers' output; totals differ only as SUM→SUBTOTAL with equal
  evaluated values.
- Tests: 843 passed, 2 skipped (Carlito/Caladea not installed on the host) across all
  `tests/test_artifact_*.py` and `tests/test_imports.py` on Python 3.11 with `-p ci_statvfs84`
  against a private Postgres; frontend vitest for SheetViewer 13/13.

## Not done / open

- Fonts (fix round 2026-09-15): Dockerfile.cuda and Dockerfile.cpu install fonts-liberation +
  fonts-liberation2, fonts-crosextra-carlito/caladea, fonts-dejavu-core, fonts-noto-core and
  fonts-lohit-deva/gujr (names verified with `apt-get install --simulate` on both pinned bases:
  Ubuntu 24.04 and Debian 13). The images have not been rebuilt yet; see "Font mapping" below.

## Font mapping

Office files always name the family the person asked for. The PDF and chart images are drawn on
the server, with the first installed family of `FontFace.pdf_candidates`, chosen by
`render.theme.resolve_font`:

| Requested | Drawn with (in the images) | Kind |
|---|---|---|
| Arial, Helvetica | Liberation Sans | metric twin |
| Times New Roman | Liberation Serif | metric twin |
| Courier New | Liberation Mono | metric twin |
| Calibri, Segoe UI, Aptos | Carlito | metric twin (Calibri); substitute (others) |
| Cambria | Caladea | metric twin |
| Georgia | Caladea | documented fallback: Gelasio, Georgia's metric twin, is not packaged for Ubuntu noble or Debian trixie |
| Verdana, Tahoma, Trebuchet MS | DejaVu Sans | substitute |
| anything else on the allowlist | its substitute if installed, else Liberation/DejaVu of the same class | generic |
| Devanagari / Gujarati text | Noto Sans (or Serif) Devanagari/Gujarati, then Lohit | script font |

`theme.pdf_font_stack` puts the chosen family first in the PDF stylesheet. Without that, fontconfig's
own alias for the missing name wins: `fc-match Georgia` gives Noto Serif on Ubuntu noble with
fonts-noto-core, and DejaVu Serif on a host with DejaVu. The render warns once per substituted font
and names the family used. The self-check counts a family as met when it is the requested one, its
declared substitute, the family `resolve_font` picks on this server, or a script font for text the
Latin family cannot draw. The item's evidence and its claimable line name the font used.
- The engine does not yet call `apply_request` (prompt-edits/intent-capability own that wiring).
- Chart images (matplotlib) do not yet read `chart_defaults`; their matrix cells are unsupported.
- 4:3 slides are declared unsupported (the deck geometry is 16:9 throughout).
- `compose.py` still dumps a parent body with its style into revision prompts; prompt-edits should
  exclude `style` there (parse_body already drops it from the answer).

## Adversarial verification (2026-09-15)

Fixed after the verifier's cases: removal and negation requests added the colour they named
(`clear`/`reset` above); "heading 1 size 20" set a 6 pt size on every heading (a heading number is
now a level); colour words inside content asks ("red team", "white house", "black friday") went to
the model call; "Comic Sans MS" silently became Arial; "paragraphs in the Risks section" styled
every paragraph; "column B" was dropped as a missing column name; font-only column/row rules carried
`stopIfTrue` and hid banding and status colours; a broad column fill hid a requested condition; the
styled grid evaluated every row before the requested page (5,000-row last page 2.4 s → 0.5 s; CF
formula tokens cached; ranges over 100,000 cells refused by the evaluator); `extract_patch_llm`
uses `asyncio.timeout` instead of `wait_for` (Python 3.11 cancellation).
