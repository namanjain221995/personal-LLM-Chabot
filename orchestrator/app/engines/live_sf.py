"""Ask Salesforce itself, and merge the answer with the synced warehouse.

The warehouse answers most questions faster and cheaper than the API, so it
stays the default. This engine exists for the two cases it genuinely cannot
cover:

  - the record changed since the last sync (up to 30 minutes stale);
  - the object was never configured, so the warehouse has no table at all.

Both sources are queried when both can help, and the results are merged on
Salesforce Id so a record held in both places is shown ONCE, with the live
values winning — a stale copy presented as current is the worst outcome here.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import llm
from ..core import salesforce
from ..core.schema_cache import format_schema, schema_cache

_FENCE_RE = re.compile(r"```(?:soql|sql)?\s*(.*?)```", re.S | re.I)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)

_SOQL_SYSTEM = (
    "You write ONE SOQL query for the Salesforce REST API. Rules:\n"
    "- SOQL, not SQL: no SELECT *, no JOIN, no GROUP BY without an aggregate "
    "function, and every field must be named explicitly.\n"
    "- Always SELECT Id first — it is what lets results be matched against "
    "records already held locally.\n"
    "- Use relationship syntax for parents (Account.Name), and a subquery for "
    "children (SELECT Id, (SELECT Id FROM Contacts) FROM Account).\n"
    "- Salesforce checkbox fields are real booleans over the API: write "
    "WHERE IsWon = true, with no quotes.\n"
    "- Dates use literals like TODAY, LAST_N_DAYS:7, THIS_MONTH.\n"
    "- Keep it small: name only the fields the question needs.\n"
    "Respond with ONLY the query, no prose and no code fence."
)


def extract_soql(raw: str) -> str:
    """Pull the query out of a model reply (fences, <think>, stray prose)."""
    text = _THINK_RE.sub("", raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    match = re.search(r"SELECT\s.+", text, re.S | re.I)
    return " ".join(match.group(0).split()) if match else ""


def _object_hint() -> str:
    """The objects already synced, so the model prefers names that exist."""
    try:
        return ", ".join(sorted(schema_cache().keys()))
    except Exception:
        return ""


async def write_soql(question: str, history: Sequence[dict] = ()) -> str:
    from ..core.sf_dictionary import hint_for

    known = _object_hint()
    context = f"Objects known to be in this org: {known}\n\n" if known else ""
    # Real API names for whatever this question is about, straight from the
    # org export — SOQL has no forgiving fuzzy matching.
    dictionary = hint_for(question)
    if dictionary:
        context = f"{dictionary}\n\n{context}"
    raw = await llm.chat_completion(
        [
            {"role": "system", "content": _SOQL_SYSTEM},
            {"role": "user", "content": f"{context}Question: {question}"},
        ],
        temperature=0.0,
        # The main model thinks before answering, and that reasoning is drawn
        # from the SAME budget. At 1200 the whole allowance went on thinking
        # and the reply arrived with no query in it at all, which surfaced as
        # "the model did not produce a SOQL query".
        max_tokens=6000,
    )
    soql = extract_soql(raw)
    if not soql:
        raise salesforce.UnsafeSoql("the model did not produce a SOQL query")
    return soql


#: "How many objects?", "what fields does Interview__c have?", "list the API
#: names" — questions about the SHAPE of the org rather than its records.
_SCHEMA_RE = re.compile(
    r"\b(objects?|fields?|schema|metadata|api\s*names?|sobjects?)\b", re.I
)
_COUNT_OR_LIST_RE = re.compile(r"\b(how many|list|show|what|which|all)\b", re.I)

_OBJECT_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*__c|Account|Contact|Lead|Opportunity|Case|User)\b")


def is_schema_question(text: str) -> bool:
    """True for questions about the org's structure, not its data.

    These cannot be answered with SOQL — asked "how many objects and their
    fields?", the model wrote `SELECT ... FROM EntityDefinition` with an
    invented `ObjectFields` relationship and Salesforce rejected it. The
    describe API answers them directly.
    """
    t = text or ""
    return bool(_SCHEMA_RE.search(t) and _COUNT_OR_LIST_RE.search(t))


async def fetch_schema(question: str) -> Tuple[str, str]:
    """Answer a schema question from the describe API. → (source, text)."""
    from ..core import salesforce as sf

    named = _OBJECT_NAME_RE.findall(question or "")
    if named:
        # Asked about specific objects: give their fields.
        blocks = []
        for name in list(dict.fromkeys(named))[:3]:
            try:
                d = await sf.describe_object(name)
            except Exception:
                continue
            fields = ", ".join(f"{f['name']} ({f['type']})" for f in d["fields"])
            blocks.append(f"{d['name']} — {len(d['fields'])} fields:\n{fields}")
        if blocks:
            return "describe", "\n\n".join(blocks)

    objects = await sf.list_objects()
    custom = [o for o in objects if o["custom"]]
    standard = [o for o in objects if not o["custom"]]
    listing = ", ".join(o["name"] for o in objects)
    return "sobjects", (
        f"This org exposes {len(objects)} queryable objects to this user: "
        f"{len(custom)} custom and {len(standard)} standard.\n\n"
        f"API names:\n{listing}"
    )


async def fetch_live(
    question: str, history: Sequence[dict] = ()
) -> Tuple[str, List[Dict[str, Any]]]:
    """Question → SOQL → live rows. Raises SalesforceUnavailable/UnsafeSoql."""
    soql = await write_soql(question, history)
    return await salesforce.run_soql(soql)


def describe_rows(rows: List[Dict[str, Any]], limit: int = 30) -> str:
    """Compact JSON for the prompt — the shape the SQL engine already uses."""
    return json.dumps(rows[:limit], default=str)
