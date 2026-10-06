# Main model in every pipeline stage: audit and plan

Spec: `main_llm_integration_in_each_stage.txt` ("Refactor Salesforce AI Pipeline So the
Main Qwen3.6-35B Model Is the Primary Semantic Engine in Every Pipeline Stage").
Scope: the semantic-IR path (`src/pipeline/ir_path.py`), the default runtime behind
`SFK_PIPELINE_ENABLED`. The step-7 stage path (`SFK_IR_PATH=false`) is a comparison
switch and is not changed.

Audit date: 2026-10-03. Baseline: 500-question benchmark
(`scripts/questions_50_objects.txt`) answered 470/500, linking "fast path 359, main
model 106", average 3778 ms (`ask_results/my_main_run1.txt`, 2026-10-01). Unit suites:
244 passed.

## A. Current architecture

```
question
 -> IntentCompiler.compile          35B, JSON IR in business words (+1 retry)
      clean()                       deterministic guards, incl. family overrides
 -> conversation.inherit            fixed merge rules for follow-ups
 -> capabilities.plan               family -> capabilities/sources table (code)
 -> IRGrounder.ground
      _normalise                    schema-settled rewrites of the IR (code)
      retrieval                     objects top 5, fields top 6 on top 3 objects
      _fast_path                    retrieval decides objects + fields when "decisive"
      _ask                          35B only when the fast path returns None
      fill-ins                      skipped entities/slots filled from retrieval
      paths                         graph shortest path, _worded_option stem match
      assemble                      IR + fields -> GroundedQuery (code)
 -> RecordEngine / SchemaEngine / OperationalEngine / HistoryEngine / SearchEngine
 -> to_interpreted                  typed facts (code)
 -> AnswerService.answer_interpreted 35B writes the answer (+1 regeneration),
                                    deterministic grounding validator, fallback
```

Entry point: `orchestrator/app/engines/sfk_bridge.py::_ir_components` builds
`IRComponents`; `SalesforcePipeline.run` hands every question to `IRPath.run`.

## B. Where the 35B is called today

| Stage | Call site | When |
|---|---|---|
| Intent | `src/pipeline/compiler.py` `IntentCompiler.compile` | every question |
| Linking | `src/salesforce/schema_linking/ir_grounder.py` `IRGrounder._ask` | only when `_fast_path` returns None (106 of 465 grounded questions) |
| Answer | `src/answer/service.py` `AnswerService._attempt` | every answered question |

## C. Where it is skipped

| Stage | What decides instead |
|---|---|
| Routing | `capabilities.plan`: a fixed family -> capability table |
| Discovery (object choice) | `_fast_path` (`_decisive` on retrieval scores); after `_ask`, `_decisive` fills any entity the model skipped |
| Linking (field choice) | `_fast_path`; `_unique_picklist`; single-date auto pick; sibling settle; post-`_ask` retrieval fill-ins; moved-object resolution |
| Relationship path | `RelationshipGraph.paths(...)[0]` (top score); `_worded_option` when the model skips |
| Logical plan | grounder step 8 assembles the plan from the IR by code |
| Result interpretation | `to_interpreted` (code); no model stage |
| Schema questions | `SchemaEngine`: retrieval top-1 object and field |
| Operational questions | `OperationalEngine`: retrieval top-1 object |
| History questions | `HistoryEngine`: retrieval top-1 object and field; record name and field chosen from the IR by code |
| Follow-ups | `conversation.inherit` fixed merge rules |

## D. Fast-path model bypasses

1. `IRGrounder._fast_path` (`ir_grounder.py:597`): whole grounding without the model.
2. `IRGrounder._ask` fill-ins (`ir_grounder.py:~930`): entities and slots the model left
   out are filled from `_decisive` retrieval.
3. Moved-object slot resolution (`ir_grounder.py:~318`): `_decisive` or single candidate.
4. Single date field auto pick (`ir_grounder.py:~326`).
5. `_worded_option` (`ir_grounder.py:~1000`): word-stem match picks a lookup.
6. `field_candidates` platform shortcuts: name/created/modified concepts return a single
   candidate, so even the model sees no choice.
7. `SchemaEngine._object/_field/_exact_field`, `OperationalEngine`, `HistoryEngine`:
   retrieval top-1.

## E. Deterministic components making semantic decisions

- `compiler.clean`: regex forces family `metadata`; family `metadata` with measures
  becomes `schema`. Other guards in `clean` remove or move parts and add no meaning
  (scope words, operation words, time-unit filters, record-number literals,
  duplicate/grouping count), and stay as structural normalisation.
- `capabilities.plan`: routing.
- `IRGrounder._normalise`: rewrites the IR before grounding (qualifier -> object,
  count of an object word -> new entity, entity drop, attribute re-homing). These
  shape which concepts are bound; with a model-created plan they become evidence
  shaping, see K.
- Base (fact entity) choice: "the measured entity, else the primary".
- `conversation.inherit`.
- `IRPath._is_name_lookup` widening to cross-object search.

## F. Proposed refactor

Principle: the model makes every semantic decision; code gathers evidence, checks
the model's choice against the runtime schema, and executes.

1. **One policy** (`src/model_roles.py`): `REQUIRED_MODEL_STAGES` = intent, routing,
   discovery, linking, planning, interpretation, answer; each
   `MAIN_MODEL_REQUIRED_FOR_<STAGE>` defaults to true. Which stages apply depends on
   the route (planning applies to record queries only, for example).
2. **One stage caller** (`src/pipeline/stage_model.py`): every model stage posts
   through it and gets a `ModelCall` record: stage, role, model, model_called,
   duration, prompt/completion tokens, candidate count, selection, output validity,
   retry count. One structured retry on malformed output. Failure returns
   `MAIN_MODEL_UNAVAILABLE`, never a deterministic substitute.
3. **Intent + routing (one call, traced as two stages).** The compiler also returns
   `sources` (RECORD_DATA, RUNTIME_SCHEMA, ...). `capabilities.plan` maps the model's
   sources to handlers and verifies they exist; it no longer chooses. The regex
   family override becomes a lexical hint in the prompt, and the metadata/schema flip
   becomes a shape check that triggers the retry. Follow-ups: the compiler returns
   the complete meaning, inheriting from the previous IR itself; `inherit` is removed.
4. **Discovery (own call, always).** High-recall object candidates per entity (8,
   plus candidates for qualifier+concept phrases and platform objects) with IDs
   `O1..On`. The model picks each entity's object and the fact entity. A model can
   ask for a second retrieval pass (`search_terms`) when nothing fits; one extra call.
5. **Linking (own call, always).** Fields are retrieved only on the chosen objects,
   with more candidates (12 per slot, type-compatible, platform fields included as
   evidence, not as the only option), IDs `F1..Fn`; relationship paths enumerated by
   the graph, IDs `R1..Rn`. The model picks a field for every slot, a picklist value
   or boolean when a value is involved, and a path for every non-base entity. Missing
   slots trigger one retry, then a failure. No fill-ins, no shortest-path default,
   no `_worded_option`.
6. **Planning (own call, record questions).** The model receives the question, the
   intent, the verified bindings (`B1..Bn`), the paths and the data types, and writes
   the logical plan: fact entity, filters with owner bindings, measures, dimensions,
   ordering, limit, distinct, existence, derived parts, comparison segments,
   duplicate threshold, and temporal semantics as structure
   (`{"kind":"relative","unit":"month","offset":0}`). Code compiles the plan into the
   existing `GroundedQuery` (verifying aggregate/type compatibility, picklist
   spelling, existence paths), then `AnalyticPlanner` builds SQL as today. Temporal
   structure is turned into ranges by `temporal.py`.
7. **Schema, operational and history questions** go through discovery (and linking
   where a field is involved) instead of retrieval top-1; the engines receive the
   model's verified object/field.
8. **Interpretation + answer (one call, traced as two stages).** The answer JSON gains
   an `interpretation` block (`answer_type`, `primary_facts`, `important_context`
   naming fact keys from the context); code checks the keys exist. The deterministic
   validator and regeneration stay; there is no model validator stage. A model
   outage is `MAIN_MODEL_UNAVAILABLE`; the deterministic fallback remains only for
   an answer the validator rejected twice (spec §33).
9. **Tracing.** A `MODEL_STAGE` event per stage; `PipelineResult.model_stages`;
   `scripts/ask.py` reports "intent model N/N, routing N/N, ..." in place of
   "fast path / main model", plus evidence-retrieval counts.

## G. Files to modify

`src/model_roles.py`, `src/pipeline/compiler.py`, `src/pipeline/capabilities.py`,
`src/pipeline/ir_path.py`, `src/pipeline/ir.py`, `src/pipeline/models.py`,
`src/pipeline/tracing.py`, `src/pipeline/conversation.py`,
`src/pipeline/engines/{schema,operational,history}.py`,
`src/salesforce/schema_linking/ir_grounder.py`,
`src/salesforce/schema_linking/temporal.py`, `src/answer/{prompts,generator,models,service}.py`,
`orchestrator/app/engines/sfk_bridge.py`, `scripts/ask.py`,
`salesforce_knowledge/config/schema_config.yaml`, tests under `tests/pipeline`.

## H. New modules

- `src/pipeline/stage_model.py`: the shared stage caller and `ModelCall` record.
- `src/pipeline/planner.py`: the planning stage and the plan -> `GroundedQuery` compiler.

## I. Model-call count

| Question kind | Today | After |
|---|---|---|
| Record (count, list, group, rank, percentage, ...) | 2 (fast path) or 3 | 5: compile, discovery, linking, planning, answer |
| Schema | 2 | 3 to 4: compile, discovery, linking when a field is asked, answer |
| Operational | 2 | 3: compile, discovery, answer |
| History | 2 | 4: compile, discovery, linking, answer |
| Unsupported / search | 2 | 2 |

Retries add at most one call per stage.

## J. Latency

Measured per call today: compile about 1.0 to 1.5 s, grounding call about 1 s, answer
about 1 to 1.5 s. Expected record question: about 6 to 8 s (today 3.8 s average).
Mitigations used: pooled HTTP client, short structured outputs, candidate IDs instead
of repeated schema text, fields only for chosen objects, intent+routing and
interpretation+answer combined where the spec allows (§42).

## K. Generalisation risks

- More model decisions means more places where a model habit can go wrong; each stage
  output is checked against the runtime schema and gets one retry.
- `_normalise` rewrites still shape which concepts become bindings. The planner cannot
  use a concept nobody bound; it can ask for it through the retry message. Listed as a
  follow-up.
- Follow-ups now depend on the compiler restating inherited meaning.

## L. Test plan

- Runtime-trace test: a scripted model behind every stage; asserts `model_called` for
  every applicable stage of record, schema, operational and history questions.
- Outage test: each stage's model failure gives `MAIN_MODEL_UNAVAILABLE` and no
  deterministic answer.
- Grounding tests on the real runtime schema with a scripted model: candidate IDs,
  rejection of IDs not offered, no fast path on an exact match, recall (Name, CreatedDate
  and other dates offered together).
- Held-out tests: planner compile on objects and fields not used while building
  (group by, avg, boolean, date filters on CreatedDate/custom dates, multi-hop
  count distinct).
- Live: `scripts/regression_20.txt`, then the 500-question benchmark, with per-stage
  participation counts and latency.
