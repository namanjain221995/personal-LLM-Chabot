# AS3 · intent-capability

The chat must hand over files when people ask for them, in the words they
actually type, and must never answer a request for a file with "as an AI I
cannot create a .docx". This track owns the intent gate, the capability line
in the answering prompts, the denial backstop, the markdown/Word importer and
the material a file turn is made from.

## The incident (2026-09-15, paraphrased)

After the assistant wrote a long audit report, the person asked, with typos,
for it "in docs … in a standard and classy format … a dox file". The rules
knew neither `docs`/`dox`, nor a hand-over verb with a bare `it`; the Fast
lane admitted the turn; the chat model, whose prompt never said the platform
makes files, denied it and pasted python-docx code.

## What changed

| Piece | File | What it does |
|---|---|---|
| Lexicon | `orchestrator/app/artifacts/lexicon.py` | `normalize()` folds case, strips zero-width characters, maps typos (`dox`, `exel`, `presentaion`, `chnage`) and Hindi/Gujarati/Hinglish/Gujlish words to English tokens and four SOV tokens (`_give_`, `_convert_`, `_in_`, `_this_`). `negative_shape()` classifies clauses as `read_source`, `how_to`, `trivia`, `feedback` or `code_request`. Also `file_signal`, `chart_signal`, `style_phrases`, `strip_style_clauses`, `undo_signal`, `language_of`. Pure, compiled at import. |
| Rules | `orchestrator/app/artifacts/intent.py` | `decide()` stays rules-only and synchronous (the Fast lane calls it). Order: UI `artifact_id` → hard negatives → artifact follow-ups (undo, answer export, convert after a file card, convert, positional create, pronoun export, edit, style/element edit) → export of the previous answer → pronoun/postposition export → create (incl. SOV, noun-phrase, chart). New fields on `ArtifactIntent`: `target`, `style_request`, `chart_request`, `language`, `upload_refs`, `llm_used`, `artifact_id_hint`. Helpers `substantial_answer_index`, `last_turn_is_artifact`, `is_artifact_turn`, `verdict_to_intent`. |
| Classifier | `orchestrator/app/artifacts/intent_llm.py` | One strict-JSON call with context (≤ 500 chars of the substantial answer, file-card flag, file titles, attachment names), thinking off, 220 tokens, `asyncio.timeout` 2.5 s at Fast / 5 s otherwise, six authored negatives in the prompt, accepted at confidence ≥ 0.75 and never for a create/export on a negative-shaped message. Skipped when disabled or when ≥ 6 live chat generations are running. Metric `artifact_intent_llm_total{result}`. |
| Importer | `orchestrator/app/artifacts/md_import.py` | `markdown_to_document()` and `docx_to_document()`: deterministic, faithful, 0 model calls. Only http/https/mailto links keep their address; images become alt text; mermaid is omitted with a note; Word uploads are read as text and tables only, after the zip caps. |
| Material | `orchestrator/app/artifacts/material_in.py` | `gather()` → `GatheredInput`: the most recent substantial answer with its markdown (≤ 120,000 chars), same-turn uploads (DOCX/MD/TXT as specs, PDF text plus approximate tables, XLSX every sheet as cached values, CSV) persisted with `db.save_document`, earlier-turn documents when the target is the upload, answer tables (`answer<N>`, provenance `assistant_answer`), prompt tables through the charts track's parser when merged. Every reader runs in `asyncio.to_thread` under a 15 s deadline. |
| Capability | `orchestrator/app/engines/capability.py` | `CAPABILITY_LINE` (design text), `denial_in()` (denials in EN/HI/GU/Hinglish, and library-code or copy-paste substitutes), `offer_line(language)`. |
| Prompts | `engines/chat.py` (one delimited concatenation in `_messages`, not the lane prompt), `engines/agent.py` `_SYNTH_SYSTEM`, `engines/document.py`, `engines/vision.py`, `engines/dataset.py` | The capability line appended. Not in `_STEP_LLM_SYSTEM`. Not in `dataset_report._NARRATIVE_SYSTEM` (a report paragraph told "do not mention PDFs, files"; the line would contradict it). |
| Formats | `orchestrator/app/artifacts/formats.py` | `explicit_formats()` reads normalised text, so the engine's own call on the raw instruction agrees with the gate. |
| Chat wiring | `orchestrator/app/main.py` (8 blocks marked `AS3 intent-capability`) | `ChatRequest.artifact_id`; the gate with context and the hook at every effort; Salesforce planner refusals of a file request held and sent to the artifact branch; `gather()` for every artifact turn (passed as `gathered=` when the engine accepts it; until then the engine gets the history ending at the substantial answer and the attachments' text); the post-answer denial backstop. |
| Flags | `orchestrator/app/config.py` | `ARTIFACT_INTENT_LLM` (true), `ARTIFACT_INTENT_LLM_TIMEOUT_FAST_S` (2.5), `ARTIFACT_INTENT_LLM_TIMEOUT_S` (5.0), `ARTIFACT_DENIAL_BACKSTOP` (true). Real `Settings` fields. |

### Salesforce mode

`sf_intel.run` keeps its place. When the message has a file signal, its
tokens and meta are held (status and steps stream). If the planner handled the
turn with route `chat` or `clarify` and no data (DENY, UNSUPPORTED,
ASK_CLARIFICATION) and the gate then wants a file, the held answer is dropped
(a pending clarification is cancelled) and the artifact branch runs; counted
in `artifact_sf_fallthrough_total{route}`. Anything else streams exactly what
the planner said. SQL answers (`route: sql`) are never held back.

### The denial backstop

After the chat / agent / dataset / vision engine returns its final text
(after `core/answer_guard`), if `denial_in(answer)` and the person's message
has a file signal, the classifier is asked once. Only a create/export/convert
verdict with confidence ≥ 0.8 starts the job; the card and "Here is the file.
Created **…**" are appended and a merged meta (`artifact_backstop: true`) is
published. Otherwise `artifact_denial_seen_total{engine}` is counted. The
streamed text is not rewritten.

## Measurements (2026-09-15)

All sets are authored; no user content. "Rules" = `decide()` plus the Fast
lane; "+ classifier" replays live verdicts recorded from the engine.

| Set | Before AS3 | After |
|---|---|---|
| 205 set, binary file recall | 0.455 | 1.000 (rules alone) |
| 205 set per language (hinglish / hi / gu / gujlish) | 0.08 / 0 / 0 / 0 | 1.00 each |
| 205 set edit+style → edit; answer conversions → export; typos | 18/52; 4/38; 2/13 | 52/52; 38/38; 13/13 |
| 150 hard negatives, false files | 8/150 (5.3 %) | 0/150 (all languages 0) |

The 205 set and the negatives were used while writing the rules, so their
numbers are development numbers. The held-out rounds were written by the
local model (a different author) after successive freezes and reviewed by
hand:

| Held-out round | Rules at the time it was written | + classifier | Notes |
|---|---|---|---|
| r1 (109 items, 68 positive) | recall 0.824, false files 5/41 (12.2 %) | not measured | rules then changed |
| r2 (101 items, 60 positive) | recall 0.883, false files 4/41 (9.8 %), hinglish 0.78, gujlish 0.67 | not measured | rules then changed |
| r3 (121 items, 80 positive) — final | recall 0.762, false files 1/41 | recall 0.988, false files 3/41 | see below |

After r3 was read, two changes were made and are disclosed because they
contaminate r3: (1) a tokenizer bug — the Devanagari danda `।` counted as a
letter, so no Hindi sentence ending in `।` matched a verb — rules recall on r3
0.762 → 0.850; (2) a classifier verdict of *edit* when no file exists is
refused instead of mapped to *create* — false files 3/41 → 2/41. Final r3 with
recorded verdicts: **recall 0.988, false files 2/41 (4.9 %), lowest language
gujlish 11/12**. The design target of ≤ 3 % false files on held-out is **not
met**; the two remaining false files are "I want to edit the attached report
before submitting." and "मुझे इस चार्ट का डेटा स्रोत चाहिए।".

Classifier cost: 48 live calls, p50 0.64 s, p95 0.76 s, max 0.77 s (thinking
off, guided JSON) — none would have hit the 2.5 s Fast timeout. It is called
0 times for messages caught by the negative pass or without a file word
(asserted), and at most once per turn.

Faithful export: a synthetic 42,062-character audit answer → 199 blocks →
DOCX/PDF: 79/79 heading lines and 828/828 table cells present in the produced
DOCX, no external relationships, no `javascript:`/`file:`/`data:` URL, no
remote image; import 8 ms, render 1.4 s, 0 model calls.

Uploads: a 30,544-character DOCX with "is file ko professional docx me bana
do" → create / target upload / docx; 43/43 headings and 516/516 cells in the
produced DOCX; the document row saved. A 60-row XLSX → `upload1` with 60 rows
and Status counts 24/14/11/8/3; a formula cell read as its cached value (42).

The capability line, measured on the three repro prompts with the chat prompt
(the path a turn takes only if the gate misses): **without** it 3/3 answers
denied or substituted python-docx; **with** the design line still 3/3 (two
denials, one python-docx substitute). Two stronger wordings tried as an
experiment gave 2/3 and 1/3 clean answers. The gate (which decides all three
as export → docx) is the fix; the backstop's detector flags all six recorded
answers.

Live end to end (`tests/test_artifact_intent_live_e2e.py`, opt-in): the three
paraphrased production prompts after a long audit answer, through the real
`/chat` route, composer and renderer at Fast — 3/3 ended in a completed Word
file card ("Created **Sales Onboarding Audit Report** as Word."), 3 engine
calls in total, backstop not involved.

Engine budget used by this track: 69 requests (48 classifier verdicts, 7
held-out generation, 11 capability-line experiments, 3 end-to-end compose).

Test-suite note: `tests/fixtures/context_assembly_golden/*.messages.json`
pin the chat prompt byte for byte; the capability suffix was inserted into
the 12 goldens at the exact place `_messages` puts it (after the identity
line, before grounding), and `test_fast_lane_classifier.py`'s non-lane prompt
assertion gained the suffix in a delimited block.

## Known limits

- False files on held-out r3 are 4.9 %, above the 3 % target (n = 41).
- The held-out author is the same local model family the classifier uses;
  its Gujlish is poor (one generated batch was discarded as unrealistic), so
  Gujlish held-out coverage is thin (13 positives, 5 negatives in r3).
- The capability line does not stop the model from denying on its own.
- md_import paragraphs keep markdown hard breaks as `\n`, which the current
  document renderer shows as a space.
- The engine (`engines/artifact.py`, prompt-edits track) still composes an
  export with the model; `gathered=` is passed as soon as the engine accepts
  it, and md_import makes that export faithful.
- `.env.example` is not in this track's files; the four variables are
  documented here.

## Adversarial verification (2026-09-15)

40 cases I wrote myself (over-trigger traps, typo and multilingual creates,
edits, style, undo, uploads, charts), plus data checks against pandas,
pathological inputs and renders. Fixed in this patch:

- **Style edits turned into conversions.** Right after a file card, "bold the
  first row and make font size 14 in the pdf" re-rendered the same PDF, and
  "make the Status column red where Open, in the sheet" became a new
  workbook. A style clause that names the file only as a place is now an
  edit. "make it a docx with dark blue headings", "turn this into an excel"
  and "convert to pdf" still convert. A remark such as "I opened the docx
  on my phone and …" is no longer a conversion either.
- **A new topic said with a postposition exported the last answer.** "sales
  ki report banao" exported the previous answer. An export-postposition with
  no reference must now be content-free. WH-questions about what goes in a
  file ("docs me kya likhna chahiye", "…so what should go in it?") are not
  files.
- **The classifier turned statements into files.** Live, the classifier said
  export at 0.9 for "my manager wants everything in excel, which is
  annoying" and "I prefer pdf over word for contracts generally". A
  create/export verdict now needs something in the message that asks: a
  question mark, a politeness word, an imperative or hand-over verb, "I/we
  want/need", "can you", a Hindi or Gujarati want-verb, or a noun-phrase ask.
  The recorded verdicts still pass every labelled threshold.
- **Denial backstop.** If the artifact job raised, the already-streamed answer
  became a failed turn. That error is now caught and the turn completes. The
  engine's sentence is held back, so "Here is the file." appears only when a
  file card exists.
- **Capability line.** It is omitted when `ARTIFACTS_ENABLED=false`.
- **md_import.** The emphasis regexes were quadratic: 60k chars of unmatched
  `*`/`_`/`**`/`~~` took 5–11 s, and a 400k TXT upload would have held the
  GIL for minutes. They are linear now. Script and style content is no
  longer copied as text. A DOCX paragraph starting "1." no longer leaks as
  "\1.". Cells after a horizontally merged cell stay under their own
  headings.
- **material_in.** `reset_dimensions()` stops a forged `<dimension>` from
  padding every row to 16,384 cells (20k rows: 4.8 s down to 0.3 s). A banner
  or title row above a table is no longer read as a one-column header, and a
  one-column sheet keeps its header. Rows wider than the header keep their
  cells.
- **Lexicon.** "isko exel sheet me daal do" is now a hand-over. "पिछला बदलाव हटा
  दो" and "pichla change hata do" now undo.

Still open, not fixed here:

- Held-out r3 false files remain 2/41.
- A workbook whose formulas have no cached values (for example, one saved by
  openpyxl or pandas) reads those cells as blank, with no note.
- Gujarati text renders poorly in the PDF renderer (styling track).
- The UI `artifact_id` is honoured only once the prompt-edits engine
  (`pick_artifact`) is merged.
