"""The deliberate changes of prompt texts that came from notebooks/object_knowledge_eval: one list for every check that
compares a prompt with the notebook's text (tests/test_prompt_files.py, the parity harness tests/parity/harness.py).

Each changed prompt names its version and its edits ``(notebook text, new text)``, applied in order. Every check
proves the new text is the notebook text with exactly these edits: :func:`revert` must give back the notebook text
(the golden hashes of tests/golden/prompts.json), :func:`apply` on the notebook text must give the new text. An edit
never touches a ``<<NAME>>`` token, so the same edits turn a filled or rendered notebook text into the new one.
"""
from __future__ import annotations

#: prompt id -> {"name": the bound constant, "version": the file's version, "why": the reason, "edits": [...]}
PROMPT_CHANGES: dict[str, dict] = {
    "records_agent_system": {
        "name": "RECORDS_AGENT_SYSTEM",
        "version": 4,
        "why": "Group B people fixes (docs/people.md): items 10 (a profile lookup is read from the person's own "
               "record: answer the direction asked and name the field), 11 (say how many records are in each "
               "status) and 7g (a role or purpose split is no filter; review fixes 2026-10-21); version 4, the "
               "value spots (2026-10-28): records the hints name need record data (step 0, unless marked as a "
               "plain word), and a named record is looked up and described from its fields and linked records "
               "(step 1, Named records)",
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
