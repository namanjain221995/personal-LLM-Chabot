"""Evaluation fixes of 2026-10-22, AGENT lane: fixes 2, 3 and 5. Offline: fake evidence, fake tools, scripted LLMs;
fake names only.

- fix 2: an answer with nothing but its Source line (and the lines code writes around an answer) is no answer
  (:func:`answer_text`): the record session drops it and code writes the people answer; the merge neither merges nor
  falls back on it; the pipeline shows, in this order, the merged answer, the record answer, the code-written people
  answer, the not-found answer and the knowledge answer (:func:`choose_answer`), never an empty one;
- fix 3: a merged count-0 clause that a clause of the record answer holds with the same claim is no reason to reject
  the merge (review of 2026-10-22: not a clause that drops the record answer's negation; the any_role clause is
  fixed by fix_any_role_scope, not let through);
- fix 5: a word of the question that is in the knowledge wording (label, from_source, from_target) of a person lookup
  other than the one the answer used fires the relation-lookup check with the direction the question asks for
  (:func:`relation_lookup_nudges`); the direction check reads that wording too (:func:`direction_nudges`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sf_assistant import pipeline
from sf_assistant.agent import record_prompts as RP
from sf_assistant.agent import record_session as RS
from sf_assistant.agent.checks import drop_ref_codes, reference_codes
from sf_assistant.agent.merge import _zero_lookup_presence, combine, fix_any_role_scope, merge_scope_issues
from sf_assistant.agent.nudges import (_direction_side, _stem, _wording, answer_text, direction_nudges,
                                       relation_lookup_nudges)
from sf_assistant.agent.people_answer import agent_people_result, named_spellings, people_answer, people_answer_from
from sf_assistant.agent.record_session import RecordAnswer, RecordSession
from sf_assistant.agent.results import MergeResult
from sf_assistant.config import load_config
from sf_assistant.engine.types import QueryEvidence, RecordsStatus
from sf_assistant.prompts import fill
from sf_assistant.tools.knowledge_tools import Tool

ROOT = Path(__file__).resolve().parents[1]
REC = "Recruiter__c"
II = "Internal_Interview__c"
INT = "Interview__c"
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."
KNOWLEDGE = "Client interviews are `Interview__c` records; `Interview_Support_Person__c` holds the support person."
STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")


def _cfg():
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=6)


def _ev(tool, result, qid="q1", obj=REC, fields_used=(), ok=True):
    return QueryEvidence(query_id=qid, tool=tool, kind="data", object=obj, arguments={}, ok=ok, error=None,
                         applied=result.get("applied") or {}, notes=[], statements=[], tables=[obj],
                         synced_at={obj: "2026-10-13 14:12 UTC"}, summary="", model_result=result, elapsed_ms=1.0,
                         fields_used=list(fields_used))


def _other(field, records, role="", group="same_target", target=REC, fits=None):
    return {"field": field, "target": target, "group": group, "role": role, "fits": fits, "records": records,
            "same_person": group != "same_name"}


def _people_res(name="Kilo Zephyr", asked="kilo zephyr", refs=()):
    """An about_people result for one employee (main record R1)."""
    return {"asked": [asked], "people": [{"ref": "P1", "asked": asked, "label": name, "kind": "employee",
                                         "detail": "Active Employee", "match": "full name", "records": ["R1"],
                                         "profile": {"Employment_Status__c (Employment Status)": "Active Employee"}}],
            "matched_records": [{"ref": "R1", "person": "P1", "label": name, "object": REC, "object_label": "Employee",
                                 "how": "name"}],
            "references": list(refs), "system_references": []}


# ======================================================================================== fix 2: empty answers
FOOTER = (fill(RP.RECORDS_FOOTER_LOOKUP_USED, LOOKUPS=f"`{II}.Recruiter__c` (recruiter)") + " "
          + fill(RP.RECORDS_FOOTER_SAME_PERSON, OBJECT=II, OTHERS="`Interviewer__c` (interviewer) 3"))
STATUS_LINE = fill(RP.RECORDS_STATUS_LINE, OBJECT="Internal Interview", FIELD="Status__c", COUNT=7,
                   SPLIT="Completed 4, Scheduled 3", ZERO="No Show")
OTHERS_LINE = fill(RP.RECORDS_OTHER_PEOPLE_LINE, WORDS="echo", OTHERS="Echo Two (employee)")
#: Stored names with an initial or a "Jr." (review of 2026-10-23): the line is still the code's (its placeholder ends at
#: its fixed text "; this answer does not cover them.", not at the first ". ").
INITIALS_LINES = [fill(RP.RECORDS_OTHER_PEOPLE_LINE, WORDS="echo", OTHERS=o) for o in (
    "E. Two (employee)", "Echo T. Two (candidate), Echo Three (employee)", "Echo Two Jr. (lead)")]


@pytest.mark.parametrize("text", [
    "", "  \n\n", SOURCE, "**Source:** preprod sandbox copy", "- _Source_: preprod sandbox copy",
    FOOTER + "\n" + SOURCE, STATUS_LINE + "\n" + SOURCE, OTHERS_LINE + "\n\n" + SOURCE,
    "**Summary:**\n" + SOURCE, "### Totals\n---\n**\n" + SOURCE,
    *(line + "\n" + SOURCE for line in INITIALS_LINES),
])
def test_an_answer_with_nothing_but_its_source_and_code_lines_says_nothing(text):
    assert answer_text(text) == ""


@pytest.mark.parametrize("text,said", [
    ("0\n" + SOURCE, "0"),
    ("Nobody.\n" + SOURCE, "Nobody."),
    ("**Summary:**\n- Delta Person took 7 mock interviews.\n" + FOOTER + "\n" + SOURCE,
     "- Delta Person took 7 mock interviews."),
    ("Status of these interviews: 5 completed, 2 scheduled.\n" + SOURCE,     # the model's own line, not the code's
     "Status of these interviews: 5 completed, 2 scheduled."),
    (fill(RP.RECORDS_NO_DATA_LINE, FIELDS="`Final_Decision_By__c`") + "\n" + SOURCE,   # the code's no-data answer
     fill(RP.RECORDS_NO_DATA_LINE, FIELDS="`Final_Decision_By__c`")),
    # review of 2026-10-22: a one-line answer written as a heading or a bold colon line says something; a model line
    # that starts like the lookup footer but holds sentences is the model's
    ("### No interviews are recorded for Delta Person\n\n" + SOURCE, "### No interviews are recorded for Delta Person"),
    ("**Delta Person has no client interviews:**\n\n" + SOURCE, "**Delta Person has no client interviews:**"),
    ("**Answer:**\n### Kilo Zephyr reports to Lima Nobel\n" + SOURCE, "### Kilo Zephyr reports to Lima Nobel"),
    ("# Mike Oscar is an active employee in the Marketing department\n" + SOURCE,
     "# Mike Oscar is an active employee in the Marketing department"),
    ("Lookup used: the reporting line. Kilo Zephyr reports to Lima Nobel.\n" + SOURCE,
     "Lookup used: the reporting line. Kilo Zephyr reports to Lima Nobel."),
])
def test_answer_text_keeps_every_line_that_says_something(text, said):
    assert answer_text(text) == said


class _Tools:
    """The record tools surface with canned results: count_records -> 37, about_people -> one employee."""

    def __init__(self, people=None):
        self._evidence: list[QueryEvidence] = []
        self.people = people or _people_res()
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {})
                      for n in ("count_records", "about_people")}

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        qid = f"q{len(self._evidence) + 1}"
        if name == "about_people":
            res = {"query_id": qid, "object": REC, **self.people}
            ev = _ev("about_people", res, qid=qid)
            ev.applied = {"names": list((arguments or {}).get("names") or [])}
        else:
            res = {"query_id": qid, "object": INT, "count": 37, "counted": "Interview__c records",
                   "applied": {"filters": ["Interview_Outcome__c = 'Offer Received'"]}}
            ev = _ev("count_records", res, qid=qid, obj=INT, fields_used=[(INT, "Interview_Outcome__c")])
        self._evidence.append(ev)
        return {"ok": True, "result": res, "error": None, "chunk_ids": []}

    def begin_question(self, question=""):
        self._evidence = []

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question):
        return ""

    def status(self):
        return STATUS

    def close(self):
        pass


class _Script:
    """Replies script[n], n = the tool results so far (the last one repeats); route call RECORDS; no names call (the
    fake tools have no name index)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def chat(self, messages, tools=None, **_):
        if messages[0].get("content") == RP.ROUTE_SYSTEM:
            return {"content": "RECORDS", "tool_calls": [], "usage": {}}
        self.calls.append(messages)
        n = sum(1 for m in messages if m.get("role") == "tool")
        step = self.script[min(n, len(self.script) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{n}", "name": step["name"], "arguments": step["args"]}],
                    "usage": {}}
        return {"content": step, "tool_calls": [], "usage": {}}


COUNT_CALL = {"name": "count_records", "args": {"object": INT}}
PEOPLE_CALL = {"name": "about_people", "args": {"names": ["Kilo Zephyr"]}}
Q_OFFERS = "how many client interviews ended with an offer"


def test_a_reply_with_only_its_source_line_is_no_answer():
    rec = RecordSession(_Tools(), _cfg(), llm=_Script([COUNT_CALL, SOURCE])).ask(Q_OFFERS)
    assert rec.no_records and rec.answer == "" and rec.error == "empty record answer"
    assert rec.dropped_answer == SOURCE and not rec.code_answer and rec.used_data


def test_a_reply_the_checks_leave_with_only_its_source_line_is_no_answer(monkeypatch):
    # a cleaner that takes out every sentence but the Source line (in the run: a caveat or overlap clause)
    monkeypatch.setattr(RS, "drop_record_side_caveats", lambda text, evidence, question="": SOURCE)
    reply = "37 client interviews ended with an offer.\n" + SOURCE
    rec = RecordSession(_Tools(), _cfg(), llm=_Script([COUNT_CALL, reply])).ask(Q_OFFERS)
    assert rec.no_records and rec.answer == "" and rec.dropped_answer == reply
    assert rec.error == "empty record answer: the checks left only its Source line"


def test_an_empty_reply_after_the_agents_own_about_people_gets_the_code_written_people_answer():
    """The triage confirmed nobody (no name index), the record agent called about_people itself and then wrote only
    the Source line: code writes the record answer from that result (code_answer), never an empty answer."""
    q = "who is the employee kilo zephyr reports to"
    rec = RecordSession(_Tools(), _cfg(), llm=_Script([PEOPLE_CALL, SOURCE])).ask(q)
    assert rec.named_people == [] and rec.code_answer and not rec.no_records and rec.error is None
    assert rec.agent_error == "empty record answer" and rec.dropped_answer == SOURCE
    # step-2 fixes of 2026-10-30 (F1, documented update): the code-written people answer without its P/R codes
    assert rec.answer.startswith(_shown(people_answer(_people_res())).split("\n")[0]) and "(P1)" not in rec.answer
    assert answer_text(rec.answer) and rec.answer.rstrip().endswith("synced 2026-10-13 14:12 UTC.")
    # a NO_RECORDS reply is the agent's decision, not an empty answer: no people answer (as before)
    rec = RecordSession(_Tools(), _cfg(), llm=_Script([PEOPLE_CALL, "NO_RECORDS"])).ask(q)
    assert rec.no_records and not rec.code_answer and rec.answer == "" and rec.error is None


@pytest.mark.parametrize("reply,said", [
    ("### Kilo Zephyr is an active employee\n" + SOURCE, "### Kilo Zephyr is an active employee"),
    ("**Answer:**\n### Kilo Zephyr is an active employee\n\n" + SOURCE, "### Kilo Zephyr is an active employee"),
])
def test_a_reply_written_as_a_heading_is_the_record_answer(reply, said):
    """Review of 2026-10-22: a one-line answer written as a heading says something, so the record session keeps it
    (no "empty record answer", no code-written people answer)."""
    q = "who is the employee kilo zephyr reports to"
    rec = RecordSession(_Tools(), _cfg(), llm=_Script([PEOPLE_CALL, reply])).ask(q)
    assert not rec.no_records and not rec.code_answer and rec.error is None and rec.dropped_answer == ""
    assert said in rec.answer and answer_text(rec.answer) == said


def test_agent_people_result_reads_the_last_successful_about_people_call():
    count = _ev("count_records", {"count": 3}, qid="q1")
    first, last = _ev("about_people", _people_res(), qid="q2"), _ev("about_people", _people_res("Lima Nobel"), qid="q3")
    failed = _ev("about_people", {}, qid="q4", ok=False)
    assert agent_people_result([count, first, last, failed]) is last.model_result
    assert agent_people_result([count]) is None and people_answer_from([count]) == "" and people_answer_from(None) == ""
    # step-2 fixes of 2026-10-30 (F1, documented update): without the P/R codes the evidence maps
    assert people_answer_from([first]) == drop_ref_codes(people_answer(_people_res()), reference_codes([first]))
    assert "(P1)" not in people_answer_from([first]) and "(P1)" in people_answer(_people_res())


def test_the_code_written_people_answer_drops_only_the_codes_the_evidence_maps():
    """Step-2 fixes of 2026-10-30 (F1, HP03): people_answer_from drops the P/R codes reference_codes maps (the agent's
    reply is cleaned the same way); a code the evidence does not hold stays, and people_answer itself keeps its
    codes."""
    res = _people_res()
    res["people"][0]["profile"] = {"Employment_Status__c (Employment Status)": "Active Employee",
                                   "Badge__c (Badge)": "(P7)"}
    ev = [_ev("about_people", res)]
    text = people_answer_from(ev)
    assert "Kilo Zephyr (P1)" in people_answer(res) and "(P1)" not in text and "Badge: (P7)" in text
    assert text.startswith("Kilo Zephyr is an employee (Active Employee)")


def _look_alike_res():
    """about_people for the asked 'Quorra Zeld' (not stored): the closest stored name is the lead 'Quinta Zelder'."""
    return {"asked": ["Quorra Zeld"], "people": [
        {"ref": "P1", "label": "Quinta Zelder", "kind": "lead", "detail": "Contacted", "match": "closest stored name",
         "asked": "Quorra Zeld", "records": ["R1"], "no_business_references": True,
         "profile": {"Status": "Contacted", "LeadSource": "Web", "Plan__c (Plan)": "Basic"}}],
        "matched_records": [{"ref": "R1", "person": "P1", "label": "Quinta Zelder", "object": "Lead",
                             "object_label": "Lead"}], "references": []}


@pytest.mark.parametrize("merge_by", ["code", "model"])
def test_the_people_answer_fallback_shows_a_look_alike_without_its_code_or_profile(monkeypatch, merge_by):
    """Step-2 fixes of 2026-10-30 (F1 and F9, HP03): the record side failed after about_people found only a
    look-alike of the asked name; choose_answer's fallback shows the not-found lead, the name and the kind, with no
    reference code and no profile item, whatever SFA_MERGE says."""
    monkeypatch.setenv("SFA_MERGE", merge_by)
    rec = _rec("", [_ev("about_people", _look_alike_res(), obj="Lead")], no_records=True, error="empty record answer")
    llm = _KnowledgeLLM()
    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, llm, 3, session=_StubSession(rec))
    assert o.kind == "records" and "merge" not in llm.roles
    assert o.body.startswith("No record is named 'Quorra Zeld'; the closest stored name is 'Quinta Zelder'. Quinta "
                             "Zelder is a lead.")
    for unasked in ("(P1)", "Status:", "Contacted", "LeadSource", "Plan:"):
        assert unasked not in o.body, unasked


