"""Group B people fixes of the AGENT lane (docs/people.md): items 10, 11 and 13. Offline: fake evidence, fake tools,
a scripted LLM; fake names only.

- item 10a: :func:`relation_wording_nudges` (a relation word that no lookup holding the named person carries);
- item 10b: :func:`direction_nudges` (which direction of a self-lookup the question asks about);
- item 11: :func:`with_status_line` (the status split with the values no record has, in ``final`` only);
- item 13: :meth:`RecordSession.triage` keeps a first name in play that both calls missed (``tools.first_name_fit``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sf_assistant.agent import record_prompts as RP
from sf_assistant.agent.nudges import (_completion_cues, _direction_side, _label_stems, _stem, direction_nudges,
                                       relation_wording_nudges, with_status_line)
from sf_assistant.agent.record_session import RecordSession
from sf_assistant.agent.triage import Triage
from sf_assistant.config import load_config
from sf_assistant.engine.types import QueryEvidence, RecordsStatus

ROOT = Path(__file__).resolve().parents[1]
REC = "Recruiter__c"
II = "Internal_Interview__c"
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."

#: The catalog view of the record session (RecordSession.lookup_directions), for three self-lookups of the employee.
LOOKUPS = {
    (REC, "Assigned_Sr_Recruiter__c"): {"label": "Reporting to", "target": REC, "person": True,
                                        "from_source": "senior recruiter this employee reports to",
                                        "from_target": "employees reporting to this senior recruiter"},
    (REC, "Manager__c"): {"label": "Manager", "target": REC, "person": True, "from_source": "manager of the employee",
                          "from_target": "employees managed by this manager"},
    (REC, "Team_Lead__c"): {"label": "Team Lead", "target": REC, "person": True,
                            "from_source": "team lead of the employee",
                            "from_target": "team members led by this employee"},
    (REC, "Department__c"): {"label": "Department", "target": "Department__c", "person": False,
                             "from_source": "", "from_target": "employees in the department"},
}


def directions(obj, field):
    return LOOKUPS.get((obj, field))


def _ev(tool, result, qid="q1", obj=REC, fields_used=()):
    return QueryEvidence(query_id=qid, tool=tool, kind="data", object=obj, arguments={}, ok=True, error=None,
                         applied=result.get("applied") or {}, notes=[], statements=[], tables=[obj], synced_at={},
                         summary="", model_result=result, elapsed_ms=1.0, fields_used=list(fields_used))


def _profile(reporting=None, manager=None, lead=None):
    out = {"Employment_Status__c (Employment Status)": "Active Employee", "Department__c (Department)": "Ops"}
    if reporting:
        out["Assigned_Sr_Recruiter__c (Reporting to)"] = reporting
    if manager:
        out["Manager__c (Manager)"] = manager
    if lead:
        out["Team_Lead__c (Team Lead)"] = lead
    return out


def _people_result(people, asked, links=(), references=()):
    """An about_people result: ``people`` = [(ref, label, profile)]; each person's main record is an Employee."""
    items, matched = [], []
    for i, (ref, label, prof) in enumerate(people, 1):
        items.append({"ref": ref, "asked": asked[0], "label": label, "kind": "employee", "match": "full name",
                      "records": [f"R{i}"], "profile": prof})
        matched.append({"ref": f"R{i}", "person": ref, "label": label, "object": REC, "object_label": "Employee",
                        "how": "name"})
    return {"asked": list(asked), "people": items, "matched_records": matched, "references": list(references),
            "system_references": [], "links": list(links)}


def _pair_evidence():
    """Kilo Zephyr and Lima Nobel: both have Papa Quebec as Manager__c and Mike Oscar as Team_Lead__c; their reporting
    lines differ (Mike Oscar, Romeo Sierra)."""
    res = _people_result([("P1", "Kilo Zephyr", _profile("Mike Oscar", "Papa Quebec", "Mike Oscar")),
                          ("P2", "Lima Nobel", _profile("Romeo Sierra", "Papa Quebec", "Mike Oscar"))],
                         ["kilo zephyr", "lima nobel"])
    return [_ev("about_people", res)]


# ------------------------------------------------------------------------------------------- item 10a: wording
@pytest.mark.parametrize("draft", [
    "**Same Manager:** Both report to **Papa Quebec**.",
    "- Both report to Papa Quebec (`Manager__c`).",            # an API name is no relation word
])
def test_relation_word_nudge_for_a_manager_called_the_reporting_line(draft):
    out = relation_wording_nudges(draft + "\n" + SOURCE, _pair_evidence(), directions)
    assert len(out) == 1
    msg = out[0]
    assert msg.startswith('Your answer says "Both report to Papa Quebec')
    assert "'Papa Quebec' is `Manager__c` (Manager) of Kilo Zephyr and Lima Nobel" in msg
    assert "'report' is the label or role of `Assigned_Sr_Recruiter__c` (Reporting to: 'Mike Oscar' for Kilo Zephyr, " \
           "'Romeo Sierra' for Lima Nobel)" in msg


