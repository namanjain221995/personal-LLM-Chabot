"""The charts code draws from record results (the charts of 2026-11-03): the ``SFA_CHARTS`` switch
(``config.charts_settings``), the ask rule, the choice of result and type, the payload, the truth and privacy rules, the
SVG renderer and its check, ``drop_talk`` and the two surfaces' pure functions in ``agent.charts``.

Offline and model-free: no warehouse and no model, only synthetic results shaped like ``QueryEvidence.model_result``;
every name, value and count is made up. The flow through the real ``pipeline.answer`` is in
``tests/test_charts_flow.py``. Every test whose subject depends on the switch sets ``SFA_CHARTS`` itself, so this file
passes with it unset, ``off`` or ``on`` in the shell.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import subprocess
import sys
import time
import types
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS

import pytest

from sf_assistant import config as C
from sf_assistant.agent import charts
from sf_assistant.engine.contact import EMAIL_SHOWN, PHONE_SHOWN

W = charts._WORDS
say = charts._say
TODAY = "2026-11-03"
SVG_NS = "{http://www.w3.org/2000/svg}"


# ---------------------------------------------------------------------------------------------- synthetic results
def group(rows, **kw):
    """A group_records result; ``rows``: (label, value), or (label, value, by)."""
    groups = [{"group": r[0], "value": r[1], **({"by": r[2]} if len(r) > 2 else {})} for r in rows]
    named = sum(1 for g in groups if g["group"] not in ("(blank)", "(unresolved id)"))
    res = {"query_id": "q2", "object": "Interview__c", "grouped_by": "Status__c", "measure": "count", "groups": groups,
           "group_count": named, "shown": len(groups), "unresolved_ids": 0, "ties_at_cutoff": [],
           "ties_at_cutoff_count": 0, "source": "fake copy"}
    if all(isinstance(g["value"], int) for g in groups):
        res["total_across_groups"] = sum(g["value"] for g in groups)
    res.update(kw)
    return res


def trend(rows, grain="month", **kw):
    """A trend_records result; ``rows``: (bucket, value), or (bucket, value, by)."""
    buckets = [{"bucket": r[0], "value": r[1], **({"by": r[2]} if len(r) > 2 else {})} for r in rows]
    res = {"query_id": "q3", "object": "Interview__c", "grain": grain, "date_field": "Date__c", "buckets": buckets,
           "first_bucket_partial": False, "last_bucket_partial": False, "records_without_date": 0,
           "source": "fake copy"}
    if all(isinstance(b["value"], int) for b in buckets):
        res["total"] = sum(b["value"] for b in buckets)
    res.update(kw)
    return res


def months(values, by=None):
    """(bucket, value) rows from 2026-01 on."""
    labels = [f"{2026 + i // 12}-{i % 12 + 1:02d}" for i in range(len(values))]
    return list(zip(labels, values)) if by is None else list(zip(labels, values, by))


def compare(rows, **kw):
    """A compare_records result; ``rows``: (label, value), or (label, value, applied)."""
    segments = [{"label": r[0], "value": r[1], "applied": r[2] if len(r) > 2 else {}} for r in rows]
    return {"query_id": "q4", "object": "Interview__c", "segments": segments, "source": "fake copy", **kw}


def breakdown(fields, count, **kw):
    """A count_records result with a breakdown."""
    return {"query_id": "q5", "object": "Interview__c", "count": count, "counted": "Interview__c records",
            "breakdown": fields, "source": "fake copy", **kw}


FIELDS = {"Status__c": "Status", "Round__c": "Round", "Date__c": "Date of Interview", "Rate__c": "Bill Rate",
          "Recruiter__c": "Recruiter", "Type__c": "Employment Type"}
CATALOG = NS(objects={
    "Interview__c": NS(api="Interview__c", label="Interview", plural_label="Interviews",
                       fields={k: NS(api=k, label=v, kind="text", ref_targets=[]) for k, v in FIELDS.items()}),
    "Note__c": NS(api="Note__c", label="Note", plural_label="", fields={}),
    "Plain__c": NS(api="Plain__c", label="", plural_label="", fields={}),
    "Session__c": NS(api="Session__c", label="Session", plural_label="Sessions", fields={}),
})


def build(tool, res, args=None, asked=None, question="synthetic question", today=None, catalog=CATALOG):
    """The items of one result, through the pure core."""
    clean = charts._Cleaner(question, [res])
    return charts._build(tool, res, args, None, clean, charts._Labels(catalog), asked, today)


def one(tool, res, args=None, asked=None, **kw):
    items = build(tool, res, args, asked, **kw)
    assert len(items) == 1
    return items[0]


def svg_of(item):
    svg = charts._draw(item)
    assert charts._check_svg(svg), svg[:300]
    return svg


def tag(el):
    return el.tag.replace(SVG_NS, "")


def nodes(svg):
    return [el for el in ET.fromstring(svg).iter() if tag(el) != "svg"]


def texts(svg):
    return [el.text or "" for el in nodes(svg) if tag(el) == "text"]


def said(svg):
    return " ".join(texts(svg))


def evidence(tool, res, ok=True, args=None, kind="data"):
    held = res if isinstance(res, dict) else {}
    return NS(tool=tool, ok=ok, kind=kind, object=held.get("object"), model_result=res if ok else None,
              query_id=held.get("query_id"), arguments=args or {}, statements=[], error=None)


def rec_of(*items, blocks=None):
    return NS(evidence=list(items), table_blocks=blocks or {})


def make(res=None, tool="group_records", question="bar chart of interviews by status", final="There are 5.",
         kind="records", mode="lean", args=None, today=TODAY, rec=None, catalog=CATALOG):
    ask = charts.chart_ask(question)
    rec = rec if rec is not None else rec_of(evidence(tool, res, args=args))
    return charts.make(ask, question, final, kind, mode, rec, catalog, today)


# ------------------------------------------------------------------------------------------------ the switch
_DEFAULT = (C.CHARTS_DEFAULT, C.CHARTS_DEFAULT == "on")   # derived; tests/test_settings.py pins the default itself


@pytest.mark.parametrize("raw,mode,on,problem", [
    (None, *_DEFAULT, ""), ("", *_DEFAULT, ""), ("   ", *_DEFAULT, ""), ("off", "off", False, ""),
    ("on", "on", True, ""), ("ON", "on", True, ""), (" On ", "on", True, ""),
    ("private-value-7", "off", False, "SFA_CHARTS must be off or on"),
    ("1", "off", False, "SFA_CHARTS must be off or on"),
])
def test_switch_read_each_call_never_stops(monkeypatch, raw, mode, on, problem):
    monkeypatch.delenv("SFA_CHARTS", raising=False)
    assert C.charts_settings() == C.ChartsConfig()   # read at each call, not at import
    if raw is not None:
        monkeypatch.setenv("SFA_CHARTS", raw)
    cfg = C.charts_settings()
    assert (cfg.mode, cfg.on, cfg.problem) == (mode, on, problem)
    assert "private-value-7" not in repr(cfg)   # the value itself is never repeated
    monkeypatch.setenv("SFA_CHARTS", "on")
    assert C.charts_settings().on
    assert C.CHARTS_DEFAULT == C.ChartsConfig().mode and C.CHARTS_MODES == ("off", "on")


# ------------------------------------------------------------------------------------------------ the ask
REQUESTS = {
    'en': [
        'show me a chart of candidates by status', 'graph the joinings per month', 'plot revenue by month',
        'plot number of candidates by month', 'plot no of joinings per month', 'visualize applications by recruiter',
        'give me a visualisation of leads by source', 'bar chart of candidates by niche',
        'pie chart of placements by client', 'line graph of monthly joinings', 'horizontal bar chart of recruiters',
        'donut chart of status', 'doughnut chart of status', 'histogram of salaries',
        'scatter plot of rate vs experience', 'area chart of applications per week', 'column chart of submissions',
        'stacked bar of status by recruiter', 'show this as a bar chart', 'show this visually',
        'draw this graphically', 'as a pie', 'show it as a line', 'make it a line chart', 'make it horizontal',
        'trend chart of applications', 'pie of sales by region', 'can you chart this', 'chart it',
        'which recruiter has the most joinings? chart it.', 'how many candidates per month, show in a graph',
        'list candidates by niche and also plot it', 'table and chart of leads by source',
        'give the records and a bar chart of them', 'just the data and a chart please', 'funnel chart of stages',
        'funnel of the conversion stages', 'how to make a chart of candidates by status',
    ],
    'salesforce-words-that-are-still-requests': [
        'bar chart of accounts', 'chart of accounts by industry', 'chart of accounts per owner',
        'chart of accounts owner wise', 'accounts ka chart banao', 'report chart of leads by source',
        'dashboard chart of cases by owner', 'cash flow chart for the year', 'graph of open opportunities by stage',
        'chart view of candidates by niche',
    ],
    'hinglish': [
        'graph dikhao', 'chart banao', 'plot karo', 'grafh', 'grafh dikhao', 'candidates ka graph dikhao',
        'status ka pie chart bana do', 'har mahine ki joining line graph me dikhao',
        'recruiter wise candidates ka bar chart banao', 'isko chart me dikhao', 'pie chart me dikhao',
        'pie me dikhao', 'owner ke hisaab se total hours ka graph banao', 'graph bana ke do', 'chart chahiye',
        'mujhe ek chart do', 'chart bnao', 'plot kar do', 'chart bana ke dikhao candidates by status',
        'month wise joining ka graph', 'isko bar chart me kar do', 'iska graph banao',
        'graph bhi dikhao aur data bhi', 'data aur chart dono do',
    ],
    'typo': [
        'bar chat of candidates', 'pie chrat of status', 'bar grpah of joinings', 'histogarm of salaries',
        'scater plot of rate vs experience', 'visulize candidates by status', 'vizualize leads by source',
        'chrat of leads', 'grpah of leads by month', 'line grph of joinings', 'graf dikhao', 'grph dikhao',
        'graaf dikhao', 'garaf dikhao', 'chrt banao', 'plto karo', 'charts of leads by source',
    ],
    'blind list of run 3': [
        'can i get a graph for total leads per city', 'need a bar graph for open positions by client',
        'show opportunity amount by stage in a bar chart', 'draw a pie for lead source share',
        'joinings for the last six months as a line graph', 'pls make graph of leads by owner',
        'give me chart for candidates created this month week wise', 'monthly submissions trend graph',
        'compare placements across recruiters using a bar chart',
        'visual representation of candidates by visa status', 'pictorial view of leads by source',
        'candidates by status ka graph plus list', 'kitne leads aaye har mahine, graph me batao',
        'har recruiter ke placements ka chart de do', 'is data ka chart bana', 'bar graph bana do visa wise',
        'pie chart bana k dikhao lead source ka', 'line chart me monthly count dikha', 'charts dikhao leads ke',
        'graf bnao month wise', 'pie chat for status', 'barchart of leads by owner', 'piechart of status',
        'bargraph of joinings', 'histogram for experience years',
    ],
    'added from the critique': [
        'no of candidates chart', 'bina table ke chart dikhao', 'sirf data ka chart', 'just the numbers in a chart',
        'only data for 2026 in a chart', 'not just the data, plot it too', 'bar diagram of leads by source',
        'pie diagram of status', 'bar char of leads', 'pie char of status', 'line char of joinings',
        'plotting leads by month', 'plotted joinings by month', 'visual of leads by source',
        'trend line of joinings', 'isko bar me dikhao', 'isko line me dikhao', 'isko pie me dikhao',
        'can you visualize this', 'show leads visually',
    ],
}
NOT_REQUESTS = {
    'org-and-flow': [
        'show me the org chart', 'org chart of the recruitment team', 'who is at the top of the org chart',
        'org-chart for engineering', 'orgchart of the sales team', 'organization chart of the company',
        'organisational chart of the client', 'flow chart of the candidate onboarding process',
        'make a flowchart of the application flow', 'flow-chart of lead conversion',
        'process flow chart of onboarding', 'workflow chart of the release', 'flow chart banao',
        'api aur db ka flow chart banao', 'lead conversion flow ka flow chart do', 'who drew the org chart',
    ],
    'chart-of-accounts': [
        'what is in the chart of accounts', 'show the chart of accounts', 'does the org have a chart of accounts',
        'chart of accounts dikhao', 'chart of accounts kidhar hai',
    ],
    'report-dashboard-listview': [
        'what is a report chart', 'which reports have a report chart', 'report chart is not loading',
        'how do I add a chart to a report in Salesforce', 'how to create a chart on a dashboard',
        'dashboard chart is not loading', 'what is a dashboard chart', 'who owns the recruiting dashboard',
        'how many dashboards are there', 'dashboard dikhao', 'how many list view charts are there',
        'list view chart on the Contact object', 'what is ListViewChart.ChartType',
        'what does Dashboard.ChartTheme hold', 'what is the chart view in the admin training dashboard',
    ],
    'graph-words': [
        'what is the Graph API', 'Microsoft Graph API integration', 'does the org use GraphQL',
        'composite graph API limit', 'what is a data graph', 'how many records are in the knowledge graph',
        'graph database for contacts',
    ],
    'plot-words': [
        'plot no 12 candidate address', 'candidates living at plot number 5', 'plot 7 sector 21 address',
        'plot twist in the project', 'what was the plot of the movie we discussed', 'summarize the subplot',
    ],
    'look-alike-words': [
        'graphic designer candidates', 'how many graphics roles are open', 'chartered accountant candidates',
        'lets chat about the numbers', 'what did I ask in the chat', 'line manager of the sales team',
        'who works as a line manager', 'show candidates with bar council certificate',
        'area manager of the west region', 'column name of candidate status', 'in line with the policy',
        'line item on the invoice', 'pie rate for the vendor',
    ],
    'data-words-without-a-chart-ask': [
        'how many open cases are there', 'list the top five accounts by revenue', 'what is the trend of joinings',
        'joining trend by month', 'what is the distribution of candidates by niche',
        'candidates in the recruitment funnel', 'funnel stage of the lead', 'month wise joining count',
        'recruiter wise candidates', 'status ka count dikhao', 'data dikhao', 'table banao',
    ],
    'about-a-chart': [
        'what is a bar chart', 'what does a box plot mean', 'pie chart kya hota hai', 'graph ka matlab kya hai',
        'what are charts used for',
    ],
    'table-follow-ups': [
        'make it a table', 'make it a summary', 'make it a list', 'make that a paragraph',
    ],
    'stop-words': [
        'no chart, just numbers', 'without a chart', 'show the table instead', 'just the table please', 'table only',
        'hide the graph', 'remove the chart', 'skip the chart, just the table', "don't plot it",
        'chart nahi chahiye, sirf table do', 'graph mat banao', 'bina chart ke dikhao', 'sirf table do',
        'chart mat dikhao',
    ],
    'blind list of run 3': [
        'show me the reporting structure of the sales team', 'who reports to the head of delivery',
        'what flow runs when a lead is converted', 'which flow updates the candidate status',
        'explain the onboarding flow', 'flowchart banake samjhao lead conversion', 'org chart me uske upar kaun hai',
        'is there a flow chart for the visa process', 'which dashboard uses the chart theme',
        'list all dashboards with their folder', 'list view of open cases', 'how many report folders are there',
        'what is the chart of accounts object in salesforce', 'ms graph connection status',
        'does salesforce support graphql queries', 'plot no. 45 industrial area candidates',
        'candidate address plot no 9 ki details', 'which area has the most candidates',
        'line of business for this account', 'bar code field on the product', 'column level security on Contact',
        'advocate as a bar council member', 'trend of lead conversion last quarter', 'status wise count of leads',
        'average experience by niche', 'top recruiters by placements this month', 'chat history of this session',
    ],
    'added from the critique': [
        'no of candidates', 'har bar me kitne leads', 'line me lagao', 'main character of the story',
        'visual studio candidates', 'data visualization candidates',
        'candidates with tableau visualization experience', 'open positions for visualization engineer',
        'how many visually impaired candidates',
    ],
}
#: Found by the review of 2026-11-03: plain requests a refusal rule read as "no chart".
REQUESTS_AFTER_REVIEW = [
    'is bar chart me dikhao', 'ek bar chart banao', 'har recruiter ka bar graph', 'show it as a bar by month',
    'chart dikhao na', 'chart banao na', 'graph dikhao na', 'chart do na', 'leads ka graph banao na',
    'pie chart dikhao na', 'plot karo na', 'leads ka chart dikhao na', 'no table just chart', 'not table but chart',
    'skip table and chart it', 'without table show chart', 'remove table and plot', 'hide names and plot it',
    'why not chart it', 'why not show a graph', 'no show interviews chart', 'no show count graph',
    'no-show chart by recruiter',
    'not submitted candidates graph', 'not submitted chart by recruiter', 'candidates without visa chart',
    'leads without owner graph', 'plot q1 revenue', 'plot h1 leads by owner',
    'use a bar chart instead of a pie chart for leads by source', 'can you make a chart of leads in salesforce',
]
#: Refusals and questions about a chart that fired, and addresses the plot guard missed.
NOT_REQUESTS_AFTER_REVIEW = [
    # the check before the go-live of 2026-11-04: "bar" as a time in Hinglish, job titles, surnames, no chart word
    'ek bar please check karo kitne interviews hue', 'is bar please sahi count do', 'is bar char candidates select hue',
    'ek bar chat history dekho', 'pichli bar please dekho', 'kitni bar please batao', 'agli bar please yaad rakhna',
    'pichli bar char interviews hue the', 'ek bar chat check karo', 'how many candidates applied as area sales manager',
    'candidates working as line supervisor', 'how many interviews mention communication as an area for improvement',
    'which candidates were flagged as a bar raiser', 'list candidates working as funnel analyst',
    'how many candidates want to switch to line manager role', 'how to show picklist values as columns in a report',
    'how many candidates for graphical designer role', 'candidates with java treemap experience',
    'did Polt attend the session', 'how many interviews did Graf take last month', 'candidates under recruiter Plott',
    'who works as a line supervisor', 'what is the funnel of a candidate in salesforce', 'switch to area manager',
    'switch to area manager in the org settings', 'advocate as a bar council member',
    'chart na banao', 'chart ke bina dikhao candidates by status', 'chart ke bina', 'graph ke bina batao',
    'chart ke bagair', 'graph ke bagair sirf count batao', 'chart ki zaroorat nahi hai, sirf numbers do',
    'chart ki jarurat nahi hai', 'no need for a chart, just list the candidates', 'give me a table instead of a chart',
    'rather than a graph show the rows', 'stop showing charts', 'graph hata do', 'chart hatao', 'chart band karo',
    'i did not ask for a chart', 'hide the pie chart', 'no bar chart', 'skip the line graph', 'remove the bar chart',
    'what is a histogram', 'what is a heatmap', "what's a treemap", 'histogram kya hai', 'histogram kya hota hai',
    'heatmap kya hai', 'how do i embed a graph in a lightning page', 'how to embed a graph in a lightning page',
    'how do i add a chart on the Lead layout', 'can i add a chart to the candidate page in salesforce',
    'when should i use a pie chart', 'what is the difference between a bar chart and a line chart',
    'which chart is best for this data', 'how do you read a line chart', 'define bar chart',
    'pie chart kab use karte hain', 'which candidates live at plot-45 sector 21', 'candidates at plot-45 sector 21',
    'plot 12b sector 7 address', 'plot a-12 candidates',
    # pinned as they are, and a miss: "show" belongs to a refusal, so these read as "no, show a chart" turned down. The
    # person gets a normal answer with no chart. `no-show` or a word between fires. The owner decides (docs/runbook.md)
    'no show chart', 'no show chart by recruiter',
]
KNOWN_WRONG = ["graph neural network candidates", "d3 charts", "Chart Industries",
               "Gantt Systems", "which area has most candidates, show a chart"]
BARE = ["now plot that", "same thing as a bar chart", "show the previous answer in a graph", "pichle answer ka graph",
        "upar wale data ka chart banao", "isko graph mai dikhao", "kya tum chart bana sakte ho", "the chart is wrong",
        "the chart is not showing", "chart is empty", "can you make a chart", "now plot that by month",
        "plot that by recruiter", "plot this for last year", "make it a pie chart by recruiter",
        "iska graph month wise banao", "plot that by lead source", "isko last 6 months ka graph banao"]
NOT_BARE = ["graph of this month", "interviews per month graph dikhao", "plot by month the leads",
            "chart by recruiter of interviews", "plot leads by month", "deliverables status wise graph me dikhao",
            "recruiter wise interviews ka chart chahiye, top 10", "plot that for west region", "chart of offers this year",
            "plot this year's leads by month", "plot that by month for leads", "plot that by month leads",
            # a split can be the data: a false bare loses the answer (review of the model-test fixes)
            "per month interviews ka graph banao", "by recruiter interviews ka chart chahiye", "per month revenue chart",
            "chart by month, leads only", "plot by month: interviews", "show chart by candidates by recruiter",
            "chart by lead source", "offer wise graph dikhao", "candidate wise chart", "lead wise pie chart"]


@pytest.mark.parametrize("phrase", [*(p for ps in REQUESTS.values() for p in ps), *REQUESTS_AFTER_REVIEW])
def test_detect_requests_fire(phrase):
    assert charts.chart_ask(phrase) is not None


@pytest.mark.parametrize("phrase", [*(p for ps in NOT_REQUESTS.values() for p in ps if p not in KNOWN_WRONG),
                                    *NOT_REQUESTS_AFTER_REVIEW])
def test_detect_non_requests_silent(phrase):
    assert charts.chart_ask(phrase) is None


@pytest.mark.parametrize("phrase", KNOWN_WRONG)
def test_detect_known_wrong(phrase):
    """Pinned as they are: these fire and should not (the last fires rightly, but its type reads ``unsupported``)."""
    assert charts.chart_ask(phrase) is not None
    assert charts.chart_ask("which area has most candidates, show a chart").type == "unsupported"


@pytest.mark.parametrize("phrase,expected", [
    ("bar chart of line items", "bar"), ("pie chat of status", "pie"), ("line grph of joinings", "line"),
    ("chart of leads by area", None), ("chart of open positions by funnel stage", None),
    ("graph of candidates by line manager", None), ("chart of leads per area", None),
    ("chart of line of business", None),
    ("horizontal bar chart of recruiters", "horizontal_bar"), ("donut chart of status", "donut"),
    ("doughnut chart of status", "donut"), ("pie chart of placements by client", "pie"),
    ("line graph of monthly joinings", "line"), ("stacked bar of status by recruiter", "bar"),
    ("column chart of submissions", "bar"), ("histogram of salaries", "unsupported"),
    ("scatter plot of rate vs experience", "unsupported"), ("funnel chart of stages", "unsupported"),
    ("area chart of applications per week", "unsupported"), ("show me a chart of candidates by status", None),
    # the first type the question names, never one it turns down
    ("use a bar chart instead of a pie chart for leads by source", "bar"),
    ("bar chart instead of pie chart of leads", "bar"),
    ("use a bar chart instead of a line chart for leads by source", "bar"),
    ("bar chart of leads, not a pie", "bar"),
    # a word before them that reads as a field: no type, so no false note on the chart
    ("chart of orders per product line", None), ("chart of revenue by business line", None),
    ("chart of leads in west area", None), ("show chart in a line", "line"), ("plot revenue as a line", "line"),
    # a horizontal chart in other words
    ("horizontal chart of leads by status", "horizontal_bar"), ("horizontal graph of leads", "horizontal_bar"),
    ("make it horizontal", "horizontal_bar"),
])
def test_named_type_table(phrase, expected):
    assert charts.chart_ask(phrase).type == expected


def test_bare_table():
    for phrase in [*BARE, "chart dikhao na", "no table just chart"]:
        assert charts.chart_ask(phrase).bare is True, phrase
    for phrase in [*NOT_BARE, "leads ka graph banao na"]:
        assert charts.chart_ask(phrase).bare is False, phrase


def test_type_words_in_a_field_name_give_no_false_note():
    for question in ("chart of orders per product line", "chart of leads in west area"):
        item = make(group(THREE), question=question)[1][0]
        assert item["chart"]["type"] == "bar" and item["notes"] == [], question
    item = make(group(THREE), question="use a bar chart instead of a line chart for leads by source")[1][0]
    assert item["chart"]["type"] == "bar" and item["notes"] == []
    item = make(group(THREE), question="use a pie chart instead of a bar chart for leads by source")[1][0]
    assert item["chart"]["type"] == "pie" and item["notes"] == []


def test_ask_time_stays_small():
    """A long question is no ask: the how-to branch would be cubic on it (the command line has no cap; the page's is the
    same 1000 characters). At that cap the worst tuned text stays in milliseconds."""
    t0 = time.perf_counter()
    assert charts.chart_ask("how to add chart in x " * 2000) is None
    assert time.perf_counter() - t0 < 0.5
    at_cap = ("how to add chart in x " * 46)[:1000]
    t0 = time.perf_counter()
    charts.chart_ask(at_cap)
    assert time.perf_counter() - t0 < 0.2
    assert charts.chart_ask("show me a chart of candidates by status " + "x" * 1000) is None


@pytest.mark.parametrize("value", [None, 5, 5.5, [], {}, b"chart", "x " * 100000, "chart " * 50000])
def test_chart_ask_never_raises(value):
    found = charts.chart_ask(value)
    assert found is None or isinstance(found, charts.ChartAsk)


def test_note_bound_and_empty_when_off(monkeypatch):
    monkeypatch.setenv("SFA_CHARTS", "off")
    assert charts.note_for("bar chart of interviews by status") == ""
    assert not charts.drawable_ask("bar chart of interviews by status")
    monkeypatch.setenv("SFA_CHARTS", "on")
    assert charts.note_for("bar chart of interviews by status") == charts.RECORDS_CHART_NOTE
    assert charts.drawable_ask("bar chart of interviews by status")
    assert charts.note_for("how many interviews are there") == ""       # no ask
    assert charts.note_for("now plot that") == ""                       # bare
    assert not charts.drawable_ask("now plot that")
    assert charts.RECORDS_CHART_NOTE.startswith("\n\nThis question asks for a chart.")
    assert len(charts.RECORDS_CHART_NOTE) == 610   # 471 before the rate clause of 2026-11-16
    assert "a rate, share or percentage over time: trend_records with part_filters" in charts.RECORDS_CHART_NOTE
    assert charts.bare_line() == W["bare"] and charts.OVERRULE_REASON == W["overrule"]
    assert len(W) == 81   # 73 + the rate words and answer_failed of 2026-11-16


# ------------------------------------------------------------------------------------------------ reasons
THREE = [("Completed", 12), ("Scheduled", 5), ("Canceled", 2)]
SINGLE_ROW = [("Completed", 12)]


@pytest.mark.parametrize("tool,res,word,rows", [
    ("about_people", {"query_id": "q1", "people": [{"name": "Person A"}]}, "people", 0),
    ("find_references", {"query_id": "q1", "references": []}, "people", 0),
    ("count_records", {"query_id": "q1", "count": 7, "counted": "x"}, "one_number", 0),
    ("aggregate_records", {"query_id": "q1", "op": "sum", "value": 7}, "one_number", 0),
    ("group_records", group(SINGLE_ROW), "one_row", 1),
    ("trend_records", trend(months([4])), "one_row", 1),
    ("compare_records", compare([("a", 4)]), "one_row", 1),
    ("count_records", breakdown({"Status__c": {"Done": 4}}, 4), "one_row", 1),
    ("list_records", {"query_id": "q1", "rows": [{"Name": "Candidate 1"}]}, "list", 0),
    ("search_records", {"query_id": "q1", "matches": []}, "list", 0),
    ("percentage_of_records", {"query_id": "q1", "percent": 40.0}, "one_share", 0),
    ("get_field_values", {"query_id": "q1", "values": [{"value": "a", "records": 3}, {"value": "b", "records": 1}]},
     "field_values", 0),
    ("get_field_history", {"query_id": "q1", "changes": []}, "field_values", 0),
    ("group_records", group([("a", 0), ("b", 0)]), "all_zero", 2),
    ("trend_records", trend(months([0, 0, 0])), "all_zero", 3),
    ("group_records", group([]), "no_rows", 0),
    ("group_records", {**group([]), "blank_group_value": 1}, "only_blank", 0),
    ("group_records", group([("a", None), ("b", None)]), "no_values", 2),
    ("group_records", group([("a", 0), ("b", None)], measure="sum", measure_field="Rate__c"), "no_positive", 2),
    ("trend_records", trend(months([None, 0, 0]), measure="avg", measure_field="Rate__c"), "no_positive", 3),
    ("group_records", {**group(THREE), "no_data_fields": ["Interview__c.Recruiter__c"]}, "no_field_data", 0),
    ("group_records", {**group([]), "no_data_fields": ["Interview__c.Recruiter__c"]}, "no_field_data", 0),
    ("trend_records", {**trend(months([0, 0])), "no_data_fields": ["Interview__c.Recruiter__c"]}, "no_field_data", 0),
    ("compare_records", {**compare([("a", 0), ("b", 1)]), "no_data_fields": ["Interview__c.Recruiter__c"]},
     "no_field_data", 0),
    ("count_records", {"query_id": "q1", "count": 0, "counted": "x", "no_data_fields": ["Interview__c.Recruiter__c"]},
     "no_field_data", 0),
    ("group_records", group([("[hidden]", 3), ("key=[hidden]", 2)]), "hidden", 2),
    ("group_records", group([("Same label", 3), ("Same  label", 2)]), "same_labels", 2),
    ("group_records", group([("a", 3), ("b", -2)]), "negative", 2),
    ("group_records", group([("a", "2026-01-02"), ("b", "2026-02-03")]), "dates", 2),
    ("group_records", {**group(THREE), "trimmed": True}, "too_large", 0),
    ("group_records", {**group(THREE), "groups": "broken"}, "fault", 0),
    ("group_records", group(THREE, measure="median_of_something"), "fault", 0),
    ("some_new_tool", {"query_id": "q1"}, "fault", 0),
])
def test_reason_per_shape(tool, res, word, rows):
    item = build(tool, res)[0]
    assert item["chart"] is None and item["reason"] == W[word]
    assert len(item["data"]) == rows
    final, items, lines, error = make(res, tool=tool, final="Answer text.")
    assert lines == [W[word]] and final == "Answer text." and error == ""
    assert [len(i["data"]) for i in items] == ([rows] if rows else [])


def test_reason_too_many_gives_the_results_own_count():
    res = group([(f"Group {i}", 40 - i) for i in range(31)])
    item = one("group_records", res)
    assert item["chart"] is None and item["reason"] == say("too_many", COUNT="31")
    assert len(item["data"]) == 31
    res = group([(f"Group {i}", 60 - i) for i in range(50)], group_count=64)
    assert one("group_records", res)["reason"] == say("too_many", COUNT="64")
    long_trend = trend(months([3] * 121))
    assert one("trend_records", long_trend)["reason"] == say("too_many", COUNT="121")
    assert one("trend_records", trend(months([3] * 120)))["chart"]["type"] == "line"
    assert one("group_records", group([(f"Group {i}", 40 - i) for i in range(30)]))["chart"]["type"] == "horizontal_bar"


def test_no_rows_says_why():
    """T23: "no records match" only for a result with no cut."""
    assert one("group_records", group([]))["reason"] == W["no_rows"]
    assert one("group_records", group([]), {"min_value": 500})["reason"] == W["no_rows_min"]
    assert one("group_records", group([]), {"min_value": "500"})["reason"] == W["no_rows_min"]
    assert one("group_records", group([]), {"min_value": " "})["reason"] == W["no_rows"]
    cut = group([], group_count=5, notes=["result trimmed to fit: showing 0 of 5"])
    assert one("group_records", cut)["reason"] == W["too_large"]
    assert one("trend_records", trend([], notes=["result trimmed to fit: showing 0 of 12"]))["reason"] == W["too_large"]
    held = group([], notes=["result trimmed to fit: showing 10 of 50"])
    assert one("group_records", held)["reason"] == W["no_rows"]


def test_no_rows_with_records_left_out_says_so():
    """T23: a ``top`` ranking leaves the blank and unresolved groups out, so records can match with no row to show. The
    real tool's shapes are in ``tests/test_charts_flow.py``."""
    assert one("group_records", group([], blank_group_value=1))["reason"] == W["only_blank"]
    assert one("group_records", group([], unresolved_ids=2))["reason"] == W["only_blank"]
    blank_sum = group([], measure="sum", measure_field="Rate__c", notes=["the (blank) group is left out of the ranking"])
    assert one("group_records", blank_sum)["reason"] == W["only_blank"]   # only a count holds the left-out numbers
    # a zero left out is nothing left out, and a cut says more
    assert one("group_records", group([], blank_group_value=0))["reason"] == W["no_rows"]
    cut = group([], blank_group_value=1, notes=["result trimmed to fit: showing 0 of 5"])
    assert one("group_records", cut)["reason"] == W["too_large"]


