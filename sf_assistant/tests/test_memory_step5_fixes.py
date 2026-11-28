"""The fixes of the real-model evaluation of conversation memory (step 5 of 2026-11-27; notes step5_model_eval.md):
W3 (a relaxed candidates scope with a type filter the record agent wrote itself), W1 (the main call of an answer
that ran several counts), W2 (the scope words of the plan line from the filters that run), the field names the
resolver model guesses (grounded on the plan's object, and listed in its prompt), dead-end calls of the record agent
and the Read-as words of a lookup's name.

Model-free and duckdb-free, on the made-up catalog view of ``tests/test_query_state.py`` with a field list added;
every name and value is synthetic. The chains of the same failures run end to end in ``tests/chains/step5_fixes.json``.
"""

from __future__ import annotations

import dataclasses

import test_query_state as TQ
from test_query_state import NO_TERMS, TODAY, VIEW, ev, list_ev, period, plan_of, records_state

from sf_assistant.agent import query_state as Q
from sf_assistant.agent import resolver as MR
from sf_assistant.engine.term_contracts import Terms, detect

R = TQ.R
#: Labels of the made-up fields (the API name in words otherwise).
LABELS = {"Interview_Status__c": "Interview Status", "Client_Account__c": "Client Account",
          "Client_Bill_Rate__c": "Client Bill Rate", "Recruiter__c": "Recruiter", "Department__c": "Department",
          "Name": "Name", "Status__c": "Status", "Team__c": "Team", "Region__c": "Region"}
PLURALS = {"Interview__c": "Interviews", "Internal_Interview__c": "Internal Interviews", "Account": "Accounts",
           "Recruiter__c": "Employees", "Department__c": "Departments", "Candidate_Training__c": "Candidate Trainings"}


class FieldsView(TQ.FakeView):
    """The made-up view with the field list (``query_state.PlanView.fields_of``) and labels."""

    def fields_of(self, obj):
        out = []
        for api, kind in self.objects.get(self.object(obj) or "", {}).items():
            k = "reference" if isinstance(kind, tuple) else kind
            if k in ("email", "phone"):
                continue
            out.append((api, LABELS.get(api, api), k))
        return out

    def label(self, obj, path=None):
        if path is None:
            return PLURALS.get(self.object(obj) or "")
        p = self.path(self.object(obj), path)
        return LABELS.get(p.field) if p is not None else None


#: Interview__c of the evaluation's fixture: no department of its own (the recruiter's is), a client lookup and a
#: client bill rate, an interview status (and other status-like names on other objects).
OBJECTS = {
    "Interview__c": {"Name": "auto_number", "Interview_Status__c": "picklist", "Interview_Outcome__c": "picklist",
                     "Date_of_Interview__c": "date", "Region__c": "picklist", "Client_Bill_Rate__c": "currency",
                     "Client_Account__c": (R, "Account"), "Recruiter__c": (R, "Recruiter__c"),
                     "Interview_Support_Person__c": (R, "Recruiter__c"), "Candidate__c": (R, "Account")},
    "Internal_Interview__c": {"Name": "auto_number", "Status__c": "picklist", "CreatedDate": "datetime",
                              "Recruiter__c": (R, "Recruiter__c")},
    "Account": dict(TQ.OBJECTS["Account"]),
    "Recruiter__c": {"Name": "name", "Department__c": (R, "Department__c"), "Team__c": (R, "Department__c"),
                     "Email__c": "email"},
    "Department__c": {"Name": "name"},
    "RecordType": {"Name": "name", "DeveloperName": "text"},
    "Candidate_Training__c": dict(TQ.OBJECTS["Candidate_Training__c"]),
}
FV = FieldsView(OBJECTS)
PER_RECRUITER = {"object": "Interview__c", "group_by": "Recruiter__c", "date_field": "Date_of_Interview__c",
                 "period": "this year"}


def _patch(plan, ops, typed, terms=NO_TERMS, view=FV):
    return Q.apply(plan, ops, typed=typed, view=view, today=TODAY, terms=terms)


def _plan(calls, view=FV, question="q", terms=None):
    out = []
    for i, c in enumerate(calls):
        c = dict(c)
        c.setdefault("role", "main" if i == 0 else "side")
        c.setdefault("nonempty", True)
        out.append(c)
    state = {"v": 1, "made_at": "", "kb": "", "profile": None,
             "plan": {"question": question, "terms": terms or {"keys": [], "text": ""}, "calls": out,
                      "show": {"chart": False, "type": None, "table": False}, "age": 0},
             "pending": None, "last": {"kind": "records", "reason": "", "op": None}}
    got = Q.check(state, view=view)
    assert got.state is not None, got
    return got.state["plan"]


