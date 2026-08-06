"""Render VALIDATED ChartSpecs to PNG with matplotlib (Agg) for report docs.

Reports are generated server-side, with no browser anywhere in the path, so
they cannot use the ECharts renderer the chat UI uses. matplotlib stays.
The two renderers share the ChartSpec contract, not an implementation.

matplotlib is imported lazily inside render_chart_png so importing this
module stays cheap and headless environments never need a display
(`test_imports.py` asserts matplotlib.pyplot is absent at import time).

EVERY ChartType has an explicit policy here — see `PNG_SUPPORTED` and
`UnsupportedChartType`. The previous behaviour for an unhandled type was to
fall through the drawing branches and save an EMPTY figure that still had a
title, which embedded a blank-but-captioned image in the report and looked
like real output. Unsupported now raises, and `report.py` degrades to the
table it already rendered.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .chart_spec import CHART_TYPES, ChartSpec


class UnsupportedChartType(ValueError):
    """This ChartType has no truthful matplotlib rendering. Use the table."""


class EmptyChartData(ValueError):
    """Nothing to draw. Raised instead of saving a blank titled PNG."""


#: Types rendered natively in reports.
#:
#: `funnel` is deliberately absent. A funnel's meaning is its ordered,
#: narrowing geometry; matplotlib has no funnel primitive, and faking one
#: from stacked centred bars produces a shape whose widths do not honestly
#: encode the values. The report shows the ordered table instead — the same
#: numbers, without a drawing that overstates them.
PNG_SUPPORTED = frozenset(
    {"bar", "horizontal_bar", "line", "area", "scatter", "pie", "donut", "histogram"}
)

#: Types with an explicit table-only policy. PNG_SUPPORTED | PNG_TABLE_ONLY
#: must equal every ChartType — `test_charts_png.py` asserts it, so adding a
#: type without deciding its report behaviour fails the suite.
PNG_TABLE_ONLY = frozenset({"funnel"})

_MAX_PIE_SLICES = 8


def _num(value: object) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # Decimal, numeric strings, ...
    except (TypeError, ValueError):
        return 0.0


def supports(chart_type: str) -> bool:
    """True when `chart_type` has a native report rendering."""
    return chart_type in PNG_SUPPORTED


def render_chart_png(
    spec: ChartSpec,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    out_path: str | Path,
) -> Path:
    """Render `spec` over (columns, rows) to `out_path` and return the path.

    Only pre-validated ChartSpec instances are accepted — never raw model
    output (spec §8: model output is parsed/validated, never executed).

    Raises UnsupportedChartType or EmptyChartData rather than writing a
    blank image.
    """
    if not isinstance(spec, ChartSpec):
        raise TypeError("render_chart_png requires a validated ChartSpec")
    if spec.type not in PNG_SUPPORTED:
        raise UnsupportedChartType(
            f"{spec.type} has no matplotlib rendering; use the table"
        )

    cols = list(columns)
    if spec.x_key not in cols:
        raise EmptyChartData(f"x_key {spec.x_key!r} is not a result column")
    missing = [y for y in spec.y_keys if y not in cols]
    if missing:
        raise EmptyChartData(f"y_keys not in result columns: {missing}")
    rows = list(rows)
    if not rows:
        raise EmptyChartData("no rows to plot")

    import matplotlib

    matplotlib.use("Agg", force=True)  # headless
    import matplotlib.pyplot as plt

    xi = cols.index(spec.x_key)
    xs = [str(r[xi]) for r in rows]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    try:
        if spec.type in ("pie", "donut"):
            _draw_part_to_whole(ax, spec, cols, rows, xs)
        elif spec.type == "horizontal_bar":
            _draw_horizontal_bar(ax, spec, cols, rows, xs)
        else:
            _draw_cartesian(ax, spec, cols, rows, xs)

        if spec.title:
            ax.set_title(spec.title)
        if spec.type not in ("pie", "donut"):
            if spec.type == "horizontal_bar":
                ax.set_ylabel(spec.x_key)
                if len(spec.y_keys) == 1:
                    ax.set_xlabel(spec.y_keys[0])
            else:
                ax.set_xlabel(spec.x_key)
                if len(spec.y_keys) == 1:
                    ax.set_ylabel(spec.y_keys[0])
        fig.tight_layout()
        out_path = Path(out_path)
        fig.savefig(out_path, dpi=144)
    finally:
        plt.close(fig)
    return Path(out_path)


def _draw_part_to_whole(ax, spec: ChartSpec, cols, rows, xs) -> None:
    yi = cols.index(spec.y_keys[0])
    pairs = [(x, max(_num(r[yi]), 0.0)) for x, r in zip(xs, rows)]
    pairs.sort(key=lambda p: p[1], reverse=True)
    if len(pairs) > _MAX_PIE_SLICES:
        head = pairs[: _MAX_PIE_SLICES - 1]
        tail = sum(v for _, v in pairs[_MAX_PIE_SLICES - 1 :])
        pairs = head + [("Other", tail)]
    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    if sum(values) <= 0:
        raise EmptyChartData("part-to-whole chart needs at least one positive value")
    # A donut is a pie with the middle removed — matplotlib does this
    # exactly, via wedge width, so the report matches the browser rather
    # than approximating it.
    wedgeprops = {"width": 0.42} if spec.type == "donut" else None
    ax.pie(values, labels=labels, autopct="%1.1f%%", wedgeprops=wedgeprops)
    ax.axis("equal")


def _draw_horizontal_bar(ax, spec: ChartSpec, cols, rows, xs) -> None:
    y_cols = spec.y_keys
    n_series = len(y_cols)
    idx = list(range(len(rows)))
    lefts = [0.0] * len(rows)
    for k, ycol in enumerate(y_cols):
        yi = cols.index(ycol)
        vs = [_num(r[yi]) for r in rows]
        if spec.stacked or n_series == 1:
            ax.barh(idx, vs, left=list(lefts), label=ycol)
            lefts = [b + v for b, v in zip(lefts, vs)]
        else:
            height = 0.8 / n_series
            positions = [i + k * height for i in idx]
            ax.barh(positions, vs, height=height, label=ycol)
    if n_series > 1 and not spec.stacked:
        ticks = [i + 0.4 - (0.8 / n_series) / 2 for i in idx]
    else:
        ticks = idx
    ax.set_yticks(ticks)
    ax.set_yticklabels(xs)
    ax.invert_yaxis()  # highest value at the top, as the browser draws it
    if n_series > 1 and spec.show_legend:
        ax.legend()


def _draw_cartesian(ax, spec: ChartSpec, cols, rows, xs) -> None:
    y_cols = spec.y_keys
    n_series = len(y_cols)
    idx = list(range(len(rows)))
    # A histogram is a bar chart over pre-binned rows (chart_data.build_
    # histogram already did the binning in trusted code); bars touch.
    bar_like = spec.type in ("bar", "histogram")
    grouped_bars = spec.type == "bar" and n_series > 1 and not spec.stacked
    bottoms = [0.0] * len(rows)
    for k, ycol in enumerate(y_cols):
        yi = cols.index(ycol)
        ys = [_num(r[yi]) for r in rows]
        if bar_like:
            if spec.type == "histogram":
                ax.bar(idx, ys, width=1.0, align="edge", edgecolor="white", label=ycol)
            elif spec.stacked:
                ax.bar(idx, ys, bottom=list(bottoms), label=ycol)
                bottoms = [b + y for b, y in zip(bottoms, ys)]
            else:
                width = 0.8 / n_series
                positions = [i + k * width for i in idx]
                ax.bar(positions, ys, width=width, label=ycol)
        elif spec.type == "line":
            ax.plot(idx, ys, marker="o", label=ycol)
        elif spec.type == "area":
            ax.plot(idx, ys, label=ycol)
            ax.fill_between(idx, ys, alpha=0.3)
        elif spec.type == "scatter":
            ax.scatter(idx, ys, label=ycol)
    if grouped_bars:
        ticks = [i + 0.4 - (0.8 / n_series) / 2 for i in idx]
    elif spec.type == "histogram":
        ticks = [i + 0.5 for i in idx]
    else:
        ticks = idx
    ax.set_xticks(ticks)
    rotation = 45 if any(len(x) > 8 for x in xs) else 0
    ax.set_xticklabels(xs, rotation=rotation, ha="right" if rotation else "center")
    if n_series > 1 and spec.show_legend:
        ax.legend()


# Fail loudly at import if a ChartType has no report policy at all. This is
# the check that would have caught the blank-image bug when `donut` and
# friends were added.
_UNDECIDED = set(CHART_TYPES) - PNG_SUPPORTED - PNG_TABLE_ONLY
if _UNDECIDED:  # pragma: no cover - guarded by test_charts_png.py
    raise RuntimeError(
        f"ChartType(s) with no report rendering policy: {sorted(_UNDECIDED)}"
    )
