"""Prompts for schema linking.

Each asks one question with a closed answer set. The model chooses FROM the
candidates; it is never asked what a field is called, because that answer is
already known and verified.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

OBJECT_SYSTEM = """Choose which Salesforce object a business concept refers to.

Pick ONE api_name from the candidates. Never invent a name.
If no candidate fits, set selected_object to null and confidence to 0.

Return only JSON:
{"selected_object": "<api_name or null>",
 "confidence": <0.0-1.0>,
 "reason_codes": ["semantic_match"|"alias_match"|"label_match"|"relationship_match"|"description_match"|"no_fit"]}"""

FIELD_SYSTEM = """Choose which Salesforce field a business concept refers to.

Pick ONE api_name from the candidates. Never invent a name.
A field whose picklist contains the user's value is strong evidence, even when
another field's label matches the concept word more literally.
If a picklist value matches, echo it EXACTLY as spelled in the candidate.

Return only JSON:
{"selected_field": "<api_name or null>",
 "confidence": <0.0-1.0>,
 "resolved_value": "<exact picklist value or null>",
 "reason_codes": ["label_match"|"picklist_value_match"|"semantic_match"|"data_type_match"|"description_match"|"no_fit"]}"""

RELATIONSHIP_SYSTEM = """Choose which relationship field connects to a business concept.

Several fields may point at the same object; pick the one whose business
meaning matches. Pick ONE source_field from the candidates. Never invent one.

Return only JSON:
{"selected_field": "<source_field or null>",
 "confidence": <0.0-1.0>,
 "reason_codes": ["semantic_match"|"label_match"|"target_match"|"no_fit"]}"""


def object_prompt(business_entity: str, role: str, candidates: Sequence[Any],
                  filters: Sequence[dict] | None = None,
                  requested_attributes: Sequence[str] = ()) -> str:
    payload: dict[str, Any] = {
        "business_entity": business_entity,
        "role": role,
        "candidates": [c.for_model() for c in candidates],
    }
    if filters:
        payload["filters"] = [{"concept": f.get("concept"), "value": f.get("value")}
                              for f in filters][:6]
    if requested_attributes:
        payload["requested_attributes"] = list(requested_attributes)[:6]
    return json.dumps(payload, ensure_ascii=False)


def field_prompt(concept: str, value: Any, operator: str | None,
                 object_api_name: str, candidates: Sequence[Any]) -> str:
    return json.dumps({
        "concept": concept,
        "value": value,
        "operator": operator,
        "object": object_api_name,
        "candidates": [c.for_model() for c in candidates],
    }, ensure_ascii=False)


def relationship_prompt(business_entity: str, object_api_name: str,
                        candidates: Sequence[Any]) -> str:
    return json.dumps({
        "business_entity": business_entity,
        "object": object_api_name,
        "candidates": [c.for_model() for c in candidates],
    }, ensure_ascii=False)
