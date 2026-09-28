# -*- coding: utf-8 -*-
"""A DIAGRAM NAMED WITH THE WORD "CHART" IS STILL A DIAGRAM.

Owner report, 2026-09-28, against the release ae25da28 that had just deployed:

    he typed : "Make A Digram or Flow Chart of Api Which Coonect to Db ??"
    he got   : "I can only draw a chart from data I can read as a table.
                Attach the file again (CSV or Excel), or paste the table into
                the message, and I'll plot it."

There was no table because he never wanted one. A flow chart is a DIAGRAM: it
belongs to the chat path, where the model draws mermaid, not to the
chart-from-data path that plots a spreadsheet. The word "chart" was doing the
work rather than the meaning -- `chart_signal("flow chart of the API")` was
True while `chart_signal("flowchart of our deploy")` was False, two spellings
of one request.

Three things are guarded here, and the third is the one that keeps this fix
honest:

  1. the diagram phrases set NO chart signal and reach NO create-chart, in
     English, Hinglish, Hindi and Gujarati;
  2. the owner's exact sentence produces no image-only version, so
     `engines/artifact._image_only` cannot reach the refusal at all;
  3. every REAL chart type still works, and `LX.without_diagram_phrases` is a
     NO-OP on all 77 authored chart requests and all 50 prompt-data cases in
     tests/fixtures/chart_requests.py -- the fix cannot have touched them.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.artifacts import formats as F
from app.artifacts import lexicon as LX
from app.artifacts.intent import decide, verdict_to_intent
from app.engines.artifact import _NO_TABLE_FOR_CHART, _image_only

from .fixtures.chart_requests import PROMPT_DATA_CASES, REQUESTS

OWNER_SENTENCE = "Make A Digram or Flow Chart of Api Which Coonect to Db ??"

#: A diagram, spelt with the word "chart" or "graph". None of these is a chart
#: drawn from the rows of a table.
DIAGRAMS = [
    OWNER_SENTENCE,
    "Make a flow chart of the API which connects to the DB",
    "make a flow chart of our deploy process",
    "make a flowchart of our deploy process",
    "make a flow-chart of our deploy process",
    "make a process flow chart of onboarding",
    "make a process chart of onboarding",
    "make a data flow chart of the ingest path",
    "make a workflow chart of the release",
    "org chart of the team",
    "draw an org-chart for engineering",
    "make an organisation chart of the company",
    "make an organization chart of the company",
    "make an organizational chart of the company",
    "draw a sequence chart of the login calls",
    "draw a swimlane chart of the approval path",
    "make an architecture chart of the stack",
    "make a hierarchy chart of the departments",
    "draw a flow graph of the API",
    # Hinglish / Gujlish, and the same words in Devanagari and Gujarati. The
    # lexicon folds चार्ट / ચાર્ટ to "chart", so the modifier in front of it is
    # what these drive.
    "flow chart banao",
    "api aur db ka flow chart banao",
    "फ्लो चार्ट बनाओ",
    "फ्लोचार्ट बनाओ",
    "ऑर्ग चार्ट बनाओ",
    "ફ્લો ચાર્ટ બનાવો",
    "ઓર્ગ ચાર્ટ બનાવો",
]

#: A chart DRAWN FROM DATA. `gantt` is in `_CHART_RE` on purpose and must stay.
CHARTS = [
    "make me a chart of this table",
    "give me a graph of this",
    "gantt chart of the plan",
    "a gantt-style chart of the plan",
    "bar chart of headcount",
    "line chart of revenue by month",
    "pie chart of spend by team",
    "donut chart of spend by team",
    "doughnut chart of spend",
    "area chart of usage",
    "scatter chart of salary vs experience",
    "scatter plot of salary vs experience",
    "bubble chart of cost vs value",
    "column chart of sales by region",
    "stacked bar chart of sales by region",
    "combo chart of revenue and margin",
    "radar chart of skills",
    "funnel chart of the pipeline",
    "waterfall chart of the variance",
    "box plot of latency",
    "histogram of latency",
    "heat map of activity by hour",
    "treemap of spend",
    "show this as a sunburst",
    "pareto chart of defects",
    "violin plot of latency",
    "bullet chart of targets",
    "बार चार्ट बनाओ",
    "પાઇ ચાર્ટ બનાવો",
    "चार्ट बनाओ",
    "ચાર્ટ બનાવો",
    "ગ્રાફ બનાવો",
]


@pytest.mark.parametrize("text", DIAGRAMS)
def test_a_diagram_named_with_the_word_chart_sets_no_chart_signal(text: str) -> None:
    assert LX.chart_signal(text) is False, f"{text!r} was read as a chart drawn from data"


@pytest.mark.parametrize("text", DIAGRAMS)
def test_a_diagram_falls_through_to_the_chat_path(text: str) -> None:
    """Exactly what "diagram" and "flowchart" already did: no file, no
    create-chart, so the chat path answers and the model draws mermaid."""
    intent = decide(text)
    assert intent.action == "none", f"{text!r} -> {intent.action}/{intent.rule}"
    assert intent.rule != "create-chart"
    assert intent.chart_request is False
    assert intent.wants_file is False


@pytest.mark.parametrize("text", DIAGRAMS)
def test_a_diagram_gets_no_image_only_version(text: str) -> None:
    """The refusal's own precondition. A png/svg version IS its charts
    (`engines/artifact._image_only`), and with no table the job could only
    ever end in `_NO_TABLE_FOR_CHART`."""
    for gate in (None, False, True):
        formats = F.decide(text, chart_request=gate).formats
        assert not _image_only(formats), f"{text!r} (gate={gate}) -> {formats}"


def test_the_owner_s_sentence_cannot_reach_the_refusal() -> None:
    intent = decide(OWNER_SENTENCE)
    assert (intent.action, intent.rule) == ("none", "no-request")
    # No file signal either, so `_should_consult` does not even offer the turn
    # to the classifier -- which is the path the live refusal came down.
    assert LX.file_signal(OWNER_SENTENCE) is False
    formats = F.decide(OWNER_SENTENCE, chart_request=bool(intent.chart_request)).formats
    assert not _image_only(formats)
    assert _NO_TABLE_FOR_CHART.startswith("I can only draw a chart from data")


@pytest.mark.parametrize("text", CHARTS)
def test_a_real_chart_type_still_sets_the_chart_signal(text: str) -> None:
    assert LX.chart_signal(text) is True, f"{text!r} lost its chart signal"


@pytest.mark.parametrize("text", CHARTS)
def test_a_real_chart_type_is_untouched_by_the_diagram_vocabulary(text: str) -> None:
    assert LX.without_diagram_phrases(text) == text
    assert LX.without_diagram_phrases(LX.normalize(text)) == LX.normalize(text)


def test_a_message_asking_for_both_keeps_its_chart() -> None:
    """Only the diagram phrase is blanked, never the sentence around it."""
    text = "a flow chart of the API and a bar chart of headcount"
    assert LX.chart_signal(text) is True
    assert F.decide(text).formats == ["png"]


@pytest.mark.parametrize("text", [
    "Waterfall chart of the cash flow items.",
    "cash flow chart of the year",
    "fund flow chart by quarter",
    "money flow chart for March",
])
def test_cash_flow_is_a_measured_quantity_not_a_sequence_of_steps(text: str) -> None:
    """`flow` behind cash/fund/money names numbers, not steps: c01 and hi05 in
    tests/fixtures/chart_requests.py are that request."""
    assert LX.chart_signal(text) is True
    assert LX.without_diagram_phrases(text) == text


def test_the_diagram_vocabulary_is_a_no_op_on_every_authored_chart_request() -> None:
    """All 77 rows of tests/fixtures/chart_requests.py, raw and normalised,
    plus the 50 prompt-data cases. If this fails, the fix reached a real
    chart request."""
    assert len(REQUESTS) == 77
    for r in REQUESTS:
        assert LX.without_diagram_phrases(r["text"]) == r["text"], r["id"]
        norm = LX.normalize(r["text"])
        assert LX.without_diagram_phrases(norm) == norm, r["id"]
    assert len(PROMPT_DATA_CASES) == 50
    for case in PROMPT_DATA_CASES:
        text = case[0] if isinstance(case, (list, tuple)) else case
        assert LX.without_diagram_phrases(text) == text, text[:60]


@pytest.mark.parametrize("text", [
    "make a flowchart of our deploy process",
    "Draw a diagram of the API which connects to the DB",
    "draw a sequence diagram of the login calls",
    "mind map of the plan",
    "फ्लोचार्ट बनाओ",
    "डायग्राम बनाओ",
    "ડાયગ્રામ બનાવો",
    "ऑर्गचार्ट बनाओ",
])
def test_the_gate_cannot_turn_a_diagram_into_an_image_version(text: str) -> None:
    """The CLASSIFIER path, where the rules are silent and the model is the
    only judge. `chart_request=True` there used to produce ['png'] for the
    one-word spelling of the owner's own request."""
    assert LX.diagram_signal(text) is True
    for gate in (None, False, True):
        assert not _image_only(F.decide(text, chart_request=gate).formats), (text, gate)