# ------------------------------------------------------------------------------------------------ W3
_PERSON = [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}]
_BY_STATUS = {"object": "Account", "group_by": "Candidate_Status__c", "filters": _PERSON}


def _candidates(text, **flags):
    return dataclasses.replace(detect(text), keys=frozenset({"candidates"}), **flags)


def test_w3_every_account_type_drops_the_person_account_filter_the_record_agent_wrote():
    plan = plan_of([{"tool": "group_records", "args": _BY_STATUS}], terms={"keys": ["candidates"], "text": "x"})
    terms = _candidates("show the top 2 of all account types")
    assert terms.all_types
    got = Q.apply(plan, [{"set_top": {"n": 2, "order": "desc"}}], typed="show the top 2 of all account types",
                  view=VIEW, today=TODAY, terms=terms)
    call = got.plan["calls"][0]
    assert "filters" not in call["args"] and call["args"]["top"] == 2 and got.touched == frozenset({0})
    line = Q.plan_line(got.plan, terms=terms, view=VIEW)
    assert "every account type" in line and "RecordTypeId" not in line and "Person Accounts" not in line
    # what runs is what the line says: the contract adds no filter under these terms
    assert not Q._term_plan(terms, call["tool"], call["args"], VIEW).add


def test_w3_the_filter_stays_unless_the_person_widens_the_scope():
    plan = plan_of([{"tool": "group_records", "args": _BY_STATUS}], terms={"keys": ["candidates"], "text": "x"})
    for typed in ("show the top 2", "only candidates, top 2"):
        got = Q.apply(plan, [{"set_top": {"n": 2, "order": "desc"}}], typed=typed, view=VIEW, today=TODAY,
                      terms=_candidates(typed))
        assert got.plan["calls"][0]["args"]["filters"] == _PERSON, typed
    # the model's standalone never relaxes (§7.1): terms_for reads the flags from the typed words only
    terms = Q.terms_for({"keys": ["candidates"], "text": "how many candidates by status"}, "top 2",
                        "top 2 of all account types")
    got = Q.apply(plan, [{"set_top": {"n": 2, "order": "desc"}}], typed="top 2", view=VIEW, today=TODAY, terms=terms)
    assert got.plan["calls"][0]["args"]["filters"] == _PERSON


def test_w3_other_account_types_drop_it_only_beside_a_filter_on_another_type():
    plan = plan_of([{"tool": "group_records", "args": _BY_STATUS}], terms={"keys": ["candidates"], "text": "x"})
    terms = _candidates("and the B2B Client accounts", other_accounts=True)
    got = Q.apply(plan, [{"add_filter": {"field": "RecordTypeId.Name", "op": "eq", "value": "B2B Client"}}],
                  typed="and the B2B Client accounts", view=VIEW, today=TODAY, terms=terms)
    assert got.plan["calls"][0]["args"]["filters"] == [{"field": "RecordTypeId.Name", "op": "eq",
                                                        "value": "B2B Client"}]
    alone = Q.apply(plan, [{"set_top": {"n": 2, "order": "desc"}}], typed="and the B2B Client accounts", view=VIEW,
                    today=TODAY, terms=terms)
    assert alone.plan["calls"][0]["args"]["filters"] == _PERSON


# ------------------------------------------------------------------------------------------------ W1
_CLIENT = {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "this month"}
_INTERNAL = {"object": "Internal_Interview__c", "date_field": "CreatedDate", "period": "this month"}
NOV = period("this month", "2026-11-01", "2026-11-30")


def _two_counts(question):
    evidence = [ev("count_records", dict(_CLIENT), {"count": 3}, NOV),
                ev("count_records", dict(_INTERNAL), {"count": 3}, period("this month", "2026-11-01", "2026-11-30",
                                                                          "CreatedDate"))]
    plan, why = Q.plan_from_evidence(evidence, question=question, terms={"keys": [], "text": question},
                                     label=FV.label, view=FV, today=TODAY)
    assert why == ""
    return plan


def test_w1_the_count_the_question_names_is_the_main_call():
    plan = _two_counts("how many interviews this month")
    main = next(c for c in plan["calls"] if c["role"] == "main")
    assert main["args"]["object"] == "Interview__c"
    # a question that names both, or neither, keeps the last call (as charts._pick does)
    for q in ("how many interviews and internal interviews this month", "how many this month"):
        p = _two_counts(q)
        assert next(c for c in p["calls"] if c["role"] == "main")["args"]["object"] == "Internal_Interview__c", q


