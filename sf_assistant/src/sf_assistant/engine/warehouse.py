"""Read-only connection to the local warehouse snapshot (imports duckdb).

One connection per process, opened ``read_only`` with external access, extension autoload and
autoinstall off, the session time zone set to UTC globally and the configuration locked. Every
statement runs on a fresh cursor, passes :func:`okrec.guard.check_sql` first, is interrupted after
``timeout_s`` and comes back as a JSON-safe :class:`okrec.types.Statement`.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
from decimal import Decimal
from typing import Any

from .sql_guard import GuardError, check_sql
from .snapshot import check_warehouse_path, ensure_private
from .types import RecordsUnavailable, Statement

__all__ = ["Warehouse", "WarehouseError", "json_cell", "CONNECT_CONFIG"]

CONNECT_CONFIG: dict[str, Any] = {
    "enable_external_access": False, "autoload_known_extensions": False,
    "autoinstall_known_extensions": False, "max_memory": "2GB", "threads": 4, "temp_directory": ""}

# Constant introspection statements (the only SQL that skips the guard).
_SQL_COLUMNS = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position")
_SQL_HAS_SYNC_META = ("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'raw' "
                      "AND table_name = '_sync_meta'")
_SQL_SYNC = 'SELECT object_name, updated_at FROM raw."_sync_meta"'
_SQL_HAS_ORG = ("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main' "
                "AND table_name = 'Organization'")
_SQL_ORG_COLUMNS = ("SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
                    "AND table_name = 'Organization'")
_ORG_FIELDS = ("IsSandbox", "TimeZoneSidKey", "FiscalYearStartMonth")
_SQL_ORG_FIELD = {f: f'SELECT "{f}" FROM main."Organization" LIMIT 1' for f in _ORG_FIELDS}
_INTROSPECT_SQL = frozenset({_SQL_COLUMNS, _SQL_HAS_SYNC_META, _SQL_SYNC, _SQL_HAS_ORG, _SQL_ORG_COLUMNS,
                             *_SQL_ORG_FIELD.values()})


class WarehouseError(Exception):
    """A statement could not run (guard failure, DuckDB error or timeout)."""

    def __init__(self, message: str, *, kind: str = "error", detail: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind          # "guard" | "timeout" | "error"
        self.detail = detail or message


def json_cell(v: Any) -> Any:
    """JSON-safe copy of one DuckDB value (see RECORDS_SPEC §7.3)."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, Decimal):
        if not v.is_finite():
            return None
        return int(v) if v == v.to_integral_value() and v.as_tuple().exponent >= 0 else float(v)
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S") if v.tzinfo is None else \
            v.astimezone(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary>"
    if isinstance(v, (list, tuple)):
        return [json_cell(x) for x in v]
    if isinstance(v, dict):
        return {str(k): json_cell(x) for k, x in v.items()}
    return str(v)


