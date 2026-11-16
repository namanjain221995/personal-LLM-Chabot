"""The lean-mode merge by code (latency plan step 2, the owner's go of 2026-10-29: "at most 3 relevant meaning
bullets"): ``agent.merge.code_merge``, the ``SFA_MERGE`` switch (``config.merge_settings``), its use in
``pipeline.answer``, the JSONL keys ``merge_by`` / ``merge_switch`` and the cli's status line.

Its review of the same day: relevance by (object, field) pair (each source on its own), the first object bullet and the
used fields before the cap, no bullet about the question's people or about the knowledge, the question, the tools or
the prompt, no cut-off or overlong bullet, the not-found count rule, a label line after a list (and the page's render
of it), the merge call for a record answer that still plans aloud, and the lookup footer, the status lines and the
person lead read without the added bullets.

The step-2 fixes of 2026-10-30 (the code-merge evaluation of 2026-10-29, docs/runbook.md; last section): bullets that
contradict a fake record catalog or the answer's own results, an off-topic subject, the last-line cut-off rule, the cap
order, the counted line, Hinglish record answers and the wider merge-call routing (F2-F8, F10, F11, F5); the gap check
of the same day: a row of an unlinked object (F2 d) and an FLS claim the knowledge does not make (F10f).

Offline: a scripted fake OpenAI client behind the real :class:`LLM` (as tests/test_timeline.py), stub record tools
(as tests/test_timeline.py and tests/test_value_route.py) and the knowledge fixture; no warehouse, no model. Fake
data only: the department, the people, their counts and the field meanings are made up. Every test that goes through
``pipeline.answer`` sets ``SFA_MERGE`` itself, so an exported value never changes it.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import sf_assistant.pipeline as pipeline
from sf_assistant import cli
from sf_assistant import config as C
from sf_assistant.agent import merge
from sf_assistant.agent import record_prompts as rprompts
from sf_assistant.agent import record_session
from sf_assistant.agent import timeline
from sf_assistant.agent.checks import audit_numbers, facts_json
from sf_assistant.agent.knowledge_agent import QuestionSpelling
from sf_assistant.agent.llm import LLM
from sf_assistant.agent.merge import CODE_MERGE_MAX_BULLETS, code_merge
from sf_assistant.agent.nudges import with_lookup_footer
from sf_assistant.agent.record_session import RecordSession
from sf_assistant.agent.results import RecordAnswer
from sf_assistant.config import load_config
from sf_assistant.engine.catalog import FieldSpec
from sf_assistant.engine.types import QueryEvidence, RecordsStatus, Statement
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.tools.knowledge_tools import Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
QUESTION = "how many employees are in team zorvex?"
STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."
LEAD = "6 employees (`Recruiter__c`) are in the department Zorvex (`Department__c`)."
RECORD_ANSWER = LEAD + "\n\n" + SOURCE
# a lean facts block: what the objects and fields of the count mean, and one bullet about a field it did not use
B_OBJECT = "- `Recruiter__c` holds the employees (staff), one record per person."
B_FIELD = "- `Department__c` is the employee's department, a lookup to `Department__c` records."
B_OTHER = "- `Interview_Status__c` is a legacy picklist; `Status__c` replaced it."
B_USED_LATER = "- `Recruiter__c.Department__c` groups employees by team."
FACTS = "\n".join([B_OBJECT, B_OTHER, B_FIELD])
MERGED_BY_MODEL = "6 employees are in Zorvex (`Recruiter__c.Department__c`).\n" + SOURCE


def _cfg(steps: int = 6):
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=steps)


@pytest.fixture(autouse=True)
def _closed_timeline():
    timeline.end()   # no timeline left open by another test
    yield
    timeline.end()


# ------------------------------------------------------------------------------------------------ fake data
def _result(**extra) -> dict:
    return {"query_id": "q1", "object": "Recruiter__c", "count": 6, "counted": "Recruiter__c records",
            "applied": {"filters": ["Department__c = 'Zorvex'"]},
            "source": "preprod sandbox copy, synced 2026-10-13 14:12 UTC", **extra}


def _evidence(qid: str = "q1", args: dict | None = None, **extra) -> QueryEvidence:
    result = _result(**extra)
    st = Statement(sql='SELECT COUNT(*) AS n FROM main."Recruiter__c" AS t0 WHERE t0."Department__c" = ?',
                   params=["Zorvex"], columns=["n"], rows=[[6]], row_count=1, truncated=False, elapsed_ms=4.0)
    return QueryEvidence(query_id=qid, tool="count_records", kind="data", object="Recruiter__c",
                         arguments=dict(args or {}), ok=True, error=None, applied=result["applied"], notes=[],
                         statements=[st], tables=["Recruiter__c"], synced_at={"Recruiter__c": "2026-10-13 14:12 UTC"},
                         summary="6", model_result=result, elapsed_ms=5.0,
                         fields_used=[("Recruiter__c", "Department__c")])


def _rec(answer: str = RECORD_ANSWER, **kw) -> RecordAnswer:
    kw.setdefault("evidence", [_evidence()])
    return RecordAnswer(question=QUESTION, answer=answer, no_records=kw.pop("no_records", False), used_data=True, **kw)


# ------------------------------------------------------------------------------------------------ code_merge itself
def test_code_merge_adds_the_relevant_bullets_before_the_source_line():
    m = code_merge(QUESTION, FACTS, _rec(), STATUS)
    assert m.merged and m.error is None and m.rejected_numbers == [] and m.seconds >= 0
    # the record answer as it is, a blank line, the bullets as the lean step wrote them (in order), the Source line last
    assert m.text == LEAD + "\n\n" + B_OBJECT + "\n" + B_FIELD + "\n\n" + SOURCE
    # without a blank line before the record answer's Source line, none is added there
    m = code_merge(QUESTION, FACTS, _rec(LEAD + "\n" + SOURCE), STATUS)
    assert m.text == LEAD + "\n\n" + B_OBJECT + "\n" + B_FIELD + "\n" + SOURCE


def test_a_bullet_about_a_field_the_answer_did_not_use_is_left_out_and_at_most_three_are_added(monkeypatch):
    # B_OTHER names fields no query used and the record answer does not name; the first bullet that names only an
    # object goes first, then the used fields (each source of relevance on its own: the relevance tests below)
    facts = "\n".join([B_OTHER, B_OBJECT, B_FIELD, B_USED_LATER, "- `Department__c` names hold no spaces."])
    m = code_merge(QUESTION, facts, _rec(), STATUS)
    assert m.merged and m.text == LEAD + "\n\n" + "\n".join([B_OBJECT, B_FIELD, B_USED_LATER]) + "\n\n" + SOURCE
    assert CODE_MERGE_MAX_BULLETS == 3
    monkeypatch.setattr(merge, "CODE_MERGE_MAX_BULLETS", 1)   # read at call time
    assert code_merge(QUESTION, facts, _rec(), STATUS).text == LEAD + "\n\n" + B_OBJECT + "\n\n" + SOURCE
    monkeypatch.setattr(merge, "CODE_MERGE_MAX_BULLETS", 3)   # (review of 2026-10-29: the cap must not decide below)
    # a field the answer names in prose only (no query used it) counts; a bullet without backticks names nothing
    rec = _rec(LEAD.replace(".", "; `Team_Lead__c` is blank.") + "\n" + SOURCE, evidence=[_evidence()])
    lead_bullet = "- `Team_Lead__c` is the employee's team lead."
    m = code_merge(QUESTION, lead_bullet + "\n- Recruiter__c holds the employees.", rec, STATUS)
    assert m.merged and m.text.split("\n")[2:] == [lead_bullet, SOURCE]


def test_a_bullet_with_a_number_the_audit_would_flag_is_left_out():
    """The number audit of the merge call for a lean facts block (knowledge_numbers=False): the record facts, the
    quoted values and API names of the facts block, the question and the status line; no number from its prose.
    Review of 2026-10-29: each source on its own (47 is only in a quoted value, 2019 only in the question, 60 only in
    the status line; the record facts hold 6, 10, 12, 13, 14 and 2026)."""
    counted = "- `Recruiter__c` holds the employees; 48 departments have some."      # 48: no source holds it
    quoted = "- `Department__c` values such as \"Team 47\" name a team."            # 47: a quoted value
    small = "- `Recruiter__c` has 2 record types."                                   # 0-12 always pass
    m = code_merge(QUESTION, "\n".join([counted, quoted, small]), _rec(), STATUS)
    assert m.merged and m.text == LEAD + "\n\n" + quoted + "\n" + small + "\n\n" + SOURCE
    assert "48" not in m.text and m.rejected_numbers == []
    m = code_merge(QUESTION, counted, _rec(), STATUS)   # nothing left: the record answer, not merged, no error
    assert (m.text, m.merged, m.error, m.rejected_numbers) == (RECORD_ANSWER, False, None, [])
    # each source alone keeps its bullet, and without it the bullet goes
    unquoted = quoted.replace('"Team 47"', "Team 47")
    assert quoted in code_merge(QUESTION, quoted, _rec(), STATUS).text
    assert code_merge(QUESTION, unquoted, _rec(), STATUS).text == RECORD_ANSWER
    since = "- `Recruiter__c` holds the employees hired since 2019."
    asked = QUESTION.replace("?", " hired since 2019?")
    assert since in code_merge(asked, since, _rec(), STATUS).text
    assert code_merge(QUESTION, since, _rec(), STATUS).text == RECORD_ANSWER
    listed = "- `Recruiter__c` is one of the 60 objects of the copy."
    assert "60 objects" in STATUS.line() and listed in code_merge(QUESTION, listed, _rec(), STATUS).text
    assert code_merge(QUESTION, listed, _rec(), None).text == RECORD_ANSWER   # no status: no status line among them


def test_working_notes_tool_talk_labels_repeats_and_scope_issues_are_left_out():
    left_out = [
        "- Let me check `Recruiter__c` once more.",                                  # working notes
        "- The `count_records` result shows `Recruiter__c` holds the employees.",    # tool talk
        "- You would need to run a query on `Recruiter__c` to count them.",          # how to fetch records
        "- `Recruiter__c` fields:",                                                  # a label only
        "- `Department__c` holds only the employees with `Status__c` = 'Active'.",   # a condition no query applied
        "- `Recruiter__c` is not available in the warehouse.",                       # contradicts the query
        "- 6 employees (`Recruiter__c`) are in the department Zorvex (`Department__c`).",   # the record answer's own
        "  - `Recruiter__c` holds the employees.",                                   # a sub-bullet is no bullet
        "* `Recruiter__c` holds the employees.",                                     # nor another marker
        # review of 2026-10-29: each of these only one filter catches
        "- The RECORD FACTS show that `Recruiter__c` holds the employees.",          # working_notes (merge input)
        "- I will note that `Recruiter__c` holds the employees.",                    # working_notes (a plan)
        "- Perfect! `Recruiter__c` holds the employees.",                            # drop_working_sentences
        "- Wait, `Department__c` is the employee's department.",                     # drop_working_sentences
    ]
    m = code_merge(QUESTION, "\n".join(left_out), _rec(), STATUS)
    assert (m.text, m.merged, m.error) == (RECORD_ANSWER, False, None)
    for bullet in left_out:   # each one alone, so that no other bullet's check decides
        assert code_merge(QUESTION, bullet, _rec(), STATUS).text == RECORD_ANSWER, bullet
    m = code_merge(QUESTION, "\n".join(left_out + [B_FIELD, B_FIELD]), _rec(), STATUS)   # a repeated bullet: once
    assert m.merged and m.text == LEAD + "\n\n" + B_FIELD + "\n\n" + SOURCE


def test_the_early_returns_are_the_merge_calls():
    m = code_merge(QUESTION, FACTS, _rec("", no_records=True, evidence=[]), STATUS)
    assert (m.text, m.merged, m.error) == ("", False, None)
    m = code_merge(QUESTION, FACTS, _rec(SOURCE), STATUS)   # nothing but a Source line (evaluation fix 2)
    assert (m.text, m.merged, m.error) == ("", False, "the record answer is empty")
    for empty in ("", "  \n"):
        m = code_merge(QUESTION, empty, _rec(), STATUS)
        assert (m.text, m.merged, m.error) == (RECORD_ANSWER, False, "the knowledge agent gave no answer")
    m = code_merge(QUESTION, "You would need to run a SOQL query.", _rec(), STATUS)
    assert (m.text, m.merged, m.error) == (RECORD_ANSWER, False,
                                           "the knowledge answer only said how to fetch records")


def test_the_whole_text_must_pass_the_number_audit_and_a_missing_source_line_is_added():
    # a record answer with a number no source holds (it passed no audit here): the record answer, rejected
    rec = _rec("77 employees (`Recruiter__c`) are in Zorvex.\n" + SOURCE)
    m = code_merge(QUESTION, B_OBJECT, rec, STATUS)
    assert (m.text, m.merged, m.rejected_numbers) == (rec.answer, False, ["77"])
    # a record answer without a Source line: the bullets end it, then the Source line of the query's object
    m = code_merge(QUESTION, B_OBJECT, _rec(LEAD), STATUS)
    assert m.merged and m.text == LEAD + "\n\n" + B_OBJECT + "\n" + SOURCE


def test_code_merge_never_raises(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("scope check down")

    monkeypatch.setattr(merge, "merge_scope_issues", broken)
    m = code_merge(QUESTION, FACTS, _rec(), STATUS)
    assert (m.text, m.merged, m.error) == (RECORD_ANSWER, False, "RuntimeError: scope check down")
    monkeypatch.undo()
    m = code_merge(QUESTION, FACTS, _rec(), None)   # no status: no status line among the sources, nothing else changes
    assert m.merged and m.text == code_merge(QUESTION, FACTS, _rec(), STATUS).text


# ------------------------------------------------------------ relevance by (object, field) pair (review of 2026-10-29)
PLAIN = "6 employees are in the department Zorvex.\n\n" + SOURCE   # names no API name: only the queries decide
B_DEPT = "- `Department__c` is the employee's department."
B_REC = "- `Recruiter__c` holds the employees."
B_INT = "- `Interview__c` holds client interviews."


def _query(qid: str = "q1", tool: str = "count_records", obj: str | None = "Recruiter__c", tables=(), fields=(),
           applied: dict | None = None, ok: bool = True, kind: str = "data",
           result: dict | None = None) -> QueryEvidence:
    """A record query of fake data with only what the test gives it (no table, field or applied block by default)."""
    res = {"query_id": qid, "object": obj, "count": 6, "source": "preprod sandbox copy, synced 2026-10-13 14:12 UTC",
           **(result or {})}
    return QueryEvidence(query_id=qid, tool=tool, kind=kind, object=obj, arguments={}, ok=ok,
                         error=None if ok else "E1: a fake failure", applied=dict(applied or {}), notes=[],
                         statements=[], tables=list(tables), synced_at={}, summary="6",
                         model_result=res if ok else None, elapsed_ms=1.0, fields_used=list(fields))


def _added(facts: str, *evidence: QueryEvidence, answer: str = PLAIN, question: str = QUESTION, rec: dict | None = None,
           **kw) -> list[str]:
    """The lines code_merge adds to ``answer`` for ``evidence`` ([] when it merges nothing); ``rec``: more fields of
    the RecordAnswer, ``kw``: code_merge's own (availability, objects). A merge that failed (its error: code_merge
    caught an exception and kept no bullet) fails the test, so a drop is never a crash (review of 2026-10-30)."""
    m = code_merge(question, facts, _rec(answer, evidence=list(evidence), **(rec or {})), STATUS, **kw)
    assert m.error is None, m.error
    return merge.code_merge_lines(m.text, answer) if m.merged else []


def test_each_query_source_makes_a_bullet_relevant_on_its_own():
    """tests-1 of the review: the record answer names no API name, so each source of ``merge._used_api`` decides."""
    assert _added(B_DEPT, _query(fields=[("Recruiter__c", "Department__c")])) == [B_DEPT]           # fields_used
    assert _added(B_DEPT, _query(applied={"filters": ["Department__c = 'Zorvex'"]})) == [B_DEPT]    # "applied"
    lc = {"object": "Recruiter__c", "field": "Department__c", "target": "Department__c", "records": 6,
          "filter": "Department__c.Name = 'Zorvex'", "person_kind": "other"}
    assert _added(B_DEPT, _query(result={"lookup_check": [lc]})) == [B_DEPT]                        # a lookup check
    assert _added(B_DEPT, _query()) == []                                                           # none of them
    assert _added(B_REC, _query()) == [B_REC]                                                       # the counted object


def test_a_failed_or_orientation_query_and_a_joined_table_make_no_bullet_relevant():
    count = _query("q3")
    assert _added(B_INT, _query("q1", obj="Interview__c", ok=False), count) == []                  # a failed query
    assert _added(B_INT, _query("q2", tool="describe_fields", kind="orientation", obj="Interview__c"), count) == []
    took = "- `Interviewer__c` is the employee who took the interview."
    assert _added(took, _query("q2", tool="describe_fields", kind="orientation", obj="Interview__c",
                               fields=[("Interview__c", "Interviewer__c")]), count) == []
    assert _added(B_INT, _query(obj="Interview__c")) == [B_INT]                                     # the counted object
    # a table the count only joined (review: an object bullet only for the counted object, never for a read list)
    assert _added(B_INT, _query(tables=["Recruiter__c", "Interview__c"])) == []


def test_the_names_of_the_record_answer_make_a_bullet_relevant():
    count = _query()   # no field, applied block or table of its own
    for answer in ("6 employees are in the department Zorvex (`Department__c`).",   # in backticks
                   "6 employees are in Department__c Zorvex.",                     # a custom name as it stands
                   "6 employees (`Recruiter__c.Department__c` Zorvex)."):          # an <Obj>.<Field> name
        assert _added(B_DEPT, count, answer=answer + "\n\n" + SOURCE) == [B_DEPT], answer
    owner = "- `OwnerId` is the user who owns the employee record."
    assert _added(owner, count, answer="6 employees are owned (OwnerId) by Zorvex.\n\n" + SOURCE) == [owner]
    assert _added(owner, count) == []
    # an object the record answer names: an object bullet about it fits
    assert _added(B_INT, count, answer="6 employees took client interviews (`Interview__c`).\n\n" + SOURCE) == [B_INT]
    full = "- `Name` is the employee's full name."   # a standard field: a field only when the answer names it
    assert _added(full, count, answer="6 employees are in Zorvex, by `Name`.\n\n" + SOURCE) == [full]
    assert _added(full, count) == []
    # the record answer puts the lookup named after its object on the counted object: that pair is used
    interviews = _query(obj="Interview__c", tables=["Interview__c", "Recruiter__c"])
    owner = "- `Recruiter__c` on `Interview__c` is the recruiter who owns the interview."
    placed = "6 interviews are owned through the `Recruiter__c` field on `Interview__c`.\n\n" + SOURCE
    assert _added(owner, interviews, answer=placed) == [owner] and _added(owner, interviews) == []


def test_a_bullet_about_a_field_the_count_did_not_use_is_left_out_also_with_the_counted_object():
    """The owner's rule ("a bullet about a field the count did not use is left out"), behaviour-3 and quality-2 of the
    review: a field placed on an object (`Obj.Field`, "`Field` on `Obj`", "`Obj`'s `Field`") counts as that pair only,
    and naming the counted object makes no bullet about another field relevant."""
    for bullet in ("- `Recruiter__c.Team_Lead__c` is the employee's team lead.",
                   "- `Team_Lead__c` on `Recruiter__c` is the employee's team lead.",
                   "- `Recruiter__c`'s `Team_Lead__c` is the employee's team lead.",
                   "- `Recruiter__c` records hold the team lead in `Team_Lead__c`.",
                   "- `Department__c` on `Interview__c` is the interview's department."):   # the same name elsewhere
        assert code_merge(QUESTION, bullet, _rec(), STATUS).text == RECORD_ANSWER, bullet
    for bullet in (B_USED_LATER, "- `Department__c` is the lookup field on `Recruiter__c` that names the team."):
        assert bullet in code_merge(QUESTION, bullet, _rec(), STATUS).text, bullet
    # "a lookup to `Obj`" names the lookup's target, not the object a field is on
    hosted = _query(obj="Session__c", tables=["Session__c", "Recruiter__c"], fields=[("Session__c", "Host_User__c")])
    target = "- The host is a lookup to `Recruiter__c` on `Session__c`."
    assert _added(target, hosted) == [target]
    # an object's name used for the lookup named after it ("the lookup `Recruiter__c`") is that field
    owned = _query(obj="Interview__c", tables=["Interview__c", "Recruiter__c"],
                   fields=[("Interview__c", "Recruiter__c")])
    legacy = "- `Recruiter_Name__c` is a legacy text field, superseded by the lookup `Recruiter__c`."
    counter = "- `Recruiter__c` records hold a running count in `No_of_Assigned_Candidates__c`."
    assert _added(legacy, owned) == [legacy] and _added(counter, owned) == []
    # "`Obj`'s `Field`": the field of that object (here not the one the count used)
    catalog = ("Interview__c", "Recruiter__c", "Session__c")
    assert _added("- `Session__c`'s `Recruiter__c` is the session's recruiter.", owned, objects=catalog) == []
    assert _added("- `Interview__c`'s `Recruiter__c` is the owning recruiter.", owned, objects=catalog) == [
        "- `Interview__c`'s `Recruiter__c` is the owning recruiter."]


def test_the_first_object_bullet_and_the_used_fields_come_first_at_the_cap():
    objects = ["- `Recruiter__c` holds the employees.", "- `Recruiter__c` has one record per person.",
               "- `Recruiter__c` is the staff directory."]
    m = code_merge(QUESTION, "\n".join(objects + [B_FIELD]), _rec(), STATUS)   # the used field fourth: still kept
    assert m.text == LEAD + "\n\n" + "\n".join(objects[:2] + [B_FIELD]) + "\n\n" + SOURCE   # in their order


def test_the_catalog_objects_tell_an_object_from_a_field():
    """An object that no query read is no field when the record catalog has it (``objects``): without it, the custom
    name reads as a field the count did not use."""
    bullet = "- `Recruiter__c` holds the employees, not the candidates of `Candidate_Profile__c` records."
    assert code_merge(QUESTION, bullet, _rec(), STATUS).text == RECORD_ANSWER
    m = code_merge(QUESTION, bullet, _rec(), STATUS, objects=("Recruiter__c", "Candidate_Profile__c"))
    assert m.text == LEAD + "\n\n" + bullet + "\n\n" + SOURCE


# ------------------------------------------------------------------------------- people, meta text, cut-off bullets
PEOPLE = "Zed Quill is Marlo Fenn's manager (`Manager__c`).\n\n" + SOURCE
B_MANAGER = "- `Manager__c` on `Recruiter__c` is the employee's manager."


def _people_query(qid: str = "q1") -> QueryEvidence:
    """about_people of fake data: Zed Quill is Marlo Fenn's manager; its read list holds several objects."""
    res = {"references": [{"person": "P1", "record": "R2", "object": "Recruiter__c", "field": "Manager__c",
                           "role": "manager", "records": 1}],
           "system_references": [{"person": "P1", "record": "R1", "object": "Lead", "field": "CreatedById",
                                  "role": "record creator", "records": 3}],
           "links": [{"between": ["P1", "P2"], "how": "own record", "path": "Recruiter__c.Manager__c (own record)",
                      "records": 1}]}
    return _query(qid, tool="about_people", tables=["Recruiter__c", "Account", "Lead", "User", "Interview__c"],
                  result=res)


