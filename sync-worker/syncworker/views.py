"""Typed views over the raw VARCHAR warehouse.

THE SHAPE. Every object exists twice:

    raw."Interview__c"    TABLE -- every column VARCHAR, exactly what Salesforce
                          sent. The upsert target. Never read by the app.
    main."Interview__c"   VIEW  -- the same columns, TRY_CAST to the types
                          describe declares. What the SQL engine queries.

The orchestrator needs NO changes for this: `schema_cache._load()` already
filters `information_schema.columns` to `table_schema = 'main'`, so it sees the
views and their real types; and the model's own `FROM Interview__c` resolves to
main by search path. The prompt's cast rules become deletable because the
schema now states the types.

WHY THE VIEW AND NOT THE TABLE. TRY_CAST at read time turns an unparseable
value into NULL for that one cell while the original string stays readable in
raw -- so a bad mapping is diagnosable and reversible. Casting at write time
destroys the evidence permanently, and re-typing a column later would mean a
migration across a thousand tables instead of a regenerate.

DuckDB note: ALTER TABLE ... SET SCHEMA is not implemented (1.5.5), so
`promote_to_raw` moves an existing table by CTAS + DROP inside one transaction.
On an empty warehouse that is instant.
"""

from __future__ import annotations

import logging

from .typemap import (FieldSpec, UnknownSalesforceType, duckdb_type,
                      plan_columns, summarize, unknown_types)

log = logging.getLogger("syncworker.views")

#: Schema holding the physical VARCHAR tables the sync writes.
RAW_SCHEMA = "raw"
#: Schema holding the typed views the application reads. `main` is DuckDB's
#: default, which is what makes the orchestrator's unqualified `FROM X` work.
VIEW_SCHEMA = "main"


def _q(identifier: str) -> str:
    """Quote an identifier for DuckDB, escaping embedded quotes."""
    return '"' + str(identifier).replace('"', '""') + '"'


def build_view_sql(
    object_name: str,
    specs: list[FieldSpec] | tuple[FieldSpec, ...],
    present: set[str] | frozenset[str],
    *,
    org_timezone: str | None = None,
    raw_schema: str = RAW_SCHEMA,
    view_schema: str = VIEW_SCHEMA,
) -> str | None:
    """CREATE OR REPLACE VIEW statement, or None when there is nothing to type.

    `present` is the set of columns the raw table actually has. describe
    reports fields the sync skipped -- field-level security, compound types,
    the SYNC_MAX_FIELDS ceiling -- and selecting a missing column is a binder
    error at view-creation time, which would take the whole object down.
    """
    columns = plan_columns(specs, present, org_timezone)
    if not columns:
        return None

    # Columns in the table that describe did not describe (a field dropped from
    # the org since the last sync, or one adopted before this describe ran).
    # Carry them through untyped rather than silently dropping data.
    planned = {spec.name for spec in specs}
    extras = [_q(c) for c in sorted(present) if c not in planned]

    body = ",\n".join("  " + expression for expression in columns + extras)
    return (
        f"CREATE OR REPLACE VIEW {_q(view_schema)}.{_q(object_name)} AS SELECT\n"
        f"{body}\n"
        f"FROM {_q(raw_schema)}.{_q(object_name)};"
    )


def table_columns(con, schema: str, table: str) -> set[str]:
    """Column names of one table, or an empty set when it does not exist."""
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchall()
    return {str(r[0]) for r in rows}


def _relation_kind(con, schema: str, table: str) -> str | None:
    """'BASE TABLE', 'VIEW', or None."""
    row = con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    return str(row[0]) if row else None


def promote_to_raw(con, object_name: str, *, _in_transaction: bool = False) -> bool:
    """One-time move of main.<obj> (a TABLE) into the raw schema.

    Idempotent: does nothing when raw already holds the table, or when main
    holds a view rather than a table. Returns True when it moved something.

    Runs as one transaction because between the DROP and the CREATE VIEW the
    object would otherwise not exist for readers.
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(RAW_SCHEMA)}")
    if _relation_kind(con, RAW_SCHEMA, object_name) is not None:
        return False
    if _relation_kind(con, VIEW_SCHEMA, object_name) != "BASE TABLE":
        return False

    # `_in_transaction` lets refresh_view wrap the move and the CREATE VIEW
    # together, so a view that fails to build rolls the move back with it.
    if not _in_transaction:
        con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            f"CREATE TABLE {_q(RAW_SCHEMA)}.{_q(object_name)} AS "
            f"SELECT * FROM {_q(VIEW_SCHEMA)}.{_q(object_name)}"
        )
        con.execute(f"DROP TABLE {_q(VIEW_SCHEMA)}.{_q(object_name)}")
        # A passthrough view in the SAME transaction, so main.<obj> exists at
        # every instant: the promote can run at the head of a sync (before
        # the extract) while the typed view is only built at the tail, and
        # in between the orchestrator must still find the object. refresh_view
        # replaces this with the typed view via CREATE OR REPLACE.
        # Explicit column list, never SELECT *: DuckDB re-binds a view when it
        # is queried, and a `*` view whose table has since gained a column
        # (ensure_table adds them at the head of a sync) fails with "Contents
        # of view were altered" until it is rebuilt. Named columns just leave
        # the new one unexposed until the tail's typed rebuild picks it up.
        columns = ", ".join(_q(c) for c in sorted(table_columns(con, RAW_SCHEMA, object_name)))
        con.execute(
            f"CREATE VIEW {_q(VIEW_SCHEMA)}.{_q(object_name)} AS "
            f"SELECT {columns} FROM {_q(RAW_SCHEMA)}.{_q(object_name)}"
        )
        if not _in_transaction:
            con.execute("COMMIT")
    except Exception:
        if not _in_transaction:
            con.execute("ROLLBACK")
        raise
    log.info(
        "warehouse table promoted to raw schema",
        extra={"event": "table_promoted", "object": object_name},
    )
    return True


def planned_columns(
    specs: list[FieldSpec] | tuple[FieldSpec, ...],
    present: set[str] | frozenset[str],
    org_timezone: str | None = None,
) -> list[tuple[str, str]]:
    """[(column, DuckDB type)] the view WOULD expose, in order.

    Written to be comparable with what `information_schema` reports for an
    existing view, which is what makes skip-if-unchanged possible.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for spec in specs:
        if not spec.name or spec.name in seen or spec.name not in present:
            continue
        seen.add(spec.name)
        try:
            # Unknown types fall back to text, matching cast_expression --
            # the two must agree or skip-if-unchanged would rebuild forever.
            target = duckdb_type(spec)
        except UnknownSalesforceType:
            target = None
        # org_timezone deliberately does not appear here: converting a
        # datetime changes the VALUE, not the column type, so it cannot make
        # a view stale on its own.
        out.append((spec.name, target or "VARCHAR"))
    for extra in sorted(c for c in present if c not in seen):
        out.append((extra, "VARCHAR"))
    return out


