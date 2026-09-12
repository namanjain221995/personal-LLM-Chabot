# Salesforce LLM Evaluation — Project Handoff

## 1. Goal

The goal is to measure how accurately the local LLM system answers questions about the Techsara Salesforce org and to identify the first pipeline stage responsible for every incorrect answer.

The system must not be represented by one vague "LLM accuracy" number. It is a pipeline containing mode selection, routing, retrieval, entity resolution, planning, validation, SOQL generation, execution, synthesis, and provenance. We therefore need both:

1. **Strict end-to-end accuracy** — percentage of test questions for which every critical stage and the final answer are correct.
2. **Stage-level metrics** — accuracy of each stage so a failure can be assigned to the frontend, router, retriever, planner, validator, compiler, executor, synthesis model, or provenance layer.

The fundamental separation is:

```text
Golden evaluation dataset = expected behavior
Backend runtime trace      = actual behavior
Evaluator                  = deterministic comparison
Report                     = accuracy and failure analysis
```

The application being tested must never receive the expected answer. Only the offline evaluation runner may read both the golden case and the actual trace.

## 2. Metrics to Produce

### Core accuracy metrics

| Metric | Definition |
| --- | --- |
| Mode accuracy | Correct effective mode / total cases |
| Intent accuracy | Correct routed intent / total cases |
| Entity-resolution accuracy | Correct required objects and fields / total cases |
| Retrieval Recall@K | Required source documents found in the top K results |
| Retrieval Precision@K | Relevant retrieved documents / all retrieved documents |
| Context-contamination rate | Cases containing unrelated prior filters or stale context / total cases |
| Plan accuracy | Plans with correct object, fields, filters, operation, grouping, sorting and limit / total cases |
| Validator accuracy | Valid plans accepted and invalid plans rejected correctly / total validation cases |
| Query accuracy | Generated SOQL or metadata operation semantically matches the expected plan / total query cases |
| Execution success rate | Queries completing successfully / attempted queries |
| Result accuracy | Actual structured result equals the trusted oracle result / executed cases |
| Grounded-answer accuracy | Final claims supported by retrieved metadata or query results / total cases |
| Provenance accuracy | Correct source, environment and freshness label / total cases |
| End-to-end accuracy | Cases passing every critical check / total cases |
| Latency | P50, P95 and P99 overall and per stage |

### Strict end-to-end formula

```text
end_to_end_accuracy = fully_correct_cases / total_evaluated_cases
```

A case is fully correct only when all critical checks pass. A good narrative cannot compensate for a wrong object, query, result, or provenance label.

### Narrative metrics

Human or LLM-assisted grading is used only for:

- Relevance
- Clarity
- Completeness
- Conciseness
- Appropriate explanation of ambiguity

The narrative grader must not decide whether Salesforce API names, SOQL, counts, dates, or returned records are correct. Those checks must be deterministic.

## 3. Work Completed

### Step 1 — Representative question bank

Created `salesforce_question_bank_v1.yaml` containing 78 questions.

| Category | Cases |
| --- | ---: |
| Object metadata | 10 |
| Fields and record types | 12 |
| Relationships | 10 |
| Business rules | 10 |
| Automations and integrations | 12 |
| Live-record queries | 10 |
| Cross-object queries | 7 |
| Ambiguity and security | 7 |
| **Total** | **78** |


Each question has a stable test ID, category, difficulty, data requirement, answer-stability classification, and Salesforce tags.

Important source facts used by the question bank:

- The supplied preprod knowledge source documents 71 objects.
- Candidate records are Account Person Accounts; there is no documented `Candidate__c` object.
- Employees are primarily represented by `Recruiter__c`, labelled Employee, and are distinct from `User`.
- External interviews use `Interview__c`.
- Internal Techsara screening uses `Internal_Interview__c` in the supplied source.
- `Session__c` is the generic meeting/calendar wrapper.

### Step 2 — Golden evaluation dataset