class _NoCallLLM:
    def chat(self, *a, **k):
        raise AssertionError("no model call expected")


def _rec(answer, evidence=None, no_records=False, error=None, triage=None):
    return RecordAnswer(question=Q_OFFERS, answer=answer, no_records=no_records, used_data=True,
                        evidence=list(evidence or []), agent=None, error=error, unsupported_numbers=[], seconds=1.0,
                        triage=dict(triage or {}))


def test_the_merge_never_merges_or_falls_back_on_an_empty_answer():
    m = combine(_NoCallLLM(), Q_OFFERS, KNOWLEDGE, _rec(SOURCE), STATUS)
    assert m.text == "" and not m.merged and m.error == "the record answer is empty"
    rec = _rec("37 client interviews ended with an offer.\n" + SOURCE, [_ev("count_records", {"count": 37}, obj=INT)])
    m = combine(_Script([SOURCE]), Q_OFFERS, KNOWLEDGE, rec, STATUS)   # the merge reply is only a Source line
    assert not m.merged and m.text == rec.answer and m.error == "empty merged answer"


BODY = "37 client interviews ended with an offer.\n" + SOURCE
PEOPLE_EV = [_ev("about_people", _people_res())]


def _shown(text: str) -> str:
    """The code-written people answer as shown (step-2 fixes of 2026-10-30, F1): without the P/R codes of PEOPLE_EV."""
    return drop_ref_codes(text, reference_codes(PEOPLE_EV))


