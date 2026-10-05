"""Turn Salesforce metadata XML into internal records.

Hashing is normalized on purpose: `raw_hash` covers the schema-bearing values
only, never the refresh timestamp, so an unchanged object hashes identically
across refreshes and the pipeline can skip rebuilding its search entries.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (Alias, ChildRelationship, PicklistValue, RecordType,
                     Relationship, SField, SObject)

NS = "{http://soap.sforce.com/2006/04/metadata}"

CUSTOM_SUFFIXES = ("__c", "__mdt", "__e", "__b", "__x", "__kav")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SUFFIX = re.compile(r"__(c|mdt|e|b|x|kav)$")

# A relationship field's type decides whether deleting the parent deletes the
# child: master-detail cascades, a lookup does not.
CASCADING_TYPES = {"MasterDetail"}


def text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def flag(element: ET.Element | None) -> bool:
    return text(element) == "true"


def integer(element: ET.Element | None) -> int | None:
    value = text(element)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def is_custom(api_name: str) -> bool:
    return api_name.endswith(CUSTOM_SUFFIXES)


def name_tokens(api_name: str) -> list[str]:
    """Date_of_Interview__c -> [date, of, interview]. Splits underscore AND camel."""
    bare = _SUFFIX.sub("", api_name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub(" ", bare)) if w]


def stable_hash(payload: Any) -> str:
    """Hash of the schema-bearing values, excluding anything time-varying."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def _hashable(record: Any, drop: tuple[str, ...]) -> dict[str, Any]:
    data = asdict(record)
    for key in drop + ("raw_hash", "source"):
        data.pop(key, None)
    return data


FIELD_TAGS = (
    ("label", "label"), ("description", "description"),
    ("inlineHelpText", "inline_help_text"), ("type", "data_type"),
    ("relationshipName", "relationship_name"), ("formula", "calculated_formula"),
    ("defaultValue", "default_value"),
)


def normalize_field(root: ET.Element, object_api_name: str,
                    api_name: str, name_field: str | None) -> SField:
    field = SField(object_api_name=object_api_name, api_name=api_name)
    for tag, attribute in FIELD_TAGS:
        setattr(field, attribute, text(root.find(f"{NS}{tag}")))
    field.length = integer(root.find(f"{NS}length"))
    field.precision_value = integer(root.find(f"{NS}precision"))
    field.scale_value = integer(root.find(f"{NS}scale"))
    # <required>true</required> means the value may not be null.
    field.is_nullable = not flag(root.find(f"{NS}required"))
    field.is_unique = flag(root.find(f"{NS}unique"))
    field.is_external_id = flag(root.find(f"{NS}externalId"))
    field.is_auto_number = field.data_type == "AutoNumber"
    field.is_calculated = field.calculated_formula is not None
    field.is_name_field = (name_field is not None and api_name == name_field)
    field.reference_to = [r for r in (text(e) for e in root.findall(f"{NS}referenceTo")) if r]
    field.defaulted_on_create = field.default_value is not None
    value_set = root.find(f"{NS}valueSet")
    if value_set is not None:
        field.restricted_picklist = flag(value_set.find(f"{NS}restricted"))
    field.raw_hash = stable_hash(_hashable(field, ()))
    return field


def normalize_picklist(root: ET.Element, object_api_name: str, api_name: str,
                       global_sets: dict[str, list[dict[str, Any]]]
                       ) -> list[PicklistValue]:
    value_set = root.find(f"{NS}valueSet")
    if value_set is None:
        return []
    global_name = text(value_set.find(f"{NS}valueSetName"))
    raw: list[dict[str, Any]] = []
    if global_name:
        raw = global_sets.get(global_name, [])
    else:
        definition = value_set.find(f"{NS}valueSetDefinition")
        if definition is not None:
            for order, value in enumerate(definition.findall(f"{NS}value")):
                full_name = text(value.find(f"{NS}fullName"))
                if full_name:
                    raw.append({"value": full_name,
                                "label": text(value.find(f"{NS}label")),
                                "is_default": flag(value.find(f"{NS}default")),
                                "is_active": text(value.find(f"{NS}isActive")) != "false",
                                "sort_order": order})
    return [PicklistValue(object_api_name=object_api_name, field_api_name=api_name,
                          value=item["value"], label=item.get("label"),
                          is_default=bool(item.get("is_default")),
                          is_active=bool(item.get("is_active", True)),
                          sort_order=item.get("sort_order"))
            for item in raw]


def normalize_relationship(field: SField) -> list[Relationship]:
    """A relationship per target: a polymorphic lookup names several."""
    if not field.reference_to:
        return []
    polymorphic = len(field.reference_to) > 1
    cascade = field.data_type in CASCADING_TYPES
    return [Relationship(source_object=field.object_api_name,
                         source_field=field.api_name,
                         target_object=target,
                         relationship_name=field.relationship_name,
                         relationship_type=field.data_type,
                         is_polymorphic=polymorphic,
                         cascade_delete=cascade)
            for target in field.reference_to]


