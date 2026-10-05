"""Safety and consistency checks, run before anything executes.

Two layers, and the duplication is deliberate. The builder cannot emit a
mutation -- there is no code path that writes one. The validator assumes that
guarantee will be broken one day by a change nobody connected to safety, and
checks the finished text anyway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import DuckDBQueryPlan, QueryError, SQLStatement
from .physical_catalog import PhysicalCatalog

FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "COPY", "ATTACH", "DETACH", "INSTALL", "LOAD", "EXPORT", "IMPORT",
    "PRAGMA", "SET", "CALL", "GRANT", "REVOKE", "VACUUM", "CHECKPOINT",
)

# Keyword as a whole word, so "Created_Date__c" does not trip "CREATE" and
# "Updated_By__c" does not trip "UPDATE".
_FORBIDDEN = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b",
                        re.IGNORECASE)
_COMMENT = re.compile(r"--|/\*")


@dataclass
class ValidationResult:
    ok: bool
    error: QueryError | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _fail(error: QueryError, detail: str) -> ValidationResult:
    return ValidationResult(False, error, detail)


def validate_grounded_plan(grounded: Any, schema_service: Any) -> ValidationResult:
    """§29: every Salesforce identifier must exist before physical planning."""
    def get(key: str, default: Any = None) -> Any:
        return (grounded.get(key, default) if isinstance(grounded, dict)
                else getattr(grounded, key, default))

    if not get("schema_grounded", False):
        return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                     "plan is not marked schema_grounded")
    primary = get("primary_object")
    if not primary:
        return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                     "plan names no primary object")
    if schema_service.repo.get_object(primary) is None:
        return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                     f"{primary!r} is not in the runtime schema")

    def as_dict(item: Any) -> dict[str, Any]:
        return item if isinstance(item, dict) else dict(getattr(item, "__dict__", {}))

    for mapping in get("entity_mappings", []) or []:
        item = as_dict(mapping)
        target = item.get("target_object")
        source_field = item.get("source_field")
        if target and schema_service.repo.get_object(target) is None:
            return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                         f"related object {target!r} is not in the runtime schema")
        if source_field and schema_service.repo.get_field(primary, source_field) is None:
            return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                         f"{primary}.{source_field} is not in the runtime schema")

    for mapping in get("filter_mappings", []) or []:
        item = as_dict(mapping)
        owner = item.get("object_api_name") or primary
        field = item.get("field")
        if field and schema_service.repo.get_field(owner, field) is None:
            return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                         f"{owner}.{field} is not in the runtime schema")

    for mapping in get("requested_attribute_mappings", []) or []:
        item = as_dict(mapping)
        owner = item.get("target_object") or primary
        field = item.get("target_field")
        if field and schema_service.repo.get_field(owner, field) is None:
            return _fail(QueryError.GROUNDING_VALIDATION_FAILED,
                         f"{owner}.{field} is not in the runtime schema")
    return ValidationResult(True)


def validate_query_plan(plan: DuckDBQueryPlan, catalog: PhysicalCatalog,
                        max_limit: int) -> ValidationResult:
    """§30: every table, column and limit checked against DuckDB itself."""
    if plan.primary_table is None:
        return _fail(QueryError.INVALID_QUERY_PLAN, "no primary table")
    if not catalog.object_is_queryable(plan.primary_table.salesforce_object):
        return _fail(QueryError.DUCKDB_TABLE_NOT_FOUND,
                     f"{plan.primary_table.table!r} is not in DuckDB")
    if not plan.select:
        return _fail(QueryError.INVALID_QUERY_PLAN, "nothing selected")

    aliases = plan.alias_map()
    object_by_alias = {ref.alias: ref.salesforce_object for ref in plan.tables()}

    for join in plan.joins:
        if not catalog.object_is_queryable(join.salesforce_object):
            return _fail(QueryError.DUCKDB_TABLE_NOT_FOUND,
                         f"joined table {join.right_table!r} is not in DuckDB")
        if not catalog.field_is_queryable(
                plan.primary_table.salesforce_object, join.left_column):
            return _fail(QueryError.DUCKDB_COLUMN_NOT_FOUND,
                         f"join column {join.left_column!r} is not in DuckDB")
        if not catalog.field_is_queryable(join.salesforce_object, join.right_column):
            return _fail(QueryError.DUCKDB_COLUMN_NOT_FOUND,
                         f"join column {join.right_column!r} is not in DuckDB")

    for group in (plan.select, plan.group_by):
        for item in group:
            if item.column == "*":
                continue
            if item.table_alias not in aliases:
                return _fail(QueryError.INVALID_QUERY_PLAN,
                             f"alias {item.table_alias!r} is not in this query")
            if not catalog.field_is_queryable(object_by_alias[item.table_alias],
                                              item.column):
                return _fail(QueryError.DUCKDB_COLUMN_NOT_FOUND,
                             f"{item.column!r} is not in DuckDB")

    for clause in plan.filters:
        if clause.table_alias not in aliases:
            return _fail(QueryError.INVALID_QUERY_PLAN,
                         f"filter alias {clause.table_alias!r} is not in this query")
        if not catalog.field_is_queryable(object_by_alias[clause.table_alias],
                                          clause.column):
            return _fail(QueryError.DUCKDB_COLUMN_NOT_FOUND,
                         f"filter column {clause.column!r} is not in DuckDB")

    for item in plan.order_by:
        if item.table_alias not in aliases:
            return _fail(QueryError.INVALID_QUERY_PLAN,
                         f"order-by alias {item.table_alias!r} is not in this query")
        if not catalog.field_is_queryable(object_by_alias[item.table_alias],
                                          item.column):
            return _fail(QueryError.DUCKDB_COLUMN_NOT_FOUND,
                         f"order-by column {item.column!r} is not in DuckDB")

    if plan.limit is not None and plan.limit > max_limit:
        return _fail(QueryError.QUERY_LIMIT_EXCEEDED,
                     f"limit {plan.limit} exceeds the maximum {max_limit}")
    return ValidationResult(True)


def validate_sql(statement: SQLStatement) -> ValidationResult:
    """§17: read-only, single statement, no comments.

    Checked on the finished text even though the builder cannot produce a
    mutation. Defence in depth is the point: this catches the change that
    introduces one, on the day nobody remembers this rule exists.
    """
    sql = statement.sql
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        return _fail(QueryError.SQL_VALIDATION_FAILED,
                     "only SELECT statements may be executed")
    if ";" in stripped.rstrip(";"):
        return _fail(QueryError.SQL_VALIDATION_FAILED,
                     "multiple statements are not allowed")
    if _COMMENT.search(sql):
        return _fail(QueryError.SQL_VALIDATION_FAILED,
                     "comments are not allowed in generated SQL")
    # Identifiers are quoted, so a keyword inside quotes is a table or column
    # name and must not trip the check.
    unquoted = re.sub(r'"[^"]*"', "", sql)
    match = _FORBIDDEN.search(unquoted)
    if match:
        return _fail(QueryError.SQL_VALIDATION_FAILED,
                     f"forbidden keyword {match.group(1).upper()} in generated SQL")
    return ValidationResult(True)
