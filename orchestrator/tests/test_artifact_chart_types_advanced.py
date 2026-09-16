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

#: Where the preview PNGs go. The default used to be one session's
#: scratchpad path, so every run of the suite wrote outside the repo into a
#: directory that exists on one machine; unset, the pictures now go to the
#: pytest temp directory the fixture below hands out.
PREVIEW = Path(os.environ["CHART_PREVIEW_DIR"]) if os.environ.get("CHART_PREVIEW_DIR") else None


@pytest.fixture(autouse=True)
def _preview_dir(tmp_path_factory):
    global PREVIEW
    if PREVIEW is None:
        PREVIEW = tmp_path_factory.mktemp("charts-preview")
    return PREVIEW


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


# ------------------------------------------- the verifier's findings, 09-16 --


def test_every_cap_that_fills_categories_fits_the_field_it_feeds():
    """MAX_CANDLES shipped at 300 against a `categories` field capped at
    types.MAX_CHART_POINTS = 200, so a candlestick over 200 periods raised
    an UNCAUGHT pydantic ValidationError out of resolve_chart. Every cap
    that becomes one category must fit the field."""
    from app.artifacts import types as T

    for name in ("MAX_CATEGORIES", "MAX_CANDLES", "MAX_BULLETS", "MAX_GANTT_ROWS", "MAX_BOXES",
                 "HEATMAP_MAX", "TREEMAP_MAX", "PARETO_MAX", "MAX_VIOLINS", "SUNBURST_MAX_INNER"):
        assert getattr(CD, name) <= T.MAX_CHART_POINTS, name


def test_a_year_of_daily_prices_draws_instead_of_raising():
    """400 periods: the last MAX_CANDLES are shown and the chart validates.
    Before the cap came down this raised
    `ValidationError: ... categories / List should have at most 200 items
    after validation, not 300`."""
    rows = [[f"P{i:04d}", 100 + i * 0.1, 100 + i * 0.1 + 2, 100 + i * 0.1 - 2, 100 + i * 0.1 + 1] for i in range(400)]
    t = table("upload_p", ["Period", "Open", "High", "Low", "Close"], rows)
    c, notes = ok("candlestick", dict(table_id="upload_p", x="Period", y=["Open", "High", "Low", "Close"]), [t])
    assert len(c.categories) == CD.MAX_CANDLES == 200
    assert c.categories[-1] == "P0399"
    assert any("too many to draw" in n for n in notes), notes


def test_a_target_stated_on_every_row_is_not_summed():
    """Two Revenue rows, 120 and 150, each stating Target 300 with bands
    150/240/330. The measure adds up (270); the goal is a property of the
    label, so it stays 300 and the bands stay 150/240/330. Summing them gave
    target 600 and "270 · 45% of target" on the picture."""
    t = table("upload_b", ["Metric", "Actual", "Target", "Poor", "Fair", "Good"], [
        ["Revenue", 120, 300, 150, 240, 330],
        ["Revenue", 150, 300, 150, 240, 330],
    ])
    c, notes = ok("bullet", dict(table_id="upload_b", x="Metric", y=["Actual", "Poor", "Fair", "Good"],
                                 target="Target", agg="sum"), [t])
    b = c.extra.bullets[0]
    assert (b.actual, b.target) == (270.0, 300.0)
    assert b.bands == [150.0, 240.0, 330.0]
    assert not any("state more than one" in n for n in notes), notes


def test_rows_that_disagree_on_the_target_are_named_not_averaged():
    t = table("upload_b", ["Metric", "Actual", "Target"], [
        ["Revenue", 120, 300],
        ["Revenue", 150, 400],
    ])
    c, notes = ok("bullet", dict(table_id="upload_b", x="Metric", y=["Actual"], target="Target", agg="sum"), [t])
    b = c.extra.bullets[0]
    assert (b.actual, b.target) == (270.0, 400.0)
    assert any("state more than one Target" in n for n in notes), notes


def test_an_average_bullet_still_averages_the_measure_only():
    t = table("upload_b", ["Metric", "Actual", "Target"], [
        ["Revenue", 100, 300],
        ["Revenue", 200, 300],
    ])
    c, _ = ok("bullet", dict(table_id="upload_b", x="Metric", y=["Actual"], target="Target", agg="avg"), [t])
    assert (c.extra.bullets[0].actual, c.extra.bullets[0].target) == (150.0, 300.0)


