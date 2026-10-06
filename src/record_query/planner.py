"""Grounded Salesforce plan -> executable DuckDB plan.

Deterministic. Joins come from the runtime schema's relationship rows, never
from a model and never from guessing that `X__c` probably points at `X`. If the
schema does not record the relationship, there is no join -- a wrong join
returns plausible rows, which is worse than none.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import RecordQueryConfig
from .models import (DuckDBQueryPlan, FilterClause, JoinClause, Operation,
                     OrderByClause, QueryError, SelectItem, TableRef)
from .operators import (OperatorError, build_binds, check_aggregation,
                        sql_operator, value_type_name)
from .physical_catalog import MappingNotFound, PhysicalCatalog

log = logging.getLogger(__name__)

# Always selected so a row can be identified and cited, even when the question
# asked only for an aggregate of something else.
IDENTITY_COLUMNS = ("Id",)


class PlanError(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


def _get(plan: Any, key: str, default: Any = None) -> Any:
    if isinstance(plan, dict):
        return plan.get(key, default)
    return getattr(plan, key, default)


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    return {k: v for k, v in getattr(item, "__dict__", {}).items()}


class QueryPlanner:
    def __init__(self, catalog: PhysicalCatalog, schema_service: Any,
                 config: RecordQueryConfig) -> None:
        self.catalog = catalog
        self.schema = schema_service
        self.config = config

    # -- aliases ----------------------------------------------------------
    @staticmethod
    def _alias(index: int) -> str:
        """t0, t1, t2 ... Deterministic, so the same plan yields the same SQL.

        Model-generated aliases would make two runs of one question produce
        different SQL, and an evaluation could not compare them.
        """
        return f"t{index}"

    # -- entry ------------------------------------------------------------
    def build(self, grounded: Any, *, operation: str = "retrieve",
              limit: int | None = None,
              group_by_fields: list[str] | None = None,
              aggregations: list[dict[str, str]] | None = None
              ) -> DuckDBQueryPlan:
        primary_object = _get(grounded, "primary_object")
        if not primary_object:
            raise PlanError(QueryError.GROUNDING_VALIDATION_FAILED,
                            "the grounded plan names no primary object")
        if not _get(grounded, "schema_grounded", False):
            raise PlanError(QueryError.GROUNDING_VALIDATION_FAILED,
                            "the grounded plan is not marked schema_grounded")

        try:
            table = self.catalog.resolve_table(primary_object)
        except MappingNotFound as exc:
            raise PlanError(exc.error, exc.detail) from exc

        try:
            op = Operation(str(operation).lower())
        except ValueError:
            op = Operation.RETRIEVE

        plan = DuckDBQueryPlan(schema=self.catalog.settings.schema, operation=op)
        plan.primary_table = TableRef(primary_object, table, self._alias(0))

        alias_by_object: dict[str, str] = {primary_object: plan.primary_table.alias}
        self._build_joins(grounded, plan, alias_by_object)
        self._build_select(grounded, plan, alias_by_object, op,
                           group_by_fields or [], aggregations or [])
        self._build_filters(grounded, plan, alias_by_object)
        self._apply_limit(plan, op, limit)
        return plan

    # -- joins ------------------------------------------------------------
    def _build_joins(self, grounded: Any, plan: DuckDBQueryPlan,
                     alias_by_object: dict[str, str]) -> None:
        primary_object = plan.primary_table.salesforce_object
        next_index = 1
        for mapping in _get(grounded, "entity_mappings", []) or []:
            item = _as_dict(mapping)
            source_field = item.get("source_field")
            target_object = item.get("target_object")
            if not source_field or not target_object:
                continue                     # the primary entity, not a join
            if target_object in alias_by_object:
                continue

            # Both sides must exist physically before a join is written.
            try:
                right_table = self.catalog.resolve_table(target_object)
                left = self.catalog.resolve_column(primary_object, source_field)
                right = self.catalog.resolve_column(target_object, "Id")
            except MappingNotFound as exc:
                raise PlanError(exc.error, exc.detail) from exc

            alias = self._alias(next_index)
            next_index += 1
            alias_by_object[target_object] = alias
            plan.joins.append(JoinClause(
                join_type=self.config.default_join_type,
                left_table_alias=plan.primary_table.alias,
                left_column=left.duckdb_column,
                right_table=right_table,
                right_table_alias=alias,
                right_column=right.duckdb_column,
                salesforce_object=target_object))

    # -- select -----------------------------------------------------------
    def _build_select(self, grounded: Any, plan: DuckDBQueryPlan,
                      alias_by_object: dict[str, str], operation: Operation,
                      group_by_fields: list[str],
                      aggregations: list[dict[str, str]]) -> None:
        primary_object = plan.primary_table.salesforce_object
        primary_alias = plan.primary_table.alias

        if operation is Operation.COUNT and not group_by_fields:
            plan.select.append(SelectItem(primary_alias, "*", "record_count",
                                          aggregate="COUNT"))
            return

        for field_name in group_by_fields:
            try:
                column = self.catalog.resolve_column(primary_object, field_name)
            except MappingNotFound as exc:
                raise PlanError(exc.error, exc.detail) from exc
            item = SelectItem(primary_alias, column.duckdb_column, field_name)
            plan.group_by.append(item)
            plan.select.append(item)

        for spec in aggregations:
            function = spec.get("function", "count")
            field_name = spec.get("field")
            if field_name:
                try:
                    column = self.catalog.resolve_column(primary_object, field_name)
                except MappingNotFound as exc:
                    raise PlanError(exc.error, exc.detail) from exc
                try:
                    sql_function = check_aggregation(function, column.duckdb_type)
                except OperatorError as exc:
                    raise PlanError(exc.error, exc.detail) from exc
                plan.select.append(SelectItem(
                    primary_alias, column.duckdb_column,
                    spec.get("alias") or f"{function}_{field_name}",
                    aggregate=sql_function))
            else:
                plan.select.append(SelectItem(
                    primary_alias, "*", spec.get("alias") or "record_count",
                    aggregate="COUNT"))

        if plan.group_by or any(s.aggregate for s in plan.select):
            return

        # Plain retrieval: identity first so every row can be cited.
        for column_name in IDENTITY_COLUMNS:
            if self.catalog.field_is_queryable(primary_object, column_name):
                plan.select.append(SelectItem(primary_alias, column_name, column_name))
        if self.catalog.field_is_queryable(primary_object, "Name"):
            plan.select.append(SelectItem(primary_alias, "Name", "Name"))

        seen = {(s.table_alias, s.column) for s in plan.select}
        for mapping in _get(grounded, "requested_attribute_mappings", []) or []:
            item = _as_dict(mapping)
            target_object = item.get("target_object") or primary_object
            target_field = item.get("target_field")
            if not target_field:
                continue
            alias = alias_by_object.get(target_object)
            if alias is None:
                raise PlanError(
                    QueryError.INVALID_QUERY_PLAN,
                    f"{target_object}.{target_field} was requested but no join "
                    f"reaches {target_object}")
            try:
                column = self.catalog.resolve_column(target_object, target_field)
            except MappingNotFound as exc:
                raise PlanError(exc.error, exc.detail) from exc
            if (alias, column.duckdb_column) in seen:
                continue
            seen.add((alias, column.duckdb_column))
            # Output alias from the business words, so the answer reads back in
            # the user's language rather than the org's.
            output = str(item.get("business_attribute") or target_field)
            output = "".join(ch if ch.isalnum() else "_" for ch in output).strip("_")
            plan.select.append(SelectItem(alias, column.duckdb_column,
                                          output or target_field))

    # -- filters ----------------------------------------------------------
    def _build_filters(self, grounded: Any, plan: DuckDBQueryPlan,
                       alias_by_object: dict[str, str]) -> None:
        primary_object = plan.primary_table.salesforce_object
        for mapping in _get(grounded, "filter_mappings", []) or _get(
                grounded, "filters", []) or []:
            item = _as_dict(mapping)
            field_name = item.get("field")
            if not field_name:
                continue
            owner = item.get("object_api_name") or item.get("object") or primary_object
            alias = alias_by_object.get(owner)
            if alias is None:
                raise PlanError(
                    QueryError.INVALID_QUERY_PLAN,
                    f"filter on {owner}.{field_name} has no table in this query")
            try:
                column = self.catalog.resolve_column(owner, field_name)
            except MappingNotFound as exc:
                raise PlanError(exc.error, exc.detail) from exc

            operator = item.get("operator", "equals")
            value = item.get("value")
            try:
                sql_op, _ = sql_operator(operator)
                binds = build_binds(operator, value, column.duckdb_type)
            except OperatorError as exc:
                raise PlanError(exc.error, exc.detail) from exc

            plan.filters.append(FilterClause(
                table_alias=alias, column=column.duckdb_column,
                operator=sql_op, value=value,
                value_type=value_type_name(value), binds=binds))

    # -- limit ------------------------------------------------------------
    def _apply_limit(self, plan: DuckDBQueryPlan, operation: Operation,
                     requested: int | None) -> None:
        # An aggregate returns one row per group; a row cap would silently
        # truncate a COUNT and make the number wrong rather than short.
        if operation is Operation.COUNT and not plan.group_by:
            plan.limit = None
            return
        limit = requested or self.config.result.default_limit
        plan.limit = min(int(limit), self.config.result.max_limit)
