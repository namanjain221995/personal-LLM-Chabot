"""The query state of Salesforce conversation memory (``agent/query_state.py``; design v2 of 2026-11-25, sections 5 to
10, test list §16.1): building it from an answer's evidence, checking a state that came back, every patch operation
with its checks and fallbacks, the period rule D1, record sets as filters, the 32 KB cap and the plan line.

Model-free and duckdb-free: the evidence is built by hand (``QueryEvidence``), the catalog is a small made-up view
(:class:`FakeView`) and every name and value is synthetic.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
import re

import pytest

from sf_assistant.agent import query_state as Q
from sf_assistant.engine.term_contracts import NO_TERMS, PathInfo, Step, Terms, detect
from sf_assistant.engine.types import QueryEvidence, Statement

TODAY = dt.date(2026, 11, 25)
R = "reference"
#: A made-up catalog: objects -> field -> kind, or (reference, target).
OBJECTS = {
    "Interview__c": {"Name": "auto_number", "Interview_Status__c": "picklist", "Interview_Outcome__c": "picklist",
                     "Date_of_Interview__c": "date", "CreatedDate": "datetime", "Region__c": "picklist",
                     "Client_Bill_Rate__c": "currency", "Interviewer_Name__c": "text",
                     "Department__c": (R, "Department__c"), "Recruiter__c": (R, "Recruiter__c"),
                     "Candidate__c": (R, "Account"), "Interview_Support_Person__c": (R, "Recruiter__c"),
                     "Preferred_Support_Person__c": (R, "Recruiter__c")},
    "Account": {"Name": "name", "RecordTypeId": (R, "RecordType"), "IsPersonAccount": "boolean",
                "Candidate_Status__c": "picklist", "PersonEmail": "email", "CreatedDate": "datetime",
                "Is_Active__c": "boolean"},
    "Recruiter__c": {"Name": "name", "Department__c": (R, "Department__c"), "Email__c": "email"},
    "Department__c": {"Name": "name"},
    "RecordType": {"Name": "name", "DeveloperName": "text"},
    "Candidate_Training__c": {"Name": "name", "Candidate__c": (R, "Account"), "Status__c": "picklist",
                              "CreatedDate": "datetime"},
    "Session__c": {"Name": "name", "Host_User__c": (R, "Recruiter__c"), "Session_Date__c": "date"},
}
#: The made-up picklist values of the picklist fields (schema data: a stored value a call may name untyped).
PICKLISTS = {"Interview_Status__c": ["Completed", "Scheduled", "Cancelled", "RescheduledTwice"],
             "Interview_Outcome__c": ["Offer Received", "Rejected", "On Hold"],
             "Region__c": ["North America", "Europe", "Pune", "Asia Pacific"],
             "Candidate_Status__c": ["Active", "Placed", "Inactive"], "Status__c": ["Open", "Done"]}


class FakeView:
    """The view the patch checks read (``query_state.PlanView`` over a real catalog has the same methods)."""

    def __init__(self, objects: dict | None = None, blocked: tuple[str, ...] = ()) -> None:
        self.objects = objects if objects is not None else OBJECTS
        self.blocked = frozenset(blocked)

    def _api(self, obj, name):
        return next((k for k in self.objects.get(obj, {}) if k.lower() == str(name).lower()), None)

    def object(self, name):
        return next((k for k in self.objects if k.lower() == str(name or "").lower()), None)

    def path(self, obj, text):
        if not isinstance(text, str) or not text.strip() or obj not in self.objects:
            return None
        parts, cur, refs, canon = text.split("."), obj, [], []
        for i, part in enumerate(parts):
            api = self._api(cur, part)
            if api is None:
                return None
            kind = self.objects[cur][api]
            canon.append(api)
            last = i == len(parts) - 1
            if isinstance(kind, tuple):
                refs.append(Step(cur, api, kind[1], ""))
                if not last:
                    cur = kind[1]
            elif not last:
                return None
        return PathInfo(".".join(canon), tuple(refs), canon[-1])

    def record_type(self, obj, value):
        return {"personaccount": "PersonAccount", "person account": "PersonAccount", "b2b client": "B2B_Client",
                "b2b_client": "B2B_Client"}.get(str(value).lower())

    def person_type(self, obj):
        return "Person Account" if obj == "Account" else None

    def has_field(self, obj, api):
        return api in self.objects.get(obj, {})

    def record_types(self, obj):
        return [("Person Account", "PersonAccount"), ("B2B Client", "B2B_Client")]

    def field_kind(self, obj, text):
        p = self.path(obj, text)
        if p is None:
            return None
        cur = obj
        for part in p.text.split(".")[:-1]:
            cur = self.objects[cur][part][1]
        k = self.objects[cur][p.field]
        return "reference" if isinstance(k, tuple) else k

    def date_path(self, obj, name):
        p = self.path(obj, name)
        return p.text if p is not None and self.field_kind(obj, name) in ("date", "datetime") else None

    def is_contact(self, obj, text):
        return self.field_kind(obj, text) in ("email", "phone")

    def is_blocked(self, obj, text):
        p = self.path(obj, text)
        return p is not None and p.field in self.blocked

    def picklist_values(self, obj, text):
        p = self.path(obj, text)
        return list(PICKLISTS.get(p.field, [])) if p is not None and self.field_kind(obj, text) == "picklist" else []

    def lookups(self, obj, target):
        return sorted(k for k, v in self.objects.get(obj, {}).items() if isinstance(v, tuple) and v[1] == target)

    def target(self, obj, text):
        p = self.path(obj, text)
        return p.refs[-1].target if p is not None and p.refs and p.refs[-1].field == p.field else None


VIEW = FakeView()


def ev(tool, args, result=None, applied=None, *, ok=True, kind="data", statements=None, enforced=None):
    """One call's evidence as the record tools log it (``arguments`` as the model sent them)."""
    e = QueryEvidence(query_id="q1", tool=tool, kind=kind, object=(args or {}).get("object"), arguments=args, ok=ok,
                      error=None if ok else "error", applied=applied or {}, notes=[], statements=statements or [],
                      tables=[], synced_at={}, summary="", model_result=result if ok else None, elapsed_ms=1.0)
    if enforced is not None:
        e.enforced_arguments = enforced
    return e


def period(phrase, first, last, field="Date_of_Interview__c"):
    return {"period": {"date_field": field, "phrase": phrase, "from": first, "to": last, "to_exclusive": None,
                       "time_zone": "Asia/Kolkata"}}


GROUP_ARGS = {"object": "Interview__c", "group_by": "Interview_Support_Person__c", "date_field": "Date_of_Interview__c",
              "period": "this month", "top": 5}
GROUP_RESULT = {"groups": [{"group": "Person Alpha", "value": 7}, {"group": "Person Beta", "value": 3}],
                "group_count": 2}
NOV = period("this month", "2026-11-01", "2026-11-30")
#: NOV as a state stores it (§5: the words, the dates and the date field only)
NOV_STORED = {"phrase": "this month", "from": "2026-11-01", "to": "2026-11-30", "date_field": "Date_of_Interview__c"}


def group_ev(args=None, result=None, applied=None):
    return ev("group_records", dict(args or GROUP_ARGS), result or GROUP_RESULT, applied or NOV)


def list_ev(args, ids, applied=None):
    st = Statement(sql="SELECT 1", params=[], columns=["Id", "Name"], rows=[[i, f"N{n}"] for n, i in enumerate(ids)],
                   row_count=len(ids), truncated=False, elapsed_ms=1.0)
    rows = [{"Name": f"N{n}"} for n in range(len(ids))]
    return ev("list_records", args, {"rows": rows, "returned": len(ids), "total_count": len(ids)}, applied,
              statements=[st])


def records_state(evidence, terms=None, show=None, **kw):
    kw.setdefault("view", VIEW)
    return Q.build("records", evidence=evidence, terms=terms or {"keys": [], "text": "q"}, show=show,
                   made_at="2026-11-25T10:00:00+05:30", kb="kb-1", reason="new", **kw)


def plan_of(calls, terms=None, show=None, age=0):
    """A checked plan from plain call entries (role main first unless given)."""
    out = []
    for i, c in enumerate(calls):
        c = dict(c)
        c.setdefault("role", "main" if i == 0 else "side")
        c.setdefault("nonempty", True)
        out.append(c)
    state = {"v": 1, "made_at": "", "kb": "", "profile": None,
             "plan": {"question": "q", "terms": terms or {"keys": [], "text": ""}, "calls": out,
                      "show": show or {"chart": False, "type": None, "table": False}, "age": age},
             "pending": None, "last": {"kind": "records", "reason": "", "op": None}}
    got = Q.check(state, view=VIEW)
    assert got.state is not None, got
    return got.state["plan"]


def patch(plan, ops, typed="", terms=NO_TERMS, **kw):
    return Q.apply(plan, ops, typed=typed, view=VIEW, today=TODAY, terms=terms, **kw)


def main_call(p):
    return next(c for c in p["calls"] if c["role"] == "main")


# ------------------------------------------------------------------------------------------- building from evidence
def test_build_main_is_the_last_chartable_call_and_order_is_kept():
    count = ev("count_records", {"object": "Interview__c"}, {"count": 12})
    st = records_state([count, group_ev()])
    calls = st["plan"]["calls"]
    assert [c["tool"] for c in calls] == ["count_records", "group_records"]
    assert [c["role"] for c in calls] == ["side", "main"]
    st = records_state([group_ev(), count])
    assert [c["role"] for c in st["plan"]["calls"]] == ["main", "side"]


def test_build_count_with_breakdown_is_chartable_and_plain_counts_pick_the_last():
    plain = ev("count_records", {"object": "Interview__c"}, {"count": 4})
    broken = ev("count_records", {"object": "Account"}, {"count": 3, "breakdown": {"Status": {"A": 1, "B": 2}}})
    st = records_state([broken, plain])
    assert [c["role"] for c in st["plan"]["calls"]] == ["main", "side"]
    st = records_state([plain, ev("count_records", {"object": "Account"}, {"count": 1})])
    assert [c["role"] for c in st["plan"]["calls"]] == ["side", "main"]


def test_build_keeps_only_successful_contract_calls_and_an_identical_call_once():
    evidence = [ev("describe_fields", {"object": "Interview__c"}, {}, kind="orientation"),
                ev("search_records", {"name": "Person Alpha"}, {"matches": []}),
                ev("count_records", {"object": "Interview__c", "filters": [{"field": "Bad__c", "op": "eq",
                                                                          "value": "x"}]}, ok=False),
                group_ev(), group_ev()]
    st = records_state(evidence)
    assert [c["tool"] for c in st["plan"]["calls"]] == ["group_records"]


