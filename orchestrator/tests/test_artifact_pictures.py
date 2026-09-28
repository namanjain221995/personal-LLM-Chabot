# -*- coding: utf-8 -*-
"""THE WORD IS NOT THE MEANING: what kind of picture a person asked for.

THE INCIDENT (2026-09-28). The owner typed

    Make A Digram or Flow Chart of Api Which Coonect to Db ??

and was answered "I can only draw a chart from data I can read as a table.
Attach the file again (CSV or Excel)". He had asked for a picture of a process;
there was no table, and none was needed. `lexicon._CHART_RE` carries the bare
alternatives `charts?|graphs?|plots?`, so every phrase ending in one of them
went to the plot-a-spreadsheet path — and `intent._should_consult` refuses the
classifier once the rules have decided to make a file, so no model could rescue
it at runtime. `flow chart` and `flowchart`, one space apart, were two different
products.

WHAT THIS FILE GATES, and why each gate is not the obvious one:

  * that the DIAGRAM SUBJECT decides, not the picture word. A test that only
    checked "flow chart" would be satisfied by a banned-word list, which is
    wrong again next week: these cases are a subject the survey never used
    alongside the words `chart`, `graph` and `plot` in turn.
  * that a chart word a diagram phrase does NOT account for still asks for a
    plot. The message that asks for both is the one a narrowing fix breaks.
  * that the product's own 77 authored chart requests and the 119-case intent
    corpus do not move. Those are the two mandatory-proof sets, and the failure
    mode of this change is breaking real charts.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.artifacts import chart_spec as CS
from app.artifacts import formats as F
from app.artifacts import intent as I
from app.artifacts import lexicon as LX
from app.artifacts import pictures as PIC
from app.artifacts import visuals as VIS


def _rules_view(text: str) -> str:
    """The text as `decide` reads it: normalised, windowed, negations blanked."""
    return I._rule_view(I._decide_window(I._clean(text)))  # noqa: SLF001 — this is what decide reads


# ----------------------------------------------- the subject, not the word --

#: One diagram subject, spelled with EVERY picture word people hang off it. The
#: point of the parametrisation is that no member of the set is special: the
#: subject is what carries the meaning, so all four must land in the same lane.
@pytest.mark.parametrize("word", ["chart", "graph", "plot", "diagram"])
@pytest.mark.parametrize("subject,token", [
    ("flow", "flow_chart"),
    ("org", "org_chart"),
    ("dependency", "dependency_graph"),
    ("architecture", "architecture"),
    ("deployment", "deployment"),
    ("sequence", "sequence"),
])
def test_the_subject_decides_which_picture_it_is(subject, token, word):
    text = f"{subject} {word} of the platform"
    got = PIC.diagram_ask(_rules_view(text))
    assert got is not None and got.token == token, text
    assert LX.chart_signal(_rules_view(text)) is False, (
        f"{text!r} still reads as a plot of numbers; `_CHART_RE`'s bare "
        f"`{word}` alternative is being read as the decision"
    )
    i = I.decide(text)
    assert i.action == "none" and not i.chart_request, f"{text!r} -> {i.action}/{i.rule}"
    assert i.rule == f"diagram-in-chat:{token}"


@pytest.mark.parametrize("text", [
    # The owner's verbatim sentence, and the one-space neighbour that already
    # worked. Two spellings of one request must be one product.
    "Make A Digram or Flow Chart of Api Which Coonect to Db ??",
    "flow chart of the API",
    "flowchart of our deploy pipeline",
    "flow-chart of the checkout steps",
    "FlowChart of the retry logic please",
    "FLOW CHART OF THE LOGIN PROCESS",
    "i need a floww chart of the onboarding",
    "process flow chart of how we handle a support ticket",
    # …and the same word in the other scripts the product speaks.
    "फ्लो चार्ट बनाओ कि रिक्वेस्ट डेटाबेस तक कैसे पहुँचती है",
    "ફ્લો ચાર્ટ બનાવો કે ઓર્ડર કેવી રીતે પ્રોસેસ થાય છે",
    "ek flow chart banao jisme API se DB tak ka flow ho",
    "process ka flow chart chahiye",
    "team ka org chart banao",
    "ઓર્ગ ચાર્ટ બનાવો ટીમનો",
])
def test_a_picture_of_a_process_never_asks_for_a_table(text):
    i = I.decide(text)
    assert i.action == "none", f"{text!r} -> {i.action}/{i.rule}"
    assert not i.chart_request, f"{text!r} claimed a chart of data"
    assert not i.unsupported_visual, f"{text!r} was refused as {i.unsupported_visual}"
    assert i.diagram, f"{text!r} decided no file but named no diagram either"


def test_the_owners_sentence_cannot_be_handed_to_the_classifier():
    """The regex half of this fix does not reach the owner's own sentence.

    Measured 2026-09-28 on ae25da28: his words gave `action=none rule=ambiguous`,
    so the live gate DID consult the LLM classifier, and the classifier answered
    `create` / formats=['png'] / chart_request in 5 of 5 runs. The refusal he saw
    survived a `_CHART_RE` fix entirely. A picture drawn in the answer needs no
    file, so there is nothing for a model to add.
    """
    i = I.decide("Make A Digram or Flow Chart of Api Which Coonect to Db ??")
    assert i.rule.startswith("diagram-in-chat:")
    assert not i.ambiguous
    assert I._should_consult(i, "Make A Digram or Flow Chart of Api Which Coonect to Db ??") is False  # noqa: SLF001


# ---------------------------------------- a plot of numbers is still a plot --

@pytest.mark.parametrize("text", [
    "bar chart of tickets per owner",
    "stacked bar chart of status by priority",
    "line chart of daily sales",
    "visualise this table as a treemap",
    "show this as a sunburst",
    "scatter of salary vs experience",
    "combo chart: revenue as bars and margin as a line",
    "heat map of defects by module and week",
    "box plot of hours by priority",
    "histogram of hours spent per ticket",
])
def test_a_plot_of_numbers_is_untouched(text):
    assert LX.chart_signal(_rules_view(text)) is True, text
    assert PIC.diagram_ask(_rules_view(text), has_dataset=True) is None, text
    i = I.decide(text, has_dataset=True, upload_formats=("xlsx",))
    assert i.action != "none" and i.chart_request, f"{text!r} -> {i.action}/{i.rule}"


def test_a_message_that_asks_for_both_still_gets_its_chart():
    """The likeliest way to fix the owner's bug badly is to break real charts.

    "give me a flow chart of the approval steps and a bar chart of tickets per
    owner" asks for two pictures. The bar chart is a real plot over a real
    table, so the turn stays in the chart lane and the model draws the flow
    chart in the same answer. A fix that discounted every chart word inside such
    a sentence would silently drop the plot.
    """
    text = "give me a flow chart of the approval steps and a bar chart of tickets per owner"
    low = _rules_view(text)
    assert PIC.diagram_ask(low) is not None, "the flow chart is still read"
    assert PIC.data_chart_named(low) is True, "the bar chart is outside every diagram phrase"
    assert LX.chart_signal(low) is True
    i = I.decide(text, has_dataset=True, upload_formats=("xlsx",))
    assert i.action == "create" and i.chart_request, f"{i.action}/{i.rule}"


def test_a_bare_picture_word_in_the_subject_matter_is_not_a_plot():
    """"draw the user journey from signup to first chart" mentions a chart and
    asks for no plot. `chart_signal` reads the bare word and cannot tell; that
    is exactly why `data_chart_named` requires a chart TYPE."""
    low = _rules_view("draw the user journey from signup to first chart")
    assert PIC.data_chart_named(low) is False
    i = I.decide("draw the user journey from signup to first chart")
    assert i.action == "none" and not i.chart_request, f"{i.action}/{i.rule}"


# --------------------------------------------- a bound table settles a dual --

def test_a_dual_subject_is_settled_by_whether_a_table_is_bound():
    """A gantt, a timeline, a quadrant, a pyramid and a roadmap are drawn BOTH
    ways here — mermaid draws the concept, `chart_spec` plots one from rows — so
    these are the only subjects whose lane depends on context."""
    with_table = I.decide("gantt chart of the project tasks from this sheet",
                          has_dataset=True, upload_formats=("xlsx",))
    assert with_table.action == "create" and with_table.chart_request

    without = I.decide("gantt chart of a three month plan, make the dates up")
    assert without.action == "none" and not without.chart_request, f"{without.action}/{without.rule}"
    assert without.diagram == "gantt"

    assert I.decide("timeline chart from the dates in this sheet",
                    has_dataset=True, upload_formats=("xlsx",)).chart_request
    assert I.decide("timeline of the project milestones").action == "none"


def test_spans_leave_the_dual_subjects_alone():
    """`chart_signal` is asked about a string with no way to know whether a
    table is bound, so discounting a gantt or a timeline there would move the
    product's own authored chart requests. `decide`, which does know, resolves
    them instead."""
    for text in ("gantt chart of the tasks", "timeline chart of the milestones",
                 "quadrant chart of price vs rating", "pyramid chart of headcount by level"):
        assert LX.chart_signal(text) is True, text
        assert PIC.spans(text) == [], text


# ----------------------------------------- the pictures nothing here can draw --

@pytest.mark.parametrize("text,token", [
    ("draw a wireframe of the chat screen", "wireframe"),
    ("make me a mockup of the settings page", "wireframe"),
    ("floor plan of a 2bhk with the kitchen on the east", "floor_plan"),
    ("draw the seating plan for the office", "floor_plan"),
    ("circuit diagram for an LED with a resistor", "circuit"),
    ("draw me a picture of a cat wearing sunglasses", "image"),
    ("generate an image of our logo on a billboard", "image"),
    ("make an infographic of our 2026 numbers", "infographic"),
    ("3d isometric diagram of the data centre", "isometric_3d"),
    ("draw me a calendar for October with the sprints marked", "calendar"),
    ("calendar view of the deadlines this month", "calendar"),
    ("chord diagram of who emails whom", "chord"),
    ("fit a decision tree on this dataset and show it", "decision_tree_model"),
])
def test_a_picture_this_platform_cannot_draw_gets_a_sentence_saying_so(text, token):
    """Fourteen phrasings in the 206-phrasing survey had no honest no, and the
    model improvised: a `flowchart TD` presented as a wireframe, ASCII art that
    the very prompt governing the answer forbids, a table of room dimensions
    that never said it could not draw the plan, and a markdown calendar headed
    "October 2024" in 2026."""
    i = I.decide(text, has_dataset=("dataset" in text))
    assert i.unsupported_visual == token, f"{text!r} -> {i.action}/{i.rule}"
    assert i.action == "none" and not i.chart_request
    sentence = VIS.refusal_for(token)
    assert sentence and sentence[0].isupper() and sentence.rstrip().endswith(".")
    assert "as an AI" not in sentence


def test_every_refusal_offers_only_something_that_can_be_drawn():
    """`Visual.nearest` is checked against CHART_TYPES rather than written down,
    which is why treemap, sunburst, violin and candlestick stopped refusing the
    day those types landed. A new entry must not undo that."""
    for v in VIS.unsupported():
        assert v.token not in CS.CHART_TYPES, v.token
        assert not v.nearest or v.nearest in CS.CHART_TYPES, (v.token, v.nearest)
        assert v.why and not v.why.endswith("."), v.token


def test_the_noun_phrase_form_keeps_its_refusal():
    """`visuals._ASK_RE` needed a verb, so the honest no was lost exactly where
    the sentence was shortest — and the model happened to refuse well on its
    own, which is its judgement and not this product's guarantee."""
    for text in ("venn diagram of the three plans and what they share",
                 "sankey diagram of how users move between stages",
                 "choropleth of revenue by state",
                 "network diagram from this edge list",
                 "word cloud of the ticket descriptions"):
        assert VIS.asked_for(text) is not None, text
    # …and a REMARK is still a remark.
    for text in ("the wireframe in that doc is unreadable",
                 "the treemap in that paper is unreadable",
                 "we already have a sankey somewhere"):
        assert VIS.asked_for(text) is None, text


