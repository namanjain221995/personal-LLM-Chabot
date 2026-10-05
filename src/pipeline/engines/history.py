"""Field-change history from the replicated <Object>__History tables (§34).

Salesforce field history, as synced: which field changed, old and new value,
when, and by whom. Only for objects whose history table exists in the
warehouse -- for the rest the answer is "history is not available for X",
never a timeline invented from LastModifiedDate.
"""
from __future__ import annotations

from typing import Any

from record_query.sql_builder import quote_identifier as q

from ..results import TypedResult


class HistoryEngine:
    def __init__(self, records: Any, retriever: Any) -> None:
        self.records, self.retriever = records, retriever

    def run(self, obj: str, record_name: str | None, field: str | None,
            limit: int = 50, period: Any = None) -> TypedResult:
        """`obj` and `field`: what discovery and linking (the main model) chose."""
        table = f"{obj[:-3]}__History" if obj.endswith("__c") else f"{obj}History"
        catalog = self.records.catalog
        if not catalog.object_is_queryable(table):
            return TypedResult(kind="unsupported", source="history",
                               values={"unavailable": f"field history for {obj} is not "
                                                      "replicated to the warehouse"})
        schema = q(catalog.settings.schema)
        where, params = [], []
        if record_name:
            where.append(f'h.{q("ParentId")} IN (SELECT {q("Id")} FROM {schema}.{q(obj)} '
                         f'WHERE {q("Name")} = ?)')
            params.append(record_name)
        if field:
            where.append(f'h.{q("Field")} = ?')
            params.append(field)
        if period is not None:
            # "changes made today": the change time, not the record's.
            if period.start is not None:
                where.append(f'h.{q("CreatedDate")} >= ?')
                params.append(period.start.isoformat())
            if period.end_exclusive is not None:
                where.append(f'h.{q("CreatedDate")} < ?')
                params.append(period.end_exclusive.isoformat())
        sql = (f'SELECT h.{q("Field")} AS field, h.{q("OldValue")} AS old_value, '
               f'h.{q("NewValue")} AS new_value, h.{q("CreatedDate")} AS changed_at, '
               f'h.{q("CreatedById")} AS changed_by_user_id '
               f'FROM {schema}.{q(table)} AS h'
               + (" WHERE " + " AND ".join(where) if where else "")
               + f' ORDER BY h.{q("CreatedDate")} DESC LIMIT {int(limit) + 1}')
        rows = self.records.executor.connection.execute(sql, params).fetchall()
        truncated = len(rows) > limit
        keys = ["field", "old_value", "new_value", "changed_at", "changed_by_user_id"]
        data = [dict(zip(keys, r)) for r in rows[:limit]]
        return TypedResult(kind="history_timeline", source="history", rows=data,
                           values={"object": obj, "history_table": table,
                                   "record": record_name, "field": field,
                                   "period": getattr(period, "expression", None),
                                   "changes_returned": len(data)},
                           returned_count=len(data), truncated=truncated,
                           total_count=None if truncated else len(data),
                           subplans=[{"sql": sql, "rows": len(data)}])
