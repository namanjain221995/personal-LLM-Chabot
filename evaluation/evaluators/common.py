from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class CheckResult:
    stage: str
    status: str
    expected: Any
    actual: Any
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def wire(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    first_incorrect_stage: Optional[str]
    checks: List[CheckResult]

    def wire(self) -> dict:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "first_incorrect_stage": self.first_incorrect_stage,
            "checks": [check.wire() for check in self.checks],
        }


def passed(stage: str, expected: Any, actual: Any) -> CheckResult:
    return CheckResult(stage, "passed", expected, actual)


def failed(stage: str, expected: Any, actual: Any, message: str) -> CheckResult:
    return CheckResult(stage, "failed", expected, actual, message)


def not_evaluable(stage: str, expected: Any, message: str) -> CheckResult:
    return CheckResult(stage, "not_evaluable", expected, None, message)


def event_details(trace: Dict[str, Any], *stages: str) -> Optional[Dict[str, Any]]:
    wanted = {stage.upper() for stage in stages}
    for event in reversed(trace.get("events") or []):
        if str(event.get("stage") or "").upper() in wanted:
            details = event.get("details")
            return details if isinstance(details, dict) else {}
    return None


def normalized_name(value: Any) -> str:
    return str(value or "").strip().lower()


def normalized_set(values: Iterable[Any]) -> set[str]:
    return {normalized_name(value) for value in values or [] if str(value or "").strip()}


def freeze(value: Any) -> str:
    """Stable representation for typed structural set comparisons."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


_OPERATOR_ALIASES = {
    "=": "equals",
    "==": "equals",
    "eq": "equals",
    "!=": "not_equals",
    "<>": "not_equals",
    "ne": "not_equals",
    ">": "greater_than",
    "gt": "greater_than",
    "<": "less_than",
    "lt": "less_than",
}


def normalize_filter(raw: Dict[str, Any]) -> Dict[str, Any]:
    item: Dict[str, Any] = {}
    if raw.get("field") is not None:
        item["field"] = normalized_name(raw["field"])
    if raw.get("relationship") is not None:
        item["relationship"] = normalized_name(raw["relationship"])
    operator = normalized_name(raw.get("operator"))
    item["operator"] = _OPERATOR_ALIASES.get(operator, operator)
    if "value" in raw:
        value = raw["value"]
        if isinstance(value, str) and value.startswith("$"):
            value = value.upper()
        item["value"] = value
    return item


def filters_from_plan(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    filters = plan.get("filters") or []
    return [item for item in filters if isinstance(item, dict)]


def actual_plan(trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    direct = trace.get("plan") or trace.get("actual_plan")
    if isinstance(direct, dict):
        return direct
    details = event_details(trace, "QUERY_PLAN_CREATED", "PLANNING")
    if details is None:
        return None
    plan = details.get("structured_query_plan") or details.get("plan")
    return plan if isinstance(plan, dict) else None
