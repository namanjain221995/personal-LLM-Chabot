"""Business-term contracts on a reused plan (C18; design v2 of 2026-11-25 §7.1, test list §16.1): the terms of a run are
rebuilt from the stored keys and typed words, the person's typed follow-up and the model's standalone question
(``agent/query_state.py`` :func:`terms_for`); the flags never come from the model, and every seeded call is enforced
again.

Two parts: pure checks of :func:`terms_for` with the made-up view of ``test_query_state.py``, and the same chains run
through the real record tools over the terms fixture (``tests/terms_fixture.py``, synthetic names), where the terms a
job uses are set as lane S3's ``begin_question(terms=...)`` will set them. Model-free.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.agent import query_state as Q  # noqa: E402
from sf_assistant.engine import term_contracts as TC  # noqa: E402
from test_query_state import VIEW  # noqa: E402

ACTIVE_OR_PLACED = "how many active or placed candidates"
STATUS_IN = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "in",
                                               "values": ["Active", "Placed"]}]}
SUPPORT_DISTINCT = {"object": "Interview__c", "group_by": "Interview_Support_Person__c", "measure": "count_distinct",
                    "measure_field": "Candidate__c"}


def stored(text: str) -> dict:
    """The ``terms`` a state holds after the typed question ``text`` started the chain."""
    return Q.next_terms(None, text, Q.terms_for(None, text, text, "new"), "new")


def reject(terms, tool, args):
    p = TC.plan(terms, tool, args, VIEW)
    return p.reject[0] if p.reject else None


def added(terms, tool, args):
    return [item for _key, item in TC.plan(terms, tool, args, VIEW).add]


# ------------------------------------------------------------------------------------------- the design's five tests
def test_active_or_placed_reruns_on_visualize_it_with_no_t2_reject():
    chain = stored(ACTIVE_OR_PLACED)
    assert chain == {"keys": ["candidates", "placement"], "text": ACTIVE_OR_PLACED}
    terms = Q.terms_for(chain, "visualize it", "Visualize the number of candidates by status")
    assert terms.keys == {"candidates", "placement"} and "active or placed" in terms.question
    assert reject(terms, "count_records", STATUS_IN) is None
    assert added(terms, "count_records", STATUS_IN) == [TC.CANDIDATE_PREDICATE]
    # what v1 would have run: the keys carried without the words is a T2 reject, the typed words alone no contract
    lost = dataclasses.replace(terms, question="visualize it")
    assert reject(lost, "count_records", STATUS_IN) == "T2"
    assert TC.detect("visualize it").keys == frozenset()


def test_all_types_then_only_candidates_applies_the_candidate_scope():
    chain = stored("how many candidates of all account types")
    relaxed = Q.terms_for(chain, "same for last month", "How many candidates of all account types last month")
    assert relaxed.all_types and added(relaxed, "count_records", {"object": "Account"}) == []
    narrowed = Q.terms_for(chain, "only candidates", "How many accounts of all account types are candidates")
    assert not narrowed.all_types and narrowed.keys == {"candidates"}
    assert added(narrowed, "count_records", {"object": "Account"}) == [TC.CANDIDATE_PREDICATE]
    # the same with no candidate word in the first question
    accounts = stored("how many accounts of all account types")
    assert accounts["keys"] == []
    assert added(Q.terms_for(accounts, "only candidates"), "count_records", {"object": "Account"}) == \
        [TC.CANDIDATE_PREDICATE]


def test_a_standalone_that_says_all_account_types_relaxes_nothing():
    chain = stored("how many candidates joined this month")
    terms = Q.terms_for(chain, "what about all of them", "How many accounts of all account types joined this month")
    assert TC.detect("How many accounts of all account types joined this month").all_types
    assert not terms.all_types and added(terms, "count_records", {"object": "Account"}) == [TC.CANDIDATE_PREDICATE]


def test_how_many_candidates_did_they_support_adds_the_person_account_filter_from_the_typed_words():
    chain = stored("interviews per support person this month")
    standalone = "How many people did each support person support this month"
    assert "candidates" not in TC.detect(standalone).keys
    terms = Q.terms_for(chain, "how many candidates did they support", standalone)
    assert terms.keys == {"candidates", "support_performance"}
    want = dict(TC.CANDIDATE_PREDICATE, field="Candidate__c.RecordTypeId")
    assert added(terms, "group_records", SUPPORT_DISTINCT) == [want]
    assert added(TC.detect(standalone), "group_records", SUPPORT_DISTINCT) == []


def test_terms_text_keeps_whole_questions_within_600_characters():
    text = ""
    questions = [f"question {i} " + "word " * 25 for i in range(12)]
    for q in questions:
        text = Q.terms_text(text, q)
        assert len(text) <= Q.MAX_TERMS_TEXT
    kept = text.split("\n")
    assert kept == [" ".join(q.split()) for q in questions[-len(kept):]] and kept[-1].startswith("question 11")
    assert Q.terms_text("", "x " * 400).startswith("x x") and len(Q.terms_text("", "y" * 900)) == Q.MAX_TERMS_TEXT
    assert Q.terms_text("a\n\nb", "  c\n d ") == "a\nb\nc d"


# ------------------------------------------------------------------------------------------------- more of the rule
@pytest.mark.parametrize("flag,words", [
    ("all_types", "regardless of account type"), ("other_accounts", "B2B accounts"), ("leads", "leads"),
    ("sessions", "sessions"), ("preferred", "preferred support person"), ("mock", "mock interviews"),
    ("placed_status", "with status Placed"), ("onboarding", "onboarding"), ("no_candidate", "with no candidate"),
])
def test_no_relaxing_flag_ever_comes_from_the_standalone(flag, words):
    assert getattr(TC.detect(words), flag), words
    chain = stored("how many candidates had interviews this month")
    terms = Q.terms_for(chain, "and last month?", f"How many candidates had interviews last month, {words}")
    assert getattr(terms, flag) is False
    new = Q.terms_for(chain, "and last month?", f"How many interviews last month, {words}", "new")
    assert getattr(new, flag) is False
    assert getattr(Q.terms_for(chain, f"and last month, {words}?", "x"), flag) is True   # typed: it counts


def test_performance_stays_with_the_chain_since_it_widens_nothing():
    chain = stored("support person performance this month")
    assert Q.terms_for(chain, "top 5 only").performance is True              # carried: names nothing
    count = Q.terms_for(chain, "how many candidates did they support")       # names candidates: still the chain's
    assert count.performance is True and "support_performance" in count.keys
    assert Q.terms_for(chain, "how many interviews", "x", "new").performance is False   # a new question starts again


# ------------------------------------------------------------- stage-1 review fixes H1, M1, F1: flags per contract
def chain_after(*typed):
    """The stored ``terms`` after the typed questions: the first one new, the rest patches (as lane S3 builds them)."""
    terms = None
    for i, q in enumerate(typed):
        kind = "new" if i == 0 else "patch"
        t = Q.terms_for(terms, q, q if kind == "new" else "", kind)
        terms = Q.next_terms(terms, q, t, kind)
    return terms


ACCOUNTS = {"object": "Account", "date_field": "CreatedDate", "period": "this month"}


@pytest.mark.parametrize("first", ["how many candidates joined this month",
                                   "how many accounts of all account types were created this month"])
def test_a_narrowing_holds_on_the_turns_after_it(first):
    """H1 / F1 (C18, §14 "a narrowing follow-up after a relaxing one"): relax, narrow, then follow-ups that name
    nothing: the candidates scope holds for the rest of the chain."""
    relax, narrow = "same for last month, all account types", "only candidates, by month"
    chain = chain_after(first, relax)
    assert Q.terms_for(chain, "visualize it").all_types                     # the relaxation, carried
    chain = chain_after(first, relax, narrow)
    assert chain["text"] == "\n".join([first, relax, narrow])
    for later in ("visualize it", "top 5", "by month", "same for last year"):
        t = Q.terms_for(chain, later, "Visualize the number of accounts of all account types by month")
        assert not t.all_types, later
        assert added(t, "count_records", ACCOUNTS) == [TC.CANDIDATE_PREDICATE], later
    # and further on: the newest question that names the contract still decides
    longer = chain_after(first, relax, narrow, "visualize it", "top 5")
    assert added(Q.terms_for(longer, "by week"), "count_records", ACCOUNTS) == [TC.CANDIDATE_PREDICATE]


def test_a_narrowing_of_sessions_holds_for_t3():
    """F1 for T3: "from sessions", then "only interview support persons", then "top 5": Session__c is refused."""
    session_call = {"object": "Session__c", "group_by": "Host_User__c"}
    chain = chain_after("support person performance from sessions this month", "only interview support persons")
    t = Q.terms_for(chain, "top 5")
    assert not t.sessions and reject(t, "group_records", session_call) == "T3"
    relaxed = chain_after("support person performance from sessions this month", "by month")
    assert Q.terms_for(relaxed, "top 5").sessions                           # not narrowed: the sessions stay


def test_a_narrowing_of_the_status_holds_for_t2():
    """F1 for T2: "with status Placed", then "only placements this month", then "by month": the status is refused."""
    placed = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}
    chain = chain_after("how many candidates with status Placed", "only placements this month")
    assert "placement" in Q.terms_for(chain, "by month").keys
    t = Q.terms_for(chain, "by month")
    assert not t.placed_status and reject(t, "count_records", placed) == "T2"


def test_naming_one_contract_keeps_the_flags_of_another():
    """M1: "only candidates who joined this year" names the candidates contract, not placement: the status flag an
    earlier "status placed" set stays, so the stored status filter is no T2 reject."""
    q1 = "how many candidates have status placed"
    plan = {"question": "q", "terms": chain_after(q1), "age": 0, "show": {"chart": False, "type": None, "table": False},
            "calls": [{"role": "main", "tool": "count_records", "nonempty": True,
                       "args": {"object": "Account", "date_field": "CreatedDate", "period": "this month",
                                "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}}]}
    q2 = "only candidates who joined this year"
    terms = Q.terms_for(plan["terms"], q2)
    assert terms.placed_status and {"candidates", "placement"} <= terms.keys
    got = Q.apply(plan, [{"set_period": {"phrase": "this year"}}], typed=q2, view=VIEW,
                  today=dt.date(2026, 11, 25), terms=terms)
    assert got.clarify is None, got.clarify
    # a follow-up that names the placement contract without the status words does end the status flag
    assert not Q.terms_for(plan["terms"], "only placements").placed_status


def test_every_flag_belongs_to_one_contract():
    owned = [f for flags in Q.CONTRACT_FLAGS.values() for f in flags]
    assert sorted(owned + ["performance"]) == sorted(Q.TERM_FLAGS) and len(owned) == len(set(owned))
    assert set(Q.CONTRACT_FLAGS) == set(Q.CONTRACT_KEYS)


def test_new_question_starts_the_terms_again():
    chain = stored("how many candidates of all account types")
    t = Q.terms_for(chain, "how many interviews this month", "How many interviews took place this month", "new")
    assert t.keys == frozenset() and not t.all_types and t.question == "how many interviews this month"
    assert Q.next_terms(chain, "how many interviews this month", t, "new") == {
        "keys": [], "text": "how many interviews this month"}
    kept = Q.next_terms(chain, "same for last month", Q.terms_for(chain, "same for last month"), "patch")
    assert kept == {"keys": ["candidates"], "text": "how many candidates of all account types\nsame for last month"}


def test_terms_carry_no_contact_flag_and_ignore_unknown_stored_keys():
    chain = {"keys": ["candidates", "contact", "admin"], "text": "email of candidate Person Alpha",
             "contact": True}
    t = Q.terms_for(chain, "visualize it")
    assert t.keys == {"candidates"}
    assert not hasattr(t, "contact") and "contact" not in Q.TERM_FLAGS
    assert "contact" not in json.dumps(Q.next_terms(chain, "visualize it", t))
    assert Q.terms_for("junk", "visualize it").keys == frozenset()


def test_every_seeded_call_is_checked_with_the_rebuilt_terms():
    """A patch that would run a refused call asks (T1); the stored call is never trusted as it ran."""
    plan = {"question": "q", "terms": stored("how many B2B accounts"), "age": 0,
            "show": {"chart": False, "type": None, "table": False},
            "calls": [{"role": "main", "tool": "count_records", "nonempty": True,
                       "args": {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq",
                                                                  "value": "B2B Client"}]}}]}
    terms = Q.terms_for(plan["terms"], "only candidates")
    got = Q.apply(plan, [{"set_show": {"chart": True}}], typed="only candidates", view=VIEW,
                  today=dt.date(2026, 11, 25), terms=terms)
    assert got.clarify is not None and got.clarify.code == "term_reject" and got.clarify.value == "T1"
    fine = Q.apply(plan, [{"set_show": {"chart": True}}], typed="plot it", view=VIEW, today=dt.date(2026, 11, 25),
                   terms=Q.terms_for(plan["terms"], "plot it"))
    assert fine.clarify is None


# ---------------------------------------------------------------------------------- through the real record tools
ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
FIXTURE_TODAY = dt.date(2026, 10, 13)


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    pytest.importorskip("duckdb")
    import terms_fixture as TF
    from sf_assistant import config as C
    from sf_assistant.config import ResolverConfig

    tmp = tmp_path_factory.mktemp("carry")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV + tuple(C.RESOLVER_ENV_VARS.values()):
            mp.delenv(name, raising=False)
        t = TF.open_terms_fixture(tmp, resolver=ResolverConfig(enabled=False))
    yield t
    t.close()


def other_types(tools) -> list[str]:
    """The Account record type names other than Person Account (as ``TermEnforceMixin._terms_for`` reads them)."""
    acc = tools.catalog.objects.get("Account")
    return [str(x) for r in (acc.record_types if acc is not None else []) if r.get("developer_name") != TC.PERSON_TYPE
            for x in (r.get("name"), r.get("developer_name")) if x and x != "(none)"]


def run_job(tools, standalone, terms, name, args):
    """One job as lane S3 runs a seeded call: ``begin_question`` with the standalone question, the rebuilt terms in
    place of today's ``_terms_for(question)``, then ``RecordTools.call``."""
    tools.begin_question(standalone)
    tools._terms = terms   # lane S3: begin_question(standalone, terms=terms)
    out = tools.call(name, args)
    return out, tools.evidence[-1]