def view_timezone_matches(con, object_name: str, org_timezone: str | None) -> bool:
    """Does the stored view definition already use the configured timezone?

    Column types cannot answer this: converting a datetime changes the VALUE,
    not the type, so a timezone change would slip past a types-only staleness
    check and the new setting would silently do nothing. DuckDB keeps the view
    definition (normalising `AT TIME ZONE` to `timezone(...)`), which is the
    real source of truth for what the view is currently doing.
    """
    row = con.execute(
        "SELECT sql FROM duckdb_views() "
        "WHERE schema_name = ? AND view_name = ?",
        [VIEW_SCHEMA, object_name],
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = str(row[0])
    wants_conversion = bool(org_timezone) and org_timezone.upper() != "UTC"
    has_conversion = "timezone(" in sql.lower()
    if not wants_conversion:
        return not has_conversion
    return f"timezone('{org_timezone}'".lower() in sql.lower()


def existing_view_columns(con, object_name: str) -> list[tuple[str, str]] | None:
    """[(column, type)] of the current main.<obj> VIEW, or None if it is not one."""
    if _relation_kind(con, VIEW_SCHEMA, object_name) != "VIEW":
        return None
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [VIEW_SCHEMA, object_name],
    ).fetchall()
    return [(str(c), str(t)) for c, t in rows]


def refresh_view(
    con,
    object_name: str,
    specs: list[FieldSpec] | tuple[FieldSpec, ...],
    org_timezone: str | None = None,
    *,
    force: bool = False,
) -> bool:
    """Migrate the object into raw if needed and (re)build its typed view.

    ATOMIC BY DESIGN. The promote and the CREATE VIEW run in ONE transaction,
    because doing them in sequence has a hole: promote moves main.<obj> into
    raw and drops it, and if the view then fails to build the object has no
    relation in main at all -- rows intact in raw, but unreachable to every
    query. One transaction means a failed view rolls the move back too, and
    the object is left exactly as it was.

    Never raises on a per-object problem: a failure leaves the object queryable
    and the rest of the warehouse untouched.

    Returns True when a view is in place afterwards -- including when it was
    already correct and nothing needed doing.
    """
    try:
        present = table_columns(con, RAW_SCHEMA, object_name) or table_columns(
            con, VIEW_SCHEMA, object_name
        )
        if not present:
            return False

        # Skip when the view already exposes exactly these columns and types.
        # A cycle over ~1,000 objects otherwise issues ~1,000 CREATE OR REPLACE
        # VIEW statements every few minutes, each taking the write lock the
        # orchestrator's reads are competing for -- for no change at all.
        planned = planned_columns(specs, present, org_timezone)
        if (
            not force
            and existing_view_columns(con, object_name) == planned
            and view_timezone_matches(con, object_name, org_timezone)
        ):
            return True

        sql = build_view_sql(object_name, specs, present,
                             org_timezone=org_timezone)
        if sql is None:
            return False

        con.execute("BEGIN TRANSACTION")
        try:
            promote_to_raw(con, object_name, _in_transaction=True)
            con.execute(sql)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception:
        log.error(
            "typed view rebuild failed; the object is unchanged and queryable",
            exc_info=True,
            extra={"event": "view_refresh_failed", "object": object_name},
        )
        return False

    counts = summarize([s for s in specs if s.name in present])
    typed = sum(n for t, n in counts.items() if t not in ("VARCHAR", "UNKNOWN"))
    unmapped = unknown_types(specs, present)
    if unmapped:
        # The columns still work as text; this says which type map entry is
        # missing, so an unanticipated Salesforce type is loud rather than a
        # quiet loss of typing.
        log.error(
            "unmapped Salesforce types kept as VARCHAR; add them to typemap",
            extra={
                "event": "view_type_unknown",
                "object": object_name,
                "types": {t: len(f) for t, f in unmapped.items()},
                "fields": {t: f[:5] for t, f in unmapped.items()},
            },
        )
    log.info(
        "typed view refreshed",
        extra={
            "event": "view_refreshed",
            "object": object_name,
            "columns": sum(counts.values()),
            "typed_columns": typed,
            "types": counts,
        },
    )
    return True
