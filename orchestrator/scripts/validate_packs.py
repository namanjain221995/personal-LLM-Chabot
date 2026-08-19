#!/usr/bin/env python3
"""Mechanical gate for brain packs: never teach a field production does not have.

This is the rule §3 of docs/06-agent-design/SF-BRAIN.md, enforced instead of
remembered. It has run as a scratchpad throwaway twice (the 2026-08-16 KB
ingestion and the 2026-08-19 feature-map ingestion); it lives here now so the
next knowledge drop does not have to reinvent it.

FAILURES — these reach the SQL writer, so they are blocking:

  1. TABLES       — every entry in `tables:` must be a real warehouse table,
     because org_brief pins them into the schema slice the SQL writer sees.
  2. PROSE        — any `Something__c` API name mentioned in `rules`, `metrics`
     or `glossary` that production does NOT have must carry a caveat within a
     couple of lines ("not synced", "not in the warehouse", "sandbox only",
     ...). Knowledge files come from sandboxes that run ahead of production;
     naming a phantom field with no caveat is how silently-wrong SQL happens.
  3. METRIC SQL   — a canonical query may never SELECT a phantom column, with
     or without a caveat.
  4. DUPLICATE KEYS — YAML silently keeps the LAST of two identical keys, so a
     field noted twice loses the first note without any error. Caught this the
     hard way on 2026-08-19: a new Deliverable__c.Due_Date_Sort__c note was
     overwritten by the older one sitting further down the same block.

ADVISORIES — real dead weight, but runtime-harmless, so they do not block:

  4. FIELD NOTES  — a `field_notes.<Object>.<Field>` production lacks is
     silently dropped by `brain.field_overlay` -> `sf_dictionary.merge`, so it
     never reaches a prompt. Worth cleaning; not worth failing a deploy over.
     (`--strict` promotes these to failures.) Eighteen packs carried 32 of
     these when this script was first run on 2026-08-19, all pre-existing.
  5. KNOWLEDGE    — those chunks are exactly where sandbox and implementation
     detail legitimately lives, and they are retrieved as documentation rather
     than as a schema.

Usage
    python scripts/validate_packs.py                       # all packs
    python scripts/validate_packs.py brain/packs/x.yaml    # one pack
    python scripts/validate_packs.py --schema wh.json ...  # offline schema
    python scripts/validate_packs.py --strict              # dead notes fail too

The warehouse schema is read from a JSON file ({"tables": [...],
"columns": {table: [col, ...]}}) when `--schema` is given, otherwise straight
from DuckDB — with a retry loop, because the sync-worker holds the write lock
for a stretch every five minutes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - the container always has it
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PACKS = REPO / "brain" / "packs"
WAREHOUSE = Path("/data/warehouse.duckdb")

API_NAME = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__c)\b")
#: A phantom field is forgiven when one of these sits within CAVEAT_WINDOW lines.
CAVEAT = re.compile(
    r"not synced|not in the (?:\w+ )?warehouse|not in production|no production data|"
    r"not queryable|cannot be queried|cannot be answered from|"
    r"hidden from the integration user|"
    r"does not exist in production|absent from the warehouse|not a warehouse|"
    r"sandbox only|sandbox-only|dev9|dev10|dev11|preprod|repo only|repo-only|"
    r"not populated|never populated|not a column|no such column|"
    r"metadata only|metadata-only|fls|field-level security|caveat",
    re.I,
)
CAVEAT_WINDOW = 3


# ---------------------------------------------------------------------------
# Warehouse schema
# ---------------------------------------------------------------------------

def load_schema(schema_file: Path | None) -> Tuple[Set[str], Dict[str, Set[str]]]:
    if schema_file:
        raw = json.loads(schema_file.read_text())
        return set(raw["tables"]), {k: set(v) for k, v in raw["columns"].items()}
    import duckdb  # imported late: only the container path needs it

    con = None
    for _ in range(120):
        try:
            con = duckdb.connect(str(WAREHOUSE), read_only=True)
            break
        except Exception:
            time.sleep(4)
    if con is None:
        raise SystemExit(
            f"could not open {WAREHOUSE} read-only after 8 minutes — the "
            "sync-worker still holds the lock; retry or pass --schema"
        )
    tables = {r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema='main'"
    ).fetchall()}
    columns: Dict[str, Set[str]] = {}
    for table, column in con.execute(
        "select table_name, column_name from information_schema.columns "
        "where table_schema='main'"
    ).fetchall():
        columns.setdefault(table, set()).add(column)
    return tables, columns


def known_fields(columns: Dict[str, Set[str]]) -> Set[str]:
    """Every custom field name the warehouse has, on ANY object.

    Prose says "Booked_Count__c" without naming its object, so the prose check
    can only be name-level. Object-level precision is the field_notes check's
    job.
    """
    return {c for cols in columns.values() for c in cols if c.endswith("__c")}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

DUP_OBJ = re.compile(r"^  ([A-Za-z]\w*):\s*$")
DUP_FIELD = re.compile(r"^    ([A-Za-z]\w*):")


def duplicate_field_notes(text: str) -> List[str]:
    """Field notes declared twice in the same object block.

    yaml.safe_load keeps the last one and reports nothing, so the loss is
    invisible in every downstream check — it has to be caught in the raw text.
    """
    problems: List[str] = []
    in_notes = False
    obj: str | None = None
    seen: Dict[str, Dict[str, int]] = {}
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.startswith("field_notes:"):
            in_notes = True
            continue
        if in_notes and line and not line.startswith(" "):
            in_notes = False
        if not in_notes:
            continue
        m = DUP_OBJ.match(line)
        if m:
            obj = m.group(1)
            seen.setdefault(obj, {})
            continue
        m = DUP_FIELD.match(line)
        if m and obj:
            field = m.group(1)
            if field in seen[obj]:
                problems.append(
                    f"field_notes: {obj}.{field} is declared twice "
                    f"(lines {seen[obj][field]} and {lineno}); YAML keeps only the last"
                )
            seen[obj][field] = lineno
    return problems


def _is_placeholder(name: str, real: Set[str]) -> bool:
    """`Ref_N_Notified__c` stands for Ref_1..Ref_4 and is not a phantom.

    Packs legitimately write one line about a numbered family of fields rather
    than four. Accept the placeholder when substituting a digit finds a real
    field.
    """
    if "_N_" not in name and not name.endswith("_N__c"):
        return False
    return any(name.replace("_N_", f"_{d}_").replace("_N__c", f"_{d}__c") in real
               for d in "123456789")


#: A knowledge chunk may declare its phantoms once, at the top, instead of
#: caveating each mention — the caveat is chunk-scoped, so the whole chunk is
#: covered for exactly the names the header lists (and no others).
CHUNK_HEADER = re.compile(
    r"\ANOT IN THE PRODUCTION WAREHOUSE[^:]*:(?P<names>.*?)(?:\n\s*\n|\Z)", re.S)


def declared_phantoms(text: str) -> Set[str]:
    m = CHUNK_HEADER.match(text)
    return set(API_NAME.findall(m.group("names"))) if m else set()


def check_prose(label: str, text: str, real: Set[str], tables: Set[str],
                declared: Set[str] | None = None) -> List[str]:
    """Uncaveated mentions of API names production does not have.

    The caveat window is JOINED before matching: pack prose is wrapped, so
    "is NOT in the\n    synced warehouse" must still read as one caveat.
    """
    problems = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for name in API_NAME.findall(line):
            if name in real or name in tables or _is_placeholder(name, real):
                continue
            if declared and name in declared:
                continue
            window = " ".join(lines[max(0, i - CAVEAT_WINDOW): i + CAVEAT_WINDOW + 1])
            if CAVEAT.search(window):
                continue
            problems.append(f"{label}: {name!r} is not in production and has no caveat nearby "
                            f"— {line.strip()[:110]}")
    return problems


def validate(path: Path, tables: Set[str], columns: Dict[str, Set[str]],
             strict: bool = False) -> Tuple[List[str], List[str]]:
    """(failures, advisories) for one pack."""
    raw = path.read_text()
    pack = yaml.safe_load(raw) or {}
    real = known_fields(columns)
    fails: List[str] = []
    advice: List[str] = []

    fails += duplicate_field_notes(raw)

    for table in pack.get("tables") or []:
        if table not in tables:
            fails.append(f"tables: {table!r} is not a warehouse table")

    dead_notes = fails if strict else advice
    for obj, fields in (pack.get("field_notes") or {}).items():
        if not isinstance(fields, dict):
            continue
        if obj not in columns:
            dead_notes.append(f"field_notes: object {obj!r} is not in the warehouse at all "
                              f"({len(fields)} note(s) will be dropped at load)")
            continue
        for field in fields:
            if field not in columns[obj]:
                dead_notes.append(f"field_notes: {obj}.{field} is not a production column "
                                  "(dropped at load)")

    fails += check_prose("rules", str(pack.get("rules") or ""), real, tables)

    for metric in pack.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        name = metric.get("name", "?")
        if metric.get("table") and metric["table"] not in tables:
            fails.append(f"metric {name!r}: table {metric['table']!r} is not in the warehouse")
        blob = "\n".join(str(metric.get(k) or "") for k in ("sql", "definition", "date_column"))
        fails += check_prose(f"metric {name!r}", blob, real, tables)
        # A metric's caveat is allowed to EXPLAIN a phantom field, but its SQL is not.
        for phantom in {n for n in API_NAME.findall(str(metric.get("sql") or ""))
                        if n not in real and n not in tables}:
            fails.append(f"metric {name!r}: SQL selects {phantom!r}, which production does not have")

    for term, meaning in (pack.get("glossary") or {}).items():
        fails += check_prose(f"glossary {term!r}", str(meaning), real, tables)

    for chunk in pack.get("knowledge") or []:
        if not isinstance(chunk, dict):
            continue
        chunk_text = str(chunk.get("text") or "")
        advice += check_prose(f"knowledge {chunk.get('title', '?')!r}",
                              chunk_text, real, tables,
                              declared=declared_phantoms(chunk_text))

    return fails, advice


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("packs", nargs="*", type=Path, help="pack files (default: brain/packs/*.yaml)")
    ap.add_argument("--schema", type=Path, help="warehouse schema JSON instead of live DuckDB")
    ap.add_argument("--advisories", action="store_true", help="also print advisories")
    ap.add_argument("--strict", action="store_true",
                    help="promote dead field notes from advisory to failure")
    args = ap.parse_args()

    paths = args.packs or sorted(DEFAULT_PACKS.glob("*.yaml"))
    if not paths:
        print("no packs found", file=sys.stderr)
        return 2

    tables, columns = load_schema(args.schema)
    print(f"warehouse: {len(tables)} tables, {sum(len(c) for c in columns.values())} columns\n")

    total_fail = 0
    for path in paths:
        fails, advice = validate(path, tables, columns, strict=args.strict)
        total_fail += len(fails)
        status = "FAIL" if fails else "ok"
        print(f"[{status:4s}] {path.name}  ({len(fails)} failure(s), {len(advice)} advisory)")
        for problem in fails:
            print(f"         ! {problem}")
        if args.advisories:
            for problem in advice[:40]:
                print(f"         ~ {problem}")
            if len(advice) > 40:
                print(f"         ~ ... and {len(advice) - 40} more advisories")

    print()
    if total_fail:
        print(f"GATE FAILED: {total_fail} problem(s) across {len(paths)} pack(s)")
        return 1
    print(f"GATE PASSED: {len(paths)} pack(s) name only fields production actually has")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