def test_no_rows_under_a_minimum_speaks_of_named_groups():
    """T23: under ``min_value`` the tool holds ``blank_group_value`` and ``unresolved_ids`` only for a group that passed
    it, and ``top`` leaves that group out. The named groups fell short all the same, so ``only_blank`` would be false,
    and the words speak only of named groups. The real tool's shapes are in ``tests/test_charts_flow.py``."""
    for left_out in ({"blank_group_value": 12}, {"unresolved_ids": 12}):
        item = one("group_records", group([], **left_out), {"top": 3, "min_value": 10})
        assert item["reason"] == W["no_rows_min"] and "no named group" in item["reason"]


def test_no_rows_of_a_ranking_under_another_measure():
    """T23: only a count holds the number of the ``(unresolved id)`` group, so under another measure a ranking of only
    such records reads like one with no match, and the words must be true of both. ``top`` reads as the tool reads it:
    0 is clamped to 1, and a blank is no argument. With no ranking, no group is left out of ``groups``."""
    rate = {"measure": "sum", "measure_field": "Rate__c"}
    for top in (3, "3", 0):
        assert one("group_records", group([], **rate), {"top": top})["reason"] == W["no_rows_top"]
    assert one("group_records", group([], **rate), {"top": " "})["reason"] == W["no_rows"]
    assert one("group_records", group([], **rate))["reason"] == W["no_rows"]
    assert one("group_records", group([]), {"top": 3})["reason"] == W["no_rows"]


def test_reason_of_the_answer_comes_first():
    res = group(THREE)
    done = evidence("group_records", res)
    bad = evidence("group_records", None, ok=False)
    sync = evidence("get_sync_status", {"query_id": "q9"})
    ask = charts.chart_ask("bar chart of interviews")
    off = charts.make(ask, "q", "text", "records", "lean", None, CATALOG, TODAY)
    assert off == ("text", [], [W["records_off"]], "")
    assert charts.make(ask, "q", "", "none", None, rec_of(bad), CATALOG, TODAY)[2] == [W["failed"]]
    assert charts.make(ask, "q", "t", "knowledge", "full", rec_of(bad), CATALOG, TODAY)[2] == [W["failed"]]
    assert charts.make(ask, "q", "t", "knowledge", "full", rec_of(done), CATALOG, TODAY)[2] == [W["no_data"]]
    assert charts.make(ask, "q", "t", "none", None, rec_of(done), CATALOG, TODAY)[2] == [W["no_data"]]
    assert charts.make(ask, "q", "t", "records", "lean", rec_of(), CATALOG, TODAY)[2] == [W["no_data"]]
    assert charts.make(ask, "q", "t", "records", "lean", rec_of(sync), CATALOG, TODAY)[2] == [W["no_data"]]
    # a failed call beside a good one: the good one is drawn; get_sync_status never decides the reason
    assert charts.make(ask, "q", "t", "records", "lean", rec_of(bad, done, sync), CATALOG, TODAY)[2] == []
    last = evidence("list_records", {"query_id": "q6", "rows": []})
    assert charts.make(ask, "q", "t", "records", "lean", rec_of(last, sync), CATALOG, TODAY)[2] == [W["list"]]


def test_no_data_result_shows_no_item_but_the_line():
    _final, items, lines, _err = make({"query_id": "q1", "rows": [{"Name": "Candidate 1"}]}, tool="list_records")
    assert items == [] and lines == [W["list"]]


# ------------------------------------------------------------------------------------------------ rows and numbers
def test_rows_equal_result():
    res = group(THREE)
    item = one("group_records", res)
    assert item["chart"]["x_key"] == "Status" and item["chart"]["y_keys"] == ["Records"]
    assert item["data"] == [{"Status": g["group"], "Records": g["value"]} for g in res["groups"]]
    res = trend(months([4, 0, 9]))
    item = one("trend_records", res)
    assert item["data"] == [{"Month": b["bucket"], "Records": b["value"]} for b in res["buckets"]]
    res = compare([("a", 4), ("b", 7)])
    item = one("compare_records", res)
    assert item["data"] == [{"Compared": "a", "Records": 4}, {"Compared": "b", "Records": 7}]
    res = breakdown({"Status__c": {"Done": 3, "Open": 1}}, 4)
    assert one("count_records", res)["data"] == [{"Status": "Done", "Records": 3}, {"Status": "Open", "Records": 1}]
    assert isinstance(item["data"][0]["Compared"], str)


def _every_shape():
    split = [{"Full": 3, "Part": 1}, {"Full": 2, "Part": 2}, {"Full": 1}]
    applied = {"period": {**_span("2026-11-01", "2026-11-30", "2026-12-01")["period"]}, "phrase": "typed words",
               "filters": ["Status__c = 'x'", "Recruiter__c.Name = 'y'"], "any_of": ["Type__c = 'z'"],
               "related": ["has a Session__c"]}
    span = {"filters": ["Status__c = 'x'"], **_span("2026-11-01", "2026-11-30", "2026-12-01")}
    return [("group_records", group(THREE), {}), ("trend_records", trend(months([3, 0, 9])), {}),
            ("compare_records", compare([("a", 4), ("b", 7)]), {}),
            ("count_records", breakdown({"Status__c": {"Done": 3, "Open": 1}, "Type__c": {"A": 2, "B": 2}}, 4), {}),
            ("group_records", group([("Done", 4, split[0]), ("Open", 4, split[1]), ("Lost", 1, split[2])]), {}),
            ("trend_records", trend(months([4, 4, 1], split)), {"split_by": "Type__c"}),
            # a null segment, every kind of "applied" and of note, and a cut under a typed order
            ("group_records", group([("a", 5, {"Full": 5, "Part": None}), ("b", 4, {"Full": 2, "Part": 2})]), {}),
            ("group_records", group(THREE, applied=applied, notes=["showing 3 of 64 groups"], group_count=64),
             {"order": "ASC", "top": 3}),
            ("trend_records", trend(months([3, 4]), applied=applied, notes=[PART_RUN]), {"order": "desc"}),
            ("compare_records", compare([("a", 4, span), ("b", 7, span)], applied=applied, notes=["a note"]), {}),
            ("count_records", breakdown({"Status__c": {"Done": 3, "Open": 1}}, 4, applied=applied), {"order": "asc"})]


def test_result_not_mutated():
    for tool, res, args in _every_shape():
        before, args_before = copy.deepcopy(res), copy.deepcopy(args)
        kept = res
        for asked in (None, "bar", "line", "pie", "horizontal_bar", "donut", "unsupported"):
            for item in build(tool, res, args, asked):
                if item["chart"]:
                    svg_of(item)
        make(res, tool=tool, args=args)
        charts.for_tool(tool, res, args, CATALOG, TODAY)
        assert res == before and res is kept and args == args_before, (tool, res)


def test_numbers_ignore_answer_text():
    res = group(THREE)
    _f1, one_items, _l, _e = make(res, final="There are 999 of them: Completed 41.")
    _f2, two_items, _l, _e = make(res, final="There are 12.")
    assert one_items[0]["data"] == two_items[0]["data"] == [{"Status": g["group"], "Records": g["value"]}
                                                            for g in res["groups"]]
    assert one_items[0]["svg_b64"] == two_items[0]["svg_b64"]


def test_code_reads_only_the_result_the_model_saw():
    """P1: blocked fields never reach ``model_result``; a chart reads nothing wider, statements above all."""
    class Guarded(NS):
        @property
        def statements(self):
            raise AssertionError("a chart reads only model_result")

    e = Guarded(tool="group_records", ok=True, kind="data", object="Interview__c", model_result=group(THREE),
                query_id="q2", arguments={}, error=None)
    _final, items, lines, error = charts.make(charts.chart_ask("bar chart"), "bar chart", "t", "records", "lean",
                                              rec_of(e), CATALOG, TODAY)
    assert items and not lines and not error


# ------------------------------------------------------------------------------------------------ type table
def _groupn(n, **kw):
    return group([(f"G{i}", 40 - i) for i in range(n)], **kw)


def _stack_group(n=3):
    return group([(f"G{i}", 5, {"Full": 3, "Part": 2}) for i in range(n)])


def _stack_trend(n=3, **kw):
    return trend(months([5] * n, [{"Full": 3, "Part": 2}] * n), **kw)


def _avg_trend(measure="avg"):
    by = {"Full": 3.5, "Part": 2.5}
    return trend(months([3.0, 3.0, 3.0], [by, by, by]), measure=measure, measure_field="Rate__c")


def _bad_stack_group():
    return group([("G0", 9, {"Full": 3, "Part": 2}), ("G1", 5, {"Full": 3, "Part": 2})])


def _bad_stack_trend():
    return trend(months([9, 5], [{"Full": 3, "Part": 2}, {"Full": 3, "Part": 2}]))


NO_FIT = "type_no_fit"
OTHER = "type_other"
LONG_LABEL = group([("A label over sixteen chars", 4), ("Short", 2)])
PIE_NO = group(THREE, measure="sum", measure_field="Rate__c")

