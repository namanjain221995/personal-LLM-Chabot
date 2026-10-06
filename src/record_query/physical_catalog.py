"""Salesforce identifiers -> DuckDB tables and columns.

The one place that knows where records physically live. Everything upstream
speaks Salesforce; everything downstream speaks DuckDB; this translates, and
nothing else is allowed to assume a table name.

In this warehouse the mapping is identity -- the sync writes tables named after
the object and columns named after the field -- but the layer exists anyway,
because a storage change would otherwise mean editing the planner, the builder
and the validator instead of one table.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from .config import DuckDBSettings
from .models import QueryError

log = logging.getLogger(__name__)


@dataclass
class PhysicalObject:
    salesforce_object: str
    duckdb_table: str
    enabled: bool = True
    row_count: int | None = None


@dataclass
class PhysicalField:
    salesforce_object: str
    salesforce_field: str
    duckdb_table: str
    duckdb_column: str
    duckdb_type: str


class MappingNotFound(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


class PhysicalCatalog:
    """What DuckDB actually contains, read from DuckDB itself.

    Introspected rather than declared: a hand-maintained mapping file would
    drift from the warehouse the first time the sync added a column, and the
    drift would only surface as a failing query.
    """

    def __init__(self, connection: Any, settings: DuckDBSettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._tables: dict[str, PhysicalObject] = {}
        self._columns: dict[str, dict[str, PhysicalField]] = {}
        self._load(connection)

    def _load(self, connection: Any) -> None:
        schema = self.settings.schema
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = ?", [schema]).fetchall()
        with self._lock:
            self._tables = {r[0].lower(): PhysicalObject(r[0], r[0])
                            for r in rows}
            self._columns = {}
            for table, column, data_type in connection.execute(
                    "SELECT table_name, column_name, data_type"
                    " FROM information_schema.columns"
                    " WHERE table_schema = ?", [schema]).fetchall():
                self._columns.setdefault(table.lower(), {})[column.lower()] = \
                    PhysicalField(table, column, table, column, data_type)
        log.info("physical catalog: %d tables in schema %s",
                 len(self._tables), schema)

    # -- resolution -------------------------------------------------------
    def object_is_queryable(self, object_api_name: str) -> bool:
        entry = self._tables.get((object_api_name or "").lower())
        return bool(entry and entry.enabled)

    def field_is_queryable(self, object_api_name: str,
                           field_api_name: str) -> bool:
        columns = self._columns.get((object_api_name or "").lower()) or {}
        return (field_api_name or "").lower() in columns

    def tables_with_column(self, column: str) -> list[tuple[str, str]]:
        """(table, physical column) for every enabled table holding `column`."""
        wanted = column.lower()
        out = []
        for key, entry in self._tables.items():
            field = (self._columns.get(key) or {}).get(wanted)
            if field is not None and entry.enabled:
                out.append((entry.duckdb_table, field.duckdb_column))
        return sorted(out)

    def columns_of(self, table: str) -> list[str]:
        return [f.duckdb_column for f in (self._columns.get(table.lower()) or {}).values()]

    def resolve_table(self, object_api_name: str) -> str:
        entry = self._tables.get((object_api_name or "").lower())
        if entry is None:
            raise MappingNotFound(
                QueryError.PHYSICAL_OBJECT_MAPPING_NOT_FOUND,
                f"{object_api_name!r} has no table in DuckDB schema "
                f"{self.settings.schema!r}")
        if not entry.enabled:
            raise MappingNotFound(
                QueryError.PHYSICAL_OBJECT_MAPPING_NOT_FOUND,
                f"{object_api_name!r} is mapped but disabled")
        return entry.duckdb_table

    def resolve_column(self, object_api_name: str,
                       field_api_name: str) -> PhysicalField:
        columns = self._columns.get((object_api_name or "").lower())
        if columns is None:
            raise MappingNotFound(
                QueryError.PHYSICAL_OBJECT_MAPPING_NOT_FOUND,
                f"{object_api_name!r} has no table in DuckDB")
        field = columns.get((field_api_name or "").lower())
        if field is None:
            raise MappingNotFound(
                QueryError.PHYSICAL_FIELD_MAPPING_NOT_FOUND,
                f"{object_api_name}.{field_api_name} has no column in DuckDB")
        return field

    def column_type(self, object_api_name: str, field_api_name: str) -> str:
        return self.resolve_column(object_api_name, field_api_name).duckdb_type

    # -- drift ------------------------------------------------------------
    def compare_with_runtime_schema(self, schema_service: Any,
                                    object_api_name: str) -> dict[str, Any]:
        """Where the Salesforce schema and the warehouse disagree.

        §27: a mismatch is reported, never silently ignored. A field the org
        has but the warehouse lacks will fail at query time; knowing which
        one, before a user asks, is the difference between a bug report and a
        diagnosis.
        """
        state = {"object": object_api_name, "status": "IN_SYNC",
                 "missing_columns": [], "extra_columns": []}
        if not self.object_is_queryable(object_api_name):
            state["status"] = "MISSING_TABLE"
            return state
        schema_fields = {f["api_name"].lower()
                         for f in schema_service.repo.get_fields(object_api_name)}
        physical = set(self._columns.get(object_api_name.lower(), {}))
        missing = sorted(schema_fields - physical)
        extra = sorted(physical - schema_fields)
        state["missing_columns"] = missing
        state["extra_columns"] = extra
        if missing:
            state["status"] = "MISSING_COLUMN"
        return state

    def stats(self) -> dict[str, Any]:
        return {"schema": self.settings.schema,
                "tables": len(self._tables),
                "columns": sum(len(c) for c in self._columns.values())}
