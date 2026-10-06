"""Record questions: one grounded query, decomposed into deterministic subplans.

    comparison   -> one aggregate per segment      -> difference, change %
    percentage   -> numerator + denominator counts -> percentage
    exists       -> one count                      -> yes / no
    otherwise    -> one query: records, a value, groups, a ranking, a trend

Each subplan is ordinary SQL from the analytical planner; every figure the
answer states is computed here, never by the model (§23, §24).
"""
from __future__ import annotations

import time
from typing import Any

from record_query.analytic import AnalyticPlanner, PlanFailure, execute
from record_query.grounded import GroundedQuery

from ..results import TypedResult, compare, percentage, ratio, trend


class RecordEngine:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.planner = AnalyticPlanner(service.catalog,
                                       default_limit=service.config.result.default_limit,
                                       max_limit=service.config.result.max_limit)

    def _freshness(self) -> Any:
        try:
            return self.service.freshness.check(self.service.executor.connection,
                                                self.service.config.duckdb.schema,
                                                self.service.config.duckdb.path)
        except Exception:                               # noqa: BLE001
            return None

    def _scalar(self, g: GroundedQuery, extra: list[Any], label: str,
                result: TypedResult) -> float | None:
        built = self.planner.build(g, extra, force_aggregate=True)
        executed = execute(self.service, built)
        result.subplans.append({"label": label, "sql": executed.sql,
                                "params": executed.param_count,
                                "ms": round(executed.execution_ms, 2),
                                "rows": executed.row_count})
        if not executed.success:
            result.error, result.error_detail = (executed.error.value if executed.error
                                                 else "QUERY_EXECUTION_FAILED",
                                                 executed.error_detail)
            return None
        row = executed.rows[0] if executed.rows else {}
        value = next(iter(row.values()), 0) if row else 0
        return float(value or 0)

    def run(self, g: GroundedQuery) -> TypedResult:
        label = self._label(g)
        try:
            result = self._run(g, label)
        except PlanFailure as exc:
            result = TypedResult(kind="unsupported", entity_label=label,
                                 error=exc.error.value, error_detail=exc.detail)
        result.entity_label = label
        result.freshness = self._freshness()
        return result

    def _label(self, g: GroundedQuery) -> str:
        """The base object's plural label, in the user's words ("Interviews")."""
        schema = getattr(self.service, "schema", None)
        row = schema.repo.get_object(g.base_object) if schema is not None else None
        return (row or {}).get("plural_label") or (row or {}).get("label") or g.base_object

    def _run(self, g: GroundedQuery, label: str) -> TypedResult:
        # An output mode whose defining part never arrived must not quietly
        # become a record listing that answers a different question.
        if g.output_mode in ("percentage", "ratio") and not g.numerator:
            return TypedResult(kind="unsupported", error="UNSUPPORTED_DERIVED_METRIC",
                               error_detail="a percentage was asked with no condition "
                                            "defining the part")
        if g.output_mode == "comparison" and len(g.segments) < 2:
            return TypedResult(kind="unsupported", error="UNSUPPORTED_DERIVED_METRIC",
                               error_detail="a comparison was asked with fewer than two sides")
        if g.output_mode == "duplicate_groups" and not g.dimensions:
            return TypedResult(kind="unsupported", error="INVALID_QUERY_PLAN",
                               error_detail="duplicate detection needs at least one key field")
        # -- comparison: one aggregate per segment ---------------------------
        if g.segments:
            result = TypedResult(kind="comparison")
            points = []
            for segment in g.segments:
                value = self._scalar(g, segment.filters, segment.label, result)
                if value is None:
                    return result
                points.append((segment.label, value))
            result.rows = [{"segment": s, "value": v} for s, v in points]
            result.derived = compare(points)
            result.returned_count = len(points)
            return result

        # -- percentage / ratio: numerator and denominator ---------------------
        if g.derived in ("percentage", "ratio") or g.numerator:
            result = TypedResult(kind="percentage")
            numerator = self._scalar(g, g.numerator, "numerator", result)
            denominator = self._scalar(g, g.denominator, "denominator", result)
            if numerator is None or denominator is None:
                return result
            facts = (ratio if g.derived == "ratio" else percentage)(numerator, denominator)
            result.values = facts
            result.returned_count = 1
            return result

        # -- exists: a count that becomes yes/no -----------------------------------
        if g.output_mode == "exists" and not g.measures:
            result = TypedResult(kind="exists")
            count = self._scalar(g, [], "exists", result)
            if count is None:
                return result
            result.values = {"exists": "Yes" if count > 0 else "No",
                             "matching_record_count": int(count)}
            result.returned_count = 1
            return result

        # -- one query -------------------------------------------------------------
        built = self.planner.build(g)
        executed = execute(self.service, built)
        result = TypedResult(kind=built.kind)
        result.subplans.append({"label": "main", "sql": executed.sql,
                                "params": executed.param_count,
                                "ms": round(executed.execution_ms, 2),
                                "rows": executed.row_count})
        if not executed.success:
            result.kind = "unsupported"
            result.error = executed.error.value if executed.error else "QUERY_EXECUTION_FAILED"
            result.error_detail = executed.error_detail
            return result
        result.rows = executed.rows
        result.returned_count = executed.row_count
        result.total_count = executed.total_count
        result.truncated = executed.truncated

        if built.kind == "aggregate":
            row = executed.rows[0] if executed.rows else {}
            only_count = len(g.measures) <= 1 and (not g.measures or g.measures[0].op == "count")
            if only_count:
                result.kind = "count"
                result.values = {"matching_record_count": int(next(iter(row.values()), 0) or 0)}
                result.total_count = result.values["matching_record_count"]
            else:
                result.values = dict(row)
                if row and all(v is None for v in row.values()):
                    # MIN/MAX/AVG/SUM over nothing: say why there is no figure.
                    result.values["records_with_a_value"] = 0
            result.rows = []
        elif built.kind == "grouped":
            grain = next((d.grain for d in g.dimensions if d.grain), None)
            measure = g.measures[0].alias if g.measures else None
            if grain:
                # Buckets named for their grain: "2026-Q2", not "April 1, 2026",
                # which reads as a day and misleads the answer.
                alias = next(d.alias for d in g.dimensions if d.grain)
                for row in executed.rows:
                    row[alias] = _bucket(row.get(alias), grain)
            if g.output_mode == "duplicate_groups":
                result.kind = "duplicate_groups"
            elif grain and measure:
                result.kind = "trend"
                result.derived = trend([(str(r.get(g.dimensions[0].alias)), float(r.get(measure) or 0))
                                        for r in executed.rows])
            elif g.ordering and g.limit:
                result.kind = "ranking"
            else:
                result.kind = "grouped"
            if measure:
                result.values["group_count"] = executed.row_count
                if not executed.truncated:
                    result.values["total_across_groups"] = sum(
                        float(r.get(measure) or 0) for r in executed.rows)
        return result


def _bucket(value: Any, grain: str) -> Any:
    if not hasattr(value, "year"):
        return value
    if grain == "year":
        return f"{value.year}"
    if grain == "quarter":
        return f"{value.year}-Q{(value.month - 1) // 3 + 1}"
    if grain == "month":
        return f"{value.year}-{value.month:02d}"
    if grain == "week":
        return f"week of {value.isoformat()[:10]}"
    return value.isoformat()[:10]
