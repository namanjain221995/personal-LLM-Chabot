from __future__ import annotations

from typing import Any, Dict, List

from .common import CheckResult, actual_plan, failed, filters_from_plan, freeze, normalize_filter, not_evaluable, passed


def _normalized(filters: List[Dict[str, Any]]) -> set[str]:
    return {freeze(normalize_filter(item)) for item in filters}


def evaluate(expected: List[Dict[str, Any]], trace: Dict[str, Any]) -> CheckResult:
    plan = actual_plan(trace)
    if plan is None:
        if not expected:
            # No plan is acceptable only when the trace explicitly records a
            # non-query operation; absence alone is not evidence.
            operation = trace.get("operation")
            if operation:
                return passed("filters", expected, [])
        return not_evaluable("filters", expected, "trace has no structured plan")
    actual = filters_from_plan(plan)
    expected_set = _normalized(expected)
    actual_set = _normalized(actual)
    if expected_set == actual_set:
        return passed("filters", expected, actual)
    return failed(
        "filters", expected, actual,
        f"missing={sorted(expected_set - actual_set)} unexpected={sorted(actual_set - expected_set)}",
    )
