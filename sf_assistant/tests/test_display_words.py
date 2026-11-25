"""Prerequisite P3 of 2026-11-25: the display words (agent/display.py), read by code only.

``split`` takes how to show an answer out of a question (chart, visual, table, detail, summary, breakdown, file) and
keeps what to fetch; free words go anywhere, gated words only in a display shape, and the guards keep a word that is a
value, a name, an API name, a quoted span, a grouping or a question about the thing itself. The chart ask grammar moved
here from agent/charts.py: ``tests/golden/chart_ask_parity.json`` holds ``charts.chart_ask`` of every question string of
tests/test_charts.py and tests/test_charts_flow.py as it was before the move. Made-up names only.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from sf_assistant.agent import charts, display as D, followup

ROOT = Path(__file__).resolve().parents[1]


def _s(text, guards=None):
    return D.split(text, guards)


# ------------------------------------------------------------------------------------------------ the log shapes
@pytest.mark.parametrize("text, search, flags", [
    # L8: a chart verb at the head of a clause reads as "show"
    ("can you plot interviews of last 6 months", "can you show interviews of last 6 months", {"chart": True}),
    # L65: "artifact" was searched as a field; it goes, the visual flag stays
    ("can you visualize all recruiter performance artifact", "can you show all recruiter performance",
     {"chart": True, "visual": True}),
    ("make a table of all the interviews having score more than 100",
     "show all the interviews having score more than 100", {"table": True}),
    # L28-31: a clause of filler and display words goes whole, the end mark stays
    ("how many interviews happened in this year, monthwise. and also plot a graph for it",
     "how many interviews happened in this year, monthwise.", {"chart": True}),
    ("how many interviews happened in this year. show me the monthwise plot.",
     "how many interviews happened in this year. show monthwise.", {"chart": True}),
    ("show a bar chart of interviews by status", "show interviews by status", {"chart": True}),
    ("bar chart of interviews by status", "interviews by status", {"chart": True}),
    ("how many interviews in the last 6 months, show as a pie", "how many interviews in the last 6 months",
     {"chart": True}),
])
def test_log_shapes(text, search, flags):
    d = _s(text)
    assert d.search == search and {k: v for k, v in d.flags().items()} == flags and not d.empty


def test_capability_ask_is_empty_and_gets_no_rewrite():
    """L68: "can you do visualizations ?" names no data: empty, and a modal ask with no pointer."""
    d = _s("can you do visualizations ?")
    assert d.empty and d.capability and d.search == "" and d.chart
    for text in ("plot it", "chart please", "isko table me dikhao", "now plot that", "can you make it a bar"):
        d = _s(text)
        assert d.empty and not d.capability, text   # a pointer or no modal frame: the rewrite reads it


# ------------------------------------------------------------------------------------------------ Hinglish and typos
@pytest.mark.parametrize("text, search, flags", [
    ("interviews ka graph dikhao last 6 months", "interviews dikhao last 6 months", {"chart": True}),
    ("interviews ka pie chart banao status wise", "interviews dikhao status wise", {"chart": True}),
    ("detail me batao interviews of this week", "batao interviews of this week", {"detail": "full"}),
    ("summary do is mahine ke interviews ki", "dikhao is mahine ke interviews ki", {"detail": "summary"}),
    ("tabel me dikhao interviews", "dikhao interviews", {"table": True}),
    ("plto interviews by month", "show interviews by month", {"chart": True}),
    ("chrat of interviews by status", "interviews by status", {"chart": True}),
    ("grafh of offers", "offers", {"chart": True}),
    ("show recruiter artifcat", "show recruiter", {"chart": True, "visual": True}),
    ("show recruiter artifactt", "show recruiter", {"chart": True, "visual": True}),
    ("visulize offers by month", "show offers by month", {"chart": True}),
    ("export interviews to exel", "show interviews", {"file": "excel"}),
    ("detial me batao interviews", "batao interviews", {"detail": "full"}),
    ("show interviews as a dashbord", "show interviews", {"chart": True, "visual": True}),
])
def test_hinglish_and_typos(text, search, flags):
    d = _s(text)
    assert d.search == search and d.flags() == flags


@pytest.mark.parametrize("text, search, flags", [
    ("show interviews as a table", "show interviews", {"table": True}),
    ("give me a table of offers by status", "show offers by status", {"table": True}),
    ("interviews this month in tabular form", "interviews this month", {"table": True}),
    ("list interviews in detail", "list interviews", {"detail": "full"}),
    ("list candidates with full details", "list candidates", {"detail": "full"}),
    ("give me a detailed report of interviews", "show interviews", {"detail": "full"}),
    ("give me a summary of interviews last month", "show interviews last month", {"detail": "summary"}),
    ("summarize interviews of last week", "show interviews of last week", {"detail": "summary"}),
    ("in short, how many offers this year?", "how many offers this year?", {"detail": "summary"}),
    ("show me a monthly report of interviews", "show monthly interviews", {"detail": "summary"}),
    ("give me a breakdown of offers this year", "show offers this year", {"breakdown": True}),
    ("export the list of interviews to csv", "show the list of interviews", {"file": "csv"}),
    ("interviews by status in excel", "interviews by status", {"file": "excel"}),
    ("interviews by status, xlsx please", "interviews by status", {"file": "excel"}),
])
def test_each_flag_and_shape(text, search, flags):
    d = _s(text)
    assert d.search == search and d.flags() == flags


@pytest.mark.parametrize("text, refused, search", [
    ("chart nahi chahiye, sirf count batao", {"chart"}, "sirf count batao"),
    ("no chart please, just the count of offers", {"chart"}, "just the count of offers"),
    ("interviews by status without a table", {"table"}, "interviews by status"),
])
def test_refused_flags(text, refused, search):
    d = _s(text)
    assert d.refused == frozenset(refused) and d.search == search and not d.chart and not d.table
    d = _s("bina table ke batao")
    assert d.refused == {"table"} and d.empty


# ------------------------------------------------------------------------------------------------ the guards
@pytest.mark.parametrize("text, guard", [
    ("interviews with status 'Detailed Review'", "quoted"),
    ('how many interviews named "Bar Chart Review"', "quoted"),
    ("what is Report_URL__c", "api"),
    ("what is a dashboard", "about"),
    ("how do I add a chart to a report in salesforce", "about"),
    ("which flow updates the trainer dashboard", "about"),
    ("what is an artifact", "about"),
    ("flow chart of onboarding", "not_a_chart"),
    ("plot number 5 owner", "not_a_chart"),
    ("breakdown by status", "grouping"),
    ("monthwise breakdown of interviews", "grouping"),
    ("give me a breakdown of interviews by status", "grouping"),
    ("session summary of S-1", "gate"),
    ("coverage detail of session S-2", "gate"),
    ("education details of Zorvan Quell", "gate"),
    ("details of Zorvan Quell", "gate"),
    ("show the dashboard", "gate"),
    ("reports to Zorvan", "gate"),
    ("trainer dashboard users", "gate"),
    ("data table flow", "gate"),
    ("candidates who know excel", "gate"),
    ("candidates skilled in excel", "gate"),
    ("interviews shown in the dashboard", "gate"),
])
def test_guards_keep_the_word(text, guard):
    d = _s(text)
    assert d.search == text and not d.flags() and any(g == guard for _w, g in d.kept), d.kept


@pytest.mark.parametrize("text", ["address line 1 of Zorvan", "how many line managers are there",
                                  "show the bar of the interview room", "what is the trend of offers over 6 months",
                                  "how many interviews by status", ""])
def test_no_display_word_no_change(text):
    d = _s(text)
    assert d.search == text and not d.found() and not d.flags()


class _Guards:
    """Guards 7-9 without a session: fixed spans and a fixed invariant answer."""

    def __init__(self, spans=(), same=True):
        self._spans, self._same, self.calls = list(spans), same, []

    def spans(self, typed):
        self.calls.append(typed)
        return list(self._spans)

    def same(self, typed, search):
        return self._same


def test_value_and_name_guards_keep_the_word():
    text = "how many interviews for Bar Chart Review this month"
    at = text.index("Bar Chart Review")
    d = _s(text, _Guards([(at, at + len("Bar Chart Review"), "value")]))
    assert d.search == text and ("chart", "value") in d.kept and not d.chart
    text = "interviews of Zorvan Artifact as a table"
    at = text.index("Zorvan Artifact")
    d = _s(text, _Guards([(at, at + len("Zorvan Artifact"), "name")]))
    assert d.search == "interviews of Zorvan Artifact" and d.table and ("artifact", "name") in d.kept


@pytest.mark.parametrize("text", [
    "send the interviews of last month to excel", "export interviews by status to Excel",
    "interviews by status in excel", "interviews by status as an excel file", "interviews by status excel me bhejo"])
def test_a_value_that_is_the_file_word_keeps_it_and_the_file_flag(text):
    """P-R12 follow-up of 2026-11-25: a company record named "Excel" (made-up here) keeps the word in the search text
    (the value spot keeps the record name) and the file flag is still set from the typed shape."""
    at = text.lower().rindex("excel")
    d = _s(text, _Guards([(at, at + len("excel"), "value")]))
    assert d.search == text and d.file == "excel" and ("excel", "value") in d.kept and d.words == ()
    assert d.flags() == {"file": "excel"} and d.as_log()["kept"] == [["excel", "value"]]
    # with another display word: that one goes, the file word stays, both flags are set
    text2 = "plot interviews of last 6 months to excel"
    at = text2.rindex("excel")
    d = _s(text2, _Guards([(at, at + 5, "value")]))
    assert d.search == "show interviews of last 6 months to excel" and d.chart and d.file == "excel"
    assert d.words == ("plot",) and ("excel", "value") in d.kept


def test_a_longer_value_a_name_or_another_word_sets_no_flag():
    text = "send the interviews to excel infotech"   # a longer stored value: no file shape of its own
    at = text.index("excel infotech")
    d = _s(text, _Guards([(at, len(text), "value")]))
    assert d.search == text and d.file is None and ("excel", "value") in d.kept
    text = "send the interviews to Excel"            # a stored person's name: kept as before, no flag
    at = text.index("Excel")
    d = _s(text, _Guards([(at, at + 5, "name")]))
    assert d.search == text and d.file is None and ("excel", "name") in d.kept
    text = "show interviews of Chart this month"      # a free chart word inside a value: no chart flag
    at = text.index("Chart")
    d = _s(text, _Guards([(at, at + 5, "value")]))
    assert d.search == text and not d.chart and ("chart", "value") in d.kept
    text = "show interviews as a table"              # a gated table word that is a stored value: no table flag
    at = text.index("table")
    d = _s(text, _Guards([(at, at + 5, "value")]))
    assert d.search == text and not d.table and ("table", "value") in d.kept


def test_the_invariant_guard_drops_the_split_and_keeps_the_flags():
    d = _s("show interviews as a table", _Guards(same=False))
    assert d.search == "show interviews as a table" and d.table and ("*", "invariant") in d.kept
    assert not _Guards().calls   # the guard runs only when there is something to take out
    g = _Guards()
    _s("how many interviews by status", g)
    assert g.calls == []


def test_a_number_or_a_quote_lost_drops_the_split():
    d = _s('plot "Q3 2026 chart" offers')
    assert '"Q3 2026 chart"' in d.search


def test_session_guards_read_the_contact_rule_and_the_terms():
    g = D.SessionGuards(types.SimpleNamespace(tools=None))
    assert g.spans("any text") == []
    assert g.same("show interviews as a table", "show interviews")
    assert not g.same("email of Zorvan as a table", "of Zorvan")             # the contact rule reads otherwise
    assert not g.same("support person performance as a chart", "as a chart")  # the term contracts read otherwise


def test_session_guards_spans_from_the_tools():
    class Tools:
        def spot_values(self, question, people=None):
            return [{"listed": True, "start": 4, "end": 9}, {"listed": False, "start": 0, "end": 3}]

        def spot_people(self, question):
            return [{"words": "zorvan quell", "strength": "strong"}, {"words": "kilo", "strength": "weak"}]

    spans = D.SessionGuards(types.SimpleNamespace(tools=Tools())).spans("the Table of Zorvan  Quell and Kilo")
    assert (4, 9, "value") in spans and any(g == "name" for *_x, g in spans)
    assert not any(s == 0 and e == 3 for s, e, _g in spans) and len([g for *_x, g in spans if g == "name"]) == 1


# ------------------------------------------------------------------------------------------------ notes and lines
def test_the_display_note_has_one_line_per_flag():
    assert D.note(None) == "" and D.note(_s("show a bar chart of interviews by status")) == ""   # chart: its own note
    note = D.note(_s("give me a table of offers by status in detail"))
    assert note.startswith("\n\n" + D.NOTE_LINES["lead"])
    assert D.NOTE_LINES["table"] in note and D.NOTE_LINES["detail"] in note and D.NOTE_LINES["summary"] not in note
    assert D.NOTE_LINES["file"] in D.note(_s("export interviews to excel"))
    assert D.note(_s("bina table ke batao")) == ""


def test_lines_and_one_word_list():
    assert set(D.LINES) == {"bare", "bare_tables", "capability", "capability_tables", "capability_visual",
                            "capability_visual_tables", "capability_file", "file"} and D.line("file") == D.LINES["file"]
    assert followup._CHART_WORDS is D.CHART_VERBS and len(D.CHART_VERBS) == 9
    for name in ("_TYPES", "_FOLD", "fold", "_NOT_A_CHART", "_ABOUT", "_STOP", "_ASK", "fires", "_FILLER",
                 "_CHART_WORD", "_ANY_CHART", "_REFUSAL_GAP", "_ENDS_TYPE", "_AFTER_TYPO"):
        assert getattr(charts, name) is getattr(D, name), name


def test_chart_ask_parity_after_the_grammar_move():
    recorded = json.loads((ROOT / "tests" / "golden" / "chart_ask_parity.json").read_text(encoding="utf-8"))
    assert len(recorded) > 1000 and sum(v is not None for v in recorded.values()) > 300
    for question, expected in recorded.items():
        got = charts.chart_ask(question)
        assert (None if got is None else [got.type, got.bare]) == expected, question


def test_log_entry_and_fault_safety(monkeypatch):
    d = _s("show interviews as a table")
    assert d.as_log() == {"flags": {"table": True}, "words": ["table"], "kept": []}
    monkeypatch.setattr(D, "_split", lambda *a: 1 / 0)
    assert D.split("show interviews as a table") == D.Display(search="show interviews as a table")
    long = "plot " + "x " * 600
    monkeypatch.undo()
    assert D.split(long).search == long


# ------------------------------------------------------------------------------------------------ the follow-up rewrite
class _Echo:
    """A rewrite model that sends the typed question back (or any fixed reply), and counts its calls."""

    cfg = types.SimpleNamespace(base_url="http://localhost:8000/v1")

    def __init__(self, reply=None):
        self.reply, self.calls = reply, 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"content": self.reply if self.reply is not None else messages[-1]["content"].rsplit("\n", 1)[-1]}


_TURNS = [{"role": "user", "content": "how many offers were made last month?"},
          {"role": "assistant", "content": "There were 4 offers last month."}]


def test_a_capability_ask_skips_the_rewrite_call():
    llm = _Echo()
    step = followup.rewrite("can you do visualizations ?", _TURNS, llm)
    assert step == followup.Rewrite(followup.OK, "can you do visualizations ?", reason="display_only") and llm.calls == 0
    llm = _Echo("plot the number of offers per month of last month")
    followup.rewrite("plot it", _TURNS, llm)    # a pointer: it means the last answer, so the rewrite reads it
    assert llm.calls == 1


def test_the_l28_shape_passes_the_echo_rule():
    """L28-31 (made-up words): a complete ask with a display clause behind it; the echo rule passes it as typed
    (PR #108), and the display split then drops the clause."""
    q = "can you give me open offers of last six months, monthwise. and also plot a graph for it"
    assert followup._fragment(q) and followup._self_anchored(q)
    assert followup.rewrite(q, _TURNS, _Echo(q)) == followup.Rewrite(followup.OK, q)
    assert D.split(q).search == "can you give me open offers of last six months, monthwise."
    assert D.split(q + ".").search == "can you give me open offers of last six months, monthwise."


def test_a_wise_word_stays_and_a_wise_breakdown_is_a_grouping():
    assert D.split("plot a monthwise graph for open offers for last six months").search == \
        "show monthwise open offers for last six months"
    d = D.split("give me a monthwise breakdown of offers")
    assert not d.breakdown and ("breakdown", "grouping") in d.kept


# ------------------------------------------------------------------------------------------------ review fixes (P3)
@pytest.mark.parametrize("text, search, flags", [
    # E-04: a table at the head of a clause or at the end of a grouped / dated ask
    ("table of interviews by month", "interviews by month", {"table": True}),
    ("give me table of interviews by month", "show interviews by month", {"table": True}),
    ("interviews by month table", "interviews by month", {"table": True}),
    ("monthly interviews table please", "monthly interviews", {"table": True}),
    # E-05: the about guard reads one clause that asks for no data
    ("how many interviews per status? show it as a pie chart using the status field",
     "how many interviews per status? show it using the status field", {"chart": True}),
    ("which objects have the most records? show a bar chart", "which objects have the most records?", {"chart": True}),
    ("kya aap interviews ka chart bana sakte ho status field ke hisaab se",
     "kya aap interviews bana sakte ho status field ke hisaab se", {"chart": True}),
    # E-06 / E-07 / E-08 / E-09: chart types, typos, inflections, file and summary shapes
    ("interviews by status in a pie", "interviews by status", {"chart": True}),
    ("interviews by status donut", "interviews by status", {"chart": True}),
    ("stacked bar of interviews by status", "interviews by status", {"chart": True}),
    ("interviews by month as columns", "interviews by month", {"chart": True}),
    ("bar chat of interviews by status", "interviews by status", {"chart": True}),
    ("make a dashbaord of interviews", "show interviews", {"chart": True, "visual": True}),
    ("make a tble of interviews", "show interviews", {"table": True}),
    ("charting interviews by month", "show interviews by month", {"chart": True}),
    ("create a viz of interviews by month", "show interviews by month", {"chart": True}),
    ("interviews excel bana do", "interviews dikhao", {"file": "excel"}),
    ("make an excel of interviews last month", "show interviews last month", {"file": "excel"}),
    ("detailed interviews last month", "interviews last month", {"detail": "full"}),
    ("quick summary interviews last month", "interviews last month", {"detail": "summary"}),
    ("brief me on interviews last month", "show interviews last month", {"detail": "summary"}),
    # E-13: no broken leftovers
    ("interviews ka table format me do", "interviews dikhao", {"table": True}),
    ("interviews tabular form me dikhao", "interviews dikhao", {"table": True}),
    ("interviews by month (chart)", "interviews by month", {"chart": True}),
    ("interviews by month - chart", "interviews by month", {"chart": True}),
    # R10 of the replay: a table asked with a chart
    ("give me offers per niche in a pie chart and a table", "give me offers per niche", {"chart": True, "table": True}),
])
def test_review_fixes_split_more_display_shapes(text, search, flags):
    d = _s(text)
    assert d.search == search and d.flags() == flags, (d.search, d.flags(), d.kept)


@pytest.mark.parametrize("text, word", [
    # E-10: Hinglish "<field> summary batao" names a field (Comment Summary, Red flags summary, Summary__c)
    ("red flags summary batao last week ke sessions ka", "summary"), ("comment summary batao", "summary"),
    ("session summary dikhao", "summary"), ("background check ka summary batao", "summary"),
    # E-11: the candidate portal dashboard is a place
    ("how many candidates logged into the dashboard last week", "dashboard"),
    # E-12: skill and phrase words
    ("candidates who are good in excel", "excel"), ("candidates strong in excel", "excel"),
    ("candidates available in short notice", "short"), ("how many background checks with details pending", "details"),
    # R7: a field phrase
    ("how many candidates were placed in report period", "report"),
    # R4 of the replay: the word before "breakdown" (or "for each") is the grouping
    ("give me niche breakdown of placements", "niche breakdown"),
    ("give me a status breakdown of interviews", "status breakdown"),
    ("per niche breakdown of placements", "niche breakdown"),
    ("breakdown of applications for each candidate", "breakdown"),
    # R1: skill and job words and refusals stay as typed, and no chart is read
    ("how many visually impaired candidates are there", "visually"),
    ("open positions for visualization engineer", "visualization"),
    ("candidates with tableau visualization experience", "visualization"),
    ("hide the pie chart", "chart"), ("stop showing charts", "charts"), ("graph hata do", "graph"),
    ("remove the chart", "chart"), ("i did not ask for a chart", "chart"),
])
def test_review_fixes_keep_data_words(text, word):
    d = _s(text)
    assert word in d.search and not d.chart and not d.breakdown and not d.detail, (d.search, d.flags(), d.kept)


def test_review_fix_summary_shapes_that_still_go():
    assert _s("summary do is mahine ke interviews ki").detail == "summary"
    d = _s("interviews ka summary me batao")
    assert d.detail == "summary" and "summary" not in d.search
    assert _s("give me a full breakdown of interviews").breakdown


def test_review_fix_refusals_of_the_chart_grammar():
    """R1: every refusal charts.chart_ask turns down is refused here too: no chart flag, the words stay."""
    for text in ("hide the pie chart", "stop showing charts", "chart band karo", "rather than a graph show the rows",
                 "i did not ask for a chart", "don't plot it"):
        d = _s(text)
        assert charts.chart_ask(text) is None and not d.chart and "chart" in d.refused, text


@pytest.mark.parametrize("text, key", [
    ("can you export to excel?", "capability_file"), ("can you make a dashboard?", "capability_visual"),
    ("can you do visualizations ?", "capability"), ("can you make tables?", "capability"),
    ("can you summarize?", "bare"),
])
def test_review_fix_the_capability_line_fits_the_ask(text, key):
    """E-14 and R5: a file ask is told no file is made, a dashboard ask that no dashboards are; with SFA_CHARTS off the
    lines promise tables only."""
    d = _s(text)
    assert d.empty and d.capability and D.capability_key(d) == key, (d.flags(), D.capability_key(d))
    off = D.capability_key(d, charts_on=False)
    assert "chart" not in D.line(off).lower() and "chart" not in D.line(D.bare_key(False)).lower()


def test_review_fix_is_it_possible_is_a_capability_ask():
    d = _s("is it possible to make a chart")
    assert d.empty and d.capability


def test_review_fix_the_rewrite_reads_a_single_chart_or_a_verb_after_an_answer():
    """R2: only plural display nouns skip the rewrite call ("can you do visualizations ?"); "can you plot", "can you
    make a pie chart" and "can you make a table" after an answer ask for that answer's chart or table."""
    for text in ("can you plot", "can you chart", "can you visualize", "could you visualise", "can you tabulate",
                 "can you make a pie chart", "can you plot a graph", "can you make a table"):
        llm = _Echo("plot the number of offers made last month")
        step = followup.rewrite(text, _TURNS, llm)
        assert llm.calls == 1 and step.reason != "display_only", text
    for text in ("can you do visualizations ?", "do you make charts", "can you make charts?"):
        llm = _Echo()
        assert followup.rewrite(text, _TURNS, llm).reason == "display_only" and llm.calls == 0, text