def test_a_word_cloud_is_not_a_word_document():
    """`formats.kind_for` read the `word` inside "word cloud" as the Word alias,
    so the refusal was skipped and the turn shipped a .docx FOR A PICTURE — the
    2026-09-16 map-answered-with-a-Word-file incident on a new visual."""
    assert F.kind_for("make a word cloud of the ticket descriptions", [])[1] == "default"
    assert F.explicit_formats("make a word cloud of the ticket descriptions") == []
    i = I.decide("make a word cloud of the ticket descriptions", has_dataset=True, upload_formats=("xlsx",))
    assert i.unsupported_visual == "wordcloud", f"{i.action}/{i.rule} formats={i.formats}"
    assert i.formats == [] and i.action == "none"
    # …and a real Word ask is still a real Word ask.
    assert F.explicit_formats("give me a word document of this") == ["docx"]
    assert F.kind_for("write up the project plan as a document", [])[1] == "document words"


def test_a_devanagari_or_gujarati_compound_is_not_a_geographic_map():
    """`visuals._NOT_A_MAP` guarded the LATIN compounds only, so माइंड मैप —
    Hindi for "mind map" — was answered "I can't draw a map: this platform has
    no geographic chart type", while the identical English sentence drew a real
    `mindmap`."""
    i = I.decide("माइंड मैप बनाओ रोडमैप का")
    assert not i.unsupported_visual, f"-> {i.rule}"
    assert i.diagram == "mindmap", f"-> {i.action}/{i.rule}"
    for compound in ("हीट मैप बनाओ", "रोड मैप दिखाओ", "માઇન્ડ મેપ બનાવો"):
        assert VIS.named_unsupported(compound) is None, compound
    # …and a real map request still is one.
    for real in ("इसे नक्शे पर दिखाओ", "આ નકશા પર બતાવો", "isko map pe dikhao", "plot these records on a map"):
        assert VIS.asked_for(real) is not None and VIS.asked_for(real).token == "map", real


