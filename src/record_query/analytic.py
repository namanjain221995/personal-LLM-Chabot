"""GroundedQuery -> deterministic SQL -> DuckDB -> rows.

The analytical half of the record layer. The original planner answers
"records of one object, some filters"; this one answers the rest -- AVG, SUM,
COUNT DISTINCT, GROUP BY, time buckets, ranking, joins along any parent path,
filters owned by a joined object, field-to-field comparisons, EXISTS and
NOT EXISTS -- from the same verified inputs, with the same rules:

  * every identifier is checked against DuckDB's own catalog, then quoted;
  * every value is a bound parameter, coerced to its column's type;
  * `main` only, never `raw`;
  * no generic LIMIT on an aggregate-only query (it would change the number);
  * one row past a record listing's cap, so truncation is detected.

No model is involved anywhere in this file.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .grounded import GFilter, GRef, GroundedQuery
from .models import QueryError, SQLStatement
from .operators import OperatorError, build_binds, sql_operator
from .physical_catalog import MappingNotFound, PhysicalCatalog
from .sql_builder import quote_identifier as q

log = logging.getLogger(__name__)

GRAIN_SQL = {"day": "day", "week": "week", "month": "month", "quarter": "quarter",
             "year": "year"}
AGGREGATES = {"count": "COUNT", "count_distinct": "COUNT", "sum": "SUM",
              "avg": "AVG", "min": "MIN", "max": "MAX"}
TIME_FORMAT = "%I:%M %p"


class PlanFailure(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error, self.detail = error, detail


@dataclass
class BuiltQuery:
    statement: SQLStatement
    kind: str                         # records | aggregate | grouped
    columns: list[dict[str, Any]] = dataclass_field(default_factory=list)
    limit: int | None = None
    count_statement: SQLStatement | None = None   # total for a truncated list


@dataclass
class Executed:
    success: bool = False
    kind: str = "records"
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)
    columns: list[str] = dataclass_field(default_factory=list)
    row_count: int = 0
    total_count: int | None = None
    truncated: bool = False
    sql: str = ""
    param_count: int = 0
    execution_ms: float = 0.0
    error: QueryError | None = None
    error_detail: str = ""


class AnalyticPlanner:
    def __init__(self, catalog: PhysicalCatalog, *, default_limit: int = 100,
                 max_limit: int = 1000) -> None:
        self.catalog = catalog
        self.default_limit = default_limit
        self.max_limit = max_limit

    # -- identifiers --------------------------------------------------------
    def _table(self, obj: str) -> str:
        try:
            return self.catalog.resolve_table(obj)
        except MappingNotFound as exc:
            raise PlanFailure(QueryError.PHYSICAL_OBJECT_MAPPING_NOT_FOUND, exc.detail) from exc

    def _column(self, obj: str, field: str) -> tuple[str, str]:
        try:
            column = self.catalog.resolve_column(obj, field)
        except MappingNotFound as exc:
            raise PlanFailure(exc.error, exc.detail) from exc
        return column.duckdb_column, column.duckdb_type

    # -- build -------------------------------------------------------------
    def build(self, g: GroundedQuery, extra: list[GFilter] | None = None, *,
              force_aggregate: bool = False) -> BuiltQuery:
        schema = q(self.catalog.settings.schema)
        base_obj = g.base_object
        aliases: dict[str, str] = {g.base: "t0"}
        joins: list[str] = []
        prefix_alias: dict[tuple, str] = {(): "t0"}

        # Parent paths become LEFT JOIN chains; shared prefixes join once.
        for ref, hops in g.paths.items():
            here, key = "t0", ()
            current_obj = base_obj
            for hop in hops:
                if hop.direction != "parent":
                    raise PlanFailure(QueryError.INVALID_QUERY_PLAN,
                                      f"{hop.describe()} is a child hop and cannot be joined")
                key = key + (hop.field,)
                if key not in prefix_alias:
                    alias = f"t{len(prefix_alias)}"
                    prefix_alias[key] = alias
                    column, _ = self._column(current_obj, hop.field)
                    joins.append(f"LEFT JOIN {schema}.{q(self._table(hop.target))} AS {q(alias)}"
                                 f" ON {q(here)}.{q(column)} = {q(alias)}.{q('Id')}")
                here = prefix_alias[key]
                current_obj = hop.target
            aliases[ref] = here

        def col(ref: GRef) -> tuple[str, str]:
            obj = g.objects[ref.entity]
            if ref.entity not in aliases:
                raise PlanFailure(QueryError.INVALID_QUERY_PLAN,
                                  f"{obj} is not joined into this query")
            column, dtype = self._column(obj, ref.field or "Id")
            return f"{q(aliases[ref.entity])}.{q(column)}", dtype

        params: list[Any] = []

        def condition(f: GFilter, alias_col=col) -> str:
            left, dtype = alias_col(f.left)
            if f.kind == "field" and f.right is not None:
                right, _ = alias_col(f.right)
                op, _ = sql_operator(f.operator)
                if f.compare_as == "time":
                    left = f"try_strptime({left}, '{TIME_FORMAT}')"
                    right = f"try_strptime({right}, '{TIME_FORMAT}')"
                text = f"{left} {op} {right}"
            elif f.kind == "null" or f.operator in ("is_null", "is_not_null"):
                text = f"{left} {'IS NOT NULL' if f.operator == 'is_not_null' else 'IS NULL'}"
            else:
                try:
                    op, _ = sql_operator(f.operator)
                    binds = build_binds(f.operator, f.value, dtype)
                except OperatorError as exc:
                    raise PlanFailure(exc.error, exc.detail) from exc
                if op in ("IN", "NOT IN"):
                    text = f"{left} {op} ({', '.join('?' for _ in binds)})"
                elif op == "BETWEEN":
                    text = f"{left} BETWEEN ? AND ?"
                elif op in ("LIKE", "NOT LIKE"):
                    text = f"{left} {op} ? ESCAPE '\\'"
                else:
                    text = f"{left} {op} ?"
                params.extend(binds)
            return f"NOT ({text})" if f.negate else text

        # -- WHERE: groups are ORed, filters inside a group ANDed -------------
        filters = list(g.filters) + list(extra or [])
        groups: dict[int, list[str]] = {}
        for f in filters:
            groups.setdefault(f.group, []).append(condition(f))
        where = []
        if len(groups) == 1:
            where += next(iter(groups.values()))
        elif groups:
            where.append("(" + " OR ".join("(" + " AND ".join(parts) + ")"
                                            for parts in groups.values()) + ")")

        for x in g.existence:
            child_table = self._table(x.child_object)
            child_column, _ = self._column(x.child_object, x.child_field)
            parent_alias = aliases[x.parent_entity]
            inner = [f"{q('c')}.{q(child_column)} = {q(parent_alias)}.{q('Id')}"]

            def child_col(ref: GRef, obj=x.child_object) -> tuple[str, str]:
                column, dtype = self._column(obj, ref.field or "Id")
                return f"{q('c')}.{q(column)}", dtype

            for f in x.filters:
                inner.append(condition(f, child_col))
            where.append(f"{'NOT ' if x.mode == 'not_exists' else ''}EXISTS (SELECT 1 FROM "
                         f"{schema}.{q(child_table)} AS {q('c')} WHERE {' AND '.join(inner)})")

        # -- SELECT --------------------------------------------------------------
        select: list[str] = []
        columns: list[dict[str, Any]] = []
        group_by: list[str] = []
        order: list[str] = []
        aggregate = bool(g.measures) or force_aggregate
        if aggregate:
            for d in g.dimensions:
                expression, dtype = col(d.ref)
                if d.grain:
                    expression = (f"CAST(date_trunc('{GRAIN_SQL[d.grain]}', "
                                  f"CAST({expression} AS TIMESTAMP)) AS DATE)")
                select.append(f"{expression} AS {q(d.alias)}")
                group_by.append(expression)
                columns.append({"name": d.alias, "role": "dimension", "grain": d.grain,
                                "label": d.ref.label or d.ref.field})
            measures = g.measures or []
            if force_aggregate and not measures:
                from .grounded import GMeasure
                measures = [GMeasure("count", None, "record_count")]
            for i, m in enumerate(measures):
                if m.ref is None:
                    expression = "COUNT(*)"
                else:
                    target, dtype = col(m.ref)
                    if m.op in ("sum", "avg") and not _numeric(dtype):
                        raise PlanFailure(QueryError.UNSUPPORTED_AGGREGATION,
                                          f"{m.op.upper()} needs a number, got {dtype}")
                    distinct = "DISTINCT " if m.op == "count_distinct" else ""
                    expression = f"{AGGREGATES[m.op]}({distinct}{target})"
                    if m.op == "avg":
                        expression = f"ROUND({expression}, 2)"
                select.append(f"{expression} AS {q(m.alias)}")
                columns.append({"name": m.alias, "role": "measure", "op": m.op})
            for o in g.ordering:
                if o.measure is not None and o.measure < len(measures):
                    order.append(f"{q(measures[o.measure].alias)}{' DESC' if o.descending else ''}")
                elif o.ref is not None:
                    expression, _ = col(o.ref)
                    order.append(f"{expression}{' DESC' if o.descending else ''}")
            if not order and any(d.grain for d in g.dimensions):
                order = [q(d.alias) for d in g.dimensions if d.grain]
            if not order and g.dimensions and measures:
                order = [f"{q(measures[0].alias)} DESC"]
        else:
            seen: set[str] = set()
            taken: set[str] = set()

            def show(ref: GRef, alias: str) -> None:
                expression, dtype = col(ref)
                if expression in seen:
                    return
                seen.add(expression)
                # Aliases are case-insensitive in DuckDB: "Name" and "name"
                # would collide and one column would overwrite the other.
                base_alias, n = alias, 2
                while alias.lower() in taken:
                    alias, n = f"{base_alias}_{n}", n + 1
                taken.add(alias.lower())
                select.append(f"{expression} AS {q(alias)}")
                columns.append({"name": alias, "role": "attribute",
                                "label": ref.label or ref.field})

            for field in ("Id", "Name"):
                if self.catalog.field_is_queryable(base_obj, field):
                    show(GRef(g.base, field, label=field), field)
            for ref, alias in g.attributes:
                show(ref, alias)
            for f in filters:                     # show what was filtered on
                if f.kind != "null":
                    show(f.left, f.left.field.lower())
                if f.kind == "field" and f.right is not None:
                    show(f.right, f.right.field.lower())
            for o in g.ordering:
                if o.ref is not None:
                    show(o.ref, o.ref.field.lower())
                    expression, _ = col(o.ref)
                    order.append(f"{expression}{' DESC' if o.descending else ''} NULLS LAST")

        # "Top 5 candidates": a ranking of things excludes rows with no thing.
        # A breakdown keeps its empty group; a ranking would crown it.
        if aggregate and g.dimensions and g.ordering and g.limit:
            for d in g.dimensions:
                if not d.grain:
                    expression, _ = col(d.ref)
                    where.append(f"{expression} IS NOT NULL")

        if g.output_mode == "duplicate_groups":
            if not group_by or not g.measures or g.measures[0].op != "count":
                raise PlanFailure(
                    QueryError.INVALID_QUERY_PLAN,
                    "duplicate detection requires key dimensions and a count measure")
            # Missing values are not duplicate business identifiers. This is
            # schema-independent and works for one key or a composite key.
            for d in g.dimensions:
                expression, _ = col(d.ref)
                where.append(f"{expression} IS NOT NULL")

        # -- assemble ----------------------------------------------------------
        lines = [("SELECT DISTINCT " if g.distinct and not aggregate else "SELECT ")
                 + ", ".join(select),
                 f"FROM {schema}.{q(self._table(base_obj))} AS {q('t0')}"]
        lines += joins
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if group_by:
            lines.append("GROUP BY " + ", ".join(group_by))
        if g.output_mode == "duplicate_groups":
            lines.append("HAVING COUNT(*) > ?")
            params.append(max(1, int(g.duplicate_threshold)))
        if order:
            lines.append("ORDER BY " + ", ".join(order))

        limit = None
        if not aggregate or g.dimensions:
            wanted = g.limit or (None if (aggregate and g.dimensions and not g.ordering)
                                 else self.default_limit)
            if wanted:
                limit = min(int(wanted), self.max_limit)
        count_statement = None
        ranking = aggregate and bool(g.dimensions) and bool(g.ordering) and bool(g.limit)
        if limit is not None and ranking:
            # "Top 5" asks for exactly five. No probe row: five of many groups
            # is the complete answer, not a truncated one.
            lines.append(f"LIMIT {limit}")
        elif limit is not None:
            # One row past the cap: the extra row proves more exist.
            lines.append(f"LIMIT {limit + 1}")
            if not aggregate:
                count_lines = [f"SELECT COUNT(*) AS {q('record_count')}",
                               f"FROM {schema}.{q(self._table(base_obj))} AS {q('t0')}"]
                count_lines += joins
                if where:
                    count_lines.append("WHERE " + "\n  AND ".join(where))
                count_statement = SQLStatement(sql="\n".join(count_lines),
                                               params=list(params))
        kind = "grouped" if (aggregate and g.dimensions) else (
            "aggregate" if aggregate else "records")
        return BuiltQuery(SQLStatement(sql="\n".join(lines), params=params), kind,
                          columns, limit, count_statement)


def _numeric(duckdb_type: str) -> bool:
    upper = (duckdb_type or "").upper().split("(")[0]
    return upper in {"BIGINT", "INTEGER", "DOUBLE", "DECIMAL", "FLOAT", "HUGEINT",
                     "SMALLINT", "TINYINT", "REAL", "NUMERIC"}


def execute(service: Any, built: BuiltQuery) -> Executed:
    """Run one built query through the record layer's validator and executor."""
    from .validator import validate_sql
    out = Executed(kind=built.kind, sql=built.statement.sql,
                   param_count=len(built.statement.params))
    check = validate_sql(built.statement)
    if not check:
        out.error, out.error_detail = check.error, check.detail
        return out
    started = time.perf_counter()
    result = service.executor.execute(
        built.statement, max_rows=built.limit or service.config.result.max_limit,
        max_bytes=service.config.result.max_response_bytes)
    out.execution_ms = (time.perf_counter() - started) * 1000
    if not result.success:
        out.error, out.error_detail = result.error, result.error_detail
        return out
    out.success = True
    out.rows, out.columns = result.rows, result.columns
    out.row_count, out.truncated = result.row_count, result.truncated
    if built.kind == "records":
        if not result.truncated:
            out.total_count = result.row_count
        elif built.count_statement is not None:
            counted = service.executor.execute(built.count_statement, max_rows=1,
                                               max_bytes=service.config.result.max_response_bytes)
            if counted.success and counted.rows:
                out.total_count = int(counted.rows[0].get("record_count", 0))
    return out