def test_a_people_answer_keeps_only_bullets_about_the_lookups_it_used():
    """quality-1 of the review: the lean step reads no record, so what it says about the people contradicts the
    record answer. Bullets that name a person, claim that no field links them or name only objects of the person
    tool's read list are left out; one about the lookup the answer used stays."""
    question = "how is zed quill related to marlo fenn?"
    facts = "\n".join([
        '- The knowledge base contains no specific records or relationships for "Zed Quill" or "Marlo Fenn"; the only '
        "person-related objects documented are `Account` (for candidates), `Lead` and `User`, none of which link "
        "these two individuals.",
        "- No lookup field on `Interview__c` directly references a person by name; person association is handled "
        "through `Interview_Participant__c`.",
        "- Zed Quill is a person, so the records are linked through a `Manager__c` lookup.",   # names a person only
        "- `Recruiter__c` holds the employees (staff).",           # an object of the read list
        "- `CreatedById` is the user who created a lead.",          # the person's system work: no lookup used
        B_MANAGER])
    added = _added(facts, _people_query(), answer=PEOPLE, question=question,
                   rec={"named_people": ["zed quill", "marlo fenn"]})
    assert added == [B_MANAGER]
    # the objects a people answer names (each reference's) make no object bullet relevant
    named = PEOPLE.replace("(`Manager__c`)", "(`Manager__c` on their `Recruiter__c` records)")
    assert _added("- `Recruiter__c` holds the employees (staff).", _people_query(), answer=named,
                  question=question) == []
    # a person tool next to a count: the lookups the count used, not the person's references
    count = _query("q2", obj="Interview__c", fields=[("Interview__c", "Interviewer__c")])
    took = "Zed Quill took 6 client interviews.\n\n" + SOURCE
    interviewer = "- `Interviewer__c` is the employee who took the interview."
    assert _added("\n".join([B_MANAGER, interviewer, B_INT]), _people_query(), count, answer=took,
                  question="how many client interviews did zed quill take?",
                  rec={"named_people": ["zed quill"]}) == [interviewer, B_INT]


def test_the_people_words_come_from_every_name_of_the_question():
    rec = _rec(named_people=["zed quil"], named_spellings={"zed quil": ["Zed Quill"]},
               triage={"names": ["Zed Quil", "Ana Bo"], "people": ["zed quil"], "missing": ["ana bo"],
                       "spelling": [{"typed": "quil", "suggestion": "Quill", "kind": "person"}]})
    assert merge._people_words(rec) == {"zed", "quil", "quill", "ana"}   # "bo": under 3 letters
    assert merge._names_people("- `Recruiter__c` holds Quill's records.", {"quill"})
    assert not merge._names_people("- `Quill_Code__c` holds a code.", {"quill"})   # part of an API name
    assert not merge._names_people(B_FIELD, set())


def test_talk_about_the_knowledge_the_question_or_the_prompt_advice_and_guesses_are_left_out():
    """quality-5 of the review: the lean step's third-person meta text passes the record drafts' cleaners (shapes of the
    page log and the evaluation runs, made-up names). Each bullet alone is relevant, so only this check decides."""
    meta = [
        "- the object that holds the records the question asks about, and what it is (`Recruiter__c` = employees);",
        "- for a person, the one lookup field whose documented role (its reference role, or the relationship's "
        "from_target) fits the question's verb: `Department__c`;",
        "- The question names a team but no other noun; the objects holding employees are `Recruiter__c` and `User`.",
        "- No specific knowledge chunk defines the team; the field linking it to `Recruiter__c` is not defined.",
        "- `Recruiter__c` counts come from the tool results.",
        "- To count the team, one must first locate the `Department__c` record and then read its employees.",
        "- `Department__c` should be used to count employees per team.",
        "- `Recruiter__c` has no documented lookup field for team leads.",
        "- `Recruiter__c` would be found through the `Department__c` lookup if such a team exists.",
        "- `Department__c` is probably the field for the team.",
        "- No field on `Department__c` stores a count of employees; the count must be derived from `Recruiter__c`.",
        "- a legacy or superseded field or value: `Department__c`.",   # the lean prompt's own words, nothing else
    ]
    m = code_merge(QUESTION, "\n".join(meta), _rec(), STATUS)
    assert (m.text, m.merged, m.error) == (RECORD_ANSWER, False, None)
    for bullet in meta:
        assert code_merge(QUESTION, bullet, _rec(), STATUS).text == RECORD_ANSWER, bullet
        assert merge._relevance(bullet, merge._used_api(_rec())), bullet   # relevant: the meta check left it out
    assert merge._prompt_echo(meta[-1]) and not merge._NOT_MEANING_RE.search(meta[-1])
    # the business words of the same kind stay: a question bank and its question text, the user who picks them
    bank = _query(obj="Question_Bank__c")
    for bullet in ("- `Question_Bank__c` is the object that holds the question bank records.",
                   "- `Question_Bank__c` holds one record per question; the question text is its name.",
                   "- `Question_Bank__c` holds the questions the user picks for an interview template."):
        assert _added(bullet, bank) == [bullet], bullet
    assert not merge._prompt_echo(B_FIELD) and not merge._prompt_echo(
        "- `Interview__c` = client interviews, `Internal_Interview__c` = internal mock interviews: different objects.")


def test_a_cut_off_or_overlong_bullet_is_left_out(monkeypatch):
    """quality-6 of the review: a lean reply cut at its token limit (an open code span, bracket or bold, a last word
    that leaves it unfinished) or a bullet longer than CODE_MERGE_MAX_CHARS."""
    for bullet in ("- `Recruiter__c` holds the employees, `Department__c",
                   "- `Department__c` is the department (a lookup to `Department__c` records",
                   "- `Department__c` is the **department** of the **team",
                   "- `Department__c` names the team of the"):
        assert code_merge(QUESTION, bullet, _rec(), STATUS).text == RECORD_ANSWER, bullet
    long = "- `Department__c` is the employee's department" + ", a team" * 60 + "."
    assert len(long) > merge.CODE_MERGE_MAX_CHARS == 400
    assert code_merge(QUESTION, long, _rec(), STATUS).text == RECORD_ANSWER
    monkeypatch.setattr(merge, "CODE_MERGE_MAX_CHARS", 1000)   # read at call time
    assert long in code_merge(QUESTION, long, _rec(), STATUS).text


def test_after_a_name_was_not_found_a_bullet_with_a_number_is_left_out():
    """behaviour-4 of the review, combine's not-found count rule: when the record answer says the name is not in the
    records, a bullet with any number beyond the question's (0 to 12 too; dates aside) is left out."""
    failed = QueryEvidence(query_id="q1", tool="count_records", kind="data", object="Interview__c", arguments={},
                           ok=False, error="E4h: no Employee record is named 'Zed Quil'", applied={}, notes=[],
                           statements=[], tables=[], synced_at={}, summary="error", model_result=None, elapsed_ms=3.0,
                           error_code="E4h", fields_used=[("Interview__c", "Interviewer__c")],
                           not_found=[{"name": "Zed Quil", "target": "Employee (Recruiter__c)",
                                       "object": "Interview__c", "field": "Interviewer__c", "match": "exact",
                                       "close": ["Zed Quill"]}])
    answer = ("No Employee (`Recruiter__c`) record is named 'Zed Quil' in the records (checked through "
              "`Interview__c.Interviewer__c`). There is no count for this name.\n\n" + SOURCE)
    question = "how many client interviews did zed quil take in 2026?"
    numbered = ["- `Interviewer__c` is the employee who took the interview; it is filled on 9 records.",
                "- `Interview__c` has 3 record types (client, internal, demo)."]
    plain = "- `Interviewer__c` is the employee who took the interview since 2026-01-01."   # a date and the question's
    assert _added("\n".join(numbered + [plain]), failed, answer=answer, question=question) == [plain]
    # the same bullets after a count that found records: kept (the rule is what left them out)
    count = _query(obj="Interview__c", fields=[("Interview__c", "Interviewer__c")])
    took = "Zed Quill took 6 client interviews (`Interview__c`).\n\n" + SOURCE
    assert _added("\n".join(numbered), count, answer=took, question=question) == numbered


