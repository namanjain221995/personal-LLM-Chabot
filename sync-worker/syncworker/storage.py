"""Batch persistence: Parquet files + DuckDB upsert + sync watermarks.

Upsert semantics: within one DuckDB transaction, DELETE existing rows whose
Id appears in the incoming batch, then INSERT the batch — so changed records
are replaced, never duplicated. Watermarks live in the _sync_meta table.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("syncworker.storage")

META_TABLE = "_sync_meta"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """DuckDB-backed warehouse with per-object tables and a _sync_meta table."""

    def __init__(self, db_path: str) -> None:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._con = duckdb.connect(db_path)
        self._con.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" ('
            "object_name VARCHAR PRIMARY KEY, "
            "watermark VARCHAR, "
            "updated_at TIMESTAMP)"
        )

    def close(self) -> None:
        self._con.close()

    # ── watermarks ──────────────────────────────────────────────────────────

    def get_watermark(self, object_name: str) -> str | None:
        row = self._con.execute(
            f'SELECT watermark FROM "{META_TABLE}" WHERE object_name = ?',
            [object_name],
        ).fetchone()
        return row[0] if row else None

    def set_watermark(self, object_name: str, watermark: str) -> None:
        self._con.execute(
            f'INSERT INTO "{META_TABLE}" (object_name, watermark, updated_at) '
            "VALUES (?, ?, now()) "
            "ON CONFLICT (object_name) DO UPDATE SET "
            "watermark = excluded.watermark, updated_at = excluded.updated_at",
            [object_name, watermark],
        )

    # ── upsert ──────────────────────────────────────────────────────────────

    def _table_exists(self, table: str) -> bool:
        row = self._con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchone()
        return bool(row and row[0])

    def _table_columns(self, table: str) -> list[str]:
        rows = self._con.execute(
            f'DESCRIBE "{_safe_ident(table)}"'
        ).fetchall()
        return [r[0] for r in rows]

    def upsert(self, object_name: str, df: pd.DataFrame) -> int:
        """Transactionally DELETE by Id then INSERT the batch. Returns row count."""
        table = _safe_ident(object_name)
        if df.empty:
            return 0
        if "Id" not in df.columns:
            raise ValueError(f"{object_name}: batch has no Id column")

        # Guard against duplicate Ids inside one batch (last write wins).
        df = df.drop_duplicates(subset=["Id"], keep="last")

        con = self._con
        con.register("_staging_df", df)
        try:
            if not self._table_exists(table):
                con.execute("BEGIN TRANSACTION")
                con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _staging_df')
                con.execute("COMMIT")
                return len(df)

            # Schema drift: add any new staging columns to the target table.
            existing = set(self._table_columns(table))
            described = con.execute("DESCRIBE SELECT * FROM _staging_df").fetchall()
            new_cols = [(c, t) for c, t, *_ in described if c not in existing]

            con.execute("BEGIN TRANSACTION")
            try:
                for col, col_type in new_cols:
                    con.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{_safe_ident(col)}" {col_type}'
                    )
                con.execute(
                    f'DELETE FROM "{table}" WHERE Id IN (SELECT Id FROM _staging_df)'
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
        return len(df)
