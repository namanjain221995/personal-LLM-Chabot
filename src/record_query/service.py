"""The public interface: grounded plan in, structured result out.

    validate grounding -> physical mapping -> query plan -> plan validation
    -> SQL -> SQL validation -> freshness -> execute -> result

Nothing outside this module touches DuckDB, and nothing outside it builds SQL.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from dataclasses import replace

from . import tracing
from .config import RecordQueryConfig, load_record_query_config
from .executor import DuckDBExecutor, ExecutionError
from .freshness import FreshnessTracker
from .models import Operation, QueryError, QueryResult, SelectItem
from .physical_catalog import PhysicalCatalog
from .planner import PlanError, QueryPlanner
from .sql_builder import BuildError, build_sql
from .validator import validate_grounded_plan, validate_query_plan, validate_sql

log = logging.getLogger(__name__)


class RecordQueryService:
    def __init__(self, schema_service: Any,
                 config: RecordQueryConfig | None = None, *,
                 root: str | Path = "salesforce_knowledge") -> None:
        self.schema = schema_service
        self.config = config or load_record_query_config(root)
        self.executor = DuckDBExecutor(self.config.duckdb)
        self.catalog = PhysicalCatalog(self.executor.connection, self.config.duckdb)
        self.planner = QueryPlanner(self.catalog, schema_service, self.config)
        self.freshness = FreshnessTracker(self.config.freshness)

    def query_records(self, grounded_plan: Any, *, operation: str = "retrieve",
                      limit: int | None = None,
                      group_by_fields: list[str] | None = None,
                      aggregations: list[dict[str, str]] | None = None
                      ) -> QueryResult:
        result = QueryResult()
        started = time.perf_counter()

        grounding = validate_grounded_plan(grounded_plan, self.schema)
        primary = (grounded_plan.get("primary_object") if isinstance(grounded_plan, dict)
                   else getattr(grounded_plan, "primary_object", None))
        result.trace.append(tracing.grounded_validated(grounding, primary))
        if not grounding:
            result.trace.append(tracing.result_created(result))
            return result.fail(grounding.error or QueryError.GROUNDING_VALIDATION_FAILED,
                               grounding.detail)

        mark = time.perf_counter()
        try:
            plan = self.planner.build(grounded_plan, operation=operation, limit=limit,
                                      group_by_fields=group_by_fields,
                                      aggregations=aggregations)
        except PlanError as exc:
            result.trace.append(tracing.result_created(result))
            return result.fail(exc.error, exc.detail)
        elapsed = int((time.perf_counter() - mark) * 1000)
        result.trace.append(tracing.physical_mapping(plan, elapsed))
        result.trace.append(tracing.plan_created(plan, elapsed))

        result.plan = plan
        plan_check = validate_query_plan(plan, self.catalog,
                                         self.config.result.max_limit)
        result.trace.append(tracing.plan_validated(plan_check))
        if not plan_check:
            result.trace.append(tracing.result_created(result))
            return result.fail(plan_check.error or QueryError.INVALID_QUERY_PLAN,
                               plan_check.detail)

        # The statement asks for one row more than the caller may have. The
        # plan keeps the real cap, so the executor returns exactly that many
        # and the extra row is what proves more rows existed. Without the
        # probe row the SQL LIMIT and the fetch cap are equal and truncation
        # could never be detected -- a short answer would be indistinguishable
        # from a complete one.
        row_cap = plan.limit
        try:
            statement = build_sql(_with_probe_row(plan))
        except BuildError as exc:
            result.trace.append(tracing.result_created(result))
            return result.fail(exc.error, exc.detail)
        result.trace.append(tracing.sql_generated(statement))

        sql_check = validate_sql(statement)
        result.trace.append(tracing.sql_validated(sql_check))
        if not sql_check:
            result.trace.append(tracing.result_created(result))
            return result.fail(sql_check.error or QueryError.SQL_VALIDATION_FAILED,
                               sql_check.detail)

        freshness = self.freshness.check(self.executor.connection,
                                         self.config.duckdb.schema,
                                         self.config.duckdb.path)
        result.trace.append(tracing.freshness_checked(freshness))
        if self.freshness.should_reject(freshness):
            result.freshness = freshness
            result.sql = statement.sql
            result.trace.append(tracing.result_created(result))
            return result.fail(
                QueryError.DATA_STALE,
                f"the replica is {freshness.age_minutes} minutes old, beyond the "
                f"{self.config.freshness.reject_after_minutes} minute limit")

        try:
            executed = self.executor.execute(
                statement, max_rows=row_cap or self.config.result.max_limit,
                max_bytes=self.config.result.max_response_bytes)
        except ExecutionError as exc:
            result.trace.append(tracing.result_created(result))
            return result.fail(exc.error, exc.detail)

        self._establish_total(executed, plan, row_cap)
        executed.plan = plan
        executed.trace = result.trace
        executed.freshness = freshness
        executed.sql = statement.sql
        executed.trace.append(tracing.query_completed(executed))
        executed.trace.append(tracing.result_created(executed))
        log.info("record query: %d rows in %.1f ms (total %.1f ms)",
                 executed.row_count, executed.execution_ms,
                 (time.perf_counter() - started) * 1000)
        return executed

    def _establish_total(self, executed: QueryResult, plan: Any,
                         row_cap: int | None) -> None:
        """How many rows matched, not just how many came back.

        Free when nothing was cut: the returned rows are the whole result. When
        rows were cut it costs a second statement, so it is configurable --
        but the alternative is `total_count = None`, and an answer that cannot
        say how many records exist is worse than one extra COUNT.
        """
        if not executed.success:
            return
        if not executed.truncated:
            executed.total_count = executed.row_count
            return
        if not self.config.result.count_total_when_truncated:
            return
        counting = replace(plan, operation=Operation.COUNT, limit=None,
                           select=[SelectItem(plan.primary_table.alias, "*",
                                              "record_count", aggregate="COUNT")],
                           group_by=[], order_by=[])
        try:
            statement = build_sql(counting)
            counted = self.executor.execute(statement, max_rows=1,
                                            max_bytes=self.config.result.max_response_bytes)
        except (BuildError, ExecutionError):
            log.warning("could not establish the total row count", exc_info=True)
            return
        if counted.success and counted.rows:
            executed.total_count = int(counted.rows[0].get("record_count", 0))

    def schema_drift(self, object_api_name: str) -> dict[str, Any]:
        return self.catalog.compare_with_runtime_schema(self.schema, object_api_name)

    def stats(self) -> dict[str, Any]:
        return {"catalog": self.catalog.stats(),
                "duckdb_path": self.config.duckdb.path,
                "schema": self.config.duckdb.schema}

    def close(self) -> None:
        self.executor.close()

    def __enter__(self) -> "RecordQueryService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def query_records(grounded_plan: Any, schema_service: Any, **kwargs: Any) -> QueryResult:
    with RecordQueryService(schema_service) as service:
        return service.query_records(grounded_plan, **kwargs)


def _with_probe_row(plan: Any) -> Any:
    """The same plan, asking for one row more than the caller wants."""
    if plan.limit is None:
        return plan
    return replace(plan, limit=plan.limit + 1)