def test_relation_word_nudge_for_a_reporting_line_called_the_manager():
    out = relation_wording_nudges("Mike Oscar is Kilo Zephyr's manager.\n" + SOURCE, _pair_evidence(), directions)
    assert len(out) == 1
    assert "'Mike Oscar' is `Assigned_Sr_Recruiter__c` (Reporting to) of Kilo Zephyr; `Team_Lead__c` (Team Lead) of " \
           "Kilo Zephyr" in out[0]
    assert "'manager' is the label or role of `Manager__c` (Manager: 'Papa Quebec' for Kilo Zephyr" in out[0]


@pytest.mark.parametrize("draft", [
    "Kilo Zephyr reports to Mike Oscar; his manager is Papa Quebec.",
    "Both have Papa Quebec as their manager (`Manager__c`) and Mike Oscar as their team lead (`Team_Lead__c`).",
    "Kilo Zephyr (reporting to Mike Oscar, manager Papa Quebec) and Lima Nobel (reporting to Romeo Sierra).",
    "Papa Quebec manages both of them and Mike Oscar leads their team.",
    "They work in the same department.",
    "Both are on Papa Quebec's team.",                          # "team" alone is no "Team Lead"
])
def test_relation_word_nudge_passes_right_wording(draft):
    assert relation_wording_nudges(draft + "\n" + SOURCE, _pair_evidence(), directions) == []


def test_relation_word_nudge_names_a_two_word_relation():
    out = relation_wording_nudges("Papa Quebec is the team lead of both.", _pair_evidence(), directions)
    assert len(out) == 1 and "'team lead' is the label or role of `Team_Lead__c` (Team Lead: 'Mike Oscar' for " \
                             "Kilo Zephyr, 'Mike Oscar' for Lima Nobel)" in out[0]


def test_relation_word_nudge_needs_the_catalog_view_and_about_people():
    assert relation_wording_nudges("Both report to Papa Quebec.", _pair_evidence(), None) == []
    count = _ev("count_records", {"count": 3, "applied": {"filters": ["Manager__c.Name = 'Papa Quebec'"]}})
    assert relation_wording_nudges("Both report to Papa Quebec.", [count], directions) == []


def test_relation_word_nudge_reads_the_links_when_profiles_were_cut():
    link = {"between": ["P1", "P2"], "how": "shared parent",
            "path": "Recruiter__c.Manager__c (the same Employee on both own records)", "records": 1,
            "examples": ["Papa Quebec"], "text": "P1 (Kilo Zephyr) and P2 (Lima Nobel) have the same `Manager__c` "
                                                  "(manager): 'Papa Quebec'"}
    cut = {"Employment_Status__c (Employment Status)": "Active Employee"}
    res = _people_result([("P1", "Kilo Zephyr", dict(cut, **{"Assigned_Sr_Recruiter__c (Reporting to)": "Mike Oscar"})),
                          ("P2", "Lima Nobel", dict(cut))], ["kilo zephyr", "lima nobel"], links=[link])
    out = relation_wording_nudges("Both report to Papa Quebec.", [_ev("about_people", res)], directions)
    assert len(out) == 1 and "'Papa Quebec' is `Manager__c` (Manager) of Kilo Zephyr and Lima Nobel" in out[0]


def test_stems_and_label_words():
    assert {_stem(w) for w in ("reporting", "reports", "reported", "report")} == {"report"}
    assert {_stem(w) for w in ("manager", "managed", "manages", "manage", "managing")} == {"manag"}
    assert _stem("Kilo's") == "kilo" and _stem("interviewed") == _stem("interviewer") == "interview"
    assert _label_stems("Reporting to") == {"report"} and _label_stems("Team Lead") == {"team", "lead"}


# ----------------------------------------------------------------------------------------- item 10b: direction
@pytest.mark.parametrize("question,lookup,side", [
    ("who reports to mike oscar", "Assigned_Sr_Recruiter__c", "target"),
    ("which employees report to mike oscar?", "Assigned_Sr_Recruiter__c", "target"),
    ("who are mike oscar's direct reports", "Assigned_Sr_Recruiter__c", "target"),
    ("who does mike oscar report to", "Assigned_Sr_Recruiter__c", "source"),
    ("mike oscar reports to whom?", "Assigned_Sr_Recruiter__c", "source"),
    ("who is mike oscar's manager", "Manager__c", "source"),
    ("who manages mike oscar", "Manager__c", "source"),
    ("who does mike oscar manage", "Manager__c", "target"),
    ("who is managed by mike oscar", "Manager__c", "target"),
    ("who is mike oscar's team lead", "Team_Lead__c", "source"),
    ("who has mike oscar as team lead", "Team_Lead__c", "target"),
    ("who are mike oscar's team members", "Team_Lead__c", "unclear"),     # a word only the reverse text has
    ("what is mike oscar's reporting line", "Assigned_Sr_Recruiter__c", "unclear"),   # a noun neither text has
    ("who is mike oscar", "Assigned_Sr_Recruiter__c", None),               # no relation word: no direction
])
def test_direction_side_from_the_word_order(question, lookup, side):
    info = LOOKUPS[(REC, lookup)]
    assert _direction_side(question, "Mike Oscar", _label_stems(info["label"]), info["from_source"],
                           info["from_target"]) == side


