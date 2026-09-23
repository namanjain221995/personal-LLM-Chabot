"""Build the component catalog from the Salesforce DX mirror.

The catalog — not the graph — is the authority for what exists and what it is
called. `graph.jsonl` overloads its `kind` field across file nodes and
component nodes, so counting by kind there double-counts, and its object nodes
include 384 entities that appear only in permission grants. Reading the mirror
directly avoids both problems: a directory either exists or it does not.

The graph remains the authority for relationships between components.
"""
from __future__ import annotations

import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

NS = "{http://soap.sforce.com/2006/04/metadata}"

SCHEMA = """
CREATE TABLE component (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    api_name       TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    label          TEXT,
    plural_label   TEXT,
    description    TEXT,
    parent_id      TEXT REFERENCES component(id),
    is_custom      INTEGER NOT NULL,
    is_stub        INTEGER NOT NULL,
    tier           INTEGER NOT NULL,
    source_path    TEXT NOT NULL,
    properties     TEXT NOT NULL
);
CREATE INDEX idx_component_kind ON component(kind);
CREATE INDEX idx_component_parent ON component(parent_id);
CREATE INDEX idx_component_api_name ON component(api_name COLLATE NOCASE);
CREATE INDEX idx_component_qualified ON component(qualified_name COLLATE NOCASE);
CREATE INDEX idx_component_label ON component(label COLLATE NOCASE);

CREATE TABLE picklist_value (
    field_id   TEXT NOT NULL REFERENCES component(id),
    value      TEXT NOT NULL,
    label      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_active  INTEGER NOT NULL DEFAULT 1,
    origin     TEXT NOT NULL,
    PRIMARY KEY (field_id, value)
);
CREATE INDEX idx_picklist_field ON picklist_value(field_id);
CREATE INDEX idx_picklist_value ON picklist_value(value COLLATE NOCASE);

CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Directory -> (kind, file suffix, tier). Everything under objects/ is handled
# separately because those components are nested under their object.
TOP_LEVEL = {
    "flows": ("flow", ".flow-meta.xml", 2),
    "layouts": ("layout", ".layout-meta.xml", 2),
    "flexipages": ("flexipage", ".flexipage-meta.xml", 2),
    "profiles": ("profile", ".profile-meta.xml", 2),
    "permissionsets": ("permission_set", ".permissionset-meta.xml", 2),
    "permissionsetgroups": ("permission_set_group", ".permissionsetgroup-meta.xml", 2),
    "globalValueSets": ("global_value_set", ".globalValueSet-meta.xml", 2),
    "sharingRules": ("sharing_rule", ".sharingRules-meta.xml", 2),
    "workflows": ("workflow", ".workflow-meta.xml", 2),
    "queues": ("queue", ".queue-meta.xml", 2),
    "roles": ("role", ".role-meta.xml", 2),
    "groups": ("group", ".group-meta.xml", 2),
    "classes": ("apex_class", ".cls", 3),
    "triggers": ("apex_trigger", ".trigger", 3),
    "quickActions": ("quick_action", ".quickAction-meta.xml", 3),
    "applications": ("app", ".app-meta.xml", 3),
    "tabs": ("custom_tab", ".tab-meta.xml", 3),
    "labels": ("custom_label_file", ".labels-meta.xml", 3),
    "staticresources": ("static_resource", ".resource-meta.xml", 3),
    "pages": ("visualforce_page", ".page", 3),
    "components": ("visualforce_component", ".component", 3),
    "customPermissions": ("custom_permission", ".customPermission-meta.xml", 3),
    "customMetadata": ("custom_metadata_record", ".md-meta.xml", 3),
    "duplicateRules": ("duplicate_rule", ".duplicateRule-meta.xml", 3),
    "matchingRules": ("matching_rule", ".matchingRule-meta.xml", 3),
    "assignmentRules": ("assignment_rule", ".assignmentRules-meta.xml", 3),
    "escalationRules": ("escalation_rule", ".escalationRules-meta.xml", 3),
    "autoResponseRules": ("auto_response_rule", ".autoResponseRules-meta.xml", 3),
    "approvalProcesses": ("approval_process", ".approvalProcess-meta.xml", 3),
    "emailservices": ("email_service", ".xml", 3),
    "connectedApps": ("connected_app", ".connectedApp-meta.xml", 3),
    "namedCredentials": ("named_credential", ".namedCredential-meta.xml", 3),
    "remoteSiteSettings": ("remote_site", ".remoteSite-meta.xml", 3),
    "notificationtypes": ("notification_type", ".notiftype-meta.xml", 3),
}

# Nested under objects/<Object>/<dir>/
OBJECT_CHILDREN = {
    "fields": ("field", ".field-meta.xml", 1),
    "validationRules": ("validation_rule", ".validationRule-meta.xml", 2),
    "recordTypes": ("record_type", ".recordType-meta.xml", 2),
    "compactLayouts": ("compact_layout", ".compactLayout-meta.xml", 2),
    "listViews": ("list_view", ".listView-meta.xml", 3),
    "webLinks": ("web_link", ".webLink-meta.xml", 3),
    "businessProcesses": ("business_process", ".businessProcess-meta.xml", 3),
    "fieldSets": ("field_set", ".fieldSet-meta.xml", 3),
    "indexes": ("index", ".index-meta.xml", 3),
}

# Bundle directories: the component is the directory, not a single file.
BUNDLES = {"lwc": ("lwc_bundle", 3), "aura": ("aura_bundle", 3)}

# Folder-nested: reports/<Folder>/<Name>.report-meta.xml
FOLDERED = {
    "reports": ("report", ".report-meta.xml", 3),
    "dashboards": ("dashboard", ".dashboard-meta.xml", 3),
    "email": ("email_template", ".email-meta.xml", 3),
    "documents": ("document", None, 3),
}


class CatalogError(RuntimeError):
    """Raised when the mirror cannot be read or is not shaped as expected."""


@dataclass
class Component:
    id: str
    kind: str
    api_name: str
    qualified_name: str
    source_path: str
    tier: int
    is_custom: bool
    label: str | None = None
    plural_label: str | None = None
    description: str | None = None
    parent_id: str | None = None
    is_stub: bool = False
    properties: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class PicklistValue:
    field_id: str
    value: str
    label: str | None
    is_default: bool
    is_active: bool
    origin: str


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _parse(path: Path) -> ET.Element | None:
    """Parse one metadata file. A malformed file is reported, never guessed at."""
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError, UnicodeError):
        return None


def _is_custom(api_name: str) -> bool:
    return api_name.endswith(("__c", "__mdt", "__e", "__b", "__x", "__kav"))


def _global_value_sets(default_dir: Path) -> dict[str, list[PicklistValue]]:
    """Read globalValueSets/ once so fields can resolve <valueSetName>."""
    out: dict[str, list[PicklistValue]] = {}
    source = default_dir / "globalValueSets"
    if not source.is_dir():
        return out
    for entry in sorted(source.iterdir()):
        if not entry.name.endswith(".globalValueSet-meta.xml"):
            continue
        name = entry.name[: -len(".globalValueSet-meta.xml")]
        root = _parse(entry)
        if root is None:
            continue
        values = []
        for value in root.findall(f"{NS}customValue"):
            full_name = _text(value.find(f"{NS}fullName"))
            if not full_name:
                continue
            values.append(
                PicklistValue(
                    field_id="",
                    value=full_name,
                    label=_text(value.find(f"{NS}label")),
                    is_default=_text(value.find(f"{NS}default")) == "true",
                    is_active=_text(value.find(f"{NS}isActive")) != "false",
                    origin=f"global:{name}",
                )
            )
        out[name] = values
    return out


def _field_picklist(
    root: ET.Element, field_id: str, globals_: dict[str, list[PicklistValue]]
) -> list[PicklistValue]:
    value_set = root.find(f"{NS}valueSet")
    if value_set is None:
        return []
    global_name = _text(value_set.find(f"{NS}valueSetName"))
    if global_name:
        return [
            PicklistValue(field_id, v.value, v.label, v.is_default, v.is_active, v.origin)
            for v in globals_.get(global_name, [])
        ]
    definition = value_set.find(f"{NS}valueSetDefinition")
    if definition is None:
        return []
    out = []
    for value in definition.findall(f"{NS}value"):
        full_name = _text(value.find(f"{NS}fullName"))
        if not full_name:
            continue
        out.append(
            PicklistValue(
                field_id=field_id,
                value=full_name,
                label=_text(value.find(f"{NS}label")),
                is_default=_text(value.find(f"{NS}default")) == "true",
                is_active=_text(value.find(f"{NS}isActive")) != "false",
                origin="local",
            )
        )
    return out


FIELD_PROPERTY_TAGS = (
    "type", "required", "unique", "externalId", "length", "precision", "scale",
    "referenceTo", "relationshipName", "relationshipLabel", "formula",
    "defaultValue", "trackHistory", "inlineHelpText", "summaryForeignKey",
    "summaryOperation", "deleteConstraint", "maskChar", "maskType", "restricted",
)


def _build_field(path: Path, object_api: str, globals_: dict[str, list[PicklistValue]]):
    api_name = path.name[: -len(".field-meta.xml")]
    qualified = f"{object_api}.{api_name}"
    component = Component(
        id=f"field:{qualified}",
        kind="field",
        api_name=api_name,
        qualified_name=qualified,
        source_path=str(path),
        tier=1,
        is_custom=_is_custom(api_name),
        parent_id=f"object:{object_api}",
    )
    root = _parse(path)
    if root is None:
        component.is_stub = True
        return component, []
    component.label = _text(root.find(f"{NS}label"))
    component.description = _text(root.find(f"{NS}description"))
    properties: dict[str, Any] = {}
    for tag in FIELD_PROPERTY_TAGS:
        value = _text(root.find(f"{NS}{tag}"))
        if value is not None:
            properties[tag] = value
    # referenceTo repeats for a polymorphic lookup.
    references = [_text(e) for e in root.findall(f"{NS}referenceTo")]
    references = [r for r in references if r]
    if references:
        properties["referenceTo"] = references
    component.properties = properties
    # A standard field the org never customised retrieves as little more than
    # its own name. Marking it is how the resolver later knows a missing label
    # is an absent customisation, not a parse failure.
    component.is_stub = not component.label and "type" not in properties
    return component, _field_picklist(root, component.id, globals_)


OBJECT_PROPERTY_TAGS = (
    "sharingModel", "deploymentStatus", "enableReports", "enableActivities",
    "enableHistory", "enableFeeds", "enableSearch", "enableBulkApi",
    "enableStreamingApi", "externalSharingModel", "visibility",
)


def _build_object(directory: Path) -> Component:
    api_name = directory.name
    component = Component(
        id=f"object:{api_name}",
        kind="object",
        api_name=api_name,
        qualified_name=api_name,
        source_path=str(directory),
        tier=1,
        is_custom=_is_custom(api_name),
    )
    meta = directory / f"{api_name}.object-meta.xml"
    if not meta.is_file():
        component.is_stub = True
        return component
    component.source_path = str(meta)
    root = _parse(meta)
    if root is None:
        component.is_stub = True
        return component
    component.label = _text(root.find(f"{NS}label"))
    component.plural_label = _text(root.find(f"{NS}pluralLabel"))
    component.description = _text(root.find(f"{NS}description"))
    properties = {}
    for tag in OBJECT_PROPERTY_TAGS:
        value = _text(root.find(f"{NS}{tag}"))
        if value is not None:
            properties[tag] = value
    name_field = root.find(f"{NS}nameField")
    if name_field is not None:
        properties["nameFieldLabel"] = _text(name_field.find(f"{NS}label"))
        properties["nameFieldType"] = _text(name_field.find(f"{NS}type"))
    component.properties = properties
    component.is_stub = component.label is None
    return component


def _build_simple(path: Path, kind: str, suffix: str, tier: int, parent_id: str | None = None):
    api_name = path.name[: -len(suffix)] if suffix and path.name.endswith(suffix) else path.stem
    qualified = f"{parent_id.split(':', 1)[1]}.{api_name}" if parent_id else api_name
    component = Component(
        id=f"{kind}:{qualified}",
        kind=kind,
        api_name=api_name,
        qualified_name=qualified,
        source_path=str(path),
        tier=tier,
        is_custom=_is_custom(api_name),
        parent_id=parent_id,
    )
    if path.suffix in (".cls", ".trigger", ".page", ".component"):
        return component
    root = _parse(path)
    if root is None:
        component.is_stub = True
        return component
    component.label = _text(root.find(f"{NS}label")) or _text(root.find(f"{NS}masterLabel"))
    component.description = _text(root.find(f"{NS}description"))
    properties = {}
    for tag in ("active", "status", "processType", "errorConditionFormula",
                "errorMessage", "errorDisplayField", "fullName", "type"):
        value = _text(root.find(f"{NS}{tag}"))
        if value is not None:
            properties[tag] = value
    component.properties = properties
    return component


def build_catalog(source: str) -> tuple[list[Component], list[PicklistValue], dict[str, Any]]:
    """Read the DX mirror and return every component it documents."""
    default_dir = Path(source)
    if not default_dir.is_dir():
        raise CatalogError(f"source directory not found: {source}")
    objects_dir = default_dir / "objects"
    if not objects_dir.is_dir():
        raise CatalogError(f"no objects/ directory under {source}; is this a DX default/ folder?")

    components: list[Component] = []
    picklists: list[PicklistValue] = []
    globals_ = _global_value_sets(default_dir)

    for directory in sorted(objects_dir.iterdir()):
        if not directory.is_dir():
            continue
        components.append(_build_object(directory))
        object_api = directory.name
        for child_dir, (kind, suffix, tier) in OBJECT_CHILDREN.items():
            child_path = directory / child_dir
            if not child_path.is_dir():
                continue
            for entry in sorted(child_path.iterdir()):
                if not entry.name.endswith(suffix):
                    continue
                if kind == "field":
                    component, values = _build_field(entry, object_api, globals_)
                    components.append(component)
                    picklists.extend(values)
                else:
                    components.append(
                        _build_simple(entry, kind, suffix, tier, parent_id=f"object:{object_api}")
                    )

    for dir_name, (kind, suffix, tier) in TOP_LEVEL.items():
        source_dir = default_dir / dir_name
        if not source_dir.is_dir():
            continue
        for entry in sorted(source_dir.iterdir()):
            if not entry.is_file() or not entry.name.endswith(suffix):
                continue
            components.append(_build_simple(entry, kind, suffix, tier))

    for dir_name, (kind, tier) in BUNDLES.items():
        source_dir = default_dir / dir_name
        if not source_dir.is_dir():
            continue
        for entry in sorted(source_dir.iterdir()):
            if not entry.is_dir():
                continue
            components.append(
                Component(
                    id=f"{kind}:{entry.name}",
                    kind=kind,
                    api_name=entry.name,
                    qualified_name=entry.name,
                    source_path=str(entry),
                    tier=tier,
                    is_custom=False,
                )
            )

    for dir_name, (kind, suffix, tier) in FOLDERED.items():
        source_dir = default_dir / dir_name
        if not source_dir.is_dir():
            continue
        for entry in sorted(source_dir.rglob("*")):
            if not entry.is_file():
                continue
            if suffix and not entry.name.endswith(suffix):
                continue
            relative = entry.relative_to(source_dir)
            folder = relative.parent.as_posix()
            api_name = entry.name[: -len(suffix)] if suffix else entry.stem
            qualified = f"{folder}/{api_name}" if folder != "." else api_name
            component = _build_simple(entry, kind, suffix or entry.suffix, tier)
            component.id = f"{kind}:{qualified}"
            component.qualified_name = qualified
            component.api_name = api_name
            component.properties["folder"] = folder if folder != "." else None
            components.append(component)

    counts: dict[str, int] = {}
    for component in components:
        counts[component.kind] = counts.get(component.kind, 0) + 1
    stats = {
        "component_count": len(components),
        "picklist_value_count": len(picklists),
        "counts_by_kind": dict(sorted(counts.items())),
        "objects_custom": sum(1 for c in components if c.kind == "object" and c.is_custom),
        "objects_stub": sum(1 for c in components if c.kind == "object" and c.is_stub),
        "fields_stub": sum(1 for c in components if c.kind == "field" and c.is_stub),
    }
    return components, picklists, stats


def write_catalog(
    components: list[Component],
    picklists: list[PicklistValue],
    stats: dict[str, Any],
    output: str,
    source: str,
) -> None:
    """Write a fresh catalog. An existing file at `output` is replaced."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(target)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO component (id, kind, api_name, qualified_name, label,"
            " plural_label, description, parent_id, is_custom, is_stub, tier,"
            " source_path, properties) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c.id, c.kind, c.api_name, c.qualified_name, c.label,
                    c.plural_label, c.description, c.parent_id, int(c.is_custom),
                    int(c.is_stub), c.tier, c.source_path,
                    json.dumps(c.properties, sort_keys=True),
                )
                for c in components
            ],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO picklist_value (field_id, value, label,"
            " is_default, is_active, origin) VALUES (?,?,?,?,?,?)",
            [
                (p.field_id, p.value, p.label, int(p.is_default), int(p.is_active), p.origin)
                for p in picklists
            ],
        )
        manifest = {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source": str(Path(source).resolve()),
            "catalog_version": "1",
            **{k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in stats.items()},
        }
        connection.executemany(
            "INSERT INTO manifest (key, value) VALUES (?,?)",
            [(k, str(v)) for k, v in manifest.items()],
        )
        connection.commit()
    finally:
        connection.close()
