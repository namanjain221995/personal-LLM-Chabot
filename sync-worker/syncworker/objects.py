"""Manage which Salesforce objects and fields get synced.

`config.yaml` drives the whole pipeline — Bulk extract, Parquet, DuckDB and the
RAG index all read it, so adding an object needs no code change. Editing it by
hand is easy to get subtly wrong though, and the failure is quiet: a missing
SystemModstamp turns every incremental sync into a full re-extract, and a
rag_field that is not also in fields simply never gets indexed.

This is the safe way to edit it:

    python -m syncworker.objects list
    python -m syncworker.objects add Opportunity \\
        --fields Name,StageName,Amount,CloseDate,AccountId --rag-fields Description
    python -m syncworker.objects add-fields Case --fields Priority,Origin
    python -m syncworker.objects remove Opportunity

Id and SystemModstamp are added automatically — they are not optional, so
asking anyone to remember them is just a way to collect broken configs.

The comment header of config.yaml is preserved verbatim: it documents the file
for whoever opens it next, and a YAML round-trip would silently delete it.
"""
from __future__ import annotations

import argparse
import csv
import collections
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List

import yaml

#: Salesforce API names: a letter, then letters/digits/underscores (Foo__c ok).
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: Required in every object: Id drives the DuckDB upsert, SystemModstamp drives
#: the incremental watermark. Without them the sync still "works" but silently
#: re-extracts everything, every cycle.
REQUIRED_FIELDS = ("Id", "SystemModstamp")

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class ConfigError(Exception):
    """A change that would produce a config the sync worker rejects."""


def _split_header(text: str) -> tuple[str, dict]:
    """Return (comment header, parsed document).

    The header is everything before the `objects:` key. yaml.safe_load discards
    comments, so it is kept as text and re-emitted unchanged.
    """
    marker = re.search(r"^objects:", text, re.M)
    header = text[: marker.start()] if marker else ""
    return header, yaml.safe_load(text) or {}


def load(path: Path = DEFAULT_CONFIG) -> tuple[str, List[dict]]:
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    header, doc = _split_header(path.read_text(encoding="utf-8"))
    objects = doc.get("objects")
    if not isinstance(objects, list):
        raise ConfigError("config.yaml has no top-level 'objects' list")
    return header, objects


def _validate(entry: dict) -> None:
    name = entry.get("name", "")
    if not _IDENT_RE.match(str(name)):
        raise ConfigError(f"invalid object name: {name!r}")
    fields = list(entry.get("fields") or [])
    rag = list(entry.get("rag_fields") or [])
    for f in fields + rag:
        if not _IDENT_RE.match(str(f)):
            raise ConfigError(f"invalid field name for {name}: {f!r}")
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise ConfigError(f"{name}: fields must include {', '.join(missing)}")
    orphan = [f for f in rag if f not in fields]
    if orphan:
        raise ConfigError(
            f"{name}: rag_fields must also appear in fields: {orphan}. "
            "A rag field that is not selected is never fetched, so it would "
            "silently never be indexed."
        )


def _ordered(fields: List[str]) -> List[str]:
    """Id first, SystemModstamp last, the rest in the order given.

    Purely cosmetic, but it makes the required pair obvious in a diff.
    """
    body = [f for f in fields if f not in REQUIRED_FIELDS]
    return ["Id", *body, "SystemModstamp"]


def _dedupe(items) -> List[str]:
    seen, out = set(), []
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def upsert_object(
    objects: List[dict], name: str, fields, rag_fields=(), replace: bool = True
) -> List[dict]:
    """Add an object, or merge fields into an existing one."""
    existing = next((o for o in objects if o.get("name") == name), None)
    if existing is not None and not replace:
        merged_fields = _dedupe([*(existing.get("fields") or []), *fields])
        merged_rag = _dedupe([*(existing.get("rag_fields") or []), *rag_fields])
    else:
        merged_fields = _dedupe(fields)
        merged_rag = _dedupe(rag_fields)

    entry = {"name": name, "fields": _ordered(_dedupe([*REQUIRED_FIELDS, *merged_fields]))}
    if merged_rag:
        entry["rag_fields"] = merged_rag
    _validate(entry)

    if existing is None:
        return [*objects, entry]
    return [entry if o is existing else o for o in objects]


