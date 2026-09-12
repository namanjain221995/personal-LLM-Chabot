from __future__ import annotations

from typing import Any, Dict, Optional

from .common import CheckResult, event_details, failed, normalized_name, not_evaluable, passed


_SOURCE_ALIASES = {
    "live_salesforce": "live_salesforce_records",
    "live_salesforce_fallback": "live_salesforce_records",
    "salesforce": "live_salesforce_records",
    "local_salesforce_warehouse": "duckdb_snapshot",
    "org_knowledge_base": "org_knowledge_base",
    "preprod_org_knowledge_base": "org_knowledge_base",
}


def _source(value: Any) -> str:
    name = normalized_name(value)
    return _SOURCE_ALIASES.get(name, name)


def _actual(trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    direct = trace.get("provenance")
    if isinstance(direct, dict):
        return direct
    meta = trace.get("meta") or {}
    direct = meta.get("provenance")
    if isinstance(direct, dict):
        return direct
    executed = event_details(trace, "QUERY_EXECUTED")
    if executed:
        return {
            "source": executed.get("source"),
            "environment": executed.get("environment"),
            "freshness": executed.get("freshness") or (
                "live"
                if _source(executed.get("source")) == "live_salesforce_records"
                else None
            ),
        }
    return None


def evaluate(expected_source: Dict[str, Any], expected: Dict[str, Any], trace: Dict[str, Any]) -> CheckResult:
    if expected.get("required") is False and expected.get("must_match_actual_runtime_source") is False:
        return passed("provenance", expected, {"required": False})
    actual = _actual(trace)
    if actual is None:
        return not_evaluable("provenance", expected, "trace has no structured provenance")
    expected_type = _source(expected_source.get("type") or expected.get("expected_source_label"))
    actual_type = _source(actual.get("source") or actual.get("source_label"))
    expected_environment = normalized_name(
        expected.get("expected_environment") or expected_source.get("environment")
    )
    actual_environment = normalized_name(actual.get("environment"))
    problems = []
    if expected_type and actual_type != expected_type:
        problems.append(f"source expected {expected_type!r}, got {actual_type!r}")
    if expected_environment and not actual_environment:
        problems.append("trace does not record the source environment")
    elif expected_environment and actual_environment != expected_environment:
        problems.append(
            f"environment expected {expected_environment!r}, got {actual_environment!r}"
        )
    if expected.get("must_indicate_freshness") and not actual.get("freshness"):
        problems.append("trace does not record freshness")
    if problems:
        return failed("provenance", expected, actual, "; ".join(problems))
    return passed("provenance", expected, actual)
