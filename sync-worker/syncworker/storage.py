"""Batch persistence: Parquet files + DuckDB upsert + sync watermarks.

Upsert semantics: within one DuckDB transaction, DELETE existing rows whose
Id appears in the incoming batch, then INSERT the batch — so changed records
are replaced, never duplicated. Watermarks live in the _sync_meta table.
"""

from __future__ import annotations

import contextlib
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

#: Everything this worker writes lives in `raw`: the per-object VARCHAR tables
#: and its own bookkeeping. `main` holds only the typed views the application
#: reads (see syncworker.views), so the SQL engine's schema slice contains
#: business data and nothing else.
#:
#: Every statement below is written UNQUALIFIED and resolves here because the
#: connection issues `USE raw`. That is deliberate -- it keeps one schema
#: decision in one place instead of threading a prefix through 25 SQL strings.
RAW_SCHEMA = "raw"

#: The bookkeeping tables' column definitions, in ONE place. They carry
#: PRIMARY KEYs that `set_watermark` and `mark_rag_pending` rely on for their
#: ON CONFLICT clauses -- and CREATE TABLE ... AS SELECT does NOT copy a
#: constraint, so a relocation that used CTAS produced a table that looked
#: right and rejected every upsert into it. Both paths build from these.
_BOOKKEEPING_DDL = {
    META_TABLE: (
        "object_name VARCHAR PRIMARY KEY, "
        "watermark VARCHAR, "
        "updated_at TIMESTAMP"
    ),
    RAG_PENDING_TABLE: (
        "object_name VARCHAR NOT NULL, "
        "record_id VARCHAR NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 1, "
        "last_error VARCHAR, "
        "updated_at TIMESTAMP NOT NULL DEFAULT now(), "
        "PRIMARY KEY (object_name, record_id)"
    ),
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How long a Store operation waits for the cross-process file lock. The
#: orchestrator's read-only queries are query-length, so contention clears in
#: well under this.
_LOCK_RETRY_SECONDS = 10.0
_LOCK_RETRY_STEP = 0.25


def _use_raw(con) -> None:
    """Point this connection's unqualified names at the raw schema."""
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{RAW_SCHEMA}"')
    con.execute(f'USE "{RAW_SCHEMA}"')


def _relocate_bookkeeping(con) -> list[str]:
    """Move _sync_meta / _rag_index_pending from main into raw, once.

    THE WATERMARKS LIVE IN _sync_meta. A warehouse built before the raw schema
    holds them in main, and reading an empty raw copy instead would make every
    object look unsynced -- turning the next cycle into a full Bulk re-extract
    of the entire org. So this runs BEFORE the CREATE TABLE IF NOT EXISTS that
    would otherwise manufacture that empty copy.

    Idempotent: a table already in raw is left alone.
    """
    moved: list[str] = []
    for table in (META_TABLE, RAG_PENDING_TABLE):
        in_raw = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [RAW_SCHEMA, table],
        ).fetchone()
        in_main = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchone()
        if (in_raw and in_raw[0]) or not (in_main and in_main[0]):
            continue
        con.execute("BEGIN TRANSACTION")
        try:
            # DDL first, then INSERT. CTAS would copy the rows and silently
            # drop the PRIMARY KEY that ON CONFLICT depends on.
            con.execute(
                f'CREATE TABLE "{RAW_SCHEMA}"."{table}" '
                f"({_BOOKKEEPING_DDL[table]})"
            )
            con.execute(
                f'INSERT INTO "{RAW_SCHEMA}"."{table}" '
                f'SELECT * FROM main."{table}"'
            )
            con.execute(f'DROP TABLE main."{table}"')
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        moved.append(table)
    if moved:
        log.info(
            "sync bookkeeping relocated to the raw schema",
            extra={"event": "bookkeeping_relocated", "tables": moved},
        )
    return moved