def test_an_all_negative_bullet_keeps_its_bars_inside_the_axes():
    """Loss -30 against -100 and Drift -5 against -50. The span used to
    collapse to 0.0 -> 1.0 and set xlim(0, 1.32), which drew every bar
    outside the axes: a blank grid with a floating annotation."""
    from app.artifacts.render import charts

    t = table("upload_neg", ["Measure", "Actual", "Target"], [["Loss", -30, -100], ["Drift", -5, -50]])
    c, _ = ok("bullet", dict(table_id="upload_neg", x="Measure", y=["Actual"], target="Target"), [t])
    assert charts.render_png is not None
    seen = []
    import matplotlib.axes

    original = matplotlib.axes.Axes.set_xlim

    def spy(self, *a, **kw):
        out = original(self, *a, **kw)
        seen.append(self.get_xlim())
        return out

    matplotlib.axes.Axes.set_xlim = spy
    try:
        draw(c, "bullet-negative")
    finally:
        matplotlib.axes.Axes.set_xlim = original
    lo, hi = seen[-1]
    assert lo <= -100.0, seen
    assert hi > 0.0, seen


def test_a_violin_of_thin_groups_says_why_not_that_there_are_no_numbers():
    t = table("t", ["Team", "Salary"], [["Alpha", 1], ["Alpha", 2], ["Bravo", 3]])
    msg = refused("violin", dict(table_id="t", x="Team", y=["Salary"]), [t])
    assert str(CD.MIN_VIOLIN_POINTS) in msg and "no numbers" not in msg, msg


def test_the_sample_note_does_not_claim_a_count_the_spec_does_not_hold():
    """A stride sample of 3,334 values stores 556, not MAX_VIOLIN_POINTS."""
    rows = [["Big", i] for i in range(3_334)]
    t = table("t", ["Team", "Salary"], rows)
    c, notes = ok("violin", dict(table_id="t", x="Team", y=["Salary"]), [t])
    stored = len(c.extra.violin[0].values)
    assert stored < CD.MAX_VIOLIN_POINTS
    note = next(n for n in notes if "even sample" in n)
    assert "at most" in note, note



def test_a_truncated_pareto_says_what_its_line_is_a_share_of():
    """top_n 3 with no Other bucket over 50/25/15/6/3/1 runs to 100% over
    three bars that are 90% of the table. The line may end at 100%, but the
    chart has to say what the 100% is."""
    c, notes = ok("pareto", dict(table_id="upload_defects", x="Cause", y=["Count"], top_n=3, other_bucket=False),
                  [DEFECTS])
    assert c.categories == ["Scratches", "Misprint", "Dents"]
    assert [round(v, 1) for v in c.series[1].values] == [55.6, 83.3, 100.0]
    assert any("not of the whole table" in n for n in notes), notes
    assert "of the categories shown" in c.caption and "% of the total" not in c.caption, c.caption


def test_an_untruncated_pareto_still_says_of_the_total():
    c, notes = ok("pareto", dict(table_id="upload_defects", x="Cause", y=["Count"]), [DEFECTS])
    assert not any("not of the whole table" in n for n in notes), notes
    assert "% of the total" in c.caption, c.caption


def test_treemap_labels_that_are_drawn_fit_the_tile_they_sit_in():
    """The drawn text is MEASURED here, through a draw_event, and compared
    with the rectangle it sits in.

    The old guard asked for 0.62 treemap units per character against a box
    100 units wide, while at width_px=1400 one unit is 3.9 pt and a
    seventeen-character name at 8 pt is 17.1 units. Measured on the base
    commit, all 24 names were drawn and 21 of them crossed their tile —
    "Category number 12" by 3.84 units, "Category number 22/23" by 4.65,
    both of them past the right edge of the axes at x=100.
    """
    import matplotlib.axes
    from app.artifacts.render import charts

    names = [f"Category number {i}" for i in range(24)]
    values = [100 - i * 3 for i in range(24)]
    t = table("t", ["Name", "Value"], [[n, v] for n, v in zip(names, values)])
    c, _ = ok("treemap", dict(table_id="t", x="Name", y=["Value"]), [t])
    box_of = dict(zip(names, charts.squarified([float(v) for v in values], 0.0, 0.0, 100.0, 62.0)))
    artists = []
    measured = []

    def on_draw(event):
        for name, ax, artist in artists:
            bb = artist.get_window_extent(renderer=event.renderer)
            (x0, _y0), (x1, _y1) = ax.transData.inverted().transform([[bb.x0, bb.y0], [bb.x1, bb.y1]])
            measured.append((name, x0, x1))

    original = matplotlib.axes.Axes.text

    def spy(self, x, y, s, *a, **kw):
        artist = original(self, x, y, s, *a, **kw)
        if s in box_of:
            if not artists:
                self.get_figure().canvas.mpl_connect("draw_event", on_draw)
            artists.append((s, self, artist))
        return artist

    matplotlib.axes.Axes.text = spy
    try:
        draw(c, "treemap-24")
    finally:
        matplotlib.axes.Axes.text = original
    assert measured, "no name label was drawn at all"
    for name, x0, x1 in measured:
        left, _y, w, _h = box_of[name]
        assert x0 >= left - 0.01 and x1 <= left + w + 0.01, (name, (x0, x1), (left, left + w))
    assert len({n for n, _, _ in measured}) < len(names), "every tile still claims to fit its name"


