"""Report PNG rendering: an explicit policy per chart type, and no blank images.

THE BUG this suite pins: `render_chart_png` used to fall through its
drawing branches for any type it did not handle, then still call
`ax.set_title(...)` and `savefig`. The result was a captioned, completely
empty PNG embedded in a Word/PDF report — output that looks deliberate and
says nothing. Unsupported now raises, and the report keeps its table.
"""
import pytest

from app.core.chart_spec import CHART_TYPES, ChartSpec
from app.core.charts_png import (
    PNG_SUPPORTED,
    PNG_TABLE_ONLY,
    EmptyChartData,
    UnsupportedChartType,
    render_chart_png,
    supports,
)

COLS = ["stage", "total"]
ROWS = [["Prospecting", 10], ["Qualification", 7], ["Closed Won", 3]]


def spec(**over):
    base = {"type": "bar", "x_key": "stage", "y_keys": ["total"], "title": "T"}
    base.update(over)
    return ChartSpec(**base)


# ---------------------------------------------------------------------------
# Policy completeness
# ---------------------------------------------------------------------------


def test_every_chart_type_has_a_report_policy():
    """Adding a ChartType without deciding how reports handle it is how the
    blank-image bug happened. This makes it a test failure instead."""
    decided = PNG_SUPPORTED | PNG_TABLE_ONLY
    assert set(CHART_TYPES) == decided
    assert not (PNG_SUPPORTED & PNG_TABLE_ONLY)


def test_funnel_is_table_only_by_policy():
    """matplotlib has no funnel primitive. A shape faked from centred bars
    encodes widths that do not honestly reflect the values, so the report
    shows the ordered table instead."""
    assert "funnel" in PNG_TABLE_ONLY
    assert supports("funnel") is False


@pytest.mark.parametrize("ctype", sorted(PNG_SUPPORTED))
def test_supported_types_render_a_non_empty_png(ctype, tmp_path):
    if ctype == "scatter":
        cols, rows = ["x", "y"], [[1, 10], [2, 20], [3, 15]]
        s = spec(type=ctype, x_key="x", y_keys=["y"])
    elif ctype == "histogram":
        cols, rows = ["bin", "count"], [["0 - 10", 4], ["10 - 20", 9]]
        s = spec(type=ctype, x_key="bin", y_keys=["count"], bins=2)
    else:
        cols, rows = COLS, ROWS
        s = spec(type=ctype)
    out = render_chart_png(s, cols, rows, tmp_path / f"{ctype}.png")
    assert out.exists()
    assert out.stat().st_size > 1000  # a real drawing, not an empty canvas


# ---------------------------------------------------------------------------
# No blank images
# ---------------------------------------------------------------------------


def test_an_unsupported_type_raises_instead_of_saving_a_blank_image(tmp_path):
    out = tmp_path / "funnel.png"
    with pytest.raises(UnsupportedChartType):
        render_chart_png(spec(type="funnel"), COLS, ROWS, out)
    assert not out.exists()


def test_no_rows_raises_instead_of_saving_a_blank_image(tmp_path):
    out = tmp_path / "empty.png"
    with pytest.raises(EmptyChartData):
        render_chart_png(spec(), COLS, [], out)
    assert not out.exists()


def test_a_spec_naming_a_missing_column_raises_before_drawing(tmp_path):
    out = tmp_path / "ghost.png"
    with pytest.raises(EmptyChartData):
        render_chart_png(spec(x_key="ghost"), COLS, ROWS, out)
    with pytest.raises(EmptyChartData):
        render_chart_png(spec(y_keys=["ghost"]), COLS, ROWS, out)
    assert not out.exists()


def test_an_all_zero_pie_raises_rather_than_drawing_nothing(tmp_path):
    out = tmp_path / "pie.png"
    with pytest.raises(EmptyChartData):
        render_chart_png(spec(type="pie"), COLS, [["a", 0], ["b", 0]], out)


def test_raw_model_output_is_refused(tmp_path):
    """Only pre-validated ChartSpec instances are ever drawn."""
    with pytest.raises(TypeError):
        render_chart_png({"type": "bar", "x_key": "stage"}, COLS, ROWS, tmp_path / "x.png")


# ---------------------------------------------------------------------------
# The new types draw what they claim to
# ---------------------------------------------------------------------------


def test_horizontal_bar_and_bar_produce_different_drawings(tmp_path):
    a = render_chart_png(spec(type="bar"), COLS, ROWS, tmp_path / "v.png").read_bytes()
    b = render_chart_png(
        spec(type="horizontal_bar"), COLS, ROWS, tmp_path / "h.png"
    ).read_bytes()
    assert a != b


def test_donut_and_pie_produce_different_drawings(tmp_path):
    a = render_chart_png(spec(type="pie"), COLS, ROWS, tmp_path / "p.png").read_bytes()
    b = render_chart_png(spec(type="donut"), COLS, ROWS, tmp_path / "d.png").read_bytes()
    assert a != b


def test_stacked_and_grouped_bars_differ(tmp_path):
    cols = ["month", "won", "lost"]
    rows = [["Jan", 3, 1], ["Feb", 5, 2]]
    grouped = render_chart_png(
        ChartSpec(type="bar", x_key="month", y_keys=["won", "lost"]),
        cols, rows, tmp_path / "g.png",
    ).read_bytes()
    stacked = render_chart_png(
        ChartSpec(type="bar", x_key="month", y_keys=["won", "lost"], stacked=True),
        cols, rows, tmp_path / "s.png",
    ).read_bytes()
    assert grouped != stacked


def test_matplotlib_is_still_not_imported_at_module_import():
    """test_imports.py asserts this globally; keeping it here too means a
    stray top-level import in charts_png fails the chart suite, where the
    person who added it is looking."""
    import importlib
    import sys

    sys.modules.pop("matplotlib.pyplot", None)
    importlib.reload(importlib.import_module("app.core.charts_png"))
    assert "matplotlib.pyplot" not in sys.modules