def test_real_tools_visualize_it_after_active_or_placed(tools):
    chain = stored(ACTIVE_OR_PLACED)
    terms = Q.terms_for(chain, "visualize it", "Visualize the candidates by status",
                        other_record_types=other_types(tools))
    out, e = run_job(tools, "Visualize the candidates by status", terms, "count_records", STATUS_IN)
    assert out["ok"], out["error"]
    assert e.applied.get("term_filters") == ["RecordTypeId = 'Person Account'"]
    assert e.arguments == STATUS_IN and e.enforced_arguments["filters"][-1]["field"] == "RecordTypeId"
    # the state stores the model's form, so the next run enforces again
    st = Q.build("records", evidence=[e], terms=Q.next_terms(chain, "visualize it", terms), show={"chart": True})
    assert st["plan"]["calls"][0]["args"] == STATUS_IN and "RecordTypeId" not in json.dumps(st)
    # today's behaviour for the same words: no question terms at all, or the keys without the words (T2)
    tools.begin_question("visualize it")
    assert "term_filters" not in (tools.call("count_records", STATUS_IN)["result"].get("applied") or {})
    lost = dataclasses.replace(terms, question="visualize it")
    out, e = run_job(tools, "visualize it", lost, "count_records", STATUS_IN)
    assert not out["ok"] and e.error_code == "T2"


