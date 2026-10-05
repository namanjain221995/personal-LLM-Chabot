"""Trace events for the record-query layer.

Emitted, not persisted: this package has no trace store and does not know
where the caller keeps one. Shapes match query_trace_events so all pipelines
land in one timeline.

SQL text is traced; bound parameters are counted, never written. The text is
built from catalog identifiers and is safe; the parameters are the user's own
values and may name a person.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

COMPONENT_VERSION = "record-query-v1"

GROUNDED_PLAN_VALIDATED = "GROUNDED_PLAN_VALIDATED"
PHYSICAL_MAPPING_COMPLETED = "PHYSICAL_MAPPING_COMPLETED"
DUCKDB_QUERY_PLAN_CREATED = "DUCKDB_QUERY_PLAN_CREATED"
DUCKDB_QUERY_PLAN_VALIDATED = "DUCKDB_QUERY_PLAN_VALIDATED"
SQL_GENERATED = "SQL_GENERATED"
SQL_VALIDATED = "SQL_VALIDATED"
DATA_FRESHNESS_CHECKED = "DATA_FRESHNESS_CHECKED"
DUCKDB_QUERY_STARTED = "DUCKDB_QUERY_STARTED"
DUCKDB_QUERY_COMPLETED = "DUCKDB_QUERY_COMPLETED"
DUCKDB_QUERY_FAILED = "DUCKDB_QUERY_FAILED"
QUERY_RESULT_CREATED = "QUERY_RESULT_CREATED"


@dataclass
class TraceEvent:
    stage: str
    status: str = "success"
    component: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    duration_ms: int | None = None
    component_version: str = COMPONENT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status,
                "component": self.component, "details": self.details,
                "duration_ms": self.duration_ms,
                "component_version": self.component_version}


def grounded_validated(result: Any, primary_object: str | None) -> TraceEvent:
    return TraceEvent(
        stage=GROUNDED_PLAN_VALIDATED,
        status="success" if result else "failed",
        component="record_query.validator.validate_grounded_plan",
        details={"primary_object": primary_object, "ok": bool(result),
                 "detail": getattr(result, "detail", "")})


def physical_mapping(plan: Any, duration_ms: int) -> TraceEvent:
    return TraceEvent(
        stage=PHYSICAL_MAPPING_COMPLETED,
        component="record_query.physical_catalog",
        duration_ms=duration_ms,
        details={"primary_table": plan.primary_table.table if plan.primary_table else None,
                 "tables": [ref.table for ref in plan.tables()]})


def plan_created(plan: Any, duration_ms: int) -> TraceEvent:
    return TraceEvent(
        stage=DUCKDB_QUERY_PLAN_CREATED,
        component="record_query.planner.build",
        duration_ms=duration_ms,
        details={"operation": plan.operation.value,
                 "primary_table": plan.primary_table.table if plan.primary_table else None,
                 "select": [f"{s.table_alias}.{s.column}" for s in plan.select],
                 "joins": [f"{j.join_type} {j.right_table} ON "
                           f"{j.left_table_alias}.{j.left_column}="
                           f"{j.right_table_alias}.{j.right_column}"
                           for j in plan.joins],
                 "filters": [f"{f.table_alias}.{f.column} {f.operator}"
                             for f in plan.filters],
                 "group_by": [g.column for g in plan.group_by],
                 "limit": plan.limit})


def plan_validated(result: Any) -> TraceEvent:
    return TraceEvent(
        stage=DUCKDB_QUERY_PLAN_VALIDATED,
        status="success" if result else "failed",
        component="record_query.validator.validate_query_plan",
        details={"ok": bool(result), "detail": getattr(result, "detail", "")})


def sql_generated(statement: Any) -> TraceEvent:
    return TraceEvent(
        stage=SQL_GENERATED, component="record_query.sql_builder.build_sql",
        details={"sql": statement.sql, "param_count": len(statement.params)})


def sql_validated(result: Any) -> TraceEvent:
    return TraceEvent(
        stage=SQL_VALIDATED, status="success" if result else "failed",
        component="record_query.validator.validate_sql",
        details={"ok": bool(result), "detail": getattr(result, "detail", "")})


def freshness_checked(freshness: Any) -> TraceEvent:
    return TraceEvent(
        stage=DATA_FRESHNESS_CHECKED,
        status="success" if getattr(freshness, "status", "") == "fresh" else "info",
        component="record_query.freshness.check",
        details=freshness.as_dict() if freshness else {})


def query_completed(result: Any) -> TraceEvent:
    stage = DUCKDB_QUERY_COMPLETED if result.success else DUCKDB_QUERY_FAILED
    return TraceEvent(
        stage=stage, status="success" if result.success else "failed",
        component="record_query.executor.execute",
        duration_ms=int(result.execution_ms),
        details={"row_count": result.row_count, "truncated": result.truncated,
                 "columns": result.columns,
                 "error": result.error.value if result.error else None,
                 "error_detail": result.error_detail})


def result_created(result: Any) -> TraceEvent:
    return TraceEvent(
        stage=QUERY_RESULT_CREATED,
        status="success" if result.success else "failed",
        component="record_query.service.query_records",
        details={"row_count": result.row_count,
                 "truncated": result.truncated,
                 "freshness": result.freshness.status if result.freshness else None})