Created `salesforce_eval_v1.yaml` containing all 78 executable case definitions.

Every case contains:

- Expected mode
- Expected intent
- Required and forbidden objects
- Required and forbidden fields
- Expected filters
- Expected source, environment and freshness
- Expected plan
- Fixed reference answer or live oracle-response template
- Provenance expectations
- Critical deterministic checks
- Narrative grading rubric

Answer distribution:

| Expected-answer type | Cases |
| --- | ---: |
| `fixed_reference` | 54 |
| `oracle_result` | 17 |
| `policy_reference` | 7 |

The dataset currently has this status:

```yaml
status: draft_pending_oracle_implementation_and_team_review
```

That status is intentional. Fixed reference answers need team review, and live cases need a trusted oracle compiler before they can execute.

### Accuracy corrections made during source verification

The supplied org source does **not** confirm active rules enforcing:

- Strict external interview-round sequencing
- A 30-minute maximum for Initial `Interview__c` rounds
- Duplicate candidate/company/position/round prevention
- Session end time being later than start time

Those cases now expect the LLM to disclose that the rule cannot be confirmed from the supplied metadata instead of inventing enforcement.

The source documents only one active `Session__c` validation rule: Host Feedback is required before marking a session Completed.

## 4. Files to Copy into the VS Code Repository

Recommended target structure:

```text
evaluation/
├── datasets/
│   ├── salesforce_question_bank_v1.yaml
│   └── salesforce_eval_v1.yaml
├── schemas/
│   ├── trace.schema.json
│   └── result.schema.json
├── evaluators/
│   ├── mode.py
│   ├── entities.py
│   ├── filters.py
│   ├── plan.py
│   ├── query.py
│   ├── result.py
│   ├── provenance.py
│   └── narrative.py
├── runners/
│   ├── evaluation_runner.py
│   └── oracle_runner.py
├── reports/
├── traces/
└── tests/
```

Do not blindly create duplicate application modules. Codex must inspect the existing repository and connect tracing to the actual API, router, retrieval, planner, validator, query compiler, executor, and response-generation files.

## 5. Next Work

### Step 2A — Team review before coding

Before treating the dataset as authoritative:

1. Review every fixed reference answer with Salesforce admins/domain owners.
2. Confirm the exact preprod API names and relationship names used by live cases.
3. Confirm whether each question should use live Salesforce, the org knowledge source, or a DuckDB snapshot.
4. Change accepted cases from draft to approved, or add a per-case `review_status`.
5. Keep unverified cases disabled until their ground truth is approved.

### Step 3 — Backend tracing layer

The backend must produce one structured trace for every query. The authoritative trace belongs in backend storage. Terminal logs may display it, and the frontend may expose a simplified developer view, but neither should be the only stored source.

Each request needs:

```json
{
  "trace_id": "tr_...",
  "request_id": "req_...",
  "session_id": "session_...",
  "test_case_id": "SF-DATA-001"
}
```

`test_case_id` is supplied only by the evaluation runner. Normal production requests may omit it.

The trace must record these stages in order:

```text
ingestion
routing
entity_resolution
retrieval
planning
validation
compilation
execution
synthesis
response
```

Minimum stage information:

| Stage | Required trace data |
| --- | --- |
| Ingestion | Question, requested mode, effective mode, conversation-state ID |
| Routing | Selected intent, confidence, router version |
| Entity resolution | Objects, fields, aliases, ambiguities |
| Retrieval | Query, index version, top K, document IDs, scores, filters |
| Planning | Operation, root object, fields, filters, grouping, ordering, limit |
| Validation | Valid/invalid, errors, warnings, schema version |
| Compilation | Query language and generated SOQL/metadata operation |
| Execution | Actual source, success, row count, normalized result, duration |
| Synthesis | Claims, evidence paths, source label, synthesis-model version |
| Response | Final text, structured answer, total duration |

Every trace must also include application, prompt, model, metadata-index, and pipeline versions so evaluation runs are reproducible.

### Trace-storage recommendation

