from __future__ import annotations

from typing import Any, Dict

from .common import CheckResult, failed, normalized_name, not_evaluable, passed


def evaluate(expected: Any, trace: Dict[str, Any]) -> CheckResult:
    actual = trace.get("resolved_mode") or trace.get("effective_mode")
    if not actual:
        details = next((e.get("details", {}) for e in reversed(trace.get("events") or [])
                        if e.get("stage") == "MODE_RESOLVED"), {})
        actual = details.get("resolved_mode")
    if not actual:
        return not_evaluable("mode", expected, "trace has no effective/resolved mode")
    if normalized_name(actual) == normalized_name(expected):
        return passed("mode", expected, actual)
    return failed("mode", expected, actual, "effective mode differs")