@pytest.mark.parametrize("m,rec,not_found,k_answer,records_only,expected", [
    # the merged answer when the merge was accepted and says something
    (MergeResult(text="Merged.\n" + SOURCE, merged=True), _rec(BODY), "", "K.", False,
     ("Merged.\n" + SOURCE, "merged")),
    # an accepted merge with nothing but its Source line: the record answer
    (MergeResult(text=SOURCE, merged=True), _rec(BODY), "", "K.", False, (BODY, "records")),
    (MergeResult(text=BODY, merged=False), _rec(BODY), "", "K.", False, (BODY, "records")),
    # an empty record answer, the record side failed, the agent used about_people: code's people answer
    # (step-2 fixes of 2026-10-30, F1, documented update: without its P/R codes)
    (MergeResult(text=SOURCE, merged=False), _rec(SOURCE, PEOPLE_EV, error="empty record answer"), "", "K.", False,
     (_shown(people_answer(_people_res())) + "\n" + "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC.",
      "records")),
    (None, _rec("", PEOPLE_EV, no_records=True, error="KeyError: 'x'"), "", "K.", False,
     (_shown(people_answer(_people_res())) + "\n" + "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC.",
      "records")),
    # ... then the not-found answer, then the knowledge answer
    (None, _rec("", [], no_records=True, error="no successful record query"), "No record is named 'x'.", "K.", False,
     ("No record is named 'x'.", "records")),
    (MergeResult(text=SOURCE, merged=False), _rec(SOURCE), "", "K.", False, ("K.", "knowledge")),
    # a NO_RECORDS decision (no error) keeps the knowledge answer, even after about_people
    (None, _rec("", PEOPLE_EV, no_records=True), "", "K.", False, ("K.", "knowledge")),
    # names that are not stored: about_people lists other people; the not-found answer stands
    (None, _rec("", PEOPLE_EV, no_records=True, error="NO_RECORDS reply after a name was not found",
                triage={"people_data": False}), "No record is named 'x'.", "K.", False,
     ("No record is named 'x'.", "records")),
    # never an empty answer: nothing left -> "" and "none"
    (MergeResult(text=SOURCE, merged=True), _rec(SOURCE), "", "  ", False, ("", "none")),
    (None, _rec(SOURCE), "", "K.", True, ("", "none")),
    (None, _rec(BODY), "", "", True, (BODY, "records")),
    # records off: the knowledge answer as before
    (None, None, "", "K.", False, ("K.", "knowledge")),
    (None, None, "", "", False, ("", "none")),
])
def test_choose_answer_order(m, rec, not_found, k_answer, records_only, expected):
    assert pipeline.choose_answer(m, rec, not_found, k_answer, records_only, STATUS) == expected