def _repair_bookkeeping_constraints(con) -> list[str]:
    """Rebuild a bookkeeping table that lost its PRIMARY KEY.

    An earlier relocation used CREATE TABLE ... AS SELECT, which copies rows
    but not constraints. The result reads fine and then fails every write:
    `set_watermark`'s ON CONFLICT has no conflict target, so no watermark ever
    advances and the sync re-extracts the whole org forever.

    Detect by asking the catalog for the constraint, not by trying a write --
    a failed write inside a session would poison the transaction. Rows are
    preserved; only the table definition changes.
    """
    repaired: list[str] = []
    for table, ddl in _BOOKKEEPING_DDL.items():
        present = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [RAW_SCHEMA, table],
        ).fetchone()
        if not (present and present[0]):
            continue
        constraints = con.execute(
            "SELECT count(*) FROM duckdb_constraints() "
            "WHERE schema_name = ? AND table_name = ? "
            "AND constraint_type = 'PRIMARY KEY'",
            [RAW_SCHEMA, table],
        ).fetchone()
        if constraints and constraints[0]:
            continue
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f'CREATE TABLE "{RAW_SCHEMA}"."{table}__fixed" ({ddl})')
            con.execute(
                f'INSERT INTO "{RAW_SCHEMA}"."{table}__fixed" '
                f'SELECT * FROM "{RAW_SCHEMA}"."{table}"'
            )
            con.execute(f'DROP TABLE "{RAW_SCHEMA}"."{table}"')
            con.execute(
                f'ALTER TABLE "{RAW_SCHEMA}"."{table}__fixed" '
                f'RENAME TO "{table}"'
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        repaired.append(table)
    if repaired:
        log.warning(
            "rebuilt bookkeeping tables that had lost their PRIMARY KEY",
            extra={"event": "bookkeeping_repaired", "tables": repaired},
        )
    return repaired


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

    CONNECTIONS ARE PER OPERATION BY DEFAULT, POOLED WITHIN A `session()`.

    The history matters, because both halves of it are right at different
    scales. A cycle-long connection once locked the orchestrator's SQL engine
    out for the entire cycle — users saw raw "Could not set lock" errors —
    so connections became per-operation, holding the file lock only for the
    milliseconds each write takes.

    That reasoning assumed opening the file is cheap. It is not, and the cost
    grows with the CATALOG, not the data: `duckdb.connect` loads every table
    definition before it will do anything. Measured on this warehouse's shape
    (40 columns per object, DuckDB 1.5.5):

        relations in file        connect()
                      0            4.5 ms
                     50             19 ms
                    250             76 ms
                  1,023            291 ms
          1,023 + views            318 ms

    At 8 objects a 5 ms open per operation was invisible. At 1,023 objects
    (~4 operations each, so ~4,000 opens) it is ~21 minutes of lock
    acquisition per cycle to perform ~127 ms of actual work — the cycle
    overruns its 5-minute interval, the worker never sleeps, and readers are
    starved continuously rather than briefly.

    `session()` pins ONE connection for a bounded run of operations. Callers
    that do not open a session are completely unaffected; every existing
    `with self._connection() as con:` call site is unchanged.

    THE RULE FOR SCOPING A SESSION: it must cover DuckDB work only, never a
    Salesforce or embedding call. A session holds the file's write lock for
    its whole lifetime, and the orchestrator's read-only queries cannot open
    the file at all while it is held. Measured with realistic per-object HTTP
    latency, a session spanning a batch of objects took chat queries from 100%
    success at ~300 ms to 40% success at ~4 s — the very failure the
    per-operation design was introduced to cure. sync_object therefore opens
    one short session around the DuckDB operations before the extract and
    one around those after it, and does its network I/O between them.
    """

    def __init__(self, db_path: str) -> None:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._path = db_path
        # MUST precede the _connection() call below — _connection consults it.
        self._pinned = None
        #: Read-write connections opened so far. Surfaced in the cycle log so
        #: the effect of session scoping is visible in production, not assumed.
        self.connects = 0
        with self._connection() as con:
            # BEFORE the CREATE IF NOT EXISTS below, or the watermarks stay
            # stranded in main behind a freshly-made empty table in raw.
            _relocate_bookkeeping(con)
            # Heal a table relocated by an earlier build's CTAS, which copied
            # the rows without the PRIMARY KEY.
            _repair_bookkeeping_constraints(con)
            for table, ddl in _BOOKKEEPING_DDL.items():
                con.execute(
                    f'CREATE TABLE IF NOT EXISTS "{table}" ({ddl})'
                )

    def _connect(self):
        """Open one read-write connection, waiting out a competing lock."""
        deadline = time.monotonic() + _LOCK_RETRY_SECONDS
        while True:
            try:
                con = duckdb.connect(self._path)
                break
            except duckdb.Error as exc:
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_STEP)
        self.connects += 1
        _use_raw(con)
        return con

    @contextmanager
    def _connection(self):
        # Inside a session the connection is already open and already pointed
        # at `raw` — reuse it, and do NOT close it here: the session owns its
        # lifetime, and closing it mid-session would strand every later
        # operation in that session.
        if self._pinned is not None:
            try:
                yield self._pinned
            except BaseException:
                # EVERY OPERATION MUST END OUTSIDE A TRANSACTION. Per-operation
                # connections guaranteed that for free: close() discarded
                # whatever was open. A pooled connection does not, and DuckDB
                # is unforgiving about the leak — the next operation's BEGIN
                # fails with "cannot start a transaction within a transaction",
                # and its ROLLBACK then undoes THIS operation's uncommitted
                # writes along with anything since. So an operation that
                # raises rolls back before the exception leaves it. Harmless
                # when there is nothing to roll back (that just raises, and is
                # swallowed here), and exactly what close() used to do.
                with contextlib.suppress(Exception):
                    self._pinned.execute("ROLLBACK")
                raise
            return
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def session(self):
        """Hold one connection for everything done inside the block.

        Turns N lock acquisitions into one. Scope it to a contiguous run of
        DuckDB operations and NOTHING ELSE — no Salesforce call, no embedding
        call — because the write lock is held for the session's whole
        lifetime and readers are locked out for all of it (see the class
        docstring for the measurement).

        Re-entrant: a nested session reuses the outer one and leaves closing
        to it, so a helper can open a session without knowing whether its
        caller already did.

        On the way out the connection is closed even if the body raised —
        a failed operation must not leak the write lock.
        """
        if self._pinned is not None:
            yield  # outer session owns the connection
            return
        con = self._connect()
        self._pinned = con
        try:
            yield
        finally:
            self._pinned = None
            # A close that fails must not mask the exception (if any) that
            # is already on its way out.
            with contextlib.suppress(Exception):
                con.close()

    @property
    def _con(self):
        """Test/debug escape hatch: a fresh connection to the same database.

        In-process connections share one cached database instance, so this is
        safe alongside the per-operation connections; production code never
        uses it.
        """
        con = duckdb.connect(self._path)
        _use_raw(con)
        return con

    def close(self) -> None:
        """Release a pinned connection if one is somehow still open.

        Normally `session()` closes its own connection on the way out; this is
        the belt-and-braces path for a caller that abandoned a session without
        unwinding it (a hard error between cycles, say). Idempotent, and a
        no-op when no session is active — which is why every existing
        `store.close()` call site keeps working unchanged.
        """
        con, self._pinned = self._pinned, None
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()

    def refresh_typed_view(
        self,
        object_name: str,
        specs,
        org_timezone: str | None = None,
    ) -> bool:
        """Rebuild main.<object> as a typed view over raw.<object>.

        Migration and refresh in one call: `promote_to_raw` is idempotent, so
        a warehouse that predates the raw schema moves itself on the first
        cycle and is a catalog check on every one after.

        Never raises -- a view that cannot be built leaves the raw table fully
        queryable, and one broken object must not stop the sync.
        """
        from .views import promote_to_raw, refresh_view

        try:
            with self._connection() as con:
                promote_to_raw(con, object_name)
                return refresh_view(con, object_name, specs, org_timezone)
        except Exception:
            log.error(
                "typed view refresh failed; raw table is unaffected",
                exc_info=True,
                extra={"event": "view_refresh_error", "object": object_name},
            )
            return False

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
    def _promote_if_needed(con, table: str) -> bool:
        """Move a pre-raw-schema table from main into raw (idempotent)."""
        from .views import promote_to_raw  # local: views imports only typemap

        return promote_to_raw(con, table)

    @staticmethod
    def _table_exists(con, table: str) -> bool:
        row = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [RAW_SCHEMA, table],
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

    def ensure_table(
        self, object_name: str, fields: list[str] | tuple[str, ...]
    ) -> list[str]:
        """Make sure the object's table exists and has every configured column.

        An object holding zero records in Salesforce never reaches upsert, so
        without this it never gets a table — and every SQL question about it
        fails with "table does not exist". The honest answer to "how many X"
        is 0, not an error. Likewise, when the config grows (say, an admin
        widens field-level security) an object that STILL has no rows would
        keep its old skinny schema forever — so missing configured columns
        are added here too. Existing columns and data are never touched.

        Returns the columns it ADDED to an existing table. A column that has
        just appeared holds NULL for every row already stored, and an
        incremental extract will only ever fill it for records modified from
        now on — so the caller uses this to decide that the object needs one
        full extract. A table created here reports nothing: every column is
        present from the start, and a table with no rows has nothing to
        backfill.
        """
        table = _safe_ident(object_name)
        cols = ", ".join(f'"{_safe_ident(f)}" VARCHAR' for f in fields)
        added: list[str] = []
        with self._connection() as con:
            # BEFORE the CREATE IF NOT EXISTS. On a warehouse that predates
            # the raw schema this object's table still lives in main; creating
            # an empty raw twin first would leave the sync writing rows the
            # app never reads while main stays frozen -- silently. Idempotent
            # and a catalog check once migrated.
            self._promote_if_needed(con, table)
            con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols})')
            existing = self._table_column_types(con, table)
            for f in fields:
                if f not in existing:
                    con.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{_safe_ident(f)}" VARCHAR'
                    )
                    added.append(f)
        return added

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
            # Same guard as ensure_table: never CTAS an empty raw twin beside
            # a populated, unmigrated main table.
            self._promote_if_needed(con, table)
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
