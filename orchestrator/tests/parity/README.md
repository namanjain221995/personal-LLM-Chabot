# The parity gate

One machine-checkable definition of "understood the prompt".

`prompt.txt` is a real request, 914 characters, asking for a 15-section
technical report in professional Markdown with headings, subheadings, tables,
bullet points, numbered steps, bold text, code blocks, warnings, notes and
recommendations. This package turns that request into **17 machine-checkable
prompt requirements**, each traced to the clause it comes from, plus **2
extra checks about diagrams** that the prompt does *not* ask for and which are
reported separately so nobody can claim it did.

Without this, every track on the prompt-comprehension programme grades its own
homework.

## Run it

```bash
cd orchestrator
python -m pytest -q tests/parity
```

Score one recording by hand:

```bash
cd orchestrator
PYTHONPATH=$PWD python -m tests.parity.score tests/parity/runs/prod_spec.json
```

## The two test files, and why there are two

| file | what it asserts | colour in CI |
| --- | --- | --- |
| `test_parity.py` | the scorer is still the scorer that was calibrated, and each frozen baseline still scores **exactly** what it scored | green, forever |
| `test_parity_gate.py` | the candidate recordings the tracks produce clear `PARITY_MIN` | green and vacuous until a track lands one |

`.github/workflows/scripts/shard_tests.py` discovers `test_*.py` at **any**
depth under `orchestrator/tests`, so both of these run in a CI shard on every
push. A permanently red test file is a pipeline that the first person under
deadline pressure repairs by lowering the bar — which is the one thing this
package exists to prevent. Hence the split.

The live producers (`run_live.py`, `run_live_chat.py`,
`runs/diagram_probe.py`) match neither `test_*.py` nor `*_test.py`. That is the
mechanism that keeps them out of the default collection: they need a GPU and a
live engine. **Do not rename them.**

## The calibration guards — read these before moving a floor

1. **`test_every_floor_is_below_the_reference`** — every floor is at or below
   what the calibration reference actually achieved, so the bar can never
   become "match the other assistant". Raise a floor past the reference and
   this fails, naming the floor and both numbers. It runs off
   `calibration/reference_counts.json` and needs no reference file present.
2. **`test_every_floor_in_the_checklist_is_calibrated`** — a floor added to
   `checklist.py` without a calibration entry fails, because a floor nobody
   justified is a floor nobody can argue with.
3. **`test_reference_passes_when_present`** — with the reference in reach, it
   is scored for real: 17/17 on the prompt checks, and the committed fixture
   must still be a true reading of it. An *extra* assertion on top of 1 and 2,
   never the only one and never a skip.
4. **`test_normaliser_reads_every_block`** — the document schema's block union
   is read from `app.artifacts.spec` at runtime and compared to what
   `normalise.py` can render. A block type added to the vocabulary and not to
   the normaliser fails here instead of scoring as nothing. **There is no skip
   in this test**: this file lives inside the orchestrator package, so a failed
   import is the correct outcome.
5. **`test_empty_table_is_not_counted_as_a_table`** — the regression guard for
   the real bug this eval hit on its first pass: a `TableBlock` nests its
   payload under `"table"` while every other block keeps its fields at the top
   level, so reading `block["columns"]` scored six real tables as zero. The
   `diagram` block nests the same way and is pinned beside it.
6. **`test_no_identifier_shaped_literals_in_runs`** — no 16-, 32- or 64-character
   lowercase-hex run and no dashed UUID anywhere under `runs/`. Those are the
   shapes this system's live handles take — `file_id`, `artifact_id`, `sha256`,
   a conversation id — and this repository is public. The scorer reads blocks
   and warnings; it never reads a handle. Check it by hand as well: this
   project's history records that gitleaks run from a worktree scans nothing.

## The calibration reference is NOT in this repository

The floors were placed below what one other assistant achieved on the same
prompt. That answer is a third-party-generated business document, this
repository is public, and `.gitignore` excludes the directory it sits in. What
is committed instead is `calibration/reference_counts.json`: its **observed
counts only** — integers and one ratio, no prose. `_observe()` in
`test_parity.py` is both the generator and the checker, so the fixture cannot
drift from the file it was derived from.