def _mike(references=True, reporting="Victor Whiskey", manager="Papa Quebec"):
    refs = [{"person": "P1", "record": "R1", "object": REC, "field": "Assigned_Sr_Recruiter__c",
             "role": "reporting senior recruiter", "meaning": "employees reporting to this senior recruiter",
             "records": 3}] if references else []
    return [_ev("about_people", _people_result([("P1", "Mike Oscar", _profile(reporting, manager))], ["mike oscar"],
                                               references=refs))]


def test_direction_nudge_for_the_reports_of_a_person():
    q = "who reports to mike oscar"
    out = direction_nudges("Mike Oscar reports to Victor Whiskey.\n" + SOURCE, _mike(), q, directions)
    assert out == [RP.RECORDS_DIRECTION_MESSAGE
                   .replace("<<COUNTED>>", " about_people counts 3 Recruiter__c records with Mike Oscar in "
                                           "`Assigned_Sr_Recruiter__c`.")
                   .replace("<<FIELD>>", "Assigned_Sr_Recruiter__c").replace("<<LABEL>>", "Reporting to")
                   .replace("<<NAME>>", "Mike Oscar").replace("<<OBJECT>>", REC).replace("<<VALUE>>", "Victor Whiskey")
                   .replace("<<FROM_SOURCE>>", "senior recruiter this employee reports to")
                   .replace("<<FROM_TARGET>>", "employees reporting to this senior recruiter")]
    assert '{"field": "Assigned_Sr_Recruiter__c", "op": "eq", "value": "Mike Oscar"}' in out[0]
    # the list ran: no nudge
    listed = _ev("list_records", {"rows": [], "returned": 0, "total_count": 0}, qid="q2",
                 fields_used=[(REC, "Assigned_Sr_Recruiter__c")])
    assert direction_nudges("Nobody.\n" + SOURCE, _mike() + [listed], q, directions) == []
    # no reference through the lookup: the same message without the count
    out = direction_nudges("Mike Oscar reports to Victor Whiskey.", _mike(references=False), q, directions)
    assert len(out) == 1 and "about_people counts" not in out[0]


def test_direction_nudge_for_the_persons_own_value():
    q = "who does mike oscar report to"
    assert direction_nudges("Mike Oscar reports to Victor Whiskey (`Assigned_Sr_Recruiter__c`).", _mike(), q,
                            directions) == []
    out = direction_nudges("3 employees report to Mike Oscar.", _mike(), q, directions)
    assert len(out) == 1 and out[0].startswith("The question asks for Mike Oscar's own `Assigned_Sr_Recruiter__c` "
                                               "(Reporting to: \"senior recruiter this employee reports to\"), which "
                                               "is Victor Whiskey on Mike Oscar's Recruiter__c record.")
    assert direction_nudges("Papa Quebec is his manager.", _mike(), "who is mike oscar's manager", directions) == []


def test_direction_nudge_when_the_word_order_does_not_tell():
    q = "what is mike oscar's reporting line"
    out = direction_nudges("Mike Oscar reports to Victor Whiskey.", _mike(), q, directions)
    assert len(out) == 1 and out[0].startswith("The question can read either direction of "
                                               "`Recruiter__c.Assigned_Sr_Recruiter__c` (Reporting to)")
    assert direction_nudges("Mike Oscar reports to Victor Whiskey (`Assigned_Sr_Recruiter__c`).", _mike(), q,
                            directions) == []


def test_direction_nudge_needs_one_asked_name_a_self_lookup_and_the_catalog_view():
    assert direction_nudges("Mike Oscar reports to Victor Whiskey.", _mike(), "who reports to mike oscar", None) == []
    two = _people_result([("P1", "Mike Oscar", _profile("Victor Whiskey")), ("P2", "Kilo Zephyr", _profile("Mike Oscar"))],
                         ["mike oscar", "kilo zephyr"])
    assert direction_nudges("x", [_ev("about_people", two)], "does kilo zephyr report to mike oscar", directions) == []
    # a lookup to another object (Department__c) is no self-lookup
    dept = {**LOOKUPS[(REC, "Department__c")], "label": "Reports"}
    assert direction_nudges("x", _mike(), "who reports to mike oscar",
                            lambda o, f: dept if f == "Assigned_Sr_Recruiter__c" else None) == []


# --------------------------------------------------------------------------------------------- item 11: status
STATUS = {II: {"label": "Internal Interview", "field": "Status__c", "field_label": "Status",
               "values": [("Unscheduled", "Unscheduled"), ("Scheduled", "Scheduled"), ("Completed", "Completed"),
                          ("No Show", "No Show")]},
          "Session__c": {"label": "Session", "field": "Status__c", "field_label": "Status",
                         "values": [("Scheduled", "Scheduled"), ("Completed", "Completed")]}}


def status_of(obj):
    return STATUS.get(obj)


def _check(fits="taken"):
    return [{"filter": "Interviewer__c.Name = 'Kilo Zephyr'", "object": II, "field": "Interviewer__c",
             "target": REC, "person_kind": "employee", "role": "interviewer", "fits": fits, "cues": [fits],
             "records": 8, "others": []}]


