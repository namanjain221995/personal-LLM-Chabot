# -*- coding: utf-8 -*-
"""What KIND of picture a person is asking for — a process, or some numbers.

THE INCIDENT (2026-09-28). The owner typed

    Make A Digram or Flow Chart of Api Which Coonect to Db ??

and was answered "I can only draw a chart from data I can read as a table.
Attach the file again (CSV or Excel)". There was no table to attach: he had
asked for a picture of a process. `lexicon._CHART_RE` carries the bare
alternatives `charts?|graphs?|plots?`, so ANY phrase ending in "chart" went to
the plot-a-spreadsheet path, and `intent._should_consult` refuses the
classifier once the rules have decided to make a file — so no model could
rescue it. `flow chart` and `flowchart`, one space apart, were two different
products.

THE PRINCIPLE, and it is the whole design: THE WORD IS NOT THE MEANING.
"chart" in "flow chart" names a diagram; "chart" in "bar chart" names a plot.
A list of banned words is wrong again next week, so nothing here bans a word.
What decides is the SUBJECT in front of the picture word — what the picture is
OF. A subject that names a PROCESS or a STRUCTURE (a flow, an org, a sequence,
a state machine, a dependency) has no chart type that could draw it from rows,
so the picture is a diagram however the person spells the noun after it. A
subject that names a MARK or a DATA SHAPE (bar, line, pie, scatter) is a plot.
The word after the subject — chart, graph, plot, diagram, map, view, board — is
EVIDENCE that a picture was asked for, and nothing more.

WHAT THIS MODULE IS NOT. It does not decide whether a file is wanted, does not
write any sentence, and does not know about formats: `intent.decide` owns the
decision and `visuals` owns the honest no. This module answers one question and
publishes the spans it read it from, so `lexicon.chart_signal` can discount a
chart word that a diagram phrase already accounts for.

THREE MECHANISMS, and the reason a wrong lane is a wrong answer:

  * A CHART FROM DATA needs a bound table and renders a real plot
    (`chart_spec.CHART_TYPES`). Asked for without a table it must still say it
    needs one — that refusal is TRUE, it was only in the wrong place.
  * A DIAGRAM IN CHAT is a ```mermaid fence the browser renders
    (`frontend/lib/mermaid.ts` DIAGRAM_HEADS, 23 of them). No file, no table,
    nothing refused: the model just draws it.
  * A DIAGRAM INSIDE A GENERATED FILE is a `spec.DiagramBlock`
    (`render/diagrams.py`). `in_file` records, per kind, whether that parser
    can carry it today — it accepts `flowchart`/`graph` only, so every other
    head is False and `md_import` substitutes a callout.

DUAL SUBJECTS, and why a bound table settles them. A gantt, a timeline, a
quadrant, a pyramid and a roadmap are drawn BOTH ways here: mermaid draws the
concept, and `chart_spec` plots one from rows. Those are marked `dual`, and a
dual subject is read as a diagram only when the turn points at no data — no
bound dataset and no words naming one. "gantt chart of the project tasks from
this sheet" is a plot; "gantt chart of a three month plan, make the dates up"
is a diagram. Nothing else in here depends on context, because nothing else is
ambiguous: there is no chart type that plots an org chart.

`sankey` IS NOT HERE, deliberately. The browser has a `sankey` head, but
`visuals` refuses a sankey and says why, because every way people ask for one
("how users move between stages") asks for a quantity moving through rows of a
table and there is no chart type for it. Naming it a diagram here would buy a
picture of nothing. The same goes for `map`: `radar` and `treemap` are real
chart types and belong to the data lane.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Diagram:
    """A picture of a process or a structure, as people ask for it.

    `head` is the mermaid head `frontend/lib/mermaid.ts` would draw it with,
    so a caller can say WHAT to draw rather than only that a diagram is
    wanted. `in_file` is whether `render/diagrams.py` can carry it into a
    generated PDF/DOCX/PPTX today.
    """

    token: str
    #: The mermaid head the browser renders it with.
    head: str
    #: What an answer calls it, with its article: "a flow chart".
    phrase: str
    pattern: str
    #: A chart type also draws this kind FROM A TABLE, so a bound table (or
    #: words naming one) makes it a plot instead. See the module docstring.
    dual: bool = False
    #: `render/diagrams.py` can carry this head into a generated file.
    in_file: bool = False


# --------------------------------------------------------- the picture word --

#: The nouns people put AFTER the subject, for the picture itself. "chart",
#: "graph" and "plot" are on this list on purpose: that is the entire point of
#: the module. They are evidence that a picture was asked for; the subject in
#: front says which picture. The Devanagari and Gujarati forms are here because
#: `lexicon.normalize` rewrites चार्ट/ચાર્ટ to "chart" but leaves
#: डायग्राम/ડાયગ્રામ alone, so both spellings reach this module.
_PIC = (r"(?:diagram(?:me)?s?|charts?|graphs?|plots?|maps?|views?|boards?|pictures?|"
        r"visuals?|visuali[sz]ations?|डायग्राम|डायाग्राम|ડાયગ્રામ|ડાયાગ્રામ|चार्ट|ચાર્ટ)")

#: The same list without `map`. A subject whose own name collides with a chart
#: type through "map" uses this one: "tree map" is `treemap`, a real chart type
#: (`chart_spec.CHART_TYPES`), and reading it as a tree DIAGRAM would send
#: "visualise this table as a treemap" to the wrong lane.
_PIC_NO_MAP = (r"(?:diagram(?:me)?s?|charts?|graphs?|plots?|views?|boards?|pictures?|"
               r"visuals?|visuali[sz]ations?|डायग्राम|डायाग्राम|ડાયગ્રામ|ડાયાગ્રામ|चार्ट|ચાર્ટ)")

#: Between the subject and the picture word: a space, a hyphen, or nothing at
#: all. "flow chart", "flow-chart", "flowchart" and "FlowChart" are one ask;
#: the owner's bug was exactly the space.
_J = r"[\s\-_]*"


#: WHAT MAKES "flow" A QUANTITY RATHER THAN A SEQUENCE OF STEPS. A cash flow, a
#: fund flow, a money flow and a capital flow are measured amounts, so "cash
#: flow chart for the year" is a real chart of real numbers and belongs in the
#: data lane. Credit where it is due: this case was found by the parallel
#: `fix/flow-chart-is-a-diagram` branch, which names the authored fixture rows
#: behind it (c01 "Waterfall chart of the cash flow items." and hi05, the same
#: request in Hindi). Measured here before the guard existed, "cash flow chart
#: for the year" was read as a flow chart.
_NOT_A_PROCESS = (r"(?<!cash )(?<!cash-)(?<!cash)(?<!fund )(?<!funds )(?<!money )"
                  r"(?<!capital )(?<!net )(?<!free )(?<!नकदी )")


def _with_pic(subject: str, pic: str = _PIC) -> str:
    """`<subject>` followed by a picture word. The shape the class lives in."""
    return rf"\b(?:{subject}){_J}{pic}"


#: A picture word TRAILING a phrase that has already been recognised. Some
#: subjects are pictures on their own — "ishikawa", "PERT", "kanban", "SWOT",
#: "hierarchy", "swim lane" — and people still add the noun: "ishikawa chart",
#: "PERT chart", "swim lane chart". The span has to cover that word or
#: `lexicon.chart_signal` finds a chart the diagram phrase never accounted for
#: and the turn goes back to the spreadsheet path. Measured before this
#: existed: `chart_signal("ishikawa chart for the latency problem")` was True.
_TRAILING_PIC_RE = re.compile(rf"^{_J}{_PIC}", re.I)


# ------------------------------------------------------------- the subjects --
#
# Order is test order: a more specific subject is written before the subject it
# contains, so "data flow chart" is a data-flow diagram and not a flow chart,
# and "family tree" is not read as a bare tree.

SUBJECTS: Tuple[Diagram, ...] = (
    # --- processes -----------------------------------------------------------
    Diagram("data_flow", "flowchart", "a data flow diagram",
            _with_pic(r"data{J}flows?".format(J=_J)) + r"|\bdfds?\b", in_file=True),
    Diagram("process_flow", "flowchart", "a process flow diagram",
            # A PROCESS ON ITS OWN IS A DIAGRAM: "process chart of invoice
            # approval" needs no "flow" in it. Found by the parallel
            # `fix/flow-chart-is-a-diagram` branch's 180-phrasing grid, where
            # `process chart` and `organizational chart` were the two nouns this
            # vocabulary did not carry; measured here, both went to the chart
            # lane.
            _with_pic(r"pro[cs]es+(?:es)?|प्रोसेस|પ્રોસેસ") + r"|"
            + _with_pic(r"(?:pro[cs]es+|प्रोसेस|પ્રોસેસ)(?:{J}(?:ka|no|na|ni))?{J}flows?".format(J=_J))
            # "show the process flow for employee offboarding": the flow IS the
            # picture, so no picture word is needed after it.
            + r"|\b(?:pro[cs]es+|प्रोसेस|પ્રોસેસ)\s+flows?\b"
            # "process ka flow chart chahiye" — the Hinglish possessive sits
            # between the subject and the picture word.
            + r"|\b(?:pro[cs]es+|प्रोसेस|પ્રોસેસ)\s+(?:ka|ki|ke|no|na|ni)\s+flow",
            in_file=True),
    Diagram("swimlane", "flowchart", "a swimlane diagram",
            _with_pic(r"swim{J}lanes?".format(J=_J)) + r"|\bswim{J}lanes?\b".format(J=_J),
            in_file=True),
    Diagram("bpmn", "flowchart", "a BPMN diagram", r"\bbpmn\b", in_file=True),
    Diagram("value_stream", "flowchart", "a value stream map",
            r"\bvalue\s+streams?\b", in_file=True),
    Diagram("flow_chart", "flowchart", "a flow chart",
            # A flow needs a picture word — "the flow of funds" is not a
            # picture — except in the one-word spellings, which are.
            #
            # `cash`, `fund`, `money` and `capital` make "flow" a MEASURED
            # QUANTITY rather than a sequence of steps, so "cash flow chart for
            # the year" is a real chart of real numbers and must stay in the
            # data lane. Credit where it is due: this case was found by the
            # parallel `fix/flow-chart-is-a-diagram` branch, which names the
            # fixture rows behind it (c01 and hi05, the cash-flow waterfall in
            # English and Hindi); measured here before the lookbehind,
            # "cash flow chart for the year" was read as a flow chart.
            #
            # A WORKFLOW, by contrast, is exactly a sequence of steps, and
            # "workflow" has no word boundary before its "flow" — so it needs
            # its own arm or it stays a chart.
            _with_pic(rf"{_NOT_A_PROCESS}flo?w+|फ्लो|ફ્લો")
            + rf"|\b{_NOT_A_PROCESS}flow{_J}charts?\b"
            + r"|" + _with_pic(r"work{J}flows?".format(J=_J))
            + r"|\bwork{J}flows?\b".format(J=_J),
            in_file=True),
    Diagram("fishbone", "flowchart", "a fishbone diagram",
            r"\b(?:fish{J}bones?|ishikawas?)\b".format(J=_J), in_file=True),
    Diagram("pert", "flowchart", "a PERT chart", r"\bperts?\b", in_file=True),
    Diagram("user_journey", "journey", "a user journey map",
            r"\b(?:user|customer|buyer|client|candidate|employee)\s+journeys?\b"
            + r"|" + _with_pic(r"journeys?")),

    # --- structures ----------------------------------------------------------
    Diagram("org_chart", "flowchart", "an org chart",
            _with_pic(r"orgs?|organi[sz]ations?|organi[sz]ational|ઓર્ગ|ऑर्ग|आर्ग")
            + r"|\borg{J}charts?\b".format(J=_J)
            + r"|\breporting\s+(?:structure|lines?|hierarchy)\b", in_file=True),
    Diagram("hierarchy", "flowchart", "a hierarchy diagram",
            r"\bhierarch(?:y|ies)\b", in_file=True),
    Diagram("family_tree", "flowchart", "a family tree", r"\bfamily\s+trees?\b", in_file=True),
    Diagram("decision_tree", "flowchart", "a decision tree", r"\bdecision\s+trees?\b", in_file=True),
    # `tree` takes the picture list WITHOUT "map": see `_PIC_NO_MAP`.
    Diagram("tree", "flowchart", "a tree diagram",
            _with_pic(r"trees?|folder|directory", _PIC_NO_MAP)
            + r"|\b(?:an?|the)\s+trees?\s+of\b", in_file=True),
    Diagram("dependency_graph", "flowchart", "a dependency graph",
            _with_pic(r"dependency|dependencies|depends?") + r"|\bdependency\s+trees?\b",
            in_file=True),
    Diagram("call_graph", "flowchart", "a call graph",
            r"\bcall\s+(?:graphs?|trees?|charts?|diagrams?)\b", in_file=True),
    Diagram("architecture", "flowchart", "an architecture diagram",
            _with_pic(r"architectures?|आर्किटेक्चर|આર્કિટેક્ચર")
            # "architecture ka diagram banao" — the possessive again.
            + r"|\b(?:architectures?|आर्किटेक्चर|આર્કિટેક્ચર)\s+(?:ka|ki|ke|no|na|ni)\s+" + _PIC
            + r"|\bsystem\s+designs?\b", in_file=True),
    Diagram("deployment", "flowchart", "a deployment diagram",
            _with_pic(r"deployments?|topolog(?:y|ies)|infra(?:structure)?"), in_file=True),
    # A NETWORK OF NAMED BOXES, not of rows. `visuals._NETWORK_PATTERN` still
    # refuses the ask that points at a table ("network diagram from this edge
    # list"), and `intent` runs that refusal FIRST, so this entry only ever
    # sees the hand-named kind.
    Diagram("network_diagram", "flowchart", "a network diagram",
            _with_pic(r"networks?"), in_file=True),
    Diagram("block_diagram", "block", "a block diagram", r"\bblock\s+diagram(?:me)?s?\b"),
    Diagram("packet", "packet", "a packet diagram", r"\bpacket\s+diagram(?:me)?s?\b"),
    Diagram("c4", "c4context", "a C4 diagram",
            r"\bc4\b(?=[\s\-]*(?:context|container|component|model|architecture|diagram))"),
    Diagram("er_diagram", "erdiagram", "an ER diagram",
            r"\be[\s\-]?r[\s\-]?ds?\b|\ber{J}(?:{pic})".format(J=_J, pic=_PIC)
            + r"|\bentity[\s\-]relationship\b"
            + r"|" + _with_pic(r"schemas?|databases?|db|ડેટાબેઝ|डेटाबेस")
            + r"|\b(?:schemas?|databases?|ડેટાબેઝ|डेटाबेस)(?:નો|નું|ना|का|के)?\s+er\b"),
    Diagram("class_diagram", "classdiagram", "a class diagram",
            r"\bclass\s+diagram(?:me)?s?\b|\buml\s+class\b"),
    Diagram("state_machine", "statediagram", "a state diagram",
            r"\bstate{J}(?:machines?|diagram(?:me)?s?|charts?|transitions?)\b".format(J=_J)
            + r"|\bstate{J}machines?\b".format(J=_J)),
    Diagram("sequence", "sequencediagram", "a sequence diagram",
            _with_pic(r"sequ(?:en|n|ne)[cs]e?s?") + r"|\bsequ(?:en|n|ne)[cs]e?\s+diagram(?:me)?s?\b"),
    Diagram("requirements", "requirementdiagram", "a requirement diagram",
            r"\brequirements?\s+diagram(?:me)?s?\b"),
    Diagram("git_graph", "gitgraph", "a git graph",
            r"\bgit{J}(?:graphs?|trees?|histor(?:y|ies))\b".format(J=_J)
            + r"|\bbranch(?:ing)?\s+(?:graphs?|diagram(?:me)?s?|trees?)\b"
            + r"|\bcommit\s+(?:graphs?|histor(?:y|ies))\b"),
    Diagram("kanban", "kanban", "a kanban board", r"\bkanbans?\b"),
    Diagram("mind_map", "mindmap", "a mind map",
            r"\bmind{J}maps?\b".format(J=_J)
            # Devanagari/Gujarati: माइंड मैप / માઇન્ડ મેપ. Without this, the
            # compound's second half matched `visuals._MAP_PATTERN` and the
            # person was told "I can't draw a map" for a mind map the English
            # sentence draws perfectly (finding 8, measured 2026-09-28).
            + r"|(?:माइंड|माइन्ड|माईंड|માઇન્ડ|માઈન્ડ)\s*(?:मैप|मेप|नक्शा|મેપ|નકશો|map)"),
    Diagram("swot", "quadrantchart", "a SWOT diagram", r"\bswots?\b"),

    # --- dual: mermaid draws the concept, chart_spec plots the rows ----------
    Diagram("timeline", "timeline", "a timeline",
            r"\btime{J}lines?\b".format(J=_J), dual=True),
    Diagram("roadmap", "timeline", "a roadmap",
            r"\broad{J}maps?\b".format(J=_J), dual=True),
    Diagram("gantt", "gantt", "a Gantt chart", r"\bgantt(?:-style)?\b", dual=True),
    Diagram("quadrant", "quadrantchart", "a quadrant chart",
            r"\bquadrants?\b|\b2\s*[x×]\s*2\b", dual=True),
    Diagram("pyramid", "flowchart", "a pyramid diagram",
            _with_pic(r"pyramids?") + r"|\b(?:\w+(?:'s)?\s+)?pyramids?\s+(?:of|for)\b", dual=True),
)


# ------------------------------------------------- what points at a dataset --

#: Words that name a table the turn is about. A DUAL subject beside one of
#: these is a plot of those rows, not a drawing of the concept: "timeline chart
#: from the dates in this sheet" and "pyramid chart of headcount by level from
#: this sheet" are charts, while "timeline of the project milestones" and
#: "draw Maslow's pyramid" are diagrams.
_DATA_SOURCE_RE = re.compile(
    r"\b(?:this|that|these|those|the|my|our|attached|uploaded)\s+"
    r"(?:\w+\s+){0,2}?(?:sheets?|spread\s?sheets?|work\s?books?|excel|xlsx?|xls|csvs?|"
    r"tables?|data\s?sets?|data\s?files?|data|rows?|records?|columns?|numbers?|figures?|export)\b"
    r"|\b(?:from|in|out\s+of|based\s+on|using)\s+(?:the\s+|this\s+|that\s+|these\s+|my\s+|our\s+)?"
    r"(?:sheets?|spread\s?sheets?|work\s?books?|excel|xlsx?|xls|csvs?|tables?|data\s?sets?|rows?|records?)\b",
    re.I,
)

#: A clause that names a file-shaped word as the SOURCE of the picture, not as
#: the thing to be produced. `formats.kind_for` cannot tell the two apart —
#: "make a network graph of the rows in this sheet" reads as a workbook ask and
#: "fit a decision tree on this dataset" as a dataset ask — so the honest
#: refusal was skipped for both (finding 9's shape, measured 2026-09-28).
#: `deliverable_is_the_picture` strips these before asking.
_SOURCE_CLAUSE_RE = re.compile(
    r"\b(?:in|from|of|on|out\s+of|inside|within|based\s+on|using|over|against|across)\s+"
    r"(?:the\s+|this\s+|that\s+|these\s+|those\s+|my\s+|our\s+|attached\s+|uploaded\s+|each\s+)*"
    r"(?:spread\s?sheets?|work\s?books?|sheets?|excel|xlsx?|xls|csvs?|data\s?sets?|data\s?files?|"
    r"tables?|uploads?|attachments?|rows?|records?|data|files?)\b",
    re.I,
)


def points_at_data(text: str) -> bool:
    """Do these words name a table for the picture to be drawn FROM?"""
    return bool(_DATA_SOURCE_RE.search((text or "")[:4000]))


def without_source_clauses(text: str) -> str:
    """`text` with the clauses that name a file as the PICTURE'S SOURCE removed.

    A format word is not a request for that format when it says where the
    numbers come from. Measured 2026-09-28, with the whole sentence read:

        "make a network graph of the rows in this sheet"
              -> formats.explicit_formats == ['xlsx']
        "fit a decision tree on this dataset and show it"
              -> formats.explicit_formats == ['csv']
        "org chart of the team from the names in this sheet"
              -> formats.explicit_formats == ['xlsx']

    Every guard that reads "did the person name a FILE as well as a picture?"
    answered yes for all three, so the first two lost the honest refusal they
    had earned and the third was sent to the spreadsheet path. Reading the same
    sentence without its source clauses answers no for all three, and leaves
    "put the map in a PDF report" — where the PDF is the deliverable — naming
    its PDF.
    """
    return _SOURCE_CLAUSE_RE.sub(" ", (text or "")[:SCAN_CHARS])


# --------------------------------------------------------------- the reader --

SCAN_CHARS = 4000


def _scan(text: str) -> str:
    return (text or "")[:SCAN_CHARS]


def spans(text: str) -> List[Tuple[int, int]]:
    """Every diagram phrase in `text`, as (start, end) character offsets.

    This is what `lexicon.chart_signal` reads. A chart word that falls inside
    one of these spans is already accounted for by the diagram phrase and is
    not evidence of a plot — which is the whole of the owner's bug, expressed
    as an offset rather than as a banned word.

    DUAL SUBJECTS ARE NOT INCLUDED. `chart_signal` is asked about a string
    alone, with no way to know whether a table is bound, and a gantt or a
    timeline really is a chart type here. Discounting them would move the
    product's own 77 authored chart requests; `intent.decide`, which does know,
    resolves those instead.
    """
    out: List[Tuple[int, int]] = []
    t = _scan(text)
    for d in SUBJECTS:
        if d.dual:
            continue
        for m in re.finditer(d.pattern, t, re.I):
            end = m.end()
            trailing = _TRAILING_PIC_RE.match(t[end:])
            if trailing:
                end += trailing.end()
            out.append((m.start(), end))
    return out


def _covered(start: int, end: int, by: Sequence[Tuple[int, int]]) -> bool:
    """Does a diagram phrase already account for this stretch of text?

    OVERLAP, NOT CONTAINMENT, and the reason is `_CHART_RE`'s own shape. Its
    type-name arm matches "stacked bar chart" as one span, so a chart match can
    START before a diagram phrase and end inside it. Anything that shares
    characters with a diagram phrase is read as part of it; a chart word
    somewhere else in the sentence — "a flow chart of the steps AND a bar chart
    of tickets per owner" — shares none and survives, which is why that message
    still draws its bar chart.
    """
    return any(s < end and start < e for s, e in by)


def names_a_diagram(text: str) -> Optional[Diagram]:
    """The first diagram kind these words name, ignoring context.

    Naming only, and dual subjects included: `diagram_ask` is what decides.
    """
    t = _scan(text)
    if not t:
        return None
    for d in SUBJECTS:
        if re.search(d.pattern, t, re.I):
            return d
    return None


def diagram_ask(text: str, *, has_dataset: bool = False) -> Optional[Diagram]:
    """The diagram this turn asks to be DRAWN, or None.

    A non-dual subject is always a diagram: there is no chart type that plots
    an org chart, so a chart lane is always the wrong answer for one. A dual
    subject is a diagram only when the turn points at no data — see the module
    docstring.

    No ask verb is required. "flow chart of the API" is a request, and the
    corpus of 206 phrasings is full of the noun-phrase form; demanding a verb
    is the same mistake `visuals._ASK_RE` made (finding 10).
    """
    t = _scan(text)
    if not t:
        return None
    pointed = has_dataset or points_at_data(t)
    for d in SUBJECTS:
        if not re.search(d.pattern, t, re.I):
            continue
        if d.dual and pointed:
            # A gantt, timeline, quadrant, pyramid or roadmap over a bound
            # table is a plot of those rows. Keep looking: the same sentence
            # may name a second, unambiguous subject.
            continue
        return d
    return None


# ------------------------------------------- is a PLOT asked for as well? --

#: A chart TYPE said in words. `lexicon.CHART_TYPE_WORDS` is the same
#: vocabulary and is not imported here only because this module must stay
#: importable by `lexicon` itself; `tests/test_artifact_pictures.py` asserts
#: the two lists have not drifted.
_TYPE_NAMED_RE = re.compile(
    r"\b(?:horizontal\s+bar|percent\s+stacked(?:\s+bar)?|stacked(?:\s+bar)?|bar|column|line|area|pie|donut|doughnut|"
    r"scatter|bubble|combo|dual[\s-]axis|waterfall|funnel|radar|spider|pareto|violin|bullet|"
    r"heat\s*map|box)\s*(?:charts?|graphs?|plots?)\b"
    r"|\b(?:histograms?|heat\s*maps?|scatter\s*plots?|box\s*plots?|tree\s*maps?|sunbursts?|candlesticks?|ohlc)\b",
    re.I,
)


def data_chart_named(text: str) -> bool:
    """Does this turn name a plot of NUMBERS that no diagram phrase covers?

    THE MESSAGE THAT ASKS FOR BOTH. "give me a flow chart of the approval steps
    and a bar chart of tickets per owner" wants the chart lane: the bar chart
    is a real plot over a real table, and the model is free to draw the flow
    chart in the same answer. So a named chart TYPE outside every diagram
    phrase keeps the turn in the data lane.

    A BARE PICTURE WORD DOES NOT COUNT, and this is the difference from
    `lexicon.chart_signal`. "draw the user journey from signup to first chart"
    mentions a chart; it names no type and asks for no plot. Reading the bare
    word as a plot is what sent it to the spreadsheet path.
    """
    t = _scan(text)
    if not t:
        return False
    sp = spans(t)
    return any(not _covered(m.start(), m.end(), sp) for m in _TYPE_NAMED_RE.finditer(t))


# ------------------------------------------ is the PICTURE the deliverable? --


def deliverable_is_the_picture(text: str, phrase_pattern: str, kind_for: object) -> bool:
    """Is the picture the only thing asked for, or was a FILE named too?

    `intent` guards its honest refusal with "the words name no other
    deliverable", and asked `formats.kind_for` about the whole sentence. That
    reads the picture's OWN name as a format:

        "make a word cloud of the ticket descriptions"  -> document words
                (the `word` inside "word cloud" is the Word alias)
        "floor plan of a 2bhk with the kitchen east"    -> document words
                (`plan` is a document word; `floor` is what makes it a drawing)
        "fit a decision tree on this dataset and show it" -> spreadsheet words
                (the dataset is the SOURCE, not the deliverable)

    All three lost their refusal and one shipped a .docx for a picture — the
    2026-09-16 incident again, on a different visual. The fix is the module's
    own principle: blank the picture's name, strip the clauses that name a file
    as the SOURCE, and ask about what is left.

    `kind_for` is passed in rather than imported so this module stays free of
    `formats`, which imports the lexicon.
    """
    t = _scan(text)
    if not t:
        return True
    rest = re.sub(phrase_pattern, " ", t, flags=re.I)
    rest = _SOURCE_CLAUSE_RE.sub(" ", rest)
    try:
        return bool(kind_for(rest, [])[1] == "default")  # type: ignore[operator, index]
    except Exception:  # noqa: BLE001 — a guard that cannot be evaluated must not lose the refusal
        return True


__all__ = [
    "Diagram", "SUBJECTS", "SCAN_CHARS", "spans", "names_a_diagram", "diagram_ask",
    "points_at_data", "without_source_clauses", "data_chart_named", "deliverable_is_the_picture",
]
