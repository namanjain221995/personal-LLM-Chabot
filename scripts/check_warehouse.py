#!/usr/bin/env python3
"""Is the record warehouse shaped the way the pipeline expects?

Read-only. Answers four questions the record-query layer depends on:

  * do `raw` base tables and `main` typed views line up?
  * are the `main` views actually typed, or still VARCHAR?
  * which runtime-schema objects can a question reach today?
  * how current is the replica?

    python scripts/check_warehouse.py
    python scripts/check_warehouse.py --database ~/warehouse.duckdb
    python scripts/check_warehouse.py --unreachable        # list every gap
    python scripts/check_warehouse.py --object Internal_Interview__c
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = "/data/warehouse.read.duckdb"
DEFAULT_SCHEMA_DB = (ROOT / "salesforce_knowledge/runtime_schema/db/production"
                     / "salesforce_runtime_schema.db")

# Bookkeeping the sync keeps in `raw`. A typed view over either would be
# meaningless, so their absence from `main` is correct, not drift.
RAW_ONLY = {"_sync_meta", "_rag_index_pending"}


def tables(connection, schema: str) -> set[str]:
    return {r[0] for r in connection.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = ?", [schema]).fetchall()}


def check_layout(connection) -> int:
    raw, main = tables(connection, "raw"), tables(connection, "main")
    print("LAYOUT")
    print(f"  raw  base tables : {len(raw):>6}")
    print(f"  main typed views : {len(main):>6}")

    unexpected_raw_only = (raw - main) - RAW_ONLY
    view_without_table = main - raw
    print(f"  raw-only (expected bookkeeping) : "
          f"{sorted((raw - main) & RAW_ONLY)}")
    problems = 0
    if unexpected_raw_only:
        print(f"  !! base table with no typed view : "
              f"{sorted(unexpected_raw_only)[:10]}")
        problems += len(unexpected_raw_only)
    if view_without_table:
        print(f"  !! view with no base table : {sorted(view_without_table)[:10]}")
        problems += len(view_without_table)
    if not problems:
        print("  OK - every base table has a typed view, and vice versa")
    return problems


def check_typing(connection, schema_objects: set[str],
                 sample: int = 200) -> int:
    """A `main` view that is still all VARCHAR is not a typed view.

    The record layer coerces filter values to the column's type. Against an
    untyped view a date comparison silently compares strings, which returns
    rows rather than an error -- the worst possible failure.

    Only counts as a problem when a question could reach it. An empty view of
    an object the runtime schema does not describe cannot be queried at all,
    so it is reported and not failed on: a check that cries wolf about 5
    unreachable tables is a check nobody runs.
    """
    rows = connection.execute(
        "SELECT table_name, count(*) FILTER (WHERE data_type <> 'VARCHAR') AS typed,"
        "       count(*) AS total"
        "  FROM information_schema.columns WHERE table_schema = 'main'"
        " GROUP BY table_name ORDER BY total DESC LIMIT ?", [sample]).fetchall()
    untyped = [t for t, typed, total in rows if total > 1 and typed == 0]
    print("\nTYPING")
    print(f"  checked the {len(rows)} widest views in `main`")
    if not untyped:
        print("  OK - every checked view carries real types")
        return 0

    reachable, unreachable = [], []
    for name in untyped:
        count = connection.execute(
            f'SELECT count(*) FROM main."{name}"').fetchone()[0]
        (reachable if (name in schema_objects or count) else unreachable
         ).append((name, count))
    if unreachable:
        print(f"  {len(unreachable)} all-VARCHAR view(s), empty and not in the "
              f"runtime schema - unreachable from a question:")
        for name, _ in unreachable:
            print(f"      {name}")
    if reachable:
        print(f"  !! {len(reachable)} all-VARCHAR view(s) a question CAN reach:")
        for name, count in reachable:
            print(f"      {name}  ({count:,} rows)")
    return len(reachable)


def check_freshness(connection) -> int:
    print("\nFRESHNESS")
    try:
        count, newest, oldest = connection.execute(
            "SELECT count(*), max(updated_at), min(updated_at)"
            "  FROM raw._sync_meta").fetchone()
    except Exception as exc:                            # noqa: BLE001
        print(f"  !! raw._sync_meta unreadable: {exc}")
        return 1
    age = (datetime.now(timezone.utc)
           - newest.replace(tzinfo=timezone.utc)).total_seconds() / 60
    print(f"  objects tracked : {count}")
    print(f"  newest sync     : {newest}  ({age:.1f} minutes ago)")
    print(f"  oldest sync     : {oldest}")
    return 0


def check_reachable(connection, schema_db: Path, show_all: bool) -> int:
    """Which runtime-schema objects a question can actually reach.

    An object in the schema but not the warehouse links cleanly and then fails
    at execution with PHYSICAL_OBJECT_MAPPING_NOT_FOUND. Knowing which ones
    before a user asks is the difference between a bug report and a diagnosis.
    """
    print("\nREACHABILITY")
    if not schema_db.is_file():
        print(f"  runtime schema not built at {schema_db}")
        return 0
    main = tables(connection, "main")
    with sqlite3.connect(schema_db) as sqlite_connection:
        objects = {r[0] for r in
                   sqlite_connection.execute("SELECT api_name FROM objects")}
    reachable = sorted(objects & main)
    missing = sorted(objects - main)
    print(f"  runtime schema objects   : {len(objects):>6}")
    print(f"  answerable from records   : {len(reachable):>6}")
    print(f"  no table in the warehouse : {len(missing):>6}")
    print(f"  warehouse tables the schema does not describe : {len(main - objects)}")
    if missing:
        shown = missing if show_all else missing[:10]
        for name in shown:
            print(f"      {name}")
        if not show_all and len(missing) > len(shown):
            print(f"      ... {len(missing) - len(shown)} more "
                  f"(pass --unreachable for the full list)")
    return 0


def check_object(connection, schema_db: Path, api_name: str) -> int:
    print(f"\nOBJECT  {api_name}")
    columns = connection.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema='main' AND table_name = ? ORDER BY column_name",
        [api_name]).fetchall()
    if not columns:
        print("  no table in the warehouse - a record query on it cannot run")
        return 1
    rows = connection.execute(
        f'SELECT count(*) FROM main."{api_name}"').fetchone()[0]
    print(f"  rows    : {rows:,}")
    print(f"  columns : {len(columns)}")
    if schema_db.is_file():
        with sqlite3.connect(schema_db) as sqlite_connection:
            fields = {r[0].lower() for r in sqlite_connection.execute(
                "SELECT api_name FROM fields WHERE object_api_name = ?",
                (api_name,))}
        physical = {name.lower() for name, _ in columns}
        missing = sorted(fields - physical)
        print(f"  fields in the schema      : {len(fields)}")
        print(f"  schema fields with no column : {len(missing)}")
        for name in missing[:10]:
            print(f"      {name}")
    for name, kind in columns[:15]:
        print(f"    {name:44} {kind}")
    if len(columns) > 15:
        print(f"    ... {len(columns) - 15} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--schema-db", default=str(DEFAULT_SCHEMA_DB))
    parser.add_argument("--unreachable", action="store_true",
                        help="list every schema object with no table")
    parser.add_argument("--object", help="inspect one object in detail")
    args = parser.parse_args(argv)

    try:
        import duckdb
    except ImportError:
        print("error: duckdb is not installed", file=sys.stderr)
        return 1
    path = Path(args.database).expanduser()
    if not path.is_file():
        print(f"error: no warehouse at {path}", file=sys.stderr)
        return 1
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except Exception as exc:                            # noqa: BLE001
        print(f"error: {str(exc).splitlines()[0]}", file=sys.stderr)
        print("hint: open warehouse.read.duckdb, not warehouse.duckdb - "
              "the sync worker holds an exclusive lock on the writable copy",
              file=sys.stderr)
        return 1

    print(f"WAREHOUSE  {path}")
    schema_db = Path(args.schema_db).expanduser()
    try:
        if args.object:
            return check_object(connection, schema_db, args.object)
        schema_objects: set[str] = set()
        if schema_db.is_file():
            with sqlite3.connect(schema_db) as sqlite_connection:
                schema_objects = {r[0] for r in sqlite_connection.execute(
                    "SELECT api_name FROM objects")}
        problems = check_layout(connection)
        problems += check_typing(connection, schema_objects)
        check_freshness(connection)
        check_reachable(connection, schema_db, args.unreachable)
    finally:
        connection.close()
    print("\nOK" if not problems else f"\n{problems} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
