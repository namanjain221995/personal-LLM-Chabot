"""Chart PNGs: every chart type draws, output is deterministic, the palette
is the app's, and the module stays import-light (matplotlib.pyplot is lazy)."""
from __future__ import annotations

import sys

import pytest

from app.artifacts import spec as S
from app.artifacts.render import charts, theme

pytest.importorskip("matplotlib")


def chart(**over) -> S.Chart:
    base = dict(type="bar", title="T", categories=["A", "B", "C"], series=[S.Series(name="one", values=[1, 2, 3])])
    base.update(over)
    return S.Chart(**base)


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "line", "pie"])
def test_every_chart_type_draws_a_png(tmp_path, kind):
    out = charts.render_chart_png(chart(type=kind), tmp_path / f"{kind}.png")
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    with Image.open(out) as im:
        w, h = im.size
    # 8 x 4.5 in at 160 dpi, before tight-layout trimming, is 1280 x 720.
    assert 1100 <= w <= 1300 and 600 <= h <= 740


def test_render_is_byte_deterministic(tmp_path):
    """The same Chart twice → the same bytes, so a cache keyed on the spec
    hash is safe and a re-render of a version is identical."""
    c = chart(type="bar", series=[S.Series(name="one", values=[1, 2, 3]), S.Series(name="two", values=[3, 2, 1])])
    a = charts.render_chart_png(c, tmp_path / "a.png").read_bytes()
    b = charts.render_chart_png(c, tmp_path / "b.png").read_bytes()
    assert a == b


def test_palette_is_the_style_guides_cvd_order(tmp_path):
    """Artifact charts use the style guide's series order (reordered for
    colour-vision deficiency, AS3): the first two bars are #2F6FB2 and
    #E07B00. Sampled from the bars."""
    from PIL import Image

    from app.artifacts import chart_spec as CS

    c = chart(type="bar", categories=["A"], series=[S.Series(name="one", values=[10]), S.Series(name="two", values=[10])])
    out = charts.render_chart_png(c, tmp_path / "p.png")
    with Image.open(out) as im:
        pixels = set(im.convert("RGB").getdata())
    assert theme.hex_to_rgb(CS.DEFAULT_PALETTE[0]) in pixels
    assert theme.hex_to_rgb(CS.DEFAULT_PALETTE[1]) in pixels