def test_build_cap_more_than_three_calls_stores_no_plan():
    calls = [ev("count_records", {"object": "Interview__c", "filters": [{"field": "Region__c", "op": "eq",
                                                                       "value": f"R{i}"}]}, {"count": i + 1})
             for i in range(4)]
    typed = {"keys": [], "text": "interviews in R0, R1, R2 and R3"}
    assert records_state(calls[:3], terms=typed)["plan"] is not None
    st = records_state(calls, terms=typed)
    assert st is not None and st["plan"] is None
    plan, why = Q.plan_from_evidence(calls, terms=typed)
    assert plan is None and why == "too_many"
    assert Q.plan_from_evidence(calls, terms=typed, max_calls=4)[1] == ""


def test_build_stores_model_arguments_never_the_enforced_form():
    model = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "=", "value": "Active"}]}
    enforced = copy.deepcopy(model)
    enforced["filters"].append({"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"})
    st = records_state([ev("count_records", model, {"count": 2}, enforced=enforced)])
    args = st["plan"]["calls"][0]["args"]
    assert args == {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Active"}]}
    assert "RecordTypeId" not in json.dumps(st)


def test_build_never_stores_a_contact_flag_results_or_answer_text():
    lst = list_ev({"object": "Recruiter__c", "fields": ["Name", "Email__c"]}, ["a06000000000099AAA"])
    st = records_state([lst], terms={"keys": [], "text": "email of Person Alpha"})
    text = json.dumps(st)
    assert "contact" not in text.lower()
    assert "N0" not in text and "rows" not in text and "a06000000000099AAA" not in text
    assert "Person Alpha" not in json.dumps(records_state([group_ev()]))   # group labels: the digest only


@pytest.mark.parametrize("tool,result,expected", [
    ("count_records", {"count": 0}, False), ("count_records", {"count": 3}, True),
    ("group_records", {"groups": []}, False), ("group_records", {"groups": [{"group": "(blank)", "value": 2}]}, True),
    ("trend_records", {"buckets": [{"bucket": "2026-10", "value": 0}], "total": 0}, False),
    ("trend_records", {"buckets": [{"bucket": "2026-10", "value": 0, "base": 4, "percent": 0.0}]}, True),
    ("compare_records", {"segments": [{"label": "a", "value": 0}, {"label": "b", "value": None}]}, False),
    ("compare_records", {"segments": [{"label": "a", "value": 0}, {"label": "b", "value": 2}]}, True),
    ("percentage_of_records", {"part": 0, "base": 0}, False), ("percentage_of_records", {"part": 0, "base": 5}, True),
    ("aggregate_records", {"records_matched": 0}, False), ("aggregate_records", {"records_matched": 6}, True),
    ("list_records", {"rows": [], "total_count": 0}, False), ("list_records", {"rows": [{}], "total_count": 1}, True),
    ("count_records", None, False), ("count_records", {"count": True}, False),
])
def test_nonempty_is_a_boolean_of_rows(tool, result, expected):
    assert Q.nonempty(tool, result) is expected


def test_build_period_from_applied_and_canonical_date_field():
    args = dict(GROUP_ARGS, date_field="interview_date")
    st = records_state([group_ev(args)])
    call = st["plan"]["calls"][0]
    assert call["period"] == {"phrase": "this month", "from": "2026-11-01", "to": "2026-11-30",
                              "date_field": "Date_of_Interview__c"}
    assert call["args"]["date_field"] == "Date_of_Interview__c"


def test_build_compare_segments_keep_their_dates_and_no_values():
    args = {"object": "Interview__c", "date_field": "Date_of_Interview__c",
            "segments": [{"label": "this month", "period": "this month"}, {"label": "last month",
                                                                           "period": "last month"}]}
    result = {"segments": [{"label": "this month", "value": 417, "applied": NOV},
                           {"label": "last month", "value": 389,
                            "applied": period("last month", "2026-10-01", "2026-10-31")}]}
    call = records_state([ev("compare_records", args, result)])["plan"]["calls"][0]
    assert call["segments"] == [{"label": "this month", "phrase": "this month", "from": "2026-11-01",
                                 "to": "2026-11-30"},
                                {"label": "last month", "phrase": "last month", "from": "2026-10-01",
                                 "to": "2026-10-31"}]
    assert "417" not in json.dumps(call) and "389" not in json.dumps(call) and "value" not in json.dumps(call)


def test_build_question_terms_show_and_last():
    st = records_state([group_ev()], terms={"keys": ["candidates", "bogus"], "text": "x" * 700},
                       show={"chart": True, "type": "pie", "table": "yes"}, question="Interviews per person?")
    plan = st["plan"]
    assert plan["question"] == "Interviews per person?" and plan["age"] == 0
    assert plan["terms"] == {"keys": ["candidates"], "text": "x" * 600}
    assert plan["show"] == {"chart": True, "type": "pie", "table": False}
    assert st["last"] == {"kind": "records", "reason": "new", "op": None}
    assert records_state([group_ev()])["plan"]["question"].startswith("number of Interview records per")


def test_build_answer_kinds_follow_section_9():
    parent = records_state([group_ev()])
    assert parent["plan"]["age"] == 0
    pend = Q.make_pending("Which one?", "visualize it", [{"label": "Interviews", "question": "Plot interviews"},
                                                         {"label": "Sessions", "question": "Plot sessions"}])
    for kind, age in (("knowledge", 1), ("no_query", 1), ("people", 1), ("unclear", 0), ("small_talk", 0),
                      ("about_answer", 0), ("clarify", 0)):
        st = Q.build(kind, parent=parent, pending=pend)
        assert st["plan"]["calls"] == parent["plan"]["calls"] and st["plan"]["age"] == age, kind
        assert (st["pending"] is not None) == (kind == "clarify"), kind
        assert st["last"]["kind"] == Q._KIND_ALIASES.get(kind, kind)
    older = Q.build("knowledge", parent=Q.build("knowledge", parent=parent))
    assert older["plan"]["age"] == 2
    assert Q.build("about_answer", parent=older)["plan"]["age"] == 0
    assert Q.build("refused", parent=parent)["plan"] is None
    no_call = Q.build("records", parent=parent, evidence=[ev("search_records", {"name": "x"}, {})])
    assert no_call["plan"]["age"] == 1
    # small talk ends a pending clarification; a clarify keeps the parent's age
    asked = Q.build("clarify", parent=Q.build("knowledge", parent=parent), pending=pend)
    assert asked["plan"]["age"] == 1 and asked["pending"]["choices"][1]["n"] == 2
    assert Q.build("small_talk", parent=asked)["pending"] is None
    assert Q.build("knowledge", parent=None)["plan"] is None
    with pytest.raises(ValueError):
        Q.build("nonsense")


def test_build_from_a_checked_parent():
    parent = Q.check(records_state([group_ev()]), view=VIEW)
    assert Q.build("knowledge", parent=parent)["plan"]["age"] == 1


def test_build_validates_arguments_like_the_record_tools():
    e = ev("group_records", {"object": "Interview__c", "group_by": ["Recruiter__c", "Interview_Outcome__c"],
                             "measure": "average", "measure_field": "Client_Bill_Rate__c"}, GROUP_RESULT)
    args = records_state([e])["plan"]["calls"][0]["args"]
    assert args["group_by"] == "Recruiter__c" and args["split_by"] == "Interview_Outcome__c"
    assert args["measure"] == "avg"
    bad = ev("count_records", {"object": "Interview__c", "colour": "red"}, {"count": 1})
    assert Q.plan_from_evidence([bad]) == (None, "no_calls")
    assert Q.model_args("count_records", {"object": "X"}, validate=lambda t, a: dict(a, seen=True))["seen"] is True
    assert Q.model_args("count_records", {"object": "X"}, validate=lambda t, a: 1 / 0) is None


# ------------------------------------------------------------------------------------------------------- set digests
def test_set_digest_of_lists_and_ranked_groups_only():
    ids = ["a0A000000000001AAA", "a0A000000000002AAA"]
    lst = list_ev({"object": "Interview__c", "limit": 2}, ids)
    assert Q.set_keys(lst) == ids
    assert records_state([lst])["plan"]["calls"][0]["set_digest"] == Q.keys_digest(ids)
    g = records_state([group_ev()])["plan"]["calls"][0]
    assert g["set_digest"] == Q.keys_digest(["Person Alpha", "Person Beta"])
    plain = dict(GROUP_ARGS)
    plain.pop("top")
    assert "set_digest" not in records_state([group_ev(plain)])["plan"]["calls"][0]
    assert "set_digest" not in records_state([ev("count_records", {"object": "Interview__c"},
                                                 {"count": 1})])["plan"]["calls"][0]
    blank = group_ev(result={"groups": [{"group": "(blank)", "value": 1}, {"group": "Person Gamma", "value": 1}]})
    assert Q.set_keys(blank) == ["Person Gamma"]
    assert Q.keys_digest(["a", "b"]) != Q.keys_digest(["b", "a"]) and re.fullmatch(r"sha256:[0-9a-f]{64}",
                                                                                   Q.keys_digest([]))


def test_keys_filter_ok_changed_rank_and_empty():
    kf = {"field": "Interview_Support_Person__c", "digest": Q.keys_digest(["A", "B"]), "rank": None}
    assert Q.keys_filter(kf, ["A", "B"]) == ({"field": "Interview_Support_Person__c", "op": "in",
                                               "values": ["A", "B"]}, "ok")
    item, status = Q.keys_filter(kf, ["B", "C"])
    assert status == "changed" and item["values"] == ["B", "C"]
    assert Q.keys_filter(kf, []) == (None, "empty")
    ranked = dict(kf, rank=2)
    assert Q.keys_filter(ranked, ["A", "B"]) == ({"field": "Interview_Support_Person__c", "op": "eq", "value": "B"},
                                                 "ok")
    assert Q.keys_filter(ranked, ["B", "A"]) == (None, "rank_changed")
    assert Q.keys_filter(dict(ranked, rank=3, digest=None), ["A", "B"]) == (None, "empty")
    shared = {"field": "Recruiter__c", "tool": "group_records", "digest": None, "rank": None}
    assert Q.keys_filter(shared, ["Person Alpha", "Person Beta (#2)"]) == (None, "namesakes")
    assert Q.keys_filter(dict(shared, rank=1), ["Person Alpha", "Person Beta (#2)"])[1] == "ok"
    assert Q.keys_filter(dict(shared, tool="list_records"), ["a (#2)"])[1] == "ok"   # ids never carry the mark
    many = [f"K{i}" for i in range(30)]
    assert len(Q.keys_filter({"field": "F", "digest": None}, many)[0]["values"]) == Q.MAX_SET_KEYS
    pct = Q.with_item("percentage_of_records", {"object": "Interview__c"}, {"field": "F", "op": "eq", "value": 1})
    assert pct["base_filters"] == [{"field": "F", "op": "eq", "value": 1}]


# ---------------------------------------------------------------------------------------------------------- checking
def test_check_round_trip_with_reordered_keys():
    st = records_state([group_ev(), ev("count_records", {"object": "Interview__c"}, {"count": 3})],
                       terms={"keys": ["candidates"], "text": "how many candidates"})

    def shuffled(x, rnd):
        if isinstance(x, dict):
            keys = list(x)
            rnd.shuffle(keys)
            return {k: shuffled(x[k], rnd) for k in keys}
        if isinstance(x, list):
            return [shuffled(v, rnd) for v in x]
        return x

    back = json.loads(json.dumps(shuffled(st, random.Random(7))))
    got = Q.check(back, view=VIEW)
    assert got.reason == "" and got.missing == () and Q._canon(got.state) == Q._canon(st)
    assert Q.usable(got) and not Q.usable(got, gap=1)


def test_check_none_and_unknown_version():
    assert Q.check(None) == Q.Checked(None)
    st = records_state([group_ev()])
    assert Q.check(dict(st, v=2)).reason == "state_version_unknown"
    assert Q.check(dict(st, v=True)).reason == "state_version_unknown"
    assert Q.check({k: v for k, v in st.items() if k != "v"}).reason == "state_invalid"


def _broken(path, value):
    st = records_state([group_ev()], terms={"keys": ["candidates"], "text": "t"})
    target = st
    for p in path[:-1]:
        target = target[p]
    if value is KeyError:
        target.pop(path[-1])
    else:
        target[path[-1]] = value
    return st


@pytest.mark.parametrize("path,value", [
    (("plan", "calls"), "not a list"), (("plan", "calls"), []),
    (("plan", "calls", 0, "role"), "side"), (("plan", "calls", 0, "tool"), "search_records"),
    (("plan", "calls", 0, "args"), {"object": "Interview__c"}),            # group_records needs group_by
    (("plan", "calls", 0, "args", "colour"), "red"), (("plan", "calls", 0, "set_digest"), "md5:abc"),
    (("plan", "calls", 0, "nonempty"), KeyError), (("plan", "calls", 0, "period", "from"), "1 Nov"),
    (("plan", "terms", "keys"), ["placements"]), (("plan", "terms", "text"), "x" * 601),
    (("plan", "age"), -1), (("plan", "age"), "0"), (("plan", "question"), None),
    (("pending",), {"asked": "?", "for": "x", "choices": []}), (("last",), None), (("last", "kind"), "weird"),
    (("profile",), 7), (("kb",), ["x"]), (("plan", "calls", 0, "keys_from"), {"field": "F", "tool": "count_records"}),
])
def test_check_bad_shapes_are_invalid(path, value):
    got = Q.check(_broken(path, value), view=VIEW)
    assert got.state is None and got.reason == "state_invalid" and got.problems


def test_check_two_mains_and_too_many_calls():
    st = records_state([group_ev(), ev("count_records", {"object": "Interview__c"}, {"count": 1})])
    st["plan"]["calls"][1]["role"] = "main"
    assert Q.check(st).reason == "state_invalid"
    st["plan"]["calls"][1]["role"] = "side"
    st["plan"]["calls"] += copy.deepcopy(st["plan"]["calls"][1:]) * 2
    assert Q.check(st).reason == "state_invalid"
    assert Q.check(st, max_calls=4).reason == ""


def test_check_size_and_profile():
    st = records_state([group_ev()])
    big = copy.deepcopy(st)
    big["kb"] = "k"
    big["made_at"] = "m"
    big["plan"]["calls"][0]["args"]["filters"] = [{"field": "Region__c", "op": "in", "values": ["v" * 250] * 20}] * 8
    assert Q.check(big).reason == "state_too_large"
    assert Q.check(st, profile="recruiters").reason == "profile_mismatch"
    assert Q.check(dict(st, profile="recruiters"), profile="recruiters").reason == ""
    assert Q.check(dict(st, profile="recruiters")).reason == "profile_mismatch"


def test_check_names_fields_removed_after_a_knowledge_change():
    args = dict(GROUP_ARGS, filters=[{"field": "Region__c", "op": "eq", "value": "North America"}])
    st = records_state([group_ev(args)])
    objects = copy.deepcopy(OBJECTS)
    del objects["Interview__c"]["Region__c"]
    got = Q.check(st, view=FakeView(objects))
    assert got.state is not None and got.missing == ("Interview__c.Region__c",)
    assert not Q.usable(got)
    gone = copy.deepcopy(OBJECTS)
    del gone["Interview__c"]
    assert Q.check(st, view=FakeView(gone)).missing == ("Interview__c",)
    assert Q.check(st).missing == ()   # no view: nothing to compare with


def test_usable_follows_gap_age_and_plan():
    st = records_state([group_ev()])
    assert Q.usable(Q.check(st))
    old = Q.build("knowledge", parent=Q.build("knowledge", parent=Q.build("knowledge", parent=st)))
    assert old["plan"]["age"] == 3 and not Q.usable(Q.check(old)) and Q.usable(Q.check(old), max_age=3)
    assert not Q.usable(Q.check(Q.build("refused", parent=st)))


def test_state_cap_is_32_kb():
    filters = [{"field": "Region__c", "op": "in", "values": [f"{i}" + "v" * 210 for i in range(20)]}] * 8
    big = ev("list_records", {"object": "Interview__c", "filters": filters}, {"rows": [], "total_count": 0})
    assert Q.state_bytes({"filters": filters}) > Q.MAX_STATE_BYTES

    class Listed(FakeView):   # the values are schema data here, so the call is kept and the size decides
        def picklist_values(self, obj, text):
            return [f"{i}" + "v" * 210 for i in range(20)]

    assert records_state([big], view=Listed()) is None
    small = records_state([group_ev()])
    assert Q.fit(small) is small and Q.state_bytes(small) < 4096 and Q.fit(None) is None


# ------------------------------------------------------------------------------------------------------- operations
def test_normalize_ops_forms_and_refusals():
    canon = [{"set_period": {"phrase": "last month"}}]
    assert Q.normalize_ops(canon) == canon
    assert Q.normalize_ops([{"op": "set_period", "args": {"phrase": "last month"}}]) == canon
    assert Q.normalize_ops([{"op": "set_period", "phrase": "last month"}]) == canon
    assert Q.normalize_ops([{"add_filter": {"field": "Region__c", "op": "eq", "value": "X"}}]) is not None
    for bad in ("x", [{"op": "add_filter", "field": "Region__c", "value": "X"}], [{"explode": {}}],
                [{"set_top": {"n": 0}}], [{"set_top": {"n": True}}], [{"set_grain": {"grain": "decade"}}],
                [{"drill": {"value": "A", "rank": 1}}], [{"drill": {}}], [{"set_show": {}}],
                [{"set_show": {"type": "radar"}}], [{"add_filter": {"field": "F", "op": "like", "value": "x"}}],
                [{"add_filter": {"field": "F", "op": "eq"}}], [{"set_period": {"phrase": " "}}],
                [{"set_period": {"phrase": "x", "extra": 1}}], [{"new_question": {}}] * 4):
        assert Q.normalize_ops(bad) is None, bad
    assert Q.normalize_ops([{"add_filter": {"field": "F", "op": "is_blank", "value": "x"}}]) == \
        [{"add_filter": {"field": "F", "op": "is_blank"}}]


def test_apply_refuses_bad_ops_and_no_plan():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    assert patch(plan, "x").clarify.code == "bad_ops"
    assert patch(plan, [{"new_question": {}}] * 4).clarify.code == "too_many_ops"
    assert patch(plan, []).clarify.code == "bad_ops"
    assert patch(None, [{"set_top": {"n": 3}}]).clarify.code == "no_plan"
    got = patch(None, [{"new_question": {}}])
    assert got.new_question and got.plan is None and got.clarify is None
    assert patch(plan, [{"set_top": {"n": 3}}, {"new_question": {}}]).new_question


def test_apply_never_changes_the_stored_plan():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    before = copy.deepcopy(plan)
    patch(plan, [{"set_top": {"n": 10}}, {"set_period": {"phrase": "last month"}}])
    assert plan == before


# set_period
def test_set_period_every_dated_call_and_typed_periods_are_relative_to_today():
    side = {"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                              "period": "this month"}, "period": NOV["period"]}
    nodate = {"tool": "count_records", "args": {"object": "Account"}}
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}, side, nodate])
    got = patch(plan, [{"set_period": {"phrase": "last month"}}], "same for last month")
    assert got.clarify is None and got.touched == frozenset({0, 1})
    calls = got.plan["calls"]
    assert [c["args"].get("period") for c in calls] == ["last month", "last month", None]
    assert "period" not in calls[0] and "period" not in calls[1]   # the dates come from the run
    assert "2026-10-01 to 2026-10-31" in Q.plan_line(got.plan, today=TODAY)