#: (name, tool, result, asked, type, stacked, series drawn, note words)
TYPE_CELLS = [
    # group, breakdown, compare with no usable split
    ("group none", "group_records", group(THREE), None, "bar", False, False, []),
    ("group 9 rows", "group_records", _groupn(9), None, "horizontal_bar", False, False, []),
    ("group long label", "group_records", LONG_LABEL, None, "horizontal_bar", False, False, []),
    ("group bar", "group_records", group(THREE), "bar", "bar", False, False, []),
    ("group bar 9 rows", "group_records", _groupn(9), "bar", "horizontal_bar", False, False, []),
    ("group horizontal", "group_records", group(THREE), "horizontal_bar", "horizontal_bar", False, False, []),
    ("group line", "group_records", group(THREE), "line", "bar", False, False, [NO_FIT]),
    ("group line 9 rows", "group_records", _groupn(9), "line", "horizontal_bar", False, False, [NO_FIT]),
    ("group pie", "group_records", group(THREE), "pie", "pie", False, False, []),
    ("group donut", "group_records", group(THREE), "donut", "donut", False, False, []),
    ("group pie refused", "group_records", PIE_NO, "pie", "bar", False, False, [NO_FIT]),
    ("group donut refused", "group_records", PIE_NO, "donut", "bar", False, False, [NO_FIT]),
    ("group other", "group_records", group(THREE), "unsupported", "bar", False, False, [OTHER]),
    ("compare none", "compare_records", compare([("a", 4), ("b", 7)]), None, "bar", False, False, []),
    ("compare line", "compare_records", compare([("a", 4), ("b", 7)]), "line", "bar", False, False, [NO_FIT]),
    ("compare pie", "compare_records", compare([("a", 4), ("b", 7)]), "pie", "bar", False, False, [NO_FIT]),
    ("compare other", "compare_records", compare([("a", 4), ("b", 7)]), "unsupported", "bar", False, False, [OTHER]),
    ("breakdown none", "count_records", breakdown({"Status__c": {"A": 3, "B": 1}}, 4), None, "bar", False, False, []),
    ("breakdown pie", "count_records", breakdown({"Status__c": {"A": 3, "B": 1}}, 4), "pie", "pie", False, False, []),
    # group, usable stack
    ("stack none", "group_records", _stack_group(), None, "bar", True, True, []),
    ("stack 9 rows", "group_records", _stack_group(9), None, "horizontal_bar", True, True, []),
    ("stack bar", "group_records", _stack_group(), "bar", "bar", True, True, []),
    ("stack horizontal", "group_records", _stack_group(), "horizontal_bar", "horizontal_bar", True, True, []),
    ("stack line", "group_records", _stack_group(), "line", "bar", True, True, [NO_FIT]),
    ("stack pie", "group_records", _stack_group(), "pie", "bar", True, True, [NO_FIT]),
    ("stack donut", "group_records", _stack_group(), "donut", "bar", True, True, [NO_FIT]),
    ("stack other", "group_records", _stack_group(), "unsupported", "bar", True, True, [OTHER]),
    # trend, no split
    ("trend none", "trend_records", trend(months([3, 4, 5])), None, "line", False, False, []),
    ("trend bar", "trend_records", trend(months([3, 4, 5])), "bar", "bar", False, False, []),
    ("trend bar 30", "trend_records", trend(months([3] * 30)), "bar", "bar", False, False, []),
    ("trend bar 31", "trend_records", trend(months([3] * 31)), "bar", "line", False, False, [NO_FIT]),
    ("trend horizontal", "trend_records", trend(months([3, 4, 5])), "horizontal_bar", "line", False, False, [NO_FIT]),
    ("trend line", "trend_records", trend(months([3, 4, 5])), "line", "line", False, False, []),
    ("trend pie", "trend_records", trend(months([3, 4, 5])), "pie", "line", False, False, [NO_FIT]),
    ("trend donut", "trend_records", trend(months([3, 4, 5])), "donut", "line", False, False, [NO_FIT]),
    ("trend other", "trend_records", trend(months([3, 4, 5])), "unsupported", "line", False, False, [OTHER]),
    # trend, usable stack (count and sum)
    ("trend stack none", "trend_records", _stack_trend(), None, "bar", True, True, []),
    ("trend stack sum", "trend_records", _stack_trend(measure="sum", measure_field="Rate__c"), None, "bar", True, True,
     []),
    ("trend stack 31", "trend_records", _stack_trend(31), None, "line", False, True, []),
    ("trend stack bar", "trend_records", _stack_trend(), "bar", "bar", True, True, []),
    ("trend stack bar 31", "trend_records", _stack_trend(31), "bar", "line", False, True, []),
    ("trend stack horizontal", "trend_records", _stack_trend(), "horizontal_bar", "bar", True, True, [NO_FIT]),
    ("trend stack line", "trend_records", _stack_trend(), "line", "line", False, True, []),
    ("trend stack pie", "trend_records", _stack_trend(), "pie", "bar", True, True, [NO_FIT]),
    ("trend stack donut", "trend_records", _stack_trend(), "donut", "bar", True, True, [NO_FIT]),
    ("trend stack other", "trend_records", _stack_trend(), "unsupported", "bar", True, True, [OTHER]),
    # trend, split under avg, min, max or distinct, up to 8 series
    ("avg none", "trend_records", _avg_trend(), None, "line", False, False, ["split_left"]),
    ("avg bar", "trend_records", _avg_trend(), "bar", "line", False, False, ["split_left"]),
    ("avg horizontal", "trend_records", _avg_trend(), "horizontal_bar", "line", False, False, ["split_left", NO_FIT]),
    ("avg line", "trend_records", _avg_trend(), "line", "line", False, True, []),
    ("min line", "trend_records", _avg_trend("min"), "line", "line", False, True, []),
    ("max line", "trend_records", _avg_trend("max"), "line", "line", False, True, []),
    ("distinct line", "trend_records", _avg_trend("count_distinct"), "line", "line", False, True, []),
    ("avg pie", "trend_records", _avg_trend(), "pie", "line", False, False, ["split_left", NO_FIT]),
    ("avg donut", "trend_records", _avg_trend(), "donut", "line", False, False, ["split_left", NO_FIT]),
    ("avg other", "trend_records", _avg_trend(), "unsupported", "line", False, False, ["split_left", OTHER]),
    # any split not usable: the totals in the row's type
    ("group bad split", "group_records", _bad_stack_group(), None, "bar", False, False, ["split_left"]),
    ("group bad split pie", "group_records", _bad_stack_group(), "pie", "pie", False, False, ["split_left"]),
    ("trend bad split", "trend_records", _bad_stack_trend(), None, "line", False, False, ["split_left"]),
    ("trend bad split bar", "trend_records", _bad_stack_trend(), "bar", "bar", False, False, ["split_left"]),
    ("trend bad split line", "trend_records", _bad_stack_trend(), "line", "line", False, False, ["split_left"]),
]


@pytest.mark.parametrize("name,tool,res,asked,kind,stacked,series,words", TYPE_CELLS, ids=[c[0] for c in TYPE_CELLS])
def test_type_table(name, tool, res, asked, kind, stacked, series, words):
    item = build(tool, res, {"split_by": "Type__c"}, asked)[0]
    chart = item["chart"]
    assert (chart["type"], chart["stacked"]) == (kind, stacked)
    assert (len(chart["y_keys"]) >= 2) == series
    assert set(chart) == {"type", "x_key", "y_keys", "title", "stacked"}
    assert all(chart["x_key"] in row and (series or chart["y_keys"][0] in row) for row in item["data"])
    prefixes = {"split_left": "The split by ", NO_FIT: "A ", OTHER: "This chart type is not available"}
    typed = [n for n in item["notes"] if n.startswith(tuple(prefixes.values()))]
    assert len(typed) == len(words), item["notes"]
    for word in words:
        assert any(n.startswith(prefixes[word]) for n in typed), (word, item["notes"])


def test_type_names_on_the_note():
    item = one("group_records", group(THREE), None, "line")
    assert say("type_no_fit", ASKED="line", TYPE="bar") in item["notes"]
    item = one("trend_records", trend(months([3, 4, 5])), None, "horizontal_bar")
    assert say("type_no_fit", ASKED="horizontal bar", TYPE="line") in item["notes"]
    item = one("group_records", _groupn(9), None, "unsupported")
    assert say("type_other", TYPE="horizontal bar") in item["notes"]


def test_code_never_picks_a_pie_by_itself():
    for tool, res, args in _every_shape():
        for item in build(tool, res, args, None):
            assert item["chart"]["type"] not in ("pie", "donut")


def test_stacked_only_on_bars():
    item = one("trend_records", _stack_trend(), {"split_by": "Type__c"}, "line")
    assert item["chart"]["type"] == "line" and item["chart"]["stacked"] is False and len(item["chart"]["y_keys"]) == 2
    assert one("trend_records", _stack_trend())["chart"]["stacked"] is True
    assert one("group_records", _stack_group())["chart"]["stacked"] is True


@pytest.mark.parametrize("tool,res,args", [
    ("group_records", group(THREE, measure="sum", measure_field="Rate__c"), None),
    ("group_records", group(THREE, records_matched=30), None),
    ("group_records", group(THREE, group_count=5), None),                       # top cut
    ("group_records", group(THREE), {"min_value": 2}),
    ("group_records", group(THREE, total_across_groups=40), None),              # does not add up
    ("group_records", group(THREE, total_across_groups=20), None),              # by 1: a looser rounding would pass
    ("group_records", group(THREE, blank_group_value=4), None),                 # the blank group left out
    ("group_records", group(THREE, unresolved_ids=4), None),
    ("group_records", group([("a", None), ("b", 4)], total_across_groups=4), None),
    ("group_records", _groupn(9), None),                                        # 9 rows
    ("count_records", breakdown({"Status__c": {"A": 3, "B": 1}}, 9), None),     # does not add up to the count
    ("trend_records", trend(months([3, 4, 5])), None),
    ("compare_records", compare([("a", 4), ("b", 7)]), None),
])
def test_pie_refused_each_cause(tool, res, args):
    for asked in ("pie", "donut"):
        item = one(tool, res, args, asked)
        assert item["chart"]["type"] not in ("pie", "donut")
        assert any(n.startswith("A ") and "does not fit" in n for n in item["notes"]), item["notes"]
    assert one(tool, res, args, "pie")["chart"]["stacked"] is False


def test_pie_allowed_when_adds_up():
    for tool, res in (("group_records", group(THREE)), ("group_records", _groupn(8)),
                      ("group_records", group([("   ", 5), ("b", 4)], group_count=2)),   # a stored blank is a named group
                      ("group_records", group([("a", 3), ("(blank)", 2)], group_count=1)),
                      ("count_records", breakdown({"Status__c": {"A": 3, "B": 1, "(other)": 2}}, 6))):
        for asked in ("pie", "donut"):
            assert one(tool, res, None, asked)["chart"]["type"] == asked


def _slice_arcs(svg):
    """``(start, more than half, end)`` of each slice's outer arc, in drawing order, from its path."""
    arc = re.compile(r"(-?[\d.]+) (-?[\d.]+)A100 100 0 ([01]) 1 (-?[\d.]+) (-?[\d.]+)")
    found = [arc.search(el.attrib["d"]) for el in nodes(svg) if tag(el) == "path"]
    return [((float(m[1]), float(m[2])), m[3] == "1", (float(m[4]), float(m[5]))) for m in found if m]


@pytest.mark.parametrize("asked", ["pie", "donut"])
@pytest.mark.parametrize("rows", [THREE, [("Done", 12), ("Open", 0), ("Late", 5), ("Lost", 2)]],
                         ids=["three", "a zero"])
def test_pie_slices_follow_each_other_by_their_share(rows, asked):
    """T17: from the top, each slice's arc starts where the one before it ended and spans its share of the whole, and
    the last one ends where the first began. A row of 0 draws no slice."""
    arcs = _slice_arcs(svg_of(one("group_records", group(rows), None, asked)))
    shares = [v / sum(r[1] for r in rows) for _label, v in rows if v > 0]
    assert len(arcs) == len(shares)
    cx, cy = arcs[0][0][0], arcs[0][0][1] + 100   # the first slice starts straight above the centre
    for (start, large, end), share, (following, _large, _end) in zip(arcs, shares, arcs[1:] + arcs[:1]):
        span = (math.atan2(end[1] - cy, end[0] - cx) - math.atan2(start[1] - cy, start[0] - cx)) % (2 * math.pi)
        assert span == pytest.approx(2 * math.pi * share, abs=0.001)
        assert large == (share > 0.5) and end == following


def test_pie_no_percent_no_centre():
    for asked in ("pie", "donut"):
        svg = svg_of(one("group_records", group(THREE), None, asked))
        assert "%" not in said(svg) and "%" not in svg.split("</style>")[1]
        assert {"12", "5", "2"} <= set(texts(svg))
        # the slices' values and no centre number; the subtitle's total reads "Total: 19"
        assert sorted(t for t in texts(svg) if t.isdigit()) == ["12", "2", "5"]


def test_no_code_made_other():
    res = group([(f"G{i}", 40 - i) for i in range(12)], group_count=20)
    item = one("group_records", res)
    assert "Other" not in json.dumps(item) and "other" not in said(svg_of(item)).replace("another", "")
    many = group([("a", 11, {f"S{i}": 1 for i in range(11)}), ("b", 11, {f"S{i}": 1 for i in range(11)})])
    item = one("group_records", many)
    assert not any("ther" in k for k in item["chart"]["y_keys"])
    own = group([("a", 9, {"S1": 3, "S2": 2, "(other)": 4}), ("b", 5, {"S1": 3, "S2": 2})])
    item = one("group_records", own)
    assert "(other)" in item["chart"]["y_keys"]
    assert item["data"][0]["(other)"] == 4 and "(other)" not in item["data"][1]


def _totals(item):
    return [s for s in item["subtitle"] if s.startswith(W["total"])]


def test_total_only_when_rows_add_up():
    assert _totals(one("group_records", group(THREE))) == [f"{W['total']}: 19"]
    assert not _totals(one("group_records", group(THREE, total_across_groups=40)))
    assert not _totals(one("group_records", group(THREE, total_across_groups=20)))   # by 1, over rows that add up to 19
    assert not _totals(one("trend_records", trend(months([3, 4, 5]), total=13)))
    res = group(THREE)
    del res["total_across_groups"]
    assert not _totals(one("group_records", res))
    assert not _totals(one("group_records", group(THREE, records_matched=19)))
    sums = group(THREE, measure="sum", measure_field="Rate__c", total_across_groups=19)
    assert not _totals(one("group_records", sums))
    assert _totals(one("trend_records", trend(months([3, 4, 5])))) == [f"{W['total']}: 12"]
    assert not _totals(one("trend_records", trend(months([3, 4, 5]), total=99)))
    assert not _totals(one("compare_records", compare([("a", 4), ("b", 7)])))
    distinct = trend(months([3, 4, 5]), measure="count_distinct", measure_field="Rate__c", total=12)
    assert not _totals(one("trend_records", distinct))
    assert _totals(one("count_records", breakdown({"Status__c": {"A": 3, "B": 1}}, 4))) == [f"{W['total']}: 4"]


# ------------------------------------------------------------------------------------------------ partial periods
PART_FIRST = "the first bucket (2026-01) covers only 2026-01-15 onward (the period starts inside that month); first"
PART_RUN = "the last bucket (2026-03) is still running (today is 2026-03-10)"
PART_ENDS = "the last bucket (2026-03) covers only up to 2026-03-20 (the period ends inside that month)"


@pytest.mark.parametrize("note,index,label", [(PART_FIRST, 0, "2026-01"), (PART_RUN, 2, "2026-03"),
                                              (PART_ENDS, 2, "2026-03")])
def test_partial_three_cases(note, index, label):
    item = one("trend_records", trend(months([3, 4, 5]), notes=[note]))
    assert item["partial"] == [index]
    assert say("partial", LABELS=label) in item["notes"]
    svg = svg_of(item)
    assert f"{label}*" in texts(svg) and any("stroke-dasharray" in el.attrib for el in nodes(svg))
    hollow = [el for el in nodes(svg) if tag(el) == "circle" and el.attrib["fill"] == "#ffffff"]
    assert len(hollow) == 1
    columns = svg_of(one("trend_records", trend(months([3, 4, 5]), notes=[note]), None, "bar"))
    assert f"{label}*" in texts(columns)
    assert sum(1 for el in nodes(columns) if el.attrib.get("opacity") == "0.45") == 1


def test_partial_marks_every_segment_and_a_horizontal_bar():
    """T4 on the marks a plain column test does not reach: every segment of a partial stacked column and a partial
    segment drawn as a horizontal bar, and no other mark."""
    by = {"Full": 2, "Part": 2, "Rest": 1}
    item = one("trend_records", trend(months([5, 5, 5], [by] * 3), notes=[PART_RUN]))
    assert item["chart"]["stacked"] is True and item["partial"] == [2]
    marks = _bar_marks(svg_of(item))
    assert len(marks) == 9
    assert [el.attrib.get("opacity") for el in marks] == [None] * 6 + ["0.45"] * 3
    res = compare([("this month", 5, _span("2026-11-01", "2026-11-30", "2026-12-01")), ("last month", 8, {})])
    item = one("compare_records", res, None, "horizontal_bar", today=TODAY)
    assert item["chart"]["type"] == "horizontal_bar" and item["partial"] == [0]
    assert [el.attrib.get("opacity") for el in _bar_marks(svg_of(item))] == ["0.45", None]


def test_partial_label_carries_its_star_on_horizontal_bars():
    """Section 8: ``*`` on a partial label, on the shape ``test_partial_three_cases`` does not reach, and on no other
    label."""
    res = compare([("this month", 5, _span("2026-11-01", "2026-11-30", "2026-12-01")), ("last month", 8, {})])
    item = one("compare_records", res, None, "horizontal_bar", today=TODAY)
    assert item["chart"]["type"] == "horizontal_bar"
    shown = texts(svg_of(item))
    assert "this month*" in shown and "last month" in shown
    assert "this month" not in shown and "last month*" not in shown


def test_partial_only_on_drawn_bucket():
    """A trend ``_fit`` trimmed: the note names a bucket that is no longer there, and nothing is marked."""
    res = trend(months([3, 4]), notes=[PART_RUN, "result trimmed to fit: showing 2 of 3"])
    item = one("trend_records", res)
    assert item["partial"] == [] and not any(n.startswith("* ") for n in item["notes"])
    assert item["truncated"] is True and say("showing", SHOWN="2", TOTAL="3", UNIT=W["periods"]) in item["notes"]
    assert one("trend_records", trend(months([3, 4]), last_bucket_partial=True))["partial"] == []   # the notes decide


def _span(start, end=None, exclusive=None):
    return {"period": {"date_field": "Date__c", "phrase": "typed words", "from": start, "to": end,
                       "to_exclusive": exclusive, "time_zone": "Asia/Kolkata"}}


def test_compare_running_period_marked():
    res = compare([("last month", 8, _span("2026-10-01", "2026-10-31", "2026-11-01")),
                   ("this month", 5, _span("2026-11-01", "2026-11-30", "2026-12-01"))])
    item = one("compare_records", res, today=TODAY)
    assert item["partial"] == [1] and say("partial", LABELS="this month") in item["notes"]
    svg = svg_of(item)
    assert "this month*" in texts(svg)
    assert sum(1 for el in nodes(svg) if el.attrib.get("opacity") == "0.45") == 1
    # to_exclusive = today + 1 day is the _future_note test's own limit: not past it
    edge = compare([("a", 8, _span("2026-10-01", "2026-11-03", "2026-11-04")),
                    ("b", 5, _span("2026-09-01", "2026-09-30", "2026-10-01"))])
    assert one("compare_records", edge, today=TODAY)["partial"] == []
    # the first date past it: to_exclusive = today + 2 days, and it is to_exclusive that is read, not to
    first = compare([("a", 8, _span("2026-10-01", "2026-11-04", "2026-11-05")),
                     ("b", 5, _span("2026-09-01", "2026-09-30", "2026-10-01"))])
    assert one("compare_records", first, today=TODAY)["partial"] == [0]


def test_compare_no_today_no_mark():
    res = compare([("a", 8, _span("2026-10-01", "2026-12-31", "2027-01-01")),
                   ("b", 5, _span("2026-09-01", "2026-09-30", "2026-10-01"))])
    assert one("compare_records", res, today=None)["partial"] == []
    assert one("compare_records", res, today="not a day")["partial"] == []
    assert not any(n.startswith("* ") for n in one("compare_records", res, today=None)["notes"])


def test_group_running_period_note():
    past = group(THREE, applied=_span("2026-11-01", "2026-11-30", "2026-12-01"))
    assert W["running"] in one("group_records", past, today=TODAY)["notes"]
    no_end = group(THREE, applied=_span("2026-01-01"))
    assert W["running"] in one("group_records", no_end, today=TODAY)["notes"]
    assert W["running"] not in one("group_records", past, today=None)["notes"]
    assert W["running"] not in one("group_records", no_end, today=None)["notes"]
    closed = group(THREE, applied=_span("2026-01-01", "2026-01-31", "2026-02-01"))
    assert W["running"] not in one("group_records", closed, today=TODAY)["notes"]
    split = breakdown({"Status__c": {"A": 3, "B": 1}}, 4, applied=_span("2026-11-01", "2026-11-30", "2026-12-01"))
    assert W["running"] in one("count_records", split, today=TODAY)["notes"]
    assert W["running"] not in one("trend_records", trend(months([3, 4]), applied=_span("2026-01-01")),
                                   today=TODAY)["notes"]


# ------------------------------------------------------------------------------------------------ cuts
def test_cut_top():
    item = one("group_records", group(THREE, group_count=9), {"top": 3})
    assert item["truncated"] is True and say("showing_top", SHOWN="3", TOTAL="9", UNIT=W["groups"]) in item["notes"]
    assert one("group_records", group(THREE))["truncated"] is False


def test_cut_counts_named_rows_only():
    """T6: named rows against ``group_count``, exact: a special row is not a named one, and a stored blank is."""
    four = [*THREE, ("(blank)", 4)]
    item = one("group_records", group(four, group_count=3))
    assert item["truncated"] is False and not any(n.startswith("Showing") for n in item["notes"])
    assert say("showing_top", SHOWN="3", TOTAL="5", UNIT=W["groups"]) in one("group_records",
                                                                             group(four, group_count=5))["notes"]
    five = [*THREE, ("(blank)", 4), ("(unresolved id)", 1)]
    assert say("showing_top", SHOWN="3", TOTAL="6", UNIT=W["groups"]) in one("group_records",
                                                                             group(five, group_count=6))["notes"]
    # a whitespace-only stored value reads (blank), yet the tool counts it as a group: no cut and no refused pie
    stored = group([("   ", 5), ("b", 4)], group_count=2)
    item = one("group_records", stored, None, "pie")
    assert item["data"][0]["Status"] == "(blank)" and item["truncated"] is False
    assert item["chart"]["type"] == "pie" and item["notes"] == []


def test_cut_lowest_first():
    item = one("group_records", group(THREE, group_count=9), {"top": 3, "order": "asc"})
    assert item["truncated"] is True and say("showing_low", SHOWN="3", TOTAL="9", UNIT=W["groups"]) in item["notes"]


def test_cut_50_cap():
    res = group([(f"G{i}", 90 - i) for i in range(50)], group_count=64, notes=["showing 50 of 64 groups"])
    res["groups"] = res["groups"][:30]
    res["shown"] = 30
    item = one("group_records", res)
    assert item["truncated"] is True and say("showing_top", SHOWN="30", TOTAL="64", UNIT=W["groups"]) in item["notes"]
    only_note = group(THREE, notes=["showing 3 of 64 groups"])
    only_note["group_count"] = "broken"
    assert say("showing", SHOWN="3", TOTAL="64", UNIT=W["groups"]) in one("group_records", only_note)["notes"]


def test_cut_two_notes_read_the_rows():
    """The 50-group cap and ``_fit`` on one result: the shown count is that of the rows, the whole that of the larger
    note. 49 named groups plus two special rows, of which ``_fit`` cut one."""
    rows = [(f"G{i}", 90 - i) for i in range(48)] + [("(blank)", 2)]
    res = group(rows, group_count=48, notes=["showing 50 of 51 groups", "result trimmed to fit: showing 49 of 50"])
    item = one("group_records", res)
    assert say("showing", SHOWN="49", TOTAL="51", UNIT=W["groups"]) in item["notes"] and item["truncated"] is True
    assert len(item["data"]) == 49


def test_cut_fit_note():
    item = one("trend_records", trend(months([3, 4]), notes=["result trimmed to fit: showing 2 of 12"]))
    assert item["truncated"] is True and say("showing", SHOWN="2", TOTAL="12", UNIT=W["periods"]) in item["notes"]
    trimmed = compare([("a", 4), ("b", 7)], notes=["result trimmed to fit: showing 2 of 4 segments"])
    item = one("compare_records", trimmed)
    assert item["truncated"] is True and say("showing", SHOWN="2", TOTAL="4", UNIT=W["segments"]) in item["notes"]
    assert one("trend_records", trend(months([3, 4])))["truncated"] is False


