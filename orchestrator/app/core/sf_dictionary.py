"""The org's field dictionary: what users CALL things → what the API calls them.

People ask for "interview status", "close date", "annual revenue". Salesforce
wants `Interview_Status__c`, `CloseDate`, `AnnualRevenue`. Without a mapping the
model guesses — and its guesses look right, which is the dangerous part:
`WHERE Status__c = ...` on an object whose field is `Interview_Status__c` does
not error, it just quietly finds nothing.

The dictionary is built once from an org export (Object API Name, Object Label,
Field API Name, Field Label, Field Type) and stored in the data volume, so it
survives restarts and costs nothing per request.

It is NOT injected wholesale — an org this size is far too large for a prompt.
Each question retrieves only the objects it plausibly refers to.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import settings

DICTIONARY_PATH = os.environ.get(
    "SF_DICTIONARY_PATH", "/data/sf_dictionary.json"
)

#: Objects whose fields are injected for one question. More than a handful and
#: the prompt grows faster than the accuracy does.
MAX_OBJECTS = 4
MAX_FIELDS_PER_OBJECT = 60

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
#: Words that match everything and therefore rank nothing.
_STOP = {
    "the", "and", "for", "with", "from", "how", "many", "what", "which", "show",
    "give", "list", "all", "our", "are", "is", "in", "of", "to", "me", "data",
    "record", "records", "salesforce", "object", "objects", "field", "fields",
    "count", "total", "last", "this", "that", "get", "please", "name", "names",
}

_cache: Optional[Dict[str, Any]] = None


def _tokens(text: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(text or "")} - _STOP


def build_from_rows(rows) -> Dict[str, Any]:
    """rows: (object_api, object_label, field_api, field_label, field_type)."""
    objects: Dict[str, Any] = {}
    for row in rows:
        if not row or not row[0]:
            continue
        obj_api = str(row[0]).strip()
        obj_label = str(row[1] or obj_api).strip()
        entry = objects.setdefault(
            obj_api, {"api": obj_api, "label": obj_label, "fields": []}
        )
        if len(row) > 2 and row[2]:
            entry["fields"].append(
                {
                    "api": str(row[2]).strip(),
                    "label": str(row[3] or row[2]).strip(),
                    "type": str(row[4] or "").strip(),
                }
            )
    return {"objects": objects}


def save(data: Dict[str, Any], path: str = DICTIONARY_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    global _cache
    _cache = data


def load(path: str = DICTIONARY_PATH) -> Dict[str, Any]:
    """Cached, and never fatal — a missing dictionary just means no hints."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        _cache = {"objects": {}}
    return _cache


def available() -> bool:
    return bool(load().get("objects"))


def _score(question_tokens: set, obj: Dict[str, Any]) -> int:
    """How strongly this object matches the question.

    Object names score highest — the question naming "Interview" should pull
    Interview__c ahead of every object that merely has a Status field.
    """
    score = 0
    obj_tokens = _tokens(obj["api"]) | _tokens(obj["label"])
    score += 6 * len(question_tokens & obj_tokens)
    for f in obj["fields"]:
        if question_tokens & (_tokens(f["api"]) | _tokens(f["label"])):
            score += 1
    return score


def relevant_objects(question: str, limit: int = MAX_OBJECTS) -> List[Dict[str, Any]]:
    data = load()
    tokens = _tokens(question)
    if not tokens:
        return []
    scored = [
        (s, o) for o in data.get("objects", {}).values() if (s := _score(tokens, o)) > 0
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["api"]))
    return [o for _s, o in scored[:limit]]


def hint_for(question: str) -> str:
    """A compact API-name reference for the objects this question mentions.

    Empty when nothing matches, so the prompt is unchanged for questions the
    dictionary cannot help with.
    """
    picked = relevant_objects(question)
    if not picked:
        return ""
    blocks = []
    for obj in picked:
        fields = obj["fields"][:MAX_FIELDS_PER_OBJECT]
        listed = "; ".join(
            f"{f['api']} = \"{f['label']}\" ({f['type']})" if f["label"] != f["api"]
            else f"{f['api']} ({f['type']})"
            for f in fields
        )
        more = "" if len(obj["fields"]) <= len(fields) else f" … +{len(obj['fields']) - len(fields)} more"
        blocks.append(f'{obj["api"]} = "{obj["label"]}"\n  {listed}{more}')
    return (
        "Salesforce API names for the objects this question mentions "
        '(API_NAME = "the label a user would say"):\n'
        + "\n".join(blocks)
        + "\nUse the API names exactly as written above. A field name that "
        "looks plausible but is wrong returns no rows instead of an error."
    )


def build_from_xlsx(path: str) -> Dict[str, Any]:
    """Read an org export workbook (openpyxl, streaming — these are large)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    next(rows, None)  # header
    return build_from_rows(rows)


def build_from_csv(path: str) -> Dict[str, Any]:
    import csv

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        return build_from_rows(reader)


def main(argv=None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.core.sf_dictionary",
        description="Load an org export so the model uses real API names.",
    )
    parser.add_argument("path")
    parser.add_argument("--out", default=DICTIONARY_PATH)
    args = parser.parse_args(argv)

    builder = build_from_xlsx if args.path.lower().endswith((".xlsx", ".xlsm")) else build_from_csv
    data = builder(args.path)
    save(data, args.out)
    objects = data["objects"]
    fields = sum(len(o["fields"]) for o in objects.values())
    print(f"  {len(objects)} objects, {fields} fields → {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