def test_set_period_checks():
    plan = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}}])
    assert patch(plan, [{"set_period": {"phrase": "last month"}}]).clarify.code == "no_date_field"
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    bad = patch(grp, [{"set_period": {"phrase": "the blue moon"}}]).clarify
    assert bad.code == "bad_period" and bad.value == "the blue moon"
    allt = patch(grp, [{"set_period": {"phrase": "all time"}}])
    assert allt.clarify is None and "period" not in allt.plan["calls"][0]["args"]
    trend = plan_of([{"tool": "trend_records", "args": {"object": "Interview__c", "grain": "month",
                                                        "date_field": "Date_of_Interview__c", "period": "this year"}}])
    assert patch(trend, [{"set_period": {"phrase": "all time"}}]).clarify.code == "bad_period"
    cmp_args = {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                "segments": [{"label": "a", "period": "this month"}, {"label": "b", "period": "last month"}]}
    cmp = plan_of([{"tool": "compare_records", "args": cmp_args}])
    assert patch(cmp, [{"set_period": {"phrase": "last year"}}]).clarify.code == "which_side"


# add_filter
def test_add_filter_main_and_sides_with_the_field():
    side_same = {"tool": "count_records", "args": {"object": "Interview__c"}}
    side_other = {"tool": "count_records", "args": {"object": "Account"}}
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}, side_same, side_other])
    got = patch(plan, [{"add_filter": {"field": "region__c", "op": "eq", "value": "North America"}}],
                "Only for North America")
    assert got.clarify is None and got.touched == frozenset({0, 1})
    want = [{"field": "Region__c", "op": "eq", "value": "North America"}]
    assert [c["args"].get("filters") for c in got.plan["calls"]] == [want, want, None]


