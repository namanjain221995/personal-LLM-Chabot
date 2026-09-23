# Salesforce Metadata GraphRAG - Development Handoff

This document captures the current project state so development can continue in
another coding assistant or session.

## 1. Project goal

Build a local, read-only GraphRAG over a Salesforce production-org metadata
mirror. The project should eventually answer natural-language Salesforce
questions using:

1. A small model for routing, entity recognition, and query planning.
2. A generated metadata catalog for API names, labels, types, aliases, and
   relationships.
3. A deterministic metadata graph for org-understanding questions.
4. Text RAG over metadata/source files for explanations and evidence.
5. DuckDB for Salesforce record data and record-related questions.
6. A grounded answer generator that cites the retrieved evidence.

The current implementation is the deterministic metadata graph foundation.
The small model, metadata catalog, text RAG, record ingestion, DuckDB, and
natural-language `ask` endpoint are not implemented yet.

## 2. Repository and environment

Project root:

```text
C:\Users\jayes\Downloads\Salesforce-Org-Data-main\Salesforce-Org-Data-main
```

Important environment facts:

- Windows PowerShell.
- This checkout is not a Git repository (`git` commands cannot provide
  commit history here).
- Python is available.
- `pip` is unavailable in the current environment.
- `pytest` is unavailable.
- Tests use Python standard-library `unittest`.
- Set the source path before running package commands:

```powershell
$env:PYTHONPATH="src"
```

The Salesforce source is a read-only mirror. Do not modify:

```text
Prod Org Data\force-app\main\default\
```

## 3. Source and generated artifacts

### Salesforce metadata source

```text
Prod Org Data\force-app\main\default\
```

This contains Salesforce DX metadata, including objects, fields, classes,
triggers, flows, layouts, flexipages, permissions, reports, dashboards,
global value sets, and many other metadata directories.

The top-level source has many Salesforce metadata types. Not every type has a
specialized parser; unsupported types may still appear as file nodes and
generic XML references.

### Persisted graph

```text
graph.jsonl
```

Current generated graph:

```text
31,602 nodes
245,336 edges
```

File size is approximately 86 MB. It is JSON Lines:

- A node record has `"type": "node"` and contains `id`, `kind`, `name`,
  `path`, and `properties`.
- An edge record has `"type": "edge"` and contains `source`, `target`, `kind`,
  and source `evidence`.

This is a generated artifact. Reindexing overwrites it; indexing does not
append duplicate records.

### Obsidian export

```text
salesforce-vault\
```

This contains one Markdown note per graph node and Obsidian wikilinks for graph
edges. Open this folder as an Obsidian vault. It is a visualization/export
layer, not the source of truth.

## 4. Core implementation files

### `pyproject.toml`

Defines the package:

- Package name: `local-graphrag`
- Python requirement: `>=3.10`
- No runtime dependencies
- Console entry point: `graphrag = graphrag.cli:main`

### `src/graphrag/models.py`

Defines immutable dataclasses:

- `Evidence`
  - `source_path`
  - `line`
  - `snippet`
- `Node`
  - `id`
  - `kind`
  - `name`
  - optional `path`
  - arbitrary `properties`
- `Edge`
  - `source`
  - `target`
  - `kind`
  - tuple of `Evidence`
- `Graph`
  - tuple of nodes
  - tuple of edges

All models have `to_dict()` methods for JSON serialization.

### `src/graphrag/scanner.py`

Scans the Salesforce DX source tree and creates `SourceFile` records. It:

- Recursively finds source files.
- Classifies DX paths and metadata components.
- Reads text content.
- Raises explicit `ScanError` for invalid/missing source.

Classification includes metadata such as Apex classes/triggers, objects,
fields, flows, permissions, profiles, roles, queues, FlexiPages, layouts,
reports, dashboards, folders, global value sets, and generic Salesforce
metadata.

### `src/graphrag/parsers.py`

Contains generic parsers:

- Apex reference extraction.
- Generic XML reference extraction.
- Evidence line/snippet generation.

Specialized field, flow, and metadata relationship extraction is primarily in
`graph.py`.

### `src/graphrag/graph.py`

Main graph builder and graph persistence module.

Public functions:

- `build_graph(source)`
- `write_graph(graph, output)`
- `read_graph(source)`

Important behavior:

- Builds file nodes for scanned source files.
- Builds normalized object and field nodes.
- Retains source paths and edge evidence.
- Uses structural parsers for field metadata and Flow metadata.
- Avoids false `object:<FieldName>` nodes from generic XML extraction.
- Reads local and global picklist values.
- Emits inverse relationship edges.
- Writes JSON or JSONL depending on output extension.

### `src/graphrag/queries.py`

Current deterministic query functions include:

- `find_nodes`
- `dependencies`
- `traverse_dependencies`
- `fields_for_object`
- `required_fields`
- `picklist_values`

`picklist_values(graph, field_name)` expects an exact field API name such as
`Account.Client_Status__c` and returns sorted `picklist_value` nodes.

### `src/graphrag/cli.py`

Current CLI commands:

- `index`
- `query`
- `fields`
- `required-fields`
- `picklist-values`
- `export-obsidian`

### `src/graphrag/obsidian.py`

Exports a graph to Markdown notes:

- One note per node.
- Outgoing relationship section.
- Incoming relationship section.
- Node properties and source path.
- Obsidian `[[wikilinks]]`.
- Windows-safe filename sanitization.
- Long/unsafe names receive a short hash suffix.
- Duplicate display names receive numeric suffixes.

### `tests/test_graphrag.py`

Dependency-free test suite. Current result:

```text
Ran 14 tests
OK
```

Tests cover:

- DX path classification.
- Apex/XML evidence.
- JSONL write/read.
- Missing source errors.
- Node lookup and reverse traversal.
- Object/field relationships.
- Lookup/master-detail relationships.
- Flow relationships.
- Structural metadata ownership.
- Grouped metadata relationships.
- Picklist options.
- Obsidian note and wikilink export.

### `README.md`

Documents the Salesforce mirror and GraphRAG MVP, supported relationships,
commands, picklist modeling, and Obsidian export.

## 5. Current graph model

### Stable node ID patterns

Examples:

```text
file:objects/Account/fields/Client_Status__c.field-meta.xml
object:Account
field:Account.Client_Status__c
picklist_value:Account.Client_Status__c.Active
flow:Some_Flow
validation_rule:Account.Some_Rule
record_type:Account.B2B_Client
layout:Account.Some_Layout
profile:Admin
permission_set:Some_Permission_Set
apex_class:SomeClass
```

Generic/reference node IDs can also exist, for example:

```text
xml_reference:<name>
```

Do not assume every generic XML reference is a fully modeled Salesforce
component.

### Field properties

Field nodes can contain:

```json
{
  "object": "Account",
  "field": "Client_Status__c",
  "label": "Client Status",
  "type": "Picklist",
  "required": false
}
```

Relationship fields can additionally contain:

- `referenceTo`
- `relationshipName`

### Picklist option properties

Picklist values are modeled as nodes:

```text
picklist_value:Account.Client_Status__c.Active
```

The field has:

```text
field:Account.Client_Status__c
  --has_value-->
picklist_value:Account.Client_Status__c.Active
```

Option properties may include:

- `field`
- `fullName`
- `label`
- `default`
- `isActive`

Local values come from `<valueSetDefinition>`. Global values are resolved
through `<valueSetName>` and `globalValueSets/*.globalValueSet-meta.xml`.

## 6. Supported relationships

The following normalized edge kinds are currently present where source
metadata provides enough evidence.

### Objects, fields, and relationships

```text
object --has_field--> field
field --lookup_to--> object
field --master_detail_to--> object
object --parent_of--> object
object --child_of--> object
```

### Object structural metadata

```text
object --has_record_type--> record_type
object --has_validation_rule--> validation_rule
object --has_duplicate_rule--> duplicate_rule
object --has_compact_layout--> compact_layout
object --has_search_layout--> search_layout
object --has_business_process--> business_process
object --has_sharing_rule--> sharing_rule
object --has_index--> index
```

Some requested metadata types have zero results in this org snapshot. A zero
count means the metadata was absent or not deterministically extractable; the
graph does not invent nodes.

### Flows

```text
flow --targets--> object
flow --triggers_on--> object
flow --triggers_on_event--> event
flow --reads_field--> field
flow --writes_field--> field
flow --creates--> object
flow --updates--> object
flow --deletes--> object
flow --calls--> apex_class
flow --calls_subflow--> subflow
flow --uses--> email_alert / approval_process / custom_notification
flow --has_element--> flow_element
flow --has_decision--> decision
flow --has_assignment--> assignment
flow --has_screen--> screen
flow --has_path--> path
```

Flow field references such as `$Record.Some_Field__c` are normalized to known
`field:Object.Field` nodes when possible.

### Validation

```text
validation_rule --applies_to--> object
validation_rule --references_field--> field
validation_rule --blocks_operation--> operation
validation_rule --uses_function--> function
validation_rule --has_error_message--> custom_label
```

### Security and sharing

The implementation uses consolidated permission edge kinds:

```text
profile --object_permission--> object
profile --field_permission--> field
profile --system_permission--> permission
permission_set --object_permission--> object
permission_set --field_permission--> field
permission_set --system_permission--> permission
permission_set_group --contains--> permission_set
permission_set --assigned_to--> user
profile --assigned_to--> user
sharing_rule --applies_to--> object
sharing_rule --shares_with--> role/group
sharing_rule --filters_by_field--> field
role --reports_to--> role
queue --supports--> object
```