def test_w1_compare_with_previous_month_compares_every_count_the_answer_showed():
    plan = _two_counts("how many interviews this month")
    got = _patch(plan, [{"compare_period": {"phrase": "previous month"}}], "Compare with previous month")
    assert [(c["role"], c["tool"], c["args"]["object"]) for c in got.plan["calls"]] == [
        ("main", "compare_records", "Interview__c"), ("side", "compare_records", "Internal_Interview__c")]
    for c in got.plan["calls"]:
        assert [s["period"] for s in c["args"]["segments"]] == ["this month", "previous month"]
    assert got.touched == frozenset({0, 1})
    line = Q.plan_line(got.plan, today=TODAY, label=FV.label, view=FV)
    assert line.count("vs previous month") == 2 and "Internal Interviews" in line


def test_w1_an_operation_on_the_main_call_alone_asks_when_the_main_call_is_not_the_named_one():
    unsure = _two_counts("how many this month")
    got = _patch(unsure, [{"set_group": {"field": "Recruiter__c"}}], "group by recruiter")
    assert got.plan is None and got.clarify.code == "which_call"
    assert "which_call" in Q.CLARIFY_CODES and "clarify_which_call" in MR.LINES
    sure = _two_counts("how many interviews this month")
    got = _patch(sure, [{"set_group": {"field": "Recruiter__c"}}], "group by recruiter")
    assert got.clarify is None and TQ.main_call(got.plan)["args"]["group_by"] == "Recruiter__c"
    # a grouping's main call is what the answer ranked: nothing asks
    grouped = _plan([{"tool": "group_records", "args": PER_RECRUITER},
                     {"tool": "count_records", "args": dict(_INTERNAL)}])
    assert _patch(grouped, [{"set_top": {"n": 3}}], "top 3").clarify is None


# ------------------------------------------------------------------------------------------------ W2
def test_w2_the_candidates_words_come_from_the_filter_that_runs():
    terms = {"keys": ["candidates"], "text": "status wise kitne candidates hain"}
    trainings = plan_of([{"tool": "group_records", "args": {"object": "Candidate_Training__c",
                                                            "group_by": "Status__c"}}], terms=terms)
    assert "Person Accounts" not in Q.plan_line(trainings, view=VIEW)
    assert "Person Accounts" in Q.plan_line(trainings)   # without a view the terms alone decide, as before
    accounts = plan_of([{"tool": "group_records", "args": {"object": "Account", "group_by": "Candidate_Status__c"}}],
                       terms=terms)
    assert "candidates as Person Accounts" in Q.plan_line(accounts, view=VIEW)
    # a lookup to the candidate gets the filter on its path: the words stay
    per_candidate = plan_of([{"tool": "group_records", "args": {"object": "Candidate_Training__c",
                                                                "group_by": "Candidate__c"}}], terms=terms)
    assert "candidates as Person Accounts" in Q.plan_line(per_candidate, view=VIEW)