def remove_object(objects: List[dict], name: str) -> List[dict]:
    out = [o for o in objects if o.get("name") != name]
    if len(out) == len(objects):
        raise ConfigError(f"{name} is not in the config")
    if not out:
        raise ConfigError("refusing to remove the last object — the sync would have nothing to do")
    return out


def dump(header: str, objects: List[dict]) -> str:
    body = yaml.safe_dump(
        {"objects": objects}, sort_keys=False, default_flow_style=False, indent=2
    )
    return header + body


def save(header: str, objects: List[dict], path: Path = DEFAULT_CONFIG) -> None:
    for entry in objects:
        _validate(entry)
    path.write_text(dump(header, objects), encoding="utf-8")


# ---------------------------------------------------------------------------
# Importing an org export ("Objects, Fields" spreadsheet)
# ---------------------------------------------------------------------------

#: SOQL can select these, the BULK API cannot — it rejects the whole query.
#: They are silently dropped rather than failing an entire object.
COMPOUND_TYPES = ("address", "location")

#: Long-text types worth chunking and embedding for semantic search.
LONG_TEXT_TYPES = ("textarea", "richtextarea")

#: Beyond this, a single object's SOQL SELECT gets unwieldy and the sync slows
#: for fields nobody asked about. Ranked by usefulness, not truncated blindly.
MAX_FIELDS_PER_OBJECT = 60


def parse_sheet(path: Path) -> Dict[str, List[str]]:
    """Read an "Objects, Fields" CSV into {object: [fields]}.

    The object name appears only on its first row and the rest are blank, so
    the name is carried down. Blank field cells (wrapped long names) are
    skipped rather than becoming empty entries.
    """
    out: Dict[str, List[str]] = collections.OrderedDict()
    current = None
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            name = (row[0] or "").strip() if row else ""
            field = (row[1] or "").strip() if len(row) > 1 else ""
            if name:
                current = name
                out.setdefault(current, [])
            if current and field:
                out[current].append(field)
    return out


def plan_from_sheet(
    sheet: Dict[str, List[str]],
    describe: Callable[[str], dict | None],
    existing: List[dict] | None = None,
) -> tuple[List[dict], List[str]]:
    """Turn a sheet into config entries, keeping only what is really readable.

    A spreadsheet says what EXISTS in the org; `describe` says what this
    integration user may actually SELECT. Writing the sheet verbatim produces a
    config whose every sync fails on INVALID_FIELD, so the two are intersected
    here and the difference is reported rather than hidden.

    Anything ALREADY configured is merged in rather than replaced. An export
    like this is usually a partial list — the first real import dropped Name,
    StageName, CloseDate, Status and Email because the sheet simply did not
    mention them. Nothing broke immediately (DuckDB keeps old columns), so the
    damage would only have shown up later as data that quietly stopped
    refreshing.
    """
    prior: Dict[str, dict] = {e["name"]: e for e in (existing or [])}
    merged_sheet: Dict[str, List[str]] = collections.OrderedDict()
    for name, fields in sheet.items():
        before = prior.get(name, {}).get("fields", [])
        merged_sheet[name] = _dedupe([*before, *fields])
    # Objects configured before but absent from the sheet stay configured.
    for name, entry in prior.items():
        if name not in merged_sheet:
            merged_sheet[name] = list(entry.get("fields", []))

    entries: List[dict] = []
    notes: List[str] = []
    for name, wanted in merged_sheet.items():
        meta = describe(name)
        if meta is None or not meta.get("queryable"):
            notes.append(f"{name}: not readable by this user — skipped")
            continue
        by_name = {f["name"]: f for f in meta.get("fields", [])}

        keep, blocked, compound = [], [], []
        for f in wanted:
            info = by_name.get(f)
            if info is None:
                blocked.append(f)
            elif info.get("type") in COMPOUND_TYPES:
                compound.append(f)
            else:
                keep.append(f)

        if not keep:
            notes.append(
                f"{name}: none of its {len(wanted)} listed fields are visible "
                "to this user (field-level security) — skipped"
            )
            continue
        if len(keep) > MAX_FIELDS_PER_OBJECT:
            notes.append(
                f"{name}: {len(keep)} fields trimmed to {MAX_FIELDS_PER_OBJECT}"
            )
            keep = keep[:MAX_FIELDS_PER_OBJECT]

        rag = [f for f in keep if by_name[f].get("type") in LONG_TEXT_TYPES]
        # Keep any rag field an earlier config chose deliberately.
        rag = _dedupe([*rag, *[f for f in prior.get(name, {}).get("rag_fields", [])
                               if f in keep]])
        if blocked:
            notes.append(f"{name}: {len(blocked)} field(s) not visible to this user")
        if compound:
            notes.append(f"{name}: {len(compound)} compound field(s) unsupported by the Bulk API")

        entry = {"name": name, "fields": _ordered(_dedupe([*REQUIRED_FIELDS, *keep]))}
        if rag:
            entry["rag_fields"] = rag
        _validate(entry)
        entries.append(entry)
    return entries, notes