Permission edge properties preserve access flags such as readable, editable,
createable, deleteable, viewAll, and modifyAll when available.

Some concepts such as profile-to-user assignment cannot be fully known from
the metadata snapshot and are intentionally not fabricated.

### UI, layouts, record types, and reports

```text
lightning_page --contains--> component
lightning_page --targets--> object
lightning_page --uses--> field
lightning_page --assigned_to--> app / record_type / profile

layout --for_object--> object
layout --displays_field--> field
layout --displays_button--> custom_button
layout --displays_section--> layout_section
layout --assigned_to--> profile / record_type

record_type --for_object--> object
record_type --uses_picklist_value--> field
record_type --assigned_to--> profile
record_type --uses_layout--> layout

report --reports_on--> object
report --selects_field--> field
report --filters--> field
report --groups_by_field--> field
report --uses_filter--> filter
report --stored_in--> folder
dashboard --contains--> report
dashboard --stored_in--> folder
```

Some conceptual relationship names are normalized to existing edge names such
as `filters`, `for_object`, `contains`, and `stored_in`.

## 7. Persisted graph counts

The latest persisted `graph.jsonl` was summarized as:

```text
nodes: 31602
edges: 245336
```

Notable node counts:

```text
objects: 722
fields: 5107
picklist values: 2827
flows: 372
validation rules: 59
profiles: 70
permission sets: 118
layouts: 370
lightning pages: 174
reports: 764
dashboards: 50
record types: 25
apex classes: 384
```

Notable edge counts:

```text
has_field: 4592
has_value: 2827
lookup_to: 209
master_detail_to: 27
parent_of: 186
child_of: 186
has_validation_rule: 59
reads_field: 387
writes_field: 910
calls: 52
updates: 66
creates: 74
field_permission: 95393
object_permission: 3888
system_permission: 1011
displays_field: 1980
references: 120352
```

Counts can change after parser changes or source refreshes.

## 8. How to build and query

### Run tests

```powershell
cd "C:\Users\jayes\Downloads\Salesforce-Org-Data-main\Salesforce-Org-Data-main"
$env:PYTHONPATH="src"
python -m unittest discover -s tests -v
```

Expected current result:

```text
Ran 14 tests
OK
```

### Rebuild the graph

Run this after source metadata changes or graph-parser changes:

```powershell
$env:PYTHONPATH="src"
python -m graphrag.cli index `
  --source "Prod Org Data\force-app\main\default" `
  --output "graph.jsonl"
```

### Exact node lookup

```powershell
$env:PYTHONPATH="src"
python -m graphrag.cli query `
  --graph "graph.jsonl" `
  --match "object:Account" `
  --mode find
```

### Reverse dependencies

```powershell
python -m graphrag.cli query `
  --graph "graph.jsonl" `
  --match "Interview__c" `
  --mode dependencies
```

### Bounded reverse traversal

```powershell
python -m graphrag.cli query `
  --graph "graph.jsonl" `
  --match "Interview__c" `
  --mode traverse `
  --depth 2
```

### List fields

```powershell
python -m graphrag.cli fields `
  --graph "graph.jsonl" `
  --object "Account"
```

### List required fields

```powershell
python -m graphrag.cli required-fields `
  --graph "graph.jsonl" `
  --object "Interview__c"
```

The `--latest-commit` flag is intentionally rejected because this checkout is
not a Git repository and the graph artifact has no commit metadata.

### List picklist values

```powershell
python -m graphrag.cli picklist-values `
  --graph "graph.jsonl" `
  --field "Account.Candidate_Status__c"
```

Global value set example:

```powershell
python -m graphrag.cli picklist-values `
  --graph "graph.jsonl" `
  --field "Account.Work_Authorization__c"
```

### Export to Obsidian

```powershell
python -m graphrag.cli export-obsidian `
  --graph "graph.jsonl" `
  --output "salesforce-vault"
```

Open `salesforce-vault` as an Obsidian vault. The complete graph is large; use
Obsidian local graph around a specific object or field rather than opening all
nodes at once.

## 9. Important historical fixes

These issues were encountered and fixed:

1. Stale `graph.jsonl` after parser changes:
   - Queries read the persisted artifact and do not rescan source.
   - Reindex after parser or source changes.
2. `Candidate__c` was incorrectly emitted as an object:
   - It is actually a field on `Interview__c` referencing `Account`.
   - Generic XML extraction was restricted for structural field metadata.
3. Flow references required special handling:
   - Flow elements and `$Record.Field` references are normalized.
4. Picklist values were initially absent:
   - Local value sets and global value sets are now modeled.
