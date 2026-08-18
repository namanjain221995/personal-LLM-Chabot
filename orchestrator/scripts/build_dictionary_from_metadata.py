#!/usr/bin/env python3
"""Build a field-dictionary overlay from a Salesforce metadata retrieve.

Input: an SFDX-style objects/ folder (one folder per object, holding
<Object>.object-meta.xml plus fields/*.field-meta.xml and
validationRules/*.validationRule-meta.xml). This is PRODUCTION truth — richer
and more authoritative than any flat export the dictionary was built from:

  * full formulas             -> compact "= ..." gist in the field help
  * roll-up summaries         -> "Roll-up: COUNT(child) where ..." gist,
                                 including the filter items (which is exactly
                                 the "cycles count only sales receipts" class
                                 of fact the model cannot guess)
  * picklist values           -> values
  * lookup/MD targets         -> ref
  * inlineHelpText/description-> help

Output: sf_dictionary-format JSON ({"objects": {...}}) to merge with
`python -m app.core.sf_dictionary <out> --merge [--add-new]`, or consumed by
the deploy recipe in docs (enrich-only first, then add-new gated on real
warehouse columns).

    .venv/bin/python scripts/build_dictionary_from_metadata.py \
        ../brain/sources/prod-metadata/objects --out /tmp/prod_overlay.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}

#: The help stored per field is a budget, not a transcript: the render layer
#: caps at 160 chars, and a 2,000-char formula would drown the label anyway.
_FORMULA_GIST_CHARS = 220
_HELP_CHARS = 300


def _text(node, tag: str) -> str:
    child = node.find(f"sf:{tag}", NS)
    return (child.text or "").strip() if child is not None and child.text else ""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def parse_field(path: Path) -> dict | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"  WARNING: {path} unparseable: {exc}", file=sys.stderr)
        return None
    api = _text(root, "fullName")
    if not api:
        return None
    entry: dict = {
        "api": api,
        "label": _text(root, "label") or api,
        "type": _text(root, "type") or "",
    }

    refs = [r.text.strip() for r in root.findall("sf:referenceTo", NS) if r.text]
    if refs:
        entry["ref"] = refs

    values = [
        v
        for v in (
            _text(value, "fullName")
            for value in root.findall(".//sf:valueSetDefinition/sf:value", NS)
        )
        if v
    ]
    if values:
        entry["values"] = values

    help_parts = []
    help_text = _text(root, "inlineHelpText") or _text(root, "description")
    if help_text:
        help_parts.append(_collapse(help_text))

    formula = _text(root, "formula")
    if formula:
        entry["type"] = entry["type"] or "Formula"
        gist = _collapse(formula)
        if len(gist) > _FORMULA_GIST_CHARS:
            gist = gist[:_FORMULA_GIST_CHARS] + "…"
        help_parts.append(f"FORMULA (read-only): = {gist}")

    operation = _text(root, "summaryOperation")
    if operation:
        summarized = _text(root, "summarizedField")
        foreign = _text(root, "summaryForeignKey")
        child = foreign.split(".")[0] if foreign else "child"
        filters = []
        for item in root.findall("sf:summaryFilterItems", NS):
            filters.append(
                f"{_text(item, 'field').split('.')[-1]} "
                f"{_text(item, 'operation')} {_text(item, 'value')}".strip()
            )
        gist = f"Roll-up (read-only): {operation.upper()}({summarized or child + ' rows'})"
        if filters:
            gist += " where " + " and ".join(filters)
        help_parts.append(_collapse(gist))

    if help_parts:
        entry["help"] = " — ".join(help_parts)[:_HELP_CHARS]
    return entry


def parse_object_label(path: Path) -> str:
    try:
        return _text(ET.parse(path).getroot(), "label")
    except ET.ParseError:
        return ""


def build(root_dir: Path) -> tuple[dict, dict]:
    """→ ({"objects": ...} dictionary overlay, validation-rules by object)."""
    objects: dict = {}
    validations: dict = {}
    for obj_dir in sorted(p for p in root_dir.iterdir() if p.is_dir()):
        api = obj_dir.name
        meta = obj_dir / f"{api}.object-meta.xml"
        label = parse_object_label(meta) if meta.exists() else ""
        fields = []
        for field_file in sorted((obj_dir / "fields").glob("*.field-meta.xml")):
            entry = parse_field(field_file)
            if entry is not None:
                fields.append(entry)
        if fields:
            objects[api] = {"api": api, "label": label or api, "fields": fields}

        rules = []
        for rule_file in sorted((obj_dir / "validationRules").glob("*.validationRule-meta.xml")):
            try:
                rule_root = ET.parse(rule_file).getroot()
            except ET.ParseError:
                continue
            rules.append(
                {
                    "name": _text(rule_root, "fullName"),
                    "active": _text(rule_root, "active") == "true",
                    "error": _collapse(_text(rule_root, "errorMessage")),
                    "description": _collapse(_text(rule_root, "description")),
                    "condition": _collapse(_text(rule_root, "errorConditionFormula"))[:300],
                }
            )
        if rules:
            validations[api] = rules
    return {"objects": objects}, validations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="the objects/ folder of a metadata retrieve")
    parser.add_argument("--out", required=True, help="overlay JSON output path")
    parser.add_argument("--validations-out", help="validation-rules JSON output path")
    args = parser.parse_args()

    overlay, validations = build(Path(args.root))
    Path(args.out).write_text(json.dumps(overlay), encoding="utf-8")
    objects = overlay["objects"]
    fields = sum(len(o["fields"]) for o in objects.values())
    enriched = sum(
        1 for o in objects.values() for f in o["fields"]
        if f.get("help") or f.get("values") or f.get("ref")
    )
    formulas = sum(
        1 for o in objects.values() for f in o["fields"]
        if "FORMULA" in (f.get("help") or "") or "Roll-up" in (f.get("help") or "")
    )
    print(f"  {len(objects)} objects, {fields} fields ({enriched} enriched, "
          f"{formulas} formula/roll-up gists) → {args.out}")
    if args.validations_out:
        Path(args.validations_out).write_text(json.dumps(validations, indent=1), encoding="utf-8")
        active = sum(1 for rules in validations.values() for r in rules if r["active"])
        print(f"  {sum(len(r) for r in validations.values())} validation rules "
              f"({active} active) across {len(validations)} objects → {args.validations_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
