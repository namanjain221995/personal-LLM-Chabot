"""The automatic chart colour scheme (app/artifacts/chart_colours.py).

Before this module every single-series chart in the product came out of
palette slot 0 — one blue for revenue, head count and churn alike, in the
same document — because the renderers varied colour per SERIES and by
nothing else. These tests pin what each rule now chooses, that every
explicit field still wins over all of it, and that the palette the rules
draw from survives the three computable colour checks.

The OKLab maths is recomputed HERE from the sRGB hexes rather than imported
from the module under test, so a mistake in the module's own conversion
cannot pass its own palette.
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import pytest

from app.artifacts import chart_colours as CC
from app.artifacts import chart_spec as CS
from app.artifacts import style as ST

# ------------------------------------------------------- independent maths --

#: Machado, Oliveira & Fernandes (2009), severity 1.0, on LINEAR rgb.
_MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
}


def _linear(hex_colour: str) -> Tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)  # type: ignore[return-value]


def _oklab(rgb: Sequence[float]) -> Tuple[float, float, float]:
    r, g, b = rgb
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def oklch(hex_colour: str) -> Tuple[float, float]:
    """(lightness, chroma) in OKLCH, computed here, not by the module."""
    L, a, b = _oklab(_linear(hex_colour))
    return L, math.hypot(a, b)


def _simulate(hex_colour: str, kind: str) -> Tuple[float, float, float]:
    r, g, b = _linear(hex_colour)
    M = _MACHADO[kind]
    return tuple(min(1.0, max(0.0, row[0] * r + row[1] * g + row[2] * b)) for row in M)  # type: ignore[return-value]


def delta_e(a: str, b: str, kind: str = "") -> float:
    """OKLab Euclidean distance x100, unsimulated or under `kind`."""
    x = _oklab(_simulate(a, kind) if kind else _linear(a))
    y = _oklab(_simulate(b, kind) if kind else _linear(b))
    return 100 * math.dist(x, y)


def wcag(a: str, b: str) -> float:
    def lum(h: str) -> float:
        r, g, bb = _linear(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bb

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- fixtures --


def bound(x: str, y: str, **over) -> CS.Chart:
    """A resolved single-series chart of `y` by `x` — the ordinary shape of
    a chart drawn from an uploaded table."""
    data = dict(table_id="upload1", x=x, y=[y], agg="sum")
    data.update(over.pop("data", {}))
    base = dict(
        type="bar", title=over.pop("title", f"{y} by {x}"),
        categories=over.pop("categories", ["A", "B", "C"]),
        series=[CS.Series(name=over.pop("series_name", y), values=over.pop("values", [3.0, 2.0, 1.0]))],
        data=CS.Binding(**data),
        provenance=CS.Provenance(table_id="upload1", rows_total=9, rows_used=9, agg="sum"),
    )
    base.update(over)
    return CS.Chart(**base)


def document(*charts: CS.Chart) -> dict:
    """The smallest spec shape `iter_chart_slots` walks: a document body
    whose blocks are charts, in order."""
    return {"document": {"title": "d", "blocks": [{"type": "chart", "chart": c.model_dump()} for c in charts]}}


def colours_of(chart: CS.Chart, plan: CC.Plan | None = None) -> List[str]:
    """The colour every CATEGORY of `chart` is drawn in, as the renderer
    resolves it (explicit fields first, then the scheme, then the palette)."""
    from app.artifacts.render import charts as R

    scheme = CC.scheme_for(chart, plan=plan)
    ctx = R._Ctx(chart=chart, style=chart.style or CS.ChartStyle(), d=R.ChartStyleDefaults(),
                 ink=R.INK, muted=R.MUTED, bg=R.WHITE, fmt=None, fraction_percent=False, scheme=scheme)
    if ctx.by_category() or chart.type in CS.NO_AXIS_TYPES:
        return [ctx.category_colour(j, cat) for j, cat in enumerate(chart.categories)]
    return [ctx.series_colour(0, chart.series[0].name)] * len(chart.categories)


def series_colours_of(chart: CS.Chart, plan: CC.Plan | None = None) -> List[str]:
    from app.artifacts.render import charts as R

    scheme = CC.scheme_for(chart, plan=plan)
    ctx = R._Ctx(chart=chart, style=chart.style or CS.ChartStyle(), d=R.ChartStyleDefaults(),
                 ink=R.INK, muted=R.MUTED, bg=R.WHITE, fmt=None, fraction_percent=False, scheme=scheme)
    return [ctx.series_colour(k, s.name) for k, s in enumerate(chart.series)]


# --------------------------------------------------------------- the rules --


def test_two_single_series_charts_of_different_subjects_get_different_colours():
    """The reported failure: every chart in a document was the same blue.
    Two subjects are two colours; the same subject charted twice is one."""
    revenue = bound("Region", "Revenue")
    headcount = bound("Region", "Headcount")
    revenue_again = bound("Region", "Revenue", title="Revenue by region, again", values=[9.0, 8.0, 7.0])
    plan = CC.plan_for(document(revenue, headcount, revenue_again))

    first = set(colours_of(revenue, plan))
    second = set(colours_of(headcount, plan))
    third = set(colours_of(revenue_again, plan))
    assert len(first) == len(second) == 1, "a nominal single series is ONE colour"
    assert first != second, "different subjects must not share a colour"
    assert first == third, "the same subject keeps its colour"
    assert first == {CC.CHART_PALETTE[0]} and second == {CC.CHART_PALETTE[1]}


def test_a_single_series_chart_without_a_plan_still_gets_one_colour():
    """A standalone render (no document) starts at slot 1 rather than
    falling back to per-series cycling."""
    assert set(colours_of(bound("Region", "Revenue"))) == {CC.CHART_PALETTE[0]}


def test_explicit_style_always_wins():
    """series.color, style.series_colors, style.category_colors and
    style.color each beat the scheme, in that order."""
    plan = CC.plan_for(document(bound("Region", "Revenue")))

    one = bound("Region", "Revenue", style=CS.ChartStyle(color="dark green"))
    assert set(colours_of(one, plan)) == {"#1E6B34"}

    per_cat = bound("Region", "Revenue", style=CS.ChartStyle(category_colors={"A": "red", "B": "gold"}))
    assert colours_of(per_cat, plan)[:2] == ["#C62828", "#B7791F"]

    per_series = bound("Region", "Revenue", series_name="Revenue",
                       style=CS.ChartStyle(series_colors={"Revenue": "purple"}))
    assert series_colours_of(per_series, plan) == ["#6D5AE6"]

    on_series = bound("Region", "Revenue")
    on_series.series[0].color = "#123456"
    on_series.style = CS.ChartStyle(color="red", series_colors={"Revenue": "gold"})
    assert series_colours_of(on_series, plan) == ["#123456"]

    # An explicit palette also wins: the scheme is consulted only after it.
    palette = bound("Region", "Revenue", style=CS.ChartStyle(palette=["#111111", "#222222"]))
    assert series_colours_of(palette, plan) == ["#111111"]


def test_status_categories_use_status_tokens():
    """A chart of statuses is coloured by what the statuses MEAN, and it
    always shows its labels — colour is never the only carrier."""
    chart = bound("Status", "Tickets", categories=["Done", "In progress", "Blocked"], values=[7.0, 3.0, 2.0])
    scheme = CC.scheme_for(chart)
    assert scheme.rule == "status" and scheme.force_labels
    assert colours_of(chart) == [CC.STATUS_MARKS["success"], CC.STATUS_MARKS["warning"], CC.STATUS_MARKS["danger"]]
    # Every status value the style engine knows maps to a mark colour.
    for cls in ST.STATUS_VALUES:
        assert cls in CC.STATUS_MARKS
    # A chart that merely CONTAINS a status word is not a status chart.
    mixed = bound("Region", "Revenue", categories=["Done", "Mumbai", "Pune"])
    assert CC.scheme_for(mixed).rule != "status"


@pytest.mark.parametrize("x_column, categories, why", [
    ("Spend band", ["High", "Medium", "Low"], "a High/Medium/Low spend band is an ordinary nominal column"),
    ("Churned", ["Yes", "No"], "a Yes/No split is an ordinary nominal column"),
    ("Region", ["Open", "Closed"], "a shop being open or closed is not a status"),
])
def test_ordinary_nominal_categories_are_not_painted_with_the_status_palette(x_column, categories, why):
    """The status palette is RESERVED, and firing it on the VALUES alone made
    colour lie about the data. Measured before the column-name gate:

        x='Spend band' High/Medium/Low -> ('#902731', '#B28D2A', '#2F8247')
        x='Churned'    Yes/No          -> ('#2F8247', '#902731')

    i.e. the biggest spend band in CRITICAL red, the smallest in SUCCESS
    green, and "Churned: Yes" green. Every other status colouring in the
    product is gated on the column NAME (style._status_column,
    render/xlsx._status_col, both via style.STATUS_COLUMN_RE); so is this
    one now."""
    chart = bound(x_column, "Revenue", categories=categories, values=[30.0, 20.0, 10.0][:len(categories)])
    scheme = CC.scheme_for(chart)
    assert scheme.rule != "status", why
    assert not set(scheme.categories) & set(CC.STATUS_MARKS.values()), scheme.categories
    assert scheme.force_labels is False, "a nominal chart does not get mandatory labels either"


@pytest.mark.parametrize("x_column, title", [
    ("Status", "Tickets by status"),
    ("Health", "Accounts by health"),
    ("Region", "Tickets by risk"),          # the name can come from the title
])
def test_a_chart_that_says_it_is_about_status_still_gets_the_status_palette(x_column, title):
    """The other half of the gate: the values AND the name. This is the case
    the reserved palette exists for, and it must not have been lost."""
    chart = bound(x_column, "Tickets", title=title, categories=["Done", "In progress", "Blocked"],
                  values=[7.0, 3.0, 2.0])
    scheme = CC.scheme_for(chart)
    assert scheme.rule == "status" and scheme.force_labels
    assert colours_of(chart) == [CC.STATUS_MARKS["success"], CC.STATUS_MARKS["warning"], CC.STATUS_MARKS["danger"]]


def test_a_long_report_does_not_run_out_of_subject_colours_at_six():
    """SUBJECT_SLOTS was 5 against an 8-colour palette, so the SIXTH subject
    of a document wrapped back to slot 1: measured on this seven-chart
    report, "Revenue by Region" and "Margin by Region" both came out
    #2F6FB2 — the reported complaint again, inside exactly the long report
    the sectioned writer now produces. Two subjects live in two different
    charts and are never side by side, so the deuteranopia ADJACENCY floor
    that justified five does not apply to them."""
    measures = ["Revenue", "Headcount", "Churn", "Pipeline", "Cost", "Margin", "Tickets"]
    charts = [bound("Region", m) for m in measures]
    plan = CC.plan_for(document(*charts))
    colours = [colours_of(c, plan)[0] for c in charts]
    assert len(set(colours)) == len(measures), dict(zip(measures, colours))
    assert colours == list(CC.CHART_PALETTE[:len(measures)])
    assert CC.SUBJECT_SLOTS == len(CC.CHART_PALETTE)


def test_the_status_marks_are_separable_even_though_they_are_green_amber_red():
    """Green / amber / red is what a status chart is read as, and it is the
    one triple a red-green reader cannot separate by HUE. These four are
    stepped apart in lightness as well, over ALL pairs, not just adjacent
    ones — a status chart puts any two of them side by side."""
    hues = [CC.STATUS_MARKS[cls] for cls in ("success", "warning", "danger", "info")]
    for colour in hues:
        L, C = oklch(colour)
        assert 0.43 <= L <= 0.77 and C >= 0.10, (colour, L, C)
        assert wcag(colour, "#FFFFFF") >= 3.0, colour
        assert max(wcag("#000000", colour), wcag("#FFFFFF", colour)) >= 4.4, colour
    for i, a in enumerate(hues):
        for b in hues[i + 1:]:
            assert delta_e(a, b) >= 15.0, (a, b, delta_e(a, b))
            cvd = min(delta_e(a, b, "protan"), delta_e(a, b, "deutan"))
            assert cvd >= 8.0, f"{a} and {b} are {cvd:.1f} apart to a red-green reader"
    # The status palette is RESERVED: it is never also a categorical slot.
    assert not set(CC.STATUS_MARKS.values()) & set(CC.CHART_PALETTE)


def test_signed_values_use_blue_gain_rose_loss_and_grey_totals():
    """Green against red is the one pairing a signed chart tempts you into
    and the one about 1 man in 12 cannot read. It is never used."""
    chart = bound("Month", "Net change", categories=["Jan", "Feb", "Total"], values=[40.0, -15.0, 25.0])
    scheme = CC.scheme_for(chart)
    assert scheme.rule == "signed"
    assert colours_of(chart) == [CC.GAIN, CC.LOSS, CC.NEUTRAL]
    assert CC.GAIN == "#2F6FB2" and CC.LOSS == "#C0566B"
    assert wcag(CC.NEUTRAL, "#FFFFFF") >= 3.0
    assert [name for name, _ in scheme.legend] == ["Increase", "Decrease", "Total"]
    used = set(colours_of(chart))
    assert "#3F8F4F" not in used and "#1E6B34" not in used, "no green against the rose loss"

    waterfall = bound("Step", "Delta", type="waterfall", categories=["Open", "Win", "Churn"], values=[100.0, 30.0, -20.0])
    assert CC.scheme_for(waterfall).signed == (CC.GAIN, CC.LOSS, CC.NEUTRAL)


def test_a_funnel_uses_a_monotone_one_hue_ramp_whose_light_end_is_2_to_1():
    chart = bound("Stage", "Leads", type="funnel", categories=["Visited", "Signed up", "Trialled", "Bought"],
                  values=[1000.0, 600.0, 300.0, 90.0])
    ramp = colours_of(chart)
    assert len(ramp) == 4 and len(set(ramp)) == 4
    lightness = [oklch(c)[0] for c in ramp]
    assert all(b > a for a, b in zip(lightness, lightness[1:])), f"lightness must rise: {lightness}"
    hues = [math.degrees(math.atan2(*reversed(_oklab(_linear(c))[1:]))) % 360 for c in ramp]
    assert max(hues) - min(hues) < 12, f"one hue, not a rainbow: {hues}"
    assert wcag(ramp[-1], "#FFFFFF") >= 2.0, "the lightest stage must still be visible on the page"
    # "Make the funnel green" is an explicit colour, so it wins over the
    # ramp and paints every stage, exactly as it did before the scheme.
    asked = bound("Stage", "Leads", type="funnel", categories=["Visited", "Signed up", "Trialled", "Bought"],
                  values=[1000.0, 600.0, 300.0, 90.0], style=CS.ChartStyle(color="dark green"))
    assert set(colours_of(asked)) == {"#1E6B34"}


def test_a_requested_ranking_highlights_the_leader_and_greys_the_rest():
    chart = bound("Customer", "Spend", title="Top 5 customers by spend",
                  categories=["Acme", "Globex", "Initech", "Umbrella", "Soylent"],
                  values=[90.0, 40.0, 30.0, 20.0, 10.0],
                  data={"table_id": "upload1", "x": "Customer", "y": ["Spend"], "agg": "sum", "sort": "value_desc", "top_n": 5})
    scheme = CC.scheme_for(chart)
    assert scheme.rule == "ranking"
    got = colours_of(chart)
    assert got[0] == CC.CHART_PALETTE[0]
    assert set(got[1:]) == {CC.NEUTRAL}
    # No ranking word, or no value sort: an ordinary comparison, one colour.
    plain = bound("Customer", "Spend", title="Spend by customer",
                  categories=["Acme", "Globex", "Initech"], values=[90.0, 40.0, 30.0],
                  data={"table_id": "upload1", "x": "Customer", "y": ["Spend"], "agg": "sum", "sort": "value_desc"})
    assert CC.scheme_for(plain).rule == "subject" and len(set(colours_of(plain))) == 1


def test_time_series_keep_one_colour():
    """A time series is one thing measured repeatedly; colouring its points
    differently says the opposite."""
    monthly = bound("Month", "Revenue", type="line", categories=["Jan", "Feb", "Mar"], values=[1.0, 2.0, 3.0],
                    data={"table_id": "upload1", "x": "Month", "y": ["Revenue"], "agg": "sum", "date_bucket": "month"})
    assert CC.scheme_for(monthly).rule == "time"
    assert len(set(colours_of(monthly))) == 1
    area = bound("Month", "Revenue", type="area", values=[1.0, 2.0, 3.0])
    assert len(set(colours_of(area))) == 1
    # A date x does not turn into a ranking, however the title is worded.
    top = bound("Month", "Revenue", type="line", title="Top months",
                data={"table_id": "upload1", "x": "Month", "y": ["Revenue"], "agg": "sum", "date_bucket": "month", "sort": "value_desc"})
    assert CC.scheme_for(top).rule == "time"


def test_category_names_keep_their_colour_across_charts_of_one_document():
    pie = bound("Region", "Revenue", type="pie", categories=["North", "South", "East"], values=[5.0, 3.0, 2.0])
    stacked = CS.Chart(
        type="stacked_bar", title="Revenue by quarter and region", categories=["Q1", "Q2"],
        series=[CS.Series(name="South", values=[1.0, 2.0]), CS.Series(name="North", values=[3.0, 4.0])],
        data=CS.Binding(table_id="upload1", x="Quarter", y=["Revenue"], agg="sum", group_by="Region"),
        provenance=CS.Provenance(table_id="upload1", rows_total=4, rows_used=4, agg="sum"),
    )
    plan = CC.plan_for(document(pie, stacked))
    slices = dict(zip(pie.categories, colours_of(pie, plan)))
    bars = dict(zip([s.name for s in stacked.series], series_colours_of(stacked, plan)))
    assert slices["North"] == bars["North"] and slices["South"] == bars["South"]
    assert bars["North"] != bars["South"]

    # A document with a single pie keeps today's slice order: slot per index.
    alone = CC.plan_for(document(pie))
    assert colours_of(pie, alone) == list(CC.CHART_PALETTE[:3])


def test_the_chart_palette_passes_band_chroma_and_normal_vision_floor():
    """The three computable checks, recomputed here. Slots 6-8 failed two of
    them before 2026-09-17: #8A5A44 and #5F6B7A had chroma 0.072 and 0.028
    (they read as grey), and #5F6B7A against #3F8F4F was 14.8 ΔE apart under
    NORMAL vision."""
    palette = list(CC.CHART_PALETTE)
    assert len(palette) == 8
    for colour in palette:
        L, C = oklch(colour)
        assert 0.43 <= L <= 0.77, f"{colour} lightness {L:.3f} outside the band"
        assert C >= 0.10, f"{colour} chroma {C:.3f} reads as grey"
    for a, b in zip(palette, palette[1:]):
        assert delta_e(a, b) >= 15.0, f"{a} and {b} are {delta_e(a, b):.1f} apart under normal vision"
        cvd = min(delta_e(a, b, "protan"), delta_e(a, b, "deutan"))
        assert cvd >= 6.0, f"{a} and {b} are {cvd:.1f} apart under simulated CVD"
    # The first five keep the style guide's order, unchanged by this round.
    assert palette[:5] == ["#2F6FB2", "#E07B00", "#0E9D9A", "#C0566B", "#6D5AE6"]
    # ALL pairs, not just adjacent ones. The 15 floor is unreachable here and
    # always was: the FIXED five already hold #2F6FB2 and #6D5AE6 12.2 apart,
    # so no choice of slots 6-8 can lift the worst pair to 15. What slots 6-8
    # must not do is make it worse than the non-adjacent pairs the five
    # already imply, and a chart that reaches slot 6 at all has >= 6 series,
    # where the renderer always draws a legend and direct labels — the
    # secondary encoding a sub-15 pair needs. The worst pair is named here so
    # a later re-step cannot quietly lower it.
    worst = min((delta_e(a, b), a, b) for i, a in enumerate(palette) for b in palette[i + 1:])
    assert worst[0] >= 8.0, worst
    assert worst[1:] == ("#E07B00", "#B38C15"), worst
    # One palette, carried in three places; they must not drift apart.
    assert tuple(palette) == CS.DEFAULT_PALETTE == ST.CHART_PALETTE


def test_data_labels_reach_four_point_five_to_one_on_the_greys_ramps_and_status_fills():
    """Contract: a label drawn INSIDE a mark reads on it. Black or white
    always reaches 4.58:1 on an opaque fill, which is the renderer's floor."""
    ramp = CC.ordinal_ramp(CC.CHART_PALETTE[0], 5)
    for fill in [CC.NEUTRAL, *CC.STATUS_MARKS.values(), *ramp]:
        best = max(wcag("#000000", fill), wcag("#FFFFFF", fill))
        assert best >= 4.5, f"nothing readable sits on {fill}"