@pytest.mark.parametrize("order,word", [("ASC", "showing_low"), ("ascending", "showing_low"), (" Asc ", "showing_low"),
                                        ("desc", "showing_top"), ("DESC", "showing_top"), ("descending", "showing_top"),
                                        (None, "showing_top"), ("sideways", "showing")])
def test_order_read_as_the_tool_reads_it(order, word):
    args = {"order": order} if order is not None else {}
    item = one("group_records", group(THREE, group_count=9), args)
    assert say(word, SHOWN="3", TOTAL="9", UNIT=W["groups"]) in item["notes"]


def test_ties_and_min_value():
    item = one("group_records", group(THREE, ties_at_cutoff_count=2, ties_at_cutoff=["Person A", "Person B"]))
    assert say("ties", COUNT="2") in item["notes"] and "Person A" not in json.dumps(item)
    assert not any("tie" in n for n in one("group_records", group(THREE))["notes"])
    item = one("group_records", group(THREE), {"min_value": 2}, "pie")
    assert say("min_value", COUNT="2") in item["notes"] and item["chart"]["type"] == "bar"
    assert item["truncated"] is True and not any(t.startswith(W["total"]) for t in item["subtitle"])
    assert say("min_value", COUNT="2.5") in one("group_records", group(THREE), {"min_value": "2.5"})["notes"]
    # the tool refuses a minimum that is no number, so a result never holds one; its text never reaches a chart
    typed = one("group_records", group(THREE), {"min_value": "two; drop table"}, "pie")
    assert typed["truncated"] is False and "drop" not in json.dumps(typed)
    assert typed["chart"]["type"] == "pie" and _totals(typed) == [f"{W['total']}: 19"]


@pytest.mark.parametrize("blank", ["", "   ", None, [], {}])
def test_blank_arguments_read_as_the_tool_reads_them(blank):
    """``RecordTools._validate`` drops them, so the tool ran without them, and every group is in the rows."""
    args = {"min_value": blank}
    item = one("group_records", group(THREE), args, "pie")
    assert item["truncated"] is False and item["chart"]["type"] == "pie" and not item["notes"]
    assert _totals(item) == [f"{W['total']}: 19"]
    assert one("compare_records", compare([("a", 4), ("b", 7)]), {"measure": blank})["chart"]["y_keys"] == ["Records"]
    cut = one("group_records", group(THREE, group_count=9), {"order": blank})
    assert say("showing_top", SHOWN="3", TOTAL="9", UNIT=W["groups"]) in cut["notes"]
    assert args == {"min_value": blank}   # read, never changed


def test_blank_out_each_measure():
    item = one("group_records", group(THREE, blank_group_value=4))
    assert say("blank_out", COUNT="4") in item["notes"] and item["truncated"] is True
    for measure in ("sum", "avg", "min", "max", "count_distinct"):
        item = one("group_records", group(THREE, measure=measure, measure_field="Rate__c", blank_group_value=4.5))
        assert say("blank_out_value", COUNT="4.5") in item["notes"], measure
    shown = group([*THREE, ("(blank)", 4)], blank_group_value=4)
    assert not any("blank" in n.lower() or "no value" in n for n in one("group_records", shown)["notes"])
    assert not any("no value" in n for n in one("group_records", group(THREE, blank_group_value=None))["notes"])
    for measure, extra in (("count", {}), ("sum", {"measure_field": "Rate__c"})):
        zero = one("group_records", group(THREE, measure=measure, blank_group_value=0, **extra))
        assert zero["notes"] == [] and zero["truncated"] is False, measure   # a blank group of 0 left nothing out


def test_unresolved_out():
    item = one("group_records", group(THREE, unresolved_ids=4))
    assert say("unresolved_out", COUNT="4") in item["notes"] and item["truncated"] is True
    shown = group([*THREE, ("(unresolved id)", 4)], unresolved_ids=4)
    assert not any("not in this copy" in n for n in one("group_records", shown)["notes"])


def test_blanks_stay_labelled():
    rows = [("Done", 5), ("(blank)", 3), ("(unresolved id)", 2), ("(no name)", 1)]
    item = one("group_records", group(rows, group_count=2))
    assert [r["Status"] for r in item["data"]] == ["Done", "(blank)", "(unresolved id)", "(no name)"]
    svg = svg_of(item)
    assert {"(blank)", "(unresolved id)", "(no name)"} <= set(texts(svg))
    assert item["truncated"] is False


def _bar_marks(svg):
    """The bars and their stack segments: series-coloured marks that are no legend swatch."""
    return [el for el in nodes(svg) if tag(el) in ("path", "rect") and el.attrib.get("class", "").startswith("f")
            and "rx" not in el.attrib]


def test_null_never_zero_bar_and_line_gap():
    res = group([("a", 5), ("b", None), ("c", 3)], total_across_groups=8)
    for asked in (None, "horizontal_bar"):
        item = one("group_records", res, None, asked)
        assert item["data"][1]["Records"] is None
        svg = svg_of(item)
        assert len(_bar_marks(svg)) == 2
        assert W["no_value"] in texts(svg)
        assert texts(svg).count("0") == 1   # the axis's own 0 only
    line = trend(months([5, None, 3, 4]), measure="avg", measure_field="Rate__c")
    item = one("trend_records", line)
    assert item["data"][1]["Average of Bill Rate"] is None
    svg = svg_of(item)
    segments = [el for el in nodes(svg) if tag(el) == "path" and el.attrib.get("class") == "l0"]
    assert len(segments) == 1                    # only 2026-03 to 2026-04: a gap at the null
    assert len([el for el in nodes(svg) if tag(el) == "circle"]) == 3


def test_undated_and_overlap():
    undated = trend(months([3, 4]), records_without_date=7)
    assert say("no_date", COUNT="7") in one("trend_records", undated)["notes"]
    assert not any("no date" in n for n in one("trend_records", trend(months([3, 4])))["notes"])
    item = one("group_records", group(THREE, records_matched=11), None, "pie")
    assert say("overlap", COUNT="11") in item["notes"] and item["chart"]["type"] == "bar"


# ------------------------------------------------------------------------------------------------ splits
def _left(item):
    return next((n for n in item["notes"] if n.startswith("The split by ")), None)


def test_stack_adds_up_after_rounding():
    res = group([("a", 0.3, {"S": 0.1, "T": 0.2}), ("b", 0.6, {"S": 0.2, "T": 0.4})], measure="sum",
                measure_field="Rate__c")
    item = one("group_records", res)
    assert item["chart"]["stacked"] is True and _left(item) is None
    nulls = group([("a", 5, {"S": 2, "T": 3}), ("Canceled", None, {"(blank)": None}), ("b", 4, {"S": None, "T": 4})],
                  measure="sum", measure_field="Rate__c")
    item = one("group_records", nulls)
    assert item["chart"]["stacked"] is True and _left(item) is None
    assert item["data"][1]["Total"] is None
    svg = svg_of(item)
    assert len(_bar_marks(svg)) == 3   # S and T of a, T of b; nothing for the null row
    assert W["no_value"] in texts(svg)
    # a real mismatch of 0.01 is one, and a null value beside a number never stacks
    off = group([("a", 0.31, {"S": 0.1, "T": 0.2}), ("b", 0.6, {"S": 0.2, "T": 0.4})], measure="sum",
                measure_field="Rate__c", split_by="Type__c")
    item = one("group_records", off)
    assert item["chart"]["stacked"] is False and _left(item) == say("split_left", FIELD="Employment Type",
                                                                    WHY=W["split_adds"])
    held = group([("a", None, {"S": 3, "T": None}), ("b", 4, {"S": 2, "T": 2})], measure="sum", measure_field="Rate__c",
                 split_by="Type__c")
    item = one("group_records", held)
    assert item["chart"]["stacked"] is False and _left(item) == say("split_left", FIELD="Employment Type",
                                                                    WHY=W["split_adds"])


def test_stack_series_limit():
    """T15: 8 series stack, 9 do not, and those stay in the rows."""
    eight = {f"S{i}": 1 for i in range(8)}
    item = one("group_records", group([("a", 8, eight), ("b", 8, eight)], split_by="Type__c"))
    assert item["chart"]["stacked"] is True and len(item["chart"]["y_keys"]) == 8 and _left(item) is None
    nine = {f"S{i}": 1 for i in range(9)}
    item = one("group_records", group([("a", 9, nine), ("b", 9, nine)], split_by="Type__c"))
    assert item["chart"]["stacked"] is False and all(f"S{i}" in item["data"][0] for i in range(9))
    assert _left(item) == say("split_left", FIELD="Employment Type", WHY=W["split_many"])


def test_split_reasons_true():
    bad = _bad_stack_group()
    expected = say("split_left", FIELD="Employment Type", WHY=W["split_adds"])
    res = {**bad, "split_by": "Type__c"}
    assert _left(one("group_records", res)) == expected
    avg = group([("a", 2.5, {"S": 3.0, "T": 2.0}), ("b", 3.5, {"S": 3.0, "T": 4.0})], measure="avg",
                measure_field="Rate__c", split_by="Type__c")
    assert _left(one("group_records", avg)) == say("split_left", FIELD="Employment Type", WHY=W["split_measure"])
    many = {f"S{i}": 1 for i in range(11)}
    eleven = group([("a", 11, many), ("b", 11, many)], split_by="Type__c")
    assert _left(one("group_records", eleven)) == say("split_left", FIELD="Employment Type", WHY=W["split_many"])
    two = group([("a", 5, {"S": 3, "T": 2}), ("b", 4, {"S": 3, "T": 1})], split_by="Type__c")
    assert _left(one("group_records", two)) is None           # the "too many" of a prototype
    float_error = group([("a", 0.3, {"S": 0.1, "T": 0.2}), ("b", 0.3, {"S": 0.1, "T": 0.2})], split_by="Type__c",
                        measure="sum", measure_field="Rate__c")
    assert _left(one("group_records", float_error)) is None   # the "does not add up" of a prototype
    # the split field on a trend is only in the arguments
    item = one("trend_records", _bad_stack_trend(), {"split_by": "Type__c"})
    assert _left(item) == say("split_left", FIELD="Employment Type", WHY=W["split_adds"])
    item = one("trend_records", _bad_stack_trend(), {"split_by": "Not_A_Field__c = 'typed value'"})
    assert _left(item) == say("split_left", FIELD=W["other_field"], WHY=W["split_adds"])
    # one series that adds up says nothing: nothing was left out
    single = group([("a", 3, {"S": 3}), ("b", 2, {"S": 2})], split_by="Type__c")
    item = one("group_records", single)
    assert _left(item) is None and item["data"][0]["S"] == 3 and item["chart"]["stacked"] is False


def test_line_count_zero_from_structure():
    res = trend(months([5, 3, 4], [{"S": 3, "T": 2}, {"S": 3}, {"S": 1, "T": 3}]))
    item = one("trend_records", res, {"split_by": "Type__c"}, "line")
    assert item["chart"]["y_keys"] == ["S", "T"] and "T" not in item["data"][1]   # data stays equal to the result
    svg = svg_of(item)
    for cls in ("f0 s", "f1 s"):
        assert sum(1 for el in nodes(svg) if tag(el) == "circle" and el.attrib["class"] == cls) == 3
    axis = next(el for el in nodes(svg) if tag(el) == "path" and el.attrib["class"] == "a")
    base = float(re.findall(r"[\d.]+", axis.attrib["d"])[1])
    t_points = [el for el in nodes(svg) if tag(el) == "circle" and el.attrib["class"] == "f1 s"]
    assert abs(float(t_points[1].attrib["cy"]) - base) < 0.2        # T16: the one 0 code draws, on the axis


def test_line_count_null_stays_a_gap():
    """T10 and T16: only a series a bucket lacks is 0 on a count line; a null it holds is a gap."""
    res = trend(months([5, 3, 4], [{"S": 3, "T": 2}, {"S": 3, "T": None}, {"S": 1, "T": 3}]))
    item = one("trend_records", res, {"split_by": "Type__c"}, "line")
    assert item["data"][1]["T"] is None
    svg = svg_of(item)
    assert sum(1 for el in nodes(svg) if tag(el) == "circle" and el.attrib["class"] == "f1 s") == 2
    assert sum(1 for el in nodes(svg) if tag(el) == "path" and el.attrib.get("class") == "l1") == 0


def test_line_sum_gap():
    res = trend(months([5, 3, 4], [{"S": 3, "T": 2}, {"S": 3}, {"S": 1, "T": 3}]), measure="sum",
                measure_field="Rate__c")
    item = one("trend_records", res, {"split_by": "Type__c"}, "line")
    assert "T" not in item["data"][1]
    svg = svg_of(item)
    assert sum(1 for el in nodes(svg) if tag(el) == "circle" and el.attrib["class"] == "f1 s") == 2
    assert sum(1 for el in nodes(svg) if tag(el) == "path" and el.attrib.get("class") == "l1") == 0   # a gap


def test_stacked_missing_stays_absent():
    res = trend(months([5, 3, 4], [{"S": 3, "T": 2}, {"S": 3}, {"S": 1, "T": 3}]))
    item = one("trend_records", res)
    assert item["chart"]["stacked"] is True and "T" not in item["data"][1] and item["data"][1]["Total"] == 3
    marks = _bar_marks(svg_of(item))
    assert sum(1 for el in marks if el.attrib["class"] == "f1") == 2   # none for the bucket that lacks it


def _path_numbers(d):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", d)]


def test_chart_equals_rows():
    """T17: every number on the chart is in the rows and every mark's size follows them; T16's point is the one mark
    with no value in the rows."""
    item = one("group_records", group(THREE), None, "horizontal_bar")
    svg = svg_of(item)
    for row in item["data"]:
        assert row["Status"] in texts(svg) and str(row["Records"]) in texts(svg)
    bars = [_path_numbers(el.attrib["d"]) for el in _bar_marks(svg)]
    lengths = [max(p[0::2]) - min(p[0::2]) for p in bars]
    assert len(lengths) == 3
    assert abs(lengths[0] / 12 - lengths[1] / 5) < 0.5 and abs(lengths[0] / 12 - lengths[2] / 2) < 0.5
    stacked = one("group_records", _stack_group(2), None, "horizontal_bar")
    svg = svg_of(stacked)
    assert [row["Total"] for row in stacked["data"]] == [5, 5] and texts(svg).count("5") >= 2
    assert sum(1 for el in _bar_marks(svg) if el.attrib["class"] == "f0") == 2
    line = one("trend_records", trend(months([3, 4, 5])), None, "line")
    svg = svg_of(line)
    assert sum(1 for el in nodes(svg) if tag(el) == "circle") == 3
    assert {"3", "5"} <= set(texts(svg))
    pie = one("group_records", group(THREE), None, "pie")
    assert {str(r["Records"]) for r in pie["data"]} <= set(texts(svg_of(pie)))


# ------------------------------------------------------------------------------------------------ the picture read back
def _tick_value(text):
    """A tick's text as a number: "1.5M" is 1500000."""
    t = text.replace(",", "")
    size = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(t[-1])
    return float(t[:-1]) * size if size else float(t)


def _axis_of(svg, horizontal=False):
    """``(px of 0, px per unit)`` of a chart, from its grid lines and their tick text only. The ticks are read as a
    reader of the picture reads them: each sits on its own grid line, and they are evenly spaced from a first tick of
    0 on that line."""
    els = nodes(svg)
    at = [_path_numbers(el.attrib["d"])[0 if horizontal else 1] for el in els if el.attrib.get("class") == "g"]
    ticks = []
    for p in at:
        if horizontal:
            found = [el for el in els if tag(el) == "text" and el.attrib.get("text-anchor") == "middle"
                     and el.attrib.get("class") == "m" and abs(float(el.attrib["x"]) - p) < 0.11]
        else:
            found = [el for el in els if tag(el) == "text" and el.attrib.get("text-anchor") == "end"
                     and el.attrib.get("class") == "m" and abs(float(el.attrib["y"]) - 4 - p) < 0.11]
        assert len(found) == 1, (p, [el.text for el in found])
        ticks.append((p, _tick_value(found[0].text)))
    assert ticks[0][1] == 0 and len(ticks) >= 2
    per = (ticks[-1][0] - ticks[0][0]) / ticks[-1][1]
    for p, v in ticks:
        assert abs(ticks[0][0] + v * per - p) <= 0.5, ticks
    axis = next(el for el in els if el.attrib.get("class") == "a")
    assert abs(_path_numbers(axis.attrib["d"])[0 if horizontal else 1] - ticks[0][0]) <= 0.2   # the zero line is the axis
    return ticks[0][0], per


def _box(el):
    """``(left, top, right, bottom)`` of a bar or a stack segment."""
    if tag(el) == "rect":
        x, y = float(el.attrib["x"]), float(el.attrib["y"])
        return x, y, x + float(el.attrib["width"]), y + float(el.attrib["height"])
    points = _path_points(el.attrib["d"])
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _fills(item):
    """Series name to its light colour, from the legend: its swatches and their names in order."""
    els = nodes(svg_of(item))
    swatches = [el for el in els if tag(el) == "rect" and el.attrib.get("rx")]
    names = {}
    for sw in swatches:
        name = next(el for el in els if tag(el) == "text"
                    and abs(float(el.attrib["x"]) - float(sw.attrib["x"]) - 15) < 0.2
                    and abs(float(el.attrib["y"]) - float(sw.attrib["y"]) - 10) < 0.2)
        names[name.text] = sw.attrib["fill"]
    return names


HBARS = [("Completed", 412), ("Scheduled", 33), ("Canceled", 120), ("(blank)", 7), ("No Show", 250)]


def test_hbars_read_back_row_by_row():
    """T17 and T3: each row's label on its bar's band, the bar read through the axis, its value at that bar's tip."""
    for rows, measure in ((HBARS, {}), ([("a", 0.25), ("b", 1.75), ("c", 1.5)], {"measure": "avg",
                                                                                  "measure_field": "Rate__c"})):
        item = one("group_records", group(rows, **measure), None, "horizontal_bar")
        svg = svg_of(item)
        zero, per = _axis_of(svg, horizontal=True)
        els = nodes(svg)
        labels = [el for el in els if tag(el) == "text" and el.attrib.get("text-anchor") == "end"
                  and el.attrib.get("class") == "i"]
        assert [el.text for el in labels] == [r[0] for r in rows]
        marks = _bar_marks(svg)
        for label, (name, value) in zip(labels, rows):
            y = float(label.attrib["y"])
            bar = [_box(m) for m in marks if _box(m)[1] <= y - 4 <= _box(m)[3]]
            assert len(bar) == 1, name
            left, _top, right, _bottom = bar[0]
            assert abs(left - zero) <= 0.2 and abs(right - (zero + value * per)) <= 1, (name, value, right)
            text = next(el for el in els if tag(el) == "text" and el is not label and el.attrib.get("class") == "i"
                        and abs(float(el.attrib["y"]) - y) < 0.2)
            assert text.text == charts._fmt(value) and abs(float(text.attrib["x"]) - right - 6) <= 1, name


def test_columns_read_back_row_by_row():
    """T17 and T3: each row's label under its column, the column read through the axis, its value over that column."""
    rows = [("Completed", 412), ("Scheduled", 33), ("Canceled", 120), ("No Show", 250)]
    for item in (one("group_records", group(rows)),
                 one("trend_records", trend(months([r[1] for r in rows])), None, "bar"),
                 one("compare_records", compare(rows))):
        svg = svg_of(item)
        zero, per = _axis_of(svg)
        els = nodes(svg)
        y_key = item["chart"]["y_keys"][0]
        marks = sorted(_bar_marks(svg), key=lambda m: _box(m)[0])
        assert len(marks) == len(rows)
        for mark, row in zip(marks, item["data"]):
            left, top, right, bottom = _box(mark)
            middle = (left + right) / 2
            assert abs(bottom - zero) <= 0.2 and abs(top - (zero + row[y_key] * per)) <= 1, row
            label = next(el for el in els if tag(el) == "text" and el.text == row[item["chart"]["x_key"]])
            assert abs(float(label.attrib["x"]) - middle) <= 1, row
            value = [el for el in els if tag(el) == "text" and el.attrib.get("class") == "i"
                     and el.attrib.get("text-anchor") == "middle" and abs(float(el.attrib["x"]) - middle) <= 1]
            assert [el.text for el in value] == [charts._fmt(row[y_key])], row
            assert abs(float(value[0].attrib["y"]) - (top - 5)) <= 0.2, row


def test_null_rows_read_back_as_no_value():
    item = one("group_records", group([("a", 5), ("b", None), ("c", 3)], total_across_groups=8))
    svg = svg_of(item)
    els = nodes(svg)
    label = next(el for el in els if el.text == "b")
    words = next(el for el in els if el.text == W["no_value"])
    assert abs(float(words.attrib["x"]) - float(label.attrib["x"])) <= 1   # the words stand over their own row


@pytest.mark.parametrize("horizontal", [False, True])
def test_stacks_read_back_row_by_row(horizontal):
    """T17 and T3: each segment read through the axis in its own series' colour, stacked from the axis in order of the
    series, the row's total at the tip. A segment is 2 px shorter than its share: the gap in the surface colour."""
    rows = [("Completed", 90, {"Full": 60, "Part": 30}), ("Scheduled", 40, {"Full": 10, "Part": 30}),
            ("Canceled", 75, {"Full": 25, "Part": 50})]
    asked = "horizontal_bar" if horizontal else None
    item = one("group_records", group(rows), None, asked)
    assert item["chart"]["stacked"] is True and item["chart"]["y_keys"] == ["Part", "Full"]
    svg = svg_of(item)
    zero, per = _axis_of(svg, horizontal)
    fills = _fills(item)
    assert fills == {"Part": charts._LIGHT[0], "Full": charts._LIGHT[1]}
    marks = _bar_marks(svg)
    assert len(marks) == 6
    els = nodes(svg)
    for r, row in enumerate(item["data"]):
        mine = marks[2 * r:2 * r + 2]
        done = 0
        for mark, key in zip(mine, item["chart"]["y_keys"]):
            assert mark.attrib["fill"] == fills[key]
            left, top, right, bottom = _box(mark)
            start, end = ((left - zero) / per, (right - zero) / per) if horizontal else \
                ((bottom - zero) / per, (top - zero) / per)
            assert abs(start - done) * abs(per) <= 0.5, (row, key)
            done += row[key]
            assert abs(end - done) * abs(per) <= 2.5, (row, key)
        tip = (zero + row["Total"] * per)
        last = _box(mine[-1])
        assert abs((last[2] if horizontal else last[1]) - tip) <= 1, row
        total = charts._fmt(row["Total"])
        at = [el for el in els if tag(el) == "text" and el.text == total and el.attrib.get("class") == "i"]
        if horizontal:
            assert any(abs(float(el.attrib["x"]) - tip - 6) <= 1 and last[1] <= float(el.attrib["y"]) - 4 <= last[3]
                       for el in at), row
        else:
            assert any(abs(float(el.attrib["y"]) - (tip - 5)) <= 0.2 and last[0] <= float(el.attrib["x"]) <= last[2]
                       for el in at), row


