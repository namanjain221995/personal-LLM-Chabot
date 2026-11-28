"""The deliberate changes of prompt texts that came from notebooks/object_knowledge_eval: one list for every check that
compares a prompt with the notebook's text (tests/test_prompt_files.py, the parity harness tests/parity/harness.py).

Each changed prompt names its version and its edits ``(notebook text, new text)``, applied in order. Every check
proves the new text is the notebook text with exactly these edits: :func:`revert` must give back the notebook text
(the golden hashes of tests/golden/prompts.json), :func:`apply` on the notebook text must give the new text. An edit
never touches a ``<<NAME>>`` token, so the same edits turn a filled or rendered notebook text into the new one.
"""
from __future__ import annotations

import copy

#: prompt id -> {"name": the bound constant, "version": the file's version, "why": the reason, "edits": [...]}
PROMPT_CHANGES: dict[str, dict] = {
    "records_agent_system": {
        "name": "RECORDS_AGENT_SYSTEM",
        "version": 7,
        "why": "Group B people fixes (docs/people.md): items 10 (a profile lookup is read from the person's own "
               "record: answer the direction asked and name the field), 11 (say how many records are in each "
               "status) and 7g (a role or purpose split is no filter; review fixes 2026-10-21); version 4, the "
               "value spots (2026-10-28): records the hints name need record data (step 0, unless marked as a "
               "plain word), and a named record is looked up and described from its fields and linked records "
               "(step 1, Named records); version 5, the placement fixes (2026-11-16): a business term the hints "
               "list is the owner's definition (step 1), a rate per period is one trend_records call with "
               "part_filters (step 3), and two examples of the placement term; version 6 (review of "
               "2026-11-16): a third example, candidates placed count different candidates; version 7 (round 2 "
               "of the pre-release fixes, 2026-11-28, D1 shape): how many of a group have fewer than N child "
               "records (max_count, those with none included), and its example",
        "edits": [
            ("     show: a record that is not finished is assigned, not done), not only the first. Call each\n"
             "     relation by its lookup's own role or label (a reporting line is not a manager). With no link,\n",
             "     show: a record that is not finished is assigned, not done: say how many are in each status),\n"
             "     not only the first. Call each relation by its lookup's own role or label (a reporting line is\n"
             "     not a manager). With no link,\n"),
            ("     answer from them; no extra query is needed for that.\n",
             "     answer from them; no extra query is needed for that.\n"
             "   - a profile lookup (reporting line, manager, team lead) is read from the person's own record and\n"
             "     names who that record points to; a reference counts other records that point to the person.\n"
             "     Answer the direction the question asks and name the field.\n"),
            ("   finished records. Never write a split that no result gives (about_people counts have none).\n",
             "   finished records. A role or purpose split is no filter: give each role's rows, or say which role\n"
             "   you counted. Never write a split that no result gives (about_people counts have none).\n"),
            # version 4 (value spots, 2026-10-28): the hint section "Records named in the question"
            ("When the hints list people named in the question, it needs record data: never reply NO_RECORDS. If it\n"
             "does NOT need record data",
             "When the hints list people named in the question, it needs record data: never reply NO_RECORDS. The\n"
             "same when the hints list records named in the question without \"(may be a plain word, not a record)\".\n"
             "If it does NOT need record data"),
            ("   sessions\" -> object Candidate_Training__c.\n2. Fields and values.",
             "   sessions\" -> object Candidate_Training__c.\n"
             "   Named records. A record the question names (the hints list it: object, field, linked records):\n"
             "   for what it is, what it does or what is known about it, call list_records on its object filtered\n"
             "   on that field (its main fields and any description), then count the linked records the hints\n"
             "   list (one count per lookup, filtered by the record's name) and describe the record from these\n"
             "   results. Never invent a purpose: when no description is stored, say so and describe it by its\n"
             "   fields and its linked records.\n2. Fields and values."),
            # version 5 (placement fixes, 2026-11-16): business terms, a rate per period, two examples
            ("   the recruiter role. Candidates are Account records with record type \"Person Account\".\n",
             "   the recruiter role. Candidates are Account records with record type \"Person Account\".\n"
             "   Business terms. When the hints list a business term of the question, that definition is the\n"
             "   owner's: use exactly its object, filter, base and date field, never another field or object\n"
             "   whose name or stored value shares the word.\n"),
            ("   what percentage / share / rate             -> percentage_of_records\n",
             "   what percentage / share / rate             -> percentage_of_records\n"
             "   a rate / share per day / week / month      -> trend_records with part_filters (one call, every period)\n"),
            ("{\"field\":\"Interview_Status__c\",\"op\":\"eq\",\"value\":\"Rescheduled\"}]}\n",
             "{\"field\":\"Interview_Status__c\",\"op\":\"eq\",\"value\":\"Rescheduled\"}]}\n"
             "- Placement rate per month this year (business term) -> trend_records {\"object\":\"Interview__c\","
             "\"grain\":\"month\",\"date_field\":\"Date_of_Interview__c\",\"period\":\"this year\",\"part_filters\":"
             "[{\"field\":\"Interview_Outcome__c\",\"op\":\"eq\",\"value\":\"Offer Received\"}]}\n"
             "- Placements per month this year (business term) -> trend_records {\"object\":\"Interview__c\","
             "\"grain\":\"month\",\"date_field\":\"Date_of_Interview__c\",\"period\":\"this year\",\"filters\":"
             "[{\"field\":\"Interview_Outcome__c\",\"op\":\"eq\",\"value\":\"Offer Received\"}]}\n"),
            # version 6 (review of 2026-11-16): candidates placed count different candidates
            ("\"period\":\"this year\",\"filters\":[{\"field\":\"Interview_Outcome__c\",\"op\":\"eq\","
             "\"value\":\"Offer Received\"}]}\n",
             "\"period\":\"this year\",\"filters\":[{\"field\":\"Interview_Outcome__c\",\"op\":\"eq\","
             "\"value\":\"Offer Received\"}]}\n"
             "- Candidates placed per month this year (business term) -> trend_records {\"object\":"
             "\"Interview__c\",\"grain\":\"month\",\"date_field\":\"Date_of_Interview__c\",\"period\":"
             "\"this year\",\"filters\":[{\"field\":\"Interview_Outcome__c\",\"op\":\"eq\",\"value\":"
             "\"Offer Received\"}],\"measure\":\"count_distinct\",\"measure_field\":\"Candidate__c\"}\n"),
            # version 7 (round 2 of the pre-release fixes, 2026-11-28): fewer than N child records, none included
            ('   without child records: related=[{"child_object": "...", "exists": false}].\n',
             '   without child records: related=[{"child_object": "...", "exists": false}]. How many of a group\n   have fewer than N child records in a period (those with none count too): count the group\'s own\n   object once, with related=[{..., "exists": true, "max_count": N-1, "date_field", "period"}];\n   never piece it together from a grouping of the children, which leaves out those with none.\n'),
            ('"related":[{"child_object":"Session__c","exists":true,"min_count":5}]}\n',
             '"related":[{"child_object":"Session__c","exists":true,"min_count":5}]}\n- Recruiters of a department with fewer than 3 client interviews in 2025, those with none included -> count_records {"object":"Recruiter__c","filters":[{"field":"Department__c","op":"eq","value":"<department>"}],"related":[{"child_object":"Interview__c","via_field":"Recruiter__c","exists":true,"max_count":2,"date_field":"Date_of_Interview__c","period":"2025"}]}\n'),
        ],
    },
    "records_unsupported_message": {
        "name": "RECORDS_UNSUPPORTED_MESSAGE",
        "version": 2,
        "why": "placement fixes (2026-11-16): a share per period comes from trend_records with part_filters, not one "
               "percentage_of_records per period",
        "edits": [
            ("percentage_of_records for a share)",
             "percentage_of_records for a share, trend_records with part_filters for a share per period)"),
        ],
    },
    "combine_system": {
        "name": "COMBINE_SYSTEM",
        "version": 2,
        "why": "Group B people fixes (docs/people.md), item 11: the merge keeps the record answer's status counts, "
               "zeros included",
        "edits": [
            ("  give both when RECORD FACTS have both.\n",
             "  give both when RECORD FACTS have both. Keep the RECORD ANSWER's status counts, zeros included.\n"),
        ],
    },
}

