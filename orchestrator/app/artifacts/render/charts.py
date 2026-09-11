"""Chart (categories + series) → PNG with matplotlib Agg, in the theme palette.

The PDF and DOCX renderers embed raster charts; PPTX and XLSX draw native
ones from the same `Chart` data. This module follows core/charts_png.py's
conventions (Agg forced, lazy pyplot import, the five-colour palette in fixed
order, tight layout, the figure always closed) but takes an artifact `Chart`
rather than a ChartSpec over SQL rows, because the data here is already
columns of numbers the spec validated.

DETERMINISM. Two renders of the same Chart produce byte-identical PNGs:
matplotlib's PNG writer embeds no timestamp, the Agg backend is
deterministic, and the metadata block is pinned to nothing but the
"Software" key it always writes. `test_artifact_render_charts.py` renders
twice and compares bytes, so a caching layer keyed on the spec hash is safe.

160 dpi at 8 x 4.5 in gives 1280 x 720 px — sharp on an A4 page at the
6.3 in column width the print CSS uses, and the same bitmap serves the DOCX.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..spec import Chart
from . import theme

#: pie slices past this are folded into "Other" — matches charts_png.py and
#: MAX_SLICES in frontend/lib/chartOption.ts.
_MAX_PIE_SLICES = 8


def _fmt_value(value: float) -> str:
    """Bar labels: integers without decimals, otherwise one decimal place,
    thousands separated."""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def render_chart_png(chart: Chart, out_path: str | Path) -> Path:
    """Draw `chart` to `out_path` (PNG) and return the path."""
    if not isinstance(chart, Chart):
        raise TypeError("render_chart_png requires a validated artifact Chart")

    import matplotlib

    matplotlib.use("Agg", force=True)  # headless
    # Every string here is a person's or the model's text, never TeX. With
    # mathtext on, a finance title such as "Revenue $M vs $K" loses the
    # literal text between the dollars and "a $\frac$ b" raises a
    # ParseSyntaxException that would fail the whole render (review
    # 2026-09-11). The rcParam exists since matplotlib 3.6; the pin is >=3.8.
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt

    categories = list(chart.categories)
    fig, ax = plt.subplots(figsize=theme.CHART_FIGSIZE)
    fig.patch.set_facecolor(theme.WHITE)
    ax.set_facecolor(theme.WHITE)
    ax.set_prop_cycle(color=list(theme.PALETTE))
    try:
        if chart.type == "pie":
            _draw_pie(ax, categories, chart.series[0].values)
        elif chart.type == "horizontal_bar":
            _draw_horizontal_bar(ax, categories, chart)
        elif chart.type == "line":
            _draw_line(ax, categories, chart)
        else:
            _draw_bar(ax, categories, chart)

        _style_axes(ax, chart)
        if chart.title:
            ax.set_title(chart.title, fontsize=12, color=theme.INK, loc="left", pad=12)
        if chart.type != "pie":
            if chart.y_label and chart.type != "horizontal_bar":
                ax.set_ylabel(chart.y_label, color=theme.INK_MUTED, fontsize=9)
            elif chart.y_label:
                ax.set_xlabel(chart.y_label, color=theme.INK_MUTED, fontsize=9)
        if len(chart.series) > 1 and chart.type != "pie":
            ax.legend(frameon=False, fontsize=9, labelcolor=theme.INK_MUTED)
        fig.tight_layout()
        out_path = Path(out_path)
        fig.savefig(out_path, dpi=theme.CHART_DPI, format="png", metadata={"Software": None})
    finally:
        plt.close(fig)
    return Path(out_path)


def _style_axes(ax, chart: Chart) -> None:
    """The app's light chart chrome: no top/right spines, faint grid, muted
    tick labels (chartTheme.ts CHROME.light)."""
    if chart.type == "pie":
        ax.axis("equal")
        return
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(theme.BORDER)
    ax.tick_params(colors=theme.INK_MUTED, labelsize=9)
    axis = "x" if chart.type == "horizontal_bar" else "y"
    ax.grid(True, axis=axis, color=theme.BORDER, linewidth=0.8)
    ax.set_axisbelow(True)


def _draw_bar(ax, categories: Sequence[str], chart: Chart) -> None:
    n_series = len(chart.series)
    idx = list(range(len(categories)))
    width = 0.8 / n_series
    label_values = len(categories) <= theme.CHART_LABEL_MAX_CATEGORIES
    for k, series in enumerate(chart.series):
        positions = [i + k * width for i in idx]
        bars = ax.bar(positions, list(series.values), width=width, label=series.name, color=theme.series_colour(k))
        if label_values:
            ax.bar_label(bars, labels=[_fmt_value(v) for v in series.values], padding=2, fontsize=8, color=theme.INK_MUTED)
    ticks = [i + 0.4 - width / 2 for i in idx]
    ax.set_xticks(ticks)
    rotation = 45 if any(len(c) > 8 for c in categories) else 0
    ax.set_xticklabels(categories, rotation=rotation, ha="right" if rotation else "center")
    ax.margins(y=0.12)


def _draw_horizontal_bar(ax, categories: Sequence[str], chart: Chart) -> None:
    n_series = len(chart.series)
    idx = list(range(len(categories)))
    height = 0.8 / n_series
    label_values = len(categories) <= theme.CHART_LABEL_MAX_CATEGORIES
    for k, series in enumerate(chart.series):
        positions = [i + k * height for i in idx]
        bars = ax.barh(positions, list(series.values), height=height, label=series.name, color=theme.series_colour(k))
        if label_values:
            ax.bar_label(bars, labels=[_fmt_value(v) for v in series.values], padding=3, fontsize=8, color=theme.INK_MUTED)
    ticks = [i + 0.4 - height / 2 for i in idx]
    ax.set_yticks(ticks)
    ax.set_yticklabels(categories)
    ax.invert_yaxis()  # first category at the top, as the browser draws it
    ax.margins(x=0.12)


def _draw_line(ax, categories: Sequence[str], chart: Chart) -> None:
    idx = list(range(len(categories)))
    for k, series in enumerate(chart.series):
        ax.plot(idx, list(series.values), marker="o", markersize=4, linewidth=2, label=series.name, color=theme.series_colour(k))
    ax.set_xticks(idx)
    rotation = 45 if any(len(c) > 8 for c in categories) else 0
    ax.set_xticklabels(categories, rotation=rotation, ha="right" if rotation else "center")


def _draw_pie(ax, categories: Sequence[str], values: Sequence[float]) -> None:
    pairs = [(c, max(float(v), 0.0)) for c, v in zip(categories, values)]
    pairs.sort(key=lambda p: p[1], reverse=True)
    if len(pairs) > _MAX_PIE_SLICES:
        head = pairs[: _MAX_PIE_SLICES - 1]
        tail = sum(v for _, v in pairs[_MAX_PIE_SLICES - 1:])
        pairs = head + [("Other", tail)]
    labels = [p[0] for p in pairs]
    sizes = [p[1] for p in pairs]
    if sum(sizes) <= 0:
        # Every value zero or negative: a pie has nothing to show. Draw the
        # honest message rather than an empty circle with a title.
        ax.text(0.5, 0.5, "No positive values to chart", ha="center", va="center", color=theme.INK_MUTED, transform=ax.transAxes)
        ax.set_axis_off()
        return
    colours = [theme.series_colour(i) for i in range(len(pairs))]
    ax.pie(
        sizes, labels=labels, colors=colours, autopct="%1.1f%%", startangle=90, counterclock=False,
        textprops={"color": theme.INK_MUTED, "fontsize": 9},
        wedgeprops={"linewidth": 1.0, "edgecolor": theme.WHITE},
    )


__all__ = ["render_chart_png"]