def _count_ev(breakdown=None, fits="taken"):
    res = {"count": 8, "counted": "Internal_Interview__c records",
           "breakdown": breakdown or {"Status__c": {"Unscheduled": 4, "(blank)": 4}}, "lookup_check": _check(fits),
           "applied": {"filters": ["Interviewer__c.Name = 'Kilo Zephyr'"]}}
    return [_ev("count_records", res, obj=II, fields_used=[(II, "Interviewer__c")])]


LINE = ("Status of these Internal Interview records (`Status__c`, 8): blank 4, Unscheduled 4; none is Scheduled, "
        "Completed or No Show.")


def test_status_line_for_a_count_whose_verb_can_mean_finished_records():
    text = "Kilo Zephyr has taken **8** mock interviews.\n" + SOURCE
    out = with_status_line(text, _count_ev(), "how many mocks has kilo zephyr taken", status_of)
    assert out == "Kilo Zephyr has taken **8** mock interviews.\n" + LINE + "\n" + SOURCE
    assert with_status_line("Kilo Zephyr has taken 8 mocks.", _count_ev(), "how many mocks has kilo taken",
                            status_of) == "Kilo Zephyr has taken 8 mocks.\n" + LINE   # no Source line: at the end


@pytest.mark.parametrize("text,question,status", [
    ("Kilo Zephyr has taken 8 mocks; 0 completed.", "how many mocks has kilo taken", True),   # a zero value named
    ("Kilo Zephyr has 8 mocks.", "how many mocks does kilo have", True),                      # no completion cue
    ("Kilo Zephyr has taken 8 mocks.", "how many mocks has kilo taken, by status", True),     # the split is asked
    ("Kilo Zephyr has taken 8 mocks.", "how many mocks has kilo taken", False),               # no catalog view
    ("", "how many mocks has kilo taken", True),
])
def test_no_status_line(text, question, status):
    assert with_status_line(text, _count_ev(), question, status_of if status else None) == text


def test_no_status_line_when_the_used_lookup_does_not_fit_the_cue_or_every_value_has_records():
    q = "how many mocks has kilo taken"
    assert with_status_line("x 8.", _count_ev(fits="supported"), q, status_of) == "x 8."
    full = {"Status__c": {"Unscheduled": 2, "Scheduled": 2, "Completed": 2, "No Show": 2}}
    assert with_status_line("x 8.", _count_ev(breakdown=full), q, status_of) == "x 8."
    other = {"Status__c": {"Completed": 7, "(other)": 1}}   # a cut split: the values with no record are unknown
    assert with_status_line("x 8.", _count_ev(breakdown=other), q, status_of) == "x 8."


def _link_ev(int_example="INT-0001 (Unscheduled)"):
    links = [{"between": ["P1", "P2"], "how": "same record", "path": f"{II}.Interviewer__c + {II}.Candidate__c",
              "records": 1, "examples": [int_example],
              "text": "P1 (Kilo Zephyr) is `Interviewer__c` (interviewer) and P2 (Lima Nobel) is `Candidate__c` "
                      "(candidate) on 1 Internal Interview records"},
             {"between": ["P1", "P2"], "how": "same record", "path": "Session__c.Host_User__c + Session__c.Candidate__c",
              "records": 1, "examples": ["SES-0001 (Scheduled)"],
              "text": "P1 (Kilo Zephyr) is `Host_User__c` (host employee) and P2 (Lima Nobel) is `Candidate__c` "
                      "(candidate) on 1 Session records"}]
    res = _people_result([("P1", "Kilo Zephyr", {}), ("P2", "Lima Nobel", {})], ["kilo", "lima"], links=links)
    return [_ev("about_people", res)]


def test_status_line_for_a_link_whose_roles_fit_the_cue():
    text = ("Yes, Kilo Zephyr interviewed Lima Nobel.\n- 1 Internal Interview record: `INT-0001` (Unscheduled).\n"
            "- 1 Session record: `SES-0001` (Scheduled).\n" + SOURCE)
    out = with_status_line(text, _link_ev(), "did kilo interview lima?", status_of)
    line = ("Status of these Internal Interview records (`Status__c`, 1): Unscheduled 1; none is Scheduled, Completed "
            "or No Show.")
    assert out == text.replace(SOURCE, line + "\n" + SOURCE)   # "(Scheduled)" of the session says nothing of the mock
    # a host link does not fit "interview"; an example status the field does not have is no split
    assert with_status_line(text, _link_ev("INT-0001 (Assigned)"), "did kilo interview lima?", status_of) == text


