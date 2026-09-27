# Salesforce-Org-Data

Versioned **read-only mirror** of the Techsara production Salesforce org's metadata
(`00DKj000002YwNLMA0`, techsara.my.salesforce.com). This is a **backup/rollback repo,
not a development repo** — nothing here is hand-edited; every commit on `main` is an
automated snapshot pulled from prod.

## Branch & tag model

| Ref | Meaning |
|---|---|
| `main` | Always the latest prod state. Linear history: one commit per snapshot. |
| `deploy/<date>-<feature>` (tag) | Snapshot taken right after a prod deployment. |
| `snapshot/<date>` (tag) | Weekly scheduled snapshot. |
| `week-1` … `week-7` (branches) | Auto-maintained pointers: `week-N` ≈ the org state N weeks ago. Rotated weekly by automation — **never commit to or merge these**. |
| `rotation/<YYYY-Www>` (tag) | Marker proving the week rotation ran for that ISO week (makes rotation idempotent). |

Because history is permanent, tags give unlimited restore points; the `week-*` branches
are just a convenient 7-week window into that history.

## How snapshots happen

The Jenkins pipeline `salesforce-org-backup` (defined in [`Jenkinsfile`](Jenkinsfile)) runs on:

1. **After every prod deployment** — triggered downstream from the deploy job with the feature name.
2. **Weekly cron** (Sun ~02:00) — also rotates the `week-*` branches.
3. **Manually** — "Build with Parameters", optional `FEATURE_NAME`.

Every run does a **full-org retrieve** (manifest regenerated from the org each time), commits
only if something actually changed, and always creates an annotated tag as the audit trail.
The pipeline is strictly read-only against prod: retrieve only, never deploy.

## Rolling back

See **[docs/RUNBOOK-rollback.md](docs/RUNBOOK-rollback.md)**. Short version: find the restore
tag, diff it against `main`, deploy only the affected components back — always `--dry-run`
first, always with human approval. Rollback is never automated.

## Layout

- `Prod Org Data/` — the SFDX project; `force-app/main/default/` is the org mirror.
- `Prod Org Data/scripts/backup/` — snapshot + rotation scripts used by the pipeline.
- `docs/` — [architecture](docs/architecture.md) and the [rollback runbook](docs/RUNBOOK-rollback.md).

Do not commit to `main` by hand between snapshots — a manual commit isn't org state and
will be misleading during an incident (automation tolerates it, but the mirror guarantee breaks).


## Local GraphRAG MVP

The read-only `graphrag` package indexes Salesforce DX source into a small JSON graph:

```powershell
python -m graphrag.cli index --source "Prod Org Data\force-app\main\default" --output graph.jsonl
```

It classifies DX paths, extracts XML and Apex references, and preserves source
line evidence on edges. Use a `.json` output for one document or `.jsonl` for
newline-delimited node and edge records. Run `python -m unittest discover -s tests`
for the dependency-free test suite. The package can also be installed locally with
`python -m pip install -e .`, after which the `graphrag` command is available.

Query an indexed graph by exact node id or name:

```powershell
$env:PYTHONPATH="src"
python -m graphrag.cli query --graph graph.jsonl --match "Interview__c" --mode dependencies
python -m graphrag.cli query --graph graph.jsonl --match "object:Interview__c" --mode find
python -m graphrag.cli query --graph graph.jsonl --match "Interview__c" --mode traverse --depth 2
```

`dependencies` returns direct source files that reference the match. `traverse`
walks reverse references up to the requested depth. Results include graph paths
and source evidence where the parser produced it.

List normalized fields for an object:

```powershell
python -m graphrag.cli fields --graph graph.jsonl --object "Interview__c"
```

Field nodes use the stable id `field:<Object>.<Field>` and are connected to
their object with a `has_field` edge. The field definition file is retained as
the node path and as evidence on the relationship.

Required fields can be listed for one object or for the entire graph:

```powershell
python -m graphrag.cli required-fields --graph graph.jsonl --object "Interview__c"
python -m graphrag.cli required-fields --graph graph.jsonl
```