def test_a_diagram_next_to_a_real_chart_still_renders_the_chart() -> None:
    text = "put the bar chart and a diagram in a png"
    assert LX.diagram_signal(text) is True
    assert F.decide(text, chart_request=True).formats == ["png"]


@pytest.mark.parametrize("text", [
    "make me a chart of this table",
    "bar chart of headcount",
    "gantt chart of the plan",
    "waterfall chart of the cash flow items",
    "चार्ट बनाओ",
    "a shape on the slide",
    "आकृति बनाओ",
])
def test_diagram_signal_stays_off_a_chart_and_off_ordinary_words(text: str) -> None:
    """`diagram_signal` suppresses the image version, so a word that is NOT a
    diagram must never set it. The Hindi "आकृति" (a figure, a shape) is the
    word deliberately left out of the vocabulary: it is broader than
    "diagram" and nothing measured needs it."""
    assert LX.diagram_signal(text) is False


def _verdict_formats(text: str, formats: list) -> list:
    """What survives `verdict_to_intent` when the CLASSIFIER answers with these
    formats for this message."""
    rules = decide(text)
    verdict = SimpleNamespace(action="create", formats=formats, target="none",
                              chart_request=True, rule="model", confidence=0.9)
    out = verdict_to_intent(verdict, rules, has_artifacts=False, has_assistant_answer=False)
    assert out is not None
    return list(out.formats)