def test_real_tools_support_chain_scopes_the_candidates(tools):
    chain = stored("interviews per support person this month")
    standalone = "How many people did each support person support"
    terms = Q.terms_for(chain, "how many candidates did they support", standalone,
                        other_record_types=other_types(tools))
    out, e = run_job(tools, standalone, terms, "group_records", SUPPORT_DISTINCT)
    assert out["ok"], out["error"]
    assert e.applied.get("term_filters") == ["Candidate__c.RecordTypeId = 'Person Account'"]
    out, e = run_job(tools, standalone, TC.detect(standalone), "group_records", SUPPORT_DISTINCT)
    assert out["ok"] and "term_filters" not in e.applied


def test_real_tools_follow_set_keeps_the_candidate_scope(tools):
    """C22: "their interviews" after a candidates count carries the contract's Person Account scope as a path filter,
    and the new call runs through the record tools."""
    view = Q.PlanView(tools.catalog)
    args = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}
    chain = stored("how many candidates have status placed")
    terms = Q.terms_for(chain, "how many candidates have status placed", "x", "new",
                        other_record_types=other_types(tools))
    out, e = run_job(tools, "how many candidates have status placed", terms, "count_records", args)
    assert out["ok"], out["error"]
    st = Q.build("records", evidence=[e], terms=Q.next_terms(chain, "how many candidates have status placed", terms,
                                                             "new"))
    checked = Q.check(json.loads(json.dumps(st)), view=view)
    assert checked.reason == "" and checked.missing == ()
    follow = Q.terms_for(checked.plan["terms"], "their interviews", other_record_types=other_types(tools))
    assert follow.placed_status   # carried: "their interviews" names no term
    lost = Q.terms_for({"keys": checked.plan["terms"]["keys"], "text": ""}, "their interviews")
    refused = Q.apply(checked.plan, [{"follow_set": {"object": "Interview__c", "via_field": "Candidate__c"}}],
                      typed="their interviews", view=view, today=FIXTURE_TODAY, terms=lost)
    assert refused.clarify is not None and refused.clarify.value == "T2"   # the words lost: the contract refuses
    which = Q.apply(checked.plan, [{"follow_set": {"object": "Interview__c"}}], typed="their interviews", view=view,
                    today=FIXTURE_TODAY, terms=follow)
    assert which.clarify is not None and which.clarify.code == "which_link"
    assert "Candidate__c" in [c["label"] for c in which.clarify.choices]
    got = Q.apply(checked.plan, [{"follow_set": {"object": "Interview__c", "via_field": "Candidate__c"}}],
                  typed="their interviews", view=view, today=FIXTURE_TODAY, terms=follow)
    assert got.clarify is None, got.clarify
    filters = got.plan["calls"][0]["args"]["filters"]
    assert {"field": "Candidate__c.RecordTypeId", "op": "eq", "value": TC.PERSON_TYPE} in filters
    run = Q.run_calls(got, FIXTURE_TODAY)[0]
    out, e = run_job(tools, "their interviews", follow, run.tool, run.args)
    assert out["ok"], out["error"]
    assert "term_filters" not in e.applied   # already scoped: code adds nothing twice


