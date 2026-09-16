"""Track 3 of the 2026-09-16 chart incident: say plainly what cannot be drawn.

The production turn. A person pasted nothing — the table was in the
assistant's OWN previous answer (Rank | State | Count (Approx) | % of Total
Records) — and asked twice to plot it on a map. There is no geographic chart
type in chart_spec.CHART_TYPES, no geo dependency is installed offline, and
the source file's PersonMailingLatitude / PersonMailingLongitude are null in
every row. The platform answered with prose inside a Word file and a PDF
nobody had asked for.

What is checked here:
  a) the unsupported list is DERIVED from CHART_TYPES, never written twice;
  b) the intent gate answers such a turn in chat and opens no job;
  c) the sentence is written by code — what, why, and the nearest real view;
  d) a column empty in every row raises the chart error instead of zeros;
  e) the capability line carves out the real limit, and the denial backstop
     no longer reads an honest refusal about a map as "the model denied
     making a file".
"""
from __future__ import annotations

import asyncio

import pytest

from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts import intent as I
from app.artifacts import visuals as V
from app.artifacts.compose import DataTable
from app.engines import capability as cap

# The incident's table, as it stood in the assistant's answer.
INCIDENT_TABLE = DataTable(
    id="answer1",
    title="Records by state",
    columns=["Rank", "State", "Count (Approx)", "% of Total Records"],
    rows=[
        [1, "Texas", 21, "13.3%"],
        [2, "Missouri", 11, "7.0%"],
        [3, "Illinois", 9, "5.7%"],
        [4, "California", 8, "5.1%"],
        [5, "New Jersey", 7, "4.4%"],
        [6, "Other 25 States", "~67", "42.4%"],
    ],
)

INCIDENT_ANSWER_MD = """### Records by state

| Rank | State | Count (Approx) | % of Total Records |
| --- | --- | --- | --- |
| 1 | Texas | 21 | 13.3% |
| 2 | Missouri | 11 | 7.0% |
| 3 | Illinois | 9 | 5.7% |
| 4 | California | 8 | 5.1% |
| 5 | New Jersey | 7 | 4.4% |
| 6 | Other 25 States | ~67 | 42.4% |

That is the distribution across the 30 distinct states in the export.
"""


# --------------------------------------------------- (a) the shared list --


def test_unsupported_is_exactly_named_minus_chart_types():
    """No second list of what IS supported: a name is unsupported exactly
    when its chart-type token is missing from CHART_TYPES."""
    for visual in V.unsupported():
        assert visual.token not in CS.CHART_TYPES
    assert {v.token for v in V.unsupported()} == {v.token for v in V._NAMED} - set(CS.CHART_TYPES)
    assert V.supported() == tuple(CS.CHART_TYPES)


def test_a_name_that_becomes_a_chart_type_stops_being_a_refusal(monkeypatch):
    """Track 4's list is the source of truth: the day a name lands in
    CHART_TYPES it is a chart request again, with no edit here."""
    assert V.asked_for("make a word cloud of the feedback").token == "wordcloud"
    monkeypatch.setattr(CS, "CHART_TYPES", CS.CHART_TYPES + ("wordcloud",))
    assert V.asked_for("make a word cloud of the feedback") is None
    assert "wordcloud" not in {v.token for v in V.unsupported()}


@pytest.mark.parametrize("text", [
    # Track 4 landed these four while this file was being written, and the
    # derived list picked them up on the same import: they are charts now,
    # so refusing them would be the platform lying the other way round.
    "show a treemap of spend",
    "plot a violin plot of salary by team",
    "draw a sunburst of the product tree",
    "give me a candlestick chart of the price",
])
def test_the_types_that_have_since_landed_are_no_longer_refused(text):
    assert V.asked_for(text) is None
    assert I.decide(text).unsupported_visual == ""


def test_every_nearest_view_offered_is_itself_drawable():
    for visual in V.unsupported():
        assert visual.nearest == "" or visual.nearest in CS.CHART_TYPES