Point `PARITY_REFERENCE` at the file to run the stronger assertion; the
default is `<repo>/backups/chatgptoutput.txt`, derived from this file's own
location.

## The frozen baselines are pinned by EXACT score, not by a floor

`runs/BASELINE_SCORES.json` records, for each recording, the exact `passed`
count **and** the exact per-check pass/fail vector.

| recording | route | effort | prompt checks | words | sections | sub-heads | tables | code | diagrams | bold | wall | model calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| calibration reference | — | — | 17/17 | 2,475 | 15 | 13/15 | 6 | 3 | 5 | 58 | — | — |
| `live_chat_think.md` | chat | think | **14/17** | 1,386 | 15 | 0/15 | 4 | 3 | 0 | 81 | 54 s | 1 |
| `live_max.json` | file | max | 12/17 | 2,007 | 16 | 0/16 | 8 | 0 | 0 | 0 | 300 s | 4 |
| `live_think.json` | file | think | 11/17 | 1,724 | 15 | 0/15 | 6 | 0 | 0 | 0 | 311 s | 4 |
| `live_fast.json` | file | fast | 10/17 | 886 | 15 | 0/15 | 2 | 0 | 0 | 0 | 26 s | 1 |
| `prod_spec.json` | file | fast | 10/17 | 1,081 | 15 | 0/15 | 1 | 0 | 0 | 0 | — | 1 |

`words` is a prose count: headings, code fences and diagram fences are
excluded. By `wc -w` the reference is 3,419.

These five numbers are pinned EXACTLY, which is what catches the scorer's
logic drifting while every floor stands still. It is **not** what catches a
floor being softened: a recording's vector only moves when a floor crosses
that recording's own count, so each floor has a gap between two recordings it
can be moved inside with this whole directory green — measured at 26 values
wide for `SECTION_WORD_FLOOR`, 282 for `TOTAL_WORDS_MIN`, and unbounded below
for `HEADINGS_MIN`. Floor **values** are therefore pinned by name and value in
`test_parity.py` (`FLOOR_VALUES`), and moving any one of them by a single step
fails that pin and names the floor. Those five numbers — 10, 10, 11, 12, 14 —
are the proof this harness entered the repository without the scorer being
softened.

## What the baseline says

Three checks — `bold`, `code_blocks`, and `diagrams` among the extras —
**cannot pass on the file route at any effort**, because `DocumentSpec` has
nine block types (`heading`, `paragraph`, `bullets`, `numbered`, `table`,
`chart`, `callout`, `kpis`, `page_break`), none of them a code block or a
figure, and `Paragraph.text` is plain text that no renderer reads inline
markup out of. Raising the effort cannot fix a schema.

Two more — `total_words` and `sections_substantive` — fail on **both** routes.
That is a size problem, not a route problem.

## Run-to-run spread — measured, and it matters to the bar

The recordings above are **one sample each**. On the day this package landed,
the chat route at Think was run twice more against the same prompt, GPU idle,
nothing changed:

| sample | prompt checks | tables | recommend\* mentions | diagrams | words |
| --- | --- | --- | --- | --- | --- |
| `live_chat_think.md` (the recorded baseline) | 14/17 | 4 | 3 | 0 | 1,386 |
| second sample | 12/17 | 2 | 1 | 1 | 1,348 |
| third sample | 14/17 | 7 | 4 | 1 | 1,312 |

Fourteen of the seventeen checks returned the same verdict in all three. Two
moved, and they moved together in the same sample: `tables` and
`recommendations`. Both are counts sitting close to their floors on a route
whose output is sampled at temperature 0.3.

The file route at Fast was rerun once over the same prompt and reproduced
10/17 exactly, check for check.

**What this means for the bar.** 14 is the MODE of that distribution, not its
floor: an unchanged system can produce a 12 and fail a gate set at 14. A
candidate recorded from a single run is therefore evidence about one sample,
not about the build. Whoever declares a candidate should say how many runs it
came from, and the integrator raising `PARITY_MIN` should know that a
two-check swing here is the engine, not a regression. This is a measurement,
not a proposal to lower the bar — `PARITY_MIN` is deliberately not lowered
here.

