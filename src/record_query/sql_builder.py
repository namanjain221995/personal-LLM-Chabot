"""DuckDB plan -> parameterised SQL.

Deterministic string assembly. No model, no user text in the statement: every
identifier comes from the physical catalog, which read it out of DuckDB, and
every value leaves as a bound parameter.

Identifiers are quoted because Salesforce names collide with SQL keywords
(`Order`, `Group`, `Case` are all real objects) and because quoting removes any
question of what a name could be made to mean.
"""
from __future__ import annotations

from typing import Any

from .models import DuckDBQueryPlan, QueryError, SQLStatement


class BuildError(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


def quote_identifier(name: str) -> str:
    """Quote for DuckDB. A name containing a quote cannot be built.

    Identifiers only ever come from information_schema, so a rejection here
    means a bug upstream rather than hostile input -- which is exactly why it
    raises instead of escaping and continuing.
    """
    if not name or '"' in name:
        raise BuildError(QueryError.SQL_VALIDATION_FAILED,
                         f"unsafe identifier {name!r}")
    return f'"{name}"'


def _column(alias: str, column: str) -> str:
    if column == "*":
        return f"{quote_identifier(alias)}.*"
    return f"{quote_identifier(alias)}.{quote_identifier(column)}"


def build_sql(plan: DuckDBQueryPlan) -> SQLStatement:
    if plan.primary_table is None:
        raise BuildError(QueryError.INVALID_QUERY_PLAN, "plan has no primary table")
    if not plan.select:
        raise BuildError(QueryError.INVALID_QUERY_PLAN, "plan selects nothing")

    params: list[Any] = []

    select_parts: list[str] = []
    for item in plan.select:
        if item.aggregate == "COUNT" and item.column == "*":
            expression = "COUNT(*)"
        elif item.aggregate:
            expression = f"{item.aggregate}({_column(item.table_alias, item.column)})"
        else:
            expression = _column(item.table_alias, item.column)
        select_parts.append(f"{expression} AS {quote_identifier(item.output_alias)}")

    schema = quote_identifier(plan.schema)
    lines = ["SELECT " + ", ".join(select_parts),
             f"FROM {schema}.{quote_identifier(plan.primary_table.table)} "
             f"AS {quote_identifier(plan.primary_table.alias)}"]

    for join in plan.joins:
        join_type = join.join_type.upper()
        if join_type not in ("LEFT", "INNER", "RIGHT", "FULL"):
            raise BuildError(QueryError.INVALID_QUERY_PLAN,
                             f"unsupported join type {join.join_type!r}")
        lines.append(
            f"{join_type} JOIN {schema}.{quote_identifier(join.right_table)} "
            f"AS {quote_identifier(join.right_table_alias)} ON "
            f"{_column(join.left_table_alias, join.left_column)} = "
            f"{_column(join.right_table_alias, join.right_column)}")

    if plan.filters:
        conditions: list[str] = []
        for clause in plan.filters:
            target = _column(clause.table_alias, clause.column)
            operator = clause.operator
            if operator in ("IS NULL", "IS NOT NULL"):
                conditions.append(f"{target} {operator}")
            elif operator in ("IN", "NOT IN"):
                if not clause.binds:
                    raise BuildError(QueryError.INVALID_QUERY_PLAN,
                                     f"{operator} with no values")
                placeholders = ", ".join("?" for _ in clause.binds)
                conditions.append(f"{target} {operator} ({placeholders})")
                params.extend(clause.binds)
            elif operator == "BETWEEN":
                if len(clause.binds) != 2:
                    raise BuildError(QueryError.INVALID_QUERY_PLAN,
                                     "BETWEEN needs exactly two values")
                conditions.append(f"{target} BETWEEN ? AND ?")
                params.extend(clause.binds)
            elif operator in ("LIKE", "NOT LIKE"):
                # ESCAPE declared so the planner's escaping of % and _ in user
                # text is actually honoured by DuckDB.
                conditions.append(f"{target} {operator} ? ESCAPE '\\'")
                params.extend(clause.binds)
            else:
                conditions.append(f"{target} {operator} ?")
                params.extend(clause.binds)
        lines.append("WHERE " + "\n  AND ".join(conditions))

    if plan.group_by:
        lines.append("GROUP BY " + ", ".join(
            _column(item.table_alias, item.column) for item in plan.group_by))

    if plan.order_by:
        lines.append("ORDER BY " + ", ".join(
            f"{_column(item.table_alias, item.column)}"
            f"{' DESC' if item.descending else ''}" for item in plan.order_by))

    if plan.limit is not None:
        # The limit is an integer from configuration, clamped by the planner;
        # inlining it keeps the statement readable and cannot carry user text.
        lines.append(f"LIMIT {int(plan.limit)}")

    return SQLStatement(sql="\n".join(lines), params=params)