def test_real_tools_ranked_set_by_month_round_trip(tools):
    """A top-2 ranking, then "by month": the fresh keys become an ``in`` filter at run time, the state stores the set
    call and no key, and the next state is built from the seeded run."""
    view = Q.PlanView(tools.catalog)
    group = {"object": "Interview__c", "group_by": "Interview_Outcome__c", "date_field": "interview_date",
             "period": "this year", "top": 2}
    t0 = Q.terms_for(None, "top 2 outcomes this year", "x", "new")
    out, e = run_job(tools, "top 2 outcomes this year", t0, "group_records", group)
    assert out["ok"], out["error"]
    st = Q.build("records", evidence=[e], terms=Q.next_terms(None, "top 2 outcomes this year", t0, "new"))
    call = st["plan"]["calls"][0]
    assert call["args"]["date_field"] == "Date_of_Interview__c" and call["period"]["from"] == "2026-01-01"
    shown = Q.set_keys(e)
    assert len(shown) == 2 and call["set_digest"] == Q.keys_digest(shown)
    checked = Q.check(json.loads(json.dumps(st)), view=view)
    assert checked.missing == ()
    patched = Q.apply(checked.plan, [{"set_grain": {"grain": "month"}}], typed="by month", view=view,
                      today=FIXTURE_TODAY, terms=Q.terms_for(checked.plan["terms"], "by month"))
    assert patched.clarify is None, patched.clarify
    run = Q.run_calls(patched, FIXTURE_TODAY)[0]
    t1 = Q.terms_for(checked.plan["terms"], "by month")
    set_out, set_ev = run_job(tools, "by month", t1, run.keys_from["tool"], run.set_args)
    assert set_out["ok"]
    item, status = Q.keys_filter(run.keys_from, Q.set_keys(set_ev))
    assert status == "ok" and item["values"] == shown
    out = tools.call(run.tool, Q.with_item(run.tool, run.args, item))
    assert out["ok"], out["error"]
    nxt = Q.build("records", evidence=tools.evidence, seeds=[run], terms=Q.next_terms(checked.plan["terms"],
                                                                                         "by month", t1))
    calls = nxt["plan"]["calls"]
    assert [c["tool"] for c in calls] == ["trend_records"] and calls[0]["keys_from"]["args"] == call["args"]
    assert not any(k in json.dumps(nxt) for k in shown)
    assert Q.plan_line(nxt, label=view.label).startswith(
        "number of Interviews by month, split by Interview Outcome, this year (2026-01-01 to 2026-10-31), "
        "for the top 2")
    assert calls[0]["period"]["to"] == "2026-10-31"   # the trend ends with the bucket of today
    same_day = Q.run_calls(nxt, FIXTURE_TODAY)[0]
    assert same_day.args["period"] == "this year" and not same_day.kept_dates
    next_month = Q.run_calls(nxt, dt.date(2026, 11, 3))[0]
    assert next_month.args["period"] == "2026-01-01 to 2026-10-31" and next_month.kept_dates
    assert not run.kept_dates   # made on the same day: the clipped dates are the trend's own