def test_the_people_answer_fallback_needs_a_result_about_every_named_person():
    """Review of 2026-10-22: the people tool failed for the question's two people after an earlier about_people call
    took the first only; a profile of one person is no answer about both, so the knowledge answer (with the warning
    the CLI puts before it) is shown. A result that covers both names (or a first name of a listed person) is used."""
    failed = "about_people failed: statements stopped (time budget)"
    rec = _rec("", PEOPLE_EV, no_records=True, error=failed)
    rec.named_people = ["kilo zephyr", "lima nobel"]
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS) == ("K.", "knowledge")
    assert people_answer_from(PEOPLE_EV, ["kilo zephyr", "lima nobel"]) == ""
    rec.named_people = ["kilo"]                         # a first name of the listed person: covered
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS)[1] == "records"
    both = _people_res()
    both["asked"] = ["kilo zephyr", "lima nobel"]
    rec = _rec("", [_ev("about_people", both)], no_records=True, error=failed)
    rec.named_people = ["kilo zephyr", "lima nobel"]
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS)[1] == "records"


def test_the_people_answer_fallback_reads_a_misspelt_name_by_its_stored_spelling():
    """Review of 2026-10-23: the question typed 'kilo zephir', the triage read it as the stored 'Kilo Zephyr' and the
    agent's about_people call took the stored spelling: that result covers the named person through the stored
    spellings of RecordAnswer.named_spellings (the typed words alone do not cover it); another person's does not."""
    failed = "about_people failed: statements stopped (time budget)"
    rec = _rec("", PEOPLE_EV, no_records=True, error=failed)
    rec.named_people = ["kilo zephir"]
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS) == ("K.", "knowledge")
    rec.named_spellings = {"kilo zephir": ["Kilo Zephyr"]}
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS)[1] == "records"
    # step-2 fixes of 2026-10-30 (F1, documented update): without the P/R codes
    assert people_answer_from(PEOPLE_EV, ["kilo zephir"], rec.named_spellings) == _shown(people_answer(_people_res()))
    rec.named_spellings = {"kilo zephir": ["Lima Nobel"]}
    assert pipeline.choose_answer(None, rec, "", "K.", False, STATUS) == ("K.", "knowledge")
    # the triage's people: a misspelt or reordered name gives its stored spellings; a stored or partial name none
    people = [{"words": "kilo zephir", "asked": "kilo zephir", "close": ["Kilo Zephyr"], "read_as": "Kilo Zephyr"},
              {"words": "zephyr kilo m", "asked": "zephyr kilo m", "close": ["Kilo M Zephyr", "Kilo Zephyr"]},
              {"words": "lima nobel", "asked": None, "close": []},
              {"words": "kilo quorz", "asked": "kilo quorz", "partial": "kilo", "close": []}]
    assert named_spellings(people) == {"kilo zephir": ["Kilo Zephyr"],
                                       "zephyr kilo m": ["Kilo M Zephyr", "Kilo Zephyr"]}
    assert named_spellings(None) == {} and named_spellings([{"words": "x"}]) == {}


class _MisspeltTools(_Tools):
    """The question types 'kilo zephir'; spot_people confirms it as the stored 'Kilo Zephyr' (asked, close, read_as);
    about_people answers the stored spelling only (any other name stops: a time budget)."""

    def spot_people(self, question):
        if "kilo zephir" not in question.lower():
            return []
        return [{"words": "kilo zephir", "recs": [(REC, "a06000000000020AAA", "Kilo Zephyr", "Active Employee")],
                 "strength": "strong", "first_only": False, "asked": "kilo zephir", "close": ["Kilo Zephyr"],
                 "read_as": "Kilo Zephyr"}]

    def call(self, name, arguments=None):
        names = [" ".join(str(n).lower().split()) for n in (arguments or {}).get("names") or []]
        if name == "about_people" and names != ["kilo zephyr"]:
            ev = _ev("about_people", {}, qid=f"q{len(self._evidence) + 1}", ok=False)
            ev.error, ev.applied = "statements stopped (time budget)", {"names": names}
            self._evidence.append(ev)
            return {"ok": False, "result": None, "error": ev.error, "chunk_ids": []}
        return super().call(name, arguments)


def test_the_record_session_gives_the_stored_spellings_of_a_misspelt_named_person():
    """The record agent called about_people with the stored spelling and wrote only the Source line; the session's own
    call with the typed words failed. The record answer carries the triage's stored spellings, so the pipeline shows
    the people answer from the agent's result (before: the knowledge answer)."""
    q = "who is the employee kilo zephir reports to"
    rec = RecordSession(_MisspeltTools(), _cfg(), llm=_Script([PEOPLE_CALL, SOURCE])).ask(q)
    assert rec.named_people == ["kilo zephir"] and rec.named_spellings == {"kilo zephir": ["Kilo Zephyr"]}
    assert rec.no_records and rec.error and not rec.code_answer
    final, kind = pipeline.choose_answer(None, rec, "", "K.", False, STATUS)
    # step-2 fixes of 2026-10-30 (F1, documented update): without the P/R codes
    assert kind == "records" and final.startswith(_shown(people_answer(_people_res())).split("\n")[0])
    assert "(P1)" not in final


class _KnowledgeLLM:
    """The knowledge agent (one search, then its answer) and the merge (counted, never expected here)."""

    def __init__(self, answer="`Interview_Outcome__c` holds the outcome of a client interview."):
        self.answer, self.roles = answer, []

    def chat(self, messages, tools=None, **_):
        if str(messages[0].get("content") or "") == RP.COMBINE_SYSTEM:
            self.roles.append("merge")
            return {"content": "Merged.\n" + SOURCE, "tool_calls": [], "usage": {}}
        self.roles.append("knowledge")
        if tools and not any(m.get("role") == "tool" for m in messages):
            return {"content": None, "tool_calls": [{"id": "k0", "name": "search_knowledge",
                                                      "arguments": {"query": "outcome", "k": 3}}], "usage": {}}
        return {"content": self.answer, "tool_calls": [], "usage": {}}


class _StubSession:
    def __init__(self, rec):
        self.rec, self.tools = rec, object()

    def ask(self, text):
        return self.rec

    def status(self):
        return STATUS


def _registry():
    from sf_assistant.knowledge.search import KnowledgeIndex
    from sf_assistant.knowledge.store import ObjectKnowledge
    from sf_assistant.tools.knowledge_tools import ToolRegistry

    kn = ObjectKnowledge.load(str(ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def test_the_pipeline_never_shows_an_answer_with_only_its_source_line():
    llm = _KnowledgeLLM()
    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, llm, 1, session=_StubSession(_rec(SOURCE)))
    assert o.kind == "knowledge" and o.final == o.body == llm.answer and "merge" not in llm.roles
    assert o.m is not None and o.m.error == "the record answer is empty"
    # the record side failed after about_people: code's people answer, with a Source line
    rec = _rec("", PEOPLE_EV, no_records=True, error="empty record answer")
    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, _KnowledgeLLM(), 2, session=_StubSession(rec))
    # step-2 fixes of 2026-10-30 (F1, documented update): the code-written people answer without its P/R codes
    assert o.kind == "records" and o.body.startswith("Kilo Zephyr is an employee") and SOURCE in o.body
    assert "(P1)" not in o.body


