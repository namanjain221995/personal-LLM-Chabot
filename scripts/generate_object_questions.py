#!/usr/bin/env python3
"""Generate test questions for the main Salesforce objects, from real schema.

Input: a JSON dump of objects with their labels, the picklist values that
actually occur in the warehouse, yes/no fields, date fields and sample record
names (produced inside the orchestrator container, where the warehouse is).
Output: one question per line, 10 per object, grouped under `# Object` headers.

Questions use the org's LABELS, never API names -- a user says "onboardings",
not Onboarding__c -- so they test the pipeline the way people will use it.
"""
from __future__ import annotations

import json
import sys


def low(text: str) -> str:
    # Labels carry punctuation meant for a form ("Active CPT?"), not a sentence.
    return " ".join(str(text).replace("?", " ").split()).strip().lower()


def questions_for(o: dict) -> list[str]:
    """Ten questions, one of each kind first, so no single kind crowds out the
    rest on an object with many picklists."""
    plural, single = low(o["plural"]), low(o["label"])
    picks, boxes, dates = o["picklists"], o["checkboxes"], o["dates"]
    other_dates = [d for d in dates if d["field"] != "CreatedDate"]

    def box(c: dict) -> str:
        label = low(c["label"])
        if label.startswith("is "):
            return f"Which {plural} are {label[3:]}?"
        if label.startswith("has "):
            return f"Which {plural} have {label[4:]}?"
        return f"How many {plural} have {label} checked?"

    rounds: list[list[str]] = [
        # round 1: one of every kind
        [f"How many {plural} do we have?",
         f"How many {plural} have {low(picks[0]['label'])} {picks[0]['values'][0]}?" if picks else "",
         box(boxes[0]) if boxes else "",
         f"How many {plural} were created last month?",
         (f"What is the {low(o['texts'][0]['label'])} of {single} {o['names'][0]}?"
          if o["names"] and o["texts"] else
          f"Show the details of {single} {o['names'][0]}." if o["names"] else ""),
         f"How many {plural} have their {low(other_dates[0]['label'])} in 2026?" if other_dates else "",
         f"Break down {plural} by {low(picks[0]['label'])}." if picks else "",
         f"List all {plural}."],
        # round 2: the next of each kind
        [f"Show {plural} where {low(picks[1]['label'])} is {picks[1]['values'][0]}." if len(picks) > 1 else "",
         box(boxes[1]) if len(boxes) > 1 else "",
         f"List {plural} created in September 2026.",
         f"Show the {low(o['texts'][0]['label'])} of all {plural}." if o["texts"] else "",
         f"List {plural} with their {low(o['lookups'][0]['label'])}." if o["lookups"] else "",
         f"How many {plural} have {low(picks[2]['label'])} {picks[2]['values'][0]}?" if len(picks) > 2 else "",
         (f"How many {plural} have {low(picks[0]['label'])} {picks[0]['values'][1]}?"
          if picks and len(picks[0]["values"]) > 1 else "")],
        # padding: only when an object has too few fields for ten
        [f"How many {plural} were created this year?",
         f"How many {plural} were created in 2025?",
         f"List {plural} created this month.",
         f"Show the most recent {plural}.",
         f"How many {plural} were created last week?"],
    ]
    seen: list[str] = []
    for group in rounds:
        for q in group:
            if q and q not in seen:
                seen.append(q)
    return seen[:10]


def main() -> int:
    objects = json.load(open(sys.argv[1], encoding="utf-8"))
    out = [f"# {len(objects) * 10} questions about the {len(objects)} custom objects "
           "with the most fields that have records in the warehouse.",
           "# Generated from the runtime schema and the warehouse's own values.",
           "# One question per line; lines starting with # are skipped."]
    for o in objects:
        out.append("")
        out.append(f"# {o['label']} ({o['api']}) - {o['fields']} fields, {o['rows']} records")
        out += questions_for(o)
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
