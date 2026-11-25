"""The record agent, the merge call and the number audit (no duckdb import).

:class:`RecordSession` answers one question with the 14 record tools through
``sf_assistant.agent.knowledge_agent.answer_question`` and its own system prompt. :func:`combine` merges the knowledge
answer and the record answer in one model call; :func:`audit_numbers` then checks that every
number of the merged text appears in the record facts (or the question, the knowledge answer, the
status line). A merge with a number that cannot be found is rejected and the record answer is used.

Lookup disambiguation (RECORDS_SPEC §21.10): code reads only structured keys of the tool results
(``lookup_check``, ``QueryEvidence.error_code``, ``fields_used``, ``not_found`` and ``names_matched``), never note or error
texts. :meth:`RecordSession.check_draft` sends nudges for a 0 whose lookup role or object does not fit
(also when it leads the draft and the fitting count is only a side note), for a leading 0 through the almost
empty lookup of the asked role while other lookups hold the person, for a leading 0 through a lookup with no
data (``no_data_fields``), for a 0 through a candidate lookup on a record linked to an employee, for a
candidate record named like an employee, for a fitting lookup that is not synced (``unsynced_fits``), for a
silent pick among people sharing a first name or a silently corrected name (``RecordTools.question_people``),
for several records of the asked kind sharing the person's name (``by_record``), for a noun without qualifier
or role word that fits several objects holding the person when only some were counted
(``RecordTools.question_objects``), for made-up overlaps or differences between counts (lookup_check and
find_references counts), for a trend on a mostly blank date field, for a NO_RECORDS reply about a named person,
and for working notes written as the reply that code cannot clean (tool words it cannot rewrite included); the draft after a nudge is
checked once more for a 0 that still leads through a lookup whose role does not fit and for a made-up overlap
(:meth:`RecordSession.recheck_draft`). The final record answer loses, by code, the made-up overlap clauses still left
(:func:`drop_overlap_clauses`), number-free caveats about lookups the question is not about
(:func:`drop_record_side_caveats`) and the parts with numbers that no result holds (:func:`drop_unsupported`: the last
answer before the tool budget runs out is checked by no model call); a code no-data line replaces a lead that gives
only the 0 through a lookup with no data (:func:`with_no_data_line`).
A reply dropped after a name was not found is logged, and code builds the not-found answer from the failed calls
(:func:`not_found_answer`; also for a NO_RECORDS reply after a name was not found). :func:`field_check_lines` feeds the merge a FIELD CHECK;
:func:`merge_scope_issues` rejects a merge that adds a condition, changes the lead count, claims an absence on
an object no query counted, calls an object or field not available that the warehouse holds, relates counts of
different lookups to each other, gives the any_role count with creator, last editor or owner, places the person
in a lookup whose count is 0, or adds a caveat about a lookup the question is not about (:func:`side_caveats`), and a
merged text with a count when the record answer says the name is not in the records; a merged text whose lead does not
say that the asked spelling of a name is not stored gets the record answer's sentence first (:func:`with_name_note`);
:func:`with_lookup_footer` names the lookup a final answer used when the answer does not. Final answers lose
working sentences (:func:`drop_working_sentences`), tool talk (:func:`drop_tool_talk`) and the system lookups listed in
an any_role scope (:func:`fix_any_role_scope`), and get a Source line when
they have none (:func:`with_source_line`); the knowledge answer reaches the merge without its SOQL and "no record
data" sentences (:func:`clean_knowledge_answer`).

Questions about named people (RECORDS_SPEC §22): :meth:`RecordSession.triage` runs the route call and one small names
call (``NAMES_SYSTEM``) in parallel and confirms the people the tools' name index spots (``spot_people``,
``resolve_person_name``); a question that names a stored person, or a name no stored person has, goes to the record
agent whatever the route said, a NO_RECORDS draft about them gets a pushback, :func:`people_tool_nudges` asks for
``about_people`` when no result covers them, and when the agent gives no answer code writes the record part from
``about_people`` (:func:`people_answer`).

Stored record values (value spots, 2026-10-28: every stored record value a question names): after the people are
decided, the triage takes the stored record values code spots in the question (``tools.spot_values``: Names, codes,
short text; picklist values and record type names are design vocabulary and never count); a strong one sends a
question the route call called NO_RECORDS to the record agent (``Triage.overruled_by`` "value", with the reason for the
trace and the log), a names-call name inside such a value is no missing person, the record tools get the values for
their hints (``begin_question(values=...)``), a NO_RECORDS draft gets a pushback (``RECORDS_VALUE_NO_RECORDS_MESSAGE``)
and :meth:`RecordSession.last_triage` tells the pipeline that the full knowledge agent answers next to the records.

Charts (``SFA_CHARTS``, ``config.charts_settings``, read at each call; ``on`` is the default): with ``on``, a question
that asks for a chart and names something to chart goes to the record agent against a NO_RECORDS route
(``Triage.overruled_by`` "chart", and the pipeline keeps the full knowledge agent, as for a value) and the agent's
system prompt ends with ``agent.charts.RECORDS_CHART_NOTE``. Off, the charts module is not imported and no byte changes.

Group B (docs/people.md): the triage keeps a first name in play that both calls missed when the data backs it
(``tools.first_name_fit``, item 13); the draft check sends the relation wording and direction nudges (item 10) over
the catalog view :meth:`RecordSession.lookup_directions`; :meth:`RecordSession.status_of` is the status view the
pipeline's status lines read (item 11).

Evaluation fixes of 2026-10-22: a reply with nothing but its Source line, before or after the checks, is no answer
(:func:`answer_text`, fix 2): it is dropped like an empty reply, and code writes the people answer when the question
names people or the record agent used about_people. The draft check reads the knowledge wording of the person lookups
(:meth:`RecordSession.person_lookups`) for the direction check and for a relation word of the question that another
lookup than the one the answer used carries (:func:`relation_lookup_nudges`, fix 5).

Rows by code (latency plan step 3, the owner's go of 2026-10-29; ``SFA_TABLES``, ``config.tables_settings``, read once
per record question; ``model`` is the default): with ``code`` the record agent's system prompt ends with
``agent.tables.RECORDS_TABLES_NOTE``, so it writes 1-3 sentences and a table line per result to show (``{{table
q3}}``), and code writes that result's rows (``agent.tables.expand``): on each draft before the draft check (the model's
history keeps its own draft; the first check sends no working-notes or number message that only code's cells set off,
and names the lines code cannot fill when another message goes back anyway or nothing else of the draft is left), and on
the final reply right after the cleaners (its table lines put on lines of their own first, so no cleaner drops one with
its sentence), so every later step reads what the user sees. The blocks' own text is a source of the number audits (a
stored line break reads as a space in a cell), the contact rule runs on the stored blocks too, and the answer carries
``code_tables`` (counts) and ``table_blocks`` (each table line's rows as they stand in the answer, for the merge call).
With ``model`` nothing of this runs.

Display words (prerequisite P3 of 2026-11-25, ``agent.display``): the pipeline passes the question without its display
words (what to fetch) and the :class:`~sf_assistant.agent.display.Display` flags (``display=``) to :meth:`RecordSession.
route`, :meth:`~RecordSession.triage` and :meth:`~RecordSession.ask`: the chart overrule and the chart note read the
flags, and a table, detail, summary, breakdown or file flag appends the display note (``display.note``) after the chart
note. Without ``display`` (any other caller) the text is read as before.

Records retry (prerequisite P2 of 2026-11-25): a records-routed (or chart-overruled) question that is no people question
gets ONE forced round (``tool_choice`` "required", ``RECORDS_TOOLS_FORCED_MESSAGE``) when its first draft ran no record
tool (at the require-tools nudge, in place of its text) or, at the first draft check, when no tool has run or every
call failed (``RECORDS_ALL_FAILED_MESSAGE``; never after a not-found, refusal or engine code, :func:`retry_kind`), or
when only ``describe_fields`` / the sync status ran and the draft states numbers the question does not hold
(``RECORDS_RUN_QUERY_MESSAGE``). A NO_RECORDS draft is forced only after a RECORDS route and never on a chart question
(the chart note asks for NO_RECORDS when nothing is named to count). With no tool run, the draft check sends
``RECORDS_REQUIRE_TOOLS_MESSAGE`` where it sent the final-form message ("from the tool results above" pushed the model
to answer with no data). Review fixes of 2026-11-25: the forced round counts no draft (the draft after it gets the full
:meth:`RecordSession.check_draft`), there is no second retry message once it is spent, a forced NO_RECORDS draft whose
round read no data keeps its NO_RECORDS (the knowledge answer), and numbers that still rest on no data call are no
answer. The answer then carries ``no_data`` ("no_call" / "all_failed") and ``tool_retry``; the pipeline writes the
clarifying question from them (``agent.clarify``).

Salesforce conversation memory (design v2 of 2026-11-25 §7, §10; the ask service with ``SFA_MEMORY=on``):
:meth:`RecordSession.ask` takes the job's rebuilt terms (``terms``), a :class:`SeedRun` of the stored or patched plan
(``seed``: its calls run through the record tools right after ``begin_question`` and the record agent starts from their
results), the last answer for a question about it (``earlier``) and ``triage=False`` for a display-only patch. The lines
a re-run plan adds (kept dates, no rows now, a changed record set) and the agent's note for a question about the last
answer are prompts/memory_exec.md (:data:`MEMORY_LINES`). Without them nothing of this runs.

Record data must stay on this machine: a session refuses any endpoint that is not localhost.

Module layout (stage-1 split of okrec/session.py): this module keeps :class:`RecordSession`, ``ROUTE_HERE``,
``LOCAL_HOSTS`` and :func:`is_local_endpoint`. The text checks and cleaners are in :mod:`sf_assistant.agent.checks`,
the result dataclasses in :mod:`sf_assistant.agent.results`, the draft nudges and checks in
:mod:`sf_assistant.agent.nudges`, the names parsing and :class:`Triage` in :mod:`sf_assistant.agent.triage`, the
people answers in :mod:`sf_assistant.agent.people_answer` and the merge in :mod:`sf_assistant.agent.merge`. This
module exports OLD ``okrec/session.py``'s ``__all__`` (the same objects as in the defining modules) without the limit
``MERGE_FACTS_CHARS``; every other name, private helpers included, is imported from the module that defines it. A limit
(``merge.MERGE_FACTS_CHARS``, ``merge.MERGE_MAX_TOKENS``, ``tools.record_base.MAX_PEOPLE``, ``record_prompts.NAMES_MAX_TOKENS``
...) lives in one module and is read there at call time: read and patch it there.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

from sf_assistant import prompts as _prompt_files                 # prompts/memory_exec.md
from sf_assistant.agent import record_prompts as _record_prompts   # NAMES_MAX_TOKENS: read (and patched) there
from sf_assistant.agent import tables as _tables                  # the code-built rows (latency plan step 3)
from sf_assistant.agent import timeline                          # the answer's timeline (latency logging)
from sf_assistant.agent import triage as _triage                  # VALUES_MESSAGE_MAX: read (and patched) there
from sf_assistant.tools import record_base as _record_base        # MAX_PEOPLE: read (and patched) there
from sf_assistant.agent.knowledge_agent import AgentResult, answer_question
from sf_assistant.agent.question import Question
from sf_assistant.config import LIMITS, charts_settings, tables_settings
from sf_assistant.agent import display as _display                # the display words (prerequisite P3)
from sf_assistant.agent.record_prompts import (
    NAMES_SYSTEM, NO_RECORDS, RECORDS, RECORDS_ALL_FAILED_MESSAGE, RECORDS_FINAL_FORM_MESSAGE,
    RECORDS_FORCE_ANSWER_MESSAGE, RECORDS_TOOLS_FORCED_MESSAGE,
    RECORDS_NOT_FOUND_MESSAGE, RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE, RECORDS_PERSON_NO_RECORDS_MESSAGE,
    RECORDS_OTHER_PEOPLE_LINE, RECORDS_REQUIRE_TOOLS_MESSAGE, RECORDS_RUN_QUERY_MESSAGE, RECORDS_UNSUPPORTED_MESSAGE,
    RECORDS_VALUE_NO_RECORDS_MESSAGE, ROUTE_SYSTEM, render_records_system)
from sf_assistant.engine.catalog import PERSON_OBJECTS
from sf_assistant.engine.contact import asks_for_contact, drop_contact_details, result_numbers   # phase 1
from sf_assistant.engine.types import DATA_TOOLS, PERSON_TOOLS, QueryEvidence, RecordsStatus, RecordsUnavailable
from sf_assistant.prompts import fill as _fill

# What the record agent itself uses from the modules the stage-1 split made of okrec/session.py.
from sf_assistant.agent.checks import (
    ENGINE_CODES, NOT_FOUND_CODES, REFUSAL_CODES, _error_code, _TextToolCalls, _is_no_records, _is_source_line,
    audit_numbers, draft_needs_final_form, drop_ref_codes, drop_tool_talk, drop_unsupported, drop_working_sentences,
    facts_json, numbers_in, reference_codes, strip_reasoning, strip_thinking)
from sf_assistant.agent.results import RecordAnswer
from sf_assistant.agent.nudges import (
    _counted_objects, _has_name, _name_missing, _no_data_but_not_found, _norm_label, _norm_text, _unsynced_nudges,
    breakdown_nudges, covers_all_named, date_nudges, drop_overlap_clauses, lookup_nudges, mirror_nudges,
    no_data_nudges, not_found_answer, not_found_reply, noun_nudges, overlap_nudges, people_nudges, shared_name_nudges,
    unfit_lead_zero_nudges, with_no_data_line, with_source_line)
from sf_assistant.agent.nudges import direction_nudges, relation_wording_nudges   # Group B item 10 (docs/people.md)
from sf_assistant.agent.nudges import child_copy_nudges, paths_nudges   # Group B items 8 and 9 (review fixes)
from sf_assistant.agent.nudges import answer_text, relation_lookup_nudges   # evaluation fixes 2 and 5 (2026-10-22)
from sf_assistant.agent.nudges import nudge_kinds   # the prompt ids of a draft-check message (step-3 fix T5)
from sf_assistant.agent.triage import Triage, _name_key, _name_tokens, parse_names
from sf_assistant.agent.triage import (MISSING_OVERRULE_REASON, PERSON_OVERRULE_REASON, strong_value,  # value spots
                                       value_named, value_reason, value_text)
from sf_assistant.agent.people_answer import (_quoted, agent_people_result, named_spellings, people_answer,
                                              people_tool_nudges)
from sf_assistant.agent.merge import drop_record_side_caveats, fix_any_role_scope, warehouse_availability
from sf_assistant.agent.term_notes import drop_unapplied_scope_claims, with_term_line   # business-term contracts

# The rest of the public surface of OLD okrec/session.py (its ``__all__``), re-exported for the callers that import
# it from here (pipeline, score_records, the tests): the same objects as in the defining modules. Any other name is
# imported from the module that defines it. A module constant is read, and patched, in its own module only
# (merge.MERGE_FACTS_CHARS, merge.MERGE_MAX_TOKENS, nudges._MAX_FOOTER_OTHERS ...): it is not re-exported here.
from sf_assistant.agent.checks import (  # noqa: F401
    NOT_FOUND_CODES, QUOTED_SPAN_RE, numbers_in, quoted_spans, text_tool_call, working_notes)
from sf_assistant.agent.results import MergeResult  # noqa: F401
from sf_assistant.agent.nudges import (  # noqa: F401
    NAME_NOT_FOUND_CODES, field_check_lines, field_checks, lookup_checks, unsynced_checks, with_lookup_footer)
from sf_assistant.agent.merge import (  # noqa: F401
    availability_issues, clean_knowledge_answer, combine, drop_side_caveats, merge_scope_issues, name_notes,
    side_caveats, with_name_note)

if TYPE_CHECKING:  # type hints only
    from sf_assistant.agent.llm import LLM
    from sf_assistant.config import Config

__all__ = ["is_local_endpoint", "RecordAnswer", "RecordSession", "MergeResult", "combine", "numbers_in",
           "audit_numbers", "facts_json", "strip_thinking", "strip_reasoning", "working_notes", "text_tool_call",
           "LOCAL_HOSTS", "NOT_FOUND_CODES", "lookup_checks", "lookup_nudges", "unsynced_checks", "people_nudges",
           "not_found_reply", "field_checks", "field_check_lines", "with_lookup_footer", "overlap_nudges",
           "date_nudges", "mirror_nudges", "merge_scope_issues", "shared_name_nudges", "no_data_nudges",
           "noun_nudges", "drop_working_sentences", "with_source_line", "clean_knowledge_answer",
           "warehouse_availability", "availability_issues", "breakdown_nudges", "unfit_lead_zero_nudges",
           "not_found_answer", "NAME_NOT_FOUND_CODES", "drop_tool_talk", "side_caveats", "drop_side_caveats",
           "fix_any_role_scope", "draft_needs_final_form", "QUOTED_SPAN_RE", "quoted_spans", "drop_unsupported",
           "drop_overlap_clauses", "drop_record_side_caveats", "name_notes", "with_name_note", "Triage",
           "parse_names", "people_tool_nudges", "people_answer", "PERSON_TOOLS"]

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_local_endpoint(base_url: str) -> bool:
    """True when the URL's hostname is localhost, 127.0.0.1 or ::1."""
    try:
        host = urlparse(str(base_url or "")).hostname
    except ValueError:
        return False
    return (host or "").lower() in LOCAL_HOSTS


