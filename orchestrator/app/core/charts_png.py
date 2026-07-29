"""Render VALIDATED ChartSpecs to PNG with matplotlib (Agg) for report docs.

matplotlib is imported lazily inside render_chart_png so importing this module
stays cheap and headless environments never need a display.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .chart_spec import ChartSpec


def _num(value: object) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # Decimal, numeric strings, ...
    except (TypeError, ValueError):
        return 0.0


def render_chart_png(
    spec: ChartSpec,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    out_path: str | Path,
) -> Path:
    """Render `spec` over (columns, rows) to `out_path` and return the path.

    Only pre-validated ChartSpec instances are accepted — never raw model
    output (spec §8: model output is parsed/validated, never executed).
    """
    if not isinstance(spec, ChartSpec):
        raise TypeError("render_chart_png requires a validated ChartSpec")

    import matplotlib

    matplotlib.use("Agg", force=True)  # headless
    import matplotlib.pyplot as plt

    cols = list(columns)
    xi = cols.index(spec.x_key)
    xs = [str(r[xi]) for r in rows]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    try:
        if spec.type == "pie":
            ycol = spec.y_keys[0]
            yi = cols.index(ycol)
            values = [max(_num(r[yi]), 0.0) for r in rows]
            ax.pie(values, labels=xs, autopct="%1.1f%%")
            ax.axis("equal")
        else:
            y_cols = spec.y_keys
            n_series = len(y_cols)
            idx = range(len(rows))
            grouped_bars = spec.type == "bar" and n_series > 1 and not spec.stacked
            bottoms = [0.0] * len(rows)
            for k, ycol in enumerate(y_cols):
                yi = cols.index(ycol)
                ys = [_num(r[yi]) for r in rows]
                if spec.type == "bar":
                    if spec.stacked:
                        ax.bar(list(idx), ys, bottom=list(bottoms), label=ycol)
                        bottoms = [b + y for b, y in zip(bottoms, ys)]
                    else:
                        width = 0.8 / n_series
                        positions = [i + k * width for i in idx]
                        ax.bar(positions, ys, width=width, label=ycol)
                elif spec.type == "line":
                    ax.plot(list(idx), ys, marker="o", label=ycol)
                elif spec.type == "area":
                    ax.plot(list(idx), ys, label=ycol)
                    ax.fill_between(list(idx), ys, alpha=0.3)
                elif spec.type == "scatter":
                    ax.scatter(list(idx), ys, label=ycol)
            if grouped_bars:
                ticks = [i + 0.4 - (0.8 / n_series) / 2 for i in idx]
            else:
                ticks = list(idx)
            ax.set_xticks(ticks)
            rotation = 45 if any(len(x) > 8 for x in xs) else 0
            ax.set_xticklabels(xs, rotation=rotation, ha="right" if rotation else "center")
            if n_series > 1:
                ax.legend()

        if spec.title:
            ax.set_title(spec.title)
        if spec.type != "pie":
            ax.set_xlabel(spec.x_key)
            if len(spec.y_keys) == 1:
                ax.set_ylabel(spec.y_keys[0])
        fig.tight_layout()
        out_path = Path(out_path)
        fig.savefig(out_path, dpi=144)
    finally:
        plt.close(fig)
    return Path(out_path)
