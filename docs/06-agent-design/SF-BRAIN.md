# The Salesforce Brain — deep research & design (2026-08-12)

How this platform gets a "best brain" for Salesforce: what exists, what was
built, how knowledge files become accuracy, and how the system learns from
chat. Written after a full seven-subsystem code audit (sync, engines,
knowledge layers, RAG, frontend toggle, feedback storage, prior design docs).

---

## 1. How Salesforce answers actually work here (research findings)

**The data.** The sync-worker copies the ENTIRE org (~1,024 objects, full
records, every readable field) into a local DuckDB warehouse
(`/data/warehouse.duckdb`) every 5 minutes — Bulk API full extract first,
then incremental on `SystemModstamp`. Long-text fields are additionally
chunked and embedded (Qwen3-Embedding-0.6B) into LanceDB for RAG. So yes:
all Salesforce data is stored locally. PostgreSQL holds only app state
(users, conversations, messages, feedback).

**The toggle.** The Salesforce chip in the composer sends
`mode: "salesforce"` on `POST /chat` (with optional `sf_live` for the live
variant). In Salesforce mode: web search is hard-blocked, the Intelligence
Mode planner may resolve context or ask one clarification, then the router
(8B model) classifies into sql / rag / vision / report / chat. The SQL engine
writes one guarded SELECT against the warehouse; live SOQL is the fallback
and the `sf_live` path.

**The knowledge (before this work).** Three hand-built layers:

| Layer | File | What it knows |
|---|---|---|
| Vocabulary | `core/sf_dictionary.py` + `/data/sf_dictionary.json` | API names, labels, types, picklists, lookup targets — retrieved per question (top 4 objects) |
| Semantics | `core/org_brief.py` | The business model, 29 canonical metrics, query traps, SQL hard rules, answer honesty rules, ONE domain-rules block (training) |
| Planner | `core/sf_intel/` | Typed live-SOQL plans, clarifications, context budget |

All grounding converges in `org_brief.grounding_for(question)`, injected into
warehouse SQL, live SOQL, and (partially) the sf_intel planner. **Every layer
was Python — only an engineer could add knowledge.** `informaton.txt` and the
schema ZIPs were referenced nowhere.

**The feedback.** Thumbs up/down are stored per message
(`messages.feedback`), and the message's `meta` already carries the exact SQL
that produced the answer. Nothing learned from it — until now.

---

## 2. What was built

```
brain/
├── sources/                          ← raw developer knowledge files
│   ├── invoice-payment-quickbooks.txt   (was ./informaton.txt)
│   └── schema/                          (extracted AI-friendly org export)
├── packs/                            ← compiled packs the orchestrator loads
│   └── qb-invoicing.yaml
└── README.md                         ← how developers add knowledge
```

**`app/core/brain.py`** loads every YAML/JSON pack from `/data/brain`
(bind-mounted from `brain/packs/`, re-read on file change — no restart) and
feeds five knowledge shapes into the existing, proven injection points:

| Pack key | Where it lands at answer time |
|---|---|
| `rules` | `org_brief.domain_rules_for` → SQL + live SOQL + sf_intel grounding |
| `metrics` | `org_brief.match_metrics` → canonical-SQL hints (semantic layer) |
| `tables` | `org_brief.tables_for` → pinned into the 24-table schema slice |
| `glossary` | `grounding_for` extras, when the term appears in the question |
| `knowledge` | lexically retrieved prose → grounding, RAG answers, sf_intel planner |
| `field_notes` | merged into the field dictionary hints — **enrich-only** |

Everything is best-effort and capped (`BRAIN_MAX_CHARS`, default 4000 chars)
because prefill latency on the 35B model is roughly linear in prompt size —
knowledge that doesn't match the question costs nothing.

**Learn-from-chat** (`app/core/learned_examples.py` +
`db.list_confirmed_sql_examples`): every 👍 on an answer whose SQL was stored
becomes a few-shot example, retrieved by lexical similarity for future
questions (max `LEARNED_EXAMPLES_K`, default 2). A 👎 on the same SQL text
anywhere disqualifies it globally. Failures degrade to "no examples", never
to a broken request. **This is the training loop: the more the team uses the
chat and rates answers, the more the SQL writer reuses verified queries.**

**The compiler** (`orchestrator/scripts/compile_brain_source.py`) turns any
future developer txt into a pack draft: section-splits into knowledge chunks,
seeds triggers, validates every `__c` name against the org dictionary
(printing warnings for names the org lacks), and — with the local LLM — can
distill the rules/glossary automatically. Output is a draft for human review.

---

## 3. The one rule that decides accuracy

**Never teach the model a field the production warehouse does not have.**

Verified during this work, column by column: `informaton.txt` documents the
Dev11 sandbox, which runs AHEAD of production. Fields like
`Amount_To_Recover__c`, `Card_Surcharge_Amount__c`, `Settlement_Amount__c`,
`Linked_Recurring_Plan__c`, `Last_EMI_*` **do not exist in production data
yet** — and production holds 71 legacy invoices with `Type__c` NULL plus
`Payment_Status__c` values ('Scheduled') the documented picklist doesn't
mention. A knowledge file ingested naively would have taught exactly the
silently-wrong-SQL failure this platform's whole design exists to prevent.