# ------------------------------------------------------------------------------------------------ the field names
def test_fields_a_guessed_status_field_is_the_one_its_value_fits():
    plan = _plan([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c"}}])
    got = _patch(plan, [{"add_filter": {"field": "Status__c", "op": "eq", "value": "completed"}}],
                 "and how many were completed?")
    assert got.clarify is None
    assert got.plan["calls"][0]["args"]["filters"] == [{"field": "Interview_Status__c", "op": "eq",
                                                        "value": "completed"}]
    assert got.ops == ({"add_filter": {"field": "Interview_Status__c", "op": "eq", "value": "completed"}},)
    # without a field list (or with a view that cannot list fields) it asks as before
    assert _patch(plan, [{"add_filter": {"field": "Status__c", "op": "eq", "value": "completed"}}],
                  "and how many were completed?", view=TQ.FakeView(OBJECTS)).clarify.code == "unknown_field"


def test_fields_a_guessed_client_field_is_the_lookup_a_name_fits():
    plan = _plan([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c"}}])
    got = _patch(plan, [{"add_filter": {"field": "Client__c", "op": "eq", "value": "Client Co"}}],
                 "narrow that down to Client Co")
    assert got.clarify is None and got.plan["calls"][0]["args"]["filters"][0]["field"] == "Client_Account__c"
    # a number goes to the number field of the same words
    got = _patch(plan, [{"add_filter": {"field": "Client__c", "op": "gt", "value": 50}}], "client rate over 50")
    assert got.plan["calls"][0]["args"]["filters"][0]["field"] == "Client_Bill_Rate__c"


def test_fields_a_department_grouping_goes_through_the_lookup_the_plan_groups_by():
    plan = _plan([{"tool": "group_records", "args": PER_RECRUITER}])
    got = _patch(plan, [{"set_group": {"field": "Department__c"}}], "Group by department instead")
    assert got.clarify is None and got.plan["calls"][0]["args"]["group_by"] == "Recruiter__c.Department__c"
    assert "per Department" in Q.plan_line(got.plan, today=TODAY, label=FV.label, view=FV)
    # two lookups lead to a department and the plan takes neither: it asks which field
    per_status = _plan([{"tool": "group_records",
                         "args": {"object": "Interview__c", "group_by": "Interview_Status__c"}}])
    assert _patch(per_status, [{"set_group": {"field": "Department__c"}}],
                  "group by department").clarify.code == "unknown_field"


def test_fields_never_replace_a_field_that_resolves_or_guess_between_two():
    plan = _plan([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c"}}])
    got = _patch(plan, [{"add_filter": {"field": "Region__c", "op": "eq", "value": "Europe"}}], "only Europe")
    assert got.plan["calls"][0]["args"]["filters"][0]["field"] == "Region__c"
    # "Interview" names several fields of the object: no guess
    assert _patch(plan, [{"add_filter": {"field": "Interview__c", "op": "eq", "value": "x"}}],
                  "only x").clarify.code == "unknown_field"
    # a remove names a filter the plan holds by its words
    filtered = _plan([{"tool": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c",
                                                         "filters": [{"field": "Interview_Status__c", "op": "eq",
                                                                      "value": "Completed"}]}}])
    got = _patch(filtered, [{"remove_filter": {"field": "Status__c"}}], "remove the status filter")
    assert got.clarify is None and "filters" not in got.plan["calls"][0]["args"]


def test_fields_the_resolver_prompt_lists_the_plans_fields_within_its_cap():
    plan = _plan([{"tool": "group_records", "args": PER_RECRUITER}, {"tool": "count_records", "args": _INTERNAL}])
    text = MR.field_lines(plan, FV)
    lines = text.split("\n")
    assert lines[0].startswith("Interview__c: ") and lines[1].startswith("Internal_Interview__c: ")
    assert "Interview_Status__c: Interview Status" in lines[0] and "Recruiter__c: Recruiter -> Recruiter__c" in lines[0]
    assert "Email__c" not in text and MR.estimate_tokens(text) <= MR.FIELDS_TOKENS
    assert MR.field_lines(plan, TQ.FakeView(OBJECTS)) == "" and MR.field_lines(None, FV) == ""
    msgs, info = MR.build_messages("only completed", [], plan=plan, plan_line="number of Interviews per Recruiter",
                                   fields=text)
    user = msgs[1]["content"]
    assert info["fields"] and user.index(MR.LINES["head_plan"]) < user.index(MR.LINES["head_fields"]) < \
        user.index(MR.LINES["head_turns"])
    assert "FIELDS" in MR.FOLLOWUP_RESOLVE_SYSTEM
    # no plan, no fields; above the cap the fields go before any turn is cut
    assert not MR.build_messages("q", [], fields=text)[1]["fields"]
    turns = [{"role": "user", "content": "w " * 900}, {"role": "assistant", "content": "v " * 900}]
    base = MR.build_messages("only completed", turns, plan=plan, plan_line="p")[1]["tokens"]
    _m, tight = MR.build_messages("only completed", turns, plan=plan, plan_line="p", fields=text, max_tokens=base)
    assert not tight["fields"] and tight["cut_turns"] == 0 and not tight["over"]


def test_fields_the_list_has_no_value_and_no_blocked_or_contact_field():
    text = MR.field_lines(_plan([{"tool": "list_records", "args": {"object": "Recruiter__c"}}]), FV)
    assert text == "Recruiter__c: Name; Department__c: Department -> Department__c; Team__c: Team -> Department__c"


# ------------------------------------------------------------------------------------------------ dead ends (RR-M)
def test_a_zero_row_call_the_record_agent_replaced_is_no_side_call():
    typed = {"keys": [], "text": "list the recruiters of Dept One"}
    dead = list_ev({"object": "Recruiter__c", "filters": [{"field": "Team__c.Name", "op": "eq", "value": "Dept One"}]},
                   [])
    found = list_ev({"object": "Recruiter__c", "filters": [{"field": "Department__c.Name", "op": "eq",
                                                            "value": "Dept One"}]}, ["a0600000000001AAA"])
    st = records_state([dead, found], terms=typed)
    assert [c["args"]["filters"][0]["field"] for c in st["plan"]["calls"]] == ["Department__c.Name"]
    # two counts of the same field, one per typed value, are both the answer
    regions = {"keys": [], "text": "interviews in Pune and in Europe"}
    pune = ev("count_records", {"object": "Interview__c", "filters": [{"field": "Region__c", "op": "eq",
                                                                     "value": "Pune"}]}, {"count": 0})
    europe = ev("count_records", {"object": "Interview__c", "filters": [{"field": "Region__c", "op": "eq",
                                                                       "value": "Europe"}]}, {"count": 2})
    assert len(records_state([pune, europe], terms=regions)["plan"]["calls"]) == 2
    # a zero-row call with another period, or one nothing replaced, stays
    other = ev("count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                 "period": "this month"}, {"count": 0})
    later = ev("count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c",
                                 "period": "last month", "filters": [{"field": "Region__c", "op": "eq",
                                                                      "value": "Pune"}]}, {"count": 4})
    assert len(records_state([other, later], terms={"keys": [], "text": "this month and last month in Pune"})
               ["plan"]["calls"]) == 2
    assert len(records_state([dead], terms=typed)["plan"]["calls"]) == 1
    # a zero count of one typed value beside a count of another field's typed value is a part of the answer
    both = {"keys": [], "text": "interviews in Pune, and how many Completed"}
    done = ev("count_records", {"object": "Interview__c", "filters": [{"field": "Interview_Status__c", "op": "eq",
                                                                     "value": "Completed"}]}, {"count": 5})
    assert len(records_state([pune, done], terms=both)["plan"]["calls"]) == 2


def test_a_list_the_record_agent_listed_again_for_the_same_records_is_no_side_call():
    typed = {"keys": [], "text": "list the recruiters of Dept One"}
    where = [{"field": "Department__c.Name", "op": "eq", "value": "Dept One"}]
    page = list_ev({"object": "Recruiter__c", "fields": ["Name", "Department__c"], "filters": where},
                   ["a0600000000001AAA"])
    shown = list_ev({"object": "Recruiter__c", "fields": ["Name"], "filters": where, "order_by": "Name",
                     "order": "asc", "limit": 50}, ["a0600000000001AAA"])
    calls = records_state([page, shown], terms=typed)["plan"]["calls"]
    assert len(calls) == 1 and calls[0]["args"]["limit"] == 50
    # a list of other records (another filter) stays beside it
    other = list_ev({"object": "Recruiter__c", "fields": ["Name"], "filters": [
        {"field": "Department__c.Name", "op": "eq", "value": "Dept Two"}]}, ["a0600000000002AAA"])
    assert len(records_state([page, other], terms={"keys": [], "text": "Dept One and Dept Two"})["plan"]["calls"]) == 2


# ------------------------------------------------------------------------------------------------ the Read-as words
def test_a_lookups_name_reads_as_the_lookup():
    plan = _plan([{"tool": "list_records", "args": {"object": "Recruiter__c", "filters": [
        {"field": "Department__c.Name", "op": "eq", "value": "Dept One"}]}}])
    line = Q.plan_line(plan, label=FV.label, view=FV)
    assert line == "Employees listed, where Department = 'Dept One'"
    assert Q.plan_line(plan) == "Recruiter records listed, where Department = 'Dept One'"
    # a lookup whose label is its API name reads as the path in words
    by_type = _plan([{"tool": "group_records", "args": {"object": "Account", "group_by": "RecordTypeId.Name"}}])
    assert Q.plan_line(by_type, label=lambda obj, path=None: "RecordTypeId" if path else None).startswith(
        "number of Account records per Record Type Id")


def test_the_fixes_keep_term_flags_out_of_the_state():
    """The relaxation reads the run's terms; nothing of them is stored (C16)."""
    plan = plan_of([{"tool": "group_records", "args": _BY_STATUS}], terms={"keys": ["candidates"], "text": "x"})
    got = Q.apply(plan, [{"set_top": {"n": 2}}], typed="all account types", view=VIEW, today=TODAY,
                  terms=_candidates("all account types"))
    assert set(got.plan["terms"]) == {"keys", "text"} and isinstance(got.plan["terms"]["keys"], list)
    assert isinstance(Terms, type)