# ------------------------------------------------------------------------------- after a list: a label line (review)
LISTED = ("6 employees (`Recruiter__c`) are in Zorvex, by status (`Status__c`):\n- Active: 4\n- Former: 2\n\n"
          + SOURCE)


def test_after_a_list_the_bullets_start_their_own_list_under_a_label_line():
    """behaviour-2 and quality-4 of the review: Markdown and the page's renderer read bullets after a blank line as
    more items of a list the record answer ends with; a label line (prompts/code_merge_label.md) ends it."""
    status = _query(fields=[("Recruiter__c", "Status__c")], result={"count": 6, "groups": [
        {"value": "Active", "count": 4}, {"value": "Former", "count": 2}]})
    assert merge.CODE_MERGE_LABEL == "What the fields mean:"
    added = _added(B_REC + "\n" + "- `Status__c` = 'Former' marks an employee who left.", status, answer=LISTED)
    assert added == [merge.CODE_MERGE_LABEL, B_REC, "- `Status__c` = 'Former' marks an employee who left."]
    body = LISTED.split("\n\n")[0]
    for tail in ("\n1. Active: 4", "\n- Active: 4\n  (two of them on leave)", "\n* Active: 4"):   # numbers, more lines
        answer = body + tail + "\n\n" + SOURCE
        assert _added(B_REC, status, answer=answer)[0] == merge.CODE_MERGE_LABEL, tail
    assert _added(B_REC, status, answer="6 employees are in Zorvex.\n\n" + SOURCE) == [B_REC]   # prose: no label


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_renders_the_added_bullets_as_a_list_of_their_own():
    status = _query(fields=[("Recruiter__c", "Status__c")], result={"groups": [
        {"value": "Active", "count": 4}, {"value": "Former", "count": 2}]})
    m = code_merge(QUESTION, B_REC, _rec(LISTED, evidence=[status]), STATUS)
    body = m.text.rsplit("\n\n", 1)[0]   # the page shows the Source line on its own (web/pipeline.split_source)
    no_label = body.replace(merge.CODE_MERGE_LABEL + "\n", "")   # the layout before the review
    proc = subprocess.run(["node", str(ROOT / "tests" / "render_check.js")], capture_output=True, text=True,
                          input=json.dumps({"merged": body, "no_label": no_label}), timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["merged"]["tree"].endswith('ul(li("Active: 4"),li("Former: 2")),p("What the fields mean:"),'
                                          'ul(li(code("Recruiter__c")," holds the employees."))')
    assert out["no_label"]["tree"].endswith('ul(li("Active: 4"),li("Former: 2"),li(code("Recruiter__c"),'
                                            '" holds the employees."))')   # the defect: one list


def test_code_merge_lines_and_plans_aloud():
    m = code_merge(QUESTION, FACTS, _rec(LEAD), STATUS)   # a record answer without a Source line gets one
    assert merge.code_merge_lines(m.text, LEAD) == [B_OBJECT, B_FIELD]
    assert merge.plans_aloud("Let me compile the comprehensive answer. " + LEAD + "\n" + SOURCE)
    assert merge.plans_aloud("Wait, the count is 6.\n" + SOURCE) and not merge.plans_aloud(RECORD_ANSWER)
    assert not merge.plans_aloud("Let me know if you need more.\n" + SOURCE)


TEAM_LEAD = "6 employees (`Recruiter__c`) are in Zorvex; `Team_Lead__c` is set on all of them.\n\n" + SOURCE
NOT_SYNCED = "- `Team_Lead__c` is not synced to the warehouse."


def _holds(api, context=()):   # the warehouse's view of fake data: the employees and their team lead are synced
    return True if str(api).split(".")[-1] in ("Team_Lead__c", "Recruiter__c") else None


def test_the_warehouse_availability_drops_a_false_not_synced_claim():
    """tests-5 of the review: a bullet that calls a field not synced while the warehouse holds it adds a scope issue
    only with the availability (``merge.warehouse_availability``); without it the claim is not caught."""
    rec = _rec(TEAM_LEAD)
    assert NOT_SYNCED in code_merge(QUESTION, NOT_SYNCED, rec, STATUS).text
    assert code_merge(QUESTION, NOT_SYNCED, rec, STATUS, availability=_holds).text == TEAM_LEAD


# ------------------------------------------------------------------------------------------------ the switch
def test_the_switch_is_read_at_each_call_and_never_stops_a_run(monkeypatch):
    monkeypatch.delenv("SFA_MERGE", raising=False)
    assert C.merge_settings() == C.MergeConfig() == C.MergeConfig(by="code", problem="")
    for raw, by in (("", "code"), ("  ", "code"), ("code", "code"), ("CODE", "code"), (" model ", "model"),
                    ("Model", "model")):
        monkeypatch.setenv("SFA_MERGE", raw)
        assert C.merge_settings() == C.MergeConfig(by=by), raw
    for raw in ("off", "on", "llm", "model,code"):
        monkeypatch.setenv("SFA_MERGE", raw)
        assert C.merge_settings() == C.MergeConfig(by="code", problem="SFA_MERGE must be code or model"), raw
    assert C.MERGE_ENV_VAR == "SFA_MERGE" and C.MERGE_MODES == ("code", "model")
    assert C.MERGE_ENV_VAR not in C.ENV_ALIASES.values()   # sf_assistant's own name: no OKEVAL_* alias
    assert {n: d for n, d, _ in C.ENV_DOC}["SFA_MERGE"] == "code"


# ------------------------------------------------------------------------------------------------ pipeline.answer
def _completion(content=None, calls=(), finish=None):
    tcs = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish or ("tool_calls" if tcs
                                                                                           else "stop"))],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


class Model:
    """``chat.completions.create`` of a fake OpenAI client, told apart by the system prompt: the route call (RECORDS),
    the names call (NONE), the record agent (one count, then ``record``), the merge call (``merged``) and the
    knowledge steps (one search, then ``facts``). ``requests`` keeps every request (thread-safe)."""

    def __init__(self, facts: str = FACTS, record: str = RECORD_ANSWER, merged: str = MERGED_BY_MODEL):
        self.facts, self.record, self.merged = facts, record, merged
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        with self._lock:
            self.requests.append(kwargs)
        system = str(kwargs["messages"][0].get("content") or "")
        n_tools = sum(1 for m in kwargs["messages"] if m.get("role") == "tool")
        if system == rprompts.ROUTE_SYSTEM:
            return _completion("RECORDS")
        if system == rprompts.NAMES_SYSTEM:
            return _completion("NONE")
        if system == rprompts.COMBINE_SYSTEM:
            return _completion(self.merged)
        if system.startswith("You answer questions about Salesforce RECORDS"):
            if n_tools == 0:
                return _completion(calls=[("count_records", {"object": "Recruiter__c", "filters": [
                    {"field": "Department__c", "op": "eq", "value": "Zorvex"}]})])
            return _completion(self.record)
        if kwargs.get("tools") and n_tools == 0:   # a knowledge step looks the field up first
            return _completion(calls=[("search_fields", {"query": "department"})])
        return _completion(self.facts)

    def merge_requests(self) -> list[dict]:
        return [r for r in self.requests if r["messages"][0].get("content") == rprompts.COMBINE_SYSTEM]


class StubTools:
    """The record tools surface the triage and the record agent use (fake data): no stored person, no value spot;
    count_records -> 6 (with ``extra`` keys in its result; ``fields`` and ``applied`` replace the query's
    ``fields_used`` and "applied" block; ``catalog``: a record catalog stand-in with its ``objects``)."""

    def __init__(self, fields=None, applied=None, catalog=None, **extra):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {})
                      for n in ("count_records", "list_records")}
        self.extra = extra
        self.fields, self.applied = fields, applied
        if catalog is not None:
            self.catalog = catalog
        self._evidence: list[QueryEvidence] = []

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        ev = _evidence(f"q{len(self._evidence) + 1}", arguments or {}, **self.extra)
        if self.fields is not None:
            ev.fields_used = list(self.fields)
        if self.applied is not None:
            ev.applied = ev.model_result["applied"] = dict(self.applied)
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def spot_people(self, question):
        return []

    def resolve_person_name(self, name):
        return None

    def spot_values(self, question, people=None):
        return []

    def begin_question(self, question="", people=None, values=None):
        self._evidence = []

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question, missing=()):
        return "[Hints from code, not from the user]\nRecords named in the question: (fake)"

    def status(self):
        return STATUS

    def close(self):
        pass


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def _answer(model: Model, tools: StubTools | None = None, **kw):
    cfg = _cfg()
    session = RecordSession(tools or StubTools(), cfg, llm=LLM(cfg, client=model))
    o = pipeline.answer(QUESTION, cfg, _registry(), None, LLM(cfg, client=model), 1, session, **kw)
    return o, pipeline.result_row(o, session), session


def _merge_stage_models(o) -> int:
    return sum(1 for e in o.timeline["entries"] if e["kind"] == "model" and e["stage"] == "merge")


def test_lean_mode_merges_by_code_without_a_merge_call(monkeypatch):
    monkeypatch.delenv("SFA_MERGE", raising=False)   # unset: code, the default
    model = Model()
    o, row, _s = _answer(model)
    assert o.mode == "lean" and o.checks == [] and model.merge_requests() == []   # no COMBINE_SYSTEM request
    assert o.kind == "merged" and o.m.merged and o.merge_by == "code" and o.merge_switch == ""
    assert o.rec.answer == RECORD_ANSWER
    assert o.final == o.body == LEAD + "\n\n" + B_OBJECT + "\n" + B_FIELD + "\n\n" + SOURCE   # B_OTHER left out
    j = row["jsonl"]
    assert (j["answer_kind"], j["merge_by"], j["merge_error"], j["rejected_numbers"]) == ("merged", "code", None, [])
    assert "merge_switch" not in j and j["merge_seconds"] is not None
    # the timeline keeps its merge stage, with no model call in it
    assert any(e["kind"] == "stage" and e["stage"] == "merge" for e in o.timeline["entries"])
    assert _merge_stage_models(o) == 0
    monkeypatch.setenv("SFA_MERGE", "code")   # the same, set explicitly
    o2, _row, _s = _answer(Model())
    assert (o2.final, o2.kind, o2.merge_by) == (o.final, "merged", "code")


def test_empty_lean_facts_give_the_record_answer(monkeypatch, capsys):
    monkeypatch.setenv("SFA_MERGE", "code")
    o, row, session = _answer(Model(facts=""))   # the lean step's empty reply: no facts block at all
    assert o.kind == "records" and o.final == RECORD_ANSWER and o.merge_by == "code"
    assert row["jsonl"]["merge_error"] == "the knowledge agent gave no answer" and row["jsonl"]["merge_by"] == "code"
    # facts without a bullet that fits: the record answer as well, and no error (nothing went wrong)
    model = Model(facts=B_OTHER + "\nThe department groups employees.")
    o, row, session = _answer(model)
    assert o.kind == "records" and o.final == RECORD_ANSWER and model.merge_requests() == []
    assert row["jsonl"]["merge_error"] is None and row["jsonl"]["answer_kind"] == "records"
    cli.print_outcome(o, session, False)
    out = capsys.readouterr().out
    assert ("-- no field meaning of the knowledge agent fits the answer (merge by code); the record agent's answer "
            "is shown") in out and "unknown error" not in out
    assert "\nKnowledge answer:\n" + B_OTHER in out   # an unmerged answer shows the facts block, as before


def test_a_field_check_line_keeps_the_merge_call(monkeypatch):
    """A FIELD CHECK line (here: the question's word fits a lookup that is documented but not synced) needs the merge
    call: it may change which count leads."""
    monkeypatch.setenv("SFA_MERGE", "code")
    unsynced = [{"field": "Team_Lead__c", "fits": "team", "role": "team lead"}]
    record = "6 employees (`Recruiter__c`) are in Zorvex; the team lead (`Team_Lead__c`) is not in the warehouse.\n"
    model = Model(record=record + SOURCE)
    o, row, _s = _answer(model, StubTools(unsynced_fits=unsynced))
    assert o.mode == "lean" and len(o.checks) == 1 and o.checks[0]["kind"] == "unsynced"
    (req,) = model.merge_requests()
    assert "FIELD CHECK" in req["messages"][1]["content"]
    assert o.merge_by == "model" and row["jsonl"]["merge_by"] == "model" and row["jsonl"]["field_check"]