def test_add_filter_checks():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    unknown = patch(plan, [{"add_filter": {"field": "Territory__c", "op": "eq", "value": "East"}}], "only East")
    assert unknown.clarify.code == "unknown_field" and unknown.clarify.field == "Territory__c"
    # a value the person never typed (it came from an assistant turn) is refused
    nt = patch(plan, [{"add_filter": {"field": "Region__c", "op": "eq", "value": "North America"}}],
               "only for that region")
    assert nt.clarify.code == "value_not_typed" and nt.clarify.value == "North America"
    many = patch(plan, [{"add_filter": {"field": "Region__c", "op": "in", "values": ["East", "Asia Pacific"]}}],
                 "only east and asia-pacific")
    assert many.clarify is None
    assert patch(plan, [{"add_filter": {"field": "Region__c", "op": "in", "values": ["East", "West"]}}],
                 "only east").clarify.code == "value_not_typed"
    # the pending choice's words count as typed
    assert patch(plan, [{"add_filter": {"field": "Region__c", "op": "eq", "value": "West"}}], "2",
                 pending_text="Interviews in West this month").clarify is None
    # numbers as numbers, booleans always
    num = patch(plan, [{"add_filter": {"field": "Client_Bill_Rate__c", "op": "gt", "value": 50.0}}], "above 50")
    assert num.clarify is None
    acc = plan_of([{"tool": "count_records", "args": {"object": "Account"}}])
    assert patch(acc, [{"add_filter": {"field": "Is_Active__c", "op": "eq", "value": True}}],
                 "only active ones").clarify is None
    blank = patch(plan, [{"add_filter": {"field": "Region__c", "op": "is_blank"}}], "with no region")
    assert blank.clarify is None and blank.plan["calls"][0]["args"]["filters"] == [{"field": "Region__c",
                                                                                     "op": "is_blank"}]


def test_add_filter_cap_and_same_field_eq_replaces():
    eight = [{"field": "Region__c", "op": "ne", "value": f"R{i}"} for i in range(8)]
    plan = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, filters=eight)}])
    assert patch(plan, [{"add_filter": {"field": "Interview_Status__c", "op": "eq", "value": "Completed"}}],
                 "completed only").clarify.code == "too_many_filters"
    one = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, filters=[
        {"field": "Region__c", "op": "eq", "value": "East"}, {"field": "Interview_Status__c", "op": "eq",
                                                              "value": "Completed"}])}])
    got = patch(one, [{"add_filter": {"field": "Region__c", "op": "eq", "value": "West"}}], "west instead")
    assert got.plan["calls"][0]["args"]["filters"] == [
        {"field": "Interview_Status__c", "op": "eq", "value": "Completed"},
        {"field": "Region__c", "op": "eq", "value": "West"}]


def test_add_filter_on_a_percentage_goes_to_the_base():
    pct = plan_of([{"tool": "percentage_of_records", "args": {"object": "Interview__c", "part_filters": [
        {"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]}}])
    got = patch(pct, [{"add_filter": {"field": "Region__c", "op": "eq", "value": "East"}}], "east only")
    args = got.plan["calls"][0]["args"]
    assert args["base_filters"] == [{"field": "Region__c", "op": "eq", "value": "East"}]
    assert args["part_filters"] == [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]


# remove_filter
def test_remove_filter_whole_item_a_value_and_no_such_filter():
    filters = [{"field": "Region__c", "op": "in", "values": ["East", "West"]},
               {"field": "Interview_Status__c", "op": "eq", "value": "Completed"}]
    plan = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, filters=filters)}])
    got = patch(plan, [{"remove_filter": {"field": "Region__c"}}], "remove the region filter")
    assert got.plan["calls"][0]["args"]["filters"] == [filters[1]] and got.touched == frozenset({0})
    got = patch(plan, [{"remove_filter": {"field": "Region__c", "value": "east"}}], "not east")
    assert got.plan["calls"][0]["args"]["filters"][0] == {"field": "Region__c", "op": "in", "values": ["West"]}
    got = patch(plan, [{"remove_filter": {"field": "Interview_Status__c"}}], "all statuses")
    assert got.plan["calls"][0]["args"]["filters"] == [filters[0]]
    none = patch(plan, [{"remove_filter": {"field": "Recruiter__c"}}], "remove the recruiter filter")
    assert none.clarify.code == "no_such_filter"


def test_remove_filter_cannot_remove_a_contract_filter():
    # the candidate filter is never in the stored arguments: code adds it on every run
    acc = plan_of([{"tool": "count_records", "args": {"object": "Account", "filters": [
        {"field": "Candidate_Status__c", "op": "eq", "value": "Active"}]}}],
        terms={"keys": ["candidates"], "text": "how many active candidates"})
    terms = Q.terms_for(acc["terms"], "remove the record type filter")
    got = patch(acc, [{"remove_filter": {"field": "RecordTypeId"}}], "remove the record type filter", terms)
    assert got.clarify.code == "no_such_filter"
    got = patch(acc, [{"remove_filter": {"field": "Candidate_Status__c"}}], "all statuses", terms)
    from sf_assistant.engine.term_contracts import plan as term_plan
    added = term_plan(terms, "count_records", got.plan["calls"][0]["args"], VIEW).add
    assert added == [("filters", {"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"})]


# set_top
def test_set_top_group_list_and_checks():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    got = patch(plan, [{"set_top": {"n": 10}}], "show the top 10")
    assert got.plan["calls"][0]["args"]["top"] == 10 and got.touched == frozenset({0})
    low = patch(plan, [{"set_top": {"n": 3, "order": "asc"}}], "bottom 3")
    assert low.plan["calls"][0]["args"]["order"] == "asc" and "bottom 3" in Q.plan_line(low.plan)
    assert patch(plan, [{"set_top": {"n": 101}}]).clarify.code == "top_too_large"
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"set_top": {"n": 20}}]).plan["calls"][0]["args"]["limit"] == 20
    assert patch(lst, [{"set_top": {"n": 51}}]).clarify.code == "top_too_large"
    for tool, args in (("count_records", {"object": "Interview__c"}),
                       ("trend_records", {"object": "Interview__c", "grain": "month",
                                          "date_field": "Date_of_Interview__c", "period": "this year"})):
        assert patch(plan_of([{"tool": tool, "args": args}]), [{"set_top": {"n": 5}}]).clarify.code == "top_of_what"


# set_show
def test_set_show_is_display_only_and_marks_unchartable_plans():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    got = patch(plan, [{"set_show": {"chart": True}}], "visualize it")
    assert got.display_only and got.touched == frozenset() and got.plan["show"]["chart"] is True
    assert got.plan["calls"] == plan["calls"] and not got.not_chartable
    typed = patch(plan, [{"set_show": {"type": "pie"}}], "as a pie chart")
    assert typed.plan["show"] == {"chart": True, "type": "pie", "table": False}
    off = patch(typed.plan, [{"set_show": {"chart": False}}], "without the chart")
    assert off.plan["show"]["chart"] is False
    table = patch(plan, [{"set_show": {"table": True}}], "as a table")
    assert table.plan["show"]["table"] is True and "as a table" in Q.plan_line(table.plan)
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"set_show": {"chart": True}}], "plot this").not_chartable
    assert not patch(plan, [{"set_show": {"chart": True}}, {"set_top": {"n": 3}}], "top 3 chart").display_only


# set_group
def test_set_group_changes_the_grouping_and_converts_counts():
    plan = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, split_by="Department__c")}])
    got = patch(plan, [{"set_group": {"field": "Department__c"}}], "group by department instead")
    args = got.plan["calls"][0]["args"]
    assert args["group_by"] == "Department__c" and "split_by" not in args and args["top"] == 5
    assert "set_digest" not in got.plan["calls"][0]
    count = plan_of([{"tool": "count_records", "args": {"object": "Interview__c", "count_distinct": "Candidate__c"}}])
    got = patch(count, [{"set_group": {"field": "Recruiter__c"}}], "per recruiter")
    call = got.plan["calls"][0]
    assert call["tool"] == "group_records" and call["args"] == {"object": "Interview__c", "group_by": "Recruiter__c",
                                                                "measure": "count_distinct",
                                                                "measure_field": "Candidate__c"}
    trend = plan_of([{"tool": "trend_records", "args": {"object": "Interview__c", "grain": "month",
                                                        "date_field": "Date_of_Interview__c", "period": "this year"}}])
    assert patch(trend, [{"set_group": {"field": "Region__c"}}]).plan["calls"][0]["args"]["split_by"] == "Region__c"


def test_set_group_checks_and_date_words():
    plan = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, top=None)}])
    assert patch(plan, [{"set_group": {"field": "Nope__c"}}]).clarify.code == "unknown_field"
    by_date = patch(plan, [{"set_group": {"field": "Date_of_Interview__c"}}], "by date")
    assert by_date.plan["calls"][0]["tool"] == "trend_records" and by_date.plan["calls"][0]["args"]["grain"] == "month"
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"set_group": {"field": "Region__c"}}]).clarify.code == "group_not_for"


# set_grain (C13)
def test_set_grain_group_by_becomes_split_by_and_top_becomes_a_set():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"],
                     "set_digest": Q.keys_digest(["Person Alpha", "Person Beta"])}])
    got = patch(plan, [{"set_grain": {"grain": "month"}}], "by month")
    call = got.plan["calls"][0]
    assert call["tool"] == "trend_records"
    assert call["args"] == {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "this month",
                            "grain": "month", "split_by": "Interview_Support_Person__c"}
    kf = call["keys_from"]
    assert kf["tool"] == "group_records" and kf["args"] == GROUP_ARGS and kf["field"] == "Interview_Support_Person__c"
    assert kf["digest"] == plan["calls"][0]["set_digest"] and kf["rank"] is None
    assert call["period"] == NOV_STORED   # the words did not change: the stored dates stay (D1)
    # at run time the fresh top-N keys become an in filter; no id or name is stored
    run = Q.run_calls(got, TODAY)[0]
    assert run.set_args == GROUP_ARGS and run.keys_from["field"] == "Interview_Support_Person__c"
    item, status = Q.keys_filter(run.keys_from, ["Person Alpha", "Person Beta"])
    assert status == "ok" and Q.with_item(run.tool, run.args, item)["filters"] == [
        {"field": "Interview_Support_Person__c", "op": "in", "values": ["Person Alpha", "Person Beta"]}]
    assert "Person Alpha" not in json.dumps(got.plan)
    # by week on the same day: the trend ends with this week, so its own dates are read, and none are "kept"
    weekly = patch(plan, [{"set_grain": {"grain": "week"}}], "by week")
    assert weekly.plan["calls"][0]["period"]["to"] == "2026-11-29"
    assert not Q.run_calls(weekly, TODAY)[0].kept_dates


