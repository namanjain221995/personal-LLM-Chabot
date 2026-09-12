from __future__ import annotations

from typing import Any, Dict

from .common import CheckResult, event_details, failed, normalized_name, not_evaluable, passed


def evaluate(expected: Any, trace: Dict[str, Any]) -> CheckResult:
    actual = trace.get("intent")
    if isinstance(actual, dict):
        actual = actual.get("name") or actual.get("intent_name")
    if not actual:
        details = event_details(trace, "QUERY_PLAN_CREATED", "INTENT_CLASSIFIED") or {}
        actual = details.get("intent_name")
        structured = details.get("intent")
        if not actual and isinstance(structured, dict):
            actual = structured.get("name") or structured.get("intent_name")
    if not actual:
        return not_evaluable(
            "intent",
            expected,
            "trace records interpreted text but no categorical intent name",
        )
    if normalized_name(actual) == normalized_name(expected):
        return passed("intent", expected, actual)
    return failed("intent", expected, actual, "routed intent differs")
