#!/usr/bin/env python3
"""Export the canonical trace store to CSV and DuckDB.

    python scripts/export_traces.py
    python scripts/export_traces.py --format csv
    python scripts/export_traces.py --database /data/traces/knowledge_traces.sqlite
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from observability.config import load_tracing_config           # noqa: E402
from observability.export import export_traces                 # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None,
                        help="trace store to export (default: configured)")
    parser.add_argument("--format", action="append", dest="formats",
                        choices=["csv", "duckdb"], default=None)
    parser.add_argument("--knowledge-root", default=str(ROOT / "salesforce_knowledge"))
    args = parser.parse_args(argv)

    config = load_tracing_config(args.knowledge_root)
    database = args.database
    if database is None:
        # Fall back to a store that exists, so an export run before the move
        # still finds the traces rather than reporting none.
        for candidate in config.candidate_databases():
            if candidate.is_file():
                database = str(candidate)
                break
    try:
        result = export_traces(config, database=database,
                               formats=args.formats or ("csv", "duckdb"))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
