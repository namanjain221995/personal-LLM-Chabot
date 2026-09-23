from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Edge, Evidence, Graph, Node
from .parsers import parse_apex, parse_xml_references
from .scanner import SourceFile, scan


def _file_node(source: SourceFile) -> Node:
    return Node(f"file:{source.relative_path}", source.kind, Path(source.relative_path).name,
                source.relative_path, {"component": source.component})


def _object_field_path(source: SourceFile) -> tuple[str, str] | None:
    parts = Path(source.relative_path).parts
    if len(parts) != 4 or parts[0] != "objects" or parts[2] != "fields":
        return None
    if not parts[3].endswith(".field-meta.xml"):
        return None
    return parts[1], parts[3][:-len(".field-meta.xml")]


_STRUCTURAL_COMPONENTS = {
    "recordTypes": ("record_type", "has_record_type", ".recordType-meta.xml"),
    "validationRules": ("validation_rule", "has_validation_rule", ".validationRule-meta.xml"),
    "duplicateRules": ("duplicate_rule", "has_duplicate_rule", ".duplicateRule-meta.xml"),
    "compactLayouts": ("compact_layout", "has_compact_layout", ".compactLayout-meta.xml"),
    "searchLayouts": ("search_layout", "has_search_layout", ".searchLayouts-meta.xml"),
    "businessProcesses": ("business_process", "has_business_process", ".businessProcess-meta.xml"),
    "sharingRules": ("sharing_rule", "has_sharing_rule", ".sharingRules-meta.xml"),
    "indexes": ("index", "has_index", ".index-meta.xml"),
}


def _object_structural_path(source: SourceFile) -> tuple[str, str, str, str] | None:
    parts = Path(source.relative_path).parts
    if len(parts) != 4 or parts[0] != "objects":
        return None
    component = _STRUCTURAL_COMPONENTS.get(parts[2])
    if component is None or not parts[3].endswith(component[2]):
        return None
    kind, edge_kind, suffix = component
    return parts[1], parts[3][:-len(suffix)], kind, edge_kind