def test_status_line_from_a_grouped_count_and_a_full_list():
    q = "how many mocks did kilo take"
    grouped = {"grouped_by": "Status__c", "measure": "count", "groups": [{"group": "Completed", "value": 3},
                                                                        {"group": "(blank)", "value": 1}],
               "group_count": 2, "shown": 2, "total_across_groups": 4, "lookup_check": _check("take")}
    out = with_status_line("Kilo took 4 mocks.", [_ev("group_records", grouped, obj=II)], q, status_of)
    assert out.endswith("Status of these Internal Interview records (`Status__c`, 4): Completed 3, blank 1; none is "
                        "Unscheduled, Scheduled or No Show.")
    rows = {"columns": ["Name", "Status__c"], "rows": [{"Name": "INT-1", "Status__c": "Scheduled"},
                                                       {"Name": "INT-2", "Status__c": "Scheduled"}],
            "returned": 2, "total_count": 2, "truncated": False, "lookup_check": _check("take")}
    out = with_status_line("Kilo took 2 mocks: INT-1, INT-2.", [_ev("list_records", rows, obj=II)], q, status_of)
    assert out.endswith("(`Status__c`, 2): Scheduled 2; none is Unscheduled, Completed or No Show.")
    cut = dict(rows, total_count=5, truncated=True)
    assert with_status_line("x", [_ev("list_records", cut, obj=II)], q, status_of) == "x"


def test_completion_cues_come_from_the_existing_stems():
    assert _completion_cues("how many mocks has kilo taken") == ["taken"]
    assert _completion_cues("did kilo interview lima?") == ["interview"]
    assert _completion_cues("how many sessions did kilo host") == []
    assert _completion_cues("who are the trainers of lima") == ["trainers"]


def test_status_line_is_in_final_only(monkeypatch):
    """pipeline.answer: the status line goes after the lookup footer into ``final``; ``body`` (scored) is unchanged."""
    import sf_assistant.pipeline as pl
    from sf_assistant.agent.record_session import RecordAnswer

    status = RecordsStatus(snapshot_path="x", source="y", copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="f",
                           org_is_sandbox=True, origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC",
                           age_hours=1.0, stale=False, time_zone="UTC", time_zone_from="x", today="2026-10-13",
                           today_pinned=True, objects=1, duckdb_version="1")
    text = "Kilo Zephyr has taken **8** mock interviews (`Interviewer__c`).\n" + SOURCE
    rec = RecordAnswer(question="q", answer=text, no_records=False, used_data=True, evidence=_count_ev(), agent=None,
                       error=None, unsupported_numbers=[], seconds=1.0)

    class Session:
        tools = object()

        def ask(self, q):
            return rec

        def status(self):
            return status

        def status_of(self, obj):
            return status_of(obj)

    o = pl.answer("how many mocks has kilo zephyr taken", None, None, None, None, 1, session=Session(),
                  records_only=True)
    assert o.kind == "records" and o.body == text and o.status_lines == [LINE]
    assert o.final == text.replace(SOURCE, LINE + "\n" + SOURCE)
    j = pl.result_row(o, Session())["jsonl"]
    assert j["answer"] == o.final and j["answer_body"] == text and j["status_lines"] == [LINE]
    assert j["lookup_footer"] is False   # the answer names its lookup: the flag is about the lookup footer only


# ---------------------------------------------------------------------------------- the record session's views
def test_lookup_directions_and_status_of_read_the_catalog():
    rel = {"field": "Manager__c", "direction_semantics": {"from_source": "manager of the employee",
                                                         "from_target": "employees managed by this manager",
                                                       "role": ""}}
    know = SimpleNamespace(relationship_for_field=lambda api: rel if api == "Manager__c" else None)
    mgr = SimpleNamespace(api="Manager__c", label="Manager", kind="reference", from_target="")
    st = SimpleNamespace(api="Status__c", label="Status", kind="picklist",
                         picklist=[("Open", "Open", ""), ("Done", "Finished", "")])
    spec = SimpleNamespace(api=REC, fields={"Manager__c": mgr, "Status__c": st}, knowledge=know)
    cat = SimpleNamespace(objects={REC: spec}, target_of=lambda f: REC, label_of=lambda o: "Employee")
    tools = SimpleNamespace(catalog=cat, _status_field=lambda s: st, close=lambda: None)
    s = RecordSession(tools, load_config(base_url="http://localhost:8000/v1", api_key="k"), llm=object())
    assert s.lookup_directions(REC, "Manager__c") == {"label": "Manager", "target": REC, "person": True,
                                                       "from_source": "manager of the employee",
                                                       "from_target": "employees managed by this manager",
                                                       "role": ""}
    assert s.lookup_directions(REC, "Status__c") is None and s.lookup_directions("Nope__c", "X") is None
    assert s.status_of(REC) == {"label": "Employee", "field": "Status__c", "field_label": "Status",
                                "values": [("Open", "Open"), ("Done", "Finished")]}
    bare = RecordSession(SimpleNamespace(close=lambda: None), load_config(base_url="http://localhost:8000/v1",
                                                                          api_key="k"), llm=object())
    assert bare.lookup_directions(REC, "Manager__c") is None and bare.status_of(REC) is None


@pytest.fixture()
def ptools(tmp_path):
    pytest.importorskip("duckdb")
    sys.path.insert(0, str(ROOT / "tests"))
    from warehouse_fixture import open_people_fixture
    t = open_people_fixture(tmp_path)
    yield t
    t.close()