def _live_describe():
    """A describe function backed by the configured Salesforce credentials."""
    from .secrets import fetch_sf_credentials
    from .sf_auth import TokenManager

    import httpx

    tm = TokenManager(fetch_sf_credentials())
    token, instance = tm.get_token()
    http = httpx.Client(timeout=60.0, headers={"Authorization": f"Bearer {token}"})

    def describe(name: str) -> dict | None:
        resp = http.get(f"{instance}/services/data/v61.0/sobjects/{name}/describe")
        return resp.json() if resp.status_code == 200 else None

    return describe


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _csv(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m syncworker.objects",
        description="Choose which Salesforce objects and fields are synced.",
    )
    # --config on a parent parser so it is accepted BEFORE or AFTER the
    # subcommand; a flag that only works in one position is a papercut.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help="show the configured objects")

    p_add = sub.add_parser("add", parents=[common],
                           help="add an object (replaces it if present)")
    p_add.add_argument("name")
    p_add.add_argument("--fields", type=_csv, required=True,
                       help="comma-separated API names; Id and SystemModstamp are automatic")
    p_add.add_argument("--rag-fields", type=_csv, default=[],
                       help="long-text fields to chunk and embed for semantic search")

    p_fields = sub.add_parser("add-fields", parents=[common],
                              help="add fields to an existing object")
    p_fields.add_argument("name")
    p_fields.add_argument("--fields", type=_csv, required=True)
    p_fields.add_argument("--rag-fields", type=_csv, default=[])

    p_rm = sub.add_parser("remove", parents=[common],
                          help="stop syncing an object")
    p_rm.add_argument("name")

    p_imp = sub.add_parser(
        "import-sheet", parents=[common],
        help="build the config from an 'Objects, Fields' CSV, keeping only "
             "what this Salesforce user can actually read")
    p_imp.add_argument("csv", type=Path)
    p_imp.add_argument("--dry-run", action="store_true",
                       help="report what would change without writing")

    args = parser.parse_args(argv)

    try:
        header, objects = load(args.config)

        if args.command == "list":
            for o in objects:
                rag = o.get("rag_fields") or []
                line = f"  {o['name']:<22} {len(o.get('fields') or []):>3} fields"
                print(line + (f"  ({len(rag)} indexed for search: {', '.join(rag)})" if rag else ""))
            return 0

        if args.command == "import-sheet":
            sheet = parse_sheet(args.csv)
            entries, notes = plan_from_sheet(sheet, _live_describe(), objects)
            kept = sum(len(e["fields"]) for e in entries)
            print(f"  sheet: {len(sheet)} objects, "
                  f"{sum(len(v) for v in sheet.values())} fields")
            print(f"  usable: {len(entries)} objects, {kept} fields")
            for n in notes:
                print(f"    - {n}")
            if not entries:
                raise ConfigError("nothing readable in the sheet — config unchanged")
            if args.dry_run:
                print("  (dry run — nothing written)")
                return 0
            objects = entries
            action = f"imported {len(entries)} objects from {args.csv.name}"
        elif args.command == "add":
            objects = upsert_object(objects, args.name, args.fields, args.rag_fields)
            action = f"configured {args.name}"
        elif args.command == "add-fields":
            if not any(o.get("name") == args.name for o in objects):
                raise ConfigError(f"{args.name} is not configured — use 'add' first")
            objects = upsert_object(
                objects, args.name, args.fields, args.rag_fields, replace=False
            )
            action = f"updated {args.name}"
        else:
            objects = remove_object(objects, args.name)
            action = f"removed {args.name}"

        save(header, objects, args.config)
        print(f"  {action} in {args.config}")
        print("  Restart the sync worker to apply:")
        print("    docker compose up -d --force-recreate sync-worker")
        if args.command != "remove":
            print("  The next cycle does a FULL extract for changed objects, then "
                  "returns to incremental syncs.")
        return 0
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