This uses the Salesforce field metadata `<required>true</required>` value and
also retains the field label and type when present. Latest-commit filtering is
not available in the current checkout because it is not a Git repository and
the graph artifact does not yet contain snapshot metadata; the command rejects
`--latest-commit` instead of returning an incomplete result.

Relationship fields now create normalized edges:

```text
field:Interview__c.Candidate__c --master_detail_to--> object:Account
field:Interview__c.Job__c       --master_detail_to--> object:Job__c
```

The relationship metadata retains `referenceTo`, `relationshipName`, and source
evidence from the field definition.

Flow metadata is modeled as executable relationships:

```text
Flow --targets--> Object
Flow --triggers_on--> Object
Flow --triggers_on_event--> Event
Flow --reads_field--> Field
Flow --writes_field--> Field
Flow --creates/updates/deletes--> Object
Flow --calls--> ApexClass
Flow --calls_subflow--> Subflow
Flow --uses--> EmailAlert/ApprovalProcess/CustomNotification
Flow --has_element--> FlowElement
```

Flow elements are typed as assignments, decisions, screens, and paths when
those metadata sections are present. Relationship evidence points back to the
Flow XML source.

Picklist options are modeled explicitly when they are defined in field metadata:

```text
field:Account.Client_Status__c
    --has_value-->
picklist_value:Account.Client_Status__c.Active
```

Query them with:

```powershell
python -m graphrag.cli picklist-values `
  --graph "graph.jsonl" `
  --field "Account.Client_Status__c"
```

Export the graph as an Obsidian vault:

```powershell
python -m graphrag.cli export-obsidian `
  --graph "graph.jsonl" `
  --output "salesforce-vault"
```

Open `salesforce-vault` as a vault in Obsidian. Each graph node becomes a
Markdown note and every graph edge becomes an Obsidian wikilink, so Obsidian's
graph view and backlinks can be used to explore the metadata.

Each option retains its source path and metadata such as `default` and
`isActive` when present. Fields backed by a Salesforce global value set are
resolved to the corresponding `globalValueSets` metadata as well.

Object-level inverse relationships are also generated from relationship fields:

```text
Field --lookup_to--> Object
Field --master_detail_to--> Object
Object --parent_of--> Object
Object --child_of--> Object
```

For example, if `Interview__c.Candidate__c` references `Account`, the graph
contains `Account --parent_of--> Interview__c` and
`Interview__c --child_of--> Account`. These inverse edges retain the same
field-definition evidence as the originating relationship.

Object-level structural metadata is also connected:

```text
Object --has_record_type--> RecordType
Object --has_validation_rule--> ValidationRule
Object --has_duplicate_rule--> DuplicateRule
Object --has_compact_layout--> CompactLayout
Object --has_search_layout--> SearchLayout
Object --has_business_process--> BusinessProcess
Object --has_sharing_rule--> SharingRule
Object --has_index--> Index
```

The indexer also extracts deterministic metadata relationships (only when the
corresponding XML contains the source value): validation rules apply to objects,
reference fields, inferred create/update operations, formula functions, and
custom-label messages; profiles and permission sets expose object, field, and
system permissions; permission-set groups contain permission sets; sharing rules
apply to objects, share with roles/groups, and filter by fields; roles report to
roles; queues support objects; Lightning pages contain components and target
objects/fields/apps/record types/profiles; layouts belong to objects and display
fields/buttons/sections with profile/record-type assignments; record types belong
to objects and use picklist fields/layouts; reports select, filter, and group
fields and are stored in folders; dashboards contain reports and are stored in
folders. Evidence is retained from the exact XML element for every edge.

Some Salesforce relationships cannot be determined from source metadata alone
(for example, role ownership, profile-to-user assignment, or inferred report
semantics) and are intentionally not emitted. Metadata types absent from a
snapshot (such as users, groups, or queues) naturally produce no nodes or edges;
the graph does not invent them.
