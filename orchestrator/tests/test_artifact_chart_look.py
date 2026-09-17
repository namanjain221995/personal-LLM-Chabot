"""How a chart LOOKS once the colours are decided: ticks, labels, gridlines,
title alignment and the library it is all drawn with.

Each of these is one of the small defaults that separate "a default
matplotlib plot" from "a report graphic": half a ticket on the axis of a
ticket count, tick marks under labels that already say which bar they belong
to, gridlines behind numbers that are already printed, a title hanging in the
middle of the image, category names rotated 45 degrees when two lines would
have done, and a stacked bar whose total the reader has to add up by eye.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.artifacts import chart_spec as CS

pytest.importorskip("matplotlib")

from app.artifacts.render import charts as R  # noqa: E402


def chart(**over: Any) -> CS.Chart:
    base = dict(type="bar", title="Tickets by team", categories=["A", "B", "C"],
                series=[CS.Series(name="Tickets", values=[3.0, 5.0, 4.0])])
    base.update(over)
    return CS.Chart(**base)


def drawn(c: CS.Chart, **kw: Any) -> Dict[str, Any]:
    """Render `c` and hand back the artists the checks read."""
    out: Dict[str, Any] = {}

    def look(fig) -> None:
        ax = fig.axes[0]
        renderer = fig.canvas.get_renderer()
        out["fig"] = fig
        out["ax"] = ax
        out["renderer"] = renderer
        out["y_ticks"] = list(ax.get_yticks())
        out["x_ticks"] = list(ax.get_xticks())
        out["x_labels"] = [t.get_text() for t in ax.get_xticklabels()]
        out["x_rotation"] = [t.get_rotation() for t in ax.get_xticklabels()]
        out["x_tick_sizes"] = [t.tick1line.get_markersize() for t in ax.xaxis.get_major_ticks()]
        out["y_grid"] = [line.get_visible() for line in ax.get_ygridlines()]
        out["texts"] = [t.get_text() for t in ax.texts]
        title = R.title_artist(ax)
        out["title_x0"] = title.get_window_extent(renderer).x0 if title.get_text() else None
        out["label_x0"] = min((t.get_window_extent(renderer).x0 for t in ax.get_yticklabels() if t.get_text()), default=None)

    R.inspect_figure(c, look, None, **kw)
    return out


def test_whole_number_counts_get_integer_ticks():
    """Three, five and four tickets: 2.5 tickets is a number that cannot
    exist, and matplotlib's default locator prints it."""
    got = drawn(chart())
    assert got["y_ticks"], "the value axis has ticks"
    assert all(abs(t - round(t)) < 1e-9 for t in got["y_ticks"]), got["y_ticks"]
    # A measure that really is fractional keeps its fractional ticks.
    fractional = drawn(chart(series=[CS.Series(name="Rate", values=[1.25, 2.5, 3.75])],
                             style=CS.ChartStyle(number_format="decimal1")))
    assert any(abs(t - round(t)) > 1e-9 for t in fractional["y_ticks"]), fractional["y_ticks"]


def test_category_axis_has_no_tick_marks():
    got = drawn(chart())
    assert got["x_tick_sizes"], "the category axis has ticks to check"
    assert all(size == 0 for size in got["x_tick_sizes"]), got["x_tick_sizes"]


def test_gridlines_hidden_when_every_bar_is_labelled():
    labelled = drawn(chart())
    assert not any(labelled["y_grid"]), "every bar carries its number; the grid is noise"
    # Turn the labels off and the gridlines come back: without them there is
    # no way to read a value at all.
    unlabelled = drawn(chart(style=CS.ChartStyle(data_labels="off")))
    assert unlabelled["y_grid"] and all(unlabelled["y_grid"])


def test_title_aligns_to_the_figure_left():
    """matplotlib's loc='left' is the left of the PLOT BOX, which leaves the
    title floating to the right of the value labels."""
    got = drawn(chart(categories=["A", "B", "C"], series=[CS.Series(name="Revenue", values=[1200000.0, 900000.0, 400000.0])],
                      style=CS.ChartStyle(number_format="currency_USD")))
    assert got["title_x0"] is not None and got["label_x0"] is not None
    assert got["title_x0"] <= got["label_x0"] + 1.0, (got["title_x0"], got["label_x0"])


def test_long_labels_wrap_before_rotating():
    got = drawn(chart(categories=["North America", "South America", "Asia Pacific region"]))
    assert all("\n" in label for label in got["x_labels"]), got["x_labels"]
    assert all(r == 0 for r in got["x_rotation"]), got["x_rotation"]
    # A single long word cannot wrap, so it still rotates.
    unbreakable = drawn(chart(categories=["Northamericaregion", "Southamericaregion", "Asiapacificregion"]))
    assert all(r == 45 for r in unbreakable["x_rotation"]), unbreakable["x_rotation"]