def test_small_segments_stay_on_their_base():
    """T3: a thin segment stays inside its own share, never under the axis nor over the segment below."""
    rows = [("a", 501, {"S": 1, "T": 500}), ("b", 1001, {"S": 1000, "T": 1})]
    item = one("group_records", group(rows))
    assert item["chart"]["type"] == "bar" and item["chart"]["stacked"] is True
    svg = svg_of(item)
    zero, per = _axis_of(svg)
    for mark in _bar_marks(svg):
        _left, top, _right, bottom = _box(mark)
        assert bottom <= zero + 0.05 and top <= bottom, (top, bottom, zero)
    first = _bar_marks(svg)[0]   # the share of "a" under series S: 1 of an axis of 1,200, well under 1 px
    assert _box(first)[3] - _box(first)[1] <= abs(per) + 0.15
    assert all(_box(m)[3] - _box(m)[1] > 0 for m in _bar_marks(svg))
    # a segment higher in the stack never reaches under the one below it
    by = {"Big": 900, "Thin": 1, "Top": 99}
    item = one("group_records", group([("a", 1000, by), ("b", 1000, by)]))
    assert item["chart"]["y_keys"] == ["Big", "Top", "Thin"]
    marks = _bar_marks(svg_of(item))
    for lower, upper in ((marks[0], marks[1]), (marks[1], marks[2])):
        assert _box(upper)[3] <= _box(lower)[1] + 2.05


def _circles(svg, series=0):
    return sorted((el for el in nodes(svg) if tag(el) == "circle"
                   and el.attrib["class"] in (f"f{series} s", f"h l{series}")), key=lambda el: float(el.attrib["cx"]))


def test_a_line_reads_back_at_every_point():
    """T17 and T3: each point read through the axis under its own period's label; the first, highest and last value each
    beside its own point, and the words of the partial period's mark on that period."""
    values = [3, 9, 4, 7, 5]
    item = one("trend_records", trend(months(values)))
    svg = svg_of(item)
    zero, per = _axis_of(svg)
    els = nodes(svg)
    points = _circles(svg)
    assert len(points) == 5
    for circle, row, value in zip(points, item["data"], values):
        assert abs(float(circle.attrib["cy"]) - (zero + value * per)) <= 1, row
        label = next(el for el in els if tag(el) == "text" and el.text == row["Month"])
        assert abs(float(label.attrib["x"]) - float(circle.attrib["cx"])) <= 0.2, row
    written = {el.text: el for el in els if tag(el) == "text" and el.attrib.get("text-anchor") == "middle"
               and el.attrib.get("class") == "i"}
    assert set(written) == {"3", "9", "5"}   # the first, the highest, the last
    for index, text in ((0, "3"), (1, "9"), (4, "5")):
        circle = points[index]
        assert abs(float(written[text].attrib["x"]) - float(circle.attrib["cx"])) <= charts._width(text) / 2 + 1
        assert abs(float(written[text].attrib["y"]) - float(circle.attrib["cy"])) <= 18.1, text


def test_lines_read_back_per_series():
    """T17 and T3: each series in its own colour on points read through the axis, its last value beside its own last
    point; the axis holds the highest value of every series, not of the first."""
    s_values, t_values = [10, 10, 10, 10], [1, 1, 25, 2]
    by = [{"S": s, "T": t} for s, t in zip(s_values, t_values)]
    res = trend(months([a + b for a, b in zip(s_values, t_values)], by))
    item = one("trend_records", res, {"split_by": "Type__c"}, "line")
    assert item["chart"]["y_keys"] == ["S", "T"]
    svg = svg_of(item)
    zero, per = _axis_of(svg)
    els = nodes(svg)
    top_tick = min(_path_numbers(el.attrib["d"])[1] for el in els if el.attrib.get("class") == "g")
    assert _fills(item) == {"S": charts._LIGHT[0], "T": charts._LIGHT[1]}
    for si, values in enumerate((s_values, t_values)):
        points = _circles(svg, si)
        assert len(points) == 4
        for circle, value in zip(points, values):
            assert circle.attrib["fill"] == charts._LIGHT[si]
            assert abs(float(circle.attrib["cy"]) - (zero + value * per)) <= 1, (si, value)
            assert float(circle.attrib["cy"]) >= top_tick - 0.2, (si, value)   # never over the axis's top tick
        for el in els:
            if tag(el) == "path" and el.attrib.get("class") == f"l{si}":
                assert el.attrib["stroke"] == charts._LIGHT[si]
        last = next(el for el in els if tag(el) == "text" and el.text == str(values[-1])
                    and "text-anchor" not in el.attrib and float(el.attrib["x"]) > float(points[-1].attrib["cx"]))
        assert abs(float(last.attrib["x"]) - float(points[-1].attrib["cx"]) - 7) <= 0.2
        assert abs(float(last.attrib["y"]) - float(points[-1].attrib["cy"])) <= 18, (si, values[-1])


@pytest.mark.parametrize("kind", ["pie", "donut"])
def test_slices_read_back_row_by_row(kind):
    """T17: each slice's angle against its row's value, in the row's colour; the legend's swatch, name and value on one
    line of that row."""
    rows = [("Completed", 50), ("Scheduled", 25), ("Canceled", 15), ("No Show", 10)]
    item = one("group_records", group(rows), None, kind)
    svg = svg_of(item)
    els = nodes(svg)
    slices = [el for el in els if tag(el) == "path" and el.attrib.get("class", "").endswith(" s")]
    assert len(slices) == 4
    for i, (el, (name, value)) in enumerate(zip(slices, rows)):
        assert el.attrib["class"] == f"f{i} s" and el.attrib["fill"] == charts._LIGHT[i]
        n = _path_numbers(el.attrib["d"])
        if kind == "pie":
            cx, cy, x0, y0, x1, y1 = n[0], n[1], n[2], n[3], n[9], n[10]
        else:
            cx, cy = charts._PAD + 112, None
            x0, y0, x1, y1 = n[0], n[1], n[7], n[8]
            cy = float(next(e for e in els if tag(e) == "rect" and e.attrib.get("rx")).attrib["y"]) + 10 - 22 + 112
        angle = (math.atan2(y1 - cy, x1 - cx) - math.atan2(y0 - cy, x0 - cx)) % (2 * math.pi)
        assert abs(angle - 2 * math.pi * value / 100) <= 0.03, (name, angle)
    swatches = [el for el in els if tag(el) == "rect" and el.attrib.get("rx")]
    assert len(swatches) == 4
    for i, (swatch, (name, value)) in enumerate(zip(swatches, rows)):
        assert swatch.attrib["fill"] == charts._LIGHT[i]
        y = float(swatch.attrib["y"]) + 10
        line = {el.text for el in els if tag(el) == "text" and abs(float(el.attrib["y"]) - y) < 0.2}
        assert line == {name, str(value)}, line


def test_rows_keep_every_series():
    many = {f"S{i}": 1 for i in range(11)}
    item = one("group_records", group([("a", 11, many), ("b", 11, many)]))
    assert item["chart"]["stacked"] is False and item["chart"]["y_keys"] == ["Records"]
    assert all(f"S{i}" in row for row in item["data"] for i in range(11)) and item["data"][0]["Records"] == 11
    item = one("group_records", _bad_stack_group())
    assert item["data"][0] == {"Status": "G0", "Records": 9, "Full": 3, "Part": 2}
    item = one("trend_records", _avg_trend())
    assert item["data"][0]["Full"] == 3.5 and item["data"][0]["Average of Bill Rate"] == 3.0


def test_same_series_name_no_overwrite():
    res = group([("a", 5, {"mail a@example.invalid": 2, "mail b@example.invalid": 3}),
                 ("b", 5, {"mail a@example.invalid": 1, "mail b@example.invalid": 4})], split_by="Type__c")
    item = one("group_records", res)
    assert item["chart"]["stacked"] is False and item["data"][0] == {"Status": "a", "Records": 5}
    # T23 and T18: the rows hold no series here, and the sentence does not say they do
    assert _left(item) == say("split_same", FIELD="Employment Type") == (
        "The split by Employment Type is left out: two of its names read the same once cleaned.")
    assert all(set(row) == {"Status", "Records"} for row in item["data"]) and "in the rows" not in _left(item)
    assert _left(one("group_records", {**_bad_stack_group(), "split_by": "Type__c"})).endswith(
        "is in the rows, not on the chart: its values do not add up to each row's total.")


@pytest.mark.parametrize("name,key", [("Total", "Total (split)"), ("__proto__", "__proto__ (split)"),
                                      ("Status", "Status (split)"), ("Records", "Records (split)")])
def test_series_named_row_key_renamed(name, key):
    res = group([("a", 5, {name: 2, "T": 3}), ("b", 4, {name: 1, "T": 3})])
    item = one("group_records", res)
    assert item["chart"]["y_keys"] == ["T", key] and item["data"][0]["Total"] == 5 and item["data"][0][key] == 2
    assert item["data"][0]["Status"] == "a"
    assert all(k in item["data"][0] for k in item["chart"]["y_keys"])


def test_field_named_total_and_unit_named_as_x():
    fields = {"Total__c": NS(api="Total__c", label="Total", kind="text", ref_targets=[]),
              "Records__c": NS(api="Records__c", label="Records", kind="text", ref_targets=[])}
    catalog = NS(objects={"Interview__c": NS(api="Interview__c", label="Interview", plural_label="Interviews",
                                             fields=fields)})
    res = group([("a", 5, {"S": 2, "T": 3}), ("b", 4, {"S": 1, "T": 3})], grouped_by="Total__c")
    item = one("group_records", res, catalog=catalog)
    assert item["chart"]["x_key"] == "Total" and "Total (split)" in item["data"][0] and item["data"][0]["Total"] == "a"
    item = one("group_records", group(THREE, grouped_by="Records__c"), catalog=catalog)
    assert item["chart"]["x_key"] == "Records" and item["chart"]["y_keys"] == ["Records (value)"]


def test_negative_row_refused():
    assert one("group_records", group([("a", 3), ("b", -1)]))["reason"] == W["negative"]
    assert one("trend_records", trend(months([3, -1])))["reason"] == W["negative"]
    assert one("compare_records", compare([("a", 3), ("b", -0.5)]))["reason"] == W["negative"]


def test_negative_segment_refused():
    item = one("group_records", group([("a", 3, {"S": 4, "T": -1}), ("b", 3, {"S": 3})]))
    assert item["chart"] is None and item["reason"] == W["negative"] and len(item["data"]) == 2


# ------------------------------------------------------------------------------------------------ words
@pytest.mark.parametrize("measure,word", [("count", "Records"), ("sum", "Sum of Bill Rate"),
                                          ("avg", "Average of Bill Rate"), ("min", "Lowest Bill Rate"),
                                          ("max", "Highest Bill Rate"),
                                          ("count_distinct", "Distinct Bill Rate")])
def test_unit_word_each_measure(measure, word):
    res = group(THREE, measure=measure, **({} if measure == "count" else {"measure_field": "Rate__c"}))
    item = one("group_records", res)
    assert item["chart"]["y_keys"] == [word] and item["data"][0][word] == 12
    if measure != "count":
        assert item["chart"]["title"] == f"Interviews: {word} by Status"
    else:
        assert item["chart"]["title"] == "Interviews by Status"
    assert "Total" not in word


def test_compare_measure_from_arguments():
    res = compare([("a", 4.5), ("b", 7.5)])
    item = one("compare_records", res, {"measure": "avg", "measure_field": "Rate__c"})
    assert item["chart"]["y_keys"] == ["Average of Bill Rate"]
    assert item["chart"]["title"] == "Interviews: Average of Bill Rate compared"
    assert one("compare_records", res)["chart"]["y_keys"] == ["Records"]
    typed = one("compare_records", res, {"measure": "sum", "measure_field": "Not_There__c"})
    assert typed["chart"]["y_keys"] == [f"Sum of {W['other_field']}"]
    for args in ({"measure": "sum"}, {"measure": "sum", "measure_field": "Status__c = 'typed value'"}):
        missing = one("compare_records", res, args)
        assert missing["chart"] is None and missing["reason"] == W["fault"] and "typed" not in json.dumps(missing)


@pytest.mark.parametrize("typed,word", [("average", "Average of Bill Rate"), ("Sum", "Sum of Bill Rate"),
                                        ("mean", "Average of Bill Rate"), (" AVG ", "Average of Bill Rate"),
                                        ("total", "Sum of Bill Rate"), ("minimum", "Lowest Bill Rate")])
def test_measure_read_as_the_tool_reads_it(typed, word):
    res = compare([("a", 4), ("b", 7)])
    assert one("compare_records", res, {"measure": typed, "measure_field": "Rate__c"})["chart"]["y_keys"] == [word]


def test_unknown_measure_is_a_fault_never_records():
    res = compare([("a", 4), ("b", 7)])
    item = one("compare_records", res, {"measure": "weird", "measure_field": "Rate__c"})
    assert item["chart"] is None and item["reason"] == W["fault"] and "Records" not in json.dumps(item)


def test_title_metadata_only():
    assert one("group_records", group(THREE))["chart"]["title"] == "Interviews by Status"
    assert one("trend_records", trend(months([3, 4])))["chart"]["title"] == "Interviews per month"
    assert one("trend_records", trend(months([3, 4])))["chart"]["x_key"] == "Month"
    assert one("trend_records", trend(months([3, 4]), grain="week"))["chart"]["x_key"] == "Week"
    assert one("compare_records", compare([("a", 4), ("b", 7)]))["chart"]["title"] == "Interviews compared"
    split = breakdown({"Status__c": {"A": 3, "B": 1}}, 4)
    assert one("count_records", split)["chart"]["title"] == "Interviews by Status"
    # no plural label: the label with a plural ending; no label: the API name in words; no catalog: in words
    note = group(THREE, object="Note__c")
    assert one("group_records", note)["chart"]["title"].startswith("Notes by ")
    spec = lambda label, plural="": types.SimpleNamespace(label=label, plural_label=plural)
    labels = charts._Labels(types.SimpleNamespace(objects={"Lead": spec("Lead"), "Opportunity": spec("Opportunity"),
                                                           "Address__c": spec("Address"), "Day__c": spec("Day"),
                                                           "Watch__c": spec("Code Watch"),
                                                           "Marketing__c": spec("Marketing", "Marketing")}))
    assert [labels.plural(o) for o in ("Lead", "Opportunity", "Address__c", "Day__c", "Watch__c", "Marketing__c")] == [
        "Leads", "Opportunities", "Address", "Days", "Code Watches", "Marketing"]
    plain = group(THREE, object="Plain__c")
    assert one("group_records", plain)["chart"]["title"].startswith("Plain by ")
    assert one("group_records", group(THREE), catalog=None)["chart"]["title"] == "Interview by Status"
    named = group(THREE, grouped_by="Recruiter__c (Recruiter name)")
    assert one("group_records", named)["chart"]["x_key"] == "Recruiter"
    # the question's words and the answer never reach a title
    item = one("group_records", group([("Candidate 1", 3), ("Candidate 2", 1)]), question="bar chart of Sentinel Word")
    assert "Sentinel" not in item["chart"]["title"] and "Candidate" not in item["chart"]["title"]


def test_subtitle_filters_any_of_related():
    applied = {"period": _span("2026-01-01", "2026-06-30", "2026-07-01")["period"],
               "filters": ["Status__c = 'Person Value'", "Round__c in ('Secret')"],
               "any_of": ["Type__c = 'Other Value'"],
               "related": ["has a Session__c with Name = 'Typed' (by Session__c)", "has no Note__c",
                           "has at least 2 Session__c", "has a Missing__c", "free text"]}
    item = one("group_records", group(THREE, applied=applied))
    assert item["subtitle"][0] == "Period: 2026-01-01 to 2026-06-30 (by Date of Interview)"
    assert item["subtitle"][1] == ("Filtered on: Status, Round, Employment Type, related Session records, "
                                   "related Note records, " + W["other_field"])
    for word in ("Person Value", "Secret", "Other Value", "Typed", "typed words", "__c"):
        assert word not in json.dumps(item["subtitle"])
    def period(start, end):
        applied = {"period": {"date_field": "Date__c", "from": start, "to": end}}
        return one("group_records", group(THREE, applied=applied))

    assert period("2026-01-01", None)["subtitle"][0] == "Period: from 2026-01-01 (by Date of Interview)"
    assert period(None, "2026-01-31")["subtitle"][0] == "Period: to 2026-01-31 (by Date of Interview)"
    assert not any(s.startswith("Period") for s in period("typed words", None)["subtitle"])
    # no silent cut: every filter stands and a long line wraps in the SVG
    long = {"filters": [f"{k} = 'x'" for k in FIELDS]}
    item = one("group_records", group(THREE, applied=long))
    assert all(v in item["subtitle"][0] for v in FIELDS.values())


def test_subtitle_lookup_name_filters_stay_apart():
    """T21: a person's filter reads as its lookup. Two lookups are two words, and one field filtered twice is one."""
    item = one("group_records", group(THREE, applied={"filters": ["Recruiter__c.Name = 'Person A'",
                                                                  "Type__c.Name = 'Person B'"]}))
    assert item["subtitle"][0] == "Filtered on: Recruiter, Employment Type"
    item = one("group_records", group(THREE, applied={"filters": ["Recruiter__c.Name = 'Person A'"]}))
    assert item["subtitle"][0] == "Filtered on: Recruiter" and "Person" not in json.dumps(item["subtitle"])
    # the contains form of the same filter reads the same, and is the same field
    item = one("group_records", group(THREE, applied={"filters": ["Recruiter__c.Name = 'Person A'",
                                                                  "Recruiter__c name contains 'Person'"]}))
    assert item["subtitle"][0] == "Filtered on: Recruiter"
    # two different fields that share a label are two words: neither filter is hidden behind the other
    fields = {"Status__c": NS(api="Status__c", label="Status", kind="text", ref_targets=[]),
              "Status2__c": NS(api="Status2__c", label="Status", kind="text", ref_targets=[])}
    catalog = NS(objects={"Interview__c": NS(api="Interview__c", label="Interview", plural_label="Interviews",
                                             fields=fields)})
    item = one("group_records", group(THREE, applied={"filters": ["Status__c = 'x'", "Status2__c = 'y'"]}),
               catalog=catalog)
    assert item["subtitle"][0] == "Filtered on: Status, Status"


def test_subtitle_compare_segment_fields():
    segments = [("a", 4, {"filters": ["Status__c = 'Completed'"], **_span("2026-01-01", "2026-01-31", "2026-02-01")}),
                ("b", 7, {"filters": ["Round__c = 'Second'"]})]
    item = one("compare_records", compare(segments, applied={"filters": ["Type__c = 'Full'"]}))
    assert item["subtitle"] == ["Periods by Date of Interview", "Filtered on: Employment Type, Status, Round"]
    assert not any(s.startswith(W["total"]) for s in item["subtitle"])


def test_subtitle_unknown_token_fixed_word():
    unknown = {"filters": ["Not_A_Field__c = 'Person Value'", "'quoted' = 1", "", 5]}
    item = one("group_records", group(THREE, applied=unknown))
    assert item["subtitle"][0] == f"Filtered on: {W['other_field']}" and "Person" not in json.dumps(item)
    # no catalog: a filter text cannot be checked, so the fixed word
    known = {"filters": ["Status__c = 'x'"], "related": ["has a Session__c"]}
    item = one("group_records", group(THREE, applied=known), catalog=None)
    assert item["subtitle"][0] == f"Filtered on: {W['other_field']}"


def test_no_api_name_on_chart():
    res = group([("Done", 3), ("Open", 2)], measure="sum", measure_field="Rate__c", blank_group_value=2, group_count=3,
                split_by="Type__c", applied={"filters": ["Status__c = 'x'"], "period": _span("2026-01-01")["period"]})
    for catalog in (CATALOG, None):
        item = one("group_records", res, catalog=catalog, today=TODAY)
        words = [item["chart"]["title"], item["chart"]["x_key"], *item["chart"]["y_keys"], *item["subtitle"],
                 *item["notes"]]
        assert "__" not in " ".join(words) and "Rate__c" not in svg_of(item)


def test_field_tools_never_drawn():
    values = {"query_id": "q1", "field": "Status__c",
              "values": [{"value": "a", "records": 3}, {"value": "b", "records": 1}]}
    for tool in ("get_field_values", "get_field_history"):
        assert one(tool, values)["chart"] is None and one(tool, values)["data"] == []
    final, items, lines, _e = make(values, tool="get_field_values")
    assert items == [] and lines == [W["field_values"]] and final == "There are 5."


def test_two_field_breakdown_two_items():
    res = breakdown({"Status__c": {"A": 3, "B": 1}, "Type__c": {"C": 2, "D": 2}}, 4)
    _final, items, lines, _err = make(res, tool="count_records")
    assert [i["chart"]["x_key"] for i in items] == ["Status", "Employment Type"] and lines == []
    assert all(i["svg_b64"] for i in items)
    res = breakdown({"Status__c": {"A": 3, "B": 1}, "Type__c": {"C": 4}}, 4)
    assert [i["chart"]["x_key"] for i in make(res, tool="count_records")[1]] == ["Status"]


def test_pick_last_first():
    first, second = group(THREE, query_id="q1"), group([("x", 3), ("y", 4)], query_id="q2")
    number = evidence("count_records", {"query_id": "q3", "count": 7})
    listed = evidence("list_records", {"query_id": "q4", "rows": []})
    rec = rec_of(evidence("group_records", first), evidence("group_records", second), number, listed)
    final, items, lines, _err = make(rec=rec)
    assert [i["query_id"] for i in items] == ["q2"] and lines == []   # a later count or list does not hide it
    # no chartable result: the reason of the last data result
    rec = rec_of(evidence("group_records", group(SINGLE_ROW, query_id="q1")), listed)
    assert make(rec=rec)[1:3] == ([], [W["list"]])
    rec = rec_of(listed, evidence("group_records", group(SINGLE_ROW, query_id="q1")))
    _final, items, lines, _err = make(rec=rec)
    assert lines == [W["one_row"]] and [len(i["data"]) for i in items] == [1]


