from __future__ import annotations

from typing import Any, Dict

from .common import CheckResult, actual_plan, failed, freeze, normalize_filter, normalized_name, not_evaluable, passed


_RESULT_MODE_OPERATION = {
    "count": "count",
    "aggregate": "aggregate",
    "comparison": "aggregate",
    "timeline": "aggregate",
    "records": "query",
}


def _language(value: Any) -> str:
    name = normalized_name(value)
    if name in {"soql", "soql_or_multi_query_plan", "multi_query"}:
        return "soql_or_multi_query_plan"
    return name


def _field_list(value: Any) -> list[str]:
    out = []
    for item in value or []:
        if isinstance(item, dict):
            function = normalized_name(item.get("function") or item.get("aggregate"))
            field = normalized_name(item.get("field"))
            out.append(f"{function}:{field}" if function else field)
        else:
            out.append(normalized_name(item))
    return sorted(item for item in out if item)


def canonical(plan: Dict[str, Any], *, actual: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    operation = plan.get("operation")
    if actual and not operation:
        operation = _RESULT_MODE_OPERATION.get(normalized_name(plan.get("result_mode")))
    if operation is not None:
        result["operation"] = normalized_name(operation)
    root = plan.get("root_object") or plan.get("object_api_name")
    if root is not None:
        result["root_object"] = normalized_name(root)
    language = plan.get("query_language")
    if actual and not language and root:
        language = "SOQL_or_multi_query_plan"
    if language is not None:
        result["query_language"] = _language(language)
    if "filters" in plan:
        result["filters"] = sorted(freeze(normalize_filter(item)) for item in plan.get("filters") or [])
    if "filter_logic" in plan:
        result["filter_logic"] = normalized_name(plan.get("filter_logic") or "and")
    if "group_by" in plan:
        result["group_by"] = _field_list(plan.get("group_by"))
    aggregates = plan.get("aggregate_fields")
    if aggregates is None and actual:
        aggregates = plan.get("aggregate_functions")
    if aggregates is not None:
        result["aggregate_fields"] = _field_list(aggregates)
    for key in (
        "requires_record_query", "requires_schema_verification",
        "requires_multi_query", "must_not_execute_record_query",
    ):
        if key in plan:
            result[key] = bool(plan[key])
    return result


def evaluate(expected: Dict[str, Any], trace: Dict[str, Any]) -> CheckResult:
    raw_actual = actual_plan(trace)
    if raw_actual is None:
        return not_evaluable("plan", expected, "trace has no structured plan")
    wanted = canonical(expected)
    found = canonical(raw_actual, actual=True)
    mismatches = {
        key: {"expected": value, "actual": found.get(key)}
        for key, value in wanted.items()
        if found.get(key) != value
    }
    if mismatches:
        return failed("plan", wanted, found, f"structural mismatches: {mismatches}")
    return passed("plan", wanted, found)