class Warehouse:
    """The snapshot connection plus schema caches."""

    def __init__(self, con: Any, path: str, duckdb_version: str, timeout_s: float, made_private: bool) -> None:
        self._con = con
        self.path = path
        self.duckdb_version = duckdb_version
        self.timeout_s = timeout_s
        self.made_private = made_private
        self.allowed: frozenset[tuple[str, str]] = frozenset()
        self.blocked_columns: frozenset[str] = frozenset()
        self.blocked_fields: frozenset[tuple[str, str]] = frozenset()
        self._columns: dict[str, dict[str, str]] | None = None
        self._sync: dict[str, dt.datetime] | None = None
        self._org: dict | None = None
        self._closed = False

    # ------------------------------------------------------------------ open / close
    @classmethod
    def open(cls, path: str, *, timeout_s: float = 10.0) -> "Warehouse":
        """Open ``path`` read-only. Raises :class:`RecordsUnavailable` with a one-line reason."""
        try:
            import duckdb
        except ImportError:
            raise RecordsUnavailable("duckdb is not installed in .venv (uv pip install --python .venv/bin/python "
                                     "--no-deps -r web/requirements.lock)") from None
        real = check_warehouse_path(path)
        made_private = False
        try:
            made_private = ensure_private(real)
        except OSError:
            pass
        try:
            con = duckdb.connect(real, read_only=True, config=dict(CONNECT_CONFIG))
            con.execute("SET GLOBAL TimeZone='UTC'")      # GLOBAL: a session-level SET does not reach cursors
            con.execute("SET lock_configuration=true")
        except Exception as exc:
            raise RecordsUnavailable(f"invalid warehouse snapshot {real}: {type(exc).__name__}: "
                                     f"{str(exc).splitlines()[0][:200]}") from None
        wh = cls(con, real, duckdb.__version__, timeout_s, made_private)
        try:
            if not wh.introspect(_SQL_HAS_SYNC_META)[0][0]:
                raise RecordsUnavailable(f"invalid warehouse snapshot {real}: raw._sync_meta is missing")
        except RecordsUnavailable:
            wh.close()
            raise
        except Exception as exc:
            wh.close()
            raise RecordsUnavailable(f"invalid warehouse snapshot {real}: {type(exc).__name__}: "
                                     f"{str(exc).splitlines()[0][:200]}") from None
        return wh

    def close(self) -> None:
        """Close the connection (idempotent)."""
        if not self._closed:
            self._closed = True
            try:
                self._con.close()
            except Exception:
                pass

    def set_policy(self, allowed: frozenset[tuple[str, str]], blocked_columns: frozenset[str],
                   blocked_fields: frozenset[tuple[str, str]] = frozenset()) -> None:
        """Tables that ``run`` may read, column names it must never reference (any table) and
        ``(table, column)`` pairs it must never reference."""
        self.allowed = frozenset(allowed)
        self.blocked_columns = frozenset(c.lower() for c in blocked_columns)
        self.blocked_fields = frozenset((t.lower(), c.lower()) for t, c in blocked_fields)

    # ------------------------------------------------------------------ execution
    @staticmethod
    def _execute(cur: Any, sql: str, params: list | tuple, timeout_s: float) -> Any:
        """Execute with an interrupt timer; returns the cursor. Raises duckdb errors as they are."""
        timer = threading.Timer(timeout_s, cur.interrupt)
        timer.daemon = True
        timer.start()
        try:
            return cur.execute(sql, list(params))
        finally:
            timer.cancel()

    def run(self, sql: str, params: list | tuple = (), *, max_rows: int = 1000,
            max_bytes: int = 2_000_000, timeout_s: float | None = None) -> Statement:
        """Guard, execute and fetch one statement. Raises :class:`WarehouseError`."""
        import duckdb

        limit = self.timeout_s if timeout_s is None else timeout_s
        t0 = time.perf_counter()
        cur = self._con.cursor()
        try:
            try:
                check_sql(cur, sql, self.allowed, self.blocked_columns, self.blocked_fields)
            except GuardError as exc:
                raise WarehouseError("internal query check failed", kind="guard", detail=str(exc)) from None
            try:
                res = self._execute(cur, sql, params, limit)
                columns = [d[0] for d in (res.description or [])]
                raw_rows = res.fetchmany(max_rows + 1)
            except duckdb.InterruptException:
                raise WarehouseError(f"query stopped after {limit:g} s", kind="timeout") from None
            except duckdb.Error as exc:
                first = (str(exc).splitlines() or [""])[0][:300]
                raise WarehouseError(f"{type(exc).__name__}: {first}") from None
        finally:
            try:
                cur.close()
            except Exception:
                pass
        truncated = len(raw_rows) > max_rows
        rows: list[list[Any]] = []
        size = 0
        for r in raw_rows[:max_rows]:
            row = [json_cell(v) for v in r]
            size += sum(len(str(c)) for c in row)
            if size > max_bytes:
                truncated = True
                break
            rows.append(row)
        return Statement(sql=sql, params=[json_cell(p) for p in params], columns=columns, rows=rows,
                         row_count=len(rows), truncated=truncated,
                         elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))

    def introspect(self, sql: str, params: list | tuple = ()) -> list[tuple]:
        """Run one module-level constant statement (schema, sync and org reads) without the allowlist."""
        if sql not in _INTROSPECT_SQL:
            raise WarehouseError("introspect takes the module's constant statements only", kind="guard")
        cur = self._con.cursor()
        try:
            return list(self._execute(cur, sql, params, self.timeout_s).fetchall())
        finally:
            cur.close()

    # ------------------------------------------------------------------ schema caches
    def columns(self) -> dict[str, dict[str, str]]:
        """main object -> {column: duckdb type}, in column order."""
        if self._columns is None:
            out: dict[str, dict[str, str]] = {}
            for table, col, typ in self.introspect(_SQL_COLUMNS):
                out.setdefault(str(table), {})[str(col)] = str(typ)
            self._columns = out
        return self._columns

    def sync_times(self) -> dict[str, dt.datetime]:
        """object_name (case as stored) -> updated_at (UTC, naive)."""
        if self._sync is None:
            out: dict[str, dt.datetime] = {}
            for name, ts in self.introspect(_SQL_SYNC):
                if name and isinstance(ts, dt.datetime):
                    out[str(name)] = ts.replace(tzinfo=None) if ts.tzinfo is None else \
                        ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
            self._sync = out
        return self._sync

    def org(self) -> dict:
        """IsSandbox, TimeZoneSidKey and FiscalYearStartMonth of the org row ({} when missing)."""
        if self._org is None:
            org: dict = {}
            try:
                if self.introspect(_SQL_HAS_ORG)[0][0]:
                    cols = {r[0] for r in self.introspect(_SQL_ORG_COLUMNS)}
                    for f in _ORG_FIELDS:
                        if f in cols:
                            rows = self.introspect(_SQL_ORG_FIELD[f])
                            org[f] = rows[0][0] if rows else None
            except Exception:
                org = {}
            self._org = org
        return self._org

    def file_mtime_utc(self) -> dt.datetime | None:
        """Modification time of the snapshot file (UTC, naive)."""
        try:
            return dt.datetime.fromtimestamp(os.path.getmtime(self.path), dt.timezone.utc).replace(tzinfo=None)
        except OSError:
            return None
