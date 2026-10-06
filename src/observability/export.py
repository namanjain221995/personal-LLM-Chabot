"""One exporter: canonical SQLite -> CSV and DuckDB.

Both formats come out of the same read, so they cannot disagree with each
other or with the store. Nothing writes a CSV row or a DuckDB row by hand --
that is how an export starts describing a request that never happened.

The exported table names are `query_traces` and `query_trace_events`, which is
what scripts and documentation already say, even though the canonical SQLite
tables are `trace` and `trace_event`. Renaming the store to match would break
every existing reader for a cosmetic gain.
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterable

from .config import TracingConfig, load_tracing_config

log = logging.getLogger(__name__)

TRACES_TABLE = "query_traces"
EVENTS_TABLE = "query_trace_events"

# Event columns are renamed on the way out to the names the documented schema
# uses. The store keeps its own names; the export speaks the published ones.
EVENT_RENAMES = {"sequence_number": "event_index", "stage": "event_name",
                 "created_at": "timestamp", "details": "payload"}


@dataclass
class ExportResult:
    database: str = ""
    traces: int = 0
    events: int = 0
    files: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"database": self.database, "traces": self.traces,
                "events": self.events, "files": list(self.files)}


def _open(path: Path) -> sqlite3.Connection:
    # Read-only: an export must never be able to change the thing it reports.
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, table: str,
          order: str) -> tuple[list[str], list[dict[str, Any]]]:
    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY {order}")
    columns = [d[0] for d in cursor.description]
    return columns, [dict(row) for row in cursor.fetchall()]


def _event_columns(columns: Iterable[str]) -> list[str]:
    return [EVENT_RENAMES.get(name, name) for name in columns]


def _rename(row: dict[str, Any]) -> dict[str, Any]:
    return {EVENT_RENAMES.get(key, key): value for key, value in row.items()}


def _write_csv(path: Path, columns: list[str],
               rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_duckdb(path: Path, tables: dict[str, tuple[list[str], list[dict]]]
                  ) -> None:
    """Rebuild the analytical copy from scratch.

    Replaced rather than appended: the canonical store is the only history, so
    a partial or duplicated analytical copy has no value and an inconsistent
    one is worse than none.
    """
    try:
        import duckdb
    except ImportError:
        log.warning("duckdb is not installed; skipping the DuckDB export")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = duckdb.connect(str(path))
    try:
        for table, (columns, rows) in tables.items():
            definition = ", ".join(f'"{name}" VARCHAR' for name in columns)
            connection.execute(f'CREATE TABLE "{table}" ({definition})')
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})',
                [[_text(row.get(name)) for name in columns] for row in rows])
    finally:
        connection.close()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def export_traces(config: TracingConfig | None = None, *,
                  database: str | Path | None = None,
                  formats: Iterable[str] = ("csv", "duckdb"),
                  ) -> ExportResult:
    """Export the canonical store to the configured exports directory."""
    config = config or load_tracing_config()
    path = Path(database) if database else config.database
    if not path.is_file():
        raise FileNotFoundError(f"no trace store at {path}")

    wanted = {f.lower() for f in formats}
    result = ExportResult(database=str(path))
    connection = _open(path)
    try:
        trace_columns, traces = _rows(connection, "trace", "started_at")
        event_columns, events = _rows(connection, "trace_event",
                                      "trace_id, sequence_number")
    finally:
        connection.close()

    events = [_rename(row) for row in events]
    event_columns = _event_columns(event_columns)
    result.traces, result.events = len(traces), len(events)

    exports = config.exports
    if "csv" in wanted:
        for name, columns, rows in (
                (config.traces_csv, trace_columns, traces),
                (config.events_csv, event_columns, events)):
            target = exports / name
            _write_csv(target, columns, rows)
            result.files.append(str(target))
    if "duckdb" in wanted:
        target = Path(config.duckdb_export_path)
        _write_duckdb(target, {TRACES_TABLE: (trace_columns, traces),
                               EVENTS_TABLE: (event_columns, events)})
        if target.exists():
            result.files.append(str(target))
    log.info("exported %d traces and %d events from %s",
             result.traces, result.events, path)
    return result