def test_pick_of_no_results():
    """An answer with no successful data call: ``make`` returns before it picks, and section 18's replay still calls
    ``_pick``, with a gate of 0 exceptions."""
    assert charts._pick([]) == []


def test_several_note():
    first, second = group(THREE, query_id="q1"), group([("x", 3), ("y", 4)], query_id="q2")
    rec = rec_of(evidence("group_records", first), evidence("group_records", second))
    item = make(rec=rec)[1][0]
    assert say("several", COUNT="2") in item["notes"]
    assert say("several", COUNT="2") in said(base64.b64decode(item["svg_b64"]).decode("utf-8"))
    assert not any("could be drawn" in n for n in make(first)[1][0]["notes"])
    # a two-field breakdown is one result
    two = breakdown({"Status__c": {"A": 3, "B": 1}, "Type__c": {"C": 2, "D": 2}}, 4)
    assert not any("could be drawn" in n for i in make(two, tool="count_records")[1] for n in i["notes"])
    # a result that cannot be drawn among them is not counted
    listed = evidence("list_records", {"query_id": "q3", "rows": []})
    item = make(rec=rec_of(evidence("group_records", first), listed, evidence("group_records", second)))[1][0]
    assert say("several", COUNT="2") in item["notes"] and say("several", COUNT="3") not in item["notes"]


# ------------------------------------------------------------------------------------------------ labels
def test_label_cut_keeps_hash():
    long = "Recruiter With A Very Long Synthetic Name (#2)"
    assert charts._cut(long, 28).endswith("… (#2)") and len(charts._cut(long, 28)) == 28
    assert charts._cut("Short (#2)", 28) == "Short (#2)"
    assert len(charts._cut("x" * 40, 28)) == 28 and charts._cut("x" * 40, 28).endswith("…")
    item = one("group_records", group([(long, 3), ("Other Recruiter With A Long Name (#3)", 1)]))
    assert long in [r["Status"] for r in item["data"]]     # the rows keep the whole label
    assert charts._cut(long, 28) in texts(svg_of(item))


def test_label_collision_refused():
    a, b = "Same first twenty chars and one word and the same end", "Same first twenty chars and two word and the same end"
    assert one("group_records", group([(a, 3), (b, 1)]))["reason"] == W["same_labels"]
    # a common start alone leaves the chart, in the middle cut: the model test's "... - Week 1" and "... - Week 2"
    a, b = "Sample Course Track One - V1 - Week 1", "Sample Course Track One - V1 - Week 2"
    item = one("group_records", group([(a, 4), (b, 2), ("Intake", 1)]))
    assert item["chart"] is not None and set(charts._shown_all([a, b, "Intake"])) <= set(texts(svg_of(item)))
    assert charts._shown_all([a, b, "Intake"]) == [charts._keep_end(a, 28), charts._keep_end(b, 28), "Intake"]
    # labels the end cut tells apart keep it: a shared end must not lose them (review of the model-test fixes)
    a, b = "Sample Course Track One V1 - Week 1", "Sample Course Track One V2 - Week 1"
    assert one("group_records", group([(a, 3), (b, 1)]))["chart"] is not None
    assert charts._shown_all([a, b]) == [charts._cut(a, 28), charts._cut(b, 28)]
    item = one("group_records", group([("Interview Round Two - Final", 3), ("Interview Round One - Final", 1)]), None, "pie")
    assert item["chart"] is not None
    # after clean too
    res = group([("mail a@example.invalid", 3), ("mail b@example.invalid", 1)])
    assert one("group_records", res)["reason"] == W["same_labels"]
    assert one("group_records", res, question="what is the e-mail address")["chart"] is not None
    # a legend's shorter cut too: a pie and a split's series, as a bar chart's 28 characters still tell them apart
    a, b = "Twenty-two characters a", "Twenty-two characters b"
    item = one("group_records", group([(a, 3), (b, 1)]), None, "pie")
    assert item["chart"] is not None and set(charts._fit_all([a, b], 22)) <= set(texts(svg_of(item)))
    a, b = "Twenty-two characters a and end", "Twenty-two characters b and end"
    assert one("group_records", group([(a, 3), (b, 1)]), None, "horizontal_bar")["chart"] is not None
    item = one("group_records", group([(a, 3), (b, 1)]), None, "pie")
    assert item["chart"] is None and item["reason"] == W["same_labels"] and len(item["data"]) == 2
    item = one("group_records", group([("x", 4, {a: 3, b: 1}), ("y", 4, {a: 1, b: 3})]))
    assert item["chart"] is None and item["reason"] == W["same_labels"] and item["data"][0][a] == 3


def test_every_group_label_on_the_axis():
    """Periods can skip an axis label; a group or a segment never does."""
    labels = [f"Group label {i:02d}xx" for i in range(8)]
    item = one("group_records", group([(lb, 40 - i) for i, lb in enumerate(labels)]))
    assert item["chart"]["type"] == "bar"
    svg = svg_of(item)
    assert set(labels) <= set(texts(svg))
    at = {el.text: float(el.attrib["y"]) for el in nodes(svg) if tag(el) == "text"}
    assert at[labels[0]] != at[labels[1]] and at[labels[0]] == at[labels[2]]   # wide labels take turns on lines
    item = one("group_records", group(THREE))
    at = {el.text: float(el.attrib["y"]) for el in nodes(svg_of(item)) if tag(el) == "text"}
    assert at["Completed"] == at["Scheduled"]


def test_group_labels_on_one_line_never_touch():
    """Wide labels take turns on the lines they need; on one line none runs into the next, also where one moved in to
    stay inside the image."""
    labels = [c * 16 for c in "WMQGDOAU"]
    item = one("group_records", group([(lb, 40 - i) for i, lb in enumerate(labels)]))
    assert item["chart"]["type"] == "bar"
    svg = svg_of(item)
    lines = {}
    for el in nodes(svg):
        if el.text in labels:
            half = charts._width(el.text, 12) / 2
            lines.setdefault(el.attrib["y"], []).append((float(el.attrib["x"]) - half, float(el.attrib["x"]) + half))
    assert sum(len(v) for v in lines.values()) == 8 and len(lines) >= 3
    for spans in lines.values():
        spans.sort()
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_week_label_axis_only():
    item = one("trend_records", trend([("week of 2026-01-05", 3), ("week of 2026-01-12", 4)], grain="week"))
    assert item["data"][0]["Week"] == "week of 2026-01-05"
    svg = svg_of(item)
    assert "2026-01-05" in texts(svg) and "week of" not in said(svg)


# ------------------------------------------------------------------------------------------------ privacy
def test_contact_in_each_label_kind():
    mail = "reach a@example.invalid now"
    results = [("group label", "group_records", group([(mail, 3), ("Other", 1)])),
               ("by key", "group_records", group([("a", 4, {mail: 3, "T": 1}), ("b", 2, {mail: 1, "T": 1})])),
               ("breakdown key", "count_records", breakdown({"Status__c": {mail: 3, "Other": 1}}, 4)),
               ("compare label", "compare_records", compare([(mail, 3), ("Other", 1)]))]
    for name, tool, res in results:
        hidden = one(tool, res, asked=None, question="bar chart by status")
        assert "a@example.invalid" not in json.dumps(hidden) and EMAIL_SHOWN in json.dumps(hidden), name
        assert "a@example.invalid" not in svg_of(hidden), name
        shown = one(tool, res, asked=None, question="bar chart of the e-mail address by status")
        assert "a@example.invalid" in json.dumps(shown), name


def test_cleaner_order_email_at_190():
    label = "x" * 190 + " a@example.invalid and more words after it"
    clean = charts._Cleaner("bar chart", [])
    out = clean(label)
    assert "a@example.invalid" not in out and "example" not in out and EMAIL_SHOWN[:5] in out
    assert len(out) <= 200
    secret = clean("token=abc123 password: hunter2")
    assert "abc123" not in secret and "hunter2" not in secret and "[hidden]" in secret
    assert clean(None) == "(blank)" and clean("  two   spaces ") == "two spaces"


def test_cleaner_a_cut_at_a_space_leaves_none_at_the_end():
    clean = charts._Cleaner("bar chart", [])
    assert clean("x" * 199 + " and more words") == "x" * 199
    assert clean("x" * 200 + " and more words") == "x" * 200


def test_a_series_name_cut_at_a_space_is_the_same_name_in_the_chart_and_the_rows():
    """The caller's parser strips the names of a chart and looks them up in the columns: a name that keeps its space is
    not found there, and the chart is lost with no reason."""
    name = "x" * 199 + " and more words"
    item = one("group_records", group([("a", 4, {name: 3, "T": 1}), ("b", 2, {name: 1, "T": 1})]))
    assert "x" * 199 in item["chart"]["y_keys"]
    assert all(key == key.strip() for key in item["chart"]["y_keys"])
    assert all(key in row for key in item["chart"]["y_keys"] for row in item["data"])


def test_phone_only_group_label_kept_by_key_dropped():
    phone = "9876543210"
    res = group([(phone, 3), ("Other", 1)])
    assert one("group_records", res)["data"][0]["Status"] == phone     # a whole value of the result: the answer's rule
    res = group([("a", 4, {phone: 3, "T": 1}), ("b", 2, {phone: 1, "T": 1})])
    item = one("group_records", res)
    assert phone not in json.dumps(item) and PHONE_SHOWN in item["chart"]["y_keys"]


def test_meeting_id_kept():
    meeting = "8123456789"
    clean = charts._Cleaner("bar chart", [group([(meeting, 3), ("Other", 1)]), {"meeting": {"id": meeting}}])
    assert clean(meeting) == meeting and clean(f"Meeting {meeting}") == f"Meeting {meeting}"
    # a number no result holds whole is replaced
    assert "7123456789" not in charts._Cleaner("bar chart", [])("7123456789")
    # the keep set is every ok result of the answer, as the record session reads it
    rec = rec_of(evidence("list_records", {"query_id": "q1", "rows": [{"Id": meeting}]}),
                 evidence("group_records", group([("a", 4, {meeting: 3, "T": 1}), ("b", 2, {meeting: 1, "T": 1})])))
    items = make(rec=rec)[1]
    assert meeting in items[0]["chart"]["y_keys"]


def test_hidden_kept_all_hidden_refused():
    item = one("group_records", group([("[hidden]", 3), ("Open", 1)]))
    assert item["data"][0]["Status"] == "[hidden]" and "[hidden]" in texts(svg_of(item))
    item = one("group_records", group([("[hidden]", 3), ("[hidden] two", 1)]))
    assert item["chart"] is None and item["reason"] == W["hidden"] and len(item["data"]) == 2


def test_about_people_never_drawn():
    people = {"query_id": "q1", "object": "Interview__c",
              "groups": [{"group": "Person A", "value": 3}, {"group": "Person B", "value": 1}]}
    for tool in ("about_people", "find_references"):
        final, items, lines, _e = make(people, tool=tool)
        assert items == [] and lines == [W["people"]] and "Person" not in json.dumps(items)


def test_compare_label_digit_replaced():
    span = _span("2026-10-01", "2026-10-31", "2026-11-01")
    res = compare([("Q4 2031", 8, span), ("this month", 5, {})])
    item = one("compare_records", res)
    assert [r["Compared"] for r in item["data"]] == ["2026-10-01 to 2026-10-31", "this month"]
    # the digits stand in the question or in the segment's own applied block
    assert one("compare_records", res, question="chart of Q4 2031 vs this month")["data"][0]["Compared"] == "Q4 2031"
    filtered = compare([("Round 2", 8, {"filters": ["Round__c = 2"]}), ("Round 3", 5, {"filters": ["Round__c = 3"]})])
    assert [r["Compared"] for r in one("compare_records", filtered)["data"]] == ["Round 2", "Round 3"]
    # no period: Segment N
    res = compare([("Typed 77", 8), ("Typed 88", 5)])
    assert [r["Compared"] for r in one("compare_records", res)["data"]] == ["Segment 1", "Segment 2"]
    # one date only
    res = compare([("Typed 77", 8, _span("2026-10-01")), ("plain", 5)])
    assert one("compare_records", res)["data"][0]["Compared"] == "2026-10-01"
    # the replaced label is the one in the partial note
    res = compare([("Typed 77", 8, _span("2026-11-01", "2026-11-30", "2026-12-01")), ("plain", 5)])
    assert say("partial", LABELS="2026-11-01 to 2026-11-30") in one("compare_records", res, today=TODAY)["notes"]


# ------------------------------------------------------------------------------------------------ the SVG
HOSTILE = ["<script>alert(1)</script>", "a & b < c > d", '"quoted" \'single\'', "ctrl\x01\x0b\x1fchars",
           "lone \ud800 surrogate", "</svg><svg onload=alert(1)>", "]]> <![CDATA[", "<!-- comment -->", "&amp;&#x26;",
           "<?xml version='1.0'?>", "url(http://example.invalid)", "<a href='javascript:alert(1)'>x</a>"]


def _hostile_items():
    labels = [(s, 20 - i) for i, s in enumerate(HOSTILE)]
    by = {s: 1 for s in HOSTILE[:6]}
    yield one("group_records", group(labels))
    yield one("group_records", group(labels[:8]), None, "pie")
    yield one("group_records", group(labels[:6]), None, "horizontal_bar")
    yield one("compare_records", compare(labels[:4]))
    yield one("group_records", group([("a", 6, by), ("b", 6, by)]))
    yield one("trend_records", trend(months([6, 6], [by, by])), {"split_by": "Type__c"}, "line")


def test_svg_hostile_labels_parse():
    seen = 0
    for item in _hostile_items():
        svg = svg_of(item)
        root = ET.fromstring(svg)
        assert tag(root) == "svg" and all(tag(el) in charts._TAGS for el in root.iter())
        assert "<script" not in svg and "<a " not in svg and "onload=" not in svg.split(">", 1)[0]
        seen += 1
    assert seen == 6
    # text stays text: a node reads back as the label with only what XML forbids removed
    item = one("group_records", group([("a & <b>", 3), ("c", 1)]), None, "horizontal_bar")
    assert "a & <b>" in texts(svg_of(item))


def test_every_hostile_item_encodes():
    """A lone surrogate never costs the answer: the page's JSON reply and the command line's print encode the item, and
    its rows, keys, notes and alt match its SVG."""
    for item in _hostile_items():
        json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
        assert not re.search("[\ud800-\udfff]", json.dumps(item, ensure_ascii=False, default=str))
    half = "Team \ud83d"
    for tool, res in (("compare_records", compare([(half + " full", 3, _span("2026-11-01", "2026-11-30", "2026-12-01")),
                                                   ("Plain", 2)])),
                      ("group_records", group([(half + " full", 3), ("Plain", 2)])),
                      ("group_records", group([("a", 3, {half: 2, "T": 1}), ("b", 2, {half: 1, "T": 1})])),
                      ("trend_records", trend(months([2, 3], [{half: 1, "T": 1}, {half: 1, "T": 2}]))),
                      ("count_records", breakdown({"Status__c": {half: 3, "Plain": 1}}, 4))):
        _final, items, _lines, error = make(res, tool=tool, today=TODAY)
        assert items and items[0]["chart"] and error == "", tool
        for item in items:
            assert "Team" in json.dumps(item, ensure_ascii=False).encode("utf-8").decode("utf-8")
            "\n".join(charts.text_lines([item])).encode("utf-8")
        out = charts.for_tool(tool, res, {}, CATALOG, TODAY)
        json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert charts._Cleaner("bar chart", [])("\ud800") == "(blank)"
    assert charts._Cleaner("bar chart", [])("esc\x1b[0m") == "esc[0m"   # nor a control character in the printed rows


def _all_shapes():
    span = _span("2026-11-01", "2026-11-30", "2026-12-01")
    by8 = {f"Series {i}": i + 1 for i in range(8)}
    long = "A long synthetic label over twenty-eight characters (#2)"
    many_notes = {"blank_group_value": 3, "unresolved_ids": 2, "ties_at_cutoff_count": 2, "group_count": 40,
                  "records_matched": 90, "split_by": "Type__c",
                  "applied": {"filters": [f"{k} = 'x'" for k in FIELDS] * 3, **span}}
    return [
        one("group_records", group(THREE)),
        one("group_records", group([("Done", 1234567), ("Open", 7)])),
        one("group_records", group([(f"Recruiter {i}", 1000 - i * 7) for i in range(30)]), None, "horizontal_bar"),
        one("group_records", group([(long, 5), ("Short", 2), ("(blank)", None)], total_across_groups=7, **many_notes),
            today=TODAY),
        one("trend_records", trend(months([3 + i % 5 for i in range(12)]),
                                   notes=[PART_RUN.replace("2026-03", "2026-12")])),
        one("trend_records", trend(months([3 + i % 7 for i in range(120)]))),
        one("trend_records", trend(months([3, None, 5, 6, None, None, 8]), measure="avg", measure_field="Rate__c")),
        # a rate per month (placement fixes, 2026-11-16): percent, with months that have no base record
        one("trend_records", trend(months([12.5, None, 100.0, 0.0, 33.3, None, 7.1, 66.7, 50.0, 4.2]),
                                   measure="percent", part_total=9, base_total=40, percent_overall=22.5)),
        one("trend_records", trend(months([12.5, None, 100.0, 0.0, 33.3]), measure="percent"), None, "bar"),
        one("trend_records", trend([(f"week of 2026-{m:02d}-01", 3 * m) for m in range(1, 13)], grain="week"), None,
            "bar"),
        one("trend_records", trend(months([100000 + i * 5000 for i in range(30)])), None, "bar"),
        one("group_records", group([(f"Group label {i:02d}xx", 40 - i) for i in range(8)])),
        one("group_records", group([(f"G{i}", i + 1) for i in range(8)]), None, "pie"),
        one("group_records", group([("A", 3), ("B", 2), ("C", 1)]), None, "donut"),
        one("group_records", group([("A", 3), ("B", 0)]), None, "pie"),
        one("group_records", group([("A", 3), ("B", 0)]), None, "donut"),
        one("group_records", group([("A", 999999), ("B", 1)]), None, "pie"),
        one("trend_records", trend(months([36] * 6, [by8] * 6))),
        one("group_records", group([(f"Group {i}", 36, by8) for i in range(4)])),
        one("group_records", group([(f"Group {i}", 36, by8) for i in range(12)])),
        one("group_records", group([(long, 36, by8)] + [(f"Group {i}", 36, by8) for i in range(2)]), None,
            "horizontal_bar"),
        one("trend_records", trend(months([36] * 12, [by8] * 12)), {"split_by": "Type__c"}, "line"),
        one("compare_records", compare([("this month", 5, _span("2026-11-01", "2026-11-30", "2026-12-01")),
                                        ("last month", 8, {}), ("a long compare label over sixteen", 3), ("d", 2)]),
            today=TODAY),
        one("group_records", group([("x", 0.25), ("y", 0.5), ("z", 1.75)], measure="avg", measure_field="Rate__c")),
        # the widest text the cuts let through
        one("group_records", group([(f"{c * 28}", 9 - i) for i, c in enumerate("WMQ")]), None, "horizontal_bar"),
        one("group_records", group([(f"{c * 16}", 9 - i) for i, c in enumerate("WMQGDOAU")])),
        one("group_records", group([(f"{c * 22}", 9 - i) for i, c in enumerate("WMQ")]), None, "pie"),
        one("trend_records", trend(months([36] * 6, [{c * 22: 12 for c in "WMQ"}] * 6))),
        one("group_records", group(THREE, grouped_by="Field_" + "M" * 90, measure="sum", measure_field="Rate__c",
                                   applied={"filters": [f"{k} = 'x'" for k in FIELDS] * 3})),
        one("trend_records", trend(months([1234567890.25, 2345678901.5, 45.25]), measure="sum",
                                   measure_field="Rate__c")),
    ]


_POINT_RE = re.compile(r"([MLQAZ])([^MLQAZ]*)")


def _path_points(d):
    """Every point of an absolute path that holds only M L Q A Z."""
    assert re.fullmatch(r"(?:[MLQAZ][-\d. ]*)+", d), d
    points = []
    for cmd, rest in _POINT_RE.findall(d):
        nums = [float(n) for n in rest.split()]
        if cmd in "ML":
            assert len(nums) == 2, d
            points.append(tuple(nums))
        elif cmd == "Q":
            assert len(nums) == 4, d
            points += [tuple(nums[:2]), tuple(nums[2:])]
        elif cmd == "A":
            assert len(nums) == 7, d
            points.append(tuple(nums[5:]))
    return points


def test_svg_marks_inside_viewbox():
    """T3's layout half, on all shapes: a small parser reads every position and path point. It lives here only."""
    for item in _all_shapes():
        svg = svg_of(item)
        root = ET.fromstring(svg)
        w, h = float(root.attrib["width"]), float(root.attrib["height"])
        assert root.attrib["viewBox"] == f"0 0 {int(w)} {int(h)}" and w == 640
        for el in root.iter():
            name, a = tag(el), el.attrib
            found = []
            if name == "rect":
                found = [(float(a["x"]), float(a["y"])),
                         (float(a["x"]) + float(a["width"]), float(a["y"]) + float(a["height"]))]
            elif name == "circle":
                r = float(a["r"]) + float(a.get("stroke-width", 0)) / 2
                found = [(float(a["cx"]) - r, float(a["cy"]) - r), (float(a["cx"]) + r, float(a["cy"]) + r)]
            elif name == "path":
                found = _path_points(a["d"])
            elif name == "text":
                found = [(float(a["x"]), float(a["y"]))]
                wide = charts._width(el.text or "", int(a.get("font-size", 13)))
                anchor = a.get("text-anchor")
                left = float(a["x"]) - (wide if anchor == "end" else wide / 2 if anchor == "middle" else 0)
                right = float(a["x"]) + (0 if anchor == "end" else wide / 2 if anchor == "middle" else wide)
                assert left >= -1 and right <= w + 1, (el.text, left, right, item["chart"]["type"])
            for x, y in found:
                assert -0.5 <= x <= w + 0.5 and -0.5 <= y <= h + 0.5, (name, x, y, item["chart"]["title"])