def test_a_tree_map_is_a_chart_type_and_not_a_geographic_map():
    """A PRE-EXISTING bug, measured on ae25da28 before any change here:

        "draw a tree map of revenue by product" -> unsupported-visual:map

    A treemap has been a real chart type since 2026-09-16, but the `map` entry
    is tested first and its lookbehind had no `tree`.
    """
    for text in ("draw a tree map of revenue by product", "show me a tree map view",
                 "value stream map of our release process", "user journey map for a new customer"):
        assert VIS.asked_for(text) is None, text


# --------------------------------------------- the UI's edit box, and a file --

def test_the_open_edit_box_does_not_swallow_a_request_for_a_picture():
    """The UI binds "Edit with a prompt" to EVERY turn while the panel is open,
    so with a report on screen six requests for a diagram silently re-rendered
    that report — 6 of 6, measured 2026-09-28. Remove `artifact_id` and all six
    were already right, so it is the binding and not the words."""
    for text in ("flowchart of the deploy pipeline", "draw a sequence diagram for the login path",
                 "ER diagram of the schema", "make me a mind map of the roadmap now",
                 "draw the architecture diagram of the platform", "timeline of the milestones"):
        i = I.decide(text, artifact_id="art_1", has_artifacts=True, last_turn_is_artifact=True,
                     has_assistant_answer=True, artifact_hints=("report",))
        assert i.action == "none", f"{text!r} -> {i.action}/{i.rule}"
        assert i.rule.startswith("diagram-in-chat:")
    # …and a real edit typed into the same box is still an edit.
    edit = I.decide("make the headings dark blue", artifact_id="art_1", has_artifacts=True,
                    last_turn_is_artifact=True, artifact_hints=("report",))
    assert edit.action == "edit" and edit.rule == "ui-edit"