@pytest.mark.parametrize("text,token", [
    ("show it on a map", "map"),
    ("plot this on a map", "map"),
    ("can you plot these states on a map?", "map"),
    ("इसे नक्शे पर दिखाओ", "map"),
    ("make a choropleth of records by state", "choropleth"),
    ("draw a sankey of the funnel stages", "sankey"),
    ("make a word cloud of the feedback", "wordcloud"),
    ("draw a network diagram of how the services talk", "network"),
    ("show me a venn diagram of the two lists", "venn"),
])
def test_named_visuals_this_platform_cannot_draw(text, token):
    visual = V.asked_for(text)
    assert visual is not None and visual.token == token


@pytest.mark.parametrize("text", [
    # Supported, and must never be mistaken for the geographic family.
    "draw a heat map of sales by region",
    "give me a heatmap of tickets by day",
    "visualise this table on pie chart",
    "draw a bar chart of records by state",
    # `map` is a verb and a word in other nouns.
    "map the columns to the schema and create a PDF",
    "show me the roadmap for Q3",
    "our site map is broken",
    "a mind map of the ideas",
    "mapping the fields took an hour",
])
def test_words_that_are_not_a_request_for_an_undrawable_visual(text):
    assert V.asked_for(text) is None


def test_a_name_without_an_ask_is_a_remark_not_a_request():
    assert V.named_unsupported("the word cloud in that paper is unreadable") is not None
    assert V.asked_for("the word cloud in that paper is unreadable") is None


# ------------------------------------------- (b) no document job is opened --


@pytest.mark.parametrize("kwargs", [
    {},
    # The incident's own context: the earlier chart ask had already produced
    # a file, so the conversation holds one and the last turn is its card.
    dict(has_artifacts=True, has_assistant_answer=True, last_turn_is_artifact=True),
    dict(has_assistant_answer=True),
])
def test_a_map_request_opens_no_job_whatever_the_conversation_holds(kwargs):
    got = I.decide("show it on a map", **kwargs)
    assert got.action == "none" and got.wants_file is False
    assert got.rule == "unsupported-visual:map"
    assert got.unsupported_visual == "map"


def test_the_classifier_is_never_asked_to_second_guess_the_refusal():
    """`_should_consult` decides whether the LLM may overrule the rules. A
    visual with no chart type cannot become a file whatever it answers."""
    got = I.decide("plot this on a map")
    assert I._should_consult(got, "plot this on a map") is False


def test_decide_with_hook_keeps_the_refusal_even_with_a_create_happy_hook():
    async def hook(text, **kw):  # a classifier that says "file" to everything
        return I.ArtifactIntent("create", formats=["pdf"], rule="model")

    got = asyncio.run(I.decide_with_hook("plot this on a map", hook, has_assistant_answer=True))
    assert got.action == "none" and got.unsupported_visual == "map"


def test_a_request_that_also_names_a_real_deliverable_is_still_that_deliverable():
    """"Put the map in a PDF report" asked for a report. The refusal belongs
    where the chart is refused, not in place of the file they asked for."""
    got = I.decide("put the map in a PDF report")
    assert got.action == "create" and got.unsupported_visual == ""


# ------------------------------------------------- (c) the code's sentence --


def test_the_sentence_names_the_visual_the_reason_and_the_nearest_real_view():
    history = [{"role": "user", "content": "how many records per state?"},
               {"role": "assistant", "content": INCIDENT_ANSWER_MD}]
    said = V.refusal_for("map", history=history)
    assert said.startswith("I can't draw a map")
    assert "no geographic chart type" in said
    # The nearest view is named from the answer's own table.
    assert "Count (Approx) by State as a bar chart" in said
    assert "I will draw it" in said
    # Nothing in it invites a document.
    for word in ("Word", "PDF", "docx", "document"):
        assert word not in said


def test_the_sentence_still_offers_a_view_when_there_is_no_table_to_point_at():
    said = V.refusal_for("map", history=[])
    assert said.startswith("I can't draw a map")
    assert "the same values by category as a bar chart" in said


def test_a_visual_with_no_close_substitute_says_so_rather_than_inventing_one():
    said = V.refusal_for("venn", history=[])
    assert "Venn diagram" in said and "Nothing this platform draws is close to it" in said


def test_the_nearest_columns_skip_the_rank_and_the_percentage():
    measure, category = V._columns_of(INCIDENT_TABLE)
    assert (measure, category) == ("Count (Approx)", "State")


