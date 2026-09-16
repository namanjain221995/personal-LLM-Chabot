"""chart_choice: the chart type is decided by the table's SHAPE, and a type
the person named wins whenever the shape can carry it.

Production 2026-09-16: "visualise this table on pie chart" over a six-state
count table drew seven equal slices, and nothing told the person what had been
drawn. Every case here is a table small enough that the right answer is
obvious by reading it.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.artifacts import chart_choice as CC
from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts.compose import DataTable


def table(id: str, columns, rows, title: str = "") -> DataTable:
    return DataTable(id=id, title=title or id, columns=list(columns), rows=[list(r) for r in rows])


def chart(**raw) -> CS.Chart:
    raw.setdefault("title", "t")
    return CS.Chart.model_validate(raw)


def shape(t, **binding) -> CC.Shape:
    sh = CC.shape_of(chart(type=binding.pop("type", "bar"), data=dict(table_id=t.id, **binding)), t)
    assert sh is not None
    return sh


# ------------------------------------------------------------- the tables --

#: The incident's own table, verbatim (the summary row included).
STATES = table(
    "answer1",
    ["Rank", "State", "Count (Approx)", "% of Total Records"],
    [
        [1, "Texas", 21, "2.57%"],
        [2, "Missouri", 11, "1.35%"],
        [3, "Illinois", 9, "1.10%"],
        [4, "California", 8, "0.98%"],
        [5, "New Jersey", 7, "0.86%"],
        ["—", "Other 25 States", "~67", "8.20%"],
        ["Total", "Distinct States", 30, "3.67%"],
    ],
)

MANY_STATES = table(
    "answer2",
    ["State", "Count"],
    [[f"State {i:02d}", 100 - i] for i in range(40)],
)

MONTHLY = table(
    "upload1",
    ["Date", "Amount"],
    [[_dt.date(2026, m, 1).isoformat(), 1000 + 37 * m] for m in range(1, 13)],
)

MONTHLY_BY_REGION = table(
    "upload2",
    ["Date", "Region", "Amount"],
    [[_dt.date(2026, m, 1).isoformat(), r, 100 * m + i] for m in range(1, 13) for i, r in enumerate(("North", "South", "East"))],
)

HEIGHT_WEIGHT = table(
    "upload3",
    ["Height", "Weight"],
    [[150 + i, 45 + i * 0.9] for i in range(30)],
)

NOISE_XY = table(
    "upload4",
    ["Height", "Weight"],
    [[150 + i, 60 + (i * 7919 % 23) - 11] for i in range(30)],
)

SALARIES = table("upload5", ["Salary"], [[30000 + (i * 4409 % 50000)] for i in range(80)])

SALARY_BY_TEAM = table(
    "upload6",
    ["Team", "Salary"],
    [[t, 30000 + (i * 4409 % 50000)] for t in ("Sales", "Support", "Field") for i in range(20)],
)

TICKET_GRID = table(
    "upload7",
    ["Status", "Priority", "Hours"],
    [[s, p, 3 + i] for i, (s, p) in enumerate(
        [(s, p) for s in ("New", "Working", "Escalated", "Closed") for p in ("Low", "Medium", "High", "Urgent")]
    )] * 2,
)

CASH_FLOW = table(
    "upload8",
    ["Line", "Amount"],
    [["Opening", 500], ["Sales", 1200], ["Refunds", -300], ["Costs", -450], ["Other", 80]],
)

#: Salesforce's own Case Status picklist — the only orders
#: core.chart_decision.trusted_stage_order will accept.
STAGES = table("upload9", ["Status", "Deals"], [["New", 400], ["Working", 210], ["Escalated", 90], ["Closed", 40]])

SCHEDULE = table(
    "upload10",
    ["Task", "Start", "End"],
    [["Design", "2026-01-05", "2026-02-10"], ["Build", "2026-02-11", "2026-04-01"], ["Ship", "2026-04-02", "2026-04-20"]],
)

SIZED = table("upload11", ["Spend", "Revenue", "Accounts"], [[10 * i, 30 * i + 5, i + 1] for i in range(1, 20)])


# ------------------------------------------------------- one test per rule --


def test_few_categories_that_add_up_are_a_pie():
    sh = shape(STATES, x="State", y=["Count (Approx)"])
    # The "Total | Distinct States | 30" row is a summary, not a sixth state.
    assert sh.n_categories == 6 and sh.parts_of_whole and sh.one_row_per_category
    got = CC.recommend(sh)
    assert got.type == "pie"
    assert "6 values" in got.reason and "Count (Approx)" in got.reason


def test_many_categories_become_a_horizontal_bar_ranked_by_value():
    sh = shape(MANY_STATES, x="State", y=["Count"])
    assert sh.n_categories == 40
    got = CC.recommend(sh)
    assert got.type == "horizontal_bar" and "40 values" in got.reason
    # "sorted by value, tail in Other" is chart_data's own `sort="auto"` plus
    # its MAX_CATEGORIES fold, which the reason above promises.
    c, _, msg = CD.resolve_chart(chart(type="horizontal_bar", data=dict(table_id="answer2", x="State", y=["Count"])), [MANY_STATES])
    assert c is not None, msg
    assert c.series[0].values == sorted(c.series[0].values, reverse=True)
    wide = table("answer3", ["State", "Count"], [[f"State {i:03d}", 1000 - i] for i in range(CD.MAX_CATEGORIES + 50)])
    c, notes, msg = CD.resolve_chart(chart(type="horizontal_bar", data=dict(table_id="answer3", x="State", y=["Count"])), [wide])
    assert c is not None, msg
    assert len(c.categories) == CD.MAX_CATEGORIES and c.categories[-1] == CD.OTHER


def test_a_date_x_with_one_measure_is_a_line():
    got = CC.recommend(shape(MONTHLY, x="Date", y=["Amount"]))
    assert got.type == "line" and "Date is a date" in got.reason


def test_a_date_x_with_several_measures_is_a_multi_series_line():
    t = table("u", ["Date", "Amount", "Units"], [[r[0], r[1], r[1] // 10] for r in MONTHLY.rows])
    got = CC.recommend(shape(t, x="Date", y=["Amount", "Units"]))
    assert got.type == "line" and "2 measures" in got.reason


def test_parts_of_a_whole_over_time_are_a_stacked_area():
    sh = shape(MONTHLY_BY_REGION, x="Date", y=["Amount"], group_by="Region")
    assert sh.parts_of_whole
    got = CC.recommend(sh)
    assert got.type == "stacked_area" and "Region" in got.reason


def test_two_measures_with_no_category_are_a_scatter_and_a_weak_fit_gets_no_trend_line():
    tight = CC.recommend(shape(HEIGHT_WEIGHT, x="Height", y=["Weight"]))
    assert tight.type == "scatter" and tight.trendline is True and "r = +1.00" in tight.reason
    loose = CC.recommend(shape(NOISE_XY, x="Height", y=["Weight"]))
    assert loose.type == "scatter" and loose.trendline is False


def test_one_measure_alone_is_a_histogram_and_one_measure_by_group_is_a_box():
    assert CC.recommend(shape(SALARIES, y=["Salary"])).type == "histogram"
    sh = shape(SALARY_BY_TEAM, x="Team", y=["Salary"], agg="none")
    assert sh.rows_per_category == 20
    assert CC.recommend(sh).type == "box"


def test_a_category_by_group_matrix_of_one_measure_is_a_heatmap():
    sh = shape(TICKET_GRID, x="Status", y=["Hours"], group_by="Priority")
    assert sh.matrix and (sh.n_categories, sh.n_groups) == (4, 4)
    assert CC.recommend(sh).type == "heatmap"


def test_contributions_to_a_net_total_are_a_waterfall():
    sh = shape(CASH_FLOW, x="Line", y=["Amount"])
    assert sh.mixed_signs
    got = CC.recommend(sh)
    assert got.type == "waterfall" and "net total" in got.reason


def test_stages_that_only_shrink_are_a_funnel_and_a_ranked_table_is_not():
    got = CC.recommend(shape(STAGES, x="Status", y=["Deals"]))
    assert got.type == "funnel" and "stage order" in got.reason
    # A table sorted by value is monotonic too; it is not a process, so the
    # labels must belong to an order somebody else defined.
    ranked = table("u2", ["State", "Count"], [["Texas", 21], ["Missouri", 11], ["Illinois", 9], ["California", 8]])
    assert shape(ranked, x="State", y=["Count"]).stage_order is False
    assert CC.recommend(shape(ranked, x="State", y=["Count"])).type != "funnel"


def test_a_label_with_a_start_and_an_end_is_a_gantt_and_a_size_column_is_a_bubble():
    assert CC.recommend(shape(SCHEDULE, label="Task", start="Start", end="End")).type == "gantt"
    assert CC.recommend(shape(SIZED, x="Spend", y=["Revenue"], size="Accounts")).type == "bubble"


def test_a_grouped_category_that_does_not_add_up_stays_a_bar():
    t = table("u3", ["Team", "Region", "Avg Handling Time"],
              [[a, b, 4 + i] for i, (a, b) in enumerate([(a, b) for a in ("Sales", "Support") for b in ("North", "South")])])
    sh = shape(t, x="Team", y=["Avg Handling Time"], group_by="Region")
    assert sh.parts_of_whole is False  # an average is not a part of anything
    assert CC.recommend(sh).type == "bar"


# --------------------------------------------- a type the person did name --


def test_a_named_pie_the_shape_carries_is_obeyed():
    c = chart(type="pie", data=dict(table_id="answer1", x="State", y=["Count (Approx)"]))
    got = CC.choose(c, STATES, "visualise this table on pie chart")
    assert got is not None and got.type == "pie" and got.note == ""


def test_a_named_donut_wins_over_the_chooser_s_own_pie():
    c = chart(type="donut", data=dict(table_id="answer1", x="State", y=["Count (Approx)"]))
    got = CC.choose(c, STATES, "make it a doughnut")
    assert got is not None and got.type == "donut" and got.note == ""


def test_a_named_pie_of_forty_categories_falls_back_and_names_both_types():
    c = chart(type="pie", data=dict(table_id="answer2", x="State", y=["Count"]))
    got = CC.choose(c, MANY_STATES, "show this as a pie chart")
    assert got is not None and got.type == "horizontal_bar"
    assert "a pie" in got.note and "horizontal bar" in got.note and "Other wedge" in got.note


def test_a_named_pie_of_a_time_series_falls_back_and_says_why():
    c = chart(type="pie", data=dict(table_id="upload1", x="Date", y=["Amount"]))
    got = CC.choose(c, MONTHLY, "pie chart of monthly amount")
    assert got is not None and got.type == "line"
    assert "a pie" in got.note and "a line" in got.note and "order they happened in" in got.note


def test_a_named_funnel_whose_stages_are_not_a_process_falls_back():
    ranked = table("u4", ["State", "Count"], [[f"State {i:02d}", 100 - i] for i in range(12)])
    c = chart(type="funnel", data=dict(table_id="u4", x="State", y=["Count"]))
    got = CC.choose(c, ranked, "draw a funnel")
    assert got is not None and got.type == "horizontal_bar" and "stages that only shrink" in got.note


def test_a_pie_of_averages_is_refused_because_averages_do_not_add_up():
    t = table("u5", ["Team", "Avg Resolution Days"], [["Sales", 4.2], ["Support", 6.1], ["Field", 3.3]])
    c = chart(type="pie", data=dict(table_id="u5", x="Team", y=["Avg Resolution Days"]))
    got = CC.choose(c, t, "pie chart please")
    assert got is not None and got.type == "bar" and "do not add up" in got.note


def test_no_named_type_keeps_the_model_s_layout_inside_the_right_family():
    # bar <-> horizontal_bar is a label-length preference, not a different
    # reading of the numbers, so the model's pick survives the chooser's
    # own horizontal_bar for the same 40 categories.
    c = chart(type="bar", data=dict(table_id="answer2", x="State", y=["Count"]))
    assert CC.recommend(shape(MANY_STATES, x="State", y=["Count"])).type == "horizontal_bar"
    assert CC.choose(c, MANY_STATES, "put a chart in the report") is None
    # A pie over a date column is a different reading, and it is replaced.
    c = chart(type="pie", data=dict(table_id="upload1", x="Date", y=["Amount"]))
    got = CC.choose(c, MONTHLY, "put a chart in the report")
    assert got is not None and got.type == "line" and got.note


# ---------------------------------------------------------------- wiring --


def test_repair_binding_applies_the_choice_and_reports_it():
    c = chart(type="pie", data=dict(table_id="upload1", x="Date", y=["Amount"]))
    fixed, notes = CD.repair_binding(c, [MONTHLY], "pie chart of monthly amount")
    assert fixed.type == "line"
    assert any("a pie" in n and "a line" in n for n in notes), notes
    resolved, _, msg = CD.resolve_chart(fixed, [MONTHLY])
    assert resolved is not None and resolved.type == "line", msg


def test_repair_binding_obeys_the_incident_s_own_request():
    c = chart(type="pie", data=dict(table_id="answer1", x="State", agg="count"))
    fixed, notes = CD.repair_binding(c, [STATES], "visualise this table on pie chart")
    assert fixed.type == "pie" and not [n for n in notes if "drawn as" in n]
    resolved, rnotes, msg = CD.resolve_chart(fixed, [STATES])
    assert resolved is not None, msg
    # The count rebind (base branch) plus the total row drop (base branch):
    # six real states with their real counts, no seven equal slices.
    assert dict(zip(resolved.categories, resolved.series[0].values)) == {
        "Other 25 States": 67.0, "Texas": 21.0, "Missouri": 11.0, "Illinois": 9.0, "California": 8.0, "New Jersey": 7.0,
    }


def test_repair_binding_turns_on_a_trend_line_only_for_a_real_fit():
    tight = chart(type="scatter", data=dict(table_id="upload3", x="Height", y=["Weight"]))
    assert CD.repair_binding(tight, [HEIGHT_WEIGHT], "scatter of height and weight")[0].data.trendline is True
    loose = chart(type="scatter", data=dict(table_id="upload4", x="Height", y=["Weight"]))
    assert CD.repair_binding(loose, [NOISE_XY], "scatter of height and weight")[0].data.trendline is False


def test_the_model_guidance_carries_the_same_rule_table():
    guide = CS.prompt_guide("document", [STATES])
    for shape_text, ctype in CC.RULES:
        assert f"- {shape_text} -> {ctype}" in guide
    assert str(CC.NAMED_PIE_CATEGORIES) in guide


def test_a_chart_with_no_binding_or_no_table_is_left_alone():
    assert CC.shape_of(chart(type="pie", categories=["a", "b"], series=[dict(name="s", values=[1, 2])]), STATES) is None
    assert CC.choose(chart(type="pie", data=dict(table_id="answer1", x="State")), None) is None


# ----------------------------------- the words are read, not just scanned --

#: Salesforce's own Opportunity stage picklist. "Sales funnel" is the SUBJECT
#: here; "bar chart" is the type the person actually typed.
SF_STAGES = table("sf", ["Stage", "Deals"], [
    ["Prospecting", 500], ["Qualification", 300], ["Proposal/Price Quote", 120],
    ["Negotiation/Review", 60], ["Closed Won", 25],
])


@pytest.mark.parametrize("text, expected", [
    # A bare subject noun must never beat the type the person literally named.
    ("show our sales funnel as a bar chart", "bar"),
    ("bar chart of pie sales by region", "bar"),
    ("bar chart of the conversion funnel", "bar"),
    ("line chart of the gantt milestones", "line"),
    ("bar chart of heat map coverage", "bar"),
    ("line chart of the waterfall project", "line"),
    # ...and the type named with its own chart word still wins over another's.
    ("a funnel chart, not a bar chart", "funnel"),
    # Everything the shipped behaviour already gets right stays right.
    ("visualise this table on pie chart", "pie"),
    ("stacked bar chart", "stacked_bar"),
    ("100% stacked bar", "percent_stacked_bar"),
    ("stacked horizontal bar chart", "stacked_horizontal_bar"),
    ("horizontal bar chart", "horizontal_bar"),
    ("box plot", "box"),
    ("trend over time", "line"),
    ("as a pie", "pie"),
    ("draw a gantt chart", "gantt"),
    ("make it a doughnut", "donut"),
    ("scatter plot of height and weight", "scatter"),
    ("heat map of tickets", "heatmap"),
    # Ordinary English that happens to contain a chart type's noun is not a
    # request for that type — the person named no chart at all.
    ("put the summary in a text box", None),
    ("on our radar", None),
    ("housing bubble", None),
    ("make the title bigger", None),
])
def test_the_type_the_person_named_wins_over_a_bare_subject_noun(text, expected):
    assert CC.named_type(text) == expected


def test_a_bar_chart_of_a_sales_funnel_is_drawn_as_a_bar():
    # End to end: the shape IS a funnel (a trusted stage order that only
    # shrinks), so the chooser would recommend one — but the person typed
    # "bar chart", and their own words win.
    sh = shape(SF_STAGES, x="Stage", y=["Deals"])
    assert sh.stage_order and CC.recommend(sh).type == "funnel"
    c = chart(type="bar", data=dict(table_id="sf", x="Stage", y=["Deals"]))
    got = CC.choose(c, SF_STAGES, "draw the sales funnel as a bar chart")
    assert got is None or (got.type == "bar" and got.note == "")


# ------------------------------- recommend never names a type it refuses --

#: 10,000 distinct names: one row per category at the DISTINCT_CAP, so
#: rows_per_category reads 25 while every group really holds one row.
ONE_ROW_EACH = table("wide", ["Name", "Score"], [[f"Name {i:05d}", i % 97] for i in range(10_000)])


def test_recommend_never_names_a_type_can_draw_refuses_for_the_same_shape():
    sh = shape(ONE_ROW_EACH, x="Name", y=["Score"], agg="none")
    assert sh.one_row_per_category and sh.n_categories == CC.DISTINCT_CAP
    got = CC.recommend(sh)
    assert CC.can_draw(got.type, sh) == "", f"recommend() named {got.type}, which can_draw refuses"
    assert got.type != "box"
    # ...and the unnamed path of choose() ships nothing can_draw refuses either.
    c = chart(type="bar", data=dict(table_id="wide", x="Name", y=["Score"], agg="none"))
    picked = CC.choose(c, ONE_ROW_EACH, "put a chart in the report")
    if picked is not None:
        assert CC.can_draw(picked.type, sh) == "", picked


# ------------------------------- an edit that is not about charts is left --


def test_an_edit_that_names_no_chart_leaves_an_accepted_chart_alone():
    c = chart(type="line", data=dict(table_id="answer1", x="State", y=["Count (Approx)"]))
    kept, notes = CD.repair_binding(c, [STATES], "make the title bigger", keep_accepted_type=True)
    assert kept.type == "line" and not [n for n in notes if "drawn as" in n], notes
    # An edit that IS about charts keeps the whole benefit of the chooser.
    fixed, notes = CD.repair_binding(c, [STATES], "turn this into a pie chart", keep_accepted_type=True)
    assert fixed.type == "pie"


def test_the_edit_path_does_not_retype_charts_the_instruction_never_mentions():
    import asyncio

    from app.artifacts import spec as S
    from app.engines import artifact as engine

    def doc():
        return S.parse_body("document", {"title": "States", "blocks": [{"type": "chart", "chart": {
            "type": "line", "title": "Records by State",
            "data": {"table_id": "answer1", "x": "State", "y": ["Count (Approx)"]}}}]})

    warned: list = []
    out = asyncio.run(engine._post_process(doc(), [STATES], warned.append, "make the title bigger",
                                           parent=doc(), model_wrote=False))
    assert out.body.blocks[0].chart.type == "line", warned
    assert not [w for w in warned if "drawn as" in w], warned


# ------------------------------- a note never claims a change that is not --


def test_a_note_is_written_only_when_the_type_actually_changed():
    c = chart(type="line", data=dict(table_id="upload1", x="Date", y=["Amount"]))
    kept, notes = CD.repair_binding(c, [MONTHLY], "pie chart of statuses please")
    assert kept.type == "line"
    assert not [n for n in notes if "drawn as a line" in n], notes


# -------------------------- a misspelled column is chart_data's callout --


def test_an_x_column_the_table_does_not_have_is_not_a_reason_to_retype():
    c = chart(type="bar", data=dict(table_id="answer1", x="Zzzqqq", y=["Count (Approx)"]))
    assert CC.shape_of(c, STATES) is None
    kept, notes = CD.repair_binding(c, [STATES], "")
    assert kept.type == "bar" and notes == [], notes
    # resolve_chart still writes the one true sentence about it.
    resolved, _, msg = CD.resolve_chart(kept, [STATES])
    assert resolved is None and "Zzzqqq" in msg