So the shipped `qb-invoicing.yaml`:
- writes metric SQL only against verified fields;
- names the sandbox-only fields explicitly and tells the model to answer
  those conceptually and SAY the data is not synced yet;
- keeps the full sandbox documentation as retrievable prose knowledge, so
  "how does the recurring EMI plan work?" gets a real answer today;
- lets the enrich-only dictionary merge drop `field_notes` for absent fields
  automatically as each org catches up.

Also fixed while verifying: the deployed `/data/sf_dictionary.json` was stale
(Invoice__c 64 → 76 fields; the whole `Payment_Issue_*` suite was missing).
Rebuilt as: enrich-only merge from the AI-friendly export **plus** additions
filtered to columns the warehouse demonstrably has (113 added, 529 enriched,
backup at `/data/sf_dictionary.json.bak-20260812`).

---

## 4. Why retrieval + packs, not fine-tuning

Considered and deliberately rejected for now:

- **LoRA/fine-tune on chat logs** — needs thousands of verified pairs (there
  are dozens), an eval harness to prove non-regression, a retraining cycle
  per knowledge change, and serving changes on a shared vLLM box. Knowledge
  packs take effect in seconds and are reviewable line by line.
- **Embedding-only RAG over knowledge** — the failure mode of SQL generation
  is vocabulary+rules, not recall; lexical trigger matching is deterministic,
  testable, and free at answer time. (Embeddings can be added later behind
  `brain.knowledge_for` without touching any caller.)

When the learned-examples corpus reaches a few hundred confirmed pairs, a
nightly LoRA on the 8B router/planner becomes worth revisiting — the few-shot
pool doubles as its training set, already cleaned by thumbs.

## 5. Operations

- Add knowledge: drop file in `brain/sources/`, compile, review, ship — see
  `brain/README.md`. Packs go live on the next question.
- Kill switches: `BRAIN_ENABLED`, `LEARNED_EXAMPLES_ENABLED`.
- Tests: `orchestrator/tests/test_brain.py` (pack loading, every injection
  surface, enrich-only overlay, learning loop, the shipped pack itself).
- Deployed 2026-08-12: image rebuilt, container recreated with the
  `./brain/packs:/data/brain:ro` mount, verified live (pack loaded, grounding
  carries billing rules + rollout caveat, dictionary knows the new fields,
  end-to-end chat answered "18 invoices Not Paid" with its population stated).

## 6. The full-org knowledge base ingestion (2026-08-16)

`Techsara_Org_Knowledge_Base.txt` (1 MB, 71 objects in 11 parts, written
against the PREPROD org) became eleven packs via a pipeline, not the one-shot
compiler — the file was ~50× larger than anything ingested before:

1. **Deterministic parse** (scratchpad `parse_kb.py`): PART/OBJECT/section
   structure → 591 knowledge chunks (≤1,400 chars, titled `Object — Section`);
   the per-field entries of every "8. FIELDS" section → 1,297 field notes.
   "REAL DATA SNAPSHOT" sections were dropped entirely (preprod counts;
   the warehouse is the only source of data facts). "Activity" notes were
   remapped to the real Task/Event tables.
2. **Production validation**: every field checked against
   `/data/sf_dictionary.json`, every table against the warehouse's 1,064
   tables. Not in production: `QB_Plan__c`, `In_App_Checklist_Settings__c`,
   the `__mdt` configs, Task/Event (not synced), and 22 documented fields
   (e.g. `Pre_Enrolment_Request__c.Candidate__c`,
   `Current_State_in_USA__c`) — all shipped only as explicit
   "not synced" caveat lines, never as queryable facts.
3. **Distill + adversarial verify** (18-agent workflow): one distiller per
   domain wrote rules/glossary/triggers/tables from the part text under the
   verified-fields whitelist; an independent verifier then re-read the source
   and tried to refute every rule line. All nine domains came back
   `ship_with_fixes` — real catches included an invented tag correlation, a
   NULL-poisoned SUM formula, an overgeneralized status list, and wrong
   sandbox-vs-production provenance.
4. **Assembly + gate**: packs `kb-*` = verified rules + deterministic
   knowledge + field notes (notes already curated in qb-invoicing /
   internal-interview stay authoritative and are excluded). Parts 5 and 9
   shipped knowledge-only (`kb-internal-interviews-kb`, `kb-billing-kb`)
   because those verified packs already own the rules/triggers. A mechanical
   gate (`validate_packs.py`) fails any pack naming an unverified `__c`
   field without a nearby caveat.

Result: 13 packs, ~1,150 knowledge chunks retrieved lexically, ~1,300
dictionary field notes. All 88 brain+org_brief tests pass (one stale probe
updated: interviews now legitimately own a domain-rules block). Verified in
the live container; note that `sf_dictionary.load()` caches per process, so
field-note enrichment needs an orchestrator restart even though
rules/knowledge/glossary hot-reload — the 2026-08-16 restart covered that.