def test_set_grain_checks():
    both = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, split_by="Interview_Outcome__c")}])
    assert patch(both, [{"set_grain": {"grain": "month"}}]).clarify.code == "grain_two_splits"
    big = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, top=21)}])
    assert patch(big, [{"set_grain": {"grain": "week"}}]).clarify.code == "grain_top_too_large"
    ok20 = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, top=20)}])
    assert patch(ok20, [{"set_grain": {"grain": "week"}}]).clarify is None
    nodate = plan_of([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c"}}])
    assert patch(nodate, [{"set_grain": {"grain": "month"}}]).clarify.code == "no_date_field"
    noper = plan_of([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c",
                                                        "date_field": "Date_of_Interview__c"}}])
    assert patch(noper, [{"set_grain": {"grain": "month"}}]).clarify.code == "no_period"
    plain = plan_of([{"tool": "group_records", "args": {k: v for k, v in GROUP_ARGS.items() if k != "top"}}])
    got = patch(plain, [{"set_grain": {"grain": "quarter"}}])
    assert "keys_from" not in got.plan["calls"][0] and got.plan["calls"][0]["args"]["grain"] == "quarter"
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"set_grain": {"grain": "month"}}]).clarify.code == "grain_not_for"


def test_set_grain_of_a_trend_count_and_percentage():
    trend = plan_of([{"tool": "trend_records", "args": {"object": "Interview__c", "grain": "month",
                                                        "date_field": "Date_of_Interview__c", "period": "this year"}}])
    assert patch(trend, [{"set_grain": {"grain": "week"}}]).plan["calls"][0]["args"]["grain"] == "week"
    count = plan_of([{"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                                        "period": "this year", "count_distinct": "Candidate__c"}}])
    call = patch(count, [{"set_grain": {"grain": "month"}}]).plan["calls"][0]
    assert call["tool"] == "trend_records" and call["args"]["measure"] == "count_distinct"
    part = [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]
    pct = plan_of([{"tool": "percentage_of_records", "args": {
        "object": "Interview__c", "part_filters": part, "base_filters": [{"field": "Region__c", "op": "eq",
                                                                          "value": "East"}],
        "date_field": "Date_of_Interview__c", "period": "this year"}}])
    call = patch(pct, [{"set_grain": {"grain": "month"}}], "by month").plan["calls"][0]
    assert call["tool"] == "trend_records" and call["args"]["part_filters"] == part
    assert call["args"]["filters"] == [{"field": "Region__c", "op": "eq", "value": "East"}]


# set_measure
def test_set_measure_and_checks():
    count = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}}])
    got = patch(count, [{"set_measure": {"measure": "count_distinct", "field": "Candidate__c"}}],
                "how many candidates")
    assert got.plan["calls"][0]["args"] == {"object": "Interview__c", "count_distinct": "Candidate__c"}
    avg = patch(count, [{"set_measure": {"measure": "avg", "field": "Client_Bill_Rate__c"}}], "average bill rate")
    assert avg.plan["calls"][0]["tool"] == "aggregate_records" and avg.plan["calls"][0]["args"]["op"] == "avg"
    assert patch(count, [{"set_measure": {"measure": "sum", "field": "Region__c"}}]).clarify.code == "number_field"
    assert patch(count, [{"set_measure": {"measure": "max", "field": "Date_of_Interview__c"}}]).clarify is None
    assert patch(count, [{"set_measure": {"measure": "sum"}}]).clarify.code == "measure_field"
    assert patch(count, [{"set_measure": {"measure": "sum", "field": "Nope__c"}}]).clarify.code == "unknown_field"
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    got = patch(grp, [{"set_measure": {"measure": "count_distinct", "field": "Candidate__c"}}])
    args = got.plan["calls"][0]["args"]
    assert args["measure"] == "count_distinct" and args["measure_field"] == "Candidate__c"
    back = plan_of([{"tool": "group_records", "args": dict(GROUP_ARGS, measure="sum",
                                                           measure_field="Client_Bill_Rate__c")}])
    args = patch(back, [{"set_measure": {"measure": "count"}}]).plan["calls"][0]["args"]
    assert "measure" not in args and "measure_field" not in args
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"set_measure": {"measure": "count"}}]).clarify.code == "measure_not_for"


# drill
def test_drill_by_typed_value_and_by_rank():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"],
                     "set_digest": Q.keys_digest(["Person Alpha", "Person Beta"])},
                    {"tool": "count_records", "args": {"object": "Interview__c"}}])
    got = patch(plan, [{"drill": {"value": "Person Alpha"}}], "show Person Alpha's interviews")
    assert len(got.plan["calls"]) == 1
    call = got.plan["calls"][0]
    assert call["tool"] == "list_records" and call["role"] == "main"
    assert call["args"]["filters"] == [{"field": "Interview_Support_Person__c", "op": "eq", "value": "Person Alpha"}]
    assert call["period"] == NOV_STORED and got.plan["show"]["chart"] is False
    nt = patch(plan, [{"drill": {"value": "Person Alpha"}}], "show the first person's interviews")
    assert nt.clarify.code == "value_not_typed"
    rank = patch(plan, [{"drill": {"rank": 1}}], "the first one")
    kf = rank.plan["calls"][0]["keys_from"]
    assert kf["rank"] == 1 and kf["digest"] == plan["calls"][0]["set_digest"]
    assert patch(plan, [{"drill": {"rank": 6}}]).clarify.code == "rank_not_shown"
    count = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}}])
    assert patch(count, [{"drill": {"rank": 1}}]).clarify.code == "drill_not_group"


# compare_period
def test_compare_period_count_becomes_compare_records():
    plan = plan_of([{"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                                       "period": "this month", "filters": [
                                                           {"field": "Region__c", "op": "eq", "value": "East"}]},
                     "period": NOV["period"]}])
    got = patch(plan, [{"compare_period": {"phrase": "previous month"}}], "Compare with previous month")
    call = got.plan["calls"][0]
    assert call["tool"] == "compare_records"
    assert call["args"]["segments"] == [{"label": "this month", "period": "this month"},
                                        {"label": "previous month", "period": "previous month"}]
    assert call["args"]["filters"] == [{"field": "Region__c", "op": "eq", "value": "East"}]
    assert call["segments"][0]["from"] == "2026-11-01" and "from" not in call["segments"][1]
    line = Q.plan_line(got.plan, today=TODAY)
    assert "2026-11-01 to 2026-11-30" in line and "2026-10-01 to 2026-10-31" in line


def test_compare_period_group_adds_a_side_call_and_checks():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    got = patch(plan, [{"compare_period": {"phrase": "last month"}}], "compare with last month")
    assert [c["role"] for c in got.plan["calls"]] == ["main", "side"] and got.touched == frozenset({1})
    assert got.plan["calls"][1]["args"]["period"] == "last month"
    full = plan_of([{"tool": "group_records", "args": GROUP_ARGS},
                    {"tool": "count_records", "args": {"object": "Interview__c"}},
                    {"tool": "count_records", "args": {"object": "Account"}}])
    assert patch(full, [{"compare_period": {"phrase": "last month"}}]).clarify.code == "call_cap"
    assert patch(plan, [{"compare_period": {"phrase": "this month"}}]).clarify.code == "same_period"
    nop = plan_of([{"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c"}}])
    assert patch(nop, [{"compare_period": {"phrase": "last month"}}]).clarify.code == "no_period"
    nod = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}}])
    assert patch(nod, [{"compare_period": {"phrase": "last month"}}]).clarify.code == "no_date_field"
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c"}}])
    assert patch(lst, [{"compare_period": {"phrase": "last month"}}]).clarify.code == "compare_not_for"
    anyof = plan_of([{"tool": "count_records", "args": {
        "object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "this month",
        "any_of": [{"field": "Region__c", "op": "eq", "value": "East"}, {"field": "Interview_Status__c", "op": "eq",
                                                                         "value": "Completed"}]}}])
    assert patch(anyof, [{"compare_period": {"phrase": "last month"}}]).clarify.code == "compare_not_for"
    assert patch(plan, [{"compare_period": {"phrase": "sometime"}}]).clarify.code == "bad_period"
    # a month later, "previous month" is the month the plan kept (D1): comparing it with itself asks
    kept = plan_of([{"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                                       "period": "this month"}, "period": NOV["period"]}])
    later = Q.apply(kept, [{"compare_period": {"phrase": "previous month"}}], typed="compare with previous month",
                    view=VIEW, today=dt.date(2026, 12, 4))
    assert later.clarify.code == "same_period"
    assert Q.apply(kept, [{"compare_period": {"phrase": "2026-10-01 to 2026-10-31"}}], typed="vs October",
                   view=VIEW, today=dt.date(2026, 12, 4)).clarify is None


def test_a_changed_call_drops_its_set_digest():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "set_digest": Q.keys_digest(["A", "B"])}])
    got = patch(plan, [{"set_top": {"n": 3}}, {"set_grain": {"grain": "month"}}], "top 3 by month")
    assert got.clarify is None and got.plan["calls"][0]["keys_from"]["digest"] is None
    assert Q.keys_filter(got.plan["calls"][0]["keys_from"], ["A", "C", "D"])[1] == "ok"
    shown = patch(plan, [{"set_grain": {"grain": "month"}}], "by month")
    assert shown.plan["calls"][0]["keys_from"]["digest"] == Q.keys_digest(["A", "B"])
    assert Q.run_calls(Q.check(records_state([group_ev()])), TODAY)[0].tool == "group_records"


