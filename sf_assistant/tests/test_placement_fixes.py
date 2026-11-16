"""The placement fixes of 2026-11-16 (four page questions about placements and their rate went wrong): the owner's
definition of placement (``engine/business_terms.py``, ``prompts/business_terms.md``, the record hints and the knowledge
files), a rate per period in one trend_records call (``part_filters``), a chart of that rate, the page's follow-up
memory (``Pipeline.answer_followup``, ``POST /api/ask`` ``recent_turns``) and the honest failure of an empty record
step (``pipeline.choose_answer``, ``RecordSession.check_draft``). Fake warehouse and invented names only; the real
knowledge files are read only when they are there (they are git-ignored)."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "web"))

from sf_assistant import pipeline as P  # noqa: E402
from sf_assistant.agent import charts  # noqa: E402
from sf_assistant.agent import followup  # noqa: E402
from sf_assistant.agent.record_prompts import RECORDS_AGENT_SYSTEM, RECORDS_UNSUPPORTED_MESSAGE  # noqa: E402
from sf_assistant.engine import business_terms as BT  # noqa: E402
from warehouse_fixture import open_fixture  # noqa: E402

import pipeline as web_pipeline  # noqa: E402   (web/pipeline.py)

KNOWLEDGE = ROOT / "knowledge" / "objects"
F = lambda f, v, op="eq": {"field": f, "op": op, "value": v}  # noqa: E731
OFFER = [F("Interview_Outcome__c", "Offer Received")]
RATE_ARGS = {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c", "period": "this year",
             "part_filters": OFFER}
W = charts._WORDS


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    t = open_fixture(tmp_path_factory.mktemp("placement"))
    yield t
    t.close()


def ok(tools, name, args):
    out = tools.call(name, args)
    assert out["ok"], out["error"]
    return out["result"]


# ------------------------------------------------------------------------------------------------ A: the definition
@pytest.mark.parametrize("question", [
    "show me the placement rate Monthwise from January 2026 to October 2026.",
    "can you plot the number of candidates placed from january to december 2026",
    "how many placements this year?", "Placement rate last quarter", "candidates placed in October",
    # review of 2026-11-16: common misspellings, and "placed" with a person or in a hiring phrase
    "how many candidates were placd this year", "plot the placment rate monthwise", "placments per month",
    "plcement rate", "how many placed last month", "who got placed in march", "list placed candidates"])
def test_question_words_name_the_placement_term(question):
    assert BT.terms_in(question) == ["placement"]
    lines = BT.term_lines(question)
    assert lines[0] == BT.TERMS_HEADER and lines[1] == "- " + BT.DEFINITIONS["placement"]
    assert BT.not_the_term(question) == {("Account", "Candidate_Status__c", "Placed")}   # the value only


@pytest.mark.parametrize("question", ["how many candidates were placed on hold?", "who signed the placement services "
                                      "agreement?", "how many interviews ended with an offer?", "",
                                      # review of 2026-11-16: other meanings of the word
                                      "how many sections are placed in each template",
                                      "how many calls were placed by recruiters last week", "placed an order",
                                      "marketing placement of ads", "pre-placement checks", "replaced"])
def test_other_words_name_no_term(question):
    assert BT.terms_in(question) == [] and BT.term_lines(question) == [] and BT.not_the_term(question) == frozenset()


def test_the_definition_is_the_owners():
    text = BT.DEFINITIONS["placement"]
    for part in ("client interview (Interview__c) whose Interview_Outcome__c is 'Offer Received'",
                 "all client interviews (Interview__c with no outcome filter) in the same period",
                 "trend_records with the same part_filters", "date_field Date_of_Interview__c, grain month",
                 "Never count Account.Candidate_Status__c = 'Placed', Onboarding__c completion",
                 # review of 2026-11-16: a question that counts candidates counts different ones
                 "use measure count_distinct with measure_field Candidate__c on the same filter"):
        assert part in text, part
    assert BT.is_not_the_term(BT.NOT_THE_TERM["placement"], "Account", "Candidate_Status__c", "placed")
    assert not BT.is_not_the_term(BT.NOT_THE_TERM["placement"], "Account", "Candidate_Status__c", "Active")


def test_hints_give_the_definition_and_drop_the_other_reading(tools, monkeypatch):
    rows = [("Account", "Candidate_Status__c", "Placed", 3), ("Interview__c", "Interview_Outcome__c", "Offer Received", 2)]
    monkeypatch.setattr(tools.catalog, "value_matches", lambda q, top_objects=None, n=5: list(rows))
    monkeypatch.setattr(tools, "_question_values", lambda q: [])
    hints = tools.object_hints("how many candidates were placed per month")
    assert BT.TERMS_HEADER in hints and BT.DEFINITIONS["placement"] in hints
    assert "'Placed' in Account.Candidate_Status__c" not in hints
    assert "'Offer Received' in Interview__c.Interview_Outcome__c" in hints
    other = tools.object_hints("how many candidates are on hold")   # no term: the hints as before
    assert BT.TERMS_HEADER not in other and "'Placed' in Account.Candidate_Status__c" in other


def test_records_named_in_the_question_drop_the_other_reading(tools, monkeypatch):
    spot = {"listed": True, "object": "Account", "field": "Candidate_Status__c", "kind": "text", "span": "placed",
            "start": 25, "end": 31, "values": ["Placed"], "value": "Placed", "records": 3, "strength": "hint"}
    monkeypatch.setattr(tools, "_question_values", lambda q: [dict(spot)])
    assert tools.value_hint_lines("how many candidates were placed") == []
    assert any("Account.Candidate_Status__c" in ln for ln in tools.value_hint_lines("how many candidates on hold"))
    active = {**spot, "span": "active", "start": 20, "end": 26, "values": ["Active"], "value": "Active"}
    monkeypatch.setattr(tools, "_question_values", lambda q: [dict(active)])   # another value of the same field stays
    assert any("Account.Candidate_Status__c" in ln
               for ln in tools.value_hint_lines("how many candidates with an active status were placed"))


def test_the_term_drops_only_its_own_value_not_the_field(tools, monkeypatch):
    """Review of 2026-11-16: the other values of Account.Candidate_Status__c and Onboarding__c.Status__c stay when
    the question names a placement and filters on them."""
    rows = [("Account", "Candidate_Status__c", "Placed", 3), ("Account", "Candidate_Status__c", "Active", 5),
            ("Onboarding__c", "Status__c", "Onboarding Completed", 2)]
    monkeypatch.setattr(tools.catalog, "value_matches", lambda q, top_objects=None, n=5: list(rows))
    monkeypatch.setattr(tools, "_question_values", lambda q: [])
    hints = tools.object_hints("how many candidates with an active candidate status were placed this year")
    assert BT.TERMS_HEADER in hints and "'Placed' in Account.Candidate_Status__c" not in hints
    assert "'Active' in Account.Candidate_Status__c" in hints
    hints = tools.object_hints("how many placed candidates have onboarding status completed")
    assert "'Onboarding Completed' in Onboarding__c.Status__c" in hints


def test_a_misspelt_placement_gets_the_definition_and_no_placed_suggestion(tools, monkeypatch):
    """Review of 2026-11-16: with the spelling help on, 'placd' was offered as 'Placed' (Account.Candidate_Status__c
    only), and 'placment' named no term. Invented spellings and targets."""
    from sf_assistant.tools import knowledge_tools as KT
    from sf_assistant.tools import record_tools as RT

    targets = {"Placed": (("value", "Account.Candidate_Status__c=Placed"),),
               "Plaid Mode": (("field", "Interview__c.Plaid_Mode__c"),)}
    monkeypatch.setattr(RT, "_metadata_targets", lambda resolver, text, stored: targets.get(text, ()))
    monkeypatch.setattr(tools.catalog, "resolver", SimpleNamespace())
    monkeypatch.setattr(tools.catalog, "value_matches", lambda q, top_objects=None, n=5: [])
    monkeypatch.setattr(tools, "_question_values", lambda q: [])
    spans: list = []
    monkeypatch.setattr(tools, "_spelling_spans", lambda q: list(spans))

    def span(q, word, options=(), used=None):
        at = q.index(word)
        decision = "corrected" if used else "ambiguous"
        return KT.SpellingSpan(word, at, at + len(word), decision, used, tuple(options),
                               targets.get(used, ()) if used else ())

    q = "how many candidates were placd this year"
    spans[:] = [span(q, "placd", ("Placed",))]
    hints = tools.object_hints(q)
    assert BT.TERMS_HEADER in hints and BT.DEFINITIONS["placement"] in hints
    assert "'placd'" not in hints and "Placed" not in hints.replace(BT.DEFINITIONS["placement"], "")
    spans[:] = [span(q, "placd", ("Placed", "Plaid Mode"))]   # another option stays
    assert "- 'placd' may mean: Plaid Mode (say which one you used, or ask)" in tools.object_hints(q)
    spans[:] = [span(q, "placd", used="Placed")]                # a correction to that value alone is not listed
    assert "'placd' ->" not in tools.object_hints(q)
    q = "plot the placment rate monthwise"
    spans[:] = []
    assert BT.TERMS_HEADER in tools.object_hints(q)
    q = "is the status placd or active"   # no placement named: the spelling help as before
    spans[:] = [span(q, "placd", ("Placed",))]
    assert "- 'placd' may mean: Placed (say which one you used, or ask)" in tools.object_hints(q)


def test_the_record_agent_reads_business_terms_and_rates_per_period():
    assert "Business terms. When the hints list a business term of the question" in RECORDS_AGENT_SYSTEM
    assert "a rate / share per day / week / month      -> trend_records with part_filters" in RECORDS_AGENT_SYSTEM
    assert ('- Placement rate per month this year (business term) -> trend_records {"object":"Interview__c",'
            '"grain":"month","date_field":"Date_of_Interview__c","period":"this year","part_filters":[{"field":'
            '"Interview_Outcome__c","op":"eq","value":"Offer Received"}]}') in RECORDS_AGENT_SYSTEM
    assert ('- Placements per month this year (business term) -> trend_records {"object":"Interview__c",'
            '"grain":"month"') in RECORDS_AGENT_SYSTEM
    assert "trend_records with part_filters for a share per period" in RECORDS_UNSUPPORTED_MESSAGE
    assert "trend_records with part_filters" in charts.RECORDS_CHART_NOTE


@pytest.mark.skipif(not (KNOWLEDGE / "Account.json").is_file(), reason="the knowledge files are git-ignored")
def test_the_knowledge_files_say_the_same():
    def load(name):
        return json.loads((KNOWLEDGE / name).read_text(encoding="utf-8"))

    account, interview, onboarding = load("Account.json"), load("Interview__c.json"), load("Onboarding__c.json")
    text = json.dumps(account, ensure_ascii=False)
    assert "not settled" not in text and "two readings" not in text
    status = next(f for f in account["fields"] if f["api_name"] == "Candidate_Status__c")
    assert "a placement count never reads this status" in json.dumps(status, ensure_ascii=False)
    outcome = next(f for f in interview["fields"] if f["api_name"] == "Interview_Outcome__c")
    assert "Offer Received is a placement" in json.dumps(outcome, ensure_ascii=False)
    assert "a placement (placed, placement rate)" in json.dumps(outcome, ensure_ascii=False)
    semantics = json.dumps(interview["object"], ensure_ascii=False)
    assert "placement rate" in semantics and "placement (offer received)" in semantics
    assert "not a placement count" in json.dumps(onboarding["object"], ensure_ascii=False)


# ------------------------------------------------------------------------------------------------ B: a rate per period
def test_trend_rate_per_month_in_one_call(tools):
    n = len(tools.evidence)
    r = ok(tools, "trend_records", RATE_ARGS)
    assert len(tools.evidence) == n + 1   # one tool call
    assert r["measure"] == "percent" and r["part_is"] == "Interview_Outcome__c = 'Offer Received'"
    assert r["base_is"] == "all Interview__c records"
    by = {b["bucket"]: (b["part"], b["base"], b["percent"]) for b in r["buckets"]}
    assert list(by) == [f"2026-{m:02d}" for m in range(1, 11)]   # every month of the window, today is 2026-10-13
    assert by["2026-08"] == (1, 1, 100.0) and by["2026-09"] == (1, 3, 33.3) and by["2026-10"] == (0, 3, 0.0)
    assert by["2026-01"] == (0, 0, None) and by["2026-06"] == (0, 0, None)   # no interview: no rate, never 0%
    assert all(b["value"] == b["percent"] for b in r["buckets"])
    assert (r["part_total"], r["base_total"], r["percent_overall"]) == (2, 9, 22.2)
    assert sum(b["part"] for b in r["buckets"]) == r["part_total"] and "total" not in r
    assert r["applied"]["part_filters"] == ["Interview_Outcome__c = 'Offer Received'"]
    assert any(n_.startswith("no base records in 2026-01, 2026-02") and n_.endswith("no rate there (not 0%)")
               for n_ in r["notes"])
    statement = tools.evidence[-1].statements[0].sql   # the main statement
    assert "FILTER (WHERE" in statement and "GROUP BY 1" in statement   # part and base per bucket in one statement


def test_trend_rate_refuses_what_it_cannot_mean(tools):
    for extra in ({"split_by": "Interview_Status__c"}, {"measure": "sum", "measure_field": "Client_Bill_Rate__c"}):
        e = tools.call("trend_records", {**RATE_ARGS, **extra})["error"]
        assert e.startswith("part_filters gives a rate per bucket")
    e = tools.call("trend_records", {**RATE_ARGS, "filters": OFFER})["error"]
    assert e.startswith("part_filters repeat filters")
    plain = ok(tools, "trend_records", {k: v for k, v in RATE_ARGS.items() if k != "part_filters"})
    assert "measure" not in plain and all("percent" not in b for b in plain["buckets"])   # without it: as before


def test_a_rate_trend_is_drawn_with_its_own_numbers(tools):
    r = ok(tools, "trend_records", RATE_ARGS)
    item = charts.for_tool("trend_records", r, RATE_ARGS, tools.catalog, "2026-10-13")
    assert item["chart"] == {"type": "line", "x_key": "Month", "y_keys": [W["percent"]],
                             "title": "Interviews: Percent per month", "stacked": False}
    assert [row[W["percent"]] for row in item["data"]] == [b["percent"] for b in r["buckets"]]
    assert [(row[W["rate_part"]], row[W["rate_base"]]) for row in item["data"]] == \
        [(b["part"], b["base"]) for b in r["buckets"]]
    assert any(s.startswith("Percent of the records in each period that match") for s in item["subtitle"])
    assert "Whole period: 22.2% (2 of 9 records)" in item["subtitle"]
    assert any(n.startswith("No records in 2026-01") for n in item["notes"])
    assert charts._check_svg(item["svg"])
    bar = charts.for_tool("trend_records", r, RATE_ARGS, tools.catalog, "2026-10-13")
    assert bar["svg"] == item["svg"]   # drawn once, the same every time


def _rec(evidence, **kw):
    base = dict(evidence=evidence, table_blocks={}, no_records=False, error=None, routed_out=False, code_answer=False)
    return NS(**{**base, **kw})


def test_a_rate_question_never_gets_counts_drawn_in_its_place(tools):
    ok(tools, "trend_records", {k: v for k, v in RATE_ARGS.items() if k != "part_filters"})
    counts = [tools.evidence[-1]]
    final, items, lines, _err = charts.make(charts.chart_ask("plot the placement rate per month"),
                                            "plot the placement rate per month", "Answer.", "records", "lean",
                                            _rec(counts), tools.catalog, "2026-10-13")
    assert lines == [W["rate_counts"]] and all(not i["chart"] for i in items)
    _f, items, lines, _err = charts.make(charts.chart_ask("plot the number of placements per month"),
                                         "plot the number of placements per month", "Answer.", "records", "lean",
                                         _rec(counts), tools.catalog, "2026-10-13")
    assert lines == [] and items[0]["chart"]["type"] == "line"   # a count question draws the counts
    ok(tools, "trend_records", RATE_ARGS)
    _f, items, lines, _err = charts.make(charts.chart_ask("plot the placement rate per month"),
                                         "plot the placement rate per month", "Answer.", "records", "lean",
                                         _rec(counts + [tools.evidence[-1]]), tools.catalog, "2026-10-13")
    assert lines == [] and items[0]["chart"]["y_keys"] == [W["percent"]]


@pytest.mark.parametrize("question, tool, args, drawn", [
    ("plot the average candidate rate per month this year", "trend_records",
     {k: v for k, v in RATE_ARGS.items() if k != "part_filters"} | {"measure": "avg",
                                                                    "measure_field": "Client_Bill_Rate__c"}, "line"),
    ("pie chart of the share of interviews by outcome", "group_records",
     {"object": "Interview__c", "group_by": "Interview_Outcome__c"}, "pie"),
    ("plot the average bill-rate per month this year", "trend_records",
     {k: v for k, v in RATE_ARGS.items() if k != "part_filters"}, "line")])
def test_a_rate_word_keeps_charts_of_shares_and_rate_fields(tools, question, tool, args, drawn):
    """Review of 2026-11-16: only counts over time are refused for a rate question."""
    ok(tools, tool, args)
    _f, items, lines, _err = charts.make(charts.chart_ask(question), question, "Answer.", "records", "lean",
                                         _rec([tools.evidence[-1]]), tools.catalog, "2026-10-13")
    assert lines == [] and items[0]["chart"]["type"] == drawn


def test_the_rate_words():
    assert charts._RATE_ASK.search("plot the placement rate in %") and charts._RATE_ASK.search("share by outcome")
    for q in ("plot interviews per month where 100% done", "average bill-rate per month", "the bill rate"):
        assert not charts._RATE_ASK.search(q), q


# ------------------------------------------------------------------------------------------------ E: honest failure
APPLICATION_NOTE = "- `Application__c` has no placement field; `Date_of_Application__c` is the application date."
INTERVIEW_NOTE = "- `Interview__c.Interview_Outcome__c` 'Offer Received' is a placement."


def _failed(**kw):
    base = dict(error="empty record answer", triage={"route": True}, no_records=True, answer="", routed_out=False,
                named_people=[], evidence=[NS(object="Interview__c", tool="trend_records", ok=True, kind="data")])
    return NS(**{**base, **kw})


def test_a_failed_record_step_says_so_and_never_shows_another_subject():
    assert P.choose_answer(None, _failed(), "", APPLICATION_NOTE) == (P.RECORDS_FAILED_ANSWER, "none")
    final, kind = P.choose_answer(None, _failed(), "", INTERVIEW_NOTE)   # the same object: a labelled note may follow
    assert kind == "none" and final == f"{P.RECORDS_FAILED_ANSWER}\n\n{P.RECORDS_FAILED_NOTE_LABEL}\n{INTERVIEW_NOTE}"
    assert P.choose_answer(None, _failed(), "", "") == (P.RECORDS_FAILED_ANSWER, "none")


def test_what_keeps_the_knowledge_answer():
    k = APPLICATION_NOTE
    assert P.choose_answer(None, _failed(error=None), "", k) == (k, "knowledge")        # no failure
    assert P.choose_answer(None, _failed(triage={"route": False, "overruled_by": "value"}), "", k) == (k, "knowledge")
    assert P.choose_answer(None, _failed(routed_out=True), "", k) == (k, "knowledge")
    assert P.choose_answer(None, _failed(named_people=["Echo One"]), "", k)[1] == "knowledge"   # the people path
    assert P.choose_answer(None, _failed(), "", k, records_only=True) == ("", "none")
    assert P.choose_answer(None, _failed(triage={"route": False, "overruled_by": "chart"}), "", k)[1] == "none"


def _ev(obj, tool="trend_records", ok=True, kind="data", **kw):
    return NS(object=obj, tool=tool, ok=ok, kind=kind, **kw)


def test_a_failed_route_call_still_says_the_figures_failed():
    """Review of 2026-11-16: route None (the route call failed or was unclear; the record agent ran all the same)."""
    assert P.choose_answer(None, _failed(triage={"route": None}), "", APPLICATION_NOTE) == \
        (P.RECORDS_FAILED_ANSWER, "none")
    # no data call read records (a design question whose route call failed): the knowledge answer as before
    for evidence in ([], [_ev("Account", "about_people", ok=False)],
                     [_ev("Interview__c", "describe_fields", kind="orientation")]):
        assert P.choose_answer(None, _failed(triage={"route": None}, evidence=evidence), "", APPLICATION_NOTE) == \
            (APPLICATION_NOTE, "knowledge")
    assert P.choose_answer(None, _failed(triage={}), "", APPLICATION_NOTE) == (APPLICATION_NOTE, "knowledge")
    k = APPLICATION_NOTE   # the missing-name overrule (route False) keeps the knowledge answer
    assert P.choose_answer(None, _failed(triage={"route": False, "overruled_by": "missing"}), "", k)[1] == "knowledge"


ONBOARDING_NOTE = "- `Onboarding__c` completion is tracked in `Onboarding__c.Status__c`."


@pytest.mark.parametrize("evidence, note", [
    # ticket 3's call shape: describe_fields on Onboarding__c before the Interview__c data calls
    ([_ev("Onboarding__c", "describe_fields", kind="orientation"), _ev("Interview__c")], ONBOARDING_NOTE),
    ([_ev("Onboarding__c", "percentage_of_records", ok=False), _ev("Interview__c")], ONBOARDING_NOTE),  # failed call
    ([_ev("Account", "count_records")],                                                            # a standard word
     "- `Application__c` links to the candidate's Account; `Date_of_Application__c` is the date."),
    ([_ev("Interview__c", "get_sync_status")], INTERVIEW_NOTE),                                         # no data call
    ([], INTERVIEW_NOTE)])
def test_only_the_objects_of_good_data_calls_keep_a_note(evidence, note):
    assert P.choose_answer(None, _failed(evidence=evidence), "", note) == (P.RECORDS_FAILED_ANSWER, "none")


def test_a_note_naming_a_field_the_record_step_read_is_kept_without_its_source_line():
    ev = [_ev("Interview__c", tables=["Interview__c"], fields_used=[("Interview__c", "Interview_Outcome__c")])]
    note = "A placement is `Interview_Outcome__c` = 'Offer Received'; the rate is those over all client interviews."
    final, kind = P.choose_answer(None, _failed(evidence=ev), "", note + "\n\nSource: object knowledge files.")
    assert kind == "none" and final == f"{P.RECORDS_FAILED_ANSWER}\n\n{P.RECORDS_FAILED_NOTE_LABEL}\n{note}"
    other = "`Onboarding__c.Interview_Outcome__c` is another object's field."   # the same name on another object
    assert P.choose_answer(None, _failed(evidence=ev), "", other) == (P.RECORDS_FAILED_ANSWER, "none")


def test_a_failed_answer_after_good_calls_gets_its_chart_line():
    rec = _failed()
    assert charts._answer_word("none", rec) == "answer_failed"
    assert charts._answer_word("none", _failed(error=None)) == "no_data"
    assert charts._answer_word("knowledge", rec) == "no_data"
    assert W["answer_failed"] == "No chart: the figures could not be computed this time."


# ------------------------------------------------------------------------------------------------ D: page follow-ups
class _Page:
    """``web/pipeline.Pipeline.answer_followup`` on a stand-in: ``answer`` records what it was asked."""

    answer_followup = web_pipeline.Pipeline.answer_followup

    def __init__(self):
        self.llm = NS(cfg=NS(base_url="http://127.0.0.1:8000/v1"))
        self.asked: list[tuple[str, str]] = []

    def answer(self, question, mode="merged"):
        self.asked.append((question, mode))
        return {"answer": "12 placements.", "kind": "merged", "source_line": "Source: fake.", "data_ts": None,
                "trace": {}, "log": {}}


TURNS = [{"role": "user", "content": "show me the placement rate monthwise this year"},
         {"role": "assistant", "content": "The placement rate per month is listed below."}]


def test_without_turns_the_page_asks_the_typed_question_unchanged(monkeypatch):
    monkeypatch.setattr(followup, "rewrite", lambda *a: pytest.fail("no rewrite without turns"))
    page = _Page()
    out = page.answer_followup("how many placements?", [], "merged")
    assert page.asked == [("how many placements?", "merged")] and "rewrite" not in out["log"]
    assert out["answer"] == "12 placements."


def test_a_follow_up_is_read_as_a_whole_question(monkeypatch):
    seen = []

    def rewrite(question, turns, llm):
        seen.append((question, turns))
        return followup.Rewrite(followup.OK, "plot the placement rate monthwise this year")

    monkeypatch.setattr(followup, "rewrite", rewrite)
    page = _Page()
    out = page.answer_followup("can you plot this placement rate", TURNS, "merged")
    assert seen == [("can you plot this placement rate", TURNS)]
    assert page.asked == [("plot the placement rate monthwise this year", "merged")]
    assert out["answer"] == "Read as: `plot the placement rate monthwise this year`\n\n12 placements."
    info = dict(out["log"]["rewrite"])
    assert isinstance(info.pop("seconds"), float)   # the rewrite call's time (review of 2026-11-16)
    assert info == {"status": "ok", "reason": "", "question_used": "plot the placement rate monthwise this year"}


@pytest.mark.parametrize("step, line", [
    (followup.Rewrite(followup.UNCLEAR, reason="unclear", reply="UNCLEAR"), web_pipeline.UNCLEAR_LINE),
    (followup.Rewrite(followup.FAILED, reason="call_failed", error="RuntimeError"), web_pipeline.REWRITE_FAILED_LINE)])
def test_an_unclear_or_failed_rewrite_answers_nothing_else(monkeypatch, step, line):
    monkeypatch.setattr(followup, "rewrite", lambda *a: step)
    page = _Page()
    out = page.answer_followup("and this?", TURNS, "merged")
    assert page.asked == [] and out["answer"] == line and out["kind"] == "none"
    assert out["log"]["rewrite"]["status"] == step.status and isinstance(out["log"]["rewrite"]["seconds"], float)


COMPLETE = ["show me the placement rate Monthwise from January 2026 to October 2026.",
            "How many client interviews were held in October 2026?"]


@pytest.mark.parametrize("question", COMPLETE)
@pytest.mark.parametrize("step", [
    followup.Rewrite(followup.FAILED, reason="call_failed", error="TimeoutError"),
    followup.Rewrite(followup.UNCLEAR, reason="standalone_changed", reply="How many interviews were held?")])
def test_a_complete_question_is_answered_as_typed_when_the_rewrite_fails(monkeypatch, question, step):
    """Review of 2026-11-16: a busy model or a reworded reply refused a complete question on the page."""
    monkeypatch.setattr(followup, "rewrite", lambda *a: step)
    page = _Page()
    out = page.answer_followup(question, TURNS, "merged")
    assert page.asked == [(question, "merged")] and out["answer"] == "12 placements."   # no Read-as line
    assert out["log"]["rewrite"]["answered"] == "typed" and out["log"]["rewrite"]["status"] == step.status


def test_the_models_unclear_and_a_failed_fragment_stay_refused(monkeypatch):
    page = _Page()
    monkeypatch.setattr(followup, "rewrite",
                        lambda *a: followup.Rewrite(followup.UNCLEAR, reason="unclear", reply="UNCLEAR"))
    assert page.answer_followup(COMPLETE[1], TURNS, "merged")["answer"] == web_pipeline.UNCLEAR_LINE
    monkeypatch.setattr(followup, "rewrite",
                        lambda *a: followup.Rewrite(followup.FAILED, reason="call_failed", error="TimeoutError"))
    for fragment in ("plot this", "and for October?"):
        assert page.answer_followup(fragment, TURNS, "merged")["answer"] == web_pipeline.REWRITE_FAILED_LINE
    assert page.asked == []


def test_the_timeline_counts_the_rewrite_call():
    from sf_assistant.evaluation import timeline_report as TR

    detail = {"rewrite": {"status": "ok", "reason": "", "seconds": 1.5}}
    a = TR._ask("#1", {"ts": "2026-11-16T13:42:00+00:00"}, {"seconds": 0.5}, "ok", 2.0, 0.0, detail)
    assert a.rewrite_s == 1.5
    assert TR._header(a)[1].endswith("page 2.00 s = rewrite 1.50 + answer 0.50 + hand-off 0.00")


def test_the_page_and_the_service_share_the_read_as_line():
    ask_mcp = pytest.importorskip("ask_mcp")
    assert ask_mcp._read_as_line is web_pipeline.read_as_line and ask_mcp.UNCLEAR_LINE == web_pipeline.UNCLEAR_LINE
    assert ask_mcp._bad_arguments({"question": "q", "recent_turns": [{"role": "user"}]}) == \
        web_pipeline.turns_problem([{"role": "user"}])


def test_the_page_sends_its_last_answered_turns():
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function recentTurns(question)" in js and "if (turns.length) { body.recent_turns = turns; }" in js
    assert "var turns = recentTurns(question);   // before this question joins the history" in js


class _ServerPipeline:
    def __init__(self):
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def _out(self, question):
        return {"answer": f"Fake answer to: {question}", "kind": "merged", "source_line": "Source: fake.",
                "data_ts": None, "trace": {}, "log": {}}

    def answer(self, question, mode="merged"):
        with self._lock:
            self.calls.append(("answer", question, mode))
        return self._out(question)

    def answer_followup(self, question, turns, mode="merged"):
        with self._lock:
            self.calls.append(("followup", question, turns, mode))
        return self._out(question)

    def status(self):
        return {"records_ok": True, "data_synced": None, "status_line": "records: fake", "records_error": None,
                "origin": "fake copy", "age_hours": 1.0, "model": "fake-model"}

    def close(self):
        pass


def test_the_ask_endpoint_passes_the_turns(tmp_path):
    import test_web as TW

    d = tmp_path / ".private"
    d.mkdir(mode=0o700)
    friend, _admin = TW.codes.write_codes(str(d / "codes.json"))
    fake = _ServerPipeline()
    app = TW.make_app(types.SimpleNamespace(dir=d), fake)

    async def go():
        async with TW.client(app) as c:
            await TW.login(c, friend)
            r = await TW.ask(c, {"question": "how many placements?"})
            assert r.status_code == 200
            r = await TW.ask(c, {"question": "plot this", "recent_turns": TURNS})
            assert r.status_code == 200
            bad = await c.post("/api/ask", json={"question": "plot this", "recent_turns": [{"role": "user"}]})
            assert bad.status_code == 400
            bad = await c.post("/api/ask", json={"question": "plot this", "recent_turns": "earlier"})
            assert bad.status_code == 400

    asyncio.run(go())
    assert fake.calls == [("answer", "how many placements?", "merged"), ("followup", "plot this", TURNS, "merged")]
    log = [json.loads(ln) for ln in (d / "web_log.jsonl").read_text(encoding="utf-8").splitlines()]
    asks = [e for e in log if e.get("event") == "ask" and e.get("status") == "ok"]
    assert [e.get("turns") for e in asks] == [None, 2]