def test_bars_from_zero_in_proportion():
    """T3: the zero line sits at the axis, each bar's base on it, and bar length follows the value within 1 px."""
    values = [120, 60, 30, 15, 3, 1]
    item = one("group_records", group([(f"G{i}", v) for i, v in enumerate(values)]), None, "horizontal_bar")
    svg = svg_of(item)
    axis = next(el for el in nodes(svg) if el.attrib.get("class") == "a")
    base = _path_numbers(axis.attrib["d"])[0]
    bars = [_path_points(el.attrib["d"]) for el in _bar_marks(svg)]
    assert len(bars) == 6
    assert all(abs(min(x for x, _y in p) - base) < 0.2 for p in bars)
    scale = (max(x for x, _y in bars[0]) - base) / values[0]
    for p, v in zip(bars, values):
        assert abs(max(x for x, _y in p) - base - v * scale) <= 1
    item = one("group_records", group([(f"G{i}", v) for i, v in enumerate(values)]))
    svg = svg_of(item)
    axis = next(el for el in nodes(svg) if el.attrib.get("class") == "a")
    base = _path_numbers(axis.attrib["d"])[1]
    bars = [_path_points(el.attrib["d"]) for el in _bar_marks(svg)]
    assert all(abs(max(y for _x, y in p) - base) < 0.2 for p in bars)
    scale = (base - min(y for _x, y in bars[0])) / values[0]
    for p, v in zip(bars, values):
        assert abs(base - min(y for _x, y in p) - v * scale) <= 1
    # the 0 tick is on the axis on a line too
    item = one("trend_records", trend(months([0, 5, 10])))
    svg = svg_of(item)
    axis = next(el for el in nodes(svg) if el.attrib.get("class") == "a")
    base = _path_numbers(axis.attrib["d"])[1]
    first = next(el for el in nodes(svg) if tag(el) == "circle")
    assert abs(float(first.attrib["cy"]) - base) < 0.2


def test_svg_holds_title_subtitle_notes():
    applied = {"filters": ["Status__c = 'x'"], "period": _span("2026-01-01", "2026-02-01", "2026-02-02")["period"]}
    res = group(THREE, group_count=9, applied=applied, blank_group_value=4, ties_at_cutoff_count=2)
    for asked in (None, "horizontal_bar", "pie"):
        item = one("group_records", res, None, asked)
        svg = svg_of(item)
        assert item["chart"]["title"] in texts(svg)
        for line in item["subtitle"] + item["notes"]:
            assert " ".join(line.split()) in said(svg), line
    long_notes = group(THREE, applied={"filters": [f"{k} = 'x'" for k in FIELDS]})
    item = one("group_records", long_notes)
    item["subtitle"][0] += ", " + ", ".join(["Another Long Field Label"] * 6)
    svg = svg_of(item)
    wrapped = [t for t in texts(svg) if "Another Long Field Label" in t]
    assert len(wrapped) >= 2 and all(len(t) <= 92 for t in wrapped)
    assert said(svg).count("Another Long Field Label") == 6


def test_svg_fixed_tags_and_attributes():
    for item in _all_shapes():
        svg = svg_of(item)
        root = ET.fromstring(svg)
        for el in root.iter():
            assert tag(el) in {"svg", "title", "style", "rect", "path", "circle", "text"}
            assert set(el.attrib) <= {"width", "height", "viewBox", "role", "class", "x", "y", "rx", "d", "cx", "cy",
                                      "r", "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap",
                                      "stroke-linejoin", "opacity", "text-anchor", "font-size", "font-weight",
                                      "font-family"}
        style = next(el for el in root.iter() if tag(el) == "style")
        assert style.text == charts._CSS


def test_svg_no_href_url_script():
    for item in [*_all_shapes(), *_hostile_items()]:
        svg = svg_of(item)
        head = svg.split("</style>")[0]   # the xmlns is the only address an SVG holds
        attrs = " ".join(v for el in ET.fromstring(svg).iter() for v in el.attrib.values())
        assert "url(" not in attrs and "href" not in " ".join(k for el in ET.fromstring(svg).iter() for k in el.attrib)
        assert "javascript:" not in attrs and "<script" not in svg and "<image" not in svg and "<a " not in svg
        assert "http://" not in head.replace('xmlns="http://www.w3.org/2000/svg"', "")


@pytest.mark.parametrize("bad", [
    "not xml at all", "<svg", "", None, 5,
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="http://example.invalid/x.png"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink"><rect x:href="#a"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:red"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(http://example.invalid/#a)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "http://example.invalid/x.css";</style></svg>',
    "<html><title>x</title></html>", "<svg><rect/></svg>",
])
def test_check_svg_refuses_bad(bad):
    assert charts._check_svg(bad) is False


def test_svg_plain_colours_and_background():
    """Readable if ``<style>`` is removed: every mark and text carries its light colour as a plain attribute."""
    for item in _all_shapes():
        svg = re.sub(r"<style>.*?</style>", "", svg_of(item), flags=re.S)
        els = nodes(svg)
        assert not any(tag(el) == "style" for el in els)
        background = next(el for el in els if tag(el) == "rect")
        assert background.attrib["class"] == "b" and background.attrib["fill"] == "#ffffff"
        assert background.attrib["width"] == "640"
        for el in els:
            if tag(el) in ("rect", "path", "circle"):
                assert el.attrib.get("fill", "none") != "none" or el.attrib.get("stroke"), el.attrib
            if tag(el) == "text":
                assert el.attrib["fill"] in ("#17232e", "#586776")
    assert charts._CSS.startswith("@media (prefers-color-scheme:dark){")


def test_series_colours_largest_first():
    res = group([("a", 6, {"Small": 1, "Big": 5}), ("b", 6, {"Small": 1, "Big": 5})])
    item = one("group_records", res)
    assert item["chart"]["y_keys"] == ["Big", "Small"]
    svg = svg_of(item)
    legend = [el for el in nodes(svg) if tag(el) == "rect" and el.attrib.get("rx")]
    assert [el.attrib["fill"] for el in legend] == ["#2a78d6", "#eb6834"]


def test_legend_only_with_two_or_more_series():
    swatches = lambda it: [el for el in nodes(svg_of(it)) if tag(el) == "rect" and el.attrib.get("rx")]  # noqa: E731
    assert not swatches(one("group_records", group(THREE)))
    assert not swatches(one("trend_records", trend(months([3, 4]))))
    assert len(swatches(one("group_records", _stack_group()))) == 2
    assert len(swatches(one("trend_records", _stack_trend(), {"split_by": "Type__c"}, "line"))) == 2


def test_svg_one_positive_slice_draws():
    item = one("group_records", group([("A", 3), ("B", 0)]), None, "pie")
    svg = svg_of(item)
    circles = [el for el in nodes(svg) if tag(el) == "circle"]
    assert len(circles) == 1 and circles[0].attrib["fill"] == "#2a78d6"
    assert not [el for el in nodes(svg) if el.attrib.get("class", "").startswith("f0 s")]
    assert {"A", "B", "3", "0"} <= set(texts(svg))
    donut = svg_of(one("group_records", group([("A", 3), ("B", 0)]), None, "donut"))
    ring = [el for el in nodes(donut) if tag(el) == "circle"]
    assert len(ring) == 1 and ring[0].attrib["fill"] == "none" and ring[0].attrib["stroke"] == "#2a78d6"
    # a slice so large that its arc would start and end on one point
    huge = svg_of(one("group_records", group([("A", 9999999), ("B", 1)]), None, "pie"))
    assert [el for el in nodes(huge) if tag(el) == "circle"]


@pytest.mark.parametrize("highest,whole", [*((n, True) for n in (1, 2, 3, 7, 12, 19, 100, 412, 445, 1000, 1234, 99999,
                                                                 1234567)),
                                           (0.3, False), (2.5, False), (1.75, False)])
def test_axis_top_tight(highest, whole):
    top, step = charts._axis(highest, whole)
    assert top - step < highest <= top + 1e-9
    assert round(top / step) <= 6
    if whole:
        assert isinstance(step, int) and isinstance(top, int)
    assert step in {m * 10 ** e for m in (1, 2, 2.5, 5) for e in range(-3, 8)} or abs(step / round(step, 6) - 1) < 1e-9


def test_width_follows_the_characters():
    """The estimate is not under what Chrome measured on DejaVu Sans, the widest face of the stack, in these."""
    measured = {"W": 0.99, "m": 0.97, "M": 0.86, "O": 0.79, "e": 0.62, "0": 0.64, "i": 0.28, "<": 0.84, "%": 0.95}
    for ch, em in measured.items():
        assert charts._width(ch * 10, 10) >= em * 100 - 1e-6
    assert charts._width("WWWW") > charts._width("Wiki") > charts._width("illi")
    assert charts._width("é") == 13 and charts._width("") == 0


def test_a_long_title_takes_a_second_line():
    title_item = one("group_records", group(THREE, grouped_by="Field_With_Many_Long_Words_" + "Word_" * 20))
    title = title_item["chart"]["title"]
    svg = svg_of(title_item)
    heads = [el.text for el in nodes(svg) if el.attrib.get("font-weight") == "600"]
    assert len(heads) >= 2 and " ".join(heads) == charts._cut(title, charts._CUT_TITLE)


def test_a_line_value_clears_its_own_line():
    """A wide first value over a rising line goes under the point; on a falling line, or with no room under it, over."""
    def first_value(values, **kw):
        svg = svg_of(one("trend_records", trend(months(values), measure="sum", measure_field="Rate__c", **kw)))
        first = next(el for el in nodes(svg) if tag(el) == "circle")
        text = next(el for el in nodes(svg) if el.text == f"{values[0]:,}")
        return float(text.attrib["y"]) - float(first.attrib["cy"])
    assert first_value([1234567.25, 2345678.5, 3456789.75]) > 0
    assert first_value([3456789.75, 1234567.25, 1234.5]) < 0
    assert first_value([9.5, 1234567.25, 3456789.75]) < 0   # the point sits on the axis, so the value stays over it


def test_a_line_value_clears_a_long_rise():
    """With many points one step is too short to see the line run through the text."""
    values = [1000000.25 + i * 90000 for i in range(40)]
    svg = svg_of(one("trend_records", trend(months(values), measure="sum", measure_field="Rate__c")))
    first = next(el for el in nodes(svg) if tag(el) == "circle")
    text = next(el for el in nodes(svg) if el.text == "1,000,000.25")
    assert float(text.attrib["y"]) > float(first.attrib["cy"])


def _value_boxes(svg):
    """The value texts over the columns, with their boxes: ``(text, left, right, top, bottom)``."""
    out = []
    for el in nodes(svg):
        if tag(el) == "text" and el.attrib.get("class") == "i" and el.attrib.get("text-anchor") == "middle":
            half = charts._width(el.text) / 2
            x, y = float(el.attrib["x"]), float(el.attrib["y"])
            out.append((el.text, x - half, x + half, y - 11, y + 3))
    return out


WIDE_COLUMNS = [
    ("short first, 20", [1234567.25] + [9000000.5 - i * 100000 for i in range(19)]),
    ("short first, 12", [123456789.25] + [900000000.5 - i * 10000000 for i in range(11)]),
    ("short first, 30 weeks", [12345678.25] + [90000000.5 - i * 1000000 for i in range(29)]),
    ("first two close", [9234567.25, 9345678.5] + [3456789.75 + i * 10000 for i in range(14)]),
    ("first two level", [9234567.25, 9245678.5] + [3456789.75 + i * 10000 for i in range(14)]),
    ("last two close", [3456789.75 + i * 10000 for i in range(14)] + [9345678.75, 9234567.5]),
]


@pytest.mark.parametrize("name,values", WIDE_COLUMNS, ids=[c[0] for c in WIDE_COLUMNS])
def test_wide_values_on_narrow_columns_stay_readable(name, values):
    """No value is painted over by a later column, sits over a taller one, or meets another value. The highest stays."""
    item = one("trend_records", trend(months(values), measure="sum", measure_field="Rate__c"), None, "bar")
    assert item["chart"]["type"] == "bar"
    svg = svg_of(item)
    boxes = _value_boxes(svg)
    assert charts._fmt(max(values)) in [b[0] for b in boxes]
    for a in boxes:
        for c in boxes:
            if c is not a and a[1] < c[2] and c[1] < a[2]:
                assert a[4] <= c[3] or c[4] <= a[3], (a, c)
    order = nodes(svg)
    last_mark = max(i for i, el in enumerate(order) if tag(el) in ("path", "rect") and
                    el.attrib.get("class", "").startswith("f") and "rx" not in el.attrib)
    first_value = min(i for i, el in enumerate(order) if tag(el) == "text" and el.text in [b[0] for b in boxes]
                      and el.attrib.get("class") == "i")
    assert last_mark < first_value   # every column is painted first
    for mark in _bar_marks(svg):
        left, top, right, _bottom = _box(mark)
        for text, x0, x1, _t, bottom in boxes:
            if x0 < right and left < x1 and text != charts._fmt(max(values)):
                assert bottom <= top + 2.5, (text, left, top)


def test_narrow_values_keep_their_place_beside_a_wide_one():
    """Per column: one wide value takes no other value's room."""
    rows = [("Status A", 12.25), ("Status B", 1234567890123.45), ("Status C", 13.5), ("Status D", 14.75)]
    svg = svg_of(one("group_records", group(rows, measure="sum", measure_field="Rate__c")))
    assert {b[0] for b in _value_boxes(svg)} == {charts._fmt(r[1]) for r in rows}
    rows = [("Status A", 1234567.25)] + [(f"Status {c}", v) for c, v in zip("BCDEFGH", (120.5, 130.5, 140.5, 150.5,
                                                                                        160.5, 170.5, 180.5))]
    svg = svg_of(one("group_records", group(rows, measure="sum", measure_field="Rate__c")))
    assert {b[0] for b in _value_boxes(svg)} >= {charts._fmt(r[1]) for r in rows[1:]}


NULL_COLUMNS = [("wide values", [1234.25, None, 5678.5] * 10), ("narrow values", [4, None, 6, 7, None] * 4)]


@pytest.mark.parametrize("name,values", NULL_COLUMNS, ids=[c[0] for c in NULL_COLUMNS])
def test_a_null_with_no_room_for_its_words_gets_the_note(name, values):
    """A null row cannot look like a 0: its words fit whole, or a note says what an empty slot is."""
    res = trend(months(values), measure="avg", measure_field="Rate__c")
    _final, items, _lines, _error = make(res, tool="trend_records", question="bar chart of interviews per month")
    item = items[0]
    assert item["chart"]["type"] == "bar" and W["null_no_column"] in item["notes"] and W["null_no_column"] in item["alt"]
    svg = base64.b64decode(item["svg_b64"]).decode("utf-8")
    assert W["null_no_column"] in texts(svg) and W["no_value"] not in texts(svg)   # never a clipped "no valu"
    # with room for its words a null has them, and needs no note
    roomy = trend(months([4, None, 6, 7]), measure="avg", measure_field="Rate__c")
    item = make(roomy, tool="trend_records", question="bar chart of interviews per month")[1][0]
    svg = base64.b64decode(item["svg_b64"]).decode("utf-8")
    assert W["no_value"] in texts(svg) and W["null_no_column"] not in item["notes"]


def _null_beside_a_wide_value():
    """Eight rows, whose null has no room for its words: the first value is wide, and low beside the highest."""
    return list(zip("ABCDEFGH", [1234567.25, None, 5.5, 6.5, 7.5, 8.5, 9.5, 123456789.25]))


@pytest.mark.parametrize("tool, res, question", [
    ("group_records", group([(f"Status {c}", v) for c, v in _null_beside_a_wide_value()], measure="avg",
                            measure_field="Rate__c"), "bar chart of interviews by status"),
    ("compare_records", compare([(f"Segment {c}", v) for c, v in _null_beside_a_wide_value()], measure="avg",
                                measure_field="Rate__c"), "bar chart comparing the interviews"),
], ids=["group", "compare"])
def test_the_note_of_a_null_column_is_true_with_no_periods(tool, res, question):
    """The note goes on any column chart, and groups and segments are not periods."""
    item = make(res, tool=tool, question=question)[1][0]
    assert item["chart"]["type"] == "bar" and W["null_no_column"] in item["notes"]
    assert "period" not in W["null_no_column"].lower()
    svg = base64.b64decode(item["svg_b64"]).decode("utf-8")
    assert W["null_no_column"] in texts(svg) and W["no_value"] not in texts(svg)


def _end_texts(svg):
    return [el for el in nodes(svg) if tag(el) == "text" and el.attrib.get("class") == "i"
            and "text-anchor" not in el.attrib and "font-size" not in el.attrib]


def _lines_item(last, grain_rows=5, measure=None):
    """A line per series whose series end at ``last``; before that each holds its own steady value."""
    names = list(last)
    start = {n: 40 + 5 * i for i, n in enumerate(names)}
    by = [dict(start) for _ in range(grain_rows - 1)] + [dict(last)]
    extra = {"measure": measure, "measure_field": "Rate__c"} if measure else {}
    res = trend(months([sum(b.values()) for b in by], by), **extra)
    return one("trend_records", res, {"split_by": "Type__c"}, "line")


@pytest.mark.parametrize("last,written", [
    ({"Series A": 1, "Series B": 1, "Series C": 0, "Series D": 0}, {"1", "0"}),
    ({f"Series {c}": 0 for c in "ABCDEFGH"}, {"0"}),
    ({f"Series {c}": 5 for c in "ABCDEFGH"}, {"5"}),
], ids=["four end low", "eight end at 0", "eight end level"])
def test_line_ends_stay_above_the_axis_and_are_written_once(last, written):
    """A pile of last values never runs under the axis, over the last period's label, where it would read as ticks; the
    same value at one point is written once."""
    item = _lines_item(last)
    svg = svg_of(item)
    zero, _per = _axis_of(svg)
    ends = _end_texts(svg)
    assert {el.text for el in ends} == written and len(ends) == len(written)
    for el in ends:
        assert float(el.attrib["y"]) <= zero + 0.05, el.text
    els = nodes(svg)
    label = next(e for e in els if e.text == item["data"][-1]["Month"])
    half = charts._width(label.text, 12) / 2
    for el in ends:   # clear of the last axis label, whose top is 11 px over its baseline
        assert float(el.attrib["y"]) + 3 <= float(label.attrib["y"]) - 11 or \
            float(el.attrib["x"]) >= float(label.attrib["x"]) + half


def test_line_ends_stay_near_their_points():
    """Labels are pushed apart only against those whose x ranges meet theirs, and a label that cannot sit within 14 px
    of its point is left out: a label far from its point reads as another value on the axis. The lines end in different
    months here, because of gaps."""
    names = [f"S{i}" for i in range(8)]
    rows = []
    for m in range(12):
        by = {}
        for i, name in enumerate(names):
            if m <= 5 + i % 7:   # each series stops at its own month
                by[name] = 10.5 + i * 1.5 + (m % 3) * 0.25
        rows.append(by)
    res = trend(months([round(sum(b.values()) / len(b), 2) for b in rows], rows), measure="avg",
                measure_field="Rate__c")
    item = one("trend_records", res, {"split_by": "Type__c"}, "line")
    assert len(item["chart"]["y_keys"]) == 8
    svg = svg_of(item)
    ends = _end_texts(svg)
    assert ends
    points = [el for el in nodes(svg) if tag(el) == "circle"]
    for el in ends:
        x, y = float(el.attrib["x"]) - 7, float(el.attrib["y"])
        here = [float(c.attrib["cy"]) for c in points if abs(float(c.attrib["cx"]) - x) <= 0.2]
        assert here and min(abs(cy - y) for cy in here) <= 14.1, (el.text, y, here)
    # none touches another whose x range meets its own
    for a in ends:
        for b in ends:
            if a is not b and float(a.attrib["x"]) < float(b.attrib["x"]) + charts._width(b.text) and \
                    float(b.attrib["x"]) < float(a.attrib["x"]) + charts._width(a.text):
                assert abs(float(a.attrib["y"]) - float(b.attrib["y"])) >= 14 - 0.05, (a.text, b.text)
    # lines that end at one point stay near it, each read back through the axis, or are left out
    crowd = _lines_item({f"S{i}": i + 1 for i in range(8)})
    svg = svg_of(crowd)
    zero, per = _axis_of(svg)
    ends = _end_texts(svg)
    assert 2 <= len(ends) < 8
    for el in ends:
        assert abs(float(el.attrib["y"]) - (zero + int(el.text) * per)) <= 14.1, el.text
    # lines that end in different months, too far apart for their labels to meet, are not pushed apart
    by = [{"A": 10, "B": 10}] * 3 + [{"B": 10.5}] * 3
    apart = trend(months([10.0] * 3 + [10.5] * 3, by), measure="avg", measure_field="Rate__c")
    svg = svg_of(one("trend_records", apart, {"split_by": "Type__c"}, "line"))
    zero, per = _axis_of(svg)
    ends = {el.text: float(el.attrib["y"]) for el in _end_texts(svg)}
    assert abs(ends["10"] - (zero + 10 * per + 4)) <= 0.2 and abs(ends["10.5"] - (zero + 10.5 * per + 4)) <= 0.2
    # lines that end far apart keep their own heights: nothing pushes them
    far = _lines_item({"A": 10, "B": 40}, 4)
    svg = svg_of(far)
    ends = {el.text: float(el.attrib["y"]) for el in _end_texts(svg)}
    zero, per = _axis_of(svg)
    assert abs(ends["10"] - (zero + 10 * per + 4)) <= 0.2 and abs(ends["40"] - (zero + 40 * per + 4)) <= 0.2


def test_a_value_at_the_axis_clears_the_ticks():
    """The first value of a line stands at the axis, level with a tick: it starts right of where the ticks end."""
    for values in ([31, 44, 29], [38, 44, 29], [1234, 5000, 2000], [9, 40, 20]):
        svg = svg_of(one("trend_records", trend(months(values))))
        axis = next(el for el in nodes(svg) if el.attrib.get("class") == "a")
        left = _path_numbers(axis.attrib["d"])[0]
        text = next(el for el in nodes(svg) if tag(el) == "text" and el.text == f"{values[0]:,}"
                    and el.attrib.get("text-anchor") == "middle" and "font-size" not in el.attrib)
        assert float(text.attrib["x"]) - charts._width(text.text) / 2 >= left - 6 + 1