# follow_set (§10, C22)
def test_follow_set_copies_the_enforced_filters_of_an_unranked_set():
    acc = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Active"}],
           "date_field": "CreatedDate", "period": "this month"}
    plan = plan_of([{"tool": "count_records", "args": acc,
                     "period": {"phrase": "this month", "from": "2026-11-01", "to": "2026-11-30",
                                "date_field": "CreatedDate"}}],
                   terms={"keys": ["candidates"], "text": "how many active candidates joined this month"})
    terms = Q.terms_for(plan["terms"], "their interviews")
    got = patch(plan, [{"follow_set": {"object": "Interview__c"}}], "their interviews", terms)
    assert got.clarify is None
    call = got.plan["calls"][0]
    assert call["tool"] == "list_records" and call["args"]["object"] == "Interview__c"
    assert call["args"]["filters"] == [
        {"field": "Candidate__c.Candidate_Status__c", "op": "eq", "value": "Active"},
        {"field": "Candidate__c.RecordTypeId", "op": "eq", "value": "PersonAccount"},   # the contract's scope (C22)
        {"field": "Candidate__c.CreatedDate", "op": "gte", "value": "2026-11-01"},
        {"field": "Candidate__c.CreatedDate", "op": "lt", "value": "2026-12-01"}]
    assert "keys_from" not in call and got.plan["show"]["chart"] is False
    # without the candidates key the stored filters only
    bare = patch(plan, [{"follow_set": {"object": "Interview__c"}}], "their interviews")
    assert [f["field"] for f in bare.plan["calls"][0]["args"]["filters"]] == [
        "Candidate__c.Candidate_Status__c", "Candidate__c.CreatedDate", "Candidate__c.CreatedDate"]


def test_follow_set_ranked_sets_resolve_fresh_and_checks():
    ids = ["001000000000001AAA", "001000000000002AAA"]
    lst = plan_of([{"tool": "list_records", "args": {"object": "Account", "limit": 2},
                    "set_digest": Q.keys_digest(ids)}])
    got = patch(lst, [{"follow_set": {"object": "Candidate_Training__c"}}], "their trainings")
    kf = got.plan["calls"][0]["keys_from"]
    assert kf["field"] == "Candidate__c" and kf["tool"] == "list_records" and kf["digest"] == Q.keys_digest(ids)
    assert ids[0] not in json.dumps(got.plan)
    grp = plan_of([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Candidate__c", "top": 5}}])
    got = patch(grp, [{"follow_set": {"object": "Candidate_Training__c"}}], "their trainings")
    assert got.plan["calls"][0]["keys_from"]["field"] == "Candidate__c"
    two = plan_of([{"tool": "group_records", "args": GROUP_ARGS}])
    which = patch(two, [{"follow_set": {"object": "Interview__c"}}], "their interviews").clarify
    assert which.code == "which_link" and [c["label"] for c in which.choices] == [
        "Interview_Support_Person__c", "Preferred_Support_Person__c", "Recruiter__c"]
    assert Q.normalize_ops(which.choices[0]["ops"]) == which.choices[0]["ops"]
    picked = patch(two, which.choices[0]["ops"], "their interviews")
    assert picked.plan["calls"][0]["keys_from"]["field"] == "Interview_Support_Person__c"
    assert patch(two, [{"follow_set": {"object": "Department__c"}}]).clarify.code == "no_link"
    assert patch(two, [{"follow_set": {"object": "Nothing__c"}}]).clarify.code == "unknown_object"
    nontop = plan_of([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Candidate__c"}}])
    assert patch(nontop, [{"follow_set": {"object": "Candidate_Training__c"}}]).clarify.code == "set_not_supported"
    big = plan_of([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Candidate__c",
                                                      "top": 25}}])
    assert patch(big, [{"follow_set": {"object": "Candidate_Training__c"}}]).clarify.code == "set_too_large"
    anyof = plan_of([{"tool": "count_records", "args": {"object": "Account", "any_of": [
        {"field": "Candidate_Status__c", "op": "eq", "value": "Active"}, {"field": "Name", "op": "eq",
                                                                          "value": "X"}]}}])
    assert patch(anyof, [{"follow_set": {"object": "Candidate_Training__c"}}]).clarify.code == "set_not_supported"


# the checks of every patched plan
def test_term_rejects_become_a_clarify():
    b2b = plan_of([{"tool": "count_records", "args": {"object": "Account", "filters": [
        {"field": "RecordTypeId", "op": "eq", "value": "B2B Client"}]}}],
        terms={"keys": [], "text": "how many B2B accounts"})
    terms = Q.terms_for(b2b["terms"], "only candidates")
    got = patch(b2b, [{"set_show": {"chart": True}}], "only candidates", terms)
    assert got.clarify.code == "term_reject" and got.clarify.value == "T1"
    placed = plan_of([{"tool": "count_records", "args": {"object": "Account"}}],
                     terms={"keys": ["candidates", "placement"], "text": "how many placed candidates"})
    terms = Q.terms_for(placed["terms"], "only placed ones")
    got = patch(placed, [{"add_filter": {"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}}],
                "only placed ones", terms)
    assert got.clarify.code == "term_reject" and got.clarify.value == "T2"
    sup = plan_of([{"tool": "group_records", "args": {"object": "Interview__c",
                                                      "group_by": "Interview_Support_Person__c"}}],
                  terms={"keys": ["support_performance"], "text": "support person performance this month"})
    typed = "group by Preferred_Support_Person__c instead"
    got = patch(sup, [{"set_group": {"field": "Preferred_Support_Person__c"}}], typed, Q.terms_for(sup["terms"], typed))
    assert got.clarify.code == "term_reject" and got.clarify.value == "T3"


def test_bad_call_after_a_patch_and_contact_fields():
    plan = plan_of([{"tool": "list_records", "args": {"object": "Account", "fields": ["Name", "PersonEmail"]}}])
    got = patch(plan, [{"set_top": {"n": 5}}], "top 5")
    assert got.clarify is None and got.contact_blocked == ("Account.PersonEmail",)
    asked = patch(plan, [{"set_top": {"n": 5}}], "top 5 with their email")
    assert asked.contact_blocked == ()
    assert patch(plan, [{"set_top": {"n": 5}}], "top 5", contact_ok=True).contact_blocked == ()
    weird = plan_of([{"tool": "trend_records", "args": {"object": "Interview__c", "grain": "month",
                                                        "date_field": "Date_of_Interview__c", "period": "this year"}}])
    got = patch(weird, [{"set_measure": {"measure": "sum", "field": "Client_Bill_Rate__c"}},
                        {"add_filter": {"field": "Region__c", "op": "in", "values": ["East"]}}], "east")
    assert got.clarify is None and got.plan["calls"][0]["args"]["measure"] == "sum"


# -------------------------------------------------------------------------------------------- running: D1 and C7
def test_run_calls_keep_the_stored_dates_of_an_unchanged_relative_period():
    plan = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}])
    same = Q.run_calls(plan, TODAY)[0]
    assert same.args["period"] == "this month" and not same.kept_dates and same.unchanged and same.expect_rows
    later = Q.run_calls(plan, dt.date(2026, 12, 2))[0]
    assert later.args["period"] == "2026-11-01 to 2026-11-30" and later.kept_dates
    assert later.entry["args"]["period"] == "this month"   # the stored form keeps the words
    moved = patch(plan, [{"set_period": {"phrase": "this month"}}], "this month")
    typed = Q.run_calls(moved, dt.date(2026, 12, 2))[0]
    assert typed.args["period"] == "this month" and not typed.kept_dates and not typed.unchanged


@pytest.mark.parametrize("phrase,first,last,want", [
    ("since 2026-08-01", "2026-08-01", None, None),
    ("last 30 days", "2026-10-26", "2026-11-24", "2026-10-26 to 2026-11-24"),
    ("year to date", "2026-01-01", "2026-11-25", "2026-01-01 to 2026-11-25"),
])
def test_run_calls_open_and_rolling_periods(phrase, first, last, want):
    args = dict(GROUP_ARGS, period=phrase)
    plan = plan_of([{"tool": "group_records", "args": args, "period": {"phrase": phrase, "from": first, "to": last,
                                                                       "date_field": "Date_of_Interview__c"}}])
    got = Q.run_calls(plan, dt.date(2026, 12, 9))[0]
    assert got.args["period"] == (want or phrase) and got.kept_dates is bool(want)


def test_run_calls_keep_compare_segment_dates_and_set_dates():
    args = {"object": "Interview__c", "date_field": "Date_of_Interview__c",
            "segments": [{"label": "this month", "period": "this month"}, {"label": "last month",
                                                                           "period": "last month"}]}
    segs = [{"label": "this month", "phrase": "this month", "from": "2026-11-01", "to": "2026-11-30"},
            {"label": "last month", "phrase": "last month", "from": "2026-10-01", "to": "2026-10-31"}]
    plan = plan_of([{"tool": "compare_records", "args": args, "segments": segs}])
    got = Q.run_calls(plan, dt.date(2026, 12, 1))[0]
    assert [s["period"] for s in got.args["segments"]] == ["2026-11-01 to 2026-11-30", "2026-10-01 to 2026-10-31"]
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}])
    trend = patch(grp, [{"set_grain": {"grain": "week"}}], "by week")
    run = Q.run_calls(trend, dt.date(2026, 12, 1))[0]
    assert run.set_args["period"] == "2026-11-01 to 2026-11-30" and run.set_kept_dates and run.kept_dates


def test_stale_empty_only_for_an_unchanged_call_that_found_rows():
    plan = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}, "nonempty": True},
                    {"tool": "count_records", "args": {"object": "Account"}, "nonempty": False}])
    runs = Q.run_calls(plan, TODAY)
    zero = ev("count_records", {"object": "Interview__c"}, {"count": 0})
    assert Q.stale_empty(runs[0], zero)
    assert not Q.stale_empty(runs[0], ev("count_records", {"object": "Interview__c"}, {"count": 2}))
    assert not Q.stale_empty(runs[1], zero)
    touched = Q.run_calls(plan, TODAY, touched={0})[0]
    assert not touched.expect_rows and not Q.stale_empty(touched, zero)
    assert not Q.stale_empty(runs[0], ev("count_records", {"object": "Interview__c"}, ok=False))


def test_build_after_a_seeded_run_keeps_the_stored_form_and_drops_the_set_rerun():
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"],
                    "set_digest": Q.keys_digest(["Person Alpha", "Person Beta"])}])
    patched = patch(grp, [{"set_grain": {"grain": "month"}}], "by month")
    later = dt.date(2026, 12, 2)
    run = Q.run_calls(patched, later)[0]
    set_ev = group_ev(run.set_args, applied=NOV)
    item, _status = Q.keys_filter(run.keys_from, Q.set_keys(set_ev))
    sent = Q.with_item(run.tool, run.args, item)
    trend_ev = ev("trend_records", sent, {"buckets": [{"bucket": "2026-11", "value": 4}], "total": 4}, NOV)
    extra = ev("count_records", {"object": "Interview__c"}, {"count": 9})
    st = Q.build("records", evidence=[set_ev, trend_ev, extra], seeds=[run], terms={"keys": [], "text": "by month"})
    calls = st["plan"]["calls"]
    assert [c["tool"] for c in calls] == ["trend_records", "count_records"]
    assert calls[0]["args"] == patched.plan["calls"][0]["args"]   # "this month", no in filter
    assert calls[0]["keys_from"] == patched.plan["calls"][0]["keys_from"]
    assert calls[0]["period"]["from"] == "2026-11-01" and calls[0]["nonempty"] is True
    assert "Person Alpha" not in json.dumps(st)