@pytest.mark.parametrize("text", [
    "org chart of the team: Asha is CEO, Ravi and Meera report to her, Dev reports to Ravi",
    OWNER_SENTENCE,
    "make a flowchart of our deploy process",
    "draw a diagram of the API which connects to the DB",
])
def test_the_classifier_cannot_hand_a_diagram_an_image_format(text: str) -> None:
    """An image format on the verdict reaches the engine as `explicit_only`,
    which `formats._decide_images` honours WITHOUT consulting
    `_chart_image_formats` -- so it bypasses the guard there and
    `_image_only` produces the refusal from the model's verdict alone. The org
    chart above is the measured case: `file_signal` is True because "report to
    her" carries the word `reports?`, so the turn IS offered to the classifier.
    """
    assert _verdict_formats(text, ["png"]) == []
    assert _verdict_formats(text, ["png", "svg"]) == []
    assert not _image_only(
        F.decide(text, explicit_only=_verdict_formats(text, ["png"]) or None, chart_request=True).formats
    )


@pytest.mark.parametrize("text", [
    "bar chart of headcount by team",
    "pie chart of spend, give me the png",
    "gantt chart of the plan as a png",
])
def test_the_classifier_keeps_the_image_format_for_a_real_chart(text: str) -> None:
    assert _verdict_formats(text, ["png"]) == ["png"]


#: The sweep. Ten ways to ask, crossed with the nouns, so the guard is the
#: MATRIX and not a list of examples somebody remembered. Measured 2026-09-28
#: against ae25da28: 170 of the 180 diagram phrasings below were
#: chart_signal=True, reached a create and were answered ['png'] -- which
#: `engines/artifact._image_only` turns into "I can only draw a chart from
#: data I can read as a table". The 10 that were already right are the
#: one-word spelling, "flowchart", which is the whole bug in one line.
_ASK_VERBS = ("make a", "draw a", "create a", "give me a", "generate a", "build a",
              "show me a", "i want a", "can you draw a", "please make a")
_DIAGRAM_NOUNS = ("flow chart", "flowchart", "flow-chart", "org chart", "org-chart",
                  "organisation chart", "organization chart", "organizational chart",
                  "process chart", "process flow chart", "sequence chart", "swimlane chart",
                  "workflow chart", "data flow chart", "architecture chart", "hierarchy chart",
                  "flow graph", "org graph")
_CHART_NOUNS = ("bar chart", "line chart", "pie chart", "donut chart", "area chart",
                "scatter chart", "bubble chart", "column chart", "stacked bar chart",
                "combo chart", "radar chart", "funnel chart", "waterfall chart", "gantt chart",
                "box plot", "histogram", "heat map", "treemap", "sunburst chart",
                "candlestick chart", "pareto chart", "violin plot", "bullet chart",
                "chart", "graph", "plot")