def test_full_mode_keeps_the_merge_call(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")
    model = Model()
    o, row, _s = _answer(model, lean_knowledge=False)   # --full-knowledge: the full knowledge agent answers
    assert o.mode == "full" and len(model.merge_requests()) == 1
    assert o.kind == "merged" and o.final == MERGED_BY_MODEL and row["jsonl"]["merge_by"] == "model"


def test_sfa_merge_model_gives_the_merge_call_as_before(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "model")
    model = Model()
    o, row, _s = _answer(model)
    assert o.mode == "lean" and o.merge_by == "model" and row["jsonl"]["merge_by"] == "model"
    # the merge call of before 2026-10-29: the same request (the cleaned facts block, the record answer, the facts)
    (req,) = model.merge_requests()
    want = rprompts.combine_user_message(QUESTION, FACTS, RECORD_ANSWER,
                                         facts_json(o.rec.evidence, merge.MERGE_FACTS_CHARS), [])
    assert req["messages"][1]["content"] == want and req["max_tokens"] == merge.MERGE_MAX_TOKENS
    assert o.kind == "merged" and o.final == MERGED_BY_MODEL and _merge_stage_models(o) == 1
    # and the same output as combine() itself gives for this record answer and facts block
    direct = merge.combine(LLM(_cfg(), client=Model()), QUESTION, FACTS, o.rec, STATUS, field_check=[],
                           availability=None, knowledge_numbers=False)
    assert (o.m.text, o.m.merged, o.m.error, o.m.rejected_numbers) == (direct.text, direct.merged, direct.error,
                                                                        direct.rejected_numbers)


def test_an_invalid_switch_is_code_and_the_jsonl_says_so(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "zorvex")
    model = Model()
    o, row, _s = _answer(model)
    assert o.merge_by == "code" and model.merge_requests() == [] and o.kind == "merged"
    j = row["jsonl"]
    assert j["merge_by"] == "code" and j["merge_switch"] == "SFA_MERGE must be code or model"
    assert "zorvex" not in j["merge_switch"] and QUESTION not in j["merge_switch"]   # no text from the question


def test_merge_by_is_left_out_without_a_merge(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")
    model = Model()
    o, row, _s = _answer(model, records_only=True)   # records-only mode: no knowledge step, no merge
    assert o.m is None and o.merge_by is None and "merge_by" not in row["jsonl"] and "merge_switch" not in row["jsonl"]
    assert o.kind == "records" and o.final == RECORD_ANSWER and model.merge_requests() == []
    monkeypatch.setenv("SFA_MERGE", "maybe")   # an invalid switch is reported on every answer
    o, row, _s = _answer(Model(), records_only=True)
    assert "merge_by" not in row["jsonl"] and row["jsonl"]["merge_switch"] == "SFA_MERGE must be code or model"


def test_the_cli_prints_a_code_merged_answer(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")
    model = Model()
    cfg = _cfg()
    session = RecordSession(StubTools(), cfg, llm=LLM(cfg, client=model))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        row = cli.ask(QUESTION, cfg, _registry(), None, LLM(cfg, client=model), 1, False, session, 20)
    text = out.getvalue()
    assert (LEAD + "\n\n" + B_OBJECT + "\n" + B_FIELD + "\n\n" + SOURCE + "\n") in text
    assert "-- answer merged from the knowledge agent and 1 record query (" in text and "Knowledge answer:" not in text
    assert row["answer"] == "merged" and row["jsonl"]["merge_by"] == "code" and model.merge_requests() == []


def test_the_record_session_facade_keeps_the_notebook_names():
    """code_merge is new code in agent/merge.py; the record_session facade exports the notebook's names only."""
    assert "code_merge" not in record_session.__all__ and not hasattr(record_session, "code_merge")


# ------------------------------------------------------------------------- the pipeline after the review (2026-10-29)
LC_LEAD = [{"object": "Recruiter__c", "field": "Team_Lead__c", "target": "Recruiter__c", "role": "team lead",
            "person_kind": "employee", "filter": "Team_Lead__c.Name = 'Zed Quill'", "records": 6, "fits": "lead"}]


def test_the_lookup_footer_reads_the_answer_without_the_added_bullets(monkeypatch):
    """quality-3 of the review: a bullet that names the lookup must not take away the footer that says what was
    counted; the footer goes where ``with_lookup_footer`` puts it (before the Source line)."""
    monkeypatch.setenv("SFA_MERGE", "code")
    record = "6 employees have Zed Quill as their team lead.\n\n" + SOURCE   # names no lookup
    lead = "- `Team_Lead__c` is the employee's team lead."
    o, _row, _s = _answer(Model(facts=lead, record=record), StubTools(lookup_check=LC_LEAD))
    footer = with_lookup_footer(record, o.rec.evidence).split("\n")[-2]
    assert footer.startswith("Lookup used: `Recruiter__c.Team_Lead__c` (team lead)")
    assert o.kind == "merged" and o.final == record.replace("\n\n" + SOURCE,
                                                           "\n\n" + lead + "\n\n" + footer + "\n" + SOURCE)
    assert with_lookup_footer(o.body, o.rec.evidence) == o.body   # read with the bullet, the footer was left out
    # pipeline.with_footer itself: without added lines it is with_lookup_footer; a footer line at the end stays there
    assert pipeline.with_footer(record, o.rec.evidence) == with_lookup_footer(record, o.rec.evidence)
    bare = "6 employees have Zed Quill as their team lead.\n" + lead
    assert pipeline.with_footer(bare, o.rec.evidence, [lead]) == bare + "\n" + footer


def _status_of(obj):   # the record session's view of the status field (fake): the training status values
    return ({"field": "Training_Status__c", "label": "Training", "field_label": "Training Status",
             "values": [("Scheduled", "Scheduled"), ("Completed", "Completed")]} if obj == "Recruiter__c" else None)


def test_the_status_lines_read_the_answer_without_the_added_bullets(monkeypatch):
    """quality-3 of the review: a bullet that names a status value no record has must not take away the status line
    that says so (Group B item 11)."""
    monkeypatch.setenv("SFA_MERGE", "code")
    done = "- `Training_Status__c` = 'Completed' marks a training that took place."
    record = "Zed Quill took 6 trainings (`Recruiter__c`, as `Team_Lead__c`).\n\n" + SOURCE
    model = Model(facts=done, record=record)
    lc = [dict(LC_LEAD[0], fits="take", role="trainee")]
    tools = StubTools(fields=[("Recruiter__c", "Team_Lead__c"), ("Recruiter__c", "Training_Status__c")],
                      applied={"filters": ["Team_Lead__c.Name = 'Zed Quill'"]}, lookup_check=lc,
                      breakdown={"Training_Status__c": {"Scheduled": 6}})
    cfg = _cfg()
    session = RecordSession(tools, cfg, llm=LLM(cfg, client=model))
    session.status_of = _status_of
    o = pipeline.answer("how many trainings did zed quill take?", cfg, _registry(), None, LLM(cfg, client=model), 1,
                        session)
    line = "Status of these Training records (`Training_Status__c`, 6): Scheduled 6; none is Completed."
    assert o.merge_by == "code" and o.kind == "merged" and done in o.final
    assert o.status_lines == [line] and o.final.endswith(line + "\n" + SOURCE)


def test_the_person_lead_reads_the_answer_without_the_added_bullets():
    """quality-3 of the review: the spelling help's person lead is added when only an added bullet names the asked
    spelling (``spelling_final`` gets the added lines; ``with_person_lead`` reads ``said``)."""
    sp = QuestionSpelling("how many employees does zed quil lead")
    session = SimpleNamespace(tools=SimpleNamespace(question_people=lambda: [
        {"words": "zed quil", "asked": "zed quil", "read_as": "Zed Quill", "names": ["Zed Quill"]}]))
    bullet = "- `Team_Lead__c` holds the team lead, such as 'zed quil'."
    text = "6 employees have Zed Quill as their team lead.\n\n" + bullet + "\n" + SOURCE
    lead = "No record is named 'zed quil'; the closest stored name is 'Zed Quill'."
    assert pipeline.spelling_final(sp, text, "merged", None, None, session) == text   # read with the bullet: no lead
    assert pipeline.spelling_final(sp, text, "merged", None, None, session, [bullet]) == lead + "\n\n" + text
    assert pipeline.with_person_lead(text, [("zed quil", "Zed Quill")], said="6 employees.") == lead + "\n\n" + text


def test_a_record_answer_that_plans_aloud_keeps_the_merge_call(monkeypatch):
    """quality-7 of the review: the merge call rewrote a planning lead the record session's cleaners keep ("Let me
    compile the comprehensive answer."); code would show it, so such an answer gets the merge call."""
    monkeypatch.setenv("SFA_MERGE", "code")
    record = "Let me compile the comprehensive answer. " + RECORD_ANSWER
    model = Model(record=record)
    o, row, _s = _answer(model)
    assert o.rec.answer == record and merge.plans_aloud(o.rec.answer)
    assert o.mode == "lean" and o.merge_by == "model" and len(model.merge_requests()) == 1
    assert o.final == MERGED_BY_MODEL and row["jsonl"]["merge_by"] == "model"


def test_the_pipeline_passes_the_warehouse_availability(monkeypatch):
    """tests-5 of the review: pipeline.answer hands ``session.availability()`` to code_merge, so a bullet that calls
    a synced field not synced is left out."""
    monkeypatch.setenv("SFA_MERGE", "code")
    for availability, shown in ((_holds, False), (None, True)):
        model = Model(facts=NOT_SYNCED + "\n" + B_OBJECT, record=TEAM_LEAD)
        cfg = _cfg()
        session = RecordSession(StubTools(), cfg, llm=LLM(cfg, client=model))
        monkeypatch.setattr(session, "availability", lambda a=availability: a)
        o = pipeline.answer(QUESTION, cfg, _registry(), None, LLM(cfg, client=model), 1, session)
        assert o.merge_by == "code" and B_OBJECT in o.final and (NOT_SYNCED in o.final) is shown, availability


def test_the_pipeline_passes_the_catalog_objects(monkeypatch):
    """pipeline.answer hands the record catalog's objects to code_merge (``objects``, a dict name -> spec since the
    step-2 fixes): their names tell an object from a field."""
    monkeypatch.setenv("SFA_MERGE", "code")
    bullet = "- `Recruiter__c` holds the employees, not the candidates of `Candidate_Profile__c` records."
    spec = SimpleNamespace(has_table=True, rows=6, fields={}, not_synced={})
    catalog = SimpleNamespace(objects={"Recruiter__c": spec, "Candidate_Profile__c": spec})
    for tools, kept in ((StubTools(catalog=catalog), True), (StubTools(), False)):
        o, _row, _s = _answer(Model(facts=bullet), tools)
        assert o.merge_by == "code" and (bullet in o.final) is kept


def test_the_pipeline_hands_the_catalog_specs_to_the_bullet_checks(monkeypatch):
    """Review of 2026-10-30: pipeline.answer passes the record catalog as a dict (name -> spec), so the catalog checks
    run in the live pipeline: a bullet that contradicts it (a lookup target the field does not have, F2; a picklist
    called a lookup) is left out; with the object names only (the hand-off before the step-2 fixes) it would be
    kept."""
    monkeypatch.setenv("SFA_MERGE", "code")
    wrong = "- `Department__c` on `Recruiter__c` is a lookup to `User` that names the team."
    objects = {"Recruiter__c": _spec("Recruiter__c", [_fs("Department__c", "reference", ["Department__c"])]),
               "Department__c": _spec("Department__c", [_fs("Name", "name")]),
               "User": _spec("User", [_fs("Name", "name")])}
    o, _row, _s = _answer(Model(facts=B_OBJECT + "\n" + wrong), StubTools(catalog=SimpleNamespace(objects=objects)))
    assert o.merge_by == "code" and B_OBJECT in o.final and wrong not in o.final
    o, _row, _s = _answer(Model(facts=B_OBJECT + "\n" + wrong), StubTools())   # no catalog: no verdict
    assert o.merge_by == "code" and wrong in o.final
    called = "- `Department__c` is the lookup field that names the team."   # a picklist called a lookup
    picklist = dict(objects, Recruiter__c=_spec("Recruiter__c", [_fs("Department__c", "picklist")]))
    o, _row, _s = _answer(Model(facts=B_OBJECT + "\n" + called), StubTools(catalog=SimpleNamespace(objects=picklist)))
    assert B_OBJECT in o.final and called not in o.final


def test_the_pipeline_hands_the_added_lines_to_the_spelling_help(monkeypatch):
    """quality-3 of the review: with the spelling help on, ``pipeline.answer`` passes the lines the merge by code added
    to ``spelling_final`` (the person lead reads the answer without them)."""
    monkeypatch.setenv("SFA_MERGE", "code")
    monkeypatch.setattr(pipeline, "question_spelling", lambda text, registry, session=None: QuestionSpelling(text))
    seen = []
    real = pipeline.spelling_final

    def spy(sp, text, kind, rec=None, r=None, session=None, inserted=()):
        seen.append(list(inserted))
        return real(sp, text, kind, rec, r, session, inserted)

    monkeypatch.setattr(pipeline, "spelling_final", spy)
    o, _row, _s = _answer(Model())
    assert o.kind == "merged" and o.merge_by == "code" and seen == [[B_OBJECT, B_FIELD]]



# ------------------------------------------------------------------ the step-2 fixes of 2026-10-30 (docs/runbook.md)
# Shapes of the failures the code-merge evaluation of 2026-10-29 found, with made-up objects, fields, people and
# counts. A fake record catalog (objects as a dict name -> spec, as pipeline.answer passes it since these fixes):
# FieldSpecs and the raw knowledge entries the legacy check reads.
def _fs(api, kind="text", targets=(), picklist=(), filled=0, legacy=False):
    """A FieldSpec of fake data (``legacy`` sets FieldSpec.legacy, which the checks must never read)."""
    return FieldSpec(api=api, label=api, kb_type=None, db_type="VARCHAR", kind=kind, meaning="", legacy=legacy,
                     ref_targets=list(targets), picklist=list(picklist), filled=filled)


def _spec(api, fields, rows=0, marks=None, not_synced=None):
    """An ObjectSpec stand-in: ``fields`` (FieldSpecs), one raw knowledge entry per field (``marks``: api -> more keys
    of the entry: legacy, replaced_by, semantic, business_meaning), ``rows`` records."""
    raw = {"object": {"api_name": api}, "fields": [{"api_name": f.api, "label": f.api, **(marks or {}).get(f.api, {})}
                                                    for f in fields]}
    return SimpleNamespace(fields={f.api: f for f in fields}, not_synced=dict(not_synced or {}),
                           knowledge=ObjectKnowledge(raw), rows=rows)


VISIT_CATALOG = {
    "Visit__c": _spec("Visit__c", [_fs("Name", "name"), _fs("Visit_Helper__c", "reference", ["Helper__c"]),
                                   _fs("Visit_Status__c", "picklist"), _fs("Visit_Date__c", "date")]),
    "Visit_Guest__c": _spec("Visit_Guest__c", [_fs("Visit__c", "reference", ["Visit__c"]),
                                               _fs("Guest_Kind__c", "picklist"), _fs("User__c", "reference", ["User"]),
                                               _fs("RecordTypeId", "reference", ["RecordType"])]),
    "Helper__c": _spec("Helper__c", [_fs("Name", "name")]),
    "User": _spec("User", [_fs("Name", "name")]),
}
VISIT_ANSWER = "12 visits (`Visit__c`) were supported by Zed Quill (`Visit_Helper__c`).\n\n" + SOURCE


def _visits() -> QueryEvidence:
    return _query(obj="Visit__c", fields=[("Visit__c", "Visit_Helper__c"), ("Visit__c", "Visit_Status__c")],
                  applied={"filters": ["Visit_Status__c = 'Done'"]}, result={"count": 12})


def test_f2_a_bullet_that_contradicts_the_catalog_is_left_out():
    """F2 (P33, P37, HR17): a lookup target its reference targets do not hold, a field placed on an object that has no
    such field, a picklist called a lookup. Without a catalog entry (the objects as names only) no verdict."""
    drop = ["- The field `Visit_Helper__c` on `Visit__c` is a lookup to `User` that names the helper of the visit.",
            "- `Visit_Helper__c` on `Visit__c` is the lookup field for the helper (User); `Owner_Agent__c` on "
            "`Visit_Guest__c` is the lookup field for the guest's user record",
            "- `Visit_Status__c` is the lookup field linking the visit to the guest"]
    keep = ["- `Visit_Helper__c` on `Visit__c` is the lookup to `Helper__c` that names the employee who supports the "
            "visit.",
            "- `Visit_Status__c` is the picklist of the visit's lifecycle.",
            "- `Visit_Status__c` is not a lookup; it is the visit's lifecycle picklist."]   # negation: no claim
    for bullet in drop:   # each one alone
        assert _added(bullet, _visits(), answer=VISIT_ANSWER, objects=VISIT_CATALOG) == [], bullet
        assert _added(bullet, _visits(), answer=VISIT_ANSWER, objects=tuple(VISIT_CATALOG)) == [bullet], bullet
    for bullet in keep:
        assert _added(bullet, _visits(), answer=VISIT_ANSWER, objects=VISIT_CATALOG) == [bullet], bullet
    assert _added("\n".join(drop + keep), _visits(), answer=VISIT_ANSWER, objects=VISIT_CATALOG) == keep
    rec = _rec(VISIT_ANSWER, evidence=[_visits()])
    used = merge._used_api(rec, VISIT_CATALOG)
    reasons = [merge._contradiction(b, used, rec, VISIT_CATALOG) for b in drop]
    assert reasons == ["Visit_Helper__c is no lookup to User", "Visit_Guest__c has no field Owner_Agent__c",
                       "Visit_Status__c is a picklist, no lookup"]
    for bullet in ("- `Visit_Guest__c.RecordType.Name` tells the guest kinds apart.",   # RecordType: RecordTypeId
                   "- `Visit_Status__c` may be blank even if a helper is assigned in the `Visit_Guest__c` object"):
        assert merge._contradiction(bullet, used, rec, VISIT_CATALOG) == "", bullet   # a loose "in `O`": no verdict


LOG_ANSWER = "Zed Quill had the most escalated logs (7) by `Log_Owner__c`.\n\n" + SOURCE
FORM_ANSWER = "12 requests (`Request_Form__c`) are on the Data track.\n\n" + SOURCE


def _legacy_catalog(old_filled: int = 83) -> dict:
    """Daily_Log__c (nothing marked legacy), Visit__c (its status meaning mentions legacy values: FieldSpec.legacy is
    true, the raw entry marks nothing) and Request_Form__c (120 rows; Old_Track__c marked legacy, replaced by
    New_Track__c)."""
    return {
        "Daily_Log__c": _spec("Daily_Log__c", [_fs("Log_Owner__c", "reference", ["Helper__c"]),
                                               _fs("Log_Status__c", "picklist")],
                              marks={"Log_Owner__c": {"business_meaning": "The employee who owns the daily log."}}),
        "Visit__c": _spec("Visit__c", [_fs("Visit_Status__c", "picklist", legacy=True)],
                          marks={"Visit_Status__c": {"business_meaning": "The visit's lifecycle; it keeps legacy values "
                                                                         "of older visits."}}),
        "Request_Form__c": _spec("Request_Form__c", [_fs("Old_Track__c", "picklist", filled=old_filled),
                                                     _fs("New_Track__c", "reference", ["Track__c"], filled=4)], rows=120,
                                 marks={"Old_Track__c": {"legacy": True, "replaced_by": "New_Track__c"}}),
        "Helper__c": _spec("Helper__c", [_fs("Name", "name")]),
    }


def _logs() -> QueryEvidence:
    return _query(tool="group_records", obj="Daily_Log__c",
                  fields=[("Daily_Log__c", "Log_Owner__c"), ("Daily_Log__c", "Log_Status__c")],
                  applied={"filters": ["Log_Status__c = 'Escalated'"]},
                  result={"grouped_by": "Log_Owner__c (Helper name)", "measure": "count", "group_count": 1, "shown": 1,
                          "groups": [{"group": "Zed Quill", "value": 7}], "total_across_groups": 7})


def _forms(field: str) -> QueryEvidence:
    return _query(obj="Request_Form__c", fields=[("Request_Form__c", field)],
                  applied={"filters": [f"{field} = 'Data'"]}, result={"count": 12})


def test_f3_a_legacy_claim_the_knowledge_does_not_make_is_left_out():
    """F3 (HP11, HR16, HP04): a field called legacy or inactive that the knowledge marks legacy on none of its objects
    (the raw entry, never FieldSpec.legacy); a value-level note ("keeps inactive legacy values") is no claim."""
    cat = _legacy_catalog()
    for bullet in ("- The `Log_Owner__c` field is marked as legacy, with many rows in the snapshot having blank values.",
                   "- Caveat: `Log_Status__c` is a legacy field; the `Escalated` value is one of several statuses."):
        assert _added(bullet, _logs(), answer=LOG_ANSWER, objects=cat) == [], bullet
        assert _added(bullet, _logs(), answer=LOG_ANSWER, objects=tuple(cat)) == [bullet], bullet   # no catalog: kept
    visits = _query(obj="Visit__c", fields=[("Visit__c", "Visit_Status__c")], result={"count": 12})
    answer = "12 visits (`Visit__c`) are done.\n\n" + SOURCE
    inactive = "- `Visit_Status__c` (the older status field, which is now inactive) once held the result."
    values = ("- `Visit_Status__c` is the lifecycle status picklist, which keeps inactive legacy values like `Silent` "
              "from when results were stored there.")
    assert _added(inactive, visits, answer=answer, objects=cat) == []
    assert _added(values, visits, answer=answer, objects=cat) == [values]
    # a legacy note about a marked field the count did not use stays
    replacing = "- `New_Track__c` is the current track lookup, replacing the legacy `Old_Track__c` picklist"
    assert _added(replacing, _forms("New_Track__c"), answer=FORM_ANSWER, objects=cat) == [replacing]


def test_f10c_a_legacy_note_under_a_count_that_used_a_mostly_filled_field_is_left_out():
    """F10c (HR06, HR14): the count used the field the knowledge marks legacy, and the snapshot fills it on at least
    half of the rows (83 of 120): its legacy note undercuts a correct count. Filled on few rows (20 of 120) it stays, a
    warning that the count misses records."""
    note = "- `Old_Track__c` is a legacy picklist filled only on older requests"
    replacing = "- `New_Track__c` is the current track lookup, replacing the legacy `Old_Track__c` picklist"
    full, sparse = _legacy_catalog(83), _legacy_catalog(20)
    for bullet in (note, replacing):
        assert _added(bullet, _forms("Old_Track__c"), answer=FORM_ANSWER, objects=full) == [], bullet
        assert _added(bullet, _forms("Old_Track__c"), answer=FORM_ANSWER, objects=sparse) == [bullet], bullet
    rec = _rec(FORM_ANSWER, evidence=[_forms("Old_Track__c")])
    assert merge._contradiction(note, merge._used_api(rec, full), rec, full) == (
        "a legacy note on Old_Track__c, which the count used and the snapshot fills on most rows")


HELPER_ANSWER = "Zed Quill reports to Marlo Fenn (`Reports_To__c`).\n\n" + SOURCE
F4_NAMES = ("Helper__c", "Audit_Note__c", "Account", "Practice_Visit__c", "Session__c", "Visit__c")
F4_CATALOG = {
    "Helper__c": _spec("Helper__c", [_fs("Name", "name"), _fs("Reports_To__c", "reference", ["Helper__c"])]),
    "Audit_Note__c": _spec("Audit_Note__c", [_fs("Report_Manager__c", "reference", ["Helper__c"])]),
    "Account": _spec("Account", [_fs("Name", "name"), _fs("Old_Tally__c", "number")]),
    "Practice_Visit__c": _spec("Practice_Visit__c", [_fs("Host__c", "reference", ["Helper__c"]),
                                                     _fs("Reports_To__c", "reference", ["Helper__c"])]),
    "Session__c": _spec("Session__c", [_fs("Host_User__c", "reference", ["Helper__c"])]),
    "Visit__c": _spec("Visit__c", [_fs("Host__c", "reference", ["Helper__c"])]),
}


def _reports() -> QueryEvidence:
    return _query(obj="Helper__c", fields=[("Helper__c", "Reports_To__c")])


def test_f4_a_bullet_about_another_object_is_off_topic_although_it_names_a_used_field():
    """F4 (HP02, HP04, HR39, Q21, G23): the subject (the first span) is another object's field or another object, and
    the bullet places no used pair on, and names no, home object: naming the used field in passing makes it no more
    relevant. A lookup-target subject, a home object named ("distinct from `Helper__c`") or "linked to" keeps it."""
    drop = ["- `Audit_Note__c.Report_Manager__c` is a calculated field that snapshots the manager (derived from "
            "`Reports_To__c`, then `Boss__c`) for audit notes, not for reporting lines.",
            "- `Old_Tally__c` on the `Account` object is a legacy roll-up based on the older `Reports_To__c` field.",
            "- `Practice_Visit__c` is a different object for practice visits; its `Reports_To__c` field is copied from "
            "the visit, not the manager.",
            "- A caveat is that `Practice_Visit__c.Host__c` is mostly blank, so the manager must be found via "
            "`Reports_To__c`."]
    keep = ["- `Reports_To__c` is the lookup field on `Helper__c` that defines who an employee reports to.",
            "- `Helper__c` holds the employees.",
            "- `Practice_Visit__c` is a different object, distinct from `Helper__c` records",
            "- The host is a lookup to `Helper__c` on `Visit__c`"]
    for objects in (F4_NAMES, F4_CATALOG):
        rec = _rec(HELPER_ANSWER, evidence=[_reports()])
        used = merge._used_api(rec, objects)
        catalog = objects if isinstance(objects, dict) else {}
        for bullet in drop:
            assert merge._relevance(bullet, used) == 2 and merge._other_object_subject(bullet, used, catalog), bullet
            assert _added(bullet, _reports(), answer=HELPER_ANSWER, objects=objects) == [], bullet
        for bullet in keep:
            assert not merge._other_object_subject(bullet, used, catalog), bullet
            assert _added(bullet, _reports(), answer=HELPER_ANSWER, objects=objects) == [bullet], bullet
    # the Q23 shape: "linked to `Session__c`" names the home object (no lookup-target position)
    hosted = _query(obj="Session__c", fields=[("Session__c", "Host_User__c")])
    linked = "- `Practice_Visit__c` is linked to `Session__c` via `Host_User__c`"
    assert _added(linked, hosted, answer="6 sessions were hosted.\n\n" + SOURCE, objects=F4_NAMES) == [linked]


def test_f4_an_object_named_like_the_grouped_lookup_and_the_cap():
    """F4, the variant of G23 (an object named like the lookup the count grouped by) and a same-object bullet that
    stays relevant but loses its place at the cap."""
    slots = _query(tool="group_records", obj="Slot_Link__c", fields=[("Slot_Link__c", "Part__c")],
                   result={"grouped_by": "Part__c (Part name)", "measure": "count", "group_count": 2, "shown": 2,
                           "groups": [{"group": "Alpha", "value": 4}, {"group": "Beta", "value": 2}]})
    names = ("Slot_Link__c", "Template_Slot__c", "Template_Part__c", "Part__c")
    rec = _rec("Parts: Alpha 4, Beta 2.\n\n" + SOURCE, evidence=[slots])
    used = merge._used_api(rec, names)
    other = ("- `Template_Slot__c` records are deleted with their parent `Template_Part__c`; the part's name comes from "
             "the linked `Part__c`")
    own = "- `Part__c` on `Slot_Link__c` is the part a question is tagged to."
    assert merge._other_object_subject(other, used, {}) and not merge._other_object_subject(own, used, {})
    text = code_merge(QUESTION, other + "\n" + own, rec, STATUS, objects=names).text
    assert own in text and other not in text
    key = ("- `Helper_Key__c` = formula field filled only for first rounds; caveat: it lists values not in the current "
           "`Reports_To__c` lookup.")
    rec = _rec(HELPER_ANSWER, evidence=[_reports()])
    used = merge._used_api(rec, F4_NAMES)
    assert merge._relevance(key, used) == 2 and not merge._other_object_subject(key, used, {})   # same object
    first = ["- `Helper__c` holds the employees.",
             "- `Reports_To__c` is the lookup field on `Helper__c` that defines who an employee reports to.",
             "- `Reports_To__c` names the manager an employee reports to."]
    assert _added("\n".join(first + [key]), _reports(), answer=HELPER_ANSWER, objects=F4_NAMES) == first   # the cap


def test_f6_only_the_last_line_can_end_unfinished():
    """F6 (HR22, HR30): a token-limit cut can only end the lean reply, so a bullet with lines after it that ends with a
    particle ("moved on", "prepares for") is complete; the other cut-off tests apply to every bullet."""
    particles = ["- `Visit_Result__c` = picklist field (values: Advanced, Declined, Silent, NA) that automation uses to "
                 "decide if a guest moved on",
                 "- `Firm__c` = lookup to the client firm (`Firm__c`) for the real visit the practice prepares for"]
    for bullet in particles:
        assert merge._cut_off_bullet(bullet) and not merge._cut_off_bullet(bullet, last=False), bullet
    for bullet in ("- `Visit_Helper__c` names the helper of the", "- `Visit_Helper__c` is a lookup to `Helper__c` "
                   "records, which", "- `Visit_Date__c` is the day of the visit, from 2026-0"):
        assert merge._cut_off_bullet(bullet) and not merge._cut_off_bullet(bullet, last=False), bullet
    for bullet in ("- `Visit_Helper__c` is the helper (a lookup to `Helper__c` records",
                   "- `Visit_Status__c` is the **status** of the **visit"):
        assert merge._cut_off_bullet(bullet, last=False) and merge._cut_off_bullet(bullet), bullet
    result = _query(obj="Visit__c", fields=[("Visit__c", "Visit_Result__c")], result={"count": 12})
    answer = "12 visits (`Visit__c`) were decided (`Visit_Result__c`).\n\n" + SOURCE
    last = "- `Visit__c` holds the client visits."
    assert _added(particles[0] + "\n" + last, result, answer=answer) == [particles[0], last]   # mid-block: complete
    assert _added(last + "\n" + particles[0], result, answer=answer) == [last]   # the last line: still cut off
    # interplay: not cut off mid-block, but about another object's field (F4)
    practice = ("- `Practice_Visit__c` = practice visit record; `Firm__c` = lookup to the client firm (`Firm__c`) for "
                "the real visit the practice prepares for")
    firms = _query(obj="Visit__c", fields=[("Visit__c", "Firm__c")], result={"count": 12})
    by_firm = "12 visits (`Visit__c`) by firm (`Firm__c`).\n\n" + SOURCE
    assert _added(practice + "\n" + last, firms, answer=by_firm, objects=("Visit__c", "Practice_Visit__c")) == [last]


STEPS_ANSWER = "Zed Quill: June 2026: 31 completed steps; July 2026: 18 (based on Starts_At__c).\n\n" + SOURCE


def _steps() -> QueryEvidence:
    seg = lambda label, month, n: {"label": label, "count": n, "applied": {   # noqa: E731
        "filters": ["Trainer__c.Name = 'Zed Quill'", "Phase__c = 'Completed'"],
        "period": {"date_field": "Starts_At__c", "phrase": f"{month} 2026"}}}
    return _query(tool="compare_records", obj="Step__c",
                  fields=[("Step__c", "Trainer__c"), ("Step__c", "Phase__c"), ("Step__c", "Starts_At__c")],
                  result={"segments": [seg("June", "June", 31), seg("July", "July", 18)]})


def test_f7_at_the_cap_the_bullet_on_the_counting_querys_date_field_comes_first():
    """F7 (HP09): at the cap, a used-field bullet whose subject field (its first field span) is a date field of the
    counting queries goes first; the kept bullets are shown in their order. Naming the date field in passing does not
    promote a bullet."""
    obj = "- `Step__c` holds the training steps, one record per day of a program."
    trainer = "- The `Trainer__c` field is a lookup to `Helper__c` that names the step's trainer."
    done = "- The `Done_At__c` field is the time a step was finished."
    phase = "- The `Phase__c` picklist is the step's progress (`Not Started`, `Completed`)."
    starts = "- The `Starts_At__c` field is the scheduled start of the step, which places it in a month."
    names = ("Step__c", "Helper__c")
    assert merge._date_fields(_rec(STEPS_ANSWER, evidence=[_steps()])) == {"starts_at__c"}
    added = _added("\n".join([obj, trainer, done, phase, starts]), _steps(), answer=STEPS_ANSWER, objects=names)
    assert added == [obj, trainer, starts]   # Phase__c left out at the cap, Done_At__c (not used) never kept
    older = "- The `Begin_Time__c` field is an older time field; `Starts_At__c` gives the date."
    added = _added("\n".join([obj, trainer, phase, older, starts]), _steps(), answer=STEPS_ANSWER, objects=names)
    assert added == [obj, trainer, starts] and older not in added
    # 3 or fewer relevant bullets: as before
    assert _added("\n".join([obj, trainer, starts]), _steps(), answer=STEPS_ANSWER, objects=names) == [obj, trainer,
                                                                                                         starts]


VISIT_GROUP = {"grouped_by": "Visit_Helper__c (Helper name)", "measure": "count", "group_count": 14, "shown": 5,
               "total_across_groups": 256, "blank_group_value": 96,
               "groups": [{"group": "Zed Quill", "value": 40}, {"group": "Marlo Fenn", "value": 31}]}
VISIT_PERIOD = {"date_field": "Visit_Date__c", "phrase": "May 2026", "from": "2026-05-01", "to": "2026-05-31",
                "time_zone": "America/New_York"}
TOP_VISITS = "Top helpers by visits in May 2026:\n1. Zed Quill: 40\n2. Marlo Fenn: 31\n\n" + SOURCE


def _group(obj="Visit__c", result=None, applied=None, notes=(), tool="group_records") -> QueryEvidence:
    e = _query(tool=tool, obj=obj, applied=applied, result=result)
    e.notes = list(notes)
    return e


def _lines(m) -> list[str]:
    return [ln for ln in m.text.split("\n") if ln.startswith(("Counted:", "Summed "))]


def test_f8_one_code_line_says_what_the_counting_query_counted():
    """F8 (P44, HR33, HR12, HR14, Q17): when the record answer leaves out the grouped field, the blank group the
    ranking left out, a not-blank filter or a list's total, one code line from the one counting query says it (the
    words: prompts/code_merge_counted.md); every value comes from the result, so the number audit passes."""
    left_out = "the (blank) group is left out of the ranking"
    e = _group(result=VISIT_GROUP, applied={"period": VISIT_PERIOD}, notes=[left_out])
    m = code_merge(QUESTION, B_INT, _rec(TOP_VISITS, evidence=[e]), STATUS)
    # the period by its first and last day (review of 2026-10-30: the period's own words can read "since ...")
    assert _lines(m) == ["Counted: `Visit__c` records grouped by `Visit_Helper__c`, with `Visit_Date__c` from 2026-05-01 "
                         "to 2026-05-31 (America/New_York); the blank group (96) is left out of the ranking."]
    assert audit_numbers(_lines(m)[0], [facts_json([e], 10**9)]) == []
    bills = _group(obj="Bill__c", applied={"filters": ["Bill_Status__c is not blank"]},
                   result={"grouped_by": "Bill_Status__c", "measure": "sum", "measure_field": "Bill_Amount__c",
                           "groups": [{"group": "Paid", "value": 1200}, {"group": "Open", "value": 300}],
                           "group_count": 2, "shown": 2})
    answer = "Paid: 1200 by `Bill_Status__c`; Open: 300.\n\n" + SOURCE
    assert _lines(code_merge(QUESTION, B_INT, _rec(answer, evidence=[bills]), STATUS)) == [
        "Summed `Bill_Amount__c` over `Bill__c` records grouped by `Bill_Status__c`, where Bill_Status__c is not blank."]
    listed = _group(obj="Practice_Visit__c", tool="list_records", applied={"filters": ["Host__c.Name = 'Zed Quill'"]},
                    result={"columns": ["Name"], "rows": [{"Name": "PV-1"}], "returned": 7, "total_count": 7})
    rows = "Zed Quill's practice visits:\n- PV-1\n\n" + SOURCE
    assert _lines(code_merge(QUESTION, B_INT, _rec(rows, evidence=[listed]), STATUS)) == [
        "Counted: 7 `Practice_Visit__c` records, where Host__c.Name = 'Zed Quill'."]
    rounds = _group(result={"grouped_by": "Round__c", "measure": "count", "group_count": 2, "shown": 2,
                            "groups": [{"group": "First", "value": 40}, {"group": "Second", "value": 17}]})
    split = "First: 40, Second: 17.\n\n" + SOURCE
    assert _lines(code_merge(QUESTION, B_INT, _rec(split, evidence=[rounds]), STATUS)) == [
        "Counted: `Visit__c` records grouped by `Round__c`."]
    path = _group(obj="Step__c", result={"grouped_by": "Step_Plan__c.Program__c (Program name)", "measure": "count",
                                         "group_count": 2, "shown": 2,
                                         "groups": [{"group": "AI", "value": 40}, {"group": "QA", "value": 17}]})
    assert _lines(code_merge(QUESTION, B_INT, _rec(split, evidence=[path]), STATUS)) == [
        "Counted: `Step__c` records grouped by `Step_Plan__c.Program__c`."]
    named = "First: 40 and QA: 17 by `Program__c`.\n\n" + SOURCE   # the path's last name: no line for the field
    assert _lines(code_merge(QUESTION, B_INT, _rec(named, evidence=[path]), STATUS)) == []


def test_f8_no_counted_line_when_the_answer_says_it_or_the_query_does_not_fit(monkeypatch):
    left_out = "the (blank) group is left out of the ranking"
    e = _group(result=VISIT_GROUP, applied={"period": VISIT_PERIOD}, notes=[left_out])
    said = ("Top helpers (`Visit_Helper__c`) in May 2026; 96 visits have no helper:\n1. Zed Quill: 40\n2. Marlo Fenn: "
            "31\n\n" + SOURCE)
    assert _lines(code_merge(QUESTION, B_INT, _rec(said, evidence=[e]), STATUS)) == []   # the answer states all of it
    variants = [
        [e, _query("q2", obj="Visit__c")],                                              # two counting queries
        [_query(obj="Visit__c", result={"count": 12})],                                 # count_records
        [_group(result={**VISIT_GROUP, "measure": "count_distinct"})],                  # another measure
        [_group(result={**VISIT_GROUP, "measure": "avg", "measure_field": "Score__c"})],
        [_group(result=VISIT_GROUP, applied={"any_of": ["Visit_Status__c = 'Done'"]})],  # any_of / related
        [_group(result=VISIT_GROUP, applied={"related": ["has Visit_Guest__c"]})],
    ]
    for evidence in variants:
        assert _lines(code_merge(QUESTION, B_INT, _rec(TOP_VISITS, evidence=evidence), STATUS)) == [], evidence
    hinglish = "May 2026 mein top helpers ke visits hue:\n1. Zed Quill: 40\n2. Marlo Fenn: 31\n\n" + SOURCE
    assert _lines(code_merge(QUESTION, B_INT, _rec(hinglish, evidence=[e]), STATUS)) == []   # F11 first
    assert _lines(code_merge(QUESTION, B_INT, _rec(TOP_VISITS, evidence=[e]), STATUS))   # (the line, as a control)
    monkeypatch.setattr(merge, "_no_data_but_not_found", lambda *a, **k: True)   # the answer says it is not found
    assert _lines(code_merge(QUESTION, B_INT, _rec(TOP_VISITS, evidence=[e]), STATUS)) == []


def test_f8_the_counted_line_alone_and_with_bullets_and_in_the_pipeline_reads(monkeypatch):
    """The counted line without a kept bullet: merged False, the line in the text (kind records), and code_merge_lines
    lists it. With bullets after a list: line, blank line, label line, bullets, Source line. pipeline.answer reads the
    lookup footer and the status lines without it (inserted)."""
    left_out = "the (blank) group is left out of the ranking"
    e = _group(result=VISIT_GROUP, applied={"period": VISIT_PERIOD}, notes=[left_out])
    m = code_merge(QUESTION, B_OTHER, _rec(TOP_VISITS, evidence=[e]), STATUS)
    line = _lines(m)[0]
    assert not m.merged and m.error is None and line in m.text and merge.code_merge_lines(m.text, TOP_VISITS) == [line]
    m = code_merge(QUESTION, "- `Visit__c` holds the client visits.", _rec(TOP_VISITS, evidence=[e]), STATUS)
    assert m.merged and m.text == TOP_VISITS.replace("\n\n" + SOURCE, "\n\n" + line + "\n\n" + merge.CODE_MERGE_LABEL
                                                     + "\n- `Visit__c` holds the client visits.\n\n" + SOURCE)
    # pipeline.answer: the lookup footer reads the answer without the counted line, which names the lookup
    monkeypatch.setenv("SFA_MERGE", "code")

    class GroupTools(StubTools):   # the count is a group_records by the team lead
        def call(self, name, arguments=None):
            out = super().call(name, arguments)
            self._evidence[-1].tool = "group_records"
            return out

    record = "6 employees have Zed Quill as their team lead:\n- Zed Quill: 6\n\n" + SOURCE
    group = {"grouped_by": "Team_Lead__c (Employee name)", "measure": "count", "group_count": 1, "shown": 1,
             "groups": [{"group": "Zed Quill", "value": 6}]}
    o, row, _s = _answer(Model(facts=B_OTHER, record=record), GroupTools(lookup_check=LC_LEAD, **group))
    line = "Counted: `Recruiter__c` records grouped by `Team_Lead__c`, where Department__c = 'Zorvex'."
    footer = with_lookup_footer(record, o.rec.evidence).split("\n")[-2]
    assert footer.startswith("Lookup used: `Recruiter__c.Team_Lead__c`")
    assert o.merge_by == "code" and o.kind == "records" and not o.m.merged and row["jsonl"]["merge_error"] is None
    assert o.final == record.replace("\n\n" + SOURCE, "\n\n" + line + "\n\n" + footer + "\n" + SOURCE)
    assert with_lookup_footer(o.m.text, o.rec.evidence) == o.m.text   # read with the line, the footer would go


def test_f10_a_wrong_value_meaning_and_claims_the_results_contradict():
    """F10 (HP04, HR41, Q31, P25): a value meaning that shares no content word with the knowledge meaning; "three
    values" or "largely unused" while a result shows more; a caveat the lookup check resolves (every other lookup holds
    the same records); a working-notes tail."""
    cat = {"Visit__c": _spec("Visit__c", [_fs("Visit_Result__c", "picklist", picklist=[
        ("Silent", "Silent", "The host never came back with a result"), ("Advanced", "Advanced", "Moved on")])])}
    decided = _query(obj="Visit__c", fields=[("Visit__c", "Visit_Result__c")], result={"count": 12})
    answer = "12 visits (`Visit__c`) are silent (`Visit_Result__c`).\n\n" + SOURCE
    wrong = ("- `Visit_Result__c` is the picklist for the visit result, where the value `Silent` indicates the guest did "
             "not show up")
    right = "- `Visit_Result__c` value `Silent` means the host never sent a result"
    assert _added(wrong, decided, answer=answer, objects=cat) == []
    assert _added(right, decided, answer=answer, objects=cat) == [right]
    assert _added(wrong, decided, answer=answer, objects=tuple(cat)) == [wrong]   # no catalog: no verdict
    # (the group tool's total_across_groups and no blank group: every ticket has a kind; review of 2026-10-30, a
    # "largely unused" claim is judged by the blank group)
    tickets = _query(tool="group_records", obj="Ticket__c", fields=[("Ticket__c", "Kind__c"), ("Ticket__c", "Urgency__c")],
                     result={"grouped_by": "Kind__c", "split_by": "Urgency__c", "measure": "count", "group_count": 9,
                             "shown": 2, "groups": [{"group": "Support", "value": 40}, {"group": "Billing", "value": 31}],
                             "total_across_groups": 85,
                             "split_totals": {"High": 40, "Medium": 31, "Critical": 12, "Low": 2}})
    listed = "Tickets by kind (`Kind__c`) and urgency (`Urgency__c`):\n- Support: 40\n- Billing: 31\n\n" + SOURCE
    for bullet in ("- `Urgency__c` is a picklist with three values: High, Medium and Low",
                   "- `Kind__c` is largely unused; the only documented value is 'Support'"):
        assert _added(bullet, tickets, answer=listed) == [], bullet
    for bullet in ("- `Kind__c` is the kind of ticket.", "- `Kind__c` is mostly filled by the intake flow."):
        assert _added(bullet, tickets, answer=listed)[1:] == [bullet], bullet   # after the label line
    lc = {"object": "Doc__c", "field": "Uploaded_By__c", "target": "Helper__c", "role": "uploader", "records": 3,
          "filter": "Uploaded_By__c.Name = 'Zed Quill'", "person_kind": "employee",
          "others": [{"field": "CreatedById", "role": "record creator", "records": 3},
                     {"field": "LastModifiedById", "records": 3}, {"field": "OwnerId", "records": 3}]}
    caveats = ["- `Uploaded_By__c` is only set for practice bookings and is blank for standard uploads.",
               "- For standard uploads, the creator is recorded in the standard `CreatedById` field (lookup to "
               "`User`), not in `Uploaded_By__c`."]
    docs = "3 documents (`Doc__c`) were uploaded by Zed Quill (`Uploaded_By__c`).\n\n" + SOURCE

    def doc_check(**changes):
        x = {**lc, **{k: v for k, v in changes.items() if k != "others"}}
        x["others"] = changes.get("others", lc["others"])
        return _query(obj="Doc__c", fields=[("Doc__c", "Uploaded_By__c")], result={"count": 3, "lookup_check": [x]})
    for bullet in caveats:
        assert merge._relevance(bullet, merge._used_api(_rec(docs, evidence=[doc_check()]))) == 2, bullet
        assert _added(bullet, doc_check(), answer=docs) == [], bullet
        five = [dict(o, records=5) if o["field"] == "OwnerId" else o for o in lc["others"]]
        assert _added(bullet, doc_check(others=five), answer=docs) == [bullet], bullet
        zeros = [dict(o, records=0) for o in lc["others"]]
        assert _added(bullet, doc_check(records=18, others=zeros),
                      answer=docs.replace("3 documents", "18 documents")) == [bullet], bullet
    tails = ["- `Visit_Helper__c` is the lookup to `Helper__c` that identifies the employee supporting the guest, fitting "
             "the 'supported' verb.",
             "- `Visit__c` has a lookup field `Visit_Helper__c` (implied by 'guest-scoped') linking the visit to the "
             "helper"]
    for bullet in tails:
        assert _added(bullet, _visits(), answer=VISIT_ANSWER) == [], bullet
        clean = bullet.replace(", fitting the 'supported' verb", "").replace(" (implied by 'guest-scoped')", "")
        assert _added(clean, _visits(), answer=VISIT_ANSWER) == [clean], clean


HINGLISH = ("Q2 2026 mein **12 alag-alag helpers** ke visits hue.\n\n- Object: `Visit__c`\n- Count: 12 distinct "
            "`Visit_Helper__c` values\n\n" + SOURCE)


def test_f11_nothing_is_added_to_a_hinglish_record_answer():
    """F11 (HP01, HP05): the record answer's language decides (the lean facts block is English and code cannot
    translate it); a Hinglish question with an English record answer, or an English answer that quotes one Hindi
    word, keeps its bullet."""
    fact = "- `Visit_Helper__c` on `Visit__c` is the lookup to `Helper__c` that names the helper."
    m = code_merge(QUESTION, fact, _rec(HINGLISH, evidence=[_visits()]), STATUS)
    assert (m.text, m.merged, m.error) == (HINGLISH, False, None)
    english = "12 different visits were supported in Q2 2026 (`Visit__c`, distinct `Visit_Helper__c`).\n\n" + SOURCE
    assert _added(fact, _visits(), answer=english) == [fact]
    assert _added(fact, _visits(), answer=english, question="Q2 2026 mein kitne visits hue?") == [fact]
    quoted = ("12 visits (`Visit__c`) are marked 'bahut accha' (`Visit_Helper__c`, value `kaam hai`).\n\n" + SOURCE)
    assert not merge._hinglish(quoted) and _added(fact, _visits(), answer=quoted) == [fact]
    assert merge._hinglish(HINGLISH) and not merge._hinglish(english)


THINKING = [
    "I have the answer. 6 employees (`Recruiter__c`) are in the department Zorvex (`Department__c`).\n\n" + SOURCE,
    "It seems 6 employees (`Recruiter__c`) are in the department Zorvex (`Department__c`).\n\n" + SOURCE,
    "There are 6 employees in Zorvex.\n\nNow I know:\n- Employees in Zorvex: 6\n\n**6 employees are in Zorvex.**\n\n"
    + SOURCE,
    "Main, ab tak ke analysis se ye samajh aata hai ki team Zorvex mein 6 employees hain.\n\nLekin main ye confirm "
    "nahi kar sakta ki ye sab active hain.\n\n" + SOURCE,
]
TOOL_TALK = ("I cannot answer this question because I did not have enough tool calls. To answer this question, I would "
             "need to:\n- Use `group_records` on `Recruiter__c` with a filter on `Department__c`.\n\n" + SOURCE)


def test_f5_thinking_aloud_and_tool_talk_go_to_the_merge_call(monkeypatch):
    """F5 (HR16, P43, HR25, HR08, HR29): plans_aloud also reads thinking aloud ("I have the answer.", "It seems ...",
    "Now I know:"), working notes and tool talk, and Hinglish first-person planning; pipeline.answer then makes the
    merge call. An ordinary answer, "Let me know ...", an ordinary Hinglish answer and "Here's the breakdown:" keep
    the merge by code."""
    for record in THINKING + [TOOL_TALK]:
        assert merge.plans_aloud(record), record
    monkeypatch.setenv("SFA_MERGE", "code")
    for record in THINKING + [TOOL_TALK]:
        model = Model(record=record)
        o, row, _s = _answer(model)
        assert o.rec.answer == record and o.merge_by == "model" and len(model.merge_requests()) == 1, record
    hinglish = "Team Zorvex mein **6 alag-alag employees** hain, sab `Recruiter__c` ke records.\n\n" + SOURCE
    plain = [RECORD_ANSWER.replace("\n\n", "\n\nLet me know if you need more.\n\n"), hinglish]
    for record in plain:
        model = Model(record=record)
        o, row, _s = _answer(model)
        assert not merge.plans_aloud(record) and o.merge_by == "code" and model.merge_requests() == [], record
    assert o.kind == "records" and o.final == hinglish   # F11: no English bullet after the Hinglish answer
    breakdown = "Here's the breakdown:\n- Active: 4\n- Former: 2\n\n" + SOURCE
    assert not merge.plans_aloud(breakdown) and not merge.plans_aloud("It is filled on most rows.\n" + SOURCE)


# ---------------------------------------------------- the review of the step-2 fixes (2026-10-30, docs/runbook.md)
# Fake shapes of the review's findings: claims about people or rules that are no field claims, lookup paths, child
# objects, fill-rate claims, a code-written people answer, open periods, the counted line next to the bullets, Hinglish
# list lines, list rows and business words that read like thinking aloud, the trace line, and the branches the build's
# tests left unpinned.
STATUS_CATALOG = {
    "Helper__c": _spec("Helper__c", [_fs("Employment_Status__c", "picklist"), _fs("Is_Active__c", "checkbox"),
                                     _fs("Offer_Date__c", "date")]),
    "Visit__c": _spec("Visit__c", [_fs("Firm_Ref__c", "reference", ["Firm__c"]), _fs("Firm_Text__c", "text")],
                      marks={"Firm_Text__c": {"legacy": True, "replaced_by": "Firm_Ref__c"}}),
    "Firm__c": _spec("Firm__c", [_fs("Name", "name")]),
}
STAFF_ANSWER = "12 employees (`Helper__c`) are active (`Employment_Status__c`).\n\n" + SOURCE


def _staff() -> QueryEvidence:
    return _query(obj="Helper__c", fields=[("Helper__c", "Employment_Status__c"), ("Helper__c", "Is_Active__c"),
                                           ("Helper__c", "Offer_Date__c")],
                  applied={"filters": ["Employment_Status__c = 'Active Employee'"]}, result={"count": 12})


def test_f3_a_word_about_people_or_a_rule_is_no_legacy_claim_about_the_field():
    """B3 and Q1 of the review: "inactive" about people ("is active or inactive", "inactive employees", "a person is
    inactive") or a rule ("this rule is inactive") claims nothing about the field, so F3 keeps these correct bullets; a
    field's span right after "the legacy free-text" is the field the claim is about. A predicate about the field (a
    parenthetical aside between) is still a claim."""
    keep = ["- `Employment_Status__c` tells whether an employee is active or inactive.",
            "- `Employment_Status__c` is set to Inactive when the employee leaves the company.",
            "- `Is_Active__c` is false for inactive employees.",
            "- `Is_Active__c` is the checkbox that is cleared once a person is inactive.",
            "- `Employment_Status__c` has two values (`Active Employee`, `Former Employee`), and former employees are "
            "inactive.",
            "- `Offer_Date__c` (date on `Helper__c`) = the date on the offer, required by validation rule "
            "`Offer_Needs_Date` when status is \"Offered\" (though this rule is inactive in production)"]
    rec = _rec(STAFF_ANSWER, evidence=[_staff()])
    used = merge._used_api(rec, STATUS_CATALOG)
    for bullet in keep:
        spans = merge._spans(bullet, used)
        assert merge._legacy_claims(bullet, spans, merge._clauses(bullet)) == [], bullet
        assert merge._contradiction(bullet, used, rec, STATUS_CATALOG) == "", bullet
    assert _added("\n".join(keep[:4]), _staff(), answer=STAFF_ANSWER, objects=STATUS_CATALOG) == keep[:3]   # the cap
    for bullet in ("- `Employment_Status__c` (text) is legacy.", "- `Is_Active__c`, now deprecated, held the flag."):
        assert merge._contradiction(bullet, used, rec, STATUS_CATALOG) == (
            f"the knowledge does not mark {bullet.split('`')[1]} legacy"), bullet
        assert _added(bullet, _staff(), answer=STAFF_ANSWER, objects=STATUS_CATALOG) == [], bullet
    # the Q17 shape: the current lookup replaces "the legacy free-text `Firm_Text__c` field" (marked legacy)
    firms = _query(tool="group_records", obj="Visit__c", fields=[("Visit__c", "Firm_Ref__c")],
                   result={"grouped_by": "Firm_Ref__c (Firm name)", "measure": "count", "group_count": 2, "shown": 2,
                           "groups": [{"group": "Alpha", "value": 4}, {"group": "Beta", "value": 2}]})
    by_firm = "Visits by firm (`Firm_Ref__c`): Alpha 4, Beta 2.\n\n" + SOURCE
    replacing = ("- The field `Firm_Ref__c` is a lookup to `Firm__c` records, replacing the legacy free-text "
                 "`Firm_Text__c` field.")
    rec = _rec(by_firm, evidence=[firms])
    used = merge._used_api(rec, STATUS_CATALOG)
    claims = merge._legacy_claims(replacing, merge._spans(replacing, used), merge._clauses(replacing))
    assert [s.name for s in claims] == ["Firm_Text__c"] and merge._contradiction(replacing, used, rec,
                                                                                 STATUS_CATALOG) == ""


def test_value_level_notes_and_a_value_placed_of_an_object_make_no_claim():
    """TP4 of the review (branches the build's tests left unpinned): "this value is now inactive", "which is an
    inactive value" and "the inactive picklist `F` value" are about a value; "`Done` of `Visit__c`" (a value in
    backticks) is placed nowhere, so it is no field the object lacks (F2 b)."""
    cat = _legacy_catalog()
    visits = _query(obj="Visit__c", fields=[("Visit__c", "Visit_Status__c")], result={"count": 12})
    answer = "12 visits (`Visit__c`) are done.\n\n" + SOURCE
    for bullet in ("- `Visit_Status__c` used to hold the value `Silent`, but this value is now inactive.",
                   "- `Visit_Status__c` once held the value `Silent` as an inactive picklist option.",
                   "- `Visit_Status__c` keeps `Silent`, which is an inactive value.",
                   "- Visits with the inactive `Visit_Status__c` value are the older ones.",
                   "- Visits with the inactive picklist `Visit_Status__c` value are the older ones."):
        assert _added(bullet, visits, answer=answer, objects=cat) == [bullet], bullet
    placed = "- `Visit_Status__c` value `Done` of `Visit__c` marks a finished visit."
    assert _added(placed, _visits(), answer=VISIT_ANSWER, objects=VISIT_CATALOG) == [placed]


PATH_CATALOG = {
    "Visit__c": _spec("Visit__c", [_fs("Name", "name"), _fs("Visit_Helper__c", "reference", ["Helper__c"])], rows=50),
    "Helper__c": _spec("Helper__c", [_fs("Name", "name"), _fs("Team__c", "reference", ["Team__c"]),
                                     _fs("Old_Grade__c", "picklist", filled=3)], rows=40,
                       marks={"Old_Grade__c": {"legacy": True}}),
    "Team__c": _spec("Team__c", [_fs("Name", "name")], rows=5),
    "Visit_Guest__c": _spec("Visit_Guest__c", [_fs("Name", "name"), _fs("Visit__c", "reference", ["Visit__c"])]),
}


def _by_path(path: str, last: str) -> QueryEvidence:
    return _query(tool="group_records", obj="Visit__c", fields=[("Visit__c", "Visit_Helper__c"), ("Helper__c", last)],
                  result={"grouped_by": path, "measure": "count", "group_count": 2, "shown": 2,
                          "groups": [{"group": "Alpha", "value": 4}, {"group": "Beta", "value": 2}]})


def test_a_lookup_path_gets_no_verdict_and_a_child_object_is_no_missing_field():
    """B6 and B7 of the review: a claim after a lookup path (its first name no object) is about the path's last field,
    so the claim checks give no verdict (the path's first hop is no lookup to `Team__c`, nor marked legacy); a child
    object placed "of"/"on" its parent ("Each `Visit_Guest__c` of the `Visit__c`") is no field the parent lacks, while a
    real wrong placement still is."""
    for path, last, bullet in (
            ("Visit_Helper__r.Team__c", "Team__c", "- `Visit_Helper__r.Team__c` is the helper's team, a lookup to `Team__c`."),
            ("Visit_Helper__r.Old_Grade__c", "Old_Grade__c",
             "- `Visit_Helper__r.Old_Grade__c` is a legacy picklist that older helpers still carry.")):
        answer = f"Visits by the helper's {last[:-3].lower()} (`{path}`): Alpha 4, Beta 2.\n\n" + SOURCE
        rec = _rec(answer, evidence=[_by_path(path, last)])
        used = merge._used_api(rec, PATH_CATALOG)
        assert merge._spans(bullet, used)[0].kind == "path" and merge._relevance(bullet, used) == 2, bullet
        assert merge._contradiction(bullet, used, rec, PATH_CATALOG) == "", bullet
        assert _added(bullet, _by_path(path, last), answer=answer, objects=PATH_CATALOG) == [bullet], bullet
    done = _query(obj="Visit__c", fields=[("Visit__c", "Name")], applied={"filters": ["Name = 'V-1'"]})
    answer = "1 visit (`Visit__c`) is named V-1.\n\n" + SOURCE
    rec = _rec(answer, evidence=[done])
    used = merge._used_api(rec, PATH_CATALOG)
    for bullet in ("- Each `Visit_Guest__c` of the `Visit__c` is one guest of that visit.",
                   "- `Visit_Guest__c` on `Visit__c` lists its guests."):
        assert merge._contradiction(bullet, used, rec, PATH_CATALOG) == "", bullet
    assert merge._contradiction("- `Owner_Note__c` on `Visit__c` is the visit's note.", used, rec, PATH_CATALOG) == (
        "Visit__c has no field Owner_Note__c")


def _tickets(blank: int | None, **extra) -> QueryEvidence:
    groups = [{"group": "Support", "value": 3}, {"group": "Billing", "value": 2}]
    result = {"grouped_by": "Kind__c", "measure": "count", "group_count": 2, "shown": 2 + (blank is not None),
              "groups": groups + ([{"group": "(blank)", "value": blank}] if blank is not None else []),
              "total_across_groups": 5 + (blank or 0), **({"blank_group_value": blank} if blank is not None else {})}
    return _query(tool="group_records", obj="Ticket__c", fields=[("Ticket__c", "Kind__c")], result=result, **extra)


def test_f10b_a_fill_rate_claim_is_judged_by_the_blank_group_and_n_active_values_are_no_count():
    """B2, Q4 and B8 of the review: "largely unused", "rarely filled" are about how often the field is filled, which a
    large blank group confirms (kept) and a filled majority contradicts (left out); a bare "unused" still goes with 2
    values; a result filtered on the field says nothing about its fill, and neither does the snapshot (the claim may be
    about some of the records): no verdict; "three active values" is no count of the values the records hold (they
    keep retired ones)."""
    listed = "Tickets by kind (`Kind__c`):\n- Support: 3\n- Billing: 2\n\n" + SOURCE
    hedged = ["- `Kind__c` is largely unused: most tickets leave it blank.",
              "- `Kind__c` is rarely filled; most tickets have no kind."]
    for bullet in hedged:
        assert _added(bullet, _tickets(950), answer=listed)[1:] == [bullet], bullet   # 5 filled, 950 blank: kept
        assert _added(bullet, _tickets(None), answer=listed) == [], bullet            # no blank group: dropped
        assert _added(bullet, _tickets(5), answer=listed) == [], bullet               # half filled: dropped
    bare = "- `Kind__c` is unused in practice; tickets carry no kind."
    assert _added(bare, _tickets(950), answer=listed) == []
    # a result filtered on the field: no verdict, whatever the snapshot fills (900 of 955 rows here)
    full = {"Ticket__c": _spec("Ticket__c", [_fs("Kind__c", "picklist", filled=900)], rows=955)}
    for applied in ({"filters": ["Kind__c is not blank"]}, {"filters": ["Kind__c.Name = 'Support'"]}):
        filtered = _tickets(None, applied=applied)
        assert hedged[0] in _added(hedged[0], filtered, answer=listed, objects=full), applied
        assert hedged[0] in _added(hedged[0], filtered, answer=listed), applied
    five = _query(tool="group_records", obj="Ticket__c", fields=[("Ticket__c", "Kind__c")],
                  result={"grouped_by": "Kind__c", "measure": "count", "group_count": 5, "shown": 5, "groups": [
                      {"group": k, "value": 2} for k in ("A", "B", "C", "D", "E")]})
    active = "- `Kind__c` is a picklist with three active values; older tickets keep two retired kinds."
    assert _added(active, five, answer=listed)[1:] == [active]
    assert _added(active.replace("three active", "three"), five, answer=listed) == []


def test_f8_the_counted_line_reads_the_kept_bullets_and_never_follows_a_code_written_people_answer():
    """Q3 and B4 of the review: a kept bullet that names the grouped field says it, when the record answer or the
    bullets name every filter's field too (else the line is the only place that names the filter's lookup); a people
    answer code wrote from about_people states no count, so no counted line follows it."""
    by_status = _query(tool="group_records", obj="Visit__c",
                       fields=[("Visit__c", "Visit_Status__c"), ("Visit__c", "Visit_Helper__c")],
                       result={"grouped_by": "Visit_Status__c", "measure": "count", "group_count": 2, "shown": 2,
                               "groups": [{"group": "Done", "value": 40}, {"group": "Open", "value": 17}]},
                       applied={"filters": ["Visit_Helper__c.Name = 'Zed Quill'"]})
    answer = "Zed Quill's visits: Done 40, Open 17.\n\n" + SOURCE
    status_bullet = "- `Visit_Status__c` is the visit's lifecycle status."
    helper_bullet = "- `Visit_Helper__c` names the employee who supports the visit."
    line = "Counted: `Visit__c` records grouped by `Visit_Status__c`, where Visit_Helper__c.Name = 'Zed Quill'."
    assert _lines(code_merge(QUESTION, B_INT, _rec(answer, evidence=[by_status]), STATUS)) == [line]
    m = code_merge(QUESTION, status_bullet, _rec(answer, evidence=[by_status]), STATUS)
    assert m.merged and status_bullet in m.text and _lines(m) == [line]   # the filter's lookup is named nowhere else
    both = status_bullet + "\n" + helper_bullet
    assert _lines(code_merge(QUESTION, both, _rec(answer, evidence=[by_status]), STATUS)) == []
    m = code_merge(QUESTION, both, _rec(answer, evidence=[by_status]), STATUS)
    assert m.merged and status_bullet in m.text and helper_bullet in m.text
    people = "Zed Quill is an employee.\n\n" + SOURCE
    rec = _rec(people, evidence=[by_status], code_answer=True)
    m = code_merge(QUESTION, B_INT, rec, STATUS)
    assert _lines(m) == [] and m.error is None
    assert _lines(code_merge(QUESTION, B_INT, _rec(people, evidence=[by_status]), STATUS)) == [line]   # agent-written


def test_f8_an_open_or_incomplete_period_reads_by_its_days():
    """B5 and Q5 of the review: the period part names its first and last day ("since <date>" reads "on or after",
    "before <date>" "up to"), never the agent's own words after "in"; a period without a date field, a time zone or a
    day gives no line, and never an error that would lose every bullet (TP4 e)."""
    left_out = "the (blank) group is left out of the ranking"
    base = "Counted: `Visit__c` records grouped by `Visit_Helper__c`, with `Visit_Date__c` "
    for period, part in (
            ({"phrase": "since 2026-01-01", "from": "2026-01-01", "to": None}, "on or after 2026-01-01"),
            ({"phrase": "upcoming", "from": "2026-10-13", "to": None}, "on or after 2026-10-13"),
            ({"phrase": "before 2026-06-01", "from": None, "to": "2026-05-31"}, "up to 2026-05-31"),
            ({"phrase": "last week", "from": "2026-10-05", "to": "2026-10-11"}, "from 2026-10-05 to 2026-10-11")):
        applied = {"period": {"date_field": "Visit_Date__c", "time_zone": "America/New_York", **period}}
        e = _group(result=VISIT_GROUP, applied=applied, notes=[left_out])
        m = code_merge(QUESTION, "- `Visit__c` holds the client visits.", _rec(TOP_VISITS, evidence=[e]), STATUS)
        assert _lines(m) == [base + part + " (America/New_York); the blank group (96) is left out of the ranking."]
        assert m.merged and m.error is None and period["phrase"] not in _lines(m)[0]
    for drop in ("time_zone", "date_field", "from"):
        period = {k: v for k, v in VISIT_PERIOD.items() if k != drop}
        if drop == "from":
            period["to"] = None
        e = _group(result=VISIT_GROUP, applied={"period": period}, notes=[left_out])
        m = code_merge(QUESTION, "- `Visit__c` holds the client visits.", _rec(TOP_VISITS, evidence=[e]), STATUS)
        assert m.error is None and m.merged and _lines(m) == [], drop


def test_f8_the_group_tools_note_is_the_one_the_counted_line_reads():
    """SPS-6 of the review: the counted line's blank-group trigger matches the group tool's own note text, so a
    rewording of that note in tools/records.py cannot switch the trigger off unseen."""
    import inspect

    import sf_assistant.tools.records as records

    assert f'"{merge._BLANK_LEFT_OUT}"' in inspect.getsource(records)


def test_f11_main_and_hinglish_list_lines_and_the_code_rows_aside():
    """Q6 and TP4 of the review: "main" (Hinglish for "in") counts as a Hindi word, a short Hinglish lead over Hinglish
    list lines is Hinglish, an English answer over its list stays English, and the test reads the prose without the
    code rows (``table_blocks``), so English labels and prose of a code block do not dilute a Hinglish lead."""
    fact = "- `Visit_Helper__c` on `Visit__c` is the lookup to `Helper__c` that names the helper."
    main = "Q2 2026 main **44 alag-alag helpers** ke visits hue.\n\n- Object: `Visit__c`\n- Count: 44\n\n" + SOURCE
    assert merge._hinglish(main)
    m = code_merge(QUESTION, fact, _rec(main, evidence=[_visits()]), STATUS)
    assert (m.text, m.merged, m.error) == (main, False, None)
    short = ("Team Zorvex ke 6 log:\n- Zed Quill ke 4 visits hue\n- Marlo Fenn ke 2 visits hue, sab done hai\n\n"
             + SOURCE)
    assert not merge._hinglish(short.split("\n- ")[0]) and merge._hinglish(short)
    english = ("12 visits were supported in Q2 2026 (`Visit__c`).\n- Zed Quill: 7\n- Marlo Fenn: 5\n- Main office: 0\n\n"
               + SOURCE)
    assert not merge._hinglish(english)
    lead = "Q2 2026 mein **12 alag-alag helpers** ke visits hue."
    block = ("Helpers:\n- Zed Quill: 7\n- Marlo Fenn: 5\n\nShowing the helpers of the visits of the whole copy, sorted "
             "by the number of visits of each helper, with the largest helper first and the smaller ones after it, so "
             "that the biggest groups of the visits come before the small ones in this list of helpers and their "
             "visits.")
    answer = lead + "\n\n" + block + "\n\n" + SOURCE
    assert merge._hinglish(answer, {"{{table q1}}": block}) and not merge._hinglish(answer)
    rec = _rec(answer, evidence=[_visits()], table_blocks={"{{table q1}}": block})
    m = code_merge(QUESTION, fact, rec, STATUS)
    assert (m.text, m.merged, m.error) == (answer, False, None)


def test_f4_an_object_named_like_a_home_field_is_no_other_object_subject():
    """TP4 of the review, F4 (b) with a catalog: a subject object whose name is also a field of a home object (a lookup
    named after its object) is not another object's subject."""
    cat = {"Visit__c": _spec("Visit__c", [_fs("Visit_Helper__c", "reference", ["Helper__c"]),
                                          _fs("Visit_Status__c", "picklist"),
                                          _fs("Guest_Kind__c", "reference", ["Guest_Kind__c"])]),
           "Guest_Kind__c": _spec("Guest_Kind__c", [_fs("Name", "name")]),
           "Helper__c": _spec("Helper__c", [_fs("Name", "name")])}
    bullet = "- `Guest_Kind__c` tells the guests apart; the helper is in `Visit_Helper__c`."
    rec = _rec(VISIT_ANSWER, evidence=[_visits()])
    assert not merge._other_object_subject(bullet, merge._used_api(rec, cat), cat)
    assert merge._other_object_subject(bullet, merge._used_api(rec, tuple(cat)), {})   # names only: off-topic
    assert _added(bullet, _visits(), answer=VISIT_ANSWER, objects=cat) == [bullet]


def test_f5_list_rows_and_business_words_keep_the_merge_by_code(monkeypatch):
    """B1 of the review (SFA_TABLES model, the live default): thinking aloud counts only in a prose line, so a list row
    "- INT-1: It seems ..." or "1. It seems urgent: 4" keeps the merge by code, and "in draft" (a picklist value) is no
    merge-input talk; tool names still send the answer to the merge call."""
    rows = ["Feedback on Zed Quill's 2 interviews (`Interview__c`):\n- INT-1 (2026-05-02): It seems the candidate was "
            "not prepared.\n- INT-2 (2026-05-09): Strong answers.\n\n" + SOURCE,
            "Top subjects (`Recruiter__c`):\n1. It seems urgent: 4\n2. Follow-up: 2\n\n" + SOURCE,
            "- Feedback: It appears the client liked him.\n\n" + SOURCE,
            "2 of Zed Quill's offer letters (`Recruiter__c`) are in draft and 1 was sent.\n\n" + SOURCE,
            "The draft offer for Zed Quill (`Recruiter__c`) was created on 2026-05-02.\n\n" + SOURCE]
    for record in rows:
        assert not merge.plans_aloud(record), record
    monkeypatch.setenv("SFA_MERGE", "code")
    for record in rows[:2] + rows[3:4]:
        model = Model(record=record)
        o, _row, _s = _answer(model)
        assert o.merge_by == "code" and model.merge_requests() == [], record
    assert merge.plans_aloud("6 employees, from the `count_records` result.\n\n" + SOURCE)
    assert merge.plans_aloud("Per the instructions, 6 employees are in Zorvex.\n\n" + SOURCE)


def test_the_trace_line_says_what_an_unmerged_code_merge_showed(monkeypatch, capsys):
    """B9 and SPS-2 of the review: a code merge that kept no bullet but added its counted line, and one that added
    nothing to a Hinglish record answer, each get their own trace line; the line of an answer with nothing to add stays."""
    monkeypatch.setenv("SFA_MERGE", "code")

    class GroupTools(StubTools):   # the count is a group_records by the team lead
        def call(self, name, arguments=None):
            out = super().call(name, arguments)
            self._evidence[-1].tool = "group_records"
            return out

    record = "6 employees have Zed Quill as their team lead:\n- Zed Quill: 6\n\n" + SOURCE
    group = {"grouped_by": "Team_Lead__c (Employee name)", "measure": "count", "group_count": 1, "shown": 1,
             "groups": [{"group": "Zed Quill", "value": 6}]}
    o, _row, session = _answer(Model(facts=B_OTHER, record=record), GroupTools(**group))
    assert o.kind == "records" and not o.m.merged and "Counted: " in o.final
    cli.print_outcome(o, session, False)
    out = capsys.readouterr().out
    assert ("-- no field-meaning bullet kept (merge by code); code added the line that says what was counted"
            in out) and "fits the answer" not in out
    hinglish = "Team Zorvex mein **6 alag-alag employees** hain, sab `Recruiter__c` ke records.\n\n" + SOURCE
    o, _row, session = _answer(Model(record=hinglish))
    assert o.kind == "records" and o.final == hinglish
    cli.print_outcome(o, session, False)
    out = capsys.readouterr().out
    assert ("-- no field-meaning bullet added (merge by code): the record answer is Hinglish and the field meanings are "
            "English; the record agent's answer is shown") in out and "fits the answer" not in out


# The gap check of the step-2 fixes (2026-10-30): two copied mistakes the 10-29 evaluation listed that F1-F12 kept (P18,
# Q29), as made-up shapes. Visit_Host__c is flagged FLS-hidden in its raw knowledge entry, Visit_Helper__c is not;
# Visit_Guest__c is a child of Visit__c, Gate_Pass__c is linked to no visit in either direction.
GATE_CATALOG = {
    "Visit__c": _spec("Visit__c", [_fs("Name", "name"), _fs("Visit_Helper__c", "reference", ["Helper__c"]),
                                   _fs("Visit_Host__c", "reference", ["Helper__c"])],
                      marks={"Visit_Host__c": {"fls_hidden": True}}),
    "Visit_Guest__c": _spec("Visit_Guest__c", [_fs("Visit__c", "reference", ["Visit__c"]),
                                               _fs("Guest_Role__c", "picklist")]),
    "Gate_Pass__c": _spec("Gate_Pass__c", [_fs("Holder__c", "reference", ["Helper__c"]),
                                           _fs("Pass_Role__c", "picklist")]),
    "Helper__c": _spec("Helper__c", [_fs("Name", "name")]),
}


def _hosted() -> QueryEvidence:
    return _query(obj="Visit__c", fields=[("Visit__c", "Visit_Helper__c"), ("Visit__c", "Visit_Host__c")],
                  result={"count": 12})


def test_f10f_a_field_called_fls_hidden_that_the_knowledge_does_not_flag_is_left_out():
    """F10f (P18): a field called FLS-hidden ("the field is FLS-hidden", the field as for F3) whose raw knowledge entry
    flags fls_hidden on none of its objects; a flagged field keeps its note (as Q33 does), a negated claim is no claim,
    and without a catalog entry (the objects as names only) there is no verdict."""
    drop = ("- The obsolete `Visit_Helper_Assign` flow once kept the `Helper` guest row by matching the visit's "
            "`Visit_Helper__c` field to the employee, but no current automation writes it and the field is FLS-hidden")
    keep = ["- `Visit_Host__c` on `Visit__c` is the lookup to `Helper__c` for the visit's host, but it is FLS-hidden "
            "and normally blank.",
            "- `Visit_Helper__c` is not FLS-hidden; it names the employee who supported the visit."]
    assert _added(drop, _hosted(), answer=VISIT_ANSWER, objects=GATE_CATALOG) == []
    assert _added(drop, _hosted(), answer=VISIT_ANSWER, objects=tuple(GATE_CATALOG)) == [drop]
    for bullet in keep:
        assert _added(bullet, _hosted(), answer=VISIT_ANSWER, objects=GATE_CATALOG) == [bullet], bullet
    rec = _rec(VISIT_ANSWER, evidence=[_hosted()])
    assert merge._contradiction(drop, merge._used_api(rec, GATE_CATALOG), rec, GATE_CATALOG) == (
        "Visit_Helper__c called FLS-hidden, which the knowledge flags on none of its objects")


def test_f2d_a_row_of_an_object_no_lookup_links_to_the_answer_is_left_out():
    """F2 (d) (Q29): "stored on the `X` ... row" of a catalog object that no lookup links to an object of the answer,
    in either direction; a child object's row and a parent's record are kept, and without a catalog entry (the objects
    as names only) there is no verdict."""
    head = "- `Visit_Helper__c` is the lookup on `Visit__c` that names the helper, though in some flows the helper is "
    drop = head + "stored on the `Gate_Pass__c` holder row with role Helper."
    keep = [head + "stored on the `Visit_Guest__c` guest row with role Helper.",   # a child of the visit
            head + "stored on the `Helper__c` record."]                          # the lookup's own target
    assert _added(drop, _visits(), answer=VISIT_ANSWER, objects=GATE_CATALOG) == []
    assert _added(drop, _visits(), answer=VISIT_ANSWER, objects=tuple(GATE_CATALOG)) == [drop]
    for bullet in keep:
        assert _added(bullet, _visits(), answer=VISIT_ANSWER, objects=GATE_CATALOG) == [bullet], bullet
    rec = _rec(VISIT_ANSWER, evidence=[_visits()])
    assert merge._contradiction(drop, merge._used_api(rec, GATE_CATALOG), rec, GATE_CATALOG) == (
        "no lookup links Gate_Pass__c to Visit__c")