def test_build_never_keeps_a_record_id_the_person_did_not_type():
    rid = "a0A000000000042AAA"
    by_id = ev("count_records", {"object": "Interview__c", "filters": [{"field": "Id", "op": "in", "values": [rid]}]},
               {"count": 1})
    st = records_state([group_ev(), by_id], terms={"keys": [], "text": "how many interviews per support person"})
    assert [c["tool"] for c in st["plan"]["calls"]] == ["group_records"] and rid not in json.dumps(st)
    typed = records_state([by_id], terms={"keys": [], "text": f"how many interviews have Id {rid}"})
    assert typed["plan"]["calls"][0]["args"]["filters"][0]["values"] == [rid]
    word = ev("count_records", {"object": "Interview__c", "filters": [{"field": "Interview_Status__c", "op": "eq",
                                                                       "value": "RescheduledTwice"}]}, {"count": 1})
    assert records_state([word])["plan"] is not None   # a word of 15 letters is no Id


def test_build_maps_an_agent_resend_of_a_seeded_call_to_the_seed():
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"],
                    "set_digest": Q.keys_digest(["Person Alpha", "Person Beta"])}])
    patched = patch(grp, [{"drill": {"rank": 1}}], "the first one")
    run = Q.run_calls(patched, TODAY)[0]
    set_ev = group_ev(run.set_args)
    item, status = Q.keys_filter(run.keys_from, Q.set_keys(set_ev))
    assert status == "ok"
    sent = Q.with_item(run.tool, run.args, item)
    rows = {"rows": [{"Name": "I-1"}], "returned": 1, "total_count": 1}
    first, again = ev("list_records", sent, rows, NOV), ev("list_records", copy.deepcopy(sent), rows, NOV)
    st = Q.build("records", evidence=[set_ev, first, again], seeds=[run], terms={"keys": [], "text": "the first one"})
    calls = st["plan"]["calls"]
    assert len(calls) == 1 and calls[0]["keys_from"]["rank"] == 1 and "Person Alpha" not in json.dumps(st)


# --------------------------------------------------------------------------------------------------- the plan line
def test_plan_line_holds_no_figure_but_dates_limits_and_typed_values():
    args = {"object": "Interview__c", "group_by": "Interview_Support_Person__c", "date_field": "Date_of_Interview__c",
            "period": "last month", "top": 5, "filters": [{"field": "Client_Bill_Rate__c", "op": "gt", "value": 50},
                                                          {"field": "Interview_Outcome__c", "op": "eq",
                                                           "value": "Offer Received"}]}
    result = {"groups": [{"group": "Person Alpha", "value": 1234}, {"group": "Person 77", "value": 84}],
              "group_count": 2, "total_across_groups": 1318}
    count = ev("count_records", {"object": "Interview__c"}, {"count": 4321})
    st = records_state([count, group_ev(args, result, period("last month", "2026-10-01", "2026-10-31"))],
                       terms={"keys": ["candidates"], "text": "bill rate over 50"},
                       show={"chart": True, "type": "horizontal_bar"})
    line = Q.plan_line(st, label=lambda obj, path=None: None)
    figures = set(re.findall(r"\d+", re.sub(r"\d{4}-\d{2}-\d{2}", " ", line)))
    assert figures <= {"5", "50"}, (figures, line)
    assert "2026-10-01 to 2026-10-31" in line and "as a horizontal bar chart" in line
    assert "Person Alpha" not in line and "Person 77" not in line and len(line) <= Q.MAX_PLAN_LINE
    # the stored question is the line written with the view: no candidates scope on a call the contract does not
    # scope (step-5 fix W2); without the view the terms alone still write it
    assert Q.plan_line(st, label=lambda obj, path=None: None, view=VIEW) == st["plan"]["question"]
    assert "candidates as Person Accounts" in line and "Person Accounts" not in st["plan"]["question"]


def test_plan_line_words_labels_and_empty():
    st = records_state([group_ev()])
    labels = {("Interview__c", None): "Interviews", ("Interview__c", "Interview_Support_Person__c"): "support person"}
    line = Q.plan_line(st, label=lambda obj, path=None: labels.get((obj, path)))
    assert line.startswith("number of Interviews per support person, this month (2026-11-01 to 2026-11-30), top 5")
    assert Q.plan_line(None) == "" and Q.plan_line(Q.build("refused")) == ""
    assert Q.plan_line(st["plan"]) == Q.plan_line(st)
    huge = dict(GROUP_ARGS, filters=[{"field": "Region__c", "op": "in", "values": ["v" * 150] * 20}])
    assert len(Q.plan_line(records_state([group_ev(huge)]))) <= Q.MAX_PLAN_LINE
    # an absolute period reads as its dates only
    absolute = plan_of([{"tool": "count_records", "args": {"object": "Interview__c",
                                                           "date_field": "Date_of_Interview__c",
                                                           "period": "2026-10-01 to 2026-10-31"}}])
    assert Q.plan_line(absolute, today=TODAY).endswith("2026-10-01 to 2026-10-31")


def test_plan_line_words_parse_and_fill():
    assert all(": " in ln for ln in Q.PLAN_LINE_WORDS.split("\n"))
    assert set(Q._WORDS) >= {"count", "group", "trend", "compare", "list", "where", "period", "top", "chart", "set"}


def test_chart_tools_match_the_charts_module():
    from sf_assistant.agent import charts
    assert Q._CHARTABLE == charts.CHART_ARG_TOOLS


def test_make_pending_caps_and_drops_bad_choices():
    choices = [{"label": f"L{i}", "question": f"Question {i}", "ops": [{"set_top": {"n": i + 1}}]} for i in range(6)]
    choices.insert(1, {"label": "", "question": "no label"})
    choices.insert(2, {"label": "bad ops", "question": "Q", "ops": [{"explode": {}}]})
    p = Q.make_pending("Which one?", "visualize it", choices)
    assert [c["n"] for c in p["choices"]] == [1, 2, 3, 4] and len(p["choices"]) == Q.MAX_CHOICES
    assert p["choices"][1] == {"n": 2, "label": "bad ops", "question": "Q", "ops": []}
    assert Q.make_pending("?", "x", []) is None
    st = Q.build("clarify", parent=records_state([group_ev()]), pending=p)
    assert Q.check(json.loads(json.dumps(st)), view=VIEW).pending == p


def test_typed_value_rules():
    assert Q.typed_value("North America", "only for north-america please")
    assert not Q.typed_value("North America", "only for north")
    assert Q.typed_value("Person 7", "what about person 7?") and not Q.typed_value("Person 7", "person 77")
    assert Q.typed_value(50, "more than 50") and Q.typed_value(50.0, "above 50") and not Q.typed_value(5, "50")
    assert Q.typed_value(True, "") and not Q.typed_value("", "anything") and not Q.typed_value("x", None)


def test_terms_for_is_pure_and_never_reads_the_state_flags():
    stored = {"keys": ["candidates"], "text": "how many candidates of all account types", "all_types": False}
    t = Q.terms_for(stored, "same for last month")
    assert isinstance(t, Terms) and t.all_types is True   # from the typed words, never from a stored flag
    assert Q.terms_for({"keys": ["candidates"], "text": "how many candidates", "all_types": True},
                       "same for last month").all_types is False
    assert detect("") == NO_TERMS


# ------------------------------------------------------------------------------------ the stage-1 review fixes
def test_set_period_after_compare_period_on_a_grouping_asks_which_side():
    """M10: compare_period on a grouping adds a side call with the other period; a later set_period asks which side
    instead of moving both calls to the same period."""
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}])
    cmp = patch(grp, [{"compare_period": {"phrase": "last month"}}], "compare with last month")
    assert cmp.clarify is None and len(cmp.plan["calls"]) == 2
    nxt = json.loads(json.dumps(cmp.plan))
    nxt["calls"] = [dict(c, nonempty=True) for c in nxt["calls"]]
    got = patch(nxt, [{"set_period": {"phrase": "Q3 2026"}}], "same for Q3 2026")
    assert got.clarify is not None and got.clarify.code == "which_side"
    # a side call of another shape is no compare: set_period moves every dated call, as before
    two = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]},
                   {"tool": "count_records", "args": {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                                      "period": "this month"}}])
    moved = patch(two, [{"set_period": {"phrase": "Q3 2026"}}], "same for Q3 2026")
    assert moved.clarify is None and [c["args"]["period"] for c in moved.plan["calls"]] == ["Q3 2026"] * 2