def _chart_note(question: str, display: Any = None) -> str:
    """The end of the record agent's system prompt for a chart question (``agent.charts.note_for``); "" otherwise, and
    with ``SFA_CHARTS`` off, which imports nothing. With ``display`` (the question's display flags, prerequisite P3)
    the flags decide: a chart flag on a question that names something to fetch."""
    if not charts_settings().on:
        return ""
    if display is not None:
        if not (getattr(display, "chart", False) and not getattr(display, "empty", False)):
            return ""
        from sf_assistant.agent.charts import RECORDS_CHART_NOTE
        return RECORDS_CHART_NOTE
    from sf_assistant.agent.charts import note_for
    return note_for(question)


def _chart_question(question: str, display: Any = None) -> bool:
    """The question asks for a chart and names something to chart (the flags with ``display``), with the switch on."""
    if not charts_settings().on:
        return False
    if display is not None:
        return bool(getattr(display, "chart", False) and not getattr(display, "empty", False))
    from sf_assistant.agent.charts import drawable_ask
    return drawable_ask(question)


def retry_kind(reply: str, evidence: list) -> str | None:
    """Why a record draft has no data to rest on (prerequisite P2): "no_call" when no record tool has run,
    "all_failed" when every call failed and none failed for a name, field or object that is not there
    (``NOT_FOUND_CODES``), for a blocked field or object (``REFUSAL_CODES``) or for a fault of the engine
    (``ENGINE_CODES``: the time budget, the query guard, DuckDB or I/O, an internal error; review of 2026-11-25: those
    stay a failure, so the caller's fallback runs); None otherwise. ``reply`` is not read: the caller decides what a
    NO_RECORDS draft gets (:refusal codes: ``agent.checks.REFUSAL_CODES``, the package's one home)."""
    ev = list(evidence or [])
    if not ev:
        return "no_call"
    if any(getattr(e, "ok", False) for e in ev):
        return None
    if {_error_code(e) for e in ev} & (NOT_FOUND_CODES | REFUSAL_CODES | ENGINE_CODES):
        return None
    return "all_failed"


def _data_call_ok(evidence: list) -> bool:
    """A successful call that read record data: a data tool other than the sync status (``describe_fields`` and
    ``find_objects`` only help pick the object and field; their counts ignore filters and periods)."""
    return any(getattr(e, "ok", False) and getattr(e, "kind", None) == "data"
               and getattr(e, "tool", None) != "get_sync_status" for e in evidence or [])


def _coded_failure(evidence: list) -> bool:
    """A failed call with a not-found, refusal or engine code: its own checks answer it (never a forced round)."""
    return bool({_error_code(e) for e in evidence or [] if not getattr(e, "ok", False)}
                & (NOT_FOUND_CODES | REFUSAL_CODES | ENGINE_CODES))


def _new_numbers(reply: str, question: str) -> list[str]:
    """The numbers of ``reply``'s answer lines (Source and code lines left out) that the question does not hold, the
    integers 0-12 included (an orientation call's count is no count of the asked records)."""
    asked = set(numbers_in(question or ""))
    return [n for n in numbers_in(answer_text(reply or "")) if n not in asked]