# ------------------------------------------------------- across the formats --


def _fill(series) -> str:
    """The solid fill of an openpyxl chart series, as `RRGGBB`."""
    value = series.graphicalProperties.solidFill
    return str(getattr(value, "srgbClr", None) or value)


def test_png_docx_pptx_xlsx_agree_on_colours_for_one_spec():
    """One chart, four writers, one set of colours. The PNG is what DOCX and
    the PDF embed, so sampling its pixels covers all three."""
    pytest.importorskip("matplotlib")
    pytest.importorskip("pptx")
    pytest.importorskip("openpyxl")
    from PIL import Image
    from pptx import Presentation

    from app.artifacts.render import chart_native as CN
    from app.artifacts.render import charts as R

    revenue = bound("Region", "Revenue")
    headcount = bound("Region", "Headcount")
    resolved = ST.resolve(_spec(revenue, headcount))
    wanted = CC.CHART_PALETTE[1]  # the SECOND subject of the document

    png = R.render_png(headcount, resolved, 800, 480)
    import io

    with Image.open(io.BytesIO(png)) as im:
        pixels = set(im.convert("RGB").getdata())
    assert tuple(int(wanted[i:i + 2], 16) for i in (1, 3, 5)) in pixels

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    CN.add_pptx_chart(slide, headcount, (0.5, 0.5, 6.0, 4.0), resolved)
    chart = next(sh.chart for sh in slide.shapes if sh.has_chart)
    assert str(chart.plots[0].series[0].format.fill.fore_color.rgb) == wanted.lstrip("#").upper()

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    CN.add_xlsx_chart(ws, headcount, "H2", resolved)
    xl = ws._charts[0]
    assert _fill(xl.series[0]) == wanted.lstrip("#").upper()