def _field_properties(source: SourceFile, field_name: str) -> dict[str, object]:
    properties: dict[str, object] = {"field": field_name}
    if source.text is None:
        return properties
    try:
        root = ET.fromstring(source.text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML in {source.relative_path}: {exc}") from exc
    for tag in ("required", "label", "type", "referenceTo", "relationshipName"):
        value = next(
            (element.text.strip() for element in root.iter()
             if element.tag.rsplit("}", 1)[-1] == tag and element.text and element.text.strip()),
            None,
        )
        if value is not None:
            properties[tag] = value.lower() == "true" if tag == "required" else value
    return properties


def _field_picklist_values(
    source: SourceFile,
    global_value_sets: dict[str, SourceFile],
) -> tuple[tuple[dict[str, object], SourceFile], ...]:
    if source.text is None:
        return ()
    root = _xml_root(source)
    if root is None:
        return ()
    values: list[dict[str, object]] = []
    for value_element in root.iter():
        if _local(value_element) != "value":
            continue
        fields: dict[str, object] = {}
        for child in list(value_element):
            tag = _local(child)
            text = child.text.strip() if child.text and child.text.strip() else None
            if text is None:
                continue
            fields[tag] = text.lower() if tag == "default" else text
        if "fullName" in fields:
            values.append((fields, source))
    for value_set_name in root.iter():
        if _local(value_set_name) != "valueSetName" or not value_set_name.text:
            continue
        global_source = global_value_sets.get(value_set_name.text.strip())
        if global_source is None:
            continue
        global_root = _xml_root(global_source)
        if global_root is None:
            continue
        for value_element in global_root.iter():
            if _local(value_element) != "customValue":
                continue
            fields: dict[str, object] = {}
            for child in list(value_element):
                tag = _local(child)
                text = child.text.strip() if child.text and child.text.strip() else None
                if text is not None:
                    fields[tag] = text.lower() if tag in {"default", "isActive"} else text
            if "fullName" in fields:
                values.append((fields, global_source))
    return tuple(values)


def _xml_evidence(source: SourceFile, tag: str, value: str) -> Evidence:
    if source.text is None:
        return Evidence(source.relative_path, 1, f"{tag}: {value}")
    for line_number, line in enumerate(source.text.splitlines(), 1):
        if value in line and f">{tag}<" in line:
            return Evidence(source.relative_path, line_number, line.strip()[:240])
    return Evidence(source.relative_path, 1, f"{tag}: {value}")


def _metadata_name(source: SourceFile) -> str:
        name = Path(source.relative_path).name
        for suffix in (".validationRule-meta.xml", ".recordType-meta.xml", ".sharingRules-meta.xml",
                       ".role-meta.xml", ".queue-meta.xml", ".flexipage-meta.xml",
                       ".report-meta.xml", ".dashboard-meta.xml", ".permissionset-meta.xml",
                       ".permissionsetgroup-meta.xml", ".profile-meta.xml", ".layout-meta.xml"):
            if name.endswith(suffix):
                return name[:-len(suffix)]
        return Path(name).stem


def _xml_root(source: SourceFile) -> ET.Element | None:
        if source.text is None:
            return None
        try:
            return ET.fromstring(source.text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid XML in {source.relative_path}: {exc}") from exc


def _text(element: ET.Element, tag: str) -> str | None:
        return next((child.text.strip() for child in element.iter()
                     if _local(child) == tag and child.text and child.text.strip()), None)


def _add_metadata_edge(
        source: SourceFile, component_id: str, target_kind: str, target_name: str, edge_kind: str,
        nodes: dict[str, Node], edges: dict[tuple[str, str, str], list[Evidence]],
        tag: str | None = None,
) -> None:
        target_id = f"{target_kind}:{target_name}"
        nodes.setdefault(target_id, Node(target_id, target_kind, target_name))
        edges.setdefault((component_id, target_id, edge_kind), []).append(
            _xml_evidence(source, tag or target_kind, target_name)
        )


def _metadata_references(
        source: SourceFile, object_names: set[str], field_keys: set[str],
        label_names: set[str], nodes: dict[str, Node],
        edges: dict[tuple[str, str, str], list[Evidence]],
) -> None:
        """Extract relationships whose meaning is specific to a metadata type."""
        parts = Path(source.relative_path).parts
        folder = parts[0] if parts else ""
        name = _metadata_name(source)
        kind_map = {
            "permissionsets": "permission_set", "profiles": "profile", "roles": "role",
            "queues": "queue", "flexipages": "lightning_page", "layouts": "layout",
            "reports": "report", "dashboards": "dashboard",
            "permissionSetGroups": "permission_set_group",
        }
        component_kind = kind_map.get(folder)
        if component_kind is None and len(parts) >= 3 and parts[0] == "objects":
            component_kind = {
                "validationRules": "validation_rule", "sharingRules": "sharing_rule",
                "recordTypes": "record_type",
            }.get(parts[2])
        if component_kind is None:
            return
        root = _xml_root(source)
        if root is None:
            return
        component_name = f"{parts[1]}.{name}" if len(parts) >= 3 and parts[0] == "objects" else name
        component_id = f"{component_kind}:{component_name}"
        nodes.setdefault(component_id, Node(component_id, component_kind, component_name, source.relative_path))

        def edge(kind: str, target_kind: str, value: str, tag: str | None = None) -> None:
            _add_metadata_edge(source, component_id, target_kind, value, kind, nodes, edges, tag)

        object_name = parts[1] if len(parts) >= 3 and parts[0] == "objects" else None
        if component_kind == "layout" and "-" in name:
            object_name = name.split("-", 1)[0]
        if component_kind == "validation_rule":
            if object_name:
                edge("applies_to", "object", object_name)
            formula = _text(root, "errorConditionFormula") or ""
            for field_key in field_keys:
                obj, field = field_key.split(".", 1)
                if obj == object_name and re.search(rf"\b{re.escape(field)}\b", formula):
                    edge("references_field", "field", field_key, "errorConditionFormula")
            for function in re.findall(r"\b([A-Z][A-Z0-9_]*)\s*\(", formula):
                edge("uses_function", "function", function, "errorConditionFormula")
            for operation, marker in (("Create", "ISNEW"), ("Update", "ISCHANGED")):
                if marker in formula.upper():
                    edge("blocks_operation", "operation", operation, "errorConditionFormula")
            message = _text(root, "errorMessage")
            if message and message in label_names:
                edge("has_error_message", "custom_label", message, "errorMessage")
            for label in re.findall(r"\$Label\.([A-Za-z0-9_]+)", formula):
                if label in label_names:
                    edge("has_error_message", "custom_label", label, "errorConditionFormula")
        elif component_kind in {"permission_set", "profile"}:
            for item in root.iter():
                tag = _local(item)
                value = _text(item, "object") if tag == "objectPermissions" else None
                if value:
                    edge("object_permission", "object", value, "object")
                if tag == "fieldPermissions":
                    field = _text(item, "field")
                    if field and "." in field:
                        edge("field_permission", "field", field, "field")
                if tag in {"userPermissions", "systemPermissions"}:
                    permission = _text(item, "name")
                    if permission:
                        edge("system_permission", "system_permission", permission, "name")
                if tag == "recordTypeVisibilities":
                    record_type = _text(item, "recordType")
                    if record_type:
                        record_id = f"record_type:{record_type}"
                        nodes.setdefault(record_id, Node(record_id, "record_type", record_type))
                        edges.setdefault((record_id, component_id, "assigned_to"), []).append(
                            _xml_evidence(source, "recordType", record_type)
                        )
        elif component_kind == "permission_set_group":
            for item in root.iter():
                if _local(item) in {"permissionSet", "permissionSetName"} and item.text and item.text.strip():
                    edge("contains", "permission_set", item.text.strip(), _local(item))
        elif component_kind == "sharing_rule":
            if object_name:
                edge("applies_to", "object", object_name)
            for tag, target_kind in (("role", "role"), ("group", "group"), ("publicGroup", "group")):
                for item in root.iter():
                    if _local(item) == tag and item.text and item.text.strip():
                        edge("shares_with", target_kind, item.text.strip(), tag)
            for item in root.iter():
                if _local(item) in {"field", "fieldName"} and item.text and "." in item.text:
                    edge("filters_by_field", "field", item.text.strip(), _local(item))
        elif component_kind == "role":
            parent = _text(root, "parentRole")
            if parent:
                edge("reports_to", "role", parent, "parentRole")
        elif component_kind == "queue":
            for item in root.iter():
                if _local(item) == "object" and item.text and item.text.strip():
                    edge("supports", "object", item.text.strip(), "object")
        elif component_kind == "lightning_page":
            for item in root.iter():
                tag = _local(item)
                value = item.text.strip() if item.text and item.text.strip() else None
                if not value:
                    continue
                if tag in {"componentName", "componentInstance"}:
                    edge("contains", "component", value, tag)
                elif tag in {"object", "sobjectType"} and value in object_names:
                    edge("targets", "object", value, tag)
                elif tag in {"fieldName", "field"} and "." in value:
                    edge("uses", "field", value, tag)
                elif tag in {"application", "app"}:
                    edge("assigned_to", "app", value, tag)
                elif tag == "recordType":
                    edge("assigned_to", "record_type", value, tag)
                elif tag == "profile":
                    edge("assigned_to", "profile", value, tag)
        elif component_kind == "layout":
            if object_name:
                edge("for_object", "object", object_name)
            for item in root.iter():
                tag = _local(item)
                value = item.text.strip() if item.text and item.text.strip() else None
                if not value:
                    continue
                if tag == "field" and f"{object_name}.{value}" in field_keys:
                    edge("displays_field", "field", f"{object_name}.{value}", tag)
                elif tag == "name" and any(_local(p) == "button" for p in root.iter() if item in list(p)):
                    edge("displays_button", "button", value, tag)
                elif tag == "layoutSections":
                    edge("displays_section", "section", value, tag)
                elif tag == "profile":
                    edge("assigned_to", "profile", value, tag)
                elif tag == "recordType":
                    edge("assigned_to", "record_type", value, tag)
        elif component_kind == "record_type":
            if object_name:
                edge("for_object", "object", object_name)
            for item in root.iter():
                if _local(item) == "picklistValues":
                    field = _text(item, "picklist")
                    if field and object_name:
                        edge("uses_picklist_value", "field", f"{object_name}.{field}", "picklist")
                elif _local(item) == "profile" and item.text and item.text.strip():
                    edge("assigned_to", "profile", item.text.strip(), "profile")
                elif _local(item) == "layout" and item.text and item.text.strip():
                    edge("uses_layout", "layout", item.text.strip(), "layout")
        elif component_kind == "report":
            folder_name = parts[1] if len(parts) > 1 else None
            if folder_name:
                edge("stored_in", "folder", folder_name)
            for item in root.iter():
                tag = _local(item)
                value = item.text.strip() if item.text and item.text.strip() else None
                if not value:
                    continue
                if tag in {"objectName", "entity"} and value in object_names:
                    edge("reports_on", "object", value, tag)
                elif tag in {"field", "detailColumn", "groupingColumn"} and "." in value:
                    edge("selects" if tag == "detailColumn" else "groups" if "group" in tag.lower() else "filters",
                         "field", value, tag)
                elif tag == "filter":
                    edge("uses_filter", "filter", value, tag)
        elif component_kind == "dashboard":
            folder_name = parts[1] if len(parts) > 1 else None
            if folder_name:
                edge("stored_in", "folder", folder_name)
            for item in root.iter():
                if _local(item) in {"report", "reportName", "component"} and item.text and item.text.strip():
                    edge("contains", "report", item.text.strip(), _local(item))
        if component_kind == "report":
            for filter_item in (item for item in root.iter() if _local(item) == "filter"):
                filter_name = _text(filter_item, "column") or _text(filter_item, "field")
                if filter_name:
                    edge("uses_filter", "filter", filter_name, "column")
        if component_kind == "layout":
            for section in (item for item in root.iter() if _local(item) == "layoutSections"):
                section_name = _text(section, "label") or _text(section, "name")
                if section_name:
                    edge("displays_section", "section", section_name, "label")


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _flow_name(source: SourceFile) -> str:
    return Path(source.relative_path).name.removesuffix(".flow-meta.xml")


def _flow_references(
    source: SourceFile,
    flow_id: str,
    object_names: set[str],
    field_names: set[str],
    field_keys: set[str],
    nodes: dict[str, Node],
    edges: dict[tuple[str, str, str], list[Evidence]],
) -> None:
    if source.text is None:
        return
    try:
        root = ET.fromstring(source.text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML in {source.relative_path}: {exc}") from exc
    trigger_object = next(
        (child.text.strip() for start in root.iter() if _local(start) == "start"
         for child in list(start)
         if _local(child) == "object" and child.text and child.text.strip()),
        None,
    )
    variable_objects: dict[str, str] = {}
    for lookup in root.iter():
        if _local(lookup) != "recordLookups":
            continue
        lookup_name = next(
            (child.text.strip() for child in list(lookup)
             if _local(child) == "name" and child.text and child.text.strip()),
            None,
        )
        lookup_object = next(
            (child.text.strip() for child in list(lookup)
             if _local(child) == "object" and child.text and child.text.strip()),
            None,
        )
        if lookup_name and lookup_object:
            variable_objects[lookup_name] = lookup_object

    def evidence(element: ET.Element, value: str) -> Evidence:
        return _xml_evidence(source, _local(element), value)

    def add_node(kind: str, name: str, path: str | None = None) -> str:
        node_id = f"{kind}:{name}"
        nodes.setdefault(node_id, Node(node_id, kind, name, path))
        return node_id

    def add_edge(target: str, kind: str, item: ET.Element, value: str) -> None:
        edges.setdefault((flow_id, target, kind), []).append(evidence(item, value))

    for element in root.iter():
        tag = _local(element)
        value = element.text.strip() if element.text and element.text.strip() else None
        if tag in {"assignments", "decisions", "screens", "paths", "waits"}:
            kind = {
                "assignments": "assignment",
                "decisions": "decision",
                "screens": "screen",
                "paths": "path",
                "waits": "element",
            }[tag]
            name = next((child.text.strip() for child in list(element)
                         if _local(child) == "name" and child.text and child.text.strip()), tag)
            element_id = add_node(kind, f"{_flow_name(source)}.{name}")
            add_edge(element_id, f"has_{kind}", element, name)
            add_edge(element_id, "has_element", element, name)
            continue
        if tag in {"recordCreates", "recordUpdates", "recordDeletes"}:
            object_element = next(
                (child for child in list(element)
                 if _local(child) == "object" and child.text and child.text.strip()),
                None,
            )
            if object_element is not None and object_element.text:
                target = add_node("object", object_element.text.strip())
                edge_kind = {
                    "recordCreates": "creates",
                    "recordUpdates": "updates",
                    "recordDeletes": "deletes",
                }[tag]
                add_edge(target, edge_kind, object_element, object_element.text.strip())
                for field in element.iter():
                    if _local(field) != "field" or not field.text or not field.text.strip():
                        continue
                    field_name = field.text.strip()
                    if f"{object_element.text.strip()}.{field_name}" in field_keys:
                        add_edge(
                            add_node("field", f"{object_element.text.strip()}.{field_name}"),
                            "writes_field",
                            field,
                            field_name,
                        )
            continue
        if value is None:
            continue
        if tag == "object" and value in object_names:
            target = add_node("object", value)
            add_edge(target, "targets", element, value)
            parent = next((candidate for candidate in root.iter()
                           if element in list(candidate)), None)
            if parent is not None and _local(parent) == "start":
                add_edge(target, "triggers_on", element, value)
        elif tag in {"triggerType", "recordTriggerType"}:
            target = add_node("event", value)
            add_edge(target, "triggers_on_event", element, value)
        elif tag == "flowName":
            target = add_node("subflow", value)
            add_edge(target, "calls_subflow", element, value)
        elif tag in {
            "assignToReference", "inputReference", "outputReference", "elementReference", "field"
        }:
            reference = value
            if reference.startswith("$Record.") and trigger_object:
                reference = f"{trigger_object}.{reference.removeprefix('$Record.')}"
            elif "." in reference:
                variable, field_name = reference.split(".", 1)
                if variable in variable_objects:
                    reference = f"{variable_objects[variable]}.{field_name}"
            if "." in reference:
                object_name, field_name = reference.split(".", 1)
                if object_name in object_names and f"{object_name}.{field_name}" in field_keys:
                    add_edge(
                        add_node("field", f"{object_name}.{field_name}"),
                        "writes_field" if tag in {"assignToReference", "outputReference", "field"}
                        else "reads_field",
                        element,
                        value,
                    )

    for action in (element for element in root.iter() if _local(element) == "actionCalls"):
        values = {
            _local(child): child.text.strip()
            for child in list(action)
            if child.text and child.text.strip()
        }
        action_type = values.get("actionType")
        action_name = values.get("name", action_type or "action")
        target_kind = {
            "apex": "apex_class",
            "apexPlugin": "apex_class",
            "emailAlert": "email_alert",
            "emailSimple": "email_template",
            "submitForApproval": "approval_process",
            "customNotification": "custom_notification",
        }.get(action_type)
        if target_kind:
            add_edge(
                add_node(target_kind, action_name),
                "calls" if target_kind == "apex_class" else "uses",
                action,
                action_name,
            )

    for element in root.iter():
        if _local(element) != "subflows":
            continue
        flow_name = next(
            (child.text.strip() for child in list(element)
             if _local(child) == "flowName" and child.text and child.text.strip()),
            None,
        )
        if flow_name:
            add_edge(add_node("subflow", flow_name), "calls_subflow", element, flow_name)


def build_graph(source: str | Path) -> Graph:
    files = scan(source)
    global_value_sets = {
        Path(item.relative_path).name[:-len(".globalValueSet-meta.xml")]: item
        for item in files
        if item.relative_path.startswith("globalValueSets/")
        and item.relative_path.endswith(".globalValueSet-meta.xml")
    }
    object_names = {
        parts[1]
        for item in files
        for parts in [Path(item.relative_path).parts]
        if len(parts) >= 2 and parts[0] == "objects"
    }
    field_names = {
        field_name
        for item in files
        for field_path in [_object_field_path(item)]
        if field_path is not None
        for field_name in [field_path[1]]
    }
    field_keys = {
        f"{object_name}.{field_name}"
        for item in files
        for object_field in [_object_field_path(item)]
        if object_field is not None
        for object_name, field_name in [object_field]
    }
    label_names = {
        value
        for item in files if item.component == "labels" and item.text
        for value in re.findall(r"<fullName>\s*([^<\s]+)", item.text)
    }
    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str, str], list[Evidence]] = {}
    for item in files:
        file_node = _file_node(item)
        nodes[file_node.id] = file_node
        if item.kind == "flow":
            flow_name = _flow_name(item)
            flow_id = f"flow:{flow_name}"
            nodes[flow_id] = Node(flow_id, "flow", flow_name, item.relative_path)
            edges.setdefault((file_node.id, flow_id, "defines"), []).append(
                Evidence(item.relative_path, 1, f"flow: {flow_name}")
            )
            _flow_references(item, flow_id, object_names, field_names, field_keys, nodes, edges)
        object_field = _object_field_path(item)
        object_structural = _object_structural_path(item)
        _metadata_references(item, object_names, field_keys, label_names, nodes, edges)
        if object_structural is not None:
            object_name, component_name, component_kind, edge_kind = object_structural
            object_id = f"object:{object_name}"
            component_id = f"{component_kind}:{object_name}.{component_name}"
            nodes.setdefault(object_id, Node(object_id, "object", object_name))
            nodes[component_id] = Node(
                component_id,
                component_kind,
                f"{object_name}.{component_name}",
                item.relative_path,
                {"object": object_name, "component": component_name},
            )
            edges.setdefault((object_id, component_id, edge_kind), []).append(
                Evidence(item.relative_path, 1, f"{component_kind}: {component_name}")
            )
        if object_field is not None:
            object_name, field_name = object_field
            object_id = f"object:{object_name}"
            field_id = f"field:{object_name}.{field_name}"
            nodes.setdefault(object_id, Node(object_id, "object", object_name))
            nodes[field_id] = Node(
                field_id,
                "field",
                f"{object_name}.{field_name}",
                item.relative_path,
                {"object": object_name, **_field_properties(item, field_name)},
            )
            edges.setdefault((object_id, field_id, "has_field"), []).append(
                Evidence(item.relative_path, 1, f"field definition: {object_name}.{field_name}")
            )
            edges.setdefault((file_node.id, field_id, "defines"), []).append(
                Evidence(item.relative_path, 1, f"field definition: {object_name}.{field_name}")
            )
            for option, option_source in _field_picklist_values(item, global_value_sets):
                option_name = str(option["fullName"])
                option_id = f"picklist_value:{object_name}.{field_name}.{option_name}"
                nodes[option_id] = Node(
                    option_id,
                    "picklist_value",
                    option_name,
                    option_source.relative_path,
                    {"field": f"{object_name}.{field_name}", **option},
                )
                edges.setdefault((field_id, option_id, "has_value"), []).append(
                    _xml_evidence(option_source, "fullName", option_name)
                )
            properties = nodes[field_id].properties
            referenced_object = properties.get("referenceTo")
            field_type = properties.get("type")
            if isinstance(referenced_object, str) and field_type in {"Lookup", "MasterDetail"}:
                target_id = f"object:{referenced_object}"
                nodes.setdefault(target_id, Node(target_id, "object", referenced_object))
                relationship_kind = (
                    "master_detail_to" if field_type == "MasterDetail" else "lookup_to"
                )
                evidence = [_xml_evidence(item, "referenceTo", referenced_object)]
                relationship_name = properties.get("relationshipName")
                if isinstance(relationship_name, str):
                    evidence.append(_xml_evidence(item, "relationshipName", relationship_name))
                edges.setdefault((field_id, target_id, relationship_kind), []).extend(evidence)
                edges.setdefault((target_id, object_id, "parent_of"), []).extend(evidence)
                edges.setdefault((object_id, target_id, "child_of"), []).extend(evidence)
        if item.kind.startswith("apex_"):
            references = parse_apex(item)
        elif object_field is not None or object_structural is not None or item.kind == "flow":
            # Field metadata is modeled structurally above. Generic XML
            # extraction would incorrectly turn component names into references.
            references = ()
        else:
            references = parse_xml_references(item)
        references = tuple(
            reference
            for reference in references
            if not (
                reference.kind in {"xml_reference", "object"}
                and reference.name in field_names
                and reference.name not in object_names
            )
        )
        for reference in references:
            ref_id = f"{reference.kind}:{reference.name}"
            nodes.setdefault(ref_id, Node(ref_id, reference.kind, reference.name))
            edges.setdefault((file_node.id, ref_id, "references"), []).append(reference.evidence)
    return Graph(tuple(nodes.values()), tuple(Edge(a, b, k, tuple(v)) for (a, b, k), v in edges.items()))


def write_graph(graph: Graph, output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for node in graph.nodes:
                handle.write(json.dumps({"type": "node", **node.to_dict()}, sort_keys=True) + "\n")
            for edge in graph.edges:
                handle.write(json.dumps({"type": "edge", **edge.to_dict()}, sort_keys=True) + "\n")
    else:
        path.write_text(json.dumps(graph.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_graph(source: str | Path) -> Graph:
    """Read a graph written by ``write_graph`` in JSON or JSONL format."""
    path = Path(source)
    if not path.is_file():
        raise OSError(f"graph file does not exist: {path}")
    if path.suffix.lower() == ".jsonl":
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        node_records = [record for record in records if record.get("type") == "node"]
        edge_records = [record for record in records if record.get("type") == "edge"]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        node_records = payload.get("nodes", [])
        edge_records = payload.get("edges", [])
    nodes = tuple(Node(**{key: value for key, value in record.items() if key != "type"})
                  for record in node_records)
    edges = tuple(
        Edge(
            source=record["source"],
            target=record["target"],
            kind=record["kind"],
            evidence=tuple(Evidence(**item) for item in record.get("evidence", [])),
        )
        for record in edge_records
    )
    return Graph(nodes, edges)