@pytest.mark.parametrize("text,fmt", [
    ("put a flow chart of the deploy pipeline in a PDF", "pdf"),
    ("write a Word document explaining onboarding, with a flowchart in it", "docx"),
    ("make a PDF report on our architecture with an architecture diagram", "pdf"),
    ("export the roadmap as a pdf with a mind map", "pdf"),
    ("slide deck of the request path, include a sequence diagram", "pptx"),
    ("a docx of the schema with an ER diagram", "docx"),
])
def test_a_diagram_asked_for_inside_a_file_still_makes_the_file(text, fmt):
    """The diagram rule must stand aside when the deliverable is a document.
    This routing was already 7/7 and the gap was downstream, so the fix must not
    disturb it."""
    i = I.decide(text)
    assert i.action != "none", f"{text!r} -> {i.action}/{i.rule}"
    assert fmt in i.formats, f"{text!r} -> {i.formats}"


def test_a_diagram_added_to_an_existing_report_is_an_edit():
    i = I.decide("add a process flow diagram to that report", has_artifacts=True,
                 last_turn_is_artifact=True, has_assistant_answer=True, artifact_hints=("report",))
    assert i.action == "edit", f"-> {i.action}/{i.rule}"


def test_a_format_named_as_the_source_is_not_a_file_ask():
    """"org chart of the team from the names in this sheet" read as explicit
    xlsx, so every guard thought a workbook had been asked for."""
    low = _rules_view("org chart of the team from the names in this sheet")
    assert F.explicit_formats(low) == ["xlsx"], "the raw reading is still the raw reading"
    assert F.explicit_formats(PIC.without_source_clauses(low)) == []
    i = I.decide("org chart of the team from the names in this sheet",
                 has_dataset=True, upload_formats=("xlsx",))
    assert i.action == "none" and i.diagram == "flowchart", f"-> {i.action}/{i.rule}"


