"""Internal schema records.

One shape regardless of where the schema came from. A field read from the DX
mirror and the same field read from a live describe produce the same record;
only `source` and the describe-only columns differ. That is what lets the two
fetchers be swapped without touching anything downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

# Columns Salesforce computes at runtime, per org and per user permissions.
# A metadata retrieve never contains them, so a mirror-sourced row leaves them
# None -- "not known", which is a different claim from False.
DESCRIBE_ONLY_OBJECT_FIELDS = (
    "key_prefix", "is_queryable", "is_searchable", "is_retrieveable",
    "is_createable", "is_updateable", "is_deletable", "is_triggerable",
)
DESCRIBE_ONLY_FIELD_FIELDS = (
    "is_createable", "is_updateable", "is_filterable", "is_sortable",
    "is_groupable",
)


@dataclass
class SObject:
    api_name: str
    label: str | None = None
    plural_label: str | None = None
    description: str | None = None
    key_prefix: str | None = None
    is_custom: bool = False
    is_queryable: bool | None = None
    is_searchable: bool | None = None
    is_retrieveable: bool | None = None
    is_createable: bool | None = None
    is_updateable: bool | None = None
    is_deletable: bool | None = None
    is_triggerable: bool | None = None
    is_deprecated: bool | None = None
    record_type_supported: bool = False
    field_count: int = 0
    relationship_count: int = 0
    source: str = "mirror"
    raw_hash: str = ""


@dataclass
class SField:
    object_api_name: str
    api_name: str
    label: str | None = None
    description: str | None = None
    inline_help_text: str | None = None
    data_type: str | None = None
    length: int | None = None
    precision_value: int | None = None
    scale_value: int | None = None
    is_nullable: bool = True
    is_unique: bool = False
    is_external_id: bool = False
    is_auto_number: bool = False
    is_calculated: bool = False
    calculated_formula: str | None = None
    is_createable: bool | None = None
    is_updateable: bool | None = None
    is_filterable: bool | None = None
    is_sortable: bool | None = None
    is_groupable: bool | None = None
    is_name_field: bool = False
    relationship_name: str | None = None
    reference_to: list[str] = dataclass_field(default_factory=list)
    default_value: str | None = None
    defaulted_on_create: bool = False
    restricted_picklist: bool = False
    source: str = "mirror"
    raw_hash: str = ""


@dataclass
class Relationship:
    source_object: str
    source_field: str
    target_object: str
    relationship_name: str | None = None
    relationship_type: str | None = None      # Lookup | MasterDetail | Hierarchy
    is_polymorphic: bool = False
    cascade_delete: bool = False


@dataclass
class ChildRelationship:
    parent_object: str
    child_object: str
    child_field: str | None = None
    relationship_name: str | None = None
    cascade_delete: bool = False


@dataclass
class PicklistValue:
    object_api_name: str
    field_api_name: str
    value: str
    label: str | None = None
    is_active: bool = True
    is_default: bool = False
    valid_for: str | None = None
    sort_order: int | None = None


@dataclass
class RecordType:
    object_api_name: str
    developer_name: str
    name: str | None = None
    description: str | None = None
    record_type_id: str | None = None    # runtime id; never in the mirror
    is_active: bool = True
    is_default: bool = False


@dataclass
class Alias:
    """A word someone might use for a component, with where it came from."""
    object_api_name: str
    alias: str
    field_api_name: str | None = None
    source: str = "generated"
    confidence: float = 1.0
    is_manual: bool = False


@dataclass
class SchemaBundle:
    """Everything one fetch produced."""
    objects: list[SObject] = dataclass_field(default_factory=list)
    fields: list[SField] = dataclass_field(default_factory=list)
    relationships: list[Relationship] = dataclass_field(default_factory=list)
    child_relationships: list[ChildRelationship] = dataclass_field(default_factory=list)
    picklist_values: list[PicklistValue] = dataclass_field(default_factory=list)
    record_types: list[RecordType] = dataclass_field(default_factory=list)
    object_aliases: list[Alias] = dataclass_field(default_factory=list)
    field_aliases: list[Alias] = dataclass_field(default_factory=list)
    errors: list[str] = dataclass_field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "objects": len(self.objects),
            "fields": len(self.fields),
            "relationships": len(self.relationships),
            "child_relationships": len(self.child_relationships),
            "picklist_values": len(self.picklist_values),
            "record_types": len(self.record_types),
            "object_aliases": len(self.object_aliases),
            "field_aliases": len(self.field_aliases),
        }