def test_views_on_the_people_fixture(ptools):
    s = RecordSession(ptools, load_config(base_url="http://localhost:8000/v1", api_key="k"), llm=object())
    d = s.lookup_directions(REC, "Manager__c")
    assert d["label"] == "Manager" and d["target"] == REC and d["person"] is True
    assert s.lookup_directions(REC, "Employment_Status__c") is None
    st = s.status_of(II)
    assert st["field"] == "Status__c" and st["label"] == "Internal Interview" and st["values"]


# ------------------------------------------------------------------------------------------ item 13: triage
class _Llm:
    """Route and names replies by system prompt; any other call fails (no record agent runs in these tests)."""

    def __init__(self, route="NO_RECORDS", names="NONE"):
        self.route, self.names = route, names

    def chat(self, messages, tools=None, max_tokens=None, **_):
        c = messages[0].get("content")
        if c == RP.ROUTE_SYSTEM:
            return {"content": self.route, "tool_calls": []}
        if c == RP.NAMES_SYSTEM:
            if isinstance(self.names, Exception):
                raise self.names
            return {"content": self.names, "tool_calls": []}
        raise AssertionError("no other model call")


class _Spots:
    """Tools whose name index finds the weak first name 'kilo' (one stored person: Kilo Zephyr)."""

    REC_KZ = (REC, "a06000000000020AAA", "Kilo Zephyr", "Active Employee")

    def __init__(self, fit=None):
        self.fit, self.fit_calls = fit, []

    def spot_people(self, question):
        if "kilo" not in question.lower():
            return []
        return [{"words": "kilo", "recs": [self.REC_KZ], "strength": "weak", "first_only": True, "asked": None,
                 "close": []}]

    def resolve_person_name(self, name):
        return None

    def is_design_word(self, word):
        return word in ("field", "session")

    def close(self):
        pass


class _Fit(_Spots):
    def first_name_fit(self, question, spot):
        self.fit_calls.append((question, dict(spot)))
        if isinstance(self.fit, Exception):
            raise self.fit
        return self.fit


FIT = {"word": "hosted", "lookup": "Session__c.Host_User__c", "records": 3, "name": "Kilo Zephyr", "name_word": "kilo"}
Q13 = "which field records that kilo hosted a session"


def _triage(tools, route="NO_RECORDS", names="NONE"):
    return RecordSession(tools, load_config(base_url="http://localhost:8000/v1", api_key="k"),
                         llm=_Llm(route, names)).triage(Q13)


def test_a_first_name_both_calls_missed_stays_in_play_when_the_data_backs_it():
    tools = _Fit(FIT)
    tri = _triage(tools)
    assert tri.route is False and tri.names == [] and tri.routed is True
    assert [p["words"] for p in tri.people] == ["kilo"] and tools.fit_calls[0][0] == Q13
    assert tools.fit_calls[0][1]["words"] == "kilo"
    d = tri.as_dict()
    assert d["first_name_fit"] == [{"words": "kilo", "word": "hosted", "lookup": "Session__c.Host_User__c",
                                    "records": 3}]
    assert "Kilo Zephyr" not in str(d)   # the trace keeps the question's words, never a stored name
    tri = _triage(_Fit(FIT), route="maybe")   # an unclear route: the same
    assert tri.routed is True and [p["words"] for p in tri.people] == ["kilo"]


@pytest.mark.parametrize("tools,route,names,called", [
    (_Fit(None), "NO_RECORDS", "NONE", True),                               # the data does not back it
    (_Fit(RuntimeError("scan failed")), "NO_RECORDS", "NONE", True),         # a fit that raises
    (_Spots(), "NO_RECORDS", "NONE", False),                                 # tools without first_name_fit
    (_Fit(FIT), "NO_RECORDS", RuntimeError("model down"), False),            # the names call failed: as before
    (_Fit(FIT), "RECORDS", "NONE", False),                                   # the route said RECORDS: as before
])
def test_the_triage_stays_as_before_item_13(tools, route, names, called):
    tri = _triage(tools, route=route, names=names)
    assert tri.people == [] and "first_name_fit" not in tri.as_dict()
    assert tri.routed is (route == "RECORDS")
    assert bool(getattr(tools, "fit_calls", [])) is called


def test_a_name_the_names_call_read_is_never_a_first_name_fit():
    class Named(_Fit):
        def resolve_person_name(self, name):
            return {"words": "kilo", "recs": [self.REC_KZ], "strength": "strong", "first_only": True}

    tools = Named(FIT)
    tri = _triage(tools, names="kilo")
    assert [p["words"] for p in tri.people] == ["kilo"] and "first_name_fit" not in tri.as_dict()
    assert tools.fit_calls == [] and Triage(routed=True, route=True).as_dict().get("first_name_fit") is None


# ------------------------------------------------------------------------------- review fixes (2026-10-21)
@pytest.mark.parametrize("question", ["who is mike oscar managed by?", "mike oscar is managed by whom?",
                                      "by whom is mike oscar managed?", "is mike oscar managed by papa quebec?"])
