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
