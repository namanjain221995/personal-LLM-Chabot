# The Salesforce Brain

This folder is where Salesforce knowledge lives. Whatever the AI knows about
HOW this org works — beyond the raw schema and the synced records — comes
from here.

```
brain/
├── sources/     ← raw knowledge files from the Salesforce team (txt/md).
│                  Drop new files here. Never loaded directly by the AI.
├── packs/       ← compiled knowledge packs (YAML). THESE are what the AI
│                  reads, on every Salesforce-mode question.
└── README.md
```

## How a developer adds knowledge

1. **Drop the raw file** into `brain/sources/` (any name, `.txt` or `.md`).
   Write it like `invoice-payment-quickbooks.txt` — field meanings, business
   rules, formulas, statuses, gotchas. More detail is better.

2. **Compile it into a pack draft:**

   ```bash
   cd orchestrator
   .venv/bin/python scripts/compile_brain_source.py ../brain/sources/your-file.txt
   ```

   This produces `brain/packs/your-file.yaml` — a draft. With the local LLM
   running it also distills rules/glossary automatically; without it you get
   the chunked knowledge sections and fill in the rest by hand.

3. **Review the draft.** The one rule that matters: **never teach the model a
   field that is not in the production warehouse.** The compiler warns about
   field names it cannot find in the org dictionary — fix or caveat those
   before shipping. (Sandbox docs run ahead of production; a plausible but
   missing field name makes queries return nothing, silently.)

4. **Ship it.** Packs in `brain/packs/` are mounted read-only into the
   orchestrator at `/data/brain` and re-read automatically when a file
   changes — no rebuild, no restart.

## What a pack contains

| Key           | What it does at answer time                                        |
|---------------|--------------------------------------------------------------------|
| `triggers`    | Words/phrases that pull this pack into a question's prompt         |
| `tables`      | Warehouse tables pinned into the schema slice when triggered       |
| `rules`       | Query-trap rules injected into SQL **and** live-SOQL generation    |
| `metrics`     | Canonical measure definitions (same shape as `org_brief.METRICS`)  |
| `glossary`    | Term → meaning, injected when the term appears in the question     |
| `field_notes` | Per-field help text merged into the field dictionary (enrich-only) |
| `knowledge`   | Prose chunks retrieved for "how does X work" questions             |

See `packs/qb-invoicing.yaml` for a complete worked example — it was compiled
from `sources/invoice-payment-quickbooks.txt` and verified against the
production warehouse field by field.

## How the AI uses it (when the Salesforce toggle is ON)

Every Salesforce-mode question flows through `orchestrator/app/core/brain.py`:

- **SQL engine** (warehouse) and **live SOQL engine** get the matched pack's
  `rules`, `metrics`, `glossary` and `knowledge` inside their grounding
  (`org_brief.grounding_for`), plus pinned `tables` in the schema slice.
- **Salesforce Intelligence mode** (the planner) gets the same knowledge in
  its schema summary.
- **RAG answers** get `knowledge` chunks so process questions ("how does the
  EMI plan work?") are answered from documentation, not "no data found".
- **Field dictionary** hints show your `field_notes` next to the field, but
  only for fields the production org actually has.

There is also a learn-from-chat loop (`core/learned_examples.py`): every
answer a user rates 👍 whose SQL is stored becomes a few-shot example for
similar future questions; a 👎 on the same SQL anywhere disqualifies it.

## Settings

| Env var                    | Default      | Meaning                                |
|----------------------------|--------------|----------------------------------------|
| `BRAIN_ENABLED`            | `true`       | Master switch                           |
| `BRAIN_DIR`                | `/data/brain`| Where the orchestrator looks for packs  |
| `BRAIN_MAX_CHARS`          | `4000`       | Cap on retrieved knowledge per prompt   |
| `LEARNED_EXAMPLES_ENABLED` | `true`       | Learn-from-chat few-shots               |
| `LEARNED_EXAMPLES_K`       | `2`          | Max examples per prompt                 |

## Before you ship a pack: run the gate

```sh
python orchestrator/scripts/validate_packs.py            # all packs, live warehouse
python orchestrator/scripts/validate_packs.py --advisories
```

It enforces the one rule that decides accuracy — *never teach the model a field
production does not have* — and it blocks on the things that reach the SQL
writer: a phantom field in `tables`, `rules`, `metrics` or `glossary` without a
caveat nearby, metric SQL selecting one, and **duplicate `field_notes` keys**
(YAML silently keeps the last, so the first note vanishes with no error).

Dead field notes and `knowledge` prose are advisory, not failures: the merge
drops unknown notes at load, and knowledge chunks are exactly where sandbox
detail belongs. If a chunk must name a sandbox-only field, open it with

```
NOT IN THE PRODUCTION WAREHOUSE (Dev9 repo/sandbox names — describe them,
never query them): Foo__c, Bar__c
```

and the gate treats the whole chunk as covered for those names only.

## Two things that bite when a pack gets large

- **Field notes are global and last-write-wins.** `brain.field_overlay` walks
  packs in filename order and `sf_dictionary.merge` overwrites, so two packs
  noting the same field means the alphabetically later one wins silently. Keep
  one owner per object, and make an override deliberate.
- **Knowledge is a shared pool with a small budget.** `knowledge_for` returns
  the top 3 across ALL packs inside `BRAIN_MAX_CHARS` (4,000), scoring a
  title/keyword hit 3x a body hit. So: keep chunks near 1,400 chars (one 3,000
  char chunk starves the other two), and keep common query words out of a
  shared title prefix — a prefix of "Training portal — " put `portal` in 308
  titles and displaced the packs that actually own that word.
- **`keywords` must be SINGLE WORDS.** `knowledge_for` stems each keyword as a
  whole string and matches it against the question's individual word tokens, so
  a multi-word keyword — `out of training`, `phase 1`, `service agreement` —
  matches nothing, ever, and is silently dead weight. Write `[out, training]`,
  not `[out of training]`. (Found 2026-08-19 ingesting the CS SOPs: "when does
  marketing start?" was answering from the Phase 1 chunk because the Phase 5
  chunk's `ready to start marketing` keyword could not fire.)
- **Take a before/after retrieval snapshot.** Adding chunks to a top-3 pool is
  never free. Diff what a set of EXISTING questions retrieves with the new pack
  present and absent — that is how both of the traps above were caught, and how
  a keyword worth removing (`signed`, pushing the DocuSign pack off its own
  question) shows itself.
