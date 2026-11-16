"""Prompt texts of the record agent and the merge call (no duckdb import).

The texts live in ``prompts/<id>.md`` (id = the constant name in lower case; :mod:`sf_assistant.prompts` loads each
file once) and are bound here at import, so the names and values are the same as before. The merge call's user
message layout is ``prompts/combine_user.md`` (:func:`combine_user_message`). The Group B texts at the end (relation
wording, direction, status line; docs/people.md) are new: no notebook constant is behind them; so is the value spots
text after them (the NO_RECORDS pushback for a question that names stored records, 2026-10-28).

Placeholders use ``<<NAME>>`` and are filled with ``str.replace``, because the texts contain JSON
braces. ``render_records_system(status)`` fills the record agent's system prompt from the live
:class:`okrec.types.RecordsStatus`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sf_assistant import prompts as _prompt_files   # the texts: prompts/<id>.md
from sf_assistant.config import LIMITS

if TYPE_CHECKING:  # type hints only
    from sf_assistant.engine.types import RecordsStatus

__all__ = ["RECORDS_AGENT_SYSTEM", "RECORDS_REQUIRE_TOOLS_MESSAGE", "RECORDS_FORCE_ANSWER_MESSAGE",
           "RECORDS_RUN_QUERY_MESSAGE", "RECORDS_UNSUPPORTED_MESSAGE", "RECORDS_OTHER_LOOKUP_MESSAGE",
           "RECORDS_OTHER_LOOKUP_FIRM_MESSAGE",
           "RECORDS_OTHER_LOOKUP_FITS_MESSAGE", "RECORDS_ELSEWHERE_MESSAGE", "RECORDS_NOT_FOUND_MESSAGE",
           "RECORDS_SAME_NAME_EMPLOYEE_MESSAGE", "RECORDS_NOT_SYNCED_MESSAGE", "RECORDS_SEVERAL_PEOPLE_MESSAGE",
           "RECORDS_NOT_EXACT_NAME_MESSAGE", "RECORDS_FINAL_FORM_MESSAGE", "RECORDS_LEAD_ZERO_NOTE",
           "RECORDS_LINKED_LOOKUP_MESSAGE", "RECORDS_LINKED_EMPLOYEE_MESSAGE", "RECORDS_UNFIT_ZERO_MESSAGE", "RECORDS_OVERLAP_MESSAGE",
           "RECORDS_DATE_COVERAGE_MESSAGE", "RECORDS_PERSON_NO_RECORDS_MESSAGE", "RECORDS_MIRROR_MESSAGE",
           "RECORDS_SPARSE_ZERO_MESSAGE", "RECORDS_SHARED_NAME_MESSAGE", "RECORDS_NO_DATA_MESSAGE",
           "RECORDS_BOTH_OBJECTS_MESSAGE", "RECORDS_BREAKDOWN_MESSAGE", "RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE",
           "RECORDS_PEOPLE_TOOL_MESSAGE", "NAMES_SYSTEM", "NAMES_MAX_TOKENS",
           "NO_RECORDS", "RECORDS", "ROUTE_SYSTEM", "KNOWLEDGE_RECORDS_NOTE", "KNOWLEDGE_LEAN_SYSTEM",
           "KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE", "KNOWLEDGE_LEAN_ANSWER_MESSAGE", "KNOWLEDGE_LEAN_BUDGET_ERROR",
           "COMBINE_SYSTEM", "render_records_system", "render_lean_system", "lean_require_tools_message",
           "lean_budget_error",
           "combine_user_message"]

RECORDS_AGENT_SYSTEM = _prompt_files.text("records_agent_system")

RECORDS_REQUIRE_TOOLS_MESSAGE = _prompt_files.text("records_require_tools_message")

RECORDS_FORCE_ANSWER_MESSAGE = _prompt_files.text("records_force_answer_message")

# Sent once (okeval.agent check_answer) when the draft answer states numbers but no query tool ran.
RECORDS_RUN_QUERY_MESSAGE = _prompt_files.text("records_run_query_message")

# Sent once when the draft answer has numbers that no tool result contains; <<NUMBERS>> lists them.
RECORDS_UNSUPPORTED_MESSAGE = _prompt_files.text("records_unsupported_message")

# Sent (check_draft, before the number checks) when a person count used one lookup and found 0, while the same
# person has records through other lookups of the object to the same target and the question has no role word (the
# model decides the role). <<OTHERS>> lists "<G> (<role>) <k>" (at most 4). <<FITS>> is " The question's word '<w>'
# fits <G> (<role>)." when the code matched the question's verb to the role of one of them, else empty. When the
# question's word fits one of them and not the used lookup, RECORDS_OTHER_LOOKUP_FIRM_MESSAGE is sent instead.
RECORDS_OTHER_LOOKUP_MESSAGE = _prompt_files.text("records_other_lookup_message")

# Sent instead of RECORDS_OTHER_LOOKUP_MESSAGE when the question's word fits another lookup with records and not the
# one the 0 used (lookup_check "fits"): the 0 is no answer, so the message offers no "answer 0" choice. Sent again
# (once) when the next draft still leads with that 0. <<WORD>> the question's word, <<OTHER_FIELD>> the first fitting
# lookup, <<OTHERS>> lists the fitting ones as "<G> (<role>) <k>"; <<SPARSE>> is " <<FIELD>> is filled on almost no
# <Obj> records." when the used lookup's role would fit but it is almost empty (lookup_check "fits_sparse"), else empty.
RECORDS_OTHER_LOOKUP_FIRM_MESSAGE = _prompt_files.text("records_other_lookup_firm_message")

# Sent (check_draft) when a person count above 0 used a lookup whose role does not fit the question's verb, while
# another lookup of the object to the same person object, whose role does fit, has records under the same conditions.
# <<OTHERS>> lists "<G> (<role>) <k>" (at most 4); <<WORD>> is the question word that fits them.
RECORDS_OTHER_LOOKUP_FITS_MESSAGE = _prompt_files.text("records_other_lookup_fits_message")

# Sent when a person count found 0, no lookup of the object holds the person, and the lookup check's reference
# scan found the person on other objects. <<ELSEWHERE>> lists "<Obj>.<G> (<role>) <k>" (at most 5); a reference whose
# role fits the question's verb reads "(<role>; fits '<word>')".
RECORDS_ELSEWHERE_MESSAGE = _prompt_files.text("records_elsewhere_message")

# Sent (check_draft) when a count used a lookup on a CANDIDATE record named like the person, while an Employee with the
# same name (a different record) has records through another lookup of the object whose role fits the question's verb
# for an employee (lookup_check entry: group same_name, own_kind employee, own_fits). <<OTHERS>> lists
# "<G> (<role>) <k>"; <<OTHER_FIELD>> is the first of them; <<WORD>> the verb.
RECORDS_SAME_NAME_EMPLOYEE_MESSAGE = _prompt_files.text("records_same_name_employee_message")

# Sent (check_draft) when a result's unsynced_fits says the lookup the question's words fit is documented but not
# synced to the warehouse, and the draft does not name it. <<PROXY>> is the lookup or path the result used instead.
RECORDS_NOT_SYNCED_MESSAGE = _prompt_files.text("records_not_synced_message")

# Sent (check_draft) when the question gives only a first name that several people have (RecordTools.question_people)
# and the draft names exactly one of them. <<WORD>> the question's word, <<NAMES>> the stored full names, <<USED>> the one
# the draft names.
RECORDS_SEVERAL_PEOPLE_MESSAGE = _prompt_files.text("records_several_people_message")

# Sent (check_draft) when the question's full name is not stored but close to a stored name (RecordTools.question_people
# "asked"), and the draft uses the stored name without saying that the asked spelling does not exist.
RECORDS_NOT_EXACT_NAME_MESSAGE = _prompt_files.text("records_not_exact_name_message")

# Sent (check_draft) when the draft reads as working notes (first-person planning such as "I will", "Let me", "Wait,"):
# without the Source line at the end (often cut off by the token limit), or with planning phrases all through it.
RECORDS_FINAL_FORM_MESSAGE = _prompt_files.text("records_final_form_message")

# Appended to RECORDS_OTHER_LOOKUP_MESSAGE when the draft names the other lookups only after a first sentence that
# gives the 0.
RECORDS_LEAD_ZERO_NOTE = _prompt_files.text("records_lead_zero_note")

# Sent (check_draft) when a count found 0 through a lookup whose value matched a record LINKED to a person (an
# employee's own Account, a login user: lookup_check "linked" entries), while that person has records on the same
# object through another lookup, and the draft leads with the 0 or does not give those counts. <<VIA>> is the link.
RECORDS_LINKED_LOOKUP_MESSAGE = _prompt_files.text("records_linked_lookup_message")

# Sent (check_draft) when a count used a candidate lookup whose value matched an employee's OWN Account (lookup_check
# "linked" others on the Employee object: the same person), while that employee has records on the object through an
# employee lookup, and the draft does not give them. <<VIA>> is the link, <<OTHERS>> lists "<G> (<role>) <k>",
# <<OTHER_FIELD>> is the first of them.
RECORDS_LINKED_EMPLOYEE_MESSAGE = _prompt_files.text("records_linked_employee_message")

# Sent (check_draft) when the draft leads with a 0 whose lookup role does not fit the question's role word (and no
# lookup of that object that fits it has data), while the person has records through other lookups of the object.
# <<SPARSE>> names a lookup whose role fits but that is almost empty (lookup_check "fits_sparse"), else empty.
RECORDS_UNFIT_ZERO_MESSAGE = _prompt_files.text("records_unfit_zero_message")

# Sent (check_draft) when the draft leads with 0 through a lookup whose role fits the question's word but that is
# empty or almost empty (lookup_check "fits_sparse"), while other lookups of the object to the same person object hold
# the person (their roles do not fit the word, or are not documented). <<OTHERS>> lists "<G> (<role>) <k>".
RECORDS_SPARSE_ZERO_MESSAGE = _prompt_files.text("records_sparse_zero_message")

# Sent (check_draft) when a person filter matched several records of the asked kind that share the name (lookup_check
# by_record) and the draft does not say so. <<RECORDS>> lists "<label> (<detail>) <k>".
RECORDS_SHARED_NAME_MESSAGE = _prompt_files.text("records_shared_name_message")

# Sent (check_draft) when a counting result says a filtered lookup has no data (result key no_data_fields) and the
# draft's first sentence gives 0. <<FIELDS>> lists "<Obj>.<Field>".
RECORDS_NO_DATA_MESSAGE = _prompt_files.text("records_no_data_message")

# Sent (check_draft) when the question's noun has no qualifier and no role word and fits the label of several objects
# that reference the named person (RecordTools.question_objects), but the draft's counting queries ran on only some of
# them. <<OBJECTS>> lists "<Obj> (<label>; <lookup> (<role>))", <<DONE>> and <<MISSING>> object API names.
RECORDS_BOTH_OBJECTS_MESSAGE = _prompt_files.text("records_both_objects_message")

# Sent (check_draft) when breakdown lines of the draft ("- Completed: 3") give a count that no result gives for that
# value: a group of a grouped result with another count, or a stored value the grouped result does not have.
# <<ITEMS>> lists "<label>: <n>".
RECORDS_BREAKDOWN_MESSAGE = _prompt_files.text("records_breakdown_message")

# Sent (check_draft) when a clause of the draft relates the counts of different lookups to each other ("of these",
# "on these records", "include", "additional", "plus", "11 + 15") using a lookup_check or find_references count, or
# gives a difference of two lookup_check counts as extra records. <<CLAUSE>> quotes the clause.
RECORDS_OVERLAP_MESSAGE = _prompt_files.text("records_overlap_message")

# Sent (check_draft) when a trend used a date field that is blank on many of the records meeting its other conditions
# (records_without_date at least 10% of them). <<MAIN>> names the object's first business date when describe_fields
# showed it.
RECORDS_DATE_COVERAGE_MESSAGE = _prompt_files.text("records_date_coverage_message")

# Sent (check_draft) when no data query succeeded, a call failed because a name, field or object is not there (E4h,
# E15a, E1b, E2b), and the draft still gives a number that is not in the question or the source line (0 included).
RECORDS_NOT_FOUND_MESSAGE = _prompt_files.text("records_not_found_message")

# Sent (check_draft) when the last counting query on an object used a lookup that copies a lookup path of the same
# record (result key copied_lookups, from the knowledge field_dependencies) and no later query used that path.
RECORDS_MIRROR_MESSAGE = _prompt_files.text("records_mirror_message")

# Sent (check_draft) when the record agent replies NO_RECORDS although the route call said the question needs record
# data and the question names a stored person's full name (RecordTools.names_person): typically after a hint said the
# lookup the question means is not in the warehouse.
RECORDS_PERSON_NO_RECORDS_MESSAGE = _prompt_files.text("records_person_no_records_message")

# Sent instead of RECORDS_REQUIRE_TOOLS_MESSAGE (answer_question's require_tools message) when the question names
# people (RecordSession triage, RECORDS_SPEC §22.8): the first reply called no tool. <<PEOPLE>> lists the names.
RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE = _prompt_files.text("records_people_require_tools_message")

# Sent once (check_draft, people_tool_nudges) when the question names two or more people and no about_people call
# covered all of them, or names a person no result covers. <<NAMES>> the names, <<MISSING>> the uncovered ones,
# <<NAMES_JSON>> the names as a JSON list.
RECORDS_PEOPLE_TOOL_MESSAGE = _prompt_files.text("records_people_tool_message")

NO_RECORDS = "NO_RECORDS"
RECORDS = "RECORDS"

# The names call (RecordSession._names_call, RECORDS_SPEC §22.3): one short model call next to the route call. Code
# keeps only names whose words all occur in the question, and matches them to stored people.
NAMES_SYSTEM = _prompt_files.text("names_system")
NAMES_MAX_TOKENS = LIMITS.names_max_tokens   # 60

# One short model call before the record agent (RecordSession.route): the agent itself tends to call tools
# for any question, so metadata questions would otherwise get record counts merged into their answer.
ROUTE_SYSTEM = _prompt_files.text("route_system")

# Appended to the knowledge agent's system prompt (ask.py only, when records are on).
KNOWLEDGE_RECORDS_NOTE = _prompt_files.text("knowledge_records_note")

# The lean knowledge step (ask.py, merged mode, when the route call says RECORDS; RECORDS_SPEC §21.19): the knowledge
# agent's whole system prompt (not appended to MULTI_AGENT_SYSTEM, whose rule 4 asks for "needs record data").
# <<MAX_TOOLS>> is filled by render_lean_system with okrec.lean.LEAN_MAX_TOOL_CALLS (okrec.lean enforces it in code),
# <<SEARCH_TOOL>> with the first search tool the lean step offers (search_all_knowledge, or search_knowledge with one
# knowledge file).
KNOWLEDGE_LEAN_SYSTEM = _prompt_files.text("knowledge_lean_system")

# Sent back once when the lean step's first reply has no tool call (answer_question's require_tools message).
KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE = _prompt_files.text("knowledge_lean_require_tools_message")

# Sent with the last model call of the lean step (no tools offered then), and as answer_question's force message.
KNOWLEDGE_LEAN_ANSWER_MESSAGE = _prompt_files.text("knowledge_lean_answer_message")

# A lean-step tool call past the budget gets this error instead of running.
KNOWLEDGE_LEAN_BUDGET_ERROR = _prompt_files.text("knowledge_lean_budget_error")

COMBINE_SYSTEM = _prompt_files.text("combine_system")

# ---- Texts code fills into the lines it writes (agent/nudges.py, agent/record_session.py; they were inline f-strings
# of okrec/session.py until stage 2). Filled in one pass with sf_assistant.prompts.fill (a value that holds a <<NAME>>
# token is never filled again), so the result is byte for byte the old f-string. Not in __all__ (the okrec/prompts.py
# surface stays as it was).

# The <<SPARSE>> part of RECORDS_UNFIT_ZERO_MESSAGE (_unfit_zero_nudge): the fitting lookup is almost empty.
RECORDS_UNFIT_ZERO_SPARSE_NOTE = _prompt_files.text("records_unfit_zero_sparse_note")
# The <<SPARSE>> part of RECORDS_OTHER_LOOKUP_FIRM_MESSAGE (_other_lookup_firm): the used lookup is almost empty.
RECORDS_OTHER_LOOKUP_FIRM_SPARSE_NOTE = _prompt_files.text("records_other_lookup_firm_sparse_note")
# The <<FITS>> part of RECORDS_OTHER_LOOKUP_MESSAGE (_fits_text). <<LOOKUPS>> lists "<G> (<role>)" (at most 2).
RECORDS_OTHER_LOOKUP_FITS_NOTE = _prompt_files.text("records_other_lookup_fits_note")
# The <<MAIN>> part of RECORDS_DATE_COVERAGE_MESSAGE (date_nudges): the object's first business date, or where to
# find it when describe_fields did not show it.
RECORDS_DATE_COVERAGE_MAIN = _prompt_files.text("records_date_coverage_main")
RECORDS_DATE_COVERAGE_MAIN_UNKNOWN = _prompt_files.text("records_date_coverage_main_unknown")
# The first line code puts above a final answer that still leads with 0 through a lookup with no data
# (with_no_data_line): one field, or several. <<FIELDS>> lists "`<Obj>.<Field>`" (at most 3).
RECORDS_NO_DATA_LINE = _prompt_files.text("records_no_data_line")
RECORDS_NO_DATA_LINE_SEVERAL = _prompt_files.text("records_no_data_line_several")
# FIELD CHECK lines for the merge call (field_checks): the lookup the question's word fits is not synced, or the
# question's verb fits another lookup that the knowledge answer names.
RECORDS_FIELD_CHECK_UNSYNCED = _prompt_files.text("records_field_check_unsynced")
RECORDS_FIELD_CHECK_LOOKUP = _prompt_files.text("records_field_check_lookup")
# The parts of the code footer under a final answer (with_lookup_footer). <<LOOKUPS>> / <<OTHERS>> list
# "`<Obj>.<Field>` (<role>)" with or without counts, <<NAMES>> the quoted people.
RECORDS_FOOTER_LOOKUP_USED = _prompt_files.text("records_footer_lookup_used")
RECORDS_FOOTER_SAME_PERSON = _prompt_files.text("records_footer_same_person")
RECORDS_FOOTER_SAME_PERSON_FOR = _prompt_files.text("records_footer_same_person_for")
RECORDS_FOOTER_WORD_FITS = _prompt_files.text("records_footer_word_fits")
RECORDS_FOOTER_NOT_SYNCED = _prompt_files.text("records_footer_not_synced")
# The last line of a final answer without a Source line (with_source_line).
RECORDS_SOURCE_LINE = _prompt_files.text("records_source_line")
# The line the record session adds when other stored people share a first name the answer does not cover
# (RecordSession._other_people_line). <<OTHERS>> lists "<name> (<kind>)" (at most 8).
RECORDS_OTHER_PEOPLE_LINE = _prompt_files.text("records_other_people_line")

# ---- Group B (docs/people.md), new texts with no notebook constant behind them; not in __all__ either.
# Item 10, sent by check_draft (agent/nudges.py): a relation word of the draft that no lookup holding the named person
# carries (relation_wording_nudges; <<HOLDS>> says which lookups hold the person, <<WORD_FIELDS>> the lookups the
# word belongs to and whom they hold), and the direction of a self-lookup (direction_nudges): the question asks for
# the records that point to the person and no list of them ran, for the person's own value and the draft does not
# give it, or the word order does not tell and the draft names no lookup. <<COUNTED>> is
# RECORDS_DIRECTION_COUNTED_NOTE (about_people's reference count) or empty.
RECORDS_RELATION_WORD_MESSAGE = _prompt_files.text("records_relation_word_message")
RECORDS_DIRECTION_MESSAGE = _prompt_files.text("records_direction_message")
RECORDS_DIRECTION_SOURCE_MESSAGE = _prompt_files.text("records_direction_source_message")
RECORDS_DIRECTION_UNCLEAR_MESSAGE = _prompt_files.text("records_direction_unclear_message")
RECORDS_DIRECTION_COUNTED_NOTE = _prompt_files.text("records_direction_counted_note")
# Item 11, the code line under a final answer (with_status_line): the status split of the person's records the
# question's verb is about, with the status values that have no record. <<SPLIT>> lists "<value> <n>", <<ZERO>> the
# values with no record ("A, B or C").
RECORDS_STATUS_LINE = _prompt_files.text("records_status_line")
# Items 8 and 9 (review fixes 2026-10-21), sent by check_draft: a person's count through the record's own lookup only
# while the lookup check reaches more records through their parents in the same role (paths_nudges; <<PATHS>> lists
# "`<path>` <n>"), and a count through a lookup that copies a child object's lookup without the source
# (child_copy_nudges; <<HELD>> is RECORDS_CHILD_COPY_HELD_NOTE, the lookup check's count through the source, or empty).
RECORDS_PATHS_MESSAGE = _prompt_files.text("records_paths_message")
RECORDS_CHILD_COPY_MESSAGE = _prompt_files.text("records_child_copy_message")
RECORDS_CHILD_COPY_HELD_NOTE = _prompt_files.text("records_child_copy_held_note")
# Item 10 without about_people (evaluation fix 5, 2026-10-22), sent by check_draft (relation_lookup_nudges): a word of
# the question is in the knowledge wording (label, from_source, from_target) of a person lookup, while the answer used
# another lookup of the same object to the same person object (<<USED>>: "`<F>` (<label>)", ...). The message says
# which direction the question asks for: the records that point to the person (RECORDS_RELATION_LOOKUP_MESSAGE), the
# person's own value of a self-lookup (..._SOURCE_MESSAGE), or either (..._UNCLEAR_MESSAGE). <<COUNTED>> is
# RECORDS_RELATION_LOOKUP_COUNTED_NOTE (a lookup check's count through that lookup) or empty.
RECORDS_RELATION_LOOKUP_MESSAGE = _prompt_files.text("records_relation_lookup_message")
RECORDS_RELATION_LOOKUP_SOURCE_MESSAGE = _prompt_files.text("records_relation_lookup_source_message")
RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE = _prompt_files.text("records_relation_lookup_unclear_message")
RECORDS_RELATION_LOOKUP_COUNTED_NOTE = _prompt_files.text("records_relation_lookup_counted_note")

# ---- Value spots (2026-10-28: every stored record value a question names), new; not in __all__ either.
# Sent by check_draft when the record agent replies NO_RECORDS while code matched words of the question to stored
# record values (a strong listed value spot of the triage) and no people message applies. <<VALUES>> lists
# "'<words>' (<object>.<field>)" (agent.triage.value_text; an e-mail address or phone number typed in the question is
# named, never repeated).
RECORDS_VALUE_NO_RECORDS_MESSAGE = _prompt_files.text("records_value_no_records_message")


def render_records_system(status: "RecordsStatus") -> str:
    """``RECORDS_AGENT_SYSTEM`` with origin, newest sync, today and time zone filled in."""
    return (RECORDS_AGENT_SYSTEM
            .replace("<<ORIGIN>>", (status.origin or "copy of unknown origin").lower())
            .replace("<<NEWEST_SYNC>>", status.newest_sync_utc or "unknown")
            .replace("<<TODAY>>", status.today)
            .replace("<<TIME_ZONE>>", status.time_zone))


#: The search tools the lean prompt can name, in order of preference.
_LEAN_SEARCH_TOOLS = ("search_all_knowledge", "search_knowledge", "search_fields")


def render_lean_system(max_tools: int, tool_names: "tuple[str, ...] | list[str] | None" = None) -> str:
    """``KNOWLEDGE_LEAN_SYSTEM`` with the tool budget and the search tool filled in. ``tool_names``: the tools the
    lean step offers (``LeanRegistry.tools``); the prompt names the first of :data:`_LEAN_SEARCH_TOOLS` among them
    (search_knowledge with a single knowledge file, which has no search_all_knowledge). None names
    search_all_knowledge."""
    names = list(tool_names) if tool_names is not None else ["search_all_knowledge"]
    search = next((t for t in _LEAN_SEARCH_TOOLS if t in names), "search_all_knowledge")
    return (KNOWLEDGE_LEAN_SYSTEM.replace("<<MAX_TOOLS>>", str(int(max_tools)))
            .replace("<<SEARCH_TOOL>>", search))


def lean_require_tools_message(max_tools: int) -> str:
    """``KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE`` with the tool budget filled in."""
    return KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE.replace("<<MAX_TOOLS>>", str(int(max_tools)))


def lean_budget_error(max_tools: int) -> str:
    """``KNOWLEDGE_LEAN_BUDGET_ERROR`` with the tool budget filled in."""
    return KNOWLEDGE_LEAN_BUDGET_ERROR.replace("<<MAX_TOOLS>>", str(int(max_tools)))


#: The merge call's user message layout (§12.4): ``{question}``, ``{knowledge_answer}``, ``{record_answer}``,
#: ``{field_check}`` and ``{facts}``, filled in one pass by :func:`combine_user_message`.
_COMBINE_USER = _prompt_files.text("combine_user")
_COMBINE_USER_RE = re.compile(r"\{(question|knowledge_answer|record_answer|field_check|facts)\}")


def combine_user_message(question: str, knowledge_answer: str, record_answer: str, facts: str,
                         field_check: list[str] | None = None) -> str:
    """The merge call's user message (§12.4 layout); FIELD CHECK lines go before RECORD FACTS (§21.9).

    One pass over ``prompts/combine_user.md``: a value that holds ``{facts}`` or JSON braces is never filled again."""
    check = "".join(f"\n- {line}" for line in field_check or [] if line)
    values = {"question": f"{question}", "knowledge_answer": f"{knowledge_answer}", "record_answer": f"{record_answer}",
              "field_check": f"\n\nFIELD CHECK:{check}" if check else "", "facts": f"{facts}"}
    return _COMBINE_USER_RE.sub(lambda m: values[m.group(1)], _COMBINE_USER)