def test_theme_palette_matches_the_legacy_report_painter():
    """theme.PALETTE and core/charts_png.py both carry the app's five series
    colours as literals (the legacy painter is not this feature's to edit);
    this keeps them from drifting apart — a chart in a report and a chart in
    an artifact must be the same colours in the same order."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "core" / "charts_png.py").read_text(encoding="utf-8")
    legacy = re.findall(r'"(#[0-9a-f]{6})"', source.split("set_prop_cycle", 1)[1].split(")", 1)[0])
    assert tuple(legacy) == theme.PALETTE


def test_pie_folds_past_eight_slices(tmp_path):
    cats = [f"c{i}" for i in range(12)]
    c = chart(type="pie", categories=cats, series=[S.Series(name="s", values=list(range(1, 13)))])
    assert charts.render_chart_png(c, tmp_path / "pie.png").stat().st_size > 0


def test_pie_with_no_positive_values_does_not_crash(tmp_path):
    # All-zero is refused by the spec now (a chart the model emptied); a
    # pie of negatives and zeros still reaches the renderer and must not crash.
    c = chart(type="pie", series=[S.Series(name="s", values=[-1, 0, -2])])
    assert charts.render_chart_png(c, tmp_path / "zero.png").stat().st_size > 0


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "line", "pie"])
def test_dollar_signs_are_text_not_tex(tmp_path, kind):
    """Finance text is full of dollars. With mathtext on, "Revenue $M vs $K"
    lost the words between the dollars and "a $\\frac$ b" raised a
    ParseSyntaxException that failed the whole render (review 2026-09-11).
    Title, category, series name and axis label each carry both shapes."""
    series = [S.Series(name="a $\\frac$ b $M vs $K", values=[1, 2, 3])]
    if kind != "pie":
        series.append(S.Series(name="$K", values=[3, 2, 1]))   # a second series puts the names in a legend
    c = S.Chart(
        type=kind, title="a $\\frac$ b — Revenue $M vs $K", categories=["$\\frac$", "$M vs $K", "$1,000"],
        series=series, y_label="$M vs $K $\\alpha$",
    )
    out = charts.render_chart_png(c, tmp_path / f"dollars-{kind}.png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    import matplotlib

    assert matplotlib.rcParams["text.parse_math"] is False


def test_rejects_anything_but_a_validated_chart(tmp_path):
    with pytest.raises(TypeError):
        charts.render_chart_png({"type": "bar"}, tmp_path / "x.png")  # type: ignore[arg-type]


def test_render_package_imports_without_pyplot():
    """The renderers are imported by the app (health, api); pyplot and the
    Office libraries must not come with them (tests/test_imports.py's rule)."""
    import subprocess

    script = (
        "import sys\n"
        "import app.artifacts.render, app.artifacts.render.charts, app.artifacts.render.html, "
        "app.artifacts.render.pdf, app.artifacts.render.docx, app.artifacts.render.pptx, "
        "app.artifacts.render.xlsx, app.artifacts.render.validate, app.artifacts.render.preview, "
        "app.artifacts.render.worker\n"
        "bad = [m for m in ('matplotlib.pyplot', 'weasyprint', 'docx', 'pptx', 'openpyxl', 'pypdfium2') if m in sys.modules]\n"
        "if bad: raise SystemExit('eager: ' + ', '.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ Chart v2 (AS3) --

import io  # noqa: E402
import itertools  # noqa: E402
import math  # noqa: E402

from app.artifacts import chart_data as CD  # noqa: E402
from app.artifacts import chart_spec as CS  # noqa: E402
from app.artifacts import types as T  # noqa: E402
from app.artifacts.render import validate as V  # noqa: E402
from tests.fixtures.charts import loader  # noqa: E402

V2_BINDINGS = {
    "bar": dict(table_id="upload_sales", x="Region", y=["Amount"]),
    "horizontal_bar": dict(table_id="upload_tickets", x="Owner", y=["Hours"]),
    "stacked_bar": dict(table_id="upload_sales", x="Date", date_bucket="quarter", group_by="Region", y=["Amount"]),
    "stacked_horizontal_bar": dict(table_id="upload_tickets", x="Status", group_by="Priority", agg="count"),
    "percent_stacked_bar": dict(table_id="upload_tickets", x="Owner", group_by="Priority", agg="count"),
    "line": dict(table_id="upload_sales", x="Date", group_by="Region", y=["Amount"]),
    "area": dict(table_id="upload_sales", x="Date", y=["Units"]),
    "stacked_area": dict(table_id="upload_sales", x="Date", group_by="Product", y=["Amount"]),
    "pie": dict(table_id="upload_tickets", x="Status", agg="count"),
    "donut": dict(table_id="upload_sales", x="Product", y=["Amount"]),
    "scatter": dict(table_id="upload_employees", x="Experience", y=["Salary"], trendline=True),
    "histogram": dict(table_id="upload_employees", y=["Salary"]),
    "combo": dict(table_id="upload_units", x="Product", y=["Q1", "Q2"], y2=["Q4"]),
    "box": dict(table_id="upload_employees", x="Department", y=["Salary"]),
    "heatmap": dict(table_id="upload_tickets", x="Status", group_by="Priority", agg="count"),
    "waterfall": dict(table_id="upload_cashflow", x="Item", y=["Amount"]),
    "funnel": dict(table_id="upload_funnel", x="Stage", y=["Count"]),
    "gantt": dict(table_id="upload_projects", label="Task", start="Start", end="End"),
    "radar": dict(table_id="upload_units", x="Product", y=["Q1", "Q2", "Q3", "Q4"]),
    "bubble": dict(table_id="upload_employees", x="Experience", y=["Salary"], size="Age"),
}


@pytest.fixture(scope="module")
def fixture_tables():
    return [loader.table(n) for n in loader.FILES]


def v2(kind, tables, **over):
    raw = {"type": kind, "title": f"{kind} chart", "data": V2_BINDINGS[kind]}
    raw.update(over)
    c, notes, msg = CD.resolve_chart(CS.Chart.model_validate(raw), tables)
    assert c is not None, msg
    return c


def _table(**kw):
    from app.artifacts.compose import DataTable

    return DataTable(**kw)


def test_bindings_cover_all_twenty_types():
    assert set(V2_BINDINGS) == set(CS.CHART_TYPES)


@pytest.mark.parametrize("kind", CS.CHART_TYPES)
def test_every_v2_type_renders_valid_png_and_svg(tmp_path, fixture_tables, kind):
    c = v2(kind, fixture_tables)
    png = charts.render_png(c)
    (tmp_path / "c.png").write_bytes(png)
    facts = V.validate_file(tmp_path / "c.png", "png")
    assert facts["width"] >= 400 and facts["height"] >= 300
    svg = charts.render_svg(c)
    assert V.validate_svg_bytes(svg) == [], kind
    assert b"<text" not in svg and b"<!DOCTYPE" not in svg and b"<metadata" not in svg
    assert charts.render_svg(c) == svg and charts.render_png(c) == png  # deterministic


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lab(rgb_lin):
    r, g, b = rgb_lin
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _deuteranopia(hex_):
    """Machado et al. (2009), severity 1.0, applied in linear RGB."""
    h = hex_.lstrip("#")
    r, g, b = (_lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    m = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))
    return _lab([min(1.0, max(0.0, m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3)])


def test_default_palette_first_five_survive_deuteranopia():
    pts = [_deuteranopia(h) for h in CS.DEFAULT_PALETTE[:5]]
    worst = min(math.dist(a, b) for a, b in itertools.combinations(pts, 2))
    assert worst >= 25, worst


def test_three_or_more_lines_get_distinct_markers_and_dashes(fixture_tables):
    c = v2("line", fixture_tables)
    assert len(c.series) == 4
    seen = {}

    def look(fig):
        names = {s.name for s in c.series}
        lines = [ln for ln in fig.axes[0].get_lines() if ln.get_label() in names]
        seen["markers"] = {ln.get_marker() for ln in lines}
        seen["dashes"] = {repr(ln.get_linestyle()) for ln in lines}
        seen["legend"] = fig.axes[0].get_legend() is not None

    charts.inspect_figure(c, look)
    assert len(seen["markers"]) == 4 and len(seen["dashes"]) == 4 and seen["legend"]


def test_more_than_five_series_use_direct_labels():
    rows = [[m, g, i + j] for i, m in enumerate(["Jan", "Feb", "Mar"]) for j, g in enumerate("ABCDEF")]
    t = _table(id="paste1", title="p", columns=["M", "G", "V"], rows=rows)
    c, _, _ = CD.resolve_chart(CS.Chart.model_validate({"type": "line", "title": "six", "data": {"table_id": "paste1", "x": "M", "group_by": "G", "y": ["V"]}}), [t])
    seen = {}

    def look(fig):
        ax = fig.axes[0]
        seen["legend"] = ax.get_legend()
        seen["labels"] = {tx.get_text() for tx in ax.texts}

    charts.inspect_figure(c, look)
    assert seen["legend"] is None and set("ABCDEF") <= seen["labels"]


def _background_under(fig, text, bg="#FFFFFF"):
    from matplotlib.patches import Rectangle, Wedge

    renderer = fig.canvas.get_renderer()
    bbox = text.get_window_extent(renderer)
    cx, cy = (bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2
    colour = bg
    for ax in fig.axes:
        for patch in ax.patches:
            if isinstance(patch, (Rectangle, Wedge)) and patch.get_visible() and patch.contains_point((cx, cy)):
                fc = patch.get_facecolor()
                if fc[3] > 0.5:
                    colour = charts._to_hex(fc)
    return colour


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar", "pie", "donut", "waterfall", "funnel"])
def test_data_labels_reach_four_point_five_to_one_on_their_background(fixture_tables, kind):
    from matplotlib.colors import to_hex

    c = v2(kind, fixture_tables)
    ratios = []

    def look(fig):
        ax = fig.axes[0]
        for tx in ax.texts:
            if tx.get_text() and tx.get_visible():
                ratios.append(charts.contrast_ratio(to_hex(tx.get_color()).upper(), _background_under(fig, tx)))

    charts.inspect_figure(c, look)
    assert ratios and min(ratios) >= 4.5, (kind, min(ratios) if ratios else None)


def test_heatmap_cell_labels_contrast_with_their_cell(fixture_tables):
    from matplotlib.colors import LinearSegmentedColormap, to_hex

    c = v2("heatmap", fixture_tables)
    grid = [list(s.values) for s in c.series]
    lo, hi = min(map(min, grid)), max(map(max, grid))
    cmap = LinearSegmentedColormap.from_list("ts_heat", ["#F3F6FA", charts.NAVY])
    seen = []

    def look(fig):
        for tx in fig.axes[0].texts:
            x, y = tx.get_position()
            cell = charts._to_hex(cmap((grid[int(round(y))][int(round(x))] - lo) / ((hi - lo) or 1)))
            seen.append(charts.contrast_ratio(to_hex(tx.get_color()).upper(), cell))

    charts.inspect_figure(c, look)
    assert seen and min(seen) >= 4.5


def test_requested_series_colour_dominates_the_png(fixture_tables):
    from PIL import Image

    c = v2("bar", fixture_tables, style={"color": "purple", "data_labels": "off"})
    img = Image.open(io.BytesIO(charts.render_png(c))).convert("RGB")
    coloured = [(n, rgb) for n, rgb in img.getcolors(maxcolors=1 << 22) if max(rgb) - min(rgb) > 40]
    _, rgb = max(coloured)
    assert math.dist(_lab([_lin(v) for v in (0x6D, 0x5A, 0xE6)]), _lab([_lin(v) for v in rgb])) < 8


def test_style_overrides_reach_the_figure(fixture_tables):
    c = v2("stacked_bar", fixture_tables, style={"legend_position": "top", "y_min": 0, "y_max": 300000, "title": {"size_pt": 18, "color": "dark red"}})
    seen = {}

    def look(fig):
        ax = fig.axes[0]
        seen["ylim"] = ax.get_ylim()
        seen["title"] = ax._left_title
        leg = ax.get_legend()
        seen["legend_y"] = leg.get_window_extent(fig.canvas.get_renderer()).y0 >= ax.get_window_extent().y1 - 1

    charts.inspect_figure(c, look)
    assert seen["ylim"] == (0.0, 300000.0) and seen["legend_y"]
    assert seen["title"].get_fontsize() == 18 and seen["title"].get_color().upper() == "#9B1C1C"


def test_no_duplicate_title_under_an_identical_heading(fixture_tables):
    c = v2("pie", fixture_tables, title="Ticket status")
    seen = {}
    charts.inspect_figure(c, lambda fig: seen.update(title=fig.axes[0].get_title(loc="left")), section_heading="ticket  STATUS")
    assert seen["title"] == ""


def test_indian_grouping_and_formats():
    assert charts.format_value(1234567, "currency_INR") == "₹12,34,567"
    assert charts.format_value(120000, "currency_INR") == "₹1,20,000"
    assert charts.format_value(1234.5, "currency_USD") == "$1,234.50"
    assert charts.format_value(0.256, "percent", fraction_percent=True) == "25.6%"
    assert charts.format_value(1500000, "compact") == "1.5M"
    assert charts.format_value(12345, None) == "12,345"


def test_indic_labels_warn_only_without_a_font(monkeypatch):
    c = CS.Chart(type="bar", title="क्षेत्र के अनुसार", categories=["उत्तर", "દક્ષિણ"], series=[{"name": "s", "values": [1, 2]}])
    if charts.font_for_script("Devanagari") and charts.font_for_script("Gujarati"):
        assert charts.chart_warnings(c) == []
    monkeypatch.setitem(charts.SCRIPT_FONTS, "Gujarati", ((0x0A80, 0x0AFF), ("No Such Gujarati Font",)))
    warnings = charts.chart_warnings(c)
    assert len(warnings) == (1 if charts.font_for_script("Devanagari") else 2)
    assert len([w for w in warnings if "Gujarati" in w]) == 1


def test_standalone_pngs_and_roles(tmp_path, fixture_tables):
    spec = {"kind": "document", "document": {"title": "Two", "blocks": [
        {"type": "chart", "chart": v2("pie", fixture_tables).model_dump()},
        {"type": "paragraph", "text": "between"},
        {"type": "chart", "chart": v2("line", fixture_tables).model_dump()},
    ]}}
    paths = charts.render_standalone(spec, "png", tmp_path, stem="Sales Review-v1")
    assert [p.name for p in paths] == ["sales-review-v1-1.png", "sales-review-v1-2.png"]
    for p in paths:
        assert V.validate_file(p, "png")["ok"]
    svgs = charts.render_standalone(spec, "svg", tmp_path)
    assert len(svgs) == 2 and all(V.validate_svg_bytes(p.read_bytes()) == [] for p in svgs)
    assert T.role_for_format("document", "png", ["png"]) == "primary"
    assert T.role_for_format("document", "png", ["docx", "png"]) == "companion"
    assert T.MIME_TYPES["svg"] == "image/svg+xml" and "svg" in T.CHART_IMAGE_FORMATS_FOR_KIND["document"]
    # AS3 integration: live — every kind takes its chart image formats after
    # the native ones (render dispatch + SVG attachment headers are in).
    for kind, images in T.CHART_IMAGE_FORMATS_FOR_KIND.items():
        assert T.FORMATS_FOR_KIND[kind][-len(images):] == images
    assert set(T.IMAGE_FORMATS) <= set(T.FORMATS)


def test_unresolved_v2_chart_is_refused():
    c = CS.Chart(type="bar", data={"table_id": "t", "x": "a"})
    with pytest.raises(ValueError, match="resolve"):
        charts.render_png(c)


def test_long_category_labels_are_clipped_on_the_axis():
    assert charts._tick_text("x" * 80) == "x" * 27 + "…" and charts._tick_text("Short") == "Short"