def test_real_tools_a_narrowing_holds_on_the_next_shortcut(tools):
    """H1 on the real record tools: after relax and narrow, "visualize it" runs the same stored call with the Person
    Account filter (before the fix it ran with none and counted every account type)."""
    first, relax, narrow = ("how many candidates joined this month", "same for last month, all account types",
                            "only candidates, by month")
    names = other_types(tools)
    args = {"object": "Account"}
    t_relaxed = Q.terms_for(chain_after(first), relax, other_record_types=names)
    out, e = run_job(tools, relax, t_relaxed, "count_records", args)
    assert out["ok"] and "term_filters" not in e.applied
    t_narrow = Q.terms_for(chain_after(first, relax), narrow, other_record_types=names)
    out, e = run_job(tools, narrow, t_narrow, "count_records", args)
    assert out["ok"] and e.applied.get("term_filters") == ["RecordTypeId = 'Person Account'"]
    narrowed_count = out["result"]["count"]
    t_next = Q.terms_for(chain_after(first, relax, narrow), "visualize it", other_record_types=names)
    out, e = run_job(tools, "visualize it", t_next, "count_records", args)
    assert out["ok"] and e.applied.get("term_filters") == ["RecordTypeId = 'Person Account'"]
    assert out["result"]["count"] == narrowed_count


