#!/usr/bin/env python3
"""Read one request out of the trace store, in the order it happened.

    python scripts/inspect_trace.py --latest 10
    python scripts/inspect_trace.py --trace-id kt_8b4604bc7f4d4871b997b003d53d7f1f
    python scripts/inspect_trace.py --trace-id kt_... --payloads

Read-only. It opens the store with mode=ro so inspecting a request can never
change what the request recorded.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from observability.config import load_tracing_config           # noqa: E402

STAGE_SUMMARY = (
    ("1 extraction", ("extraction_model", "extraction_endpoint",
                      "extraction_mode", "extraction_ms")),
    ("2 routing", ("route", "ir_family", "ir_output_mode", "ir_capabilities")),
    ("3 discovery", ("schema_served_from", "schema_objects")),
    ("4 linking", ("grounded_ok", "grounded_primary_object", "linker_mode",
                   "linker_decided_by")),
    ("5 record query", ("record_row_count", "record_param_count", "record_ms",
                        "record_error")),
    ("6 answer", ("answer_model", "answer_result_type", "answer_grounded",
                  "answer_regenerated", "answer_fallback", "answer_ms")),
)

DURATIONS = ("extraction_duration_ms", "discovery_duration_ms",
             "linking_duration_ms", "record_query_duration_ms",
             "answer_duration_ms", "total_duration_ms")


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _store(explicit: str | None, knowledge_root: str) -> Path:
    if explicit:
        return Path(explicit)
    config = load_tracing_config(knowledge_root)
    for candidate in config.candidate_databases():
        if candidate.is_file():
            return candidate
    return config.database


def _short(value: Any, width: int = 70) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def show_latest(connection: sqlite3.Connection, limit: int) -> None:
    rows = connection.execute(
        "SELECT trace_id, started_at, status, route, environment,"
        " total_duration_ms, question FROM trace"
        " ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("no traces")
        return
    print(f"{'trace_id':36} {'status':8} {'route':24} {'ms':>7}  question")
    print("-" * 110)
    for row in rows:
        print(f"{row['trace_id']:36} {str(row['status'] or ''):8} "
              f"{str(row['route'] or ''):24} "
              f"{str(row['total_duration_ms'] or ''):>7}  "
              f"{_short(row['question'], 34)}")


def show_trace(connection: sqlite3.Connection, trace_id: str,
               payloads: bool) -> int:
    row = connection.execute("SELECT * FROM trace WHERE trace_id=?",
                             (trace_id,)).fetchone()
    if row is None:
        print(f"no trace {trace_id}", file=sys.stderr)
        return 1
    trace = dict(row)

    print("TRACE SUMMARY")
    print(f"  trace_id       : {trace['trace_id']}")
    print(f"  question       : {trace.get('question')}")
    print(f"  environment    : {trace.get('environment')}")
    print(f"  route          : {trace.get('route')}")
    print(f"  status         : {trace.get('status')}")
    print(f"  started_at     : {trace.get('started_at')}")
    print(f"  completed_at   : {trace.get('completed_at')}")
    print(f"  schema_version : {trace.get('trace_schema_version')}")

    print("\nSTAGES")
    for label, columns in STAGE_SUMMARY:
        filled = [(c, trace.get(c)) for c in columns]
        missing = [c for c, v in filled if v in (None, "", "[]")]
        print(f"  {label:16} " + "  ".join(
            f"{c}={_short(v, 34)}" for c, v in filled if v not in (None, "", "[]")))
        if missing:
            print(f"  {'':16} (empty: {', '.join(missing)})")

    # The semantic-IR path stores the question's meaning as the extraction.
    try:
        ir = json.loads(trace.get("extraction_json") or "null") or {}
    except (TypeError, ValueError):
        ir = {}
    if ir.get("family"):
        print("\nMEANING (semantic IR)")
        print(f"  family / mode  : {ir.get('family')} / {ir.get('output_mode')}"
              f"{'  (retried)' if ir.get('retried') else ''}")
        print(f"  capabilities   : {ir.get('capabilities')}")
        print(f"  entities       : " + ", ".join(
            f"{e.get('ref')}={e.get('concept')}({e.get('role')})"
            for e in ir.get("entities") or []))
        for key in ("filters", "measures", "dimensions", "temporal", "comparison",
                    "derived", "existence", "schema"):
            if ir.get(key):
                print(f"  {key:14} : {_short(json.dumps(ir[key], default=str), 200)}")

    print("\nDURATIONS (ms)")
    for column in DURATIONS:
        value = trace.get(column)
        if value is not None:
            print(f"  {column:26} {value}")

    events = connection.execute(
        "SELECT sequence_number, stage, status, duration_ms, component, details"
        " FROM trace_event WHERE trace_id=? ORDER BY sequence_number",
        (trace_id,)).fetchall()
    print(f"\nEVENTS ({len(events)})")
    for event in events:
        duration = "" if event["duration_ms"] is None else f"{event['duration_ms']} ms"
        print(f"  {event['sequence_number']:02d} {event['stage']:40} "
              f"{event['status']:8} {duration}")
        if payloads and event["details"]:
            try:
                detail = json.loads(event["details"])
            except json.JSONDecodeError:
                detail = event["details"]
            for line in json.dumps(detail, indent=2, sort_keys=True).splitlines():
                print(f"       {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-id")
    parser.add_argument("--latest", type=int, default=0)
    parser.add_argument("--database")
    parser.add_argument("--payloads", action="store_true",
                        help="print each event's details")
    parser.add_argument("--knowledge-root", default=str(ROOT / "salesforce_knowledge"))
    args = parser.parse_args(argv)

    path = _store(args.database, args.knowledge_root)
    if not path.is_file():
        print(f"no trace store at {path}", file=sys.stderr)
        return 1
    connection = _open(path)
    try:
        if args.trace_id:
            return show_trace(connection, args.trace_id, args.payloads)
        show_latest(connection, args.latest or 10)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