# ------------------------------------------------ the two mandatory-proof sets --

def test_the_authored_chart_requests_keep_reaching_a_chart():
    """The product's own 77 rows, through the live `decide`, with a table bound.

    43 of 77 reached a chart on ae25da28; 70 reach one here. The gate is the
    FLOOR, because the failure mode of this whole change is breaking real
    charts — and CI could not see it before: the fixture's own tests resolve
    every oracle offline and score VALUES, not routing.
    """
    from tests.fixtures.chart_requests import REQUESTS

    reached = sum(1 for r in REQUESTS
                  if (lambda i: i.action != "none" and i.chart_request)(
                      I.decide(r["text"], has_dataset=True, upload_formats=("xlsx",))))
    assert len(REQUESTS) == 77
    assert reached >= 70, f"only {reached}/77 authored chart requests reach a chart (was 43 on ae25da28)"


def test_no_authored_chart_request_is_read_as_a_diagram():
    """Nothing in the 77 may be claimed by the diagram vocabulary. Measured: 0."""
    from tests.fixtures.chart_requests import REQUESTS

    claimed = [r["id"] for r in REQUESTS
               if PIC.diagram_ask(_rules_view(r["text"]), has_dataset=True) is not None]
    assert claimed == [], claimed


def test_a_remark_about_a_chart_is_not_a_request_for_one():
    """`chart_type_noun_ask` reads a chart type named as a bare noun, which is
    also how people TALK about a chart that already exists. Measured before the
    clause-end guard: "the bar chart is wrong" and "the line chart looks off"
    both opened a chart job."""
    for text in ("the bar chart is wrong", "the line chart looks off",
                 "explain the bar chart you drew", "the bar chart you drew is unreadable",
                 "no chart please", "what is in the bar chart"):
        i = I.decide(text, has_dataset=True, upload_formats=("xlsx",))
        assert i.action == "none", f"{text!r} -> {i.action}/{i.rule}"


def test_chart_type_noun_ask_is_not_part_of_chart_signal():
    """Rule 3 of the brief: do not widen `chart_signal` to catch more. The bare
    type noun is read only by `_dataset_ask`, where a table is actually bound."""
    for text in ("Stacked bar of status broken down by priority.", "Funnel of the conversion stages.",
                 "Headcount per department as a pie."):
        assert LX.chart_type_noun_ask(text) is True, text
        assert LX.chart_signal(text) is False, (
            f"{text!r} leaked into chart_signal; with no table bound that is a wrong lane"
        )
        assert I.decide(text).action == "none", text


# ------------------------------------------------------- the module's shape --

def test_every_subject_names_a_head_the_browser_draws():
    heads = re.search(r"const DIAGRAM_HEADS = \[(.*?)\];",
                      (Path(__file__).resolve().parents[2] / "frontend/lib/mermaid.ts").read_text(encoding="utf-8"),
                      re.S)
    assert heads
    known = {h.strip().strip("'\"") for h in heads.group(1).replace("\n", " ").split(",") if h.strip()}
    for d in PIC.SUBJECTS:
        assert d.head in known, (d.token, d.head)
        assert d.phrase.startswith(("a ", "an ")), d.token
        re.compile(d.pattern)


def test_the_subject_tokens_are_unique():
    tokens = [d.token for d in PIC.SUBJECTS]
    assert len(tokens) == len(set(tokens)), [t for t in tokens if tokens.count(t) > 1]


def test_sankey_and_map_are_refusals_and_not_diagrams():
    """The browser has a `sankey` head, but every way people ask for one asks
    for a quantity moving through rows of a table and there is no chart type for
    it. Naming it a diagram here would buy a picture of nothing."""
    for text in ("sankey diagram of how users move between stages", "draw a sankey of the energy flow",
                 "plot these records on a map"):
        assert PIC.diagram_ask(text) is None, text
        assert VIS.asked_for(text) is not None, text