def test_the_sentence_is_not_read_as_a_denial_of_making_files():
    """(e) The backstop's whole job is to catch "as an AI I cannot make a
    file" and make the file. This sentence is not that."""
    assert cap.denial_in(V.refusal_for("map", history=[])) is False
    assert cap.denial_in(V.refusal_for("venn", history=[])) is False


# ------------------------------------------ (d) an all-empty column refuses --


def _chart(**raw):
    raw.setdefault("title", "t")
    return CS.Chart.model_validate(raw)


EMPTY_LATLONG = DataTable(
    id="upload1",
    title="contacts.csv",
    columns=["State", "PersonMailingLatitude", "PersonMailingLongitude"],
    rows=[["Texas", None, ""], ["Missouri", "", None], ["Illinois", None, None]],
)


def test_a_column_empty_in_every_row_raises_instead_of_plotting_zeros():
    with pytest.raises(CD.ChartDataError) as exc:
        CD.compute(_chart(type="bar", data=dict(table_id="upload1", x="State", y=["PersonMailingLatitude"])), EMPTY_LATLONG)
    assert "'PersonMailingLatitude' is empty in every row" in str(exc.value)


def test_the_message_is_shown_in_place_of_the_chart_not_raised_at_the_caller():
    chart, notes, message = CD.resolve_chart(
        _chart(type="bar", data=dict(table_id="upload1", x="State", y=["PersonMailingLongitude"])), [EMPTY_LATLONG])
    assert chart is None
    assert "empty in every row" in message and message in notes


def test_a_column_with_numbers_still_plots():
    """The whitelist that let "empty" through is not simply removed: a column
    typed `empty` from a blank sample must still plot when the rows the chart
    uses have numbers in them."""
    table = DataTable(id="upload1", title="t", columns=["State", "Count"], rows=[["Texas", 21], ["Missouri", 11]])
    got = CD.compute(_chart(type="bar", data=dict(table_id="upload1", x="State", y=["Count"])), table)
    assert got.series[0].values == [21, 11]


def test_counting_rows_is_untouched_by_the_empty_check():
    got = CD.compute(_chart(type="bar", data=dict(table_id="upload1", x="State", agg="count")), EMPTY_LATLONG)
    assert got.series[0].values == [1, 1, 1]


# ------------------------------------------------- (e) the capability line --


def test_the_capability_line_names_the_real_limit_and_forbids_the_document():
    line = cap.CAPABILITY_LINE
    assert "map" in line and "cannot draw" in line
    assert "do not make a document instead of saying it" in line
    # Still the file promise it was written for.
    assert "Do not say you cannot create or attach files" in line


def test_the_limits_sentence_is_built_from_the_unsupported_list():
    said = V.limits_sentence()
    for visual in V.unsupported():
        name = visual.phrase.split(" ", 1)[1] if visual.phrase.startswith(("a ", "an ")) else visual.phrase
        assert name in said


#: Every one of these is read as "the model denied making a file" at
#: 5dcb7fc, which is what sends the backstop off to build the Word file and
#: the PDF the 2026-09-16 turn came back with. Each is TRUE: there is no
#: geographic chart type, so there is no map file to attach either.
HONEST_VISUAL_REFUSALS = [
    "I cannot create a map file for you — this platform has no geographic chart type.",
    "I am unable to generate a map document; there is no geographic chart type here.",
    "I can't produce a downloadable map, because this platform has no geographic chart type.",
    "A choropleth file cannot be created from here; the platform has no geographic chart type.",
    "As a text-based assistant I cannot draw a map, and there is no map file I can attach.",
    "I can't export the map as a PDF because no geographic chart type exists here.",
    "I can't put these counts on a map — this platform has no geographic chart type.",
]


@pytest.mark.parametrize("answer", HONEST_VISUAL_REFUSALS)
def test_an_honest_refusal_about_a_visual_is_not_a_denial_of_making_files(answer):
    assert cap.denial_in(answer) is False


@pytest.mark.parametrize("answer", [
    "I cannot directly create or send a `.docx` file because I am an AI running in a text-based interface.",
    "I'm unable to attach a PDF to this chat.",
    # The carve-out is per SENTENCE: a real denial next to an honest one still counts.
    "I can't draw a map here. Separately, I cannot create a Word document for you.",
])
def test_a_real_denial_of_a_file_is_still_caught(answer):
    assert cap.denial_in(answer) is True
