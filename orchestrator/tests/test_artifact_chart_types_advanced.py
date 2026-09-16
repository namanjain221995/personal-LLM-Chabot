"""The tier-2 types added for the 2026-09-16 chart round: pareto, treemap,
violin, candlestick, sunburst and bullet.

Every number a reader sees is pinned here against a figure computed by hand
from a table small enough to add up in your head, because the whole point of
Chart v2 is that no number on an axis was written by the model. Each type is
also drawn once to a real PNG, so a binding that computes cannot ship with a
renderer that raises.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS

pytest.importorskip("matplotlib")

PREVIEW = Path(os.environ.get(
    "CHART_PREVIEW_DIR",
    "/tmp/claude-1000/-home-techsphere-Documents-project-personal-LLM-Chabot/"
    "c633fe6d-2f75-46c5-90e6-6fe41b6142e1/scratchpad/charts-preview",
))


def table(tid, columns, rows, title=""):
    from app.artifacts.compose import DataTable

    return DataTable(id=tid, title=title or tid, columns=list(columns), rows=[list(r) for r in rows])


def resolve(kind, binding, tables, **over):
    raw = {"type": kind, "title": f"{kind} chart", "data": binding}
    raw.update(over)
    return CD.resolve_chart(CS.Chart.model_validate(raw), tables)


def ok(kind, binding, tables, **over):
    c, notes, msg = resolve(kind, binding, tables, **over)
    assert c is not None, msg
    return c, notes


def refused(kind, binding, tables, **over):
    c, notes, msg = resolve(kind, binding, tables, **over)
    assert c is None, f"expected a refusal, got {getattr(c, 'series', None)}"
    return msg


def draw(chart, name):
    """Render once and keep the picture: a computed chart that cannot be
    drawn is not a chart."""
    from app.artifacts.render import charts

    PREVIEW.mkdir(parents=True, exist_ok=True)
    data = charts.render_png(chart, None, 1400, 850, include_caption=True)
    out = PREVIEW / f"{name}.png"
    out.write_bytes(data)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert 20_000 < len(data) < 4_000_000, len(data)
    from PIL import Image

    with Image.open(out) as im:
        assert im.size[0] >= 600 and im.size[1] >= 350, im.size
    return out


# ------------------------------------------------------------------ tables --

DEFECTS = table("upload_defects", ["Cause", "Count"], [
    ["Scratches", 50], ["Misprint", 25], ["Dents", 15], ["Loose seal", 6], ["Wrong label", 3], ["Other damage", 1],
])

#: The production table of 2026-09-16, verbatim, summary row and all.
STATES = table("answer1", ["Rank", "State", "Count (Approx)", "% of Total Records"], [
    [1, "Texas", 21, "17.21%"], [2, "Missouri", 11, "9.02%"], [3, "Illinois", 9, "7.38%"],
    [4, "California", 8, "6.56%"], [5, "New Jersey", 7, "5.74%"], [6, "Other 25 States", "~67", "54.10%"],
    ["Total", "Distinct States", 30, "3.67%"],
], title="States by record count")

SALES = table("upload_sales", ["Region", "Product", "Amount"], [
    ["East", "Alpha", 10], ["East", "Bravo", 30], ["West", "Alpha", 25],
])

PRICES = table("upload_prices", ["Day", "Open", "High", "Low", "Close"], [
    ["2026-03-02", 10, 12, 9, 11],
    ["2026-03-02", 11, 15, 8, 14],
    ["2026-03-03", 14, 16, 13, 15],
])

SALARIES = table("upload_pay", ["Team", "Salary"], (
    [["Alpha", v] for v in (1, 2, 3, 4, 5, 6)]
    + [["Bravo", v] for v in (10, 20, 30, 40, 50, 60)]
))

TARGETS = table("upload_targets", ["Measure", "Actual", "Target", "Poor", "Fair", "Good"], [
    ["Revenue", 270, 300, 150, 240, 330],
    ["New customers", 88, 80, 40, 64, 96],
    ["Churn saves", 31, 50, 25, 40, 55],
])


# ------------------------------------------------------------------ pareto --


def test_pareto_bars_fall_and_the_line_is_the_running_share():
    """50 + 25 + 15 + 6 + 3 + 1 = 100 defects, so the cumulative line reads
    50, 75, 90, 96, 99, 100 per cent — computed here, not by the model."""
    c, _ = ok("pareto", dict(table_id="upload_defects", x="Cause", y=["Count"]), [DEFECTS])
    assert c.categories == ["Scratches", "Misprint", "Dents", "Loose seal", "Wrong label", "Other damage"]
    assert [round(v) for v in c.series[0].values] == [50, 25, 15, 6, 3, 1]
    line = c.series[1]
    assert line.name == "Cumulative %" and line.axis == "secondary" and line.kind == "line"
    assert [round(v, 6) for v in line.values] == [50.0, 75.0, 90.0, 96.0, 99.0, 100.0]
    draw(c, "pareto")


def test_pareto_forces_the_descending_order_a_pareto_asserts():
    c, notes = ok("pareto", dict(table_id="upload_defects", x="Cause", y=["Count"], sort="x"), [DEFECTS])
    assert c.categories[0] == "Scratches" and c.categories[-1] == "Other damage"
    assert any("largest bar down" in n for n in notes), notes


def test_pareto_caption_names_where_the_line_crosses_eighty():
    c, _ = ok("pareto", dict(table_id="upload_defects", x="Cause", y=["Count"]), [DEFECTS])
    assert "top 3 of 6" in c.caption and "90%" in c.caption, c.caption


def test_pareto_refuses_a_negative_value():
    t = table("t", ["Cause", "Count"], [["A", 10], ["B", -4]])
    msg = refused("pareto", dict(table_id="t", x="Cause", y=["Count"]), [t])
    assert "negative" in msg.lower()


# ----------------------------------------------------------------- treemap --


def test_squarified_rectangles_are_area_proportional():
    """Two hand-checkable layouts: [1, 1] in a 2x1 box is two unit squares,
    and [3, 1] in a 4x1 box is a 3x1 beside a 1x1."""
    from app.artifacts.render.charts import squarified

    assert squarified([1, 1], 0.0, 0.0, 2.0, 1.0) == [(0.0, 0.0, 1.0, 1.0), (1.0, 0.0, 1.0, 1.0)]
    assert squarified([3, 1], 0.0, 0.0, 4.0, 1.0) == [(0.0, 0.0, 3.0, 1.0), (3.0, 0.0, 1.0, 1.0)]


def test_squarified_area_matches_the_value_share_for_every_rectangle():
    from app.artifacts.render.charts import squarified

    values = [50, 25, 15, 6, 3, 1]
    rects = squarified(values, 0.0, 0.0, 100.0, 62.0)
    assert len(rects) == len(values)
    total_area = 100.0 * 62.0
    for v, (_x, _y, w, h) in zip(values, rects):
        assert w * h == pytest.approx(total_area * v / sum(values), rel=1e-9)
    assert sum(w * h for _x, _y, w, h in rects) == pytest.approx(total_area, rel=1e-9)


def test_squarified_covers_the_box_without_overlap():
    from app.artifacts.render.charts import squarified

    rects = squarified([9, 7, 5, 4, 3, 2, 1], 0.0, 0.0, 40.0, 25.0)
    for x, y, w, h in rects:
        assert x >= -1e-9 and y >= -1e-9 and x + w <= 40.0 + 1e-9 and y + h <= 25.0 + 1e-9
        assert w > 0 and h > 0


def test_treemap_plots_the_measure_not_one_slice_per_row():
    """The incident table: six states, one row each. A count of rows gives
    six equal rectangles; the Count column gives 21/11/9/8/7/67, and the
    "Total | Distinct States | 30" row is not one of them."""
    c, notes = ok("treemap", dict(table_id="answer1", x="State", agg="count"), [STATES])
    assert c.categories == ["Other 25 States", "Texas", "Missouri", "Illinois", "California", "New Jersey"]
    assert [round(v) for v in c.series[0].values] == [67, 21, 11, 9, 8, 7]
    assert len(c.series) == 1
    assert any("total row" in n for n in notes), notes
    draw(c, "treemap")


def test_treemap_refuses_a_negative_part_of_a_whole():
    t = table("t", ["Item", "Amount"], [["A", 10], ["B", -3]])
    msg = refused("treemap", dict(table_id="t", x="Item", y=["Amount"]), [t])
    assert "negative" in msg.lower()


def test_treemap_has_no_axis_labels():
    c, _ = ok("treemap", dict(table_id="upload_defects", x="Cause", y=["Count"]), [DEFECTS])
    assert c.x_label == "" and c.y_label == ""


# ------------------------------------------------------------------ violin --


def test_violin_carries_the_sample_and_the_same_five_numbers_as_a_box():
    """Alpha is 1..6: q1 2.25, median 3.5, q3 4.75 (linear interpolation).
    Bravo is ten times that."""
    c, _ = ok("violin", dict(table_id="upload_pay", x="Team", y=["Salary"]), [SALARIES])
    assert c.categories == ["Alpha", "Bravo"]
    alpha, bravo = c.extra.box
    assert (alpha.q1, alpha.median, alpha.q3) == (2.25, 3.5, 4.75)
    assert (bravo.q1, bravo.median, bravo.q3) == (22.5, 35.0, 47.5)
    assert (alpha.min, alpha.max, alpha.n) == (1.0, 6.0, 6)
    dists = {d.name: d for d in c.extra.violin}
    assert dists["Alpha"].values == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] and dists["Alpha"].n == 6
    assert dists["Bravo"].values == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    assert [round(v, 6) for v in c.series[0].values] == [3.5, 35.0]
    draw(c, "violin")


def test_violin_leaves_out_a_group_too_thin_for_a_curve():
    rows = SALARIES.rows + [["Solo", 99], ["Solo", 98]]
    t = table("upload_pay", ["Team", "Salary"], rows)
    c, notes = ok("violin", dict(table_id="upload_pay", x="Team", y=["Salary"]), [t])
    assert "Solo" not in c.categories
    assert any("too few for a distribution" in n for n in notes), notes


def test_violin_samples_a_huge_group_and_says_so():
    rows = [["Big", i] for i in range(CD.MAX_VIOLIN_POINTS * 3)]
    t = table("t", ["Team", "Salary"], rows)
    c, notes = ok("violin", dict(table_id="t", x="Team", y=["Salary"]), [t])
    d = c.extra.violin[0]
    assert d.n == CD.MAX_VIOLIN_POINTS * 3
    assert len(d.values) <= CD.MAX_VIOLIN_POINTS
    assert any("even sample" in n for n in notes), notes
    # the box still uses every value: the median of 0..1799 is 899.5
    assert c.extra.box[0].median == 899.5


# ------------------------------------------------------------- candlestick --


def test_candlestick_opens_on_the_first_row_and_closes_on_the_last():
    """2026-03-02 has two rows: (10, 12, 9, 11) then (11, 15, 8, 14).
    The candle is open 10, high 15, low 8, close 14."""
    c, _ = ok("candlestick", dict(table_id="upload_prices", x="Day", y=["Open", "High", "Low", "Close"]), [PRICES])
    first, second = c.extra.candles
    assert (first.open, first.high, first.low, first.close) == (10.0, 15.0, 8.0, 14.0)
    assert (second.open, second.high, second.low, second.close) == (14.0, 16.0, 13.0, 15.0)
    assert [s.name for s in c.series] == ["Open", "High", "Low", "Close"]
    assert c.series[1].values == [15.0, 16.0] and c.series[2].values == [8.0, 13.0]
    draw(c, "candlestick")


def test_candlestick_refuses_the_four_columns_out_of_order():
    msg = refused("candlestick", dict(table_id="upload_prices", x="Day", y=["Open", "Low", "High", "Close"]), [PRICES])
    assert "open, high, low, close" in msg, msg


def test_candlestick_needs_exactly_four_columns():
    msg = refused("candlestick", dict(table_id="upload_prices", x="Day", y=["Open", "Close"]), [PRICES])
    assert "exactly four" in msg, msg


# ---------------------------------------------------------------- sunburst --


def test_sunburst_outer_ring_adds_up_to_its_inner_slice():
    """East is Alpha 10 + Bravo 30 = 40, West is Alpha 25. Alpha (35) is the
    larger product, so it is the first ring series."""
    c, _ = ok("sunburst", dict(table_id="upload_sales", x="Region", group_by="Product", y=["Amount"]), [SALES])
    assert c.categories == ["East", "West"]
    assert [s.name for s in c.series] == ["Alpha", "Bravo"]
    assert c.series[0].values == [10.0, 25.0]
    assert c.series[1].values == [30.0, 0.0]
    assert c.extra.ring_counts == [2, 1]
    inner = [sum(s.values[j] for s in c.series) for j in range(2)]
    assert inner == [40.0, 25.0]
    draw(c, "sunburst")


def test_sunburst_without_a_second_level_is_refused_not_guessed():
    msg = refused("sunburst", dict(table_id="upload_sales", x="Region", y=["Amount"]), [SALES])
    assert "group_by" in msg


# ------------------------------------------------------------------ bullet --


def test_bullet_pins_actual_target_and_the_bands_that_came_from_the_table():
    c, _ = ok("bullet", dict(table_id="upload_targets", x="Measure", y=["Actual", "Poor", "Fair", "Good"],
                             target="Target"), [TARGETS])
    assert c.categories == ["Revenue", "New customers", "Churn saves"]
    rows = {b.label: b for b in c.extra.bullets}
    assert (rows["Revenue"].actual, rows["Revenue"].target) == (270.0, 300.0)
    assert rows["Revenue"].bands == [150.0, 240.0, 330.0]
    assert (rows["Churn saves"].actual, rows["Churn saves"].target) == (31.0, 50.0)
    assert [s.name for s in c.series] == ["Actual", "Target"]
    assert c.series[1].values == [300.0, 80.0, 50.0]
    draw(c, "bullet")


def test_bullet_invents_no_band_when_the_table_offers_none():
    c, _ = ok("bullet", dict(table_id="upload_targets", x="Measure", y=["Actual"], target="Target"), [TARGETS])
    assert all(b.bands == [] for b in c.extra.bullets)


def test_bullet_without_a_target_is_refused():
    msg = refused("bullet", dict(table_id="upload_targets", x="Measure", y=["Actual"]), [TARGETS])
    assert "target" in msg.lower()


def test_a_target_on_another_type_is_dropped_before_compute():
    chart = CS.Chart.model_validate({"type": "bar", "title": "t",
                                     "data": dict(table_id="upload_targets", x="Measure", y=["Actual"], target="Target")})
    repaired, _ = CD.repair_binding(chart, [TARGETS])
    assert repaired.data.target is None


# ------------------------------------------------------- containers & guide --


@pytest.mark.parametrize("kind", ["pareto", "treemap", "violin", "candlestick", "sunburst", "bullet"])
def test_new_types_are_tier_two_and_fall_back_to_a_picture(kind):
    assert kind in CS.TIER2_TYPES and kind not in CS.TIER1_TYPES
    for fmt in ("xlsx", "pptx", "docx", "pdf", "html", "png", "svg"):
        assert CS.support_for(fmt, kind) == "image", (fmt, kind)
    assert CS.support_for("csv", kind) == "unsupported"


@pytest.mark.parametrize("word,kind", [
    ("pareto", "pareto"), ("tree map", "treemap"), ("treemap", "treemap"), ("violin plot", "violin"),
    ("ohlc", "candlestick"), ("candlestick", "candlestick"), ("sunburst", "sunburst"), ("bullet chart", "bullet"),
])
def test_the_model_type_alias_accepts_what_people_write(word, kind):
    assert CS.chart_type_alias(word) == kind


@pytest.mark.parametrize("kind", ["pareto", "treemap", "violin", "candlestick", "sunburst", "bullet"])
def test_the_prompt_guide_tells_the_model_when_to_choose_it(kind):
    guide = CS.prompt_guide("document", [DEFECTS])
    assert kind in guide
    assert guide.count(kind) >= 2, f"{kind} is listed but never explained"


def _requested_types(instruction: str):
    from app.artifacts import requirements as RQ

    return [i.expected for i in RQ.extract_rules(instruction, kind="document", operation="create")
            if i.category == "chart" and i.property == "type"]


@pytest.mark.parametrize("instruction,kind", [
    ("show the defects on a pareto chart", "pareto"),
    ("put the spend in a tree map", "treemap"),
    ("a violin plot of salary by department", "violin"),
    ("draw the OHLC as a candlestick", "candlestick"),
    ("a sunburst of region and product", "sunburst"),
    ("a bullet chart of actual against target", "bullet"),
])
def test_the_words_people_write_reach_the_new_types(instruction, kind):
    assert kind in _requested_types(instruction)


def test_bullet_points_in_a_deck_are_not_a_bullet_chart():
    """"bullet" is a bullet point in most decks; only "bullet chart" or
    "bullet graph" asks for the chart."""
    assert "bullet" not in _requested_types("make a deck with bullet points about the sales table")


def test_a_candle_on_a_cake_is_not_a_candlestick():
    assert "candlestick" not in _requested_types("a slide about candle sales this quarter")


def test_every_type_tuple_names_a_real_type():
    """A typo in one of the role tuples would silently take a type out of a
    rule (the negative-value ban, the group_by split) with nothing failing."""
    for tup in (CS.AGGREGATING_TYPES, CS.PART_OF_WHOLE_TYPES, CS.NO_AXIS_TYPES, CS.NON_NEGATIVE_TYPES,
                CS.SPLIT_TYPES, CS.XY_TYPES, CS.BAR_FAMILY, CS.STACKED_TYPES, CS.TIER1_TYPES, CS.TIER2_TYPES):
        assert set(tup) <= set(CS.CHART_TYPES), set(tup) - set(CS.CHART_TYPES)
    from app.artifacts.render import charts

    assert set(charts._DRAWERS) == set(CS.CHART_TYPES)


def test_the_new_types_keep_the_rules_the_old_ones_have():
    assert {"pareto", "treemap", "sunburst", "bullet"} <= set(CS.AGGREGATING_TYPES)
    assert {"treemap", "sunburst", "pareto"} <= set(CS.NON_NEGATIVE_TYPES)
    assert "sunburst" in CS.SPLIT_TYPES and "treemap" not in CS.SPLIT_TYPES
    assert {"treemap", "sunburst"} <= set(CS.NO_AXIS_TYPES)
    # a violin's one series is the group medians, so it cannot back a table
    from app.artifacts import derived

    assert "violin" in derived._NON_AGG_TYPES and "candlestick" not in derived._NON_AGG_TYPES