def test_labels_that_still_collide_rotate_after_wrapping():
    """Wrapping is the first answer, but whether two lines FIT depends on how
    wide the chart is and how many categories share it. Five one-word stages
    in a half-page figure ran into each other ("Churned" over "Expanded" in
    the waterfall) because the old rule only counted characters."""
    crowded = drawn(chart(type="waterfall", categories=["Open", "Won", "Churned", "Expanded"],
                          series=[CS.Series(name="Delta", values=[100.0, 30.0, -20.0, 15.0])]),
                    width_px=460, height_px=300)
    assert all(r == 45 for r in crowded["x_rotation"]), crowded["x_rotation"]
    assert all("\n" not in label for label in crowded["x_labels"]), crowded["x_labels"]
    # The bottom legend is placed under the labels AS DRAWN, so it must be
    # measured after the rotation, not before it.
    legend = crowded["ax"].get_legend()
    assert legend is not None, "a signed chart carries a legend"
    lowest = min(t.get_window_extent(crowded["renderer"]).y0
                 for t in crowded["ax"].get_xticklabels() if t.get_text())
    assert legend.get_window_extent(crowded["renderer"]).y1 <= lowest + 1.0, "the legend sits on the rotated labels"
    # A fixed tick formatter rewrites the label strings on every redraw, and
    # a figure is drawn again on save, so the rotation has to survive one.
    crowded["fig"].canvas.draw()
    again = [t.get_text() for t in crowded["ax"].get_xticklabels()]
    assert all("\n" not in label for label in again), again
    assert all(t.get_rotation() == 45 for t in crowded["ax"].get_xticklabels())
    # The same categories with room to breathe stay horizontal.
    roomy = drawn(chart(type="waterfall", categories=["Open", "Won", "Churned", "Expanded"],
                        series=[CS.Series(name="Delta", values=[100.0, 30.0, -20.0, 15.0])]),
                  width_px=1400, height_px=520)
    assert all(r == 0 for r in roomy["x_rotation"]), roomy["x_rotation"]


def test_stacked_bars_print_totals():
    stacked = chart(type="stacked_bar", categories=["Q1", "Q2"], series=[
        CS.Series(name="New", values=[3.0, 4.0]),
        CS.Series(name="Renewal", values=[2.0, 6.0]),
    ])
    got = drawn(stacked)
    assert "5" in got["texts"] and "10" in got["texts"], got["texts"]


def test_the_renderer_never_changes_the_chart_type():
    """chart_choice owns types (the PR #77 rule). The painter draws what it
    is given and leaves the spec exactly as it found it."""
    for kind in ("bar", "horizontal_bar", "line", "area", "pie", "donut", "stacked_bar", "funnel", "waterfall"):
        c = chart(type=kind, categories=["A", "B", "C"], series=[CS.Series(name="v", values=[3.0, 2.0, 1.0])])
        before = c.model_dump()
        R.render_png(c, None, 640, 400)
        assert c.type == kind
        assert c.model_dump() == before, f"{kind}: the painter modified the spec"


def test_matplotlib_is_pinned_to_3_11_2_in_both_requirement_files():
    """The chart tests read tick locators and artist geometry, which move
    between matplotlib releases, so CI must install the build the production
    image runs. No second plotting library is added either."""
    root = Path(__file__).resolve().parents[1]
    pin = re.compile(r"^matplotlib==3\.11\.2(\s|#|$)")
    for name in ("requirements.txt", "requirements-dev.txt"):
        lines = (root / name).read_text(encoding="utf-8").splitlines()
        matched = [ln for ln in lines if pin.match(ln.strip())]
        assert len(matched) == 1, f"{name}: {[ln for ln in lines if 'matplotlib' in ln]}"
        banned = [ln for ln in lines for pkg in ("plotly", "bokeh", "altair", "seaborn", "pygal", "vl-convert")
                  if ln.strip().lower().startswith(pkg)]
        assert not banned, f"{name} grew a second plotting library: {banned}"


def test_the_grid_is_the_chart_gridline_token():
    from app.artifacts import chart_colours as CC
    from app.artifacts import style as ST

    assert R.GRID == CC.GRID == "#E8ECF1"
    assert ST.resolve(None).chart_defaults.grid_color == CC.GRID
    # The table hairline is a different, darker token and does not move.
    assert ST.resolve(None).tokens.grid == "#E5E9F0"


def test_a_status_chart_always_shows_its_labels():
    """Colour alone must never carry 'critical'."""
    c = chart(categories=["Done", "In progress", "Blocked"], series=[CS.Series(name="Tickets", values=[7.0, 3.0, 2.0])],
              style=CS.ChartStyle(data_labels="auto"))
    got = drawn(c)
    bar_labels: List[str] = [t.get_text() for t in got["ax"].texts]
    assert {"7", "3", "2"} <= set(bar_labels), bar_labels