def test_a_passive_question_about_a_person_asks_for_their_own_value(question):
    info = LOOKUPS[(REC, "Manager__c")]
    assert _direction_side(question, "Mike Oscar", _label_stems(info["label"]), info["from_source"],
                           info["from_target"]) == "source"
    assert direction_nudges("Mike Oscar is managed by Papa Quebec (`Manager__c`).", _mike(), question,
                            directions) == []


def test_direction_nudge_for_a_person_with_reports_but_no_own_value():
    refs = [{"person": "P1", "record": "R1", "object": REC, "field": "Team_Lead__c", "role": "team lead",
             "records": 3}]
    ev = [_ev("about_people", _people_result([("P1", "Mike Oscar", _profile())], ["mike oscar"], references=refs))]
    out = direction_nudges("Mike Oscar's team lead is nobody.", ev, "who has mike oscar as team lead", directions)
    assert len(out) == 1 and out[0].startswith("The question asks for the other Recruiter__c records whose "
                                               "`Team_Lead__c` (Team Lead) is Mike Oscar")
    assert "own `Team_Lead__c` (blank)" in out[0] and "about_people counts 3" in out[0]
    # the source side of a blank own value needs no message
    assert direction_nudges("Nobody is recorded.", ev, "who is mike oscar's team lead", directions) == []


def test_relation_facts_read_an_own_record_links_holder():
    from sf_assistant.agent.nudges import _relation_facts
    link = {"between": ["P1", "P2"], "how": "own record", "path": "Recruiter__c.Manager__c (own record)",
            "holder": "P2", "records": 1, "examples": [], "text": "(reworded text)"}
    res = _people_result([("P1", "Papa Quebec", {}), ("P2", "Kilo Zephyr", {})], ["papa", "kilo"], links=[link])
    facts = _relation_facts([_ev("about_people", res)], directions)
    assert [(f["holder"], f["field"], f["value"]) for f in facts] == [("Kilo Zephyr", "Manager__c", "Papa Quebec")]


def test_status_split_roles_come_from_the_catalog_view():
    from sf_assistant.agent.nudges import _link_roles
    lk = {"path": f"{II}.Interviewer__c + {II}.Candidate__c", "text": "no roles here"}
    roles = {(II, "Interviewer__c"): {"role": "interviewer"}, (II, "Candidate__c"): {"role": "candidate"}}
    assert _link_roles(lk, lambda o, f: roles.get((o, f))) == ["interviewer", "candidate"]
    assert _link_roles({"path": "x", "text": "P1 is `A__c` (host employee) and P2 is `B__c` (candidate)"}) == \
        ["host employee", "candidate"]


def test_no_status_line_for_a_count_filtered_on_the_status_itself():
    ev = _count_ev(breakdown={"Status__c": {"Completed": 8}})
    ev[0].arguments = {"filters": [{"field": "Interviewer__c", "op": "eq", "value": "Kilo Zephyr"},
                                   {"field": "Status__c", "op": "eq", "value": "Completed"}]}
    text = "Kilo Zephyr has taken 8 completed mocks."
    assert with_status_line(text, ev, "how many completed mocks has kilo zephyr taken", status_of) == text


def test_no_status_line_when_two_people_share_the_object():
    def cnt(name, n, by, qid):
        chk = _check("taken")
        chk[0]["filter"], chk[0]["records"] = f"Interviewer__c.Name = '{name}'", n
        return _ev("count_records", {"count": n, "breakdown": {"Status__c": by}, "lookup_check": chk}, qid=qid, obj=II)
    ev = [cnt("Kilo Zephyr", 8, {"Completed": 8}, "q1"), cnt("Lima Nobel", 2, {"Unscheduled": 2}, "q2")]
    text = "Kilo Zephyr has taken 8 mocks and Lima Nobel 2."
    assert with_status_line(text, ev, "how many mocks have kilo and lima taken", status_of) == text
    # the same person twice (a count, then a list): one split, the last
    again = cnt("Kilo Zephyr", 8, {"Unscheduled": 8}, "q3")
    assert "Unscheduled 8" in with_status_line("Kilo took 8.", [ev[0], again], "how many mocks has kilo taken",
                                               status_of)


def _paths_ev(records=31, any_path=51):
    lc = {"filter": "Candidate__c.Name = 'Lima Nobel'", "object": "Session__c", "field": "Candidate__c",
          "target": "Account", "person_kind": "candidate", "role": "candidate", "fits": None, "records": records,
          "others": [], "any_path": any_path,
          "paths": [{"path": "Session__c.Candidate_Training__c -> Candidate_Training__c.Candidate__c",
                     "role": "candidate", "fits": None, "records": 31}],
          "related": [{"object": "Session_Attendee__c", "field": "Candidate__c", "via": "Session__c",
                       "role": "candidate", "fits": None, "records": 20, "distinct": 18, "same_role": True}]}
    return [_ev("count_records", {"count": records, "lookup_check": [lc]}, obj="Session__c")]


