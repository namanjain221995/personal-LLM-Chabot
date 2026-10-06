# Salesforce knowledge

Schema knowledge used before a record query is generated.

```text
Source (mirror | live describe)
    ↓  fetcher.py      one interface, two implementations
    ↓  normalizer.py   XML → internal records, stable hashes
    ↓  repository.py   the only place that issues SQL
SQLite  runtime_schema/db/<environment>/salesforce_runtime_schema.db
    ↓  search.py       FTS5 + exact/label/alias ranking ladder
    ↓  cache.py        L1 memory for hot objects
service.py             the public interface
```

## Source of truth, and what it cannot tell you

The live org is authoritative. Today the subsystem reads the **DX metadata
mirror**, which is a snapshot of that truth with known holes: Salesforce
computes some properties per org and per user, and they never appear in a
metadata retrieve.

Left `NULL` by a mirror refresh — 15 columns:

```text
objects   key_prefix · is_queryable · is_searchable · is_retrieveable
          is_createable · is_updateable · is_deletable · is_triggerable
fields    is_createable · is_updateable · is_filterable · is_sortable
          is_groupable
record_types  record_type_id
```

`NULL` means "this source cannot know", never `false`. Every row carries
`source`, and every describe-only column is written with `COALESCE`, so a later
live-describe refresh fills the gaps without a mirror refresh erasing them.

## Usage

```python
from salesforce.runtime_schema.service import RuntimeSchemaService

with RuntimeSchemaService(root="salesforce_knowledge") as svc:
    svc.refresh_schema()                      # fetch → SQLite → FTS → manifest
    svc.start()                               # catalog + hot objects into memory

    svc.search_objects("mock interview")      # compact candidates
    svc.get_object_schema("Interview__c")     # detail, once one is chosen
```

Progressive retrieval is the point: the catalog ranks candidates, detail is
fetched only after one is picked.

## Scripts

```bash
python salesforce_knowledge/scripts/refresh_runtime_schema.py [--export]
python salesforce_knowledge/scripts/rebuild_search_indexes.py
python salesforce_knowledge/scripts/export_runtime_schema.py
python salesforce_knowledge/scripts/validate_runtime_schema.py
```

## Hot objects

```text
pinned (schema_config.yaml)   hot immediately, never auto-demoted
auto   (usage threshold)      promoted on access count, demotable
new object                    starts cold unless pinned
```

## Not in this subsystem

Flow, ValidationRule, Profile, PermissionSet, Layout, FlexiPage and Apex
metadata belong to `metadata_context/`, which is a placeholder. Vector search
belongs to `indexes/vector/`; `search_objects` and `search_fields` are the seam
it slots behind.

Credentials never reach this subsystem — not the database, manifests, exports,
snapshots or logs.