def test_the_embedded_chart_writer_takes_the_documents_colour_plan(tmp_path):
    """`render_chart_png` is what a DOCX, a PDF and an HTML document embed,
    through `render/__init__`'s chart loop. It used to take only a chart and
    a path, so the document's plan could not reach it and every chart of a
    document came back slot 1 — the reported failure, in the file the person
    actually opens. The plan rides on `resolved`, so it has to be passable.
    (The call site in `render/__init__.py` belongs to another track; this
    pins the half that lives here.)"""
    pytest.importorskip("matplotlib")
    from PIL import Image

    from app.artifacts.render import charts as R

    revenue = bound("Region", "Revenue")
    headcount = bound("Region", "Headcount")
    resolved = ST.resolve(_spec(revenue, headcount))

    def fill_of(chart: CS.Chart, name: str, style) -> str:
        path = R.render_chart_png(chart, tmp_path / name, style)
        with Image.open(path) as im:
            counts: dict = {}
            for pixel in im.convert("RGB").getdata():
                counts[pixel] = counts.get(pixel, 0) + 1
        bar = max((c for c in counts if len(set(c)) > 1), key=lambda c: counts[c])
        return "#%02X%02X%02X" % bar

    assert fill_of(revenue, "a.png", resolved) == CC.CHART_PALETTE[0]
    assert fill_of(headcount, "b.png", resolved) == CC.CHART_PALETTE[1]
    # Without a style there is no plan, and the documented fallback applies.
    assert fill_of(headcount, "c.png", None) == CC.CHART_PALETTE[0]