#: bound constant name -> prompt id, for the checks that go by name
BY_NAME = {c["name"]: pid for pid, c in PROMPT_CHANGES.items()}


def _swap(pid: str, text: str, forward: bool) -> str:
    edits = PROMPT_CHANGES[pid]["edits"]
    for old, new in (edits if forward else list(reversed(edits))):
        a, b = (old, new) if forward else (new, old)
        if text.count(a) != 1:
            raise AssertionError(f"{pid}: the edit text occurs {text.count(a)} times, not once: {a[:60]!r}")
        text = text.replace(a, b)
    return text


def apply(pid: str, text: str) -> str:
    """The notebook text (or a fill or render of it) with this prompt's edits; any other id: ``text``."""
    return _swap(pid, text, True) if pid in PROMPT_CHANGES else text


def revert(pid: str, text: str) -> str:
    """The new text (or a fill or render of it) back to the notebook's; any other id: ``text``."""
    return _swap(pid, text, False) if pid in PROMPT_CHANGES else text


def notebook_tools(tools: list[dict]) -> list[dict]:
    """A copy of tools (a list_tools() or openai_tools() record tool list) with the deliberate changes against the
    notebook's list taken out: the rate per period of trend_records (placement fixes, 2026-11-16:
    registry.TREND_RATE_TEXT in its description and the part_filters argument) and the page of list_records
    (development A of 2026-11-27: its limit's maximum LIMITS.list_max_rows and its description, the notebook's 50 and
    "Rows to return (default 10)."), and the at-most count of a related condition (round 2 of the pre-release fixes
    of 2026-11-28: registry.RELATED_MAX_TEXT in its description and the max_count argument). The golden hashes of
    tests/golden/tool_lists.json are the notebook's, so they compare with this view."""
    from sf_assistant.config import LIMITS
    from sf_assistant.tools.registry import RELATED_MAX_TEXT, TREND_RATE_TEXT

    out = copy.deepcopy(tools)
    seen: set[int] = set()   # one RELATED fragment may be shared by several schemas (deepcopy keeps that)
    for tool in out:
        fn = tool.get("function", tool)
        related = fn.get("parameters", fn.get("inputSchema", {})).get("properties", {}).get("related")
        if related is not None and id(related) not in seen:
            seen.add(id(related))
            assert related["description"].count(RELATED_MAX_TEXT) == 1
            related["description"] = related["description"].replace(RELATED_MAX_TEXT, "", 1)
            del related["items"]["properties"]["max_count"]
        if fn.get("name") == "list_records":
            limit = fn.get("parameters", fn.get("inputSchema"))["properties"]["limit"]
            assert limit["maximum"] == LIMITS.list_max_rows
            limit["maximum"], limit["description"] = 50, "Rows to return (default 10)."
            continue
        if fn.get("name") != "trend_records":
            continue
        assert fn["description"].count(TREND_RATE_TEXT) == 1
        fn["description"] = fn["description"].replace(TREND_RATE_TEXT, "", 1)
        schema = fn.get("parameters", fn.get("inputSchema"))
        assert "part_filters" in schema["properties"]
        del schema["properties"]["part_filters"]
    return out