def test_axis_ticks_compact_and_labels_exact():
    assert charts._tick(10000) == "10K" and charts._tick(1500000) == "1.5M" and charts._tick(9000) == "9,000"
    assert charts._tick(0) == "0" and charts._tick(0.5) == "0.5" and charts._tick(2000000000) == "2B"
    assert charts._tick(2 * 10 ** 12) == "2T" and charts._tick(10 ** 16) == "10,000T"
    assert charts._tick(2.5e15) == "2,500T"
    for values in ([5e15, 7.5e15, 2.25e15, 9.1e15], [5e11, 9e11]):
        res = trend(months(values), measure="sum", measure_field="Rate__c")
        svg = svg_of(one("trend_records", res))
        assert not any("e+" in t for t in texts(svg)), texts(svg)
        _axis_of(svg)
    assert charts._fmt(1234567) == "1,234,567" and charts._fmt(2000.0) == "2,000" and charts._fmt(1234.5) == "1,234.5"
    assert charts._fmt(None) == W["no_value"] and charts._fmt(0) == "0"


# ------------------------------------------------------------------------------------------------ chart talk
TALK_DROPPED = [
    ("I cannot draw a chart here, so here are the counts:\n- A: 5\n- B: 3", "- A: 5\n- B: 3"),
    ("Here is the bar chart of interviews by status:\n- A: 5", "- A: 5"),
    ("Let me plot this as a bar chart.\n- A: 5", "- A: 5"),
    ("Completed ████ 12\nScheduled ██ 5", "Completed 12\nScheduled 5"),
    ("```python\nimport matplotlib.pyplot as plt\nplt.bar([1],[2])\n```\nThere are 2 groups.", "There are 2 groups."),
    ("![chart](https://example.invalid/a.png)\nThere are 2 groups.", "There are 2 groups."),
    ("**{{ Chart: q1 }}** and {{plot q2 bar}} then text.", "and then text."),
    ("I used make_chart to count. There are 2 groups.", "There are 2 groups."),
    ("chart nahi bana sakta, lekin counts ye hain:\n- A: 5", "- A: 5"),
    ("Status has 3 values. See the chart below. Most are completed.", "Status has 3 values. Most are completed."),
    ("The chart below shows the breakdown by status. Completed is the largest group.",
     "Completed is the largest group."),
    ("The code will draw the chart. Completed is the largest group.", "Completed is the largest group."),
    ("A chart is provided below. Completed is the largest group.", "Completed is the largest group."),
    ("A bar chart has been generated for this data. Completed is the largest group.",
     "Completed is the largest group."),
    ("Chart neeche hai. Completed sabse zyada hai.", "Completed sabse zyada hai."),
    ("Yeh raha chart. Completed sabse zyada hai.", "Completed sabse zyada hai."),
    ("- See the chart. Most are done.\n- A: 5", "- Most are done.\n- A: 5"),
    ("1. See the chart below.\n2. A is 5", "2. A is 5"),
    ("- See the chart below:\n- A: 5", "- A: 5"),
    ("- See the chart. Chart Analyst (Open)\n- A: 5", "- Chart Analyst (Open)\n- A: 5"),
    ("A: 5\n```python\nimport matplotlib.pyplot as plt", "A: 5"),   # a reply cut off inside its plotting fence
]
#: The rows of a list stay (decision 14, section 9's list row): the end of a list item with no closing stop is a row.
LIST_ROWS = ["- I-Chart Analyst, Scheduled\n- I-Java Developer, Scheduled",
             "- Data Visualization Engineer (Open)\n- Chart Analyst (Open)\n- Java Developer (Open)",
             "- Bar chart: (no value)", "1. Chart Analyst\n2. Graph Designer", "* Plot Manager (Closed)"]
TALK_KEPT = [*LIST_ROWS, "```\nplain code 5\n```\nkeep", "Here is the chart: 120 interviews in total.",
             "| chart | 5 |\n|---|---|\n| cannot plot | 3 |", "Source: fake copy; chart of accounts not used.",
             "The line manager reports the status. Completed is the largest group.",
             "Here are the candidates drawn from the referral pool.", "You can draw on the bench list for these roles.",
             "Let me plot this.", "**Source:** fake copy, chart word.", "```\nchart without digits\n```"]


@pytest.mark.parametrize("text,kept", TALK_DROPPED)
def test_drop_talk_table(text, kept):
    assert charts.drop_talk(text) == kept


@pytest.mark.parametrize("text", TALK_KEPT)
def test_drop_talk_keeps(text):
    assert charts.drop_talk(text) == text


def test_sentence_with_digit_stays():
    assert charts.drop_talk("The chart shows 120 interviews.") == "The chart shows 120 interviews."
    assert charts.drop_talk("Here is the chart: 120 in total. See the chart.") == "Here is the chart: 120 in total."
    # an accepted gap, pinned: a digit-free sentence with a chart word goes from a record answer
    assert charts.drop_talk("The record shows how the graph API was set up. Done.") == "Done."


def test_drop_talk_never_empties():
    """The talk sentences come back when only labels or a Source line would be left, by the test the rest of the answer
    path uses (``agent.nudges.answer_text``)."""
    for text in ("Let me plot this.", "See the chart.",
                 "Here is a bar chart of interviews by status.\nSource: fake copy.",
                 "See the chart.\n\n**Source:** fake copy.\n",
                 "**Summary:**\nHere is a bar chart of interviews by status.\nSource: fake copy.",
                 "### Totals\nHere is a bar chart of interviews by status.\nSource: fake copy.",
                 "Here is a bar chart of interviews by status:\n\n---\nSource: fake copy."):
        assert charts.drop_talk(text) == text
    for text in (None, "", 5, []):
        assert charts.drop_talk(text) == text


@pytest.mark.parametrize("text,left", [
    ("{{chart q1}}", ""), ("**{{chart q1}}**", ""), ("![a](b)", ""), ("████", ""), ("```\nmatplotlib\n```", ""),
    ("{{chart q1 bar}}\nSource: fake copy.", "Source: fake copy."),
    ("![Interviews by status](chart.png)\nSource: fake copy.", "Source: fake copy."),
    ("```python\nimport matplotlib.pyplot as plt\nplt.bar([1],[2])\n```\nSource: fake copy.", "Source: fake copy."),
    ("- {{chart q1}}\nSource: fake copy.", "Source: fake copy."),
    # the sentences come back without the marks, and the fence stays out
    ("See the chart. {{chart q1 bar}}\nSource: fake copy.", "See the chart.\nSource: fake copy."),
    ("**Summary:**\n{{chart q1}}\nHere is a bar chart.\nSource: fake copy.",
     "**Summary:**\nHere is a bar chart.\nSource: fake copy."),
    ("See the chart.\n```python\nplt.bar([1],[2])\n```", "See the chart."),
])
def test_drop_talk_never_gives_back_a_mark(text, left):
    assert charts.drop_talk(text) == left


def test_drop_talk_leaves_the_rows_code_wrote():
    block = "- Bar chart: see the chart.\n- Pie chart: see the chart."
    text = f"There are two types.\n{block}\nSource: fake copy."
    assert charts.drop_talk(text) == "There are two types.\nSource: fake copy."   # typed: read as sentences
    assert charts.drop_talk(text, {"{{table q1}}": block}) == text
    rec = NS(evidence=[evidence("group_records", group(THREE))], table_blocks={"{{table q1}}": block})
    assert make(final=text, rec=rec)[0] == text


def test_drop_talk_linear_time():
    long = ("cannot " + "x " * 5000 + "chart. ") * 20
    t0 = time.perf_counter()
    assert charts.drop_talk(long) == long                     # every sentence goes, so the text stays
    first = time.perf_counter() - t0
    assert first < 1.0
    word = "a_" * 100000 + "\n" + "| x |\n" * 20000 + "{{" * 50000
    t0 = time.perf_counter()
    charts.drop_talk(word)
    assert time.perf_counter() - t0 < 1.0
    # a failed Source match is linear in a line of runaway whitespace
    t0 = time.perf_counter()
    assert charts.drop_talk(" " * 64000 + "x") == " " * 64000 + "x"
    assert time.perf_counter() - t0 < 1.0


# ------------------------------------------------------------------------------------------------ never raises
def _fuzzed():
    """Each result with each key removed, and each key set to each wrong type."""
    wrong = [None, 5, -1, "text", True, [], {}, [None], [5], ["x"], {"a": 1}, float("nan"), float("inf")]
    rows = [{"group": "a", "value": 3, "by": {"S": 1, "T": 2}}, {"group": "b", "value": 2}]
    applied = {"filters": ["Status__c = 'x'"], "period": {"date_field": "Date__c", "from": "2026-01-01"}}
    shapes = [("group_records", group(THREE, applied=applied, blank_group_value=2, ties_at_cutoff_count=1,
                                      split_totals={"S": 1})),
              ("group_records", {**group(THREE), "groups": rows}),
              ("trend_records", trend(months([3, 4]), notes=[PART_RUN], measure="sum", measure_field="Rate__c")),
              ("compare_records", compare([("a", 4, _span("2026-01-01")), ("b", 7)])),
              ("count_records", breakdown({"Status__c": {"A": 3, "B": 1}}, 4))]
    for tool, res in shapes:
        yield tool, res
        for key in list(res):
            yield tool, {k: v for k, v in res.items() if k != key}
            for bad in wrong:
                yield tool, {**res, key: bad}
        for row_key in ("groups", "buckets", "segments"):
            if isinstance(res.get(row_key), list) and res[row_key]:
                first = res[row_key][0]
                for inner in first:
                    for bad in wrong:
                        yield tool, {**res, row_key: [{**first, inner: bad}, *res[row_key][1:]]}
    for tool in ("group_records", "trend_records", "compare_records", "count_records", "list_records", "nothing", None,
                 5):
        for bad in wrong:
            yield tool, bad


def test_make_never_raises_fuzz():
    count = 0
    for tool, res in _fuzzed():
        count += 1
        out = make(res, tool=tool, final="Answer.")
        assert isinstance(out, tuple) and len(out) == 4 and out[0] == "Answer." and len(out[2]) <= 1, (tool, res)
        assert isinstance(out[3], str)
        for item in out[1]:
            json.dumps(item, allow_nan=False)
            if item.get("svg_b64"):
                assert charts._check_svg(base64.b64decode(item["svg_b64"]).decode("utf-8"))
    assert count > 500
    ask = charts.chart_ask("bar chart")
    for rec in (NS(), NS(evidence=None), NS(evidence=[None]), NS(evidence=[5]), NS(evidence=[NS()]), 5, "rec"):
        final, items, lines, _error = charts.make(ask, "bar chart", "Answer.", "records", "lean", rec, None, None)
        assert final == "Answer." and items == [] and len(lines) == 1
    for catalog in (None, 5, NS(), NS(objects=None), NS(objects={"Interview__c": None}),
                    NS(objects={"Interview__c": 5}), NS(objects={"Interview__c": NS(fields=None)})):
        final, items, _lines, error = make(group(THREE), catalog=catalog)
        assert final == "There are 5." and items and not error, catalog
    for today in ("not a day", 5, None):
        assert make(group(THREE), today=today)[1]
    assert charts.make(None, None, None, None, None, None, None, None) == (None, [], [W["records_off"]], "")


def test_for_tool_never_raises():
    count = 0
    for tool, res in _fuzzed():
        count += 1
        for args in ({}, None, 5, {"order": 5, "measure": [], "min_value": {}}):
            out = charts.for_tool(tool, res, args, CATALOG, TODAY)
            assert isinstance(out, dict) and "chart" in out and "reason" in out, (tool, res)
            assert "svg_b64" not in out and not any(k.startswith("_") for k in out)
            assert out["chart"] is None or out["svg"]
            json.dumps(out, allow_nan=False)
    assert count > 500
    done = charts.for_tool("group_records", group(THREE), {}, None, None)
    assert done["chart"]["type"] == "bar" and charts._check_svg(done["svg"])
    assert done["chart"]["title"] == "Interview by Status"


def test_fault_in_the_picture_keeps_the_rows(monkeypatch):
    res = group(THREE)
    monkeypatch.setattr(charts, "_check_svg", lambda svg: False)
    final, items, lines, error = make(res)
    assert final == "There are 5." and lines == [W["fault"]] and error == "_SvgRefused"
    assert items[0]["chart"] is None and items[0]["reason"] == W["fault"] and len(items[0]["data"]) == 3
    assert "svg_b64" not in items[0]
    monkeypatch.undo()

    def broken(item):
        raise ZeroDivisionError

    monkeypatch.setattr(charts, "_draw", broken)
    final, items, lines, error = make(res)
    assert final == "There are 5." and lines == [W["fault"]] and error == "ZeroDivisionError"
    assert len(items[0]["data"]) == 3
    assert charts.for_tool("group_records", res, {}, None, None)["reason"] == W["fault"]
    monkeypatch.undo()

    def inner(*_a, **_k):
        raise KeyError("private-value")

    monkeypatch.setattr(charts, "_build", inner)
    final, items, lines, error = make(res)
    assert final == "There are 5." and items == [] and lines == [W["fault"]] and error == "KeyError"
    monkeypatch.undo()
    monkeypatch.setattr(charts, "_pick", inner)
    assert make(res)[1:] == ([], [W["fault"]], "KeyError")


def test_fault_in_every_item_leaves_no_private_key(monkeypatch):
    """The fault's class name goes to ``make``'s error and no further: not on the first item, and not on a later one,
    which reaches the page reply and the sidecar."""
    res = breakdown({"Status__c": {"A": 6, "B": 4}, "Round__c": {"1": 7, "2": 3}}, 10)

    def broken(item):
        raise KeyError("private-value")

    monkeypatch.setattr(charts, "_draw", broken)
    _final, items, lines, error = make(res, tool="count_records")
    assert error == "KeyError" and lines == [W["fault"]] and len(items) == 2
    for item in [*items, *charts.for_jsonl(items)]:
        assert not [k for k in item if k.startswith("_")] and item["chart"] is None
    assert "private-value" not in json.dumps(items)


def test_fault_in_a_result_not_drawn_leaves_its_trace():
    """Decision 15: a fault in a result ``_pick`` does not choose still reaches ``charts_error``, in either order, and
    the chart of the good result stays."""
    good = evidence("group_records", group(THREE, query_id="q1"))
    bad = evidence("group_records", {**group(THREE, query_id="q2"), "groups": [5, 6]})
    for rec in (rec_of(good, bad), rec_of(bad, good)):
        _final, items, lines, error = make(rec=rec)
        assert error == "AttributeError" and lines == []
        assert [i["query_id"] for i in items] == ["q1"] and items[0]["chart"]
        assert not [k for k in items[0] if k.startswith("_")]
    assert make(rec=rec_of(good))[3] == ""


def test_make_drops_talk_only_from_record_text():
    talk = "See the chart below. Completed is the largest group."
    kept = "Completed is the largest group."
    assert make(group(THREE), final=talk, kind="records")[0] == kept
    assert make(group(THREE), final=talk, kind="merged", mode="lean")[0] == kept
    assert make(group(THREE), final=talk, kind="merged", mode="full")[0] == talk
    assert make(group(THREE), final=talk, kind="merged", mode="lean+full")[0] == talk
    assert make(group(THREE), final=talk, kind="knowledge", mode="full")[0] == talk


def test_make_removes_a_mark_from_every_kind():
    """R9: a mark is never an answer's content, unlike a sentence about a chart."""
    for kind, mode in (("knowledge", "full"), ("merged", "full"), ("merged", "lean+full"), ("none", None)):
        text = make(group(THREE), final="10 interviews. See the chart.\n{{chart q1 bar}}\nSource: x.", kind=kind,
                    mode=mode)[0]
        assert text == "10 interviews. See the chart.\nSource: x.", kind
    assert make(group(THREE), final="Answer **{{chart q1}}** here.", kind="knowledge", mode="full")[0] == "Answer here."
    assert make(group(THREE), final=None, kind="knowledge", mode="full")[0] is None


def test_make_leaves_the_words_code_wrote():
    """``make`` cleans what the model says. A not-found or people answer is code's own, and a name in it can hold a
    chart word; a mark in it still goes."""
    text = "No record is named 'Chart Persson'. There is no count for this name.\nSource: x."
    gone = "There is no count for this name.\nSource: x."
    rec = rec_of(evidence("group_records", group(THREE)))
    assert make(final=text, rec=rec)[0] == gone   # the model's: the sentence goes
    for extra in ({"no_records": True}, {"code_answer": True}):
        coded = NS(evidence=rec.evidence, table_blocks={}, **extra)
        assert make(final=text, rec=coded)[0] == text, extra
        assert make(final=text + "\n{{chart q1}}", rec=coded)[0] == text, extra
    # a merged text of a people answer code wrote keeps them too; a no_records flag says nothing of a merged text
    assert make(final=text, kind="merged", rec=NS(evidence=rec.evidence, table_blocks={}, code_answer=True))[0] == text
    assert make(final=text, kind="merged", rec=NS(evidence=rec.evidence, table_blocks={}, no_records=True))[0] == gone


def test_rows_in_answer_follows_the_table_block():
    res = group(THREE)
    block = "- Completed: 12\n- Scheduled: 5\n- Canceled: 2"
    rec = rec_of(evidence("group_records", res), blocks={"{{table q2}}": block})
    assert make(rec=rec, final=f"There are 19.\n{block}\nSource: fake copy.")[1][0]["rows_in_answer"] is True
    assert make(rec=rec, final="There are 19.")[1][0]["rows_in_answer"] is False
    assert make(res)[1][0]["rows_in_answer"] is False


# ------------------------------------------------------------------------------------------------ payload and surfaces
def test_payload_keys():
    item = make(group(THREE, query_id="q7"))[1][0]
    assert set(item) == {"query_id", "tool", "chart", "data", "truncated", "subtitle", "notes", "partial", "reason",
                         "rows_in_answer", "alt", "svg_b64"}
    assert item["query_id"] == "q7" and item["tool"] == "group_records" and item["reason"] == ""
    assert "<" not in item["svg_b64"] and item["alt"].startswith("Interviews by Status. ")
    svg = base64.b64decode(item["svg_b64"]).decode("utf-8")
    assert charts._check_svg(svg)
    json.dumps(item)
    done = charts.for_tool("group_records", group(THREE), {}, CATALOG, TODAY)
    assert "svg" in done and "svg_b64" not in done and charts._check_svg(done["svg"])
    item = make(group(SINGLE_ROW))[1][0]
    assert item["chart"] is None and item["reason"] == W["one_row"] and "svg_b64" not in item and item["alt"]


def test_for_jsonl_no_picture():
    items = [charts.make(charts.chart_ask("bar chart"), "bar chart", "t", "records", "lean",
                         rec_of(evidence("group_records", group(THREE))), CATALOG, TODAY)[1][0],
             charts.for_tool("group_records", group(THREE), {}, CATALOG, TODAY)]
    out = charts.for_jsonl(items)
    assert all("svg_b64" not in i and "svg" not in i for i in out) and "svg_b64" in items[0] and "svg" in items[1]
    assert out[0]["data"] == items[0]["data"] and out[0]["chart"] == items[0]["chart"]
    assert charts.for_jsonl(None) == [] and charts.for_jsonl(5) == [] and charts.for_jsonl([5]) == []


def test_text_lines():
    res = group([("Done", 3, {"S": 1, "T": 2}), ("Open", 1, {"S": 1})], group_count=9,
                applied={"filters": ["Status__c = 'x'"]})
    item = make(res)[1][0]
    lines = charts.text_lines([item])
    assert lines[0] == f"Chart (bar): {item['chart']['title']}"
    assert "Filtered on: Status" in lines and say("showing_top", SHOWN="2", TOTAL="9", UNIT=W["groups"]) in lines
    rows = lines[lines.index(W["cli_rows"]) + 1:]
    assert rows == ["Status | S | T | Total", "Done | 1 | 2 | 3", "Open | 1 |  | 1"]
    hb = one("group_records", _groupn(9))
    assert charts.text_lines([{**hb, "rows_in_answer": True}])[0] == f"Chart (horizontal bar): {hb['chart']['title']}"
    assert W["cli_rows"] not in charts.text_lines([{**item, "rows_in_answer": True}])
    no_chart = make(group([("a", None), ("b", None)]))[1][0]
    lines = charts.text_lines([no_chart])
    assert not lines[0].startswith("Chart (") and "a | (no value)" in lines
    assert charts.text_lines(None) == [] and charts.text_lines([5]) == []


def test_with_chart_arg_is_a_copy():
    tool = {"name": "group_records", "description": "d",
            "inputSchema": {"type": "object", "properties": {"object": {"type": "string"}}, "required": ["object"]}}
    before = copy.deepcopy(tool)
    out = charts.with_chart_arg(tool)
    assert tool == before and out is not tool and out["inputSchema"] is not tool["inputSchema"]
    assert out["inputSchema"]["properties"]["chart"]["type"] == "boolean"
    assert out["inputSchema"]["required"] == ["object"] and out["name"] == "group_records"
    assert charts.CHART_ARG_TOOLS == ("group_records", "trend_records", "compare_records")


# ------------------------------------------------------------------------------------------------ import
def test_import_has_no_side_effects():
    """Constants only: no env read, no warehouse, no model, no file but its two prompts."""
    code = """
import os, sys
import sf_assistant.config as cfg
class Env(dict):
    def __init__(self, base):
        super().__init__(base)
        self.read = []
    def get(self, key, *a):
        self.read.append(key)
        return super().get(key, *a)
    def __getitem__(self, key):
        self.read.append(key)
        return super().__getitem__(key)
env = Env(os.environ)
os.environ = env
def boom():
    raise SystemExit("the switch was read at import")
cfg.charts_settings = boom
from sf_assistant import prompts
loaded = []
real = prompts.load
def load(pid):
    loaded.append(pid)
    return real(pid)
prompts.load = load
import sf_assistant.agent.charts as C
assert "SFA_CHARTS" not in env.read, env.read
for name in ("duckdb", "openai", "sf_assistant.agent.record_session", "sf_assistant.agent.merge",
             "sf_assistant.pipeline"):
    assert name not in sys.modules, name
assert {"records_chart_note", "records_chart_words"} <= set(loaded), loaded
assert C.RECORDS_CHART_NOTE and C.RECORDS_CHART_WORDS
print("ok")
"""
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
