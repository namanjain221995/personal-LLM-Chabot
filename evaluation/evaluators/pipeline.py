from __future__ import annotations

from typing import Any, Dict

from ..loader import EvaluationCase
from . import entities, filters, intent, mode, plan, provenance
from .common import CaseResult, CheckResult, not_evaluable


_ORDER = ("mode", "intent", "entities", "filters", "source", "plan", "answer", "provenance")


def evaluate_case(case: EvaluationCase | Dict[str, Any], trace: Dict[str, Any]) -> CaseResult:
    raw = case.raw if isinstance(case, EvaluationCase) else case
    expected = raw["expected"]
    critical = set(raw["grading"]["critical_checks"])
    checks: Dict[str, CheckResult] = {
        "mode": mode.evaluate(expected["mode"], trace),
        "intent": intent.evaluate(expected["intent"], trace),
        "entities": entities.evaluate(expected["entities"], trace),
        "filters": filters.evaluate(expected["filters"], trace),
        "plan": plan.evaluate(expected["plan"], trace),
        "provenance": provenance.evaluate(expected["source"], expected["provenance"], trace),
    }
    # These comparators belong to the next increments. Explicitly failing as
    # not_evaluable prevents a partial evaluator from inflating end-to-end
    # accuracy while still allowing stage-level development now.
    if "source" in critical:
        checks["source"] = not_evaluable(
            "source", expected["source"], "standalone source/retrieval evaluator not implemented"
        )
    if "answer" in critical:
        checks["answer"] = not_evaluable(
            "answer", expected["answer"], "answer/result comparator not implemented"
        )

    ordered = [checks[name] for name in _ORDER if name in critical and name in checks]
    # Preserve any future/custom critical checks as visible gaps.
    for name in sorted(critical - set(_ORDER)):
        ordered.append(not_evaluable(name, None, f"no evaluator registered for {name}"))
    first = next((check.stage for check in ordered if not check.passed), None)
    return CaseResult(
        case_id=str(raw["id"]),
        passed=first is None,
        first_incorrect_stage=first,
        checks=ordered,
    )
