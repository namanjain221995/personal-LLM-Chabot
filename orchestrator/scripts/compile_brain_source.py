#!/usr/bin/env python3
"""Compile a raw Salesforce knowledge file into a brain pack draft.

The Salesforce team writes knowledge as prose (see brain/sources/). The
orchestrator consumes knowledge as packs (see brain/packs/ and
app/core/brain.py). This script is the bridge:

    .venv/bin/python scripts/compile_brain_source.py ../brain/sources/foo.txt

What it always does (no model needed):
  * splits the source into sections (=== headers, markdown headings, or
    blank-line paragraphs as a last resort) → `knowledge` chunks with
    keywords derived from each section title;
  * collects every Object__c / Field__c token the text mentions, checks each
    against the org dictionary (/data/sf_dictionary.json or a --dictionary
    path), and prints a WARNING for names the org does not have — the #1 way
    a knowledge file poisons SQL generation;
  * seeds `triggers` from the most distinctive recurring words.

What it adds when the local LLM is reachable (--llm, or LLM_BASE_URL set):
  * a distilled `rules` block (the query traps),
  * a `glossary` of the org's own terms.

The output is a DRAFT: `brain/packs/<name>.yaml`, refused if the file already
exists (use --force). A human reviews it before it ships — especially the
warnings.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# --- section splitting -------------------------------------------------------

_RULE_LINE = re.compile(r"^={5,}\s*$")
_MD_HEADING = re.compile(r"^#{1,3}\s+(.+)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.{3,80})$")

#: A chunk under this many characters carries a heading and nothing else.
_MIN_CHUNK = 120
#: Chunks above this are split on paragraph boundaries.
_MAX_CHUNK = 1600


def split_sections(text: str) -> list[dict]:
    """(title, body) sections from ===-ruled files, markdown, or paragraphs."""
    lines = text.splitlines()
    sections: list[dict] = []
    title, body = "", []

    def flush() -> None:
        content = "\n".join(body).strip()
        if len(content) >= _MIN_CHUNK:
            sections.append({"title": title.strip(), "text": content})

    i = 0
    while i < len(lines):
        line = lines[i]
        # "===== / TITLE / =====" sandwich, the style of the first source file.
        if _RULE_LINE.match(line) and i + 2 < len(lines) and _RULE_LINE.match(lines[i + 2]):
            flush()
            title, body = lines[i + 1], []
            i += 3
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            flush()
            title, body = heading.group(1), []
            i += 1
            continue
        body.append(line)
        i += 1
    flush()

    if len(sections) <= 1 and sections and len(sections[0]["text"]) > _MAX_CHUNK:
        # No structure at all: fall back to paragraph packing.
        paragraphs = re.split(r"\n\s*\n", sections[0]["text"])
        sections, buf = [], ""
        for paragraph in paragraphs:
            if len(buf) + len(paragraph) > _MAX_CHUNK and buf:
                sections.append({"title": "", "text": buf.strip()})
                buf = ""
            buf += paragraph + "\n\n"
        if buf.strip():
            sections.append({"title": "", "text": buf.strip()})

    # Long sections get split so retrieval stays precise.
    final = []
    for section in sections:
        text_ = section["text"]
        if len(text_) <= _MAX_CHUNK:
            final.append(section)
            continue
        parts, buf = [], ""
        for paragraph in re.split(r"\n\s*\n", text_):
            if len(buf) + len(paragraph) > _MAX_CHUNK and buf:
                parts.append(buf.strip())
                buf = ""
            buf += paragraph + "\n\n"
        if buf.strip():
            parts.append(buf.strip())
        for index, part in enumerate(parts):
            suffix = f" ({index + 1})" if len(parts) > 1 else ""
            final.append({"title": section["title"] + suffix, "text": part})
    return final


# --- field validation ---------------------------------------------------------

_API_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__c)\b")
_STANDARD_FIELDS = {
    "Id", "Name", "OwnerId", "CreatedById", "CreatedDate", "LastModifiedById",
    "LastModifiedDate", "SystemModstamp", "IsDeleted", "RecordTypeId",
}


def check_against_dictionary(text: str, dictionary_path: str) -> tuple[set, set, set]:
    """→ (known objects mentioned, known fields mentioned, unknown names)."""
    mentioned = set(_API_TOKEN.findall(text))
    if not mentioned:
        return set(), set(), set()
    try:
        data = json.loads(Path(dictionary_path).read_text(encoding="utf-8"))
    except Exception:
        print(f"  note: no dictionary at {dictionary_path}; field check skipped")
        return set(), set(), set()
    objects, fields = set(), set()
    for obj in data.get("objects", {}).values():
        objects.add(obj["api"])
        for field in obj.get("fields", []):
            fields.add(field["api"])
    return mentioned & objects, mentioned & fields, mentioned - objects - fields


# --- trigger seeding ----------------------------------------------------------

_WORD = re.compile(r"[a-z][a-z]{3,}")
_GENERIC = {
    "salesforce", "field", "fields", "record", "records", "value", "values",
    "object", "objects", "with", "from", "this", "that", "when", "then",
    "does", "into", "only", "every", "each", "them", "have", "been", "also",
    "must", "never", "always", "section", "example", "note", "true", "false",
    "text", "date", "number", "status", "name", "amount", "picklist", "lookup",
}


def seed_triggers(text: str, top: int = 25) -> list[str]:
    counts = Counter(w for w in _WORD.findall(text.lower()) if w not in _GENERIC)
    return [word for word, count in counts.most_common(top) if count >= 4]


# --- optional LLM distillation --------------------------------------------------

_DISTILL_PROMPT = """You are compiling Salesforce org documentation into a knowledge pack \
for a text-to-SQL assistant. From the documentation below, extract:

1. RULES: the 8-15 facts a SQL writer must know to avoid a wrong-but-plausible \
query (population filters, which column really holds a value, status meanings, \
fields that look right but are wrong). One line each, imperative.
2. GLOSSARY: the org's own terms a user might say, with a one-sentence meaning \
each (max 10).

Answer as JSON only: {"rules": "...multi-line string...", "glossary": {"term": "meaning"}}

DOCUMENTATION:
"""


def distill_with_llm(text: str) -> dict:
    import httpx

    base = (os.environ.get("LLM_BASE_URL") or "http://127.0.0.1:30000/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or ""
    if not model:
        try:
            listing = httpx.get(f"{base}/models", timeout=10).json()
            model = listing["data"][0]["id"]
        except Exception as exc:
            raise RuntimeError(f"cannot list models at {base}: {exc}") from exc
    response = httpx.post(
        f"{base}/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": _DISTILL_PROMPT + text[:60000]}],
            "temperature": 0.1,
            "max_tokens": 4000,
        },
        timeout=600,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise RuntimeError("model returned no JSON")
    return json.loads(match.group(0))


# --- main ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="raw knowledge file (txt/md)")
    parser.add_argument("--out", help="output pack path (default brain/packs/<stem>.yaml)")
    parser.add_argument("--dictionary", default=os.environ.get(
        "SF_DICTIONARY_PATH", "/data/sf_dictionary.json"))
    parser.add_argument("--llm", action="store_true",
                        help="distill rules/glossary with the local LLM")
    parser.add_argument("--force", action="store_true", help="overwrite an existing pack")
    args = parser.parse_args()

    source = Path(args.source)
    text = source.read_text(encoding="utf-8", errors="replace")
    out = Path(args.out) if args.out else REPO / "brain" / "packs" / f"{source.stem}.yaml"
    if out.exists() and not args.force:
        print(f"refusing to overwrite {out} (use --force)")
        return 1

    sections = split_sections(text)
    print(f"  {len(sections)} knowledge chunk(s)")

    objects, fields, unknown = check_against_dictionary(text, args.dictionary)
    if objects or fields:
        print(f"  verified against the org dictionary: {len(objects)} object(s), "
              f"{len(fields)} field name(s)")
    for name in sorted(unknown):
        print(f"  WARNING: {name} is not in the org dictionary — sandbox-only? "
              "Do not let the pack teach SQL against it.")

    pack: dict = {
        "name": source.stem,
        "description": f"DRAFT compiled from brain/sources/{source.name} — review before shipping.",
        "triggers": seed_triggers(text),
        "tables": sorted(objects),
        "rules": "",
        "glossary": {},
        "knowledge": [
            {
                "title": section["title"] or f"{source.stem} — part",
                "keywords": [w for w in _WORD.findall(section["title"].lower())
                             if w not in _GENERIC][:6],
                "text": section["text"],
            }
            for section in sections
        ],
    }

    if args.llm or os.environ.get("LLM_BASE_URL"):
        try:
            distilled = distill_with_llm(text)
            pack["rules"] = str(distilled.get("rules") or "")
            glossary = distilled.get("glossary") or {}
            if isinstance(glossary, dict):
                pack["glossary"] = {str(k): str(v) for k, v in glossary.items()}
            print("  rules + glossary distilled by the local model — REVIEW THEM")
        except Exception as exc:
            print(f"  LLM distillation skipped: {exc}")

    import yaml

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(pack, sort_keys=False, allow_unicode=True, width=78),
        encoding="utf-8",
    )
    print(f"  draft written → {out}")
    print("  next: review the draft (triggers, tables, rules, warnings), then ship it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