def test_build_leaves_out_a_call_with_a_name_or_figure_of_a_result():
    """F2 / M11 (§2.4, §16.1, C20, D14): a call of the record agent's that filters by a name or a figure it read in a
    result is not kept, so neither reaches the state or the plan line; typed values, dates, true/false, picklist
    values and record types are kept."""
    typed = "who supported the most interviews this month, and how many offers did that person get?"
    terms = {"keys": [], "text": typed}
    top = group_ev(dict(GROUP_ARGS, top=1), {"groups": [{"group": "Person Alpha", "value": 12}], "group_count": 1})
    by_name = ev("count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                   "period": "this month", "filters": [
                                       {"field": "Interview_Support_Person__c.Name", "op": "eq", "value": "Person Alpha"},
                                       {"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]},
                 {"count": 5}, NOV)
    st = records_state([top, by_name], terms=terms)
    assert [c["tool"] for c in st["plan"]["calls"]] == ["group_records"]
    assert "Person Alpha" not in json.dumps(st) and "Person Alpha" not in Q.plan_line(st)
    avg = ev("aggregate_records", {"object": "Interview__c", "op": "avg", "field": "Client_Bill_Rate__c"},
             {"value": 4321.5, "records_matched": 30, "records_with_value": 30})
    above = ev("count_records", {"object": "Interview__c", "filters": [
        {"field": "Client_Bill_Rate__c", "op": "gt", "value": 4321.5}]}, {"count": 11})
    st = records_state([avg, above], terms={"keys": [], "text": "interviews above the average bill rate"})
    assert [c["tool"] for c in st["plan"]["calls"]] == ["aggregate_records"] and "4321" not in json.dumps(st)
    # kept: a typed figure and name, a picklist value, a date, true/false, a record type, a min_value of 2
    kept = ev("count_records", {"object": "Interview__c", "filters": [
        {"field": "Client_Bill_Rate__c", "op": "gt", "value": 50},
        {"field": "Interviewer_Name__c", "op": "eq", "value": "Person Gamma"},
        {"field": "Region__c", "op": "in", "values": ["north america", "Europe"]},
        {"field": "Date_of_Interview__c", "op": "gte", "value": "2026-11-01"},
        {"field": "Candidate__c.Is_Active__c", "op": "eq", "value": True},
        {"field": "Candidate__c.RecordTypeId", "op": "eq", "value": "B2B Client"}]}, {"count": 3})
    dup = ev("group_records", {"object": "Account", "group_by": "Name", "min_value": 2},
             {"groups": [{"group": "x", "value": 2}], "group_count": 1})
    st = records_state([kept, dup], terms={"keys": [], "text": "rate over 50 with interviewer person gamma"})
    assert [c["tool"] for c in st["plan"]["calls"]] == ["count_records", "group_records"]
    # without a view nothing is schema data: only typed values, dates and true/false
    st = records_state([kept], terms={"keys": [], "text": "rate over 50 with interviewer person gamma"}, view=None)
    assert st["plan"] is None
    # a figure as a group's min_value is a result's too
    above_avg = ev("group_records", {"object": "Interview__c", "group_by": "Recruiter__c", "min_value": 17},
                   {"groups": [{"group": "x", "value": 20}], "group_count": 1})
    assert records_state([above_avg])["plan"] is None
    assert records_state([above_avg], terms={"keys": [], "text": "recruiters with at least 17"})["plan"] is not None


def test_build_writes_compare_labels_in_codes_words():
    """F2: a compare call's segment labels are model text; a label nobody typed becomes the segment's period words."""
    cmp_ = ev("compare_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c", "segments": [
        {"label": "this month (Person Alpha led with 12)", "period": "this month"},
        {"label": "Region Pune", "filters": [{"field": "Region__c", "op": "eq", "value": "Pune"}]},
        {"label": "last month", "period": "last month"}]},
        {"segments": [{"label": "a", "value": 30}, {"label": "b", "value": 25}, {"label": "c", "value": 1}]})
    st = records_state([cmp_], terms={"keys": [], "text": "compare this month, Pune and last month"})
    labels = [s["label"] for s in st["plan"]["calls"][0]["args"]["segments"]]
    assert labels == ["this month", "part 2", "last month"]
    assert [s["label"] for s in st["plan"]["calls"][0]["segments"]] == labels
    line = Q.plan_line(st, today=TODAY)
    assert "Person Alpha" not in json.dumps(st) and "led with" not in line
    assert line.startswith("number of Interview records: this month (2026-11-01 to 2026-11-30) vs part 2 where")


def test_apply_honours_a_lower_call_cap():
    """L1: SFA_MEMORY_MAX_CALLS reaches apply (compare_period's side call and the final cap)."""
    two = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]},
                   {"tool": "count_records", "args": {"object": "Interview__c"}}])
    ops = [{"compare_period": {"phrase": "last month"}}]
    assert patch(two, ops, "compare with last month").clarify is None
    got = patch(two, ops, "compare with last month", max_calls=2)
    assert got.clarify is not None and got.clarify.code == "call_cap"
    one = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}])
    assert patch(one, ops, "compare with last month", max_calls=1).clarify.code == "call_cap"


def test_check_names_the_link_field_of_a_record_set():
    """L2: the field of the call that a record set's keys filter (keys_from.field) is checked too."""
    set_call = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Active"}],
                "limit": 5}
    plan = {"question": "q", "terms": {"keys": [], "text": ""}, "age": 0,
            "show": {"chart": False, "type": None, "table": False},
            "calls": [{"role": "main", "tool": "list_records", "args": {"object": "Interview__c"}, "nonempty": True,
                       "keys_from": {"field": "Candidate__c", "tool": "list_records", "args": set_call,
                                     "digest": None, "rank": None}}]}
    state = {"v": 1, "made_at": "", "kb": "", "profile": None, "plan": plan, "pending": None,
             "last": {"kind": "records", "reason": "", "op": None}}
    assert Q.check(state, view=VIEW).missing == ()
    objects = copy.deepcopy(OBJECTS)
    del objects["Interview__c"]["Candidate__c"]
    got = Q.check(state, view=FakeView(objects))
    assert got.missing == ("Interview__c.Candidate__c",) and not Q.usable(got)


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "２０２６-01-01"])
def test_check_refuses_an_impossible_stored_date(bad):
    """L6: a stored date must be a real calendar date in ASCII digits, so run_calls never sends dates no parser
    reads and apply never raises on them."""
    call = {"role": "main", "tool": "count_records", "nonempty": True,
            "args": {"object": "Account", "date_field": "CreatedDate", "period": "this month"},
            "period": {"phrase": "this month", "from": "2026-11-01", "to": bad, "date_field": "CreatedDate"}}
    state = {"v": 1, "made_at": "", "kb": "", "profile": None,
             "plan": {"question": "q", "terms": {"keys": [], "text": ""}, "calls": [call],
                      "show": {"chart": False, "type": None, "table": False}, "age": 0},
             "pending": None, "last": {"kind": "records", "reason": "", "op": None}}
    got = Q.check(state, view=VIEW)
    assert got.state is None and got.reason == "state_invalid" and "plan.calls[0].period" in got.problems
    good = copy.deepcopy(state)
    good["plan"]["calls"][0]["period"]["to"] = "2026-11-30"
    assert Q.check(good, view=VIEW).reason == ""


def test_top_n_on_a_list_keeps_its_sort_and_bottom_n_asks():
    """M9: "top 5" of "the 10 earliest interviews" is the first 5 rows as shown: only the limit changes. Its bottom 5
    is no query of its own, so it asks."""
    lst = plan_of([{"tool": "list_records", "args": {"object": "Interview__c", "order_by": "Date_of_Interview__c",
                                                     "order": "asc", "limit": 10}}])
    got = patch(lst, [{"set_top": {"n": 5, "order": "desc"}}], "only top 5")
    assert got.clarify is None and main_call(got.plan)["args"]["limit"] == 5
    assert main_call(got.plan)["args"]["order"] == "asc"
    assert patch(lst, [{"set_top": {"n": 5}}], "first 5").plan["calls"][0]["args"]["order"] == "asc"
    bottom = patch(lst, [{"set_top": {"n": 3, "order": "asc"}}], "bottom 3")
    assert bottom.clarify is not None and bottom.clarify.code == "list_bottom" and bottom.clarify.value == "3"
    # a grouping's bottom N is its lowest groups, as before
    grp = plan_of([{"tool": "group_records", "args": GROUP_ARGS, "period": NOV["period"]}])
    assert main_call(patch(grp, [{"set_top": {"n": 3, "order": "asc"}}], "bottom 3").plan)["args"]["order"] == "asc"


def test_plan_line_states_the_scope_that_runs():
    """M2: the candidates scope is written only when the run keeps it."""
    count = [{"tool": "count_records", "args": {"object": "Account", "date_field": "CreatedDate",
                                                "period": "this month"}}]
    relaxed = plan_of(count, terms={"keys": ["candidates"],
                                    "text": "how many candidates of all account types joined this month"})
    line = Q.plan_line(relaxed, today=TODAY)
    assert "Person Accounts" not in line and "every account type" in line
    scoped = plan_of(count, terms={"keys": ["candidates"], "text": "how many candidates joined this month"})
    assert "candidates as Person Accounts" in Q.plan_line(scoped, today=TODAY)
    # the run's terms win: a narrowing typed now scopes the line of the relaxed plan
    narrowed = Q.terms_for(relaxed["terms"], "only candidates")
    assert "candidates as Person Accounts" in Q.plan_line(relaxed, today=TODAY, terms=narrowed)
    # other account types named, and the call filters the record type itself: its filter says the scope
    typed = plan_of([{"tool": "count_records", "args": {"object": "Account", "filters": [
        {"field": "RecordTypeId", "op": "eq", "value": "B2B Client"}]}}],
        terms={"keys": ["candidates"], "text": "how many candidates and B2B accounts"})
    assert "Person Accounts" not in Q.plan_line(typed, today=TODAY)


def test_state_size_is_the_platforms_measure():
    """F9: the cap is measured as the platform's store measures it (json.dumps with ensure_ascii=False and the
    default separators), so a state this side returns is never stored NULL by the bridge for its size."""
    st = records_state([group_ev()])
    platform = len(json.dumps(st, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8"))
    assert Q.state_bytes(st) == platform
    assert Q.state_bytes(json.loads(json.dumps(st, sort_keys=True))) == platform   # key order changes nothing
    st["plan"]["question"] = ""   # fill the state to exactly the cap, then one byte over
    st["plan"]["question"] = "x" * (Q.MAX_STATE_BYTES - Q.state_bytes(st))
    assert Q.state_bytes(st) == Q.MAX_STATE_BYTES and Q.fit(st) is st
    st["plan"]["question"] += "x"
    assert Q.fit(st) is None and Q.check(st).reason == "state_too_large"


def test_a_blocked_field_is_refused_at_patch_time_and_named_by_check():
    """F12: a blocked field (credentials, passcodes) is refused by the patch checks like an unknown field, so no plan
    ever names it, and a stored call that names one is not usable."""
    view = FakeView(blocked=("Interviewer_Name__c",))
    plan = plan_of([{"tool": "count_records", "args": {"object": "Interview__c"}}])
    for ops in ([{"set_group": {"field": "Interviewer_Name__c"}}],
                [{"add_filter": {"field": "Interviewer_Name__c", "op": "is_not_blank"}}],
                [{"set_measure": {"measure": "count_distinct", "field": "Interviewer_Name__c"}}]):
        got = Q.apply(plan, ops, typed="by interviewer name", view=view, today=TODAY)
        assert got.clarify is not None and got.clarify.code == "unknown_field", ops
        assert Q.apply(plan, ops, typed="by interviewer name", view=VIEW, today=TODAY).clarify is None
    stored = records_state([ev("group_records", {"object": "Interview__c", "group_by": "Interviewer_Name__c"},
                               {"groups": [{"group": "x", "value": 1}], "group_count": 1})])
    got = Q.check(stored, view=view)
    assert got.missing == ("Interview__c.Interviewer_Name__c",) and not Q.usable(got)
