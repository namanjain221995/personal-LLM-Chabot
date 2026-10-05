"""Records the record-query layer passes between its stages.

Two plans, deliberately separate. The grounded plan is database-neutral and
speaks Salesforce; the DuckDB plan speaks tables, aliases and columns. Keeping
them apart is what lets the storage target change without touching the layer
that decided what the question meant.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class QueryError(str, Enum):
    """Explicit failure states. A raw exception never reaches the user."""

    GROUNDING_VALIDATION_FAILED = "GROUNDING_VALIDATION_FAILED"
    PHYSICAL_OBJECT_MAPPING_NOT_FOUND = "PHYSICAL_OBJECT_MAPPING_NOT_FOUND"
    PHYSICAL_FIELD_MAPPING_NOT_FOUND = "PHYSICAL_FIELD_MAPPING_NOT_FOUND"
    DUCKDB_TABLE_NOT_FOUND = "DUCKDB_TABLE_NOT_FOUND"
    DUCKDB_COLUMN_NOT_FOUND = "DUCKDB_COLUMN_NOT_FOUND"
    INVALID_QUERY_PLAN = "INVALID_QUERY_PLAN"
    UNSUPPORTED_OPERATOR = "UNSUPPORTED_OPERATOR"
    UNSUPPORTED_AGGREGATION = "UNSUPPORTED_AGGREGATION"
    SQL_VALIDATION_FAILED = "SQL_VALIDATION_FAILED"
    QUERY_LIMIT_EXCEEDED = "QUERY_LIMIT_EXCEEDED"
    DUCKDB_EXECUTION_FAILED = "DUCKDB_EXECUTION_FAILED"
    DATA_NOT_SYNCED = "DATA_NOT_SYNCED"
    DATA_STALE = "DATA_STALE"
    SCHEMA_DATA_MISMATCH = "SCHEMA_DATA_MISMATCH"


class Operation(str, Enum):
    RETRIEVE = "retrieve"
    SEARCH = "search"
    COUNT = "count"
    AGGREGATE = "aggregate"
    COMPARE = "compare"


@dataclass
class TableRef:
    salesforce_object: str
    table: str
    alias: str


@dataclass
class SelectItem:
    table_alias: str
    column: str
    output_alias: str
    # Set for COUNT/SUM/AVG/MIN/MAX; None for a plain column.
    aggregate: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class JoinClause:
    join_type: str
    left_table_alias: str
    left_column: str
    right_table: str
    right_table_alias: str
    right_column: str
    salesforce_object: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class FilterClause:
    table_alias: str
    column: str
    operator: str          # a DuckDB operator, already mapped
    value: Any
    value_type: str = "string"
    # IS NULL / IS NOT NULL bind nothing; BETWEEN and IN bind several.
    binds: list[Any] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "binds"}


@dataclass
class OrderByClause:
    table_alias: str
    column: str
    descending: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DuckDBQueryPlan:
    """The executable physical plan. Validated before any SQL is built."""

    database: str = "duckdb"
    schema: str = "main"
    operation: Operation = Operation.RETRIEVE
    primary_table: TableRef | None = None
    select: list[SelectItem] = dataclass_field(default_factory=list)
    joins: list[JoinClause] = dataclass_field(default_factory=list)
    filters: list[FilterClause] = dataclass_field(default_factory=list)
    group_by: list[SelectItem] = dataclass_field(default_factory=list)
    order_by: list[OrderByClause] = dataclass_field(default_factory=list)
    limit: int | None = 100

    def tables(self) -> list[TableRef]:
        refs = [self.primary_table] if self.primary_table else []
        refs += [TableRef(j.salesforce_object, j.right_table, j.right_table_alias)
                 for j in self.joins]
        return refs

    def alias_map(self) -> dict[str, str]:
        return {ref.alias: ref.table for ref in self.tables()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "schema": self.schema,
            "operation": self.operation.value,
            "primary_table": (dict(self.primary_table.__dict__)
                              if self.primary_table else None),
            "select": [s.as_dict() for s in self.select],
            "joins": [j.as_dict() for j in self.joins],
            "filters": [f.as_dict() for f in self.filters],
            "group_by": [g.as_dict() for g in self.group_by],
            "order_by": [o.as_dict() for o in self.order_by],
            "limit": self.limit,
        }


@dataclass
class SQLStatement:
    sql: str
    params: list[Any] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # Parameters are user values and may name a person. The SQL text is
        # safe to log; the bindings are counted, not printed.
        return {"sql": self.sql, "param_count": len(self.params)}


@dataclass
class Freshness:
    last_sync_at: str | None = None
    age_minutes: float | None = None
    status: str = "unknown"        # fresh | stale | very_stale | unknown
    expected_sync_minutes: int = 30

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class QueryResult:
    success: bool = False
    database: str = "duckdb"
    columns: list[str] = dataclass_field(default_factory=list)
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)
    row_count: int = 0
    # How many rows matched, as opposed to how many were returned. None means
    # "not established" -- which is a different answer from a number, and the
    # answer layer is required to keep them apart (a truncated result must not
    # be reported as "there are 100 records").
    total_count: int | None = None
    truncated: bool = False
    execution_ms: float = 0.0
    freshness: Freshness | None = None
    error: QueryError | None = None
    error_detail: str = ""
    sql: str = ""
    # The physical plan that produced these rows. Carried so a later stage can
    # tell a COUNT from a one-row SELECT without re-deriving it -- the
    # difference decides how the result is classified and therefore how it is
    # described. Internal: deliberately not in as_dict(), which is the
    # caller-facing shape.
    plan: Any = None
    trace: list[Any] = dataclass_field(default_factory=list)

    def fail(self, error: QueryError, detail: str) -> "QueryResult":
        self.success = False
        self.error = error
        self.error_detail = detail
        return self

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "database": self.database,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "total_count": self.total_count,
            "truncated": self.truncated,
            "execution_ms": round(self.execution_ms, 2),
        }
        if self.freshness:
            out["data_freshness"] = self.freshness.as_dict()
        if self.error:
            out["error"] = self.error.value
            out["error_detail"] = self.error_detail
        return out
