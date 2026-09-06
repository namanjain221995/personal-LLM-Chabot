#!/usr/bin/env python3
"""Prove a FRESH install reaches the same schema as an UPGRADE from an old one.

`orchestrator/app/db.py` carries migrations V1..VN and applies the unapplied
ones at startup. Two databases must therefore be indistinguishable in shape:

  * FRESH    — an empty database that runs V1..VN in one go, which is what a
               new deployment, a new developer and every CI service container
               gets; and
  * UPGRADED — a database stopped at some older version VK, then brought
               forward to VN, which is what THIS box gets on every deploy.

They drift whenever a migration is edited in place instead of appended, or
when a later migration assumes a shape only the fresh path produces. The
symptom is always the same and always in production only: a column, index or
constraint that exists on new installs and not on the one that matters.

Subcommands
-----------
  stage --to K      apply only migrations V1..VK to $APP_DATABASE_URL, using
                    the real init_schema() with the migration table truncated
                    to its first K entries. This is the "old deployment".
  upgrade           apply every migration to $APP_DATABASE_URL (the real
                    startup path, unmodified).
  compare --a DSN --b DSN
                    dump both schemas structurally and diff them.

The comparison is STRUCTURAL, never row data: migrations legitimately backfill
different rows down the two paths (V23 heals pages only an old database has).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import sys

# --------------------------------------------------------------------------
# Structural dump. Deliberately uses psycopg directly rather than app.db, so
# `compare` has no dependency on the application's pool or settings and can
# look at two databases in one process.
# --------------------------------------------------------------------------

#: Every query is ordered, so the dumps are comparable line by line. `public`
#: only: nothing this app owns lives anywhere else.
QUERIES: dict[str, str] = {
    "columns": """
        SELECT table_name, column_name, ordinal_position, data_type,
               COALESCE(character_maximum_length, -1) AS len,
               COALESCE(numeric_precision, -1)        AS prec,
               is_nullable, COALESCE(column_default, '') AS col_default,
               is_identity, COALESCE(identity_generation, '') AS identity_generation
          FROM information_schema.columns
         WHERE table_schema = 'public'
         ORDER BY table_name, ordinal_position
    """,
    "tables": """
        SELECT table_name, table_type
          FROM information_schema.tables
         WHERE table_schema = 'public'
         ORDER BY table_name
    """,
    "constraints": """
        SELECT c.relname AS table_name, con.conname, con.contype,
               pg_get_constraintdef(con.oid) AS definition
          FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
         ORDER BY c.relname, con.conname
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef
          FROM pg_indexes
         WHERE schemaname = 'public'
         ORDER BY tablename, indexname
    """,
    "sequences": """
        SELECT sequence_name, data_type, start_value, increment
          FROM information_schema.sequences
         WHERE sequence_schema = 'public'
         ORDER BY sequence_name
    """,
    "views": """
        SELECT table_name, view_definition
          FROM information_schema.views
         WHERE table_schema = 'public'
         ORDER BY table_name
    """,
    "triggers": """
        SELECT event_object_table, trigger_name, action_timing,
               event_manipulation, action_statement
          FROM information_schema.triggers
         WHERE trigger_schema = 'public'
         ORDER BY event_object_table, trigger_name, event_manipulation
    """,
}


def dump(dsn: str) -> list[str]:
    import psycopg
    from psycopg.rows import dict_row

    lines: list[str] = []
    with psycopg.connect(dsn, row_factory=dict_row) as con:
        versions = [
            int(r["version"])
            for r in con.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        lines.append(f"[schema_migrations] applied = {versions}")
        for section, sql in QUERIES.items():
            lines.append(f"--- {section} ---")
            for row in con.execute(sql).fetchall():
                lines.append(
                    f"{section}: "
                    + " | ".join(f"{k}={row[k]!r}" for k in sorted(row))
                )
    return lines


# --------------------------------------------------------------------------
# Migration driving. Imports the REAL app.db so the thing under test is the
# code that runs in production, not a reimplementation of it.
# --------------------------------------------------------------------------


def _load_db(orchestrator: pathlib.Path):
    sys.path.insert(0, str(orchestrator))
    import app.db as db  # noqa: E402  (path has to be set first)

    return db


def cmd_stage(args) -> int:
    db = _load_db(args.orchestrator)
    full = db._MIGRATIONS
    known = [v for v, _ in full]
    if args.to not in known:
        print(f"--to {args.to} is not a migration version; have {known}", file=sys.stderr)
        return 2
    # Truncate the migration table to V1..VK and run the ordinary startup path.
    # Nothing about init_schema() is stubbed: the same advisory lock, the same
    # transaction, the same schema_migrations bookkeeping.
    db._MIGRATIONS = tuple((v, d) for v, d in full if v <= args.to)
    db.init_schema()
    got = db.schema_version()
    db._MIGRATIONS = full
    print(f"staged an OLD database at schema version {got} (asked for {args.to})")
    return 0 if got == args.to else 1


def cmd_upgrade(args) -> int:
    db = _load_db(args.orchestrator)
    before = db.schema_version()
    db.init_schema()
    after = db.schema_version()
    latest = db.LATEST_SCHEMA_VERSION
    print(f"upgraded {before} -> {after} (LATEST_SCHEMA_VERSION={latest})")
    if after != latest:
        print(f"init_schema() left the database at {after}, not {latest}", file=sys.stderr)
        return 1
    return 0


def cmd_invariants(args) -> int:
    """Cheap checks on the migration table itself, no database required."""
    db = _load_db(args.orchestrator)
    versions = [v for v, _ in db._MIGRATIONS]
    problems = []
    if versions != sorted(versions):
        problems.append(f"migrations are not in ascending order: {versions}")
    if len(set(versions)) != len(versions):
        problems.append(f"duplicate migration versions: {versions}")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        problems.append(f"migration versions are not contiguous 1..N: {versions}")
    if db.LATEST_SCHEMA_VERSION != max(versions):
        problems.append(
            f"LATEST_SCHEMA_VERSION={db.LATEST_SCHEMA_VERSION} but the highest "
            f"migration is {max(versions)}"
        )
    for version, ddl in db._MIGRATIONS:
        if not str(ddl).strip():
            problems.append(f"V{version} has an empty body")
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"migration table OK: V1..V{max(versions)}, contiguous, unique, non-empty")
    return 0


def cmd_compare(args) -> int:
    a = dump(args.a)
    b = dump(args.b)
    if a == b:
        print(
            f"schemas are IDENTICAL ({len(a)} structural lines compared)\n"
            f"  A (fresh install)      = {args.a.rsplit('@', 1)[-1]}\n"
            f"  B (staged then upgraded) = {args.b.rsplit('@', 1)[-1]}"
        )
        return 0
    diff = list(
        difflib.unified_diff(a, b, fromfile="fresh-install", tofile="upgraded", lineterm="")
    )
    print("FRESH INSTALL AND UPGRADE PRODUCED DIFFERENT SCHEMAS", file=sys.stderr)
    for line in diff[:400]:
        print(line, file=sys.stderr)
    if len(diff) > 400:
        print(f"... {len(diff) - 400} more diff lines", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### ❌ Schema drift: fresh install != upgrade\n\n```diff\n")
            fh.write("\n".join(diff[:200]))
            fh.write("\n```\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orchestrator",
        type=pathlib.Path,
        default=pathlib.Path("orchestrator"),
        help="path to the orchestrator package root (contains app/db.py)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stage", help="apply only V1..VK to $APP_DATABASE_URL")
    p.add_argument("--to", type=int, required=True)
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("upgrade", help="apply every migration to $APP_DATABASE_URL")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("invariants", help="check the migration table (no database)")
    p.set_defaults(func=cmd_invariants)

    p = sub.add_parser("compare", help="structurally diff two databases")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