def _forcing_off(llm: Any) -> bool:
    fn = getattr(llm, "forcing_off", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------------------- seeded calls
#: The lines code writes when a stored query plan runs again as seeded calls, and the record agent's note for a
#: question about the last answer (prompts/memory_exec.md; Salesforce conversation memory, design v2 §6.7, §7.4, §10).
MEMORY_EXEC_TEXT = _prompt_files.text("memory_exec")
MEMORY_LINES: dict[str, str] = dict(ln.split(": ", 1) for ln in MEMORY_EXEC_TEXT.split("\n") if ": " in ln)
#: Why the seeded calls stopped before the main call (:attr:`SeedRun.stop`): each has a ``stop_<code>`` line.
SEED_STOPS = ("rank_changed", "namesakes", "empty", "set_failed")


@dataclasses.dataclass
class SeedRun:
    """The seeded calls of a stored or patched query plan (Salesforce conversation memory, design v2 §7.1, §7.4, §10).

    :meth:`RecordSession.ask` runs them with :meth:`run` right after ``begin_question``, so each goes through
    ``RecordTools.call`` like a call of the record agent: validated, enforced with the job's terms (never the stored
    ``enforced_arguments``), grounded and logged as evidence. The record agent then starts from their results.

    ``calls``: the ``agent.query_state.RunCall`` list (``query_state.run_calls``), in plan order; they are sent side
    calls first and the main call last, in plan order otherwise; ``today`` and
    ``label``: the business date and the catalog's display words, for the plan line of the "no rows now" line. A call
    with ``keys_from`` (a stored record set, §10) first runs the set's call fresh and filters on its keys
    (``query_state.keys_filter``); the keys live only for this run. After :meth:`run`:

    - ``ran``: the RunCall of each plan call sent, in the order sent (``query_state.build(seeds=...)``);
    - ``pairs``: ``(tool, arguments, payload)`` of every call sent, the fresh run of a set included
      (``answer_question(seed=...)``);
    - ``lines``: the code lines for the answer: the stored dates kept (D1), no rows now where the stored call found
      some (C7), a record set that changed since it was shown;
    - ``stop``: ``""``, or why a record set cannot become a filter (:data:`SEED_STOPS`): the main call is not sent,
      no model call follows and the answer is :meth:`stop_text`; ``stop_rank`` is the rank of an ordinal drill.
    """

    calls: list = dataclasses.field(default_factory=list)
    today: Any = None
    label: Any = None
    ran: list = dataclasses.field(default_factory=list)
    pairs: list = dataclasses.field(default_factory=list)
    lines: list = dataclasses.field(default_factory=list)
    stop: str = ""
    stop_rank: int | None = None

    def _line(self, key: str, **fill: Any) -> None:
        text = _fill(MEMORY_LINES[key], **{k: str(v) for k, v in fill.items()})
        if text not in self.lines:
            self.lines.append(text)

    def run(self, tools: Any) -> list[tuple[str, dict, dict]]:
        """Send the calls through ``tools.call`` (evidence logged as for any call); the ``pairs``. Never raises a
        fault of a call: a failed call is its payload, as for the record agent."""
        from sf_assistant.agent import query_state as qs

        self.ran, self.pairs, self.lines, self.stop, self.stop_rank = [], [], [], "", None
        # the main call last (stage-2 review fix M2): the chart is drawn from the last chartable result and the next
        # plan's main call is the last chartable call, so both stay the plan's main call after an operation that
        # added a side call (compare_period on a grouping or a trend)
        for rc in sorted(self.calls, key=lambda c: getattr(c, "role", "") == "main"):
            args = rc.args
            if rc.keys_from:
                tool = str(rc.keys_from.get("tool") or "")
                got, evs = self._send(tools, tool, rc.set_args)
                if not got.get("ok") or not evs:
                    self.stop = "set_failed"
                    break
                item, status = qs.keys_filter(rc.keys_from, qs.set_keys(evs[-1]))
                if item is None:
                    self.stop = status if status in SEED_STOPS else "set_failed"
                    self.stop_rank = rc.keys_from.get("rank")
                    break
                if status == "changed":
                    self._line("set_changed")
                if rc.set_kept_dates:
                    self._kept(rc.set_args)
                args = qs.with_item(rc.tool, rc.args, item)
            _got, evs = self._send(tools, rc.tool, args)
            self.ran.append(rc)
            if rc.kept_dates:
                self._kept(args)
            if evs and qs.stale_empty(rc, evs[-1]):
                line = qs.plan_line({"calls": [dict(rc.entry, role="main")]}, today=self.today, label=self.label)
                self._line("no_rows_now", PLAN=line)
        return list(self.pairs)

    def _send(self, tools: Any, tool: str, args: dict) -> tuple[dict, list]:
        before = len(tools.evidence)
        got = tools.call(tool, args)
        got = got if isinstance(got, dict) else {"ok": False, "result": None, "error": "no result", "chunk_ids": []}
        self.pairs.append((tool, args, got))
        return got, list(tools.evidence)[before:]

    def _kept(self, args: dict) -> None:
        """The D1 note: the stored dates, as the call sends them (an absolute range)."""
        periods = [args["period"]] if isinstance(args.get("period"), str) else [
            s.get("period") for s in args.get("segments") or [] if isinstance(s, dict) and isinstance(s.get("period"), str)]
        if periods:
            self._line("kept_dates", PERIOD=" and ".join(periods))

    def stop_text(self) -> str:
        """The clarifying line of :attr:`stop` ("" when the calls did not stop)."""
        if not self.stop:
            return ""
        return _fill(MEMORY_LINES[f"stop_{self.stop}"], N=str(self.stop_rank or ""))


def earlier_block(earlier: str) -> str:
    """The record agent's note and the earlier answer for a question about the last answer (design v2 §6.7): the
    note, then the heading and the answer as one JSON string (at most 2,000 characters), so no text in it can end its
    part. "" for no earlier answer."""
    text = str(earlier or "").strip()[:2000]
    if not text:
        return ""
    return (MEMORY_LINES["earlier_note"] + "\n\n" + MEMORY_LINES["earlier_head"] + "\n"
            + json.dumps(text, ensure_ascii=False))


# ------------------------------------------------------------------------------------- session

#: Default of ``RecordSession.ask(routed=)``: make the route call inside ``ask``.
ROUTE_HERE: Any = object()


class RecordSession:
    """The record agent over one open :class:`sf_assistant.tools.record_tools.RecordTools`."""

    def __init__(self, tools: Any, cfg: "Config", llm: "LLM | None" = None, max_steps: int = LIMITS.records_max_steps) -> None:
        if not is_local_endpoint(getattr(cfg, "base_url", "")):
            raise RecordsUnavailable("OKEVAL_BASE_URL is not on this machine; record data must stay local")
        self.tools = tools
        self.cfg = dataclasses.replace(cfg, max_tool_steps=int(max_steps))
        if llm is None:
            from sf_assistant.agent.llm import LLM

            llm = LLM(self.cfg)
        self.llm = llm
        self._n = 0
        self._lock = threading.Lock()
        self._last_triage: tuple[str, Triage] | None = None

    def status(self) -> RecordsStatus:
        """Where the records come from and how fresh they are (recomputed now)."""
        return self.tools.status()

    def availability(self) -> Any:
        """:func:`warehouse_availability` over the tools' catalog (for :func:`combine`), or None without a catalog."""
        catalog = getattr(self.tools, "catalog", None)
        return warehouse_availability(catalog) if catalog is not None and getattr(catalog, "objects", None) else None

    def close(self) -> None:
        """Close the snapshot connection."""
        try:
            self.tools.close()
        except Exception:  # noqa: BLE001 - closing must never fail a run
            pass

    def route(self, question: str, display: Any = None) -> bool | None:
        """True = the question needs record data, False = design only, None = unclear: :meth:`triage`'s ``routed``
        (True for a question that names people, or a stored record strongly against a NO_RECORDS route, else the route
        call's answer). Never raises; the triage is kept for the next :meth:`ask` of the same question
        (:meth:`last_triage`). ``display``: the question's display flags (``agent.display``), or None."""
        return (self.triage(question, display=display) if display is not None else self.triage(question)).routed

    def _route_call(self, question: str) -> bool | None:
        """One short model call (``ROUTE_SYSTEM``): True = RECORDS, False = NO_RECORDS, None = unclear or failed."""
        try:
            resp = self.llm.chat([{"role": "system", "content": ROUTE_SYSTEM}, {"role": "user", "content": question}],
                                 max_tokens=8)
        except Exception:  # noqa: BLE001 - routing is an optimisation, never a failure
            return None
        word = re.sub(r"[^A-Z_]", "", strip_thinking((resp or {}).get("content")).upper().replace(" ", "_"))
        if word.startswith(NO_RECORDS):
            return False
        if word.startswith(RECORDS):
            return True
        return None

    def _names_call(self, question: str) -> list[str] | None:
        """One small model call (``NAMES_SYSTEM``, at most NAMES_MAX_TOKENS tokens): the names of people the question
        mentions (:func:`parse_names`), [] for none, None when the call failed."""
        try:
            resp = self.llm.chat([{"role": "system", "content": NAMES_SYSTEM}, {"role": "user", "content": question}],
                                 max_tokens=_record_prompts.NAMES_MAX_TOKENS)
        except Exception:  # noqa: BLE001 - the triage falls back to the strong spots
            return None
        return parse_names((resp or {}).get("content"), question)

    def _tools_fn(self, name: str) -> Any:
        fn = getattr(self.tools, name, None)
        return fn if callable(fn) else None

    def _value_spots(self, question: str, people: list[dict]) -> list[dict]:
        """The listed value spots of ``question`` (``tools.spot_values``, value spots 2026-10-28): stored record values
        the question names, with ``people`` (the confirmed and the hinted people, [] for none) owning their words.
        [] for tools without ``spot_values`` (the triage as before) or on any failure. Never raises."""
        fn = self._tools_fn("spot_values")
        if fn is None:
            return []
        try:
            res = fn(question, people=list(people))
        except Exception:  # noqa: BLE001 - optional: the triage as before
            return []
        return [dict(d) for d in res if isinstance(d, dict) and d.get("listed")] if isinstance(res, list) else []

    def triage(self, question: str, route: Any = ROUTE_HERE, display: Any = None) -> Triage:
        """Who the question names (RECORDS_SPEC §22.3). Never raises.

        Code spots stored names in the question (``tools.spot_people``); the route call (unless ``route`` gives its
        answer) and the names call run in parallel. Confirmed people: strong spots (except when the route said
        NO_RECORDS and the names call ran and named none of its words: a client or system user's name in a design
        question is only hinted), spots and names that the names call confirms (``tools.resolve_person_name``: a
        stored full, first or last name, the same name written another way, or a word of it that is a stored first or
        last name); when the names call failed, weak spots only when the route said RECORDS, and only their Employee
        and User records; when the names call named nobody and the route did not say RECORDS, a weak spot that
        ``tools.first_name_fit`` keeps in play (one stored person has that first name and a lookup holding them fits
        a word of the question; Group B item 13, RECORDS_SPEC §22.13), with its ``first_name_fit`` (word, lookup,
        records) in the spot and in :meth:`Triage.as_dict`. ``missing``: names of the names call that no stored person has, nor a word of them, with a
        word that is no design word (a client company's name is neither). A person overrules the route (``routed``
        True) unless only a last name of it is stored; a missing name overrules it only when it is a close spelling of
        a staff first name (``tools.staff_close_name``) or has 2 or more words and no design word; else ``routed`` is
        the route call's answer. Tools without
        ``resolve_person_name`` get no names call (the route call alone, as before); tools without ``first_name_fit``
        (or a fit of None, or one that raises) keep the triage as before item 13.

        Value spots (2026-10-28): after the people are decided, ``tools.spot_values(question, people=<the confirmed
        and the hinted people>)`` gives the stored record values the question names; the listed ones are
        ``Triage.values``. A names-call name whose every word that is no design word lies inside a listed spot of kind
        name, code or text is no missing person (:func:`~sf_assistant.agent.triage.value_named`). When no person and
        no missing name overruled the route, the route call said NO_RECORDS and a listed spot is strong
        (:func:`~sf_assistant.agent.triage.strong_value`: no picklist value, no record type name), ``routed`` is True
        with ``overruled_by`` "value" and the first strong spot as the reason; an unclear route stays None and
        RECORDS needs no overrule. ``overruled_by`` is "person" or "missing name" when those overrules routed a
        question the route call did not call RECORDS (log only). Tools without ``spot_values``: no values, the triage
        as before.

        Charts (``SFA_CHARTS=on``): when ``routed`` is still False and the question asks for a chart and names something
        to chart (``agent.charts.drawable_ask``; with ``display``, its chart flag on a question that is not display-only,
        prerequisite P3), ``routed`` is True with ``overruled_by`` "chart". An unclear route stays None and RECORDS
        needs no overrule. Off, this reads nothing else."""
        t0 = time.perf_counter()
        spot = self._tools_fn("spot_people")
        resolve = self._tools_fn("resolve_person_name")
        try:
            spots = [d for d in (spot(question) if spot else []) if isinstance(d, dict) and d.get("words")]
        except Exception:  # noqa: BLE001 - optional
            spots = []
        timeline.add("stage", t0, stage="people_spots")
        rt: bool | None = None if route is ROUTE_HERE else route
        names: list[str] | None = None
        if route is ROUTE_HERE and resolve is not None:
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                f_route = pool.submit(timeline.run_in, "route", self._route_call, question)
                f_names = pool.submit(timeline.run_in, "names", self._names_call, question)
                rt, names = f_route.result(), f_names.result()
            finally:
                pool.shutdown(wait=False)
        elif route is ROUTE_HERE:
            with timeline.stage("route"):
                rt = self._route_call(question)
        elif resolve is not None:
            with timeline.stage("names"):
                names = self._names_call(question)
        design_fn = self._tools_fn("is_design_word")
        close_fn = self._tools_fn("staff_close_name")
        fit_fn = self._tools_fn("first_name_fit")

        def design_word(w: str) -> bool:
            try:
                return bool(design_fn(w)) if design_fn else False
            except Exception:  # noqa: BLE001
                return False

        def first_fit(q: str, d: dict) -> dict | None:
            """``tools.first_name_fit`` as ``{"word", "lookup", "records"}`` (the trace keeps no stored name), or None."""
            try:
                fit = fit_fn(q, d) if fit_fn else None
            except Exception:  # noqa: BLE001 - optional: today's triage
                return None
            if not isinstance(fit, dict) or not fit.get("lookup"):
                return None
            return {"word": fit.get("word"), "lookup": fit.get("lookup"), "records": fit.get("records")}

        def words(d: dict) -> set[str]:
            return set(str(d["words"]).split())
        people: list[dict] = []

        def add(d: dict) -> None:
            ws = words(d)
            if len(people) < _record_base.MAX_PEOPLE and not any(ws <= words(p) or words(p) <= ws for p in people):
                people.append(d)

        # The names call's names: stored (resolve_person_name), or not.
        resolved: list[tuple[set[str], dict | None, str]] = []
        for n in names or []:
            nt = set(_name_tokens(n))
            r = None
            if not any(nt <= words(d) for d in spots if d.get("strength") == "strong"):
                try:
                    r = resolve(n) if resolve is not None else None
                except Exception:  # noqa: BLE001
                    r = None
            resolved.append((nt, r if isinstance(r, dict) and r.get("recs") else None, n))
        hint_only: list[dict] = []
        for d in spots:
            ws = words(d)
            hit = next(((nt, r) for nt, r, _n in resolved if ws == nt or (r is not None and ws <= nt)), None)
            if d.get("strength") == "strong":
                if rt is False and names is not None and not any(ws & nt for nt, _r, _n in resolved):
                    hint_only.append(d)   # NO_RECORDS, and the names call named none of its words
                else:
                    add(d)
            elif hit is not None:   # the names call read the word as a name (alone, or inside a longer name)
                add(hit[1] if hit[1] is not None and hit[0] != ws else d)
            elif names is None and rt is True:   # no names call: the staff records of a first name, as before §22
                staff = [r for r in d.get("recs") or [] if r and r[0] in ("Recruiter__c", "User")]
                if staff:
                    add({**d, "recs": staff})
            elif names == [] and rt is not True and fit_fn is not None:
                # both calls missed a stored first name (§22.13, Group B item 13): the data keeps it in play when
                # one stored person has it and a lookup holding them fits a word of the question (tools.first_name_fit)
                fit = first_fit(question, d)
                if fit:
                    add({**d, "first_name_fit": fit})
        company_fn = self._tools_fn("company_name")
        missing: list[str] = []
        for nt, r, n in resolved:
            if any(nt <= words(d) for d in people + hint_only):
                continue
            if r is not None:
                add(r)
            elif any(not design_word(w) for w in nt) and all(_name_key(m) != _name_key(n) for m in missing):
                try:
                    if company_fn is not None and company_fn(n):
                        continue   # a client company's name: no person, and no missing person either
                except Exception:  # noqa: BLE001
                    pass
                missing.append(n)
        # Value spots (2026-10-28), after the people are decided: the people own their words, and a names-call name
        # that lies inside a stored record value ("Team Zentrova" for a stored department) is no missing person.
        t_values = time.perf_counter()
        values = self._value_spots(question, people + hint_only)
        timeline.add("stage", t_values, stage="value_spots")
        if values and missing:
            missing = [m for m in missing if not value_named(m, values, design_word)]
        complete = self._tools_fn("name_index_complete")
        try:   # a name the index lacks is "not stored" only when the index read every person object to the end
            if complete is not None and not complete():
                missing = []
        except Exception:  # noqa: BLE001
            missing = []
        missing = missing[:max(0, _record_base.MAX_PEOPLE - len(people))]

        def close_staff(n: str) -> bool:
            try:
                return bool(close_fn(n)) if close_fn else False
            except Exception:  # noqa: BLE001
                return False
        # a missing name overrules the route when the data backs it (a close spelling of a staff first name) or when
        # it has the shape of a full name: 2 or more words, none of them a design word. One unknown word (a vendor or
        # product name the names call took for a name) stays with the route call.
        if any(not str(p.get("match") or "").endswith("last name") for p in people):
            people_by: str | None = "person"
        elif any(close_staff(m) or (len(_name_tokens(m)) >= 2 and not any(design_word(w) for w in _name_tokens(m)))
                 for m in missing):
            people_by = "missing name"
        else:
            people_by = None
        routed = True if people_by else rt
        by, reason = None, ""
        if people_by and rt is not True:   # log only: the people overrules decide as before
            by, reason = people_by, PERSON_OVERRULE_REASON if people_by == "person" else MISSING_OVERRULE_REASON
        strong = next((d for d in values if strong_value(d)), None)
        if people_by is None and rt is False and strong is not None:
            # a stored record the question names strongly (a Name, a code, distinctive short text) overrules a
            # NO_RECORDS route, as a named person does; picklist values and plain words never do
            routed, by, reason = True, "value", value_reason(strong)
        if routed is False and _chart_question(question, display):   # a chart question is a data question
            from sf_assistant.agent import charts as _charts
            routed, by, reason = True, "chart", _charts.OVERRULE_REASON
        tri = Triage(routed=routed, route=rt, people=people, hinted=list(people) + hint_only, missing=missing,
                     names=names, seconds=round(time.perf_counter() - t0, 3), values=values, overruled_by=by,
                     overrule_reason=reason)
        timeline.add("stage", t0, stage="triage")
        with self._lock:
            self._last_triage = (question, tri)
        return tri

    def _triage_for(self, question: str, routed: Any = ROUTE_HERE, display: Any = None) -> Triage:
        """The triage of :meth:`route` for this question when it ran (``ask.py`` routes first), else a new one; a
        ``routed`` given by the caller stands for the route call."""
        with self._lock:
            cached = self._last_triage
        if cached is not None and cached[0] == question and (routed is ROUTE_HERE or routed in (cached[1].routed,
                                                                                                cached[1].route)):
            return cached[1]
        return self.triage(question, route=routed, display=display) if display is not None else \
            self.triage(question, route=routed)

    def last_triage(self, question: str) -> Triage | None:
        """The triage that :meth:`route` (or :meth:`triage`) kept for ``question``, or None when the last triage was of
        another question or none ran: the pipeline reads ``overruled_by`` to pick the knowledge step (value spots,
        2026-10-28). Never raises."""
        with self._lock:
            cached = self._last_triage
        return cached[1] if cached is not None and cached[0] == question else None

    def check_draft(self, draft: str, question: str, status: RecordsStatus, routed: bool | None = None,
                    triage: Triage | None = None) -> str | None:
        """Retry message for the first draft answer, else None (``okeval.agent`` check_answer; the draft after a
        message gets only :meth:`recheck_draft`).

        In this order (RECORDS_SPEC §21.10), every message that applies, joined with a blank line:
        the other-lookup, linked, sparse-zero, unfit-zero and elsewhere nudges of :func:`lookup_nudges` (a count
        through a lookup whose role or object does not fit, while the person has records elsewhere: a "0" draft passes
        the number checks, so these run first); a leading 0 through a lookup with no data (:func:`no_data_nudges`);
        the not-synced, people, shared-name (:func:`shared_name_nudges`), both-objects (:func:`noun_nudges`), overlap
        (:func:`overlap_nudges`), breakdown (:func:`breakdown_nudges`: a small count the 0-12 allowance would pass),
        date-coverage (:func:`date_nudges`) and copied-lookup (:func:`mirror_nudges`) checks; a count through a copy
        of a child object's lookup (:func:`child_copy_nudges`) and a person's count through the record's own lookup
        while the parent paths reach more (:func:`paths_nudges`; Group B items 9 and 8); the relation wording of
        the about_people person lookups (:func:`relation_wording_nudges`) and the direction of a self-lookup the
        question asks about (:func:`direction_nudges`; both read the catalog through :meth:`lookup_directions`, Group B
        item 10; the direction check also reads the knowledge wording of :meth:`person_lookups`), and when that sends
        nothing, a word of the question that is in the knowledge wording of another person lookup than the one the
        answer used (:func:`relation_lookup_nudges`, evaluation fix 5); working notes that code
        cannot clean (:func:`draft_needs_final_form`: planning, two drafts, no Source line, a tool word that
        :func:`drop_tool_talk` cannot rewrite; openers and rewritable tool words cost no model call);
        then ``RECORDS_NOT_FOUND_MESSAGE`` when no data call found anything (:func:`_data_found`), one failed with a
        not-found code and the draft still gives a count (0 included: not :func:`not_found_reply`); else ``RECORDS_RUN_QUERY_MESSAGE`` for a draft with numbers
        (beyond 0-12 and those of the question) but no successful query tool call, or
        ``RECORDS_UNSUPPORTED_MESSAGE`` for numbers found in no tool result. NO_RECORDS replies pass, except when
        the question names people (``triage``: confirmed people or names no stored person has, whatever the route
        said; without a triage: the route said True and the question holds a stored person's full name): then
        ``RECORDS_PERSON_NO_RECORDS_MESSAGE``; else, when the triage has a strong listed value spot (a stored record the
        question names, value spots 2026-10-28), ``RECORDS_VALUE_NO_RECORDS_MESSAGE`` naming those values. Before every
        other check, :func:`people_tool_nudges` asks for about_people when the evidence does not cover the people the
        question names (RECORDS_SPEC §22.8).
        """
        reply = strip_reasoning(draft)
        # no record tool has run: "write the final answer from the tool results above" would push the model to answer
        # with no data (prerequisite P2, 2026-11-25); it is asked for the record tools instead
        final_form = RECORDS_FINAL_FORM_MESSAGE if self.tools.evidence else RECORDS_REQUIRE_TOOLS_MESSAGE
        if not reply:
            # a draft that is all reasoning would be shown as nothing: one more round for the answer (2026-11-16)
            return final_form if str(draft or "").strip() else None
        if not _is_no_records(reply) and not answer_text(drop_tool_talk(drop_working_sentences(reply))):
            # the cleaners would leave nothing but the Source line ("empty record answer", page ticket 3 of
            # 2026-11-16): ask for the final answer now, while rounds are left, instead of dropping the reply
            return final_form
        if _is_no_records(reply):
            if triage is not None and (triage.people or triage.missing):
                return RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", _quoted(triage.asked()))
            if triage is None and routed is True and self._names_person(question):
                return RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "a stored person's full name")
            strong = triage.strong_values() if triage is not None else []
            if strong:   # code matched words of the question to stored records (value spots, 2026-10-28)
                named = list(dict.fromkeys(value_text(d) for d in strong))[:max(1, _triage.VALUES_MESSAGE_MAX)]
                return _fill(RECORDS_VALUE_NO_RECORDS_MESSAGE, VALUES=", ".join(named))
            return None
        evidence = self.tools.evidence
        messages = people_tool_nudges(reply, evidence, triage)
        messages += lookup_nudges(reply, evidence)
        messages += no_data_nudges(reply, evidence)
        messages += _unsynced_nudges(reply, evidence)
        messages += people_nudges(reply, self._question_people())
        messages += shared_name_nudges(reply, evidence)
        messages += noun_nudges(reply, evidence, self._question_objects())
        messages += overlap_nudges(reply, evidence)
        messages += breakdown_nudges(reply, evidence, self._status_values(_counted_objects(evidence)))
        messages += date_nudges(evidence)
        messages += mirror_nudges(evidence)
        messages += child_copy_nudges(reply, evidence)
        messages += paths_nudges(reply, evidence)
        messages += relation_wording_nudges(reply, evidence, self.lookup_directions)
        direction = direction_nudges(reply, evidence, question, self.lookup_directions, lookups=self.person_lookups)
        messages += direction or relation_lookup_nudges(reply, evidence, question, self.person_lookups,
                                                        people=triage.people if triage is not None else None)
        if draft_needs_final_form(reply):
            messages.append(final_form)
        people_data = triage.people_data() if triage is not None else True
        if _no_data_but_not_found(evidence, people_data) and not not_found_reply(reply, evidence, question, status):
            messages.append(RECORDS_NOT_FOUND_MESSAGE)
        else:
            number = self._number_message(reply, question, status, evidence)
            if number:
                messages.append(number)
        return "\n\n".join(messages) or None

    def recheck_draft(self, draft: str) -> str | None:
        """Retry message for the draft written after a first nudge (``okeval.agent`` calls it with ``max_checks=2``),
        else None: :func:`unfit_lead_zero_nudges` (the draft still leads with the 0 of a lookup whose role does not
        fit the question's word while a lookup that fits holds the person) and :func:`overlap_nudges` (a made-up overlap
        written in the second draft, often while fixing what the first nudge asked for). Code removes what is still
        left after that (:func:`drop_overlap_clauses`)."""
        reply = strip_reasoning(draft)
        if not reply or _is_no_records(reply):
            return None
        evidence = self.tools.evidence
        return "\n\n".join(unfit_lead_zero_nudges(reply, evidence) + overlap_nudges(reply, evidence)) or None

    @staticmethod
    def _number_message(reply: str, question: str, status: RecordsStatus, evidence: list[QueryEvidence]) -> str | None:
        if not audit_numbers(reply, [question, status.line()]):
            return None
        if not any(e.ok and e.kind == "data" for e in evidence):
            return RECORDS_RUN_QUERY_MESSAGE
        bad = audit_numbers(reply, [facts_json(evidence, 10**9), question, status.line()])
        if bad:
            return RECORDS_UNSUPPORTED_MESSAGE.replace("<<NUMBERS>>", ", ".join(bad[:8]))
        return None

    def _own_words_messages(self, msg: str, seen: str, x: Any, question: str, status: RecordsStatus) -> str | None:
        """``SFA_TABLES=code`` (review of 2026-10-30): the first check's ``msg`` on a draft whose table lines code
        filled (``seen``: the draft as the user would see it; ``x``: its ``agent.tables.Expansion``) without what only
        code's rows set off, which the model never typed and cannot change (it costs a round, and the model may drop
        its table lines to obey): ``RECORDS_FINAL_FORM_MESSAGE`` when the model's own text (its table lines in place
        of the rows) reads as no working notes (a stored note "I will call back" in a cell), and the number message
        with the rows' own text as one more source (a stored line break reads as "n15" in the facts and as "15" in a
        cell). :meth:`check_draft` itself is unchanged; the other checks keep reading the rows."""
        def without(text: str, part: str) -> str:
            for cut in ("\n\n" + part, part + "\n\n", part):
                if cut in text:
                    return text.replace(cut, "", 1)
            return text

        reply = strip_reasoning(seen)
        if RECORDS_FINAL_FORM_MESSAGE in msg and not draft_needs_final_form(_tables.marked(reply, x.blocks) or reply):
            msg = without(msg, RECORDS_FINAL_FORM_MESSAGE)
        evidence = self.tools.evidence
        number = self._number_message(reply, question, status, evidence)
        if number and number != RECORDS_RUN_QUERY_MESSAGE and number in msg:
            bad = audit_numbers(reply, [facts_json(evidence, 10**9), question, status.line()] + x.sources)
            msg = (msg.replace(number, RECORDS_UNSUPPORTED_MESSAGE.replace("<<NUMBERS>>", ", ".join(bad[:8])), 1)
                   if bad else without(msg, number))
        return msg or None

    def _people_result(self, asked: list[str]) -> dict | None:
        """A successful about_people result that took every asked name (the agent's own call), else one call made
        here (logged as evidence, with a note that code ran it); None when it fails."""
        keys = {_name_key(w) for w in asked}
        for e in reversed(self.tools.evidence):
            took = {_name_key(n) for n in (e.applied or {}).get("names") or []}
            if e.ok and e.tool in PERSON_TOOLS and keys <= took and isinstance(e.model_result, dict):
                return e.model_result
        call = getattr(self.tools, "call", None)
        if not callable(call) or not asked:
            return None
        try:
            out = call("about_people", {"names": list(asked)})
        except Exception:  # noqa: BLE001 - optional
            return None
        try:
            ev = self.tools.evidence
            if ev and ev[-1].tool == "about_people":
                ev[-1].notes.append("run by code after the record agent gave no answer")
        except Exception:  # noqa: BLE001
            pass
        return out.get("result") if isinstance(out, dict) and out.get("ok") and isinstance(out.get("result"), dict) \
            else None

    def _other_people_line(self, answer: str, tri: Triage) -> str:
        """A forced answer that names one of several people sharing an asked first name (:func:`people_nudges` would
        have nudged) gets "Other stored people named '<w>': <name> (<kind>), ...; this answer does not cover them.",
        unless the answer makes a statement about every person with that name (:func:`covers_all_named`: "No other
        Kilo has ...", "any Kilo (employee or candidate)")."""
        kind_of = self._tools_fn("kind_of")
        out = answer
        for p in self._question_people():
            if not people_nudges(answer, [p]) or not p.get("first_only"):
                continue
            if covers_all_named(answer, str(p.get("words") or "")):
                continue
            text = _norm_text(answer)
            used = {_norm_text(n) for n in p.get("names") or [] if _has_name(text, n)}
            spot = next((d for d in tri.people if d.get("words") == p.get("words")), None)
            others: list[str] = []
            for r in (spot or {}).get("recs") or []:
                nm = " ".join(str(r[2]).replace("\xa0", " ").split())
                if _norm_text(nm) in used or any(o.startswith(nm + " (") for o in others):
                    continue
                kind = ""
                try:
                    kind = kind_of(r[0], r[3]) if kind_of else ""
                except Exception:  # noqa: BLE001
                    kind = ""
                others.append(f"{nm} ({kind})" if kind else nm)
            if others:
                line = _fill(RECORDS_OTHER_PEOPLE_LINE, WORDS=p.get('words'), OTHERS=", ".join(others[:8]))
                lines = out.rstrip().split("\n")
                at = len(lines)
                while at > 0 and (not lines[at - 1].strip() or _is_source_line(lines[at - 1])):
                    at -= 1
                out = "\n".join(lines[:at] + ["", line] + ([""] + [x for x in lines[at:] if x.strip()]
                                                            if any(x.strip() for x in lines[at:]) else []))
        return out

    def _status_values(self, objects: list[str]) -> set[str]:
        """The tools' ``status_values(objects)`` as breakdown labels (empty when the tools have none; never raises)."""
        fn = getattr(self.tools, "status_values", None)
        try:
            return {_norm_label(v) for v in (fn(objects) if callable(fn) else ()) if str(v or "").strip()}
        except Exception:  # noqa: BLE001 - optional
            return set()

    def lookup_directions(self, obj: str, field: str) -> dict | None:
        """The catalog's view of the lookup ``<obj>.<field>`` for the relation and direction checks (Group B item 10):
        ``{"label", "target", "person", "from_source", "from_target", "role"}`` (``person``: the target is a person object;
        the texts are the knowledge ``direction_semantics``: the catalog's ``from_source`` / ``from_target``, else the
        object's relationship entry), or None for a field that is no lookup or tools without a catalog. Never raises."""
        cat = getattr(self.tools, "catalog", None)
        try:
            spec = cat.objects.get(str(obj)) if cat is not None else None
            f = spec.fields.get(str(field)) if spec is not None else None
            if f is None or f.kind != "reference":
                return None
            target = cat.target_of(f)
            src, tgt = str(getattr(f, "from_source", "") or ""), str(getattr(f, "from_target", "") or "")
            if not (src and tgt) and getattr(spec, "knowledge", None) is not None:
                rel = spec.knowledge.relationship_for_field(f.api) or {}
                ds = rel.get("direction_semantics") if isinstance(rel.get("direction_semantics"), dict) else {}
                src, tgt = src or str(ds.get("from_source") or ""), tgt or str(ds.get("from_target") or "")
            try:
                role = str(_record_base._role(f) or "")
            except Exception:  # noqa: BLE001 - a field without role texts
                role = ""
            return {"label": str(f.label or f.api), "target": target, "person": target in PERSON_OBJECTS,
                    "from_source": " ".join(src.split()), "from_target": " ".join(tgt.split()), "role": role}
        except Exception:  # noqa: BLE001 - optional
            return None

    def person_lookups(self, obj: str) -> list[dict]:
        """The catalog's lookups of ``obj`` to a person object that hold data (no system lookup, none blocked), each as
        :meth:`lookup_directions` gives it plus ``field`` and ``object_words`` (the words of the names of ``obj`` and
        of the target: they name the records, not how they relate), for the relation-word checks (evaluation fix 5,
        :func:`~sf_assistant.agent.nudges.relation_lookup_nudges`, :func:`~sf_assistant.agent.nudges.direction_nudges`).
        [] for an object the catalog does not have or tools without a catalog. Never raises."""
        cat = getattr(self.tools, "catalog", None)
        try:
            spec = cat.objects.get(str(obj)) if cat is not None else None
            if spec is None:
                return []
            out: list[dict] = []
            for target, fields in cat.person_lookups(spec).items():
                try:
                    words = sorted(cat._object_words(spec.api, target))
                except Exception:  # noqa: BLE001 - a catalog without object words: the label words only
                    words = sorted({w for t in (spec.api, target, cat.label_of(spec.api), cat.label_of(target))
                                    for w in re.findall(r"[a-z]+", re.sub(r"__c$", "", str(t)).replace("_", " ").lower())})
                for f in fields:
                    if getattr(f, "filled", 1) <= 0:
                        continue
                    info = self.lookup_directions(spec.api, f.api)
                    if info and info.get("person"):
                        out.append({"field": f.api, **info, "object_words": words})
            return out
        except Exception:  # noqa: BLE001 - optional
            return []

    def status_of(self, obj: str) -> dict | None:
        """The object's status field for the status line (Group B item 11, :func:`~sf_assistant.agent.nudges.
        with_status_line`): ``{"label", "field", "field_label", "values": [(value, label), ...]}`` (the tools' status field and its
        knowledge picklist; ``field`` None when the object has none), or None without a catalog. Never raises."""
        cat = getattr(self.tools, "catalog", None)
        fn = getattr(self.tools, "_status_field", None)
        try:
            spec = cat.objects.get(str(obj)) if cat is not None else None
            if spec is None:
                return None
            f = fn(spec) if callable(fn) else None
            return {"label": str(cat.label_of(spec.api) or spec.api), "field": f.api if f is not None else None,
                    "field_label": str(f.label or f.api) if f is not None else "",
                    "values": [(str(v), str(lab or v)) for v, lab, *_m in (f.picklist if f is not None else [])]}
        except Exception:  # noqa: BLE001 - optional
            return None

    def _question_people(self) -> list[dict]:
        """The tools' ``question_people()`` for the current question ([] when the tools have none; never raises)."""
        fn = getattr(self.tools, "question_people", None)
        try:
            res = fn() if callable(fn) else []
            return [p for p in res if isinstance(p, dict)] if isinstance(res, list) else []
        except Exception:  # noqa: BLE001 - optional
            return []

    def _question_objects(self) -> list[dict]:
        """The tools' ``question_objects()`` for the current question ([] when the tools have none; never raises)."""
        fn = getattr(self.tools, "question_objects", None)
        try:
            res = fn() if callable(fn) else []
            return [x for x in res if isinstance(x, dict)] if isinstance(res, list) else []
        except Exception:  # noqa: BLE001 - optional
            return []

    def _names_person(self, question: str) -> bool:
        """The tools' name index finds a stored person's full name in the question (never raises)."""
        fn = getattr(self.tools, "names_person", None)
        try:
            return bool(fn(question)) if callable(fn) else False
        except Exception:  # noqa: BLE001 - optional
            return False

    def ask(self, question: str, routed: Any = ROUTE_HERE, display: Any = None, *, seed: SeedRun | None = None,
            terms: Any = None, earlier: str = "", triage: bool = True,
            contact_ok: bool | None = None) -> RecordAnswer:
        """Answer ``question`` from records, or reply NO_RECORDS. Never raises.

        ``routed`` is the route call's result when the caller made it already (``ask.py`` routes first in merged mode
        to pick the knowledge step, RECORDS_SPEC §21.19): True, False or None (unclear), used as :meth:`route`'s
        result. The default :data:`ROUTE_HERE` makes the route call here, as before.

        People (RECORDS_SPEC §22): :meth:`triage` (the route call and the names call) decides; a question that names a
        stored person, or a name no stored person has, goes to the record agent whatever the route said, its hints
        show the confirmed people, a NO_RECORDS draft gets a pushback, and when the agent still gives no answer (NO_RECORDS,
        empty, an error, no successful call) code writes the record part from ``about_people`` (:func:`people_answer`,
        ``code_answer``). A forced answer (the tool budget ran out, so no check saw it) that names one of several
        people sharing an asked first name gets a line naming the others. When no data call succeeded but one failed because a name, field or object is
        not there, the reply is kept only as a "no such person / not recorded" answer (:func:`not_found_reply`:
        no count, not even 0; orientation calls such as describe_fields do not change this, and after a name was not
        found neither do a value list without a corrected name or a list whose name filter matched nothing,
        :func:`_data_found`); then ``no_records`` and ``used_data`` are False. A reply that fails it is dropped and
        kept in ``dropped_answer``, and ``not_found_answer`` holds the code-built answer for the names that were not
        found (:func:`not_found_answer`); so does a NO_RECORDS reply after a name was not found. The draft check runs on
        the first draft (:meth:`check_draft`) and, after a nudge, once more on the next one (:meth:`recheck_draft`).
        The final reply loses working sentences and tool talk (:func:`drop_tool_talk`), and the system lookups listed
        in the scope of an any_role count (:func:`fix_any_role_scope`); a kept answer then gets the no-data line
        (:func:`with_no_data_line`) and loses made-up overlap clauses (:func:`drop_overlap_clauses`), number-free
        caveats about lookups the question is not about (:func:`drop_record_side_caveats`) and the parts with numbers
        no result holds (:func:`drop_unsupported`, listed in ``dropped_numbers``), before the Source line is added.
        Business-term contracts (2026-11-24): a Person Account or record-type scope claim that no applied filter backs
        loses its clause (``agent.term_notes.drop_unapplied_scope_claims``, listed in ``dropped_claims``), and when
        code added the candidate filter a code line says so before the Source line (``with_term_line``).
        A reply with no text besides its Source line, as written or after these checks (:func:`answer_text`,
        evaluation fix 2), is no answer: ``no_records`` with ``error`` "empty record answer..." and the reply in
        ``dropped_answer``; code then writes the people answer (``code_answer``) for a question that names people, and
        also from the record agent's own about_people result when the triage confirmed nobody.

        Value spots (2026-10-28): the tools get the triage's values with the question (``begin_question(question,
        people=..., values=...)``; tools without the argument as before) for the hints on the records the question
        names, and a NO_RECORDS draft about such records gets a pushback (:meth:`check_draft`).

        Phase 1 (2026-10-26): the tools show contact details only when the question asks for them (``begin_question``);
        when it does not, the answer and ``dropped_answer`` lose every e-mail address and phone number
        (:func:`sf_assistant.engine.contact.drop_contact_details`) as the last step; a number the tool results hold as
        a whole value of a field (a Zoom meeting id, :func:`~sf_assistant.engine.contact.result_numbers`) stays.

        Rows by code (``SFA_TABLES=code``, read here once; latency plan step 3): the system prompt ends with
        ``agent.tables.RECORDS_TABLES_NOTE``; each draft is checked with its table lines replaced by the rows
        (``agent.tables.expand``) while the model's history keeps its own draft, the first check sends no
        working-notes or number message that only code's cells set off (:meth:`_own_words_messages`) and adds
        ``RECORDS_TABLES_MARKER_MESSAGE`` for lines code cannot fill when it sends a message anyway or nothing else
        of the draft is left; the final reply's table lines go on lines of their own before the cleaners
        (``agent.tables.own_lines``) and get their rows right after them. The blocks' text is an audit source, the
        contact rule runs on them too, and ``code_tables`` / ``table_blocks`` record them (``cut``: the table lines
        ``strip_reasoning`` removed). A block dropped because the model typed its rows comes back when a check cuts a line
        of that copy (``agent.tables.put_back``). ``model`` (the default): none of this runs.

        In every mode ``nudge_kinds`` holds the prompt id of each draft-check message, in order, read
        from the final text of each draft check's message (:func:`~sf_assistant.agent.nudges.nudge_kinds`; step-3 fix
        T5): a log counter, nothing reads it back.

        Charts (``SFA_CHARTS``, read at each call): with ``on``, a chart question's system prompt ends with
        ``agent.charts.RECORDS_CHART_NOTE``, after the note on the table lines. The tools and the user message stay as
        they are, and off the prompt is byte for byte as before.

        Display words (prerequisite P3): ``question`` is the search text (the pipeline took the display words out) and
        ``display`` its flags; the chart note and the chart overrule read the flags, and a table, detail, summary,
        breakdown or file flag appends ``agent.display.note`` after the chart note. None: as before.

        Records retry (prerequisite P2, see the module text): one forced round for a records-routed or chart-overruled
        question that is no people question, at the require-tools nudge (``tools_first`` of ``answer_question``) or
        at the first draft check; the answer carries ``no_data`` and ``tool_retry``.

        Salesforce conversation memory (design v2 §6.7, §7.1, §7.2; all five keyword arguments are unset for every
        other caller, and then nothing of this runs):

        - ``terms``: the terms the record tools enforce for this job (``begin_question(terms=...)``, rebuilt by code
          from the stored plan and the typed follow-up); None: today's terms of ``question``.
        - ``seed``: a :class:`SeedRun` of the stored or patched plan. It runs right after ``begin_question``, so its
          calls are validated, enforced with ``terms`` and logged like the record agent's own, and the agent starts
          from their results (``answer_question(seed=...)``). The seeds count as tool calls that ran: no require-tools
          nudge and no forced round on them (C35), and the draft check reads their evidence. A record set that cannot
          become a filter (:attr:`SeedRun.stop`) ends the answer there with no model call (``no_records``, ``error``
          "memory: <code>"); the caller writes :meth:`SeedRun.stop_text`.
        - ``earlier``: the last answer's text for a question about it (§6.7): the agent's user message ends with the
          note and the answer as data (:func:`earlier_block`).
        - ``triage`` False (a patch that only changes how the plan is shown): no triage call; the question goes to
          the record agent as a RECORDS route with no people and no values.
        - ``contact_ok``: whether the person's typed question asks for contact details, read by the caller
          (stage-2 review fix PE1): the tools' contact flag (``begin_question(contact_ok=...)``) and the last drop of
          e-mail addresses and phone numbers use it instead of reading ``question``, which for a patch or a shortcut
          is the plan line (code's words around the typed values of earlier questions). None: read from ``question``.
        """
        t0 = time.perf_counter()
        evidence: list[QueryEvidence] = []
        r: AgentResult | None = None
        try:
            with self._lock:
                self._n += 1
                n = self._n
            if seed is not None:
                routed = True   # a stored plan runs: the route is records (no route call), whatever a route said
            tri = (self._triage_for(question, routed, display) if triage
                   else Triage(routed=True, route=True))   # a display-only patch: no triage call (§7.2)
            routed = tri.routed
            contact_q = asks_for_contact(question) if contact_ok is None else bool(contact_ok)
            if terms is not None or contact_ok is not None:   # conversation memory: never a fallback without them
                mem = {k: v for k, v in (("terms", terms), ("contact_ok", contact_ok)) if v is not None}
                self.tools.begin_question(question, people=tri.hinted, values=tri.values, **mem)
            else:
                try:  # C1: clears evidence and caches; the question gives the role cues, the triage its people and values
                    self.tools.begin_question(question, people=tri.hinted, values=tri.values)
                except TypeError:   # tools without the values argument (value spots, 2026-10-28), or without people
                    try:
                        self.tools.begin_question(question, people=tri.hinted)
                    except TypeError:   # tools without the people argument
                        self.tools.begin_question(question)
            person_q = bool(tri.people or tri.missing)
            asked = tri.asked()
            if routed is False:
                return RecordAnswer(question=question, answer="", no_records=True, used_data=False, routed_out=True,
                                    seconds=round(time.perf_counter() - t0, 3), triage=tri.as_dict())
            seeded: list = []
            if seed is not None:   # the stored plan's calls, before the record agent (conversation memory, §7.1)
                t_seed = time.perf_counter()
                seeded = seed.run(self.tools)
                timeline.add("stage", t_seed, stage="seed", calls=len(seeded))
                if seed.stop:   # a record set that cannot become a filter: the caller asks (stop_text), no model call
                    return RecordAnswer(question=question, answer="", no_records=True, used_data=False,
                                        evidence=self.tools.evidence, error=f"memory: {seed.stop}",
                                        seconds=round(time.perf_counter() - t0, 3), triage=tri.as_dict())
            t_hints = time.perf_counter()
            hints = self.tools.object_hints(question, missing=tri.missing) if tri.missing else \
                self.tools.object_hints(question)
            timeline.add("stage", t_hints, stage="hints", chars=len(hints or ""))   # the hints' size only, never them
            user_text = question + ("\n\n" + hints if hints else "")
            if earlier:   # a question about the last answer (§6.7): its text as data, after the hints
                block = earlier_block(earlier)
                user_text += ("\n\n" + block) if block else ""
            q = Question(qid=f"rec-{n:03d}", category="cross_section", question=user_text, expected_facts=[],
                         expected_tools=[], answerable=True, source="records", gold_chunk_ids=[])
            status = self.tools.status()
            tables_on = tables_settings().by == "code"   # latency plan step 3 (SFA_TABLES): code writes the result rows
            labels = _tables.column_labels(getattr(self.tools, "catalog", None)) if tables_on else None
            nudged = [0]
            nudges: list[str] = []
            kinds: list[str] = []
            drafts = [0]

            # prerequisite P2: one forced round for a records-routed or chart-overruled question that names nobody
            chart_q = _chart_question(question, display)
            budget = [1 if (tri.route is True or tri.overruled_by == "chart") and not person_q else 0]
            retry: dict = {}
            fallbacks = getattr(self.llm, "forced_fallbacks", 0)

            def forced(at: str, why: str, message: str) -> tuple[str, str]:
                budget[0] = 0
                retry.update({"at": at, "why": why, "choice": "auto_off" if _forcing_off(self.llm) else "required"})
                nudges.append(message)
                kinds.extend(nudge_kinds(message))
                return message, "required"

            def tools_first(draft: str) -> tuple[str, str | None]:
                """The require-tools nudge (round 1 called no tool): forced when the budget is left, the draft is no
                NO_RECORDS reply, or it is one after a RECORDS route on a question that asks for no chart."""
                no_rec = _is_no_records(strip_reasoning(draft))
                if budget[0] and (not no_rec or (tri.route is True and not chart_q)):
                    return forced("tools_first", "no_records" if no_rec else "no_call", RECORDS_TOOLS_FORCED_MESSAGE)
                return require_msg, None

            def check(draft: str) -> Any:
                if drafts[0] >= 2:   # a third draft is not checked, as before (the forced round counts no draft)
                    return None
                drafts[0] += 1   # the full check on the first draft; the second only for a 0 still leading unfit
                t_check, msg, choice = time.perf_counter(), None, None
                name = "draft" if drafts[0] == 1 else "redraft"
                try:
                    x = _tables.expand(strip_reasoning(draft), self.tools.evidence, labels) if tables_on else None
                    seen = x.text if x is not None else draft   # what the user would see: rows in place, no table line
                    no_rec = _is_no_records(strip_reasoning(seen))
                    if drafts[0] == 1 and budget[0] and not person_q:   # the one forced round, while it is left
                        why = retry_kind("", self.tools.evidence)
                        if why is not None and no_rec and not (tri.route is True and not chart_q):
                            why = None   # a NO_RECORDS draft passes as before (route not RECORDS, or a chart question)
                        if why is None and not no_rec and self.tools.evidence and not _data_call_ok(self.tools.evidence) \
                                and not _coded_failure(self.tools.evidence) and _new_numbers(seen, question):
                            why = "no_data_call"   # numbers after describe_fields or the sync status only (E-15)
                        if why is not None:
                            # review fix E-16: the forced round is no draft, so the draft after it gets the full
                            # check (check_draft, with the number audit), not the recheck
                            drafts[0] -= 1
                            message = {"all_failed": RECORDS_ALL_FAILED_MESSAGE,
                                       "no_data_call": RECORDS_RUN_QUERY_MESSAGE}.get(why, RECORDS_TOOLS_FORCED_MESSAGE)
                            msg, choice = forced("check", "no_records" if why == "no_call" and no_rec else why, message)
                            return msg, choice
                    # review fix E-17: once the forced round is spent, every draft gets the checks of before (no second
                    # retry message)
                    msg = (self.check_draft(seen, question, status, routed, tri) if drafts[0] == 1
                           else self.recheck_draft(seen))
                    if x is not None and drafts[0] == 1 and x.blocks and msg:   # code's cells are not the model's words
                        msg = self._own_words_messages(msg, seen, x, question, status)
                    if x is not None and drafts[0] == 1 and x.bad and (msg or not answer_text(x.text)):
                        # joined to another message: no extra round; alone when nothing else is left: the rescue
                        msg = "\n\n".join(m for m in (msg, _tables.marker_message(x.bad)) if m)
                        nudged[0] = 1
                finally:
                    timeline.add("check", t_check, name=name, nudge=bool(msg))
                if msg and choice is None:
                    nudges.append(msg)
                    kinds.extend(nudge_kinds(msg))
                return msg

            require_msg = (RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE.replace("<<PEOPLE>>", _quoted(asked)) if person_q
                           else RECORDS_REQUIRE_TOOLS_MESSAGE)
            # code mode: the note on the table lines is appended (never prepended, never inside the shared template);
            # so is the note of a chart question
            system = (render_records_system(status) + (_tables.RECORDS_TABLES_NOTE if tables_on else "")
                      + _chart_note(question, display) + _display.note(display))
            r = answer_question(q, _TextToolCalls(self.llm), self.tools, self.cfg, system=system,
                                require_tools=True, require_tools_message=require_msg,
                                force_answer_message=RECORDS_FORCE_ANSWER_MESSAGE, check_answer=check,
                                # one more check for the forced round, which is no draft (check stops at 2 drafts)
                                max_checks=3 if budget[0] else 2,
                                **({"tools_first": tools_first} if budget[0] else {}),
                                **({"seed": seeded} if seeded else {}))
            if retry and retry.get("choice") == "required" and getattr(self.llm, "forced_fallbacks", 0) > fallbacks:
                retry["choice"] = "auto_fallback"   # the server did not take the forced call: it went out as "auto"
            t_post = time.perf_counter()   # the code after the record agent: cleaners, audits, the people answer
            evidence = self.tools.evidence
            ok_calls = [e for e in evidence if e.ok]
            people_data = tri.people_data()   # only names that are not stored: namesakes count nothing
            used_data = any(e.kind == "data" and e.tool in DATA_TOOLS for e in ok_calls)
            raw = strip_reasoning(r.answer)
            cut = 0
            if tables_on:   # each table line on a line of its own: a cleaner that drops its sentence keeps it
                cut = max(0, _tables.line_count(r.answer) - _tables.line_count(raw))
                raw = _tables.own_lines(raw)
            reply = fix_any_role_scope(drop_ref_codes(drop_tool_talk(drop_working_sentences(raw)),
                                                      reference_codes(evidence)), evidence)
            xt = _tables.expand(reply, evidence, labels) if tables_on else None
            if xt is not None:   # each table line becomes its result's rows (after the cleaners, which rewrite cells)
                reply = xt.text
            error = r.error
            dropped = found_none = ""
            empty = False   # the reply said nothing but its Source line, before or after the checks (fix 2)
            no_data = None   # prerequisite P2: "no_call" / "all_failed" when no record call gave data
            if retry.get("why") == "no_records" and r.error is None and not _data_call_ok(evidence) \
                    and not _is_no_records(reply):
                # review fix E-19: the forced round of a NO_RECORDS draft read no record data, so the draft's NO_RECORDS
                # stands (a design question the route call read as RECORDS keeps its knowledge answer, as before)
                dropped, reply = reply, NO_RECORDS
            if _is_no_records(reply):
                no_records, answer = True, ""
                if _name_missing(evidence) and _no_data_but_not_found(evidence, people_data):
                    # a name the calls filtered on is not stored: code's not-found answer, not the knowledge answer
                    found_none = not_found_answer(evidence)
                    found_none = with_source_line(found_none, evidence, status) if found_none else ""
                    if found_none:
                        error = error or "NO_RECORDS reply after a name was not found"
            elif _no_data_but_not_found(evidence, people_data):   # a successful describe_fields makes no count acceptable
                if (_name_missing(evidence) and not_found_answer(evidence)) or (not people_data and person_q):
                    # a name no record has, and nothing else was found: code's not-found answer (the names and the
                    # closest stored names the errors list), never the reply, which can name "closest" people that no
                    # result listed. Only names that are not stored and no failed call yet: code asks about_people
                    # below, whose not-found error gives that answer.
                    no_records, answer = True, ""
                    error = error or ("a name was not found; code wrote the not-found answer"
                                      if not_found_reply(reply, evidence, question, status)
                                      else "no successful record query")   # the reply gave a count (0 included)
                    dropped = reply
                    found_none = not_found_answer(evidence) if _name_missing(evidence) else ""
                    found_none = with_source_line(found_none, evidence, status) if found_none else ""
                elif not_found_reply(reply, evidence, question, status):
                    no_records, answer = False, reply  # e.g. "no field ...": no count may appear
                else:
                    no_records, answer = True, ""
                    error = error or "no successful record query"
                    dropped = reply   # kept for the log: a count (0 included) after a not-found error
                    found_none = not_found_answer(evidence)
                    found_none = with_source_line(found_none, evidence, status) if found_none else ""
            elif retry.get("why") == "no_data_call" and r.error is None and not _data_call_ok(evidence) \
                    and _new_numbers(reply, question):
                # review fix E-15: numbers again after the forced round, still with no data call behind them (only
                # describe_fields or the sync status): no answer, the clarifying question instead
                no_records, answer, dropped, no_data = True, "", reply, "no_call"
                error = error or "no successful record query"
            elif not ok_calls:
                no_records, answer = True, ""
                if r.error is None and not person_q:   # a model fault stays a failure (the caller's fallback runs)
                    no_data = retry_kind("", evidence)
                if error is None:
                    error = ("no successful record query" if r.tool_calls
                             else "the record agent answered without calling a record tool")
            elif not answer_text(reply):   # empty, or nothing but a Source line (evaluation fix 2): no answer
                no_records, answer, empty, dropped = True, "", True, reply
                error = error or "empty record answer"
            else:
                no_records, answer = False, reply
            removed: list[str] = []
            claims: list[str] = []   # scope claims no applied filter backs (business-term contracts, 2026-11-24)
            if answer:
                # the code rows' own text is a source too: a stored line break reads as a space in a cell, as "n15" in
                # the facts (code mode only; otherwise the list is as before)
                sources = ([facts_json(evidence, 10**9), question, status.line()]
                           + (xt.sources if xt is not None else []))
                answer = drop_record_side_caveats(drop_overlap_clauses(with_no_data_line(answer, evidence), evidence),
                                                  evidence, question)
                answer, removed = drop_unsupported(answer, sources)
                if xt is not None:   # these checks cut a line of a copy the model typed: code's rows go back
                    answer = _tables.put_back(answer, xt)
                # the business-term contracts (2026-11-24): a Person Account / record-type claim no query applied goes
                answer, claims = drop_unapplied_scope_claims(answer, evidence, question)
                if not answer_text(answer):   # the checks left nothing but the Source line: no answer (fix 2)
                    no_records, answer, empty, dropped = True, "", True, dropped or reply
                    error = error or "empty record answer: the checks left only its Source line"
                else:
                    if drafts[0] == 0 and person_q:   # a forced answer: no check saw it
                        answer = self._other_people_line(answer, tri)
                    answer = with_term_line(answer, evidence)   # the candidate filter code added, said by code
                    answer = with_source_line(answer, evidence, status)
                    if xt is not None and xt.blocks:   # a blank line between the last rows and the added Source line
                        answer = _tables.gap_before_source(answer, xt.blocks)
            code_answer, agent_error = False, None
            # an empty answer after the record agent's own about_people call (no confirmed person): the people answer
            own = agent_people_result(evidence) if empty and not person_q else None
            if (person_q or own is not None) and no_records and not found_none:
                # The record agent gave no answer about a stored (or missing) person: code writes it from about_people.
                res = self._people_result(asked) if person_q else own
                # without the internal P/R codes, as the agent's own reply (step-2 fixes of 2026-10-30, F1)
                text = drop_ref_codes(people_answer(res), reference_codes(self.tools.evidence)) if res is not None else ""
                evidence = self.tools.evidence
                if text:
                    answer = with_source_line(text, evidence, status)
                    agent_error = error or "NO_RECORDS reply"
                    no_records, used_data, code_answer, error = False, True, True, None
                elif _name_missing(evidence) and _no_data_but_not_found(evidence, people_data) and \
                        not_found_answer(evidence):
                    # the people tool read the records and found no one by these names: code's not-found answer
                    found_none = with_source_line(not_found_answer(evidence), evidence, status)
                    agent_error = error or "NO_RECORDS reply"
                    error = error if dropped else "NO_RECORDS reply after a name was not found"
                else:   # the people tool failed too: say so (ask.py puts a line before the knowledge answer)
                    failed = [e for e in evidence if e.tool in PERSON_TOOLS and not e.ok]
                    error = error or ("about_people failed: " + " ".join(str(failed[-1].error or "").split())[:200]
                                      if failed else "no record data about the named people")
            blocks = dict(xt.blocks) if xt is not None else {}   # code mode: the code rows, as the answer holds them
            if not contact_q:   # phase 1: no e-mail address or phone number that was not asked for
                keep = result_numbers(getattr(e, "model_result", None) for e in evidence if getattr(e, "ok", False))
                answer, dropped = drop_contact_details(answer, keep)[0], drop_contact_details(dropped, keep)[0]
                blocks = {k: drop_contact_details(b, keep)[0] for k, b in blocks.items()}   # equal to their text in it
            unsupported = audit_numbers(answer, [facts_json(evidence, 10**9), question, status.line()]
                                        + (xt.sources if xt is not None else [])) if answer else []
            raw_kept = str(r.answer or "") if empty else ""   # an empty answer's raw text, for the private log only
            if raw_kept and not contact_q:
                raw_kept = drop_contact_details(raw_kept, result_numbers(
                    getattr(e, "model_result", None) for e in evidence if getattr(e, "ok", False)))[0]
            timeline.add("stage", t_post, stage="post")
            return RecordAnswer(question=question, answer=answer, no_records=no_records, used_data=used_data,
                                evidence=evidence, agent=r, error=error, unsupported_numbers=unsupported,
                                seconds=round(time.perf_counter() - t0, 3), nudges=nudges, dropped_answer=dropped,
                                not_found_answer=found_none, dropped_numbers=removed, dropped_claims=claims,
                                code_answer=code_answer,
                                agent_error=agent_error, named_people=list(asked), triage=tri.as_dict(),
                                named_spellings=named_spellings(tri.people),
                                code_tables=_tables.answer_counts(xt, answer, blocks, nudged[0], cut) if xt is not None
                                else {},
                                table_blocks={k: b for k, b in blocks.items() if _tables.holds(answer, b)},
                                nudge_kinds=kinds, dropped_raw=raw_kept[:4000], no_data=no_data,
                                tool_retry=dict(retry) if retry else None)
        except Exception as exc:  # noqa: BLE001 - the caller always gets an answer object
            try:
                evidence = evidence or self.tools.evidence
            except Exception:  # noqa: BLE001
                evidence = []
            return RecordAnswer(question=question, answer="", no_records=True, used_data=False, evidence=evidence,
                                agent=r, error=f"{type(exc).__name__}: {exc}", seconds=round(time.perf_counter() - t0, 3))