5. Obsidian export initially failed on unsafe multiline XML-reference names:
   - Filename sanitization now handles control characters, Windows-invalid
     characters, long names, and duplicate display names.

## 10. Current limitations

### No record data

The graph contains Salesforce metadata only. It cannot answer questions such
as:

- How many Account records have a particular status?
- Which candidates were updated yesterday?
- What is the current value of a record field?

Those require Salesforce record extraction and DuckDB.

### No natural-language question layer

There is no `ask` command, LLM integration, entity resolver, intent router,
typed query planner, or answer synthesis layer yet.

### No metadata catalog resource

The graph has node properties, but there is no separate optimized catalog with
aliases, labels, business terms, or resolution rules.

### No text RAG

Source evidence is stored on graph edges, but there is no chunking, embedding,
vector index, or semantic source retrieval yet.

### No Git snapshot metadata

The graph does not record commit, snapshot, or source version information.

### Generic references

The parser still creates generic XML-reference nodes for many metadata types.
Specialized parsing should be preferred for any new relationship to avoid
false positives.

### Obsidian scale

The generated vault has tens of thousands of notes. A complete Obsidian graph
may be crowded or slow. Local graph views are the practical usage pattern.

## 11. Planned target architecture

The desired end-to-end system is:

```text
User question
  ↓
Question router
  ├── metadata/org-understanding
  ├── record/data
  └── hybrid
  ↓
Small model extracts entities, intent, filters, and time scope
  ↓
Entity resolver
  ├── metadata catalog
  ├── API names and labels
  ├── aliases
  └── business rules
  ↓
Typed query planner
  ├── graph plan
  ├── RAG plan
  └── DuckDB SQL plan
  ↓
Plan validation and authorization
  ↓
Execute graph, text RAG, and/or parameterized DuckDB query
  ↓
Join results for hybrid questions
  ↓
Grounded answer with evidence, confidence, and freshness
```

### Example routes

| Question | Route |
|---|---|
| Which fields are on Account? | metadata graph |
| What values can Candidate_Status__c have? | metadata graph |
| Which flows write Account status? | graph plus source RAG |
| Which Accounts have Active status? | DuckDB record query |
| Why did this Account status change? | hybrid |
| Which permission set grants field edit access? | metadata graph |

### Typed intermediate plan

Do not let a model execute arbitrary Cypher, Python, or SQL. Have the model
produce validated JSON plans.

Metadata plan example:

```json
{
  "route": "metadata",
  "intent": "list_picklist_values",
  "entities": [
    {
      "kind": "field",
      "api_name": "Account.Candidate_Status__c"
    }
  ],
  "operations": [
    {
      "relationship": "has_value",
      "target_kind": "picklist_value"
    }
  ],
  "confidence": 0.98
}
```

Record plan example:

```json
{
  "route": "record",
  "table": "Account",
  "select": ["Id", "Name", "Candidate_Status__c"],
  "filters": [
    {
      "column": "Candidate_Status__c",
      "operator": "=",
      "value": "In Progress"
    }
  ],
  "limit": 100
}
```

The application must validate table and column names against the metadata
catalog, parameterize values, enforce row limits/timeouts, and log the plan.

## 12. Recommended next implementation order

1. Generate a metadata catalog from `graph.jsonl`:
   - entities
   - fields
   - picklist values
   - relationships
   - labels
   - aliases
2. Add deterministic query templates for common metadata questions.
3. Add a typed question router returning `metadata`, `record`, or `hybrid`.
4. Add entity resolution against the catalog.
5. Add typed graph query plans and plan validation.
6. Add source-text RAG over Salesforce metadata files.
7. Add Salesforce record ingestion into DuckDB.
8. Add typed SQL planning and parameterized execution.
9. Add hybrid query joins.
10. Add grounded answer synthesis and evaluation tests.

The first practical feature should be a deterministic `ask` command for common
metadata questions, before introducing an LLM:

```powershell
python -m graphrag.cli ask `
  --graph "graph.jsonl" `
  --question "Which picklist values are available on Account.Candidate_Status__c?"
```

The LLM can later improve phrasing and intent classification, but execution
must remain deterministic and grounded in validated graph/catalog results.

## 13. Development rules

- Preserve read-only behavior for the Salesforce mirror.
- Do not invent objects, fields, users, permissions, or relationships when the
  metadata is absent.
- Keep source evidence for every specialized edge.
- Prefer explicit specialized parsers over broad generic XML heuristics.
- Rebuild `graph.jsonl` after parser/model changes.
- Run the full standard-library test suite after changes.
- Keep generated `salesforce-vault` synchronized with `graph.jsonl` when the
  graph artifact changes.
- Do not add dependencies unless the dependency manifest changes and the
  environment can install them.
- Do not assume Git history exists in this checkout.