def test_plan_view_over_the_catalog(tools):
    view = Q.PlanView(tools.catalog)
    assert view.date_path("Interview__c", "interview_date") == "Date_of_Interview__c"
    assert view.date_path("Interview__c", "created") == "CreatedDate"
    assert view.date_path("Interview__c", "Recruiter__c") is None
    assert view.field_kind("Interview__c", "Client_Bill_Rate__c") == "currency"
    assert view.field_kind("Interview__c", "interview_date") == "date"
    assert view.is_contact("Recruiter__c", "Email__c") and not view.is_contact("Interview__c", "Recruiter__c")
    assert "Candidate__c" in view.lookups("Interview__c", "Account")
    assert view.target("Interview__c", "Interview_Support_Person__c") == "Recruiter__c"
    assert view.label("Interview__c") == "Interviews" and view.label("Nothing__c") is None
    assert view.path("Interview__c", "Candidate__c.RecordTypeId") is not None
    # F12: the blocked fields of the fixture; F2: the picklist values the value screen reads
    assert view.is_blocked("Interview__c", "Zoom_Passcode__c") and not view.is_blocked("Interview__c", "Recruiter__c")
    assert "Placed" in view.picklist_values("Account", "Candidate_Status__c")
    assert Q.schema_value(view, "Account", "Candidate_Status__c", "placed")
    assert not Q.schema_value(view, "Account", "Name", "placed")
    gone = Q.check(Q.build("records", evidence=[], parent=None))
    assert gone.state is not None and gone.plan is None