def _spec(*charts: CS.Chart):
    from app.artifacts import spec as S

    return S.ArtifactSpec.model_validate({"kind": "document", **document(*charts)})


def test_legacy_unstyled_deck_and_workbook_charts_match_documents(tmp_path):
    """The legacy python-pptx and openpyxl writers used theme.PALETTE (teal
    first) while documents used the artifact palette (blue first), so one
    chart was two colours in two files."""
    pytest.importorskip("pptx")
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook
    from pptx import Presentation

    from app.artifacts import spec as S
    from app.artifacts.render import html as H
    from app.artifacts.render.pptx import render_pptx
    from app.artifacts.render.xlsx import render_xlsx

    legacy = S.Chart(type="bar", title="Revenue by region", categories=["North", "South"],
                     series=[S.Series(name="FY25", values=[1.0, 2.0]), S.Series(name="FY26", values=[3.0, 4.0])])
    deck = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(
        title="d", slides=[S.Slide(layout="chart", title="c", chart=legacy)]))
    path = render_pptx(deck.body, tmp_path / "d.pptx", plan=H.plan_deck(deck.body))
    chart = next(sh.chart for s in Presentation(str(path)).slides for sh in s.shapes if sh.has_chart)
    assert [str(s.format.fill.fore_color.rgb) for s in chart.series] == [
        CC.CHART_PALETTE[0].lstrip("#").upper(), CC.CHART_PALETTE[1].lstrip("#").upper()]

    book = S.ArtifactSpec(kind="workbook", workbook=S.WorkbookSpec(title="w", sheets=[
        S.Sheet(name="Data", columns=[S.Column(name="Region"), S.Column(name="FY25", type="number"), S.Column(name="FY26", type="number")],
                rows=[["North", 1, 3], ["South", 2, 4]], charts=[legacy])]))
    out = render_xlsx(book.body, tmp_path / "w.xlsx")
    ws = load_workbook(out)["Data"]
    fills = [_fill(s) for s in ws._charts[0].series]
    assert fills == [CC.CHART_PALETTE[0].lstrip("#").upper(), CC.CHART_PALETTE[1].lstrip("#").upper()]


def test_the_scheme_never_writes_the_chart_style():
    """chart.style belongs to the person: it is what an edit turn reads and
    what self-check compares. An automatic colour must stay out of it."""
    chart = bound("Region", "Revenue")
    before = chart.model_dump()
    plan = CC.plan_for(document(chart))
    CC.scheme_for(chart, plan=plan)
    colours_of(chart, plan)
    assert chart.model_dump() == before
    assert chart.style is None