def test_the_status_line_says_which_answer_choose_answer_showed(capsys):
    """cli.print_outcome names the answer that is shown (integration of fix 2): the knowledge answer after an empty
    record answer, and the code-written people answer of the pipeline."""
    from sf_assistant import cli

    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, _KnowledgeLLM(), 1, session=_StubSession(_rec(SOURCE)))
    cli.print_outcome(o, _StubSession(o.rec), False)
    out = capsys.readouterr().out
    assert "-- could not merge (the record answer is empty); the knowledge answer is shown" in out
    assert "the record agent's answer is shown" not in out
    rec = _rec("", PEOPLE_EV, no_records=True, error="empty record answer")
    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, _KnowledgeLLM(), 2, session=_StubSession(rec))
    cli.print_outcome(o, _StubSession(rec), False)
    out = capsys.readouterr().out
    assert ("-- records: the record agent gave no usable answer (empty record answer); code wrote the record part "
            "from about_people") in out and "knowledge answer shown" not in out
    # the merge's own fallback on the record answer: as before
    rec = _rec(BODY, [_ev("count_records", {"count": 37}, obj=INT)])
    o = pipeline.answer(Q_OFFERS, _cfg(), _registry(), None, _ScriptMerge(SOURCE), 3, session=_StubSession(rec))
    cli.print_outcome(o, _StubSession(rec), False)
    assert "-- could not merge (empty merged answer); the record agent's answer is shown" in capsys.readouterr().out


class _ScriptMerge(_KnowledgeLLM):
    """:class:`_KnowledgeLLM` whose merge reply is ``merged``."""

    def __init__(self, merged):
        super().__init__()
        self.merged = merged

    def chat(self, messages, tools=None, **kw):
        if str(messages[0].get("content") or "") == RP.COMBINE_SYSTEM:
            self.roles.append("merge")
            return {"content": self.merged, "tool_calls": [], "usage": {}}
        return super().chat(messages, tools, **kw)


# ===================================================================== fix 3: the record answer's own clause
ANY_ROLE = ("- In any of the Employee lookups on `Interview__c` (including `Interview_Support_Person__c`, "
            "`varPreferred_support_person__c`, `Interviewer_Assignment_Constraint__c`, and `Recruiter__c`), he is "
            "referenced on 146 records.")
REC_ANSWER = ("Delta Person supported **143** client interviews as the `Interview_Support_Person__c` (interview "
              "support person).\n\n- He is the TL preferred support person on 3 interviews "
              "(`varPreferred_support_person__c`).\n" + ANY_ROLE + "\n\n" + SOURCE)
Q_SUPPORT = "how many client interviews did delta person support"


def _support_ev():
    check = {"filter": "Interview_Support_Person__c.Name = 'Delta Person'", "object": INT,
             "field": "Interview_Support_Person__c", "target": REC, "target_label": "Employee",
             "person_kind": "employee", "role": "support person", "fits": "support", "cues": ["support"],
             "records": 143, "any_role": 146,
             "others": [_other("varPreferred_support_person__c", 3, role="tl preferred support person"),
                        _other("Interviewer_Assignment_Constraint__c", 0, role="assignment constraint employee"),
                        _other("Recruiter__c", 0, role="owning recruiter"),
                        _other("LastModifiedById", 6, role="last editor", group="system", target="User")],
             "scope": "all other conditions of this query"}
    res = {"query_id": "q1", "object": INT, "count": 143, "counted": "Interview__c records",
           "applied": {"filters": ["Interview_Support_Person__c.Name = 'Delta Person'"]}, "lookup_check": [check]}
    return [_ev("count_records", res, obj=INT, fields_used=[(INT, "Interview_Support_Person__c")])]