def invert_to_child_relationships(relationships: list[Relationship]
                                  ) -> list[ChildRelationship]:
    """Parent -> child edges, derived by inverting every lookup.

    A live describe returns childRelationships directly. The metadata mirror
    does not, but it holds every referenceTo, and a child relationship IS the
    reverse of one -- so inverting recovers the same edges without inventing
    anything.
    """
    return [ChildRelationship(parent_object=r.target_object,
                              child_object=r.source_object,
                              child_field=r.source_field,
                              relationship_name=r.relationship_name,
                              cascade_delete=r.cascade_delete)
            for r in relationships]


OBJECT_TAGS = (("label", "label"), ("pluralLabel", "plural_label"),
               ("description", "description"))


def normalize_object(root: ET.Element | None, api_name: str, *,
                     has_record_types: bool) -> tuple[SObject, str | None]:
    """The object record, plus its name field if the metadata declares one."""
    obj = SObject(api_name=api_name, is_custom=is_custom(api_name),
                  record_type_supported=has_record_types)
    name_field: str | None = None
    if root is not None:
        for tag, attribute in OBJECT_TAGS:
            setattr(obj, attribute, text(root.find(f"{NS}{tag}")))
        status = text(root.find(f"{NS}deploymentStatus"))
        if status is not None:
            obj.is_deprecated = status.lower() == "deprecated"
        name = root.find(f"{NS}nameField")
        if name is not None:
            name_field = text(name.find(f"{NS}fullName")) or "Name"
    obj.raw_hash = stable_hash(_hashable(obj, ("field_count", "relationship_count")))
    return obj, name_field


# Fields Salesforce puts on every object. The DX mirror only carries fields
# someone deployed, so none of these appear in it -- which left "records created
# in May" and "the employee named Jayesh Prajapati" with nothing to ground to.
# They are platform facts, not guesses: every custom object has all of them.
PLATFORM_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("Id", "Record ID", "Id"),
    ("CreatedDate", "Created Date", "DateTime"),
    ("CreatedById", "Created By ID", "Lookup"),
    ("LastModifiedDate", "Last Modified Date", "DateTime"),
    ("LastModifiedById", "Last Modified By ID", "Lookup"),
    ("SystemModstamp", "System Modstamp", "DateTime"),
    ("IsDeleted", "Deleted", "Checkbox"),
)


def name_field_info(root: ET.Element | None) -> tuple[str | None, str | None]:
    """Label and type of the object's declared name field, if it declares one."""
    if root is None:
        return None, None
    name = root.find(f"{NS}nameField")
    if name is None:
        return None, None
    return text(name.find(f"{NS}label")), text(name.find(f"{NS}type"))


def platform_fields(obj: SObject, existing: set[str], name_field: str | None,
                    name_label: str | None, name_type: str | None) -> list[SField]:
    """The standard fields the mirror omits, marked `source='platform_standard'`.

    Only fields not already present -- a standard object's mirror may carry
    some of these itself, and the mirror's own definition wins. `Name` is added
    only when the object's metadata declares a name field, because not every
    standard object has one (Case does not) and inventing it would pass
    grounding and then fail against the warehouse.
    """
    added: list[SField] = []
    wanted = list(PLATFORM_FIELDS)
    if name_field:
        wanted.insert(1, (name_field, name_label or "Name",
                          "AutoNumber" if name_type == "AutoNumber" else "Text"))
    for api_name, label, data_type in wanted:
        if api_name in existing:
            continue
        field = SField(object_api_name=obj.api_name, api_name=api_name,
                       label=label, data_type=data_type,
                       is_name_field=(api_name == name_field),
                       is_auto_number=(data_type == "AutoNumber"),
                       is_nullable=api_name not in ("Id", "CreatedDate"),
                       reference_to=["User"] if data_type == "Lookup" else [],
                       source="platform_standard")
        field.raw_hash = stable_hash(_hashable(field, ()))
        added.append(field)
    return added


def generate_aliases(obj: SObject, fields: list[SField]) -> tuple[list[Alias], list[Alias]]:
    """Aliases derivable from names and labels alone.

    Only what a name or label implies. Business vocabulary -- "placed",
    "candidate" -- is not here: nothing in the metadata says it, and inventing
    it would put a guess behind a query planner.
    """
    object_aliases: list[Alias] = []
    seen: set[str] = set()

    def add(target: list[Alias], alias: str | None, field_name: str | None,
            source: str, confidence: float) -> None:
        if not alias:
            return
        key = f"{field_name or ''}|{alias.lower()}"
        if key in seen:
            return
        seen.add(key)
        target.append(Alias(object_api_name=obj.api_name, alias=alias.lower(),
                            field_api_name=field_name, source=source,
                            confidence=confidence))

    add(object_aliases, " ".join(name_tokens(obj.api_name)), None, "api_name", 0.9)
    add(object_aliases, obj.label, None, "label", 1.0)
    add(object_aliases, obj.plural_label, None, "plural_label", 0.95)

    field_aliases: list[Alias] = []
    for field in fields:
        add(field_aliases, " ".join(name_tokens(field.api_name)),
            field.api_name, "api_name", 0.9)
        add(field_aliases, field.label, field.api_name, "label", 1.0)
    return object_aliases, field_aliases
