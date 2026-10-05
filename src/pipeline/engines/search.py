"""Find a named record when the question does not say what kind it is (§31).

"Who is Naman Jain in our company?" names a person, not an object. The name
may be a User, an employee (Recruiter__c), a Contact and a candidate Account
all at once, and choosing one object for "person" would be a guess. So the
value is looked up in the Name column of every replicated table -- one bound
parameter per table, ~0.3 s over 340 tables -- and every match is reported
with the kind of record it is and a few fields that say what it is.

Contact details (email, phone, address) are never selected: identifying a
record does not need them.
"""
from __future__ import annotations

import re
import time
from typing import Any

from record_query.sql_builder import quote_identifier as q

from ..results import TypedResult

# Fields that say WHAT a record is: title, department, role, status, type.
_DESCRIBING = re.compile(
    r"^(person)?(title|department|designation|team|role|contact_role|type|"
    r"employment_status|candidate_status|current_job_position|status|isactive|"
    r"usertype|active)(__c)?$", re.IGNORECASE)
_EXACT_LIMIT = 5          # records shown per kind of record
_MAX_KINDS = 12


class SearchEngine:
    def __init__(self, records: Any, schema: Any = None) -> None:
        self.records, self.schema = records, schema

    def _label(self, table: str) -> str:
        row = self.schema.repo.get_object(table) if self.schema is not None else None
        return (row or {}).get("label") or table

    def _field_label(self, table: str, field: str) -> str:
        row = self.schema.repo.get_field(table, field) if self.schema is not None else None
        return (row or {}).get("label") or field

    def _readable(self, connection: Any, schema: str, table: str, field: str,
                  value: Any) -> Any:
        """A lookup's Name instead of its record Id ("AI/ML", not "a28Oy0...")."""
        row = self.schema.repo.get_field(table, field) if self.schema is not None else None
        targets = (row or {}).get("reference_to") or []
        if not targets or not isinstance(value, str):
            return value
        target = targets[0]
        if not self.records.catalog.field_is_queryable(target, "Name"):
            return value
        try:
            found = connection.execute(
                f"SELECT {q('Name')} FROM {schema}.{q(self.records.catalog.resolve_table(target))}"
                f" WHERE {q('Id')} = ?", [value]).fetchone()
        except Exception:                               # noqa: BLE001
            return value
        return found[0] if found and found[0] else value

    def run(self, value: str, *, contains: bool = False) -> TypedResult:
        started = time.perf_counter()
        catalog = self.records.catalog
        connection = self.records.executor.connection
        schema = q(catalog.settings.schema)
        text = str(value or "").strip()
        if not text:
            return TypedResult(kind="unsupported", source="record_search",
                               values={"unavailable": "no value to search for"})
        rows: list[dict[str, Any]] = []
        kinds: dict[str, int] = {}
        for table, column in catalog.tables_with_column("Name"):
            if table.endswith("History") or table.endswith("__History"):
                continue
            where = (f"{q(column)} ILIKE ? ESCAPE '\\'" if contains
                     else f"lower({q(column)}) = lower(?)")
            bound = ("%" + text.replace("\\", "\\\\").replace("%", "\\%")
                     .replace("_", "\\_") + "%") if contains else text
            try:
                count = connection.execute(
                    f"SELECT count(*) FROM {schema}.{q(table)} WHERE {where}",
                    [bound]).fetchone()[0]
            except Exception:                           # noqa: BLE001
                continue
            if not count:
                continue
            kinds[table] = count
            describing = [c for c in catalog.columns_of(table) if _DESCRIBING.match(c)][:8]
            select = ", ".join([q(column)] + [q(c) for c in describing])
            found = connection.execute(
                f"SELECT {select} FROM {schema}.{q(table)} WHERE {where} "
                f"LIMIT {_EXACT_LIMIT}", [bound]).fetchall()
            names = [column] + describing
            label = self._label(table)
            for record in found:
                row = {"kind_of_record": label}
                for name, value in zip(names, record):
                    if value in (None, ""):
                        continue
                    row[self._field_label(table, name)] = self._readable(
                        connection, schema, table, name, value)
                rows.append(row)
            if len(kinds) >= _MAX_KINDS:
                break
        elapsed = (time.perf_counter() - started) * 1000
        freshness = None
        try:
            settings = self.records.config.duckdb
            freshness = self.records.freshness.check(connection, settings.schema, settings.path)
        except Exception:                               # noqa: BLE001
            pass
        return TypedResult(
            freshness=freshness,
            kind="records" if rows else "empty", source="record_search", rows=rows,
            values={"searched_value": text,
                    "match": "contains" if contains else "exact name",
                    "kinds_of_record_matched": len(kinds),
                    **{f"matches_in_{self._label(t)}": n for t, n in kinds.items()}},
            returned_count=len(rows), total_count=sum(kinds.values()),
            truncated=sum(kinds.values()) > len(rows),
            subplans=[{"label": "name_search", "sql": "SELECT count(*) ... WHERE "
                       "lower(\"Name\") = lower(?)  -- over every table with a Name",
                       "params": 1, "ms": round(elapsed, 2), "rows": len(rows)}])
