"""Batch persistence: Parquet files + DuckDB upsert + sync watermarks.

Upsert semantics: within one DuckDB transaction, DELETE existing rows whose
Id appears in the incoming batch, then INSERT the batch — so changed records
are replaced, never duplicated. Watermarks live in the _sync_meta table.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("syncworker.storage")

META_TABLE = "_sync_meta"
RAG_PENDING_TABLE = "_rag_index_pending"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How long a Store operation waits for the cross-process file lock. The
#: orchestrator's read-only queries are query-length, so contention clears in
#: well under this.
_LOCK_RETRY_SECONDS = 10.0
_LOCK_RETRY_STEP = 0.25


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def sf_datetime_literal(dt: datetime) -> str:
    """Format a datetime as a Salesforce SOQL datetime literal (UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_records(records: list[dict]) -> list[dict]:
    """Normalize REST/Bulk record values to consistent string-or-None values.

    Bulk CSV yields strings ('' for null, 'true'/'false' for booleans); REST
    JSON yields typed values. Normalizing both keeps DuckDB column types
    stable across full and incremental syncs.
    """
    out: list[dict] = []
    for rec in records:
        row: dict = {}
        for key, value in rec.items():
            if value is None or value == "":
                row[key] = None
            elif isinstance(value, bool):
                row[key] = "true" if value else "false"
            else:
                row[key] = str(value)
        out.append(row)
    return out


def write_parquet_batch(df: pd.DataFrame, object_name: str, parquet_dir: str) -> str:
    """Write one batch to PARQUET_DIR/<object>/<object>_<utc>_<uuid>.parquet."""
    _safe_ident(object_name)
    target_dir = os.path.join(parquet_dir, object_name)
    os.makedirs(target_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = os.path.join(
        target_dir, f"{object_name}_{stamp}_{uuid.uuid4().hex[:8]}.parquet"
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


class Store:
    """DuckDB-backed warehouse with per-object tables and a _sync_meta table.

    Connections are PER OPERATION (opened, used, closed), not per Store: a
    write connection excludes every reader across processes, and the old
    cycle-long connection locked the orchestrator's SQL engine out for the
    whole cycle — users saw raw "Could not set lock" errors whenever a sync
    (or the one-time column-healing backfill) was running. Now the file lock
    is held only for the milliseconds each write actually takes, and both
    sides retry briefly, so chat queries interleave freely with syncing.
    """

    def __init__(self, db_path: str) -> None:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._path = db_path
        with self._connection() as con:
            con.execute(
                f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" ('
                "object_name VARCHAR PRIMARY KEY, "
                "watermark VARCHAR, "
                "updated_at TIMESTAMP)"
            )
            con.execute(
                f'CREATE TABLE IF NOT EXISTS "{RAG_PENDING_TABLE}" ('
                "object_name VARCHAR NOT NULL, "
                "record_id VARCHAR NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 1, "
                "last_error VARCHAR, "
                "updated_at TIMESTAMP NOT NULL DEFAULT now(), "
                "PRIMARY KEY (object_name, record_id))"
            )

    @contextmanager
    def _connection(self):
        deadline = time.monotonic() + _LOCK_RETRY_SECONDS
        while True:
            try:
                con = duckdb.connect(self._path)
                break
            except duckdb.Error as exc:
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_STEP)
        try:
            yield con
        finally:
            con.close()

    @property
    def _con(self):
        """Test/debug escape hatch: a fresh connection to the same database.

        In-process connections share one cached database instance, so this is
        safe alongside the per-operation connections; production code never
        uses it.
        """
        return duckdb.connect(self._path)

    def close(self) -> None:
        """Kept for call-site compatibility; connections are per-operation."""

    # ── watermarks ──────────────────────────────────────────────────────────

    def get_watermark(self, object_name: str) -> str | None:
        with self._connection() as con:
            row = con.execute(
                f'SELECT watermark FROM "{META_TABLE}" WHERE object_name = ?',
                [object_name],
            ).fetchone()
        return row[0] if row else None

    def set_watermark(self, object_name: str, watermark: str) -> None:
        with self._connection() as con:
            con.execute(
                f'INSERT INTO "{META_TABLE}" (object_name, watermark, updated_at) '
                "VALUES (?, ?, now()) "
                "ON CONFLICT (object_name) DO UPDATE SET "
                "watermark = excluded.watermark, updated_at = excluded.updated_at",
                [object_name, watermark],
            )

    # ── retryable RAG indexing state ─────────────────────────────────────────

    def mark_rag_pending(
        self, object_name: str, record_ids: list[str], error: str
    ) -> int:
        """Persist records whose warehouse write succeeded but indexing failed."""
        _safe_ident(object_name)
        ids = sorted({str(record_id) for record_id in record_ids if record_id})
        if not ids:
            return 0
        message = str(error)[:2000]
        with self._connection() as con:
            con.executemany(
                f'INSERT INTO "{RAG_PENDING_TABLE}" '
                "(object_name, record_id, attempts, last_error, updated_at) "
                "VALUES (?, ?, 1, ?, now()) "
                "ON CONFLICT (object_name, record_id) DO UPDATE SET "
                f'attempts = "{RAG_PENDING_TABLE}".attempts + 1, '
                "last_error = excluded.last_error, updated_at = excluded.updated_at",
                [(object_name, record_id, message) for record_id in ids],
            )
        return len(ids)

    def clear_rag_pending(self, object_name: str, record_ids: list[str]) -> int:
        _safe_ident(object_name)
        ids = sorted({str(record_id) for record_id in record_ids if record_id})
        if not ids:
            return 0
        removed = 0
        with self._connection() as con:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                placeholders = ", ".join("?" for _ in batch)
                before = con.execute(
                    f'SELECT count(*) FROM "{RAG_PENDING_TABLE}" '
                    f"WHERE object_name = ? AND record_id IN ({placeholders})",
                    [object_name, *batch],
                ).fetchone()[0]
                con.execute(
                    f'DELETE FROM "{RAG_PENDING_TABLE}" '
                    f"WHERE object_name = ? AND record_id IN ({placeholders})",
                    [object_name, *batch],
                )
                removed += int(before)
        return removed

    def pending_rag_ids(self, object_name: str) -> list[str]:
        _safe_ident(object_name)
        with self._connection() as con:
            rows = con.execute(
                f'SELECT record_id FROM "{RAG_PENDING_TABLE}" '
                "WHERE object_name = ? ORDER BY updated_at, record_id",
                [object_name],
            ).fetchall()
        return [str(row[0]) for row in rows]

    def pending_rag_records(
        self,
        object_name: str,
        rag_fields: list[str] | tuple[str, ...],
        *,
        limit: int = 500,
    ) -> list[dict]:
        """Read pending records back from the authoritative warehouse table."""
        table = _safe_ident(object_name)
        requested = ["Id", *rag_fields]
        with self._connection() as con:
            if not self._table_exists(con, table):
                return []
            existing = self._table_column_types(con, table)
            if "SystemModstamp" in existing:
                requested.append("SystemModstamp")
            fields = []
            for field in requested:
                safe = _safe_ident(str(field))
                if safe in existing and safe not in fields:
                    fields.append(safe)
            if "Id" not in fields:
                return []
            select = ", ".join(f't."{field}"' for field in fields)
            cursor = con.execute(
                f'SELECT {select} FROM "{table}" AS t '
                f'JOIN "{RAG_PENDING_TABLE}" AS p '
                "ON p.object_name = ? AND p.record_id = t.Id "
                "ORDER BY p.updated_at, p.record_id LIMIT ?",
                [object_name, max(1, int(limit))],
            )
            rows = cursor.fetchall()
            names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in rows]

    # ── upsert ──────────────────────────────────────────────────────────────

    @staticmethod
    def _table_exists(con, table: str) -> bool:
        row = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _table_column_types(con, table: str) -> dict[str, str]:
        rows = con.execute(f'DESCRIBE "{_safe_ident(table)}"').fetchall()
        return {r[0]: r[1] for r in rows}

    def delete_ids(self, object_name: str, ids: list[str]) -> int:
        """Remove rows whose Id is in `ids` (records deleted in Salesforce)."""
        table = _safe_ident(object_name)
        if not ids:
            return 0
        df = pd.DataFrame({"Id": [str(i) for i in ids]})
        with self._connection() as con:
            con.register("_deleted_ids", df)
            try:
                before = 0
                if self._table_exists(con, table):
                    before = con.execute(
                        f'SELECT count(*) FROM "{table}" '
                        "WHERE Id IN (SELECT Id FROM _deleted_ids)"
                    ).fetchone()[0]
                    con.execute(
                        f'DELETE FROM "{table}" WHERE Id IN '
                        "(SELECT Id FROM _deleted_ids)"
                    )
                con.execute(
                    f'DELETE FROM "{RAG_PENDING_TABLE}" WHERE object_name = ? '
                    "AND record_id IN (SELECT Id FROM _deleted_ids)",
                    [object_name],
                )
            finally:
                con.unregister("_deleted_ids")
        return int(before)

    def reconcile_full(self, object_name: str, keep_ids: set[str]) -> list[str]:
        """After a FULL extract, drop rows the extract did not contain.

        A full extract is a complete snapshot of the org's live records, so
        anything local that was absent from it is a record deleted (or hidden)
        in Salesforce. Returns the removed Ids so callers can purge the RAG
        index too. Incremental cycles cannot do this — they only see changes.
        """
        table = _safe_ident(object_name)
        df = pd.DataFrame({"Id": [str(i) for i in keep_ids]})
        with self._connection() as con:
            if not self._table_exists(con, table):
                return []
            con.register("_keep_ids", df)
            try:
                removed = [
                    r[0]
                    for r in con.execute(
                        f'SELECT Id FROM "{table}" '
                        "WHERE Id NOT IN (SELECT Id FROM _keep_ids)"
                    ).fetchall()
                ]
                if removed:
                    con.execute(
                        f'DELETE FROM "{table}" '
                        "WHERE Id NOT IN (SELECT Id FROM _keep_ids)"
                    )
                con.execute(
                    f'DELETE FROM "{RAG_PENDING_TABLE}" WHERE object_name = ? '
                    "AND record_id NOT IN (SELECT Id FROM _keep_ids)",
                    [object_name],
                )
            finally:
                con.unregister("_keep_ids")
        return removed

    def clear_watermark(self, object_name: str) -> bool:
        """Forget an object's watermark so its next sync is a FULL extract."""
        with self._connection() as con:
            cur = con.execute(
                f'DELETE FROM "{META_TABLE}" WHERE object_name = ?', [object_name]
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def ensure_table(self, object_name: str, fields: list[str] | tuple[str, ...]) -> None:
        """Make sure the object's table exists and has every configured column.

        An object holding zero records in Salesforce never reaches upsert, so
        without this it never gets a table — and every SQL question about it
        fails with "table does not exist". The honest answer to "how many X"
        is 0, not an error. Likewise, when the config grows (say, an admin
        widens field-level security) an object that STILL has no rows would
        keep its old skinny schema forever — so missing configured columns
        are added here too. Existing columns and data are never touched.
        """
        table = _safe_ident(object_name)
        cols = ", ".join(f'"{_safe_ident(f)}" VARCHAR' for f in fields)
        with self._connection() as con:
            con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols})')
            existing = self._table_column_types(con, table)
            for f in fields:
                if f not in existing:
                    con.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{_safe_ident(f)}" VARCHAR'
                    )

    def upsert(self, object_name: str, df: pd.DataFrame) -> int:
        """Transactionally DELETE by Id then INSERT the batch. Returns row count."""
        table = _safe_ident(object_name)
        if df.empty:
            return 0
        if "Id" not in df.columns:
            raise ValueError(f"{object_name}: batch has no Id column")

        # Guard against duplicate Ids inside one batch (last write wins).
        df = df.drop_duplicates(subset=["Id"], keep="last")
        # Every value is already string-or-None (normalize_records), but an
        # ALL-None column arrives as pandas `object` and DuckDB resolves its
        # NULL type to INTEGER on CREATE TABLE AS — the first real value
        # ('Full Time' into an INT32 Employment_Type__c) then fails every
        # cycle. Pin the staging frame to pandas string dtype so DuckDB
        # always sees VARCHAR.
        df = df.astype("string")

        mistyped: list[str] = []
        with self._connection() as con:
            con.register("_staging_df", df)
            try:
                if not self._table_exists(con, table):
                    con.execute("BEGIN TRANSACTION")
                    con.execute(
                        f'CREATE TABLE "{table}" AS SELECT * FROM _staging_df'
                    )
                    con.execute("COMMIT")
                    return len(df)

                # Schema drift: add any new staging columns to the table.
                existing_types = self._table_column_types(con, table)
                described = con.execute(
                    "DESCRIBE SELECT * FROM _staging_df"
                ).fetchall()
                new_cols = [
                    (c, t) for c, t, *_ in described if c not in existing_types
                ]
                # Heal columns mistyped by the old NULL-type inference:
                # anything non-VARCHAR in an all-strings warehouse rejects
                # real values.
                mistyped = [
                    c for c, t in existing_types.items()
                    if c in df.columns and t != "VARCHAR"
                ]

                con.execute("BEGIN TRANSACTION")
                try:
                    for col in mistyped:
                        con.execute(
                            f'ALTER TABLE "{table}" '
                            f'ALTER COLUMN "{_safe_ident(col)}" '
                            "SET DATA TYPE VARCHAR"
                        )
                    for col, col_type in new_cols:
                        con.execute(
                            f'ALTER TABLE "{table}" '
                            f'ADD COLUMN "{_safe_ident(col)}" {col_type}'
                        )
                    con.execute(
                        f'DELETE FROM "{table}" '
                        "WHERE Id IN (SELECT Id FROM _staging_df)"
                    )
                    con.execute(
                        f'INSERT INTO "{table}" BY NAME SELECT * FROM _staging_df'
                    )
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.unregister("_staging_df")
        if mistyped:
            log.info(
                "healed mistyped warehouse columns",
                extra={"event": "columns_healed", "object": object_name,
                       "columns": mistyped},
            )
        return len(df)
