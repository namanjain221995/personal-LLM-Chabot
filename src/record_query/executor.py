"""The only place that executes DuckDB SQL.

One connection layer, read-only. Nothing else in the application opens a
connection or runs a statement -- which is what makes "no writes reach the
warehouse" a property of the architecture rather than a convention.
"""
from __future__ import annotations

import logging
import threading
import weakref
import time
from typing import Any

from .config import DuckDBSettings
from .models import QueryError, QueryResult, SQLStatement

log = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


class DuckDBExecutor:
    def __init__(self, settings: DuckDBSettings) -> None:
        self.settings = settings
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[tuple[Any, Any]] = []  # (weakref to owner thread, connection)

    def _open(self) -> Any:
        try:
            import duckdb
        except ImportError as exc:
            raise ExecutionError(QueryError.DUCKDB_EXECUTION_FAILED,
                                 "duckdb is not installed") from exc
        try:
            connection = duckdb.connect(self.settings.path,
                                        read_only=self.settings.read_only)
        except Exception as exc:
            raise ExecutionError(
                QueryError.DATA_NOT_SYNCED,
                f"cannot open {self.settings.path}: {exc}") from exc
        with self._lock:
            # One connection per thread, owned by that thread. When a new
            # thread connects, connections whose thread has ENDED are closed:
            # a pool that replaces its workers otherwise leaks one handle per
            # retired thread per component -- 4 per question, found live when
            # a 500-question run hit 'Too many open files' at question 145.
            alive = []
            for owner, held in self._connections:
                thread = owner()
                if thread is not None and thread.is_alive():
                    alive.append((owner, held))
                    continue
                try:
                    held.close()
                except Exception:                    # noqa: BLE001
                    pass
            alive.append((weakref.ref(threading.current_thread()), connection))
            self._connections = alive
        return connection

    @property
    def connection(self) -> Any:
        # DuckDB connections are not thread-safe; a cursor per thread is the
        # supported pattern and costs nothing against a read-only file.
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._open()
            self._local.connection = existing
        return existing

    def close(self) -> None:
        with self._lock:
            for _owner, connection in self._connections:
                try:
                    connection.close()
                except Exception:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def execute(self, statement: SQLStatement, *, max_rows: int,
                max_bytes: int) -> QueryResult:
        result = QueryResult(sql=statement.sql)
        started = time.perf_counter()
        try:
            cursor = self.connection.execute(statement.sql, statement.params)
            columns = [d[0] for d in (cursor.description or [])]
            # One row past the cap, so truncation is detectable rather than
            # silently indistinguishable from a result that just fits.
            fetched = cursor.fetchmany(max_rows + 1)
        except Exception as exc:
            result.execution_ms = (time.perf_counter() - started) * 1000
            # The DuckDB message can name columns and values; it is logged in
            # full and summarised for the caller.
            log.warning("duckdb execution failed: %s", exc)
            return result.fail(QueryError.DUCKDB_EXECUTION_FAILED,
                               str(exc).splitlines()[0][:200])

        result.execution_ms = (time.perf_counter() - started) * 1000
        result.columns = columns
        # The statement asks for one row past the cap precisely so this can
        # tell "exactly the cap" from "the cap and more".
        result.truncated = len(fetched) > max_rows
        rows = fetched[:max_rows]
        result.rows = [dict(zip(columns, row)) for row in rows]

        # A result that would flood the answering model is cut here, not there.
        size = 0
        for index, row in enumerate(result.rows):
            size += sum(len(str(v)) for v in row.values())
            if size > max_bytes:
                result.rows = result.rows[:index]
                result.truncated = True
                break

        result.row_count = len(result.rows)
        result.success = True
        return result

    def __enter__(self) -> "DuckDBExecutor":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
