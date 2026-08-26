"""One-shot: give every warehouse table a typed view.

    python -m syncworker.retype --dry-run     # describe everything, write nothing
    python -m syncworker.retype               # migrate + build every view
    python -m syncworker.retype --object Invoice__c

WHAT IT DOES, per object: move main.<obj> into the raw schema (CTAS + DROP in
one transaction) and create main.<obj> as a view that TRY_CASTs each column to
the type Salesforce's describe declares. Rows are never touched; the raw table
keeps the exact strings Salesforce sent.

WHY IT EXISTS SEPARATELY FROM THE SYNC. The sync worker does the same thing
per object as it goes, so a warehouse converts itself over one full cycle
anyway. But a cycle takes as long as it takes, objects convert in sync order,
and the whole warehouse is half-typed in the meantime -- which makes "did this
work?" hard to answer. Running this once converts everything up front, and
`--dry-run` answers the question before anything changes.

COST. One describe call per object. The sync worker already issues exactly
that many per cycle (`clear_describe_cache`), so this is one extra cycle's
worth of metadata calls, not a new order of magnitude. API usage is reported
from the Sforce-Limit-Info header as it goes.

SAFETY. Objects are independent: one failure is logged and the rest continue.
An object that fails keeps its plain VARCHAR table and stays fully queryable,
because a missing view just means the table is still the table.
"""

from __future__ import annotations

import argparse
import logging
import sys

import duckdb

from .config import load_settings
from .jsonlog import setup_logging
from .secrets import fetch_sf_credentials
from .sf_auth import TokenManager
from .sf_client import SalesforceClient
from .storage import RAW_SCHEMA, _relocate_bookkeeping
from .typemap import UnknownSalesforceType, summarize
from .views import build_view_sql, promote_to_raw, refresh_view, table_columns

log = logging.getLogger("syncworker.retype")

#: Sync bookkeeping, not Salesforce objects -- they are relocated by Store,
#: never given a view.
_INTERNAL_PREFIX = "_"


def warehouse_objects(con) -> list[str]:
    """Every business table in the warehouse, from either schema.

    Read from the warehouse rather than config.yaml on purpose: auto-adoption
    (SYNC_AUTO_OBJECTS) means the warehouse routinely holds objects config
    never listed, and those need typing just as much.
    """
    rows = con.execute(
        "SELECT DISTINCT table_name FROM information_schema.tables "
        "WHERE table_schema IN ('main', ?) AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        [RAW_SCHEMA],
    ).fetchall()
    return [str(r[0]) for r in rows if not str(r[0]).startswith(_INTERNAL_PREFIX)]


def plan_object(client, con, object_name: str) -> dict:
    """What typing this object would do. Never writes."""
    specs = client.describe_field_specs(object_name)
    present = table_columns(con, RAW_SCHEMA, object_name) or table_columns(
        con, "main", object_name
    )
    relevant = [s for s in specs if s.name in present]
    counts = summarize(relevant)
    typed = sum(n for t, n in counts.items() if t not in ("VARCHAR", "UNKNOWN"))
    return {
        "object": object_name,
        "columns": len(present),
        "described": len(relevant),
        "typed": typed,
        "types": counts,
        "specs": specs,
    }


def retype_object(client, con, object_name: str, org_timezone: str) -> dict:
    """Migrate one object into raw and (re)build its typed view."""
    plan = plan_object(client, con, object_name)
    promote_to_raw(con, object_name)
    plan["applied"] = refresh_view(con, object_name, plan["specs"], org_timezone)
    return plan


def main(argv=None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m syncworker.retype",
        description="Give every warehouse table a typed view over its raw data.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="describe every object and report what would change; write nothing",
    )
    parser.add_argument(
        "--object", action="append", dest="objects", metavar="NAME",
        help="limit to one object (repeatable)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="stop after N objects (useful for a first look)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    client = SalesforceClient(
        TokenManager(fetch_sf_credentials()), settings.sf_api_version
    )
    # A dry run must not need the write lock: the sync worker holds it for
    # much of every cycle, and "tell me what would change" should never have
    # to wait for -- or interrupt -- a running sync.
    con = duckdb.connect(settings.duckdb_path, read_only=args.dry_run)
    if not args.dry_run:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{RAW_SCHEMA}"')
        # Same relocation Store performs on startup. Without it this tool
        # leaves _sync_meta sitting in main next to 1,010 views -- which the
        # orchestrator's schema slice would then see as a business table.
        _relocate_bookkeeping(con)

    targets = args.objects or warehouse_objects(con)
    if args.limit:
        targets = targets[: args.limit]

    print(
        f"{'DRY RUN — nothing will be written' if args.dry_run else 'RETYPING'}: "
        f"{len(targets)} objects, timezone={settings.sf_org_timezone}",
        file=sys.stderr,
    )

    totals: dict[str, int] = {}
    done = failed = skipped = 0
    unknown_types: dict[str, str] = {}

    for index, name in enumerate(targets, 1):
        try:
            if args.dry_run:
                plan = plan_object(client, con, name)
                # Build the SQL too: a statement that will not compile is a
                # failure worth finding now rather than at write time.
                build_view_sql(
                    name, plan["specs"],
                    table_columns(con, RAW_SCHEMA, name)
                    or table_columns(con, "main", name),
                    org_timezone=settings.sf_org_timezone,
                )
            else:
                plan = retype_object(con=con, client=client, object_name=name,
                                     org_timezone=settings.sf_org_timezone)
        except UnknownSalesforceType as exc:
            failed += 1
            unknown_types[name] = str(exc)
            continue
        except Exception as exc:  # one object must not stop the rest
            failed += 1
            log.error(
                "retype failed for one object; its table is unaffected",
                exc_info=True,
                extra={"event": "retype_failed", "object": name},
            )
            print(f"  !! {name}: {type(exc).__name__}: {exc}"[:160], file=sys.stderr)
            continue

        if plan["typed"] == 0:
            skipped += 1
        else:
            done += 1
        for type_name, count in plan["types"].items():
            totals[type_name] = totals.get(type_name, 0) + count
        if index % 100 == 0:
            print(f"  … {index}/{len(targets)}", file=sys.stderr)

    con.close()

    total_cols = sum(totals.values())
    typed_cols = sum(n for t, n in totals.items() if t not in ("VARCHAR", "UNKNOWN"))
    print(f"\nobjects: {done} typed, {skipped} all-text, {failed} failed")
    print(f"columns: {typed_cols}/{total_cols} gain a real type "
          f"({typed_cols * 100 // total_cols if total_cols else 0}%)\n")
    for type_name, count in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>7}  {type_name}")
    if unknown_types:
        print(f"\n{len(unknown_types)} objects had an unmapped Salesforce type:")
        for name, reason in list(unknown_types.items())[:10]:
            print(f"  {name}: {reason}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
