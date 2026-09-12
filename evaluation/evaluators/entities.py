from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from .common import CheckResult, actual_plan, failed, normalized_set, not_evaluable, passed


def _actual_entities(trace: Dict[str, Any]) -> Tuple[set[str], set[str]] | None:
    entities = trace.get("entities")
    if isinstance(entities, dict):
        return normalized_set(entities.get("objects") or []), normalized_set(entities.get("fields") or [])
    plan = actual_plan(trace)
    if plan is None:
        return None
    objects = [plan.get("root_object") or plan.get("object_api_name")]
    fields: list[Any] = []
    fields.extend(plan.get("select_fields") or plan.get("fields") or [])
    fields.extend(plan.get("relationship_paths") or [])
    fields.extend(plan.get("group_by") or [])
    for item in plan.get("filters") or []:
        if isinstance(item, dict):
            fields.append(item.get("field"))
    for item in plan.get("aggregate_functions") or plan.get("aggregate_fields") or []:
        if isinstance(item, dict):
            fields.append(item.get("field"))
        else:
            fields.append(item)
    return normalized_set(objects), normalized_set(fields)


def _rules(expected: Dict[str, Any], key: str) -> Tuple[set[str], set[str]]:
    block = expected.get(key) or {}
    return normalized_set(block.get("required") or []), normalized_set(block.get("forbidden") or [])


def _violations(required: Iterable[str], forbidden: Iterable[str], actual: set[str]) -> dict:
    return {
        "missing_required": sorted(set(required) - actual),
        "present_forbidden": sorted(set(forbidden) & actual),
    }


def evaluate(expected: Dict[str, Any], trace: Dict[str, Any]) -> CheckResult:
    actual = _actual_entities(trace)
    if actual is None:
        return not_evaluable("entities", expected, "trace has no entity-resolution output or structured plan")
    objects, fields = actual
    required_objects, forbidden_objects = _rules(expected, "objects")
    required_fields, forbidden_fields = _rules(expected, "fields")
    problems = {
        "objects": _violations(required_objects, forbidden_objects, objects),
        "fields": _violations(required_fields, forbidden_fields, fields),
    }
    wire = {"objects": sorted(objects), "fields": sorted(fields)}
    if any(value for group in problems.values() for value in group.values()):
        return failed("entities", expected, wire, str(problems))
    return passed("entities", expected, wire)