def test_a_flow_that_is_a_QUANTITY_stays_in_the_data_lane():
    """A cash flow, a fund flow and a capital flow are measured amounts, not
    sequences of steps, so "cash flow chart for the year" is a real chart of
    real numbers. Found by the parallel `fix/flow-chart-is-a-diagram` branch,
    which names the authored fixture rows behind it (c01 "Waterfall chart of the
    cash flow items." and hi05, the same request in Hindi). Measured here before
    the guard: "cash flow chart for the year" was read as a flow chart.

    A WORKFLOW is the opposite case and is exactly a sequence of steps — and
    "workflow" has no word boundary before its "flow", so it needs its own arm.
    """
    for quantity in ("cash flow chart for the year", "cash flow graph of Q1", "fund flow chart",
                     "money flow chart of the accounts", "capital flow chart",
                     "net flow chart of the warehouse"):
        assert PIC.diagram_ask(_rules_view(quantity)) is None, quantity
        assert LX.chart_signal(_rules_view(quantity)) is True, (
            f"{quantity!r} lost its chart reading; it IS a chart of numbers"
        )
    for process in ("workflow chart of the approval", "workflow diagram of the approval",
                    "draw our workflow", "work flow chart of onboarding"):
        got = PIC.diagram_ask(_rules_view(process))
        assert got is not None and got.token == "flow_chart", process

    # …and the two fixture rows themselves still plot.
    for row in ("Waterfall chart of the cash flow items.", "नकदी प्रवाह का वॉटरफॉल चार्ट बनाइए"):
        i = I.decide(row, has_dataset=True, upload_formats=("xlsx",))
        assert i.action == "create" and i.chart_request, f"{row!r} -> {i.action}/{i.rule}"


# ---------------------------------- the picture INSIDE a generated file --

def test_the_three_kinds_that_are_node_and_edge_graphs_are_drawn():
    """A generated file could hold only a `flowchart`: `_DIR_RE` accepted
    `flowchart`/`graph`, so a source opening `erDiagram`, `stateDiagram-v2` or
    `mindmap` refused at its first line and md_import substituted a callout.
    "a docx of the schema with an ER diagram" routed perfectly and then shipped
    an apology where the picture goes."""
    from app.artifacts import spec as S
    from app.artifacts.render import diagrams as D

    cases = {
        "erDiagram\n    USERS ||--o{ SESSIONS : has\n    SESSIONS ||--o{ MESSAGES : contains": (3, 2),
        "stateDiagram-v2\n    [*] --> Queued\n    Queued --> Running: picked up\n    Running --> [*]": (4, 3),
        "mindmap\n  root((Roadmap))\n    Q1\n      Upload reliability\n    Q2": (4, 3),
    }
    for src, (nodes, edges) in cases.items():
        fields = D.parse_mermaid(src)
        assert fields, src.splitlines()[0]
        S.Diagram(**fields)                       # the strict model must accept it
        assert (len(fields["nodes"]), len(fields["edges"])) == (nodes, edges), src.splitlines()[0]


def test_a_mindmap_label_is_not_truncated_to_its_last_word():
    """A bug the rendered PNG showed and no assertion in the parser would have.

    `_MIND_RE` had an OPTIONAL id in front of an OPTIONAL bracket, so the id
    group matched the first WORD of a bracket-less label: "Upload reliability"
    became a node called "reliability". Every multi-word branch of every mind
    map was drawn under its last word.
    """
    from app.artifacts.render import diagrams as D

    fields = D.parse_mermaid(
        "mindmap\n  root((Roadmap))\n    Q1\n      Upload reliability\n      Fast lane\n"
        "    Q2\n      Diagrams in files\n    Q3\n      Public API"
    )
    assert fields
    labels = [n["label"] for n in fields["nodes"]]
    assert labels == ["Roadmap", "Q1", "Upload reliability", "Fast lane", "Q2",
                      "Diagrams in files", "Q3", "Public API"], labels
    # …and the bracketed `id[Label]` form still works.
    f2 = D.parse_mermaid("mindmap\n  root((Product))\n    a[Chat]\n    b[Upload reliability]")
    assert [n["label"] for n in f2["nodes"]] == ["Product", "Chat", "Upload reliability"]