def test_a_merged_clause_the_record_answer_has_is_no_reason_to_reject_the_merge():
    ev = _support_ev()
    rec = _rec(REC_ANSWER, ev)
    # why: the any-role clause names a lookup whose count is 0, which the count-0 check reads as a presence claim
    assert _zero_lookup_presence(ANY_ROLE, ev) == "Interviewer_Assignment_Constraint__c"
    merged = (KNOWLEDGE + "\n\n" + REC_ANSWER.replace(SOURCE, "").rstrip() + "\n" + SOURCE)
    assert merge_scope_issues(merged, rec, Q_SUPPORT) == []
    # the same clause in another form (bold, no backticks, no list marker) is still the record answer's clause
    reworded = merged.replace(ANY_ROLE, "In any of the Employee lookups on **Interview__c** (including "
                              "Interview_Support_Person__c, varPreferred_support_person__c, "
                              "Interviewer_Assignment_Constraint__c, and Recruiter__c), he is referenced on **146** "
                              "records.")
    assert merge_scope_issues(reworded, rec, Q_SUPPORT) == []
    m = combine(_Script([merged]), Q_SUPPORT, KNOWLEDGE, rec, STATUS)
    assert m.merged and m.error is None and "146 records" in m.text
    # a clause the record answer does not have still rejects the merge
    added = merged.replace(SOURCE, "He also appears in `Interviewer_Assignment_Constraint__c` on these interviews.\n"
                           + SOURCE)
    assert any(i.startswith("the person in Interviewer_Assignment_Constraint__c, whose count is 0")
               for i in merge_scope_issues(added, rec, Q_SUPPORT))
    m = combine(_Script([added]), Q_SUPPORT, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.text == rec.answer


def test_a_merged_clause_that_drops_the_record_answers_negation_is_not_the_record_answers():
    """Review of 2026-10-22: the record answer says the person is NOT in the count-0 lookup; a merged clause with the
    same words but without the negation is a new presence claim, so the merge is rejected."""
    ev = _support_ev()
    neg = "No record shows that he is also referenced in `Interviewer_Assignment_Constraint__c`."
    rec = _rec("Delta Person supported **143** client interviews as the `Interview_Support_Person__c`.\n\n" + neg
               + "\n\n" + SOURCE, ev)
    claim = "He is also referenced in `Interviewer_Assignment_Constraint__c`."
    assert _zero_lookup_presence(neg, ev) is None and _zero_lookup_presence(claim, ev) == "Interviewer_Assignment_Constraint__c"
    merged = KNOWLEDGE + "\n\nDelta Person supported **143** client interviews as the `Interview_Support_Person__c`. " \
        + claim + "\n" + SOURCE
    assert any(i.startswith("the person in Interviewer_Assignment_Constraint__c, whose count is 0")
               for i in merge_scope_issues(merged, rec, Q_SUPPORT))
    m = combine(_Script([merged]), Q_SUPPORT, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.text == rec.answer


def test_an_any_role_clause_is_an_issue_even_when_the_record_answer_has_it_and_the_scope_fix_repairs_both():
    """Review of 2026-10-22: fix 3 is the count-0 check only; an any_role count given with creator or editor misstates
    its scope in both texts, and fix_any_role_scope now splits a bracket at "+" and "/" too (the typo run's
    "(Employee lookups + User creator/editor): 474"), so the clause is fixed and the merge passes without an
    exemption."""
    ev = _support_ev()
    clause = "- Counting creator and owner too, in any role: 146"
    rec = _rec("Delta Person supported 143 client interviews (`Interview_Support_Person__c`).\n" + clause + "\n"
               + SOURCE, ev)
    merged = KNOWLEDGE + "\n" + rec.answer
    assert any(i.startswith("the count in any role with creator") for i in merge_scope_issues(merged, rec, Q_SUPPORT))
    bracket = "- **In any role** (Employee lookups + User creator/editor): 146"
    assert fix_any_role_scope(bracket, ev) == "- **In any role** (Employee lookups): 146"
    assert fix_any_role_scope("- In any role (support, recruiter, creator, last editor): 146", ev) == \
        "- In any role (support, recruiter): 146"
    lead = "Delta Person supported 143 client interviews (`Interview_Support_Person__c`).\n"
    rec = _rec(lead + fix_any_role_scope(bracket, ev) + "\n" + SOURCE, ev)
    m = combine(_Script([KNOWLEDGE + "\n\n" + lead + bracket + "\n" + SOURCE]), Q_SUPPORT, KNOWLEDGE, rec, STATUS)
    assert m.merged and m.error is None and "(Employee lookups): 146" in m.text and "creator" not in m.text


# ===================================================================== fix 5: the question's relation word
OBJECT_WORDS = ["employee", "employees", "recruiter"]
#: The record session's person-lookup view (RecordSession.person_lookups) of the employee's self-lookups and of two
#: lookups of the internal interview to the employee.
VIEWS = {
    REC: [{"field": "Assigned_Sr_Recruiter__c", "label": "Reporting to", "target": REC, "person": True,
           "from_source": "senior recruiter this employee reports to",
           "from_target": "employees reporting to this senior recruiter", "role": "reporting senior recruiter",
           "object_words": OBJECT_WORDS},
          {"field": "Manager__c", "label": "Manager", "target": REC, "person": True,
           "from_source": "manager of the employee", "from_target": "employees managed by this manager",
           "role": "manager", "object_words": OBJECT_WORDS},
          {"field": "Team_Lead__c", "label": "Team Lead", "target": REC, "person": True,
           "from_source": "team lead of the employee", "from_target": "team members led by this employee",
           "role": "team lead", "object_words": OBJECT_WORDS}],
    II: [{"field": "Interviewer__c", "label": "Interviewer", "target": REC, "person": True,
          "from_source": "employee who takes this mock interview", "from_target": "mock interviews this employee took",
          "role": "interviewer", "object_words": ["internal", "interview", "interviews"] + OBJECT_WORDS},
         {"field": "Shadow__c", "label": "Shadow", "target": REC, "person": True,
          "from_source": "employee who shadows this mock interview",
          "from_target": "mock interviews this employee shadowed", "role": "shadow interviewer",
          "object_words": ["internal", "interview", "interviews"] + OBJECT_WORDS}],
}


def lookups(obj):
    return VIEWS.get(obj, [])


def _count_through(field="Manager__c", records=0, others=(("Assigned_Sr_Recruiter__c", 3), ("Team_Lead__c", 0)),
                   name="Mike Oscar", obj=REC, fits=None, cues=(), qid="q1"):
    """A count of ``obj`` records filtered on one person through ``field``, with its lookup check."""
    check = {"filter": f"{field}.Name = '{name}'", "object": obj, "field": field, "target": REC,
             "target_label": "Employee", "person_kind": "employee", "role": field.lower(), "fits": fits,
             "cues": list(cues), "records": records, "others": [_other(f, n) for f, n in others],
             "scope": "all other conditions of this query"}
    res = {"query_id": qid, "object": obj, "count": records, "applied": {"filters": [check["filter"]]},
           "lookup_check": [check]}
    return _ev("count_records", res, qid=qid, obj=obj, fields_used=[(obj, field), (REC, "Name")])


DRAFT = "Nobody reports to Mike Oscar: no employee has him as manager (`Manager__c`: 0).\n" + SOURCE
SR = VIEWS[REC][0]


def _target_message(counted=" The results count 3 Recruiter__c records with Mike Oscar in `Assigned_Sr_Recruiter__c`."):
    return fill(RP.RECORDS_RELATION_LOOKUP_MESSAGE, WORD="reports", OBJECT=REC, FIELD="Assigned_Sr_Recruiter__c",
                LABEL="Reporting to", FROM_SOURCE=SR["from_source"], FROM_TARGET=SR["from_target"],
                USED="`Manager__c` (Manager)", NAME="Mike Oscar", COUNTED=counted)


def test_wording_words_come_from_the_label_and_both_texts_without_the_object_names():
    assert _wording(SR) == {"report", "senior"}
    assert _wording(VIEWS[REC][1]) == {"manag"} and _wording(VIEWS[REC][2]) == {"team", "lead", _stem("members")}
    assert _wording({**SR, "object_words": []}) >= {"employ", "recruit"} and _wording(None) == set()


def test_relation_word_of_the_question_on_another_lookup_than_the_counted_one():
    """'who reports to X' counted through Manager__c (0): 'reports' is the wording of the reporting line, so the
    message names that lookup and the direction the question asks for (the records that point to X)."""
    out = relation_lookup_nudges(DRAFT, [_count_through()], "who reports to mike oscar", lookups)
    assert out == [_target_message()]
    assert '{"field": "Assigned_Sr_Recruiter__c", "op": "eq", "value": "Mike Oscar"}' in out[0]
    assert "not in that of `Manager__c` (Manager), which your answer used" in out[0]


def test_relation_word_with_a_count_grouped_by_a_lookup_and_no_person_filter():
    """No lookup check (a count grouped by Manager__c): the person comes from the triage's people stored on the object,
    the count is unknown (no COUNTED part)."""
    grouped = _ev("group_records", {"query_id": "q1", "object": REC, "groups": [{"group": "Papa Quebec", "value": 2}]},
                  fields_used=[(REC, "Manager__c")])
    people = [{"words": "mike oscar", "recs": [(REC, "a06000000000001AAA", "Mike Oscar", "Active Employee")]}]
    out = relation_lookup_nudges(DRAFT, [grouped], "who reports to mike oscar", lookups, people=people)
    assert out == [_target_message(counted="")]
    assert relation_lookup_nudges(DRAFT, [grouped], "who reports to mike oscar", lookups) == []   # no person: none
    two = people + [{"words": "kilo zephyr", "recs": [(REC, "a06000000000002AAA", "Kilo Zephyr", "")]}]
    assert relation_lookup_nudges(DRAFT, [grouped], "does kilo zephyr report to mike oscar", lookups, people=two) == []


def test_relation_word_reads_the_persons_own_value_and_an_unclear_order():
    out = relation_lookup_nudges(DRAFT, [_count_through()], "who does mike oscar report to", lookups)
    assert out == [fill(RP.RECORDS_RELATION_LOOKUP_SOURCE_MESSAGE, WORD="report", OBJECT=REC,
                        FIELD="Assigned_Sr_Recruiter__c", LABEL="Reporting to", FROM_SOURCE=SR["from_source"],
                        FROM_TARGET=SR["from_target"], USED="`Manager__c` (Manager)", NAME="Mike Oscar")]
    out = relation_lookup_nudges(DRAFT, [_count_through()], "what is mike oscar's reporting line", lookups)
    assert len(out) == 1 and out[0].startswith("The question's word 'reporting' is in the knowledge wording of "
                                               "`Recruiter__c.Assigned_Sr_Recruiter__c`")
    assert "The question can read either direction" in out[0] and "The results count 3" in out[0]


@pytest.mark.parametrize("question", [
    "who is mike oscar",                                   # no relation word at all
    "how many employees does mike oscar have",             # "employees" names the records, not a relation
    "what is the department of mike oscar",
    "who does mike oscar manage",                          # the counted lookup carries the word
    "is mike oscar managed by papa quebec",
])
def test_relation_word_check_stays_quiet_on_unrelated_words(question):
    assert relation_lookup_nudges(DRAFT, [_count_through()], question, lookups) == []


def test_relation_word_check_needs_an_open_lookup_and_no_role_fit():
    q = "who reports to mike oscar"
    assert relation_lookup_nudges(DRAFT, [_count_through()], q, None) == []                    # no catalog view
    assert relation_lookup_nudges(DRAFT, [_count_through(fits="manage", cues=("manage",))], q, lookups) == []
    named = "0 through `Manager__c`; 3 employees have him in `Assigned_Sr_Recruiter__c`.\n" + SOURCE
    assert relation_lookup_nudges(named, [_count_through()], q, lookups) == []                 # the draft names it
    listed = _ev("list_records", {"query_id": "q2", "object": REC, "rows": [], "returned": 0}, qid="q2",
                 fields_used=[(REC, "Assigned_Sr_Recruiter__c")])
    assert relation_lookup_nudges(DRAFT, [_count_through(), listed], q, lookups) == []        # a call used it
    zero = _count_through(others=(("Assigned_Sr_Recruiter__c", 0),))
    assert relation_lookup_nudges(DRAFT, [zero], q, lookups) == []        # 0 both ways under the same conditions
    # 5 through the manager line but 0 through the reporting line: the answer's count stands, no message (review of
    # 2026-10-22: a message would steer a count with records to a lookup at 0)
    five = _count_through(records=5, others=(("Assigned_Sr_Recruiter__c", 0),))
    assert relation_lookup_nudges("5 employees report to Mike Oscar (`Manager__c`).", [five], q, lookups) == []
    several = _count_through()
    several.model_result["lookup_check"][0]["filter"] = "Manager__c.Name IN ('Mike Oscar', 'Kilo Zephyr')"
    assert relation_lookup_nudges(DRAFT, [several], q, lookups) == []                          # two people


TEAM_DRAFT = "3 employees report to Mike Oscar (`Assigned_Sr_Recruiter__c`).\n" + SOURCE


def _team_count(team_lead=0):
    """A count of the employees who report to Mike Oscar (Assigned_Sr_Recruiter__c, 3) with the other lookups' counts
    under the same conditions (Manager__c 0, Team_Lead__c ``team_lead``)."""
    return _count_through(field="Assigned_Sr_Recruiter__c", records=3,
                          others=(("Manager__c", 0), ("Team_Lead__c", team_lead)))


@pytest.mark.parametrize("question", [
    "who is in mike oscar's team", "how big is mike oscar's team", "list the team of mike oscar",
    "who are mike oscar's team members", "who are the team members of mike oscar",
    "how many people are in mike oscar's team", "mike oscar ki team mein kitne log hai",
    "which team does mike oscar lead",
])
def test_a_team_question_answered_from_the_reporting_line_gets_no_message(question):
    """Review of 2026-10-22: 'team' is in the wording of Team_Lead__c ("team lead of the employee", "team members led
    by this employee"), but the person has 0 through it while the answer's lookup has 3: the answer stands. Before,
    these got the own-value message for Team_Lead__c (the wrong direction, a lookup at 0)."""
    assert relation_lookup_nudges(TEAM_DRAFT, [_team_count()], question, lookups) == []


def test_half_a_label_or_a_word_only_one_text_has_reads_as_either_direction():
    """With records through Team_Lead__c (2): 'team' alone is half of the label "Team Lead", and "members" is only in
    the from_target text, so neither question reads as the person's own value: the unclear form, with the count."""
    unclear = [fill(RP.RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE, WORD=w, OBJECT=REC, FIELD="Team_Lead__c",
                    LABEL="Team Lead", FROM_SOURCE="team lead of the employee",
                    FROM_TARGET="team members led by this employee", USED="`Assigned_Sr_Recruiter__c` (Reporting to)",
                    NAME="Mike Oscar",
                    COUNTED=" The results count 2 Recruiter__c records with Mike Oscar in `Team_Lead__c`.")
               for w in ("team", "team members")]
    assert relation_lookup_nudges(TEAM_DRAFT, [_team_count(2)], "who is in mike oscar's team", lookups) == unclear[:1]
    assert relation_lookup_nudges(TEAM_DRAFT, [_team_count(2)], "who are mike oscar's team members",
                                  lookups) == unclear[1:]
    # the whole label in the question: the person's own value
    out = relation_lookup_nudges(TEAM_DRAFT, [_team_count(2)], "who is mike oscar's team lead", lookups)
    assert out == [fill(RP.RECORDS_RELATION_LOOKUP_SOURCE_MESSAGE, WORD="team lead", OBJECT=REC, FIELD="Team_Lead__c",
                        LABEL="Team Lead", FROM_SOURCE="team lead of the employee",
                        FROM_TARGET="team members led by this employee",
                        USED="`Assigned_Sr_Recruiter__c` (Reporting to)", NAME="Mike Oscar")]


def test_the_direction_reads_the_label_words_the_question_has():
    """Review of 2026-10-23: with records through Team_Lead__c (2) and both words of its label in the question, the
    direction reads those words ("team", "lead"), so "members", a word only its from_target text has, still makes the
    own-value reading unclear. Read through every hit word ("members" among them) it was the own-value form."""
    q = "who is the team lead of mike oscar's team members"
    assert relation_lookup_nudges(TEAM_DRAFT, [_team_count(2)], q, lookups) == [
        fill(RP.RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE, WORD="team lead members", OBJECT=REC, FIELD="Team_Lead__c",
             LABEL="Team Lead", FROM_SOURCE="team lead of the employee",
             FROM_TARGET="team members led by this employee", USED="`Assigned_Sr_Recruiter__c` (Reporting to)",
             NAME="Mike Oscar",
             COUNTED=" The results count 2 Recruiter__c records with Mike Oscar in `Team_Lead__c`.")]


def test_relation_word_check_leaves_a_lookup_about_people_shows_to_the_direction_check():
    """about_people showed the person's own reporting line and the draft gives it: the direction check said nothing,
    and the relation check does not second-guess it although a count went through Manager__c."""
    q = "who does mike oscar report to"
    draft = "Mike Oscar reports to Victor Whiskey; no employee has him as manager (`Manager__c`: 0).\n" + SOURCE
    people = _ev("about_people", {**_people_res("Mike Oscar", "mike oscar"), "query_id": "q2"}, qid="q2")
    people.model_result["people"][0]["profile"]["Assigned_Sr_Recruiter__c (Reporting to)"] = "Victor Whiskey"
    ev = [_count_through(), people]
    assert direction_nudges(draft, ev, q, lambda o, f: {k: v for k, v in SR.items() if k != "object_words"}
                            if f == SR["field"] else None, lookups=lookups) == []
    assert relation_lookup_nudges(draft, ev, q, lookups) == []
    assert len(relation_lookup_nudges(draft, [_count_through()], q, lookups)) == 1   # without about_people: the message


def test_relation_word_on_a_lookup_to_another_object_reads_only_the_records_that_point_to_the_person():
    count = _count_through(field="Interviewer__c", obj=II, others=(("Shadow__c", 2),))
    draft = "Mike Oscar took 0 mock interviews (`Interviewer__c`).\n" + SOURCE
    out = relation_lookup_nudges(draft, [count], "how many mock interviews did mike oscar shadow", lookups)
    assert len(out) == 1 and out[0].startswith("The question's word 'shadow' is in the knowledge wording of "
                                               f"`{II}.Shadow__c` (Shadow")
    assert f"The question asks for the {II} records whose `Shadow__c` is Mike Oscar" in out[0]
    # "who shadowed X's interviews" asks about the other people of X's interviews: no reading puts X in Shadow__c
    assert _direction_side("who shadowed mike oscar's interviews", "mike oscar", {"shadow"},
                           VIEWS[II][1]["from_source"], VIEWS[II][1]["from_target"]) == "source"
    assert relation_lookup_nudges(draft, [count], "who shadowed mike oscar's interviews", lookups) == []


# ---------------------------------------------------------------- the direction check reads the knowledge wording
#: The same reporting line with a label that has no relation word (the words are only in the knowledge texts).
PLAIN = {**SR, "label": "Assigned Sr Recruiter"}


def _plain_directions(obj, field):
    return {k: v for k, v in PLAIN.items() if k not in ("field", "object_words")} if field == PLAIN["field"] else None


def _mike_people():
    refs = [{"person": "P1", "record": "R1", "object": REC, "field": "Assigned_Sr_Recruiter__c",
             "role": "reporting senior recruiter", "records": 3}]
    res = _people_res("Mike Oscar", "mike oscar", refs)
    res["people"][0]["profile"]["Assigned_Sr_Recruiter__c (Assigned Sr Recruiter)"] = "Victor Whiskey"
    return [_ev("about_people", res)]


def test_the_direction_check_reads_the_knowledge_texts_when_the_label_has_no_word_of_the_question():
    draft = "Mike Oscar reports to Victor Whiskey.\n" + SOURCE
    q = "who reports to mike oscar"
    assert direction_nudges(draft, _mike_people(), q, _plain_directions) == []          # label words only: none
    out = direction_nudges(draft, _mike_people(), q, _plain_directions, lookups=lambda o: [PLAIN] if o == REC else [])
    assert len(out) == 1 and out[0].startswith("The question asks for the other Recruiter__c records whose "
                                               "`Assigned_Sr_Recruiter__c` (Assigned Sr Recruiter) is Mike Oscar")
    # the names of the objects in the texts are no relation words
    assert direction_nudges(draft, _mike_people(), "how many employees does mike oscar have", _plain_directions,
                            lookups=lambda o: [PLAIN]) == []


# ---------------------------------------------------------------------------- the record session's view and check
@pytest.fixture()
def gtools(tmp_path):
    pytest.importorskip("duckdb")
    sys.path.insert(0, str(ROOT / "tests"))
    from groupb_fixture import open_groupb_fixture
    t = open_groupb_fixture(tmp_path)
    yield t
    t.close()


def test_person_lookups_and_the_draft_check_on_the_fixture(gtools):
    s = RecordSession(gtools, _cfg(), llm=object())
    views = {v["field"]: v for v in s.person_lookups(REC)}
    sr = views["Assigned_Sr_Recruiter__c"]
    assert sr["label"] == "Reporting to" and sr["target"] == REC and sr["person"] is True
    assert sr["from_target"] == "employees reporting to this senior recruiter"
    assert {"employee", "recruiter"} <= set(sr["object_words"]) and "Manager__c" in views
    assert all(v["field"] not in ("CreatedById", "LastModifiedById", "OwnerId") for v in views.values())
    assert s.person_lookups("Nope__c") == [] and RecordSession(SimpleNamespace(close=lambda: None), _cfg(),
                                                               llm=object()).person_lookups(REC) == []
    q = "who reports to papa quinn"
    gtools.begin_question(q)
    gtools.call("count_records", {"object": REC, "filters": [{"field": "Manager__c", "op": "eq",
                                                              "value": "Papa Quinn"}]})
    draft = "Nobody reports to Papa Quinn: no employee has Papa Quinn as manager (`Manager__c`).\n" + SOURCE
    msg = s.check_draft(draft, q, gtools.status())
    assert ("The question's word 'reports' is in the knowledge wording of `Recruiter__c.Assigned_Sr_Recruiter__c` "
            "(Reporting to") in msg
    assert "The results count 1 Recruiter__c records with Papa Quinn in `Assigned_Sr_Recruiter__c`." in msg
    # the question's own lookup counted: no relation message
    gtools.begin_question(q)
    gtools.call("count_records", {"object": REC, "filters": [{"field": "Assigned_Sr_Recruiter__c", "op": "eq",
                                                              "value": "Papa Quinn"}]})
    msg = s.check_draft("1 employee reports to Papa Quinn (`Assigned_Sr_Recruiter__c`).\n" + SOURCE, q,
                        gtools.status())
    assert "knowledge wording" not in (msg or "")
