"""Questions about the org's STRUCTURE, answered from the runtime schema (§31).

"Which fields are required on Interview__c?", "What values are valid for
Interview_Status__c?", "Which object stores internal interviews?" -- the main
model chooses the object and field (discovery, linking); this engine reads the
local runtime schema for them. No org call.
"""
from __future__ import annotations

from typing import Any

from ..results import TypedResult

PLATFORM = "platform_standard"


class SchemaEngine:
    """Looks up what the main model resolved; resolves nothing itself.

    Which object and which field a schema question means is decided by the
    discovery and linking stages (the main model, by candidate ID). This
    engine receives those verified choices and reads the runtime schema.
    """

    def __init__(self, schema: Any, retriever: Any, graph: Any) -> None:
        self.schema, self.retriever, self.graph = schema, retriever, graph

    def run(self, asks: list[Any], resolved: list[dict[str, Any]]) -> TypedResult:
        rows: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        for ask, choice in zip(asks, resolved):
            obj = choice.get("object")
            if ask.kind == "object_for_concept":
                if obj:
                    best = self.schema.repo.get_object(obj) or {}
                    values.update({"best_match_object": obj,
                                   "best_match_label": best.get("label")})
                for name in [obj] if obj else []:
                    row = self.schema.repo.get_object(name) or {}
                    rows.append({"object": name, "label": row.get("label"),
                                 "plural_label": row.get("plural_label")})
                continue
            if obj is None:
                rows.append({"question_part": ask.kind,
                             "finding": f"no object matched {ask.object_concept!r}"})
                continue
            fields = self.schema.repo.get_fields(obj)
            if ask.kind in ("fields_of_object", "describe_object"):
                row = self.schema.repo.get_object(obj) or {}
                values.update({"object": obj, "label": row.get("label"),
                               "field_count": len(fields),
                               "custom_field_count": sum(1 for f in fields
                                                         if f["api_name"].endswith("__c"))})
                if ask.kind == "fields_of_object":
                    for f in fields[:80]:
                        rows.append({"field": f["api_name"], "label": f.get("label"),
                                     "type": f.get("data_type")})
            elif ask.kind == "required_fields":
                required = [f for f in fields if not f.get("is_nullable")
                            and f.get("source") != PLATFORM]
                values.update({"object": obj, "required_field_count": len(required),
                               "scope": "fields marked required in the field metadata; "
                                        "requirements set by page layouts or validation "
                                        "rules are not part of the runtime schema"})
                for f in required:
                    rows.append({"field": f["api_name"], "label": f.get("label"),
                                 "type": f.get("data_type")})
            elif ask.kind in ("field_for_concept", "field_datatype", "picklist_values",
                              "lookup_target", "standard_or_custom"):
                field, options = choice.get("field"), choice.get("field_options") or []
                if field is None:
                    rows.append({"object": obj, "finding":
                                 f"no field on {obj} matched {ask.field_concept!r}"})
                    continue
                row = self.schema.repo.get_field(obj, field) or {}
                fact = {"object": obj, "field": field, "label": row.get("label"),
                        "type": row.get("data_type")}
                if ask.kind == "field_for_concept" and len(options) > 1:
                    fact["other_candidates"] = ", ".join(options[1:4])
                if ask.kind == "picklist_values":
                    picks = [p["value"] for p in self.schema.repo.get_picklist_values(obj, field)
                             if p.get("is_active", 1)]
                    fact["value_count"] = len(picks)
                    rows.append(fact)
                    for value in picks:
                        rows.append({"field": field, "picklist_value": value})
                    continue
                if ask.kind == "lookup_target":
                    fact["references"] = ", ".join(row.get("reference_to") or []) or "none"
                if ask.kind == "standard_or_custom":
                    fact["kind"] = ("custom" if field.endswith("__c") else "standard")
                rows.append(fact)
            elif ask.kind == "objects_referencing":
                for rel in self.schema.repo.get_child_relationships(obj):
                    rows.append({"object": rel.get("child_object"),
                                 "lookup_field": rel.get("child_field"), "references": obj})
                values["referencing_object_count"] = len(rows)
            elif ask.kind == "record_types":
                for rt in self.schema.repo.get_record_types(obj):
                    rows.append({"object": obj, "record_type": rt.get("developer_name"),
                                 "label": rt.get("name"), "active": bool(rt.get("is_active"))})
                values["record_type_count"] = sum(1 for r in rows if r.get("record_type"))
            elif ask.kind == "relationship_between":
                other = choice.get("other")
                if other is None:
                    rows.append({"finding": f"no object matched {ask.other_object_concept!r}"})
                    continue
                for path in self.graph.paths(obj, other, max_hops=2)[:3]:
                    rows.append({"from": obj, "to": other, "path": path.describe()})
                if not any(r.get("path") for r in rows):
                    rows.append({"from": obj, "to": other,
                                 "finding": "no relationship within two hops"})
        return TypedResult(kind="schema_facts", source="runtime_schema", rows=rows,
                           values=values, returned_count=len(rows), total_count=len(rows))