- Emit structured JSON logs from the backend.
- Store one complete trace as JSON or append traces as JSONL during local development.
- Load evaluation results and selected normalized trace fields into DuckDB for historical analysis.
- Redact passwords, access tokens, session tokens, Salesforce credentials, private candidate data, and unnecessary record payloads.
- Store hashes or sampled/redacted payloads when full records are unnecessary.

### Step 4 — Deterministic evaluators

Implement one comparator per stage.

#### Exact comparisons

- Mode
- Intent
- Source label
- Environment
- Freshness classification
- Scalar counts and values

#### Set comparisons

- Required objects must be a subset of actual objects.
- Required fields must be a subset of actual fields.
- Forbidden objects and fields must not appear.

#### Semantic structural comparisons

- Normalize filters into field/operator/typed-value structures.
- Normalize dates, time zones, booleans, nulls and relative date literals.
- Compare plans as structures, not prose.
- Parse SOQL and compare selected fields, root object, relationships, predicates, aggregation, grouping, ordering and limit.
- Do not use raw SOQL string equality because equivalent queries can differ in formatting and field order.

#### Result comparisons

- Counts: exact integer equality.
- Record sets: compare by stable keys such as Salesforce ID.
- Unordered lists: normalize order before comparison.
- Decimal values: use an explicitly configured tolerance.
- Date/time values: normalize time zone before comparison.

The evaluator must record the **first incorrect stage** and all secondary failures.

### Step 4A — Trusted live oracle

The 17 `oracle_result` cases contain semantic oracle plans with:

```yaml
implementation_status: pending_oracle_compiler
```

Implement a trusted, deterministic compiler that converts only approved oracle plans into Salesforce queries. The candidate LLM must not generate or modify oracle queries.

Recommended safety rules:

- Allowlist Salesforce objects, fields, operators and aggregations.
- Use typed values and safe escaping.
- Enforce read-only operations.
- Apply maximum row limits.
- Use the configured preprod connection.
- Record oracle query, execution time, source and result hash separately.
- Never send oracle results to the application before the application produces its answer.

For deterministic CI, also support fixed DuckDB snapshots. Use live Salesforce oracle runs for nightly or integration evaluation.

### Step 5 — Narrative grader

After deterministic checks complete, optionally grade narrative quality with a human or a separate grader model.

Recommended rubric:

```yaml
relevance: 0.30
clarity: 0.25
completeness: 0.30
conciseness: 0.15
```

The grader receives the question, reference answer or required claims, and actual narrative. It must not override deterministic failures.

### Step 6 — Reports

Generate three report formats per run:

1. `run_<timestamp>.json` — machine-readable summary and per-case checks.
2. `failures_<timestamp>.csv` — spreadsheet-friendly failure list.
3. `evaluation_report_<timestamp>.html` or `.md` — human-readable dashboard.

The summary must include:

- Dataset and pipeline versions
- Total, passed and failed cases
- Strict end-to-end accuracy
- Every stage-level accuracy metric
- Accuracy by category and object
- First-incorrect-stage distribution
- P50/P95/P99 latency
- Comparison with the previous run
- List of cases that regressed or improved

### Step 7 — Production regression loop

For every confirmed production failure:

1. Find its trace by `trace_id`.
2. Remove candidate names, emails, Salesforce IDs, credentials and sensitive values.
3. Reproduce the behavior in preprod or a controlled snapshot.
4. Establish and review the correct expected behavior.
5. Add a permanent `SF-REGRESSION-xxx` case.
6. Verify the new case fails before the fix.
7. Implement the fix.
8. Verify the new case and the full suite pass afterward.

## 6. Recommended Implementation Order in VS Code

Implement in small, testable increments:

1. Copy the two dataset files into `evaluation/datasets/`.
2. Inspect the repository and produce an entrypoint/stage/file map.
3. Add trace IDs at API query ingestion.
4. Implement a shared `TraceContext` and structured stage recorder.
5. Instrument mode ingestion and routing first.
6. Instrument entity resolution and retrieval.
7. Instrument planning, validation and query compilation.
8. Instrument execution, result normalization, synthesis and provenance.
9. Add `trace.schema.json` and trace-schema tests.
10. Build the evaluation dataset loader.
11. Build deterministic mode, intent, entity, filter, plan and provenance checks.
12. Build the trusted oracle compiler and result comparator.
13. Add SOQL semantic parsing/normalization.
14. Add the optional narrative grader.
15. Produce JSON, CSV and HTML/Markdown reports.
16. Run a small smoke set of 5–10 cases before running all 78.

## 7. First Coding Milestone

The first milestone should instrument tracing without changing answer behavior.

Acceptance criteria:

- Every chat request receives one unique `trace_id`.
- The selected frontend mode and effective backend mode are both recorded.
- Each pipeline stage records status, start time, end time and duration.
- Stage inputs/outputs needed for evaluation are structured, not only free-text logs.
- The final API response includes `trace_id` in its metadata.
- Sensitive values are redacted.
- A trace can be retrieved by `trace_id`.
- Existing chat behavior and tests remain unchanged.
- At least one unit test verifies a complete successful trace.
- At least one unit test verifies a failed stage trace.

## 8. Paste-Ready Prompt for Codex in VS Code

```text
We are implementing a stage-wise evaluation system for our local-LLM Salesforce question-answering pipeline.

The repository now contains:
- evaluation/datasets/salesforce_question_bank_v1.yaml
- evaluation/datasets/salesforce_eval_v1.yaml

The golden dataset contains 78 cases. Each case specifies expected mode, intent, objects, fields, filters, source, plan, answer/reference or oracle, provenance, and grading checks.

First inspect the repository and identify the actual files/functions for:
1. Chat/API query ingestion
2. Selected-mode handling
3. Intent routing
4. Entity resolution
5. Metadata/context retrieval
6. Query planning
7. Plan validation
8. SQL/SOQL compilation
9. Query execution
10. Response synthesis and provenance

Before editing, give me a file-level change map showing the existing filename, relevant function/class, pipeline stage, and proposed change. Do not guess filenames and do not create duplicate pipeline components when equivalent code already exists.

Then implement only the first tracing milestone:
- Generate trace_id and request_id at backend ingestion.
- Accept optional test_case_id for evaluation runs.
- Record requested_mode and effective_mode separately.
- Add a shared structured TraceContext/stage recorder.
- Record stage name, status, started_at, completed_at, duration_ms, structured input summary, structured output summary, error code, and component version.
- Instrument every existing pipeline stage without changing business behavior.
- Add trace_id to response metadata.
- Persist structured traces in a configurable backend; JSONL is acceptable for local development, but design the interface so another store can replace it.
- Redact passwords, tokens, credentials, candidate PII, and unnecessary Salesforce record payloads.
- Add a JSON Schema for traces.
- Add tests for a successful trace, a failed-stage trace, mode preservation, trace retrieval, and sensitive-data redaction.

The authoritative trace belongs in the backend. Terminal logs may display it and the frontend may show a debug view, but neither is the authoritative store.

Important: the application under evaluation must never receive expected answers from salesforce_eval_v1.yaml. Only the offline evaluator may join expected cases with actual traces using test_case_id.

After implementation, run the relevant tests and provide:
- Files changed
- Trace flow from ingestion to response
- Example sanitized trace
- Test results
- Any pipeline stage that could not be instrumented
```

## 9. Definition of Project Completion

The evaluation project is complete when:

- All approved cases can be executed automatically.
- Every case has a complete backend trace.
- All deterministic checks run without manual inspection.
- Live cases use a trusted independent oracle.
- Reports show stage-level and strict end-to-end accuracy.
- The first incorrect stage is assigned for every failure.
- Model, prompt, metadata-index and pipeline versions are recorded.
- Production failures can be converted into sanitized regression tests.
- Accuracy changes can be compared across releases.