# ------------------------------------------------ the words that route it --


@pytest.mark.parametrize("text", [
    "visualise this table as a treemap",
    "make a treemap of this table",
    "show this as a sunburst",
    "draw a candlestick of these prices",
])
def test_a_new_type_name_routes_to_a_picture_not_to_word_and_pdf(text):
    from app.artifacts import formats as F

    assert F._chart_image_formats(text) == ["png"], (text, F.decide(text).formats)


@pytest.mark.parametrize("text", [
    "explain the Pareto principle in a one page doc",
    "explain Pareto's principle in a one page doc",
    "write about pareto optimal allocations",
    "a slide about the violin in classical music",
    "the treemap data structure explained",
])
def test_an_ordinary_word_is_not_a_chart_request(text):
    from app.artifacts import formats as F

    assert F._chart_image_formats(text) == [], text
    assert _requested_types(text) == [], text


@pytest.mark.parametrize("text,kind", [
    ("put the spend in a tree map", "treemap"),
    ("a pareto of the defect causes", "pareto"),
    ("a violin plot of salary by department", "violin"),
    ("show spend by team in a treemap", "treemap"),
])
def test_the_guard_keeps_the_real_chart_asks(text, kind):
    assert kind in _requested_types(text), text


# ------------------------------- the guard reads the phrase, not the sentence --
# Recheck 2026-09-16 of the first fix round. The rule is one rule in both
# directions: a chart word that names a CHART is a chart, and the same word
# used as a concept is not. The first round decided it by scanning the whole
# sentence, which is neither.


@pytest.mark.parametrize("text,kind", [
    # BLOCKER: `\btree\s*map\b(?!.*\b(?:...|class)\b)` is unanchored, so a
    # data-structure word anywhere later in the sentence cancelled a treemap
    # that was named outright at the start of it. All three asked for a
    # treemap and got no chart type at all.
    ("a treemap of revenue by asset class", "treemap"),
    ("a treemap of storage by folder and a note on the hash map cache", "treemap"),
    ("a treemap of the outage causes, then explain the red-black tree", "treemap"),
    # The pareto guard threw away an ask that names a chart outright, because
    # "distribution" is one of the concept words and it never looked at what
    # the concept word was modifying.
    ("a pareto distribution chart of the failures", "pareto"),
    ("plot a pareto distribution graph of the failures", "pareto"),
    ("the pareto distribution diagram of defects", "pareto"),
])
def test_a_chart_word_that_names_a_chart_is_a_chart(text, kind):
    assert kind in _requested_types(text), text


@pytest.mark.parametrize("text", [
    # The pareto guard could not see across an apostrophe-s, so every
    # possessive form of the concept demanded a pareto chart.
    "explain Pareto's principle in a one page doc",
    "write about Pareto's law of the vital few",
    "a note on Pareto's efficiency for the team",
    "a doc about Pareto's optimality",
    # and the plain forms stay concepts.
    "write about pareto optimal allocations",
    "explain the Pareto principle in a one page doc",
    # treemap: the data-structure sense is the word next to it, which is
    # exactly what the anchored guard keeps.
    "the treemap data structure explained",
    "document the TreeMap interface for the team",
    "compare a treemap and a hash map in a doc",
])
def test_the_same_word_used_as_a_concept_is_not_a_chart(text):
    assert _requested_types(text) == [], text


@pytest.mark.parametrize("text", [
    "visualise this table as a treemap",
    "make a treemap of this table",
    "show this as a sunburst",
    "draw a candlestick of these prices",
])
def test_the_gate_sees_a_new_type_name_as_a_chart_ask(text):
    """formats.decide takes the intent gate's verdict OVER its own regex
    (`asked = words if chart_request is None else bool(chart_request)`), so
    the words have to reach lexicon._CHART_RE as well: with only
    formats._CHART_WORDS_RE extended, production still answered these four
    with Word and PDF."""
    from app.artifacts import formats as F
    from app.artifacts import lexicon

    assert lexicon.chart_signal(text) is True, text
    assert F.decide(text, chart_request=lexicon.chart_signal(text)).formats == ["png"], text


@pytest.mark.parametrize("text,kind", [
    ("make it a pareto chart instead", "pareto"),
    ("make it a treemap chart instead", "treemap"),
    ("as a violin plot please", "violin"),
    ("change it to a candlestick chart", "candlestick"),
])
def test_a_follow_up_can_change_the_chart_to_a_new_type(text, kind):
    """lexicon.CHART_TYPE_WORDS is what routes "make it a X chart instead"
    to a set_chart edit; every reader of it requires the chart word after
    the name, so the six new names belong in it."""
    from app.artifacts import edits

    named = edits.chart_type_named(text)
    assert named is not None and named[0] == kind, (text, named)