def test_every_way_of_asking_for_a_diagram_reaches_the_chat_path() -> None:
    bad = []
    for verb in _ASK_VERBS:
        for noun in _DIAGRAM_NOUNS:
            text = f"{verb} {noun} of the API which connects to the DB"
            intent = decide(text)
            if (LX.chart_signal(text) or intent.action != "none" or intent.chart_request
                    or _image_only(F.decide(text, chart_request=True).formats)):
                bad.append(text)
    assert not bad, f"{len(bad)} of {len(_ASK_VERBS) * len(_DIAGRAM_NOUNS)} diagram phrasings still ask for a chart: {bad[:5]}"


def test_every_way_of_asking_for_a_chart_still_gets_one() -> None:
    """The gate's verdict is passed to `formats.decide`, because that is what
    production does (`engines/artifact.py` calls
    `F.decide(instruction, explicit_only=..., chart_request=bool(intent.chart_request))`).

    It matters for exactly 3 of these 260: "i want a histogram / heat map /
    treemap of headcount by team" answer ['docx', 'pdf'] when `formats.py` is
    left to its own smaller vocabulary -- `i want` is not one of its ask
    verbs and those three nouns are not at the head of the sentence for
    `_CHART_NOUN_ASK_RE`. Measured on ae25da28 and on this branch, the same
    3 both times: a pre-existing gap in formats.py, not this fix, and
    invisible in production because the gate says chart_request=True."""
    bad = []
    for verb in _ASK_VERBS:
        for noun in _CHART_NOUNS:
            text = f"{verb} {noun} of headcount by team"
            intent = decide(text)
            formats = F.decide(text, chart_request=bool(intent.chart_request)).formats
            if not LX.chart_signal(text) or formats != ["png"]:
                bad.append((text, formats))
    assert not bad, f"{len(bad)} of {len(_ASK_VERBS) * len(_CHART_NOUNS)} chart phrasings lost their chart: {bad[:5]}"


def test_a_question_about_a_flow_chart_inside_our_file_is_still_a_question() -> None:
    """The blank is applied where a CHART IS DRAWN, never to the question
    rules: a document we made can hold a mermaid diagram, and asking what is
    in it is still a question about that file's contents."""
    for text in ("what is in the flow chart?", "what does the org chart show?"):
        intent = decide(text, has_artifacts=True, artifact_hints=("report",))
        assert intent.answer_about_artifact is True, f"{text!r} -> {intent.rule}"
        assert intent.action == "none"


# --------------------------------------------------------------------------
# The nukta, and the exception the first fix took away from Indic users.
# --------------------------------------------------------------------------

#: "flow" in Devanagari has three spellings that look identical on screen:
#: no nukta, decomposed (फ + U+093C) and precomposed (U+095E). A person types
#: whichever their keyboard gives. Before this, only the first was matched, so
#: `फ़्लो चार्ट बनाओ` still reached the chart path and got the "attach a CSV"
#: refusal -- the owner's reported bug, alive in an ordinary spelling.
NUKTA_FLOW_CHART_ASKS = (
    "फ्लो चार्ट बनाओ",
    "फ़्लो चार्ट बनाओ",
    "फ़्लो चार्ट बनाओ",
    "ફ્લો ચાર્ટ બનાવો",
)

#: A CASH FLOW chart is a real chart drawn from numbers. The first fix's
#: cash/fund/money exception was Latin-only, so blanking "flow chart" took the
#: chart away from every Indic cash-flow ask: a false refusal traded for a true
#: one, landing on exactly the users the diagram fix was written for.
INDIC_CASH_FLOW_CHART_ASKS = (
    "कैश फ्लो चार्ट बनाओ",
    "फंड फ्लो चार्ट बनाओ",
    "કેશ ફ્લો ચાર્ટ બનાવો",
    "make a cash flow chart",
)


@pytest.mark.parametrize("text", NUKTA_FLOW_CHART_ASKS)
def test_every_spelling_of_flow_chart_is_a_diagram(text):
    assert LX.chart_signal(text) is False, (text, [hex(ord(c)) for c in text[:4]])


@pytest.mark.parametrize("text", INDIC_CASH_FLOW_CHART_ASKS)
def test_a_cash_flow_chart_is_still_a_chart_in_every_script(text):
    assert LX.chart_signal(text) is True, (text, "the diagram fix must not eat a real chart")