def test_the_kinds_a_file_cannot_hold_say_which_one_and_why():
    """"Diagram omitted / A diagram in the answer was not reproduced in this
    document" was true and useless: a reader could not tell whether the model
    had failed, the document had, or the platform simply cannot put that kind of
    picture in a file. It is the third."""
    from app.artifacts import md_import as M
    from app.artifacts.render import diagrams as D

    for src, kind in (("sequenceDiagram\n    A->>B: x", "Sequence diagram"),
                      ("gantt\n  title P\n  section A\n  T :a1, 2026-01-01, 30d", "Gantt chart"),
                      ("kanban\n  Todo\n    A", "Kanban board")):
        doc, notes = M.markdown_to_document("# T\n\n```mermaid\n" + src + "\n```\n")
        callouts = [b for b in doc.blocks if type(b).__name__ == "Callout"]
        assert len(callouts) == 1, src.splitlines()[0]
        assert callouts[0].title == f"{kind} not included", callouts[0].title
        assert "drawn in the answer above" in callouts[0].text
        assert notes and kind.lower() in notes[0].lower()
    # every head the browser draws has a reason written for it
    import re as _re
    from pathlib import Path as _P
    heads = _re.search(r"const DIAGRAM_HEADS = \[(.*?)\];",
                       (_P(__file__).resolve().parents[2] / "frontend/lib/mermaid.ts").read_text(encoding="utf-8"),
                       _re.S).group(1)
    for name in [h.strip().strip("'\"") for h in heads.replace("\n", " ").split(",") if h.strip()]:
        if name in ("flowchart", "graph") or name in D.TRANSLATABLE:
            continue
        assert name in D.UNTRANSLATABLE_REASON, f"{name} would get the anonymous callout"


def test_the_role_palette_still_passes_the_validator_on_paper():
    """This branch routes far more requests into the role-coloured diagram, so
    the palette is re-checked rather than assumed. Re-run 2026-09-28 with the
    dataviz validator, light, surface #fcfcfb: ALL CHECKS PASS. The two warnings
    (CVD ΔE 7.1 deutan on external<->model; #E07B00 contrast 2.92:1) are legal
    only WITH secondary encoding, and this asserts the encoding is there."""
    from app.artifacts import spec as S
    from app.artifacts.render import diagrams as D

    assert list(D.ROLE_COLOURS) == list(D.DIAGRAM_ROLES) == ["service", "store", "model", "external"]
    assert S.DiagramNode.model_fields["label"].metadata, "a label must be required, or colour stands alone"
    # a node's label is never empty, so identity is never colour-alone
    fields = D.parse_mermaid('flowchart TD\n  A["Client"]:::external --> B["Gateway"]:::service')
    assert fields and all(n["label"] for n in fields["nodes"])
    # and the key names only the roles the diagram actually declares
    assert D._legend_for(S.Diagram(**fields)) == ("service", "external")


# ------------------------------------- the grid the parallel branch built --

#: `fix/flow-chart-is-a-diagram` was built in parallel on the same `_CHART_RE`,
#: and its test file crosses ten ask verbs with eighteen diagram nouns. Its
#: assertions are written against that branch's own helpers
#: (`lexicon.diagram_signal`, `lexicon.without_diagram_phrases`), which do not
#: exist here — but the BEHAVIOUR they pin does, and the grid is a better
#: vocabulary check than anything written from this side alone. Run through this
#: tree's `decide` it found two nouns this module did not carry: `process chart`
#: (a process needs no "flow" after it) and `organizational chart` (the
#: adjective, beside the noun that was already there). 20 of 180 asked for a
#: chart; both are fixed and it is 0 of 180 now. Kept here so the integrator can
#: drop one of the two branches and lose nothing.
_ASK_VERBS = ("make a", "draw a", "create a", "give me a", "generate a", "build a",
              "i need a", "can you make a", "please draw a", "show me a")
_DIAGRAM_NOUNS = (
    "flow chart", "flowchart", "flow-chart", "org chart", "org-chart",
    "organisation chart", "organization chart", "organisational chart",
    "organizational chart", "process chart", "process flow", "data flow diagram",
    "sequence diagram", "state diagram", "mind map", "dependency graph",
    "call graph", "swimlane diagram",
)


@pytest.mark.parametrize("noun", _DIAGRAM_NOUNS)
def test_the_parallel_branchs_grid_never_asks_for_a_table(noun):
    for verb in _ASK_VERBS:
        text = f"{verb} {noun} of the API which connects to the DB"
        i = I.decide(text)
        assert i.action == "none", f"{text!r} -> {i.action}/{i.rule}"
        assert not i.chart_request, f"{text!r} claimed a chart of data"
        assert i.diagram, f"{text!r} made no file but named no diagram either"