## `PARITY_MIN`

```python
PARITY_MIN = int(os.environ.get("PARITY_MIN", "14"))
```

14 is the best the system reaches today, on the chat route at Think. It is the
integrator's dial and nobody else's: raised as tracks land, **never lowered to
make a build green**. If a candidate cannot clear it, that is the measurement,
and the measurement is the deliverable.

## The two block shapes the tracks build to

Neither exists in `DocumentSpec` yet. `normalise.py` renders both already, so
the track that adds them builds to a reader that is already in the repository
and the route track can report `code_blocks` as *unsupported* rather than as a
permanent fail while it waits.

```json
{"type": "code", "language": "bash", "text": "echo hello", "caption": ""}
```

```json
{"type": "diagram", "diagram": {
  "title": "Architecture", "direction": "TD",
  "nodes": [{"id": "A", "label": "Frontend", "kind": "service"}],
  "edges": [{"source": "A", "target": "B", "label": "reads", "style": "solid"}],
  "caption": ""}}
```

The diagram block **nests under `"diagram"`**, exactly as `TableBlock` nests
under `"table"`. That is the shape the guard in item 5 above pins.

### The role vocabulary

`normalise.DIAGRAM_ROLES = ("service", "store", "model", "external")` is the
single source. `app/artifacts/spec.py`'s enum, `DIAGRAM_INSTRUCTION`,
`app/artifacts/render/diagrams.py` and `frontend/lib/mermaidTheme.ts` each copy
it verbatim — copies, because a Python tuple cannot be imported by TypeScript
and a prompt is a string. A disagreement is a bug in whichever file drifted.

**Four is the ceiling**, and it is a palette fact: the theme must separate
these fills from each other and from the page, in both light and dark mode, at
the contrast the renderer guarantees. A fifth role would be a colour a reader
cannot tell from another colour — which is the defect the owner reported,
arrived at from the other direction.

### Why `diagram_colour` became `diagram_roles`

The old check matched `style|classDef|linkStyle|fill:|stroke:|%%{init` — the
directives that colour a diagram by hand. After this release the prompt forbids
the model to write any of them and the sanitiser strips them if it does, so the
check is unpassable by construction. A check no correct answer can pass is not a
bar; it is a permanent red mark that teaches people to ignore the scorer.

What replaces it is what a Markdown scorer can actually see: did the answer tag
its nodes with a role the renderer has something to colour *from*. **The colour
itself is asserted where colour exists** — the renderer's tests and the
frontend's token tests — and deliberately not here.

## Producing a new candidate

Each producer waits for the shared engine to be idle
(`vllm:num_requests_running` = 0) and refuses to start if it cannot read the
gauge. None writes to the production database or creates an artifact in
anyone's conversation.

```bash
cd orchestrator
python tests/parity/run_live.py      fast|think|max   # the FILE route, real composer, in-process
python tests/parity/run_live_chat.py fast|think|max   # the CHAT route, real chat system prompt
python tests/parity/runs/diagram_probe.py             # does the chat prompt draw when asked?
```

Then put the recording in `runs/candidates/` and declare it in
`runs/candidates/INDEX.json` with the track that owes it, **in the same
commit**. A declared candidate whose file is absent fails and names the track;
a recording nobody declared fails too.

## Files

| file | what it is |
| --- | --- |
| `checklist.py` | the 17 prompt checks + 2 extras, each with its clause, and every floor with the measurement that justifies it |
| `normalise.py` | DocumentSpec → the Markdown it is equivalent to, plus the two block shapes and the role vocabulary |
| `score.py` | the Markdown parser and the checks; reports what it OBSERVED, not just pass/fail |
| `test_parity.py` | the floor-VALUE pin (`FLOOR_VALUES`, with its changelog), the calibration guards, and the exact-score pins on the recordings |
| `test_parity_gate.py` | `PARITY_MIN` applied to the candidates |
| `calibration/reference_counts.json` | the reference's observed counts, derived; the reference itself is not here |
| `runs/` | the frozen baselines, each pinned by name |
| `runs/candidates/` | what the tracks produce, declared in `INDEX.json` |
| `runs/probe/` | diagram-probe output; deliberately not beside the frozen baselines |
