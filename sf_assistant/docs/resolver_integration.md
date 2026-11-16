# Resolver wiring plan

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

**Status (2026-10-20, review fixes 2026-10-21, leftovers 2026-10-21, on by default 2026-10-27): wired behind
`SFA_RESOLVER`, on by default.** Steps 0 to (7) are built and tested, the knowledge side of step (3) and the E4h order
of step (5) included; after the owner's evening evaluation (8) of 2026-10-22 the owner ordered on 2026-10-26 "make it
true and the required changes as we will use it from now to understand the miswritten words": unset or empty is on,
`SFA_RESOLVER=off` turns it off (the parity harness and the evaluation's reference replay always run with it off). With
the switch off every resolver code path is the old one (`.private/parity_2026-10-21_review_fixes.md`; before the review
fixes `.private/parity_2026-10-20_resolver_wiring.md`; P0 lists each OLD definition the wiring touched, with its
reason). The Group B people fixes of the same day ([people.md](people.md)) are the first deliberate change with the
switch off; `.private/parity_2026-10-21_groupb.md` explains each of their differences, and the resolver's own
definitions still take OLD's path when off. With it on, the behaviour below; the model-free checks on the real data are
in `.private/resolver_wiring_checks_2026-10-20.md` (real names, not tracked). How to switch it off:
[resolver.md](resolver.md#how-to-switch-it-off).

Owner decisions: **A1** (the matcher and the knowledge vocabulary moved to `knowledge/resolver.py` and
`knowledge/terms.py`) and **B** (a value, object or field is bound automatically only when the resolver says
`corrected`, and both the tool result and the answer state it; `ambiguous` gives a did-you-mean, `none` today's
not-found text and closest names; a person correction is stated first; a filter never binds a misspelt person).

| step | status | where | tests |
|---|---|---|---|
| 0 (A1) | done | `knowledge/resolver.py`, `knowledge/terms.py`; `engine/vocabulary.py` keeps the snapshot part | `test_resolver.py`, `test_layering.py` |
| Build | done | `config.ResolverConfig` (`SFA_RESOLVER`, `SFA_LEXICON`), `pipeline.build` (knowledge resolver), `open_records` / `attach_spelling_help` (full resolver), start-up lines, `web/Dockerfile` word lists, `web/compose.yaml` passes `SFA_RESOLVER` | `test_resolver_wiring.py`, `test_settings.py`, `test_web.py` |
| (1) hints | done | `RecordTools.object_hints` (override), `knowledge_agent.question_spelling`, `prompts/spelling_hints.md`, `pipeline.answer` (the knowledge step's block) | `test_resolver_hints.py`, `test_resolver_answer.py` |
| (2) BM25 | done | `knowledge/search.py` (`resolver_expander`, `KnowledgeIndex.search_read_as`); the knowledge tools; the catalog's object and field indexes (`Catalog.attach_resolver`) and `find_objects` `read_as` | `test_resolver_hints.py`, `test_resolver_catalog.py` |
| (3) objects, fields | done (knowledge side 2026-10-21) | record side: `READ_AS`, `Catalog.spelling`, `resolve_object`, `field`, `unknown_field_text`, `RecordTools.call`, `_log`; knowledge side: `MultiToolRegistry._close_object`, `ToolRegistry._close_field` / `_close_field_names` / `_fields_elsewhere`, the `ARGUMENT_READ_AS` channel, `read_as` (`read_as_arguments` next to a search tool's expansion dict, not stated) | `test_resolver_catalog.py`, `test_resolver_knowledge_tools.py` |
| (4) values | done | `ground_value`, `value_error` | `test_resolver_catalog.py` |
| (5) people | done (the several-people nudge 2026-10-21) | `tools/names.py` (`_spot`, `resolve_person_name`, `staff_close_name`, `_people_hints`), `tools/people.py` (`_resolve_asked`, E15a), `tools/lookups.py` (`_e4h`: the resolver's names first, 2026-10-21), `agent/nudges.py` (`people_nudges`: a first name the spelling help read that several people share), `Triage.as_dict` | `test_resolver_people.py`, `test_agent_groupb.py` |
| (6) answer line | done | `with_read_as_line` (`tools/knowledge_tools.py`), `prompts/read_as_line.md`, `QuestionSpelling.final`, `pipeline.answer`, `merge.combine(name_words=...)` | `test_resolver_answer.py`, `test_resolver_hints.py` |
| (7) tests, checks | done | flag-on twins in the test files above; the suites pass with the switch off and on, also with `OKEVAL_RECORDS_LIVE=1` | `.private/resolver_wiring_checks_2026-10-20.md` |
| (8) evening run | done (owner, 2026-10-22); on by default since 2026-10-27 | `config.ResolverConfig.enabled` (True), `web/compose.yaml` (empty = on) | `test_settings.py`, `test_web.py`, `test_resolver_wiring.py` |
| value spots (2026-10-28) | done | `tools/value_spots.py` `_spelling_value_spots`: the question's `corrected` spans (never `ambiguous`, never a person, never a cut field) are matched again in the entity index, `read_as` on the spot ([value_spots.md](value_spots.md)); the vocabulary is unchanged | `test_value_spots.py` |

Still open:

- **(5), the several-people nudge:** built in the Group B review (2026-10-21): a `first_only` spot with `read_as`
  gets `RECORDS_SEVERAL_PEOPLE_MESSAGE` when the draft names exactly one of the full names in `names` (the stored
  first name alone is no pick; `<<WORD>>` is the stored first name), next to the not-exact-name check.
- `_close_surname` stays: the switch is on by default since 2026-10-27, but `SFA_RESOLVER=off` still takes the
  switch-off path that uses it (with the switch on no entry point calls it,
  `test_close_surname_runs_only_with_the_switch_off`). The optional 0.7 s saving
  of "Build" is done another way (review 2026-10-21): `terms.start_vocabulary` keeps the knowledge and word-list part
  per process, keyed by the files it read.
- The page and the MCP server get the switch from `web/compose.yaml` (empty = the code default, on since
  2026-10-27); the image holds the word lists (`web/Dockerfile`). A start without the word lists leaves the spelling
  help off with a start-up line.

Review fixes (2026-10-21; the checks are in `.private/resolver_wiring_checks_2026-10-20.md`, "Review fixes"):

- **Numbers.** No reading changes a number: the doubled-letter tier keeps digits (`'01:55 PM'` had read as
  `01:15 PM`, `'H11B'` as `H1B`), a `corrected` reading with other numbers is a did-you-mean, `_close_value` refuses
  other numbers, and E4's did-you-means for a value typed with numbers keep them (`'9:30 AM'` -> `'09:30 AM'`).
- **Hinglish.** `knowledge/spoken_words.txt` (romanized Hindi and Gujarati question words) are known words next to
  any English list ("kaunse" had read as the stored value Kansas); a question span is stated in the "Read as" line
  only when the answer or a successful call used it.
- **People.** A one-word reading before an unknown surname that no stored record of that first name has is no spot
  (an unstored full name is a missing name); `question_people()` carries `read_as`, the not-exact-name nudge also
  fires for a first-name-only draft, and code leads the final answer with "No record is named 'x'; the closest
  stored name is 'Y'." when it still does not name the asked spelling (`prompts/read_as_person_line.md`).
- **Fields and values.** Another object's exact field API name is never read as a field of the queried object; text
  fields named for people (vendor, trainer, owner, contact, manager, POC/SPOC ... `_Name`) give no stored values.
- **Settings.** An invalid `SFA_RESOLVER` is never fatal (off, one line; `web/run.sh` refuses it before starting);
  `settings.resolver` reaches every session open of the page and the MCP server (record tools and, new, its
  knowledge registry); the evaluation's reference replay always runs with the switch off.
- **Cost.** A reopened page or MCP session releases the old one before the new open (the old resolver was kept alive
  during and after each reopen), and the knowledge and word-list part is read once per process. The measured cost
  table is in [resolver.md](resolver.md#how-to-switch-it-off) ("Cost").
- **Layering.** `question_spelling`, the Read-as pairs and the one-word name words moved from
  `agent/knowledge_agent.py` (knowledge side) to `pipeline.py` (wiring); the knowledge side keeps the span list and
  the block.
- **Image and harness.** `web/Dockerfile` checks the word lists' SHA-256 (no rebuild was made); the parity harness
  exports `SFA_RESOLVER=off`, asserts it, passes the switched-off config explicitly (`ResolverConfig(enabled=False)`
  since the default became on on 2026-10-27), and computes the code version and switch state when it writes the
  report.

Leftovers built on 2026-10-21 (checks in `.private/groupb_checks_2026-10-21.md`):

- **(3), knowledge side:** with the switch on, an object or field argument of the knowledge tools that no exact
  tier knows is used when the resolver says `corrected` with one target inside a tool call, and the result says so
  in `read_as` (the existing "Read as" line states it); an `ambiguous`, a missed `exact` or a `corrected` reading
  outside a call is a did-you-mean in the error; `none` keeps today's error. Another object's exact field API name
  is never bound; `find_links_to_object`, tool names and process ids stay exact. Off, every result is byte-equal.
  Where it differs from the plan: next to a search tool's own `read_as` dict the argument list is
  `read_as_arguments` and is not stated in the answer (that would need `agent/knowledge_agent.py`); the "check the
  object" sentence was dropped (generic names listed unrelated objects).
- **(5), E4h order:** with the switch on, the "Close spellings" of E4h (and `not_found` "close") start with the
  resolver's names, records of the filter's target objects only; the code stays E4h and the count never runs. A
  resolver that fails leaves today's list.

Where it differs from the plan below (each one a choice to confirm):

- **No word list, no spelling help.** Without a readable word list the resolver is not built (`spelling help off:
  no English word list could be read`), not only warned about: Decision B binds `corrected` values, and without a
  word list ordinary words were corrected in the benchmark.
- **A bad switch value** (`SFA_RESOLVER=maybe`) never stops anything (review 2026-10-21, the plan's "never fatal"):
  every entry point prints one `spelling help off: SFA_RESOLVER must be off or on` line and carries on with it off;
  `web/run.sh up` / `up-mcp` refuse such a value before starting the containers.
- **(2)** The expander lives in `knowledge/search.py` (not `knowledge/resolver.py`), and it never reads a correct
  English or knowledge word (`is_known_word`), even one an index lacks: the catalog's object index is small, and
  without that rule right words of the control questions ("receive", "resulted", "core") changed the hints' object
  list.
- **(3)** A resolver `exact` reading that the catalog's own tiers missed (fillers, plurals) only gives a
  did-you-mean. A fourth `how`, "closest picklist value", names a knowledge value no record has yet. Field
  did-you-means leave out blocked and not-synced fields. A failed call keeps its corrections in the evidence.
- **(5)** `_close_people` and the G2 spelling query still run after the resolver in `about_people` with today's
  binding rules, so they can bind a name the resolver calls ambiguous. A full-name did-you-mean is a missing name with
  suggestions instead of a partial first-name match.
- **(6)** A pair is left out of the "Read as" line only when the answer names both the typed and the used spelling
  (the plan said: when it names the stored spelling); a question span is a pair only when the answer or a successful
  call used it; a person correction the final answer does not state gets the code lead sentence. `with_read_as_line` lives in `tools/knowledge_tools.py`, not next to
  `with_lookup_footer` (the knowledge side, which uses it, may not import the record-side `agent/nudges.py`). With records off the knowledge block keeps only `corrected`
  spans.

The rest of this page is the plan as written on 2026-10-20 against the stage-1 tree (`src/sf_assistant` as of
05:05 IST, while stage 1 was still being finished). Line numbers will move, so the function names are the anchors.
"OLD" means `notebooks/object_knowledge_eval` (`okeval/`, `okrec/`, `ask.py`). OLD stays untouched; it is listed only
so that each site can be traced back.

The plan wires in the resolver of [resolver.md](resolver.md): `build_vocabulary`, `build`, `resolve_term` and
`spell_correct`.

## Contents

- [0. Before wiring: preconditions and decisions](#0-before-wiring-preconditions-and-decisions)
- [1. Build and ownership](#1-build-and-ownership)
- [2. Call sites](#2-call-sites)
  - [(1) Question spelling hints for both agents](#1-question-spelling-hints-for-both-agents)
  - [(2) Knowledge BM25 query expansion](#2-knowledge-bm25-query-expansion)
  - [(3) Catalog objects and fields](#3-catalog-objects-and-fields)
  - [(4) ground_value close values](#4-ground_value-close-values)
  - [(5) Person names](#5-person-names)
  - [(6) How the answer states each correction](#6-how-the-answer-states-each-correction)
  - [(7) Tests and model-free checks](#7-tests-and-model-free-checks)
  - [(8) Evening evaluation](#8-evening-evaluation)
- [3. Work estimate](#3-work-estimate)
- [4. Risks](#4-risks)

## 0. Before wiring: preconditions and decisions

1. **Stage 1 is signed off first.** Parity (`tests/parity`, `.private/parity_*.md`) is the stage-1 contract: NEW
   behaves exactly as OLD. Every change below changes behaviour, so the work starts only after the parity report passes,
   and it builds on that tree.
2. **One switch.** `SFA_RESOLVER` (`off` or `on`) feeds a new `Settings` field. The default is `off` until the evening
   run passes, and `on` after that (on since 2026-10-27, by the owner's order of 2026-10-26). With the switch off, `catalog.resolver`, `registry.resolver` and every
   `KnowledgeIndex.expand` are None, and every code path below is byte-identical to today. The parity suite and the
   existing tests then run unchanged. A second setting, `SFA_LEXICON`, gives the word-list paths (default: the system
   lists). Both variables go in `config.ENV_DOC`, which `tests/test_settings.py:90` checks.
3. **Decision A: where the resolver lives.** This is needed before (2), and before any records-off run can use it.
   - The finding: `tests/test_layering.py` orders the layers config/prompts 0 < knowledge 1 < engine 2 < tools 3 <
     agent 4. Three rules block the current location:
     - The knowledge-side modules (`KNOWLEDGE_SIDE`: `knowledge/search.py`, `tools/knowledge_tools.py`,
       `agent/knowledge_agent.py` ...) may import nothing under `sf_assistant.engine`
       (`test_knowledge_side_never_imports_record_code`).
     - A `--no-records` run may load no `sf_assistant.engine*` module
       (`tests/test_pipeline_cli.py::test_no_records_imports_neither_record_modules_nor_duckdb`).
     - So `engine/resolver.py`, where it is now, cannot serve the BM25 search, the knowledge tools or a records-off
       run. The resolver's docstring, which says "engine is the lowest layer", is wrong: knowledge is lower.
   - **A1 (recommended): move the standard-library matcher one layer down.**
     - `engine/resolver.py` moves to `knowledge/resolver.py`.
     - `engine/vocabulary.py` is split. The knowledge and word-list part (`load_knowledge`, `add_knowledge`,
       `load_lexicon`, and a new `knowledge_vocabulary(knowledge_dir, lexicon)`) moves to `knowledge/terms.py`.
       `SnapshotPolicy`, `add_snapshot` and `build_vocabulary` stay in `engine/vocabulary.py`, which imports both
       (engine may import knowledge).
     - Both new modules go into `KNOWLEDGE_SIDE` in `tests/test_layering.py`. The imports of `tests/test_resolver.py`
       and `docs/resolver.md` follow.
     - There is no logic change. The resolver tests and the benchmark must give the same numbers as before.
   - **A2: keep the files where they are.** `pipeline` belongs to the wiring layer and may import engine. It would pass
     plain callables to the knowledge side, but only when records are on, because a records-off run cannot import
     engine. Records-off runs and `--no-records` would then get no spelling help. That matters, because "what is
     accont" and "how does interviw persn work" are knowledge questions.
4. **Decision B: binding a value.** The `engine/values.py` docstring (and RECORDS_SPEC §11.4) says "Nothing fuzzy is
   ever bound automatically". Step (4) binds a value only when the resolver says `corrected`, and both the result and
   the answer say so. The owner needs to confirm this. Without that confirmation, (4) gives did-you-means only. The
   spec copy that stage 2 moves gets the new rule; the OLD file stays as it is.
5. **The LAN page.** It runs OLD code (`web/pipeline.py` imports `ask` from the notebook folder) until stage 2 switches
   it over, so it gets none of this before that switch.

## 1. Build and ownership

| resolver | vocabulary | built in | used by | cost (measured 2026-10-20) |
|---|---|---|---|---|
| knowledge resolver | the knowledge files and the English word list | `pipeline.build()` (pipeline.py:38), only when the switch is on | the knowledge tools, the knowledge BM25 expansion, and the knowledge agent's hints when records are off | plan-time estimate; measured: [resolver.md](resolver.md#how-to-switch-it-off) "Cost" |
| full resolver (`catalog.resolver`) | the same, plus the snapshot's person names and stored values | `open_records()` (tools/open_records.py:49), right after `Catalog.build(wh, knowledge_dir)` | the record tools, the catalog, values, people, and both agents' hints | plan-time estimate; measured: [resolver.md](resolver.md#how-to-switch-it-off) "Cost" |

- **One entry point.** A new `Catalog.attach_resolver(resolver)` sets `catalog.resolver` and the expander of
  `_object_index` (catalog.py:911). It also sets the expander on every `ObjectSpec`; `field_index()` (catalog.py:307)
  passes that expander to the `KnowledgeIndex` it creates on first use.
- **Snapshot query function.** Use the warehouse's guarded connection, never the file path:
  - `lambda sql: wh.run(sql, max_rows=500_000, max_bytes=100_000_000, timeout_s=rcfg.timeout_s).rows`
  - with `columns=wh.columns()` and `policy=SnapshotPolicy.from_guard(cat.allowed, cat.blocked_columns, cat.blocked_fields)`.
  - Step 1 of the "Wiring" section in `docs/resolver.md` needs a fix: its `max_rows=600` would silently cut the
    person statements. In the current snapshot those statements return thousands of rows (person Accounts, Leads).
  - Check `Statement.truncated`, count it in `vocab.info["snapshot"]`, and show a warning on the start-up lines when
    it is not zero.
- **Optional saving (0.7 s).** `open_record_session` passes the knowledge Vocabulary on, and `open_records` adds the
  snapshot terms to a copy of it instead of reading the knowledge and the word list again.
- **Never fatal.** Any exception leaves the resolver at None and prints one start-up line,
  `spelling help off: <reason>`. Records stay on.
- **Word list.**
  - The image (`python:3.11-slim`, `web/Dockerfile`) has no `/usr/share/dict`. Install `wamerican wbritish` in the
    Dockerfile (the same lists the benchmark used), or set `SFA_LEXICON`.
  - With no word list, `vocab.info["lexicon"]["warning"]` gives a start-up line, and the switch should stay off.
    Without a word list, ordinary words get flagged as typos (see "Controls" in resolver.md).
- **Life cycle.**
  - There is one resolver per session. The web pipeline reopens the session when the snapshot file is replaced
    (a new inode), so the stored values and names follow the snapshot.
  - The resolver is read-only after `build`, so the record agent's thread and the knowledge agent's thread can share it.

## 2. Call sites

### (1) Question spelling hints for both agents

Record agent:

| site | change |
|---|---|
| `NamesMixin.object_hints` (tools/names.py:50; OLD okrec/tools.py:616) | (a) `spans = cat.resolver.spell_correct(question)`, cached per question (cleared in `begin_question`, record_tools.py:141). (b) `fixed` is the question with every `corrected` span replaced by its suggestion. Use `fixed` for `catalog.search_objects` (names.py:54), `value_matches` and `value_conditions` (names.py:73-75): "backgrond chech" then finds the background-check objects, and "Sheduled" finds the stored value. (c) A first section, "Spelling (read by code)", with lines such as `- 'accont' -> Account (object)` and `- 'persn' may mean: <s1>, <s2> (say which one you used, or ask)`. Person spans are not listed here; the people lines of `_people_hints` cover them. |
| `RecordSession.ask` (agent/record_session.py:560) | No change: it already sends `object_hints(...)` with the question. |
| `RecordSession.triage` (agent/record_session.py:214) | Same shape. It gets better spots from (5). `Triage.as_dict` (agent/triage.py) gets a `spelling` list (typed text, decision, suggestion) for the JSONL sidecar. |

Knowledge agent:

| site | change |
|---|---|
| `pipeline.answer` (pipeline.py:172; OLD ask.py:226-290) | After `q = Question(...)` (183): `block = spelling_block(text, resolver)`. Use the full resolver when a session is open, else the knowledge resolver. Keep only spans whose candidates are metadata: `object`, `field`, `value` with source `picklist`, `record_type`, `component`. This way the knowledge side never sees stored values or person names. Pass `kq = dataclasses.replace(q, question=text + "\n\n" + block)` to the four `answer_question(q, ...)` calls (191, 219, 223, 236) and to `lean_knowledge_answer(q, ...)` (214). `q`, `text` and the JSONL keep the user's own words. |
| text | A new prompt file, `prompts/spelling_hints.md` (`<<LINES>>`): "Spelling (read by code, not from the user): ... Use these names. If your answer rests on one, say how you read the word. For a 'may mean' line, say which one you assumed, or ask." It is bound in the module that uses it. The records side's hints section uses the same file and the same line format. |

Both agents see the same spans; only the kinds they see differ.

The route call and the names call keep the raw question. If the typo set (8) shows a typo changing the route, a later
step can give them the `fixed` question instead.

### (2) Knowledge BM25 query expansion

| site | change |
|---|---|
| `KnowledgeIndex.__init__` (knowledge/search.py:101) | Add `self.words = set().union(*self._docs)` and `self.expand = None`, where `expand` is a callable `(query, unknown_tokens) -> [(typed, [suggestion texts])]`. |
| `KnowledgeIndex.search` (search.py:118; OLD okeval/search.py:118) | Keeps its signature and its result. A new `search_read_as(query, k, section)` returns `(hits, read_as)`, and `search` returns its hits. When every query token is in `self.words`, or `expand` is None, the expander is never called, so correct queries rank exactly as they do today. Otherwise, the expander's tokens that are index words are added to the query once each. |
| expander (under A1, in `knowledge/resolver.py`) | First `spell_correct(query)`, then `resolve_term(tok, wide=False)` for each token that is still unknown. This is the strict scoring, because a search word is not a tool argument. `corrected` adds the suggestion's tokens. `ambiguous` adds only the tokens that every suggestion shares (for `persn`, the word all its candidates contain). Kinds: the knowledge resolver uses every kind it has; the catalog indexes use the metadata kinds. |
| report | `ToolRegistry._search_knowledge` (knowledge_tools.py:759), `_search_fields` (428) and `MultiToolRegistry._search_all` (902) call `search_read_as` and add `read_as: {"backgrond": "background"}` to the result. The fallback in `_suggest_fields` (181) only uses the hits. |
| attach | In `pipeline.build` (pipeline.py:55-58): the index of the `ToolRegistry`, or the shared index and each per-object index of the `MultiToolRegistry` (knowledge_tools.py:814, 823). The catalog's indexes get theirs through `attach_resolver`. |
| leave as is | `evaluation/grading.py:708`, `evaluation/questions.py:1530` and `server/knowledge_mcp.py:122`: `expand` stays None there, so retrieval grading does not move. |

### (3) Catalog objects and fields

The goal is a "did you mean" for unknown names, plus automatic use of one clear match, with the correction stated.

**Correction channel.**

- A `contextvars.ContextVar` named `READ_AS` in `engine/catalog.py` holds the list for the current tool call.
- `RecordTools.call` (tools/record_tools.py:209) sets it around `tool.handler(...)` (235) and resets it in `finally`.
- Outside a tool call (hints, tests, the catalog build) it is unset. There the catalog never uses a match on its own;
  it raises the did-you-mean error instead.
- This avoids passing a list through the 20 or so call sites of `resolve_object`, `field` and `resolve_path` (in
  record_tools.py, records.py, discovery.py, meta.py, people.py and query_builder.py).

Each correction has the form `{"arg": <argument path>, "typed", "used", "kind", "target", "how"}`, where `how` is
"closest object name", "closest field name" or "closest stored value". It is recorded in four places:

- **Tool result.** `result["read_as"]` (record_tools.py, next to the notes at 250). Add `"read_as"` to
  `_PROTECTED_KEYS` (record_tools.py:61) so that `_fit` never drops it. There is also one note per correction, for
  example `object 'Interviw' read as 'Interview__c' (closest object name)`.
- **Evidence.** A new `QueryEvidence.read_as: list[dict]` (engine/types.py:55, default `[]`), filled in `_log`
  (record_tools.py:292).
- **Failed calls.** An optional `RecordError.did_you_mean` feeds a new `QueryEvidence.did_you_mean`.
- **Error codes stay the same.** `NOT_FOUND_CODES` (agent/checks.py:22), `NAME_NOT_FOUND_CODES`
  (agent/nudges.py:1289) and every nudge keep working.

| site | change |
|---|---|
| `Catalog.resolve_object` (engine/catalog.py:960; OLD okrec/catalog.py:961) | After the four exact tiers and before the E1c/E1 raise, call `resolve_term(raw, kinds=["object"])`. A `corrected` match inside a tool call is used as the object. Then the `has_table` check gives E1b, with "read 'X' as Y" in its text, and the callers' sensitive-object checks (E1d) run as before. `ambiguous` gives E1: "unknown object 'X'. Did you mean A or B? ... Call find_objects with the user's words." `none` keeps today's E1 text (the BM25 and difflib "Closest" list). |
| `Catalog.field` (catalog.py:1003; OLD 1004) | Before `raise RecordError(self.unknown_field_text(spec, raw), "E2")`, call `resolve_term(raw, kinds=["field"], context_object=spec.api, within=f"{spec.api}.")`. A `corrected` match gives the field when it is in `spec.fields`. A documented field that is not synced raises `_not_synced` (E2b), as today. Sensitivity checks stay with the callers (`resolve_path` E2c at each hop, plus the filter, group and list checks), so a correction never skips them. |
| `Catalog.unknown_field_text` (1133) / `suggest_fields` (1099) | The resolver's suggestions come first in "Closest:", then today's list, 3 names in all. |
| `Catalog.resolve_path` (1148) | No change: the hops and the last field go through `field()`. |
| `DiscoveryMixin._describe_fields`, direct match (tools/discovery.py:131) | Clear `READ_AS` around this `cat.field(spec, q)`. A search query is not a field argument, and the BM25 expansion handles its typos. |
| `DiscoveryMixin._find_objects` (discovery.py:27) | `search_objects` already expands through `_object_index`. Add the `read_as` it reports to the result. |
| knowledge side (A1 only) | `MultiToolRegistry._resolve_object` (tools/knowledge_tools.py:858, exact matches only today; OLD okeval/multi.py:154) gets the knowledge resolver's object tier. `ToolRegistry._resolve_field` and `_suggest_fields` (163/170, difflib today; OLD okeval/tools.py:181/188) get the field tier with `within=f"{api}."`, with its suggestions first. Their results carry `read_as`, and `_execute_tool` (agent/knowledge_agent.py:150) copies it into the trace record. Tool names (105, 875), process ids (238) and relationship facets (634) keep difflib. |

### (4) ground_value close values

| site | change |
|---|---|
| `ground_value` (engine/values.py:131; OLD okrec/values.py:131) | A new tier goes after the knowledge-picklist match and before the `entry.truncated` -> None / `value_error` step: `resolve_term(s, kinds=["value"], context_object=obj, within=f"{obj}.{f.api}=")`. It binds only when the match is `corrected`, the call is inside a tool call, and the index is not truncated. Map the suggestion with `normalize()` to the stored spellings, or to a picklist value with no records (the existing `zero` path), and bind it. Add the note `'Completd' read as 'Completed' (closest stored value of Status__c)`, set `Grounded.read_as = (typed, bound)`, and append to `READ_AS`. The "legacy value", "knowledge spells it" and "also stored in" notes follow as before. |
| truncated value index (more than 500 values) | Never bound, because the typed value may be stored beyond the index. The `lower(c) = lower(?)` compare stays. |
| `value_error` (values.py:96; OLD 96) | When the resolver has suggestions, `Did you mean: 'Completed' (<n>), ...?` replaces the difflib ` Close: ...` clause. The first sentence, the stored list, "Knowledge values with no records" and the last sentence keep their text (`tests/test_engine_values.py:71-80`). |
| callers | query_builder.py:459 and 471 (eq, in), 497 and 525 (ne, not_in): no change. The new note must not match `_ALSO_RE` (record_tools.py:85). |

Record-type values can follow the same pattern later (`kinds=["record_type"]`, `within=f"{obj}.RecordType="`).

### (5) Person names

The rule: a person name inside a filter is never bound automatically, because a count for the wrong person is the
worst error this system can make. People are corrected where the question is read (spots, the names call,
about_people), using the existing "closest stored name" wording.

| site | change |
|---|---|
| `NamesMixin._spot` (tools/names.py:217; OLD okrec/tools.py:3331) | Keep the exact passes. Replace the `_close_surname(nxt, ...)` step (names.py:258; OLD 3372) and add a pass over the words not used yet: the question's `spell_correct` spans that have `person` in `kinds`. A `corrected` span becomes a spot `{"words": typed key, "asked": typed, "close": [stored name], "recs": records of the stored name, "first_only": one word}`. An `ambiguous` span gives no spot; it appears as a missing name with did-you-means (below). |
| `NamesMixin.resolve_person_name` (names.py:378; OLD 3492) | After `_close_people` and before `_name_word`, call `resolve_term(name, kinds=["person"])`. `corrected` gives `match="closest stored name"` with `asked`, `close` and `recs`. `ambiguous` returns None (the triage then lists the name as missing), and the suggestions are kept for the hint line. |
| `NamesMixin.staff_close_name` (names.py:363; OLD 3477) | Replace `_close_word` over the staff first names with resolver candidates (corrected or ambiguous) whose target object is `Recruiter__c` or `User`. |
| `PeopleMixin._resolve_asked` (tools/people.py:77; OLD okrec/tools.py:2194) | One word (people.py:138-147): after the exact first-name and last-name lookups, a resolver `corrected` match (doubled letters or an identical sound) gives `match="closest stored name"` with today's note. Two or more words: `resolve_term(v, kinds=["person"])`, limited to `objs`, replaces `_close_surname(toks[1], ...)` (people.py:150; OLD 2266). `_close_people` and the G2 spelling query stay after it. `ambiguous` means not found, and E15a's close list (people.py:219-245) starts with the resolver's suggestions. |
| hint line for a missing name (names.py:676-678) | Add "did you mean X or Y?" when the resolver has suggestions. |
| E4h close spellings (`LookupsMixin._close_parts`, tools/lookups.py:122; G2 at 93) | The resolver's suggestions, limited to the filter's target objects, come first in "Close spellings:", followed by the DuckDB `jaro_winkler_similarity` rows. The error stays E4h. |
| `_close_surname` (tools/record_base.py:209; OLD 5884) | Removed, with its imports (names.py:21, people.py:22), once both callers have moved. `_close_word` and `_same_name_words` (names.py:908, 917) stay; they handle reordered names, initials, and extra or missing words. |

- **Map back by name, not by Id.** The resolver's `person:Obj/Id` targets come from its own read, which leaves out
  test, demo, non-human and digit names. Look the suggestion up in the name index instead (`_names_full`,
  `_names_first` and `_names_last`, by `name_key`).
- **Unchanged:**
  - the names call and `parse_names` (a name's words must occur in the question, so names stay as typed);
  - the hint "'x' is not a stored name; the closest stored name is 'Y'. If you answer for it, say ..."
    (names.py:635-639);
  - `people_nudges` and `RECORDS_NOT_EXACT_NAME_MESSAGE`;
  - the lead sentence of `people_answer` (agent/people_answer.py:100).

### (6) How the answer states each correction

| correction | stated by | text |
|---|---|---|
| a question word or phrase, `corrected` (metadata) | a code line | `Read as: 'accont' as Account; 'backgrond chech' as Background Check.` |
| a tool argument used automatically (object, field, value) | the tool note (seen by the model), then the same code line from `QueryEvidence.read_as` | the same line |
| a person, `corrected` (spot, names call, about_people) | the lead sentence, as today for a close name | "No record is named 'x'; the closest stored name is 'Y'." (`people_answer`, the not-exact-name nudge, `with_name_note`) |
| `ambiguous`, any kind | the agent, told by the hint | it names the one it assumed, or it asks; there is no code line |
| a person not found, with did-you-means | the not-found answer written by code | `not_found_answer`, with the resolver's suggestions first in `close` |
| BM25 expansion only | `read_as` in the search result (seen by the model) | no code line |

- **The code line.**
  - A new `with_read_as_line(text, corrections)` goes next to `with_lookup_footer` (agent/nudges.py:1460).
  - `pipeline.answer` calls it after the lookup footer (pipeline.py:268), for every answer kind: merged, records and
    knowledge.
  - Its corrections are the question's `corrected` metadata spans plus the `read_as` of the successful record calls
    and knowledge tool calls, one per typed text.
  - It is skipped when the text already names the stored spelling.
  - It goes before a last `Source:` line.
  - Its text is a new prompt file, `prompts/read_as_line.md` (`<<PAIRS>>`).
- **The line goes in `final` only, never in `body`.** `score_records` scores `body`, so the golden set does not move.
  The line is added after the number audit and the merge audit.
- **One-word names.** `name_notes` (agent/merge.py:377) only keeps quoted names of two or more words. For a one-word
  name that the triage corrected, `with_name_note` (403) takes the asked spelling and the stored spelling from the
  triage's structured data instead of from the regex.

### (7) Tests and model-free checks

New tests go in `tests/test_resolver_wiring.py`. They use fake data only: `tests/warehouse_fixture.py`,
`tests/fixtures/Internal_Interview__c.json` and made-up vocabularies.

1. **Switch off.** `catalog.resolver` is None. `resolve_object("Interviewz")` still raises E1, `ground_value(..., "Offer")`
   gives today's E4 text, and `object_hints` and `KnowledgeIndex.search` are byte-equal to today.
2. **BM25.** A typo query returns the clean query's top chunk. A correct query never calls the expander (checked with
   a spy) and ranks the same as today. Only index words are added. `read_as` appears in `search_knowledge` and
   `search_all_knowledge`.
3. **Catalog.**
   - An object typo inside a tool call is `corrected`: `READ_AS` is filled, and the note, `result["read_as"]` and
     `QueryEvidence.read_as` are present.
   - Outside a tool call, a match is never used automatically.
   - `ambiguous` gives E1 with "Did you mean", and the old substrings are still there.
   - A typo of an object with no table gives E1b; a typo of a sensitive object gives E1d.
   - A field typo gives the field. A typo of a blocked field gives E2c; a typo of a field that is not synced gives
     E2b.
   - `describe_fields(query=<typo>)` records no read-as.
4. **Values.**
   - `Ofer Received` binds `Offer Received` and adds the note.
   - `Offer` stays E4 (missing words make it a did-you-mean), with every old section.
   - A truncated index never binds.
   - In an `in` list, each value is grounded on its own.
   - The new note does not match `_ALSO_RE`.
5. **End to end on the fixture.** A `count_records` call with typos in the object, the field and the value succeeds,
   and `read_as` survives `_fit`. Failed calls keep their error codes.
6. **People.**
   - `Dellta Person` (a doubled letter) is spotted with `asked` and `close`.
   - `Delta Persn` is still found after `_close_surname` is removed.
   - A one-word typo that is neither a doubled letter nor an identical sound gives no spot; it becomes a missing name
     with a did-you-mean.
   - `resolve_person_name` and `about_people(["Kilo Zephir"])` still give "closest stored name".
   - The E15a and E4h close lists start with the resolver's names.
   - A filter never binds a corrected person.
7. **Hints.** A typo question lists the right object and the spelling section. The knowledge block never holds a
   person or a stored value. Fake control questions give no spelling section.
8. **Answer line.** There is one line, before `Source:`. It is skipped when the text already says it, it is
   deduplicated, and it is not in `body`. The one-word name note works.
9. **Build.**
   - Through the guarded query function, the fixture gives the same term counts as the path build.
   - A truncated person statement is counted and warned about.
   - A failing build leaves the resolver at None and prints a status line; records stay on.
10. **Layering (A1).** The knowledge side imports no engine module. `--no-records` builds the knowledge resolver while
    loading no engine module and no duckdb.
11. **Settings.** `SFA_RESOLVER` off, on and default; `SFA_LEXICON`; the warning when there is no word list.

Existing tests to adjust:

- `tests/test_prompt_files.py:48`: the file count goes from 68 to 70 for the two new prompt files. They are new, so the
  notebook golden does not cover them.
- `tests/test_settings.py:90`: `ENV_DOC` lists the two new variables.
- Under A1: `KNOWLEDGE_SIDE` in `tests/test_layering.py`, and the imports in `tests/test_resolver.py`.
- Expectations that change with the switch on. These get new flag-on twins in the wiring file; the old tests keep the
  switch off:
  - `tests/test_engine_catalog.py:170` (`Interviewz`)
  - `tests/test_engine_values.py:71-80` (the Close clause)
  - `tests/test_tools_records.py:782` (E4h close spellings) and `:1590` (the hint line)
- Do not change tool descriptions or system prompts. `tests/test_tool_registry.py` (the golden tool lists and
  `docs/tools.md`) and `tests/test_prompt_files.py` (the golden prompts) pin them to the notebook folder.

Model-free checks (no model calls), in this order:

```bash
cd sf_assistant
SFA_RESOLVER=off PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests  # switch off: the old counts
SFA_RESOLVER=on PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests   # only the listed twins differ
OKEVAL_RECORDS_LIVE=1 SFA_RESOLVER=on PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
# since 2026-10-27 the switch is on by default: the same runs without SFA_RESOLVER equal the SFA_RESOLVER=on ones
```

- **Benchmark with the in-app vocabulary.** Run the resolver benchmark again on the vocabulary built through the
  guarded warehouse. The term counts by kind must equal the offline build, `query_errors` must be 0, and the targets
  in resolver.md must still hold.
- **Control sweep through the real hints, not only `spell_correct`.**
  - Run `object_hints` and the knowledge block on: the 16 person-set controls, `easy.txt`, `records.txt`, the
    40-question lookup set, the person dev and holdout sets, `medium`, `hard`, `twisted`, `sample`, and the five
    repo `scripts/*.txt` sets.
  - Target: 0 corrected spans on correct words. List the flagged ones.
  - Run it once with the full resolver and once with the knowledge resolver. With records off, the vocabulary has no
    person names, so names could be read as metadata. This sweep decides whether the records-off knowledge block
    should keep only `corrected` spans.
- **Hints dump.** Dump the hints for the owner's 15 examples to `.private/` (the dump prints names) and read them.
- **Cost.** Measure session open time and memory before and after (measured: [resolver.md](resolver.md#how-to-switch-it-off)
  "Cost"; the plan's "+1.9 s and +140 MB" was an estimate of a standalone build).
- **Privacy.** Run `.private/pii_check.py` over the tracked files.

### (8) Evening evaluation

The owner runs it, off hours.

| set | modes | compare with | target |
|---|---|---|---|
| the lookup set, 40 questions (OLD okeval_run/lookup_eval/questions.txt, expected.json) | records-only, merged | the 2026-10-15 wrap-up | 39-40/40 records-only, 40/40 merged |
| the golden 24 (`.private/questions/records_golden.json`, `score_records`) | records-only, merged | 24/24 | 24/24 |
| the person sets: dev 44, holdout, controls 16 (lookup_eval/people) | merged, records-only | the r2 grades (grades_r2.md, holdout_grades_r2_*.tsv) | no grade lost; controls unchanged |
| the knowledge sets easy and twisted (`.private/questions`) | `--no-records`, merged | the last knowledge run | no score lost |
| NEW: the typo set `.private/questions/typos.txt` (about 60 questions; see below) | merged | the clean twin's answer | see below |

The typo set:

- **Contents.** The owner's 15 examples as questions, plus typo twins of 20 lookup questions, 10 person dev questions
  and 15 easy questions. The twins are made by the benchmark's typo generator, and each one sits next to its clean
  twin. Added by the review (2026-10-21): a few Hinglish questions (the control set in the checks file: no
  correction, no "may mean" on the Hindi words), values with numbers (times such as `9:30 AM` for a stored
  `09:30 AM`, and a time no record has: a did-you-mean, never another slot's count), an unstored full name whose
  first name sounds like a stored one (a missing name, never the other person), and first-name-only answers about a
  misspelt name (the answer must state the reading first).
- **Targets.**
  - In at least 90% of the cases, the typo twin gives the clean twin's facts.
  - Every correction is stated.
  - No answer is about another person without saying so.
  - Control questions get no corrections.

Machine time, based on the 2026-10-16 runs: the lookup set records-only took about 21 min, the golden set 9 + 14 min,
and easy (14 questions) 3.5 min. All the sets above take about 3 hours. The typo set and its expected facts live in
`.private/`, because they contain real names.

## 3. Work estimate

| step | days |
|---|---|
| 0: Decision A1 move and split, with test updates | 0.5 |
| Build: switch, settings, word list in the image, builds in `open_records` and `pipeline.build`, status lines | 0.5 |
| (3) Catalog objects and fields, the `READ_AS` channel, evidence keys, knowledge-tool tiers | 1.0 |
| (4) ground_value | 0.5 |
| (5) People | 1.5 |
| (1) Question hints for both agents | 1.0 |
| (2) BM25 expansion | 0.5 |
| (6) Answer line and name note | 0.5 |
| (7) Wiring tests (spread over the steps), flag-on twins, model-free checks and sweeps | 1.0 |
| Docs: resolver.md "Wiring" becomes "done", README, and `docs/tools.md` only if it is regenerated | 0.25 |
| **Total** | **about 7.25 developer days** |
| Evening run | about 3 h of machine time, plus 0.5 day of grading and 0.5-1 day of fixes |

Order: step 0, then the build, then (3) and (4) (deterministic, and the most value per day), then (5), (1), (2) and
(6). Each step lands with its tests and keeps the switch-off suite and parity green.

## 4. Risks

- **Wrong person.**
  - Single names are corrected only for doubled letters or identical sounds. In the benchmark, 12 of 120
    leave-one-out names were corrected, all to an identical sound.
  - Every person correction is stated first, and filters never bind one.
- **Words taken for typos.** This happens when the image has no word list. Without one, the benchmark found 75 of
  6,000 common words corrected and 543 flagged.
- **Cost.** See the measured table in [resolver.md](resolver.md#how-to-switch-it-off) ("Cost"): the page process,
  each reopen after a read-copy swap (the first question after it waits for it) and the peak during a reopen.
- **Parity.** With the switch on, NEW no longer equals OLD. Parity runs with the switch off (since the default is
  on, the harness sets `SFA_RESOLVER=off` itself: `tests/parity/harness.no_model`); the evening run is the check for
  the switch on.
- **Tool-level binding.** The question-level scan only flags some typos (4-5 letter words). Such a typo can still be
  bound at tool level, where the kind is known; the code line then states the correction.
- **The page.** The LAN page gets none of this before stage 2's web switch.