def test_paths_nudge_when_the_draft_leads_with_the_direct_count():
    from sf_assistant.agent.nudges import paths_nudges
    out = paths_nudges("Lima Nobel has 31 sessions.\n" + SOURCE, _paths_ev())
    assert len(out) == 1 and out[0].startswith("Your answer gives 31 Session__c records with 'Lima Nobel' in "
                                               "`Candidate__c`")
    assert "`Session__c.Candidate_Training__c -> Candidate_Training__c.Candidate__c` 31" in out[0]
    assert "`Session_Attendee__c.Candidate__c` rows (18 Session__c records)" in out[0] and "51" in out[0]
    for ok_draft in ("Lima Nobel has 51 sessions through every path.", "Lima Nobel has 31 sessions through "
                     "`Candidate__c`; through `Candidate_Training__c` there are more.", "No sessions."):
        assert paths_nudges(ok_draft, _paths_ev()) == []
    assert paths_nudges("Lima Nobel has 31 sessions.", _paths_ev(any_path=31)) == []   # no more through the paths


def test_child_copy_nudge_until_the_source_is_used():
    from sf_assistant.agent.nudges import child_copy_nudges
    lc = {"filter": "Assigned_Recruiter__c.Name = 'Kilo Zephyr'", "object": "Account", "field": "Assigned_Recruiter__c",
          "records": 1, "others": [], "copy_source": {"field": "Assigned_Recruiter__c", "source": "Marketing__c.Recruiter__c",
                                                      "via": "Marketing__c.Candidate__c", "records": 3}}
    res = {"count": 1, "lookup_check": [lc],
           "child_copy": [{"field": "Assigned_Recruiter__c", "source": "Marketing__c.Recruiter__c",
                           "via": "Marketing__c.Candidate__c", "filled": 1, "source_filled": 4}]}
    ev = [_ev("count_records", res, obj="Account", fields_used=[("Account", "Assigned_Recruiter__c")])]
    out = child_copy_nudges("Kilo Zephyr is assigned 1 candidate.", ev)
    assert len(out) == 1 and out[0].startswith("Your answer counts `Account.Assigned_Recruiter__c`, a partial copy of "
                                               "`Marketing__c.Recruiter__c`: it is filled on 1 Account records, while 4")
    assert "Through `Marketing__c.Recruiter__c`, 3 different Account records have 'Kilo Zephyr'." in out[0]
    assert "count_distinct `Candidate__c`" in out[0]
    assert child_copy_nudges("Through `Marketing__c.Recruiter__c`: 3 candidates.", ev) == []
    later = _ev("count_records", {"count": 3}, qid="q2", obj="Marketing__c",
                fields_used=[("Marketing__c", "Recruiter__c")])
    assert child_copy_nudges("Kilo Zephyr is assigned 1 candidate.", ev + [later]) == []


def test_several_people_nudge_for_a_first_name_the_spelling_help_read():
    from sf_assistant.agent.nudges import people_nudges
    ppl = [{"words": "killo", "first_only": True, "asked": "killo", "read_as": "Kilo",
            "names": ["Kilo Zephyr", "Kilo Quill"]}]
    out = people_nudges("Killo is Kilo Zephyr, an employee.", ppl)
    assert any(m == RP.RECORDS_SEVERAL_PEOPLE_MESSAGE.replace("<<WORD>>", "Kilo")
               .replace("<<NAMES>>", "'Kilo Zephyr', 'Kilo Quill'").replace("<<USED>>", "'Kilo Zephyr'") for m in out)
    assert not any("Kilo Quill'" in m and "several" in m.lower() for m in people_nudges("Kilo took 3.", ppl))


def test_merge_facts_drop_the_group_b_details_first():
    import json
    from sf_assistant.agent.checks import facts_json
    lc = {"filter": "x", "object": II, "field": "Interviewer__c", "records": 8, "others": [],
          "paths": [{"path": "p" * 400, "records": 3}], "any_path": 9}
    big = {"count": 8, "lookup_check": [lc], "notes": ["'K' through the parents of X records " + "n" * 300, "keep me"]}
    second = {"count": 5, "lookup_check": []}
    ev = [_ev("count_records", big, obj=II), _ev("count_records", second, qid="q2", obj=II)]
    full = facts_json(ev, max_chars=10 ** 9)
    assert '"paths"' in full and "through the parents of" in full
    cut = facts_json(ev, max_chars=len(full) - 100)
    got = json.loads(cut)
    assert [e["query_id"] for e in got] == ["q1", "q2"]                 # both data entries stay
    assert "paths" not in got[0]["result"]["lookup_check"][0] and got[0]["result"]["notes"] == ["keep me"]
    assert ev[0].model_result["lookup_check"][0]["paths"]               # the evidence itself is unchanged


def test_people_answer_reads_a_copy_source_key():
    from sf_assistant.agent.people_answer import _profile_items
    prof = {"Marketing__c.Recruiter__c (via Candidate__c)": "Kilo Zephyr", "Plan__c (Plan)": "Career",
            "Assigned_Recruiter__c (Assigned Recruiter; partial copy of Marketing__c.Recruiter__c)": "Kilo Zephyr"}
    assert _profile_items(prof, 2) == ["Plan: Career", "Assigned Recruiter: Kilo Zephyr"]
    assert _profile_items(prof, 3)[-1] == "Recruiter (on Marketing): Kilo Zephyr"
